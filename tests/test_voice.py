import asyncio
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from anima.capabilities.tools import CapabilityContext
from anima.adapters.storage import FilePluginStorage, FilePluginStorageFactory
from anima.core.models import Event
from anima.core.audio_ports import AudioLane, AudioOutput
from anima.core.sandbox import SandboxKey
from anima.core.services import SandboxServices
from anima.plugins.voice.config import VoiceSettings
from anima.plugins.voice.espeak import EspeakSynthesizer
from anima.plugins.voice.plugin import VoicePlugin, VoicePluginDefinition


class Process:
    def __init__(self, output, *, returncode=0, stderr=b"", timeout=False, create=True):
        self.output = output
        self.returncode = returncode
        self.stderr = stderr
        self.timeout = timeout
        self.create = create
        self.killed = False

    async def communicate(self):
        if self.timeout and not self.killed:
            raise TimeoutError
        if self.create and self.returncode == 0:
            self.output.write_bytes(b"RIFFaudio")
        return b"", self.stderr

    def kill(self):
        self.killed = True


class ProcessFactory:
    def __init__(self, **options):
        self.options = options
        self.arguments = None
        self.keywords = None
        self.process = None

    async def __call__(self, *arguments, **keywords):
        self.arguments = arguments
        self.keywords = keywords
        output = Path(arguments[arguments.index("-w") + 1])
        self.process = Process(output, **self.options)
        return self.process


class Output:
    def __init__(self, error=None):
        self.error = error
        self.played = []
        self.stopped = False

    async def play_wav(self, path, actor_id, *, lane=AudioLane.SPEECH):
        if self.error:
            raise self.error
        self.played.append((path.read_bytes(), actor_id, lane))

    async def play_pcm(self, source, actor_id, *, lane):
        self.played.append((source, actor_id, lane))

    async def stop(self, lane=None):
        self.stopped = lane

    def set_lane_volume(self, lane, volume):
        return volume

    def set_ducking_volume(self, volume):
        return volume


class Synthesizer:
    def __init__(self, error=None):
        self.error = error

    async def synthesize(self, text, output):
        if self.error:
            raise self.error
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(text.encode())


class VoiceSettingsTests(unittest.TestCase):
    def test_defaults(self):
        settings = VoiceSettings()
        self.assertTrue(settings.enabled)
        self.assertEqual(settings.voice, "ja")

    def test_validation(self):
        cases = [
            {"voice": ""}, {"voice": "x" * 41},
            {"speed": 79}, {"speed": 451},
            {"volume": -1}, {"volume": 201},
            {"maximum_characters": 0}, {"maximum_characters": 2001},
            {"timeout_seconds": 0}, {"timeout_seconds": 121},
        ]
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ValueError):
                VoiceSettings(**values)


