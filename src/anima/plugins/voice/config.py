"""Configuration values for the local voice plugin."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class VoiceSettings:
    enabled: bool = True
    voice: str = "ja"
    speed: int = 175
    volume: int = 100
    maximum_characters: int = 500
    timeout_seconds: float = 20.0

    def __post_init__(self) -> None:
        if not self.voice.strip() or len(self.voice) > 40:
            raise ValueError("voice name is invalid")
        if not 80 <= self.speed <= 450:
            raise ValueError("voice speed is invalid")
        if not 0 <= self.volume <= 200:
            raise ValueError("voice volume is invalid")
        if not 1 <= self.maximum_characters <= 2_000:
            raise ValueError("voice character limit is invalid")
        if not 1 <= self.timeout_seconds <= 120:
            raise ValueError("voice timeout is invalid")
