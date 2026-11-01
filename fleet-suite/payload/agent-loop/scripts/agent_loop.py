#!/usr/bin/env python3
"""Governed L1 control loops above Agent Fleet.

V0.1 is deliberately report-only. It owns cadence state, budgets, circuit
breakers, path claims, and an append-only evidence ledger. Agent Fleet remains
the only model router.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import security_contract as security


VERSION = "0.1.0"
SCHEMA_VERSION = 1
MAX_JSON_BYTES = 1_048_576
MAX_LEDGER_LINE_BYTES = 1_048_576
MAX_FLEET_RESULT_BYTES = 1_048_576
DEFAULT_SNAPSHOT_MAX_FILES = 20_000
DEFAULT_SNAPSHOT_MAX_BYTES = 1_073_741_824
LOOP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")
TRUTHY = {"1", "true", "yes", "on"}
BREAKER_REASONS = {
    "KILL_SWITCH",
    "MAX_ATTEMPTS",
    "REPEATED_FAILURE",
    "NO_PROGRESS",
    "TIMEOUT",
    "BUDGET_EXCEEDED",
    "TELEMETRY_UNKNOWN",
    "L1_WRITE_DETECTED",
    "CONTROL_PLANE_WRITE_DETECTED",
    "INTERRUPTED",
}
RUN_STATUSES = {
    "completed",
    "failed",
    "timeout",
    "stopped",
    "skipped-budget",
    "skipped-kill",
    "rejected-write",
    "rejected-control-plane",
}


class AgentLoopError(Exception):
    """Expected, operator-facing refusal."""


@dataclass(frozen=True)
class LoopPaths:
    project: Path
    root: Path
    contract: Path
    gate: Path
    state: Path
    ledger: Path
    reports: Path
    loop_lock: Path
    claims_root: Path
    claims_lock: Path


def utc_now() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def event_id(prefix: str = "evt") -> str:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:10]}"


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AgentLoopError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path: Path, label: str, *, required: bool = True) -> Any:
    if not path.is_file():
        if required:
            raise AgentLoopError(f"{label} does not exist: {path}")
        return None
    if path.stat().st_size > MAX_JSON_BYTES:
        raise AgentLoopError(f"{label} exceeds {MAX_JSON_BYTES} bytes: {path}")
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise AgentLoopError(f"{label} must not contain a UTF-8 BOM: {path}")
    if b"\x00" in raw:
        raise AgentLoopError(f"{label} contains NUL bytes: {path}")
    try:
        return json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_no_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AgentLoopError(f"{label} is invalid UTF-8 JSON: {path}: {exc}") from exc


def require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AgentLoopError(f"{label} must be a JSON object")
    return value


def require_exact_keys(
    value: Mapping[str, Any],
    *,
    label: str,
    required: Iterable[str],
    optional: Iterable[str] = (),
) -> None:
    required_set = set(required)
    allowed = required_set | set(optional)
    missing = sorted(required_set - set(value))
    unknown = sorted(set(value) - allowed)
    if missing:
        raise AgentLoopError(f"{label} missing fields: {', '.join(missing)}")
    if unknown:
        raise AgentLoopError(f"{label} has unknown fields: {', '.join(unknown)}")


def ensure_positive_int(
    value: Any,
    label: str,
    *,
    minimum: int = 1,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AgentLoopError(f"{label} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        suffix = f"..{maximum}" if maximum is not None else " or greater"
        raise AgentLoopError(f"{label} must be {minimum}{suffix}")
    return value


def ensure_nullable_string(value: Any, label: str) -> None:
    if value is not None and not isinstance(value, str):
        raise AgentLoopError(f"{label} must be a string or null")


def ensure_timestamp(value: Any, label: str, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str):
        raise AgentLoopError(f"{label} must be an ISO-8601 timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AgentLoopError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise AgentLoopError(f"{label} must include a timezone")


def resolve_project(value: str | Path) -> Path:
    project = Path(value).expanduser().resolve(strict=True)
    if not project.is_dir():
        raise AgentLoopError(f"Project is not a directory: {project}")
    return project


def build_paths(project: Path, loop_id: str) -> LoopPaths:
    if not LOOP_ID_RE.fullmatch(loop_id):
        raise AgentLoopError(f"Invalid loop_id: {loop_id!r}")
    loops_root = security.resolve_under_root(
        project,
        project / ".agents" / "loops",
    )
    root = security.resolve_under_root(
        project,
        security.loop_state_root(project, loop_id),
    )
    return LoopPaths(
        project=project,
        root=root,
        contract=root / "loop.json",
        gate=root / "gate.json",
        state=root / "state.json",
        ledger=root / "runs.jsonl",
        reports=root / "reports",
        loop_lock=root / "loop.lock",
        claims_root=loops_root / ".claims",
        claims_lock=loops_root / ".claims.lock",
    )


def _normalize_relative_pattern(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AgentLoopError(f"{label} must be a non-empty relative path or glob")
    normalized = value.replace("\\", "/").strip()
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise AgentLoopError(f"{label} must be relative: {value!r}")
    parts = [part for part in normalized.split("/") if part not in {"", "."}]
    if ".." in parts:
        raise AgentLoopError(f"{label} must not contain '..': {value!r}")
    return "/".join(parts) or "."


def resolve_loop_file(paths: LoopPaths, relative: str, label: str) -> Path:
    normalized = _normalize_relative_pattern(relative, label)
    if any(char in normalized for char in "*?[]"):
        raise AgentLoopError(f"{label} must be a concrete file path")
    candidate = security.resolve_under_root(paths.project, paths.root / normalized)
    if not candidate.is_relative_to(paths.root.resolve(strict=False)):
        raise AgentLoopError(f"{label} must remain inside the loop directory")
    if not candidate.is_file():
        raise AgentLoopError(f"{label} does not exist: {candidate}")
    return candidate


def validate_task_file(path: Path) -> None:
    if path.stat().st_size > MAX_JSON_BYTES:
        raise AgentLoopError(f"delegation.task_file exceeds {MAX_JSON_BYTES} bytes")
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf") or b"\x00" in raw:
        raise AgentLoopError("delegation.task_file contains a BOM or NUL")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise AgentLoopError("delegation.task_file must be UTF-8") from exc
    if not text.strip():
        raise AgentLoopError("delegation.task_file must not be empty")


def resolve_kill_file(paths: LoopPaths, relative: str) -> Path:
    normalized = _normalize_relative_pattern(relative, "kill_switch.file")
    if any(char in normalized for char in "*?[]"):
        raise AgentLoopError("kill_switch.file must be a concrete path")
    candidate = security.resolve_under_root(paths.project, paths.root / normalized)
    if not candidate.is_relative_to(paths.root.resolve(strict=False)):
        raise AgentLoopError("kill_switch.file must remain inside the loop directory")
    return candidate


def validate_gate(paths: LoopPaths, gate: Mapping[str, Any]) -> None:
    require_exact_keys(
        gate,
        label="gate.json",
        required={"schema_version", "loop_id", "verifier_default", "criteria", "gates"},
    )
    if gate["schema_version"] != SCHEMA_VERSION:
        raise AgentLoopError("gate.json schema_version must be 1")
    if gate["loop_id"] != paths.root.name:
        raise AgentLoopError("gate.json loop_id must match the directory name")
    if gate["verifier_default"] != "REJECT":
        raise AgentLoopError("gate.json verifier_default must be REJECT")
    if not isinstance(gate["criteria"], list) or not gate["criteria"]:
        raise AgentLoopError("gate.json criteria must be a non-empty array")
    criterion_ids: set[str] = set()
    for index, item in enumerate(gate["criteria"]):
        criterion = require_object(item, f"gate.criteria[{index}]")
        require_exact_keys(
            criterion,
            label=f"gate.criteria[{index}]",
            required={"id", "description", "check_kind", "required"},
        )
        criterion_id = criterion["id"]
        if not isinstance(criterion_id, str) or not criterion_id:
            raise AgentLoopError(f"gate.criteria[{index}].id is invalid")
        if criterion_id in criterion_ids:
            raise AgentLoopError(f"Duplicate gate criterion: {criterion_id}")
        criterion_ids.add(criterion_id)
        if (
            not isinstance(criterion["description"], str)
            or not criterion["description"].strip()
        ):
            raise AgentLoopError(f"gate.criteria[{index}].description is invalid")
        if criterion["check_kind"] not in {
            "deterministic",
            "test-suite",
            "parent-review",
        }:
            raise AgentLoopError(f"gate.criteria[{index}].check_kind is invalid")
        if not isinstance(criterion["required"], bool):
            raise AgentLoopError(f"gate.criteria[{index}].required must be boolean")
    if not isinstance(gate["gates"], list):
        raise AgentLoopError("gate.json gates must be an array")
    gate_ids: set[str] = set()
    for index, item in enumerate(gate["gates"]):
        current = require_object(item, f"gate.gates[{index}]")
        require_exact_keys(
            current,
            label=f"gate.gates[{index}]",
            required={"id", "kind", "status"},
            optional={"approver", "decided_at", "note"},
        )
        gate_id = current["id"]
        if not isinstance(gate_id, str) or not gate_id or gate_id in gate_ids:
            raise AgentLoopError(f"gate.gates[{index}].id is invalid or duplicate")
        gate_ids.add(gate_id)
        if current["kind"] not in {"human", "automated"}:
            raise AgentLoopError(f"gate.gates[{index}].kind is invalid")
        if current["status"] not in {
            "pending",
            "approved",
            "rejected",
            "not-applicable",
        }:
            raise AgentLoopError(f"gate.gates[{index}].status is invalid")
        ensure_nullable_string(
            current.get("approver"),
            f"gate.gates[{index}].approver",
        )
        ensure_timestamp(
            current.get("decided_at"),
            f"gate.gates[{index}].decided_at",
            nullable=True,
        )
        ensure_nullable_string(current.get("note"), f"gate.gates[{index}].note")
        if current["status"] in {"approved", "rejected"} and (
            not current.get("approver") or current.get("decided_at") is None
        ):
            raise AgentLoopError(
                f"gate.gates[{index}] decision requires approver and decided_at"
            )


def validate_contract(
    paths: LoopPaths,
    contract: Mapping[str, Any],
    gate: Mapping[str, Any],
) -> dict[str, Any]:
    errors = security.validate_loop_json(contract)
    if errors:
        raise AgentLoopError("loop.json validation failed: " + ", ".join(errors))
    validate_gate(paths, gate)
    if contract["loop_id"] != paths.root.name:
        raise AgentLoopError("loop.json loop_id must match the directory name")
    if len(contract["goal"]) > 1000:
        raise AgentLoopError("goal exceeds 1000 characters")
    if contract["maturity"] != "L1":
        raise AgentLoopError("agent-loop V0.1 supports only maturity L1")
    if not isinstance(contract["non_goals"], list) or not all(
        isinstance(item, str) for item in contract["non_goals"]
    ):
        raise AgentLoopError("non_goals must contain only strings")

    watched_scope = contract["watched_scope"]
    if not all(isinstance(item, str) for item in watched_scope):
        raise AgentLoopError("watched_scope must contain only strings")
    normalized_watched = [
        _normalize_relative_pattern(item, "watched_scope") for item in watched_scope
    ]

    cadence = require_object(contract["cadence"], "cadence")
    require_exact_keys(
        cadence,
        label="cadence",
        required={"kind"},
        optional={"cron", "interval_seconds"},
    )
    cadence_kind = cadence["kind"]
    if cadence_kind == "interval":
        ensure_positive_int(
            cadence.get("interval_seconds"), "cadence.interval_seconds", minimum=60
        )
        if "cron" in cadence:
            raise AgentLoopError("interval cadence must not define cron")
    elif cadence_kind == "cron":
        if not isinstance(cadence.get("cron"), str) or not cadence["cron"].strip():
            raise AgentLoopError("cadence.cron is required for cron cadence")
        if "interval_seconds" in cadence:
            raise AgentLoopError("cron cadence must not define interval_seconds")
    elif cadence_kind != "manual":
        raise AgentLoopError("cadence.kind must be manual, interval, or cron")
    elif set(cadence) != {"kind"}:
        raise AgentLoopError("manual cadence must contain only kind")

    timezone = contract["timezone"]
    if not isinstance(timezone, str):
        raise AgentLoopError("timezone must be a string")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise AgentLoopError(f"Unknown timezone: {timezone}") from exc

    delegation = require_object(contract["delegation"], "delegation")
    require_exact_keys(
        delegation,
        label="delegation",
        required={
            "via",
            "mode",
            "agent",
            "kind",
            "complexity",
            "task_file",
            "assessment_file",
            "enable_web",
        },
    )
    if delegation["via"] != "agent-fleet":
        raise AgentLoopError("delegation.via must be agent-fleet")
    if delegation["mode"] not in {"advisory", "review"}:
        raise AgentLoopError("L1 delegation.mode must be advisory or review")
    if not isinstance(delegation["agent"], str) or not delegation["agent"]:
        raise AgentLoopError("delegation.agent must be a non-empty string")
    if delegation["kind"] not in {
        "auto",
        "architecture",
        "debugging",
        "frontend",
        "implementation",
        "research",
        "review",
    }:
        raise AgentLoopError("delegation.kind is invalid")
    if delegation["complexity"] not in {"low", "medium", "high"}:
        raise AgentLoopError("delegation.complexity is invalid")
    if not isinstance(delegation["enable_web"], bool):
        raise AgentLoopError("delegation.enable_web must be boolean")
    task_file = resolve_loop_file(
        paths, delegation["task_file"], "delegation.task_file"
    )
    assessment_file = resolve_loop_file(
        paths,
        delegation["assessment_file"],
        "delegation.assessment_file",
    )
    validate_task_file(task_file)
    require_object(
        load_json(assessment_file, "delegation.assessment_file"),
        "delegation.assessment_file",
    )

    limits = require_object(contract["limits"], "limits")
    require_exact_keys(
        limits,
        label="limits",
        required={"max_attempts_per_run", "run_timeout_seconds", "budget"},
        optional={"snapshot_max_files", "snapshot_max_bytes"},
    )
    max_attempts = ensure_positive_int(
        limits["max_attempts_per_run"],
        "limits.max_attempts_per_run",
        maximum=1,
    )
    timeout = ensure_positive_int(
        limits["run_timeout_seconds"],
        "limits.run_timeout_seconds",
        minimum=30,
        maximum=7200,
    )
    snapshot_max_files = ensure_positive_int(
        limits.get("snapshot_max_files", DEFAULT_SNAPSHOT_MAX_FILES),
        "limits.snapshot_max_files",
        maximum=100_000,
    )
    snapshot_max_bytes = ensure_positive_int(
        limits.get("snapshot_max_bytes", DEFAULT_SNAPSHOT_MAX_BYTES),
        "limits.snapshot_max_bytes",
    )
    budget = require_object(limits["budget"], "limits.budget")
    require_exact_keys(
        budget,
        label="limits.budget",
        required={
            "runs_per_day",
            "tokens_per_run",
            "tokens_per_day",
            "unknown_token_policy",
        },
        optional={"token_policy"},
    )
    runs_per_day = ensure_positive_int(
        budget["runs_per_day"],
        "limits.budget.runs_per_day",
        maximum=10_000,
    )
    for key in ("tokens_per_run", "tokens_per_day"):
        value = budget[key]
        if value is not None:
            ensure_positive_int(value, f"limits.budget.{key}", minimum=0)
    if budget["unknown_token_policy"] not in {"stop", "allow"}:
        raise AgentLoopError("limits.budget.unknown_token_policy is invalid")
    token_policy = budget.get("token_policy", "hard")
    if token_policy not in {"hard", "estimate"}:
        raise AgentLoopError("limits.budget.token_policy is invalid")

    stop_conditions = require_object(contract["stop_conditions"], "stop_conditions")
    require_exact_keys(
        stop_conditions,
        label="stop_conditions",
        required={"no_progress_runs", "repeated_failure_runs", "budget_stop"},
    )
    no_progress_runs = ensure_positive_int(
        stop_conditions["no_progress_runs"],
        "stop_conditions.no_progress_runs",
    )
    repeated_failure_runs = ensure_positive_int(
        stop_conditions["repeated_failure_runs"],
        "stop_conditions.repeated_failure_runs",
    )
    if not isinstance(stop_conditions["budget_stop"], bool):
        raise AgentLoopError("stop_conditions.budget_stop must be boolean")

    if (
        not isinstance(contract["human_gates"], list)
        or not all(
            isinstance(item, str)
            and item in {"L2_promotion", "mutation_approval", "scope_expansion"}
            for item in contract["human_gates"]
        )
        or len(contract["human_gates"]) != len(set(contract["human_gates"]))
    ):
        raise AgentLoopError("human_gates must be an array")
    declared_gates = {
        str(item["id"]): item.get("kind")
        for item in gate["gates"]
        if isinstance(item, dict) and "id" in item
    }
    missing_gates = sorted(set(contract["human_gates"]) - set(declared_gates))
    if missing_gates:
        raise AgentLoopError(
            "gate.json is missing declared human gates: " + ", ".join(missing_gates)
        )
    non_human = sorted(
        gate_id
        for gate_id in contract["human_gates"]
        if declared_gates.get(gate_id) != "human"
    )
    if non_human:
        raise AgentLoopError(
            "declared human gates must use kind=human: " + ", ".join(non_human)
        )
    kill_switch = require_object(contract["kill_switch"], "kill_switch")
    require_exact_keys(
        kill_switch,
        label="kill_switch",
        required={"file", "env"},
    )
    kill_file = resolve_kill_file(paths, kill_switch["file"])
    if not isinstance(kill_switch["env"], str) or not ENV_NAME_RE.fullmatch(
        kill_switch["env"]
    ):
        raise AgentLoopError("kill_switch.env must be an uppercase environment name")

    retention = require_object(contract["retention"], "retention")
    require_exact_keys(
        retention,
        label="retention",
        required={"runs_keep_days", "reports_keep_days"},
    )
    ensure_positive_int(retention["runs_keep_days"], "retention.runs_keep_days")
    ensure_positive_int(retention["reports_keep_days"], "retention.reports_keep_days")

    write_policy = require_object(contract["write_policy"], "write_policy")
    require_exact_keys(
        write_policy,
        label="write_policy",
        required={"allowed_write_areas", "path_claims", "forbid_watched_writes"},
    )
    if write_policy["forbid_watched_writes"] is not True:
        raise AgentLoopError("L1 requires write_policy.forbid_watched_writes=true")
    if (
        not isinstance(write_policy["allowed_write_areas"], list)
        or not write_policy["allowed_write_areas"]
    ):
        raise AgentLoopError("allowed_write_areas must be a non-empty array")
    normalized_write_areas = [
        _normalize_relative_pattern(item, "allowed_write_areas")
        for item in write_policy["allowed_write_areas"]
    ]
    expected_prefix = paths.root.relative_to(paths.project).as_posix()
    for area in normalized_write_areas:
        stable = area.removesuffix("/**")
        if stable != expected_prefix:
            raise AgentLoopError(
                "L1 allowed_write_areas may contain only the current loop directory"
            )
    if not isinstance(write_policy["path_claims"], list):
        raise AgentLoopError("path_claims must be an array")
    normalized_claims = [
        security.normalize_claim(paths.project, str(item))
        for item in write_policy["path_claims"]
    ]

    return {
        "contract": dict(contract),
        "gate": dict(gate),
        "watched_scope": normalized_watched,
        "claims": normalized_claims,
        "task_file": task_file,
        "assessment_file": assessment_file,
        "kill_file": kill_file,
        "timezone": ZoneInfo(timezone),
        "max_attempts": max_attempts,
        "timeout": timeout,
        "snapshot_max_files": snapshot_max_files,
        "snapshot_max_bytes": snapshot_max_bytes,
        "runs_per_day": runs_per_day,
        "tokens_per_run": budget["tokens_per_run"],
        "tokens_per_day": budget["tokens_per_day"],
        "unknown_token_policy": budget["unknown_token_policy"],
        "token_policy": token_policy,
        "budget_stop": stop_conditions["budget_stop"],
        "no_progress_runs": no_progress_runs,
        "repeated_failure_runs": repeated_failure_runs,
    }


def load_validated(
    project_value: str | Path, loop_id: str
) -> tuple[LoopPaths, dict[str, Any]]:
    project = resolve_project(project_value)
    paths = build_paths(project, loop_id)
    security.resolve_under_root(paths.project, paths.contract)
    security.resolve_under_root(paths.project, paths.gate)
    contract = require_object(load_json(paths.contract, "loop.json"), "loop.json")
    gate = require_object(load_json(paths.gate, "gate.json"), "gate.json")
    return paths, validate_contract(paths, contract, gate)


def default_state(loop_id: str, timezone: ZoneInfo, lock_path: Path) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "loop_id": loop_id,
        "phase": "IDLE",
        "updated_at": utc_now(),
        "current_run_id": None,
        "current_started_at": None,
        "attempt": 0,
        "breaker": {"open": False, "reason": None, "opened_at": None},
        "progress": {
            "marker": None,
            "last_change_run_id": None,
            "unchanged_runs": 0,
        },
        "failure": {"consecutive": 0, "last_error_kind": None},
        "budget": {
            "day": dt.datetime.now(timezone).date().isoformat(),
            "runs_today": 0,
            "tokens_today": 0,
            "unknown_token_runs": 0,
        },
        "settled_run_ids": [],
        "lock": {
            "pid": None,
            "host": None,
            "acquired_at": None,
            "lock_file": str(lock_path),
        },
        "last_run_id": None,
        "last_fleet_run_id": None,
        "last_status": None,
    }


def validate_state(state: Mapping[str, Any], loop_id: str) -> None:
    required = {
        "schema_version",
        "loop_id",
        "phase",
        "updated_at",
        "current_run_id",
        "current_started_at",
        "attempt",
        "breaker",
        "progress",
        "failure",
        "budget",
        "settled_run_ids",
        "lock",
        "last_run_id",
        "last_fleet_run_id",
        "last_status",
    }
    require_exact_keys(state, label="state.json", required=required)
    if state["schema_version"] != SCHEMA_VERSION or state["loop_id"] != loop_id:
        raise AgentLoopError("state.json identity does not match the loop")
    if state["phase"] not in {
        "IDLE",
        "TRIGGERED",
        "VALIDATING",
        "PLANNING",
        "DELEGATING",
        "VERIFYING",
        "RECORDING",
        "RECOVERING",
        "STOPPED",
    }:
        raise AgentLoopError("state.json phase is invalid")
    ensure_timestamp(state["updated_at"], "state.updated_at")
    ensure_nullable_string(state["current_run_id"], "state.current_run_id")
    ensure_timestamp(
        state["current_started_at"],
        "state.current_started_at",
        nullable=True,
    )
    ensure_positive_int(state["attempt"], "state.attempt", minimum=0)

    breaker = require_object(state["breaker"], "state.breaker")
    require_exact_keys(
        breaker,
        label="state.breaker",
        required={"open", "reason", "opened_at"},
    )
    if not isinstance(breaker["open"], bool):
        raise AgentLoopError("state.breaker.open must be boolean")
    if breaker["reason"] not in BREAKER_REASONS | {None}:
        raise AgentLoopError("state.breaker.reason is invalid")
    ensure_timestamp(
        breaker["opened_at"],
        "state.breaker.opened_at",
        nullable=True,
    )
    if breaker["open"] and (breaker["reason"] is None or breaker["opened_at"] is None):
        raise AgentLoopError("an open state.breaker requires reason and opened_at")
    if not breaker["open"] and (
        breaker["reason"] is not None or breaker["opened_at"] is not None
    ):
        raise AgentLoopError("a closed state.breaker must not retain open metadata")

    progress = require_object(state["progress"], "state.progress")
    require_exact_keys(
        progress,
        label="state.progress",
        required={"marker", "last_change_run_id", "unchanged_runs"},
    )
    ensure_nullable_string(progress["marker"], "state.progress.marker")
    ensure_nullable_string(
        progress["last_change_run_id"],
        "state.progress.last_change_run_id",
    )
    ensure_positive_int(
        progress["unchanged_runs"],
        "state.progress.unchanged_runs",
        minimum=0,
    )

    failure = require_object(state["failure"], "state.failure")
    require_exact_keys(
        failure,
        label="state.failure",
        required={"consecutive", "last_error_kind"},
    )
    ensure_positive_int(
        failure["consecutive"],
        "state.failure.consecutive",
        minimum=0,
    )
    ensure_nullable_string(
        failure["last_error_kind"],
        "state.failure.last_error_kind",
    )

    budget = require_object(state["budget"], "state.budget")
    require_exact_keys(
        budget,
        label="state.budget",
        required={"day", "runs_today", "tokens_today", "unknown_token_runs"},
    )
    if not isinstance(budget["day"], str):
        raise AgentLoopError("state.budget.day must be YYYY-MM-DD")
    try:
        dt.date.fromisoformat(budget["day"])
    except ValueError as exc:
        raise AgentLoopError("state.budget.day must be YYYY-MM-DD") from exc
    for key in ("runs_today", "tokens_today", "unknown_token_runs"):
        ensure_positive_int(
            budget[key],
            f"state.budget.{key}",
            minimum=0,
        )

    settled = state["settled_run_ids"]
    if (
        not isinstance(settled, list)
        or not all(isinstance(item, str) and item for item in settled)
        or len(settled) != len(set(settled))
    ):
        raise AgentLoopError("state.json settled_run_ids must be unique")

    lock = require_object(state["lock"], "state.lock")
    require_exact_keys(
        lock,
        label="state.lock",
        required={"pid", "host", "acquired_at", "lock_file"},
    )
    if lock["pid"] is not None and (
        isinstance(lock["pid"], bool) or not isinstance(lock["pid"], int)
    ):
        raise AgentLoopError("state.lock.pid must be an integer or null")
    ensure_nullable_string(lock["host"], "state.lock.host")
    ensure_timestamp(
        lock["acquired_at"],
        "state.lock.acquired_at",
        nullable=True,
    )
    if not isinstance(lock["lock_file"], str):
        raise AgentLoopError("state.lock.lock_file must be a string")
    for key in ("last_run_id", "last_fleet_run_id", "last_status"):
        ensure_nullable_string(state[key], f"state.{key}")


def load_state(
    paths: LoopPaths, config: Mapping[str, Any]
) -> tuple[dict[str, Any], bool]:
    security.resolve_under_root(paths.project, paths.state)
    existing = load_json(paths.state, "state.json", required=False)
    if existing is None:
        return (
            default_state(paths.root.name, config["timezone"], paths.loop_lock),
            False,
        )
    state = require_object(existing, "state.json")
    validate_state(state, paths.root.name)
    return state, True


def save_state(
    paths: LoopPaths, state: Mapping[str, Any], config: Mapping[str, Any]
) -> None:
    security.assert_l1_write_allowed(
        paths.project,
        paths.root.name,
        paths.state,
        watched_globs=config["watched_scope"],
    )
    payload = dict(state)
    payload["updated_at"] = utc_now()
    validate_state(payload, paths.root.name)
    security.atomic_write_json(paths.state, payload)


def validate_ledger_event(
    event: Mapping[str, Any],
    *,
    loop_id: str,
    label: str,
) -> None:
    event_type = event.get("event_type")
    common_strings = ("event_id", "run_id")
    for key in common_strings:
        if not isinstance(event.get(key), str) or not event[key]:
            raise AgentLoopError(f"{label}.{key} must be a non-empty string")
    if event.get("schema_version") != SCHEMA_VERSION:
        raise AgentLoopError(f"{label}.schema_version must be 1")

    if event_type == "run":
        require_exact_keys(
            event,
            label=label,
            required={
                "schema_version",
                "event_id",
                "event_type",
                "run_id",
                "loop_id",
                "started_at",
                "ended_at",
                "status",
                "fleet",
                "verification",
                "budget_charged",
                "breaker_event",
                "writes_performed",
                "report_path",
            },
        )
        if event["loop_id"] != loop_id:
            raise AgentLoopError(f"{label}.loop_id does not match the loop")
        ensure_timestamp(event["started_at"], f"{label}.started_at")
        ensure_timestamp(event["ended_at"], f"{label}.ended_at")
        if event["status"] not in RUN_STATUSES:
            raise AgentLoopError(f"{label}.status is invalid")

        fleet = require_object(event["fleet"], f"{label}.fleet")
        require_exact_keys(
            fleet,
            label=f"{label}.fleet",
            required={
                "fleet_run_id",
                "agent",
                "model",
                "tokens",
                "quota_before",
                "quota_after",
                "duration_seconds",
                "run_status",
            },
        )
        for key in ("fleet_run_id", "agent", "model", "run_status"):
            ensure_nullable_string(fleet[key], f"{label}.fleet.{key}")
        tokens = fleet["tokens"]
        if tokens != "unknown" and (
            isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0
        ):
            raise AgentLoopError(f"{label}.fleet.tokens is invalid")
        for key in ("quota_before", "quota_after"):
            if not isinstance(fleet[key], (dict, str, type(None))):
                raise AgentLoopError(f"{label}.fleet.{key} is invalid")
        duration = fleet["duration_seconds"]
        if duration is not None and (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or duration < 0
        ):
            raise AgentLoopError(f"{label}.fleet.duration_seconds is invalid")

        verification = require_object(
            event["verification"],
            f"{label}.verification",
        )
        require_exact_keys(
            verification,
            label=f"{label}.verification",
            required={"verdict", "reasons"},
        )
        if (
            verification["verdict"] != "REJECT"
            or not isinstance(verification["reasons"], list)
            or not all(isinstance(item, str) for item in verification["reasons"])
        ):
            raise AgentLoopError(f"{label}.verification is invalid")

        budget = require_object(event["budget_charged"], f"{label}.budget_charged")
        require_exact_keys(
            budget,
            label=f"{label}.budget_charged",
            required={
                "run_id_settled",
                "tokens_counted",
                "tokens_unknown",
                "runs_today",
                "tokens_today",
            },
            optional={"warnings"},
        )
        if not isinstance(budget["run_id_settled"], bool) or not isinstance(
            budget["tokens_unknown"], bool
        ):
            raise AgentLoopError(f"{label}.budget_charged booleans are invalid")
        for key in ("tokens_counted", "runs_today", "tokens_today"):
            ensure_positive_int(
                budget[key],
                f"{label}.budget_charged.{key}",
                minimum=0,
            )
        if "warnings" in budget and (
            not isinstance(budget["warnings"], list)
            or not all(isinstance(item, str) for item in budget["warnings"])
        ):
            raise AgentLoopError(f"{label}.budget_charged.warnings is invalid")

        breaker = event["breaker_event"]
        if breaker is not None:
            breaker = require_object(breaker, f"{label}.breaker_event")
            require_exact_keys(
                breaker,
                label=f"{label}.breaker_event",
                required={"reason"},
            )
            if breaker["reason"] not in BREAKER_REASONS:
                raise AgentLoopError(f"{label}.breaker_event.reason is invalid")
        ensure_positive_int(
            event["writes_performed"],
            f"{label}.writes_performed",
            minimum=0,
        )
        ensure_nullable_string(event["report_path"], f"{label}.report_path")
        return

    if event_type == "acceptance":
        require_exact_keys(
            event,
            label=label,
            required={
                "schema_version",
                "event_id",
                "event_type",
                "run_id",
                "fleet_run_id",
                "recorded_at",
                "verified_by",
                "quality",
                "criteria_pass",
                "verdict",
                "note",
            },
        )
        for key in ("fleet_run_id", "verified_by", "note"):
            if not isinstance(event[key], str):
                raise AgentLoopError(f"{label}.{key} must be a string")
        if not event["fleet_run_id"] or not event["verified_by"]:
            raise AgentLoopError(f"{label} identifiers must be non-empty")
        ensure_timestamp(event["recorded_at"], f"{label}.recorded_at")
        ensure_positive_int(
            event["quality"],
            f"{label}.quality",
            minimum=0,
            maximum=100,
        )
        if not isinstance(event["criteria_pass"], bool):
            raise AgentLoopError(f"{label}.criteria_pass must be boolean")
        expected = "ACCEPT" if event["criteria_pass"] else "REJECT"
        if event["verdict"] != expected:
            raise AgentLoopError(f"{label}.verdict conflicts with criteria_pass")
        return

    raise AgentLoopError(f"{label}.event_type is invalid")


def read_ledger(paths: LoopPaths) -> list[dict[str, Any]]:
    security.resolve_under_root(paths.project, paths.ledger)
    if not paths.ledger.is_file():
        return []
    events: list[dict[str, Any]] = []
    seen_events: set[str] = set()
    seen_runs: set[str] = set()
    accepted_runs: set[str] = set()
    fleet_runs: dict[str, str | None] = {}
    with paths.ledger.open("rb") as handle:
        for line_number, raw in enumerate(handle, start=1):
            if len(raw) > MAX_LEDGER_LINE_BYTES:
                raise AgentLoopError(
                    f"runs.jsonl line {line_number} exceeds the size limit"
                )
            if not raw.strip():
                continue
            try:
                event = json.loads(
                    raw.decode("utf-8", errors="strict"),
                    object_pairs_hook=_no_duplicate_keys,
                )
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise AgentLoopError(
                    f"runs.jsonl line {line_number} is invalid: {exc}"
                ) from exc
            if not isinstance(event, dict):
                raise AgentLoopError(f"runs.jsonl line {line_number} is not an object")
            validate_ledger_event(
                event,
                loop_id=paths.root.name,
                label=f"runs.jsonl line {line_number}",
            )
            current_event_id = event.get("event_id")
            if not isinstance(current_event_id, str) or current_event_id in seen_events:
                raise AgentLoopError(
                    f"runs.jsonl line {line_number} has an invalid/duplicate event_id"
                )
            seen_events.add(current_event_id)
            if event.get("event_type") == "run":
                run_id = event.get("run_id")
                if not isinstance(run_id, str) or run_id in seen_runs:
                    raise AgentLoopError(
                        f"runs.jsonl line {line_number} has a duplicate run event"
                    )
                seen_runs.add(run_id)
                fleet = require_object(
                    event["fleet"],
                    f"runs.jsonl line {line_number}.fleet",
                )
                fleet_runs[run_id] = fleet["fleet_run_id"]
            elif event.get("event_type") == "acceptance":
                run_id = str(event["run_id"])
                if run_id not in seen_runs:
                    raise AgentLoopError(
                        f"runs.jsonl line {line_number} accepts an unknown run"
                    )
                if run_id in accepted_runs:
                    raise AgentLoopError(
                        f"runs.jsonl line {line_number} duplicates acceptance"
                    )
                if event["fleet_run_id"] != fleet_runs[run_id]:
                    raise AgentLoopError(
                        f"runs.jsonl line {line_number} has a mismatched Fleet run ID"
                    )
                accepted_runs.add(run_id)
            events.append(event)
    return events


def append_event(
    paths: LoopPaths,
    config: Mapping[str, Any],
    event: Mapping[str, Any],
) -> None:
    validate_ledger_event(
        event,
        loop_id=paths.root.name,
        label="new ledger event",
    )
    security.assert_l1_write_allowed(
        paths.project,
        paths.root.name,
        paths.ledger,
        watched_globs=config["watched_scope"],
    )
    security.append_jsonl(paths.ledger, event)


def local_day(value: str, timezone: ZoneInfo) -> str:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(timezone).date().isoformat()


def reconcile_budget(
    state: dict[str, Any],
    events: Sequence[Mapping[str, Any]],
    timezone: ZoneInfo,
) -> bool:
    today = dt.datetime.now(timezone).date().isoformat()
    run_events = [event for event in events if event.get("event_type") == "run"]
    settled = [str(event["run_id"]) for event in run_events]
    runs_today = 0
    tokens_today = 0
    unknown_today = 0
    for event in run_events:
        ended_at = event.get("ended_at")
        if not isinstance(ended_at, str) or local_day(ended_at, timezone) != today:
            continue
        runs_today += 1
        charged = event.get("budget_charged")
        if isinstance(charged, dict):
            tokens_today += int(charged.get("tokens_counted") or 0)
            unknown_today += int(bool(charged.get("tokens_unknown")))
    expected_budget = {
        "day": today,
        "runs_today": runs_today,
        "tokens_today": tokens_today,
        "unknown_token_runs": unknown_today,
    }
    changed = (
        state.get("settled_run_ids") != settled
        or state.get("budget") != expected_budget
    )
    state["settled_run_ids"] = settled
    state["budget"] = expected_budget
    return changed


def kill_switch_active(config: Mapping[str, Any]) -> tuple[bool, str | None]:
    env_name = str(config["contract"]["kill_switch"]["env"])
    env_value = os.environ.get(env_name, "").strip().lower()
    if env_value in TRUTHY:
        return True, f"environment:{env_name}"
    if Path(config["kill_file"]).exists():
        return True, f"file:{config['kill_file']}"
    return False, None


def open_breaker(state: dict[str, Any], reason: str) -> None:
    state["breaker"] = {
        "open": True,
        "reason": reason,
        "opened_at": utc_now(),
    }
    state["phase"] = "STOPPED"


def preflight_budget(state: Mapping[str, Any], config: Mapping[str, Any]) -> None:
    budget = require_object(state["budget"], "state.budget")
    if int(budget["runs_today"]) >= int(config["runs_per_day"]):
        raise AgentLoopError("Daily run budget is exhausted")
    token_limit = config["tokens_per_day"]
    if (
        config.get("token_policy", "hard") == "hard"
        and token_limit is not None
        and int(budget["tokens_today"]) >= int(token_limit)
    ):
        raise AgentLoopError("Daily token budget is exhausted")


def settle_budget(
    state: dict[str, Any],
    config: Mapping[str, Any],
    run_id: str,
    tokens: int | None,
) -> tuple[dict[str, Any], str | None]:
    if run_id in state["settled_run_ids"]:
        return (
            {
                "run_id_settled": False,
                "tokens_counted": 0,
                "tokens_unknown": tokens is None,
                "runs_today": int(state["budget"]["runs_today"]),
                "tokens_today": int(state["budget"]["tokens_today"]),
            },
            None,
        )
    state["settled_run_ids"].append(run_id)
    state["budget"]["runs_today"] = int(state["budget"]["runs_today"]) + 1
    breaker: str | None = None
    warnings: list[str] = []
    counted = 0
    unknown = tokens is None
    if unknown:
        state["budget"]["unknown_token_runs"] = (
            int(state["budget"]["unknown_token_runs"]) + 1
        )
        if config.get("token_policy", "hard") == "hard" and config["unknown_token_policy"] == "stop" and (
            config["tokens_per_run"] is not None or config["tokens_per_day"] is not None
        ):
            breaker = "TELEMETRY_UNKNOWN"
        elif config["tokens_per_run"] is not None or config["tokens_per_day"] is not None:
            warnings.append("token telemetry is unknown; numeric totals remain separate")
    else:
        assert tokens is not None
        if tokens < 0:
            raise AgentLoopError("Agent Fleet returned a negative token count")
        counted = tokens
        state["budget"]["tokens_today"] = int(state["budget"]["tokens_today"]) + tokens
        if config["tokens_per_run"] is not None and tokens > int(
            config["tokens_per_run"]
        ):
            message = (
                f"tokens_per_run budget exceeded: {tokens}>{config['tokens_per_run']}"
            )
            if config["token_policy"] == "hard":
                breaker = "BUDGET_EXCEEDED"
            else:
                warnings.append(message)
        if config["tokens_per_day"] is not None and int(
            state["budget"]["tokens_today"]
        ) > int(config["tokens_per_day"]):
            message = (
                "tokens_per_day budget exceeded: "
                f"{state['budget']['tokens_today']}>{config['tokens_per_day']}"
            )
            if config["token_policy"] == "hard":
                breaker = "BUDGET_EXCEEDED"
            else:
                warnings.append(message)
    if int(state["budget"]["runs_today"]) > int(config["runs_per_day"]):
        breaker = "BUDGET_EXCEEDED"
    return (
        {
            "run_id_settled": True,
            "tokens_counted": counted,
            "tokens_unknown": unknown,
            "runs_today": int(state["budget"]["runs_today"]),
            "tokens_today": int(state["budget"]["tokens_today"]),
            "warnings": warnings,
        },
        breaker,
    )


def _process_alive(pid: int, host: str) -> bool:
    if host.casefold() != socket.gethostname().casefold():
        return True
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
    try:
        import ctypes

        process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not process:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(
                process, ctypes.byref(exit_code)
            ):
                return True
            return exit_code.value == 259
        finally:
            ctypes.windll.kernel32.CloseHandle(process)
    except Exception:
        return True


class PathClaimsLease:
    """Process-shared path claim registry guarded by an OS-held lock."""

    def __init__(self, paths: LoopPaths, claims: Sequence[str]) -> None:
        self.paths = paths
        self.claims = list(claims)
        self.lease_id = uuid.uuid4().hex
        self.lease_path = paths.claims_root / f"{paths.root.name}.json"
        self.acquired = False

    def _read_existing(self) -> list[dict[str, Any]]:
        existing: list[dict[str, Any]] = []
        claims_root = security.resolve_under_root(
            self.paths.project,
            self.paths.claims_root,
        )
        if not self.paths.claims_root.is_dir():
            return existing
        for path in sorted(self.paths.claims_root.glob("*.json")):
            if path == self.lease_path:
                continue
            safe_path = security.resolve_under_root(self.paths.project, path)
            if not safe_path.is_relative_to(claims_root):
                raise AgentLoopError(f"Path-claim lease escapes registry: {path}")
            data = require_object(
                load_json(safe_path, "path-claim lease"),
                "path-claim lease",
            )
            require_exact_keys(
                data,
                label=f"path-claim lease {safe_path.name}",
                required={
                    "schema_version",
                    "lease_id",
                    "loop_id",
                    "pid",
                    "host",
                    "acquired_at",
                    "claims",
                },
            )
            if data["schema_version"] != SCHEMA_VERSION:
                raise AgentLoopError(f"Invalid path-claim lease: {safe_path}")
            pid = data.get("pid")
            host = data.get("host")
            lease_id = data.get("lease_id")
            other_loop_id = data.get("loop_id")
            claims = data.get("claims")
            if (
                isinstance(pid, bool)
                or not isinstance(pid, int)
                or pid <= 0
                or not isinstance(host, str)
                or not host
                or not isinstance(lease_id, str)
                or not lease_id
                or not isinstance(other_loop_id, str)
                or not LOOP_ID_RE.fullmatch(other_loop_id)
                or safe_path.stem != other_loop_id
                or not isinstance(claims, list)
                or not all(isinstance(item, str) for item in claims)
            ):
                raise AgentLoopError(f"Invalid path-claim lease: {safe_path}")
            ensure_timestamp(
                data["acquired_at"],
                f"path-claim lease {safe_path.name}.acquired_at",
            )
            normalized_claims = [
                security.normalize_claim(self.paths.project, item) for item in claims
            ]
            if normalized_claims != claims:
                raise AgentLoopError(f"Path-claim lease is not normalized: {safe_path}")
            if not _process_alive(pid, host):
                safe_path.unlink(missing_ok=True)
                continue
            existing.append({**data, "claims": normalized_claims})
        return existing

    def acquire(self) -> None:
        if self.acquired:
            raise AgentLoopError("Path claim lease is already acquired")
        security.resolve_under_root(self.paths.project, self.paths.claims_lock)
        safe_lease_path = security.resolve_under_root(
            self.paths.project,
            self.lease_path,
        )
        with security.LoopLock(self.paths.claims_lock):
            for existing in self._read_existing():
                existing_claims = existing.get("claims")
                if not isinstance(existing_claims, list):
                    raise AgentLoopError("Invalid claims list in active lease")
                for claim in self.claims:
                    for other in existing_claims:
                        if security.claims_overlap(claim, str(other)):
                            raise AgentLoopError(
                                f"Path claim collision: {claim!r} with "
                                f"{existing.get('loop_id')!r}:{other!r}"
                            )
            payload = {
                "schema_version": SCHEMA_VERSION,
                "lease_id": self.lease_id,
                "loop_id": self.paths.root.name,
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "acquired_at": utc_now(),
                "claims": self.claims,
            }
            security.atomic_write_json(safe_lease_path, payload)
        self.acquired = True

    def release(self) -> None:
        if not self.acquired:
            return
        safe_lease_path = security.resolve_under_root(
            self.paths.project,
            self.lease_path,
        )
        with security.LoopLock(self.paths.claims_lock):
            existing = load_json(
                safe_lease_path,
                "path-claim lease",
                required=False,
            )
            if isinstance(existing, dict) and existing.get("lease_id") == self.lease_id:
                safe_lease_path.unlink(missing_ok=True)
        self.acquired = False

    def __enter__(self) -> "PathClaimsLease":
        self.acquire()
        return self

    def __exit__(self, *args: object) -> None:
        self.release()


def _iter_watched_files(
    project: Path,
    patterns: Sequence[str],
) -> Iterable[Path]:
    operational = (project / ".agents" / "loops").resolve(strict=False)
    git_root = (project / ".git").resolve(strict=False)
    seen: set[Path] = set()
    for pattern in patterns:
        normalized = pattern
        candidates: Iterable[Path]
        if normalized == ".":
            candidates = [project]
        else:
            candidates = project.glob(normalized)
        for candidate in candidates:
            resolved = candidate.resolve(strict=False)
            if resolved.is_relative_to(operational) or resolved.is_relative_to(
                git_root
            ):
                continue
            security.assert_no_reparse_chain(project, resolved)
            nested: Iterable[Path]
            if resolved.is_dir():
                nested = resolved.rglob("*")
            else:
                nested = [resolved]
            for item in nested:
                resolved_item = item.resolve(strict=False)
                if (
                    resolved_item.is_relative_to(operational)
                    or resolved_item.is_relative_to(git_root)
                    or not resolved_item.is_file()
                ):
                    continue
                security.assert_no_reparse_chain(project, resolved_item)
                if resolved_item not in seen:
                    seen.add(resolved_item)
                    yield resolved_item


def snapshot_watched(
    project: Path,
    patterns: Sequence[str],
    *,
    max_files: int,
    max_bytes: int,
) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    total_bytes = 0
    for path in _iter_watched_files(project, patterns):
        if len(snapshot) >= max_files:
            raise AgentLoopError(f"Watched snapshot exceeds {max_files} files")
        size = path.stat().st_size
        total_bytes += size
        if total_bytes > max_bytes:
            raise AgentLoopError(f"Watched snapshot exceeds {max_bytes} bytes")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        relative = path.relative_to(project).as_posix()
        snapshot[relative] = digest.hexdigest()
    return snapshot


def snapshot_diff(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    changed = sorted(
        path for path in set(before) | set(after) if before.get(path) != after.get(path)
    )
    return changed


def snapshot_control_plane(
    paths: LoopPaths,
    config: Mapping[str, Any],
) -> dict[str, str | None]:
    files = {
        paths.contract,
        paths.gate,
        Path(config["task_file"]),
        Path(config["assessment_file"]),
        paths.state,
        paths.ledger,
    }
    snapshot: dict[str, str | None] = {}
    for path in sorted(files):
        safe_path = security.resolve_under_root(paths.project, path)
        relative = safe_path.relative_to(paths.project).as_posix()
        if not safe_path.exists():
            snapshot[relative] = None
            continue
        if not safe_path.is_file():
            raise AgentLoopError(f"Control-plane path is not a file: {safe_path}")
        digest = hashlib.sha256()
        with safe_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        snapshot[relative] = digest.hexdigest()
    return snapshot


def find_fleet_script(explicit: str | None = None) -> Path | None:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    home = Path.home()
    candidates.extend(
        [
            home / ".codex" / "skills" / "agent-fleet" / "scripts" / "fleet.py",
            home / ".agents" / "skills" / "agent-fleet" / "scripts" / "fleet.py",
            Path(__file__).resolve().parents[2]
            / "agent-fleet"
            / "scripts"
            / "fleet.py",
        ]
    )
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError:
            continue
        if resolved.is_file():
            return resolved
    return None


def build_fleet_command(
    paths: LoopPaths,
    config: Mapping[str, Any],
    fleet_script: Path,
) -> list[str]:
    delegation = config["contract"]["delegation"]
    command = [
        sys.executable,
        str(fleet_script),
        "run",
        "--task-file",
        str(config["task_file"]),
        "--assessment-file",
        str(config["assessment_file"]),
        "--agent",
        str(delegation["agent"]),
        "--mode",
        str(delegation["mode"]),
        "--kind",
        str(delegation["kind"]),
        "--complexity",
        str(delegation["complexity"]),
        "--workspace",
        str(paths.project),
        "--timeout",
        str(config["timeout"]),
        "--native-subagent-limit",
        "0",
    ]
    command.append("--enable-web" if delegation["enable_web"] else "--disable-web")
    return command


def _parse_process_json(stdout: str, stderr: str) -> dict[str, Any]:
    source = stdout.strip() or stderr.strip()
    if not source:
        raise AgentLoopError("Agent Fleet returned no JSON")
    if len(source.encode("utf-8")) > MAX_FLEET_RESULT_BYTES:
        raise AgentLoopError("Agent Fleet JSON exceeds the size limit")
    try:
        parsed = json.loads(source, object_pairs_hook=_no_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise AgentLoopError(f"Agent Fleet returned invalid JSON: {exc}") from exc
    return require_object(parsed, "Agent Fleet result")


def invoke_fleet(
    paths: LoopPaths,
    config: Mapping[str, Any],
    fleet_script: Path,
) -> dict[str, Any]:
    command = build_fleet_command(paths, config, fleet_script)
    environment = security.scrub_environment()
    completed = subprocess.run(
        command,
        cwd=str(paths.project),
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=int(config["timeout"]) + 30,
        check=False,
    )
    result = _parse_process_json(completed.stdout, completed.stderr)
    result["_controller_exit_code"] = completed.returncode
    return result


def safe_external_text(value: Any, *, maximum: int = 256) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = "".join(
        " " if character in "\r\n\t`" or ord(character) < 32 else character
        for character in value
    ).strip()
    if len(normalized) > maximum:
        normalized = normalized[: maximum - 3] + "..."
    try:
        security.assert_no_secrets_in_text(normalized)
    except security.SecurityError:
        return "[redacted]"
    return normalized or None


def safe_quota(value: Any) -> dict[str, Any] | str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return safe_external_text(value)
    if not isinstance(value, dict):
        return "unknown"
    allowed = {
        "status",
        "observed_at",
        "short_remaining_fraction",
        "weekly_remaining_fraction",
        "credits_remaining",
        "short_reset_at",
        "weekly_reset_at",
        "source",
        "confidence",
        "snapshot_id",
    }
    result: dict[str, Any] = {}
    for key in sorted(allowed):
        if key not in value:
            continue
        item = value[key]
        if isinstance(item, str):
            result[key] = safe_external_text(item)
        elif item is None or (
            not isinstance(item, bool) and isinstance(item, (int, float))
        ):
            result[key] = item
        else:
            result[key] = "unknown"
    return result


def extract_tokens(result: Mapping[str, Any]) -> int | None:
    token_usage = result.get("token_usage")
    if not isinstance(token_usage, dict):
        return None
    total = token_usage.get("total_tokens")
    if isinstance(total, bool) or not isinstance(total, int):
        return None
    return total


def safe_fleet_summary(result: Mapping[str, Any]) -> dict[str, Any]:
    duration = result.get("duration_seconds")
    safe_duration = (
        duration
        if not isinstance(duration, bool)
        and isinstance(duration, (int, float))
        and duration >= 0
        else None
    )
    return {
        "fleet_run_id": safe_external_text(result.get("run_id")),
        "agent": safe_external_text(result.get("agent")),
        "model": safe_external_text(result.get("model")),
        "tokens": extract_tokens(result)
        if extract_tokens(result) is not None
        else "unknown",
        "quota_before": safe_quota(result.get("quota_before")),
        "quota_after": safe_quota(result.get("quota_after")),
        "duration_seconds": safe_duration,
        "run_status": safe_external_text(result.get("status")),
    }


def progress_marker(summary: Mapping[str, Any], result: Mapping[str, Any]) -> str:
    handoff = result.get("handoff")
    safe_handoff = None
    if isinstance(handoff, dict):
        safe_handoff = {
            key: handoff.get(key)
            for key in ("status", "summary", "files_changed", "tests")
            if key in handoff
        }
    payload = {
        "fleet": {
            "agent": summary.get("agent"),
            "model": summary.get("model"),
            "run_status": summary.get("run_status"),
        },
        "handoff": safe_handoff,
    }
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def update_circuit_after_result(
    state: dict[str, Any],
    config: Mapping[str, Any],
    run_id: str,
    fleet_result: Mapping[str, Any],
    marker: str,
) -> str | None:
    status = str(fleet_result.get("status") or "FAILED").upper()
    if bool(fleet_result.get("timed_out")) or status == "TIMEOUT":
        state["failure"]["consecutive"] = int(state["failure"]["consecutive"]) + 1
        state["failure"]["last_error_kind"] = "timeout"
        return "TIMEOUT"
    if status not in {"COMPLETED", "PARTIAL"}:
        state["failure"]["consecutive"] = int(state["failure"]["consecutive"]) + 1
        state["failure"]["last_error_kind"] = status.lower()
        if int(state["failure"]["consecutive"]) >= int(config["repeated_failure_runs"]):
            return "REPEATED_FAILURE"
        return None
    state["failure"] = {"consecutive": 0, "last_error_kind": None}
    if state["progress"]["marker"] == marker:
        state["progress"]["unchanged_runs"] = (
            int(state["progress"]["unchanged_runs"]) + 1
        )
    else:
        state["progress"] = {
            "marker": marker,
            "last_change_run_id": run_id,
            "unchanged_runs": 0,
        }
    if int(state["progress"]["unchanged_runs"]) >= int(config["no_progress_runs"]):
        return "NO_PROGRESS"
    return None


def write_report(
    paths: LoopPaths,
    config: Mapping[str, Any],
    *,
    run_id: str,
    started_at: str,
    ended_at: str,
    status: str,
    fleet: Mapping[str, Any],
    changed: Sequence[str],
    breaker: str | None,
    acceptance_status: str = "pending",
) -> Path:
    report_path = paths.reports / f"{run_id}.md"
    security.assert_l1_write_allowed(
        paths.project,
        paths.root.name,
        report_path,
        watched_globs=config["watched_scope"],
    )
    lines = [
        f"# agent-loop report: {run_id}",
        "",
        f"- loop_id: `{paths.root.name}`",
        f"- started_at: `{started_at}`",
        f"- ended_at: `{ended_at}`",
        f"- status: `{status}`",
        f"- fleet_run_id: `{fleet.get('fleet_run_id') or 'unknown'}`",
        f"- agent: `{fleet.get('agent') or 'unknown'}`",
        f"- model: `{fleet.get('model') or 'unknown'}`",
        f"- tokens: `{fleet.get('tokens', 'unknown')}`",
        f"- parent_acceptance: `{acceptance_status}`",
        "- verifier: `REJECT`",
        f"- breaker: `{breaker or 'none'}`",
        f"- writes_outside_loop_state: `{len(changed)}`",
    ]
    if changed:
        lines.extend(["", "## Detected unauthorized changes"])
        lines.extend(f"- `{path}`" for path in changed[:50])
    payload = "\n".join(lines) + "\n"
    security.assert_no_secrets_in_text(payload)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = report_path.with_name(f".{report_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, report_path)
    finally:
        temporary.unlink(missing_ok=True)
    return report_path


def make_run_event(
    paths: LoopPaths,
    *,
    run_id: str,
    started_at: str,
    ended_at: str,
    status: str,
    fleet: Mapping[str, Any],
    budget: Mapping[str, Any],
    breaker: str | None,
    writes_performed: int,
    report_path: Path | None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": event_id("evt"),
        "event_type": "run",
        "run_id": run_id,
        "loop_id": paths.root.name,
        "started_at": started_at,
        "ended_at": ended_at,
        "status": status,
        "fleet": dict(fleet),
        "verification": {
            "verdict": "REJECT",
            "reasons": ["Parent acceptance has not been recorded."],
        },
        "budget_charged": dict(budget),
        "breaker_event": {"reason": breaker} if breaker else None,
        "writes_performed": writes_performed,
        "report_path": (
            report_path.relative_to(paths.project).as_posix() if report_path else None
        ),
    }


def command_doctor(args: argparse.Namespace) -> dict[str, Any]:
    if bool(args.project) != bool(args.loop):
        raise AgentLoopError("doctor requires both --project and --loop, or neither")
    fleet = find_fleet_script(args.fleet_script)
    result: dict[str, Any] = {
        "action": "doctor",
        "status": "READY" if fleet else "BLOCKED",
        "version": VERSION,
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "lock_backend": "msvcrt" if os.name == "nt" else "fcntl",
        "fleet_script": str(fleet) if fleet else None,
        "writes_performed": 0,
    }
    if args.project and args.loop:
        paths, config = load_validated(args.project, args.loop)
        state, state_exists = load_state(paths, config)
        result["loop"] = {
            "loop_id": paths.root.name,
            "contract_valid": True,
            "state_exists": state_exists,
            "phase": state["phase"],
        }
    return result


def command_validate(args: argparse.Namespace) -> dict[str, Any]:
    paths, config = load_validated(args.project, args.loop)
    return {
        "action": "validate",
        "status": "VALID",
        "loop_id": paths.root.name,
        "maturity": config["contract"]["maturity"],
        "writes_performed": 0,
    }


def command_status(args: argparse.Namespace) -> dict[str, Any]:
    paths, config = load_validated(args.project, args.loop)
    state, state_exists = load_state(paths, config)
    events = read_ledger(paths)
    reconciled = json.loads(json.dumps(state))
    reconciliation_required = reconcile_budget(reconciled, events, config["timezone"])
    interrupted = bool(
        state.get("current_run_id") and state.get("phase") not in {"IDLE", "STOPPED"}
    )
    return {
        "action": "status",
        "status": "RECOVERY_REQUIRED"
        if interrupted
        else ("STOPPED" if state["breaker"]["open"] else "READY"),
        "loop_id": paths.root.name,
        "state_exists": state_exists,
        "phase": state["phase"],
        "breaker": state["breaker"],
        "budget": reconciled["budget"],
        "event_count": len(events),
        "reconciliation_required": reconciliation_required,
        "writes_performed": 0,
    }


def command_plan(args: argparse.Namespace) -> dict[str, Any]:
    paths, config = load_validated(args.project, args.loop)
    fleet = find_fleet_script(args.fleet_script)
    if fleet is None:
        raise AgentLoopError("Agent Fleet script was not found")
    command = build_fleet_command(paths, config, fleet)
    active, source = kill_switch_active(config)
    return {
        "action": "plan",
        "status": "BLOCKED_KILL_SWITCH" if active else "PLANNED",
        "loop_id": paths.root.name,
        "maturity": "L1",
        "report_only": True,
        "kill_switch": source,
        "fleet_command": command,
        "watched_scope": config["watched_scope"],
        "path_claims": config["claims"],
        "writes_performed": 0,
    }


def _load_fake_result(paths: LoopPaths, value: str) -> dict[str, Any]:
    if os.environ.get("AGENT_LOOP_TEST_MODE") != "1":
        raise AgentLoopError("Fake Fleet results are allowed only in test mode")
    fake_path = resolve_loop_file(paths, value, "fake_fleet_result")
    return require_object(
        load_json(fake_path, "fake Fleet result"), "fake Fleet result"
    )


def command_run(args: argparse.Namespace) -> dict[str, Any]:
    if not args.report_only:
        raise AgentLoopError("V0.1 run requires --report-only")
    paths, config = load_validated(args.project, args.loop)
    fleet_script = find_fleet_script(args.fleet_script)
    if fleet_script is None and not args.fake_fleet_result:
        raise AgentLoopError("Agent Fleet script was not found")
    with security.LoopLock(paths.loop_lock):
        state, _ = load_state(paths, config)
        events = read_ledger(paths)
        if state.get("current_run_id") and state.get("phase") not in {
            "IDLE",
            "STOPPED",
        }:
            raise AgentLoopError(
                "Interrupted run detected; use recover with operator confirmation"
            )
        reconcile_budget(state, events, config["timezone"])
        if state["breaker"]["open"] or state["phase"] == "STOPPED":
            raise AgentLoopError(
                f"Circuit breaker is open: {state['breaker'].get('reason')}"
            )
        active, kill_source = kill_switch_active(config)
        if active:
            raise AgentLoopError(f"Kill switch is active: {kill_source}")
        preflight_budget(state, config)
        with PathClaimsLease(paths, config["claims"]):
            before = snapshot_watched(
                paths.project,
                config["watched_scope"],
                max_files=config["snapshot_max_files"],
                max_bytes=config["snapshot_max_bytes"],
            )
            run_id = security.new_run_id()
            started_at = utc_now()
            state["phase"] = "DELEGATING"
            state["current_run_id"] = run_id
            state["current_started_at"] = started_at
            state["attempt"] = 1
            state["lock"] = {
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "acquired_at": started_at,
                "lock_file": str(paths.loop_lock),
            }
            save_state(paths, state, config)
            control_before = snapshot_control_plane(paths, config)
            try:
                if args.fake_fleet_result:
                    fleet_result = _load_fake_result(paths, args.fake_fleet_result)
                else:
                    assert fleet_script is not None
                    fleet_result = invoke_fleet(paths, config, fleet_script)
            except subprocess.TimeoutExpired:
                fleet_result = {
                    "status": "TIMEOUT",
                    "timed_out": True,
                    "run_id": None,
                    "token_usage": {"status": "unknown", "total_tokens": None},
                }
            ended_at = utc_now()
            control_after = snapshot_control_plane(paths, config)
            control_changed = snapshot_diff(control_before, control_after)
            after = snapshot_watched(
                paths.project,
                config["watched_scope"],
                max_files=config["snapshot_max_files"],
                max_bytes=config["snapshot_max_bytes"],
            )
            changed = snapshot_diff(before, after)
            fleet = safe_fleet_summary(fleet_result)
            if control_changed:
                unauthorized = sorted(set(changed) | set(control_changed))
                breaker = "CONTROL_PLANE_WRITE_DETECTED"
                open_breaker(state, breaker)
                report = write_report(
                    paths,
                    config,
                    run_id=run_id,
                    started_at=started_at,
                    ended_at=ended_at,
                    status="rejected-control-plane",
                    fleet=fleet,
                    changed=unauthorized,
                    breaker=breaker,
                    acceptance_status="unavailable",
                )
                state["current_run_id"] = None
                state["current_started_at"] = None
                state["attempt"] = 0
                state["lock"] = {
                    "pid": None,
                    "host": None,
                    "acquired_at": None,
                    "lock_file": str(paths.loop_lock),
                }
                state["last_run_id"] = run_id
                state["last_fleet_run_id"] = fleet["fleet_run_id"]
                state["last_status"] = "rejected-control-plane"
                save_state(paths, state, config)
                return {
                    "action": "run",
                    "status": "REJECTED_CONTROL_PLANE",
                    "loop_id": paths.root.name,
                    "run_id": run_id,
                    "fleet": fleet,
                    "verifier": "REJECT",
                    "parent_acceptance_required": False,
                    "breaker": breaker,
                    "writes_performed": len(unauthorized),
                    "report_path": str(report),
                    "ledger_event_recorded": False,
                }
            tokens = extract_tokens(fleet_result)
            budget, budget_breaker = settle_budget(state, config, run_id, tokens)
            marker = progress_marker(fleet, fleet_result)
            result_breaker = update_circuit_after_result(
                state, config, run_id, fleet_result, marker
            )
            breaker = None
            if changed:
                breaker = "L1_WRITE_DETECTED"
            elif budget_breaker and config["budget_stop"]:
                breaker = budget_breaker
            elif result_breaker:
                breaker = result_breaker
            if breaker:
                open_breaker(state, breaker)
            else:
                state["phase"] = "IDLE"
            fleet_status = str(fleet_result.get("status") or "FAILED").upper()
            if changed:
                status = "rejected-write"
            elif fleet_status in {"COMPLETED", "PARTIAL"}:
                status = "completed"
            elif fleet_status == "TIMEOUT":
                status = "timeout"
            else:
                status = "failed"
            report = write_report(
                paths,
                config,
                run_id=run_id,
                started_at=started_at,
                ended_at=ended_at,
                status=status,
                fleet=fleet,
                changed=changed,
                breaker=breaker,
            )
            run_event = make_run_event(
                paths,
                run_id=run_id,
                started_at=started_at,
                ended_at=ended_at,
                status=status,
                fleet=fleet,
                budget=budget,
                breaker=breaker,
                writes_performed=len(changed),
                report_path=report,
            )
            append_event(paths, config, run_event)
            state["current_run_id"] = None
            state["current_started_at"] = None
            state["attempt"] = 0
            state["lock"] = {
                "pid": None,
                "host": None,
                "acquired_at": None,
                "lock_file": str(paths.loop_lock),
            }
            state["last_run_id"] = run_id
            state["last_fleet_run_id"] = fleet["fleet_run_id"]
            state["last_status"] = status
            save_state(paths, state, config)
            return {
                "action": "run",
                "status": status.upper().replace("-", "_"),
                "loop_id": paths.root.name,
                "run_id": run_id,
                "fleet": fleet,
                "verifier": "REJECT",
                "parent_acceptance_required": True,
                "breaker": breaker,
                "writes_performed": len(changed),
                "report_path": str(report),
            }


def command_stop(args: argparse.Namespace) -> dict[str, Any]:
    paths, config = load_validated(args.project, args.loop)
    with security.LoopLock(paths.loop_lock):
        state, _ = load_state(paths, config)
        kill_file = Path(config["kill_file"])
        security.assert_l1_write_allowed(
            paths.project,
            paths.root.name,
            kill_file,
            watched_globs=config["watched_scope"],
        )
        security.atomic_write_json(
            kill_file,
            {
                "stopped_at": utc_now(),
                "reason": args.reason,
                "operator": os.environ.get("USERNAME")
                or os.environ.get("USER")
                or "unknown",
            },
        )
        open_breaker(state, "KILL_SWITCH")
        state["last_status"] = "stopped"
        save_state(paths, state, config)
    return {
        "action": "stop",
        "status": "STOPPED",
        "loop_id": paths.root.name,
        "reason": args.reason,
        "writes_performed": 2,
    }


def command_recover(args: argparse.Namespace) -> dict[str, Any]:
    if not args.operator_confirmed:
        raise AgentLoopError("recover requires --operator-confirmed")
    paths, config = load_validated(args.project, args.loop)
    with security.LoopLock(paths.loop_lock):
        state, _ = load_state(paths, config)
        run_id = state.get("current_run_id")
        if not isinstance(run_id, str):
            return {
                "action": "recover",
                "status": "NOT_REQUIRED",
                "loop_id": paths.root.name,
                "writes_performed": 0,
            }
        events = read_ledger(paths)
        if any(
            event.get("event_type") == "run" and event.get("run_id") == run_id
            for event in events
        ):
            raise AgentLoopError("Interrupted run already exists in the ledger")
        started_at = (
            state.get("current_started_at")
            if isinstance(state.get("current_started_at"), str)
            else utc_now()
        )
        ended_at = utc_now()
        budget, _ = settle_budget(state, config, run_id, None)
        breaker = "INTERRUPTED"
        report = write_report(
            paths,
            config,
            run_id=run_id,
            started_at=started_at,
            ended_at=ended_at,
            status="stopped",
            fleet={
                "fleet_run_id": None,
                "agent": None,
                "model": None,
                "tokens": "unknown",
                "quota_before": None,
                "quota_after": None,
                "duration_seconds": None,
                "run_status": "INTERRUPTED",
            },
            changed=[],
            breaker=breaker,
        )
        run_event = make_run_event(
            paths,
            run_id=run_id,
            started_at=started_at,
            ended_at=ended_at,
            status="stopped",
            fleet={
                "fleet_run_id": None,
                "agent": None,
                "model": None,
                "tokens": "unknown",
                "quota_before": None,
                "quota_after": None,
                "duration_seconds": None,
                "run_status": "INTERRUPTED",
            },
            budget=budget,
            breaker=breaker,
            writes_performed=0,
            report_path=report,
        )
        append_event(paths, config, run_event)
        open_breaker(state, breaker)
        state["current_run_id"] = None
        state["current_started_at"] = None
        state["attempt"] = 0
        state["last_run_id"] = run_id
        state["last_fleet_run_id"] = None
        state["last_status"] = "stopped"
        save_state(paths, state, config)
    return {
        "action": "recover",
        "status": "STOPPED",
        "loop_id": paths.root.name,
        "run_id": run_id,
        "reason": args.reason,
        "writes_performed": 3,
    }


def command_record_result(args: argparse.Namespace) -> dict[str, Any]:
    paths, config = load_validated(args.project, args.loop)
    result_path = resolve_loop_file(paths, args.result, "result")
    result = require_object(load_json(result_path, "parent result"), "parent result")
    require_exact_keys(
        result,
        label="parent result",
        required={
            "run_id",
            "fleet_run_id",
            "verified_by",
            "quality",
            "criteria_pass",
            "note",
        },
    )
    if not isinstance(result["run_id"], str) or not isinstance(
        result["fleet_run_id"], str
    ):
        raise AgentLoopError("parent result run identifiers must be strings")
    if not isinstance(result["verified_by"], str) or not result["verified_by"]:
        raise AgentLoopError("parent result verified_by is required")
    quality = ensure_positive_int(
        result["quality"], "parent result quality", minimum=0, maximum=100
    )
    if not isinstance(result["criteria_pass"], bool):
        raise AgentLoopError("parent result criteria_pass must be boolean")
    if not isinstance(result["note"], str):
        raise AgentLoopError("parent result note must be a string")
    with security.LoopLock(paths.loop_lock):
        events = read_ledger(paths)
        run_event = next(
            (
                event
                for event in events
                if event.get("event_type") == "run"
                and event.get("run_id") == result["run_id"]
            ),
            None,
        )
        if run_event is None:
            raise AgentLoopError("Parent result references an unknown loop run")
        fleet = run_event.get("fleet")
        if (
            not isinstance(fleet, dict)
            or fleet.get("fleet_run_id") != result["fleet_run_id"]
        ):
            raise AgentLoopError("Parent result Fleet run ID does not match the ledger")
        verdict = "ACCEPT" if result["criteria_pass"] else "REJECT"
        acceptance = {
            "schema_version": SCHEMA_VERSION,
            "event_id": event_id("evt"),
            "event_type": "acceptance",
            "run_id": result["run_id"],
            "fleet_run_id": result["fleet_run_id"],
            "recorded_at": utc_now(),
            "verified_by": result["verified_by"],
            "quality": quality,
            "criteria_pass": result["criteria_pass"],
            "verdict": verdict,
            "note": result["note"],
        }
        existing = [
            event
            for event in events
            if event.get("event_type") == "acceptance"
            and event.get("run_id") == result["run_id"]
        ]
        if existing:
            comparable = dict(acceptance)
            comparable.pop("event_id")
            comparable.pop("recorded_at")
            prior = dict(existing[0])
            prior.pop("event_id", None)
            prior.pop("recorded_at", None)
            if comparable != prior:
                raise AgentLoopError("A conflicting parent acceptance already exists")
            return {
                "action": "record-result",
                "status": "ALREADY_RECORDED",
                "run_id": result["run_id"],
                "verdict": verdict,
                "writes_performed": 0,
            }
        append_event(paths, config, acceptance)
    return {
        "action": "record-result",
        "status": "RECORDED",
        "run_id": result["run_id"],
        "verdict": verdict,
        "writes_performed": 1,
    }


def add_loop_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", required=True)
    parser.add_argument("--loop", required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run governed report-only control loops through Agent Fleet."
    )
    parser.add_argument("--version", action="version", version=f"agent-loop {VERSION}")
    parser.add_argument("--fleet-script")
    subparsers = parser.add_subparsers(dest="action", required=True)

    doctor = subparsers.add_parser("doctor")
    doctor.add_argument("--project")
    doctor.add_argument("--loop")

    validate = subparsers.add_parser("validate")
    add_loop_args(validate)

    status = subparsers.add_parser("status")
    add_loop_args(status)

    plan = subparsers.add_parser("plan")
    add_loop_args(plan)

    run = subparsers.add_parser("run")
    add_loop_args(run)
    run.add_argument("--report-only", action="store_true")
    run.add_argument("--fake-fleet-result")

    stop = subparsers.add_parser("stop")
    add_loop_args(stop)
    stop.add_argument("--reason", required=True)

    recover = subparsers.add_parser("recover")
    add_loop_args(recover)
    recover.add_argument("--operator-confirmed", action="store_true")
    recover.add_argument("--reason", required=True)

    record = subparsers.add_parser("record-result")
    add_loop_args(record)
    record.add_argument("--result", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.action == "doctor":
            result = command_doctor(args)
        elif args.action == "validate":
            result = command_validate(args)
        elif args.action == "status":
            result = command_status(args)
        elif args.action == "plan":
            result = command_plan(args)
        elif args.action == "run":
            result = command_run(args)
        elif args.action == "stop":
            result = command_stop(args)
        elif args.action == "recover":
            result = command_recover(args)
        elif args.action == "record-result":
            result = command_record_result(args)
        else:
            raise AgentLoopError(f"Unsupported action: {args.action}")
        sys.stdout.buffer.write(canonical_bytes(result))
        return 0
    except (
        AgentLoopError,
        security.SecurityError,
        OSError,
        ValueError,
        ZoneInfoNotFoundError,
    ) as exc:
        error = {
            "action": getattr(args, "action", None),
            "status": "REFUSED",
            "error": f"{type(exc).__name__}: {exc}",
            "writes_performed": 0,
        }
        sys.stderr.buffer.write(canonical_bytes(error))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
