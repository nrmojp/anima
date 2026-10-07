"""Configuration loaded from process environment and a local .env file."""

from __future__ import annotations

import os
import math
import re
from dataclasses import dataclass, field
import json
from anima.capabilities.configuration import resolve_configuration
from anima.capabilities.plugin_loader import PluginLoader
from pathlib import Path

from anima.bootstrap.runtime_config import DISCOVERED_PLUGINS, merge_runtime_config
from anima.bootstrap.deployment import deployment_defaults
from anima.core.environment import ConfigurationError, load_dotenv




KNOWN_PLUGINS = frozenset(DISCOVERED_PLUGINS)


def _plugin_configuration(root: Path, values: dict[str, str]) -> dict[str, dict]:
    try:
        return _raw_plugin_configuration(root, values)
    except (ValueError, TypeError) as error:
        raise ConfigurationError(str(error)) from error


def _raw_plugin_configuration(root: Path, values: dict[str, str]) -> dict[str, dict]:
    result = {}
    for manifest in PluginLoader().load().manifests:
        path = root / "plugins" / f"{manifest.name}.json"
        supplied = {}
        if path.is_symlink():
            raise ConfigurationError("unsafe plugin configuration")
        if path.exists():
            if not path.is_file():
                raise ConfigurationError("unsafe plugin configuration")
            supplied = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(supplied, dict):
                raise ConfigurationError("plugin configuration must be an object")
        for item in manifest.configuration:
            if item.env not in values:
                continue
            raw = values[item.env]
            if item.kind == "bool":
                value = _parse_bool(raw)
            elif item.kind == "int":
                value = int(raw)
            elif item.kind == "float":
                value = float(raw)
            elif item.kind in {"list", "multi"}:
                value = [part.strip() for part in raw.split(",") if part.strip()]
            else:
                value = raw
            supplied[item.key] = value
        result[manifest.name] = resolve_configuration(manifest.configuration, supplied)
    return result




