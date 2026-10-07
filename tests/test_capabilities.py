from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import unittest
from zoneinfo import ZoneInfo

from anima.capabilities.contracts import (
    ActionRecord,
    CapabilityConfigurationError,
    CapabilityContext,
    ContextReference,
    FunctionToolSpec,
    MAX_ARGUMENT_BYTES,
    MAX_DESCRIPTION_LENGTH,
    MAX_MODEL_PAYLOAD_BYTES,
    MAX_REFERENCE_ATTRIBUTES,
    MAX_RESULT_ITEMS,
    MAX_SCHEMA_BYTES,
    MAX_SUMMARY_LENGTH,
    MAX_TOOL_COUNT,
    NativeToolEvent,
    NativeToolSpec,
    PermissionSet,
    ToolRegistry,
    ToolResult,
    normalized_text,
)
from anima.core.models import Event
from anima.core.sandbox import SandboxKey
from anima.capabilities.availability import Availability, AvailabilityStatus


JST = ZoneInfo("Asia/Tokyo")


def event(*, sandbox_key: str = "guild:1") -> Event:
    return Event(
        id="evt1", ts=datetime(2026, 9, 15, 12, 0, tzinfo=JST), kind="channel",
        channel_id="10", channel_name="#test", author_id="20", author_name="user",
        text="hello", guild_id="1", sandbox_key=sandbox_key,
    )


def object_schema(properties=None, required=None):
    return {
        "type": "object", "properties": properties or {},
        "required": required or [], "additionalProperties": False,
    }


class Provider:
    def __init__(self, specs, result=None):
        self.specs = specs
        self.result = result or ToolResult("success", {"ok": True})
        self.calls = []

    async def tools(self, context):
        self.context = context
        return self.specs

    async def execute_tool(self, name, arguments, context, invocation_id):
        self.calls.append((name, arguments, context, invocation_id))
        return self.result

    async def consume_native(self, events, context):
        self.native_call = (events, context)
        return self.result


