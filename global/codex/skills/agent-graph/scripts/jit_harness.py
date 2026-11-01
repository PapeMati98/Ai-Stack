#!/usr/bin/env python3
"""Validate narrowing-only JIT node harnesses and emit shadow receipts."""

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
REPAIR_STRICTNESS = {"block": 0, "static-fallback": 1, "bounded-repair": 2}


class JitError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def canonical_bytes(value: Any) -> bytes:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
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
    if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
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
        raise JitError("schema-array", f"{label} must contain {minimum}..{maximum} entries")
    result: list[str] = []
    for index, item in enumerate(value):
        text = text_value(item, f"{label}[{index}]", 1, 500)
        if pattern and not pattern.fullmatch(text):
            raise JitError("schema-identifier", f"Invalid identifier in {label}")
        result.append(text)
    if len(result) != len(set(result)):
        raise JitError("schema-duplicate", f"{label} must contain unique entries")
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


def validate_budget(value: Any, label: str) -> dict[str, int]:
    budget = exact_keys(value, {"max_wall_seconds", "max_tool_calls", "max_tokens", "max_attempts"}, label)
    return {
        "max_wall_seconds": int_value(budget["max_wall_seconds"], f"{label}.max_wall_seconds", 1, 604800),
        "max_tool_calls": int_value(budget["max_tool_calls"], f"{label}.max_tool_calls", 1, 100000),
        "max_tokens": int_value(budget["max_tokens"], f"{label}.max_tokens", 1, 100000000),
        "max_attempts": int_value(budget["max_attempts"], f"{label}.max_attempts", 1, 3),
    }


def validate_repair(value: Any, label: str) -> dict[str, Any]:
    repair = exact_keys(value, {"on_reject", "max_repairs"}, label)
    if repair["on_reject"] not in REPAIR_STRICTNESS:
        raise JitError("schema-repair", f"Unsupported {label}.on_reject")
    return {
        "on_reject": repair["on_reject"],
        "max_repairs": int_value(repair["max_repairs"], f"{label}.max_repairs", 0, 2),
    }


def validate_reviewer(value: Any, label: str) -> dict[str, Any]:
    reviewer = exact_keys(value, {"different_provider_lineage", "required_capabilities"}, label)
    if reviewer["different_provider_lineage"] is not True:
        raise JitError("reviewer-lineage", "Reviewer must use a different provider lineage")
    return {
        "different_provider_lineage": True,
        "required_capabilities": string_list(reviewer["required_capabilities"], f"{label}.required_capabilities", minimum=1, maximum=16),
    }


