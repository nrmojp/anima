#!/usr/bin/env python3
"""Reject private/runtime artifacts before publishing the template."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess


MEDIA_SUFFIXES = {
    ".aivmx", ".flac", ".gif", ".jpeg", ".jpg", ".m4a", ".mp3", ".mp4",
    ".ogg", ".onnx", ".opus", ".png", ".safetensors", ".wav", ".webm",
}
SECRET_ASSIGNMENT = re.compile(
    r"(?im)^(?:DISCORD_BOT_TOKEN|OPENAI_API_KEY|[A-Z0-9_]*(?:SECRET|PASSWORD))[ \t]*=[ \t]*[^\s#]+"
)
DISCORD_ID = re.compile(r"(?<!\d)\d{17,20}(?!\d)")
PRIVATE_KEY = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
LFS_POINTER = "version https://git-lfs.github.com/spec/v1"
FIXTURE_PATHS = {Path("scripts/check_public_tree.py"), Path("tests/test_public_scan.py")}


def tracked_paths(root: Path) -> tuple[Path, ...]:
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True,
    )
    return tuple(root / item.decode() for item in result.stdout.split(b"\0") if item)


def scan(root: Path, paths: tuple[Path, ...] | None = None) -> tuple[str, ...]:
    findings: list[str] = []
    deny_terms = tuple(
        term.strip() for term in os.environ.get("PUBLIC_SCAN_DENY_TERMS", "").split(",")
        if term.strip()
    )
    for path in paths if paths is not None else tracked_paths(root):
        relative = path.relative_to(root)
        if relative.name == ".env" or relative.parts[:1] == ("state",) and relative.name != ".gitkeep":
            findings.append(f"runtime or secret file: {relative}")
        if path.suffix.lower() in MEDIA_SUFFIXES:
            findings.append(f"media or model file: {relative}")
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        contains_fixtures = relative in FIXTURE_PATHS
        if SECRET_ASSIGNMENT.search(text) and not contains_fixtures:
            findings.append(f"non-empty secret assignment: {relative}")
        if PRIVATE_KEY.search(text) and not contains_fixtures:
            findings.append(f"private key: {relative}")
        if DISCORD_ID.search(text) and not contains_fixtures:
            findings.append(f"probable Discord ID: {relative}")
        if LFS_POINTER in text and not contains_fixtures:
            findings.append(f"Git LFS pointer: {relative}")
        for term in deny_terms:
            if term.casefold() in text.casefold():
                findings.append(f"denied private term {term!r}: {relative}")
    return tuple(dict.fromkeys(findings))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    root = parser.parse_args().root.resolve()
    findings = scan(root)
    if findings:
        print("Public tree scan failed:")
        for finding in findings:
            print(f"- {finding}")
        return 1
    print(f"Public tree scan passed ({len(tracked_paths(root))} tracked files).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
