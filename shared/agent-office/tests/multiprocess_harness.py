"""Windows multiprocessing acceptance harness for coordfs atomicity."""

from __future__ import annotations

import json
import multiprocessing
import os
import queue
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from coordfs.atomic import exclusive_create, unlink_with_backoff
from coordfs.leases import CoordBoard


def _claim_worker(
    home: str,
    cwd: str,
    resource: str,
    index: int,
    ttl_s: int,
    ready: Any,
    start: Any,
    results: Any,
) -> None:
    ready.put(index)
    if not start.wait(90):
        results.put({"index": index, "pid": os.getpid(), "error": "start-timeout"})
        return
    try:
        board = CoordBoard(
            home=Path(home),
            cwd=Path(cwd),
            events_max_bytes_value=32 * 1024 * 1024,
            events_retain_value=1,
        )
        result = board.claim(
            resource,
            f"stress-{socket.gethostname().lower()}-session-{index}",
            ttl_s=ttl_s,
            meta={"harness": "multiprocess-stress", "session": str(index)},
        )
        results.put(
            {
                "index": index,
                "pid": os.getpid(),
                "status": result.get("status"),
                "code": result.get("code"),
                "holder_agent": result.get("holder_agent"),
            }
        )
    except BaseException as exc:  # pragma: no cover - returned to parent as evidence
        results.put(
            {
                "index": index,
                "pid": os.getpid(),
                "error": f"{type(exc).__name__}: {exc}",
            }
        )


def _atomic_file_worker(
    root: str,
    worker_id: int,
    file_count: int,
    iterations: int,
    ready: Any,
    start: Any,
    results: Any,
) -> None:
    ready.put(worker_id)
    if not start.wait(90):
        results.put({"worker_id": worker_id, "pid": os.getpid(), "error": "start-timeout"})
        return
    attempts = 0
    successes = 0
    collisions = 0
    corruptions = 0
    unexpected: list[str] = []
    started = time.monotonic()
    root_path = Path(root)
    for iteration in range(iterations):
        for file_index in range(file_count):
            attempts += 1
            path = root_path / f"atomic-{file_index:03d}.lock"
            token = f"{worker_id}:{os.getpid()}:{iteration}:{file_index}".encode("ascii")
            try:
                exclusive_create(path, token)
            except FileExistsError:
                collisions += 1
                continue
            except BaseException as exc:  # pragma: no cover - returned to parent as evidence
                if len(unexpected) < 50:
                    unexpected.append(f"create:{type(exc).__name__}:{getattr(exc, 'winerror', None)}:{exc}")
                continue
            successes += 1
            try:
                if path.read_bytes() != token:
                    corruptions += 1
                    if len(unexpected) < 50:
                        unexpected.append(f"token-mismatch:{iteration}:{file_index}")
                if not unlink_with_backoff(path, timeout_s=1.0):
                    if len(unexpected) < 50:
                        unexpected.append(f"cleanup-timeout:{iteration}:{file_index}")
            except BaseException as exc:  # pragma: no cover - returned to parent as evidence
                if len(unexpected) < 50:
                    unexpected.append(f"cleanup:{type(exc).__name__}:{getattr(exc, 'winerror', None)}:{exc}")
    results.put(
        {
            "worker_id": worker_id,
            "pid": os.getpid(),
            "attempts": attempts,
            "successes": successes,
            "collisions": collisions,
            "corruptions": corruptions,
            "unexpected": unexpected,
            "duration_s": round(time.monotonic() - started, 3),
        }
    )


def _collect_ready(ready: Any, count: int, timeout_s: float) -> list[int]:
    deadline = time.monotonic() + timeout_s
    values: list[int] = []
    while len(values) < count:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            values.append(int(ready.get(timeout=remaining)))
        except queue.Empty:
            break
    return values


def _collect_results(results: Any, count: int, timeout_s: float) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout_s
    values: list[dict[str, Any]] = []
    while len(values) < count:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            values.append(dict(results.get(timeout=remaining)))
        except queue.Empty:
            break
    return values


def _finish_processes(processes: list[Any], timeout_s: float) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout_s
    for process in processes:
        process.join(timeout=max(0.0, deadline - time.monotonic()))
    timed_out: list[dict[str, Any]] = []
    for process in processes:
        if process.is_alive():
            timed_out.append({"pid": process.pid, "name": process.name})
            process.terminate()
            process.join(timeout=5)
    return timed_out


def run_claim_stress(
    home: Path,
    cwd: Path,
    *,
    claimers: int = 100,
    ttl_s: int = 120,
    timeout_s: float = 240.0,
) -> dict[str, Any]:
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    results = context.Queue()
    resource = "data:coordfs-multiprocess-acceptance"
    processes = [
        context.Process(
            target=_claim_worker,
            args=(str(home), str(cwd), resource, index, ttl_s, ready, start, results),
            name=f"coord-claimer-{index:03d}",
        )
        for index in range(claimers)
    ]
    started = time.monotonic()
    for process in processes:
        process.start()
    ready_values = _collect_ready(ready, claimers, timeout_s / 2)
    start.set()
    values = _collect_results(results, claimers, timeout_s)
    process_timeouts = _finish_processes(processes, 30.0)
    winners = [item for item in values if item.get("status") == "ok"]
    warnings = [item for item in values if item.get("status") == "warning"]
    errors = [item for item in values if "error" in item]
    return {
        "claimers": claimers,
        "ready": len(ready_values),
        "result_count": len(values),
        "unique_pids": len({item.get("pid") for item in values if item.get("pid")}),
        "winner_count": len(winners),
        "warning_count": len(warnings),
        "errors": errors,
        "missing_results": claimers - len(values),
        "process_timeouts": process_timeouts,
        "winner": winners[0] if len(winners) == 1 else None,
        "duration_s": round(time.monotonic() - started, 3),
        "passed": (
            len(ready_values) == claimers
            and len(values) == claimers
            and len(winners) == 1
            and len(warnings) == claimers - 1
            and not errors
            and not process_timeouts
        ),
    }


