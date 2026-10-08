"""Decisions endpoint adapter, compatible with pre-Decisions OpenAI SDKs."""
import time
from typing import Any
from anima.core.model_contracts import DecisionAnswer, DecisionResult
from anima.core.telemetry import emit
from anima.adapters.openai.model_errors import model_errors


class OpenAIDecisionBackend:
    probabilistic = True
    max_choices = 255

    def __init__(self, *, client, model="gpt-6-luna"):
        self.client, self.model = client, model

    @model_errors
    async def evaluate(self, request):
        questions = []
        for q in request.questions:
            item = {"name": q.name, "type": q.kind, "instructions": q.instructions}
            if q.kind == "choice":
                if len(q.choices) > self.max_choices:
                    raise ValueError("too many decision choices")
                item["choices"] = [{"value": v} for v in q.choices]
            if q.kind == "score":
                item["levels"] = [{"label": v} for v in q.choices]
            questions.append(item)
        started = time.monotonic()
        emit("openai.request.started", operation=request.purpose, model=self.model, backend="decisions")
        try:
            body = await self.client.post("/decisions", cast_to=dict[str, Any],
                body={"model": self.model, "input": request.evidence, "questions": questions})
            answers = []
            by_name = {q.name: q for q in request.questions}
            for raw in body["answers"]:
                q = by_name[raw["name"]]
                kind = raw["type"]
                if kind == "refusal":
                    answer = DecisionAnswer(q.name, q.kind, status="refused")
                elif kind == "predicate":
                    p = raw["probability"]
                    answer = DecisionAnswer(q.name, kind, p >= 0.5, probability=p)
                elif kind == "choice":
                    answer = DecisionAnswer(q.name, kind, raw["choice"], confidence=raw["confidence"],
                        probabilities=tuple((v["value"], v["probability"]) for v in raw["probabilities"]))
                elif kind == "score":
                    answer = DecisionAnswer(q.name, kind, raw["score"], confidence=raw["confidence"],
                        probabilities=tuple((v["label"], v["probability"]) for v in raw["probabilities"]))
                else:
                    raise ValueError("unknown decision answer type")
                answers.append(answer)
            usage = body.get("usage", {})
            result = DecisionResult(tuple(answers), body.get("model", self.model), usage.get("input_tokens"))
            result.validate(request)
        except Exception as error:
            emit("decision.failed", operation=request.purpose, error_type=type(error).__name__)
            raise
        emit("openai.response.completed", operation=request.purpose, model=result.model, backend="decisions",
             duration_ms=round((time.monotonic()-started)*1000),
             input_tokens=result.input_tokens, output_tokens=usage.get("output_tokens"),
             total_tokens=usage.get("total_tokens"), pricing_profile="decisions")
        return result
