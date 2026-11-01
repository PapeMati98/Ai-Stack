#!/usr/bin/env python3
"""Validate and read-only inspect the Capability Hub V1.2 empirical matrix."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from capability_core import (
    CAPABILITY_MARKER_NAME,
    LEGACY_MARKER_NAME,
    SLUG,
    CapabilityError,
    absolute_plain_path,
    canonical_json_hash,
    find_managed_marker,
    parse_iso_timestamp,
    read_marker,
    sha256_bytes,
    strict_json_loads,
)
from capability_targets import (
    TargetRegistry,
    guard_hashes,
    load_registry,
    resolve_target,
)


MATRIX_NAME = "capability-matrix-v1.2.json"
ROW_KEYS = {
    "activation_boundary",
    "approval_posture",
    "backup_like_count",
    "blocker",
    "capability_target_state",
    "collision",
    "discovery_evidence",
    "discovery_status",
    "file_lock_behavior",
    "host",
    "observed_at",
    "rollout_status",
    "rules_mutation",
    "runtime_version",
    "runtime_version_evidence",
    "scan_root",
    "scan_root_evidence",
    "scan_root_inventory_sha256",
    "symlink_behavior",
    "tool",
}
ROLLOUT_STATUSES = {"blocked", "eligible", "pilot-verified"}
COLLISIONS = {"exact-id", "none", "semantic-overlap"}


@dataclass(frozen=True)
class CapabilityMatrix:
    path: Path
    capability_id: str
    verified_at: str
    targets: dict[str, dict[str, Any]]


def _nonempty(row: dict[str, Any], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise CapabilityError(f"Matrix field {key} must be a non-empty string")
    return value


def load_matrix(
    path: Path | str,
    registry: TargetRegistry,
    *,
    raw: bytes | None = None,
) -> CapabilityMatrix:
    if raw is None:
        matrix_path = absolute_plain_path(path, must_exist=True)
        if not stat.S_ISREG(matrix_path.lstat().st_mode):
            raise CapabilityError(f"Capability matrix is not a plain file: {matrix_path}")
        raw = matrix_path.read_bytes()
    else:
        matrix_path = Path(str(path))
    try:
        value = strict_json_loads(raw.decode("ascii", errors="strict"))
    except (OSError, UnicodeDecodeError, CapabilityError) as exc:
        raise CapabilityError(f"Invalid capability matrix: {matrix_path}") from exc
    if not isinstance(value, dict) or set(value) != {
        "capability_id",
        "schema_version",
        "targets",
        "verified_at",
    }:
        raise CapabilityError("Capability matrix top-level keys are invalid")
    if value.get("schema_version") != 1:
        raise CapabilityError("Unsupported capability matrix schema")
    capability_id = value.get("capability_id")
    if not isinstance(capability_id, str) or not SLUG.fullmatch(capability_id):
        raise CapabilityError("Capability matrix capability_id is invalid")
    verified_at = value.get("verified_at")
    if not isinstance(verified_at, str):
        raise CapabilityError("Capability matrix verified_at is invalid")
    parse_iso_timestamp(verified_at, "verified_at")
    rows = value.get("targets")
    if not isinstance(rows, list) or len(rows) != len(registry.targets):
        raise CapabilityError("Capability matrix must contain every registered target")
    targets: dict[str, dict[str, Any]] = {}
    for raw in rows:
        if not isinstance(raw, dict) or set(raw) != ROW_KEYS:
            raise CapabilityError("Capability matrix row keys are invalid")
        row = dict(raw)
        tool = _nonempty(row, "tool")
        if tool not in registry.targets or tool in targets:
            raise CapabilityError(f"Capability matrix tool is invalid or duplicate: {tool}")
        for key in ROW_KEYS - {
            "backup_like_count",
            "blocker",
            "rules_mutation",
        }:
            _nonempty(row, key)
        parse_iso_timestamp(_nonempty(row, "observed_at"), "observed_at")
        if row["activation_boundary"] != registry.targets[tool].activation_boundary:
            raise CapabilityError(f"Matrix activation boundary drift for {tool}")
        if row["host"].casefold() != registry.targets[tool].expected_host.casefold():
            raise CapabilityError(f"Matrix host identity drift for {tool}")
        if row.get("rules_mutation") is not False:
            raise CapabilityError(f"Matrix rules mutation must be false for {tool}")
        if (
            not isinstance(row.get("backup_like_count"), int)
            or row["backup_like_count"] < 0
        ):
            raise CapabilityError(f"Matrix backup count is invalid for {tool}")
        if row.get("collision") not in COLLISIONS:
            raise CapabilityError(f"Matrix collision state is invalid for {tool}")
        inventory_hash = row.get("scan_root_inventory_sha256")
        if (
            not isinstance(inventory_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", inventory_hash)
        ):
            raise CapabilityError(f"Matrix inventory hash is invalid for {tool}")
        status = row.get("rollout_status")
        if status not in ROLLOUT_STATUSES:
            raise CapabilityError(f"Matrix rollout status is invalid for {tool}")
        blocker = row.get("blocker")
        if status == "blocked":
            if not isinstance(blocker, str) or not blocker:
                raise CapabilityError(f"Blocked matrix row lacks blocker for {tool}")
        else:
            if blocker is not None:
                raise CapabilityError(f"Eligible matrix row has a blocker for {tool}")
            if row["collision"] != "none" or row["backup_like_count"] != 0:
                raise CapabilityError(f"Eligible matrix row is inconsistent for {tool}")
        scan_root_value = str(row["scan_root"])
        scan_root = (
            PureWindowsPath(scan_root_value)
            if registry.targets[tool].platform == "windows"
            else PurePosixPath(scan_root_value)
        )
        if not scan_root.is_absolute():
            raise CapabilityError(f"Matrix scan root is not absolute for {tool}")
        targets[tool] = row
    if set(targets) != set(registry.targets):
        raise CapabilityError("Capability matrix target set differs from registry")
    return CapabilityMatrix(matrix_path, capability_id, verified_at, targets)


def _backup_count(scan_root: Path) -> int:
    with os.scandir(scan_root) as iterator:
        entries = list(iterator)
    count = 0
    for entry in entries:
        if ".backup-" not in entry.name.casefold():
            continue
        details = entry.stat(follow_symlinks=False)
        if entry.is_symlink() or not stat.S_ISDIR(details.st_mode):
            raise CapabilityError(
                f"Backup-like scan-root entry is not a plain directory: {entry.name}"
            )
        count += 1
    return count


def scan_root_inventory(
    scan_root: Path, *, exclude_name: str | None = None
) -> str:
    rows: list[dict[str, Any]] = []
    excluded = 0
    with os.scandir(scan_root) as iterator:
        entries = sorted(iterator, key=lambda item: item.name.encode("utf-8"))
    for entry in entries:
        if (
            exclude_name is not None
            and os.path.normcase(entry.name) == os.path.normcase(exclude_name)
        ):
            excluded += 1
            continue
        details = entry.stat(follow_symlinks=False)
        if entry.is_symlink():
            kind = "symlink"
        elif stat.S_ISDIR(details.st_mode):
            kind = "directory"
        elif stat.S_ISREG(details.st_mode):
            kind = "file"
        else:
            kind = "other"
        row: dict[str, Any] = {
            "kind": kind,
            "name": entry.name,
            "skill_sha256": None,
            "managed_marker": None,
        }
        if kind == "directory":
            directory = Path(entry.path)
            skill = directory / "SKILL.md"
            if skill.exists():
                plain = absolute_plain_path(skill, must_exist=True)
                skill_details = plain.lstat()
                if not stat.S_ISREG(skill_details.st_mode) or skill_details.st_nlink != 1:
                    raise CapabilityError(
                        f"Inventory SKILL.md is not a single-link plain file: {skill}"
                    )
                row["skill_sha256"] = sha256_bytes(plain.read_bytes())
            marker = find_managed_marker(directory)
            if marker is not None:
                row["managed_marker"] = marker.name
        rows.append(row)
    if exclude_name is not None and excluded != 1:
        raise CapabilityError(
            "Excluded scan-root target must identify exactly one direct child"
        )
    return canonical_json_hash({"entries": rows})


def inspect_live(
    matrix: CapabilityMatrix,
    registry: TargetRegistry,
    tool: str,
) -> dict[str, Any]:
    row = matrix.targets[tool]
    target = resolve_target(registry, tool, matrix.capability_id)
    scan_root = absolute_plain_path(target.scan_root, must_exist=True)
    if not scan_root.is_dir():
        raise CapabilityError(f"Scan root is not a directory: {scan_root}")
    if os.path.normcase(str(scan_root)) != os.path.normcase(str(row["scan_root"])):
        raise CapabilityError(f"Resolved scan root differs from matrix for {tool}")
    backup_count = _backup_count(scan_root)
    if not target.target.exists():
        state = "absent"
    else:
        marker_path = find_managed_marker(target.target)
        if marker_path is None:
            state = "unmanaged-existing"
        elif marker_path.name == LEGACY_MARKER_NAME:
            state = "legacy-managed"
        elif marker_path.name == CAPABILITY_MARKER_NAME:
            marker = read_marker(marker_path)
            state = (
                "managed-current-id"
                if marker.data.get("capability_id") == matrix.capability_id
                else "managed-other-id"
            )
        else:
            state = "unknown-marker"
    expected_state = str(row["capability_target_state"])
    state_matches = (
        state == expected_state
        or (state == "absent" and expected_state.startswith("absent"))
    )
    guards = guard_hashes(target)
    inventory_sha256 = scan_root_inventory(scan_root)
    return {
        "activation_boundary": target.spec.activation_boundary,
        "backup_like_count": backup_count,
        "backup_count_matches": backup_count == row["backup_like_count"],
        "capability_id": matrix.capability_id,
        "guard_hashes": guards,
        "lock_present": target.lock_path.exists(),
        "matrix_state": expected_state,
        "resolved_scan_root": str(scan_root),
        "rollout_status": row["rollout_status"],
        "state_matches": state_matches,
        "scan_root_inventory_matches": (
            inventory_sha256 == row["scan_root_inventory_sha256"]
        ),
        "scan_root_inventory_sha256": inventory_sha256,
        "target_state": state,
        "tool": tool,
        "writes_performed": 0,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    for name in ("validate", "inspect-local"):
        action = subparsers.add_parser(name)
        action.add_argument("--registry", required=True)
        action.add_argument("--matrix", required=True)
        if name == "inspect-local":
            action.add_argument(
                "--tools",
                default="codex,claude,kimi,kimi-work,antigravity,grok",
            )
    return parser


def emit(value: object, *, stream: Any = sys.stdout) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), file=stream)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        registry = load_registry(args.registry)
        matrix = load_matrix(args.matrix, registry)
        if args.action == "validate":
            result: object = {
                "capability_id": matrix.capability_id,
                "matrix": str(matrix.path),
                "result": "PASS",
                "target_count": len(matrix.targets),
                "writes_performed": 0,
            }
        else:
            tools = [item.strip() for item in args.tools.split(",") if item.strip()]
            if not tools or any(tool not in matrix.targets for tool in tools):
                raise CapabilityError("inspect-local tools are invalid")
            rows = [inspect_live(matrix, registry, tool) for tool in tools]
            result = {
                "capability_id": matrix.capability_id,
                "result": (
                    "PASS"
                    if all(
                        row["state_matches"]
                        and row["backup_count_matches"]
                        and row["scan_root_inventory_matches"]
                        and not row["lock_present"]
                        for row in rows
                    )
                    else "DRIFT"
                ),
                "targets": rows,
                "writes_performed": 0,
            }
        emit(result)
        return 0
    except (CapabilityError, OSError) as exc:
        emit(
            {
                "error": str(exc),
                "result": "REFUSED",
                "writes_performed": 0,
            },
            stream=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
