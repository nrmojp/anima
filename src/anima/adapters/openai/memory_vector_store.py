"""Synchronize one sandbox's long-term memory with an OpenAI vector store."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any


class MemoryVectorStore:
    def __init__(self, root: Path, client: Any, *, sandbox_key: str) -> None:
        self.root = root.resolve()
        self.memory_root = self.root / "memory"
        self.manifest_path = self.root / "runtime" / "memory-vector-store.json"
        self.client = client
        self.sandbox_key = sandbox_key
        self._lock = asyncio.Lock()

    async def ensure(self) -> str | None:
        content = self._content()
        if not content:
            return None
        fingerprint = hashlib.sha256(content).hexdigest()
        async with self._lock:
            manifest = self._load_manifest()
            vector_store_id = manifest.get("vector_store_id")
            if vector_store_id and manifest.get("fingerprint") == fingerprint:
                return str(vector_store_id)
            if not vector_store_id:
                store = await self.client.vector_stores.create(
                    name=f"anima-memory-{self.sandbox_key}",
                    description="Anima long-term memory isolated to one Discord sandbox.",
                    metadata={"sandbox_key": self.sandbox_key},
                )
                vector_store_id = str(store.id)
                manifest = {
                    "schema_version": 1,
                    "sandbox_key": self.sandbox_key,
                    "vector_store_id": vector_store_id,
                }
                self._save_manifest(manifest)
            uploaded = await self.client.vector_stores.files.upload_and_poll(
                vector_store_id=vector_store_id,
                file=("anima-memory.md", content, "text/markdown"),
                attributes={"sandbox_key": self.sandbox_key, "dataset": "memory"},
                chunking_strategy={
                    "type": "static",
                    "static": {"max_chunk_size_tokens": 200, "chunk_overlap_tokens": 40},
                },
            )
            if str(uploaded.status) != "completed":
                await self.client.vector_stores.files.delete(
                    str(uploaded.id), vector_store_id=vector_store_id
                )
                await self.client.files.delete(str(uploaded.id))
                raise RuntimeError(f"memory vector file indexing failed: {uploaded.status}")
            new_file_id = str(uploaded.id)
            previous_file_id = manifest.get("file_id")
            self._save_manifest({
                "schema_version": 1, "sandbox_key": self.sandbox_key,
                "vector_store_id": vector_store_id, "file_id": new_file_id,
                "fingerprint": fingerprint,
                "synced_at": datetime.now(timezone.utc).isoformat(),
                "content_bytes": len(content),
                "document_count": memory_document_count(self.memory_root),
            })
            if previous_file_id and previous_file_id != new_file_id:
                await self.client.vector_stores.files.delete(
                    str(previous_file_id), vector_store_id=vector_store_id
                )
                await self.client.files.delete(str(previous_file_id))
            return vector_store_id

    def _content(self) -> bytes:
        return memory_content(self.memory_root)

    def _load_manifest(self) -> dict[str, Any]:
        return load_memory_manifest(self.manifest_path, sandbox_key=self.sandbox_key)

    def _save_manifest(self, value: dict[str, Any]) -> None:
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.manifest_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temporary, self.manifest_path)

    async def remote_files(self) -> list[Any]:
        """List every file attached to the managed store, following pagination."""
        manifest = self._load_manifest()
        vector_store_id = manifest.get("vector_store_id")
        if not vector_store_id:
            return []
        page = await self.client.vector_stores.files.list(
            vector_store_id=str(vector_store_id), limit=100
        )
        files: list[Any] = []
        while True:
            files.extend(page.data)
            if not page.has_next_page():
                return files
            page = await page.get_next_page()

    async def prune_orphans(self, *, apply: bool = False) -> dict[str, Any]:
        """Find, and optionally delete, superseded files owned by this sandbox."""
        manifest = self._load_manifest()
        vector_store_id = manifest.get("vector_store_id")
        current_file_id = manifest.get("file_id")
        candidates = []
        unmanaged = []
        for item in await self.remote_files():
            item_id = str(item.id)
            if item_id == current_file_id:
                continue
            attributes = getattr(item, "attributes", None) or {}
            summary = {
                "id": item_id,
                "status": str(getattr(item, "status", "unknown")),
                "usage_bytes": int(getattr(item, "usage_bytes", 0) or 0),
            }
            if (
                attributes.get("sandbox_key") == self.sandbox_key
                and attributes.get("dataset") == "memory"
            ):
                candidates.append(summary)
            else:
                unmanaged.append(summary)
        if apply and vector_store_id:
            for item in candidates:
                await self.client.vector_stores.files.delete(
                    item["id"], vector_store_id=str(vector_store_id)
                )
                await self.client.files.delete(item["id"])
        return {
            "dry_run": not apply,
            "candidates": candidates,
            "unmanaged": unmanaged,
            "deleted": len(candidates) if apply else 0,
        }

    async def rebuild(self) -> str | None:
        """Build a replacement store first, then retire the previous resources."""
        if not self._content():
            return None
        previous = self._load_manifest()
        replacement = await self.client.vector_stores.create(
            name=f"anima-memory-{self.sandbox_key}",
            description="Anima long-term memory isolated to one Discord sandbox.",
            metadata={"sandbox_key": self.sandbox_key},
        )
        replacement_id = str(replacement.id)
        self._save_manifest({
            "schema_version": 1,
            "sandbox_key": self.sandbox_key,
            "vector_store_id": replacement_id,
        })
        try:
            result = await self.ensure()
        except Exception:
            if previous:
                self._save_manifest(previous)
            else:
                self.manifest_path.unlink(missing_ok=True)
            try:
                await self.client.vector_stores.delete(replacement_id)
            except Exception:
                pass
            raise
        old_store_id = previous.get("vector_store_id")
        old_file_id = previous.get("file_id")
        if old_store_id and old_store_id != replacement_id:
            await self.client.vector_stores.delete(str(old_store_id))
        if old_file_id:
            await self.client.files.delete(str(old_file_id))
        return result


def memory_content(memory_root: Path) -> bytes:
    """Build the exact Markdown payload used by the remote memory index."""
    memory_root = memory_root.resolve()
    if not memory_root.is_dir() or memory_root.is_symlink():
        return b""
    sections: list[str] = []
    for path in sorted(memory_root.glob("**/*.md")):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.resolve().relative_to(memory_root).as_posix()
        content = path.read_text(encoding="utf-8").strip()
        if content:
            sections.append(f"# source: memory/{relative}\n\n{content}")
    return ("\n\n---\n\n".join(sections) + ("\n" if sections else "")).encode()


def memory_document_count(memory_root: Path) -> int:
    memory_root = memory_root.resolve()
    if not memory_root.is_dir() or memory_root.is_symlink():
        return 0
    return sum(
        1 for path in memory_root.glob("**/*.md")
        if path.is_file() and not path.is_symlink()
        and path.read_text(encoding="utf-8").strip()
    )


def load_memory_manifest(path: Path, *, sandbox_key: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if value.get("sandbox_key") == sandbox_key else {}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def manifest_modified_at(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    except OSError:
        return None


def memory_vector_status(root: Path, *, sandbox_key: str) -> dict[str, Any]:
    """Describe local-to-Vector-Store synchronization without calling OpenAI."""
    memory_root = root / "memory"
    content = memory_content(memory_root)
    documents = memory_document_count(memory_root)
    manifest_path = root / "runtime" / "memory-vector-store.json"
    manifest = load_memory_manifest(manifest_path, sandbox_key=sandbox_key)
    fingerprint = hashlib.sha256(content).hexdigest() if content else None
    complete = bool(manifest.get("vector_store_id") and manifest.get("file_id"))
    if not content:
        state = "empty"
    elif not complete:
        state = "not_created"
    elif manifest.get("fingerprint") == fingerprint:
        state = "synced"
    else:
        state = "stale"
    synced_at = manifest.get("synced_at")
    if not synced_at and complete:
        synced_at = manifest_modified_at(manifest_path)
    indexed_bytes = manifest.get("content_bytes")
    if indexed_bytes is None and state == "synced":
        indexed_bytes = len(content)
    return {
        "state": state,
        "synced_at": synced_at,
        "current_bytes": len(content),
        "indexed_bytes": indexed_bytes,
        "document_count": documents,
        "vector_store_id": manifest.get("vector_store_id"),
        "file_id": manifest.get("file_id"),
    }
