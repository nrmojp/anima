"""Transport-neutral contracts and registry for optional agent capabilities."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Literal, Protocol, TypeAlias, runtime_checkable
import unicodedata

from anima.core.models import ActionRecord, ContextReference, Event
from anima.core.sandbox import SandboxKey
from anima.capabilities.availability import (
    Availability,
    AvailabilityProvider,
    AvailabilityStatus,
)


SAFE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MAX_TOOL_COUNT = 64
MAX_DESCRIPTION_LENGTH = 1_024
MAX_SCHEMA_BYTES = 32 * 1_024
MAX_TOTAL_SCHEMA_BYTES = 256 * 1_024
MAX_ARGUMENT_BYTES = 16 * 1_024
MAX_MODEL_PAYLOAD_BYTES = 64 * 1_024
MAX_RESULT_ITEMS = 16
MAX_NATIVE_EVENTS = 16
MAX_REFERENCE_ATTRIBUTES = 12
MAX_SUMMARY_LENGTH = 600
MAX_ATTRIBUTE_KEY_LENGTH = 64
MAX_ATTRIBUTE_VALUE_LENGTH = 500

_SCHEMA_KEYS = {
    "type", "description", "enum", "required", "properties", "items", "minimum", "maximum",
    "minLength", "maxLength", "minItems", "maxItems", "additionalProperties",
}
_SCHEMA_TYPES = {"object", "array", "string", "integer", "number", "boolean"}


class CapabilityConfigurationError(ValueError):
    """A capability definition is unsafe or internally inconsistent."""


@dataclass(frozen=True, slots=True)
class PermissionSet:
    values: frozenset[str] = frozenset()

    def allows(self, permission: str) -> bool:
        return permission in self.values


@dataclass(frozen=True, slots=True)
class CapabilityContext:
    source: Event | None
    sandbox_key: SandboxKey
    permissions: PermissionSet = PermissionSet()
    services: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.source is not None and self.source.sandbox_key is not None:
            if self.source.sandbox_key != str(self.sandbox_key):
                raise ValueError("capability context sandbox does not match source event")


@dataclass(frozen=True, slots=True)
class ToolResult:
    status: Literal["success", "rejected"]
    model_payload: Mapping[str, object]
    actions: tuple[ActionRecord, ...] = ()
    references: tuple[ContextReference, ...] = ()
    attachments: tuple[Path, ...] = ()
    usage_category: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"success", "rejected"}:
            raise ValueError("tool result status is invalid")
        if len(self.actions) > MAX_RESULT_ITEMS or len(self.references) > MAX_RESULT_ITEMS:
            raise ValueError("tool result contains too many records")
        if len(self.attachments) > MAX_RESULT_ITEMS:
            raise ValueError("tool result contains too many attachments")
        if self.usage_category is not None:
            _safe_name(self.usage_category, "usage category")
        try:
            encoded = _canonical_json(self.model_payload).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise ValueError("model payload must be JSON serializable") from error
        if len(encoded) > MAX_MODEL_PAYLOAD_BYTES:
            raise ValueError("model payload is too large")

    def output_json(self) -> str:
        return _canonical_json(self.model_payload)


@dataclass(frozen=True, slots=True)
class FunctionToolSpec:
    name: str
    description: str
    parameters: Mapping[str, object]
    strict: bool = True
    side_effect: bool = False
    requires_source: bool = True

    def __post_init__(self) -> None:
        _safe_name(self.name, "tool name")
        _description(self.description)
        _validate_schema(self.parameters, root=True)
        encoded = _canonical_json(self.parameters).encode("utf-8")
        if len(encoded) > MAX_SCHEMA_BYTES:
            raise CapabilityConfigurationError("tool schema is too large")
        if not self.strict:
            raise CapabilityConfigurationError("function tools must be strict")


@dataclass(frozen=True, slots=True)
class NativeToolSpec:
    kind: str
    options: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _safe_name(self.kind, "native tool kind")
        try:
            _canonical_json(self.options)
        except (TypeError, ValueError) as error:
            raise CapabilityConfigurationError(
                "native tool options must be JSON serializable"
            ) from error


ToolSpec: TypeAlias = FunctionToolSpec | NativeToolSpec


@dataclass(frozen=True, slots=True)
class NativeToolEvent:
    kind: str
    call_id: str
    status: Literal["completed", "failed"]
    payload: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _safe_name(self.kind, "native tool kind")
        if not self.call_id or len(self.call_id) > 200 or "\n" in self.call_id:
            raise ValueError("native tool call ID is invalid")
        if self.status not in {"completed", "failed"}:
            raise ValueError("native tool status is invalid")
        try:
            _canonical_json(self.payload)
        except (TypeError, ValueError) as error:
            raise ValueError("native tool payload must be JSON serializable") from error


@runtime_checkable
class ToolProvider(Protocol):
    async def tools(self, context: CapabilityContext) -> tuple[ToolSpec, ...]: ...

    async def execute_tool(
        self,
        name: str,
        arguments: Mapping[str, object],
        context: CapabilityContext,
        invocation_id: str,
    ) -> ToolResult: ...


@runtime_checkable
class NativeToolConsumer(Protocol):
    async def consume_native(
        self, events: tuple[NativeToolEvent, ...], context: CapabilityContext
    ) -> ToolResult: ...


@dataclass(frozen=True, slots=True)
class PreparedToolSet:
    specs: tuple[ToolSpec, ...]
    _functions: Mapping[str, tuple[FunctionToolSpec, ToolProvider]] = field(
        repr=False, compare=False
    )
    _context: CapabilityContext = field(repr=False, compare=False)
    _native: Mapping[str, NativeToolConsumer] = field(
        default_factory=dict, repr=False, compare=False
    )
    unavailable: Mapping[str, AvailabilityStatus] = field(
        default_factory=dict, repr=False, compare=False
    )
    contextual_instructions: str = ""
    context_owners: Mapping[str, str] = field(default_factory=dict)
    instruction_parts: tuple[tuple[str, str], ...] = ()

    @property
    def native_kinds(self) -> frozenset[str]:
        return frozenset(self._native)

    async def execute(self, name: str, raw_arguments: str) -> ToolResult:
        registered = self._functions.get(name)
        if registered is None:
            return ToolResult("rejected", {"ok": False, "error": "unknown_tool"})
        spec, provider = registered
        if len(raw_arguments.encode("utf-8")) > MAX_ARGUMENT_BYTES:
            return ToolResult("rejected", {"ok": False, "error": "arguments_too_large"})
        try:
            arguments = json.loads(raw_arguments or "{}")
        except (TypeError, json.JSONDecodeError):
            return ToolResult("rejected", {"ok": False, "error": "invalid_arguments"})
        if not isinstance(arguments, dict) or not _matches_schema(arguments, spec.parameters):
            return ToolResult("rejected", {"ok": False, "error": "invalid_arguments"})
        if spec.requires_source and self._context.source is None:
            return ToolResult("rejected", {"ok": False, "error": "missing_source"})
        if spec.side_effect and self._context.source is None:
            return ToolResult("rejected", {"ok": False, "error": "missing_source"})
        canonical = _canonical_json(arguments)
        source_id = self._context.source.id if self._context.source is not None else "none"
        invocation_id = sha256(
            f"{self._context.sandbox_key}\0{source_id}\0{name}\0{canonical}".encode("utf-8")
        ).hexdigest()
        from anima.core.plugin_logs import plugin_log
        import time
        started = time.monotonic()
        plugin = self.context_owners.get(name, "core")
        fields = dict(sandbox_key=str(self._context.sandbox_key), event_id=source_id,
                      invocation_id=invocation_id, tool_name=name)
        plugin_log(plugin, "tool.started", arguments=arguments, **fields)
        try:
            result = await provider.execute_tool(name, arguments, self._context, invocation_id)
        except Exception as error:
            plugin_log(plugin, "tool.failed", error_type=type(error).__name__,
                       duration_ms=round((time.monotonic() - started) * 1000), **fields)
            raise
        if not isinstance(result, ToolResult):
            raise TypeError("tool provider must return ToolResult")
        log_payload = dict(result.model_payload)
        if log_payload.get("instructions_only") is True:
            log_payload.pop("text", None)
        plugin_log(plugin, "tool.completed", result=log_payload, status=result.status,
                   duration_ms=round((time.monotonic() - started) * 1000), **fields)
        return result

    async def consume_native(
        self, events: Sequence[NativeToolEvent]
    ) -> tuple[ToolResult, ...]:
        if len(events) > MAX_NATIVE_EVENTS:
            raise ValueError("too many native tool events")
        grouped: dict[str, list[NativeToolEvent]] = {}
        call_ids: set[str] = set()
        for event in events:
            if event.call_id in call_ids:
                raise ValueError("duplicate native tool call ID")
            call_ids.add(event.call_id)
            if event.kind not in self._native:
                raise ValueError(f"unregistered native tool kind: {event.kind}")
            grouped.setdefault(event.kind, []).append(event)
        results = []
        for kind, selected in grouped.items():
            result = await self._native[kind].consume_native(tuple(selected), self._context)
            if not isinstance(result, ToolResult):
                raise TypeError("native tool consumer must return ToolResult")
            from anima.core.plugin_logs import plugin_log
            plugin_log(self.context_owners.get(kind, "core"), "native.completed",
                       sandbox_key=str(self._context.sandbox_key),
                       event_id=self._context.source.id if self._context.source else None,
                       tool_name=kind, result=result.model_payload, status=result.status)
            results.append(result)
        return tuple(results)


class ToolRegistry:
    def __init__(
        self, providers: Sequence[ToolProvider] = (), *,
        owners: Mapping[int, str] | None = None,
    ) -> None:
        self.providers = tuple(providers)
        self.owners = dict(owners or {})
        if any(not isinstance(provider, ToolProvider) for provider in self.providers):
            raise TypeError("tool providers must implement ToolProvider")

    async def prepare(self, context: CapabilityContext) -> PreparedToolSet:
        specs: list[ToolSpec] = []
        functions: dict[str, tuple[FunctionToolSpec, ToolProvider]] = {}
        native_kinds: set[str] = set()
        native: dict[str, NativeToolConsumer] = {}
        unavailable: dict[str, AvailabilityStatus] = {}
        contextual_instructions: list[str] = []
        context_owners: dict[str, str] = {}
        instruction_parts: list[tuple[str, str]] = []
        total_schema_bytes = 0
        for provider in self.providers:
            owner = self.owners.get(id(provider), "core")
            contribution = getattr(provider, "context_instructions", None)
            if contribution is not None:
                value = contribution(context)
                if not isinstance(value, str):
                    raise CapabilityConfigurationError(
                        "tool context instructions must be text"
                    )
                if value.strip():
                    contextual_instructions.append(value.strip())
                    parts = getattr(provider, "context_parts", None)
                    instruction_parts.extend(
                        parts(context) if parts else ((owner, value.strip()),)
                    )
            provided = await provider.tools(context)
            if not isinstance(provided, tuple):
                raise CapabilityConfigurationError("tool provider must return a tuple")
            for spec in provided:
                if not isinstance(spec, (FunctionToolSpec, NativeToolSpec)):
                    raise CapabilityConfigurationError("provider returned an invalid tool spec")
                capability_name = spec.name if isinstance(spec, FunctionToolSpec) else spec.kind
                if isinstance(provider, AvailabilityProvider):
                    status = await provider.capability_availability(
                        "tool", capability_name, context
                    )
                    if not isinstance(status, AvailabilityStatus):
                        raise TypeError(
                            "availability provider must return AvailabilityStatus"
                        )
                    if status.state is not Availability.AVAILABLE:
                        unavailable[capability_name] = status
                        continue
                if isinstance(spec, FunctionToolSpec):
                    if spec.name in functions:
                        raise CapabilityConfigurationError(f"duplicate tool name: {spec.name}")
                    functions[spec.name] = (spec, provider)
                    total_schema_bytes += len(
                        _canonical_json(spec.parameters).encode("utf-8")
                    )
                else:
                    if spec.kind in native_kinds:
                        raise CapabilityConfigurationError(
                            f"duplicate native tool kind: {spec.kind}"
                        )
                    native_kinds.add(spec.kind)
                    if not isinstance(provider, NativeToolConsumer):
                        raise CapabilityConfigurationError(
                            f"native tool provider has no consumer: {spec.kind}"
                        )
                    native[spec.kind] = provider
                specs.append(spec)
                context_owners[capability_name] = owner
                if len(specs) > MAX_TOOL_COUNT:
                    raise CapabilityConfigurationError("too many tools")
        if total_schema_bytes > MAX_TOTAL_SCHEMA_BYTES:
            raise CapabilityConfigurationError("total tool schema size is too large")
        return PreparedToolSet(
            tuple(specs), functions, context, native, unavailable,
            "\n\n".join(contextual_instructions),
            context_owners, tuple(instruction_parts),
        )


def _safe_name(value: str, label: str) -> None:
    if not SAFE_NAME.fullmatch(value):
        raise ValueError(f"{label} is invalid")


def _description(value: str) -> None:
    if not value.strip() or len(value) > MAX_DESCRIPTION_LENGTH:
        raise CapabilityConfigurationError("tool description is invalid")


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _validate_schema(schema: object, *, root: bool = False) -> None:
    if not isinstance(schema, Mapping):
        raise CapabilityConfigurationError("tool schema must be an object")
    unknown = set(schema) - _SCHEMA_KEYS
    if unknown:
        raise CapabilityConfigurationError(
            "unsupported schema keywords: " + ", ".join(sorted(unknown))
        )
    if "description" in schema:
        description = schema["description"]
        if not isinstance(description, str) or not description.strip():
            raise CapabilityConfigurationError("schema description is invalid")
        if len(description) > MAX_DESCRIPTION_LENGTH:
            raise CapabilityConfigurationError("schema description is too long")
    schema_type = schema.get("type")
    if schema_type not in _SCHEMA_TYPES:
        raise CapabilityConfigurationError("schema type is unsupported")
    if root and schema_type != "object":
        raise CapabilityConfigurationError("tool schema root must be an object")
    if "enum" in schema:
        enum = schema["enum"]
        if not isinstance(enum, list) or not enum:
            raise CapabilityConfigurationError("schema enum must be a non-empty list")
    if schema_type == "object":
        if schema.get("additionalProperties") is not False:
            raise CapabilityConfigurationError(
                "object schema must set additionalProperties to false"
            )
        properties = schema.get("properties")
        required = schema.get("required")
        if not isinstance(properties, Mapping) or not isinstance(required, list):
            raise CapabilityConfigurationError(
                "object schema requires properties and required"
            )
        if any(not isinstance(name, str) for name in required) or set(required) - set(properties):
            raise CapabilityConfigurationError("schema required fields are invalid")
        if len(required) != len(set(required)):
            raise CapabilityConfigurationError("schema required fields must be unique")
        for child in properties.values():
            _validate_schema(child)
    elif schema_type == "array":
        if "items" not in schema:
            raise CapabilityConfigurationError("array schema requires items")
        _validate_schema(schema["items"])
    for minimum, maximum in (
        ("minLength", "maxLength"), ("minItems", "maxItems"), ("minimum", "maximum")
    ):
        low, high = schema.get(minimum), schema.get(maximum)
        if low is not None and (not isinstance(low, (int, float)) or isinstance(low, bool)):
            raise CapabilityConfigurationError(f"{minimum} must be numeric")
        if high is not None and (not isinstance(high, (int, float)) or isinstance(high, bool)):
            raise CapabilityConfigurationError(f"{maximum} must be numeric")
        if low is not None and high is not None and low > high:
            raise CapabilityConfigurationError(f"{minimum} must not exceed {maximum}")


def _matches_schema(value: object, schema: Mapping[str, object]) -> bool:
    schema_type = schema["type"]
    if schema_type == "object":
        if not isinstance(value, dict):
            return False
        properties = schema["properties"]
        assert isinstance(properties, Mapping)
        required = schema["required"]
        assert isinstance(required, list)
        if set(required) - set(value) or set(value) - set(properties):
            return False
        return all(_matches_schema(item, properties[key]) for key, item in value.items())
    if schema_type == "array":
        if not isinstance(value, list):
            return False
        if not _within(len(value), schema.get("minItems"), schema.get("maxItems")):
            return False
        return all(_matches_schema(item, schema["items"]) for item in value)
    if schema_type == "string":
        matches = isinstance(value, str) and _within(
            len(value), schema.get("minLength"), schema.get("maxLength")
        )
    elif schema_type == "integer":
        matches = isinstance(value, int) and not isinstance(value, bool)
    elif schema_type == "number":
        matches = isinstance(value, (int, float)) and not isinstance(value, bool)
    else:
        matches = isinstance(value, bool)
    if not matches:
        return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    if schema_type in {"integer", "number"} and not _within(
        value, schema.get("minimum"), schema.get("maximum")
    ):
        return False
    return True


def _within(value: int | float, low: object, high: object) -> bool:
    return (low is None or value >= low) and (high is None or value <= high)


def normalized_text(value: str) -> str:
    """Shared deterministic normalization reserved for local capability indexes."""
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())
