"""A single Discord PCM stream that mixes music and synthesized speech."""

from __future__ import annotations

from array import array
from dataclasses import dataclass
import threading

import discord

from anima.core.audio_ports import AfterCallback, AudioInput, AudioLane, AudioStreamOutput, PCMSource


PCM_FRAME_BYTES = 3840  # 20 ms, 48 kHz, stereo, signed 16-bit PCM
@dataclass(slots=True)
class _Slot:
    source: PCMSource
    after: AfterCallback


class MixingAudioSource(discord.AudioSource):
    """Thread-safe two-channel mixer with automatic music ducking."""

    def __init__(self, *, music_volume: float = 0.55, ducking_volume: float = 0.16,
                 speech_volume: float = 1.0, effect_volume: float = 1.0) -> None:
        self.music_volume = max(0.0, min(1.0, music_volume))
        self.ducking_volume = max(0.0, min(1.0, ducking_volume))
        self.speech_volume = max(0.0, min(1.0, speech_volume))
        self.effect_volume = max(0.0, min(1.0, effect_volume))
        self._music: _Slot | None = None
        self._speech: _Slot | None = None
        self._effect: _Slot | None = None
        self._lock = threading.RLock()
        self._closed = False

    def is_opus(self) -> bool:
        return False

    def read(self) -> bytes:
        callbacks: list[tuple[AfterCallback, Exception | None]] = []
        with self._lock:
            if self._closed:
                return b""
            speech = self._read_slot("_speech", callbacks)
            effect = self._read_slot("_effect", callbacks)
            music = self._read_slot("_music", callbacks)
            if music:
                volume = self.ducking_volume if speech else self.music_volume
                music = _scale(_frame(music), volume)
            if speech:
                speech = _scale(_frame(speech), self.speech_volume)
            if effect:
                effect = _scale(_frame(effect), self.effect_volume)
            output = bytes(PCM_FRAME_BYTES)
            for data in (music, effect, speech):
                if data:
                    output = _mix(output, data)
        for callback, error in callbacks:
            callback(error)
        return output

    def play_input(self, value: AudioInput) -> None:
        self._replace(_lane_attribute(value.lane), value.source, value.after)

    def stop_input(self, lane: AudioLane) -> None:
        self._stop(_lane_attribute(lane))

    def input_active(self, lane: AudioLane) -> bool:
        with self._lock:
            return getattr(self, _lane_attribute(lane)) is not None

    def set_music_levels(self, normal: float, ducked: float) -> None:
        with self._lock:
            self.music_volume = max(0.0, min(1.0, normal))
            self.ducking_volume = max(0.0, min(1.0, ducked))

    def set_lane_volume(self, lane: AudioLane, volume: float) -> float:
        value = max(0.0, min(1.0, volume))
        attribute = {AudioLane.MUSIC: "music_volume", AudioLane.SPEECH: "speech_volume", AudioLane.EFFECT: "effect_volume"}[lane]
        with self._lock:
            setattr(self, attribute, value)
        return value

    def set_ducking_volume(self, volume: float) -> float:
        with self._lock:
            self.ducking_volume = max(0.0, min(1.0, volume))
            return self.ducking_volume

    @property
    def music_active(self) -> bool:
        with self._lock:
            return self._music is not None

    @property
    def speech_active(self) -> bool:
        with self._lock:
            return self._speech is not None

    def cleanup(self) -> None:
        with self._lock:
            self._closed = True
            slots = (self._music, self._speech, self._effect)
            self._music = None
            self._speech = None
            self._effect = None
        for slot in slots:
            if slot:
                slot.source.cleanup()
                slot.after(None)

    def _replace(
        self, attribute: str, source: PCMSource, after: AfterCallback
    ) -> None:
        self._stop(attribute)
        with self._lock:
            if self._closed:
                source.cleanup()
                raise RuntimeError("audio mixer is closed")
            setattr(self, attribute, _Slot(source, after))

    def _stop(self, attribute: str) -> None:
        with self._lock:
            slot = getattr(self, attribute)
            setattr(self, attribute, None)
        if slot:
            slot.source.cleanup()
            slot.after(None)

    def _read_slot(
        self,
        attribute: str,
        callbacks: list[tuple[AfterCallback, Exception | None]],
    ) -> bytes:
        slot: _Slot | None = getattr(self, attribute)
        if slot is None:
            return b""
        try:
            data = slot.source.read()
        except Exception as error:
            setattr(self, attribute, None)
            slot.source.cleanup()
            callbacks.append((slot.after, error))
            return b""
        if data:
            return data
        setattr(self, attribute, None)
        slot.source.cleanup()
        callbacks.append((slot.after, None))
        return b""


