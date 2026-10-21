"""AWS X-Ray tracing helpers. No-ops unless XRAY_ENABLED=true so unit tests need no daemon.

* API (ECS): Flask X-Ray middleware + X-Ray daemon sidecar container.
* Lambdas: tracing_config { mode = "Active" } in Terraform; this module adds custom subsegments.
* Step Functions: tracing_configuration { enabled = true }, so one trace spans API -> saga -> Lambdas.
"""

from __future__ import annotations

import contextlib
import functools
import os
from typing import Callable, Iterator

_enabled = os.environ.get("XRAY_ENABLED", "false").lower() == "true"
_recorder = None

if _enabled:  # pragma: no cover - requires the X-Ray SDK + daemon
    from aws_xray_sdk.core import patch_all, xray_recorder

    xray_recorder.configure(
        service=os.environ.get("SERVICE_NAME", "momo"),
        context_missing="LOG_ERROR",  # never crash the request because tracing failed
    )
    patch_all()  # traces boto3/botocore calls (SNS, Step Functions, Secrets Manager, ...)
    _recorder = xray_recorder


def tracing_enabled() -> bool:
    return _enabled


def get_recorder():
    return _recorder


@contextlib.contextmanager
def subsegment(name: str, **annotations) -> Iterator[None]:
    if not _enabled or _recorder is None:
        yield
        return
    with _recorder.in_subsegment(name) as seg:  # pragma: no cover
        for key, value in annotations.items():
            if seg is not None:
                seg.put_annotation(key, str(value))
        yield


def traced(name: str) -> Callable:
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with subsegment(name):
                return fn(*args, **kwargs)

        return wrapper

    return decorator
