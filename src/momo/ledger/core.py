"""Double-entry ledger primitives.

`move()` is the ONLY function that changes a balance. Its guarantees:

* Concurrency safety - both wallet rows are locked with SELECT ... FOR UPDATE, always in sorted id
  order (no deadlocks between opposite transfers). The balance check happens AFTER the lock is held,
  so 50 concurrent debits of one wallet are serialised and can never overdraw it. The CHECK
  constraint on wallets.balance is a database-level backstop should this code ever be wrong.
* Idempotency per saga step - the (txn_id, step) pair is checked after the locks are held and is
  protected by a UNIQUE (txn_id, step, direction) constraint, so replaying a step is a no-op.
* Conservation - every move writes exactly one DEBIT and one CREDIT of the same amount, in the same
  database transaction as the balance updates. Money cannot be created or destroyed by one move.
* Tamper evidence - each wallet has a hash chain over its entries (see common.hashing).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import psycopg

from ..common.errors import (
    CurrencyMismatch,
    InsufficientFunds,
    InvalidRequest,
    WalletNotActive,
    WalletNotFound,
)
from ..common.hashing import GENESIS_HASH, compute_entry_hash
from ..common.tracing import traced

MINT_WALLET_ID = "00000000-0000-0000-0000-000000000001"
ESCROW_WALLET_ID = "00000000-0000-0000-0000-000000000002"

_WALLET_COLS = (
    "id, owner_id, kind, currency, balance, allow_negative, status, entry_seq, last_entry_hash"
)


def _serialise_wallet(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "owner_id": row["owner_id"],
        "kind": row["kind"],
        "currency": row["currency"],
        "balance": row["balance"],
        "status": row["status"],
    }


def create_wallet(conn: psycopg.Connection, owner_id: str, kind: str, currency: str = "ZMW") -> dict:
    if kind not in {"CUSTOMER", "MERCHANT", "AGENT"}:
        raise InvalidRequest("kind must be CUSTOMER, MERCHANT or AGENT")
    row = conn.execute(
        f"INSERT INTO wallets (owner_id, kind, currency) VALUES (%s, %s, %s) RETURNING {_WALLET_COLS}",
        (owner_id, kind, currency),
    ).fetchone()
    return _serialise_wallet(row)


def get_wallet(conn: psycopg.Connection, wallet_id: str) -> dict:
    try:
        row = conn.execute(
            f"SELECT {_WALLET_COLS} FROM wallets WHERE id = %s", (str(wallet_id),)
        ).fetchone()
    except psycopg.errors.InvalidTextRepresentation:
        raise WalletNotFound("wallet not found", wallet_id=wallet_id) from None
    if row is None:
        raise WalletNotFound("wallet not found", wallet_id=str(wallet_id))
    return _serialise_wallet(row)


def step_applied(conn: psycopg.Connection, txn_id: str, step: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM ledger_entries WHERE txn_id = %s AND step = %s LIMIT 1",
            (str(txn_id), step),
        ).fetchone()
        is not None
    )


@traced("ledger.move")
def move(
    conn: psycopg.Connection,
    *,
    txn_id: str,
    step: str,
    from_wallet: str,
    to_wallet: str,
    amount: int,
    entry_type: str,
    correlation_id: str | None = None,
    check_status: bool = True,
) -> bool:
    """Move `amount` from one wallet to another. Returns True if applied, False if it was a replay.

    Must be called inside a transaction (the caller commits). Raises BusinessRejection subclasses
    for rule violations; the caller's transaction then rolls back with nothing changed.
    """
    if amount <= 0:
        raise InvalidRequest("amount must be positive")
    from_wallet, to_wallet, txn_id = str(from_wallet), str(to_wallet), str(txn_id)
    if from_wallet == to_wallet:
        raise InvalidRequest("source and destination wallets must differ")

    # 1. Lock both rows in a global order => no deadlocks, and serialises concurrent writers.
    locked: dict[str, dict] = {}
    for wid in sorted((from_wallet, to_wallet)):
        try:
            row = conn.execute(
                f"SELECT {_WALLET_COLS} FROM wallets WHERE id = %s FOR UPDATE", (wid,)
            ).fetchone()
        except psycopg.errors.InvalidTextRepresentation:
            raise WalletNotFound("wallet not found", wallet_id=wid) from None
        if row is None:
            raise WalletNotFound("wallet not found", wallet_id=wid)
        locked[wid] = row
    src, dst = locked[from_wallet], locked[to_wallet]

    # 2. Replay protection, evaluated while holding the locks.
    if step_applied(conn, txn_id, step):
        return False

    # 3. Business rules.
    if check_status:
        for w in (src, dst):
            if w["status"] != "ACTIVE":
                raise WalletNotActive("wallet is not active", wallet_id=str(w["id"]))
    if src["currency"] != dst["currency"]:
        raise CurrencyMismatch("wallets use different currencies")
    if not src["allow_negative"] and src["balance"] < amount:
        raise InsufficientFunds(
            "insufficient funds", wallet_id=from_wallet, balance=src["balance"], requested=amount
        )

    # 4. Apply: balances, hash chains, ledger entries - one atomic unit with the caller's commit.
    for wallet, direction, delta in ((src, "DEBIT", -amount), (dst, "CREDIT", amount)):
        new_balance = wallet["balance"] + delta
        seq = wallet["entry_seq"] + 1
        entry_hash = compute_entry_hash(
            prev_hash=wallet["last_entry_hash"],
            wallet_id=str(wallet["id"]),
            wallet_seq=seq,
            txn_id=txn_id,
            step=step,
            direction=direction,
            amount=amount,
            balance_after=new_balance,
        )
        conn.execute(
            """INSERT INTO ledger_entries
                 (txn_id, step, wallet_id, wallet_seq, direction, amount, balance_after,
                  entry_type, prev_hash, entry_hash, correlation_id)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                txn_id, step, str(wallet["id"]), seq, direction, amount, new_balance,
                entry_type, wallet["last_entry_hash"], entry_hash, correlation_id,
            ),
        )
        conn.execute(
            "UPDATE wallets SET balance = %s, entry_seq = %s, last_entry_hash = %s WHERE id = %s",
            (new_balance, seq, entry_hash, str(wallet["id"])),
        )
    return True


