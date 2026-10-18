from datetime import datetime, timezone
from types import SimpleNamespace
import json
import unittest
from unittest.mock import patch

from anima.core.models import Event
from anima.core.observations import RuntimeObservationRegistry, RuntimeStateProvider
from anima.core.sandbox import SandboxKey
from anima.core.context_usage import context_usage
from anima.core.context import ContextBuilder
from anima.core.models import Mood, StateSnapshot


class RuntimeObservationTests(unittest.TestCase):
    def setUp(self):
        self.key = SandboxKey("guild", "123")
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.source = Event("1", self.now, "channel", "c", "channel", "u", "user",
                            "hello", guild_id="123")
        self.registry = RuntimeObservationRegistry(self.key, clock=lambda: self.now)

    def test_refresh_and_source_and_ownership_and_accounting(self):
        state = {"active": False, "count": 0, "name": None, "volume": 0.5}
        received = []
        def observe(source):
            received.append(source)
            return state
        self.registry.replace_providers([RuntimeStateProvider("example", tuple(state), observe, "Automatic reply.")])
        with patch("anima.core.observations.emit") as emit:
            first = self.registry.collect(self.source)
        self.assertIn('"active": false', first[0][1])
        self.assertIn('"sandbox_key": "guild:123"', first[0][1])
        self.assertIn('"observed_at": "2026-01-01T00:00:00+00:00"', first[0][1])
        self.assertIn("Automatic reply.", first[0][1])
        self.assertEqual(first[0][0], "example")
        self.assertEqual(emit.call_args.kwargs["status"], "known")
        state["active"] = True
        self.assertIn('"active": true', self.registry.collect(self.source)[0][1])
        self.registry.collect(None)
        self.assertEqual(received, [self.source, self.source, None])
        measured = context_usage({"instructions": first[0][1]}, observation_parts=first)
        self.assertGreater(next(row for row in measured["components"] if row["plugin"] == "example")["instructions"], 0)
        self.assertEqual(sum(row["total"] for row in measured["components"]), measured["total"])
        self.registry.replace_providers([])
        self.assertEqual(self.registry.collect(self.source), ())
        self.assertEqual(RuntimeObservationRegistry(self.key).collect(None), ())

    def test_invalid_registration(self):
        for args in (("BAD", ("x",), lambda _: {}), ("p", (), lambda _: {}),
                     ("p", ("x", "x"), lambda _: {}), ("p", ("BAD",), lambda _: {}),
                     ("p", ("x",), None), ("p", tuple(f"x{i}" for i in range(17)), lambda _: {})):
            with self.subTest(args=args), self.assertRaises(ValueError):
                RuntimeStateProvider(*args)
        with self.assertRaises(ValueError):
            RuntimeStateProvider("p", ("x",), lambda _: {}, "a" * 601)
        p = RuntimeStateProvider("p", ("x",), lambda _: {})
        for providers in ([object()], [p, p], [p] * 33):
            with self.assertRaises(ValueError):
                self.registry.replace_providers(providers)
        with self.assertRaises(ValueError):
            self.registry.collect(Event("2", self.now, "channel", "c", "c", "u", "u", "hi", guild_id="456"))
        self.registry.clock = lambda: datetime(2026, 1, 1)
        with self.assertRaises(ValueError):
            self.registry.collect(None)

    def test_context_defers_collection_until_requested(self):
        snapshot = StateSnapshot(version=1, persona="p", rules="r", habitus="",
            mood=Mood("calm", "test", "ふつう", "", self.now), open_items="", digest="",
            self_memory="", world_memory="", channel_memory="", people_memory=())
        registry = SimpleNamespace(collect=lambda source: (("test", source.id),))
        context = ContextBuilder(observations=registry).build(snapshot, now=self.now, source_event=self.source)
        self.assertEqual(context.runtime_observations(), (("test", "1"),))

    def test_invalid_observations_are_unknown_and_isolated(self):
        invalid = [{"extra": 1}, {"x": []}, {"x": float("nan")}, {"x": "a" * 257},
                   {f"x{i}": "a" * 256 for i in range(8)}, None]
        for value in invalid:
            with self.subTest(value=value):
                self.registry.replace_providers([
                    RuntimeStateProvider("bad", tuple(["x", *(f"x{i}" for i in range(8))]), lambda _: value, "Still automatic."),
                    RuntimeStateProvider("good", ("available",), lambda _: {"available": False}),
                ])
                parts = self.registry.collect(self.source)
                self.assertIn('"status": "unknown"', parts[0][1])
                self.assertIn("Still automatic.", parts[0][1])
                self.assertNotIn('"state":', parts[0][1])
                self.assertIn('"available": false', parts[1][1])
        def fail(_):
            raise PermissionError("private detail")
        self.registry.replace_providers([RuntimeStateProvider("bad", ("x",), fail)])
        text = self.registry.collect(None)[0][1]
        self.assertIn("PermissionError", text)
        self.assertNotIn("private detail", text)


class ObservationLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_observation_refreshes_after_tool_and_survives_compaction(self):
        from test_adapters import FakeResponses
        from anima.adapters.openai.client import OpenAIResponder
        from anima.capabilities.contracts import ToolRegistry, FunctionToolSpec, ToolResult
        from anima.core.models import Context
        state = {"active": False}
        registry = RuntimeObservationRegistry(SandboxKey("guild", "123"))
        registry.replace_providers([RuntimeStateProvider("example", ("active",), lambda _: state, "Reply automatically.")])
        class Provider:
            async def tools(self, context):
                return (FunctionToolSpec("activate", "Activate", {"type": "object", "properties": {},
                    "required": [], "additionalProperties": False}, requires_source=False, side_effect=False),)
            async def execute_tool(self, name, arguments, context, invocation_id):
                state["active"] = True
                return ToolResult("success", {"ok": True})
        requests = []
        async def create(**payload):
            requests.append(payload)
            if len(requests) == 1:
                return SimpleNamespace(output=[SimpleNamespace(type="function_call", call_id="call1",
                    name="activate", arguments="{}")], usage=None)
            response = await FakeResponses().create(**payload)
            response.output = []
            return response
        responder = OpenAIResponder(api_key="unused", model="test-model",
            client=SimpleNamespace(responses=SimpleNamespace(create=create)),
            tool_registry=ToolRegistry((Provider(),)), sandbox_key=SandboxKey("guild", "123"))
        await responder.respond(Context("long memory " * 1000, ({"role": "user", "content": "hello"},), 1,
            runtime_observations=lambda: registry.collect(None)))
        self.assertEqual(len(requests), 2)
        self.assertIn('"active": false', requests[0]["instructions"])
        self.assertIn('"active": true', requests[1]["instructions"])
        self.assertNotIn('"active": false', requests[1]["instructions"])
        self.assertIn("Reply automatically.", requests[1]["instructions"])
        self.assertLess(len(requests[1]["instructions"]), len(requests[0]["instructions"]))
