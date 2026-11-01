#!/usr/bin/env python3
"""Deterministic acceptance gate for investor-facing artifact packages."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import Any


VERSION = "0.1.0"
FUNDRAISING_WORKFLOW_SHA256 = "b6053eeae545781b21372e43354462ef35712c6938b2192209ade293e7f65918"
ID_RE = re.compile(r"^[a-z][a-z0-9-]{2,95}$")
SHA_RE = re.compile(r"^[a-f0-9]{64}$")
ARTIFACT_TYPES = {"deck", "memorandum", "one-pager"}
CANONICAL_CAMPAIGN_INPUTS = frozenset(
    {"campaign.json", "sources.json", "claims.json", "numbers.json"}
)
GATE_BINDINGS = {
    "factual": "editable",
    "financial": "editable",
    "narrative": "editable",
    "visual": "pdf",
    "export": "pdf",
    "confidentiality": "both",
}
SEVERITIES = {"P0", "P1", "P2", "P3"}


class AcceptanceError(RuntimeError):
    pass


def no_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AcceptanceError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=no_duplicate_pairs)
    except (OSError, UnicodeError, json.JSONDecodeError, AcceptanceError) as exc:
        raise AcceptanceError(f"cannot read strict JSON {path}: {exc}") from exc


@lru_cache(maxsize=1)
def load_fundraising_workflow() -> ModuleType:
    script = (
        Path(__file__).resolve().parents[2]
        / "fundraising"
        / "scripts"
        / "fundraising_workflow.py"
    )
    if not script.is_file() or script.is_symlink():
        raise AcceptanceError(f"canonical campaign validator is unavailable: {script}")
    actual_digest = hashlib.sha256(script.read_bytes()).hexdigest()
    if actual_digest != FUNDRAISING_WORKFLOW_SHA256:
        raise AcceptanceError(
            "canonical campaign validator digest mismatch: "
            f"expected={FUNDRAISING_WORKFLOW_SHA256}, actual={actual_digest}"
        )
    module_name = f"_fundraising_workflow_{actual_digest[:16]}"
    spec = importlib.util.spec_from_file_location(module_name, script)
    if spec is None or spec.loader is None:
        raise AcceptanceError(f"cannot load canonical campaign validator: {script}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(module_name, None)
        raise AcceptanceError(f"canonical campaign validator failed to load: {exc}") from exc
    if getattr(module, "VERSION", None) != VERSION or not callable(getattr(module, "validate_campaign", None)):
        sys.modules.pop(module_name, None)
        raise AcceptanceError("canonical campaign validator version or interface mismatch")
    return module


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


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def measured_pdf_page_count(path: Path) -> int:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise AcceptanceError("pypdf is required for mechanical PDF page-count validation") from exc
    try:
        return len(PdfReader(str(path), strict=True).pages)
    except Exception as exc:
        raise AcceptanceError(f"cannot parse PDF for page-count validation: {path}: {exc}") from exc


def is_reparse(path: Path) -> bool:
    try:
        attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & 0x400)


def safe_file(root: Path, raw: Any, label: str, *, must_exist: bool = True) -> Path:
    if not isinstance(raw, str) or not raw or "\\" in raw:
        raise AcceptanceError(f"{label} must be a non-empty forward-slash relative path")
    candidate = Path(raw)
    if candidate.is_absolute() or ".." in candidate.parts or "." in candidate.parts:
        raise AcceptanceError(f"{label} escapes the campaign root")
    resolved_root = root.resolve()
    resolved = (resolved_root / candidate).resolve(strict=False)
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise AcceptanceError(f"{label} escapes the campaign root")
    current = resolved_root
    for part in candidate.parts:
        current = current / part
        if current.exists() and (current.is_symlink() or is_reparse(current)):
            raise AcceptanceError(f"{label} crosses a link or reparse point: {current}")
    if must_exist and (not resolved.is_file() or resolved.is_symlink() or is_reparse(resolved)):
        raise AcceptanceError(f"{label} is not a regular file: {raw}")
    return resolved


def valid_id(value: Any, prefix: str) -> bool:
    return isinstance(value, str) and value.startswith(prefix) and bool(ID_RE.fullmatch(value))


def require_mapping(value: Any, label: str, report: "AcceptanceReport") -> dict[str, Any]:
    if not isinstance(value, dict):
        report.errors.append(f"{label} must be an object")
        return {}
    return value


def require_list(value: Any, label: str, report: "AcceptanceReport") -> list[Any]:
    if not isinstance(value, list):
        report.errors.append(f"{label} must be an array")
        return []
    return value


@dataclass
class AcceptanceReport:
    errors: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> str:
        if self.errors:
            return "INVALID"
        if self.blockers:
            return "NOT_ACCEPTED"
        return "ACCEPTABLE"

    def output(self) -> dict[str, Any]:
        return {
            "blockers": sorted(set(self.blockers)),
            "errors": sorted(set(self.errors)),
            "state": self.state,
            "status": self.status,
            "warnings": sorted(set(self.warnings)),
        }


def binding_hash(artifact: dict[str, Any], kind: str) -> str:
    if kind == "editable":
        return artifact["editable_sha256"]
    if kind == "pdf":
        return artifact["pdf_sha256"]
    if kind == "both":
        return sha256_text(f"{artifact['editable_sha256']}:{artifact['pdf_sha256']}")
    raise AcceptanceError(f"unknown binding kind: {kind}")


def validate_candidate(root: Path, receipt_raw: str) -> AcceptanceReport:
    report = AcceptanceReport()
    root = root.resolve()
    try:
        receipt_path = safe_file(root, receipt_raw, "receipt")
        candidate = require_mapping(load_json(receipt_path), "receipt", report)
    except AcceptanceError as exc:
        report.errors.append(str(exc))
        return report

    allowed_root = {
        "schema_version",
        "receipt_id",
        "campaign_id",
        "campaign_seal",
        "artifacts",
        "content_lineage",
        "design_lineage",
        "reviews",
        "findings",
        "repair_budget",
        "distribution_status",
    }
    unknown = sorted(set(candidate) - allowed_root)
    if unknown:
        report.errors.append(f"receipt has unknown fields: {unknown}")
    if type(candidate.get("schema_version")) is not int or candidate.get("schema_version") != 1:
        report.errors.append("receipt.schema_version must equal 1")
    receipt_id = candidate.get("receipt_id")
    if not valid_id(receipt_id, "receipt-"):
        report.errors.append("receipt_id is invalid")
    campaign_id = candidate.get("campaign_id")
    if not valid_id(campaign_id, "campaign-"):
        report.errors.append("campaign_id is invalid")
    if candidate.get("distribution_status") != "not_authorized":
        report.errors.append("distribution_status must remain not_authorized")

    seal_info = require_mapping(candidate.get("campaign_seal"), "campaign_seal", report)
    campaign: dict[str, Any] = {}
    seal: dict[str, Any] = {}
    campaign_gate_status = "UNAVAILABLE"
    try:
        seal_path = safe_file(root, seal_info.get("path"), "campaign_seal.path")
        expected_seal_hash = seal_info.get("sha256")
        if not isinstance(expected_seal_hash, str) or not SHA_RE.fullmatch(expected_seal_hash):
            raise AcceptanceError("campaign_seal.sha256 is invalid")
        if sha256_file(seal_path) != expected_seal_hash:
            raise AcceptanceError("campaign seal hash mismatch")
        seal = require_mapping(load_json(seal_path), "campaign seal", report)
        if type(seal.get("schema_version")) is not int or seal.get("schema_version") != 1 or seal.get("status") != "READY_FOR_DRAFTING":
            raise AcceptanceError("campaign seal is not READY_FOR_DRAFTING")
        if seal.get("campaign_id") != campaign_id:
            raise AcceptanceError("campaign seal id does not match receipt")
        files = seal.get("files")
        required_seal_files = {"campaign.json", "sources.json", "claims.json", "numbers.json"}
        if not isinstance(files, dict) or set(files) != required_seal_files:
            raise AcceptanceError("campaign seal must bind exactly campaign.json and all three ledgers")
        for relative, expected_hash in files.items():
            bound_path = safe_file(root, relative, f"campaign_seal.files.{relative}")
            if not isinstance(expected_hash, str) or not SHA_RE.fullmatch(expected_hash):
                raise AcceptanceError(f"campaign seal hash is invalid: {relative}")
            if sha256_file(bound_path) != expected_hash:
                raise AcceptanceError(f"campaign seal file changed: {relative}")
        campaign = require_mapping(load_json(root / "campaign.json"), "campaign", report)
        if campaign.get("campaign_id") != campaign_id:
            raise AcceptanceError("campaign.json id does not match receipt")
        if campaign.get("distribution_status") != "not_authorized":
            raise AcceptanceError("campaign distribution is not blocked")
        campaign_report = load_fundraising_workflow().validate_campaign(root)
        campaign_gate_status = campaign_report.status
        report.errors.extend(f"campaign gate: {item}" for item in campaign_report.errors)
        report.blockers.extend(f"campaign gate: {item}" for item in campaign_report.blockers)
        report.warnings.extend(f"campaign gate: {item}" for item in campaign_report.warnings)
        if campaign_report.status not in {"READY", "INVALID", "BLOCKED"}:
            raise AcceptanceError(f"canonical campaign validator returned an unknown status: {campaign_report.status}")
    except AcceptanceError as exc:
        report.errors.append(str(exc))

    content_lineage = require_mapping(candidate.get("content_lineage"), "content_lineage", report)
    design_lineage = require_mapping(candidate.get("design_lineage"), "design_lineage", report)
    content_maker = content_lineage.get("maker")
    design_maker = design_lineage.get("maker")
    if not isinstance(content_maker, str) or not content_maker.strip():
        report.errors.append("content_lineage.maker is required")
    if not isinstance(design_maker, str) or not design_maker.strip():
        report.errors.append("design_lineage.maker is required")
    campaign_content = campaign.get("content_lineage", {}) if isinstance(campaign, dict) else {}
    campaign_design = campaign.get("design_lineage", {}) if isinstance(campaign, dict) else {}
    if isinstance(content_maker, str) and content_maker != campaign_content.get("maker"):
        report.errors.append("content_lineage.maker must equal the sealed campaign content maker")
    if isinstance(design_maker, str) and design_maker != campaign_design.get("maker"):
        report.errors.append("design_lineage.maker must equal the sealed campaign design maker")

    artifacts = require_list(candidate.get("artifacts"), "artifacts", report)
    artifact_by_id: dict[str, dict[str, Any]] = {}
    artifact_types: set[str] = set()
    used_paths: set[Path] = set()
    for index, raw in enumerate(artifacts):
        artifact = require_mapping(raw, f"artifacts[{index}]", report)
        artifact_id = artifact.get("artifact_id")
        if not valid_id(artifact_id, "artifact-"):
            report.errors.append(f"artifacts[{index}].artifact_id is invalid")
            continue
        if artifact_id in artifact_by_id:
            report.errors.append(f"duplicate artifact_id: {artifact_id}")
            continue
        artifact_type = artifact.get("artifact_type")
        if artifact_type not in ARTIFACT_TYPES:
            report.errors.append(f"{artifact_id} has invalid artifact_type")
        elif artifact_type in artifact_types:
            report.errors.append(f"duplicate artifact_type: {artifact_type}")
        else:
            artifact_types.add(artifact_type)
        for kind in ("editable", "pdf"):
            raw_path = artifact.get(f"{kind}_path")
            expected_hash = artifact.get(f"{kind}_sha256")
            try:
                path = safe_file(root, raw_path, f"{artifact_id}.{kind}_path")
                if path in used_paths:
                    raise AcceptanceError(f"artifact path is reused: {raw_path}")
                used_paths.add(path)
                if not isinstance(expected_hash, str) or not SHA_RE.fullmatch(expected_hash):
                    raise AcceptanceError(f"{artifact_id}.{kind}_sha256 is invalid")
                if sha256_file(path) != expected_hash:
                    raise AcceptanceError(f"{artifact_id} {kind} hash mismatch")
            except AcceptanceError as exc:
                report.errors.append(str(exc))
        page_count = artifact.get("pdf_page_count")
        if not isinstance(page_count, int) or isinstance(page_count, bool) or page_count < 1:
            report.errors.append(f"{artifact_id}.pdf_page_count must be positive")
        else:
            try:
                pdf_path = safe_file(root, artifact.get("pdf_path"), f"{artifact_id}.pdf_path")
                measured_pages = measured_pdf_page_count(pdf_path)
                if measured_pages != page_count:
                    report.errors.append(
                        f"{artifact_id}.pdf_page_count does not match the PDF bytes: declared={page_count}, measured={measured_pages}"
                    )
            except AcceptanceError as exc:
                report.errors.append(str(exc))
        if artifact_type == "one-pager" and page_count != 1:
            report.errors.append("one-pager PDF must contain exactly one page")
        artifact_by_id[artifact_id] = artifact

    requested = campaign.get("artifact_types", []) if isinstance(campaign, dict) else []
    if not isinstance(requested, list) or set(requested) != artifact_types:
        report.errors.append("artifact set does not match campaign.json")

    reviews = require_list(candidate.get("reviews"), "reviews", report)
    review_by_gate: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(reviews):
        review = require_mapping(raw, f"reviews[{index}]", report)
        gate = review.get("gate")
        if gate not in GATE_BINDINGS:
            report.errors.append(f"reviews[{index}].gate is invalid")
            continue
        if gate in review_by_gate:
            report.errors.append(f"duplicate review gate: {gate}")
            continue
        review_by_gate[gate] = review
        expected_kind = GATE_BINDINGS[gate]
        if review.get("binding_kind") != expected_kind:
            report.errors.append(f"{gate} review uses the wrong binding kind")
        reviewer = review.get("reviewer")
        if not isinstance(reviewer, str) or not reviewer.strip():
            report.errors.append(f"{gate} reviewer is required")
        if gate in {"factual", "financial", "narrative"} and reviewer == content_maker:
            report.errors.append(f"{gate} reviewer cannot equal content maker")
        if gate in {"visual", "export"} and reviewer == design_maker:
            report.errors.append(f"{gate} reviewer cannot equal design maker")
        if gate == "confidentiality" and reviewer in {content_maker, design_maker}:
            report.errors.append("confidentiality reviewer must be independent from both makers")
        status = review.get("status")
        if status not in {"PASS", "FAIL", "BLOCKED"}:
            report.errors.append(f"{gate} review status is invalid")
        elif status != "PASS":
            report.blockers.append(f"review gate is not PASS: {gate}")
        hashes = review.get("artifact_hashes")
        if not isinstance(hashes, dict) or set(hashes) != set(artifact_by_id):
            report.errors.append(f"{gate} review does not bind every current artifact")
        else:
            for artifact_id, artifact in artifact_by_id.items():
                if hashes.get(artifact_id) != binding_hash(artifact, expected_kind):
                    report.errors.append(f"{gate} review has stale binding for {artifact_id}")
        try:
            evidence = safe_file(root, review.get("evidence_ref"), f"{gate}.evidence_ref")
            expected_evidence_hash = review.get("evidence_sha256")
            if not isinstance(expected_evidence_hash, str) or not SHA_RE.fullmatch(expected_evidence_hash):
                raise AcceptanceError(f"{gate}.evidence_sha256 is invalid")
            if sha256_file(evidence) != expected_evidence_hash:
                raise AcceptanceError(f"{gate} evidence hash mismatch")
        except AcceptanceError as exc:
            report.errors.append(str(exc))
    missing_gates = sorted(set(GATE_BINDINGS) - set(review_by_gate))
    if missing_gates:
        report.blockers.append(f"missing review gates: {missing_gates}")

    findings = require_list(candidate.get("findings"), "findings", report)
    for index, raw in enumerate(findings):
        finding = require_mapping(raw, f"findings[{index}]", report)
        severity = finding.get("severity")
        status = finding.get("status")
        if severity not in SEVERITIES or status not in {"open", "resolved"}:
            report.errors.append(f"findings[{index}] has invalid severity or status")
            continue
        if status == "open" and severity in {"P0", "P1"}:
            report.blockers.append(f"open {severity} finding: {finding.get('code', index)}")
        elif status == "open":
            report.warnings.append(f"open {severity} finding: {finding.get('code', index)}")

    repair = require_mapping(candidate.get("repair_budget"), "repair_budget", report)
    maximum = repair.get("max_rounds")
    used = repair.get("used_rounds")
    if not isinstance(maximum, int) or isinstance(maximum, bool) or not 1 <= maximum <= 3:
        report.errors.append("repair_budget.max_rounds must be between 1 and 3")
    if not isinstance(used, int) or isinstance(used, bool) or used < 0:
        report.errors.append("repair_budget.used_rounds must be non-negative")
    elif isinstance(maximum, int) and used > maximum:
        report.blockers.append("repair budget is exceeded")

    report.state = {
        "artifact_bindings": {
            artifact_id: {
                "both": binding_hash(artifact, "both"),
                "editable": binding_hash(artifact, "editable"),
                "pdf": binding_hash(artifact, "pdf"),
            }
            for artifact_id, artifact in sorted(artifact_by_id.items())
        },
        "artifact_count": len(artifact_by_id),
        "campaign_id": campaign_id,
        "campaign_gate_status": campaign_gate_status,
        "candidate_receipt_path": receipt_raw,
        "candidate_receipt_sha256": sha256_file(receipt_path),
        "gate_count": len(review_by_gate),
        "receipt_id": receipt_id,
    }
    return report


def print_report(report: AcceptanceReport) -> None:
    print(json.dumps(report.output(), ensure_ascii=True, indent=2, sort_keys=True))


def command_validate(args: argparse.Namespace) -> int:
    report = validate_candidate(Path(args.campaign_dir), args.receipt)
    print_report(report)
    if report.errors:
        return 2
    if report.blockers:
        return 3
    return 0


def command_seal(args: argparse.Namespace) -> int:
    root = Path(args.campaign_dir).resolve()
    report = validate_candidate(root, args.receipt)
    if report.status != "ACCEPTABLE":
        print_report(report)
        return 2 if report.errors else 3
    try:
        output = safe_file(root, args.output, "output", must_exist=False)
        protected = {
            os.path.normcase(str((root / relative).resolve(strict=False)))
            for relative in CANONICAL_CAMPAIGN_INPUTS
        }
        if os.path.normcase(str(output)) in protected:
            raise AcceptanceError("output cannot overwrite canonical campaign inputs")
    except AcceptanceError as exc:
        print(json.dumps({"error": str(exc), "status": "ERROR"}, sort_keys=True), file=sys.stderr)
        return 2
    final = {
        "acceptance_id": f"acceptance-{report.state['receipt_id'][len('receipt-'):]}",
        "accepted_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "artifact_bindings": report.state["artifact_bindings"],
        "campaign_id": report.state["campaign_id"],
        "candidate_receipt": {
            "path": report.state["candidate_receipt_path"],
            "sha256": report.state["candidate_receipt_sha256"],
        },
        "distribution_status": "not_authorized",
        "schema_version": 1,
        "status": "ACCEPTED",
    }
    atomic_json(output, final)
    print(json.dumps({"output": str(output), "sha256": sha256_file(output), "status": "ACCEPTED"}, sort_keys=True))
    return 0


def command_verify(args: argparse.Namespace) -> int:
    root = Path(args.campaign_dir).resolve()
    try:
        acceptance_path = safe_file(root, args.acceptance, "acceptance")
        final = load_json(acceptance_path)
        if (
            not isinstance(final, dict)
            or type(final.get("schema_version")) is not int
            or final.get("schema_version") != 1
            or final.get("status") != "ACCEPTED"
        ):
            raise AcceptanceError("final receipt is not ACCEPTED schema version 1")
        if final.get("distribution_status") != "not_authorized":
            raise AcceptanceError("final receipt attempts to authorize distribution")
        candidate = final.get("candidate_receipt")
        if not isinstance(candidate, dict):
            raise AcceptanceError("final receipt has no candidate binding")
        candidate_path = safe_file(root, candidate.get("path"), "candidate_receipt.path")
        if sha256_file(candidate_path) != candidate.get("sha256"):
            raise AcceptanceError("candidate receipt changed after acceptance")
        report = validate_candidate(root, candidate.get("path"))
        if report.status != "ACCEPTABLE":
            print_report(report)
            return 3
        if final.get("campaign_id") != report.state["campaign_id"]:
            raise AcceptanceError("final receipt campaign id drift")
        if final.get("artifact_bindings") != report.state["artifact_bindings"]:
            raise AcceptanceError("artifact bindings changed after acceptance")
        print(json.dumps({"acceptance": str(acceptance_path), "status": "VERIFIED"}, sort_keys=True))
        return 0
    except AcceptanceError as exc:
        print(json.dumps({"error": str(exc), "status": "INVALID"}, sort_keys=True), file=sys.stderr)
        return 2


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
    validate = commands.add_parser("validate")
    validate.add_argument("--campaign-dir", required=True)
    validate.add_argument("--receipt", required=True)
    validate.set_defaults(handler=command_validate)
    seal = commands.add_parser("seal")
    seal.add_argument("--campaign-dir", required=True)
    seal.add_argument("--receipt", required=True)
    seal.add_argument("--output", default="final-acceptance.json")
    seal.set_defaults(handler=command_seal)
    verify = commands.add_parser("verify")
    verify.add_argument("--campaign-dir", required=True)
    verify.add_argument("--acceptance", required=True)
    verify.set_defaults(handler=command_verify)
    return root


def main() -> int:
    args = parser().parse_args()
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
