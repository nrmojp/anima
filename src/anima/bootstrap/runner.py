"""Generic foreground application launcher with optional plugin-owned services."""

from pathlib import Path

from anima.bootstrap.settings import load_dotenv, selected_plugins
from anima.bootstrap.deployment import deployment_defaults
from anima.capabilities.plugin_loader import PluginLoader


def run_local(*, cwd=None, environ=None, app_main=None, **options):
    base = (cwd or Path.cwd()).resolve()
    values = {**deployment_defaults(base), **load_dotenv(base / ".env", environ=environ)}
    if app_main is None:
        from anima.bootstrap.app import main as app_main
    enabled = selected_plugins(values)
    entry = app_main
    for definition in reversed(PluginLoader().load().definitions):
        launcher = getattr(definition, "run_application", None)
        if definition.manifest.name in enabled and launcher is not None:
            downstream = entry
            entry = lambda launcher=launcher, downstream=downstream: launcher(
                downstream, cwd=base, environ=values, **options,
            )
    entry()
