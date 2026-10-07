import asyncio
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from anima.adapters.discord.output import DiscordMessageOutput
from anima.adapters.discord.voice_lifecycle import leave_empty_voice_channel
from anima.core.access import ActivityPolicy
from anima.core.events import SandboxEventMailbox
from anima.core.models import ResponseDraft, ProcessOutcome, OutcomeKind
from anima.core.sandbox import SandboxKey, list_sandboxes
from anima.core.sandbox_runtime import SandboxRuntime, SandboxRouter
from test_sandbox import event


def draft(images=()):
    return ResponseDraft("hello", "calm", "", "弱い", "", images=images)


class OutputRoutesTests(unittest.IsolatedAsyncioTestCase):
    async def test_output_validates_destination_and_sandbox_for_every_transport(self):
        guild = SimpleNamespace(guild=SimpleNamespace(id=1), send=AsyncMock())
        client = SimpleNamespace(get_channel=lambda _: guild)
        output = DiscordMessageOutput(client, SandboxKey("guild", "1"))
        await output.send("100", draft())
        self.assertNotIn("files", guild.send.call_args.kwargs)
        with patch("anima.adapters.discord.output.discord.File", side_effect=lambda path:path):
            await output.send("100", draft((Path("one.png"),)))
        self.assertEqual(guild.send.call_args.kwargs["files"], [Path("one.png")])
        self.assertFalse(guild.send.call_args.kwargs["allowed_mentions"].everyone)
        for identifier in ("bad", "１", "-1"):
            with self.assertRaises(ValueError):
                await output.send(identifier, draft())
        guild.guild.id = 2
        with self.assertRaises(PermissionError):
            await output.send("100", draft())
        client.get_channel = lambda _:None
        with self.assertRaises(RuntimeError):
            await output.send("100", draft())
        output.sandbox_key = SandboxKey("matrix_room", "room")
        with self.assertRaises(RuntimeError):
            await output.send("100", draft())
        dm = SimpleNamespace(recipient=SimpleNamespace(id=20), send=AsyncMock())
        client.get_channel = lambda _:dm
        output.sandbox_key = SandboxKey("dm", "20")
        await output.send("100", draft())
        dm.recipient = None
        with self.assertRaises(PermissionError):
            await output.send("100", draft())

    async def test_mailbox_accepts_actor_outcome_and_unbinds(self):
        key = SandboxKey("guild", "1")
        mailbox = SandboxEventMailbox(key)
        outcome = ProcessOutcome(OutcomeKind.IGNORED, "10")
        mailbox.bind(AsyncMock(return_value=outcome))
        self.assertIs(await mailbox.publish(replace(event(), sandbox_key=str(key))), outcome)
        await mailbox.stop()
        with self.assertRaises(RuntimeError):
            await mailbox.publish(replace(event(), sandbox_key=str(key)))

    async def test_empty_vc_releases_new_shared_audio_output_only(self):
        with TemporaryDirectory() as directory:
            key = SandboxKey("guild", "1")
            router = SandboxRouter(Path(directory), Mock(), policy=ActivityPolicy(frozenset({"1"})))
            audio = SimpleNamespace(stop=AsyncMock())
            router.runtimes[key] = SandboxRuntime(SimpleNamespace(), audio_output=audio)
            channel = SimpleNamespace(id=100, members=[])
            guild = SimpleNamespace(id=1)
            connection = SimpleNamespace(guild=guild, channel=channel, is_connected=lambda:True, disconnect=AsyncMock())
            client = SimpleNamespace(actor=router, voice=None, music=None, voice_clients=[connection])
            await leave_empty_voice_channel(client, SimpleNamespace(bot=False, guild=guild), SimpleNamespace(channel=channel), SimpleNamespace(channel=None))
            audio.stop.assert_awaited_once()
            connection.disconnect.assert_awaited_once_with(force=True)
            connection.is_connected = lambda:False
            await leave_empty_voice_channel(client, SimpleNamespace(bot=False, guild=guild), SimpleNamespace(channel=channel), SimpleNamespace(channel=None))
            self.assertEqual(connection.disconnect.await_count, 1)


class RuntimeServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_services_start_once_rollback_and_reverse_shutdown(self):
        with TemporaryDirectory() as directory:
            log = []
            class Service:
                def __init__(self, name, fail=False): self.name, self.fail = name, fail
                async def start(self):
                    log.append("start:"+self.name)
                    if self.fail: raise ValueError("startup failure")
                async def stop(self): log.append("stop:"+self.name)
            actor = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
            first, second = Service("first"), Service("second")
            runtime = SandboxRuntime(actor, services=(first, first, second), plugins=SimpleNamespace(start=AsyncMock(), stop=AsyncMock()), jobs=SimpleNamespace(start=AsyncMock(), stop=AsyncMock()), self_time=SimpleNamespace(start=AsyncMock(), stop=AsyncMock()))
            router = SandboxRouter(Path(directory), lambda *_:runtime, policy=ActivityPolicy(frozenset({"1"})))
            await router.start()
            await router.runtime(SandboxKey("guild", "1"))
            await router.stop()
            self.assertEqual(log, ["start:first", "start:second", "stop:second", "stop:first"])
            self.assertEqual(runtime.started_services, ())
            log.clear()
            second.fail = True
            await router.start()
            with self.assertRaises(ValueError): await router.runtime(SandboxKey("guild", "1"))
            self.assertEqual(log, ["start:first", "start:second", "stop:first"])
            self.assertEqual(router.runtimes, {})
            await router.stop()

    async def test_services_release_even_when_components_or_services_fail(self):
        service = SimpleNamespace(stop=AsyncMock(side_effect=ValueError("service")))
        other = SimpleNamespace(stop=AsyncMock())
        actor = SimpleNamespace(stop=AsyncMock(side_effect=RuntimeError("actor")))
        runtime = SandboxRuntime(actor, started_services=(other, service))
        with self.assertRaises(ExceptionGroup): await SandboxRouter._stop_runtime(runtime)
        other.stop.assert_awaited_once()
        self.assertEqual(runtime.started_services, ())
        actor.stop.side_effect = None
        runtime.started_services = (service,)
        with self.assertRaisesRegex(ValueError, "service"):
            await SandboxRouter._stop_runtime(runtime)
        jobs = SimpleNamespace(stop=AsyncMock())
        runtime.jobs = jobs
        runtime.self_time = SimpleNamespace(stop=AsyncMock(side_effect=ValueError("self time")))
        runtime.plugins = SimpleNamespace(stop=AsyncMock(side_effect=RuntimeError("plugin")))
        with self.assertRaises(ExceptionGroup):
            await SandboxRouter._stop_runtime(runtime)
        jobs.stop.assert_awaited_once()

    async def test_legacy_optional_lifecycles_remain_supported(self):
        with TemporaryDirectory() as directory:
            runtime = SandboxRuntime(SimpleNamespace(start=AsyncMock(), stop=AsyncMock()),
                                     reminders=SimpleNamespace(start=AsyncMock(), stop=AsyncMock()))
            router = SandboxRouter(Path(directory), lambda *_:runtime, policy=ActivityPolicy(frozenset({"1"})))
            await router.start()
            await router.runtime(SandboxKey("guild", "1"))
            runtime.reminders.start.assert_awaited_once()
            await router.stop()
            runtime.reminders.stop.assert_awaited_once()

    def test_generic_sandboxes_are_enumerated_and_explicitly_admitted(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            key = SandboxKey("matrix_room", "room-1")
            key.path(root).mkdir(parents=True)
            self.assertEqual(list_sandboxes(root), [key])
            self.assertFalse(ActivityPolicy(dm_enabled=True).allows(key))
            self.assertTrue(ActivityPolicy(allowed_sandbox_keys=frozenset({key})).allows(key))
            (root / "sandboxes" / "invalid").touch()
            with self.assertRaises(ValueError): list_sandboxes(root)
