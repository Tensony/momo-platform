"""Fraud service: gathers facts from the database, runs the pure rules, records alerts.

Two entry points, both served by the fraud-engine Lambda (one deployable, two triggers):
  * check_transaction()   - synchronous, called by the saga before money is credited. Can DECLINE.
  * review_completed()    - asynchronous, driven by TransactionCompleted events from SNS -> SQS.
                            Looks back over 24h (rules that need hindsight) and raises REVIEW alerts.
SQS is at-least-once, so alert inserts are idempotent (UNIQUE (txn_id, decision)).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import psycopg
from psycopg.types.json import Jsonb

from ..common.db import transaction
from ..common.tracing import traced
from .config import FraudConfig, load_config
from .rules import ASYNC_RULES, SYNC_RULES, Decision, Evaluation, FraudContext, evaluate

log = logging.getLogger(__name__)


def build_context(conn: psycopg.Connection, txn: dict, cfg: FraudConfig) -> FraudContext:
    txn_id, wallet = str(txn["txn_id"]), str(txn["from_wallet"])
    kind_row = conn.execute("SELECT kind FROM wallets WHERE id = %s", (wallet,)).fetchone()
    recent = conn.execute(
        """SELECT count(*) AS n FROM transactions
            WHERE from_wallet = %s AND id <> %s AND status IN ('PENDING','COMPLETED')
              AND created_at >= now() - make_interval(secs => %s)""",
        (wallet, txn_id, cfg.velocity_window_s),
    ).fetchone()
    hist = conn.execute(
        """SELECT avg(amount)::float8 AS avg_amount, count(*) AS n FROM transactions
            WHERE from_wallet = %s AND id <> %s AND status = 'COMPLETED'
              AND created_at >= now() - interval '30 days'""",
        (wallet, txn_id),
    ).fetchone()
    last = conn.execute(
        """SELECT geo_lat, geo_lon, created_at FROM transactions
            WHERE from_wallet = %s AND id <> %s AND geo_lat IS NOT NULL
              AND status IN ('PENDING','COMPLETED')
            ORDER BY created_at DESC LIMIT 1""",
        (wallet, txn_id),
    ).fetchone()
    daily = conn.execute(
        """SELECT COALESCE(sum(amount), 0)::bigint AS total FROM transactions
            WHERE from_wallet = %s AND status = 'COMPLETED'
              AND created_at >= now() - interval '24 hours'""",
        (wallet,),
    ).fetchone()
    geo = (
        (float(txn["geo_lat"]), float(txn["geo_lon"]))
        if txn.get("geo_lat") is not None and txn.get("geo_lon") is not None
        else None
    )
    return FraudContext(
        txn_id=txn_id,
        txn_type=txn["type"],
        from_wallet=wallet,
        from_kind=kind_row["kind"] if kind_row else "UNKNOWN",
        amount=int(txn["amount"]),
        now=datetime.now(timezone.utc),
        recent_count=int(recent["n"]),
        avg_amount_30d=hist["avg_amount"],
        history_count_30d=int(hist["n"]),
        geo=geo,
        last_geo=(float(last["geo_lat"]), float(last["geo_lon"])) if last else None,
        last_geo_time=last["created_at"] if last else None,
        daily_outgoing_total=int(daily["total"]),
    )


def _record_alert(conn: psycopg.Connection, ctx: FraudContext, ev: Evaluation, cid: str | None):
    if ev.decision == Decision.APPROVE:
        return
    conn.execute(
        """INSERT INTO fraud_alerts (txn_id, wallet_id, decision, rules, correlation_id)
           VALUES (%s, %s, %s, %s, %s) ON CONFLICT (txn_id, decision) DO NOTHING""",
        (ctx.txn_id, ctx.from_wallet, ev.decision.value, Jsonb(ev.as_dict()["rules"]), cid),
    )


@traced("fraud.check_transaction")
def check_transaction(txn: dict, cfg: FraudConfig | None = None) -> dict:
    """Synchronous pre-credit check. `txn` is the saga context dict."""
    cfg = cfg or load_config()
    with transaction() as conn:
        ctx = build_context(conn, txn, cfg)
        ev = evaluate(ctx, cfg, SYNC_RULES)
        _record_alert(conn, ctx, ev, txn.get("correlation_id"))
    log.info(
        "fraud check complete",
        extra={"txn_id": ctx.txn_id, "decision": ev.decision.value, "rules": ev.as_dict()["rules"]},
    )
    return ev.as_dict()


@traced("fraud.review_completed")
def review_completed(event: dict, cfg: FraudConfig | None = None) -> dict:
    """Asynchronous post-completion review of a TransactionCompleted event."""
    cfg = cfg or load_config()
    txn = {
        "txn_id": event["txn_id"],
        "type": event.get("type", "TRANSFER"),
        "from_wallet": event["from_wallet"],
        "amount": event["amount"],
    }
    with transaction() as conn:
        ctx = build_context(conn, txn, cfg)
        ev = evaluate(ctx, cfg, ASYNC_RULES)
        _record_alert(conn, ctx, ev, event.get("correlation_id"))
    return ev.as_dict()
