"""Discord-shaped local adapter without a Discord Gateway connection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import mimetypes
import os
from pathlib import Path
import re
from typing import Callable
from uuid import uuid4

from anima.core.inventory import MAX_ARTIFACT_BYTES
from anima.core.models import Attachment, Event, SentMessage
from anima.core.sandbox import SandboxKey


@dataclass(frozen=True, slots=True)
class StubDelivery:
    id: str
    source_event_id: str
    text: str
    attachments: tuple[Path, ...]
    kind: str = "reply"

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "source_event_id": self.source_event_id,
            "text": self.text,
            "attachments": [str(path) for path in self.attachments],
            "kind": self.kind,
        }


class StubDiscordSender:
    """Capture Discord output while implementing the normal outbound ports."""

    def __init__(
        self, *, clock: Callable[[], datetime],
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.clock = clock
        self.id_factory = id_factory or (lambda: "stub-" + uuid4().hex)
        self.deliveries: list[StubDelivery] = []
        self.reactions: list[tuple[str, str]] = []
        self.faces: list[tuple[str, str, str]] = []
        self._by_source: dict[str, SentMessage] = {}

    async def send(self, source: Event, text: str, *, attachments=()) -> SentMessage:
        return self._record(source, text, tuple(attachments), "reply")

    async def send_background(
        self, source: Event, text: str, *, attachments=()
    ) -> SentMessage:
        return self._record(source, text, tuple(attachments), "background")

    async def send_proactive(self, source: Event, text: str) -> SentMessage:
        return self._record(source, text, (), "proactive")

    async def send_reminder(self, reminder) -> None:
        identifier = self.id_factory()
        source_id = str(getattr(reminder, "id", "reminder"))
        text = f"<@{reminder.author_id}> リマインダー：{reminder.message}"
        self.deliveries.append(
            StubDelivery(identifier, source_id, text, (), "reminder")
        )

    async def find_reply(self, source: Event) -> SentMessage | None:
        return self._by_source.get(source.id)

    async def react(self, source: Event, emoji: str) -> bool:
        item = (source.id, emoji)
        if item not in self.reactions:
            self.reactions.append(item)
        return True

    def prepare_face(self, text: str, face: str, strength: str) -> str:
        del face, strength
        return text

    async def show_face(
        self, source: Event, sent: SentMessage, face: str, strength: str, text: str
    ) -> None:
        del sent, text
        self.faces.append((source.id, face, strength))

    def _record(
        self, source: Event, text: str, attachments: tuple[Path, ...], kind: str
    ) -> SentMessage:
        identifier = self.id_factory()
        sent = SentMessage(identifier, self.clock(), text)
        self.deliveries.append(
            StubDelivery(identifier, source.id, text, attachments, kind)
        )
        self._by_source[source.id] = sent
        return sent


class StubDiscordAdapter:
    """Translate local input into the same Event shape as Discord messages."""

    def __init__(
        self, router, state_root: Path, *, clock: Callable[[], datetime],
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.router = router
        self.state_root = state_root
        self.clock = clock
        self.id_factory = id_factory or (lambda: "stub-" + uuid4().hex)

    async def submit(
        self,
        text: str,
        *,
        sandbox: SandboxKey,
        attachments: tuple[Path, ...] = (),
        channel_id: str = "1",
        channel_name: str = "stub-lab",
        author_id: str = "1",
        author_name: str = "Tester",
    ):
        if sandbox.kind != "guild":
            raise ValueError("Discord stub currently requires a guild sandbox")
        event_id = self.id_factory()
        event = Event(
            id=event_id,
            ts=self.clock(),
            kind="channel",
            channel_id=channel_id,
            channel_name=channel_name,
            author_id=author_id,
            author_name=author_name,
            text=text,
            guild_id=sandbox.id,
            mention=True,
            called_name=True,
            attachments=self._cache_attachments(sandbox, event_id, attachments),
            sandbox_key=str(sandbox),
        )
        outcome = await self.router.submit(event, allow_reactions=False)
        return event, outcome

    def _cache_attachments(
        self, sandbox: SandboxKey, event_id: str, sources: tuple[Path, ...]
    ) -> tuple[Attachment, ...]:
        directory = sandbox.path(self.state_root) / "attachments"
        result = []
        for index, source in enumerate(sources):
            if source.is_symlink() or not source.is_file():
                raise ValueError(f"stub attachment is not a regular file: {source}")
            size = source.stat().st_size
            if not size or size > MAX_ARTIFACT_BYTES:
                raise ValueError(f"stub attachment has an invalid size: {source}")
            suffix = source.suffix.lower()
            if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
                suffix = ".bin"
            cache_name = f"{event_id}-{index}{suffix}"
            target = directory / cache_name
            directory.mkdir(parents=True, exist_ok=True)
            temporary = directory / f".{cache_name}.tmp"
            temporary.write_bytes(source.read_bytes())
            os.replace(temporary, target)
            content_type = mimetypes.guess_type(source.name)[0]
            result.append(Attachment(f"stub://{cache_name}", content_type, cache_name))
        return tuple(result)
