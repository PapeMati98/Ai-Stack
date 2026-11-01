"""Bounded manual entry point for the maintenance tick.

This module is deliberately a wrapper, not a scheduler.  It gives one
maintenance invocation a task-owned temporary directory and turns degraded
or blocked reports into a non-zero process result for an external operator.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import maintenance


_ENV_KEYS = (
    "TEMP",
    "TMP",
    "PYTHONDONTWRITEBYTECODE",
    "HUB_CARETAKER_ALLOW_EXEC",
)


def _fatal_payload(exc: BaseException) -> Dict[str, Any]:
    """Return exception metadata without serializing exception text."""
    error_type = re.sub(r"[^A-Za-z0-9_.-]", "_", type(exc).__name__)
    return {
        "status": "BLOCKED",
        "wrapper": "scheduled_tick",
        "error_type": error_type or "UnknownError",
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def _write_fatal(state: Path, payload: Dict[str, Any]) -> None:
    """Persist redacted fatal metadata where maintenance reports live."""
    try:
        state.mkdir(parents=True, exist_ok=True)
        (state / "latest.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2,
                       sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except (OSError, UnicodeError, TypeError, ValueError):
        # The JSON stdout result remains the operator-facing fallback when
        # the state directory itself is unavailable.
        return


def _is_degraded_or_blocked(value: Any) -> bool:
    """Inspect report status fields, including per-project child reports."""
    if isinstance(value, dict):
        for key, child in value.items():
            if (str(key).casefold() == "status"
                    and isinstance(child, str)):
                status = child.strip().upper()
                if status == "DEGRADED" or "BLOCKED" in status:
                    return True
            if _is_degraded_or_blocked(child):
                return True
        return False
    if isinstance(value, list):
        return any(_is_degraded_or_blocked(item) for item in value)
    return False


def _restore_environment(saved: Dict[str, Optional[str]]) -> None:
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def run(control_path: str | os.PathLike[str]) -> tuple[Dict[str, Any], int]:
    """Run one maintenance tick and return its redacted report and exit code."""
    state: Optional[Path] = None
    saved = {key: os.environ.get(key) for key in _ENV_KEYS}
    try:
        path = Path(control_path)
        control = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(control, dict):
            raise ValueError("control document must be an object")
        raw_state = control.get("state_dir")
        if not isinstance(raw_state, str) or not raw_state.strip() \
                or "\0" in raw_state:
            raise ValueError("control state_dir must be a non-empty path")
        state = Path(raw_state).resolve()
        state.mkdir(parents=True, exist_ok=True)
        task_temp = state / "task-temp" / uuid.uuid4().hex
        task_temp.mkdir(parents=True, exist_ok=True)
        os.environ.update({
            "TEMP": str(task_temp),
            "TMP": str(task_temp),
            "PYTHONDONTWRITEBYTECODE": "1",
            "HUB_CARETAKER_ALLOW_EXEC": "1",
        })
        try:
            result = maintenance.tick(str(path))
        except Exception as exc:
            result = _fatal_payload(exc)
            _write_fatal(state, result)
            return result, 1
        if not isinstance(result, dict):
            result = _fatal_payload(ValueError("maintenance result"))
            result["error_type"] = "InvalidTickResult"
            _write_fatal(state, result)
            return result, 1
        return result, 1 if _is_degraded_or_blocked(result) else 0
    except Exception as exc:
        result = _fatal_payload(exc)
        if state is not None:
            _write_fatal(state, result)
        return result, 1
    finally:
        _restore_environment(saved)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scheduled-tick",
        description="Run one bounded maintenance tick; no scheduler.",
    )
    parser.add_argument("--control", required=True,
                        help="Path to the owner-configured control JSON.")
    args = parser.parse_args(argv)
    result, code = run(args.control)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
