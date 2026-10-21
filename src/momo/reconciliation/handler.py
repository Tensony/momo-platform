"""reconciliation Lambda. Triggered by EventBridge schedules:

  {"task": "reconcile"}      daily settlement + integrity checks (also run hourly for the checks)
  {"task": "outbox_sweep"}   republish any outbox events that were not published after commit
"""

from __future__ import annotations

import logging
from datetime import date

from ..common.logging_setup import configure_logging, set_correlation_id
from ..events.outbox import publish_pending
from .report import run_reconciliation

configure_logging("reconciliation")
log = logging.getLogger("reconciliation")


def handler(event, context=None):
    set_correlation_id(None)
    task = (event or {}).get("task", "reconcile")
    if task == "outbox_sweep":
        return {"published": publish_pending(limit=200)}
    report_date = date.fromisoformat(event["date"]) if event and event.get("date") else None
    report = run_reconciliation(report_date)
    return {"status": report["status"], "issues": list(report["issues"])}
