import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import discord

from anima.core.models import Attachment
from anima.bootstrap.cli import collect_status, main
from anima.adapters.dashboard.server import recent_events
from anima.core.sandbox import SandboxKey
from anima.core.state import FileStateStore
from test_sandbox import event
from test_core import NOW
from anima.adapters.discord.client import DiscordMessageSender, AnimaDiscordClient
from anima.core.sandbox_runtime import SandboxRouter, SandboxRuntime
from anima.core.access import ActivityPolicy


class Typing:
    def __init__(self):
        self.entered = False
        self.exited = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.exited = True


class ScopedPlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_plugins_do_not_register_slash_commands(self):
        client = AnimaDiscordClient(
            actor=SimpleNamespace(stop=AsyncMock()),
            sender=DiscordMessageSender(),
            policy=ActivityPolicy(frozenset({"1"})),
            plugins=frozenset(),
        )
        guild = discord.Object(id=1)
        self.assertIsNone(client.tree.get_command("anima-maintenance", guild=guild))
        self.assertIsNone(client.tree.get_command("anima-mode", guild=guild))
        self.assertIsNone(client.tree.get_command("anima-music", guild=guild))
        await client.close()

    async def test_discord_image_is_cached_inside_its_sandbox(self):
        with tempfile.TemporaryDirectory() as directory:
            actor = SandboxRouter(
                Path(directory), MagicMock(),
                policy=ActivityPolicy(frozenset({"30"})),
            )
            client = AnimaDiscordClient(actor=actor, sender=DiscordMessageSender())
            source = SimpleNamespace(
                read=AsyncMock(return_value=b"discord image"),
                filename="upload.png",
            )
            message = SimpleNamespace(attachments=(source,))
            original = replace(
                event(), id="10", guild_id="30",
                attachments=(Attachment("https://cdn.discordapp.com/lost.png", "image/png"),),
            )

            cached = await client._cache_message_attachments(
                message, original, SandboxKey("guild", "30")
            )

            self.assertEqual(cached.attachments[0].cache_name, "10-0.png")
            self.assertEqual(
                (Path(directory) / "sandboxes/guilds/30/attachments/10-0.png").read_bytes(),
                b"discord image",
            )
            source.read.assert_awaited_once_with(use_cached=True)

    async def test_discord_non_image_is_cached_for_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            actor = SandboxRouter(
                Path(directory), MagicMock(),
                policy=ActivityPolicy(frozenset({"30"})),
            )
            client = AnimaDiscordClient(actor=actor, sender=DiscordMessageSender())
            source = SimpleNamespace(
                read=AsyncMock(return_value=b"plain text"),
                filename="memo.txt", size=10,
            )
            original = replace(
                event(), id="11", guild_id="30",
                attachments=(Attachment("https://example.test/memo.txt", "text/plain"),),
            )

            cached = await client._cache_message_attachments(
                SimpleNamespace(attachments=(source,)), original,
                SandboxKey("guild", "30"),
            )

            self.assertEqual(cached.attachments[0].cache_name, "11-0.txt")
            self.assertEqual(
                (Path(directory) / "sandboxes/guilds/30/attachments/11-0.txt").read_bytes(),
                b"plain text",
            )

    async def test_oversized_discord_attachment_is_not_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            actor = SandboxRouter(
                Path(directory), MagicMock(),
                policy=ActivityPolicy(frozenset({"30"})),
            )
            client = AnimaDiscordClient(actor=actor, sender=DiscordMessageSender())
            source = SimpleNamespace(
                read=AsyncMock(return_value=b"unused"), filename="large.bin",
                size=26 * 1024 * 1024,
            )
            original = replace(
                event(), id="12", guild_id="30",
                attachments=(Attachment("https://example.test/large.bin"),),
            )

            with self.assertLogs("anima.adapters.discord.client", level="ERROR"):
                cached = await client._cache_message_attachments(
                    SimpleNamespace(attachments=(source,)), original,
                    SandboxKey("guild", "30"),
                )

            self.assertEqual(cached.attachments, original.attachments)
            source.read.assert_not_awaited()

    async def test_discord_attachment_fallback_suffix_and_empty_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            actor = SandboxRouter(
                Path(directory), MagicMock(),
                policy=ActivityPolicy(frozenset({"30"})),
            )
            client = AnimaDiscordClient(actor=actor, sender=DiscordMessageSender())
            original = replace(
                event(), id="13", guild_id="30",
                attachments=(Attachment("https://example.test/file"),),
            )
            valid = SimpleNamespace(
                read=AsyncMock(return_value=b"data"), filename="no-extension", size=4,
            )
            cached = await client._cache_message_attachments(
                SimpleNamespace(attachments=(valid,)), original,
                SandboxKey("guild", "30"),
            )
            self.assertEqual(cached.attachments[0].cache_name, "13-0.bin")

            empty = SimpleNamespace(
                read=AsyncMock(return_value=b""), filename="empty.bin", size=0,
            )
            with self.assertLogs("anima.adapters.discord.client", level="ERROR"):
                rejected = await client._cache_message_attachments(
                    SimpleNamespace(attachments=(empty,)), original,
                    SandboxKey("guild", "30"),
                )
            self.assertEqual(rejected.attachments, original.attachments)

    async def test_failed_image_cache_keeps_original_discord_url(self):
        with tempfile.TemporaryDirectory() as directory:
            actor = SandboxRouter(
                Path(directory), MagicMock(),
                policy=ActivityPolicy(frozenset({"30"})),
            )
            client = AnimaDiscordClient(actor=actor, sender=DiscordMessageSender())
            source = SimpleNamespace(
                read=AsyncMock(side_effect=OSError("disk full")), filename="upload.png"
            )
            original = replace(
                event(), id="10", guild_id="30",
                attachments=(Attachment("https://cdn.discordapp.com/original.png", "image/png"),),
            )

            with self.assertLogs("anima.adapters.discord.client", level="ERROR"):
                cached = await client._cache_message_attachments(
                    SimpleNamespace(attachments=(source,)), original,
                    SandboxKey("guild", "30"),
                )

            self.assertEqual(cached.attachments, original.attachments)

    async def test_discord_event_keeps_mentioned_people_except_self_and_author(self):
        client = AnimaDiscordClient.__new__(AnimaDiscordClient)
        client._connection = SimpleNamespace(user=SimpleNamespace(id=99))
        author = SimpleNamespace(id=1, display_name="A")
        introduced = SimpleNamespace(id=2, display_name="B")
        message = SimpleNamespace(
            id=10, created_at=NOW,
            channel=SimpleNamespace(id=20, name="general"),
            author=author, content="<@2>を紹介するよ",
            guild=SimpleNamespace(id=30), reference=None,
            mentions=(introduced, SimpleNamespace(id=99, display_name="ペルソナ"), author),
            attachments=(),
        )

        client.persona_names = ()
        converted = client._to_event(message)

        self.assertEqual(
            tuple((person.id, person.name) for person in converted.mentioned_people),
            (("2", "B"),),
        )
        self.assertEqual(converted.sandbox_key, "guild:30")

    async def test_sender_only_uses_discord_reply_for_reply_to_bot(self):
        sender = DiscordMessageSender()
        sent = SimpleNamespace(
            id=20,
            created_at=NOW.astimezone(timezone.utc),
            content="返事",
        )
        channel = SimpleNamespace(id=100, send=AsyncMock(return_value=sent))
        message = SimpleNamespace(
            channel=channel,
            guild=SimpleNamespace(id=1),
            reply=AsyncMock(return_value=sent),
        )
        sender.register("10", message)

        await sender.send(event(), "通常メッセージ")
        channel.send.assert_awaited_once()
        self.assertEqual(channel.send.call_args.kwargs["nonce"], "10")
        self.assertIsInstance(
            channel.send.call_args.kwargs["allowed_mentions"],
            discord.AllowedMentions,
        )
        message.reply.assert_not_awaited()

        source = replace(event(identifier="11"), reply_to_self=True)
        sender.register("11", message)
        await sender.send(source, "返信メッセージ")
        message.reply.assert_awaited_once()
        self.assertFalse(message.reply.call_args.kwargs["mention_author"])
        self.assertIsInstance(
            message.reply.call_args.kwargs["allowed_mentions"],
            discord.AllowedMentions,
        )

    async def test_sender_recovers_channel_message_by_source_nonce(self):
        sender = DiscordMessageSender()
        candidate = SimpleNamespace(
            id=20,
            created_at=NOW.astimezone(timezone.utc),
            content="返事",
            reference=None,
            nonce="10",
            author=SimpleNamespace(id=999),
        )

        class HistoryChannel:
            id = 100

            def history(self, **kwargs):
                async def values():
                    yield candidate
                return values()

        message = SimpleNamespace(
            channel=HistoryChannel(),
            guild=SimpleNamespace(id=1),
            created_at=NOW,
        )
        sender.client = SimpleNamespace(user=SimpleNamespace(id=999))
        sender.register("10", message)

        recovered = await sender.find_reply(event())

        self.assertEqual(recovered.id, "20")

    async def test_discord_dispatch_and_empty_vc_stop_only_selected_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            def factory(key, root):
                return SandboxRuntime(
                    SimpleNamespace(start=AsyncMock(), stop=AsyncMock(), submit=AsyncMock(),
                                    mark_deleted=AsyncMock(return_value=True)),
                    SimpleNamespace(bind=MagicMock(), start=AsyncMock(), stop=AsyncMock(), cancel_current=AsyncMock()),
                    SimpleNamespace(bind=MagicMock(), stop=AsyncMock(), handle=AsyncMock(return_value="stopped")),
                    SimpleNamespace(stop=AsyncMock()),
                )
            router = SandboxRouter(Path(directory), factory, policy=ActivityPolicy(frozenset({"1", "2"})))
            client = AnimaDiscordClient(actor=router, sender=DiscordMessageSender())
            client.tree.sync = AsyncMock(return_value=[])
            client._connection.user = SimpleNamespace(id=999)
            await client.setup_hook()
            self.assertEqual(client.tree.sync.await_count, 2)
            a = await router.runtime(SandboxKey("guild", "1"))
            b = await router.runtime(SandboxKey("guild", "2"))
            source = event()
            typing = Typing()
            channel = SimpleNamespace(typing=lambda: typing)
            message = SimpleNamespace(id=10, author=SimpleNamespace(bot=False), guild=SimpleNamespace(id=1),
                                      channel=channel, content="ペルソナ、曲止めて", reply=AsyncMock())
            with patch.object(client, "_to_event", return_value=source):
                await client.on_message(message)
                a.music.handle.assert_not_awaited()
                b.music.handle.assert_not_awaited()
                a.actor.submit.assert_awaited_once_with(source)
                message.content = "hello"
                await client.on_message(message)
                self.assertEqual(a.actor.submit.await_count, 2)
                self.assertTrue(typing.entered)
                self.assertTrue(typing.exited)
                await client.on_raw_message_delete(SimpleNamespace(
                    guild_id=1, channel_id=100, message_id=10, cached_message=None))
                a.actor.mark_deleted.assert_awaited_once_with("100", "10")
                b.actor.mark_deleted.assert_not_awaited()
                await client.on_raw_bulk_message_delete(SimpleNamespace(
                    guild_id=1, channel_id=100, message_ids={11, 12}, cached_messages=[]))
                self.assertEqual(a.actor.mark_deleted.await_count, 3)
                self.assertEqual(
                    {call.args for call in a.actor.mark_deleted.await_args_list[1:]},
                    {("100", "11"), ("100", "12")},
                )
            channel = SimpleNamespace(id=100, members=[])
            guild = SimpleNamespace(id=1)
            connection = SimpleNamespace(guild=guild, channel=channel, disconnect=AsyncMock())
            with patch.object(AnimaDiscordClient, "voice_clients", new_callable=PropertyMock, return_value=[connection]):
                await client.on_voice_state_update(
                    SimpleNamespace(bot=False, guild=guild), SimpleNamespace(channel=channel), SimpleNamespace(channel=None))
            a.voice.cancel_current.assert_awaited_once()
            a.music.stop.assert_awaited_once()
            a.dj.stop.assert_awaited_once_with("channel_empty")
            b.voice.cancel_current.assert_not_awaited()
            b.music.stop.assert_not_awaited()
            b.dj.stop.assert_not_awaited()
            await client.close()

    async def test_uncached_dm_delete_is_skipped_without_guessing_sandbox(self):
        actor = SimpleNamespace(policy=ActivityPolicy(dm_enabled=True))
        client = AnimaDiscordClient.__new__(AnimaDiscordClient)
        client.actor = actor
        client.policy = actor.policy

        with patch("anima.adapters.discord.client.emit") as emit_event:
            await client.on_raw_message_delete(SimpleNamespace(
                guild_id=None, channel_id=100, message_id=10, cached_message=None))

        emit_event.assert_called_once_with(
            "discord.message.delete_skipped",
            channel_id="100",
            event_id="10",
            reason="dm_sandbox_unknown",
        )

    async def test_cached_dm_delete_uses_dm_owner_and_direct_actor(self):
        actor = SimpleNamespace(
            policy=ActivityPolicy(dm_enabled=True),
            mark_deleted=AsyncMock(return_value=True),
        )
        client = AnimaDiscordClient.__new__(AnimaDiscordClient)
        client.actor = actor
        client.policy = actor.policy
        cached = SimpleNamespace(author=SimpleNamespace(bot=False, id=42))

        await client.on_raw_message_delete(SimpleNamespace(
            guild_id=None, channel_id=100, message_id=10, cached_message=cached))

        actor.mark_deleted.assert_awaited_once_with("100", "10")

    async def test_sender_rejects_wrong_destination(self):
        sender = DiscordMessageSender()
        message = SimpleNamespace(channel=SimpleNamespace(id=100), guild=SimpleNamespace(id=2), reply=AsyncMock())
        sender.register("10", message)
        with self.assertRaises(ValueError):
            await sender.send(event(), "private reply")
        message.reply.assert_not_awaited()

