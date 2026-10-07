import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from anima.capabilities.plugin_loader import PluginLoader


class ExternalPluginLoaderTests(unittest.TestCase):
    def test_environment_selects_external_package(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "external_bot"
            plugin = root / "plugins" / "sample"
            plugin.mkdir(parents=True)
            for package in (root, root / "plugins", plugin):
                (package / "__init__.py").write_text("")
            (plugin / "plugin.py").write_text(
                "from types import SimpleNamespace\n"
                "from anima.capabilities.plugins import PluginManifest\n"
                "PLUGIN = SimpleNamespace(manifest=PluginManifest('sample', '1'))\n"
            )
            with patch.dict(os.environ, {"ANIMA_PLUGIN_NAMESPACE": "external_bot.plugins"}), patch.object(sys, "path", [directory, *sys.path]):
                try:
                    catalog = PluginLoader().load()
                    self.assertEqual([m.name for m in catalog.manifests], ["sample"])
                finally:
                    for name in tuple(sys.modules):
                        if name == "external_bot" or name.startswith("external_bot."):
                            sys.modules.pop(name)

    def test_explicit_namespace_overrides_environment(self):
        with patch.dict(os.environ, {"ANIMA_PLUGIN_NAMESPACE": "invalid-name"}):
            self.assertEqual(PluginLoader("anima.plugins").namespace, "anima.plugins")

    def test_invalid_environment_fails_closed(self):
        for namespace in ("", "bad-name", "a..b"):
            with self.subTest(namespace=namespace), patch.dict(os.environ, {"ANIMA_PLUGIN_NAMESPACE": namespace}), self.assertRaises(ValueError):
                PluginLoader()

    def test_default_namespace(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(PluginLoader().namespace, "anima.plugins")
