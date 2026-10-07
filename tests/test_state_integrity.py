import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from anima.core.state import FileStateStore
from test_operations import NOW, old_event


class IntegrityTests(unittest.TestCase):
    def test_transaction_checksum_is_verified_before_any_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = FileStateStore(root)
            store.ensure_layout(now=NOW)
            with patch.object(store, "_clear_transaction"):
                store._commit_transaction(files={"open.md": "original"})
            path = root / ".anima-transaction.json"
            manifest = json.loads(path.read_text())
            self.assertEqual(manifest["version"], 2)
            manifest["files"]["open.md"] = "corrupted"
            path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "checksum"):
                store._recover_transaction()
            self.assertEqual((root / "open.md").read_text(), "original")
            self.assertTrue(path.exists())
            manifest["files"]["open.md"] = "original"
            path.write_text(json.dumps(manifest))
            store._recover_transaction()
            self.assertFalse(path.exists())

    def test_failed_gzip_verification_preserves_source_and_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = FileStateStore(root)
            store.ensure_layout(now=NOW)
            event = old_event("m1")
            store.append_received(event)
            store.mark_handled(event)
            cursor = json.loads((root / "cursor.json").read_text())
            cursor.update(digested_until=NOW.isoformat(), slept_at=NOW.isoformat())
            (root / "cursor.json").write_text(json.dumps(cursor))
            with patch.object(store, "_stream_hash", side_effect=["bad", "good"]):
                with self.assertRaisesRegex(ValueError, "checksum"):
                    store.archive_old_logs(now=NOW)
            self.assertTrue((root / "log/general" / f"{event.ts.date()}.jsonl").exists())
            self.assertTrue((root / "handled/general" / f"{event.ts.date()}.txt").exists())
            self.assertEqual(list((root / "archive/log/general").iterdir()), [])
