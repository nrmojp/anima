"""Sandbox-local background jobs contributed by capability plugins."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
import json
from pathlib import Path
from typing import Literal
from uuid import uuid4

from anima.core.telemetry import emit
from anima.core.modes import ModeState


JobState = Literal["queued", "running", "completed", "failed", "cancelled"]


@dataclass(frozen=True, slots=True)
class JobRecord:
    id: str
    plugin: str
    kind: str
    state: JobState
    created_at: str
    updated_at: str
    summary: str = ""
    result: Mapping[str, object] | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id, "plugin": self.plugin, "kind": self.kind,
            "state": self.state, "created_at": self.created_at,
            "updated_at": self.updated_at, "summary": self.summary,
            "result": dict(self.result) if self.result is not None else None,
            "error": self.error,
        }


class PluginJobManager:
    """Own background tasks and a durable, inspectable job journal for one sandbox."""

    def __init__(
        self, path: Path, *, history_limit: int = 100, max_active: int = 1,
    ) -> None:
        if history_limit <= 0 or max_active <= 0:
            raise ValueError("job limits must be positive")
        self.path = path
        self.history_limit = history_limit
        self.max_active = max_active
        self._records = self._load()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._stopping = False

    async def start(self) -> None:
        self._stopping = False

    async def stop(self) -> None:
        self._stopping = True
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def submit(
        self, plugin: str, kind: str, summary: str,
        runner: Callable[[], Awaitable[Mapping[str, object]]],
        completed: Callable[[JobRecord], Awaitable[None]] | None = None,
        *, job_id: str | None = None,
    ) -> JobRecord:
        if self._stopping:
            raise RuntimeError("job manager is stopping")
        if not plugin or not kind or not callable(runner):
            raise ValueError("job submission is invalid")
        if self.active_count() >= self.max_active:
            raise RuntimeError("job capacity reached")
        identifier = job_id or self.create_id()
        if identifier in self._records or not identifier.startswith("j-"):
            raise ValueError("job ID is invalid or already used")
        now = datetime.now().astimezone().isoformat()
        record = JobRecord(
            id=identifier, plugin=plugin, kind=kind,
            state="queued", created_at=now, updated_at=now, summary=summary[:240],
        )
        self._records[record.id] = record
        self._trim_and_write()
        task = asyncio.create_task(
            self._run(record.id, runner, completed), name=f"plugin-job-{record.id}"
        )
        self._tasks[record.id] = task
        emit("plugin_job.queued", job_id=record.id, plugin=plugin, kind=kind)
        return record

    @staticmethod
    def create_id() -> str:
        return f"j-{uuid4().hex[:12]}"

    def get(self, job_id: str) -> JobRecord | None:
        return self._records.get(job_id)

    def list(self, *, plugin: str | None = None) -> tuple[JobRecord, ...]:
        values = self._records.values()
        if plugin is not None:
            values = (item for item in values if item.plugin == plugin)
        return tuple(sorted(values, key=lambda item: item.created_at, reverse=True))

    def active_count(self, *, plugin: str | None = None, kind: str | None = None) -> int:
        return sum(
            item.state in {"queued", "running"}
            and (plugin is None or item.plugin == plugin)
            and (kind is None or item.kind == kind)
            for item in self._records.values()
        )

    def modes(self) -> tuple[ModeState, ...]:
        return tuple(
            ModeState(
                item.plugin, f"job.{item.id}",
                item.summary or f"{item.kind}を実行している",
                f"{item.kind}（{item.state}）", item.created_at,
            )
            for item in self.list()
            if item.state in {"queued", "running"}
        )

    async def cancel(self, job_id: str) -> bool:
        task = self._tasks.get(job_id)
        if task is None or task.done():
            return False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return True

    async def _run(self, job_id, runner, completed) -> None:
        record = self._update(job_id, state="running")
        emit("plugin_job.started", job_id=job_id, plugin=record.plugin, kind=record.kind)
        try:
            result = dict(await runner())
            json.dumps(result)
            record = self._update(job_id, state="completed", result=result)
            emit("plugin_job.completed", job_id=job_id, plugin=record.plugin, kind=record.kind)
            if completed is not None:
                try:
                    await completed(record)
                except Exception as error:
                    emit("plugin_job.notification_failed", job_id=job_id,
                         plugin=record.plugin, kind=record.kind,
                         error_type=type(error).__name__)
        except asyncio.CancelledError:
            record = self._update(job_id, state="cancelled")
            emit("plugin_job.cancelled", job_id=job_id, plugin=record.plugin, kind=record.kind)
        except Exception as error:
            record = self._update(
                job_id, state="failed", error=type(error).__name__
            )
            emit("plugin_job.failed", job_id=job_id, plugin=record.plugin,
                 kind=record.kind, error_type=type(error).__name__)
            if completed is not None:
                try:
                    await completed(record)
                except Exception as notify_error:
                    emit("plugin_job.notification_failed", job_id=job_id,
                         plugin=record.plugin, kind=record.kind,
                         error_type=type(notify_error).__name__)
        finally:
            self._tasks.pop(job_id, None)

    def _update(self, job_id: str, **changes) -> JobRecord:
        record = replace(
            self._records[job_id], updated_at=datetime.now().astimezone().isoformat(),
            **changes,
        )
        self._records[job_id] = record
        self._trim_and_write()
        return record

    def _load(self) -> dict[str, JobRecord]:
        try:
            values = json.loads(self.path.read_text(encoding="utf-8"))["jobs"]
            result = {}
            for value in values:
                state = str(value["state"])
                error = value.get("error")
                if state in {"queued", "running"}:
                    state, error = "failed", "interrupted_by_restart"
                item = JobRecord(
                    id=str(value["id"]), plugin=str(value["plugin"]),
                    kind=str(value["kind"]), state=state,
                    created_at=str(value["created_at"]), updated_at=str(value["updated_at"]),
                    summary=str(value.get("summary", "")), result=value.get("result"),
                    error=str(error) if error is not None else None,
                )
                result[item.id] = item
            return result
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return {}

    def _trim_and_write(self) -> None:
        ordered = sorted(self._records.values(), key=lambda item: item.created_at)
        removable = [item for item in ordered if item.state not in {"queued", "running"}]
        while len(ordered) > self.history_limit and removable:
            item = removable.pop(0)
            self._records.pop(item.id, None)
            ordered.remove(item)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(
            {"jobs": [item.to_dict() for item in ordered]}, ensure_ascii=False, indent=2,
        ) + "\n", encoding="utf-8")
        temporary.replace(self.path)
