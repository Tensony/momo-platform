import json
from datetime import date

import psycopg
import pytest

from momo.common.db import transaction
from momo.events import outbox
from momo.fraud import handler as fraud_handler
from momo.ledger.core import ESCROW_WALLET_ID
from momo.reconciliation import handler as recon_handler
from momo.reconciliation.report import run_reconciliation
from momo.saga import handlers as saga_handlers
from momo.saga import steps
from momo.saga.orchestrator import run_saga
from tests.integration.helpers import new_ctx

pytestmark = pytest.mark.integration
TODAY = date.today()


class FakePublisher:
    def __init__(self, fail=False):
        self.events, self.fail = [], fail

    def publish(self, event_type, payload, correlation_id):
        if self.fail:
            raise RuntimeError("sns down")
        self.events.append((event_type, payload, correlation_id))


def test_clean_system_reconciles_and_settlement_is_archived_in_db(make_wallet):
    agent, a, shop = make_wallet(5_000, "AGENT"), make_wallet(1_000), make_wallet(0, "MERCHANT")
    run_saga(new_ctx(a, shop, 400, type="MERCHANT_PAYMENT"))
    run_saga(new_ctx(agent, a, 100, type="CASH_IN"))
    rep = run_reconciliation(TODAY)
    assert rep["status"] == "OK" and rep["issues"] == {}
    types = {(r["type"], r["status"]): r for r in rep["settlement"]["by_type_and_status"]}
    assert types[("MERCHANT_PAYMENT", "COMPLETED")]["amount"] == 400
    assert rep["settlement"]["merchant_settlements"][0]["gross"] == 400
    assert any(x["wallet_id"] == agent for x in rep["settlement"]["agent_float"])
    run_reconciliation(TODAY)  # idempotent for the same date
    with transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM settlement_reports").fetchone()["n"] == 1


def test_reconciliation_detects_tampering_and_drift(make_wallet, db):
    a, b = make_wallet(1_000), make_wallet(0)
    run_saga(new_ctx(a, b, 100))
    with psycopg.connect(db, autocommit=True) as admin:           # attacker with DB superuser rights
        admin.execute("ALTER TABLE ledger_entries DISABLE TRIGGER USER")
        admin.execute("UPDATE ledger_entries SET amount = amount + 5 WHERE wallet_id=%s AND wallet_seq=1", (a,))
        admin.execute("ALTER TABLE ledger_entries ENABLE TRIGGER USER")
        admin.execute("ALTER TABLE wallets DISABLE TRIGGER ALL")
        admin.execute("UPDATE wallets SET balance = balance + 7 WHERE id=%s", (b,))
    rep = run_reconciliation(TODAY)
    assert rep["status"] == "DISCREPANCY"
    assert {"conservation_sum_of_balances", "broken_hash_chains", "wallet_ledger_drift"} <= set(rep["issues"])


def test_reconciliation_flags_stuck_pending_and_escrow_mismatch(make_wallet):
    a, b = make_wallet(1_000), make_wallet(0)
    ctx = new_ctx(a, b, 300)
    steps.reserve(ctx)                                  # saga "died" after the hold
    assert run_reconciliation(TODAY)["status"] == "OK"  # in-flight: escrow == held, not yet stuck
    with transaction() as conn:
        conn.execute("UPDATE transactions SET created_at = now() - interval '10 minutes'")
    rep = run_reconciliation(TODAY)
    assert rep["issues"]["stuck_sagas"]["pending_over_5min"] == 1
    with transaction() as conn:
        conn.execute("UPDATE wallets SET balance = balance + 1 WHERE id=%s", (ESCROW_WALLET_ID,))
        conn.execute("UPDATE wallets SET balance = balance - 1 WHERE id=%s", (a,))
    assert "escrow_mismatch" in run_reconciliation(TODAY)["issues"]


def test_outbox_publishes_once_and_retries_failures(make_wallet):
    a, b = make_wallet(1_000), make_wallet(0)
    run_saga(new_ctx(a, b, 100, correlation_id="corr-outbox-1"))
    with pytest.raises(RuntimeError):
        outbox.publish_pending(publisher=FakePublisher(fail=True))
    with transaction() as conn:   # failure rolled back: still unpublished
        assert conn.execute("SELECT count(*) AS n FROM outbox_events WHERE published_at IS NULL").fetchone()["n"] == 1
    pub = FakePublisher()
    assert outbox.publish_pending(publisher=pub) == 1
    assert outbox.publish_pending(publisher=pub) == 0
    assert pub.events[0][0] == "TransactionCompleted" and pub.events[0][2] == "corr-outbox-1"
    assert outbox.publish_pending() == 0   # no EVENT_TOPIC_ARN configured => no-op


