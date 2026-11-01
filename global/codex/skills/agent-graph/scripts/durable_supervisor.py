"""Durable supervisor and ZCode (arbor-campaign/v1) adapter over todo_graph.

No second scheduler: campaign units are translated to one todo_graph plan and
all execution, interruption inspection and recovery go through todo_graph
(``run``, ``inspect_run``, ``resolve_interrupted``, ``cancel_tasks``). This
module only adds campaign validation, hard lockbox isolation, owner fencing in
``durable-state.json``, deterministic no-model exits for an empty / pool-wait
queue, atomic per-unit results and idempotent experience appends.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

import todo_graph  # noqa: E402
import history_scout  # noqa: E402

SCHEMA_VERSION = "arbor-campaign/v1"
CAMPAIGN_SCHEMA = SCHEMA_VERSION
UNIT_RESULT_SCHEMA = "arbor-unit-result/v1"
EXPERIENCE_SCHEMA = "arbor-experience/v1"
DURABLE_STATE_SCHEMA = "arbor-durable-state/v1"
TERMINAL = {"ACCEPTED", "CANCELLED", "BLOCKED"}
_ID = todo_graph.ID


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_json(path):
    return todo_graph.read(Path(path))


def _write_json(path, value):
    todo_graph.write(Path(path), value)


def _nonempty_str(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(label + " must be a nonempty string")
    return value


def _str_list(value, label, nonempty=False):
    if value is None:
        return []
    if not isinstance(value, list) or (nonempty and not value) or not all(
            isinstance(x, str) and x.strip() for x in value):
        raise ValueError(label + " must be a list of nonempty strings")
    if len(set(value)) != len(value):
        raise ValueError(label + " contains duplicates")
    return list(value)


def _unit_id(unit):
    if not isinstance(unit, dict):
        raise ValueError("units[] entries must be objects")
    uid = unit.get("id") or unit.get("unit_id")
    if not isinstance(uid, str) or not _ID.fullmatch(uid):
        raise ValueError("unit id/unit_id is invalid: " + repr(uid))
    return uid


def _contains_lockbox(value, lockbox_ref, workspace=None):
    """True when a maker-visible path references the lockbox (either direction)."""
    if not isinstance(value, str):
        return False
    lock = lockbox_ref.replace("\\", "/").strip().casefold()
    item = value.replace("\\", "/").strip().casefold()
    if not lock or not item:
        return False
    if item == lock or lock in item:
        return True
    # A whole-workspace scope ('.', './', root, or a bare wildcard) contains
    # every workspace path, including the lockbox wherever it lives.
    if item in (".", "./", "/", "**", "*", "./**", "./*", "**/*"):
        return True
    lock_parts = [p for p in PurePosixPath(lock).parts if p not in ("/", "")]
    item_parts = [p for p in PurePosixPath(item).parts if p not in ("/", "")]
    if not item_parts:
        return True
    if any(ch in item for ch in "*?["):
        # Wildcard scope: it touches the lockbox when the lockbox path matches
        # the pattern or lives under the pattern's literal prefix.
        if _glob_match(lock, item):
            return True
        literal_prefix = []
        for part in item_parts:
            if any(ch in part for ch in "*?["):
                break
            literal_prefix.append(part)
        if not literal_prefix or lock_parts[:len(literal_prefix)] == literal_prefix:
            return True
    def _subseq(needle, hay):
        n = len(needle)
        return n > 0 and any(hay[i:i + n] == needle for i in range(len(hay) - n + 1))
    if _subseq(lock_parts, item_parts) or _subseq(item_parts, lock_parts):
        return True
    if workspace and isinstance(workspace, str) and workspace.strip():
        try:
            ws_p = Path(workspace)
            if not ws_p.is_absolute():
                ws_p = Path.cwd() / ws_p
            ws_p = ws_p.resolve()
            lock_p = Path(lockbox_ref)
            if not lock_p.is_absolute():
                lock_p = ws_p / lock_p
            lock_p = lock_p.resolve()
            if lock_p.is_relative_to(ws_p):
                rel_lock = lock_p.relative_to(ws_p).as_posix()
                if rel_lock and rel_lock.casefold() != lock and _contains_lockbox(value, rel_lock, workspace=None):
                    return True
            if not any(ch in item for ch in "*?["):
                item_p = Path(value)
                if not item_p.is_absolute():
                    item_p = ws_p / item_p
                item_p = item_p.resolve()
                if item_p == lock_p or item_p.is_relative_to(lock_p) or lock_p.is_relative_to(item_p):
                    return True
        except (OSError, ValueError):
            pass
    return False


def _any_lockbox(value, lockbox_ref, workspace=None):
    if isinstance(value, dict):
        return any(_any_lockbox(item, lockbox_ref, workspace=workspace) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_any_lockbox(item, lockbox_ref, workspace=workspace) for item in value)
    return isinstance(value, str) and _contains_lockbox(value, lockbox_ref, workspace=workspace)


_UNIT_SCOPE_KEYS = ("scope", "inputs", "input", "files", "paths")


def ensure_lockbox_isolated(campaign):
    """Reject (ValueError) when lockbox_ref leaks into real unit scope/inputs.

    Budget limits (tree_budget.allowed_write_globs), goal/requirement text and
    other descriptive fields are not scope semantics: a broad glob such as
    "**" there must not be treated as if it named the lockbox path.
    """
    lockbox = campaign.get("lockbox_ref") if isinstance(campaign, dict) else None
    if not lockbox or not isinstance(campaign, dict):
        return
    workspace = campaign.get("workspace")
    for unit in campaign.get("units") or []:
        if not isinstance(unit, dict):
            continue
        for key in _UNIT_SCOPE_KEYS:
            value = unit.get(key)
            if value is not None and _any_lockbox(value, lockbox, workspace=workspace):
                raise ValueError("lockbox_ref is present in maker-visible unit scope/inputs; lockbox isolation refused")
    return True


def _glob_match(value, pattern):
    value = value.replace("\\", "/")
    pattern = pattern.replace("\\", "/")
    if fnmatch.fnmatchcase(value, pattern):
        return True
    if pattern.endswith("/**") and (value == pattern[:-3] or value.startswith(pattern[:-2])):
        return True
    if "/**/" in pattern:
        head, tail = pattern.split("/**/", 1)
        return value.startswith(head + "/") and fnmatch.fnmatchcase(value.rsplit("/", 1)[-1], tail)
    return False


def _candidates(unit, role, allowed_pools=None):
    raw = unit.get(role)
    if raw is None:
        raw = unit.get(role + "_candidates")
    if raw is None:
        agent = "fleet"
        if allowed_pools and "fleet" not in allowed_pools:
            agent = allowed_pools[0]
        return [{"agent": agent, "model": "fleet-default",
                 "reason": "arbor campaign default candidate", "score": 50}]
    if isinstance(raw, dict):
        raw = [raw]
    if isinstance(raw, str):
        raw = [{"agent": "fleet", "model": raw, "reason": "arbor campaign candidate", "score": 50}]
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"unit {role} must be a candidate, list of candidates, or model string")
    out = []
    for cand in raw:
        if isinstance(cand, str):
            cand = {"agent": "fleet", "model": cand}
        if not isinstance(cand, dict):
            raise ValueError(f"unit {role} candidates must be objects")
        merged = {"agent": "fleet", "reason": "arbor campaign candidate", "score": 50}
        merged.update(cand)
        out.append(merged)
    return out


def validate_campaign(campaign, profiles=None):
    """Validate an arbor-campaign/v1 document. Returns the campaign unchanged."""
    if not isinstance(campaign, dict):
        raise ValueError("campaign must be a JSON object")
    if campaign.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("schema_version must be 'arbor-campaign/v1'")
    cid = campaign.get("campaign_id")
    if not isinstance(cid, str) or not _ID.fullmatch(cid):
        raise ValueError("campaign_id is invalid")
    _nonempty_str(campaign.get("goal"), "goal")
    _nonempty_str(campaign.get("workspace"), "workspace")
    _nonempty_str(campaign.get("baseline_ref"), "baseline_ref")
    _nonempty_str(campaign.get("lockbox_ref"), "lockbox_ref")

    budget = campaign.get("tree_budget") or {}
    if not isinstance(budget, dict):
        raise ValueError("tree_budget must be an object")
    unknown = set(budget) - {"max_parallel", "max_total_units", "max_total_nodes",
                             "max_depth", "allowed_pools", "allowed_write_globs"}
    if unknown:
        raise ValueError("tree_budget has unsupported keys: " + ", ".join(sorted(unknown)))
    for key in ("max_parallel", "max_total_units", "max_total_nodes"):
        val = budget.get(key)
        if val is not None and (not isinstance(val, int) or isinstance(val, bool) or val < 1):
            raise ValueError(f"tree_budget.{key} must be a positive integer")
    if budget.get("max_depth", 1) != 1:
        raise ValueError("tree_budget.max_depth must be 1")
    allowed_pools = _str_list(budget.get("allowed_pools"), "tree_budget.allowed_pools")
    allowed_globs = _str_list(budget.get("allowed_write_globs"), "tree_budget.allowed_write_globs")

    units = campaign.get("units")
    if units is None:
        units = []
    if not isinstance(units, list):
        raise ValueError("units must be a list")
    max_total = budget.get("max_total_units", budget.get("max_total_nodes"))
    if max_total is not None and len(units) > max_total:
        raise ValueError("tree_budget.max_total_units exceeded by units")

    ids = []
    dedupe_keys = []
    for unit in units:
        uid = _unit_id(unit)
        ids.append(uid)
        dedupe_key = unit.get("dedupe_key")
        if dedupe_key is not None:
            if not isinstance(dedupe_key, str) or not dedupe_key.strip():
                raise ValueError(f"unit {uid} dedupe_key must be a nonempty string")
            dedupe_keys.append(dedupe_key)
        text = (unit.get("text") or unit.get("goal") or unit.get("title")
                or unit.get("task") or unit.get("description") or unit.get("hypothesis"))
        _nonempty_str(text, f"unit {uid} text/goal/hypothesis")
        scope = _str_list(unit.get("scope"), f"unit {uid} scope")
        inputs = _str_list(unit.get("inputs"), f"unit {uid} inputs")
        for value in scope + inputs:
            if any(ch in value for ch in "*?["):
                raise ValueError(f"unit {uid} scope/inputs must be literal paths, not glob patterns: {value!r}")
            todo_graph.relative(value)
        _str_list(unit.get("depends_on") or unit.get("dependsOn") or unit.get("deps"),
                  f"unit {uid} depends_on")
        if allowed_globs:
            for value in scope:
                if not any(_glob_match(value, pat) for pat in allowed_globs):
                    raise ValueError(f"unit {uid} scope {value!r} is outside allowed_write_globs")
        pool = unit.get("pool")
        if pool is not None and allowed_pools and pool not in allowed_pools:
            raise ValueError(f"unit {uid} pool {pool!r} is not in allowed_pools")
        if allowed_pools:
            for role in ("maker", "reviewer"):
                for cand in _candidates(unit, role, allowed_pools):
                    cand_pool = cand.get("pool") if isinstance(cand, dict) else None
                    cand_agent = cand.get("agent") if isinstance(cand, dict) else None
                    effective = cand_pool if cand_pool is not None else cand_agent
                    if effective is not None and effective not in allowed_pools:
                        raise ValueError(f"unit {uid} {role} pool {effective!r} is not in allowed_pools")
    if len(set(ids)) != len(ids):
        raise ValueError("units contains duplicate ids")
    if len(set(dedupe_keys)) != len(dedupe_keys):
        raise ValueError("units contains duplicate dedupe_key values")
    known = set(ids)
    depth_memo = {}
    def depth(uid, seen):
        if uid in depth_memo:
            return depth_memo[uid]
        if uid in seen:
            raise ValueError("units dependency cycle at " + uid)
        unit = next(u for u in units if _unit_id(u) == uid)
        deps = unit.get("depends_on") or unit.get("dependsOn") or unit.get("deps") or []
        for dep in deps:
            if dep not in known:
                raise ValueError(f"unit {uid} depends on unknown unit {dep!r}")
        value = 0 if not deps else 1 + max(depth(d, seen | {uid}) for d in deps)
        depth_memo[uid] = value
        return value
    for uid in ids:
        if depth(uid, set()) > 1:
            raise ValueError("units exceed tree_budget.max_depth=1 at " + uid)

    scout_contract = campaign.get("historical_scout")
    if scout_contract is not None:
        if not isinstance(scout_contract, dict):
            raise ValueError("historical_scout must be an object")
        if set(scout_contract) - history_scout.CONTRACT_KEYS:
            raise ValueError("historical_scout has unsupported keys: "
                             + ", ".join(sorted(set(scout_contract) - history_scout.CONTRACT_KEYS)))
        topics = scout_contract.get("topics")
        if (not isinstance(topics, list) or not topics
                or not all(isinstance(t, str) and t.strip() for t in topics)):
            raise ValueError("historical_scout.topics must be a nonempty list of strings")
        # Full contract validation (exactly one advisory scout, consumers
        # depending on it) runs in campaign_to_plan via
        # history_scout.validate_plan_contract.

    ensure_lockbox_isolated(campaign)
    return campaign


# Backward/forward-compatible aliases.
validate = validate_campaign


def load_campaign(campaign_path):
    path = Path(campaign_path)
    if path.is_dir():
        path = path / "campaign.json"
    campaign = _read_json(path)
    validate_campaign(campaign)
    return campaign


def campaign_to_plan(campaign):
    """Translate a validated campaign to a todo_graph plan (lockbox never included)."""
    validate_campaign(campaign)
    budget = campaign.get("tree_budget") or {}
    max_parallel = budget.get("max_parallel") or campaign.get("max_parallel") or 1
    tasks = []
    for unit in campaign.get("units") or []:
        uid = _unit_id(unit)
        text = (unit.get("text") or unit.get("goal") or unit.get("title")
                or unit.get("task") or unit.get("description") or unit.get("hypothesis"))
        _mode = unit.get("mode")
        if _mode in ("doc", "scout"):
            _mode = "advisory"
        if _mode is None:
            _mode = "implement" if unit.get("checks") else "advisory"
        task = {
            "id": uid,
            "text": text,
            "mode": _mode,
            "covers": _str_list(unit.get("covers"), "covers") or [uid],
            "done_when": _str_list(unit.get("done_when"), "done_when") or [f"Unit {uid} completed"],
            "scope": _str_list(unit.get("scope"), "scope"),
            "inputs": _str_list(unit.get("inputs"), "inputs"),
            "depends_on": list(unit.get("depends_on") or unit.get("dependsOn") or unit.get("deps") or []),
            "maker": _candidates(unit, "maker", budget.get("allowed_pools")),
            "reviewer": _candidates(unit, "reviewer", budget.get("allowed_pools")),
        }
        if unit.get("role"):
            task["role"] = unit["role"]
        for key in ("checks", "resources", "requires_approval", "timeout_seconds",
                    "complexity", "task_kind", "kind", "allow_pool_wait", "durable",
                    "dedupe_key"):
            if key in unit:
                task[key] = unit[key]
        # Pool cooldown must surface as RESUMABLE_POOL_WAIT (todo_fleet only
        # classifies it when the task opts in), never as a model retry.
        task.setdefault("allow_pool_wait", True)
        tasks.append(task)
    _ws = Path(campaign["workspace"])
    if not _ws.is_absolute():
        _ws = Path.cwd() / _ws
    _reqs = _str_list(campaign.get("requirements"), "requirements")
    if not _reqs:
        _reqs = list(dict.fromkeys(c for t in tasks for c in t["covers"]))
    plan = {
        "version": 1,
        "campaign_id": campaign["campaign_id"],
        "baseline_ref": campaign["baseline_ref"],
        "goal": campaign["goal"],
        "planner": {"model": "arbor-campaign/v1", "source": "durable_supervisor"},
        "requirements": _reqs,
        "max_parallel": max_parallel,
        "workspace": str(_ws.resolve()),
        "constraints": _str_list(campaign.get("constraints"), "constraints"),
        "tasks": tasks,
    }
    if campaign.get("historical_scout") is not None:
        # The native todo_graph entry enforces this contract for every new
        # run; pass it through verbatim and validate it here so a malformed
        # campaign fails at translation time, not after owner claiming.
        plan["historical_scout"] = campaign["historical_scout"]
        history_scout.validate_plan_contract(plan)
    elif campaign.get("execution_context") is not None:
        plan["execution_context"] = campaign["execution_context"]
    else:
        plan["execution_context"] = "legacy-test"
    tree_budget = {}
    if budget.get("max_parallel") is not None:
        tree_budget["max_parallel"] = budget["max_parallel"]
    max_total = budget.get("max_total_units", budget.get("max_total_nodes"))
    if max_total is not None:
        tree_budget["max_total_nodes"] = max_total
    tree_budget["max_depth"] = 1
    if budget.get("allowed_pools"):
        tree_budget["allowed_pools"] = list(budget["allowed_pools"])
    if tree_budget:
        plan["tree_budget"] = tree_budget
    # Defence in depth: check real plan values (not JSON serialization, which
    # doubles Windows backslashes) before the plan becomes maker-visible.
    # Real task scope/inputs keep full containment semantics (a scope that
    # contains the lockbox would expose it); every other maker-visible value
    # is checked only for literally naming the lockbox, so a lockbox that
    # merely sits inside the workspace tree does not reject the campaign.
    for task in tasks:
        for key in _UNIT_SCOPE_KEYS:
            if key in task and _any_lockbox(task[key], campaign["lockbox_ref"], workspace=plan["workspace"]):
                raise ValueError("lockbox_ref would leak into maker-visible todo.json; refusing plan")
    plan_without_scope = dict(plan)
    plan_without_scope["tasks"] = [
        {k: v for k, v in task.items() if k not in _UNIT_SCOPE_KEYS} for task in tasks
    ]
    if _literal_lockbox_leak(plan_without_scope, campaign["lockbox_ref"]):
        raise ValueError("lockbox_ref would leak into maker-visible todo.json; refusing plan")
    if tasks:
        todo_graph.validate(plan)
    return plan


build_plan = campaign_to_plan
to_todo_plan = campaign_to_plan


def _fleet_native_adapter(adapter):
    """True when the adapter drives the real Fleet bridge (default or subclass)."""
    if adapter is None:
        return True
    fleet = getattr(todo_graph, "FleetAdapter", None)
    return fleet is not None and isinstance(adapter, fleet)


@contextmanager
def _pool_guard_env(campaign, adapter):
    """Make tree_budget.allowed_pools a hard limit for fleet-native runs.

    todo_graph does not forward allowed_pools and todo_fleet's tier routing
    may substitute a candidate from the whole catalog when no allowed-pool
    candidate passes preflight. AGENT_GRAPH_TIER_ROUTING=0 disables that
    substitution, so an exhausted allowed pool yields the pool-wait/BLOCKED
    path instead of an out-of-pool call. Candidates in the plan are already
    restricted to allowed pools by campaign_to_plan/_candidates.

    The flag is set on this process instead of injecting a wrapper adapter:
    todo_graph.command merges os.environ into every bridge child, and keeping
    adapter=None preserves todo_graph's native live path (the
    evidence-outside-workspace guard and Office monitor publishing), which an
    injected FleetAdapter subclass would silently disable.
    """
    budget = campaign.get("tree_budget") or {}
    if not budget.get("allowed_pools") or not _fleet_native_adapter(adapter):
        yield
        return
    previous = os.environ.get("AGENT_GRAPH_TIER_ROUTING")
    os.environ["AGENT_GRAPH_TIER_ROUTING"] = "0"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("AGENT_GRAPH_TIER_ROUTING", None)
        else:
            os.environ["AGENT_GRAPH_TIER_ROUTING"] = previous


def _enforce_evidence_outside_workspace(campaign, plan, cdir, adapter=None):
    """Refuse fleet-native execution with the campaign dir inside the workspace.

    todo_graph's native guard fires only on its live path (adapter is None).
    The supervisor applies the same verdict itself, before ownership is
    claimed or any model is called, so a campaign cannot write evidence into
    the execution workspace regardless of the adapter passed.
    """
    if not _fleet_native_adapter(adapter):
        return
    workspace = Path(plan["workspace"]).resolve()
    if _canonical_dir(cdir).is_relative_to(workspace):
        raise ValueError("Keep evidence outside the execution workspace")


def _canonical_dir(value):
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def _resolve_paths(campaign, campaign_path, campaign_dir):
    if isinstance(campaign, (str, Path)):
        cpath = Path(campaign).expanduser()
        if not cpath.is_absolute():
            cpath = Path.cwd() / cpath
        cpath = cpath.resolve()
        if cpath.is_dir():
            cdir = cpath
            cfile = cpath / "campaign.json"
            data = load_campaign(cfile)
        else:
            cfile = cpath
            cdir = _canonical_dir(campaign_dir) if campaign_dir else cpath.parent
            data = load_campaign(cfile)
        return data, cdir
    if isinstance(campaign, dict):
        validate_campaign(campaign)
        if campaign_dir is None and campaign_path is not None:
            p = Path(campaign_path).expanduser()
            if not p.is_absolute():
                p = Path.cwd() / p
            p = p.resolve()
            cdir = p if p.is_dir() else p.parent
        elif campaign_dir is not None:
            cdir = _canonical_dir(campaign_dir)
        else:
            raise ValueError("campaign_dir is required for an in-memory campaign")
        return campaign, cdir
    raise ValueError("campaign must be a path or an object")


def _durable_path(cdir):
    return Path(cdir) / "durable-state.json"


def _read_durable(cdir):
    path = _durable_path(cdir)
    if path.is_file():
        try:
            data = _read_json(path)
            return data if isinstance(data, dict) else {}
        except (ValueError, OSError):
            return {}
    return {}


def _owner_alive(state):
    """True unless the recorded owner process instance is proven gone.

    Process-instance identity matters: a live PID whose current start time
    is newer than the recorded claim (todo_graph._pid_reused) is a foreign
    process that recycled the PID — the recorded owner is dead and takeover
    may proceed. Unknown liveness or unknown identity evidence stays
    blocked (fail-closed).
    """
    pid = state.get("pid")
    if not isinstance(pid, int):
        return False
    if pid == os.getpid():
        return True
    # Fail closed: only a proven-dead owner (alive is False) may be replaced;
    # unknown liveness (None) blocks takeover exactly like a live owner.
    alive = todo_graph._process_alive(pid)
    if alive is False:
        return False
    if alive is not True:
        return True
    recorded = state.get("process_started_at")
    if recorded is None:
        recorded = state.get("started_at")
    if todo_graph._pid_reused(pid, recorded) is True:
        return False  # PID recycled by a foreign process; owner is gone
    return True


def _dead_lock_owner(raw, protect_own_pid=False):
    try:
        pid = int(raw.decode("utf-8").strip().split(":", 1)[0])
    except (UnicodeDecodeError, ValueError, IndexError):
        return False
    if not todo_graph._valid_pid(pid) or (protect_own_pid and pid == os.getpid()):
        return False
    return todo_graph._process_alive(pid) is False


def _windows_retire_lock_if_dead(path, target, protect_own_pid):
    """Rename the exact open lock instance; its share mode blocks replacement."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetFileSizeEx.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
    kernel.GetFileSizeEx.restype = wintypes.BOOL
    kernel.ReadFile.argtypes = [
        wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
        wintypes.LPVOID, wintypes.LPVOID,
    ]
    kernel.ReadFile.restype = wintypes.BOOL
    kernel.SetFileInformationByHandle.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
    ]
    kernel.SetFileInformationByHandle.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(
        str(path), 0x80000000 | 0x00010000,  # GENERIC_READ | DELETE
        0x00000001 | 0x00000002,  # share read/write, deliberately not delete
        None, 3, 0x00000080, None,  # OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL
    )
    invalid = wintypes.HANDLE(-1).value
    if not handle or handle == invalid:
        error = ctypes.get_last_error()
        raise OSError(error, "cannot pin lock instance", str(path))
    try:
        current = path.lstat()
        if path.is_symlink() or getattr(current, "st_file_attributes", 0) & 0x400:
            return False
        size = ctypes.c_longlong()
        if not kernel.GetFileSizeEx(handle, ctypes.byref(size)):
            raise OSError(ctypes.get_last_error(), "cannot size lock instance", str(path))
        if size.value < 1 or size.value > 4096:
            return False
        buffer = ctypes.create_string_buffer(size.value)
        read = wintypes.DWORD()
        if not kernel.ReadFile(handle, buffer, size.value, ctypes.byref(read), None):
            raise OSError(ctypes.get_last_error(), "cannot read lock instance", str(path))
        raw = buffer.raw[:read.value]
        if not _dead_lock_owner(raw, protect_own_pid=protect_own_pid):
            return False

        class RenameInfo(ctypes.Structure):
            _fields_ = [
                ("ReplaceIfExists", wintypes.BOOLEAN),
                ("RootDirectory", wintypes.HANDLE),
                ("FileNameLength", wintypes.DWORD),
                ("FileName", wintypes.WCHAR * 1),
            ]

        encoded = str(target).encode("utf-16-le")
        storage = ctypes.create_string_buffer(ctypes.sizeof(RenameInfo) + len(encoded) + 2)
        info = ctypes.cast(storage, ctypes.POINTER(RenameInfo)).contents
        info.ReplaceIfExists = False
        info.RootDirectory = None
        info.FileNameLength = len(encoded)
        ctypes.memmove(ctypes.addressof(info) + RenameInfo.FileName.offset, encoded, len(encoded))
        if not kernel.SetFileInformationByHandle(handle, 3, ctypes.byref(info), len(storage)):
            raise OSError(ctypes.get_last_error(), "cannot retire pinned lock instance", str(path))
        return True
    finally:
        kernel.CloseHandle(handle)


