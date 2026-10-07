import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
import wave

from anima.adapters.discord.voice import DiscordVoiceOutput, WavPCMAudio
from anima.core.audio_ports import AudioLane
from anima.core.sandbox import SandboxKey


class VoiceClient:
    def __init__(self, channel, *, connected=True):
        self.channel = channel
        self.connected = connected
        self.sources = []
        self.moved = []
        self.stopped = False
        self.disconnected = False
        self.playing = False

    def is_connected(self):
        return self.connected

    async def move_to(self, channel):
        self.moved.append(channel)
        self.channel = channel

    def is_playing(self):
        return self.playing

    def play(self, source):
        self.sources.append(source)
        self.playing = True
        loop = asyncio.get_running_loop()
        loop.call_soon(source.read)
        loop.call_soon(source.read)

    def stop(self):
        self.stopped = True
        self.playing = False

    async def disconnect(self, *, force):
        self.disconnected = force
        self.connected = False


class Channel:
    def __init__(self):
        self.client = VoiceClient(self)
        self.connects = 0

    async def connect(self):
        self.connects += 1
        return self.client


class BrokenChannel(Channel):
    async def connect(self):
        raise RuntimeError("connection failed")


class Guild:
    def __init__(self, channel, *, voice_client=None, member=True):
        self.voice_client = voice_client
        self._member = SimpleNamespace(voice=SimpleNamespace(channel=channel)) if member else None

    def get_member(self, actor_id):
        self.actor_id = actor_id
        return self._member


class Client:
    def __init__(self, guild):
        self.guild = guild

    def get_guild(self, guild_id):
        self.guild_id = guild_id
        return self.guild


class Source:
    def __init__(self, error=None):
        self.frames = [b"\0\0"]
        self.error = error
        self.cleaned = False

    def read(self):
        if self.error:
            raise self.error
        return self.frames.pop(0) if self.frames else b""

    def cleanup(self):
        self.cleaned = True


class DiscordVoiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_connect_play_and_stop(self):
        channel = Channel()
        guild = Guild(channel)
        output = DiscordVoiceOutput(
            Client(guild), SandboxKey("discord_guild", "1"), lambda path: Source()
        )
        await output.play_wav(Path("speech.wav"), "42", lane=AudioLane.EFFECT)
        self.assertEqual(channel.connects, 1)
        self.assertEqual(channel.client.sources, [output.mixer.source])
        await output.stop()
        self.assertTrue(channel.client.stopped)
        self.assertTrue(channel.client.disconnected)
        await output.stop()

    async def test_reuses_and_moves_connected_client(self):
        old_channel = Channel()
        channel = Channel()
        voice_client = VoiceClient(old_channel)
        guild = Guild(channel, voice_client=voice_client)
        output = DiscordVoiceOutput(Client(guild), SandboxKey("discord_guild", "1"), lambda _: Source())
        await output.play_wav(Path("speech.wav"), "42")
        self.assertEqual(voice_client.moved, [channel])
        self.assertEqual(channel.connects, 0)

    async def test_reconnects_disconnected_client(self):
        channel = Channel()
        old = VoiceClient(channel, connected=False)
        output = DiscordVoiceOutput(
            Client(Guild(channel, voice_client=old)), SandboxKey("discord_guild", "1"), lambda _: Source()
        )
        await output.play_wav(Path("speech.wav"), "42")
        self.assertEqual(channel.connects, 1)

    async def test_rejects_dm_and_member_without_channel(self):
        output = DiscordVoiceOutput(Client(None), SandboxKey("discord_dm", "1"), lambda _: Source())
        with self.assertRaisesRegex(RuntimeError, "requires a Discord guild"):
            await output.play_wav(Path("speech.wav"), "42")
        output = DiscordVoiceOutput(
            Client(Guild(None, member=False)), SandboxKey("discord_guild", "1"), lambda _: Source()
        )
        with self.assertRaisesRegex(RuntimeError, "not in a voice channel"):
            await output.play_wav(Path("speech.wav"), "42")

    async def test_propagates_playback_error(self):
        channel = Channel()
        output = DiscordVoiceOutput(
            Client(Guild(channel)), SandboxKey("discord_guild", "1"),
            lambda _: Source(RuntimeError("decoder failed")),
        )
        with self.assertRaisesRegex(RuntimeError, "decoder failed"):
            await output.play_wav(Path("speech.wav"), "42")

    async def test_cleans_unsubmitted_pcm_when_connection_fails(self):
        source = Source()
        output = DiscordVoiceOutput(
            Client(Guild(BrokenChannel())), SandboxKey("discord_guild", "1"), lambda _: source
        )
        with self.assertRaisesRegex(RuntimeError, "connection failed"):
            await output.play_pcm(source, "42", lane=AudioLane.MUSIC)
        self.assertTrue(source.cleaned)

    async def test_lane_stop_completes_once_and_volume_is_forwarded(self):
        channel = Channel()
        output = DiscordVoiceOutput(
            Client(Guild(channel)), SandboxKey("discord_guild", "1"), lambda _: Source()
        )
        task = asyncio.create_task(output.play_wav(Path("speech.wav"), "42"))
        await asyncio.sleep(0)
        await output.stop(AudioLane.SPEECH)
        await task
        await output.stop(AudioLane.SPEECH)
        self.assertEqual(output.set_lane_volume(AudioLane.MUSIC, 0.75), 0.75)
        self.assertEqual(output.set_ducking_volume(0.2), 0.2)


class WavPCMAudioTests(unittest.TestCase):
    def write_wav(self, path, *, channels=1, width=2, rate=22_050, frames=b"\x00\x00" * 10):
        with wave.open(str(path), "wb") as output:
            output.setnchannels(channels)
            output.setsampwidth(width)
            output.setframerate(rate)
            output.writeframes(frames)

    def test_converts_mono_and_pads_final_frame(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "speech.wav"
            self.write_wav(path)
            source = WavPCMAudio(str(path))
            self.assertEqual(len(source.read()), source.FRAME_BYTES)
            self.assertEqual(source.read(), b"")
            self.assertFalse(source.is_opus())
            source.cleanup()

    def test_accepts_native_stereo_rate(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "speech.wav"
            frames = b"\x01\x00\x01\x00" * 1_000
            self.write_wav(path, channels=2, rate=48_000, frames=frames)
            source = WavPCMAudio(str(path))
            self.assertEqual(source.read(), frames[:source.FRAME_BYTES])
            source.cleanup()

    def test_rejects_unsupported_wav(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "speech.wav"
            for channels, width in ((1, 1), (3, 2)):
                self.write_wav(path, channels=channels, width=width)
                with self.subTest(channels=channels, width=width), self.assertRaises(ValueError):
                    WavPCMAudio(str(path))


if __name__ == "__main__":
    unittest.main()