def run_atomic_file_stress(
    root: Path,
    *,
    process_count: int = 2,
    file_count: int = 50,
    iterations: int = 1000,
    timeout_s: float = 1800.0,
) -> dict[str, Any]:
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    results = context.Queue()
    root.mkdir(parents=True, exist_ok=True)
    processes = [
        context.Process(
            target=_atomic_file_worker,
            args=(str(root), index, file_count, iterations, ready, start, results),
            name=f"coord-atomic-{index}",
        )
        for index in range(process_count)
    ]
    started = time.monotonic()
    for process in processes:
        process.start()
    ready_values = _collect_ready(ready, process_count, min(timeout_s / 2, 120.0))
    start.set()
    values = _collect_results(results, process_count, timeout_s)
    process_timeouts = _finish_processes(processes, 30.0)
    worker_errors = [item for item in values if "error" in item]
    attempts = sum(int(item.get("attempts", 0)) for item in values)
    collisions = sum(int(item.get("collisions", 0)) for item in values)
    successes = sum(int(item.get("successes", 0)) for item in values)
    corruptions = sum(int(item.get("corruptions", 0)) for item in values)
    unexpected = [error for item in values for error in item.get("unexpected", [])]
    unexpected_count = len(unexpected) + len(worker_errors) + len(process_timeouts)
    expected_attempts = process_count * file_count * iterations
    rate = unexpected_count / expected_attempts if expected_attempts else 1.0
    leftovers = sorted(path.name for path in root.glob("atomic-*.lock"))
    return {
        "process_count": process_count,
        "file_count": file_count,
        "iterations": iterations,
        "expected_attempts": expected_attempts,
        "attempts": attempts,
        "successes": successes,
        "file_exists_collisions": collisions,
        "unexpected_error_count": unexpected_count,
        "unexpected_error_rate": rate,
        "unexpected_samples": unexpected[:50],
        "corruptions": corruptions,
        "leftover_files": leftovers,
        "worker_results": values,
        "worker_errors": worker_errors,
        "ready": len(ready_values),
        "unique_pids": len({item.get("pid") for item in values if item.get("pid")}),
        "process_timeouts": process_timeouts,
        "duration_s": round(time.monotonic() - started, 3),
        "passed": (
            len(ready_values) == process_count
            and len(values) == process_count
            and attempts == expected_attempts
            and rate <= 0.02
            and corruptions == 0
            and not leftovers
            and not process_timeouts
        ),
    }


def run_crash_ttl_recovery(home: Path, cwd: Path, *, ttl_s: int = 1) -> dict[str, Any]:
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    results = context.Queue()
    resource = "data:coordfs-crash-ttl"
    process = context.Process(
        target=_claim_worker,
        args=(str(home), str(cwd), resource, 9001, ttl_s, ready, start, results),
        name="coord-crash-holder",
    )
    process.start()
    ready_count = len(_collect_ready(ready, 1, 30.0))
    start.set()
    process.join(timeout=30)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
    child_results = _collect_results(results, 1, 5.0)
    time.sleep(ttl_s + 0.25)
    board = CoordBoard(home=home, cwd=cwd)
    reclaimed = board.claim(
        resource,
        f"stress-{socket.gethostname().lower()}-session-reclaimer",
        ttl_s=30,
    )
    return {
        "ready": ready_count,
        "child_exit_code": process.exitcode,
        "child_result": child_results[0] if child_results else None,
        "reclaim_status": reclaimed.get("status"),
        "reclaim_code": reclaimed.get("code"),
        "new_holder": reclaimed.get("holder_agent"),
        "passed": (
            ready_count == 1
            and process.exitcode == 0
            and len(child_results) == 1
            and child_results[0].get("status") == "ok"
            and reclaimed.get("status") == "ok"
            and reclaimed.get("code") == "reclaimed"
        ),
    }


def defender_status() -> dict[str, Any]:
    if os.name != "nt":
        return {"verified": False, "active": False, "reason": "not-windows"}
    script = (
        "$s=Get-MpComputerStatus; "
        "[pscustomobject]@{AntivirusEnabled=$s.AntivirusEnabled;"
        "AMServiceEnabled=$s.AMServiceEnabled;"
        "RealTimeProtectionEnabled=$s.RealTimeProtectionEnabled} | ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"verified": False, "active": False, "reason": str(exc)}
    if completed.returncode != 0:
        return {"verified": False, "active": False, "reason": completed.stderr.strip()[-500:]}
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return {"verified": False, "active": False, "reason": str(exc)}
    active = all(
        bool(payload.get(field))
        for field in ("AntivirusEnabled", "AMServiceEnabled", "RealTimeProtectionEnabled")
    )
    payload.update({"verified": True, "active": active})
    return payload
