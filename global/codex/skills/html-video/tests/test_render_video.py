#!/usr/bin/env python3
"""
Test runner shim and discovery endpoint for html-video skill.
Imports and executes tests from scripts/test_render_video.py while ensuring
strict no-cleanup retention and compatibility with unittest discovery.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure bytecode is never written to disk
sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

import importlib.util

TESTS_DIR = Path(__file__).resolve().parent
SKILL_ROOT = TESTS_DIR.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

# Load canonical test module from scripts using a unique module name to prevent circular import under unittest discovery
_canonical_script_path = SCRIPTS_DIR / "test_render_video.py"
_spec = importlib.util.spec_from_file_location("_canonical_test_render_video", _canonical_script_path)
if _spec is None or _spec.loader is None:
    raise ImportError(f"Cannot load test module from {_canonical_script_path}")
_canonical_mod = importlib.util.module_from_spec(_spec)
sys.modules["_canonical_test_render_video"] = _canonical_mod
_spec.loader.exec_module(_canonical_mod)

# Re-export all test classes, helpers, and entrypoint for unittest discovery and direct invocation
for _attr in dir(_canonical_mod):
    if _attr.startswith("Test") or _attr in ("main", "set_artifact_root", "get_fixture_dir"):
        globals()[_attr] = getattr(_canonical_mod, _attr)

if __name__ == "__main__":
    sys.exit(main())
