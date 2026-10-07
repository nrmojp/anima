"""Offline, sandbox-local snapshots with verified, non-overwriting restoration."""

import hashlib
import json
from pathlib import Path, PurePosixPath
import tempfile
import zipfile

from anima.core.sandbox import SandboxKey, reject_symlinks


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def backup(root: Path, key: SandboxKey, output: Path) -> dict:
    source = key.path(root)
    if not source.is_dir():
        raise ValueError("sandbox does not exist")
    if output.resolve().is_relative_to(root.resolve()):
        raise ValueError("backup must be outside the state root")
    entries = {}
    for path in sorted(source.rglob("*")):
        reject_symlinks(path, root)
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("only regular files can be backed up")
        entries[path.relative_to(source).as_posix()] = path.read_bytes()
    manifest = {"version": 1, "sandbox": str(key),
                "files": {name: _digest(data) for name, data in entries.items()}}
    # Exclusive creation protects an earlier snapshot; mode protects private memories.
    with output.open("xb") as stream:
        output.chmod(0o600)
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
            for name, data in entries.items():
                archive.writestr("data/" + name, data)
    return {"sandbox": str(key), "files": len(entries), "archive": str(output)}


def restore(root: Path, key: SandboxKey, archive_path: Path) -> dict:
    target = key.path(root)
    if target.exists():
        raise FileExistsError("restore target already exists; preserve it before restoring")
    with zipfile.ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("version") != 1 or manifest.get("sandbox") != str(key):
            raise ValueError("backup version or sandbox mismatch")
        files = manifest.get("files")
        if not isinstance(files, dict):
            raise ValueError("invalid backup manifest")
        names = archive.namelist()
        if len(names) != len(set(names)) or set(names) != {"manifest.json", *("data/" + n for n in files)}:
            raise ValueError("unexpected or duplicate archive entries")
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".restore-", dir=target.parent) as staging:
            prepared = Path(staging) / "sandbox"
            prepared.mkdir()
            for name, digest in files.items():
                path = PurePosixPath(name)
                if not name or path.is_absolute() or ".." in path.parts or "\\" in name or path.as_posix() != name:
                    raise ValueError("unsafe archive path")
                data = archive.read("data/" + name)
                if _digest(data) != digest:
                    raise ValueError("backup checksum mismatch")
                destination = prepared / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
            prepared.rename(target)
    return {"sandbox": str(key), "files": len(files), "restored": str(target)}
