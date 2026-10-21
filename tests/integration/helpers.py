import uuid

from momo.common.db import transaction
from momo.ledger.core import ESCROW_WALLET_ID
from momo.saga.context import TxnContext


def new_ctx(frm, to, amount, **kw):
    kw.setdefault("type", "TRANSFER")
    return TxnContext(txn_id=str(uuid.uuid4()), from_wallet=frm, to_wallet=to, amount=amount, **kw)


def balance(wallet_id):
    with transaction() as conn:
        return conn.execute("SELECT balance FROM wallets WHERE id = %s", (wallet_id,)).fetchone()["balance"]


def assert_money_conserved():
    """The invariants that must hold after ANY sequence of operations, including failures."""
    with transaction() as conn:
        assert conn.execute("SELECT sum(balance) AS s FROM wallets").fetchone()["s"] == 0, "money created/destroyed"
        net = conn.execute(
            "SELECT COALESCE(sum(CASE direction WHEN 'CREDIT' THEN amount ELSE -amount END),0) AS s FROM ledger_entries"
        ).fetchone()["s"]
        assert net == 0, "ledger does not balance"
        bad = conn.execute(
            """SELECT txn_id, step FROM ledger_entries GROUP BY txn_id, step
               HAVING sum(CASE direction WHEN 'CREDIT' THEN amount ELSE -amount END) <> 0"""
        ).fetchall()
        assert not bad, f"unbalanced steps: {bad}"
        assert conn.execute("SELECT count(*) AS n FROM wallets WHERE balance < 0 AND NOT allow_negative").fetchone()["n"] == 0


def escrow_balance():
    return balance(ESCROW_WALLET_ID)
