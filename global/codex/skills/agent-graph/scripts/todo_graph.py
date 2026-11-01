"""Small TODO scheduler. Old contracted graph runs are never loaded or rewritten."""
from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid
import child_terminal
import history_scout
import run_telemetry as telemetry
import team_support
import team_bridge
import artifact_recovery

sys.dont_write_bytecode = True
VERSION = "1.0.1"
ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$")
IGNORED = {".git", "node_modules", "__pycache__", ".venv", "venv"}


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def relative(value):
    p = Path(value)
    if not isinstance(value, str) or not value or p.is_absolute() or ".." in p.parts or ":" in value:
        raise ValueError("Scopes and inputs must be relative paths without traversal")
    return p


def strings(value, label, nonempty=True):
    if not isinstance(value, list) or (nonempty and not value) or not all(isinstance(x, str) and x.strip() for x in value):
        raise ValueError(label + " must be a list of nonempty strings")
    if len(set(value)) != len(value):
        raise ValueError(label + " contains duplicates")


VALID_CANDIDATE_POLICIES = frozenset({"open", "pin", "pinned"})


def validate_candidate_policy(policy, label="candidate_policy"):
    if policy is None:
        return
    if isinstance(policy, str):
        if policy.strip().casefold() not in VALID_CANDIDATE_POLICIES:
            raise ValueError(f"{label} must be 'open' or 'pin', got {policy!r}")
        return
    if isinstance(policy, dict):
        if not policy or set(policy) - {"maker", "reviewer"}:
            raise ValueError(f"{label} keys must be 'maker' or 'reviewer', got {list(policy)}")
        for lane, val in policy.items():
            if not isinstance(val, str) or val.strip().casefold() not in VALID_CANDIDATE_POLICIES:
                raise ValueError(f"{label}[{lane!r}] must be 'open' or 'pin', got {val!r}")
        return
    raise ValueError(f"{label} must be 'open' or 'pin', got {type(policy).__name__}")


_TREE_BUDGET_KEYS = {"max_parallel", "max_total_nodes", "max_depth", "allowed_pools", "allowed_scope"}


def _budget_count(budget, key, default=None):
    value = budget.get(key, default)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("tree_budget." + key + " must be a positive integer")
    return value


def _scope_within(scope, allowed):
    parts = tuple(part.casefold() for part in Path(scope).parts)
    for limit in allowed:
        limit_parts = tuple(part.casefold() for part in Path(limit).parts)
        if parts[:len(limit_parts)] == limit_parts:
            return True
    return False


def _validate_tree_budget(plan):
    budget = plan.get("tree_budget")
    if budget is None:
        budget = {}
    if not isinstance(budget, dict) or set(budget) - _TREE_BUDGET_KEYS:
        raise ValueError("tree_budget must be an object with max_parallel, max_total_nodes, max_depth, allowed_pools, allowed_scope")
    max_parallel = _budget_count(budget, "max_parallel")
    if max_parallel is not None and max_parallel > plan["max_parallel"]:
        raise ValueError("tree_budget.max_parallel cannot exceed plan max_parallel")
    max_total = _budget_count(budget, "max_total_nodes")
    if max_total is not None and len(plan["tasks"]) > max_total:
        raise ValueError("tree_budget.max_total_nodes exceeded by plan tasks")
    max_depth = _budget_count(budget, "max_depth", 1 if "max_depth" in budget else None)
    allowed_pools = budget.get("allowed_pools")
    if allowed_pools is not None:
        strings(allowed_pools, "tree_budget.allowed_pools")
    allowed_scope = budget.get("allowed_scope")
    if allowed_scope is not None:
        strings(allowed_scope, "tree_budget.allowed_scope")
        for item in allowed_scope:
            relative(item)
    # Dependency depth: roots are depth 0, so max_depth=1 allows one dependency edge.
    by_id = {task["id"]: task for task in plan["tasks"]}
    memo = {}

    def depth(task_id, seen=()):
        if task_id in memo:
            return memo[task_id]
        if task_id in seen:
            return 0
        deps = by_id[task_id].get("depends_on", [])
        value = 0 if not deps else 1 + max(depth(dep, seen + (task_id,)) for dep in deps)
        memo[task_id] = value
        return value

    for task in plan["tasks"]:
        if max_depth is not None and depth(task["id"]) > max_depth:
            raise ValueError("tree_budget.max_depth exceeded by task " + task["id"])
        if allowed_pools is not None:
            for role in ("maker", "reviewer"):
                for candidate in task.get(role, []):
                    if candidate.get("agent") not in allowed_pools:
                        raise ValueError("tree_budget.allowed_pools excludes candidate pool " + str(candidate.get("agent")))
        if allowed_scope is not None:
            for item in task.get("scope", []):
                if not _scope_within(item, allowed_scope):
                    raise ValueError("tree_budget.allowed_scope excludes scope " + str(item))
        own = task.get("tree_budget")
        if own is None:
            continue
        if not isinstance(own, dict) or set(own) - _TREE_BUDGET_KEYS:
            raise ValueError("task tree_budget must use tree_budget fields")
        for key in ("max_parallel", "max_total_nodes", "max_depth"):
            own_value, root_value = _budget_count(own, key), _budget_count(budget, key)
            if own_value is not None and root_value is not None and own_value > root_value:
                raise ValueError("task tree_budget." + key + " cannot expand the tree budget")
        if own.get("allowed_pools") is not None:
            strings(own["allowed_pools"], "task tree_budget.allowed_pools")
            if allowed_pools is not None and not set(own["allowed_pools"]) <= set(allowed_pools):
                raise ValueError("task tree_budget.allowed_pools cannot expand the tree budget")
        if own.get("allowed_scope") is not None:
            strings(own["allowed_scope"], "task tree_budget.allowed_scope")
            for item in own["allowed_scope"]:
                relative(item)
                if allowed_scope is not None and not _scope_within(item, allowed_scope):
                    raise ValueError("task tree_budget.allowed_scope cannot expand the tree budget")
            for item in task.get("scope", []):
                if not _scope_within(item, own["allowed_scope"]):
                    raise ValueError("task tree_budget.allowed_scope excludes scope " + str(item))
    return budget


def _effective_max_parallel(plan):
    values = [plan["max_parallel"]]
    budget = plan.get("tree_budget") if isinstance(plan.get("tree_budget"), dict) else {}
    if isinstance(budget.get("max_parallel"), int):
        values.append(budget["max_parallel"])
    for task in plan.get("tasks", []):
        own = task.get("tree_budget") if isinstance(task, dict) else None
        if isinstance(own, dict) and isinstance(own.get("max_parallel"), int):
            values.append(own["max_parallel"])
    return min(values)


def validate(plan, profiles=None):
    team_support.validate_team(plan)
    if plan.get("version") != 1 or not isinstance(plan.get("goal"), str) or not plan["goal"].strip():
        raise ValueError("Expected TODO version 1 and a goal")
    if not isinstance(plan.get("planner"), dict) or not all(plan["planner"].get(k) for k in ("model", "source")):
        raise ValueError("Record actual planner model and source; current parent is sufficient")
    planner_model = str(plan["planner"].get("model", "")).rsplit("/", 1)[-1].strip()
    if re.search(r"^(?:gemini-3\.1(?:$|[-._/]|pro)|gpt-6-sol(?:$|[^0-9]))|contributor", planner_model, re.I):
        raise ValueError(f"Planner model is forbidden (DENY): {planner_model}")
    strings(plan.get("requirements"), "requirements")
    if "candidate_policy" in plan and plan["candidate_policy"] is not None:
        validate_candidate_policy(plan["candidate_policy"], "plan candidate_policy")
    if not isinstance(plan.get("max_parallel"), int) or isinstance(plan["max_parallel"], bool) or plan["max_parallel"] < 1:
        raise ValueError("max_parallel must describe actual available capacity")
    root = Path(plan["workspace"])
    if not root.is_absolute() or not root.is_dir():
        raise ValueError("workspace must be an existing absolute directory")
    tasks = plan.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("TODO needs tasks")
    ids = [t.get("id", "") for t in tasks]
    if any(not ID.fullmatch(x) for x in ids) or len(set(ids)) != len(ids):
        raise ValueError("Task IDs must be unique safe identifiers")
    covered = set()
    for task in tasks:
        if not task.get("text") or task.get("mode", "implement") not in {"implement", "advisory"}:
            raise ValueError("Task requires text and implement/advisory mode")
        if "web" in task:
            web = task["web"]
            if web is not False and web != "managed-chrome":
                raise ValueError("web must be false or exactly managed-chrome")
            if web == "managed-chrome" and (
                task.get("STOP") or task.get("stop")
                or "STOP" in task.get("requires_approval", [])
                or task.get("mode", "implement") != "implement"
            ):
                raise ValueError("managed-chrome opt-in refused by STOP or non-implementation TODO")
        strings(task.get("covers"), "covers")
        covered.update(task["covers"])
        strings(task.get("done_when"), "done_when")
        strings(task.get("scope"), "scope")
        for item in task["scope"] + task.get("inputs", []):
            relative(item)
        strings(task.get("depends_on", []), "depends_on", False)
        if not set(task.get("depends_on", [])) <= set(ids) - {task["id"]}:
            raise ValueError("Unknown or self dependency")
        for role in ("maker", "reviewer"):
            candidates = task.get(role)
            if not isinstance(candidates, list) or not candidates:
                raise ValueError(role + " needs a ranked candidate list")
            for c in candidates:
                if not all(isinstance(c.get(k), str) and c[k] for k in ("agent", "model", "reason")):
                    raise ValueError("Each candidate needs agent, model and fit reason")
                if not isinstance(c.get("score"), int) or c["score"] not in range(0, 101, 5):
                    raise ValueError("Fit score must be 0..100 in steps of 5")
        for key in ("min_tier_execute", "reviewer_min_tier"):
            if task.get(key) is not None and task[key] not in ("D", "M", "K", "W"):
                raise ValueError(key + " must be one of D, M, K, W")
        tool_budgets = task.get("max_model_tool_calls", {})
        if not isinstance(tool_budgets, dict) or set(tool_budgets) - {"maker", "reviewer"}:
            raise ValueError("max_model_tool_calls must be a maker/reviewer object")
        if any(type(value) is not int or not 1 <= value <= 256
               for value in tool_budgets.values()):
            raise ValueError("max_model_tool_calls values must be integers from 1 to 256")
        checks = task.get("checks", [])
        if not isinstance(checks, list) or (task.get("mode", "implement") == "implement" and not checks):
            raise ValueError("Implement task requires at least one executable check")
        for check in checks:
            strings(check, "check argv")
        strings(task.get("requires_approval", []), "requires_approval", False)
        if "candidate_policy" in task and task["candidate_policy"] is not None:
            validate_candidate_policy(task["candidate_policy"], f"task {task.get('id', '')} candidate_policy")
    if covered != set(plan["requirements"]):
        raise ValueError("TODO must cover every requirement, without invented requirements")
    pending, done = list(tasks), set()
    while pending:
        ready = [t for t in pending if set(t.get("depends_on", [])) <= done]
        if not ready:
            raise ValueError("Dependency cycle")
        done.update(t["id"] for t in ready)
        pending = [t for t in pending if t not in ready]
    if profiles is not None:
        from todo_fleet import validate_candidate_modes
        for task in tasks:
            validate_candidate_modes(task, profiles)
    _validate_tree_budget(plan)
    return plan


