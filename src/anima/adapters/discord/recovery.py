"""Recovery of accepted Discord messages that have not completed processing."""

from __future__ import annotations

import logging

import discord

from anima.core.sandbox import SandboxKey


LOGGER = logging.getLogger(__name__)


async def recover_pending_messages(client) -> None:
    """Replay pending events after checking their Discord source and sandbox."""
    if client._recovering:
        return
    client._recovering = True
    try:
        pending = client.actor.pending_events()
        if pending:
            LOGGER.info("Recovering %d pending Discord message(s)", len(pending))
        for event in pending:
            key = SandboxKey.for_event(event)
            if not client.policy.allows(key) or client.activity_modes.get(key) == "silent":
                continue
            try:
                channel = client.get_channel(int(event.conversation_id))
                if channel is None:
                    channel = await client.fetch_channel(int(event.conversation_id))
                message = await channel.fetch_message(int(event.id))
                recovered = client._to_event(message)
                if SandboxKey.for_event(recovered) != key:
                    raise ValueError("recovered message sandbox mismatch")
            except discord.NotFound:
                LOGGER.warning(
                    "Pending Discord message %s was deleted; abandoning it", event.id
                )
                client.actor.abandon(event)
                continue
            except (discord.Forbidden, discord.HTTPException, ValueError):
                LOGGER.exception("Could not recover pending Discord message %s", event.id)
                continue
            client.sender.register(event.id, message)
            try:
                async with message.channel.typing():
                    await client.actor.submit(event)
            except Exception:
                LOGGER.exception("Failed to replay Discord message %s", event.id)
            finally:
                client.sender.discard(event.id)
    finally:
        client._recovering = False


async def backfill_offline_messages(client, *, limit_per_sandbox: int = 500) -> None:
    """Fill journal gaps for known guild channels without replaying old replies."""
    from anima.core.sandbox_runtime import SandboxRouter

    if not isinstance(client.actor, SandboxRouter):
        return
    for key, runtime in tuple(client.actor.runtimes.items()):
        if key.kind != "guild" or not client.policy.allows(key):
            continue
        guild = client.get_guild(int(key.id))
        if guild is None:
            continue
        latest = runtime.actor.latest_event_ids()
        recovered = []
        truncated = False
        remaining = limit_per_sandbox
        for channel in guild.text_channels:
            after_id = latest.get(str(channel.id))
            if after_id is None or remaining <= 0:
                continue
            try:
                messages = [message async for message in channel.history(
                    after=discord.Object(id=int(after_id)),
                    oldest_first=True, limit=remaining + 1,
                )]
            except (discord.Forbidden, discord.HTTPException):
                LOGGER.warning("Could not backfill Discord channel %s", channel.id)
                continue
            if len(messages) > remaining:
                truncated = True
                messages = messages[:remaining]
            for message in messages:
                if client.user is not None and message.author.id == client.user.id:
                    continue
                event = client._to_event(message)
                event = await client._cache_message_attachments(message, event, key)
                recovered.append(event)
            remaining = limit_per_sandbox - len(recovered)
        runtime.actor.record_backfill(tuple(recovered), truncated=truncated)
        runtime.actor.mark_seen()
        if recovered or truncated:
            LOGGER.info(
                "Backfilled %d Discord message(s) for %s%s",
                len(recovered), key, " (truncated)" if truncated else "",
            )
