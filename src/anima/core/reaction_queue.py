"""Core batching primitives for optional reaction capabilities."""

import asyncio
from dataclasses import dataclass

from .models import Event
from .telemetry import emit


@dataclass(frozen=True)
class ReactionBatch:
    events: tuple[Event, ...]


class ReactionBuffer:
    """Fixed debounce window: continuous traffic cannot postpone a batch forever."""

    def __init__(self, enqueue, *, delay=5):
        self.enqueue, self.delay = enqueue, delay
        self.pending, self.tasks = {}, {}

    def add(self, event):
        channel = event.channel_id
        if channel not in self.pending and len(self.pending) >= 64:
            return
        items = self.pending.setdefault(channel, [])
        items.append(event)
        del items[:-20]
        if channel not in self.tasks:
            self.tasks[channel] = asyncio.create_task(self._flush(channel))

    async def _flush(self, channel):
        await asyncio.sleep(self.delay)
        batch = ReactionBatch(tuple(self.pending.pop(channel)))
        self.tasks.pop(channel)
        try:
            self.enqueue(batch)
        except asyncio.QueueFull:
            emit("reaction.skipped", reason="queue_full", channel_id=channel)

    async def stop(self):
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        self.tasks.clear()
        self.pending.clear()
