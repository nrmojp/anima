"""Read-only consistency and contamination checks for durable memory documents."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re


MEMORY_LINE = re.compile(r"^- \[\d{4}-\d{2}-\d{2} (?:DM|#[^\] ]+)(?: [^\]]+)?\] .+")
RELATIONSHIP_LINE = re.compile(r"^関係: .+")
RISK_MARKERS = (
    "ignore previous", "ignore all previous", "system prompt", "developer message",
    "前の指示を無視", "これまでの指示を無視", "システムプロンプト", "命令を上書き",
)


@dataclass(frozen=True, slots=True)
class AuditFinding:
    level: str
    path: str
    line: int
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "level": self.level, "path": self.path,
            "line": self.line, "reason": self.reason,
        }


class MemoryAuditor:
    """Inspect derived memory without changing it or the source journal."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def run(self) -> dict[str, object]:
        documents = self._documents()
        findings: list[AuditFinding] = []
        hashes: dict[str, str] = {}
        line_count = strong_count = relationship_count = 0
        for path in documents:
            relative = path.relative_to(self.root).as_posix()
            content = path.read_text(encoding="utf-8", errors="replace")
            hashes[relative] = sha256(content.encode("utf-8")).hexdigest()
            for number, raw in enumerate(content.splitlines(), 1):
                line = raw.strip()
                if not line:
                    continue
                line_count += 1
                lowered = line.casefold()
                if any(marker in lowered for marker in RISK_MARKERS):
                    findings.append(AuditFinding(
                        "warning", relative, number, "instruction-like text in derived memory",
                    ))
                if "※強" in line:
                    strong_count += 1
                if RELATIONSHIP_LINE.fullmatch(line):
                    relationship_count += 1
                if relative.startswith("memory/") and line.startswith("-") and not MEMORY_LINE.fullmatch(line):
                    findings.append(AuditFinding(
                        "error", relative, number, "memory entry has no valid date/source prefix",
                    ))
        return {
            "documents": len(documents), "lines": line_count,
            "strong_entries": strong_count, "relationships": relationship_count,
            "findings": [finding.to_dict() for finding in findings],
            "hashes": hashes,
        }

    def compare(self, baseline: dict[str, str]) -> dict[str, tuple[str, ...]]:
        current = self.run()["hashes"]
        assert isinstance(current, dict)
        before, after = set(baseline), set(current)
        return {
            "added": tuple(sorted(after - before)),
            "removed": tuple(sorted(before - after)),
            "changed": tuple(sorted(
                path for path in before & after if baseline[path] != current[path]
            )),
        }

    def _documents(self) -> tuple[Path, ...]:
        selected = [self.root / "habitus.md"]
        memory = self.root / "memory"
        if memory.is_dir():
            selected.extend(memory.rglob("*.md"))
        return tuple(path for path in sorted(selected) if path.is_file())
