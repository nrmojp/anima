"""Discord voice-channel lifecycle handling."""

from __future__ import annotations

import discord

from anima.core.sandbox import SandboxKey
from anima.core.sandbox_runtime import SandboxRouter
from anima.core.telemetry import emit


def has_human_members(channel: discord.abc.GuildChannel) -> bool:
    return any(not member.bot for member in getattr(channel, "members", ()))


async def leave_empty_voice_channel(client, member, before, after) -> None:
    """Disconnect when the last human leaves the active voice channel."""
    if member.bot or before.channel is None or before.channel == after.channel:
        return
    connection = discord.utils.get(client.voice_clients, guild=member.guild)
    if connection is None or connection.channel != before.channel:
        return
    if has_human_members(before.channel):
        return

    channel_id = str(before.channel.id)
    audio = None
    voice, music, dj = client.voice, client.music, None
    if isinstance(client.actor, SandboxRouter):
        runtime = client.actor.runtimes.get(
            SandboxKey("guild", str(member.guild.id))
        )
        voice, music, dj = (
            (runtime.voice, runtime.music, runtime.dj)
            if runtime else (None, None, None)
        )
        audio = getattr(runtime, "audio_output", None) if runtime else None
    if voice is not None:
        await voice.cancel_current("channel_empty")
    if dj is not None:
        await dj.stop("channel_empty")
    if music is not None:
        await music.stop("channel_empty")
    if audio is not None:
        await audio.stop()
        if connection.is_connected():
            await connection.disconnect(force=True)
    else:
        await connection.disconnect(force=True)
    emit("voice.disconnected", channel_id=channel_id, reason="channel_empty")
