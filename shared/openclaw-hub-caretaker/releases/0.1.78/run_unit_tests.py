"""Bounded unittest runner for isolated contractor verification."""
from __future__ import annotations

import sys
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _workspace_mkdtemp(suffix=None, prefix=None, dir=None):
    """Keep test fixtures writable and confined to this isolated checkout."""
    parent = Path(dir).resolve() if dir is not None else ROOT / '.test-tmp'
    if not parent.is_relative_to(ROOT):
        raise ValueError('test temporary directory outside caretaker')
    parent.mkdir(parents=True, exist_ok=True)
    for _ in range(100):
        path = parent / (str(prefix or 'tmp') + uuid.uuid4().hex + str(suffix or ''))
        try:
            # tempfile's Windows mode 0700 ACL excludes the workspace sandbox.
            path.mkdir()
            return str(path)
        except FileExistsError:
            pass
    raise OSError('cannot allocate test temporary directory')


def main() -> int:
    tempfile.tempdir = str(ROOT / '.test-tmp')
    tempfile.mkdtemp = _workspace_mkdtemp
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=str(ROOT / "tests"), pattern="test_*.py", top_level_dir=str(ROOT))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
