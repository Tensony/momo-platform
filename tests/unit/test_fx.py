"""Unit tests for Foreign Exchange (FX) rates, quotes, and multi-currency conversion."""

import pytest

from momo.common.errors import InvalidRequest
from momo.fx.service import (
    DEFAULT_RATES_TO_ZMW,
    convert_currency,
    create_quote,
    get_exchange_rate,
    verify_quote,
)


def test_rates_symmetry_and_cross_rates():
    # ZMW to ZMW is 1.0
    assert get_exchange_rate("ZMW", "ZMW") == 1.0

    # 1 USD = 27.50 ZMW
    usd_to_zmw = get_exchange_rate("USD", "ZMW")
    assert usd_to_zmw == 27.50

    # 1 ZMW in USD = 1 / 27.50
    zmw_to_usd = get_exchange_rate("ZMW", "USD")
    assert round(zmw_to_usd * 27.50, 4) == 1.0

    # Cross rate EUR to USD
    eur_to_usd = get_exchange_rate("EUR", "USD")
    expected = DEFAULT_RATES_TO_ZMW["EUR"] / DEFAULT_RATES_TO_ZMW["USD"]
    assert abs(eur_to_usd - expected) < 1e-6


def test_unsupported_currency_raises():
    with pytest.raises(InvalidRequest, match="unsupported currency pair"):
        get_exchange_rate("ZMW", "XYZ")


def test_create_and_verify_quote():
    quote = create_quote("USD", "ZMW", from_amount=10_000)  # $100.00
    assert quote.from_currency == "USD"
    assert quote.to_currency == "ZMW"
    assert quote.from_amount == 10_000
    assert quote.fee_minor > 0
    assert quote.to_amount > 0
    assert verify_quote(quote) is True

    # Tampered quote fails verification
    tampered = quote.to_dict()
    tampered["to_amount"] = quote.to_amount + 50000
    assert verify_quote(tampered) is False


def test_convert_currency_same_currency():
    to_amt, rate, fee = convert_currency("ZMW", "ZMW", 5000)
    assert to_amt == 5000
    assert rate == 1.0
    assert fee == 0


def test_convert_currency_with_valid_quote():
    q = create_quote("ZMW", "USD", 27_500)  # 275.00 ZMW
    to_amt, rate, fee = convert_currency("ZMW", "USD", 27_500, quote=q)
    assert to_amt == q.to_amount
    assert fee == q.fee_minor
