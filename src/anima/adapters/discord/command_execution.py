"""Discord adapter for transport-neutral capability commands."""

from __future__ import annotations

import logging
from zoneinfo import ZoneInfo

from anima.capabilities.contracts import PermissionSet
from anima.capabilities.commands import CommandContext
from anima.bootstrap.command_providers import command_event
from anima.core.models import Event
from anima.core.sandbox import SandboxKey
from anima.core.telemetry import emit, sandbox_context


JST = ZoneInfo("Asia/Tokyo")
LOGGER = logging.getLogger(__name__)


def command_source(interaction, key: SandboxKey, text: str) -> Event:
    voice = getattr(interaction.user, "voice", None)
    channel = getattr(voice, "channel", None)
    return Event(
        id=f"interaction-{interaction.id}",
        ts=interaction.created_at.astimezone(JST), kind="channel",
        channel_id=str(interaction.channel_id),
        channel_name=f"#{getattr(interaction.channel, 'name', 'unknown')}",
        author_id=str(interaction.user.id), author_name=interaction.user.display_name,
        text=text, guild_id=str(interaction.guild.id),
        author_voice_channel_id=str(channel.id) if channel else None,
        sandbox_key=str(key),
    )


async def execute_registered_command(
    runtime, interaction, path: tuple[str, ...], arguments: dict[str, object], text: str
) -> bool:
    registry = getattr(runtime, "commands", None)
    if registry is None:
        return False
    key = SandboxKey("guild", str(interaction.guild.id))
    source = command_source(interaction, key, text)
    permissions = getattr(interaction.user, "guild_permissions", None)
    values = frozenset(
        {"manage_sandbox"} if getattr(permissions, "manage_guild", False) else set()
    )
    spec = registry.spec(path)
    response_done = getattr(interaction.response, "is_done", lambda: False)()
    deferred = spec is not None and spec.response_mode == "deferred"
    if deferred and not response_done:
        await interaction.response.defer(
            ephemeral=bool(spec.permission), thinking=True
        )
    token = sandbox_context.set(str(key))
    try:
        try:
            result = await registry.execute(
                path, arguments,
                CommandContext(source, key, str(interaction.user.id), PermissionSet(values)),
            )
            recorded = command_event(result, source)
            if recorded is not None:
                store = getattr(runtime.actor, "store", None)
                if store is not None:
                    store.append_received(recorded)
        except Exception as error:
            LOGGER.exception("Discord command failed: %s", " ".join(path))
            emit(
                "command.failed", command=" ".join(path),
                error_type=type(error).__name__,
            )
            message = "コマンドの実行に失敗しました。ログを確認してください。"
            if deferred or getattr(interaction.response, "is_done", lambda: False)():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
            return True
    finally:
        sandbox_context.reset(token)
    ephemeral = result.visibility == "private"
    if spec is not None and spec.response_mode == "deferred":
        await interaction.followup.send(result.text, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(result.text, ephemeral=ephemeral)
    return True
