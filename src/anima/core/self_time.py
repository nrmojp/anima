"""Low-frequency, interruptible autonomous activity loop for one sandbox."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
import json
import logging
from pathlib import Path
from typing import Literal, Protocol

from anima.core.modes import ModeState
from anima.core.telemetry import emit


LOGGER = logging.getLogger(__name__)
HISTORY_LIMIT = 5


@dataclass(frozen=True, slots=True)
class SelfTimeDecision:
    action: Literal["none", "continue", "finish"]
    note: str = ""
    mood_state: str = "普通"
    mood_cause: str = "自分の時間"
    mood_strength: Literal["弱い", "ふつう", "強い"] = "ふつう"
    mood_focus: str = ""
    reflection: str = "記録なし"
    direction: Literal["deepen", "broaden"] = "deepen"
    discovery: str = "記録なし"

    def __post_init__(self) -> None:
        if self.action not in {"none", "continue", "finish"}:
            raise ValueError("self-time action is invalid")
        if self.direction not in {"deepen", "broaden"}:
            raise ValueError("self-time direction is invalid")
        if not self.discovery.strip() or len(self.discovery) > 500:
            raise ValueError("self-time discovery is invalid")
        if self.action == "none" and self.note:
            raise ValueError("none decision must not invent an activity note")
        if self.action != "none" and (not self.note.strip() or len(self.note) > 500):
            raise ValueError("self-time activity note is invalid")
        if self.mood_strength not in {"弱い", "ふつう", "強い"}:
            raise ValueError("self-time mood strength is invalid")
        if not self.reflection.strip() or len(self.reflection) > 500:
            raise ValueError("self-time reflection is invalid")


class SelfTimeDecider(Protocol):
    async def decide(
        self, context: Mapping[str, object], iteration: int,
        previous: SelfTimeDecision | None,
    ) -> SelfTimeDecision: ...


class SelfTimeService:
    """Run rare self-driven iterations with daily, concurrency and iteration fuses."""

    def __init__(
        self, path: Path, decider: SelfTimeDecider,
        context: Callable[[], Mapping[str, object]],
        commit: Callable[[SelfTimeDecision, datetime], None],
        *, clock: Callable[[], datetime], allowed: Callable[[], bool],
        busy: Callable[[], bool], interval_seconds: float = 21_600,
        daily_limit: int = 2, max_iterations: int = 4,
        modes_changed: Callable[[], object] | None = None,
    ) -> None:
        if interval_seconds <= 0 or daily_limit <= 0 or max_iterations <= 0:
            raise ValueError("self-time limits must be positive")
        self.path, self.decider, self.context, self.commit = path, decider, context, commit
        self.clock, self.allowed, self.busy = clock, allowed, busy
        self.interval_seconds = interval_seconds
        self.daily_limit, self.max_iterations = daily_limit, max_iterations
        self.modes_changed = modes_changed or (lambda: None)
        self._worker: asyncio.Task[None] | None = None
        self._active: asyncio.Task[tuple[SelfTimeDecision, ...]] | None = None
        self._started_at: str | None = None

    async def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._run(), name="self-time-scheduler")

    async def stop(self) -> None:
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.cancel()
        await self.interrupt()
        if worker is not None:
            await asyncio.gather(worker, return_exceptions=True)

    async def interrupt(self) -> bool:
        task = self._active
        if task is None or task.done() or task is asyncio.current_task():
            return False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        emit("self_time.interrupted")
        return True

    async def tick(self, *, forced: bool = False) -> tuple[SelfTimeDecision, ...]:
        if self._active is not None and not self._active.done():
            return ()
        self._active = asyncio.create_task(self._tick(forced=forced), name="self-time-tick")
        try:
            return await self._active
        finally:
            self._active = None
            self._started_at = None
            self.modes_changed()

    def modes(self) -> tuple[ModeState, ...]:
        if self._active is None or self._active.done():
            return ()
        return (ModeState(
            "core", "self_time", "自分の時間を過ごしている",
            "続けるか、やめるか考えながら活動中", self._started_at,
        ),)

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.interval_seconds)
            try:
                await self.tick()
            except asyncio.CancelledError:
                if self._worker is asyncio.current_task():
                    continue
                raise
            except Exception as error:
                LOGGER.exception("Self-time iteration failed; scheduler will continue")
                emit("self_time.failed", error_type=type(error).__name__)

    async def _tick(self, *, forced: bool = False) -> tuple[SelfTimeDecision, ...]:
        now = self.clock()
        state = self._read_state()
        history = state.get("history", [])
        history = [item for item in history if isinstance(item, dict)][-HISTORY_LIMIT:] if isinstance(history, list) else []
        day = now.date().isoformat()
        starts = int(state.get("starts", 0)) if state.get("day") == day else 0
        allowed, busy = self.allowed(), self.busy()
        if busy or (not forced and (not allowed or starts >= self.daily_limit)):
            emit("self_time.skipped", forced=forced, reason=(
                "busy" if busy else "mode" if not allowed else "daily_limit"
            ))
            return ()
        state = {
            "day": day,
            "starts": starts if forced else starts + 1,
            "last_started_at": now.isoformat(),
            "history": history,
        }
        self._write_state(state)
        self._started_at = now.isoformat()
        self.modes_changed()
        emit("self_time.started", iteration_limit=self.max_iterations, forced=forced)
        results: list[SelfTimeDecision] = []
        previous = None
        session = {"started_at": now.isoformat(), "trigger": "experimental" if forced else "scheduled", "status": "running", "decisions": []}
        try:
            for iteration in range(1, self.max_iterations + 1):
                context = dict(self.context())
                context["self_time_trigger"] = "experimental" if forced else "scheduled"
                context["recent_self_time"] = history
                context["current_self_time"] = session["decisions"]
                decision = await self.decider.decide(context, iteration, previous)
                if not isinstance(decision, SelfTimeDecision):
                    raise TypeError("self-time decider returned an invalid decision")
                results.append(decision)
                record = {**asdict(decision), "state_committed": False}
                session["decisions"].append(record)
                if decision.action == "none":
                    break
                self.commit(decision, self.clock())
                record["state_committed"] = True
                if decision.action == "finish":
                    break
                previous = decision
            session["status"] = "completed"
            emit(
                "self_time.completed", iterations=len(results), action=results[-1].action,
                notes=[result.note for result in results if result.note],
                reflections=[result.reflection for result in results],
                directions=[result.direction for result in results],
                discoveries=[result.discovery for result in results],
            )
            return tuple(results)
        except asyncio.CancelledError:
            session["status"] = "interrupted"
            emit("self_time.cancelled", iterations=len(results))
            raise
        except Exception as error:
            session["status"] = "failed"
            session["error_type"] = type(error).__name__
            raise
        finally:
            session["ended_at"] = self.clock().isoformat()
            state["history"] = [*history, session][-HISTORY_LIMIT:]
            self._write_state(state)

    def _read_state(self) -> dict[str, object]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, json.JSONDecodeError):
            return {}

    def _write_state(self, value: Mapping[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(self.path)
