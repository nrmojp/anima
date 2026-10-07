import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from anima.core.access import ACTIVITY_MODES, ActivityModeStore, ActivityPolicy
from anima.bootstrap.settings import ConfigurationError, Settings
from anima.adapters.discord.client import DiscordMessageSender, AnimaDiscordClient
from anima.core.sandbox import SandboxKey
from anima.core.sandbox_runtime import SandboxRouter, SandboxRuntime
from anima.core.state import FileStateStore
from test_core import NOW
from test_sandbox import event


class ActivityPolicyTests(unittest.TestCase):
    def test_defaults_and_independent_switches(self):
        guild, other, dm = SandboxKey("guild", "1"), SandboxKey("guild", "2"), SandboxKey("dm", "99")
        for policy, expected in (
            (ActivityPolicy(), (False, False, False)),
            (ActivityPolicy(frozenset({"1"})), (True, False, False)),
            (ActivityPolicy(dm_enabled=True), (False, False, True)),
            (ActivityPolicy(frozenset({"1", "2"}), True), (True, True, True)),
        ):
            self.assertEqual(tuple(policy.allows(key) for key in (guild, other, dm)), expected)

    def test_configuration_parsing_and_invalid_values(self):
        with tempfile.TemporaryDirectory() as directory:
            def load(**values):
                return Settings.load(cwd=Path(directory), environ={
                    "OPENAI_API_KEY": "test", "DISCORD_BOT_TOKEN": "test", **values})
            self.assertEqual(load().allowed_guild_ids, frozenset())
            self.assertFalse(load().dm_enabled)
            self.assertEqual(load(ANIMA_ALLOWED_GUILD_IDS="  ").allowed_guild_ids, frozenset())
            largest = str(2**64 - 1)
            settings = load(ANIMA_ALLOWED_GUILD_IDS=f" 1, 2,1,{largest} ", ANIMA_DM_ENABLED="true")
            self.assertEqual(settings.allowed_guild_ids, frozenset({"1", "2", largest}))
            self.assertTrue(settings.dm_enabled)
            for raw in ("0", "-1", "01", "１", "1,", ",1", "1,,2", "abc", "1 2", "1.0", str(2**64), "9" * 21):
                with self.subTest(raw=raw), self.assertRaises(ConfigurationError):
                    load(ANIMA_ALLOWED_GUILD_IDS=raw)
            with self.assertRaises(ConfigurationError):
                load(ANIMA_DM_ENABLED="maybe")


