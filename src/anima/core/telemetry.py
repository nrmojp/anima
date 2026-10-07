"""Small JSON logging helpers for operational events."""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any


LOGGER = logging.getLogger("anima.telemetry")
sandbox_context = ContextVar("sandbox_key", default=None)


def emit(name: str, **fields: Any) -> None:
    if sandbox_context.get() is not None:
        fields["sandbox_key"] = sandbox_context.get()
    LOGGER.info(
        json.dumps(
            {"ts": datetime.now(timezone.utc).isoformat(), "event": name, **fields},
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
    )
