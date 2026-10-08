import asyncio
import threading
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from anima.core.actor import PersonaActor
from anima.core.context import ContextBuilder
from anima.core.models import OutcomeKind, SentMessage
from anima.core.reaction_queue import ReactionBatch
from anima.core.state import FileStateStore
from test_core import NOW, FakeResponder, FakeSender, FakeMaintainer
from test_sandbox import event


class KeywordError(Exception):
    def __init__(self, *, reason): super().__init__(reason)


class ActorRoutesTests(unittest.IsolatedAsyncioTestCase):
    async def test_contextual_address_guards_and_failures(self):
        source = replace(event(), mention=False)
        self.store.ensure_layout(now=NOW)
        self.assertFalse(await self.actor._contextual_address(source))
        classifier = SimpleNamespace(expects_reply=AsyncMock(return_value=True))
        self.actor.address_classifier = classifier
        for excluded in (replace(source, author_is_bot=True),
                         replace(source, kind="dm")):
            self.assertFalse(await self.actor._contextual_address(excluded))
        self.actor.contextual_reply_allowed = lambda: False
        self.assertFalse(await self.actor._contextual_address(source))
        self.actor.contextual_reply_allowed = lambda: True
        self.assertTrue(await self.actor._contextual_address(source))
        from datetime import timedelta
        history = (replace(source, id="human-before"),
                   replace(source, id="self-before", author_id="self", text="何を描こう？"),
                   replace(source, id="other-channel", author_id="self", channel_id="other"),
                   replace(source, id="old", author_id="self", ts=NOW-timedelta(seconds=601)),
                   source)
        snapshot = SimpleNamespace(recent_events=history)
        with patch.object(self.store, "load_snapshot", return_value=snapshot):
            self.assertTrue(await self.actor._contextual_address(source))
            self.assertEqual(classifier.expects_reply.call_args.args[1], history[:2])
            classifier.expects_reply.return_value = False
            self.assertFalse(await self.actor._contextual_address(source))
            for error in (ValueError("invalid"), TimeoutError()):
                classifier.expects_reply.side_effect = error
                self.assertFalse(await self.actor._contextual_address(source))
            classifier.expects_reply.side_effect = None
            classifier.expects_reply.return_value = "true"
            self.assertFalse(await self.actor._contextual_address(source))
            async def downgrade(*args):
                self.actor.contextual_reply_allowed = lambda: False
                return True
            classifier.expects_reply.side_effect = downgrade
            self.assertFalse(await self.actor._contextual_address(source))

    async def test_contextual_question_uses_normal_response_once(self):
        await self.actor.start()
        try:
            self.actor.self_time = SimpleNamespace(interrupt=AsyncMock())
            source = replace(event(), id="followup", mention=False, text="猫を描いてくれる？")
            classifier = SimpleNamespace(expects_reply=AsyncMock(return_value=True))
            self.actor.address_classifier = classifier
            self.store.append_received(replace(source, id="human-before", text="絵をお願い"))
            self.store.append_received(replace(source, id="self-before", author_id="self",
                                               text="どんな絵がいい？"))
            result = await self.actor.submit(source, allow_reactions=False)
            self.assertEqual(result.kind, OutcomeKind.SPOKE)
            self.assertEqual((await self.actor.submit(source)).kind, OutcomeKind.DUPLICATE)
            classifier.expects_reply.assert_awaited_once()
            await self.actor.submit(replace(event(), id="explicit"))
            classifier.expects_reply.assert_awaited_once()
            self.actor.self_time.interrupt.assert_awaited()
        finally:
            await self.actor.stop()

    async def test_contextual_routing_does_not_follow_other_peoples_conversation(self):
        from datetime import timedelta
        self.store.ensure_layout(now=NOW)
        source = replace(event(), mention=False, text="あのアイコンにするね")
        classifier = SimpleNamespace(expects_reply=AsyncMock(return_value=False))
        self.actor.address_classifier = classifier
        previous = replace(source, id="previous", text="Soraにも褒められちゃった")
        own = replace(source, id="self-before", author_id="self")
        histories = (
            (previous, replace(own, ts=NOW-timedelta(seconds=121))),
            (replace(previous, author_id="other"), own),
            (own,),
            (previous, own, replace(source, id="intervening", author_id="other")),
        )
        for history in histories:
            with self.subTest(history=history), patch.object(
                self.store, "load_snapshot", return_value=SimpleNamespace(recent_events=history)
            ):
                self.assertFalse(await self.actor._contextual_address(source))
        self.assertEqual(classifier.expects_reply.await_count, len(histories))
        with patch.object(self.store, "load_snapshot", return_value=SimpleNamespace(
            recent_events=(previous, own)
        )):
            self.assertFalse(await self.actor._contextual_address(replace(
                source, text="いつも素敵なアイコンありがとう💕", reply_to="other-message"
            )))
            classifier.expects_reply.return_value = True
            self.assertTrue(await self.actor._contextual_address(replace(
                source, ts=NOW+timedelta(seconds=120), text="猫を描いてくれる？"
            )))
        self.assertEqual(classifier.expects_reply.await_count, len(histories) + 2)

    async def test_contextual_address_uses_recorded_response_source(self):
        source = replace(event(), mention=False)
        a = replace(source, id='a')
        b = replace(source, id='b', author_id='another')
        own = replace(source, id='own', author_id='self', response_to='a')
        self.actor.address_classifier = SimpleNamespace(expects_reply=AsyncMock(return_value=True))
        self.store.ensure_layout(now=NOW)
        with patch.object(self.store, 'load_snapshot', return_value=SimpleNamespace(recent_events=(a, b, own))):
            self.assertTrue(await self.actor._contextual_address(source))
            self.assertEqual(self.actor.address_classifier.expects_reply.call_args.args[1], (a, b, own))
        self.actor.address_classifier.expects_reply.return_value = False
        with patch.object(self.store, 'load_snapshot', return_value=SimpleNamespace(recent_events=(a, b, replace(own, response_to='missing')))):
            self.assertFalse(await self.actor._contextual_address(source))

    async def test_unmatched_name_reaches_classifier_without_previous_conversation(self):
        await self.actor.start()
        try:
            classifier = SimpleNamespace(expects_reply=AsyncMock(return_value=True))
            self.actor.address_classifier = classifier
            source = replace(event(), id="stretched-name", mention=False, called_name=False,
                             text="Soora、おるか")
            self.assertEqual((await self.actor.submit(source, allow_reactions=False)).kind, OutcomeKind.SPOKE)
            classifier.expects_reply.assert_awaited_once()
            self.assertEqual(classifier.expects_reply.call_args.args[2].text, source.text)
        finally:
            await self.actor.stop()

    async def test_name_occurrence_is_classified_not_an_automatic_reply(self):
        await self.actor.start()
        try:
            classifier = SimpleNamespace(expects_reply=AsyncMock(return_value=False))
            self.actor.address_classifier = classifier
            source = replace(event(), id="name-mention", mention=False, called_name=True,
                             text="Soraにも褒められちゃった")
            self.assertFalse(source.requires_immediate_response)
            self.assertEqual((await self.actor.submit(source, allow_reactions=False)).kind,
                             OutcomeKind.IGNORED)
            classifier.expects_reply.assert_awaited_once()
            classifier.expects_reply.return_value = True
            self.assertEqual((await self.actor.submit(replace(
                source, id="name-call", text="Sora、いる？"
            ), allow_reactions=False)).kind, OutcomeKind.SPOKE)
        finally:
            await self.actor.stop()

    async def test_sleep_check_does_not_block_event_loop(self):
        entered = threading.Event()
        release = threading.Event()
        def slow_check(*, now):
            entered.set()
            release.wait(timeout=2)
            return False
        with patch.object(self.store, "sleep_due", side_effect=slow_check):
            task = asyncio.create_task(self.actor._schedule_sleep())
            try:
                await asyncio.wait_for(asyncio.to_thread(entered.wait, 1), timeout=1.5)
                self.assertTrue(entered.is_set())
                self.assertFalse(release.is_set())
                self.assertFalse(task.done())
            finally:
                release.set()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = FileStateStore(Path(temporary.name))
        self.actor = PersonaActor(store=self.store, context_builder=ContextBuilder(), responder=FakeResponder(), sender=FakeSender(), clock=lambda:NOW, retry_base_delay_seconds=.001)

    async def test_guards_helpers_and_independent_ambient_decisions(self):
        with self.assertRaises(RuntimeError): await self.actor.submit(event())
        await self.actor.start()
        await self.actor.start()
        try:
            self.actor.record_backfill((event(),), truncated=False)
            self.actor.mark_seen()
            self.assertEqual(self.actor.latest_event_ids(), {"100":"10"})
            self.assertEqual((await self.actor._process(event())).kind, OutcomeKind.DUPLICATE)
            await self.actor.cancel_pending_reactions()
            handler = SimpleNamespace(process=AsyncMock(return_value=replace(event(), mention=False)))
            self.actor.reactions = handler
            self.actor.proactive_allowed = lambda:True
            self.actor._process_proactive = AsyncMock()
            self.actor.reaction_buffer.delay = .001
            await self.actor.submit(replace(event(), id="ambient", mention=False))
            await asyncio.gather(*self.actor.reaction_buffer.tasks.values())
            await self.actor.wait_idle()
            self.actor._process_proactive.assert_awaited_once()
        finally: await self.actor.stop()

    async def test_worker_clones_nonstandard_errors_without_losing_worker(self):
        self.actor.maintainer = FakeMaintainer()
        await self.actor.start()
        try:
            with patch.object(self.store, "mark_deleted", side_effect=KeywordError(reason="deleted")):
                with self.assertRaisesRegex(RuntimeError, "deleted"):
                    await self.actor.mark_deleted("100", "10")
            with patch.object(self.store, "forced_maintenance", side_effect=KeywordError(reason="maintenance")):
                with self.assertRaisesRegex(RuntimeError, "maintenance"):
                    await self.actor.force_maintenance("nap")
            with patch.object(self.actor, "_process", new=AsyncMock(side_effect=KeywordError(reason="process"))):
                with self.assertRaisesRegex(RuntimeError, "process"):
                    await self.actor.submit(event())
            self.assertFalse(self.actor._worker.done())
        finally: await self.actor.stop()

    async def test_voice_face_best_effort_delay_and_proactive_prepare(self):
        self.actor.voice = SimpleNamespace(enqueue=AsyncMock())
        self.actor.face_presenter = SimpleNamespace(show_face=AsyncMock(side_effect=ValueError("deleted")))
        builder = ContextBuilder()
        self.actor.context_builder = SimpleNamespace(build=lambda *args, **kwargs:replace(builder.build(*args, **kwargs), response_delay_seconds=.001))
        await self.actor.start()
        try:
            self.assertEqual((await self.actor.submit(event())).kind, OutcomeKind.SPOKE)
            self.actor.voice.enqueue.side_effect = RuntimeError("audio offline")
            with self.assertLogs("anima.core.actor", level="ERROR"):
                self.assertEqual((await self.actor.submit(replace(event(), id="second"))).kind, OutcomeKind.SPOKE)
            self.actor.proactive_sender = None
            with self.assertRaises(RuntimeError): await self.actor._process_proactive(event())
            self.actor.proactive_sender = SimpleNamespace(send_proactive=AsyncMock(return_value=SentMessage("sent", NOW)))
            self.actor.proactive_allowed = lambda:True
            self.actor.face_preparer = SimpleNamespace(prepare_face=Mock(return_value="decorated"))
            await self.actor._process_proactive(replace(event(), id="ambient", mention=False))
            self.actor.proactive_sender.send_proactive.assert_awaited_once()
        finally: await self.actor.stop()
        self.actor.maintainer = FakeMaintainer()
        with self.assertRaises(TypeError): await self.actor._run_maintenance(object())

    async def test_send_retry_recovery_and_immutable_exception_annotation(self):
        self.store.ensure_layout(now=NOW)
        sent = SentMessage("recovered", NOW)
        self.actor.sender = SimpleNamespace(send=AsyncMock(side_effect=TimeoutError("uncertain")), find_reply=AsyncMock(return_value=sent))
        self.assertIs(await self.actor._send_with_retries(event(), "hello"), sent)
        class ImmutableError(Exception):
            def __setattr__(self, name, value): raise AttributeError(name)
        self.actor._annotate_failure(ImmutableError("test"), "test", "test")

    async def test_scheduler_handles_full_queue_and_failed_state_checks(self):
        self.store.ensure_layout(now=NOW)
        sleep_job = self.store.forced_maintenance("sleep", now=NOW)
        async def finish_sleep(_seconds): raise asyncio.CancelledError()
        with patch.object(self.store, "sleep_due", return_value=True), patch.object(self.actor._queue, "put_nowait", side_effect=asyncio.QueueFull), patch("anima.core.actor.asyncio.sleep", side_effect=finish_sleep):
            with self.assertRaises(asyncio.CancelledError): await self.actor._schedule_sleep()
        with patch.object(self.store, "sleep_due", side_effect=ValueError("corrupt")), patch("anima.core.actor.asyncio.sleep", side_effect=finish_sleep), self.assertLogs("anima.core.actor", level="ERROR"):
            with self.assertRaises(asyncio.CancelledError): await self.actor._schedule_sleep()
        self.assertGreater(self.actor._maintenance_retry_after, 0)


    async def test_idle_maintenance_waits_for_already_queued_work(self):
        self.actor._queue.put_nowait((0, 0, object(), None, 0, False))
        with patch.object(self.store, "next_maintenance") as next_job:
            await self.actor._maintain_when_idle()
            next_job.assert_not_called()
        self.actor._queue.get_nowait()
        self.actor._queue.task_done()
