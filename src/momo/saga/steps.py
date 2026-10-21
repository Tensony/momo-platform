"""Saga steps. Each step is ONE local ACID database transaction and is idempotent.

Money path of a successful transaction (payer -> payee):

    reserve   payer  -> ESCROW        (hold funds; fails fast on insufficient funds)
    fraud     (no money moves)        (decision; DECLINE triggers compensation)
    credit    ESCROW -> payee
    finalize  mark COMPLETED, write the TransactionCompleted outbox event

Compensations - decided from DATABASE state, never from orchestrator memory, so they are safe to
call blindly after any failure or timeout, any number of times:

    reverse_credit  payee  -> ESCROW   only if `credit` applied and not yet reversed
    release         ESCROW -> payer    only if `reserve` applied and not yet released

Business outcomes (insufficient funds, frozen wallet) are RETURNED as {"ok": False, "code": ...};
only technical failures raise. Step Functions and the in-process orchestrator therefore follow
the same decision logic.
"""

from __future__ import annotations

import psycopg
from psycopg.types.json import Jsonb

from ..common.db import transaction
from ..common.errors import BusinessRejection, CompensationOrderError
from ..common.tracing import traced
from ..fraud import service as fraud_service
from ..ledger.core import ESCROW_WALLET_ID, move, step_applied
from . import faults
from .context import TxnContext


def _ok() -> dict:
    return {"ok": True}


def _set_saga(conn, txn_id: str, state: str, error: str | None = None) -> None:
    conn.execute(
        """INSERT INTO sagas (txn_id, state, last_error) VALUES (%s, %s, %s)
           ON CONFLICT (txn_id) DO UPDATE
              SET state = EXCLUDED.state, last_error = EXCLUDED.last_error, updated_at = now()""",
        (txn_id, state, error),
    )


def _ensure_transaction_row(conn, ctx: TxnContext) -> str | None:
    """Create the PENDING transaction row if absent. Returns None on success, or a rejection code.

    Uses a savepoint so a constraint violation does not poison the surrounding transaction.
    """
    try:
        with conn.transaction():
            conn.execute(
                """INSERT INTO transactions
                     (id, type, status, from_wallet, to_wallet, amount, currency, reverses_txn_id,
                      reason, geo_lat, geo_lon, correlation_id, created_by)
                   VALUES (%s,%s,'PENDING',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (id) DO NOTHING""",
                (
                    ctx.txn_id, ctx.type, ctx.from_wallet, ctx.to_wallet, ctx.amount, ctx.currency,
                    ctx.reverses_txn_id, ctx.reason, ctx.geo_lat, ctx.geo_lon, ctx.correlation_id,
                    ctx.created_by,
                ),
            )
    except psycopg.errors.UniqueViolation:
        return "ALREADY_REVERSED"  # txn_one_reversal_idx: another reversal is pending/completed
    except (psycopg.errors.ForeignKeyViolation, psycopg.errors.InvalidTextRepresentation):
        return "WALLET_NOT_FOUND"
    return None


def _row_exists(conn, txn_id: str) -> bool:
    return conn.execute("SELECT 1 FROM transactions WHERE id = %s", (txn_id,)).fetchone() is not None


# --------------------------------------------------------------------------- forward steps
@traced("saga.reserve")
def reserve(ctx: TxnContext) -> dict:
    try:
        with transaction() as conn:
            rejection = _ensure_transaction_row(conn, ctx)
            if rejection:
                return {"ok": False, "code": rejection, "detail": {}}
            move(
                conn, txn_id=ctx.txn_id, step="reserve", from_wallet=ctx.from_wallet,
                to_wallet=ESCROW_WALLET_ID, amount=ctx.amount, entry_type="HOLD",
                correlation_id=ctx.correlation_id,
            )
            _set_saga(conn, ctx.txn_id, "RESERVED")
    except BusinessRejection as exc:
        return {"ok": False, "code": exc.code, "detail": exc.details}
    faults.check(ctx.fault, "reserve.after")
    return _ok()


@traced("saga.fraud_check")
def fraud_check(ctx: TxnContext) -> dict:
    """In-process fraud check (local mode). In AWS the state machine invokes the fraud Lambda."""
    faults.check(ctx.fault, "fraud.before")
    return fraud_service.check_transaction(ctx.to_dict())


@traced("saga.credit")
def credit(ctx: TxnContext) -> dict:
    faults.check(ctx.fault, "credit.before")
    try:
        with transaction() as conn:
            if not step_applied(conn, ctx.txn_id, "reserve"):
                raise CompensationOrderError("credit attempted without a reserve")
            move(
                conn, txn_id=ctx.txn_id, step="credit", from_wallet=ESCROW_WALLET_ID,
                to_wallet=ctx.to_wallet, amount=ctx.amount, entry_type=ctx.type,
                correlation_id=ctx.correlation_id,
            )
            _set_saga(conn, ctx.txn_id, "CREDITED")
    except BusinessRejection as exc:  # e.g. payee frozen: caller releases the hold
        return {"ok": False, "code": exc.code, "detail": exc.details}
    faults.check(ctx.fault, "credit.after")
    return _ok()


