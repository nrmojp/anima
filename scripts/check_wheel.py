"""Check the explicitly allowed contents of an Anima wheel."""

from pathlib import Path
import sys
from zipfile import ZipFile


def check_wheel(path: Path) -> None:
    with ZipFile(path) as archive:
        names = set(archive.namelist())
    required = {
        "anima/__init__.py", "anima/py.typed",
        "anima/capabilities/plugin_loader.py",
        "anima/adapters/dashboard/assets/index.html",
        "anima/adapters/dashboard/assets/dashboard.js",
        "anima/adapters/dashboard/assets/dashboard.css",
    }
    if missing := required - names:
        raise ValueError(f"wheel is missing required files: {sorted(missing)}")
    metadata_roots = {name.split('/')[0] for name in names if '.dist-info/' in name}
    if len(metadata_roots) != 1 or not next(iter(metadata_roots)).startswith('anima-'):
        raise ValueError("wheel metadata must belong to Anima")
    metadata_root = next(iter(metadata_roots))
    for name in names:
        parts = Path(name).parts
        if '..' in parts or name.startswith('/'):
            raise ValueError(f"unsafe wheel path: {name}")
        if parts[0] == metadata_root:
            continue
        if parts[0] != 'anima':
            raise ValueError(f"unexpected wheel member: {name}")
        if name in required or name.endswith('.py'):
            continue
        raise ValueError(f"unexpected package data: {name}")


if __name__ == '__main__':
    check_wheel(Path(sys.argv[1]))
    print('Anima wheel contents passed.')
