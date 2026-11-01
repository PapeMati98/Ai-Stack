"""Metadata-only completion sidecar shared by Agent Graph and Hub closeout.

This module does not run a model, inspect project files, or contact a sink. It
stores a bounded record under the existing local TODO telemetry outbox.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys

sys.dont_write_bytecode = True

_CORE_SPEC = importlib.util.spec_from_file_location(
    "_audit_completion_audit_core", Path(__file__).with_name("audit_core.py"))
audit_core = importlib.util.module_from_spec(_CORE_SPEC)
sys.modules[_CORE_SPEC.name] = audit_core
_CORE_SPEC.loader.exec_module(audit_core)

_SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}\Z", re.ASCII)
_HEX = re.compile(r"[a-f0-9]{64}\Z", re.ASCII)
_RUN_ID = re.compile(r"run-\d{8}T\d{6}Z-[0-9a-f]+\Z", re.ASCII)
_SUCCESS = frozenset(("success", "succeeded", "completed", "complete", "stop",
                      "end_turn", "turn_complete", "done"))
_EXCEPTIONS = frozenset(("none", "trivial", "credentials", "irreversible",
                         "capability-unavailable", "quota-exhausted",
                         "explicit-owner-model", "worker-node"))
_KINDS = frozenset(("delivery", "repair", "audit", "audit-review", "subtask",
                    "status", "metadata-only"))
_FAMILIES = frozenset(("implementation", "research", "operations", "skill", "other"))
_EPISODE_ROLES = frozenset(("top-level", "worker", "audit", "audit-review", "status", "unknown"))
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
MAX_SIDECARS = 64
MAX_SIDECAR_BYTES = 65536
MAX_CONTEXT_BYTES = 1024 * 1024


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate metadata key")
        result[key] = value
    return result


def _is_link(path):
    try:
        info = Path(path).lstat()
    except OSError:
        return True
    return Path(path).is_symlink() or bool(getattr(info, "st_file_attributes", 0) & _REPARSE)


def _safe_id(value):
    return type(value) is str and _SAFE.fullmatch(value) is not None and ".." not in value


def _session_hash(session_id):
    return _sha(session_id.encode("utf-8"))


def _path_hash(value):
    if type(value) is not str or not value or not Path(value).is_absolute():
        return None
    try:
        canonical = str(Path(value).resolve(strict=False)).casefold()
        return _sha(canonical.encode("utf-8"))
    except (OSError, RuntimeError, ValueError):
        return None


def _binding_key(agent, session_hash, project):
    return _sha(_json([agent, session_hash, project]))


def _native_session_ok(session_id, environ):
    if _RUN_ID.fullmatch(session_id):
        return False
    values = [environ.get(name) for name in ("CODEX_SESSION_ID", "CODEX_THREAD_ID")
              if isinstance(environ.get(name), str) and environ.get(name)]
    return not values or (len(set(values)) == 1 and values[0] == session_id)


def _has_reparse_component(path, stop):
    path, stop = Path(path), Path(stop)
    try:
        path.relative_to(stop)
    except ValueError:
        return True
    cursor = path
    while True:
        if _is_link(cursor):
            return True
        if cursor == stop:
            return False
        if cursor.parent == cursor:
            return True
        cursor = cursor.parent


def _read_context_receipt(plan, environ, receipt_root=None):
    """Validate the local begin-task receipt's binding fields; never retain its path."""
    agent, session_id = plan.get("context_receipt_agent"), plan.get("context_receipt_session")
    requested = plan.get("context_receipt")
    if not (_safe_id(agent) and _safe_id(session_id) and _native_session_ok(session_id, environ)):
        return None
    if not isinstance(requested, str):
        return None
    path = Path(requested)
    root = (Path(receipt_root) if receipt_root is not None else
            Path.home() / ".agents" / "knowledge-context-receipts")
    if not root.is_absolute() or not path.is_absolute():
        return None
    try:
        root = Path(os.path.abspath(os.fspath(root)))
        path = Path(os.path.abspath(os.fspath(path)))
        if not root.is_dir() or _has_reparse_component(root, root.anchor and Path(root.anchor)):
            return None
        if _has_reparse_component(path, root):
            return None
        resolved_root = root.resolve(strict=True)
        resolved = path.resolve(strict=True)
        if (not resolved.is_relative_to(resolved_root) or not resolved.is_file()
                or resolved.stat().st_size > MAX_CONTEXT_BYTES):
            return None
        # Bound the actual read too, in case the file changes after the stat.
        with resolved.open("rb") as stream:
            raw = stream.read(MAX_CONTEXT_BYTES + 1)
        if len(raw) > MAX_CONTEXT_BYTES:
            return None
        payload = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_object)
    except (OSError, ValueError, UnicodeError, RecursionError):
        return None
    if type(payload) is not dict:
        return None
    digest = _session_hash(session_id)
    binding = payload.get("vault_binding")
    execution = payload.get("execution")
    attempts = execution.get("attempts") if type(execution) is dict else None
    health = payload.get("health")
    if not (
        payload.get("schema_version") == 2
        and payload.get("action") == "begin-task"
        and payload.get("status") == "COMPLETE"
        and payload.get("agent") == agent
        and payload.get("session_sha256") == digest
        and _safe_id(payload.get("project_slug"))
        and type(binding) is dict
        and binding.get("version") == 1
        and binding.get("kind") == "local-execution-evidence"
        and binding.get("producer_authentication") == "none"
        and _HEX.fullmatch(str(binding.get("vault_path_sha256") or ""))
        and type(health) is dict
        and health.get("status") == "PASS"
        and health.get("lint") == "PASS"
        and health.get("lint_error_count") == 0
        and payload.get("coherent_read") is True
        and type(attempts) is list and bool(attempts)
        and type(attempts[-1]) is dict
        and attempts[-1].get("status") == "COMPLETE"
        and attempts[-1].get("coherent_read") is True
    ):
        return None
    return {
        "caller_id": agent,
        "session_sha256": digest,
        "project_slug": payload["project_slug"],
        "context_receipt_sha256": _sha(raw),
    }


