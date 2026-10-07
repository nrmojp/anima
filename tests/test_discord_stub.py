from __future__ import annotations

from datetime import datetime, timezone
import logging
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from anima.adapters.stub.discord import StubDelivery, StubDiscordAdapter, StubDiscordSender
from anima.bootstrap.cli import main
from anima.bootstrap.discord_stub import (
    _capture_telemetry, _usage, run_discord_stub,
)
from anima.bootstrap.settings import Settings
from anima.core.inventory import MAX_ARTIFACT_BYTES
from anima.core.inventory import InventoryStore
from anima.core.models import Event, OutcomeKind
from anima.core.sandbox import SandboxKey
from anima.core.telemetry import emit
from anima.capabilities.contracts import CapabilityContext, ToolRegistry
from anima.bootstrap.resource_providers import (
    AttachmentResourceProvider, InventoryResourceProvider,
)
from anima.core.resources import (
    ResourceCollectionSpec, ResourceRegistration, ResourceRegistry, ResourceToolProvider,
)


NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)


def source_event(identifier="source1"):
    return Event(
        id=identifier, ts=NOW, kind="channel", channel_id="10",
        channel_name="stub", author_id="20", author_name="Tester", text="hello",
        guild_id="30", mention=True, sandbox_key="guild:30",
    )


class FakeRouter:
    def __init__(self):
        self.calls = []

    async def submit(self, event, *, allow_reactions=True):
        self.calls.append((event, allow_reactions))
        return SimpleNamespace(kind=OutcomeKind.SPOKE, reply="stub reply")


class StubDiscordSenderTests(unittest.IsolatedAsyncioTestCase):
    async def test_captures_all_delivery_ports_and_metadata(self):
        identifiers = iter(("stub-1", "stub-2", "stub-3", "stub-4"))
        sender = StubDiscordSender(
            clock=lambda: NOW, id_factory=lambda: next(identifiers)
        )
        source = source_event()
        attachment = Path("picture.png")
        sent = await sender.send(source, "reply", attachments=(attachment,))
        self.assertEqual(sent.id, "stub-1")
        self.assertEqual((await sender.find_reply(source)).text, "reply")
        await sender.send_background(source, "background")
        await sender.send_proactive(source, "proactive")
        await sender.send_reminder(SimpleNamespace(
            id="rem1", author_id="20", message="wake up",
        ))
        self.assertEqual(
            [item.kind for item in sender.deliveries],
            ["reply", "background", "proactive", "reminder"],
        )
        self.assertEqual(sender.deliveries[0].to_dict()["attachments"], ["picture.png"])
        self.assertIn("wake up", sender.deliveries[-1].text)
        self.assertTrue(await sender.react(source, "👍"))
        self.assertTrue(await sender.react(source, "👍"))
        self.assertEqual(sender.reactions, [("source1", "👍")])
        self.assertEqual(sender.prepare_face("hello", "喜", "強い"), "hello")
        await sender.show_face(source, sent, "喜", "強い", "hello")
        self.assertEqual(sender.faces, [("source1", "喜", "強い")])
        self.assertIsNone(await sender.find_reply(source_event("unknown")))

    async def test_separate_stub_process_senders_use_unique_message_ids(self):
        first = StubDiscordSender(clock=lambda: NOW)
        second = StubDiscordSender(clock=lambda: NOW)
        first_sent = await first.send(source_event("source1"), "one")
        second_sent = await second.send(source_event("source2"), "two")
        self.assertNotEqual(first_sent.id, second_sent.id)
        self.assertRegex(first_sent.id, r"^stub-[0-9a-f]{32}$")


class StubDiscordAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_submits_discord_shaped_event_with_cached_attachments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            picture = root / "picture.png"
            picture.write_bytes(b"png")
            unknown = root / "README"
            unknown.write_bytes(b"text")
            router = FakeRouter()
            identifiers = iter(("stub1", "stub2"))
            adapter = StubDiscordAdapter(
                router, root, clock=lambda: NOW, id_factory=lambda: next(identifiers)
            )

            event, outcome = await adapter.submit(
                "これを残して", sandbox=SandboxKey("guild", "30"),
                attachments=(picture, unknown,), channel_id="10", author_id="20",
            )

            self.assertEqual(outcome.reply, "stub reply")
            self.assertEqual(event.id, "stub1")
            self.assertTrue(event.directed_to_agent)
            self.assertEqual(
                [item.cache_name for item in event.attachments],
                ["stub1-0.png", "stub1-1.bin"],
            )
            self.assertEqual(
                (root / "sandboxes/guilds/30/attachments/stub1-0.png").read_bytes(),
                b"png",
            )
            self.assertFalse(router.calls[0][1])
            second, _ = await adapter.submit("again", sandbox=SandboxKey("guild", "30"))
            self.assertEqual(second.id, "stub2")

    async def test_rejects_dm_missing_empty_symlink_and_large_attachments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = StubDiscordAdapter(FakeRouter(), root, clock=lambda: NOW)
            with self.assertRaisesRegex(ValueError, "guild"):
                await adapter.submit("x", sandbox=SandboxKey("dm", "30"))
            missing = root / "missing.bin"
            with self.assertRaisesRegex(ValueError, "regular"):
                await adapter.submit(
                    "x", sandbox=SandboxKey("guild", "30"), attachments=(missing,)
                )
            empty = root / "empty.bin"
            empty.touch()
            with self.assertRaisesRegex(ValueError, "invalid size"):
                await adapter.submit(
                    "x", sandbox=SandboxKey("guild", "30"), attachments=(empty,)
                )
            target = root / "target.bin"
            target.write_bytes(b"x")
            link = root / "link.bin"
            link.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "regular"):
                await adapter.submit(
                    "x", sandbox=SandboxKey("guild", "30"), attachments=(link,)
                )
            large = root / "large.bin"
            large.write_bytes(b"x" * (MAX_ARTIFACT_BYTES + 1))
            with self.assertRaisesRegex(ValueError, "invalid size"):
                await adapter.submit(
                    "x", sandbox=SandboxKey("guild", "30"), attachments=(large,)
                )

    async def test_attachment_flows_through_resource_tool_into_scoped_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = SandboxKey("guild", "30")
            store = InventoryStore(root, key)
            inventory = InventoryResourceProvider(store)
            attachments = AttachmentResourceProvider(store)
            registry = ResourceRegistry((
                ResourceRegistration(ResourceCollectionSpec(
                    "core.inventory", "core", "inventory", "sandbox",
                    frozenset({"list", "read", "write", "delete", "export", "import"}),
                    writable_content="text", transfer_policy="copy",
                ), inventory),
                ResourceRegistration(ResourceCollectionSpec(
                    "interface.current_attachments", "core", "current attachments", "turn",
                    frozenset({"list", "read", "export"}),
                    transfer_policy="copy",
                ), attachments),
            ))
            tools = ToolRegistry((ResourceToolProvider(registry),))

            class ResourceRouter:
                async def submit(self, received, *, allow_reactions=True):
                    self.received = received
                    self.allow_reactions = allow_reactions
                    prepared = await tools.prepare(CapabilityContext(received, key))
                    self.result = await prepared.execute(
                        "resource_transfer",
                        '{"source_collection":"interface.current_attachments",'
                        '"source_id":"0","destination_collection":"core.inventory",'
                        '"destination_id":"saved-note.txt"}',
                    )
                    return SimpleNamespace(kind=OutcomeKind.SPOKE, reply="保存したよ")

            source = root / "note.txt"
            source.write_text("stub attachment", encoding="utf-8")
            router = ResourceRouter()
            adapter = StubDiscordAdapter(
                router, root, clock=lambda: NOW, id_factory=lambda: "stub-resource"
            )
            received, outcome = await adapter.submit(
                "これを残して", sandbox=key, attachments=(source,)
            )

            self.assertEqual(outcome.reply, "保存したよ")
            self.assertFalse(router.allow_reactions)
            self.assertEqual(received.attachments[0].cache_name, "stub-resource-0.txt")
            self.assertEqual(router.result.status, "success")
            self.assertEqual(
                store.resolve("saved-note.txt", location="inventory").read_text(encoding="utf-8"),
                "stub attachment",
            )
            other = InventoryStore(root, SandboxKey("guild", "31"))
            self.assertEqual(other.list(include_temporary=True), ())


class StubDiscordCliTests(unittest.TestCase):
    def test_cli_routes_message_attachments_and_isolated_state(self):
        settings = MagicMock()
        result = {"outcome": "spoke"}
        with (
            patch("anima.bootstrap.cli.Settings.load", return_value=settings),
            patch(
                "anima.bootstrap.discord_stub.run_discord_stub",
                new=AsyncMock(return_value=result),
            ) as run,
            patch("anima.bootstrap.cli._print") as output,
        ):
            main([
                "discord-stub", "残して", "--attachment", "one.png",
                "--attachment", "two.txt", "--guild-id", "30",
                "--state-root", "/tmp/stub-test",
            ])

        run.assert_awaited_once_with(
            settings, "残して", state_root=Path("/tmp/stub-test"), guild_id="30",
            attachments=(Path("one.png"), Path("two.txt")),
        )
        output.assert_called_once_with(result, True)


