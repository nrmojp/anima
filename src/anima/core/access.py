"""Installation admission and persistent per-sandbox activity modes."""

from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path

from anima.core.sandbox import SandboxKey, reject_symlinks


ACTIVITY_MODES = ("silent", "reply", "react", "proactive")
_MODE_LEVEL = {mode: level for level, mode in enumerate(ACTIVITY_MODES)}


@dataclass(frozen=True)
class ActivityPolicy:
    allowed_guild_ids: frozenset[str] = frozenset()
    dm_enabled: bool = False
    allowed_sandbox_keys: frozenset[SandboxKey] = frozenset()

    def allows(self, key: SandboxKey) -> bool:
        if key.kind == "guild":
            return key.id in self.allowed_guild_ids
        if key.kind == "dm":
            return self.dm_enabled
        return key in self.allowed_sandbox_keys


class ActivityModeStore:
    """Persist one cumulative activity mode per guild; missing state defaults to reply."""

    def __init__(self, state_root: Path | None = None) -> None:
        self.state_root = state_root
        self._memory: dict[SandboxKey, dict[str, str]] = {}

    def get(self, key: SandboxKey) -> str:
        if key.kind == "dm":
            return "reply"
        value = self.details(key)
        return str(value["mode"])

    def details(self, key: SandboxKey) -> dict[str, str | None]:
        if key.kind == "dm":
            return {"mode": "reply", "changed_by": None, "changed_at": None}
        if self.state_root is None:
            return dict(self._memory.get(key, {"mode": "reply", "changed_by": None, "changed_at": None}))
        path = self._path(key)
        if not path.exists():
            return {"mode": "reply", "changed_by": None, "changed_at": None}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("mode") not in ACTIVITY_MODES:
                raise ValueError("invalid activity mode state")
            return {
                "mode": value["mode"],
                "changed_by": value.get("changed_by") if isinstance(value.get("changed_by"), str) else None,
                "changed_at": value.get("changed_at") if isinstance(value.get("changed_at"), str) else None,
            }
        except (OSError, ValueError, json.JSONDecodeError):
            return {"mode": "silent", "changed_by": None, "changed_at": None}

    def set(self, key: SandboxKey, mode: str, *, changed_by: str, changed_at: datetime) -> dict[str, str]:
        if key.kind != "guild":
            raise ValueError("activity mode is only configurable for guilds")
        if mode not in ACTIVITY_MODES:
            raise ValueError("unknown activity mode")
        value = {
            "mode": mode,
            "changed_by": changed_by,
            "changed_at": changed_at.isoformat(),
        }
        if self.state_root is None:
            self._memory[key] = value
            return dict(value)
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
        return dict(value)

    def permits(self, key: SandboxKey, capability: str) -> bool:
        if capability not in _MODE_LEVEL:
            raise ValueError("unknown activity capability")
        return _MODE_LEVEL[self.get(key)] >= _MODE_LEVEL[capability]

    def _path(self, key: SandboxKey) -> Path:
        assert self.state_root is not None
        path = key.path(self.state_root) / "settings" / "activity.json"
        reject_symlinks(path, self.state_root)
        return path