def _classification(plan):
    item = plan.get("completion")
    fields = {"task_kind", "task_family", "episode_role", "eligible", "exception"}
    if type(item) is not dict or set(item) not in (fields, fields | {"uat_required"}):
        return None
    if (type(item["task_kind"]) is not str or item["task_kind"] not in _KINDS
            or type(item["task_family"]) is not str or item["task_family"] not in _FAMILIES
            or type(item["episode_role"]) is not str or item["episode_role"] not in _EPISODE_ROLES
            or (type(item["eligible"]) is not bool and item["eligible"] is not None)
            or type(item["exception"]) is not str or item["exception"] not in _EXCEPTIONS
            or ("uat_required" in item and type(item["uat_required"]) is not bool)):
        return None
    return dict(item)


def _catalog_path():
    # Both installed skills and the source tree place skills/agent-graph beside
    # skills/delivery-audit. This is code-owned and does not use an env path.
    skills_root = Path(__file__).resolve().parents[2]
    return skills_root / "agent-graph" / "references" / "model-catalog.json"


def _tier(agent, model, catalog_path=None):
    if not (_safe_id(agent) and isinstance(model, str) and model):
        return "unknown"
    try:
        catalog = json.loads(Path(catalog_path or _catalog_path()).read_text(encoding="utf-8"))
        for entry in catalog.get("models", []):
            if entry.get("tier") not in {"D", "M", "K", "W"}:
                continue
            if any(profile.get("agent") == agent and profile.get("model") == model
                   for profile in entry.get("profiles", [])):
                return entry["tier"]
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return "unknown"


