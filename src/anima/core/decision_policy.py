"""Pure adoption policy; provider probabilities are estimates, not accuracy."""
import math
from dataclasses import dataclass


@dataclass(frozen=True)
class DecisionPolicy:
    threshold: float | None = None
    margin: float = 0.0

    def __post_init__(self):
        for value in (self.threshold, self.margin):
            if value is not None and (type(value) not in {int, float} or not math.isfinite(value) or not 0 <= value <= 1):
                raise ValueError("invalid decision threshold")

    def accepts(self, answer):
        if answer.status != "answered":
            return False
        if self.threshold is None:
            return True
        if answer.kind == "predicate":
            return answer.value is True and answer.probability is not None and answer.probability >= self.threshold
        probabilities = dict(answer.probabilities)
        selected = probabilities.get(answer.value)
        if selected is None:
            return False
        other = max((p for value, p in answer.probabilities if value != answer.value), default=0)
        return selected >= self.threshold and selected - other >= self.margin