def get_ledger(conn: psycopg.Connection, wallet_id: str, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        """SELECT wallet_seq, txn_id, step, direction, amount, balance_after, entry_type,
                  entry_hash, created_at
             FROM ledger_entries WHERE wallet_id = %s ORDER BY wallet_seq DESC LIMIT %s""",
        (str(wallet_id), max(1, min(limit, 500))),
    ).fetchall()
    return [
        {
            "seq": r["wallet_seq"],
            "txn_id": str(r["txn_id"]),
            "step": r["step"],
            "direction": r["direction"],
            "amount": r["amount"],
            "balance_after": r["balance_after"],
            "entry_type": r["entry_type"],
            "hash": r["entry_hash"],
            "created_at": r["created_at"].isoformat(),
        }
        for r in rows
    ]


@dataclass
class ChainResult:
    wallet_id: str
    entries: int = 0
    ok: bool = True
    errors: list[str] = field(default_factory=list)


def verify_wallet_chain(conn: psycopg.Connection, wallet_id: str) -> ChainResult:
    """Recompute a wallet's hash chain and running balance from its ledger entries."""
    wallet_id = str(wallet_id)
    result = ChainResult(wallet_id=wallet_id)
    wallet = conn.execute(
        "SELECT balance, entry_seq, last_entry_hash, allow_negative FROM wallets WHERE id = %s",
        (wallet_id,),
    ).fetchone()
    if wallet is None:
        result.ok = False
        result.errors.append("wallet missing")
        return result

    prev_hash, running, expected_seq = GENESIS_HASH, 0, 1
    cur = conn.execute(
        """SELECT wallet_seq, txn_id, step, direction, amount, balance_after, prev_hash, entry_hash
             FROM ledger_entries WHERE wallet_id = %s ORDER BY wallet_seq""",
        (wallet_id,),
    )
    for e in cur:
        result.entries += 1
        if e["wallet_seq"] != expected_seq:
            result.errors.append(f"gap/reorder at seq {e['wallet_seq']} (expected {expected_seq})")
        running += e["amount"] if e["direction"] == "CREDIT" else -e["amount"]
        if e["balance_after"] != running:
            result.errors.append(f"seq {e['wallet_seq']}: balance_after does not match running total")
        if e["prev_hash"] != prev_hash:
            result.errors.append(f"seq {e['wallet_seq']}: prev_hash does not match chain")
        recomputed = compute_entry_hash(
            prev_hash=e["prev_hash"],
            wallet_id=wallet_id,
            wallet_seq=e["wallet_seq"],
            txn_id=str(e["txn_id"]),
            step=e["step"],
            direction=e["direction"],
            amount=e["amount"],
            balance_after=e["balance_after"],
        )
        if recomputed != e["entry_hash"]:
            result.errors.append(f"seq {e['wallet_seq']}: entry hash mismatch (tampered?)")
        prev_hash, expected_seq = e["entry_hash"], expected_seq + 1

    if wallet["balance"] != running:
        result.errors.append("wallet.balance does not equal the sum of its ledger entries")
    if wallet["last_entry_hash"] != prev_hash:
        result.errors.append("wallet.last_entry_hash does not match the chain head")
    if wallet["entry_seq"] != expected_seq - 1:
        result.errors.append("wallet.entry_seq does not match the number of entries")
    result.ok = not result.errors
    return result
