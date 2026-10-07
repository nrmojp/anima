import unittest
from types import SimpleNamespace

from anima.core.context_usage import context_usage, serialized_size


class ContextUsageTests(unittest.TestCase):
    def test_images_are_separate_and_request_is_not_modified(self):
        import copy
        payload = "A" * 3497572
        request = {"input": [{"role": "user", "content": [
            {"type": "input_text", "text": "猫を見て"},
            {"type": "input_image", "image_url": "data:image/png;base64," + payload},
            {"type": "input_image", "image_url": "https://example.com/猫.png", "detail": "low"},
            {"type": "input_image", "file_id": "file-test"},
            {"type": "input_image", "image_url": None},
        ]}]}
        original = copy.deepcopy(request)
        result = context_usage(request)
        self.assertEqual(request, original)
        self.assertLess(result["total"], 500)
        self.assertGreater(result["wire_characters"], 3497572)
        self.assertEqual(result["measurement_version"], 2)
        self.assertEqual(result["images"], {
            "count": 4, "embedded_count": 1, "remote_count": 1,
            "payload_bytes": len(("data:image/png;base64," + payload).encode())
            + len("https://example.com/猫.png".encode()),
        })
        self.assertEqual(sum(row["total"] for row in result["components"]), result["total"])
        self.assertNotIn(payload, str(result))

    def test_empty_and_unicode_request(self):
        for request in ({}, {"input": "日本語", "tools": [{"type": "web_search"}]}):
            result = context_usage(request)
            self.assertEqual(sum(row["total"] for row in result["components"]), result["total"])
            self.assertEqual(result["components"][0]["plugin"], "core")
        self.assertEqual(serialized_size("猫"), 3)

    def test_attributes_only_sent_tools_and_response_properties(self):
        prepared = SimpleNamespace(context_owners={"play": "music", "unused": "drawing"},
                                   instruction_parts=(("music", "曲"), ("core", "共有")))
        response = SimpleNamespace(contributions=[SimpleNamespace(property_name="react"), SimpleNamespace(property_name="fallback")],
                                   routes={"react": SimpleNamespace(manifest=SimpleNamespace(name="reactions")), "fallback": object()})
        request = {"instructions": "曲共有", "tools": [{"name": "play"}],
                   "text": {"format": {"schema": {"properties": {"react": {}, "fallback": {}}}}}}
        result = context_usage(request, prepared, response)
        rows = {row["plugin"]: row for row in result["components"]}
        self.assertEqual(rows["music"]["tools"], serialized_size({"name": "play"}))
        self.assertEqual(rows["music"]["instructions"], 1)
        self.assertGreater(rows["reactions"]["response"], 0)
        self.assertNotIn("drawing", rows)
        self.assertEqual(sum(row["total"] for row in rows.values()), result["total"])
