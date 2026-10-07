import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from anima.core.sandbox import SandboxKey, legacy_entries, list_sandboxes, require_separated_layout, reject_symlinks
from anima.core.sandbox_runtime import SandboxRouter, SandboxRuntime, LimitedCalls
from anima.core.state import FileStateStore
from anima.core.models import Event, ReflectionDraft, SentMessage
from anima.core.telemetry import sandbox_context, emit
from anima.bootstrap.cli import collect_status
from anima.adapters.dashboard.server import recent_events
from test_core import NOW, FakeResponder, FakeSender
from anima.core.actor import PersonaActor
from anima.core.context import ContextBuilder


def event(guild="1", *, identifier="10", user="99"):
    return Event(id=identifier, ts=NOW, kind="channel" if guild else "dm",
                 channel_id="100", channel_name="#general" if guild else "DM",
                 author_id=user, author_name="same person", text="hello", guild_id=guild, mention=True)


class SandboxIdentityTests(unittest.TestCase):
    def test_finder_metadata_does_not_break_sandbox_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = SandboxKey("guild", "1")
            key.path(root).mkdir(parents=True)
            (root / "sandboxes" / ".DS_Store").write_bytes(b"Finder metadata")
            (root / "sandboxes" / "guilds" / ".DS_Store").write_bytes(b"Finder metadata")
            self.assertEqual(list_sandboxes(root), [key])

    def test_keys_and_untrusted_inputs(self):
        self.assertEqual(str(SandboxKey.for_event(event())), "guild:1")
        self.assertEqual(str(SandboxKey.for_event(event(None))), "dm:99")
        self.assertEqual(SandboxKey.parse("guild:1"), SandboxKey("guild", "1"))
        own = replace(event(None), author_id="self", sandbox_key="dm:99")
        self.assertEqual(str(SandboxKey.for_event(own)), "dm:99")
        for value in ("", "guild:../x", "guild:abc", "Bad:1", "dm:"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                SandboxKey.parse(value)
        generic = SandboxKey.parse("matrix_room:room-1")
        self.assertEqual((generic.namespace, generic.identifier), ("matrix_room", "room-1"))
        for item in (replace(event(), guild_id=None), replace(event(None), guild_id="1"),
                     replace(event(), sandbox_key="guild:2"), replace(own, sandbox_key="guild:1"),
                     replace(own, sandbox_key=None)):
            with self.subTest(item=item), self.assertRaises(ValueError):
                SandboxKey.for_event(item)

    def test_paths_legacy_and_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(list_sandboxes(root), [])
            require_separated_layout(root)
            for kind in ("guild", "dm"):
                SandboxKey(kind, "1").path(root).mkdir(parents=True)
            self.assertEqual(len(list_sandboxes(root)), 2)
            (root / "cursor.json").write_text("{}")
            self.assertEqual(legacy_entries(root), ["cursor.json"])
            with self.assertRaises(RuntimeError):
                require_separated_layout(root)
            with self.assertRaises(ValueError):
                reject_symlinks(root.parent, root)
            bad = root / "sandboxes/guilds/2"
            bad.touch()
            with self.assertRaises(ValueError):
                list_sandboxes(root)
            bad.unlink()
            bad.symlink_to(root, target_is_directory=True)
            with self.assertRaises(ValueError):
                SandboxKey("guild", "2").path(root)


class ScopedStateTests(unittest.TestCase):
    def test_state_context_jobs_and_recovery_stay_local(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            stores = []
            for key in (SandboxKey("guild", "1"), SandboxKey("guild", "2"), SandboxKey("dm", "99")):
                store = FileStateStore(key.path(base), sandbox_key=key, resource_root=base, recent_limit=0)
                store.ensure_layout(now=NOW)
                stores.append(store)
            a, b, dm = stores
            for store, source in zip(stores, (event(), event("2"), event(None))):
                store.append_received(source)
                job = store.next_maintenance(now=NOW)
                self.assertEqual(job.sandbox_key, str(store.sandbox_key))
                store.commit_digest(job, f"- [12:00 #general] only {store.sandbox_key}")
                self.assertIn(str(store.sandbox_key), store.load_snapshot(source).digest)
            self.assertNotIn("guild:1", b.load_snapshot(event("2")).digest)
            sleep = a.next_maintenance(now=NOW + timedelta(days=1))
            self.assertEqual(sleep.sandbox_key, "guild:1")
            for job in (sleep, a.next_maintenance(now=NOW)):
                if job is not None:
                    with self.assertRaises(ValueError):
                        b._check_job(job)
            with self.assertRaises(ValueError):
                a._check_job(replace(sleep, events=(event("2"),)))
            with self.assertRaises(ValueError):
                a.append_received(event("2"))
            with self.assertRaises(ValueError):
                a.load_snapshot(event("2"))
            with self.assertRaises(ValueError):
                a.read_channel_events("../../outside")
            cursor = a._read_json(a.root / "cursor.json", {})
            cursor["slept_at"] = (NOW + timedelta(days=8)).isoformat()
            a._atomic_write_json(a.root / "cursor.json", cursor)
            reflect = a.next_maintenance(now=NOW + timedelta(days=8))
            a.commit_reflection(reflect, ReflectionDraft(True, "- 話をよく聞く"), now=NOW)
            self.assertEqual(b.load_snapshot(event("2")).habitus, "")
            with self.assertRaises(ValueError):
                b.commit_reflection(reflect, ReflectionDraft(False, ""), now=NOW)
            manifest = {"version": 1, "sandbox_key": "guild:2", "files": {"digest.md": "bad"}}
            with self.assertRaises(ValueError):
                a._apply_transaction(manifest)
            manifest["sandbox_key"] = "guild:1"
            manifest["append_events"] = [event("2").to_log_dict()]
            with self.assertRaises(ValueError):
                a._apply_transaction(manifest)
            self.assertNotEqual(a._read_text(a.root / "digest.md"), "bad")
            (a.root / "memory/link").symlink_to(b.root / "memory", target_is_directory=True)
            with self.assertRaises(ValueError):
                a.load_snapshot(event())

    def test_dm_response_retains_owner_and_cannot_reenter_other_dm(self):
        with tempfile.TemporaryDirectory() as directory:
            key = SandboxKey("dm", "99")
            store = FileStateStore(key.path(Path(directory)), sandbox_key=key)
            store.ensure_layout(now=NOW)
            source = event(None)
            store.append_received(source)
            from anima.core.models import ResponseDraft
            store.commit_response(source=source, draft=ResponseDraft("reply", "ok", "", "弱い", ""),
                                  sent=SentMessage("11", NOW), expected_version=0)
            saved = store.read_channel_events("100")[-1]
            self.assertEqual(SandboxKey.for_event(saved), key)
            self.assertEqual(saved.sandbox_key, "dm:99")


from anima.core.access import ActivityPolicy


class SandboxRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_reload_configuration_updates_each_active_runtime(self):
        first, second = Mock(), Mock()
        router = SandboxRouter(Path("/unused"), Mock())
        router.runtimes = {
            SandboxKey("guild", "1"): SandboxRuntime(Mock(), reload_configuration=first),
            SandboxKey("dm", "2"): SandboxRuntime(Mock(), reload_configuration=second),
            SandboxKey("guild", "3"): SandboxRuntime(Mock()),
        }

        self.assertEqual(router.reload_configuration(), 2)
        first.assert_called_once_with()
        second.assert_called_once_with()

    async def test_independent_real_actors_and_persistent_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            responders = {}
            def factory(key, path):
                responders[key] = FakeResponder()
                return SandboxRuntime(PersonaActor(
                    store=FileStateStore(path, sandbox_key=key, resource_root=root),
                    context_builder=ContextBuilder(), responder=responders[key],
                    sender=FakeSender(), clock=lambda: NOW))
            router = SandboxRouter(root, factory, policy=ActivityPolicy(frozenset({"1", "2"}), True))
            with self.assertRaises(RuntimeError):
                await router.runtime(SandboxKey("guild", "1"))
            await router.start()
            try:
                with self.assertLogs("anima.telemetry", level="INFO") as logs:
                    await asyncio.gather(router.submit(event()), router.submit(event("2")), router.submit(event(None)))
                self.assertEqual(len(router.runtimes), 3)
                self.assertEqual({json.loads(record.getMessage())["sandbox_key"] for record in logs.records},
                                 {"guild:1", "guild:2", "dm:99"})
                self.assertIsNone(sandbox_context.get())
                self.assertEqual(router.pending_events(), ())
                router.abandon(event())
            finally:
                await router.stop()
            await router.start()
            self.assertEqual(len(router.runtimes), 3)
            await router.stop()

    async def test_capacity_voice_music_cleanup_failure_and_parallelism(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            blocked = asyncio.Event()
            async def submit(source):
                if source.guild_id == "1":
                    await blocked.wait()
                return source.guild_id
            def factory(key, path):
                return SandboxRuntime(SimpleNamespace(start=AsyncMock(), stop=AsyncMock(), submit=submit),
                                      SimpleNamespace(bind=Mock(), start=AsyncMock(), stop=AsyncMock()),
                                      SimpleNamespace(bind=Mock(), stop=AsyncMock()),
                                      SimpleNamespace(stop=AsyncMock()))
            router = SandboxRouter(root, factory, max_sandboxes=2, policy=ActivityPolicy(frozenset({"1", "2", "3"}), True))
            client = object()
            router.bind(client)
            await router.start()
            task = asyncio.create_task(router.submit(event()))
            self.assertEqual(await asyncio.wait_for(router.submit(event("2")), 1), "2")
            blocked.set()
            await task
            runtime = await router.runtime(SandboxKey("guild", "1"))
            runtime.voice.bind.assert_called_once_with(client)
            with self.assertRaises(RuntimeError):
                await router.submit(event("3"))
            await router.stop()
            runtime.music.stop.assert_awaited_once()
            runtime.dj.stop.assert_awaited_once_with("bot_stopping")
            runtime.voice.stop.assert_awaited_once()
            bad = factory(None, None)
            bad.voice.start.side_effect = RuntimeError("failed")
            key = SandboxKey("guild", "1")
            key.path(root).mkdir(parents=True)
            failing = SandboxRouter(root, lambda *_: bad, policy=ActivityPolicy(frozenset({"1"})))
            with self.assertRaisesRegex(RuntimeError, "failed"):
                await failing.start()
            bad.actor.stop.assert_awaited_once()
            self.assertEqual(failing.runtimes, {})

    async def test_shared_limit_releases_on_error(self):
        semaphore = asyncio.Semaphore(1)
        target = SimpleNamespace(call=AsyncMock(side_effect=[RuntimeError("fail"), "ok"]))
        limited = LimitedCalls(target, semaphore)
        with self.assertRaises(RuntimeError):
            await limited.call()
        self.assertEqual(await limited.call(), "ok")
