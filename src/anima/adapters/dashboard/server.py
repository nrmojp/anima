"""Read-only local operations dashboard."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
from pathlib import Path
import threading
from urllib.parse import urlparse, parse_qs

from anima.bootstrap.cli import collect_status, _read_json
from anima.bootstrap.runtime_config import (
    FIELDS, RuntimeConfigError, load_runtime_config, save_runtime_config,
)
from anima.core.access import ActivityModeStore, ActivityPolicy
from anima.core.sandbox import SandboxKey, list_sandboxes
from anima.core.inventory import InventoryStore
from anima.core.jobs import PluginJobManager
from anima.core.telemetry import emit


ASSET_DIR = Path(__file__).with_name("assets")
DEFAULT_BRANDING = {
    "browser_title": "Anima Operations",
    "heading": "Anima Observatory",
    "eyebrow": "ANIMA / LOCAL OBSERVATORY",
    "memory_guide": "現在保持している記憶文書を確認できます。",
}
CONFIG_DOCUMENTS = {
    "persona": ("persona.md", "ペルソナ", "markdown", 16000),
    "rules": ("rules.md", "会話ルール", "markdown", 16000),
    "appearance": ("appearance.md", "外見設定", "markdown", 16000),
    "dashboard": ("dashboard.json", "ダッシュボード表示", "json", 4000),
}


def dashboard_branding(root: Path) -> dict[str, str]:
    """Read bounded presentation metadata owned by the deployed persona."""
    value = _read_json(root / "dashboard.json")
    result = dict(DEFAULT_BRANDING)
    for key, limit in (("browser_title", 80), ("heading", 80), ("eyebrow", 80),
                       ("memory_guide", 240)):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip() and len(candidate) <= limit:
            result[key] = candidate.strip()
    return result


def memory_contents(state: Path) -> dict[str, object]:
    """Return readable memory documents without following links outside state."""
    documents = []
    candidates = [state / "digest.md", state / "open.md", state / "habitus.md"]
    memory_root = state / "memory"
    if memory_root.is_dir() and not memory_root.is_symlink():
        candidates.extend(sorted(memory_root.glob("**/*.md")))
    for path in candidates:
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(state)
        parent = path.parent
        linked_parent = False
        while parent != state:
            linked_parent = linked_parent or parent.is_symlink()
            parent = parent.parent
        if linked_parent:
            continue
        content = path.read_text(encoding="utf-8", errors="replace").strip()
        kind = (
            "digest" if relative.as_posix() == "digest.md"
            else "open" if relative.as_posix() == "open.md"
            else "habitus" if relative.as_posix() == "habitus.md"
            else relative.parts[1] if len(relative.parts) > 2
            else relative.stem
        )
        documents.append({
            "path": relative.as_posix(),
            "kind": kind,
            "title": next((line.lstrip("# ").strip() for line in content.splitlines()
                           if line.startswith(("## ", "# ")) and line.lstrip("# ").strip()),
                          f"人物 {path.stem}" if kind == "people" else ""),
            "person_id": path.stem if kind == "people" else None,
            "entries": sum(line.startswith("- ") for line in content.splitlines()),
            "lines": len(content.splitlines()) if content else 0,
            "content": content,
        })
    return {
        "documents": documents,
        "nonempty_documents": sum(bool(item["content"]) for item in documents),
    }


class DashboardData:
    """Read only; every detail request resolves to one existing sandbox."""

    def __init__(self, root, state_root, *, default=None, policy=None, effective_config=None,
                 reload_configuration=None):
        self.root, self.state_root = root, state_root
        self.default = default
        self.policy = policy or ActivityPolicy()
        self.activity_modes = ActivityModeStore(state_root)
        self.effective_config = effective_config
        self._reload_configuration = reload_configuration

    def scopes(self):
        runtime = _read_json(self.state_root / "runtime" / "status.json")
        names = runtime.get("sandbox_names", {})
        activity = runtime.get("activity")
        policy = self.policy if activity is None else ActivityPolicy(
            frozenset(activity.get("allowed_guild_ids", [])), activity.get("dm_enabled", False))
        items = []
        for key in list_sandboxes(self.state_root):
            enabled = policy.allows(key)
            activity = self.activity_modes.details(key)
            activity["effective_mode"] = activity["mode"] if enabled else "disabled"
            items.append({
                "key": str(key),
                "kind": key.kind,
                "id": key.id,
                "name": names.get(
                    str(key), f"{'ギルド' if key.kind == 'guild' else 'DM相手' if key.kind == 'dm' else key.namespace} {key.id}"
                ),
                "enabled": enabled,
                "activity": activity,
            })
        keys = [item["key"] for item in items]
        default = self.default if self.default in keys else next(
            (item["key"] for item in items if item["enabled"]), keys[0] if keys else None)
        return {
            "sandboxes": items,
            "default": default,
            "branding": dashboard_branding(self.root),
        }

    def resolve(self, requested):
        if not requested:
            raise ValueError("sandbox is required")
        key = SandboxKey.parse(requested)
        for item in self.scopes()["sandboxes"]:
            if item["key"] == str(key):
                return item
        raise ValueError("unknown sandbox")

    def status(self, requested):
        selected = self.resolve(requested)
        sandbox_key = SandboxKey.parse(selected["key"])
        sandbox_root = sandbox_key.path(self.state_root)
        result = collect_status(self.root, state_root=self.state_root, sandbox=selected["key"])
        result["memory_contents"] = memory_contents(sandbox_root)
        inventory = InventoryStore(self.state_root, sandbox_key)
        items = inventory.list(include_temporary=True)
        result["inventory"] = {
            "summary": inventory.summary(),
            "items": [item.to_dict() for item in items],
            "durable_count": sum(item.location == "inventory" for item in items),
            "temporary_count": sum(item.location == "temporary" for item in items),
            "total_bytes": sum(item.size for item in items),
        }
        result["resources"] = _read_json(sandbox_root / "runtime" / "resources.json")
        from anima.core.plugin_logs import read_plugin_logs
        result["plugin_logs"] = read_plugin_logs(sandbox_root)
        jobs = PluginJobManager(sandbox_root / "runtime" / "plugin-jobs.json")
        records = jobs.list()
        result["plugin_jobs"] = {
            "items": [item.to_dict() for item in records[:50]],
            "active_count": sum(item.state in {"queued", "running"} for item in records),
            "failed_count": sum(item.state == "failed" for item in records),
        }
        result["modes"] = _read_json(sandbox_root / "runtime" / "modes.json")
        history = recent_events(self.state_root, limit=500, sandbox=selected["key"])
        result["self_time"] = self_time_summary(
            history,
            _read_json(sandbox_root / "runtime" / "self-time.json"),
            result["modes"],
        )
        operations = operation_summary(history)
        resource_events = [
            event for event in history if str(event.get("event", "")).startswith("resource.")
        ]
        resource_counts: dict[str, int] = {}
        for event in resource_events:
            if event.get("event") != "resource.operation":
                continue
            collection = str(event.get("collection", "unknown"))
            resource_counts[collection] = resource_counts.get(collection, 0) + 1
        result["resources"]["usage"] = resource_counts
        result["errors"] = [
            event for event in history
            if str(event.get("event", "")).endswith(".failed")
        ][:30]
        actor = result.get("actor") or {}
        if actor:
            operations["observed_queue_depth"] = int(actor.get("queue_depth", 0) or 0)
            current = actor.get("current")
            operations["current"] = current
            operations["current_event_id"] = (
                current.get("event_id") if isinstance(current, dict) else None
            )
            operations["last_retry"] = actor.get("retry")
            operations["maintenance"]["active"] = bool(
                isinstance(current, dict) and current.get("kind") == "maintenance"
            )
            if operations["maintenance"]["active"]:
                operations["maintenance"]["operation"] = current.get("operation")
        if not result["bot"]["running"]:
            operations["current_event_id"] = None
            operations["maintenance"]["active"] = False
        result["operations"] = operations
        result["selection"] = selected
        return result

    def events(self, requested):
        selected = self.resolve(requested)
        return {"sandbox_key": selected["key"], "events": recent_events(self.state_root, sandbox=selected["key"])}

    def inventory_artifact(self, requested, artifact_id, location):
        selected = self.resolve(requested)
        key = SandboxKey.parse(selected["key"])
        store = InventoryStore(self.state_root, key)
        path = store.resolve(str(artifact_id), location=str(location))
        item = store.describe(str(artifact_id), location=str(location))
        emit(
            "inventory.dashboard.downloaded", sandbox_key=str(key),
            artifact_id=item.id, location=item.location, size=item.size,
        )
        return path, item

    def delete_inventory_artifact(self, requested, artifact_id, location):
        selected = self.resolve(requested)
        key = SandboxKey.parse(selected["key"])
        store = InventoryStore(self.state_root, key)
        removed = store.discard(str(artifact_id), location=str(location))
        emit(
            "inventory.dashboard.deleted", sandbox_key=str(key),
            artifact_id=str(artifact_id), location=removed,
        )
        return {"deleted": True, "artifact_id": str(artifact_id), "location": removed}

    def configuration(self, *, writable: bool) -> dict[str, object]:
        documents = []
        for document_id, (filename, label, format_name, limit) in CONFIG_DOCUMENTS.items():
            path = self.root / filename
            content = ""
            if path.is_file() and not path.is_symlink():
                content = path.read_text(encoding="utf-8")
            documents.append({
                "id": document_id, "label": label, "format": format_name,
                "content": content, "max_length": limit,
            })
        return {"writable": writable, "documents": documents}

    def save_configuration(self, document_id: str, content: object) -> dict[str, object]:
        spec = CONFIG_DOCUMENTS.get(document_id)
        if spec is None or not isinstance(content, str):
            raise ValueError("unknown configuration document")
        filename, _label, format_name, limit = spec
        if not content.strip() or len(content) > limit:
            raise ValueError("configuration content is invalid")
        if format_name == "json":
            try:
                value = json.loads(content)
            except json.JSONDecodeError as error:
                raise ValueError("configuration JSON is invalid") from error
            if not isinstance(value, dict) or set(value) - set(DEFAULT_BRANDING):
                raise ValueError("configuration JSON fields are invalid")
            for key, field_limit in (("browser_title", 80), ("heading", 80), ("eyebrow", 80),
                                     ("memory_guide", 240)):
                field = value.get(key)
                if not isinstance(field, str) or not field.strip() or len(field) > field_limit:
                    raise ValueError("configuration JSON values are invalid")
            content = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        elif not content.endswith("\n"):
            content += "\n"
        path = self.root / filename
        if path.is_symlink():
            raise ValueError("configuration path is unsafe")
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
        return {"id": document_id, "saved": True, "content": content}

    def reload_configuration(self) -> dict[str, object]:
        """Validate fixed resources and notify live adapters to reload retained values."""
        documents = self.configuration(writable=True)["documents"]
        if len(documents) != len(CONFIG_DOCUMENTS) or any(
            not item["content"].strip() for item in documents
        ):
            raise ValueError("fixed configuration is incomplete")
        dashboard_branding(self.root)
        active_sandboxes = self._reload_configuration() if self._reload_configuration else 0
        return {
            "reloaded": True,
            "documents": list(CONFIG_DOCUMENTS),
            "active_sandboxes": active_sandboxes,
        }

    def runtime_configuration(self, *, writable: bool) -> dict[str, object]:
        saved = load_runtime_config(self.root / "config.json")
        current = {field.key: list(field.default) if field.kind in {"list", "multi"} else field.default for field in FIELDS}
        current.update(self.effective_config or {})
        values = dict(current)
        values.update(saved)
        return {
            "writable": writable,
            "restart_required": bool(saved) and values != current,
            "fields": [field.public() for field in FIELDS],
            "values": values,
            "effective": current,
        }

    def save_runtime_configuration(self, value: object) -> dict[str, object]:
        saved = save_runtime_config(self.root / "config.json", value)
        return {"saved": True, "restart_required": True, "values": saved}


def recent_events(root: Path, *, limit: int = 40, sandbox: str | None = None) -> list[dict[str, object]]:
    paths = sorted(
        (root / "runtime").glob("anima.jsonl*"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    events: list[dict[str, object]] = []
    for path in paths:
        if path.suffix == ".gz":
            continue
        for line in reversed(path.read_text(encoding="utf-8", errors="replace").splitlines()):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and (sandbox is None or value.get("sandbox_key") == sandbox):
                events.append(value)
            if len(events) >= limit:
                return events
    return events


def operation_summary(events: list[dict[str, object]]) -> dict[str, object]:
    """Summarize newest-first telemetry without treating stale history as live state."""
    latest_queue = next(
        (event for event in events if event.get("event") in {"persona.queued", "persona.processed"}),
        None,
    )
    latest_maintenance = next(
        (event for event in events if str(event.get("event", "")).startswith("maintenance.")),
        None,
    )
    latest_retry = next((event for event in events if event.get("event") == "operation.retry"), None)
    failure_events = {
        "external.failed", "maintenance.failed", "reaction.failed", "voice.failed", "music.failed"
    }
    latest_api = next(
        (event for event in events if event.get("event") in {
            "openai.request.started", "openai.response.completed", "external.completed",
            *failure_events
        }),
        None,
    )
    latest_usage = next(
        (event for event in events if event.get("event") == "openai.response.completed"),
        None,
    )
    music_activity = []
    for event in events:
        name = str(event.get("event", ""))
        if not (name.startswith("music.") or name.startswith("dj.")):
            continue
        music_activity.append({
            key: event[key]
            for key in ("event", "ts", "title", "track_id", "theme", "reason", "error_type")
            if key in event
        })
        if len(music_activity) >= 8:
            break

    current_event = None
    completed: set[str] = set()
    for event in events:
        event_id = event.get("event_id")
        if not isinstance(event_id, str):
            continue
        if event.get("event") == "persona.processed":
            completed.add(event_id)
        elif event.get("event") == "persona.queued" and event_id not in completed:
            current_event = event_id
            break

    maintenance_name = latest_maintenance.get("event") if latest_maintenance else None
    return {
        "observed_queue_depth": int(latest_queue.get("depth", 0) or 0) if latest_queue else 0,
        "current_event_id": current_event,
        "maintenance": {
            "active": maintenance_name in {"maintenance.queued", "maintenance.started"},
            "event": maintenance_name,
            "operation": latest_maintenance.get("operation") if latest_maintenance else None,
            "at": latest_maintenance.get("ts") if latest_maintenance else None,
        },
        "last_retry": dict(latest_retry) if latest_retry else None,
        "context_usage": {
            operation: next((dict(event) for event in events
                             if event.get("event") == "model.context.measured"
                             and event.get("operation") == operation), None)
            for operation in ("respond", "self_time")
        },
        "music_activity": music_activity,
        "usage_history": sorted([
            {key: event.get(key) for key in (
                "ts", "operation", "model", "input_tokens", "request_count",
            )}
            for event in events
            if event.get("event") == "openai.response.completed"
            and isinstance(event.get("input_tokens"), (int, float))
            and not isinstance(event.get("input_tokens"), bool)
            and event.get("input_tokens", -1) >= 0
            and event.get("ts")
        ][:100], key=lambda item: str(item["ts"])),
        "last_usage": ({
            key: latest_usage.get(key)
            for key in (
                "ts", "operation", "model", "input_tokens", "output_tokens",
                "total_tokens", "request_count",
            )
        } if latest_usage else None),
        "last_openai": {
            "state": (
                "running" if latest_api and latest_api.get("event") == "openai.request.started"
                else "failed" if latest_api and latest_api.get("event") in failure_events
                else "completed" if latest_api else "none"
            ),
            "operation": (
                latest_api.get("operation")
                or str(latest_api.get("event", "")).removesuffix(".failed")
                if latest_api else None
            ),
            "model": latest_api.get("model") if latest_api else None,
            "at": latest_api.get("ts") if latest_api else None,
            "duration_ms": latest_api.get("duration_ms") if latest_api else None,
            "error_type": latest_api.get("error_type") if latest_api else None,
            "reason": latest_api.get("reason") if latest_api else None,
        },
    }


def self_time_summary(
    events: list[dict[str, object]],
    runtime: dict[str, object],
    modes: dict[str, object],
) -> dict[str, object]:
    """Build bounded, operator-readable autonomous activity sessions."""
    active = any(
        isinstance(item, dict)
        and item.get("plugin") == "core"
        and item.get("id") == "self_time"
        for item in modes.get("active", [])
    )
    relevant = []
    for event in reversed(events):
        name = str(event.get("event", ""))
        operation = event.get("operation")
        if not (
            name.startswith("self_time.")
            or (name.startswith("openai.tool.") and operation == "self_time")
        ):
            continue
        relevant.append({
            key: event[key]
            for key in (
                "ts", "event", "action", "iterations", "notes", "reflections",
                "directions", "discoveries", "reason",
                "error_type", "tool_name", "duration_ms", "attachment_count",
            )
            if key in event
        })
    sessions: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    terminal = {"self_time.completed", "self_time.failed", "self_time.interrupted",
                "self_time.cancelled"}
    for item in relevant:
        name = str(item.get("event", ""))
        if name == "self_time.started":
            if current is not None:
                current["state"] = "interrupted"
                sessions.append(current)
            current = {"state": "running", "started_at": item.get("ts"), "tools": []}
        elif name == "self_time.skipped":
            sessions.append({"state": "skipped", "at": item.get("ts"),
                             "reason": item.get("reason"), "tools": []})
        elif name.startswith("openai.tool."):
            if current is None:
                current = {"state": "unknown", "tools": []}
            current["tools"].append(item)
        elif name in terminal:
            if current is None:
                current = {"state": "unknown", "tools": []}
            current.update({
                "state": name.removeprefix("self_time.").replace("cancelled", "interrupted"),
                "completed_at": item.get("ts"),
                **{key: item[key] for key in (
                    "action", "iterations", "notes", "reflections",
                    "directions", "discoveries", "error_type",
                ) if key in item},
            })
            sessions.append(current)
            current = None
    if current is not None:
        if not active:
            current["state"] = "interrupted"
        sessions.append(current)
    sessions = list(reversed(sessions[-8:]))
    latest = sessions[0] if sessions else None
    state = (
        "running" if active
        else str(latest.get("state")) if latest
        else "idle"
    )
    return {
        "state": state,
        "day": runtime.get("day"),
        "starts": int(runtime.get("starts", 0) or 0),
        "last_started_at": runtime.get("last_started_at"),
        "latest": latest,
        "sessions": sessions,
    }


def serve_dashboard(
    root: Path, *, state_root: Path | None = None, host: str = "0.0.0.0", port: int = 8765,
    sandbox: str | None = None,
    policy: ActivityPolicy | None = None, admin_token: str = "", effective_config=None,
    reload_configuration=None,
) -> None:
    handler = _handler(root.resolve(), (state_root or root).resolve(), sandbox=sandbox,
                       policy=policy, admin_token=admin_token, effective_config=effective_config,
                       reload_configuration=reload_configuration)
    server = ThreadingHTTPServer((host, port), handler)
    address = "<LAN-IP>" if host == "0.0.0.0" else host
    print(f"Anima dashboard: http://{address}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


class DashboardServer:
    """Run the read-only dashboard beside the foreground Bot process."""

    def __init__(
        self,
        root: Path,
        *,
        state_root: Path,
        host: str,
        port: int,
        policy: ActivityPolicy,
        admin_token: str = "",
        effective_config=None,
        reload_configuration=None,
    ) -> None:
        handler = _handler(root.resolve(), state_root.resolve(), policy=policy,
                           admin_token=admin_token, effective_config=effective_config,
                           reload_configuration=reload_configuration)
        self.server = ThreadingHTTPServer((host, port), handler)
        self.host, self.port = host, port
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="anima-dashboard",
            daemon=True,
        )

    def start(self) -> None:
        self.thread.start()
        address = "<LAN-IP>" if self.host == "0.0.0.0" else self.host
        print(f"Anima dashboard: http://{address}:{self.port}")

    def stop(self) -> None:
        if self.thread.is_alive():
            self.server.shutdown()
            self.thread.join(timeout=5)
        self.server.server_close()

    def __enter__(self) -> DashboardServer:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()


def _handler(root: Path, state_root: Path, *, sandbox: str | None = None, policy=None,
             admin_token: str = "", effective_config=None, reload_configuration=None):
    if admin_token and len(admin_token) < 16:
        raise ValueError("dashboard admin token must be at least 16 characters")
    data = DashboardData(root, state_root, default=sandbox, policy=policy,
                         effective_config=effective_config,
                         reload_configuration=reload_configuration)
    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            if path in {"/api/sandboxes", "/api/status", "/api/events", "/api/config", "/api/settings"}:
                try:
                    if path == "/api/sandboxes":
                        value = data.scopes()
                    elif path == "/api/config":
                        value = data.configuration(writable=bool(admin_token))
                    elif path == "/api/settings":
                        value = data.runtime_configuration(writable=bool(admin_token))
                    else:
                        query = parse_qs(parsed.query, keep_blank_values=True).get("sandbox", [])
                        if len(query) != 1:
                            raise ValueError("exactly one sandbox is required")
                        value = data.status(query[0]) if path == "/api/status" else data.events(query[0])
                    self._json(value)
                except ValueError:
                    self._json({"error": "表示対象が不正、未指定、または存在しません"}, status=400)
                except OSError:
                    self._json({"error": "表示対象のデータを読み取れません"}, status=503)
                return
            assets = {
                "/": ("index.html", "text/html; charset=utf-8"),
                "/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
                "/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
            }
            asset = assets.get(path)
            if asset is None:
                self.send_error(404)
                return
            body = (ASSET_DIR / asset[0]).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", asset[1])
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_PUT(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/api/settings":
                self._put_settings(admin_token, data)
                return
            prefix = "/api/config/"
            if not parsed.path.startswith(prefix):
                self.send_error(404)
                return
            supplied = self.headers.get("X-Dashboard-Token", "")
            if not admin_token or not hmac.compare_digest(supplied, admin_token):
                self._json({"error": "管理トークンが正しくありません"}, status=403)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 20000:
                    raise ValueError("request size is invalid")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict) or set(payload) != {"content"}:
                    raise ValueError("request body is invalid")
                value = data.save_configuration(parsed.path[len(prefix):], payload["content"])
                self._json(value)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                self._json({"error": "設定内容が不正です"}, status=400)
            except OSError:
                self._json({"error": "設定を保存できません"}, status=503)

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/api/inventory/download":
                if not self._authorized(admin_token):
                    return
                try:
                    payload = self._request_json()
                    if set(payload) != {"sandbox", "artifact_id", "location"}:
                        raise ValueError("request body is invalid")
                    artifact, item = data.inventory_artifact(
                        payload["sandbox"], payload["artifact_id"], payload["location"]
                    )
                    body = artifact.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", item.content_type)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header(
                        "Content-Disposition", f'attachment; filename="{item.id}"'
                    )
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError, FileNotFoundError):
                    self._json({"error": "持ち物が見つからないか指定が不正です"}, status=400)
                except OSError:
                    self._json({"error": "持ち物を読み取れません"}, status=503)
                return
            if path != "/api/config/reload":
                self.send_error(404)
                return
            if not self._authorized(admin_token):
                return
            try:
                self._json(data.reload_configuration())
            except ValueError:
                self._json({"error": "固定設定が不正です"}, status=400)
            except OSError:
                self._json({"error": "設定を再読み込みできません"}, status=503)

        def do_DELETE(self) -> None:  # noqa: N802
            if urlparse(self.path).path != "/api/inventory":
                self.send_error(404)
                return
            if not self._authorized(admin_token):
                return
            try:
                payload = self._request_json()
                if set(payload) != {"sandbox", "artifact_id", "location"}:
                    raise ValueError("request body is invalid")
                self._json(data.delete_inventory_artifact(
                    payload["sandbox"], payload["artifact_id"], payload["location"]
                ))
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, FileNotFoundError):
                self._json({"error": "持ち物が見つからないか指定が不正です"}, status=400)
            except OSError:
                self._json({"error": "持ち物を削除できません"}, status=503)

        def _authorized(self, admin_token: str) -> bool:
            supplied = self.headers.get("X-Dashboard-Token", "")
            if admin_token and hmac.compare_digest(supplied, admin_token):
                return True
            self._json({"error": "管理トークンが正しくありません"}, status=403)
            return False

        def _request_json(self) -> dict[str, object]:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 20000:
                raise ValueError("request size is invalid")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("request body is invalid")
            return payload

        def _put_settings(self, admin_token: str, data: DashboardData) -> None:
            supplied = self.headers.get("X-Dashboard-Token", "")
            if not admin_token or not hmac.compare_digest(supplied, admin_token):
                self._json({"error": "管理トークンが正しくありません"}, status=403)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 30000:
                    raise ValueError("request size is invalid")
                payload = json.loads(self.rfile.read(length))
                value = data.save_runtime_configuration(payload)
                self._json(value)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RuntimeConfigError):
                self._json({"error": "運用設定が不正です"}, status=400)
            except OSError:
                self._json({"error": "運用設定を保存できません"}, status=503)

        def _json(self, value: object, *, status=200) -> None:
            body = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    return DashboardHandler
