"""Sandbox- and plugin-bound persistence contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class PluginStorage(Protocol):
    """Restricted state and artifact storage exposed to one plugin instance."""

    def read_json(self, name: str, default: object) -> object: ...
    def write_json(self, name: str, value: object) -> None: ...
    def artifact_path(self, category: str, name: str) -> Path: ...
    def temporary_path(self, name: str) -> Path: ...
    def clear_temporary(self) -> None: ...


@runtime_checkable
class PluginStorageFactory(Protocol):
    """Create storage already bound to one safe plugin identifier."""

    def for_plugin(self, plugin_id: str) -> PluginStorage: ...
