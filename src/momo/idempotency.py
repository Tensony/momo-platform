"""Idempotency for write APIs (Idempotency-Key header).

Guarantee: however many times (and however concurrently) a client replays a request with the same
key, the business effect happens at most once and every replay sees the same final response.

Mechanics
---------
1. `begin()` inserts (scope, key) with status IN_PROGRESS in its own short transaction. The primary
   key makes concurrent duplicates serialise: exactly one inserter wins ("NEW").
2. The winner runs the saga, then `complete()` stores the final HTTP response.
3. Duplicates get the stored response (COMPLETED), or IN_PROGRESS while the winner is still running.
4. If the winner crashed, the IN_PROGRESS lease expires and ONE retry "takes over". Because the
   transaction id is derived deterministically from (scope, key) and every saga step is itself
   idempotent, resuming is safe and cannot double-post.
5. Same key + different body => IdempotencyKeyReused (422), a client bug we refuse to guess about.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from .common.db import transaction
from .common.errors import IdempotencyKeyReused, InvalidRequest, RequestInProgress

LEASE_SECONDS = 30
_NAMESPACE = uuid.UUID("6f1d3c52-7b0e-4a5e-9c55-3d0a9e7c2f10")


@dataclass
class BeginResult:
    state: str  # NEW | TAKEOVER | IN_PROGRESS | COMPLETED
    response_code: int | None = None
    response_body: dict | None = None


def validate_key(key: str | None) -> str:
    if not key or not (8 <= len(key) <= 128) or not key.isprintable():
        raise InvalidRequest("Idempotency-Key header is required (8-128 printable characters)")
    return key


def derive_txn_id(scope: str, key: str) -> str:
    """Deterministic transaction id => a resumed/retried request lands on the same transaction."""
    return str(uuid.uuid5(_NAMESPACE, f"{scope}:{key}"))


def begin(scope: str, key: str, request_hash: str) -> BeginResult:
    with transaction() as conn:
        inserted = conn.execute(
            """INSERT INTO idempotency_keys (scope, key, request_hash, status)
               VALUES (%s, %s, %s, 'IN_PROGRESS')
               ON CONFLICT (scope, key) DO NOTHING RETURNING 1""",
            (scope, key, request_hash),
        ).fetchone()
        if inserted:
            return BeginResult("NEW")

        row = conn.execute(
            """SELECT request_hash, status, response_code, response_body,
                      EXTRACT(EPOCH FROM (now() - created_at)) AS age_s
                 FROM idempotency_keys WHERE scope = %s AND key = %s FOR UPDATE""",
            (scope, key),
        ).fetchone()
        if row["request_hash"] != request_hash:
            raise IdempotencyKeyReused("this Idempotency-Key was used with a different request body")
        if row["status"] == "COMPLETED":
            return BeginResult("COMPLETED", row["response_code"], row["response_body"])
        if row["age_s"] > LEASE_SECONDS:  # winner presumed dead: renew the lease and take over
            conn.execute(
                "UPDATE idempotency_keys SET created_at = now() WHERE scope = %s AND key = %s",
                (scope, key),
            )
            return BeginResult("TAKEOVER")
        return BeginResult("IN_PROGRESS")


def complete(scope: str, key: str, response_code: int, response_body: dict) -> None:
    from psycopg.types.json import Jsonb

    with transaction() as conn:
        conn.execute(
            """UPDATE idempotency_keys
                  SET status = 'COMPLETED', response_code = %s, response_body = %s,
                      completed_at = now()
                WHERE scope = %s AND key = %s""",
            (response_code, Jsonb(response_body), scope, key),
        )


def wait_for_completion(scope: str, key: str, timeout_s: float = 5.0) -> BeginResult:
    """A duplicate arrived while the original is running: wait briefly for its result."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with transaction() as conn:
            row = conn.execute(
                "SELECT status, response_code, response_body FROM idempotency_keys "
                "WHERE scope = %s AND key = %s",
                (scope, key),
            ).fetchone()
        if row and row["status"] == "COMPLETED":
            return BeginResult("COMPLETED", row["response_code"], row["response_body"])
        time.sleep(0.05)
    raise RequestInProgress("the original request is still being processed; retry shortly")
