from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from anima.capabilities.contracts import ActionRecord
from anima.capabilities.commands import CommandRegistry, CommandResult, CommandSpec
from anima.adapters.discord.command_execution import command_source, execute_registered_command
from anima.core.telemetry import sandbox_context


class Provider:
    def __init__(self, spec, result=None):
        self.spec = spec
        self.result = result or CommandResult("できた", "private")

    def commands(self):
        return (self.spec,)

    async def execute_command(self, path, arguments, context):
        self.context = context
        self.telemetry_sandbox = sandbox_context.get()
        return self.result


class FailingProvider(Provider):
    async def execute_command(self, path, arguments, context):
        raise RuntimeError("broken")


def interaction(*, done=False):
    response = SimpleNamespace(
        is_done=lambda: done,
        defer=AsyncMock(),
        send_message=AsyncMock(),
    )
    return SimpleNamespace(
        id=1, created_at=datetime.now(timezone.utc), channel_id=10,
        channel=SimpleNamespace(name="test"), guild=SimpleNamespace(id=3),
        user=SimpleNamespace(
            id=20, display_name="user", voice=None,
            guild_permissions=SimpleNamespace(manage_guild=True),
        ),
        response=response, followup=SimpleNamespace(send=AsyncMock()),
    )


class DiscordCommandExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_failure_is_reported_after_defer_and_context_is_reset(self):
        spec = CommandSpec(("x",), "x", response_mode="deferred")
        runtime = SimpleNamespace(
            commands=CommandRegistry((FailingProvider(spec),)),
            actor=SimpleNamespace(store=None),
        )
        value = interaction()

        with self.assertLogs("anima.adapters.discord.command_execution", level="ERROR"):
            self.assertTrue(await execute_registered_command(
                runtime, value, ("x",), {}, "/x"
            ))

        value.response.defer.assert_awaited_once()
        value.followup.send.assert_awaited_once_with(
            "コマンドの実行に失敗しました。ログを確認してください。", ephemeral=True,
        )
        self.assertIsNone(sandbox_context.get())

    async def test_missing_registry_and_deferred_execution(self):
        self.assertFalse(await execute_registered_command(
            SimpleNamespace(commands=None), interaction(), ("x",), {}, "/x"
        ))
        spec = CommandSpec(
            ("x",), "x", permission="manage_sandbox", guild_only=True,
            response_mode="deferred",
        )
        provider = Provider(spec)
        registry = CommandRegistry((provider,))
        store = SimpleNamespace(append_received=Mock())
        runtime = SimpleNamespace(commands=registry, actor=SimpleNamespace(store=store))
        value = interaction()
        self.assertTrue(await execute_registered_command(runtime, value, ("x",), {}, "/x"))
        value.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        value.followup.send.assert_awaited_once_with("できた", ephemeral=True)
        self.assertEqual(provider.context.source.text, "/x")
        self.assertEqual(provider.telemetry_sandbox, "guild:3")
        self.assertIsNone(sandbox_context.get())
        store.append_received.assert_not_called()

    async def test_immediate_execution_and_source_voice(self):
        spec = CommandSpec(("x",), "x")
        provider = Provider(spec)
        runtime = SimpleNamespace(
            commands=CommandRegistry((provider,)), actor=SimpleNamespace(store=None)
        )
        value = interaction(done=True)
        value.user.voice = SimpleNamespace(channel=SimpleNamespace(id=99))
        await execute_registered_command(runtime, value, ("x",), {}, "/x")
        value.response.send_message.assert_awaited_once_with("できた", ephemeral=True)
        self.assertEqual(provider.context.source.author_voice_channel_id, "99")
        self.assertEqual(command_source(value, provider.context.sandbox_key, "/x").guild_id, "3")

    async def test_capability_actions_are_persisted_as_context(self):
        spec = CommandSpec(("x",), "x")
        provider = Provider(spec, CommandResult(
            "実行した", actions=(ActionRecord("test", "run", "実行結果"),),
        ))
        store = SimpleNamespace(append_received=Mock())
        runtime = SimpleNamespace(
            commands=CommandRegistry((provider,)), actor=SimpleNamespace(store=store)
        )
        await execute_registered_command(runtime, interaction(), ("x",), {}, "/x")
        recorded = store.append_received.call_args.args[0]
        self.assertEqual(recorded.actions[0].action, "run")
        self.assertEqual(recorded.sandbox_key, "guild:3")


if __name__ == "__main__":
    unittest.main()
