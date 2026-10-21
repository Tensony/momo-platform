"""Transactional outbox: events are written in the SAME database transaction as the ledger change
(see saga.steps.finalize) and published to SNS afterwards. If the process dies between commit and
publish, a scheduled sweep (reconciliation Lambda) publishes what was missed - so no completed
transaction is ever silently absent from the event stream. Delivery is at-least-once; consumers
(the fraud reviewer) are idempotent.

Async path:  outbox -> SNS topic (txn-events) -> SQS (fraud-review queue, DLQ after 5 receives)
"""

from __future__ import annotations

import json
import logging
import os
from typing import Protocol

from ..common.db import transaction

log = logging.getLogger(__name__)


class Publisher(Protocol):
    def publish(self, event_type: str, payload: dict, correlation_id: str | None) -> None: ...


class SnsPublisher:
    def __init__(self, topic_arn: str, client=None):
        import boto3

        self.topic_arn = topic_arn
        self.client = client or boto3.client("sns")

    def publish(self, event_type: str, payload: dict, correlation_id: str | None) -> None:
        attrs = {"event_type": {"DataType": "String", "StringValue": event_type}}
        if correlation_id:
            attrs["correlation_id"] = {"DataType": "String", "StringValue": correlation_id}
        self.client.publish(
            TopicArn=self.topic_arn, Message=json.dumps(payload), MessageAttributes=attrs
        )


def default_publisher() -> Publisher | None:
    arn = os.environ.get("EVENT_TOPIC_ARN")
    return SnsPublisher(arn) if arn else None


def publish_pending(limit: int = 50, publisher: Publisher | None = None) -> int:
    """Publish unpublished outbox rows. SKIP LOCKED lets concurrent sweepers/finalizers coexist."""
    publisher = publisher or default_publisher()
    if publisher is None:
        return 0
    published = 0
    with transaction() as conn:
        rows = conn.execute(
            """SELECT id, event_type, payload, correlation_id FROM outbox_events
                WHERE published_at IS NULL ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED""",
            (limit,),
        ).fetchall()
        for row in rows:
            publisher.publish(row["event_type"], row["payload"], row["correlation_id"])
            conn.execute("UPDATE outbox_events SET published_at = now() WHERE id = %s", (row["id"],))
            published += 1
    if published:
        log.info("published outbox events", extra={"count": published})
    return published
