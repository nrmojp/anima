"""Application adapters for Core Resource Collections."""

from __future__ import annotations

from pathlib import Path

import mimetypes

import re

from datetime import datetime, timezone


from anima.capabilities.contracts import CapabilityContext

from anima.core.inventory import InventoryStore

from anima.core.memory import LocalMemoryRetriever

from anima.core.resources import ResourceItem, ResourcePage, ResourceRead, ResourceTransfer


from anima.core.resource_provider import ResourceProviderBase

class InventoryResourceProvider(ResourceProviderBase):
    def __init__(self, store: InventoryStore) -> None:
        self.store = store
        self._generated: dict[str, str] = {}

    def register_generated(self, event_id: str, artifact_id: str) -> None:
        self.store.describe(artifact_id, location="temporary")
        self._generated[event_id] = artifact_id

    def generated_id(self, context: CapabilityContext) -> str:
        source = context.source
        if source is None or source.id not in self._generated:
            raise FileNotFoundError("current turn has no generated artifact")
        return self._generated[source.id]

    async def list_resources(self, collection, *, cursor, limit, context):
        del cursor, context
        location = "inventory" if collection == "core.inventory" else "temporary"
        items = tuple(
            self._item(item) for item in self.store.list(include_temporary=True)
            if item.location == location
        )[:limit]
        return ResourcePage(items)

    async def read_resource(self, collection, resource_id, context):
        del context
        location = "inventory" if collection == "core.inventory" else "temporary"
        path = self.store.resolve(resource_id, location=location)
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        payload = {
            "ok": True, "resource_id": resource_id,
            "content_type": media_type, "location": location,
        }
        if media_type.startswith("text/") or path.suffix.lower() in {".json", ".md"}:
            if path.stat().st_size > 256 * 1024:
                raise ValueError("inventory text is too large to read")
            return {**payload, "content": path.read_text(encoding="utf-8")[:8_000]}
        return ResourceRead({**payload, "attached": True}, (path,))

    async def write_resource(self, collection, resource_id, mode, content, context):
        del collection, context
        if not resource_id.lower().endswith((".txt", ".md")):
            raise ValueError("text resource must use .txt or .md")
        return self._item(self.store.write_text(
            resource_id, content, append=mode == "append"
        ))

    async def delete_resource(self, collection, resource_id, context):
        location = "inventory" if collection == "core.inventory" else "temporary"
        self.store.discard(resource_id, location=location)
        if context.source is not None and self._generated.get(context.source.id) == resource_id:
            self._generated.pop(context.source.id, None)

    async def export_resource(self, collection, resource_id, context):
        del context
        location = "inventory" if collection == "core.inventory" else "temporary"
        path = self.store.resolve(resource_id, location=location)
        return ResourceTransfer(
            resource_id, mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            path.stat().st_size, path=path,
        )

    async def import_resource(self, collection, resource_id, transfer, context):
        del collection
        if transfer.path is not None:
            item = self.store.import_file(resource_id, transfer.path)
        else:
            if not resource_id.lower().endswith((".txt", ".md")):
                raise ValueError("text resource must use .txt or .md")
            item = self.store.write_text(resource_id, transfer.text)
        if context.source is not None:
            self._generated[context.source.id] = item.id
        return self._item(item)


    @staticmethod
    def _item(item) -> ResourceItem:
        return ResourceItem(
            item.id, item.id, media_type=item.content_type, size=item.size,
            updated_at=item.modified_at, metadata={"location": item.location},
        )

class OpenItemsResourceProvider(ResourceProviderBase):
    """Expose intentions; self time may reconcile the unfinished list."""

    def __init__(self, sandbox_root: Path) -> None:
        self.path = sandbox_root / "open.md"

    def _item(self) -> ResourceItem:
        size = self.path.stat().st_size if self.path.exists() else 0
        updated = (
            datetime.fromtimestamp(self.path.stat().st_mtime, timezone.utc)
            if self.path.exists() else None
        )
        return ResourceItem(
            "open.md", "これからしたいこと", media_type="text/markdown",
            size=size, updated_at=updated,
        )

    async def list_resources(self, collection, *, cursor, limit, context):
        del collection, cursor, context
        return ResourcePage((self._item(),) if limit else ())

    async def read_resource(self, collection, resource_id, context):
        del collection, context
        if resource_id != "open.md":
            raise FileNotFoundError(resource_id)
        return {
            "ok": True, "resource_id": resource_id,
            "content": self.path.read_text(encoding="utf-8") if self.path.exists() else "",
        }

    async def write_resource(self, collection, resource_id, mode, content, context):
        del collection
        if resource_id != "open.md" or mode not in {"append", "replace"}:
            raise PermissionError("unsupported open items write")
        now = context.source.ts if context.source is not None else datetime.now(timezone.utc)
        existing = self.path.read_text(encoding="utf-8") if self.path.exists() else ""
        if mode == "replace":
            if "self_time" not in context.permissions.values:
                raise PermissionError("open items replacement requires self time")
            lines = [line for line in content.splitlines() if line.strip()]
            if len(content) > 20000 or any(not re.fullmatch(r"- \[\d{4}-\d{2}-\d{2} (?:DM|#[^\] ]+)\] .+", line) for line in lines):
                raise ValueError("open items format is invalid")
            if any(line not in lines for line in existing.splitlines() if "(job:" in line):
                raise PermissionError("active job promises must be preserved")
            updated = "\n".join(lines) + ("\n" if lines else "")
        else:
            summary = " ".join(content.split())
            if not summary or len(summary) > 500:
                raise ValueError("open item is invalid")
            line = f"- [{now.date().isoformat()} #self-time] {summary}\n"
            updated = existing.rstrip() + ("\n" if existing.strip() else "") + line
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(updated, encoding="utf-8")
        temporary.replace(self.path)
        return self._item()

