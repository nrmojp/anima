from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from anima.capabilities.contracts import ActionRecord, PermissionSet
from anima.capabilities.commands import CommandContext
from anima.bootstrap.command_providers import (
    ActivityCommandProvider,
    MaintenanceCommandProvider,
    _activity_message,
    _optional_text,
    command_event,
)
from anima.core.models import Event
from anima.core.sandbox import SandboxKey


NOW = datetime(2026, 9, 15, tzinfo=timezone.utc)


def source():
    return Event(
        id="interaction-1", ts=NOW, kind="channel", channel_id="10",
        channel_name="#test", author_id="20", author_name="user", text="/command",
        guild_id="1", sandbox_key="guild:1",
    )


def context():
    return CommandContext(
        source(), SandboxKey("guild", "1"), "20",
        PermissionSet(frozenset({"manage_sandbox"})),
    )


class Modes:
    def __init__(self):
        self.mode = "proactive"

    def get(self, key):
        return self.mode

    def details(self, key):
        return {"mode": self.mode}

    def set(self, key, mode, **metadata):
        self.mode = mode
        self.metadata = metadata
        return {"mode": mode}


class CommandProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_maintenance_and_activity(self):
        actor = SimpleNamespace(
            force_maintenance=AsyncMock(return_value="sleep"),
            force_self_time=AsyncMock(return_value=(SimpleNamespace(action="finish"),)),
            cancel_pending_reactions=AsyncMock(),
        )
        maintenance = MaintenanceCommandProvider(actor)
        self.assertEqual(maintenance.commands()[0].permission, "manage_sandbox")
        result = await maintenance.execute_command(
            ("anima-maintenance",), {"kind": "sleep"}, context()
        )
        self.assertEqual(result.text, "睡眠を完了しました。")
        self.assertEqual(len(maintenance.commands()[0].parameters[0].choices), 4)
        self_time = await maintenance.execute_command(
            ("anima-maintenance",), {"kind": "self_time"}, context()
        )
        self.assertIn("Self Timeを1反復", self_time.text)
        actor.force_self_time.assert_awaited_once()

        modes = Modes()
        activity = ActivityCommandProvider(modes, actor, clock=lambda: NOW)
        self.assertEqual(len(activity.commands()[0].parameters[0].choices), 5)
        status = await activity.execute_command(
            ("anima-mode",), {"mode": "status"}, context()
        )
        self.assertIn("proactive", status.text)
        changed = await activity.execute_command(
            ("anima-mode",), {"mode": "reply"}, context()
        )
        actor.cancel_pending_reactions.assert_awaited_once()
        self.assertEqual(changed.actions[0].action, "set_mode")
        self.assertEqual(modes.metadata["changed_by"], "20")

    def test_helpers_and_command_event(self):
        self.assertIsNone(_optional_text("  "))
        self.assertEqual(_optional_text(3), "3")
        self.assertIn("完全に沈黙", _activity_message({"mode": "silent"}))
        from anima.capabilities.commands import CommandResult
        self.assertIsNone(command_event(CommandResult("ok"), source()))
        event = command_event(CommandResult(
            "ok", actions=(), references=(),
        ), source())
        self.assertIsNone(event)
        action_result = CommandResult(
            "ok", actions=(
                ActionRecord("music", "play_music", "再生"),
            ),
        )
        recorded = command_event(action_result, source())
        self.assertEqual(recorded.acts, ("play_music",))


if __name__ == "__main__":
    unittest.main()
