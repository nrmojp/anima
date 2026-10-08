"""Address and engagement uses of the single bounded-decision contract."""
import json
from .model_contracts import DecisionRequest, DecisionQuestion
from .decision_policy import DecisionPolicy
from .prompts import ADDRESS, REACT
from .expressions import FACE_GUIDANCE
from .telemetry import emit


class AddressClassifier:
    def __init__(self, backend, *, policy=DecisionPolicy()):
        self.backend, self.policy = backend, policy
        self.model = getattr(backend, "model", "")
        if policy.threshold is not None and not backend.probabilistic:
            raise ValueError("address policy requires probabilities")

    async def expects_reply(self, snapshot, history, event):
        evidence = json.dumps({"persona": snapshot.persona[:2000], "target": event.id,
            "events": [{"id": e.id, "author_id": e.author_id, "name": e.author_name,
                "reply_to": e.reply_to, "response_to": e.response_to,
                "reply_author_name": e.reply_author_name, "reply_text": (e.reply_text or "")[:1000],
                "called_name": e.called_name, "text": e.text[:1000]} for e in (*history, event)]}, ensure_ascii=False)
        request = DecisionRequest(evidence, (DecisionQuestion("expects_reply", ADDRESS.instructions),), "address")
        answer = (await self.backend.evaluate(request)).validate(request)["expects_reply"]
        accepted = self.policy.accepts(answer) and answer.value is True
        emit("decision.adopted", operation="address", accepted=accepted, status=answer.status,
             probability=answer.probability)
        return accepted


class EngagementClassifier:
    def __init__(self, backend, *, speech_policy=DecisionPolicy(), reaction_policy=DecisionPolicy()):
        self.backend, self.speech_policy, self.reaction_policy = backend, speech_policy, reaction_policy
        self.model = getattr(backend, "model", "")
        if any(p.threshold is not None for p in (speech_policy, reaction_policy)) and not backend.probabilistic:
            raise ValueError("engagement policy requires probabilities")

    async def classify(self, snapshot, events, available_faces, *, allow_react=True, allow_speak=False):
        mapping = {"c0": {"action": "none", "target": None, "face": None}}
        for event in events[-20:]:
            if allow_speak:
                mapping[f"c{len(mapping)}"] = {"action": "speak", "target": event.id, "face": None}
            if allow_react:
                for face in dict.fromkeys(available_faces):
                    mapping[f"c{len(mapping)}"] = {"action": "react", "target": event.id, "face": face}
        if len(mapping) == 1:
            return mapping["c0"]
        if len(mapping) > self.backend.max_choices:
            raise ValueError("engagement candidates exceed backend limit")
        evidence = json.dumps({"persona": snapshot.persona[:4000], "habitus": snapshot.habitus[:2000],
            "mood": snapshot.mood.to_dict(), "candidates": mapping,
            "events": [{"id": e.id, "author_id": e.author_id, "who": e.author_name[:80],
                "bot": e.author_is_bot, "text": e.text[:500]} for e in events[-20:]]}, ensure_ascii=False)
        instructions = REACT.instructions + "\n" + FACE_GUIDANCE + "\nChoose one candidate ID."
        if allow_speak:
            instructions += "\n特に自然に会話へ加わる価値が高い場合だけspeakを選ぶ。"
        request = DecisionRequest(evidence, (DecisionQuestion("engagement", instructions, "choice", tuple(mapping)),), "react")
        answer = (await self.backend.evaluate(request)).validate(request)["engagement"]
        selected = mapping.get(answer.value, mapping["c0"])
        policy = self.speech_policy if selected["action"] == "speak" else self.reaction_policy
        accepted = policy.accepts(answer)
        emit("decision.adopted", operation="react", accepted=accepted, status=answer.status,
             choice=answer.value, confidence=answer.confidence, probabilities=answer.probabilities)
        return selected if accepted else mapping["c0"]
