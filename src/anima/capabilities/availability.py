"""Request-scoped capability availability shared by tools and commands."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable


class Availability(str, Enum):
    AVAILABLE = "available"
    DISABLED = "disabled"
    UNSUPPORTED = "unsupported"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class AvailabilityStatus:
    state: Availability
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, Availability):
            raise TypeError("availability state must be Availability")
        if self.reason is not None and (not self.reason.strip() or len(self.reason) > 200):
            raise ValueError("availability reason is invalid")


@runtime_checkable
class AvailabilityProvider(Protocol):
    async def capability_availability(
        self, kind: str, name: str, context: object,
    ) -> AvailabilityStatus: ...
