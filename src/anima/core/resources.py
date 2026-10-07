"""Sandbox-safe model-facing resource collections and shared CRUD tools."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import re
from typing import Literal, Protocol

from anima.capabilities.contracts import (
    ActionRecord,
    CapabilityConfigurationError,
    CapabilityContext,
    ContextReference,
    FunctionToolSpec,
    ToolResult,
)
from anima.core.telemetry import emit


ResourceOperation = Literal[
    "list", "search", "read", "write", "delete", "export", "import"
]
ResourceScope = Literal["turn", "sandbox", "process"]
TransferPolicy = Literal["none", "copy", "consume"]
COLLECTION_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}\.[a-z][a-z0-9_]{0,63}$")
OPERATIONS = frozenset({
    "list", "search", "read", "write", "delete", "export", "import",
})


@dataclass(frozen=True, slots=True)
class ResourceCollectionSpec:
    id: str
    plugin: str
    description: str
    scope: ResourceScope
    operations: frozenset[ResourceOperation]
    writable_content: Literal["none", "text"] = "none"
    transfer_policy: TransferPolicy = "none"
    max_list_results: int = 50
    max_search_results: int = 10

    def __post_init__(self) -> None:
        if not COLLECTION_ID.fullmatch(self.id):
            raise CapabilityConfigurationError("resource collection ID is invalid")
        owner = self.id.split(".", 1)[0]
        if owner in {"core", "interface"}:
            if self.plugin != "core":
                raise CapabilityConfigurationError("resource collection prefix is reserved")
        elif owner != self.plugin:
            raise CapabilityConfigurationError("resource collection owner must match plugin")
        if not self.description.strip() or len(self.description) > 240:
            raise CapabilityConfigurationError("resource collection description is invalid")
        if self.scope not in {"turn", "sandbox", "process"}:
            raise CapabilityConfigurationError("resource collection scope is invalid")
        if not self.operations or not self.operations <= OPERATIONS:
            raise CapabilityConfigurationError("resource collection operations are invalid")
        if self.writable_content not in {"none", "text"}:
            raise CapabilityConfigurationError("resource writable content is invalid")
        if "write" in self.operations and self.writable_content == "none":
            raise CapabilityConfigurationError("writable collection must declare content type")
        if self.transfer_policy not in {"none", "copy", "consume"}:
            raise CapabilityConfigurationError("resource transfer policy is invalid")
        if self.transfer_policy != "none" and "export" not in self.operations:
            raise CapabilityConfigurationError("transfer policy requires export")
        if not 1 <= self.max_list_results <= 100 or not 1 <= self.max_search_results <= 50:
            raise CapabilityConfigurationError("resource result limit is invalid")


@dataclass(frozen=True, slots=True)
class ResourceItem:
    id: str
    title: str
    summary: str | None = None
    media_type: str | None = None
    size: int | None = None
    updated_at: datetime | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id or len(self.id) > 256 or not self.title or len(self.title) > 500:
            raise ValueError("resource item identity is invalid")
        if self.summary is not None and len(self.summary) > 2_000:
            raise ValueError("resource item summary is too long")
        if self.size is not None and self.size < 0:
            raise ValueError("resource item size is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "summary": self.summary,
            "media_type": self.media_type,
            "size": self.size,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ResourcePage:
    items: tuple[ResourceItem, ...]
    next_cursor: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {"items": [item.to_dict() for item in self.items], "next_cursor": self.next_cursor}


@dataclass(frozen=True, slots=True)
class ResourceTransfer:
    source_id: str
    media_type: str
    size: int
    path: Path | None = None
    text: str | None = None

    def __post_init__(self) -> None:
        if (self.path is None) == (self.text is None):
            raise ValueError("resource transfer requires exactly one payload")
        if self.size < 1:
            raise ValueError("resource transfer is empty")
        if self.path is not None and (self.path.is_symlink() or not self.path.is_file()):
            raise ValueError("resource transfer path is unsafe")


@dataclass(frozen=True, slots=True)
class ResourceRead:
    """Provider-neutral read result with optional model-observable media."""

    payload: Mapping[str, object]
    attachments: tuple[Path, ...] = ()


class ResourceProvider(Protocol):
    async def list_resources(
        self, collection: str, *, cursor: str | None, limit: int, context: CapabilityContext
    ) -> ResourcePage: ...
    async def search_resources(
        self, collection: str, *, query: str, limit: int, context: CapabilityContext
    ) -> ResourcePage: ...
    async def read_resource(
        self, collection: str, resource_id: str, context: CapabilityContext
    ) -> Mapping[str, object] | ResourceRead: ...
    async def write_resource(
        self, collection: str, resource_id: str, mode: str, content: str,
        context: CapabilityContext,
    ) -> ResourceItem: ...
    async def delete_resource(
        self, collection: str, resource_id: str, context: CapabilityContext
    ) -> None: ...
    async def export_resource(
        self, collection: str, resource_id: str, context: CapabilityContext
    ) -> ResourceTransfer: ...
    async def import_resource(
        self, collection: str, resource_id: str, transfer: ResourceTransfer,
        context: CapabilityContext,
    ) -> ResourceItem: ...


@dataclass(frozen=True, slots=True)
class ResourceRegistration:
    spec: ResourceCollectionSpec
    provider: ResourceProvider


class ResourceRegistry:
    def __init__(self, registrations: Sequence[ResourceRegistration] = ()) -> None:
        values: dict[str, ResourceRegistration] = {}
        for registration in registrations:
            for operation in registration.spec.operations:
                method = {
                    "list": "list_resources", "search": "search_resources",
                    "read": "read_resource", "write": "write_resource",
                    "delete": "delete_resource", "export": "export_resource",
                    "import": "import_resource",
                }[operation]
                if not callable(getattr(registration.provider, method, None)):
                    raise TypeError(
                        f"resource provider does not implement {operation}"
                    )
            if registration.spec.id in values:
                raise CapabilityConfigurationError(
                    f"duplicate resource collection: {registration.spec.id}"
                )
            values[registration.spec.id] = registration
        self._registrations = values

    @property
    def specs(self) -> tuple[ResourceCollectionSpec, ...]:
        return tuple(value.spec for value in self._registrations.values())

    def catalog(self) -> str:
        lines = ["# 利用可能なResource Collection"]
        if self._registrations:
            lines.append("各Collectionは括弧内の操作だけ実行できる。")
            if "core.memory" in self._registrations:
                lines.append("core.memoryは読み取り専用（search/read）。会話中にresource_writeで長期記憶を書き換えない。長期記憶の更新は睡眠処理が行う。")
            if "core.inventory" in self._registrations:
                lines.append("ユーザーが文章・歌詞などを手元に保存するよう提案・依頼した場合、保存先はcore.inventory。内容を確認できたら.txtまたは.mdの名前でresource_writeする。保存に成功するまで保存済みとは言わない。")
            lines.append("writeを持たないCollectionは参照専用。読み出した内容を手元に残す場合は、元のCollectionではなく保存用Collectionへ書き込む。")
        for spec in self.specs:
            operations = ", ".join(sorted(spec.operations))
            lines.append(f"- {spec.id}: {spec.description}（{operations}）")
        return "\n".join(lines) if self._registrations else ""

    def snapshot(self) -> tuple[dict[str, object], ...]:
        return tuple({
            "id": spec.id, "plugin": spec.plugin, "description": spec.description,
            "scope": spec.scope, "operations": sorted(spec.operations),
            "transfer_policy": spec.transfer_policy,
        } for spec in self.specs)

    def _get(self, collection: str, operation: ResourceOperation) -> ResourceRegistration:
        registration = self._registrations.get(collection)
        if registration is None:
            raise LookupError("unknown_collection")
        if operation not in registration.spec.operations:
            raise PermissionError("operation_not_allowed")
        return registration

    async def execute(
        self, operation: ResourceOperation, arguments: Mapping[str, object],
        context: CapabilityContext,
    ) -> tuple[Mapping[str, object], tuple[Path, ...]]:
        collection = str(arguments.get("collection", ""))
        registration = self._get(collection, operation)
        provider = registration.provider
        attachments: tuple[Path, ...] = ()
        if operation == "list":
            limit = min(int(arguments["limit"]), registration.spec.max_list_results)
            page = await provider.list_resources(
                collection, cursor=str(arguments["cursor"]) or None,
                limit=limit, context=context,
            )
            payload: Mapping[str, object] = page.to_dict()
        elif operation == "search":
            limit = min(int(arguments["limit"]), registration.spec.max_search_results)
            page = await provider.search_resources(
                collection, query=str(arguments["query"]), limit=limit, context=context,
            )
            payload = page.to_dict()
        elif operation == "read":
            read = await provider.read_resource(
                collection, str(arguments["resource_id"]), context
            )
            if isinstance(read, ResourceRead):
                payload, attachments = read.payload, read.attachments
            else:
                payload = read
        elif operation == "write":
            item = await provider.write_resource(
                collection, str(arguments["resource_id"]), str(arguments["mode"]),
                str(arguments["content"]), context,
            )
            payload = {"item": item.to_dict()}
        elif operation == "delete":
            resource_id = str(arguments["resource_id"])
            await provider.delete_resource(collection, resource_id, context)
            payload = {"resource_id": resource_id}
        else:
            raise ValueError("transfer must use transfer()")
        emit("resource.operation", collection=collection, resource_operation=operation)
        return {"ok": True, **payload}, attachments

    async def transfer(
        self, arguments: Mapping[str, object], context: CapabilityContext
    ) -> Mapping[str, object]:
        source_id = str(arguments["source_id"])
        source = self._get(str(arguments["source_collection"]), "export")
        destination = self._get(str(arguments["destination_collection"]), "import")
        transfer = await source.provider.export_resource(
            source.spec.id, source_id, context
        )
        item = await destination.provider.import_resource(
            destination.spec.id, str(arguments["destination_id"]), transfer, context
        )
        if source.spec.transfer_policy == "consume":
            if "delete" not in source.spec.operations:
                raise CapabilityConfigurationError("consuming collection cannot delete")
            await source.provider.delete_resource(source.spec.id, source_id, context)
        emit(
            "resource.transfer", source_collection=source.spec.id,
            destination_collection=destination.spec.id, bytes=transfer.size,
        )
        return {"ok": True, "item": item.to_dict()}


class ResourceToolProvider:
    """Expose a fixed tool set regardless of registered collection count."""

    def __init__(self, registry: ResourceRegistry) -> None:
        self.registry = registry

    def context_instructions(self, context: CapabilityContext) -> str:
        del context
        return self.registry.catalog()

    def context_parts(self, context: CapabilityContext) -> tuple[tuple[str, str], ...]:
        del context
        lines = self.registry.catalog().splitlines(keepends=True)
        owners = {f"- {spec.id}:": spec.plugin for spec in self.registry.specs}
        return tuple((next((owner for prefix, owner in owners.items()
                            if line.startswith(prefix)), "core"), line) for line in lines)

    async def tools(self, context: CapabilityContext) -> tuple[FunctionToolSpec, ...]:
        del context
        obj = lambda properties, required: {
            "type": "object", "properties": properties, "required": required,
            "additionalProperties": False,
        }
        collection = {"type": "string", "minLength": 3, "maxLength": 129}
        resource_id = {"type": "string", "minLength": 1, "maxLength": 256}
        return (
            FunctionToolSpec("resource_list", "Collectionの項目を一覧する。", obj({
                "collection": collection, "cursor": {"type": "string", "maxLength": 256},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            }, ["collection", "cursor", "limit"]), requires_source=False),
            FunctionToolSpec("resource_search", "Collectionを検索する。", obj({
                "collection": collection, "query": {"type": "string", "minLength": 1, "maxLength": 2000},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10},
            }, ["collection", "query", "limit"]), requires_source=False),
            FunctionToolSpec("resource_read", "Resourceを読む。画像は自分の確認用。相手に見せる必要がある時だけattach_to_reply=trueにする。確認・感想・会話の続きではfalse。同じ画像を毎回送り直さない。", obj({
                "collection": collection, "resource_id": resource_id,
                "attach_to_reply": {"type": "boolean"},
            }, ["collection", "resource_id", "attach_to_reply"]), requires_source=False),
            FunctionToolSpec("resource_write", "テキストResourceを作成・置換・追記する。", obj({
                "collection": collection, "resource_id": resource_id,
                "mode": {"type": "string", "enum": ["replace", "append"]},
                "content": {"type": "string", "minLength": 1, "maxLength": 16000},
            }, ["collection", "resource_id", "mode", "content"]), side_effect=True),
            FunctionToolSpec("resource_transfer", "Resourceを別Collectionへ安全に移す。", obj({
                "source_collection": collection, "source_id": resource_id,
                "destination_collection": collection, "destination_id": resource_id,
            }, ["source_collection", "source_id", "destination_collection", "destination_id"]), side_effect=True),
            FunctionToolSpec("resource_delete", "Resourceを削除する。", obj({
                "collection": collection, "resource_id": resource_id,
            }, ["collection", "resource_id"]), side_effect=True),
        )

    async def execute_tool(
        self, name: str, arguments: Mapping[str, object], context: CapabilityContext,
        invocation_id: str,
    ) -> ToolResult:
        del invocation_id
        operation = name.removeprefix("resource_")
        if (
            context.permissions.allows("self_time") and operation == "delete"
            and (
                not context.permissions.allows("resource.delete_temporary")
                or arguments.get("collection") != "core.temporary_artifacts"
            )
        ):
            return ToolResult(
                "rejected", {"ok": False, "error": "self_time_delete_not_allowed"}
            )
        try:
            if operation == "transfer":
                payload = await self.registry.transfer(arguments, context)
                attachments = ()
                collection = str(arguments["destination_collection"])
            elif operation in {"list", "search", "read", "write", "delete"}:
                payload, attachments = await self.registry.execute(
                    operation, arguments, context  # type: ignore[arg-type]
                )
                collection = str(arguments["collection"])
            else:
                return ToolResult("rejected", {"ok": False, "error": "unknown_tool"})
        except (ValueError, TypeError, LookupError, PermissionError, OSError, UnicodeError) as error:
            emit(
                "resource.failed", collection=str(
                    arguments.get("collection")
                    or arguments.get("destination_collection") or "unknown"
                ), resource_operation=operation, error_type=type(error).__name__,
                event_id=context.source.id if context.source is not None else None,
                channel_id=context.source.channel_id if context.source is not None else None,
            )
            return ToolResult("rejected", {"ok": False, "error": str(error) or type(error).__name__})
        reference = ContextReference(
            "resource", "collection", f"{name}を実行した", (("collection", collection),)
        )
        if operation == "read":
            payload = {**payload, "attach_to_reply": arguments.get("attach_to_reply") is True}
        return ToolResult(
            "success", payload,
            actions=(ActionRecord("resource", name, f"{collection}へ{name}を実行した"),),
            references=(reference,), attachments=attachments, usage_category="resource",
        )
