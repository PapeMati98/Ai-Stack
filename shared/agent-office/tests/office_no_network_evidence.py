"""Emit a fresh isolated-process receipt for Office's no-network boundary."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from hashlib import sha256
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(TESTS_ROOT))

from office_support import McpClient  # noqa: E402


BOUND_SNAPSHOT_FILES = (
    "shared/agent-office/coordfs/projects.py",
    "shared/agent-office/tests/test_office_projects.py",
    "shared/agent-office/tests/office_no_network_evidence.py",
    "shared/agent-office/tests/office_no_network_probe.py",
    "shared/agent-office/tests/test_office_no_network.py",
)


def snapshot_file_hashes() -> list[dict[str, object]]:
    files: list[dict[str, object]] = []
    for relative in BOUND_SNAPSHOT_FILES:
        path = REPO_ROOT / Path(relative)
        files.append(
            {
                "path": relative,
                "sha256": sha256(path.read_bytes()).hexdigest(),
                "size_bytes": path.stat().st_size,
            }
        )
    return files


def socket_probe() -> dict[str, object]:
    probe = TESTS_ROOT / "office_no_network_probe.py"
    expected_probe_sha256 = sha256(probe.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix="office-net-probe-") as tmp:
        env = os.environ.copy()
        env["COORDFS_HOME"] = str(Path(tmp) / "coord-home")
        completed = subprocess.run(
            [sys.executable, "-I", str(probe)],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        receipt = json.loads(completed.stdout)
        return {
            "python_isolated": bool(receipt.get("python_isolated")),
            "python_version": receipt.get("python_version"),
            "probe_sha256": receipt.get("probe_sha256"),
            "probe_hash_matches": receipt.get("probe_sha256") == expected_probe_sha256,
            "child_exit_code": completed.returncode,
            "child_stderr_empty": not completed.stderr,
            "receipt": receipt,
        }


def process_probe() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="office-net-process-") as tmp:
        env = os.environ.copy()
        env["COORDFS_HOME"] = str(Path(tmp) / "coord-home")
        alice = McpClient("net-alice", env, ttl_s=5)
        bob = McpClient("net-bob", env, ttl_s=5)
        try:
            alice.handshake()
            bob.handshake()
            alice.call("office_send", {"to": "net-bob", "body": "netstat evidence"})
            bob.call("office_read")
            alive_before_netstat = alice.returncode is None and bob.returncode is None
            time.sleep(0.2)
            listing = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            pids = {str(alice.pid), str(bob.pid)}
            lines = listing.stdout.splitlines()
            offending = [line for line in lines if line.split() and line.split()[-1] in pids]
            return {
                "mcp_pids": sorted(pids),
                "netstat_exit_code": listing.returncode,
                "netstat_line_count": len(lines),
                "children_alive_before_netstat": alive_before_netstat,
                "children_alive_after_netstat": alice.returncode is None and bob.returncode is None,
                "sample_bound_to_live_children": alive_before_netstat
                and alice.returncode is None
                and bob.returncode is None,
                "matching_endpoint_count": len(offending),
                "offending_lines": offending,
            }
        finally:
            alice.close()
            bob.close()


def main() -> int:
    receipt = {
        "snapshot_files": snapshot_file_hashes(),
        "socket_probe": socket_probe(),
        "process_probe": process_probe(),
    }
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