class EspeakTests(unittest.IsolatedAsyncioTestCase):
    async def test_synthesize(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "speech.wav"
            factory = ProcessFactory()
            synthesizer = EspeakSynthesizer(VoiceSettings(), process_factory=factory)
            await synthesizer.synthesize(" こんにちは ", path)
            self.assertTrue(path.is_file())
            self.assertEqual(factory.arguments[-1], "こんにちは")
            self.assertEqual(factory.arguments[0], "espeak-ng")
            self.assertIn("stderr", factory.keywords)

    async def test_rejects_invalid_text(self):
        synthesizer = EspeakSynthesizer(VoiceSettings(maximum_characters=3))
        for text in ("", "four"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                await synthesizer.synthesize(text, Path("unused.wav"))

    async def test_process_failures(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "speech.wav"
            for options, expected in (
                ({"returncode": 2, "stderr": b"bad voice"}, "bad voice"),
                ({"returncode": 2}, "unknown error"),
                ({"create": False}, "produced no audio"),
                ({"timeout": True}, "timed out"),
            ):
                factory = ProcessFactory(**options)
                synthesizer = EspeakSynthesizer(VoiceSettings(), process_factory=factory)
                with self.subTest(options=options), self.assertRaisesRegex(RuntimeError, expected):
                    await synthesizer.synthesize("hello", path)
                self.assertFalse(path.exists())
                if options.get("timeout"):
                    self.assertTrue(factory.process.killed)


class VoicePluginTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def storage(directory):
        root = Path(directory)
        return FilePluginStorage(root / "state", root / "world")

    def context(self):
        key = SandboxKey("discord_guild", "1")
        return CapabilityContext(Event("event", datetime.now(timezone.utc), "channel", "10", "general", "42", "user", "speak", sandbox_key=str(key)), key)

    async def test_definition_and_lifecycle(self):
        with TemporaryDirectory() as directory:
            output = Output()
            key = SandboxKey("discord_guild", "1")
            services = SandboxServices(key, {
                "audio_output": output,
                "plugin_storage_factory": FilePluginStorageFactory(Path(directory), key),
            })
            configuration = {
                "enabled": True, "voice": "ja", "speed": 175, "volume": 100,
                "maximum_characters": 500, "timeout_seconds": 20.0,
            }
            plugin = VoicePluginDefinition().create(services.for_plugin("voice"), configuration)
            await plugin.start()
            self.assertFalse(plugin.storage.temporary_directory.exists())
            self.assertEqual(plugin.snapshot()["engine"], "espeak-ng")
            plugin.storage.temporary_path("stale.wav").write_bytes(b"x")
            await plugin.stop()
            self.assertEqual(output.stopped, AudioLane.SPEECH)
            self.assertFalse(plugin.storage.temporary_directory.exists())
            self.assertFalse(plugin.snapshot()["running"])

    async def test_disabled_plugin(self):
        with TemporaryDirectory() as directory:
            plugin = VoicePlugin(
                VoiceSettings(enabled=False), Synthesizer(), Output(), self.storage(directory)
            )
            await plugin.start()
            self.assertEqual(plugin.tool_providers, ())
            self.assertFalse(plugin.storage.temporary_directory.exists())
            await plugin.stop()

    async def test_tool_success_and_rejections(self):
        with TemporaryDirectory() as directory:
            output = Output()
            plugin = VoicePlugin(
                VoiceSettings(), Synthesizer(), output, self.storage(directory)
            )
            await plugin.start()
            context = self.context()
            self.assertEqual((await plugin.tools(context))[0].name, "speak")
            result = await plugin.execute_tool(
                "speak", {"text": "hello"}, context, "invocation"
            )
            self.assertEqual(result.status, "success")
            self.assertEqual(output.played, [(b"hello", "42", AudioLane.SPEECH)])
            self.assertFalse(plugin.storage.temporary_path("invocation.wav").exists())
            unknown = await plugin.execute_tool("unknown", {}, context, "id")
            self.assertEqual(unknown.status, "rejected")
            missing = await plugin.execute_tool(
                "speak", {"text": "x"}, CapabilityContext(None, context.sandbox_key), "id"
            )
            self.assertEqual(missing.model_payload["error"], "missing_source")

    async def test_tool_reports_synthesis_and_output_errors(self):
        with TemporaryDirectory() as directory:
            for synthesizer, output, expected in (
                (Synthesizer(ValueError("bad text")), Output(), "bad text"),
                (Synthesizer(), Output(RuntimeError("not in VC")), "not in VC"),
            ):
                plugin = VoicePlugin(
                    VoiceSettings(), synthesizer, output, self.storage(directory)
                )
                result = await plugin.execute_tool(
                    "speak", {"text": "hello"}, self.context(), "id"
                )
                self.assertEqual(result.model_payload["error"], expected)
                self.assertFalse(plugin.storage.temporary_path("id.wav").exists())

    def test_definition_requires_services(self):
        key = SandboxKey("discord_guild", "1")
        definition = VoicePluginDefinition()
        with self.assertRaises(LookupError):
            definition.create(SandboxServices(key), {})
        self.assertIsInstance(Output(), AudioOutput)


if __name__ == "__main__":
    unittest.main()