@dataclass(frozen=True, slots=True)
class Settings:
    discord_bot_token: str
    openai_api_key: str
    openai_model: str
    openai_reflection_model: str
    anima_root: Path
    state_root: Path
    recent_limit: int
    enable_web_search: bool
    openai_response_timeout_seconds: float
    openai_maintenance_timeout_seconds: float
    discord_send_timeout_seconds: float
    openai_max_retries: int
    digest_max_lines: int
    memory_max_lines: int
    event_max_retries: int
    retry_base_delay_seconds: float
    persona_queue_size: int
    memory_strong_max: int
    log_retention_days: int
    archive_retention_days: int
    inventory_max_items: int
    inventory_max_bytes: int
    inventory_temporary_retention_hours: int
    operational_log_retention_days: int
    shutdown_timeout_seconds: float
    dashboard_host: str
    dashboard_port: int
    proactive_daily_limit: int = 4
    proactive_cooldown_seconds: int = 1800
    bot_loop_window_seconds: int = 600
    bot_loop_max_speaks: int = 2
    self_time_interval_seconds: int = 21600
    self_time_daily_limit: int = 2
    self_time_max_iterations: int = 4
    allowed_guild_ids: frozenset[str] = frozenset()
    dm_enabled: bool = False
    plugins: frozenset[str] = frozenset()
    dashboard_admin_token: str = ""
    persona_names: tuple[str, ...] = ()
    command_prefix: str = "anima"
    plugin_configuration: dict = field(default_factory=dict)
    proactive_decision_daily_limit: int = 100

    def __getattr__(self, name):
        # Compatibility access is derived from declarations, never concrete feature names.
        for manifest in PluginLoader().load().manifests:
            if name == manifest.name + "_enabled":
                return manifest.name in self.plugins
            for item in manifest.configuration:
                if item.attribute == name:
                    return self.plugin_configuration.get(manifest.name, {}).get(item.key, item.default)
        raise AttributeError(name)

    @classmethod
    def load(cls, *, cwd: Path | None = None, environ: dict[str, str] | None = None) -> Settings:
        base = (cwd or Path.cwd()).resolve()
        values = load_dotenv(base / ".env", environ=environ)
        values = {**deployment_defaults(base), **values}
        missing = [key for key in ("DISCORD_BOT_TOKEN", "OPENAI_API_KEY") if not values.get(key)]
        if missing:
            raise ConfigurationError("missing required environment variables: " + ", ".join(missing))
        placeholders = [
            key
            for key in ("DISCORD_BOT_TOKEN", "OPENAI_API_KEY")
            if values.get(key, "").strip().lower() in {"replace-me", "changeme"}
        ]
        if placeholders:
            raise ConfigurationError(
                "replace placeholder values for: " + ", ".join(placeholders)
            )
        recent_limit = _positive_int(values, "ANIMA_RECENT_LIMIT", 30)
        root_value = Path(values.get("ANIMA_ROOT", "config"))
        root = root_value if root_value.is_absolute() else base / root_value
        values = merge_runtime_config(values, root / "config.json")
        state_value = Path(values.get("ANIMA_STATE_ROOT", "../state"))
        state_root = state_value if state_value.is_absolute() else root / state_value
        plugins = selected_plugins(values)
        return cls(
            command_prefix=_command_prefix(values.get("ANIMA_COMMAND_PREFIX", "anima")),
            plugin_configuration=_plugin_configuration(root, values),
            proactive_decision_daily_limit=_positive_int(values, "ANIMA_PROACTIVE_DECISION_DAILY_LIMIT", 100),
            persona_names=tuple(name.strip() for name in values.get("ANIMA_PERSONA_NAMES", "").split(",") if name.strip()),
            discord_bot_token=values["DISCORD_BOT_TOKEN"],
            allowed_guild_ids=_guild_ids(values.get("ANIMA_ALLOWED_GUILD_IDS", "")),
            dm_enabled=_parse_bool(values.get("ANIMA_DM_ENABLED", "false")),
            proactive_daily_limit=_positive_int(
                values, "ANIMA_PROACTIVE_DAILY_LIMIT", 4
            ),
            proactive_cooldown_seconds=_positive_int(
                values, "ANIMA_PROACTIVE_COOLDOWN_SECONDS", 1800
            ),
            bot_loop_window_seconds=_positive_int(
                values, "ANIMA_BOT_LOOP_WINDOW_SECONDS", 600
            ),
            bot_loop_max_speaks=_positive_int(
                values, "ANIMA_BOT_LOOP_MAX_SPEAKS", 2
            ),
            self_time_interval_seconds=_positive_int(
                values, "ANIMA_SELF_TIME_INTERVAL_SECONDS", 21600
            ),
            self_time_daily_limit=_positive_int(
                values, "ANIMA_SELF_TIME_DAILY_LIMIT", 2
            ),
            self_time_max_iterations=_positive_int(
                values, "ANIMA_SELF_TIME_MAX_ITERATIONS", 4
            ),
            openai_api_key=values["OPENAI_API_KEY"],
            openai_model=values.get("OPENAI_MODEL", "gpt-6-luna"),
            openai_reflection_model=values.get(
                "OPENAI_REFLECTION_MODEL", "gpt-6-sol"
            ),
            anima_root=root.resolve(),
            state_root=state_root.resolve(),
            recent_limit=recent_limit,
            enable_web_search=_parse_bool(values.get("ANIMA_ENABLE_WEB_SEARCH", "false")),
            openai_response_timeout_seconds=_positive_float(
                values, "OPENAI_RESPONSE_TIMEOUT_SECONDS", 45.0
            ),
            openai_maintenance_timeout_seconds=_positive_float(
                values, "OPENAI_MAINTENANCE_TIMEOUT_SECONDS", 300.0
            ),
            discord_send_timeout_seconds=_positive_float(
                values, "DISCORD_SEND_TIMEOUT_SECONDS", 15.0
            ),
            openai_max_retries=_nonnegative_int(values, "OPENAI_MAX_RETRIES", 2),
            digest_max_lines=_positive_int(values, "ANIMA_DIGEST_MAX_LINES", 30),
            memory_max_lines=_positive_int(values, "ANIMA_MEMORY_MAX_LINES", 80),
            event_max_retries=_nonnegative_int(values, "ANIMA_EVENT_MAX_RETRIES", 2),
            retry_base_delay_seconds=_positive_float(
                values, "ANIMA_RETRY_BASE_DELAY_SECONDS", 0.5
            ),
            persona_queue_size=_positive_int(values, "ANIMA_PERSONA_QUEUE_SIZE", 100),
            memory_strong_max=_positive_int(values, "ANIMA_MEMORY_STRONG_MAX", 10),
            log_retention_days=_positive_int(values, "ANIMA_LOG_RETENTION_DAYS", 30),
            archive_retention_days=_positive_int(
                values, "ANIMA_ARCHIVE_RETENTION_DAYS", 365
            ),
            inventory_max_items=_positive_int(
                values, "ANIMA_INVENTORY_MAX_ITEMS", 500
            ),
            inventory_max_bytes=_positive_int(
                values, "ANIMA_INVENTORY_MAX_BYTES", 268435456
            ),
            inventory_temporary_retention_hours=_positive_int(
                values, "ANIMA_INVENTORY_TEMPORARY_RETENTION_HOURS", 24
            ),
            operational_log_retention_days=_positive_int(
                values, "ANIMA_OPERATIONAL_LOG_RETENTION_DAYS", 14
            ),
            shutdown_timeout_seconds=_positive_float(
                values, "ANIMA_SHUTDOWN_TIMEOUT_SECONDS", 30.0
            ),
            dashboard_host=_dashboard_host(values.get("ANIMA_DASHBOARD_HOST", "127.0.0.1")),
            dashboard_port=_port(values, "ANIMA_DASHBOARD_PORT", 8765),
            dashboard_admin_token=values.get("ANIMA_DASHBOARD_ADMIN_TOKEN", "").strip(),
            plugins=plugins,
        )


