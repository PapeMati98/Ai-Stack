#!/usr/bin/env python3
"""Fixed SSH forced-command entry point; no shell or caller command evaluation."""

from __future__ import annotations

import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

from coordfs.office_ssh_gate import main

if __name__ == "__main__":
    raise SystemExit(main())
