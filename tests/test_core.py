from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from anima import ContextBuilder, Event, FileStateStore, PersonaActor, ResponseDraft
from anima.core.models import (
    ActionRecord,
    Context,
    ContextReference,
    DigestJob,
    MemoryDocument,
    MentionedPerson,
    Mood,
    MusicReference,
    OutcomeKind,
    ReflectionDraft,
    ReflectionJob,
    ResearchNote,
    ResearchSource,
    SentMessage,
    SleepDraft,
    SleepJob,
)


NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone(timedelta(hours=9)))


class FakeResponder:
    def __init__(self, *, delay: float = 0, research: ResearchNote | None = None) -> None:
        self.contexts: list[Context] = []
        self.delay = delay
        self.active = 0
        self.max_active = 0
        self.research = research
        self.music = ()
        self.images = ()
        self.actions = ()
        self.references = ()

    async def respond(self, context: Context) -> ResponseDraft:
        self.contexts.append(context)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(self.delay)
        self.active -= 1
        return ResponseDraft(
            reply="おかえり。",
            mood_state="うれしい",
            mood_cause="呼んでもらった",
            mood_strength="ふつう",
            mood_focus="何をしていたのか気になる",
            research=self.research,
            music=self.music,
            images=self.images,
            actions=self.actions,
            references=self.references,
        )


class FlakyResponder(FakeResponder):
    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures
        self.attempts = 0

    async def respond(self, context: Context) -> ResponseDraft:
        self.attempts += 1
        if self.attempts <= self.failures:
            raise TimeoutError("temporary OpenAI failure")
        return await super().respond(context)


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[Event, str]] = []
        self.attachments = []

    async def send(self, source: Event, text: str, *, attachments=()) -> SentMessage:
        self.sent.append((source, text))
        self.attachments.append(tuple(attachments))
        return SentMessage(id=f"bot-{source.id}", timestamp=NOW + timedelta(seconds=len(self.sent)))


class FailingSender:
    def __init__(self) -> None:
        self.attempts = 0

    async def send(self, source: Event, text: str) -> SentMessage:
        self.attempts += 1
        raise RuntimeError("Discord is unavailable")


class FlakySender(FakeSender):
    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures
        self.attempts = 0

    async def send(self, source: Event, text: str) -> SentMessage:
        self.attempts += 1
        if self.attempts <= self.failures:
            raise TimeoutError("temporary Discord failure")
        return await super().send(source, text)

    async def find_reply(self, source: Event) -> SentMessage | None:
        return None


class RecoveringSender(FakeSender):
    def __init__(self, recovered: SentMessage) -> None:
        super().__init__()
        self.recovered = recovered

    async def find_reply(self, source: Event) -> SentMessage | None:
        return self.recovered


class FakeMaintainer:
    def __init__(self) -> None:
        self.digest_jobs: list[DigestJob] = []
        self.sleep_jobs: list[SleepJob] = []
        self.reflection_jobs: list[ReflectionJob] = []

    async def digest(self, job: DigestJob) -> str:
        self.digest_jobs.append(job)
        return "- [12:00 #general] 太郎が帰宅し、ペルソナが迎えた"

    async def sleep(self, job: SleepJob) -> SleepDraft:
        self.sleep_jobs.append(job)
        return SleepDraft(
            memories=(
                MemoryDocument("self", "self", "- [2026-09-01 #general] 太郎を迎えた"),
                MemoryDocument("world", "world", job.world_memory),
                MemoryDocument("person", "u1", "- [2026-09-01 #general] 帰宅を知らせてくれた"),
            ),
            open_items="- [2026-09-01 #general] 太郎と明日また話す",
            mood_state="すっきりしている",
            mood_cause="眠って一日を整理した",
            mood_strength="弱い",
            mood_focus="太郎との次の話",
        )

    async def reflect(self, job: ReflectionJob) -> ReflectionDraft:
        self.reflection_jobs.append(job)
        return ReflectionDraft(True, "- 人の話は最後まで聞く", None)


class FailingMaintainer(FakeMaintainer):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def digest(self, job: DigestJob) -> str:
        self.calls += 1
        raise RuntimeError("maintenance unavailable")


class SlowMaintainer(FakeMaintainer):
    async def digest(self, job: DigestJob) -> str:
        await asyncio.sleep(0.05)
        return await super().digest(job)


def event(identifier: str, *, channel: str = "general", mention: bool = False) -> Event:
    return Event(
        id=identifier,
        ts=NOW,
        kind="channel",
        channel_id=channel,
        channel_name=f"#{channel}",
        author_id="u1",
        author_name="太郎",
        text="ただいま",
        mention=mention,
    )


class CoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = FileStateStore(self.root)
        self.responder = FakeResponder()
        self.sender = FakeSender()
        self.actor = PersonaActor(
            store=self.store,
            context_builder=ContextBuilder(),
            responder=self.responder,
            sender=self.sender,
            clock=lambda: NOW,
        )
        await self.actor.start()

    async def asyncTearDown(self) -> None:
        await self.actor.stop()
        self.temporary.cleanup()

    async def test_unaddressed_event_is_logged_but_ignored(self) -> None:
        outcome = await self.actor.submit(event("m1"))

        self.assertEqual(outcome.kind, OutcomeKind.IGNORED)
        self.assertEqual([item.id for item in self.store.read_channel_events("general")], ["m1"])
        self.assertEqual(self.responder.contexts, [])
        self.assertEqual(self.sender.sent, [])

    async def test_completed_job_closes_promise_only_after_response_commit(self) -> None:
        requested = event("request", mention=True)
        self.store.add_open_promise("j-123456789abc", requested, "猫を描く")
        completion = replace(
            requested, id="j-123456789abc", author_id="self", author_name="システム",
            response_target=MentionedPerson(requested.author_id, requested.author_name),
            mention=False, author_is_bot=True,
            actions=(ActionRecord(
                "drawing", "job_completed", "画像生成ジョブが完了した",
            ),),
        )

        await self.actor.submit(completion)

        self.assertNotIn("j-123456789abc", (self.root / "open.md").read_text())

    async def test_delete_is_serialized_and_removed_from_later_context(self) -> None:
        await self.actor.submit(replace(event("deleted"), text="消した発言"))
        self.assertTrue(await self.actor.mark_deleted("general", "deleted"))
        self.assertFalse(await self.actor.mark_deleted("general", "deleted"))
        with self.assertRaises(ValueError):
            await self.actor.mark_deleted("../general", "deleted")

        await self.actor.submit(event("current", mention=True))

        rendered = str(self.responder.contexts[-1].input)
        self.assertNotIn("deleted", rendered)
        self.assertNotIn("消した発言", rendered)
        self.assertTrue(self.store.is_deleted("general", "deleted"))

    async def test_delete_requires_started_actor(self) -> None:
        await self.actor.stop()
        with self.assertRaisesRegex(RuntimeError, "start"):
            await self.actor.mark_deleted("general", "deleted")

    async def test_activity_downgrade_cancels_pending_reactions(self) -> None:
        self.actor.reaction_buffer.stop = AsyncMock()

        await self.actor.cancel_pending_reactions()

        self.actor.reaction_buffer.stop.assert_awaited_once()

    async def test_observation_can_record_context_without_reaction(self) -> None:
        self.actor.reactions = SimpleNamespace(
            faces=SimpleNamespace(available={"joy"})
        )
        self.actor.reaction_buffer.add = MagicMock()

        await self.actor.submit(event("observed"), allow_reactions=False)
        self.actor.reaction_buffer.add.assert_not_called()
        await self.actor.submit(event("reactable"))

        self.actor.reaction_buffer.add.assert_called_once()
        self.assertEqual(
            [item.id for item in self.store.read_channel_events("general")],
            ["observed", "reactable"],
        )

    async def test_proactive_response_is_a_channel_message_and_rechecks_mode(self) -> None:
        source = event("proactive-source")
        await self.actor.submit(source)
        sent = SentMessage("proactive-sent", NOW, "こっそり混ざる。")
        self.sender.send_proactive = AsyncMock(return_value=sent)
        self.actor.proactive_sender = self.sender
        self.actor.proactive_allowed = lambda: True

        await self.actor._process_proactive(source)

        self.sender.send_proactive.assert_awaited_once()
        response = self.store.read_channel_events("general")[-1]
        self.assertEqual(response.id, "proactive-sent")
        self.assertIsNone(response.reply_to)
        self.assertIn("proactive", response.acts)

        blocked = event("blocked-source")
        await self.actor.submit(blocked)
        self.actor.proactive_allowed = lambda: False
        await self.actor._process_proactive(blocked)
        self.sender.send_proactive.assert_awaited_once()

    async def test_forced_nap_runs_without_waiting_for_threshold(self) -> None:
        maintainer = FakeMaintainer()
        self.actor.maintainer = maintainer
        await self.actor.submit(event("manual-nap"))

        operation = await self.actor.force_maintenance("nap")

        self.assertEqual(operation, "nap")
        self.assertEqual(len(maintainer.digest_jobs), 1)
        self.assertEqual(maintainer.digest_jobs[0].events[0].id, "manual-nap")
        self.assertIn("太郎が帰宅", (self.root / "digest.md").read_text())

    async def test_forced_maintenance_rejects_empty_nap_and_unknown_kind(self) -> None:
        self.actor.maintainer = FakeMaintainer()
        with self.assertRaisesRegex(ValueError, "新しいログ"):
            await self.actor.force_maintenance("nap")
        with self.assertRaisesRegex(ValueError, "unknown maintenance kind"):
            await self.actor.force_maintenance("unknown")

    async def test_forced_reflection_runs_through_actor_queue(self) -> None:
        maintainer = FakeMaintainer()
        self.actor.maintainer = maintainer

        operation = await self.actor.force_maintenance("reflection")

        self.assertEqual(operation, "reflection")
        self.assertEqual(len(maintainer.reflection_jobs), 1)
        self.assertIn("人の話は最後まで聞く", (self.root / "habitus.md").read_text())

    async def test_forced_sleep_runs_without_waiting_for_day_boundary(self) -> None:
        maintainer = FakeMaintainer()
        self.actor.maintainer = maintainer
        await self.actor.submit(
            replace(event("manual-sleep"), ts=NOW + timedelta(seconds=1))
        )

        operation = await self.actor.force_maintenance("sleep")

        self.assertEqual(operation, "sleep")
        self.assertEqual(len(maintainer.sleep_jobs), 1)
        self.assertEqual(maintainer.sleep_jobs[0].events[0].id, "manual-sleep")
        self.assertIn("太郎を迎えた", (self.root / "memory" / "self.md").read_text())

    async def test_forced_maintenance_requires_started_configured_actor(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "not configured"):
            await self.actor.force_maintenance("sleep")
        await self.actor.stop()
        self.actor.maintainer = FakeMaintainer()
        with self.assertRaisesRegex(RuntimeError, "start"):
            await self.actor.force_maintenance("sleep")

    async def test_forced_self_time_requires_started_configured_and_available_run(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "not configured"):
            await self.actor.force_self_time()
        self.actor.self_time = SimpleNamespace(tick=AsyncMock(return_value=()))
        with self.assertRaisesRegex(ValueError, "別の処理中"):
            await self.actor.force_self_time()
        decision = SimpleNamespace(action="none")
        self.actor.self_time.tick.return_value = (decision,)
        self.assertEqual(await self.actor.force_self_time(), (decision,))
        self.actor.self_time.tick.assert_awaited_with(forced=True)
        await self.actor.stop()
        with self.assertRaisesRegex(RuntimeError, "start"):
            await self.actor.force_self_time()

    async def test_duplicate_event_is_not_processed_twice(self) -> None:
        first = await self.actor.submit(event("m1"))
        second = await self.actor.submit(event("m1"))

        self.assertEqual(first.kind, OutcomeKind.IGNORED)
        self.assertEqual(second.kind, OutcomeKind.DUPLICATE)
        self.assertEqual(len(self.store.read_channel_events("general")), 1)

    async def test_addressed_event_commits_reply_and_mood_after_send(self) -> None:
        self.actor.face_preparer = SimpleNamespace(
            prepare_face=lambda text, face, strength: text + " <:face:1>"
        )
        outcome = await self.actor.submit(event("m2", mention=True))

        self.assertEqual(outcome.kind, OutcomeKind.SPOKE)
        self.assertEqual(outcome.sent_message_id, "bot-m2")
        self.assertEqual(self.sender.sent[0][1], "おかえり。 <:face:1>")
        events = self.store.read_channel_events("general")
        self.assertEqual([item.id for item in events], ["m2", "bot-m2"])
        self.assertIsNone(events[-1].reply_to)
        snapshot = self.store.load_snapshot(event("next", mention=True))
        self.assertEqual(snapshot.version, 1)
        self.assertEqual(snapshot.mood.state, "うれしい")
        self.assertEqual(snapshot.last_spoke_at, NOW + timedelta(seconds=1))
        self.assertIn("太郎 (たった今): ただいま", str(self.responder.contexts[0].input))

        replied = replace(event("m3"), reply_to_self=True)
        await self.actor.submit(replied)
        self.assertEqual(
            self.store.read_channel_events("general")[-1].reply_to,
            "m3",
        )

    async def test_generated_drawing_is_attached_to_the_same_response(self) -> None:
        image = self.root / "world" / "drawing.png"
        self.responder.images = (image,)

        await self.actor.submit(event("drawing", mention=True))

        self.assertEqual(self.sender.attachments[-1], (image,))

    async def test_search_research_is_committed_and_emitted(self) -> None:
        self.responder.research = ResearchNote(
            "調べた事実",
            ("検索語",),
            (ResearchSource("出典", "https://example.test/source"),),
        )
        with patch("anima.core.actor.emit") as emitted:
            await self.actor.submit(event("searched", mention=True))

        response = self.store.read_channel_events("general")[-1]
        self.assertEqual(response.research, self.responder.research)
        emitted.assert_any_call(
            "research.saved",
            event_id="bot-searched",
            query_count=1,
            source_count=1,
        )

    async def test_selected_music_metadata_is_committed_on_bot_response(self) -> None:
        self.responder.music = (MusicReference(
            "cat", "猫の歌", 90, ("pop",), "https://example.test/cat"
        ),)

        await self.actor.submit(event("music-selected", mention=True))

        response = self.store.read_channel_events("general")[-1]
        self.assertEqual(response.music, self.responder.music)
        next_context = ContextBuilder().build(
            self.store.load_snapshot(event("next", mention=True)), now=NOW
        )
        self.assertIn("正規URL: https://example.test/cat", str(next_context.input))

    async def test_capability_metadata_is_committed_on_bot_response(self) -> None:
        self.responder.actions = (ActionRecord("music", "search_music", "曲を検索した"),)
        self.responder.references = (
            ContextReference("music", "track", "猫の歌", (("id", "cat"),)),
        )

        await self.actor.submit(event("capability", mention=True))

        response = self.store.read_channel_events("general")[-1]
        self.assertEqual(response.actions, self.responder.actions)
        self.assertEqual(response.references, self.responder.references)
        next_context = ContextBuilder().build(
            self.store.load_snapshot(event("next-capability", mention=True)), now=NOW
        )
        self.assertIn("music.search_music: 曲を検索した", str(next_context.input))

    async def test_habitus_is_injected_after_rules(self) -> None:
        (self.root / "rules.md").write_text("自然に話す\n", encoding="utf-8")
        (self.root / "habitus.md").write_text(
            "- 人の話は最後まで聞く\n", encoding="utf-8"
        )

        await self.actor.submit(event("habitus", mention=True))

        instructions = self.responder.contexts[-1].instructions
        self.assertLess(
            instructions.index("存在のしかた"),
            instructions.index("身についたこと"),
        )
        self.assertLess(
            instructions.index("身についたこと"),
            instructions.index("いまの自分"),
        )

    async def test_context_only_contains_current_channel(self) -> None:
        await self.actor.submit(event("other", channel="kitchen"))
        await self.actor.submit(event("target", channel="general", mention=True))

        rendered = str(self.responder.contexts[-1].input)
        self.assertIn("target", str(self.store.read_channel_events("general")))
        self.assertNotIn("#kitchen", rendered)

    async def test_dm_memory_is_marked_private_in_public_context(self) -> None:
        people = self.root / "memory" / "people"
        people.mkdir(parents=True, exist_ok=True)
        (people / "u1.md").write_text(
            "## 太郎\n- [2026-08-31 DM] 転職を考えている\n",
            encoding="utf-8",
        )

        await self.actor.submit(event("public", mention=True))

        instructions = self.responder.contexts[-1].instructions
        self.assertIn("転職を考えている  ※この場では触れない", instructions)

    async def test_dm_memory_is_not_marked_private_inside_dm(self) -> None:
        people = self.root / "memory" / "people"
        people.mkdir(parents=True, exist_ok=True)
        (people / "u1.md").write_text(
            "## 太郎\n- [2026-08-31 DM] 転職を考えている\n",
            encoding="utf-8",
        )
        direct = Event(
            id="dm1",
            ts=NOW,
            kind="dm",
            channel_id="dm",
            channel_name="DM",
            author_id="u1",
            author_name="太郎",
            text="続きだけど",
        )

        await self.actor.submit(direct)

        instructions = self.responder.contexts[-1].instructions
        self.assertIn("[2026-08-31 DM] 転職を考えている", instructions)
        self.assertNotIn("この場では触れない", instructions)

    async def test_person_gap_is_computed_across_channels(self) -> None:
        old = Event(
            id="old",
            ts=NOW - timedelta(days=2),
            kind="channel",
            channel_id="kitchen",
            channel_name="#kitchen",
            author_id="u1",
            author_name="太郎",
            text="またね",
        )
        await self.actor.submit(old)

        await self.actor.submit(event("current", channel="general", mention=True))

        instructions = self.responder.contexts[-1].instructions
        self.assertIn("太郎と最後にやりとりしてから: 2日0時間", instructions)


