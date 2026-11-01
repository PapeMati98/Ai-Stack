from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    suite_root = Path(__file__).resolve().parents[2]
    script = suite_root / "model-radar" / "scripts" / "model_radar.py"
    return subprocess.call([sys.executable, str(script), "check"])


if __name__ == "__main__":
    raise SystemExit(main())
