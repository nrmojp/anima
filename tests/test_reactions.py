import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from anima.adapters.discord.expressions import FaceCatalog, FACE_NAMES, FACE_GUIDANCE
from anima.core.reaction_queue import ReactionBatch, ReactionBuffer
from anima.core.sandbox import SandboxKey
from anima.core.state import FileStateStore
from anima.core.context import ContextBuilder
from anima.core.actor import PersonaActor
from anima.adapters.openai.client import OpenAIReactionClassifier
from anima.core.models import MentionedPerson, ActionRecord, Event, SentMessage
from anima.adapters.discord.client import DiscordMessageSender, AnimaDiscordClient
from test_sandbox import event
from test_core import NOW, FakeResponder, FakeSender


def faces():
    result = FaceCatalog({name: f"<:face_{i}:{i+1}>" for i, name in enumerate(FACE_NAMES)})
    result.validate_registered(result.faces.values())
    return result


class FaceTests(unittest.TestCase):
    def test_catalog_validation_and_missing_registration(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "faces.json"
            self.assertEqual(FaceCatalog.load(path).faces, dict.fromkeys(FACE_NAMES))
            path.write_text(json.dumps({"default": "ふつう", "faces": faces().faces}))
            catalog = FaceCatalog.load(path)
            self.assertIsNone(catalog.emoji("怒"))
            status = catalog.validate_registered([catalog.faces["怒"]])
            self.assertEqual(status["怒"], "ready")
            self.assertEqual(status["喜"], "unregistered")
            self.assertIn("ペルソナ設定に従う", FACE_GUIDANCE)
            for value, default in (({}, "ふつう"), (dict.fromkeys(FACE_NAMES), "unknown"),
                                   ({**catalog.faces, "怒": "😡"}, "ふつう"),
                                   ({**catalog.faces, "怒": 12}, "ふつう")):
                with self.assertRaises(ValueError):
                    FaceCatalog(value, default)


class ReactionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        key = SandboxKey("guild", "1")
        self.store = FileStateStore(key.path(root), sandbox_key=key)
        self.store.ensure_layout(now=NOW)
        self.event = replace(event(), mention=False)
        self.store.append_received(self.event)
        self.faces = faces()
        self.classifier = SimpleNamespace(classify=AsyncMock(return_value={"action": "react", "target": "10", "face": "怒"}))
        self.sender = SimpleNamespace(react=AsyncMock(return_value=True))
        self.batch = ReactionBatch((self.event,))

    async def test_buffer_bounded_flush_full_queue_and_shutdown(self):
        enqueue = MagicMock()
        buffer = ReactionBuffer(enqueue, delay=.001)
        for i in range(30):
            buffer.add(replace(self.event, id=str(i)))
        await asyncio.gather(*buffer.tasks.values())
        self.assertEqual(len(enqueue.call_args.args[0].events), 20)
        enqueue.side_effect = asyncio.QueueFull
        buffer.add(self.event)
        await asyncio.gather(*buffer.tasks.values())
        for i in range(65):
            buffer.add(replace(self.event, channel_id=str(i)))
        self.assertEqual(len(buffer.pending), 64)
        await buffer.stop()
        self.assertEqual(buffer.pending, {})

    async def test_actor_immediate_bypass_debounce_and_failure_isolation(self):
        handler = SimpleNamespace(faces=self.faces, process=AsyncMock())
        actor = PersonaActor(store=self.store, context_builder=ContextBuilder(), responder=FakeResponder(),
                             sender=FakeSender(), clock=lambda: NOW, reactions=handler)
        actor.reaction_buffer.delay = .001
        await actor.start()
        try:
            await actor.submit(event())
            handler.process.assert_not_awaited()
            source = replace(self.event, id="20")
            await actor.submit(source)
            await actor.submit(source)
            await asyncio.gather(*actor.reaction_buffer.tasks.values())
            await actor.wait_idle()
            handler.process.assert_awaited_once()
            handler.process.side_effect = ValueError("invalid")
            actor._queue_reaction(ReactionBatch((source,)))
            await actor.wait_idle()
            self.assertFalse(actor._worker.done())
            handler.process.side_effect = None
            handler.process.return_value = source
            actor.proactive_allowed = lambda: True
            actor._process_proactive = AsyncMock(side_effect=RuntimeError("send failed"))
            actor._queue_reaction(ReactionBatch((source,)))
            await actor.wait_idle()
            actor._process_proactive.assert_awaited_once_with(source)
            self.assertFalse(actor._worker.done())
        finally:
            await actor.stop()

    async def test_classifier_schema_prompt_and_usage(self):
        create = AsyncMock(return_value=SimpleNamespace(output_text='{"action":"none","target":null,"face":null}'))
        classifier = OpenAIReactionClassifier(client=SimpleNamespace(responses=SimpleNamespace(create=create)), model="test")
        await classifier.classify(self.store.load_snapshot(self.event), (self.event,), ("怒",))
        args = create.call_args.kwargs
        self.assertFalse(args["store"])
        self.assertEqual(args["reasoning"], {"effort": "none"})
        self.assertIn("ペルソナ設定に従う", args["instructions"])
        self.assertIn("原則はnone", args["instructions"])
        self.assertIn("単なる報告", args["instructions"])
        self.assertNotIn("20〜30件に1回以下", args["instructions"])
        self.assertIn("単なる作品や画像の投稿", args["instructions"])
        self.assertEqual(args["text"]["format"]["schema"]["properties"]["action"]["enum"], ["none", "react"])
        self.assertEqual(args["max_output_tokens"], 256)
        await classifier.classify(
            self.store.load_snapshot(self.event), (self.event,), ("怒",), allow_speak=True
        )
        args = create.call_args.kwargs
        self.assertEqual(
            args["text"]["format"]["schema"]["properties"]["action"]["enum"],
            ["none", "react", "speak"],
        )
        self.assertIn("speak", args["instructions"])
        await classifier.classify(
            self.store.load_snapshot(self.event), (self.event,), (),
            allow_react=False, allow_speak=True,
        )
        args = create.call_args.kwargs
        self.assertEqual(
            args["text"]["format"]["schema"]["properties"]["action"]["enum"],
            ["none", "speak"],
        )

class DiscordFaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_proactive_send_uses_channel_without_reply_or_mentions(self):
        sender = DiscordMessageSender()
        channel = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            send=AsyncMock(return_value=SimpleNamespace(id=20, created_at=NOW, content="hello")),
        )
        sender.client = SimpleNamespace(get_channel=lambda _: channel)

        sent = await sender.send_proactive(event(), "hello")

        self.assertEqual(sent.id, "20")
        channel.send.assert_awaited_once()
        self.assertEqual(channel.send.call_args.args, ("hello",))
        self.assertIsInstance(channel.send.call_args.kwargs["allowed_mentions"], discord.AllowedMentions)
        channel.guild.id = 2
        with self.assertRaisesRegex(ValueError, "sandbox mismatch"):
            await sender.send_proactive(event(), "no")

    async def test_background_send_supports_artifacts_and_checks_sandbox(self):
        sender = DiscordMessageSender()
        channel = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            send=AsyncMock(return_value=SimpleNamespace(id=21, created_at=NOW, content="done")),
        )
        sender.client = SimpleNamespace(get_channel=lambda _: channel)
        with patch("anima.adapters.discord.delivery.discord.File", side_effect=lambda p: p):
            sent = await sender.send_background(event(), "done", attachments=(Path("one.png"),))
        self.assertEqual(sent.id, "21")
        self.assertEqual(channel.send.call_args.kwargs["files"], [Path("one.png")])
        channel.send.reset_mock()
        await sender.send_background(event(), "failed")
        self.assertNotIn("files", channel.send.call_args.kwargs)
        channel.guild.id = 2
        with self.assertRaisesRegex(ValueError, "sandbox mismatch"):
            await sender.send_background(event(), "no")

    async def test_internal_job_event_uses_background_delivery(self):
        sender = DiscordMessageSender()
        sender.send_background = AsyncMock(return_value=SentMessage("21", NOW, "done"))
        internal = replace(
            event(), id="j-1", author_id="self", response_target=MentionedPerson("20", "太郎"), actions=(
                ActionRecord("echo", "job_completed", "ジョブが完了した"),
            ),
        )
        sent = await sender.send(internal, "done", attachments=(Path("one.png"),))
        self.assertEqual(sent.id, "21")
        sender.send_background.assert_awaited_once()
        self.assertIsNone(await sender.find_reply(internal))

    async def test_ready_validates_application_emojis_and_failure_disables_them(self):
        sender = DiscordMessageSender(faces=faces())
        actor = SimpleNamespace(stop=AsyncMock())
        client = AnimaDiscordClient(actor=actor, sender=sender)
        client.fetch_application_emojis = AsyncMock(return_value=list(sender.faces.faces.values()))
        client._recover_pending_messages = AsyncMock()
        client._write_status = MagicMock()
        await client.on_ready()
        self.assertEqual(len(sender.faces.available), 5)
        client.fetch_application_emojis.side_effect = RuntimeError("offline")
        await client.on_ready()
        self.assertEqual(sender.faces.available, {})
        await client.close()

    async def test_reaction_destination_and_existing_emoji(self):
        sender = DiscordMessageSender(faces=faces())
        message = SimpleNamespace(author=SimpleNamespace(bot=False), reactions=[], add_reaction=AsyncMock())
        channel = SimpleNamespace(guild=SimpleNamespace(id=1), fetch_message=AsyncMock(return_value=message))
        sender.client = SimpleNamespace(get_channel=lambda _: channel)
        self.assertTrue(await sender.react(event(), "<:face_1:2>"))
        message.reactions = [SimpleNamespace(emoji="<:face_1:2>", me=True)]
        self.assertTrue(await sender.react(event(), "<:face_1:2>"))
        message.add_reaction.assert_awaited_once()
        message.author.bot = True
        self.assertFalse(await sender.react(event(), "<:face_1:2>"))
        with self.assertRaises(ValueError):
            await sender.react(event("2"), "<:face_1:2>")

    async def test_face_is_prepared_before_send_and_strong_face_is_separate(self):
        sender = DiscordMessageSender(faces=faces())
        channel = SimpleNamespace(id=100, send=AsyncMock())
        sender.register("10", SimpleNamespace(channel=channel, guild=SimpleNamespace(id=1)))
        sent = SentMessage("11", NOW)
        self.assertEqual(sender.prepare_face("hello", "unknown", "弱い"), "hello")
        self.assertEqual(sender.prepare_face("hello", "ふつう", "弱い"), "hello")
        self.assertEqual(sender.prepare_face("hello", "怒", "弱い"), "hello <:face_2:3>")
        self.assertEqual(sender.prepare_face("hello", "喜", "強い"), "hello")
        self.assertEqual(sender.prepare_face("x" * 2000, "喜", "弱い"), "x" * 2000)
        await sender.show_face(event(), sent, "unknown", "弱い", "hello")
        await sender.show_face(event(), sent, "ふつう", "強い", "hello")
        await sender.show_face(event(), sent, "怒", "弱い", "hello")
        channel.send.assert_not_awaited()
        await sender.show_face(event(), sent, "喜", "強い", "hello")
        channel.send.assert_awaited_once()
        with self.assertRaises(ValueError):
            await sender.show_face(event("2"), sent, "喜", "弱い", "hello")