class CapabilityValueTests(unittest.TestCase):
    def test_permissions_and_context_sandbox(self):
        permissions = PermissionSet(frozenset({"manage_sandbox"}))
        self.assertTrue(permissions.allows("manage_sandbox"))
        self.assertFalse(permissions.allows("missing"))
        context = CapabilityContext(event(), SandboxKey("guild", "1"), permissions)
        self.assertEqual(context.source.id, "evt1")
        with self.assertRaises(ValueError):
            CapabilityContext(event(), SandboxKey("guild", "2"))

    def test_action_and_reference_validation(self):
        action = ActionRecord("music", "play", "曲を再生した")
        reference = ContextReference("music", "track", "一曲目", (("id", "one"),))
        self.assertEqual(action.action, "play")
        self.assertEqual(reference.attributes, (("id", "one"),))
        self.assertEqual(ActionRecord.from_dict(action.to_dict()), action)
        self.assertEqual(ContextReference.from_dict(reference.to_dict()), reference)
        with self.assertRaisesRegex(ValueError, "attributes must be"):
            ContextReference.from_dict({
                "plugin": "music", "kind": "track", "summary": "x", "attributes": [],
            })
        invalid_actions = [
            ("Music", "play", "ok"), ("music", "bad-name", "ok"),
            ("music", "play", ""), ("music", "play", "x\ny"),
            ("music", "play", "x" * (MAX_SUMMARY_LENGTH + 1)),
        ]
        for values in invalid_actions:
            with self.subTest(values=values), self.assertRaises(ValueError):
                ActionRecord(*values)
        with self.assertRaises(ValueError):
            ContextReference("Music", "track", "ok")
        with self.assertRaises(ValueError):
            ContextReference("music", "bad-kind", "ok")
        with self.assertRaises(ValueError):
            ContextReference("music", "track", "ok", tuple((str(i), "v") for i in range(MAX_REFERENCE_ATTRIBUTES + 1)))
        for attributes in ((("", "v"),), (("k" * 65, "v"),), (("k\nx", "v"),),
                           (("k", "v" * 501),), (("k", "v\nx"),),
                           (("k", "a"), ("k", "b"))):
            with self.subTest(attributes=attributes), self.assertRaises(ValueError):
                ContextReference("music", "track", "ok", attributes)

    def test_tool_result_validation_and_json(self):
        result = ToolResult("success", {"日本語": True}, usage_category="music")
        self.assertEqual(result.output_json(), '{"日本語":true}')
        for kwargs in (
            {"status": "failed", "model_payload": {}},
            {"status": "success", "model_payload": {"bad": Path("x")}},
            {"status": "success", "model_payload": {"x": "x" * MAX_MODEL_PAYLOAD_BYTES}},
            {"status": "success", "model_payload": {}, "usage_category": "bad-name"},
            {"status": "success", "model_payload": {}, "actions": tuple(ActionRecord("p", "a", "x") for _ in range(MAX_RESULT_ITEMS + 1))},
            {"status": "success", "model_payload": {}, "references": tuple(ContextReference("p", "r", "x") for _ in range(MAX_RESULT_ITEMS + 1))},
            {"status": "success", "model_payload": {}, "attachments": tuple(Path(str(i)) for i in range(MAX_RESULT_ITEMS + 1))},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ToolResult(**kwargs)

    def test_tool_spec_validation(self):
        spec = FunctionToolSpec("echo", "Echo", object_schema())
        self.assertTrue(spec.strict)
        NativeToolSpec("web_search", {"depth": 1})
        bad_specs = [
            lambda: FunctionToolSpec("Bad", "x", object_schema()),
            lambda: FunctionToolSpec("ok", "", object_schema()),
            lambda: FunctionToolSpec("ok", "x" * (MAX_DESCRIPTION_LENGTH + 1), object_schema()),
            lambda: FunctionToolSpec("ok", "x", {"type": "string"}),
            lambda: FunctionToolSpec("ok", "x", object_schema(), strict=False),
            lambda: FunctionToolSpec("ok", "x", {**object_schema(), "title": "x"}),
            lambda: FunctionToolSpec("ok", "x", {"type": "object", "properties": {}, "required": []}),
            lambda: NativeToolSpec("Bad"),
            lambda: NativeToolSpec("web_search", {"bad": Path("x")}),
        ]
        for make in bad_specs:
            with self.subTest(make=make), self.assertRaises((ValueError, CapabilityConfigurationError)):
                make()

    def test_supported_schema_shapes_and_invalid_definitions(self):
        valid = object_schema({
            "s": {
                "type": "string", "description": "Value to echo",
                "enum": ["a"], "minLength": 1, "maxLength": 2,
            },
            "i": {"type": "integer", "minimum": 0, "maximum": 2},
            "n": {"type": "number"}, "b": {"type": "boolean"},
            "a": {"type": "array", "items": {"type": "string"}, "minItems": 0, "maxItems": 2},
        }, ["s", "i", "n", "b", "a"])
        FunctionToolSpec("all_types", "all", valid)
        invalid = [
            {"type": "null"},
            object_schema({"x": {"type": "array"}}, []),
            {"type": "object", "properties": [], "required": [], "additionalProperties": False},
            {"type": "object", "properties": {}, "required": "x", "additionalProperties": False},
            object_schema({}, ["x"]), object_schema({}, [1]), object_schema({"x": {"type": "string"}}, ["x", "x"]),
            {"type": "string", "enum": []}, {"type": "string", "enum": "x"},
            {"type": "string", "description": ""},
            {"type": "string", "description": 1},
            {"type": "string", "description": "x" * (MAX_DESCRIPTION_LENGTH + 1)},
            {"type": "string", "minLength": "x"}, {"type": "string", "minLength": 2, "maxLength": 1},
            object_schema({"x": "not-a-schema"}),
            object_schema({"x": {"type": "string", "enum": []}}),
            object_schema({"x": {"type": "string", "minLength": "x"}}),
            object_schema({"x": {"type": "string", "maxLength": "x"}}),
            object_schema({"x": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 1}}),
        ]
        for index, schema in enumerate(invalid):
            with self.subTest(index=index), self.assertRaises(CapabilityConfigurationError):
                FunctionToolSpec(f"bad_{index}", "bad", schema)

    def test_normalized_text(self):
        self.assertEqual(normalized_text(" ＡＢＣ  Foo\nBAR "), "abc foo bar")

    def test_capability_metadata_survives_event_log(self):
        original = event()
        original = replace(
            original,
            actions=(ActionRecord("music", "search_music", "曲を検索した"),),
            references=(ContextReference("music", "track", "犬の歌", (("id", "dog"),)),),
        )
        restored = Event.from_log_dict(original.to_log_dict())
        self.assertEqual(restored.actions, original.actions)
        self.assertEqual(restored.references, original.references)


class ToolRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_failure_is_logged_and_propagated(self):
        from unittest.mock import patch

        class Failing(Provider):
            async def execute_tool(self, name, arguments, context, invocation_id):
                raise RuntimeError("offline")

        provider = Failing((FunctionToolSpec("read", "Read", object_schema(), requires_source=False),))
        prepared = await ToolRegistry((provider,), owners={id(provider): "sample"}).prepare(
            CapabilityContext(None, SandboxKey("guild", "1")),
        )
        with patch("anima.core.plugin_logs.plugin_log") as log:
            with self.assertRaisesRegex(RuntimeError, "offline"):
                await prepared.execute("read", "{}")
        self.assertEqual([call.args[:2] for call in log.call_args_list], [
            ("sample", "tool.started"), ("sample", "tool.failed"),
        ])
        self.assertEqual(log.call_args.kwargs["error_type"], "RuntimeError")

    async def test_instruction_validation_and_availability_ownership(self):
        class Conditional(Provider):
            instruction = ""
            status = AvailabilityStatus(Availability.DISABLED)

            def context_instructions(self, context):
                return self.instruction

            async def capability_availability(self, kind, name, context):
                return self.status

        provider = Conditional((NativeToolSpec("web_search"),))
        registry = ToolRegistry((provider,), owners={id(provider): "research"})
        context = CapabilityContext(None, SandboxKey("guild", "1"))
        prepared = await registry.prepare(context)
        self.assertEqual(prepared.context_owners, {})
        self.assertEqual(prepared.instruction_parts, ())
        self.assertEqual(prepared.native_kinds, frozenset())
        self.assertIn("web_search", prepared.unavailable)
        provider.status = AvailabilityStatus(Availability.AVAILABLE)
        self.assertEqual((await registry.prepare(context)).context_owners, {"web_search": "research"})
        provider.status = None
        with self.assertRaises(TypeError):
            await registry.prepare(context)
        provider.instruction = None
        with self.assertRaises(CapabilityConfigurationError):
            await registry.prepare(context)

    async def test_context_ownership_and_instruction_parts(self):
        class Contributing(Provider):
            def context_instructions(self, context):
                return "plugin instructions"

        provider = Contributing((NativeToolSpec("web_search"),))
        prepared = await ToolRegistry((provider,), owners={id(provider): "research"}).prepare(
            CapabilityContext(None, SandboxKey("guild", "1")))
        self.assertEqual(prepared.context_owners, {"web_search": "research"})
        self.assertEqual(prepared.instruction_parts, (("research", "plugin instructions"),))

    def setUp(self):
        self.context = CapabilityContext(event(), SandboxKey("guild", "1"))
        self.schema = object_schema({
            "text": {"type": "string", "minLength": 1, "maxLength": 5},
            "count": {"type": "integer", "minimum": 1, "maximum": 2},
            "ratio": {"type": "number", "minimum": 0, "maximum": 1},
            "enabled": {"type": "boolean"},
            "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 2},
            "nested": object_schema(),
            "choice": {"type": "string", "enum": ["a"]},
        }, ["text"])

    async def test_prepare_and_execute(self):
        provider = Provider((FunctionToolSpec("echo", "Echo", self.schema, side_effect=True), NativeToolSpec("web_search")))
        prepared = await ToolRegistry((provider,)).prepare(self.context)
        self.assertEqual([item.name if isinstance(item, FunctionToolSpec) else item.kind for item in prepared.specs], ["echo", "web_search"])
        result = await prepared.execute("echo", '{"text":"にゃ"}')
        self.assertEqual(result.status, "success")
        name, arguments, context, invocation_id = provider.calls[0]
        self.assertEqual((name, arguments, context), ("echo", {"text": "にゃ"}, self.context))
        self.assertEqual(len(invocation_id), 64)
        again = await prepared.execute("echo", '{ "text" : "にゃ" }')
        self.assertEqual(provider.calls[1][3], invocation_id)
        self.assertEqual(again.status, "success")
        event_value = NativeToolEvent("web_search", "call-1", "completed", {"query": "猫"})
        self.assertEqual(await prepared.consume_native((event_value,)), (provider.result,))
        self.assertEqual(provider.native_call, ((event_value,), self.context))

    async def test_native_event_validation_and_rejections(self):
        for make in (
            lambda: NativeToolEvent("Bad", "c", "completed"),
            lambda: NativeToolEvent("web_search", "", "completed"),
            lambda: NativeToolEvent("web_search", "c\nx", "completed"),
            lambda: NativeToolEvent("web_search", "c", "unknown"),
            lambda: NativeToolEvent("web_search", "c", "completed", {"x": Path("x")}),
        ):
            with self.subTest(make=make), self.assertRaises(ValueError):
                make()
        prepared = await ToolRegistry((Provider((NativeToolSpec("web_search"),)),)).prepare(self.context)
        with self.assertRaisesRegex(ValueError, "unregistered"):
            await prepared.consume_native((NativeToolEvent("image_generation", "c", "completed"),))
        duplicate = NativeToolEvent("web_search", "c", "completed")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            await prepared.consume_native((duplicate, duplicate))
        with self.assertRaisesRegex(ValueError, "too many"):
            await prepared.consume_native(tuple(
                NativeToolEvent("web_search", f"c{i}", "completed")
                for i in range(17)
            ))
        provider = Provider((NativeToolSpec("web_search"),), result="wrong")
        prepared = await ToolRegistry((provider,)).prepare(self.context)
        with self.assertRaisesRegex(TypeError, "must return ToolResult"):
            await prepared.consume_native((duplicate,))

    async def test_rejections_do_not_call_provider(self):
        provider = Provider((FunctionToolSpec("echo", "Echo", self.schema),))
        prepared = await ToolRegistry((provider,)).prepare(self.context)
        cases = [
            ("missing", "{}", "unknown_tool"), ("echo", "{", "invalid_arguments"),
            ("echo", "[]", "invalid_arguments"), ("echo", "{}", "invalid_arguments"),
            ("echo", '{"text":"toolong"}', "invalid_arguments"),
            ("echo", '{"text":"x","extra":1}', "invalid_arguments"),
            ("echo", "x" * (MAX_ARGUMENT_BYTES + 1), "arguments_too_large"),
        ]
        for name, raw, error in cases:
            with self.subTest(name=name, error=error):
                result = await prepared.execute(name, raw)
                self.assertEqual(result.model_payload["error"], error)
        self.assertEqual(provider.calls, [])

    async def test_schema_runtime_branches(self):
        provider = Provider((FunctionToolSpec("typed", "Typed", self.schema),))
        prepared = await ToolRegistry((provider,)).prepare(self.context)
        valid = '{"text":"x","count":2,"ratio":0.5,"enabled":true,"tags":["a"]}'
        self.assertEqual((await prepared.execute("typed", valid)).status, "success")
        invalid_values = [
            {"text": 1}, {"text": "x", "count": True}, {"text": "x", "count": 3},
            {"text": "x", "ratio": True}, {"text": "x", "ratio": -1},
            {"text": "x", "enabled": 1}, {"text": "x", "tags": "a"},
            {"text": "x", "tags": ["a", "b", "c"]}, {"text": "x", "tags": [1]},
            {"text": "x", "nested": []}, {"text": "x", "choice": "b"},
        ]
        for value in invalid_values:
            with self.subTest(value=value):
                self.assertEqual((await prepared.execute("typed", json.dumps(value))).status, "rejected")

    async def test_source_requirement_and_provider_contract(self):
        no_source = CapabilityContext(None, SandboxKey("guild", "1"))
        provider = Provider((FunctionToolSpec("read", "Read", object_schema()), FunctionToolSpec("write", "Write", object_schema(), side_effect=True, requires_source=False)))
        prepared = await ToolRegistry((provider,)).prepare(no_source)
        self.assertEqual((await prepared.execute("read", "{}")).model_payload["error"], "missing_source")
        self.assertEqual((await prepared.execute("write", "{}")).model_payload["error"], "missing_source")
        provider.result = "wrong"
        sourced = await ToolRegistry((provider,)).prepare(self.context)
        with self.assertRaises(TypeError):
            await sourced.execute("read", "{}")

    async def test_registry_definition_failures(self):
        spec = FunctionToolSpec("same", "Same", object_schema())
        cases = [
            (Provider([spec]), "tuple"),
            (Provider((object(),)), "invalid tool spec"),
            (Provider((spec, spec)), "duplicate tool name"),
            (Provider((NativeToolSpec("web_search"), NativeToolSpec("web_search"))), "duplicate native"),
            (Provider(tuple(FunctionToolSpec(f"t{i}", "x", object_schema()) for i in range(MAX_TOOL_COUNT + 1))), "too many tools"),
        ]
        for provider, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(CapabilityConfigurationError, message):
                await ToolRegistry((provider,)).prepare(self.context)
        with self.assertRaises(TypeError):
            ToolRegistry((object(),))
        class NoConsumer:
            async def tools(self, context):
                return (NativeToolSpec("web_search"),)
            async def execute_tool(self, name, arguments, context, invocation_id):
                return ToolResult("success", {})
        no_consumer = NoConsumer()
        with self.assertRaisesRegex(CapabilityConfigurationError, "no consumer"):
            await ToolRegistry((no_consumer,)).prepare(self.context)

    async def test_total_schema_limit_and_individual_schema_limit(self):
        huge_enum = ["x" * 100 for _ in range(400)]
        with self.assertRaisesRegex(CapabilityConfigurationError, "schema is too large"):
            FunctionToolSpec("huge", "x", object_schema({"x": {"type": "string", "enum": huge_enum}}))
        medium_enum = [f"{i:04d}" + "x" * 90 for i in range(300)]
        providers = tuple(Provider((FunctionToolSpec(f"tool{i}", "x", object_schema({"x": {"type": "string", "enum": medium_enum}})),)) for i in range(9))
        with self.assertRaisesRegex(CapabilityConfigurationError, "total tool schema"):
            await ToolRegistry(providers).prepare(self.context)

    async def test_empty_registry(self):
        prepared = await ToolRegistry().prepare(self.context)
        self.assertEqual(prepared.specs, ())