def _retire_lock_if_dead(path, target, protect_own_pid=False):
    """Retire one proven-dead lease without a check/path-rename race.

    Failure to pin or atomically rename the checked instance refuses takeover;
    there is deliberately no fallback to a fresh stat followed by Path.rename.
    """
    path = Path(path)
    target = Path(target)
    try:
        if os.name == "nt":
            return _windows_retire_lock_if_dead(path, target, protect_own_pid)
        import fcntl

        fd = os.open(path, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            raw = os.read(fd, 4097)
            if len(raw) > 4096 or not _dead_lock_owner(raw, protect_own_pid=protect_own_pid):
                return False
            pinned = os.fstat(fd)
            current = path.lstat()
            if (path.is_symlink()
                    or (pinned.st_dev, pinned.st_ino) != (current.st_dev, current.st_ino)):
                return False
            path.rename(target)
            return True
        finally:
            os.close(fd)
    except (OSError, ValueError):
        return False


@contextmanager
def _owner_lock(cdir):
    """Exclusive owner lock; a proven-dead holder's lock is retired by rename."""
    lock = Path(cdir) / ".durable-owner.lock"
    while True:
        try:
            with todo_graph.exclusive(lock):
                yield
            return
        except FileExistsError:
            stale = lock.with_name(lock.name + f".stale-{uuid.uuid4().hex}")
            if _retire_lock_if_dead(lock, stale, protect_own_pid=True):
                continue
            raise ValueError("durable-state owner lock is held; refusing concurrent claim")


def _claim_owner(cdir, campaign, status="RUNNING"):
    """Fencing: atomically increment generation only when the previous owner is dead."""
    cdir = _canonical_dir(cdir)
    cdir.mkdir(parents=True, exist_ok=True)
    with _owner_lock(cdir):
        prior = _read_durable(cdir)
        if prior and prior.get("campaign_id") != campaign.get("campaign_id"):
            raise ValueError("durable-state campaign_id mismatch; refusing to reuse this campaign directory")
        if (prior and prior.get("baseline_ref") is not None
                and prior.get("baseline_ref") != campaign.get("baseline_ref")):
            raise ValueError("durable-state baseline_ref mismatch; refusing to reuse this campaign directory")
        if prior and _owner_alive(prior) and prior.get("pid") != os.getpid():
            raise ValueError(f"durable-state owner pid {prior.get('pid')} is still alive; refusing takeover")
        generation = int(prior.get("generation") or 0) + 1 if prior else 1
        state = {
            "schema_version": DURABLE_STATE_SCHEMA,
            "campaign_id": campaign.get("campaign_id"),
            "baseline_ref": campaign.get("baseline_ref"),
            "pid": os.getpid(),
            "started_at": _now(),
            "process_started_at": todo_graph._process_start_time(os.getpid()),
            "generation": generation,
            "status": status,
            "model_calls": int(prior.get("model_calls") or 0),
            "updated_at": _now(),
        }
        if prior and "next_check_at" in prior:
            state["next_check_at"] = prior["next_check_at"]
        _write_json(_durable_path(cdir), state)
        return state


def _save_durable(cdir, durable, **updates):
    cdir = _canonical_dir(cdir)
    with _owner_lock(cdir):
        on_disk = _read_durable(cdir)
        if on_disk and durable.get("generation") is not None:
            if (on_disk.get("pid") != durable.get("pid")
                    or on_disk.get("generation") != durable.get("generation")):
                raise ValueError("durable-state ownership lost (pid/generation mismatch); refusing stale write")
        durable.update(updates)
        durable["updated_at"] = _now()
        _write_json(_durable_path(cdir), durable)
        return durable


def _run_state(cdir):
    path = Path(cdir) / "state.json"
    if path.is_file():
        try:
            data = _read_json(path)
            return data if isinstance(data, dict) else {}
        except (ValueError, OSError):
            return {}
    return {}


def _parse_time(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _pool_wait_pending(nodes):
    """Return (waiting, next_check_at) when any node waits on a pool cooldown."""
    waiting = False
    nxt = None
    for node in nodes.values():
        if not isinstance(node, dict):
            continue
        if node.get("status") != "RESUMABLE_POOL_WAIT" and not node.get("durable_parked_pool_wait"):
            continue
        waiting = True
        candidate = _parse_time(node.get("next_check_at"))
        if candidate is not None:
            nxt = candidate if nxt is None else min(nxt, candidate)
    return waiting, nxt


def _result_is_model_call(result):
    """True when adapter evidence shows a model was actually invoked.

    Preflight/candidate-selection refusals and pool-wait responses never reach
    a model. A real invocation carries identity/usage evidence (lineage,
    tokens, run_id, ...) or a product status (CANDIDATE/PARTIAL/REVIEWED).
    """
    if not isinstance(result, dict):
        return False
    if result.get("status") == "RESUMABLE_POOL_WAIT" or result.get("resumable_pool_wait") is True:
        return False
    for key in ("lineage", "tokens", "usage_record", "model_tool_calls", "run_id"):
        if result.get(key):
            return True
    if result.get("status") in ("CANDIDATE", "PARTIAL", "REVIEWED"):
        return True
    preflight = result.get("preflight")
    if isinstance(preflight, list) and preflight:
        return False
    if isinstance(preflight, dict) and preflight.get("status") not in (
            None, "not-applicable", "pass", "PASS", "ok", "OK"):
        return False
    return False


def _node_model_calls(node):
    """Confirmed model invocations recorded for one node.

    Counts maker/reviewer/autopsy history entries whose result evidences an
    actual model execution; pool-wait responses and preflight refusals are
    excluded. Falls back to an explicit node model_calls counter only when no
    invocation history entries exist at all.
    """
    if not isinstance(node, dict):
        return 0
    history = node.get("history")
    if isinstance(history, list):
        calls = 0
        seen_invocation_entries = False
        for entry in history:
            if not isinstance(entry, dict) or entry.get("role") not in ("maker", "reviewer", "autopsy"):
                continue
            seen_invocation_entries = True
            result = entry.get("result")
            if not isinstance(result, dict):
                continue
            if _result_is_model_call(result):
                calls += 1
        if seen_invocation_entries:
            return calls
    counter = node.get("model_calls")
    return counter if isinstance(counter, int) and not isinstance(counter, bool) else 0


def _model_calls(nodes):
    return sum(_node_model_calls(node) for node in nodes.values())


def _future_pool_waits(nodes):
    """RESUMABLE_POOL_WAIT nodes whose own next_check_at is still in future."""
    return sorted(_cooled_units(nodes).items())


def _cooled_units(nodes):
    """Units still inside their own pool cooldown: {unit_id: next_check_at}.

    Parked leftovers (durable_parked_pool_wait) count too, so a crash while
    units were parked out of a delegation cannot make them runnable early.
    """
    now = time.time()
    cooled = {}
    for uid, node in nodes.items():
        if not isinstance(node, dict):
            continue
        if node.get("status") != "RESUMABLE_POOL_WAIT" and not node.get("durable_parked_pool_wait"):
            continue
        candidate = _parse_time(node.get("next_check_at"))
        if candidate is not None and candidate > now:
            cooled[uid] = candidate
    return cooled


def _park_cooled_units(cdir, cooled_ids):
    """Hold cooled units out of one todo_graph.run delegation.

    todo_graph.run pops RESUMABLE_POOL_WAIT nodes and would redispatch them
    immediately, so each cooled node is parked as WAITING_APPROVAL (a status
    the scheduler leaves untouched) while keeping its next_check_at. Returns
    the original node dicts for restoration after the run.
    """
    state = _run_state(cdir)
    nodes = state.get("nodes")
    if not isinstance(nodes, dict):
        return {}
    originals = {}
    for uid in cooled_ids:
        node = nodes.get(uid)
        if not isinstance(node, dict):
            continue
        if node.get("status") == "RESUMABLE_POOL_WAIT":
            originals[uid] = dict(node)
            parked = dict(node)
            parked["status"] = "WAITING_APPROVAL"
            parked["durable_parked_pool_wait"] = True
            nodes[uid] = parked
        elif node.get("durable_parked_pool_wait"):
            original = dict(node)
            original["status"] = "RESUMABLE_POOL_WAIT"
            original.pop("durable_parked_pool_wait", None)
            originals[uid] = original
    if originals:
        _write_json(Path(cdir) / "state.json", state)
    return originals


def _restore_parked_units(cdir, originals, nodes):
    """Undo _park_cooled_units in scheduler state and in the returned nodes."""
    if not originals:
        return nodes
    merged = dict(nodes)
    for uid, original in originals.items():
        current = merged.get(uid)
        if isinstance(current, dict) and current.get("durable_parked_pool_wait"):
            merged[uid] = original
    state = _run_state(cdir)
    snodes = state.get("nodes")
    if isinstance(snodes, dict):
        changed = False
        for uid, original in originals.items():
            current = snodes.get(uid)
            if isinstance(current, dict) and current.get("durable_parked_pool_wait"):
                snodes[uid] = original
                changed = True
        if changed:
            _write_json(Path(cdir) / "state.json", state)
    return merged


def _restore_leftover_parked(cdir):
    """Restore every leftover durable_parked_pool_wait before readiness.

    A crash between park and restore leaves WAITING_APPROVAL nodes that
    todo_graph.run would skip forever once next_check_at expires. Restoring
    them to RESUMABLE_POOL_WAIT first lets _cooled_units park still-cooling
    units again, while expired ones become runnable (todo_graph pops
    RESUMABLE_POOL_WAIT and re-dispatches).
    """
    state = _run_state(cdir)
    nodes = state.get("nodes")
    if not isinstance(nodes, dict):
        return {}
    restored = {}
    for uid, node in list(nodes.items()):
        if not isinstance(node, dict) or not node.get("durable_parked_pool_wait"):
            continue
        original = dict(node)
        original["status"] = "RESUMABLE_POOL_WAIT"
        original.pop("durable_parked_pool_wait", None)
        nodes[uid] = original
        restored[uid] = original
    if restored:
        _write_json(Path(cdir) / "state.json", state)
    return restored


def _norm_lockbox_str(value):
    """Windows-normalized path string: forward slashes, casefold, no ./ or tail /."""
    s = str(value).replace("\\", "/").casefold()
    while s.startswith("./"):
        s = s[2:]
    return s.rstrip("/")


def _lock_segments(value):
    s = _norm_lockbox_str(value)
    return [p for p in PurePosixPath(s).parts if p not in ("/", "")]


def _embed_boundary_before(norm, i):
    if i <= 0:
        return True
    ch = norm[i - 1]
    return not (ch.isalnum() or ch in "._-")


def _embed_boundary_after(norm, j):
    if j >= len(norm):
        return True
    ch = norm[j]
    if ch in "/\\":
        return True  # still names the lockbox or a path under it
    if ch == ".":
        return j + 1 >= len(norm) or not norm[j + 1].isalnum()
    return not (ch.isalnum() or ch in "_-")


# Split free text / CLI arguments into path-like tokens (keep :, /, \\, ., _, -).
_LOCKBOX_TOKEN_SPLIT = re.compile(r"[\s=,;()\[\]{}<>|'\"`!?&*]+")


def _string_names_lockbox(item, lock_str, lock_parts):
    """True when a single string names the lockbox inside text or arguments."""
    norm = _norm_lockbox_str(item)
    if not norm or not lock_str or not lock_parts:
        return False
    # 1) lockbox path embedded in free text or a CLI argument value
    start = 0
    while True:
        idx = norm.find(lock_str, start)
        if idx < 0:
            break
        end = idx + len(lock_str)
        if _embed_boundary_before(norm, idx) and _embed_boundary_after(norm, end):
            return True
        start = idx + 1
    # 2) segment match on path-like tokens (relative / suffix naming)
    min_tail = min(2, len(lock_parts))
    for token in _LOCKBOX_TOKEN_SPLIT.split(norm):
        if not token or token in (".", ".."):
            continue
        parts = [p for p in PurePosixPath(token).parts if p not in ("/", "")]
        if not parts:
            continue
        if any(parts[i:i + len(lock_parts)] == lock_parts
               for i in range(len(parts) - len(lock_parts) + 1)):
            return True
        for k in range(0, len(lock_parts) - min_tail + 1):
            if parts == lock_parts[k:]:
                return True
    return False


def _literal_lockbox_leak(value, lockbox_ref):
    """True when lockbox_ref is named in a maker-visible value.

    Unlike _contains_lockbox this has no containment semantics: an ancestor
    path such as the workspace root does not count as naming the lockbox, so
    a lockbox merely located inside the workspace tree is not a leak. Free
    text and CLI arguments are scanned for embedded lockbox names, with
    Windows separator and casefold normalization.
    """
    if isinstance(value, dict):
        return any(_literal_lockbox_leak(item, lockbox_ref) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_literal_lockbox_leak(item, lockbox_ref) for item in value)
    if not isinstance(value, str):
        return False
    lock_parts = _lock_segments(lockbox_ref)
    if not lock_parts:
        return False
    return _string_names_lockbox(value, _norm_lockbox_str(lockbox_ref), lock_parts)


_CANCEL_REQUEST_GLOB = "cancel-request*.json"


def _cancel_request_paths(cdir):
    paths = []
    for path in sorted(Path(cdir).glob(_CANCEL_REQUEST_GLOB)):
        name = path.name
        if name.endswith(".applied.json") or ".applied." in name or name.endswith(".tmp"):
            continue
        paths.append(path)
    return paths


def _read_cancel_request(path, campaign):
    try:
        data = _read_json(path)
    except (ValueError, OSError):
        return None
    if not isinstance(data, dict) or data.get("campaign_id") != campaign.get("campaign_id"):
        return None
    unit_ids = data.get("unit_ids")
    if unit_ids is not None and (not isinstance(unit_ids, list)
                                 or not all(isinstance(u, str) for u in unit_ids)):
        return None
    return {"path": Path(path), "unit_ids": unit_ids,
            "requested_at": data.get("requested_at"),
            "request_id": data.get("request_id") or Path(path).stem}


def _pending_cancel_requests(cdir, campaign):
    out = []
    for path in _cancel_request_paths(cdir):
        req = _read_cancel_request(path, campaign)
        if req is not None:
            out.append(req)
    return out


def _write_cancel_request(cdir, campaign, unit_ids):
    """One durable file per request so concurrent cancels never overwrite each other."""
    rid = uuid.uuid4().hex
    request = {"campaign_id": campaign["campaign_id"],
               "unit_ids": list(unit_ids) if unit_ids is not None else None,
               "requested_at": _now(),
               "request_id": rid}
    path = Path(cdir) / f"cancel-request.{rid}.json"
    _write_json(path, request)
    return {"path": path, "unit_ids": request["unit_ids"],
            "requested_at": request["requested_at"], "request_id": rid}


def _mark_cancel_applied(cdir, requests):
    """Retire only the applied request files by rename (never delete)."""
    retired = []
    for req in requests or []:
        path = req.get("path") if isinstance(req, dict) else Path(req)
        if not path:
            continue
        path = Path(path)
        if not path.is_file():
            continue
        try:
            path.rename(path.with_name(f"{path.name}.{time.time_ns()}.applied.json"))
            retired.append(str(path))
        except OSError:
            pass
    return retired


def _apply_cancel_requests(campaign, plan, cdir, durable, requests, calls_made):
    """Owner-side application of cross-process cancel requests.

    Runs todo_graph.cancel_tasks while this process holds the durable
    ownership claim, then retires exactly the applied request files so a
    newer request that arrived during application is never dropped.
    """
    if not requests:
        requests = [{"path": None, "unit_ids": None}]
    if any(req.get("unit_ids") is None for req in requests):
        task_ids = None
    else:
        task_ids = list(dict.fromkeys(u for req in requests for u in req.get("unit_ids") or []))
    state = todo_graph.cancel_tasks(cdir, plan, task_ids=task_ids)
    nodes = state.get("nodes") or {}
    persisted = _run_state(cdir).get("nodes") or {}
    requested = task_ids if task_ids is not None else [_unit_id(u) for u in campaign.get("units") or []]
    for uid in requested:
        if (persisted.get(uid) or {}).get("status") != "CANCELLED":
            raise ValueError(f"cancel was not persisted for unit {uid}")
    _publish_results(campaign, cdir, nodes)
    _mark_cancel_applied(cdir, [req for req in requests if req.get("path")])
    campaign_calls = _model_calls(nodes)
    out = {"status": "CANCELLED", "campaign_id": campaign["campaign_id"],
           "model_calls": calls_made,
           "campaign_model_calls": campaign_calls,
           "units": {k: v.get("status") for k, v in nodes.items()}}
    _save_durable(cdir, durable, status="CANCELLED",
                  model_calls=campaign_calls, units=out["units"])
    return out


def _verify_terminal_integrity(plan, run_state):
    """Mirror todo_graph.run integrity gates for the no-delegation fast path.

    Returns an error string, or None when plan_hash and every ACCEPTED
    artifact / final-UAT snapshot still match the current workspace.
    """
    if not run_state:
        return None
    if run_state.get("plan_hash") != todo_graph.digest(plan):
        return "plan_hash mismatch: campaign/plan changed since the recorded run"
    tasks = {task["id"]: task for task in plan.get("tasks") or []}
    workspace = plan["workspace"]
    for key, node in (run_state.get("nodes") or {}).items():
        if not isinstance(node, dict) or node.get("status") != "ACCEPTED":
            continue
        task = tasks.get(key)
        if task is None:
            return f"ACCEPTED node {key} is not in the current plan"
        try:
            actual = todo_graph.snapshot(workspace, list(task.get("scope") or []))
        except (ValueError, OSError) as exc:
            return f"ACCEPTED node {key} artifact cannot be verified: {exc}"
        if node.get("artifact") != actual:
            return f"ACCEPTED node {key} artifact changed since acceptance"
    uat = run_state.get("uat") or {}
    final = uat.get("final") or {}
    evidence = final.get("evidence") or {}
    if final.get("status") == "ACCEPTED" and evidence.get("artifact_digest"):
        try:
            actual = todo_graph.snapshot(workspace, list(evidence.get("artifact_scope") or []))
        except (ValueError, OSError) as exc:
            return f"final UAT artifact cannot be verified: {exc}"
        if evidence.get("artifact_digest") != actual:
            return "final UAT artifact changed since acceptance"
    return None


def _retire_dead_locks(cdir, plan, allow_workspace_lock=True):
    """Rename (never delete) locks proven dead so recovery can re-acquire them.

    Mirrors todo_graph._lock_evidence paths and its retained ``.released``
    convention. A lock is retired only when _process_alive(pid) is False
    (proven dead); live or unknown liveness leaves the lock untouched.
    """
    retired = []
    workspace_key = hashlib.sha256(
        str(Path(plan["workspace"]).resolve()).casefold().encode()
    ).hexdigest()
    candidates = [Path(cdir) / "runner.lock"]
    if allow_workspace_lock:
        candidates.append(Path.home() / ".agents" / "todo-locks" / (workspace_key + ".lock"))
    for path in candidates:
        if not path.is_file():
            continue
        target = path.with_name(
            f"{path.name}.{time.time_ns()}.{uuid.uuid4().hex[:8]}.stale-released"
        )
        if _retire_lock_if_dead(path, target, protect_own_pid=True):
            retired.append(str(path))
    return retired


def append_experience(campaign_dir, record):
    """Idempotently append one arbor-experience/v1 record (never truncates)."""
    if not isinstance(record, dict):
        raise ValueError("experience record must be an object")
    rec = dict(record)
    rec.setdefault("schema_version", EXPERIENCE_SCHEMA)
    if rec["schema_version"] != EXPERIENCE_SCHEMA:
        raise ValueError("experience schema_version must be 'arbor-experience/v1'")
    lesson_id = rec.get("lesson_id")
    unit_id = rec.get("unit_id")
    if not lesson_id and unit_id:
        lesson_id = str(unit_id)
        rec["lesson_id"] = lesson_id
    if not lesson_id:
        raise ValueError("experience record requires lesson_id or unit_id")
    rec.setdefault("created_at", _now())
    cdir = Path(campaign_dir)
    cdir.mkdir(parents=True, exist_ok=True)
    path = cdir / "experience.jsonl"
    existing_lessons, existing_units = set(), set()
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                old = json.loads(line)
            except ValueError:
                continue
            if isinstance(old, dict):
                if old.get("lesson_id"):
                    existing_lessons.add(old["lesson_id"])
                if old.get("unit_id"):
                    existing_units.add(old["unit_id"])
    # Dedupe contract is lesson_id/unit_id: a known lesson_id or a known
    # unit_id is always a duplicate; re-reading the file after a crash between
    # append and state save therefore never appends the same record twice.
    if lesson_id in existing_lessons or (unit_id and unit_id in existing_units):
        return {"appended": False, "duplicate": True, "lesson_id": lesson_id}
    # Resume safely after a torn tail (crash mid-append): never glue the new
    # record onto an incomplete final line and never truncate existing bytes.
    needs_separator = False
    if path.is_file():
        with path.open("rb") as probe:
            probe.seek(0, os.SEEK_END)
            if probe.tell() > 0:
                probe.seek(-1, os.SEEK_END)
                needs_separator = probe.read(1) != b"\n"
    with path.open("a", encoding="utf-8") as fh:
        if needs_separator:
            fh.write("\n")
        fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return {"appended": True, "duplicate": False, "lesson_id": lesson_id}


def _dedupe_key(campaign, unit):
    return unit.get("dedupe_key") or f"{campaign['campaign_id']}:{_unit_id(unit)}"


def _dedupe_payload_hash(campaign, unit):
    payload = {key: value for key, value in unit.items() if key != "dedupe_key"}
    return todo_graph.digest({
        "campaign_id": campaign["campaign_id"],
        "unit_id": _unit_id(unit),
        "unit": payload,
    })


def _register_dedupe_events(campaign, cdir):
    """Claim every unit's dedupe_key in the run-folder event registry.

    The registry is the existing ``durable-events.json`` owned by
    ``todo_graph.record_durable_event``; no second registry is created.
    A repeated identical delivery is a duplicate (no new effect claim),
    while the same key with a different payload raises ``ValueError``.
    """
    outcomes = {}
    for unit in campaign.get("units") or []:
        key = _dedupe_key(campaign, unit)
        outcomes[key] = todo_graph.record_durable_event(
            cdir, key, _dedupe_payload_hash(campaign, unit))
    return outcomes


def _publish_results(campaign, cdir, nodes):
    results_dir = Path(cdir) / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    published = {}
    cid = campaign.get("campaign_id")
    ws_raw = campaign.get("workspace")
    ws_path = Path(ws_raw) if ws_raw else None
    for unit in campaign.get("units") or []:
        uid = _unit_id(unit)
        node = nodes.get(uid)
        if not isinstance(node, dict):
            continue
        status = node.get("status")
        if status not in TERMINAL and status != "RESUMABLE_POOL_WAIT":
            continue
        ts = _now()
        mcalls = _node_model_calls(node)
        hyp = (unit.get("hypothesis") or unit.get("text") or unit.get("goal")
               or unit.get("title") or unit.get("task") or unit.get("description") or "")
        history = node.get("history") if isinstance(node.get("history"), list) else []
        check_results = None
        check_evidence_at = -1
        review_verdict = None
        for index, entry in enumerate(history):
            if not isinstance(entry, dict):
                continue
            if (entry.get("role") in ("reviewer", "tests")
                    and isinstance(entry.get("tests"), list)
                    and index > check_evidence_at):
                check_results = entry["tests"]
                check_evidence_at = index
            if entry.get("role") == "reviewer" and isinstance(entry.get("result"), dict):
                review_verdict = entry["result"]
        if check_results is None and isinstance(node.get("tests"), list):
            check_results = node["tests"]
        if check_results is None:
            check_results = node.get("checks") or node.get("check_results") or []
        if review_verdict is None:
            review_verdict = node.get("review") or node.get("review_verdict") or {}
        record_durable = todo_graph.record_durable_event(
            cdir, _dedupe_key(campaign, unit), _dedupe_payload_hash(campaign, unit))
        record = {
            "schema_version": UNIT_RESULT_SCHEMA,
            "campaign_id": cid,
            "unit_id": uid,
            "parent_job_id": unit.get("parent_job_id") or cid,
            "dedupe_key": unit.get("dedupe_key") or f"{cid}:{uid}",
            "durable_event_duplicate": bool(record_durable.get("duplicate")),
            "run_id": node.get("run_id") or (node.get("maker") or {}).get("run_id") or f"run-{cid}-{uid}",
            "status": status,
            "model_calls": mcalls,
            "check_results": check_results,
            "review_verdict": review_verdict,
            "artifact_hashes": ({"scope_digest": node["artifact"]}
                                if node.get("artifact") else (node.get("artifact_hashes") or {})),
            "usage": node.get("usage") or {"model_calls": mcalls},
            "evidence_paths": node.get("evidence_paths") or [f"results/{uid}.json"],
            "completed_at": ts,
            "finished_at": ts,
            "result": node,
        }
        _write_json(results_dir / f"{uid}.json", record)
        published[uid] = record
        if status in TERMINAL:
            metrics_delta = {}
            summary = f"Unit {uid} finished with status {status}"
            if ws_path:
                for sp in unit.get("scope") or []:
                    cand = ws_path / sp
                    if cand.is_file() and cand.suffix.lower() == ".json":
                        try:
                            exp_res = _read_json(cand)
                            if isinstance(exp_res, dict) and "delta_accuracy" in exp_res:
                                metrics_delta = {
                                    k: exp_res[k]
                                    for k in ("selection_accuracy", "selection_logloss",
                                              "baseline_selection_accuracy", "delta_accuracy",
                                              "repeats", "verdict")
                                    if k in exp_res
                                }
                                summary = (f"Unit {uid} ({status}): verdict={exp_res.get('verdict')} "
                                           f"selection_acc={exp_res.get('selection_accuracy')} "
                                           f"delta={exp_res.get('delta_accuracy')}")
                                break
                        except (ValueError, OSError):
                            pass
            append_experience(cdir, {
                "schema_version": EXPERIENCE_SCHEMA,
                "lesson_id": f"{cid}:{uid}",
                "campaign_id": cid,
                "unit_id": uid,
                "timestamp": ts,
                "domain": campaign.get("domain") or "ml-experiment",
                "project": campaign.get("project_slug") or "agent-stack",
                "environment": "AILab-windows",
                "trust": "verified" if status == "ACCEPTED" else "observed",
                "transfer_condition": f"Campaign {cid} unit {uid}",
                "hypothesis": hyp,
                "status": status,
                "outcome": status,
                "outcome_summary": summary,
                "lesson": summary,
                "metrics_delta": metrics_delta,
                "evidence_ref": f"results/{uid}.json",
            })
    return published


def _pool_wait_checkpoint(cdir, key):
    """Return the authoritative worker pool-wait checkpoint, or a refusal reason."""
    folder = Path(cdir) / key
    progress_path = folder / "progress.json"
    result_path = folder / "result.json"
    if not progress_path.is_file() and not result_path.is_file():
        return None, None
    try:
        progress = _read_json(progress_path) if progress_path.is_file() else None
        result = _read_json(result_path) if result_path.is_file() else None
    except (ValueError, OSError):
        return None, "pool-wait checkpoint is unreadable"
    progress_is_wait = (
        isinstance(progress, dict) and progress.get("status") == "RESUMABLE_POOL_WAIT"
    )
    result_is_wait = (
        isinstance(result, dict) and result.get("status") == "RESUMABLE_POOL_WAIT"
    )
    if not progress_is_wait:
        if result_is_wait:
            return None, "pool-wait checkpoint has no authoritative progress.json"
        return None, None
    # todo_graph writes progress.json before result.json. A complete progress
    # checkpoint therefore wins when a crash left result.json absent or stale.
    if progress.get("pool_phase") not in ("maker", "reviewer"):
        return None, "pool-wait checkpoint has no compatible maker/reviewer phase"
    if not isinstance(progress.get("history"), list):
        return None, "pool-wait checkpoint has no valid history"
    if progress.get("pool_phase") == "reviewer":
        pending = progress.get("review_pending")
        maker = pending.get("maker") if isinstance(pending, dict) else None
        if not isinstance(maker, dict) or maker.get("status") not in ("CANDIDATE", "PARTIAL"):
            return None, "reviewer pool-wait checkpoint has no retained maker candidate"
    return progress, None


def _interrupted_evidence_error(inspection, running):
    for key in running:
        evidence = (inspection.get("nodes") or {}).get(key) or {}
        ok_p, why_p = todo_graph._process_evidence_ok(evidence.get("process_evidence") or {})
        ok_l, why_l = todo_graph._lock_evidence_ok(evidence.get("lock_evidence") or {})
        if not (ok_p and ok_l):
            return why_p or why_l
    return None


def _latest_authoritative_maker(folder):
    """Return the newest completed maker, refusing an interrupted newer maker."""
    try:
        attempts = sorted(
            (int(p.name) for p in folder.iterdir() if p.is_dir() and p.name.isdigit()),
            reverse=True,
        )
    except OSError:
        attempts = []
    for attempt in attempts:
        auth_res, auth_usage = todo_graph._authoritative_maker_result(folder, attempt)
        if auth_res:
            return attempt, auth_res, auth_usage
        if (folder / str(attempt) / "maker").is_dir():
            return attempt, None, None
    return None, None, None


def _bind_progress_maker(progress, attempt, auth_res):
    """Bind both history and retained review to the same authoritative maker."""
    restored = dict(progress)
    restored["maker_attempt"] = attempt
    receipt = todo_graph._maker_receipt(restored)
    if receipt is None or todo_graph.digest(receipt) != todo_graph.digest(auth_res):
        history = list(restored.get("history") or [])
        history.append({"role": "maker", "result": auth_res})
        restored["history"] = history
    pending = restored.get("review_pending")
    if isinstance(pending, dict):
        restored_pending = dict(pending)
        restored_pending["maker"] = auth_res
        restored["review_pending"] = restored_pending
    return restored


def _bind_maker_evidence(evidence, attempt, auth_res, auth_usage):
    """Bind inspection identity to the maker attempt, not a later review round."""
    identity = todo_graph._receipt_identity(auth_res, auth_usage)
    evidence.update({
        "round": attempt,
        "maker_receipt": auth_res,
        "model": identity["model"],
        "model_hash": todo_graph.digest(identity["model"]) if identity["model"] is not None else None,
        "runtime": identity["runtime"],
        "runtime_hash": (todo_graph.digest(identity["runtime"])
                         if identity["runtime"] is not None else None),
        "lineage": identity["lineage"],
        "run_id": identity["run_id"],
    })
    return evidence


def _restore_pool_wait_checkpoints(cdir, plan, running, inspection=None):
    """Restore worker wait checkpoints only after maker trust gates pass."""
    workspace_key = hashlib.sha256(
        str(Path(plan["workspace"]).resolve()).casefold().encode()
    ).hexdigest()
    workspace_lock = Path.home() / ".agents" / "todo-locks" / (workspace_key + ".lock")
    tasks = {task.get("id"): task for task in plan.get("tasks") or [] if isinstance(task, dict)}
    restored = []
    errors = {}
    with todo_graph.exclusive(workspace_lock), todo_graph.exclusive(Path(cdir) / "runner.lock"):
        state = _run_state(cdir)
        nodes = state.get("nodes") or {}
        for key in running:
            current = nodes.get(key)
            if not isinstance(current, dict) or current.get("status") != "RUNNING":
                continue
            checkpoint, error = _pool_wait_checkpoint(cdir, key)
            if error:
                errors[key] = error
                continue
            if checkpoint is None:
                continue
            folder = Path(cdir) / key
            process_evidence = todo_graph._node_process_evidence(folder)
            ok, why = todo_graph._process_evidence_ok(process_evidence)
            if not ok:
                errors[key] = why
                continue
            if checkpoint.get("pool_phase") == "reviewer":
                task = tasks.get(key)
                if task is None:
                    errors[key] = "reviewer pool-wait checkpoint has no current task definition"
                    continue
                if inspection is None:
                    try:
                        inspection = todo_graph.inspect_run(cdir, plan)
                    except (OSError, ValueError) as exc:
                        errors[key] = f"maker inspection is unavailable: {type(exc).__name__}"
                        continue
                evidence = (inspection.get("nodes") or {}).get(key)
                if not isinstance(evidence, dict):
                    errors[key] = "reviewer pool-wait checkpoint has no maker inspection evidence"
                    continue
                attempt, auth_res, auth_usage = _latest_authoritative_maker(folder)
                if not auth_res:
                    errors[key] = "missing authoritative maker result.json on disk (forged progress-only receipt)"
                    continue
                if not auth_usage:
                    errors[key] = "missing mandatory authoritative Fleet usage record on disk"
                    continue
                bound_evidence = _bind_maker_evidence(dict(evidence), attempt, auth_res, auth_usage)
                ok, why = todo_graph._trustworthy_maker(
                    auth_res, bound_evidence, plan["workspace"], task.get("scope", []),
                    task=task, task_folder=folder, run_id=Path(cdir).name,
                )
                if not ok:
                    errors[key] = "No trustworthy maker receipt: " + why
                    continue
                checkpoint = _bind_progress_maker(checkpoint, attempt, auth_res)
            # Reconcile both worker publications before exposing the verified
            # checkpoint to the scheduler; these writes are idempotent.
            _write_json(folder / "progress.json", checkpoint)
            _write_json(folder / "result.json", checkpoint)
            nodes[key] = dict(checkpoint)
            restored.append(key)
        if restored:
            _write_json(Path(cdir) / "state.json", state)
    return restored, errors


def _rehydrate_authoritative_maker_progress(cdir, inspection, running):
    """Bind progress to the newest authoritative maker without replaying it."""
    rehydrated = []
    errors = {}
    for key in running:
        folder = Path(cdir) / key
        progress_path = folder / "progress.json"
        try:
            progress = _read_json(progress_path) if progress_path.is_file() else {}
        except (OSError, ValueError) as exc:
            errors[key] = f"interrupted progress is unreadable: {type(exc).__name__}"
            continue
        if not isinstance(progress, dict):
            errors[key] = "interrupted progress is not a JSON object"
            continue
        if not isinstance(progress.get("history", []), list):
            errors[key] = "interrupted progress has invalid history"
            continue
        resolved_attempt, auth_res, _auth_usage = _latest_authoritative_maker(folder)
        if not auth_res:
            if resolved_attempt is not None:
                errors[key] = (f"interrupted maker attempt {resolved_attempt} has no authoritative "
                               "result.json; refusing fallback to an older maker")
            continue
        restored_progress = _bind_progress_maker(progress, resolved_attempt, auth_res)
        current_round = restored_progress.get("round")
        if (not isinstance(current_round, int) or isinstance(current_round, bool)
                or current_round < resolved_attempt):
            restored_progress["round"] = resolved_attempt
        restored_progress.setdefault("status", "RUNNING")
        if restored_progress == progress:
            continue
        try:
            _write_json(progress_path, restored_progress)
        except (OSError, ValueError) as exc:
            errors[key] = f"cannot restore authoritative maker progress: {type(exc).__name__}"
            continue
        rehydrated.append(key)
    return rehydrated, errors


def _bind_authoritative_maker_attempts(cdir, inspection, running):
    """Bind inspection round and identity to the retained maker attempt."""
    changed = False
    for key in running:
        evidence = (inspection.get("nodes") or {}).get(key)
        if not isinstance(evidence, dict):
            continue
        progress_path = Path(cdir) / key / "progress.json"
        try:
            progress = _read_json(progress_path) if progress_path.is_file() else {}
        except (OSError, ValueError):
            continue
        attempt = progress.get("maker_attempt") if isinstance(progress, dict) else None
        if not isinstance(attempt, int) or isinstance(attempt, bool):
            continue
        auth_res, auth_usage = todo_graph._authoritative_maker_result(Path(cdir) / key, attempt)
        if not auth_res:
            continue
        _bind_maker_evidence(evidence, attempt, auth_res, auth_usage)
        changed = True
    if changed:
        inspection["inspection_hash"] = todo_graph._inspection_hash(inspection)
    return inspection


def _recovery_call_baseline(cdir, nodes, running):
    """Count worker history before recovery, including progress not yet in state."""
    baseline = {}
    for key in running:
        node = nodes.get(key) or {}
        progress_path = Path(cdir) / key / "progress.json"
        progress = _read_json(progress_path) if progress_path.is_file() else None
        baseline[key] = _node_model_calls(progress if isinstance(progress, dict) else node)
    return baseline


def _recover_running(campaign, plan, cdir, adapter=None):
    """inspect_run + resolve_interrupted for dead-owner RUNNING nodes. Returns info dict."""
    state = _run_state(cdir)
    # todo_graph.resolve_interrupted does not compare the saved state hash,
    # so enforce the plan_hash contract here before any inspection/reuse.
    saved_hash = state.get("plan_hash")
    if saved_hash and saved_hash != todo_graph.digest(plan):
        nodes0 = state.get("nodes") or {}
        running0 = [k for k, v in nodes0.items() if isinstance(v, dict) and v.get("status") == "RUNNING"]
        return {"recovered": [], "running": running0, "model_calls": 0,
                "blocked_reason": "saved plan_hash does not match current campaign plan; "
                                  "refusing to resume interrupted work under a changed plan"}
    nodes = state.get("nodes") or {}
    running = [k for k, v in nodes.items() if isinstance(v, dict) and v.get("status") == "RUNNING"]
    if not running:
        # A crash before the first dispatch or between units leaves no
        # RUNNING node, but it can leave proven-dead runner/workspace locks
        # whose stale files would otherwise block every later resume.
        has_own_state = bool(state) or (Path(cdir) / "runner.lock").is_file()
        _retire_dead_locks(cdir, plan, allow_workspace_lock=has_own_state)
        return {"recovered": [], "running": [], "model_calls": 0}
    inspection = todo_graph.inspect_run(cdir, plan)
    evidence_error = _interrupted_evidence_error(inspection, running)
    if evidence_error:
        return {"recovered": [], "running": running, "model_calls": 0,
                "blocked_reason": evidence_error}
    # Evidence gates proved the lock holders dead, but todo_graph's
    # exclusive() uses O_EXCL and would refuse the stale files. Retire them
    # by retained rename (never delete) before delegating.
    _retire_dead_locks(cdir, plan)
    restored, checkpoint_errors = _restore_pool_wait_checkpoints(
        cdir, plan, running, inspection=inspection
    )
    after_restore = _run_state(cdir).get("nodes") or {}
    remaining = [
        k for k, v in after_restore.items()
        if isinstance(v, dict) and v.get("status") == "RUNNING"
    ]
    if checkpoint_errors:
        reason = "; ".join(f"{key}: {checkpoint_errors[key]}" for key in sorted(checkpoint_errors))
        return {"recovered": [], "running": remaining, "model_calls": 0,
                "pool_wait_restored": restored, "blocked_reason": reason,
                "isolation_safe": True}
    if not remaining:
        return {"recovered": [], "running": [], "model_calls": 0,
                "pool_wait_restored": restored}

    rehydrated, rehydrate_errors = _rehydrate_authoritative_maker_progress(
        cdir, inspection, remaining
    )
    if rehydrate_errors:
        reason = "; ".join(f"{key}: {rehydrate_errors[key]}" for key in sorted(rehydrate_errors))
        return {"recovered": [], "running": remaining, "model_calls": 0,
                "pool_wait_restored": restored, "progress_rehydrated": rehydrated,
                "blocked_reason": reason, "isolation_safe": True}

    # The first inspection also covered nodes just restored to pool-wait.
    # Rebind candidate recovery to the remaining RUNNING nodes and current state.
    inspection = todo_graph.inspect_run(cdir, plan)
    _bind_authoritative_maker_attempts(cdir, inspection, remaining)
    evidence_error = _interrupted_evidence_error(inspection, remaining)
    if evidence_error:
        return {"recovered": [], "running": remaining, "model_calls": 0,
                "pool_wait_restored": restored, "blocked_reason": evidence_error}
    _retire_dead_locks(cdir, plan)
    before_resolve = _recovery_call_baseline(
        cdir, _run_state(cdir).get("nodes") or {}, remaining
    )
    result = todo_graph.resolve_interrupted(cdir, plan, inspection, adapter=adapter)
    after = _run_state(cdir).get("nodes") or {}
    recovery_calls = sum(
        max(0, _node_model_calls(after.get(key)) - before_resolve.get(key, 0))
        for key in remaining
    )
    still = [k for k, v in after.items() if isinstance(v, dict) and v.get("status") == "RUNNING"]
    resolve_nodes = result.get("nodes") if isinstance(result, dict) else {}
    isolation_safe = not (isinstance(result, dict) and result.get("reason"))
    for key in still:
        resolved = resolve_nodes.get(key) if isinstance(resolve_nodes, dict) else None
        reason = str(resolved.get("reason") or "") if isinstance(resolved, dict) else ""
        if (not isinstance(resolved, dict)
                or resolved.get("original_status") != "RUNNING"
                or resolved.get("state_preserved") is not True
                or reason.startswith(("Process evidence refuses recovery",
                                      "Lock evidence refuses recovery"))):
            isolation_safe = False
    return {"recovered": result.get("recovered", []) if isinstance(result, dict) else [],
            "running": still, "model_calls": recovery_calls,
            "pool_wait_restored": restored, "progress_rehydrated": rehydrated,
            "isolation_safe": isolation_safe, "resolve_result": result}


def _recovery_conflicts(plan, interrupted):
    """Units that must not run while an interrupted unit remains unresolved."""
    tasks = {task["id"]: task for task in plan.get("tasks") or []}
    roots = set(interrupted)
    conflicts = set(roots)
    for uid, task in tasks.items():
        if uid in roots:
            continue
        if any(root in tasks and todo_graph.overlaps(task, tasks[root]) for root in roots):
            conflicts.add(uid)
    return conflicts


def _park_recovery_holds(cdir, plan, interrupted):
    """Isolate unresolved RUNNING nodes and conflicting work in state.json.

    WAITING_APPROVAL is left untouched by todo_graph. The complete original node
    stays inside a durable marker so a crash during delegation can be undone on
    the next supervisor pass without replaying or inventing maker evidence.
    """
    state = _run_state(cdir)
    nodes = state.get("nodes") or {}
    conflicts = _recovery_conflicts(plan, interrupted)
    roots = set(interrupted)
    parked = []
    for task in plan.get("tasks") or []:
        uid = task["id"]
        if uid not in conflicts:
            continue
        current = nodes.get(uid)
        if uid in roots:
            if not isinstance(current, dict) or current.get("status") != "RUNNING":
                continue
        elif (isinstance(current, dict) and current.get("status") in {
                "ACCEPTED", "CANCELLED", "BLOCKED", "WAITING_APPROVAL"}):
            continue
        original = dict(current) if isinstance(current, dict) else None
        held = dict(original) if original is not None else {}
        held["status"] = "WAITING_APPROVAL"
        held["reason"] = "DURABLE_RECOVERY_HOLD"
        held["durable_recovery_hold"] = {
            "original_absent": original is None,
            "original_node": original,
            "interrupted": uid in roots,
            "created_at": _now(),
        }
        nodes[uid] = held
        parked.append(uid)
    if parked:
        state["nodes"] = nodes
        _write_json(Path(cdir) / "state.json", state)
    return parked


def _restore_recovery_holds(cdir):
    """Restore every durable recovery hold before recovery or after delegation."""
    state = _run_state(cdir)
    nodes = state.get("nodes") or {}
    restores = []
    for uid, node in nodes.items():
        if not isinstance(node, dict) or "durable_recovery_hold" not in node:
            continue
        marker = node.get("durable_recovery_hold")
        if not isinstance(marker, dict):
            raise ValueError(f"durable recovery hold is malformed for unit {uid}")
        original = marker.get("original_node")
        if marker.get("original_absent") is True and original is None:
            restores.append((uid, None))
        elif marker.get("original_absent") is False and isinstance(original, dict):
            restores.append((uid, original))
        else:
            raise ValueError(f"durable recovery hold has no restorable original for unit {uid}")
    for uid, original in restores:
        if original is None:
            nodes.pop(uid, None)
        else:
            nodes[uid] = original
    if restores:
        state["nodes"] = nodes
        _write_json(Path(cdir) / "state.json", state)
    return [uid for uid, _original in restores]


def _execute(campaign, cdir, adapter=None, resume=False):
    validate_campaign(campaign)
    plan = campaign_to_plan(campaign)
    cdir = _canonical_dir(cdir)
    # Reject before ownership is claimed or any model is called: fleet-native
    # execution must keep evidence outside the execution workspace.
    _enforce_evidence_outside_workspace(campaign, plan, cdir, adapter=adapter)
    with _pool_guard_env(campaign, adapter):
        return _execute_owned(campaign, plan, cdir, adapter=adapter)


def _execute_owned(campaign, plan, cdir, adapter=None):
    cdir.mkdir(parents=True, exist_ok=True)
    durable = _claim_owner(cdir, campaign, status="RUNNING")
    # Claim every unit's dedupe_key before any maker can run. A repeated
    # delivery of the same payload is a duplicate; the same key with a
    # different payload is rejected before work or a result is replayed.
    try:
        _register_dedupe_events(campaign, cdir)
    except ValueError as exc:
        # Per-attempt count is 0 (no new call was made), but the durable
        # cumulative count must keep the campaign's prior consumption.
        preserved_calls = max(int(durable.get("model_calls") or 0),
                              _model_calls(_run_state(cdir).get("nodes") or {}))
        out = {"status": "BLOCKED", "reason": str(exc),
               "campaign_id": campaign["campaign_id"], "model_calls": 0,
               "campaign_model_calls": preserved_calls, "units": {}}
        _save_durable(cdir, durable, status="BLOCKED", model_calls=preserved_calls, units={})
        return out
    # A supervisor crash during delegation can leave durable holds behind.
    # Restore them before inspect_run/resolve_interrupted so recovery sees the
    # original RUNNING nodes rather than a scheduler-safe parking status.
    _restore_recovery_holds(cdir)

    # Cancels requested while a previous owner was active are honoured by
    # this owner before any new model call is made.
    pending_cancels = _pending_cancel_requests(cdir, campaign)
    if pending_cancels:
        return _apply_cancel_requests(campaign, plan, cdir, durable, pending_cancels, 0)

    pre_state = _run_state(cdir)
    if pre_state:
        pre_integrity = _verify_terminal_integrity(plan, pre_state)
        if pre_integrity:
            pre_nodes = pre_state.get("nodes") or {}
            out = {"status": "BLOCKED", "reason": pre_integrity,
                   "campaign_id": campaign["campaign_id"], "model_calls": 0,
                   "campaign_model_calls": _model_calls(pre_nodes),
                   "units": {k: v.get("status") for k, v in pre_nodes.items()}}
            _save_durable(cdir, durable, status="BLOCKED", model_calls=_model_calls(pre_nodes), units=out["units"])
            return out

    try:
        recovery = _recover_running(campaign, plan, cdir, adapter=adapter)
    except FileExistsError:
        out = {"status": "BLOCKED", "reason": "Run lock held by a live owner",
               "campaign_id": campaign["campaign_id"], "model_calls": 0, "units": {}}
        _save_durable(cdir, durable, status="BLOCKED", model_calls=0, units={})
        return out
    # Restore any leftover park markers BEFORE readiness: a crash between
    # park and restore leaves WAITING_APPROVAL nodes that todo_graph.run
    # skips, and once next_check_at expires _cooled_units would no longer
    # see them, leaving the unit stuck forever.
    _restore_leftover_parked(cdir)
    state = _run_state(cdir)
    nodes = state.get("nodes") or {}
    # Only invocations made by resolve_interrupted itself are recovery calls;
    # restored worker history belongs to the previous invocation.
    recovery_calls = max(0, int(recovery.get("model_calls") or 0))
    execution_baseline_calls = _model_calls(nodes)
    waiting, _next_check = _pool_wait_pending(nodes)
    cooled = _cooled_units(nodes)

    unresolved = list(recovery.get("running") or [])
    if (unresolved or recovery.get("blocked_reason")) and not recovery.get("isolation_safe"):
        reason = recovery.get("blocked_reason") or "Interrupted RUNNING nodes could not be safely resolved"
        out = {"status": "BLOCKED", "reason": reason,
               "campaign_id": campaign["campaign_id"], "model_calls": recovery_calls,
               "campaign_model_calls": _model_calls(nodes),
               "recovery": recovery, "units": {k: v.get("status") for k, v in nodes.items()}}
        _save_durable(cdir, durable, status="BLOCKED", model_calls=_model_calls(nodes), units=out["units"])
        return out
    recovery_conflicts = _recovery_conflicts(plan, unresolved) if unresolved else set()

    units = campaign.get("units") or []
    ready = []
    for unit in units:
        uid = _unit_id(unit)
        node = nodes.get(uid) or {}
        if node.get("status") in ("ACCEPTED", "CANCELLED"):
            continue
        if uid in cooled:
            continue  # this unit's own pool cooldown has not expired yet
        if uid in recovery_conflicts:
            continue  # unresolved interrupted work or overlapping scope/resources
        deps = unit.get("depends_on") or unit.get("dependsOn") or unit.get("deps") or []
        if all((nodes.get(d) or {}).get("status") == "ACCEPTED" for d in deps):
            ready.append(uid)
    if not ready:
        if unresolved:
            status = "BLOCKED"
        elif units and all((nodes.get(_unit_id(u)) or {}).get("status") in ("ACCEPTED", "CANCELLED") for u in units):
            status = "ACCEPTED" if all((nodes.get(_unit_id(u)) or {}).get("status") == "ACCEPTED" for u in units) else "COMPLETED"
            # Fast path must not republish a stale run as accepted: apply the
            # same plan_hash / artifact integrity gates todo_graph.run uses.
            integrity_error = _verify_terminal_integrity(plan, state)
            if integrity_error:
                out = {"status": "BLOCKED", "reason": integrity_error,
                       "campaign_id": campaign["campaign_id"], "model_calls": 0,
                       "campaign_model_calls": _model_calls(nodes),
                       "units": {k: v.get("status") for k, v in nodes.items()}}
                _save_durable(cdir, durable, status="BLOCKED", model_calls=0, units=out["units"])
                return out
        elif waiting:
            status = "RESUMABLE_POOL_WAIT"
        else:
            status = "EMPTY"
        _publish_results(campaign, cdir, nodes)
        out = {"status": status, "campaign_id": campaign["campaign_id"],
               "model_calls": recovery_calls,
               "campaign_model_calls": _model_calls(nodes),
               "units": {k: v.get("status") for k, v in nodes.items()}}
        if unresolved:
            out["reason"] = "Interrupted RUNNING nodes could not be safely resolved"
            out["recovery"] = recovery
        if waiting:
            out["next_check_at"] = _next_check
        _save_durable(cdir, durable, status=status, model_calls=_model_calls(nodes), units=out["units"],
                      next_check_at=_next_check if waiting else None)
        return out

    # Per-unit cooldown: todo_graph.run clears every RESUMABLE_POOL_WAIT
    # before rerunning, so park each cooled unit (WAITING_APPROVAL +
    # durable_parked_pool_wait) while independent ready units execute, then
    # restore the original wait state. Dependents stay blocked because the
    # parked unit is not ACCEPTED.
    parked = _park_cooled_units(cdir, list(cooled)) if cooled else {}
    recovery_parked = _park_recovery_holds(cdir, plan, unresolved) if unresolved else []

    def restore_delegation_parks(current_nodes):
        if recovery_parked:
            _restore_recovery_holds(cdir)
        if parked:
            _restore_parked_units(cdir, parked, current_nodes)
        if recovery_parked or parked:
            return _run_state(cdir).get("nodes") or current_nodes
        return current_nodes

    # Delegate scheduling/execution to todo_graph (single scheduler/state registry).
    # Non-native (injected/test) adapters use todo_graph's explicit legacy-test
    # context, exactly as todo_graph's own test plans do; live Fleet execution
    # (adapter=None or FleetAdapter) keeps the native historical-scout policy.
    if adapter is not None and "historical_scout" not in plan:
        _fleet_cls = getattr(todo_graph, "FleetAdapter", None)
        _is_fleet = _fleet_cls is not None and isinstance(adapter, _fleet_cls)
        if not _is_fleet and not getattr(adapter, "native_historical_scout", False):
            plan["execution_context"] = "legacy-test"
    state_file = Path(cdir) / "state.json"
    if not state_file.is_file() and "historical_scout" not in plan and plan.get("execution_context") == "legacy-test":
        _write_json(state_file, {"plan_hash": todo_graph.digest(plan), "nodes": {}, "version": todo_graph.VERSION})
    try:
        result = todo_graph.run(plan, cdir, adapter=adapter)
    except FileExistsError:
        nodes = restore_delegation_parks(_run_state(cdir).get("nodes") or {})
        out = {"status": "BLOCKED", "reason": "Run lock held by a live owner",
               "campaign_id": campaign["campaign_id"], "model_calls": recovery_calls,
               "campaign_model_calls": _model_calls(nodes),
               "units": {k: v.get("status") for k, v in nodes.items()}}
        _save_durable(cdir, durable, status="BLOCKED", model_calls=_model_calls(nodes), units=out["units"])
        return out
    except BaseException:
        restore_delegation_parks(_run_state(cdir).get("nodes") or {})
        raise
    nodes = restore_delegation_parks(dict(result.get("nodes") or {}))
    # A cancel requested while this owner was running is applied now, while
    # ownership is still held, so no later worker result overwrites it.
    pending_cancels = _pending_cancel_requests(cdir, campaign)
    if pending_cancels:
        run_calls = recovery_calls + max(0, _model_calls(nodes) - execution_baseline_calls)
        return _apply_cancel_requests(campaign, plan, cdir, durable, pending_cancels, run_calls)
    _publish_results(campaign, cdir, nodes)
    run_calls = recovery_calls + max(0, _model_calls(nodes) - execution_baseline_calls)
    campaign_calls = _model_calls(nodes)
    w, nxt = _pool_wait_pending(nodes)
    status = result.get("status") or "BLOCKED"
    if unresolved:
        status = "BLOCKED"
    elif w and status == "BLOCKED":
        if all(isinstance(v, dict) and v.get("status") in (
                "ACCEPTED", "CANCELLED", "RESUMABLE_POOL_WAIT", "DEPENDENCY_BLOCKED")
               for v in nodes.values()):
            status = "RESUMABLE_POOL_WAIT"
    out = {"status": status, "campaign_id": campaign["campaign_id"],
           "model_calls": run_calls, "campaign_model_calls": campaign_calls,
           "units": {k: v.get("status") for k, v in nodes.items()}, "result": result}
    if unresolved:
        out["reason"] = "Interrupted RUNNING nodes could not be safely resolved; independent units were delegated"
        out["recovery"] = recovery
    if w:
        out["next_check_at"] = nxt
    _save_durable(cdir, durable, status=status, model_calls=campaign_calls, units=out["units"],
                  next_check_at=nxt if w else None)
    return out


def run_campaign(campaign, campaign_dir=None, adapter=None, campaign_path=None):
    data, cdir = _resolve_paths(campaign, campaign_path, campaign_dir)
    return _execute(data, cdir, adapter=adapter, resume=False)


def resume_campaign(campaign, campaign_dir=None, adapter=None, campaign_path=None):
    data, cdir = _resolve_paths(campaign, campaign_path, campaign_dir)
    return _execute(data, cdir, adapter=adapter, resume=True)


resume = resume_campaign
run = run_campaign


def cancel_campaign(campaign, campaign_dir=None, unit_ids=None, campaign_path=None):
    data, cdir = _resolve_paths(campaign, campaign_path, campaign_dir)
    plan = campaign_to_plan(data)
    cdir = _canonical_dir(cdir)
    cdir.mkdir(parents=True, exist_ok=True)
    durable = _read_durable(cdir) or {"schema_version": DURABLE_STATE_SCHEMA,
                                      "campaign_id": data["campaign_id"], "generation": 0}
    known = {_unit_id(u) for u in data.get("units") or []}
    if unit_ids is not None:
        unknown = [u for u in unit_ids if u not in known]
        if unknown:
            raise ValueError(f"cancel requested for unknown units: {unknown}")
    if durable.get("pid") not in (None, os.getpid()) and _owner_alive(durable) is not False:
        # A live (or unverifiable) owner must apply the cancel itself so an
        # in-flight worker result cannot overwrite CANCELLED afterwards.
        # Persist a durable request; the owner honours it at the start of the
        # next pass and again right after todo_graph.run returns.
        # One durable file per request (cancel-request.<uuid>.json) so the
        # live owner picks it up via _pending_cancel_requests and concurrent
        # cancels never overwrite each other.
        _write_cancel_request(cdir, data, unit_ids)
        return {"status": "CANCEL_REQUESTED", "campaign_id": data["campaign_id"],
                "model_calls": 0,
                "units": {k: v.get("status") for k, v in (_run_state(cdir).get("nodes") or {}).items()}}
    # Atomically claim ownership BEFORE any mutation (increments generation
    # under the owner lock and re-checks liveness), so a concurrent run cannot
    # take over between the earlier owner read and these writes. A cancel that
    # mutated state without ownership could not be rolled back by a later
    # claim refusal, and the scheduler could overwrite CANCELLED with its
    # own result.
    durable = _claim_owner(cdir, data, status="RUNNING")
    try:
        # Propagate scheduler errors (unknown --unit, plan_hash conflict, ...):
        # reporting CANCELLED without a persisted scheduler state.json would let a
        # later resume execute a unit the user believes is cancelled.
        state = todo_graph.cancel_tasks(cdir, plan, task_ids=unit_ids)
        nodes = state.get("nodes") or {}
        persisted = _run_state(cdir).get("nodes") or {}
        requested = unit_ids if unit_ids is not None else list(known)
        for uid in requested:
            if (persisted.get(uid) or {}).get("status") != "CANCELLED":
                raise ValueError(f"cancel was not persisted for unit {uid}")
        _publish_results(data, cdir, nodes)
    except BaseException:
        try:
            _save_durable(cdir, durable, status="BLOCKED",
                          units={k: v.get("status") for k, v in (_run_state(cdir).get("nodes") or {}).items()})
        except (ValueError, OSError):
            pass  # keep the original cancel error; a dead owner is reclaimed on resume
        raise
    # Cancellation makes no new model call, but it must not erase
    # consumption already evidenced by node history or prior durable state.
    campaign_calls = max(int(durable.get("model_calls") or 0),
                         _model_calls(nodes),
                         _model_calls(persisted))
    _save_durable(cdir, durable, status="CANCELLED", model_calls=campaign_calls,
                  units={k: v.get("status") for k, v in nodes.items()})
    return {"status": "CANCELLED", "campaign_id": data["campaign_id"],
            "model_calls": campaign_calls, "campaign_model_calls": campaign_calls,
            "units": {k: v.get("status") for k, v in nodes.items()}}


cancel = cancel_campaign


def campaign_status(campaign=None, campaign_dir=None, campaign_path=None):
    if campaign is None and campaign_path is None and campaign_dir is None:
        raise ValueError("campaign status requires a campaign path or directory")
    if campaign is None:
        src = campaign_path if campaign_path is not None else campaign_dir
        data, cdir = _resolve_paths(src, None, None)
    else:
        data, cdir = _resolve_paths(campaign, campaign_path, campaign_dir)
    durable = _read_durable(cdir)
    state = _run_state(cdir)
    nodes = state.get("nodes") or {}
    waiting, _nxt = _pool_wait_pending(nodes)
    status = durable.get("status") or state.get("status") or "UNKNOWN"
    if waiting and status not in ("ACCEPTED", "CANCELLED"):
        if status != "BLOCKED" or all(
            isinstance(v, dict) and v.get("status") in (
                "ACCEPTED", "CANCELLED", "RESUMABLE_POOL_WAIT", "DEPENDENCY_BLOCKED")
            for v in nodes.values()
        ):
            status = "RESUMABLE_POOL_WAIT"
    next_check_at = None
    if status == "RESUMABLE_POOL_WAIT":
        next_check_at = _nxt if _nxt is not None else durable.get("next_check_at")
    stored_calls = durable.get("model_calls")
    return {"status": status, "campaign_id": data.get("campaign_id"),
            "model_calls": max(int(stored_calls or 0), _model_calls(nodes)),
            "generation": durable.get("generation"), "pid": durable.get("pid"),
            "started_at": durable.get("started_at"),
            "next_check_at": next_check_at,
            "units": {k: v.get("status") for k, v in nodes.items()},
            "durable_state": durable}


status_campaign = campaign_status


def _find_hidden_launch():
    found = shutil.which("HiddenLaunch.exe") or shutil.which("HiddenLaunch")
    if found:
        return found
    canonical = Path.home() / ".agents" / "runtime" / "hidden-launch" / "1.0.0" / "HiddenLaunch.exe"
    if canonical.is_file():
        return str(canonical)
    here = Path(__file__).resolve()
    for parent in [here.parent, *here.parents[:4]]:
        candidate = parent / "HiddenLaunch.exe"
        if candidate.is_file():
            return str(candidate)
    return None


def spawn_detached(campaign, campaign_dir=None, action="resume", campaign_path=None):
    """Launch run/resume windowlessly (HiddenLaunch.exe or CREATE_NO_WINDOW)."""
    data, cdir = _resolve_paths(campaign, campaign_path, campaign_dir)
    cdir = _canonical_dir(cdir)
    # The child starts with cwd=campaign_dir; persist the workspace resolved in
    # the parent so a relative campaign cannot silently select another checkout.
    data = dict(data)
    data["workspace"] = str(_canonical_dir(data["workspace"]))
    validate_campaign(data)
    # The detached child runs fleet-native (adapter=None): reject here, before
    # a process, spec or log file is created, what the child would reject.
    _enforce_evidence_outside_workspace(data, campaign_to_plan(data), cdir)
    campaign_file = cdir / "campaign.json"
    if campaign_file.is_file():
        # Never launch whatever campaign.json happens to be on disk while
        # reporting the passed campaign_id: reject any conflict before a
        # process, spec or log file is created.
        try:
            existing = _read_json(campaign_file)
        except Exception as exc:
            raise ValueError(f"existing campaign.json is unreadable: {exc}") from exc
        if not isinstance(existing, dict) or existing.get("campaign_id") != data.get("campaign_id"):
            raise ValueError("campaign.json conflict: existing campaign_id "
                             f"{(existing or {}).get('campaign_id')!r} != requested "
                             f"{data.get('campaign_id')!r}")
        existing_norm = dict(existing)
        if isinstance(existing_norm.get("workspace"), str) and existing_norm["workspace"].strip():
            existing_norm["workspace"] = str(_canonical_dir(existing_norm["workspace"]))
        if existing_norm != data:
            raise ValueError("campaign.json conflict: existing campaign content "
                             "differs from the requested campaign")
    cdir.mkdir(parents=True, exist_ok=True)
    _write_json(campaign_file, data)
    argv = [sys.executable, "-B", str(Path(__file__).resolve()), action,
            "--campaign", str(campaign_file), "--campaign-dir", str(cdir)]
    hidden = _find_hidden_launch()
    log_path = cdir / "supervisor.log"
    if hidden:
        # HiddenLaunch.exe takes exactly one argument: an XML spec file
        # (<hidden-launch version="1" log-directory=...><action .../></hidden-launch>),
        # waits for the child and writes <runId>.status / <runId>-0.log there.
        from xml.sax.saxutils import quoteattr

        spec_path = cdir / "hidden-launch.xml"
        spec = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            f"<hidden-launch version=\"1\" isolate-desktop=\"true\" log-directory={quoteattr(str(cdir))}>"
            f"<action executable={quoteattr(argv[0])} "
            f"arguments={quoteattr(subprocess.list2cmdline(argv[1:]))} "
            f"directory={quoteattr(str(cdir))}/>"
            "</hidden-launch>\n"
        )
        spec_path.write_text(spec, encoding="utf-8")
        proc = subprocess.Popen([hidden, str(spec_path)], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        flags = 0
        for name in ("CREATE_NO_WINDOW", "DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP"):
            flags |= getattr(subprocess, name, 0)
        with log_path.open("ab") as log:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                    cwd=str(cdir), creationflags=flags, close_fds=True)
    return {"status": "SPAWNED", "campaign_id": data["campaign_id"], "pid": proc.pid,
            "action": action, "model_calls": 0, "launcher": "HiddenLaunch.exe" if hidden else "CREATE_NO_WINDOW"}


def _campaign_arg(args):
    src = args.campaign or args.campaign_dir or args.path
    if not src:
        raise ValueError("provide a campaign file/directory via positional path, --campaign, or --campaign-dir")
    return src


def main(argv=None):
    parser = argparse.ArgumentParser(description="Durable arbor-campaign/v1 supervisor over todo_graph")
    parser.add_argument("action", choices=["validate-campaign", "run", "resume", "cancel", "status", "spawn-detached"])
    parser.add_argument("path", nargs="?", help="Campaign file or campaign directory")
    parser.add_argument("--campaign", help="Path to campaign.json (or campaign directory)")
    parser.add_argument("--campaign-dir", help="Campaign directory holding campaign.json and durable-state.json")
    parser.add_argument("--unit", action="append", default=[], help="Unit id for cancel (repeatable)")
    parser.add_argument("--spawn-action", choices=["run", "resume"], default="resume",
                        help="Action for spawn-detached to launch")
    args = parser.parse_args(argv)
    try:
        src = _campaign_arg(args)
        if args.action == "validate-campaign":
            data, _cdir = _resolve_paths(src, None, args.campaign_dir)
            # Public validation must prove the campaign is executable and fully
            # lockbox-isolated the same way run/resume do: translate to the
            # todo_graph plan (rejects empty unit scope via todo_graph.validate
            # and lockbox_ref embedded in maker-visible task text via
            # _literal_lockbox_leak), not just the campaign document shape.
            plan = campaign_to_plan(data)
            # Mirror todo_graph.run's new-run gate when historical_scout is
            # declared or execution_context is not legacy-test.
            if (plan.get("tasks") and not (Path(_cdir) / "state.json").exists()
                    and (data.get("historical_scout") is not None
                         or plan.get("execution_context") != "legacy-test")):
                history_scout.enforce_new_run_scout_policy(plan)
            # Parity with run/resume: a VALID campaign must survive the same
            # pre-model gates todo_graph.run applies to fleet-native execution -
            # the evidence location guard and FleetAdapter.validate_plan, which
            # checks every candidate against real Fleet profiles (agent exists,
            # supported_modes covers the lane mode). Without these, a campaign
            # with an unknown agent or unsupported mode reported VALID here but
            # was rejected by run/resume before execution.
            _enforce_evidence_outside_workspace(data, plan, _cdir)
            todo_graph.FleetAdapter().validate_plan(plan)
            result = {"status": "VALID", "valid": True, "campaign_id": data["campaign_id"],
                      "units": len(data.get("units") or []), "tasks": len(plan.get("tasks") or []),
                      "model_calls": 0}
        elif args.action == "run":
            result = run_campaign(src, campaign_dir=args.campaign_dir)
        elif args.action == "resume":
            result = resume_campaign(src, campaign_dir=args.campaign_dir)
        elif args.action == "cancel":
            result = cancel_campaign(src, campaign_dir=args.campaign_dir,
                                     unit_ids=args.unit or None)
        elif args.action == "status":
            result = campaign_status(src, campaign_dir=args.campaign_dir)
        elif args.action == "spawn-detached":
            result = spawn_detached(src, campaign_dir=args.campaign_dir, action=args.spawn_action)
        else:  # pragma: no cover
            result = {"status": "BLOCKED", "reason": "unknown action"}
    except ValueError as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc), "model_calls": 0}, ensure_ascii=False))
        return 3
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("status") not in {"BLOCKED"} else 3


if __name__ == "__main__":
    raise SystemExit(main())
