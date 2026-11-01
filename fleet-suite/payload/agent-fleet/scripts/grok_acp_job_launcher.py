from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path


GATE_PATH_ENV = "AGENT_FLEET_GROK_JOB_GATE"
GATE_TOKEN_ENV = "AGENT_FLEET_GROK_JOB_TOKEN"
GATE_TIMEOUT_SECONDS = 15.0


def main() -> int:
    command = sys.argv[1:]
    if command[:1] == ["--"]:
        command = command[1:]
    gate_value = os.environ.pop(GATE_PATH_ENV, "")
    token = os.environ.pop(GATE_TOKEN_ENV, "")
    if not command or not gate_value or not token:
        return 78

    gate = Path(gate_value)
    deadline = time.monotonic() + GATE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            if gate.read_text(encoding="ascii") == token:
                break
        except (FileNotFoundError, OSError, UnicodeError):
            pass
        time.sleep(0.02)
    else:
        return 78

    try:
        child = subprocess.Popen(
            command,
            stdin=sys.stdin,
            stdout=sys.stdout,
            stderr=sys.stderr,
            env=os.environ.copy(),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except OSError:
        return 78
    return int(child.wait())


if __name__ == "__main__":
    raise SystemExit(main())
