"""Public API for the local voice plugin."""

from anima.plugins.voice.config import VoiceSettings
from anima.plugins.voice.espeak import EspeakSynthesizer
from anima.plugins.voice.plugin import VoicePlugin, VoicePluginDefinition

__all__ = ["EspeakSynthesizer", "VoicePlugin", "VoicePluginDefinition", "VoiceSettings"]
