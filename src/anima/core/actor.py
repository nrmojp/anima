"""Single-writer persona actor."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
import itertools
import logging
import time
from dataclasses import dataclass
from typing import TypeVar

from anima.core.context import ContextBuilder
from anima.core.models import (
    DigestJob,
    Event,
    OutcomeKind,
    ProcessOutcome,
    ReflectionJob,
    ResponseDraft,
    SleepJob,
)
from anima.core.ports import (
    FacePreparer,
    FacePresenter,
    MemoryIndexSynchronizer,
    MemoryMaintainer,
    MessageSender,
    ProactiveMessageSender,
    Responder,
    VoiceSink,
)
from anima.core.retry import classify_retry, retry_delay
from anima.core.state import FileStateStore
from anima.core.telemetry import emit
from .reaction_queue import ReactionBatch, ReactionBuffer


@dataclass(slots=True)
class _MaintenanceRequest:
    kind: str
    future: asyncio.Future[str] | None


@dataclass(slots=True)
class _DeletionRequest:
    channel_id: str
    event_id: str
    future: asyncio.Future[bool]


QueuePayload = Event | ReactionBatch | _MaintenanceRequest | _DeletionRequest | None
QueueItem = tuple[int, int, QueuePayload, asyncio.Future[ProcessOutcome] | None, float, bool]
LOGGER = logging.getLogger(__name__)
T = TypeVar("T")
MAINTENANCE_RETRY_DELAY_SECONDS = 3600
SLEEP_CHECK_INTERVAL_SECONDS = 60


class PersonaActor:
    def __init__(
        self,
        *,
        store: FileStateStore,
        context_builder: ContextBuilder,
        responder: Responder,
        sender: MessageSender,
        face_preparer: FacePreparer | None = None,
        face_presenter: FacePresenter | None = None,
        proactive_sender: ProactiveMessageSender | None = None,
        voice: VoiceSink | None = None,
        maintainer: MemoryMaintainer | None = None,
        memory_index: MemoryIndexSynchronizer | None = None,
        clock: Callable[[], datetime],
        queue_size: int = 100,
        response_timeout_seconds: float = 45.0,
        send_timeout_seconds: float = 15.0,
        maintenance_timeout_seconds: float = 300.0,
        max_retries: int = 2,
        retry_base_delay_seconds: float = 0.5,
        reactions=None,
        proactive_allowed: Callable[[], bool] | None = None,
        self_time=None,
        address_classifier=None,
        contextual_reply_allowed: Callable[[], bool] | None = None,
    ) -> None:
        self.store = store
        self.reactions = reactions
        self.proactive_allowed = proactive_allowed or (lambda: False)
        self.self_time = self_time
        self.address_classifier = address_classifier
        self.contextual_reply_allowed = contextual_reply_allowed or (lambda: True)
        self._address_lock = asyncio.Lock()
        self.reaction_buffer = ReactionBuffer(self._queue_reaction)
        self.context_builder = context_builder
        self.responder = responder
        self.sender = sender
        self.face_preparer = face_preparer or (
            sender if isinstance(sender, FacePreparer) else None
        )
        self.face_presenter = face_presenter or (
            sender if isinstance(sender, FacePresenter) else None
        )
        self.proactive_sender = proactive_sender or (
            sender if isinstance(sender, ProactiveMessageSender) else None
        )
        self.voice = voice
        self.maintainer = maintainer
        self.memory_index = memory_index
        self.clock = clock
        self.response_timeout_seconds = response_timeout_seconds
        self.send_timeout_seconds = send_timeout_seconds
        self.maintenance_timeout_seconds = maintenance_timeout_seconds
        self.max_retries = max_retries
        self.retry_base_delay_seconds = retry_base_delay_seconds
        self._queue: asyncio.PriorityQueue[QueueItem] = asyncio.PriorityQueue(queue_size)
        self._sequence = itertools.count()
        self._worker: asyncio.Task[None] | None = None
        self._sleep_scheduler: asyncio.Task[None] | None = None
        self._sleep_check_pending = False
        self._maintenance_retry_after = 0.0
        self._current: dict[str, object] | None = None
        self._retry_state: dict[str, object] | None = None

    async def start(self) -> None:
        if self._worker is not None:
            return
        self.store.ensure_layout(now=self.clock())
        self._worker = asyncio.create_task(self._run(), name="persona-actor")
        if self.maintainer is not None:
            self._sleep_scheduler = asyncio.create_task(
                self._schedule_sleep(), name="persona-sleep-scheduler"
            )
        self._write_runtime_state(running=True)

    async def stop(self) -> None:
        await self.reaction_buffer.stop()
        scheduler, self._sleep_scheduler = self._sleep_scheduler, None
        if scheduler is not None:
            scheduler.cancel()
            await asyncio.gather(scheduler, return_exceptions=True)
        if self._worker is None:
            return
        await self._queue.put((99, next(self._sequence), None, None, time.monotonic(), False))
        await self._worker
        self._worker = None
        self._current = None
        self._retry_state = None
        self._write_runtime_state(running=False)

    async def submit(self, event: Event, *, allow_reactions: bool = True) -> ProcessOutcome:
        if self._worker is None:
            raise RuntimeError("PersonaActor.start() must be called before submit()")
        if self.self_time is not None:
            await self.self_time.interrupt()
        already_logged = self.store.contains_event(event)
        self.store.append_received(event)
        if self.store.is_handled(event):
            return ProcessOutcome(OutcomeKind.DUPLICATE, event.id)
        contextual = False
        if not event.requires_immediate_response:
            contextual = await self._contextual_address(event)
        if not event.requires_immediate_response and not contextual:
            self.store.mark_handled(event)
            if (
                allow_reactions
                and self.reactions is not None
                and (getattr(getattr(self.reactions, "faces", None), "available", ())
                     or self.proactive_allowed())
                and not event.is_private
            ):
                self.reaction_buffer.add(event)
            return ProcessOutcome(OutcomeKind.IGNORED, event.id)
        future = asyncio.get_running_loop().create_future()
        priority = 0 if event.is_private else 1 if event.reply_to_self else 2
        await self._queue.put(
            (priority, next(self._sequence), event, future, time.monotonic(), already_logged)
        )
        self._write_runtime_state(running=True)
        emit("persona.queued", event_id=event.id, priority=priority, depth=self._queue.qsize())
        return await future

    async def _contextual_address(self, event: Event) -> bool:
        """Conservative, read-only routing decision, independent of ambient reactions."""
        if (self.address_classifier is None or event.author_is_bot or event.is_private
                or not self.contextual_reply_allowed()):
            return False
        async with self._address_lock:
            snapshot = self.store.load_snapshot(event)
            history = tuple(e for e in snapshot.recent_events
                            if e.channel_id == event.channel_id and e.id != event.id
                            and 0 <= (event.ts - e.ts).total_seconds() <= 600)[-20:]
            if not event.called_name:
                own = [e for e in history if e.author_id == "self"]
                if not own or (event.ts - own[-1].ts).total_seconds() > 120:
                    return False
                # Do not carry an old conversational invitation into other people's exchanges.
                anchor = history.index(own[-1])
                before = [e for e in history[:anchor] if e.author_id != "self"]
                linked = next((e for e in history if e.id == (own[-1].response_to or own[-1].reply_to)), None)
                previous_author = (linked.author_id if linked else None
                                   if own[-1].response_to else before[-1].author_id if before else None)
                if previous_author != event.author_id:
                    return False
                if any(e.author_id not in {"self", event.author_id}
                       for e in history[anchor + 1:]):
                    return False
            if event.reply_to and not event.reply_to_self:
                return False
            try:
                async with asyncio.timeout(15):
                    decision = await self.address_classifier.expects_reply(snapshot, history, event)
                if type(decision) is not bool:
                    raise ValueError("address decision must be boolean")
            except Exception as error:
                emit("address.failed", event_id=event.id, error_type=type(error).__name__)
                LOGGER.exception("Contextual address decision failed for %s", event.id)
                return False
            accepted = decision and self.contextual_reply_allowed()
            emit("address.decided", event_id=event.id, expects_reply=accepted)
            return accepted

    async def mark_deleted(self, channel_id: str, event_id: str) -> bool:
        if self._worker is None:
            raise RuntimeError("PersonaActor.start() must be called before mark_deleted()")
        future = asyncio.get_running_loop().create_future()
        await self._queue.put(
            (-1, next(self._sequence), _DeletionRequest(channel_id, event_id, future), None,
             time.monotonic(), False)
        )
        return await future

    def pending_events(self) -> tuple[Event, ...]:
        return self.store.pending_addressed_events()

    def latest_event_ids(self) -> dict[str, str]:
        return self.store.latest_event_ids()

    def record_backfill(self, events: tuple[Event, ...], *, truncated: bool) -> None:
        self.store.record_backfill(events, truncated=truncated, now=self.clock())

    def mark_seen(self) -> None:
        self.store.mark_seen(now=self.clock())

    def abandon(self, event: Event) -> None:
        self.store.mark_handled(event)

    async def wait_idle(self) -> None:
        await self._queue.join()

    async def cancel_pending_reactions(self) -> None:
        """Discard debounced ambient reactions after an activity-mode downgrade."""
        await self.reaction_buffer.stop()

    async def force_maintenance(self, kind: str) -> str:
        """Run an experimental maintenance job through the single-writer queue."""
        if self._worker is None:
            raise RuntimeError("PersonaActor.start() must be called before maintenance")
        if self.maintainer is None:
            raise RuntimeError("memory maintenance is not configured")
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        request = _MaintenanceRequest(kind, future)
        await self._queue.put((0, next(self._sequence), request, None, time.monotonic(), False))
        self._write_runtime_state(running=True)
        emit("maintenance.queued", operation=kind, forced=True, depth=self._queue.qsize())
        return await future

    async def _schedule_sleep(self) -> None:
        while True:
            if not self._sleep_check_pending and time.monotonic() >= self._maintenance_retry_after:
                try:
                    if await asyncio.to_thread(self.store.sleep_due, now=self.clock()):
                        self._queue.put_nowait((
                            98, next(self._sequence), _MaintenanceRequest("scheduled_sleep", None),
                            None, time.monotonic(), False,
                        ))
                        self._sleep_check_pending = True
                        emit("maintenance.queued", operation="sleep", forced=False,
                             depth=self._queue.qsize())
                except asyncio.QueueFull:
                    pass
                except Exception as error:
                    self._maintenance_retry_after = (
                        time.monotonic() + MAINTENANCE_RETRY_DELAY_SECONDS
                    )
                    emit("maintenance.failed", operation="sleep", error_type=type(error).__name__,
                         retry_delay_seconds=MAINTENANCE_RETRY_DELAY_SECONDS)
                    LOGGER.exception("Scheduled sleep check failed")
            await asyncio.sleep(SLEEP_CHECK_INTERVAL_SECONDS)

    async def force_self_time(self) -> tuple[object, ...]:
        """Run Self Time immediately while retaining concurrency safety fuses."""
        if self._worker is None:
            raise RuntimeError("PersonaActor.start() must be called before self time")
        if self.self_time is None:
            raise RuntimeError("self time is not configured")
        decisions = await self.self_time.tick(forced=True)
        if not decisions:
            raise ValueError("Self Timeは別の処理中のため開始できませんでした。")
        return decisions

    async def _run(self) -> None:
        while True:
            _, _, event, future, queued_at, recovery = await self._queue.get()
            if event is None:
                self._queue.task_done()
                return
            self._current = self._runtime_item(event)
            self._retry_state = None
            self._write_runtime_state(running=True)
            if isinstance(event, _DeletionRequest):
                try:
                    changed = self.store.mark_deleted(event.channel_id, event.event_id)
                    event.future.set_result(changed)
                    emit(
                        "discord.message.deleted",
                        channel_id=event.channel_id,
                        event_id=event.event_id,
                        newly_recorded=changed,
                    )
                except Exception as error:
                    try:
                        forwarded = type(error)(*error.args)
                    except Exception:
                        forwarded = RuntimeError(str(error))
                    event.future.set_exception(forwarded)
                finally:
                    self._queue.task_done()
                    self._current = None
                    self._write_runtime_state(running=True)
                continue
            if isinstance(event, ReactionBatch):
                source = None
                try:
                    source = await self.reactions.process(
                        event, now=self.clock(), allow_speak=self.proactive_allowed()
                    )
                    if source is not None and self.proactive_allowed():
                        await self._process_proactive(source)
                except Exception as error:
                    emit(
                        "proactive.failed" if source is not None else "reaction.failed",
                        error_type=type(error).__name__,
                    )
                finally:
                    self._queue.task_done()
                    self._current = None
                    self._write_runtime_state(running=True)
                continue
            if isinstance(event, _MaintenanceRequest):
                try:
                    if event.kind == "scheduled_sleep":
                        job = self.store.next_maintenance(now=self.clock())
                        if isinstance(job, SleepJob):
                            await self._run_maintenance(job)
                    else:
                        job = self.store.forced_maintenance(event.kind, now=self.clock())
                        operation = await self._run_maintenance(job, forced=True)
                        assert event.future is not None
                        event.future.set_result(operation)
                except Exception as error:
                    if event.future is not None:
                        try:
                            forwarded = type(error)(*error.args)
                        except Exception:
                            forwarded = RuntimeError(str(error))
                        event.future.set_exception(forwarded)
                    else:
                        self._maintenance_retry_after = (
                            time.monotonic() + MAINTENANCE_RETRY_DELAY_SECONDS
                        )
                        emit("maintenance.failed", operation="sleep",
                             error_type=type(error).__name__,
                             retry_delay_seconds=MAINTENANCE_RETRY_DELAY_SECONDS)
                        LOGGER.exception("Scheduled sleep failed")
                finally:
                    if event.kind == "scheduled_sleep":
                        self._sleep_check_pending = False
                    self._queue.task_done()
                    self._current = None
                    self._write_runtime_state(running=True)
                continue
            started = time.monotonic()
            try:
                outcome = await self._process(event, recovery=recovery)
                assert future is not None
                future.set_result(outcome)
            except Exception as error:
                if event.id not in self.store.event_failures():
                    self._record_failure(event, "persona.process", error, "failed")
                emit(
                    "persona.failed",
                    event_id=event.id,
                    error_type=type(error).__name__,
                )
                # Do not hand the worker's active exception object to another Task.
                # Reusing its traceback can retain an already-awaited coroutine.
                try:
                    forwarded = type(error)(*error.args)
                except Exception:
                    forwarded = RuntimeError(str(error))
                assert future is not None
                future.set_exception(forwarded)
            else:
                if outcome.kind == OutcomeKind.SPOKE and self.maintainer is not None:
                    if time.monotonic() >= self._maintenance_retry_after:
                        try:
                            await self._maintain_when_idle()
                            self._maintenance_retry_after = 0.0
                        except Exception as error:
                            self._maintenance_retry_after = (
                                time.monotonic() + MAINTENANCE_RETRY_DELAY_SECONDS
                            )
                            emit(
                                "maintenance.failed",
                                event_id=event.id,
                                error_type=type(error).__name__,
                                retry_delay_seconds=MAINTENANCE_RETRY_DELAY_SECONDS,
                            )
                            LOGGER.exception(
                                "Memory maintenance failed after event %s; retrying in %ds",
                                event.id,
                                MAINTENANCE_RETRY_DELAY_SECONDS,
                            )
            finally:
                emit(
                    "persona.processed",
                    event_id=event.id,
                    queue_wait_ms=round((started - queued_at) * 1000),
                    duration_ms=round((time.monotonic() - started) * 1000),
                    depth=self._queue.qsize(),
                )
                self._queue.task_done()
                self._current = None
                self._retry_state = None
                self._write_runtime_state(running=True)

    async def _maintain_when_idle(self) -> None:
        if not self._queue.empty():
            return
        job = self.store.next_maintenance(now=self.clock())
        if job is not None:
            await self._run_maintenance(job)

    async def _run_maintenance(self, job, *, forced: bool = False) -> str:
        operation = (
            "digest" if isinstance(job, DigestJob)
            else "sleep" if isinstance(job, SleepJob)
            else "reflection" if isinstance(job, ReflectionJob)
            else None
        )
        if operation is not None:
            emit("maintenance.started", operation=operation, forced=forced)
        if isinstance(job, DigestJob):
            started = time.monotonic()
            async with asyncio.timeout(self.maintenance_timeout_seconds):
                content = await self.maintainer.digest(job)
            self.store.commit_digest(job, content)
            emit(
                "maintenance.completed",
                operation="digest",
                duration_ms=round((time.monotonic() - started) * 1000),
                event_count=len(job.events),
                forced=forced,
            )
            return "nap"
        elif isinstance(job, SleepJob):
            started = time.monotonic()
            async with asyncio.timeout(self.maintenance_timeout_seconds):
                draft = await self.maintainer.sleep(job)
            self.store.commit_sleep(job, draft, now=self.clock())
            if self.memory_index is not None:
                sync_started = time.monotonic()
                try:
                    available = bool(await self.memory_index.ensure())
                    emit(
                        "memory.vector_store.ready",
                        operation="sleep",
                        available=available,
                        duration_ms=round((time.monotonic() - sync_started) * 1000),
                    )
                except Exception as error:
                    emit(
                        "memory.vector_store.failed",
                        operation="sleep",
                        duration_ms=round((time.monotonic() - sync_started) * 1000),
                        error_type=type(error).__name__,
                    )
            emit(
                "maintenance.completed",
                operation="sleep",
                duration_ms=round((time.monotonic() - started) * 1000),
                event_count=len(job.events),
                forced=forced,
            )
            return "sleep"
        elif isinstance(job, ReflectionJob):
            started = time.monotonic()
            async with asyncio.timeout(self.maintenance_timeout_seconds):
                draft = await self.maintainer.reflect(job)
            self.store.commit_reflection(job, draft, now=self.clock())
            emit(
                "maintenance.completed",
                operation="reflection",
                duration_ms=round((time.monotonic() - started) * 1000),
                changed=draft.changed,
                conflict=draft.conflict,
                forced=forced,
            )
            return "reflection"
        raise TypeError(f"unsupported maintenance job: {type(job).__name__}")

    async def _process(self, event: Event, *, recovery: bool = False) -> ProcessOutcome:
        if self.store.is_handled(event):
            return ProcessOutcome(OutcomeKind.DUPLICATE, event.id)

        if recovery:
            existing = await self._retry(
                "discord.find_reply",
                event,
                lambda: self._find_existing_reply(event),
            )
            if existing is not None:
                self._commit_recovered(event, existing)
                return ProcessOutcome(
                    OutcomeKind.SPOKE,
                    event.id,
                    reply=existing.text,
                    sent_message_id=existing.id,
                )

        snapshot = self.store.load_snapshot(event)
        context = self.context_builder.build(snapshot, now=self.clock(), source_event=event)
        if context.response_delay_seconds:
            await asyncio.sleep(context.response_delay_seconds)
        draft = await self._retry(
            "openai.respond",
            event,
            lambda: self._respond_with_timeout(context),
        )
        reply_text = draft.reply
        if self.face_preparer is not None:
            reply_text = self.face_preparer.prepare_face(
                draft.reply, draft.face, draft.mood_strength
            )
        sent = await self._send_with_retries(event, reply_text, draft.images)
        self.store.commit_response(
            source=event,
            draft=draft,
            sent=sent,
            expected_version=context.state_version,
        )
        if event.is_notification:
            self.store.close_open_promise(event.id)
        if draft.research is not None:
            emit(
                "research.saved",
                event_id=sent.id,
                query_count=len(draft.research.queries),
                source_count=len(draft.research.sources),
            )
        self.store.clear_event_failure(event)
        if self.face_presenter is not None:
            try:
                async with asyncio.timeout(self.send_timeout_seconds):
                    await self.face_presenter.show_face(
                        event, sent, draft.face, draft.mood_strength, draft.reply
                    )
            except Exception as error:
                emit("face.failed", event_id=event.id, error_type=type(error).__name__)
        if self.voice is not None:
            # Voice is best-effort and starts after the durable text response.
            # A full or unavailable voice pipeline must never fail the turn.
            try:
                await self.voice.enqueue(event, draft.reply, draft.mood_strength)
            except Exception:
                LOGGER.exception("Could not enqueue voice for event %s", event.id)
        return ProcessOutcome(
            OutcomeKind.SPOKE,
            event.id,
            reply=draft.reply,
            sent_message_id=sent.id,
        )

    async def _respond_with_timeout(self, context):
        async with asyncio.timeout(self.response_timeout_seconds):
            return await self.responder.respond(context)

    async def _process_proactive(self, source: Event) -> None:
        if self.proactive_sender is None:
            raise RuntimeError("proactive message delivery is not configured")
        snapshot = self.store.load_snapshot(source)
        context = self.context_builder.build(snapshot, now=self.clock(), source_event=source, proactive=True)
        draft = await self._respond_with_timeout(context)
        if not self.proactive_allowed():
            emit("proactive.skipped", reason="mode_changed", target=source.id)
            return
        text = draft.reply
        if self.face_preparer is not None:
            text = self.face_preparer.prepare_face(
                text, draft.face, draft.mood_strength
            )
        async with asyncio.timeout(self.send_timeout_seconds):
            sent = await self.proactive_sender.send_proactive(source, text)
        self.store.commit_response(
            source=source,
            draft=draft,
            sent=sent,
            expected_version=context.state_version,
            proactive=True,
        )
        emit("proactive.sent", target=source.id, sent_message_id=sent.id)

    def _queue_reaction(self, batch):
        self._queue.put_nowait((3, next(self._sequence), batch, None, time.monotonic(), False))
        self._write_runtime_state(running=True)

    async def _send_with_retries(self, event: Event, text: str, images=()):
        started = time.monotonic()
        for attempt in range(self.max_retries + 1):
            try:
                if attempt:
                    existing = await self._find_existing_reply(event)
                    if existing is not None:
                        return existing
                async with asyncio.timeout(self.send_timeout_seconds):
                    sent = (
                        await self.sender.send(event, text, attachments=images)
                        if images else await self.sender.send(event, text)
                    )
                emit(
                    "external.completed",
                    operation="discord.send",
                    event_id=event.id,
                    duration_ms=round((time.monotonic() - started) * 1000),
                )
                return sent
            except Exception as error:
                decision = classify_retry(error, operation="discord.send")
                if not decision.retryable or attempt >= self.max_retries:
                    self._record_failure(event, "discord.send", error, decision.reason)
                    self._annotate_failure(error, "discord.send", decision.reason)
                    raise
                delay = retry_delay(
                    base_delay=self.retry_base_delay_seconds,
                    attempt=attempt + 1,
                    decision=decision,
                )
                self._emit_retry("discord.send", event, attempt + 1, error, decision.reason, delay)
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    async def _find_existing_reply(self, event: Event):
        finder = getattr(self.sender, "find_reply", None)
        if finder is None:
            return None
        async with asyncio.timeout(self.send_timeout_seconds):
            return await finder(event)

    def _commit_recovered(self, event: Event, sent) -> None:
        snapshot = self.store.load_snapshot(event)
        draft = ResponseDraft(
            reply=sent.text or "",
            mood_state=snapshot.mood.state,
            mood_cause=snapshot.mood.cause,
            mood_strength=snapshot.mood.strength,
            mood_focus=snapshot.mood.focus,
        )
        self.store.commit_response(
            source=event,
            draft=draft,
            sent=sent,
            expected_version=snapshot.version,
        )
        self.store.clear_event_failure(event)
        emit("discord.reply.recovered", event_id=event.id, message_id=sent.id)

    async def _retry(
        self,
        operation: str,
        event: Event,
        call: Callable[[], Awaitable[T]],
    ) -> T:
        for attempt in range(self.max_retries + 1):
            try:
                return await call()
            except Exception as error:
                decision = classify_retry(error, operation=operation)
                if not decision.retryable or attempt >= self.max_retries:
                    self._record_failure(event, operation, error, decision.reason)
                    self._annotate_failure(error, operation, decision.reason)
                    raise
                delay = retry_delay(
                    base_delay=self.retry_base_delay_seconds,
                    attempt=attempt + 1,
                    decision=decision,
                )
                self._emit_retry(operation, event, attempt + 1, error, decision.reason, delay)
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    def _emit_retry(
        self,
        operation: str,
        event: Event,
        attempt: int,
        error: Exception,
        reason: str,
        delay: float,
    ) -> None:
        self._retry_state = {
            "operation": operation,
            "event_id": event.id,
            "attempt": attempt,
            "reason": reason,
            "delay_seconds": delay,
        }
        self._write_runtime_state(running=True)
        emit(
            "operation.retry",
            operation=operation,
            event_id=event.id,
            attempt=attempt,
            error_type=type(error).__name__,
            reason=reason,
            delay_seconds=delay,
        )

    @staticmethod
    def _annotate_failure(error: Exception, operation: str, reason: str) -> None:
        try:
            error.operation = operation  # type: ignore[attr-defined]
            error.retry_reason = reason  # type: ignore[attr-defined]
        except Exception:
            pass

    def _record_failure(
        self, event: Event, operation: str, error: Exception, reason: str
    ) -> None:
        emit(
            "external.failed",
            operation=operation,
            event_id=event.id,
            error_type=type(error).__name__,
            reason=reason,
        )
        self.store.record_event_failure(
            event,
            operation=operation,
            error_type=type(error).__name__,
            reason=reason,
            now=self.clock(),
        )

    @staticmethod
    def _runtime_item(item: QueuePayload) -> dict[str, object] | None:
        if isinstance(item, Event):
            return {"kind": "event", "event_id": item.id, "operation": "respond"}
        if isinstance(item, ReactionBatch):
            return {"kind": "reaction", "event_ids": [event.id for event in item.events]}
        if isinstance(item, _MaintenanceRequest):
            return {"kind": "maintenance", "operation": item.kind}
        if isinstance(item, _DeletionRequest):
            return {"kind": "deletion", "event_id": item.event_id}
        return None

    def _write_runtime_state(self, *, running: bool) -> None:
        self.store._atomic_write_json(
            self.store.root / "runtime" / "actor.json",
            {
                "running": running,
                "queue_depth": self._queue.qsize(),
                "current": self._current,
                "retry": self._retry_state,
                "updated_at": self.clock().isoformat(),
            },
        )
