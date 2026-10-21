"""Reconciliation and settlement.

Reconciliation answers "is the books-and-balances picture internally consistent?" with checks that
must ALL hold; any failure is a P1 (see docs/RUNBOOK.md):

  1. Conservation      SUM(wallet balances) == 0            (MINT is negative by e-money issued)
  2. Ledger balance    SUM(credits) - SUM(debits) == 0       across the whole ledger
  3. Step balance      every (txn, step) has debits == credits
  4. Wallet vs ledger  each wallet.balance == SUM of its own ledger entries
  5. Hash chains       every wallet chain recomputes (tamper evidence)
  6. Stuck sagas       transactions PENDING for > 5 minutes, or COMPENSATION_FAILED sagas
  7. Escrow            ESCROW balance == amount held by in-flight transactions

Settlement summarises the day's completed money movement per type, per merchant and per agent.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from ..common import metrics
from ..common.db import transaction
from ..ledger.core import ESCROW_WALLET_ID, verify_wallet_chain

log = logging.getLogger(__name__)

STUCK_AFTER_MINUTES = 5


def _checks(conn) -> dict:
    issues: dict[str, object] = {}

    total = conn.execute("SELECT COALESCE(sum(balance),0)::bigint AS s FROM wallets").fetchone()["s"]
    if total != 0:
        issues["conservation_sum_of_balances"] = total

    ledger_total = conn.execute(
        """SELECT COALESCE(sum(CASE direction WHEN 'CREDIT' THEN amount ELSE -amount END),0)::bigint AS s
             FROM ledger_entries"""
    ).fetchone()["s"]
    if ledger_total != 0:
        issues["ledger_net"] = ledger_total

    unbalanced = conn.execute(
        """SELECT txn_id, step FROM ledger_entries GROUP BY txn_id, step
           HAVING sum(CASE direction WHEN 'CREDIT' THEN amount ELSE -amount END) <> 0 LIMIT 20"""
    ).fetchall()
    if unbalanced:
        issues["unbalanced_steps"] = [{"txn_id": str(r["txn_id"]), "step": r["step"]} for r in unbalanced]

    drift = conn.execute(
        """SELECT w.id, w.balance, COALESCE(l.net,0)::bigint AS ledger_net
             FROM wallets w LEFT JOIN (
                SELECT wallet_id, sum(CASE direction WHEN 'CREDIT' THEN amount ELSE -amount END) AS net
                  FROM ledger_entries GROUP BY wallet_id) l ON l.wallet_id = w.id
            WHERE w.balance <> COALESCE(l.net,0) LIMIT 20"""
    ).fetchall()
    if drift:
        issues["wallet_ledger_drift"] = [
            {"wallet_id": str(r["id"]), "balance": r["balance"], "ledger_net": r["ledger_net"]}
            for r in drift
        ]

    broken = []
    for row in conn.execute("SELECT id FROM wallets WHERE entry_seq > 0").fetchall():
        res = verify_wallet_chain(conn, str(row["id"]))
        if not res.ok:
            broken.append({"wallet_id": res.wallet_id, "errors": res.errors[:3]})
    if broken:
        issues["broken_hash_chains"] = broken

    stuck = conn.execute(
        """SELECT count(*) AS n FROM transactions
            WHERE status = 'PENDING' AND created_at < now() - make_interval(mins => %s)""",
        (STUCK_AFTER_MINUTES,),
    ).fetchone()["n"]
    comp_failed = conn.execute(
        "SELECT count(*) AS n FROM sagas WHERE state = 'COMPENSATION_FAILED'"
    ).fetchone()["n"]
    if stuck or comp_failed:
        issues["stuck_sagas"] = {"pending_over_5min": stuck, "compensation_failed": comp_failed}

    escrow = conn.execute(
        "SELECT balance FROM wallets WHERE id = %s", (ESCROW_WALLET_ID,)
    ).fetchone()["balance"]
    held = conn.execute(
        """SELECT COALESCE(sum(t.amount),0)::bigint AS s FROM transactions t
            WHERE EXISTS (SELECT 1 FROM ledger_entries e WHERE e.txn_id = t.id AND e.step='reserve')
              AND NOT EXISTS (SELECT 1 FROM ledger_entries e WHERE e.txn_id = t.id AND e.step IN ('credit','release'))"""
    ).fetchone()["s"]
    # Money in escrow = reserved but neither credited nor released (a credit that was reversed also
    # leaves its reserve outstanding until release - handled by the NOT EXISTS above being on release).
    in_flight_after_reversal = conn.execute(
        """SELECT COALESCE(sum(t.amount),0)::bigint AS s FROM transactions t
            WHERE EXISTS (SELECT 1 FROM ledger_entries e WHERE e.txn_id = t.id AND e.step='reverse_credit')
              AND NOT EXISTS (SELECT 1 FROM ledger_entries e WHERE e.txn_id = t.id AND e.step='release')"""
    ).fetchone()["s"]
    if escrow != held + in_flight_after_reversal:
        issues["escrow_mismatch"] = {"escrow": escrow, "expected": held + in_flight_after_reversal}
    return issues


def _settlement(conn, day: date, currency: str) -> dict:
    start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    by_type = conn.execute(
        """SELECT type, status, count(*) AS n, COALESCE(sum(amount),0)::bigint AS total
             FROM transactions WHERE created_at >= %s AND created_at < %s AND currency = %s
            GROUP BY type, status ORDER BY type, status""",
        (start, end, currency),
    ).fetchall()
    merchants = conn.execute(
        """SELECT t.to_wallet AS wallet_id, count(*) AS n, sum(t.amount)::bigint AS gross
             FROM transactions t WHERE t.type = 'MERCHANT_PAYMENT' AND t.status IN ('COMPLETED','REVERSED')
              AND t.created_at >= %s AND t.created_at < %s AND t.currency = %s
            GROUP BY t.to_wallet ORDER BY gross DESC LIMIT 100""",
        (start, end, currency),
    ).fetchall()
    agents = conn.execute(
        """SELECT w.id AS wallet_id, w.balance AS float_balance,
                  COALESCE(sum(t.amount) FILTER (WHERE t.type='CASH_IN'),0)::bigint  AS cash_in,
                  COALESCE(sum(t.amount) FILTER (WHERE t.type='CASH_OUT'),0)::bigint AS cash_out
             FROM wallets w LEFT JOIN transactions t
               ON (t.from_wallet = w.id OR t.to_wallet = w.id) AND t.status = 'COMPLETED'
              AND t.created_at >= %s AND t.created_at < %s
            WHERE w.kind = 'AGENT' AND w.currency = %s GROUP BY w.id, w.balance LIMIT 100""",
        (start, end, currency),
    ).fetchall()
    return {
        "date": day.isoformat(),
        "currency": currency,
        "by_type_and_status": [
            {"type": r["type"], "status": r["status"], "count": r["n"], "amount": r["total"]}
            for r in by_type
        ],
        "merchant_settlements": [
            {"wallet_id": str(r["wallet_id"]), "payments": r["n"], "gross": r["gross"]} for r in merchants
        ],
        "agent_float": [
            {"wallet_id": str(r["wallet_id"]), "float_balance": r["float_balance"],
             "cash_in": r["cash_in"], "cash_out": r["cash_out"]} for r in agents
        ],
    }


def check_integrity() -> dict:
    """Read-only run of the integrity checks (used by the admin API and the correctness proofs)."""
    with transaction() as conn:
        issues = _checks(conn)
    return {"status": "DISCREPANCY" if issues else "OK", "issues": issues}


def run_reconciliation(report_date: date | None = None, currency: str = "ZMW") -> dict:
    day = report_date or (datetime.now(timezone.utc).date() - timedelta(days=1))
    with transaction() as conn:
        issues = _checks(conn)
        settlement = _settlement(conn, day, currency)
        report = {"status": "DISCREPANCY" if issues else "OK", "issues": issues, "settlement": settlement}
        conn.execute(
            """INSERT INTO settlement_reports (report_date, currency, summary) VALUES (%s,%s,%s)
               ON CONFLICT (report_date, currency) DO UPDATE SET summary = EXCLUDED.summary, created_at = now()""",
            (day, currency, Jsonb(report)),
        )
    metrics.emit("ReconciliationDiscrepancies", len(issues), Service="reconciliation")
    level = logging.ERROR if issues else logging.INFO
    log.log(level, "reconciliation finished", extra={"status": report["status"], "issues": list(issues)})
    _archive_to_s3(day, report)
    return report


def _archive_to_s3(day: date, report: dict) -> None:
    bucket = os.environ.get("SETTLEMENT_BUCKET")
    if not bucket:
        return
    import boto3

    boto3.client("s3").put_object(
        Bucket=bucket,
        Key=f"settlement/{day.isoformat()}.json",
        Body=json.dumps(report, default=str).encode(),
        ContentType="application/json",
        ServerSideEncryption="aws:kms",
    )
