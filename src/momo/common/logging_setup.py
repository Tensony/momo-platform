"""Structured JSON logging with a correlation id that follows a request across services.

Correlation id flow:
  client --X-Correlation-ID--> API --(saga input.correlation_id)--> Step Functions
        --> saga-steps / fraud Lambdas --(SNS message attribute)--> SQS --> fraud consumer
Every log line carries `correlation_id`, plus the X-Ray `trace_id` when available, so one id lets you
query all services in CloudWatch Logs Insights.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone

_correlation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "correlation_id", default=None
)
_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("trace_id", default=None)

_STANDARD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


def new_correlation_id() -> str:
    return str(uuid.uuid4())


def set_correlation_id(value: str | None) -> str:
    cid = value or new_correlation_id()
    _correlation_id.set(cid)
    return cid


def get_correlation_id() -> str | None:
    return _correlation_id.get()


def set_trace_id(value: str | None) -> None:
    _trace_id.set(value)


def get_trace_id() -> str | None:
    tid = _trace_id.get()
    if tid:
        return tid
    # Lambda exposes the active X-Ray trace in this env var: "Root=1-...;Parent=...;Sampled=1"
    env = os.environ.get("_X_AMZN_TRACE_ID")
    if env:
        for part in env.split(";"):
            if part.startswith("Root="):
                return part[len("Root=") :]
    return None


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "msg": record.getMessage(),
            "correlation_id": get_correlation_id(),
            "trace_id": get_trace_id(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and key not in payload:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(service: str, level: str | None = None) -> logging.Logger:
    root = logging.getLogger()
    for handler in list(root.handlers):  # Lambda pre-installs a handler; replace it
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    root.addHandler(handler)
    root.setLevel(level or os.environ.get("LOG_LEVEL", "INFO"))
    return logging.getLogger(service)
