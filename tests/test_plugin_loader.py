from types import SimpleNamespace
import unittest
from unittest.mock import patch

from anima.capabilities.plugin_loader import PluginLoader
from anima.capabilities.plugins import PluginManifest


class Definition:
    def __init__(self, name):
        self.manifest = PluginManifest(name, "1")


class Candidate:
    def __init__(self, name, ispkg=True):
        self.name = name
        self.ispkg = ispkg


class PluginLoaderTests(unittest.TestCase):
    def test_loads_in_tree_plugins_and_skips_non_entry_packages(self):
        catalog = PluginLoader().load()
        self.assertEqual([manifest.name for manifest in catalog.manifests], ["echo", "voice", "web_search"])
        self.assertEqual(set(catalog.skill_roots), {"echo", "voice", "web_search"})
        from anima.capabilities.plugins import PluginCatalog
        with self.assertRaisesRegex(ValueError, "unknown skill provider"):
            PluginCatalog((), skill_roots={"unknown": None})

    def test_invalid_namespace(self):
        for namespace in ("", "bad-name", "anima..plugins"):
            with self.subTest(namespace=namespace), self.assertRaises(ValueError):
                PluginLoader(namespace)

    @patch("anima.capabilities.plugin_loader.import_module")
    def test_namespace_must_be_package(self, import_module):
        import_module.return_value = object()
        with self.assertRaisesRegex(ValueError, "must be a package"):
            PluginLoader("example.plugins").load()

    @patch("anima.capabilities.plugin_loader.find_spec")
    @patch("anima.capabilities.plugin_loader.pkgutil.iter_modules")
    @patch("anima.capabilities.plugin_loader.import_module")
    def test_skips_modules_and_packages_without_entry_point(
        self, import_module, iter_modules, find_spec
    ):
        import_module.return_value = SimpleNamespace(__path__=("plugins",))
        iter_modules.return_value = (Candidate("module", False), Candidate("config"))
        find_spec.return_value = None
        self.assertEqual(PluginLoader("example.plugins").load().manifests, ())
        find_spec.assert_called_once_with("example.plugins.config.plugin")

    @patch("anima.capabilities.plugin_loader.find_spec", return_value=object())
    @patch("anima.capabilities.plugin_loader.pkgutil.iter_modules")
    @patch("anima.capabilities.plugin_loader.import_module")
    def test_rejects_missing_entry_point(self, import_module, iter_modules, _find_spec):
        import_module.side_effect = (
            SimpleNamespace(__path__=("plugins",)), SimpleNamespace(),
        )
        iter_modules.return_value = (Candidate("broken"),)
        with self.assertRaisesRegex(ValueError, "has no PLUGIN"):
            PluginLoader("example.plugins").load()

    @patch("anima.capabilities.plugin_loader.find_spec", return_value=object())
    @patch("anima.capabilities.plugin_loader.pkgutil.iter_modules")
    @patch("anima.capabilities.plugin_loader.import_module")
    def test_rejects_directory_manifest_mismatch(
        self, import_module, iter_modules, _find_spec
    ):
        import_module.side_effect = (
            SimpleNamespace(__path__=("plugins",)),
            SimpleNamespace(PLUGIN=Definition("different")),
        )
        iter_modules.return_value = (Candidate("expected"),)
        with self.assertRaisesRegex(ValueError, "manifest name differ"):
            PluginLoader("example.plugins").load()


if __name__ == "__main__":
    unittest.main()
