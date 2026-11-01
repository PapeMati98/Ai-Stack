"""Conservative provenance inventory for private release candidates.

This module checks inventory shape and coverage. It does not validate legal
rights, author identity, the contents of evidence, or publication approval.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


_COMPONENT_KEYS = {
    "id",
    "targets",
    "source",
    "license",
    "notice_paths",
    "evidence",
    "status",
}
_KNOWN_STATUSES = {"authored", "third-party", "unknown"}
_UNKNOWN_LICENSES = {"", "unknown", "unlicensed", "noassertion", "none", "n/a"}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_NOTICE_BYTES = 128 * 1024


def _literal_path(value: Any, label: str) -> str:
    """Validate a portable, literal relative path without normalizing it."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty literal path")
    if "\x00" in value or "\\" in value or value.startswith("/"):
        raise ValueError(f"{label} must be a portable relative path")
    if len(value) >= 2 and value[1] == ":":
        raise ValueError(f"{label} must not contain a drive prefix")
    pieces = value.split("/")
    if any(piece in {"", ".", ".."} for piece in pieces):
        raise ValueError(f"{label} must not contain empty or traversal segments")
    if any(piece.endswith((".", " ")) for piece in pieces):
        raise ValueError(f"{label} contains a non-portable path segment")
    if any(piece.split(".", 1)[0].upper() in _WINDOWS_RESERVED for piece in pieces):
        raise ValueError(f"{label} contains a reserved path segment")
    if any(":" in piece for piece in pieces):
        raise ValueError(f"{label} must not contain an alternate data stream")
    return value


_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _nonempty_evidence(value: Any) -> bool:
    """Recognize recorded evidence without exposing or interpreting its value."""
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple)):
        return bool(value) and all(_nonempty_evidence(item) for item in value)
    if isinstance(value, dict):
        return bool(value) and all(
            isinstance(key, str) and bool(key.strip()) and _nonempty_evidence(item)
            for key, item in value.items()
        )
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _manifest_files(manifest: dict[str, Any]) -> list[dict[str, str]]:
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise ValueError("manifest must be a version 1 object")
    sources = manifest.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("manifest sources must be a non-empty list")
    files: list[dict[str, str]] = []
    for source_index, source in enumerate(sources):
        if not isinstance(source, dict) or not isinstance(source.get("files"), list):
            raise ValueError(f"manifest source {source_index} must contain a files list")
        for file_index, file_entry in enumerate(source["files"]):
            label = f"manifest source {source_index} file {file_index}"
            if not isinstance(file_entry, dict):
                raise ValueError(f"{label} must be an object")
            target = _literal_path(file_entry.get("target"), f"{label} target")
            digest = file_entry.get("sha256")
            if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
                raise ValueError(f"{label} sha256 must be 64 lowercase hexadecimal characters")
            files.append({"target": target, "sha256": digest})
    if not files:
        raise ValueError("manifest must export at least one file")
    return files


def _component_fingerprint(
    component: dict[str, Any],
    target_digests: dict[str, list[str]],
) -> str:
    digests = sorted(
        {
            digest
            for target in component["targets"]
            for digest in target_digests.get(target, [])
        }
    )
    material = {
        "source": component["source"].strip(),
        "license": component["license"].strip(),
        "status": component["status"],
        "content_sha256": digests,
    }
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _safe_source_label(value: str) -> str:
    """Avoid publishing absolute/path-like source references in an inventory."""
    stripped = value.strip()
    if not stripped or any(character in stripped for character in ("/", "\\", ":", "\n", "\r")):
        return "[source reference omitted]"
    return stripped


def _markdown_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("`", "\\`").replace("\n", " ").replace("\r", " ")


