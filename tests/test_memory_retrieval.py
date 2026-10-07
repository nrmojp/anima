from __future__ import annotations

import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from anima.core.memory import (
    LocalMemoryRetriever,
    MemoryQuery,
    _bigrams,
    _normalize,
    _passages,
    _score,
)


class LocalMemoryRetrieverTests(unittest.IsolatedAsyncioTestCase):
    async def test_ranked_recall_excludes_direct_documents(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            memory = root / "memory"
            (memory / "people").mkdir(parents=True)
            (memory / "channels").mkdir()
            (memory / "self.md").write_text("# 自分\n- オレンジが好き", encoding="utf-8")
            (memory / "people" / "current.md").write_text(
                "# 本人\n- オレンジジュースが好き", encoding="utf-8"
            )
            (memory / "people" / "friend.md").write_text(
                "# 友達\n- オレンジジュースが大好き ※強\n- 猫も好き", encoding="utf-8"
            )
            (memory / "channels" / "room.md").write_text(
                "# 部屋\n- オレンジの話をした", encoding="utf-8"
            )
            recall = await LocalMemoryRetriever(root).prepare_recall(MemoryQuery(
                "オレンジジュース", person_id="current", conversation_id="room"
            ))
            self.assertEqual(len(recall.passages), 1)
            self.assertEqual(recall.passages[0].source, "memory/people/friend.md")
            self.assertIn("友達", recall.passages[0].text)

    async def test_empty_missing_and_unreadable_documents(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            retriever = LocalMemoryRetriever(root)
            self.assertEqual((await retriever.prepare_recall(MemoryQuery(""))).passages, ())
            self.assertEqual((await retriever.prepare_recall(MemoryQuery("猫"))).passages, ())
            memory = root / "memory"
            memory.mkdir()
            broken = memory / "broken.md"
            broken.write_bytes(b"\xff")
            self.assertEqual((await retriever.prepare_recall(MemoryQuery("猫"))).passages, ())

    async def test_scan_and_result_limits_are_enforced(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            memory = root / "memory" / "people"
            memory.mkdir(parents=True)
            for index in range(7):
                (memory / f"p{index}.md").write_text(
                    f"# 人{index}\n- 猫が好き", encoding="utf-8"
                )
            recall = await LocalMemoryRetriever(root).prepare_recall(MemoryQuery("猫が好き"))
            self.assertEqual(len(recall.passages), 5)
            with patch("anima.core.memory.MAX_DOCUMENTS", 1):
                with self.assertLogs("anima.telemetry", level="INFO") as captured:
                    await LocalMemoryRetriever(root).prepare_recall(MemoryQuery("猫"))
            self.assertIn("memory.local_recall.truncated", "\n".join(captured.output))
            with patch("anima.core.memory.MAX_BYTES", 1):
                documents, truncated = LocalMemoryRetriever(root)._documents()
            self.assertEqual(documents, [])
            self.assertTrue(truncated)

    def test_helpers_cover_matching_and_limits(self):
        self.assertEqual(_normalize(" Ａ  B\n"), "a b")
        self.assertEqual(_bigrams("猫"), set())
        self.assertEqual(_bigrams("猫 犬"), {"猫犬"})
        self.assertEqual(_passages("前文\n# 見出し\n- 内容\n本文"), ((2, "見出し\n- 内容"),))
        strong = _score("オレンジ", "オレンジ ※強", person_match=True, conversation_match=True)
        self.assertGreaterEqual(strong, 155)
        self.assertEqual(_score("猫", "犬", person_match=False, conversation_match=False), 0)


if __name__ == "__main__":
    unittest.main()
