"""Sandbox-local temporary artifacts and durable personal inventory."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import mimetypes
from pathlib import Path
import re
import shutil

from anima.core.sandbox import SandboxKey, reject_symlinks


SAFE_ARTIFACT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
MAX_ARTIFACT_BYTES = 25 * 1024 * 1024
MAX_TEXT_BYTES = 256 * 1024
DEFAULT_MAX_ITEMS = 500
DEFAULT_MAX_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class InventoryItem:
    id: str
    location: str
    size: int
    modified_at: datetime
    content_type: str

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "location": self.location,
            "size": self.size,
            "modified_at": self.modified_at.isoformat(),
            "content_type": self.content_type,
        }


class InventoryStore:
    """Own artifacts for exactly one sandbox; plugins never receive this store."""

    def __init__(
        self, root: Path, sandbox_key: SandboxKey, *,
        max_items: int = DEFAULT_MAX_ITEMS,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        if max_items < 1 or max_bytes < 1:
            raise ValueError("inventory limits must be positive")
        self.sandbox_key = sandbox_key
        self.max_items = max_items
        self.max_bytes = max_bytes
        self.sandbox_root = sandbox_key.path(root)
        self.inventory_root = self.sandbox_root / "inventory"
        self.temporary_root = self.sandbox_root / "runtime" / "tmp" / "artifacts"

    def list(self, *, include_temporary: bool = False) -> tuple[InventoryItem, ...]:
        result = list(self._items(self.inventory_root, "inventory"))
        if include_temporary:
            result.extend(self._items(self.temporary_root, "temporary"))
        return tuple(sorted(result, key=lambda item: (item.location, item.id)))

    def summary(self) -> str:
        durable = self.list()
        temporary = tuple(
            item for item in self.list(include_temporary=True)
            if item.location == "temporary"
        )
        if not durable and not temporary:
            return "手元に残している物はない。"
        kinds: dict[str, int] = {}
        for item in durable:
            kind = item.content_type.split("/", 1)[0]
            kinds[kind] = kinds.get(kind, 0) + 1
        detail = "、".join(f"{kind} {count}" for kind, count in sorted(kinds.items()))
        durable_text = f"残した物は{len(durable)}個" + (f"（{detail}）" if detail else "")
        return f"{durable_text}。判断待ちの一時成果物は{len(temporary)}個ある。"

    def stage_bytes(self, artifact_id: str, content: bytes) -> InventoryItem:
        if not content:
            raise ValueError("artifact is empty")
        if len(content) > MAX_ARTIFACT_BYTES:
            raise ValueError("artifact is too large")
        path = self._path(self.temporary_root, artifact_id)
        if path.exists() or self._path(self.inventory_root, artifact_id).exists():
            raise FileExistsError(artifact_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return self.describe(artifact_id, location="temporary")

    def stage_file(self, artifact_id: str, source: Path) -> InventoryItem:
        """Copy a validated external file into this sandbox's temporary area."""
        if source.is_symlink() or not source.is_file():
            raise ValueError("artifact source is not a regular file")
        size = source.stat().st_size
        if not size:
            raise ValueError("artifact is empty")
        if size > MAX_ARTIFACT_BYTES:
            raise ValueError("artifact is too large")
        path = self._path(self.temporary_root, artifact_id)
        if path.exists() or self._path(self.inventory_root, artifact_id).exists():
            raise FileExistsError(artifact_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, path)
        return self.describe(artifact_id, location="temporary")

    def import_file(self, artifact_id: str, source: Path) -> InventoryItem:
        """Copy a validated file directly into durable inventory."""
        if source.is_symlink() or not source.is_file():
            raise ValueError("artifact source is not a regular file")
        size = source.stat().st_size
        if not size:
            raise ValueError("artifact is empty")
        if size > MAX_ARTIFACT_BYTES:
            raise ValueError("artifact is too large")
        target = self._path(self.inventory_root, artifact_id)
        if target.exists() or self._path(self.temporary_root, artifact_id).exists():
            raise FileExistsError(artifact_id)
        if Path(artifact_id).suffix.lower() != source.suffix.lower():
            raise ValueError("imported artifact must preserve its file extension")
        self._check_capacity(additional_items=1, additional_bytes=size)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        return self.describe(artifact_id)

    def write_text(self, artifact_id: str, content: str, *, append: bool = False) -> InventoryItem:
        encoded = content.encode("utf-8")
        path = self._path(self.inventory_root, artifact_id)
        if self._path(self.temporary_root, artifact_id).exists():
            raise FileExistsError(artifact_id)
        if append and not path.is_file():
            raise FileNotFoundError(artifact_id)
        existing = path.stat().st_size if append and path.exists() else 0
        if existing + len(encoded) > MAX_TEXT_BYTES:
            raise ValueError("inventory text is too large")
        self._check_capacity(
            additional_items=0 if path.exists() else 1,
            additional_bytes=len(encoded) - existing,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a" if append else "w", encoding="utf-8") as stream:
            stream.write(content)
        return self.describe(artifact_id)

    def read_text(self, artifact_id: str, *, max_chars: int = 8_000) -> str:
        path = self.resolve(artifact_id)
        if path.stat().st_size > MAX_TEXT_BYTES:
            raise ValueError("inventory item is not readable text")
        return path.read_text(encoding="utf-8")[:max_chars]

    def keep(self, artifact_id: str, *, filename: str | None = None) -> InventoryItem:
        source = self._path(self.temporary_root, artifact_id)
        if not source.is_file():
            raise FileNotFoundError(artifact_id)
        if source.stat().st_size > MAX_ARTIFACT_BYTES:
            raise ValueError("artifact is too large")
        target_id = filename or artifact_id
        if Path(target_id).suffix.lower() != Path(artifact_id).suffix.lower():
            raise ValueError("kept artifact must preserve its file extension")
        target = self._path(self.inventory_root, target_id)
        if target.exists():
            raise FileExistsError(target_id)
        self._check_capacity(additional_items=1, additional_bytes=source.stat().st_size)
        target.parent.mkdir(parents=True, exist_ok=True)
        source.replace(target)
        return self.describe(target_id)

    def discard(self, artifact_id: str, *, location: str | None = None) -> str:
        locations = self._locations(location)
        for selected, root in locations:
            path = self._path(root, artifact_id)
            if path.is_file():
                path.unlink()
                return selected
        raise FileNotFoundError(artifact_id)

    def resolve(self, artifact_id: str, *, location: str | None = None) -> Path:
        for _selected, root in self._locations(location):
            path = self._path(root, artifact_id)
            if path.is_file():
                if path.stat().st_size > MAX_ARTIFACT_BYTES:
                    raise ValueError("artifact is too large")
                return path
        raise FileNotFoundError(artifact_id)

    def _locations(self, location: str | None):
        values = {
            "inventory": self.inventory_root,
            "temporary": self.temporary_root,
        }
        if location is not None:
            if location not in values:
                raise ValueError("artifact location is invalid")
            return ((location, values[location]),)
        return tuple(values.items())

    def _check_capacity(self, *, additional_items: int, additional_bytes: int) -> None:
        items = self.list()
        if len(items) + additional_items > self.max_items:
            raise ValueError("inventory item limit reached")
        if sum(item.size for item in items) + additional_bytes > self.max_bytes:
            raise ValueError("inventory byte limit reached")

    def describe(self, artifact_id: str, *, location: str = "inventory") -> InventoryItem:
        root = self.inventory_root if location == "inventory" else self.temporary_root
        path = self._path(root, artifact_id)
        stat = path.stat()
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return InventoryItem(
            artifact_id, location, stat.st_size,
            datetime.fromtimestamp(stat.st_mtime, timezone.utc), mime,
        )

    def cleanup_temporary(self, *, now: datetime, max_age: timedelta = timedelta(days=1)) -> int:
        if not self.temporary_root.exists():
            return 0
        cutoff = now.astimezone(timezone.utc) - max_age
        removed = 0
        for item in tuple(self._items(self.temporary_root, "temporary")):
            if item.modified_at < cutoff:
                self._path(self.temporary_root, item.id).unlink()
                removed += 1
        return removed

    def _items(self, root: Path, location: str):
        if not root.exists():
            return
        reject_symlinks(root, self.sandbox_root)
        for path in root.iterdir():
            if path.is_symlink():
                raise ValueError("inventory item must not be a symbolic link")
            if path.is_file():
                yield self.describe(path.name, location=location)

    def _path(self, root: Path, artifact_id: str) -> Path:
        if not SAFE_ARTIFACT_ID.fullmatch(artifact_id) or artifact_id in {".", ".."}:
            raise ValueError("artifact ID is invalid")
        reject_symlinks(root, self.sandbox_root)
        path = root / artifact_id
        reject_symlinks(path, self.sandbox_root)
        return path