def audit_provenance(manifest: dict, components: list[dict]) -> dict:
    """Inventory file coverage, component identity, notices, and open rights.

    ``status`` describes the rights gate. It remains ``OPEN`` until an
    independent owner/legal review supplies and verifies the applicable rights
    decisions. ``inventory_status`` separately reports whether structural
    coverage is exact. This function never grants a license or publication
    readiness.
    """
    files = _manifest_files(manifest)
    if not isinstance(components, list):
        raise ValueError("components must be a list")

    target_rows: dict[str, list[dict[str, str]]] = {}
    for row in files:
        target_rows.setdefault(row["target"], []).append(row)
    target_casefold: dict[str, list[str]] = {}
    target_digests: dict[str, list[str]] = {}
    for target, rows in target_rows.items():
        target_casefold.setdefault(target.casefold(), []).append(target)
        target_digests[target] = [row["sha256"] for row in rows]

    findings: list[dict[str, str]] = []

    def finding(code: str, *, target: str | None = None, component_id: str | None = None) -> None:
        item: dict[str, str] = {"code": code}
        if target is not None:
            item["target"] = target
        if component_id is not None:
            item["component_id"] = component_id
        findings.append(item)

    for folded_targets in target_casefold.values():
        if len(folded_targets) > 1:
            finding("manifest_target_ambiguous")

    coverage: dict[str, list[str]] = {target: [] for target in target_rows}
    component_records: list[dict[str, Any]] = []
    id_fingerprints: dict[str, set[str]] = {}
    fingerprints: set[str] = set()
    declared_notice_count = 0
    resolved_notice_hashes: set[str] = set()

    for component_index, component in enumerate(components):
        if not isinstance(component, dict) or set(component) != _COMPONENT_KEYS:
            raise ValueError(f"component {component_index} must have exactly the documented keys")
        component_id = component["id"]
        if not isinstance(component_id, str) or not component_id.strip() or len(component_id) > 200:
            raise ValueError(f"component {component_index} id must be a non-empty short string")
        source = component["source"]
        license_name = component["license"]
        status = component["status"]
        if not isinstance(source, str) or len(source) > 1000:
            raise ValueError(f"component {component_id!r} source must be a string")
        if not isinstance(license_name, str) or len(license_name) > 256:
            raise ValueError(f"component {component_id!r} license must be a short string")
        if not isinstance(status, str):
            raise ValueError(f"component {component_id!r} status must be a string")
        targets = component["targets"]
        notice_paths = component["notice_paths"]
        if not isinstance(targets, list) or not isinstance(notice_paths, list):
            raise ValueError(f"component {component_id!r} targets and notice_paths must be lists")
        safe_targets = [_literal_path(path, f"component {component_id!r} target") for path in targets]
        safe_notices = [_literal_path(path, f"component {component_id!r} notice path") for path in notice_paths]
        evidence = component["evidence"]
        # Ensure the evidence object is JSON-serializable, but never copy it to findings.
        try:
            json.dumps(evidence, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"component {component_id!r} evidence must be JSON-serializable") from exc

        for target in safe_targets:
            if target not in coverage:
                finding("component_target_missing", target=target, component_id=component_id)
            else:
                coverage[target].append(component_id)

        normalized_status = status.strip().casefold()
        normalized_license = license_name.strip().casefold()
        if normalized_status not in _KNOWN_STATUSES:
            finding("ownership_status_unknown", component_id=component_id)
        if normalized_status == "unknown":
            finding("ownership_unknown", component_id=component_id)
        if normalized_license in _UNKNOWN_LICENSES:
            finding("license_unknown", component_id=component_id)
        if not _nonempty_evidence(evidence):
            finding("evidence_missing", component_id=component_id)

        for path in safe_notices:
            declared_notice_count += 1
            exact_rows = target_rows.get(path, [])
            folded_matches = target_casefold.get(path.casefold(), [])
            if len(folded_matches) > 1:
                finding("notice_path_ambiguous", target=path, component_id=component_id)
            elif len(exact_rows) == 1:
                resolved_notice_hashes.add(exact_rows[0]["sha256"])
            elif len(exact_rows) > 1:
                finding("notice_path_ambiguous", target=path, component_id=component_id)
            else:
                if len(folded_matches) == 1:
                    finding("notice_path_case_mismatch", target=path, component_id=component_id)
                else:
                    finding("notice_path_missing", target=path, component_id=component_id)

        fingerprint = _component_fingerprint(
            {"source": source, "license": license_name, "status": status, "targets": safe_targets},
            target_digests,
        )
        fingerprints.add(fingerprint)
        id_fingerprints.setdefault(component_id, set()).add(fingerprint)
        component_records.append(
            {
                "id": component_id,
                "source": _safe_source_label(source),
                "license": license_name.strip() or "UNKNOWN",
                "status": status.strip() or "unknown",
                "targets": sorted(set(safe_targets)),
                "notice_paths": sorted(set(safe_notices)),
                "notice_hashes": sorted(
                    {
                        target_rows[path][0]["sha256"]
                        for path in safe_notices
                        if len(target_rows.get(path, [])) == 1
                        and len(target_casefold.get(path.casefold(), [])) == 1
                    }
                ),
                "evidence_present": _nonempty_evidence(evidence),
            }
        )

    for target, component_ids in coverage.items():
        if len(component_ids) == 0:
            finding("file_coverage_missing", target=target)
        elif len(component_ids) > 1:
            finding("file_coverage_duplicate", target=target)

    for component_id, values in id_fingerprints.items():
        if len(values) > 1:
            finding("component_id_conflict", component_id=component_id)

    # Evidence values are owner-supplied assertions. This library does not
    # inspect or authenticate them, so the public-rights gate remains open.
    if component_records:
        finding("rights_evidence_not_independently_verified")

    structural_codes = {
        "manifest_target_ambiguous",
        "component_target_missing",
        "file_coverage_missing",
        "file_coverage_duplicate",
        "component_id_conflict",
        "notice_path_ambiguous",
        "notice_path_case_mismatch",
        "notice_path_missing",
    }
    inventory_status = "OPEN" if any(item["code"] in structural_codes for item in findings) else "PASS"
    unique_component_count = len(fingerprints)
    duplicate_component_copies = max(0, len(components) - unique_component_count)
    report = {
        "version": 1,
        "status": "OPEN",
        "inventory_status": inventory_status,
        "public_ready": False,
        "findings": findings,
        "counts": {
            "exported_files": len(files),
            "covered_files_once": sum(1 for values in coverage.values() if len(values) == 1),
            "components_declared": len(components),
            "unique_components": unique_component_count,
            "duplicate_component_copies": duplicate_component_copies,
            "notice_paths_declared": declared_notice_count,
            "unique_notice_hashes": len(resolved_notice_hashes),
        },
        "components": sorted(component_records, key=lambda item: (item["id"].casefold(), item["id"])),
        "limitations": [
            "Owner-supplied rights assertions and evidence are inventoried, not independently verified.",
            "This inventory does not choose a license or publisher and does not grant publication rights.",
        ],
    }
    return report


