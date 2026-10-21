"""Application service: the use-cases behind the HTTP API (and the 'local' proof backend).

Authorization (who may touch which wallet) is decided in the API layer; this layer enforces the
business invariants, idempotency and saga execution.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass

from . import idempotency
from .common.db import transaction
from .common.errors import (
    AlreadyReversed,
    BusinessRejection,
    InvalidRequest,
    MomoError,
    NotFound,
    NotReversible,
)
from .common.hashing import hash_request
from .common.money import parse_amount, parse_currency
from .ledger import core as ledger
from .saga.context import SagaResult, TxnContext
from .saga.orchestrator import run_saga

log = logging.getLogger(__name__)


def as_uuid(value, what: str = "resource") -> str:
    """Validate an id from untrusted input; malformed ids are simply 'not found'."""
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        raise NotFound(f"{what} not found") from None


# --------------------------------------------------------------------------- saga runners
class LocalRunner:
    """Runs the saga in this process (tests, proofs, local dev)."""

    def run(self, ctx: TxnContext) -> SagaResult:
        return run_saga(ctx)


class StepFunctionsRunner:
    """Runs the saga on an EXPRESS Step Functions state machine, synchronously.

    Express is at-least-once, which is safe because every step is idempotent (txn_id, step)."""

    def __init__(self, state_machine_arn: str, client=None):
        import boto3

        self.arn = state_machine_arn
        self.client = client or boto3.client("stepfunctions")

    def run(self, ctx: TxnContext) -> SagaResult:
        resp = self.client.start_sync_execution(
            stateMachineArn=self.arn, input=json.dumps({"ctx": ctx.to_dict()})
        )
        if resp["status"] != "SUCCEEDED":
            # The machine catches step errors itself, so this means the execution could not run
            # to a decision. Outcome unknown => do NOT answer; the idempotency lease expires and a
            # retry resumes the (idempotent) saga safely.
            raise MomoError(f"saga execution {resp['status']}: {resp.get('cause', '')[:200]}")
        out = json.loads(resp["output"])
        return SagaResult(
            txn_id=ctx.txn_id, outcome=out["outcome"], code=out.get("code"), detail=out.get("detail", {})
        )


def default_runner():
    if os.environ.get("SAGA_MODE", "local") == "stepfunctions":
        return StepFunctionsRunner(os.environ["STATE_MACHINE_ARN"])
    return LocalRunner()


# --------------------------------------------------------------------------- wallets
def create_wallet(owner_id: str, kind: str, currency: str | None = None) -> dict:
    with transaction() as conn:
        return ledger.create_wallet(conn, owner_id, kind, parse_currency(currency))


def get_wallet(wallet_id: str) -> dict:
    with transaction() as conn:
        return ledger.get_wallet(conn, wallet_id)


def get_ledger(wallet_id: str, limit: int = 50) -> list[dict]:
    with transaction() as conn:
        ledger.get_wallet(conn, wallet_id)  # 404 if missing
        return ledger.get_ledger(conn, wallet_id, limit)


def get_transaction(txn_id: str) -> dict:
    txn_id = as_uuid(txn_id, "transaction")
    with transaction() as conn:
        row = conn.execute(
            """SELECT id, type, status, from_wallet, to_wallet, amount, currency, reason,
                      reverses_txn_id, created_at FROM transactions WHERE id = %s""",
            (txn_id,),
        ).fetchone()
    if row is None:
        raise NotFound("transaction not found")
    return {
        "transaction_id": str(row["id"]), "type": row["type"], "status": row["status"],
        "from_wallet": str(row["from_wallet"]), "to_wallet": str(row["to_wallet"]),
        "amount": row["amount"], "currency": row["currency"], "reason": row["reason"],
        "reverses_transaction_id": str(row["reverses_txn_id"]) if row["reverses_txn_id"] else None,
        "created_at": row["created_at"].isoformat(),
    }


# --------------------------------------------------------------------------- transactions
@dataclass
class TxnRequest:
    type: str
    from_wallet: str
    to_wallet: str
    amount: int
    currency: str = "ZMW"
    geo: tuple[float, float] | None = None
    reason: str | None = None
    reverses_txn_id: str | None = None


def parse_geo(raw) -> tuple[float, float] | None:
    if raw is None:
        return None
    try:
        lat, lon = float(raw["lat"]), float(raw["lon"])
    except (KeyError, TypeError, ValueError):
        raise InvalidRequest("geo must be {\"lat\": number, \"lon\": number}") from None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise InvalidRequest("geo coordinates out of range")
    return lat, lon


def parse_txn_request(txn_type: str, body: dict) -> TxnRequest:
    if not isinstance(body, dict):
        raise InvalidRequest("JSON object body required")
    for field in ("from_wallet", "to_wallet"):
        if not isinstance(body.get(field), str):
            raise InvalidRequest(f"{field} is required")
    return TxnRequest(
        type=txn_type,
        from_wallet=body["from_wallet"],
        to_wallet=body["to_wallet"],
        amount=parse_amount(body.get("amount")),
        currency=parse_currency(body.get("currency")),
        geo=parse_geo(body.get("geo")),
    )


def _terminal_outcome(txn_id: str) -> SagaResult | None:
    """If this transaction already reached a final state (e.g. API died before replying), reuse it."""
    with transaction() as conn:
        row = conn.execute(
            "SELECT status, reason FROM transactions WHERE id = %s", (txn_id,)
        ).fetchone()
    if not row:
        return None
    if row["status"] in ("COMPLETED", "REVERSED"):
        return SagaResult(txn_id, "COMPLETED")
    if row["status"] == "REJECTED":
        return SagaResult(txn_id, "REJECTED", row["reason"])
    if row["status"] == "FAILED":
        return SagaResult(txn_id, "FAILED", "SAGA_FAILED_COMPENSATED")
    return None


_REJECTION_MESSAGES = {
    "INSUFFICIENT_FUNDS": "The paying wallet does not have enough funds.",
    "FRAUD_DECLINED": "The transaction was declined by fraud screening.",
    "WALLET_NOT_ACTIVE": "One of the wallets is not active.",
    "WALLET_NOT_FOUND": "One of the wallets does not exist.",
    "CURRENCY_MISMATCH": "The wallets use different currencies.",
    "ALREADY_REVERSED": "This transaction already has a reversal in progress or completed.",
}


def to_http(result: SagaResult) -> tuple[int, dict]:
    body = {"transaction_id": result.txn_id, "status": result.outcome}
    if result.outcome == "COMPLETED":
        if result.detail.get("fraud_decision") == "REVIEW":
            body["flagged_for_review"] = True
        return 201, body
    body["code"] = result.code
    if result.outcome == "REJECTED":
        body["message"] = _REJECTION_MESSAGES.get(result.code, "The transaction was rejected.")
        return 422, body
    body["message"] = (
        "The transaction failed and any held funds were returned."
        if result.code == "SAGA_FAILED_COMPENSATED"
        else "The transaction failed; funds are safe and the case is being investigated."
    )
    return 500, body


def submit(
    actor: str,
    req: TxnRequest,
    idem_key: str | None,
    correlation_id: str | None,
    *,
    fault: str | None = None,
    runner=None,
) -> tuple[int, dict, bool]:
    """Execute a transaction exactly once per (actor, type, Idempotency-Key).

    Returns (http_status, body, replayed)."""
    key = idempotency.validate_key(idem_key)
    scope = f"{actor}:{req.type}"
    payload = {
        "type": req.type, "from": req.from_wallet, "to": req.to_wallet, "amount": req.amount,
        "currency": req.currency, "geo": req.geo, "reverses": req.reverses_txn_id,
    }
    begun = idempotency.begin(scope, key, hash_request(payload))
    if begun.state == "COMPLETED":
        return begun.response_code, begun.response_body, True
    if begun.state == "IN_PROGRESS":
        done = idempotency.wait_for_completion(scope, key)  # RequestInProgress (409) on timeout
        return done.response_code, done.response_body, True

    txn_id = idempotency.derive_txn_id(scope, key)
    result = _terminal_outcome(txn_id)
    if result is None:
        ctx = TxnContext(
            txn_id=txn_id, type=req.type, from_wallet=req.from_wallet, to_wallet=req.to_wallet,
            amount=req.amount, currency=req.currency,
            geo_lat=req.geo[0] if req.geo else None, geo_lon=req.geo[1] if req.geo else None,
            correlation_id=correlation_id, created_by=actor, reverses_txn_id=req.reverses_txn_id,
            reason=req.reason, fault=fault,
        )
        result = (runner or default_runner()).run(ctx)
    code, body = to_http(result)
    idempotency.complete(scope, key, code, body)
    return code, body, False


# --------------------------------------------------------------------------- reversals & disputes
def reverse_transaction(
    actor: str, txn_id: str, reason: str | None, idem_key: str | None,
    correlation_id: str | None, *, runner=None,
) -> tuple[int, dict, bool]:
    """Reverse a completed transaction with a NEW compensating transaction (payee -> payer).
    The original ledger entries are never modified - the ledger stays append-only."""
    txn_id = as_uuid(txn_id, "transaction")
    with transaction() as conn:
        orig = conn.execute(
            "SELECT id, type, status, from_wallet, to_wallet, amount, currency "
            "FROM transactions WHERE id = %s", (txn_id,),
        ).fetchone()
    if orig is None:
        raise NotFound("transaction not found")
    if orig["status"] == "REVERSED":
        raise AlreadyReversed("transaction already reversed")
    if orig["status"] != "COMPLETED" or orig["type"] == "REVERSAL":
        raise NotReversible("only completed, non-reversal transactions can be reversed")
    req = TxnRequest(
        type="REVERSAL", from_wallet=str(orig["to_wallet"]), to_wallet=str(orig["from_wallet"]),
        amount=orig["amount"], currency=orig["currency"].strip(), reason=reason,
        reverses_txn_id=str(orig["id"]),
    )
    return submit(actor, req, idem_key, correlation_id, runner=runner)


def raise_dispute(actor: str, txn_id: str, reason: str) -> dict:
    if not isinstance(reason, str) or not (3 <= len(reason) <= 500):
        raise InvalidRequest("reason must be 3-500 characters")
    txn = get_transaction(txn_id)
    with transaction() as conn:
        row = conn.execute(
            """INSERT INTO disputes (txn_id, raised_by, reason) VALUES (%s, %s, %s)
               ON CONFLICT DO NOTHING RETURNING id, status, created_at""",
            (txn["transaction_id"], actor, reason),
        ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT id, status, created_at FROM disputes WHERE txn_id = %s AND status = 'OPEN'",
                (txn["transaction_id"],),
            ).fetchone()
    return {
        "dispute_id": str(row["id"]), "transaction_id": txn["transaction_id"],
        "status": row["status"], "created_at": row["created_at"].isoformat(),
    }


def get_dispute(dispute_id: str) -> dict:
    dispute_id = as_uuid(dispute_id, "dispute")
    with transaction() as conn:
        row = conn.execute("SELECT * FROM disputes WHERE id = %s", (dispute_id,)).fetchone()
    if row is None:
        raise NotFound("dispute not found")
    return {
        "dispute_id": str(row["id"]), "transaction_id": str(row["txn_id"]),
        "raised_by": row["raised_by"], "reason": row["reason"], "status": row["status"],
        "resolution_note": row["resolution_note"],
    }


def resolve_dispute(
    admin: str, dispute_id: str, action: str, note: str | None, correlation_id: str | None,
    *, runner=None,
) -> dict:
    if action not in ("REVERSE", "REJECT"):
        raise InvalidRequest("action must be REVERSE or REJECT")
    d = get_dispute(dispute_id)
    if d["status"] != "OPEN":
        raise InvalidRequest("dispute is already resolved")
    new_status = "RESOLVED_REJECTED"
    if action == "REVERSE":
        # Key derived from the dispute id: resolving twice cannot reverse twice.
        code, body, _ = reverse_transaction(
            admin, d["transaction_id"], f"dispute {dispute_id}", f"dispute-{dispute_id}",
            correlation_id, runner=runner,
        )
        if code != 201:
            raise BusinessRejection(
                f"reversal could not be completed ({body.get('code')}); dispute stays open",
                reversal=body,
            )
        new_status = "RESOLVED_REVERSED"
    with transaction() as conn:
        conn.execute(
            """UPDATE disputes SET status = %s, resolution_note = %s, resolved_by = %s,
                      resolved_at = now() WHERE id = %s AND status = 'OPEN'""",
            (new_status, note, admin, str(dispute_id)),
        )
    return get_dispute(dispute_id)
