from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from anima.bootstrap.app import build_client, build_sandbox, main
from anima.bootstrap.settings import Settings
from anima.capabilities.plugin_loader import PluginLoader
from anima.capabilities.contracts import CapabilityContext
from anima.core.access import ActivityModeStore
from anima.core.models import Event, OutcomeKind
from anima.core.sandbox import SandboxKey
from anima.adapters.stub.discord import StubDiscordSender
from test_adapters import FakeOpenAIClient
import asyncio


def settings_at(root, plugins="echo"):
    config = root / "config"
    config.mkdir()
    for name in ("persona.md", "rules.md", "appearance.md"):
        (config / name).write_text("Neutral sample.", encoding="utf-8")
    return Settings.load(cwd=root, environ={
        "DISCORD_BOT_TOKEN": "unused", "OPENAI_API_KEY": "unused",
        "ANIMA_ROOT": "config",
        "ANIMA_ALLOWED_GUILD_IDS": "1", "ANIMA_PLUGINS": plugins,
    })


class CompositionTests(unittest.IsolatedAsyncioTestCase):
    def test_contextual_address_composition_scope_and_mode(self):
        with TemporaryDirectory() as directory:
            settings = settings_at(Path(directory))
            activity = ActivityModeStore(settings.state_root)
            limit = asyncio.Semaphore(4)
            catalog = PluginLoader().load()
            with patch("anima.adapters.openai.client.AsyncOpenAI"), patch("anima.bootstrap.app.AsyncOpenAI") as api:
                for kind in ("guild", "dm"):
                    key = SandboxKey(kind, "1")
                    runtime = build_sandbox(settings, key, key.path(settings.state_root),
                                            StubDiscordSender(clock=lambda: datetime.now(timezone.utc)),
                                            activity, catalog, limit, lambda: None)
                    if kind == "guild":
                        self.assertIs(runtime.actor.address_classifier.semaphore, limit)
                        self.assertEqual(runtime.actor.address_classifier.target.model, settings.openai_model)
                        api.assert_any_call(api_key=settings.openai_api_key, timeout=15, max_retries=0)
                    else:
                        self.assertIsNone(runtime.actor.address_classifier)
                    self.assertTrue(runtime.actor.contextual_reply_allowed())
                    self.assertIsNone(runtime.actor.face_preparer)
                    self.assertIsNone(runtime.actor.face_presenter)
                    if kind == "guild":
                        activity.set(key, "silent", changed_by="test", changed_at=datetime.now(timezone.utc))
                        self.assertFalse(runtime.actor.contextual_reply_allowed())

    async def test_complete_runtime_works_with_only_reference_echo(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            settings = settings_at(root)
            source = Event("one", datetime.now(timezone.utc), "channel", "10", "general", "20", "Tester", "hello", mention=True, guild_id="1", sandbox_key="guild:1")
            fake = FakeOpenAIClient()
            create = fake.responses.create
            async def quiet_response(**kwargs):
                response = await create(**kwargs)
                response.output = []
                return response
            fake.responses.create = quiet_response
            with patch("anima.adapters.openai.client.AsyncOpenAI", return_value=fake), patch("anima.bootstrap.app.AsyncOpenAI"):
                runtime = build_sandbox(settings, SandboxKey("guild", "1"), SandboxKey("guild", "1").path(settings.state_root), StubDiscordSender(clock=lambda: datetime.now(timezone.utc)), ActivityModeStore(settings.state_root), PluginLoader().load(), asyncio.Semaphore(4), lambda: None)
                await runtime.plugins.start()
                await runtime.actor.start()
                try:
                    outcome = await runtime.actor.submit(source)
                    self.assertEqual(outcome.kind, OutcomeKind.SPOKE)
                    prepared = await runtime.actor.responder.target.tool_registry.prepare(CapabilityContext(source, SandboxKey("guild", "1")))
                    names = {spec.name for spec in prepared.specs}
                    self.assertIn("echo", names)
                    self.assertIn("resource_read", names)
                    self.assertNotIn("generate_image", names)
                    self.assertEqual({spec.path for spec in runtime.commands.specs}, {("echo",), ("anima-mode",), ("anima-maintenance",)})
                    runtime.reload_configuration()
                    self.assertEqual(runtime.actor.responder.target.appearance, "Neutral sample.")
                    self.assertEqual(runtime.self_time.max_iterations, 4)
                    self.assertTrue((runtime.actor.store.root / "runtime" / "resources.json").exists())
                    self.assertTrue((runtime.actor.store.root / "runtime" / "skills.json").exists())
                    from test_skills import make_skill
                    make_skill(settings.anima_root / "skills", metadata='metadata:\n  anima.requires: "echo"\n')
                    runtime.reload_configuration()
                    prepared = await runtime.actor.responder.target.tool_registry.prepare(CapabilityContext(source, SandboxKey("guild", "1")))
                    self.assertIn("core:sample", prepared.contextual_instructions)
                    self.assertNotIn("Secret detailed workflow", prepared.contextual_instructions)
                    read = await prepared.execute("resource_read", '{"collection":"core.skills","resource_id":"core:sample","attach_to_reply":false}')
                    self.assertEqual(read.status, "success")
                    import json
                    skill_status = json.loads((runtime.actor.store.root / "runtime" / "skills.json").read_text())
                    self.assertEqual(skill_status["reads"][-1]["skill_id"], "core:sample")
                    # Plugin-local skills are mounted from the catalog, not the application root.
                    plugin_root = Path(directory) / "echo-skills"
                    make_skill(plugin_root, "bundled")
                    catalog = PluginLoader().load()
                    catalog.skill_roots["echo"] = plugin_root
                    other = build_sandbox(settings, SandboxKey("guild", "2"), SandboxKey("guild", "2").path(settings.state_root), StubDiscordSender(clock=lambda: datetime.now(timezone.utc)), ActivityModeStore(settings.state_root), catalog, asyncio.Semaphore(4), lambda: None)
                    other_context = CapabilityContext(None, SandboxKey("guild", "2"))
                    other_tools = await other.actor.responder.target.tool_registry.prepare(other_context)
                    self.assertIn("echo:bundled", other_tools.contextual_instructions)
                finally:
                    await runtime.actor.stop()
                    await runtime.plugins.stop()

    async def test_client_policy_and_unknown_plugin_fail_fast(self):
        with TemporaryDirectory() as directory:
            settings = settings_at(Path(directory))
            client = build_client(settings)
            self.assertTrue(client.policy.allows(SandboxKey("guild", "1")))
            self.assertFalse(client.policy.allows(SandboxKey("guild", "2")))
            await client.close()

    def test_main_owns_process_lock_dashboard_and_configuration_reload(self):
        with TemporaryDirectory() as directory:
            settings = settings_at(Path(directory))
            with patch("anima.bootstrap.app.Settings.load", return_value=settings), patch("anima.bootstrap.app.ProcessLock") as lock, patch("anima.bootstrap.app.configure_logging"), patch("anima.bootstrap.app.build_client") as factory, patch("anima.bootstrap.app.DashboardServer") as dashboard:
                main()
            lock.assert_called_once_with(settings.state_root / "runtime" / "bot.lock")
            factory.return_value.run.assert_called_once_with("unused", log_handler=None)
            self.assertEqual(dashboard.call_args.kwargs["host"], "127.0.0.1")


class ConfigurationExtensionsTests(unittest.TestCase):
    def test_plugin_file_environment_and_dashboard_precedence(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            settings_at(root)
            config = root / "config"
            (config / "plugins").mkdir()
            (config / "plugins" / "voice.json").write_text('{"speed":120}', encoding="utf-8")
            env = {"DISCORD_BOT_TOKEN":"unused", "OPENAI_API_KEY":"unused", "ANIMA_VOICE_SPEED":"140", "ANIMA_ECHO_PREFIX":"Test: "}
            settings = Settings.load(cwd=root, environ=env)
            self.assertEqual(settings.plugin_configuration["voice"]["speed"], 140)
            self.assertEqual(settings.plugin_configuration["echo"]["prefix"], "Test: ")
            (config / "config.json").write_text('{"voice.speed":160}', encoding="utf-8")
            self.assertEqual(Settings.load(cwd=root, environ=env).plugin_configuration["voice"]["speed"], 160)
            for content in ('[]', 'not-json', '{"unknown":true}'):
                (config / "plugins" / "voice.json").write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError):
                    Settings.load(cwd=root, environ=env)
