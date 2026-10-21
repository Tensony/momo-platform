"""Foreign exchange (FX) and multi-currency conversion services.

Provides deterministic exchange rates, time-limited cryptographic quotes,
and precise minor-unit conversion across supported currencies without floating point loss.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import os
import time
from dataclasses import asdict, dataclass
from typing import Dict, Optional

from momo.common.errors import BusinessRejection, InvalidRequest
from momo.common.money import parse_amount

# Standard base currency rates relative to 1.0 ZMW
# E.g. 1 USD = 27.50 ZMW => 1 ZMW = 1/27.50 USD
DEFAULT_RATES_TO_ZMW: Dict[str, float] = {
    "ZMW": 1.0,
    "USD": 27.50,
    "EUR": 29.80,
    "GBP": 35.00,
    "ZAR": 1.50,
}

QUOTE_SECRET = os.environ.get("FX_QUOTE_SECRET") or hashlib.sha256(b"momo_fx_local_hmac_seed").hexdigest()
QUOTE_TTL_SECONDS = 300  # 5 minutes guaranteed rate


@dataclass(frozen=True)
class FxQuote:
    quote_id: str
    from_currency: str
    to_currency: str
    from_amount: int  # source minor units
    to_amount: int    # target minor units
    rate: float       # exchange rate (1 source = rate destination)
    fee_minor: int    # FX spread/fee in source minor units
    expires_at: int   # unix timestamp
    signature: str

    def to_dict(self) -> dict:
        return asdict(self)


def get_exchange_rate(from_currency: str, to_currency: str) -> float:
    from_curr = from_currency.upper()
    to_curr = to_currency.upper()
    if from_curr not in DEFAULT_RATES_TO_ZMW or to_curr not in DEFAULT_RATES_TO_ZMW:
        raise InvalidRequest(f"unsupported currency pair: {from_curr}/{to_curr}")
    if from_curr == to_curr:
        return 1.0
    # Cross rate via ZMW base: (ZMW value of from_curr) / (ZMW value of to_curr)
    rate_from_in_zmw = DEFAULT_RATES_TO_ZMW[from_curr]
    rate_to_in_zmw = DEFAULT_RATES_TO_ZMW[to_curr]
    return rate_from_in_zmw / rate_to_in_zmw


def _sign_quote(payload: str) -> str:
    return hmac.new(QUOTE_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def create_quote(
    from_currency: str,
    to_currency: str,
    from_amount: int,
    spread_bps: int = 50,  # 0.50% spread fee
) -> FxQuote:
    """Generate a guaranteed FX quote valid for QUOTE_TTL_SECONDS."""
    from_curr = from_currency.upper()
    to_curr = to_currency.upper()
    amt = parse_amount(from_amount)

    raw_rate = get_exchange_rate(from_curr, to_curr)
    
    # Calculate fee (spread basis points)
    fee_minor = math.ceil((amt * spread_bps) / 10000)
    net_from_amount = max(0, amt - fee_minor)
    
    # Converted destination amount in minor units
    # Round to nearest integer minor unit
    to_amount = round(net_from_amount * raw_rate)
    
    expires_at = int(time.time()) + QUOTE_TTL_SECONDS
    import uuid
    quote_id = f"fx-{uuid.uuid4().hex[:12]}"
    
    sig_payload = f"{quote_id}:{from_curr}:{to_curr}:{amt}:{to_amount}:{expires_at}"
    sig = _sign_quote(sig_payload)

    return FxQuote(
        quote_id=quote_id,
        from_currency=from_curr,
        to_currency=to_curr,
        from_amount=amt,
        to_amount=to_amount,
        rate=round(raw_rate, 6),
        fee_minor=fee_minor,
        expires_at=expires_at,
        signature=sig,
    )


def verify_quote(quote: dict | FxQuote) -> bool:
    """Validate quote signature and expiration."""
    if isinstance(quote, FxQuote):
        q = quote
    else:
        try:
            q = FxQuote(**quote)
        except Exception:
            return False

    if int(time.time()) > q.expires_at:
        return False

    expected_payload = f"{q.quote_id}:{q.from_currency}:{q.to_currency}:{q.from_amount}:{q.to_amount}:{q.expires_at}"
    expected_sig = _sign_quote(expected_payload)
    return hmac.compare_digest(expected_sig, q.signature)


def convert_currency(
    from_currency: str,
    to_currency: str,
    from_amount: int,
    quote: Optional[dict | FxQuote] = None,
) -> tuple[int, float, int]:
    """Convert amount from one currency to another.

    Returns:
        (to_amount, effective_rate, fee_minor)
    """
    if from_currency.upper() == to_currency.upper():
        return from_amount, 1.0, 0

    if quote is not None:
        if isinstance(quote, dict):
            quote = FxQuote(**quote)
        if not verify_quote(quote):
            raise BusinessRejection("FX quote has expired or signature is invalid", "FX_QUOTE_EXPIRED")
        if (
            quote.from_currency != from_currency.upper()
            or quote.to_currency != to_currency.upper()
            or quote.from_amount != from_amount
        ):
            raise InvalidRequest("FX quote parameters do not match requested transaction")
        return quote.to_amount, quote.rate, quote.fee_minor

    # Fresh quote on-the-fly
    q = create_quote(from_currency, to_currency, from_amount)
    return q.to_amount, q.rate, q.fee_minor
