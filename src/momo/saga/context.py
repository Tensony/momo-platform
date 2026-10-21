"""The saga context: the JSON document passed between steps (and through Step Functions)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

TXN_TYPES = {"TRANSFER", "MERCHANT_PAYMENT", "CASH_IN", "CASH_OUT", "FLOAT_TOPUP", "REVERSAL"}


@dataclass
class TxnContext:
    txn_id: str
    type: str
    from_wallet: str
    to_wallet: str
    amount: int
    currency: str = "ZMW"
    geo_lat: float | None = None
    geo_lon: float | None = None
    correlation_id: str | None = None
    created_by: str | None = None
    reverses_txn_id: str | None = None
    reason: str | None = None
    fault: str | None = None  # fault-injection point; honoured only if ENABLE_FAULT_INJECTION=true

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TxnContext":
        known = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        return cls(**known)


@dataclass
class SagaResult:
    txn_id: str
    outcome: str  # COMPLETED | REJECTED | FAILED
    code: str | None = None
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)
