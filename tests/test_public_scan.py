from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from tempfile import TemporaryDirectory
import os
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "scripts" / "check_public_tree.py"
SPEC = spec_from_file_location("check_public_tree", SCRIPT)
assert SPEC and SPEC.loader
scanner = module_from_spec(SPEC)
SPEC.loader.exec_module(scanner)


class PublicScanTests(unittest.TestCase):
    def test_blank_secret_examples_do_not_consume_the_next_line(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / ".env.example"
            path.write_text("DISCORD_BOT_TOKEN=\nOPENAI_API_KEY= \nANIMA_DM_ENABLED=false\n", encoding="utf-8")
            self.assertEqual(scanner.scan(root, (path,)), ())
            path.write_text("DISCORD_BOT_TOKEN= value\n", encoding="utf-8")
            self.assertTrue(scanner.scan(root, (path,)))

    def test_runtime_files_are_rejected_even_without_private_content(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "state" / "runtime" / "status.json"
            path.parent.mkdir(parents=True)
            path.write_text("{}", encoding="utf-8")
            self.assertIn("runtime or secret file", scanner.scan(root, (path,))[0])

    def test_clean_files(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "README.md"
            path.write_text("Public text", encoding="utf-8")
            self.assertEqual(scanner.scan(root, (path,)), ())

    def test_detects_private_artifacts(self):
        with TemporaryDirectory() as directory, patch.dict(
            os.environ, {"PUBLIC_SCAN_DENY_TERMS": "private-person"}
        ):
            root = Path(directory)
            files = {
                ".env": "OPENAI_API_KEY=secret\n",
                "state/memory.md": "private-person and 123456789012345678\n",
                "voice/model.aivmx": "binary-ish",
                "key.txt": "-----BEGIN PRIVATE KEY-----\n",
                "pointer.txt": "version https://git-lfs.github.com/spec/v1\n",
            }
            paths = []
            for name, content in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
                paths.append(path)
            findings = scanner.scan(root, tuple(paths))
            joined = "\n".join(findings)
            for expected in ("secret", "runtime", "media", "private key", "Discord ID", "LFS", "private-person"):
                self.assertIn(expected, joined)

    def test_ignores_unreadable_binary(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "data.bin"
            path.write_bytes(b"\xff\xfe")
            self.assertEqual(scanner.scan(root, (path,)), ())


if __name__ == "__main__":
    unittest.main()
