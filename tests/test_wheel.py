import importlib.util
from pathlib import Path
import tempfile
import unittest
import runpy
from unittest.mock import patch
from zipfile import ZipFile

spec = importlib.util.spec_from_file_location('wheel_check', Path(__file__).parents[1] / 'scripts/check_wheel.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class WheelTests(unittest.TestCase):
    base = (
        'anima/__init__.py', 'anima/py.typed',
        'anima/capabilities/plugin_loader.py',
        'anima/adapters/dashboard/assets/index.html',
        'anima/adapters/dashboard/assets/dashboard.js',
        'anima/adapters/dashboard/assets/dashboard.css',
        'anima/adapters/dashboard/assets/messages.json',
        'anima/core/actor.py', 'anima-0.1.0.dist-info/METADATA',
    )

    def check(self, names):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'test.whl'
            with ZipFile(path, 'w') as archive:
                for name in names:
                    archive.writestr(name, '')
            module.check_wheel(path)

    def test_valid(self):
        self.check(self.base)

    def test_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'test.whl'
            with ZipFile(path, 'w') as archive:
                for name in self.base:
                    archive.writestr(name, '')
            with patch('sys.argv', ['check_wheel.py', str(path)]), patch('builtins.print') as output:
                runpy.run_path(str(Path(__file__).parents[1] / 'scripts/check_wheel.py'), run_name='__main__')
                output.assert_called_once_with('Anima wheel contents passed.')

    def test_missing(self):
        with self.assertRaisesRegex(ValueError, 'missing required'):
            self.check(self.base[1:])
        with self.assertRaisesRegex(ValueError, 'messages.json'):
            self.check(tuple(name for name in self.base if not name.endswith('messages.json')))

    def test_metadata(self):
        for names in (self.base[:-1], (*self.base[:-1], 'other-1.dist-info/METADATA'), (*self.base, 'anima-2.dist-info/METADATA')):
            with self.subTest(names=names), self.assertRaisesRegex(ValueError, 'metadata'):
                self.check(names)

    def test_disallowed_members(self):
        for name in ('bot/persona.md', 'anima/.env', 'anima/state.json', '../escape.py', '/absolute.py'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.check((*self.base, name))