def snapshot(workspace, scope):
    root = Path(workspace).resolve()
    result = {}
    for value in scope:
        path = root / relative(value)
        if not path.resolve().is_relative_to(root):
            raise ValueError("Scope escapes workspace through a link")
        files = [path] if path.is_file() else path.rglob("*") if path.is_dir() else []
        for file in files:
            rel = file.relative_to(root)
            if set(rel.parts) & IGNORED:
                continue
            if file.is_symlink() or not file.resolve().is_relative_to(root):
                raise ValueError("Linked scope is not supported")
            if file.is_file():
                h = hashlib.sha256()
                with file.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        h.update(chunk)
                result[rel.as_posix()] = h.hexdigest()
    return digest(result)


def overlaps(first, second):
    for a in team_support.effective_scope(first):
        for b in team_support.effective_scope(second):
            pa, pb = Path(a).parts, Path(b).parts
            if tuple(x.casefold() for x in pa[:len(pb)]) == tuple(x.casefold() for x in pb) or tuple(x.casefold() for x in pb[:len(pa)]) == tuple(x.casefold() for x in pa):
                return True
    return bool(set(first.get("resources", [])) & set(second.get("resources", [])))


@contextmanager
def exclusive(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    owner_content = f"{os.getpid()}:{token}\n"
    owner_bytes = owner_content.encode("utf-8")
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0))
    try:
        os.write(fd, owner_bytes)
        initial_stat = os.fstat(fd)
    finally:
        os.close(fd)

    try:
        yield
    finally:
        try:
            if path.is_file():
                current_stat = path.lstat()
                same_identity = (
                    not path.is_symlink()
                    and not getattr(current_stat, "st_file_attributes", 0) & 0x400
                    and (initial_stat.st_dev, initial_stat.st_ino)
                    == (current_stat.st_dev, current_stat.st_ino)
                )

                if same_identity and path.read_bytes() == owner_bytes:
                    released_name = f"{path.name}.{os.getpid()}.{token}.released"
                    released_path = path.with_name(released_name)
                    if released_path.exists():
                        raise OSError("Retained lease destination already exists")
                    path.rename(released_path)
        except OSError:
            pass


def _valid_pid(pid):
    return type(pid) is int and 0 < pid <= 0xFFFFFFFF


def _start_epoch(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        if not isinstance(value, str):
            return None
        try:
            stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if stamp.tzinfo is None:
                return None
            result = stamp.timestamp()
        except (ValueError, OSError, OverflowError):
            return None
    return result if math.isfinite(result) and result > 0 else None


def _windows_process_probe(pid):
    """Read liveness and creation on one handle, without a subprocess or cache."""
    result = {'alive': None, 'created_at': None, 'creation_filetime': None,
              'method': 'win32-getprocesstimes'}
    if not _valid_pid(pid) or os.name != 'nt':
        return result
    import ctypes
    from ctypes import wintypes
    handle = None
    try:
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.GetProcessTimes.restype = wintypes.BOOL
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION only
        if not handle:
            error = ctypes.get_last_error()
            result['error'] = error
            if error == 87:  # valid PID absent; ACCESS_DENIED remains unknown
                result['alive'] = False
            return result
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            result['error'] = ctypes.get_last_error()
            return result
        ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        result.update(created_at=ticks / 10_000_000 - 11_644_473_600,
                      creation_filetime=ticks)
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            result['error'] = ctypes.get_last_error()
            return result
        # Exit code 259 after exit can conservatively look live, never dead.
        result['alive'] = code.value == 259
        return result
    except (OSError, AttributeError, TypeError, ValueError, OverflowError):
        return result
    finally:
        if handle:
            kernel.CloseHandle(handle)


def _process_alive(pid, *, probe=None):
    """Return True/False/None(unknown) for a process-id liveness check."""
    if not _valid_pid(pid):
        return None
    if os.name == "nt":
        return (probe if probe is not None else _windows_process_probe(pid))['alive']
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return None


def _process_start_time(pid, *, probe=None):
    """Return a comparable creation-time value or None when unavailable."""
    if not _valid_pid(pid) or os.name != "nt":
        return None
    return (probe if probe is not None else _windows_process_probe(pid))['created_at']


_UNSAMPLED = object()


def _pid_reused(pid, recorded_start, *, alive=_UNSAMPLED, current_start=_UNSAMPLED):
    """Return True / False / None(unknown) for PID-reuse detection."""
    if not _valid_pid(pid):
        return None
    recorded = _start_epoch(recorded_start)
    if recorded is None:
        return None
    if alive is _UNSAMPLED:
        alive = _process_alive(pid)
    if alive is None:
        return None
    if alive is False:
        return False
    current = _start_epoch(_process_start_time(pid) if current_start is _UNSAMPLED else current_start)
    if current is None:
        return None
    # Legacy timestamps are sampled AFTER Popen, not exact creation times.
    # Earlier/different creation never proves reuse; close timestamps block live.
    return current > recorded + 1.0


def _newer_instance_proven(entry):
    recorded = _start_epoch(entry.get('started'))
    created = _start_epoch(entry.get('created_at'))
    ticks = entry.get('creation_filetime')
    return (entry.get('probe_method') == 'win32-getprocesstimes'
            and entry.get('pid_alive') is True and _valid_pid(entry.get('pid'))
            and type(ticks) is int and ticks > 0
            and recorded is not None and created is not None
            and abs(ticks / 10_000_000 - 11_644_473_600 - created) < 0.000001
            and created > recorded + 1.0)


def command(argv, cwd, folder, timeout, env=None):
    folder.mkdir(parents=True, exist_ok=True)
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    child_environment = os.environ.copy()
    if env:
        child_environment.update(env)
    began = time.monotonic()
    with (folder / "stdout.txt").open("wb") as out, (folder / "stderr.txt").open("wb") as err:
        proc = subprocess.Popen(
            argv, cwd=cwd, stdout=out, stderr=err, stdin=subprocess.DEVNULL,
            env=child_environment, **options,
        )
        write(folder / "process.json", {"pid": proc.pid, "started": time.time()})
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Stop this owned process tree, never an unrelated CLI session.
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, **options)
            else:
                proc.kill()
            proc.wait()
            code = 124
    result = {"exit_code": code, "seconds": round(time.monotonic() - began, 3), "stdout": str(folder / "stdout.txt")}
    write(folder / "finished.json", result)
    return result


class FleetAdapter:
    def __init__(self, bridge=None):
        self.bridge = Path(bridge or Path(__file__).with_name("todo_fleet.py"))

    def validate_plan(self, plan):
        from todo_fleet import validate_native_plan
        validate_native_plan(plan)

    def invoke(self, plan, task, role, workspace, folder, feedback, maker=None, child_env=None):
        # Autopsy reuses the read-only reviewer lane: same qualification, lineage
        # gate and tier substitution (the autopsy task carries reviewer_min_tier).
        role = "reviewer" if role == "autopsy" else role
        request = {"goal": plan["goal"], "task": task, "role": role, "workspace": str(workspace),
                   "feedback": feedback, "maker": maker, "constraints": plan.get("constraints", [])}
        if 'candidate_policy' in plan:
            request['candidate_policy'] = plan['candidate_policy']
        for field in ('office_channel', 'office_parent'):
            if field in plan:
                request[field] = plan[field]
                request['task'] = {**request['task'], field: plan[field]}
        if 'project_slug' in plan and plan['project_slug']:
            request['project_slug'] = plan['project_slug']
        else:
            from native_session_journal import parent_project_slug
            project = parent_project_slug(plan)
            if project is not None:
                request['project_slug'] = project
        write(folder / "request.json", request)
        result = command([sys.executable, "-B", str(self.bridge), "--request", str(folder / "request.json"),
                          "--output", str(folder / "result.json")], workspace, folder,
                         task.get("timeout_seconds", 3600) + 360, env=child_env)
        if not (folder / "result.json").is_file():
            return {"status": "BLOCKED", "reason": "Adapter did not return a result", "process": result}
        return read(folder / "result.json")


_FleetAdapterClass = FleetAdapter


def invoke_adapter(adapter, plan, task, role, workspace, folder, feedback, maker, child_env):
    is_fleet = False
    try:
        if isinstance(FleetAdapter, type) and isinstance(adapter, FleetAdapter):
            is_fleet = True
    except TypeError:
        is_fleet = False
    if not is_fleet:
        try:
            if isinstance(adapter, _FleetAdapterClass):
                is_fleet = True
        except TypeError:
            is_fleet = False

    if is_fleet:
        return adapter.invoke(
            plan, task, role, workspace, folder, feedback, maker, child_env=child_env,
        )
    if maker is None:
        return adapter.invoke(plan, task, role, workspace, folder, feedback)
    return adapter.invoke(plan, task, role, workspace, folder, feedback, maker)


def _scout_material(task, folder):
    """Discovered scout input of this run, or None (no scout / no discovery)."""
    path = Path(folder) / 'context' / 'historical-input.json'
    if task.get('role') != 'scout' or not path.is_file():
        return None
    return read(path)


def run_task(plan, task, folder, adapter, journal=None, child_env=None):
    journal = journal or telemetry.Journal(folder, enabled=False)
    journal.emit("node_start", node=task["id"])
    try:
        # Scout brief (topics/consumers/record protocol) before any native model.
        task = history_scout.prepare_scout_task(
            plan, task, folder=Path(folder) / 'context',
            discover_context=isinstance(adapter, _FleetAdapterClass)
                or bool(getattr(adapter, 'native_historical_scout', False)))
        task, knowledge = team_bridge.prepare_task(plan, task, Path(folder) / 'context')
        uat_before = None
        if task.get('role') == 'uat':
            if any(snapshot(plan['workspace'], [item]) == digest({}) for item in task['uat']['artifact_scope']):
                raise ValueError('UAT_ARTIFACT_MISSING')
            uat_before = snapshot(plan['workspace'], task['uat']['artifact_scope'])
            if uat_before == digest({}):
                raise ValueError('UAT_ARTIFACT_MISSING')
        result = _run_task(plan, task, folder, adapter, journal, child_env)
        if knowledge:
            result['knowledge'] = knowledge
        if uat_before is not None:
            after = snapshot(plan['workspace'], task['uat']['artifact_scope'])
            result['uat_evidence'] = {'artifact_digest': uat_before, 'artifact_scope': task['uat']['artifact_scope'],
                                      'unchanged_during_uat': after == uat_before,
                                      'environment': task['uat']['environment'], 'final': task['uat']['final'],
                                      'scenarios': task['uat']['scenarios']}
            if after != uat_before:
                result.update(status='BLOCKED', reason='UAT_ARTIFACT_CHANGED')
        if knowledge or uat_before is not None:
            write(Path(folder) / 'result.json', result)
            write(Path(folder) / 'progress.json', result)
    except Exception as exc:
        journal.emit("node_terminal", node=task["id"], status="BLOCKED", code=type(exc).__name__)
        raise
    journal.emit("node_terminal", node=task["id"], status=result.get("status"), round=result.get("round"))
    return result


