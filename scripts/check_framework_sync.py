"""Read-only verification of the vendored Anima framework and optional peer."""

import argparse
import hashlib
import json
from pathlib import Path


GROUPS = ("core", "capabilities", "adapters", "bootstrap")
SUFFIXES = {".py", ".js", ".css", ".html"}


def inventory(root: Path) -> dict[str, bytes]:
    files = {}
    for group in GROUPS:
        for path in (root / "src/anima" / group).rglob("*"):
            if "__pycache__" in path.parts or path.suffix not in SUFFIXES or not path.is_file():
                continue
            if path.is_symlink():
                raise ValueError("framework files must not be symlinks")
            files[path.relative_to(root).as_posix()] = path.read_bytes()
    for name in ("__init__.py", "py.typed"):
        path = root / "src/anima" / name
        if not path.is_file() or path.is_symlink():
            raise ValueError("framework package marker is missing or unsafe")
        files[path.relative_to(root).as_posix()] = path.read_bytes()
    return files


def fingerprint(files: dict[str, bytes]) -> dict[str, object]:
    digest = hashlib.sha256()
    for name, content in sorted(files.items()):
        digest.update(name.encode("utf-8") + b"\0" + hashlib.sha256(content).digest())
    return {"version": 1, "file_count": len(files), "sha256": digest.hexdigest()}


def verify(root: Path, peer: Path | None = None) -> list[str]:
    files = inventory(root)
    lock = json.loads((root / "framework-lock.json").read_text(encoding="utf-8"))
    problems = []
    if lock != fingerprint(files):
        problems.append("framework-lock.json does not match the common framework")
    if peer is not None:
        other = inventory(peer)
        for name in sorted(files.keys() | other.keys()):
            if files.get(name) != other.get(name):
                problems.append(name)
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--peer", type=Path)
    parser.add_argument("--fingerprint", action="store_true", help="Print a reviewed replacement lock; never writes files")
    args = parser.parse_args(argv)
    try:
        if args.fingerprint:
            print(json.dumps(fingerprint(inventory(args.root)), indent=2))
            return 0
        problems = verify(args.root, args.peer)
    except (OSError, ValueError) as error:
        print(f"Framework verification failed: {error}")
        return 1
    if problems:
        print("Framework mismatch:\n" + "\n".join(problems))
        return 1
    print("Common Anima framework verified" + (" against peer" if args.peer else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
