"""Values passed between the domain core and its adapters."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
import re
from typing import Any, Literal
from urllib.parse import urlparse


SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
SAFE_CAPABILITY_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class OutcomeKind(str, Enum):
    DUPLICATE = "duplicate"
    IGNORED = "ignored"
    SPOKE = "spoke"


@dataclass(frozen=True, slots=True)
class ActionRecord:
    plugin: str
    action: str
    summary: str

    def __post_init__(self) -> None:
        if not SAFE_CAPABILITY_ID.fullmatch(self.plugin):
            raise ValueError("action plugin is invalid")
        if not SAFE_CAPABILITY_ID.fullmatch(self.action):
            raise ValueError("action name is invalid")
        _capability_line(self.summary, "action summary")

    def to_dict(self) -> dict[str, str]:
        return {"plugin": self.plugin, "action": self.action, "summary": self.summary}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ActionRecord:
        return cls(str(value["plugin"]), str(value["action"]), str(value["summary"]))


@dataclass(frozen=True, slots=True)
class ContextReference:
    plugin: str
    kind: str
    summary: str
    attributes: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not SAFE_CAPABILITY_ID.fullmatch(self.plugin):
            raise ValueError("reference plugin is invalid")
        if not SAFE_CAPABILITY_ID.fullmatch(self.kind):
            raise ValueError("reference kind is invalid")
        _capability_line(self.summary, "reference summary")
        if len(self.attributes) > 12:
            raise ValueError("reference has too many attributes")
        keys: set[str] = set()
        for key, value in self.attributes:
            if (
                not key or len(key) > 64 or "\n" in key or "\r" in key
                or key in keys
            ):
                raise ValueError("reference attribute key is invalid")
            keys.add(key)
            if len(value) > 500 or "\n" in value or "\r" in value:
                raise ValueError("reference attribute value is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "plugin": self.plugin,
            "kind": self.kind,
            "summary": self.summary,
            "attributes": {key: value for key, value in self.attributes},
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ContextReference:
        attributes = value.get("attributes", {})
        if not isinstance(attributes, Mapping):
            raise ValueError("reference attributes must be an object")
        return cls(
            str(value["plugin"]), str(value["kind"]), str(value["summary"]),
            tuple((str(key), str(item)) for key, item in attributes.items()),
        )


def _capability_line(value: str, label: str) -> None:
    if not value or len(value) > 600 or "\n" in value or "\r" in value:
        raise ValueError(f"{label} must be a non-empty single line of at most 600 characters")


@dataclass(frozen=True, slots=True)
class Attachment:
    url: str
    content_type: str | None = None
    cache_name: str | None = None

    def __post_init__(self) -> None:
        if self.cache_name is not None and not re.fullmatch(
            r"[A-Za-z0-9_-]+\.[A-Za-z0-9]+", self.cache_name
        ):
            raise ValueError("attachment cache_name contains unsafe characters")

    def to_dict(self) -> dict[str, Any]:
        value = {"url": self.url, "content_type": self.content_type}
        if self.cache_name is not None:
            value["cache_name"] = self.cache_name
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Attachment:
        return cls(
            url=str(value["url"]),
            content_type=value.get("content_type"),
            cache_name=value.get("cache_name"),
        )


@dataclass(frozen=True, slots=True)
class ResearchSource:
    title: str
    url: str

    def __post_init__(self) -> None:
        if not self.title.strip() or len(self.title) > 200:
            raise ValueError("research source title must be 1-200 characters")
        parsed = urlparse(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or len(self.url) > 500:
            raise ValueError("research source must be a valid HTTP(S) URL of at most 500 characters")

    def to_dict(self) -> dict[str, str]:
        return {"title": self.title, "url": self.url}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ResearchSource:
        return cls(title=str(value["title"]), url=str(value["url"]))


@dataclass(frozen=True, slots=True)
class ResearchNote:
    summary: str
    queries: tuple[str, ...] = ()
    sources: tuple[ResearchSource, ...] = ()

    def __post_init__(self) -> None:
        if not self.summary.strip() or len(self.summary) > 600:
            raise ValueError("research summary must be 1-600 characters")
        if len(self.queries) > 3 or any(not item.strip() or len(item) > 200 for item in self.queries):
            raise ValueError("research queries must contain at most three non-empty items")
        if len(self.sources) > 3:
            raise ValueError("research sources must contain at most three items")

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "queries": list(self.queries),
            "sources": [source.to_dict() for source in self.sources],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ResearchNote:
        return cls(
            summary=str(value["summary"]),
            queries=tuple(str(item) for item in value.get("queries", [])),
            sources=tuple(ResearchSource.from_dict(item) for item in value.get("sources", [])),
        )


@dataclass(frozen=True, slots=True)
class MusicReference:
    id: str
    title: str
    duration_sec: int
    styles: tuple[str, ...]
    source_url: str

    def __post_init__(self) -> None:
        if not SAFE_ID.fullmatch(self.id):
            raise ValueError("music reference id contains unsafe characters")
        if not self.title.strip() or len(self.title) > 200:
            raise ValueError("music reference title must be 1-200 characters")
        parsed = urlparse(self.source_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("music reference source_url must be HTTP(S)")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "duration_sec": self.duration_sec,
            "styles": list(self.styles),
            "source_url": self.source_url,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> MusicReference:
        return cls(
            id=str(value["id"]),
            title=str(value["title"]),
            duration_sec=int(value["duration_sec"]),
            styles=tuple(str(item) for item in value.get("styles", [])),
            source_url=str(value["source_url"]),
        )


@dataclass(frozen=True, slots=True)
class MentionedPerson:
    """A Discord member named in an event, with a stable identity."""

    id: str
    name: str

    def __post_init__(self) -> None:
        if not SAFE_ID.fullmatch(self.id):
            raise ValueError("mentioned person id contains unsafe characters")
        if not self.name.strip() or "\n" in self.name or "\r" in self.name:
            raise ValueError("mentioned person name must be a non-empty single line")

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "name": self.name}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> MentionedPerson:
        return cls(id=str(value["id"]), name=str(value["name"]))


@dataclass(frozen=True, slots=True)
class Event:
    id: str
    ts: datetime
    kind: Literal["channel", "dm"]
    channel_id: str
    channel_name: str
    author_id: str
    author_name: str
    text: str
    guild_id: str | None = None
    author_voice_channel_id: str | None = None
    mention: bool = False
    called_name: bool = False
    reply_to: str | None = None
    reply_to_self: bool = False
    reply_author_name: str | None = None
    reply_text: str | None = None
    attachments: tuple[Attachment, ...] = ()
    mentioned_people: tuple[MentionedPerson, ...] = ()
    acts: tuple[str, ...] = ()
    sandbox_key: str | None = None
    react: str | None = None
    research: ResearchNote | None = None
    music: tuple[MusicReference, ...] = ()
    actions: tuple[ActionRecord, ...] = ()
    references: tuple[ContextReference, ...] = ()
    author_is_bot: bool = False
    response_target: MentionedPerson | None = None
    response_target_is_bot: bool = False
    response_to: str | None = None

    def __post_init__(self) -> None:
        if self.response_to is not None and (not isinstance(self.response_to, str) or not SAFE_ID.fullmatch(self.response_to)):
            raise ValueError("event.response_to contains unsafe characters")
        if self.ts.tzinfo is None or self.ts.utcoffset() is None:
            raise ValueError("event.ts must include a timezone")
        for name, value in (("id", self.id), ("channel_id", self.channel_id), ("author_id", self.author_id)):
            if not SAFE_ID.fullmatch(value):
                raise ValueError(f"event.{name} contains unsafe characters")
        if self.sandbox_key is not None and not re.fullmatch(
            r"(?:guild|dm):[0-9]+", self.sandbox_key
        ):
            raise ValueError("event.sandbox_key is invalid")

    @property
    def response_person_id(self) -> str:
        return self.response_target.id if self.response_target else self.author_id

    @property
    def conversation_id(self) -> str:
        """Transport-neutral identity of the stream containing this event."""
        return self.channel_id

    @property
    def is_private(self) -> bool:
        return self.kind == "dm"

    @property
    def directed_to_agent(self) -> bool:
        # Name occurrence is a routing hint, not proof that the speaker addressed us.
        return self.is_private or self.mention or self.reply_to_self

    @property
    def requires_immediate_response(self) -> bool:
        return self.is_notification or (self.directed_to_agent and not self.author_is_bot)

    @property
    def is_notification(self) -> bool:
        """Only internal service events can carry an explicit response target."""
        return self.author_id == "self" and self.response_target is not None

    @property
    def audio_destination_id(self) -> str | None:
        """Optional transport-provided destination for voice or music output."""
        return self.author_voice_channel_id

    @property
    def addressed_to_self(self) -> bool:
        """Compatibility alias for the v1 event schema."""
        return self.directed_to_agent

    def to_log_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "id": self.id,
            "ts": self.ts.isoformat(),
            "kind": self.kind,
            "ch": self.channel_name,
            "ch_id": self.channel_id,
            "who": self.author_name,
            "who_id": self.author_id,
            "text": self.text,
            "guild_id": self.guild_id,
            "sandbox_key": self.sandbox_key,
            "author_voice_channel_id": self.author_voice_channel_id,
            "mention": self.mention,
            "called_name": self.called_name,
            "reply_to": self.reply_to,
            "reply_to_self": self.reply_to_self,
            "reply_author_name": self.reply_author_name,
            "reply_text": self.reply_text,
            "author_bot": self.author_is_bot,
            "files": [attachment.to_dict() for attachment in self.attachments],
            "mentioned_people": [person.to_dict() for person in self.mentioned_people],
        }
        if self.acts:
            value["acts"] = list(self.acts)
        if self.response_to is not None:
            value["response_to"] = self.response_to
        if self.react is not None:
            value["react"] = self.react
        if self.research is not None:
            value["research"] = self.research.to_dict()
        if self.music:
            value["music"] = [reference.to_dict() for reference in self.music]
        if self.actions:
            value["actions"] = [action.to_dict() for action in self.actions]
        if self.references:
            value["references"] = [reference.to_dict() for reference in self.references]
        if self.response_target is not None:
            value["response_target"] = self.response_target.to_dict()
            value["response_target_is_bot"] = self.response_target_is_bot
        return value

    @classmethod
    def from_log_dict(cls, value: dict[str, Any]) -> Event:
        return cls(
            id=str(value["id"]),
            ts=datetime.fromisoformat(value["ts"]),
            kind=value["kind"],
            channel_id=str(value.get("ch_id", value["ch"])),
            channel_name=str(value["ch"]),
            author_id=str(value["who_id"]),
            author_name=str(value["who"]),
            text=str(value["text"]),
            guild_id=value.get("guild_id"),
            sandbox_key=value.get("sandbox_key"),
            react=value.get("react"),
            author_voice_channel_id=value.get("author_voice_channel_id"),
            mention=bool(value.get("mention", False)),
            called_name=bool(value.get("called_name", False)),
            reply_to=value.get("reply_to"),
            response_to=value.get("response_to"),
            reply_to_self=bool(value.get("reply_to_self", False)),
            reply_author_name=value.get("reply_author_name"),
            reply_text=value.get("reply_text"),
            author_is_bot=bool(value.get("author_bot", False)),
            response_target=(MentionedPerson.from_dict(value["response_target"])
                             if value.get("response_target") is not None else None),
            response_target_is_bot=bool(value.get("response_target_is_bot", False)),
            attachments=tuple(Attachment.from_dict(item) for item in value.get("files", [])),
            mentioned_people=tuple(
                MentionedPerson.from_dict(item)
                for item in value.get("mentioned_people", [])
            ),
            acts=tuple(value.get("acts", [])),
            research=(
                ResearchNote.from_dict(value["research"])
                if value.get("research") is not None
                else None
            ),
            music=cls._music_from_log(value.get("music")),
            actions=tuple(ActionRecord.from_dict(item) for item in value.get("actions", [])),
            references=tuple(
                ContextReference.from_dict(item) for item in value.get("references", [])
            ),
        )

    @staticmethod
    def _music_from_log(value: Any) -> tuple[MusicReference, ...]:
        if value is None:
            return ()
        values = value if isinstance(value, list) else [value]
        return tuple(MusicReference.from_dict(item) for item in values)


@dataclass(frozen=True, slots=True)
class Mood:
    state: str
    cause: str
    strength: Literal["弱い", "ふつう", "強い"]
    focus: str
    since: datetime

    def __post_init__(self) -> None:
        if self.since.tzinfo is None or self.since.utcoffset() is None:
            raise ValueError("mood.since must include a timezone")
        if self.strength not in {"弱い", "ふつう", "強い"}:
            raise ValueError("mood.strength must be 弱い, ふつう, or 強い")
        if len(self.state) > 40:
            raise ValueError("mood.state must be at most 40 characters")
        if len(self.cause) > 40:
            raise ValueError("mood.cause must be at most 40 characters")
        if len(self.focus) > 60:
            raise ValueError("mood.focus must be at most 60 characters")

    def to_dict(self) -> dict[str, str]:
        return {
            "state": self.state,
            "cause": self.cause,
            "strength": self.strength,
            "focus": self.focus,
            "since": self.since.isoformat(),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Mood:
        return cls(
            state=str(value["state"]),
            cause=str(value["cause"]),
            strength=value["strength"],
            focus=str(value["focus"]),
            since=datetime.fromisoformat(value["since"]),
        )


@dataclass(frozen=True, slots=True)
class Context:
    instructions: str
    input: tuple[dict[str, Any], ...]
    state_version: int
    source_event: Event | None = None
    response_delay_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class ResponseDraft:
    reply: str
    mood_state: str
    mood_cause: str
    mood_strength: Literal["弱い", "ふつう", "強い"]
    mood_focus: str
    acts: tuple[str, ...] = ()
    response_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    face: str = "ふつう"
    research: ResearchNote | None = None
    music: tuple[MusicReference, ...] = ()
    images: tuple[Path, ...] = ()
    actions: tuple[ActionRecord, ...] = ()
    references: tuple[ContextReference, ...] = ()


@dataclass(frozen=True, slots=True)
class SentMessage:
    id: str
    timestamp: datetime
    text: str | None = None


@dataclass(frozen=True, slots=True)
class ProcessOutcome:
    kind: OutcomeKind
    source_event_id: str
    reply: str | None = None
    sent_message_id: str | None = None


@dataclass(frozen=True, slots=True)
class StateSnapshot:
    version: int
    persona: str
    rules: str
    habitus: str
    mood: Mood
    open_items: str
    digest: str
    self_memory: str
    world_memory: str
    channel_memory: str
    people_memory: tuple[str, ...] = field(default_factory=tuple)
    recent_events: tuple[Event, ...] = field(default_factory=tuple)
    last_spoke_at: datetime | None = None
    current_kind: Literal["channel", "dm"] = "channel"
    current_author_id: str = ""
    current_author_name: str = ""
    current_author_is_bot: bool = False
    current_author_memory: str = ""
    current_author_last_seen_at: datetime | None = None
    backfill_count: int = 0
    backfill_truncated: bool = False


@dataclass(frozen=True, slots=True)
class DigestJob:
    expected_version: int
    events: tuple[Event, ...]
    existing_digest: str
    sandbox_key: str | None = None


@dataclass(frozen=True, slots=True)
class SleepJob:
    expected_version: int
    events: tuple[Event, ...]
    digest: str
    open_items: str
    mood: Mood
    persona: str
    rules: str
    habitus: str
    self_memory: str
    world_memory: str
    channel_memories: tuple[tuple[str, str], ...]
    people_memories: tuple[tuple[str, str], ...]
    sandbox_key: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryDocument:
    scope: Literal["self", "world", "channel", "person"]
    key: str
    content: str

    def __post_init__(self) -> None:
        if self.scope in {"channel", "person"} and not SAFE_ID.fullmatch(self.key):
            raise ValueError("memory document key contains unsafe characters")
        if self.scope in {"self", "world"} and self.key != self.scope:
            raise ValueError("self/world memory key must match its scope")


@dataclass(frozen=True, slots=True)
class SleepDraft:
    memories: tuple[MemoryDocument, ...]
    open_items: str
    mood_state: str
    mood_cause: str
    mood_strength: Literal["弱い", "ふつう", "強い"]
    mood_focus: str


@dataclass(frozen=True, slots=True)
class ReflectionJob:
    expected_version: int
    persona: str
    habitus: str
    self_memory: str
    sandbox_key: str | None = None


@dataclass(frozen=True, slots=True)
class ReflectionDraft:
    changed: bool
    habitus: str
    conflict: str | None = None
