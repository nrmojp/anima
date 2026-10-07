"""Sandbox-bound service bundle assembled by a host application."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, TypeVar, runtime_checkable

from anima.core.sandbox import SandboxKey
from anima.core.storage import PluginStorageFactory


T = TypeVar("T")


@runtime_checkable
class SandboxServiceLifecycle(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...


@dataclass(frozen=True, slots=True)
class SandboxServices:
    """Explicit services that one sandbox's plugins may use.

    A host should expose narrow ports here, never an unrestricted SDK client or
    global state store. The bundle itself is immutable; service implementations are
    responsible for enforcing their own mutability rules.
    """

    sandbox_key: SandboxKey
    values: Mapping[str, object] = field(default_factory=dict)

    def require(self, name: str, expected_type: type[T]) -> T:
        service = self.values.get(name)
        if service is None:
            raise LookupError(f"sandbox service is unavailable: {name}")
        if not isinstance(service, expected_type):
            raise TypeError(f"sandbox service has the wrong type: {name}")
        return service

    def for_plugin(self, plugin_id: str) -> "SandboxServices":
        """Bind optional shared factories before exposing services to a plugin."""
        values = dict(self.values)
        factory = values.pop("plugin_storage_factory", None)
        if factory is not None:
            if not isinstance(factory, PluginStorageFactory):
                raise TypeError("sandbox service has the wrong type: plugin_storage_factory")
            values["plugin_storage"] = factory.for_plugin(plugin_id)
        plugin_services = values.pop("plugin_services", None)
        if plugin_services is not None:
            if not isinstance(plugin_services, Mapping):
                raise TypeError("sandbox service has the wrong type: plugin_services")
            scoped = plugin_services.get(plugin_id, {})
            if not isinstance(scoped, Mapping):
                raise TypeError("plugin services must be a mapping")
            values.update(scoped)
        return SandboxServices(self.sandbox_key, values)

    def lifecycles(self) -> tuple[SandboxServiceLifecycle, ...]:
        selected: list[SandboxServiceLifecycle] = []
        seen: set[int] = set()
        for service in self.values.values():
            if isinstance(service, SandboxServiceLifecycle) and id(service) not in seen:
                selected.append(service)
                seen.add(id(service))
        return tuple(selected)
