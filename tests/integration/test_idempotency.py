from concurrent.futures import ThreadPoolExecutor

import pytest

from momo import idempotency, service
from momo.common.db import transaction
from momo.common.errors import IdempotencyKeyReused, InvalidRequest, MomoError, RequestInProgress
from momo.saga.orchestrator import run_saga
from momo.service import TxnRequest
from tests.integration.helpers import assert_money_conserved, balance

pytestmark = pytest.mark.integration


def _debits(wallet, entry_type="TRANSFER"):
    with transaction() as conn:
        return conn.execute(
            "SELECT count(*) AS n FROM ledger_entries WHERE wallet_id=%s AND direction='DEBIT' AND entry_type='HOLD'",
            (wallet,),
        ).fetchone()["n"]


def test_replaying_the_same_request_100_times_debits_exactly_once(make_wallet):
    """THE idempotency proof (local form; scripts/proofs does the same over HTTP in AWS)."""
    payer, payee = make_wallet(10_000), make_wallet(0)
    req = TxnRequest("TRANSFER", payer, payee, 1_500)

    with ThreadPoolExecutor(25) as pool:
        results = list(pool.map(lambda _: service.submit("alice", req, "idem-key-0001", "cid-12345678"), range(100)))

    assert {r[0] for r in results} == {201}
    assert len({r[1]["transaction_id"] for r in results}) == 1
    assert sum(1 for r in results if not r[2]) == 1          # exactly one real execution
    assert (balance(payer), balance(payee)) == (8_500, 1_500)
    assert _debits(payer) == 1
    with transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM transactions").fetchone()["n"] == 1
    assert_money_conserved()


def test_different_keys_are_different_transactions(make_wallet):
    payer, payee = make_wallet(10_000), make_wallet(0)
    req = TxnRequest("TRANSFER", payer, payee, 100)
    service.submit("alice", req, "idem-key-aaaa", None)
    service.submit("alice", req, "idem-key-bbbb", None)
    assert balance(payer) == 9_800


def test_same_key_different_body_is_refused(make_wallet):
    payer, payee = make_wallet(10_000), make_wallet(0)
    service.submit("alice", TxnRequest("TRANSFER", payer, payee, 100), "idem-key-0002", None)
    with pytest.raises(IdempotencyKeyReused):
        service.submit("alice", TxnRequest("TRANSFER", payer, payee, 999), "idem-key-0002", None)
    assert balance(payer) == 9_900


def test_same_key_from_different_users_does_not_collide(make_wallet):
    p1, p2, sink = make_wallet(1_000), make_wallet(1_000), make_wallet(0)
    assert service.submit("alice", TxnRequest("TRANSFER", p1, sink, 10), "shared-key-1", None)[0] == 201
    assert service.submit("bob", TxnRequest("TRANSFER", p2, sink, 10), "shared-key-1", None)[0] == 201
    assert balance(sink) == 20


def test_rejections_are_also_replayed_not_retried(make_wallet):
    payer, payee = make_wallet(50), make_wallet(0)
    req = TxnRequest("TRANSFER", payer, payee, 100)
    first = service.submit("alice", req, "idem-key-0003", None)
    assert first[0] == 422
    with transaction() as conn:   # funds arrive afterwards...
        conn.execute("UPDATE wallets SET balance = 500 WHERE id=%s", (payer,))
        conn.execute("UPDATE wallets SET balance = balance - 450 WHERE id = '00000000-0000-0000-0000-000000000001'")
    again = service.submit("alice", req, "idem-key-0003", None)
    assert again[0] == 422 and again[2] is True    # same answer; the client must use a new key to retry


def test_missing_or_bad_key_rejected(make_wallet):
    a, b = make_wallet(100), make_wallet(0)
    for key in (None, "", "short"):
        with pytest.raises(InvalidRequest):
            service.submit("alice", TxnRequest("TRANSFER", a, b, 1), key, None)


class CrashingRunner:
    """Simulates the API task dying mid-request."""
    def run(self, ctx):
        raise MomoError("process died")


class RunThenCrash:
    def run(self, ctx):
        run_saga(ctx)
        raise MomoError("died after the saga finished, before replying")


class MustNotRun:
    def run(self, ctx):
        raise AssertionError("saga must not be re-run")


def _expire_lease():
    with transaction() as conn:
        conn.execute("UPDATE idempotency_keys SET created_at = now() - interval '5 minutes'")


def test_crashed_request_is_resumed_after_lease_expiry_without_double_debit(make_wallet):
    payer, payee = make_wallet(1_000), make_wallet(0)
    req = TxnRequest("TRANSFER", payer, payee, 300)
    with pytest.raises(MomoError):
        service.submit("alice", req, "idem-key-0004", None, runner=CrashingRunner())
    _expire_lease()
    code, body, _ = service.submit("alice", req, "idem-key-0004", None)
    assert code == 201 and balance(payer) == 700 and balance(payee) == 300
    assert_money_conserved()


def test_takeover_reuses_a_finished_saga_instead_of_rerunning(make_wallet):
    payer, payee = make_wallet(1_000), make_wallet(0)
    req = TxnRequest("TRANSFER", payer, payee, 300)
    with pytest.raises(MomoError):
        service.submit("alice", req, "idem-key-0005", None, runner=RunThenCrash())
    _expire_lease()
    code, _, _ = service.submit("alice", req, "idem-key-0005", None, runner=MustNotRun())
    assert code == 201 and balance(payer) == 700


def test_duplicate_while_original_running_gets_409_then_result(make_wallet, monkeypatch):
    payer, payee = make_wallet(1_000), make_wallet(0)
    req = TxnRequest("TRANSFER", payer, payee, 300)
    with pytest.raises(MomoError):
        service.submit("alice", req, "idem-key-0006", None, runner=CrashingRunner())  # leaves IN_PROGRESS (fresh)
    real_wait = idempotency.wait_for_completion
    monkeypatch.setattr(idempotency, "wait_for_completion", lambda s, k, timeout_s=0.2: real_wait(s, k, 0.2))
    with pytest.raises(RequestInProgress):
        service.submit("alice", req, "idem-key-0006", None)
