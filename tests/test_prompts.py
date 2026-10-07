import json
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from anima.core.models import Context, DigestJob, Mood, ReflectionJob, SleepJob
from anima.adapters.openai.client import OpenAIMemoryMaintainer, OpenAIResponder
from anima.core.prompts import NAP, REFLECT, RESPOND, SLEEP, PromptSpec
from anima.adapters.discord.expressions import FACE_GUIDANCE


class PromptSpecTests(unittest.TestCase):
    def test_sleep_consolidates_people_without_losing_identity_or_provenance(self):
        self.assertEqual(SLEEP.version, "7")
        for rule in ("既存entriesをすべてそのまま残す意味ではない", "日付ごとに追加しない",
                     "呼び名", "本人の発言と他人からの紹介を混ぜない", "件数を減らすためだけに捨てない"):
            self.assertIn(rule, SLEEP.instructions)

    def test_validation_and_immutable_metadata(self):
        for args in [("", "1", "low"), ("x", " ", "low"), ("x", "1", "invalid")]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                PromptSpec(*args)
        for effort in ("none", "low", "medium", "high"):
            spec = PromptSpec("test", "1", effort, "private text")
            self.assertEqual(spec.log_fields("test-model"), {
                "prompt_name": "test", "prompt_version": "1",
                "model": "test-model", "effort": effort,
            })
            with self.assertRaises(FrozenInstanceError):
                spec.version = "2"


class PromptIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_operations_send_registered_prompt_and_log_metadata(self):
        mood = {"state": "穏やか", "cause": "", "strength": "弱い", "focus": ""}
        now = datetime.now(timezone.utc)
        outputs = [
            {"reply": "hello", "research_summary": None, "mood": mood},
            {"entries": [{"time": "12:00", "place": "#test", "content": "hello"}]},
            {"memories": [], "open_items": [], "mood": mood},
            {"changed": False, "habits": [], "conflict": None},
        ]
        create = AsyncMock(side_effect=[SimpleNamespace(
            output_text=json.dumps(output), output=[],
            usage=SimpleNamespace(input_tokens=2, output_tokens=3, total_tokens=5),
        ) for output in outputs])
        client = SimpleNamespace(responses=SimpleNamespace(create=create))
        responder = OpenAIResponder(api_key="unused", model="response-model", client=client)
        maintainer = OpenAIMemoryMaintainer(
            api_key="unused", model="memory-model", reflection_model="upper-model", client=client,
        )
        with self.assertLogs("anima.telemetry", level="INFO") as logs:
            await responder.respond(Context("private context", (), 0))
            await maintainer.digest(DigestJob(0, (), ""))
            await maintainer.sleep(SleepJob(
                expected_version=0, events=(), digest="", open_items="",
                mood=Mood("穏やか", "", "弱い", "", now), persona="private persona",
                rules="", habitus="", self_memory="", world_memory="",
                channel_memories=(), people_memories=(),
            ))
            await maintainer.reflect(ReflectionJob(0, "private persona", "", ""))
        events = [json.loads(record.getMessage()) for record in logs.records]
        measured = [event for event in events if event["event"] == "model.context.measured"]
        self.assertEqual(len(measured), 1)
        self.assertGreater(measured[0]["total"], 0)
        events = [event for event in events if event["event"] != "model.context.measured"]
        self.assertEqual(len(events), 9)
        for i, (spec, model, operation) in enumerate([
            (RESPOND, "response-model", "respond"), (NAP, "memory-model", "digest"),
            (SLEEP, "memory-model", "sleep"), (REFLECT, "upper-model", "reflection"),
        ]):
            payload = create.call_args_list[i].kwargs
            self.assertEqual(
                payload["instructions"],
                "private context\n\n" + FACE_GUIDANCE + "\n" + RESPOND.instructions
                if spec is RESPOND else spec.instructions,
            )
            self.assertEqual(payload["reasoning"], {"effort": spec.effort})
            self.assertEqual(payload["model"], model)
            self.assertFalse(payload["store"])
            start = 0 if i == 0 else 3 + (i - 1) * 2
            operation_events = events[start:start + (3 if i == 0 else 2)]
            for event in operation_events:
                self.assertEqual(event["operation"], operation)
                if event["event"] != "openai.response.round_completed":
                    for key, value in spec.log_fields(model).items():
                        self.assertEqual(event[key], value)
            self.assertEqual(operation_events[0]["event"], "openai.request.started")
            self.assertNotIn("total_tokens", operation_events[0])
            self.assertEqual(operation_events[-1]["total_tokens"], 5)
        self.assertEqual(events[1]["event"], "openai.response.round_completed")
        self.assertNotIn("private", " ".join(logs.output))

    async def test_failed_call_still_logs_prompt_without_completion(self):
        create = AsyncMock(side_effect=RuntimeError("offline"))
        client = SimpleNamespace(responses=SimpleNamespace(create=create))
        maintainer = OpenAIMemoryMaintainer(api_key="unused", model="test-model", client=client)
        with self.assertLogs("anima.telemetry", level="INFO") as logs:
            with self.assertRaisesRegex(RuntimeError, "offline"):
                await maintainer.digest(DigestJob(0, (), ""))
        self.assertEqual(len(logs.records), 1)
        event = json.loads(logs.records[0].getMessage())
        self.assertEqual(event["event"], "openai.request.started")
        self.assertEqual(event["prompt_version"], NAP.version)
