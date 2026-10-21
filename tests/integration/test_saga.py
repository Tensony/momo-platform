import pytest

from momo.common.db import transaction
from momo.fraud import config as fraud_config
from momo.fraud.config import FraudConfig
from momo.saga import faults, steps
from momo.saga.orchestrator import run_saga
from tests.integration.helpers import assert_money_conserved, balance, escrow_balance, new_ctx

pytestmark = pytest.mark.integration
nosleep = lambda s: None  # noqa: E731


def _status(txn_id):
    with transaction() as conn:
        t = conn.execute("SELECT status, reason FROM transactions WHERE id=%s", (txn_id,)).fetchone()
        s = conn.execute("SELECT state FROM sagas WHERE txn_id=%s", (txn_id,)).fetchone()
    return t["status"], t["reason"], s["state"] if s else None


def test_successful_transfer(make_wallet):
    a, b = make_wallet(1000), make_wallet(0)
    ctx = new_ctx(a, b, 300)
    r = run_saga(ctx)
    assert r.outcome == "COMPLETED"
    assert (balance(a), balance(b), escrow_balance()) == (700, 300, 0)
    assert _status(ctx.txn_id) == ("COMPLETED", None, "COMPLETED")
    with transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM outbox_events WHERE txn_id=%s", (ctx.txn_id,)).fetchone()["n"] == 1
    assert_money_conserved()


def test_insufficient_funds_is_rejected_cleanly(make_wallet):
    a, b = make_wallet(100), make_wallet(0)
    ctx = new_ctx(a, b, 101)
    r = run_saga(ctx)
    assert (r.outcome, r.code) == ("REJECTED", "INSUFFICIENT_FUNDS")
    assert (balance(a), balance(b)) == (100, 0)
    assert _status(ctx.txn_id)[:2] == ("REJECTED", "INSUFFICIENT_FUNDS")
    assert_money_conserved()


def test_unknown_wallet_rejected(make_wallet):
    a = make_wallet(100)
    r = run_saga(new_ctx(a, "00000000-0000-0000-0000-0000000000ff", 10))
    assert (r.outcome, r.code) == ("REJECTED", "WALLET_NOT_FOUND")
    assert balance(a) == 100
    assert_money_conserved()


def test_frozen_payee_releases_the_hold(make_wallet):
    a, b = make_wallet(500), make_wallet(0)
    with transaction() as conn:
        conn.execute("UPDATE wallets SET status='FROZEN' WHERE id=%s", (b,))
    r = run_saga(new_ctx(a, b, 200))
    assert (r.outcome, r.code) == ("REJECTED", "WALLET_NOT_ACTIVE")
    assert (balance(a), balance(b), escrow_balance()) == (500, 0, 0)
    assert_money_conserved()


def test_fraud_velocity_decline_compensates(make_wallet, monkeypatch):
    monkeypatch.setattr(fraud_config, "load_config", lambda: FraudConfig(velocity_max_txns=2))
    monkeypatch.setattr("momo.fraud.service.load_config", lambda: FraudConfig(velocity_max_txns=2))
    a, b = make_wallet(10_000), make_wallet(0)
    outcomes = [run_saga(new_ctx(a, b, 100)) for _ in range(4)]
    assert [o.outcome for o in outcomes] == ["COMPLETED", "COMPLETED", "REJECTED", "REJECTED"]
    assert outcomes[2].code == "FRAUD_DECLINED"
    assert (balance(a), balance(b), escrow_balance()) == (9_800, 200, 0)
    with transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM fraud_alerts WHERE decision='DECLINE'").fetchone()["n"] == 2
    assert_money_conserved()


@pytest.mark.parametrize("fault", faults.POINTS)
def test_failure_mid_transaction_creates_and_destroys_no_money(make_wallet, fault):
    """THE saga-compensation proof: inject a crash at every step; money must be conserved and the
    payer made whole."""
    a, b = make_wallet(1000), make_wallet(250)
    ctx = new_ctx(a, b, 400, fault=fault)
    r = run_saga(ctx, sleep=nosleep)
    assert (r.outcome, r.code) == ("FAILED", "SAGA_FAILED_COMPENSATED"), fault
    assert (balance(a), balance(b), escrow_balance()) == (1000, 250, 0), fault
    assert _status(ctx.txn_id) == ("FAILED", _status(ctx.txn_id)[1], "FAILED")
    assert_money_conserved()


def test_compensation_failure_is_escalated_and_funds_stay_safe(make_wallet, monkeypatch):
    a, b = make_wallet(1000), make_wallet(0)

    def broken_release(ctx):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(steps, "release", broken_release)
    ctx = new_ctx(a, b, 400, fault="credit.before")
    r = run_saga(ctx, sleep=nosleep)
    assert r.code == "COMPENSATION_FAILED"
    assert _status(ctx.txn_id)[2] == "COMPENSATION_FAILED"
    assert (balance(a), balance(b), escrow_balance()) == (600, 0, 400)  # held in escrow, not lost
    assert_money_conserved()
    # An operator (or the retry) can finish the job later, safely:
    monkeypatch.undo()
    steps.release(ctx)
    assert (balance(a), escrow_balance()) == (1000, 0)
    assert_money_conserved()


def test_resuming_a_half_finished_saga_never_double_posts(make_wallet):
    a, b = make_wallet(1000), make_wallet(0)
    ctx = new_ctx(a, b, 300)
    steps.reserve(ctx)
    steps.credit(ctx)           # process "crashed" here, before finalize
    r = run_saga(ctx)           # a retry resumes the same transaction id
    assert r.outcome == "COMPLETED"
    assert (balance(a), balance(b), escrow_balance()) == (700, 300, 0)
    assert_money_conserved()


def test_release_refuses_to_run_before_credit_is_reversed(make_wallet):
    from momo.common.errors import CompensationOrderError

    a, b = make_wallet(1000), make_wallet(0)
    ctx = new_ctx(a, b, 300)
    steps.reserve(ctx)
    steps.credit(ctx)
    with pytest.raises(CompensationOrderError):
        steps.release(ctx)
    assert_money_conserved()
