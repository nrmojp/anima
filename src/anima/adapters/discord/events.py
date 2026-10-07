"""Map discord.py messages into the transport-neutral event envelope."""

from __future__ import annotations

from datetime import tzinfo

import discord

from anima.core.models import Attachment, Event, MentionedPerson


def event_from_message(
    message: discord.Message,
    *,
    bot_user: discord.ClientUser | discord.User,
    timezone: tzinfo,
    persona_names: tuple[str, ...] = (),
) -> Event:
    """Normalize one Discord message at the adapter boundary."""
    is_dm = isinstance(message.channel, discord.DMChannel)
    channel_name = "DM" if is_dm else f"#{getattr(message.channel, 'name', message.channel.id)}"
    reply_to = (
        str(message.reference.message_id)
        if message.reference and message.reference.message_id
        else None
    )
    resolved = message.reference.resolved if message.reference else None
    reply_to_self = (
        isinstance(resolved, discord.Message) and resolved.author.id == bot_user.id
    )
    sandbox_key = (
        f"guild:{message.guild.id}"
        if message.guild
        else f"dm:{message.author.id}"
    )
    return Event(
        id=str(message.id),
        ts=message.created_at.astimezone(timezone),
        kind="dm" if is_dm else "channel",
        channel_id=str(message.channel.id),
        channel_name=channel_name,
        author_id=str(message.author.id),
        author_name=message.author.display_name,
        author_is_bot=bool(getattr(message.author, "bot", False)),
        text=message.content,
        guild_id=str(message.guild.id) if message.guild else None,
        author_voice_channel_id=(
            str(message.author.voice.channel.id)
            if isinstance(message.author, discord.Member)
            and message.author.voice
            and message.author.voice.channel
            else None
        ),
        mention=bot_user in message.mentions,
        called_name=any(name.casefold() in message.content.casefold() for name in persona_names),
        reply_to=reply_to,
        reply_to_self=reply_to_self,
        reply_author_name=(resolved.author.display_name if isinstance(resolved, discord.Message) else None),
        reply_text=(resolved.content if isinstance(resolved, discord.Message) else None),
        sandbox_key=sandbox_key,
        attachments=tuple(
            Attachment(url=item.url, content_type=item.content_type)
            for item in message.attachments
        ),
        mentioned_people=tuple(
            MentionedPerson(id=str(member.id), name=member.display_name)
            for member in message.mentions
            if member.id != bot_user.id and member.id != message.author.id
        ),
    )
