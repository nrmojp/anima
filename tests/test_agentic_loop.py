import unittest

from anima.core.agentic_loop import (
    AgenticLoop, AgentToolCall, ThoughtStep, ToolObservation,
)


class Backend:
    def __init__(self, steps):
        self.steps = iter(steps)
        self.requests = []

    async def think(self, request_count, tools_enabled, observations):
        self.requests.append((request_count, tools_enabled, observations))
        return next(self.steps)


class Executor:
    def __init__(self, *, invalid=False):
        self.calls = []
        self.invalid = invalid

    async def execute(self, call):
        self.calls.append(call)
        return None if self.invalid else ToolObservation(call, "done")


class AgenticLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_multiple_tool_rounds_and_final_result(self):
        first = AgentToolCall("1", "read", "{}")
        second = AgentToolCall("2", "write", "{}")
        backend = Backend((ThoughtStep((first, second)), ThoughtStep(result="finished")))
        executor = Executor()
        result = await AgenticLoop(max_tool_rounds=2).run(backend=backend, executor=executor)
        self.assertEqual((result.result, result.request_count, result.tool_call_count),
                         ("finished", 2, 2))
        self.assertEqual(executor.calls, [first, second])
        self.assertEqual(backend.requests[0], (1, True, ()))
        self.assertEqual(backend.requests[1][2],
                         (ToolObservation(first, "done"), ToolObservation(second, "done")))

    async def test_final_request_disables_tools(self):
        call = AgentToolCall("1", "read", "{}")
        backend = Backend((ThoughtStep((call,)), ThoughtStep(result="ok")))
        await AgenticLoop(max_tool_rounds=1).run(backend=backend, executor=Executor())
        self.assertEqual([item[1] for item in backend.requests], [True, False])

    async def test_invalid_setup_and_backends(self):
        with self.assertRaises(ValueError):
            AgenticLoop(max_tool_rounds=0)
        cases = (
            (Backend((None,)), Executor(), TypeError),
            (Backend((ThoughtStep(),)), Executor(), RuntimeError),
            (Backend((ThoughtStep((AgentToolCall("1", "x", "{}"),)),)), None, RuntimeError),
            (Backend((ThoughtStep((AgentToolCall("1", "x", "{}"),)),)),
             Executor(invalid=True), TypeError),
        )
        for backend, executor, error in cases:
            with self.subTest(error=error), self.assertRaises(error):
                await AgenticLoop(max_tool_rounds=1).run(backend=backend, executor=executor)

    async def test_round_limit_rejects_continued_calls(self):
        call = AgentToolCall("1", "read", "{}")
        backend = Backend((ThoughtStep((call,)), ThoughtStep((call,))))
        with self.assertRaisesRegex(RuntimeError, "round limit"):
            await AgenticLoop(max_tool_rounds=1).run(backend=backend, executor=Executor())

    async def test_mismatched_observation_is_rejected(self):
        call = AgentToolCall("1", "read", "{}")

        class WrongExecutor:
            async def execute(self, call):
                return ToolObservation(AgentToolCall("other", call.name, call.arguments), "bad")

        with self.assertRaisesRegex(TypeError, "invalid observation"):
            await AgenticLoop(max_tool_rounds=1).run(
                backend=Backend((ThoughtStep((call,)),)), executor=WrongExecutor(),
            )
