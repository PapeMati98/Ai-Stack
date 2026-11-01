#!/usr/bin/env python3
"""Durable local Office Loop ledger; supplied evidence never grants authority.

CLI: loop_ledger.py <cmd> [--root <absolute home>] [--goal-id <id>] ...
Home defaults to OFFICE_LOOP_HOME or ~/.agents/office-loop. Each goal has
its own subdirectory; --goal-id is optional in a home containing one goal.
"""
from __future__ import annotations
import sys
sys.dont_write_bytecode = True

import argparse
import contextlib
import fnmatch
import hashlib
import json
import math
import os
import re
import stat
import threading
import time
import uuid
from datetime import datetime
from functools import lru_cache
from pathlib import Path

try:
    from . import loop_guard as guard
except ImportError:
    # Explicit sibling loading also works with python -I and importlib callers;
    # do not import an unrelated module from the current working directory.
    import importlib.util
    _guard_spec = importlib.util.spec_from_file_location(
        "office_loop_ledger_guard", Path(__file__).with_name("loop_guard.py"))
    guard = importlib.util.module_from_spec(_guard_spec)
    _guard_spec.loader.exec_module(guard)

if os.name == "nt":
    import msvcrt
else:
    import fcntl

MAX_INPUT_BYTES = 64 * 1024
MAX_STATE_BYTES = 4 * 1024 * 1024
MAX_SNAPSHOT_FILES = 10000
MAX_SNAPSHOT_FILE_BYTES = 50 * 1024 * 1024
MAX_ENTRIES = 256
IGNORED_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv"}
SKILL_TREE = Path(__file__).resolve().parents[2]
THREAD_LOCK = threading.RLock()
COMMANDS = ("init", "acquire", "renew", "release", "reconcile", "review",
            "invalidate", "prepare", "dispatching", "delivered", "completed",
            "stop", "status", "observation", "compact")


class LedgerError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def fail(code, message):
    raise LedgerError(code, message)


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def obj(value, keys, label):
    if not isinstance(value, dict) or set(value) - set(keys):
        fail("INVALID_INPUT", label + " must contain only allowed fields")
    return value


def text(value, label, maximum=8000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        fail("INVALID_INPUT", label + " must be nonempty bounded text")
    return value


def boolean(value, label):
    if type(value) is not bool:
        fail("INVALID_INPUT", label + " must be boolean")
    return value


def number(value, label, minimum=0, maximum=None, integer=False):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or value < minimum or (maximum is not None and value > maximum)
            or (integer and type(value) is not int)):
        fail("INVALID_INPUT", label + " must be a finite number in range")
    return value


def digest(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        fail("INVALID_INPUT", label + " must be a SHA256 hex digest")
    return value


def unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            fail("INVALID_JSON", "duplicate JSON key")
        result[key] = value
    return result


def decode(raw):
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique_json,
                          parse_constant=lambda _: fail("INVALID_JSON", "nonfinite JSON value"))
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        fail("INVALID_JSON", "input must be bounded UTF-8 JSON")


def read_bytes(path, maximum=MAX_INPUT_BYTES):
    with Path(path).open("rb") as handle:
        if os.fstat(handle.fileno()).st_size > maximum:
            fail("INPUT_TOO_LARGE", "input exceeds byte limit")
        raw = handle.read(maximum + 1)
    if len(raw) > maximum:
        fail("INPUT_TOO_LARGE", "input exceeds byte limit")
    return raw


def read_json(path, maximum=MAX_INPUT_BYTES):
    return decode(read_bytes(path, maximum))


def inside(path, base):
    return path == base or base in path.parents


def goal_id(value):
    reserved = {"CON", "PRN", "AUX", "NUL"} | {"COM" + str(i) for i in range(1, 10)} | {"LPT" + str(i) for i in range(1, 10)}
    if (not isinstance(value, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value)
            or value.endswith(".") or value.split(".")[0].upper() in reserved):
        fail("INVALID_GOAL_ID", "goal id must be a safe directory and marker component")
    return value


