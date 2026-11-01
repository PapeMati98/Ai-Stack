#!/usr/bin/env python3
"""Mirrored office CLI entry. Safe under `python -I` without an install."""

from __future__ import annotations

import sys
from pathlib import Path

# `-I` implies `-E`, so PYTHONDONTWRITEBYTECODE cannot be honoured by the
# interpreter here, and the installer snapshots every byte of the runtime tree:
# any coordfs/__pycache__ written during the import below reads back as DRIFT.
sys.dont_write_bytecode = True

sys.path.insert(0, str(Path(__file__).resolve().parent))

from coordfs.office_cli import main

if __name__ == "__main__":
    raise SystemExit(main())
