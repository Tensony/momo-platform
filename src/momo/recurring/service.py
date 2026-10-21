"""Scheduled Recurring Payments Service.

Enables standing orders, daily/weekly/monthly subscriptions, and scheduled transfers.
Execution uses deterministic Idempotency-Keys to guarantee exactly-once processing
per scheduled period even under network retries or concurrent scheduler ticks.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from momo.common.errors import InvalidRequest, NotFound
from momo.common.money import parse_amount, parse_currency

log = logging.getLogger(__name__)

SUPPORTED_FREQUENCIES = {"DAILY", "WEEKLY", "MONTHLY"}


@dataclass
class RecurringSchedule:
    id: str
    owner_id: str
    from_wallet: str
    to_wallet: str
    amount: int
    currency: str
    frequency: str
    status: str  # ACTIVE, PAUSED, CANCELLED
    next_run_at: datetime
    last_run_at: Optional[datetime] = None
    execution_count: int = 0
    created_at: datetime = None

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now(timezone.utc)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "owner_id": self.owner_id,
            "from_wallet": self.from_wallet,
            "to_wallet": self.to_wallet,
            "amount": self.amount,
            "currency": self.currency,
            "frequency": self.frequency,
            "status": self.status,
            "next_run_at": self.next_run_at.isoformat(),
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "execution_count": self.execution_count,
            "created_at": self.created_at.isoformat(),
        }


def compute_next_run(current_run: datetime, frequency: str) -> datetime:
    freq = frequency.upper()
    if freq == "DAILY":
        return current_run + timedelta(days=1)
    elif freq == "WEEKLY":
        return current_run + timedelta(weeks=1)
    elif freq == "MONTHLY":
        # Approximate 30-day billing cycle
        return current_run + timedelta(days=30)
    else:
        raise InvalidRequest(f"unsupported frequency: {frequency}")


# In-memory registry / database bridge for recurring schedules
class RecurringScheduleRegistry:
    def __init__(self):
        self._schedules: dict[str, RecurringSchedule] = {}

    def create(
        self,
        owner_id: str,
        from_wallet: str,
        to_wallet: str,
        amount: int,
        frequency: str,
        currency: str = "ZMW",
        start_at: Optional[datetime] = None,
    ) -> RecurringSchedule:
        freq = frequency.upper()
        if freq not in SUPPORTED_FREQUENCIES:
            raise InvalidRequest(f"frequency must be one of {sorted(SUPPORTED_FREQUENCIES)}")
        amt = parse_amount(amount)
        curr = parse_currency(currency)
        
        now = datetime.now(timezone.utc)
        next_run = start_at if start_at is not None else now

        sched_id = str(uuid.uuid4())
        sched = RecurringSchedule(
            id=sched_id,
            owner_id=owner_id,
            from_wallet=from_wallet,
            to_wallet=to_wallet,
            amount=amt,
            currency=curr,
            frequency=freq,
            status="ACTIVE",
            next_run_at=next_run,
        )
        self._schedules[sched_id] = sched
        return sched

    def get(self, schedule_id: str) -> RecurringSchedule:
        if schedule_id not in self._schedules:
            raise NotFound("recurring schedule not found")
        return self._schedules[schedule_id]

    def list_by_owner(self, owner_id: str) -> List[RecurringSchedule]:
        return [s for s in self._schedules.values() if s.owner_id == owner_id]

    def cancel(self, schedule_id: str, owner_id: Optional[str] = None) -> RecurringSchedule:
        s = self.get(schedule_id)
        if owner_id and s.owner_id != owner_id:
            raise NotFound("recurring schedule not found")
        s.status = "CANCELLED"
        return s

    def get_due_schedules(self, as_of: Optional[datetime] = None) -> List[RecurringSchedule]:
        now = as_of or datetime.now(timezone.utc)
        return [
            s for s in self._schedules.values()
            if s.status == "ACTIVE" and s.next_run_at <= now
        ]

    def advance_schedule(self, schedule_id: str, execution_time: datetime) -> RecurringSchedule:
        s = self.get(schedule_id)
        s.last_run_at = execution_time
        s.execution_count += 1
        s.next_run_at = compute_next_run(s.next_run_at, s.frequency)
        return s


_REGISTRY = RecurringScheduleRegistry()


def get_recurring_registry() -> RecurringScheduleRegistry:
    return _REGISTRY


def process_due_recurring_transfers(
    transfer_executor,
    as_of: Optional[datetime] = None,
) -> List[dict]:
    """Execute all due recurring payments using deterministic idempotency keys.

    transfer_executor is a callable: (idempotency_key, from_wallet, to_wallet, amount, currency) -> result
    """
    now = as_of or datetime.now(timezone.utc)
    registry = get_recurring_registry()
    due = registry.get_due_schedules(as_of=now)
    results = []

    for item in due:
        # Deterministic idempotency key: recurring-{id}-{date-str}
        date_str = item.next_run_at.strftime("%Y%m%d%H%M")
        idempotency_key = f"recurring-{item.id}-{date_str}"
        
        try:
            res = transfer_executor(
                idempotency_key=idempotency_key,
                from_wallet=item.from_wallet,
                to_wallet=item.to_wallet,
                amount=item.amount,
                currency=item.currency,
            )
            registry.advance_schedule(item.id, now)
            results.append({"schedule_id": item.id, "status": "EXECUTED", "result": res})
        except Exception as e:
            log.exception("Error executing recurring schedule %s: %s", item.id, e)
            results.append({"schedule_id": item.id, "status": "FAILED", "error": str(e)})

    return results
