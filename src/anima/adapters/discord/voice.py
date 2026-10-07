"""Discord connection and WAV routing for the shared sandbox mixer."""

from __future__ import annotations

import asyncio
import audioop
from collections.abc import Callable
import io
from pathlib import Path
import wave

import discord

from anima.adapters.audio import DiscordAudioMixer
from anima.core.audio_ports import AudioInput, AudioLane, PCMSource
from anima.core.sandbox import SandboxKey


class WavPCMAudio(discord.AudioSource):
    """Convert a small PCM WAV file to Discord's 48 kHz stereo PCM frames."""

    FRAME_BYTES = 3_840

    def __init__(self, path: str) -> None:
        self._stream = io.BytesIO()
        with wave.open(path, "rb") as source:
            channels = source.getnchannels()
            width = source.getsampwidth()
            rate = source.getframerate()
            frames = source.readframes(source.getnframes())
        if width != 2 or channels not in {1, 2}:
            raise ValueError("voice WAV must contain 16-bit mono or stereo PCM")
        if channels == 1:
            frames = audioop.tostereo(frames, width, 1, 1)
            channels = 2
        if rate != 48_000:
            frames, _state = audioop.ratecv(frames, width, channels, rate, 48_000, None)
        self._stream = io.BytesIO(frames)

    def read(self) -> bytes:
        frame = self._stream.read(self.FRAME_BYTES)
        if frame and len(frame) < self.FRAME_BYTES:
            frame += bytes(self.FRAME_BYTES - len(frame))
        return frame

    def is_opus(self) -> bool:
        return False

    def cleanup(self) -> None:
        self._stream.close()


class DiscordVoiceOutput:
    def __init__(
        self, client: object, sandbox_key: SandboxKey,
        source_factory: Callable[[str], object],
        mixer: DiscordAudioMixer | None = None,
    ) -> None:
        self.client = client
        self.sandbox_key = sandbox_key
        self.source_factory = source_factory
        self.mixer = mixer or DiscordAudioMixer()
        self._lock = asyncio.Lock()
        self._voice_client: object | None = None

    async def start(self) -> None:
        return None

    async def play_wav(
        self, path: Path, actor_id: str, *, lane: AudioLane = AudioLane.SPEECH
    ) -> None:
        await self.play_pcm(self.source_factory(str(path)), actor_id, lane=lane)

    async def play_pcm(
        self, source: PCMSource, actor_id: str, *, lane: AudioLane
    ) -> None:
        if self.sandbox_key.kind != "guild":
            source.cleanup()
            raise RuntimeError("voice playback requires a Discord guild")
        guild = self.client.get_guild(int(self.sandbox_key.identifier))
        member = guild.get_member(int(actor_id)) if guild is not None else None
        channel = getattr(getattr(member, "voice", None), "channel", None)
        if channel is None:
            source.cleanup()
            raise RuntimeError("requesting member is not in a voice channel")

        loop = asyncio.get_running_loop()
        completed = loop.create_future()

        def after(error: Exception | None) -> None:
            def finish() -> None:
                if completed.done():
                    return
                if error is None:
                    completed.set_result(None)
                else:
                    completed.set_exception(error)
            loop.call_soon_threadsafe(finish)

        submitted = False
        try:
            async with self._lock:
                voice_client = getattr(guild, "voice_client", None)
                if voice_client is None or not voice_client.is_connected():
                    voice_client = await channel.connect()
                elif voice_client.channel != channel:
                    await voice_client.move_to(channel)
                self._voice_client = voice_client
                self.mixer.attach_output(voice_client)
                submitted = True
                self.mixer.play_input(AudioInput(lane, source, after))
        except BaseException:
            if not submitted:
                source.cleanup()
            raise
        await completed

    async def stop(self, lane: AudioLane | None = None) -> None:
        if lane is not None:
            self.mixer.stop_input(lane)
            return
        voice_client, self._voice_client = self._voice_client, None
        self.mixer.detach_output()
        if voice_client is not None and voice_client.is_connected():
            voice_client.stop()
            await voice_client.disconnect(force=True)

    def set_lane_volume(self, lane: AudioLane, volume: float) -> float:
        return self.mixer.set_lane_volume(lane, volume)

    def set_ducking_volume(self, volume: float) -> float:
        return self.mixer.set_ducking_volume(volume)
