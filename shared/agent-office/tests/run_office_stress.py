#!/usr/bin/env python3
"""Run the Agent Office inbox acceptance load and print one JSON receipt."""

from __future__ import annotations

import argparse
import json
import platform
import socket
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from office_stress_harness import run_inbox_stress  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--senders", type=int, default=100)
    parser.add_argument("--per-sender", type=int, default=10)
    args = parser.parse_args()
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="office-stress-") as temp:
        home = Path(temp) / "coord-home"
        result = run_inbox_stress(home, senders=args.senders, per_sender=args.per_sender)
        receipt = {
            "schema_version": 1,
            "test": "agent-office-inbox-multiprocess-acceptance",
            "executed_at": datetime.now(timezone.utc).isoformat(),
            "host": socket.gethostname(),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "inbox_stress": result,
            "duration_s": round(time.monotonic() - started, 3),
            "status": "PASS" if result["passed"] else "FAIL",
        }
        print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