class DiscordAudioMixer:
    """Owns the one long-lived AudioSource played by a Discord connection."""

    def __init__(self, *, music_volume: float = 0.55, ducking_volume: float = 0.16,
                 speech_volume: float = 1.0, effect_volume: float = 1.0) -> None:
        self.music_volume = max(0.0, min(1.0, music_volume))
        self.ducking_volume = max(0.0, min(1.0, ducking_volume))
        self.speech_volume = max(0.0, min(1.0, speech_volume))
        self.effect_volume = max(0.0, min(1.0, effect_volume))
        self._ducking_ratio = self.ducking_volume / self.music_volume if self.music_volume else 0.0
        self.source = MixingAudioSource(
            music_volume=music_volume, ducking_volume=ducking_volume,
            speech_volume=speech_volume, effect_volume=effect_volume,
        )
        self.connection: discord.VoiceClient | None = None

    def attach_output(self, connection: AudioStreamOutput) -> MixingAudioSource:
        if self.connection is connection and connection.is_playing():
            return self.source
        if self.connection is not connection and connection.is_playing():
            raise RuntimeError("audio output is already playing another source")
        if self.connection is not connection:
            self.source.cleanup()
            self.source = MixingAudioSource(
                music_volume=self.music_volume,
                ducking_volume=self.ducking_volume,
                speech_volume=self.speech_volume, effect_volume=self.effect_volume,
            )
            self.connection = connection
        if connection.is_playing():
            raise RuntimeError("Discord voice connection is already playing another source")
        connection.play(self.source)
        return self.source

    def detach_output(self) -> None:
        self.source.cleanup()
        self.connection = None

    def play_input(self, value: AudioInput) -> None:
        self.source.play_input(value)

    def stop_input(self, lane: AudioLane) -> None:
        self.source.stop_input(lane)

    def input_active(self, lane: AudioLane) -> bool:
        return self.source.input_active(lane)

    def set_music_volume(self, volume: float) -> float:
        self.music_volume = max(0.0, min(1.0, volume))
        self.ducking_volume = self.music_volume * self._ducking_ratio
        self.source.set_music_levels(self.music_volume, self.ducking_volume)
        return self.music_volume

    def set_lane_volume(self, lane: AudioLane, volume: float) -> float:
        if lane is AudioLane.MUSIC:
            return self.set_music_volume(volume)
        result = self.source.set_lane_volume(lane, volume)
        if lane is AudioLane.SPEECH:
            self.speech_volume = result
        else:
            self.effect_volume = result
        return result

    def set_ducking_volume(self, volume: float) -> float:
        self.ducking_volume = self.source.set_ducking_volume(volume)
        self._ducking_ratio = self.ducking_volume / self.music_volume if self.music_volume else 0.0
        return self.ducking_volume


def _frame(data: bytes) -> bytes:
    if len(data) >= PCM_FRAME_BYTES:
        return data[:PCM_FRAME_BYTES]
    return data + bytes(PCM_FRAME_BYTES - len(data))


def _lane_attribute(lane: AudioLane) -> str:
    return {
        AudioLane.MUSIC: "_music",
        AudioLane.SPEECH: "_speech",
        AudioLane.EFFECT: "_effect",
    }[lane]


def _samples(data: bytes) -> array[int]:
    values = array("h")
    values.frombytes(data)
    return values


def _scale(data: bytes, volume: float) -> bytes:
    values = _samples(data)
    for index, value in enumerate(values):
        values[index] = max(-32768, min(32767, round(value * volume)))
    return values.tobytes()


def _mix(left: bytes, right: bytes) -> bytes:
    left_values = _samples(left)
    right_values = _samples(right)
    for index, value in enumerate(right_values):
        left_values[index] = max(-32768, min(32767, left_values[index] + value))
    return left_values.tobytes()
