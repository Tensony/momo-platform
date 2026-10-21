"""Money helpers. All amounts are integer minor units (1 ZMW = 100 ngwee). Floats are rejected."""

from __future__ import annotations

from .errors import InvalidRequest

MAX_AMOUNT = 1_000_000_000_00  # 1 billion ZMW in ngwee - sanity cap, far above any real transfer
SUPPORTED_CURRENCIES = frozenset({"ZMW", "USD", "EUR", "GBP", "ZAR"})


def parse_amount(value) -> int:
    """Validate an amount from untrusted input. Returns int minor units."""
    # bool is a subclass of int - reject it explicitly
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidRequest("amount must be an integer number of minor units (e.g. ngwee)")
    if value <= 0:
        raise InvalidRequest("amount must be greater than zero")
    if value > MAX_AMOUNT:
        raise InvalidRequest("amount exceeds the maximum allowed")
    return value


def parse_currency(value) -> str:
    if value is None:
        return "ZMW"
    if not isinstance(value, str) or value.upper() not in SUPPORTED_CURRENCIES:
        raise InvalidRequest(f"unsupported currency; supported: {sorted(SUPPORTED_CURRENCIES)}")
    return value.upper()


def format_minor(amount: int, currency: str = "ZMW") -> str:
    sign = "-" if amount < 0 else ""
    major, minor = divmod(abs(amount), 100)
    return f"{sign}{currency} {major:,}.{minor:02d}"
