#!/usr/bin/env python3
"""Mirrored coord hook entry. Safe under `python -I` without an install."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from coordfs.hook import hook_main


if __name__ == "__main__":
    raise SystemExit(hook_main())
