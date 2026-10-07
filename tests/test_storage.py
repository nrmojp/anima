from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from anima.adapters.storage import FilePluginStorage, FilePluginStorageFactory
from anima.core.sandbox import SandboxKey
from anima.core.services import SandboxServices
from anima.core.storage import PluginStorage, PluginStorageFactory


class FilePluginStorageTests(unittest.TestCase):
    def test_json_artifact_and_temporary_routes_are_plugin_scoped(self):
        with TemporaryDirectory() as directory:
            factory = FilePluginStorageFactory(Path(directory), SandboxKey("discord_guild", "123"))
            storage = factory.for_plugin("music")
            self.assertIsInstance(storage, PluginStorage)
            marker = object()
            self.assertIs(storage.read_json("queue.json", marker), marker)
            storage.write_json("queue.json", {"tracks": ["one"]})
            self.assertEqual(storage.read_json("queue.json", {}), {"tracks": ["one"]})
            artifact = storage.artifact_path("covers", "one.png")
            temporary = storage.temporary_path("decode.pcm")
            self.assertIn("/sandboxes/guilds/123/world/music/covers/", str(artifact))
            self.assertIn("/runtime/plugins/music/tmp/", str(temporary))
            temporary.write_bytes(b"pcm")
            storage.clear_temporary()
            self.assertFalse(temporary.exists())
            storage.clear_temporary()

    def test_rejects_unsafe_names_and_symbolic_links(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            storage = FilePluginStorage(root / "state", root / "world")
            for operation in (
                lambda: storage.read_json("../secret", {}),
                lambda: storage.write_json("/secret", {}),
                lambda: storage.artifact_path("..", "x"),
                lambda: storage.artifact_path("ok", "a/b"),
                lambda: storage.temporary_path("../x"),
            ):
                with self.subTest(operation=operation), self.assertRaises(ValueError):
                    operation()
            storage.state_directory.mkdir(parents=True)
            (storage.state_directory / "linked.json").symlink_to(root / "target")
            with self.assertRaisesRegex(ValueError, "symbolic"):
                storage.read_json("linked.json", {})
            (root / "target").write_text("x")
            (storage.artifact_directory / "covers").mkdir(parents=True)
            (storage.artifact_directory / "covers" / "linked.png").symlink_to(root / "target")
            with self.assertRaisesRegex(ValueError, "artifact path"):
                storage.artifact_path("covers", "linked.png")
            storage.temporary_directory.mkdir(parents=True)
            (storage.temporary_directory / "linked.pcm").symlink_to(root / "target")
            with self.assertRaisesRegex(ValueError, "temporary path"):
                storage.temporary_path("linked.pcm")
            storage.clear_temporary()
            storage.temporary_directory.parent.mkdir(parents=True, exist_ok=True)
            storage.temporary_directory.symlink_to(root / "target", target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symbolic"):
                storage.clear_temporary()

    def test_factory_and_service_validation(self):
        with TemporaryDirectory() as directory:
            key = SandboxKey("test", "one")
            factory = FilePluginStorageFactory(Path(directory), key)
            self.assertIsInstance(factory, PluginStorageFactory)
            with self.assertRaisesRegex(ValueError, "plugin ID"):
                factory.for_plugin("Bad")
            scoped = SandboxServices(
                key, {"plugin_storage_factory": factory, "shared": "value"}
            ).for_plugin("echo")
            self.assertIsInstance(scoped.require("plugin_storage", PluginStorage), PluginStorage)
            self.assertEqual(scoped.require("shared", str), "value")
            self.assertNotIn("plugin_storage_factory", scoped.values)
            with self.assertRaisesRegex(TypeError, "plugin_storage_factory"):
                SandboxServices(key, {"plugin_storage_factory": object()}).for_plugin("echo")


if __name__ == "__main__":
    unittest.main()
