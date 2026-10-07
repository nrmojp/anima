"""Reusable local vector retrieval infrastructure with persisted embeddings."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any, Protocol

@dataclass(frozen=True, slots=True)
class RetrievalDocument:
    id: str
    text: str
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    document: RetrievalDocument
    score: float


class EmbeddingProvider(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class VectorIndex:
    """Dataset-agnostic cosine index persisted as JSON on local disk."""

    def __init__(
        self,
        path: Path,
        provider: EmbeddingProvider,
        *,
        identity: str,
    ) -> None:
        self.path = path
        self.provider = provider
        self.identity = identity
        self._documents: tuple[RetrievalDocument, ...] = ()
        self._vectors: tuple[tuple[float, ...], ...] = ()
        self._fingerprint = ""
        self._lock = asyncio.Lock()

    async def ensure(self, documents: tuple[RetrievalDocument, ...]) -> bool:
        fingerprint = _fingerprint(self.identity, documents)
        async with self._lock:
            if self._fingerprint == fingerprint and self._documents:
                return False
            cached = self._load(fingerprint)
            if cached is not None:
                self._documents, self._vectors = cached
                self._fingerprint = fingerprint
                return False
            vectors = await self.provider.embed([document.text for document in documents])
            if len(vectors) != len(documents):
                raise RuntimeError("embedding provider returned the wrong vector count")
            self._documents = documents
            self._vectors = tuple(_normalize(vector) for vector in vectors)
            self._fingerprint = fingerprint
            self._save(fingerprint)
            return True

    async def search(
        self,
        query: str,
        documents: tuple[RetrievalDocument, ...],
        *,
        limit: int = 5,
    ) -> tuple[RetrievalResult, ...]:
        await self.ensure(documents)
        query_vectors = await self.provider.embed([query])
        query_vector = _normalize(query_vectors[0])
        ranked = sorted(
            (
                RetrievalResult(document, sum(a * b for a, b in zip(vector, query_vector)))
                for document, vector in zip(self._documents, self._vectors)
            ),
            key=lambda result: result.score,
            reverse=True,
        )
        return tuple(ranked[: max(1, limit)])

    def _load(
        self, fingerprint: str
    ) -> tuple[tuple[RetrievalDocument, ...], tuple[tuple[float, ...], ...]] | None:
        if not self.path.exists():
            return None
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if value.get("fingerprint") != fingerprint:
                return None
            documents = tuple(
                RetrievalDocument(item["id"], item["text"], item["metadata"])
                for item in value["documents"]
            )
            vectors = tuple(tuple(float(x) for x in vector) for vector in value["vectors"])
            if len(documents) != len(vectors):
                return None
            return documents, vectors
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def _save(self, fingerprint: str) -> None:
        value = {
            "schema_version": 1,
            "identity": self.identity,
            "fingerprint": fingerprint,
            "documents": [
                {"id": item.id, "text": item.text, "metadata": item.metadata}
                for item in self._documents
            ],
            "vectors": self._vectors,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temporary, self.path)


def _fingerprint(identity: str, documents: tuple[RetrievalDocument, ...]) -> str:
    payload = json.dumps(
        {
            "identity": identity,
            "documents": [
                {"id": item.id, "text": item.text, "metadata": item.metadata}
                for item in documents
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _normalize(vector: list[float]) -> tuple[float, ...]:
    magnitude = math.sqrt(sum(value * value for value in vector))
    if magnitude == 0:
        raise ValueError("embedding vector must not be zero")
    return tuple(value / magnitude for value in vector)
