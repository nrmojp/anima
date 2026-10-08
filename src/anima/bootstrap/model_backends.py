"""Independent conversation/decision composition, replaceable by host factories."""
from dataclasses import dataclass
from anima.core.decision_requests import AddressClassifier, EngagementClassifier
from anima.core.decision_policy import DecisionPolicy
from anima.adapters.openai.client import OpenAIResponder, OpenAIMemoryMaintainer, OpenAISelfTimeDecider
from anima.adapters.openai.decisions import OpenAIDecisionBackend
from anima.adapters.openai.response_decisions import OpenAIResponseDecisionBackend
from anima.core.decision_shadow import ShadowDecisionBackend
from datetime import datetime, timezone


@dataclass(frozen=True)
class OpenAIConversationFactory:
    """Default domain service factory. Hosts can supply another implementation."""
    responder_type: type = OpenAIResponder
    maintainer_type: type = OpenAIMemoryMaintainer
    self_time_type: type = OpenAISelfTimeDecider

    def responder(self, settings, **context):
        return self.responder_type(api_key=settings.openai_api_key, model=settings.openai_model,
            timeout_seconds=settings.openai_response_timeout_seconds, max_retries=0, **context)

    def maintainer(self, settings):
        return self.maintainer_type(api_key=settings.openai_api_key, model=settings.openai_model,
            reflection_model=settings.openai_reflection_model,
            timeout_seconds=settings.openai_maintenance_timeout_seconds, max_retries=0)

    def self_time(self, settings, *, client, **context):
        return self.self_time_type(client=client, model=settings.openai_model, **context)


def decision_classifiers(settings, client, *, decision_factory=None, store=None, semaphore=None):
    model = settings.decision_model or settings.openai_model
    backend = (decision_factory(settings, client) if decision_factory else
        OpenAIDecisionBackend(client=client, model=model) if settings.decision_backend == "decisions" else
        OpenAIResponseDecisionBackend(client=client, model=model))
    if settings.decision_shadow_backend:
        if store is None or semaphore is None:
            raise ValueError("shadow comparison requires scoped storage and limiter")
        secondary = (OpenAIDecisionBackend(client=client, model="gpt-6-luna")
            if settings.decision_shadow_backend == "decisions" else
            OpenAIResponseDecisionBackend(client=client, model=settings.openai_model))
        def reserve():
            path = store.root / "runtime" / "decision-shadow.json"
            day = datetime.now(timezone.utc).date().isoformat()
            state = store._read_json(path, {})
            count = state.get("attempts", 0) if state.get("day") == day else 0
            if count >= settings.decision_shadow_daily_limit:
                return False
            store._atomic_write_json(path, {"day": day, "attempts": count + 1})
            return True
        backend = ShadowDecisionBackend(backend, secondary, reserve=reserve, semaphore=semaphore)
    probabilistic = settings.decision_backend == "decisions"
    def policy(value):
        return DecisionPolicy(value if probabilistic else None)
    return (AddressClassifier(backend, policy=policy(settings.decision_address_threshold)),
        EngagementClassifier(backend, speech_policy=policy(settings.decision_speech_threshold),
            reaction_policy=policy(settings.decision_reaction_threshold)))
