"""Operational command line tools for a headless persona-agent installation."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import sys
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo
from openai import AsyncOpenAI

from anima.bootstrap.settings import ConfigurationError, Settings, load_dotenv, _guild_ids, _parse_bool
from anima.core.access import ActivityPolicy
from anima.adapters.openai.memory_vector_store import MemoryVectorStore, memory_vector_status
from anima.core.models import Event
from anima.bootstrap.settings import KNOWN_PLUGINS
from anima.core.state import FileStateStore
from anima.core.memory_audit import MemoryAuditor
from anima.core.sandbox import SandboxKey, legacy_entries, list_sandboxes


JST = ZoneInfo("Asia/Tokyo")
SAFE_DASHBOARD_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="anima")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for operation in ("backup", "restore"):
        snapshot_parser = subparsers.add_parser(operation, help="offline sandbox snapshot maintenance")
        snapshot_parser.add_argument("archive", type=Path)
        snapshot_parser.add_argument("--sandbox", required=True)
    subparsers.add_parser(
        "run", help="run AivisSpeech, Bot, and Dashboard together in the foreground"
    )
    stub_parser = subparsers.add_parser(
        "discord-stub", help="run one Discord-shaped model turn without Discord"
    )
    stub_parser.add_argument("text", help="message body presented to the persona")
    stub_parser.add_argument(
        "--attachment", action="append", type=Path, default=[],
        help="local file to present as a Discord attachment; repeatable",
    )
    stub_parser.add_argument("--guild-id", default="1")
    stub_parser.add_argument(
        "--state-root", type=Path, default=Path("/tmp/anima-discord-stub")
    )
    status_parser = subparsers.add_parser("status", help="show local runtime status")
    status_parser.add_argument("--json", action="store_true", dest="as_json")
    doctor_parser = subparsers.add_parser("doctor", help="check configuration and state")
    doctor_parser.add_argument("--online", action="store_true")
    doctor_parser.add_argument("--json", action="store_true", dest="as_json")
    prune_parser = subparsers.add_parser("prune", help="archive expired processed logs")
    prune_parser.add_argument("--json", action="store_true", dest="as_json")
    dashboard_parser = subparsers.add_parser(
        "dashboard", help="serve the read-only LAN operations dashboard"
    )
    dashboard_parser.add_argument("--host", choices=("0.0.0.0", "127.0.0.1"))
    dashboard_parser.add_argument("--port", type=int, default=8765)
    memory_index_parser = subparsers.add_parser(
        "memory-index", help="inspect or maintain a sandbox memory vector store"
    )
    memory_index_parser.add_argument(
        "action", choices=("status", "sync", "rebuild", "prune")
    )
    memory_index_parser.add_argument(
        "--apply", action="store_true", help="delete prune candidates; otherwise dry-run"
    )
    memory_index_parser.add_argument("--json", action="store_true", dest="as_json")
    memory_audit_parser = subparsers.add_parser(
        "memory-audit", help="inspect durable memory for drift and contamination"
    )
    memory_audit_parser.add_argument(
        "action", choices=("status", "baseline"), default="status", nargs="?"
    )
    memory_audit_parser.add_argument("--json", action="store_true", dest="as_json")
    subparsers.add_parser("sandboxes", help="list sandbox roots and unassigned legacy state")
    for scoped_parser in (
        status_parser, doctor_parser, prune_parser, dashboard_parser, memory_index_parser,
        memory_audit_parser,
    ):
        scoped_parser.add_argument("--sandbox", help="guild:<ID> or dm:<user ID>")
    from anima.capabilities.plugin_loader import PluginLoader
    for definition in PluginLoader().load().definitions:
        register = getattr(definition, "register_cli", None)
        if register is not None:
            register(subparsers)
    args = parser.parse_args(argv)

    if args.command == "run":
        from anima.bootstrap.runner import run_local

        run_local()
        return

    if args.command == "discord-stub":
        from anima.bootstrap.discord_stub import run_discord_stub

        settings = Settings.load()
        _print(asyncio.run(run_discord_stub(
            settings,
            args.text,
            state_root=args.state_root,
            guild_id=args.guild_id,
            attachments=tuple(args.attachment),
        )), True)
        return

    values, root, state_root = _environment()
    handler = getattr(args, "_plugin_handler", None)
    if handler is not None:
        handler(args, values, root, state_root, _print)
        return
    if args.command in {"backup", "restore"}:
        from anima.adapters.storage.backup import backup, restore
        from anima.bootstrap.process_guard import ProcessLock

        try:
            key = SandboxKey.parse(args.sandbox)
            with ProcessLock(state_root / "runtime" / "bot.lock"):
                operation = backup if args.command == "backup" else restore
                _print(operation(state_root, key, args.archive), True)
        except (OSError, ValueError, RuntimeError) as error:
            parser.error(str(error))
        return
    process_root = state_root
    if args.command == "sandboxes":
        _print({"sandboxes": [str(key) for key in list_sandboxes(state_root)],
                "unassigned_legacy": legacy_entries(state_root)}, True)
        return
    selected = getattr(args, "sandbox", None)
    if selected:
        try:
            state_root = SandboxKey.parse(selected).path(process_root)
        except ValueError as error:
            parser.error(str(error))
        if not state_root.is_dir():
            parser.error("sandbox does not exist")
    elif args.command in {"prune", "doctor", "memory-index", "memory-audit"}:
        parser.error("--sandbox is required; use anima sandboxes to list IDs")
    if args.command == "status" and not selected:
        _print({"sandboxes": [str(key) for key in list_sandboxes(state_root)],
                "unassigned_legacy": legacy_entries(state_root),
                "process": _read_json(state_root / "runtime" / "status.json")}, args.as_json)
        return
    if args.command == "status":
        result = collect_status(root, state_root=process_root, sandbox=selected)
        _print(result, args.as_json)
        return
    if args.command == "doctor":
        checks = run_doctor(root, values, state_root=state_root, online=args.online)
        _print_checks(checks, args.as_json)
        if any(item["status"] == "error" for item in checks):
            raise SystemExit(1)
        return
    if args.command == "prune":
        store = FileStateStore(
            state_root,
            sandbox_key=SandboxKey.parse(selected),
            resource_root=root,
            log_retention_days=_integer(values, "ANIMA_LOG_RETENTION_DAYS", 30),
            archive_retention_days=_integer(values, "ANIMA_ARCHIVE_RETENTION_DAYS", 365),
        )
        store._check_tree()
        result = store.archive_old_logs(now=datetime.now(JST))
        _print(result, args.as_json)
        return
    if args.command == "memory-audit":
        auditor = MemoryAuditor(state_root)
        baseline_path = state_root / "runtime" / "memory-audit-baseline.json"
        report = auditor.run()
        previous = _read_json(baseline_path).get("hashes", {})
        report["changes"] = auditor.compare(previous if isinstance(previous, dict) else {})
        if args.action == "baseline":
            baseline_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = baseline_path.with_suffix(".tmp")
            temporary.write_text(json.dumps({
                "created_at": datetime.now(JST).isoformat(), "hashes": report["hashes"],
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(baseline_path)
            report["baseline_updated"] = True
        _print(report, args.as_json)
        return
    if args.command == "memory-index":
        if args.apply and args.action != "prune":
            parser.error("--apply is only valid with memory-index prune")
        if args.action == "status":
            _print(memory_vector_status(state_root, sandbox_key=selected), args.as_json)
            return
        process_status = _read_json(process_root / "runtime" / "status.json")
        process_pid = process_status.get("pid")
        if isinstance(process_pid, int) and _pid_exists(process_pid):
            parser.error("stop the Bot before changing the memory index")
        api_key = values.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            parser.error("OPENAI_API_KEY is required")
        memory_index = MemoryVectorStore(
            state_root,
            AsyncOpenAI(api_key=api_key, max_retries=0),
            sandbox_key=selected,
        )
        try:
            if args.action == "sync":
                vector_store_id = asyncio.run(memory_index.ensure())
                result = {
                    **memory_vector_status(state_root, sandbox_key=selected),
                    "vector_store_id": vector_store_id,
                }
            elif args.action == "rebuild":
                vector_store_id = asyncio.run(memory_index.rebuild())
                result = {
                    **memory_vector_status(state_root, sandbox_key=selected),
                    "vector_store_id": vector_store_id,
                }
            else:
                result = asyncio.run(memory_index.prune_orphans(apply=args.apply))
        except Exception as error:
            print(f"memory index {args.action} failed: {error}", file=sys.stderr)
            raise SystemExit(1) from error
        _print(result, args.as_json)
        return
    if args.command == "dashboard":
        if not 1 <= args.port <= 65535:
            raise SystemExit("--port must be between 1 and 65535")
        from anima.adapters.dashboard.server import serve_dashboard

        serve_dashboard(root, state_root=process_root, sandbox=selected,
                        host=args.host or values.get("ANIMA_DASHBOARD_HOST", "127.0.0.1"), port=args.port,
                        policy=ActivityPolicy(_guild_ids(values.get("ANIMA_ALLOWED_GUILD_IDS", "")),
                                              _parse_bool(values.get("ANIMA_DM_ENABLED", "false"))),
                        admin_token=values.get("ANIMA_DASHBOARD_ADMIN_TOKEN", "").strip())
        return


def collect_status(root: Path, *, state_root: Path | None = None, sandbox: str | None = None) -> dict[str, object]:
    state = state_root or root
    process_root = state
    if sandbox is not None:
        state = SandboxKey.parse(sandbox).path(process_root)
        FileStateStore(state, sandbox_key=SandboxKey.parse(sandbox))._check_tree()
    cursor = _read_json(state / "cursor.json")
    mood = _read_json(state / "mood.md")
    runtime = _read_json(state / "runtime" / "status.json")
    if sandbox is not None:
        runtime.update(_read_json(process_root / "runtime" / "status.json"))
    features = runtime.get("features")
    if not isinstance(features, dict):
        features = {}
    voice = runtime.get("voice")
    if not isinstance(voice, dict):
        key = SandboxKey.parse(sandbox) if sandbox is not None else None
        voice = {
            "enabled": bool(
                key is not None
                and key.kind == "guild"
                and features.get("voice_enabled")
            ),
            "initialized": False,
            "connected": False,
            "channel_id": None,
            "playing_event_id": None,
            "queue_depth": 0,
        }
    else:
        voice = {**voice, "initialized": True}
    pid = runtime.get("pid")
    running = bool(runtime.get("connected") and isinstance(pid, int) and _pid_exists(pid))
    plugin_runtime = _read_json(state / "runtime" / "plugins.json").get("plugins", [])
    if not isinstance(plugin_runtime, list):
        plugin_runtime = []
    configured_plugins = set(features.get("plugins", []))
    plugin_by_name = {
        value.get("name"): value for value in plugin_runtime
        if isinstance(value, dict) and isinstance(value.get("name"), str)
    }
    plugins = []
    dashboard_panels = []
    for name in sorted(KNOWN_PLUGINS):
        current = plugin_by_name.get(name, {})
        enabled = name in configured_plugins
        panels = _dashboard_panels(current.get("dashboard_panels"), plugin=name)
        plugins.append({
            "name": name,
            "enabled": enabled,
            "available": bool(current.get("available")),
            "running": bool(current.get("running")) and running,
            "version": current.get("version"),
            "provides": current.get("provides", []),
            "description": current.get("description", ""),
            "configuration_fields": current.get("configuration_fields", []),
            "dashboard_panels": panels,
        })
        if enabled:
            dashboard_panels.extend(panels)
    dashboard_panels.sort(key=lambda item: (item["order"], item["id"]))
    store = FileStateStore(state, resource_root=root)
    pending = store.pending_addressed_events()
    failures = store.event_failures()
    log_files = list((state / "log").glob("*/*.jsonl")) if (state / "log").exists() else []
    handled_files = (
        list((state / "handled").glob("*/*.txt")) if (state / "handled").exists() else []
    )
    memory_files = list((state / "memory").glob("**/*.md")) if (state / "memory").exists() else []
    archive_files = (
        list((state / "archive" / "log").glob("*/*.jsonl.gz"))
        if (state / "archive" / "log").exists()
        else []
    )
    usage = _usage_summary(process_root / "runtime", sandbox=sandbox)
    reflection = _reflection_summary(process_root / "runtime", sandbox=sandbox)
    habitus_path = state / "habitus.md"
    habitus = (
        [line[2:] for line in habitus_path.read_text(encoding="utf-8").splitlines() if line.startswith("- ")]
        if habitus_path.exists()
        else []
    )
    return {
        "root": str(root),
        "state_root": str(state),
        "sandbox_key": sandbox,
        "reactions": _read_json(state / "runtime" / "reactions.json"),
        "bot": {
            "running": running,
            "connected": bool(runtime.get("connected")),
            "pid": pid,
            "user": runtime.get("user"),
            "updated_at": runtime.get("updated_at"),
            "gateway_ping_ms": runtime.get("gateway_ping_ms"),
            "last_unclean_shutdown_at": runtime.get("last_unclean_shutdown_at"),
            "last_unclean_shutdown_pid": runtime.get("last_unclean_shutdown_pid"),
        },
        "state": {
            "version": cursor.get("version"),
            "last_spoke_at": cursor.get("last_spoke_at"),
            "slept_at": cursor.get("slept_at"),
            "digested_until": cursor.get("digested_until"),
            "reflected_at": cursor.get("reflected_at"),
            "mood": mood.get("state"),
            "focus": mood.get("focus"),
            "habitus": habitus,
            "reflection": reflection,
            "research": _latest_research(log_files),
        },
        "queue": {
            "pending": len(pending),
            "failures": [
                failures[event.id]
                for event in pending
                if event.id in failures
            ],
        },
        "actor": _read_json(state / "runtime" / "actor.json"),
        "voice": voice,
        "music": runtime.get("music", {"enabled": False}),
        "reminders": _read_json(state / "runtime" / "reminders.json").get("reminders", []),
        "memory_vector_store": (
            memory_vector_status(state, sandbox_key=sandbox) if sandbox is not None else None
        ),
        "plugins": plugins,
        "dashboard": {"panels": dashboard_panels},
        "storage": {
            "active_log_files": len(log_files),
            "active_log_bytes": sum(path.stat().st_size for path in log_files),
            "handled_files": len(handled_files),
            "memory_files": len(memory_files),
            "memory_lines": sum(_line_count(path) for path in memory_files),
            "archive_files": len(archive_files),
            "archive_bytes": sum(path.stat().st_size for path in archive_files),
        },
        "openai_usage": usage,
    }


def _dashboard_panels(value: object, *, plugin: str) -> list[dict[str, object]]:
    """Read only the bounded declarative panel fields written by the Registry."""
    if not isinstance(value, list):
        return []
    panels = []
    for item in value[:16]:
        if not isinstance(item, dict):
            continue
        panel_id, title, renderer, order, description, eyebrow, group = (
            item.get("id"), item.get("title"), item.get("renderer"), item.get("order"),
            item.get("description", ""), item.get("eyebrow", "PLUGIN"), item.get("group", "status"),
        )
        if not all(isinstance(part, str) for part in (panel_id, title, renderer, description, eyebrow, group)):
            continue
        if group not in {"status", "memory", "configuration", "diagnostics"}:
            continue
        if not isinstance(order, int):
            continue
        if not SAFE_DASHBOARD_ID.fullmatch(panel_id) or not SAFE_DASHBOARD_ID.fullmatch(renderer):
            continue
        if (not title.strip() or len(title) > 80 or len(description) > 240
                or not eyebrow.strip() or len(eyebrow) > 80 or not 0 <= order <= 1000):
            continue
        panels.append({
            "id": panel_id,
            "title": title,
            "renderer": renderer,
            "order": order,
            "plugin": plugin,
            "description": description,
            "eyebrow": eyebrow,
            "group": group,
        })
    return panels


def _latest_research(log_files: list[Path]) -> dict[str, object] | None:
    for path in sorted(log_files, key=lambda item: item.stat().st_mtime, reverse=True):
        for line in reversed(path.read_text(encoding="utf-8", errors="replace").splitlines()):
            try:
                event = Event.from_log_dict(json.loads(line))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if event.research is not None:
                return {
                    "event_id": event.id,
                    "searched_at": event.ts.isoformat(),
                    **event.research.to_dict(),
                }
    return None


def run_doctor(
    root: Path,
    values: dict[str, str],
    *,
    state_root: Path | None = None,
    online: bool = False,
) -> list[dict[str, str]]:
    state = state_root or root
    checks: list[dict[str, str]] = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append({"name": name, "status": status, "detail": detail})

    for key in ("DISCORD_BOT_TOKEN", "OPENAI_API_KEY"):
        value = values.get(key, "").strip()
        valid = bool(value and value.lower() not in {"replace-me", "changeme"})
        add(key, "ok" if valid else "error", "configured" if valid else "missing or placeholder")
    add("root", "ok" if root.exists() else "error", str(root))
    add("root_writable", "ok" if os.access(root, os.W_OK) else "error", str(root))
    add("state_root", "ok" if state.exists() else "error", str(state))
    add("state_writable", "ok" if os.access(state, os.W_OK) else "error", str(state))
    for name in ("persona.md", "rules.md", "appearance.md"):
        path = root / name
        add(name, "ok" if path.is_file() and path.stat().st_size else "error", str(path))
    for name in ("cursor.json", "mood.md"):
        path = state / name
        try:
            _read_json(path, required=True)
        except (OSError, ValueError) as error:
            add(name, "error", str(error))
        else:
            add(name, "ok", "valid JSON")
    transaction = state / ".anima-transaction.json"
    add(
        "transaction",
        "warning" if transaction.exists() else "ok",
        "recovery pending" if transaction.exists() else "none",
    )
    try:
        pending = len(FileStateStore(state, resource_root=root).pending_addressed_events())
    except Exception as error:
        add("event_logs", "error", str(error))
    else:
        add("event_logs", "ok", f"{pending} pending")
    if online:
        _online_check(
            checks,
            "discord_api",
            "https://discord.com/api/v10/users/@me",
            {"Authorization": f"Bot {values.get('DISCORD_BOT_TOKEN', '')}"},
        )
        _online_check(
            checks,
            "openai_api",
            "https://api.openai.com/v1/models",
            {"Authorization": f"Bearer {values.get('OPENAI_API_KEY', '')}"},
        )
    from anima.capabilities.plugin_loader import PluginLoader
    from anima.bootstrap.settings import selected_plugins
    enabled = selected_plugins(values)
    for definition in PluginLoader().load().definitions:
        callback = getattr(definition, "doctor_checks", None)
        if definition.manifest.name in enabled and callback is not None:
            callback(root, values, online, checks, _online_check)
    return checks


def _online_check(
    checks: list[dict[str, str]], name: str, url: str, headers: dict[str, str]
) -> None:
    request_headers = {"User-Agent": "anima/0.1", **headers}
    try:
        response = urllib.request.urlopen(
            urllib.request.Request(url, headers=request_headers), timeout=15
        )
    except (urllib.error.URLError, TimeoutError) as error:
        checks.append({"name": name, "status": "error", "detail": str(error)})
    else:
        checks.append({"name": name, "status": "ok", "detail": f"HTTP {response.status}"})
        response.close()


def _usage_summary(runtime: Path, *, sandbox: str | None = None) -> dict[str, object]:
    totals: dict[str, object] = {
        "requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "by_operation": {},
        "by_model": {},
        "by_day": {},
    }
    if not runtime.exists():
        return totals
    for path in runtime.glob("anima.jsonl*"):
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if sandbox is not None and value.get("sandbox_key") != sandbox:
                    continue
                if value.get("event") != "openai.response.completed":
                    if value.get("event") == "drawing.saved" or (
                        value.get("event") == "image_generation.completed"
                        and value.get("operation", "image_generation") == "image_generation"
                    ):
                        images = int(value.get("image_count", 0) or 0)
                        if images <= 0:
                            continue
                        for group, name in (
                            ("by_operation", str(value.get("operation") or "image_generation")),
                            ("by_model", str(value.get("model") or "unknown")),
                            ("by_day", str(value.get("ts") or "unknown")[:10]),
                        ):
                            bucket = totals[group]
                            assert isinstance(bucket, dict)
                            entry = bucket.setdefault(
                                name, {"requests": 0, "total_tokens": 0, "image_count": 0}
                            )
                            entry["image_count"] = int(entry.get("image_count", 0)) + images
                    continue
                totals["requests"] = int(totals["requests"]) + 1
                for key in ("input_tokens", "output_tokens", "total_tokens"):
                    totals[key] = int(totals[key]) + int(value.get(key, 0) or 0)
                tokens = int(value.get("total_tokens", 0) or 0)
                for group, name in (
                    ("by_operation", str(value.get("operation") or "unknown")),
                    ("by_model", str(value.get("model") or "unknown")),
                    ("by_day", str(value.get("ts") or "unknown")[:10]),
                ):
                    bucket = totals[group]
                    assert isinstance(bucket, dict)
                    entry = bucket.setdefault(name, {"requests": 0, "total_tokens": 0})
                    entry["requests"] += 1
                    entry["total_tokens"] += tokens
    return totals


def _reflection_summary(runtime: Path, *, sandbox: str | None = None) -> dict[str, object]:
    result: dict[str, object] = {"changed": None, "conflict": None, "completed_at": None}
    if not runtime.exists():
        return result
    candidates: list[dict[str, object]] = []
    for path in runtime.glob("anima.jsonl*"):
        if path.suffix == ".gz":
            continue
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    isinstance(value, dict)
                    and (sandbox is None or value.get("sandbox_key") == sandbox)
                    and value.get("event") == "maintenance.completed"
                    and value.get("operation") == "reflection"
                ):
                    candidates.append(value)
    if not candidates:
        return result
    latest = max(candidates, key=lambda value: str(value.get("ts", "")))
    return {
        "changed": latest.get("changed"),
        "conflict": latest.get("conflict"),
        "completed_at": latest.get("ts"),
    }


def _environment() -> tuple[dict[str, str], Path, Path]:
    cwd = Path.cwd().resolve()
    from anima.bootstrap.deployment import deployment_defaults
    values = dict(os.environ)
    try:
        load_dotenv(cwd / ".env", environ=values)
    except ConfigurationError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
    values = {**deployment_defaults(cwd), **values}
    root_value = Path(values.get("ANIMA_ROOT", "config"))
    root = root_value if root_value.is_absolute() else cwd / root_value
    state_value = Path(values.get("ANIMA_STATE_ROOT", "../state"))
    state = state_value if state_value.is_absolute() else root / state_value
    return values, root.resolve(), state.resolve()


def _read_json(path: Path, *, required: bool = False) -> dict[str, object]:
    if not path.exists():
        if required:
            raise FileNotFoundError(path)
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, ValueError):
        return False
    return True


def _line_count(path: Path) -> int:
    with path.open(encoding="utf-8") as stream:
        return sum(1 for line in stream if line.strip())


def _integer(values: dict[str, str], key: str, default: int) -> int:
    try:
        value = int(values.get(key, str(default)))
    except ValueError as error:
        raise SystemExit(f"{key} must be an integer") from error
    if value < 1:
        raise SystemExit(f"{key} must be at least 1")
    return value


def _print(value: dict[str, object], as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return
    for section, content in value.items():
        if isinstance(content, dict):
            print(f"{section}:")
            for key, item in content.items():
                print(f"  {key}: {item}")
        else:
            print(f"{section}: {content}")


def _print_checks(checks: list[dict[str, str]], as_json: bool) -> None:
    if as_json:
        print(json.dumps(checks, ensure_ascii=False, indent=2))
        return
    for check in checks:
        print(f"[{check['status'].upper():7}] {check['name']}: {check['detail']}")


if __name__ == "__main__":
    main()
