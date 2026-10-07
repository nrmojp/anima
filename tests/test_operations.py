from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from anima.bootstrap.cli import _dashboard_panels, _reflection_summary, collect_status, run_doctor
from anima.core.models import Event, ResearchNote, ResearchSource
from anima.core.state import FileStateStore


NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone(timedelta(hours=9)))


def old_event(identifier: str) -> Event:
    return Event(
        id=identifier,
        ts=NOW - timedelta(days=40),
        kind="channel",
        channel_id="general",
        channel_name="#general",
        author_id="u1",
        author_name="太郎",
        text="ペルソナ",
        mention=True,
    )


class RetentionTests(unittest.TestCase):
    def test_sleeped_handled_log_is_archived_and_handled_marker_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = FileStateStore(root, log_retention_days=30)
            store.ensure_layout(now=NOW - timedelta(days=50))
            event = old_event("m1")
            store.append_received(event)
            store.mark_handled(event)
            cursor_path = root / "cursor.json"
            cursor = json.loads(cursor_path.read_text())
            cursor["digested_until"] = NOW.isoformat()
            cursor["slept_at"] = NOW.isoformat()
            cursor_path.write_text(json.dumps(cursor), encoding="utf-8")

            result = store.archive_old_logs(now=NOW)

            archive = root / "archive" / "log" / "general" / f"{event.ts.date()}.jsonl.gz"
            self.assertEqual(result["archived"], 1)
            self.assertTrue(archive.exists())
            with gzip.open(archive, "rt", encoding="utf-8") as stream:
                self.assertIn('"id":"m1"', stream.read())
            self.assertFalse((root / "log" / "general" / f"{event.ts.date()}.jsonl").exists())
            self.assertFalse((root / "handled" / "general" / f"{event.ts.date()}.txt").exists())

    def test_unhandled_log_is_never_archived(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = FileStateStore(root, log_retention_days=30)
            store.ensure_layout(now=NOW - timedelta(days=50))
            event = old_event("m1")
            store.append_received(event)
            cursor_path = root / "cursor.json"
            cursor = json.loads(cursor_path.read_text())
            cursor["digested_until"] = NOW.isoformat()
            cursor["slept_at"] = NOW.isoformat()
            cursor_path.write_text(json.dumps(cursor), encoding="utf-8")

            result = store.archive_old_logs(now=NOW)

            self.assertEqual(result["archived"], 0)
            self.assertEqual([item.id for item in store.pending_addressed_events()], ["m1"])

    def test_expired_archive_is_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "archive" / "log" / "general" / "2026-01-01.jsonl.gz"
            archive.parent.mkdir(parents=True)
            archive.write_bytes(b"old")
            store = FileStateStore(root, archive_retention_days=30)

            result = store.archive_old_logs(now=NOW)

            self.assertEqual(result["removed_archives"], 1)
            self.assertFalse(archive.exists())


class OperationalCliTests(unittest.TestCase):
    def test_dashboard_panel_metadata_is_bounded_and_validated(self):
        valid = {"id": "music", "title": " Music ", "renderer": "music", "order": 40,
                 "description": "Playback status", "eyebrow": "NOW PLAYING"}
        self.assertEqual(_dashboard_panels([valid], plugin="music"), [{
            **valid, "plugin": "music", "group": "status",
        }])
        invalid = [
            None,
            {"id": 1, "title": "x", "renderer": "x", "order": 1},
            {"id": "bad-id", "title": "x", "renderer": "x", "order": 1},
            {"id": "ok", "title": "x", "renderer": "bad-id", "order": 1},
            {"id": "ok", "title": "x", "renderer": "ok", "order": "1"},
            {"id": "ok", "title": " ", "renderer": "ok", "order": 1},
            {"id": "ok", "title": "x" * 81, "renderer": "ok", "order": 1},
            {"id": "ok", "title": "x", "renderer": "ok", "order": 1001},
            {"id": "ok", "title": "x", "renderer": "ok", "order": 1,
             "description": "x" * 241},
            {"id": "ok", "title": "x", "renderer": "ok", "order": 1,
             "eyebrow": ""},
            {"id": "ok", "title": "x", "renderer": "ok", "order": 1,
             "group": "unknown"},
        ]
        self.assertEqual(_dashboard_panels(invalid, plugin="test"), [])
        self.assertEqual(_dashboard_panels(None, plugin="test"), [])
    def test_status_reads_resources_and_state_from_separate_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            resources = Path(temporary) / "bot"
            state = resources / "state"
            resources.mkdir()
            FileStateStore(state, resource_root=resources).ensure_layout(now=NOW)
            (state / "habitus.md").write_text("- 話を聞く\n", encoding="utf-8")
            FileStateStore(state)._append_event(replace(
                old_event("researched"),
                author_id="self",
                research=ResearchNote(
                    "検索要約",
                    ("検索語",),
                    (ResearchSource("出典", "https://example.test/source"),),
                ),
            ))

            status = collect_status(resources, state_root=state)

            self.assertEqual(status["root"], str(resources))
            self.assertEqual(status["state_root"], str(state))
            self.assertEqual(status["state"]["habitus"], ["話を聞く"])
            self.assertEqual(status["state"]["research"]["summary"], "検索要約")
            self.assertEqual(status["state"]["research"]["queries"], ["検索語"])

    def test_reflection_summary_returns_latest_valid_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            (runtime / "anima.jsonl").write_text(
                "invalid\n"
                + json.dumps(
                    {
                        "event": "maintenance.completed",
                        "operation": "reflection",
                        "ts": "2026-08-01T00:00:00+09:00",
                        "changed": False,
                        "conflict": None,
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "event": "maintenance.completed",
                        "operation": "reflection",
                        "ts": "2026-09-01T00:00:00+09:00",
                        "changed": True,
                        "conflict": "personaの要確認",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (runtime / "anima.jsonl.1.gz").write_bytes(b"ignored")

            summary = _reflection_summary(runtime)

            self.assertTrue(summary["changed"])
            self.assertEqual(summary["conflict"], "personaの要確認")
            self.assertEqual(summary["completed_at"], "2026-09-01T00:00:00+09:00")

    def test_reflection_summary_is_empty_without_runtime_or_events(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertIsNone(_reflection_summary(root / "missing")["changed"])
            self.assertIsNone(_reflection_summary(root)["changed"])

    def test_status_summarizes_state_storage_and_openai_usage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            FileStateStore(root).ensure_layout(now=NOW)
            runtime = root / "runtime"
            runtime.mkdir()
            (root / "habitus.md").write_text("- 人の話を聞く\n", encoding="utf-8")
            (runtime / "anima.jsonl").write_text(
                "\n".join(json.dumps(value) for value in (
                    {"event": "openai.response.completed", "operation": "respond",
                     "model": "main", "ts": "2026-09-01T01:00:00Z",
                     "input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
                    {"event": "openai.response.completed", "operation": "sleep",
                     "model": "memory", "ts": "2026-09-02T01:00:00Z",
                     "input_tokens": 20, "output_tokens": 6, "total_tokens": 26},
                    {"event": "image_generation.completed", "operation": "image_generation",
                     "model": "gpt-image-2.5-flare", "ts": "2026-09-02T01:01:00Z",
                     "image_count": 1},
                    {"event": "image_generation.completed", "ts": "2026-09-02T01:02:00Z"},
                )) + "\n",
                encoding="utf-8",
            )

            status = collect_status(root)

            self.assertEqual(status["queue"]["pending"], 0)
            self.assertEqual(status["queue"]["failures"], [])
            self.assertEqual(status["openai_usage"]["requests"], 2)
            self.assertEqual(status["openai_usage"]["total_tokens"], 40)
            self.assertEqual(status["openai_usage"]["by_operation"]["sleep"],
                             {"requests": 1, "total_tokens": 26})
            self.assertEqual(status["openai_usage"]["by_operation"]["image_generation"],
                             {"requests": 0, "total_tokens": 0, "image_count": 1})
            self.assertEqual(status["openai_usage"]["by_model"]["gpt-image-2.5-flare"],
                             {"requests": 0, "total_tokens": 0, "image_count": 1})
            self.assertNotIn("unknown", status["openai_usage"]["by_model"])
            self.assertEqual(status["openai_usage"]["by_model"]["main"],
                             {"requests": 1, "total_tokens": 14})
            self.assertEqual(status["openai_usage"]["by_day"]["2026-09-02"],
                             {"requests": 1, "total_tokens": 26, "image_count": 1})
            self.assertEqual(status["state"]["habitus"], ["人の話を聞く"])
            self.assertEqual(status["state"]["reflected_at"], NOW.isoformat())

    def test_status_associates_final_failure_with_pending_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = FileStateStore(root)
            store.ensure_layout(now=NOW)
            source = old_event("m1")
            store.append_received(source)
            store.record_event_failure(
                source,
                operation="openai.respond",
                error_type="AuthenticationError",
                reason="http_401",
                now=NOW,
            )

            status = collect_status(root)

            self.assertEqual(status["queue"]["pending"], 1)
            self.assertEqual(status["queue"]["failures"][0]["event_id"], "m1")
            self.assertEqual(status["queue"]["failures"][0]["reason"], "http_401")

    def test_doctor_checks_secrets_without_exposing_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            FileStateStore(root).ensure_layout(now=NOW)
            (root / "persona.md").write_text("persona", encoding="utf-8")
            (root / "rules.md").write_text("rules", encoding="utf-8")
            (root / "appearance.md").write_text("appearance", encoding="utf-8")

            checks = run_doctor(
                root,
                {
                    "DISCORD_BOT_TOKEN": "discord-supersecret",
                    "OPENAI_API_KEY": "openai-supersecret",
                },
                online=False,
            )

            by_name = {item["name"]: item for item in checks}
            self.assertEqual(by_name["DISCORD_BOT_TOKEN"]["status"], "ok")
            self.assertEqual(by_name["OPENAI_API_KEY"]["status"], "ok")
            self.assertEqual(by_name["appearance.md"]["status"], "ok")
            rendered = json.dumps(checks)
            self.assertNotIn("discord-supersecret", rendered)
            self.assertNotIn("openai-supersecret", rendered)


if __name__ == "__main__":
    unittest.main()