class ScopedOperationsTests(unittest.TestCase):
    def test_memory_index_maintenance_commands_are_scoped_and_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = SandboxKey("guild", "1")
            state = key.path(root)
            FileStateStore(state, sandbox_key=key).ensure_layout(now=NOW)
            (state / "memory/self.md").write_text("記憶\n", encoding="utf-8")
            index = MagicMock()
            index.ensure = AsyncMock(return_value="vs_synced")
            index.rebuild = AsyncMock(return_value="vs_rebuilt")
            index.prune_orphans = AsyncMock(return_value={
                "dry_run": True, "candidates": [], "unmanaged": [], "deleted": 0,
            })
            environment = ({"OPENAI_API_KEY": "test-key"}, root, root)
            with patch("anima.bootstrap.cli._environment", return_value=environment), \
                 patch("anima.bootstrap.cli.AsyncOpenAI"), \
                 patch("anima.bootstrap.cli.MemoryVectorStore", return_value=index), \
                 patch("anima.bootstrap.cli._print") as output:
                main(["memory-index", "status", "--sandbox", "guild:1"])
                self.assertEqual(output.call_args.args[0]["state"], "not_created")
                main(["memory-index", "sync", "--sandbox", "guild:1"])
                index.ensure.assert_awaited_once()
                main(["memory-index", "rebuild", "--sandbox", "guild:1"])
                index.rebuild.assert_awaited_once()
                main(["memory-index", "prune", "--sandbox", "guild:1"])
                index.prune_orphans.assert_awaited_once_with(apply=False)
                main(["memory-index", "prune", "--apply", "--sandbox", "guild:1"])
                index.prune_orphans.assert_awaited_with(apply=True)
                with self.assertRaises(SystemExit):
                    main(["memory-index", "sync", "--apply", "--sandbox", "guild:1"])

    def test_memory_index_mutation_requires_stopped_bot_and_api_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = SandboxKey("guild", "1")
            state = key.path(root)
            FileStateStore(state, sandbox_key=key).ensure_layout(now=NOW)
            runtime = root / "runtime"
            runtime.mkdir()
            with patch("anima.bootstrap.cli._environment", return_value=({}, root, root)):
                with self.assertRaises(SystemExit):
                    main(["memory-index", "sync", "--sandbox", "guild:1"])
            (runtime / "status.json").write_text(json.dumps({"pid": 123}))
            with patch(
                "anima.bootstrap.cli._environment",
                return_value=({"OPENAI_API_KEY": "test-key"}, root, root),
            ), patch("anima.bootstrap.cli._pid_exists", return_value=True):
                with self.assertRaises(SystemExit):
                    main(["memory-index", "sync", "--sandbox", "guild:1"])

    def test_status_and_dashboard_events_filter_by_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = SandboxKey("guild", "1")
            FileStateStore(key.path(root), sandbox_key=key).ensure_layout(now=NOW)
            runtime = root / "runtime"
            runtime.mkdir()
            values = [
                {"event": "openai.response.completed", "total_tokens": 5, "sandbox_key": "guild:1"},
                {"event": "openai.response.completed", "total_tokens": 999, "sandbox_key": "guild:2"},
                {"event": "maintenance.completed", "operation": "reflection", "sandbox_key": "guild:2", "conflict": "private"},
            ]
            (runtime / "anima.jsonl").write_text("\n".join(json.dumps(v) for v in values))
            status = collect_status(root, state_root=root, sandbox="guild:1")
            self.assertEqual(status["sandbox_key"], "guild:1")
            self.assertEqual(status["openai_usage"]["total_tokens"], 5)
            self.assertIsNone(status["state"]["reflection"]["conflict"])
            self.assertEqual(len(recent_events(root, sandbox="guild:1")), 1)
            with patch("anima.bootstrap.cli._environment", return_value=({}, root, root)), patch("anima.bootstrap.cli._print") as output:
                main(["sandboxes"])
                self.assertEqual(output.call_args.args[0]["sandboxes"], ["guild:1"])
                main(["status", "--sandbox", "guild:1", "--json"])
                self.assertEqual(output.call_args.args[0]["sandbox_key"], "guild:1")
                main(["status", "--json"])
                with self.assertRaises(SystemExit):
                    main(["prune"])
                with self.assertRaises(SystemExit):
                    main(["status", "--sandbox", "guild:../x"])
                with self.assertRaises(SystemExit):
                    main(["status", "--sandbox", "guild:2"])
