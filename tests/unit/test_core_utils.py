import json
import logging

import pytest

from momo import idempotency
from momo.common import metrics
from momo.common.errors import InvalidRequest
from momo.common.hashing import GENESIS_HASH, compute_entry_hash, hash_request
from momo.common.logging_setup import (
    JsonFormatter,
    get_correlation_id,
    new_correlation_id,
    set_correlation_id,
)
from momo.common.money import format_minor, parse_amount, parse_currency
from momo.saga import faults
from momo.saga.context import SagaResult, TxnContext
from momo.service import parse_geo, parse_txn_request, to_http


@pytest.mark.parametrize("bad", [0, -5, 1.5, "100", None, True, 10**15])
def test_parse_amount_rejects_bad_values(bad):
    with pytest.raises(InvalidRequest):
        parse_amount(bad)


def test_parse_amount_and_currency_ok():
    assert parse_amount(2500) == 2500
    assert parse_currency(None) == "ZMW" and parse_currency("zmw") == "ZMW"
    with pytest.raises(InvalidRequest):
        parse_currency("XYZ")
    assert format_minor(123456) == "ZMW 1,234.56" and format_minor(-5) == "-ZMW 0.05"


def test_entry_hash_is_deterministic_and_sensitive():
    kw = dict(prev_hash=GENESIS_HASH, wallet_id="w", wallet_seq=1, txn_id="t", step="s",
              direction="DEBIT", amount=10, balance_after=90)
    assert compute_entry_hash(**kw) == compute_entry_hash(**kw)
    for field, value in [("amount", 11), ("balance_after", 91), ("direction", "CREDIT"), ("wallet_seq", 2)]:
        assert compute_entry_hash(**{**kw, field: value}) != compute_entry_hash(**kw)


def test_request_hash_ignores_key_order():
    assert hash_request({"a": 1, "b": 2}) == hash_request({"b": 2, "a": 1})
    assert hash_request({"a": 1}) != hash_request({"a": 2})


def test_idempotency_key_validation_and_txn_id():
    assert idempotency.validate_key("abcdefgh") == "abcdefgh"
    for bad in (None, "", "short", "x" * 129, "bad\nkey-12345"):
        with pytest.raises(InvalidRequest):
            idempotency.validate_key(bad)
    assert idempotency.derive_txn_id("s", "k1") == idempotency.derive_txn_id("s", "k1")
    assert idempotency.derive_txn_id("s", "k1") != idempotency.derive_txn_id("s", "k2")


def test_json_logging_carries_correlation_id():
    cid = set_correlation_id("req-12345678")
    assert cid == get_correlation_id() == "req-12345678"
    rec = logging.LogRecord("x", logging.INFO, "f", 1, "hello %s", ("world",), None)
    rec.txn_id = "t-1"
    out = json.loads(JsonFormatter("svc").format(rec))
    assert out["msg"] == "hello world" and out["correlation_id"] == "req-12345678"
    assert out["service"] == "svc" and out["txn_id"] == "t-1"
    assert len(new_correlation_id()) == 36


def test_emf_metric_format(capsys):
    metrics.emit("TransactionRequest", 1, Outcome="COMPLETED")
    line = json.loads(capsys.readouterr().out.strip())
    cw = line["_aws"]["CloudWatchMetrics"][0]
    assert cw["Namespace"] == "Momo" and cw["Dimensions"] == [["Outcome"]]
    assert line["TransactionRequest"] == 1 and line["Outcome"] == "COMPLETED"


def test_parse_geo_and_txn_request():
    assert parse_geo(None) is None
    assert parse_geo({"lat": -15.4, "lon": 28.3}) == (-15.4, 28.3)
    for bad in ({"lat": 99, "lon": 0}, {"lat": "x", "lon": 0}, {"lat": 1}, "nope"):
        with pytest.raises(InvalidRequest):
            parse_geo(bad)
    req = parse_txn_request("TRANSFER", {"from_wallet": "a", "to_wallet": "b", "amount": 5})
    assert (req.type, req.amount, req.currency) == ("TRANSFER", 5, "ZMW")
    with pytest.raises(InvalidRequest):
        parse_txn_request("TRANSFER", {"to_wallet": "b", "amount": 5})
    with pytest.raises(InvalidRequest):
        parse_txn_request("TRANSFER", ["x"])


def test_http_mapping_of_saga_results():
    assert to_http(SagaResult("t", "COMPLETED"))[0] == 201
    assert to_http(SagaResult("t", "COMPLETED", detail={"fraud_decision": "REVIEW"}))[1]["flagged_for_review"]
    code, body = to_http(SagaResult("t", "REJECTED", "INSUFFICIENT_FUNDS"))
    assert code == 422 and body["code"] == "INSUFFICIENT_FUNDS"
    assert to_http(SagaResult("t", "FAILED", "SAGA_FAILED_COMPENSATED"))[0] == 500
    assert "investigated" in to_http(SagaResult("t", "FAILED", "COMPENSATION_FAILED"))[1]["message"]


def test_fault_injection_is_off_by_default(monkeypatch):
    monkeypatch.delenv("ENABLE_FAULT_INJECTION", raising=False)
    faults.check("credit.after", "credit.after")  # must not raise when disabled
    monkeypatch.setenv("ENABLE_FAULT_INJECTION", "true")
    faults.check("credit.after", "credit.before")  # different point: no raise
    with pytest.raises(Exception, match="injected"):
        faults.check("credit.after", "credit.after")


def test_txn_context_roundtrip_ignores_unknown_keys():
    ctx = TxnContext(txn_id="t", type="TRANSFER", from_wallet="a", to_wallet="b", amount=1)
    assert TxnContext.from_dict({**ctx.to_dict(), "extra": 1}) == ctx
