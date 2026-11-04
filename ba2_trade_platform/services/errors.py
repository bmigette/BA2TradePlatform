"""Typed errors of the service layer. The API maps ``status_code`` / ``code`` onto the JSON envelope
``{"error": code, "message": ..., "detail": {...}}``."""
from typing import Any, Dict, Optional


class ServiceError(Exception):
    status_code: int = 500
    code: str = "internal"

    def __init__(self, message: str, detail: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.detail: Dict[str, Any] = {} if detail is None else detail


class NotFound(ServiceError):
    status_code, code = 404, "not_found"


class Conflict(ServiceError):
    status_code, code = 409, "conflict"


class Invalid(ServiceError):
    status_code, code = 422, "invalid"


class BrokerRefused(ServiceError):
    """The broker answered and said no. ``message`` is the broker's own text."""
    status_code, code = 502, "broker_refused"


class BrokerUnreachable(ServiceError):
    status_code, code = 503, "broker_unreachable"


class Unmeasurable(ServiceError):
    """A figure the request asks for cannot be measured. The request fails; it never reads as 0 or null."""
    status_code, code = 503, "unmeasurable"


class PriceSourceUnreachable(ServiceError):
    """The historical price source (not a broker) did not answer within the request's deadline."""
    status_code, code = 503, "price_source_unreachable"
