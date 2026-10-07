import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from anima.core.modes import CallableModeProvider, ModeRegistry, ModeState


NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


class ModeStateTests(unittest.TestCase):
    def test_validation_and_serialization(self):
        mode = ModeState("music", "playback", "音楽をかけている", "曲A", NOW.isoformat())
        self.assertEqual(mode.key, "music:playback")
        self.assertEqual(mode.to_dict()["detail"], "曲A")
        for value in (
            {"plugin": "Bad", "id": "x", "label": "x"},
            {"plugin": "ok", "id": "bad/id", "label": "x"},
            {"plugin": "ok", "id": "x", "label": ""},
            {"plugin": "ok", "id": "x", "label": "x\ny"},
            {"plugin": "ok", "id": "x", "label": "x", "detail": "x\ny"},
            {"plugin": "ok", "id": "x", "label": "x", "started_at": "2026-01-01"},
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ModeState(**value)


class CallableModeProviderTests(unittest.TestCase):
    def test_validates_callback_values(self):
        mode = ModeState("music", "playback", "再生中")
        self.assertEqual(CallableModeProvider(lambda: [mode]).modes(), (mode,))
        with self.assertRaises(TypeError):
            CallableModeProvider(lambda: [object()]).modes()


class ModeRegistryTests(unittest.TestCase):
    def test_records_started_changed_continued_and_ended(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "modes.json"
            values = [ModeState("drawing", "job.a", "絵を描いている", "queued")]
            provider = CallableModeProvider(lambda: values)
            registry = ModeRegistry(path, [provider], history_limit=3, clock=lambda: NOW)
            self.assertIn("絵を描いている", registry.summary())
            registry.refresh()  # An unchanged continuation is not noisy.
            values[0] = ModeState("drawing", "job.a", "絵を描いている", "running")
            registry.refresh()
            values.clear()
            self.assertEqual(registry.summary(), "いま外から確認できる継続中の活動はない。")
            data = json.loads(path.read_text())
            self.assertEqual(
                [item["transition"] for item in data["history"]],
                ["started", "continued", "ended"],
            )
            loaded = ModeRegistry(path, [], history_limit=3, clock=lambda: NOW)
            self.assertEqual(loaded.snapshot()["active"], ())
            self.assertEqual(len(loaded.snapshot()["history"]), 3)

    def test_rejects_bad_configuration_and_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "modes.json"
            with self.assertRaises(ValueError):
                ModeRegistry(path, history_limit=0)
            with self.assertRaises(TypeError):
                ModeRegistry(path, [object()])
            registry = ModeRegistry(path)
            with self.assertRaises(TypeError):
                registry.replace_providers([object()])
            mode = ModeState("voice", "connected", "VCにいる")
            registry.replace_providers([
                CallableModeProvider(lambda: (mode,)),
                CallableModeProvider(lambda: (mode,)),
            ])
            with self.assertRaisesRegex(ValueError, "duplicate mode"):
                registry.refresh()

    def test_corrupt_file_is_ignored_and_history_is_trimmed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "modes.json"
            path.write_text("not json")
            values = [ModeState("music", "playback", "再生中", "A")]
            registry = ModeRegistry(
                path, [CallableModeProvider(lambda: values)],
                history_limit=1, clock=lambda: NOW,
            )
            registry.refresh()
            values.clear()
            registry.refresh()
            data = json.loads(path.read_text())
            self.assertEqual(len(data["history"]), 1)
            self.assertEqual(data["history"][0]["transition"], "ended")


if __name__ == "__main__":
    unittest.main()
