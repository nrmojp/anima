"""Typed, non-secret runtime configuration editable from the Dashboard."""

from __future__ import annotations

import json
import math
from dataclasses import replace
from pathlib import Path
import re
from typing import Mapping

from anima.capabilities.configuration import ConfigField
from anima.capabilities.plugin_loader import PluginLoader


class RuntimeConfigError(ValueError):
    pass


DISCOVERED_PLUGINS = tuple(
    manifest.name for manifest in PluginLoader().load().manifests
)

CORE_FIELDS = (
    ConfigField("bot_loop_window_seconds", "ANIMA_BOT_LOOP_WINDOW_SECONDS", "bot_loop_window_seconds", "Bot会話ヒューズ時間窓（秒）", "自発発言", "int", 600, 60, 86400),
    ConfigField("bot_loop_max_speaks", "ANIMA_BOT_LOOP_MAX_SPEAKS", "bot_loop_max_speaks", "時間窓内のBot宛て発話上限", "自発発言", "int", 2, 1, 20),
    ConfigField("command_prefix", "ANIMA_COMMAND_PREFIX", "command_prefix", "管理コマンドの接頭辞", "Discord", "text", "anima"),
    ConfigField("allowed_guild_ids", "ANIMA_ALLOWED_GUILD_IDS", "allowed_guild_ids", "許可ギルドID", "Discord", "list", ()),
    ConfigField("dm_enabled", "ANIMA_DM_ENABLED", "dm_enabled", "DMを有効にする", "Discord", "bool", False),
    ConfigField("persona_names", "ANIMA_PERSONA_NAMES", "persona_names", "呼びかけ名", "Discord", "list", ()),
    ConfigField("enable_web_search", "ANIMA_ENABLE_WEB_SEARCH", "enable_web_search", "Web検索を許可する", "OpenAI", "bool", False),
    ConfigField("plugins", "ANIMA_PLUGINS", "plugins", "有効プラグイン", "能力", "multi", (), options=DISCOVERED_PLUGINS),
    ConfigField("openai_model", "OPENAI_MODEL", "openai_model", "会話モデル", "OpenAI", "text", "gpt-6-luna"),
    ConfigField("openai_reflection_model", "OPENAI_REFLECTION_MODEL", "openai_reflection_model", "振り返りモデル", "OpenAI", "text", "gpt-6-sol"),
    ConfigField("openai_response_timeout_seconds", "OPENAI_RESPONSE_TIMEOUT_SECONDS", "openai_response_timeout_seconds", "応答タイムアウト（秒）", "OpenAI", "float", 45.0, 1, 600),
    ConfigField("openai_maintenance_timeout_seconds", "OPENAI_MAINTENANCE_TIMEOUT_SECONDS", "openai_maintenance_timeout_seconds", "記憶処理タイムアウト（秒）", "OpenAI", "float", 300.0, 1, 1800),
    ConfigField("log_retention_days", "ANIMA_LOG_RETENTION_DAYS", "log_retention_days", "会話ログ保持日数", "保存", "int", 30, 1, 3650),
    ConfigField("archive_retention_days", "ANIMA_ARCHIVE_RETENTION_DAYS", "archive_retention_days", "アーカイブ保持日数", "保存", "int", 365, 1, 3650),
    ConfigField("inventory_max_items", "ANIMA_INVENTORY_MAX_ITEMS", "inventory_max_items", "持ち物の最大件数", "保存", "int", 500, 1, 10000),
    ConfigField("inventory_max_bytes", "ANIMA_INVENTORY_MAX_BYTES", "inventory_max_bytes", "持ち物の最大容量（bytes）", "保存", "int", 268435456, 1048576, 10737418240),
    ConfigField("inventory_temporary_retention_hours", "ANIMA_INVENTORY_TEMPORARY_RETENTION_HOURS", "inventory_temporary_retention_hours", "一時生成物の保持時間", "保存", "int", 24, 1, 720),
    ConfigField("self_time_interval_seconds", "ANIMA_SELF_TIME_INTERVAL_SECONDS", "self_time_interval_seconds", "自分の時間の判定間隔（秒）", "自分の時間", "int", 21600, 300, 604800),
    ConfigField("self_time_daily_limit", "ANIMA_SELF_TIME_DAILY_LIMIT", "self_time_daily_limit", "自分の時間の日次起動上限", "自分の時間", "int", 2, 1, 24),
    ConfigField("self_time_max_iterations", "ANIMA_SELF_TIME_MAX_ITERATIONS", "self_time_max_iterations", "1回の最大反復数", "自分の時間", "int", 4, 1, 10),
    ConfigField("proactive_decision_daily_limit", "ANIMA_PROACTIVE_DECISION_DAILY_LIMIT", "proactive_decision_daily_limit", "自発発言の1日判定上限", "自発発言", "int", 100, 1, 10000),
    ConfigField("proactive_daily_limit", "ANIMA_PROACTIVE_DAILY_LIMIT", "proactive_daily_limit", "自発発言の1日上限", "自発発言", "int", 4, 1, 1000),
    ConfigField("proactive_cooldown_seconds", "ANIMA_PROACTIVE_COOLDOWN_SECONDS", "proactive_cooldown_seconds", "自発発言の最短間隔（秒）", "自発発言", "int", 1800, 1, 604800),
)
PLUGIN_FIELDS = tuple(
    replace(field, key=f"{manifest.name}.{field.key}", plugin=manifest.name)
    for manifest in PluginLoader().load().manifests for field in manifest.configuration
)
FIELDS = (*CORE_FIELDS, *PLUGIN_FIELDS)
FIELD_BY_KEY = {field.key: field for field in FIELDS}


