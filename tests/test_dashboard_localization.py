from io import BytesIO
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from anima.adapters.dashboard.localization import catalogs, message, normalize_locale, render_asset, TOKEN
from anima.adapters.dashboard.server import ASSET_DIR, DashboardData, dashboard_branding, _handler, memory_contents


class DashboardLocalizationTests(unittest.TestCase):
    def test_default_and_supported_locales(self):
        for value in (None, "", "fr", "ja-JP", 1, [], {}):
            self.assertEqual(normalize_locale(value), "en")
        self.assertEqual(normalize_locale("en"), "en")
        self.assertEqual(normalize_locale("ja"), "ja")
        self.assertEqual(message("記憶の書庫", "en"), "Memory archive")
        self.assertEqual(message("Memory archive", "ja"), "記憶の書庫")
        self.assertEqual(message("Custom extension label", "ja"), "Custom extension label")
        self.assertEqual(message("Persona-owned にゃ text", "en"), "Persona-owned にゃ text")

    def test_all_template_messages_exist_in_both_languages(self):
        self.assertEqual(set(catalogs()["en"]), set(catalogs()["ja"]))
        for filename in ("index.html", "dashboard.js"):
            template = (ASSET_DIR / filename).read_text()
            for key in TOKEN.findall(template):
                for locale in ("en", "ja"):
                    self.assertTrue(catalogs()[locale][key])
            for locale in ("en", "ja"):
                value = render_asset(template, locale, script=filename.endswith(".js"))
                self.assertNotIn("__ANIMA_I18N_", value)
                if locale == "en":
                    self.assertNotRegex(value, r"[ぁ-んァ-ヶ一-龯]")
            if filename == "index.html":
                self.assertIn('lang="en"', render_asset(template, "en"))
                self.assertIn('lang="ja"', render_asset(template, "ja"))
            else:
                self.assertIn("'en-US'", render_asset(template, "en", script=True))
                self.assertIn("'ja-JP'", render_asset(template, "ja", script=True))

    def test_html_and_script_values_are_escaped(self):
        text = "<img>\\'\"`${attack}\n\r\u2028\u2029"
        with patch("anima.adapters.dashboard.localization.catalogs", return_value={"en": {"test": text}, "ja": {"test": "安全"}}):
            self.assertEqual(render_asset("__ANIMA_I18N_test__", "en"),
                             "&lt;img&gt;\\&#x27;&quot;`${attack}\n\r\u2028\u2029")
            self.assertEqual(render_asset("__ANIMA_I18N_test__", "en", script=True),
                             "<img>\\\\\\'\\\"\\`\\${attack}\\n\\r\\u2028\\u2029")
            with self.assertRaises(KeyError):
                render_asset("__ANIMA_I18N_missing__", "en")

    def test_locale_validation_and_custom_content_survives(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = DashboardData(root, root)
            self.assertEqual(dashboard_branding(root)["locale"], "en")
            data.save_configuration("dashboard", '{"locale":"ja"}')
            self.assertEqual(dashboard_branding(root)["locale"], "ja")
            data.save_configuration("dashboard", '{}')
            self.assertEqual(data.configuration(writable=True)["documents"][0]["label"], "Persona")
            config = {"locale": "ja", "browser_title": "固有のタイトル", "heading": "見出し", "eyebrow": "CUSTOM", "memory_guide": "記憶の書庫"}
            data.save_configuration("dashboard", json.dumps(config))
            self.assertEqual(dashboard_branding(root), config)
            self.assertEqual(data.configuration(writable=True)["documents"][0]["label"], "ペルソナ")
            for invalid in ("fr", "ja-JP", None, 1, [], {}):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    data.save_configuration("dashboard", json.dumps(dict(config, locale=invalid)))
            (root / "dashboard.json").write_text(json.dumps({"locale": "fr"}))
            self.assertEqual(dashboard_branding(root)["locale"], "en")
            (root / "dashboard.json").write_text('{"locale":"ja"}')
            self.assertEqual(dashboard_branding(root)["memory_guide"], "現在保持している記憶文書を確認できます。")
            people = root / "memory" / "people"
            people.mkdir(parents=True)
            (people / "1.md").write_text("本文は翻訳しない")
            self.assertEqual(memory_contents(root, locale="en")["documents"][0]["content"], "本文は翻訳しない")
            self.assertEqual(memory_contents(root, locale="ja")["documents"][0]["title"], "人物 1")

    def test_served_assets_and_api_errors_use_deployment_locale(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            handler = _handler(root, root)

            def get(path):
                instance = handler.__new__(handler)
                instance.path = path
                instance.wfile = BytesIO()
                instance.send_response = MagicMock()
                instance.send_header = MagicMock()
                instance.end_headers = MagicMock()
                instance.do_GET()
                return instance.wfile.getvalue().decode()

            self.assertIn('lang="en"', get("/"))
            self.assertIn("Self memory", get("/dashboard.js"))
            self.assertIn("Display scope", json.loads(get("/api/status"))["error"])
            self.assertEqual(json.loads(get("/api/sandboxes"))["branding"]["locale"], "en")
            (root / "dashboard.json").write_text('{"locale":"ja"}')
            self.assertIn('lang="ja"', get("/"))
            self.assertIn("自分の記憶", get("/dashboard.js"))
            self.assertIn("表示対象", json.loads(get("/api/status"))["error"])
            fields = json.loads(get("/api/settings"))["fields"]
            self.assertIn("許可ギルドID", [field["label"] for field in fields])
            (root / "dashboard.json").write_text('{"locale":"en"}')
            fields = json.loads(get("/api/settings"))["fields"]
            self.assertIn("Allowed guild IDs", [field["label"] for field in fields])
            self.assertTrue(all(not re.search(r'[ぁ-んァ-ヶ一-龯]', field["label"]) for field in fields))
