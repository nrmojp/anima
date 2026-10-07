from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from anima.core.jobs import PluginJobManager


class PluginJobManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_filter_persist_and_trim(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "jobs.json"
            manager = PluginJobManager(path, history_limit=1)
            await manager.start()
            notified = []

            async def run(): return {"artifact_id": "one.png"}
            async def notify(record): notified.append(record.state)

            first = manager.submit("drawing", "image", "one", run, notify)
            self.assertEqual(manager.active_count(plugin="drawing", kind="image"), 1)
            await asyncio.sleep(0.01)
            self.assertEqual(manager.get(first.id).state, "completed")
            self.assertEqual(notified, ["completed"])
            second = manager.submit("drawing", "image", "two", run)
            await asyncio.sleep(0.01)
            self.assertIsNone(manager.get(first.id))
            self.assertEqual(manager.list(plugin="drawing")[0].id, second.id)
            await manager.stop()
            loaded = PluginJobManager(path)
            self.assertEqual(loaded.get(second.id).result["artifact_id"], "one.png")

    async def test_failure_cancel_restart_and_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "jobs.json"
            with self.assertRaises(ValueError):
                PluginJobManager(path, history_limit=0)
            with self.assertRaises(ValueError):
                PluginJobManager(path, max_active=0)
            manager = PluginJobManager(path)
            with self.assertRaises(ValueError):
                manager.submit("", "kind", "bad", lambda: None)

            failed_notifications = []
            async def fail(): raise RuntimeError("secret")
            async def bad_notify(record):
                failed_notifications.append(record.state)
                raise RuntimeError("notification")
            failed = manager.submit("drawing", "image", "bad", fail, bad_notify)
            await asyncio.sleep(0.01)
            self.assertEqual(manager.get(failed.id).state, "failed")
            self.assertEqual(manager.get(failed.id).error, "RuntimeError")
            self.assertEqual(failed_notifications, ["failed"])

            blocker = asyncio.Event()
            async def wait():
                await blocker.wait()
                return {}
            pending = manager.submit("drawing", "image", "wait", wait)
            await asyncio.sleep(0)
            mode = manager.modes()[0]
            self.assertEqual(mode.plugin, "drawing")
            self.assertEqual(mode.label, "wait")
            self.assertIn(pending.id, mode.id)
            with self.assertRaisesRegex(RuntimeError, "capacity"):
                manager.submit("drawing", "image", "second", wait)
            self.assertTrue(await manager.cancel(pending.id))
            self.assertEqual(manager.get(pending.id).state, "cancelled")
            self.assertEqual(manager.modes(), ())
            self.assertFalse(await manager.cancel(pending.id))
            with self.assertRaisesRegex(ValueError, "job ID"):
                manager.submit("x", "y", "bad id", wait, job_id="invalid")

            path.write_text(json.dumps({"jobs": [{
                "id": "j-old", "plugin": "x", "kind": "y", "state": "running",
                "created_at": "2026-01-01", "updated_at": "2026-01-01",
            }]}), encoding="utf-8")
            restarted = PluginJobManager(path)
            self.assertEqual(restarted.get("j-old").state, "failed")
            self.assertEqual(restarted.get("j-old").error, "interrupted_by_restart")
            path.write_text("bad", encoding="utf-8")
            self.assertEqual(PluginJobManager(path).list(), ())

            active = manager.submit("drawing", "image", "shutdown", wait)
            await asyncio.sleep(0)
            await manager.stop()
            self.assertEqual(manager.get(active.id).state, "cancelled")
            with self.assertRaises(RuntimeError):
                manager.submit("x", "y", "stopped", wait)

    async def test_successful_job_is_not_failed_by_notification(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = PluginJobManager(Path(temporary) / "jobs.json")
            async def run(): return {}
            async def notify(_record): raise RuntimeError("delivery")
            record = manager.submit("drawing", "image", "ok", run, notify)
            await asyncio.sleep(0.01)
            self.assertEqual(manager.get(record.id).state, "completed")
