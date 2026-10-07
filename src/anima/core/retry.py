"""Retry classification for external API operations."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from anima.core.external_errors import ExternalOperationError


@dataclass(frozen=True, slots=True)
class RetryDecision:
    retryable: bool
    reason: str
    retry_after: float | None = None


def classify_retry(error: Exception, *, operation: str) -> RetryDecision:
    """Return whether an external-operation failure is safe to retry."""
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        return RetryDecision(True, "timeout")

    if isinstance(error, ExternalOperationError):
        return RetryDecision(error.retryable, error.reason, error.retry_after)

    status = _status_code(error)
    retry_after = _retry_after(error)
    if status == 429:
        return RetryDecision(True, "rate_limited", retry_after)
    if status is not None and 500 <= status <= 599:
        return RetryDecision(True, "server_error", retry_after)
    if status is not None:
        return RetryDecision(False, f"http_{status}")

    if isinstance(error, (ConnectionError, OSError)):
        return RetryDecision(True, "connection_error")
    return RetryDecision(False, "non_retryable")


def retry_delay(*, base_delay: float, attempt: int, decision: RetryDecision) -> float:
    exponential = base_delay * (2 ** (attempt - 1))
    if decision.retry_after is None:
        return exponential
    return max(exponential, decision.retry_after)


def _status_code(error: Exception) -> int | None:
    for value in (
        getattr(error, "status_code", None),
        getattr(error, "status", None),
        getattr(getattr(error, "response", None), "status_code", None),
        getattr(getattr(error, "response", None), "status", None),
    ):
        if isinstance(value, int):
            return value
    return None


def _retry_after(error: Exception) -> float | None:
    direct = getattr(error, "retry_after", None)
    parsed = _seconds(direct)
    if parsed is not None:
        return parsed
    response = getattr(error, "response", None)
    headers: Any = getattr(response, "headers", None) or getattr(error, "headers", None)
    if headers is None:
        return None
    try:
        value = headers.get("retry-after") or headers.get("Retry-After")
    except AttributeError:
        return None
    parsed = _seconds(value)
    if parsed is not None:
        return parsed
    if isinstance(value, str):
        try:
            moment = parsedate_to_datetime(value)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            return max(0.0, (moment - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None
    return None


def _seconds(value: object) -> float | None:
    try:
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return max(0.0, seconds)
