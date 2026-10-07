"""Small, explicit levers for finite attention without reducing reasoning quality."""

from __future__ import annotations

from dataclasses import dataclass

from anima.core.models import Event


@dataclass(frozen=True, slots=True)
class AttentionPolicy:
    normal_window: int = 30
    focused_window: int = 12
    called_window: int = 20
    return_delay_seconds: float = 0.75

    def __post_init__(self) -> None:
        if min(self.normal_window, self.focused_window, self.called_window) <= 0:
            raise ValueError("attention windows must be positive")
        if self.focused_window > self.called_window or self.called_window > self.normal_window:
            raise ValueError("attention windows must be focused <= called <= normal")
        if self.return_delay_seconds < 0:
            raise ValueError("attention delay must not be negative")

    def select(self, events: tuple[Event, ...], source: Event | None, *, occupied: bool) -> tuple[Event, ...]:
        if not occupied:
            limit = self.normal_window
        elif source is not None and source.requires_immediate_response:
            limit = self.called_window
        else:
            limit = self.focused_window
        if source is None:
            return events[-limit:]
        available = [e for e in events if e.channel_id == source.channel_id
                     and e.sandbox_key == source.sandbox_key and e.guild_id == source.guild_id]
        if not any(e.id == source.id for e in available):
            available.append(source)
        by_id = {e.id: e for e in available}
        chosen = {source.id}
        current = source
        for _ in range(3):
            parent = current.response_to or current.reply_to
            if parent not in by_id or parent in chosen or len(chosen) >= limit:
                break
            chosen.add(parent)
            current = by_id[parent]
        for event in reversed(available):
            if len(chosen) >= limit:
                break
            chosen.add(event.id)
        return tuple(e for e in available if e.id in chosen)

    def delay(self, source: Event | None, *, occupied: bool) -> float:
        if occupied and source is not None and source.requires_immediate_response:
            return self.return_delay_seconds
        return 0.0
