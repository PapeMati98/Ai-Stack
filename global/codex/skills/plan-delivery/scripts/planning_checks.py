"""Additive, offline checks for prepared planning contracts."""
from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any


DEPENDENCY_KINDS = {"data/interface", "write-conflict", "gate"}
CAPACITY_KINDS = {"owner-hard-cap", "host-capacity-estimate"}
PREPARATION_POLICY = "parallel-planning-v1"


def _text(value: Any, label: str, minimum: int = 1, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def validate_dependency_justifications(tasks: Mapping[str, Mapping[str, Any]], *, required: bool = False) -> None:
    """Require a reason and concrete input for every newly declared edge."""
    for task_id, task in tasks.items():
        deps = task.get("depends_on", [])
        justifications = task.get("dependency_justifications")
        if not required and "dependency_justifications" not in task:
            continue  # Legacy v1 maps remain readable.
        if not isinstance(justifications, Mapping) or set(justifications) != set(deps):
            raise ValueError(f"{task_id}.dependency_justifications must cover exactly depends_on")
        for dependency in deps:
            item = justifications[dependency]
            if not isinstance(item, Mapping) or set(item) != {"kind", "reason", "input"}:
                raise ValueError(f"{task_id}->{dependency} dependency justification is incomplete")
            if not isinstance(item["kind"], str) or item["kind"] not in DEPENDENCY_KINDS:
                raise ValueError(f"{task_id}->{dependency} has unsupported dependency kind")
            _text(item["reason"], f"{task_id}->{dependency}.reason", 8)
            _text(item["input"], f"{task_id}->{dependency}.input", 3)


def validate_capacity_basis(value: Any) -> dict[str, Any] | None:
    """Validate an optional capacity basis without deriving it from providers."""
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {"kind", "value", "reason", "evidence"}:
        raise ValueError("capacity_basis must contain kind, value, reason and evidence")
    if not isinstance(value["kind"], str) or value["kind"] not in CAPACITY_KINDS:
        raise ValueError("capacity_basis.kind must be owner-hard-cap or host-capacity-estimate")
    if type(value["value"]) is not int or not 1 <= value["value"] <= 16:
        raise ValueError("capacity_basis.value must be an integer in 1..16")
    _text(value["reason"], "capacity_basis.reason", 8, 500)
    _text(value["evidence"], "capacity_basis.evidence", 3)
    encoded = json.dumps({'planning_capacity_basis': dict(value)}, ensure_ascii=True,
                         sort_keys=True, separators=(',', ':')).encode('ascii')
    if len(encoded) > 4096:
        raise ValueError('capacity_basis exceeds the executable dispatch evidence bound')
    return dict(value)


def validate_adapter_declarations(tasks: Mapping[str, Mapping[str, Any]], declarations: Any) -> dict[str, Any]:
    """Check dispatchability while making manual authority explicit."""
    if not isinstance(declarations, Mapping):
        raise ValueError("adapter_declarations must be an object")
    active = {task_id: task for task_id, task in tasks.items() if task.get("status") == "pending"}
    unknown = sorted(set(declarations) - set(active))
    if unknown:
        raise ValueError("adapter_declarations references unknown or inactive tasks: " + ", ".join(unknown))
    result = {}
    for task_id, task in active.items():
        item = declarations.get(task_id)
        manual = task.get("kind") == "human" or task.get("capability") in {"final-acceptance", "production-approval", "deep-security-approval"} or task.get("executor") == "human"
        if manual:
            if item != {"mode": "manual"}:
                raise ValueError(f"{task_id} must be explicitly declared manual")
            result[task_id] = dict(item)
        elif isinstance(item, Mapping) and item.get("mode") == "manual":
            if set(item) != {"mode", "reason"}:
                raise ValueError(f"{task_id} manual technical override requires a concrete reason")
            _text(item["reason"], f"{task_id}.manual.reason", 8)
            result[task_id] = dict(item)
        else:
            if not isinstance(item, Mapping) or set(item) != {"mode", "adapter"} or item["mode"] != "dispatch":
                raise ValueError(f"{task_id} is dispatchable and requires an adapter")
            _text(item["adapter"], f"{task_id}.adapter", 2, 256)
            result[task_id] = dict(item)
    return result


def validate_preparation(contract: Mapping[str, Any]) -> None:
    """Enforce the explicit new-draft policy without upgrading legacy contracts."""
    planning = contract["planning"]
    if "preparation_policy" not in planning:
        return
    if planning["preparation_policy"] != PREPARATION_POLICY:
        raise ValueError("Unsupported planning.preparation_policy")
    tasks = {task["task_id"]: task for task in contract["tasks"]}
    validate_dependency_justifications(tasks, required=True)
    if contract["graph"].get("capacity_basis") is None:
        raise ValueError("Prepared drafts require graph.capacity_basis")
    validate_capacity_basis(contract["graph"]["capacity_basis"])
    validate_adapter_declarations(tasks, planning.get("adapter_declarations"))


def parallelism_report(tasks: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Report likely needless serial edges; never edits dependencies."""
    if not isinstance(tasks, Mapping) or not 1 <= len(tasks) <= 128:
        raise ValueError("Report requires a DAG of 1..128 tasks")
    edges = {}
    for task_id, task in tasks.items():
        deps = task.get("depends_on", []) if isinstance(task, Mapping) else None
        if (not isinstance(deps, list) or len(deps) > 32
                or any(not isinstance(dep, str) or dep not in tasks for dep in deps)
                or len(set(deps)) != len(deps)):
            raise ValueError("Report requires valid unique dependency references")
        edges[task_id] = deps
    # Bounded topological traversal also rejects cycles before computing closure.
    ancestors = {}
    pending = set(tasks)
    while pending:
        ready = [task_id for task_id in tasks if task_id in pending
                 and all(dep in ancestors for dep in edges[task_id])]
        if not ready:
            raise ValueError("Report requires an acyclic dependency graph")
        for task_id in ready:
            ancestors[task_id] = set(edges[task_id])
            for dep in edges[task_id]:
                ancestors[task_id].update(ancestors[dep])
            pending.remove(task_id)

    warnings = []
    for task_id, task in tasks.items():
        for dep in edges[task_id]:
            declarations = task.get("dependency_justifications")
            item = declarations.get(dep) if isinstance(declarations, Mapping) else None
            try:
                validate_dependency_justifications({task_id: {
                    "depends_on": [dep], "dependency_justifications": {dep: item}}}, required=True)
            except ValueError:
                warnings.append({"from": dep, "to": task_id, "reason": "missing or invalid dependency justification"})
            if any(dep in ancestors[other] for other in edges[task_id] if other != dep):
                warnings.append({"from": dep, "to": task_id, "reason": "redundant transitive edge; review business intent"})
    return {"status": "REPORT_ONLY", "suspicious_serial_edges": warnings, "dependencies_changed": False}
