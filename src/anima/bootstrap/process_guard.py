"""Foreground process ownership and previous unclean-shutdown detection."""

from __future__ import annotations

from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
from typing import Callable, TextIO


class AlreadyRunningError(RuntimeError):
    pass


class ProcessLock:
    """Hold one advisory lock for the complete lifetime of the foreground Bot."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._stream: TextIO | None = None

    def acquire(self) -> None:
        if self._stream is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            stream.seek(0)
            owner = stream.read().strip() or "unknown"
            stream.close()
            raise AlreadyRunningError(f"Anima Bot is already running (pid {owner})") from error
        stream.seek(0)
        stream.truncate()
        stream.write(str(os.getpid()))
        stream.flush()
        os.fsync(stream.fileno())
        self._stream = stream

    def release(self) -> None:
        if self._stream is None:
            return
        fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        self._stream.close()
        self._stream = None

    def __enter__(self) -> ProcessLock:
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


def mark_previous_unclean_shutdown(
    status_path: Path,
    *,
    now: datetime,
    pid_exists: Callable[[int], bool],
) -> bool:
    """Record a stale connected process at the next manual startup."""
    if not status_path.exists():
        return False
    try:
        value = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    if not isinstance(value, dict):
        return False
    pid = value.get("pid")
    if not value.get("connected") or not isinstance(pid, int) or pid_exists(pid):
        return False
    value["connected"] = False
    value["last_unclean_shutdown_at"] = now.isoformat()
    value["last_unclean_shutdown_pid"] = pid
    temporary = status_path.with_suffix(".startup.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, status_path)
    return True