def _node_checks_pass(node, task):
    checks = task.get("checks")
    if type(checks) is not list or not checks:
        return False
    observed = node.get("tests") if type(node.get("tests")) is list else None
    if observed is None:
        history = node.get("history")
        if type(history) is list:
            for entry in reversed(history):
                if type(entry) is dict and entry.get("role") == "reviewer" and type(entry.get("tests")) is list:
                    observed = entry["tests"]
                    break
    return (type(observed) is list and len(observed) == len(checks)
            and all(type(test) is dict and test.get("exit_code") == 0 for test in observed))


def _plan_digest(plan):
    return _sha(json.dumps(plan, sort_keys=True, ensure_ascii=True,
                           separators=(",", ":"), allow_nan=False).encode("ascii"))


def _safe_ref(prefix, value):
    return prefix + _sha(_json(value))[:32]


def _candidate_runs(plan, nodes, run_folder, workspace_id):
    runs, seen = [], set()
    tasks = {task["id"]: task for task in plan.get("tasks", [])}
    for node_id, node in nodes.items():
        task = tasks.get(node_id)
        history = node.get("history") if type(node) is dict else None
        if task is None or type(history) is not list:
            continue
        current_round = 0
        for entry in history:
            if type(entry) is not dict or entry.get("role") not in {"maker", "reviewer", "autopsy"}:
                continue
            scheduler_role = entry["role"]
            if scheduler_role == "maker":
                current_round += 1
            result = entry.get("result")
            run_id = result.get("run_id") if type(result) is dict else None
            if not _safe_id(run_id):
                continue
            if run_id in seen:
                return None
            seen.add(run_id)
            role = ("reviewer" if entry["role"] in {"reviewer", "autopsy"} else
                    "scout" if task.get("role") == "scout" else "maker")
            output_dir_sha256 = None
            if scheduler_role in {"maker", "reviewer"} and current_round > 0:
                output_dir = (Path(run_folder) / node_id / str(current_round) / scheduler_role /
                              "fleet" / run_id)
                output_dir_sha256 = _path_hash(str(output_dir))
            runs.append({
                "run_id": run_id, "node_ref": _safe_ref("node-", node_id), "role": role,
                "scheduler_role": scheduler_role, "output_dir_sha256": output_dir_sha256,
                "workspace_sha256": workspace_id,
            })
    return runs