def selected_plugins(values: dict[str, str]) -> frozenset[str]:
    defaults = frozenset(manifest.name for manifest in PluginLoader().load().manifests
        if (_parse_bool(values.get(manifest.enabled_env, str(manifest.default_enabled)))
            if manifest.enabled_env else manifest.default_enabled))
    return _plugins(values.get("ANIMA_PLUGINS"), defaults)


def _command_prefix(value: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,19}", value):
        raise ConfigurationError("ANIMA_COMMAND_PREFIX must be a safe command prefix of up to 20 characters")
    return value


def _guild_ids(value: str) -> frozenset[str]:
    if not value.strip():
        return frozenset()
    ids = [part.strip() for part in value.split(",")]
    if any(not re.fullmatch(r"[1-9][0-9]{0,19}", item) or int(item) >= 2**64 for item in ids):
        raise ConfigurationError("ANIMA_ALLOWED_GUILD_IDS must contain comma-separated positive Discord IDs")
    return frozenset(ids)


def _plugins(value: str | None, default: frozenset[str]) -> frozenset[str]:
    if value is None:
        return default
    selected = frozenset(part.strip() for part in value.split(",") if part.strip())
    unknown = selected - KNOWN_PLUGINS
    if unknown:
        raise ConfigurationError("ANIMA_PLUGINS contains unknown plugins: " + ", ".join(sorted(unknown)))
    return selected


def _dashboard_host(value: str) -> str:
    host = value.strip()
    if host not in {"0.0.0.0", "127.0.0.1"}:
        raise ConfigurationError("ANIMA_DASHBOARD_HOST must be 0.0.0.0 or 127.0.0.1")
    return host


def _port(values: dict[str, str], key: str, default: int) -> int:
    value = _positive_int(values, key, default)
    if value > 65535:
        raise ConfigurationError(f"{key} must be at most 65535")
    return value


def _parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"invalid boolean value: {value}")


def _positive_int(values: dict[str, str], key: str, default: int) -> int:
    value = _nonnegative_int(values, key, default)
    if value < 1:
        raise ConfigurationError(f"{key} must be at least 1")
    return value


def _nonnegative_int(values: dict[str, str], key: str, default: int) -> int:
    try:
        value = int(values.get(key, str(default)))
    except ValueError as error:
        raise ConfigurationError(f"{key} must be an integer") from error
    if value < 0:
        raise ConfigurationError(f"{key} must be at least 0")
    return value


def _positive_float(values: dict[str, str], key: str, default: float) -> float:
    try:
        value = float(values.get(key, str(default)))
    except ValueError as error:
        raise ConfigurationError(f"{key} must be a number") from error
    if not math.isfinite(value) or value <= 0:
        raise ConfigurationError(f"{key} must be greater than 0")
    return value
