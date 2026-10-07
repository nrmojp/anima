import asyncio
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from anima.adapters.storage import FilePluginStorage, FilePluginStorageFactory
from anima.bootstrap.runtime import RuntimeHost
from anima.capabilities.availability import Availability, AvailabilityStatus
from anima.capabilities.configuration import ConfigField, resolve_configuration
from anima.capabilities.commands import CommandContext
from anima.capabilities.plugin_loader import PluginLoader
from anima.capabilities.plugins import (
    BoundPlugin, DashboardPanelSpec, PluginCatalog, PluginManifest, UnavailablePlugin,
)
from anima.capabilities.responses import (
    ResponseContribution, ResponseContributionRegistry,
)
from anima.capabilities.tools import CapabilityContext, ToolResult
from anima.core.events import EventIngress, SandboxEventMailbox
from anima.core.models import ActionRecord, ContextReference, Event, ResponseDraft
from anima.core.sandbox import SandboxKey
from anima.core.services import SandboxServices
from anima.core.storage import PluginStorage, PluginStorageFactory
from anima.plugins.echo import EchoPluginDefinition


JST = ZoneInfo("Asia/Tokyo")


def event(key=SandboxKey("guild", "1"), *, identifier="e", text="hello"):
    return Event(
        id=identifier, ts=datetime.now(JST), kind="channel", channel_id="10",
        channel_name="#test", author_id="20", author_name="tester", text=text,
        guild_id=key.id if key.kind == "guild" else None, sandbox_key=str(key),
    )


def draft(text="ok"):
    return ResponseDraft(text, "calm", "test", "弱い", "testing")


class Provider:
    def __init__(self, contribution, result=None):
        self.contribution = contribution
        self.result = result or ToolResult("success", {"ok": True})
        self.calls = []

    def response_contributions(self):
        if isinstance(self.contribution, (tuple, list)):
            return self.contribution
        return (self.contribution,)

    async def consume_response(self, property_name, value, context):
        self.calls.append((property_name, value, context))
        return self.result


class Definition:
    def __init__(self, manifest, *, fail=False, log=None):
        self.manifest = manifest
        self.fail = fail
        self.log = log if log is not None else []

    def create(self, services, configuration):
        return Instance(self.manifest, self.fail, self.log, services, configuration)


class Instance:
    tool_providers = ()
    command_providers = ()

    def __init__(self, manifest, fail, log, services, configuration):
        self.manifest = manifest
        self.fail = fail
        self.log = log
        self.services = services
        self.configuration = configuration
        self.running = False

    async def start(self):
        self.log.append("start:" + self.manifest.name)
        if self.fail:
            raise RuntimeError("boom")
        self.running = True

    async def stop(self):
        self.log.append("stop:" + self.manifest.name)
        self.running = False

    def snapshot(self) -> Mapping[str, object]:
        return {"running": self.running}


class Service:
    def __init__(self, log, *, fail=False):
        self.log = log
        self.fail = fail

    async def start(self):
        self.log.append("service:start")
        if self.fail:
            raise RuntimeError("service failed")

    async def stop(self):
        self.log.append("service:stop")


class Candidate:
    def __init__(self, name, ispkg=True):
        self.name = name
        self.ispkg = ispkg