def _build_candidate(plan, result, runner_kind, binding, catalog_path):
    task_rows = plan.get("tasks")
    if runner_kind == "graph":
        if result.get("status") != "ACCEPTED_CANDIDATE":
            return None
        nodes = result.get("nodes")
        if (type(nodes) is not dict or set(nodes) != {task.get("id") for task in task_rows}
                or any(type(nodes.get(task["id"])) is not dict
                       or nodes[task["id"]].get("status") != "ACCEPTED" for task in task_rows)):
            return None
    elif runner_kind == "pair":
        if result.get("status") != "ACCEPTED" or len(task_rows) != 1:
            return None
        nodes = {task_rows[0]["id"]: result}
    else:
        return None

    completion = _classification(plan)
    if completion is None or not binding:
        return None
    plan_hash = _plan_digest(plan)
    workspace = plan.get("workspace")
    if not isinstance(workspace, str) or not Path(workspace).is_absolute():
        return None
    try:
        workspace_id = _path_hash(workspace)
        if workspace_id is None:
            return None
    except (OSError, ValueError):
        return None

    evidence_refs = set()
    requirement_rows = []
    for requirement in plan.get("requirements", []):
        requirement_id = _safe_ref("req-", requirement)
        covering = [task for task in task_rows if requirement in task.get("covers", [])]
        check_refs = []
        passed = bool(covering)
        for task in covering:
            node = nodes.get(task["id"], {})
            if not _node_checks_pass(node, task):
                passed = False
                continue
            ref = _safe_ref("check-", [requirement_id, task["id"], task.get("checks", [])])
            check_refs.append(ref)
            evidence_refs.add(ref)
        requirement_rows.append({"id": requirement_id,
                                 "status": "pass" if passed and check_refs else "unknown",
                                 "evidence_refs": sorted(set(check_refs))})

    runs = _candidate_runs(plan, nodes, binding["run_folder"], workspace_id)
    if runs is None:
        return None
    for run in runs:
        evidence_refs.add(run["node_ref"])
    uat_tasks = [task for task in task_rows if task.get("role") == "uat" or "uat" in task]
    uat_required = completion.get("uat_required")
    uat_status = "unknown"
    if uat_tasks:
        uat_proven = True
        for task in uat_tasks:
            node = nodes.get(task["id"], {})
            evidence = node.get("uat_evidence") if type(node) is dict else None
            if (type(evidence) is not dict or evidence.get("final") is not True
                    or evidence.get("unchanged_during_uat") is not True
                    or not _node_checks_pass(node, task)):
                uat_proven = False
        uat_status = "pass" if uat_proven else "unknown"
    elif uat_required is False:
        uat_status = "not-applicable"

    round_values = [node.get("round") for node in nodes.values()]
    repair_rounds = (sum(max(0, value - 1) for value in round_values)
                     if all(type(value) is int and value >= 0 for value in round_values) else None)
    planner_model = plan.get("planner", {}).get("model")
    parent_tier = _tier(binding["caller_id"], planner_model, catalog_path)
    candidate = {
        "schema_version": 1,
        "source": "todo-graph" if runner_kind == "graph" else "todo-pair",
        "candidate_status": "ACCEPTED_CANDIDATE",
        "caller_id": binding["caller_id"],
        "session_sha256": binding["session_sha256"],
        "project_slug": binding["project_slug"],
        "context_receipt_sha256": binding["context_receipt_sha256"],
        "plan_sha256": plan_hash,
        "task_id": "task-" + plan_hash[:32],
        "workspace_id": workspace_id,
        "harness": binding["harness"],
        "task_kind": completion["task_kind"],
        "task_family": completion["task_family"],
        "episode_role": completion["episode_role"],
        "eligible": completion["eligible"],
        "exception": completion["exception"],
        "parent_tier": parent_tier,
        "planned_tasks": len(task_rows),
        "requirements": requirement_rows,
        "runs": runs,
        "metrics": {"wall_seconds": None, "repair_rounds": repair_rounds,
                    "duplicate_work": None, "uat_required": uat_required,
                    "uat_status": uat_status, "evidence_complete": None},
        "observations": [],
        "evidence_refs": sorted(evidence_refs),
    }
    body = _json(candidate)
    candidate["sidecar_sha256"] = _sha(body)
    return candidate


def _verify_sidecar(value):
    if type(value) is not dict or not _HEX.fullmatch(str(value.get("sidecar_sha256") or "")):
        return False
    body = {key: child for key, child in value.items() if key != "sidecar_sha256"}
    try:
        return _sha(_json(body)) == value["sidecar_sha256"]
    except (TypeError, ValueError):
        return False


def _base_dir(outbox_root=None):
    return Path(outbox_root) if outbox_root is not None else Path.home() / ".agents" / "telemetry" / "todo"


def _ensure_dir(path):
    path = Path(path)
    if not path.is_absolute():
        return False
    if path.exists():
        return path.is_dir() and not _is_link(path)
    parent = path.parent
    if not _ensure_dir(parent):
        return False
    try:
        path.mkdir()
    except FileExistsError:
        pass
    except OSError:
        return False
    return path.is_dir() and not _is_link(path)


def _candidate_dir(binding, outbox_root=None, *, create=False):
    base = _base_dir(outbox_root)
    if (not base.is_absolute() or not base.is_dir()
            or _has_reparse_component(base, Path(base.anchor))):
        return None
    family = base / "task-closeouts"
    if create and not _ensure_dir(family):
        return None
    if not family.is_dir() or _is_link(family):
        return None
    bound = family / _binding_key(binding["caller_id"], binding["session_sha256"], binding["project_slug"])
    if create and not _ensure_dir(bound):
        return None
    if not bound.is_dir() or _is_link(bound):
        return None
    return bound


