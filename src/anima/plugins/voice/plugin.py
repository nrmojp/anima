"""Local TTS capability routed through the sandbox audio mixer."""

from __future__ import annotations

from collections.abc import Mapping

from anima.capabilities.configuration import ConfigField
from anima.capabilities.plugins import DashboardPanelSpec, PluginManifest
from anima.capabilities.tools import CapabilityContext, FunctionToolSpec, ToolResult
from anima.core.models import ActionRecord
from anima.core.audio_ports import AudioLane, AudioOutput
from anima.core.services import SandboxServices
from anima.core.storage import PluginStorage
from anima.plugins.voice.config import VoiceSettings
from anima.plugins.voice.espeak import EspeakSynthesizer


_SPEAK_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}


class VoicePluginDefinition:
    manifest = PluginManifest(
        name="voice",
        version="1.0.0",
        default_enabled=True,
        uses_audio=True,
        provides=("voice",),
        description="Generate speech locally and play it in the requester's Discord VC.",
        configuration=(
            ConfigField("enabled", "ANIMA_VOICE_ENABLED", "enabled", "Enabled", "Voice", "bool", True, plugin="voice"),
            ConfigField("voice", "ANIMA_VOICE_NAME", "voice", "eSpeak voice", "Voice", "text", "ja", plugin="voice"),
            ConfigField("speed", "ANIMA_VOICE_SPEED", "speed", "Words per minute", "Voice", "int", 175, 80, 450, plugin="voice"),
            ConfigField("volume", "ANIMA_VOICE_VOLUME", "volume", "Volume", "Voice", "int", 100, 0, 200, plugin="voice"),
            ConfigField("maximum_characters", "ANIMA_VOICE_MAXIMUM_CHARACTERS", "maximum_characters", "Character limit", "Voice", "int", 500, 1, 2_000, plugin="voice"),
            ConfigField("timeout_seconds", "ANIMA_VOICE_TIMEOUT_SECONDS", "timeout_seconds", "Timeout", "Voice", "float", 20.0, 1, 120, plugin="voice"),
        ),
        dashboard_panels=(DashboardPanelSpec(
            "voice", "Voice", "status", 40,
            "Local synthesis and shared audio-route status.",
        ),),
    )

    def create(
        self, sandbox_services: SandboxServices, configuration: Mapping[str, object]
    ) -> "VoicePlugin":
        settings = VoiceSettings(**configuration)
        output = sandbox_services.require("audio_output", AudioOutput)
        storage = sandbox_services.require("plugin_storage", PluginStorage)
        return VoicePlugin(settings, EspeakSynthesizer(settings), output, storage)


class VoicePlugin:
    manifest = VoicePluginDefinition.manifest
    resource_registrations = ()

    def __init__(
        self, settings: VoiceSettings, synthesizer: EspeakSynthesizer,
        output: AudioOutput, storage: PluginStorage,
    ) -> None:
        self.settings = settings
        self.synthesizer = synthesizer
        self.output = output
        self.storage = storage
        self.tool_providers = (self,) if settings.enabled else ()
        self.command_providers = ()
        self.running = False

    async def start(self) -> None:
        if self.settings.enabled:
            self.storage.clear_temporary()
        self.running = True

    async def stop(self) -> None:
        await self.output.stop(AudioLane.SPEECH)
        self.storage.clear_temporary()
        self.running = False

    def snapshot(self) -> Mapping[str, object]:
        return {
            "name": self.manifest.name,
            "running": self.running,
            "enabled": self.settings.enabled,
            "engine": "espeak-ng",
            "voice": self.settings.voice,
        }

    async def tools(self, context: CapabilityContext) -> tuple[FunctionToolSpec, ...]:
        del context
        return (FunctionToolSpec(
            "speak", "Speak text in the requesting member's current Discord voice channel.",
            _SPEAK_SCHEMA, side_effect=True,
        ),)

    async def execute_tool(
        self, name: str, arguments: Mapping[str, object],
        context: CapabilityContext, invocation_id: str,
    ) -> ToolResult:
        if name != "speak":
            return ToolResult("rejected", {"ok": False, "error": "unknown_tool"})
        if context.source is None:
            return ToolResult("rejected", {"ok": False, "error": "missing_source"})
        path = self.storage.temporary_path(f"{invocation_id}.wav")
        try:
            await self.synthesizer.synthesize(str(arguments["text"]), path)
            await self.output.play_wav(
                path, context.source.author_id, lane=AudioLane.SPEECH,
            )
        except (RuntimeError, ValueError) as error:
            return ToolResult("rejected", {"ok": False, "error": str(error)})
        finally:
            path.unlink(missing_ok=True)
        return ToolResult(
            "success", {"ok": True, "spoken": True},
            actions=(ActionRecord("voice", "play", "Played locally synthesized speech"),),
        )


PLUGIN = VoicePluginDefinition()
