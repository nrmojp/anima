from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from anima.bootstrap.settings import ConfigurationError, Settings, load_dotenv


class ConfigurationTests(unittest.TestCase):
    def test_env_example_contains_only_current_first_run_settings(self):
        path = Path(__file__).parents[1] / ".env.example"
        active = {
            line.split("=", 1)[0]
            for line in path.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")
        }

        self.assertEqual(active, {
            "DISCORD_BOT_TOKEN",
            "OPENAI_API_KEY",
            "ANIMA_ALLOWED_GUILD_IDS",
            "ANIMA_DM_ENABLED",
            "ANIMA_ENABLE_WEB_SEARCH",
            "ANIMA_PLUGINS",
        })
        self.assertNotIn("ANIMA_REACTIONS_ENABLED", path.read_text(encoding="utf-8"))

    def test_dotenv_comments_quotes_and_existing_values(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            path.write_text('# comment\n\nA="hello=world"\nB=\'value\'\nC=new\n')
            values = {'C': 'existing'}
            self.assertIs(load_dotenv(path, environ=values), values)
            self.assertEqual(values, {'A': 'hello=world', 'B': 'value', 'C': 'existing'})

    def test_dotenv_rejects_malformed_lines(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            for line in ('invalid', 'A="', 'A="unclosed'):
                with self.subTest(line=line):
                    path.write_text(line)
                    with self.assertRaises(ConfigurationError):
                        load_dotenv(path, environ={})

    def test_invalid_settings(self):
        cases = {
            'ANIMA_ENABLE_WEB_SEARCH': ['maybe'],
            'ANIMA_RECENT_LIMIT': ['0', '-1', 'bad'],
            'OPENAI_MAX_RETRIES': ['-1', '1.5'],
            'OPENAI_RESPONSE_TIMEOUT_SECONDS': ['0', '-1', 'bad', 'nan', 'inf'],
            'ANIMA_PROACTIVE_DECISION_DAILY_LIMIT': ['0', '-1', 'bad'],
            'ANIMA_PROACTIVE_COOLDOWN_SECONDS': ['0', '-1', 'bad'],
            'ANIMA_SHUTDOWN_TIMEOUT_SECONDS': ['0', '-1', 'bad'],
            'ANIMA_INVENTORY_MAX_ITEMS': ['0', '-1', 'bad'],
            'ANIMA_INVENTORY_MAX_BYTES': ['0', '-1', 'bad'],
            'ANIMA_INVENTORY_TEMPORARY_RETENTION_HOURS': ['0', '-1', 'bad'],
            'ANIMA_DASHBOARD_PORT': ['0', '65536', 'bad'],
            'ANIMA_DASHBOARD_HOST': ['', 'localhost', '192.168.1.2'],
            'ANIMA_PLUGINS': ['unknown', 'music,unknown'],
        }
        with TemporaryDirectory() as directory:
            for key, values in cases.items():
                for value in values:
                    with self.subTest(key=key, value=value):
                        with self.assertRaises(ConfigurationError):
                            Settings.load(cwd=Path(directory), environ={
                                'DISCORD_BOT_TOKEN': 'test', 'OPENAI_API_KEY': 'test', key: value,
                            })

    def test_explicit_plugins_override_legacy_feature_flags(self):
        with TemporaryDirectory() as directory:
            settings = Settings.load(cwd=Path(directory), environ={
                'DISCORD_BOT_TOKEN': 'test', 'OPENAI_API_KEY': 'test',
                'ANIMA_PLUGINS': 'echo',
                'ANIMA_ENABLE_WEB_SEARCH': 'true',
                'ANIMA_VOICE_ENABLED': 'true',
                'ANIMA_MUSIC_ENABLED': 'true',
            })
        self.assertEqual(
            settings.plugins,
            frozenset({'echo'}),
        )
        self.assertTrue(settings.enable_web_search)
        self.assertFalse(settings.voice_enabled)

    def test_empty_explicit_plugin_list_disables_all_optional_capabilities(self):
        with TemporaryDirectory() as directory:
            settings = Settings.load(cwd=Path(directory), environ={
                'DISCORD_BOT_TOKEN': 'test', 'OPENAI_API_KEY': 'test',
                'ANIMA_PLUGINS': '',
            })
        self.assertEqual(settings.plugins, frozenset())

    def test_dashboard_runtime_config_overrides_non_secret_environment(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "config"
            root.mkdir()
            (root / "config.json").write_text(
                '{"openai_model":"ui-model","voice.volume":20}', encoding="utf-8"
            )
            settings = Settings.load(cwd=Path(directory), environ={
                "DISCORD_BOT_TOKEN": "secret-discord",
                "OPENAI_API_KEY": "secret-openai",
                "OPENAI_MODEL": "env-model",
                "ANIMA_MUSIC_VOLUME": "0.9",
            })
            self.assertEqual(settings.openai_model, "ui-model")
            self.assertEqual(settings.plugin_configuration["voice"]["volume"], 20)
            self.assertEqual(settings.discord_bot_token, "secret-discord")
            self.assertEqual(settings.openai_api_key, "secret-openai")

    def test_absolute_paths_boolean_aliases_and_boundaries(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for enabled in ('1', 'true', 'yes', 'on'):
                settings = Settings.load(cwd=root, environ={
                    'DISCORD_BOT_TOKEN': 'test', 'OPENAI_API_KEY': 'test',
                    'ANIMA_ROOT': str(root / 'resources'),
                    'ANIMA_STATE_ROOT': str(root / 'mutable'),
                    'ANIMA_ENABLE_WEB_SEARCH': enabled,
                    'OPENAI_MAX_RETRIES': '0',
                })
                self.assertTrue(settings.enable_web_search)
                self.assertEqual(settings.state_root, root / 'mutable')
                self.assertEqual(settings.anima_root, root / 'resources')
                self.assertEqual(settings.openai_max_retries, 0)
