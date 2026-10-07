"""Declarative non-secret configuration fields contributed by core and plugins."""

from __future__ import annotations

from dataclasses import dataclass
import math
from collections.abc import Mapping


@dataclass(frozen=True, slots=True)
class ConfigField:
    key: str
    env: str
    attribute: str
    label: str
    category: str
    kind: str
    default: object
    minimum: float | None = None
    maximum: float | None = None
    options: tuple[str, ...] = ()
    plugin: str | None = None

    def __post_init__(self) -> None:
        if not self.key or not self.env or not self.attribute or not self.label or not self.category:
            raise ValueError("configuration field identity is invalid")
        if self.kind not in {"bool", "list", "multi", "text", "int", "float"}:
            raise ValueError("configuration field kind is invalid")
        if any(secret in self.env for secret in ("TOKEN", "KEY", "SECRET", "PASSWORD")):
            raise ValueError("secret configuration fields are not allowed")

    def public(self) -> dict[str, object]:
        return {
            "key": self.key, "label": self.label, "category": self.category,
            "kind": self.kind, "default": self.default, "minimum": self.minimum,
            "maximum": self.maximum, "options": self.options, "plugin": self.plugin,
        }

    def validate(self, value: object) -> object:
        """Validate one resolved value without exposing environment details."""
        if self.kind == "bool":
            valid = isinstance(value, bool)
        elif self.kind in {"list", "multi"}:
            valid = isinstance(value, (tuple, list)) and all(
                isinstance(item, str) for item in value
            )
        elif self.kind == "text":
            valid = isinstance(value, str)
        elif self.kind == "int":
            valid = isinstance(value, int) and not isinstance(value, bool)
        else:
            valid = isinstance(value, (int, float)) and not isinstance(value, bool)
        if not valid:
            raise ValueError(f"invalid value for configuration field {self.key}")
        if self.options:
            selected = value if isinstance(value, (tuple, list)) else (value,)
            if any(item not in self.options for item in selected):
                raise ValueError(f"invalid value for configuration field {self.key}")
        if isinstance(value, (int, float)):
            if not math.isfinite(value):
                raise ValueError(f"invalid value for configuration field {self.key}")
            if self.minimum is not None and value < self.minimum:
                raise ValueError(f"invalid value for configuration field {self.key}")
            if self.maximum is not None and value > self.maximum:
                raise ValueError(f"invalid value for configuration field {self.key}")
        return value


def resolve_configuration(
    fields: tuple[ConfigField, ...], values: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Resolve a Plugin's non-secret values against its declared fields."""
    supplied = dict(values or {})
    known = {field.key for field in fields}
    if set(supplied) - known:
        raise ValueError("unknown configuration fields")
    return {
        field.key: field.validate(supplied.get(field.key, field.default))
        for field in fields
    }
