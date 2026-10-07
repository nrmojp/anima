"""Interfaces implemented by external adapters."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from anima.core.models import (
    Context,
    DigestJob,
    Event,
    ReflectionDraft,
    ReflectionJob,
    ResponseDraft,
    SentMessage,
    SleepDraft,
    SleepJob,
)

class Responder(Protocol):
    async def respond(self, context: Context) -> ResponseDraft: ...


class MessageSender(Protocol):
    async def send(
        self,
        source: Event,
        text: str,
        *,
        attachments: tuple[Path, ...] = (),
    ) -> SentMessage: ...

    async def find_reply(self, source: Event) -> SentMessage | None: ...


class TargetedNotification(Protocol):
    channel_id: str
    guild_id: str | None
    author_id: str
    message: str


@runtime_checkable
class MessageOutput(Protocol):
    async def send(self, destination_id: str, response: ResponseDraft) -> None: ...


@runtime_checkable
class ProactiveMessageSender(Protocol):
    async def send_proactive(self, source: Event, text: str) -> SentMessage: ...


@runtime_checkable
class ReactionSender(Protocol):
    async def react(self, source: Event, emoji: str) -> bool: ...


@runtime_checkable
class FacePreparer(Protocol):
    def prepare_face(self, text: str, face: str, mood_strength: str) -> str: ...


@runtime_checkable
class FacePresenter(Protocol):
    async def show_face(
        self,
        source: Event,
        sent: SentMessage,
        face: str,
        mood_strength: str,
        text: str,
    ) -> None: ...


class VoiceSink(Protocol):
    async def enqueue(self, source: Event, text: str, mood_strength: str) -> None: ...


class MemoryMaintainer(Protocol):
    async def digest(self, job: DigestJob) -> str: ...

    async def sleep(self, job: SleepJob) -> SleepDraft: ...

    async def reflect(self, job: ReflectionJob) -> ReflectionDraft: ...


class MemoryIndexSynchronizer(Protocol):
    async def ensure(self) -> str | None: ...
