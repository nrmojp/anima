"""Optional public deployment defaults; credentials stay in the environment."""

import json
from pathlib import Path


def deployment_defaults(base: Path) -> dict[str, str]:
    path = base / "deployment.json"
    if path.is_symlink():
        raise ValueError("unsafe deployment profile")
    if not path.exists():
        return {}
    if not path.is_file():
        raise ValueError("unsafe deployment profile")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) != {"environment"}:
        raise ValueError("invalid deployment profile")
    environment = value["environment"]
    if not isinstance(environment, dict):
        raise ValueError("invalid deployment environment")
    for key, item in environment.items():
        if (not isinstance(key, str) or not key.startswith(("ANIMA_", "AIVIS_", "OPENAI_"))
                or any(word in key for word in ("TOKEN", "SECRET", "API_KEY"))
                or not isinstance(item, str)):
            raise ValueError("deployment defaults must be public string settings")
    return dict(environment)
