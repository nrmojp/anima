"""Self-contained capability implementation."""

from __future__ import annotations

from collections.abc import Mapping
from anima.capabilities.contracts import ActionRecord, CapabilityContext, ContextReference, NativeToolEvent, NativeToolSpec, ToolResult


class WebSearchToolProvider:
    def __init__(self, *, enabled: bool) -> None:
        self.enabled = enabled

    async def tools(self, context: CapabilityContext) -> tuple[NativeToolSpec, ...]:
        return (NativeToolSpec("web_search"),) if self.enabled else ()

    async def execute_tool(
        self, name: str, arguments: Mapping[str, object],
        context: CapabilityContext, invocation_id: str,
    ) -> ToolResult:
        return ToolResult("rejected", {"ok": False, "error": "native_tool"})

    async def consume_native(
        self, events: tuple[NativeToolEvent, ...], context: CapabilityContext
    ) -> ToolResult:
        completed = tuple(event for event in events if event.status == "completed")
        references = []
        queries = []
        for event in completed:
            query = str(event.payload.get("query") or "").strip()
            if query and query not in queries:
                queries.append(query)
            for source in event.payload.get("sources", ()):
                if not isinstance(source, Mapping):
                    continue
                url = str(source.get("url") or "").strip()
                title = str(source.get("title") or url).strip()
                if url and not any(dict(item.attributes).get("url") == url for item in references):
                    references.append(ContextReference(
                        "web_search", "source", title[:600], (("url", url[:500]),)
                    ))
                if len(references) >= 3:
                    break
        return ToolResult(
            "success" if completed else "rejected",
            {"ok": bool(completed), "queries": queries[:3]},
            actions=(ActionRecord("web_search", "web_search", "Web検索を実行した"),)
            if completed else (),
            references=tuple(references),
            usage_category="web_search",
        )
