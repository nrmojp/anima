"""Generic environment parsing shared by hosts and optional plugin launchers."""

import os
from pathlib import Path

class ConfigurationError(ValueError):
    pass

def load_dotenv(path: Path, *, environ: dict[str, str] | None = None) -> dict[str, str]:
    target = os.environ if environ is None else environ
    if not path.exists():
        return target
    for number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigurationError(f"{path}:{number}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value[:1] in {"'", '"'}:
            if len(value) < 2 or value[-1] != value[0]:
                raise ConfigurationError(f"{path}:{number}: unclosed quote")
            value = value[1:-1]
        target.setdefault(key, value)
    return target
