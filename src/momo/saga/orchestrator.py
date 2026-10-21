"""In-process saga orchestrator.

This is the reference implementation of the saga flow, used by local runs, the unit tests and the
"local" backend of the correctness proofs. In AWS the identical flow is executed by the Step
Functions state machine in statemachine/transfer_saga.asl.json (which calls the same step code
through Lambda). The two are kept in lock-step; tests/unit/test_asl.py checks the state machine
still routes every failure to the compensation states.

      +--> REJECTED (reserve refused: insufficient funds, frozen wallet ...)   nothing moved
      |
  reserve --> fraud --DECLINE--> release --> REJECTED
      |         |
      |       APPROVE/REVIEW
      |         v
      |       credit --refused--> release --> REJECTED
      |         |
      |       finalize --> COMPLETED
      |
      any technical error --> reverse_credit --> release --> FAILED  (funds back where they began)
"""

from __future__ import annotations

import logging
import time
from typing import Protocol

from . import steps as default_steps
from .context import SagaResult, TxnContext

log = logging.getLogger(__name__)

COMPENSATION_ATTEMPTS = 5


class Steps(Protocol):  # structural type so tests can substitute fakes
    def reserve(self, ctx: TxnContext) -> dict: ...
    def fraud_check(self, ctx: TxnContext) -> dict: ...
    def credit(self, ctx: TxnContext) -> dict: ...
    def finalize(self, ctx: TxnContext) -> dict: ...
    def reverse_credit(self, ctx: TxnContext) -> dict: ...
    def release(self, ctx: TxnContext) -> dict: ...
    def mark_rejected(self, ctx: TxnContext, code: str) -> dict: ...
    def mark_failed(self, ctx: TxnContext, error: str) -> dict: ...
    def mark_compensation_failed(self, ctx: TxnContext, error: str) -> None: ...


def _compensate(steps: Steps, ctx: TxnContext, sleep=time.sleep) -> bool:
    """reverse_credit then release, each retried with backoff. Returns True if both succeeded."""
    for name in ("reverse_credit", "release"):
        fn = getattr(steps, name)
        for attempt in range(1, COMPENSATION_ATTEMPTS + 1):
            try:
                fn(ctx)
                break
            except Exception:
                log.exception(
                    "compensation step failed",
                    extra={"txn_id": ctx.txn_id, "step": name, "attempt": attempt},
                )
                if attempt == COMPENSATION_ATTEMPTS:
                    return False
                sleep(min(0.05 * 2**attempt, 1.0))
    return True


def run_saga(ctx: TxnContext, steps: Steps = default_steps, sleep=time.sleep) -> SagaResult:
    log.info("saga started", extra={"txn_id": ctx.txn_id, "type": ctx.type, "amount": ctx.amount})
    try:
        r = steps.reserve(ctx)
        if not r["ok"]:
            steps.mark_rejected(ctx, r["code"])
            return SagaResult(ctx.txn_id, "REJECTED", r["code"], r.get("detail", {}))

        verdict = steps.fraud_check(ctx)
        if verdict["decision"] == "DECLINE":
            steps.release(ctx)
            steps.mark_rejected(ctx, "FRAUD_DECLINED")
            return SagaResult(ctx.txn_id, "REJECTED", "FRAUD_DECLINED", {"rules": verdict["rules"]})

        c = steps.credit(ctx)
        if not c["ok"]:
            steps.release(ctx)
            steps.mark_rejected(ctx, c["code"])
            return SagaResult(ctx.txn_id, "REJECTED", c["code"], c.get("detail", {}))

        steps.finalize(ctx)
        detail = {"fraud_decision": verdict["decision"]}
        return SagaResult(ctx.txn_id, "COMPLETED", None, detail)

    except Exception as exc:
        log.exception("saga step failed - compensating", extra={"txn_id": ctx.txn_id})
        if _compensate(steps, ctx, sleep):
            steps.mark_failed(ctx, f"{type(exc).__name__}: {exc}")
            return SagaResult(ctx.txn_id, "FAILED", "SAGA_FAILED_COMPENSATED", {"error": str(exc)})
        steps.mark_compensation_failed(ctx, f"{type(exc).__name__}: {exc}")
        return SagaResult(ctx.txn_id, "FAILED", "COMPENSATION_FAILED", {"error": str(exc)})
