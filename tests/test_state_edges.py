import json
import unittest
from types import SimpleNamespace
from datetime import datetime, timezone, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from anima.core.state import FileStateStore, ConcurrentStateChange, InvalidMaintenanceDraft
from anima.core.self_time import SelfTimeDecision
from anima.core.models import (
    Event, MentionedPerson, SleepJob, SleepDraft, MemoryDocument,
    ResponseDraft, SentMessage,
)


class StateEdgeTests(unittest.TestCase):
    def test_sleep_due_reads_no_history_and_checks_deadline(self):
        with TemporaryDirectory() as directory:
            store = FileStateStore(Path(directory))
            now = datetime(2026, 10, 4, tzinfo=timezone.utc)
            with patch.object(store, "_read_all_events", side_effect=AssertionError("history loaded")):
                self.assertFalse(store.sleep_due(now=now))
            store.ensure_layout(now=now)
            with patch.object(store, "_read_all_events", side_effect=AssertionError("history loaded")):
                self.assertFalse(store.sleep_due(now=now + timedelta(hours=24, microseconds=-1)))
                self.assertTrue(store.sleep_due(now=now + timedelta(hours=24)))
                self.assertTrue(store.sleep_due(now=now + timedelta(days=2)))

    def test_bulk_event_validation_does_not_rescan_tree_per_event(self):
        from test_sandbox import event
        from anima.core.sandbox import SandboxKey
        from dataclasses import replace
        with TemporaryDirectory() as directory:
            base = Path(directory)
            key = SandboxKey("guild", "1")
            store = FileStateStore(key.path(base), sandbox_key=key, resource_root=base)
            now = event().ts
            store.ensure_layout(now=now)
            for index in range(30):
                store.append_received(replace(event(), id=str(index)))
            with patch.object(store, "_check_tree", wraps=store._check_tree) as checks:
                self.assertEqual(len(store.read_channel_events("100")), 30)
                self.assertEqual(checks.call_count, 1)
            job = store.forced_maintenance("sleep", now=now)
            with patch.object(store, "_check_tree", wraps=store._check_tree) as checks:
                store._check_job(job)
                self.assertEqual(checks.call_count, 1)
            with self.assertRaisesRegex(ValueError, "another sandbox"):
                store._check_event(event("2"), check_tree=False)
            (store.root / "unsafe").symlink_to(base)
            with self.assertRaises(ValueError):
                store.read_channel_events("100")

    def test_background_notification_loads_requester_memory_not_system(self):
        from anima.core.context import ContextBuilder
        for kind in ("channel", "dm"):
            with self.subTest(kind=kind), TemporaryDirectory() as directory:
                root = Path(directory)
                now = datetime(2026, 10, 1, tzinfo=timezone.utc)
                store = FileStateStore(root)
                store.ensure_layout(now=now)
                people = root / "memory/people"
                people.mkdir(exist_ok=True)
                (people / "user.md").write_text("呼び方: にるもちゃん\n- [2026-09-30 DM] 秘密の好み\n")
                (people / "self.md").write_text("これは相手ではない")
                original = Event("m1", now - timedelta(minutes=1), kind, "c", "#c", "user", "表示名", "描いて")
                store.append_received(original)
                notification = Event("job", now, kind, "c", "#c", "self", "システム", "失敗",
                                     author_is_bot=True, response_target=MentionedPerson("user", "表示名"),
                                     response_target_is_bot=True)
                snapshot = store.load_snapshot(notification)
                self.assertEqual(snapshot.current_author_id, "user")
                self.assertEqual(snapshot.current_author_name, "表示名")
                self.assertTrue(snapshot.current_author_is_bot)
                self.assertEqual(snapshot.current_author_last_seen_at, original.ts)
                self.assertNotIn(snapshot.current_author_memory, snapshot.people_memory)
                instructions = ContextBuilder()._current_author_section(snapshot)
                self.assertIn("にるもちゃん", instructions)
                self.assertNotIn("これは相手ではない", instructions)
                self.assertEqual("この場では触れない" in instructions, kind == "channel")

    def test_self_time_updates_only_self_memory_mood_and_version(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime(2026, 9, 20, tzinfo=timezone.utc)
            store = FileStateStore(root)
            store.ensure_layout(now=now)
            context = store.self_time_context()
            self.assertEqual(context["self_memory"], "")
            decision = SelfTimeDecision(
                "finish", "カレーの香りについて考えた", "満足", "考えごと", "弱い", "カレー",
            )
            store.commit_self_time(decision, now=now)
            self.assertIn("#self self-time", (root / "memory/self.md").read_text())
            self.assertEqual(json.loads((root / "mood.md").read_text())["state"], "満足")
            self.assertEqual(json.loads((root / "cursor.json").read_text())["version"], 1)
            with self.assertRaises(ValueError):
                store.commit_self_time(SelfTimeDecision("none"), now=now)
            with self.assertRaisesRegex(ValueError, "note"):
                store.commit_self_time(SimpleNamespace(action="finish", note=""), now=now)

    def test_job_promises_are_exact_idempotent_and_validated(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime(2026, 9, 1, tzinfo=timezone.utc)
            store = FileStateStore(root)
            store.ensure_layout(now=now)
            source = Event(
                id="m1", ts=now, kind="channel", channel_id="general",
                channel_name="#general room]", author_id="user", author_name="user",
                text="猫を描いて",
            )
            store.add_open_promise("j-123456789abc", source, "  猫を   描く  ")
            store.add_open_promise("j-123456789abc", source, "重複")
            content = (root / "open.md").read_text()
            self.assertEqual(content.count("j-123456789abc"), 1)
            self.assertIn("#general-room-", content)
            self.assertFalse(store.close_open_promise("j-missing"))
            self.assertTrue(store.close_open_promise("j-123456789abc"))
            self.assertEqual((root / "open.md").read_text(), "")
            with self.assertRaisesRegex(ValueError, "job ID"):
                store.add_open_promise("bad", source, "猫")
            with self.assertRaisesRegex(ValueError, "empty"):
                store.add_open_promise("j-abcdefabcdef", source, "  ")

    def test_retention_keeps_recent_empty_invalid_and_unprocessed_logs(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime(2026, 9, 1, tzinfo=timezone.utc)
            store = FileStateStore(root)
            store.ensure_layout(now=now)
            logs = root / 'log' / 'general'
            logs.mkdir(exist_ok=True)
            for name in ('invalid.jsonl', '2026-08-31.jsonl', '2026-01-01.jsonl'):
                (logs / name).write_text('')
            event = Event(id='old', ts=now - timedelta(days=40), kind='channel',
                          channel_id='general', channel_name='#general',
                          author_id='user', author_name='user', text='hello')
            store.append_received(event)
            cursor = root / 'cursor.json'
            data = json.loads(cursor.read_text())
            data['digested_until'] = (now - timedelta(days=50)).isoformat()
            cursor.write_text(json.dumps(data))
            archives = root / 'archive' / 'log' / 'general'
            archives.mkdir(parents=True, exist_ok=True)
            (archives / 'invalid.jsonl.gz').write_bytes(b'')
            self.assertEqual(store.archive_old_logs(now=now), {'archived': 0, 'removed_archives': 0})
            self.assertEqual(len(list(logs.glob('*.jsonl'))), 4)
            self.assertTrue((archives / 'invalid.jsonl.gz').exists())

    def test_idle_maintenance_and_invalid_sleep_preserve_state(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime(2026, 9, 1, tzinfo=timezone.utc)
            store = FileStateStore(root)
            store.ensure_layout(now=now)
            self.assertIsNone(store.next_maintenance(now=now))
            job = store.next_maintenance(now=now + timedelta(days=1))
            self.assertIsInstance(job, SleepJob)
            before = (root / 'cursor.json').read_text()
            document = MemoryDocument('self', 'self', '')
            for memories, message in (((document, document), 'duplicate'), ((document,), 'omitted')):
                with self.subTest(message=message):
                    draft = SleepDraft(memories, '', '穏やか', '', '弱い', '')
                    with self.assertRaisesRegex(ValueError, message):
                        store.commit_sleep(job, draft, now=now)
                    self.assertEqual((root / 'cursor.json').read_text(), before)

    def test_sleep_accepts_mentioned_person_id_and_rejects_invented_id(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime(2026, 9, 1, tzinfo=timezone.utc)
            store = FileStateStore(root)
            store.ensure_layout(now=now)
            introduction = Event(
                id='intro', ts=now + timedelta(seconds=1), kind='channel', channel_id='general',
                channel_name='#general', author_id='a', author_name='A',
                text='<@b>を紹介するよ',
                mentioned_people=(MentionedPerson('b', 'B'),),
            )
            store.append_received(introduction)
            store.mark_handled(introduction)
            job = store.next_maintenance(now=now + timedelta(days=1))
            self.assertIsInstance(job, SleepJob)
            base = (
                MemoryDocument('self', 'self', ''),
                MemoryDocument('world', 'world', ''),
            )
            unknown = SleepDraft(
                base + (MemoryDocument('person', 'guessed', '## 誰か'),),
                '', '穏やか', '', '弱い', '',
            )
            with self.assertRaisesRegex(ValueError, 'invented person ids'):
                store.commit_sleep(job, unknown, now=now)

            valid = SleepDraft(
                base + (MemoryDocument(
                    'person', 'b', '## B\n- [2026-09-01 #general A談] 紹介された'
                ),),
                '', '穏やか', '', '弱い', '',
            )
            store.commit_sleep(job, valid, now=now)
            self.assertIn('A談', (root / 'memory' / 'people' / 'b.md').read_text())

    def test_stale_response_does_not_write(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime(2026, 9, 1, tzinfo=timezone.utc)
            store = FileStateStore(root)
            store.ensure_layout(now=now)
            source = Event(id='m1', ts=now, kind='channel', channel_id='general',
                           channel_name='#general', author_id='user', author_name='user', text='hello')
            with self.assertRaises(ConcurrentStateChange):
                store.commit_response(source=source, draft=ResponseDraft('hi', '穏やか', '', '弱い', ''),
                                      sent=SentMessage('r1', now), expected_version=1)
            self.assertEqual(store._read_all_events(), [])

    def test_deleted_event_is_excluded_without_losing_idempotency(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = FileStateStore(root)
            store.ensure_layout(now=datetime(2026, 9, 1, tzinfo=timezone.utc))
            first = Event(id='100', ts=datetime(2026, 9, 1, tzinfo=timezone.utc),
                          kind='channel', channel_id='200', channel_name='#general',
                          author_id='300', author_name='user', text='消した発言', mention=True)
            second = Event(id='101', ts=first.ts + timedelta(seconds=1), kind='channel',
                           channel_id='200', channel_name='#general', author_id='300',
                           author_name='user', text='次の発言', mention=True)
            store.append_received(first)
            store.append_received(second)

            self.assertTrue(store.mark_deleted('200', '100'))
            self.assertFalse(store.mark_deleted('200', '100'))
            self.assertTrue(store.is_deleted('200', '100'))
            self.assertTrue(store.contains_event(first))
            self.assertEqual([item.id for item in store.load_snapshot(second).recent_events], ['101'])
            self.assertEqual([item.id for item in store.pending_addressed_events()], ['101'])
            self.assertIn('100', (root / 'deleted' / '200.txt').read_text())
            with self.assertRaises(ValueError):
                store.mark_deleted('../outside', '100')
            with self.assertRaises(ValueError):
                store.is_deleted('200', '../100')

    def test_legacy_cursor_missing_timestamps_is_initialized(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'cursor.json').write_text('{"version": 4}')
            now = datetime(2026, 1, 1, tzinfo=timezone.utc)
            store = FileStateStore(root)
            store.ensure_layout(now=now)
            cursor = json.loads((root / 'cursor.json').read_text())
            self.assertEqual(cursor['version'], 4)
            self.assertEqual(cursor['slept_at'], now.isoformat())
            self.assertEqual(cursor['reflected_at'], now.isoformat())

    def test_unknown_transaction_version_preserves_manifest(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / '.anima-transaction.json'
            manifest.write_text('{"version": 99}')
            with self.assertRaisesRegex(RuntimeError, 'unsupported'):
                FileStateStore(root).ensure_layout(now=datetime.now(timezone.utc))
            self.assertTrue(manifest.exists())

    def test_symlink_cannot_escape_state_root(self):
        with TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / 'state'
            root.mkdir()
            outside = base / 'outside'
            outside.mkdir()
            (root / 'link').symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'escapes'):
                FileStateStore(root)._safe_state_path('link/data.md')
            self.assertFalse((outside / 'data.md').exists())

    def test_missing_directories_and_stale_version(self):
        with TemporaryDirectory() as directory:
            store = FileStateStore(Path(directory))
            self.assertEqual(store._read_all_events(), [])
            self.assertEqual(store._read_memory_directory('people'), ())
            with self.assertRaises(ConcurrentStateChange):
                store._cursor_at_version(1)

    def test_invalid_maintenance_and_metadata_only_compaction(self):
        with TemporaryDirectory() as directory:
            store = FileStateStore(Path(directory), memory_max_lines=2)
            with self.assertRaises(InvalidMaintenanceDraft):
                store._validate_digest('  ')
            with self.assertRaisesRegex(InvalidMaintenanceDraft, 'invalid digest line'):
                store._validate_digest('missing timestamp and channel')
            with self.assertRaises(InvalidMaintenanceDraft):
                store._validate_open_items('not an open item')
            self.assertEqual(store._compact_memory('## a\n## b\n## c'), '## a\n## b')
