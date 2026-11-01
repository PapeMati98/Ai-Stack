"""Test helpers."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
FIXTURES = HERE / "fixtures"


def config_path() -> str:
    return str(ROOT / "config.example.json")


def state_dir() -> str:
    return str(ROOT / ".tmp_state")


def write_config(tmp: Path) -> Path:
    raw = json.loads(Path(config_path()).read_text("utf-8"))
    raw["state_dir"] = str(tmp)
    raw["inputs"] = [
        {"path": str(FIXTURES / "codex_session.jsonl"),
         "kind": "session_jsonl", "tool": "codex"},
        {"path": str(FIXTURES / "openclaw_session.jsonl"),
         "kind": "session_jsonl", "tool": "openclaw"},
        {"path": str(FIXTURES / "claude_session.jsonl"),
         "kind": "session_jsonl", "tool": "claude"},
        {"path": str(FIXTURES / "normalized_event.jsonl"),
         "kind": "normalized_events", "tool": "openclaw"},
    ]
    target = tmp / "config.json"
    target.write_text(json.dumps(raw, indent=2), "utf-8")
    return target


def load_config(tmp: Path):
    from caretaker.config import load_config
    return load_config(write_config(tmp))
