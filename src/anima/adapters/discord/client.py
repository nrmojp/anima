"""discord.py adapter that translates messages into domain events."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import mimetypes
import os
import re
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord import app_commands

from anima.core.actor import PersonaActor
from anima.core.models import Attachment, Event
from anima.core.inventory import MAX_ARTIFACT_BYTES
from anima.adapters.discord.events import event_from_message
from anima.adapters.discord.delivery import DiscordMessageSender
from anima.adapters.discord.commands import PluginCommandRegistrar
from anima.adapters.discord.command_handlers import (
    activity_mode_message,
    run_activity_mode,
    run_experimental_maintenance,
)
from anima.adapters.discord.recovery import backfill_offline_messages, recover_pending_messages
from anima.adapters.discord.voice_lifecycle import has_human_members, leave_empty_voice_channel
from anima.core.telemetry import emit
from anima.core.sandbox import SandboxKey
from anima.core.sandbox_runtime import SandboxRouter
from anima.core.access import ActivityModeStore, ActivityPolicy


LOGGER = logging.getLogger(__name__)
JST = ZoneInfo("Asia/Tokyo")

class AnimaDiscordClient(discord.Client):
    def __init__(
        self,
        *,
        actor: PersonaActor,
        sender: DiscordMessageSender,
        voice: object | None = None,
        music: object | None = None,
        status_path: Path | None = None,
        policy: ActivityPolicy | None = None,
        activity_modes: ActivityModeStore | None = None,
        shutdown_timeout_seconds: float = 30.0,
        voice_enabled: bool = False,
        plugins: frozenset[str] | None = None,
        persona_names: tuple[str, ...] = (),
        command_prefix: str = "anima",
    ) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.actor = actor
        self.policy = actor.policy if isinstance(actor, SandboxRouter) else (policy or ActivityPolicy())
        self.activity_modes = activity_modes or ActivityModeStore()
        if isinstance(actor, SandboxRouter):
            actor.bind(self)
        self.sender = sender
        sender.client = self
        self.voice = voice
        self.voice_enabled = voice_enabled or voice is not None
        self.plugins = plugins if plugins is not None else frozenset()
        self.music = music
        self.persona_names = persona_names
        self.command_prefix = command_prefix
        if voice is not None:
            voice.bind(self)
        if music is not None:
            music.bind(self)
        self._recovering = False
        self.status_path = status_path
        self.shutdown_timeout_seconds = shutdown_timeout_seconds
        self._dm_names = {}
        self.tree = app_commands.CommandTree(self)
        self.plugin_command_registrar = PluginCommandRegistrar(self)

    async def setup_hook(self) -> None:
        await self.actor.start()
        if self.voice is not None:
            await self.voice.start()
        for guild_id in sorted(self.policy.allowed_guild_ids):
            guild = discord.Object(id=int(guild_id))
            if isinstance(self.actor, SandboxRouter):
                runtime = await self.actor.runtime(SandboxKey("guild", str(guild_id)))
                if runtime.commands is not None:
                    self.plugin_command_registrar.install(runtime.commands.specs, guild=guild)
            await self.tree.sync(guild=guild)

    async def _run_experimental_maintenance(
        self, interaction: discord.Interaction, kind: str
    ) -> None:
        await run_experimental_maintenance(self, interaction, kind)

    async def _run_activity_mode(self, interaction: discord.Interaction, mode: str) -> None:
        await run_activity_mode(self, interaction, mode)



    @staticmethod
    def _activity_mode_message(details) -> str:
        return activity_mode_message(details)

    async def close(self) -> None:
        try:
            try:
                async with asyncio.timeout(self.shutdown_timeout_seconds):
                    await self.actor.stop()
            except TimeoutError:
                LOGGER.error(
                    "Bot shutdown exceeded %.1fs; abandoning the remaining queue",
                    self.shutdown_timeout_seconds,
                )
                emit("runtime.shutdown.timeout", timeout_seconds=self.shutdown_timeout_seconds)
            if self.music is not None:
                await self.music.stop("bot_stopping")
            if self.voice is not None:
                await self.voice.stop()
        finally:
            self._write_status(connected=False)
            await super().close()

    async def on_ready(self) -> None:
        if any(self.sender.faces.faces.values()):
            try:
                self.sender.faces.validate_registered(await self.fetch_application_emojis())
            except Exception as error:
                self.sender.faces.available.clear()
                emit("face.unavailable", error_type=type(error).__name__)
        LOGGER.info("Connected to Discord as %s", self.user)
        self._write_status(connected=True)
        await backfill_offline_messages(self)
        await self._recover_pending_messages()

    def _write_status(self, *, connected: bool) -> None:
        if self.status_path is None:
            return
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        value = {}
        if self.status_path.exists():
            try:
                value = json.loads(self.status_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        value.update({
            "connected": connected,
            "pid": os.getpid(),
            "updated_at": datetime.now(JST).isoformat(),
            "user": str(self.user) if self.user else None,
            "gateway_ping_ms": (
                round(float(self.latency) * 1000)
                if connected and isinstance(self.latency, (int, float))
                and math.isfinite(self.latency) else None
            ),
            "activity": {"allowed_guild_ids": sorted(self.policy.allowed_guild_ids),
                         "dm_enabled": self.policy.dm_enabled},
            "features": {
                "voice_enabled": self.voice_enabled,
                "plugins": sorted(self.plugins),
            },
            "sandbox_names": {**value.get("sandbox_names", {}),
                              **{f"guild:{guild.id}": guild.name for guild in self.guilds},
                              **self._dm_names},
        })
        temporary = self.status_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        os.replace(temporary, self.status_path)

    async def on_message(self, message: discord.Message) -> None:
        if self.user is None or getattr(message.author, "id", None) == self.user.id:
            return
        if message.guild is not None:
            key = SandboxKey("guild", str(message.guild.id))
        elif isinstance(message.channel, discord.DMChannel):
            key = SandboxKey("dm", str(message.author.id))
        else:
            return
        if not self.policy.allows(key):
            return
        mode = self.activity_modes.get(key)
        if mode == "silent":
            return
        if key.kind == "dm" and self._dm_names.get(str(key)) != message.author.display_name:
            self._dm_names[str(key)] = message.author.display_name
            self._write_status(connected=True)
        event = self._to_event(message)
        event = await self._cache_message_attachments(message, event, key)
        music = self.music
        if isinstance(self.actor, SandboxRouter):
            runtime = await self.actor.runtime(SandboxKey.for_event(event))
            mark_seen = getattr(runtime.actor, "mark_seen", None)
            if mark_seen is not None:
                mark_seen()
            music = runtime.music
        self.sender.register(event.id, message)
        try:
            if event.requires_immediate_response:
                async with message.channel.typing():
                    await self.actor.submit(event)
            else:
                await self.actor.submit(
                    event, allow_reactions=mode in {"react", "proactive"}
                )
        except Exception:
            LOGGER.exception("Failed to process Discord message %s", message.id)
        finally:
            self.sender.discard(event.id)

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        """Record a Discord tombstone even when the deleted message was not cached."""
        await self._record_message_delete(
            guild_id=payload.guild_id,
            channel_id=payload.channel_id,
            message_id=payload.message_id,
            cached_message=payload.cached_message,
        )

    async def on_raw_bulk_message_delete(
        self, payload: discord.RawBulkMessageDeleteEvent
    ) -> None:
        """Apply every message ID from Discord's separate bulk-delete event."""
        cached = {message.id: message for message in payload.cached_messages}
        for message_id in payload.message_ids:
            await self._record_message_delete(
                guild_id=payload.guild_id,
                channel_id=payload.channel_id,
                message_id=message_id,
                cached_message=cached.get(message_id),
            )

    async def _record_message_delete(
        self, *, guild_id, channel_id, message_id, cached_message
    ) -> None:
        if guild_id is not None:
            key = SandboxKey("guild", str(guild_id))
        else:
            if cached_message is None or cached_message.author.bot:
                emit(
                    "discord.message.delete_skipped",
                    channel_id=str(channel_id),
                    event_id=str(message_id),
                    reason="dm_sandbox_unknown",
                )
                return
            key = SandboxKey("dm", str(cached_message.author.id))
        if not self.policy.allows(key):
            return
        try:
            if isinstance(self.actor, SandboxRouter):
                await self.actor.mark_deleted(
                    key, str(channel_id), str(message_id)
                )
            else:
                await self.actor.mark_deleted(
                    str(channel_id), str(message_id)
                )
        except Exception:
            LOGGER.exception("Failed to record deleted Discord message %s", message_id)

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """Leave when the last human leaves the channel occupied by the bot."""
        await leave_empty_voice_channel(self, member, before, after)

    async def _recover_pending_messages(self) -> None:
        await recover_pending_messages(self)

    def _to_event(self, message: discord.Message) -> Event:
        if self.user is None:
            raise RuntimeError("Discord client user is unavailable")
        return event_from_message(message, bot_user=self.user, timezone=JST, persona_names=getattr(self, "persona_names", ()))

    async def _cache_message_attachments(
        self, message: discord.Message, event: Event, key: SandboxKey
    ) -> Event:
        if not isinstance(self.actor, SandboxRouter) or not event.attachments:
            return event
        directory = key.path(self.actor.root) / "attachments"
        cached = []
        for index, (source, attachment) in enumerate(
            zip(message.attachments, event.attachments, strict=True)
        ):
            inferred_type = attachment.content_type or mimetypes.guess_type(
                getattr(source, "filename", "")
            )[0]
            suffix = Path(getattr(source, "filename", "")).suffix.lower()
            if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
                suffix = mimetypes.guess_extension(inferred_type or "") or ".bin"
            name = f"{event.id}-{index}{suffix}"
            try:
                if getattr(source, "size", 0) > MAX_ARTIFACT_BYTES:
                    raise ValueError("Discord attachment is too large")
                data = await source.read(use_cached=True)
                if not data or len(data) > MAX_ARTIFACT_BYTES:
                    raise ValueError("Discord attachment has an invalid size")
                directory.mkdir(parents=True, exist_ok=True)
                temporary = directory / f".{name}.tmp"
                temporary.write_bytes(data)
                os.replace(temporary, directory / name)
                cached.append(Attachment(attachment.url, inferred_type, name))
                emit(
                    "attachment.cached", event_id=event.id, cache_name=name,
                    byte_count=len(data), content_type=attachment.content_type,
                )
            except (discord.HTTPException, OSError, ValueError):
                LOGGER.exception("Could not cache Discord attachment for message %s", event.id)
                emit(
                    "attachment.cache_failed", event_id=event.id,
                    content_type=attachment.content_type,
                )
                cached.append(attachment)
        return replace(event, attachments=tuple(cached))