# Owner decision 2026-10-13: a non-converging REPAIR loop is escalated, not
# stopped. Every AUTOPSY_EVERY rejected rounds, and on stagnation, one read-only
# Manager-class (Director if no Manager qualifies) autopsy picks one action.
AUTOPSY_EVERY = 3
AUTOPSY_ACTIONS = ("CONTINUE", "SWAP_MAKER", "SWAP_REVIEWER", "OWNER_QUESTION")
_AUTOPSY_ACTION = re.compile(r"AUTOPSY_ACTION:\s*(" + "|".join(AUTOPSY_ACTIONS) + r")\b")
_AUTOPSY_TEXT_LIMIT = 600


def autopsy_enabled():
    """Rollback: AGENT_GRAPH_AUTOPSY=0 restores the stop-for-parent-diagnosis loop."""
    return os.environ.get("AGENT_GRAPH_AUTOPSY", "1").strip() != "0"


def _identity(result):
    result = result if isinstance(result, dict) else {}
    return {key: result.get(key) for key in ("agent", "model", "lineage")}


def _node_task(task, state):
    """The node's task with autopsy-swapped candidates left out."""
    excluded = state.get("excluded") or {}
    if not excluded or not any(excluded.get(lane) for lane in ("maker", "reviewer")):
        return task
    lanes = {}
    for lane in ("maker", "reviewer"):
        if lane in task:
            skip = {(c.get("agent"), c.get("model"))
                    for c in excluded.get(lane, [])
                    if isinstance(c, dict) and c.get("agent") and c.get("model")}
            agent_skips = {c.get("agent")
                           for c in excluded.get(lane, [])
                           if isinstance(c, dict) and c.get("agent") and not c.get("model")}
            kept = [c for c in task.get(lane, [])
                    if isinstance(c, dict) and (c.get("agent"), c.get("model")) not in skip
                    and c.get("agent") not in agent_skips]
            if len(kept) != len(task.get(lane, [])):
                lanes[lane] = kept
    # Carried for catalog substitution, which picks outside the declared list.
    return {**task, **lanes, "excluded_candidates": excluded}


def _swap(task, state, lane, used):
    """Skip the used candidate in this node."""
    if not isinstance(task, dict) or not isinstance(state, dict):
        return False
    if lane not in ("maker", "reviewer"):
        return False
    if not isinstance(used, dict):
        return False
    agent = used.get("agent")
    if not isinstance(agent, str) or not agent.strip():
        return False
    agent = agent.strip()
    model = used.get("model")
    if model is not None:
        if not isinstance(model, str) or not model.strip():
            return False
        model = model.strip()

    if model:
        targets = [{"agent": agent, "model": model}]
    else:
        current = task.get(lane, []) if isinstance(task.get(lane), list) else []
        targets = [{"agent": c["agent"], "model": c["model"]}
                   for c in current
                   if isinstance(c, dict) and c.get("agent") == agent and c.get("model")]
        if not targets:
            targets = [{"agent": agent}]

    excluded_lane = state.setdefault("excluded", {}).setdefault(lane, [])
    existing = {(c.get("agent"), c.get("model")) for c in excluded_lane if isinstance(c, dict)}
    for target in targets:
        key = (target.get("agent"), target.get("model"))
        if key not in existing:
            excluded_lane.append(target)
            existing.add(key)
    return True


def _note_swap_bypass(state, lane, result):
    """A swapped-out executor came back (e.g. via catalog substitution): tell the next autopsy."""
    used = (result or {}).get("agent"), (result or {}).get("model")
    if state.get("autopsies") and any((c["agent"], c["model"]) == used
                                      for c in (state.get("excluded") or {}).get(lane, [])):
        state["autopsies"][-1]["swap_bypassed"] = True


def _autopsy_candidates(task, floor):
    """Reviewer lane for the autopsy: Manager first, Director only when no Manager qualifies.

    Declared reviewers of exactly the floor class run directly. Otherwise only
    below-floor reviewers stay (possibly none): tier routing disallows them, so
    Fleet's catalog substitution (smallest sufficient tier first) tries every
    qualified M before any D. Without tier routing nothing enforces the class,
    so only declared reviewers at or above the floor may run; the legacy score
    tie-break sees Directors below every Manager, so a Director runs only when
    no Manager passes preflight."""
    import model_tiers
    declared = list(task.get("reviewer", []))
    catalog = model_tiers.load_catalog()
    ranks = [model_tiers._tier_rank((model_tiers.lookup(catalog, c["agent"], c["model"]) or {}).get("tier"))
             for c in declared]
    need = model_tiers._tier_rank(floor)
    if os.environ.get("AGENT_GRAPH_TIER_ROUTING", "1") == "0":
        exact = [c for c, rank in zip(declared, ranks) if rank == need]
        above = [c for c, rank in zip(declared, ranks) if rank > need]
        if exact and above:
            # Outside the legacy 5-point near-tie band of the weakest Manager.
            ceiling = min(c["score"] for c in exact) - 6
            above = [{**c, "score": min(c["score"], ceiling)} for c in above]
        return exact + above
    exact = [c for c, rank in zip(declared, ranks) if rank == need]
    if exact:
        return exact
    return [c for c, rank in zip(declared, ranks) if rank < need]


def _autopsy_task(task, state, trigger, findings, tests, maker, reviewer):
    context = {
        "trigger": trigger, "round": state["round"],
        "rejected_rounds_since_last_autopsy": state.get("rejected_since_autopsy", 0),
        "last_findings": [str(f)[:_AUTOPSY_TEXT_LIMIT] for f in (findings or [])[:12]],
        "failed_checks": [{"index": i, "exit_code": t.get("exit_code")}
                          for i, t in enumerate(tests) if t.get("exit_code") != 0],
        "progress_signatures": state.get("progress_signatures", [])[-6:],
        "maker": _identity(maker), "reviewer": _identity(reviewer),
        "previous_autopsies": [{k: a.get(k) for k in ("round", "trigger", "action", "swapped",
                                                      "swap_bypassed", "artifact")}
                               for a in state.get("autopsies", [])[-3:]],
    }
    text = (task["text"] + "\n\nAUTOPSY of this stuck TODO node (read-only, advisory). The maker/reviewer "
            "loop below is not converging. Inspect done_when, the actual files in scope, failed checks and "
            "findings, and diagnose the root cause. You grant no acceptance and no permission. Choose exactly "
            "one action: CONTINUE (a concrete repair brief for the same maker), SWAP_MAKER (this maker cannot "
            "do it), SWAP_REVIEWER (the reviewer's demands contradict done_when or are inconsistent), "
            "OWNER_QUESTION (only a business decision or requirement from the owner can settle it). If a "
            "previous autopsy chose CONTINUE and the artifact did not change, do not choose CONTINUE again. "
            "Reply in the review schema: verdict REPAIR; every summary field starts with 'AUTOPSY_ACTION: <ACTION>' then "
            "the diagnosis; findings hold the maker brief, or the single owner question; checked lists the "
            "done_when strings you examined.\n" + json.dumps(context, ensure_ascii=False))
    floor = "D" if "D" in {task.get("reviewer_min_tier"), task.get("min_tier_execute")} else "M"
    return {**task, "text": text, "reviewer": _autopsy_candidates(task, floor), "reviewer_min_tier": floor,
            "review_complexity": "high"}


def _autopsy(plan, task, state, folder, adapter, journal, child_env, trigger, findings, tests,
             maker, reviewer, workspace):
    """One autopsy call. Returns (action, feedback) or (None, None) after a terminal state."""
    tests = [t for t in tests or [] if isinstance(t, dict)]
    failed = [t for t in tests if t.get("exit_code") != 0]
    reviewer = reviewer or next((h["result"] for h in reversed(state["history"])
                                 if h.get("role") == "reviewer"), None)
    before = snapshot(workspace, task["scope"])
    try:
        autopsy_task = _autopsy_task(task, state, trigger, findings, tests, maker, reviewer)
    except Exception as exc:  # any unreadable/malformed catalog is a reasoned stop, never a lost node result
        state.update(status="BLOCKED", reason="AUTOPSY_FAILED: model catalog unavailable (" + type(exc).__name__ + ")")
        return None, None
    journal.emit("autopsy", node=task["id"], code=trigger, round=state["round"])
    with telemetry.stage(journal, task["id"], "autopsy", round=state["round"]):
        # All checks keep their original index, so evidence maps to the right command.
        result = invoke_adapter(
            adapter, plan, autopsy_task,
            "autopsy", workspace, folder / str(state["round"]) / "autopsy", {"tests": tests}, maker, child_env)
    telemetry.provider_result(journal, task["id"], "autopsy", result, state["round"])
    state["history"].append({"role": "autopsy", "trigger": trigger, "result": result})
    if snapshot(workspace, task["scope"]) != before:
        state.update(status="BLOCKED", reason="Artifact changed during read-only autopsy; preserve and inspect")
        return None, None
    # Native reviewer envelopes carry the review in quality_gate plus an outer
    # summary; todo_fleet exposes the latter as native_summary. Models put the
    # marker in either field (5/5 Qwen autopsies on 2026-10-14 used the outer one).
    match = None
    if result.get("status") == "REVIEWED":
        for text in (result.get("summary"), result.get("native_summary")):
            match = _AUTOPSY_ACTION.search(str(text or ""))
            if match is not None:
                break
    if match is None:
        state.update(status="BLOCKED", reason="AUTOPSY_FAILED: " + str(
            result.get("reason") or ("no AUTOPSY_ACTION in review" if result.get("status") == "REVIEWED"
                                     else "autopsy transport failed")))
        return None, None
    action = match.group(1)
    brief = [str(f) for f in result.get("findings", []) if str(f).strip()]
    swapped = None
    if action == "SWAP_MAKER":
        swapped = _swap(task, state, "maker", _identity(maker))
    elif action == "SWAP_REVIEWER":
        swapped = _swap(task, state, "reviewer", _identity(reviewer))
    state.setdefault("autopsies", []).append({
        "round": state["round"], "trigger": trigger, "action": action, "swapped": swapped,
        "artifact": before, "autopsy": _identity(result)})
    state["rejected_since_autopsy"] = 0
    feedback = {"findings": ["AUTOPSY " + action + ": " + str(result["summary"])] + brief
                + [str(f) for f in (findings or [])], "failed_tests": failed}
    # Persisted until newer findings replace it, so --retry-task and recovery keep the brief.
    state["next_feedback"] = feedback
    if action == "OWNER_QUESTION":
        state.update(status="WAITING_APPROVAL", reason="AUTOPSY_OWNER_QUESTION",
                     requires=["autopsy-answer:" + task["id"]], question=brief or [result["summary"]])
        return action, None
    return action, feedback


