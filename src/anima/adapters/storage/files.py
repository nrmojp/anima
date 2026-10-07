"""Safe filesystem-backed plugin state and artifact storage."""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
from tempfile import NamedTemporaryFile

from anima.core.sandbox import SandboxKey


_SAFE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
_PLUGIN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class FilePluginStorage:
    def __init__(
        self, state_directory: Path, artifact_directory: Path,
        temporary_directory: Path | None = None,
    ) -> None:
        self.state_directory = state_directory
        self.artifact_directory = artifact_directory
        self.temporary_directory = temporary_directory or state_directory / "tmp"

    def read_json(self, name: str, default: object) -> object:
        path = self._state_path(name)
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    def write_json(self, name: str, value: object) -> None:
        path = self._state_path(name)
        encoded = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.",
            suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
            stream.flush()
        temporary.replace(path)

    def artifact_path(self, category: str, name: str) -> Path:
        category = _safe_name(category, "artifact category")
        name = _safe_name(name, "artifact name")
        directory = self.artifact_directory / category
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        if path.is_symlink():
            raise ValueError("artifact path must not be a symbolic link")
        return path

    def temporary_path(self, name: str) -> Path:
        name = _safe_name(name, "temporary name")
        self.temporary_directory.mkdir(parents=True, exist_ok=True)
        path = self.temporary_directory / name
        if path.is_symlink():
            raise ValueError("temporary path must not be a symbolic link")
        return path

    def clear_temporary(self) -> None:
        if self.temporary_directory.is_symlink():
            raise ValueError("temporary directory must not be a symbolic link")
        if self.temporary_directory.exists():
            shutil.rmtree(self.temporary_directory)

    def _state_path(self, name: str) -> Path:
        name = _safe_name(name, "state name")
        path = self.state_directory / name
        if path.is_symlink():
            raise ValueError("state path must not be a symbolic link")
        return path


class FilePluginStorageFactory:
    def __init__(self, root: Path, sandbox_key: SandboxKey) -> None:
        self.root = root
        self.sandbox_key = sandbox_key

    def for_plugin(self, plugin_id: str) -> FilePluginStorage:
        if not _PLUGIN.fullmatch(plugin_id):
            raise ValueError("plugin ID is invalid")
        sandbox = self.sandbox_key.path(self.root)
        return FilePluginStorage(
            sandbox / "runtime" / "plugins" / plugin_id,
            sandbox / "world" / plugin_id,
            sandbox / "runtime" / "plugins" / plugin_id / "tmp",
        )


def _safe_name(value: str, label: str) -> str:
    if not _SAFE.fullmatch(value) or value in {".", ".."}:
        raise ValueError(f"{label} is invalid")
    return value
