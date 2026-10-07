from __future__ import annotations

import asyncio
import json
import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from anima.bootstrap.settings import ConfigurationError, Settings, load_dotenv
from anima.adapters.discord.client import has_human_members
from anima.core.jobs import PluginJobManager
from datetime import datetime, timezone

from anima.core.models import (
    Context, DigestJob, Event, MentionedPerson, Mood, MusicReference,
    ReflectionJob, SleepJob,
)
from anima.core.memory import MemoryPassage, MemoryRecall
from anima.adapters.openai.client import (
    OpenAIAddressClassifier,
    OpenAIMemoryMaintainer,
    OpenAIResponder,
    OpenAISelfTimeDecider,
    _compact_tool_instructions,
    _input_image_urls,
    _is_image_download_error,
    _openai_tool_spec,
    _tool_log_fields,
    _without_input_images,
    _reasoning_summary_text,
)


class AddressClassifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_structured_routing_input_and_validation(self):
        from test_sandbox import event
        from dataclasses import replace
        client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(
            return_value=SimpleNamespace(output_text='{"expects_reply":true}', usage=None))))
        classifier = OpenAIAddressClassifier(client=client, model="test-model")
        source = replace(event(), mention=False, text="それをお願い")
        history = (replace(source, id="prior", author_id="self", text="猫を描こうか？"),)
        self.assertTrue(await classifier.expects_reply(SimpleNamespace(persona="persona"), history, source))
        args = client.responses.create.call_args.kwargs
        self.assertEqual(args["text"]["format"]["schema"]["properties"]["expects_reply"],
                         {"type": "boolean"})
        self.assertEqual(json.loads(args["input"])["events"][0]["author_id"], "self")
        self.assertNotIn("tools", args)
        client.responses.create.return_value.output_text = '{"expects_reply":false}'
        self.assertFalse(await classifier.expects_reply(SimpleNamespace(persona=""), history, source))
        client.responses.create.return_value.output_text = '{"expects_reply":"true"}'
        with self.assertRaises(ValueError):
            await classifier.expects_reply(SimpleNamespace(persona=""), history, source)
from anima.capabilities.contracts import (
    ContextReference, FunctionToolSpec, NativeToolSpec, ToolRegistry, ToolResult,
)
from anima.core.sandbox import SandboxKey
from anima.core.inventory import InventoryStore
from anima.core.self_time import SelfTimeDecision
from anima.capabilities.responses import ResponseContributionRegistry
from anima.bootstrap.native_tools import WebSearchToolProvider


class FakeResponses:
    def __init__(self) -> None:
        self.payload = None
        self.reply = "おかえり。"

    async def create(self, **payload):
        self.payload = payload
        return SimpleNamespace(
            output_text=json.dumps(
                {
                    "reply": self.reply,
                    "research_summary": "帰宅への挨拶について調べた。",
                    "mood": {
                        "state": "うれしい",
                        "cause": "呼んでもらった",
                        "strength": "ふつう",
                        "focus": "今日の話",
                    },
                },
                ensure_ascii=False,
            ),
            output=[SimpleNamespace(
                type="web_search_call",
                action=SimpleNamespace(
                    query="帰宅 挨拶",
                    sources=[
                        SimpleNamespace(title="挨拶資料", url="https://example.com/greeting"),
                        SimpleNamespace(title="危険", url="javascript:alert(1)"),
                        SimpleNamespace(title="資料2", url="https://example.com/two"),
                        SimpleNamespace(title="資料3", url="https://example.com/three"),
                        SimpleNamespace(title="資料4", url="https://example.com/four"),
                    ],
                ),
            )],
        )


class FakeMaintenanceResponses:
    def __init__(self, outputs) -> None:
        self.outputs = iter(outputs)
        self.payloads = []

    async def create(self, **payload):
        self.payloads.append(payload)
        return SimpleNamespace(output_text=json.dumps(next(self.outputs), ensure_ascii=False))


class FakeMaintenanceClient:
    def __init__(self, outputs) -> None:
        self.responses = FakeMaintenanceResponses(outputs)


class FakeOpenAIClient:
    def __init__(self) -> None:
        self.responses = FakeResponses()


class ImageDownloadFailure(Exception):
    status_code = 400
    body = {"error": {"message": "Error while downloading file. Upstream status code: 404."}}


class FailingImageResponses(FakeResponses):
    def __init__(self) -> None:
        super().__init__()
        self.payloads = []

    async def create(self, **payload):
        self.payloads.append(payload)
        if len(self.payloads) == 1:
            raise ImageDownloadFailure("discord image unavailable")
        return await super().create(**payload)


class FakeMusicTools:
    calls = []

    @staticmethod
    def definitions(source=None):
        return [{
            "type": "function", "name": "search_music", "description": "Search music",
            "strict": True, "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 3},
                },
                "required": ["query", "limit"], "additionalProperties": False,
            },
        }]

    async def execute(self, name, arguments, *, source=None):
        self.calls.append((name, arguments))
        return ('{"results":['
                '{"id":"cat","title":"猫の歌"},'
                '{"id":"dog","title":"犬の歌"}]}' )

    @staticmethod
    def references(identifiers):
        tracks = {
            "cat": MusicReference("cat", "猫の歌", 90, ("pop",), "https://example.test/cat"),
            "dog": MusicReference("dog", "犬の歌", 91, ("rock",), "https://example.test/dog"),
        }
        return tuple(tracks[identifier] for identifier in identifiers if identifier in tracks)


class FailingMusicTools(FakeMusicTools):
    async def execute(self, name, arguments, *, source=None):
        raise RuntimeError("tool broke")


class FakeFunctionCall(SimpleNamespace):
    def model_dump(self, exclude_none=True):
        return {
            "type": self.type,
            "name": self.name,
            "call_id": self.call_id,
            "arguments": self.arguments,
        }


