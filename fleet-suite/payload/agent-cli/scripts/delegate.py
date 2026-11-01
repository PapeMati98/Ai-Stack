from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    script = Path(__file__).with_name("delegate.ps1")
    return subprocess.call(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            *sys.argv[1:],
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())
