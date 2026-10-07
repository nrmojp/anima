from __future__ import annotations

import unittest
import asyncio
from types import SimpleNamespace
from datetime import datetime, timezone
from unittest.mock import patch
import httpx
from openai import APIConnectionError, APITimeoutError

from anima.core.external_errors import PermanentExternalError, TransientExternalError
from anima.adapters.openai.client import normalize_openai_errors
from anima.core.retry import classify_retry, retry_delay


class HttpFailure(Exception):
    def __init__(self, status: int, retry_after: str | None = None) -> None:
        super().__init__(f"HTTP {status}")
        self.status_code = status
        self.response = SimpleNamespace(
            headers={} if retry_after is None else {"Retry-After": retry_after}
        )


class RetryPolicyTests(unittest.TestCase):
    def test_connection_and_adapter_failures(self):
        for error, expected in (
            (ConnectionError(), 'connection_error'),
            (PermanentExternalError('login failed', reason='authentication'), 'authentication'),
            (TransientExternalError('gateway missing', reason='transport_temporary'), 'transport_temporary'),
        ):
            with self.subTest(error=type(error).__name__):
                decision = classify_retry(error, operation='discord.send')
                self.assertEqual(decision.reason, expected)
                self.assertEqual(decision.retryable, expected != 'authentication')

    def test_adapter_retry_after_is_preserved(self):
        decision = classify_retry(
            TransientExternalError('slow down', reason='rate_limited', retry_after=2.5),
            operation='delivery.send',
        )
        self.assertTrue(decision.retryable)
        self.assertEqual(decision.reason, 'rate_limited')
        self.assertEqual(decision.retry_after, 2.5)

    def test_retry_after_formats_and_invalid_headers(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        cases = [
            ({'retry_after': 3}, 3),
            ({'headers': object()}, None),
            ({'headers': {'Retry-After': 'invalid'}}, None),
            ({'headers': {'Retry-After': object()}}, None),
            ({'headers': {'Retry-After': '-2'}}, 0),
            ({'headers': {'Retry-After': 'Thu, 01 Jan 2026 00:00:10 GMT'}}, 10),
            ({'headers': {'Retry-After': 'Thu, 01 Jan 2026 00:00:10'}}, 10),
            ({'headers': {'Retry-After': 'Wed, 31 Dec 2025 23:59:59 GMT'}}, 0),
        ]
        with patch('anima.core.retry.datetime') as clock:
            clock.now.return_value = now
            for attributes, expected in cases:
                with self.subTest(attributes=attributes):
                    error = RuntimeError()
                    error.status_code = 429
                    for name, value in attributes.items():
                        setattr(error, name, value)
                    self.assertEqual(classify_retry(error, operation='test').retry_after, expected)

    def test_exponential_delay_without_server_hint(self):
        decision = classify_retry(TimeoutError(), operation='test')
        self.assertEqual(retry_delay(base_delay=0.5, attempt=3, decision=decision), 2)

    def test_timeout_rate_limit_and_server_errors_are_retryable(self) -> None:
        self.assertEqual(classify_retry(TimeoutError(), operation="openai.respond").reason, "timeout")
        limited = classify_retry(HttpFailure(429, "3.5"), operation="openai.respond")
        self.assertTrue(limited.retryable)
        self.assertEqual(limited.retry_after, 3.5)
        self.assertTrue(classify_retry(HttpFailure(503), operation="discord.send").retryable)

    def test_auth_input_permission_and_not_found_errors_are_not_retryable(self) -> None:
        for status in (400, 401, 403, 404, 422):
            with self.subTest(status=status):
                self.assertFalse(
                    classify_retry(HttpFailure(status), operation="openai.respond").retryable
                )

    def test_retry_after_wins_over_exponential_backoff(self) -> None:
        decision = classify_retry(HttpFailure(429, "4"), operation="openai.respond")
        self.assertEqual(retry_delay(base_delay=0.5, attempt=2, decision=decision), 4.0)

    def test_unknown_runtime_error_is_not_retried(self) -> None:
        decision = classify_retry(RuntimeError("bad request"), operation="discord.send")
        self.assertFalse(decision.retryable)
        self.assertEqual(decision.reason, "non_retryable")

    def test_openai_adapter_normalizes_connection_failures(self) -> None:
        request = httpx.Request("POST", "https://api.openai.test/responses")
        for source, reason in (
            (APITimeoutError(request=request), "timeout"),
            (APIConnectionError(request=request), "connection_error"),
        ):
            async def fail(error=source):
                raise error

            with self.subTest(reason=reason), self.assertRaises(TransientExternalError) as caught:
                asyncio.run(normalize_openai_errors(fail)())
            self.assertEqual(caught.exception.reason, reason)


if __name__ == "__main__":
    unittest.main()
