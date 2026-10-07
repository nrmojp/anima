from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import tempfile
import unittest

from anima.capabilities.contracts import CapabilityContext
from anima.core.context import ContextBuilder
from anima.core.inventory import InventoryStore, MAX_ARTIFACT_BYTES, MAX_TEXT_BYTES
from anima.core.models import Attachment, Event
from anima.core.sandbox import SandboxKey


class InventoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.key = SandboxKey("guild", "1")
        self.store = InventoryStore(self.root, self.key)

    def tearDown(self):
        self.temporary.cleanup()

    def test_stage_keep_list_resolve_and_discard(self):
        self.assertEqual(self.store.list(include_temporary=True), ())
        staged = self.store.stage_bytes("drawing.png", b"png")
        self.assertEqual(staged.to_dict()["id"], "drawing.png")
        self.assertEqual(staged.location, "temporary")
        self.assertIn("一時成果物は1個", self.store.summary())
        self.assertEqual(self.store.resolve("drawing.png").read_bytes(), b"png")
        kept = self.store.keep("drawing.png", filename="orange-cat.png")
        self.assertEqual(kept.location, "inventory")
        self.assertEqual([item.id for item in self.store.list()], ["orange-cat.png"])
        self.assertIn("残した物は1個", self.store.summary())
        self.assertEqual(self.store.discard("orange-cat.png"), "inventory")
        self.assertEqual(self.store.summary(), "手元に残している物はない。")

    def test_stage_file_validates_and_copies_external_content(self):
        source = self.root / "source.bin"
        source.write_bytes(b"external")
        item = self.store.stage_file("copy.bin", source)
        self.assertEqual(item.location, "temporary")
        self.assertEqual(self.store.resolve("copy.bin").read_bytes(), b"external")
        with self.assertRaises(FileExistsError):
            self.store.stage_file("copy.bin", source)
        empty = self.root / "empty.bin"
        empty.touch()
        with self.assertRaisesRegex(ValueError, "empty"):
            self.store.stage_file("empty.bin", empty)
        with self.assertRaisesRegex(ValueError, "regular"):
            self.store.stage_file("missing.bin", self.root / "missing.bin")
        link = self.root / "link.bin"
        link.symlink_to(source)
        with self.assertRaisesRegex(ValueError, "regular"):
            self.store.stage_file("link.bin", link)
        large = self.root / "large.bin"
        large.write_bytes(b"x" * (MAX_ARTIFACT_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "too large"):
            self.store.stage_file("large.bin", large)

    def test_import_file_validates_and_copies_to_inventory(self):
        source = self.root / "source.bin"
        source.write_bytes(b"content")
        item = self.store.import_file("saved.bin", source)
        self.assertEqual(item.location, "inventory")
        self.assertEqual(self.store.resolve("saved.bin").read_bytes(), b"content")
        with self.assertRaises(FileExistsError):
            self.store.import_file("saved.bin", source)
        with self.assertRaisesRegex(ValueError, "extension"):
            self.store.import_file("saved.txt", source)
        empty = self.root / "empty.bin"
        empty.touch()
        with self.assertRaisesRegex(ValueError, "empty"):
            self.store.import_file("empty-copy.bin", empty)
        with self.assertRaisesRegex(ValueError, "regular"):
            self.store.import_file("missing.bin", self.root / "missing.bin")
        large = self.root / "large-import.bin"
        large.write_bytes(b"x" * (MAX_ARTIFACT_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "too large"):
            self.store.import_file("large-copy.bin", large)

    def test_text_write_append_read_and_limits(self):
        self.store.write_text("note.md", "a")
        self.store.write_text("note.md", "b", append=True)
        self.assertEqual(self.store.read_text("note.md"), "ab")
        with self.assertRaises(FileNotFoundError):
            self.store.write_text("missing.md", "x", append=True)
        with self.assertRaises(FileExistsError):
            self.store.stage_bytes("note.md", b"x")
        self.store.stage_bytes("temporary.txt", b"x")
        with self.assertRaises(FileExistsError):
            self.store.write_text("temporary.txt", "x")
        with self.assertRaisesRegex(ValueError, "too large"):
            self.store.write_text("large.md", "x" * (MAX_TEXT_BYTES + 1))
        oversized = self.store.inventory_root / "oversized.bin"
        oversized.write_bytes(b"x" * (MAX_TEXT_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "readable text"):
            self.store.read_text("oversized.bin")
        with self.assertRaisesRegex(ValueError, "empty"):
            self.store.stage_bytes("x.bin", b"")
        with self.assertRaisesRegex(ValueError, "invalid"):
            self.store.write_text("../x.md", "x")
        with self.assertRaises(FileNotFoundError):
            self.store.resolve("missing.txt")
        with self.assertRaisesRegex(ValueError, "too large"):
            self.store.stage_bytes("huge.bin", b"x" * (MAX_ARTIFACT_BYTES + 1))

    def test_missing_and_collision_branches(self):
        self.assertEqual(self.store.cleanup_temporary(now=datetime.now(timezone.utc)), 0)
        with self.assertRaises(FileNotFoundError):
            self.store.keep("missing.bin")
        with self.assertRaises(FileNotFoundError):
            self.store.discard("missing.bin")
        self.store.stage_bytes("same.bin", b"temporary")
        self.store.inventory_root.mkdir(parents=True, exist_ok=True)
        (self.store.inventory_root / "same.bin").write_bytes(b"durable")
        with self.assertRaises(FileExistsError):
            self.store.keep("same.bin")
        self.store.stage_bytes("rename.png", b"png")
        with self.assertRaisesRegex(ValueError, "extension"):
            self.store.keep("rename.png", filename="rename.jpg")
        oversized = self.store.inventory_root / "direct.bin"
        oversized.write_bytes(b"x" * (MAX_ARTIFACT_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "too large"):
            self.store.resolve("direct.bin")
        temporary_oversized = self.store.temporary_root / "temporary-huge.bin"
        temporary_oversized.write_bytes(b"x" * (MAX_ARTIFACT_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "too large"):
            self.store.keep("temporary-huge.bin")
        with self.assertRaisesRegex(ValueError, "location"):
            self.store.resolve("same.bin", location="other")
        self.assertEqual(
            self.store.resolve("same.bin", location="temporary").read_bytes(),
            b"temporary",
        )

    def test_configurable_capacity_limits(self):
        with self.assertRaisesRegex(ValueError, "positive"):
            InventoryStore(self.root, self.key, max_items=0)
        count_limited = InventoryStore(self.root / "count", self.key, max_items=1)
        count_limited.write_text("one.md", "1")
        with self.assertRaisesRegex(ValueError, "item limit"):
            count_limited.write_text("two.md", "2")
        byte_limited = InventoryStore(self.root / "bytes", self.key, max_bytes=2)
        byte_limited.write_text("one.md", "12")
        with self.assertRaisesRegex(ValueError, "byte limit"):
            byte_limited.write_text("one.md", "123")

    def test_rejects_symlinks_and_cleans_expired_temporary(self):
        self.store.stage_bytes("old.bin", b"x")
        old = datetime.now(timezone.utc) - timedelta(days=2)
        os.utime(self.store.temporary_root / "old.bin", (old.timestamp(), old.timestamp()))
        self.assertEqual(
            self.store.cleanup_temporary(now=datetime.now(timezone.utc)), 1
        )
        self.assertEqual(self.store.cleanup_temporary(now=datetime.now(timezone.utc)), 0)
        self.store.inventory_root.mkdir(parents=True, exist_ok=True)
        (self.store.inventory_root / "linked.txt").symlink_to(self.root / "outside")
        with self.assertRaisesRegex(ValueError, "symbolic"):
            self.store.list()

    def test_context_summary(self):
        self.store.write_text("note.md", "hello")
        section = ContextBuilder(inventory=self.store)._inventory_section()
        self.assertIn("残した物は1個", section)
        self.assertIn("resource_list", section)
        self.assertIn("interface.current_attachments", section)
