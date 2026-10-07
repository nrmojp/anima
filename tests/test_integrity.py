import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from anima.core.integrity import recover_jsonl_tail, validate_state, require_space
from anima.core.state import FileStateStore, INITIAL_MOOD, INITIAL_CURSOR
from test_operations import NOW


class IntegrityRecoveryTests(unittest.TestCase):
    def test_state_validation_preserves_invalid_original(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            validate_state(root / "absent", root)
            cases = [("cursor.json", "[]"), ("cursor.json", "{"),
                     ("cursor.json", json.dumps({"version": -1})),
                     ("cursor.json", json.dumps({"version": 0, "slept_at": "bad"})),
                     ("cursor.json", json.dumps({"version": 0, "slept_at": 1})),
                     ("mood.md", "{}"),
                     ("mood.md", json.dumps({**INITIAL_MOOD, "strength": "bad"})),
                     ("mood.md", json.dumps({**INITIAL_MOOD, "since": "bad"}))]
            for name, content in cases:
                path = root / name
                path.write_text(content)
                with self.assertRaisesRegex(ValueError, "preserved"):
                    validate_state(path, root)
                self.assertEqual(path.read_text(), content)
            self.assertEqual(len(list((root / "runtime/quarantine").iterdir())), len(cases))
            for name, value in (("cursor.json", INITIAL_CURSOR), ("mood.md", INITIAL_MOOD)):
                path = root / name
                path.write_text(json.dumps(value))
                validate_state(path, root)

    def test_tail_recovery_and_middle_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "log.jsonl"
            for data, expected in ((b"", b""), (b'{}\n', b'{}\n'), (b'{}', b'{}\n'),
                                   (b'{}\n{', b'{}\n'), (b'{', b''),
                                   (b'{}\n\n\xff', b'{}\n\n')):
                path.write_bytes(data)
                recover_jsonl_tail(path, root)
                self.assertEqual(path.read_bytes(), expected)
            path.write_bytes(b'bad\n{')
            with self.assertRaises(ValueError):
                recover_jsonl_tail(path, root)
            self.assertEqual(path.read_bytes(), b'bad\n{')

    def test_low_space_and_store_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("anima.core.integrity.shutil.disk_usage", return_value=SimpleNamespace(free=0)):
                with self.assertRaises(OSError):
                    require_space(root)
                with self.assertRaises(OSError):
                    FileStateStore(root)._atomic_write_text(root / "test", "hello")
            store = FileStateStore(root)
            store.ensure_layout(now=NOW)
            log = root / "log/channel/test.jsonl"
            log.parent.mkdir()
            log.write_bytes(b'{')
            store.ensure_layout(now=NOW)
            self.assertEqual(log.read_bytes(), b'')
            (root / "cursor.json").write_text("{")
            with self.assertRaisesRegex(ValueError, "repair or restore"):
                store.ensure_layout(now=NOW)
