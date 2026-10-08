"""Request-local Responses sessions for the provider-neutral agentic loop."""
from dataclasses import dataclass
from typing import Callable, Any
import json
from anima.core.model_contracts import ConversationRequest, ConversationResult
from anima.core.agentic_loop import ThoughtStep
from anima.capabilities.contracts import _validate_schema, _matches_schema
from anima.adapters.openai.client import OpenAIThoughtBackend


@dataclass(frozen=True)
class OpenAIConversationRequest:
    base_input: Any
    tools: list
    request: Callable
    observe: Callable | None = None


class OpenAIConversationBackend:
    def __init__(self, *, client=None, model=""):
        self.client, self.model = client, model

    def open(self, request):
        if isinstance(request, ConversationRequest):
            if self.client is None or not self.model:
                raise ValueError("conversation client and model are required")
            if request.output_schema is not None:
                _validate_schema(request.output_schema, root=True)
            async def send(number, items, tools):
                options = {"model": self.model, "instructions": request.instructions,
                    "input": items, "store": False}
                if tools:
                    options["tools"] = tools
                if request.output_schema is not None:
                    options["text"] = {"format": {"type": "json_schema", "name": "anima_result",
                        "strict": True, "schema": request.output_schema}}
                return await self.client.responses.create(**options)
            return _NeutralSession(OpenAIThoughtBackend(base_input=list(request.messages),
                tools=list(request.tools), request=send), schema=request.output_schema)
        return OpenAIThoughtBackend(base_input=request.base_input, tools=request.tools,
            request=request.request, observe=request.observe)


class _NeutralSession:
    def __init__(self, session, *, schema):
        self.session, self.schema = session, schema

    async def think(self, request_count, tools_enabled, observations):
        step = await self.session.think(request_count, tools_enabled, observations)
        if step.calls:
            return step
        response = step.result
        refused = any(getattr(part, "type", None) == "refusal"
            for item in getattr(response, "output", ()) for part in getattr(item, "content", ()))
        text = getattr(response, "output_text", "")
        usage = getattr(response, "usage", None)
        value = json.loads(text) if self.schema is not None and not refused else None
        if self.schema is not None and not refused and not _matches_schema(value, self.schema):
            raise ValueError("conversation result violates output schema")
        return ThoughtStep(result=ConversationResult(text,
            value, refused,
            getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None)))
