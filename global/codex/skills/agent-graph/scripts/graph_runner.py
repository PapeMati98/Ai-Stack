"""Execute a bound dispatch map without model-driven status polling.

The dispatch adapter owns provider-specific evidence collection. Exit code zero is
not acceptance. The graph's existing evidence and closeout validators remain in force.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import datetime as dt
import hashlib
import json
import re
import stat
import os
from pathlib import Path
import signal
import subprocess
import sys
sys.dont_write_bytecode = True
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import agent_graph as graph


TECHNICAL_PARALLELISM_CEILING = 16
CLI_MEMORY_RESERVATION_BYTES = 2 * 1024**3
SYSTEM_MEMORY_HEADROOM_BYTES = 16 * 1024**3
CPU_THREADS_PER_CLI = 2


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def available_memory_bytes():
    """Return current host-available memory without a third-party dependency."""
    if os.name == "nt":
        try:
            import ctypes

            class MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ("length", ctypes.c_ulong),
                    ("memory_load", ctypes.c_ulong),
                    ("total_physical", ctypes.c_ulonglong),
                    ("available_physical", ctypes.c_ulonglong),
                    ("total_page_file", ctypes.c_ulonglong),
                    ("available_page_file", ctypes.c_ulonglong),
                    ("total_virtual", ctypes.c_ulonglong),
                    ("available_virtual", ctypes.c_ulonglong),
                    ("available_extended_virtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatusEx()
            status.length = ctypes.sizeof(status)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return None
            return int(status.available_physical)
        except (AttributeError, OSError, ValueError):
            return None
    try:
        pages = int(os.sysconf("SC_AVPHYS_PAGES"))
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        return pages * page_size if pages > 0 and page_size > 0 else None
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def host_capacity_snapshot():
    logical_cpu = os.cpu_count()
    available = available_memory_bytes()
    missing = []
    if not isinstance(logical_cpu, int) or logical_cpu < 1:
        logical_cpu = None
        missing.append("logical-cpu-count")
    if not isinstance(available, int) or available < 1:
        available = None
        missing.append("available-memory")
    cpu_capacity = max(1, logical_cpu // CPU_THREADS_PER_CLI) if logical_cpu else 1
    memory_capacity = (
        max(1, (available - SYSTEM_MEMORY_HEADROOM_BYTES) // CLI_MEMORY_RESERVATION_BYTES)
        if available is not None
        else 1
    )
    capacity = max(
        1,
        min(TECHNICAL_PARALLELISM_CEILING, cpu_capacity, memory_capacity),
    )
    return {
        "observed_at": utc_now(),
        "logical_cpu_count": logical_cpu,
        "available_memory_bytes": available,
        "cpu_capacity": cpu_capacity,
        "memory_capacity": memory_capacity,
        "per_cli_memory_bytes": CLI_MEMORY_RESERVATION_BYTES,
        "reserved_system_headroom_bytes": SYSTEM_MEMORY_HEADROOM_BYTES,
        "technical_ceiling": TECHNICAL_PARALLELISM_CEILING,
        "capacity": capacity,
        "missing_evidence": missing,
    }


def resolve_parallelism(dispatch, host):
    requested = int(dispatch["max_parallel"])
    if dispatch.get("schema_version") == 1:
        policy = "legacy-hard"
        reason = "legacy-v1-max-parallel-preserved-as-hard-cap"
        evidence = {"source": "legacy-dispatch-map"}
    else:
        declaration = dispatch["parallelism"]
        policy = declaration["policy"]
        reason = declaration["reason"]
        evidence = declaration["evidence"]
    host_limit = max(1, min(int(host["capacity"]), TECHNICAL_PARALLELISM_CEILING))
    effective = (
        min(requested, host_limit)
        if policy in {"legacy-hard", "hard"}
        else host_limit
    )
    return {
        "policy": policy,
        "requested": requested,
        "effective": max(1, effective),
        "reason": reason,
        "evidence": evidence,
        "host": host,
    }


def office_event(run_id, node_id, status, office_home=None):
    try:
        from office_bridge import publish
        return publish(run_id, node_id, status, office_home=office_home)
    except Exception:
        return {"status": "unavailable"}


@contextlib.contextmanager
def exclusive(path):
    """Kernel locks survive stale files and release on process exit."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise graph.GraphError("RESOURCE_BUSY: " + str(path)) from None
    try:
        # Windows permits locking beyond EOF; initialize only while owning it.
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        yield
    finally:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _capacity_metadata(lease, **changes):
    lease.update(changes)
    stored = {
        key: value
        for key, value in lease.items()
        if not key.startswith("_")
    }
    write(lease["_metadata_path"], stored)


