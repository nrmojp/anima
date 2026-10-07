"""Sandbox-bound ingress for interface and background events."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from anima.core.models import Event, ProcessOutcome
from anima.core.ports import ResponseDraft
from anima.core.sandbox import SandboxKey


@runtime_checkable
class EventIngress(Protocol):
    """Submit an Event to the actor that owns one Sandbox."""

    async def publish(self, event: Event) -> ResponseDraft | ProcessOutcome: ...


class SandboxEventMailbox:
    """Serialize live and background events through one bound actor handler."""

    def __init__(self, sandbox_key: SandboxKey) -> None:
        self.sandbox_key = sandbox_key
        self._handler: Callable[[Event], Awaitable[ResponseDraft | ProcessOutcome]] | None = None
        self._lock = asyncio.Lock()

    def bind(self, handler: Callable[[Event], Awaitable[ResponseDraft | ProcessOutcome]]) -> None:
        if not callable(handler):
            raise TypeError("event handler must be callable")
        if self._handler is not None:
            raise RuntimeError("event mailbox is already bound")
        self._handler = handler

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        self.unbind()

    def unbind(self) -> None:
        self._handler = None

    async def publish(self, event: Event) -> ResponseDraft | ProcessOutcome:
        if event.sandbox_key != str(self.sandbox_key):
            raise ValueError("event mailbox sandbox does not match event")
        if self._handler is None:
            raise RuntimeError("event mailbox is not bound")
        async with self._lock:
            result = await self._handler(event)
        if not isinstance(result, (ResponseDraft, ProcessOutcome)):
            raise TypeError("event handler must return ResponseDraft or ProcessOutcome")
        return result
