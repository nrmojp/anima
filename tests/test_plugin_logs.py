import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from anima.core.plugin_logs import LOGGER, PluginLogHandler, plugin_log, read_plugin_logs, sanitize


class PluginLogTests(unittest.TestCase):
    def test_sanitize(self):
        value = sanitize({"token": "private", "nested": [{"prompt": "cat sk-secret Bearer abc data:image/png;base64,xxx"}], "count": 1})
        self.assertEqual(value["token"], "[redacted]")
        self.assertNotIn("sk-secret", str(value))
        self.assertEqual(value["count"], 1)
        self.assertEqual(len(sanitize("a" * 17000)), 16000)

    def test_write_read_retention_and_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            handler = PluginLogHandler(root, 7)
            logs = root / "sandboxes/guilds/1/runtime/plugins/drawing"
            logs.mkdir(parents=True)
            (logs / "2000-01-01.jsonl").write_text("{}\n")
            (logs / "notes.jsonl").write_text("invalid\n[]\n")
            record = logging.LogRecord("test", 20, "", 0, json.dumps({"sandbox_key": "guild:1", "plugin": "drawing", "ts": "2026", "event": "request", "prompt": "hand drawn"}), (), None)
            handler.emit(record)
            routed = PluginLogHandler(root, 7, ("voice",))
            record.name = "anima.telemetry"
            for event in ("voice.completed", "persona.started"):
                record.msg = json.dumps({"event": event, "sandbox_key": "guild:1"})
                routed.emit(record)
            record.msg = json.dumps({"event": "voice.started"})
            routed.emit(record)
            self.assertEqual(read_plugin_logs(root / "sandboxes/guilds/1")[1]["plugin"], "voice")
            record.name = "test"
            self.assertFalse((logs / "2000-01-01.jsonl").exists())
            self.assertTrue((logs / "notes.jsonl").exists())
            self.assertEqual(read_plugin_logs(root / "sandboxes/guilds/1")[0]["prompt"], "hand drawn")
            self.assertEqual(read_plugin_logs(root / "sandboxes/guilds/2"), [])
            path = next(p for p in logs.glob("*.jsonl") if p.name != "notes.jsonl")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            for payload in ("bad", json.dumps({"plugin": "../bad", "sandbox_key": "guild:1"}), "{}"):
                record.msg = payload
                with self.assertLogs("anima.core.plugin_logs", level="ERROR"):
                    handler.emit(record)
            path.unlink()
            path.symlink_to(logs / "notes.jsonl")
            with self.assertRaises(ValueError):
                read_plugin_logs(root / "sandboxes/guilds/1")
            record.msg = json.dumps({"plugin": "drawing", "sandbox_key": "guild:1"})
            with self.assertLogs("anima.core.plugin_logs", level="ERROR"):
                handler.emit(record)

    def test_interface_validation_and_reference(self):
        with self.assertRaises(ValueError):
            PluginLogHandler(Path("unused"), 0)
        with self.assertRaises(ValueError):
            plugin_log("../unsafe", "event")
        with patch("anima.core.plugin_logs.sandbox_context") as scope:
            scope.get.return_value = None
            with patch.object(LOGGER, "info") as info:
                plugin_log("drawing", "event")
                info.assert_not_called()
                plugin_log("drawing", "request", sandbox_key="guild:1", api_key="private")
                record = json.loads(info.call_args.args[0])
                self.assertEqual(record["api_key"], "[redacted]")
                self.assertEqual(record["sandbox_key"], "guild:1")
