"""Unit tests for ML and anomaly-based fraud scoring."""

from datetime import datetime, timedelta, timezone

from momo.fraud.config import FraudConfig
from momo.fraud.ml_scoring import compute_fraud_score, ml_scoring_rule
from momo.fraud.rules import Decision, FraudContext

NOW = datetime(2026, 10, 21, 14, 0, tzinfo=timezone.utc)
NOCTURNAL = datetime(2026, 10, 21, 2, 30, tzinfo=timezone.utc)
CFG = FraudConfig()
LUSAKA, NDOLA = (-15.3875, 28.3228), (-12.9587, 28.6366)


def test_benign_transaction_low_score():
    ctx = FraudContext(
        txn_id="t1",
        txn_type="TRANSFER",
        from_wallet="w1",
        from_kind="CUSTOMER",
        amount=5_000,
        now=NOW,
        recent_count=1,
        avg_amount_30d=5_000.0,
        history_count_30d=15,
        daily_outgoing_total=5_000,
    )
    res = compute_fraud_score(ctx, CFG)
    assert res.score < 0.50
    assert res.decision == Decision.APPROVE

    rule_res = ml_scoring_rule(ctx, CFG)
    assert not rule_res.triggered


def test_high_risk_anomalies_produce_high_score():
    # Extreme velocity, sudden huge amount, impossible travel, nocturnal
    ctx = FraudContext(
        txn_id="t2",
        txn_type="CASH_OUT",
        from_wallet="w1",
        from_kind="CUSTOMER",
        amount=2_000_000,
        now=NOCTURNAL,
        recent_count=18,
        avg_amount_30d=10_000.0,
        history_count_30d=10,
        geo=NDOLA,
        last_geo=LUSAKA,
        last_geo_time=NOCTURNAL - timedelta(minutes=5),
        daily_outgoing_total=4_800_000,
    )
    res = compute_fraud_score(ctx, CFG)
    assert res.score >= 0.85
    assert res.decision == Decision.DECLINE
    assert len(res.factors) >= 3

    rule_res = ml_scoring_rule(ctx, CFG)
    assert rule_res.triggered
    assert rule_res.severity == Decision.DECLINE
    assert "ml_score=" in rule_res.detail