def _pool_wait_info(result, task, plan):
    if not isinstance(result, dict):
        return None
    reasons = result.get("pool_wait_reasons")
    if not isinstance(reasons, list) or not reasons:
        reason = str(result.get("reason") or "")
        reasons = [reason] if ("Pool availability:" in reason or "rate-limit" in reason.casefold() or "rate limit" in reason.casefold() or "429" in reason) else []
    is_wait = result.get("status") == "RESUMABLE_POOL_WAIT" or result.get("resumable_pool_wait") is True or bool(reasons)
    if not is_wait or not reasons:
        return None
    allowed = (result.get("status") == "RESUMABLE_POOL_WAIT" or result.get("resumable_pool_wait") is True
               or task.get("allow_pool_wait") is True or task.get("durable") is True
               or plan.get("allow_pool_wait") is True or plan.get("durable") is True)
    if not allowed and result.get("status") != "RESUMABLE_POOL_WAIT":
        return None
    return {"pool_wait_reasons": [str(item) for item in reasons], "next_check_at": result.get("next_check_at")}


def _run_task(plan, task, folder, adapter, journal, child_env=None):
    workspace = Path(plan["workspace"])
    state = read(folder / "progress.json") if (folder / "progress.json").exists() else {"status": "RUNNING", "round": 0, "history": []}
    if state.get("status") == "CANCELLED":
        return state
    feedback, prior_signature, stalled = state.get("next_feedback", []), None, 0
    autopsy_on = autopsy_enabled()
    base_task = task
    if state.get("reason") == "AUTOPSY_OWNER_QUESTION":
        # Retried after the owner answered: resume the same node history.
        for key in ("reason", "requires", "question"):
            state.pop(key, None)
        state["status"] = "RUNNING"
    for file in task.get("inputs", []):
        p = workspace / relative(file)
        if not p.is_file() or not p.resolve().is_relative_to(workspace.resolve()):
            return {"status": "BLOCKED", "reason": "Missing or unsafe input: " + file}
    material = _scout_material(task, folder)
    if material is not None and material.get('model_required') is False:
        # Deterministic pre-check: nothing worth a model selection pass.
        tests = [command(argv, workspace, folder / 'precheck' / ('check-' + str(i)),
                         task.get('timeout_seconds', 3600), env=child_env)
                 for i, argv in enumerate(task.get('checks', []))]
        passed = all(t['exit_code'] == 0 for t in tests)
        state = {'status': 'ACCEPTED' if passed else 'BLOCKED', 'round': 0, 'history': [],
                 'acceptance': 'deterministic-precheck', 'model_calls': 0, 'tests': tests,
                 'material_status': material.get('status'), 'material_reason': material.get('reason'),
                 'transport_status': 'not-invoked', 'artifact_status': 'precheck',
                 'acceptance_status': 'ACCEPTED' if passed else 'BLOCKED',
                 'knowledge_closeout': 'PARENT_PENDING'}
        if passed:
            state.update(artifact=snapshot(workspace, task['scope']), workspace=str(workspace))
        else:
            state['reason'] = 'SCOUT_PRECHECK_CHECKS_FAILED'
        write(folder / "progress.json", state)
        write(folder / "result.json", state)
        return state
    while True:
        task = _node_task(base_task, state)
        resume_review = state.pop("review_pending", None)
        state["round"] += 1
        attempt = folder / str(state["round"])
        if resume_review:
            if resume_review["artifact"] != snapshot(workspace, task["scope"]):
                return {**state, "status": "BLOCKED", "reason": "Retained artifact changed; explicit repair needed"}
            maker = resume_review["maker"]
        else:
            with telemetry.stage(journal, task["id"], "maker", round=state["round"]):
                maker = invoke_adapter(
                    adapter, plan, task, "maker", workspace, attempt / "maker",
                    feedback, None, child_env,
                )
            state["history"].append({"role": "maker", "result": maker})
            _note_swap_bypass(state, "maker", maker)
        telemetry.provider_result(journal, task["id"], "maker", maker, state["round"])
        if resume_review:
            journal.emit("recovery", node=task["id"], recovery="retained-artifact", round=state["round"])
        if maker.get("status") not in {"CANDIDATE", "PARTIAL"}:
            pool_wait = _pool_wait_info(maker, task, plan)
            if pool_wait:
                state.update(status="RESUMABLE_POOL_WAIT", reason=maker.get("reason", "Pool availability"), pool_phase="maker", **pool_wait)
            else:
                state.update(status="BLOCKED", reason=maker.get("reason", "Maker transport/preflight failed; artifacts preserved"))
            break
        saved_tests = resume_review.get('tests') if resume_review else None
        reuse_tests = (isinstance(saved_tests, list)
                       and resume_review.get('checks_digest') == digest(task.get('checks', []))
                       and resume_review.get('inputs_digest') == snapshot(workspace, task.get('inputs', []))
                       and len(saved_tests) == len(task.get('checks', []))
                       and all(t.get('exit_code') == 0 and Path(t.get('stdout', '')).is_file()
                               for t in saved_tests))
        with telemetry.stage(journal, task["id"], "tests", round=state["round"]):
            tests = saved_tests if reuse_tests else [
                command(argv, workspace, attempt / ("check-" + str(i)),
                        task.get("timeout_seconds", 3600), env=child_env)
                for i, argv in enumerate(task.get("checks", []))]
        state['test_evidence_reused'] = bool(reuse_tests)
        for test in tests:
            journal.emit("test_result", node=task["id"], exit_code=test["exit_code"], seconds=test["seconds"])
        before = snapshot(workspace, task["scope"])
        failed_tests = [t for t in tests if t["exit_code"] != 0]
        if failed_tests:
            journal.emit("repair", node=task["id"], code="TEST_FAILURE", round=state["round"])
            feedback = {"failed_tests": failed_tests}
            state.pop("next_feedback", None)
            state["history"].append({"role": "tests", "tests": tests, "artifact": before})
            signature = digest([before, [t["exit_code"] for t in failed_tests]])
            state["rejected_since_autopsy"] = state.get("rejected_since_autopsy", 0) + 1
            state.setdefault("progress_signatures", []).append(signature)
            stagnant = signature == prior_signature
            if stagnant and not autopsy_on:
                state.update(status="BLOCKED", reason="Same failed tests and artifact twice; diagnose instead of repeating")
                break
            prior_signature = signature
            if autopsy_on and (stagnant or state["rejected_since_autopsy"] >= AUTOPSY_EVERY):
                action, feedback = _autopsy(
                    plan, base_task, state, folder, adapter, journal, child_env,
                    "stagnation" if stagnant else "rejections", [], tests, maker, None, workspace)
                if action is None or action == "OWNER_QUESTION":
                    break
                prior_signature, stalled = None, 0
            write(folder / "progress.json", state)
            continue
        material = _scout_material(task, folder)
        if material is not None:
            # Scout acceptance is deterministic grounding: evidence and caveats are
            # replaced by source excerpts, so a model reviewer would judge discarded text.
            handoff = maker.get("handoff") if isinstance(maker.get("handoff"), dict) else {}
            transport = maker.get("handoff_evidence")
            if (maker.get("status") != "CANDIDATE" or handoff.get("status") != "completed"
                    or (transport is not None and (not isinstance(transport, dict)
                                                   or transport.get("status") != "available"
                                                   or transport.get("missing_fields")
                                                   or transport.get("truncated_fields")))):
                state.update(status="BLOCKED", reason="SCOUT_PARTIAL_NOT_CONSUMED")
                break
            known = {t["id"] for t in plan["tasks"] if isinstance(t, dict)}
            _records, grounding = history_scout.ground_scout_handoff(maker, material, known_nodes=known)
            state.update(status="ACCEPTED", acceptance="deterministic-grounding", grounding=grounding,
                         artifact=before, workspace=str(workspace))
            break
        state["review_pending"] = {"maker": maker, "artifact": before,
                                   'tests': tests, 'checks_digest': digest(task.get('checks', [])),
                                   'inputs_digest': snapshot(workspace, task.get('inputs', []))}
        write(folder / "progress.json", state)
        with telemetry.stage(journal, task["id"], "reviewer", round=state["round"]):
            reviewer = invoke_adapter(
                adapter, plan, task, "reviewer", workspace, attempt / "reviewer",
                {"tests": tests}, maker, child_env,
            )
        telemetry.provider_result(journal, task["id"], "reviewer", reviewer, state["round"])
        state["history"].append({"role": "reviewer", "result": reviewer, "tests": tests, "artifact": before})
        pool_wait = _pool_wait_info(reviewer, task, plan)
        if pool_wait:
            state.update(status="RESUMABLE_POOL_WAIT", reason=reviewer.get("reason", "Pool availability"), pool_phase="reviewer", **pool_wait)
            break
        _note_swap_bypass(state, "reviewer", reviewer)
        after = snapshot(workspace, task["scope"])
        if before != after:
            state.update(status="BLOCKED", reason="Artifact changed during read-only review; preserve and inspect")
            break
        if not maker.get("lineage") or not reviewer.get("lineage") or maker["lineage"] == reviewer["lineage"]:
            state.update(status="BLOCKED", reason="Independent actual model lineages not proven")
            break
        checked = reviewer.get("checked", [])
        if reviewer.get("status") != "REVIEWED" or not set(task["done_when"]).issubset(set(checked)):
            state.update(status="BLOCKED", reason="Review missing product evidence; maker artifacts retained, no automatic recoding")
            break
        if reviewer.get("verdict") == "ACCEPT" and not reviewer.get("findings") and all(t["exit_code"] == 0 for t in tests):
            state.update(status="ACCEPTED", artifact=after, workspace=str(workspace))
            state.pop("review_pending", None)
            break
        feedback = {"findings": reviewer.get("findings", []), "failed_tests": [t for t in tests if t["exit_code"] != 0]}
        if reviewer.get("verdict") not in {"ACCEPT", "REPAIR"} or not any(feedback.values()):
            state.update(status="BLOCKED", reason="Reviewer returned no actionable product findings")
            break
        state.pop("review_pending", None)
        state.pop("next_feedback", None)
        journal.emit("repair", node=task["id"], code="REVIEW_FINDINGS", round=state["round"])
        # Rephrased findings are not evidence that the product changed. Two
        # rejected rounds on unchanged artifacts/check outcomes need diagnosis,
        # including advisory runs that cannot repair the inspected source.
        signature = digest([after, [t["exit_code"] for t in tests]])
        stalled = stalled + 1 if signature == prior_signature else 0
        prior_signature = signature
        state["rejected_since_autopsy"] = state.get("rejected_since_autopsy", 0) + 1
        state.setdefault("progress_signatures", []).append(signature)
        write(folder / "progress.json", state)
        if stalled >= 1 and not autopsy_on:
            state.update(status="BLOCKED", reason="Unchanged artifact rejected twice; diagnose rather than repeat")
            break
        if autopsy_on and (stalled >= 1 or state["rejected_since_autopsy"] >= AUTOPSY_EVERY):
            action, feedback = _autopsy(
                plan, base_task, state, folder, adapter, journal, child_env,
                "stagnation" if stalled >= 1 else "rejections", feedback["findings"],
                tests, maker, reviewer, workspace)
            if action is None or action == "OWNER_QUESTION":
                break
            prior_signature, stalled = None, 0
            write(folder / "progress.json", state)
    if state.get("status") == "ACCEPTED":
        state.pop("next_feedback", None)
    state['transport_status'] = maker.get('transport_status') or maker.get('cause') or 'reported'
    state['artifact_status'] = 'tested' if state.get('status') == 'ACCEPTED' else 'preserved'
    state['acceptance_status'] = state.get('status')
    state['knowledge_closeout'] = 'PARENT_PENDING'
    write(folder / "progress.json", state)
    write(folder / "result.json", state)
    return state


