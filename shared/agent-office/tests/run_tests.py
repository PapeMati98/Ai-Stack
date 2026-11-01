#!/usr/bin/env python3
"""Discover and run coordfs tests without changing the working directory."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


def main() -> int:
    tests_dir = Path(__file__).resolve().parent
    package_root = tests_dir.parent
    sys.path.insert(0, str(package_root))
    sys.path.insert(0, str(tests_dir))
    suite = unittest.TestLoader().discover(
        start_dir=str(tests_dir),
        pattern="test_*.py",
        top_level_dir=str(tests_dir),
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
