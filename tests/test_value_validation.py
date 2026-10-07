import unittest
from dataclasses import replace
from datetime import datetime, timezone
from anima.core.models import Mood, MemoryDocument
from anima.core.sandbox import SandboxKey
from test_sandbox import event


class ValueValidationTests(unittest.TestCase):
    def test_mood_boundaries_and_invalid_values(self):
        base = Mood("x" * 40, "y" * 40, "ふつう", "z" * 60, datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(Mood.from_dict(base.to_dict()), base)
        for changes in ({"since": datetime(2026, 1, 1)}, {"strength": "unknown"}, {"state": "x" * 41}, {"cause": "y" * 41}, {"focus": "z" * 61}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(base, **changes)

    def test_memory_keys_and_dm_response_scope(self):
        self.assertEqual(replace(event(), react="happy").to_log_dict()["react"], "happy")
        for scope, key in (("channel", "../unsafe"), ("person", "../unsafe"), ("self", "world"), ("world", "self")):
            with self.subTest(scope=scope), self.assertRaises(ValueError):
                MemoryDocument(scope, key, "text")
        with self.assertRaises(ValueError):
            SandboxKey.for_event(replace(event(None), author_id="self", sandbox_key="guild:1"))
