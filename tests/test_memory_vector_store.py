from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from anima.adapters.openai.memory_vector_store import (
    MemoryVectorStore, manifest_modified_at, memory_document_count,
    memory_vector_status,
)


def client(*, uploads=None, stores=None, pages=None):
    return SimpleNamespace(
        vector_stores=SimpleNamespace(
            create=AsyncMock(side_effect=stores or [SimpleNamespace(id="vs_1")]),
            delete=AsyncMock(),
            files=SimpleNamespace(
                upload_and_poll=AsyncMock(side_effect=uploads or [
                    SimpleNamespace(id="file_1", status="completed")
                ]),
                delete=AsyncMock(),
                list=AsyncMock(side_effect=pages or []),
            ),
        ),
        files=SimpleNamespace(delete=AsyncMock()),
    )


class MemoryVectorStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_creates_store_uploads_memory_and_reuses_fingerprint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "memory" / "people").mkdir(parents=True)
            (root / "memory" / "self.md").write_text("猫が好き\n", encoding="utf-8")
            (root / "memory" / "people" / "42.md").write_text(
                "春に会った\n", encoding="utf-8"
            )
            api = client()
            memory = MemoryVectorStore(root, api, sandbox_key="guild:1")

            self.assertEqual(await memory.ensure(), "vs_1")
            self.assertEqual(await memory.ensure(), "vs_1")

            api.vector_stores.create.assert_awaited_once()
            api.vector_stores.files.upload_and_poll.assert_awaited_once()
            upload = api.vector_stores.files.upload_and_poll.await_args.kwargs
            self.assertEqual(upload["vector_store_id"], "vs_1")
            self.assertIn(b"# source: memory/people/42.md", upload["file"][1])
            self.assertIn(b"# source: memory/self.md", upload["file"][1])
            self.assertEqual(upload["chunking_strategy"]["static"], {
                "max_chunk_size_tokens": 200, "chunk_overlap_tokens": 40,
            })
            manifest = json.loads(memory.manifest_path.read_text())
            self.assertEqual(manifest["file_id"], "file_1")
            self.assertEqual(manifest["document_count"], 2)
            self.assertGreater(manifest["content_bytes"], 0)
            self.assertIn("synced_at", manifest)
            status = memory_vector_status(root, sandbox_key="guild:1")
            self.assertEqual(status["state"], "synced")
            self.assertEqual(status["document_count"], 2)
            self.assertEqual(status["indexed_bytes"], status["current_bytes"])

    async def test_changed_memory_replaces_old_file_after_new_one_is_ready(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "memory").mkdir()
            path = root / "memory" / "world.md"
            path.write_text("朝だった\n", encoding="utf-8")
            api = client(uploads=[
                SimpleNamespace(id="file_1", status="completed"),
                SimpleNamespace(id="file_2", status="completed"),
            ])
            memory = MemoryVectorStore(root, api, sandbox_key="dm:2")
            await memory.ensure()
            path.write_text("夜になった\n", encoding="utf-8")

            self.assertEqual(await memory.ensure(), "vs_1")

            api.vector_stores.files.delete.assert_awaited_once_with(
                "file_1", vector_store_id="vs_1"
            )
            api.files.delete.assert_awaited_once_with("file_1")
            self.assertEqual(json.loads(memory.manifest_path.read_text())["file_id"], "file_2")

    async def test_empty_unsafe_or_failed_memory_is_not_exposed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            api = client(uploads=[SimpleNamespace(id="bad", status="failed")])
            memory = MemoryVectorStore(root, api, sandbox_key="guild:3")
            self.assertIsNone(await memory.ensure())
            (root / "memory").mkdir()
            (root / "memory" / "empty.md").write_text("\n")
            self.assertIsNone(await memory.ensure())
            (root / "memory" / "world.md").write_text("秘密\n")
            with self.assertRaisesRegex(RuntimeError, "indexing failed"):
                await memory.ensure()
            self.assertEqual(
                json.loads(memory.manifest_path.read_text())["vector_store_id"], "vs_1"
            )
            api.vector_stores.files.delete.assert_awaited_once_with(
                "bad", vector_store_id="vs_1"
            )
            api.files.delete.assert_awaited_once_with("bad")

    def test_manifest_rejects_other_sandbox_and_invalid_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            memory = MemoryVectorStore(root, client(), sandbox_key="guild:4")
            memory.manifest_path.parent.mkdir(parents=True)
            memory.manifest_path.write_text('{"sandbox_key":"guild:5"}')
            self.assertEqual(memory._load_manifest(), {})
            memory.manifest_path.write_text("{")
            self.assertEqual(memory._load_manifest(), {})

    def test_content_ignores_links_and_non_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "memory").mkdir()
            (root / "memory" / "folder.md").mkdir()
            outside = root / "outside.md"
            outside.write_text("漏らさない")
            (root / "memory" / "linked.md").symlink_to(outside)
            memory = MemoryVectorStore(root, client(), sandbox_key="guild:6")
            self.assertEqual(memory._content(), b"")
            self.assertEqual(memory_document_count(root / "missing"), 0)
            self.assertIsNone(manifest_modified_at(root / "missing.json"))
            self.assertEqual(
                memory_vector_status(root, sandbox_key="guild:6")["state"], "empty"
            )

    def test_status_reports_not_created_stale_and_legacy_sync_time(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "memory").mkdir()
            memory_path = root / "memory" / "self.md"
            memory_path.write_text("最初の記憶\n", encoding="utf-8")
            self.assertEqual(
                memory_vector_status(root, sandbox_key="guild:7")["state"],
                "not_created",
            )
            memory = MemoryVectorStore(root, client(), sandbox_key="guild:7")
            content = memory._content()
            memory._save_manifest({
                "sandbox_key": "guild:7",
                "vector_store_id": "vs_legacy",
                "file_id": "file_legacy",
                "fingerprint": hashlib.sha256(content).hexdigest(),
            })
            synced = memory_vector_status(root, sandbox_key="guild:7")
            self.assertEqual(synced["state"], "synced")
            self.assertIsNotNone(synced["synced_at"])
            self.assertEqual(synced["indexed_bytes"], len(content))
            memory_path.write_text("変わった記憶\n", encoding="utf-8")
            stale = memory_vector_status(root, sandbox_key="guild:7")
            self.assertEqual(stale["state"], "stale")
            self.assertIsNone(stale["indexed_bytes"])

    async def test_remote_files_and_prune_only_owned_orphans(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            memory = MemoryVectorStore(root, client(), sandbox_key="guild:8")
            memory._save_manifest({
                "sandbox_key": "guild:8",
                "vector_store_id": "vs_8",
                "file_id": "current",
            })
            current = SimpleNamespace(id="current", attributes={}, status="completed")
            orphan = SimpleNamespace(
                id="orphan", status="completed", usage_bytes=123,
                attributes={"sandbox_key": "guild:8", "dataset": "memory"},
            )
            foreign = SimpleNamespace(
                id="foreign", status="failed", usage_bytes=None,
                attributes={"sandbox_key": "guild:9", "dataset": "memory"},
            )
            second = SimpleNamespace(
                data=[foreign], has_next_page=MagicMock(return_value=False)
            )
            first = SimpleNamespace(
                data=[current, orphan], has_next_page=MagicMock(return_value=True),
                get_next_page=AsyncMock(return_value=second),
            )
            api = client(pages=[first, first])
            memory.client = api

            dry_run = await memory.prune_orphans()
            applied = await memory.prune_orphans(apply=True)

            self.assertEqual([item["id"] for item in dry_run["candidates"]], ["orphan"])
            self.assertEqual([item["id"] for item in dry_run["unmanaged"]], ["foreign"])
            self.assertTrue(dry_run["dry_run"])
            self.assertEqual(applied["deleted"], 1)
            api.vector_stores.files.delete.assert_awaited_once_with(
                "orphan", vector_store_id="vs_8"
            )
            api.files.delete.assert_awaited_once_with("orphan")

    async def test_rebuild_replaces_store_only_after_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "memory").mkdir()
            (root / "memory/self.md").write_text("記憶\n", encoding="utf-8")
            api = client(
                stores=[SimpleNamespace(id="vs_old"), SimpleNamespace(id="vs_new")],
                uploads=[
                    SimpleNamespace(id="file_old", status="completed"),
                    SimpleNamespace(id="file_new", status="completed"),
                ],
            )
            memory = MemoryVectorStore(root, api, sandbox_key="guild:9")
            await memory.ensure()

            self.assertEqual(await memory.rebuild(), "vs_new")

            manifest = memory._load_manifest()
            self.assertEqual(manifest["vector_store_id"], "vs_new")
            self.assertEqual(manifest["file_id"], "file_new")
            api.vector_stores.delete.assert_awaited_once_with("vs_old")
            api.files.delete.assert_awaited_once_with("file_old")

    async def test_rebuild_restores_manifest_on_failure_and_handles_empty(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            memory = MemoryVectorStore(root, client(), sandbox_key="guild:10")
            self.assertIsNone(await memory.rebuild())
            (root / "memory").mkdir()
            (root / "memory/self.md").write_text("記憶\n", encoding="utf-8")
            original = {
                "sandbox_key": "guild:10", "vector_store_id": "vs_old",
                "file_id": "file_old", "fingerprint": "old",
            }
            memory._save_manifest(original)
            api = client(
                stores=[SimpleNamespace(id="vs_new")],
                uploads=[SimpleNamespace(id="bad", status="failed")],
            )
            api.vector_stores.delete.side_effect = RuntimeError("cleanup unavailable")
            memory.client = api

            with self.assertRaisesRegex(RuntimeError, "indexing failed"):
                await memory.rebuild()

            self.assertEqual(memory._load_manifest(), original)

            fresh_root = root / "fresh"
            (fresh_root / "memory").mkdir(parents=True)
            (fresh_root / "memory/self.md").write_text("記憶\n", encoding="utf-8")
            fresh = MemoryVectorStore(
                fresh_root,
                client(
                    stores=[SimpleNamespace(id="vs_fresh")],
                    uploads=[SimpleNamespace(id="bad_fresh", status="failed")],
                ),
                sandbox_key="guild:11",
            )
            with self.assertRaisesRegex(RuntimeError, "indexing failed"):
                await fresh.rebuild()
            self.assertFalse(fresh.manifest_path.exists())

    async def test_prune_without_store_is_an_empty_dry_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            memory = MemoryVectorStore(Path(temporary), client(), sandbox_key="dm:11")
            result = await memory.prune_orphans(apply=True)
            self.assertEqual(result["candidates"], [])
            self.assertEqual(result["deleted"], 0)
