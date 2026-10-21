"""saga-steps Lambda: runs ONE saga step per invocation, called by the Step Functions state machine.

Event shape:  {"action": "reserve|credit|finalize|reverse_credit|release|mark_rejected|mark_failed",
               "ctx": {...TxnContext...}, "code": "...", "error": "..."}
Business outcomes are returned as {"ok": false, "code": ...}; technical failures raise so Step
Functions can retry / route to compensation.
"""

from __future__ import annotations

import logging

from ..common.logging_setup import configure_logging, set_correlation_id
from ..events.outbox import publish_pending
from . import steps
from .context import TxnContext

configure_logging("saga-steps")
log = logging.getLogger("saga-steps")


def handler(event, context=None):
    ctx = TxnContext.from_dict(event["ctx"])
    set_correlation_id(ctx.correlation_id)
    action = event["action"]
    log.info("saga step", extra={"action": action, "txn_id": ctx.txn_id})

    if action == "reserve":
        return steps.reserve(ctx)
    if action == "credit":
        return steps.credit(ctx)
    if action == "finalize":
        result = steps.finalize(ctx)
        try:  # best effort - the outbox sweep guarantees eventual publication
            publish_pending(limit=10)
        except Exception:
            log.exception("outbox publish failed; sweep will retry")
        return result
    if action == "reverse_credit":
        return steps.reverse_credit(ctx)
    if action == "release":
        return steps.release(ctx)
    if action == "mark_rejected":
        return steps.mark_rejected(ctx, event.get("code", "REJECTED"))
    if action == "mark_failed":
        return steps.mark_failed(ctx, event.get("error", "saga failed"))
    if action == "mark_compensation_failed":
        steps.mark_compensation_failed(ctx, event.get("error", "compensation failed"))
        return {"ok": True}
    raise ValueError(f"unknown saga action: {action}")