@contextlib.contextmanager
def shared_capacity_lease(
    root,
    capacity,
    *,
    attempt_id,
    node_id,
    timeout,
):
    """Acquire one cross-run host slot and hold its kernel lock until release."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    if type(capacity) is not int or not 1 <= capacity <= TECHNICAL_PARALLELISM_CEILING:
        raise graph.GraphError("Shared capacity must be an integer in 1..16")
    deadline = time.monotonic() + max(0.1, float(timeout))
    queued_at = time.monotonic_ns()
    observed_busy = False
    while True:
        allocator = contextlib.ExitStack()
        stack = contextlib.ExitStack()
        slot_index = None
        try:
            allocator.enter_context(exclusive(root / "allocation.lock"))
            busy = 0
            for candidate in range(TECHNICAL_PARALLELISM_CEILING):
                probe = contextlib.ExitStack()
                try:
                    probe.enter_context(
                        exclusive(root / f"slot-{candidate:02d}.lock")
                    )
                except graph.GraphError:
                    busy += 1
                finally:
                    probe.close()
            if busy < capacity:
                for candidate in range(TECHNICAL_PARALLELISM_CEILING):
                    try:
                        stack.enter_context(
                            exclusive(root / f"slot-{candidate:02d}.lock")
                        )
                    except graph.GraphError:
                        stack.close()
                        stack = contextlib.ExitStack()
                        continue
                    slot_index = candidate
                    break
        except graph.GraphError:
            observed_busy = True
        finally:
            allocator.close()
        if slot_index is not None:
            acquired_at = time.monotonic_ns()
            lease = {
                "attempt_id": str(attempt_id),
                "node_id": str(node_id),
                "slot_index": slot_index,
                "capacity": capacity,
                "supervisor_pid": os.getpid(),
                "child_pid": None,
                "descendant_peak": None,
                "descendant_visibility": "not-sampled",
                "queued_monotonic_ns": queued_at,
                "acquired_monotonic_ns": acquired_at,
                "queue_wait_seconds": round((acquired_at - queued_at) / 1_000_000_000, 6),
                "wait_reasons": ["capacity"] if observed_busy else [],
                "active": True,
                "_metadata_path": root / f"slot-{slot_index:02d}.json",
            }
            try:
                _capacity_metadata(lease)
                yield lease
            finally:
                try:
                    _capacity_metadata(
                        lease,
                        active=False,
                        released_monotonic_ns=time.monotonic_ns(),
                    )
                finally:
                    stack.close()
            return
        observed_busy = True
        stack.close()
        if time.monotonic() >= deadline:
            raise graph.GraphError("CAPACITY_QUEUE_TIMEOUT before any executor call")
        time.sleep(0.05)


def maximum_overlap(intervals):
    events = []
    for item in intervals:
        start = item.get("start", item.get("execution_started_monotonic_ns"))
        end = item.get("end", item.get("execution_completed_monotonic_ns"))
        if isinstance(start, int) and isinstance(end, int) and end >= start:
            events.extend(((start, 1), (end, -1)))
    active = peak = 0
    for _stamp, change in sorted(events, key=lambda row: (row[0], row[1])):
        active += change
        peak = max(peak, active)
    return peak


def read(path):
    return graph.load_json(Path(path), "runner record")


def write(path, value):
    graph.atomic_write_json(Path(path), value)


def validate_dispatch(value, plan, run_id):
    base_fields = {"schema_version", "run_id", "graph_sha256", "max_parallel", "nodes"}
    version = value.get("schema_version") if isinstance(value, dict) else None
    expected_fields = base_fields if version == 1 else base_fields | {"parallelism"}
    if version not in {1, 2} or set(value) != expected_fields:
        raise graph.GraphError("Dispatch map has missing or unknown fields")
    if value["run_id"] != run_id:
        raise graph.GraphError("Dispatch map run binding mismatch")
    if value["graph_sha256"] != graph.sha256_value(plan):
        raise graph.GraphError("Dispatch map is stale: graph hash mismatch")
    if (
        type(value["max_parallel"]) is not int
        or not 1 <= value["max_parallel"] <= TECHNICAL_PARALLELISM_CEILING
    ):
        raise graph.GraphError("max_parallel must be an integer in 1..16")
    if version == 2:
        parallelism = value["parallelism"]
        if not isinstance(parallelism, dict) or set(parallelism) != {
            "policy",
            "reason",
            "evidence",
        }:
            raise graph.GraphError("Invalid parallelism declaration")
        if parallelism["policy"] not in {"hard", "estimate"}:
            raise graph.GraphError("parallelism policy must be hard or estimate")
        reason = parallelism["reason"]
        if (
            not isinstance(reason, str)
            or not reason.strip()
            or len(reason) > 500
            or "\0" in reason
        ):
            raise graph.GraphError("parallelism reason must be bounded text")
        evidence = parallelism["evidence"]
        if not isinstance(evidence, dict) or not evidence:
            raise graph.GraphError("parallelism evidence must be a nonempty object")
        try:
            evidence_bytes = json.dumps(
                evidence, ensure_ascii=True, sort_keys=True, separators=(",", ":")
            ).encode("ascii")
        except (TypeError, ValueError):
            raise graph.GraphError("parallelism evidence must be JSON data") from None
        if len(evidence_bytes) > 4096:
            raise graph.GraphError("parallelism evidence exceeds 4096 bytes")
    nodes = {node["node_id"]: node for node in plan["nodes"] if node["active"]}
    if not isinstance(value["nodes"], dict) or set(value["nodes"]) - set(nodes):
        raise graph.GraphError("Unknown dispatch node")
    for key, item in value["nodes"].items():
        if nodes[key]["executor"] == "human" or nodes[key]["capability"] in {
            "production-approval", "deep-security-approval", "final-acceptance"
        }:
            raise graph.GraphError("Authority/closeout nodes cannot be auto-dispatched: " + key)
        if set(item) != {"argv", "workspace_root", "pinned_files"}:
            raise graph.GraphError("Invalid dispatch entry: " + key)
        argv = item["argv"]
        if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) and "\0" not in arg for arg in argv):
            raise graph.GraphError("argv must be a nonempty string array")
        executable = Path(argv[0])
        if not executable.is_absolute() or not executable.is_file():
            raise graph.GraphError("Dispatch executable must be an existing absolute file")
        if executable.suffix.lower() in {".cmd", ".bat", ".ps1"}:
            raise graph.GraphError("Use an explicit interpreter executable, never implicit shell=True")
        workspace = Path(item["workspace_root"])
        if not workspace.is_absolute() or not workspace.is_dir():
            raise graph.GraphError("Dispatch workspace must exist and be absolute")
        pins = item["pinned_files"]
        if not isinstance(pins, dict) or str(executable) not in pins:
            raise graph.GraphError("Pin the executable and adapter/input files")
        verify_pins(pins)
    return value


def verify_pins(pins):
    for name, digest in pins.items():
        path = Path(name)
        if not path.is_absolute() or not path.is_file() or graph.sha256_file(path) != digest:
            raise graph.GraphError("Dispatch input changed: " + name)


def terminate_owned(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW, check=False)
    else:
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


@contextlib.contextmanager
def resource_set(job):
    deadline = time.monotonic() + job.get("queue_timeout", job["timeout"])
    queued_at = time.monotonic_ns()
    observed_busy = False
    while True:
        stack = contextlib.ExitStack()
        try:
            for resource in sorted(job["resources"]):
                token = hashlib.sha256(resource.encode()).hexdigest()
                stack.enter_context(exclusive(Path(job["lock_root"]) / (token + ".lock")))
        except graph.GraphError:
            observed_busy = True
            stack.close()
            if time.monotonic() >= deadline:
                raise graph.GraphError("RESOURCE_QUEUE_TIMEOUT before any executor call") from None
            time.sleep(0.1)
            continue
        try:
            acquired_at = time.monotonic_ns()
            yield {
                "queue_wait_seconds": round(
                    (acquired_at - queued_at) / 1_000_000_000, 6
                ),
                "wait_reasons": ["resource-lock"] if observed_busy else [],
            }
        finally:
            stack.close()
        return


def descendant_process_count(parent_pid):
    """Count descendants without reading or persisting process command lines."""
    parents = {}
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class ProcessEntry(ctypes.Structure):
                _fields_ = [
                    ("dwSize", wintypes.DWORD),
                    ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            create_snapshot = kernel32.CreateToolhelp32Snapshot
            create_snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
            create_snapshot.restype = wintypes.HANDLE
            snapshot = create_snapshot(0x00000002, 0)
            invalid = ctypes.c_void_p(-1).value
            if snapshot == invalid:
                return None
            try:
                entry = ProcessEntry()
                entry.dwSize = ctypes.sizeof(entry)
                first = kernel32.Process32FirstW
                next_entry = kernel32.Process32NextW
                first.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
                next_entry.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
                first.restype = next_entry.restype = wintypes.BOOL
                if first(snapshot, ctypes.byref(entry)):
                    while True:
                        parents[int(entry.th32ProcessID)] = int(
                            entry.th32ParentProcessID
                        )
                        if not next_entry(snapshot, ctypes.byref(entry)):
                            break
            finally:
                close_handle = kernel32.CloseHandle
                close_handle.argtypes = [wintypes.HANDLE]
                close_handle.restype = wintypes.BOOL
                close_handle(snapshot)
        except (AttributeError, OSError, TypeError, ValueError):
            return None
    else:
        proc = Path("/proc")
        if not proc.is_dir():
            return None
        try:
            for path in proc.iterdir():
                if not path.name.isdigit():
                    continue
                text = (path / "stat").read_text(encoding="ascii", errors="replace")
                tail = text[text.rfind(")") + 2 :].split()
                if len(tail) >= 2:
                    parents[int(path.name)] = int(tail[1])
        except (OSError, ValueError):
            return None
    descendants = set()
    frontier = {int(parent_pid)}
    while frontier:
        children = {
            pid for pid, parent in parents.items() if parent in frontier and pid not in descendants
        }
        if not children:
            break
        descendants.update(children)
        frontier = children
    return len(descendants)


def worker(job_path):
    """Separate supervisor owns resource locks even if the scheduler is interrupted."""
    job = read(job_path)
    folder = Path(job_path).parent
    queue_started = time.monotonic_ns()
    receipt = {
        "attempt_id": job["attempt_id"],
        "status": "INFRA_ERROR",
        "queue_started_monotonic_ns": queue_started,
        "queue_wait_seconds": 0.0,
        "execution_seconds": 0.0,
        "wait_reasons": [],
    }
    try:
        with resource_set(job) as resource_queue:
            with shared_capacity_lease(
                job["capacity_root"],
                job["capacity"],
                attempt_id=job["attempt_id"],
                node_id=job["node_id"],
                timeout=job.get("queue_timeout", job["timeout"]),
            ) as capacity_lease:
                receipt["wait_reasons"] = sorted(
                    set(resource_queue["wait_reasons"] + capacity_lease["wait_reasons"])
                )
                verify_pins(job["dispatch"]["pinned_files"])
                env = os.environ.copy()
                env.update({"AGENT_GRAPH_ATTEMPT_ID": job["attempt_id"],
                            "AGENT_GRAPH_EVIDENCE_PATH": str(folder / "adapter-result.json"),
                            "AGENT_GRAPH_NODE_ID": job["node_id"],
                            "AGENT_GRAPH_ATTEMPT": str(job["attempt"]),
                            "AGENT_GRAPH_CONTEXT_PATH": str(folder / "context.json")})
                with (folder / "stdout.log").open("wb") as out, (folder / "stderr.log").open("wb") as err:
                    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
                    process = subprocess.Popen(job["dispatch"]["argv"], cwd=job["dispatch"]["workspace_root"],
                                               env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err, **options)
                    execution_started = time.monotonic_ns()
                    receipt.update(
                        queue_wait_seconds=round(
                            (execution_started - queue_started) / 1_000_000_000, 6
                        ),
                        execution_started_monotonic_ns=execution_started,
                        capacity_slot_index=capacity_lease["slot_index"],
                        capacity=capacity_lease["capacity"],
                    )
                    write(folder / "process.json", {"pid": process.pid, "supervisor_pid": os.getpid(),
                                                   "attempt_id": job["attempt_id"]})
                    descendant_peak = 0
                    descendant_visible = False
                    _capacity_metadata(capacity_lease, child_pid=process.pid)
                    deadline = time.monotonic() + job["timeout"]
                    while process.poll() is None and time.monotonic() < deadline:
                        descendants = descendant_process_count(process.pid)
                        if descendants is not None:
                            descendant_visible = True
                            new_peak = max(descendant_peak, descendants)
                            if new_peak != descendant_peak:
                                descendant_peak = new_peak
                                _capacity_metadata(
                                    capacity_lease,
                                    descendant_peak=descendant_peak,
                                    descendant_visibility="observed",
                                )
                        time.sleep(0.05)
                    _capacity_metadata(
                        capacity_lease,
                        child_pid=process.pid,
                        descendant_peak=descendant_peak if descendant_visible else None,
                        descendant_visibility="observed" if descendant_visible else "unavailable",
                    )
                    if process.poll() is None:
                        terminate_owned(process)
                        receipt["status"] = "TIMEOUT"
                    else:
                        code = process.wait()
                        receipt.update(status="EXITED", returncode=code)
                        result = folder / "adapter-result.json"
                        if result.is_file():
                            receipt["evidence_sha256"] = graph.sha256_file(result)
                    execution_completed = time.monotonic_ns()
                    receipt.update(
                        execution_completed_monotonic_ns=execution_completed,
                        execution_seconds=round(
                            (execution_completed - execution_started) / 1_000_000_000,
                            6,
                        ),
                        descendant_peak=(descendant_peak if descendant_visible else None),
                        descendant_visibility=(
                            "observed" if descendant_visible else "unavailable"
                        ),
                    )
    except (OSError, graph.GraphError) as exc:
        receipt["error"] = str(exc)
    finally:
        write(folder / "finished.json", receipt)
    return 0


def conflict_reasons(left, right):
    reasons = []
    if set(left["node"]["resource_locks"]) & set(right["node"]["resource_locks"]):
        reasons.append("resource-lock")
    if os.path.normcase(left["dispatch"]["workspace_root"]) != os.path.normcase(right["dispatch"]["workspace_root"]):
        return reasons
    # A writer cannot overlap another reader/writer in the same live checkout.
    for a, b in ((left, right), (right, left)):
        for write_scope in a["node"]["write_scope"]:
            for scope in b["node"]["read_scope"] + b["node"]["write_scope"]:
                x, y = write_scope.rstrip("/\\").casefold(), scope.rstrip("/\\").casefold()
                if x == y or x.startswith(y + "/") or y.startswith(x + "/") or x in {".", "*"} or y in {".", "*"}:
                    reasons.append("workspace-lock")
                    return sorted(set(reasons))
    return sorted(set(reasons))


def conflict(left, right):
    return bool(conflict_reasons(left, right))


def record_wait(journal, node_id, code, details):
    waits = journal.setdefault("waits", {})
    key = node_id + ":" + code
    now = utc_now()
    row = waits.get(key)
    if row is None:
        waits[key] = {
            "node_id": node_id,
            "code": code,
            "details": details,
            "first_observed_at": now,
            "last_observed_at": now,
            "observations": 1,
        }
        return True
    if row.get("details") != details:
        row["details"] = details
        row["last_observed_at"] = now
        row["observations"] = int(row.get("observations", 0)) + 1
        return True
    return False


def scheduler_diagnostics(run_dir, journal, parallelism):
    attempts = []
    intervals = []
    reported_waits = {
        (row["node_id"], row["code"]): dict(row)
        for row in journal.get("waits", {}).values()
    }
    for key, row in sorted(journal.get("attempts", {}).items()):
        receipt_path = run_dir / "runner" / key / "finished.json"
        if not receipt_path.is_file():
            continue
        receipt = read(receipt_path)
        attempt = {
            "attempt_id": key,
            "node_id": row["node_id"],
            "status": receipt.get("status"),
            "queue_wait_seconds": float(receipt.get("queue_wait_seconds", 0.0)),
            "execution_seconds": float(receipt.get("execution_seconds", 0.0)),
            "wait_reasons": list(receipt.get("wait_reasons", [])),
            "capacity_slot_index": receipt.get("capacity_slot_index"),
            "descendant_peak": receipt.get("descendant_peak"),
            "descendant_visibility": receipt.get("descendant_visibility", "unavailable"),
        }
        started = receipt.get("execution_started_monotonic_ns")
        completed = receipt.get("execution_completed_monotonic_ns")
        if isinstance(started, int) and isinstance(completed, int):
            intervals.append({"start": started, "end": completed})
        for code in attempt["wait_reasons"]:
            reported_waits.setdefault(
                (row["node_id"], code),
                {
                    "node_id": row["node_id"],
                    "code": code,
                    "details": {
                        "queue_wait_seconds": attempt["queue_wait_seconds"]
                    },
                    "first_observed_at": None,
                    "last_observed_at": None,
                    "observations": 1,
                },
            )
        attempts.append(attempt)
    return {
        "parallelism": parallelism,
        "observed_execution_overlap": maximum_overlap(intervals),
        "attempts": attempts,
        "wait_reasons": sorted(
            reported_waits.values(),
            key=lambda row: (row["node_id"], row["code"]),
        ),
        "visibility_gaps": [
            "quota",
            "external-process-occupancy",
            "nested-agent-lineage",
        ],
        "capacity_authority": "kernel-leases",
        "stored_slot_metadata_authority": "observation-only",
    }


SETTLED_PHASES = {"RECORDED", "FAILURE_RECONCILED"}
ADOPTION_INTENT_NAME = "dispatch-adoption-intent.json"
SNAPSHOT_DIRNAME = "dispatch-snapshots"
CORRECTION_INTENT_NAME = "negative-correction-intent.json"
CORRECTION_RECEIPT_NAME = "negative-correction-receipt.json"
CORRECTED_EVIDENCE_NAME = "corrected-evidence.json"
NEGATIVE_CORRECTION_STATUS = "NEGATIVE_EVIDENCE_CORRECTED"


def _sha256_hex(value):
    if not isinstance(value, str) or not graph.SHA256_RE.fullmatch(value):
        raise graph.GraphError("Dispatch hash must be a 64-character lowercase SHA-256")
    return value


def _plan_revisions(run_dir):
    plans = {}
    folder = run_dir / "plans"
    if not folder.is_dir():
        return plans
    for path in folder.glob("revision-*.json"):
        plan = read(path)
        plans[graph.sha256_value(plan)] = {
            "revision": plan.get("revision"),
            "path": path,
            "plan": plan,
        }
    return plans


def _unresolved_attempts(journal, run_dir, state):
    running_nodes = [
        node_id
        for node_id, row in state["nodes"].items()
        if row["status"] in {"running", "recovery-required"}
    ]
    unsettled = [
        row
        for row in journal.get("attempts", {}).values()
        if row.get("phase") not in SETTLED_PHASES
    ]
    return running_nodes, unsettled


def _adoption_request(old_hash, new_dispatch, state):
    return {
        "schema_version": 1,
        "expected_old_dispatch_hash": old_hash,
        "new_dispatch_sha256": graph.sha256_value(new_dispatch),
        "new_graph_sha256": new_dispatch["graph_sha256"],
        "old_graph_sha256": None,
        "revision": state["revision"],
        "plan_hash": state["plan_hash"],
    }


def _write_immutable_snapshot(path, value, expected_hash):
    if path.exists():
        existing = read(path)
        if graph.sha256_value(existing) != expected_hash:
            raise graph.GraphError("Stored dispatch snapshot bytes were changed")
        return
    write(path, value)
    stored = read(path)
    if graph.sha256_value(stored) != expected_hash:
        raise graph.GraphError("Dispatch snapshot hash mismatch after write")


def _complete_adoption(run_dir, journal, old_dispatch, new_dispatch, request, old_hash):
    folder = run_dir / "runner"
    snapshots = folder / SNAPSHOT_DIRNAME
    snapshots.mkdir(parents=True, exist_ok=True)
    snapshot_path = snapshots / f"{old_hash}.json"
    _write_immutable_snapshot(snapshot_path, old_dispatch, old_hash)
    write(folder / "dispatch.json", new_dispatch)
    adoptions = journal.setdefault("adoptions", [])
    record = {
        "old_dispatch_sha256": old_hash,
        "new_dispatch_sha256": request["new_dispatch_sha256"],
        "old_graph_sha256": request["old_graph_sha256"],
        "new_graph_sha256": request["new_graph_sha256"],
        "revision": request["revision"],
        "snapshot": str(Path(SNAPSHOT_DIRNAME) / f"{old_hash}.json").replace("\\", "/"),
        "recorded_at": utc_now(),
    }
    if not any(
        row.get("old_dispatch_sha256") == record["old_dispatch_sha256"]
        and row.get("new_dispatch_sha256") == record["new_dispatch_sha256"]
        and row.get("revision") == record["revision"]
        for row in adoptions
    ):
        adoptions.append(record)
    journal["dispatch_sha256"] = request["new_dispatch_sha256"]
    write(folder / "journal.json", journal)
    intent = folder / ADOPTION_INTENT_NAME
    if intent.exists():
        intent.unlink()
    return {
        "status": "DISPATCH_ADOPTED",
        "run_id": journal.get("run_id"),
        "old_dispatch_sha256": old_hash,
        "new_dispatch_sha256": request["new_dispatch_sha256"],
        "revision": request["revision"],
        "adoption": record,
        "note": "Old dispatch snapshots remain immutable; attempt files were not rewritten.",
    }


def adopt_dispatch(args):
    """Replace the runner dispatch only after an accepted planning amendment."""
    run_dir = graph.resolve_run_dir(graph.resolve_state_root(args.state_root), args.run_id)
    old_hash = _sha256_hex(args.expected_old_dispatch_hash)
    new_dispatch_path = Path(args.dispatch)
    with exclusive(run_dir / "runner/runner.lock"):
        with graph.run_lock(run_dir):
            state = graph.load_state(run_dir)
            plan = graph.load_current_plan(run_dir, state)
            new_dispatch = validate_dispatch(read(new_dispatch_path), plan, args.run_id)
            validate_planning_dispatch(run_dir, state, plan, new_dispatch)
            folder = run_dir / "runner"
            journal_path = folder / "journal.json"
            dispatch_path = folder / "dispatch.json"
            if not journal_path.is_file() or not dispatch_path.is_file():
                raise graph.GraphError("Runner has no stored dispatch to adopt from")
            journal = read(journal_path)
            current_dispatch = read(dispatch_path)
            stored_hash = journal.get("dispatch_sha256")
            snapshot_path = folder / SNAPSHOT_DIRNAME / f"{old_hash}.json"
            intent_path = folder / ADOPTION_INTENT_NAME
            request = _adoption_request(old_hash, new_dispatch, state)
            if snapshot_path.exists():
                old_dispatch = read(snapshot_path)
                if graph.sha256_value(old_dispatch) != old_hash:
                    raise graph.GraphError("Stored dispatch snapshot bytes were changed")
            else:
                old_dispatch = current_dispatch
            request["old_graph_sha256"] = old_dispatch.get("graph_sha256")
            if intent_path.exists():
                intent = read(intent_path)
                comparable = {key: intent.get(key) for key in request}
                if comparable != request:
                    raise graph.GraphError(
                        "Conflicting dispatch adoption request; journal was not reset"
                    )
            already = [
                row
                for row in journal.get("adoptions", [])
                if row.get("old_dispatch_sha256") == old_hash
                and row.get("new_dispatch_sha256") == request["new_dispatch_sha256"]
                and row.get("revision") == request["revision"]
            ]
            if stored_hash == request["new_dispatch_sha256"] and already:
                if graph.sha256_value(current_dispatch) != request["new_dispatch_sha256"]:
                    raise graph.GraphError("Adopted dispatch file does not match the journal")
                if intent_path.exists():
                    intent_path.unlink()
                return {
                    "status": "DISPATCH_ADOPTED",
                    "run_id": args.run_id,
                    "old_dispatch_sha256": old_hash,
                    "new_dispatch_sha256": request["new_dispatch_sha256"],
                    "revision": request["revision"],
                    "adoption": already[-1],
                    "idempotent": True,
                    "note": "Old dispatch snapshots remain immutable; attempt files were not rewritten.",
                }
            recovering = intent_path.exists()
            actual_current = graph.sha256_value(current_dispatch)
            if recovering:
                if actual_current not in {old_hash, request["new_dispatch_sha256"]}:
                    raise graph.GraphError(
                        "Stored dispatch does not match the expected old dispatch hash"
                    )
                if graph.sha256_value(old_dispatch) != old_hash:
                    raise graph.GraphError(
                        "Stored dispatch does not match the expected old dispatch hash"
                    )
            elif stored_hash != old_hash or actual_current != old_hash:
                raise graph.GraphError(
                    "Stored dispatch does not match the expected old dispatch hash"
                )
            if old_dispatch.get("graph_sha256") == new_dispatch["graph_sha256"]:
                raise graph.GraphError("Same-revision dispatch replacement is refused")
            plans = _plan_revisions(run_dir)
            prior = plans.get(old_dispatch["graph_sha256"])
            current = plans.get(state["plan_hash"])
            if prior is None or current is None:
                raise graph.GraphError(
                    "Stored dispatch/plan snapshots are missing from the amendment chain"
                )
            if current["revision"] != state["revision"]:
                raise graph.GraphError("Current plan revision does not match run state")
            if prior["revision"] >= current["revision"]:
                raise graph.GraphError("Same-revision dispatch replacement is refused")
            amendments = state.get("amendments") or []
            if not amendments:
                raise graph.GraphError(
                    "Foreign or missing planning amendment cannot adopt dispatch"
                )
            linked = any(
                row.get("base_revision") == prior["revision"]
                and row.get("new_revision") == current["revision"]
                for row in amendments
            ) or (
                amendments[-1].get("new_revision") == current["revision"]
                and prior["revision"] < current["revision"]
            )
            if not linked:
                raise graph.GraphError(
                    "No accepted planning amendment between the stored dispatch and the current plan"
                )
            if new_dispatch["graph_sha256"] != state["plan_hash"]:
                raise graph.GraphError("Dispatch map is stale: graph hash mismatch")
            running_nodes, unsettled = _unresolved_attempts(journal, run_dir, state)
            if running_nodes or unsettled:
                raise graph.GraphError(
                    "Refuse dispatch adoption while attempts are running or unresolved"
                )
            retained = set(old_dispatch.get("nodes", {})) & set(new_dispatch["nodes"])
            for node_id in sorted(retained):
                if old_dispatch["nodes"][node_id] != new_dispatch["nodes"][node_id]:
                    raise graph.GraphError(
                        "Retained node adapter cannot change silently: " + node_id
                    )
            attempt_root = folder
            before_attempts = {}
            if attempt_root.is_dir():
                for attempt_dir in attempt_root.iterdir():
                    if attempt_dir.is_dir() and attempt_dir.name not in {
                        SNAPSHOT_DIRNAME
                    }:
                        for name in ("job.json", "context.json"):
                            path = attempt_dir / name
                            if path.is_file():
                                before_attempts[str(path)] = path.read_bytes()
            if not intent_path.exists():
                write(intent_path, request)
            result = _complete_adoption(
                run_dir, journal, old_dispatch, new_dispatch, request, old_hash
            )
            result["run_id"] = args.run_id
            for path_text, payload in before_attempts.items():
                if Path(path_text).read_bytes() != payload:
                    raise graph.GraphError("Adoption mutated historical attempt files")
            return result


def _load_json_if_file(path):
    if not path.is_file():
        return None
    return read(path)


def _start_binding(state, row, job):
    pinned_op = row.get("start_operation_id")
    attempt_number = row.get("attempt_number")
    if attempt_number is None and isinstance(job, dict):
        attempt_number = job.get("attempt")
    operations = state.get("operations") or {}
    if pinned_op:
        operation = operations.get(pinned_op)
        if not isinstance(operation, dict) or operation.get("action") != "start":
            raise graph.GraphError("Original start operation is missing or changed")
        result = operation.get("result") or {}
        if result.get("node_id") != row["node_id"] or result.get("attempt") != attempt_number:
            raise graph.GraphError("Start operation does not bind this node and attempt")
        return pinned_op, attempt_number, result
    derived_id = "op-runner-start-" + row["attempt_id"]
    operation = operations.get(derived_id)
    if isinstance(operation, dict) and operation.get("action") == "start":
        result = operation.get("result") or {}
        if result.get("node_id") == row["node_id"] and result.get("attempt") == attempt_number:
            return derived_id, attempt_number, result
    matches = []
    for operation_id, operation in operations.items():
        if operation.get("action") != "start":
            continue
        result = operation.get("result") or {}
        if result.get("node_id") == row["node_id"] and result.get("attempt") == attempt_number:
            matches.append((operation_id, result))
    if len(matches) != 1:
        raise graph.GraphError(
            "Canonical terminal result does not bind this node and attempt"
        )
    return matches[0][0], attempt_number, matches[0][1]


def _adapter_result_present(folder):
    adapter_path = folder / "adapter-result.json"
    return adapter_path.exists() or adapter_path.is_symlink()


def _stored_evidence_rows(node_state, evidence_hash, verdict):
    return [
        item
        for item in node_state.get("evidence") or []
        if item.get("evidence_hash") == evidence_hash and item.get("verdict") == verdict
    ]


def _unique_result_ops(state, node_id, evidence_hash, verdict):
    run_id = state.get("run_id")
    return [
        (operation_id, operation)
        for operation_id, operation in (state.get("operations") or {}).items()
        if operation.get("action") == "result"
        and (operation.get("result") or {}).get("node_id") == node_id
        and (operation.get("result") or {}).get("evidence_hash") == evidence_hash
        and (operation.get("result") or {}).get("verdict") == verdict
        and (operation.get("result") or {}).get("run_id") in {None, run_id}
    ]


def _finish_canonical_failure(
    state, row, job, node_id, verdict, evidence, evidence_hash, stored_rows, result_ops
):
    start_id, attempt_number, start_result = _start_binding(state, row, job)
    if attempt_number is None:
        return None
    if start_result.get("attempt") != attempt_number:
        return None
    if start_result.get("run_id") not in {None, state.get("run_id")}:
        return None
    if evidence.get("attempt") not in {None, attempt_number} and evidence.get("attempt") != attempt_number:
        raise graph.GraphError(
            "Later unrelated terminal attempt cannot reconcile this attempt"
        )
    later_starts = [
        operation
        for operation in (state.get("operations") or {}).values()
        if operation.get("action") == "start"
        and (operation.get("result") or {}).get("node_id") == node_id
        and isinstance((operation.get("result") or {}).get("attempt"), int)
        and (operation.get("result") or {}).get("attempt") > attempt_number
    ]
    matched_result = result_ops[0][1]
    if later_starts:
        later_at = min(item.get("recorded_at") or "" for item in later_starts)
        if (matched_result.get("recorded_at") or "") >= later_at:
            raise graph.GraphError(
                "Later unrelated terminal attempt cannot reconcile this attempt"
            )
    if isinstance(job, dict):
        if job.get("attempt_id") != row["attempt_id"] or job.get("node_id") != node_id:
            return None
        if job.get("attempt") not in {None, attempt_number} and job.get("attempt") != attempt_number:
            return None
    return {
        "verdict": verdict,
        "evidence_hash": evidence_hash,
        "start_operation_id": start_id,
        "attempt_number": attempt_number,
        "result_operation_id": result_ops[0][0],
        "evidence_id": stored_rows[0].get("evidence_id"),
    }


def _canonical_failure_from_stored_evidence(run_dir, state, row, job, node_id, node_state):
    del run_dir
    verdict = node_state["status"]
    result_ops = [
        (operation_id, operation)
        for operation_id, operation in (state.get("operations") or {}).items()
        if operation.get("action") == "result"
        and (operation.get("result") or {}).get("node_id") == node_id
        and (operation.get("result") or {}).get("verdict") == verdict
        and (operation.get("result") or {}).get("run_id") in {None, state.get("run_id")}
    ]
    if len(result_ops) != 1:
        return None
    evidence_hash = (result_ops[0][1].get("result") or {}).get("evidence_hash")
    if not isinstance(evidence_hash, str) or len(evidence_hash) != 64:
        return None
    stored_rows = _stored_evidence_rows(node_state, evidence_hash, verdict)
    if len(stored_rows) != 1:
        return None
    stored_path = Path(stored_rows[0]["path"])
    if not stored_path.is_file() or stored_path.is_symlink():
        return None
    stored_evidence = read(stored_path)
    if graph.sha256_value(stored_evidence) != evidence_hash:
        return None
    if stored_evidence.get("node_id") != node_id or stored_evidence.get("verdict") != verdict:
        return None
    hashed_ops = _unique_result_ops(state, node_id, evidence_hash, verdict)
    if hashed_ops != result_ops:
        return None
    return _finish_canonical_failure(
        state,
        row,
        job,
        node_id,
        verdict,
        stored_evidence,
        evidence_hash,
        stored_rows,
        result_ops,
    )


def _canonical_failure(run_dir, state, row, job, receipt):
    if receipt.get("status") != "EXITED" or receipt.get("returncode") in {None, 0}:
        return None
    if receipt.get("attempt_id") not in {None, row["attempt_id"]}:
        return None
    node_id = row["node_id"]
    node_state = state["nodes"].get(node_id)
    if not isinstance(node_state, dict):
        return None
    if node_state["status"] in {"running", "recovery-required", "pending"}:
        return None
    if node_state["status"] not in {"rejected", "blocked"}:
        return None
    folder = run_dir / "runner" / row["attempt_id"]
    if _adapter_result_present(folder):
        envelope = _load_json_if_file(folder / "adapter-result.json")
        if not isinstance(envelope, dict):
            return None
        if set(envelope) != {"attempt_id", "evidence"} or envelope["attempt_id"] != row["attempt_id"]:
            return None
        evidence = envelope["evidence"]
        if not isinstance(evidence, dict) or evidence.get("node_id") != node_id:
            return None
        verdict = evidence.get("verdict")
        if verdict not in {"rejected", "blocked"} or verdict != node_state["status"]:
            return None
        evidence_hash = graph.sha256_value(evidence)
        stored_rows = _stored_evidence_rows(node_state, evidence_hash, verdict)
        if len(stored_rows) != 1:
            return None
        stored_path = Path(stored_rows[0]["path"])
        if not stored_path.is_file():
            return None
        stored_evidence = read(stored_path)
        if graph.sha256_value(stored_evidence) != evidence_hash:
            return None
        result_ops = _unique_result_ops(state, node_id, evidence_hash, verdict)
        if len(result_ops) != 1:
            return None
        return _finish_canonical_failure(
            state,
            row,
            job,
            node_id,
            verdict,
            evidence,
            evidence_hash,
            stored_rows,
            result_ops,
        )
    return _canonical_failure_from_stored_evidence(
        run_dir, state, row, job, node_id, node_state
    )


def apply_failure_reconciliation(run_dir, journal, row, receipt, canonical):
    row["phase"] = "FAILURE_RECONCILED"
    row["disposition"] = "failure-reconciled"
    row["canonical_verdict"] = canonical["verdict"]
    row["canonical_evidence_hash"] = canonical["evidence_hash"]
    row["start_operation_id"] = canonical["start_operation_id"]
    row["attempt_number"] = canonical["attempt_number"]
    row["result_operation_id"] = canonical["result_operation_id"]
    write(run_dir / "runner/journal.json", journal)
    return {
        "status": "FAILURE_RECONCILED",
        "attempt_id": row["attempt_id"],
        "node_id": row["node_id"],
        "disposition": "failure-reconciled",
        "verdict": canonical["verdict"],
        "evidence_hash": canonical["evidence_hash"],
        "receipt_status": receipt.get("status"),
        "returncode": receipt.get("returncode"),
        "note": "Nonzero receipt and failed/blocked verdict are preserved; this is not acceptance.",
    }


def inspect_settled_failure(run_dir, journal, row, receipt):
    del journal
    folder = run_dir / "runner" / row["attempt_id"]
    job = _load_json_if_file(folder / "job.json")
    context = _load_json_if_file(folder / "context.json")
    if not isinstance(job, dict) or not isinstance(context, dict):
        return None
    if job.get("attempt_id") != row["attempt_id"] or job.get("node_id") != row["node_id"]:
        return None
    state = graph.load_state(run_dir)
    if context.get("run_id") != state.get("run_id"):
        return None
    node = context.get("node") if isinstance(context.get("node"), dict) else {}
    if node.get("node_id") != row["node_id"]:
        return None
    return _canonical_failure(run_dir, state, row, job, receipt)


def reconcile_failure(args):
    run_dir = graph.resolve_run_dir(graph.resolve_state_root(args.state_root), args.run_id)
    node_id = args.node_id
    attempt_id = args.attempt_id
    with exclusive(run_dir / "runner/runner.lock"):
        with graph.run_lock(run_dir):
            journal_path = run_dir / "runner/journal.json"
            if not journal_path.is_file():
                raise graph.GraphError("Runner journal is missing")
            journal = read(journal_path)
            row = journal.get("attempts", {}).get(attempt_id)
            if not isinstance(row, dict) or row.get("node_id") != node_id:
                raise graph.GraphError(
                    "Settled failure reconciliation requires the exact node and attempt"
                )
            folder = run_dir / "runner" / attempt_id
            finished = folder / "finished.json"
            if not finished.is_file():
                raise graph.GraphError("Missing or changed canonical failure evidence")
            receipt = read(finished)
            if row.get("phase") == "FAILURE_RECONCILED":
                canonical = inspect_settled_failure(run_dir, journal, row, receipt)
                if canonical is None or canonical["evidence_hash"] != row.get(
                    "canonical_evidence_hash"
                ):
                    raise graph.GraphError("Missing or changed canonical failure evidence")
                return {
                    "status": "FAILURE_RECONCILED",
                    "attempt_id": attempt_id,
                    "node_id": node_id,
                    "disposition": "failure-reconciled",
                    "verdict": row.get("canonical_verdict"),
                    "evidence_hash": row.get("canonical_evidence_hash"),
                    "receipt_status": receipt.get("status"),
                    "returncode": receipt.get("returncode"),
                    "idempotent": True,
                    "note": "Nonzero receipt and failed/blocked verdict are preserved; this is not acceptance.",
                }
            if row.get("phase") == "RECORDED":
                raise graph.GraphError(
                    "Canonical terminal result does not bind this node and attempt"
                )
            state = graph.load_state(run_dir)
            node_state = state["nodes"].get(node_id) or {}
            if node_state.get("status") in {"running", "recovery-required"}:
                raise graph.GraphError("Running node cannot be failure-reconciled")
            try:
                canonical = inspect_settled_failure(run_dir, journal, row, receipt)
            except graph.GraphError:
                raise
            if canonical is None:
                raise graph.GraphError(
                    "Canonical terminal result does not bind this node and attempt"
                )
            result = apply_failure_reconciliation(run_dir, journal, row, receipt, canonical)
            result["run_id"] = args.run_id
            return result


def _plain_path(path, *, missing=False):
    path = Path(os.path.abspath(path))
    for part in [*reversed(path.parents), path]:
        try:
            info = part.lstat()
        except FileNotFoundError:
            if missing and part == path:
                continue
            raise graph.GraphError("Correction path is missing")
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise graph.GraphError("Linked/reparse correction path is refused")
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise graph.GraphError("Hardlinked correction file is refused")
    return path


def _strict_json(path):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value
    def nonfinite(value):
        raise ValueError("nonfinite JSON")
    try:
        payload = _plain_path(path).read_bytes()
        value = json.loads(payload.decode("utf-8-sig"), object_pairs_hook=pairs,
                           parse_constant=nonfinite)
        _json_value(value)
        return value, hashlib.sha256(payload).hexdigest()
    except (ValueError, UnicodeError, OSError) as exc:
        raise graph.GraphError("Invalid correction JSON: " + type(exc).__name__) from exc


def _json_value(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True,
                      separators=(",", ":"), allow_nan=False)


def _immutable_correction(path, value):
    _plain_path(path, missing=True)
    if path.exists():
        existing, _ = _strict_json(path)
        if _json_value(existing) != _json_value(value):
            raise graph.GraphError("Conflicting correction replay")
        return
    # Atomic no-replace publication. The temporary hardlink disappears before
    # subsequent validation; the published file is flushed and never rewritten.
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("xb") as handle:
            handle.write((_json_value(value) + "\n").encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temp, path)
    except FileExistsError as exc:
        raise graph.GraphError("Conflicting correction publication") from exc
    finally:
        if temp.exists():
            temp.unlink()


def _negative_correction_present(folder):
    return any(os.path.lexists(folder / name) for name in
               (CORRECTION_INTENT_NAME, CORRECTION_RECEIPT_NAME, CORRECTED_EVIDENCE_NAME))


def _correction_subject(run_dir, args):
    if not re.fullmatch(r"[0-9a-f]{32}", args.attempt_id):
        raise graph.GraphError("Correction requires an exact attempt ID")
    _plain_path(run_dir)
    journal, _ = _strict_json(run_dir / "runner/journal.json")
    row = journal.get("attempts", {}).get(args.attempt_id)
    if not isinstance(row, dict) or row.get("node_id") != args.node_id or row.get("attempt_id") != args.attempt_id:
        raise graph.GraphError("Correction node/attempt does not match journal")
    if row.get("phase") not in {"STARTED", "RECORDED"}:
        raise graph.GraphError("Correction requires a started or corrected attempt")
    folder = _plain_path(run_dir / "runner" / args.attempt_id)
    files, pins = {}, {}
    for name in ("adapter-result.json", "finished.json", "job.json", "context.json"):
        files[name], pins[name] = _strict_json(folder / name)
        if not isinstance(files[name], dict):
            raise graph.GraphError("Correction subject must be an object")
    original, receipt, job, context = (files[k] for k in
        ("adapter-result.json", "finished.json", "job.json", "context.json"))
    if (receipt.get("status") != "EXITED" or type(receipt.get("returncode")) is not int
            or receipt["returncode"] != 0 or receipt.get("attempt_id") != args.attempt_id
            or receipt.get("evidence_sha256") != pins["adapter-result.json"]):
        raise graph.GraphError("Correction requires exact finished EXITED rc0 evidence")
    if set(original) != {"attempt_id", "evidence"} or original["attempt_id"] != args.attempt_id:
        raise graph.GraphError("Wrong original adapter envelope")
    state, _ = _strict_json(run_dir / "state.json")
    if state.get("run_id") != args.run_id:
        raise graph.GraphError("Foreign correction run")
    # Native runner journal shape carries only attempts, dispatch_sha256, waits.
    # The run_id is bound strictly through state.run_id, context.run_id, the
    # exact lexical run/attempt path, the start result run_id, the job, and the
    # original file/object hashes. A foreign journal.run_id, if anyone ever
    # introduced one, must still be rejected.
    journal_run_id = journal.get("run_id")
    if journal_run_id is not None and journal_run_id != args.run_id:
        raise graph.GraphError("Foreign correction run")
    node_state = state["nodes"].get(args.node_id, {})
    number = job.get("attempt")
    if (type(number) is not int or number < 1 or type(node_state.get("attempts")) is not int
            or node_state.get("attempts") != number):
        raise graph.GraphError("Later or unrelated attempt cannot be corrected")
    if job.get("attempt_id") != args.attempt_id or job.get("node_id") != args.node_id:
        raise graph.GraphError("Job does not bind correction attempt")
    starts = [(key, op) for key, op in state.get("operations", {}).items()
              if op.get("action") == "start" and op.get("result", {}).get("node_id") == args.node_id
              and op.get("result", {}).get("attempt") == number]
    if len(starts) != 1:
        raise graph.GraphError("Ambiguous or missing original start")
    start_id, start = starts[0]
    if type(start["result"].get("attempt")) is not int:
        raise graph.GraphError("Invalid original start attempt type")
    for item in (row, job):
        if item.get("start_operation_id", start_id) != start_id:
            raise graph.GraphError("Changed original start binding")
    if row.get("attempt_number", number) != number or start["result"].get("run_id") != args.run_id:
        raise graph.GraphError("Wrong original start run/number")
    if (start["result"].get("status") != "STARTED"
            or job.get("timeout") != start["result"].get("dispatch_bounds", {}).get("timeout")
            or _json_value(context.get("dispatch_bounds")) != _json_value(start["result"].get("dispatch_bounds"))):
        raise graph.GraphError("Wrong original start bounds")
    if any(op.get("action") == "start" and op.get("result", {}).get("node_id") == args.node_id
           and op.get("result", {}).get("attempt", 0) > number for op in state["operations"].values()):
        raise graph.GraphError("Later attempt exists")
    workspace = node_state.get("workspace_root")
    if job.get("dispatch", {}).get("workspace_root") != workspace:
        raise graph.GraphError("Wrong original workspace binding")
    plan = graph.load_current_plan(run_dir, state)
    node = next((n for n in plan["nodes"] if n["node_id"] == args.node_id), None)
    if (context.get("run_id") != args.run_id or _json_value(context.get("node")) != _json_value(node)
            or graph.sha256_value(context.get("graph")) != state["plan_hash"]):
        raise graph.GraphError("Wrong context/plan binding")
    evidence = original.get("evidence")
    if not isinstance(evidence, dict) or evidence.get("verdict") not in {"rejected", "blocked"}:
        raise graph.GraphError("Only original negative evidence is eligible")
    try:
        graph.validate_evidence(evidence, node, expected_workspace_root=workspace)
    except graph.GraphError:
        pass
    else:
        raise graph.GraphError("Original evidence is already valid")
    normalized = copy.deepcopy(evidence)
    changed = False
    for name in ("quota_before", "quota_after"):
        value = normalized.get("telemetry", {}).get(name)
        if isinstance(value, dict):
            normalized["telemetry"][name] = _json_value(value)
            changed = True
    if not changed:
        raise graph.GraphError("Original evidence has no object quota to correct")
    graph.validate_evidence(normalized, node, expected_workspace_root=workspace)
    binding = {"schema_version": 1, "run_id": args.run_id, "node_id": args.node_id,
               "attempt_id": args.attempt_id, "attempt_number": number,
               "original_hashes": pins, "original_object_sha256": graph.sha256_value(original),
               "original_journal_fields": {k: v for k, v in row.items() if k not in {"phase", "negative_correction"}},
               "start_operation_id": start_id, "start_operation_sha256": graph.sha256_value(start),
               "plan_sha256": state["plan_hash"], "workspace_root": workspace}
    return journal, row, state, node, evidence, normalized, binding


def _correct_negative_evidence_locked(run_dir, args, journal=None):
    loaded, row, state, node, original, normalized, binding = _correction_subject(run_dir, args)
    folder = run_dir / "runner" / args.attempt_id
    supplied, _ = _strict_json(Path(args.evidence))
    graph.validate_evidence(supplied, node, expected_workspace_root=binding["workspace_root"])
    comparable = copy.deepcopy(supplied)
    for name in ("evidence_id", "summary"):
        comparable[name] = normalized[name]
    for name in ("quota_before", "quota_after"):
        if isinstance(original["telemetry"].get(name), dict):
            value = supplied["telemetry"][name]
            if not isinstance(value, str):
                raise graph.GraphError("Corrected object quota must be a JSON string")
            def unique(items):
                obj = {}
                for key, item in items:
                    if key in obj:
                        raise ValueError("duplicate quota key")
                    obj[key] = item
                return obj
            try:
                decoded = json.loads(value, object_pairs_hook=unique)
                canonical = _json_value(decoded)
            except (ValueError, TypeError) as exc:
                raise graph.GraphError("Invalid corrected quota JSON") from exc
            if not isinstance(decoded, dict) or canonical != normalized["telemetry"][name]:
                raise graph.GraphError("Corrected quota is not losslessly equivalent")
            comparable["telemetry"][name] = canonical
            supplied["telemetry"][name] = canonical
    if _json_value(comparable) != _json_value(normalized):
        raise graph.GraphError("Correction changes fields other than allowed metadata/quotas")
    corrected = folder / CORRECTED_EVIDENCE_NAME
    intent_path = folder / CORRECTION_INTENT_NAME
    receipt_path = folder / CORRECTION_RECEIPT_NAME
    corrected_bytes = (_json_value(supplied) + "\n").encode("utf-8")
    intent = {**binding, "corrected_sha256": hashlib.sha256(corrected_bytes).hexdigest(),
              "evidence_hash": graph.sha256_value(supplied),
              "result_operation_id": "op-negative-correction-" + args.attempt_id}
    existed = intent_path.exists()
    if existed:
        old, _ = _strict_json(intent_path)
        if _json_value(old) != _json_value(intent):
            raise graph.GraphError("Changed original pins or conflicting correction replay")
    elif row["phase"] != "STARTED" or node.get("active") is not True or node_state_status(state, args.node_id) not in {"running", "recovery-required"}:
        raise graph.GraphError("New correction requires the unresolved original attempt")
    _immutable_correction(intent_path, intent)
    _immutable_correction(corrected, supplied)
    _, corrected_hash = _strict_json(corrected)
    if corrected_hash != intent["corrected_sha256"]:
        raise graph.GraphError("Changed corrected evidence bytes")
    # Check all original pins again before the public mutation. command_result
    # owns the run lock; nesting it here would deadlock on Windows.
    if _correction_subject(run_dir, args)[-1] != binding:
        raise graph.GraphError("Correction subject changed before result")
    if receipt_path.exists():
        prior, _ = _strict_json(receipt_path)
        expected = {"intent_sha256": graph.sha256_value(intent), "result": state.get("operations", {}).get(intent["result_operation_id"], {}).get("result")}
        if prior != expected or row.get("negative_correction") != intent:
            raise graph.GraphError("Changed correction settlement receipt")
    result = graph.command_result(argparse.Namespace(state_root=args.state_root, run_id=args.run_id,
        node_id=args.node_id, operation_id=intent["result_operation_id"], evidence=str(corrected)))
    if any(result.get(key) != value for key, value in {
        "run_id": args.run_id, "node_id": args.node_id, "verdict": original["verdict"],
        "evidence_hash": intent["evidence_hash"], "evidence_id": supplied["evidence_id"],
    }.items()):
        raise graph.GraphError("Changed public correction result")
    post = graph.load_state(run_dir)
    matches = _stored_evidence_rows(post["nodes"][args.node_id], intent["evidence_hash"], original["verdict"])
    if len(matches) != 1 or post["nodes"][args.node_id]["status"] != original["verdict"]:
        raise graph.GraphError("Ambiguous corrected result settlement")
    stored, _ = _strict_json(Path(matches[0]["path"]))
    if _json_value(stored) != _json_value(supplied):
        raise graph.GraphError("Changed canonical corrected result")
    _, _, _, _, _, _, post_binding = _correction_subject(run_dir, args)
    if post_binding != binding:
        raise graph.GraphError("Correction subject changed during result")
    row.update(phase="RECORDED", negative_correction=intent)
    write(run_dir / "runner/journal.json", loaded)
    _immutable_correction(receipt_path, {"intent_sha256": graph.sha256_value(intent), "result": result})
    if journal is not None:
        journal.clear()
        journal.update(loaded)
    return {"status": NEGATIVE_CORRECTION_STATUS, "run_id": args.run_id,
            "node_id": args.node_id, "attempt_id": args.attempt_id,
            "verdict": original["verdict"], "evidence_hash": intent["evidence_hash"],
            "idempotent": existed, "note": "Negative outcome preserved; this is not acceptance."}


def node_state_status(state, node_id):
    return state.get("nodes", {}).get(node_id, {}).get("status")


def correct_negative_evidence(args):
    if not graph.RUN_ID_RE.fullmatch(args.run_id):
        raise graph.GraphError("Invalid correction run")
    try:
        if args.state_root is not None:
            _plain_path(Path(args.state_root).expanduser())
        root = _plain_path(graph.resolve_state_root(args.state_root))
        run_dir = _plain_path(root / args.run_id)
        with exclusive(run_dir / "runner/runner.lock"):
            return _correct_negative_evidence_locked(run_dir, args)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise graph.GraphError("Invalid correction subject: " + type(exc).__name__) from exc


def reconcile(run_dir, args, journal, errors, notify=None):
    state = graph.load_state(run_dir)
    del state
    for key, row in list(journal["attempts"].items()):
        folder = run_dir / "runner" / key
        if row.get("negative_correction") or _negative_correction_present(folder):
            previous_phase = row.get("phase")
            try:
                consume_args = argparse.Namespace(
                    state_root=args.state_root,
                    run_id=args.run_id,
                    node_id=row["node_id"],
                    attempt_id=key,
                    evidence=str(folder / CORRECTED_EVIDENCE_NAME),
                )
                result = _correct_negative_evidence_locked(run_dir, consume_args, journal)
            except (graph.GraphError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                errors[row["node_id"]] = {"status": "CORRECTION_INVALID", "error": str(exc)}
                continue
            if (
                notify
                and previous_phase not in SETTLED_PHASES
                and journal["attempts"][key].get("phase") in SETTLED_PHASES
            ):
                notify(row["node_id"], result.get("verdict") or "rejected")
            continue
        if row["phase"] in SETTLED_PHASES:
            continue
        folder = run_dir / "runner" / key
        finished = folder / "finished.json"
        if not finished.is_file():
            continue
        receipt = read(finished)
        if receipt.get("status") != "EXITED" or receipt.get("returncode") != 0:
            try:
                canonical = inspect_settled_failure(run_dir, journal, row, receipt)
            except graph.GraphError:
                canonical = None
            if canonical is not None:
                apply_failure_reconciliation(run_dir, journal, row, receipt, canonical)
                if notify:
                    notify(row["node_id"], canonical["verdict"])
                continue
            errors[row["node_id"]] = receipt
            continue
        result = folder / "adapter-result.json"
        if not result.is_file() or receipt.get("evidence_sha256") != graph.sha256_file(result):
            errors[row["node_id"]] = {"status": "MISSING_OR_CHANGED_EVIDENCE"}
            continue
        envelope = read(result)
        if set(envelope) != {"attempt_id", "evidence"} or envelope["attempt_id"] != key:
            errors[row["node_id"]] = {"status": "WRONG_ATTEMPT_EVIDENCE"}
            continue
        evidence = envelope["evidence"]
        if evidence.get("node_id") != row["node_id"]:
            errors[row["node_id"]] = {"status": "WRONG_NODE_EVIDENCE"}
            continue
        evidence_path = folder / "graph-evidence.json"
        write(evidence_path, evidence)
        try:
            graph.command_result(argparse.Namespace(state_root=args.state_root, run_id=args.run_id,
                node_id=row["node_id"], operation_id="op-runner-result-" + key, evidence=str(evidence_path)))
        except graph.GraphError as exc:
            errors[row["node_id"]] = {"status": "EVIDENCE_INVALID", "error": str(exc)}
            continue
        row["phase"] = "RECORDED"
        write(run_dir / "runner/journal.json", journal)
        if notify:
            notify(row["node_id"], "completed" if evidence["verdict"] == "accepted" else evidence["verdict"])


def validate_planning_dispatch(run_dir, state, plan, dispatch):
    """Bind complete new-plan dispatch intent without changing legacy approvals."""
    module, contract = graph.load_linked_planning_contract(run_dir, state, plan)
    requirements = module.dispatch_requirements(contract)
    if requirements['status'] == 'LEGACY_UNDECLARED':
        return requirements
    remaining = {key for key, row in state['nodes'].items()
                 if row['status'] not in {'accepted', 'inactive'}}
    missing = (set(requirements['required_dispatch_node_ids']) & remaining) - set(dispatch['nodes'])
    if missing:
        raise graph.GraphError('Missing planned dispatch adapters: ' + ', '.join(sorted(missing)))
    unexpected = set(requirements['manual_declarations']) & set(dispatch['nodes'])
    if unexpected:
        raise graph.GraphError('Manual planning nodes cannot be dispatched: ' + ', '.join(sorted(unexpected)))
    basis = requirements['capacity_basis']
    expected_policy = 'hard' if basis['kind'] == 'owner-hard-cap' else 'estimate'
    parallelism = dispatch.get('parallelism', {})
    if (dispatch['schema_version'] != 2 or dispatch['max_parallel'] != basis['value']
            or parallelism.get('policy') != expected_policy
            or parallelism.get('reason') != basis['reason']
            or parallelism.get('evidence', {}).get('planning_capacity_basis') != basis):
        raise graph.GraphError('Dispatch capacity differs from approved planning intent')
    return requirements


def drive(args):
    run_dir = graph.resolve_run_dir(graph.resolve_state_root(args.state_root), args.run_id)
    with exclusive(run_dir / "runner/runner.lock"):
        state = graph.load_state(run_dir)
        plan = graph.load_current_plan(run_dir, state)
        dispatch = validate_dispatch(read(args.dispatch), plan, args.run_id)
        validate_planning_dispatch(run_dir, state, plan, dispatch)
        host_capacity = host_capacity_snapshot()
        parallelism = resolve_parallelism(dispatch, host_capacity)
        folder = run_dir / "runner"
        journal_path = folder / "journal.json"
        fingerprint = graph.sha256_value(dispatch)
        journal = read(journal_path) if journal_path.exists() else {
            "dispatch_sha256": fingerprint,
            "attempts": {},
            "waits": {},
        }
        journal.setdefault("waits", {})
        if journal["dispatch_sha256"] != fingerprint:
            raise graph.GraphError("Dispatch changed after execution began; amend the graph explicitly")
        write(journal_path, journal)
        write(folder / "dispatch.json", dispatch)
        running = {}
        errors = {}
        nodes = {node["node_id"]: node for node in plan["nodes"]}
        office = ThreadPoolExecutor(max_workers=1, thread_name_prefix="graph-office")
        notifications = []
        office_failed = False
        office_home = getattr(args, "office_home", None)
        if not office_home and args.state_root:
            office_home = str(graph.resolve_state_root(args.state_root) / ".office")
        def deliver(node_id, status):
            nonlocal office_failed
            if office_failed:
                return {"status": "skipped", "reason": "office-circuit-open"}
            result = office_event(args.run_id, node_id, status, office_home)
            office_failed = result.get("status") != "ok"
            return result
        def notify(node_id, status):
            notifications.append(office.submit(deliver, node_id, status))
        def outcome(**payload):
            payload["run_id"] = args.run_id
            payload["scheduler"] = scheduler_diagnostics(
                run_dir, journal, parallelism
            )
            payload["reconciled_failures"] = [
                {
                    "attempt_id": key,
                    "node_id": row["node_id"],
                    "disposition": row.get("disposition"),
                    "verdict": row.get("canonical_verdict"),
                }
                for key, row in journal.get("attempts", {}).items()
                if row.get("phase") == "FAILURE_RECONCILED"
            ]
            return payload
        last_presence = time.monotonic()
        try:
            while True:
                wait_changed = False
                if running and time.monotonic() - last_presence >= 25:
                    notify("scheduler", "heartbeat")
                    last_presence = time.monotonic()
                reconcile(run_dir, args, journal, errors, notify)
                for node_id, error in errors.items():
                    wait_changed |= record_wait(
                        journal,
                        node_id,
                        "transport",
                        {"status": str(error.get("status", "unknown"))},
                    )
                state = graph.load_state(run_dir)
                if graph.sha256_value(graph.load_current_plan(run_dir, state)) != dispatch["graph_sha256"]:
                    if wait_changed:
                        write(journal_path, journal)
                    return outcome(
                        status="RECONCILE_REQUIRED",
                        reason="Graph revision changed while the schedule was running",
                    )
                if state["status"] != "ACTIVE":
                    if wait_changed:
                        write(journal_path, journal)
                    return outcome(status=state["status"])
                reaped = False
                for key, process in list(running.items()):
                    if process.poll() is not None:
                        process.wait()
                        del running[key]
                        reaped = True
                if reaped:
                    continue
                unresolved = [row for row in journal["attempts"].values() if row["phase"] not in SETTLED_PHASES]
                # Unknown inherited attempts are never redispatched, even after a crash.
                inherited = [row for row in unresolved if row["attempt_id"] not in running and
                             not (folder / row["attempt_id"] / "finished.json").exists()]
                if inherited:
                    if wait_changed:
                        write(journal_path, journal)
                    return outcome(
                        status="RECONCILE_REQUIRED",
                        attempts=inherited,
                        note="Supervisor may still be running; rerun after receipt, never blind retry.",
                    )
                ready = graph.command_ready(argparse.Namespace(state_root=args.state_root, run_id=args.run_id))
                candidates = [row["node_id"] for row in ready["ready"]] + ready.get("retry_eligible", [])
                for node_id, node in nodes.items():
                    node_state = state["nodes"][node_id]
                    if not node["active"] or node_state["status"] == "accepted":
                        continue
                    blocked_by = sorted(
                        dependency
                        for dependency in node["depends_on"]
                        if state["nodes"][dependency]["status"] != "accepted"
                    )
                    if blocked_by:
                        wait_changed |= record_wait(
                            journal,
                            node_id,
                            "dependency",
                            {"blocked_by": blocked_by},
                        )
                launched = False
                for node_id in candidates:
                    node = nodes[node_id]
                    if node_id in errors:
                        continue
                    if node_id not in dispatch["nodes"]:
                        if node["executor"] != "human" and node["capability"] not in {
                            "production-approval",
                            "deep-security-approval",
                            "final-acceptance",
                        }:
                            wait_changed |= record_wait(
                                journal,
                                node_id,
                                "missing-adapter",
                                {"dispatch_entry": False},
                            )
                        continue
                    if len(running) >= parallelism["effective"]:
                        wait_changed |= record_wait(
                            journal,
                            node_id,
                            "capacity",
                            {
                                "active_supervisors": len(running),
                                "effective_limit": parallelism["effective"],
                            },
                        )
                        continue
                    item = {"node": nodes[node_id], "dispatch": dispatch["nodes"][node_id]}
                    live = [{"node": nodes[journal["attempts"][key]["node_id"]],
                             "dispatch": dispatch["nodes"][journal["attempts"][key]["node_id"]]} for key in running]
                    conflicts = sorted(
                        {
                            reason
                            for other in live
                            for reason in conflict_reasons(item, other)
                        }
                    )
                    if conflicts:
                        for reason in conflicts:
                            wait_changed |= record_wait(
                                journal,
                                node_id,
                                reason,
                                {"active_conflicts": len(live)},
                            )
                        continue
                    verify_pins(item["dispatch"]["pinned_files"])
                    attempt_id = uuid.uuid4().hex
                    attempt_dir = folder / attempt_id
                    attempt_dir.mkdir()
                    start_operation_id = "op-runner-start-" + attempt_id
                    row = {
                        "attempt_id": attempt_id,
                        "node_id": node_id,
                        "phase": "PREPARED",
                        "start_operation_id": start_operation_id,
                    }
                    journal["attempts"][attempt_id] = row
                    write(journal_path, journal)
                    try:
                        started = graph.command_start(argparse.Namespace(state_root=args.state_root, run_id=args.run_id,
                            node_id=node_id, operation_id=start_operation_id,
                            workspace_root=item["dispatch"]["workspace_root"], external_ref=None))
                    except graph.GraphError as exc:
                        row["phase"] = "RECORDED"
                        errors[node_id] = {"status": "START_REFUSED", "error": str(exc)}
                        write(journal_path, journal)
                        continue
                    row["attempt_number"] = started["attempt"]
                    job = {"attempt_id": attempt_id, "node_id": node_id, "attempt": started["attempt"],
                           "start_operation_id": start_operation_id,
                           "dispatch": item["dispatch"], "timeout": started["dispatch_bounds"]["timeout"],
                           "queue_timeout": started["dispatch_bounds"]["timeout"],
                           "resources": nodes[node_id]["resource_locks"],
                           "lock_root": str(graph.resolve_state_root(args.state_root) / ".resource-locks"),
                           "capacity": host_capacity["capacity"],
                           "capacity_root": str(graph.resolve_state_root(args.state_root) / ".capacity-leases")}
                    write(attempt_dir / "job.json", job)
                    write(attempt_dir / "context.json", {"graph": plan, "state": state, "node": nodes[node_id],
                                                       "dispatch_bounds": started["dispatch_bounds"], "run_id": args.run_id})
                    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
                    process = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), "_worker", str(attempt_dir / "job.json")],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)
                    row.update(phase="STARTED", supervisor_pid=process.pid)
                    running[attempt_id] = process
                    write(journal_path, journal)
                    notify(node_id, "dispatch")
                    launched = True
                if not running and not launched:
                    pending = {key: row["status"] for key, row in state["nodes"].items()
                               if nodes[key]["active"] and row["status"] != "accepted"}
                    if wait_changed:
                        write(journal_path, journal)
                    return outcome(
                        status="NEEDS_ATTENTION" if errors else "WAITING" if pending else "EXECUTION_COMPLETE",
                        pending=pending,
                        errors=errors,
                        note="Execution is not final acceptance or production authorization.",
                    )
                if wait_changed:
                    write(journal_path, journal)
                time.sleep(0.1)
        finally:
            # Supervisors keep running and retaining locks on Ctrl-C; receipt recovery is idempotent.
            write(journal_path, journal)
            office.shutdown(wait=True)
            write(folder / "office-events.json", [future.result() for future in notifications if future.done() and not future.cancelled()])


def command_run(args):
    return drive(args)


def command_adopt_dispatch(args):
    return adopt_dispatch(args)


def command_reconcile_failure(args):
    return reconcile_failure(args)


PUBLIC_COMMANDS = {"adopt-dispatch", "reconcile-failure", "correct-negative-evidence"}
SUCCESS_STATUSES = {
    "EXECUTION_COMPLETE",
    "CLOSED",
    "WAITING",
    "DISPATCH_ADOPTED",
    "FAILURE_RECONCILED",
    "NEGATIVE_EVIDENCE_CORRECTED",
}


def build_parser(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in PUBLIC_COMMANDS:
        parser = argparse.ArgumentParser(
            description=__doc__,
            prog="graph_runner.py",
        )
        sub = parser.add_subparsers(dest="action", required=True)
        adopt = sub.add_parser(
            "adopt-dispatch",
            help="Adopt a new dispatch map after an accepted planning amendment",
        )
        adopt.add_argument("--run-id", required=True)
        adopt.add_argument(
            "--expected-old-dispatch-hash",
            required=True,
            help="SHA-256 of the currently stored dispatch map",
        )
        adopt.add_argument(
            "--dispatch",
            required=True,
            help="Path to the new dispatch map bound to the current approved plan",
        )
        adopt.add_argument("--state-root")
        recon = sub.add_parser(
            "reconcile-failure",
            help="Annotate a journal attempt as failure-reconciled from canonical evidence",
        )
        recon.add_argument("--run-id", required=True)
        recon.add_argument("--node-id", required=True)
        recon.add_argument("--attempt-id", required=True)
        recon.add_argument("--state-root")
        correction = sub.add_parser("correct-negative-evidence", help="Recover exact negative rc0 evidence with object quotas")
        for name in ("run-id", "node-id", "attempt-id", "evidence"):
            correction.add_argument("--" + name, required=True)
        correction.add_argument("--state-root")
        return parser.parse_args(argv)
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "Public operations: adopt-dispatch, reconcile-failure, correct-negative-evidence. "
            "Example: python graph_runner.py adopt-dispatch --run-id <id> "
            "--expected-old-dispatch-hash <sha256> --dispatch <new.json>"
        ),
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--dispatch", required=True)
    parser.add_argument("--state-root")
    parser.add_argument("--office-home")
    args = parser.parse_args(argv)
    args.action = "run"
    return args


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "_worker":
        raise SystemExit(worker(sys.argv[2]))
    args = build_parser()
    try:
        if getattr(args, "action", "run") == "adopt-dispatch":
            result = adopt_dispatch(args)
        elif getattr(args, "action", "run") == "reconcile-failure":
            result = reconcile_failure(args)
        elif getattr(args, "action", "run") == "correct-negative-evidence":
            result = correct_negative_evidence(args)
        else:
            result = drive(args)
        print(json.dumps(result, indent=2))
        raise SystemExit(0 if result["status"] in SUCCESS_STATUSES else 3)
    except (graph.GraphError, OSError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}))
        raise SystemExit(2)