class FakeToolResponses:
    def __init__(self):
        self.payloads = []

    async def create(self, **payload):
        self.payloads.append(payload)
        usage = SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15)
        if len(self.payloads) == 1:
            return SimpleNamespace(
                id="tool-response",
                output_text="",
                output=[FakeFunctionCall(type="function_call", name="search_music", call_id="c1", arguments='{"query":"猫","limit":3}')],
                usage=usage,
            )
        return SimpleNamespace(
            id="final-response",
            output_text=json.dumps(
                {
                    "reply": "犬の歌があるよ。",
                    "research_summary": None,
                    "music_refs": ["dog", "not-a-candidate"],
                    "mood": {"state": "得意げ", "cause": "曲を紹介した", "strength": "ふつう", "focus": "猫の歌"},
                },
                ensure_ascii=False,
            ),
            output=[],
            usage=usage,
        )


class FakeToolClient:
    def __init__(self):
        self.responses = FakeToolResponses()


class FakeLoopingToolResponses:
    def __init__(self):
        self.payloads = []

    async def create(self, **payload):
        self.payloads.append(payload)
        usage = SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15)
        if payload["tools"]:
            number = len(self.payloads)
            return SimpleNamespace(
                id=f"tool-response-{number}",
                output_text="",
                output=[FakeFunctionCall(
                    type="function_call",
                    name="search_music",
                    call_id=f"c{number}",
                    arguments='{"query":"元気な曲","limit":3}',
                )],
                usage=usage,
            )
        return SimpleNamespace(
            id="forced-final-response",
            output_text=json.dumps(
                {
                    "reply": "選んだ曲を流すね。",
                    "research_summary": None,
                    "music_refs": ["dog"],
                    "face": "楽",
                    "mood": {
                        "state": "楽しい", "cause": "選曲した",
                        "strength": "ふつう", "focus": "BGM",
                    },
                },
                ensure_ascii=False,
            ),
            output=[],
            usage=usage,
        )


class FakeLoopingToolClient:
    def __init__(self):
        self.responses = FakeLoopingToolResponses()


class ConfigurationTests(unittest.TestCase):
    def test_dotenv_does_not_override_process_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".env"
            path.write_text("OPENAI_API_KEY=file-key\nDISCORD_BOT_TOKEN='discord-key'\n")
            values = {"OPENAI_API_KEY": "process-key"}

            load_dotenv(path, environ=values)

            self.assertEqual(values["OPENAI_API_KEY"], "process-key")
            self.assertEqual(values["DISCORD_BOT_TOKEN"], "discord-key")

    def test_settings_require_both_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ConfigurationError, "DISCORD_BOT_TOKEN"):
                Settings.load(
                    cwd=Path(temporary),
                    environ={"OPENAI_API_KEY": "key"},
                )

    def test_settings_use_safe_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = Settings.load(
                cwd=Path(temporary),
                environ={
                    "OPENAI_API_KEY": "openai-key",
                    "DISCORD_BOT_TOKEN": "discord-key",
                },
            )

            self.assertEqual(settings.openai_model, "gpt-6-luna")
            self.assertEqual(settings.openai_reflection_model, "gpt-6-sol")
            self.assertEqual(settings.anima_root, (Path(temporary) / "config").resolve())
            self.assertEqual(
                settings.state_root, (Path(temporary) / "state").resolve()
            )
            self.assertFalse(settings.enable_web_search)
            self.assertEqual(settings.recent_limit, 30)
            self.assertEqual(settings.openai_response_timeout_seconds, 45.0)
            self.assertEqual(settings.openai_maintenance_timeout_seconds, 300.0)
            self.assertEqual(settings.discord_send_timeout_seconds, 15.0)
            self.assertEqual(settings.openai_max_retries, 2)
            self.assertEqual(settings.digest_max_lines, 30)
            self.assertEqual(settings.memory_max_lines, 80)
            self.assertEqual(settings.memory_strong_max, 10)
            self.assertEqual(settings.event_max_retries, 2)
            self.assertEqual(settings.retry_base_delay_seconds, 0.5)
            self.assertEqual(settings.persona_queue_size, 100)
            self.assertEqual(settings.log_retention_days, 30)
            self.assertEqual(settings.archive_retention_days, 365)
            self.assertEqual(settings.operational_log_retention_days, 14)
            self.assertEqual(settings.shutdown_timeout_seconds, 30.0)
            self.assertEqual(settings.dashboard_host, "127.0.0.1")
            self.assertEqual(settings.dashboard_port, 8765)
            self.assertEqual(settings.dashboard_admin_token, "")
            self.assertEqual(settings.proactive_decision_daily_limit, 100)
            self.assertEqual(settings.proactive_cooldown_seconds, 1800)
            self.assertEqual(settings.proactive_daily_limit, 4)
            self.assertEqual(settings.self_time_max_iterations, 4)


