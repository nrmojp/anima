"""Generic framework extension boundaries, shared with the reference repository."""

import ast
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from anima.bootstrap.deployment import deployment_defaults
from anima.capabilities.plugins import PluginManifest
from anima.capabilities.responses import PreparedResponse, ResponseContributionRegistry
from anima.capabilities.tools import CapabilityContext, ToolResult
from anima.core.models import ResponseDraft
from anima.core.plugin_assembly import PluginAssembly


class FrameworkCompositionTests(unittest.TestCase):
    def test_optional_text_compatibility_helper(self):
        from anima.bootstrap.command_providers import _optional_text
        self.assertIsNone(_optional_text(None))
        self.assertEqual(_optional_text(" text "), "text")

    def test_legacy_configuration_aliases_require_unique_and_single_values(self):
        from anima.bootstrap.runtime_config import validate_runtime_config, RuntimeConfigError
        from anima.capabilities.configuration import ConfigField
        first = ConfigField("first.value", "TEST_FIRST", "value", "value", "test", "int", 1)
        second = ConfigField("second.value", "TEST_SECOND", "value", "value", "test", "int", 1)
        with patch("anima.bootstrap.runtime_config.PLUGIN_FIELDS", (first,)), \
                patch("anima.bootstrap.runtime_config.FIELD_BY_KEY", {first.key: first}):
            self.assertEqual(validate_runtime_config({"value": 2}), {"first.value": 2})
            with self.assertRaises(RuntimeConfigError):
                validate_runtime_config({"value": 2, "first.value": 3})
        with patch("anima.bootstrap.runtime_config.PLUGIN_FIELDS", (first, second)):
            with self.assertRaises(RuntimeConfigError):
                validate_runtime_config({"value": 2})

    def test_response_item_limits_are_generic_and_enforced(self):
        from anima.capabilities.responses import ResponseContribution, _matches
        schema = {"type": "array", "items": {"type": "string"}, "maxItems": 1}
        ResponseContribution("references", schema)
        self.assertTrue(_matches(["one"], schema))
        self.assertFalse(_matches(["one", "two"], schema))
        for value in (-1, True, "1"):
            with self.assertRaises(ValueError):
                ResponseContribution("references", {**schema, "maxItems": value})
        with self.assertRaises(ValueError):
            ResponseContribution("references", {"type": "string", "maxItems": 1})

    def test_plugin_configuration_is_declared_typed_and_path_guarded(self):
        from anima.bootstrap.settings import _plugin_configuration, Settings, ConfigurationError, _command_prefix
        from anima.capabilities.configuration import ConfigField
        fields = (
            ConfigField("flag", "TEST_FLAG", "flag", "flag", "test", "bool", False),
            ConfigField("count", "TEST_COUNT", "count", "count", "test", "int", 1),
            ConfigField("ratio", "TEST_RATIO", "ratio", "ratio", "test", "float", 0.5),
            ConfigField("names", "TEST_NAMES", "names", "names", "test", "list", ()),
            ConfigField("text", "TEST_TEXT", "text", "text", "test", "text", "default"),
        )
        manifest = PluginManifest("test", "1", configuration=fields)
        with TemporaryDirectory() as directory, patch("anima.bootstrap.settings.PluginLoader") as loader:
            root = Path(directory)
            loader.return_value.load.return_value.manifests = (manifest,)
            path = root / "plugins/test.json"
            path.parent.mkdir()
            path.write_text('{"text":"file"}')
            values = _plugin_configuration(root, {"TEST_FLAG": "true", "TEST_COUNT": "2", "TEST_RATIO": "0.75", "TEST_NAMES": "a,b", "TEST_TEXT": "env"})
            self.assertEqual(values["test"], {"flag": True, "count": 2, "ratio": .75, "names": ["a", "b"], "text": "env"})
            with self.assertRaises(ConfigurationError):
                _plugin_configuration(root, {"TEST_COUNT": "bad"})
            for text in ("[]", "bad", '{"unknown":1}'):
                path.write_text(text)
                with self.assertRaises(ConfigurationError):
                    _plugin_configuration(root, {})
            path.unlink()
            path.mkdir()
            with self.assertRaises(ConfigurationError):
                _plugin_configuration(root, {})
            path.rmdir()
            path.symlink_to(root / "missing")
            with self.assertRaises(ConfigurationError):
                _plugin_configuration(root, {})
            fake = SimpleNamespace(plugins=frozenset({"test"}), plugin_configuration=values)
            self.assertEqual(Settings.__getattr__(fake, "text"), "env")
            self.assertTrue(Settings.__getattr__(fake, "test_enabled"))
            with self.assertRaises(AttributeError):
                Settings.__getattr__(fake, "missing")
        with self.assertRaises(ConfigurationError):
            _command_prefix("INVALID")

    def test_common_layers_do_not_import_concrete_plugins_or_persona(self):
        root = Path(__file__).parents[1] / "src/anima"
        for group in ("core", "capabilities", "adapters", "bootstrap"):
            for path in (root / group).rglob("*.py"):
                if "__pycache__" in path.parts:
                    continue
                text = path.read_text(encoding="utf-8")
                for node in ast.walk(ast.parse(text)):
                    if isinstance(node, ast.ImportFrom):
                        self.assertFalse((node.module or "").startswith("anima.plugins."), str(path))
                    elif isinstance(node, ast.Import):
                        self.assertFalse(any(item.name.startswith("anima.plugins.") for item in node.names), str(path))

    def test_public_deployment_profile_and_unsafe_inputs(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "deployment.json"
            self.assertEqual(deployment_defaults(root), {})
            valid = {"ANIMA_ROOT": "custom", "AIVIS_URL": "local", "OPENAI_MODEL": "model"}
            path.write_text(json.dumps({"environment": valid}))
            self.assertEqual(deployment_defaults(root), valid)
            for value in ([], {}, {"environment": []}, {"environment": {"DISCORD_BOT_TOKEN": "secret"}},
                          {"environment": {"OPENAI_API_KEY": "secret"}}, {"environment": {"ANIMA_SECRET": "secret"}},
                          {"environment": {"ANIMA_ROOT": 1}}, {"environment": {}, "extra": True}):
                path.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    deployment_defaults(root)
            path.write_text("not json")
            with self.assertRaises(ValueError):
                deployment_defaults(root)
            path.unlink()
            path.mkdir()
            with self.assertRaises(ValueError):
                deployment_defaults(root)
            path.rmdir()
            path.symlink_to(root / "missing")
            with self.assertRaises(ValueError):
                deployment_defaults(root)

    def test_sandbox_assembly_bindings(self):
        assembly = PluginAssembly(None, None, Path("assets"), None, None, lambda: None,
                                  "model", None, lambda: None, lambda: None)
        self.assertIsNone(assembly.get("missing"))
        self.assertEqual(assembly.get("missing", 42), 42)
        assembly.publish("extension", 3)
        self.assertEqual(assembly.get("extension"), 3)
        for name in ("", "extension"):
            with self.assertRaises(ValueError):
                assembly.publish(name, 4)

    def test_activation_and_ordering_declarations_are_validated(self):
        self.assertTrue(PluginManifest("test", "1", after=("optional",), uses_audio=True,
                                       default_enabled=True, enabled_env="TEST_ENABLED").uses_audio)
        for values in ({"after": ("../invalid",)}, {"default_enabled": 1},
                       {"enabled_env": "bad-env"}, {"uses_audio": 1}):
            with self.assertRaises(ValueError):
                PluginManifest("test", "1", **values)

    def test_observation_callbacks_are_once_per_provider_and_enrichment_is_typed(self):
        from unittest.mock import Mock
        context = SimpleNamespace(source=None)
        provider = SimpleNamespace(observe_tool_result=Mock())
        plain = object()
        result = ToolResult("success", {"ok": True})
        prepared = PreparedResponse((), {"one": provider, "two": provider, "other": plain}, context)
        prepared.observe_tool_result(result)
        provider.observe_tool_result.assert_called_once_with(result, context)
        registry = ResponseContributionRegistry()
        draft = ResponseDraft("hello", "平穏", "test", "弱い", "test")
        registry.providers = (plain, SimpleNamespace(enrich_draft=lambda value, context: value))
        self.assertIs(registry.enrich_draft(draft, context), draft)
        registry.providers = (SimpleNamespace(enrich_draft=lambda value, context: None),)
        with self.assertRaises(TypeError):
            registry.enrich_draft(draft, context)

    def test_generic_launcher_calls_only_enabled_plugin_hooks(self):
        from anima.bootstrap.runner import run_local
        from unittest.mock import Mock
        main = Mock()
        launcher = Mock(side_effect=lambda downstream, **kwargs: downstream())
        definition = SimpleNamespace(manifest=PluginManifest("test", "1"), run_application=launcher)
        passive = SimpleNamespace(manifest=PluginManifest("other", "1"))
        with TemporaryDirectory() as directory, patch("anima.bootstrap.runner.PluginLoader") as loader, \
                patch("anima.bootstrap.runner.selected_plugins", return_value=frozenset({"test", "other"})):
            loader.return_value.load.return_value.definitions = (definition, passive)
            run_local(cwd=Path(directory), environ={}, app_main=main)
            main.assert_called_once()
            launcher.assert_called_once()
        with TemporaryDirectory() as directory, patch("anima.bootstrap.runner.PluginLoader") as loader, \
                patch("anima.bootstrap.runner.selected_plugins", return_value=frozenset()), \
                patch("anima.bootstrap.app.main") as default:
            loader.return_value.load.return_value.definitions = (definition,)
            run_local(cwd=Path(directory), environ={})
            default.assert_called_once()
        self.assertEqual(launcher.call_count, 1)


class ResourceProviderDefaultsTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_undeclared_operations_are_denied(self):
        from anima.core.resource_provider import ResourceProviderBase
        provider = ResourceProviderBase()
        operations = (
            provider.list_resources(None, cursor=None, limit=1, context=None),
            provider.search_resources(None, query="x", limit=1, context=None),
            provider.read_resource(None, "id", None),
            provider.write_resource(None, "id", "create", "text", None),
            provider.delete_resource(None, "id", None),
            provider.export_resource(None, "id", None),
            provider.import_resource(None, "id", None, None),
        )
        for operation in operations:
            with self.assertRaises(PermissionError):
                await operation
