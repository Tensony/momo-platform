"""Saga flow tested with fake steps: every failure point must route to the right compensation."""
import pytest

from momo.saga.context import TxnContext
from momo.saga.orchestrator import COMPENSATION_ATTEMPTS, run_saga


class FakeSteps:
    def __init__(self, reserve=None, fraud="APPROVE", credit=None, fail_at=None, fail_comp=None):
        self.calls, self.fail_at, self.fail_comp = [], fail_at, fail_comp
        self._reserve = reserve or {"ok": True}
        self._credit = credit or {"ok": True}
        self._fraud = fraud

    def _maybe_fail(self, name):
        if self.fail_at == name:
            raise RuntimeError(f"boom at {name}")

    def reserve(self, ctx):
        self.calls.append("reserve"); self._maybe_fail("reserve"); return self._reserve

    def fraud_check(self, ctx):
        self.calls.append("fraud"); self._maybe_fail("fraud")
        return {"decision": self._fraud, "rules": [{"rule": "velocity"}]}

    def credit(self, ctx):
        self.calls.append("credit"); self._maybe_fail("credit"); return self._credit

    def finalize(self, ctx):
        self.calls.append("finalize"); self._maybe_fail("finalize"); return {"ok": True}

    def reverse_credit(self, ctx):
        self.calls.append("reverse_credit")
        if self.fail_comp == "reverse_credit":
            raise RuntimeError("comp boom")
        return {"ok": True}

    def release(self, ctx):
        self.calls.append("release")
        if self.fail_comp == "release":
            raise RuntimeError("comp boom")
        return {"ok": True}

    def mark_rejected(self, ctx, code): self.calls.append(f"rejected:{code}"); return {"ok": True}
    def mark_failed(self, ctx, error): self.calls.append("failed"); return {"ok": True}
    def mark_compensation_failed(self, ctx, error): self.calls.append("comp_failed")


CTX = TxnContext(txn_id="t", type="TRANSFER", from_wallet="a", to_wallet="b", amount=10)
nosleep = lambda s: None  # noqa: E731


def test_happy_path():
    s = FakeSteps()
    r = run_saga(CTX, s, nosleep)
    assert r.outcome == "COMPLETED" and s.calls == ["reserve", "fraud", "credit", "finalize"]


def test_review_decision_still_completes_and_is_flagged():
    r = run_saga(CTX, FakeSteps(fraud="REVIEW"), nosleep)
    assert r.outcome == "COMPLETED" and r.detail["fraud_decision"] == "REVIEW"


def test_reserve_rejection_moves_nothing():
    s = FakeSteps(reserve={"ok": False, "code": "INSUFFICIENT_FUNDS", "detail": {}})
    r = run_saga(CTX, s, nosleep)
    assert (r.outcome, r.code) == ("REJECTED", "INSUFFICIENT_FUNDS")
    assert s.calls == ["reserve", "rejected:INSUFFICIENT_FUNDS"]


def test_fraud_decline_releases_hold():
    s = FakeSteps(fraud="DECLINE")
    r = run_saga(CTX, s, nosleep)
    assert (r.outcome, r.code) == ("REJECTED", "FRAUD_DECLINED")
    assert s.calls == ["reserve", "fraud", "release", "rejected:FRAUD_DECLINED"]


def test_credit_refusal_releases_hold():
    s = FakeSteps(credit={"ok": False, "code": "WALLET_NOT_ACTIVE", "detail": {}})
    r = run_saga(CTX, s, nosleep)
    assert (r.outcome, r.code) == ("REJECTED", "WALLET_NOT_ACTIVE")
    assert s.calls == ["reserve", "fraud", "credit", "release", "rejected:WALLET_NOT_ACTIVE"]


@pytest.mark.parametrize("point", ["reserve", "fraud", "credit", "finalize"])
def test_technical_failure_anywhere_compensates_in_order(point):
    s = FakeSteps(fail_at=point)
    r = run_saga(CTX, s, nosleep)
    assert (r.outcome, r.code) == ("FAILED", "SAGA_FAILED_COMPENSATED")
    assert s.calls[-3:] == ["reverse_credit", "release", "failed"]


def test_compensation_is_retried_then_escalated():
    s = FakeSteps(fail_at="credit", fail_comp="release")
    r = run_saga(CTX, s, nosleep)
    assert r.code == "COMPENSATION_FAILED"
    assert s.calls.count("release") == COMPENSATION_ATTEMPTS
    assert s.calls[-1] == "comp_failed"


def test_compensation_recovers_after_transient_failure():
    class Flaky(FakeSteps):
        n = 0
        def release(self, ctx):
            Flaky.n += 1
            if Flaky.n < 3:
                raise RuntimeError("transient")
            return super().release(ctx)
    r = run_saga(CTX, Flaky(fail_at="finalize"), nosleep)
    assert r.code == "SAGA_FAILED_COMPENSATED"
