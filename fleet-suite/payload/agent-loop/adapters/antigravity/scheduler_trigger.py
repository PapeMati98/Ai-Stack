"""Zero-write Task Scheduler contract builder for the Antigravity target."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import TypedDict

LOOP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")

#: Environment override for the interpreter pinned into the scheduled action.
INTERPRETER_ENV = "AGENT_LOOP_PYTHON"


class TriggerContract(TypedDict):
    task_name: str
    command: str
    argv: list[str]
    schedule_type: str
    interval_minutes: int
    writes_performed: int
    interpreter: str
    interpreter_source: str
    warnings: list[str]


def _interpreter_warnings(interpreter: Path, source: str) -> list[str]:
    """
    A scheduled task outlives the process that proposed it. Pinning an
    environment-dependent interpreter (venv, temp dir) produces a task that
    silently breaks when that environment disappears.
    """
    warnings: list[str] = []
    if not interpreter.exists():
        warnings.append(f"interpreter does not exist: {interpreter}")
    for parent in (interpreter.parent, *interpreter.parents):
        if (parent / "pyvenv.cfg").exists():
            warnings.append(
                f"interpreter lives in a virtual environment ({parent}); "
                "pass python_executable or set "
                f"{INTERPRETER_ENV} to a stable system interpreter"
            )
            break
    lowered = str(interpreter).lower()
    for marker in (os.sep + "temp" + os.sep, os.sep + "tmp" + os.sep):
        if marker in lowered:
            warnings.append(f"interpreter path looks temporary: {interpreter}")
            break
    if source == "sys.executable":
        warnings.append(
            "contract is not reproducible: it depends on which interpreter "
            "happened to build it"
        )
    return warnings


class SchedulerContractError(Exception):
    """Raised when the dry-run scheduler contract is invalid."""


def build_scheduler_contract(
    project_path: str,
    loop_id: str,
    cadence_minutes: int,
    dry_run: bool = True,
    *,
    controller_path: str | None = None,
    python_executable: str | None = None,
) -> TriggerContract:
    if not dry_run:
        raise SchedulerContractError(
            "V0.1 supports only dry_run=True for Task Scheduler"
        )
    if (
        isinstance(cadence_minutes, bool)
        or not isinstance(cadence_minutes, int)
        or cadence_minutes <= 0
    ):
        raise SchedulerContractError("Cadence minutes must be positive")
    if not LOOP_ID_RE.fullmatch(loop_id):
        raise SchedulerContractError("Invalid loop_id")
    if any(character in project_path for character in "\r\n\x00"):
        raise SchedulerContractError("Invalid project path")

    controller = (
        Path(controller_path).expanduser().resolve(strict=False)
        if controller_path
        else Path(__file__).resolve().parents[2] / "scripts" / "agent_loop.py"
    )
    if any(character in str(controller) for character in "\r\n\x00"):
        raise SchedulerContractError("Invalid controller path")
    project = Path(project_path).expanduser().resolve(strict=False)

    # Interpreter resolution: explicit > environment > current process.
    # `sys.executable` is only a last resort because a scheduled task must not
    # depend on whichever interpreter happened to build this contract.
    env_interpreter = os.environ.get(INTERPRETER_ENV)
    if python_executable:
        interpreter, interpreter_source = Path(python_executable), "explicit"
    elif env_interpreter:
        interpreter, interpreter_source = (
            Path(env_interpreter),
            f"env:{INTERPRETER_ENV}",
        )
    else:
        interpreter, interpreter_source = Path(sys.executable), "sys.executable"
    interpreter = interpreter.expanduser().resolve(strict=False)
    if any(character in str(interpreter) for character in "\r\n\x00"):
        raise SchedulerContractError("Invalid interpreter path")

    action_argv = [
        str(interpreter),
        str(controller),
        "run",
        "--project",
        str(project),
        "--loop",
        loop_id,
        "--report-only",
    ]
    task_name = f"AgentLoop_{loop_id}"
    argv = [
        "schtasks.exe",
        "/create",
        "/tn",
        task_name,
        "/tr",
        subprocess.list2cmdline(action_argv),
        "/sc",
        "MINUTE",
        "/mo",
        str(cadence_minutes),
        "/f",
    ]
    return {
        "task_name": task_name,
        "command": subprocess.list2cmdline(argv),
        "argv": argv,
        "schedule_type": "MINUTE",
        "interval_minutes": cadence_minutes,
        "writes_performed": 0,
        "interpreter": str(interpreter),
        "interpreter_source": interpreter_source,
        "warnings": _interpreter_warnings(interpreter, interpreter_source),
    }
