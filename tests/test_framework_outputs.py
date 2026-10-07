from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from anima.adapters.discord.output import DiscordMessageOutput
from anima.adapters.openai.embeddings import OpenAIEmbeddingProvider
from anima.core.models import ResponseDraft
from anima.core.sandbox import SandboxKey


class FrameworkOutputTests(unittest.IsolatedAsyncioTestCase):
    async def test_embedding_results_follow_input_order(self):
        create = AsyncMock(return_value=SimpleNamespace(data=[
            SimpleNamespace(index=1, embedding=[0, 1]),
            SimpleNamespace(index=0, embedding=[1, 0]),
        ]))
        provider = OpenAIEmbeddingProvider(SimpleNamespace(embeddings=SimpleNamespace(create=create)), model="test", dimensions=2)
        self.assertEqual(await provider.embed(["a", "b"]), [[1, 0], [0, 1]])
        create.assert_awaited_once_with(model="test", input=["a", "b"], dimensions=2, encoding_format="float")

    async def test_output_destination_and_mentions_are_sandbox_bound(self):
        response = ResponseDraft("hello", "calm", "", "弱い", "")
        channel = SimpleNamespace(guild=SimpleNamespace(id=1), send=AsyncMock())
        client = SimpleNamespace(get_channel=lambda _: channel)
        output = DiscordMessageOutput(client, SandboxKey("guild", "1"))
        await output.send("100", response)
        self.assertNotIn("files", channel.send.call_args.kwargs)
        self.assertFalse(channel.send.call_args.kwargs["allowed_mentions"].everyone)
        from dataclasses import replace
        with patch("anima.adapters.discord.output.discord.File", side_effect=lambda path: path):
            await output.send("100", replace(response, images=(Path("one.png"),)))
        self.assertEqual(channel.send.call_args.kwargs["files"], [Path("one.png")])
        for identifier in ("bad", "１", "-1"):
            with self.assertRaises(ValueError):
                await output.send(identifier, response)
        channel.guild.id = 2
        with self.assertRaises(PermissionError):
            await output.send("100", response)
        client.get_channel = lambda _: None
        with self.assertRaises(RuntimeError):
            await output.send("100", response)
        output.sandbox_key = SandboxKey("matrix_room", "room")
        with self.assertRaises(RuntimeError):
            await output.send("100", response)
        dm = SimpleNamespace(recipient=SimpleNamespace(id=20), send=AsyncMock())
        client.get_channel = lambda _: dm
        output.sandbox_key = SandboxKey("dm", "20")
        await output.send("100", response)
        dm.recipient = None
        with self.assertRaises(PermissionError):
            await output.send("100", response)


class MixerLevelTests(unittest.TestCase):
    def test_level_setters_clip_and_lane_properties_are_live(self):
        from anima.adapters.audio.mixer import MixingAudioSource
        source = MixingAudioSource()
        self.assertFalse(source.music_active)
        self.assertFalse(source.speech_active)
        source._music = object()
        source._speech = object()
        self.assertTrue(source.music_active)
        self.assertTrue(source.speech_active)
        self.assertEqual(source.set_ducking_volume(2), 1)
        self.assertEqual(source.set_ducking_volume(-1), 0)
        # Avoid cleanup of deliberately minimal sentinel sources.
        source._music = source._speech = None
