"""Conservative preservation of damaged durable state."""

import errno
import json
import os
from pathlib import Path
import shutil
import tempfile
from datetime import datetime
from anima.core.models import Mood


def require_space(directory: Path, required: int = 0) -> None:
    if shutil.disk_usage(directory).free < required + 1024 * 1024:
        raise OSError(errno.ENOSPC, "insufficient disk space (1 MiB reserve)", str(directory))


def preserve(path: Path, root: Path) -> Path:
    directory = root / "runtime" / "quarantine"
    directory.mkdir(parents=True, exist_ok=True)
    require_space(directory, path.stat().st_size)
    with tempfile.NamedTemporaryFile(prefix=path.name + ".", dir=directory, delete=False) as stream:
        destination = Path(stream.name)
        with path.open("rb") as source:
            shutil.copyfileobj(source, stream)
        stream.flush()
        os.fsync(stream.fileno())
    return destination


def validate_state(path: Path, root: Path) -> None:
    if not path.exists():
        return
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("expected object")
        if path.name == "cursor.json":
            if type(value.get("version")) is not int or value["version"] < 0:
                raise ValueError("invalid cursor version")
            for name in ("digested_until", "slept_at", "reflected_at", "last_spoke_at", "last_seen_at"):
                if value.get(name) is not None:
                    datetime.fromisoformat(value[name])
        else:
            for name in ("state", "cause", "strength", "focus", "since"):
                if not isinstance(value.get(name), str):
                    raise ValueError("invalid mood field")
            if value["strength"] not in ("弱い", "ふつう", "強い"):
                raise ValueError("invalid mood strength")
            datetime.fromisoformat(value["since"])
            Mood.from_dict(value)
    except (ValueError, TypeError) as error:
        saved = preserve(path, root)
        raise ValueError(f"damaged state: {path}; preserved at {saved}; repair or restore required") from error


def recover_jsonl_tail(path: Path, root: Path) -> None:
    data = path.read_bytes()
    if not data or data.endswith(b"\n"):
        return
    boundary = data.rfind(b"\n") + 1
    try:
        json.loads(data[boundary:])
    except (ValueError, UnicodeDecodeError):
        # Verify preceding records first; never silently discard middle corruption.
        for line in data[:boundary].splitlines():
            if line.strip():
                json.loads(line)
        preserve(path, root)
        replacement = data[:boundary]
    else:
        replacement = data + b"\n"
    require_space(path.parent, len(replacement))
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(replacement)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