def _descendants(tasks, roots):
    children = {}
    for task_id, task in tasks.items():
        for dep in task.get("depends_on", []):
            children.setdefault(dep, set()).add(task_id)
    seen, stack = set(), list(roots)
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(children.get(cur, ()))
    return seen


def _terminate_confirmed(task_folder):
    terminated = []
    for entry in _node_process_evidence(task_folder).get("all_processes", []):
        pid = entry.get("pid")
        if not entry.get("alive") or entry.get("pid_reused") is not False or not _valid_pid(pid):
            continue
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
            else:
                os.kill(int(pid), 15)
            terminated.append(int(pid))
        except (OSError, ValueError):
            continue
    return terminated


def cancel_tasks(folder, plan, task_ids=None, reason="cancelled"):
    """Cancel tasks and all descendants; terminate only PID+start_time confirmed processes."""
    validate(plan)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    tasks = {t["id"]: t for t in plan["tasks"]}
    roots = list(task_ids) if task_ids is not None else list(tasks)
    unknown = [x for x in roots if x not in tasks]
    if unknown:
        raise ValueError("Cancel names unknown task: " + ", ".join(sorted(unknown)))
    affected = _descendants(tasks, roots)
    path = folder / "state.json"
    state = read(path) if path.exists() else {"plan_hash": digest(plan), "nodes": {}, "version": VERSION}
    if state.get("plan_hash") not in (None, digest(plan)):
        raise ValueError("Plan changed: preserve old run; revise TODO explicitly, never silently reuse results")
    state.setdefault("nodes", {})
    terminated = {}
    for key in sorted(affected):
        node = dict(state["nodes"].get(key) or {})
        node.update(status="CANCELLED", reason=reason, cancelled_at=time.time())
        state["nodes"][key] = node
        pids = _terminate_confirmed(folder / key)
        if pids:
            terminated[key] = pids
        progress_path = folder / key / "progress.json"
        if progress_path.exists():
            try:
                progress = read(progress_path)
                if isinstance(progress, dict):
                    progress.update(status="CANCELLED", reason=reason, cancelled_at=node["cancelled_at"])
                    write(progress_path, progress)
                    write(folder / key / "result.json", progress)
            except (ValueError, OSError):
                pass
    state.setdefault("cancellations", []).append(
        {"tasks": sorted(affected), "reason": reason, "at": time.time(), "terminated": terminated})
    write(path, state)
    write(folder / "todo.json", plan)
    return state


def record_durable_event(folder, event_id, payload_hash):
    """Idempotent durable event registry stored in the run folder (no second state registry)."""
    if not isinstance(event_id, str) or not event_id.strip():
        raise ValueError("event_id must be a nonempty string")
    if not isinstance(payload_hash, str) or not payload_hash.strip():
        raise ValueError("payload_hash must be a nonempty string")
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "durable-events.json"
    registry = read(path) if path.exists() else {"version": VERSION, "events": {}}
    events = registry.setdefault("events", {})
    existing = events.get(event_id)
    if existing is not None:
        if existing.get("payload_hash") != payload_hash:
            raise ValueError("Durable event conflict for event_id: " + event_id)
        return {"event_id": event_id, "payload_hash": payload_hash, "duplicate": True}
    events[event_id] = {"payload_hash": payload_hash, "recorded_at": time.time()}
    write(path, registry)
    return {"event_id": event_id, "payload_hash": payload_hash, "duplicate": False}


# ---------------------------------------------------------------------------
# Inspected interruption recovery
# ---------------------------------------------------------------------------

_INSPECTION_SCHEMA = 1


def _node_process_evidence(task_folder):
    """Collect PID / start-time evidence across maker, reviewer, check processes, and native checkpoints."""
    evidence = {
        "pid": None,
        "started": None,
        "alive": None,
        "pid_reused": None,
        "source": None,
        "all_processes": [],
    }
    if not task_folder.is_dir():
        return evidence
    proc_files = sorted(
        set(task_folder.rglob("process.json"))
        | set(task_folder.rglob("*.process-checkpoint.json"))
        | set(task_folder.rglob("*process-checkpoint.json"))
    )
    if not proc_files:
        return evidence

    alive_found = False
    unknown_alive_found = False
    reused_found = False
    unknown_reused_found = False
    primary = None
    samples = {}  # One bounded native observation per PID, fresh for every scan.

    for proc_file in proc_files:
        try:
            proc = read(proc_file)
            if not isinstance(proc, dict):
                unknown_alive_found = True
                continue
        except (OSError, ValueError):
            unknown_alive_found = True
            continue

        pid = proc.get("pid")
        started = proc.get("started_at") if proc.get("started_at") is not None else proc.get("started")

        if not _valid_pid(pid):
            unknown_alive_found = True
            entry = {"pid": pid, "started": started, "alive": None, "pid_reused": None, "source": str(proc_file)}
            evidence["all_processes"].append(entry)
            continue

        if pid not in samples:
            probe = _windows_process_probe(pid) if os.name == 'nt' else None
            pid_alive = _process_alive(pid, probe=probe)
            current_start = _process_start_time(pid, probe=probe) if pid_alive is True else None
            samples[pid] = (pid_alive, current_start, probe)
        pid_alive, current_start, probe = samples[pid]
        reused = _pid_reused(pid, started, alive=pid_alive, current_start=current_start)
        entry = {"pid": pid, "started": started, "alive": pid_alive,
                 "pid_alive": pid_alive, "pid_reused": reused, "source": str(proc_file),
                 "created_at": current_start,
                 "creation_filetime": probe.get('creation_filetime') if probe else None,
                 "probe_method": probe.get('method') if probe else None}
        newer = reused is True and _newer_instance_proven(entry)
        alive = False if newer else pid_alive
        entry.update(alive=alive, newer_instance_proven=newer)
        evidence["all_processes"].append(entry)

        # Reuse is a fact even when another entry is live/unknown; do not lose it
        # in the liveness priority chain. The gate validates every reused entry.
        if reused is True:
            reused_found = True
        elif reused is None:
            unknown_reused_found = True
        if alive is True:
            alive_found = True
            primary = entry
        elif alive is None:
            unknown_alive_found = True
            if primary is None:
                primary = entry
        elif reused is True:
            reused_found = True
            if primary is None:
                primary = entry
        elif reused is None or started is None:
            unknown_reused_found = True
            if primary is None:
                primary = entry
        elif primary is None:
            primary = entry

    if primary is None and evidence["all_processes"]:
        primary = evidence["all_processes"][-1]

    if primary:
        evidence.update(pid=primary["pid"], started=primary["started"], source=primary["source"])

    if alive_found:
        evidence["alive"] = True
    elif unknown_alive_found:
        evidence["alive"] = None
    else:
        evidence["alive"] = False

    if reused_found:
        evidence["pid_reused"] = True
    elif unknown_reused_found:
        evidence["pid_reused"] = None
    else:
        evidence["pid_reused"] = False

    return evidence


def _lock_evidence(folder, plan):
    """Inspect lock files without acquiring or deleting them."""
    workspace_key = hashlib.sha256(str(Path(plan["workspace"]).resolve()).casefold().encode()).hexdigest()
    workspace_lock = Path.home() / ".agents/todo-locks" / (workspace_key + ".lock")
    result = {}
    for name, path in (("runner_lock", folder / "runner.lock"),
                       ("workspace_lock", workspace_lock)):
        entry = {"exists": path.is_file(), "pid": None, "alive": None}
        if entry["exists"]:
            try:
                raw = path.read_text(encoding="utf-8").strip()
                entry["pid"] = int(raw.split(":")[0])
            except (OSError, ValueError):
                entry["pid"] = None
            entry["alive"] = _process_alive(entry["pid"])
        result[name] = entry
    return result


def _maker_receipt(progress):
    """Extract the last maker result from progress history, if any."""
    for item in reversed(progress.get("history", [])):
        if item.get("role") == "maker" and isinstance(item.get("result"), dict):
            return item["result"]
    pending = progress.get("review_pending")
    if isinstance(pending, dict) and isinstance(pending.get("maker"), dict):
        return pending["maker"]
    return None


def _authoritative_maker_result(task_folder, attempt_num=None):
    """Read authoritative result.json and mandatory local Fleet usage record from disk."""
    if not task_folder.is_dir():
        return None, None
    if attempt_num is not None:
        candidates = [task_folder / str(attempt_num)]
    else:
        candidates = sorted((p for p in task_folder.iterdir() if p.is_dir() and p.name.isdigit()),
                            key=lambda p: int(p.name), reverse=True)
    for attempt in candidates:
        res_file = attempt / "maker" / "result.json"
        if res_file.is_file():
            try:
                res = read(res_file)
                if not isinstance(res, dict):
                    continue
                usage = None
                usage_path = res.get("usage_record")
                if usage_path:
                    p = Path(usage_path)
                    if not p.is_absolute():
                        p = (attempt / "maker" / p).resolve()
                    if p.is_file():
                        try:
                            usage = read(p)
                            if not isinstance(usage, dict):
                                usage = None
                        except (OSError, ValueError):
                            usage = None
                # Local Fleet usage is mandatory via result usage_record only.
                # A loose usage.json is never an authoritative fallback.
                return res, usage
            except (OSError, ValueError):
                continue
    return None, None


