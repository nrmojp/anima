from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock, AsyncMock, patch

from anima.adapters.dashboard.server import (
    recent_events, operation_summary, self_time_summary, memory_contents, DashboardData, DashboardServer, _handler,
    serve_dashboard, dashboard_branding, CONFIG_DOCUMENTS,
)
from anima.adapters.dashboard.localization import render_asset
from anima.core.access import ActivityModeStore, ActivityPolicy
from anima.core.sandbox import SandboxKey
from anima.core.state import FileStateStore
from anima.core.inventory import InventoryStore
from anima.adapters.discord.client import AnimaDiscordClient, DiscordMessageSender
from test_core import NOW
from anima.core.telemetry import emit


class DashboardTests(unittest.TestCase):
    def test_usage_history_is_bounded_chronological_and_contains_no_text(self):
        events = [
            {"event": "openai.response.completed", "ts": f"2026-10-02T00:{i // 60:02}:{i % 60:02}Z",
             "operation": "respond", "model": "test", "input_tokens": i, "text": "private"}
            for i in range(120, -1, -1)
        ]
        bad = [{"event": "openai.response.completed", "ts": "x", "input_tokens": value}
               for value in (None, "4", -1, True)]
        history = operation_summary(bad + events)["usage_history"]
        self.assertEqual(len(history), 100)
        self.assertEqual(history[0]["input_tokens"], 21)
        self.assertEqual(history[-1]["input_tokens"], 120)
        self.assertNotIn("text", history[0])
        self.assertEqual(operation_summary([])["usage_history"], [])

    def test_context_usage_latest_per_operation(self):
        measured = {"event": "model.context.measured", "operation": "respond", "total": 12, "components": []}
        summary = operation_summary([measured, {**measured, "total": 5},
                                     {**measured, "operation": "self_time", "total": 7}])
        self.assertEqual(summary["context_usage"]["respond"]["total"], 12)
        self.assertEqual(summary["context_usage"]["self_time"]["total"], 7)
        self.assertIsNone(operation_summary([])["context_usage"]["respond"])

    def test_scoped_data_names_disabled_dm_validation_and_isolation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = DashboardData(root, root)
            self.assertEqual(data.scopes(), {
                "sandboxes": [], "default": None,
                "branding": dashboard_branding(root),
            })
            for key in ("guild:1", "guild:2", "dm:99"):
                sandbox = SandboxKey.parse(key)
                store = FileStateStore(sandbox.path(root), sandbox_key=sandbox)
                store.ensure_layout(now=NOW)
                (store.root / "habitus.md").write_text(f"- private {key}\n")
            runtime = root / "runtime"
            runtime.mkdir()
            (runtime / "anima.jsonl").write_text('\n'.join(json.dumps({"event": "test", "sandbox_key": k}) for k in ("guild:1", "guild:2", "dm:99")))
            data = DashboardData(root, root, default="guild:2", policy=ActivityPolicy(frozenset({"1"})))
            self.assertEqual(data.scopes()["default"], "guild:2")
            data.default = "guild:missing"
            self.assertEqual(data.scopes()["default"], "guild:1")
            data.policy = ActivityPolicy()
            self.assertEqual(data.scopes()["default"], "guild:1")
            (runtime / "status.json").write_text(json.dumps({"sandbox_names": {"guild:1": "実験室", "dm:99": "太郎"},
                "activity": {"allowed_guild_ids": ["2"], "dm_enabled": True}}))
            self.assertEqual(data.scopes()["default"], "guild:2")
            self.assertEqual(data.resolve("guild:1")["name"], "実験室")
            self.assertFalse(data.resolve("guild:1")["enabled"])
            self.assertTrue(data.resolve("dm:99")["enabled"])
            self.assertEqual(data.resolve("guild:1")["activity"]["mode"], "reply")
            ActivityModeStore(root).set(
                SandboxKey("guild", "1"), "silent", changed_by="admin", changed_at=NOW
            )
            InventoryStore(root, SandboxKey("guild", "1")).write_text(
                "note.md", "private inventory"
            )
            resources = root / "sandboxes/guilds/1/runtime/resources.json"
            resources.parent.mkdir(parents=True, exist_ok=True)
            resources.write_text(json.dumps({"collections": [{
                "id": "core.inventory", "scope": "sandbox", "operations": ["list"],
                "description": "inventory",
            }]}), encoding="utf-8")
            resources.with_name("skills.json").write_text(json.dumps({"skills": [{"id": "core:example"}], "reads": [{"resource_id": "core:example", "characters": 42}]}))
            self.assertEqual(data.status("guild:1")["skills"]["skills"][0]["id"], "core:example")
            self.assertEqual(data.status("guild:2")["skills"], {})
            with (runtime / "anima.jsonl").open("a", encoding="utf-8") as stream:
                stream.write("\n" + json.dumps({
                    "event": "resource.operation", "sandbox_key": "guild:1",
                    "collection": "core.inventory",
                }))
                stream.write("\n" + json.dumps({
                    "event": "resource.failed", "sandbox_key": "guild:1",
                    "collection": "core.memory", "resource_operation": "write",
                    "error_type": "PermissionError", "event_id": "message-1",
                    "ts": "2026-09-24T10:34:40Z",
                }))
                stream.write("\n" + json.dumps({
                    "event": "resource.failed", "sandbox_key": "guild:2",
                    "collection": "core.inventory", "error_type": "ValueError",
                }))
            self.assertEqual(data.resolve("guild:1")["activity"]["mode"], "silent")
            for key in ("guild:1", "guild:2", "dm:99"):
                self.assertEqual(data.status(key)["state"]["habitus"], [f"private {key}"])
                self.assertEqual(
                    data.status(key)["memory_contents"]["documents"][2]["path"], "habitus.md"
                )
                self.assertEqual(data.status(key)["memory_vector_store"]["state"], "empty")
                inventory = data.status(key)["inventory"]
                self.assertEqual(inventory["durable_count"], 1 if key == "guild:1" else 0)
                resources_status = data.status(key)["resources"]
                self.assertEqual(
                    resources_status.get("usage", {}).get("core.inventory", 0),
                    1 if key == "guild:1" else 0,
                )
                errors = data.status(key)["errors"]
                self.assertEqual(len(errors), 1 if key in {"guild:1", "guild:2"} else 0)
                if key == "guild:1":
                    self.assertEqual(errors[0]["event_id"], "message-1")
                    self.assertEqual(errors[0]["collection"], "core.memory")
                    self.assertNotIn("last_error", resources_status)
                result = data.events(key)
                self.assertEqual(result["sandbox_key"], key)
                self.assertTrue(result["events"])
                self.assertEqual({e["sandbox_key"] for e in result["events"]}, {key})
            for key in (None, "", "guild:3", "guild:../../", "dm:abc"):
                with self.subTest(key=key), self.assertRaises(ValueError):
                    data.status(key)
            (root / "sandboxes/guilds/3").symlink_to(root / "sandboxes/guilds/1")
            with self.assertRaises(ValueError):
                data.events("guild:3")

    def test_memory_contents_reads_scoped_documents_and_rejects_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            (state / "memory/people").mkdir(parents=True)
            (state / "digest.md").write_text("- 今日の話\n", encoding="utf-8")
            (state / "open.md").write_text("", encoding="utf-8")
            (state / "habitus.md").write_text("- よく笑う\n", encoding="utf-8")
            (state / "memory/self.md").write_text("- 自分の記憶\n", encoding="utf-8")
            (state / "memory/people/1.md").write_text("# 太郎\n- 猫好き\n", encoding="utf-8")
            (state / "memory/linked.md").symlink_to(state / "memory/self.md")
            (state / "memory/linked-dir").symlink_to(state / "memory/people")

            value = memory_contents(state)

            self.assertEqual(value["nonempty_documents"], 4)
            self.assertEqual(
                [item["path"] for item in value["documents"]],
                ["digest.md", "open.md", "habitus.md", "memory/people/1.md", "memory/self.md"],
            )
            self.assertEqual(value["documents"][2]["kind"], "habitus")
            person = value["documents"][3]
            self.assertEqual(person["kind"], "people")
            self.assertEqual(person["lines"], 2)
            self.assertEqual(person["title"], "太郎")
            self.assertEqual(person["person_id"], "1")
            self.assertEqual(person["entries"], 1)
            self.assertEqual(person["content"], "# 太郎\n- 猫好き")
            (state / "memory/people/1.md").write_text("## にもちゃん／お姉ちゃん\n関係: 姉\n- 体験\n", encoding="utf-8")
            self.assertEqual(memory_contents(state)["documents"][3]["title"], "にもちゃん／お姉ちゃん")
            (state / "memory/people/1.md").write_text("## \n", encoding="utf-8")
            self.assertEqual(memory_contents(state)["documents"][3]["title"], "Person 1")
            linked_child = state / "memory/linked-dir/1.md"
            with patch.object(Path, "glob", return_value=[linked_child]):
                guarded = memory_contents(state)
            self.assertEqual(
                [item["path"] for item in guarded["documents"]],
                ["digest.md", "open.md", "habitus.md"],
            )

    def test_http_requires_exact_scope_and_serves_assets_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            key = SandboxKey("guild", "1")
            FileStateStore(key.path(root), sandbox_key=key).ensure_layout(now=NOW)
            handler = _handler(root, root, sandbox="guild:1")
            def request(path):
                instance = object.__new__(handler)
                instance.path = path
                instance.wfile = BytesIO()
                instance.send_response = MagicMock()
                instance.send_header = MagicMock()
                instance.end_headers = MagicMock()
                instance.send_error = MagicMock()
                instance.do_GET()
                instance.log_message("unused")
                return instance
            for path in ("/", "/dashboard.css", "/dashboard.js", "/api/sandboxes", "/api/config", "/api/settings", "/api/status?sandbox=guild%3A1", "/api/events?sandbox=guild%3A1"):
                response = request(path)
                response.send_response.assert_called_once_with(200)
                self.assertTrue(response.wfile.getvalue())
            for query in ("", "?sandbox=", "?sandbox=guild:2", "?sandbox=guild:1&sandbox=guild:1", "?sandbox=../"):
                response = request("/api/status" + query)
                response.send_response.assert_called_once_with(400)
            request("/nope").send_error.assert_called_once_with(404)
            with patch("anima.adapters.dashboard.server.DashboardData.scopes", side_effect=OSError):
                request("/api/sandboxes").send_response.assert_called_once_with(503)
            with patch("anima.adapters.dashboard.server.ThreadingHTTPServer") as server:
                server.return_value.serve_forever.side_effect = KeyboardInterrupt
                serve_dashboard(root)
                server.assert_called_once_with(("0.0.0.0", 8765), unittest.mock.ANY)
                server.return_value.server_close.assert_called_once()

    def test_configuration_is_allowlisted_validated_and_atomic(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "persona.md").write_text("old\n", encoding="utf-8")
            data = DashboardData(root, root)
            configuration = data.configuration(writable=True)
            self.assertTrue(configuration["writable"])
            self.assertEqual(configuration["documents"][0]["content"], "old\n")
            saved = data.save_configuration("persona", "new persona")
            self.assertEqual(saved["content"], "new persona\n")
            self.assertEqual((root / "persona.md").read_text(), "new persona\n")
            dashboard = data.save_configuration("dashboard", json.dumps({
                "browser_title": "Title", "heading": "Heading", "eyebrow": "EYEBROW",
                "memory_guide": "Guide",
            }))
            self.assertIn('"heading": "Heading"', dashboard["content"])
            for document_id, content in (
                ("unknown", "x"), ("persona", ""), ("persona", "x" * 16001),
                ("dashboard", "bad"), ("dashboard", "[]"),
                ("dashboard", '{"unknown":"x"}'),
                ("dashboard", '{"browser_title":1}'),
            ):
                with self.subTest(document_id=document_id, content=content[:20]):
                    with self.assertRaises(ValueError):
                        data.save_configuration(document_id, content)
            (root / "rules.md").symlink_to(root / "persona.md")
            with self.assertRaises(ValueError):
                data.save_configuration("rules", "unsafe")

    def test_configuration_reload_applies_all_fixed_documents(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for document_id, (filename, *_rest) in CONFIG_DOCUMENTS.items():
                content = '{"heading":"N"}' if document_id == "dashboard" else document_id
                (root / filename).write_text(content, encoding="utf-8")
            reload_configuration = MagicMock(return_value=3)
            data = DashboardData(
                root, root, reload_configuration=reload_configuration,
            )

            result = data.reload_configuration()

            self.assertEqual(result["documents"], list(CONFIG_DOCUMENTS))
            self.assertEqual(result["active_sandboxes"], 3)
            reload_configuration.assert_called_once_with()
            (root / "appearance.md").write_text("", encoding="utf-8")
            with self.assertRaises(ValueError):
                data.reload_configuration()

    def test_runtime_configuration_uses_effective_values_and_requires_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = DashboardData(root, root, effective_config={
                "allowed_guild_ids": [], "dm_enabled": False,
                "plugins": ["echo"], "openai_model": "current",
            })
            current = data.runtime_configuration(writable=True)
            self.assertTrue(current["writable"])
            self.assertFalse(current["restart_required"])
            self.assertEqual(current["values"]["openai_model"], "current")
            values = dict(current["values"])
            values["openai_model"] = "next"
            self.assertTrue(data.save_runtime_configuration(values)["restart_required"])
            pending = data.runtime_configuration(writable=True)
            self.assertTrue(pending["restart_required"])
            self.assertEqual(pending["values"]["openai_model"], "next")

    def test_configuration_put_requires_token_and_rejects_bad_requests(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            key = SandboxKey("guild", "1")
            FileStateStore(key.path(root), sandbox_key=key).ensure_layout(now=NOW)
            InventoryStore(root, key).write_text("note.md", "download me")
            handler = _handler(root, root, admin_token="secret-token-1234")

            def put(path, payload, token="secret-token-1234", length=None):
                instance = object.__new__(handler)
                body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                instance.path = path
                instance.rfile = BytesIO(body)
                instance.wfile = BytesIO()
                instance.headers = {
                    "Content-Length": str(len(body) if length is None else length),
                    "X-Dashboard-Token": token,
                }
                instance.send_response = MagicMock()
                instance.send_header = MagicMock()
                instance.end_headers = MagicMock()
                instance.send_error = MagicMock()
                instance.do_PUT()
                return instance

            def post(path, token="secret-token-1234", payload=None):
                instance = object.__new__(handler)
                instance.path = path
                body = b"" if payload is None else (
                    payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                )
                instance.rfile = BytesIO(body)
                instance.wfile = BytesIO()
                instance.headers = {
                    "X-Dashboard-Token": token,
                    "Content-Length": str(len(body)),
                }
                instance.send_response = MagicMock()
                instance.send_header = MagicMock()
                instance.end_headers = MagicMock()
                instance.send_error = MagicMock()
                instance.do_POST()
                return instance

            def delete(path, payload, token="secret-token-1234"):
                instance = object.__new__(handler)
                body = json.dumps(payload).encode()
                instance.path = path
                instance.rfile = BytesIO(body)
                instance.wfile = BytesIO()
                instance.headers = {
                    "X-Dashboard-Token": token, "Content-Length": str(len(body)),
                }
                instance.send_response = MagicMock()
                instance.send_header = MagicMock()
                instance.end_headers = MagicMock()
                instance.send_error = MagicMock()
                instance.do_DELETE()
                return instance

            response = put("/api/config/persona", {"content": "new"})
            self.assertEqual(response.send_response.call_args.args[0], 200)
            self.assertEqual((root / "persona.md").read_text(), "new\n")
            self.assertEqual(put("/api/config/persona", {"content": "x"}, "wrong").send_response.call_args.args[0], 403)
            self.assertEqual(put("/api/config/persona", b"bad").send_response.call_args.args[0], 400)
            self.assertEqual(put("/api/config/persona", {"other": "x"}).send_response.call_args.args[0], 400)
            self.assertEqual(put("/api/config/persona", {"content": "x"}, length=0).send_response.call_args.args[0], 400)
            with patch("anima.adapters.dashboard.server.DashboardData.save_configuration",
                       side_effect=OSError):
                self.assertEqual(
                    put("/api/config/persona", {"content": "x"}).send_response.call_args.args[0],
                    503,
                )
            self.assertEqual(put("/api/settings", {}).send_response.call_args.args[0], 200)
            self.assertTrue((root / "config.json").is_file())
            self.assertEqual(put("/api/settings", {}, "wrong").send_response.call_args.args[0], 403)
            self.assertEqual(put("/api/settings", b"bad").send_response.call_args.args[0], 400)
            self.assertEqual(put("/api/settings", {}, length=0).send_response.call_args.args[0], 400)
            with patch("anima.adapters.dashboard.server.DashboardData.save_runtime_configuration",
                       side_effect=OSError):
                self.assertEqual(put("/api/settings", {}).send_response.call_args.args[0], 503)
            put("/nope", {"content": "x"}).send_error.assert_called_once_with(404)
            with patch("anima.adapters.dashboard.server.DashboardData.reload_configuration",
                       return_value={"reloaded": True}):
                self.assertEqual(post("/api/config/reload").send_response.call_args.args[0], 200)
                self.assertEqual(post("/api/config/reload", "wrong").send_response.call_args.args[0], 403)
            post("/nope").send_error.assert_called_once_with(404)
            with patch("anima.adapters.dashboard.server.DashboardData.reload_configuration",
                       side_effect=ValueError):
                self.assertEqual(post("/api/config/reload").send_response.call_args.args[0], 400)
            with patch("anima.adapters.dashboard.server.DashboardData.reload_configuration",
                       side_effect=OSError):
                self.assertEqual(post("/api/config/reload").send_response.call_args.args[0], 503)
            artifact = {
                "sandbox": "guild:1", "artifact_id": "note.md", "location": "inventory",
            }
            downloaded = post("/api/inventory/download", payload=artifact)
            self.assertEqual(downloaded.send_response.call_args.args[0], 200)
            self.assertEqual(downloaded.wfile.getvalue(), b"download me")
            self.assertEqual(
                post("/api/inventory/download", "wrong", artifact).send_response.call_args.args[0],
                403,
            )
            self.assertEqual(
                post("/api/inventory/download", payload={}).send_response.call_args.args[0], 400
            )
            self.assertEqual(
                post("/api/inventory/download", payload=[]).send_response.call_args.args[0], 400
            )
            self.assertEqual(
                post("/api/inventory/download", payload=b"").send_response.call_args.args[0], 400
            )
            with patch(
                "anima.adapters.dashboard.server.DashboardData.inventory_artifact",
                side_effect=OSError,
            ):
                self.assertEqual(
                    post("/api/inventory/download", payload=artifact).send_response.call_args.args[0],
                    503,
                )
            self.assertEqual(
                delete("/api/inventory", artifact, "wrong").send_response.call_args.args[0], 403
            )
            self.assertEqual(
                delete("/api/inventory", {}).send_response.call_args.args[0], 400
            )
            with patch(
                "anima.adapters.dashboard.server.DashboardData.delete_inventory_artifact",
                side_effect=OSError,
            ):
                self.assertEqual(
                    delete("/api/inventory", artifact).send_response.call_args.args[0], 503
                )
            self.assertEqual(delete("/api/inventory", artifact).send_response.call_args.args[0], 200)
            self.assertFalse((key.path(root) / "inventory/note.md").exists())
            self.assertEqual(delete("/api/inventory", artifact).send_response.call_args.args[0], 400)
            delete("/nope", artifact).send_error.assert_called_once_with(404)
            with self.assertRaises(ValueError):
                _handler(root, root, admin_token="short")

    def test_cli_dashboard_no_longer_requires_initial_scope(self):
        from anima.bootstrap.cli import main
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("anima.bootstrap.cli._environment", return_value=({}, root, root)), patch("anima.adapters.dashboard.server.serve_dashboard") as serve:
                main(["dashboard"])
                self.assertIsNone(serve.call_args.kwargs["sandbox"])
                self.assertEqual(serve.call_args.kwargs["host"], "127.0.0.1")
                self.assertFalse(serve.call_args.kwargs["policy"].dm_enabled)

    def test_embedded_dashboard_server_starts_and_stops_thread(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "anima.adapters.dashboard.server.ThreadingHTTPServer"
        ) as server_type, patch("anima.adapters.dashboard.server.threading.Thread") as thread_type:
            thread = thread_type.return_value
            thread.is_alive.return_value = True
            server = DashboardServer(
                Path(directory),
                state_root=Path(directory),
                host="127.0.0.1",
                port=8765,
                policy=ActivityPolicy(),
            )
            with server:
                thread.start.assert_called_once_with()
            server_type.assert_called_once_with(("127.0.0.1", 8765), unittest.mock.ANY)
            thread_type.assert_called_once_with(
                target=server_type.return_value.serve_forever,
                name="anima-dashboard",
                daemon=True,
            )
            server_type.return_value.shutdown.assert_called_once_with()
            thread.join.assert_called_once_with(timeout=5)
            server_type.return_value.server_close.assert_called_once_with()

    def test_embedded_dashboard_stop_before_start_only_closes_socket(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "anima.adapters.dashboard.server.ThreadingHTTPServer"
        ) as server_type, patch("anima.adapters.dashboard.server.threading.Thread") as thread_type:
            thread_type.return_value.is_alive.return_value = False
            server = DashboardServer(
                Path(directory), state_root=Path(directory), host="0.0.0.0", port=8765,
                policy=ActivityPolicy(),
            )
            server.stop()
            server_type.return_value.shutdown.assert_not_called()
            server_type.return_value.server_close.assert_called_once_with()

    def test_telemetry_contains_timestamp_for_dashboard_timeline(self) -> None:
        with self.assertLogs("anima.telemetry", level="INFO") as captured:
            emit("dashboard.test", value=1)
        value = json.loads(captured.output[0].split(":", 2)[2])
        self.assertEqual(value["event"], "dashboard.test")
        self.assertIn("+00:00", value["ts"])

    def test_recent_events_returns_newest_first_and_ignores_invalid_lines(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "runtime"
            runtime.mkdir()
            (runtime / "anima.jsonl").write_text(
                json.dumps({"event": "first"})
                + "\ninvalid\n"
                + json.dumps({"event": "last"})
                + "\n",
                encoding="utf-8",
            )

            events = recent_events(root)

            self.assertEqual([item["event"] for item in events], ["last", "first"])
            (runtime / "anima.jsonl.gz").write_bytes(b"unused")
            self.assertEqual(len(recent_events(root, limit=1)), 1)

    def test_operation_summary_reports_live_and_latest_operational_state(self) -> None:
        events = [
            {"ts": "2026-09-05T03:06:00Z", "event": "openai.request.started",
             "operation": "sleep", "model": "memory"},
            {"ts": "2026-09-05T03:05:30Z", "event": "music.started",
             "track_id": "song-1", "title": "夜の曲"},
            {"ts": "2026-09-05T03:05:00Z", "event": "maintenance.started",
             "operation": "sleep"},
            {"ts": "2026-09-05T03:04:00Z", "event": "operation.retry",
             "operation": "openai.respond", "attempt": 2},
            {"ts": "2026-09-05T03:03:00Z", "event": "persona.queued",
             "event_id": "active", "depth": 2},
            {"ts": "2026-09-05T03:02:00Z", "event": "persona.processed",
             "event_id": "done", "depth": 1},
            {"ts": "2026-09-05T03:01:30Z", "event": "openai.response.completed",
             "operation": "respond", "model": "gpt-test", "input_tokens": 120,
             "output_tokens": 30, "total_tokens": 150, "request_count": 2},
            {"ts": "2026-09-05T03:01:00Z", "event": "persona.queued",
             "event_id": "done", "depth": 1},
        ]

        summary = operation_summary(events)

        self.assertEqual(summary["observed_queue_depth"], 2)
        self.assertEqual(summary["current_event_id"], "active")
        self.assertTrue(summary["maintenance"]["active"])
        self.assertEqual(summary["maintenance"]["operation"], "sleep")
        self.assertEqual(summary["last_retry"]["attempt"], 2)
        self.assertEqual(summary["last_openai"]["state"], "running")
        self.assertEqual(summary["last_usage"]["total_tokens"], 150)
        self.assertEqual(summary["last_usage"]["input_tokens"], 120)
        self.assertEqual(summary["last_usage"]["request_count"], 2)


    def test_status_prefers_live_actor_snapshot_over_log_inference(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            key = SandboxKey("guild", "1")
            state = key.path(root)
            FileStateStore(state, sandbox_key=key).ensure_layout(now=NOW)
            (root / "runtime").mkdir(exist_ok=True)
            (root / "runtime" / "status.json").write_text(json.dumps({
                "connected": True, "pid": os.getpid(), "sandbox_names": {"guild:1": "実験室"},
                "features": {"voice_enabled": True, "plugins": ["voice"]},
            }))
            actor_path = state / "runtime" / "actor.json"
            actor_path.parent.mkdir(exist_ok=True)
            actor_path.write_text(json.dumps({
                "running": True, "queue_depth": 4,
                "current": {"kind": "maintenance", "operation": "sleep"},
                "retry": {"operation": "openai.respond", "attempt": 1},
            }))
            (state / "runtime" / "plugins.json").write_text(json.dumps({"plugins": [{
                "name": "voice", "version": "1", "available": True, "running": True,
                "provides": ["voice"], "dashboard_panels": [{
                    "id": "voice", "title": "声の待機列", "renderer": "voice", "order": 30,
                }],
            }]}))
            data = DashboardData(root, root, policy=ActivityPolicy(frozenset({"1"})))

            status = data.status("guild:1")

            self.assertEqual(status["operations"]["observed_queue_depth"], 4)
            self.assertTrue(status["operations"]["maintenance"]["active"])
            self.assertEqual(status["operations"]["maintenance"]["operation"], "sleep")
            self.assertEqual(status["operations"]["last_retry"]["attempt"], 1)
            self.assertTrue(status["voice"]["enabled"])
            self.assertFalse(status["voice"]["initialized"])
            self.assertEqual(
                [item["name"] for item in status["plugins"]],
                ["echo", "voice", "web_search"],
            )
            self.assertTrue(next(item for item in status["plugins"] if item["name"] == "voice")["enabled"])
            self.assertEqual(status["dashboard"]["panels"], [{
                "id": "voice", "title": "声の待機列", "renderer": "voice",
                "order": 30, "plugin": "voice", "description": "", "eyebrow": "PLUGIN", "group": "status",
            }])
            actor_path.write_text(json.dumps({
                "running": True, "queue_depth": 0, "current": None, "retry": None,
            }))
            self.assertIsNone(data.status("guild:1")["operations"]["current_event_id"])

    def test_operation_summary_handles_completed_and_empty_history(self) -> None:
        completed = operation_summary([
            {"ts": "2026-09-05T03:06:00Z", "event": "maintenance.completed",
             "operation": "digest"},
            {"ts": "2026-09-05T03:05:00Z", "event": "openai.response.completed",
             "operation": "digest", "model": "main", "duration_ms": 50},
            {"ts": "2026-09-05T03:04:00Z", "event": "persona.processed",
             "event_id": "done", "depth": 0},
            {"ts": "2026-09-05T03:03:00Z", "event": "persona.queued",
             "event_id": "done", "depth": 1},
        ])
        self.assertFalse(completed["maintenance"]["active"])
        self.assertIsNone(completed["current_event_id"])
        self.assertEqual(completed["last_openai"]["state"], "completed")
        self.assertEqual(completed["last_openai"]["duration_ms"], 50)
        empty = operation_summary([])
        self.assertEqual(empty["observed_queue_depth"], 0)
        self.assertEqual(empty["last_openai"]["state"], "none")
        self.assertIsNone(empty["last_usage"])

        failed = operation_summary([{
            "ts": "2026-09-05T03:07:00Z", "event": "external.failed",
            "operation": "openai.respond", "error_type": "TimeoutError", "reason": "timeout",
        }])
        self.assertEqual(failed["last_openai"]["state"], "failed")
        self.assertEqual(failed["last_openai"]["error_type"], "TimeoutError")
        sent = operation_summary([{
            "ts": "2026-09-05T03:08:00Z", "event": "external.completed",
            "operation": "discord.send", "duration_ms": 12,
        }])
        self.assertEqual(sent["last_openai"]["state"], "completed")
        self.assertEqual(sent["last_openai"]["operation"], "discord.send")

    def test_self_time_summary_exposes_saved_notes_and_tool_activity(self) -> None:
        events = [
            {"ts": "2026-09-22T03:04:00Z", "event": "self_time.completed",
             "action": "finish", "iterations": 2, "notes": ["絵を眺めて残した"],
             "directions": ["deepen", "broaden"], "discoveries": ["絵を見返した", "猫の話題に広げた"],
             "reflections": ["前に描いた絵が気になった", "残しておこうと思った"]},
            {"ts": "2026-09-22T03:03:00Z", "event": "openai.tool.completed",
             "operation": "self_time", "tool_name": "resource_transfer", "duration_ms": 12},
            {"ts": "2026-09-22T03:02:00Z", "event": "openai.tool.completed",
             "operation": "respond", "tool_name": "search_music"},
        ]

        value = self_time_summary(
            events, {"day": "2026-09-22", "starts": 1,
                     "last_started_at": "2026-09-22T03:00:00+09:00"},
            {"active": []},
        )

        self.assertEqual(value["state"], "completed")
        self.assertEqual(value["starts"], 1)
        self.assertEqual(value["latest"]["notes"], ["絵を眺めて残した"])
        self.assertEqual(value["latest"]["reflections"][0], "前に描いた絵が気になった")
        self.assertEqual(value["latest"]["directions"], ["deepen", "broaden"])
        self.assertEqual(value["latest"]["discoveries"][1], "猫の話題に広げた")
        self.assertEqual(len(value["sessions"]), 1)
        self.assertEqual(value["sessions"][0]["tools"][0]["tool_name"], "resource_transfer")
        running = self_time_summary([], {}, {"active": [{
            "plugin": "core", "id": "self_time",
        }]})
        self.assertEqual(running["state"], "running")
        bounded = self_time_summary([
            {"event": "self_time.skipped", "reason": "mode"}
            for _ in range(20)
        ], {}, {"active": []})
        self.assertEqual(len(bounded["sessions"]), 8)
        self.assertEqual(bounded["state"], "skipped")

        lifecycle_edges = self_time_summary([
            {"event": "self_time.completed", "ts": "2026-09-22T03:06:00Z"},
            {"event": "self_time.started", "ts": "2026-09-22T03:05:00Z"},
            {"event": "self_time.started", "ts": "2026-09-22T03:04:00Z"},
        ], {}, {"active": []})
        self.assertEqual(len(lifecycle_edges["sessions"]), 2)
        orphan = self_time_summary([
            {"event": "self_time.failed", "error_type": "ValueError"},
        ], {}, {"active": []})
        self.assertEqual(orphan["latest"]["state"], "failed")
        unfinished = self_time_summary([
            {"event": "openai.tool.started", "operation": "self_time",
             "tool_name": "resource_read"},
            {"event": "self_time.started"},
        ], {}, {"active": []})
        self.assertEqual(unfinished["latest"]["state"], "interrupted")

    def test_dashboard_assets_are_packaged_beside_module(self) -> None:
        assets = Path(__file__).parents[1] / "src" / "anima" / "adapters" / "dashboard" / "assets"
        self.assertTrue((assets / "index.html").is_file())
        self.assertTrue((assets / "dashboard.css").is_file())
        self.assertTrue((assets / "dashboard.js").is_file())
        guide = render_asset((assets / "index.html").read_text(encoding="utf-8"), "ja")
        script = render_asset((assets / "dashboard.js").read_text(encoding="utf-8"), "ja", script=True)
        styles = (assets / "dashboard.css").read_text(encoding="utf-8")
        self.assertIn("ひとりの時間", guide)
        self.assertIn('class="skip-link" href="#dashboard-groups"', guide)
        self.assertIn('id="dashboard-groups" tabindex="-1"', guide)
        self.assertIn('grid-template-columns:240px minmax(0,1fr)', styles)
        self.assertIn('#dashboard-groups{display:block;', styles)
        self.assertIn('desktopNavigation.addEventListener', script)
        self.assertIn("--panel-gap:24px", styles)
        self.assertIn(".grid{grid-template-columns:repeat(4,minmax(0,1fr))}", styles)
        self.assertIn(".grid{grid-template-columns:repeat(2,minmax(0,1fr))}", styles)
        self.assertIn("#events,#plugin-jobs,#error-history,#plugin-logs{max-height:360px;overflow:auto}", styles)
        self.assertIn(".memory-reader pre{padding:16px;font-size:12px", styles)
        self.assertIn("renderSelfTime", script)
        self.assertIn('id="resources" class="resource-grid"', guide)
        self.assertIn("className='resource-card'", script)
        self.assertIn("className='resource-operations'", script)
        self.assertIn(".resource-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))", styles)
        self.assertIn("@media(max-width:760px){.resource-grid{grid-template-columns:1fr}}", styles)
        self.assertIn('class="section-menu"', guide)
        for section_id in ("overview", "self-time", "plugins-section", "config-section",
                           "settings-section", "usage", "inventory-section", "resources-section",
                           "memory-section", "jobs-section", "errors-section", "trace-section"):
            self.assertIn(f'href="#{section_id}"', guide)
            self.assertIn(f'id="{section_id}"', guide)
        for group in ("status", "memory", "configuration", "diagnostics"):
            self.assertIn(f'id="group-{group}"', guide)
            self.assertIn(f'id="plugin-menu-{group}"', guide)
            self.assertIn(f'plugin-panels-${{group}}', script)
        self.assertIn('function organizeDashboard()', script)
        self.assertIn('link.href=`#${panel.id}`', script)
        self.assertIn('if(!panel.id)panel.id=', script)
        self.assertIn('function renderMusic(', script)
        for event in ("persona.*", "openai.*", "maintenance.*", "reaction.* / face.*",
                      "voice.*", "native_tool.*", "operation.retry", "discord.reply.recovered"):
            self.assertIn(event, guide)
        for field in ("duration_ms", "queue_wait_ms", "depth", "tool_name", "round",
                      "error_type", "reason"):
            self.assertIn(field, guide)
        for element_id in ("actor-state", "maintenance-state", "api-state",
                           "gateway-ping", "usage-operation", "usage-model", "usage-day",
                           "usage-current-total", "usage-current-input",
                           "usage-current-output", "usage-current-meta", "usage-current-time",
                           "retry-detail", "plugin-panels", "memory-summary", "memory-documents",
                           "memory-reader-title", "memory-reader-body", "config-load", "config-editor", "config-content",
                           "config-save", "settings-load", "settings-editor",
                           "settings-fields", "settings-save", "inventory-durable-items",
                           "inventory-temporary-items", "inventory-durable-count",
                           "inventory-temporary-count"):
            source = guide
            self.assertIn(f'id="{element_id}"', source)
        for element_id in ("voice-state", "voice-detail", "research-summary"):
            self.assertNotIn(f'id="{element_id}"', guide)
            self.assertIn(f'id="{element_id}"', script)

    def test_dashboard_branding_is_persona_owned_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(dashboard_branding(root)["heading"], "Anima Observatory")
            (root / "dashboard.json").write_text(json.dumps({
                "browser_title": "Persona Console",
                "heading": "Persona Observatory",
                "eyebrow": "PERSONA / STATUS",
                "memory_guide": "Persona memory.",
            }), encoding="utf-8")
            self.assertEqual(dashboard_branding(root), {
                "locale": "en",
                "browser_title": "Persona Console",
                "heading": "Persona Observatory",
                "eyebrow": "PERSONA / STATUS",
                "memory_guide": "Persona memory.",
            })
            (root / "dashboard.json").write_text(json.dumps({
                "heading": "x" * 81, "eyebrow": " ", "memory_guide": 1,
            }), encoding="utf-8")
            self.assertEqual(dashboard_branding(root)["heading"], "Anima Observatory")


class DashboardMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_timeout_is_logged_without_blocking_close(self):
        async def slow_stop():
            await asyncio.sleep(1)

        client = AnimaDiscordClient(
            actor=SimpleNamespace(stop=slow_stop),
            sender=DiscordMessageSender(),
            shutdown_timeout_seconds=0.001,
        )
        with self.assertLogs("anima.adapters.discord.client", level="ERROR") as captured:
            await client.close()
        self.assertIn("shutdown exceeded", "\n".join(captured.output))

    async def test_status_persists_names_and_activity_without_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime/status.json"
            client = AnimaDiscordClient(actor=SimpleNamespace(stop=AsyncMock()), sender=DiscordMessageSender(),
                status_path=path, policy=ActivityPolicy(frozenset({"1"}), True),
                voice_enabled=True)
            client._connection._guilds = {1: SimpleNamespace(id=1, name="実験室")}
            client._dm_names = {"dm:99": "太郎"}
            client._write_status(connected=True)
            value = json.loads(path.read_text())
            self.assertEqual(value["sandbox_names"], {"guild:1": "実験室", "dm:99": "太郎"})
            self.assertEqual(value["activity"], {"allowed_guild_ids": ["1"], "dm_enabled": True})
            self.assertEqual(value["features"], {
                "voice_enabled": True,
                "plugins": [],
            })
            self.assertIsNone(value["gateway_ping_ms"])
            client._dm_names.clear()
            client._write_status(connected=True)
            self.assertEqual(json.loads(path.read_text())["sandbox_names"]["dm:99"], "太郎")
            await client.close()


if __name__ == "__main__":
    unittest.main()
