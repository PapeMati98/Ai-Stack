#!/usr/bin/env python3
"""Run the exact coordfs Windows acceptance load and print one JSON receipt."""

from __future__ import annotations

import argparse
import json
import platform
import socket
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from multiprocess_harness import (
    defender_status,
    run_atomic_file_stress,
    run_claim_stress,
    run_crash_ttl_recovery,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--claimers", type=int, default=100)
    parser.add_argument("--processes", type=int, default=2)
    parser.add_argument("--files", type=int, default=50)
    parser.add_argument("--iterations", type=int, default=1000)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="coordfs-full-stress-") as temp:
        root = Path(temp)
        home = root / "coord-home"
        cwd = root / "cwd"
        cwd.mkdir()
        defender = defender_status()
        claims = run_claim_stress(home, cwd, claimers=args.claimers)
        crash_ttl = run_crash_ttl_recovery(home, cwd)
        atomic_files = run_atomic_file_stress(
            root / "atomic-files",
            process_count=args.processes,
            file_count=args.files,
            iterations=args.iterations,
        )
        passed = bool(
            defender.get("active")
            and claims.get("passed")
            and crash_ttl.get("passed")
            and atomic_files.get("passed")
        )
        receipt = {
            "schema_version": 1,
            "test": "coordfs-full-multiprocess-acceptance",
            "executed_at": datetime.now(timezone.utc).isoformat(),
            "host": socket.gethostname(),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "defender": defender,
            "claims": claims,
            "crash_ttl": crash_ttl,
            "atomic_files": atomic_files,
            "tailscale_host_gate": "NOT_RUN_SEPARATE_GATE",
            "duration_s": round(time.monotonic() - started, 3),
            "status": "PASS" if passed else "FAIL",
        }
        print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