def test_outbox_partial_delivery_retries_and_consumer_deduplicates(make_wallet, monkeypatch):
    class FailAfterFirstDelivery:
        def __init__(self):
            self.events = []
            self.calls = 0

        def publish(self, event_type, payload, correlation_id):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("sns unavailable after first delivery")
            self.events.append((event_type, payload, correlation_id))

    payer, payee = make_wallet(5_000), make_wallet(0)
    contexts = [new_ctx(payer, payee, 800) for _ in range(2)]
    for ctx in contexts:
        run_saga(ctx)

    publisher = FailAfterFirstDelivery()
    with pytest.raises(RuntimeError, match="sns unavailable"):
        outbox.publish_pending(publisher=publisher)
    with transaction() as conn:
        assert conn.execute(
            "SELECT count(*) AS n FROM outbox_events WHERE published_at IS NULL"
        ).fetchone()["n"] == 2

    assert outbox.publish_pending(publisher=publisher) == 2
    delivered_ids = [event[1]["txn_id"] for event in publisher.events]
    assert delivered_ids[0] == delivered_ids[1]
    assert delivered_ids[2] != delivered_ids[0]

    from momo.fraud.config import FraudConfig

    monkeypatch.setattr("momo.fraud.service.load_config", lambda: FraudConfig(daily_volume_limit=500))
    records = [
        {
            "messageId": f"m{index}",
            "body": json.dumps(payload),
            "messageAttributes": {
                "correlation_id": {"stringValue": correlation_id or ""},
            },
        }
        for index, (_, payload, correlation_id) in enumerate(publisher.events)
    ]
    assert fraud_handler.handler({"Records": records}) == {"batchItemFailures": []}
    with transaction() as conn:
        alerts = conn.execute(
            "SELECT txn_id FROM fraud_alerts WHERE decision = 'REVIEW' ORDER BY txn_id"
        ).fetchall()
    assert sorted(str(row["txn_id"]) for row in alerts) == sorted(ctx.txn_id for ctx in contexts)


def test_async_fraud_review_is_idempotent_and_raises_alert(make_wallet, monkeypatch):
    from momo.fraud.config import FraudConfig
    monkeypatch.setattr("momo.fraud.service.load_config", lambda: FraudConfig(daily_volume_limit=500))
    a, b = make_wallet(10_000), make_wallet(0)
    ctx = new_ctx(a, b, 800)
    run_saga(ctx)
    event = {"txn_id": ctx.txn_id, "type": "TRANSFER", "from_wallet": a, "to_wallet": b, "amount": 800, "correlation_id": "c-1234567"}
    sqs = {"Records": [{"messageId": "m1", "body": json.dumps(event),
                        "messageAttributes": {"correlation_id": {"stringValue": "c-1234567"}}}]}
    for _ in range(3):                                  # SQS redelivery
        assert fraud_handler.handler(sqs) == {"batchItemFailures": []}
    with transaction() as conn:
        rows = conn.execute("SELECT decision FROM fraud_alerts WHERE txn_id=%s", (ctx.txn_id,)).fetchall()
    assert [r["decision"] for r in rows] == ["REVIEW"]
    bad = {"Records": [{"messageId": "m2", "body": "{not json"}]}
    assert fraud_handler.handler(bad) == {"batchItemFailures": [{"itemIdentifier": "m2"}]}   # -> retry -> DLQ


def test_fraud_lambda_direct_invocation_and_geo_rule(make_wallet):
    a, b = make_wallet(10_000), make_wallet(0)
    first = new_ctx(a, b, 100, geo_lat=-15.3875, geo_lon=28.3228)
    run_saga(first)
    second = new_ctx(a, b, 100, geo_lat=-12.9587, geo_lon=28.6366)   # Ndola seconds after Lusaka
    steps.reserve(second)
    out = fraud_handler.handler(second.to_dict())
    assert out["decision"] == "REVIEW" and out["rules"][0]["rule"] == "geo_anomaly"


def test_lambda_handlers_drive_a_full_saga(make_wallet):
    a, b = make_wallet(1_000), make_wallet(0)
    ctx = new_ctx(a, b, 250, correlation_id="corr-lambda-1")
    call = lambda action, **kw: saga_handlers.handler({"action": action, "ctx": ctx.to_dict(), **kw})  # noqa: E731
    assert call("reserve")["ok"] and call("credit")["ok"] and call("finalize")["ok"]
    assert call("reverse_credit")["ok"] and call("release")["ok"] is True
    assert call("mark_rejected", code="X")["ok"] and call("mark_failed", error="e")["ok"]
    assert call("mark_compensation_failed", error="e")["ok"]
    with pytest.raises(ValueError):
        call("bogus")


def test_reconciliation_lambda_tasks(make_wallet):
    assert recon_handler.handler({"task": "outbox_sweep"}) == {"published": 0}
    out = recon_handler.handler({"task": "reconcile", "date": TODAY.isoformat()})
    assert out == {"status": "OK", "issues": []}