def validate_static(value: Any) -> dict[str, Any]:
    static = exact_keys(
        value,
        {
            "schema_version",
            "contract_id",
            "planning_contract_sha256",
            "graph_sha256",
            "node_id",
            "static_template_id",
            "pilot_mode",
            "control_veto",
            "objective",
            "capability",
            "required_context_refs",
            "allowed_context_refs",
            "skill_allowlist",
            "tool_allowlist",
            "read_scope",
            "write_scope",
            "budget",
            "stop_conditions",
            "criterion_refs",
            "repair_policy",
            "reviewer_capabilities",
            "maker_provider_lineage",
            "reviewer_provider_lineage",
        },
        "static_contract",
    )
    if static["schema_version"] != SCHEMA_VERSION:
        raise JitError("schema-version", "Unsupported static contract schema_version")
    if static["pilot_mode"] != "shadow-only":
        raise JitError("active-mode-forbidden", "Only shadow-only pilot mode is supported")
    node_id = text_value(static["node_id"], "static_contract.node_id", 5, 105)
    if not re.fullmatch(r"node-[a-z0-9][a-z0-9-]{2,100}", node_id):
        raise JitError("schema-identifier", "Invalid static_contract.node_id")
    required_context = validate_context_refs(static["required_context_refs"], "static_contract.required_context_refs")
    allowed_context = validate_context_refs(static["allowed_context_refs"], "static_contract.allowed_context_refs")
    required_ids = {(item["path"], item["sha256"]) for item in required_context}
    allowed_ids = {(item["path"], item["sha256"]) for item in allowed_context}
    if not required_ids <= allowed_ids:
        raise JitError("static-context", "Required context exceeds allowed context")
    maker = text_value(static["maker_provider_lineage"], "static_contract.maker_provider_lineage", 2, 100)
    reviewer = text_value(static["reviewer_provider_lineage"], "static_contract.reviewer_provider_lineage", 2, 100)
    if maker.casefold() == reviewer.casefold():
        raise JitError("static-lineage", "Maker and reviewer lineages must differ")
    return {
        "schema_version": SCHEMA_VERSION,
        "contract_id": text_value(static["contract_id"], "static_contract.contract_id", 3, 120),
        "planning_contract_sha256": sha_value(static["planning_contract_sha256"], "static_contract.planning_contract_sha256"),
        "graph_sha256": sha_value(static["graph_sha256"], "static_contract.graph_sha256"),
        "node_id": node_id,
        "static_template_id": text_value(static["static_template_id"], "static_contract.static_template_id", 3, 100),
        "pilot_mode": "shadow-only",
        "control_veto": bool_value(static["control_veto"], "static_contract.control_veto"),
        "objective": text_value(static["objective"], "static_contract.objective", 8, 2000),
        "capability": text_value(static["capability"], "static_contract.capability", 3, 100),
        "required_context_refs": required_context,
        "allowed_context_refs": allowed_context,
        "skill_allowlist": string_list(static["skill_allowlist"], "static_contract.skill_allowlist", maximum=32, pattern=SKILL_RE),
        "tool_allowlist": string_list(static["tool_allowlist"], "static_contract.tool_allowlist", maximum=64),
        "read_scope": scope_list(static["read_scope"], "static_contract.read_scope", 128),
        "write_scope": scope_list(static["write_scope"], "static_contract.write_scope", 64),
        "budget": validate_budget(static["budget"], "static_contract.budget"),
        "stop_conditions": string_list(static["stop_conditions"], "static_contract.stop_conditions", minimum=1, maximum=16),
        "criterion_refs": string_list(static["criterion_refs"], "static_contract.criterion_refs", minimum=1, maximum=64, pattern=CRIT_RE),
        "repair_policy": validate_repair(static["repair_policy"], "static_contract.repair_policy"),
        "reviewer_capabilities": string_list(static["reviewer_capabilities"], "static_contract.reviewer_capabilities", minimum=1, maximum=16),
        "maker_provider_lineage": maker,
        "reviewer_provider_lineage": reviewer,
    }


