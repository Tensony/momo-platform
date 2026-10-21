

from momo.common import db as dbm
from momo.common.logging_setup import get_trace_id, set_trace_id
from momo.fraud.config import clear_cache, load_config


def test_trace_id_from_lambda_env(monkeypatch):
    set_trace_id(None)
    monkeypatch.setenv("_X_AMZN_TRACE_ID", "Root=1-abc-def;Parent=1;Sampled=1")
    assert get_trace_id() == "1-abc-def"
    set_trace_id("explicit")
    assert get_trace_id() == "explicit"
    set_trace_id(None)


def test_fraud_config_loads_from_ssm_with_safe_fallback(monkeypatch):
    class Ssm:
        def get_parameter(self, Name):
            return {"Parameter": {"Value": '{"velocity_max_txns": 42}'}}

    import boto3
    monkeypatch.setenv("FRAUD_CONFIG_PARAM", "/momo/fraud/config")
    monkeypatch.setattr(boto3, "client", lambda *_a, **_k: Ssm())
    clear_cache()
    assert load_config().velocity_max_txns == 42
    assert load_config().velocity_max_txns == 42   # cached

    def boom(*_a, **_k):
        raise RuntimeError("ssm down")
    monkeypatch.setattr(boto3, "client", boom)
    clear_cache()
    assert load_config().velocity_max_txns == 5     # defaults: payments keep flowing
    clear_cache()


def test_iam_token_is_cached(monkeypatch):
    import boto3
    calls = []

    class Rds:
        def generate_db_auth_token(self, **kw):
            calls.append(kw)
            return f"token-{len(calls)}"

    monkeypatch.setattr(boto3, "client", lambda *_a, **_k: Rds())
    dbm._token_cache.clear()
    assert dbm._iam_token("h", 5432, "momo_app", "af-south-1") == "token-1"
    assert dbm._iam_token("h", 5432, "momo_app", "af-south-1") == "token-1"
    assert len(calls) == 1
    dbm._token_cache.clear()
