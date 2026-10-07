"""Core memory-retrieval contract and deterministic sandbox-local fallback."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import unicodedata
from typing import Protocol

from anima.capabilities.contracts import NativeToolSpec
from anima.core.telemetry import emit


MAX_DOCUMENTS = 500
MAX_BYTES = 4 * 1024 * 1024
MAX_PASSAGE_CHARS = 1_000
MAX_RECALL_CHARS = 3_000
TOKEN = re.compile(r"[^\W_]{2,}", re.UNICODE)


@dataclass(frozen=True, slots=True)
class MemoryQuery:
    text: str
    person_id: str | None = None
    conversation_id: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryPassage:
    source: str
    position: int
    text: str
    score: float


@dataclass(frozen=True, slots=True)
class MemoryRecall:
    passages: tuple[MemoryPassage, ...] = ()
    native_tool: NativeToolSpec | None = None


class MemoryRetriever(Protocol):
    async def prepare_recall(self, query: MemoryQuery) -> MemoryRecall: ...


class LocalMemoryRetriever:
    def __init__(self, sandbox_root: Path) -> None:
        self.root = sandbox_root

    async def prepare_recall(self, query: MemoryQuery) -> MemoryRecall:
        normalized_query = _normalize(query.text)
        if not normalized_query:
            return MemoryRecall()
        documents, truncated = self._documents()
        if truncated:
            emit("memory.local_recall.truncated")
        direct = {"memory/self.md", "memory/world.md"}
        if query.person_id:
            direct.add(f"memory/people/{query.person_id}.md")
        if query.conversation_id:
            direct.add(f"memory/channels/{query.conversation_id}.md")
        ranked = []
        for source, content in documents:
            for position, text in _passages(content):
                score = _score(
                    normalized_query, text,
                    person_match=bool(query.person_id and query.person_id in source),
                    conversation_match=bool(
                        query.conversation_id and query.conversation_id in source
                    ),
                )
                if score > 0 and source not in direct:
                    ranked.append(MemoryPassage(source, position, text, score))
        ranked.sort(key=lambda item: (-item.score, item.source, item.position))
        selected = []
        length = 0
        for passage in ranked:
            if len(selected) >= 5 or length + len(passage.text) > MAX_RECALL_CHARS:
                break
            selected.append(passage)
            length += len(passage.text)
        return MemoryRecall(tuple(selected))

    def search(self, text: str, *, limit: int = 5) -> tuple[MemoryPassage, ...]:
        """Search every local memory document, including normally injected files."""
        normalized = _normalize(text)
        if not normalized or limit < 1:
            return ()
        ranked = []
        documents, truncated = self._documents()
        if truncated:
            emit("memory.local_search.truncated")
        for source, content in documents:
            for position, passage in _passages(content):
                score = _score(
                    normalized, passage, person_match=False, conversation_match=False
                )
                if score > 0:
                    ranked.append(MemoryPassage(source, position, passage, score))
        ranked.sort(key=lambda item: (-item.score, item.source, item.position))
        return tuple(ranked[:limit])

    def _documents(self) -> tuple[list[tuple[str, str]], bool]:
        memory = self.root / "memory"
        if not memory.is_dir():
            return [], False
        result = []
        total = 0
        truncated = False
        for path in sorted(memory.rglob("*.md")):
            if len(result) >= MAX_DOCUMENTS:
                truncated = True
                break
            try:
                relative = path.relative_to(self.root).as_posix()
                size = path.stat().st_size
                if total + size > MAX_BYTES:
                    truncated = True
                    break
                result.append((relative, path.read_text(encoding="utf-8")))
                total += size
            except (OSError, UnicodeError, ValueError):
                continue
        return result, truncated


def _passages(content: str) -> tuple[tuple[int, str], ...]:
    heading = ""
    result = []
    for position, raw in enumerate(content.splitlines()):
        line = raw.strip()
        if line.startswith("#"):
            heading = line.lstrip("# ").strip()
        elif line.startswith("-"):
            text = (f"{heading}\n" if heading else "") + line
            result.append((position, text[:MAX_PASSAGE_CHARS]))
    return tuple(result)


def _score(query: str, passage: str, *, person_match: bool, conversation_match: bool) -> float:
    candidate = _normalize(passage)
    score = 100.0 if query in candidate else 0.0
    terms = tuple(dict.fromkeys(TOKEN.findall(query)))
    score += min(60, sum(20 for term in terms if term in candidate))
    left, right = _bigrams(query), _bigrams(candidate)
    if left and right:
        score += 40 * len(left & right) / len(left | right)
    if person_match:
        score += 30
    if conversation_match:
        score += 20
    if "※強" in passage:
        score += 5
    return score


def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _bigrams(value: str) -> set[str]:
    compact = "".join(value.split())
    return {compact[index:index + 2] for index in range(len(compact) - 1)}
