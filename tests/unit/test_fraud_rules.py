from datetime import datetime, timedelta, timezone

from momo.fraud.config import FraudConfig, parse_config
from momo.fraud.rules import (
    Decision,
    FraudContext,
    amount_rule,
    daily_volume_rule,
    evaluate,
    geo_rule,
    haversine_km,
    velocity_rule,
)

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
CFG = FraudConfig()
LUSAKA, NDOLA, LIVINGSTONE = (-15.3875, 28.3228), (-12.9587, 28.6366), (-17.8419, 25.8543)


def ctx(**kw):
    base = dict(txn_id="t1", txn_type="TRANSFER", from_wallet="w1", from_kind="CUSTOMER", amount=10_000, now=NOW)
    base.update(kw)
    return FraudContext(**base)


def test_haversine_known_distance():
    assert 370 < haversine_km(LUSAKA, LIVINGSTONE) < 390
    assert haversine_km(LUSAKA, LUSAKA) == 0


def test_velocity_declines_at_limit_only():
    assert not velocity_rule(ctx(recent_count=CFG.velocity_max_txns - 1), CFG).triggered
    r = velocity_rule(ctx(recent_count=CFG.velocity_max_txns), CFG)
    assert r.triggered and r.severity == Decision.DECLINE


def test_amount_hard_cap_declines():
    assert amount_rule(ctx(amount=CFG.amount_hard_cap + 1), CFG).severity == Decision.DECLINE
    assert not amount_rule(ctx(amount=CFG.amount_hard_cap), CFG).triggered


def test_amount_unusual_vs_history_reviews():
    big = ctx(amount=1_000_000, avg_amount_30d=50_000.0, history_count_30d=10)
    assert amount_rule(big, CFG).severity == Decision.REVIEW


def test_amount_rule_needs_history_and_floor():
    assert not amount_rule(ctx(amount=1_000_000, avg_amount_30d=50_000.0, history_count_30d=1), CFG).triggered
    assert not amount_rule(ctx(amount=400_000, avg_amount_30d=1_000.0, history_count_30d=10), CFG).triggered
    assert not amount_rule(ctx(amount=1_000_000, avg_amount_30d=None), CFG).triggered


def test_geo_impossible_travel_reviews():
    c = ctx(geo=NDOLA, last_geo=LUSAKA, last_geo_time=NOW - timedelta(minutes=10))
    r = geo_rule(c, CFG)
    assert r.triggered and r.severity == Decision.REVIEW


def test_geo_plausible_travel_and_jitter_ignored():
    slow = ctx(geo=NDOLA, last_geo=LUSAKA, last_geo_time=NOW - timedelta(hours=8))
    assert not geo_rule(slow, CFG).triggered
    near = ctx(geo=(-15.39, 28.33), last_geo=LUSAKA, last_geo_time=NOW - timedelta(seconds=30))
    assert not geo_rule(near, CFG).triggered
    assert not geo_rule(ctx(geo=NDOLA), CFG).triggered


def test_daily_volume_rule():
    assert daily_volume_rule(ctx(daily_outgoing_total=CFG.daily_volume_limit + 1), CFG).triggered
    assert not daily_volume_rule(ctx(daily_outgoing_total=CFG.daily_volume_limit), CFG).triggered


def test_engine_takes_most_severe_decision():
    c = ctx(recent_count=99, geo=NDOLA, last_geo=LUSAKA, last_geo_time=NOW - timedelta(minutes=5))
    ev = evaluate(c, CFG)
    assert ev.decision == Decision.DECLINE
    assert {r["rule"] for r in ev.as_dict()["rules"]} == {"velocity", "geo_anomaly"}
    only_review = ctx(geo=NDOLA, last_geo=LUSAKA, last_geo_time=NOW - timedelta(minutes=5))
    assert evaluate(only_review, CFG).decision == Decision.REVIEW
    assert evaluate(ctx(), CFG).decision == Decision.APPROVE


def test_system_and_reversal_traffic_is_exempt():
    assert evaluate(ctx(from_kind="SYSTEM", recent_count=999), CFG).decision == Decision.APPROVE
    assert evaluate(ctx(txn_type="REVERSAL", recent_count=999), CFG).decision == Decision.APPROVE


def test_config_parsing_is_defensive():
    assert parse_config(None) == CFG
    assert parse_config("not json") == CFG
    assert parse_config("[1,2]") == CFG
    cfg = parse_config('{"velocity_max_txns": 500, "geo_max_speed_kmh": 1200, "bogus": 1, "amount_hard_cap": "x", "daily_volume_limit": true}')
    assert cfg.velocity_max_txns == 500 and cfg.geo_max_speed_kmh == 1200.0
    assert cfg.amount_hard_cap == CFG.amount_hard_cap and cfg.daily_volume_limit == CFG.daily_volume_limit