def _read_json_file(path, max_bytes=MAX_SIDECAR_BYTES):
    path = Path(path)
    if _is_link(path) or not path.is_file():
        return None
    try:
        if path.stat().st_size > max_bytes:
            return None
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_object)
    except (OSError, ValueError, UnicodeError, RecursionError):
        return None


def emit_completion(plan, result, folder, runner_kind, *, outbox_root=None,
                    environ=None, catalog_path=None, context_receipt_root=None):
    """Best-effort idempotent sidecar hook; product result is never modified."""
    if type(plan) is not dict or type(result) is not dict:
        return {"status": "SKIPPED", "code": "INVALID_INPUT"}
    if runner_kind == "graph" and result.get("status") != "ACCEPTED_CANDIDATE":
        return {"status": "SKIPPED", "code": "NOT_ACCEPTED"}
    if runner_kind == "pair" and result.get("status") != "ACCEPTED":
        return {"status": "SKIPPED", "code": "NOT_ACCEPTED"}
    if _classification(plan) is None:
        return {"status": "SKIPPED_UNCLASSIFIED", "code": "UNCLASSIFIED"}
    env = os.environ if environ is None else environ
    binding = _read_context_receipt(plan, env, context_receipt_root)
    if binding is None:
        return {"status": "SKIPPED", "code": "CONTEXT_BINDING_UNAVAILABLE"}
    declared_harness = env.get("COORDFS_HARNESS") if hasattr(env, "get") else None
    if isinstance(declared_harness, str) and declared_harness in {
        "codex", "claude", "antigravity", "zcode", "kimi", "grok", "qwen",
        "minimax", "mimo", "mistral", "muse", "openclaw", "other",
    }:
        binding["harness"] = declared_harness
    elif binding["caller_id"] in {"codex", "claude", "antigravity", "zcode", "kimi", "grok",
                                   "qwen", "minimax", "mimo", "mistral", "muse", "openclaw"}:
        binding["harness"] = binding["caller_id"]
    else:
        return {"status": "SKIPPED", "code": "HARNESS_UNCLASSIFIED"}
    if not isinstance(folder, (str, os.PathLike)) or not Path(folder).is_absolute():
        return {"status": "SKIPPED", "code": "RUN_FOLDER_UNAVAILABLE"}
    binding["run_folder"] = str(Path(folder).resolve(strict=False))
    candidate = _build_candidate(plan, result, runner_kind, binding, catalog_path)
    if candidate is None:
        return {"status": "SKIPPED", "code": "CANDIDATE_METADATA_UNAVAILABLE"}
    try:
        if len(_json(candidate)) > MAX_SIDECAR_BYTES:
            return {"status": "SKIPPED", "code": "SIDECAR_TOO_LARGE"}
    except (TypeError, ValueError):
        return {"status": "SKIPPED", "code": "SIDECAR_INVALID"}
    directory = _candidate_dir(binding, outbox_root, create=True)
    if directory is None:
        return {"status": "SKIPPED", "code": "OUTBOX_UNAVAILABLE"}
    target = directory / (candidate["plan_sha256"] + ".json")
    serialized = _json(candidate)
    try:
        with target.open("xb") as stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        return {"status": "EMITTED", "candidate_id": candidate["plan_sha256"]}
    except FileExistsError:
        existing = _read_json_file(target)
        if _verify_sidecar(existing) and _json(existing) == serialized:
            return {"status": "DUPLICATE", "candidate_id": candidate["plan_sha256"]}
        return {"status": "SKIPPED", "code": "SIDECAR_CONFLICT"}
    except OSError:
        return {"status": "SKIPPED", "code": "OUTBOX_WRITE_FAILED"}