class ActivityModeStoreTests(unittest.TestCase):
    def test_memory_modes_are_cumulative_and_dm_is_fixed_to_reply(self):
        store = ActivityModeStore()
        guild, dm = SandboxKey("guild", "1"), SandboxKey("dm", "2")
        self.assertEqual(store.get(guild), "reply")
        self.assertEqual(store.get(dm), "reply")
        for mode in ACTIVITY_MODES:
            value = store.set(guild, mode, changed_by="admin", changed_at=NOW)
            self.assertEqual(value["mode"], mode)
            self.assertEqual(store.get(guild), mode)
            for capability in ACTIVITY_MODES:
                self.assertEqual(
                    store.permits(guild, capability),
                    ACTIVITY_MODES.index(mode) >= ACTIVITY_MODES.index(capability),
                )
        with self.assertRaisesRegex(ValueError, "only configurable"):
            store.set(dm, "silent", changed_by="admin", changed_at=NOW)
        with self.assertRaisesRegex(ValueError, "unknown activity mode"):
            store.set(guild, "loud", changed_by="admin", changed_at=NOW)
        with self.assertRaisesRegex(ValueError, "unknown activity capability"):
            store.permits(guild, "music")

    def test_persistent_round_trip_missing_and_corrupt_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = SandboxKey("guild", "1")
            store = ActivityModeStore(root)
            self.assertEqual(
                store.details(key),
                {"mode": "reply", "changed_by": None, "changed_at": None},
            )
            store.set(key, "react", changed_by="admin (9)", changed_at=NOW)
            self.assertEqual(ActivityModeStore(root).details(key), {
                "mode": "react", "changed_by": "admin (9)", "changed_at": NOW.isoformat(),
            })
            path = key.path(root) / "settings" / "activity.json"
            for invalid in ("not json", "[]", '{"mode":"unknown"}', '{"mode":"react","changed_by":1}'):
                path.write_text(invalid, encoding="utf-8")
                details = ActivityModeStore(root).details(key)
                if invalid.endswith('"changed_by":1}'):
                    self.assertEqual(details, {"mode": "react", "changed_by": None, "changed_at": None})
                else:
                    self.assertEqual(details["mode"], "silent")
            path.parent.rename(path.parent.with_name("real-settings"))
            path.parent.symlink_to(path.parent.with_name("real-settings"), target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlinks"):
                store.get(key)


class ActivityRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_activity_mode_command_requires_admin_persists_and_cancels_downgrade(self):
        key = SandboxKey("guild", "1")
        runtime = SimpleNamespace(actor=SimpleNamespace(cancel_pending_reactions=AsyncMock()))
        router = SimpleNamespace(runtimes={key: runtime})
        modes = ActivityModeStore()
        client = AnimaDiscordClient.__new__(AnimaDiscordClient)
        client.actor = router
        client.policy = ActivityPolicy(frozenset({"1"}))
        client.activity_modes = modes
        response = SimpleNamespace(send_message=AsyncMock())
        user = SimpleNamespace(
            id=9,
            guild_permissions=SimpleNamespace(manage_guild=False),
            __str__=lambda _: "admin",
        )
        interaction = SimpleNamespace(guild=SimpleNamespace(id=1), user=user, response=response)

        await client._run_activity_mode(interaction, "react")
        self.assertEqual(modes.get(key), "reply")
        user.guild_permissions.manage_guild = True
        with patch("anima.adapters.discord.command_handlers.SandboxRouter", SimpleNamespace):
            await client._run_activity_mode(interaction, "proactive")
            self.assertEqual(modes.get(key), "proactive")
            await client._run_activity_mode(interaction, "reply")
        runtime.actor.cancel_pending_reactions.assert_awaited_once()
        await client._run_activity_mode(interaction, "status")
        await client._run_activity_mode(interaction, "invalid")
        self.assertEqual(response.send_message.await_count, 5)
        self.assertIn("reply", response.send_message.call_args_list[-2].args[0])
        self.assertIn("不正", response.send_message.call_args_list[-1].args[0])

    async def test_experimental_maintenance_is_scoped_and_requires_manage_guild(self):
        actor = SimpleNamespace(
            force_maintenance=AsyncMock(return_value="sleep"),
            force_self_time=AsyncMock(return_value=(SimpleNamespace(action="none"),)),
        )
        router = SimpleNamespace(
            runtime=AsyncMock(return_value=SimpleNamespace(actor=actor))
        )
        client = AnimaDiscordClient.__new__(AnimaDiscordClient)
        client.actor = router
        client.policy = ActivityPolicy(frozenset({"1"}))
        response = SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock())
        followup = SimpleNamespace(send=AsyncMock())
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            user=SimpleNamespace(guild_permissions=SimpleNamespace(manage_guild=False)),
            response=response,
            followup=followup,
        )

        await client._run_experimental_maintenance(interaction, "sleep")
        response.send_message.assert_awaited_once()
        router.runtime.assert_not_awaited()

        interaction.user.guild_permissions.manage_guild = True
        with patch("anima.adapters.discord.command_handlers.SandboxRouter", SimpleNamespace):
            await client._run_experimental_maintenance(interaction, "sleep")
        response.defer.assert_awaited_once()
        actor.force_maintenance.assert_awaited_once_with("sleep")
        followup.send.assert_awaited_once_with("睡眠を完了しました。", ephemeral=True)
        followup.send.reset_mock()
        with patch("anima.adapters.discord.command_handlers.SandboxRouter", SimpleNamespace):
            await client._run_experimental_maintenance(interaction, "self_time")
        actor.force_self_time.assert_awaited_once()
        followup.send.assert_awaited_once_with(
            "Self Timeを1反復実行しました。", ephemeral=True
        )

    async def test_experimental_maintenance_reports_empty_and_internal_failures(self):
        actor = SimpleNamespace(force_maintenance=AsyncMock(side_effect=ValueError("空です")))
        router = SimpleNamespace(runtime=AsyncMock(return_value=SimpleNamespace(actor=actor)))
        client = AnimaDiscordClient.__new__(AnimaDiscordClient)
        client.actor = router
        client.policy = ActivityPolicy(frozenset({"1"}))
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            user=SimpleNamespace(guild_permissions=SimpleNamespace(manage_guild=True)),
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        with patch("anima.adapters.discord.command_handlers.SandboxRouter", SimpleNamespace):
            with self.assertLogs("anima.adapters.discord.command_handlers", level="WARNING") as logs:
                await client._run_experimental_maintenance(interaction, "nap")
            self.assertIn(
                "Experimental maintenance rejected for guild 1 (nap): 空です",
                "\n".join(logs.output),
            )
            interaction.followup.send.assert_awaited_once_with("空です", ephemeral=True)
            interaction.followup.send.reset_mock()
            actor.force_maintenance.side_effect = RuntimeError("secret")
            await client._run_experimental_maintenance(interaction, "sleep")
        interaction.followup.send.assert_awaited_once_with(
            "メンテナンスに失敗しました。ログを確認してください。", ephemeral=True
        )

    async def test_router_blocks_creation_and_existing_state_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            allowed, denied, dm = SandboxKey("guild", "1"), SandboxKey("guild", "2"), SandboxKey("dm", "99")
            for key in (allowed, denied, dm):
                FileStateStore(key.path(root), sandbox_key=key).ensure_layout(now=NOW)
            before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            actor = SimpleNamespace(
                start=AsyncMock(),
                stop=AsyncMock(),
                submit=AsyncMock(return_value="observed"),
                pending_events=lambda: (),
            )
            factory = MagicMock(return_value=SandboxRuntime(actor))
            router = SandboxRouter(root, factory, policy=ActivityPolicy(frozenset({"1"})))
            await router.start()
            self.assertEqual(set(router.runtimes), {allowed})
            factory.assert_called_once_with(allowed, allowed.path(root))
            self.assertEqual(
                await router.submit(event(), allow_reactions=False), "observed"
            )
            actor.submit.assert_awaited_once_with(event(), allow_reactions=False)
            for source in (event("2"), event("3"), event(None)):
                with self.assertRaises(PermissionError):
                    await router.submit(source)
            self.assertFalse(SandboxKey("guild", "3").path(root).exists())
            self.assertEqual(before, {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()})
            self.assertEqual(router.pending_events(), ())
            await router.stop()
            default = SandboxRouter(root, factory)
            await default.start()
            self.assertEqual(default.runtimes, {})
            await default.stop()

    async def test_message_gate_precedes_conversion_sender_and_music(self):
        actor = SimpleNamespace(submit=AsyncMock(), stop=AsyncMock())
        sender = DiscordMessageSender()
        music = SimpleNamespace(bind=MagicMock(), handle=AsyncMock(), stop=AsyncMock())
        client = AnimaDiscordClient(actor=actor, sender=sender, music=music,
                                     policy=ActivityPolicy(frozenset({"1"})))
        client._connection.user = SimpleNamespace(id=999)
        message = SimpleNamespace(author=SimpleNamespace(bot=False, id=99, display_name="test"), guild=SimpleNamespace(id=2),
                                  channel=MagicMock(spec=discord.DMChannel), content="ペルソナ 曲再生", reply=AsyncMock())
        with patch.object(client, "_to_event", return_value=event()) as convert:
            await client.on_message(message)
            message.guild = None
            await client.on_message(message)
            message.channel = SimpleNamespace()  # group/unsupported channel
            await client.on_message(message)
            convert.assert_not_called()
            actor.submit.assert_not_awaited()
            music.handle.assert_not_awaited()
            self.assertEqual(sender._messages, {})
            message.guild = SimpleNamespace(id=1)
            message.channel = MagicMock()
            message.content = "hello"
            ignored = replace(event(), mention=False)
            convert.return_value = ignored
            await client.on_message(message)
            actor.submit.assert_awaited_once_with(ignored, allow_reactions=False)
            client.activity_modes.set(
                SandboxKey("guild", "1"), "react", changed_by="admin", changed_at=NOW
            )
            await client.on_message(message)
            self.assertEqual(actor.submit.await_count, 2)
            actor.submit.assert_awaited_with(ignored, allow_reactions=True)
            message.channel.typing.assert_not_called()
            client.activity_modes.set(
                SandboxKey("guild", "1"), "silent", changed_by="admin", changed_at=NOW
            )
            convert.reset_mock()
            await client.on_message(message)
            convert.assert_not_called()
            client.policy = ActivityPolicy(dm_enabled=True)
            message.guild = None
            message.channel = MagicMock(spec=discord.DMChannel)
            convert.return_value = event(None)
            await client.on_message(message)
            self.assertEqual(actor.submit.await_count, 3)
        await client.close()

    async def test_recovery_filters_before_discord_fetch_and_keeps_pending(self):
        denied = event("2")
        allowed = event()
        actor = SimpleNamespace(pending_events=lambda: (denied, allowed, event(None)),
                                submit=AsyncMock(), stop=AsyncMock(), abandon=MagicMock())
        client = AnimaDiscordClient(actor=actor, sender=DiscordMessageSender(),
                                     policy=ActivityPolicy(frozenset({"1"})))
        typing = AsyncMock()
        typing.__aenter__ = AsyncMock()
        typing.__aexit__ = AsyncMock()
        message = SimpleNamespace(channel=SimpleNamespace(typing=lambda: typing))
        channel = SimpleNamespace(fetch_message=AsyncMock(return_value=message))
        client.get_channel = MagicMock(return_value=channel)
        client.fetch_channel = AsyncMock()
        with patch.object(client, "_to_event", return_value=allowed):
            await client._recover_pending_messages()
        client.get_channel.assert_called_once_with(int(allowed.channel_id))
        channel.fetch_message.assert_awaited_once_with(int(allowed.id))
        actor.submit.assert_awaited_once_with(allowed)
        typing.__aenter__.assert_awaited_once()
        typing.__aexit__.assert_awaited_once()
        actor.abandon.assert_not_called()
        client.fetch_channel.assert_not_awaited()
        await client.close()
