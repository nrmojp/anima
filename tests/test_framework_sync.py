import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("framework_sync", Path(__file__).parents[1] / "scripts/check_framework_sync.py")
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


class FrameworkSyncTests(unittest.TestCase):
    def create(self, root):
        package = root / "src/anima"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("package")
        (package / "py.typed").touch()
        (package / "core").mkdir()
        (package / "core/test.py").write_text("code")
        (package / "core/ignored.txt").write_text("ignored")
        (root / "framework-lock.json").write_text(json.dumps(sync.fingerprint(sync.inventory(root))))

    def test_matching_peer_changed_missing_added_and_unsafe_files(self):
        with TemporaryDirectory() as first, TemporaryDirectory() as second:
            root, peer = Path(first), Path(second)
            self.create(root)
            self.create(peer)
            self.assertEqual(sync.verify(root, peer), [])
            changed = peer / "src/anima/core/test.py"
            changed.write_text("changed")
            self.assertEqual(sync.verify(root, peer), ["src/anima/core/test.py"])
            changed.unlink()
            self.assertEqual(sync.verify(root, peer), ["src/anima/core/test.py"])
            (peer / "src/anima/core/extra.py").write_text("extra")
            self.assertEqual(len(sync.verify(root, peer)), 2)
            changed.symlink_to(root / "src/anima/core/test.py")
            with self.assertRaises(ValueError):
                sync.inventory(peer)
            (root / "framework-lock.json").write_text("{}")
            self.assertEqual(len(sync.verify(root)), 1)
            (root / "src/anima/py.typed").unlink()
            with self.assertRaises(ValueError):
                sync.inventory(root)

    def test_cli_success_fingerprint_mismatch_and_invalid_lock(self):
        with TemporaryDirectory() as directory, patch("builtins.print"):
            root = Path(directory)
            self.create(root)
            self.assertEqual(sync.main(["--root", directory]), 0)
            self.assertEqual(sync.main(["--root", directory, "--fingerprint"]), 0)
            lock = root / "framework-lock.json"
            lock.write_text("{}")
            self.assertEqual(sync.main(["--root", directory]), 1)
            lock.write_text("invalid")
            self.assertEqual(sync.main(["--root", directory]), 1)