def _is_runtime_verified(runtime):
    return artifact_recovery.valid_runtime_attestation(runtime)


def _receipt_identity(receipt, usage=None):
    """Normalize model/runtime/checkout-base identity from maker result and mandatory usage record."""
    if not isinstance(receipt, dict):
        receipt = {}
    if not isinstance(usage, dict):
        usage = {}

    runtime = usage.get("runtime_attestation")
    if runtime is None:
        runtime = receipt.get("runtime_attestation")
    if runtime is None:
        runtime = receipt.get("runtime")

    base = usage.get("worktree_base_commit_sha")
    if base is None:
        base = usage.get("base_commit") or usage.get("base")
    if base is None:
        base = receipt.get("worktree_base_commit_sha") or receipt.get("base_commit") or receipt.get("base")

    model = usage.get("model") or receipt.get("model")
    lineage = (
        usage.get("model_provider_id")
        or usage.get("lineage")
        or receipt.get("lineage")
        or receipt.get("model_provider_id")
    )
    run_id = usage.get("run_id") or receipt.get("run_id")
    agent = usage.get("agent") or receipt.get("agent")

    return {
        "model": model,
        "runtime": runtime,
        "base": base,
        "lineage": lineage,
        "run_id": run_id,
        "agent": agent,
    }


def _workspace_base(workspace):
    """Return the current checkout base commit when the workspace is a Git repo."""
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(workspace), capture_output=True, text=True, timeout=5, **options,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _inspection_hash(record):
    body = {k: v for k, v in record.items() if k != "inspection_hash"}
    return digest(body)


def inspect_run(folder, plan):
    """Return a hash-bound inspection record for interrupted RUNNING nodes.

    Yields exact task/run/model/runtime/checkout-base/plan/scope/artifact hashes
    plus process and lock evidence. Read-only: never mutates run state.
    """
    validate(plan)
    folder = telemetry.require_absolute_run_dir(folder).resolve()
    workspace = Path(plan["workspace"]).resolve()
    if folder.is_relative_to(workspace):
        raise ValueError("Keep evidence outside the execution workspace")
    state_path = folder / "state.json"
    if not state_path.is_file():
        raise ValueError("No state.json in run directory")
    state = read(state_path)
    workspace_base = _workspace_base(workspace)
    nodes = {}
    for key, node in state.get("nodes", {}).items():
        if node.get("status") != "RUNNING":
            continue
        task = next((t for t in plan["tasks"] if t["id"] == key), None)
        if task is None:
            continue
        task_folder = folder / key
        progress = read(task_folder / "progress.json") if (task_folder / "progress.json").is_file() else {}
        scope = task.get("scope", [])
        scope_hash = snapshot(plan["workspace"], scope) if scope else digest({})
        try:
            artifact_hashes = artifact_recovery.manifest(workspace, scope)
        except (ValueError, OSError):
            artifact_hashes = {}
        auth_res, auth_usage = _authoritative_maker_result(
            task_folder, progress.get("round", node.get("round"))
        )
        receipt = auth_res if auth_res is not None else _maker_receipt(progress)
        identity = _receipt_identity(receipt, auth_usage)
        # Inspection binds the actual checkout; the claimed usage base is
        # separately checked against it, never used as its own proof.
        base = workspace_base
        nodes[key] = {
            "status": "RUNNING",
            "task_id": key,
            "round": progress.get("round", node.get("round", 0)),
            "task_hash": digest(task),
            "checks_hash": digest(task.get("checks", [])),
            "scope": list(scope),
            "scope_hash": scope_hash,
            "artifact_hashes": artifact_hashes,
            "model": identity["model"],
            "model_hash": digest(identity["model"]) if identity["model"] is not None else None,
            "runtime": identity["runtime"],
            "runtime_hash": digest(identity["runtime"]) if identity["runtime"] is not None else None,
            "checkout_base": base,
            "base_hash": digest(base) if base is not None else None,
            "lineage": identity["lineage"],
            "run_id": identity["run_id"],
            "maker_receipt": receipt,
            "process_evidence": _node_process_evidence(task_folder),
            "lock_evidence": _lock_evidence(folder, plan),
        }
    record = {
        "schema_version": _INSPECTION_SCHEMA,
        "run_id": folder.name,
        "inspected_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "plan_hash": digest(plan),
        "workspace": str(workspace),
        "workspace_base": workspace_base,
        "nodes": nodes,
    }
    record["inspection_hash"] = _inspection_hash(record)
    return record


def _trustworthy_maker(receipt, node_evidence, workspace, scope, task=None, task_folder=None, run_id=None):
    """Fail-closed check for a usable retained maker candidate.

    Reuses artifact_recovery.manifest for file hashes and refuses any
    task/run/model/runtime/checkout-base drift since inspection.
    Enforces mandatory authoritative local Fleet usage JSON.
    """
    if not isinstance(receipt, dict):
        return False, "missing maker receipt"
    status = receipt.get("status")
    if status not in {"CANDIDATE", "PARTIAL"}:
        return False, f"maker status {status!r} is not a candidate"
    if status == "PARTIAL":
        proof = receipt.get("artifact_recovery")
        if not isinstance(proof, dict) or proof.get("provenance") != "local-verified-invocation-not-cryptographic-authentication":
            return False, "maker status 'PARTIAL' requires verified retained_candidate proof"

    if task_folder is None:
        return False, "missing task folder for authoritative verification"

    auth_res, auth_usage = _authoritative_maker_result(task_folder, node_evidence.get("round"))
    if not auth_res:
        return False, "missing authoritative maker result.json on disk (forged progress-only receipt)"
    if not auth_usage:
        return False, "missing mandatory authoritative Fleet usage record on disk"

    # Authoritative on-disk status, not the progress-only claim.
    auth_status = auth_res.get("status")
    if auth_status not in {"CANDIDATE", "PARTIAL"}:
        return False, f"maker status {auth_status!r} is not a candidate"
    if auth_status == "PARTIAL":
        auth_proof = auth_res.get("artifact_recovery")
        if not isinstance(auth_proof, dict) or auth_proof.get("provenance") != "local-verified-invocation-not-cryptographic-authentication":
            return False, "maker status 'PARTIAL' requires verified retained_candidate proof"

    identity = _receipt_identity(auth_res, auth_usage)
    for field in ("model", "runtime", "base", "lineage", "run_id"):
        if not identity.get(field):
            return False, f"maker receipt missing required field: {field}"

    if not _is_runtime_verified(identity["runtime"]):
        return False, f"runtime {identity['runtime']!r} is not VERIFIED"

    fleet_run_id = identity["run_id"]
    if auth_res.get("run_id") and auth_res["run_id"] != fleet_run_id:
        return False, f"maker result run_id {auth_res['run_id']} does not match usage run_id {fleet_run_id}"
    if node_evidence.get("run_id") and node_evidence["run_id"] != fleet_run_id:
        return False, f"maker receipt run_id {fleet_run_id} does not match inspected run_id {node_evidence['run_id']}"

    ok, why = artifact_recovery.verify_usage_record(
        auth_usage, auth_res, workspace=workspace,
        expected_model=identity["model"], lineage=identity["lineage"],
        base=identity["base"], run_id=fleet_run_id,
    )
    if not ok:
        return False, "usage record verification failed: " + why

    if auth_usage.get("exit_code") != 0:
        return False, f"usage record exit code {auth_usage.get('exit_code')} is non-zero"
    terminal = str(auth_usage.get("provider_terminal_reason") or auth_res.get("terminal_reason") or "").casefold()
    if not terminal or terminal not in {'success', 'succeeded', 'completed', 'complete', 'stop', 'end_turn', 'turn_complete', 'done'}:
        return False, f"provider terminal reason {terminal!r} is not success"

    exec_ws = auth_usage.get("execution_workspace")
    if not exec_ws:
        return False, "missing execution workspace in usage record"
    try:
        if Path(exec_ws).resolve() != Path(workspace).resolve():
            return False, "execution workspace mismatch in usage record"
    except (ValueError, OSError):
        return False, "invalid execution workspace in usage record"

    if task is not None:
        maker_candidates = task.get("maker", [])
        allowed_models = {c.get("model") for c in maker_candidates if isinstance(c, dict)}
        allowed_agents = {c.get("agent") for c in maker_candidates if isinstance(c, dict)}
        if allowed_models and identity["model"] not in allowed_models:
            return False, f"maker model {identity['model']!r} not in task maker candidates"
        if identity.get("agent") and allowed_agents and identity["agent"] not in allowed_agents:
            return False, f"maker agent {identity['agent']!r} not in task maker candidates"

    if task is not None and node_evidence.get("task_hash") not in (None, digest(task)):
        return False, "task definition changed since inspection"
    if task is not None and node_evidence.get("checks_hash") not in (None, digest(task.get("checks", []))):
        return False, "checks changed since inspection"

    if node_evidence.get("model") and identity["model"] != node_evidence["model"]:
        return False, "model changed since inspection"
    if node_evidence.get("model_hash") and digest(identity["model"]) != node_evidence["model_hash"]:
        return False, "model hash changed since inspection"

    if node_evidence.get("lineage") and identity["lineage"] != node_evidence["lineage"]:
        return False, "lineage changed since inspection"

    if node_evidence.get("runtime_hash") and digest(identity["runtime"]) != node_evidence["runtime_hash"]:
        return False, "runtime hash changed since inspection"

    current_base = _workspace_base(Path(workspace))
    inspected_base = node_evidence.get("checkout_base")
    if inspected_base is None or current_base is None:
        return False, "actual checkout base unavailable"
    if current_base != inspected_base:
        return False, "checkout base changed since inspection"
    if identity["base"] != inspected_base:
        return False, "recorded base changed since inspection"
    if node_evidence.get("base_hash") and digest(inspected_base) != node_evidence["base_hash"]:
        return False, "base hash changed since inspection"

    current = snapshot(workspace, scope) if scope else digest({})
    if current != node_evidence.get("scope_hash"):
        return False, "scope hash changed since inspection"
    try:
        current_artifacts = artifact_recovery.manifest(Path(workspace).resolve(), scope)
    except (ValueError, OSError):
        return False, "cannot hash current artifacts"
    if current_artifacts != node_evidence.get("artifact_hashes"):
        return False, "artifact hashes changed since inspection"

    return True, ""


