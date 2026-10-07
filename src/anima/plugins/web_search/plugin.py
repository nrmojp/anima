"""Web-search capability entry point."""

from collections.abc import Mapping
from dataclasses import dataclass

from anima.capabilities.plugins import BoundPlugin, DashboardPanelSpec, PluginManifest
from anima.core.services import SandboxServices

MANIFEST = PluginManifest(
    "web_search", "1.0.0", provides=("web_search",),
    enabled_env="ANIMA_ENABLE_WEB_SEARCH",
    description="必要に応じてWebを検索し、根拠付きの応答を支援します。",
    dashboard_panels=(DashboardPanelSpec(
        "research", "直近の検索メモ", "research", 70,
        "直近のWeb検索で参照した要約・検索語・出典を表示します。",
        "RESEARCH",
        group="memory",
    ),),
)


@dataclass(frozen=True, slots=True)
class WebSearchPluginDefinition:
    manifest: PluginManifest = MANIFEST

    def create(self, services: SandboxServices, configuration: Mapping[str, object]):
        environment = services.values.get("assembly")
        if environment is not None:
            return BoundPlugin(self.manifest, tool_providers=(environment.get("web_search_provider"),),
                               available=environment.get("web_search_enabled", False))
        del configuration
        provider = services.values.get("tool_provider")
        return BoundPlugin(
            self.manifest,
            tool_providers=(provider,) if provider is not None else (),
            available=provider is not None and bool(services.values.get("available", False)),
        )


PLUGIN = WebSearchPluginDefinition()
