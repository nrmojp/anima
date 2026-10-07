"""Self-contained capability implementation."""

from __future__ import annotations

from collections.abc import Mapping
import json
from anima.capabilities.contracts import ActionRecord, CapabilityConfigurationError, FunctionToolSpec


def _function_specs(
    definitions: list[dict[str, object]], *, side_effects: frozenset[str],
    requires_source: bool | frozenset[str] = True,
) -> tuple[FunctionToolSpec, ...]:
    result = []
    for definition in definitions:
        if definition.get("type") != "function":
            raise CapabilityConfigurationError("legacy provider returned a non-function tool")
        try:
            result.append(FunctionToolSpec(
                name=str(definition["name"]),
                description=str(definition["description"]),
                parameters=definition["parameters"],
                strict=definition.get("strict") is True,
                side_effect=str(definition["name"]) in side_effects,
                requires_source=(
                    requires_source
                    if isinstance(requires_source, bool)
                    else str(definition["name"]) in requires_source
                ),
            ))
        except KeyError as error:
            raise CapabilityConfigurationError(
                f"legacy tool definition is missing {error.args[0]}"
            ) from error
    return tuple(result)

def _payload(output: str) -> Mapping[str, object]:
    try:
        value = json.loads(output)
    except (TypeError, json.JSONDecodeError) as error:
        raise TypeError("legacy tool output must be valid JSON") from error
    if not isinstance(value, dict):
        raise TypeError("legacy tool output must be a JSON object")
    return value

def _status(payload: Mapping[str, object]) -> str:
    return "rejected" if payload.get("ok") is False or "error" in payload else "success"

def _actions(plugin: str, name: str, status: str) -> tuple[ActionRecord, ...]:
    return (
        (ActionRecord(plugin, name, f"{name}を実行した"),)
        if status == "success"
        else ()
    )

def _json(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
