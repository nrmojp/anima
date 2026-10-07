from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from anima.bootstrap.runtime_config import (
    FIELDS, RuntimeConfigError, effective_runtime_config, load_runtime_config,
    merge_runtime_config, save_runtime_config, validate_runtime_config,
)
from anima.bootstrap.settings import Settings
from anima.capabilities.configuration import ConfigField


class RuntimeConfigTests(unittest.TestCase):
    def test_round_trip_merge_and_effective_values(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "config.json"
            value = {
                "dm_enabled": True,
                "plugins": ["echo", "voice"],
                "allowed_guild_ids": ["123"],
                "voice.volume": 25,
                "voice.speed": 150,
                "openai_model": "model-x",
            }
            self.assertEqual(save_runtime_config(path, value), value)
            self.assertEqual(load_runtime_config(path), value)
            merged = merge_runtime_config({"ANIMA_VOICE_VOLUME": "80"}, path)
            self.assertEqual(merged["ANIMA_VOICE_VOLUME"], "25")
            self.assertEqual(merged["ANIMA_DM_ENABLED"], "true")
            self.assertEqual(merged["ANIMA_PLUGINS"], "echo,voice")
            settings = Settings.load(cwd=root, environ={
                "DISCORD_BOT_TOKEN": "token", "OPENAI_API_KEY": "key",
                "ANIMA_ROOT": str(root), "ANIMA_VOICE_VOLUME": "80",
            })
            self.assertEqual(settings.plugin_configuration["voice"]["volume"], 25)
            self.assertEqual(settings.plugins, frozenset({"echo", "voice"}))
            effective = effective_runtime_config(settings)
            self.assertEqual(effective["allowed_guild_ids"], ["123"])
            self.assertEqual(effective["plugins"], ["echo", "voice"])
            self.assertEqual(len(effective), len(FIELDS))

    def test_missing_and_all_supported_value_types(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            self.assertEqual(load_runtime_config(path), {})
            value = {
                field.key: list(field.default) if field.kind in {"list", "multi"} else field.default
                for field in FIELDS
            }
            validated = validate_runtime_config(value)
            self.assertEqual(set(validated), {field.key for field in FIELDS})
            self.assertEqual(validate_runtime_config({"plugins": []}), {"plugins": []})

    def test_invalid_files_fields_and_values(self):
        bad_values = (
            [], {"unknown": 1}, {"dm_enabled": "yes"},
            {"plugins": "music"}, {"plugins": ["unknown"]},
            {"allowed_guild_ids": ["0"]}, {"allowed_guild_ids": [1]},
            {"openai_model": ""}, {"openai_model": "x" * 201},
            {"voice.speed": True}, {"voice.speed": 0},
            {"inventory_max_items": 0}, {"inventory_max_bytes": 1},
            {"inventory_temporary_retention_hours": 0},
            {"voice.timeout_seconds": "x"}, {"voice.timeout_seconds": 0},
            {"voice.timeout_seconds": float("nan")}, {"voice.timeout_seconds": float("inf")},
        )
        for value in bad_values:
            with self.subTest(value=value), self.assertRaises(RuntimeConfigError):
                validate_runtime_config(value)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "config.json"
            path.mkdir()
            with self.assertRaises(RuntimeConfigError):
                load_runtime_config(path)
            path.rmdir()
            path.write_text("bad", encoding="utf-8")
            with self.assertRaises(RuntimeConfigError):
                load_runtime_config(path)
            path.unlink()
            path.symlink_to(root / "missing")
            with self.assertRaises(RuntimeConfigError):
                load_runtime_config(path)
            with self.assertRaises(RuntimeConfigError):
                save_runtime_config(path, {})

    def test_public_schema_has_no_environment_or_secret_names(self):
        for field in FIELDS:
            public = field.public()
            self.assertNotIn("env", public)
            self.assertNotIn("attribute", public)
            self.assertNotIn("TOKEN", json.dumps(public))
        plugins = {field.plugin for field in FIELDS if field.plugin}
        self.assertEqual(plugins, {"echo", "voice"})
        for args in (
            ("", "ENV", "attr", "Label", "Group", "text", "x"),
            ("key", "ENV", "attr", "Label", "Group", "unknown", "x"),
            ("key", "API_TOKEN", "attr", "Label", "Group", "text", "x"),
        ):
            with self.assertRaises(ValueError):
                ConfigField(*args)
