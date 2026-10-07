"""Filesystem-backed journal and persona state."""

from __future__ import annotations

import json
import gzip
import hashlib
import os
import re
import shutil
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from anima.core.sandbox import SandboxKey, reject_symlinks
from anima.core.telemetry import emit

from anima.core.models import (
    DigestJob,
    Event,
    Mood,
    ReflectionDraft,
    ReflectionJob,
    ResponseDraft,
    SentMessage,
    SleepDraft,
    SleepJob,
    StateSnapshot,
    SAFE_ID,
)
from anima.core.self_time import SelfTimeDecision
from anima.core.integrity import validate_state, recover_jsonl_tail, require_space


INITIAL_MOOD = {
    "state": "穏やか",
    "cause": "特にない",
    "strength": "弱い",
    "focus": "",
    "since": "1970-01-01T00:00:00+00:00",
}
INITIAL_CURSOR = {
    "version": 0,
    "digested_until": None,
    "slept_at": None,
    "reflected_at": None,
    "last_spoke_at": None,
}
HABITUS_MAX_LINES = 10
HABITUS_DIFFERENCE_MARKERS = ("前より", "以前は", "ようになった")


class ConcurrentStateChange(RuntimeError):
    """The state changed after a context was built."""


class InvalidMaintenanceDraft(ValueError):
    """Generated maintenance content violates the durable file format."""


DIGEST_LINE = re.compile(r"^- \[\d{2}:\d{2} [^\]]+\] .+")
MEMORY_LINE = re.compile(r"^- \[\d{4}-\d{2}-\d{2} (?:DM|#[^\] ]+)(?: [^\]]+)?\] .+")
OPEN_LINE = re.compile(r"^- \[\d{4}-\d{2}-\d{2} (?:DM|#[^\] ]+)\] .+")