def _process_evidence_ok(proc_evidence):
    """Refuse live/unknown original instances; retain proven newer PID evidence."""
    if proc_evidence.get("alive") is None:
        return False, "process state unknown"
    if proc_evidence.get("alive"):
        return False, "process still alive"
    if proc_evidence.get("pid_reused") is None and proc_evidence.get("pid") is not None:
        return False, "PID reuse state unknown"
    entries = proc_evidence.get('all_processes', [])
    for entry in entries:
        if entry.get('alive') is not False or entry.get('pid_reused') is None:
            return False, "recorded process state unknown or alive"
        if entry.get('pid_reused') and not _newer_instance_proven(entry):
            return False, "PID was reused without native newer-instance proof"
    if proc_evidence.get("pid_reused") and not entries:
        return False, "PID was reused without per-process evidence"
    return True, ""


def _lock_evidence_ok(lock_evidence):
    for name, entry in lock_evidence.items():
        if entry.get("alive"):
            return False, f"{name} held by live process"
        if entry.get("alive") is None and entry.get("exists"):
            return False, f"{name} state unknown"
    return True, ""


def resolve_interrupted(folder, plan, inspection, adapter=None, journal=None, child_env=None):
    """Explicit inspected recovery for RUNNING nodes. Fail closed on any doubt.

    Requires a hash-bound inspection record and proof that no native process or
    controller is still running before any state change. Fail-closed outcomes
    preserve the original RUNNING state, history, transport evidence and every
    artifact; they never fabricate CANDIDATE or ACCEPT and never blind-replay.
    """
    validate(plan)
    folder = telemetry.require_absolute_run_dir(folder).resolve()
    workspace = Path(plan["workspace"]).resolve()
    if folder.is_relative_to(workspace):
        raise ValueError("Keep evidence outside the execution workspace")
    state_path = folder / "state.json"
    if not state_path.is_file():
        raise ValueError("No state.json in run directory")
    if inspection.get("schema_version") != _INSPECTION_SCHEMA:
        return {"status": "BLOCKED", "reason": "Unsupported inspection schema"}
    expected_hash = inspection.get("inspection_hash")
    if not expected_hash or _inspection_hash(inspection) != expected_hash:
        return {"status": "BLOCKED", "reason": "Inspection record hash mismatch"}
    if inspection.get("run_id") not in (None, folder.name):
        return {"status": "BLOCKED", "reason": "Run id changed since inspection"}
    if inspection.get("plan_hash") != digest(plan):
        return {"status": "BLOCKED", "reason": "Plan changed since inspection"}

    workspace_key = hashlib.sha256(str(workspace).casefold().encode()).hexdigest()
    workspace_lock = Path.home() / ".agents/todo-locks" / (workspace_key + ".lock")
    runner_lock = folder / "runner.lock"

    # Pre-check lock evidence before attempting acquisition
    lock_ev = _lock_evidence(folder, plan)
    ok, why = _lock_evidence_ok(lock_ev)
    if not ok:
        return {"status": "BLOCKED", "reason": "Lock evidence refuses recovery: " + why}

    try:
        with exclusive(workspace_lock), exclusive(runner_lock):
            return _resolve_interrupted_locked(folder, plan, inspection, adapter, journal, child_env)
    except (FileExistsError, OSError) as exc:
        return {"status": "BLOCKED", "reason": f"Cannot acquire lock; scheduler or recovery in progress ({type(exc).__name__})"}


def _resolve_interrupted_locked(folder, plan, inspection, adapter=None, journal=None, child_env=None):
    state_path = folder / "state.json"
    state = read(state_path)
    workspace = plan["workspace"]
    adapter = adapter or FleetAdapter()
    results = {}
    state_changed = False

    for key, node_evidence in inspection.get("nodes", {}).items():
        current = state.get("nodes", {}).get(key)
        if not isinstance(current, dict) or current.get("status") != "RUNNING":
            results[key] = {
                "status": "BLOCKED",
                "reason": "Node is no longer RUNNING; state changed since inspection",
                "original_status": current.get("status") if isinstance(current, dict) else None,
            }
            continue

        # Prove no current native process/controller before changing state.
        task_folder = folder / key
        ok, why = _process_evidence_ok(_node_process_evidence(task_folder))
        if not ok:
            results[key] = {
                "status": "BLOCKED",
                "reason": "Process evidence refuses recovery: " + why,
                "original_status": "RUNNING",
                "state_preserved": True,
            }
            continue

        task = next((t for t in plan["tasks"] if t["id"] == key), None)
        if task is None:
            results[key] = {
                "status": "BLOCKED",
                "reason": "Task not found in plan",
                "original_status": "RUNNING",
                "state_preserved": True,
            }
            continue

        progress = read(task_folder / "progress.json") if (task_folder / "progress.json").is_file() else {"status": "RUNNING", "round": 0, "history": []}
        receipt = _maker_receipt(progress)
        ok, why = _trustworthy_maker(
            receipt, node_evidence, workspace, node_evidence.get("scope", []),
            task=task, task_folder=task_folder, run_id=folder.name,
        )
        if not ok:
            results[key] = {
                "status": "BLOCKED",
                "reason": "No trustworthy maker receipt: " + why,
                "original_status": "RUNNING",
                "state_preserved": True,
            }
            continue

        progress.setdefault("history", []).append({
            "role": "recovery", "action": "resolve-interrupted",
            "original_status": "RUNNING", "at": time.time(),
        })
        auth_res, _ = _authoritative_maker_result(task_folder, node_evidence.get("round"))
        progress["review_pending"] = {
            "maker": auth_res if auth_res is not None else receipt,
            "artifact": node_evidence["scope_hash"],
            "tests": None,
            "checks_digest": digest(task.get("checks", [])),
            "inputs_digest": snapshot(workspace, task.get("inputs", [])),
        }
        write(task_folder / "progress.json", progress)
        try:
            result = run_task(plan, task, task_folder, adapter, journal, child_env)
        except Exception as exc:
            result = {"status": "BLOCKED", "reason": "Recovery run failed: " + type(exc).__name__,
                      "original_status": "RUNNING"}
        results[key] = result
        state["nodes"][key] = result
        state_changed = True

    if state_changed:
        write(state_path, state)
    recovered = [k for k, v in results.items() if v.get("status") == "ACCEPTED"]
    blocked = [k for k, v in results.items() if v.get("status") != "ACCEPTED"]
    return {
        "status": "ACCEPTED_CANDIDATE" if recovered and not blocked else "BLOCKED",
        "nodes": results,
        "recovered": recovered,
        "blocked": blocked,
    }



def _emit_terminal(plan, folder, terminal_state, started_at):
    run_id = folder.name
    identifier = child_terminal.event_id(run_id, terminal_state)
    receipt_path = folder / "watch-event.json"
    if receipt_path.is_file():
        try:
            previous = read(receipt_path)
            if previous.get("attempted") is True and previous.get("event_id") == identifier:
                return previous
        except (OSError, ValueError, TypeError, UnicodeError):
            pass
    try:
        receipt = child_terminal.emit(
            run_id,
            "agent-graph",
            terminal_state,
            folder / "state.json",
            plan.get("workspace"),
            started_at,
        )
    except Exception as exc:
        receipt = {
            "schema": child_terminal.RECEIPT_SCHEMA,
            "event_id": identifier,
            "status": "error",
            "attempted": True,
            "channel_sent": False,
            "parent_requested": False,
            "parent_sent": False,
            "errors": ["emitter_" + type(exc).__name__],
        }
    write(receipt_path, receipt)
    return receipt


def _emit_completion_metadata(plan, result, folder, runner_kind):
    """Best-effort metadata hook from the normal installed skill location."""
    try:
        import importlib.util
        path = (Path.home() / ".agents" / "skills" / "delivery-audit" /
                "scripts" / "completion.py")
        if path.is_symlink() or not path.is_file():
            return None
        name = "_agent_graph_audit_completion"
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module.emit_completion(plan, result, folder, runner_kind)
    except Exception:
        return None


def run(plan, folder, adapter=None, approved=(), retry=(), *, emit_terminal=False,
        emit_completion=False):
    """Execute a graph; only production entry points opt in to Office emission."""
    validate(plan)
    if adapter is None or isinstance(adapter, _FleetAdapterClass):
        (adapter or FleetAdapter()).validate_plan(plan)
    folder = telemetry.require_absolute_run_dir(folder).resolve()
    journal = telemetry.Journal(telemetry.root_for(folder))
    began = time.monotonic()
    started_at = child_terminal.utc_now() if emit_terminal else None
    terminal_state = "BLOCKED"
    journal.emit("run_start", scheduler_version=VERSION)
    try:
        result = _run(plan, folder, adapter, approved, retry, journal)
        terminal_state = result.get("status", "BLOCKED")
        journal.emit("run_terminal", status=result.get("status"), seconds=time.monotonic() - began,
                     tasks=len(plan["tasks"]), accepted=sum(n.get("status") == "ACCEPTED" for n in result["nodes"].values()))
        if emit_completion and result.get("status") == "ACCEPTED_CANDIDATE":
            _emit_completion_metadata(plan, result, folder, "graph")
        return result
    except Exception as exc:
        journal.emit("run_terminal", status="BLOCKED", code=type(exc).__name__, seconds=time.monotonic() - began)
        raise
    finally:
        if emit_terminal:
            try:
                _emit_terminal(plan, folder, terminal_state, started_at)
            except Exception:
                pass
        journal.close()


def _load_scout_history(folder, plan, state):
    """Bounded historical packets from the accepted scout only.

    Blocked/non-accepted scout output is never consumed. Resume reuses the
    accepted scout result without redoing discovery. Unrelated nodes get no
    irrelevant records. Returns ``None`` while the scout is still pending.
    """
    scouts = [t for t in plan["tasks"] if t.get("role") == "scout"]
    if not scouts:
        return {}
    scout_id = scouts[0]["id"]
    node = state.get("nodes", {}).get(scout_id) or {}
    status = node.get("status")
    if status != "ACCEPTED":
        return None if status == "RUNNING" else {}
    result_path = folder / scout_id / "result.json"
    if not result_path.is_file():
        return {}
    try:
        result = read(result_path)
    except (OSError, ValueError):
        return {}
    contract = plan.get("historical_scout") or {}
    known = {t["id"] for t in plan["tasks"] if isinstance(t, dict)}
    records, _meta = history_scout.records_from_accepted_node(result, known_nodes=known)
    consumers = contract.get("consumers")
    if not isinstance(consumers, list):
        consumers = [t["id"] for t in plan["tasks"]
                     if isinstance(t, dict) and t.get("id") != scout_id
                     and scout_id in (t.get("depends_on") or [])]
    budget = contract.get("recipient_max_bytes", history_scout.RECIPIENT_MAX_BYTES)
    material_path = folder / scout_id / 'context' / 'historical-input.json'
    material = None
    if material_path.is_file():
        material = read(material_path)
        records = history_scout.grounded_records(records, material)
    packets = history_scout.route_records(records, node_ids=consumers, max_bytes=budget)
    if material is not None:
        for packet in packets.values():
            if not packet.get('records'):
                packet['status'] = material.get('status') if material.get('status') in {'EMPTY', 'UNAVAILABLE'} else 'EMPTY_SELECTION'
                packet['continuation'] = material.get('continuation')
                for key in ('reason', 'withheld'):
                    if material.get(key):
                        packet[key] = material[key]
        return packets
    # Drop empty packets for nodes with no relevant evidence (never claim absence).
    return {k: v for k, v in packets.items() if v.get("records") or v.get("omitted_count")}


