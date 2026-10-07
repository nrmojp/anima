"""Self-contained capability implementation."""

from __future__ import annotations

from pathlib import Path
import re
from anima.capabilities.contracts import ActionRecord, CapabilityContext, FunctionToolSpec, ToolResult
from anima.core.inventory import InventoryStore


class InventoryProvider:
    """Core-owned primitive inventory and attachment tools."""

    def __init__(self, store: InventoryStore) -> None:
        self.store = store
        self._generated: dict[str, str] = {}

    def register_generated(self, event_id: str, artifact_id: str) -> None:
        """Bind the current turn's opaque generated-artifact reference."""
        self.store.describe(artifact_id, location="temporary")
        self._generated[event_id] = artifact_id

    def _artifact_id(self, value: object, context: CapabilityContext) -> str:
        artifact_id = str(value)
        if artifact_id.startswith("$attachment:"):
            return self._stage_attachment(artifact_id, context)
        if artifact_id != "$generated":
            return artifact_id
        source = context.source
        if source is None or source.id not in self._generated:
            raise FileNotFoundError("current turn has no generated artifact")
        return self._generated[source.id]

    def _stage_attachment(self, reference: str, context: CapabilityContext) -> str:
        source = context.source
        match = re.fullmatch(r"\$attachment:([0-9]+)", reference)
        if source is None or match is None:
            raise FileNotFoundError("current turn has no attachment")
        index = int(match.group(1))
        if index >= len(source.attachments):
            raise FileNotFoundError("current turn has no such attachment")
        attachment = source.attachments[index]
        if attachment.cache_name is None:
            raise FileNotFoundError("attachment is not cached")
        cached = self.store.sandbox_root / "attachments" / attachment.cache_name
        suffix = Path(attachment.cache_name).suffix.lower()
        artifact_id = f"discord-{source.id}-{index}{suffix}"
        try:
            self.store.stage_file(artifact_id, cached)
        except FileExistsError:
            self.store.describe(artifact_id, location="temporary")
        return artifact_id

    async def tools(self, context: CapabilityContext) -> tuple[FunctionToolSpec, ...]:
        object_schema = lambda properties, required: {
            "type": "object", "properties": properties, "required": required,
            "additionalProperties": False,
        }
        artifact = {"type": "string", "minLength": 1, "maxLength": 128}
        return (
            FunctionToolSpec(
                "list_inventory", "手元に残している物と、今回の一時生成物を一覧する。",
                object_schema({"include_temporary": {"type": "boolean"}}, ["include_temporary"]),
                requires_source=False,
            ),
            FunctionToolSpec(
                "read_inventory", "テキストの持ち物または一時生成物を読む。",
                object_schema({"artifact_id": artifact}, ["artifact_id"]),
                requires_source=False,
            ),
            FunctionToolSpec(
                "keep_artifact",
                "一時生成物または現在のDiscord添付を、内容が分かる新しい英数字ファイル名で手元に残す。添付は$attachment:0のように指定し、元の拡張子を保つ。",
                object_schema({
                    "artifact_id": artifact,
                    "filename": artifact,
                }, ["artifact_id", "filename"]), side_effect=True,
            ),
            FunctionToolSpec(
                "edit_inventory_text",
                "文章の持ち物を作成・置換または追記する。拡張子は.txtか.mdにする。",
                object_schema({
                    "artifact_id": artifact,
                    "mode": {"type": "string", "enum": ["replace", "append"]},
                    "content": {
                    "type": "string", "minLength": 1, "maxLength": 16000,
                }}, ["artifact_id", "mode", "content"]), side_effect=True,
            ),
            FunctionToolSpec(
                "discard_artifact", "一時生成物または持ち物を捨てる。",
                object_schema({"artifact_id": artifact}, ["artifact_id"]), side_effect=True,
            ),
            FunctionToolSpec(
                "attach_artifact",
                "一時生成物または持ち物を、次の最終応答へ添付する。",
                object_schema({"artifact_id": artifact}, ["artifact_id"]), side_effect=True,
            ),
        )

    async def execute_tool(self, name, arguments, context, invocation_id) -> ToolResult:
        del invocation_id
        try:
            artifact_id = self._artifact_id(arguments.get("artifact_id", ""), context)
            if name == "list_inventory":
                items = self.store.list(
                    include_temporary=bool(arguments["include_temporary"])
                )
                payload = {"ok": True, "items": [item.to_dict() for item in items]}
            elif name == "read_inventory":
                payload = {"ok": True, "artifact_id": artifact_id,
                           "content": self.store.read_text(artifact_id)}
            elif name == "keep_artifact":
                filename = str(arguments["filename"])
                if filename == artifact_id:
                    raise ValueError("kept artifact requires a new filename")
                payload = {"ok": True, "item": self.store.keep(
                    artifact_id, filename=filename,
                ).to_dict()}
                if (context.source is not None
                        and self._generated.get(context.source.id) == artifact_id):
                    self._generated[context.source.id] = filename
            elif name == "edit_inventory_text":
                if not artifact_id.lower().endswith((".txt", ".md")):
                    raise ValueError("text inventory must use .txt or .md")
                item = self.store.write_text(
                    artifact_id, str(arguments["content"]),
                    append=arguments["mode"] == "append",
                )
                payload = {"ok": True, "item": item.to_dict()}
            elif name == "discard_artifact":
                payload = {"ok": True, "artifact_id": artifact_id,
                           "location": self.store.discard(artifact_id)}
                if context.source is not None:
                    self._generated.pop(context.source.id, None)
            elif name == "attach_artifact":
                path = self.store.resolve(artifact_id)
                payload = {"ok": True, "artifact_id": artifact_id, "attached": True}
                if context.source is not None:
                    self._generated.pop(context.source.id, None)
                action = ActionRecord("inventory", name, "持ち物を添付対象にした")
                return ToolResult(
                    "success", payload, actions=(action,), attachments=(path,),
                    usage_category="inventory",
                )
            else:
                return ToolResult("rejected", {"ok": False, "error": "unknown_tool"})
        except (ValueError, UnicodeError, FileNotFoundError, FileExistsError) as error:
            return ToolResult("rejected", {"ok": False, "error": type(error).__name__})
        action = ActionRecord("inventory", name, f"{name}を実行した")
        return ToolResult("success", payload, actions=(action,), usage_category="inventory")
