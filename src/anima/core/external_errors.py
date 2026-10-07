"""Transport-neutral failures exposed by external adapters to the core."""

from __future__ import annotations


class ExternalOperationError(Exception):
    """Base error carrying retry metadata without an SDK dependency."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        reason: str,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.reason = reason
        self.retry_after = retry_after


class TransientExternalError(ExternalOperationError):
    def __init__(
        self, message: str, *, reason: str = "temporary", retry_after: float | None = None
    ) -> None:
        super().__init__(
            message, retryable=True, reason=reason, retry_after=retry_after
        )


class PermanentExternalError(ExternalOperationError):
    def __init__(self, message: str, *, reason: str = "permanent") -> None:
        super().__init__(message, retryable=False, reason=reason)
