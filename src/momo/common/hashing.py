"""Hashing helpers: ledger hash chain and canonical request hashing for idempotency."""

from __future__ import annotations

import hashlib
import json

GENESIS_HASH = "0" * 64


def compute_entry_hash(
    *,
    prev_hash: str,
    wallet_id: str,
    wallet_seq: int,
    txn_id: str,
    step: str,
    direction: str,
    amount: int,
    balance_after: int,
) -> str:
    """Hash of one ledger entry, chained to the previous entry of the same wallet.

    Any edit to a past entry (or removal/reordering) changes every later hash in that wallet's
    chain, which the reconciliation job detects by recomputing the chain.
    """
    material = "|".join(
        [
            prev_hash,
            str(wallet_id),
            str(wallet_seq),
            str(txn_id),
            step,
            direction,
            str(amount),
            str(balance_after),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def hash_request(payload: dict) -> str:
    """Stable hash of a request body so the same Idempotency-Key with a different body is caught."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