class FileStateStore:
    def __init__(
        self,
        root: Path,
        *,
        recent_limit: int = 30,
        digest_max_lines: int = 30,
        memory_max_lines: int = 80,
        memory_strong_max: int = 10,
        log_retention_days: int = 30,
        archive_retention_days: int = 365,
        resource_root: Path | None = None,
        legacy_root: Path | None = None,
        sandbox_key: SandboxKey | None = None,
    ) -> None:
        self.root = root
        self._sandbox_anchor = root.parent.parent.parent if sandbox_key is not None else root
        self.sandbox_key = sandbox_key
        self.resource_root = resource_root or root
        self.legacy_root = legacy_root
        self.recent_limit = recent_limit
        self.digest_max_lines = digest_max_lines
        self.memory_max_lines = memory_max_lines
        self.memory_strong_max = memory_strong_max
        self.log_retention_days = log_retention_days
        self.archive_retention_days = archive_retention_days

    def ensure_layout(self, *, now: datetime) -> None:
        self._check_tree()
        self._migrate_legacy_layout()
        (self.root / "memory" / "channels").mkdir(parents=True, exist_ok=True)
        (self.root / "memory" / "people").mkdir(parents=True, exist_ok=True)
        (self.root / "log").mkdir(parents=True, exist_ok=True)
        (self.root / "world").mkdir(parents=True, exist_ok=True)
        (self.root / "handled").mkdir(parents=True, exist_ok=True)
        (self.root / "deleted").mkdir(parents=True, exist_ok=True)
        self._recover_transaction()
        for name in ("cursor.json", "mood.md"):
            validate_state(self.root / name, self.root)
        for path in sorted((self.root / "log").glob("*/*.jsonl")):
            recover_jsonl_tail(path, self.root)
        defaults: dict[str, str] = {
            "habitus.md": "",
            "open.md": "",
            "digest.md": "",
            "memory/self.md": "",
            "memory/world.md": "",
        }
        for relative, content in defaults.items():
            path = self.root / relative
            if not path.exists():
                self._atomic_write_text(path, content)
        if not (self.root / "mood.md").exists():
            initial_mood = {**INITIAL_MOOD, "since": now.isoformat()}
            self._atomic_write_json(self.root / "mood.md", initial_mood)
        if not (self.root / "cursor.json").exists():
            self._atomic_write_json(
                self.root / "cursor.json",
                {
                    **INITIAL_CURSOR,
                    "slept_at": now.isoformat(),
                    "reflected_at": now.isoformat(),
                },
            )
        else:
            cursor = self._read_json(self.root / "cursor.json", INITIAL_CURSOR)
            changed = False
            if not cursor.get("slept_at"):
                cursor["slept_at"] = now.isoformat()
                changed = True
            if not cursor.get("reflected_at"):
                cursor["reflected_at"] = now.isoformat()
                changed = True
            if changed:
                self._atomic_write_json(self.root / "cursor.json", cursor)
        self._initialize_inbox_tracking()
        self.archive_old_logs(now=now)

    def _migrate_legacy_layout(self) -> None:
        legacy = self.legacy_root
        if legacy is None or legacy.resolve() == self.root.resolve():
            return
        self.root.mkdir(parents=True, exist_ok=True)
        names = (
            "habitus.md",
            "mood.md",
            "open.md",
            "digest.md",
            "cursor.json",
            "memory",
            "log",
            "handled",
            "deleted",
            "archive",
            "runtime",
            "world",
            ".anima-transaction.json",
            ".inbox-v1",
        )
        for name in names:
            source = legacy / name
            target = self.root / name
            if source.exists() and not target.exists():
                shutil.move(str(source), str(target))

    def append_received(self, event: Event) -> None:
        self._check_event(event)
        if not self.contains_event(event):
            self._append_event(event)

    def latest_event_ids(self) -> dict[str, str]:
        """Return the newest known Discord message ID for each conversation."""
        latest: dict[str, Event] = {}
        for event in self._read_all_events():
            # Slash-command results and other internal events have prefixed IDs.
            # They are useful conversation records, but cannot be Discord history cursors.
            if not event.id.isdecimal():
                continue
            current = latest.get(event.channel_id)
            if current is None or (event.ts, event.id) > (current.ts, current.id):
                latest[event.channel_id] = event
        return {channel_id: event.id for channel_id, event in latest.items()}

    def record_backfill(
        self, events: tuple[Event, ...], *, truncated: bool, now: datetime,
    ) -> None:
        """Append offline observations without turning each old mention into a new reply."""
        for event in events:
            self.append_received(event)
            self.mark_handled(event)
        self._atomic_write_json(self.root / "runtime" / "backfill.json", {
            "completed_at": now.isoformat(), "count": len(events),
            "truncated": bool(truncated),
            "latest_event_id": events[-1].id if events else None,
        })

    def backfill_status(self) -> dict[str, Any]:
        return self._read_json(self.root / "runtime" / "backfill.json", {})

    def mark_seen(self, *, now: datetime) -> None:
        cursor = self._read_json(self.root / "cursor.json", INITIAL_CURSOR)
        cursor["last_seen_at"] = now.isoformat()
        self._atomic_write_json(self.root / "cursor.json", cursor)

    def self_time_context(self) -> dict[str, object]:
        return {
            "persona": self._read_text(self.resource_root / "persona.md"),
            "rules": self._read_text(self.resource_root / "rules.md"),
            "habitus": self._read_habitus(),
            "mood": self._read_json(self.root / "mood.md", INITIAL_MOOD),
            "open_items": self._read_text(self.root / "open.md"),
            "self_memory": self._read_text(self.root / "memory" / "self.md"),
        }

    def commit_self_time(self, decision: SelfTimeDecision, *, now: datetime) -> None:
        if decision.action == "none":
            raise ValueError("an empty self-time decision cannot be committed")
        note = " ".join(decision.note.split())
        if not note or len(note) > 500:
            raise ValueError("self-time note is invalid")
        memory = self._read_text(self.root / "memory" / "self.md").strip()
        line = f"- [{now.date().isoformat()} #self self-time] {note}"
        content = "\n".join(part for part in (memory, line) if part)
        self._validate_memory(content)
        cursor = self._read_json(self.root / "cursor.json", INITIAL_CURSOR)
        cursor["version"] = int(cursor.get("version", 0)) + 1
        mood = Mood(
            decision.mood_state, decision.mood_cause, decision.mood_strength,
            decision.mood_focus, now,
        )
        self._commit_transaction(files={
            "memory/self.md": self._compact_memory(content) + "\n",
            "mood.md": self._json_text(mood.to_dict()),
            "cursor.json": self._json_text(cursor),
        })

    def mark_deleted(self, channel_id: str, event_id: str) -> bool:
        """Hide a Discord message from every future read without rewriting the journal."""
        self._check_tree()
        for name, value in (("channel_id", channel_id), ("event_id", event_id)):
            if not SAFE_ID.fullmatch(value):
                raise ValueError(f"{name} contains unsafe characters")
        path = self._safe_state_path(f"deleted/{channel_id}.txt")
        deleted = self._read_deleted_ids(channel_id)
        if event_id in deleted:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(event_id + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        return True

    def is_deleted(self, channel_id: str, event_id: str) -> bool:
        self._check_tree()
        for name, value in (("channel_id", channel_id), ("event_id", event_id)):
            if not SAFE_ID.fullmatch(value):
                raise ValueError(f"{name} contains unsafe characters")
        return event_id in self._read_deleted_ids(channel_id)

    def mark_handled(self, event: Event) -> None:
        self._check_event(event)
        if not self.is_handled(event):
            self._append_handled(event)
        self.clear_event_failure(event)

    def record_event_failure(
        self,
        event: Event,
        *,
        operation: str,
        error_type: str,
        reason: str,
        now: datetime,
    ) -> None:
        path = self.root / "runtime" / "event-failures.json"
        self._check_event(event)
        failures = self._read_json(path, {})
        failures[event.id] = {
            "event_id": event.id,
            "channel_id": event.channel_id,
            "operation": operation,
            "error_type": error_type,
            "reason": reason,
            "failed_at": now.isoformat(),
        }
        self._atomic_write_json(path, failures)

    def clear_event_failure(self, event: Event) -> None:
        self._check_event(event)
        path = self.root / "runtime" / "event-failures.json"
        failures = self._read_json(path, {})
        if event.id in failures:
            del failures[event.id]
            self._atomic_write_json(path, failures)

    def event_failures(self) -> dict[str, Any]:
        return self._read_json(self.root / "runtime" / "event-failures.json", {})

    def is_handled(self, event: Event) -> bool:
        self._check_event(event)
        path = self._handled_path(event)
        if not path.exists():
            return False
        with path.open(encoding="utf-8") as stream:
            return any(line.strip() == event.id for line in stream)

    def pending_addressed_events(self) -> tuple[Event, ...]:
        return tuple(
            event
            for event in self._read_all_events()
            if event.author_id != "self"
            and event.addressed_to_self
            and not self.is_handled(event)
        )

    def archive_old_logs(self, *, now: datetime) -> dict[str, int]:
        cursor = self._read_json(self.root / "cursor.json", INITIAL_CURSOR)
        digested_until = self._parse_optional_datetime(cursor.get("digested_until"))
        slept_at = self._parse_optional_datetime(cursor.get("slept_at"))
        cutoff = now.date() - timedelta(days=self.log_retention_days)
        archived = 0
        removed_archives = 0

        log_root = self.root / "log"
        if log_root.exists() and digested_until is not None and slept_at is not None:
            for path in sorted(log_root.glob("*/*.jsonl")):
                try:
                    day = datetime.fromisoformat(path.stem).date()
                except ValueError:
                    continue
                if day >= cutoff:
                    continue
                events = self._read_event_file(path)
                if not events:
                    continue
                newest = max(event.ts for event in events)
                if newest > digested_until or newest > slept_at:
                    continue
                if any(
                    event.author_id != "self"
                    and event.addressed_to_self
                    and not self.is_handled(event)
                    for event in events
                ):
                    continue
                destination = (
                    self.root
                    / "archive"
                    / "log"
                    / path.parent.name
                    / f"{path.name}.gz"
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                require_space(destination.parent, path.stat().st_size * 2 + 1024)
                descriptor, temporary_name = tempfile.mkstemp(
                    dir=destination.parent, prefix=f".{destination.name}."
                )
                os.close(descriptor)
                temporary = Path(temporary_name)
                try:
                    with path.open("rb") as source, gzip.open(temporary, "wb") as target:
                        shutil.copyfileobj(source, target)
                    with gzip.open(temporary, "rb") as restored, path.open("rb") as original:
                        if self._stream_hash(restored) != self._stream_hash(original):
                            raise ValueError("archived log checksum mismatch")
                    os.replace(temporary, destination)
                finally:
                    temporary.unlink(missing_ok=True)
                path.unlink()
                handled = self.root / "handled" / path.parent.name / f"{path.stem}.txt"
                handled.unlink(missing_ok=True)
                archived += 1

        archive_cutoff = now.date() - timedelta(days=self.archive_retention_days)
        archive_root = self.root / "archive" / "log"
        if archive_root.exists():
            for path in archive_root.glob("*/*.jsonl.gz"):
                name = path.name.removesuffix(".jsonl.gz")
                try:
                    day = datetime.fromisoformat(name).date()
                except ValueError:
                    continue
                if day < archive_cutoff:
                    path.unlink()
                    removed_archives += 1

        return {"archived": archived, "removed_archives": removed_archives}

    def contains_event(self, event: Event) -> bool:
        self._check_event(event)
        return any(
            item.id == event.id
            for item in self._read_recent_events(
                event.channel_id, limit=None, include_deleted=True
            )
        )

    def load_snapshot(self, event: Event) -> StateSnapshot:
        self._check_event(event)
        cursor = self._read_json(self.root / "cursor.json", INITIAL_CURSOR)
        recent = self._read_recent_events(event.channel_id)
        # Resolve bounded ancestry within this sandbox's journal, without fetching remotely.
        all_events = None
        current = event
        extra = [event] if not any(e.id == event.id for e in recent) else []
        seen = {event.id}
        for _ in range(3):
            parent = current.response_to or current.reply_to
            if not parent or parent in seen:
                break
            seen.add(parent)
            found = next((e for e in recent if e.id == parent), None)
            if found is None:
                if all_events is None:
                    all_events = self._read_recent_events(event.channel_id, limit=None)
                found = next((e for e in all_events if e.id == parent), None)
                if found is None:
                    break
                extra.append(found)
            current = found
        if extra:
            if all_events is None:
                all_events = self._read_recent_events(event.channel_id, limit=None)
            selected_ids = {e.id for e in (*recent, *extra)}
            recent = [e for e in all_events if e.id in selected_ids]
            if not any(e.id == event.id for e in recent):
                recent.insert(0, event)
        deleted = self._read_deleted_ids(event.channel_id)
        recent = [replace(e, reply_text=None, reply_author_name=None)
                  if e.reply_to in deleted else e for e in recent]
        participant_ids = {
            item.author_id for item in recent if item.author_id != "self"
        } - {event.response_person_id}
        current_author_memory = self._read_text(
            self.root / "memory" / "people" / f"{event.response_person_id}.md"
        )

        people = tuple(
            content
            for user_id in sorted(participant_ids)
            if (content := self._read_text(self.root / "memory" / "people" / f"{user_id}.md"))
        )
        backfill = self.backfill_status()
        return StateSnapshot(
            version=int(cursor.get("version", 0)),
            persona=self._read_text(self.resource_root / "persona.md"),
            rules=self._read_text(self.resource_root / "rules.md"),
            habitus=self._read_habitus(),
            mood=Mood.from_dict(self._read_json(self.root / "mood.md", INITIAL_MOOD)),
            open_items=self._read_text(self.root / "open.md"),
            digest=self._read_text(self.root / "digest.md"),
            self_memory=self._read_text(self.root / "memory" / "self.md"),
            world_memory=self._read_text(self.root / "memory" / "world.md"),
            channel_memory=self._read_text(
                self.root / "memory" / "channels" / f"{event.channel_id}.md"
            ),
            people_memory=people,
            recent_events=tuple(recent),
            last_spoke_at=self._parse_optional_datetime(cursor.get("last_spoke_at")),
            current_kind=event.kind,
            current_author_id=event.response_person_id,
            current_author_name=event.response_target.name if event.response_target else event.author_name,
            current_author_is_bot=event.response_target_is_bot if event.response_target else event.author_is_bot,
            current_author_memory=current_author_memory,
            current_author_last_seen_at=self._last_seen_before(event),
            backfill_count=int(backfill.get("count", 0) or 0),
            backfill_truncated=bool(backfill.get("truncated", False)),
        )

    def add_open_promise(self, job_id: str, source: Event, summary: str) -> None:
        """Add one exact background-job promise while running on the Actor writer."""
        self._check_event(source)
        if not re.fullmatch(r"j-[a-f0-9]{12}", job_id):
            raise ValueError("job ID is invalid")
        place = "DM" if source.kind == "dm" else "#" + re.sub(
            r"[\]\s]+", "-", source.channel_name.lstrip("#")
        )[:80]
        clean = " ".join(summary.split())[:300]
        if not clean:
            raise ValueError("promise summary is empty")
        marker = f"(job:{job_id})"
        lines = self._read_text(self.root / "open.md").strip().splitlines()
        if any(marker in line for line in lines):
            return
        lines.append(f"- [{source.ts.date().isoformat()} {place}] {clean} {marker}")
        content = "\n".join(lines).strip() + "\n"
        self._validate_open_items(content)
        self._atomic_write_text(self.root / "open.md", content)

    def close_open_promise(self, job_id: str) -> bool:
        """Close exactly one fulfilled job promise without touching other open items."""
        marker = f"(job:{job_id})"
        path = self.root / "open.md"
        lines = self._read_text(path).splitlines()
        kept = [line for line in lines if marker not in line]
        if len(kept) == len(lines):
            return False
        self._atomic_write_text(path, ("\n".join(kept).strip() + "\n") if kept else "")
        return True

    def commit_response(
        self,
        *,
        source: Event,
        draft: ResponseDraft,
        sent: SentMessage,
        expected_version: int,
        proactive: bool = False,
    ) -> Mood:
        self._check_event(source)
        cursor = self._read_json(self.root / "cursor.json", INITIAL_CURSOR)
        if int(cursor.get("version", 0)) != expected_version:
            raise ConcurrentStateChange(
                f"expected state version {expected_version}, got {cursor.get('version', 0)}"
            )
        mood = Mood(
            state=draft.mood_state,
            cause=draft.mood_cause,
            strength=draft.mood_strength,
            focus=draft.mood_focus,
            since=sent.timestamp,
        )
        response_event = Event(
            id=sent.id,
            ts=sent.timestamp,
            kind=source.kind,
            channel_id=source.channel_id,
            channel_name=source.channel_name,
            author_id="self",
            author_name="自分",
            text=draft.reply,
            guild_id=source.guild_id,
            sandbox_key=str(self.sandbox_key) if self.sandbox_key else source.sandbox_key,
            reply_to=(source.id if source.reply_to_self and not proactive else None),
            response_to=None if proactive else source.id,
            acts=(*draft.acts, "proactive") if proactive else draft.acts,
            research=draft.research,
            music=draft.music,
            actions=draft.actions,
            references=draft.references,
        )
        cursor["version"] = expected_version + 1
        cursor["last_spoke_at"] = sent.timestamp.isoformat()
        self._commit_transaction(
            files={
                "mood.md": self._json_text(mood.to_dict()),
                "cursor.json": self._json_text(cursor),
            },
            append_events=(response_event,),
            handled_events=() if proactive else (source,),
        )
        emit("conversation.response.saved", sent_id=sent.id,
             response_to=response_event.response_to, reply_to=response_event.reply_to)
        return mood

    def read_channel_events(self, channel_id: str) -> tuple[Event, ...]:
        return tuple(self._read_recent_events(channel_id, limit=None))

    def sleep_due(self, *, now: datetime) -> bool:
        """Check the sleep deadline without loading conversation history."""
        cursor = self._read_json(self.root / "cursor.json", INITIAL_CURSOR)
        slept_at = self._parse_optional_datetime(cursor.get("slept_at"))
        return slept_at is not None and now - slept_at >= timedelta(hours=24)

    def next_maintenance(self, *, now: datetime) -> DigestJob | SleepJob | ReflectionJob | None:
        cursor = self._read_json(self.root / "cursor.json", INITIAL_CURSOR)
        version = int(cursor.get("version", 0))
        all_events = self._read_all_events()
        slept_at = self._parse_optional_datetime(cursor.get("slept_at"))
        if slept_at is not None and now - slept_at >= timedelta(hours=24):
            events = tuple(event for event in all_events if event.ts > slept_at)
            return SleepJob(
                sandbox_key=str(self.sandbox_key) if self.sandbox_key else None,
                expected_version=version,
                events=events,
                digest=self._read_text(self.root / "digest.md"),
                open_items=self._read_text(self.root / "open.md"),
                mood=Mood.from_dict(self._read_json(self.root / "mood.md", INITIAL_MOOD)),
                persona=self._read_text(self.resource_root / "persona.md"),
                rules=self._read_text(self.resource_root / "rules.md"),
                habitus=self._read_habitus(),
                self_memory=self._read_text(self.root / "memory" / "self.md"),
                world_memory=self._read_text(self.root / "memory" / "world.md"),
                channel_memories=self._read_memory_directory("channels"),
                people_memories=self._read_memory_directory("people"),
            )
        reflected_at = self._parse_optional_datetime(cursor.get("reflected_at"))
        if reflected_at is not None and now - reflected_at >= timedelta(days=7):
            return ReflectionJob(
                sandbox_key=str(self.sandbox_key) if self.sandbox_key else None,
                expected_version=version,
                persona=self._read_text(self.resource_root / "persona.md"),
                habitus=self._read_habitus(),
                self_memory=self._read_text(self.root / "memory" / "self.md"),
            )
        digested_until = self._parse_optional_datetime(cursor.get("digested_until"))
        undigested = tuple(
            event for event in all_events if digested_until is None or event.ts > digested_until
        )
        if len(undigested) > self.recent_limit:
            return DigestJob(
                sandbox_key=str(self.sandbox_key) if self.sandbox_key else None,
                expected_version=version,
                events=undigested,
                existing_digest=self._read_text(self.root / "digest.md"),
            )
        return None

    def forced_maintenance(
        self, kind: str, *, now: datetime
    ) -> DigestJob | SleepJob | ReflectionJob:
        """Build one maintenance job without waiting for its normal schedule."""
        cursor = self._read_json(self.root / "cursor.json", INITIAL_CURSOR)
        version = int(cursor.get("version", 0))
        all_events = self._read_all_events()
        if kind == "nap":
            digested_until = self._parse_optional_datetime(cursor.get("digested_until"))
            events = tuple(
                event for event in all_events
                if digested_until is None or event.ts > digested_until
            )
            if not events:
                raise ValueError("昼寝で整理する新しいログがありません")
            return DigestJob(
                sandbox_key=str(self.sandbox_key) if self.sandbox_key else None,
                expected_version=version,
                events=events,
                existing_digest=self._read_text(self.root / "digest.md"),
            )
        if kind == "sleep":
            slept_at = self._parse_optional_datetime(cursor.get("slept_at"))
            events = tuple(
                event for event in all_events if slept_at is None or event.ts > slept_at
            )
            return SleepJob(
                sandbox_key=str(self.sandbox_key) if self.sandbox_key else None,
                expected_version=version,
                events=events,
                digest=self._read_text(self.root / "digest.md"),
                open_items=self._read_text(self.root / "open.md"),
                mood=Mood.from_dict(self._read_json(self.root / "mood.md", INITIAL_MOOD)),
                persona=self._read_text(self.resource_root / "persona.md"),
                rules=self._read_text(self.resource_root / "rules.md"),
                habitus=self._read_habitus(),
                self_memory=self._read_text(self.root / "memory" / "self.md"),
                world_memory=self._read_text(self.root / "memory" / "world.md"),
                channel_memories=self._read_memory_directory("channels"),
                people_memories=self._read_memory_directory("people"),
            )
        if kind == "reflection":
            return ReflectionJob(
                sandbox_key=str(self.sandbox_key) if self.sandbox_key else None,
                expected_version=version,
                persona=self._read_text(self.resource_root / "persona.md"),
                habitus=self._read_habitus(),
                self_memory=self._read_text(self.root / "memory" / "self.md"),
            )
        raise ValueError(f"unknown maintenance kind: {kind}")

    def commit_digest(self, job: DigestJob, content: str) -> None:
        self._check_job(job)
        self._validate_digest(content)
        cursor = self._cursor_at_version(job.expected_version)
        if job.events:
            cursor["digested_until"] = max(event.ts for event in job.events).isoformat()
        cursor["version"] = job.expected_version + 1
        self._commit_transaction(
            files={
                "digest.md": self._limit_lines(content, self.digest_max_lines) + "\n",
                "cursor.json": self._json_text(cursor),
            }
        )

    def commit_sleep(self, job: SleepJob, draft: SleepDraft, *, now: datetime) -> None:
        self._check_job(job)
        cursor = self._cursor_at_version(job.expected_version)
        expected = {
            ("self", "self"),
            ("world", "world"),
            *(("channel", key) for key, _ in job.channel_memories),
            *(("person", key) for key, _ in job.people_memories),
        }
        supplied = {(document.scope, document.key) for document in draft.memories}
        if len(supplied) != len(draft.memories):
            raise ValueError("sleep draft contains duplicate memory documents")
        if not expected.issubset(supplied):
            missing = sorted(expected - supplied)
            raise ValueError(f"sleep draft omitted existing memory documents: {missing}")
        observed_events = self._read_all_events()
        known_people = {
            event.author_id for event in observed_events if event.author_id != "self"
        } | {
            person.id for event in observed_events for person in event.mentioned_people
        }
        unknown_people = sorted(
            document.key
            for document in draft.memories
            if document.scope == "person"
            and (document.scope, document.key) not in expected
            and document.key not in known_people
        )
        if unknown_people:
            raise ValueError(f"sleep draft invented person ids: {unknown_people}")
        self._validate_open_items(draft.open_items)
        files: dict[str, str] = {}
        for document in draft.memories:
            self._validate_memory(document.content)
            if document.scope in {"self", "world"}:
                relative = f"memory/{document.scope}.md"
            else:
                directory = "channels" if document.scope == "channel" else "people"
                relative = f"memory/{directory}/{document.key}.md"
            files[relative] = self._compact_memory(document.content) + "\n"
        files["open.md"] = draft.open_items.strip() + "\n"
        files["mood.md"] = self._json_text(
            Mood(
                state=draft.mood_state,
                cause=draft.mood_cause,
                strength=draft.mood_strength,
                focus=draft.mood_focus,
                since=now,
            ).to_dict()
        )
        files["digest.md"] = ""
        cursor["slept_at"] = now.isoformat()
        if job.events:
            cursor["digested_until"] = max(event.ts for event in job.events).isoformat()
        cursor["version"] = job.expected_version + 1
        files["cursor.json"] = self._json_text(cursor)
        self._commit_transaction(files=files)

    def commit_reflection(
        self, job: ReflectionJob, draft: ReflectionDraft, *, now: datetime
    ) -> None:
        self._check_job(job)
        cursor = self._cursor_at_version(job.expected_version)
        files: dict[str, str] = {}
        if draft.changed:
            self._validate_habitus(draft.habitus)
            files["habitus.md"] = draft.habitus.strip() + "\n"
        cursor["reflected_at"] = now.isoformat()
        cursor["version"] = job.expected_version + 1
        files["cursor.json"] = self._json_text(cursor)
        self._commit_transaction(files=files)

    @staticmethod
    def _validate_digest(content: str) -> None:
        if not content.strip():
            raise InvalidMaintenanceDraft("digest must not be empty")
        invalid = [line for line in content.strip().splitlines() if line.strip() and not DIGEST_LINE.fullmatch(line)]
        if invalid:
            raise InvalidMaintenanceDraft(f"invalid digest line: {invalid[0]}")

    def _validate_memory(self, content: str) -> None:
        lines = [line for line in content.strip().splitlines() if line.strip()]
        invalid = [
            line
            for line in lines
            if not (
                line.startswith("## ")
                or line.startswith("関係:")
                or MEMORY_LINE.fullmatch(line)
            )
        ]
        if invalid:
            raise InvalidMaintenanceDraft(f"invalid memory line: {invalid[0]}")
        strong_count = sum("※強" in line for line in lines)
        if strong_count > self.memory_strong_max:
            raise InvalidMaintenanceDraft(
                f"memory contains {strong_count} strong entries; maximum is {self.memory_strong_max}"
            )

    @staticmethod
    def _validate_open_items(content: str) -> None:
        invalid = [line for line in content.strip().splitlines() if line.strip() and not OPEN_LINE.fullmatch(line)]
        if invalid:
            raise InvalidMaintenanceDraft(f"invalid open item: {invalid[0]}")

    @staticmethod
    def _validate_habitus(content: str) -> None:
        lines = [line for line in content.strip().splitlines() if line.strip()]
        if len(lines) > HABITUS_MAX_LINES:
            raise InvalidMaintenanceDraft(
                f"habitus contains {len(lines)} entries; maximum is {HABITUS_MAX_LINES}"
            )
        invalid = [line for line in lines if not line.startswith("- ") or len(line) <= 2]
        if invalid:
            raise InvalidMaintenanceDraft(f"invalid habitus line: {invalid[0]}")
        differences = [
            line
            for line in lines
            if any(marker in line for marker in HABITUS_DIFFERENCE_MARKERS)
        ]
        if differences:
            raise InvalidMaintenanceDraft(
                f"habitus must describe current behavior, not a difference: {differences[0]}"
            )

    def _read_habitus(self) -> str:
        content = self._read_text(self.root / "habitus.md")
        self._validate_habitus(content)
        return content

    @staticmethod
    def _limit_lines(content: str, maximum: int) -> str:
        lines = [line for line in content.strip().splitlines() if line.strip()]
        return "\n".join(lines[-maximum:])

    def _compact_memory(self, content: str) -> str:
        lines = [line for line in content.strip().splitlines() if line.strip()]
        if len(lines) <= self.memory_max_lines:
            return "\n".join(lines)

        metadata = [line for line in lines if not line.lstrip().startswith("-")]
        entries = [line for line in lines if line.lstrip().startswith("-")]
        metadata = metadata[: self.memory_max_lines]
        capacity = self.memory_max_lines - len(metadata)
        if capacity <= 0:
            return "\n".join(metadata)

        strong_indices = [index for index, line in enumerate(entries) if "※強" in line][
            -capacity:
        ]
        remaining = capacity - len(strong_indices)
        normal_indices = (
            [index for index, line in enumerate(entries) if "※強" not in line][-remaining:]
            if remaining
            else []
        )
        selected = set(strong_indices + normal_indices)
        kept_entries = [line for index, line in enumerate(entries) if index in selected]
        return "\n".join([*metadata, *kept_entries])

    def _append_event(self, event: Event) -> None:
        self._check_event(event)
        directory = self.root / "log" / event.channel_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{event.ts.date().isoformat()}.jsonl"
        line = json.dumps(event.to_log_dict(), ensure_ascii=False, separators=(",", ":"))
        require_space(directory, len(line.encode("utf-8")) + 1)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def _append_handled(self, event: Event) -> None:
        path = self._handled_path(event)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(event.id + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def _handled_path(self, event: Event) -> Path:
        self._check_event(event)
        return self.root / "handled" / event.channel_id / f"{event.ts.date().isoformat()}.txt"

    def _initialize_inbox_tracking(self) -> None:
        marker = self.root / ".inbox-v1"
        if marker.exists():
            return
        # Events written by older versions have no handled marker. Baseline them so
        # an upgrade does not replay the entire Discord history.
        for event in self._read_all_events():
            if event.author_id != "self" and event.addressed_to_self:
                self.mark_handled(event)
        self._atomic_write_text(marker, "1\n")

    def _read_recent_events(
        self,
        channel_id: str,
        limit: int | None = -1,
        *,
        include_deleted: bool = False,
    ) -> list[Event]:
        self._check_tree()
        self._safe_state_path(f"log/{channel_id}")
        directory = self.root / "log" / channel_id
        if not directory.exists():
            return []
        events: list[Event] = []
        for path in sorted(directory.glob("*.jsonl")):
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    if line.strip():
                        events.append(Event.from_log_dict(json.loads(line)))
        for event in events:
            self._check_event(event, check_tree=False)
        if not include_deleted:
            deleted = self._read_deleted_ids(channel_id)
            events = [event for event in events if event.id not in deleted]
        if limit is None:
            return events
        actual_limit = self.recent_limit if limit == -1 else limit
        return events[-actual_limit:]

    def _read_deleted_ids(self, channel_id: str) -> set[str]:
        path = self._safe_state_path(f"deleted/{channel_id}.txt")
        if not path.exists():
            return set()
        with path.open(encoding="utf-8") as stream:
            return {line.strip() for line in stream if line.strip()}

    @staticmethod
    def _read_event_file(path: Path) -> list[Event]:
        events: list[Event] = []
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    events.append(Event.from_log_dict(json.loads(line)))
        return events

    def _read_all_events(self) -> list[Event]:
        self._check_tree()
        events: list[Event] = []
        log_root = self.root / "log"
        if not log_root.exists():
            return events
        for directory in sorted(path for path in log_root.iterdir() if path.is_dir()):
            events.extend(self._read_recent_events(directory.name, limit=None))
        return sorted(events, key=lambda event: (event.ts, event.id))

    def _last_seen_before(self, current: Event) -> datetime | None:
        candidates = [
            event.ts
            for event in self._read_all_events()
            if event.author_id == current.response_person_id
            and event.id != current.id
            and (event.ts, event.id) < (current.ts, current.id)
        ]
        return max(candidates) if candidates else None

    def _read_memory_directory(self, name: str) -> tuple[tuple[str, str], ...]:
        directory = self.root / "memory" / name
        if not directory.exists():
            return ()
        return tuple(
            (path.stem, self._read_text(path)) for path in sorted(directory.glob("*.md"))
        )

    def _cursor_at_version(self, expected_version: int) -> dict[str, Any]:
        cursor = self._read_json(self.root / "cursor.json", INITIAL_CURSOR)
        if int(cursor.get("version", 0)) != expected_version:
            raise ConcurrentStateChange(
                f"expected state version {expected_version}, got {cursor.get('version', 0)}"
            )
        return cursor

    def _read_text(self, path: Path) -> str:
        self._check_tree()
        return path.read_text(encoding="utf-8").strip() if path.exists() else ""

    def _read_json(self, path: Path, default: dict[str, Any]) -> dict[str, Any]:
        self._check_tree()
        if not path.exists():
            return dict(default)
        return json.loads(path.read_text(encoding="utf-8"))

    def _atomic_write_json(self, path: Path, value: dict[str, Any]) -> None:
        self._atomic_write_text(path, self._json_text(value))

    def _atomic_write_text(self, path: Path, value: str) -> None:
        self._check_tree()
        path.parent.mkdir(parents=True, exist_ok=True)
        require_space(path.parent, len(value.encode("utf-8")))
        descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _commit_transaction(
        self,
        *,
        files: dict[str, str],
        append_events: tuple[Event, ...] = (),
        handled_events: tuple[Event, ...] = (),
    ) -> None:
        manifest = {
            "version": 2,
            "sandbox_key": str(self.sandbox_key) if self.sandbox_key else None,
            "files": files,
            "append_events": [event.to_log_dict() for event in append_events],
            "handled_events": [event.to_log_dict() for event in handled_events],
        }
        manifest["checksum"] = self._transaction_hash(manifest)
        self._atomic_write_json(self.root / ".anima-transaction.json", manifest)
        self._apply_transaction(manifest)
        self._clear_transaction()

    def _recover_transaction(self) -> None:
        path = self.root / ".anima-transaction.json"
        if not path.exists():
            return
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("version") not in (1, 2):
            raise RuntimeError("unsupported state transaction version")
        self._apply_transaction(manifest)
        self._clear_transaction()

    def _apply_transaction(self, manifest: dict[str, Any]) -> None:
        if manifest.get("version") == 2 and manifest.get("checksum") != self._transaction_hash(manifest):
            raise ValueError("state transaction checksum mismatch")
        if self.sandbox_key is not None and manifest.get("sandbox_key") != str(self.sandbox_key):
            raise ValueError("transaction belongs to another sandbox")
        # Validate the entire manifest before applying even its first file.
        for relative in manifest.get("files", {}):
            self._safe_state_path(relative)
        for value in (*manifest.get("append_events", []), *manifest.get("handled_events", [])):
            self._check_event(Event.from_log_dict(value))
        for relative, content in manifest.get("files", {}).items():
            target = self._safe_state_path(relative)
            self._atomic_write_text(target, str(content))
        for value in manifest.get("append_events", []):
            event = Event.from_log_dict(value)
            if not self.contains_event(event):
                self._append_event(event)
        for value in manifest.get("handled_events", []):
            event = Event.from_log_dict(value)
            self.mark_handled(event)

    def _clear_transaction(self) -> None:
        path = self.root / ".anima-transaction.json"
        path.unlink(missing_ok=True)
        descriptor = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _safe_state_path(self, relative: str) -> Path:
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"unsafe transaction path: {relative}")
        target = (self.root / path).resolve()
        root = self.root.resolve()
        if target != root and root not in target.parents:
            raise ValueError(f"transaction path escapes state root: {relative}")
        return target

    def _check_tree(self) -> None:
        if self.sandbox_key is not None:
            reject_symlinks(self.root, self._sandbox_anchor)
            for path in self.root.rglob("*"):
                if path.is_symlink():
                    raise ValueError("symlinks are not allowed in sandbox state")

    def _check_event(self, event: Event, *, check_tree: bool = True) -> None:
        if check_tree:
            self._check_tree()
        if self.sandbox_key is not None and SandboxKey.for_event(event) != self.sandbox_key:
            raise ValueError("event belongs to another sandbox")

    def _check_job(self, job) -> None:
        self._check_tree()
        if self.sandbox_key is not None:
            if job.sandbox_key != str(self.sandbox_key):
                raise ValueError("maintenance job belongs to another sandbox")
            for event in getattr(job, "events", ()):
                self._check_event(event, check_tree=False)

    @staticmethod
    def _transaction_hash(manifest: dict[str, Any]) -> str:
        payload = {key: value for key, value in manifest.items() if key != "checksum"}
        return hashlib.sha256(json.dumps(
            payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()

    @staticmethod
    def _stream_hash(stream) -> str:
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _json_text(value: dict[str, Any]) -> str:
        return json.dumps(value, ensure_ascii=False, indent=2) + "\n"

    @staticmethod
    def _parse_optional_datetime(value: Any) -> datetime | None:
        return datetime.fromisoformat(value) if value else None
