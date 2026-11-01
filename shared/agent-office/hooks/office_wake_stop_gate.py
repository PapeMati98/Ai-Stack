"""Claude Code UserPromptSubmit gate: drop an Office self-pull prompt while Wake STOP is active.

A self-pull timer lives in the session's harness Cron, where Office cannot delete it. When a
timer confirmed before STOP fires after STOP, the model would spend a full turn only to learn
`action=stop`. This hook runs before the model: for the exact self-pull prompt Wake issues it
asks the Office CLI for `wake-stop-status` (local or remote, whatever route the installed
wrapper uses) and exits 2 -- which rejects and erases the prompt, so no model turn happens --
when STOP is active or its state cannot be confirmed (the same fail-closed rule Wake applies).

Any other prompt, including one that quotes or extends the template, returns exit 0 without
starting a process. The hook writes nothing. After START the chain is resumed with
`wake-pull-arm --lost` once CronList shows the fired job is gone.

Usage (settings.json, command hook, UserPromptSubmit):
    python -I <runtime>/hooks/office_wake_stop_gate.py --office <runtime>/office.cmd
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

# The hook ships inside the Office runtime (runtime/hooks/), next to coordfs: the template is
# taken from Wake itself, never copied, so the gate cannot drift from what Wake issues.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

MAX_STATUS_OUTPUT = 64 * 1024
STATUS_TIMEOUT_S = 12.0
MARKER = "[Office Wake self-pull]"
# Candidate fields only (binding id limit as Wake: 128); the decision is an exact comparison
# with Wake._pull_prompt for these fields.
_CANDIDATE = re.compile(
    r"\[Office Wake self-pull\] Uruchom `office wake-pull --binding-id "
    r"(?P<binding>[a-z0-9][a-z0-9._-]{0,127}) --token (?P<token>[0-9a-f]{32})`"
)

Runner = Callable[[Sequence[str]], "tuple[int, bytes]"]


def _issued_prompt(binding: str, token: str) -> str | None:
    try:
        from coordfs.wake import Wake
    except Exception:  # noqa: BLE001 - no Office code: the prompt cannot be verified
        return None
    return Wake._pull_prompt(binding, token)


def self_pull_binding(prompt: Any) -> str | None:
    """Binding id when the prompt IS the Office self-pull prompt (exact text), else None."""
    if not isinstance(prompt, str) or not prompt.startswith(MARKER):
        return None
    match = _CANDIDATE.match(prompt)
    if match is None:
        return None
    issued = _issued_prompt(match.group("binding"), match.group("token"))
    # Without the Office code next to the hook the exact prompt cannot be verified (and the
    # STOP state could not be read either): never guess from a prefix, which would block an
    # ordinary prompt that quotes the command. The pull itself still stops under STOP.
    if issued is None or prompt.rstrip("\r\n") != issued:
        return None
    return match.group("binding")


def status_command(office: Path, home: Path | None) -> list[str]:
    if not office.is_absolute():
        # No executable search (PATH/CWD): the gate runs inside arbitrary checkouts.
        raise ValueError("--office must be an absolute path")
    if office.suffix.lower() == ".py":
        command = [sys.executable, "-I", str(office)]
    else:
        command = [str(office)]  # office.cmd: constant arguments only, no prompt text
    if home is not None:
        command += ["--home", str(home)]
    return command + ["wake-stop-status"]


def _kill(process: "subprocess.Popen[bytes]") -> None:
    process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _creationflags() -> int:
    """CREATE_NO_WINDOW on Windows so the status child never flashes a console."""
    if sys.platform == "win32":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return 0


def _run(command: Sequence[str]) -> tuple[int, bytes]:
    """(exit code, stdout) under a hard byte bound; stderr is discarded, never buffered."""
    process = subprocess.Popen(
        list(command), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, shell=False, creationflags=_creationflags(),
    )
    chunks: list[bytes] = []

    def read() -> None:
        assert process.stdout is not None
        chunks.append(process.stdout.read(MAX_STATUS_OUTPUT + 1))

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    reader.join(STATUS_TIMEOUT_S)
    if reader.is_alive():
        _kill(process)
        raise subprocess.TimeoutExpired(list(command), STATUS_TIMEOUT_S)
    raw = chunks[0] if chunks else b""
    if len(raw) > MAX_STATUS_OUTPUT:
        _kill(process)
        raise ValueError("office status output exceeds its bound")
    try:
        code = process.wait(timeout=STATUS_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        _kill(process)
        raise
    return code, raw


def stop_state(command: Sequence[str], runner: Runner = _run) -> bool | None:
    """True/False when Office confirms the STOP state, None when it cannot be confirmed."""
    try:
        returncode, raw = runner(command)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if returncode != 0 or not isinstance(raw, bytes) or len(raw) > MAX_STATUS_OUTPUT:
        return None
    try:
        payload = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return None
    active = payload.get("stop_active")
    return active if isinstance(active, bool) else None


def prompt_binding(hook_input: bytes) -> str | None:
    try:
        data = json.loads(hook_input.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return self_pull_binding(data.get("prompt") if isinstance(data, dict) else None)


def decide(hook_input: bytes, command: Sequence[str], runner: Runner = _run) -> tuple[int, str]:
    """(exit code, stderr message). Exit 2 rejects the prompt before any model turn."""
    binding = prompt_binding(hook_input)
    if binding is None:
        return 0, ""
    active = stop_state(command, runner)
    if active is False:
        return 0, ""
    why = "Office Wake STOP jest aktywny" if active else "stanu Office Wake STOP nie da sie potwierdzic"
    return 2, (
        f"[office-wake-stop-gate] Self-pull bindingu {binding} zatrzymany przed tura modelu: {why}. "
        "Nic nie wykonano. Po START: CronList, potem wake-pull-arm --lost dla tego bindingu."
    )


def main(argv: list[str] | None = None, *, stdin: Any = None, runner: Runner = _run) -> int:
    parser = argparse.ArgumentParser(description="Office Wake STOP gate for Claude Code UserPromptSubmit.")
    parser.add_argument("--office", type=Path, required=True,
                        help="Absolute path to the installed office.cmd (or office.py).")
    parser.add_argument("--home", type=Path, help="Explicit coord home (tests / synthetic homes only).")
    args = parser.parse_args(argv)
    stream = stdin if stdin is not None else sys.stdin.buffer
    # The harness writes this input and already holds the whole prompt in memory; reading it
    # whole means no size or padding trick can move a self-pull past the check.
    hook_input = stream.read()
    binding = prompt_binding(hook_input)
    if binding is None:
        return 0  # ordinary prompt: no process, no I/O
    try:
        command = status_command(args.office, args.home)
    except ValueError as exc:
        sys.stderr.write(f"[office-wake-stop-gate] {exc}; self-pull bindingu {binding} zatrzymany.\n")
        return 2
    code, message = decide(hook_input, command, runner)
    if message:
        sys.stderr.write(message + "\n")
    return code


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    raise SystemExit(main())
