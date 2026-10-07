from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from anima.core.proactivity import ProactiveDecisionEngine
from anima.core.reaction_queue import ReactionBatch
from anima.core.sandbox import SandboxKey
from anima.core.state import FileStateStore
from test_core import NOW
from test_sandbox import event


class ProactivityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = FileStateStore(SandboxKey("guild", "1").path(Path(temporary.name)), sandbox_key=SandboxKey("guild", "1"))
        self.store.ensure_layout(now=NOW)
        self.source = replace(event(), mention=False)
        self.store.append_received(self.source)
        self.classifier = SimpleNamespace(classify=AsyncMock(return_value={"action":"speak", "target":"10", "face":None}))
        self.engine = ProactiveDecisionEngine(self.store, self.classifier, cooldown=1)

    async def process(self, sources=None, **kwargs):
        return await self.engine.process(ReactionBatch(tuple(sources if sources is not None else (self.source,))), now=kwargs.pop("now", NOW), allow_speak=kwargs.pop("allow_speak", True), **kwargs)

    async def test_success_restart_cooldown_duplicate_and_day_rollover(self):
        self.assertEqual(await self.process(), self.source)
        arguments = self.classifier.classify.call_args
        self.assertEqual(arguments.args[2], ())
        self.assertFalse(arguments.kwargs["allow_react"])
        self.assertIsNone(await self.process())
        self.assertIsNone(await self.process(now=NOW+timedelta(seconds=2)))
        new = replace(self.source, id="new", ts=NOW+timedelta(days=1))
        self.classifier.classify.return_value["target"] = "new"
        self.assertEqual(await self.process((new,), now=new.ts), new)

    async def test_eligibility_stale_invalid_and_no_decision(self):
        self.assertIsNone(await self.process(()))
        self.assertIsNone(await self.process(allow_speak=False))
        self.assertIsNone(await self.process(now=NOW+timedelta(seconds=61)))
        self.assertIsNone(await self.process(now=NOW-timedelta(seconds=1)))
        for source in (event(), replace(self.source, author_id="self"), event(None)):
            with self.assertRaises(ValueError):
                await self.process((source,))
        with self.assertRaises(ValueError):
            await self.process((self.source, replace(self.source, channel_id="200")))
        self.classifier.classify.return_value = {"action":"none", "target":None, "face":None}
        self.assertIsNone(await self.process())
        for decision in ({"action":"speak", "target":"missing", "face":None}, {"action":"react", "target":"10", "face":"喜"}, {"action":"speak", "target":"10", "face":"喜"}):
            self.classifier.classify.return_value = decision
            with self.assertRaises(ValueError):
                await self.process()

    async def test_limits_bot_loop_and_failed_api_reservation(self):
        path = self.store.root / "runtime/proactivity.json"
        initial = {"day":NOW.date().isoformat(), "calls":0, "speaks":0, "last_spoke_at":0, "targets":[], "bot_speaks":{}}
        for changes in ({"calls":100}, {"speaks":4}, {"last_spoke_at":NOW.timestamp()}, {"bot_speaks":{"100":[NOW.timestamp(), NOW.timestamp()]}}):
            self.store._atomic_write_json(path, {**initial, **changes})
            self.assertIsNone(await self.process((replace(self.source, author_is_bot=True),)))
        self.store._atomic_write_json(path, initial)
        self.assertEqual(await self.process((replace(self.source, author_is_bot=True),)), replace(self.source, author_is_bot=True))
        self.assertEqual(len(self.store._read_json(path, {})["bot_speaks"]["100"]), 1)
        self.store._atomic_write_json(path, initial)
        self.classifier.classify.side_effect = TimeoutError("offline")
        with self.assertRaises(TimeoutError):
            await self.process()
        self.assertEqual(self.store._read_json(path, {})["calls"], 1)
        with self.assertRaises(ValueError):
            ProactiveDecisionEngine(self.store, self.classifier, daily_limit=0)
