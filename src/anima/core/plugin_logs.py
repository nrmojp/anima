"""Sandbox-local plugin diagnostics, separate from public operational traces."""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from anima.core.sandbox import SandboxKey, reject_symlinks
from anima.core.telemetry import emit, sandbox_context

LOGGER = logging.getLogger("anima.plugin_details")
LOGGER.propagate = False


def sanitize(value, key=""):
    if re.search(r"secret|token|password|authorization|api_key|b64|base64|headers", key, re.I):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(k): sanitize(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(v) for v in value[:100]]
    if isinstance(value, str):
        return re.sub(r"sk-[\w-]+|Bearer\s+\S+|data:[^\s]+", "[redacted]", value)[:16000]
    return value


def plugin_log(plugin: str, event: str, *, sandbox_key=None, **details):
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", plugin):
        raise ValueError("invalid plugin log name")
    sandbox = sandbox_key or sandbox_context.get()
    if sandbox is None:
        return
    sandbox = str(SandboxKey.parse(str(sandbox)))
    identifier = uuid.uuid4().hex
    record = dict(sanitize(details), ts=datetime.now(timezone.utc).isoformat(),
                  plugin=plugin, event=event, sandbox_key=sandbox, log_id=identifier)
    LOGGER.info(json.dumps(record, ensure_ascii=False, default=str))
    emit("plugin.log", plugin=plugin, log_id=identifier, detail_event=event,
         sandbox_key=sandbox, event_id=details.get("event_id"))


class PluginLogHandler(logging.Handler):
    def __init__(self, root: Path, retention_days: int, plugins=()):
        super().__init__()
        if retention_days <= 0:
            raise ValueError("plugin log retention must be positive")
        self.root, self.retention_days = root, retention_days
        self.plugins = frozenset(plugins)

    def emit(self, record):
        try:
            value = json.loads(record.getMessage())
            if record.name == "anima.telemetry":
                plugin = value.get("plugin") or str(value.get("event", "")).split(".")[0]
                if plugin not in self.plugins or not value.get("sandbox_key") or value.get("event") == "plugin.log":
                    return
                value["plugin"] = plugin
            plugin = value["plugin"]
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", plugin):
                raise ValueError("invalid plugin")
            root = SandboxKey.parse(value["sandbox_key"]).path(self.root)
            directory = root / "runtime/plugins" / plugin
            reject_symlinks(directory, self.root)
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            now = datetime.now(timezone.utc)
            path = directory / f"{now:%Y-%m-%d}.jsonl"
            reject_symlinks(path, self.root)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(sanitize(value), ensure_ascii=False) + "\n")
            path.chmod(0o600)
            cutoff = (now - timedelta(days=self.retention_days)).date().isoformat()
            for old in directory.glob("*.jsonl"):
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", old.stem) and old.stem < cutoff:
                    reject_symlinks(old, self.root)
                    old.unlink()
        except (OSError, ValueError, KeyError, TypeError):
            logging.getLogger(__name__).error("Plugin diagnostic log could not be saved")


def read_plugin_logs(root: Path, *, limit=50):
    directory = root / "runtime/plugins"
    reject_symlinks(directory, root)
    records = []
    for path in sorted(directory.glob("*/*.jsonl"), reverse=True)[:500]:
        reject_symlinks(path, root)
        with path.open(encoding="utf-8") as stream:
            # Bound dashboard memory even when a plugin is noisy.
            from collections import deque
            lines = deque(stream, maxlen=limit)
        for line in lines:
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    records.append(value)
            except ValueError:
                continue
    return sorted(records, key=lambda item: str(item.get("ts", "")), reverse=True)[:limit]
