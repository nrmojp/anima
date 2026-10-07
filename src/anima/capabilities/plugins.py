"""Trusted in-process plugin catalog and lifecycle."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import inspect
import json
from pathlib import Path
import re
from typing import Protocol

from anima.capabilities.commands import CommandProvider, CommandRegistry
from anima.capabilities.configuration import ConfigField, resolve_configuration
from anima.capabilities.tools import ToolProvider, ToolRegistry
from anima.capabilities.responses import ResponseContributionProvider, ResponseContributionRegistry
from anima.core.services import SandboxServices
from anima.core.resources import ResourceRegistration


_SAFE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


@dataclass(frozen=True, slots=True)
class DashboardPanelSpec:
    """Declarative contribution rendered by a trusted dashboard renderer."""

    id: str
    title: str
    renderer: str = "status"
    order: int = 100
    description: str = ""
    eyebrow: str = "PLUGIN"
    group: str = "status"

    def __post_init__(self) -> None:
        if not _SAFE.fullmatch(self.id) or not _SAFE.fullmatch(self.renderer):
            raise ValueError("dashboard panel ID is invalid")
        if self.group not in {"status", "memory", "configuration", "diagnostics"}:
            raise ValueError("dashboard panel group is invalid")
        if not self.title.strip() or len(self.title) > 80:
            raise ValueError("dashboard panel title is invalid")
        if not 0 <= self.order <= 1_000:
            raise ValueError("dashboard panel order is invalid")
        if len(self.description) > 240:
            raise ValueError("dashboard panel description is invalid")
        if not self.eyebrow.strip() or len(self.eyebrow) > 80:
            raise ValueError("dashboard panel eyebrow is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id, "title": self.title, "renderer": self.renderer,
            "order": self.order, "description": self.description,
            "eyebrow": self.eyebrow,
            "group": self.group,
        }


@dataclass(frozen=True, slots=True)
class PluginManifest:
    name: str
    version: str
    provides: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    description: str = ""
    configuration: tuple[ConfigField, ...] = ()
    dashboard_panels: tuple[DashboardPanelSpec, ...] = ()
    after: tuple[str, ...] = ()
    default_enabled: bool = False
    enabled_env: str = ""
    uses_audio: bool = False

    def __post_init__(self) -> None:
        if not _SAFE.fullmatch(self.name):
            raise ValueError("plugin name is invalid")
        if not self.version or len(self.version) > 32:
            raise ValueError("plugin version is invalid")
        if len(self.description) > 240:
            raise ValueError("plugin description is invalid")
        capabilities = (*self.provides, *self.requires)
        if any(not _SAFE.fullmatch(value) for value in self.after):
            raise ValueError("plugin ordering ID is invalid")
        if type(self.default_enabled) is not bool or (self.enabled_env and not re.fullmatch(r"[A-Z][A-Z0-9_]*", self.enabled_env)):
            raise ValueError("plugin activation default is invalid")
        if type(self.uses_audio) is not bool:
            raise ValueError("plugin audio declaration is invalid")
        if any(not _SAFE.fullmatch(value) for value in capabilities):
            raise ValueError("plugin capability ID is invalid")
        if len(self.provides) != len(set(self.provides)) or len(self.requires) != len(set(self.requires)):
            raise ValueError("plugin capabilities must be unique")
        keys = [field.key for field in self.configuration]
        if len(keys) != len(set(keys)):
            raise ValueError("plugin configuration fields must be unique")
        if any(not isinstance(panel, DashboardPanelSpec) for panel in self.dashboard_panels):
            raise ValueError("plugin dashboard panel is invalid")
        panel_ids = [panel.id for panel in self.dashboard_panels]
        if len(panel_ids) != len(set(panel_ids)):
            raise ValueError("plugin dashboard panels must be unique")


class PluginInstance(Protocol):
    manifest: PluginManifest
    tool_providers: tuple[ToolProvider, ...]
    command_providers: tuple[CommandProvider, ...]
    resource_registrations: tuple[ResourceRegistration, ...]

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def snapshot(self) -> Mapping[str, object]: ...


class PluginDefinition(Protocol):
    manifest: PluginManifest

    def create(
        self, sandbox_services: SandboxServices, configuration: Mapping[str, object]
    ) -> PluginInstance: ...


class PluginCatalog:
    def __init__(self, definitions: Sequence[PluginDefinition]) -> None:
        self._definitions = {definition.manifest.name: definition for definition in definitions}
        if len(self._definitions) != len(definitions):
            raise ValueError("duplicate plugin name")
        providers: dict[str, str] = {}
        panels: dict[str, str] = {}
        for definition in definitions:
            for capability in definition.manifest.provides:
                if capability in providers:
                    raise ValueError(f"duplicate capability provider: {capability}")
                providers[capability] = definition.manifest.name
            for panel in definition.manifest.dashboard_panels:
                if panel.id in panels:
                    raise ValueError(f"duplicate dashboard panel: {panel.id}")
                panels[panel.id] = definition.manifest.name
        if any(required not in providers for definition in definitions for required in definition.manifest.requires):
            raise ValueError("unknown plugin dependency")
        self._ordered = self._resolve()

    @property
    def manifests(self) -> tuple[PluginManifest, ...]:
        return tuple(self._definitions[name].manifest for name in self._ordered)

    @property
    def definitions(self) -> tuple[PluginDefinition, ...]:
        return tuple(self._definitions[name] for name in self._ordered)

    def create(
        self,
        enabled: frozenset[str],
        sandbox_services: SandboxServices,
        configuration: Mapping[str, Mapping[str, object]] | None = None,
    ) -> "PluginRuntime":
        unknown = enabled - set(self._definitions)
        if unknown:
            raise ValueError("unknown enabled plugins: " + ", ".join(sorted(unknown)))
        values = configuration or {}
        unknown_configuration = set(values) - set(self._definitions)
        if unknown_configuration:
            raise ValueError(
                "configuration for unknown plugins: "
                + ", ".join(sorted(unknown_configuration))
            )
        instances: list[PluginInstance] = []
        provided: set[str] = set()
        for name in self._ordered:
            if name not in enabled:
                continue
            definition = self._definitions[name]
            missing = set(definition.manifest.requires) - provided
            if missing:
                raise ValueError(f"enabled plugin {name} requires: " + ", ".join(sorted(missing)))
            config = resolve_configuration(
                definition.manifest.configuration, dict(values.get(name, {}))
            )
            instances.append(definition.create(sandbox_services.for_plugin(name), config))
            provided.update(definition.manifest.provides)
        status_path = sandbox_services.values.get("plugin_status_path")
        if status_path is not None and not isinstance(status_path, Path):
            raise TypeError("plugin_status_path must be a Path")
        return PluginRuntime(
            tuple(instances), status_path=status_path, configured=enabled,
        )

    def _resolve(self) -> tuple[str, ...]:
        pending = list(self._definitions)
        ordered: list[str] = []
        provided: set[str] = set()
        while pending:
            ready = [
                name for name in pending
                if set(self._definitions[name].manifest.requires) <= provided
                and not (set(self._definitions[name].manifest.after) & set(pending))
            ]
            if not ready:
                raise ValueError("cyclic plugin dependencies")
            for name in ready:
                pending.remove(name)
                ordered.append(name)
                provided.update(self._definitions[name].manifest.provides)
        return tuple(ordered)


class PluginRuntime:
    def __init__(
        self, instances: tuple[PluginInstance, ...], *,
        status_path: Path | None = None, configured: frozenset[str] = frozenset(),
    ) -> None:
        self.instances = instances
        self._started: list[PluginInstance] = []
        self.status_path = status_path
        self.configured = configured or frozenset(
            instance.manifest.name for instance in instances
        )

    @property
    def tools(self) -> ToolRegistry:
        return ToolRegistry(tuple(provider for instance in self.instances for provider in instance.tool_providers), owners={
            id(provider): instance.manifest.name
            for instance in self.instances for provider in instance.tool_providers
        })

    @property
    def commands(self) -> CommandRegistry:
        return CommandRegistry(tuple(provider for instance in self.instances for provider in instance.command_providers))

    @property
    def responses(self) -> ResponseContributionRegistry:
        providers = tuple(
            instance for instance in self.instances
            if isinstance(instance, ResponseContributionProvider)
        ) + tuple(provider for instance in self.instances
                  for provider in getattr(instance, "response_providers", ()))
        return ResponseContributionRegistry(providers)

    @property
    def resources(self) -> tuple[ResourceRegistration, ...]:
        return tuple(
            registration for instance in self.instances
            for registration in instance.resource_registrations
        )

    async def start(self) -> None:
        if self._started:
            return
        try:
            for instance in self.instances:
                await instance.start()
                self._started.append(instance)
        except BaseException:
            await self.stop()
            raise
        self._write_status()

    async def stop(self) -> None:
        while self._started:
            await self._started.pop().stop()
        self._write_status()

    def snapshots(self) -> tuple[Mapping[str, object], ...]:
        snapshots = []
        for instance in self.instances:
            value = dict(instance.snapshot())
            value.update({
                "name": instance.manifest.name,
                "version": instance.manifest.version,
                "description": instance.manifest.description,
                "provides": instance.manifest.provides,
                "dashboard_panels": tuple(
                    panel.to_dict() for panel in instance.manifest.dashboard_panels
                ),
            })
            snapshots.append(value)
        return tuple(snapshots)

    def _write_status(self) -> None:
        if self.status_path is None:
            return
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.status_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"plugins": self.snapshots()}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.status_path)


class BoundPlugin:
    """Generic instance whose facets are selected by one Plugin definition."""

    def __init__(
        self,
        manifest: PluginManifest,
        *,
        tool_providers: tuple[ToolProvider, ...] = (),
        command_providers: tuple[CommandProvider, ...] = (),
        resource_registrations: tuple[ResourceRegistration, ...] = (),
        start: object | None = None,
        stop: object | None = None,
        available: object = True,
    ) -> None:
        self.manifest = manifest
        self.tool_providers = tool_providers
        self.command_providers = command_providers
        self.resource_registrations = resource_registrations
        self._start = start
        self._stop = stop
        self._available = available
        self.running = False

    async def start(self) -> None:
        if self.running:
            return
        if self._start is not None:
            result = self._start()  # type: ignore[operator]
            if inspect.isawaitable(result):
                await result
        self.running = True

    async def stop(self) -> None:
        if not self.running:
            return
        self.running = False
        if self._stop is not None:
            result = self._stop()  # type: ignore[operator]
            if inspect.isawaitable(result):
                await result

    def snapshot(self) -> Mapping[str, object]:
        available = self._available() if callable(self._available) else self._available
        return {
            "enabled": True,
            "available": bool(available),
            "running": self.running,
            "configuration_fields": tuple(
                field.public() for field in self.manifest.configuration
            ),
        }


class UnavailablePlugin:
    """Lifecycle-safe placeholder when an enabled Plugin is unsupported here."""

    def __init__(self, manifest: PluginManifest) -> None:
        self.manifest = manifest
        self.tool_providers = ()
        self.command_providers = ()
        self.resource_registrations = ()
        self.running = False

    async def start(self) -> None:
        self.running = True

    async def stop(self) -> None:
        self.running = False

    def snapshot(self) -> Mapping[str, object]:
        return {"enabled": True, "available": False, "running": self.running}
