"""Structured application errors.

Every failure raised by the service layer is an :class:`EmberError`.  The API
layer converts these into the documented structured error envelope::

    {"error": {"code": "...", "message": "...", "retryable": false}}
"""

from __future__ import annotations

from typing import Any


class EmberError(Exception):
    """Base class for all application errors."""

    status_code: int = 400
    code: str = "BAD_REQUEST"
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        retryable: bool | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        if retryable is not None:
            self.retryable = retryable
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.details:
            payload["details"] = self.details
        return {"error": payload}


class ValidationError(EmberError):
    status_code = 422
    code = "VALIDATION_FAILED"


class AuthenticationError(EmberError):
    status_code = 401
    code = "NOT_AUTHENTICATED"


class SessionExpiredError(AuthenticationError):
    code = "SESSION_EXPIRED"


class PermissionDeniedError(EmberError):
    status_code = 403
    code = "PERMISSION_DENIED"


class NotFoundError(EmberError):
    status_code = 404
    code = "NOT_FOUND"


class ConflictError(EmberError):
    status_code = 409
    code = "CONFLICT"


class RateLimitedError(EmberError):
    status_code = 429
    code = "RATE_LIMITED"
    retryable = True


class InvalidStateTransitionError(EmberError):
    status_code = 409
    code = "INVALID_STATE_TRANSITION"


class PersistenceError(EmberError):
    """Raised when the database refuses a write we expected to succeed."""

    status_code = 500
    code = "PERSISTENCE_FAILED"
    retryable = True


class ExternalServiceError(EmberError):
    status_code = 502
    code = "EXTERNAL_SERVICE_FAILED"
    retryable = True
