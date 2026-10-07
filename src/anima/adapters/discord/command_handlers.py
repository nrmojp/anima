"""Execution handlers for Discord administration commands."""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from anima.core.access import ACTIVITY_MODES
from anima.core.sandbox import SandboxKey
from anima.core.sandbox_runtime import SandboxRouter
from anima.core.telemetry import emit, sandbox_context
from anima.adapters.discord.command_execution import execute_registered_command


LOGGER = logging.getLogger(__name__)
JST = ZoneInfo("Asia/Tokyo")


def activity_mode_message(details) -> str:
    labels = {
        "silent": "完全に沈黙",
        "reply": "呼ばれたら応答",
        "react": "応答＋リアクション",
        "proactive": "応答＋リアクション＋自発発言",
    }
    return f"現在の活動レベル: **{labels[details['mode']]}** (`{details['mode']}`)"


async def run_experimental_maintenance(client, interaction, kind: str) -> None:
    guild = interaction.guild
    permissions = getattr(interaction.user, "guild_permissions", None)
    if (
        guild is None
        or not getattr(permissions, "manage_guild", False)
        or not client.policy.allows(SandboxKey("guild", str(guild.id)))
    ):
        await interaction.response.send_message(
            "このコマンドを実行する権限がありません。", ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        if not isinstance(client.actor, SandboxRouter):
            raise RuntimeError("sandbox router is not configured")
        runtime = await client.actor.runtime(SandboxKey("guild", str(guild.id)))
        if await execute_registered_command(
            runtime, interaction, (getattr(client, "command_prefix", "anima") + "-maintenance",), {"kind": kind},
            f"/{getattr(client, 'command_prefix', 'anima')}-maintenance {kind}",
        ):
            return
        if kind == "self_time":
            decisions = await runtime.actor.force_self_time()
            await interaction.followup.send(
                f"Self Timeを{len(decisions)}反復実行しました。", ephemeral=True
            )
            return
        operation = await runtime.actor.force_maintenance(kind)
    except ValueError as error:
        LOGGER.warning(
            "Experimental maintenance rejected for guild %s (%s): %s",
            guild.id,
            kind,
            error,
            exc_info=True,
        )
        emit(
            "maintenance.rejected",
            operation=kind,
            forced=True,
            error_type=type(error).__name__,
            reason=str(error),
        )
        await interaction.followup.send(str(error), ephemeral=True)
    except Exception as error:
        LOGGER.exception("Experimental maintenance failed for guild %s", guild.id)
        emit(
            "maintenance.failed",
            operation=kind,
            forced=True,
            error_type=type(error).__name__,
        )
        await interaction.followup.send(
            "メンテナンスに失敗しました。ログを確認してください。", ephemeral=True
        )
    else:
        labels = {"nap": "昼寝", "sleep": "睡眠", "reflection": "振り返り"}
        await interaction.followup.send(
            f"{labels[operation]}を完了しました。", ephemeral=True
        )


async def run_activity_mode(client, interaction, mode: str) -> None:
    guild = interaction.guild
    permissions = getattr(interaction.user, "guild_permissions", None)
    if (
        guild is None
        or not getattr(permissions, "manage_guild", False)
        or not client.policy.allows(SandboxKey("guild", str(guild.id)))
    ):
        await interaction.response.send_message(
            "このコマンドを実行する権限がありません。", ephemeral=True
        )
        return
    key = SandboxKey("guild", str(guild.id))
    runtime_method = getattr(client.actor, "runtime", None)
    runtime = (
        await runtime_method(key)
        if isinstance(client.actor, SandboxRouter) and callable(runtime_method) else None
    )
    if runtime is not None and await execute_registered_command(
        runtime, interaction, (getattr(client, "command_prefix", "anima") + "-mode",), {"mode": mode},
        f"/{getattr(client, 'command_prefix', 'anima')}-mode {mode}",
    ):
        return
    if mode == "status":
        details = client.activity_modes.details(key)
        await interaction.response.send_message(
            activity_mode_message(details), ephemeral=True
        )
        return
    if mode not in ACTIVITY_MODES:
        await interaction.response.send_message("活動レベルが不正です。", ephemeral=True)
        return
    previous = client.activity_modes.get(key)
    details = client.activity_modes.set(
        key,
        mode,
        changed_by=f"{interaction.user} ({interaction.user.id})",
        changed_at=datetime.now(JST),
    )
    if ACTIVITY_MODES.index(mode) < ACTIVITY_MODES.index(previous):
        runtime = (
            client.actor.runtimes.get(key)
            if isinstance(client.actor, SandboxRouter)
            else None
        )
        if runtime is not None:
            await runtime.actor.cancel_pending_reactions()
    token = sandbox_context.set(str(key))
    try:
        emit(
            "activity_mode.changed",
            previous=previous,
            mode=mode,
            changed_by=str(interaction.user.id),
        )
    finally:
        sandbox_context.reset(token)
    await interaction.response.send_message(
        activity_mode_message(details), ephemeral=True
    )
