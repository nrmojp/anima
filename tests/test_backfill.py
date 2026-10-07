import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from anima.adapters.discord.recovery import backfill_offline_messages
from anima.core.access import ActivityPolicy
from anima.core.models import Event
from anima.core.sandbox import SandboxKey
from anima.core.sandbox_runtime import SandboxRouter
from anima.core.state import FileStateStore


NOW = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)


def event(identifier="10", channel="20"):
    return Event(
        identifier, NOW, "channel", channel, "general", "30", "person", "hello",
        guild_id="1", sandbox_key="guild:1",
    )


class StateBackfillTests(unittest.TestCase):
    def test_backfill_is_idempotent_handled_and_observable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = FileStateStore(root)
            store.ensure_layout(now=NOW)
            first = event()
            store.record_backfill((first,), truncated=True, now=NOW)
            store.record_backfill((first,), truncated=True, now=NOW)
            internal = Event(
                "command-interaction-30", NOW, "channel",
                "20", "general", "self", "自分", "活動モードを変更した",
                guild_id="1", sandbox_key="guild:1",
            )
            store.append_received(internal)
            self.assertEqual(store.latest_event_ids(), {"20": "10"})
            self.assertTrue(store.is_handled(first))
            self.assertEqual(store.backfill_status()["count"], 1)
            self.assertTrue(store.backfill_status()["truncated"])
            store.mark_seen(now=NOW)
            self.assertEqual(store._read_json(root / "cursor.json", {})["last_seen_at"], NOW.isoformat())


class DiscordBackfillTests(unittest.IsolatedAsyncioTestCase):
    async def test_known_channels_are_bounded_and_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            key = SandboxKey("guild", "1")
            actor = SimpleNamespace(
                latest_event_ids=MagicMock(return_value={"20": "9"}),
                record_backfill=MagicMock(), mark_seen=MagicMock(),
            )
            router = SandboxRouter(Path(directory), MagicMock(), policy=ActivityPolicy(frozenset({"1"})))
            router.runtimes[key] = SimpleNamespace(actor=actor)
            messages = [
                SimpleNamespace(id=10, author=SimpleNamespace(id=30)),
                SimpleNamespace(id=11, author=SimpleNamespace(id=31)),
            ]

            class Channel:
                id = 20
                async def history(self, **kwargs):
                    self.kwargs = kwargs
                    for message in messages:
                        yield message

            channel = Channel()
            client = SimpleNamespace(
                actor=router, policy=ActivityPolicy(frozenset({"1"})),
                user=SimpleNamespace(id=99),
                get_guild=lambda _id: SimpleNamespace(text_channels=[channel]),
                _to_event=lambda message: event(str(message.id)),
                _cache_message_attachments=AsyncMock(side_effect=lambda _m, item, _k: item),
            )
            await backfill_offline_messages(client, limit_per_sandbox=1)
            recorded, = actor.record_backfill.call_args.args
            self.assertEqual([item.id for item in recorded], ["10"])
            self.assertTrue(actor.record_backfill.call_args.kwargs["truncated"])
            actor.mark_seen.assert_called_once_with()
            self.assertEqual(channel.kwargs["limit"], 2)

    async def test_skips_unsupported_router_guild_and_channel_failures(self):
        plain = SimpleNamespace(actor=object())
        await backfill_offline_messages(plain)
        with tempfile.TemporaryDirectory() as directory:
            key = SandboxKey("guild", "1")
            actor = SimpleNamespace(
                latest_event_ids=MagicMock(return_value={}),
                record_backfill=MagicMock(), mark_seen=MagicMock(),
            )
            router = SandboxRouter(Path(directory), MagicMock(), policy=ActivityPolicy(frozenset({"1"})))
            router.runtimes[key] = SimpleNamespace(actor=actor)
            client = SimpleNamespace(
                actor=router, policy=ActivityPolicy(frozenset({"1"})), user=None,
                get_guild=lambda _id: None,
            )
            await backfill_offline_messages(client)
            actor.record_backfill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