def _load_candidates(binding, outbox_root=None):
    directory = _candidate_dir(binding, outbox_root)
    if directory is None:
        return [], "SIDECAR_UNAVAILABLE"
    try:
        paths = [entry for entry in directory.iterdir()
                 if re.fullmatch(r"[a-f0-9]{64}\.json", entry.name, re.ASCII)]
    except OSError:
        return [], "SIDECAR_UNAVAILABLE"
    if len(paths) > MAX_SIDECARS:
        return [], "SIDECAR_OVERFLOW"
    candidates = []
    for path in paths:
        value = _read_json_file(path)
        if (not _verify_sidecar(value) or value.get("plan_sha256") != path.stem
                or value.get("caller_id") != binding["caller_id"]
                or value.get("session_sha256") != binding["session_sha256"]
                or value.get("project_slug") != binding["project_slug"]
                or value.get("candidate_status") != "ACCEPTED_CANDIDATE"):
            return [], "SIDECAR_INVALID"
        candidates.append(value)
    return candidates, None


def _event_dir(candidate, binding, outbox_root=None, *, create=False):
    root = _candidate_dir(binding, outbox_root, create=create)
    if root is None:
        return None
    path = root / (candidate["plan_sha256"] + ".closeouts")
    if create and not _ensure_dir(path):
        return None
    return path if path.is_dir() and not _is_link(path) else None


def _verify_event(event, candidate, checkpoint_key):
    fields = {
        "schema_version", "event_type", "caller_id", "session_sha256", "project_slug",
        "plan_sha256", "candidate_sha256", "checkpoint_key", "packet", "audit_id",
        "event_sha256",
    }
    if (type(event) is not dict or set(event) != fields
            or type(event.get("schema_version")) is not int or event.get("schema_version") != 1
            or event.get("event_type") != "delivery-audit.completion.v1"
            or not _HEX.fullmatch(str(event.get("event_sha256") or ""))):
        return False
    body = {key: value for key, value in event.items() if key != "event_sha256"}
    try:
        if (_sha(_json(body)) != event["event_sha256"]
                or event.get("candidate_sha256") != candidate.get("sidecar_sha256")
                or event.get("plan_sha256") != candidate.get("plan_sha256")
                or event.get("checkpoint_key") != checkpoint_key
                or event.get("caller_id") != candidate.get("caller_id")
                or event.get("session_sha256") != candidate.get("session_sha256")
                or event.get("project_slug") != candidate.get("project_slug")):
            return False
        packet = audit_core.validate_packet(event.get("packet"))
        return (
            event.get("audit_id") == audit_core.audit_id(packet)
            and packet["task_id"] == candidate.get("task_id")
            and packet["project_slug"] == candidate.get("project_slug")
            and _session_hash(packet["session_id"]) == candidate.get("session_sha256")
            and packet["workspace_id"] == candidate.get("workspace_id")
            and packet["outcome"] == "COMPLETED"
        )
    except (ValueError, TypeError, NameError):
        return False


def _frozen_events(candidate, binding, outbox_root=None):
    """Load and verify every bounded frozen event for one candidate."""
    root = _candidate_dir(binding, outbox_root)
    if root is None:
        return None, "EVENT_UNAVAILABLE"
    directory = root / (candidate["plan_sha256"] + ".closeouts")
    try:
        directory.lstat()
    except FileNotFoundError:
        return [], None
    except OSError:
        return None, "EVENT_UNAVAILABLE"
    if not directory.is_dir() or _is_link(directory):
        return None, "EVENT_UNAVAILABLE"
    try:
        paths = list(directory.iterdir())
    except OSError:
        return None, "EVENT_UNAVAILABLE"
    if len(paths) > MAX_SIDECARS:
        return None, "EVENT_OVERFLOW"
    events = []
    for path in paths:
        if (not re.fullmatch(r"[a-f0-9]{64}\.json", path.name, re.ASCII)
                or _is_link(path) or not path.is_file()):
            return None, "EVENT_INVALID"
        checkpoint_key = path.stem
        event = _read_json_file(path)
        if not _verify_event(event, candidate, checkpoint_key):
            return None, "EVENT_INVALID"
        events.append((checkpoint_key, event))
    return events, None


