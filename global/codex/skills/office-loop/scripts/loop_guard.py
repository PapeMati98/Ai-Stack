#!/usr/bin/env python3
"""Pure stdlib helper for the Office Loop skill.

Decides the next typed action for a goal-bound Director-to-Fleet loop and
validates a Director's response as data. The helper is a deterministic
syntactic/scope guard: it does not authenticate evidence, dispatch a TODO,
spawn a process, touch the network, schedule timers, delete files, mutate
state, or grant authority. A validated response is data, never a permission.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

sys.dont_write_bytecode = True

VERSION = "1.0.0"
SCHEMA_VERSION = 1
MAX_INPUT_BYTES = 64 * 1024

EXECUTION = {"active", "terminal", "unknown"}
DELIVERY = {"settled", "inflight", "unknown"}
SESSION = {"active", "ended", "unknown"}
QUOTA = {"available", "exhausted", "unknown"}
DECISION = {"next_batch", "blocked", "complete"}
TASK_MODES = {"implement", "advisory"}

GOAL_REQUIRED = {"version", "goal_id", "goal", "workspace", "requirements", "constraints", "allowed_scope"}
OBSERVATION_REQUIRED = {
    "goal_id", "stop", "execution", "delivery", "session", "wake_verified",
    "quota", "final_acceptance", "unmet", "ready_work", "new_planning_gap",
    "same_failed_result_count", "director_packet_unchanged",
}
TASK_REQUIRED = {
    "id", "text", "covers", "scope", "depends_on", "checks", "done_when",
    "mode", "maker", "reviewer",
}
TASK_OPTIONAL = {"inputs", "requires_approval"}

TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


class GuardError(ValueError):
    """Raised when input data fails the bounded syntactic/scope guard."""


def _fail(message: str) -> None:
    raise GuardError(message)


def _bool(value: Any, label: str) -> bool:
    if type(value) is not bool:
        _fail(f"{label} must be boolean")
    return value


def _int_not_bool(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or type(value) is not int or not (minimum <= value <= maximum):
        _fail(f"{label} must be an integer in {minimum}..{maximum}")
    return value


def _enum(value: Any, label: str, allowed: set[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        _fail(f"{label} must be one of {sorted(allowed)}")
    return value


def _nonempty_text(value: Any, label: str, maximum: int = 8000) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{label} must be a nonempty string")
    if len(value) > maximum:
        _fail(f"{label} exceeds {maximum} characters")
    return value


def _string_list(value: Any, label: str, *, allow_empty: bool = False, maximum: int = 256) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        _fail(f"{label} must be a list with at most {maximum} entries")
    if not allow_empty and not value:
        _fail(f"{label} must be a nonempty list")
    seen: set[str] = set()
    out: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            _fail(f"{label}[{index}] must be a nonempty string")
        if item in seen:
            _fail(f"{label}[{index}] duplicate: {item!r}")
        seen.add(item)
        out.append(item)
    return out


def _safe_relative_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(f"{label} must be a nonempty relative path")
    value = value.replace("\\", "/")
    if ":" in value or "\x00" in value or value.startswith("/"):
        _fail(f"{label} contains a drive, stream, root or NUL")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if posix.is_absolute() or windows.is_absolute():
        _fail(f"{label} must be relative, not absolute: {value!r}")
    if value.endswith("/") or value.endswith("\\"):
        _fail(f"{label} must not end with a directory separator (use a tree boundary in allowed_scope)")
    parts = value.split("/")
    if any(part in {"..", ".", ""} for part in parts):
        _fail(f"{label} contains traversal, empty or dot segment: {value!r}")
    return value


def _resolve_under_workspace(path_str: str, workspace: Path, label: str) -> Path:
    try:
        resolved_workspace = workspace.resolve(strict=False)
        resolved = (workspace / path_str).resolve(strict=False)
    except (OSError, ValueError) as exc:
        _fail(f"cannot resolve {label} {path_str!r}: {exc}")
    try:
        resolved.relative_to(resolved_workspace)
    except ValueError:
        _fail(f"{label} {path_str!r} escapes the workspace via junction, symlink or traversal")
    return resolved


def _scope_subset(scope_path: str, allowed_scope: list[str]) -> bool:
    scope_path = scope_path.replace("\\", "/")
    for entry in allowed_scope:
        if entry.endswith("/"):
            tree = entry[:-1]
            if scope_path == tree or scope_path.startswith(tree + "/"):
                return True
        else:
            if scope_path == entry:
                return True
    return False


def _validate_goal(goal: Any) -> dict[str, Any]:
    if not isinstance(goal, dict):
        _fail("goal must be a JSON object")
    if type(goal.get("version")) is not int or goal["version"] != 1:
        _fail("goal.version must equal 1")
    unknown = set(goal) - GOAL_REQUIRED
    if unknown:
        _fail(f"goal has unknown keys: {sorted(unknown)}")
    goal_id = _nonempty_text(goal.get("goal_id"), "goal.goal_id", maximum=256)
    goal_text = _nonempty_text(goal.get("goal"), "goal.goal", maximum=8000)
    workspace_raw = goal.get("workspace")
    if not isinstance(workspace_raw, str) or not workspace_raw.strip():
        _fail("goal.workspace must be a nonempty absolute path")
    workspace_path = Path(workspace_raw)
    posix_w = PurePosixPath(workspace_raw)
    windows_w = PureWindowsPath(workspace_raw)
    if not (posix_w.is_absolute() or windows_w.is_absolute()):
        _fail(f"goal.workspace must be absolute: {workspace_raw!r}")
    if not workspace_path.is_dir():
        _fail(f"goal.workspace must exist and be a directory: {workspace_raw}")
    requirements = _string_list(goal.get("requirements"), "goal.requirements")
    constraints = _string_list(goal.get("constraints"), "goal.constraints")
    allowed_scope_raw = goal.get("allowed_scope")
    if not isinstance(allowed_scope_raw, list) or not allowed_scope_raw:
        _fail("goal.allowed_scope must be a nonempty list of paths")
    allowed_scope: list[str] = []
    for index, item in enumerate(allowed_scope_raw):
        label = f"goal.allowed_scope[{index}]"
        if not isinstance(item, str) or not item:
            _fail(f"{label} must be a nonempty string")
        if item.endswith("/") or item.endswith("\\"):
            tree = _safe_relative_path(item[:-1], label)
            _resolve_under_workspace(tree, workspace_path, label)
            allowed_scope.append(tree + "/")
        else:
            safe = _safe_relative_path(item, label)
            _resolve_under_workspace(safe, workspace_path, label)
            allowed_scope.append(safe)
    return {
        "version": 1,
        "goal_id": goal_id,
        "goal": goal_text,
        "workspace": str(workspace_path),
        "requirements": requirements,
        "constraints": constraints,
        "allowed_scope": allowed_scope,
    }


def _validate_observation(observation: Any, expected_goal_id: str) -> dict[str, Any]:
    if not isinstance(observation, dict):
        _fail("observation must be a JSON object")
    unknown = set(observation) - OBSERVATION_REQUIRED
    if unknown:
        _fail(f"observation has unknown keys: {sorted(unknown)}")
    if observation.get("goal_id") != expected_goal_id:
        _fail("observation.goal_id does not match goal.goal_id")
    return {
        "goal_id": expected_goal_id,
        "stop": _bool(observation.get("stop"), "observation.stop"),
        "execution": _enum(observation.get("execution"), "observation.execution", EXECUTION),
        "delivery": _enum(observation.get("delivery"), "observation.delivery", DELIVERY),
        "session": _enum(observation.get("session"), "observation.session", SESSION),
        "wake_verified": _bool(observation.get("wake_verified"), "observation.wake_verified"),
        "quota": _enum(observation.get("quota"), "observation.quota", QUOTA),
        "final_acceptance": _bool(observation.get("final_acceptance"), "observation.final_acceptance"),
        "unmet": _string_list(observation.get("unmet", []), "observation.unmet", allow_empty=True),
        "ready_work": _bool(observation.get("ready_work"), "observation.ready_work"),
        "new_planning_gap": _bool(observation.get("new_planning_gap"), "observation.new_planning_gap"),
        "same_failed_result_count": _int_not_bool(
            observation.get("same_failed_result_count"),
            "observation.same_failed_result_count", 0, 10_000,
        ),
        "director_packet_unchanged": _bool(
            observation.get("director_packet_unchanged"), "observation.director_packet_unchanged"
        ),
    }


def _validate_task(task: Any, label: str, *, allowed_scope: list[str], workspace: Path,
                   requirement_pool: set[str]) -> None:
    if not isinstance(task, dict):
        _fail(f"{label} must be an object")
    # TODO's normal validator owns its extensible schema; this guard owns scope.
    task_id = _nonempty_text(task.get("id"), f"{label}.id", maximum=128)
    if not TASK_ID_RE.fullmatch(task_id):
        _fail(f"{label}.id must match {TASK_ID_RE.pattern}")
    _nonempty_text(task.get("text"), f"{label}.text", maximum=8000)
    mode = _enum(task.get("mode", "implement"), f"{label}.mode", TASK_MODES)
    covers = _string_list(task.get("covers"), f"{label}.covers")
    bad_covers = sorted(set(covers) - requirement_pool)
    if bad_covers:
        _fail(f"{label}.covers references requirements outside the proposal: {bad_covers}")
    scope = _string_list(task.get("scope"), f"{label}.scope")
    for index, entry in enumerate(scope):
        entry_label = f"{label}.scope[{index}]"
        _safe_relative_path(entry, entry_label)
        if not _scope_subset(entry, allowed_scope):
            _fail(f"{entry_label} is not within goal.allowed_scope")
        _resolve_under_workspace(entry, workspace, entry_label)
    inputs = task.get("inputs", [])
    if not isinstance(inputs, list):
        _fail(f"{label}.inputs must be a list")
    for index, entry in enumerate(inputs):
        entry_label = f"{label}.inputs[{index}]"
        _safe_relative_path(entry, entry_label)
        _resolve_under_workspace(entry, workspace, entry_label)
    _string_list(task.get("depends_on", []), f"{label}.depends_on", allow_empty=True)
    _string_list(task.get("done_when"), f"{label}.done_when")
    checks = task.get("checks", [])
    if mode == "implement":
        if not isinstance(checks, list) or not checks:
            _fail(f"{label}.checks must be a nonempty list of argv arrays for implement mode")
        for index, argv in enumerate(checks):
            if not isinstance(argv, list) or not argv or not all(isinstance(a,str) and a.strip() for a in argv):
                _fail(f"{label}.checks[{index}] must be a nonempty argv array")
    else:
        if not isinstance(checks, list):
            _fail(f"{label}.checks must be a list")
    for role in ("maker", "reviewer"):
        candidates = task.get(role)
        if not isinstance(candidates, list) or not candidates:
            _fail(f"{label}.{role} must be a nonempty list")
        for index, candidate in enumerate(candidates):
            clabel = f"{label}.{role}[{index}]"
            if not isinstance(candidate, dict):
                _fail(f"{clabel} must be an object")
            _nonempty_text(candidate.get("agent"), f"{clabel}.agent", maximum=256)
            _nonempty_text(candidate.get("model"), f"{clabel}.model", maximum=256)
            _nonempty_text(candidate.get("reason"), f"{clabel}.reason", maximum=2000)
            score = candidate.get("score")
            if isinstance(score, bool) or type(score) is not int or score < 0 or score > 100 or score % 5 != 0:
                _fail(f"{clabel}.score must be an integer 0..100 in steps of 5")
    requires_approval = task.get("requires_approval", [])
    if not isinstance(requires_approval, list):
        _fail(f"{label}.requires_approval must be a list")
    for index, item in enumerate(requires_approval):
        _nonempty_text(item, f"{label}.requires_approval[{index}]", maximum=256)


def _detect_cycle(task_ids: list[str], deps: dict[str, list[str]]) -> bool:
    in_degree = {tid: 0 for tid in task_ids}
    edges: dict[str, list[str]] = {tid: [] for tid in task_ids}
    for tid, dep_list in deps.items():
        for dep in dep_list:
            edges[dep].append(tid)
            in_degree[tid] += 1
    queue = [tid for tid in task_ids if in_degree[tid] == 0]
    visited = 0
    while queue:
        current = queue.pop()
        visited += 1
        for nxt in edges[current]:
            in_degree[nxt] -= 1
            if in_degree[nxt] == 0:
                queue.append(nxt)
    return visited != len(task_ids)


def next_action(goal: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
    """Return the next typed action for the active Office Loop session."""
    validated_goal = _validate_goal(goal)
    obs = _validate_observation(observation, validated_goal["goal_id"])
    if set(obs["unmet"]) - set(validated_goal["requirements"]):
        _fail("observation.unmet contains requirements outside the goal")
    if obs["final_acceptance"] and obs["unmet"]:
        _fail("final_acceptance contradicts unmet requirements")

    base = {
        "schema_version": SCHEMA_VERSION,
        "skill": "office-loop",
        "goal_id": validated_goal["goal_id"],
    }

    if obs["stop"]:
        return {**base, "decision": "stop",
                "reason": "STOP prevents any dispatch, Director call or model call"}
    if obs["quota"] == "exhausted":
        return {**base, "decision": "stop",
                "reason": "quota is exhausted; do not dispatch or call Director"}

    if obs["session"] != "active":
        if obs["execution"] != "terminal" or obs["delivery"] != "settled":
            return {**base, "decision": "reconcile_execution",
                    "reason": "non-active session cannot own an active execution; reconcile before any action"}
        if obs["session"] == "ended" and obs["ready_work"] and obs["wake_verified"]:
            return {**base, "decision": "request_verified_wake",
                    "reason": "ended session, concrete ready work, wake verified"}
        return {**base, "decision": "WAITING_NATIVE_WAKE",
                "reason": "session is not active and wake is not verified"}

    if obs["execution"] == "active":
        return {**base, "decision": "wait_on_existing_handle",
                "reason": "active execution; wait on its existing handle, do not dispatch a duplicate"}
    if obs["execution"] != "terminal" or obs["delivery"] != "settled":
        return {**base, "decision": "reconcile_execution",
                "reason": "execution or delivery is not settled; do not dispatch a duplicate"}

    if not obs["unmet"]:
        if obs["final_acceptance"]:
            return {**base, "decision": "complete",
                    "reason": "no unmet requirements and final acceptance attested"}
        return {**base, "decision": "final_acceptance_check",
                "reason": "no unmet requirements but final acceptance is not yet attested"}

    if obs["same_failed_result_count"] >= 2:
        return {**base, "decision": "diagnose",
                "reason": "two identical failed results require diagnosis before more dispatch"}

    if obs["ready_work"]:
        return {**base, "decision": "execute_current_batch",
                "reason": "ready work exists for the current batch"}

    if obs["new_planning_gap"] and not obs["director_packet_unchanged"]:
        return {**base, "decision": "consult_director",
                "reason": "new planning gap requires Director input"}
    if obs["new_planning_gap"] and obs["director_packet_unchanged"]:
        return {**base, "decision": "reuse_advice",
                "reason": "unchanged Director packet can be reused; do not call Director again"}

    if obs["ready_work"]:
        return {**base, "decision": "execute_current_batch",
                "reason": "ready work exists for the current batch"}

    return {**base, "decision": "wait",
            "reason": "no ready work, no new planning gap, no diagnosis; wait for fresh evidence"}


def validate_proposal(goal: dict[str, Any], proposal: dict[str, Any]) -> dict[str, Any]:
    """Validate a Director response as data; never grant authority or dispatch."""
    validated_goal = _validate_goal(goal)
    if not isinstance(proposal, dict):
        _fail("proposal must be a JSON object")
    decision = _enum(proposal.get("decision"), "proposal.decision", DECISION)
    if proposal.get("goal_id") != validated_goal["goal_id"]:
        _fail("proposal.goal_id does not match goal.goal_id")

    base = {
        "schema_version": SCHEMA_VERSION,
        "skill": "office-loop",
        "goal_id": validated_goal["goal_id"],
        "decision": decision,
        "authority_granted": False,
        "dispatch": False,
    }

    if decision in {"blocked", "complete"}:
        reason = _nonempty_text(proposal.get("reason"), f"proposal.reason for {decision}", maximum=2000)
        return {**base, "advisory_only": True, "reason": reason,
                "note": "blocked/complete is ADVICE_ONLY; the helper grants no authority"}

    todo = proposal.get("todo")
    if not isinstance(todo, dict):
        _fail("proposal.todo must be an object for next_batch")
    if type(todo.get("version")) is not int or todo["version"] != 1:
        _fail("proposal.todo.version must equal 1")
    if todo.get("goal") != validated_goal["goal"]:
        _fail("proposal.todo.goal does not preserve the exact original goal text")

    workspace_raw = todo.get("workspace")
    if not isinstance(workspace_raw, str) or not workspace_raw.strip():
        _fail("proposal.todo.workspace must be a nonempty absolute path")
    if not (PurePosixPath(workspace_raw).is_absolute() or PureWindowsPath(workspace_raw).is_absolute()):
        _fail(f"proposal.todo.workspace must be absolute: {workspace_raw!r}")
    workspace_path = Path(workspace_raw)
    if workspace_path.resolve() != Path(validated_goal["workspace"]).resolve():
        _fail("proposal.todo.workspace does not match the goal workspace")

    requirements = _string_list(todo.get("requirements"), "proposal.todo.requirements")
    proposal_pool = set(requirements)
    foreign = sorted(proposal_pool - set(validated_goal["requirements"]))
    if foreign:
        _fail(f"proposal.todo.requirements includes foreign requirements: {foreign}")

    constraints = _string_list(todo.get("constraints"), "proposal.todo.constraints")
    if not set(validated_goal["constraints"]).issubset(constraints):
        missing = sorted(set(validated_goal["constraints"]) - set(constraints))
        added = sorted(set(constraints) - set(validated_goal["constraints"]))
        _fail(f"proposal.todo.constraints must retain every original constraint "
              f"(missing={missing}; added={added})")

    planner = todo.get("planner")
    if not isinstance(planner, dict):
        _fail("proposal.todo.planner must be an object")
    _nonempty_text(planner.get("model"), "proposal.todo.planner.model", maximum=256)
    _nonempty_text(planner.get("source"), "proposal.todo.planner.source", maximum=512)

    _int_not_bool(todo.get("max_parallel"), "proposal.todo.max_parallel", 1, 256)

    tasks = todo.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        _fail("proposal.todo.tasks must be a nonempty list")

    task_ids: list[str] = []
    seen_ids: set[str] = set()
    deps_map: dict[str, list[str]] = {}
    approval_rejections: list[str] = []
    for index, task in enumerate(tasks):
        label = f"proposal.todo.tasks[{index}]"
        _validate_task(
            task, label,
            allowed_scope=validated_goal["allowed_scope"],
            workspace=workspace_path,
            requirement_pool=proposal_pool,
        )
        task_id = task["id"]
        if task_id in seen_ids:
            _fail(f"{label}.id duplicate: {task_id!r}")
        seen_ids.add(task_id)
        task_ids.append(task_id)
        deps_map[task_id] = list(task.get("depends_on", []))
        approval = task.get("requires_approval", [])
        if approval:
            approval_rejections.append(
                f"{label}.requires_approval is non-empty ({approval!r}); OWNER_DECISION_REQUIRED; "
                "the helper does not grant authority from a Director response"
            )

    covered: set[str] = set()
    for task in tasks:
        covered.update(task.get("covers", []))
    if covered != proposal_pool:
        missing = sorted(proposal_pool - covered)
        extra = sorted(covered - proposal_pool)
        _fail(f"task coverage mismatch (missing={missing}; extra={extra})")

    for index, task in enumerate(tasks):
        unknown = [d for d in task.get("depends_on", []) if d not in seen_ids or d == task["id"]]
        if unknown:
            _fail(f"proposal.todo.tasks[{index}].depends_on unknown or self: {unknown}")

    if _detect_cycle(task_ids, deps_map):
        _fail("task dependency graph contains a cycle")

    if approval_rejections:
        _fail("; ".join(approval_rejections))

    reason_raw = proposal.get("reason")
    if isinstance(reason_raw, str) and reason_raw.strip():
        reason_text = reason_raw.strip()[:2000]
    else:
        reason_text = "next_batch validated as data; leader must still perform semantic review"
    return {
        **base, "advisory_only": False, "reason": reason_text,
        "note": "validated data is not a dispatch or authorization; run todo_graph.py validate and "
                "the leader's semantic review before any dispatch",
    }


def _read_bounded_json(path: Path, label: str) -> Any:
    try:
        size = path.stat().st_size
    except OSError as exc:
        _fail(f"cannot read {label} {path}: {exc}")
    if size > MAX_INPUT_BYTES:
        _fail(f"{label} {path} is {size} bytes; limit is {MAX_INPUT_BYTES} bytes")
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            _fail(f"{label} exceeds limit {MAX_INPUT_BYTES}")
        text = raw.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        _fail(f"cannot decode {label} {path}: {exc}")
    try:
        def unique(pairs):
            result = {}
            for key,value in pairs:
                if key in result:
                    _fail(f"duplicate JSON key: {key}")
                result[key] = value
            return result
        return json.loads(text, object_pairs_hook=unique, parse_constant=lambda v: _fail(f"invalid JSON constant: {v}"))
    except json.JSONDecodeError as exc:
        _fail(f"{label} {path} is not valid JSON: {exc}")


def _emit(payload: dict[str, Any]) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write("\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loop_guard.py",
        description="Pure Office Loop helper: next_action and proposal validation.",
    )
    parser.add_argument("--version", action="version", version=f"loop_guard.py {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    decide = sub.add_parser("decide", help="Compute the next typed action for an observation.")
    decide.add_argument("--goal", type=Path, required=True)
    decide.add_argument("--observation", type=Path, required=True)

    validate = sub.add_parser("validate", help="Validate a Director proposal as data.")
    validate.add_argument("--goal", type=Path, required=True)
    validate.add_argument("--proposal", type=Path, required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "decide":
            goal = _read_bounded_json(args.goal, "goal file")
            observation = _read_bounded_json(args.observation, "observation file")
            payload = next_action(goal, observation)
        else:
            goal = _read_bounded_json(args.goal, "goal file")
            proposal = _read_bounded_json(args.proposal, "proposal file")
            payload = validate_proposal(goal, proposal)
    except GuardError as exc:
        json.dump(
            {"schema_version": SCHEMA_VERSION, "skill": "office-loop", "status": "ERROR",
             "code": "GUARD_REJECTED", "error": str(exc)},
            sys.stderr, ensure_ascii=False, separators=(",", ":"),
        )
        sys.stderr.write("\n")
        return 2
    _emit(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
