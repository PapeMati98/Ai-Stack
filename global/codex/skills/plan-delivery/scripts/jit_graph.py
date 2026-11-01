#!/usr/bin/env python3
"""Validate declarative JIT graph candidates and emit shadow-only receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any


VERSION = "0.1.0"
SCHEMA_VERSION = 1
SHA_RE = re.compile(r"^[a-f0-9]{64}$")
ID_RE = re.compile(r"^[a-z][a-z0-9-]{2,100}$")
REQ_RE = re.compile(r"^REQ-[0-9]{3,}$")
CRIT_RE = re.compile(r"^crit-[a-z0-9][a-z0-9-]{2,100}$")
SKILL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")

EXECUTABLE_PATTERNS = (
    re.compile(r"```"),
    re.compile(r"(?i)\b(?:powershell|pwsh|cmd|bash|sh|python|node)\s+(?:-[ce]|/c)\b"),
    re.compile(r"(?i)\b(?:invoke-expression|start-process|subprocess\.|os\.system)\b"),
    re.compile(r"(?:&&|\|\||;\s*(?:rm|del|remove-item|curl|wget)\b)"),
)
WORKER_SELECTION_RE = re.compile(
    r"(?i)\b(?:use|route\s+to|assign\s+to|worker|maker)\s+"
    r"(?:gpt|claude|grok|gemini|qwen|kimi|glm|deepseek|minimax|codex|antigravity)\b"
)
SENSITIVE_PATH_RE = re.compile(
    r"(?i)(?:^|/)(?:\.env(?:\..*)?|credentials?(?:\..*)?|secrets?(?:\..*)?|"
    r"cookies?(?:\..*)?|tokens?(?:\..*)?|id_rsa|[^/]+\.(?:pem|pfx|p12|key))$"
)
REPAIR_STRICTNESS = {"block": 0, "bounded-repair": 1}


class JitError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def canonical_bytes(value: Any) -> bytes:
    text = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return unicodedata.normalize("NFC", text).encode("utf-8")


def sha256_value(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise JitError("invalid-json", f"Cannot read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise JitError("invalid-shape", f"{label} must be an object")
    return value


def write_new_json(path: Path, value: dict[str, Any]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise JitError("output-exists", f"Refusing to overwrite output: {path}") from exc
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def exact_keys(value: Any, required: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise JitError("invalid-shape", f"{label} must be an object")
    missing = sorted(required - set(value))
    extra = sorted(set(value) - required)
    if missing or extra:
        raise JitError("schema-keys", f"{label} keys mismatch: missing={missing}; extra={extra}")
    return value


def text_value(value: Any, label: str, minimum: int = 1, maximum: int = 2000) -> str:
    if not isinstance(value, str) or not (minimum <= len(value.strip()) <= maximum):
        raise JitError("schema-text", f"{label} must be text with length {minimum}..{maximum}")
    return value


def bool_value(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise JitError("schema-boolean", f"{label} must be boolean")
    return value


def int_value(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise JitError("schema-integer", f"{label} must be an integer in {minimum}..{maximum}")
    return value


def sha_value(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA_RE.fullmatch(value):
        raise JitError("schema-sha256", f"{label} must be a lowercase SHA-256")
    return value


def string_list(
    value: Any,
    label: str,
    *,
    minimum: int = 0,
    maximum: int = 128,
    pattern: re.Pattern[str] | None = None,
) -> list[str]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise JitError("schema-array", f"{label} must contain {minimum}..{maximum} items")
    result: list[str] = []
    for index, item in enumerate(value):
        text = text_value(item, f"{label}[{index}]", 1, 500)
        if pattern and not pattern.fullmatch(text):
            raise JitError("schema-identifier", f"Invalid identifier in {label}")
        result.append(text)
    if len(result) != len(set(result)):
        raise JitError("schema-duplicate", f"{label} must contain unique values")
    return result


def relative_path(value: Any, label: str, *, allow_glob: bool = False) -> str:
    path = text_value(value, label, 1, 500)
    normalized = path.replace("\\", "/")
    if path != normalized or normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise JitError("path-scope", f"{label} must be a normalized relative path")
    plain = re.sub(r"[?*\[\]]", "x", normalized) if allow_glob else normalized
    if any(part in {"", ".", ".."} for part in plain.rstrip("/").split("/")):
        raise JitError("path-scope", f"{label} contains an invalid path segment")
    if not allow_glob and any(marker in normalized for marker in ("*", "?", "[", "]")):
        raise JitError("path-scope", f"{label} cannot contain a glob")
    return normalized


def scope_list(value: Any, label: str, maximum: int) -> list[str]:
    scopes = string_list(value, label, maximum=maximum)
    return [relative_path(scope, f"{label}[{index}]", allow_glob=True) for index, scope in enumerate(scopes)]


def validate_generator(value: Any, label: str) -> dict[str, str]:
    generator = exact_keys(
        value,
        {"fleet_run_id", "provider_lineage", "model_identity"},
        label,
    )
    return {
        "fleet_run_id": text_value(generator["fleet_run_id"], f"{label}.fleet_run_id", 8, 200),
        "provider_lineage": text_value(generator["provider_lineage"], f"{label}.provider_lineage", 2, 100),
        "model_identity": text_value(generator["model_identity"], f"{label}.model_identity", 2, 200),
    }


def validate_context_refs(value: Any, label: str) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > 64:
        raise JitError("schema-array", f"{label} must contain at most 64 entries")
    result: list[dict[str, str]] = []
    identities: set[tuple[str, str]] = set()
    paths: set[str] = set()
    for index, item in enumerate(value):
        entry = exact_keys(item, {"path", "sha256", "purpose"}, f"{label}[{index}]")
        path = relative_path(entry["path"], f"{label}[{index}].path")
        if SENSITIVE_PATH_RE.search(path):
            raise JitError("sensitive-context", f"Sensitive context path is forbidden in {label}")
        normalized = {
            "path": path,
            "sha256": sha_value(entry["sha256"], f"{label}[{index}].sha256"),
            "purpose": text_value(entry["purpose"], f"{label}[{index}].purpose", 3, 300),
        }
        identity = (normalized["path"], normalized["sha256"])
        if identity in identities:
            raise JitError("schema-duplicate", f"Duplicate context reference in {label}")
        if path in paths:
            raise JitError("context-hash-conflict", f"One context path has multiple hashes in {label}")
        identities.add(identity)
        paths.add(path)
        result.append(normalized)
    return result


def validate_budget(value: Any, label: str, *, include_repairs: bool) -> dict[str, int]:
    keys = {"max_wall_seconds", "max_tool_calls", "max_tokens", "max_attempts"}
    if include_repairs:
        keys.add("max_repairs")
    budget = exact_keys(value, keys, label)
    result = {
        "max_wall_seconds": int_value(budget["max_wall_seconds"], f"{label}.max_wall_seconds", 1, 604800),
        "max_tool_calls": int_value(budget["max_tool_calls"], f"{label}.max_tool_calls", 1, 100000),
        "max_tokens": int_value(budget["max_tokens"], f"{label}.max_tokens", 1, 100000000),
        "max_attempts": int_value(budget["max_attempts"], f"{label}.max_attempts", 1, 3),
    }
    if include_repairs:
        result["max_repairs"] = int_value(budget["max_repairs"], f"{label}.max_repairs", 0, 2)
    return result


def validate_reviewer(value: Any, label: str) -> dict[str, Any]:
    reviewer = exact_keys(value, {"different_provider_lineage", "required_capabilities"}, label)
    if reviewer["different_provider_lineage"] is not True:
        raise JitError("reviewer-lineage", "Reviewer must use a different provider lineage")
    capabilities = string_list(
        reviewer["required_capabilities"],
        f"{label}.required_capabilities",
        minimum=1,
        maximum=16,
    )
    return {"different_provider_lineage": True, "required_capabilities": capabilities}


def validate_authority(value: Any, label: str) -> dict[str, Any]:
    authority = exact_keys(
        value,
        {
            "authority_ceiling_ref",
            "requirement_refs",
            "capability",
            "required_context_refs",
            "allowed_context_refs",
            "skill_allowlist",
            "tool_allowlist",
            "read_scope",
            "write_scope",
            "budget",
            "required_stop_conditions",
            "required_evidence_refs",
            "required_criterion_refs",
            "required_reviewer_capabilities",
            "repair_ceiling",
        },
        label,
    )
    ceiling_ref = text_value(authority["authority_ceiling_ref"], f"{label}.authority_ceiling_ref", 3, 160)
    required_context = validate_context_refs(authority["required_context_refs"], f"{label}.required_context_refs")
    allowed_context = validate_context_refs(authority["allowed_context_refs"], f"{label}.allowed_context_refs")
    required_ids = {(item["path"], item["sha256"]) for item in required_context}
    allowed_ids = {(item["path"], item["sha256"]) for item in allowed_context}
    if not required_ids <= allowed_ids:
        raise JitError("manifest-context", f"{label} required context exceeds allowed context")
    repair = exact_keys(authority["repair_ceiling"], {"on_reject", "max_repairs"}, f"{label}.repair_ceiling")
    if repair["on_reject"] not in REPAIR_STRICTNESS:
        raise JitError("manifest-repair", f"Unsupported {label}.repair_ceiling.on_reject")
    return {
        "authority_ceiling_ref": ceiling_ref,
        "requirement_refs": string_list(authority["requirement_refs"], f"{label}.requirement_refs", minimum=1, maximum=64, pattern=REQ_RE),
        "capability": text_value(authority["capability"], f"{label}.capability", 3, 100),
        "required_context_refs": required_context,
        "allowed_context_refs": allowed_context,
        "skill_allowlist": string_list(authority["skill_allowlist"], f"{label}.skill_allowlist", maximum=32, pattern=SKILL_RE),
        "tool_allowlist": string_list(authority["tool_allowlist"], f"{label}.tool_allowlist", maximum=64),
        "read_scope": scope_list(authority["read_scope"], f"{label}.read_scope", 128),
        "write_scope": scope_list(authority["write_scope"], f"{label}.write_scope", 64),
        "budget": validate_budget(authority["budget"], f"{label}.budget", include_repairs=True),
        "required_stop_conditions": string_list(authority["required_stop_conditions"], f"{label}.required_stop_conditions", minimum=1, maximum=16),
        "required_evidence_refs": string_list(authority["required_evidence_refs"], f"{label}.required_evidence_refs", minimum=1, maximum=64),
        "required_criterion_refs": string_list(authority["required_criterion_refs"], f"{label}.required_criterion_refs", minimum=1, maximum=64, pattern=CRIT_RE),
        "required_reviewer_capabilities": string_list(authority["required_reviewer_capabilities"], f"{label}.required_reviewer_capabilities", minimum=1, maximum=16),
        "repair_ceiling": {
            "on_reject": repair["on_reject"],
            "max_repairs": int_value(repair["max_repairs"], f"{label}.repair_ceiling.max_repairs", 0, 2),
        },
    }


def validate_manifest(value: Any) -> dict[str, Any]:
    manifest = exact_keys(
        value,
        {
            "schema_version",
            "manifest_id",
            "planning_contract_sha256",
            "static_topology_id",
            "pilot_mode",
            "control_veto",
            "signals",
            "requirements",
            "authority_ceilings",
            "global_budget",
        },
        "manifest",
    )
    if manifest["schema_version"] != SCHEMA_VERSION:
        raise JitError("schema-version", "Unsupported manifest schema_version")
    manifest_id = text_value(manifest["manifest_id"], "manifest.manifest_id", 3, 101)
    if not ID_RE.fullmatch(manifest_id):
        raise JitError("schema-identifier", "Invalid manifest_id")
    if manifest["pilot_mode"] != "shadow-only":
        raise JitError("active-mode-forbidden", "Only shadow-only pilot mode is supported")
    signals = exact_keys(
        manifest["signals"],
        {
            "high_complexity",
            "bounded_ambiguity",
            "multi_system_context",
            "parallel_streams",
            "multiple_gates",
            "repair_history",
        },
        "manifest.signals",
    )
    normalized_signals = {key: bool_value(value, f"manifest.signals.{key}") for key, value in signals.items()}
    requirements = string_list(manifest["requirements"], "manifest.requirements", minimum=1, maximum=256, pattern=REQ_RE)
    ceilings_value = manifest["authority_ceilings"]
    if not isinstance(ceilings_value, list) or not 1 <= len(ceilings_value) <= 128:
        raise JitError("schema-array", "manifest.authority_ceilings must contain 1..128 entries")
    ceilings: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(ceilings_value):
        ceiling = validate_authority(item, f"manifest.authority_ceilings[{index}]")
        ref = ceiling["authority_ceiling_ref"]
        if ref in ceilings:
            raise JitError("schema-duplicate", f"Duplicate authority ceiling: {ref}")
        if not set(ceiling["requirement_refs"]) <= set(requirements):
            raise JitError("manifest-requirements", f"Authority {ref} references unknown requirements")
        ceilings[ref] = ceiling
    global_budget = exact_keys(
        manifest["global_budget"],
        {"max_tasks", "max_parallel_streams", "max_review_gates", "max_coordination_steps"},
        "manifest.global_budget",
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "manifest_id": manifest_id,
        "planning_contract_sha256": sha_value(manifest["planning_contract_sha256"], "manifest.planning_contract_sha256"),
        "static_topology_id": text_value(manifest["static_topology_id"], "manifest.static_topology_id", 3, 120),
        "pilot_mode": "shadow-only",
        "control_veto": bool_value(manifest["control_veto"], "manifest.control_veto"),
        "signals": normalized_signals,
        "requirements": requirements,
        "authority_ceilings": ceilings,
        "global_budget": {
            "max_tasks": int_value(global_budget["max_tasks"], "manifest.global_budget.max_tasks", 1, 128),
            "max_parallel_streams": int_value(global_budget["max_parallel_streams"], "manifest.global_budget.max_parallel_streams", 1, 32),
            "max_review_gates": int_value(global_budget["max_review_gates"], "manifest.global_budget.max_review_gates", 1, 128),
            "max_coordination_steps": int_value(global_budget["max_coordination_steps"], "manifest.global_budget.max_coordination_steps", 1, 512),
        },
    }


def validate_task(value: Any, label: str) -> dict[str, Any]:
    task = exact_keys(
        value,
        {
            "task_id",
            "objective",
            "requirement_refs",
            "authority_ceiling_ref",
            "static_task_contract_sha256",
            "capability",
            "context_refs",
            "skill_bindings",
            "tool_allowlist",
            "read_scope",
            "write_scope",
            "budget",
            "stop_conditions",
            "evidence_refs",
            "criterion_refs",
            "reviewer_constraint",
            "repair_route",
        },
        label,
    )
    task_id = text_value(task["task_id"], f"{label}.task_id", 4, 105)
    if not re.fullmatch(r"task-[a-z0-9][a-z0-9-]{2,100}", task_id):
        raise JitError("schema-identifier", f"Invalid {label}.task_id")
    repair = exact_keys(task["repair_route"], {"on_reject", "on_exhaustion"}, f"{label}.repair_route")
    if repair["on_reject"] not in REPAIR_STRICTNESS or repair["on_exhaustion"] not in {"static-fallback", "block"}:
        raise JitError("schema-repair", f"Unsupported {label}.repair_route")
    return {
        "task_id": task_id,
        "objective": text_value(task["objective"], f"{label}.objective", 8, 2000),
        "requirement_refs": string_list(task["requirement_refs"], f"{label}.requirement_refs", minimum=1, maximum=64, pattern=REQ_RE),
        "authority_ceiling_ref": text_value(task["authority_ceiling_ref"], f"{label}.authority_ceiling_ref", 3, 160),
        "static_task_contract_sha256": sha_value(task["static_task_contract_sha256"], f"{label}.static_task_contract_sha256"),
        "capability": text_value(task["capability"], f"{label}.capability", 3, 100),
        "context_refs": validate_context_refs(task["context_refs"], f"{label}.context_refs"),
        "skill_bindings": string_list(task["skill_bindings"], f"{label}.skill_bindings", maximum=8, pattern=SKILL_RE),
        "tool_allowlist": string_list(task["tool_allowlist"], f"{label}.tool_allowlist", maximum=32),
        "read_scope": scope_list(task["read_scope"], f"{label}.read_scope", 64),
        "write_scope": scope_list(task["write_scope"], f"{label}.write_scope", 32),
        "budget": validate_budget(task["budget"], f"{label}.budget", include_repairs=True),
        "stop_conditions": string_list(task["stop_conditions"], f"{label}.stop_conditions", minimum=1, maximum=16),
        "evidence_refs": string_list(task["evidence_refs"], f"{label}.evidence_refs", minimum=1, maximum=64),
        "criterion_refs": string_list(task["criterion_refs"], f"{label}.criterion_refs", minimum=1, maximum=64, pattern=CRIT_RE),
        "reviewer_constraint": validate_reviewer(task["reviewer_constraint"], f"{label}.reviewer_constraint"),
        "repair_route": {"on_reject": repair["on_reject"], "on_exhaustion": repair["on_exhaustion"]},
    }


def validate_candidate(value: Any) -> dict[str, Any]:
    candidate = exact_keys(
        value,
        {
            "schema_version",
            "candidate_id",
            "synthesis_input_sha256",
            "planning_contract_sha256",
            "static_topology_id",
            "generator",
            "requirements_covered",
            "tasks",
            "dependencies",
            "coordination_estimate",
            "fallback",
        },
        "candidate",
    )
    if candidate["schema_version"] != SCHEMA_VERSION:
        raise JitError("schema-version", "Unsupported candidate schema_version")
    candidate_id = text_value(candidate["candidate_id"], "candidate.candidate_id", 11, 90)
    if not re.fullmatch(r"jit-graph-[a-z0-9][a-z0-9-]{7,80}", candidate_id):
        raise JitError("schema-identifier", "Invalid candidate_id")
    tasks_value = candidate["tasks"]
    if not isinstance(tasks_value, list) or not 1 <= len(tasks_value) <= 128:
        raise JitError("schema-array", "candidate.tasks must contain 1..128 entries")
    tasks: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(tasks_value):
        task = validate_task(item, f"candidate.tasks[{index}]")
        if task["task_id"] in tasks:
            raise JitError("schema-duplicate", f"Duplicate task ID: {task['task_id']}")
        tasks[task["task_id"]] = task
    dependencies_value = candidate["dependencies"]
    if not isinstance(dependencies_value, list) or len(dependencies_value) > 512:
        raise JitError("schema-array", "candidate.dependencies must contain at most 512 entries")
    dependencies: list[dict[str, str]] = []
    seen_edges: set[tuple[str, str, str]] = set()
    for index, item in enumerate(dependencies_value):
        edge = exact_keys(item, {"from", "to", "kind"}, f"candidate.dependencies[{index}]")
        source = text_value(edge["from"], f"candidate.dependencies[{index}].from", 4, 105)
        target = text_value(edge["to"], f"candidate.dependencies[{index}].to", 4, 105)
        if source not in tasks or target not in tasks or source == target:
            raise JitError("topology-edge", "Dependency endpoint is missing or self-referential")
        if edge["kind"] not in {"requires", "evidence-gate", "review-gate"}:
            raise JitError("topology-edge", "Unsupported dependency kind")
        identity = (source, target, edge["kind"])
        if identity in seen_edges:
            raise JitError("schema-duplicate", "Duplicate dependency")
        seen_edges.add(identity)
        dependencies.append({"from": source, "to": target, "kind": edge["kind"]})
    coordination = exact_keys(
        candidate["coordination_estimate"],
        {"parallel_streams", "review_gates", "estimated_coordination_steps"},
        "candidate.coordination_estimate",
    )
    fallback = exact_keys(candidate["fallback"], {"static_topology_id", "trigger"}, "candidate.fallback")
    return {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "synthesis_input_sha256": sha_value(candidate["synthesis_input_sha256"], "candidate.synthesis_input_sha256"),
        "planning_contract_sha256": sha_value(candidate["planning_contract_sha256"], "candidate.planning_contract_sha256"),
        "static_topology_id": text_value(candidate["static_topology_id"], "candidate.static_topology_id", 3, 120),
        "generator": validate_generator(candidate["generator"], "candidate.generator"),
        "requirements_covered": string_list(candidate["requirements_covered"], "candidate.requirements_covered", minimum=1, maximum=256, pattern=REQ_RE),
        "tasks": tasks,
        "dependencies": dependencies,
        "coordination_estimate": {
            "parallel_streams": int_value(coordination["parallel_streams"], "candidate.coordination_estimate.parallel_streams", 1, 32),
            "review_gates": int_value(coordination["review_gates"], "candidate.coordination_estimate.review_gates", 1, 128),
            "estimated_coordination_steps": int_value(coordination["estimated_coordination_steps"], "candidate.coordination_estimate.estimated_coordination_steps", 1, 512),
        },
        "fallback": {
            "static_topology_id": text_value(fallback["static_topology_id"], "candidate.fallback.static_topology_id", 3, 120),
            "trigger": text_value(fallback["trigger"], "candidate.fallback.trigger", 3, 500),
        },
    }


def walk_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        result: list[str] = []
        for key, item in value.items():
            if key not in {"model_identity", "provider_lineage", "fleet_run_id"}:
                result.extend(walk_strings(item))
        return result
    if isinstance(value, list):
        result = []
        for item in value:
            result.extend(walk_strings(item))
        return result
    return []


def reject_executable_or_worker_selection(candidate: dict[str, Any]) -> None:
    for text in walk_strings(candidate):
        if any(pattern.search(text) for pattern in EXECUTABLE_PATTERNS):
            raise JitError("executable-payload", "Candidate contains executable-like content")
        if WORKER_SELECTION_RE.search(text):
            raise JitError("worker-selection", "Candidate attempts to select a worker model")


def normalize_scope(value: str) -> str:
    return value.replace("\\", "/").rstrip("/").lower()


def scope_prefix(value: str) -> str:
    normalized = normalize_scope(value)
    wildcard = len(normalized)
    for marker in ("*", "?", "["):
        index = normalized.find(marker)
        if index >= 0:
            wildcard = min(wildcard, index)
    return normalized[:wildcard].rstrip("/")


def scopes_overlap(left: str, right: str) -> bool:
    a = scope_prefix(left)
    b = scope_prefix(right)
    if not a or not b:
        return True
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def transitive_reachability(task_ids: set[str], dependencies: list[dict[str, str]]) -> dict[str, set[str]]:
    outgoing = {task_id: set() for task_id in task_ids}
    indegree = {task_id: 0 for task_id in task_ids}
    for edge in dependencies:
        outgoing[edge["from"]].add(edge["to"])
        indegree[edge["to"]] += 1
    queue = sorted(task_id for task_id, degree in indegree.items() if degree == 0)
    visited: list[str] = []
    while queue:
        current = queue.pop(0)
        visited.append(current)
        for target in sorted(outgoing[current]):
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
                queue.sort()
    if len(visited) != len(task_ids):
        raise JitError("topology-cycle", "Candidate dependency graph contains a cycle")
    reachable = {task_id: set() for task_id in task_ids}
    for source in reversed(visited):
        for target in outgoing[source]:
            reachable[source].add(target)
            reachable[source].update(reachable[target])
    return reachable


def compare_authority(manifest: dict[str, Any], candidate: dict[str, Any]) -> None:
    manifest_hash = sha256_value({
        **manifest,
        "authority_ceilings": list(manifest["authority_ceilings"].values()),
    })
    if candidate["synthesis_input_sha256"] != manifest_hash:
        raise JitError("manifest-binding", "Candidate does not bind the frozen synthesis manifest")
    if candidate["planning_contract_sha256"] != manifest["planning_contract_sha256"]:
        raise JitError("planning-binding", "Candidate planning contract hash mismatch")
    if candidate["static_topology_id"] != manifest["static_topology_id"]:
        raise JitError("topology-binding", "Candidate static topology mismatch")
    if candidate["fallback"]["static_topology_id"] != manifest["static_topology_id"]:
        raise JitError("fallback-binding", "Candidate fallback topology mismatch")
    required = set(manifest["requirements"])
    if set(candidate["requirements_covered"]) != required:
        raise JitError("requirement-coverage", "Candidate requirement coverage is not exact")
    if len(candidate["tasks"]) > manifest["global_budget"]["max_tasks"]:
        raise JitError("global-budget", "Candidate exceeds task budget")
    estimate = candidate["coordination_estimate"]
    for candidate_field, manifest_field in (
        ("parallel_streams", "max_parallel_streams"),
        ("review_gates", "max_review_gates"),
        ("estimated_coordination_steps", "max_coordination_steps"),
    ):
        if estimate[candidate_field] > manifest["global_budget"][manifest_field]:
            raise JitError("global-budget", f"Candidate exceeds {candidate_field} ceiling")
    if estimate["parallel_streams"] > len(candidate["tasks"]):
        raise JitError("coordination-estimate", "Parallel stream count exceeds task count")
    if estimate["review_gates"] < len(candidate["tasks"]):
        raise JitError("missing-review-gate", "Every task requires an independent review gate")

    covered: set[str] = set()
    aggregate: dict[str, dict[str, int]] = {}
    for task in candidate["tasks"].values():
        ref = task["authority_ceiling_ref"]
        ceiling = manifest["authority_ceilings"].get(ref)
        if ceiling is None:
            raise JitError("authority-reference", f"Unknown authority ceiling: {ref}")
        if task["static_task_contract_sha256"] != sha256_value(ceiling):
            raise JitError("authority-binding", f"Static task contract hash mismatch for {task['task_id']}")
        if task["capability"] != ceiling["capability"]:
            raise JitError("capability-expansion", f"Capability mismatch for {task['task_id']}")
        task_requirements = set(task["requirement_refs"])
        if not task_requirements <= set(ceiling["requirement_refs"]):
            raise JitError("requirement-expansion", f"Task requirements exceed ceiling for {task['task_id']}")
        covered.update(task_requirements)
        candidate_context = {(item["path"], item["sha256"]) for item in task["context_refs"]}
        required_context = {(item["path"], item["sha256"]) for item in ceiling["required_context_refs"]}
        allowed_context = {(item["path"], item["sha256"]) for item in ceiling["allowed_context_refs"]}
        if not required_context <= candidate_context or not candidate_context <= allowed_context:
            raise JitError("context-authority", f"Context mismatch for {task['task_id']}")
        for field, ceiling_field in (
            ("skill_bindings", "skill_allowlist"),
            ("tool_allowlist", "tool_allowlist"),
            ("read_scope", "read_scope"),
            ("write_scope", "write_scope"),
        ):
            if not set(task[field]) <= set(ceiling[ceiling_field]):
                raise JitError(f"{field}-expansion", f"{field} exceeds ceiling for {task['task_id']}")
        if not set(ceiling["required_stop_conditions"]) <= set(task["stop_conditions"]):
            raise JitError("stop-condition-loss", f"Required stop condition missing for {task['task_id']}")
        if not set(ceiling["required_evidence_refs"]) <= set(task["evidence_refs"]):
            raise JitError("evidence-loss", f"Required evidence missing for {task['task_id']}")
        if not set(ceiling["required_criterion_refs"]) <= set(task["criterion_refs"]):
            raise JitError("criterion-loss", f"Required criterion missing for {task['task_id']}")
        if not set(ceiling["required_reviewer_capabilities"]) <= set(task["reviewer_constraint"]["required_capabilities"]):
            raise JitError("reviewer-capability-loss", f"Reviewer capability missing for {task['task_id']}")
        if REPAIR_STRICTNESS[task["repair_route"]["on_reject"]] > REPAIR_STRICTNESS[ceiling["repair_ceiling"]["on_reject"]]:
            raise JitError("repair-expansion", f"Repair route is looser for {task['task_id']}")
        if task["repair_route"]["on_exhaustion"] != "block" and ceiling["repair_ceiling"]["on_reject"] == "block":
            raise JitError("repair-expansion", f"Repair exhaustion is looser for {task['task_id']}")
        totals = aggregate.setdefault(ref, {key: 0 for key in task["budget"]})
        for key, value in task["budget"].items():
            totals[key] += value

    if covered != required:
        raise JitError("requirement-coverage", "Candidate tasks do not cover every required requirement")
    for ref, totals in aggregate.items():
        ceiling_budget = manifest["authority_ceilings"][ref]["budget"]
        for key, total in totals.items():
            if total > ceiling_budget[key]:
                raise JitError("budget-expansion", f"Aggregate {key} exceeds authority ceiling {ref}")

    reachability = transitive_reachability(set(candidate["tasks"]), candidate["dependencies"])
    task_items = list(candidate["tasks"].values())
    for index, left in enumerate(task_items):
        for right in task_items[index + 1 :]:
            ordered = right["task_id"] in reachability[left["task_id"]] or left["task_id"] in reachability[right["task_id"]]
            if ordered:
                continue
            if any(scopes_overlap(a, b) for a in left["write_scope"] for b in right["write_scope"]):
                raise JitError("unordered-write-conflict", f"Unordered tasks overlap write scope: {left['task_id']}, {right['task_id']}")


def activation_score(signals: dict[str, bool]) -> int:
    return (
        2 * int(signals["high_complexity"])
        + 2 * int(signals["bounded_ambiguity"])
        + 2 * int(signals["multi_system_context"])
        + 2 * int(signals["parallel_streams"])
        + int(signals["multiple_gates"])
        + int(signals["repair_history"])
    )


def manifest_hash(manifest: dict[str, Any]) -> str:
    normalized = dict(manifest)
    normalized["authority_ceilings"] = list(manifest["authority_ceilings"].values())
    return sha256_value(normalized)


def receipt(
    *,
    static_hash: str,
    candidate_hash: str | None,
    decision: str,
    reason_codes: list[str],
) -> dict[str, Any]:
    base = {
        "schema_version": SCHEMA_VERSION,
        "stage": "stage-a",
        "static_contract_sha256": static_hash,
        "candidate_sha256": candidate_hash,
        "decision": decision,
        "reason_codes": sorted(set(reason_codes)),
        "static_path_selected": True,
    }
    receipt_id = "jit-receipt-" + sha256_value(base)[:24]
    return {"schema_version": SCHEMA_VERSION, "stage": "stage-a", "receipt_id": receipt_id, **{key: value for key, value in base.items() if key not in {"schema_version", "stage"}}}


def evaluate(manifest_path: Path, candidate_path: Path) -> dict[str, Any]:
    manifest_raw = load_json(manifest_path, "manifest")
    manifest = validate_manifest(manifest_raw)
    static_hash = manifest_hash(manifest)
    candidate_hash: str | None = None
    try:
        candidate_raw = load_json(candidate_path, "candidate")
        candidate_hash = sha256_value(candidate_raw)
        reject_executable_or_worker_selection(candidate_raw)
        candidate = validate_candidate(candidate_raw)
        compare_authority(manifest, candidate)
    except JitError as exc:
        return receipt(
            static_hash=static_hash,
            candidate_hash=candidate_hash,
            decision="rejected",
            reason_codes=[exc.code, "static-fallback"],
        )
    score = activation_score(manifest["signals"])
    reasons = [f"score-{score}", "shadow-only", "static-fallback"]
    if manifest["control_veto"]:
        return receipt(
            static_hash=static_hash,
            candidate_hash=candidate_hash,
            decision="disabled",
            reason_codes=reasons + ["control-veto"],
        )
    if score < 4:
        return receipt(
            static_hash=static_hash,
            candidate_hash=candidate_hash,
            decision="disabled",
            reason_codes=reasons + ["below-shadow-threshold"],
        )
    return receipt(
        static_hash=static_hash,
        candidate_hash=candidate_hash,
        decision="accepted-shadow",
        reason_codes=reasons + (["active-path-not-implemented"] if score >= 7 else []),
    )


def unavailable(manifest_path: Path, reason: str) -> dict[str, Any]:
    manifest = validate_manifest(load_json(manifest_path, "manifest"))
    decision = "disabled" if reason == "disabled" else reason
    return receipt(
        static_hash=manifest_hash(manifest),
        candidate_hash=None,
        decision=decision,
        reason_codes=[reason, "static-fallback"],
    )


def emit(value: dict[str, Any], output: str | None) -> None:
    if output:
        write_new_json(Path(output), value)
    print(json.dumps(value, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate Stage A JIT candidates in shadow-only mode.")
    parser.add_argument("--version", action="version", version=VERSION)
    subparsers = parser.add_subparsers(dest="command", required=True)
    doctor = subparsers.add_parser("doctor")
    doctor.set_defaults(handler=lambda _args: {"status": "READY", "version": VERSION, "mode": "shadow-only"})
    hash_parser = subparsers.add_parser("hash-manifest")
    hash_parser.add_argument("--manifest", required=True)
    hash_parser.set_defaults(
        handler=lambda args: {
            "synthesis_input_sha256": manifest_hash(
                validate_manifest(load_json(Path(args.manifest), "manifest"))
            )
        }
    )
    evaluate_parser = subparsers.add_parser("evaluate")
    evaluate_parser.add_argument("--manifest", required=True)
    evaluate_parser.add_argument("--candidate", required=True)
    evaluate_parser.add_argument("--output")
    evaluate_parser.set_defaults(handler=lambda args: evaluate(Path(args.manifest), Path(args.candidate)))
    unavailable_parser = subparsers.add_parser("unavailable")
    unavailable_parser.add_argument("--manifest", required=True)
    unavailable_parser.add_argument("--reason", choices=["unavailable", "late", "disabled"], required=True)
    unavailable_parser.add_argument("--output")
    unavailable_parser.set_defaults(handler=lambda args: unavailable(Path(args.manifest), args.reason))
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        value = args.handler(args)
        emit(value, getattr(args, "output", None))
        return 0
    except JitError as exc:
        print(json.dumps({"status": "ERROR", "reason_code": exc.code}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