def existing_event(candidate, binding, checkpoint_key, *, outbox_root=None):
    events, error = _frozen_events(candidate, binding, outbox_root)
    if error:
        return None, error
    matching = [event for key, event in events if key == checkpoint_key]
    if len(matching) > 1:
        return None, "EVENT_INVALID"
    if matching:
        return matching[0], None
    if events:
        return None, "SKIPPED_AMBIGUOUS"
    return None, None


def resolve_candidate(runtime_agent, session_id, project_slug, checkpoint_key, *,
                      outbox_root=None, environ=None):
    env = os.environ if environ is None else environ
    if not (_safe_id(runtime_agent) and _safe_id(session_id) and _safe_id(project_slug)
            and _HEX.fullmatch(str(checkpoint_key or ""))
            and _native_session_ok(session_id, env)):
        return None, None, "INVALID_BINDING"
    binding = {"caller_id": runtime_agent, "session_sha256": _session_hash(session_id),
               "project_slug": project_slug}
    candidates, error = _load_candidates(binding, outbox_root)
    if error:
        return None, binding, error
    if not candidates:
        return None, binding, "SIDECAR_NOT_FOUND"
    matching, unconsumed = [], []
    for candidate in candidates:
        events, event_error = _frozen_events(candidate, binding, outbox_root)
        if event_error:
            return None, binding, event_error
        for key, _event in events:
            if key == checkpoint_key:
                matching.append(candidate)
        if not events:
            unconsumed.append(candidate)
    if len(matching) == 1:
        return matching[0], binding, None
    if len(matching) > 1:
        return None, binding, "SKIPPED_AMBIGUOUS"
    if len(unconsumed) == 1:
        return unconsumed[0], binding, None
    if len(unconsumed) > 1:
        return None, binding, "SKIPPED_AMBIGUOUS"
    return None, binding, "SIDECAR_NOT_FOUND"


def store_event(candidate, binding, checkpoint_key, packet, audit_id, *, outbox_root=None):
    directory = _event_dir(candidate, binding, outbox_root, create=True)
    if directory is None:
        return None, "EVENT_UNAVAILABLE"
    try:
        paths = [entry for entry in directory.iterdir()
                 if re.fullmatch(r"[a-f0-9]{64}\.json", entry.name, re.ASCII)]
    except OSError:
        return None, "EVENT_UNAVAILABLE"
    if len(paths) > MAX_SIDECARS:
        return None, "EVENT_OVERFLOW"
    if any(path.stem != checkpoint_key for path in paths):
        return None, "SKIPPED_AMBIGUOUS"
    try:
        canonical = audit_core.validate_packet(packet)
        if audit_id != audit_core.audit_id(canonical):
            return None, "EVENT_INVALID"
    except (ValueError, TypeError):
        return None, "EVENT_INVALID"
    event = {
        "schema_version": 1, "event_type": "delivery-audit.completion.v1",
        "caller_id": binding["caller_id"], "session_sha256": binding["session_sha256"],
        "project_slug": binding["project_slug"], "plan_sha256": candidate["plan_sha256"],
        "candidate_sha256": candidate["sidecar_sha256"], "checkpoint_key": checkpoint_key,
        "packet": canonical, "audit_id": audit_id,
    }
    event["event_sha256"] = _sha(_json(event))
    if len(_json(event)) > MAX_SIDECAR_BYTES:
        return None, "EVENT_TOO_LARGE"
    target = directory / (checkpoint_key + ".json")
    serialized = _json(event)
    try:
        with target.open("xb") as stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        return event, None
    except FileExistsError:
        existing = _read_json_file(target)
        if _verify_event(existing, candidate, checkpoint_key) and _json(existing) == serialized:
            return existing, None
        return None, "EVENT_CONFLICT"
    except OSError:
        return None, "EVENT_WRITE_FAILED"
