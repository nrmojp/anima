"""Plugin contributions to a model's final structured response."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import json
import re
from typing import Protocol, runtime_checkable

from anima.capabilities.tools import CapabilityContext, ToolResult


_SAFE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_RESERVED = {"reply", "mood", "face"}
_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list}


@dataclass(frozen=True, slots=True)
class ResponseContribution:
    property_name: str
    schema: Mapping[str, object]
    required: bool = False

    def __post_init__(self) -> None:
        if not _SAFE.fullmatch(self.property_name) or self.property_name in _RESERVED:
            raise ValueError("response contribution name is invalid")
        _validate_schema(self.schema)
        try:
            json.dumps(self.schema, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as error:
            raise ValueError("response contribution schema is not JSON serializable") from error


@runtime_checkable
class ResponseContributionProvider(Protocol):
    def response_contributions(self) -> tuple[ResponseContribution, ...]: ...

    async def consume_response(
        self, property_name: str, value: object, context: CapabilityContext,
    ) -> ToolResult: ...


@dataclass(frozen=True, slots=True)
class PreparedResponse:
    contributions: tuple[ResponseContribution, ...]
    routes: Mapping[str, ResponseContributionProvider] = field(repr=False)
    context: CapabilityContext = field(repr=False)

    def observe_tool_result(self, result: ToolResult) -> None:
        """Give declared response contributors observations, without feature branches."""
        seen = set()
        for provider in self.routes.values():
            if id(provider) in seen:
                continue
            seen.add(id(provider))
            callback = getattr(provider, "observe_tool_result", None)
            if callback is not None:
                callback(result, self.context)

    @property
    def format(self) -> Mapping[str, object] | None:
        if not self.contributions:
            return None
        properties = {"reply": {"type": "string"}}
        properties.update({
            item.property_name: (
                dict(item.schema) if item.required
                else {"anyOf": [dict(item.schema), {"type": "null"}]}
            )
            for item in self.contributions
        })
        required = ["reply", *(item.property_name for item in self.contributions)]
        return {
            "type": "json_schema", "name": "anima_response", "strict": True,
            "schema": {
                "type": "object", "properties": properties, "required": required,
                "additionalProperties": False,
            },
        }

    def compose_format(
        self, base_schema: Mapping[str, object], *, name: str = "anima_response",
    ) -> Mapping[str, object]:
        """Add contributed fields to an existing strict response schema."""
        properties = dict(base_schema.get("properties", {}))
        required = list(base_schema.get("required", ()))
        for item in self.contributions:
            if item.property_name in properties:
                raise ValueError(
                    f"response contribution conflicts with core field: {item.property_name}"
                )
            properties[item.property_name] = (
                dict(item.schema) if item.required
                else {"anyOf": [dict(item.schema), {"type": "null"}]}
            )
            required.append(item.property_name)
        return {
            "type": "json_schema", "name": name, "strict": True,
            "schema": {
                **dict(base_schema),
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        }

    async def consume(self, text: str) -> tuple[str, tuple[ToolResult, ...]]:
        if not self.contributions:
            return text, ()
        try:
            value = json.loads(text)
        except (TypeError, json.JSONDecodeError) as error:
            raise RuntimeError("model returned invalid structured response") from error
        return await self.consume_value(value)

    async def consume_value(
        self, value: object, *, allowed_core: frozenset[str] = frozenset({"reply"}),
    ) -> tuple[str, tuple[ToolResult, ...]]:
        """Consume contributions from an already parsed host response."""
        allowed = {*allowed_core, *self.routes}
        if not isinstance(value, dict) or set(value) - allowed:
            raise RuntimeError("model returned invalid structured response")
        reply = value.get("reply")
        if not isinstance(reply, str) or not reply.strip():
            raise RuntimeError("model returned invalid structured response")
        results = []
        for item in self.contributions:
            if item.property_name not in value:
                if item.required:
                    raise RuntimeError("model omitted required response contribution")
                continue
            child = value[item.property_name]
            if child is None and not item.required:
                continue
            if not _matches(child, item.schema):
                raise RuntimeError("model returned invalid response contribution")
            result = await self.routes[item.property_name].consume_response(
                item.property_name, child, self.context,
            )
            if not isinstance(result, ToolResult):
                raise TypeError("response contribution provider must return ToolResult")
            results.append(result)
        return reply.strip(), tuple(results)


class ResponseContributionRegistry:
    def __init__(self, providers: Sequence[ResponseContributionProvider] = ()) -> None:
        self.providers = tuple(providers)
        if any(not isinstance(provider, ResponseContributionProvider) for provider in self.providers):
            raise TypeError("response providers must implement ResponseContributionProvider")

    def prepare(self, context: CapabilityContext) -> PreparedResponse:
        contributions = []
        routes = {}
        for provider in self.providers:
            values = provider.response_contributions()
            if not isinstance(values, tuple):
                raise ValueError("response provider must return a tuple")
            for item in values:
                if not isinstance(item, ResponseContribution):
                    raise ValueError("response provider returned an invalid contribution")
                if item.property_name in routes:
                    raise ValueError(f"duplicate response contribution: {item.property_name}")
                contributions.append(item)
                routes[item.property_name] = provider
        return PreparedResponse(tuple(contributions), routes, context)

    def enrich_draft(self, draft, context: CapabilityContext):
        """Apply trusted, synchronous metadata contributions after final response parsing."""
        from anima.core.models import ResponseDraft
        for provider in self.providers:
            callback = getattr(provider, "enrich_draft", None)
            if callback is not None:
                draft = callback(draft, context)
                if not isinstance(draft, ResponseDraft):
                    raise TypeError("response enrichment must return a ResponseDraft")
        return draft


def _matches(value: object, schema: Mapping[str, object]) -> bool:
    kind = str(schema["type"])
    if not isinstance(value, _TYPES[kind]) or kind in {"integer", "number"} and isinstance(value, bool):
        return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    if kind == "array" and "maxItems" in schema and len(value) > schema["maxItems"]:
        return False
    if kind == "array" and "items" in schema:
        item_schema = schema["items"]
        if not isinstance(item_schema, dict) or item_schema.get("type") not in _TYPES:
            return False
        return all(_matches(child, item_schema) for child in value)
    return True


def _validate_schema(schema: Mapping[str, object]) -> None:
    if schema.get("type") not in _TYPES:
        raise ValueError("response contribution schema is invalid")
    allowed = {"type", "description", "enum", "items", "maxItems"}
    if set(schema) - allowed:
        raise ValueError("response contribution schema contains unsupported fields")
    kind = schema["type"]
    if "maxItems" in schema and (kind != "array" or type(schema["maxItems"]) is not int or schema["maxItems"] < 0):
        raise ValueError("response contribution item limit is invalid")
    if "enum" in schema and not isinstance(schema["enum"], (list, tuple)):
        raise ValueError("response contribution enum is invalid")
    if "enum" in schema and any(
        not _matches(value, {"type": schema["type"]}) for value in schema["enum"]
    ):
        raise ValueError("response contribution enum value is invalid")
    if kind == "array" and "items" not in schema:
        raise ValueError("response contribution array requires items")
    if "items" in schema:
        if kind != "array" or not isinstance(schema["items"], Mapping):
            raise ValueError("response contribution items schema is invalid")
        _validate_schema(schema["items"])
