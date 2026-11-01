#!/usr/bin/env python3
"""Agent Graph state validation and bounded schedule entrypoint.

State commands only validate/record. The explicit run command delegates execution
to graph_runner; existing Fleet/Loop remain the executors and model routers.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys
sys.dont_write_bytecode = True
import tempfile
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator


VERSION = "0.5.3"
SCHEMA_VERSION = 1
SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE_ROOT = Path.home() / ".agents" / "agent-graph" / "runs"

GRAPH_ID_RE = re.compile(r"^graph-[a-z0-9][a-z0-9-]{2,63}$")
NODE_ID_RE = re.compile(r"^node-[a-z0-9][a-z0-9-]{2,63}$")
CRITERION_ID_RE = re.compile(r"^crit-[a-z0-9][a-z0-9-]{2,63}$")
EVIDENCE_ID_RE = re.compile(r"^evidence-[a-z0-9][a-z0-9-]{2,95}$")
AMENDMENT_ID_RE = re.compile(r"^amend-[a-z0-9][a-z0-9-]{2,95}$")
OPERATION_ID_RE = re.compile(r"^op-[a-z0-9][a-z0-9-]{2,95}$")
RUN_ID_RE = re.compile(r"^run-graph-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{10}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
LOCK_RE = re.compile(r"^[a-z0-9][a-z0-9:./_-]{1,255}$")
PLANNER_VALIDATOR_SHA256 = "9b4c1ec8a66ce42d37b55de1b1a3990dc26bf5f8b953fa223d3f7b800de4f3e0"
ACCEPTANCE_VALIDATOR_SHA256 = "b6c250fb3d23612ff44da06a7b7904a1299a0626f6bd8b352250dceec2a3d343"

NODE_KINDS = {"work", "verify", "gate", "human"}
CAPABILITIES = {
    "discovery",
    "contract-definition",
    "implementation",
    "integration",
    "deterministic-tests",
    "user-journey-qa",
    "visual-qa",
    "ux-review",
    "accessibility-review",
    "independent-quality",
    "incremental-security",
    "deep-security-approval",
    "production-approval",
    "final-acceptance",
}
EXECUTORS = {
    "fleet-run",
    "agent-loop-l2",
    "fleet-quality-gate",
    "fleet-security-gate",
    "parent-local",
    "human",
}
WORKSPACE_MODES = {"read-only", "detached-worktree", "integration-owner", "none"}
CLASSIFICATIONS = {"public", "internal", "confidential", "restricted"}
EVIDENCE_KINDS = {
    "artifact",
    "deterministic-check",
    "fleet-handoff",
    "quality-gate",
    "security-gate",
    "browser-e2e",
    "visual-evidence",
    "human-approval",
    "parent-acceptance",
}
EXPECTED_EXTERNAL_REF = {
    "fleet-run": "fleet_run",
    "agent-loop-l2": "agent_loop",
    "fleet-quality-gate": "fleet_quality_gate",
    "fleet-security-gate": "fleet_security_gate",
    "human": "human_approval",
}
EXTERNAL_REF_KINDS = {
    "fleet_run",
    "agent_loop",
    "fleet_quality_gate",
    "fleet_security_gate",
    "human_approval",
    "parent_acceptance",
}


class GraphError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GraphError(f"Invalid UTC timestamp: {value}") from exc


def canonical_bytes(value: Any) -> bytes:
    text = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return unicodedata.normalize("NFC", text).encode("utf-8")


def sha256_value(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise GraphError(f"Missing {label}: {path}") from exc
    except json.JSONDecodeError as exc:
        raise GraphError(f"Invalid JSON in {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise GraphError(f"{label} must be a JSON object: {path}")
    return value


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def require_exact_keys(
    value: dict[str, Any], required: set[str], optional: set[str], label: str
) -> None:
    keys = set(value)
    missing = required - keys
    extra = keys - required - optional
    if missing:
        raise GraphError(f"{label} missing keys: {', '.join(sorted(missing))}")
    if extra:
        raise GraphError(f"{label} has unsupported keys: {', '.join(sorted(extra))}")


def require_string(value: Any, label: str, minimum: int = 1, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not (minimum <= len(value) <= maximum):
        raise GraphError(f"{label} must be a string of length {minimum}..{maximum}")
    return value


def require_int(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not (minimum <= value <= maximum):
        raise GraphError(f"{label} must be an integer in {minimum}..{maximum}")
    return value


def require_nullable_int(value: Any, label: str, minimum: int, maximum: int) -> int | None:
    if value is None:
        return None
    return require_int(value, label, minimum, maximum)


def normalize_scope(value: Any, label: str, allow_directory: bool = True) -> str:
    text = require_string(value, label, 1, 512)
    if "\\" in text or any(character in text for character in '*?"<>|'):
        raise GraphError(f"{label} must use normalized POSIX syntax without wildcards: {text}")
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise GraphError(f"{label} must be repository-relative: {text}")
    is_directory = text.endswith("/")
    if is_directory and not allow_directory:
        raise GraphError(f"{label} must identify a file, not a directory: {text}")
    body = text[:-1] if is_directory else text
    parts = body.split("/")
    if not body or any(part in {"", ".", ".."} for part in parts):
        raise GraphError(f"{label} contains an empty, dot, or parent segment: {text}")
    return body + ("/" if is_directory else "")


def scope_contains(scope: str, relative_path: str) -> bool:
    base = scope[:-1] if scope.endswith("/") else scope
    if scope.endswith("/"):
        return relative_path == base or relative_path.startswith(base + "/")
    return relative_path == base


def scopes_overlap(left: str, right: str) -> bool:
    left_base = left[:-1] if left.endswith("/") else left
    right_base = right[:-1] if right.endswith("/") else right
    if left_base == right_base:
        return True
    if left.endswith("/") and right_base.startswith(left_base + "/"):
        return True
    if right.endswith("/") and left_base.startswith(right_base + "/"):
        return True
    return False


def validate_budget(value: Any, label: str, global_budget: bool = False) -> None:
    if not isinstance(value, dict):
        raise GraphError(f"{label} must be an object")
    if value.get("token_policy", "hard") not in {"hard", "estimate"}:
        raise GraphError(f"{label}.token_policy must be hard or estimate")
    if global_budget:
        if value.get("time_policy", "estimate") not in {"hard", "estimate"}:
            raise GraphError(f"{label}.time_policy must be hard or estimate")
        if value.get("clock", "elapsed") not in {"elapsed", "active"}:
            raise GraphError(f"{label}.clock must be elapsed or active")
        require_exact_keys(
            value,
            {"max_total_attempts", "max_total_tokens", "max_wall_seconds", "max_amendments"},
            {"token_policy", "time_policy", "clock"},
            label,
        )
        require_int(value["max_total_attempts"], f"{label}.max_total_attempts", 1, 512)
        require_nullable_int(value["max_total_tokens"], f"{label}.max_total_tokens", 1, 100_000_000)
        require_int(value["max_wall_seconds"], f"{label}.max_wall_seconds", 60, 604_800)
        require_int(value["max_amendments"], f"{label}.max_amendments", 0, 32)
        return
    require_exact_keys(
        value,
        {"max_attempts", "max_wall_seconds", "max_tool_calls", "max_tokens"},
        {"token_policy", "tool_policy"},
        label,
    )
    if value.get("tool_policy", "estimate") not in {"hard", "estimate"}:
        raise GraphError(f"{label}.tool_policy must be hard or estimate")
    require_int(value["max_attempts"], f"{label}.max_attempts", 1, 10)
    require_int(value["max_wall_seconds"], f"{label}.max_wall_seconds", 30, 86_400)
    require_int(value["max_tool_calls"], f"{label}.max_tool_calls", 0, 2048)
    require_nullable_int(value["max_tokens"], f"{label}.max_tokens", 1, 10_000_000)


def validate_node(node: Any, index: int) -> dict[str, Any]:
    label = f"nodes[{index}]"
    if not isinstance(node, dict):
        raise GraphError(f"{label} must be an object")
    required = {
        "node_id",
        "active",
        "kind",
        "capability",
        "title",
        "depends_on",
        "evidence_from",
        "executor",
        "reviewer",
        "workspace_mode",
        "read_scope",
        "write_scope",
        "resource_locks",
        "acceptance_criteria",
        "budget",
        "repair_policy",
        "loop_policy",
        "council_trigger",
    }
    require_exact_keys(
        node,
        required,
        {"expires_after_seconds", "maker_lineage", "reviewer_lineage"},
        label,
    )
    node_id = require_string(node["node_id"], f"{label}.node_id", 4, 69)
    if not NODE_ID_RE.fullmatch(node_id):
        raise GraphError(f"Invalid node_id: {node_id}")
    if not isinstance(node["active"], bool):
        raise GraphError(f"{label}.active must be a boolean")
    if node["kind"] not in NODE_KINDS:
        raise GraphError(f"Unsupported {label}.kind: {node['kind']}")
    if node["capability"] not in CAPABILITIES:
        raise GraphError(f"Unsupported {label}.capability: {node['capability']}")
    require_string(node["title"], f"{label}.title", 4, 200)
    if node["executor"] not in EXECUTORS:
        raise GraphError(f"Unsupported {label}.executor: {node['executor']}")
    reviewer = require_string(node["reviewer"], f"{label}.reviewer", 3, 200)
    if reviewer == node["executor"]:
        raise GraphError(f"{node_id} cannot self-review")
    maker_lineage = node.get("maker_lineage")
    reviewer_lineage = node.get("reviewer_lineage")
    if (maker_lineage is None) != (reviewer_lineage is None):
        raise GraphError(
            f"{node_id} must declare both maker_lineage and reviewer_lineage"
        )
    if maker_lineage is not None:
        maker_lineage = require_string(
            maker_lineage, f"{label}.maker_lineage", 2, 100
        )
        reviewer_lineage = require_string(
            reviewer_lineage, f"{label}.reviewer_lineage", 2, 100
        )
        if maker_lineage.casefold() == reviewer_lineage.casefold():
            raise GraphError(
                f"{node_id} maker_lineage cannot equal reviewer_lineage"
            )
    if node["workspace_mode"] not in WORKSPACE_MODES:
        raise GraphError(f"Unsupported {label}.workspace_mode: {node['workspace_mode']}")

    for field in ("depends_on", "evidence_from"):
        values = node[field]
        if not isinstance(values, list) or len(values) > 32 or len(values) != len(set(values)):
            raise GraphError(f"{label}.{field} must be a unique array with at most 32 entries")
        for dependency in values:
            if not isinstance(dependency, str) or not NODE_ID_RE.fullmatch(dependency):
                raise GraphError(f"Invalid {label}.{field} node ID: {dependency}")
            if dependency == node_id:
                raise GraphError(f"{node_id} cannot reference itself in {field}")

    for field in ("read_scope", "write_scope"):
        scopes = node[field]
        if not isinstance(scopes, list) or len(scopes) > 128:
            raise GraphError(f"{label}.{field} must be an array with at most 128 entries")
        normalized = [normalize_scope(item, f"{label}.{field}") for item in scopes]
        if len(normalized) != len(set(normalized)):
            raise GraphError(f"{label}.{field} contains duplicates")
        if normalized != scopes:
            raise GraphError(f"{label}.{field} is not normalized")

    locks = node["resource_locks"]
    if not isinstance(locks, list) or len(locks) > 128 or len(locks) != len(set(locks)):
        raise GraphError(f"{label}.resource_locks must be a unique array with at most 128 entries")
    for lock in locks:
        if not isinstance(lock, str) or not LOCK_RE.fullmatch(lock):
            raise GraphError(f"Invalid resource lock in {node_id}: {lock}")

    criteria = node["acceptance_criteria"]
    if not isinstance(criteria, list) or not (1 <= len(criteria) <= 64):
        raise GraphError(f"{label}.acceptance_criteria must contain 1..64 items")
    criterion_ids: set[str] = set()
    for criterion_index, criterion in enumerate(criteria):
        criterion_label = f"{label}.acceptance_criteria[{criterion_index}]"
        if not isinstance(criterion, dict):
            raise GraphError(f"{criterion_label} must be an object")
        require_exact_keys(
            criterion, {"criterion_id", "description", "evidence_kind"}, set(), criterion_label
        )
        criterion_id = require_string(
            criterion["criterion_id"], f"{criterion_label}.criterion_id", 4, 69
        )
        if not CRITERION_ID_RE.fullmatch(criterion_id):
            raise GraphError(f"Invalid criterion_id: {criterion_id}")
        if criterion_id in criterion_ids:
            raise GraphError(f"Duplicate criterion_id in {node_id}: {criterion_id}")
        criterion_ids.add(criterion_id)
        require_string(criterion["description"], f"{criterion_label}.description", 8, 1000)
        if criterion["evidence_kind"] not in EVIDENCE_KINDS:
            raise GraphError(f"Unsupported evidence kind in {criterion_label}")

    validate_budget(node["budget"], f"{label}.budget")
    repair = node["repair_policy"]
    if not isinstance(repair, dict):
        raise GraphError(f"{label}.repair_policy must be an object")
    require_exact_keys(
        repair, {"on_reject", "max_same_scope_repairs", "max_amendments"}, set(), f"{label}.repair_policy"
    )
    if repair["on_reject"] not in {"retry-same-scope", "amend-graph", "block", "human-gate"}:
        raise GraphError(f"Unsupported {label}.repair_policy.on_reject")
    require_int(repair["max_same_scope_repairs"], f"{label}.repair_policy.max_same_scope_repairs", 0, 5)
    require_int(repair["max_amendments"], f"{label}.repair_policy.max_amendments", 0, 10)

    loop = node["loop_policy"]
    if not isinstance(loop, dict):
        raise GraphError(f"{label}.loop_policy must be an object")
    require_exact_keys(loop, {"enabled", "max_rounds", "stop_condition"}, set(), f"{label}.loop_policy")
    if not isinstance(loop["enabled"], bool):
        raise GraphError(f"{label}.loop_policy.enabled must be boolean")
    require_int(loop["max_rounds"], f"{label}.loop_policy.max_rounds", 0, 10)
    if not loop["enabled"] and loop["max_rounds"] != 0:
        raise GraphError(f"{label}.loop_policy.max_rounds must be 0 when disabled")
    require_string(loop["stop_condition"], f"{label}.loop_policy.stop_condition", 3, 1000)

    council = node["council_trigger"]
    if not isinstance(council, dict):
        raise GraphError(f"{label}.council_trigger must be an object")
    require_exact_keys(council, {"required", "reason"}, set(), f"{label}.council_trigger")
    if not isinstance(council["required"], bool):
        raise GraphError(f"{label}.council_trigger.required must be boolean")
    require_string(council["reason"], f"{label}.council_trigger.reason", 3, 1000)

    expiry = node.get("expires_after_seconds")
    if expiry is not None:
        require_int(expiry, f"{label}.expires_after_seconds", 60, 2_592_000)

    mutating = bool(node["write_scope"])
    if mutating and node["workspace_mode"] not in {"detached-worktree", "integration-owner"}:
        raise GraphError(f"Mutating node {node_id} needs detached-worktree or integration-owner")
    if not mutating and node["workspace_mode"] in {"detached-worktree", "integration-owner"}:
        raise GraphError(f"Non-mutating node {node_id} cannot claim a mutating workspace mode")
    if node["workspace_mode"] == "read-only" and mutating:
        raise GraphError(f"Read-only node {node_id} declares write scope")
    if node["kind"] == "human":
        if node["executor"] != "human" or node["workspace_mode"] != "none" or mutating:
            raise GraphError(f"Human node {node_id} must use executor=human, workspace_mode=none, no writes")
    if node["executor"] == "human" and node["kind"] != "human":
        raise GraphError(f"Only human nodes may use executor=human: {node_id}")
    if node["executor"] == "fleet-quality-gate" and node["capability"] != "independent-quality":
        raise GraphError(f"fleet-quality-gate requires independent-quality capability: {node_id}")
    if node["executor"] == "fleet-security-gate" and node["capability"] != "incremental-security":
        raise GraphError(f"fleet-security-gate requires incremental-security capability: {node_id}")
    if node["capability"] == "integration" and node["workspace_mode"] != "integration-owner":
        raise GraphError(f"Integration capability must be the integration owner: {node_id}")
    if node["capability"] == "final-acceptance" and not (
        node["kind"] == "gate" and node["executor"] == "parent-local"
    ):
        raise GraphError(f"Final acceptance must be a parent-local gate: {node_id}")
    return node


def dependency_ancestors(node_id: str, nodes: dict[str, dict[str, Any]]) -> set[str]:
    ancestors: set[str] = set()
    stack = list(nodes[node_id]["depends_on"])
    while stack:
        current = stack.pop()
        if current in ancestors:
            continue
        ancestors.add(current)
        stack.extend(nodes[current]["depends_on"])
    return ancestors


def provenance_ancestors(node_id: str, nodes: dict[str, dict[str, Any]]) -> set[str]:
    ancestors: set[str] = set()
    stack = list(nodes[node_id]["depends_on"]) + list(nodes[node_id]["evidence_from"])
    while stack:
        current = stack.pop()
        if current in ancestors:
            continue
        ancestors.add(current)
        stack.extend(nodes[current]["depends_on"])
        stack.extend(nodes[current]["evidence_from"])
    return ancestors


def transitive_descendants(
    node_ids: set[str], nodes: dict[str, dict[str, Any]], include_evidence: bool = True
) -> set[str]:
    descendants = set(node_ids)
    changed = True
    while changed:
        changed = False
        for candidate_id, candidate in nodes.items():
            refs = set(candidate["depends_on"])
            if include_evidence:
                refs.update(candidate["evidence_from"])
            if candidate_id not in descendants and refs & descendants:
                descendants.add(candidate_id)
                changed = True
    return descendants


def validate_graph(graph: dict[str, Any]) -> dict[str, Any]:
    require_exact_keys(
        graph,
        {
            "schema_version",
            "graph_id",
            "revision",
            "previous_revision_hash",
            "intent",
            "project_root",
            "data_classification",
            "integration_node_id",
            "global_budget",
            "nodes",
        },
        set(),
        "graph",
    )
    if graph["schema_version"] != SCHEMA_VERSION:
        raise GraphError(f"Unsupported graph schema_version: {graph['schema_version']}")
    graph_id = require_string(graph["graph_id"], "graph.graph_id", 4, 69)
    if not GRAPH_ID_RE.fullmatch(graph_id):
        raise GraphError(f"Invalid graph_id: {graph_id}")
    revision = require_int(graph["revision"], "graph.revision", 1, 10_000)
    previous_hash = graph["previous_revision_hash"]
    if revision == 1 and previous_hash is not None:
        raise GraphError("Revision 1 must have previous_revision_hash=null")
    if revision > 1 and (not isinstance(previous_hash, str) or not SHA256_RE.fullmatch(previous_hash)):
        raise GraphError("Revision >1 requires a lowercase SHA-256 previous_revision_hash")
    require_string(graph["intent"], "graph.intent", 10, 2000)
    project_root = require_string(graph["project_root"], "graph.project_root", 1, 1024)
    if not os.path.isabs(project_root):
        raise GraphError("graph.project_root must be absolute")
    if graph["data_classification"] not in CLASSIFICATIONS:
        raise GraphError(f"Unsupported data classification: {graph['data_classification']}")
    validate_budget(graph["global_budget"], "graph.global_budget", global_budget=True)

    raw_nodes = graph["nodes"]
    if not isinstance(raw_nodes, list) or not (1 <= len(raw_nodes) <= 128):
        raise GraphError("graph.nodes must contain 1..128 nodes")
    nodes: dict[str, dict[str, Any]] = {}
    all_criteria: set[str] = set()
    for index, raw_node in enumerate(raw_nodes):
        node = validate_node(raw_node, index)
        node_id = node["node_id"]
        if node_id in nodes:
            raise GraphError(f"Duplicate node_id: {node_id}")
        nodes[node_id] = node
        for criterion in node["acceptance_criteria"]:
            criterion_id = criterion["criterion_id"]
            if criterion_id in all_criteria:
                raise GraphError(f"criterion_id must be graph-unique: {criterion_id}")
            all_criteria.add(criterion_id)

    for node_id, node in nodes.items():
        for field in ("depends_on", "evidence_from"):
            missing = [reference for reference in node[field] if reference not in nodes]
            if missing:
                raise GraphError(f"{node_id}.{field} references missing nodes: {', '.join(missing)}")
        if node["active"]:
            inactive_dependencies = [
                dependency for dependency in node["depends_on"] if not nodes[dependency]["active"]
            ]
            if inactive_dependencies:
                raise GraphError(
                    f"Active node {node_id} depends on inactive nodes: {', '.join(inactive_dependencies)}"
                )

    active_nodes = {node_id: node for node_id, node in nodes.items() if node["active"]}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visiting:
            raise GraphError(f"Dependency cycle detected at {node_id}")
        if node_id in visited:
            return
        visiting.add(node_id)
        for dependency in active_nodes[node_id]["depends_on"]:
            visit(dependency)
        visiting.remove(node_id)
        visited.add(node_id)

    for node_id in active_nodes:
        visit(node_id)

    for node_id, node in active_nodes.items():
        ancestors = dependency_ancestors(node_id, active_nodes)
        active_evidence = {
            source for source in node["evidence_from"] if active_nodes.get(source)
        }
        unrelated_evidence = sorted(active_evidence - ancestors)
        if unrelated_evidence:
            raise GraphError(
                f"{node_id}.evidence_from must cite dependency ancestors: "
                + ", ".join(unrelated_evidence)
            )

    mutating = [node for node in active_nodes.values() if node["write_scope"]]
    parallel_pairs: list[tuple[str, str]] = []
    for left_index, left in enumerate(mutating):
        for right in mutating[left_index + 1 :]:
            left_ancestors = dependency_ancestors(left["node_id"], active_nodes)
            right_ancestors = dependency_ancestors(right["node_id"], active_nodes)
            ordered = left["node_id"] in right_ancestors or right["node_id"] in left_ancestors
            if ordered:
                continue
            parallel_pairs.append((left["node_id"], right["node_id"]))
            overlaps = [
                (left_scope, right_scope)
                for left_scope in left["write_scope"]
                for right_scope in right["write_scope"]
                if scopes_overlap(left_scope, right_scope)
            ]
            if overlaps:
                rendered = ", ".join(f"{a}<->{b}" for a, b in overlaps)
                raise GraphError(
                    f"Unordered mutating nodes overlap: {left['node_id']} and {right['node_id']}: {rendered}"
                )
            shared_locks = sorted(set(left["resource_locks"]) & set(right["resource_locks"]))
            if shared_locks:
                raise GraphError(
                    f"Unordered mutating nodes share resource locks: {left['node_id']} and "
                    f"{right['node_id']}: {', '.join(shared_locks)}"
                )

    integration_id = graph["integration_node_id"]
    if integration_id is not None:
        if not isinstance(integration_id, str) or not NODE_ID_RE.fullmatch(integration_id):
            raise GraphError("Invalid integration_node_id")
        if integration_id not in active_nodes:
            raise GraphError("integration_node_id must reference an active node")
        integration = active_nodes[integration_id]
        if integration["capability"] != "integration" or integration["workspace_mode"] != "integration-owner":
            raise GraphError("integration_node_id must reference the active integration owner")
    if parallel_pairs:
        if integration_id is None:
            raise GraphError("Parallel mutating branches require integration_node_id")
        integration_ancestors = dependency_ancestors(integration_id, active_nodes)
        branch_ids = {node_id for pair in parallel_pairs for node_id in pair}
        missing_branches = sorted(branch_ids - integration_ancestors)
        if missing_branches:
            raise GraphError(
                "Integration owner does not depend on all parallel mutation branches: "
                + ", ".join(missing_branches)
            )

    implementation_ids = [
        node_id for node_id, node in active_nodes.items() if node["capability"] == "implementation"
    ]
    integration_ids = [
        node_id for node_id, node in active_nodes.items() if node["capability"] == "integration"
    ]
    quality_target_ids = [
        node_id
        for node_id, node in active_nodes.items()
        if node["capability"]
        in {
            "implementation",
            "integration",
            "deterministic-tests",
            "user-journey-qa",
            "visual-qa",
            "ux-review",
            "accessibility-review",
        }
    ]
    quality_nodes = [
        node for node in active_nodes.values() if node["executor"] == "fleet-quality-gate"
    ]
    security_nodes = [
        node for node in active_nodes.values() if node["executor"] == "fleet-security-gate"
    ]
    gate_ids = {node["node_id"] for node in quality_nodes + security_nodes}
    gate_review_edges = sorted(
        (node["node_id"], reference)
        for node in quality_nodes + security_nodes
        for reference in node["evidence_from"] + node["depends_on"]
        if reference in gate_ids
    )
    if implementation_ids:
        if not quality_nodes:
            raise GraphError("Graphs with implementation nodes require an independent quality gate")
        quality_covered = {
            source for quality in quality_nodes for source in quality["evidence_from"]
        }
        missing_quality = sorted(set(quality_target_ids) - quality_covered)
        if missing_quality:
            raise GraphError(
                "Independent quality gate does not explicitly cover delivery nodes: "
                + ", ".join(missing_quality)
            )
        if not security_nodes:
            raise GraphError("Graphs with implementation nodes require an incremental security gate")
        security_covered = {
            source for security in security_nodes for source in security["evidence_from"]
        }
        missing_security = sorted(
            (set(implementation_ids) | set(integration_ids)) - security_covered
        )
        if missing_security:
            raise GraphError(
                "Incremental security gate does not explicitly cover implementation batch nodes: "
                + ", ".join(missing_security)
            )

    final_nodes = [
        node for node in active_nodes.values() if node["capability"] == "final-acceptance"
    ]
    if len(final_nodes) != 1:
        raise GraphError("Exactly one active final-acceptance node is required")
    return {
        "graph_id": graph_id,
        "revision": revision,
        "node_count": len(nodes),
        "active_node_count": len(active_nodes),
        "parallel_mutation_pairs": [list(pair) for pair in parallel_pairs],
        "scheduling_warnings": [
            f"Check whether gate dependency {left}->{right} is a real dependency, not review-of-review"
            for left, right in sorted(set(gate_review_edges))
        ],
        "graph_hash": sha256_value(graph),
    }


def validate_evidence(
    evidence: dict[str, Any], node: dict[str, Any], expected_workspace_root: str
) -> dict[str, Any]:
    required = {
        "schema_version",
        "evidence_id",
        "node_id",
        "verdict",
        "summary",
        "workspace_root",
        "changed_paths",
        "artifacts",
        "criteria_results",
        "checks",
        "external_refs",
        "findings",
        "telemetry",
    }
    require_exact_keys(evidence, required, set(), "evidence")
    if evidence["schema_version"] != SCHEMA_VERSION:
        raise GraphError("Unsupported evidence schema_version")
    evidence_id = require_string(evidence["evidence_id"], "evidence.evidence_id", 4, 104)
    if not EVIDENCE_ID_RE.fullmatch(evidence_id):
        raise GraphError(f"Invalid evidence_id: {evidence_id}")
    if evidence["node_id"] != node["node_id"]:
        raise GraphError("Evidence node_id does not match the target node")
    verdict = evidence["verdict"]
    if verdict not in {"accepted", "rejected", "blocked"}:
        raise GraphError(f"Unsupported evidence verdict: {verdict}")
    require_string(evidence["summary"], "evidence.summary", 4, 4000)

    workspace_root = Path(require_string(evidence["workspace_root"], "evidence.workspace_root", 1, 1024))
    if not workspace_root.is_absolute() or not workspace_root.is_dir():
        raise GraphError(f"Evidence workspace_root is not an existing absolute directory: {workspace_root}")
    workspace_root = workspace_root.resolve()
    if workspace_root != Path(expected_workspace_root).resolve():
        raise GraphError(
            "Evidence workspace_root does not match the workspace bound at node start"
        )

    changed_paths = evidence["changed_paths"]
    if not isinstance(changed_paths, list) or len(changed_paths) > 2048:
        raise GraphError("evidence.changed_paths must be an array with at most 2048 entries")
    normalized_changes = [
        normalize_scope(path, "evidence.changed_paths", allow_directory=False) for path in changed_paths
    ]
    if normalized_changes != changed_paths or len(normalized_changes) != len(set(normalized_changes)):
        raise GraphError("evidence.changed_paths must be normalized and unique")
    if not node["write_scope"] and changed_paths:
        raise GraphError(f"Read-only node {node['node_id']} reports changed paths")
    for changed_path in changed_paths:
        if not any(scope_contains(scope, changed_path) for scope in node["write_scope"]):
            raise GraphError(
                f"Changed path is outside {node['node_id']} write scope: {changed_path}"
            )
    if node["write_scope"] and verdict == "accepted" and not changed_paths:
        raise GraphError(f"Accepted mutating node {node['node_id']} needs changed_paths evidence")

    artifacts = evidence["artifacts"]
    if not isinstance(artifacts, list) or len(artifacts) > 512:
        raise GraphError("evidence.artifacts must be an array with at most 512 entries")
    artifact_paths: set[str] = set()
    for index, artifact in enumerate(artifacts):
        label = f"evidence.artifacts[{index}]"
        if not isinstance(artifact, dict):
            raise GraphError(f"{label} must be an object")
        require_exact_keys(artifact, {"path", "sha256", "size_bytes"}, set(), label)
        relative = normalize_scope(artifact["path"], f"{label}.path", allow_directory=False)
        if relative in artifact_paths:
            raise GraphError(f"Duplicate artifact path: {relative}")
        artifact_paths.add(relative)
        digest = artifact["sha256"]
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise GraphError(f"Invalid {label}.sha256")
        size = require_int(artifact["size_bytes"], f"{label}.size_bytes", 0, 2**63 - 1)
        artifact_path = (workspace_root / Path(relative)).resolve()
        try:
            artifact_path.relative_to(workspace_root)
        except ValueError as exc:
            raise GraphError(f"Artifact escapes workspace_root: {relative}") from exc
        if not artifact_path.is_file():
            raise GraphError(f"Artifact file does not exist: {artifact_path}")
        if artifact_path.stat().st_size != size or sha256_file(artifact_path) != digest:
            raise GraphError(f"Artifact hash or size mismatch: {artifact_path}")

    criteria_results = evidence["criteria_results"]
    if not isinstance(criteria_results, list) or len(criteria_results) > 64:
        raise GraphError("evidence.criteria_results must be an array with at most 64 entries")
    result_map: dict[str, str] = {}
    for index, result in enumerate(criteria_results):
        label = f"evidence.criteria_results[{index}]"
        if not isinstance(result, dict):
            raise GraphError(f"{label} must be an object")
        require_exact_keys(result, {"criterion_id", "status", "evidence_ref"}, set(), label)
        criterion_id = result["criterion_id"]
        if not isinstance(criterion_id, str) or not CRITERION_ID_RE.fullmatch(criterion_id):
            raise GraphError(f"Invalid {label}.criterion_id")
        if criterion_id in result_map:
            raise GraphError(f"Duplicate criterion result: {criterion_id}")
        if result["status"] not in {"PASS", "FAIL", "BLOCKED"}:
            raise GraphError(f"Unsupported {label}.status")
        require_string(result["evidence_ref"], f"{label}.evidence_ref", 1, 1024)
        result_map[criterion_id] = result["status"]
    expected_criteria = {
        criterion["criterion_id"] for criterion in node["acceptance_criteria"]
    }
    if set(result_map) != expected_criteria:
        missing = sorted(expected_criteria - set(result_map))
        extra = sorted(set(result_map) - expected_criteria)
        raise GraphError(f"Criterion result mismatch; missing={missing}, extra={extra}")
    if verdict == "accepted" and any(status != "PASS" for status in result_map.values()):
        raise GraphError("Accepted evidence requires PASS for every criterion")
    if verdict != "accepted" and all(status == "PASS" for status in result_map.values()):
        raise GraphError("Rejected/blocked evidence must identify a failing or blocked criterion")

    checks = evidence["checks"]
    if not isinstance(checks, list) or len(checks) > 256:
        raise GraphError("evidence.checks must be an array with at most 256 entries")
    for index, check in enumerate(checks):
        label = f"evidence.checks[{index}]"
        if not isinstance(check, dict):
            raise GraphError(f"{label} must be an object")
        require_exact_keys(check, {"name", "status", "evidence_ref"}, set(), label)
        require_string(check["name"], f"{label}.name", 1, 200)
        if check["status"] not in {"PASS", "FAIL", "BLOCKED", "NOT_APPLICABLE"}:
            raise GraphError(f"Unsupported {label}.status")
        require_string(check["evidence_ref"], f"{label}.evidence_ref", 1, 1024)

    external_refs = evidence["external_refs"]
    if not isinstance(external_refs, list) or len(external_refs) > 64:
        raise GraphError("evidence.external_refs must be an array with at most 64 entries")
    external_pairs: set[tuple[str, str]] = set()
    for index, reference in enumerate(external_refs):
        label = f"evidence.external_refs[{index}]"
        if not isinstance(reference, dict):
            raise GraphError(f"{label} must be an object")
        require_exact_keys(reference, {"kind", "id"}, set(), label)
        if reference["kind"] not in EXTERNAL_REF_KINDS:
            raise GraphError(f"Unsupported {label}.kind")
        reference_id = require_string(reference["id"], f"{label}.id", 3, 256)
        pair = (reference["kind"], reference_id)
        if pair in external_pairs:
            raise GraphError(f"Duplicate external reference: {pair}")
        external_pairs.add(pair)
    expected_ref = EXPECTED_EXTERNAL_REF.get(node["executor"])
    if expected_ref and not any(kind == expected_ref for kind, _ in external_pairs):
        raise GraphError(f"{node['executor']} evidence requires external ref kind {expected_ref}")
    if node["capability"] == "final-acceptance" and not any(
        kind == "parent_acceptance" for kind, _ in external_pairs
    ):
        raise GraphError("Final acceptance evidence requires parent_acceptance reference")

    findings = evidence["findings"]
    if not isinstance(findings, list) or len(findings) > 256:
        raise GraphError("evidence.findings must be an array with at most 256 entries")
    for index, finding in enumerate(findings):
        label = f"evidence.findings[{index}]"
        if not isinstance(finding, dict):
            raise GraphError(f"{label} must be an object")
        require_exact_keys(finding, {"severity", "code", "summary", "evidence_ref"}, set(), label)
        if finding["severity"] not in {"critical", "high", "medium", "low", "info"}:
            raise GraphError(f"Unsupported {label}.severity")
        require_string(finding["code"], f"{label}.code", 2, 100)
        require_string(finding["summary"], f"{label}.summary", 4, 1000)
        require_string(finding["evidence_ref"], f"{label}.evidence_ref", 1, 1024)

    telemetry = evidence["telemetry"]
    if not isinstance(telemetry, dict):
        raise GraphError("evidence.telemetry must be an object")
    telemetry_keys = {
        "model",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "tool_calls",
        "cost_usd",
        "quota_before",
        "quota_after",
        "duration_seconds",
    }
    require_exact_keys(telemetry, telemetry_keys, set(), "evidence.telemetry")
    if telemetry["model"] is not None:
        require_string(telemetry["model"], "evidence.telemetry.model", 1, 256)
    for field in ("input_tokens", "output_tokens", "total_tokens", "tool_calls"):
        require_nullable_int(telemetry[field], f"evidence.telemetry.{field}", 0, 10**12)
    for field in ("cost_usd", "duration_seconds"):
        number = telemetry[field]
        if number is not None and (isinstance(number, bool) or not isinstance(number, (int, float)) or number < 0):
            raise GraphError(f"evidence.telemetry.{field} must be non-negative or null")
    for field in ("quota_before", "quota_after"):
        if telemetry[field] is not None and not isinstance(telemetry[field], (str, int, float)):
            raise GraphError(f"evidence.telemetry.{field} has unsupported type")

    return {
        "evidence_id": evidence_id,
        "verdict": verdict,
        "workspace_root": str(workspace_root),
        "evidence_hash": sha256_value(evidence),
    }


def validate_amendment(amendment: dict[str, Any]) -> None:
    require_exact_keys(
        amendment,
        {
            "schema_version",
            "amendment_id",
            "base_revision",
            "reason",
            "triggered_by_node",
            "invalidate_nodes",
            "add_nodes",
            "new_integration_node_id",
            "approval_scope",
        },
        set(),
        "amendment",
    )
    if amendment["schema_version"] != SCHEMA_VERSION:
        raise GraphError("Unsupported amendment schema_version")
    amendment_id = require_string(amendment["amendment_id"], "amendment.amendment_id", 4, 104)
    if not AMENDMENT_ID_RE.fullmatch(amendment_id):
        raise GraphError(f"Invalid amendment_id: {amendment_id}")
    require_int(amendment["base_revision"], "amendment.base_revision", 1, 10_000)
    require_string(amendment["reason"], "amendment.reason", 12, 4000)
    if not isinstance(amendment["triggered_by_node"], str) or not NODE_ID_RE.fullmatch(
        amendment["triggered_by_node"]
    ):
        raise GraphError("Invalid amendment.triggered_by_node")
    invalidate = amendment["invalidate_nodes"]
    if not isinstance(invalidate, list) or not (1 <= len(invalidate) <= 64) or len(invalidate) != len(set(invalidate)):
        raise GraphError("amendment.invalidate_nodes must contain 1..64 unique node IDs")
    for node_id in invalidate:
        if not isinstance(node_id, str) or not NODE_ID_RE.fullmatch(node_id):
            raise GraphError(f"Invalid amendment invalidate node ID: {node_id}")
    additions = amendment["add_nodes"]
    if not isinstance(additions, list) or not (1 <= len(additions) <= 64):
        raise GraphError("amendment.add_nodes must contain 1..64 nodes")
    for index, node in enumerate(additions):
        validate_node(node, index)
        if not node["active"]:
            raise GraphError("Every appended amendment node must be active")
    integration_id = amendment["new_integration_node_id"]
    if integration_id is not None and (
        not isinstance(integration_id, str) or not NODE_ID_RE.fullmatch(integration_id)
    ):
        raise GraphError("Invalid amendment.new_integration_node_id")
    if amendment["approval_scope"] not in {
        "technical-parent",
        "human-business",
        "human-sensitive",
    }:
        raise GraphError("Unsupported amendment.approval_scope")


@contextmanager
def run_lock(run_dir: Path) -> Iterator[None]:
    run_dir.mkdir(parents=True, exist_ok=True)
    lock_path = run_dir / ".state.lock"
    with lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            except OSError as exc:
                raise GraphError(f"Run state lock is unavailable: {lock_path}") from exc
            try:
                yield
            finally:
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError as exc:
                    raise GraphError(f"Run state lock could not be released: {lock_path}") from exc
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def resolve_state_root(value: str | None) -> Path:
    return Path(value).expanduser().resolve() if value else DEFAULT_STATE_ROOT.resolve()


def resolve_run_dir(state_root: Path, run_id: str) -> Path:
    if not RUN_ID_RE.fullmatch(run_id):
        raise GraphError(f"Invalid run_id: {run_id}")
    run_dir = (state_root / run_id).resolve()
    try:
        run_dir.relative_to(state_root.resolve())
    except ValueError as exc:
        raise GraphError("Run path escapes state root") from exc
    if not run_dir.is_dir():
        raise GraphError(f"Unknown run_id: {run_id}")
    return run_dir


def load_state(run_dir: Path) -> dict[str, Any]:
    return load_json(run_dir / "state.json", "run state")


def legacy_plan_view(plan: dict[str, Any]) -> dict[str, Any]:
    """Add post-0.2 governance fields only to an already stored legacy plan view."""
    compatible = copy.deepcopy(plan)
    nodes = compatible.get("nodes")
    if not isinstance(nodes, list):
        return compatible
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node.setdefault("reviewer", "legacy-independent-reviewer")
        node.setdefault("resource_locks", [])
        node.setdefault(
            "loop_policy",
            {
                "enabled": False,
                "max_rounds": 0,
                "stop_condition": "Historical run retains its original repair policy.",
            },
        )
        node.setdefault(
            "council_trigger",
            {
                "required": False,
                "reason": "Historical run predates explicit council triggers.",
            },
        )
    return compatible


def load_current_plan(run_dir: Path, state: dict[str, Any]) -> dict[str, Any]:
    revision = state["revision"]
    plan = load_json(run_dir / "plans" / f"revision-{revision}.json", "current graph plan")
    if sha256_value(plan) != state["plan_hash"]:
        raise GraphError("Current plan hash does not match run state")
    linkage = state.get("planning_contract", {})
    if linkage.get("mode", "legacy-pre-0.2") == "approved-contract":
        validate_graph(plan)
        return plan
    compatible = legacy_plan_view(plan)
    validate_graph(compatible)
    return compatible


def event(action: str, **fields: Any) -> dict[str, Any]:
    return {"at": utc_now(), "action": action, **fields}


def run_mutation(
    run_dir: Path,
    operation_id: str,
    action: str,
    payload: dict[str, Any],
    callback: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    if not OPERATION_ID_RE.fullmatch(operation_id):
        raise GraphError(f"Invalid operation_id: {operation_id}")
    payload_hash = sha256_value({"action": action, "payload": payload})
    with run_lock(run_dir):
        state = load_state(run_dir)
        existing = state["operations"].get(operation_id)
        if existing:
            if existing["payload_hash"] != payload_hash:
                raise GraphError(f"operation_id reused with another payload: {operation_id}")
            return existing["result"]
        result = callback(state)
        state["updated_at"] = utc_now()
        state["operations"][operation_id] = {
            "action": action,
            "payload_hash": payload_hash,
            "recorded_at": state["updated_at"],
            "result": result,
        }
        state["events"].append(event(action, operation_id=operation_id, result=result))
        atomic_write_json(run_dir / "state.json", state)
        return result


def command_validate(args: argparse.Namespace) -> dict[str, Any]:
    graph = load_json(Path(args.graph), "graph")
    return {"status": "VALID", **validate_graph(graph)}


def load_planner_module() -> Any:
    module_path = (
        SKILL_ROOT.parent
        / "plan-delivery"
        / "scripts"
        / "plan_contract.py"
    ).resolve()
    if not module_path.is_file() or module_path.is_symlink():
        raise GraphError(
            "Planning contract validator is unavailable; install plan-delivery"
        )
    before_hash = sha256_file(module_path)
    if before_hash != PLANNER_VALIDATOR_SHA256:
        raise GraphError(
            "Planning contract validator fingerprint differs from the Agent Graph pin"
        )
    spec = importlib.util.spec_from_file_location(
        "plan_delivery_plan_contract", module_path
    )
    if spec is None or spec.loader is None:
        raise GraphError(f"Cannot load planning contract validator: {module_path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        if sha256_file(module_path) != before_hash:
            raise GraphError("Planning contract validator changed while loading")
        return module
    except GraphError:
        raise
    except Exception as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise GraphError(f"Cannot load planning contract validator: {exc}") from exc


def load_acceptance_module() -> Any:
    module_path = (
        SKILL_ROOT.parent
        / "accept-system"
        / "scripts"
        / "acceptance.py"
    ).resolve()
    if not module_path.is_file() or module_path.is_symlink():
        raise GraphError("Acceptance validator is unavailable; install accept-system")
    before_hash = sha256_file(module_path)
    if before_hash != ACCEPTANCE_VALIDATOR_SHA256:
        raise GraphError(
            "Acceptance validator fingerprint differs from the Agent Graph pin"
        )
    spec = importlib.util.spec_from_file_location("accept_system_acceptance", module_path)
    if spec is None or spec.loader is None:
        raise GraphError(f"Cannot load acceptance validator: {module_path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        if sha256_file(module_path) != before_hash:
            raise GraphError("Acceptance validator changed while loading")
        return module
    except GraphError:
        raise
    except Exception as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise GraphError(f"Cannot load acceptance validator: {exc}") from exc


def graph_requires_acceptance(graph: dict[str, Any]) -> bool:
    return any(
        node.get("active", True) and node.get("capability") == "final-acceptance"
        for node in graph.get("nodes", [])
    )


def validate_acceptance_contract(
    acceptance_path: Path,
    planning_contract: dict[str, Any],
    graph: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    module = load_acceptance_module()
    try:
        acceptance_contract = module.load_json(
            acceptance_path, "acceptance contract"
        )
        summary = module.validate_contract_bindings(
            acceptance_contract, planning_contract, graph
        )
    except Exception as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise GraphError(f"Acceptance contract validation failed: {exc}") from exc
    linkage = {
        "mode": "accept-system-0.1",
        "acceptance_id": acceptance_contract["acceptance_id"],
        "contract_hash": summary["acceptance_contract_hash"],
        "planning_contract_hash": summary["planning_contract_hash"],
        "graph_hash": summary["graph_hash"],
        "source_path": str(acceptance_path),
        "snapshot_path": f"contracts/acceptance-revision-{planning_contract['revision']}.json",
        "final_receipt_hash": None,
        "final_receipt_snapshot_path": None,
    }
    return acceptance_contract, linkage


def load_linked_acceptance_contract(
    run_dir: Path,
    state: dict[str, Any],
    current_plan: dict[str, Any],
    planning_contract: dict[str, Any],
) -> tuple[Any, dict[str, Any]]:
    linkage = state.get("acceptance_contract", {})
    if linkage.get("mode") != "accept-system-0.1":
        raise GraphError("Run is not bound to an accept-system contract")
    snapshot_value = linkage.get("snapshot_path")
    if not isinstance(snapshot_value, str) or not snapshot_value:
        raise GraphError("Acceptance contract linkage is missing snapshot_path")
    snapshot_relative = Path(snapshot_value)
    if snapshot_relative.is_absolute():
        raise GraphError("Acceptance contract snapshot_path must be relative")
    run_root = run_dir.resolve()
    snapshot_path = (run_root / snapshot_relative).resolve()
    try:
        snapshot_path.relative_to(run_root)
    except ValueError as exc:
        raise GraphError("Acceptance contract snapshot_path escapes run directory") from exc
    if not snapshot_path.is_file() or snapshot_path.is_symlink():
        raise GraphError("Acceptance contract snapshot is missing or unsafe")
    module = load_acceptance_module()
    try:
        contract = load_json(snapshot_path, "current acceptance contract")
        summary = module.validate_contract_bindings(
            contract, planning_contract, current_plan
        )
    except Exception as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise GraphError(f"Stored acceptance contract validation failed: {exc}") from exc
    expected = {
        "acceptance_id": contract["acceptance_id"],
        "contract_hash": summary["acceptance_contract_hash"],
        "planning_contract_hash": summary["planning_contract_hash"],
        "graph_hash": summary["graph_hash"],
    }
    mismatches = sorted(
        key for key, value in expected.items() if linkage.get(key) != value
    )
    if mismatches:
        raise GraphError(
            "Acceptance contract snapshot/linkage mismatch: "
            + ", ".join(mismatches)
        )
    return module, contract


def validate_planning_contract(
    contract_path: Path, graph: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    module = load_planner_module()
    try:
        contract = module.load_json(contract_path, "planning contract")
        module.validate_approved_contract(contract)
        module.verify_bound_files(contract_path, contract)
        expected_graph = module.compile_graph_value(contract)
    except Exception as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise GraphError(f"Planning contract validation failed: {exc}") from exc
    if graph != expected_graph:
        raise GraphError(
            "Graph is not the deterministic projection of the approved planning contract"
        )
    linkage = {
        "mode": "approved-contract",
        "contract_id": contract["contract_id"],
        "revision": contract["revision"],
        "contract_hash": contract["approval"]["contract_hash"],
        "prompt_sha256": contract["prompt"]["sha256"],
        "projection_sha256": contract["graph"]["projection_sha256"],
        "source_path": str(contract_path),
        "snapshot_path": f"contracts/revision-{contract['revision']}.json",
    }
    return contract, linkage


def load_linked_planning_contract(
    run_dir: Path,
    state: dict[str, Any],
    current_plan: dict[str, Any],
) -> tuple[Any, dict[str, Any]]:
    linkage = state.get("planning_contract", {})
    if linkage.get("mode") != "approved-contract":
        raise GraphError("Run is not bound to an approved planning contract")
    snapshot_value = linkage.get("snapshot_path")
    if not isinstance(snapshot_value, str) or not snapshot_value:
        raise GraphError("Planning contract linkage is missing snapshot_path")
    snapshot_relative = Path(snapshot_value)
    if snapshot_relative.is_absolute():
        raise GraphError("Planning contract snapshot_path must be relative")
    run_root = run_dir.resolve()
    snapshot_path = (run_root / snapshot_relative).resolve()
    try:
        snapshot_path.relative_to(run_root)
    except ValueError as exc:
        raise GraphError("Planning contract snapshot_path escapes the run directory") from exc
    if not snapshot_path.is_file() or snapshot_path.is_symlink():
        raise GraphError("Planning contract snapshot is missing or unsafe")
    module = load_planner_module()
    try:
        contract = load_json(snapshot_path, "current planning contract")
        module.validate_approved_contract(contract)
        expected_linkage = {
            "contract_id": contract["contract_id"],
            "revision": contract["revision"],
            "contract_hash": contract["approval"]["contract_hash"],
            "prompt_sha256": contract["prompt"]["sha256"],
            "projection_sha256": contract["graph"]["projection_sha256"],
        }
        mismatches = sorted(
            key
            for key, expected in expected_linkage.items()
            if linkage.get(key) != expected
        )
        if state.get("revision") != contract["revision"]:
            mismatches.append("state.revision")
        if current_plan != module.compile_graph_value(contract):
            mismatches.append("current_plan")
        if mismatches:
            raise GraphError(
                "Planning contract snapshot/linkage mismatch: "
                + ", ".join(sorted(set(mismatches)))
            )
        return module, contract
    except GraphError:
        raise
    except Exception as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise GraphError(f"Stored planning contract validation failed: {exc}") from exc


def command_init(args: argparse.Namespace) -> dict[str, Any]:
    graph_path = Path(args.graph).resolve()
    graph = load_json(graph_path, "graph")
    summary = validate_graph(graph)
    planning_contract_arg = getattr(args, "planning_contract", None)
    if not planning_contract_arg:
        raise GraphError("New Agent Graph runs require --planning-contract")
    contract_path = Path(planning_contract_arg).resolve()
    contract, planning_linkage = validate_planning_contract(contract_path, graph)
    acceptance_contract_arg = getattr(args, "acceptance_contract", None)
    acceptance_contract = None
    if graph_requires_acceptance(graph):
        if not acceptance_contract_arg:
            raise GraphError(
                "Graph with final-acceptance requires --acceptance-contract"
            )
        acceptance_path = Path(acceptance_contract_arg).resolve()
        acceptance_contract, acceptance_linkage = validate_acceptance_contract(
            acceptance_path, contract, graph
        )
    elif acceptance_contract_arg:
        raise GraphError(
            "Graph without final-acceptance does not accept --acceptance-contract"
        )
    else:
        acceptance_linkage = {
            "mode": "not-required",
            "reason": "graph has no active final-acceptance node",
        }
    project_root = Path(graph["project_root"]).resolve()
    if not project_root.is_dir():
        raise GraphError(f"Graph project_root does not exist: {project_root}")
    state_root = resolve_state_root(args.state_root)
    state_root.mkdir(parents=True, exist_ok=True)
    run_id = "run-graph-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:10]
    run_dir = state_root / run_id
    staging_dir = state_root / f".init-{run_id}-{uuid.uuid4().hex}"
    staging_dir.mkdir(parents=False, exist_ok=False)
    now = utc_now()
    node_states = {
        node["node_id"]: {
            "status": "pending" if node["active"] else "inactive",
            "attempts": 0,
            "started_at": None,
            "completed_at": None,
            "workspace_root": None,
            "external_refs": [],
            "evidence": [],
            "inactive_reason": None if node["active"] else "plan-inactive",
        }
        for node in graph["nodes"]
    }
    state = {
        "schema_version": SCHEMA_VERSION,
        "runtime_version": VERSION,
        "run_id": run_id,
        "graph_id": graph["graph_id"],
        "revision": graph["revision"],
        "plan_hash": summary["graph_hash"],
        "planning_contract": planning_linkage,
        "acceptance_contract": acceptance_linkage,
        "status": "ACTIVE",
        "project_root": str(project_root),
        "created_at": now,
        "updated_at": now,
        "closed_at": None,
        "nodes": node_states,
        "amendments": [],
        "operations": {},
        "events": [
            event(
                "init",
                graph_hash=summary["graph_hash"],
                source=str(graph_path),
                planning_contract=planning_linkage,
                acceptance_contract=acceptance_linkage,
            )
        ],
        "telemetry": {
            "total_attempts": 0,
            "known_total_tokens": 0,
            "attested_total_tokens": 0,
            "estimated_unknown_tokens": 0,
            "unknown_token_results": 0,
            "known_cost_usd": 0.0,
            "unknown_cost_results": 0,
            "budget_exceeded": False,
        },
    }
    try:
        (staging_dir / "plans").mkdir()
        (staging_dir / "evidence").mkdir()
        (staging_dir / "contracts").mkdir()
        atomic_write_json(
            staging_dir / planning_linkage["snapshot_path"], contract
        )
        if acceptance_contract is not None:
            atomic_write_json(
                staging_dir / acceptance_linkage["snapshot_path"],
                acceptance_contract,
            )
        atomic_write_json(
            staging_dir / "plans" / f"revision-{graph['revision']}.json", graph
        )
        atomic_write_json(staging_dir / "state.json", state)
        os.replace(staging_dir, run_dir)
    except Exception:
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        raise
    return {
        "status": "INITIALIZED",
        "run_id": run_id,
        "run_dir": str(run_dir),
        "graph_hash": summary["graph_hash"],
        "planning_contract": planning_linkage,
        "acceptance_contract": acceptance_linkage,
        "ready": ready_nodes(graph, state),
    }


def ready_nodes(plan: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = {node["node_id"]: node for node in plan["nodes"]}
    ready: list[dict[str, Any]] = []
    for node_id, node in nodes.items():
        node_state = state["nodes"][node_id]
        if not node["active"] or node_state["status"] != "pending":
            continue
        if all(state["nodes"][dependency]["status"] == "accepted" for dependency in node["depends_on"]):
            ready.append(
                {
                    "node_id": node_id,
                    "kind": node["kind"],
                    "capability": node["capability"],
                    "executor": node["executor"],
                    "title": node["title"],
                }
            )
    return sorted(ready, key=lambda item: item["node_id"])


def command_ready(args: argparse.Namespace) -> dict[str, Any]:
    state_root = resolve_state_root(args.state_root)
    run_dir = resolve_run_dir(state_root, args.run_id)
    state = load_state(run_dir)
    plan = load_current_plan(run_dir, state)
    retries = []
    for node in plan["nodes"]:
        node_state = state["nodes"][node["node_id"]]
        if (
            node["active"]
            and node_state["status"] == "rejected"
            and node["repair_policy"]["on_reject"] == "retry-same-scope"
            and node_state["attempts"] < node["budget"]["max_attempts"]
            and node_state["attempts"] - 1 < node["repair_policy"]["max_same_scope_repairs"]
        ):
            retries.append(node["node_id"])
    return {
        "status": state["status"],
        "run_id": args.run_id,
        "revision": state["revision"],
        "ready": ready_nodes(plan, state),
        "retry_eligible": sorted(retries),
    }


def parse_external_ref(value: str | None) -> dict[str, str] | None:
    if value is None:
        return None
    if ":" not in value:
        raise GraphError("external-ref must have kind:id syntax")
    kind, reference_id = value.split(":", 1)
    if kind not in EXTERNAL_REF_KINDS or len(reference_id) < 3:
        raise GraphError(f"Invalid external-ref: {value}")
    return {"kind": kind, "id": reference_id}


def budget_elapsed_seconds(plan: dict[str, Any], state: dict[str, Any], now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    if plan["global_budget"].get("clock", "elapsed") == "elapsed":
        return max(0.0, (now - parse_utc(state["created_at"])).total_seconds())
    # Union of work intervals, not summed parallel worker time or human idle time.
    spans = []
    for interval in state["telemetry"].get("active_intervals", []):
        start = parse_utc(interval["start"])
        end = parse_utc(interval["end"]) if interval["end"] else now
        if end < start or start > now or end > now:
            raise GraphError("Invalid active budget interval")
        spans.append((start, end))
    total = 0.0
    last_end = None
    for start, end in sorted(spans):
        left = max(start, last_end) if last_end else start
        total += max(0.0, (end - left).total_seconds())
        last_end = max(last_end, end) if last_end else end
    return total


def token_policy(plan: dict[str, Any], node: dict[str, Any] | None = None) -> str:
    global_policy = plan["global_budget"].get("token_policy", "hard")
    return node["budget"].get("token_policy", global_policy) if node else global_policy


def time_policy(plan: dict[str, Any]) -> str:
    return plan["global_budget"].get("time_policy", "hard")


def tool_policy(node: dict[str, Any]) -> str:
    return node["budget"].get("tool_policy", "hard")


def command_start(args: argparse.Namespace) -> dict[str, Any]:
    state_root = resolve_state_root(args.state_root)
    run_dir = resolve_run_dir(state_root, args.run_id)
    external_ref = parse_external_ref(args.external_ref)
    workspace_root = Path(args.workspace_root).expanduser().resolve()
    if not workspace_root.is_dir():
        raise GraphError(f"Node workspace_root is not an existing directory: {workspace_root}")
    payload = {
        "node_id": args.node_id,
        "external_ref": external_ref,
        "workspace_root": str(workspace_root),
    }

    def mutate(state: dict[str, Any]) -> dict[str, Any]:
        if state["status"] != "ACTIVE":
            raise GraphError(f"Run is not active: {state['status']}")
        plan = load_current_plan(run_dir, state)
        nodes = {node["node_id"]: node for node in plan["nodes"]}
        if args.node_id not in nodes or not nodes[args.node_id]["active"]:
            raise GraphError(f"Unknown or inactive node: {args.node_id}")
        node = nodes[args.node_id]
        node_state = state["nodes"][args.node_id]
        expected_ref_kind = EXPECTED_EXTERNAL_REF.get(node["executor"])
        if external_ref and expected_ref_kind and external_ref["kind"] != expected_ref_kind:
            raise GraphError(
                f"{node['executor']} start requires external-ref kind {expected_ref_kind}"
            )
        if node_state["workspace_root"] not in {None, str(workspace_root)}:
            raise GraphError("A same-node retry cannot switch its bound workspace_root")
        current_status = node_state["status"]
        if current_status == "pending":
            ready_ids = {item["node_id"] for item in ready_nodes(plan, state)}
            if args.node_id not in ready_ids:
                raise GraphError(f"Node is not ready: {args.node_id}")
        elif current_status == "rejected":
            if node["repair_policy"]["on_reject"] != "retry-same-scope":
                raise GraphError(f"Rejected node requires graph amendment or block: {args.node_id}")
            if node_state["attempts"] - 1 >= node["repair_policy"]["max_same_scope_repairs"]:
                raise GraphError(f"Same-scope repair budget exhausted: {args.node_id}")
        elif current_status != "recovery-required":
            raise GraphError(f"Node cannot start from status {current_status}: {args.node_id}")
        if node_state["attempts"] >= node["budget"]["max_attempts"]:
            raise GraphError(f"Node attempt budget exhausted: {args.node_id}")
        global_budget = plan["global_budget"]
        if state["telemetry"]["total_attempts"] >= global_budget["max_total_attempts"]:
            raise GraphError("Graph total attempt budget exhausted")
        elapsed = budget_elapsed_seconds(plan, state)
        if elapsed > global_budget["max_wall_seconds"] and time_policy(plan) == "hard":
            raise GraphError("Graph wall-time budget exhausted")
        max_total_tokens = global_budget["max_total_tokens"]
        node_max_tokens = node["budget"]["max_tokens"]
        if max_total_tokens is not None and token_policy(plan) == "hard":
            known_total = state["telemetry"]["known_total_tokens"]
            if known_total >= max_total_tokens:
                raise GraphError("Graph hard token budget is already exhausted")
            if node_max_tokens is not None and known_total + node_max_tokens > max_total_tokens:
                raise GraphError("Starting node could exceed the graph hard token budget")
        attested_total = int(
            state["telemetry"].get(
                "attested_total_tokens",
                state["telemetry"].get("known_total_tokens", 0),
            )
        )
        if max_total_tokens is not None and token_policy(plan) == "hard":
            if attested_total >= max_total_tokens:
                raise GraphError("Graph attested token budget is already exhausted")
            if node_max_tokens is not None and attested_total + node_max_tokens > max_total_tokens:
                raise GraphError("Starting node could exceed the graph attested token budget")
            if "token_policy" in global_budget:
                reserved = sum(
                    nodes[key]["budget"]["max_tokens"] or 0
                    for key, value in state["nodes"].items()
                    if value["status"] in {"running", "recovery-required"}
                )
                if max(attested_total, known_total) + reserved + (node_max_tokens or 0) > max_total_tokens:
                    raise GraphError("Starting node could exceed the reserved global hard token budget")
        if global_budget.get("clock") == "active" and any(
            row["node_id"] == args.node_id and row["end"] is None
            for row in state["telemetry"].get("active_intervals", [])
        ):
            raise GraphError("Reconcile the prior live attempt before restarting an active-clock node")
        node_state["status"] = "running"
        node_state["attempts"] += 1
        node_state["started_at"] = utc_now()
        node_state["completed_at"] = None
        if global_budget.get("clock") == "active" and node["kind"] != "human":
            state["telemetry"].setdefault("active_intervals", []).append({
                "node_id": args.node_id, "attempt": node_state["attempts"],
                "start": node_state["started_at"], "end": None,
            })
        node_state["workspace_root"] = str(workspace_root)
        if external_ref and external_ref not in node_state["external_refs"]:
            node_state["external_refs"].append(external_ref)
        state["telemetry"]["total_attempts"] += 1
        return {
            "status": "STARTED",
            "run_id": args.run_id,
            "node_id": args.node_id,
            "attempt": node_state["attempts"],
            "token_policy": token_policy(plan, node),
            "time_policy": time_policy(plan),
            "tool_policy": tool_policy(node),
            "estimate_warning": token_policy(plan) == "estimate" and max_total_tokens is not None and attested_total + (node_max_tokens or 0) > max_total_tokens,
            "dispatch_bounds": {
                **(
                    {"max_model_tool_calls": node["budget"]["max_tool_calls"]}
                    if tool_policy(node) == "hard"
                    else {}
                ),
                "timeout": (
                    min(node["budget"]["max_wall_seconds"], max(0, int(global_budget["max_wall_seconds"] - elapsed)))
                    if time_policy(plan) == "hard"
                    else node["budget"]["max_wall_seconds"]
                ),
            },
        }

    return run_mutation(run_dir, args.operation_id, "start", payload, mutate)


def command_result(args: argparse.Namespace) -> dict[str, Any]:
    state_root = resolve_state_root(args.state_root)
    run_dir = resolve_run_dir(state_root, args.run_id)
    evidence_path = Path(args.evidence).resolve()
    evidence = load_json(evidence_path, "evidence")
    evidence_input_hash = sha256_value(evidence)
    payload = {
        "node_id": args.node_id,
        "evidence_input_hash": evidence_input_hash,
    }

    def mutate(state: dict[str, Any]) -> dict[str, Any]:
        if state["status"] != "ACTIVE":
            raise GraphError(f"Run is not active: {state['status']}")
        plan = load_current_plan(run_dir, state)
        nodes = {node["node_id"]: node for node in plan["nodes"]}
        if args.node_id not in nodes or not nodes[args.node_id]["active"]:
            raise GraphError(f"Unknown or inactive node: {args.node_id}")
        node = nodes[args.node_id]
        node_state = state["nodes"][args.node_id]
        if node_state["status"] not in {"running", "recovery-required"}:
            raise GraphError(f"Node cannot record result from status {node_state['status']}")
        if node_state["workspace_root"] is None:
            raise GraphError("Node result has no workspace_root bound at start")
        evidence_summary = validate_evidence(
            evidence, node, expected_workspace_root=node_state["workspace_root"]
        )
        provided_refs = {
            (reference["kind"], reference["id"]) for reference in evidence["external_refs"]
        }
        required_source_refs = {
            (reference["kind"], reference["id"])
            for source in node["evidence_from"]
            for reference in state["nodes"][source]["external_refs"]
            if reference["kind"] in {"fleet_run", "agent_loop"}
        }
        missing_source_refs = sorted(required_source_refs - provided_refs)
        if missing_source_refs:
            rendered = ", ".join(
                f"{kind}:{reference_id}" for kind, reference_id in missing_source_refs
            )
            raise GraphError("Gate evidence omits covered executor references: " + rendered)
        evidence_target = run_dir / "evidence" / f"{evidence_summary['evidence_id']}.json"
        if evidence_target.exists():
            existing = load_json(evidence_target, "stored evidence")
            if sha256_value(existing) != evidence_summary["evidence_hash"]:
                raise GraphError(f"Evidence ID already exists with another payload: {evidence_summary['evidence_id']}")
        else:
            atomic_write_json(evidence_target, evidence)
        node_state["status"] = evidence_summary["verdict"]
        node_state["completed_at"] = utc_now()
        for interval in state["telemetry"].get("active_intervals", []):
            if interval["node_id"] == args.node_id and interval["end"] is None:
                interval["end"] = node_state["completed_at"]
        node_state["evidence"].append(
            {
                "evidence_id": evidence_summary["evidence_id"],
                "evidence_hash": evidence_summary["evidence_hash"],
                "verdict": evidence_summary["verdict"],
                "path": str(evidence_target),
            }
        )
        for reference in evidence["external_refs"]:
            if reference not in node_state["external_refs"]:
                node_state["external_refs"].append(reference)
        telemetry = evidence["telemetry"]
        total_tokens = telemetry["total_tokens"]
        state["telemetry"].setdefault("attested_total_tokens", state["telemetry"]["known_total_tokens"])
        state["telemetry"].setdefault("estimated_unknown_tokens", 0)
        if total_tokens is None:
            state["telemetry"]["unknown_token_results"] += 1
        else:
            state["telemetry"]["known_total_tokens"] += total_tokens
            state["telemetry"]["attested_total_tokens"] += total_tokens
        cost = telemetry["cost_usd"]
        if cost is None:
            state["telemetry"]["unknown_cost_results"] += 1
        else:
            state["telemetry"]["known_cost_usd"] = round(
                state["telemetry"]["known_cost_usd"] + float(cost), 12
            )
        budget_exceeded = False
        charged_tokens = (
            total_tokens if total_tokens is not None else node["budget"]["max_tokens"]
        )
        warnings = []
        if charged_tokens is not None and node["budget"]["max_tokens"] is not None and charged_tokens > node["budget"]["max_tokens"]:
            if token_policy(plan, node) == "hard":
                budget_exceeded = True
            else:
                warnings.append("node-token-estimate-exceeded")
        tool_calls = telemetry["tool_calls"]
        if tool_calls is not None and tool_calls > node["budget"]["max_tool_calls"]:
            if tool_policy(node) == "hard":
                budget_exceeded = True
            else:
                warnings.append("node-tool-estimate-exceeded")
        duration = telemetry["duration_seconds"]
        if duration is not None and duration > node["budget"]["max_wall_seconds"]:
            budget_exceeded = True
        current_elapsed = budget_elapsed_seconds(plan, state)
        if current_elapsed > plan["global_budget"]["max_wall_seconds"]:
            if time_policy(plan) == "hard":
                budget_exceeded = True
            else:
                warnings.append("global-time-estimate-exceeded")
        max_total_tokens = plan["global_budget"]["max_total_tokens"]
        if (
            max_total_tokens is not None
            and max(state["telemetry"]["attested_total_tokens"], state["telemetry"]["known_total_tokens"]) > max_total_tokens
        ):
            if token_policy(plan) == "hard":
                budget_exceeded = True
            else:
                warnings.append("global-token-estimate-exceeded")
        if warnings:
            state["telemetry"].setdefault("budget_warnings", []).append({
                "node_id": args.node_id, "evidence_id": evidence_summary["evidence_id"],
                "warnings": warnings, "owner_approval_required": False,
            })
        if budget_exceeded:
            state["telemetry"]["budget_exceeded"] = True
        return {
            "status": "RECORDED",
            "run_id": args.run_id,
            "node_id": args.node_id,
            "verdict": evidence_summary["verdict"],
            "evidence_id": evidence_summary["evidence_id"],
            "evidence_hash": evidence_summary["evidence_hash"],
            "budget_exceeded": budget_exceeded,
            "estimate_warnings": warnings,
        }

    return run_mutation(run_dir, args.operation_id, "result", payload, mutate)


def command_contracted_amend(args: argparse.Namespace, run_dir: Path) -> dict[str, Any]:
    contract_path = Path(args.planning_contract_amendment).resolve()
    candidate = load_json(contract_path, "planning contract amendment")
    candidate_hash = sha256_value(candidate)
    acceptance_candidate_arg = getattr(args, "acceptance_contract_amendment", None)
    acceptance_candidate_path = (
        Path(acceptance_candidate_arg).resolve()
        if acceptance_candidate_arg
        else None
    )
    acceptance_candidate_hash = (
        sha256_file(acceptance_candidate_path)
        if acceptance_candidate_path
        else None
    )
    payload = {
        "planning_contract_candidate_hash": candidate_hash,
        "acceptance_contract_candidate_file_hash": acceptance_candidate_hash,
        "triggered_by_node": args.triggered_by_node,
    }

    def mutate(state: dict[str, Any]) -> dict[str, Any]:
        if state["status"] != "ACTIVE":
            raise GraphError(f"Run is not active: {state['status']}")
        linkage = state.get("planning_contract", {})
        if linkage.get("mode") != "approved-contract":
            raise GraphError("Contract amendment is available only for contracted runs")
        if not args.triggered_by_node:
            raise GraphError("Contract amendment requires --triggered-by-node")
        current_plan = load_current_plan(run_dir, state)
        module, current_contract = load_linked_planning_contract(
            run_dir, state, current_plan
        )
        try:
            module.validate_amendment(current_contract, candidate)
            module.validate_approved_contract(candidate)
            module.verify_bound_files(contract_path, candidate)
            new_plan = module.compile_graph_value(candidate)
        except Exception as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise GraphError(f"Planning contract amendment validation failed: {exc}") from exc
        if candidate["revision"] != state["revision"] + 1:
            raise GraphError("Planning contract amendment revision must increment run revision")
        if new_plan["previous_revision_hash"] != state["plan_hash"]:
            raise GraphError("Planning contract amendment does not bind the current graph hash")
        trigger = args.triggered_by_node
        current_nodes = {node["node_id"]: node for node in current_plan["nodes"]}
        if trigger not in current_nodes:
            raise GraphError(f"Unknown amendment trigger node: {trigger}")
        if state["nodes"][trigger]["status"] not in {"accepted", "rejected", "blocked"}:
            raise GraphError("Amendment trigger must have terminal evidence")
        new_summary = validate_graph(new_plan)
        current_acceptance_linkage = state.get("acceptance_contract", {})
        acceptance_candidate = None
        new_acceptance_linkage = current_acceptance_linkage
        if current_acceptance_linkage.get("mode") == "accept-system-0.1":
            if acceptance_candidate_path is None:
                raise GraphError(
                    "Contracted graph amendment requires --acceptance-contract-amendment"
                )
            acceptance_candidate, new_acceptance_linkage = validate_acceptance_contract(
                acceptance_candidate_path, candidate, new_plan
            )
            if (
                new_acceptance_linkage["acceptance_id"]
                != current_acceptance_linkage["acceptance_id"]
            ):
                raise GraphError(
                    "Acceptance contract amendment must preserve acceptance_id"
                )
        elif acceptance_candidate_path is not None:
            raise GraphError(
                "Legacy graph amendment does not accept an acceptance contract amendment"
            )
        new_nodes = {node["node_id"]: node for node in new_plan["nodes"]}
        removed_ids = sorted(
            node_id
            for node_id, node in current_nodes.items()
            if node["active"] and (
                node_id not in new_nodes or not new_nodes[node_id]["active"]
            )
        )
        added_ids = sorted(set(new_nodes) - set(current_nodes))
        for node_id in sorted(set(current_nodes) & set(new_nodes)):
            old_node = current_nodes[node_id]
            new_node = new_nodes[node_id]
            old_without_active = {key: value for key, value in old_node.items() if key != "active"}
            new_without_active = {key: value for key, value in new_node.items() if key != "active"}
            allowed_deactivation = (
                old_node["active"]
                and not new_node["active"]
                and old_without_active == new_without_active
            )
            old_without_criteria = {
                key: value
                for key, value in old_node.items()
                if key not in {"active", "acceptance_criteria"}
            }
            new_without_criteria = {
                key: value
                for key, value in new_node.items()
                if key not in {"active", "acceptance_criteria"}
            }
            old_criteria = old_node.get("acceptance_criteria", [])
            new_criteria = new_node.get("acceptance_criteria", [])
            allowed_append_only_criteria = (
                old_node.get("active") is True
                and new_node.get("active") is True
                and old_without_criteria == new_without_criteria
                and len(new_criteria) >= len(old_criteria)
                and new_criteria[: len(old_criteria)] == old_criteria
            )
            if (
                old_node != new_node
                and not allowed_deactivation
                and not allowed_append_only_criteria
            ):
                raise GraphError(
                    f"Contract amendment changes node in place; append a replacement instead: {node_id}"
                )
        unsafe_removed = [
            node_id
            for node_id in removed_ids
            if state["nodes"][node_id]["status"] in {"running", "recovery-required"}
        ]
        if unsafe_removed:
            raise GraphError(
                "Contract amendment cannot deactivate running/recovery nodes: "
                + ", ".join(unsafe_removed)
            )
        current_tasks = {
            task["task_id"]: task for task in current_contract["tasks"]
        }
        affected_node_ids = {
            current_tasks[task_id]["graph_node_id"]
            for task_id in candidate["amendment"]["affected_task_ids"]
            if task_id in current_tasks
        }
        unrelated_removed = sorted(set(removed_ids) - affected_node_ids)
        if unrelated_removed:
            raise GraphError(
                "Contract amendment deactivates nodes outside affected_task_ids: "
                + ", ".join(unrelated_removed)
            )
        contract_target = run_dir / "contracts" / f"revision-{candidate['revision']}.json"
        plan_target = run_dir / "plans" / f"revision-{candidate['revision']}.json"
        for target, value, label in (
            (contract_target, candidate, "contract"),
            (plan_target, new_plan, "plan"),
        ):
            if target.exists():
                if canonical_bytes(load_json(target, f"stored {label}")) != canonical_bytes(value):
                    raise GraphError(f"Revision {candidate['revision']} {label} already differs")
            else:
                atomic_write_json(target, value)
        if acceptance_candidate is not None:
            acceptance_target = run_dir / new_acceptance_linkage["snapshot_path"]
            if acceptance_target.exists():
                if canonical_bytes(
                    load_json(acceptance_target, "stored acceptance contract")
                ) != canonical_bytes(acceptance_candidate):
                    raise GraphError(
                        f"Revision {candidate['revision']} acceptance contract already differs"
                    )
            else:
                atomic_write_json(acceptance_target, acceptance_candidate)
        for node_id in removed_ids:
            state["nodes"][node_id]["status"] = "inactive"
            state["nodes"][node_id]["inactive_reason"] = (
                f"planning-contract-revision-{candidate['revision']}"
            )
        for node_id in added_ids:
            state["nodes"][node_id] = {
                "status": "pending" if new_nodes[node_id]["active"] else "inactive",
                "attempts": 0,
                "started_at": None,
                "completed_at": None,
                "workspace_root": None,
                "external_refs": [],
                "evidence": [],
                "inactive_reason": None if new_nodes[node_id]["active"] else "plan-inactive",
            }
        new_linkage = {
            "mode": "approved-contract",
            "contract_id": candidate["contract_id"],
            "revision": candidate["revision"],
            "contract_hash": candidate["approval"]["contract_hash"],
            "prompt_sha256": candidate["prompt"]["sha256"],
            "projection_sha256": candidate["graph"]["projection_sha256"],
            "source_path": str(contract_path),
            "snapshot_path": f"contracts/revision-{candidate['revision']}.json",
        }
        state["revision"] = candidate["revision"]
        state["plan_hash"] = new_summary["graph_hash"]
        state["planning_contract"] = new_linkage
        state["acceptance_contract"] = new_acceptance_linkage
        state["amendments"].append(
            {
                "amendment_id": f"contract-revision-{candidate['revision']}",
                "amendment_hash": candidate_hash,
                "base_revision": current_contract["revision"],
                "new_revision": candidate["revision"],
                "triggered_by_node": trigger,
                "invalidated_nodes": removed_ids,
                "deactivated_nodes": removed_ids,
                "added_nodes": added_ids,
                "approval_scope": "planning-contract",
                "approval_ref": {
                    "kind": "parent_acceptance",
                    "id": candidate["approval"]["contract_hash"],
                },
                "recorded_at": utc_now(),
            }
        )
        return {
            "status": "AMENDED",
            "run_id": args.run_id,
            "revision": state["revision"],
            "plan_hash": state["plan_hash"],
            "contract_hash": candidate["approval"]["contract_hash"],
            "acceptance_contract_hash": new_acceptance_linkage.get(
                "contract_hash"
            ),
            "deactivated_nodes": removed_ids,
            "added_nodes": added_ids,
        }

    return run_mutation(run_dir, args.operation_id, "contract-amend", payload, mutate)


def command_amend(args: argparse.Namespace) -> dict[str, Any]:
    state_root = resolve_state_root(args.state_root)
    run_dir = resolve_run_dir(state_root, args.run_id)
    state = load_state(run_dir)
    mode = state.get("planning_contract", {}).get("mode", "legacy-pre-0.2")
    if mode == "approved-contract":
        if not getattr(args, "planning_contract_amendment", None):
            raise GraphError(
                "Contracted runs require --planning-contract-amendment"
            )
        return command_contracted_amend(args, run_dir)
    if not getattr(args, "amendment", None):
        raise GraphError(
            "Historical uncontracted run recovery requires --legacy-amendment"
        )
    amendment_path = Path(args.amendment).resolve()
    amendment = load_json(amendment_path, "amendment")
    validate_amendment(amendment)
    approval_ref = parse_external_ref(args.approval_ref)
    if amendment["approval_scope"] != "technical-parent":
        if approval_ref is None or approval_ref["kind"] != "human_approval":
            raise GraphError("Human-scoped amendment requires --approval-ref human_approval:<id>")
    payload = {
        "amendment_hash": sha256_value(amendment),
        "approval_ref": approval_ref,
    }

    def mutate(state: dict[str, Any]) -> dict[str, Any]:
        if state["status"] != "ACTIVE":
            raise GraphError(f"Run is not active: {state['status']}")
        plan = load_current_plan(run_dir, state)
        if amendment["base_revision"] != state["revision"]:
            raise GraphError(
                f"Amendment base revision {amendment['base_revision']} != current {state['revision']}"
            )
        if len(state["amendments"]) >= plan["global_budget"]["max_amendments"]:
            raise GraphError("Graph amendment budget exhausted")
        nodes = {node["node_id"]: node for node in plan["nodes"]}
        trigger = amendment["triggered_by_node"]
        if trigger not in nodes:
            raise GraphError(f"Unknown amendment trigger node: {trigger}")
        if state["nodes"][trigger]["status"] not in {"accepted", "rejected", "blocked"}:
            raise GraphError("Amendment trigger must have terminal evidence")
        invalidate_ids = set(amendment["invalidate_nodes"])
        unknown = sorted(invalidate_ids - set(nodes))
        if unknown:
            raise GraphError(f"Amendment invalidates unknown nodes: {', '.join(unknown)}")
        inactive_requested = sorted(node_id for node_id in invalidate_ids if not nodes[node_id]["active"])
        if inactive_requested:
            raise GraphError(f"Amendment tries to invalidate inactive nodes: {', '.join(inactive_requested)}")
        trigger_provenance = provenance_ancestors(trigger, nodes) | {trigger}
        unrelated_invalidations = sorted(invalidate_ids - trigger_provenance)
        if unrelated_invalidations:
            raise GraphError(
                "Amendment invalidation must target its trigger or upstream provenance: "
                + ", ".join(unrelated_invalidations)
            )
        amendment_counts = {
            node_id: sum(node_id in record["invalidated_nodes"] for record in state["amendments"])
            for node_id in invalidate_ids
        }
        exhausted = [
            node_id
            for node_id, count in amendment_counts.items()
            if count >= nodes[node_id]["repair_policy"]["max_amendments"]
        ]
        if exhausted:
            raise GraphError(f"Node amendment budget exhausted: {', '.join(sorted(exhausted))}")
        deactivate_ids = transitive_descendants(invalidate_ids, nodes, include_evidence=True)
        new_plan = copy.deepcopy(plan)
        new_plan["revision"] = state["revision"] + 1
        new_plan["previous_revision_hash"] = state["plan_hash"]
        for node in new_plan["nodes"]:
            if node["node_id"] in deactivate_ids:
                node["active"] = False
        existing_ids = {node["node_id"] for node in new_plan["nodes"]}
        added_ids: list[str] = []
        for node in amendment["add_nodes"]:
            if node["node_id"] in existing_ids:
                raise GraphError(f"Amendment node ID already exists: {node['node_id']}")
            existing_ids.add(node["node_id"])
            added_ids.append(node["node_id"])
            new_plan["nodes"].append(copy.deepcopy(node))
        if amendment["new_integration_node_id"] is not None:
            new_plan["integration_node_id"] = amendment["new_integration_node_id"]
        new_summary = validate_graph(new_plan)
        new_revision = new_plan["revision"]
        plan_target = run_dir / "plans" / f"revision-{new_revision}.json"
        if plan_target.exists():
            raise GraphError(f"Plan revision already exists: {plan_target}")
        atomic_write_json(plan_target, new_plan)
        for node_id in deactivate_ids:
            state["nodes"][node_id]["status"] = "inactive"
            state["nodes"][node_id]["inactive_reason"] = amendment["amendment_id"]
        for node in amendment["add_nodes"]:
            state["nodes"][node["node_id"]] = {
                "status": "pending",
                "attempts": 0,
                "started_at": None,
                "completed_at": None,
                "workspace_root": None,
                "external_refs": [],
                "evidence": [],
                "inactive_reason": None,
            }
        state["revision"] = new_revision
        state["plan_hash"] = new_summary["graph_hash"]
        amendment_record = {
            "amendment_id": amendment["amendment_id"],
            "amendment_hash": sha256_value(amendment),
            "base_revision": amendment["base_revision"],
            "new_revision": new_revision,
            "triggered_by_node": trigger,
            "invalidated_nodes": sorted(invalidate_ids),
            "deactivated_nodes": sorted(deactivate_ids),
            "added_nodes": added_ids,
            "approval_scope": amendment["approval_scope"],
            "approval_ref": approval_ref,
            "recorded_at": utc_now(),
        }
        state["amendments"].append(amendment_record)
        return {
            "status": "AMENDED",
            "run_id": args.run_id,
            "revision": new_revision,
            "plan_hash": new_summary["graph_hash"],
            "deactivated_nodes": sorted(deactivate_ids),
            "added_nodes": added_ids,
        }

    return run_mutation(run_dir, args.operation_id, "amend", payload, mutate)


def command_recover(args: argparse.Namespace) -> dict[str, Any]:
    state_root = resolve_state_root(args.state_root)
    run_dir = resolve_run_dir(state_root, args.run_id)
    stale_after = require_int(args.stale_after_seconds, "stale_after_seconds", 30, 2_592_000)
    payload = {"stale_after_seconds": stale_after}

    def mutate(state: dict[str, Any]) -> dict[str, Any]:
        if state["status"] != "ACTIVE":
            raise GraphError(f"Run is not active: {state['status']}")
        plan = load_current_plan(run_dir, state)
        nodes = {node["node_id"]: node for node in plan["nodes"]}
        now = datetime.now(timezone.utc)
        marked: list[str] = []
        expired_human: list[str] = []
        for node_id, node_state in state["nodes"].items():
            if node_state["status"] != "running" or not node_state["started_at"]:
                continue
            age = (now - parse_utc(node_state["started_at"])).total_seconds()
            node = nodes[node_id]
            expiry = node.get("expires_after_seconds")
            if node["kind"] == "human" and expiry is not None and age >= expiry:
                node_state["status"] = "blocked"
                node_state["completed_at"] = utc_now()
                expired_human.append(node_id)
            elif age >= stale_after:
                node_state["status"] = "recovery-required"
                marked.append(node_id)
        return {
            "status": "RECOVERY_REVIEW_REQUIRED" if marked or expired_human else "CURRENT",
            "run_id": args.run_id,
            "recovery_required": sorted(marked),
            "expired_human_gates": sorted(expired_human),
        }

    return run_mutation(run_dir, args.operation_id, "recover", payload, mutate)


def summarize_state(run_dir: Path, state: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for node_state in state["nodes"].values():
        counts[node_state["status"]] = counts.get(node_state["status"], 0) + 1
    return {
        "status": state["status"],
        "run_id": state["run_id"],
        "graph_id": state["graph_id"],
        "revision": state["revision"],
        "plan_hash": state["plan_hash"],
        "planning_contract": state.get(
            "planning_contract",
            {
                "mode": "legacy-pre-0.2",
                "reason": "state predates planning-contract linkage",
            },
        ),
        "acceptance_contract": state.get(
            "acceptance_contract",
            {
                "mode": "legacy-pre-0.3",
                "reason": "state predates accept-system linkage",
            },
        ),
        "run_dir": str(run_dir),
        "node_status_counts": counts,
        "ready": ready_nodes(plan, state),
        "amendment_count": len(state["amendments"]),
        "telemetry": state["telemetry"],
        "created_at": state["created_at"],
        "updated_at": state["updated_at"],
        "closed_at": state["closed_at"],
    }


def command_status(args: argparse.Namespace) -> dict[str, Any]:
    state_root = resolve_state_root(args.state_root)
    run_dir = resolve_run_dir(state_root, args.run_id)
    state = load_state(run_dir)
    plan = load_current_plan(run_dir, state)
    return summarize_state(run_dir, state, plan)


def command_close(args: argparse.Namespace) -> dict[str, Any]:
    state_root = resolve_state_root(args.state_root)
    run_dir = resolve_run_dir(state_root, args.run_id)
    receipt_arg = getattr(args, "planning_closeout_receipt", None)
    receipt_path = Path(receipt_arg).resolve() if receipt_arg else None
    receipt_input_hash = sha256_file(receipt_path) if receipt_path else None
    acceptance_receipt_arg = getattr(args, "acceptance_receipt", None)
    acceptance_receipt_path = (
        Path(acceptance_receipt_arg).resolve() if acceptance_receipt_arg else None
    )
    acceptance_receipt_input_hash = (
        sha256_file(acceptance_receipt_path) if acceptance_receipt_path else None
    )
    payload: dict[str, Any] = {
        "planning_closeout_receipt_sha256": receipt_input_hash,
        "acceptance_receipt_sha256": acceptance_receipt_input_hash,
    }

    def mutate(state: dict[str, Any]) -> dict[str, Any]:
        if state["status"] == "CLOSED":
            return {"status": "CLOSED", "run_id": args.run_id, "closed_at": state["closed_at"]}
        if state["status"] != "ACTIVE":
            raise GraphError(f"Run cannot close from status {state['status']}")
        plan = load_current_plan(run_dir, state)
        active_ids = [node["node_id"] for node in plan["nodes"] if node["active"]]
        incomplete = [node_id for node_id in active_ids if state["nodes"][node_id]["status"] != "accepted"]
        if incomplete:
            raise GraphError("Active nodes are not accepted: " + ", ".join(sorted(incomplete)))
        final_ids = [
            node["node_id"]
            for node in plan["nodes"]
            if node["active"] and node["capability"] == "final-acceptance"
        ]
        if len(final_ids) != 1 or state["nodes"][final_ids[0]]["status"] != "accepted":
            raise GraphError("Current final-acceptance gate is not accepted")
        elapsed = budget_elapsed_seconds(plan, state)
        if elapsed > plan["global_budget"]["max_wall_seconds"] and time_policy(plan) == "hard":
            raise GraphError("Graph wall-time budget was exceeded; close requires a new run")
        if state["telemetry"]["budget_exceeded"]:
            raise GraphError("Graph budget was exceeded; close requires a new run")
        if token_policy(plan) == "hard" and state["telemetry"].get("unknown_token_results", 0):
            raise GraphError("Graph hard token budget cannot close with unknown token telemetry")
        linkage = state.get("planning_contract", {})
        if linkage.get("mode") == "approved-contract":
            if receipt_path is None:
                raise GraphError(
                    "Contracted run close requires --planning-closeout-receipt"
                )
            module, contract = load_linked_planning_contract(run_dir, state, plan)
            receipt = load_json(receipt_path, "planning closeout receipt")
            try:
                module.validate_closeout_receipt(contract, receipt, receipt_path)
            except Exception as exc:
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                raise GraphError(f"Planning closeout validation failed: {exc}") from exc
            receipt_target = (
                run_dir / "contracts" / f"closeout-revision-{state['revision']}.json"
            )
            if receipt_target.exists():
                if canonical_bytes(load_json(receipt_target, "stored planning closeout")) != canonical_bytes(receipt):
                    raise GraphError("Stored planning closeout receipt differs")
            else:
                atomic_write_json(receipt_target, receipt)
            linkage["closeout_receipt_hash"] = receipt["receipt_hash"]
            linkage["closeout_snapshot_path"] = str(
                receipt_target.relative_to(run_dir).as_posix()
            )
        elif receipt_path is not None:
            raise GraphError("Legacy run does not accept a planning closeout receipt")

        acceptance_linkage = state.get("acceptance_contract", {})
        if acceptance_linkage.get("mode") == "accept-system-0.1":
            if acceptance_receipt_path is None:
                raise GraphError(
                    "Graph close requires --acceptance-receipt from accept-system"
                )
            if linkage.get("mode") != "approved-contract":
                raise GraphError(
                    "accept-system linkage requires an approved planning contract"
                )
            acceptance_module, acceptance_contract = load_linked_acceptance_contract(
                run_dir, state, plan, contract
            )
            try:
                acceptance_summary = acceptance_module.verify_receipt_document(
                    acceptance_receipt_path,
                    expected_contract_hash=acceptance_linkage["contract_hash"],
                    expected_graph_hash=state["plan_hash"],
                    expected_graph_run_id=state["run_id"],
                    require_accepted=True,
                    require_current_runtime=True,
                )
                acceptance_receipt = load_json(
                    acceptance_receipt_path, "acceptance receipt"
                )
            except Exception as exc:
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                raise GraphError(f"Acceptance receipt validation failed: {exc}") from exc
            if acceptance_receipt.get("acceptance_id") != acceptance_contract["acceptance_id"]:
                raise GraphError("Acceptance receipt acceptance_id mismatch")
            acceptance_target = (
                run_dir
                / "contracts"
                / f"acceptance-closeout-revision-{state['revision']}.json"
            )
            if acceptance_target.exists():
                if canonical_bytes(
                    load_json(acceptance_target, "stored acceptance receipt")
                ) != canonical_bytes(acceptance_receipt):
                    raise GraphError("Stored acceptance receipt differs")
            else:
                atomic_write_json(acceptance_target, acceptance_receipt)
            acceptance_linkage["final_receipt_hash"] = acceptance_summary[
                "receipt_hash"
            ]
            acceptance_linkage["final_receipt_snapshot_path"] = str(
                acceptance_target.relative_to(run_dir).as_posix()
            )
        elif acceptance_receipt_path is not None:
            raise GraphError(
                "Run without accept-system linkage does not accept an acceptance receipt"
            )
        state["status"] = "CLOSED"
        state["closed_at"] = utc_now()
        return {
            "status": "CLOSED",
            "run_id": args.run_id,
            "revision": state["revision"],
            "plan_hash": state["plan_hash"],
            "closed_at": state["closed_at"],
            "acceptance_receipt_hash": state.get("acceptance_contract", {}).get(
                "final_receipt_hash"
            ),
            "telemetry": state["telemetry"],
        }

    return run_mutation(run_dir, args.operation_id, "close", payload, mutate)


def command_doctor(args: argparse.Namespace) -> dict[str, Any]:
    suite_root = (
        Path.home() / ".agents" / "runtime" / "agent-fleet-suite" / "current"
    ).resolve()
    required = {
        "fleet": suite_root / "agent-fleet" / "scripts" / "fleet.py",
        "loop_l2": suite_root / "agent-loop" / "scripts" / "agent_loop_l2.py",
        "runtime_marker": suite_root / ".agent-fleet-suite-runtime.json",
        "planner_validator": (
            SKILL_ROOT.parent
            / "plan-delivery"
            / "scripts"
            / "plan_contract.py"
        ).resolve(),
        "acceptance_validator": (
            SKILL_ROOT.parent
            / "accept-system"
            / "scripts"
            / "acceptance.py"
        ).resolve(),
    }
    checks = {name: path.is_file() for name, path in required.items()}
    planner_hash = (
        sha256_file(required["planner_validator"])
        if checks["planner_validator"] and not required["planner_validator"].is_symlink()
        else None
    )
    checks["planner_validator_pinned"] = planner_hash == PLANNER_VALIDATOR_SHA256
    acceptance_hash = (
        sha256_file(required["acceptance_validator"])
        if checks["acceptance_validator"]
        and not required["acceptance_validator"].is_symlink()
        else None
    )
    checks["acceptance_validator_pinned"] = (
        acceptance_hash == ACCEPTANCE_VALIDATOR_SHA256
    )
    runtime_marker = None
    if checks["runtime_marker"]:
        runtime_marker = load_json(required["runtime_marker"], "Agent Fleet runtime marker")
    versions: dict[str, Any] = {}
    for name in ("fleet", "loop_l2"):
        if not checks[name]:
            versions[name] = None
            continue
        try:
            source = required[name].read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            versions[name] = None
            continue
        match = re.search(r'^VERSION\s*=\s*["\']([^"\']+)["\']', source, re.MULTILINE)
        versions[name] = match.group(1) if match else "present-no-version-constant"
    return {
        "status": "READY" if all(checks.values()) and all(versions.values()) else "NOT_READY",
        "agent_graph_version": VERSION,
        "suite_root": str(suite_root),
        "checks": checks,
        "versions": versions,
        "planner_validator_sha256": planner_hash,
        "planner_validator_expected_sha256": PLANNER_VALIDATOR_SHA256,
        "acceptance_validator_sha256": acceptance_hash,
        "acceptance_validator_expected_sha256": ACCEPTANCE_VALIDATOR_SHA256,
        "runtime_marker": runtime_marker,
        "probe_mode": "read-only-no-exec",
        "effects": [],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and record bounded Agent Graph execution state."
    )
    parser.add_argument("--version", action="version", version=VERSION)
    subparsers = parser.add_subparsers(dest="action", required=True)

    runner = subparsers.add_parser("run", help="Execute a bound dispatch map; no model status polling")
    runner.add_argument("--run-id", required=True)
    runner.add_argument("--dispatch", required=True)
    runner.add_argument("--state-root")
    runner.add_argument("--office-home", help="Optional isolated Office home; explicit state roots isolate events by default")
    def run_schedule(args):
        from graph_runner import command_run
        return command_run(args)
    runner.set_defaults(handler=run_schedule)

    adopt = subparsers.add_parser(
        "adopt-dispatch",
        help="Adopt a new dispatch map after an accepted planning amendment; ordinary drive mismatch stays refused",
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
    def run_adopt(args):
        from graph_runner import command_adopt_dispatch
        return command_adopt_dispatch(args)
    adopt.set_defaults(handler=run_adopt)

    recon = subparsers.add_parser(
        "reconcile-failure",
        help="Annotate an exact attempt as failure-reconciled from canonical terminal evidence",
    )
    recon.add_argument("--run-id", required=True)
    recon.add_argument("--node-id", required=True)
    recon.add_argument("--attempt-id", required=True)
    recon.add_argument("--state-root")
    def run_reconcile(args):
        from graph_runner import command_reconcile_failure
        return command_reconcile_failure(args)
    recon.set_defaults(handler=run_reconcile)

    correction = subparsers.add_parser(
        "correct-negative-evidence",
        help="Recover exact negative rc0 evidence with lossless object quota normalization",
    )
    for name in ("run-id", "node-id", "attempt-id", "evidence"):
        correction.add_argument("--" + name, required=True)
    correction.add_argument("--state-root")
    def run_correction(args):
        from graph_runner import correct_negative_evidence
        return correct_negative_evidence(args)
    correction.set_defaults(handler=run_correction)

    doctor = subparsers.add_parser("doctor")
    doctor.set_defaults(handler=command_doctor)

    validate = subparsers.add_parser("validate")
    validate.add_argument("--graph", required=True)
    validate.set_defaults(handler=command_validate)

    init = subparsers.add_parser("init")
    init.add_argument("--graph", required=True)
    init.add_argument("--planning-contract", required=True)
    init.add_argument("--acceptance-contract")
    init.add_argument("--state-root")
    init.set_defaults(handler=command_init)

    for action, handler in (("ready", command_ready), ("status", command_status)):
        command = subparsers.add_parser(action)
        command.add_argument("--run-id", required=True)
        command.add_argument("--state-root")
        command.set_defaults(handler=handler)

    start = subparsers.add_parser("start")
    start.add_argument("--run-id", required=True)
    start.add_argument("--node-id", required=True)
    start.add_argument("--operation-id", required=True)
    start.add_argument("--workspace-root", required=True)
    start.add_argument("--external-ref")
    start.add_argument("--state-root")
    start.set_defaults(handler=command_start)

    result = subparsers.add_parser("result")
    result.add_argument("--run-id", required=True)
    result.add_argument("--node-id", required=True)
    result.add_argument("--operation-id", required=True)
    result.add_argument("--evidence", required=True)
    result.add_argument("--state-root")
    result.set_defaults(handler=command_result)

    amend = subparsers.add_parser("amend")
    amend.add_argument("--run-id", required=True)
    amend.add_argument("--operation-id", required=True)
    amend_mode = amend.add_mutually_exclusive_group(required=True)
    amend_mode.add_argument("--planning-contract-amendment")
    amend_mode.add_argument("--legacy-amendment", dest="amendment", help=argparse.SUPPRESS)
    amend.add_argument("--triggered-by-node")
    amend.add_argument("--acceptance-contract-amendment")
    amend.add_argument("--approval-ref")
    amend.add_argument("--state-root")
    amend.set_defaults(handler=command_amend)

    recover = subparsers.add_parser("recover")
    recover.add_argument("--run-id", required=True)
    recover.add_argument("--operation-id", required=True)
    recover.add_argument("--stale-after-seconds", type=int, default=900)
    recover.add_argument("--state-root")
    recover.set_defaults(handler=command_recover)

    close = subparsers.add_parser("close")
    close.add_argument("--run-id", required=True)
    close.add_argument("--operation-id", required=True)
    close.add_argument("--planning-closeout-receipt")
    close.add_argument("--acceptance-receipt")
    close.add_argument("--state-root")
    close.set_defaults(handler=command_close)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.handler(args)
        print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
        if args.action == "run" and result["status"] not in {"EXECUTION_COMPLETE", "CLOSED", "WAITING"}:
            return 3
        if args.action in {"adopt-dispatch", "reconcile-failure", "correct-negative-evidence"} and result["status"] not in {
            "DISPATCH_ADOPTED",
            "FAILURE_RECONCILED",
            "NEGATIVE_EVIDENCE_CORRECTED",
        }:
            return 3
        return 0
    except GraphError as exc:
        print(
            json.dumps(
                {"status": "ERROR", "error": str(exc)},
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
