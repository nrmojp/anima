"""Responses implementation of the same decision contract, without fake probabilities."""
import json
import time
from anima.core.model_contracts import DecisionAnswer, DecisionResult
from anima.adapters.openai.client import OpenAIMemoryMaintainer
from anima.adapters.openai.model_errors import model_errors
from anima.core.prompts import ADDRESS, REACT


class OpenAIResponseDecisionBackend:
    probabilistic = False
    max_choices = 255

    def __init__(self, *, client, model):
        self.client, self.model = client, model

    @model_errors
    async def evaluate(self, request):
        started = time.monotonic()
        properties = {}
        for q in request.questions:
            properties[q.name] = ({"type": "boolean"} if q.kind == "predicate" else
                {"type": "string", "enum": list(q.choices)} if q.kind == "choice" else
                {"type": "integer", "minimum": 0, "maximum": len(q.choices)-1})
        response = await self.client.responses.create(model=self.model, store=False,
            reasoning={"effort": "none"}, input=request.evidence,
            instructions="\n".join(f"{q.name}: {q.instructions}" for q in request.questions),
            max_output_tokens=256,
            text={"format": {"type": "json_schema", "name": "anima_decision", "strict": True,
                "schema": {"type": "object", "properties": properties,
                    "required": list(properties), "additionalProperties": False}}})
        if any(getattr(part, "type", None) == "refusal" for item in getattr(response, "output", ())
               for part in getattr(item, "content", ())):
            answers = tuple(DecisionAnswer(q.name, q.kind, status="refused") for q in request.questions)
        else:
            data = json.loads(response.output_text)
            if set(data) != set(properties):
                raise ValueError("invalid decision response fields")
            answers = tuple(DecisionAnswer(q.name, q.kind, data[q.name]) for q in request.questions)
        result = DecisionResult(answers, self.model)
        result.validate(request)
        OpenAIMemoryMaintainer._emit_usage(response, request.purpose, started,
            ADDRESS if request.purpose == "address" else REACT, self.model)
        return result
