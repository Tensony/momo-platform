"""fraud-engine Lambda: one deployable service, two triggers.

  * Direct invocation by the Step Functions saga   -> synchronous decision
  * SQS event source mapping (SNS fan-out queue)   -> asynchronous post-completion review
"""

from __future__ import annotations

import json
import logging

from ..common.logging_setup import configure_logging, set_correlation_id
from ..saga import faults
from . import service

configure_logging("fraud-engine")
log = logging.getLogger("fraud-engine")


def _handle_sqs(event: dict) -> dict:
    failures = []
    for record in event["Records"]:
        try:
            attrs = record.get("messageAttributes") or {}
            cid = (attrs.get("correlation_id") or {}).get("stringValue")
            body = json.loads(record["body"])
            set_correlation_id(cid or body.get("correlation_id"))
            result = service.review_completed(body)
            log.info("async fraud review", extra={"txn_id": body.get("txn_id"), **result})
        except Exception:
            log.exception("async fraud review failed", extra={"message_id": record.get("messageId")})
            failures.append({"itemIdentifier": record["messageId"]})  # retried, then -> DLQ
    return {"batchItemFailures": failures}


def handler(event, context=None):
    if "Records" in event:
        return _handle_sqs(event)
    set_correlation_id(event.get("correlation_id"))
    faults.check(event.get("fault"), "fraud.before")
    return service.check_transaction(event)