def load_runtime_config(path: Path) -> dict[str, object]:
    if path.is_symlink():
        raise RuntimeConfigError("runtime configuration path is unsafe")
    if not path.exists():
        return {}
    if not path.is_file():
        raise RuntimeConfigError("runtime configuration path is unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeConfigError("runtime configuration is invalid") from error
    return validate_runtime_config(value)


def validate_runtime_config(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RuntimeConfigError("runtime configuration fields are invalid")
    normalized = {}
    for key, item in value.items():
        if key not in FIELD_BY_KEY:
            aliases = [field.key for field in PLUGIN_FIELDS if field.key.split(".", 1)[1] == key]
            if len(aliases) != 1:
                raise RuntimeConfigError("runtime configuration fields are invalid")
            key = aliases[0]
        if key in normalized:
            raise RuntimeConfigError("duplicate runtime configuration field")
        normalized[key] = _validate(FIELD_BY_KEY[key], item)
    return normalized


def merge_runtime_config(values: Mapping[str, str], path: Path) -> dict[str, str]:
    merged = dict(values)
    for key, value in load_runtime_config(path).items():
        field = FIELD_BY_KEY[key]
        if isinstance(value, bool):
            merged[field.env] = "true" if value else "false"
        elif isinstance(value, list):
            merged[field.env] = ",".join(value)
        else:
            merged[field.env] = str(value)
    return merged


def effective_runtime_config(settings: object) -> dict[str, object]:
    result = {}
    for field in FIELDS:
        value = (getattr(settings, "plugin_configuration", {}).get(field.plugin, {}).get(field.key.split(".", 1)[1], field.default)
                 if field.plugin else getattr(settings, field.attribute, field.default))
        result[field.key] = sorted(value) if isinstance(value, frozenset) else list(value) if isinstance(value, tuple) else value
    return result


def save_runtime_config(path: Path, value: object) -> dict[str, object]:
    validated = validate_runtime_config(value)
    if path.is_symlink():
        raise RuntimeConfigError("runtime configuration path is unsafe")
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(validated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return validated


def _validate(field: ConfigField, value: object) -> object:
    if field.kind == "bool":
        if not isinstance(value, bool):
            raise RuntimeConfigError(f"{field.key} must be boolean")
        return value
    if field.kind in {"list", "multi"}:
        if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
            raise RuntimeConfigError(f"{field.key} must be a string list")
        normalized = list(dict.fromkeys(item.strip() for item in value))
        if field.key == "allowed_guild_ids" and any(
            not re.fullmatch(r"[1-9][0-9]{0,19}", item) or int(item) >= 2**64
            for item in normalized
        ):
            raise RuntimeConfigError("allowed_guild_ids contains an invalid Discord ID")
        if field.options and any(item not in field.options for item in normalized):
            raise RuntimeConfigError(f"{field.key} has an unsupported value")
        return normalized
    if field.kind == "text":
        if not isinstance(value, str) or not value.strip() or len(value) > 200:
            raise RuntimeConfigError(f"{field.key} must be text")
        return value.strip()
    if field.kind == "int" and (not isinstance(value, int) or isinstance(value, bool)):
        raise RuntimeConfigError(f"{field.key} must be an integer")
    if field.kind == "float" and (not isinstance(value, (int, float)) or isinstance(value, bool)):
        raise RuntimeConfigError(f"{field.key} must be numeric")
    number = float(value)
    if not math.isfinite(number) or field.minimum is not None and number < field.minimum or field.maximum is not None and number > field.maximum:
        raise RuntimeConfigError(f"{field.key} is out of range")
    return int(value) if field.kind == "int" else number
