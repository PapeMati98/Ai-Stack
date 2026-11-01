"""Shared fixtures for coordfs tests."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from coordfs.leases import CoordBoard


class CoordTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "coord-home"
        self.cwd = Path(self.tmp.name) / "cwd"
        self.cwd.mkdir()
        self.clock = [1_700_000_000.0]
        self.board = CoordBoard(
            home=self.home,
            cwd=self.cwd,
            now=lambda: self.clock[0],
            events_max_bytes_value=4096,
            events_retain_value=3,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def advance(self, seconds: float) -> None:
        self.clock[0] += seconds
