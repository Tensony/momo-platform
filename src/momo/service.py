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


def list_wallets(
    owner_id: str, *, is_admin: bool, kind: str | None = None, status: str | None = None,
    currency: str | None = None, search: str | None = None, limit: int = 50, offset: int = 0,
) -> dict:
    filters, params = [], []
    if not is_admin:
        filters.append("owner_id = %s")
        params.append(owner_id)
    if kind:
        filters.append("kind = %s")
        params.append(kind)
    if status:
        filters.append("status = %s")
        params.append(status)
    if currency:
        filters.append("currency = %s")
        params.append(currency)
    if search:
        filters.append("(CAST(id AS text) ILIKE %s OR owner_id ILIKE %s)")
        params.extend((f"%{search}%", f"%{search}%"))
    where = f" WHERE {' AND '.join(filters)}" if filters else ""
    with transaction() as conn:
        total = conn.execute(f"SELECT count(*) AS n FROM wallets{where}", params).fetchone()["n"]
        rows = conn.execute(
            """SELECT id, owner_id, kind, currency, balance, status, created_at
                 FROM wallets""" + where + """ ORDER BY created_at DESC, id LIMIT %s OFFSET %s""",
            [*params, limit, offset],
        ).fetchall()
    return {
        "wallets": [
            {
                "id": str(row["id"]), "owner_id": row["owner_id"], "kind": row["kind"],
                "currency": row["currency"].strip(), "balance": row["balance"],
                "status": row["status"], "created_at": row["created_at"].isoformat(),
            }
            for row in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


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


def list_transactions(
    actor: str, *, is_admin: bool, txn_type: str | None = None, status: str | None = None,
    wallet_id: str | None = None, search: str | None = None, limit: int = 50, offset: int = 0,
) -> dict:
    filters, params = [], []
    if not is_admin:
        filters.append("(src.owner_id = %s OR dst.owner_id = %s)")
        params.extend((actor, actor))
    if txn_type:
        filters.append("t.type = %s")
        params.append(txn_type)
    if status:
        filters.append("t.status = %s")
        params.append(status)
    if wallet_id:
        filters.append("(t.from_wallet = %s OR t.to_wallet = %s)")
        params.extend((wallet_id, wallet_id))
    if search:
        filters.append(
            "(CAST(t.id AS text) ILIKE %s OR CAST(t.from_wallet AS text) ILIKE %s "
            "OR CAST(t.to_wallet AS text) ILIKE %s)"
        )
        params.extend((f"%{search}%", f"%{search}%", f"%{search}%"))
    where = f" WHERE {' AND '.join(filters)}" if filters else ""
    from_sql = (
        " FROM transactions t JOIN wallets src ON src.id = t.from_wallet"
        " JOIN wallets dst ON dst.id = t.to_wallet"
    )
    with transaction() as conn:
        total = conn.execute(f"SELECT count(*) AS n{from_sql}{where}", params).fetchone()["n"]
        rows = conn.execute(
            """SELECT t.id, t.type, t.status, t.from_wallet, t.to_wallet, t.amount, t.currency,
                      t.reason, t.reverses_txn_id, t.created_at""" + from_sql + where
            + """ ORDER BY t.created_at DESC, t.id LIMIT %s OFFSET %s""",
            [*params, limit, offset],
        ).fetchall()
    return {
        "transactions": [
            {
                "transaction_id": str(row["id"]), "type": row["type"], "status": row["status"],
                "from_wallet": str(row["from_wallet"]), "to_wallet": str(row["to_wallet"]),
                "amount": row["amount"], "currency": row["currency"].strip(),
                "reason": row["reason"],
                "reverses_transaction_id": str(row["reverses_txn_id"]) if row["reverses_txn_id"] else None,
                "created_at": row["created_at"].isoformat(),
            }
            for row in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def get_transaction_ledger(txn_id: str) -> list[dict]:
    txn_id = as_uuid(txn_id, "transaction")
    with transaction() as conn:
        rows = conn.execute(
            """SELECT e.id, e.txn_id, e.step, e.wallet_id, e.wallet_seq, e.direction,
                      e.amount, e.entry_type, e.created_at, w.currency
                 FROM ledger_entries e JOIN wallets w ON w.id = e.wallet_id
                WHERE e.txn_id = %s ORDER BY e.created_at, e.id""",
            (txn_id,),
        ).fetchall()
    return [
        {
            "entry_id": row["id"], "transaction_id": str(row["txn_id"]),
            "step": row["step"], "wallet_id": str(row["wallet_id"]),
            "wallet_seq": row["wallet_seq"], "direction": row["direction"],
            "amount": row["amount"], "currency": row["currency"].strip(),
            "entry_type": row["entry_type"], "created_at": row["created_at"].isoformat(),
        }
        for row in rows
    ]


def lookup_operation(actor: str, txn_type: str, key: str) -> dict:
    allowed_types = {"TRANSFER", "MERCHANT_PAYMENT", "CASH_IN", "CASH_OUT", "FLOAT_TOPUP", "REVERSAL"}
    if txn_type not in allowed_types:
        raise InvalidRequest("unsupported transaction type")
    key = idempotency.validate_key(key)
    scope = f"{actor}:{txn_type}"
    txn_id = idempotency.derive_txn_id(scope, key)
    with transaction() as conn:
        operation = conn.execute(
            """SELECT status, response_code, response_body FROM idempotency_keys
                WHERE scope = %s AND key = %s""",
            (scope, key),
        ).fetchone()
        if operation is None:
            raise NotFound("operation not found")
        row = conn.execute(
            """SELECT id, type, status, from_wallet, to_wallet, amount, currency, reason,
                      reverses_txn_id, created_at FROM transactions WHERE id = %s""",
            (txn_id,),
        ).fetchone()
    txn = None
    if row:
        txn = {
            "transaction_id": str(row["id"]), "type": row["type"], "status": row["status"],
            "from_wallet": str(row["from_wallet"]), "to_wallet": str(row["to_wallet"]),
            "amount": row["amount"], "currency": row["currency"].strip(), "reason": row["reason"],
            "reverses_transaction_id": str(row["reverses_txn_id"]) if row["reverses_txn_id"] else None,
            "created_at": row["created_at"].isoformat(),
        }
    return {
        "operation_status": operation["status"],
        "response_code": operation["response_code"],
        "response": operation["response_body"],
        "transaction": txn,
    }


def list_disputes(actor: str, *, is_admin: bool, limit: int = 50, offset: int = 0) -> dict:
    where = "" if is_admin else " WHERE d.raised_by = %s"
    params = [] if is_admin else [actor]
    with transaction() as conn:
        total = conn.execute(
            "SELECT count(*) AS n FROM disputes d" + where, params
        ).fetchone()["n"]
        rows = conn.execute(
            """SELECT d.id, d.txn_id, d.raised_by, d.reason, d.status, d.resolution_note,
                      d.created_at, d.resolved_at FROM disputes d""" + where
            + """ ORDER BY d.created_at DESC, d.id LIMIT %s OFFSET %s""",
            [*params, limit, offset],
        ).fetchall()
    return {
        "disputes": [
            {
                "dispute_id": str(row["id"]), "transaction_id": str(row["txn_id"]),
                "raised_by": row["raised_by"], "reason": row["reason"], "status": row["status"],
                "resolution_note": row["resolution_note"],
                "created_at": row["created_at"].isoformat(),
                "resolved_at": row["resolved_at"].isoformat() if row["resolved_at"] else None,
            }
            for row in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def list_fraud_alerts(limit: int = 50, offset: int = 0) -> dict:
    with transaction() as conn:
        total = conn.execute("SELECT count(*) AS n FROM fraud_alerts").fetchone()["n"]
        rows = conn.execute(
            """SELECT a.id, a.txn_id, a.wallet_id, a.decision, a.rules, a.created_at,
                      t.type, t.status, t.amount, t.currency
                 FROM fraud_alerts a LEFT JOIN transactions t ON t.id = a.txn_id
                ORDER BY a.created_at DESC, a.id DESC LIMIT %s OFFSET %s""",
            (limit, offset),
        ).fetchall()
    return {
        "alerts": [
            {
                "alert_id": row["id"], "transaction_id": str(row["txn_id"]),
                "wallet_id": str(row["wallet_id"]), "decision": row["decision"],
                "rules": row["rules"], "created_at": row["created_at"].isoformat(),
                "transaction_type": row["type"], "transaction_status": row["status"],
                "amount": row["amount"], "currency": row["currency"].strip() if row["currency"] else None,
            }
            for row in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def list_settlement_reports(limit: int = 50, offset: int = 0) -> dict:
    with transaction() as conn:
        total = conn.execute("SELECT count(*) AS n FROM settlement_reports").fetchone()["n"]
        rows = conn.execute(
            """SELECT id, report_date, currency, summary, created_at
                 FROM settlement_reports ORDER BY report_date DESC, id DESC LIMIT %s OFFSET %s""",
            (limit, offset),
        ).fetchall()
    return {
        "reports": [
            {
                "report_id": row["id"], "report_date": row["report_date"].isoformat(),
                "currency": row["currency"].strip(), "summary": row["summary"],
                "created_at": row["created_at"].isoformat(),
            }
            for row in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
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
