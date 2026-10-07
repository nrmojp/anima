"""Trusted sandbox identities and filesystem boundaries (no Discord dependency)."""

from dataclasses import dataclass
from pathlib import Path
import re


@dataclass(frozen=True)
class SandboxKey:
    kind: str
    id: str

    def __post_init__(self):
        normalized = {"discord_guild": "guild", "discord_dm": "dm"}.get(
            self.kind, self.kind
        )
        object.__setattr__(self, "kind", normalized)
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", normalized):
            raise ValueError("sandbox namespace is invalid")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", self.id):
            raise ValueError("sandbox identifier is invalid")
        if normalized in {"guild", "dm"} and not self.id.isdecimal():
            raise ValueError("Discord sandbox identifiers must be numeric")

    def __str__(self):
        return f"{self.kind}:{self.id}"

    @property
    def namespace(self) -> str:
        """Transport-neutral namespace used by generic Anima contracts."""
        return {"guild": "discord_guild", "dm": "discord_dm"}.get(
            self.kind, self.kind
        )

    @property
    def identifier(self) -> str:
        """Opaque identifier alias used by generic Anima contracts."""
        return self.id

    @classmethod
    def parse(cls, value):
        kind, _, identifier = value.partition(":")
        kind = {"discord_guild": "guild", "discord_dm": "dm"}.get(kind, kind)
        return cls(kind, identifier)

    @classmethod
    def for_event(cls, event):
        if event.sandbox_key is not None:
            key = cls.parse(event.sandbox_key)
        elif event.kind == "channel" and event.guild_id:
            key = cls("guild", event.guild_id)
        elif event.kind == "dm" and event.guild_id is None:
            if event.author_id == "self":
                key = cls.parse(event.sandbox_key or "")
                if key.kind != "dm":
                    raise ValueError("DM response requires DM sandbox")
            else:
                key = cls("dm", event.author_id)
        else:
            raise ValueError("event has no trusted sandbox")
        if event.kind == "channel" and event.guild_id and key != cls("guild", event.guild_id):
            raise ValueError("event sandbox mismatch")
        if event.kind == "dm" and event.author_id == "self" and key.kind != "dm":
            raise ValueError("DM response requires DM sandbox")
        if event.kind == "dm" and event.author_id != "self" and key != cls("dm", event.author_id):
            raise ValueError("event sandbox mismatch")
        return key

    def path(self, root: Path):
        directory = {"guild": "guilds", "dm": "dms"}.get(self.kind, self.kind)
        target = root / "sandboxes" / directory / self.id
        reject_symlinks(target, root)
        return target


def reject_symlinks(path: Path, root: Path):
    """Reject symlinked components, including the supplied root itself."""
    if not path.is_relative_to(root):
        raise ValueError("path escapes sandbox root")
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError("symlinks are not allowed in sandbox paths")
        if part == root:
            break


LEGACY_NAMES = (
    "cursor.json", "mood.md", "habitus.md", "digest.md", "open.md", "memory",
    "log", "handled", "deleted", "archive", "world", ".anima-transaction.json", ".inbox-v1",
)


def legacy_entries(root: Path):
    return [name for name in LEGACY_NAMES if (root / name).exists() or (root / name).is_symlink()]


def require_separated_layout(root: Path):
    if legacy_entries(root):
        raise RuntimeError("Unassigned legacy state detected; sandbox migration is required before starting")


def list_sandboxes(root: Path):
    keys = []
    base = root / "sandboxes"
    reject_symlinks(base, root)
    entries = [("guild", "guilds"), ("dm", "dms")]
    if base.exists():
        entries.extend((path.name, path.name) for path in sorted(base.iterdir()) if path.name not in {"guilds", "dms", ".DS_Store"})
    for kind, directory in entries:
        parent = root / "sandboxes" / directory
        reject_symlinks(parent, root)
        if parent.exists():
            if not parent.is_dir():
                raise ValueError("sandbox namespace must be a directory")
            for path in sorted(parent.iterdir()):
                if path.name == ".DS_Store":
                    continue
                key = SandboxKey(kind, path.name)
                key.path(root)
                if not path.is_dir():
                    raise ValueError("sandbox entry must be a directory")
                keys.append(key)
    return keys
