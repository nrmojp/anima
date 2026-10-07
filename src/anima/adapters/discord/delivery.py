"""Discord implementation of the outbound message ports."""

from __future__ import annotations

from datetime import timezone
from zoneinfo import ZoneInfo

import discord

from anima.core.expressions import FaceCatalog
from anima.core.models import Event, SentMessage
from anima.core.ports import TargetedNotification


JST = ZoneInfo("Asia/Tokyo")


class DiscordMessageSender:
    def __init__(self, *, faces=None) -> None:
        self._messages: dict[str, discord.Message] = {}
        self.faces = faces or FaceCatalog()
        self.client = None

    async def react(self, source, emoji):
        channel = self.client.get_channel(int(source.channel_id))
        if channel is None or str(getattr(getattr(channel, "guild", None), "id", "")) != source.guild_id:
            raise ValueError("reaction destination sandbox mismatch")
        message = await channel.fetch_message(int(source.id))
        if message.author.bot:
            return False
        if any(str(item.emoji) == emoji and item.me for item in message.reactions):
            return True
        await message.add_reaction(discord.PartialEmoji.from_str(emoji))
        return True

    def prepare_face(self, text, face, strength):
        """Include non-strong faces in the initial message to avoid an edited badge."""
        if face == self.faces.default:
            return text
        emoji = self.faces.emoji(face)
        decorated = text + " " + emoji if emoji is not None else text
        if strength != "強い" and len(decorated) <= 2000:
            return decorated
        return text

    async def show_face(self, source, sent, face, strength, text):
        if face == self.faces.default:
            return
        emoji = self.faces.emoji(face)
        if emoji is None:
            return
        message = self._messages.get(source.id)
        if message is None:
            return
        if str(message.channel.id) != source.channel_id or (str(message.guild.id) if message.guild else None) != source.guild_id:
            raise ValueError("face destination sandbox mismatch")
        if strength == "強い":
            await message.channel.send(emoji, allowed_mentions=discord.AllowedMentions.none())

    def register(self, event_id: str, message: discord.Message) -> None:
        self._messages[event_id] = message

    def discard(self, event_id: str) -> None:
        self._messages.pop(event_id, None)

    async def send(self, source: Event, text: str, *, attachments=()) -> SentMessage:
        message = self._messages.get(source.id)
        if message is None:
            if source.is_notification:
                return await self.send_background(source, text, attachments=attachments)
            raise RuntimeError(f"Discord source message {source.id} is no longer registered")
        if str(message.channel.id) != source.channel_id or (
            str(message.guild.id) if message.guild else None
        ) != source.guild_id:
            raise ValueError("Discord destination does not match source")
        allowed_mentions = discord.AllowedMentions.none()
        files = [discord.File(path) for path in attachments]
        file_arguments = {"files": files} if files else {}
        if source.reply_to_self:
            sent = await message.reply(
                text, mention_author=False, allowed_mentions=allowed_mentions, **file_arguments
            )
        else:
            sent = await message.channel.send(
                text, nonce=source.id, allowed_mentions=allowed_mentions, **file_arguments
            )
        return self._sent_message(sent)

    async def send_proactive(self, source: Event, text: str) -> SentMessage:
        channel = self.client.get_channel(int(source.channel_id))
        if (
            channel is None
            or str(getattr(getattr(channel, "guild", None), "id", "")) != source.guild_id
        ):
            raise ValueError("proactive destination sandbox mismatch")
        return self._sent_message(
            await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
        )

    async def send_background(
        self, source: Event, text: str, *, attachments=()
    ) -> SentMessage:
        """Deliver a completed background job without retaining its source message."""
        channel = self.client.get_channel(int(source.channel_id))
        if (
            channel is None
            or str(getattr(getattr(channel, "guild", None), "id", "")) != source.guild_id
        ):
            raise ValueError("background destination sandbox mismatch")
        files = [discord.File(path) for path in attachments]
        file_arguments = {"files": files} if files else {}
        return self._sent_message(await channel.send(
            text, allowed_mentions=discord.AllowedMentions.none(), **file_arguments,
        ))

    async def send_reminder(self, reminder: TargetedNotification) -> None:
        channel = self.client.get_channel(int(reminder.channel_id))
        guild_id = str(channel.guild.id) if channel is not None and channel.guild else None
        if channel is None or guild_id != reminder.guild_id:
            raise ValueError("reminder destination sandbox mismatch")
        mention = discord.Object(id=int(reminder.author_id))
        await channel.send(
            f"<@{reminder.author_id}> リマインダー：{reminder.message}",
            allowed_mentions=discord.AllowedMentions(users=[mention]),
        )

    async def find_reply(self, source: Event) -> SentMessage | None:
        message = self._messages.get(source.id)
        if message is None:
            if source.author_id == "self":
                return None
            raise RuntimeError(f"Discord source message {source.id} is no longer registered")
        bot_user = self.client.user
        if bot_user is None:
            return None
        async for candidate in message.channel.history(
            limit=500, after=message.created_at, oldest_first=True
        ):
            reference_id = (
                candidate.reference.message_id
                if candidate.reference and candidate.reference.message_id
                else None
            )
            matches_delivery = (
                str(reference_id) == source.id
                if source.reply_to_self
                else str(getattr(candidate, "nonce", "")) == source.id
            )
            if candidate.author.id == bot_user.id and matches_delivery:
                return self._sent_message(candidate)
        return None

    @staticmethod
    def _sent_message(message) -> SentMessage:
        timestamp = message.created_at
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return SentMessage(
            id=str(message.id),
            timestamp=timestamp.astimezone(JST),
            text=message.content,
        )
