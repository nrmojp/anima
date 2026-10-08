from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from anima.core.skills import (MAX_BYTES, SkillLoader, SkillRegistry, SkillResourceProvider,
                               SkillSource, parse_skill, read_text)
from anima.core.resources import (ResourceCollectionSpec, ResourceRegistration,
                                  ResourceRegistry, ResourceToolProvider)
from anima.capabilities.contracts import CapabilityContext, PermissionSet, ToolRegistry
from anima.core.sandbox import SandboxKey


def make_skill(root, name="sample", description="Use for a sample task.", metadata=""):
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n{metadata}---\n"
        "Secret detailed workflow. Read references/guide.md when needed.\n", encoding="utf-8")
    return directory


class SkillTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = SkillSource("core", self.root)
        self.directory = make_skill(self.root)
        self.skill = parse_skill(self.source, self.directory)
        self.context = CapabilityContext(None, SandboxKey("guild", "1"))
        self.internal = replace(self.context, permissions=PermissionSet(frozenset({"self_time"})))

    def test_metadata_validation(self):
        self.assertEqual(self.skill.id, "core:sample")
        with self.assertRaises(ValueError):
            SkillSource("../bad", self.root)
        valid = make_skill(self.root, "extended", description='"Describe: this task"',
            metadata='metadata:\n  anima.requires: "drawing web_search"\n  anima.contexts: "conversation self_time"\n')
        parsed = parse_skill(self.source, valid)
        self.assertEqual(parsed.requires, {"drawing", "web_search"})
        samples = ("", "---\nname: sample\n", "---\n[]\n---\nbody", "---\nname: []\n---\nbody",
                   "---\nname: other\ndescription: x\n---\nbody",
                   "---\nname: sample\ndescription: []\n---\nbody",
                   "---\nname: sample\ndescription: ''\n---\nbody",
                   "---\nname: sample\ndescription: x\n---\n",
                   "---\nname: sample\ndescription: x\nmetadata: []\n---\nbody",
                   "---\nname: sample\ndescription: x\nmetadata: {test: 1}\n---\nbody",
                   "---\nname: sample\ndescription: x\nmetadata: {anima.requires: '../bad'}\n---\nbody",
                   "---\nname: sample\ndescription: x\nmetadata: {anima.contexts: shell}\n---\nbody",
                   "---\nname: sample\ndescription: x\nmetadata: {anima.contexts: ''}\n---\nbody",
                   "---\nname: sample\ndescription: &alias x\nlicense: *alias\n---\nbody",
                   "---\nname: sample\ndescription: x\nextra: " + "[" * 33 + "0" + "]" * 33 + "\n---\nbody",
                   "---\nname: sample\ndescription: x\nextra: [" + ",".join("0" for _ in range(4096)) + "]\n---\nbody",
                   "---\nname: sample\ndescription: " + "x" * 1025 + "\n---\nbody")
        for text in samples:
            with self.subTest(text=text):
                (self.directory / "SKILL.md").write_text(text)
                with self.assertRaises(ValueError):
                    parse_skill(self.source, self.directory)

    def test_safe_bounded_reads(self):
        refs = self.directory / "references"
        refs.mkdir()
        (refs / "guide.md").write_text("guide")
        self.assertEqual(read_text(self.directory, "references/guide.md"), "guide")
        for path in ("", "/absolute", "../escape", "references/../SKILL.md", "./SKILL.md", "a\\b"):
            with self.subTest(path=path), self.assertRaises(PermissionError):
                read_text(self.directory, path)
        with self.assertRaises(FileNotFoundError):
            read_text(self.directory, "missing.md")
        (refs / "link.md").symlink_to(self.directory / "SKILL.md")
        with self.assertRaises(PermissionError):
            read_text(self.directory, "references/link.md")
        link = self.root / "linked"
        link.symlink_to(self.directory, target_is_directory=True)
        with self.assertRaises(PermissionError):
            read_text(link, "SKILL.md")
        (refs / "big.md").write_bytes(b"x" * MAX_BYTES)
        self.assertEqual(len(read_text(self.directory, "references/big.md")), MAX_BYTES)
        (refs / "big.md").write_bytes(b"x" * (MAX_BYTES + 1))
        with self.assertRaises(ValueError):
            read_text(self.directory, "references/big.md")
        (refs / "binary.md").write_bytes(b"\xff")
        with self.assertRaises(UnicodeError):
            read_text(self.directory, "references/binary.md")

    def test_discovery_limits_diagnostics_and_collisions(self):
        loader = SkillLoader()
        (self.root / "README.md").write_text("ignored")
        (self.root / "broken").mkdir()
        (self.root / "symlink").symlink_to(self.directory, target_is_directory=True)
        skills, errors = loader.load((self.source, SkillSource("plugin", self.root)))
        self.assertEqual([s.id for s in skills], ["core:sample", "plugin:sample"])
        self.assertEqual(len(errors), 4)
        duplicate, errors = loader.load((self.source, self.source))
        self.assertEqual(len(duplicate), 1)
        self.assertTrue(errors)
        with patch("anima.core.skills.MAX_SKILLS", 0):
            self.assertFalse(loader.load((self.source,))[0])
        link = self.root / "root-link"
        link.symlink_to(self.directory, target_is_directory=True)
        loaded, errors = loader.load((SkillSource("core", link), SkillSource("core", self.root / "README.md"),
                                     SkillSource("core", self.root / "absent")))
        self.assertFalse(loaded)
        self.assertEqual(len(errors), 2)
        (self.root / "broken" / "SKILL.md").write_text("---\n[bad\n---\nbody")
        self.assertTrue(loader.load((self.source,))[1])
        with patch.object(Path, "iterdir", side_effect=PermissionError("private")):
            self.assertEqual(loader.load((self.source,))[1][0]["error"], "unreadable_skill_root")
        with patch.object(Path, "iterdir", return_value=iter([self.root / "README.md"] * 257)):
            self.assertEqual(loader.load((self.source,))[1][0]["error"], "skill_scan_limit")

    async def test_context_and_requirement_filtering(self):
        both = replace(self.skill, contexts=frozenset({"conversation", "self_time"}))
        needs = replace(self.skill, id="drawing:sample", requires=frozenset({"drawing"}))
        registry = SkillRegistry((both, needs), frozenset())
        self.assertEqual(registry.available(self.context), (both,))
        self.assertEqual(registry.available(self.internal), (both,))
        with self.assertRaises(LookupError):
            registry.get(needs.id, self.context)
        with self.assertRaises(ValueError):
            SkillRegistry((both, both), frozenset())
        provider = SkillResourceProvider(SkillRegistry((self.skill,), frozenset()))
        catalog = provider.context_instructions(self.context)
        self.assertIn("core:sample", catalog)
        self.assertNotIn("Secret detailed workflow", catalog)
        self.assertEqual(provider.context_instructions(self.internal), "")
        self.assertEqual(await provider.tools(self.context), ())
        with self.assertRaises(LookupError):
            await provider.execute_tool("x", {}, self.context, "call")
        prepared = await ToolRegistry((provider,)).prepare(self.context)
        self.assertFalse(prepared.specs)
        self.assertIn("core:sample", prepared.contextual_instructions)

    async def test_pages_read_references_and_status(self):
        registry = SkillRegistry((self.skill, replace(self.skill, id="other:sample")), frozenset())
        status = self.root / "runtime" / "skills.json"
        provider = SkillResourceProvider(registry, status_path=status)
        page = await provider.list_resources("core.skills", cursor=None, limit=1, context=self.context)
        self.assertEqual(page.next_cursor, "1")
        self.assertIsNone((await provider.list_resources("core.skills", cursor="1", limit=1, context=self.context)).next_cursor)
        for cursor, limit in (("-1", 1), (None, 0), ("bad", 1)):
            with self.assertRaises(ValueError):
                await provider.list_resources("core.skills", cursor=cursor, limit=limit, context=self.context)
        refs = self.directory / "references"
        refs.mkdir()
        (refs / "guide.md").write_text("guide")
        with patch("anima.core.skills.emit") as emit:
            for _ in range(51):
                read = await provider.read_resource("core.skills", self.skill.id, self.context)
            self.assertIn("Secret detailed workflow", read["text"])
            self.assertTrue(read["instructions_only"])
            self.assertEqual(len(provider.reads), 50)
            emit.assert_called()
        self.assertEqual((await provider.read_resource("core.skills", "core:sample/references/guide.md", self.context))["text"], "guide")
        internal_dir = make_skill(self.root, "internal", metadata='metadata:\n  anima.contexts: "self_time"\n')
        internal_skill = parse_skill(self.source, internal_dir)
        internal_provider = SkillResourceProvider(SkillRegistry((internal_skill,), frozenset()))
        await internal_provider.read_resource("core.skills", internal_skill.id, self.internal)
        self.assertEqual(internal_provider.reads[0]["context"], "self_time")
        data = json.loads(status.read_text())
        self.assertNotIn("Secret detailed workflow", status.read_text())
        self.assertEqual(len(data["reads"]), 50)
        for path in ("scripts/run.py", "SKILL.md", "references/file.png", "references/a/b/c/d.md", "references/../../SKILL.md"):
            with self.subTest(path=path), self.assertRaises(PermissionError):
                await provider.read_resource("core.skills", f"core:sample/{path}", self.context)
        (self.directory / "SKILL.md").write_text("---\nname: sample\ndescription: changed\n---\nbody")
        with self.assertRaisesRegex(ValueError, "reload_required"):
            await provider.read_resource("core.skills", self.skill.id, self.context)
        provider = SkillResourceProvider(SkillRegistry((self.skill,), frozenset()))
        with patch.object(Path, "is_symlink", return_value=True), self.assertRaises(PermissionError):
            await provider.read_resource("core.skills", self.skill.id, self.context)

    async def test_primitive_resource_access_does_not_add_tools_or_write_access(self):
        from anima.adapters.stub.discord import StubDiscordSender
        from anima.bootstrap.resource_providers import InventoryResourceProvider
        from anima.core.inventory import InventoryStore
        from anima.core.agentic_loop import AgenticLoop, AgentToolCall, ThoughtStep, ToolObservation
        from test_resources import event
        context = replace(self.context, source=event())
        store = InventoryStore(self.root / "state", context.sandbox_key)
        provider = SkillResourceProvider(SkillRegistry((self.skill,), frozenset()))
        registration = ResourceRegistration(ResourceCollectionSpec("core.skills", "core", "skills", "process", frozenset({"list", "read"})), provider)
        inventory = ResourceRegistration(ResourceCollectionSpec("core.inventory", "core", "inventory", "sandbox", frozenset({"write"}), writable_content="text"), InventoryResourceProvider(store))
        resources = ResourceRegistry((registration, inventory))
        prepared = await ToolRegistry((ResourceToolProvider(resources), provider)).prepare(context)
        with patch("anima.core.plugin_logs.plugin_log") as detail_log:
            read = await prepared.execute("resource_read", json.dumps({"collection": "core.skills", "resource_id": self.skill.id, "attach_to_reply": False}))
            self.assertNotIn("Secret detailed workflow", str(detail_log.call_args_list))
        self.assertEqual(read.status, "success")
        self.assertIn("Secret detailed workflow", str(read.model_payload))
        self.assertNotIn("activate_skill", {spec.name for spec in prepared.specs})
        with self.assertRaises(PermissionError):
            await resources.execute("write", {"collection": "core.skills"}, self.context)
        with self.assertRaises(LookupError):
            await provider.read_resource("core.skills", self.skill.id, self.internal)
        class Backend:
            async def think(self, request_count, tools_enabled, observations):
                if request_count == 1:
                    return ThoughtStep((AgentToolCall("read", "resource_read", json.dumps({"collection": "core.skills", "resource_id": "core:sample", "attach_to_reply": False})),))
                if request_count == 2:
                    assert "Secret detailed workflow" in observations[0].output
                    return ThoughtStep((AgentToolCall("write", "resource_write", json.dumps({"collection": "core.inventory", "resource_id": "result.md", "mode": "replace", "content": "completed work"})),))
                assert json.loads(observations[0].output)["ok"]
                return ThoughtStep(result="Saved the result.")
        class Executor:
            async def execute(self, call):
                result = await prepared.execute(call.name, call.arguments)
                return ToolObservation(call, json.dumps(result.model_payload))
        completed = await AgenticLoop(max_tool_rounds=3).run(backend=Backend(), executor=Executor())
        sender = StubDiscordSender(clock=lambda: context.source.ts)
        await sender.send(context.source, completed.result)
        self.assertEqual(completed.tool_call_count, 2)
        self.assertEqual(sender.deliveries[0].text, "Saved the result.")
        self.assertEqual(store.read_text("result.md"), "completed work")

    async def test_instruction_body_is_offloaded_between_response_runs(self):
        from types import SimpleNamespace
        from anima.adapters.openai.client import OpenAIResponder
        from anima.core.models import Context
        from test_adapters import FakeFunctionCall
        from test_resources import event
        provider = SkillResourceProvider(SkillRegistry((self.skill,), frozenset()))
        resources = ResourceRegistry((ResourceRegistration(ResourceCollectionSpec(
            "core.skills", "core", "skills", "process", frozenset({"list", "read"})), provider),))
        payloads = []
        async def create(**payload):
            payloads.append(payload)
            if len(payloads) == 1:
                return SimpleNamespace(id="read", output_text="", usage=None, output=[FakeFunctionCall(
                    type="function_call", name="resource_read", call_id="c1", arguments=json.dumps({
                        "collection": "core.skills", "resource_id": self.skill.id, "attach_to_reply": False}))])
            return SimpleNamespace(id="done", output=[], usage=None, output_text=json.dumps({
                "reply": "Completed.", "mood": {"state": "calm", "cause": "done", "strength": "ふつう", "focus": "task"}}))
        responder = OpenAIResponder(api_key="unused", model="test-model", client=SimpleNamespace(
            responses=SimpleNamespace(create=create)), tool_registry=ToolRegistry((ResourceToolProvider(resources), provider)))
        context = Context("Persona and rules", (), 1, source_event=event())
        first = await responder.respond(context)
        self.assertIn("Secret detailed workflow", json.dumps(payloads[1]))
        self.assertNotIn("Secret detailed workflow", str(first.references))
        await responder.respond(context)
        self.assertNotIn("Secret detailed workflow", json.dumps(payloads[2]))
        self.assertEqual(len(provider.reads), 1)
        self.assertFalse(payloads[2]["store"])
