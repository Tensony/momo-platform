"""Custom CloudWatch metrics via Embedded Metric Format (EMF).

A single JSON line on stdout is turned into a CloudWatch metric automatically (ECS awslogs driver and
Lambda both ship stdout to CloudWatch Logs) - no PutMetricData call, no extra IAM permission, no
added request latency.
"""

from __future__ import annotations

import json
import sys
import time

NAMESPACE = "Momo"


def emit(name: str, value: float = 1, unit: str = "Count", **dimensions: str) -> None:
    record = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": NAMESPACE,
                    "Dimensions": [list(dimensions)] if dimensions else [[]],
                    "Metrics": [{"Name": name, "Unit": unit}],
                }
            ],
        },
        name: value,
        **dimensions,
    }
    sys.stdout.write(json.dumps(record) + "\n")
    sys.stdout.flush()
