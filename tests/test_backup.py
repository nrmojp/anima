import json
import os
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from anima.adapters.storage.backup import backup, restore
from anima.core.sandbox import SandboxKey


class BackupTests(unittest.TestCase):
    def test_round_trip_and_guards(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "state"
            key = SandboxKey("guild", "1")
            source = key.path(root)
            source.mkdir(parents=True)
            (source / "memory").mkdir()
            (source / "memory/self.md").write_text("思い出")
            output = base / "snapshot.zip"
            self.assertEqual(backup(root, key, output)["files"], 1)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                backup(root, key, output)
            with self.assertRaises(ValueError):
                backup(root, key, root / "bad.zip")
            with self.assertRaises(ValueError):
                backup(root, SandboxKey("guild", "2"), output)
            with self.assertRaises(FileExistsError):
                restore(root, key, output)
            restored = base / "restored"
            self.assertEqual(restore(restored, key, output)["files"], 1)
            self.assertEqual((key.path(restored) / "memory/self.md").read_text(), "思い出")
            (source / "link").symlink_to(output)
            with self.assertRaises(ValueError):
                backup(root, key, base / "link.zip")
            (source / "link").unlink()
            os.mkfifo(source / "pipe")
            with self.assertRaises(ValueError):
                backup(root, key, base / "pipe.zip")

    def test_cli(self):
        from anima.bootstrap.cli import main
        from anima.bootstrap.process_guard import AlreadyRunningError

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "state"
            key = SandboxKey("guild", "1")
            key.path(root).mkdir(parents=True)
            output = base / "snapshot.zip"
            with patch("anima.bootstrap.cli._environment", return_value=({}, base, root)), patch("anima.bootstrap.cli._print") as display:
                main(["backup", str(output), "--sandbox", str(key)])
                self.assertEqual(display.call_args.args[0]["files"], 0)
                with self.assertRaises(SystemExit):
                    main(["restore", str(output), "--sandbox", str(key)])
                with patch("anima.bootstrap.process_guard.ProcessLock.__enter__", side_effect=AlreadyRunningError("running")):
                    with self.assertRaises(SystemExit):
                        main(["backup", str(output), "--sandbox", str(key)])
            with patch("anima.bootstrap.cli._environment", return_value=({}, base, base / "restored")), patch("anima.bootstrap.cli._print"):
                main(["restore", str(output), "--sandbox", str(key)])

    def test_invalid_archives_never_publish(self):
        cases = [
            ({"version": 2, "sandbox": "guild:1", "files": {}}, {}),
            ({"version": 1, "sandbox": "guild:2", "files": {}}, {}),
            ({"version": 1, "sandbox": "guild:1", "files": []}, {}),
            ({"version": 1, "sandbox": "guild:1", "files": {}}, {"extra": b"x"}),
            *[({"version": 1, "sandbox": "guild:1", "files": {name: "bad"}},
               {"data/" + name: b"x"}) for name in ("../escape", "/absolute", "a\\b", "a//b", "", "normal")],
        ]
        for manifest, entries in cases:
            with self.subTest(manifest=manifest), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                archive = base / "bad.zip"
                with zipfile.ZipFile(archive, "w") as stream:
                    stream.writestr("manifest.json", json.dumps(manifest))
                    for name, data in entries.items():
                        stream.writestr(name, data)
                root = base / "state"
                key = SandboxKey("guild", "1")
                with self.assertRaises(ValueError):
                    restore(root, key, archive)
                self.assertFalse(key.path(root).exists())