class DiscordVoiceStateTests(unittest.TestCase):
    def test_empty_or_bot_only_channel_has_no_human_members(self) -> None:
        self.assertFalse(has_human_members(SimpleNamespace(members=[])))
        self.assertFalse(
            has_human_members(SimpleNamespace(members=[SimpleNamespace(bot=True)]))
        )

    def test_channel_with_person_has_human_members(self) -> None:
        self.assertTrue(
            has_human_members(
                SimpleNamespace(
                    members=[SimpleNamespace(bot=True), SimpleNamespace(bot=False)]
                )
            )
        )

    def test_settings_parse_operational_limits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = Settings.load(
                cwd=Path(temporary),
                environ={
                    "OPENAI_API_KEY": "openai-key",
                    "DISCORD_BOT_TOKEN": "discord-key",
                    "OPENAI_RESPONSE_TIMEOUT_SECONDS": "12.5",
                    "OPENAI_MAINTENANCE_TIMEOUT_SECONDS": "90",
                    "DISCORD_SEND_TIMEOUT_SECONDS": "3",
                    "OPENAI_MAX_RETRIES": "1",
                    "ANIMA_DIGEST_MAX_LINES": "10",
                    "ANIMA_MEMORY_MAX_LINES": "20",
                    "ANIMA_SHUTDOWN_TIMEOUT_SECONDS": "4.5",
                    "ANIMA_DASHBOARD_HOST": "127.0.0.1",
                    "ANIMA_DASHBOARD_PORT": "9876",
                    "ANIMA_PROACTIVE_COOLDOWN_SECONDS": "600",
                    "ANIMA_PROACTIVE_DAILY_LIMIT": "2",
                },
            )

            self.assertEqual(settings.openai_response_timeout_seconds, 12.5)
            self.assertEqual(settings.openai_maintenance_timeout_seconds, 90.0)
            self.assertEqual(settings.discord_send_timeout_seconds, 3.0)
            self.assertEqual(settings.openai_max_retries, 1)
            self.assertEqual(settings.digest_max_lines, 10)
            self.assertEqual(settings.memory_max_lines, 20)
            self.assertEqual(settings.shutdown_timeout_seconds, 4.5)
            self.assertEqual(settings.dashboard_host, "127.0.0.1")
            self.assertEqual(settings.dashboard_port, 9876)
            self.assertEqual(settings.proactive_cooldown_seconds, 600)
            self.assertEqual(settings.proactive_daily_limit, 2)

    def test_settings_reject_invalid_operational_limits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ConfigurationError, "greater than 0"):
                Settings.load(
                    cwd=Path(temporary),
                    environ={
                        "OPENAI_API_KEY": "openai-key",
                        "DISCORD_BOT_TOKEN": "discord-key",
                        "DISCORD_SEND_TIMEOUT_SECONDS": "0",
                    },
                )

    def test_settings_reject_example_placeholders(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ConfigurationError, "placeholder"):
                Settings.load(
                    cwd=Path(temporary),
                    environ={
                        "OPENAI_API_KEY": "replace-me",
                        "DISCORD_BOT_TOKEN": "replace-me",
                    },
                )


class OpenAIAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_self_time_decider_uses_bounded_structured_output(self):
        response = SimpleNamespace(
            output_text=json.dumps({
                "action": "finish", "note": "少し考えた", "mood_state": "普通",
                "direction": "broaden", "discovery": "猫から夜の散歩に興味が移った",
                "mood_cause": "考えごと", "mood_strength": "弱い", "mood_focus": "猫",
                "reflection": "猫のことが気になった",
            }),
            output=[SimpleNamespace(type="reasoning", summary=[
                SimpleNamespace(text="猫について考えた。"),
            ])],
            id="r1", usage=SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15),
        )
        create = AsyncMock(return_value=response)
        decider = OpenAISelfTimeDecider(
            client=SimpleNamespace(responses=SimpleNamespace(create=create)), model="test",
        )
        self.assertEqual(decider.max_tool_rounds, 10)
        previous = SelfTimeDecision("continue", "考え始めた")
        result = await decider.decide({"mood": "普通"}, 2, previous)
        self.assertEqual(result.action, "finish")
        self.assertEqual(result.direction, "broaden")
        self.assertEqual(result.discovery, "猫から夜の散歩に興味が移った")
        self.assertEqual(result.reflection, "猫について考えた。")
        payload = create.call_args.kwargs
        self.assertFalse(payload["store"])
        self.assertEqual(payload["reasoning"], {"effort": "low", "summary": "auto"})
        self.assertIn("previous", payload["input"][0]["content"][0]["text"])
        self.assertEqual(payload["text"]["format"]["name"], "anima_self_time")
        self.assertIn("reflection", payload["text"]["format"]["schema"]["required"])
        self.assertIn("direction", payload["text"]["format"]["schema"]["required"])
        self.assertIn("discovery", payload["text"]["format"]["schema"]["required"])
        self.assertIn("別の関心", payload["instructions"])
        self.assertEqual(payload["max_output_tokens"], 900)

    async def test_self_time_repairs_invalid_record_without_tools(self):
        from unittest.mock import patch
        good = {"action": "finish", "note": "持ち物を確認した", "direction": "deepen",
                "discovery": "猫の絵があった", "mood_state": "普通", "mood_cause": "観察",
                "mood_strength": "ふつう", "mood_focus": "猫", "reflection": "絵を見返した"}
        for bad in ("", " \n ", "x" * 501, 42):
            with self.subTest(note=bad):
                def response(value):
                    return SimpleNamespace(output_text=json.dumps(value), output=[], usage=None, id="test")
                create = AsyncMock(side_effect=[response(dict(good, note=bad)), response(good)])
                decider = OpenAISelfTimeDecider(client=SimpleNamespace(responses=SimpleNamespace(create=create)), model="test")
                with patch("anima.adapters.openai.client.emit") as telemetry:
                    result = await decider.decide({}, 1, None)
                self.assertEqual(result.note, good["note"])
                self.assertEqual(create.await_count, 2)
                repair = create.call_args.kwargs
                self.assertEqual(repair["tools"], [])
                self.assertIn("ツールは再実行せず", repair["input"][-1]["content"])
                invalid = next(c.kwargs for c in telemetry.call_args_list if c.args[0] == "self_time.decision.invalid")
                self.assertTrue(invalid["retrying"])
                self.assertNotIn("note", invalid)
                self.assertTrue(any(c.args[0] == "self_time.decision.repaired" for c in telemetry.call_args_list))
        create = AsyncMock(return_value=response(dict(good, note="")))
        decider = OpenAISelfTimeDecider(client=SimpleNamespace(responses=SimpleNamespace(create=create)), model="test")
        with patch("anima.adapters.openai.client.emit") as telemetry:
            with self.assertRaisesRegex(ValueError, "activity note"):
                await decider.decide({}, 1, None)
        self.assertEqual(create.await_count, 2)
        invalid = [c.kwargs for c in telemetry.call_args_list if c.args[0] == "self_time.decision.invalid"]
        self.assertEqual([c["retrying"] for c in invalid], [True, False])
        create = AsyncMock(side_effect=[response(dict(good, unexpected="extra")), response(good)])
        decider = OpenAISelfTimeDecider(client=SimpleNamespace(responses=SimpleNamespace(create=create)), model="test")
        self.assertEqual((await decider.decide({}, 1, None)).action, "finish")

    async def test_self_time_decider_accepts_one_object_with_trailing_output(self):
        value = {
            "action": "finish", "note": "絵を見た", "mood_state": "満足",
            "mood_cause": "完成した", "mood_strength": "ふつう",
            "mood_focus": "絵", "reflection": "完成した絵を確認した。",
        }
        response = SimpleNamespace(
            output_text=json.dumps(value, ensure_ascii=False) + "\n余分な出力",
            output=[], id="trailing", usage=None,
        )
        decider = OpenAISelfTimeDecider(
            client=SimpleNamespace(responses=SimpleNamespace(
                create=AsyncMock(return_value=response),
            )),
            model="test",
        )

        decision = await decider.decide({}, 1, None)

        self.assertEqual(decision.action, "finish")
        self.assertEqual(decision.note, "絵を見た")

    def test_reasoning_summary_extracts_object_and_dict_items(self):
        response = SimpleNamespace(output=[
            {"type": "reasoning", "summary": [{"text": " 最初の検討 "}]},
            SimpleNamespace(type="reasoning", summary=[SimpleNamespace(text="次の判断")]),
            SimpleNamespace(type="message", summary=[SimpleNamespace(text="対象外")]),
        ])
        self.assertEqual(
            _reasoning_summary_text(response), "最初の検討\n\n次の判断",
        )
        self.assertEqual(_reasoning_summary_text(SimpleNamespace(output=None)), "")

    async def test_self_time_decider_discards_note_from_none_decision(self):
        response = SimpleNamespace(
            output_text=json.dumps({
                "action": "none", "note": "特に何もしなかった",
                "mood_state": "普通", "mood_cause": "平穏",
                "mood_strength": "弱い", "mood_focus": "",
            }),
            id="r1", usage=None,
        )
        decider = OpenAISelfTimeDecider(
            client=SimpleNamespace(responses=SimpleNamespace(
                create=AsyncMock(return_value=response),
            )),
            model="test",
        )

        decision = await decider.decide({}, 1, None)

        self.assertEqual(decision.action, "none")
        self.assertEqual(decision.note, "")

    async def test_self_time_decider_executes_only_allowlisted_tools(self):
        schema = {
            "type": "object", "properties": {
                "collection": {"type": "string"},
                "resource_id": {"type": "string"},
                "mode": {"type": "string"},
                "content": {"type": "string"},
            }, "required": ["collection", "resource_id", "mode", "content"],
            "additionalProperties": False,
        }

        class Provider:
            def __init__(self): self.calls = []
            async def tools(self, context):
                self.context = context
                return (
                    FunctionToolSpec("resource_write", "文章を保存する。", schema, side_effect=True),
                    FunctionToolSpec("play_music", "音楽を再生する。", {
                        "type": "object", "properties": {}, "required": [],
                        "additionalProperties": False,
                    }, side_effect=True),
                    FunctionToolSpec("safe_plugin_action", "安全な内部操作。", {
                        "type": "object", "properties": {}, "required": [],
                        "additionalProperties": False,
                    }),
                )
            async def execute_tool(self, name, arguments, context, invocation_id):
                self.calls.append((name, arguments, context, invocation_id))
                return ToolResult("success", {"ok": True, "item": {"id": "idea.md"}})

        tool_response = SimpleNamespace(
            id="st-tool", output_text="", usage=None,
            output=[FakeFunctionCall(
                type="function_call", name="resource_write", call_id="st1",
                arguments=json.dumps({
                    "collection": "core.inventory", "resource_id": "idea.md",
                    "mode": "replace", "content": "猫の話",
                }),
            )],
        )
        final_response = SimpleNamespace(
            id="st-final", usage=None, output=[], output_text=json.dumps({
                "action": "finish", "note": "猫の話を書いて残した",
                "mood_state": "満足", "mood_cause": "文章を書いた",
                "mood_strength": "ふつう", "mood_focus": "猫",
            }, ensure_ascii=False),
        )
        create = AsyncMock(side_effect=[tool_response, final_response])
        provider = Provider()
        key = SandboxKey("guild", "3")
        decider = OpenAISelfTimeDecider(
            client=SimpleNamespace(responses=SimpleNamespace(create=create)), model="test",
            tool_registry=ToolRegistry((provider,)), sandbox_key=key,
            allowed_action_tools=frozenset({"safe_plugin_action"}),
        )
        result = await decider.decide({"mood": "普通"}, 1, None)
        self.assertEqual(result.note, "猫の話を書いて残した")
        self.assertEqual(provider.calls[0][0], "resource_write")
        self.assertEqual(provider.calls[0][2].source.sandbox_key, "guild:3")
        self.assertEqual(
            [tool["name"] for tool in create.await_args_list[0].kwargs["tools"]],
            ["resource_write", "safe_plugin_action"],
        )
        self.assertIn("function_call_output", str(create.await_args_list[1].kwargs["input"]))

        invalid_response = SimpleNamespace(id="invalid", usage=None, output=[],
            output_text=json.dumps({"action": "finish", "note": ""}))
        create.side_effect = [tool_response, invalid_response, final_response]
        create.reset_mock()
        provider.calls.clear()
        await decider.decide({"mood": "普通"}, 1, None)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(create.await_count, 3)
        self.assertEqual(create.call_args.kwargs["tools"], [])
        self.assertIn("function_call_output", str(create.call_args.kwargs["input"]))

    async def test_self_time_decider_allows_and_records_native_web_search(self):
        web_call = SimpleNamespace(
            type="web_search_call", id="ws1", status="completed",
            action=SimpleNamespace(query="猫の睡眠", sources=[
                SimpleNamespace(title="猫資料", url="https://example.test/cat"),
            ]),
        )
        response = SimpleNamespace(
            id="searched", usage=None, output=[web_call], output_text=json.dumps({
                "action": "none", "note": "", "mood_state": "普通",
                "mood_cause": "猫を調べた", "mood_strength": "弱い",
                "mood_focus": "猫", "reflection": "猫の睡眠が気になって調べた",
            }, ensure_ascii=False),
        )
        create = AsyncMock(return_value=response)
        decider = OpenAISelfTimeDecider(
            client=SimpleNamespace(responses=SimpleNamespace(create=create)), model="test",
            tool_registry=ToolRegistry((WebSearchToolProvider(enabled=True),)),
            sandbox_key=SandboxKey("guild", "3"),
        )

        result = await decider.decide({}, 1, None)

        self.assertEqual(result.action, "none")
        payload = create.await_args.kwargs
        self.assertEqual(payload["tools"], [{"type": "web_search"}])
        self.assertEqual(payload["include"], ["web_search_call.action.sources"])

    async def test_self_time_decider_rejects_invalid_tool_configuration(self):
        with self.assertRaises(ValueError):
            OpenAISelfTimeDecider(client=object(), model="test", max_tool_rounds=0)
        decider = OpenAISelfTimeDecider(
            client=object(), model="test", tool_registry=ToolRegistry(),
        )
        with self.assertRaises(RuntimeError):
            await decider.decide({}, 1, None)

    async def test_self_time_decider_observes_image_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "draft.png"
            image.write_bytes(b"image-bytes")

            class Provider:
                async def tools(self, context):
                    return (FunctionToolSpec("resource_read", "画像を見る。", {
                        "type": "object", "properties": {
                            "collection": {"type": "string"},
                            "resource_id": {"type": "string"},
                        }, "required": ["collection", "resource_id"],
                        "additionalProperties": False,
                    }, side_effect=True),)
                async def execute_tool(self, name, arguments, context, invocation_id):
                    return ToolResult("success", {"ok": True}, attachments=(image,))

            responses = SimpleNamespace(create=AsyncMock(side_effect=[
                SimpleNamespace(
                    id="read-image", output_text="", usage=None,
                    output=[FakeFunctionCall(
                        type="function_call", name="resource_read", call_id="p1",
                        arguments='{"collection":"core.temporary_artifacts","resource_id":"draft.png"}',
                    )],
                ),
                SimpleNamespace(id="done", usage=None, output=[], output_text=json.dumps({
                    "action": "finish", "note": "絵を見て残すことにした",
                    "mood_state": "満足", "mood_cause": "絵を見た",
                    "mood_strength": "ふつう", "mood_focus": "絵",
                }, ensure_ascii=False)),
            ]))
            decider = OpenAISelfTimeDecider(
                client=SimpleNamespace(responses=responses), model="test",
                tool_registry=ToolRegistry((Provider(),)),
                sandbox_key=SandboxKey("guild", "3"),
            )
            await decider.decide({}, 1, None)
            second_input = responses.create.await_args_list[1].kwargs["input"]
            image_parts = [
                part for item in second_input if item.get("role") == "user"
                for part in item["content"] if part["type"] == "input_image"
            ]
            self.assertTrue(image_parts[0]["image_url"].startswith("data:image/png;base64,"))

    def test_image_download_error_detection_is_narrow(self) -> None:
        self.assertTrue(_is_image_download_error(ImageDownloadFailure()))
        other = ImageDownloadFailure()
        other.status_code = 429
        self.assertFalse(_is_image_download_error(other))
        self.assertFalse(_is_image_download_error(RuntimeError("network failed")))

    def test_input_images_can_be_removed_without_losing_text(self) -> None:
        original = [
            {"role": "user", "content": [
                {"type": "input_text", "text": "利用者: ペルソナ装備"},
                {"type": "input_image", "image_url": "https://cdn.discordapp.com/lost.png"},
            ]},
            {"role": "assistant", "content": "かわいいね"},
        ]
        self.assertEqual(_without_input_images(original), [
            {"role": "user", "content": [
                {"type": "input_text", "text": "利用者: ペルソナ装備"},
            ]},
            {"role": "assistant", "content": "かわいいね"},
        ])
        self.assertEqual(len(original[0]["content"]), 2)
        self.assertEqual(
            _input_image_urls(original), {"https://cdn.discordapp.com/lost.png"}
        )

        kept = _without_input_images(original, {"https://example.test/other.png"})
        self.assertEqual(len(kept[0]["content"]), 2)

    async def test_responder_retries_without_images_when_discord_url_is_unavailable(self) -> None:
        responses = FailingImageResponses()
        responder = OpenAIResponder(
            api_key="unused", model="gpt-5.6-luna",
            client=SimpleNamespace(responses=responses),
        )

        with self.assertLogs("anima.telemetry", level="INFO") as captured:
            draft = await responder.respond(Context(
                instructions="ペルソナとして話す",
                input=({"role": "user", "content": [
                    {"type": "input_text", "text": "利用者: ペルソナ装備"},
                    {"type": "input_image", "image_url": "https://cdn.discordapp.com/lost.png"},
                ]},),
                state_version=1,
            ))

        self.assertEqual(draft.reply, "おかえり。")
        self.assertEqual(len(responses.payloads), 2)
        self.assertEqual(responses.payloads[1]["input"][0]["content"], [
            {"type": "input_text", "text": "利用者: ペルソナ装備"},
        ])
        self.assertIn('"event":"openai.image.unavailable"', "\n".join(captured.output))

        await responder.respond(Context(
            instructions="ペルソナとして話す",
            input=({"role": "user", "content": [
                {"type": "input_text", "text": "利用者: もう一度"},
                {"type": "input_image", "image_url": "https://cdn.discordapp.com/lost.png"},
                {"type": "input_image", "image_url": "https://example.test/new.png"},
            ]},),
            state_version=2,
        ))
        self.assertEqual(len(responses.payloads), 3)
        self.assertEqual(responses.payloads[2]["input"][0]["content"], [
            {"type": "input_text", "text": "利用者: もう一度"},
            {"type": "input_image", "image_url": "https://example.test/new.png"},
        ])

    async def test_responder_does_not_hide_unrelated_request_failure(self) -> None:
        responses = SimpleNamespace(create=AsyncMock(side_effect=RuntimeError("offline")))
        responder = OpenAIResponder(
            api_key="unused", model="gpt-5.6-luna",
            client=SimpleNamespace(responses=responses),
        )

        with self.assertRaisesRegex(RuntimeError, "offline"):
            await responder.respond(Context(
                instructions="ペルソナとして話す",
                input=({"role": "user", "content": "利用者: おーい"},),
                state_version=1,
            ))

    async def test_responder_exposes_sandbox_vector_store_as_file_search(self) -> None:
        value = {
            "reply": "前に猫が好きって聞いたよ。", "research_summary": None,
            "music_refs": [],
            "mood": {"state": "うれしい", "cause": "思い出した", "strength": "ふつう", "focus": "猫"},
        }
        responses = SimpleNamespace(create=AsyncMock(return_value=SimpleNamespace(
            id="r1", output_text=json.dumps(value, ensure_ascii=False), usage=None,
            output=[SimpleNamespace(type="file_search_call", queries=["猫の好み"], results=[])],
        )))
        vector_store = SimpleNamespace(ensure=AsyncMock(return_value="vs_guild_1"))
        responder = OpenAIResponder(
            api_key="unused", model="gpt-5.6-luna",
            client=SimpleNamespace(responses=responses), memory_vector_store=vector_store,
        )

        draft = await responder.respond(Context(
            instructions="ペルソナとして話す", input=(), state_version=1,
        ))

        self.assertEqual(draft.acts, ("file_search",))
        self.assertEqual(responses.create.await_args.kwargs["tools"], [{
            "type": "file_search", "vector_store_ids": ["vs_guild_1"],
            "max_num_results": 5,
        }])
        self.assertEqual(
            responses.create.await_args.kwargs["include"], ["file_search_call.results"]
        )

    async def test_vector_store_sync_failure_does_not_block_response(self) -> None:
        from anima.core.models import MentionedPerson
        source = Event("job", datetime.now().astimezone(), "channel", "c", "#c",
                       "self", "システム", "生成失敗",
                       response_target=MentionedPerson("requester", "依頼者"))
        client = FakeOpenAIClient()
        vector_store = SimpleNamespace(ensure=AsyncMock(side_effect=RuntimeError("offline")))
        retriever = SimpleNamespace(prepare_recall=AsyncMock(return_value=MemoryRecall((
            MemoryPassage("memory/people/friend.md", 2, "友達\n- 猫が好き", 80),
        ))))
        responder = OpenAIResponder(
            api_key="unused", model="gpt-5.6-luna", client=client,
            memory_vector_store=vector_store,
            memory_retriever=retriever,
        )

        with self.assertLogs("anima.telemetry", level="INFO") as captured:
            draft = await responder.respond(Context(
                instructions="ペルソナとして話す", input=(), state_version=1,
                source_event=source,
            ))

        self.assertEqual(draft.reply, "おかえり。")
        self.assertEqual(client.responses.payload["tools"], [])
        self.assertEqual(retriever.prepare_recall.await_args.args[0].person_id, "requester")
        self.assertNotIn("include", client.responses.payload)
        self.assertIn("関連して思い出したこと", client.responses.payload["instructions"])
        self.assertIn("猫が好き", client.responses.payload["instructions"])
        self.assertIn('"event":"memory.vector_store.failed"', "\n".join(captured.output))
        self.assertIn('"error_type":"RuntimeError"', "\n".join(captured.output))

    async def test_resource_image_read_does_not_automatically_send_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "black-cat-witch.png"
            image.write_bytes(b"image")
            for attach in (False, True):
                class Provider:
                    async def tools(self, context):
                        return (FunctionToolSpec("resource_read", "read", {
                            "type": "object", "properties": {},
                            "required": [], "additionalProperties": False,
                        }, requires_source=False),)
                    async def execute_tool(self, name, arguments, context, invocation_id):
                        return ToolResult("success", {"attach_to_reply": attach}, attachments=(image,))

                responses = SimpleNamespace(create=AsyncMock(side_effect=[
                    SimpleNamespace(id="read", usage=None, output_text="", output=[
                        FakeFunctionCall(type="function_call", name="resource_read", call_id="r", arguments="{}"),
                    ]),
                    SimpleNamespace(id="reply", usage=None, output=[], output_text=json.dumps({
                        "reply": "この絵の話を続けよう", "research_summary": None,
                        "mood": {"state": "普通", "cause": "会話", "strength": "ふつう", "focus": "絵"},
                    })),
                ]))
                responder = OpenAIResponder(
                    api_key="unused", model="test", client=SimpleNamespace(responses=responses),
                    tool_registry=ToolRegistry((Provider(),)), sandbox_key=SandboxKey("guild", "3"),
                )
                draft = await responder.respond(Context(instructions="会話", input=(), state_version=1))
                self.assertEqual(draft.images, (image,) if attach else ())
                second_input = responses.create.await_args_list[1].kwargs["input"]
                self.assertTrue(any(
                    part.get("type") == "input_image"
                    for item in second_input if item.get("role") == "user"
                    for part in item["content"]
                ))

    def test_capability_specs_and_references_are_adapted(self) -> None:
        schema = {
            "type": "object", "properties": {}, "required": [],
            "additionalProperties": False,
        }
        self.assertEqual(_openai_tool_spec(FunctionToolSpec("echo", "Echo", schema)), {
            "type": "function", "name": "echo", "description": "Echo",
            "strict": True, "parameters": schema,
        })
        self.assertEqual(
            _openai_tool_spec(NativeToolSpec("web_search", {"depth": 1})),
            {"type": "web_search", "depth": 1},
        )

    def test_tool_log_fields_keep_diagnostics_without_full_payload(self) -> None:
        self.assertEqual(_tool_log_fields("not-json"), {"payload_valid": False})
        self.assertEqual(_tool_log_fields("[]"), {"payload_valid": False})
        request = _tool_log_fields(json.dumps({
            "query": "元気な曲", "limit": 3, "identifier": "recent",
            "include_lyrics": True, "percent": 35, "theme": "夏の夜",
        }))
        self.assertEqual(request["query"], "元気な曲")
        self.assertEqual(request["limit"], 3)
        self.assertEqual(request["identifier"], "recent")
        self.assertTrue(request["include_lyrics"])
        self.assertEqual(request["percent"], 35)
        self.assertEqual(request["theme"], "夏の夜")
        result = _tool_log_fields(json.dumps({
            "ok": False,
            "error": "VCに参加してください",
            "playing": False,
            "results": [{"id": "a", "lyrics": "記録しない"}, {"id": "b"}],
            "lyrics": "これも記録しない",
        }, ensure_ascii=False), result=True)
        self.assertEqual(result["result_count"], 2)
        self.assertEqual(result["result_ids"], ["a", "b"])
        self.assertNotIn("lyrics", result)
        self.assertEqual(
            _tool_log_fields('{"track":{"id":"now"}}', result=True)["track_id"],
            "now",
        )
        self.assertEqual(_tool_log_fields('{"id":"detail"}', result=True)["track_id"], "detail")

    async def test_short_tool_instructions_are_not_modified(self) -> None:
        self.assertEqual(_compact_tool_instructions("short"), "short")

    async def test_reflection_uses_upper_model_and_high_effort(self) -> None:
        client = FakeMaintenanceClient(
            [
                {
                    "changed": True,
                    "habits": ["人の話は最後まで\n聞く"],
                    "conflict": None,
                }
            ]
        )
        maintainer = OpenAIMemoryMaintainer(
            api_key="unused",
            model="gpt-5.6-luna",
            reflection_model="gpt-5.6-sol",
            client=client,
        )

        draft = await maintainer.reflect(
            ReflectionJob(0, "ペルソナ", "", "- [2026-09-01 #general] 話を聞いた")
        )

        self.assertTrue(draft.changed)
        self.assertEqual(draft.habitus, "- 人の話は最後まで 聞く")
        payload = client.responses.payloads[0]
        self.assertEqual(payload["model"], "gpt-5.6-sol")
        self.assertEqual(payload["reasoning"], {"effort": "high"})
        self.assertEqual(payload["text"]["format"]["name"], "anima_reflection")

    async def test_responder_uses_stateless_strict_structured_output(self) -> None:
        client = FakeOpenAIClient()
        responder = OpenAIResponder(
            api_key="unused",
            model="gpt-5.6-luna",
            client=client,
            tool_registry=ToolRegistry((WebSearchToolProvider(enabled=True),)),
            sandbox_key=SandboxKey("guild", "1"),
        )

        draft = await responder.respond(
            Context(
                instructions="ペルソナとして話す",
                input=({"role": "user", "content": "太郎: ただいま"},),
                state_version=3,
            )
        )

        payload = client.responses.payload
        self.assertEqual(payload["model"], "gpt-5.6-luna")
        self.assertFalse(payload["store"])
        self.assertEqual(payload["reasoning"], {"effort": "none"})
        self.assertEqual(payload["text"]["format"]["type"], "json_schema")
        self.assertTrue(payload["text"]["format"]["strict"])
        self.assertEqual(payload["tools"], [{"type": "web_search"}])
        self.assertEqual(payload["include"], ["web_search_call.action.sources"])
        self.assertIn("research_summary", payload["text"]["format"]["schema"]["required"])
        self.assertEqual(draft.reply, "おかえり。")
        self.assertEqual(draft.acts, ("web_search",))
        self.assertEqual(draft.actions[0].plugin, "web_search")
        self.assertEqual(draft.references[0].summary, "挨拶資料")
        self.assertEqual(draft.research.summary, "帰宅への挨拶について調べた。")
        self.assertEqual(draft.research.queries, ("帰宅 挨拶",))
        self.assertEqual(len(draft.research.sources), 3)
        self.assertEqual(draft.research.sources[0].url, "https://example.com/greeting")

    async def test_responder_normalizes_literal_newline_codes_in_reply(self) -> None:
        client = FakeOpenAIClient()
        client.responses.reply = "天使化……\\r\\n. + ⊂⊃ +\\n¥nペルソナ"
        responder = OpenAIResponder(
            api_key="unused", model="gpt-5.6-luna", client=client
        )

        draft = await responder.respond(
            Context(instructions="ペルソナとして話す", input=(), state_version=1)
        )

        self.assertEqual(draft.reply, "天使化……\n. + ⊂⊃ +\n\nペルソナ")

    async def test_tool_registry_requires_trusted_sandbox_without_source(self) -> None:
        responder = OpenAIResponder(
            api_key="unused", model="gpt-5.6-luna", client=FakeOpenAIClient(),
            tool_registry=ToolRegistry(),
        )
        with self.assertRaisesRegex(RuntimeError, "sandbox key"):
            await responder.respond(Context(instructions="x", input=(), state_version=1))

    async def test_memory_maintainer_uses_separate_digest_and_sleep_schemas(self) -> None:
        client = FakeMaintenanceClient(
            (
                {"entries": [{"time": "12:00", "place": "#general", "content": "帰宅の\n話"}]},
                {
                    "memories": [
                        {
                            "scope": "self",
                            "key": "self",
                            "heading": "自分",
                            "relationship": None,
                            "entries": [{
                                "date": "2026-09-01", "place": "#general",
                                "source": "自分", "content": "自分の記憶", "strong": False,
                            }],
                        },
                        {
                            "scope": "world", "key": "world", "heading": None,
                            "relationship": None, "entries": [],
                        },
                    ],
                    "open_items": [{
                        "date": "2026-09-01", "place": "#general", "content": "また話す",
                    }],
                    "mood": {
                        "state": "すっきり",
                        "cause": "眠った",
                        "strength": "弱い",
                        "focus": "",
                    },
                },
            )
        )
        maintainer = OpenAIMemoryMaintainer(
            api_key="unused", model="gpt-5.6-luna", client=client
        )
        now = datetime(2026, 9, 1, tzinfo=timezone.utc)
        log_event = Event(
            id="m1",
            ts=now,
            kind="channel",
            channel_id="general",
            channel_name="#general",
            author_id="u1",
            author_name="太郎",
            text="ただいま",
            mentioned_people=(MentionedPerson("u2", "花子"),),
        )

        digest = await maintainer.digest(DigestJob(0, (log_event,), ""))
        sleep = await maintainer.sleep(
            SleepJob(
                expected_version=1,
                events=(log_event,),
                digest=digest,
                open_items="",
                mood=Mood("穏やか", "特にない", "弱い", "", now),
                persona="ペルソナ",
                rules="自然に話す",
                habitus="- 人の話は最後まで聞く",
                self_memory="",
                world_memory="",
                channel_memories=(),
                people_memories=(),
            )
        )

        self.assertEqual(digest, "- [12:00 #general] 帰宅の 話")
        self.assertEqual(sleep.mood_state, "すっきり")
        self.assertIn("- [2026-09-01 #general 自分] 自分の記憶", sleep.memories[0].content)
        self.assertEqual(sleep.open_items, "- [2026-09-01 #general] また話す")
        sleep_input = json.loads(client.responses.payloads[1]["input"])
        self.assertEqual(sleep_input["habitus"], "- 人の話は最後まで聞く")
        self.assertEqual(
            sleep_input["events"][0]["mentioned_people"],
            [{"id": "u2", "name": "花子"}],
        )
        self.assertEqual(client.responses.payloads[0]["reasoning"], {"effort": "low"})
        self.assertEqual(client.responses.payloads[1]["reasoning"], {"effort": "high"})
        self.assertEqual(
            client.responses.payloads[0]["text"]["format"]["name"], "anima_digest"
        )
        self.assertEqual(
            client.responses.payloads[1]["text"]["format"]["name"], "anima_sleep"
        )

    def test_sleep_memory_renderer_caps_strong_entries_instead_of_failing(self) -> None:
        document = {
            "heading": "自分",
            "relationship": None,
            "entries": [
                {
                    "date": "2026-09-15",
                    "place": "#general",
                    "source": None,
                    "content": f"強い記憶{index}",
                    "strong": True,
                }
                for index in range(12)
            ],
        }

        rendered = OpenAIMemoryMaintainer._memory_text(document, strong_max=10)

        self.assertEqual(rendered.count("※強"), 10)
        self.assertIn("強い記憶11", rendered)
        with self.assertRaisesRegex(ValueError, "non-negative"):
            OpenAIMemoryMaintainer._memory_text(document, strong_max=-1)
        with self.assertRaisesRegex(ValueError, "non-negative"):
            OpenAIMemoryMaintainer(
                api_key="unused", model="test", memory_strong_max=-1
            )

    def test_sleep_merges_duplicate_memory_documents_without_losing_entries(self) -> None:
        first_entry = {
            "date": "2026-09-21", "place": "#general", "source": None,
            "content": "最初の記憶", "strong": False,
        }
        second_entry = {
            "date": "2026-09-21", "place": "#general", "source": "太郎",
            "content": "別の記憶", "strong": True,
        }
        documents = [
            {
                "scope": "self", "key": "self", "heading": "古い見出し",
                "relationship": None, "entries": [first_entry],
            },
            {
                "scope": "world", "key": "world", "heading": None,
                "relationship": None, "entries": [],
            },
            {
                "scope": "self", "key": "self", "heading": "新しい見出し",
                "relationship": "大切", "entries": [first_entry, second_entry],
            },
        ]

        merged = OpenAIMemoryMaintainer._merge_memory_documents(documents)

        self.assertEqual([(item["scope"], item["key"]) for item in merged], [
            ("self", "self"), ("world", "world"),
        ])
        self.assertEqual(merged[0]["heading"], "新しい見出し")
        self.assertEqual(merged[0]["relationship"], "大切")
        self.assertEqual(merged[0]["entries"], [first_entry, second_entry])
        self.assertIsNot(merged[0]["entries"], documents[0]["entries"])

    def test_digest_renderer_rejects_invalid_structured_fields(self) -> None:
        render = OpenAIMemoryMaintainer._digest_text
        for entries, message in (
            ([], "must not be empty"),
            ([{"time": "25:00", "place": "#general", "content": "話"}], "time"),
            ([{"time": "12:00", "place": "general", "content": "話"}], "place"),
            ([{"time": "12:00", "place": "DM", "content": " \n "}], "content"),
        ):
            with self.subTest(entries=entries), self.assertRaisesRegex(ValueError, message):
                render(entries)

    def test_sleep_and_reflection_renderers_own_markdown_format(self) -> None:
        maintainer = OpenAIMemoryMaintainer
        document = {
            "heading": " 太郎 ", "relationship": "友達\nかも", "entries": [{
                "date": "2026-09-01", "place": "DM", "source": "太郎→自分",
                "content": "大事な話 ※強", "strong": True,
            }],
        }
        self.assertEqual(
            maintainer._memory_text(document),
            "## 太郎\n関係: 友達 かも\n- [2026-09-01 DM 太郎→自分] 大事な話 ※強",
        )
        self.assertEqual(
            maintainer._open_text([{
                "date": "2026-09-01", "place": "#general", "content": "また\n話す",
            }]),
            "- [2026-09-01 #general] また 話す",
        )
        self.assertEqual(
            maintainer._habitus_text(["- よく聞く", " 猫が好き "]),
            "- よく聞く\n- 猫が好き",
        )

    def test_sleep_renderers_reject_invalid_structured_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "memory date"):
            OpenAIMemoryMaintainer._memory_text({
                "heading": None, "relationship": None,
                "entries": [{"date": "today", "place": "DM", "source": None,
                             "content": "話", "strong": False}],
            })
        with self.assertRaisesRegex(ValueError, "open place"):
            OpenAIMemoryMaintainer._open_text([
                {"date": "2026-09-01", "place": "general", "content": "話"}
            ])
        with self.assertRaisesRegex(ValueError, "habit"):
            OpenAIMemoryMaintainer._habitus_text([" \n "])


if __name__ == "__main__":
    unittest.main()