class GenericRuntimeRoutesTests(unittest.IsolatedAsyncioTestCase):
    def test_availability_and_configuration(self):
        self.assertEqual(AvailabilityStatus(Availability.AVAILABLE).state, Availability.AVAILABLE)
        with self.assertRaises(TypeError):
            AvailabilityStatus("available")
        with self.assertRaises(ValueError):
            AvailabilityStatus(Availability.DISABLED, "")
        field = ConfigField(
            "count", "ANIMA_COUNT", "count", "Count", "Test", "int", 2,
            minimum=1, maximum=3, plugin="echo",
        )
        self.assertEqual(resolve_configuration((field,), {"count": 3}), {"count": 3})
        for value in (True, 0, 4):
            with self.assertRaises(ValueError):
                field.validate(value)
        with self.assertRaises(ValueError):
            resolve_configuration((field,), {"unknown": 1})
        fields = (
            ConfigField("enabled", "ANIMA_ENABLED", "enabled", "Enabled", "Test", "bool", True),
            ConfigField("names", "ANIMA_NAMES", "names", "Names", "Test", "list", ()),
            ConfigField("name", "ANIMA_NAME", "name", "Name", "Test", "text", "x"),
            ConfigField("ratio", "ANIMA_RATIO", "ratio", "Ratio", "Test", "float", 0.5),
            ConfigField("mode", "ANIMA_MODE", "mode", "Mode", "Test", "text", "a", options=("a", "b")),
        )
        self.assertEqual(resolve_configuration(fields)["name"], "x")
        for selected, value in ((fields[0], 1), (fields[1], [1]), (fields[2], 1),
                                (fields[3], "x"), (fields[4], "c")):
            with self.subTest(field=selected.key), self.assertRaises(ValueError):
                selected.validate(value)
        with self.assertRaises(ValueError):
            ConfigField("", "X", "x", "X", "X", "text", "x")
        with self.assertRaises(ValueError):
            ConfigField("x", "TOKEN", "x", "X", "X", "text", "x")
        for value in (float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                fields[3].validate(value)

    async def test_response_contribution_composes_and_consumes(self):
        contribution = ResponseContribution(
            "selection", {"type": "array", "items": {"type": "string"}},
        )
        result = ToolResult(
            "success", {"ok": True},
            actions=(ActionRecord("echo", "select", "selected"),),
            references=(ContextReference("echo", "item", "one"),),
            attachments=(Path("/tmp/one.txt"),),
        )
        provider = Provider(contribution, result)
        prepared = ResponseContributionRegistry((provider,)).prepare(
            CapabilityContext(event(), SandboxKey("guild", "1"))
        )
        base = {
            "type": "object", "properties": {"reply": {"type": "string"}},
            "required": ["reply"], "additionalProperties": False,
        }
        composed = prepared.compose_format(base)
        self.assertIn("selection", composed["schema"]["properties"])
        reply, results = await prepared.consume_value(
            {"reply": "done", "selection": ["one"]}
        )
        self.assertEqual((reply, results), ("done", (result,)))
        self.assertEqual(provider.calls[0][1], ["one"])
        empty = ResponseContributionRegistry().prepare(
            CapabilityContext(event(), SandboxKey("guild", "1"))
        )
        self.assertIsNone(empty.format)
        self.assertEqual(await empty.consume(" plain "), (" plain ", ()))
        with self.assertRaises(ValueError):
            prepared.compose_format({
                "properties": {"selection": {"type": "string"}},
                "required": ["selection"],
            })
        with self.assertRaises(RuntimeError):
            await prepared.consume("not-json")
        with self.assertRaises(ValueError):
            ResponseContribution("reply", {"type": "string"})

    async def test_response_contribution_validation_and_failures(self):
        for name in ("Bad", "mood", "face", "", "a-b"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                ResponseContribution(name, {"type": "string"})
        invalid_schemas = (
            {"type": "object"},
            {"type": "string", "minimum": 1},
            {"type": "string", "enum": "x"},
            {"type": "string", "enum": [1]},
            {"type": "string", "items": {"type": "string"}},
            {"type": "array", "items": "string"},
            {"type": "array"},
            {"type": "array", "items": {"type": "object"}},
            {"type": "string", "description": object()},
        )
        for schema in invalid_schemas:
            with self.subTest(schema=schema), self.assertRaises(ValueError):
                ResponseContribution("extra", schema)
        with self.assertRaises(TypeError):
            ResponseContributionRegistry((object(),))
        required = ResponseContribution("count", {"type": "integer"}, True)
        optional = ResponseContribution("modes", {
            "type": "array", "items": {"type": "string", "enum": ["on"]},
        })
        provider = Provider((required, optional))
        prepared = ResponseContributionRegistry((provider,)).prepare(
            CapabilityContext(event(), SandboxKey("guild", "1"))
        )
        self.assertIsNotNone(prepared.format)
        for value in (
            [], {"reply": "x", "unknown": 1}, {"reply": " "},
            {"reply": "x"}, {"reply": "x", "count": True},
            {"reply": "x", "count": 1, "modes": ["off"]},
        ):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                await prepared.consume_value(value)
        self.assertEqual(
            await prepared.consume_value({"reply": "ok", "count": 1, "modes": None}),
            ("ok", (provider.result,)),
        )
        self.assertEqual(
            await prepared.consume('{"reply":"ok","count":1}'),
            ("ok", (provider.result,)),
        )
        with self.assertRaises(ValueError):
            ResponseContributionRegistry((Provider([], None),)).prepare(
                CapabilityContext(event(), SandboxKey("guild", "1"))
            )
        with self.assertRaises(ValueError):
            ResponseContributionRegistry((Provider((object(),)),)).prepare(
                CapabilityContext(event(), SandboxKey("guild", "1"))
            )
        duplicate = ResponseContribution("same", {"type": "string"})
        with self.assertRaises(ValueError):
            ResponseContributionRegistry((Provider((duplicate,)), Provider((duplicate,)))).prepare(
                CapabilityContext(event(), SandboxKey("guild", "1"))
            )
        wrong = Provider((duplicate,), result="wrong")
        with self.assertRaises(TypeError):
            await ResponseContributionRegistry((wrong,)).prepare(
                CapabilityContext(event(), SandboxKey("guild", "1"))
            ).consume_value({"reply": "ok", "same": "x"})
        item_schema = {"type": "string"}
        mutable = ResponseContribution(
            "mutable", {"type": "array", "items": item_schema},
        )
        item_schema["type"] = "object"
        with self.assertRaises(RuntimeError):
            await ResponseContributionRegistry((Provider((mutable,)),)).prepare(
                CapabilityContext(event(), SandboxKey("guild", "1"))
            ).consume_value({"reply": "ok", "mutable": []})

    async def test_event_mailbox_serializes_and_checks_sandbox(self):
        key = SandboxKey("guild", "1")
        mailbox = SandboxEventMailbox(key)
        self.assertIsInstance(mailbox, EventIngress)
        with self.assertRaises(RuntimeError):
            await mailbox.publish(event(key))
        active = maximum = 0

        async def handler(source):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0)
            active -= 1
            return draft(source.text)

        mailbox.bind(handler)
        with self.assertRaises(RuntimeError):
            mailbox.bind(handler)
        await asyncio.gather(*(mailbox.publish(event(key, identifier=str(i))) for i in range(3)))
        self.assertEqual(maximum, 1)
        await mailbox.start()
        mailbox.unbind()
        async def invalid(_source):
            return "bad"
        mailbox.bind(invalid)
        with self.assertRaises(TypeError):
            await mailbox.publish(event(key))
        with self.assertRaises(ValueError):
            await mailbox.publish(event(SandboxKey("guild", "2")))
        await mailbox.stop()
        with self.assertRaises(RuntimeError):
            await mailbox.publish(event(key))
        with self.assertRaises(TypeError):
            SandboxEventMailbox(key).bind(None)

    def test_plugin_loader_and_catalog(self):
        catalog = PluginLoader().load()
        self.assertIn("echo", [manifest.name for manifest in catalog.manifests])
        with self.assertRaises(ValueError):
            PluginLoader("bad-name")
        panel = DashboardPanelSpec("echo", "Echo")
        self.assertEqual(panel.to_dict()["renderer"], "status")
        self.assertEqual(panel.to_dict()["group"], "status")
        with self.assertRaises(ValueError):
            DashboardPanelSpec("echo", "Echo", group="unknown")
        manifest = PluginManifest("sample", "1", provides=("sample",), dashboard_panels=(panel,))
        self.assertEqual(manifest.name, "sample")
        with self.assertRaises(ValueError):
            PluginCatalog((EchoPluginDefinition(), EchoPluginDefinition()))

    @patch("anima.capabilities.plugin_loader.find_spec")
    @patch("anima.capabilities.plugin_loader.pkgutil.iter_modules")
    @patch("anima.capabilities.plugin_loader.import_module")
    def test_plugin_loader_boundaries(self, import_module, iter_modules, find_spec):
        import_module.return_value = object()
        with self.assertRaises(ValueError):
            PluginLoader("example.plugins").load()
        import_module.return_value = SimpleNamespace(__path__=("plugins",))
        iter_modules.return_value = (Candidate("module", False), Candidate("config"))
        find_spec.return_value = None
        self.assertEqual(PluginLoader("example.plugins").load().manifests, ())
        find_spec.return_value = object()
        import_module.side_effect = (
            SimpleNamespace(__path__=("plugins",)), SimpleNamespace(),
        )
        iter_modules.return_value = (Candidate("broken"),)
        with self.assertRaises(ValueError):
            PluginLoader("example.plugins").load()
        import_module.side_effect = (
            SimpleNamespace(__path__=("plugins",)),
            SimpleNamespace(PLUGIN=Definition(PluginManifest("different", "1"))),
        )
        iter_modules.return_value = (Candidate("expected"),)
        with self.assertRaises(ValueError):
            PluginLoader("example.plugins").load()

    def test_plugin_manifest_panel_and_catalog_validation(self):
        panel = DashboardPanelSpec("status", "Status")
        invalid_manifests = (
            {"name": "Bad", "version": "1"},
            {"name": "ok", "version": ""},
            {"name": "ok", "version": "1", "description": "x" * 241},
            {"name": "ok", "version": "1", "provides": ("Bad",)},
            {"name": "ok", "version": "1", "provides": ("x", "x")},
            {"name": "ok", "version": "1", "configuration": (
                ConfigField("x", "ANIMA_X", "x", "X", "X", "text", ""),
            ) * 2},
            {"name": "ok", "version": "1", "dashboard_panels": (panel, panel)},
            {"name": "ok", "version": "1", "dashboard_panels": (object(),)},
        )
        for kwargs in invalid_manifests:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                PluginManifest(**kwargs)
        for args in (
            ("Bad", "Title"), ("ok", ""), ("ok", "x" * 81),
            ("ok", "Title", "Bad"), ("ok", "Title", "status", -1),
            ("ok", "Title", "status", 1001),
            ("ok", "Title", "status", 1, "x" * 241),
            ("ok", "Title", "status", 1, "", ""),
        ):
            with self.subTest(args=args), self.assertRaises(ValueError):
                DashboardPanelSpec(*args)
        base = Definition(PluginManifest("base", "1", provides=("base",)))
        child = Definition(PluginManifest("child", "1", requires=("base",)))
        self.assertEqual([m.name for m in PluginCatalog((child, base)).manifests], ["base", "child"])
        with self.assertRaises(ValueError):
            PluginCatalog((base, Definition(PluginManifest("other", "1", provides=("base",))),))
        with self.assertRaises(ValueError):
            PluginCatalog((Definition(PluginManifest("missing", "1", requires=("none",))),))
        a = Definition(PluginManifest("a", "1", provides=("a",), requires=("b",)))
        b = Definition(PluginManifest("b", "1", provides=("b",), requires=("a",)))
        with self.assertRaises(ValueError):
            PluginCatalog((a, b))
        shared = DashboardPanelSpec("same", "Same")
        with self.assertRaises(ValueError):
            PluginCatalog((
                Definition(PluginManifest("pa", "1", dashboard_panels=(shared,))),
                Definition(PluginManifest("pb", "1", dashboard_panels=(shared,))),
            ))

    async def test_plugin_creation_lifecycle_and_status(self):
        key = SandboxKey("guild", "1")
        base = Definition(PluginManifest("base", "1", provides=("base",)))
        child = Definition(PluginManifest("child", "1", requires=("base",)))
        catalog = PluginCatalog((base, child))
        with self.assertRaises(ValueError):
            catalog.create(frozenset({"unknown"}), SandboxServices(key))
        with self.assertRaises(ValueError):
            catalog.create(frozenset({"child"}), SandboxServices(key))
        with self.assertRaises(ValueError):
            catalog.create(frozenset({"base"}), SandboxServices(key), {"unknown": {}})
        with self.assertRaises(TypeError):
            catalog.create(frozenset({"base"}), SandboxServices(key, {"plugin_status_path": "bad"}))
        log = []
        runtime = PluginCatalog((
            Definition(PluginManifest("one", "1"), log=log),
            Definition(PluginManifest("two", "1"), fail=True, log=log),
        )).create(frozenset({"one", "two"}), SandboxServices(key))
        with self.assertRaises(RuntimeError):
            await runtime.start()
        self.assertEqual(log, ["start:one", "start:two", "stop:one"])
        with TemporaryDirectory() as directory:
            status = Path(directory) / "plugins.json"
            echo = PluginCatalog((EchoPluginDefinition(),)).create(
                frozenset({"echo"}), SandboxServices(key, {"plugin_status_path": status}),
            )
            await echo.start()
            await echo.start()
            plugin = echo.instances[0]
            context = CapabilityContext(event(), key)
            self.assertEqual((await plugin.execute_tool("unknown", {}, context, "id")).status, "rejected")
            command_context = CommandContext(event(), key, "20")
            self.assertEqual((await plugin.execute_command(
                ("echo",), {"message": "hi"}, command_context,
            )).text, "Echo: hi")
            await echo.stop()
            self.assertTrue(status.exists())
        unavailable = UnavailablePlugin(PluginManifest("missing", "1"))
        await unavailable.start()
        self.assertFalse(unavailable.snapshot()["available"])
        await unavailable.stop()
        bound = BoundPlugin(PluginManifest("provided", "1"))
        await bound.start()
        await bound.stop()

    async def test_bound_plugin_lifecycle_and_plugin_factory_availability(self):
        log = []

        async def start():
            log.append("start")

        def stop():
            log.append("stop")

        bound = BoundPlugin(
            PluginManifest("bound", "1"), start=start, stop=stop,
            available=lambda: True,
        )
        await bound.start()
        await bound.start()
        self.assertTrue(bound.snapshot()["available"])
        await bound.stop()
        await bound.stop()
        self.assertEqual(log, ["start", "stop"])
        async def async_stop():
            log.append("async-stop")
        asynchronous = BoundPlugin(
            PluginManifest("asynchronous", "1"), start=lambda: log.append("sync-start"),
            stop=async_stop,
        )
        await asynchronous.start()
        await asynchronous.stop()
        self.assertEqual(log[-2:], ["sync-start", "async-stop"])

    async def test_runtime_host_lifecycle(self):
        host = RuntimeHost(
            PluginCatalog((EchoPluginDefinition(),)), frozenset({"echo"}),
            lambda key: SandboxServices(key), {"echo": {"prefix": "> "}},
        )
        key = SandboxKey("guild", "1")
        runtime = await host.start_sandbox(key)
        self.assertIs(await host.start_sandbox(key), runtime)
        prepared = await runtime.tools.prepare(CapabilityContext(event(key), key))
        result = await prepared.execute("echo", '{"message":"hi"}')
        self.assertEqual(result.model_payload["text"], "> hi")
        self.assertEqual(len(runtime.commands.specs), 1)
        self.assertEqual(runtime.responses.providers, ())
        self.assertEqual(runtime.snapshot()["sandbox"], "guild:1")
        self.assertEqual(host.sandbox(key), runtime)
        await host.stop()
        self.assertEqual(host.snapshots(), ())

    async def test_runtime_host_failures_and_services(self):
        key = SandboxKey("guild", "1")
        host = RuntimeHost(
            PluginCatalog((EchoPluginDefinition(),)), frozenset({"echo"}),
            lambda _key: SandboxServices(SandboxKey("guild", "2")),
        )
        with self.assertRaises(ValueError):
            await host.start_sandbox(key)
        log = []
        service = Service(log)
        host = RuntimeHost(
            PluginCatalog((EchoPluginDefinition(),)), frozenset({"echo"}),
            lambda value: SandboxServices(value, {"one": service, "two": service}),
        )
        await host.start_sandbox(key)
        await host.stop_sandbox(key)
        await host.stop_sandbox(key)
        self.assertEqual(log, ["service:start", "service:stop"])
        log = []
        first, failing = Service(log), Service(log, fail=True)
        host = RuntimeHost(
            PluginCatalog((EchoPluginDefinition(),)), frozenset({"echo"}),
            lambda value: SandboxServices(value, {"first": first, "failing": failing}),
        )
        with self.assertRaises(RuntimeError):
            await host.start_sandbox(key)
        self.assertEqual(log, ["service:start", "service:start", "service:stop"])

    def test_scoped_storage_and_services(self):
        with TemporaryDirectory() as directory:
            key = SandboxKey("guild", "123")
            factory = FilePluginStorageFactory(Path(directory), key)
            self.assertIsInstance(factory, PluginStorageFactory)
            storage = factory.for_plugin("echo")
            self.assertIsInstance(storage, PluginStorage)
            storage.write_json("state.json", {"count": 1})
            self.assertEqual(storage.read_json("state.json", {}), {"count": 1})
            self.assertIs(storage.read_json("missing.json", marker := object()), marker)
            self.assertIn("/sandboxes/guilds/123/world/echo/", str(
                storage.artifact_path("images", "one.png")
            ))
            temporary = storage.temporary_path("work.tmp")
            temporary.write_text("x")
            storage.clear_temporary()
            self.assertFalse(temporary.exists())
            services = SandboxServices(
                key, {"plugin_storage_factory": factory, "name": "value"},
            ).for_plugin("echo")
            self.assertIsInstance(services.require("plugin_storage", PluginStorage), PluginStorage)
            self.assertEqual(services.require("name", str), "value")
            with self.assertRaises(LookupError):
                services.require("missing", str)
            with self.assertRaises(TypeError):
                services.require("name", int)
            with self.assertRaises(ValueError):
                factory.for_plugin("Bad")
            with self.assertRaises(TypeError):
                SandboxServices(key, {"plugin_storage_factory": object()}).for_plugin("echo")
            with self.assertRaises(TypeError):
                SandboxServices(key, {"plugin_services": object()}).for_plugin("echo")
            with self.assertRaises(TypeError):
                SandboxServices(key, {"plugin_services": {"echo": object()}}).for_plugin("echo")
            self.assertEqual(SandboxServices(
                key, {"plugin_services": {"echo": {"name": "scoped"}}},
            ).for_plugin("echo").values["name"], "scoped")

    def test_storage_rejects_unsafe_paths(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            storage = FilePluginStorage(root / "state", root / "world")
            for operation in (
                lambda: storage.read_json("../secret", {}),
                lambda: storage.write_json("/secret", {}),
                lambda: storage.artifact_path("..", "x"),
                lambda: storage.temporary_path("../x"),
            ):
                with self.assertRaises(ValueError):
                    operation()
            storage.state_directory.mkdir(parents=True)
            target = root / "target"
            target.write_text("x")
            (storage.state_directory / "linked.json").symlink_to(target)
            with self.assertRaises(ValueError):
                storage.read_json("linked.json", {})
            (storage.artifact_directory / "covers").mkdir(parents=True)
            (storage.artifact_directory / "covers" / "linked.png").symlink_to(target)
            with self.assertRaises(ValueError):
                storage.artifact_path("covers", "linked.png")
            storage.temporary_directory.mkdir(parents=True)
            (storage.temporary_directory / "linked.tmp").symlink_to(target)
            with self.assertRaises(ValueError):
                storage.temporary_path("linked.tmp")
            storage.clear_temporary()
            storage.temporary_directory.parent.mkdir(parents=True, exist_ok=True)
            storage.temporary_directory.symlink_to(target)
            with self.assertRaises(ValueError):
                storage.clear_temporary()


if __name__ == "__main__":
    unittest.main()
