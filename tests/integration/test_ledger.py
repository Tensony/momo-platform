import threading
import uuid

import psycopg
import pytest

from momo.common.db import transaction
from momo.common.errors import CurrencyMismatch, InsufficientFunds, WalletNotActive, WalletNotFound
from momo.ledger.core import ESCROW_WALLET_ID, MINT_WALLET_ID, get_wallet, move, verify_wallet_chain

pytestmark = pytest.mark.integration


def _move(frm, to, amount, step="t", txn=None, **kw):
    with transaction() as conn:
        return move(conn, txn_id=txn or str(uuid.uuid4()), step=step, from_wallet=frm, to_wallet=to,
                    amount=amount, entry_type="TEST", **kw)


def test_move_updates_balances_and_conserves_money(make_wallet):
    a, b = make_wallet(1000), make_wallet(0)
    assert _move(a, b, 400) is True
    with transaction() as conn:
        assert get_wallet(conn, a)["balance"] == 600
        assert get_wallet(conn, b)["balance"] == 400
        total = conn.execute("SELECT sum(balance) AS s FROM wallets").fetchone()["s"]
        assert total == 0  # MINT is -1000, everyone else sums to +1000


def test_insufficient_funds_rejected_and_nothing_changes(make_wallet):
    a, b = make_wallet(100), make_wallet(0)
    with pytest.raises(InsufficientFunds):
        _move(a, b, 101)
    with transaction() as conn:
        assert get_wallet(conn, a)["balance"] == 100


def test_same_step_replay_is_a_noop(make_wallet):
    a, b = make_wallet(1000), make_wallet(0)
    txn = str(uuid.uuid4())
    assert _move(a, b, 100, step="credit", txn=txn) is True
    assert _move(a, b, 100, step="credit", txn=txn) is False
    with transaction() as conn:
        assert get_wallet(conn, a)["balance"] == 900


def test_unknown_and_malformed_wallets(make_wallet):
    a = make_wallet(100)
    with pytest.raises(WalletNotFound):
        _move(a, str(uuid.uuid4()), 10)
    with pytest.raises(WalletNotFound):
        _move(a, "not-a-uuid", 10)


def test_frozen_wallet_blocks_normal_moves_but_not_compensation(make_wallet):
    a, b = make_wallet(500), make_wallet(0)
    with transaction() as conn:
        conn.execute("UPDATE wallets SET status='FROZEN' WHERE id=%s", (b,))
    with pytest.raises(WalletNotActive):
        _move(a, b, 10)
    assert _move(a, b, 10, check_status=False) is True


def test_currency_mismatch(make_wallet):
    a, b = make_wallet(500), make_wallet(0)
    with transaction() as conn:
        conn.execute("UPDATE wallets SET currency='USD' WHERE id=%s", (b,))
    with pytest.raises(CurrencyMismatch):
        _move(a, b, 10)


def test_ledger_is_append_only(make_wallet):
    a, b = make_wallet(1000), make_wallet(0)
    _move(a, b, 100)
    with pytest.raises(psycopg.errors.IntegrityError):
        with transaction() as conn:
            conn.execute("UPDATE ledger_entries SET amount = 1")
    with pytest.raises(psycopg.errors.IntegrityError):
        with transaction() as conn:
            conn.execute("DELETE FROM ledger_entries")
    with pytest.raises(psycopg.errors.IntegrityError):
        with transaction() as conn:
            conn.execute("TRUNCATE ledger_entries")


def test_db_check_constraint_is_a_backstop_against_overdraft(make_wallet):
    a = make_wallet(100)
    with pytest.raises(psycopg.errors.CheckViolation):
        with transaction() as conn:
            conn.execute("UPDATE wallets SET balance = -1 WHERE id = %s", (a,))


def test_hash_chain_verifies_and_detects_tampering(make_wallet, db):
    a, b = make_wallet(1000), make_wallet(0)
    for _ in range(5):
        _move(a, b, 10)
    with transaction() as conn:
        assert verify_wallet_chain(conn, a).ok
        assert verify_wallet_chain(conn, a).entries == 6  # seed + 5 debits
    # Tamper as superuser with triggers disabled (an attacker with DB admin rights)
    with psycopg.connect(db, autocommit=True) as admin:
        admin.execute("ALTER TABLE ledger_entries DISABLE TRIGGER USER")
        admin.execute("UPDATE ledger_entries SET amount = amount + 1 WHERE wallet_id = %s AND wallet_seq = 3", (a,))
        admin.execute("ALTER TABLE ledger_entries ENABLE TRIGGER USER")
    with transaction() as conn:
        res = verify_wallet_chain(conn, a)
    assert not res.ok and any("tampered" in e for e in res.errors)


def test_concurrent_debits_never_overdraw(make_wallet):
    """50 concurrent transfers of 30 from a wallet holding 1000: exactly 33 can succeed."""
    src, dst = make_wallet(1000), make_wallet(0)
    results, lock = [], threading.Lock()

    def worker():
        try:
            _move(src, dst, 30)
            outcome = "ok"
        except InsufficientFunds:
            outcome = "insufficient"
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(50)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count("ok") == 33 and results.count("insufficient") == 17
    with transaction() as conn:
        assert get_wallet(conn, src)["balance"] == 10
        assert get_wallet(conn, dst)["balance"] == 990


def test_opposite_direction_transfers_do_not_deadlock(make_wallet):
    a, b = make_wallet(10_000), make_wallet(10_000)
    errors = []

    def worker(i):
        try:
            for _ in range(20):
                _move(a, b, 1) if i % 2 else _move(b, a, 1)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors


def test_system_wallets_exist(db):
    with transaction() as conn:
        assert get_wallet(conn, MINT_WALLET_ID)["kind"] == "SYSTEM"
        assert get_wallet(conn, ESCROW_WALLET_ID)["balance"] == 0
