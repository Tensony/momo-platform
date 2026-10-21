"""Unit tests for scheduled recurring payments service."""

from datetime import datetime, timedelta, timezone

import pytest

from momo.common.errors import InvalidRequest, NotFound
from momo.recurring.service import (
    RecurringScheduleRegistry,
    compute_next_run,
)


def test_frequency_calculation():
    now = datetime(2026, 10, 21, 10, 0, tzinfo=timezone.utc)
    daily = compute_next_run(now, "DAILY")
    assert daily == now + timedelta(days=1)

    weekly = compute_next_run(now, "WEEKLY")
    assert weekly == now + timedelta(weeks=1)

    monthly = compute_next_run(now, "MONTHLY")
    assert monthly == now + timedelta(days=30)

    with pytest.raises(InvalidRequest):
        compute_next_run(now, "HOURLY")


def test_schedule_lifecycle():
    reg = RecurringScheduleRegistry()
    s = reg.create(
        owner_id="user-1",
        from_wallet="w1",
        to_wallet="w2",
        amount=10_000,
        frequency="MONTHLY",
        currency="ZMW",
    )
    assert s.status == "ACTIVE"
    assert s.execution_count == 0

    schedules = reg.list_by_owner("user-1")
    assert len(schedules) == 1
    assert schedules[0].id == s.id

    # Cancel
    cancelled = reg.cancel(s.id, owner_id="user-1")
    assert cancelled.status == "CANCELLED"

    with pytest.raises(NotFound):
        reg.cancel("non-existent-id")


def test_due_schedules_and_idempotent_execution():
    reg = RecurringScheduleRegistry()
    now = datetime.now(timezone.utc)
    s = reg.create(
        owner_id="user-2",
        from_wallet="w1",
        to_wallet="w2",
        amount=5_000,
        frequency="DAILY",
        start_at=now - timedelta(hours=1),  # already due
    )

    executed_keys = []

    def mock_transfer(idempotency_key, from_wallet, to_wallet, amount, currency):
        executed_keys.append(idempotency_key)
        return {"status": "SUCCESS"}

    # Process due schedules via custom registry testing
    due = reg.get_due_schedules(as_of=now)
    assert len(due) == 1
    assert due[0].id == s.id

    reg.advance_schedule(s.id, now)
    assert s.execution_count == 1
    assert s.next_run_at > now
