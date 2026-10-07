from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from anima.core.models import Event
from anima.adapters.openai.embeddings import OpenAIEmbeddingProvider
from anima.core.vector_index import RetrievalDocument, VectorIndex


class FakeEmbeddings:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [[float("春" in text), float("猫" in text), 0.1] for text in texts]


class FakePlayer:
    def __init__(self) -> None:
        self.commands = []

    async def handle(self, source, command):
        self.commands.append((source, command))
        return "『猫の歌』を流すね。"


class RetrievalTests(unittest.IsolatedAsyncioTestCase):
    async def test_embedding_adapter_restores_input_order(self):
        create = AsyncMock(return_value=SimpleNamespace(data=[
            SimpleNamespace(index=1, embedding=[0, 1]),
            SimpleNamespace(index=0, embedding=[1, 0]),
        ]))
        provider = OpenAIEmbeddingProvider(SimpleNamespace(embeddings=SimpleNamespace(create=create)),
                                           model='test-model', dimensions=2)
        self.assertEqual(await provider.embed(['a', 'b']), [[1, 0], [0, 1]])
        create.assert_awaited_once_with(model='test-model', input=['a', 'b'], dimensions=2,
                                        encoding_format='float')

    async def test_cache_reuse_invalidation_and_corruption_rebuild(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'index.json'
            documents = (RetrievalDocument('cat', '猫', {}),)
            provider = FakeEmbeddings()
            index = VectorIndex(path, provider, identity='test')
            self.assertTrue(await index.ensure(documents))
            self.assertFalse(await index.ensure(documents))
            self.assertEqual(len(provider.calls), 1)
            valid = json.loads(path.read_text())
            corruptions = ['{', json.dumps({**valid, 'fingerprint': 'different'}),
                           json.dumps({**valid, 'vectors': []}),
                           json.dumps({**valid, 'documents': [{}]}),
                           json.dumps({**valid, 'vectors': [[None]]})]
            for content in corruptions:
                with self.subTest(content=content):
                    path.write_text(content)
                    fresh = VectorIndex(path, provider, identity='test')
                    self.assertTrue(await fresh.ensure(documents))
                    self.assertEqual(json.loads(path.read_text())['documents'][0]['id'], 'cat')

    async def test_invalid_provider_vectors_are_not_persisted(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'index.json'
            documents = (RetrievalDocument('cat', '猫', {}),)
            for vectors, exception in (([], RuntimeError), ([[0, 0]], ValueError)):
                with self.subTest(vectors=vectors):
                    provider = SimpleNamespace(embed=AsyncMock(return_value=vectors))
                    with self.assertRaises(exception):
                        await VectorIndex(path, provider, identity='test').ensure(documents)
                    self.assertFalse(path.exists())

    async def test_vector_index_persists_documents_and_ranks_query(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            provider = FakeEmbeddings()
            documents = (
                RetrievalDocument("spring", "春の明るい歌", {"title": "春"}),
                RetrievalDocument("cat", "猫の速い歌", {"title": "猫"}),
            )
            index_path = Path(temporary) / "index.json"
            index = VectorIndex(index_path, provider, identity="test")

            results = await index.search("猫っぽい", documents, limit=1)

            self.assertEqual(results[0].document.id, "cat")
            self.assertEqual(len(provider.calls), 2)  # document batch + query
            cached_provider = FakeEmbeddings()
            cached = VectorIndex(index_path, cached_provider, identity="test")
            await cached.ensure(documents)
            self.assertEqual(cached_provider.calls, [])
