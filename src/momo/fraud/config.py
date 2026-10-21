"""Fraud rule thresholds. Runtime-tunable through SSM Parameter Store (JSON), no redeploy needed.

Amounts are minor units (ngwee). Example override:
  aws ssm put-parameter --name /momo/fraud/config --type String --overwrite \
      --value '{"velocity_max_txns": 500}'
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, fields


@dataclass(frozen=True)
class FraudConfig:
    velocity_window_s: int = 60
    velocity_max_txns: int = 5            # this many prior txns inside the window => DECLINE
    amount_hard_cap: int = 5_000_000      # ZMW 50,000 in a single transaction => DECLINE
    amount_soft_floor: int = 500_000      # below ZMW 5,000 the "unusual amount" rule never fires
    amount_avg_multiplier: float = 5.0    # > 5x the wallet's 30-day average => REVIEW
    amount_min_history: int = 3           # need this many past txns before using the average
    geo_max_speed_kmh: float = 900.0      # faster than a passenger jet => impossible travel
    geo_min_distance_km: float = 50.0     # ignore GPS jitter
    daily_volume_limit: int = 10_000_000  # ZMW 100,000 outgoing per 24h => REVIEW (async rule)


_CACHE: tuple[FraudConfig, float] | None = None
_TTL_S = 60


def parse_config(raw: str | None) -> FraudConfig:
    """Parse a JSON override; unknown keys and wrongly-typed values are ignored, never fatal."""
    defaults = FraudConfig()
    if not raw:
        return defaults
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return defaults
    if not isinstance(data, dict):
        return defaults
    overrides = {}
    for f in fields(FraudConfig):
        if f.name not in data:
            continue
        value = data[f.name]
        default = getattr(defaults, f.name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        overrides[f.name] = float(value) if isinstance(default, float) else int(value)
    return FraudConfig(**{**defaults.__dict__, **overrides})


def load_config() -> FraudConfig:
    global _CACHE
    now = time.time()
    if _CACHE and _CACHE[1] > now:
        return _CACHE[0]
    raw = os.environ.get("FRAUD_CONFIG_JSON")  # local/dev override
    param = os.environ.get("FRAUD_CONFIG_PARAM")
    if not raw and param:
        try:
            import boto3

            raw = boto3.client("ssm").get_parameter(Name=param)["Parameter"]["Value"]
        except Exception:  # SSM unavailable: fall back to safe defaults rather than failing payments
            raw = None
    cfg = parse_config(raw)
    _CACHE = (cfg, now + _TTL_S)
    return cfg


def clear_cache() -> None:
    global _CACHE
    _CACHE = None
