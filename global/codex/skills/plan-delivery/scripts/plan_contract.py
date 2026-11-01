#!/usr/bin/env python3
"""Deterministic planning-contract validator and Agent Graph V1 compiler."""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
import re
import sys
sys.dont_write_bytecode = True
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _load_budget_pools() -> Any:
    module_path = Path(__file__).with_name("budget_pools.py")
    spec = importlib.util.spec_from_file_location("zaplanuj_budget_pools", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load budget pool module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


budget_pools = _load_budget_pools()


def _load_planning_checks() -> Any:
    spec = importlib.util.spec_from_file_location(
        "zaplanuj_planning_checks", Path(__file__).with_name("planning_checks.py"))
    if spec is None or spec.loader is None:
        raise RuntimeError("Planning checks are unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


planning_checks = _load_planning_checks()


def _load_planner_witness() -> Any:
    spec = importlib.util.spec_from_file_location(
        "zaplanuj_planner_witness", Path(__file__).with_name("planner_witness.py"))
    if spec is None or spec.loader is None:
        raise RuntimeError("Strong planner evidence verifier is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


planner_witness = _load_planner_witness()
BudgetPoolError = budget_pools.BudgetPoolError
validate_owner_ceiling_value = budget_pools.validate_owner_ceiling
reconcile_runs = budget_pools.reconcile_runs
render_reconciliation_markdown = budget_pools.render_reconciliation_markdown


VERSION = "0.3.0"
SCHEMA_VERSION = 1

SHA_RE = re.compile(r"^[a-f0-9]{64}$")
CONTRACT_ID_RE = re.compile(r"^plan-[a-z0-9][a-z0-9-]{2,63}$")
REQ_ID_RE = re.compile(r"^REQ-[0-9]{3,6}$")
CLAUSE_ID_RE = re.compile(r"^CLAUSE-[0-9]{3,6}$")
TASK_ID_RE = re.compile(r"^TASK-[0-9]{3,6}$")
GRAPH_ID_RE = re.compile(r"^graph-[a-z0-9][a-z0-9-]{2,63}$")
NODE_ID_RE = re.compile(r"^node-[a-z0-9][a-z0-9-]{2,63}$")
CRIT_ID_RE = re.compile(r"^crit-[a-z0-9][a-z0-9-]{2,63}$")
GENERIC_ID_RE = re.compile(r"^[A-Z][A-Z0-9-]{2,63}$")
LOCK_RE = re.compile(r"^[a-z0-9][a-z0-9:./_-]{1,255}$")

CONTRACT_STATUSES = {"draft", "approved", "amended", "blocked", "closed"}
TIERS = {"compact", "full"}
CLASSIFICATIONS = {"public", "internal", "confidential", "restricted"}
REQUIREMENT_KINDS = {
    "goal",
    "constraint",
    "negative",
    "acceptance",
    "output",
    "reporting",
    "risk",
    "out_of_scope",
    "question",
    "decision",
    "conditional",
    "deferred",
    "owner_decision",
}
CLAUSE_KINDS = {
    "explicit",
    "negative",
    "conditional",
    "deferred",
    "acceptance",
    "output",
    "reporting",
    "risk",
    "out_of_scope",
    "owner_decision",
    "derived",
}
REQUIREMENT_STATUSES = {"active", "satisfied", "blocked", "not_applicable"}
TASK_STATUSES = {"pending", "blocked"}
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
GATE_STATUSES = {"pending", "pass", "repair", "blocked"}
HARD_RISK_FLAGS = {
    "production",
    "auth-role",
    "secrets",
    "customer-data",
    "destructive-write",
    "public-contract",
    "external-mutation",
    "material-cost",
    "irreversible-decision",
    "instruction-conflict",
}
HARD_RISK_PATTERNS = {
    "production": re.compile(
        r"\b(?:production|prod|produk(?:cja|cji|cyjny|cyjna|cyjne|cyjnego|cyjnym))\b"
    ),
    "auth-role": re.compile(
        r"\b(?:oauth|rbac|authentication|authorization|uwierzyteln\w*|uprawnien\w*)\b"
    ),
    "secrets": re.compile(
        r"\b(?:secret\w*|credential\w*|password\w*|api[ _-]?key\w*|sekret\w*|hasl\w*)\b"
    ),
    "customer-data": re.compile(
        r"\b(?:customer data|personal data|pii|dane klient\w*|dane osobow\w*)\b"
    ),
    "destructive-write": re.compile(
        r"\b(?:drop table|truncate table|delete data|destructive migration|usun dane|migracj\w+ destrukcyjn\w*)\b"
    ),
    "public-contract": re.compile(
        r"\b(?:public api|public domain|external integration contract|publiczn\w+ api|domen\w+ publiczn\w*)\b"
    ),
    "external-mutation": re.compile(
        r"\b(?:send (?:an )?(?:email|message)|calendar mutation|remote mutation|wyslij (?:mail|wiadomosc)|platnosc\w*)\b"
    ),
    "material-cost": re.compile(
        r"\b(?:material cost|paid purchase|significant spend|istotn\w+ koszt\w*)\b"
    ),
    "irreversible-decision": re.compile(
        r"\b(?:irreversible decision|nieodwracaln\w+ decyzj\w*)\b"
    ),
    "instruction-conflict": re.compile(
        r"\b(?:conflicting instructions|instruction conflict|sprzeczn\w+ polecen\w*)\b"
    ),
}


class ContractError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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


def canonical_contract_hash(contract: dict[str, Any]) -> str:
    payload = copy.deepcopy(contract)
    approval = payload.get("approval")
    if isinstance(approval, dict):
        approval["contract_hash"] = None
    return sha256_value(payload)


def planning_payload(contract: dict[str, Any]) -> dict[str, Any]:
    graph = copy.deepcopy(contract["graph"])
    graph["projection_sha256"] = None
    payload = {
        "prompt": contract["prompt"],
        "classification": contract["classification"],
        "clauses": contract["clauses"],
        "requirements": contract["requirements"],
        "questions": contract["questions"],
        "assumptions": contract["assumptions"],
        "out_of_scope": contract["out_of_scope"],
        "decisions": contract["decisions"],
        "tasks": contract["tasks"],
        "owner_ceiling": contract["owner_ceiling"],
        "graph": graph,
    }
    # Keep legacy hashes byte-compatible; bind the opt-in policy when present.
    for field in ("preparation_policy", "adapter_declarations"):
        if field in contract["planning"]:
            payload[field] = copy.deepcopy(contract["planning"][field])
    if "review_policy" in contract["planning"]:
        payload["review_policy"] = contract["planning"]["review_policy"]
        payload["planner"] = {key: contract['planning'][key] for key in (
            'planner_identity', 'planner_lineage', 'planner_session_id')}
    migration = (contract.get('amendment') or {}).get('review_policy_migration')
    if migration is not None:
        payload['review_policy_migration'] = migration
    return payload


def planning_payload_hash(contract: dict[str, Any]) -> str:
    return sha256_value(planning_payload(contract))


def normalize_prompt(text: str) -> str:
    normalized = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    return normalized.rstrip("\n") + "\n"


def detectable_prompt_text(text: str) -> str:
    translated = text.translate({ord("\u0142"): "l", ord("\u0141"): "L"})
    decomposed = unicodedata.normalize("NFKD", translated)
    return "".join(char for char in decomposed if not unicodedata.combining(char)).lower()


def detect_hard_risk_flags(text: str) -> list[str]:
    searchable = detectable_prompt_text(text)
    return sorted(
        flag for flag, pattern in HARD_RISK_PATTERNS.items() if pattern.search(searchable)
    )


def prompt_hash(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ContractError(f"Cannot read UTF-8 prompt {path}: {exc}") from exc
    return hashlib.sha256(normalize_prompt(text).encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ContractError(f"Cannot read evidence {path}: {exc}") from exc
    return digest.hexdigest()


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"Cannot read {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError(f"{label} must be a JSON object")
    return value


def write_new_json(path: Path, value: dict[str, Any]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n").encode("utf-8")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ContractError(f"Refusing to overwrite immutable output: {path}") from exc
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            path.unlink(missing_ok=True)
        finally:
            raise


def exact_keys(value: Any, required: set[str], optional: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{label} must be an object")
    missing = sorted(required - set(value))
    extra = sorted(set(value) - required - optional)
    if missing or extra:
        raise ContractError(f"{label} keys mismatch: missing={missing}; extra={extra}")
    return value


def text_value(value: Any, label: str, minimum: int = 1, maximum: int = 4000) -> str:
    if not isinstance(value, str) or not (minimum <= len(value.strip()) <= maximum):
        raise ContractError(f"{label} must be text with length {minimum}..{maximum}")
    return value


def int_value(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not (minimum <= value <= maximum):
        raise ContractError(f"{label} must be an integer in {minimum}..{maximum}")
    return value


def bool_value(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(f"{label} must be boolean")
    return value


def sha_value(value: Any, label: str, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not SHA_RE.fullmatch(value):
        raise ContractError(f"{label} must be a lowercase SHA-256")
    return value


def string_list(value: Any, label: str, maximum: int = 256, unique: bool = True) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ContractError(f"{label} must be an array with at most {maximum} entries")
    result: list[str] = []
    for index, item in enumerate(value):
        result.append(text_value(item, f"{label}[{index}]", 1, 1024))
    if unique and len(result) != len(set(result)):
        raise ContractError(f"{label} must contain unique values")
    return result


def relative_path(value: Any, label: str, allow_directory: bool = False) -> str:
    path = text_value(value, label, 1, 512)
    if "\\" in path or "*" in path or "?" in path or os.path.isabs(path):
        raise ContractError(f"{label} must be a normalized relative path")
    is_directory = path.endswith("/")
    if is_directory and not allow_directory:
        raise ContractError(f"{label} must identify a file")
    body = path[:-1] if is_directory else path
    parts = body.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ContractError(f"{label} contains an invalid path segment")
    return path


def path_scope_list(value: Any, label: str) -> list[str]:
    paths = string_list(value, label, 128)
    for index, path in enumerate(paths):
        relative_path(path, f"{label}[{index}]", allow_directory=True)
    return paths


def validate_prompt(value: Any) -> None:
    prompt = exact_keys(value, {"source", "summary", "sha256", "context_sources"}, set(), "prompt")
    relative_path(prompt["source"], "prompt.source")
    text_value(prompt["summary"], "prompt.summary", 10, 2000)
    sha_value(prompt["sha256"], "prompt.sha256")
    sources = prompt["context_sources"]
    if not isinstance(sources, list) or len(sources) > 64:
        raise ContractError("prompt.context_sources must contain at most 64 entries")
    labels: set[str] = set()
    for index, source in enumerate(sources):
        label = f"prompt.context_sources[{index}]"
        source = exact_keys(source, {"label", "kind", "path", "sha256"}, set(), label)
        name = text_value(source["label"], f"{label}.label", 1, 120)
        if name in labels:
            raise ContractError(f"Duplicate context source label: {name}")
        labels.add(name)
        if source["kind"] not in {"file", "note", "url"}:
            raise ContractError(f"Unsupported {label}.kind")
        if source["kind"] == "file":
            relative_path(source["path"], f"{label}.path")
        else:
            text_value(source["path"], f"{label}.path", 1, 1024)
        sha_value(source["sha256"], f"{label}.sha256")


def validate_classification(value: Any) -> None:
    classification = exact_keys(
        value,
        {"tier", "hard_risk_flags", "data_classification", "production_boundary"},
        set(),
        "classification",
    )
    if classification["tier"] not in TIERS:
        raise ContractError("classification.tier must be compact or full")
    flags = string_list(classification["hard_risk_flags"], "classification.hard_risk_flags", 32)
    if len(flags) != len(set(flags)) or flags != sorted(flags):
        raise ContractError("classification.hard_risk_flags must be unique and sorted")
    unknown = sorted(set(flags) - HARD_RISK_FLAGS)
    if unknown:
        raise ContractError(f"Unknown hard-risk flags: {unknown}")
    if flags and classification["tier"] != "full":
        raise ContractError("Any hard-risk flag requires tier=full")
    if classification["data_classification"] not in CLASSIFICATIONS:
        raise ContractError("Unsupported classification.data_classification")
    text_value(classification["production_boundary"], "classification.production_boundary", 3, 1000)


def numeric_id(identifier: str) -> int:
    return int(identifier.rsplit("-", 1)[1])


def validate_clauses(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list) or not (1 <= len(value) <= 256):
        raise ContractError("clauses must contain 1..256 entries")
    result: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(value):
        label = f"clauses[{index}]"
        item = exact_keys(
            item,
            {"clause_id", "kind", "text", "source_ref", "material", "requirement_refs"},
            set(),
            label,
        )
        clause_id = text_value(item["clause_id"], f"{label}.clause_id", 10, 20)
        if not CLAUSE_ID_RE.fullmatch(clause_id) or clause_id in result:
            raise ContractError(f"Invalid or duplicate clause ID: {clause_id}")
        if item["kind"] not in CLAUSE_KINDS:
            raise ContractError(f"Unsupported {label}.kind")
        text_value(item["text"], f"{label}.text", 3, 2000)
        text_value(item["source_ref"], f"{label}.source_ref", 3, 512)
        bool_value(item["material"], f"{label}.material")
        refs = string_list(item["requirement_refs"], f"{label}.requirement_refs", 128)
        for ref in refs:
            if not REQ_ID_RE.fullmatch(ref):
                raise ContractError(f"Invalid requirement ref {ref} in {clause_id}")
        if item["material"] and not refs:
            raise ContractError(f"Material clause has no requirement coverage: {clause_id}")
        result[clause_id] = item
    ordered = [numeric_id(identifier) for identifier in result]
    if ordered != sorted(ordered):
        raise ContractError("Clause IDs must be in ascending order")
    return result


def validate_requirements(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list) or not (1 <= len(value) <= 256):
        raise ContractError("requirements must contain 1..256 entries")
    result: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(value):
        label = f"requirements[{index}]"
        item = exact_keys(
            item,
            {
                "requirement_id",
                "kind",
                "text",
                "source_ref",
                "clause_refs",
                "material",
                "task_refs",
                "status",
            },
            set(),
            label,
        )
        req_id = text_value(item["requirement_id"], f"{label}.requirement_id", 7, 16)
        if not REQ_ID_RE.fullmatch(req_id) or req_id in result:
            raise ContractError(f"Invalid or duplicate requirement ID: {req_id}")
        if item["kind"] not in REQUIREMENT_KINDS:
            raise ContractError(f"Unsupported {label}.kind")
        text_value(item["text"], f"{label}.text", 3, 2000)
        text_value(item["source_ref"], f"{label}.source_ref", 3, 512)
        clause_refs = string_list(item["clause_refs"], f"{label}.clause_refs", 128)
        if not clause_refs:
            raise ContractError(f"{req_id} must trace to at least one clause")
        for ref in clause_refs:
            if not CLAUSE_ID_RE.fullmatch(ref):
                raise ContractError(f"Invalid clause ref {ref} in {req_id}")
        bool_value(item["material"], f"{label}.material")
        refs = string_list(item["task_refs"], f"{label}.task_refs", 128)
        for ref in refs:
            if not TASK_ID_RE.fullmatch(ref):
                raise ContractError(f"Invalid task ref {ref} in {req_id}")
        if item["status"] not in REQUIREMENT_STATUSES:
            raise ContractError(f"Unsupported {label}.status")
        result[req_id] = item
    ordered = [numeric_id(identifier) for identifier in result]
    if ordered != sorted(ordered):
        raise ContractError("Requirement IDs must be in ascending order")
    return result


def validate_assumptions(value: Any) -> None:
    if not isinstance(value, list) or len(value) > 64:
        raise ContractError("assumptions must contain at most 64 entries")
    ids: set[str] = set()
    for index, item in enumerate(value):
        label = f"assumptions[{index}]"
        item = exact_keys(item, {"assumption_id", "text", "reversible", "blast_radius", "evidence_ref"}, set(), label)
        item_id = text_value(item["assumption_id"], f"{label}.assumption_id", 3, 64)
        if not GENERIC_ID_RE.fullmatch(item_id) or item_id in ids:
            raise ContractError(f"Invalid or duplicate assumption ID: {item_id}")
        ids.add(item_id)
        text_value(item["text"], f"{label}.text", 3, 1000)
        if item["reversible"] is not True:
            raise ContractError(f"{label} must be reversible")
        if item["blast_radius"] not in {"local", "workspace"}:
            raise ContractError(f"{label}.blast_radius must be local or workspace")
        text_value(item["evidence_ref"], f"{label}.evidence_ref", 3, 512)


def validate_decisions(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 64:
        raise ContractError("decisions must contain at most 64 entries")
    ids: set[str] = set()
    decisions: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        label = f"decisions[{index}]"
        item = exact_keys(item, {"decision_id", "text", "authority", "evidence_ref"}, set(), label)
        item_id = text_value(item["decision_id"], f"{label}.decision_id", 3, 64)
        if not GENERIC_ID_RE.fullmatch(item_id) or item_id in ids:
            raise ContractError(f"Invalid or duplicate decision ID: {item_id}")
        ids.add(item_id)
        text_value(item["text"], f"{label}.text", 3, 1000)
        if item["authority"] not in {"parent", "mateusz", "council"}:
            raise ContractError(f"Unsupported {label}.authority")
        text_value(item["evidence_ref"], f"{label}.evidence_ref", 3, 512)
        decisions.append(item)
    return decisions


def validate_budget(value: Any, label: str, global_budget: bool = False) -> None:
    if isinstance(value, dict) and value.get("token_policy", "estimate") not in {"hard", "estimate"}:
        raise ContractError(f"{label}.token_policy must be hard or estimate")
    if global_budget and isinstance(value, dict) and value.get("time_policy", "estimate") not in {"hard", "estimate"}:
        raise ContractError(f"{label}.time_policy must be hard or estimate")
    if not global_budget and isinstance(value, dict) and value.get("tool_policy", "estimate") not in {"hard", "estimate"}:
        raise ContractError(f"{label}.tool_policy must be hard or estimate")
    if global_budget and isinstance(value, dict) and value.get("clock", "elapsed") not in {"elapsed", "active"}:
        raise ContractError(f"{label}.clock must be elapsed or active")
    if global_budget:
        budget = exact_keys(
            value,
            {"max_total_attempts", "max_total_tokens", "max_wall_seconds", "max_amendments"},
            {"token_policy", "time_policy", "clock"},
            label,
        )
        int_value(budget["max_total_attempts"], f"{label}.max_total_attempts", 1, 512)
        int_value(budget["max_wall_seconds"], f"{label}.max_wall_seconds", 60, 604_800)
        int_value(budget["max_total_tokens"], f"{label}.max_total_tokens", 1, 100_000_000)
        int_value(budget["max_amendments"], f"{label}.max_amendments", 0, 32)
        return
    budget = exact_keys(
        value,
        {"max_attempts", "max_wall_seconds", "max_tokens", "max_tool_calls"},
        {"token_policy", "tool_policy"},
        label,
    )
    int_value(budget["max_attempts"], f"{label}.max_attempts", 1, 10)
    int_value(budget["max_wall_seconds"], f"{label}.max_wall_seconds", 30, 86_400)
    if budget["max_tokens"] is not None:
        int_value(budget["max_tokens"], f"{label}.max_tokens", 1, 10_000_000)
    int_value(budget["max_tool_calls"], f"{label}.max_tool_calls", 0, 2048)


def validate_task(value: Any, index: int) -> dict[str, Any]:
    label = f"tasks[{index}]"
    task = exact_keys(
        value,
        {
            "task_id",
            "graph_node_id",
            "title",
            "kind",
            "requirement_refs",
            "depends_on",
            "evidence_from",
            "wave",
            "capability",
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
            "status",
        },
        {"maker_lineage", "reviewer_lineage", "dependency_justifications", "interface_contract"},
        label,
    )
    task_id = text_value(task["task_id"], f"{label}.task_id", 8, 18)
    if not TASK_ID_RE.fullmatch(task_id):
        raise ContractError(f"Invalid task ID: {task_id}")
    node_id = text_value(task["graph_node_id"], f"{label}.graph_node_id", 6, 69)
    if not NODE_ID_RE.fullmatch(node_id):
        raise ContractError(f"Invalid graph node ID: {node_id}")
    text_value(task["title"], f"{label}.title", 4, 200)
    if task["kind"] not in NODE_KINDS:
        raise ContractError(f"Unsupported {label}.kind")
    for field, pattern, maximum in (
        ("requirement_refs", REQ_ID_RE, 128),
        ("depends_on", TASK_ID_RE, 32),
        ("evidence_from", TASK_ID_RE, 32),
    ):
        refs = string_list(task[field], f"{label}.{field}", maximum)
        for ref in refs:
            if not pattern.fullmatch(ref):
                raise ContractError(f"Invalid {field} reference {ref} in {task_id}")
    if "dependency_justifications" in task:
        try:
            planning_checks.validate_dependency_justifications({task_id: task})
        except ValueError as exc:
            raise ContractError(str(exc)) from exc
    if "interface_contract" in task:
        interface = task["interface_contract"]
        if not isinstance(interface, dict) or set(interface) != {"status", "evidence"}:
            raise ContractError(f"{task_id}.interface_contract must contain status and evidence")
        if interface["status"] not in {"not-applicable", "declared", "ready", "implemented"}:
            raise ContractError(f"Unsupported {task_id}.interface_contract.status")
        text_value(interface["evidence"], f"{label}.interface_contract.evidence", 3, 4096)
    int_value(task["wave"], f"{label}.wave", 0, 10_000)
    if task["capability"] not in CAPABILITIES:
        raise ContractError(f"Unsupported {label}.capability")
    if task["executor"] not in EXECUTORS:
        raise ContractError(f"Unsupported {label}.executor")
    text_value(task["reviewer"], f"{label}.reviewer", 3, 200)
    if task["reviewer"] == task["executor"]:
        raise ContractError(f"{task_id} cannot self-review")
    maker_lineage = task.get("maker_lineage")
    reviewer_lineage = task.get("reviewer_lineage")
    if (maker_lineage is None) != (reviewer_lineage is None):
        raise ContractError(
            f"{task_id} must declare both maker_lineage and reviewer_lineage"
        )
    if maker_lineage is not None:
        text_value(maker_lineage, f"{label}.maker_lineage", 2, 100)
        text_value(reviewer_lineage, f"{label}.reviewer_lineage", 2, 100)
        if maker_lineage.casefold() == reviewer_lineage.casefold():
            raise ContractError(
                f"{task_id} maker_lineage cannot equal reviewer_lineage"
            )
    if task["workspace_mode"] not in WORKSPACE_MODES:
        raise ContractError(f"Unsupported {label}.workspace_mode")
    path_scope_list(task["read_scope"], f"{label}.read_scope")
    path_scope_list(task["write_scope"], f"{label}.write_scope")
    locks = string_list(task["resource_locks"], f"{label}.resource_locks", 128)
    for lock in locks:
        if not LOCK_RE.fullmatch(lock):
            raise ContractError(f"Invalid resource lock in {task_id}: {lock}")
    criteria = task["acceptance_criteria"]
    if not isinstance(criteria, list) or not (1 <= len(criteria) <= 64):
        raise ContractError(f"{label}.acceptance_criteria must contain 1..64 entries")
    criterion_ids: set[str] = set()
    for criterion_index, criterion in enumerate(criteria):
        criterion_label = f"{label}.acceptance_criteria[{criterion_index}]"
        criterion = exact_keys(criterion, {"criterion_id", "description", "evidence_kind"}, set(), criterion_label)
        criterion_id = text_value(criterion["criterion_id"], f"{criterion_label}.criterion_id", 6, 69)
        if not CRIT_ID_RE.fullmatch(criterion_id) or criterion_id in criterion_ids:
            raise ContractError(f"Invalid or duplicate criterion ID: {criterion_id}")
        criterion_ids.add(criterion_id)
        text_value(criterion["description"], f"{criterion_label}.description", 8, 1000)
        if criterion["evidence_kind"] not in EVIDENCE_KINDS:
            raise ContractError(f"Unsupported {criterion_label}.evidence_kind")
    validate_budget(task["budget"], f"{label}.budget")
    repair = exact_keys(task["repair_policy"], {"on_reject", "max_same_scope_repairs", "max_amendments"}, set(), f"{label}.repair_policy")
    if repair["on_reject"] not in {"retry-same-scope", "amend-graph", "block", "human-gate"}:
        raise ContractError(f"Unsupported {label}.repair_policy.on_reject")
    int_value(repair["max_same_scope_repairs"], f"{label}.repair_policy.max_same_scope_repairs", 0, 5)
    int_value(repair["max_amendments"], f"{label}.repair_policy.max_amendments", 0, 10)
    loop = exact_keys(task["loop_policy"], {"enabled", "max_rounds", "stop_condition"}, set(), f"{label}.loop_policy")
    bool_value(loop["enabled"], f"{label}.loop_policy.enabled")
    int_value(loop["max_rounds"], f"{label}.loop_policy.max_rounds", 0, 10)
    if not loop["enabled"] and loop["max_rounds"] != 0:
        raise ContractError(f"{label}.loop_policy.max_rounds must be 0 when disabled")
    text_value(loop["stop_condition"], f"{label}.loop_policy.stop_condition", 3, 1000)
    council = exact_keys(task["council_trigger"], {"required", "reason"}, set(), f"{label}.council_trigger")
    bool_value(council["required"], f"{label}.council_trigger.required")
    text_value(council["reason"], f"{label}.council_trigger.reason", 3, 1000)
    if task["status"] not in TASK_STATUSES:
        raise ContractError(f"Unsupported {label}.status")
    mutating = bool(task["write_scope"])
    if mutating and task["workspace_mode"] not in {"detached-worktree", "integration-owner"}:
        raise ContractError(
            f"Mutating task {task_id} needs detached-worktree or integration-owner"
        )
    if not mutating and task["workspace_mode"] in {"detached-worktree", "integration-owner"}:
        raise ContractError(
            f"Non-mutating task {task_id} cannot claim a mutating workspace mode"
        )
    if task["workspace_mode"] == "read-only" and mutating:
        raise ContractError(f"Read-only task {task_id} declares write scope")
    if task["kind"] == "human":
        if task["executor"] != "human" or task["workspace_mode"] != "none" or mutating:
            raise ContractError(
                f"Human task {task_id} must use executor=human, workspace_mode=none, no writes"
            )
    if task["executor"] == "human" and task["kind"] != "human":
        raise ContractError(f"Only human tasks may use executor=human: {task_id}")
    if task["executor"] == "fleet-quality-gate" and task["capability"] != "independent-quality":
        raise ContractError(
            f"fleet-quality-gate requires independent-quality capability: {task_id}"
        )
    if task["executor"] == "fleet-security-gate" and task["capability"] != "incremental-security":
        raise ContractError(
            f"fleet-security-gate requires incremental-security capability: {task_id}"
        )
    if task["capability"] == "integration" and task["workspace_mode"] != "integration-owner":
        raise ContractError(f"Integration capability must be the integration owner: {task_id}")
    if task["capability"] == "final-acceptance" and not (
        task["kind"] == "gate" and task["executor"] == "parent-local"
    ):
        raise ContractError(f"Final acceptance must be a parent-local gate: {task_id}")
    return task


def path_prefix_overlap(left: str, right: str) -> bool:
    left_parts = left.rstrip("/").split("/")
    right_parts = right.rstrip("/").split("/")
    common = min(len(left_parts), len(right_parts))
    return left_parts[:common] == right_parts[:common]


def transitive_dependencies(task_id: str, tasks: dict[str, dict[str, Any]]) -> set[str]:
    result: set[str] = set()
    stack = list(tasks[task_id]["depends_on"])
    while stack:
        current = stack.pop()
        if current in result:
            continue
        result.add(current)
        stack.extend(tasks[current]["depends_on"])
    return result


def validate_tasks(
    value: Any,
    requirements: dict[str, dict[str, Any]],
    clauses: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list) or not (1 <= len(value) <= 128):
        raise ContractError("tasks must contain 1..128 entries")
    tasks: dict[str, dict[str, Any]] = {}
    node_ids: set[str] = set()
    criterion_ids: set[str] = set()
    for index, raw in enumerate(value):
        task = validate_task(raw, index)
        task_id = task["task_id"]
        if task_id in tasks:
            raise ContractError(f"Duplicate task ID: {task_id}")
        if task["graph_node_id"] in node_ids:
            raise ContractError(f"Duplicate graph node ID: {task['graph_node_id']}")
        tasks[task_id] = task
        node_ids.add(task["graph_node_id"])
        for criterion in task["acceptance_criteria"]:
            criterion_id = criterion["criterion_id"]
            if criterion_id in criterion_ids:
                raise ContractError(f"criterion_id must be contract-unique: {criterion_id}")
            criterion_ids.add(criterion_id)
    ordered = [numeric_id(identifier) for identifier in tasks]
    if ordered != sorted(ordered):
        raise ContractError("Task IDs must be in ascending order")
    for task_id, task in tasks.items():
        missing_requirements = sorted(set(task["requirement_refs"]) - set(requirements))
        missing_dependencies = sorted(set(task["depends_on"]) - set(tasks))
        missing_evidence = sorted(set(task["evidence_from"]) - set(tasks))
        if missing_requirements:
            raise ContractError(f"{task_id} references missing requirements: {missing_requirements}")
        if missing_dependencies or missing_evidence:
            raise ContractError(f"{task_id} has missing task refs: dependencies={missing_dependencies}; evidence={missing_evidence}")
        if task_id in transitive_dependencies(task_id, tasks):
            raise ContractError(f"Task graph contains a cycle through {task_id}")
        for req_id in task["requirement_refs"]:
            if task_id not in requirements[req_id]["task_refs"]:
                raise ContractError(f"Traceability mismatch: {task_id} -> {req_id} is not reciprocal")
    for req_id, requirement in requirements.items():
        missing_clauses = sorted(set(requirement["clause_refs"]) - set(clauses))
        if missing_clauses:
            raise ContractError(f"{req_id} references missing clauses: {missing_clauses}")
        for clause_id in requirement["clause_refs"]:
            if req_id not in clauses[clause_id]["requirement_refs"]:
                raise ContractError(
                    f"Traceability mismatch: {req_id} -> {clause_id} is not reciprocal"
                )
        missing_tasks = sorted(set(requirement["task_refs"]) - set(tasks))
        if missing_tasks:
            raise ContractError(f"{req_id} references missing tasks: {missing_tasks}")
        if requirement["material"] and requirement["status"] == "active":
            active_task_refs = [
                task_id
                for task_id in requirement["task_refs"]
                if tasks[task_id]["status"] == "pending"
            ]
            if not active_task_refs:
                raise ContractError(
                    f"Material active requirement has no active task coverage: {req_id}"
                )
        for task_id in requirement["task_refs"]:
            if req_id not in tasks[task_id]["requirement_refs"]:
                raise ContractError(f"Traceability mismatch: {req_id} -> {task_id} is not reciprocal")
    for clause_id, clause in clauses.items():
        missing_requirements = sorted(set(clause["requirement_refs"]) - set(requirements))
        if missing_requirements:
            raise ContractError(
                f"{clause_id} references missing requirements: {missing_requirements}"
            )
        for req_id in clause["requirement_refs"]:
            if clause_id not in requirements[req_id]["clause_refs"]:
                raise ContractError(
                    f"Traceability mismatch: {clause_id} -> {req_id} is not reciprocal"
                )
    ids = list(tasks)
    dependencies = {task_id: transitive_dependencies(task_id, tasks) for task_id in ids}
    for left_index, left_id in enumerate(ids):
        left = tasks[left_id]
        if left["status"] != "pending":
            continue
        for right_id in ids[left_index + 1 :]:
            right = tasks[right_id]
            if right["status"] != "pending":
                continue
            if left_id in dependencies[right_id] or right_id in dependencies[left_id]:
                continue
            overlap = any(path_prefix_overlap(a, b) for a in left["write_scope"] for b in right["write_scope"])
            shared_locks = sorted(set(left["resource_locks"]) & set(right["resource_locks"]))
            # Read-only peers can contend for a runtime lock without inventing
            # a data dependency. Unordered writers retain the existing gate.
            if overlap or (shared_locks and (left["write_scope"] or right["write_scope"])):
                raise ContractError(
                    f"Unsafe unordered tasks {left_id}/{right_id}: write_overlap={overlap}; shared_locks={shared_locks}"
                )
    return tasks


def dispatch_requirements(contract: dict[str, Any]) -> dict[str, Any]:
    """Expose approved intent keyed by Graph node ID, without creating a map."""
    validate_approved_contract(contract)
    declarations = contract["planning"].get("adapter_declarations", {})
    dispatch, manual, undeclared = {}, {}, []
    for task in contract["tasks"]:
        if task["status"] != "pending":
            continue
        node_id = task["graph_node_id"]
        declaration = declarations.get(task["task_id"])
        if declaration is None:
            undeclared.append(node_id)
        elif declaration["mode"] == "manual":
            manual[node_id] = copy.deepcopy(declaration)
        else:
            dispatch[node_id] = copy.deepcopy(declaration)
    capacity = copy.deepcopy(contract["graph"].get("capacity_basis"))
    return {
        "status": "DECLARED" if not undeclared and capacity is not None else "LEGACY_UNDECLARED",
        "contract_hash": contract["approval"]["contract_hash"],
        "graph_sha256": contract["graph"]["projection_sha256"],
        "required_dispatch_node_ids": list(dispatch),
        "dispatch_declarations": dispatch,
        "manual_declarations": manual,
        "undeclared_node_ids": undeclared,
        "capacity_basis": capacity,
        "executable": False,
    }


def prepare_dispatch(contract: dict[str, Any], declarations: Any) -> dict[str, Any]:
    """Build a non-executable, hash-bound adapter declaration report."""
    validate_contract(contract, "any")
    if ("adapter_declarations" in contract["planning"]
            and declarations != contract["planning"]["adapter_declarations"]):
        raise ContractError("Dispatch declarations differ from bound planning intent")
    try:
        adapters = planning_checks.validate_adapter_declarations(
            {task["task_id"]: task for task in contract["tasks"]}, declarations
        )
    except ValueError as exc:
        raise ContractError(str(exc)) from exc
    return {
        "schema_version": 1,
        "contract_hash": canonical_contract_hash(contract),
        "adapters": adapters,
        "parallelism": planning_checks.parallelism_report(
            {task["task_id"]: task for task in contract["tasks"]}
        ),
        "executable": False,
    }


def report_parallelism(contract: dict[str, Any]) -> dict[str, Any]:
    """Return review hints without changing the contract or its dependencies."""
    try:
        tasks = {task["task_id"]: task for task in contract["tasks"]}
        if len(tasks) != len(contract["tasks"]):
            raise ValueError("Report requires unique task IDs")
        return planning_checks.parallelism_report(tasks)
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError(f"Parallelism report: {exc}") from exc


def validate_execution_topology(
    tasks: dict[str, dict[str, Any]], integration_task_id: str
) -> None:
    active = {
        task_id: task for task_id, task in tasks.items() if task["status"] == "pending"
    }
    dependencies = {
        task_id: transitive_dependencies(task_id, tasks) for task_id in active
    }
    for task_id, task in active.items():
        inactive_refs = sorted(
            (set(task["depends_on"]) | set(task["evidence_from"])) - set(active)
        )
        if inactive_refs:
            raise ContractError(
                f"Active task {task_id} references inactive tasks: {inactive_refs}"
            )
        unrelated_evidence = sorted(
            set(task["evidence_from"]) - dependencies[task_id]
        )
        if unrelated_evidence:
            raise ContractError(
                f"{task_id}.evidence_from must cite dependency ancestors: "
                + ", ".join(unrelated_evidence)
            )

    mutating = [task for task in active.values() if task["write_scope"]]
    parallel_pairs: list[tuple[str, str]] = []
    for left_index, left in enumerate(mutating):
        for right in mutating[left_index + 1 :]:
            ordered = (
                left["task_id"] in dependencies[right["task_id"]]
                or right["task_id"] in dependencies[left["task_id"]]
            )
            if not ordered:
                parallel_pairs.append((left["task_id"], right["task_id"]))

    if integration_task_id not in active:
        raise ContractError("graph.integration_task_id must reference an active task")
    integration = active[integration_task_id]
    if (
        integration["capability"] != "integration"
        or integration["workspace_mode"] != "integration-owner"
    ):
        raise ContractError(
            "graph.integration_task_id must reference the active integration owner"
        )
    if parallel_pairs:
        branch_ids = {task_id for pair in parallel_pairs for task_id in pair}
        missing_branches = sorted(branch_ids - dependencies[integration_task_id])
        if missing_branches:
            raise ContractError(
                "Integration owner does not depend on all parallel mutation branches: "
                + ", ".join(missing_branches)
            )

    implementation_ids = {
        task_id
        for task_id, task in active.items()
        if task["capability"] == "implementation"
    }
    integration_ids = {
        task_id
        for task_id, task in active.items()
        if task["capability"] == "integration"
    }
    quality_target_ids = {
        task_id
        for task_id, task in active.items()
        if task["capability"]
        in {
            "implementation",
            "integration",
            "deterministic-tests",
            "user-journey-qa",
            "visual-qa",
            "ux-review",
            "accessibility-review",
        }
    }
    quality_tasks = [
        task for task in active.values() if task["executor"] == "fleet-quality-gate"
    ]
    security_tasks = [
        task for task in active.values() if task["executor"] == "fleet-security-gate"
    ]
    if implementation_ids:
        if not quality_tasks:
            raise ContractError(
                "Contracts with implementation tasks require an independent quality gate"
            )
        quality_covered = {
            source for task in quality_tasks for source in task["evidence_from"]
        }
        missing_quality = sorted(quality_target_ids - quality_covered)
        if missing_quality:
            raise ContractError(
                "Independent quality gate does not explicitly cover delivery tasks: "
                + ", ".join(missing_quality)
            )
        if not security_tasks:
            raise ContractError(
                "Contracts with implementation tasks require an incremental security gate"
            )
        security_covered = {
            source for task in security_tasks for source in task["evidence_from"]
        }
        missing_security = sorted(
            (implementation_ids | integration_ids) - security_covered
        )
        if missing_security:
            raise ContractError(
                "Incremental security gate does not explicitly cover implementation batch tasks: "
                + ", ".join(missing_security)
            )

    final_tasks = [
        task
        for task in active.values()
        if task["capability"] == "final-acceptance"
    ]
    if len(final_tasks) != 1:
        raise ContractError("Exactly one active final-acceptance task is required")


def validate_auditor(value: Any, label: str) -> None:
    auditor = exact_keys(value, {"identity", "lineage", "session_id", "independent_from"}, {"model"}, label)
    if 'model' in auditor:
        text_value(auditor['model'], f'{label}.model', 3, 200)
    text_value(auditor["identity"], f"{label}.identity", 3, 200)
    text_value(auditor["lineage"], f"{label}.lineage", 3, 200)
    text_value(auditor["session_id"], f"{label}.session_id", 3, 256)
    string_list(auditor["independent_from"], f"{label}.independent_from", 16)


def validate_gate(value: Any, label: str) -> dict[str, Any]:
    gate = exact_keys(
        value,
        {
            "round",
            "status",
            "auditor",
            "reviewed_prompt_sha256",
            "reviewed_plan_sha256",
            "reviewed_clause_ids",
            "reviewed_requirement_ids",
            "reviewed_task_ids",
            "attested_hard_risk_flags",
            "evidence",
        },
        set(),
        label,
    )
    int_value(gate["round"], f"{label}.round", 0, 2)
    if gate["status"] not in GATE_STATUSES:
        raise ContractError(f"Unsupported {label}.status")
    for field, pattern in (
        ("reviewed_clause_ids", CLAUSE_ID_RE),
        ("reviewed_requirement_ids", REQ_ID_RE),
        ("reviewed_task_ids", TASK_ID_RE),
    ):
        refs = string_list(gate[field], f"{label}.{field}", 256)
        if any(not pattern.fullmatch(ref) for ref in refs):
            raise ContractError(f"Invalid identifier in {label}.{field}")
    attested_flags = string_list(
        gate["attested_hard_risk_flags"],
        f"{label}.attested_hard_risk_flags",
        32,
    )
    if len(attested_flags) != len(set(attested_flags)) or attested_flags != sorted(attested_flags):
        raise ContractError(f"{label}.attested_hard_risk_flags must be unique and sorted")
    unknown_flags = sorted(set(attested_flags) - HARD_RISK_FLAGS)
    if unknown_flags:
        raise ContractError(f"Unknown attested hard-risk flags in {label}: {unknown_flags}")
    if gate["status"] == "pending":
        if any(gate[field] is not None for field in ("auditor", "reviewed_prompt_sha256", "reviewed_plan_sha256", "evidence")):
            raise ContractError(f"Pending {label} must not contain review evidence")
        if any(gate[field] for field in ("reviewed_clause_ids", "reviewed_requirement_ids", "reviewed_task_ids")):
            raise ContractError(f"Pending {label} must not contain reviewed IDs")
        if attested_flags:
            raise ContractError(f"Pending {label} must not contain hard-risk attestation")
        return gate
    validate_auditor(gate["auditor"], f"{label}.auditor")
    sha_value(gate["reviewed_prompt_sha256"], f"{label}.reviewed_prompt_sha256")
    sha_value(gate["reviewed_plan_sha256"], f"{label}.reviewed_plan_sha256")
    evidence = exact_keys(gate["evidence"], {"path", "sha256"}, set(), f"{label}.evidence")
    relative_path(evidence["path"], f"{label}.evidence.path")
    sha_value(evidence["sha256"], f"{label}.evidence.sha256")
    return gate


def validate_review_history(
    value: Any,
    gates: dict[str, dict[str, Any]],
    repair_round: int,
    revision: int,
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 1024:
        raise ContractError("planning.review_history must contain at most 1024 entries")
    seen: set[tuple[int, int, str]] = set()
    history: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        label = f"planning.review_history[{index}]"
        record = exact_keys(
            item,
            {
                "revision",
                "gate",
                "round",
                "status",
                "auditor",
                "reviewed_prompt_sha256",
                "reviewed_plan_sha256",
                "reviewed_clause_ids",
                "reviewed_requirement_ids",
                "reviewed_task_ids",
                "attested_hard_risk_flags",
                "evidence",
            },
            set(),
            label,
        )
        record_revision = int_value(record["revision"], f"{label}.revision", 1, revision)
        gate_name = record["gate"]
        if gate_name not in {"fidelity", "sufficiency"}:
            raise ContractError(f"Unsupported {label}.gate")
        gate_value = {
            key: item_value
            for key, item_value in record.items()
            if key not in {"revision", "gate"}
        }
        validate_gate(gate_value, label)
        if gate_value["status"] == "pending":
            raise ContractError(f"{label} cannot record a pending review")
        key = (record_revision, gate_value["round"], gate_name)
        if key in seen:
            raise ContractError(
                f"Duplicate review history entry: revision={key[0]} round={key[1]} gate={key[2]}"
            )
        seen.add(key)
        history.append(record)
    expected_order = sorted(
        history,
        key=lambda item: (
            item["revision"],
            item["round"],
            0 if item["gate"] == "fidelity" else 1,
        ),
    )
    if history != expected_order:
        raise ContractError("planning.review_history must be ordered by revision, round and gate")

    historical_revisions = sorted(
        {item["revision"] for item in history if item["revision"] < revision}
    )
    if historical_revisions != list(range(1, revision)):
        raise ContractError("planning.review_history must preserve every approved prior revision")
    for historical_revision in historical_revisions:
        revision_rows = [
            item for item in history if item["revision"] == historical_revision
        ]
        rounds = sorted({item["round"] for item in revision_rows})
        if rounds != list(range(rounds[-1] + 1)):
            raise ContractError(
                f"Review rounds for revision {historical_revision} must be contiguous from zero"
            )
        for round_number in rounds:
            rows = [item for item in revision_rows if item["round"] == round_number]
            if len(rows) != 2:
                raise ContractError(
                    f"Historical revision {historical_revision} round {round_number} requires two reviews"
                )
            if round_number < rounds[-1]:
                if not any(item["status"] == "repair" for item in rows):
                    raise ContractError(
                        f"Historical revision {historical_revision} round {round_number} lacks REPAIR"
                    )
                if any(item["status"] == "blocked" for item in rows):
                    raise ContractError(
                        f"Historical revision {historical_revision} round {round_number} was blocked"
                    )
            elif any(item["status"] != "pass" for item in rows):
                raise ContractError(
                    f"Historical revision {historical_revision} must end with two PASS reviews"
                )

    current_history = [item for item in history if item["revision"] == revision]
    if current_history:
        rounds = sorted({item["round"] for item in current_history})
        if rounds != list(range(rounds[-1] + 1)):
            raise ContractError("Current revision review rounds must be contiguous from zero")
        latest_round = rounds[-1]
        pending_new_round = (
            latest_round == repair_round - 1
            and all(gate["status"] == "pending" for gate in gates.values())
        )
        if latest_round not in {repair_round, repair_round - 1} or (
            latest_round == repair_round - 1 and not pending_new_round
        ):
            raise ContractError(
                "planning.repair_round must match the latest review or one new pending round"
            )
        historical_rounds = rounds if pending_new_round else rounds[:-1]
        for round_number in historical_rounds:
            rows = [item for item in current_history if item["round"] == round_number]
            if len(rows) != 2 or not any(item["status"] == "repair" for item in rows):
                raise ContractError(
                    f"Review round {round_number} may advance only after two reviews and a REPAIR verdict"
                )
            if any(item["status"] == "blocked" for item in rows):
                raise ContractError(f"Blocked review round {round_number} cannot advance")
        for gate_name, gate in gates.items():
            rows = [
                item
                for item in current_history
                if item["round"] == repair_round and item["gate"] == gate_name
            ]
            if rows:
                expected = {
                    key: item_value
                    for key, item_value in rows[0].items()
                    if key not in {"revision", "gate"}
                }
                if gate != expected:
                    raise ContractError(f"gates.{gate_name} differs from review history")
            elif gate["status"] != "pending":
                raise ContractError(f"gates.{gate_name} has no current-round history entry")
    else:
        if repair_round != 0:
            raise ContractError("Planning without review history must use repair_round=0")
        if any(gate["status"] != "pending" for gate in gates.values()):
            raise ContractError("Non-pending gates require append-only review history")
    return history


def validate_graph_config(value: Any, task_ids: set[str]) -> None:
    graph = exact_keys(
        value,
        {
            "graph_id",
            "project_root",
            "integration_task_id",
            "global_budget",
            "previous_projection_sha256",
            "projection_sha256",
        },
        {"capacity_basis"},
        "graph",
    )
    if not isinstance(graph["graph_id"], str) or not GRAPH_ID_RE.fullmatch(graph["graph_id"]):
        raise ContractError("Invalid graph.graph_id")
    project_root = text_value(graph["project_root"], "graph.project_root", 1, 1024)
    if not os.path.isabs(project_root):
        raise ContractError("graph.project_root must be absolute")
    if graph["integration_task_id"] not in task_ids:
        raise ContractError("graph.integration_task_id must reference a task")
    validate_budget(graph["global_budget"], "graph.global_budget", global_budget=True)
    sha_value(
        graph["previous_projection_sha256"],
        "graph.previous_projection_sha256",
        nullable=True,
    )
    sha_value(graph["projection_sha256"], "graph.projection_sha256", nullable=True)
    if "capacity_basis" in graph:
        try:
            planning_checks.validate_capacity_basis(graph["capacity_basis"])
        except ValueError as exc:
            raise ContractError(str(exc)) from exc


def validate_owner_ceiling_field(value: Any, global_budget: dict[str, Any]) -> dict[str, Any]:
    try:
        ceiling = validate_owner_ceiling_value(value)
    except BudgetPoolError as exc:
        raise ContractError(str(exc)) from exc
    global_policy = global_budget.get("token_policy", "hard")
    owner_policy = ceiling.get("token_policy", "hard")
    if global_policy != owner_policy:
        raise ContractError("Graph and owner_ceiling token policies must agree")
    if global_budget.get("max_total_tokens") != ceiling["execution"]:
        raise ContractError(
            "graph.global_budget.max_total_tokens must equal owner_ceiling.execution"
        )
    return ceiling


def normalize_new_contract_defaults(contract: dict[str, Any]) -> dict[str, Any]:
    """Prepare only an unreviewed draft; never rebind somebody else's review."""
    if (contract.get("status") != "draft" or contract.get("revision") != 1
            or contract["planning"]["review_history"]
            or any(gate["status"] != "pending" for gate in contract["gates"].values())):
        raise ContractError("Estimate defaults require a new unreviewed draft")
    normalized = copy.deepcopy(contract)
    global_budget = normalized["graph"]["global_budget"]
    global_budget.setdefault("token_policy", "estimate")
    global_budget.setdefault("time_policy", "estimate")
    normalized["owner_ceiling"].setdefault(
        "token_policy", global_budget["token_policy"]
    )
    for task in normalized["tasks"]:
        task["budget"].setdefault("token_policy", global_budget["token_policy"])
        task["budget"].setdefault("tool_policy", "estimate")
    return normalized


def validate_amendment_field(value: Any, revision: int) -> None:
    if revision == 1:
        if value is not None:
            raise ContractError("Revision 1 must have amendment=null")
        return
    amendment = exact_keys(
        value,
        {"reason", "affected_task_ids", "authority", "approval_ref"},
        {"introduced_task_ids", "review_policy_migration"},
        "amendment",
    )
    text_value(amendment["reason"], "amendment.reason", 4, 2000)
    refs = string_list(amendment["affected_task_ids"], "amendment.affected_task_ids", 128)
    if not refs or any(not TASK_ID_RE.fullmatch(ref) for ref in refs):
        raise ContractError("amendment.affected_task_ids must contain task IDs")
    if "introduced_task_ids" in amendment:
        introduced = string_list(
            amendment["introduced_task_ids"], "amendment.introduced_task_ids", 128
        )
        if len(introduced) != len(set(introduced)) or any(
            not TASK_ID_RE.fullmatch(ref) for ref in introduced
        ):
            raise ContractError(
                "amendment.introduced_task_ids must contain unique task IDs"
            )
    if amendment["authority"] != "parent":
        raise ContractError(
            "amendment.authority must be parent; changed owner intent requires a new prompt contract"
        )
    text_value(amendment["approval_ref"], "amendment.approval_ref", 3, 512)


def _verify_policy_migration(contract: dict[str, Any]) -> None:
    migration = (contract.get('amendment') or {}).get('review_policy_migration')
    if migration is None:
        return
    migration = exact_keys(migration, {'version', 'authority', 'base_contract_hash', 'evidence'}, set(),
                           'amendment.review_policy_migration')
    if (migration['version'] != 'full-combined-migration-v1' or migration['authority'] != 'coordinator'
            or contract['revision'] < 2 or contract['planning'].get('review_policy') != 'full-combined-v1'
            or migration['base_contract_hash'] != contract['previous_contract_hash']):
        raise ContractError('Invalid full-combined policy migration authority/binding')
    evidence = exact_keys(migration['evidence'], {'path', 'sha256'}, set(), 'migration.evidence')
    text_value(evidence['path'], 'migration.evidence.path', 3, 4096)
    sha_value(evidence['sha256'], 'migration.evidence.sha256')
    source = Path(evidence['path'])
    if not source.is_absolute():
        raise ContractError('Migration evidence path must be absolute')
    try:
        if file_hash(source) != evidence['sha256']:
            raise ContractError('Migration authority evidence hash mismatch')
        authority = exact_keys(load_json(source, 'migration authority'),
            {'authority', 'reference', 'scope', 'contract_id', 'base_contract_hash',
             'target_revision', 'review_policy', 'candidate_sha256'}, set(), 'migration authority')
        text_value(authority['reference'], 'migration authority.reference', 3, 2000)
        # Exclude the authority descriptor itself to avoid a circular evidence hash.
        subject = planning_payload(contract)
        subject.pop('review_policy_migration', None)
        if (authority['authority'] != 'coordinator'
                or authority['scope'] != 'planning.review_policy:new-revision-opt-in'
                or authority['contract_id'] != contract['contract_id']
                or authority['base_contract_hash'] != contract['previous_contract_hash']
                or authority['target_revision'] != contract['revision']
                or authority['review_policy'] != 'full-combined-v1'
                or authority['candidate_sha256'] != sha256_value(subject)):
            raise ContractError('Migration authority does not bind this baseline/current candidate')
    except (OSError, UnicodeError) as exc:
        raise ContractError('Migration authority evidence is unreadable') from exc


def validate_contract(contract: dict[str, Any], phase: str = "any") -> dict[str, Any]:
    exact_keys(
        contract,
        {
            "schema_version",
            "contract_id",
            "revision",
            "previous_contract_hash",
            "status",
            "prompt",
            "classification",
            "clauses",
            "requirements",
            "questions",
            "assumptions",
            "out_of_scope",
            "decisions",
            "tasks",
            "gates",
            "planning",
            "approval",
            "owner_ceiling",
            "graph",
            "amendment",
            "closeout",
        },
        set(),
        "contract",
    )
    if contract["schema_version"] != SCHEMA_VERSION:
        raise ContractError("Unsupported contract.schema_version")
    contract_id = text_value(contract["contract_id"], "contract.contract_id", 6, 69)
    if not CONTRACT_ID_RE.fullmatch(contract_id):
        raise ContractError("Invalid contract.contract_id")
    revision = int_value(contract["revision"], "contract.revision", 1, 10000)
    if revision == 1:
        if contract["previous_contract_hash"] is not None:
            raise ContractError("Revision 1 requires previous_contract_hash=null")
    else:
        sha_value(contract["previous_contract_hash"], "contract.previous_contract_hash")
    if contract["status"] not in CONTRACT_STATUSES:
        raise ContractError("Unsupported contract.status")
    validate_prompt(contract["prompt"])
    validate_classification(contract["classification"])
    clauses = validate_clauses(contract["clauses"])
    requirements = validate_requirements(contract["requirements"])
    if not any(item["material"] for item in requirements.values()):
        raise ContractError("A persistent planning contract requires a material requirement")
    string_list(contract["questions"], "questions", 64, unique=False)
    validate_assumptions(contract["assumptions"])
    string_list(contract["out_of_scope"], "out_of_scope", 128, unique=False)
    decisions = validate_decisions(contract["decisions"])
    for decision in decisions:
        clause_id = decision["evidence_ref"]
        if clause_id not in clauses:
            raise ContractError(
                f"Decision {decision['decision_id']} must cite its clause ledger entry"
            )
        clause = clauses[clause_id]
        allowed_kinds = {"owner_decision"} if decision["authority"] in {"parent", "mateusz"} else {"owner_decision", "derived"}
        if clause["kind"] not in allowed_kinds or clause["text"] != decision["text"]:
            raise ContractError(
                f"Decision {decision['decision_id']} differs from its clause ledger entry"
            )
    tasks = validate_tasks(contract["tasks"], requirements, clauses)
    if (
        "instruction-conflict" in contract["classification"]["hard_risk_flags"]
        and not any(task["council_trigger"]["required"] for task in tasks.values())
    ):
        raise ContractError(
            "instruction-conflict requires at least one mandatory council trigger"
        )
    gates = exact_keys(contract["gates"], {"fidelity", "sufficiency"}, set(), "gates")
    fidelity = validate_gate(gates["fidelity"], "gates.fidelity")
    sufficiency = validate_gate(gates["sufficiency"], "gates.sufficiency")
    planning = exact_keys(
        contract["planning"],
        {
            "repair_round",
            "max_repair_rounds",
            "planner_identity",
            "planner_lineage",
            "planner_session_id",
            "review_history",
        },
        {"review_policy", "adapter_declarations", "preparation_policy"},
        "planning",
    )
    try:
        planning_checks.validate_preparation(contract)
    except ValueError as exc:
        raise ContractError(str(exc)) from exc
    if "review_policy" in planning and planning["review_policy"] != "full-combined-v1":
        raise ContractError("Unsupported planning.review_policy")
    if 'review_policy' in planning and contract['classification']['tier'] != 'full':
        raise ContractError('full-combined-v1 requires full tier')
    repair_round = int_value(planning["repair_round"], "planning.repair_round", 0, 2)
    if planning["max_repair_rounds"] != 2:
        raise ContractError("planning.max_repair_rounds must equal 2")
    for field in ("planner_identity", "planner_lineage", "planner_session_id"):
        text_value(planning[field], f"planning.{field}", 3, 256)
    validate_review_history(planning["review_history"], gates, repair_round, revision)
    if "adapter_declarations" in planning:
        try:
            planning_checks.validate_adapter_declarations(
                tasks, planning["adapter_declarations"]
            )
        except ValueError as exc:
            raise ContractError(str(exc)) from exc
    if (
        fidelity["status"] != "pending"
        and fidelity["attested_hard_risk_flags"]
        != contract["classification"]["hard_risk_flags"]
    ):
        raise ContractError(
            "Fidelity hard-risk attestation differs from classification.hard_risk_flags"
        )
    if sufficiency["attested_hard_risk_flags"]:
        raise ContractError("Sufficiency gate must not attest hard-risk flags")
    clause_ids = list(clauses)
    requirement_ids = list(requirements)
    task_ids = list(tasks)
    if fidelity["status"] != "pending":
        if fidelity["reviewed_clause_ids"] != clause_ids:
            raise ContractError("Fidelity gate must review every declared clause in order")
        if fidelity["reviewed_requirement_ids"] != requirement_ids:
            raise ContractError("Fidelity gate must review every requirement in order")
        if fidelity["reviewed_task_ids"]:
            raise ContractError("Fidelity gate does not claim task sufficiency coverage")
    if sufficiency["status"] != "pending":
        if sufficiency["reviewed_clause_ids"] != clause_ids:
            raise ContractError("Sufficiency gate must receive every declared clause in order")
        if sufficiency["reviewed_requirement_ids"] != requirement_ids:
            raise ContractError("Sufficiency gate must review every requirement in order")
        if sufficiency["reviewed_task_ids"] != task_ids:
            raise ContractError("Sufficiency gate must review every task in order")
    approval = exact_keys(
        contract["approval"],
        {"approved_by", "approved_at", "contract_hash"},
        {"planner_witness", "reviewer_witness"},
        "approval",
    )
    if "planner_witness" in approval:
        witness = approval["planner_witness"]
        if not isinstance(witness, dict) or witness.get("status") != "VERIFIED":
            raise ContractError("approval.planner_witness must be verified")
        if witness.get("prompt_sha256") != contract["prompt"]["sha256"] or witness.get("plan_sha256") != planning_payload_hash(contract):
            raise ContractError("Strong planner witness is stale")
    validate_graph_config(contract["graph"], set(tasks))
    validate_owner_ceiling_field(contract["owner_ceiling"], contract["graph"]["global_budget"])
    validate_execution_topology(tasks, contract["graph"]["integration_task_id"])
    previous_projection = contract["graph"]["previous_projection_sha256"]
    if revision == 1 and previous_projection is not None:
        raise ContractError("Revision 1 requires graph.previous_projection_sha256=null")
    if revision > 1 and previous_projection is None:
        raise ContractError("Revision >1 requires graph.previous_projection_sha256")
    validate_amendment_field(contract["amendment"], revision)
    _verify_policy_migration(contract)
    closeout = exact_keys(contract["closeout"], {"status", "receipt_sha256"}, set(), "closeout")
    if closeout["status"] not in {"pending", "complete", "blocked"}:
        raise ContractError("Unsupported closeout.status")
    sha_value(closeout["receipt_sha256"], "closeout.receipt_sha256", nullable=True)
    if phase == "draft":
        if contract["status"] != "draft":
            raise ContractError("Draft validation requires status=draft")
        if any(approval[field] is not None for field in ("approved_by", "approved_at", "contract_hash")):
            raise ContractError("Draft approval fields must be null")
    if phase == "approved" or contract["status"] in {"approved", "amended", "closed"}:
        if contract["status"] not in {"approved", "amended", "closed"}:
            raise ContractError("Approved validation requires approved/amended/closed status")
        if approval["approved_by"] != "parent-architect":
            raise ContractError("approval.approved_by must be parent-architect")
        text_value(approval["approved_at"], "approval.approved_at", 10, 80)
        expected_hash = canonical_contract_hash(contract)
        if approval["contract_hash"] != expected_hash:
            raise ContractError("approval.contract_hash does not match canonical contract")
        if contract["graph"]["projection_sha256"] is None:
            raise ContractError("Approved contract requires graph.projection_sha256")
        if planning.get('review_policy') == 'full-combined-v1':
            _verify_policy_evidence(contract)
    return {
        "contract_id": contract_id,
        "revision": revision,
        "status": contract["status"],
        "tier": contract["classification"]["tier"],
        "clause_count": len(clauses),
        "requirement_count": len(requirements),
        "task_count": len(tasks),
        "planning_payload_sha256": planning_payload_hash(contract),
    }


def resolve_relative(base_file: Path, relative: str) -> Path:
    root = base_file.resolve().parent
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ContractError(f"Path escapes contract directory: {relative}") from exc
    return candidate


def verify_bound_files(contract_path: Path, contract: dict[str, Any]) -> None:
    prompt_path = resolve_relative(contract_path, contract["prompt"]["source"])
    if prompt_hash(prompt_path) != contract["prompt"]["sha256"]:
        raise ContractError("prompt.sha256 does not match prompt.source")
    try:
        prompt_text = prompt_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ContractError(f"Cannot scan prompt hard risks {prompt_path}: {exc}") from exc
    detected_hard_risks = set(detect_hard_risk_flags(prompt_text))
    declared_hard_risks = set(contract["classification"]["hard_risk_flags"])
    missing_hard_risks = sorted(detected_hard_risks - declared_hard_risks)
    if missing_hard_risks:
        raise ContractError(
            "Prompt contains undeclared hard-risk flags: "
            + ", ".join(missing_hard_risks)
        )
    for source in contract["prompt"]["context_sources"]:
        if source["kind"] != "file":
            continue
        path = resolve_relative(contract_path, source["path"])
        if file_hash(path) != source["sha256"]:
            raise ContractError(f"Context source hash mismatch: {source['label']}")
    for gate_name in ("fidelity", "sufficiency"):
        gate = contract["gates"][gate_name]
        if gate["status"] == "pending":
            continue
        path = resolve_relative(contract_path, gate["evidence"]["path"])
        if file_hash(path) != gate["evidence"]["sha256"]:
            raise ContractError(f"Gate evidence hash mismatch: {gate_name}")


def verify_gate_approval(contract: dict[str, Any]) -> None:
    plan_hash = planning_payload_hash(contract)
    planner_lineage = contract["planning"]["planner_lineage"]
    planner_session = contract["planning"]["planner_session_id"]
    auditors: dict[str, dict[str, Any]] = {}
    repair_round = contract["planning"]["repair_round"]
    history = contract["planning"]["review_history"]
    current_rows = [
        item
        for item in history
        if item["revision"] == contract["revision"] and item["round"] == repair_round
    ]
    if len(current_rows) != 2:
        raise ContractError("Approval requires both append-only reviews in the current round")
    for gate_name in ("fidelity", "sufficiency"):
        gate = contract["gates"][gate_name]
        if gate["round"] != repair_round:
            raise ContractError(f"{gate_name} gate round differs from planning.repair_round")
        if gate["status"] != "pass":
            raise ContractError(f"Approval requires {gate_name} gate PASS")
        if gate["reviewed_prompt_sha256"] != contract["prompt"]["sha256"]:
            raise ContractError(f"{gate_name} gate reviewed a different prompt")
        if gate["reviewed_plan_sha256"] != plan_hash:
            raise ContractError(f"{gate_name} gate reviewed a different planning payload")
        auditor = gate["auditor"]
        auditors[gate_name] = auditor
        if auditor["lineage"] == planner_lineage or auditor["session_id"] == planner_session:
            raise ContractError(f"{gate_name} auditor is not independent from planner")
        if contract["planning"]["planner_identity"] not in auditor["independent_from"]:
            raise ContractError(f"{gate_name} auditor did not attest planner independence")
    if (
        contract["classification"]["tier"] == "full"
        and auditors["fidelity"]["lineage"] == auditors["sufficiency"]["lineage"]
    ):
        if contract["planning"].get("review_policy") != "full-combined-v1":
            raise ContractError("Full tier requires distinct fidelity and sufficiency lineages")
def compile_graph_value(contract: dict[str, Any]) -> dict[str, Any]:
    task_map = {task["task_id"]: task for task in contract["tasks"]}
    active_task_ids = {
        task_id for task_id, task in task_map.items() if task["status"] == "pending"
    }
    if contract["graph"]["integration_task_id"] not in active_task_ids:
        raise ContractError("Active graph is missing integration task")
    for task_id in sorted(active_task_ids):
        task = task_map[task_id]
        inactive_dependencies = sorted(
            set(task["depends_on"] + task["evidence_from"]) - active_task_ids
        )
        if inactive_dependencies:
            raise ContractError(
                f"Active task {task_id} references inactive tasks: {inactive_dependencies}"
            )
    nodes: list[dict[str, Any]] = []
    for task in task_map.values():
        node = {
                "node_id": task["graph_node_id"],
                "active": task["status"] == "pending",
                "kind": task["kind"],
                "capability": task["capability"],
                "title": task["title"],
                "depends_on": [task_map[ref]["graph_node_id"] for ref in task["depends_on"]],
                "evidence_from": [task_map[ref]["graph_node_id"] for ref in task["evidence_from"]],
                "executor": task["executor"],
                "reviewer": task["reviewer"],
                "workspace_mode": task["workspace_mode"],
                "read_scope": task["read_scope"],
                "write_scope": task["write_scope"],
                "resource_locks": task["resource_locks"],
                "acceptance_criteria": task["acceptance_criteria"],
                "budget": task["budget"],
                "repair_policy": task["repair_policy"],
                "loop_policy": task["loop_policy"],
                "council_trigger": task["council_trigger"],
            }
        for field in ("maker_lineage", "reviewer_lineage"):
            if field in task:
                node[field] = task[field]
        nodes.append(node)
    integration_node = task_map[contract["graph"]["integration_task_id"]]["graph_node_id"]
    return {
        "schema_version": 1,
        "graph_id": contract["graph"]["graph_id"],
        "revision": contract["revision"],
        "previous_revision_hash": contract["graph"]["previous_projection_sha256"],
        "intent": contract["prompt"]["summary"],
        "project_root": contract["graph"]["project_root"],
        "data_classification": contract["classification"]["data_classification"],
        "integration_node_id": integration_node,
        "global_budget": contract["graph"]["global_budget"],
        "nodes": nodes,
    }


def _verify_policy_evidence(contract: dict[str, Any]) -> None:
    witness = contract['approval'].get('planner_witness')
    if not isinstance(witness, dict):
        raise ContractError('full-combined-v1 requires a verified planner witness')
    try:
        verified = planner_witness.verify_descriptor(
            {key: witness[key] for key in ('kind', 'source', 'source_sha256')},
            contract['prompt']['sha256'], planning_payload_hash(contract))
        if verified != witness:
            raise ValueError('Stored planner witness differs from actual source')
        if (verified['session_id'] != contract['planning']['planner_session_id']
                or planner_witness.model_lineage(verified['model']) != contract['planning']['planner_lineage']):
            raise ValueError('Actual planner identity differs from planning metadata')
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ContractError('Strong planner evidence: ' + str(exc)) from exc
    _full_combined_projections(contract, contract['gates'], None)


def validate_approved_contract(contract: dict[str, Any]) -> None:
    validate_contract(contract, "approved")
    verify_gate_approval(contract)
    expected_projection = sha256_value(compile_graph_value(contract))
    if contract["graph"]["projection_sha256"] != expected_projection:
        raise ContractError("graph.projection_sha256 does not match deterministic projection")


def immutable_requirement_view(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item[key]
        for key in (
            "requirement_id",
            "kind",
            "text",
            "source_ref",
            "clause_refs",
            "material",
        )
    }


def validate_amendment(base: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    validate_approved_contract(base)
    if candidate.get("status") in {"approved", "amended"}:
        validate_approved_contract(candidate)
    else:
        validate_contract(candidate, "draft")
    if candidate["contract_id"] != base["contract_id"]:
        raise ContractError("Amendment changes contract_id")
    if candidate["revision"] != base["revision"] + 1:
        raise ContractError("Amendment revision must increment by one")
    if candidate["previous_contract_hash"] != base["approval"]["contract_hash"]:
        raise ContractError("Amendment previous_contract_hash does not match base")
    for field in ("source", "summary", "sha256"):
        if candidate["prompt"][field] != base["prompt"][field]:
            raise ContractError("Amendment cannot rewrite the baseline prompt")
    base_sources = base["prompt"]["context_sources"]
    candidate_sources = candidate["prompt"]["context_sources"]
    if candidate_sources[: len(base_sources)] != base_sources:
        raise ContractError(
            "Amendment may only append prompt.context_sources"
        )
    for field in (
        "classification",
        "clauses",
        "questions",
        "assumptions",
        "out_of_scope",
        "decisions",
    ):
        if candidate[field] != base[field]:
            raise ContractError(f"Amendment cannot rewrite baseline {field}")
    for field in ("graph_id", "project_root", "global_budget"):
        if candidate["graph"][field] != base["graph"][field]:
            raise ContractError(f"Amendment cannot rewrite graph.{field}")
    if candidate["owner_ceiling"] != base["owner_ceiling"]:
        raise ContractError("Amendment cannot rewrite owner_ceiling")
    if candidate["planning"].get("review_policy") != base["planning"].get("review_policy"):
        if (base['planning'].get('review_policy') is not None
                or candidate['planning'].get('review_policy') != 'full-combined-v1'
                or not candidate['amendment'].get('review_policy_migration')):
            raise ContractError("Amendment cannot silently migrate planning.review_policy")
    elif candidate['amendment'].get('review_policy_migration'):
        raise ContractError('Migration authority is valid only when opting in from legacy')
    if candidate["graph"]["previous_projection_sha256"] != base["graph"]["projection_sha256"]:
        raise ContractError("Amendment previous graph projection does not match base")
    if candidate["closeout"] != {"status": "pending", "receipt_sha256": None}:
        raise ContractError("Amendment must begin with a pending closeout")
    base_history = base["planning"]["review_history"]
    candidate_history = candidate["planning"]["review_history"]
    if candidate_history[: len(base_history)] != base_history:
        raise ContractError("Amendment must preserve append-only review history")
    new_reviews = candidate_history[len(base_history) :]
    if any(item["revision"] != candidate["revision"] for item in new_reviews):
        raise ContractError("Amendment review history may append only its current revision")
    historical_sessions = {
        item["auditor"]["session_id"]
        for item in base_history
        if item.get("auditor")
    }
    historical_evidence_paths = {
        item["evidence"]["path"]
        for item in base_history
        if item.get("evidence")
    }
    for item in new_reviews:
        if item["auditor"]["session_id"] in historical_sessions:
            raise ContractError("Amendment requires fresh auditor sessions")
        if item["evidence"]["path"] in historical_evidence_paths:
            raise ContractError("Amendment requires fresh gate evidence files")
    if candidate["planning"]["planner_session_id"] == base["planning"]["planner_session_id"]:
        raise ContractError("Amendment requires a fresh planner session")
    affected = set(candidate["amendment"]["affected_task_ids"])
    base_requirements = {item["requirement_id"]: item for item in base["requirements"]}
    candidate_requirements = {item["requirement_id"]: item for item in candidate["requirements"]}
    if set(candidate_requirements) != set(base_requirements):
        raise ContractError(
            "Amendment cannot add or remove requirements; changed owner intent needs a new contract"
        )
    for req_id, old in base_requirements.items():
        new = candidate_requirements[req_id]
        if immutable_requirement_view(old) != immutable_requirement_view(new):
            raise ContractError(f"Amendment changes baseline requirement: {req_id}")
        if not set(old["task_refs"]).issubset(new["task_refs"]):
            raise ContractError(f"Amendment removes task coverage from {req_id}")
    base_tasks = {item["task_id"]: item for item in base["tasks"]}
    candidate_tasks = {item["task_id"]: item for item in candidate["tasks"]}
    missing_tasks = sorted(set(base_tasks) - set(candidate_tasks))
    if missing_tasks:
        raise ContractError(f"Amendment removes tasks: {missing_tasks}")
    unknown_affected = sorted(affected - set(base_tasks))
    if unknown_affected:
        raise ContractError(
            f"Amendment references non-baseline affected tasks: {unknown_affected}"
        )
    for task_id, old in base_tasks.items():
        new = candidate_tasks[task_id]
        if task_id not in affected:
            old_without_criteria = {
                key: value for key, value in old.items() if key != "acceptance_criteria"
            }
            new_without_criteria = {
                key: value for key, value in new.items() if key != "acceptance_criteria"
            }
            old_criteria = old["acceptance_criteria"]
            new_criteria = new["acceptance_criteria"]
            if (
                old_without_criteria != new_without_criteria
                or new_criteria[: len(old_criteria)] != old_criteria
            ):
                raise ContractError(
                    "Amendment may only append acceptance_criteria without "
                    f"rewriting unaffected task: {task_id}"
                )
            continue
        if task_id in affected:
            old_without_status = {key: value for key, value in old.items() if key != "status"}
            new_without_status = {key: value for key, value in new.items() if key != "status"}
            if (
                old["status"] != "pending"
                or new["status"] != "blocked"
                or old_without_status != new_without_status
            ):
                raise ContractError(
                    f"Amendment must deactivate affected task without rewriting it: {task_id}"
                )
    introduced_task_ids = set(candidate_tasks) - set(base_tasks)
    declared_introduced = candidate["amendment"].get("introduced_task_ids")
    if declared_introduced is not None and set(declared_introduced) != introduced_task_ids:
        raise ContractError(
            "amendment.introduced_task_ids must exactly match appended candidate tasks"
        )
    if introduced_task_ids:
        max_old = max(numeric_id(identifier) for identifier in base_tasks)
        if min(numeric_id(identifier) for identifier in introduced_task_ids) <= max_old:
            raise ContractError("New task IDs must append after the baseline range")
    if any(
        base_requirements[req_id]["status"] != candidate_requirements[req_id]["status"]
        for req_id in base_requirements
    ):
        raise ContractError(
            "Amendment cannot change baseline requirement disposition; changed owner intent needs a new contract"
        )
    return {
        "status": "VALID_AMENDMENT",
        "contract_id": base["contract_id"],
        "base_revision": base["revision"],
        "candidate_revision": candidate["revision"],
        "affected_tasks": sorted(affected),
    }


def validate_closeout_input(value: dict[str, Any], contract: dict[str, Any], evidence_path: Path) -> tuple[list[dict[str, Any]], bool]:
    closeout = exact_keys(value, {"schema_version", "contract_id", "contract_hash", "requirements"}, set(), "closeout")
    if closeout["schema_version"] != 1:
        raise ContractError("Unsupported closeout schema_version")
    if closeout["contract_id"] != contract["contract_id"] or closeout["contract_hash"] != contract["approval"]["contract_hash"]:
        raise ContractError("Closeout is bound to a different contract")
    records = closeout["requirements"]
    if not isinstance(records, list) or len(records) > 256:
        raise ContractError("closeout.requirements must contain at most 256 entries")
    by_id: dict[str, dict[str, Any]] = {}
    for index, record in enumerate(records):
        label = f"closeout.requirements[{index}]"
        record = exact_keys(record, {"requirement_id", "disposition", "criterion_ids", "evidence", "blocker"}, set(), label)
        req_id = text_value(record["requirement_id"], f"{label}.requirement_id", 7, 16)
        if req_id in by_id:
            raise ContractError(f"Duplicate closeout requirement: {req_id}")
        if record["disposition"] not in {"satisfied", "blocked"}:
            raise ContractError(f"Unsupported {label}.disposition")
        criterion_ids = string_list(record["criterion_ids"], f"{label}.criterion_ids", 128)
        for criterion_id in criterion_ids:
            if not CRIT_ID_RE.fullmatch(criterion_id):
                raise ContractError(f"Invalid criterion in closeout: {criterion_id}")
        evidence = record["evidence"]
        if not isinstance(evidence, list) or len(evidence) > 128:
            raise ContractError(f"{label}.evidence must contain at most 128 entries")
        evidence_criteria: set[str] = set()
        for evidence_index, item in enumerate(evidence):
            item_label = f"{label}.evidence[{evidence_index}]"
            item = exact_keys(item, {"criterion_id", "path", "sha256"}, set(), item_label)
            criterion_id = text_value(
                item["criterion_id"], f"{item_label}.criterion_id", 4, 128
            )
            if not CRIT_ID_RE.fullmatch(criterion_id):
                raise ContractError(f"Invalid {item_label}.criterion_id")
            evidence_criteria.add(criterion_id)
            relative_path(item["path"], f"{item_label}.path")
            sha_value(item["sha256"], f"{item_label}.sha256")
            path = resolve_relative(evidence_path, item["path"])
            if file_hash(path) != item["sha256"]:
                raise ContractError(f"Closeout evidence hash mismatch: {item['path']}")
        if evidence_criteria != set(criterion_ids):
            raise ContractError(
                f"{label}.evidence must cover every cited criterion by identifier"
            )
        if record["blocker"] is not None:
            text_value(record["blocker"], f"{label}.blocker", 4, 1000)
        by_id[req_id] = record
    tasks = {task["task_id"]: task for task in contract["tasks"]}
    requirements = {item["requirement_id"]: item for item in contract["requirements"]}
    required_ids = {
        req_id
        for req_id, requirement in requirements.items()
        if requirement["material"] and requirement["status"] != "not_applicable"
    }
    missing = sorted(required_ids - set(by_id))
    extra = sorted(set(by_id) - set(requirements))
    if missing or extra:
        raise ContractError(f"Closeout coverage mismatch: missing={missing}; extra={extra}")
    any_blocked = False
    for req_id in sorted(required_ids):
        requirement = requirements[req_id]
        record = by_id[req_id]
        valid_criteria = {
            criterion["criterion_id"]
            for task_id in requirement["task_refs"]
            if tasks[task_id]["status"] == "pending"
            for criterion in tasks[task_id]["acceptance_criteria"]
        }
        if not set(record["criterion_ids"]).issubset(valid_criteria):
            raise ContractError(f"Closeout cites criteria not linked to {req_id}")
        if record["disposition"] == "satisfied":
            if not record["criterion_ids"] or not record["evidence"] or record["blocker"] is not None:
                raise ContractError(f"Satisfied requirement needs criteria and evidence: {req_id}")
        elif record["disposition"] == "blocked":
            any_blocked = True
            if not record["criterion_ids"] or not record["blocker"] or not record["evidence"]:
                raise ContractError(
                    f"Blocked requirement needs criteria, blocker and evidence: {req_id}"
                )
    return [by_id[req_id] for req_id in sorted(by_id)], any_blocked


def validate_closeout_receipt(
    contract: dict[str, Any], receipt: dict[str, Any], receipt_path: Path
) -> None:
    validate_approved_contract(contract)
    receipt = exact_keys(
        receipt,
        {
            "schema_version",
            "contract_id",
            "contract_hash",
            "status",
            "closed_at",
            "requirements",
            "receipt_hash",
        },
        set(),
        "closeout receipt",
    )
    if receipt["schema_version"] != 1:
        raise ContractError("Unsupported closeout receipt schema_version")
    if receipt["contract_id"] != contract["contract_id"]:
        raise ContractError("Closeout receipt contract_id mismatch")
    if receipt["contract_hash"] != contract["approval"]["contract_hash"]:
        raise ContractError("Closeout receipt contract_hash mismatch")
    if receipt["status"] != "COMPLETE":
        raise ContractError("Agent Graph close requires a COMPLETE planning receipt")
    text_value(receipt["closed_at"], "closeout receipt.closed_at", 10, 80)
    if not isinstance(receipt["requirements"], list):
        raise ContractError("closeout receipt.requirements must be an array")
    expected_hash = sha256_value({**receipt, "receipt_hash": None})
    if receipt["receipt_hash"] != expected_hash:
        raise ContractError("Closeout receipt hash mismatch")
    closeout_input = {
        "schema_version": receipt["schema_version"],
        "contract_id": receipt["contract_id"],
        "contract_hash": receipt["contract_hash"],
        "requirements": receipt["requirements"],
    }
    _, any_blocked = validate_closeout_input(closeout_input, contract, receipt_path)
    if any_blocked:
        raise ContractError("Agent Graph close requires every material requirement satisfied")


def command_doctor(_: argparse.Namespace) -> dict[str, Any]:
    return {
        "status": "READY",
        "version": VERSION,
        "schema_version": SCHEMA_VERSION,
        "python": sys.version.split()[0],
        "network_required": False,
        "provider_runtime": False,
        "owner_ceiling": True,
        "unknown_usage_policy": "preserve_unknown",
    }


def command_reconcile_budget(args: argparse.Namespace) -> dict[str, Any]:
    source = load_json(Path(args.contract).resolve(), "planning contract")
    if "owner_ceiling" in source:
        validate_contract(source, "any")
        ceiling = source["owner_ceiling"]
    else:
        ceiling = source
    runs_payload = load_json(Path(args.runs).resolve(), "run ledger")
    records = runs_payload.get("runs", runs_payload if isinstance(runs_payload, list) else None)
    if not isinstance(records, list):
        raise ContractError("Run ledger must be an array or an object with a runs array")
    try:
        report = reconcile_runs(ceiling, records)
    except BudgetPoolError as exc:
        raise ContractError(str(exc)) from exc
    output = Path(args.output).resolve()
    write_new_json(output, report)
    markdown_path = None
    if args.markdown:
        markdown_path = Path(args.markdown).resolve()
        write_new_json_text(markdown_path, render_reconciliation_markdown(report))
    return {
        "status": "RECONCILED",
        "output": str(output),
        "markdown": str(markdown_path) if markdown_path else None,
        "planning_runs": report["pools"]["planning_and_review"]["run_count"],
        "execution_runs": report["pools"]["execution"]["run_count"],
        "planning_attested_tokens": report["pools"]["planning_and_review"]["attested_tokens"],
        "execution_attested_tokens": report["pools"]["execution"]["attested_tokens"],
        "writes_performed": 1 + (1 if markdown_path else 0),
    }


def write_new_json_text(path: Path, text: str) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ContractError(f"Refusing to overwrite immutable output: {path}") from exc
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            path.unlink(missing_ok=True)
        finally:
            raise


def command_hash_prompt(args: argparse.Namespace) -> dict[str, Any]:
    path = Path(args.prompt).resolve()
    return {"status": "HASHED", "path": str(path), "sha256": prompt_hash(path)}


def command_validate(args: argparse.Namespace) -> dict[str, Any]:
    path = Path(args.contract).resolve()
    contract = load_json(path, "contract")
    if args.phase == "approved":
        validate_approved_contract(contract)
        verify_bound_files(path, contract)
        summary = validate_contract(contract, "approved")
    else:
        summary = validate_contract(contract, args.phase)
    return {"status": "VALID", **summary}


def _compact_combined_projections(
    contract: dict[str, Any], gate: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """Expand one compact review into the two legacy gate projections."""
    if contract["classification"]["tier"] != "compact":
        raise ContractError("review.gate=combined is allowed only for compact tier")
    if contract["classification"]["hard_risk_flags"]:
        raise ContractError("Compact combined review cannot attest hard-risk flags")
    if any(
        contract["gates"][name]["status"] != "pending"
        for name in ("fidelity", "sufficiency")
    ):
        raise ContractError("Combined review requires both compact gates to be pending")
    expected_clauses = [item["clause_id"] for item in contract["clauses"]]
    expected_requirements = [item["requirement_id"] for item in contract["requirements"]]
    expected_tasks = [item["task_id"] for item in contract["tasks"]]
    if gate["reviewed_clause_ids"] != expected_clauses:
        raise ContractError("Combined review must cover every declared clause in order")
    if gate["reviewed_requirement_ids"] != expected_requirements:
        raise ContractError(
            "Combined review must cover every declared requirement in order"
        )
    if gate["reviewed_task_ids"] != expected_tasks:
        raise ContractError("Combined review must cover every declared task in order")
    auditor = gate["auditor"]
    planning = contract["planning"]
    if (
        auditor["lineage"] == planning["planner_lineage"]
        or auditor["session_id"] == planning["planner_session_id"]
        or planning["planner_identity"] not in auditor["independent_from"]
    ):
        raise ContractError("Combined reviewer is not independent from planner")
    fidelity = copy.deepcopy(gate)
    fidelity["reviewed_task_ids"] = []
    return {"fidelity": fidelity, "sufficiency": copy.deepcopy(gate)}


def _full_combined_projections(
    contract: dict[str, Any], result: dict[str, Any], contract_path: Path | None
) -> dict[str, dict[str, Any]]:
    if contract["classification"]["tier"] != "full" or contract["planning"].get("review_policy") != "full-combined-v1":
        raise ContractError("full combined review requires planning.review_policy=full-combined-v1")
    if not isinstance(result, dict) or set(result) != {"fidelity", "sufficiency"}:
        raise ContractError("full-combined-v1 must attest fidelity and sufficiency separately")
    projections = {name: validate_gate(result[name], f"review.result.{name}") for name in result}
    expected = {
        "clauses": [item["clause_id"] for item in contract["clauses"]],
        "requirements": [item["requirement_id"] for item in contract["requirements"]],
        "tasks": [item["task_id"] for item in contract["tasks"]],
    }
    fidelity, sufficiency = projections["fidelity"], projections["sufficiency"]
    if any(gate['round'] != contract['planning']['repair_round'] for gate in projections.values()):
        raise ContractError('Both review rounds must equal planning.repair_round')
    if fidelity["status"] != "pass" or sufficiency["status"] != "pass":
        raise ContractError("full-combined-v1 requires separate fidelity and sufficiency PASS")
    if fidelity["reviewed_clause_ids"] != expected["clauses"] or sufficiency["reviewed_clause_ids"] != expected["clauses"]:
        raise ContractError("Full combined review must cover every declared clause in order")
    if fidelity["reviewed_requirement_ids"] != expected["requirements"] or sufficiency["reviewed_requirement_ids"] != expected["requirements"]:
        raise ContractError("Full combined review must cover every requirement in order")
    if fidelity["reviewed_task_ids"] or sufficiency["reviewed_task_ids"] != expected["tasks"]:
        raise ContractError("Full combined review must project complete task coverage")
    if fidelity["attested_hard_risk_flags"] != contract["classification"]["hard_risk_flags"] or sufficiency["attested_hard_risk_flags"]:
        raise ContractError("Full combined review has incorrect hard-risk projection")
    for field in ("reviewed_prompt_sha256", "reviewed_plan_sha256"):
        if fidelity[field] != sufficiency[field]:
            raise ContractError("Full combined review projections use different hashes")
    if fidelity["reviewed_prompt_sha256"] != contract["prompt"]["sha256"] or fidelity["reviewed_plan_sha256"] != planning_payload_hash(contract):
        raise ContractError("Review is bound to another prompt or planning payload")
    if any(fidelity[key] != sufficiency[key] for key in ("auditor", "evidence")):
        raise ContractError("Full combined review must use one complete bound reviewer receipt")
    auditor = fidelity["auditor"]
    planning = contract["planning"]
    if (auditor['identity'] == planning['planner_identity']
            or auditor["lineage"].casefold() == planning["planner_lineage"].casefold()
            or auditor["session_id"] == planning["planner_session_id"]
            or planning["planner_identity"] not in auditor["independent_from"]):
        raise ContractError("Combined reviewer is not independent from planner")
    if contract_path is not None:
        evidence_path = resolve_relative(contract_path, fidelity['evidence']['path'])
    else:
        bound = exact_keys(contract['approval'].get('reviewer_witness'),
                           {'source', 'source_sha256', 'evidence_path'}, set(), 'approval.reviewer_witness')
        text_value(bound['source'], 'reviewer_witness.source', 3, 4096)
        evidence_path = Path(bound['source'])
        if (not evidence_path.is_absolute() or bound['source_sha256'] != fidelity['evidence']['sha256']
                or bound['evidence_path'] != fidelity['evidence']['path']):
            raise ContractError('Stored reviewer evidence differs from gate binding')
    try:
        planner_witness.verify_reviewer(evidence_path, fidelity['evidence']['sha256'],
                                       fidelity['reviewed_prompt_sha256'], fidelity['reviewed_plan_sha256'], auditor)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ContractError('Full combined reviewer evidence: ' + str(exc)) from exc
    return projections


def command_record_review(args: argparse.Namespace) -> dict[str, Any]:
    contract_path = Path(args.contract).resolve()
    output = Path(args.output).resolve()
    if output.parent != contract_path.parent:
        raise ContractError("record-review output must stay beside its contract evidence")
    contract = load_json(contract_path, "contract")
    validate_contract(contract, "draft")
    review = load_json(Path(args.review).resolve(), "review")
    review = exact_keys(review, {"gate", "result"}, set(), "review")
    gate_name = review["gate"]
    if gate_name not in {"fidelity", "sufficiency", "combined"}:
        raise ContractError("review.gate must be fidelity, sufficiency, or combined")
    if gate_name == "combined" and contract["classification"]["tier"] == "full":
        gate = None
    else:
        gate = validate_gate(review["result"], "review.result")
        if gate["status"] == "pending":
            raise ContractError("record-review requires pass, repair, or blocked")
    current_round = contract["planning"]["repair_round"]
    if gate_name == "combined" and gate is None:
        projections = _full_combined_projections(contract, review['result'], contract_path)
        if any(item['status'] != 'pending' for item in contract['gates'].values()):
            raise ContractError('Combined review requires both gates to be pending')
        review_round = projections['fidelity']['round']
    else:
        review_round = gate["round"]
    if review_round != current_round:
        raise ContractError("Review round differs from planning.repair_round")
    if gate_name == "combined":
        if contract["classification"]["tier"] == "compact":
            projections = _compact_combined_projections(contract, gate)
        gate_for_binding = gate or projections["fidelity"]
        if gate_for_binding["reviewed_prompt_sha256"] != contract["prompt"]["sha256"]:
            raise ContractError("Review is bound to another prompt")
        if gate_for_binding["reviewed_plan_sha256"] != planning_payload_hash(contract):
            raise ContractError("Review is bound to another planning payload")
        result = copy.deepcopy(contract)
        for projection_name in ("fidelity", "sufficiency"):
            projection = projections[projection_name]
            result["gates"][projection_name] = projection
            result["planning"]["review_history"].append(
                {
                    "revision": contract["revision"],
                    "gate": projection_name,
                    **copy.deepcopy(projection),
                }
            )
        validate_contract(result, "draft")
        verify_bound_files(contract_path, result)
        write_new_json(output, result)
        return {
            "status": "REVIEW_RECORDED",
            "gate": "combined",
            "projections": ["fidelity", "sufficiency"],
            "verdict": projections["fidelity"]["status"],
            "round": current_round,
            "output": str(output),
            "writes_performed": 1,
        }
    if contract["gates"][gate_name]["status"] != "pending":
        raise ContractError(f"Current {gate_name} review is already recorded")
    if gate["reviewed_prompt_sha256"] != contract["prompt"]["sha256"]:
        raise ContractError("Review is bound to another prompt")
    if gate["reviewed_plan_sha256"] != planning_payload_hash(contract):
        raise ContractError("Review is bound to another planning payload")
    result = copy.deepcopy(contract)
    result["gates"][gate_name] = gate
    result["planning"]["review_history"].append(
        {
            "revision": contract["revision"],
            "gate": gate_name,
            **copy.deepcopy(gate),
        }
    )
    validate_contract(result, "draft")
    verify_bound_files(contract_path, result)
    write_new_json(output, result)
    return {
        "status": "REVIEW_RECORDED",
        "gate": gate_name,
        "verdict": gate["status"],
        "round": current_round,
        "output": str(output),
        "writes_performed": 1,
    }


def command_prepare_draft(args: argparse.Namespace) -> dict[str, Any]:
    contract_path = Path(args.contract).resolve()
    output = Path(args.output).resolve()
    if output.parent != contract_path.parent:
        raise ContractError("prepare-draft output must stay beside its evidence")
    draft = normalize_new_contract_defaults(load_json(contract_path, "contract"))
    policy = draft["planning"].get("preparation_policy", planning_checks.PREPARATION_POLICY)
    if policy != planning_checks.PREPARATION_POLICY:
        raise ContractError("Unsupported planning.preparation_policy")
    draft["planning"]["preparation_policy"] = policy
    validate_contract(draft, "draft")
    verify_bound_files(contract_path, draft)
    write_new_json(output, draft)
    return {"status": "DRAFT_PREPARED", "output": str(output), "writes_performed": 1}


def command_advance_repair(args: argparse.Namespace) -> dict[str, Any]:
    base_path = Path(args.contract).resolve()
    candidate_path = Path(args.candidate).resolve()
    output = Path(args.output).resolve()
    if output.parent != candidate_path.parent:
        raise ContractError("advance-repair output must stay beside candidate evidence")
    base = load_json(base_path, "repair base contract")
    candidate = load_json(candidate_path, "repair candidate contract")
    validate_contract(base, "draft")
    current_round = base["planning"]["repair_round"]
    current_rows = [
        item
        for item in base["planning"]["review_history"]
        if item["revision"] == base["revision"] and item["round"] == current_round
    ]
    if len(current_rows) != 2:
        raise ContractError("advance-repair requires two current reviews")

    def persist_blocked(reason: str) -> dict[str, Any]:
        blocked = copy.deepcopy(base)
        blocked["status"] = "blocked"
        validate_contract(blocked, "any")
        write_new_json(output, blocked)
        return {
            "status": "BLOCKED",
            "round": current_round,
            "reason": reason,
            "output": str(output),
            "writes_performed": 1,
            "_exit_code": 3,
        }

    if any(item["status"] == "blocked" for item in current_rows):
        return persist_blocked("review-blocked")
    if not any(item["status"] == "repair" for item in current_rows):
        raise ContractError("advance-repair requires at least one REPAIR verdict")
    if current_round >= base["planning"]["max_repair_rounds"]:
        return persist_blocked("repair-budget-exhausted")
    if candidate["contract_id"] != base["contract_id"] or candidate["revision"] != base["revision"]:
        raise ContractError("Repair candidate must preserve contract identity and revision")
    for field in ("source", "summary", "sha256"):
        if candidate["prompt"][field] != base["prompt"][field]:
            raise ContractError("Repair candidate cannot rewrite the bound prompt")
    base_sources = base["prompt"]["context_sources"]
    candidate_sources = candidate["prompt"]["context_sources"]
    if candidate_sources[: len(base_sources)] != base_sources:
        raise ContractError(
            "Repair candidate may only append prompt.context_sources"
        )
    if candidate["planning"]["review_history"] != base["planning"]["review_history"]:
        raise ContractError("Repair candidate must preserve append-only review history")
    if candidate["planning"]["repair_round"] != current_round + 1:
        raise ContractError("Repair candidate must increment planning.repair_round by one")
    if any(gate["status"] != "pending" for gate in candidate["gates"].values()):
        raise ContractError("A new repair round must begin with pending gates")
    if any(gate["round"] != current_round + 1 for gate in candidate["gates"].values()):
        raise ContractError("A new repair round must bind both gates to the new round")
    validate_contract(candidate, "draft")
    verify_bound_files(candidate_path, candidate)
    write_new_json(output, candidate)
    return {
        "status": "REPAIR_ADVANCED",
        "from_round": current_round,
        "to_round": current_round + 1,
        "output": str(output),
        "writes_performed": 1,
    }


def command_approve(args: argparse.Namespace) -> dict[str, Any]:
    contract_path = Path(args.contract).resolve()
    contract = load_json(contract_path, "contract")
    validate_contract(contract, "draft")
    verify_bound_files(contract_path, contract)
    verify_gate_approval(contract)
    approved = copy.deepcopy(contract)
    witness_path = getattr(args, "planner_evidence", None)
    new_policy = contract["planning"].get("review_policy") == "full-combined-v1"
    if new_policy:
        _full_combined_projections(contract, contract['gates'], contract_path)
        evidence = contract['gates']['fidelity']['evidence']
        approved['approval']['reviewer_witness'] = {
            'source': str(resolve_relative(contract_path, evidence['path'])),
            'source_sha256': evidence['sha256'], 'evidence_path': evidence['path']}
    if contract["classification"]["tier"] == "full" and (getattr(args, "require_planner_witness", False) or new_policy) and not witness_path:
        raise ContractError("Full CLI approval requires --planner-evidence from an actual qualified planner; reuse the native strong parent or one bounded architect review")
    if witness_path:
        try:
            approved["approval"]["planner_witness"] = planner_witness.verify(
                witness_path, contract["prompt"]["sha256"], planning_payload_hash(contract))
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ContractError("Strong planner evidence: " + str(exc)) from exc
    approved["status"] = "approved" if approved["revision"] == 1 else "amended"
    approved["graph"]["projection_sha256"] = sha256_value(compile_graph_value(approved))
    if args.approved_by != "parent-architect":
        raise ContractError("approve --approved-by must be parent-architect")
    approved["approval"]["approved_by"] = args.approved_by
    approved["approval"]["approved_at"] = utc_now()
    approved["approval"]["contract_hash"] = canonical_contract_hash(approved)
    validate_approved_contract(approved)
    base_contract_arg = getattr(args, "base_contract", None)
    if approved["revision"] == 1:
        if base_contract_arg:
            raise ContractError("Revision 1 approval must not provide --base-contract")
    else:
        if not base_contract_arg:
            raise ContractError("Amendment approval requires --base-contract")
        base = load_json(Path(base_contract_arg).resolve(), "base contract")
        validate_amendment(base, approved)
    output = Path(args.output).resolve()
    write_new_json(output, approved)
    return {
        "status": "APPROVED",
        "contract_id": approved["contract_id"],
        "revision": approved["revision"],
        "contract_hash": approved["approval"]["contract_hash"],
        "graph_projection_sha256": approved["graph"]["projection_sha256"],
        "output": str(output),
        "writes_performed": 1,
    }


def command_compile_graph(args: argparse.Namespace) -> dict[str, Any]:
    contract = load_json(Path(args.contract).resolve(), "contract")
    validate_approved_contract(contract)
    graph = compile_graph_value(contract)
    graph_hash = sha256_value(graph)
    if graph_hash != contract["graph"]["projection_sha256"]:
        raise ContractError("Compiled graph does not match approved projection hash")
    output = Path(args.output).resolve()
    write_new_json(output, graph)
    return {"status": "COMPILED", "graph_id": graph["graph_id"], "graph_hash": graph_hash, "output": str(output), "writes_performed": 1}


def command_verify_graph(args: argparse.Namespace) -> dict[str, Any]:
    contract = load_json(Path(args.contract).resolve(), "contract")
    graph = load_json(Path(args.graph).resolve(), "graph")
    validate_approved_contract(contract)
    expected = compile_graph_value(contract)
    if canonical_bytes(graph) != canonical_bytes(expected):
        raise ContractError("Graph differs from deterministic contract projection")
    graph_hash = sha256_value(graph)
    if graph_hash != contract["graph"]["projection_sha256"]:
        raise ContractError("Graph hash differs from approved projection hash")
    return {"status": "VERIFIED", "graph_id": graph["graph_id"], "graph_hash": graph_hash, "writes_performed": 0}


def command_validate_amendment(args: argparse.Namespace) -> dict[str, Any]:
    base = load_json(Path(args.base).resolve(), "base contract")
    candidate = load_json(Path(args.candidate).resolve(), "candidate contract")
    return validate_amendment(base, candidate)


def command_prepare_dispatch(args: argparse.Namespace) -> dict[str, Any]:
    contract_path = Path(args.contract).resolve()
    output = Path(args.output).resolve()
    if output.parent != contract_path.parent:
        raise ContractError("prepare-dispatch output must stay beside its contract")
    contract = load_json(contract_path, "contract")
    validate_contract(contract, "any")
    declarations = load_json(Path(args.adapters).resolve(), "adapter declarations")
    result = prepare_dispatch(contract, declarations)
    write_new_json(output, result)
    return {"status": "DISPATCH_PREPARED", "output": str(output), "writes_performed": 1}


def command_report_parallelism(args: argparse.Namespace) -> dict[str, Any]:
    contract = load_json(Path(args.contract).resolve(), "contract")
    return report_parallelism(contract)


def command_closeout(args: argparse.Namespace) -> dict[str, Any]:
    contract = load_json(Path(args.contract).resolve(), "contract")
    validate_approved_contract(contract)
    evidence_path = Path(args.evidence).resolve()
    output = Path(args.output).resolve()
    if output.parent != evidence_path.parent:
        raise ContractError("closeout output must stay beside its evidence files")
    evidence = load_json(evidence_path, "closeout evidence")
    records, any_blocked = validate_closeout_input(evidence, contract, evidence_path)
    receipt = {
        "schema_version": 1,
        "contract_id": contract["contract_id"],
        "contract_hash": contract["approval"]["contract_hash"],
        "status": "BLOCKED" if any_blocked else "COMPLETE",
        "closed_at": utc_now(),
        "requirements": records,
        "receipt_hash": None,
    }
    receipt["receipt_hash"] = sha256_value({**receipt, "receipt_hash": None})
    write_new_json(output, receipt)
    result = {
        "status": receipt["status"],
        "contract_id": receipt["contract_id"],
        "receipt_hash": receipt["receipt_hash"],
        "output": str(output),
        "writes_performed": 1,
    }
    if any_blocked:
        result["_exit_code"] = 3
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate immutable planning contracts and compile Agent Graph V1 projections.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor")
    doctor.set_defaults(handler=command_doctor)

    hash_prompt_parser = subparsers.add_parser("hash-prompt")
    hash_prompt_parser.add_argument("--prompt", required=True)
    hash_prompt_parser.set_defaults(handler=command_hash_prompt)

    validate = subparsers.add_parser("validate")
    validate.add_argument("--contract", required=True)
    validate.add_argument("--phase", choices=("any", "draft", "approved"), default="any")
    validate.set_defaults(handler=command_validate)

    record_review = subparsers.add_parser("record-review")
    record_review.add_argument("--contract", required=True)
    record_review.add_argument("--review", required=True)
    record_review.add_argument("--output", required=True)
    record_review.set_defaults(handler=command_record_review)

    advance_repair = subparsers.add_parser("advance-repair")
    advance_repair.add_argument("--contract", required=True)
    advance_repair.add_argument("--candidate", required=True)
    advance_repair.add_argument("--output", required=True)
    advance_repair.set_defaults(handler=command_advance_repair)

    prepare = subparsers.add_parser("prepare-draft")
    prepare.add_argument("--contract", required=True)
    prepare.add_argument("--output", required=True)
    prepare.set_defaults(handler=command_prepare_draft)

    approve = subparsers.add_parser("approve")
    approve.add_argument("--contract", required=True)
    approve.add_argument("--output", required=True)
    approve.add_argument("--approved-by", required=True)
    approve.add_argument("--base-contract")
    approve.add_argument("--planner-evidence", help="Actual Fleet/native qualified-planner witness for a full plan")
    approve.set_defaults(handler=command_approve, require_planner_witness=True)

    compile_graph = subparsers.add_parser("compile-graph")
    compile_graph.add_argument("--contract", required=True)
    compile_graph.add_argument("--output", required=True)
    compile_graph.set_defaults(handler=command_compile_graph)

    verify_graph = subparsers.add_parser("verify-graph")
    verify_graph.add_argument("--contract", required=True)
    verify_graph.add_argument("--graph", required=True)
    verify_graph.set_defaults(handler=command_verify_graph)

    amendment = subparsers.add_parser("validate-amendment")
    amendment.add_argument("--base", required=True)
    amendment.add_argument("--candidate", required=True)
    amendment.set_defaults(handler=command_validate_amendment)

    dispatch = subparsers.add_parser("prepare-dispatch")
    dispatch.add_argument("--contract", required=True)
    dispatch.add_argument("--adapters", required=True)
    dispatch.add_argument("--output", required=True)
    dispatch.set_defaults(handler=command_prepare_dispatch)

    parallelism = subparsers.add_parser("report-parallelism")
    parallelism.add_argument("--contract", required=True)
    parallelism.set_defaults(handler=command_report_parallelism)

    closeout = subparsers.add_parser("closeout")
    closeout.add_argument("--contract", required=True)
    closeout.add_argument("--evidence", required=True)
    closeout.add_argument("--output", required=True)
    closeout.set_defaults(handler=command_closeout)

    reconcile = subparsers.add_parser("reconcile-budget")
    reconcile.add_argument("--contract", required=True)
    reconcile.add_argument("--runs", required=True)
    reconcile.add_argument("--output", required=True)
    reconcile.add_argument("--markdown")
    reconcile.set_defaults(handler=command_reconcile_budget)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        result = args.handler(args)
        exit_code = int(result.pop("_exit_code", 0))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return exit_code
    except ContractError as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
