"""SDK-independent conversation and bounded decision contracts."""
from dataclasses import dataclass
from typing import Protocol, Any
import math

from .agentic_loop import ThoughtBackend


class ModelBackendError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__("model backend failure: " + reason)


@dataclass(frozen=True)
class ConversationRequest:
    instructions: str
    messages: tuple[dict, ...]
    tools: tuple[dict, ...] = ()
    output_schema: dict | None = None


@dataclass(frozen=True)
class ConversationResult:
    text: str
    value: Any = None
    refused: bool = False
    input_tokens: int | None = None
    output_tokens: int | None = None


class ConversationServiceFactory(Protocol):
    """Domain integration around conversation sessions, injected by the host."""
    def responder(self, settings, **context): ...
    def maintainer(self, settings): ...
    def self_time(self, settings, *, client, **context): ...


class ConversationBackend(Protocol):
    """Open a request-local session; the existing AgenticLoop drives its turns."""
    def open(self, request: ConversationRequest) -> ThoughtBackend: ...


@dataclass(frozen=True)
class DecisionQuestion:
    name: str
    instructions: str
    kind: str = "predicate"
    choices: tuple[str, ...] = ()

    def __post_init__(self):
        if not self.name or self.kind not in {"predicate", "choice", "score"}:
            raise ValueError("invalid decision question")
        if self.kind != "predicate" and (len(self.choices) < 2 or
                len(set(self.choices)) != len(self.choices) or not all(self.choices)):
            raise ValueError("decision choices must be unique and nonempty")
        if self.kind == "predicate" and self.choices:
            raise ValueError("predicate cannot have choices")


@dataclass(frozen=True)
class DecisionRequest:
    evidence: str
    questions: tuple[DecisionQuestion, ...]
    purpose: str = "decision"

    def __post_init__(self):
        if not self.questions or len({q.name for q in self.questions}) != len(self.questions):
            raise ValueError("decision question names must be unique")


@dataclass(frozen=True)
class DecisionAnswer:
    name: str
    kind: str
    value: bool | str | float | None = None
    status: str = "answered"
    probability: float | None = None
    probabilities: tuple[tuple[str, float], ...] = ()
    confidence: float | None = None

    def validate(self, question):
        if self.name != question.name or self.kind != question.kind or self.status not in {
                "answered", "refused", "unavailable"}:
            raise ValueError("decision answer does not match its question")
        if self.status != "answered":
            if self.value is not None or self.probability is not None or self.probabilities or self.confidence is not None:
                raise ValueError("non-answer cannot carry judgement")
            return
        for signal in (self.probability, self.confidence, *(p for _, p in self.probabilities)):
            if signal is not None and (type(signal) not in {int, float} or
                    not math.isfinite(signal) or not 0 <= signal <= 1):
                raise ValueError("invalid decision probability")
        if self.kind == "predicate":
            if type(self.value) is not bool or self.probabilities:
                raise ValueError("predicate must return boolean")
        elif self.kind == "choice":
            if self.value not in question.choices or self.probability is not None:
                raise ValueError("invalid decision choice")
        elif type(self.value) not in {int, float} or not math.isfinite(self.value) or not 0 <= self.value <= len(question.choices)-1:
            raise ValueError("invalid decision score")
        if self.probabilities:
            if (len(self.probabilities) != len(question.choices) or
                    {v for v, _ in self.probabilities} != set(question.choices) or
                    abs(sum(p for _, p in self.probabilities)-1) > 0.02):
                raise ValueError("invalid decision distribution")


@dataclass(frozen=True)
class DecisionResult:
    answers: tuple[DecisionAnswer, ...]
    model: str = ""
    input_tokens: int | None = None

    def validate(self, request):
        if self.input_tokens is not None and (type(self.input_tokens) is not int or self.input_tokens < 0):
            raise ValueError("invalid decision usage")
        if len(self.answers) != len(request.questions) or {a.name for a in self.answers} != {q.name for q in request.questions}:
            raise ValueError("missing or duplicate decision answers")
        indexed = {a.name: a for a in self.answers}
        for question in request.questions:
            indexed[question.name].validate(question)
        return indexed


class DecisionBackend(Protocol):
    probabilistic: bool
    max_choices: int
    async def evaluate(self, request: DecisionRequest) -> DecisionResult: ...
