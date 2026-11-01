#!/usr/bin/env python3
"""Deterministic acceptance contract and evidence gate for accept-system 0.2.1.

This helper does not invoke models, tests, browsers, Agent Fleet, Agent Graph,
Agent Loop, Git, or network operations. It validates immutable acceptance
contracts and records externally produced evidence in an append-only ledger.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


VERSION = "0.2.1"
SCHEMA_VERSION = 1
SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE_ROOT = Path.home() / ".agents" / "acceptance" / "runs"

ACCEPTANCE_ID_RE = re.compile(r"^accept-[a-z0-9][a-z0-9-]{2,63}$")
CRITERION_ID_RE = re.compile(r"^accept-[a-z0-9][a-z0-9-]{2,95}$")
EVIDENCE_ID_RE = re.compile(r"^evidence-[a-z0-9][a-z0-9-]{2,95}$")
BINDING_ID_RE = re.compile(r"^binding-[a-z0-9][a-z0-9-]{2,95}$")
OPERATION_ID_RE = re.compile(r"^op-[a-z0-9][a-z0-9-]{2,95}$")
RUN_ID_RE = re.compile(r"^run-accept-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{10}$")
GRAPH_RUN_ID_RE = re.compile(r"^run-graph-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{10}$")
REQ_ID_RE = re.compile(r"^REQ-[0-9]{3,6}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")

ENVIRONMENTS = {"DEV", "TEST", "STAGING", "PROD"}
RECIPIENT_POLICIES = {"synthetic-only", "allowlisted", "owner-approved"}
SIDE_EFFECT_POLICIES = {"blocked", "allowlisted", "owner-approved"}
DOMAINS = {
    "business",
    "backend",
    "data",
    "integration",
    "ui-functional",
    "ux",
    "responsive",
    "accessibility",
    "performance",
    "reliability",
    "security",
    "side-effect",
}
ORACLE_KINDS = {
    "business-oracle",
    "user-journey-evidence",
    "side-effect-safety",
}
EVIDENCE_KINDS = {
    "artifact",
    "deterministic-check",
    "api-check",
    "browser-e2e",
    "visual-evidence",
    "accessibility-check",
    "performance-check",
    "security-gate",
    "quality-gate",
    "human-approval",
}
ENFORCEMENT_SCOPES = {
    "browser-context",
    "application",
    "network-egress",
    "external-service",
    "host",
}
RESULT_STATUSES = {"PASS", "FAIL", "BLOCKED"}
SURFACES = {"cli", "graphical"}
PLACEHOLDER_RE = re.compile(r"^(?:todo|tbd|unknown|n/?a|agent[- ]?choice|fill[- ]?me)$", re.I)
SENSITIVE_KEYS = {
    "authorization",
    "cookie",
    "cookies",
    "credential",
    "credentials",
    "password",
    "secret",
    "secrets",
    "storage_state",
    "storagestate",
    "token",
    "tokens",
    "api_key",
    "apikey",
}
SCORE_KEYS = {"score", "aggregate_score", "quality_score", "percentage", "rating"}
SENSITIVE_PATH_RE = re.compile(
    r"(?:^|[._/\\-])(?:cookies?|credentials?|passwords?|secrets?|tokens?|storage[-_]?state)(?:$|[._/\\-])",
    re.I,
)


class AcceptanceError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def compact_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise AcceptanceError(f"Invalid UTC timestamp: {value}") from exc
    if parsed.tzinfo is None:
        raise AcceptanceError(f"UTC timestamp must include timezone: {value}")
    return parsed.astimezone(timezone.utc)


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
        raise AcceptanceError(f"Missing {label}: {path}") from exc
    except json.JSONDecodeError as exc:
        raise AcceptanceError(f"Invalid JSON in {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise AcceptanceError(f"{label} must be a JSON object: {path}")
    return value


def atomic_write_json(path: Path, value: Any, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if exclusive and path.exists():
        raise AcceptanceError(f"Refusing to overwrite existing file: {path}")
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
        if exclusive and path.exists():
            raise AcceptanceError(f"Refusing to overwrite existing file: {path}")
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
        raise AcceptanceError(f"{label} missing keys: {', '.join(sorted(missing))}")
    if extra:
        raise AcceptanceError(f"{label} has unsupported keys: {', '.join(sorted(extra))}")


def require_string(value: Any, label: str, minimum: int = 1, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not (minimum <= len(value) <= maximum):
        raise AcceptanceError(f"{label} must be a string of length {minimum}..{maximum}")
    return value


def require_int(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not (minimum <= value <= maximum):
        raise AcceptanceError(f"{label} must be an integer in {minimum}..{maximum}")
    return value


def require_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise AcceptanceError(f"{label} must be a boolean")
    return value


def require_sha(value: Any, label: str) -> str:
    text = require_string(value, label, 64, 64).lower()
    if not SHA256_RE.fullmatch(text):
        raise AcceptanceError(f"{label} must be a lowercase SHA-256")
    return text


def require_id(value: Any, pattern: re.Pattern[str], label: str) -> str:
    text = require_string(value, label, 4, 128)
    if not pattern.fullmatch(text):
        raise AcceptanceError(f"Invalid {label}: {text}")
    return text


def require_argv(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 128:
        raise AcceptanceError(f"{label} must contain 1-128 arguments")
    for index, item in enumerate(value):
        if not isinstance(item, str) or len(item) > 1024 or "\x00" in item:
            raise AcceptanceError(f"Invalid {label}[{index}]")
    if not value[0]:
        raise AcceptanceError(f"{label} requires an executable")
    return value


def require_unique_strings(
    value: Any, label: str, maximum: int = 128, minimum: int = 0
) -> list[str]:
    if not isinstance(value, list) or not (minimum <= len(value) <= maximum):
        raise AcceptanceError(f"{label} must be an array of {minimum}..{maximum} strings")
    result: list[str] = []
    for index, item in enumerate(value):
        result.append(require_string(item, f"{label}[{index}]", 1, 1024))
    if len(result) != len(set(result)):
        raise AcceptanceError(f"{label} must not contain duplicates")
    return result


def normalize_relative_path(value: Any, label: str) -> str:
    text = require_string(value, label, 1, 512)
    if "\\" in text or any(character in text for character in '*?"<>|'):
        raise AcceptanceError(f"{label} must use normalized POSIX syntax: {text}")
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise AcceptanceError(f"{label} must be relative: {text}")
    parts = text.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise AcceptanceError(f"{label} contains an empty, dot, or parent segment: {text}")
    if SENSITIVE_PATH_RE.search(text):
        raise AcceptanceError(f"{label} looks like sensitive browser/auth material: {text}")
    return text


def resolve_under(root: Path, relative: str, label: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise AcceptanceError(f"{label} escapes its evidence root: {relative}") from exc
    return candidate


def reject_sensitive_or_score_keys(value: Any, label: str = "value") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).replace("-", "_").lower()
            if normalized in SENSITIVE_KEYS:
                raise AcceptanceError(f"{label} contains forbidden sensitive key: {key}")
            if normalized in SCORE_KEYS:
                raise AcceptanceError(f"{label} contains forbidden score key: {key}")
            reject_sensitive_or_score_keys(child, f"{label}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            reject_sensitive_or_score_keys(child, f"{label}[{index}]")


def _payload_records() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(SKILL_ROOT.rglob("*"), key=lambda item: item.as_posix().lower()):
        if not path.is_file():
            continue
        relative = path.relative_to(SKILL_ROOT).as_posix()
        parts = set(path.relative_to(SKILL_ROOT).parts)
        if "__pycache__" in parts or path.suffix == ".pyc" or path.name.startswith("."):
            continue
        records.append(
            {
                "path": relative,
                "sha256": sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
        )
    return records


def runtime_identity() -> dict[str, Any]:
    records = _payload_records()
    return {
        "version": VERSION,
        "helper_sha256": sha256_file(Path(__file__).resolve()),
        "skill_manifest_sha256": sha256_value(records),
        "skill_file_count": len(records),
    }


def validate_identity(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise AcceptanceError(f"{label} must be an object")
    require_exact_keys(value, {"id", "lineage", "role"}, set(), label)
    return {
        "id": require_string(value["id"], f"{label}.id", 2, 200),
        "lineage": require_string(value["lineage"], f"{label}.lineage", 2, 100),
        "role": require_string(value["role"], f"{label}.role", 2, 100),
    }


def validate_environment(value: Any) -> dict[str, Any]:
    label = "environment"
    if not isinstance(value, dict):
        raise AcceptanceError(f"{label} must be an object")
    require_exact_keys(
        value,
        {
            "name",
            "target",
            "synthetic_data",
            "recipient_policy",
            "external_side_effects",
            "allowed_destinations",
            "denied_effects",
            "enforcement_scope",
            "owner_approval",
        },
        set(),
        label,
    )
    name = require_string(value["name"], f"{label}.name", 3, 16).upper()
    if name not in ENVIRONMENTS:
        raise AcceptanceError(f"Unsupported environment: {name}")
    require_string(value["target"], f"{label}.target", 3, 500)
    synthetic = require_bool(value["synthetic_data"], f"{label}.synthetic_data")
    recipient_policy = value["recipient_policy"]
    side_effects = value["external_side_effects"]
    if recipient_policy not in RECIPIENT_POLICIES:
        raise AcceptanceError(f"Unsupported recipient policy: {recipient_policy}")
    if side_effects not in SIDE_EFFECT_POLICIES:
        raise AcceptanceError(f"Unsupported external side-effect policy: {side_effects}")
    destinations = require_unique_strings(value["allowed_destinations"], f"{label}.allowed_destinations", 64)
    denied = require_unique_strings(value["denied_effects"], f"{label}.denied_effects", 64, 1)
    normalized_denied = {item.lower() for item in denied}
    scopes = value["enforcement_scope"]
    if not isinstance(scopes, list) or not (1 <= len(scopes) <= 16):
        raise AcceptanceError(f"{label}.enforcement_scope must contain 1..16 entries")
    seen_scopes: set[str] = set()
    for index, item in enumerate(scopes):
        item_label = f"{label}.enforcement_scope[{index}]"
        if not isinstance(item, dict):
            raise AcceptanceError(f"{item_label} must be an object")
        require_exact_keys(item, {"scope", "mechanism", "evidence_ref"}, set(), item_label)
        scope = require_string(item["scope"], f"{item_label}.scope", 2, 64)
        if scope not in ENFORCEMENT_SCOPES:
            raise AcceptanceError(f"Unsupported enforcement scope: {scope}")
        if scope in seen_scopes:
            raise AcceptanceError(f"Duplicate enforcement scope: {scope}")
        seen_scopes.add(scope)
        require_string(item["mechanism"], f"{item_label}.mechanism", 4, 1000)
        require_string(item["evidence_ref"], f"{item_label}.evidence_ref", 3, 1000)

    approval = value["owner_approval"]
    if name in {"DEV", "TEST"}:
        if not synthetic:
            raise AcceptanceError(f"{name} requires synthetic_data=true")
        if recipient_policy != "synthetic-only":
            raise AcceptanceError(f"{name} requires recipient_policy=synthetic-only")
        if side_effects != "blocked":
            raise AcceptanceError(f"{name} requires external_side_effects=blocked")
        if destinations:
            raise AcceptanceError(f"{name} must not contain allowed real destinations")
        required_denials = {"email", "payment", "calendar", "crm", "public-integration"}
        if not required_denials.issubset(normalized_denied):
            missing = sorted(required_denials - normalized_denied)
            raise AcceptanceError(f"{name} denied_effects missing: {', '.join(missing)}")
        if approval is not None:
            raise AcceptanceError(f"{name} must not carry production owner approval")
    if name == "STAGING" and side_effects == "allowlisted" and not destinations:
        raise AcceptanceError("STAGING allowlisted side effects require explicit destinations")
    if name == "PROD":
        if recipient_policy != "owner-approved" or side_effects != "owner-approved":
            raise AcceptanceError("PROD requires owner-approved recipients and side effects")
        if not isinstance(approval, dict):
            raise AcceptanceError("PROD requires a bounded owner_approval")
        require_exact_keys(
            approval, {"approval_id", "approved_by", "scope", "expires_at"}, set(), "owner_approval"
        )
        require_string(approval["approval_id"], "owner_approval.approval_id", 4, 200)
        if require_string(approval["approved_by"], "owner_approval.approved_by", 3, 200) != "Mateusz":
            raise AcceptanceError("PROD owner_approval.approved_by must be Mateusz")
        require_string(approval["scope"], "owner_approval.scope", 8, 2000)
        if parse_utc(require_string(approval["expires_at"], "owner_approval.expires_at")) <= datetime.now(timezone.utc):
            raise AcceptanceError("PROD owner approval is expired")
    elif approval is not None:
        raise AcceptanceError("owner_approval is only valid for PROD")
    return value


def validate_criterion(value: Any, index: int, maker: dict[str, str], env: dict[str, Any]) -> dict[str, Any]:
    label = f"criteria[{index}]"
    if not isinstance(value, dict):
        raise AcceptanceError(f"{label} must be an object")
    require_exact_keys(
        value,
        {
            "criterion_id",
            "title",
            "requirement_refs",
            "mandatory",
            "applicability",
            "domain",
            "oracle_kind",
            "oracle",
            "evidence_kinds",
            "max_age_seconds",
            "reviewer_independence",
            "n_a_authority",
            "required_enforcement_scopes",
        },
        {"surface", "cli"},
        label,
    )
    require_id(value["criterion_id"], CRITERION_ID_RE, f"{label}.criterion_id")
    require_string(value["title"], f"{label}.title", 6, 300)
    refs = require_unique_strings(value["requirement_refs"], f"{label}.requirement_refs", 64, 1)
    for ref in refs:
        if not REQ_ID_RE.fullmatch(ref):
            raise AcceptanceError(f"Invalid requirement ref in {label}: {ref}")
    require_bool(value["mandatory"], f"{label}.mandatory")
    applicability = value["applicability"]
    if applicability not in {"applicable", "not-applicable"}:
        raise AcceptanceError(f"Unsupported {label}.applicability: {applicability}")
    domain = value["domain"]
    if domain not in DOMAINS:
        raise AcceptanceError(f"Unsupported {label}.domain: {domain}")
    oracle_kind = value["oracle_kind"]
    if oracle_kind not in ORACLE_KINDS:
        raise AcceptanceError(f"Unsupported {label}.oracle_kind: {oracle_kind}")
    if domain == "business" and oracle_kind != "business-oracle":
        raise AcceptanceError(f"{label} business domain requires business-oracle")
    if domain in {"ui-functional", "ux", "responsive", "accessibility"} and oracle_kind != "user-journey-evidence":
        raise AcceptanceError(f"{label} user-facing domain requires user-journey-evidence")
    if domain == "side-effect" and oracle_kind != "side-effect-safety":
        raise AcceptanceError(f"{label} side-effect domain requires side-effect-safety")

    surface = value.get("surface", "graphical")
    if surface not in SURFACES:
        raise AcceptanceError(f"Unsupported {label}.surface: {surface}")
    cli = value.get("cli")
    if surface == "cli":
        if oracle_kind != "user-journey-evidence":
            raise AcceptanceError(f"{label} CLI surface requires user-journey-evidence")
        if not isinstance(cli, dict):
            raise AcceptanceError(f"{label}.cli is required for CLI surface")
        require_exact_keys(cli, {"argv", "expected_exit_code"}, set(), f"{label}.cli")
        require_argv(cli["argv"], f"{label}.cli.argv")
        require_int(cli["expected_exit_code"], f"{label}.cli.expected_exit_code", -255, 255)
    elif cli is not None:
        raise AcceptanceError(f"{label}.cli belongs only to CLI surface")

    oracle = value["oracle"]
    if not isinstance(oracle, dict):
        raise AcceptanceError(f"{label}.oracle must be an object")
    require_exact_keys(oracle, {"expected", "method", "source_ref"}, set(), f"{label}.oracle")
    for field in ("expected", "method", "source_ref"):
        text = require_string(oracle[field], f"{label}.oracle.{field}", 4, 2000).strip()
        if PLACEHOLDER_RE.fullmatch(text):
            raise AcceptanceError(f"{label}.oracle.{field} is a placeholder")

    evidence_kinds = require_unique_strings(value["evidence_kinds"], f"{label}.evidence_kinds", 16, 1)
    unsupported = set(evidence_kinds) - EVIDENCE_KINDS
    if unsupported:
        raise AcceptanceError(f"Unsupported evidence kinds in {label}: {', '.join(sorted(unsupported))}")
    if oracle_kind == "user-journey-evidence":
        if surface == "cli":
            if evidence_kinds != ["deterministic-check"]:
                raise AcceptanceError(f"{label} CLI journey requires deterministic-check only")
        elif not set(evidence_kinds).intersection({"browser-e2e", "visual-evidence"}):
            raise AcceptanceError(f"{label} graphical journey requires browser-e2e or visual-evidence")
    require_int(value["max_age_seconds"], f"{label}.max_age_seconds", 60, 604800)

    independence = value["reviewer_independence"]
    if not isinstance(independence, dict):
        raise AcceptanceError(f"{label}.reviewer_independence must be an object")
    require_exact_keys(independence, {"not_same_actor", "not_same_lineage", "allowed_roles"}, set(), f"{label}.reviewer_independence")
    if not require_bool(independence["not_same_actor"], f"{label}.reviewer_independence.not_same_actor"):
        raise AcceptanceError(f"{label} must require a reviewer distinct from the maker")
    require_bool(independence["not_same_lineage"], f"{label}.reviewer_independence.not_same_lineage")
    require_unique_strings(independence["allowed_roles"], f"{label}.reviewer_independence.allowed_roles", 16, 1)

    authority = value["n_a_authority"]
    if applicability == "not-applicable":
        if not isinstance(authority, dict):
            raise AcceptanceError(f"{label} N/A requires trusted authority")
        require_exact_keys(authority, {"kind", "actor_id", "actor_role", "evidence_ref"}, set(), f"{label}.n_a_authority")
        if authority["kind"] not in {"trusted-discovery", "owner-waiver"}:
            raise AcceptanceError(f"Unsupported N/A authority in {label}: {authority['kind']}")
        actor_id = require_string(authority["actor_id"], f"{label}.n_a_authority.actor_id", 2, 200)
        actor_role = require_string(authority["actor_role"], f"{label}.n_a_authority.actor_role", 2, 100)
        if actor_id == maker["id"]:
            raise AcceptanceError(f"{label} maker cannot author N/A")
        expected_role = "owner" if authority["kind"] == "owner-waiver" else "discovery-gate"
        if actor_role != expected_role:
            raise AcceptanceError(f"{label} {authority['kind']} requires actor_role={expected_role}")
        require_string(authority["evidence_ref"], f"{label}.n_a_authority.evidence_ref", 3, 1000)
    elif authority is not None:
        raise AcceptanceError(f"{label} applicable criterion must set n_a_authority=null")

    required_scopes = require_unique_strings(
        value["required_enforcement_scopes"], f"{label}.required_enforcement_scopes", 16
    )
    if set(required_scopes) - ENFORCEMENT_SCOPES:
        raise AcceptanceError(f"Unsupported required enforcement scope in {label}")
    env_scopes = {item["scope"] for item in env["enforcement_scope"]}
    if not set(required_scopes).issubset(env_scopes):
        missing = sorted(set(required_scopes) - env_scopes)
        raise AcceptanceError(f"{label} requires scopes absent from environment: {', '.join(missing)}")
    if oracle_kind == "side-effect-safety" and not required_scopes:
        raise AcceptanceError(f"{label} side-effect safety requires exact enforcement scopes")
    if oracle_kind != "side-effect-safety" and required_scopes:
        raise AcceptanceError(f"{label} enforcement scopes belong only to side-effect-safety")
    return value


def validate_contract(contract: dict[str, Any]) -> dict[str, Any]:
    reject_sensitive_or_score_keys(contract, "acceptance contract")
    require_exact_keys(
        contract,
        {
            "schema_version",
            "acceptance_id",
            "project_slug",
            "status",
            "planning_contract",
            "graph",
            "environment",
            "maker",
            "max_generations",
            "criteria",
        },
        {"created_at"},
        "acceptance contract",
    )
    if contract["schema_version"] != SCHEMA_VERSION:
        raise AcceptanceError(f"Unsupported acceptance schema: {contract['schema_version']}")
    require_id(contract["acceptance_id"], ACCEPTANCE_ID_RE, "acceptance_id")
    require_string(contract["project_slug"], "project_slug", 2, 100)
    if contract["status"] != "READY":
        raise AcceptanceError("Acceptance contract must be READY before init")
    if "created_at" in contract:
        parse_utc(require_string(contract["created_at"], "created_at"))

    planning = contract["planning_contract"]
    if not isinstance(planning, dict):
        raise AcceptanceError("planning_contract must be an object")
    require_exact_keys(planning, {"contract_id", "contract_hash"}, set(), "planning_contract")
    require_string(planning["contract_id"], "planning_contract.contract_id", 4, 100)
    require_sha(planning["contract_hash"], "planning_contract.contract_hash")

    graph = contract["graph"]
    if not isinstance(graph, dict):
        raise AcceptanceError("graph must be an object")
    require_exact_keys(graph, {"graph_id", "graph_hash"}, set(), "graph")
    require_string(graph["graph_id"], "graph.graph_id", 4, 100)
    require_sha(graph["graph_hash"], "graph.graph_hash")

    env = validate_environment(contract["environment"])
    maker = validate_identity(contract["maker"], "maker")
    require_int(contract["max_generations"], "max_generations", 1, 10)
    criteria = contract["criteria"]
    if not isinstance(criteria, list) or not (3 <= len(criteria) <= 128):
        raise AcceptanceError("criteria must contain 3..128 entries")
    seen: set[str] = set()
    oracle_kinds: set[str] = set()
    domains_seen: set[str] = set()
    for index, criterion in enumerate(criteria):
        validate_criterion(criterion, index, maker, env)
        criterion_id = criterion["criterion_id"]
        if criterion_id in seen:
            raise AcceptanceError(f"Duplicate criterion_id: {criterion_id}")
        seen.add(criterion_id)
        oracle_kinds.add(criterion["oracle_kind"])
        domains_seen.add(criterion["domain"])
    if oracle_kinds != ORACLE_KINDS:
        missing = sorted(ORACLE_KINDS - oracle_kinds)
        raise AcceptanceError(f"Contract missing oracle kinds: {', '.join(missing)}")
    if domains_seen != DOMAINS:
        missing = sorted(DOMAINS - domains_seen)
        raise AcceptanceError(
            f"Contract missing explicit domain applicability: {', '.join(missing)}"
        )
    return contract


def validate_contract_bindings(
    contract: dict[str, Any], planning: dict[str, Any], graph: dict[str, Any]
) -> dict[str, Any]:
    validate_contract(contract)
    if planning.get("status") not in {"approved", "amended"}:
        raise AcceptanceError("Planning contract must be approved or amended")
    plan_hash = planning.get("approval", {}).get("contract_hash") or planning.get(
        "contract_hash"
    )
    require_sha(plan_hash, "approved planning contract.contract_hash")
    if contract["planning_contract"]["contract_id"] != planning.get("contract_id"):
        raise AcceptanceError("Acceptance contract planning contract_id mismatch")
    if contract["planning_contract"]["contract_hash"] != plan_hash:
        raise AcceptanceError("Acceptance contract planning hash mismatch")
    graph_hash = sha256_value(graph)
    if contract["graph"]["graph_id"] != graph.get("graph_id"):
        raise AcceptanceError("Acceptance contract graph_id mismatch")
    if contract["graph"]["graph_hash"] != graph_hash:
        raise AcceptanceError("Acceptance contract graph hash mismatch")
    projection = planning.get("graph", {}).get("projection_sha256")
    if projection != graph_hash:
        raise AcceptanceError("Planning contract projection does not match graph bytes")
    if not any(node.get("active", True) and node.get("capability") == "final-acceptance" for node in graph.get("nodes", [])):
        raise AcceptanceError("Graph must contain an active final-acceptance node")

    requirements = planning.get("requirements")
    if not isinstance(requirements, list):
        raise AcceptanceError("Planning contract requirements must be an array")
    active_material = {
        item.get("requirement_id")
        for item in requirements
        if item.get("material") is True and item.get("status") in {"active", "pending"}
    }
    if None in active_material:
        raise AcceptanceError("Planning contract contains a material requirement without ID")
    known = {item.get("requirement_id") for item in requirements}
    coverage: dict[str, list[dict[str, Any]]] = {}
    for criterion in contract["criteria"]:
        for requirement_id in criterion["requirement_refs"]:
            if requirement_id not in known:
                raise AcceptanceError(f"Unknown requirement_ref in acceptance contract: {requirement_id}")
            coverage.setdefault(requirement_id, []).append(criterion)
    missing = sorted(active_material - set(coverage))
    if missing:
        raise AcceptanceError(f"Acceptance contract misses material requirements: {', '.join(missing)}")
    for requirement_id in sorted(active_material):
        if not any(item["mandatory"] and item["applicability"] == "applicable" for item in coverage[requirement_id]):
            raise AcceptanceError(
                f"Material requirement lacks applicable mandatory acceptance: {requirement_id}"
            )
    return {
        "status": "VALID",
        "acceptance_id": contract["acceptance_id"],
        "acceptance_contract_hash": sha256_value(contract),
        "planning_contract_hash": plan_hash,
        "graph_hash": graph_hash,
        "criterion_count": len(contract["criteria"]),
        "material_requirement_count": len(active_material),
    }


def validate_binding(binding: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    reject_sensitive_or_score_keys(binding, "binding")
    require_exact_keys(binding, {"schema_version", "binding_id", "code", "environment", "maker"}, set(), "binding")
    if binding["schema_version"] != SCHEMA_VERSION:
        raise AcceptanceError("Unsupported binding schema")
    require_id(binding["binding_id"], BINDING_ID_RE, "binding_id")
    code = binding["code"]
    if not isinstance(code, dict):
        raise AcceptanceError("binding.code must be an object")
    require_exact_keys(code, {"revision", "sha256", "evidence_ref"}, set(), "binding.code")
    require_string(code["revision"], "binding.code.revision", 1, 500)
    require_sha(code["sha256"], "binding.code.sha256")
    require_string(code["evidence_ref"], "binding.code.evidence_ref", 3, 1000)
    env = binding["environment"]
    if not isinstance(env, dict):
        raise AcceptanceError("binding.environment must be an object")
    require_exact_keys(env, {"name", "target", "policy_sha256"}, set(), "binding.environment")
    if env["name"] != contract["environment"]["name"] or env["target"] != contract["environment"]["target"]:
        raise AcceptanceError("Binding environment differs from frozen acceptance contract")
    require_sha(env["policy_sha256"], "binding.environment.policy_sha256")
    maker = validate_identity(binding["maker"], "binding.maker")
    if maker != contract["maker"]:
        raise AcceptanceError("Binding maker differs from frozen acceptance contract")
    return binding


def _initial_criteria(contract: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for criterion in contract["criteria"]:
        if criterion["applicability"] == "not-applicable":
            result[criterion["criterion_id"]] = {
                "status": "N/A",
                "authority": copy.deepcopy(criterion["n_a_authority"]),
                "evidence": None,
            }
        else:
            result[criterion["criterion_id"]] = {
                "status": "PENDING",
                "authority": None,
                "evidence": None,
            }
    return result


def resolve_state_root(value: str | None) -> Path:
    return Path(value).expanduser().resolve() if value else DEFAULT_STATE_ROOT.resolve()


def resolve_run_dir(state_root: Path, run_id: str) -> Path:
    require_id(run_id, RUN_ID_RE, "run_id")
    run_dir = (state_root / run_id).resolve()
    try:
        run_dir.relative_to(state_root.resolve())
    except ValueError as exc:
        raise AcceptanceError("Run directory escapes state root") from exc
    return run_dir


@contextmanager
def run_lock(run_dir: Path) -> Iterator[None]:
    lock_path = run_dir / ".lock"
    handle = lock_path.open("a+b")
    locked = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as exc:
            raise AcceptanceError(f"Run is locked: {run_dir.name}") from exc
        handle.seek(1)
        handle.write(f"pid={os.getpid()}\n".encode("ascii"))
        handle.truncate()
        handle.flush()
        os.fsync(handle.fileno())
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _event_without_hash(
    state: dict[str, Any], operation_id: str, action: str, payload: dict[str, Any]
) -> dict[str, Any]:
    return {
        "seq": state["event_count"] + 1,
        "timestamp": utc_now(),
        "operation_id": operation_id,
        "action": action,
        "payload_sha256": sha256_value(payload),
        "previous_event_hash": state["event_head"],
    }


def _read_events(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "events.jsonl"
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AcceptanceError(f"Invalid event JSON at line {line_number}") from exc
            if not isinstance(event, dict):
                raise AcceptanceError(f"Event line {line_number} must be an object")
            events.append(event)
    return events


def verify_event_chain(run_dir: Path) -> dict[str, Any]:
    previous = None
    events = _read_events(run_dir)
    for index, event in enumerate(events, 1):
        if event.get("seq") != index:
            raise AcceptanceError(f"Event sequence mismatch at {index}")
        if event.get("previous_event_hash") != previous:
            raise AcceptanceError(f"Event chain mismatch at {index}")
        supplied = event.get("event_hash")
        base = {key: value for key, value in event.items() if key != "event_hash"}
        calculated = sha256_value(base)
        if supplied != calculated:
            raise AcceptanceError(f"Event hash mismatch at {index}")
        previous = supplied
    return {"count": len(events), "head": previous}


def _append_event(run_dir: Path, event: dict[str, Any]) -> None:
    events = _read_events(run_dir)
    if any(item.get("event_hash") == event["event_hash"] for item in events):
        return
    previous = events[-1]["event_hash"] if events else None
    if event["previous_event_hash"] != previous or event["seq"] != len(events) + 1:
        raise AcceptanceError("Refusing to append event onto a different chain")
    with (run_dir / "events.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(event, ensure_ascii=True, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _load_state(run_dir: Path) -> dict[str, Any]:
    return load_json(run_dir / "state.json", "acceptance state")


def _recover_pending_locked(run_dir: Path) -> int:
    transaction_path = run_dir / "pending-transaction.json"
    if not transaction_path.exists():
        return 0
    transaction = load_json(transaction_path, "pending transaction")
    require_exact_keys(transaction, {"old_state_sha256", "new_state", "event"}, set(), "pending transaction")
    current = _load_state(run_dir)
    current_hash = sha256_value(current)
    new_state = transaction["new_state"]
    new_hash = sha256_value(new_state)
    if current_hash not in {transaction["old_state_sha256"], new_hash}:
        raise AcceptanceError("Pending transaction does not match current state")
    _append_event(run_dir, transaction["event"])
    if current_hash != new_hash:
        atomic_write_json(run_dir / "state.json", new_state)
    transaction_path.unlink()
    return 1


def _commit_mutation(
    run_dir: Path,
    old_state: dict[str, Any],
    new_state: dict[str, Any],
    operation_id: str,
    action: str,
    response: dict[str, Any],
) -> dict[str, Any]:
    require_id(operation_id, OPERATION_ID_RE, "operation_id")
    new_state = copy.deepcopy(new_state)
    new_state["updated_at"] = utc_now()
    new_state.setdefault("operations", {})[operation_id] = copy.deepcopy(response)
    event_base = _event_without_hash(old_state, operation_id, action, response)
    event = {**event_base, "event_hash": sha256_value(event_base)}
    new_state["event_count"] = event["seq"]
    new_state["event_head"] = event["event_hash"]
    transaction = {
        "old_state_sha256": sha256_value(old_state),
        "new_state": new_state,
        "event": event,
    }
    transaction_path = run_dir / "pending-transaction.json"
    atomic_write_json(transaction_path, transaction, exclusive=True)
    _append_event(run_dir, event)
    atomic_write_json(run_dir / "state.json", new_state)
    transaction_path.unlink()
    return response


def _cached_operation(state: dict[str, Any], operation_id: str) -> dict[str, Any] | None:
    require_id(operation_id, OPERATION_ID_RE, "operation_id")
    cached = state.get("operations", {}).get(operation_id)
    return copy.deepcopy(cached) if isinstance(cached, dict) else None


def _assert_runtime(state: dict[str, Any]) -> dict[str, Any]:
    current = runtime_identity()
    if current["helper_sha256"] != state["runtime"]["helper_sha256"]:
        raise AcceptanceError("Runtime helper drifted after run initialization")
    if current["skill_manifest_sha256"] != state["runtime"]["skill_manifest_sha256"]:
        raise AcceptanceError("Skill manifest drifted after run initialization")
    return current


def _criterion_map(contract: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["criterion_id"]: item for item in contract["criteria"]}


def _current_generation(state: dict[str, Any]) -> dict[str, Any]:
    if state["current_generation"] < 1 or not state["generations"]:
        raise AcceptanceError("Acceptance run has not started")
    generation = state["generations"][-1]
    if generation["generation"] != state["current_generation"]:
        raise AcceptanceError("Current generation ledger is inconsistent")
    return generation


def _frozen_acceptance_inputs(
    contract: dict[str, Any], planning: dict[str, Any]
) -> dict[str, str]:
    if not isinstance(contract.get("criteria"), list):
        raise AcceptanceError("Acceptance criteria snapshot is invalid; full retest is required")
    if not isinstance(planning.get("requirements"), list):
        raise AcceptanceError("Planning requirements snapshot is invalid; full retest is required")
    return {
        "criteria_sha256": sha256_value(contract["criteria"]),
        "requirements_sha256": sha256_value(planning["requirements"]),
    }


def _assert_frozen_acceptance_inputs(run_dir: Path, state: dict[str, Any]) -> dict[str, Any]:
    contract = load_json(run_dir / "contracts" / "acceptance.json", "acceptance snapshot")
    planning = load_json(run_dir / "contracts" / "planning.json", "planning snapshot")
    graph = load_json(run_dir / "contracts" / "graph.json", "graph snapshot")
    frozen = _frozen_acceptance_inputs(contract, planning)
    if not state.get("criteria_sha256") or not state.get("requirements_sha256"):
        raise AcceptanceError(
            "Evidence-only rebind requires frozen criteria and requirements hashes"
        )
    if sha256_value(contract) != state["acceptance_contract_hash"]:
        raise AcceptanceError("Acceptance contract changed; full retest is required")
    if sha256_value(graph) != state["graph_hash"]:
        raise AcceptanceError("Graph changed; full retest is required")
    for key, label in (
        ("criteria_sha256", "Acceptance criteria"),
        ("requirements_sha256", "Planning requirements"),
    ):
        if frozen[key] != state[key]:
            raise AcceptanceError(f"{label} changed; full retest is required")
    return {"contract": contract, "planning": planning, "graph": graph}


def _validate_artifacts(
    artifacts: Any, source_root: Path, label: str
) -> list[dict[str, Any]]:
    if not isinstance(artifacts, list) or not (1 <= len(artifacts) <= 64):
        raise AcceptanceError(f"{label} must contain 1..64 artifacts")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, artifact in enumerate(artifacts):
        item_label = f"{label}[{index}]"
        if not isinstance(artifact, dict):
            raise AcceptanceError(f"{item_label} must be an object")
        require_exact_keys(artifact, {"path", "sha256", "size_bytes"}, set(), item_label)
        relative = normalize_relative_path(artifact["path"], f"{item_label}.path")
        if relative in seen:
            raise AcceptanceError(f"Duplicate artifact path: {relative}")
        seen.add(relative)
        expected_hash = require_sha(artifact["sha256"], f"{item_label}.sha256")
        expected_size = require_int(artifact["size_bytes"], f"{item_label}.size_bytes", 0, 2_147_483_647)
        path = resolve_under(source_root, relative, item_label)
        if not path.is_file():
            raise AcceptanceError(f"Missing artifact: {path}")
        if path.stat().st_size != expected_size:
            raise AcceptanceError(f"Artifact size mismatch: {relative}")
        if sha256_file(path) != expected_hash:
            raise AcceptanceError(f"Artifact hash mismatch: {relative}")
        result.append({"path": relative, "sha256": expected_hash, "size_bytes": expected_size})
    return result


def validate_evidence(
    evidence: dict[str, Any],
    evidence_path: Path,
    criterion: dict[str, Any],
    contract: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    reject_sensitive_or_score_keys(evidence, "evidence")
    require_exact_keys(
        evidence,
        {
            "schema_version",
            "evidence_id",
            "criterion_id",
            "generation",
            "binding_sha256",
            "status",
            "reviewer",
            "observed_at",
            "summary",
            "oracle_result",
            "evidence_kind",
            "artifacts",
            "journey",
            "enforcement_scope",
            "runtime",
            "findings",
        },
        set(),
        "evidence",
    )
    if evidence["schema_version"] != SCHEMA_VERSION:
        raise AcceptanceError("Unsupported evidence schema")
    require_id(evidence["evidence_id"], EVIDENCE_ID_RE, "evidence_id")
    if evidence["criterion_id"] != criterion["criterion_id"]:
        raise AcceptanceError("Evidence criterion_id mismatch")
    if evidence["generation"] != state["current_generation"]:
        raise AcceptanceError("Evidence generation is not current")
    if evidence["binding_sha256"] != state["current_binding_sha256"]:
        raise AcceptanceError("Evidence binding is not current")
    if evidence["status"] not in RESULT_STATUSES:
        raise AcceptanceError(f"Unsupported evidence status: {evidence['status']}")
    reviewer = validate_identity(evidence["reviewer"], "evidence.reviewer")
    independence = criterion["reviewer_independence"]
    if reviewer["role"] not in independence["allowed_roles"]:
        raise AcceptanceError("Reviewer role is not allowed for this criterion")
    if independence["not_same_actor"] and reviewer["id"] == contract["maker"]["id"]:
        raise AcceptanceError("Maker cannot review own criterion")
    if independence["not_same_lineage"] and reviewer["lineage"] == contract["maker"]["lineage"]:
        raise AcceptanceError("Reviewer lineage must differ from maker lineage")
    observed_at = parse_utc(require_string(evidence["observed_at"], "evidence.observed_at"))
    now = datetime.now(timezone.utc)
    if observed_at > now.replace(microsecond=0) and (observed_at - now).total_seconds() > 300:
        raise AcceptanceError("Evidence observed_at is implausibly in the future")
    if (now - observed_at).total_seconds() > criterion["max_age_seconds"]:
        raise AcceptanceError("Evidence is stale at ingestion")
    require_string(evidence["summary"], "evidence.summary", 4, 4000)
    if evidence["evidence_kind"] not in criterion["evidence_kinds"]:
        raise AcceptanceError("Evidence kind is not allowed by criterion")

    oracle_result = evidence["oracle_result"]
    if not isinstance(oracle_result, dict):
        raise AcceptanceError("evidence.oracle_result must be an object")
    require_exact_keys(oracle_result, {"expected", "actual", "method"}, set(), "evidence.oracle_result")
    if oracle_result["expected"] != criterion["oracle"]["expected"]:
        raise AcceptanceError("Evidence expected value differs from frozen oracle")
    if oracle_result["method"] != criterion["oracle"]["method"]:
        raise AcceptanceError("Evidence method differs from frozen oracle")
    require_string(oracle_result["actual"], "evidence.oracle_result.actual", 1, 4000)

    runtime = evidence["runtime"]
    if not isinstance(runtime, dict):
        raise AcceptanceError("evidence.runtime must be an object")
    require_exact_keys(runtime, {"helper_sha256", "skill_manifest_sha256"}, set(), "evidence.runtime")
    if require_sha(runtime["helper_sha256"], "evidence.runtime.helper_sha256") != state["runtime"]["helper_sha256"]:
        raise AcceptanceError("Evidence helper hash differs from run")
    if require_sha(runtime["skill_manifest_sha256"], "evidence.runtime.skill_manifest_sha256") != state["runtime"]["skill_manifest_sha256"]:
        raise AcceptanceError("Evidence skill manifest differs from run")

    artifacts = _validate_artifacts(evidence["artifacts"], evidence_path.parent.resolve(), "evidence.artifacts")
    findings = evidence["findings"]
    if not isinstance(findings, list) or len(findings) > 64:
        raise AcceptanceError("evidence.findings must be an array with at most 64 entries")
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            raise AcceptanceError(f"evidence.findings[{index}] must be an object")
        require_exact_keys(finding, {"finding_id", "severity", "summary"}, set(), f"evidence.findings[{index}]")
        require_string(finding["finding_id"], f"evidence.findings[{index}].finding_id", 3, 200)
        if finding["severity"] not in {"low", "medium", "high", "critical"}:
            raise AcceptanceError(f"Unsupported finding severity: {finding['severity']}")
        require_string(finding["summary"], f"evidence.findings[{index}].summary", 4, 2000)
    if evidence["status"] in {"FAIL", "BLOCKED"} and not findings:
        raise AcceptanceError("FAIL/BLOCKED evidence requires at least one finding")

    journey = evidence["journey"]
    if criterion["oracle_kind"] == "user-journey-evidence":
        if not isinstance(journey, dict):
            raise AcceptanceError("User-journey criterion requires journey evidence")
        if criterion.get("surface", "graphical") == "cli":
            require_exact_keys(
                journey,
                {"role", "command", "actions", "expected", "actual", "exit_code", "expected_exit_code"},
                set(),
                "evidence.journey",
            )
            require_string(journey["role"], "evidence.journey.role", 2, 100)
            command = require_argv(journey["command"], "evidence.journey.command")
            if command != criterion["cli"]["argv"]:
                raise AcceptanceError("CLI command differs from frozen criterion argv")
            require_unique_strings(journey["actions"], "evidence.journey.actions", 100, 1)
            if journey["expected"] != criterion["oracle"]["expected"]:
                raise AcceptanceError("Journey expected differs from frozen oracle")
            require_string(journey["actual"], "evidence.journey.actual", 1, 4000)
            actual_exit = require_int(journey["exit_code"], "evidence.journey.exit_code", -255, 255)
            expected_exit = require_int(
                journey["expected_exit_code"], "evidence.journey.expected_exit_code", -255, 255
            )
            frozen_exit = criterion["cli"]["expected_exit_code"]
            if expected_exit != frozen_exit:
                raise AcceptanceError("CLI expected_exit_code differs from frozen criterion")
            if evidence["status"] == "PASS" and actual_exit != frozen_exit:
                raise AcceptanceError("CLI PASS requires actual exit_code to equal expected_exit_code")
            artifact_stems = {Path(item["path"]).stem.lower() for item in artifacts}
            if artifact_stems != {"stdout", "stderr"} or len(artifacts) != 2:
                raise AcceptanceError("CLI journey requires stdout and stderr artifacts")
        else:
            require_exact_keys(journey, {"role", "route", "viewport", "actions", "expected", "actual"}, set(), "evidence.journey")
            require_string(journey["role"], "evidence.journey.role", 2, 100)
            require_string(journey["route"], "evidence.journey.route", 1, 1000)
            viewport = journey["viewport"]
            if not isinstance(viewport, dict):
                raise AcceptanceError("evidence.journey.viewport must be an object")
            require_exact_keys(viewport, {"width", "height"}, set(), "evidence.journey.viewport")
            require_int(viewport["width"], "evidence.journey.viewport.width", 240, 10000)
            require_int(viewport["height"], "evidence.journey.viewport.height", 240, 10000)
            require_unique_strings(journey["actions"], "evidence.journey.actions", 100, 1)
            if journey["expected"] != criterion["oracle"]["expected"]:
                raise AcceptanceError("Journey expected differs from frozen oracle")
            require_string(journey["actual"], "evidence.journey.actual", 1, 4000)
    elif journey is not None:
        raise AcceptanceError("Journey payload belongs only to user-journey evidence")

    enforcement = evidence["enforcement_scope"]
    if criterion["oracle_kind"] == "side-effect-safety":
        if not isinstance(enforcement, list) or not enforcement:
            raise AcceptanceError("Side-effect criterion requires enforcement_scope evidence")
        expected_scopes = set(criterion["required_enforcement_scopes"])
        actual_scopes: set[str] = set()
        env_by_scope = {item["scope"]: item for item in contract["environment"]["enforcement_scope"]}
        for index, item in enumerate(enforcement):
            item_label = f"evidence.enforcement_scope[{index}]"
            if not isinstance(item, dict):
                raise AcceptanceError(f"{item_label} must be an object")
            require_exact_keys(item, {"scope", "mechanism", "evidence_ref"}, set(), item_label)
            scope = require_string(item["scope"], f"{item_label}.scope", 2, 64)
            if scope not in expected_scopes or scope in actual_scopes:
                raise AcceptanceError(f"Unexpected or duplicate enforcement scope: {scope}")
            actual_scopes.add(scope)
            mechanism = require_string(item["mechanism"], f"{item_label}.mechanism", 4, 1000)
            require_string(item["evidence_ref"], f"{item_label}.evidence_ref", 3, 1000)
            if mechanism != env_by_scope[scope]["mechanism"]:
                raise AcceptanceError(f"Enforcement mechanism differs from frozen environment for {scope}")
            if scope != "browser-context" and re.search(
                r"playwright|browser|chromium|route\.abort", mechanism, re.I
            ):
                raise AcceptanceError(f"Browser-only mechanism cannot prove {scope} enforcement")
        if actual_scopes != expected_scopes:
            missing = sorted(expected_scopes - actual_scopes)
            raise AcceptanceError(f"Missing enforcement scopes: {', '.join(missing)}")
    elif enforcement not in (None, []):
        raise AcceptanceError("Enforcement scope payload belongs only to side-effect-safety")
    return {**evidence, "artifacts": artifacts, "reviewer": reviewer}


def _copy_evidence_into_run(
    run_dir: Path, evidence: dict[str, Any], evidence_path: Path
) -> dict[str, Any]:
    generation = evidence["generation"]
    evidence_id = evidence["evidence_id"]
    manifest_relative = f"evidence/g{generation}/{evidence_id}.json"
    artifact_root_relative = f"evidence-artifacts/g{generation}/{evidence_id}"
    manifest_target = run_dir / manifest_relative
    artifact_root = run_dir / artifact_root_relative
    if manifest_target.exists() or artifact_root.exists():
        raise AcceptanceError(f"Evidence snapshot already exists: {evidence_id}")
    created: list[Path] = []
    try:
        artifact_root.mkdir(parents=True, exist_ok=False)
        created.append(artifact_root)
        for artifact in evidence["artifacts"]:
            source = resolve_under(evidence_path.parent.resolve(), artifact["path"], "artifact source")
            target = resolve_under(artifact_root, artifact["path"], "artifact target")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if target.stat().st_size != artifact["size_bytes"] or sha256_file(target) != artifact["sha256"]:
                raise AcceptanceError(f"Copied artifact verification failed: {artifact['path']}")
        atomic_write_json(manifest_target, evidence, exclusive=True)
        created.append(manifest_target)
        return {
            "evidence_id": evidence_id,
            "status": evidence["status"],
            "manifest_path": manifest_relative,
            "manifest_sha256": sha256_file(manifest_target),
            "artifact_root": artifact_root_relative,
            "reviewer": evidence["reviewer"],
            "observed_at": evidence["observed_at"],
            "generation": generation,
            "binding_sha256": evidence["binding_sha256"],
        }
    except Exception:
        if manifest_target.exists():
            manifest_target.unlink()
        if artifact_root.exists():
            shutil.rmtree(artifact_root)
        raise


def _verify_evidence_record(
    run_dir: Path,
    record: dict[str, Any],
    criterion: dict[str, Any],
    runtime: dict[str, Any],
    reference_time: datetime,
) -> None:
    manifest_path = resolve_under(run_dir, record["manifest_path"], "evidence manifest")
    if not manifest_path.is_file() or sha256_file(manifest_path) != record["manifest_sha256"]:
        raise AcceptanceError(f"Evidence manifest drift: {record['evidence_id']}")
    evidence = load_json(manifest_path, "evidence snapshot")
    if evidence.get("evidence_id") != record["evidence_id"] or evidence.get("status") != record["status"]:
        raise AcceptanceError(f"Evidence record mismatch: {record['evidence_id']}")
    if evidence.get("runtime", {}).get("helper_sha256") != runtime["helper_sha256"]:
        raise AcceptanceError(f"Evidence helper drift: {record['evidence_id']}")
    if evidence.get("runtime", {}).get("skill_manifest_sha256") != runtime["skill_manifest_sha256"]:
        raise AcceptanceError(f"Evidence skill manifest drift: {record['evidence_id']}")
    observed = parse_utc(evidence["observed_at"])
    if (reference_time - observed).total_seconds() > criterion["max_age_seconds"]:
        raise AcceptanceError(f"Evidence expired before close: {record['evidence_id']}")
    artifact_root = resolve_under(run_dir, record["artifact_root"], "evidence artifact root")
    _validate_artifacts(evidence["artifacts"], artifact_root, "snapshot artifacts")


def command_doctor(_: argparse.Namespace) -> dict[str, Any]:
    identity = runtime_identity()
    return {
        "status": "READY",
        "skill": "accept-system",
        "version": VERSION,
        **identity,
        "state_root": str(DEFAULT_STATE_ROOT.resolve()),
        "network_calls": 0,
        "model_calls": 0,
        "writes_performed": 0,
    }


def command_validate(args: argparse.Namespace) -> dict[str, Any]:
    contract = load_json(Path(args.contract).resolve(), "acceptance contract")
    planning = load_json(Path(args.planning_contract).resolve(), "planning contract")
    graph = load_json(Path(args.graph).resolve(), "Agent Graph")
    result = validate_contract_bindings(contract, planning, graph)
    return {**result, "writes_performed": 0}


def command_init(args: argparse.Namespace) -> dict[str, Any]:
    contract_path = Path(args.contract).resolve()
    planning_path = Path(args.planning_contract).resolve()
    graph_path = Path(args.graph).resolve()
    contract = load_json(contract_path, "acceptance contract")
    planning = load_json(planning_path, "planning contract")
    graph = load_json(graph_path, "Agent Graph")
    binding_result = validate_contract_bindings(contract, planning, graph)
    graph_run_id = require_id(args.graph_run_id, GRAPH_RUN_ID_RE, "graph_run_id")
    state_root = resolve_state_root(args.state_root)
    state_root.mkdir(parents=True, exist_ok=True)
    run_id = f"run-accept-{compact_utc()}-{uuid.uuid4().hex[:10]}"
    run_dir = state_root / run_id
    staging = state_root / f".init-{run_id}-{uuid.uuid4().hex}"
    runtime = runtime_identity()
    try:
        staging.mkdir(parents=False, exist_ok=False)
        (staging / "contracts").mkdir()
        atomic_write_json(staging / "contracts" / "acceptance.json", contract, exclusive=True)
        atomic_write_json(staging / "contracts" / "planning.json", planning, exclusive=True)
        atomic_write_json(staging / "contracts" / "graph.json", graph, exclusive=True)
        created = utc_now()
        state: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "graph_run_id": graph_run_id,
            "acceptance_id": contract["acceptance_id"],
            "status": "READY",
            "final": False,
            "created_at": created,
            "updated_at": created,
            "acceptance_contract_hash": binding_result["acceptance_contract_hash"],
            "planning_contract_hash": binding_result["planning_contract_hash"],
            "graph_hash": binding_result["graph_hash"],
            **_frozen_acceptance_inputs(contract, planning),
            "runtime": runtime,
            "max_generations": contract["max_generations"],
            "current_generation": 0,
            "current_binding_sha256": None,
            "generations": [],
            "historical_failures": [],
            "evaluations": [],
            "final_receipt": None,
            "operations": {},
            "event_count": 0,
            "event_head": None,
        }
        response = {
            "status": "INITIALIZED",
            "run_id": run_id,
            "graph_run_id": graph_run_id,
            "run_dir": str(run_dir.resolve()),
            "acceptance_contract_hash": binding_result["acceptance_contract_hash"],
            "planning_contract_hash": binding_result["planning_contract_hash"],
            "graph_hash": binding_result["graph_hash"],
            "writes_performed": 4,
        }
        event_base = _event_without_hash(state, "op-init-bootstrap", "init", response)
        event = {**event_base, "event_hash": sha256_value(event_base)}
        state["event_count"] = 1
        state["event_head"] = event["event_hash"]
        state["operations"]["op-init-bootstrap"] = copy.deepcopy(response)
        with (staging / "events.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(event, ensure_ascii=True, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        atomic_write_json(staging / "state.json", state, exclusive=True)
        if run_dir.exists():
            raise AcceptanceError(f"Run already exists: {run_id}")
        staging.rename(run_dir)
        return response
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def command_start(args: argparse.Namespace) -> dict[str, Any]:
    state_root = resolve_state_root(args.state_root)
    run_dir = resolve_run_dir(state_root, args.run_id)
    binding_path = Path(args.binding).resolve()
    binding = load_json(binding_path, "execution binding")
    with run_lock(run_dir):
        _recover_pending_locked(run_dir)
        state = _load_state(run_dir)
        cached = _cached_operation(state, args.operation_id)
        if cached is not None:
            return cached
        _assert_runtime(state)
        if state["final"] or state["status"] != "READY" or state["current_generation"] != 0:
            raise AcceptanceError("Run can start exactly once from READY")
        contract = load_json(run_dir / "contracts" / "acceptance.json", "acceptance snapshot")
        validate_binding(binding, contract)
        binding_hash = sha256_value(binding)
        binding_relative = "bindings/g1.json"
        atomic_write_json(run_dir / binding_relative, binding, exclusive=True)
        now = utc_now()
        generation = {
            "generation": 1,
            "started_at": now,
            "binding_sha256": binding_hash,
            "binding_path": binding_relative,
            "criteria": _initial_criteria(contract),
            "closed_at": None,
        }
        new_state = copy.deepcopy(state)
        new_state["status"] = "RUNNING"
        new_state["current_generation"] = 1
        new_state["current_binding_sha256"] = binding_hash
        new_state["generations"].append(generation)
        response = {
            "status": "STARTED",
            "run_id": args.run_id,
            "generation": 1,
            "binding_sha256": binding_hash,
            "writes_performed": 3,
        }
        return _commit_mutation(run_dir, state, new_state, args.operation_id, "start", response)


def command_result(args: argparse.Namespace) -> dict[str, Any]:
    state_root = resolve_state_root(args.state_root)
    run_dir = resolve_run_dir(state_root, args.run_id)
    evidence_path = Path(args.evidence).resolve()
    evidence = load_json(evidence_path, "acceptance evidence")
    with run_lock(run_dir):
        _recover_pending_locked(run_dir)
        state = _load_state(run_dir)
        cached = _cached_operation(state, args.operation_id)
        if cached is not None:
            return cached
        _assert_runtime(state)
        if state["final"] or state["status"] != "RUNNING":
            raise AcceptanceError("Evidence can be recorded only in a non-final RUNNING run")
        contract = load_json(run_dir / "contracts" / "acceptance.json", "acceptance snapshot")
        criteria = _criterion_map(contract)
        if args.criterion_id not in criteria:
            raise AcceptanceError(f"Unknown criterion: {args.criterion_id}")
        criterion = criteria[args.criterion_id]
        if criterion["applicability"] != "applicable":
            raise AcceptanceError("Cannot record result for frozen not-applicable criterion")
        generation = _current_generation(state)
        current = generation["criteria"][args.criterion_id]
        if current["status"] != "PENDING":
            raise AcceptanceError("Criterion already has a result in this generation")
        normalized = validate_evidence(evidence, evidence_path, criterion, contract, state)
        record = _copy_evidence_into_run(run_dir, normalized, evidence_path)
        new_state = copy.deepcopy(state)
        target_generation = new_state["generations"][-1]
        target_generation["criteria"][args.criterion_id] = {
            "status": normalized["status"],
            "authority": None,
            "evidence": record,
        }
        if normalized["status"] in {"FAIL", "BLOCKED"}:
            new_state["historical_failures"].append(
                {
                    "evidence_id": normalized["evidence_id"],
                    "criterion_id": args.criterion_id,
                    "generation": state["current_generation"],
                    "status": normalized["status"],
                    "resolved_by_generation": None,
                    "resolution_sha256": None,
                }
            )
        response = {
            "status": "RESULT_RECORDED",
            "run_id": args.run_id,
            "generation": state["current_generation"],
            "criterion_id": args.criterion_id,
            "verdict": normalized["status"],
            "evidence_id": normalized["evidence_id"],
            "manifest_sha256": record["manifest_sha256"],
            "writes_performed": 3,
        }
        return _commit_mutation(run_dir, state, new_state, args.operation_id, "result", response)


def _validate_resolution(
    resolution: dict[str, Any], resolution_path: Path, expected_failures: set[str]
) -> dict[str, Any]:
    reject_sensitive_or_score_keys(resolution, "repair resolution")
    require_exact_keys(
        resolution,
        {"schema_version", "reason", "resolved_evidence_ids", "changed_paths", "artifacts"},
        set(),
        "repair resolution",
    )
    if resolution["schema_version"] != SCHEMA_VERSION:
        raise AcceptanceError("Unsupported repair resolution schema")
    require_string(resolution["reason"], "repair resolution.reason", 8, 4000)
    resolved = set(require_unique_strings(resolution["resolved_evidence_ids"], "repair resolution.resolved_evidence_ids", 128, 1))
    if resolved != expected_failures:
        missing = sorted(expected_failures - resolved)
        extra = sorted(resolved - expected_failures)
        raise AcceptanceError(f"Repair must resolve exactly current failures; missing={missing}; extra={extra}")
    paths = require_unique_strings(resolution["changed_paths"], "repair resolution.changed_paths", 256, 1)
    for index, path in enumerate(paths):
        normalize_relative_path(path, f"repair resolution.changed_paths[{index}]")
    _validate_artifacts(resolution["artifacts"], resolution_path.parent.resolve(), "repair resolution.artifacts")
    return resolution


def _validate_evidence_only_resolution(
    resolution: dict[str, Any], resolution_path: Path, expected_failures: set[str]
) -> dict[str, Any]:
    reject_sensitive_or_score_keys(resolution, "evidence-only repair resolution")
    require_exact_keys(
        resolution,
        {
            "schema_version",
            "reason",
            "resolved_evidence_ids",
            "preserve_pass_evidence_ids",
            "changed_paths",
            "packet_paths",
            "artifacts",
        },
        set(),
        "evidence-only repair resolution",
    )
    if resolution["schema_version"] != SCHEMA_VERSION:
        raise AcceptanceError("Unsupported evidence-only repair resolution schema")
    require_string(resolution["reason"], "evidence-only repair resolution.reason", 8, 4000)
    resolved = set(
        require_unique_strings(
            resolution["resolved_evidence_ids"],
            "evidence-only repair resolution.resolved_evidence_ids",
            128,
            1,
        )
    )
    if resolved != expected_failures:
        missing = sorted(expected_failures - resolved)
        extra = sorted(resolved - expected_failures)
        raise AcceptanceError(
            f"Evidence-only repair must resolve exactly current failures; missing={missing}; extra={extra}"
        )
    require_unique_strings(
        resolution["preserve_pass_evidence_ids"],
        "evidence-only repair resolution.preserve_pass_evidence_ids",
        128,
        0,
    )
    changed_paths = require_unique_strings(
        resolution["changed_paths"], "evidence-only repair resolution.changed_paths", 256, 0
    )
    if changed_paths:
        raise AcceptanceError(
            "Evidence-only rebind cannot preserve PASS when product paths changed; use full repair"
        )
    packet_paths = require_unique_strings(
        resolution["packet_paths"], "evidence-only repair resolution.packet_paths", 256, 0
    )
    for index, path in enumerate(packet_paths):
        normalize_relative_path(path, f"evidence-only repair resolution.packet_paths[{index}]")
    _validate_artifacts(
        resolution["artifacts"],
        resolution_path.parent.resolve(),
        "evidence-only repair resolution.artifacts",
    )
    return resolution


def _verify_preserved_pass(
    run_dir: Path,
    record: dict[str, Any],
    criterion: dict[str, Any],
    state: dict[str, Any],
    source_generation: int,
    binding_sha256: str,
    reference_time: datetime,
) -> None:
    if record.get("status") != "PASS":
        raise AcceptanceError("Evidence-only rebind can preserve PASS evidence only")
    if record.get("generation") != source_generation:
        raise AcceptanceError("Preserved evidence generation is not current")
    if record.get("binding_sha256") != binding_sha256:
        raise AcceptanceError("Preserved evidence binding hash differs from current binding")
    _verify_evidence_record(run_dir, record, criterion, state["runtime"], reference_time)
    manifest_path = resolve_under(run_dir, record["manifest_path"], "evidence manifest")
    evidence = load_json(manifest_path, "evidence snapshot")
    if evidence.get("criterion_id") != criterion["criterion_id"]:
        raise AcceptanceError("Preserved evidence criterion mismatch")
    if evidence.get("generation") != source_generation:
        raise AcceptanceError("Preserved evidence snapshot generation mismatch")
    if evidence.get("binding_sha256") != binding_sha256:
        raise AcceptanceError("Preserved evidence snapshot binding hash mismatch")
    reviewer = validate_identity(evidence.get("reviewer"), "preserved evidence.reviewer")
    independence = criterion["reviewer_independence"]
    if reviewer["role"] not in independence["allowed_roles"]:
        raise AcceptanceError("Preserved reviewer role is not allowed for this criterion")
    maker = load_json(run_dir / "contracts" / "acceptance.json", "acceptance snapshot")["maker"]
    if independence["not_same_actor"] and reviewer["id"] == maker["id"]:
        raise AcceptanceError("Preserved evidence reviewer cannot be the maker")
    if independence["not_same_lineage"] and reviewer["lineage"] == maker["lineage"]:
        raise AcceptanceError("Preserved evidence reviewer lineage must differ from maker lineage")
    oracle_result = evidence.get("oracle_result")
    if not isinstance(oracle_result, dict):
        raise AcceptanceError("Preserved evidence oracle result is missing")
    if oracle_result.get("expected") != criterion["oracle"]["expected"]:
        raise AcceptanceError("Preserved evidence oracle expected value changed")
    if oracle_result.get("method") != criterion["oracle"]["method"]:
        raise AcceptanceError("Preserved evidence oracle method changed")


def command_repair(args: argparse.Namespace) -> dict[str, Any]:
    state_root = resolve_state_root(args.state_root)
    run_dir = resolve_run_dir(state_root, args.run_id)
    binding_path = Path(args.binding).resolve()
    resolution_path = Path(args.resolution).resolve()
    binding = load_json(binding_path, "replacement execution binding")
    resolution = load_json(resolution_path, "repair resolution")
    with run_lock(run_dir):
        _recover_pending_locked(run_dir)
        state = _load_state(run_dir)
        cached = _cached_operation(state, args.operation_id)
        if cached is not None:
            return cached
        _assert_runtime(state)
        if state["final"] or state["status"] != "RUNNING":
            raise AcceptanceError("Repair requires a non-final RUNNING run")
        if state["current_generation"] >= state["max_generations"]:
            raise AcceptanceError("Repair generation budget is exhausted")
        generation = _current_generation(state)
        failures = {
            item["evidence"]["evidence_id"]
            for item in generation["criteria"].values()
            if item["status"] in {"FAIL", "BLOCKED"} and item["evidence"]
        }
        if not failures:
            raise AcceptanceError("Repair requires a current FAIL or BLOCKED result")
        contract = load_json(run_dir / "contracts" / "acceptance.json", "acceptance snapshot")
        validate_binding(binding, contract)
        new_binding_hash = sha256_value(binding)
        if new_binding_hash == state["current_binding_sha256"]:
            raise AcceptanceError("Repair requires a changed execution binding")
        _validate_resolution(resolution, resolution_path, failures)
        next_generation = state["current_generation"] + 1
        binding_relative = f"bindings/g{next_generation}.json"
        resolution_relative = f"repairs/g{next_generation}/resolution.json"
        atomic_write_json(run_dir / binding_relative, binding, exclusive=True)
        atomic_write_json(run_dir / resolution_relative, resolution, exclusive=True)
        resolution_hash = sha256_value(resolution)
        artifact_root = run_dir / f"repairs/g{next_generation}/artifacts"
        artifact_root.mkdir(parents=True, exist_ok=False)
        for artifact in resolution["artifacts"]:
            source = resolve_under(resolution_path.parent.resolve(), artifact["path"], "repair artifact source")
            target = resolve_under(artifact_root, artifact["path"], "repair artifact target")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if sha256_file(target) != artifact["sha256"] or target.stat().st_size != artifact["size_bytes"]:
                raise AcceptanceError(f"Repair artifact copy verification failed: {artifact['path']}")
        new_state = copy.deepcopy(state)
        new_state["generations"][-1]["closed_at"] = utc_now()
        for failure in new_state["historical_failures"]:
            if failure["evidence_id"] in failures and failure["resolved_by_generation"] is None:
                failure["resolved_by_generation"] = next_generation
                failure["resolution_sha256"] = resolution_hash
        new_state["generations"].append(
            {
                "generation": next_generation,
                "started_at": utc_now(),
                "binding_sha256": new_binding_hash,
                "binding_path": binding_relative,
                "criteria": _initial_criteria(contract),
                "closed_at": None,
                "resolution_path": resolution_relative,
                "resolution_sha256": resolution_hash,
            }
        )
        new_state["current_generation"] = next_generation
        new_state["current_binding_sha256"] = new_binding_hash
        response = {
            "status": "REPAIR_GENERATION_STARTED",
            "run_id": args.run_id,
            "generation": next_generation,
            "binding_sha256": new_binding_hash,
            "resolved_evidence_ids": sorted(failures),
            "all_prior_passes_invalidated": True,
            "writes_performed": 4 + len(resolution["artifacts"]),
        }
        return _commit_mutation(run_dir, state, new_state, args.operation_id, "repair", response)


def command_evidence_only_rebind(args: argparse.Namespace) -> dict[str, Any]:
    state_root = resolve_state_root(args.state_root)
    run_dir = resolve_run_dir(state_root, args.run_id)
    binding_path = Path(args.binding).resolve()
    resolution_path = Path(args.resolution).resolve()
    binding = load_json(binding_path, "rebound execution binding")
    resolution = load_json(resolution_path, "evidence-only repair resolution")
    with run_lock(run_dir):
        _recover_pending_locked(run_dir)
        state = _load_state(run_dir)
        cached = _cached_operation(state, args.operation_id)
        if cached is not None:
            return cached
        _assert_runtime(state)
        if state["final"] or state["status"] != "RUNNING":
            raise AcceptanceError("Evidence-only rebind requires a non-final RUNNING run")
        if state["current_generation"] >= state["max_generations"]:
            raise AcceptanceError("Repair generation budget is exhausted")
        frozen = _assert_frozen_acceptance_inputs(run_dir, state)
        contract = frozen["contract"]
        generation = _current_generation(state)
        failures = {
            item["evidence"]["evidence_id"]
            for item in generation["criteria"].values()
            if item["status"] in {"FAIL", "BLOCKED"} and item["evidence"]
        }
        if not failures:
            raise AcceptanceError("Evidence-only rebind requires a current FAIL or BLOCKED result")
        validate_binding(binding, contract)
        new_binding_hash = sha256_value(binding)
        if new_binding_hash != state["current_binding_sha256"]:
            raise AcceptanceError(
                "Evidence-only rebind requires an unchanged product binding; use full repair"
            )
        current_binding = load_json(
            resolve_under(run_dir, generation["binding_path"], "current binding"),
            "current binding",
        )
        if sha256_value(current_binding) != state["current_binding_sha256"]:
            raise AcceptanceError("Current binding snapshot drifted; use full repair")
        _validate_evidence_only_resolution(resolution, resolution_path, failures)
        preserve_ids = set(resolution["preserve_pass_evidence_ids"])
        pass_records: dict[str, tuple[str, dict[str, Any]]] = {}
        for criterion_id, value in generation["criteria"].items():
            if value["status"] == "PASS" and value["evidence"]:
                evidence_id = value["evidence"]["evidence_id"]
                pass_records[evidence_id] = (criterion_id, value["evidence"])
        missing_passes = sorted(preserve_ids - set(pass_records))
        if missing_passes:
            raise AcceptanceError(
                f"Evidence-only rebind references missing current PASS evidence: {missing_passes}"
            )
        criteria = _criterion_map(contract)
        reference_time = datetime.now(timezone.utc)
        for evidence_id in sorted(preserve_ids):
            criterion_id, record = pass_records[evidence_id]
            _verify_preserved_pass(
                run_dir,
                record,
                criteria[criterion_id],
                state,
                state["current_generation"],
                state["current_binding_sha256"],
                reference_time,
            )

        next_generation = state["current_generation"] + 1
        binding_relative = f"bindings/g{next_generation}.json"
        resolution_relative = f"repairs/g{next_generation}/resolution.json"
        atomic_write_json(run_dir / binding_relative, binding, exclusive=True)
        atomic_write_json(run_dir / resolution_relative, resolution, exclusive=True)
        resolution_hash = sha256_value(resolution)
        artifact_root = run_dir / f"repairs/g{next_generation}/artifacts"
        artifact_root.mkdir(parents=True, exist_ok=False)
        for artifact in resolution["artifacts"]:
            source = resolve_under(
                resolution_path.parent.resolve(), artifact["path"], "repair artifact source"
            )
            target = resolve_under(artifact_root, artifact["path"], "repair artifact target")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if sha256_file(target) != artifact["sha256"] or target.stat().st_size != artifact["size_bytes"]:
                raise AcceptanceError(f"Repair artifact copy verification failed: {artifact['path']}")
        new_state = copy.deepcopy(state)
        new_state["generations"][-1]["closed_at"] = utc_now()
        for failure in new_state["historical_failures"]:
            if failure["evidence_id"] in failures and failure["resolved_by_generation"] is None:
                failure["resolved_by_generation"] = next_generation
                failure["resolution_sha256"] = resolution_hash
        next_criteria = _initial_criteria(contract)
        for criterion_id, value in generation["criteria"].items():
            if value["status"] == "PASS" and value["evidence"] and value["evidence"]["evidence_id"] in preserve_ids:
                next_criteria[criterion_id] = copy.deepcopy(value)
        new_state["generations"].append(
            {
                "generation": next_generation,
                "started_at": utc_now(),
                "binding_sha256": new_binding_hash,
                "binding_path": binding_relative,
                "criteria": next_criteria,
                "closed_at": None,
                "resolution_path": resolution_relative,
                "resolution_sha256": resolution_hash,
            }
        )
        new_state["current_generation"] = next_generation
        new_state["current_binding_sha256"] = new_binding_hash
        response = {
            "status": "EVIDENCE_ONLY_REBIND_STARTED",
            "run_id": args.run_id,
            "generation": next_generation,
            "binding_sha256": new_binding_hash,
            "resolved_evidence_ids": sorted(failures),
            "preserved_evidence_ids": sorted(preserve_ids),
            "all_prior_passes_invalidated": False,
            "writes_performed": 4 + len(resolution["artifacts"]),
        }
        return _commit_mutation(
            run_dir, state, new_state, args.operation_id, "evidence-only-rebind", response
        )


def _evaluate_state(
    run_dir: Path, state: dict[str, Any], contract: dict[str, Any], reference_time: datetime
) -> dict[str, Any]:
    generation = _current_generation(state)
    criteria = _criterion_map(contract)
    blocking: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    for criterion_id in sorted(criteria):
        criterion = criteria[criterion_id]
        value = generation["criteria"][criterion_id]
        status = value["status"]
        evidence = value["evidence"]
        stale_or_invalid: str | None = None
        if evidence is not None:
            try:
                _verify_evidence_record(run_dir, evidence, criterion, state["runtime"], reference_time)
            except AcceptanceError as exc:
                stale_or_invalid = str(exc)
        effective_status = "INVALID" if stale_or_invalid else status
        results.append(
            {
                "criterion_id": criterion_id,
                "mandatory": criterion["mandatory"],
                "applicability": criterion["applicability"],
                "status": effective_status,
                "evidence_id": evidence["evidence_id"] if evidence else None,
                "reason": stale_or_invalid,
            }
        )
        if criterion["mandatory"] and criterion["applicability"] == "applicable" and effective_status != "PASS":
            blocking.append(
                {
                    "criterion_id": criterion_id,
                    "status": effective_status,
                    "reason": stale_or_invalid or f"current status is {status}",
                }
            )
    unresolved_history = [
        item for item in state["historical_failures"] if item["resolved_by_generation"] is None
    ]
    if not blocking and unresolved_history:
        blocking.append(
            {
                "criterion_id": "historical-failures",
                "status": "UNRESOLVED",
                "reason": "historical FAIL/BLOCKED entries lack an explicit repair resolution",
            }
        )
    return {
        "verdict": "ACCEPTED" if not blocking else "NOT_ACCEPTED",
        "criteria": results,
        "blocking": blocking,
        "unresolved_history": unresolved_history,
    }


def _build_receipt(
    run_dir: Path,
    state: dict[str, Any],
    contract: dict[str, Any],
    evaluation: dict[str, Any],
    verdict: str,
    stop_reason: str,
    closed_at: str,
) -> dict[str, Any]:
    generation = _current_generation(state)
    criteria_records: list[dict[str, Any]] = []
    for item in evaluation["criteria"]:
        state_value = generation["criteria"][item["criterion_id"]]
        criteria_records.append(
            {
                **item,
                "evidence": copy.deepcopy(state_value["evidence"]),
                "n_a_authority": copy.deepcopy(state_value["authority"]),
            }
        )
    base = {
        "schema_version": SCHEMA_VERSION,
        "receipt_id": f"receipt-{state['run_id'].removeprefix('run-accept-')}",
        "run_id": state["run_id"],
        "graph_run_id": state["graph_run_id"],
        "run_root": str(run_dir.resolve()),
        "acceptance_id": state["acceptance_id"],
        "acceptance_contract_hash": state["acceptance_contract_hash"],
        "planning_contract_hash": state["planning_contract_hash"],
        "graph_hash": state["graph_hash"],
        "final_verdict": verdict,
        "stop_reason": stop_reason,
        "closed_at": closed_at,
        "final_generation": state["current_generation"],
        "binding_sha256": state["current_binding_sha256"],
        "helper_sha256": state["runtime"]["helper_sha256"],
        "skill_manifest_sha256": state["runtime"]["skill_manifest_sha256"],
        "criteria_results": criteria_records,
        "historical_failures": copy.deepcopy(state["historical_failures"]),
        "evaluation_history": copy.deepcopy(state["evaluations"]),
        "event_chain_head_before_close": state["event_head"],
    }
    reject_sensitive_or_score_keys(base, "receipt")
    return {**base, "receipt_hash": sha256_value(base)}


def command_close(args: argparse.Namespace) -> dict[str, Any]:
    state_root = resolve_state_root(args.state_root)
    run_dir = resolve_run_dir(state_root, args.run_id)
    with run_lock(run_dir):
        _recover_pending_locked(run_dir)
        state = _load_state(run_dir)
        cached = _cached_operation(state, args.operation_id)
        if cached is not None:
            return cached
        _assert_runtime(state)
        if state["final"]:
            raise AcceptanceError("Final receipt already exists for this run")
        if state["status"] != "RUNNING":
            raise AcceptanceError("Close requires a RUNNING run")
        contract = load_json(run_dir / "contracts" / "acceptance.json", "acceptance snapshot")
        now = datetime.now(timezone.utc)
        evaluation = _evaluate_state(run_dir, state, contract, now)
        stop_reason_arg = args.stop_reason
        if stop_reason_arg is not None:
            require_string(stop_reason_arg, "stop_reason", 4, 1000)
        if evaluation["verdict"] == "ACCEPTED" and stop_reason_arg:
            raise AcceptanceError("Do not provide stop_reason for an accepted run")
        exhausted = state["current_generation"] >= state["max_generations"]
        final = evaluation["verdict"] == "ACCEPTED" or exhausted or bool(stop_reason_arg)
        new_state = copy.deepcopy(state)
        if not final:
            evaluation_record = {
                "evaluation_id": f"evaluation-g{state['current_generation']}-{len(state['evaluations']) + 1}",
                "generation": state["current_generation"],
                "verdict": "NOT_ACCEPTED",
                "blocking": evaluation["blocking"],
                "evaluated_at": utc_now(),
                "binding_sha256": state["current_binding_sha256"],
            }
            evaluation_record["evaluation_sha256"] = sha256_value(evaluation_record)
            new_state["evaluations"].append(evaluation_record)
            response = {
                "status": "NOT_ACCEPTED",
                "final": False,
                "run_id": args.run_id,
                "generation": state["current_generation"],
                "blocking": evaluation["blocking"],
                "repair_generations_remaining": state["max_generations"] - state["current_generation"],
                "writes_performed": 2,
            }
            return _commit_mutation(run_dir, state, new_state, args.operation_id, "evaluate-not-accepted", response)

        verdict = evaluation["verdict"]
        stop_reason = (
            "all-mandatory-criteria-passed"
            if verdict == "ACCEPTED"
            else stop_reason_arg or "repair-generation-budget-exhausted"
        )
        closed_at = utc_now()
        receipt = _build_receipt(run_dir, state, contract, evaluation, verdict, stop_reason, closed_at)
        receipt_path = run_dir / "final-receipt.json"
        atomic_write_json(receipt_path, receipt, exclusive=True)
        new_state["status"] = verdict
        new_state["final"] = True
        new_state["generations"][-1]["closed_at"] = closed_at
        new_state["final_receipt"] = {
            "path": "final-receipt.json",
            "receipt_hash": receipt["receipt_hash"],
            "verdict": verdict,
        }
        response = {
            "status": verdict,
            "final": True,
            "run_id": args.run_id,
            "generation": state["current_generation"],
            "stop_reason": stop_reason,
            "receipt_path": str(receipt_path.resolve()),
            "receipt_hash": receipt["receipt_hash"],
            "blocking": evaluation["blocking"],
            "writes_performed": 3,
        }
        committed = _commit_mutation(run_dir, state, new_state, args.operation_id, "close-final", response)
        if args.output:
            output = Path(args.output).resolve()
            atomic_write_json(output, receipt, exclusive=True)
            committed = {**committed, "output": str(output), "writes_performed": committed["writes_performed"] + 1}
        return committed


def _event_head_exists(run_dir: Path, head: str | None) -> bool:
    if head is None:
        return False
    return any(event.get("event_hash") == head for event in _read_events(run_dir))


def verify_receipt_document(
    receipt_path: Path,
    expected_contract_hash: str | None = None,
    expected_graph_hash: str | None = None,
    expected_graph_run_id: str | None = None,
    require_accepted: bool = False,
    require_current_runtime: bool = True,
) -> dict[str, Any]:
    receipt = load_json(receipt_path.resolve(), "acceptance receipt")
    reject_sensitive_or_score_keys(receipt, "receipt")
    receipt_hash = receipt.get("receipt_hash")
    require_sha(receipt_hash, "receipt.receipt_hash")
    base = {key: value for key, value in receipt.items() if key != "receipt_hash"}
    if sha256_value(base) != receipt_hash:
        raise AcceptanceError("Acceptance receipt hash mismatch")
    if receipt.get("final_verdict") not in {"ACCEPTED", "NOT_ACCEPTED"}:
        raise AcceptanceError("Acceptance receipt has invalid final verdict")
    if require_accepted and receipt["final_verdict"] != "ACCEPTED":
        raise AcceptanceError("Agent Graph requires an ACCEPTED acceptance receipt")
    if expected_contract_hash and receipt.get("acceptance_contract_hash") != expected_contract_hash:
        raise AcceptanceError("Acceptance receipt contract hash mismatch")
    if expected_graph_hash and receipt.get("graph_hash") != expected_graph_hash:
        raise AcceptanceError("Acceptance receipt graph hash mismatch")
    graph_run_id = require_id(
        receipt.get("graph_run_id"), GRAPH_RUN_ID_RE, "receipt.graph_run_id"
    )
    if expected_graph_run_id:
        require_id(expected_graph_run_id, GRAPH_RUN_ID_RE, "expected_graph_run_id")
        if graph_run_id != expected_graph_run_id:
            raise AcceptanceError("Acceptance receipt Agent Graph run_id mismatch")
    run_dir = Path(require_string(receipt.get("run_root"), "receipt.run_root", 3, 2000)).resolve()
    stored_path = run_dir / "final-receipt.json"
    if not stored_path.is_file() or sha256_file(stored_path) != sha256_file(receipt_path.resolve()):
        raise AcceptanceError("Receipt is not identical to the stored final receipt")
    contract = load_json(run_dir / "contracts" / "acceptance.json", "acceptance snapshot")
    if sha256_value(contract) != receipt["acceptance_contract_hash"]:
        raise AcceptanceError("Acceptance contract snapshot drift")
    graph = load_json(run_dir / "contracts" / "graph.json", "graph snapshot")
    if sha256_value(graph) != receipt["graph_hash"]:
        raise AcceptanceError("Graph snapshot drift")
    planning = load_json(run_dir / "contracts" / "planning.json", "planning snapshot")
    planning_hash = planning.get("approval", {}).get("contract_hash") or planning.get(
        "contract_hash"
    )
    if planning_hash != receipt["planning_contract_hash"]:
        raise AcceptanceError("Planning contract snapshot drift")
    chain = verify_event_chain(run_dir)
    if not _event_head_exists(run_dir, receipt.get("event_chain_head_before_close")):
        raise AcceptanceError("Receipt event-chain checkpoint is missing")
    if require_current_runtime:
        runtime = runtime_identity()
        if runtime["helper_sha256"] != receipt["helper_sha256"]:
            raise AcceptanceError("Current helper differs from receipt")
        if runtime["skill_manifest_sha256"] != receipt["skill_manifest_sha256"]:
            raise AcceptanceError("Current skill manifest differs from receipt")
    runtime_record = {
        "helper_sha256": receipt["helper_sha256"],
        "skill_manifest_sha256": receipt["skill_manifest_sha256"],
    }
    criteria = _criterion_map(contract)
    closed_at = parse_utc(receipt["closed_at"])
    seen: set[str] = set()
    for item in receipt.get("criteria_results", []):
        criterion_id = item.get("criterion_id")
        if criterion_id not in criteria or criterion_id in seen:
            raise AcceptanceError(f"Receipt has unknown or duplicate criterion: {criterion_id}")
        seen.add(criterion_id)
        if item.get("evidence") is not None:
            _verify_evidence_record(run_dir, item["evidence"], criteria[criterion_id], runtime_record, closed_at)
    if seen != set(criteria):
        raise AcceptanceError("Receipt does not contain every criterion")
    if receipt["final_verdict"] == "ACCEPTED":
        for item in receipt["criteria_results"]:
            criterion = criteria[item["criterion_id"]]
            if criterion["mandatory"] and criterion["applicability"] == "applicable" and item["status"] != "PASS":
                raise AcceptanceError("Accepted receipt contains a non-PASS mandatory criterion")
        unresolved = [item for item in receipt["historical_failures"] if item.get("resolved_by_generation") is None]
        if unresolved:
            raise AcceptanceError("Accepted receipt contains unresolved historical failures")
    return {
        "status": "VERIFIED",
        "receipt_hash": receipt_hash,
        "final_verdict": receipt["final_verdict"],
        "run_id": receipt["run_id"],
        "graph_run_id": graph_run_id,
        "graph_hash": receipt["graph_hash"],
        "event_count": chain["count"],
        "writes_performed": 0,
    }


def command_verify_receipt(args: argparse.Namespace) -> dict[str, Any]:
    return verify_receipt_document(
        Path(args.receipt).resolve(),
        expected_contract_hash=args.acceptance_contract_hash,
        expected_graph_hash=args.graph_hash,
        expected_graph_run_id=args.graph_run_id,
        require_accepted=args.require_accepted,
        require_current_runtime=not args.allow_runtime_drift,
    )


def command_status(args: argparse.Namespace) -> dict[str, Any]:
    state_root = resolve_state_root(args.state_root)
    run_dir = resolve_run_dir(state_root, args.run_id)
    state = _load_state(run_dir)
    chain = verify_event_chain(run_dir)
    generation = state["generations"][-1] if state["generations"] else None
    counts: dict[str, int] = {}
    if generation:
        for value in generation["criteria"].values():
            counts[value["status"]] = counts.get(value["status"], 0) + 1
    return {
        "status": state["status"],
        "final": state["final"],
        "run_id": state["run_id"],
        "generation": state["current_generation"],
        "max_generations": state["max_generations"],
        "criteria_counts": counts,
        "historical_failure_count": len(state["historical_failures"]),
        "event_count": chain["count"],
        "event_head": chain["head"],
        "final_receipt": state["final_receipt"],
        "writes_performed": 0,
    }


def command_recover(args: argparse.Namespace) -> dict[str, Any]:
    state_root = resolve_state_root(args.state_root)
    run_dir = resolve_run_dir(state_root, args.run_id)
    with run_lock(run_dir):
        recovered = _recover_pending_locked(run_dir)
        state = _load_state(run_dir)
        chain = verify_event_chain(run_dir)
        if chain["count"] != state["event_count"] or chain["head"] != state["event_head"]:
            raise AcceptanceError("State and event chain differ after recovery")
        return {
            "status": "RECOVERED" if recovered else "CONSISTENT",
            "run_id": args.run_id,
            "pending_transactions_recovered": recovered,
            "event_count": chain["count"],
            "writes_performed": 1 if recovered else 0,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor")
    doctor.set_defaults(handler=command_doctor)

    validate = subparsers.add_parser("validate")
    validate.add_argument("--contract", required=True)
    validate.add_argument("--planning-contract", required=True)
    validate.add_argument("--graph", required=True)
    validate.set_defaults(handler=command_validate)

    init = subparsers.add_parser("init")
    init.add_argument("--contract", required=True)
    init.add_argument("--planning-contract", required=True)
    init.add_argument("--graph", required=True)
    init.add_argument("--graph-run-id", required=True)
    init.add_argument("--state-root")
    init.set_defaults(handler=command_init)

    for action, handler in (
        ("start", command_start),
        ("result", command_result),
        ("repair", command_repair),
        ("evidence-only-rebind", command_evidence_only_rebind),
        ("close", command_close),
    ):
        command = subparsers.add_parser(action)
        command.add_argument("--run-id", required=True)
        command.add_argument("--operation-id", required=True)
        command.add_argument("--state-root")
        command.set_defaults(handler=handler)
    subparsers.choices["start"].add_argument("--binding", required=True)
    subparsers.choices["result"].add_argument("--criterion-id", required=True)
    subparsers.choices["result"].add_argument("--evidence", required=True)
    subparsers.choices["repair"].add_argument("--binding", required=True)
    subparsers.choices["repair"].add_argument("--resolution", required=True)
    subparsers.choices["evidence-only-rebind"].add_argument("--binding", required=True)
    subparsers.choices["evidence-only-rebind"].add_argument("--resolution", required=True)
    subparsers.choices["close"].add_argument("--stop-reason")
    subparsers.choices["close"].add_argument("--output")

    status = subparsers.add_parser("status")
    status.add_argument("--run-id", required=True)
    status.add_argument("--state-root")
    status.set_defaults(handler=command_status)

    recover = subparsers.add_parser("recover")
    recover.add_argument("--run-id", required=True)
    recover.add_argument("--state-root")
    recover.set_defaults(handler=command_recover)

    verify = subparsers.add_parser("verify-receipt")
    verify.add_argument("--receipt", required=True)
    verify.add_argument("--acceptance-contract-hash")
    verify.add_argument("--graph-hash")
    verify.add_argument("--graph-run-id")
    verify.add_argument("--require-accepted", action="store_true")
    verify.add_argument("--allow-runtime-drift", action="store_true")
    verify.set_defaults(handler=command_verify_receipt)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.handler(args)
    except AcceptanceError as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc), "writes_performed": 0}, ensure_ascii=True, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
