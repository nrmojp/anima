from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import unittest

from anima.capabilities.contracts import ActionRecord, PermissionSet
from anima.capabilities.commands import (
    CommandChoice,
    CommandContext,
    CommandParameter,
    CommandProvider,
    CommandRegistry,
    CommandResult,
    CommandSpec,
)
from anima.core.models import Event
from anima.core.sandbox import SandboxKey


def source(*, sandbox="guild:1"):
    return Event(
        id="c1", ts=datetime.now(timezone.utc), kind="channel", channel_id="10",
        channel_name="#test", author_id="20", author_name="user", text="/test",
        guild_id="1", sandbox_key=sandbox,
    )


class Provider:
    def __init__(self, specs, result=None):
        self._specs = specs
        self.result = result or CommandResult("ok")
        self.calls = []

    def commands(self):
        return self._specs

    async def execute_command(self, path, arguments, context):
        self.calls.append((path, arguments, context))
        return self.result


class CommandValueTests(unittest.TestCase):
    def test_specs_context_and_results(self):
        choice = CommandChoice("開始", "on")
        parameter = CommandParameter("mode", "操作", "choice", choices=(choice,))
        spec = CommandSpec(
            ("anima-music", "dj"), "DJ操作", (parameter,),
            permission="manage_sandbox", guild_only=True, response_mode="deferred",
        )
        self.assertEqual(spec.path[-1], "dj")
        context = CommandContext(
            source(), SandboxKey("guild", "1"), "20",
            PermissionSet(frozenset({"manage_sandbox"})),
        )
        self.assertEqual(context.actor_id, "20")
        result = CommandResult("done", attachments=(Path("x"),), actions=(
            ActionRecord("music", "play", "再生"),
        ))
        self.assertEqual(result.visibility, "public")

    def test_invalid_values(self):
        makers = (
            lambda: CommandChoice("", "x"), lambda: CommandChoice("x", ""),
            lambda: CommandParameter("Bad", "x", "string"),
            lambda: CommandParameter("x", "", "string"),
            lambda: CommandParameter("x", "x", "bad"),
            lambda: CommandParameter("x", "x", "choice"),
            lambda: CommandParameter("x", "x", "string", choices=(CommandChoice("x", "x"),)),
            lambda: CommandParameter("x", "x", "boolean", minimum=1),
            lambda: CommandParameter("x", "x", "integer", minimum=2, maximum=1),
            lambda: CommandSpec((), "x"), lambda: CommandSpec(("a", "b", "c"), "x"),
            lambda: CommandSpec(("Bad",), "x"), lambda: CommandSpec(("x",), ""),
            lambda: CommandSpec(("x",), "x", (
                CommandParameter("a", "x", "string"),
                CommandParameter("a", "x", "string"),
            )),
            lambda: CommandSpec(("x",), "x", (
                CommandParameter("a", "x", "string", required=False),
                CommandParameter("b", "x", "string"),
            )),
            lambda: CommandSpec(("x",), "x", permission="bad-name"),
            lambda: CommandSpec(("x",), "x", response_mode="later"),
            lambda: CommandResult(""), lambda: CommandResult("x", "hidden"),
            lambda: CommandContext(source(), SandboxKey("guild", "2"), "20"),
        )
        for make in makers:
            with self.subTest(make=make), self.assertRaises(ValueError):
                make()


class CommandRegistryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.parameters = (
            CommandParameter("text", "text", "string", minimum=1, maximum=3),
            CommandParameter("count", "count", "integer", required=False, minimum=1, maximum=2),
            CommandParameter("flag", "flag", "boolean", required=False),
        )
        self.spec = CommandSpec(
            ("test",), "test", self.parameters,
            permission="manage_sandbox", guild_only=True,
        )
        self.context = CommandContext(
            source(), SandboxKey("guild", "1"), "20",
            PermissionSet(frozenset({"manage_sandbox"})),
        )

    async def test_execute_and_private_permission_result(self):
        provider = Provider((self.spec,))
        registry = CommandRegistry((provider,))
        self.assertIsInstance(provider, CommandProvider)
        self.assertEqual(registry.specs, (self.spec,))
        self.assertEqual(registry.spec(("test",)), self.spec)
        self.assertIsNone(registry.spec(("none",)))
        result = await registry.execute(
            ("test",), {"text": "猫", "count": 2, "flag": True}, self.context
        )
        self.assertEqual(result.visibility, "private")
        self.assertEqual(len(provider.calls), 1)

    async def test_rejections_and_argument_types(self):
        provider = Provider((self.spec,))
        registry = CommandRegistry((provider,))
        denied = CommandContext(source(), SandboxKey("guild", "1"), "20")
        dm = CommandContext(
            source(sandbox=None), SandboxKey("dm", "20"), "20",
            PermissionSet(frozenset({"manage_sandbox"})),
        )
        cases = (
            (("missing",), {}, self.context),
            (("test",), {"text": "猫"}, denied),
            (("test",), {"text": "猫"}, dm),
            (("test",), {}, self.context),
            (("test",), {"text": "長すぎる"}, self.context),
            (("test",), {"text": 1}, self.context),
            (("test",), {"text": "猫", "count": True}, self.context),
            (("test",), {"text": "猫", "count": 0}, self.context),
            (("test",), {"text": "猫", "flag": 1}, self.context),
            (("test",), {"text": "猫", "extra": 1}, self.context),
        )
        for path, arguments, context in cases:
            with self.subTest(arguments=arguments):
                self.assertEqual(
                    (await registry.execute(path, arguments, context)).visibility, "private"
                )

    async def test_choice_and_provider_contract(self):
        spec = CommandSpec(("choose",), "choose", (
            CommandParameter("mode", "mode", "choice", choices=(CommandChoice("On", "on"),)),
        ))
        provider = Provider((spec,), result="wrong")
        registry = CommandRegistry((provider,))
        self.assertEqual(
            (await registry.execute(("choose",), {"mode": "off"}, self.context)).visibility,
            "private",
        )
        with self.assertRaises(TypeError):
            await registry.execute(("choose",), {"mode": "on"}, self.context)
        provider.result = CommandResult("ok")
        self.assertEqual(
            (await registry.execute(("choose",), {"mode": "on"}, self.context)).visibility,
            "public",
        )

    def test_registry_definition_errors(self):
        with self.assertRaises(TypeError):
            CommandRegistry((object(),))
        for provider in (
            Provider([]), Provider((object(),)), Provider((self.spec, self.spec)),
        ):
            with self.subTest(provider=provider), self.assertRaises(ValueError):
                CommandRegistry((provider,))


if __name__ == "__main__":
    unittest.main()