class AttachmentResourceProvider(ResourceProviderBase):
    def __init__(self, store: InventoryStore) -> None:
        self.store = store

    def _attachment(self, resource_id: str, context: CapabilityContext):
        source = context.source
        if source is None or not resource_id.isdigit():
            raise FileNotFoundError("current turn has no attachment")
        index = int(resource_id)
        if index >= len(source.attachments):
            raise FileNotFoundError("current turn has no such attachment")
        attachment = source.attachments[index]
        if attachment.cache_name is None:
            raise FileNotFoundError("attachment is not cached")
        path = self.store.sandbox_root / "attachments" / attachment.cache_name
        if path.is_symlink() or not path.is_file():
            raise FileNotFoundError("attachment cache is unavailable")
        return index, attachment, path

    async def list_resources(self, collection, *, cursor, limit, context):
        del collection, cursor
        source = context.source
        if source is None:
            return ResourcePage(())
        values = []
        for index, attachment in enumerate(source.attachments):
            if attachment.cache_name is None:
                continue
            path = self.store.sandbox_root / "attachments" / attachment.cache_name
            if path.is_file() and not path.is_symlink():
                values.append(ResourceItem(
                    str(index), Path(attachment.cache_name).name,
                    media_type=attachment.content_type, size=path.stat().st_size,
                ))
        return ResourcePage(tuple(values[:limit]))

    async def read_resource(self, collection, resource_id, context):
        del collection
        _index, attachment, path = self._attachment(resource_id, context)
        payload = {
            "ok": True, "resource_id": resource_id,
            "content_type": attachment.content_type,
        }
        media_type = attachment.content_type or mimetypes.guess_type(path.name)[0] or ""
        if media_type.startswith("text/"):
            if path.stat().st_size > 256 * 1024:
                raise ValueError("attachment text is too large to read")
            return {**payload, "content": path.read_text(encoding="utf-8")[:8_000]}
        return ResourceRead({**payload, "attached": True}, (path,))

    async def export_resource(self, collection, resource_id, context):
        del collection
        _index, attachment, path = self._attachment(resource_id, context)
        return ResourceTransfer(
            path.name, attachment.content_type or mimetypes.guess_type(path.name)[0]
            or "application/octet-stream", path.stat().st_size, path=path,
        )

class MemoryResourceProvider(ResourceProviderBase):
    def __init__(self, root: Path, retriever: LocalMemoryRetriever) -> None:
        self.root = root
        self.retriever = retriever

    def _path(self, resource_id: str) -> Path:
        relative = Path(resource_id)
        if relative.is_absolute() or ".." in relative.parts or relative.suffix != ".md":
            raise ValueError("memory resource ID is invalid")
        path = self.root / relative
        memory = (self.root / "memory").resolve()
        if path.is_symlink() or not path.resolve().is_relative_to(memory):
            raise ValueError("memory resource path is unsafe")
        return path

    async def search_resources(self, collection, *, query, limit, context):
        del collection
        del context
        passages = self.retriever.search(query, limit=limit)
        return ResourcePage(tuple(ResourceItem(
            passage.source, passage.source, summary=passage.text,
            media_type="text/markdown", metadata={
                "position": passage.position, "score": round(passage.score, 4),
            },
        ) for passage in passages))

    async def read_resource(self, collection, resource_id, context):
        del collection, context
        path = self._path(resource_id)
        return {
            "ok": True, "resource_id": resource_id,
            "content": path.read_text(encoding="utf-8")[:16_000],
        }
