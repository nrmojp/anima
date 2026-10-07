"""Face vocabulary and validated application emoji mappings."""

import json
import re
from pathlib import Path

FACE_NAMES = ("ふつう", "喜", "怒", "哀", "楽")
FACE_GUIDANCE = (
    "faceはふつう・喜・怒・哀・楽から選ぶ。喜はうれしい・祝福、楽は楽しい・面白い、"
    "哀は悲しい・共感。怒は怒りや抗議で、表現の程度はペルソナ設定に従う。"
    "ふつうと反応しないことは別。"
)
EMOJI = re.compile(r"<(?P<animated>a?):(?P<name>[A-Za-z0-9_]{2,32}):(?P<id>[0-9]+)>")


class FaceCatalog:
    def __init__(self, faces=None, default="ふつう"):
        self.faces = dict(faces) if faces is not None else dict.fromkeys(FACE_NAMES)
        if set(self.faces) != set(FACE_NAMES) or default not in FACE_NAMES:
            raise ValueError("faces must contain ふつう・喜・怒・哀・楽 and a valid default")
        for value in self.faces.values():
            if value is not None and (not isinstance(value, str) or not EMOJI.fullmatch(value)):
                raise ValueError("face must be an application emoji markup or null")
        self.default = default
        self.available = {}

    @classmethod
    def load(cls, path: Path):
        if not path.exists():
            return cls()
        value = json.loads(path.read_text(encoding="utf-8"))
        return cls(value["faces"], value["default"])

    def validate_registered(self, emojis):
        registered = {str(emoji) for emoji in emojis}
        self.available = {name: value for name, value in self.faces.items() if value in registered}
        return {name: "ready" if name in self.available else "unregistered" for name in self.faces}

    def emoji(self, face):
        return self.available.get(face)