@traced("saga.finalize")
def finalize(ctx: TxnContext) -> dict:
    faults.check(ctx.fault, "finalize.before")
    with transaction() as conn:
        conn.execute(
            "UPDATE transactions SET status = 'COMPLETED', updated_at = now() WHERE id = %s",
            (ctx.txn_id,),
        )
        if ctx.type == "REVERSAL" and ctx.reverses_txn_id:
            conn.execute(
                "UPDATE transactions SET status = 'REVERSED', updated_at = now() WHERE id = %s",
                (ctx.reverses_txn_id,),
            )
        conn.execute(
            """INSERT INTO outbox_events (event_type, txn_id, payload, correlation_id)
               VALUES ('TransactionCompleted', %s, %s, %s)
               ON CONFLICT (event_type, txn_id) DO NOTHING""",
            (
                ctx.txn_id,
                Jsonb(
                    {
                        "txn_id": ctx.txn_id, "type": ctx.type, "from_wallet": ctx.from_wallet,
                        "to_wallet": ctx.to_wallet, "amount": ctx.amount,
                        "currency": ctx.currency, "correlation_id": ctx.correlation_id,
                    }
                ),
                ctx.correlation_id,
            ),
        )
        _set_saga(conn, ctx.txn_id, "COMPLETED")
    return _ok()


# --------------------------------------------------------------------------- compensations
@traced("saga.reverse_credit")
def reverse_credit(ctx: TxnContext) -> dict:
    with transaction() as conn:
        if step_applied(conn, ctx.txn_id, "credit") and not step_applied(
            conn, ctx.txn_id, "reverse_credit"
        ):
            move(
                conn, txn_id=ctx.txn_id, step="reverse_credit", from_wallet=ctx.to_wallet,
                to_wallet=ESCROW_WALLET_ID, amount=ctx.amount, entry_type="COMPENSATION",
                correlation_id=ctx.correlation_id, check_status=False,
            )
            _set_saga(conn, ctx.txn_id, "COMPENSATING")
    return _ok()


@traced("saga.release")
def release(ctx: TxnContext) -> dict:
    with transaction() as conn:
        has_credit = step_applied(conn, ctx.txn_id, "credit")
        credit_reversed = step_applied(conn, ctx.txn_id, "reverse_credit")
        if has_credit and not credit_reversed:
            raise CompensationOrderError("release before reverse_credit would strand the payee credit")
        if step_applied(conn, ctx.txn_id, "reserve") and not step_applied(
            conn, ctx.txn_id, "release"
        ):
            move(
                conn, txn_id=ctx.txn_id, step="release", from_wallet=ESCROW_WALLET_ID,
                to_wallet=ctx.from_wallet, amount=ctx.amount, entry_type="COMPENSATION",
                correlation_id=ctx.correlation_id, check_status=False,
            )
            _set_saga(conn, ctx.txn_id, "COMPENSATING")
    return _ok()


# --------------------------------------------------------------------------- terminal marks
def mark_rejected(ctx: TxnContext, code: str) -> dict:
    """Nothing was (or nothing remains) moved. Records the reason for audit and the client."""
    with transaction() as conn:
        _ensure_transaction_row(conn, ctx)  # best effort: may be impossible (e.g. unknown wallet)
        conn.execute(
            """UPDATE transactions SET status = 'REJECTED', reason = %s, updated_at = now()
                WHERE id = %s AND status = 'PENDING'""",
            (code, ctx.txn_id),
        )
        if _row_exists(conn, ctx.txn_id):
            _set_saga(conn, ctx.txn_id, "REJECTED", code)
    return _ok()


def mark_failed(ctx: TxnContext, error: str) -> dict:
    """Technical failure; compensation has run, so funds are back where they started."""
    with transaction() as conn:
        _ensure_transaction_row(conn, ctx)
        conn.execute(
            """UPDATE transactions SET status = 'FAILED', reason = %s, updated_at = now()
                WHERE id = %s AND status = 'PENDING'""",
            (error[:200], ctx.txn_id),
        )
        if _row_exists(conn, ctx.txn_id):
            _set_saga(conn, ctx.txn_id, "FAILED", error[:500])
    return _ok()


def mark_compensation_failed(ctx: TxnContext, error: str) -> None:
    """Compensation itself failed. Money is conserved (it sits in ESCROW) but a human must act."""
    with transaction() as conn:
        if _row_exists(conn, ctx.txn_id):
            _set_saga(conn, ctx.txn_id, "COMPENSATION_FAILED", error[:500])