def write_notices(report: dict, destination) -> dict:
    """Create a new bounded Markdown provenance inventory without license text."""
    if not isinstance(report, dict) or report.get("version") != 1:
        raise ValueError("report must be a version 1 provenance inventory")
    components = report.get("components")
    counts = report.get("counts")
    if not isinstance(components, list) or not isinstance(counts, dict):
        raise ValueError("report must contain component and count inventories")
    try:
        exported_files = int(counts["exported_files"])
        unique_components = int(counts["unique_components"])
        unique_notice_hashes = int(counts["unique_notice_hashes"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("report counts are incomplete") from exc
    if min(exported_files, unique_components, unique_notice_hashes) < 0:
        raise ValueError("report counts must not be negative")

    lines = [
        "# Private provenance and notices inventory",
        "",
        f"- Inventory status: `{_markdown_text(str(report.get('inventory_status', 'OPEN')))}`",
        "- Rights status: `OPEN`",
        "- Public readiness: `false`",
        f"- Exported files: {exported_files}",
        f"- Unique components: {unique_components}",
        f"- Unique notice hashes: {unique_notice_hashes}",
        "",
        "This inventory records supplied assertions. It does not verify legal rights,",
        "reproduce license text, choose a license or publisher, or grant publication rights.",
        "Existing source and copyright notices must remain preserved in the candidate.",
        "",
        "## Components",
        "",
    ]
    for component in components:
        if not isinstance(component, dict):
            raise ValueError("report component entries must be objects")
        component_id = component.get("id")
        license_name = component.get("license")
        source = component.get("source")
        status = component.get("status")
        targets = component.get("targets")
        notice_paths = component.get("notice_paths")
        notice_hashes = component.get("notice_hashes")
        if not all(isinstance(value, str) for value in (component_id, license_name, source, status)):
            raise ValueError("report component labels are incomplete")
        if not isinstance(targets, list) or not all(isinstance(path, str) for path in targets):
            raise ValueError("report component targets are invalid")
        if not isinstance(notice_paths, list) or not all(isinstance(path, str) for path in notice_paths):
            raise ValueError("report component notice paths are invalid")
        if not isinstance(notice_hashes, list) or not all(isinstance(digest, str) and _SHA256_RE.fullmatch(digest) for digest in notice_hashes):
            raise ValueError("report component notice hashes are invalid")
        lines.extend(
            [
                f"### {_markdown_text(component_id)}",
                "",
                f"- Source: {_markdown_text(source)}",
                f"- Declared license: {_markdown_text(license_name)}",
                f"- Ownership status: {_markdown_text(status)}",
                f"- Evidence recorded: {'yes' if component.get('evidence_present') is True else 'no'}",
                "- Covered targets:",
            ]
        )
        if targets:
            lines.extend(f"  - `{_markdown_text(path)}`" for path in targets)
        else:
            lines.append("  - (none)")
        lines.append("- Notice paths:")
        if notice_paths:
            lines.extend(f"  - `{_markdown_text(path)}`" for path in notice_paths)
        else:
            lines.append("  - (none recorded)")
        if notice_hashes:
            lines.extend(f"- Notice SHA256: `{digest}`" for digest in notice_hashes)
        lines.append("")

    rendered = "\n".join(lines).rstrip() + "\n"
    encoded = rendered.encode("utf-8")
    if len(encoded) > _MAX_NOTICE_BYTES:
        raise ValueError("provenance inventory exceeds the bounded output size")
    output_path = Path(destination)
    try:
        with output_path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(rendered)
    except FileExistsError as exc:
        raise ValueError("destination already exists; refusing to overwrite") from exc
    return {
        "version": 1,
        "status": "OPEN",
        "destination_created": True,
        "bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "public_ready": False,
    }