class ValueTests(unittest.TestCase):
    def test_mentioned_person_round_trip_validation_and_legacy_log(self) -> None:
        person = MentionedPerson("u2", "花子")
        original = replace(event("introduction"), mentioned_people=(person,))
        self.assertEqual(Event.from_log_dict(original.to_log_dict()), original)
        legacy = original.to_log_dict()
        legacy.pop("mentioned_people")
        self.assertEqual(Event.from_log_dict(legacy).mentioned_people, ())
        for person_id, name in (("../u2", "花子"), ("u2", ""), ("u2", "花\n子")):
            with self.subTest(person_id=person_id, name=name), self.assertRaises(ValueError):
                MentionedPerson(person_id, name)

    def test_music_reference_round_trip_and_validation(self) -> None:
        reference = MusicReference(
            "cat", "猫の歌", 90, ("pop",), "https://example.test/cat"
        )
        original = replace(event("music"), music=(reference,))
        self.assertEqual(Event.from_log_dict(original.to_log_dict()), original)
        legacy = original.to_log_dict()
        legacy["music"] = reference.to_dict()
        self.assertEqual(Event.from_log_dict(legacy).music, (reference,))
        with self.assertRaises(ValueError):
            MusicReference("../cat", "猫", 1, (), "https://example.test/cat")
        with self.assertRaises(ValueError):
            MusicReference("cat", "", 1, (), "https://example.test/cat")
        with self.assertRaises(ValueError):
            MusicReference("cat", "猫", 1, (), "file:///cat")

    def test_research_note_round_trip_and_bounds(self) -> None:
        note = ResearchNote(
            "検索結果の要約",
            ("検索語",),
            (ResearchSource("出典", "https://example.test/source"),),
        )
        original = replace(event("research"), research=note)
        self.assertEqual(Event.from_log_dict(original.to_log_dict()), original)
        for create, message in (
            (lambda: ResearchNote(""), "summary"),
            (lambda: ResearchNote("要約", ("",)), "queries"),
            (lambda: ResearchNote("要約", ("a", "b", "c", "d")), "queries"),
            (lambda: ResearchNote("要約", sources=(ResearchSource("出典", "https://example.test"),) * 4), "sources"),
            (lambda: ResearchSource("", "https://example.test"), "title"),
            (lambda: ResearchSource("出典", "file:///tmp/source"), "HTTP"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                create()

    def test_runtime_item_ignores_shutdown_sentinel(self) -> None:
        self.assertIsNone(PersonaActor._runtime_item(None))

    def test_event_requires_safe_identifiers_and_timezone(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsafe"):
            Event(
                id="m1",
                ts=NOW,
                kind="channel",
                channel_id="../outside",
                channel_name="#general",
                author_id="u1",
                author_name="太郎",
                text="test",
            )
        with self.assertRaisesRegex(ValueError, "timezone"):
            Event(
                id="m1",
                ts=datetime(2026, 9, 1),
                kind="channel",
                channel_id="general",
                channel_name="#general",
                author_id="u1",
                author_name="太郎",
                text="test",
            )
        with self.assertRaisesRegex(ValueError, "sandbox_key"):
            replace(event("m1"), sandbox_key="discord:guild:1")

    def test_dm_and_name_call_are_addressed_to_self(self) -> None:
        direct_message = Event(
            id="dm1",
            ts=NOW,
            kind="dm",
            channel_id="dm",
            channel_name="DM",
            author_id="u1",
            author_name="太郎",
            text="やあ",
        )
        name_call = Event(
            id="m1",
            ts=NOW,
            kind="channel",
            channel_id="general",
            channel_name="#general",
            author_id="u1",
            author_name="太郎",
            text="ペルソナ、いる？",
            called_name=True,
        )

        self.assertTrue(direct_message.addressed_to_self)
        self.assertFalse(name_call.addressed_to_self)
        self.assertFalse(name_call.requires_immediate_response)
        bot_call = replace(name_call, author_is_bot=True)
        self.assertFalse(bot_call.directed_to_agent)
        self.assertFalse(bot_call.requires_immediate_response)
        self.assertEqual(Event.from_log_dict(bot_call.to_log_dict()), bot_call)
        self.assertTrue(direct_message.directed_to_agent)
        self.assertTrue(direct_message.is_private)
        self.assertFalse(name_call.is_private)
        self.assertEqual(name_call.conversation_id, "general")
        self.assertIsNone(name_call.audio_destination_id)


class SerializationTests(unittest.IsolatedAsyncioTestCase):
    async def test_scheduled_sleep_runs_without_a_message_and_only_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = FileStateStore(root)
            store.ensure_layout(now=NOW)
            store.append_received(event("m1", mention=True))
            maintainer = FakeMaintainer()
            actor = PersonaActor(
                store=store, context_builder=ContextBuilder(), responder=FakeResponder(),
                sender=FakeSender(), maintainer=maintainer,
                clock=lambda: NOW + timedelta(days=1),
            )
            with patch("anima.core.actor.SLEEP_CHECK_INTERVAL_SECONDS", 0.01):
                await actor.start()
                try:
                    for _ in range(100):
                        if maintainer.sleep_jobs:
                            break
                        await asyncio.sleep(0.01)
                    self.assertEqual(len(maintainer.sleep_jobs), 1)
                    await actor.wait_idle()
                    await asyncio.sleep(0.04)
                    self.assertEqual(len(maintainer.sleep_jobs), 1)
                finally:
                    await actor.stop()

    async def test_scheduled_sleep_failure_backs_off(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = FileStateStore(root)
            store.ensure_layout(now=NOW)
            maintainer = FakeMaintainer()
            maintainer.sleep = AsyncMock(side_effect=RuntimeError("unavailable"))
            actor = PersonaActor(
                store=store, context_builder=ContextBuilder(), responder=FakeResponder(),
                sender=FakeSender(), maintainer=maintainer,
                clock=lambda: NOW + timedelta(days=1),
            )
            with patch("anima.core.actor.SLEEP_CHECK_INTERVAL_SECONDS", 0.01):
                await actor.start()
                try:
                    for _ in range(100):
                        if maintainer.sleep.await_count:
                            break
                        await asyncio.sleep(0.01)
                    await actor.wait_idle()
                    await asyncio.sleep(0.04)
                    self.assertEqual(maintainer.sleep.await_count, 1)
                    self.assertGreater(actor._maintenance_retry_after, 0)
                finally:
                    await actor.stop()

    async def test_actor_runtime_snapshot_tracks_queue_current_retry_and_stop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            actor = PersonaActor(
                store=FileStateStore(root),
                context_builder=ContextBuilder(),
                responder=FlakyResponder(1),
                sender=FakeSender(),
                clock=lambda: NOW,
                retry_base_delay_seconds=0.05,
            )
            await actor.start()
            first = asyncio.create_task(actor.submit(event("m1", mention=True)))
            await asyncio.sleep(0.01)
            second = asyncio.create_task(actor.submit(event("m2", mention=True)))
            await asyncio.sleep(0.01)
            active = json.loads((root / "runtime" / "actor.json").read_text())
            self.assertTrue(active["running"])
            self.assertEqual(active["current"]["event_id"], "m1")
            self.assertEqual(active["queue_depth"], 1)
            self.assertEqual(active["retry"]["operation"], "openai.respond")
            await asyncio.gather(first, second)
            idle = json.loads((root / "runtime" / "actor.json").read_text())
            self.assertIsNone(idle["current"])
            self.assertIsNone(idle["retry"])
            await actor.stop()
            stopped = json.loads((root / "runtime" / "actor.json").read_text())
            self.assertFalse(stopped["running"])

    async def test_persona_responses_are_serialized(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            responder = FakeResponder(delay=0.02)
            actor = PersonaActor(
                store=FileStateStore(Path(temporary)),
                context_builder=ContextBuilder(),
                responder=responder,
                sender=FakeSender(),
                clock=lambda: NOW,
            )
            await actor.start()
            try:
                await asyncio.gather(
                    actor.submit(event("m1", mention=True)),
                    actor.submit(event("m2", mention=True)),
                )
            finally:
                await actor.stop()

            self.assertEqual(responder.max_active, 1)

    async def test_dm_overtakes_lower_priority_addressed_messages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            sender = FakeSender()
            actor = PersonaActor(
                store=FileStateStore(Path(temporary)),
                context_builder=ContextBuilder(),
                responder=FakeResponder(delay=0.02),
                sender=sender,
                clock=lambda: NOW,
            )
            await actor.start()
            first = asyncio.create_task(actor.submit(event("first", mention=True)))
            await asyncio.sleep(0.002)
            lower = asyncio.create_task(
                actor.submit(
                    Event(
                        id="lower",
                        ts=NOW,
                        kind="channel",
                        channel_id="general",
                        channel_name="#general",
                        author_id="u1",
                        author_name="太郎",
                        text="ペルソナ",
                        called_name=True,
                        mention=True,
                    )
                )
            )
            direct = asyncio.create_task(
                actor.submit(
                    Event(
                        id="direct",
                        ts=NOW,
                        kind="dm",
                        channel_id="dm",
                        channel_name="DM",
                        author_id="u1",
                        author_name="太郎",
                        text="急ぎ",
                    )
                )
            )
            try:
                await asyncio.gather(first, lower, direct)
            finally:
                await actor.stop()

            self.assertEqual([source.id for source, _ in sender.sent], ["first", "direct", "lower"])


class FailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_openai_response_is_retried_at_event_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            responder = FlakyResponder(failures=1)
            actor = PersonaActor(
                store=FileStateStore(Path(temporary)),
                context_builder=ContextBuilder(),
                responder=responder,
                sender=FakeSender(),
                clock=lambda: NOW,
                max_retries=2,
                retry_base_delay_seconds=0.001,
            )
            await actor.start()
            try:
                outcome = await actor.submit(event("m1", mention=True))
            finally:
                await actor.stop()

            self.assertEqual(outcome.kind, OutcomeKind.SPOKE)
            self.assertEqual(responder.attempts, 2)

    async def test_send_failure_does_not_commit_reply_or_mood(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = FileStateStore(Path(temporary))
            sender = FailingSender()
            actor = PersonaActor(
                store=store,
                context_builder=ContextBuilder(),
                responder=FakeResponder(),
                sender=sender,
                clock=lambda: NOW,
            )
            await actor.start()
            initial_mood = store.load_snapshot(event("initial")).mood
            try:
                with self.assertRaisesRegex(RuntimeError, "Discord is unavailable"):
                    await actor.submit(event("m1", mention=True))
            finally:
                await actor.stop()

            snapshot = store.load_snapshot(event("next"))
            self.assertEqual(snapshot.version, 0)
            self.assertEqual(snapshot.mood, initial_mood)
            self.assertEqual([item.id for item in store.read_channel_events("general")], ["m1"])
            self.assertEqual(sender.attempts, 1)
            self.assertEqual(store.event_failures()["m1"]["operation"], "discord.send")
            self.assertEqual(store.event_failures()["m1"]["reason"], "non_retryable")

    async def test_response_timeout_leaves_only_received_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = FileStateStore(Path(temporary))
            sender = FakeSender()
            actor = PersonaActor(
                store=store,
                context_builder=ContextBuilder(),
                responder=FakeResponder(delay=0.05),
                sender=sender,
                clock=lambda: NOW,
                response_timeout_seconds=0.001,
            )
            await actor.start()
            try:
                with self.assertRaises(TimeoutError):
                    await actor.submit(event("m1", mention=True))
            finally:
                await actor.stop()

            self.assertEqual(store.load_snapshot(event("next")).version, 0)
            self.assertEqual([item.id for item in store.read_channel_events("general")], ["m1"])
            self.assertEqual(sender.sent, [])
            self.assertEqual([item.id for item in store.pending_addressed_events()], ["m1"])

    async def test_discord_send_is_retried_without_regenerating_reply(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            responder = FakeResponder()
            sender = FlakySender(failures=1)
            actor = PersonaActor(
                store=FileStateStore(Path(temporary)),
                context_builder=ContextBuilder(),
                responder=responder,
                sender=sender,
                clock=lambda: NOW,
                max_retries=2,
                retry_base_delay_seconds=0.001,
            )
            await actor.start()
            try:
                outcome = await actor.submit(event("m1", mention=True))
            finally:
                await actor.stop()

            self.assertEqual(outcome.kind, OutcomeKind.SPOKE)
            self.assertEqual(sender.attempts, 2)
            self.assertEqual(len(responder.contexts), 1)

    async def test_recovery_uses_existing_discord_reply_without_resending(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = FileStateStore(root)
            store.ensure_layout(now=NOW)
            source = event("m1", mention=True)
            store.append_received(source)
            sender = RecoveringSender(
                SentMessage(id="existing", timestamp=NOW, text="もう送ったよ")
            )
            responder = FakeResponder()
            actor = PersonaActor(
                store=store,
                context_builder=ContextBuilder(),
                responder=responder,
                sender=sender,
                clock=lambda: NOW,
            )
            await actor.start()
            try:
                outcome = await actor.submit(source)
            finally:
                await actor.stop()

            self.assertEqual(outcome.sent_message_id, "existing")
            self.assertEqual(responder.contexts, [])
            self.assertEqual(sender.sent, [])
            self.assertEqual(store.pending_addressed_events(), ())

    async def test_pending_event_can_be_replayed_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_store = FileStateStore(root)
            first_actor = PersonaActor(
                store=first_store,
                context_builder=ContextBuilder(),
                responder=FakeResponder(delay=0.05),
                sender=FakeSender(),
                clock=lambda: NOW,
                response_timeout_seconds=0.001,
            )
            await first_actor.start()
            try:
                with self.assertRaises(TimeoutError):
                    await first_actor.submit(event("m1", mention=True))
            finally:
                await first_actor.stop()

            second_store = FileStateStore(root)
            second_actor = PersonaActor(
                store=second_store,
                context_builder=ContextBuilder(),
                responder=FakeResponder(),
                sender=FakeSender(),
                clock=lambda: NOW,
            )
            await second_actor.start()
            try:
                pending = second_actor.pending_events()
                self.assertEqual([item.id for item in pending], ["m1"])
                outcome = await second_actor.submit(pending[0])
            finally:
                await second_actor.stop()

            self.assertEqual(outcome.kind, OutcomeKind.SPOKE)
            self.assertEqual(second_store.pending_addressed_events(), ())
            self.assertEqual(
                [item.id for item in second_store.read_channel_events("general")],
                ["m1", "bot-m1"],
            )


class MaintenanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_weekly_reflection_updates_habitus(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = FileStateStore(root)
            store.ensure_layout(now=NOW)
            cursor_path = root / "cursor.json"
            cursor = json.loads(cursor_path.read_text(encoding="utf-8"))
            cursor["reflected_at"] = (NOW - timedelta(days=8)).isoformat()
            cursor_path.write_text(json.dumps(cursor), encoding="utf-8")
            actor = PersonaActor(
                store=store,
                context_builder=ContextBuilder(),
                responder=FakeResponder(),
                sender=FakeSender(),
                maintainer=FakeMaintainer(),
                clock=lambda: NOW,
            )
            await actor.start()
            try:
                await actor.submit(event("reflect", mention=True))
                await actor.wait_idle()
            finally:
                await actor.stop()

            self.assertEqual(
                (root / "habitus.md").read_text(encoding="utf-8"),
                "- 人の話は最後まで聞く\n",
            )
            cursor = json.loads((root / "cursor.json").read_text(encoding="utf-8"))
            self.assertEqual(cursor["reflected_at"], NOW.isoformat())

    async def test_digest_runs_after_response_when_window_overflows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = FileStateStore(Path(temporary), recent_limit=1)
            maintainer = FakeMaintainer()
            actor = PersonaActor(
                store=store,
                context_builder=ContextBuilder(),
                responder=FakeResponder(),
                sender=FakeSender(),
                maintainer=maintainer,
                clock=lambda: NOW,
            )
            await actor.start()
            try:
                await actor.submit(event("m1", mention=True))
                await actor.wait_idle()
            finally:
                await actor.stop()

            self.assertEqual(len(maintainer.digest_jobs), 1)
            self.assertIn("太郎が帰宅", (Path(temporary) / "digest.md").read_text())
            cursor = json.loads((Path(temporary) / "cursor.json").read_text())
            self.assertEqual(cursor["version"], 2)
            self.assertIsNotNone(cursor["digested_until"])

    async def test_sleep_replaces_memories_open_items_and_mood(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = FileStateStore(root, recent_limit=30)
            maintainer = FakeMaintainer()
            memory_index = SimpleNamespace(ensure=AsyncMock(return_value="vs_1"))
            clock_values = iter((NOW, NOW + timedelta(hours=25), NOW + timedelta(hours=25)))
            last = NOW + timedelta(hours=25)

            def clock() -> datetime:
                return next(clock_values, last)

            actor = PersonaActor(
                store=store,
                context_builder=ContextBuilder(),
                responder=FakeResponder(),
                sender=FakeSender(),
                maintainer=maintainer,
                memory_index=memory_index,
                clock=clock,
            )
            await actor.start()
            try:
                await actor.submit(event("m1", mention=True))
                await actor.wait_idle()
            finally:
                await actor.stop()

            self.assertEqual(len(maintainer.sleep_jobs), 1)
            self.assertIn("太郎を迎えた", (root / "memory" / "self.md").read_text())
            self.assertIn("帰宅を知らせて", (root / "memory" / "people" / "u1.md").read_text())
            self.assertIn("明日また話す", (root / "open.md").read_text())
            self.assertEqual((root / "digest.md").read_text(), "")
            snapshot = store.load_snapshot(event("next"))
            self.assertEqual(snapshot.mood.state, "すっきりしている")
            self.assertEqual(snapshot.version, 2)
            memory_index.ensure.assert_awaited_once_with()

    async def test_sleep_sync_failure_does_not_undo_local_memory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = FileStateStore(root, recent_limit=30)
            memory_index = SimpleNamespace(
                ensure=AsyncMock(side_effect=RuntimeError("vector unavailable"))
            )
            job = SleepJob(
                expected_version=0, events=(event("m1"),), digest="", open_items="",
                self_memory="", world_memory="", channel_memories=(),
                people_memories=(("u1", ""),),
                persona="", rules="", habitus="",
                mood=Mood("穏やか", "特にない", "弱い", "", NOW),
            )
            actor = PersonaActor(
                store=store, context_builder=ContextBuilder(), responder=FakeResponder(),
                sender=FakeSender(), maintainer=FakeMaintainer(), memory_index=memory_index,
                clock=lambda: NOW,
            )
            store.ensure_layout(now=NOW)

            with self.assertLogs("anima.telemetry", level="INFO") as captured:
                self.assertEqual(await actor._run_maintenance(job), "sleep")

            self.assertIn("太郎を迎えた", (root / "memory" / "self.md").read_text())
            self.assertIn('"event":"memory.vector_store.failed"', "\n".join(captured.output))

    async def test_maintenance_failure_does_not_stop_persona_actor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            maintainer = FailingMaintainer()
            actor = PersonaActor(
                store=FileStateStore(Path(temporary), recent_limit=1),
                context_builder=ContextBuilder(),
                responder=FakeResponder(),
                sender=FakeSender(),
                maintainer=maintainer,
                clock=lambda: NOW,
            )
            await actor.start()
            try:
                first = await actor.submit(event("m1", mention=True))
                await actor.wait_idle()
                second = await actor.submit(event("m2", mention=True))
            finally:
                await actor.stop()

            self.assertEqual(first.kind, OutcomeKind.SPOKE)
            self.assertEqual(second.kind, OutcomeKind.SPOKE)
            self.assertEqual(maintainer.calls, 1)

    async def test_maintenance_timeout_does_not_stop_persona_actor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            actor = PersonaActor(
                store=FileStateStore(root, recent_limit=1),
                context_builder=ContextBuilder(),
                responder=FakeResponder(),
                sender=FakeSender(),
                maintainer=SlowMaintainer(),
                clock=lambda: NOW,
                maintenance_timeout_seconds=0.001,
            )
            await actor.start()
            try:
                first = await actor.submit(event("m1", mention=True))
                await actor.wait_idle()
                second = await actor.submit(event("m2", mention=True))
            finally:
                await actor.stop()

            self.assertEqual(first.kind, OutcomeKind.SPOKE)
            self.assertEqual(second.kind, OutcomeKind.SPOKE)
            self.assertEqual((root / "digest.md").read_text(), "")


class StateBoundsTests(unittest.TestCase):
    def test_legacy_mutable_state_moves_under_separate_state_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            resources = repository / "bot"
            state = resources / "state"
            resources.mkdir()
            (resources / "persona.md").write_text("ペルソナ\n", encoding="utf-8")
            (resources / "rules.md").write_text("自然に話す\n", encoding="utf-8")
            (repository / "habitus.md").write_text("- 話を聞く\n", encoding="utf-8")
            (repository / "cursor.json").write_text(
                json.dumps(
                    {
                        "version": 0,
                        "digested_until": None,
                        "slept_at": NOW.isoformat(),
                        "reflected_at": NOW.isoformat(),
                        "last_spoke_at": None,
                    }
                ),
                encoding="utf-8",
            )
            store = FileStateStore(
                state, resource_root=resources, legacy_root=repository
            )

            store.ensure_layout(now=NOW)
            store.ensure_layout(now=NOW)

            self.assertFalse((repository / "habitus.md").exists())
            self.assertEqual((state / "habitus.md").read_text(), "- 話を聞く\n")
            snapshot = store.load_snapshot(event("separate", mention=True))
            self.assertEqual(snapshot.persona, "ペルソナ")
            self.assertEqual(snapshot.rules, "自然に話す")
            self.assertEqual(snapshot.habitus, "- 話を聞く")

    def test_unchanged_reflection_only_advances_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = FileStateStore(root)
            store.ensure_layout(now=NOW)
            cursor_path = root / "cursor.json"
            cursor = json.loads(cursor_path.read_text(encoding="utf-8"))
            cursor["reflected_at"] = (NOW - timedelta(days=8)).isoformat()
            cursor_path.write_text(json.dumps(cursor), encoding="utf-8")
            (root / "habitus.md").write_text("- そのまま\n", encoding="utf-8")
            job = store.next_maintenance(now=NOW)
            self.assertIsInstance(job, ReflectionJob)

            store.commit_reflection(
                job,
                ReflectionDraft(False, "不正な内容は無視される", "要確認"),
                now=NOW,
            )

            self.assertEqual((root / "habitus.md").read_text(), "- そのまま\n")
            cursor = json.loads((root / "cursor.json").read_text())
            self.assertEqual(cursor["reflected_at"], NOW.isoformat())
    def test_existing_state_gets_empty_habitus_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "persona.md").write_text("ペルソナ\n", encoding="utf-8")
            (root / "cursor.json").write_text(
                json.dumps(
                    {
                        "version": 3,
                        "digested_until": None,
                        "slept_at": NOW.isoformat(),
                        "last_spoke_at": None,
                    }
                ),
                encoding="utf-8",
            )

            FileStateStore(root).ensure_layout(now=NOW)

            self.assertTrue((root / "habitus.md").exists())
            self.assertEqual((root / "habitus.md").read_text(encoding="utf-8"), "")
            cursor = json.loads((root / "cursor.json").read_text(encoding="utf-8"))
            self.assertEqual(cursor["version"], 3)
            self.assertEqual(cursor["reflected_at"], NOW.isoformat())

    def test_habitus_rejects_too_many_or_differential_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = FileStateStore(root)
            store.ensure_layout(now=NOW)
            source = event("habitus-check", mention=True)
            (root / "habitus.md").write_text(
                "\n".join(f"- 癖{i}" for i in range(11)), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "maximum is 10"):
                store.load_snapshot(source)

            (root / "habitus.md").write_text(
                "- 前より人の話を聞くようになった\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "current behavior"):
                store.load_snapshot(source)

            (root / "habitus.md").write_text(
                "人の話は最後まで聞く\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "invalid habitus line"):
                store.load_snapshot(source)

    def test_digest_keeps_only_newest_lines(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = FileStateStore(root, digest_max_lines=2)
            store.ensure_layout(now=NOW)
            job = DigestJob(0, (event("m1"),), "")

            store.commit_digest(
                job,
                "- [10:00 #general] old\n- [11:00 #general] middle\n- [12:00 #general] newest",
            )

            self.assertEqual(
                (root / "digest.md").read_text(),
                "- [11:00 #general] middle\n- [12:00 #general] newest\n",
            )

    def test_memory_bound_preserves_metadata_and_strong_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = FileStateStore(root, memory_max_lines=3)
            store.ensure_layout(now=NOW)
            job = SleepJob(
                expected_version=0,
                events=(),
                digest="",
                open_items="",
                mood=Mood("穏やか", "特にない", "弱い", "", NOW),
                persona="ペルソナ",
                rules="自然に話す",
                habitus="",
                self_memory="",
                world_memory="",
                channel_memories=(),
                people_memories=(),
            )
            draft = SleepDraft(
                memories=(
                    MemoryDocument(
                        "self",
                        "self",
                        "## 自分\n- [2026-08-01 #general] 古い通常\n"
                        "- [2026-08-02 #general] 忘れない ※強\n"
                        "- [2026-08-03 #general] 新しい通常",
                    ),
                    MemoryDocument("world", "world", "## 世界"),
                ),
                open_items="",
                mood_state="穏やか",
                mood_cause="整理した",
                mood_strength="弱い",
                mood_focus="",
            )

            store.commit_sleep(job, draft, now=NOW)

            self.assertEqual(
                (root / "memory" / "self.md").read_text(),
                "## 自分\n- [2026-08-02 #general] 忘れない ※強\n"
                "- [2026-08-03 #general] 新しい通常\n",
            )

    def test_invalid_memory_format_and_excessive_strong_entries_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = FileStateStore(Path(temporary), memory_strong_max=1)
            with self.assertRaisesRegex(ValueError, "invalid memory line"):
                store._validate_memory("- 日付のない記憶")
            with self.assertRaisesRegex(ValueError, "maximum is 1"):
                store._validate_memory(
                    "- [2026-09-01 #general] 一つ ※強\n"
                    "- [2026-09-01 #general] 二つ ※強"
                )
            with self.assertRaisesRegex(ValueError, "must not be empty"):
                store._validate_digest("")


class TransactionRecoveryTests(unittest.TestCase):
    def test_upgrade_baselines_old_events_without_replaying_them(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = FileStateStore(root)
            store._append_event(event("old", mention=True))

            store.ensure_layout(now=NOW)

            self.assertEqual(store.pending_addressed_events(), ())
            self.assertTrue((root / ".inbox-v1").exists())

    def test_startup_recovers_incomplete_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = FileStateStore(root)
            store.ensure_layout(now=NOW)
            reply = Event(
                id="bot-m1",
                ts=NOW,
                kind="channel",
                channel_id="general",
                channel_name="#general",
                author_id="self",
                author_name="自分",
                text="おかえり。",
                reply_to="m1",
            )
            cursor = json.loads((root / "cursor.json").read_text())
            cursor["version"] = 1
            manifest = {
                "version": 1,
                "files": {
                    "digest.md": "- recovered\n",
                    "cursor.json": json.dumps(cursor, ensure_ascii=False, indent=2) + "\n",
                },
                "append_events": [reply.to_log_dict()],
            }
            (root / ".anima-transaction.json").write_text(
                json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
            )

            FileStateStore(root).ensure_layout(now=NOW)

            self.assertEqual((root / "digest.md").read_text(), "- recovered\n")
            self.assertEqual(json.loads((root / "cursor.json").read_text())["version"], 1)
            self.assertEqual(
                [item.id for item in FileStateStore(root).read_channel_events("general")],
                ["bot-m1"],
            )
            self.assertFalse((root / ".anima-transaction.json").exists())

    def test_recovery_does_not_append_event_twice(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = FileStateStore(root)
            store.ensure_layout(now=NOW)
            reply = Event(
                id="bot-m1",
                ts=NOW,
                kind="channel",
                channel_id="general",
                channel_name="#general",
                author_id="self",
                author_name="自分",
                text="おかえり。",
            )
            store.append_received(reply)
            manifest = {
                "version": 1,
                "files": {},
                "append_events": [reply.to_log_dict()],
            }
            (root / ".anima-transaction.json").write_text(
                json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
            )

            FileStateStore(root).ensure_layout(now=NOW)

            events = FileStateStore(root).read_channel_events("general")
            self.assertEqual([item.id for item in events], ["bot-m1"])

    def test_recovery_rejects_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = FileStateStore(root)
            store.ensure_layout(now=NOW)
            manifest = {
                "version": 1,
                "files": {"../outside": "bad"},
                "append_events": [],
            }
            (root / ".anima-transaction.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )

            with self.assertRaisesRegex(ValueError, "unsafe transaction path"):
                FileStateStore(root).ensure_layout(now=NOW)


if __name__ == "__main__":
    unittest.main()
