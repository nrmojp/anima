"""Local interface adapters used for deterministic and live harness checks."""

from .discord import StubDiscordAdapter, StubDiscordSender

__all__ = ["StubDiscordAdapter", "StubDiscordSender"]
