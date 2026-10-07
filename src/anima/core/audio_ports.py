"""Transport-neutral contracts for routed audio and mixed PCM output."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable


AfterCallback = Callable[[Exception | None], None]


class AudioLane(str, Enum):
    """Separately controllable lanes in one sandbox audio mixer."""

    MUSIC = "music"
    SPEECH = "speech"
    EFFECT = "effect"


class PCMSource(Protocol):
    """A 48 kHz stereo signed-16-bit PCM producer."""

    def read(self) -> bytes: ...
    def cleanup(self) -> None: ...


@dataclass(frozen=True, slots=True)
class AudioInput:
    """One source submitted by a capability to a mixer lane."""

    lane: AudioLane
    source: PCMSource
    after: AfterCallback

    def __post_init__(self) -> None:
        if not isinstance(self.lane, AudioLane):
            raise TypeError("audio input lane is invalid")
        if not callable(getattr(self.source, "read", None)):
            raise TypeError("audio input source must provide read")
        if not callable(getattr(self.source, "cleanup", None)):
            raise TypeError("audio input source must provide cleanup")
        if not callable(self.after):
            raise TypeError("audio input callback must be callable")


class AudioStreamOutput(Protocol):
    """A connected transport capable of consuming one continuous PCM stream."""

    def is_playing(self) -> bool: ...
    def play(self, source: PCMSource) -> None: ...


class AudioMixerPort(Protocol):
    """Shared mixer boundary used by audio-capable adapters."""

    music_volume: float
    ducking_volume: float

    def attach_output(self, output: AudioStreamOutput) -> PCMSource: ...
    def detach_output(self) -> None: ...
    def play_input(self, value: AudioInput) -> None: ...
    def stop_input(self, lane: AudioLane) -> None: ...
    def input_active(self, lane: AudioLane) -> bool: ...
    def set_music_volume(self, volume: float) -> float: ...
    def set_lane_volume(self, lane: AudioLane, volume: float) -> float: ...
    def set_ducking_volume(self, volume: float) -> float: ...


@runtime_checkable
class AudioOutput(Protocol):
    """Route WAV audio from a plugin to a sandbox's shared output mixer."""

    async def play_pcm(
        self, source: PCMSource, actor_id: str, *, lane: AudioLane
    ) -> None: ...
    async def play_wav(
        self, path: Path, actor_id: str, *, lane: AudioLane = AudioLane.SPEECH
    ) -> None: ...
    async def stop(self, lane: AudioLane | None = None) -> None: ...
    def set_lane_volume(self, lane: AudioLane, volume: float) -> float: ...
    def set_ducking_volume(self, volume: float) -> float: ...
