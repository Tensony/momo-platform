"""Pure fraud rules engine. No I/O here - facts are gathered by fraud.service - so every rule is
trivially unit-testable. Each rule returns a RuleResult; the engine takes the most severe one."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from .config import FraudConfig


class Decision(str, Enum):
    APPROVE = "APPROVE"
    REVIEW = "REVIEW"    # allow, but raise an alert for the fraud team
    DECLINE = "DECLINE"  # block; the saga compensates and releases the hold


_SEVERITY = {Decision.APPROVE: 0, Decision.REVIEW: 1, Decision.DECLINE: 2}


@dataclass(frozen=True)
class FraudContext:
    txn_id: str
    txn_type: str
    from_wallet: str
    from_kind: str
    amount: int
    now: datetime
    recent_count: int = 0                         # prior non-failed txns in the velocity window
    avg_amount_30d: float | None = None
    history_count_30d: int = 0
    geo: tuple[float, float] | None = None
    last_geo: tuple[float, float] | None = None
    last_geo_time: datetime | None = None
    daily_outgoing_total: int = 0                 # completed outgoing in the last 24h incl. this one


@dataclass(frozen=True)
class RuleResult:
    rule: str
    triggered: bool
    severity: Decision = Decision.APPROVE
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "rule": self.rule,
            "triggered": self.triggered,
            "severity": self.severity.value,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class Evaluation:
    decision: Decision
    results: list[RuleResult] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "decision": self.decision.value,
            "rules": [r.as_dict() for r in self.results if r.triggered],
        }


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * 6371.0088 * math.asin(math.sqrt(h))


def velocity_rule(ctx: FraudContext, cfg: FraudConfig) -> RuleResult:
    if ctx.recent_count >= cfg.velocity_max_txns:
        return RuleResult(
            "velocity", True, Decision.DECLINE,
            f"{ctx.recent_count} transactions in the last {cfg.velocity_window_s}s "
            f"(limit {cfg.velocity_max_txns})",
        )
    return RuleResult("velocity", False)


def amount_rule(ctx: FraudContext, cfg: FraudConfig) -> RuleResult:
    if ctx.amount > cfg.amount_hard_cap:
        return RuleResult(
            "unusual_amount", True, Decision.DECLINE,
            f"amount {ctx.amount} exceeds the hard cap {cfg.amount_hard_cap}",
        )
    if (
        ctx.avg_amount_30d is not None
        and ctx.history_count_30d >= cfg.amount_min_history
        and ctx.amount > cfg.amount_soft_floor
        and ctx.amount > cfg.amount_avg_multiplier * ctx.avg_amount_30d
    ):
        return RuleResult(
            "unusual_amount", True, Decision.REVIEW,
            f"amount {ctx.amount} is over {cfg.amount_avg_multiplier}x the 30-day average "
            f"{ctx.avg_amount_30d:.0f}",
        )
    return RuleResult("unusual_amount", False)


def geo_rule(ctx: FraudContext, cfg: FraudConfig) -> RuleResult:
    if not (ctx.geo and ctx.last_geo and ctx.last_geo_time):
        return RuleResult("geo_anomaly", False)
    distance = haversine_km(ctx.geo, ctx.last_geo)
    if distance < cfg.geo_min_distance_km:
        return RuleResult("geo_anomaly", False)
    hours = max((ctx.now - ctx.last_geo_time).total_seconds() / 3600.0, 1 / 3600.0)
    speed = distance / hours
    if speed > cfg.geo_max_speed_kmh:
        return RuleResult(
            "geo_anomaly", True, Decision.REVIEW,
            f"{distance:.0f} km from the previous location in {hours * 60:.1f} min "
            f"({speed:.0f} km/h)",
        )
    return RuleResult("geo_anomaly", False)


def daily_volume_rule(ctx: FraudContext, cfg: FraudConfig) -> RuleResult:
    if ctx.daily_outgoing_total > cfg.daily_volume_limit:
        return RuleResult(
            "daily_volume", True, Decision.REVIEW,
            f"{ctx.daily_outgoing_total} sent in 24h (limit {cfg.daily_volume_limit})",
        )
    return RuleResult("daily_volume", False)


def ml_scoring_rule(ctx: FraudContext, cfg: FraudConfig) -> RuleResult:
    from .ml_scoring import ml_scoring_rule as _ml_rule
    return _ml_rule(ctx, cfg)


SYNC_RULES = (velocity_rule, amount_rule, geo_rule)                   # run inside the saga, can DECLINE
ASYNC_RULES = (daily_volume_rule,)                                    # run after completion via SNS -> SQS
ALL_RULES = (velocity_rule, amount_rule, geo_rule, ml_scoring_rule)   # includes ML behavioral scoring stretch goal


def _exempt(ctx: FraudContext) -> bool:
    # System-initiated money (float top-ups from MINT, reversals ordered by an admin) is not
    # customer behaviour; fraud rules would only produce noise.
    return ctx.from_kind == "SYSTEM" or ctx.txn_type in {"REVERSAL", "FLOAT_TOPUP"}


def evaluate(ctx: FraudContext, cfg: FraudConfig, rules=SYNC_RULES) -> Evaluation:
    if _exempt(ctx):
        return Evaluation(Decision.APPROVE, [])
    results = [rule(ctx, cfg) for rule in rules]
    worst = Decision.APPROVE
    for r in results:
        if r.triggered and _SEVERITY[r.severity] > _SEVERITY[worst]:
            worst = r.severity
    return Evaluation(worst, results)
