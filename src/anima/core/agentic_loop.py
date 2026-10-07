"""Provider-neutral iterative thought and tool execution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class AgentToolCall:
    id: str
    name: str
    arguments: str
    round: int = 0


@dataclass(frozen=True, slots=True)
class ToolObservation:
    call: AgentToolCall
    output: str
    attachments: tuple[Path, ...] = ()
    value: object | None = None


@dataclass(frozen=True, slots=True)
class ThoughtStep:
    calls: tuple[AgentToolCall, ...] = ()
    result: object | None = None


@dataclass(frozen=True, slots=True)
class AgenticLoopResult:
    result: object
    request_count: int
    tool_call_count: int


class ThoughtBackend(Protocol):
    async def think(
        self, request_count: int, tools_enabled: bool,
        observations: tuple[ToolObservation, ...],
    ) -> ThoughtStep: ...


class AgentToolExecutor(Protocol):
    async def execute(self, call: AgentToolCall) -> ToolObservation: ...


class AgenticLoop:
    """Repeat thought and primitive tool execution without knowing an LLM API."""

    def __init__(self, *, max_tool_rounds: int) -> None:
        if max_tool_rounds < 1:
            raise ValueError("agentic tool round limit must be positive")
        self.max_tool_rounds = max_tool_rounds

    async def run(
        self, *, backend: ThoughtBackend,
        executor: AgentToolExecutor | None,
    ) -> AgenticLoopResult:
        observations: tuple[ToolObservation, ...] = ()
        tool_call_count = 0
        for round_index in range(self.max_tool_rounds + 1):
            request_count = round_index + 1
            step = await backend.think(
                request_count, round_index < self.max_tool_rounds, observations,
            )
            if not isinstance(step, ThoughtStep):
                raise TypeError("thought backend returned an invalid step")
            if not step.calls:
                if step.result is None:
                    raise RuntimeError("thought backend returned no result")
                return AgenticLoopResult(step.result, request_count, tool_call_count)
            if executor is None:
                raise RuntimeError(f"no handler for tool {step.calls[0].name}")
            tool_call_count += len(step.calls)
            values = []
            for call in step.calls:
                observation = await executor.execute(call)
                if not isinstance(observation, ToolObservation) or observation.call != call:
                    raise TypeError("tool executor returned an invalid observation")
                values.append(observation)
            observations = tuple(values)
        raise RuntimeError("agentic tool loop exceeded its round limit")
