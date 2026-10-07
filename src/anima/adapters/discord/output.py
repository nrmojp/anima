"""Sandbox-bound output exposed to trusted capability plugins."""
from __future__ import annotations
import discord
from anima.core.sandbox import SandboxKey
from anima.core.models import ResponseDraft


class DiscordMessageOutput:
    def __init__(self, client, sandbox_key: SandboxKey):
        self.client = client
        self.sandbox_key = sandbox_key

    async def send(self, destination_id: str, response: ResponseDraft) -> None:
        if self.sandbox_key.kind not in {"guild", "dm"}:
            raise RuntimeError("Discord output requires a Discord sandbox")
        if not destination_id.isascii() or not destination_id.isdecimal():
            raise ValueError("Discord destination ID is invalid")
        channel = self.client.get_channel(int(destination_id))
        if channel is None:
            raise RuntimeError("Discord destination is unavailable")
        guild = getattr(channel, "guild", None)
        recipient = getattr(channel, "recipient", None)
        actual = (SandboxKey("guild", str(guild.id)) if guild is not None
                  else SandboxKey("dm", str(recipient.id)) if recipient is not None else None)
        if actual != self.sandbox_key:
            raise PermissionError("Discord destination sandbox mismatch")
        files = [discord.File(path) for path in response.images]
        kwargs = {"files": files} if files else {}
        await channel.send(response.reply, allowed_mentions=discord.AllowedMentions.none(), **kwargs)