class StubDiscordLiveHarnessTests(unittest.IsolatedAsyncioTestCase):
    def test_telemetry_capture_and_usage_summary(self):
        with _capture_telemetry() as records:
            logging.getLogger("anima.telemetry").info("not-json")
            logging.getLogger("anima.telemetry").info("[]")
            emit(
                "openai.response.round_completed", operation="respond", round=1,
                input_tokens=10, output_tokens=2, total_tokens=12,
            )
            emit(
                "openai.response.completed", operation="respond", input_tokens=10,
                output_tokens=2, total_tokens=12, request_count=1, tool_call_count=0,
            )
        summary = _usage(records)
        self.assertTrue(summary["available"])
        self.assertEqual(summary["total_tokens"], 12)
        self.assertEqual(summary["rounds"][0]["input_tokens"], 10)
        self.assertEqual(_usage([])["available"], False)

    async def test_live_harness_composes_isolated_runtime_and_reports_results(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings.load(cwd=root, environ={
                "DISCORD_BOT_TOKEN": "unused-discord",
                "OPENAI_API_KEY": "test-openai",
            })
            sender = StubDiscordSender(clock=lambda: NOW, id_factory=lambda: "unused")
            sender.deliveries.append(StubDelivery(
                "stub-1", "source", "reply", (Path("saved.png"),),
            ))
            sender.reactions.append(("source", "👍"))
            sender.faces.append(("source", "喜", "強い"))
            inventory = SimpleNamespace(list=MagicMock(return_value=(
                SimpleNamespace(to_dict=lambda: {"id": "saved.png"}),
            )))
            runtime = SimpleNamespace(
                actor=SimpleNamespace(
                    context_builder=SimpleNamespace(inventory=inventory),
                )
            )
            event = source_event("stub-event")
            outcome = SimpleNamespace(kind=OutcomeKind.SPOKE, reply="reply")

            class Router:
                def __init__(self, state_root, factory, *, policy):
                    self.state_root = state_root
                    self.policy = policy
                    self.value = factory(
                        SandboxKey("guild", "30"),
                        SandboxKey("guild", "30").path(state_root),
                    )
                    self.started = self.stopped = False

                async def start(self):
                    self.started = True

                async def runtime(self, key):
                    self.key = key
                    return self.value

                async def stop(self):
                    self.stopped = True

            async def submit(*args, **kwargs):
                emit(
                    "openai.response.round_completed", operation="respond", round=1,
                    input_tokens=100, output_tokens=20, total_tokens=120,
                )
                emit(
                    "openai.response.completed", operation="respond", input_tokens=100,
                    output_tokens=20, total_tokens=120, request_count=1,
                    tool_call_count=2,
                )
                return event, outcome

            adapter = SimpleNamespace(submit=AsyncMock(side_effect=submit))
            loader = MagicMock()
            loader.load.return_value = "plugins"
            with (
                patch("anima.bootstrap.discord_stub.StubDiscordSender", return_value=sender),
                patch("anima.bootstrap.discord_stub.ActivityModeStore", return_value="modes"),
                patch("anima.bootstrap.discord_stub.PluginLoader", return_value=loader),
                patch("anima.bootstrap.discord_stub.SandboxRouter", Router),
                patch("anima.bootstrap.discord_stub.StubDiscordAdapter", return_value=adapter),
                patch("anima.bootstrap.discord_stub.build_sandbox", return_value=runtime) as build,
            ):
                result = await run_discord_stub(
                    settings, "残して", state_root=root / "stub-state", guild_id="30",
                    attachments=(Path("one.png"),),
                )

            self.assertEqual(result["outcome"], "spoke")
            self.assertEqual(result["inventory"], [{"id": "saved.png"}])
            self.assertEqual(result["deliveries"][0]["attachments"], ["saved.png"])
            self.assertEqual(result["reactions"], [["source", "👍"]])
            self.assertEqual(result["faces"], [["source", "喜", "強い"]])
            self.assertEqual(result["usage"], {
                "available": True,
                "input_tokens": 100,
                "output_tokens": 20,
                "total_tokens": 120,
                "request_count": 1,
                "tool_call_count": 2,
                "rounds": [{
                    "round": 1, "input_tokens": 100,
                    "output_tokens": 20, "total_tokens": 120,
                }],
            })
            adapter.submit.assert_awaited_once_with(
                "残して", sandbox=SandboxKey("guild", "30"),
                attachments=(Path("one.png"),),
            )
            build.assert_called_once()


if __name__ == "__main__":
    unittest.main()
