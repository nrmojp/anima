import ast
import importlib
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).parents[1] / "src" / "anima"


class ArchitectureTests(unittest.TestCase):
    def test_compatibility_facades_import_without_launching_services(self):
        for path in ROOT.glob("*.py"):
            if path.stem == "__init__":
                continue
            with self.subTest(module=path.stem):
                importlib.import_module("anima." + path.stem)

    def test_dependency_direction(self):
        forbidden = {
            "core": ("anima.plugins", "anima.adapters", "anima.bootstrap"),
            "capabilities": ("anima.plugins", "anima.adapters"),
            "plugins": ("anima.adapters",),
        }
        for layer, prefixes in forbidden.items():
            for path in (ROOT / layer).rglob("*.py"):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                imports = []
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        imports.extend(alias.name for alias in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        imports.append(node.module)
                with self.subTest(path=path):
                    self.assertFalse(
                        [name for name in imports if name.startswith(prefixes)],
                        f"forbidden dependency in {path}",
                    )

    def test_plugins_are_self_contained(self):
        for plugin_root in (ROOT / "plugins").iterdir():
            if not plugin_root.is_dir():
                continue
            own_prefix = f"anima.plugins.{plugin_root.name}"
            for path in plugin_root.rglob("*.py"):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                invalid = []
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        names = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                        names = [node.module]
                    else:
                        continue
                    for name in names:
                        root = name.split(".", 1)[0]
                        allowed_anima = name.startswith(("anima.core", "anima.capabilities", own_prefix))
                        if root not in sys.stdlib_module_names and not allowed_anima:
                            invalid.append(name)
                with self.subTest(path=path):
                    self.assertFalse(invalid, f"plugin dependency escapes its directory: {path}")


if __name__ == "__main__":
    unittest.main()