def validate_candidate(value: Any) -> dict[str, Any]:
    candidate = exact_keys(
        value,
        {
            "schema_version",
            "harness_id",
            "node_id",
            "planning_contract_sha256",
            "graph_sha256",
            "static_node_contract_sha256",
            "generator",
            "reviewer_constraint",
            "objective",
            "capability",
            "context_refs",
            "skill_bindings",
            "tool_allowlist",
            "read_scope",
            "write_scope",
            "budget",
            "stop_conditions",
            "criterion_refs",
            "repair_policy",
            "fallback",
        },
        "candidate",
    )
    if candidate["schema_version"] != SCHEMA_VERSION:
        raise JitError("schema-version", "Unsupported candidate schema_version")
    harness_id = text_value(candidate["harness_id"], "candidate.harness_id", 12, 90)
    if not re.fullmatch(r"harness-[a-z0-9][a-z0-9-]{7,80}", harness_id):
        raise JitError("schema-identifier", "Invalid candidate.harness_id")
    node_id = text_value(candidate["node_id"], "candidate.node_id", 5, 105)
    if not re.fullmatch(r"node-[a-z0-9][a-z0-9-]{2,100}", node_id):
        raise JitError("schema-identifier", "Invalid candidate.node_id")
    generator = exact_keys(candidate["generator"], {"fleet_run_id", "provider_lineage", "model_identity"}, "candidate.generator")
    fallback = exact_keys(candidate["fallback"], {"static_template_id", "trigger"}, "candidate.fallback")
    return {
        "schema_version": SCHEMA_VERSION,
        "harness_id": harness_id,
        "node_id": node_id,
        "planning_contract_sha256": sha_value(candidate["planning_contract_sha256"], "candidate.planning_contract_sha256"),
        "graph_sha256": sha_value(candidate["graph_sha256"], "candidate.graph_sha256"),
        "static_node_contract_sha256": sha_value(candidate["static_node_contract_sha256"], "candidate.static_node_contract_sha256"),
        "generator": {
            "fleet_run_id": text_value(generator["fleet_run_id"], "candidate.generator.fleet_run_id", 8, 200),
            "provider_lineage": text_value(generator["provider_lineage"], "candidate.generator.provider_lineage", 2, 100),
            "model_identity": text_value(generator["model_identity"], "candidate.generator.model_identity", 2, 200),
        },
        "reviewer_constraint": validate_reviewer(candidate["reviewer_constraint"], "candidate.reviewer_constraint"),
        "objective": text_value(candidate["objective"], "candidate.objective", 8, 2000),
        "capability": text_value(candidate["capability"], "candidate.capability", 3, 100),
        "context_refs": validate_context_refs(candidate["context_refs"], "candidate.context_refs"),
        "skill_bindings": string_list(candidate["skill_bindings"], "candidate.skill_bindings", maximum=8, pattern=SKILL_RE),
        "tool_allowlist": string_list(candidate["tool_allowlist"], "candidate.tool_allowlist", maximum=32),
        "read_scope": scope_list(candidate["read_scope"], "candidate.read_scope", 64),
        "write_scope": scope_list(candidate["write_scope"], "candidate.write_scope", 32),
        "budget": validate_budget(candidate["budget"], "candidate.budget"),
        "stop_conditions": string_list(candidate["stop_conditions"], "candidate.stop_conditions", minimum=1, maximum=16),
        "criterion_refs": string_list(candidate["criterion_refs"], "candidate.criterion_refs", minimum=1, maximum=64, pattern=CRIT_RE),
        "repair_policy": validate_repair(candidate["repair_policy"], "candidate.repair_policy"),
        "fallback": {
            "static_template_id": text_value(fallback["static_template_id"], "candidate.fallback.static_template_id", 3, 100),
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


def compare_narrowing(static: dict[str, Any], candidate: dict[str, Any]) -> None:
    static_hash = sha256_value(static)
    if candidate["static_node_contract_sha256"] != static_hash:
        raise JitError("static-binding", "Candidate static node contract hash mismatch")
    for field in ("planning_contract_sha256", "graph_sha256", "node_id"):
        if candidate[field] != static[field]:
            raise JitError(f"{field}-binding", f"Candidate {field} mismatch")
    if candidate["fallback"]["static_template_id"] != static["static_template_id"]:
        raise JitError("fallback-binding", "Candidate static template mismatch")
    if candidate["objective"] != static["objective"]:
        raise JitError("objective-expansion", "Phase 1 requires the exact static objective")
    if candidate["capability"] != static["capability"]:
        raise JitError("capability-expansion", "Candidate capability mismatch")
    generator_lineage = candidate["generator"]["provider_lineage"].casefold()
    if generator_lineage in {
        static["maker_provider_lineage"].casefold(),
        static["reviewer_provider_lineage"].casefold(),
    }:
        raise JitError("lineage-collision", "Harness generator must differ from maker and reviewer")
    candidate_context = {(item["path"], item["sha256"]) for item in candidate["context_refs"]}
    required_context = {(item["path"], item["sha256"]) for item in static["required_context_refs"]}
    allowed_context = {(item["path"], item["sha256"]) for item in static["allowed_context_refs"]}
    if not required_context <= candidate_context:
        raise JitError("context-loss", "Candidate omits required context")
    if not candidate_context <= allowed_context:
        raise JitError("context-expansion", "Candidate adds unapproved context")
    for field, static_field in (
        ("skill_bindings", "skill_allowlist"),
        ("tool_allowlist", "tool_allowlist"),
        ("read_scope", "read_scope"),
        ("write_scope", "write_scope"),
    ):
        if not set(candidate[field]) <= set(static[static_field]):
            raise JitError(f"{field}-expansion", f"Candidate {field} exceeds static authority")
    for field, value in candidate["budget"].items():
        if value > static["budget"][field]:
            raise JitError("budget-expansion", f"Candidate {field} exceeds static budget")
    if not set(static["stop_conditions"]) <= set(candidate["stop_conditions"]):
        raise JitError("stop-condition-loss", "Candidate omits required stop conditions")
    if not set(static["criterion_refs"]) <= set(candidate["criterion_refs"]):
        raise JitError("criterion-loss", "Candidate omits acceptance criteria")
    if not set(static["reviewer_capabilities"]) <= set(candidate["reviewer_constraint"]["required_capabilities"]):
        raise JitError("reviewer-capability-loss", "Candidate weakens reviewer requirements")
    if REPAIR_STRICTNESS[candidate["repair_policy"]["on_reject"]] > REPAIR_STRICTNESS[static["repair_policy"]["on_reject"]]:
        raise JitError("repair-expansion", "Candidate repair route is looser")
    if candidate["repair_policy"]["max_repairs"] > static["repair_policy"]["max_repairs"]:
        raise JitError("repair-expansion", "Candidate repair count exceeds static ceiling")


def receipt(
    *,
    static_hash: str,
    candidate_hash: str | None,
    decision: str,
    reason_codes: list[str],
) -> dict[str, Any]:
    base = {
        "schema_version": SCHEMA_VERSION,
        "stage": "stage-b",
        "static_contract_sha256": static_hash,
        "candidate_sha256": candidate_hash,
        "decision": decision,
        "reason_codes": sorted(set(reason_codes)),
        "static_path_selected": True,
    }
    receipt_id = "jit-receipt-" + sha256_value(base)[:24]
    return {"schema_version": SCHEMA_VERSION, "stage": "stage-b", "receipt_id": receipt_id, **{key: value for key, value in base.items() if key not in {"schema_version", "stage"}}}


def evaluate(static_path: Path, candidate_path: Path) -> dict[str, Any]:
    static = validate_static(load_json(static_path, "static contract"))
    static_hash = sha256_value(static)
    candidate_hash: str | None = None
    try:
        candidate_raw = load_json(candidate_path, "candidate")
        candidate_hash = sha256_value(candidate_raw)
        reject_executable_or_worker_selection(candidate_raw)
        candidate = validate_candidate(candidate_raw)
        compare_narrowing(static, candidate)
    except JitError as exc:
        return receipt(
            static_hash=static_hash,
            candidate_hash=candidate_hash,
            decision="rejected",
            reason_codes=[exc.code, "static-fallback"],
        )
    reasons = ["shadow-only", "static-fallback"]
    if static["control_veto"]:
        return receipt(
            static_hash=static_hash,
            candidate_hash=candidate_hash,
            decision="disabled",
            reason_codes=reasons + ["control-veto"],
        )
    return receipt(
        static_hash=static_hash,
        candidate_hash=candidate_hash,
        decision="accepted-shadow",
        reason_codes=reasons + ["active-path-not-implemented"],
    )


def unavailable(static_path: Path, reason: str) -> dict[str, Any]:
    static = validate_static(load_json(static_path, "static contract"))
    decision = "disabled" if reason == "disabled" else reason
    return receipt(
        static_hash=sha256_value(static),
        candidate_hash=None,
        decision=decision,
        reason_codes=[reason, "static-fallback"],
    )


def emit(value: dict[str, Any], output: str | None) -> None:
    if output:
        write_new_json(Path(output), value)
    print(json.dumps(value, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate Stage B JIT harnesses in shadow-only mode.")
    parser.add_argument("--version", action="version", version=VERSION)
    subparsers = parser.add_subparsers(dest="command", required=True)
    doctor = subparsers.add_parser("doctor")
    doctor.set_defaults(handler=lambda _args: {"status": "READY", "version": VERSION, "mode": "shadow-only"})
    hash_parser = subparsers.add_parser("hash-static")
    hash_parser.add_argument("--static-contract", required=True)
    hash_parser.set_defaults(handler=lambda args: {"static_contract_sha256": sha256_value(validate_static(load_json(Path(args.static_contract), "static contract")))})
    evaluate_parser = subparsers.add_parser("evaluate")
    evaluate_parser.add_argument("--static-contract", required=True)
    evaluate_parser.add_argument("--candidate", required=True)
    evaluate_parser.add_argument("--output")
    evaluate_parser.set_defaults(handler=lambda args: evaluate(Path(args.static_contract), Path(args.candidate)))
    unavailable_parser = subparsers.add_parser("unavailable")
    unavailable_parser.add_argument("--static-contract", required=True)
    unavailable_parser.add_argument("--reason", choices=["unavailable", "late", "disabled"], required=True)
    unavailable_parser.add_argument("--output")
    unavailable_parser.set_defaults(handler=lambda args: unavailable(Path(args.static_contract), args.reason))
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
