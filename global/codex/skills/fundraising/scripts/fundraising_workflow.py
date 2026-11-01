#!/usr/bin/env python3
"""Deterministic evidence and arithmetic gate for fundraising artifacts."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, ROUND_HALF_UP
from pathlib import Path
from typing import Any


VERSION = "0.1.0"
ID_RE = re.compile(r"^[a-z][a-z0-9-]{2,95}$")
SHA_RE = re.compile(r"^[a-f0-9]{64}$")
ARTIFACT_TYPES = {"deck", "memorandum", "one-pager"}
CLAIM_KINDS = {
    "fact",
    "founder_statement",
    "assumption",
    "projection",
    "scenario",
    "unknown",
}
CLASSIFICATIONS = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}
DATA_CLASSES = {"D0", "D1", "D2", "D3"}
DATA_CLASS_LEVELS = {"D0": 0, "D1": 1, "D2": 2, "D3": 3}
CANONICAL_LEDGERS = {
    "sources": "sources.json",
    "claims": "claims.json",
    "numbers": "numbers.json",
}
CANONICAL_CAMPAIGN_INPUTS = {"campaign.json", *CANONICAL_LEDGERS.values()}
REQUIRED_DECISIONS = {
    "audience",
    "stage",
    "geography",
    "funding_amount",
    "instrument",
    "terms",
    "sensitive_disclosure",
    "final_ask",
}
ROUNDING = {"HALF_EVEN": ROUND_HALF_EVEN, "HALF_UP": ROUND_HALF_UP}


class ContractError(RuntimeError):
    pass


def no_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=no_duplicate_pairs)
    except (OSError, UnicodeError, json.JSONDecodeError, ContractError) as exc:
        raise ContractError(f"cannot read strict JSON {path}: {exc}") from exc


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=True, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def is_reparse(path: Path) -> bool:
    try:
        attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & 0x400)


def safe_file(root: Path, raw: Any, label: str, *, must_exist: bool = True) -> Path:
    if not isinstance(raw, str) or not raw or "\\" in raw:
        raise ContractError(f"{label} must be a non-empty forward-slash relative path")
    candidate = Path(raw)
    if candidate.is_absolute() or ".." in candidate.parts or "." in candidate.parts:
        raise ContractError(f"{label} escapes the campaign root")
    resolved_root = root.resolve()
    resolved = (resolved_root / candidate).resolve(strict=False)
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ContractError(f"{label} escapes the campaign root")
    current = resolved_root
    for part in candidate.parts:
        current = current / part
        if current.exists() and (current.is_symlink() or is_reparse(current)):
            raise ContractError(f"{label} crosses a link or reparse point: {current}")
    if must_exist and (not resolved.is_file() or resolved.is_symlink() or is_reparse(resolved)):
        raise ContractError(f"{label} is not a regular file: {raw}")
    return resolved


def parse_date(value: Any, label: str) -> date:
    if not isinstance(value, str):
        raise ContractError(f"{label} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ContractError(f"{label} must be YYYY-MM-DD") from exc


def as_decimal(value: Any, label: str) -> Decimal:
    if not isinstance(value, str):
        raise ContractError(f"{label} must be a decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ContractError(f"{label} is not a decimal string") from exc
    if not parsed.is_finite():
        raise ContractError(f"{label} must be finite")
    return parsed


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> str:
        if self.errors:
            return "INVALID"
        if self.blockers:
            return "BLOCKED"
        return "READY"

    def output(self) -> dict[str, Any]:
        return {
            "blockers": sorted(set(self.blockers)),
            "errors": sorted(set(self.errors)),
            "state": self.state,
            "status": self.status,
            "warnings": sorted(set(self.warnings)),
        }


class DecimalExpression(ast.NodeVisitor):
    def __init__(self, values: dict[str, Decimal]) -> None:
        self.values = values

    def evaluate(self, expression: str) -> Decimal:
        try:
            tree = ast.parse(expression, mode="eval")
        except SyntaxError as exc:
            raise ContractError("invalid arithmetic formula") from exc
        return self.visit(tree)

    def visit_Expression(self, node: ast.Expression) -> Decimal:
        return self.visit(node.body)

    def visit_Name(self, node: ast.Name) -> Decimal:
        if node.id not in self.values:
            raise ContractError(f"formula uses undeclared variable: {node.id}")
        return self.values[node.id]

    def visit_Constant(self, node: ast.Constant) -> Decimal:
        if isinstance(node.value, bool) or not isinstance(node.value, int):
            raise ContractError("formula constants must be integers")
        return Decimal(node.value)

    def visit_UnaryOp(self, node: ast.UnaryOp) -> Decimal:
        value = self.visit(node.operand)
        if isinstance(node.op, ast.USub):
            return -value
        if isinstance(node.op, ast.UAdd):
            return value
        raise ContractError("formula contains a forbidden unary operator")

    def visit_BinOp(self, node: ast.BinOp) -> Decimal:
        left = self.visit(node.left)
        right = self.visit(node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            if right == 0:
                raise ContractError("division by zero")
            return left / right
        raise ContractError("formula contains a forbidden operator")

    def generic_visit(self, node: ast.AST) -> Decimal:
        raise ContractError(f"formula contains a forbidden node: {type(node).__name__}")


def require_mapping(value: Any, label: str, report: ValidationReport) -> dict[str, Any]:
    if not isinstance(value, dict):
        report.errors.append(f"{label} must be an object")
        return {}
    return value


def require_list(value: Any, label: str, report: ValidationReport) -> list[Any]:
    if not isinstance(value, list):
        report.errors.append(f"{label} must be an array")
        return []
    return value


def valid_id(value: Any, prefix: str) -> bool:
    return isinstance(value, str) and value.startswith(prefix) and bool(ID_RE.fullmatch(value))


def validate_campaign(root: Path, *, today: date | None = None) -> ValidationReport:
    report = ValidationReport()
    today = today or date.today()
    root = root.resolve()
    try:
        campaign_path = safe_file(root, "campaign.json", "campaign")
        campaign = require_mapping(load_json(campaign_path), "campaign", report)
    except ContractError as exc:
        report.errors.append(str(exc))
        return report

    allowed_root = {
        "schema_version",
        "campaign_id",
        "company",
        "mode",
        "artifact_types",
        "owner_decisions",
        "provider_policy",
        "ledgers",
        "content_lineage",
        "design_lineage",
        "repair_budget",
        "distribution_status",
    }
    unknown_root = sorted(set(campaign) - allowed_root)
    if unknown_root:
        report.errors.append(f"campaign has unknown fields: {unknown_root}")
    if type(campaign.get("schema_version")) is not int or campaign.get("schema_version") != 1:
        report.errors.append("campaign.schema_version must equal 1")
    campaign_id = campaign.get("campaign_id")
    if not valid_id(campaign_id, "campaign-"):
        report.errors.append("campaign_id is invalid")
    if campaign.get("mode") not in {"create", "review-repair"}:
        report.errors.append("mode must be create or review-repair")

    artifact_types = require_list(campaign.get("artifact_types"), "artifact_types", report)
    if not artifact_types or len(set(artifact_types)) != len(artifact_types):
        report.errors.append("artifact_types must be non-empty and unique")
    if any(item not in ARTIFACT_TYPES for item in artifact_types):
        report.errors.append("artifact_types contains an unsupported type")

    company = require_mapping(campaign.get("company"), "company", report)
    if not isinstance(company.get("name"), str) or not company.get("name", "").strip():
        report.errors.append("company.name is required")
    if not isinstance(company.get("project_slug"), str) or not company.get("project_slug", "").strip():
        report.errors.append("company.project_slug is required")
    continuity = company.get("continuity_status")
    if continuity not in {"MISSING", "PENDING", "VERIFIED"}:
        report.errors.append("company.continuity_status is invalid")
    elif continuity != "VERIFIED":
        report.blockers.append(f"project continuity is {continuity}")
    data_class = company.get("data_classification")
    if data_class not in DATA_CLASSES:
        report.errors.append("company.data_classification is invalid")

    decisions = require_mapping(campaign.get("owner_decisions"), "owner_decisions", report)
    for decision_id in sorted(REQUIRED_DECISIONS):
        decision = decisions.get(decision_id)
        if not isinstance(decision, dict):
            report.blockers.append(f"owner decision missing: {decision_id}")
            continue
        if decision.get("status") != "approved":
            report.blockers.append(f"owner decision not approved: {decision_id}")
        value = decision.get("value")
        if value is None or (isinstance(value, str) and not value.strip()):
            report.blockers.append(f"owner decision has no value: {decision_id}")

    provider = require_mapping(campaign.get("provider_policy"), "provider_policy", report)
    allowed_providers = require_list(provider.get("allowed_providers"), "provider_policy.allowed_providers", report)
    selected_providers = require_list(provider.get("selected_providers"), "provider_policy.selected_providers", report)
    if any(not isinstance(item, str) or not item for item in allowed_providers + selected_providers):
        report.errors.append("provider names must be non-empty strings")
    if len(set(allowed_providers)) != len(allowed_providers) or len(set(selected_providers)) != len(selected_providers):
        report.errors.append("provider lists must be unique")
    if not set(selected_providers).issubset(set(allowed_providers)):
        report.errors.append("selected providers exceed the provider allowlist")
    if not selected_providers:
        report.blockers.append("no provider selected for delegated work")
    # Classification, owner decisions and campaign/per-source provider checks
    # bind the current campaign and evidence. governance_contract_hash remains
    # optional legacy metadata; a separate sealed contract is not a TODO gate.

    for label in ("content_lineage", "design_lineage"):
        lineage = require_mapping(campaign.get(label), label, report)
        maker = lineage.get("maker")
        reviewer = lineage.get("reviewer")
        if not isinstance(maker, str) or not maker.strip():
            report.blockers.append(f"{label}.maker is unassigned")
        if not isinstance(reviewer, str) or not reviewer.strip():
            report.blockers.append(f"{label}.reviewer is unassigned")
        if isinstance(maker, str) and maker and maker == reviewer:
            report.errors.append(f"{label} maker and reviewer must differ")

    repair = require_mapping(campaign.get("repair_budget"), "repair_budget", report)
    maximum = repair.get("max_rounds")
    used = repair.get("used_rounds")
    if not isinstance(maximum, int) or isinstance(maximum, bool) or not 1 <= maximum <= 3:
        report.errors.append("repair_budget.max_rounds must be between 1 and 3")
    if not isinstance(used, int) or isinstance(used, bool) or used < 0:
        report.errors.append("repair_budget.used_rounds must be a non-negative integer")
    elif isinstance(maximum, int) and used > maximum:
        report.errors.append("repair budget is exceeded")
    if campaign.get("distribution_status") != "not_authorized":
        report.errors.append("distribution_status must remain not_authorized")

    ledgers = require_mapping(campaign.get("ledgers"), "ledgers", report)
    if ledgers != CANONICAL_LEDGERS:
        report.errors.append(
            "ledgers must equal the canonical ledger paths: "
            "sources.json, claims.json and numbers.json"
        )
    loaded: dict[str, Any] = {}
    ledger_hashes: dict[str, str] = {}
    for key, collection in (("sources", "sources"), ("claims", "claims"), ("numbers", "numbers")):
        try:
            ledger_path = safe_file(root, CANONICAL_LEDGERS[key], f"ledgers.{key}")
            payload = require_mapping(load_json(ledger_path), key, report)
            if type(payload.get("schema_version")) is not int or payload.get("schema_version") != 1:
                report.errors.append(f"{key}.schema_version must equal 1")
            loaded[key] = require_list(payload.get(collection), key, report)
            ledger_hashes[key] = sha256_file(ledger_path)
        except ContractError as exc:
            report.errors.append(str(exc))
            loaded[key] = []

    source_by_id: dict[str, dict[str, Any]] = {}
    source_usable: dict[str, bool] = {}
    for index, raw in enumerate(loaded["sources"]):
        source = require_mapping(raw, f"sources[{index}]", report)
        source_id = source.get("source_id")
        if not valid_id(source_id, "source-"):
            report.errors.append(f"sources[{index}].source_id is invalid")
            continue
        if source_id in source_by_id:
            report.errors.append(f"duplicate source_id: {source_id}")
            continue
        source_by_id[source_id] = source
        usable = True
        if source.get("verification_status") != "verified":
            usable = False
        classification = source.get("classification")
        if classification not in CLASSIFICATIONS:
            report.errors.append(f"{source_id} has invalid classification")
            usable = False
        elif data_class in DATA_CLASS_LEVELS and CLASSIFICATIONS[classification] > DATA_CLASS_LEVELS[data_class]:
            report.errors.append(
                f"{source_id} classification exceeds campaign data_classification"
            )
            usable = False
        if not isinstance(source.get("title"), str) or not source.get("title", "").strip():
            report.errors.append(f"{source_id} has no title")
            usable = False
        if not isinstance(source.get("owner"), str) or not source.get("owner", "").strip():
            report.errors.append(f"{source_id} has no owner")
            usable = False
        try:
            parse_date(source.get("observed_at"), f"{source_id}.observed_at")
            expires = source.get("expires_on")
            if expires is not None and parse_date(expires, f"{source_id}.expires_on") < today:
                report.errors.append(f"{source_id} is stale")
                usable = False
        except ContractError as exc:
            report.errors.append(str(exc))
            usable = False
        try:
            evidence_path = safe_file(root, source.get("evidence_ref"), f"{source_id}.evidence_ref")
            expected_hash = source.get("sha256")
            if not isinstance(expected_hash, str) or not SHA_RE.fullmatch(expected_hash):
                raise ContractError(f"{source_id}.sha256 is invalid")
            if sha256_file(evidence_path) != expected_hash:
                raise ContractError(f"{source_id} evidence hash mismatch")
        except ContractError as exc:
            report.errors.append(str(exc))
            usable = False
        source_providers = source.get("allowed_providers", [])
        if not isinstance(source_providers, list) or any(not isinstance(item, str) for item in source_providers):
            report.errors.append(f"{source_id}.allowed_providers is invalid")
            usable = False
        elif selected_providers and not set(selected_providers).issubset(set(source_providers)):
            report.errors.append(f"{source_id} does not allow every selected provider")
            usable = False
        source_usable[source_id] = usable

    claim_by_id: dict[str, dict[str, Any]] = {}
    verified_claims = 0
    for index, raw in enumerate(loaded["claims"]):
        claim = require_mapping(raw, f"claims[{index}]", report)
        claim_id = claim.get("claim_id")
        if not valid_id(claim_id, "claim-"):
            report.errors.append(f"claims[{index}].claim_id is invalid")
            continue
        if claim_id in claim_by_id:
            report.errors.append(f"duplicate claim_id: {claim_id}")
            continue
        claim_by_id[claim_id] = claim
        kind = claim.get("claim_kind")
        if kind not in CLAIM_KINDS:
            report.errors.append(f"{claim_id} has invalid claim_kind")
        classification = claim.get("classification")
        if classification not in CLASSIFICATIONS:
            report.errors.append(f"{claim_id} has invalid classification")
        elif data_class in DATA_CLASS_LEVELS and CLASSIFICATIONS[classification] > DATA_CLASS_LEVELS[data_class]:
            report.errors.append(
                f"{claim_id} classification exceeds campaign data_classification"
            )
        if not isinstance(claim.get("text"), str) or not claim.get("text", "").strip():
            report.errors.append(f"{claim_id} has no text")
        status = claim.get("status")
        if status not in {"verified", "proposed", "rejected", "unknown"}:
            report.errors.append(f"{claim_id} has invalid status")
        elif status != "verified":
            report.blockers.append(f"claim is not verified: {claim_id}")
        source_ids = claim.get("source_ids", [])
        if not isinstance(source_ids, list) or any(not isinstance(item, str) for item in source_ids):
            report.errors.append(f"{claim_id}.source_ids is invalid")
            source_ids = []
        if status == "verified" and kind != "unknown" and not source_ids:
            report.errors.append(f"verified claim lacks evidence: {claim_id}")
        if kind == "unknown" and status == "verified":
            report.errors.append(f"unknown claim cannot be verified: {claim_id}")
        for source_id in source_ids:
            if source_id not in source_by_id:
                report.errors.append(f"{claim_id} references missing source: {source_id}")
            elif not source_usable.get(source_id, False):
                report.errors.append(f"{claim_id} references unusable source: {source_id}")
        scopes = claim.get("artifact_scopes")
        if not isinstance(scopes, list) or not scopes or not set(scopes).issubset(set(artifact_types)):
            report.errors.append(f"{claim_id}.artifact_scopes is invalid")
        expires = claim.get("expires_on")
        if expires is not None:
            try:
                if parse_date(expires, f"{claim_id}.expires_on") < today:
                    report.errors.append(f"{claim_id} is stale")
            except ContractError as exc:
                report.errors.append(str(exc))
        if status == "verified" and kind != "unknown":
            verified_claims += 1
    if verified_claims == 0:
        report.blockers.append("no verified claims")

    number_by_id: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(loaded["numbers"]):
        number = require_mapping(raw, f"numbers[{index}]", report)
        number_id = number.get("number_id")
        if not valid_id(number_id, "number-"):
            report.errors.append(f"numbers[{index}].number_id is invalid")
            continue
        if number_id in number_by_id:
            report.errors.append(f"duplicate number_id: {number_id}")
            continue
        number_by_id[number_id] = number

    evaluated: dict[str, Decimal] = {}
    evaluating: set[str] = set()

    def evaluate_number(number_id: str) -> Decimal:
        if number_id in evaluated:
            return evaluated[number_id]
        if number_id in evaluating:
            raise ContractError(f"number formula cycle: {number_id}")
        if number_id not in number_by_id:
            raise ContractError(f"missing number: {number_id}")
        evaluating.add(number_id)
        number = number_by_id[number_id]
        declared = as_decimal(number.get("value"), f"{number_id}.value")
        kind = number.get("kind")
        if kind == "input":
            source_ids = number.get("source_ids")
            if not isinstance(source_ids, list) or not source_ids:
                raise ContractError(f"input number lacks sources: {number_id}")
            for source_id in source_ids:
                if source_id not in source_by_id or not source_usable.get(source_id, False):
                    raise ContractError(f"{number_id} references unusable source: {source_id}")
            calculated = declared
        elif kind == "derived":
            formula = number.get("formula")
            inputs = number.get("inputs")
            if not isinstance(formula, str) or not formula.strip() or not isinstance(inputs, dict):
                raise ContractError(f"derived number lacks formula or inputs: {number_id}")
            values: dict[str, Decimal] = {}
            for variable, dependency in inputs.items():
                if not isinstance(variable, str) or not variable.isidentifier() or not isinstance(dependency, str):
                    raise ContractError(f"{number_id}.inputs is invalid")
                values[variable] = evaluate_number(dependency)
            calculated = DecimalExpression(values).evaluate(formula)
        else:
            raise ContractError(f"{number_id}.kind must be input or derived")
        rounding = number.get("rounding")
        if not isinstance(rounding, dict):
            raise ContractError(f"{number_id}.rounding is required")
        places = rounding.get("places")
        mode = rounding.get("mode")
        if not isinstance(places, int) or isinstance(places, bool) or not 0 <= places <= 8:
            raise ContractError(f"{number_id}.rounding.places is invalid")
        if mode not in ROUNDING:
            raise ContractError(f"{number_id}.rounding.mode is invalid")
        quantum = Decimal(1).scaleb(-places)
        calculated = calculated.quantize(quantum, rounding=ROUNDING[mode])
        tolerance = as_decimal(number.get("tolerance", "0"), f"{number_id}.tolerance")
        if tolerance < 0 or abs(declared - calculated) > tolerance:
            raise ContractError(f"{number_id} declared value does not match its canonical calculation")
        if not isinstance(number.get("unit"), str) or not number.get("unit", "").strip():
            raise ContractError(f"{number_id}.unit is required")
        if not isinstance(number.get("period"), str) or not number.get("period", "").strip():
            raise ContractError(f"{number_id}.period is required")
        usages = number.get("usages", [])
        if not isinstance(usages, list):
            raise ContractError(f"{number_id}.usages must be an array")
        for usage in usages:
            if not isinstance(usage, dict) or usage.get("artifact") not in artifact_types:
                raise ContractError(f"{number_id} has invalid artifact usage")
            displayed = as_decimal(usage.get("display_value"), f"{number_id}.display_value")
            if abs(displayed - calculated) > tolerance:
                raise ContractError(f"{number_id} drifts in artifact {usage.get('artifact')}")
        evaluating.remove(number_id)
        evaluated[number_id] = calculated
        return calculated

    verified_numbers = 0
    for number_id, number in number_by_id.items():
        status = number.get("status")
        if status != "verified":
            report.blockers.append(f"number is not verified: {number_id}")
        else:
            verified_numbers += 1
        try:
            evaluate_number(number_id)
        except ContractError as exc:
            report.errors.append(str(exc))
            evaluating.discard(number_id)
    if verified_numbers == 0:
        report.blockers.append("no verified numbers")

    report.state = {
        "artifact_types": artifact_types,
        "campaign_id": campaign_id,
        "claim_count": len(claim_by_id),
        "continuity_status": continuity,
        "data_classification": data_class,
        "ledger_hashes": ledger_hashes,
        "number_count": len(number_by_id),
        "selected_providers": selected_providers,
        "source_count": len(source_by_id),
    }
    return report


def init_campaign(args: argparse.Namespace) -> int:
    root = Path(args.campaign_dir).resolve()
    if root.exists() and any(root.iterdir()):
        raise ContractError(f"campaign directory is not empty: {root}")
    if not valid_id(args.campaign_id, "campaign-"):
        raise ContractError("campaign-id must match campaign-[a-z0-9-]")
    artifacts = [item.strip() for item in args.artifacts.split(",") if item.strip()]
    if not artifacts or len(set(artifacts)) != len(artifacts) or any(item not in ARTIFACT_TYPES for item in artifacts):
        raise ContractError("artifacts must be a unique comma-separated subset of deck,memorandum,one-pager")
    root.mkdir(parents=True, exist_ok=True)
    (root / "evidence").mkdir()
    decisions = {key: {"status": "pending", "value": None} for key in sorted(REQUIRED_DECISIONS)}
    campaign = {
        "artifact_types": artifacts,
        "campaign_id": args.campaign_id,
        "company": {
            "continuity_status": "MISSING",
            "data_classification": "D1",
            "name": args.company_name,
            "project_slug": args.project_slug,
        },
        "content_lineage": {"maker": None, "reviewer": None},
        "design_lineage": {"maker": None, "reviewer": None},
        "distribution_status": "not_authorized",
        "ledgers": dict(CANONICAL_LEDGERS),
        "mode": args.mode,
        "owner_decisions": decisions,
        "provider_policy": {"allowed_providers": [], "governance_contract_hash": None, "selected_providers": []},
        "repair_budget": {"max_rounds": 2, "used_rounds": 0},
        "schema_version": 1,
    }
    atomic_json(root / "campaign.json", campaign)
    atomic_json(root / "sources.json", {"schema_version": 1, "sources": []})
    atomic_json(root / "claims.json", {"claims": [], "schema_version": 1})
    atomic_json(root / "numbers.json", {"numbers": [], "schema_version": 1})
    print(json.dumps({"campaign_dir": str(root), "status": "INITIALIZED"}, sort_keys=True))
    return 0


def print_report(report: ValidationReport) -> None:
    print(json.dumps(report.output(), ensure_ascii=True, indent=2, sort_keys=True))


def command_validate(args: argparse.Namespace) -> int:
    report = validate_campaign(Path(args.campaign_dir))
    print_report(report)
    return 2 if report.errors else 0


def command_gate(args: argparse.Namespace) -> int:
    report = validate_campaign(Path(args.campaign_dir))
    print_report(report)
    if report.errors:
        return 2
    if report.blockers:
        return 3
    return 0


def command_seal(args: argparse.Namespace) -> int:
    root = Path(args.campaign_dir).resolve()
    report = validate_campaign(root)
    if report.status != "READY":
        print_report(report)
        return 2 if report.errors else 3
    output = safe_file(root, args.output, "output", must_exist=False)
    output_relative = output.relative_to(root).as_posix()
    if output_relative in CANONICAL_CAMPAIGN_INPUTS:
        raise ContractError(f"output cannot overwrite campaign input: {output_relative}")
    source_paths = [root / "campaign.json"]
    source_paths.extend(root / name for name in CANONICAL_LEDGERS.values())
    seal = {
        "campaign_id": report.state["campaign_id"],
        "files": {path.name: sha256_file(path) for path in source_paths},
        "schema_version": 1,
        "sealed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "READY_FOR_DRAFTING",
    }
    atomic_json(output, seal)
    print(json.dumps({"output": str(output), "sha256": sha256_file(output), "status": "SEALED"}, sort_keys=True))
    return 0


def command_doctor(_: argparse.Namespace) -> int:
    payload = {
        "network_used": False,
        "python": sys.version.split()[0],
        "status": "READY" if sys.version_info >= (3, 10) else "BLOCKED",
        "version": VERSION,
    }
    print(json.dumps(payload, sort_keys=True))
    return 0 if payload["status"] == "READY" else 2


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--version", action="version", version=VERSION)
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor").set_defaults(handler=command_doctor)
    init = commands.add_parser("init")
    init.add_argument("--campaign-dir", required=True)
    init.add_argument("--campaign-id", required=True)
    init.add_argument("--company-name", required=True)
    init.add_argument("--project-slug", required=True)
    init.add_argument("--artifacts", required=True)
    init.add_argument("--mode", choices=("create", "review-repair"), default="create")
    init.set_defaults(handler=init_campaign)
    for name, handler in (("validate", command_validate), ("gate", command_gate)):
        sub = commands.add_parser(name)
        sub.add_argument("--campaign-dir", required=True)
        sub.set_defaults(handler=handler)
    seal = commands.add_parser("seal")
    seal.add_argument("--campaign-dir", required=True)
    seal.add_argument("--output", default="campaign-seal.json")
    seal.set_defaults(handler=command_seal)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        return int(args.handler(args))
    except ContractError as exc:
        print(json.dumps({"error": str(exc), "status": "ERROR"}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