def _run(plan, folder, adapter, approved, retry, journal):
    validate(plan)
    folder = telemetry.require_absolute_run_dir(folder).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    child_env = {"AGENT_GRAPH_RUN_ID": folder.name}
    live = adapter is None
    adapter = adapter or FleetAdapter()
    if live and folder.is_relative_to(Path(plan["workspace"]).resolve()):
        raise ValueError("Keep evidence outside the execution workspace")
    tasks = {t["id"]: t for t in plan["tasks"]}
    path = folder / "state.json"
    workspace_key = hashlib.sha256(str(Path(plan["workspace"]).resolve()).casefold().encode()).hexdigest()
    workspace_lock = Path.home() / ".agents/todo-locks" / (workspace_key + ".lock")
    with exclusive(workspace_lock), exclusive(folder / "runner.lock"):
        state = read(path) if path.exists() else {"plan_hash": digest(plan), "nodes": {}, "version": VERSION}
        if not path.exists():
            # New-run policy at the public Graph execution entry, before any model.
            # Old resumed graphs and explicit legacy-test plans stay usable.
            native_execution = (live or isinstance(adapter, _FleetAdapterClass)
                                or bool(getattr(adapter, 'native_historical_scout', False)))
            history_scout.enforce_new_run_scout_policy(plan, allow_legacy_test=not native_execution)
        if state["plan_hash"] != digest(plan):
            raise ValueError("Plan changed: preserve old run; revise TODO explicitly, never silently reuse results")
        for key in retry:
            if key not in tasks or state["nodes"].get(key, {}).get("status") not in {"BLOCKED", "WAITING_APPROVAL", "DEPENDENCY_BLOCKED", "RESUMABLE_POOL_WAIT"}:
                raise ValueError("Retry names a nonblocked task: " + key)
            state.setdefault("retries", []).append({"task": key, "previous": state["nodes"].pop(key), "at": time.time()})
        if retry:
            for key in list(state["nodes"]):
                if state["nodes"][key]["status"] == "DEPENDENCY_BLOCKED":
                    state["nodes"].pop(key)
        for key, node in state["nodes"].items():
            if node["status"] == "RUNNING":
                raise ValueError("Interrupted attempt needs inspection, not blind replay: " + key)
            if node["status"] == "ACCEPTED" and node["artifact"] != snapshot(plan["workspace"], tasks[key]["scope"]):
                raise ValueError("Previously reviewed artifact changed: " + key)
            if node['status'] == 'ACCEPTED' and node.get('uat_evidence', {}).get('final'):
                evidence = node['uat_evidence']
                if evidence['artifact_digest'] != snapshot(plan['workspace'], evidence['artifact_scope']):
                    raise ValueError('Previously tested UAT artifact changed: ' + key)
        write(folder / "todo.json", plan)
        for key in [k for k, n in state["nodes"].items() if n.get("status") in {"RESUMABLE_POOL_WAIT", "DEPENDENCY_BLOCKED"}]:
            state.setdefault("resumes", []).append({"task": key, "previous": state["nodes"].pop(key), "at": time.time()})
        waits = {}
        effective_parallel = _effective_max_parallel(plan)
        starts = {}
        monitor_fingerprint = None
        def observe():
            nonlocal monitor_fingerprint
            observations = {key: {'elapsed_seconds': time.monotonic() - began,
                                  'timeout_seconds': tasks[key].get('timeout_seconds', 3600) * 2 + 120}
                            for key, began in starts.items()
                            if state['nodes'].get(key, {}).get('status') == 'RUNNING'}
            report = team_support.monitor_report(plan, state, now=datetime.now(timezone.utc), observations=observations)
            # Historical partial UAT is evidence of that exact revision only.
            report['uat_current'] = {key: snapshot(plan['workspace'], node['uat_evidence']['artifact_scope']) == node['uat_evidence']['artifact_digest']
                                     for key, node in state['nodes'].items() if node.get('uat_evidence')}
            report['release_authorized'] = False
            report['final_uat_current'] = [key for key, current in report['uat_current'].items()
                                           if current and state['nodes'][key]['uat_evidence']['final']
                                           and state['nodes'][key]['status'] == 'ACCEPTED']
            write(folder / 'monitor.json', report)
            if live:
                delivery = team_bridge.publish_monitor(plan, report, monitor_fingerprint)
                monitor_fingerprint = delivery['fingerprint']
                write(folder / 'monitor-delivery.json', delivery)
        def record_wait(key, reason):
            if waits.get(key) != reason:
                waits[key] = reason
                journal.emit("node_wait", node=key, wait_reason=reason)

        for key in tasks:
            journal.emit("node_queued", node=key)
        with ThreadPoolExecutor(max_workers=effective_parallel) as pool:
            active = {}
            while True:
                for key, task in tasks.items():
                    if key in state["nodes"]:
                        continue
                    deps = [state["nodes"].get(x, {}).get("status") for x in task.get("depends_on", [])]
                    if any(x == "RESUMABLE_POOL_WAIT" for x in deps):
                        state["nodes"][key] = {"status": "RESUMABLE_POOL_WAIT", "reason": "dependency pool wait", "pool_phase": "dependency"}
                        journal.emit("node_wait", node=key, wait_reason="pool_wait")
                        continue
                    if any(x == "CANCELLED" for x in deps):
                        state["nodes"][key] = {"status": "CANCELLED", "reason": "dependency cancelled"}
                        journal.emit("node_terminal", node=key, status="CANCELLED")
                        continue
                    if any(x in {"BLOCKED", "WAITING_APPROVAL", "DEPENDENCY_BLOCKED"} for x in deps):
                        state["nodes"][key] = {"status": "DEPENDENCY_BLOCKED"}
                        journal.emit("node_terminal", node=key, status="DEPENDENCY_BLOCKED")
                        continue
                    if any(x != "ACCEPTED" for x in deps):
                        record_wait(key, "dependency")
                        continue
                    missing = set(task.get("requires_approval", [])) - set(approved)
                    if missing:
                        state["nodes"][key] = {"status": "WAITING_APPROVAL", "requires": sorted(missing)}
                        journal.emit("node_terminal", node=key, status="WAITING_APPROVAL")
                        continue
                    if len(active) >= effective_parallel:
                        record_wait(key, "capacity")
                        continue
                    if any(overlaps(task, tasks[other]) for other in active.values()):
                        record_wait(key, "resource")
                        continue
                    state["nodes"][key] = {"status": "RUNNING"}
                    starts[key] = time.monotonic()
                    write(path, state)
                    journal.emit("node_dispatch", node=key, parallel=len(active) + 1)
                    history_packets = _load_scout_history(folder, plan, state)
                    dispatch_task = (history_scout.attach_records(task, history_packets)
                                     if history_packets else task)
                    active[pool.submit(
                        run_task, plan, dispatch_task, folder / key, adapter, journal, child_env,
                    )] = key
                if not active:
                    break
                observe()
                finished, _ = wait(active, timeout=30, return_when=FIRST_COMPLETED)
                for future in finished:
                    key = active.pop(future)
                    try:
                        state["nodes"][key] = future.result()
                    except Exception as exc:
                        state["nodes"][key] = {"status": "BLOCKED", "reason": str(exc)}
                    write(path, state)
                observe()
        for key, node in state['nodes'].items():
            evidence = node.get('uat_evidence', {})
            if node.get('status') == 'ACCEPTED' and evidence.get('final'):
                if snapshot(plan['workspace'], evidence['artifact_scope']) != evidence['artifact_digest']:
                    node.update(status='BLOCKED', reason='FINAL_UAT_ARTIFACT_CHANGED')
                    write(folder / key / 'result.json', node)
                    write(folder / key / 'progress.json', node)
        if all(x.get("status") == "ACCEPTED" for x in state["nodes"].values()) and len(state["nodes"]) == len(tasks):
            state["status"] = "ACCEPTED_CANDIDATE"
        elif any(x.get("status") == "RESUMABLE_POOL_WAIT" for x in state["nodes"].values()):
            state["status"] = "RESUMABLE_POOL_WAIT"
        else:
            state["status"] = "BLOCKED"
        state["production_deployed"] = False
        write(path, state)
        observe()
        return state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["validate", "run", "status", "graph", "inspect", "resolve-interrupted"])
    parser.add_argument("--todo")
    parser.add_argument("--run-dir", help="Absolute TODO evidence directory")
    parser.add_argument("--inspection", help="Path to a hash-bound inspection record for resolve-interrupted")
    parser.add_argument("--retry-task", action="append", default=[], help="Resume this blocked task; never rerun accepted nodes")
    parser.add_argument("--approved", action="append", default=[], help="Exact pre-existing owner approval reference; not an approval request")
    args = parser.parse_args(argv)
    if args.action == "status":
        result = read(telemetry.require_absolute_run_dir(args.run_dir) / "state.json")
        result = {"status": result.get("status"), "nodes": {k: {f: v[f] for f in ("status", "reason", "round") if f in v} for k, v in result["nodes"].items()}}
    elif args.action == "inspect":
        plan = validate(read(args.todo))
        result = inspect_run(args.run_dir, plan)
    elif args.action == "resolve-interrupted":
        plan = validate(read(args.todo))
        if not args.inspection:
            print(json.dumps({"status": "BLOCKED", "reason": "--inspection is required for resolve-interrupted"}))
            return 3
        inspection = read(args.inspection)
        result = resolve_interrupted(args.run_dir, plan, inspection)
    else:
        plan = read(args.todo) if args.action == "run" else validate(read(args.todo))
        if args.action == 'validate':
            FleetAdapter().validate_plan(plan)
        if args.action == "graph":
            print("flowchart TD")
            for t in plan["tasks"]:
                print(f'  {t["id"]}["{t["id"]}: maker + reviewer"]')
                for dep in t.get("depends_on", []):
                    print(f'  {dep} --> {t["id"]}')
            return 0
        result = run(
            plan,
            args.run_dir,
            approved=args.approved,
            retry=args.retry_task,
            emit_terminal=True,
            emit_completion=True,
        ) if args.action == "run" else {"valid": True, "tasks": len(plan["tasks"])}
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result.get("status") not in {"BLOCKED", "WAITING_APPROVAL", "RESUMABLE_POOL_WAIT"} else 3


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError) as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)}))
        raise SystemExit(3)
