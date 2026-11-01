#!/usr/bin/env python3
"""Deterministic data-governance contract, graph-fragment and evidence gate."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


VERSION = "0.1.0"
SCHEMA_VERSION = 1
CONTRACT_SCHEMA_VERSION = "1.0.0"
SKILL_ROOT = Path(__file__).resolve().parents[1]

CONTRACT_ID_RE = re.compile(r"^data-gov-[a-z0-9][a-z0-9-]{2,95}$")
PROJECT_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,95}$")
NAMED_ID_RE = re.compile(r"^[a-z][a-z0-9-]{2,127}$")
REQ_ID_RE = re.compile(r"^REQ-[0-9]{3,6}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
ISO_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")

TIERS = {"D0": 0, "D1": 1, "D2": 2, "D3": 3}
TRIGGERS = {
    "persistent-data",
    "schema-change",
    "etl-elt",
    "published-metric",
    "personal-data",
    "restricted-data",
    "cross-system",
    "migration",
    "backfill",
    "retention-deletion",
}
ASSET_KINDS = {
    "database",
    "schema",
    "table",
    "view",
    "file",
    "object-store",
    "topic",
    "api",
    "metric",
    "feature",
    "cache",
    "export",
    "report",
}
CLASSIFICATIONS = {"public", "internal", "confidential", "restricted"}
ENVIRONMENTS = {"DEV", "TEST", "STAGING", "PROD"}
SOURCE_KINDS = {
    "owner-decision",
    "primary-standard",
    "internal-policy",
    "schema",
    "repository",
    "live-observation",
}
DECISION_STATUSES = {"DECIDED", "APPROVED", "PENDING", "NOT_APPLICABLE"}
COMPATIBILITY = {"backward", "forward", "full", "breaking"}
QUALITY_DIMENSIONS = {
    "completeness",
    "validity",
    "uniqueness",
    "consistency",
    "timeliness",
    "freshness",
    "referential-integrity",
    "reconciliation",
    "distribution",
    "business-rule",
}
GATE_KINDS = {
    "classification-purpose",
    "data-contract",
    "privacy",
    "access-security",
    "quality-reconciliation",
    "migration-readiness",
    "retention-deletion",
    "data-acceptance",
}
GATE_PHASES = {"mutation", "publish", "cutover", "close"}
RESULT_STATUSES = {"PASS", "BLOCKED", "NOT_APPLICABLE"}
CHANGE_SCHEMA_VALUES = {"none", "additive-compatible", "breaking"}
MATERIALITY_VALUES = {"low", "material", "high-risk"}
SENSITIVE_KEYS = {
    "authorization",
    "cookie",
    "cookies",
    "credential",
    "credentials",
    "database_password",
    "password",
    "passwords",
    "secret",
    "secrets",
    "storage_state",
    "token",
    "tokens",
    "api_key",
    "apikey",
    "private_key",
}
RECORD_KEYS = {
    "record",
    "records",
    "rows",
    "row_values",
    "sample_record",
    "sample_records",
    "example_values",
    "raw_data",
}
SENSITIVE_PATH_RE = re.compile(
    r"(?:^|[._/\\-])(?:cookies?|credentials?|passwords?|secrets?|tokens?|storage[-_]?state)(?:$|[._/\\-])",
    re.I,
)


class GovernanceError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


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
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GovernanceError(f"Cannot read {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise GovernanceError(f"{label} must be a JSON object: {path}")
    return value


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def require_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GovernanceError(f"{label} must be an object")
    return value


def require_list(value: Any, label: str, *, nonempty: bool = False) -> list[Any]:
    if not isinstance(value, list):
        raise GovernanceError(f"{label} must be an array")
    if nonempty and not value:
        raise GovernanceError(f"{label} must not be empty")
    return value


def require_string(value: Any, label: str, *, min_length: int = 1) -> str:
    if not isinstance(value, str) or len(value.strip()) < min_length:
        raise GovernanceError(f"{label} must be a non-empty string")
    return value.strip()


def require_id(value: Any, label: str, pattern: re.Pattern[str] = NAMED_ID_RE) -> str:
    result = require_string(value, label)
    if not pattern.fullmatch(result):
        raise GovernanceError(f"{label} has invalid format: {result}")
    return result


def require_utc(value: Any, label: str) -> str:
    result = require_string(value, label)
    if not ISO_UTC_RE.fullmatch(result):
        raise GovernanceError(f"{label} must be an ISO-8601 UTC timestamp")
    return result


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def unique_strings(value: Any, label: str, *, allowed: set[str] | None = None, nonempty: bool = False) -> list[str]:
    items = require_list(value, label, nonempty=nonempty)
    result: list[str] = []
    for index, item in enumerate(items):
        text = require_string(item, f"{label}[{index}]")
        if allowed is not None and text not in allowed:
            raise GovernanceError(f"{label}[{index}] has unsupported value: {text}")
        result.append(text)
    if len(set(result)) != len(result):
        raise GovernanceError(f"{label} must contain unique values")
    return result


def reject_sensitive_payload(value: Any, path: str = "contract") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in SENSITIVE_KEYS:
                raise GovernanceError(f"sensitive field is forbidden in governance contract: {path}.{key}")
            if normalized in RECORD_KEYS:
                raise GovernanceError(f"record payload is forbidden in governance contract: {path}.{key}")
            reject_sensitive_payload(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            reject_sensitive_payload(item, f"{path}[{index}]")


def contract_hash(contract: dict[str, Any]) -> str:
    payload = copy.deepcopy(contract)
    payload.pop("contract_hash", None)
    return sha256_value(payload)


def classify_change(value: dict[str, Any]) -> dict[str, Any]:
    assessment = require_dict(value, "change assessment")
    if assessment.get("schema_version") != SCHEMA_VERSION:
        raise GovernanceError(f"schema_version must be {SCHEMA_VERSION}")
    require_id(assessment.get("project_slug"), "project_slug", PROJECT_SLUG_RE)
    require_string(assessment.get("summary"), "summary", min_length=8)
    boolean_fields = (
        "synthetic_only",
        "persistent_write",
        "new_consumer",
        "published_metric_change",
        "personal_or_restricted",
        "cross_system",
        "access_change",
        "migration_or_backfill",
        "retention_or_delete",
        "production",
    )
    for field in boolean_fields:
        if not isinstance(assessment.get(field), bool):
            raise GovernanceError(f"{field} must be boolean")
    schema_change = require_string(assessment.get("schema_change"), "schema_change")
    if schema_change not in CHANGE_SCHEMA_VALUES:
        raise GovernanceError(f"schema_change must be one of {sorted(CHANGE_SCHEMA_VALUES)}")

    material_flags = {
        "new-consumer": assessment["new_consumer"],
        "published-metric": assessment["published_metric_change"],
        "cross-system": assessment["cross_system"],
        "access-change": assessment["access_change"],
        "migration-backfill": assessment["migration_or_backfill"],
        "retention-delete": assessment["retention_or_delete"],
        "breaking-schema": schema_change == "breaking",
    }
    material_reasons = sorted(name for name, enabled in material_flags.items() if enabled)
    if assessment["personal_or_restricted"]:
        tier = "D3"
    elif assessment["production"] and material_reasons:
        tier = "D3"
    elif material_reasons:
        tier = "D2"
    elif assessment["persistent_write"] or schema_change == "additive-compatible":
        tier = "D1"
    elif assessment["synthetic_only"] and not assessment["production"]:
        tier = "D0"
    else:
        raise GovernanceError("change does not have enough evidence for D0 and has no declared governance trigger")
    return {
        "status": "CLASSIFIED",
        "tier": tier,
        "activation": "SKIP" if tier == "D0" else ("FAST_PATH" if tier == "D1" else "FULL"),
        "contract_profile": None if tier == "D0" else ("compact" if tier == "D1" else "full"),
        "material": tier in {"D2", "D3"},
        "reasons": material_reasons
        + (["personal-or-restricted"] if assessment["personal_or_restricted"] else [])
        + (["persistent-write"] if assessment["persistent_write"] else [])
        + ([f"schema:{schema_change}"] if schema_change != "none" else []),
        "fail_closed_rule": "Any unknown signal must be resolved before classification.",
    }


def _validate_sources(contract: dict[str, Any]) -> set[str]:
    source_ids: set[str] = set()
    for index, raw in enumerate(require_list(contract.get("sources"), "sources", nonempty=True)):
        source = require_dict(raw, f"sources[{index}]")
        source_id = require_id(source.get("source_id"), f"sources[{index}].source_id")
        if source_id in source_ids:
            raise GovernanceError(f"duplicate source_id: {source_id}")
        source_ids.add(source_id)
        kind = require_string(source.get("kind"), f"sources[{index}].kind")
        if kind not in SOURCE_KINDS:
            raise GovernanceError(f"sources[{index}].kind is unsupported: {kind}")
        require_string(source.get("reference"), f"sources[{index}].reference", min_length=4)
        require_string(source.get("version_or_commit"), f"sources[{index}].version_or_commit")
        require_utc(source.get("observed_at"), f"sources[{index}].observed_at")
        if source.get("sha256") is not None and not SHA256_RE.fullmatch(str(source["sha256"])):
            raise GovernanceError(f"sources[{index}].sha256 must be lowercase SHA-256")
    return source_ids


def _validate_decision(value: Any, label: str, source_ids: set[str], *, allow_pending: bool = True) -> str:
    decision = require_dict(value, label)
    status = require_string(decision.get("status"), f"{label}.status")
    if status not in DECISION_STATUSES or (not allow_pending and status == "PENDING"):
        raise GovernanceError(f"{label}.status is unsupported: {status}")
    require_string(decision.get("decision_owner") or decision.get("owner"), f"{label}.decision_owner")
    source_ref = require_string(decision.get("source_ref"), f"{label}.source_ref")
    if source_ref not in source_ids:
        raise GovernanceError(f"{label}.source_ref does not exist: {source_ref}")
    require_utc(decision.get("verified_at"), f"{label}.verified_at")
    if status == "NOT_APPLICABLE":
        require_string(decision.get("rationale"), f"{label}.rationale", min_length=8)
    return status


def validate_contract(contract: dict[str, Any], phase: str = "ready") -> dict[str, Any]:
    if phase not in {"draft", "ready"}:
        raise GovernanceError(f"Unsupported validation phase: {phase}")
    require_dict(contract, "contract")
    reject_sensitive_payload(contract)
    if contract.get("schema_version") != SCHEMA_VERSION:
        raise GovernanceError(f"schema_version must be {SCHEMA_VERSION}")
    if contract.get("contract_schema_version") != CONTRACT_SCHEMA_VERSION:
        raise GovernanceError(f"contract_schema_version must be {CONTRACT_SCHEMA_VERSION}")
    require_id(contract.get("contract_id"), "contract_id", CONTRACT_ID_RE)
    require_id(contract.get("project_slug"), "project_slug", PROJECT_SLUG_RE)
    expected_status = "DRAFT" if phase == "draft" else "READY"
    if contract.get("status") != expected_status:
        raise GovernanceError(f"status must be {expected_status} for {phase} validation")
    tier = require_string(contract.get("tier"), "tier")
    if tier not in TIERS:
        raise GovernanceError(f"tier must be one of {sorted(TIERS)}")
    if tier == "D0":
        raise GovernanceError("D0 uses an inline anti-trigger and must not create a governance contract")
    profile = require_string(contract.get("profile"), "profile")
    expected_profile = "compact" if tier == "D1" else "full"
    if profile != expected_profile:
        raise GovernanceError(f"tier {tier} requires {expected_profile} profile")
    change = require_dict(contract.get("change"), "change")
    require_string(change.get("summary"), "change.summary", min_length=8)
    materiality = require_string(change.get("materiality"), "change.materiality")
    if materiality not in MATERIALITY_VALUES:
        raise GovernanceError(f"change.materiality must be one of {sorted(MATERIALITY_VALUES)}")
    if tier == "D1" and materiality != "low":
        raise GovernanceError("D1 requires low materiality")
    if tier == "D2" and materiality != "material":
        raise GovernanceError("D2 requires material materiality")
    if tier == "D3" and materiality != "high-risk":
        raise GovernanceError("D3 requires high-risk materiality")
    if not isinstance(change.get("synthetic_only"), bool):
        raise GovernanceError("change.synthetic_only must be boolean")
    unique_strings(change.get("changed_surfaces"), "change.changed_surfaces", nonempty=True)
    triggers = unique_strings(contract.get("triggers"), "triggers", allowed=TRIGGERS, nonempty=True)

    planning = require_dict(contract.get("planning_contract"), "planning_contract")
    require_string(planning.get("contract_id"), "planning_contract.contract_id", min_length=4)
    if not SHA256_RE.fullmatch(str(planning.get("contract_hash", ""))):
        raise GovernanceError("planning_contract.contract_hash must be lowercase SHA-256")
    refs = unique_strings(planning.get("requirement_refs"), "planning_contract.requirement_refs", nonempty=True)
    for ref in refs:
        if not REQ_ID_RE.fullmatch(ref):
            raise GovernanceError(f"invalid requirement ref: {ref}")

    source_ids = _validate_sources(contract)
    actors = require_dict(contract.get("actors"), "actors")
    require_string(actors.get("business_owner"), "actors.business_owner")
    require_string(actors.get("data_steward"), "actors.data_steward")
    legal_dpo = require_dict(actors.get("legal_dpo"), "actors.legal_dpo")
    if legal_dpo.get("status") not in {"AVAILABLE", "EXTERNAL_REQUIRED", "NOT_APPLICABLE"}:
        raise GovernanceError("actors.legal_dpo.status is unsupported")
    if legal_dpo["status"] == "AVAILABLE":
        require_string(legal_dpo.get("identity"), "actors.legal_dpo.identity")
        authority_ref = require_string(legal_dpo.get("authority_source_ref"), "actors.legal_dpo.authority_source_ref")
        if authority_ref not in source_ids:
            raise GovernanceError("actors.legal_dpo.authority_source_ref does not exist")
    elif legal_dpo["status"] == "EXTERNAL_REQUIRED":
        require_string(legal_dpo.get("rationale"), "actors.legal_dpo.rationale", min_length=8)

    previous = contract.get("previous_contract")
    if previous is not None:
        previous_doc = require_dict(previous, "previous_contract")
        require_id(previous_doc.get("contract_id"), "previous_contract.contract_id", CONTRACT_ID_RE)
        if not SHA256_RE.fullmatch(str(previous_doc.get("contract_hash", ""))):
            raise GovernanceError("previous_contract.contract_hash must be lowercase SHA-256")
        if previous_doc.get("relation") != "supersedes":
            raise GovernanceError("previous_contract.relation must be supersedes")
        require_string(previous_doc.get("change_summary"), "previous_contract.change_summary", min_length=8)
    asset_ids: set[str] = set()
    has_personal = False
    has_restricted = False
    for index, raw in enumerate(require_list(contract.get("assets"), "assets", nonempty=True)):
        asset = require_dict(raw, f"assets[{index}]")
        asset_id = require_id(asset.get("asset_id"), f"assets[{index}].asset_id")
        if asset_id in asset_ids:
            raise GovernanceError(f"duplicate asset_id: {asset_id}")
        asset_ids.add(asset_id)
        require_string(asset.get("name"), f"assets[{index}].name")
        if asset.get("kind") not in ASSET_KINDS:
            raise GovernanceError(f"assets[{index}].kind is unsupported")
        require_string(asset.get("owner"), f"assets[{index}].owner")
        require_string(asset.get("steward"), f"assets[{index}].steward")
        if not isinstance(asset.get("system_of_record"), bool):
            raise GovernanceError(f"assets[{index}].system_of_record must be boolean")
        classification = require_string(asset.get("classification"), f"assets[{index}].classification")
        if classification not in CLASSIFICATIONS:
            raise GovernanceError(f"assets[{index}].classification is unsupported")
        for field in ("personal_data", "special_category_data"):
            if not isinstance(asset.get(field), bool):
                raise GovernanceError(f"assets[{index}].{field} must be boolean")
        unique_strings(asset.get("environments"), f"assets[{index}].environments", allowed=ENVIRONMENTS, nonempty=True)
        unique_strings(asset.get("consumers"), f"assets[{index}].consumers")
        has_personal = has_personal or bool(asset["personal_data"]) or bool(asset["special_category_data"])
        has_restricted = has_restricted or classification == "restricted"

    required_tier = 0
    if "persistent-data" in triggers:
        required_tier = max(required_tier, 1)
    if any(trigger in triggers for trigger in {"cross-system", "published-metric", "etl-elt", "schema-change"}):
        required_tier = max(required_tier, 2)
    if has_personal or has_restricted or any(trigger in triggers for trigger in {"personal-data", "restricted-data"}):
        required_tier = 3
    if TIERS[tier] < required_tier:
        raise GovernanceError(f"tier {tier} is too low; this contract requires D{required_tier}")

    purpose_ids: set[str] = set()
    for index, raw in enumerate(require_list(contract.get("purposes"), "purposes", nonempty=True)):
        purpose = require_dict(raw, f"purposes[{index}]")
        purpose_id = require_id(purpose.get("purpose_id"), f"purposes[{index}].purpose_id")
        if purpose_id in purpose_ids:
            raise GovernanceError(f"duplicate purpose_id: {purpose_id}")
        purpose_ids.add(purpose_id)
        purpose_assets = unique_strings(purpose.get("asset_refs"), f"purposes[{index}].asset_refs", nonempty=True)
        if not set(purpose_assets).issubset(asset_ids):
            raise GovernanceError(f"purposes[{index}] references an unknown asset")
        require_string(purpose.get("description"), f"purposes[{index}].description", min_length=8)
        unique_strings(purpose.get("allowed_uses"), f"purposes[{index}].allowed_uses", nonempty=True)
        unique_strings(purpose.get("prohibited_uses"), f"purposes[{index}].prohibited_uses", nonempty=True)
        decision = require_dict(purpose.get("decision"), f"purposes[{index}].decision")
        if decision.get("status") != "DECIDED":
            raise GovernanceError(f"purposes[{index}].decision must be DECIDED before READY")
        _validate_decision(decision, f"purposes[{index}].decision", source_ids, allow_pending=False)

    lineage = require_dict(contract.get("lineage"), "lineage")
    unique_strings(lineage.get("sources"), "lineage.sources", nonempty=True)
    unique_strings(lineage.get("transformations"), "lineage.transformations", nonempty=True)
    unique_strings(lineage.get("outputs"), "lineage.outputs", nonempty=True)

    data_contract = require_dict(contract.get("data_contract"), "data_contract")
    require_string(data_contract.get("version"), "data_contract.version")
    if data_contract.get("compatibility") not in COMPATIBILITY:
        raise GovernanceError("data_contract.compatibility is unsupported")
    field_keys: set[tuple[str, str]] = set()
    for index, raw in enumerate(require_list(data_contract.get("fields"), "data_contract.fields", nonempty=True)):
        field = require_dict(raw, f"data_contract.fields[{index}]")
        asset_ref = require_string(field.get("asset_ref"), f"data_contract.fields[{index}].asset_ref")
        if asset_ref not in asset_ids:
            raise GovernanceError(f"data_contract.fields[{index}] references an unknown asset")
        name = require_string(field.get("name"), f"data_contract.fields[{index}].name")
        key = (asset_ref, name)
        if key in field_keys:
            raise GovernanceError(f"duplicate field: {asset_ref}.{name}")
        field_keys.add(key)
        require_string(field.get("type"), f"data_contract.fields[{index}].type")
        if not isinstance(field.get("nullable"), bool):
            raise GovernanceError(f"data_contract.fields[{index}].nullable must be boolean")
        require_string(field.get("description"), f"data_contract.fields[{index}].description", min_length=4)
        if field.get("classification") not in CLASSIFICATIONS:
            raise GovernanceError(f"data_contract.fields[{index}].classification is unsupported")

    control_ids: set[str] = set()
    access_required = tier in {"D2", "D3"} or any(
        trigger in triggers for trigger in {"cross-system", "personal-data", "restricted-data"}
    )
    for index, raw in enumerate(
        require_list(contract.get("access_controls", []), "access_controls", nonempty=access_required)
    ):
        control = require_dict(raw, f"access_controls[{index}]")
        control_id = require_id(control.get("control_id"), f"access_controls[{index}].control_id")
        if control_id in control_ids:
            raise GovernanceError(f"duplicate control_id: {control_id}")
        control_ids.add(control_id)
        control_assets = unique_strings(control.get("asset_refs"), f"access_controls[{index}].asset_refs", nonempty=True)
        if not set(control_assets).issubset(asset_ids):
            raise GovernanceError(f"access_controls[{index}] references an unknown asset")
        require_string(control.get("principal"), f"access_controls[{index}].principal")
        unique_strings(control.get("privileges"), f"access_controls[{index}].privileges", nonempty=True)
        require_string(control.get("scope"), f"access_controls[{index}].scope")
        require_string(control.get("justification"), f"access_controls[{index}].justification", min_length=8)
        require_string(control.get("reviewer"), f"access_controls[{index}].reviewer")

    check_ids: set[str] = set()
    reconciliation_checks: set[str] = set()
    for index, raw in enumerate(require_list(contract.get("quality_checks"), "quality_checks", nonempty=True)):
        check = require_dict(raw, f"quality_checks[{index}]")
        check_id = require_id(check.get("check_id"), f"quality_checks[{index}].check_id")
        if check_id in check_ids:
            raise GovernanceError(f"duplicate check_id: {check_id}")
        check_ids.add(check_id)
        check_assets = unique_strings(check.get("asset_refs"), f"quality_checks[{index}].asset_refs", nonempty=True)
        if not set(check_assets).issubset(asset_ids):
            raise GovernanceError(f"quality_checks[{index}] references an unknown asset")
        dimension = require_string(check.get("dimension"), f"quality_checks[{index}].dimension")
        if dimension not in QUALITY_DIMENSIONS:
            raise GovernanceError(f"quality_checks[{index}].dimension is unsupported")
        if dimension == "reconciliation":
            reconciliation_checks.add(check_id)
        require_string(check.get("method"), f"quality_checks[{index}].method", min_length=8)
        require_string(check.get("threshold"), f"quality_checks[{index}].threshold")
        if check.get("severity") not in {"advisory", "blocking"}:
            raise GovernanceError(f"quality_checks[{index}].severity is unsupported")
        require_string(check.get("owner"), f"quality_checks[{index}].owner")
        require_string(check.get("evidence_kind"), f"quality_checks[{index}].evidence_kind")

    retention = require_dict(contract.get("retention"), "retention")
    if retention.get("status") not in {"DECIDED", "NOT_APPLICABLE"}:
        raise GovernanceError("retention.status must be DECIDED or NOT_APPLICABLE before READY")
    require_string(retention.get("policy"), "retention.policy", min_length=8)
    require_string(retention.get("decision_owner"), "retention.decision_owner")
    source_ref = require_string(retention.get("source_ref"), "retention.source_ref")
    if source_ref not in source_ids:
        raise GovernanceError("retention.source_ref does not exist")
    require_utc(retention.get("verified_at"), "retention.verified_at")
    require_string(retention.get("legal_hold_behavior"), "retention.legal_hold_behavior", min_length=8)
    retention_assets: set[str] = set()
    for index, raw in enumerate(require_list(retention.get("targets"), "retention.targets", nonempty=True)):
        target = require_dict(raw, f"retention.targets[{index}]")
        asset_ref = require_string(target.get("asset_ref"), f"retention.targets[{index}].asset_ref")
        if asset_ref not in asset_ids:
            raise GovernanceError(f"retention.targets[{index}] references an unknown asset")
        retention_assets.add(asset_ref)
        stores = set(unique_strings(target.get("stores"), f"retention.targets[{index}].stores", nonempty=True))
        if "primary" not in stores:
            raise GovernanceError(f"retention.targets[{index}].stores must include primary")
        require_string(target.get("deletion_verification"), f"retention.targets[{index}].deletion_verification", min_length=8)
    if retention_assets != asset_ids:
        raise GovernanceError("retention.targets must cover every asset")

    privacy_value = contract.get("privacy")
    if privacy_value is None:
        if has_personal or has_restricted or tier == "D3":
            raise GovernanceError("D3, personal or restricted data requires the privacy module")
        privacy: dict[str, Any] = {}
    else:
        privacy = require_dict(privacy_value, "privacy")
        if not isinstance(privacy.get("review_required"), bool):
            raise GovernanceError("privacy.review_required must be boolean")
        if has_personal and not privacy["review_required"]:
            raise GovernanceError("personal data requires privacy.review_required=true")
        for key in ("lawful_basis", "dpia", "transfer"):
            if key not in privacy:
                raise GovernanceError(f"privacy.{key} is required")
            _validate_decision(privacy[key], f"privacy.{key}", source_ids)

    migration_value = contract.get("migration")
    if migration_value is None:
        if "migration" in triggers or "backfill" in triggers:
            raise GovernanceError("migration or backfill trigger requires the migration module")
        migration: dict[str, Any] = {"required": False}
    else:
        migration = require_dict(migration_value, "migration")
        if not isinstance(migration.get("required"), bool):
            raise GovernanceError("migration.required must be boolean")
    if ("migration" in triggers or "backfill" in triggers) and not migration["required"]:
        raise GovernanceError("migration or backfill trigger requires migration.required=true")
    if migration["required"]:
        if "migration" not in triggers and "backfill" not in triggers:
            raise GovernanceError("migration.required needs a migration or backfill trigger")
        for key in ("strategy", "rehearsal", "checkpoint", "backup", "restore_test", "rollback", "roll_forward"):
            require_string(migration.get(key), f"migration.{key}", min_length=8)
        if not isinstance(migration.get("destructive"), bool):
            raise GovernanceError("migration.destructive must be boolean")
        unique_strings(migration.get("environments"), "migration.environments", allowed=ENVIRONMENTS, nonempty=True)
        validation_refs = set(unique_strings(migration.get("validation_check_refs"), "migration.validation_check_refs", nonempty=True))
        if not validation_refs.issubset(check_ids):
            raise GovernanceError("migration.validation_check_refs contains an unknown check")
        if not validation_refs.intersection(reconciliation_checks):
            raise GovernanceError("migration requires at least one reconciliation quality check")
        approval = require_dict(migration.get("cutover_approval"), "migration.cutover_approval")
        if approval.get("status") not in {"PENDING", "APPROVED"}:
            raise GovernanceError("migration.cutover_approval.status must be PENDING or APPROVED")
        require_string(approval.get("owner"), "migration.cutover_approval.owner")
        require_string(approval.get("scope"), "migration.cutover_approval.scope")

    gate_ids: set[str] = set()
    gate_kinds: set[str] = set()
    for index, raw in enumerate(require_list(contract.get("gates"), "gates", nonempty=True)):
        gate = require_dict(raw, f"gates[{index}]")
        gate_id = require_id(gate.get("gate_id"), f"gates[{index}].gate_id")
        if gate_id in gate_ids:
            raise GovernanceError(f"duplicate gate_id: {gate_id}")
        gate_ids.add(gate_id)
        kind = require_string(gate.get("kind"), f"gates[{index}].kind")
        if kind not in GATE_KINDS:
            raise GovernanceError(f"gates[{index}].kind is unsupported")
        gate_kinds.add(kind)
        if gate.get("required_before") not in GATE_PHASES:
            raise GovernanceError(f"gates[{index}].required_before is unsupported")
        if not isinstance(gate.get("mandatory"), bool):
            raise GovernanceError(f"gates[{index}].mandatory must be boolean")
        if gate.get("applicability") not in {"applicable", "not-applicable"}:
            raise GovernanceError(f"gates[{index}].applicability is unsupported")
        if gate["applicability"] == "not-applicable":
            require_string(gate.get("rationale"), f"gates[{index}].rationale", min_length=8)
        if gate.get("reviewer_policy") not in {"independent", "legal-dpo", "owner"}:
            raise GovernanceError(f"gates[{index}].reviewer_policy is unsupported")
        max_age = gate.get("max_age_seconds")
        if not isinstance(max_age, int) or not 60 <= max_age <= 2592000:
            raise GovernanceError(f"gates[{index}].max_age_seconds must be between 60 and 2592000")
        unique_strings(gate.get("invalidate_on"), f"gates[{index}].invalidate_on", nonempty=True)
        unique_strings(gate.get("evidence_subjects"), f"gates[{index}].evidence_subjects", nonempty=True)

    required_gate_kinds = {"data-contract", "quality-reconciliation"}
    if has_personal:
        required_gate_kinds.add("privacy")
    if migration["required"]:
        required_gate_kinds.add("migration-readiness")
    missing_gate_kinds = required_gate_kinds - gate_kinds
    if missing_gate_kinds:
        raise GovernanceError(f"missing required governance gates: {sorted(missing_gate_kinds)}")

    if phase == "ready":
        require_utc(contract.get("frozen_at"), "frozen_at")
        supplied = str(contract.get("contract_hash", ""))
        if not SHA256_RE.fullmatch(supplied):
            raise GovernanceError("contract_hash must be lowercase SHA-256")
        expected = contract_hash(contract)
        if supplied != expected:
            raise GovernanceError(f"contract_hash mismatch: expected {expected}")
    elif contract.get("contract_hash") is not None or contract.get("frozen_at") is not None:
        raise GovernanceError("DRAFT must not contain frozen_at or contract_hash")
    return contract


def seal_contract(contract: dict[str, Any], frozen_at: str | None = None) -> dict[str, Any]:
    validate_contract(contract, "draft")
    result = copy.deepcopy(contract)
    result["status"] = "READY"
    result["frozen_at"] = frozen_at or utc_now()
    require_utc(result["frozen_at"], "frozen_at")
    result["contract_hash"] = contract_hash(result)
    validate_contract(result, "ready")
    return result


def _fragment_node(
    key: str,
    title: str,
    capability: str,
    kind: str,
    depends_on: list[str],
    gate_refs: list[str],
    asset_ids: list[str],
    reviewer_policy: str,
) -> dict[str, Any]:
    return {
        "node_key": key,
        "title": title,
        "capability": capability,
        "kind": kind,
        "depends_on": depends_on,
        "executor": "parent-local" if kind == "work" else "fleet-quality-gate",
        "reviewer_policy": reviewer_policy,
        "resource_locks": [f"data:{asset_id}" for asset_id in asset_ids],
        "source_gate_refs": gate_refs,
        "acceptance_criteria": [
            {
                "criterion_key": f"{key}-evidence",
                "statement": f"Every applicable governance gate for {title} has current independent evidence.",
                "evidence_shape": "content-hashed artifact plus reviewer identity and lineage",
            }
        ],
    }


def compile_graph_fragment(contract: dict[str, Any]) -> dict[str, Any]:
    validate_contract(contract, "ready")
    gates_by_kind: dict[str, list[str]] = {}
    for gate in contract["gates"]:
        gates_by_kind.setdefault(gate["kind"], []).append(gate["gate_id"])
    assets = sorted(asset["asset_id"] for asset in contract["assets"])
    nodes: list[dict[str, Any]] = []
    nodes.append(
        _fragment_node(
            "data-contract",
            "Data inventory, purpose, lineage and immutable data contract",
            "contract-definition",
            "work",
            [],
            gates_by_kind.get("data-contract", []) + gates_by_kind.get("classification-purpose", []),
            assets,
            "independent",
        )
    )
    needs_privacy = any(asset["personal_data"] or asset["classification"] == "restricted" for asset in contract["assets"])
    if needs_privacy or "privacy" in gates_by_kind or "access-security" in gates_by_kind:
        nodes.append(
            _fragment_node(
                "privacy-access",
                "Privacy, minimisation, retention and least-privilege review",
                "incremental-security",
                "gate",
                ["data-contract"],
                gates_by_kind.get("privacy", [])
                + gates_by_kind.get("access-security", [])
                + gates_by_kind.get("retention-deletion", []),
                assets,
                "legal-dpo" if needs_privacy else "independent",
            )
        )
    quality_dependency = "privacy-access" if any(node["node_key"] == "privacy-access" for node in nodes) else "data-contract"
    if contract.get("migration", {}).get("required", False):
        nodes.append(
            _fragment_node(
                "migration-readiness",
                "Rehearsed migration, checkpoint, restore and cutover readiness",
                "independent-quality",
                "gate",
                [quality_dependency],
                gates_by_kind.get("migration-readiness", []),
                assets,
                "independent",
            )
        )
        quality_dependency = "migration-readiness"
    nodes.append(
        _fragment_node(
            "quality-reconciliation",
            "Data quality, reconciliation and consumer contract verification",
            "deterministic-tests",
            "verify",
            [quality_dependency],
            gates_by_kind.get("quality-reconciliation", []),
            assets,
            "independent",
        )
    )
    nodes.append(
        _fragment_node(
            "data-acceptance",
            "Final data governance acceptance",
            "independent-quality",
            "gate",
            ["quality-reconciliation"],
            gates_by_kind.get("data-acceptance", []),
            assets,
            "independent",
        )
    )
    fragment = {
        "schema_version": 1,
        "fragment_kind": "agent-graph-data-governance",
        "contract_id": contract["contract_id"],
        "contract_hash": contract["contract_hash"],
        "contract_schema_version": contract["contract_schema_version"],
        "project_slug": contract["project_slug"],
        "nodes": nodes,
        "integration_rule": "Map node_key to fresh Agent Graph node IDs; preserve dependencies, locks, gates and criteria.",
        "mutation_sequence": "seal -> compile -> graph init -> acquire data resource locks -> bind fresh live-state evidence -> revalidate contract and fragment -> mutate",
        "assurance_boundary": "This projection validates declared controls and evidence integrity; evidence producers must test the live system.",
    }
    fragment["fragment_hash"] = sha256_value(fragment)
    return fragment


def verify_graph_fragment(contract: dict[str, Any], fragment: dict[str, Any]) -> dict[str, Any]:
    validate_contract(contract, "ready")
    if fragment.get("contract_id") != contract["contract_id"] or fragment.get("contract_hash") != contract["contract_hash"]:
        raise GovernanceError("graph fragment is bound to another contract")
    supplied_hash = str(fragment.get("fragment_hash", ""))
    payload = copy.deepcopy(fragment)
    payload.pop("fragment_hash", None)
    if supplied_hash != sha256_value(payload):
        raise GovernanceError("graph fragment hash mismatch")
    expected = compile_graph_fragment(contract)
    if fragment != expected:
        raise GovernanceError("graph fragment differs from deterministic projection")
    return {"status": "VALID", "contract_hash": contract["contract_hash"], "fragment_hash": supplied_hash}


def _verify_evidence_item(
    raw: Any,
    label: str,
    evidence_root: Path,
    gate: dict[str, Any],
    closed_at: datetime,
) -> dict[str, Any]:
    item = require_dict(raw, label)
    relative_text = require_string(item.get("path"), f"{label}.path")
    relative = Path(relative_text)
    if relative.is_absolute() or ".." in relative.parts:
        raise GovernanceError(f"{label}.path must stay within the evidence directory")
    if SENSITIVE_PATH_RE.search(relative.as_posix()):
        raise GovernanceError(f"{label}.path looks sensitive")
    root = evidence_root.resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise GovernanceError(f"{label}.path escapes the evidence directory") from exc
    if not path.is_file() or path.is_symlink():
        raise GovernanceError(f"{label}.path must be an existing plain file: {relative_text}")
    expected_hash = str(item.get("sha256", ""))
    if not SHA256_RE.fullmatch(expected_hash) or sha256_file(path) != expected_hash:
        raise GovernanceError(f"{label}.sha256 does not match the evidence file")
    if item.get("size_bytes") != path.stat().st_size:
        raise GovernanceError(f"{label}.size_bytes does not match the evidence file")
    observed_text = require_utc(item.get("observed_at"), f"{label}.observed_at")
    observed_at = parse_utc(observed_text)
    age_seconds = (closed_at - observed_at).total_seconds()
    if age_seconds < -300:
        raise GovernanceError(f"{label}.observed_at is in the future")
    if age_seconds > gate["max_age_seconds"]:
        raise GovernanceError(f"{label} is stale for gate {gate['gate_id']}")
    subject_kind = require_string(item.get("subject_kind"), f"{label}.subject_kind")
    if subject_kind not in gate["evidence_subjects"]:
        raise GovernanceError(f"{label}.subject_kind is not allowed by gate {gate['gate_id']}")
    subject_sha256 = str(item.get("subject_sha256", ""))
    if not SHA256_RE.fullmatch(subject_sha256):
        raise GovernanceError(f"{label}.subject_sha256 must bind evidence to an exact live-state subject")
    return {
        "path": relative.as_posix(),
        "sha256": expected_hash,
        "size_bytes": path.stat().st_size,
        "observed_at": observed_text,
        "subject_kind": subject_kind,
        "subject_sha256": subject_sha256,
    }


def closeout(contract: dict[str, Any], closeout_value: dict[str, Any], evidence_root: Path) -> dict[str, Any]:
    validate_contract(contract, "ready")
    closeout_doc = require_dict(closeout_value, "closeout")
    reject_sensitive_payload(closeout_doc, "closeout")
    if closeout_doc.get("contract_id") != contract["contract_id"] or closeout_doc.get("contract_hash") != contract["contract_hash"]:
        raise GovernanceError("closeout is bound to another contract")
    operation_id = require_id(closeout_doc.get("operation_id"), "closeout.operation_id")
    maker = require_dict(closeout_doc.get("maker"), "closeout.maker")
    maker_id = require_string(maker.get("id"), "closeout.maker.id")
    maker_lineage = require_string(maker.get("lineage"), "closeout.maker.lineage")
    gates = {gate["gate_id"]: gate for gate in contract["gates"]}
    closed_at = datetime.now(timezone.utc)
    results: dict[str, Any] = {}
    blocked: list[str] = []
    not_applicable: list[str] = []
    for index, raw in enumerate(require_list(closeout_doc.get("results"), "closeout.results", nonempty=True)):
        result = require_dict(raw, f"closeout.results[{index}]")
        gate_id = require_string(result.get("gate_id"), f"closeout.results[{index}].gate_id")
        if gate_id not in gates:
            raise GovernanceError(f"closeout result references unknown gate: {gate_id}")
        if gate_id in results:
            raise GovernanceError(f"duplicate closeout result for gate: {gate_id}")
        status = require_string(result.get("status"), f"closeout.results[{index}].status")
        if status not in RESULT_STATUSES:
            raise GovernanceError(f"unsupported closeout status for {gate_id}: {status}")
        gate = gates[gate_id]
        if status == "NOT_APPLICABLE" and gate["applicability"] != "not-applicable":
            raise GovernanceError(f"gate {gate_id} cannot become NOT_APPLICABLE during closeout")
        if status == "PASS" and gate["kind"] == "privacy":
            pending = [
                name
                for name in ("lawful_basis", "dpia", "transfer")
                if contract.get("privacy", {}).get(name, {}).get("status") == "PENDING"
            ]
            if pending:
                raise GovernanceError(f"privacy gate cannot PASS while decisions are PENDING: {pending}")
        if (
            status == "PASS"
            and gate["kind"] == "migration-readiness"
            and contract.get("migration", {}).get("cutover_approval", {}).get("status") == "PENDING"
        ):
            raise GovernanceError("migration-readiness cannot PASS while cutover approval is PENDING")
        if status == "BLOCKED":
            require_string(result.get("blocker"), f"closeout.results[{index}].blocker", min_length=8)
            blocked.append(gate_id)
        if status == "NOT_APPLICABLE":
            not_applicable.append(gate_id)
        reviewer = require_dict(result.get("reviewer"), f"closeout.results[{index}].reviewer")
        reviewer_id = require_string(reviewer.get("id"), f"closeout.results[{index}].reviewer.id")
        reviewer_lineage = require_string(reviewer.get("lineage"), f"closeout.results[{index}].reviewer.lineage")
        if reviewer_id == maker_id or reviewer_lineage == maker_lineage:
            raise GovernanceError(f"gate {gate_id} requires an independent reviewer")
        evidence_items = [
            _verify_evidence_item(
                item,
                f"closeout.results[{index}].evidence[{evidence_index}]",
                evidence_root,
                gate,
                closed_at,
            )
            for evidence_index, item in enumerate(require_list(result.get("evidence"), f"closeout.results[{index}].evidence", nonempty=True))
        ]
        results[gate_id] = {
            "status": status,
            "reviewer": {"id": reviewer_id, "lineage": reviewer_lineage},
            "evidence": evidence_items,
            "blocker": result.get("blocker") if status == "BLOCKED" else None,
        }
    missing = sorted(set(gates) - set(results))
    if missing:
        raise GovernanceError(f"closeout is missing gate results: {missing}")
    failed_required = sorted(
        gate_id
        for gate_id, gate in gates.items()
        if gate["mandatory"] and gate["applicability"] == "applicable" and results[gate_id]["status"] != "PASS"
    )
    receipt = {
        "schema_version": 1,
        "receipt_kind": "data-governance-closeout",
        "contract_id": contract["contract_id"],
        "contract_hash": contract["contract_hash"],
        "operation_id": operation_id,
        "status": "ACCEPTED" if not failed_required else "NOT_ACCEPTED",
        "closed_at": closed_at.isoformat().replace("+00:00", "Z"),
        "assurance_scope": "artifact-integrity-and-independent-review",
        "live_state_claim": "Evidence subjects were content-bound; this helper did not query or mutate the live system.",
        "maker": {"id": maker_id, "lineage": maker_lineage},
        "results": results,
        "failed_required_gates": failed_required,
        "blocked_gates": sorted(blocked),
        "not_applicable_gates": sorted(not_applicable),
    }
    receipt["receipt_hash"] = sha256_value(receipt)
    return receipt


def verify_receipt(contract: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
    validate_contract(contract, "ready")
    if receipt.get("receipt_kind") != "data-governance-closeout":
        raise GovernanceError("unsupported receipt kind")
    if receipt.get("assurance_scope") != "artifact-integrity-and-independent-review":
        raise GovernanceError("unsupported receipt assurance scope")
    if receipt.get("contract_id") != contract["contract_id"] or receipt.get("contract_hash") != contract["contract_hash"]:
        raise GovernanceError("receipt is bound to another contract")
    supplied = str(receipt.get("receipt_hash", ""))
    payload = copy.deepcopy(receipt)
    payload.pop("receipt_hash", None)
    if supplied != sha256_value(payload):
        raise GovernanceError("receipt hash mismatch")
    expected_gates = {gate["gate_id"] for gate in contract["gates"]}
    if set(require_dict(receipt.get("results"), "receipt.results")) != expected_gates:
        raise GovernanceError("receipt gate coverage mismatch")
    failed = require_list(receipt.get("failed_required_gates"), "receipt.failed_required_gates")
    expected_status = "ACCEPTED" if not failed else "NOT_ACCEPTED"
    if receipt.get("status") != expected_status:
        raise GovernanceError("receipt status does not match failed required gates")
    return {"status": "VALID", "receipt_status": expected_status, "receipt_hash": supplied}


def doctor() -> dict[str, Any]:
    required = [
        SKILL_ROOT / "SKILL.md",
        SKILL_ROOT / "VERSION",
        SKILL_ROOT / "references" / "contract-format.md",
        SKILL_ROOT / "references" / "risk-and-gates.md",
        SKILL_ROOT / "references" / "migration-and-backfill.md",
        SKILL_ROOT / "references" / "privacy-and-retention.md",
    ]
    missing = [str(path) for path in required if not path.is_file()]
    return {
        "status": "OK" if not missing else "INVALID",
        "version": VERSION,
        "schema_version": SCHEMA_VERSION,
        "contract_schema_version": CONTRACT_SCHEMA_VERSION,
        "missing": missing,
        "network_calls": 0,
        "database_calls": 0,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deterministic data-governance contract and evidence gate")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor")
    classify = subparsers.add_parser("classify")
    classify.add_argument("--change", type=Path, required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--contract", type=Path, required=True)
    validate.add_argument("--phase", choices=("draft", "ready"), default="ready")
    seal = subparsers.add_parser("seal")
    seal.add_argument("--contract", type=Path, required=True)
    seal.add_argument("--output", type=Path, required=True)
    seal.add_argument("--frozen-at")
    hash_parser = subparsers.add_parser("hash-contract")
    hash_parser.add_argument("--contract", type=Path, required=True)
    compile_parser = subparsers.add_parser("compile-graph-fragment")
    compile_parser.add_argument("--contract", type=Path, required=True)
    compile_parser.add_argument("--output", type=Path, required=True)
    verify_fragment = subparsers.add_parser("verify-graph-fragment")
    verify_fragment.add_argument("--contract", type=Path, required=True)
    verify_fragment.add_argument("--fragment", type=Path, required=True)
    close_parser = subparsers.add_parser("closeout")
    close_parser.add_argument("--contract", type=Path, required=True)
    close_parser.add_argument("--evidence", type=Path, required=True)
    close_parser.add_argument("--output", type=Path, required=True)
    verify_parser = subparsers.add_parser("verify-receipt")
    verify_parser.add_argument("--contract", type=Path, required=True)
    verify_parser.add_argument("--receipt", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "doctor":
            result = doctor()
        elif args.command == "classify":
            result = classify_change(load_json(args.change, "change assessment"))
        elif args.command == "validate":
            contract = load_json(args.contract, "contract")
            validate_contract(contract, args.phase)
            result = {"status": "VALID", "phase": args.phase, "contract_id": contract["contract_id"]}
        elif args.command == "seal":
            result = seal_contract(load_json(args.contract, "contract"), args.frozen_at)
            atomic_write_json(args.output, result)
        elif args.command == "hash-contract":
            contract = load_json(args.contract, "contract")
            result = {"contract_hash": contract_hash(contract)}
        elif args.command == "compile-graph-fragment":
            result = compile_graph_fragment(load_json(args.contract, "contract"))
            atomic_write_json(args.output, result)
        elif args.command == "verify-graph-fragment":
            result = verify_graph_fragment(
                load_json(args.contract, "contract"),
                load_json(args.fragment, "graph fragment"),
            )
        elif args.command == "closeout":
            result = closeout(
                load_json(args.contract, "contract"),
                load_json(args.evidence, "closeout evidence"),
                args.evidence.parent,
            )
            atomic_write_json(args.output, result)
        elif args.command == "verify-receipt":
            result = verify_receipt(
                load_json(args.contract, "contract"),
                load_json(args.receipt, "receipt"),
            )
        else:
            raise GovernanceError(f"Unsupported command: {args.command}")
    except GovernanceError as exc:
        print(
            json.dumps(
                {"status": "ERROR", "code": "DG_VALIDATION_ERROR", "error": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
