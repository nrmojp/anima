from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from anima.bootstrap.resource_providers import (
    AttachmentResourceProvider,
    InventoryResourceProvider,
    MemoryResourceProvider,
    OpenItemsResourceProvider,
    ResourceProviderBase,
)
from anima.capabilities.contracts import (
    CapabilityConfigurationError, CapabilityContext, PermissionSet, ToolRegistry,
)
from anima.core.inventory import InventoryStore
from anima.core.memory import LocalMemoryRetriever
from anima.core.models import Attachment, Event
from anima.core.resources import (
    ResourceCollectionSpec,
    ResourceItem,
    ResourcePage,
    ResourceRegistration,
    ResourceRead,
    ResourceRegistry,
    ResourceToolProvider,
    ResourceTransfer,
)
from anima.core.sandbox import SandboxKey


KEY = SandboxKey("guild", "1")


def event(*, attachments=()):
    return Event(
        id="message-1", ts=datetime.now(timezone.utc), kind="message",
        channel_id="10", channel_name="general", author_id="20", author_name="user",
        text="hello", guild_id="1", sandbox_key=str(KEY), attachments=attachments,
    )


class CompleteProvider(ResourceProviderBase):
    def __init__(self):
        self.deleted = []
        self.imported = []

    async def list_resources(self, collection, *, cursor, limit, context):
        return ResourcePage((ResourceItem("one", "One"),), "next")

    async def search_resources(self, collection, *, query, limit, context):
        return ResourcePage((ResourceItem("found", query),))

    async def read_resource(self, collection, resource_id, context):
        if resource_id == "media":
            return ResourceRead({"resource_id": resource_id, "attached": True}, (Path(__file__),))
        return {"resource_id": resource_id, "content": "text"}

    async def write_resource(self, collection, resource_id, mode, content, context):
        return ResourceItem(resource_id, resource_id, summary=content)

    async def delete_resource(self, collection, resource_id, context):
        self.deleted.append(resource_id)

    async def export_resource(self, collection, resource_id, context):
        return ResourceTransfer(resource_id, "text/plain", 1, text="x")

    async def import_resource(self, collection, resource_id, transfer, context):
        self.imported.append((resource_id, transfer.text))
        return ResourceItem(resource_id, resource_id)



def spec(identifier="test.items", operations=frozenset({"list"}), **kwargs):
    return ResourceCollectionSpec(
        identifier, identifier.split(".", 1)[0], "test items", "sandbox", operations,
        **kwargs,
    )


class ResourceValueTests(unittest.TestCase):
    def test_specs_validate_contract(self):
        valid = spec(operations=frozenset({"write"}), writable_content="text")
        self.assertEqual(valid.scope, "sandbox")
        failures = (
            lambda: spec("invalid", frozenset({"list"})),
            lambda: ResourceCollectionSpec("core.items", "test", "x", "sandbox", frozenset({"list"})),
            lambda: spec(operations=frozenset()),
            lambda: spec(operations=frozenset({"unknown"})),
            lambda: spec(operations=frozenset({"write"})),
            lambda: spec(operations=frozenset({"list"}), transfer_policy="copy"),
            lambda: spec(max_list_results=0),
            lambda: ResourceCollectionSpec("other.items", "test", "x", "sandbox", frozenset({"list"})),
            lambda: spec(operations=frozenset({"list"}), writable_content="binary"),
            lambda: spec(operations=frozenset({"export"}), transfer_policy="invalid"),
            lambda: ResourceCollectionSpec("test.items", "test", "", "sandbox", frozenset({"list"})),
            lambda: ResourceCollectionSpec("test.items", "test", "x", "invalid", frozenset({"list"})),
        )
        for make in failures:
            with self.subTest(make=make), self.assertRaises(CapabilityConfigurationError):
                make()

    def test_items_pages_and_transfers_validate(self):
        now = datetime.now(timezone.utc)
        item = ResourceItem("id", "title", "summary", "text/plain", 1, now, {"x": 1})
        self.assertEqual(item.to_dict()["updated_at"], now.isoformat())
        self.assertEqual(ResourcePage((item,), "c").to_dict()["next_cursor"], "c")
        for args in (("", "x"), ("x", "")):
            with self.subTest(args=args), self.assertRaises(ValueError):
                ResourceItem(args[0], args[1])
        with self.assertRaises(ValueError):
            ResourceItem("x", "x", size=-1)
        with self.assertRaises(ValueError):
            ResourceItem("x", "x", summary="x" * 2001)
        transfer = ResourceTransfer("x", "text/plain", 1, text="x")
        self.assertEqual(transfer.text, "x")
        with self.assertRaises(ValueError):
            ResourceTransfer("x", "text/plain", 0, text="")
        with self.assertRaises(ValueError):
            ResourceTransfer("x", "text/plain", 1, path=Path(__file__), text="x")
        with self.assertRaises(ValueError):
            ResourceTransfer("x", "text/plain", 1, path=Path("missing"))


class ResourceRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_image_read_is_observation_unless_delivery_is_requested(self):
        tools = ResourceToolProvider(self.registry)
        for requested in (None, False, True):
            arguments = {"collection": "test.items", "resource_id": "media"}
            if requested is not None:
                arguments["attach_to_reply"] = requested
            result = await tools.execute_tool("resource_read", arguments, self.context, "read")
            self.assertEqual(result.attachments, (Path(__file__),))
            self.assertEqual(result.model_payload["attach_to_reply"], requested is True)

    async def asyncSetUp(self):
        self.provider = CompleteProvider()
        operations = frozenset({
            "list", "search", "read", "write", "delete", "export", "import",
        })
        self.registry = ResourceRegistry((ResourceRegistration(spec(
            operations=operations, writable_content="text", transfer_policy="consume"
        ), self.provider),))
        self.context = CapabilityContext(event(), KEY)

    async def test_all_operations_and_transfer(self):
        cases = (
            ("list", {"collection": "test.items", "cursor": "", "limit": 2}),
            ("search", {"collection": "test.items", "query": "needle", "limit": 2}),
            ("read", {"collection": "test.items", "resource_id": "one"}),
            ("write", {"collection": "test.items", "resource_id": "note.md", "mode": "replace", "content": "x"}),
            ("delete", {"collection": "test.items", "resource_id": "one"}),
        )
        for operation, arguments in cases:
            payload, attachments = await self.registry.execute(operation, arguments, self.context)
            self.assertTrue(payload["ok"])
            self.assertFalse(attachments)
        media, attachments = await self.registry.execute("read", {
            "collection": "test.items", "resource_id": "media",
        }, self.context)
        self.assertTrue(media["attached"])
        self.assertEqual(attachments, (Path(__file__),))
        value = await self.registry.transfer({
            "source_collection": "test.items", "source_id": "one",
            "destination_collection": "test.items", "destination_id": "copy",
        }, self.context)
        self.assertTrue(value["ok"])
        self.assertEqual(self.provider.imported, [("copy", "x")])
        self.assertIn("one", self.provider.deleted)

    async def test_registry_and_tool_rejections(self):
        self.assertEqual(ResourceRegistry().catalog(), "")
        self.assertEqual(ResourceRegistry().snapshot(), ())
        with self.assertRaises(TypeError):
            ResourceRegistry((ResourceRegistration(spec(), object()),))
        with self.assertRaises(CapabilityConfigurationError):
            ResourceRegistry((
                ResourceRegistration(spec(), self.provider),
                ResourceRegistration(spec(), self.provider),
            ))
        with self.assertRaises(LookupError):
            await self.registry.execute("read", {
                "collection": "test.missing", "resource_id": "x",
            }, self.context)
        restricted = ResourceRegistry((ResourceRegistration(spec(), self.provider),))
        with self.assertRaises(PermissionError):
            await restricted.execute("read", {
                "collection": "test.items", "resource_id": "x",
            }, self.context)
        tools = ResourceToolProvider(restricted)
        prepared = await ToolRegistry((tools,)).prepare(self.context)
        self.assertEqual(len(prepared.specs), 6)
        self.assertIn("test.items", prepared.contextual_instructions)
        self.assertIn("括弧内の操作だけ実行できる", prepared.contextual_instructions)
        self.assertEqual("".join(text for _, text in prepared.instruction_parts), prepared.contextual_instructions)
        self.assertTrue(any(owner == "test" for owner, _ in prepared.instruction_parts))
        memory_catalog = ResourceRegistry((ResourceRegistration(ResourceCollectionSpec(
            "core.memory", "core", "長期記憶", "sandbox", frozenset({"search", "read"}),
        ), self.provider),)).catalog()
        self.assertIn("core.memoryは読み取り専用", memory_catalog)
        inventory_catalog = ResourceRegistry((
            ResourceRegistration(ResourceCollectionSpec(
                "core.inventory", "core", "持ち物", "sandbox",
                frozenset({"read", "write"}), writable_content="text",
            ), self.provider),
            ResourceRegistration(ResourceCollectionSpec(
                "music.tracks", "music", "楽曲", "process",
                frozenset({"list", "read"}),
            ), self.provider),
        )).catalog()
        self.assertIn("文章・歌詞などを手元に保存", inventory_catalog)
        self.assertIn("core.inventory", inventory_catalog)
        self.assertIn("writeを持たないCollectionは参照専用", inventory_catalog)
        with patch("anima.core.resources.emit") as emit_event:
            rejected = await tools.execute_tool("resource_read", {
                "collection": "test.items", "resource_id": "x",
            }, self.context, "i")
        emit_event.assert_called_once_with(
            "resource.failed", collection="test.items", resource_operation="read",
            error_type="PermissionError", event_id="message-1", channel_id="10",
        )
        self.assertEqual(rejected.status, "rejected")
        unknown = await tools.execute_tool("other", {}, self.context, "i")
        self.assertEqual(unknown.model_payload["error"], "unknown_tool")
        source = event()
        autonomous_source = Event(
            id="selftime-1", ts=source.ts, kind="channel", channel_id="1",
            channel_name="self-time", author_id="self", author_name="self", text="",
            guild_id="1", sandbox_key=str(KEY), author_is_bot=True,
        )
        autonomous = CapabilityContext(
            autonomous_source, KEY,
            PermissionSet(frozenset({"self_time", "resource.delete_temporary"})),
        )
        protected = await ResourceToolProvider(self.registry).execute_tool(
            "resource_delete", {"collection": "test.items", "resource_id": "one"},
            autonomous, "i",
        )
        self.assertEqual(protected.model_payload["error"], "self_time_delete_not_allowed")
        success = await ResourceToolProvider(self.registry).execute_tool(
            "resource_transfer", {
                "source_collection": "test.items", "source_id": "one",
                "destination_collection": "test.items", "destination_id": "copy-two",
            }, self.context, "i",
        )
        self.assertEqual(success.status, "success")
        listed = await ResourceToolProvider(self.registry).execute_tool(
            "resource_list", {"collection": "test.items", "cursor": "", "limit": 1},
            self.context, "i",
        )
        self.assertEqual(listed.status, "success")
        with self.assertRaisesRegex(ValueError, "transfer"):
            await self.registry.execute("export", {"collection": "test.items"}, self.context)
        consuming = ResourceRegistry((ResourceRegistration(spec(
            operations=frozenset({"export", "import"}), transfer_policy="consume"
        ), self.provider),))
        with self.assertRaises(CapabilityConfigurationError):
            await consuming.transfer({
                "source_collection": "test.items", "source_id": "one",
                "destination_collection": "test.items", "destination_id": "copy",
            }, self.context)


class ConcreteResourceProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = InventoryStore(self.root, KEY)
        self.provider = InventoryResourceProvider(self.store)
        self.context = CapabilityContext(event(), KEY)

    async def test_open_items_are_append_only_and_sandbox_local(self):
        provider = OpenItemsResourceProvider(self.root)
        page = await provider.list_resources(
            "core.open_items", cursor=None, limit=1, context=self.context,
        )
        self.assertEqual(page.items[0].id, "open.md")
        item = await provider.write_resource(
            "core.open_items", "open.md", "append", "  猫の絵を  見せる  ",
            self.context,
        )
        self.assertGreater(item.size, 0)
        value = await provider.read_resource(
            "core.open_items", "open.md", self.context,
        )
        self.assertIn("#self-time] 猫の絵を 見せる", value["content"])
        with self.assertRaises(PermissionError):
            await provider.write_resource(
                "core.open_items", "open.md", "replace", "消す", self.context,
            )
        with self.assertRaises(ValueError):
            await provider.write_resource(
                "core.open_items", "open.md", "append", "", self.context,
            )
        with self.assertRaises(FileNotFoundError):
            await provider.read_resource(
                "core.open_items", "other.md", self.context,
            )

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_self_time_reconciles_open_items_and_protects_jobs(self):
        provider = OpenItemsResourceProvider(self.root)
        context = CapabilityContext(event(), KEY, PermissionSet(frozenset({"self_time"})))
        for resource_id, mode in (("other.md", "append"), ("open.md", "create")):
            with self.assertRaises(PermissionError):
                await provider.write_resource("core.open_items", resource_id, mode, "予定", context)
        job = "- [2026-09-20 #一般] 絵を描く (job:j-123456789abc)"
        todo = "- [2026-09-20 #一般] 絵を眺める"
        self.root.joinpath("open.md").write_text(job + "\n" + todo + "\n")
        with self.assertRaises(PermissionError):
            await provider.write_resource("core.open_items", "open.md", "replace", todo, context)
        with self.assertRaises(ValueError):
            await provider.write_resource("core.open_items", "open.md", "replace", "不正", context)
        with self.assertRaises(ValueError):
            await provider.write_resource("core.open_items", "open.md", "replace", "x" * 20001, context)
        await provider.write_resource("core.open_items", "open.md", "replace", job, context)
        self.assertEqual(provider.path.read_text(), job + "\n")
        provider.path.write_text(todo)
        await provider.write_resource("core.open_items", "open.md", "replace", "", context)
        self.assertEqual(provider.path.read_text(), "")

    async def test_inventory_temporary_transfer_and_multimodal_read(self):
        self.store.stage_bytes("drawing.png", b"png")
        temporary = await self.provider.list_resources(
            "core.temporary_artifacts", cursor=None, limit=5, context=self.context
        )
        self.assertEqual(temporary.items[0].id, "drawing.png")
        transfer = await self.provider.export_resource(
            "core.temporary_artifacts", "drawing.png", self.context
        )
        imported = await self.provider.import_resource(
            "core.inventory", "orange-cat.png", transfer, self.context
        )
        self.assertEqual(imported.id, "orange-cat.png")
        await self.provider.delete_resource(
            "core.temporary_artifacts", "drawing.png", self.context
        )
        image_read = await self.provider.read_resource(
            "core.inventory", "orange-cat.png", self.context
        )
        self.assertIsInstance(image_read, ResourceRead)
        self.assertEqual(image_read.attachments[0].name, "orange-cat.png")
        await self.provider.write_resource(
            "core.inventory", "note.md", "replace", "one", self.context
        )
        await self.provider.write_resource(
            "core.inventory", "note.md", "append", " two", self.context
        )
        read = await self.provider.read_resource(
            "core.inventory", "note.md", self.context
        )
        self.assertEqual(read["content"], "one two")
        (self.store.inventory_root / "large.md").write_bytes(b"x" * (256 * 1024 + 1))
        with self.assertRaisesRegex(ValueError, "too large"):
            await self.provider.read_resource("core.inventory", "large.md", self.context)
        self.assertEqual(self.provider.generated_id(self.context), "orange-cat.png")
        self.store.stage_bytes("second.png", b"png")
        self.provider.register_generated("message-1", "second.png")
        self.assertEqual(self.provider.generated_id(self.context), "second.png")
        await self.provider.delete_resource(
            "core.temporary_artifacts", "second.png", self.context
        )
        with self.assertRaises(FileNotFoundError):
            self.provider.generated_id(self.context)
        with self.assertRaises(FileNotFoundError):
            self.provider.generated_id(CapabilityContext(None, KEY))
        text = ResourceTransfer("source.md", "text/markdown", 4, text="memo")
        imported_text = await self.provider.import_resource(
            "core.inventory", "imported.md", text, self.context
        )
        self.assertEqual(imported_text.id, "imported.md")
        with self.assertRaises(ValueError):
            await self.provider.import_resource(
                "core.inventory", "bad.bin", text, self.context
            )
        with self.assertRaises(ValueError):
            await self.provider.write_resource(
                "core.inventory", "bad.bin", "replace", "x", self.context
            )

    async def test_attachment_list_read_and_export(self):
        cache = self.store.sandbox_root / "attachments"
        cache.mkdir(parents=True)
        (cache / "memo.txt").write_text("memo", encoding="utf-8")
        context = CapabilityContext(event(attachments=(
            Attachment("https://example.test/skip", "text/plain"),
            Attachment("https://example.test/memo.txt", "text/plain", "memo.txt"),
        )), KEY)
        provider = AttachmentResourceProvider(self.store)
        page = await provider.list_resources(
            "interface.current_attachments", cursor=None, limit=5, context=context
        )
        self.assertEqual(page.items[0].id, "1")
        self.assertEqual((await provider.read_resource(
            "interface.current_attachments", "1", context
        ))["content"], "memo")
        self.assertEqual((await provider.export_resource(
            "interface.current_attachments", "1", context
        )).text, None)
        (cache / "image.png").write_bytes(b"png")
        image_context = CapabilityContext(event(attachments=(Attachment(
            "https://example.test/image.png", "image/png", "image.png"
        ),)), KEY)
        image_read = await provider.read_resource(
            "interface.current_attachments", "0", image_context
        )
        self.assertIsInstance(image_read, ResourceRead)
        self.assertEqual(image_read.attachments[0].name, "image.png")
        self.assertEqual((await provider.list_resources(
            "interface.current_attachments", cursor=None, limit=5,
            context=CapabilityContext(None, KEY),
        )).items, ())
        for resource_id in ("bad", "9"):
            with self.subTest(resource_id=resource_id), self.assertRaises(FileNotFoundError):
                await provider.read_resource("interface.current_attachments", resource_id, context)
        uncached = CapabilityContext(event(attachments=(Attachment(
            "https://example.test/a", "text/plain"
        ),)), KEY)
        with self.assertRaises(FileNotFoundError):
            await provider.read_resource("interface.current_attachments", "0", uncached)
        missing = CapabilityContext(event(attachments=(Attachment(
            "https://example.test/a", "text/plain", "missing.txt"
        ),)), KEY)
        with self.assertRaises(FileNotFoundError):
            await provider.read_resource("interface.current_attachments", "0", missing)
        large = cache / "large.txt"
        large.write_bytes(b"x" * (256 * 1024 + 1))
        too_large = CapabilityContext(event(attachments=(Attachment(
            "https://example.test/large", "text/plain", "large.txt"
        ),)), KEY)
        with self.assertRaises(ValueError):
            await provider.read_resource("interface.current_attachments", "0", too_large)

    async def test_memory_search_and_read(self):
        memory = self.store.sandbox_root / "memory"
        memory.mkdir(parents=True)
        (memory / "self.md").write_text("# 好み\n- オレンジが好き\n", encoding="utf-8")
        provider = MemoryResourceProvider(
            self.store.sandbox_root, LocalMemoryRetriever(self.store.sandbox_root)
        )
        page = await provider.search_resources(
            "core.memory", query="オレンジ", limit=5, context=self.context
        )
        self.assertTrue(page.items)
        self.assertEqual(provider.retriever.search("", limit=5), ())
        with patch.object(provider.retriever, "_documents", return_value=(
            [("memory/self.md", "# X\n- orange")], True,
        )):
            self.assertTrue(provider.retriever.search("orange", limit=1))
        read = await provider.read_resource(
            "core.memory", "memory/self.md", self.context
        )
        self.assertIn("オレンジ", read["content"])
        with self.assertRaises(ValueError):
            await provider.read_resource("core.memory", "../secret.md", self.context)
        link = self.store.sandbox_root / "memory" / "link.md"
        link.symlink_to(self.root / "outside.md")
        with self.assertRaises(ValueError):
            await provider.read_resource("core.memory", "memory/link.md", self.context)

    async def test_provider_base_rejects_every_operation(self):
        provider = ResourceProviderBase()
        calls = (
            lambda: provider.list_resources("x", cursor=None, limit=1, context=self.context),
            lambda: provider.search_resources("x", query="q", limit=1, context=self.context),
            lambda: provider.read_resource("x", "id", self.context),
            lambda: provider.write_resource("x", "id", "replace", "x", self.context),
            lambda: provider.delete_resource("x", "id", self.context),
            lambda: provider.export_resource("x", "id", self.context),
            lambda: provider.import_resource(
                "x", "id", ResourceTransfer("x", "text/plain", 1, text="x"), self.context
            ),
        )
        for call in calls:
            with self.subTest(call=call), self.assertRaises(PermissionError):
                await call()
