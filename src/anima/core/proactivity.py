"""Bounded ambient conversation decisions, independent of emoji plugins."""

import asyncio
from datetime import timedelta

from anima.core.telemetry import emit


class ProactiveDecisionEngine:
    def __init__(self, store, classifier, *, decision_limit=100, daily_limit=4,
                 cooldown=1800, timeout=15, bot_loop_window=600, bot_loop_max_speaks=2):
        if min(decision_limit, daily_limit, cooldown, timeout, bot_loop_window, bot_loop_max_speaks) <= 0:
            raise ValueError("proactive limits must be positive")
        self.store, self.classifier = store, classifier
        self.decision_limit, self.daily_limit, self.cooldown = decision_limit, daily_limit, cooldown
        self.timeout, self.bot_loop_window, self.bot_loop_max_speaks = timeout, bot_loop_window, bot_loop_max_speaks

    async def process(self, batch, *, now, allow_speak=False):
        if not batch.events or not allow_speak:
            return None
        for event in batch.events:
            self.store._check_event(event)
            if event.is_private or event.author_id == "self" or (event.directed_to_agent and not event.author_is_bot):
                raise ValueError("proactive candidate must be ambient guild speech")
            if event.channel_id != batch.events[0].channel_id:
                raise ValueError("proactive batch spans channels")
        events = tuple(event for event in batch.events if timedelta(0) <= now - event.ts <= timedelta(seconds=60))
        if not events:
            return None
        path = self.store.root / "runtime" / "proactivity.json"
        state = self.store._read_json(path, {})
        day = now.date().isoformat()
        if state.get("day") != day:
            state = {"day": day, "calls": 0, "speaks": 0, "last_spoke_at": 0, "targets": [], "bot_speaks": {}}
        channel = events[0].channel_id
        bot_times = [value for value in state["bot_speaks"].get(channel, []) if now.timestamp() - value < self.bot_loop_window]
        if (state["calls"] >= self.decision_limit or state["speaks"] >= self.daily_limit
                or now.timestamp() - state["last_spoke_at"] < self.cooldown
                or (any(event.author_is_bot for event in events) and len(bot_times) >= self.bot_loop_max_speaks)):
            emit("proactive.skipped", reason="rate_limit", channel_id=channel)
            return None
        # Reserve before calling the model: failures and restarts cannot reset the budget.
        state["calls"] += 1
        self.store._atomic_write_json(path, state)
        async with asyncio.timeout(self.timeout):
            value = await self.classifier.classify(self.store.load_snapshot(events[-1]), events, (), allow_react=False, allow_speak=True)
        action, target, face = value["action"], value["target"], value["face"]
        if action == "none" and target is None and face is None:
            emit("proactive.decided", action="none", channel_id=channel)
            return None
        candidates = {event.id: event for event in events}
        if action != "speak" or target not in candidates or face is not None:
            raise ValueError("invalid proactive decision")
        if target in state["targets"]:
            return None
        state["targets"].append(target)
        state["speaks"] += 1
        state["last_spoke_at"] = now.timestamp()
        if candidates[target].author_is_bot:
            state["bot_speaks"][channel] = [*bot_times, now.timestamp()]
        self.store._atomic_write_json(path, state)
        emit("proactive.decided", action="speak", target=target, channel_id=channel)
        return candidates[target]
