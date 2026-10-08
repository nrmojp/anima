"""Deterministic discovery of trusted in-tree plugins."""

from __future__ import annotations

from importlib import import_module
from importlib.util import find_spec
import pkgutil
import os
from pathlib import Path

from anima.capabilities.plugins import PluginCatalog, PluginDefinition


class PluginLoader:
    """Load plugin entry points from immediate child packages of a namespace."""

    def __init__(self, namespace: str | None = None) -> None:
        if namespace is None:
            namespace = os.environ.get("ANIMA_PLUGIN_NAMESPACE", "anima.plugins")
        if not namespace or any(not part.isidentifier() for part in namespace.split(".")):
            raise ValueError("plugin namespace is invalid")
        self.namespace = namespace

    def load(self) -> PluginCatalog:
        package = import_module(self.namespace)
        paths = getattr(package, "__path__", None)
        if paths is None:
            raise ValueError("plugin namespace must be a package")

        definitions: list[PluginDefinition] = []
        skill_roots: dict[str, Path] = {}
        candidates = sorted(pkgutil.iter_modules(paths), key=lambda candidate: candidate.name)
        for candidate in candidates:
            if not candidate.ispkg:
                continue
            entry_name = f"{self.namespace}.{candidate.name}.plugin"
            if find_spec(entry_name) is None:
                continue
            entry = import_module(entry_name)
            definition = getattr(entry, "PLUGIN", None)
            if definition is None:
                raise ValueError(f"plugin entry point has no PLUGIN: {entry_name}")
            if definition.manifest.name != candidate.name:
                raise ValueError(
                    f"plugin directory and manifest name differ: {candidate.name}"
                )
            definitions.append(definition)
            if getattr(entry, "__file__", None):
                skill_roots[definition.manifest.name] = Path(entry.__file__).parent / "skills"
        return PluginCatalog(tuple(definitions), skill_roots=skill_roots)
