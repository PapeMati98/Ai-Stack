"""Read-only discovery through the canonical agent-loop validator."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, TypedDict

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import agent_loop  # noqa: E402
import security_contract as security  # noqa: E402


class LoopManifest(TypedDict):
    loop_id: str
    maturity: str
    cadence: dict[str, Any] | None
    valid: bool
    error: str | None


def discover_loops(project_root: Path) -> dict[str, LoopManifest]:
    try:
        resolved_root = project_root.expanduser().resolve(strict=True)
    except FileNotFoundError:
        return {}
    loops_dir = resolved_root / ".agents" / "loops"
    if not loops_dir.is_dir():
        return {}
    security.assert_no_reparse_chain(resolved_root, loops_dir.resolve(strict=True))

    discovered: dict[str, LoopManifest] = {}
    for loop_dir in sorted(loops_dir.iterdir()):
        if loop_dir.name.startswith(".") or not loop_dir.is_dir():
            continue
        if not (loop_dir / "loop.json").is_file():
            continue
        try:
            resolved_loop = loop_dir.resolve(strict=True)
            security.assert_no_reparse_chain(resolved_root, resolved_loop)
            paths, config = agent_loop.load_validated(resolved_root, loop_dir.name)
            discovered[loop_dir.name] = {
                "loop_id": paths.root.name,
                "maturity": str(config["contract"]["maturity"]),
                "cadence": dict(config["contract"]["cadence"]),
                "valid": True,
                "error": None,
            }
        except Exception as exc:
            discovered[loop_dir.name] = {
                "loop_id": loop_dir.name,
                "maturity": "unknown",
                "cadence": None,
                "valid": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
    return discovered
