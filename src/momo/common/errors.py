"""Domain errors. `code` is the stable machine-readable identifier returned to API clients."""

from __future__ import annotations


class MomoError(Exception):
    code = "INTERNAL_ERROR"
    http_status = 500

    def __init__(self, message: str | None = None, **details):
        super().__init__(message or self.code)
        self.message = message or self.code
        self.details = details


class BusinessRejection(MomoError):
    """The request is valid but cannot be honoured. No money moved; do not retry unchanged."""

    http_status = 422


class InsufficientFunds(BusinessRejection):
    code = "INSUFFICIENT_FUNDS"


class WalletNotFound(BusinessRejection):
    code = "WALLET_NOT_FOUND"
    http_status = 404


class WalletNotActive(BusinessRejection):
    code = "WALLET_NOT_ACTIVE"


class CurrencyMismatch(BusinessRejection):
    code = "CURRENCY_MISMATCH"


class FraudDeclined(BusinessRejection):
    code = "FRAUD_DECLINED"


class InvalidRequest(MomoError):
    code = "INVALID_REQUEST"
    http_status = 400


class NotAuthenticated(MomoError):
    code = "NOT_AUTHENTICATED"
    http_status = 401


class NotAuthorized(MomoError):
    code = "NOT_AUTHORIZED"
    http_status = 403


class NotFound(MomoError):
    code = "NOT_FOUND"
    http_status = 404


class IdempotencyKeyReused(MomoError):
    """Same Idempotency-Key used with a different request body."""

    code = "IDEMPOTENCY_KEY_REUSED"
    http_status = 422


class RequestInProgress(MomoError):
    """The original request with this Idempotency-Key is still running."""

    code = "REQUEST_IN_PROGRESS"
    http_status = 409


class AlreadyReversed(BusinessRejection):
    code = "ALREADY_REVERSED"
    http_status = 409


class NotReversible(BusinessRejection):
    code = "NOT_REVERSIBLE"
    http_status = 409


class CompensationOrderError(MomoError):
    """A compensation was attempted out of order (programming error / corrupted saga)."""

    code = "COMPENSATION_ORDER"


class InjectedFault(MomoError):
    """Raised by the fault-injection hooks used in the correctness proofs. Never enabled in prod."""

    code = "INJECTED_FAULT"
