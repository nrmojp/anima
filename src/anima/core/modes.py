"""Observable, plugin-owned persistent activity modes."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Protocol, runtime_checkable


SAFE_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


@dataclass(frozen=True, slots=True)
class ModeState:
    """One externally verifiable activity reported by its owning plugin."""

    plugin: str
    id: str
    label: str
    detail: str = ""
    started_at: str | None = None

    def __post_init__(self) -> None:
        if not SAFE_ID.fullmatch(self.plugin) or not SAFE_ID.fullmatch(self.id):
            raise ValueError("mode plugin and ID must be safe identifiers")
        if not self.label.strip() or len(self.label) > 120 or "\n" in self.label:
            raise ValueError("mode label must be a non-empty short line")
        if len(self.detail) > 500 or "\n" in self.detail or "\r" in self.detail:
            raise ValueError("mode detail must be a short line")
        if self.started_at is not None:
            value = datetime.fromisoformat(self.started_at)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("mode started_at must include a timezone")

    @property
    def key(self) -> str:
        return f"{self.plugin}:{self.id}"

    def to_dict(self) -> dict[str, object]:
        return {
            "plugin": self.plugin, "id": self.id, "label": self.label,
            "detail": self.detail, "started_at": self.started_at,
        }


@runtime_checkable
class ModeProvider(Protocol):
    def modes(self) -> tuple[ModeState, ...]: ...


class CallableModeProvider:
    def __init__(self, callback: Callable[[], Sequence[ModeState]]) -> None:
        self.callback = callback

    def modes(self) -> tuple[ModeState, ...]:
        values = tuple(self.callback())
        if any(not isinstance(value, ModeState) for value in values):
            raise TypeError("mode provider returned an invalid mode")
        return values


class ModeRegistry:
    """Aggregate modes and keep a bounded transition journal for one sandbox."""

    def __init__(
        self, path: Path, providers: Sequence[ModeProvider] = (), *, history_limit: int = 200,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if history_limit <= 0:
            raise ValueError("mode history limit must be positive")
        if any(not isinstance(provider, ModeProvider) for provider in providers):
            raise TypeError("mode providers must implement ModeProvider")
        self.path = path
        self.providers = tuple(providers)
        self.history_limit = history_limit
        self.clock = clock or (lambda: datetime.now().astimezone())
        self._current, self._history = self._load()

    def replace_providers(self, providers: Sequence[ModeProvider]) -> None:
        if any(not isinstance(provider, ModeProvider) for provider in providers):
            raise TypeError("mode providers must implement ModeProvider")
        self.providers = tuple(providers)

    def refresh(self) -> tuple[ModeState, ...]:
        observed: dict[str, ModeState] = {}
        for provider in self.providers:
            for mode in provider.modes():
                if mode.key in observed:
                    raise ValueError(f"duplicate mode: {mode.key}")
                observed[mode.key] = mode
        now = self.clock().isoformat()
        for key, mode in observed.items():
            previous = self._current.get(key)
            transition = "started" if previous is None else "continued"
            if previous != mode:
                self._history.append({"at": now, "transition": transition, **mode.to_dict()})
        for key, previous in self._current.items():
            if key not in observed:
                self._history.append({"at": now, "transition": "ended", **previous.to_dict()})
        changed = observed != self._current
        self._current = observed
        if changed:
            self._write()
        return tuple(sorted(observed.values(), key=lambda item: item.key))

    def snapshot(self) -> Mapping[str, object]:
        current = self.refresh()
        return {
            "active": tuple(item.to_dict() for item in current),
            "history": tuple(self._history[-self.history_limit:]),
        }

    def summary(self) -> str:
        current = self.refresh()
        if not current:
            return "いま外から確認できる継続中の活動はない。"
        return "\n".join(
            f"- {item.label}" + (f": {item.detail}" if item.detail else "")
            for item in current
        )

    def _load(self) -> tuple[dict[str, ModeState], list[dict[str, object]]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            current = {
                ModeState(**item).key: ModeState(**item)
                for item in data.get("active", [])
            }
            history = [dict(item) for item in data.get("history", []) if isinstance(item, dict)]
            return current, history[-self.history_limit:]
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return {}, []

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({
            "active": [item.to_dict() for item in self._current.values()],
            "history": self._history[-self.history_limit:],
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)
        self._history = self._history[-self.history_limit:]
