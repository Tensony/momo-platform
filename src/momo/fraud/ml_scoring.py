"""Machine Learning and Statistical Anomaly Fraud Scoring Engine.

Extracts multidimensional behavioral feature vectors from FraudContext and computes
a calibrated risk score in [0.0, 1.0] with feature attribution.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Tuple

from .config import FraudConfig
from .rules import Decision, FraudContext, RuleResult, haversine_km


@dataclass(frozen=True)
class MLScoreResult:
    score: float
    decision: Decision
    factors: List[str]


# Model weights calibrated for mobile money transaction patterns
W_INTERCEPT = -2.80
W_AMOUNT_ANOMALY = 1.45
W_VELOCITY = 1.20
W_GEO_VELOCITY = 1.60
W_NOCTURNAL = 0.55
W_DAILY_VOLUME = 0.90


def extract_features(ctx: FraudContext, cfg: FraudConfig) -> Tuple[dict, List[str]]:
    """Extract and normalize risk features from context."""
    features = {}
    factors = []

    # 1. Amount anomaly ratio
    if ctx.avg_amount_30d and ctx.avg_amount_30d > 0 and ctx.history_count_30d >= cfg.amount_min_history:
        ratio = ctx.amount / ctx.avg_amount_30d
        features["amount_ratio"] = min(ratio / 10.0, 3.0)  # normalized
        if ratio >= 4.0:
            factors.append(f"amount_{ratio:.1f}x_30d_avg")
    else:
        features["amount_ratio"] = 0.0

    # 2. Velocity score (recent counts vs window limit)
    if cfg.velocity_max_txns > 0:
        vel_ratio = ctx.recent_count / float(cfg.velocity_max_txns)
        features["velocity"] = min(vel_ratio, 2.5)
        if vel_ratio >= 0.7:
            factors.append(f"high_velocity_{ctx.recent_count}_txns")
    else:
        features["velocity"] = 0.0

    # 3. Geovelocity (km/h)
    if ctx.geo and ctx.last_geo and ctx.last_geo_time:
        distance = haversine_km(ctx.geo, ctx.last_geo)
        hours = max((ctx.now - ctx.last_geo_time).total_seconds() / 3600.0, 1 / 3600.0)
        speed_kmh = distance / hours
        # Normalize: 0 to 1200 km/h mapped to 0..2.0
        features["geovelocity"] = min(speed_kmh / 500.0, 3.0)
        if speed_kmh > 400.0:
            factors.append(f"impossible_speed_{speed_kmh:.0f}kmh")
    else:
        features["geovelocity"] = 0.0

    # 4. Nocturnal hour risk (1 AM to 5 AM)
    hour = ctx.now.hour
    is_nocturnal = 1.0 if (1 <= hour <= 5) else 0.0
    features["nocturnal"] = is_nocturnal
    if is_nocturnal:
        factors.append("nocturnal_activity")

    # 5. Daily volume saturation
    if cfg.daily_volume_limit > 0:
        vol_ratio = ctx.daily_outgoing_total / float(cfg.daily_volume_limit)
        features["daily_volume"] = min(vol_ratio, 2.0)
        if vol_ratio >= 0.8:
            factors.append(f"daily_volume_{vol_ratio*100:.0f}%_saturated")
    else:
        features["daily_volume"] = 0.0

    return features, factors


def compute_fraud_score(ctx: FraudContext, cfg: FraudConfig) -> MLScoreResult:
    """Compute ML anomaly score in [0.0, 1.0] using logistic calibration."""
    features, factors = extract_features(ctx, cfg)

    logit = (
        W_INTERCEPT
        + W_AMOUNT_ANOMALY * features["amount_ratio"]
        + W_VELOCITY * features["velocity"]
        + W_GEO_VELOCITY * features["geovelocity"]
        + W_NOCTURNAL * features["nocturnal"]
        + W_DAILY_VOLUME * features["daily_volume"]
    )

    # Sigmoid function
    score = 1.0 / (1.0 + math.exp(-logit))
    score = round(score, 4)

    # Thresholds: >= 0.85 DECLINE, >= 0.60 REVIEW, < 0.60 APPROVE
    if score >= 0.85:
        decision = Decision.DECLINE
    elif score >= 0.60:
        decision = Decision.REVIEW
    else:
        decision = Decision.APPROVE

    return MLScoreResult(score=score, decision=decision, factors=factors)


def ml_scoring_rule(ctx: FraudContext, cfg: FraudConfig) -> RuleResult:
    """Pluggable fraud rule for ML behavioral anomaly scoring."""
    result = compute_fraud_score(ctx, cfg)
    if result.decision != Decision.APPROVE:
        factors_str = ", ".join(result.factors) if result.factors else "multivariate_anomaly"
        return RuleResult(
            rule="ml_anomaly_score",
            triggered=True,
            severity=result.decision,
            detail=f"ml_score={result.score:.2f} ({factors_str})",
        )
    return RuleResult("ml_anomaly_score", False)
