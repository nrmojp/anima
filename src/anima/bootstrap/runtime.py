"""Lifecycle owner for sandbox-scoped plugin runtimes."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from anima.capabilities.commands import CommandRegistry
from anima.capabilities.plugins import PluginCatalog, PluginRuntime
from anima.capabilities.tools import ToolRegistry
from anima.capabilities.responses import ResponseContributionRegistry
from anima.core.sandbox import SandboxKey
from anima.core.services import SandboxServices


@dataclass(frozen=True, slots=True)
class SandboxRuntime:
    key: SandboxKey
    plugins: PluginRuntime
    services: SandboxServices
    started_services: tuple[object, ...] = ()

    @property
    def tools(self) -> ToolRegistry:
        return self.plugins.tools

    @property
    def commands(self) -> CommandRegistry:
        return self.plugins.commands

    @property
    def responses(self) -> ResponseContributionRegistry:
        return self.plugins.responses

    def snapshot(self) -> Mapping[str, object]:
        return {"sandbox": str(self.key), "plugins": self.plugins.snapshots()}


class RuntimeHost:
    """Creates and owns exactly one plugin runtime for each active sandbox."""

    def __init__(
        self,
        catalog: PluginCatalog,
        enabled: frozenset[str],
        service_factory: Callable[[SandboxKey], SandboxServices],
        configuration: Mapping[str, Mapping[str, object]] | None = None,
    ) -> None:
        self.catalog = catalog
        self.enabled = enabled
        self.service_factory = service_factory
        self.configuration = configuration or {}
        self._runtimes: dict[SandboxKey, SandboxRuntime] = {}

    async def start_sandbox(self, key: SandboxKey) -> SandboxRuntime:
        existing = self._runtimes.get(key)
        if existing is not None:
            return existing
        services = self.service_factory(key)
        if services.sandbox_key != key:
            raise ValueError("service factory returned a different sandbox")
        plugins = self.catalog.create(self.enabled, services, self.configuration)
        started_services = []
        try:
            for service in services.lifecycles():
                await service.start()
                started_services.append(service)
            await plugins.start()
        except BaseException:
            await plugins.stop()
            while started_services:
                await started_services.pop().stop()
            raise
        runtime = SandboxRuntime(key, plugins, services, tuple(started_services))
        self._runtimes[key] = runtime
        return runtime

    def sandbox(self, key: SandboxKey) -> SandboxRuntime | None:
        return self._runtimes.get(key)

    async def stop_sandbox(self, key: SandboxKey) -> None:
        runtime = self._runtimes.pop(key, None)
        if runtime is not None:
            await runtime.plugins.stop()
            for service in reversed(runtime.started_services):
                await service.stop()

    async def stop(self) -> None:
        for key in tuple(reversed(self._runtimes)):
            await self.stop_sandbox(key)

    def snapshots(self) -> tuple[Mapping[str, object], ...]:
        return tuple(runtime.snapshot() for runtime in self._runtimes.values())
