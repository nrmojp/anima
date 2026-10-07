import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from anima.core.self_time import SelfTimeDecision, SelfTimeService


NOW = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)


class Decider:
    def __init__(self, values): self.values = list(values)
    async def decide(self, context, iteration, previous):
        self.last = (context, iteration, previous)
        return self.values.pop(0)


class SelfTimeDecisionTests(unittest.TestCase):
    def test_validation(self):
        SelfTimeDecision("none")
        SelfTimeDecision("finish", "考えごとをした")
        SelfTimeDecision("none", direction="broaden", discovery="別の手がかりはまだない")
        for value in (
            {"action": "bad"}, {"action": "none", "note": "嘘"},
            {"action": "finish"}, {"action": "finish", "note": "x", "mood_strength": "bad"},
            {"action": "none", "reflection": ""},
            {"action": "none", "direction": "random"},
            {"action": "none", "discovery": ""},
            {"action": "none", "discovery": "x" * 501},
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                SelfTimeDecision(**value)


class SelfTimeServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_survives_restart_is_bounded_and_includes_none(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps({"history": [None, *({"started_at": str(i)} for i in range(7))]}))
            first = Decider([SelfTimeDecision("finish", "絵を保存した", reflection="前の絵を確認した")])
            def make(decider):
                return SelfTimeService(path, decider, dict, lambda *_: None,
                    clock=lambda: NOW, allowed=lambda: True, busy=lambda: False)
            await make(first).tick(forced=True)
            self.assertEqual(len(first.last[0]["recent_self_time"]), 5)
            second = Decider([SelfTimeDecision(
                "none", direction="broaden", discovery="鳥の絵から別の話題は見つからなかった",
                reflection="もう保存済みだった",
            )])
            await make(second).tick(forced=True)
            prior = second.last[0]["recent_self_time"][-1]
            self.assertEqual(prior["status"], "completed")
            self.assertEqual(prior["decisions"][0]["note"], "絵を保存した")
            self.assertEqual(prior["decisions"][0]["reflection"], "前の絵を確認した")
            saved = json.loads(path.read_text())["history"]
            self.assertEqual(len(saved), 5)
            self.assertEqual(saved[-1]["decisions"][0]["action"], "none")
            self.assertEqual(saved[-1]["decisions"][0]["direction"], "broaden")
            self.assertEqual(saved[-1]["decisions"][0]["discovery"], "鳥の絵から別の話題は見つからなかった")

    async def test_failed_commit_history_and_invalid_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps({"history": "invalid"}))
            def fail(*_):
                raise ValueError("commit failed")
            decider = Decider([SelfTimeDecision("finish", "考えた")])
            service = SelfTimeService(path, decider, dict, fail,
                clock=lambda: NOW, allowed=lambda: True, busy=lambda: False)
            with self.assertRaises(ValueError):
                await service.tick()
            self.assertEqual(decider.last[0]["recent_self_time"], [])
            saved = json.loads(path.read_text())["history"][-1]
            self.assertEqual(saved["status"], "failed")
            self.assertEqual(saved["error_type"], "ValueError")

    async def test_tick_loops_commits_modes_and_daily_fuse(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "self-time.json"
            now = [NOW]
            commits = []
            gate = asyncio.Event()

            class BlockingDecider:
                calls = 0
                async def decide(self, context, iteration, previous):
                    self.calls += 1
                    if self.calls == 1:
                        await gate.wait()
                        return SelfTimeDecision("continue", "考え始めた")
                    return SelfTimeDecision("finish", "考え終えた")

            service = SelfTimeService(
                path, BlockingDecider(), lambda: {"mood": "普通"},
                lambda value, at: commits.append((value, at)),
                clock=lambda: now[0], allowed=lambda: True, busy=lambda: False,
                interval_seconds=99, daily_limit=1, max_iterations=3,
            )
            task = asyncio.create_task(service.tick())
            await asyncio.sleep(0)
            self.assertEqual(service.modes()[0].id, "self_time")
            self.assertEqual(await service.tick(), ())
            gate.set()
            result = await task
            self.assertEqual([item.action for item in result], ["continue", "finish"])
            self.assertEqual([item.reflection for item in result], ["記録なし", "記録なし"])
            self.assertEqual(len(commits), 2)
            self.assertEqual(len(json.loads(path.read_text())["history"][-1]["decisions"]), 2)
            self.assertEqual(service.modes(), ())
            self.assertEqual(await service.tick(), ())
            self.assertFalse(await service.interrupt())
            self.assertEqual(json.loads(path.read_text())["starts"], 1)

    async def test_skip_reasons_new_day_invalid_state_and_bad_decider(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "self-time.json"
            path.write_text("bad")
            now = [NOW]
            none = Decider([SelfTimeDecision("none")])
            service = SelfTimeService(
                path, none, dict, lambda *_: None, clock=lambda: now[0],
                allowed=lambda: False, busy=lambda: False, interval_seconds=99,
            )
            self.assertEqual(await service.tick(), ())
            service.allowed = lambda: True
            service.busy = lambda: True
            self.assertEqual(await service.tick(), ())
            service.busy = lambda: False
            self.assertEqual((await service.tick())[0].action, "none")
            now[0] += timedelta(days=1)
            service.decider = Decider([object()])
            with self.assertRaises(TypeError):
                await service.tick()

    async def test_forced_tick_bypasses_mode_and_daily_limit_but_not_busy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "self-time.json"
            path.write_text(json.dumps({"day": NOW.date().isoformat(), "starts": 2}))
            decider = Decider([SelfTimeDecision("none")])
            service = SelfTimeService(
                path, decider, dict, lambda *_: None,
                clock=lambda: NOW, allowed=lambda: False, busy=lambda: False,
                interval_seconds=99, daily_limit=2,
            )
            self.assertEqual((await service.tick(forced=True))[0].action, "none")
            self.assertEqual(json.loads(path.read_text())["starts"], 2)
            self.assertEqual(decider.last[0]["self_time_trigger"], "experimental")
            service.busy = lambda: True
            self.assertEqual(await service.tick(forced=True), ())

    async def test_start_stop_and_interrupt(self):
        with tempfile.TemporaryDirectory() as directory:
            gate = asyncio.Event()
            entered = asyncio.Event()
            class Waiting:
                async def decide(self, *_args):
                    entered.set()
                    await gate.wait()
                    return SelfTimeDecision("none")
            service = SelfTimeService(
                Path(directory) / "state.json", Waiting(), dict, lambda *_: None,
                clock=lambda: NOW, allowed=lambda: True, busy=lambda: False,
                interval_seconds=.001,
            )
            await service.start()
            worker = service._worker
            await service.start()
            self.assertIs(service._worker, worker)
            await entered.wait()
            self.assertTrue(await service.interrupt())
            self.assertEqual(json.loads(service.path.read_text())["history"][-1]["status"], "interrupted")
            await asyncio.sleep(.01)
            self.assertFalse(worker.done())
            await service.stop()
            await service.stop()

    async def test_scheduler_reports_failure_and_continues_next_interval(self):
        with tempfile.TemporaryDirectory() as directory:
            recovered = asyncio.Event()
            mode_changes = []

            class Flaky:
                calls = 0
                async def decide(self, *_args):
                    self.calls += 1
                    if self.calls == 1:
                        raise ValueError("invalid model decision")
                    recovered.set()
                    return SelfTimeDecision("none")

            service = SelfTimeService(
                Path(directory) / "state.json", Flaky(), dict, lambda *_: None,
                clock=lambda: NOW, allowed=lambda: True, busy=lambda: False,
                interval_seconds=.001, daily_limit=2,
                modes_changed=lambda: mode_changes.append(service.modes()),
            )
            with self.assertLogs("anima.core.self_time", level="ERROR") as logs:
                await service.start()
                await asyncio.wait_for(recovered.wait(), timeout=1)
                await service.stop()

            self.assertTrue(any("scheduler will continue" in item for item in logs.output))
            self.assertTrue(any(values for values in mode_changes))
            self.assertEqual(mode_changes[-1], ())

    async def test_invalid_limits(self):
        for values in ((0, 1, 1), (1, 0, 1), (1, 1, 0)):
            with self.assertRaises(ValueError):
                SelfTimeService(
                    Path("x"), Decider([]), dict, lambda *_: None,
                    clock=lambda: NOW, allowed=lambda: True, busy=lambda: False,
                    interval_seconds=values[0], daily_limit=values[1], max_iterations=values[2],
                )


if __name__ == "__main__":
    unittest.main()
