"""Fault injection for the correctness proofs ("a failure mid-transaction creates/destroys no money").

Disabled unless ENABLE_FAULT_INJECTION=true in the environment of the service doing the work. In
production Terraform leaves it false; it is switched on only for the evidence run.

Injection points (name -> what it simulates):
  reserve.after    crash after the hold was committed
  fraud.before     fraud service unavailable
  credit.before    crash before the payee is credited
  credit.after     crash AFTER the credit committed (ambiguous outcome - the nastiest case)
  finalize.before  crash while finalising
"""

from __future__ import annotations

import os

from ..common.errors import InjectedFault

POINTS = ("reserve.after", "fraud.before", "credit.before", "credit.after", "finalize.before")


def enabled() -> bool:
    return os.environ.get("ENABLE_FAULT_INJECTION", "false").lower() == "true"


def check(fault: str | None, point: str) -> None:
    if fault and enabled() and fault == point:
        raise InjectedFault(f"injected fault at {point}")