def linked(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def location(root, gid=None, goal=None):
    """Fence every destination before mkdir, lock creation or replacement."""
    if not root.is_absolute():
        fail("STATE_LOCATION_INVALID", "state root must be absolute")
    paths = [root, root / ".ledger.lock"]
    if gid is not None:
        directory = root / goal_id(gid)
        paths += [directory, directory / "state.json", directory / "state.json.tmp"]
    forbidden = [SKILL_TREE, SKILL_TREE.resolve()]
    if goal:
        workspace = Path(goal["workspace"])
        forbidden += [workspace.absolute(), workspace.resolve()]
    for path in paths:
        for candidate in (path.absolute(), path.resolve()):
            if any(inside(candidate, boundary) for boundary in forbidden):
                fail("STATE_LOCATION_INVALID", "state must be outside workspace and skill trees")
        if (path.exists() or path.is_symlink()) and linked(path):
            fail("STATE_LOCATION_INVALID", "state destinations cannot be links or junctions")


@contextlib.contextmanager
def exclusive(root, create=True):
    """Persistent file lock; the OS releases ownership after a process crash."""
    with THREAD_LOCK:
        if create:
            root.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | (os.O_CREAT if create else 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(root / ".ledger.lock", flags, 0o600)
        with os.fdopen(fd, "r+b") as handle:
            if os.fstat(fd).st_size == 0:
                if not create:
                    fail("STATE_INVALID", "existing ledger lock is empty")
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)


def atomic_json(path, value):
    raw = canonical(value)
    if len(raw) > MAX_STATE_BYTES:
        fail("STATE_TOO_LARGE", "state exceeds byte limit")
    temp = path.with_name(path.name + ".tmp")
    if any(p.is_symlink() or (p.exists() and linked(p)) for p in (path, temp)):
        fail("STATE_LOCATION_INVALID", "atomic write destinations cannot be links")
    with temp.open("wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def config(value):
    value = obj(value, {"watch", "max_batches", "max_hours", "lease_minutes"}, "config")
    result = {"watch": value.get("watch", ["**/*"]), "lease_minutes": value.get("lease_minutes", 30)}
    patterns = result["watch"]
    if not isinstance(patterns, list) or not patterns or len(patterns) > 128:
        fail("INVALID_CONFIG", "watch must be a nonempty bounded list")
    for pattern in patterns:
        text(pattern, "watch pattern", 512)
        if ("\\" in pattern or ":" in pattern or pattern.startswith("/")
                or any(p in {"", ".", ".."} for p in pattern.split("/"))):
            fail("INVALID_CONFIG", "watch patterns must be relative globs")
    number(result["lease_minutes"], "lease_minutes", minimum=0.001, maximum=60)
    if "max_batches" in value:
        result["max_batches"] = number(value["max_batches"], "max_batches", integer=True)
    if "max_hours" in value:
        result["max_hours"] = number(value["max_hours"], "max_hours")
    return result


def glob_match(relative, pattern):
    """** matches zero or more path segments, including root-level files."""
    parts, globs = relative.split("/"), pattern.split("/")

    @lru_cache(None)
    def match(i, j):
        if j == len(globs):
            return i == len(parts)
        if globs[j] == "**":
            return match(i, j + 1) or (i < len(parts) and match(i + 1, j))
        return (i < len(parts) and fnmatch.fnmatchcase(parts[i], globs[j]) and match(i + 1, j + 1))
    return match(0, 0)


def fingerprint(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def file_hash(path):
    before = path.lstat()
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    result, size = hashlib.sha256(), 0
    with os.fdopen(fd, "rb") as handle:
        opened = os.fstat(handle.fileno())
        # On Windows Python versions, path stat and fstat can expose different
        # meanings of ctime (creation versus metadata change). Compare ctime
        # only between observations made through the SAME API.
        if not stat.S_ISREG(opened.st_mode) or fingerprint(before)[:4] != fingerprint(opened)[:4]:
            fail("FILE_CHANGED", "watched file changed while opening")
        if opened.st_size > MAX_SNAPSHOT_FILE_BYTES:
            fail("SNAPSHOT_LIMIT", "watched file exceeds 50 MB")
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_SNAPSHOT_FILE_BYTES:
                fail("SNAPSHOT_LIMIT", "watched file exceeds 50 MB")
            result.update(chunk)
        after = os.fstat(handle.fileno())
    if (size != before.st_size or fingerprint(opened) != fingerprint(after)
            or fingerprint(before) != fingerprint(path.lstat()) or linked(path)):
        fail("FILE_CHANGED", "watched file changed during hashing")
    return result.hexdigest()


def snapshot(state):
    workspace, rows = Path(state["goal"]["workspace"]), []

    def walk_error(_):
        fail("SNAPSHOT_UNREADABLE", "cannot traverse watched workspace")

    for directory, dirs, files in os.walk(workspace, followlinks=False, onerror=walk_error):
        base = Path(directory)
        dirs[:] = sorted(d for d in dirs if d not in IGNORED_DIRS and not linked(base / d))
        for name in sorted(files):
            path = base / name
            if name in IGNORED_DIRS or linked(path) or not stat.S_ISREG(path.stat().st_mode):
                continue
            relative = path.relative_to(workspace).as_posix()
            if not any(glob_match(relative, p) for p in state["config"]["watch"]):
                continue
            if len(rows) >= MAX_SNAPSHOT_FILES:
                fail("SNAPSHOT_LIMIT", "snapshot exceeds 10000 files")
            rows.append([relative, file_hash(path)])
    return sha(canonical(sorted(rows)))


def reconciliation(state):
    current = snapshot(state)
    changed = current != state.get("review_artifact_sha256")
    return {"artifact_sha256": current, "changed_since_review": changed,
            "stale_review": changed or state.get("review_sha256") is None}


def review_report(value, state, current):
    fields = {"round", "summary", "reconciliation", "checks", "requirements", "claims",
              "issues", "previous_review_sha256", "artifact_sha256", "progress", "final_acceptance"}
    report = obj(value, fields, "review")
    if set(report) != fields:
        fail("REVIEW_INVALID", "review must supply every required field")
    if type(report["round"]) is not int or report["round"] != state["round"]:
        fail("ROUND_MISMATCH", "review round must equal the current round")
    for key in ("summary", "reconciliation"):
        text(report[key], "review." + key)
    checks = report["checks"]
    if not isinstance(checks, list) or not checks or len(checks) > MAX_ENTRIES:
        fail("REVIEW_INVALID", "checks must be a nonempty bounded list")
    for check in checks:
        obj(check, {"method", "result", "evidence"}, "check")
        for field in ("method", "result", "evidence"):
            text(check.get(field), "check." + field)
    requirements = report["requirements"]
    if not isinstance(requirements, dict) or set(requirements) != set(state["goal"]["requirements"]):
        fail("REVIEW_INVALID", "review must reconcile exactly all goal requirements")
    for entry in requirements.values():
        obj(entry, {"status", "evidence"}, "requirement")
        if entry.get("status") not in ("met", "not_met", "unknown"):
            fail("REVIEW_INVALID", "invalid requirement status")
        text(entry.get("evidence"), "requirement.evidence")
    seen, previous = set(), state.get("review") or {}
    for kind, statuses in (("claims", {"accepted", "refuted", "uncertain"}), ("issues", {"open", "resolved"})):
        entries = report[kind]
        if not isinstance(entries, list) or len(entries) > MAX_ENTRIES:
            fail("REVIEW_INVALID", kind + " must be a bounded list")
        index = {}
        for entry in entries:
            allowed = {"id", "statement", "evidence", "status"} | ({"blocking"} if kind == "issues" else set())
            obj(entry, allowed, kind)
            identifier = text(entry.get("id"), kind + ".id", 128)
            if identifier in seen:
                fail("REVIEW_INVALID", "review IDs must be unique across claims and issues")
            seen.add(identifier)
            index[identifier] = entry
            text(entry.get("statement"), kind + ".statement")
            text(entry.get("evidence"), kind + ".evidence")
            if entry.get("status") not in statuses:
                fail("REVIEW_INVALID", "invalid " + kind + " status")
            if kind == "issues":
                boolean(entry.get("blocking"), "issue.blocking")
        for old in previous.get(kind, []):
            if old["id"] not in index or index[old["id"]]["statement"] != old["statement"]:
                fail("APPEND_ONLY", "previous IDs, kinds and statements must be retained")
            if (kind == "issues" and old["status"] == "open" and old["blocking"]
                    and index[old["id"]]["status"] == "open" and not index[old["id"]]["blocking"]):
                fail("APPEND_ONLY", "a blocking issue must be resolved before clearing its blocker")
    if report["previous_review_sha256"] != state.get("review_sha256"):
        fail("PREVIOUS_REVIEW_MISMATCH", "previous review hash does not match the ledger")
    digest(report["artifact_sha256"], "artifact_sha256")
    if report["artifact_sha256"] != current:
        fail("STALE_ARTIFACTS", "review artifact hash does not match the current snapshot")
    boolean(report["progress"], "review.progress")
    final = obj(report["final_acceptance"], {"passed", "evidence"}, "final_acceptance")
    boolean(final.get("passed"), "final_acceptance.passed")
    text(final.get("evidence"), "final_acceptance.evidence")
    return report


def blockers(state):
    return [i for i in (state.get("review") or {}).get("issues", []) if i["status"] == "open" and i["blocking"]]


def unmet(state):
    reqs = (state.get("review") or {}).get("requirements", {})
    return [r for r in state["goal"]["requirements"] if reqs.get(r, {}).get("status") != "met"]


def accepted(state):
    return (state.get("review") is not None and not unmet(state) and not blockers(state)
            and state["review"]["final_acceptance"]["passed"])


COMPACT_LIMIT_BYTES = 4096
ITEM_STRING_LIMIT = 200


def _compact_size(payload):
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def compact_payload(state, stale=False):
    """Bounded resume summary (<= COMPACT_LIMIT_BYTES serialized, reserving 1 byte for newline). Read-only."""
    limit = COMPACT_LIMIT_BYTES - 1
    all_unmet = [str(r)[:ITEM_STRING_LIMIT] for r in unmet(state)]
    all_issues = blockers(state)
    unmet_total = len(all_unmet)
    blockers_total = len(all_issues)

    goal_id_val = str(state["goal"]["goal_id"])[:128]
    review_sha = state.get("review_sha256")
    if review_sha:
        review_sha = str(review_sha)[:64]
    marker = (state.get("pending") or {}).get("marker")
    if marker:
        marker = str(marker)[:128]
    req_action = action(state, stale)

    base = {
        "status": "OK",
        "goal_id": goal_id_val,
        "phase": state["phase"],
        "round": state["round"],
        "review_sha256": review_sha,
        "marker": marker,
        "required_action": req_action,
        "release_authority": False,
    }

    full_blockers = [
        {"id": str(i["id"])[:128], "statement": str(i.get("statement", ""))[:ITEM_STRING_LIMIT]}
        for i in all_issues
    ]
    payload = {
        **base,
        "unmet": list(all_unmet),
        "blockers": full_blockers,
    }
    if _compact_size(payload) <= limit:
        return payload

    stages = [
        (16, [{"id": str(i["id"])[:128]} for i in all_issues[:16]]),
        (8, [{"id": str(i["id"])[:128]} for i in all_issues[:8]]),
        (4, [{"id": str(i["id"])[:128]} for i in all_issues[:4]]),
        (2, [{"id": str(i["id"])[:128]} for i in all_issues[:2]]),
        (0, []),
    ]

    for unmet_cap, block_list in stages:
        payload = {
            **base,
            "unmet": all_unmet[:unmet_cap],
            "blockers": block_list,
            "unmet_total": unmet_total,
            "blockers_total": blockers_total,
            "truncated": True,
        }
        if _compact_size(payload) <= limit:
            return payload

    payload = {
        "status": "OK",
        "goal_id": goal_id_val[:64],
        "phase": state["phase"],
        "round": state["round"],
        "review_sha256": review_sha,
        "marker": marker,
        "required_action": req_action,
        "unmet": [],
        "blockers": [],
        "unmet_total": unmet_total,
        "blockers_total": blockers_total,
        "truncated": True,
        "release_authority": False,
    }
    while _compact_size(payload) > limit:
        if payload.get("marker"):
            payload["marker"] = None
        elif payload.get("review_sha256"):
            payload["review_sha256"] = None
        else:
            break
    return payload


def limit_reason(state, now, starting=False):
    cfg = state["config"]
    if "max_hours" in cfg and now - state["created_at"] >= cfg["max_hours"] * 3600:
        return "max_hours"
    if "max_batches" in cfg:
        used = state["batches_dispatched"]
        if used > cfg["max_batches"] or (starting and used >= cfg["max_batches"]):
            return "max_batches"
    return None


def require_phase(state, *phases):
    if state["phase"] not in phases:
        fail("INVALID_PHASE", "command is not allowed in the current phase")


def require_token(state, token, now):
    lease = state.get("lease")
    if not token or not lease or token != lease["token"] or now >= lease["expires_at"]:
        fail("LEASE_REQUIRED", "a valid, unexpired lease token is required")


def observed(value, now, fields, required=()):
    value = obj(value, fields, "evidence")
    for key in required:
        if key not in value:
            fail("EVIDENCE_REQUIRED", "missing required evidence field")
    if "observed_at" in value:
        stamp = value["observed_at"]
        if isinstance(stamp, str):
            try:
                parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    fail("INVALID_INPUT", "observed_at must include a timezone")
                stamp = parsed.timestamp()
            except ValueError:
                fail("INVALID_INPUT", "observed_at must be an epoch or ISO timestamp")
        number(stamp, "observed_at")
        if not 0 <= now - stamp <= 300:
            fail("STALE_EVIDENCE", "observed_at must be within the preceding 300 seconds")
    if "detail" in value:
        text(value["detail"], "evidence.detail")
    return value


def receipt(value):
    if isinstance(value, str):
        return text(value, "receipt", 2000)
    identifiers = {"id", "message_id", "receipt_id", "run_id", "native_id"}
    obj(value, identifiers | {"source", "kind"}, "receipt")
    if not set(value) & identifiers:
        fail("EVIDENCE_REQUIRED", "receipt requires an identifiable receipt ID")
    for key, item in value.items():
        text(item, "receipt." + key, 512)
    return value


def batch(value):
    value = obj(value, {"todo_path", "todo_sha256", "recipient", "summary"}, "batch")
    todo_path = Path(text(value.get("todo_path"), "todo_path", 4096))
    if not todo_path.is_absolute():
        fail("INVALID_INPUT", "todo_path must be absolute")
    digest(value.get("todo_sha256"), "todo_sha256")
    text(value.get("recipient"), "recipient", 256)
    text(value.get("summary"), "summary", 2000)
    raw = read_bytes(todo_path)
    if sha(raw) != value["todo_sha256"]:
        fail("TODO_HASH_MISMATCH", "TODO content does not match todo_sha256")
    if not isinstance(decode(raw), dict):
        fail("INVALID_INPUT", "TODO must be a JSON object")
    return {**value, "todo_text": raw.decode("utf-8")}


def action(state, stale=False):
    phase = state["phase"]
    if phase in {"stopped", "stopped_limit", "complete"}:
        return "none"
    if phase == "dispatching":
        return "reconcile_delivery"
    if phase == "running":
        return "wait_on_existing_handle"
    if phase in {"observing", "reviewing"} or stale:
        return "review"
    if state["no_progress_reviews"] >= 2:
        return "diagnose"
    return "dispatch" if phase == "prepared" else "prepare"


def output(state, **fields):
    return {"status": "OK", "goal_id": state["goal"]["goal_id"], "phase": state["phase"],
            "round": state["round"], **fields, "release_authority": False}


def observation(state, partial, stale, now):
    # Known derived fields cannot override ledger facts; unknown fields are refused.
    obj(partial, guard.OBSERVATION_REQUIRED, "partial observation")
    source_fields = {"session", "wake_verified", "quota", "stop", "ready_work", "new_planning_gap", "director_packet_unchanged"}
    obs = {key: partial.get(key) for key in source_fields}
    boolean(obs["stop"], "partial.stop")
    phase = state["phase"]
    obs.update({"goal_id": state["goal"]["goal_id"], "unmet": unmet(state),
                "final_acceptance": phase == "complete" and accepted(state) and not stale,
                "execution": "active" if phase == "running" else "unknown" if phase == "dispatching" else "terminal",
                "delivery": "unknown" if phase == "dispatching" else
                            (state["delivery"].get("delivery", "settled") if phase == "running" else "settled"),
                # Saturate guard's bounded signal, never the ledger counter or loop.
                "same_failed_result_count": min(10000, state["no_progress_reviews"]),
                "stop": obs["stop"] or phase in {"stopped", "stopped_limit"} or
                        (phase != "complete" and limit_reason(state, now) is not None)})
    clean = guard._validate_observation(obs, state["goal"]["goal_id"])
    decision = {**guard.next_action(state["goal"], clean), "release_authority": False}
    return output(state, observation=clean, next=decision)


def load_state(root, gid):
    value = read_json(root / gid / "state.json", MAX_STATE_BYTES)
    if (not isinstance(value, dict) or value.get("schema_version") != 1
            or value.get("goal", {}).get("goal_id") != gid):
        fail("STATE_INVALID", "state identity or version mismatch")
    guard._validate_goal(value["goal"])
    return value


def select_goal(root, selected):
    if selected is not None:
        return goal_id(selected)
    candidates = [p.name for p in root.iterdir() if p.is_dir() and (p / "state.json").is_file()] if root.is_dir() else []
    if len(candidates) != 1:
        fail("GOAL_REQUIRED", "specify --goal-id unless the state root has exactly one goal")
    return goal_id(candidates[0])


def execute(args):
    root = Path(args.root or os.environ.get("OFFICE_LOOP_HOME", str(Path.home() / ".agents" / "office-loop")))
    location(root)
    command = args.command
    if command == "init":
        goal = guard._validate_goal(read_json(args.goal))
        gid = goal_id(goal["goal_id"])
        if args.goal_id is not None and args.goal_id != gid:
            fail("INVALID_GOAL_ID", "--goal-id must match the supplied goal")
        cfg = config(read_json(args.config) if args.config else {})
    else:
        gid = select_goal(root, args.goal_id)
        goal = None
        location(root, gid)
        if not (root / ".ledger.lock").exists():
            # Missing/copy-only state is read-only: never create a lock before
            # checking its workspace. A valid initialized ledger has a lock.
            goal = load_state(root, gid)["goal"]
            location(root, gid, goal)
            fail("STATE_INVALID", "initialized ledger lock is missing")
    location(root, gid, goal)
    # Existing-state commands only OPEN the lock; no filesystem writes precede
    # the locked goal/location validation. Reading state under the same lock
    # also avoids Windows sharing violations during os.replace in another writer.
    with exclusive(root, create=command == "init"):
        location(root, gid, goal)
        state_file, now = root / gid / "state.json", time.time()
        if command == "init":
            if state_file.exists():
                fail("ALREADY_INITIALIZED", "goal is already initialized; nothing was overwritten")
            state = {"schema_version": 1, "goal": goal, "config": cfg, "phase": "observing",
                     "round": 0, "created_at": now, "updated_at": now, "lease": None,
                     "review": None, "review_sha256": None, "review_artifact_sha256": None,
                     "review_count": 0, "no_progress_reviews": 0, "batches_dispatched": 0,
                     "batches_completed": 0, "pending": None, "intent": None, "delivery": None}
            state["initial_artifact_sha256"] = snapshot(state)
            (root / gid).mkdir(parents=True, exist_ok=True)
            atomic_json(state_file, state)
            return output(state, config=cfg, root=str(root))
        state = load_state(root, gid)
        location(root, gid, state["goal"])

        def save():
            location(root, gid, state["goal"])
            state["updated_at"] = time.time()
            atomic_json(state_file, state)

        def enforce_limit():
            reason = limit_reason(state, now, starting=True)
            if reason:
                state.update(phase="stopped_limit", limit_reason=reason)
                save()
                fail("LIMIT_REACHED", "an explicit owner limit prevents further dispatch")

        if command == "stop":
            state.update(phase="stopped", stop_reason=text(args.reason, "reason"), lease=None)
            save()
            return output(state)
        if command == "acquire":
            lease = state["lease"]
            if lease and now < lease["expires_at"]:
                fail("LEASE_HELD", "another valid lease already exists")
            token = uuid.uuid4().hex
            state["lease"] = {"token": token, "expires_at": now + state["config"]["lease_minutes"] * 60}
            save()
            return output(state, **state["lease"])
        if command == "compact":
            stale = state.get("review_sha256") is None
            return compact_payload(state, stale=stale)
        if command in {"status", "reconcile", "observation"}:
            rec = reconciliation(state)
            if command == "observation":
                return observation(state, read_json(args.partial), rec["stale_review"], now)
            if command == "reconcile":
                return output(state, **rec)
            return output(state, **rec, review_sha256=state["review_sha256"],
                          no_progress_reviews=state["no_progress_reviews"],
                          counters={k: state[k] for k in ("review_count", "batches_dispatched", "batches_completed")},
                          blocking_issues=[{k: i[k] for k in ("id", "status", "blocking")} for i in blockers(state)],
                          unmet=unmet(state), required_action=action(state, rec["stale_review"]),
                          lease_expires_at=(state["lease"] or {}).get("expires_at"), marker=(state["pending"] or {}).get("marker"))
        require_token(state, args.token, now)
        if command == "renew":
            state["lease"]["expires_at"] = now + state["config"]["lease_minutes"] * 60
            save()
            return output(state, **state["lease"])
        if command == "release":
            state["lease"] = None
        elif command == "review":
            if state["phase"] != "reviewing" and not (state["phase"] == "observing" and state["round"] == 0):
                fail("INVALID_PHASE", "review requires reviewing or observing round zero")
            report = review_report(read_json(args.report), state, snapshot(state))
            review_sha = sha(canonical(report))
            state.update(review=report, review_sha256=review_sha, review_artifact_sha256=report["artifact_sha256"],
                         review_count=state["review_count"] + 1,
                         no_progress_reviews=0 if report["progress"] else state["no_progress_reviews"] + 1)
            state["phase"] = "complete" if accepted(state) else "reviewed"
            reason = limit_reason(state, now, starting=state["phase"] != "complete")
            if reason:
                state.update(phase="stopped_limit", limit_reason=reason)
            atomic_json(root / gid / ("review-" + str(state["review_count"]) + "-" + review_sha + ".json"), report)
        elif command == "invalidate":
            require_phase(state, "prepared", "reviewed")
            text(args.reason, "reason")
            if state["phase"] == "prepared":
                state["round"] -= 1
                state.update(pending=None, intent=None, delivery=None)
            state.update(phase="reviewing", invalidation_reason=args.reason)
        elif command == "prepare":
            require_phase(state, "reviewed")
            enforce_limit()
            if reconciliation(state)["stale_review"]:
                fail("STALE_ARTIFACTS", "review is stale; invalidate and review the current artifacts")
            frozen = batch(read_json(args.batch))
            state["round"] += 1
            frozen["marker"] = "[OL-" + gid + "-" + format(state["round"], "04d") + "]"
            frozen["batch_sha256"] = sha(canonical(frozen))
            state.update(phase="prepared", pending=frozen, intent=None, delivery=None)
        elif command == "dispatching":
            if state["phase"] == "dispatching":
                fail("DELIVERY_UNCERTAIN", "reconcile this marker; never redispatch an uncertain attempt")
            require_phase(state, "prepared")
            enforce_limit()
            if reconciliation(state)["stale_review"]:
                fail("STALE_ARTIFACTS", "review is stale; dispatch intent was not recorded")
            evidence = observed(read_json(args.evidence), now, {"observed_at", "detail"}, ("observed_at", "detail"))
            state.update(phase="dispatching", intent=evidence, batches_dispatched=state["batches_dispatched"] + 1)
        elif command == "delivered":
            require_phase(state, "dispatching")
            if not state["intent"]:
                fail("INTENT_REQUIRED", "delivery requires a previously persisted dispatch intent")
            evidence = observed(read_json(args.evidence), now, {"observed_at", "detail", "marker_seen", "marker", "receipt", "delivery"})
            if evidence.get("marker_seen") is not True or evidence.get("marker") != state["pending"]["marker"]:
                fail("MARKER_REQUIRED", "delivery evidence must identify the prepared marker")
            receipt(evidence.get("receipt"))
            if evidence.get("delivery", "settled") not in {"settled", "inflight"}:
                fail("INVALID_INPUT", "delivery must be settled or inflight")
            state.update(phase="running", delivery=evidence)
        elif command == "completed":
            if state["phase"] != "running" and not (state["phase"] == "observing" and state["round"] == 0):
                fail("INVALID_PHASE", "completed requires running or observing round zero")
            evidence = observed(read_json(args.evidence), now, {"observed_at", "detail", "terminal", "run_id"})
            if evidence.get("terminal") is not True:
                fail("EVIDENCE_REQUIRED", "completed requires terminal=true")
            text(evidence.get("run_id"), "run_id", 256)
            if state["phase"] == "running":
                state["batches_completed"] += 1
            state.update(phase="reviewing", completion=evidence)
        save()
        return output(state, review_sha256=state["review_sha256"], marker=(state["pending"] or {}).get("marker"),
                      batch_sha256=(state["pending"] or {}).get("batch_sha256"), required_action=action(state))


class JSONParser(argparse.ArgumentParser):
    def error(self, message):
        fail("ARGUMENT_ERROR", "invalid CLI arguments; use <command> --root <absolute home> ...")


def build_parser(command=None):
    """Build the loop_ledger parser.

    With no argument, returns the top-level parser. With a command name
    (e.g. build_parser("compact")), returns that subcommand's parser so
    callers can parse that subcommand's flags directly.
    """
    wanted = command
    parser = JSONParser(prog="loop_ledger.py", add_help=False)
    sub = parser.add_subparsers(dest="command", required=True)
    children = {}
    for command in COMMANDS:
        child = sub.add_parser(command, add_help=False)
        child.add_argument("--root")
        child.add_argument("--goal-id")
        if command == "init":
            child.add_argument("--goal", required=True)
            child.add_argument("--config")
        if command not in {"init", "acquire", "status", "reconcile", "observation", "stop", "compact"}:
            child.add_argument("--token")
        for cmd, flag in (("review", "report"), ("prepare", "batch"), ("observation", "partial"), ("invalidate", "reason"), ("stop", "reason")):
            if command == cmd:
                child.add_argument("--" + flag, required=True)
        if command in {"dispatching", "delivered", "completed"}:
            child.add_argument("--evidence", required=True)
        children[command] = child
    if wanted is not None:
        child = children.get(wanted)
        if child is None:
            fail("ARGUMENT_ERROR", "unknown command: %r" % (wanted,))
        return child
    return parser


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        if argv in (["--help"], ["-h"]):
            payload = {"status": "OK", "commands": list(COMMANDS),
                       "usage": "loop_ledger.py <command> [--root <absolute home>] [--goal-id <id>] ...", "release_authority": False}
        else:
            payload = execute(build_parser().parse_args(argv))
        code = 0
    except LedgerError as exc:
        payload = {"status": "ERROR", "code": exc.code, "message": str(exc), "release_authority": False}
        code = 2
    except guard.GuardError:
        payload = {"status": "ERROR", "code": "GUARD_REJECTED", "message": "goal or observation failed loop_guard validation", "release_authority": False}
        code = 2
    except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
        payload = {"status": "ERROR", "code": "IO_OR_STATE_ERROR", "message": "input or state could not be processed", "release_authority": False}
        code = 2
    json.dump(payload, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write("\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
