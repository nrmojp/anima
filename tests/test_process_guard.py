from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest

from anima.bootstrap.process_guard import AlreadyRunningError, ProcessLock, mark_previous_unclean_shutdown


NOW = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)


class ProcessLockTests(unittest.TestCase):
    def test_lock_is_idempotent_exclusive_and_reusable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime" / "bot.lock"
            first = ProcessLock(path)
            second = ProcessLock(path)
            first.acquire()
            first.acquire()
            self.assertEqual(path.read_text(), str(os.getpid()))
            with self.assertRaisesRegex(AlreadyRunningError, str(os.getpid())):
                second.acquire()
            first.release()
            first.release()
            with second:
                self.assertEqual(path.read_text(), str(os.getpid()))
            self.assertIsNone(second._stream)

    def test_unclean_shutdown_detection_updates_only_stale_connected_pid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            self.assertFalse(mark_previous_unclean_shutdown(
                path, now=NOW, pid_exists=lambda _: False
            ))
            path.write_text("[]")
            self.assertFalse(mark_previous_unclean_shutdown(path, now=NOW, pid_exists=lambda _:False))
            path.write_text("invalid")
            self.assertFalse(mark_previous_unclean_shutdown(
                path, now=NOW, pid_exists=lambda _: False
            ))
            for value, exists in (({"connected": False, "pid": 1}, False),
                                  ({"connected": True, "pid": "1"}, False),
                                  ({"connected": True, "pid": 1}, True)):
                path.write_text(json.dumps(value))
                self.assertFalse(mark_previous_unclean_shutdown(
                    path, now=NOW, pid_exists=lambda _: exists
                ))
            path.write_text(json.dumps({"connected": True, "pid": 42, "user": "anima"}))
            self.assertTrue(mark_previous_unclean_shutdown(
                path, now=NOW, pid_exists=lambda _: False
            ))
            result = json.loads(path.read_text())
            self.assertFalse(result["connected"])
            self.assertEqual(result["last_unclean_shutdown_pid"], 42)
            self.assertEqual(result["last_unclean_shutdown_at"], NOW.isoformat())
            self.assertEqual(result["user"], "anima")


if __name__ == "__main__":
    unittest.main()
