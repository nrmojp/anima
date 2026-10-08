"""Bounded opt-in comparison. Secondary judgements can never execute actions."""
import asyncio
from .telemetry import emit


class ShadowDecisionBackend:
    def __init__(self, primary, secondary, *, reserve, semaphore, timeout=15):
        self.primary, self.secondary = primary, secondary
        self.reserve, self.semaphore, self.timeout = reserve, semaphore, timeout
        self.probabilistic, self.max_choices = primary.probabilistic, primary.max_choices
        self.model = getattr(primary, "model", "")
        self.tasks = set()
        self.stopping = False

    async def start(self):
        self.stopping = False

    async def stop(self):
        self.stopping = True
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def evaluate(self, request):
        result = await self.primary.evaluate(request)
        result.validate(request)
        if not self.stopping and len(self.tasks) < 1:
            try:
                reserved = self.reserve()
            except Exception as error:
                emit("decision.shadow.failed", operation=request.purpose, error_type=type(error).__name__)
                reserved = False
            if reserved:
                task = asyncio.create_task(self._compare(request, result))
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)
        return result

    async def _compare(self, request, result):
        try:
            async with asyncio.timeout(self.timeout):
                async with self.semaphore:
                    other = await self.secondary.evaluate(request)
            first, second = result.validate(request), other.validate(request)
            emit("decision.shadow.completed", operation=request.purpose,
                disagreements=[name for name in first if
                    (first[name].status, first[name].value) != (second[name].status, second[name].value)])
        except Exception as error:
            emit("decision.shadow.failed", operation=request.purpose, error_type=type(error).__name__)
