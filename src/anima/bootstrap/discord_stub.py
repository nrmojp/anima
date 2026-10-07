"""One-shot live harness for the Discord interface adapter."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
import json
import logging
from pathlib import Path
from zoneinfo import ZoneInfo


from anima.adapters.stub.discord import StubDiscordAdapter, StubDiscordSender
from anima.bootstrap.app import build_sandbox
from anima.bootstrap.settings import Settings
from anima.capabilities.plugin_loader import PluginLoader
from anima.core.access import ActivityModeStore, ActivityPolicy
from anima.core.sandbox import SandboxKey
from anima.core.sandbox_runtime import SandboxRouter


JST = ZoneInfo("Asia/Tokyo")


class _TelemetryCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.records: list[dict[str, object]] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            value = json.loads(record.getMessage())
        except (TypeError, ValueError):
            return
        if isinstance(value, dict):
            self.records.append(value)


@contextmanager
def _capture_telemetry():
    logger = logging.getLogger("anima.telemetry")
    previous_level = logger.level
    handler = _TelemetryCapture()
    logger.addHandler(handler)
    if not logger.isEnabledFor(logging.INFO):
        logger.setLevel(logging.INFO)
    try:
        yield handler.records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


def _usage(events: list[dict[str, object]]) -> dict[str, object]:
    rounds = [
        {
            "round": int(item.get("round", 0) or 0),
            "input_tokens": int(item.get("input_tokens", 0) or 0),
            "output_tokens": int(item.get("output_tokens", 0) or 0),
            "total_tokens": int(item.get("total_tokens", 0) or 0),
        }
        for item in events
        if item.get("event") == "openai.response.round_completed"
        and item.get("operation") == "respond"
    ]
    completed = next((
        item for item in reversed(events)
        if item.get("event") == "openai.response.completed"
        and item.get("operation") == "respond"
    ), None)
    return {
        "available": completed is not None,
        "input_tokens": int((completed or {}).get("input_tokens", 0) or 0),
        "output_tokens": int((completed or {}).get("output_tokens", 0) or 0),
        "total_tokens": int((completed or {}).get("total_tokens", 0) or 0),
        "request_count": int((completed or {}).get("request_count", 0) or 0),
        "tool_call_count": int((completed or {}).get("tool_call_count", 0) or 0),
        "rounds": rounds,
    }


async def run_discord_stub(
    settings: Settings,
    text: str,
    *,
    state_root: Path,
    guild_id: str,
    attachments: tuple[Path, ...] = (),
) -> dict[str, object]:
    """Run one real model turn through a local Discord-shaped adapter."""
    key = SandboxKey("guild", guild_id)
    isolated = replace(
        settings,
        state_root=state_root.resolve(),
        allowed_guild_ids=frozenset({guild_id}),
        dm_enabled=False,
        enable_web_search=False,
        plugins=frozenset(),
    )
    now = lambda: datetime.now(JST)
    sender = StubDiscordSender(clock=now)
    activity_modes = ActivityModeStore(isolated.state_root)
    plugins = PluginLoader().load()
    api_limit = asyncio.Semaphore(4)
    router = SandboxRouter(
        isolated.state_root,
        lambda selected, root: build_sandbox(
            isolated, selected, root, sender, activity_modes, plugins, api_limit, lambda: None,
        ),
        policy=ActivityPolicy(frozenset({guild_id})),
    )
    adapter = StubDiscordAdapter(router, isolated.state_root, clock=now)
    await router.start()
    try:
        with _capture_telemetry() as telemetry:
            event, outcome = await adapter.submit(
                text, sandbox=key, attachments=attachments,
            )
        runtime = await router.runtime(key)
        inventory = runtime.actor.context_builder.inventory
        return {
            "warning": "This live stub called the configured OpenAI model and may incur usage.",
            "sandbox": str(key),
            "event_id": event.id,
            "outcome": outcome.kind.value,
            "reply": outcome.reply,
            "usage": _usage(telemetry),
            "deliveries": [item.to_dict() for item in sender.deliveries],
            "reactions": [list(item) for item in sender.reactions],
            "faces": [list(item) for item in sender.faces],
            "inventory": [
                item.to_dict() for item in inventory.list(include_temporary=True)
            ],
        }
    finally:
        await router.stop()
