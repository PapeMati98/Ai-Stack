#!/usr/bin/env python3
"""Transactionally retire the legacy loose Kimi Desktop agent-loop skill."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CAPABILITY_ID = "agent-loop"
MARKER_NAME = ".agent-loop-managed.json"
EXPECTED_MANAGER = "agent-loop-local-distributor"
EXPECTED_VERSION = "0.1.0"
EXPECTED_DEPLOY_TRANSACTION = "dfaec406872f4f16b58c3349dfda1e74"
EXPECTED_FINGERPRINT = "12755fe9b2da2ac12d5596c9b0b328d40900de2adced56f024a2f50d2289bcfb"
REPARSE_POINT = 0x400
FORBIDDEN_PARTS = {"__pycache__", ".pytest_cache", ".ruff_cache"}


class RetirementError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def is_reparse(path: Path) -> bool:
    stat_result = path.lstat()
    attributes = getattr(stat_result, "st_file_attributes", 0)
    return path.is_symlink() or bool(attributes & REPARSE_POINT)


def assert_plain_chain(path: Path, label: str) -> None:
    current = Path(os.path.abspath(path))
    existing: list[Path] = []
    while True:
        if current.exists():
            existing.append(current)
        if current.parent == current:
            break
        current = current.parent
    for item in reversed(existing):
        if is_reparse(item):
            raise RetirementError(f"{label} crosses a reparse point: {item}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def payload_manifest(root: Path) -> tuple[list[dict[str, Any]], str]:
    if not root.is_dir():
        raise RetirementError(f"Legacy target is not a directory: {root}")
    assert_plain_chain(root, "legacy target")
    rows: list[dict[str, Any]] = []
    for item in sorted(root.rglob("*"), key=lambda value: value.as_posix()):
        relative = item.relative_to(root)
        if any(part in FORBIDDEN_PARTS for part in relative.parts):
            raise RetirementError(f"Forbidden artifact in legacy target: {relative}")
        if is_reparse(item):
            raise RetirementError(f"Legacy target contains a reparse point: {relative}")
        if item.is_dir() or item.name == MARKER_NAME:
            continue
        if not item.is_file():
            raise RetirementError(f"Legacy target contains a non-file: {relative}")
        rows.append(
            {
                "bytes": item.stat().st_size,
                "path": relative.as_posix(),
                "sha256": sha256_file(item),
            }
        )
    encoded = json.dumps(
        rows, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("ascii")
    return rows, hashlib.sha256(encoded).hexdigest()


def read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RetirementError(f"Invalid {label}: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RetirementError(f"Invalid {label} object: {path}")
    return value


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    temporary.write_text(
        json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def roots(appdata: Path, state_root: Path) -> tuple[Path, Path]:
    scan_root = appdata / "kimi-desktop/daimon-share/daimon/skills"
    target = scan_root / CAPABILITY_ID
    absolute_target = Path(os.path.abspath(target))
    if absolute_target.parent != Path(os.path.abspath(scan_root)):
        raise RetirementError("Legacy target is not a direct child of Kimi skills root")
    return absolute_target, Path(os.path.abspath(state_root))


def validate_target(target: Path) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    marker = read_json(target / MARKER_NAME, "legacy marker")
    expected = {
        "capability_id": CAPABILITY_ID,
        "capability_version": EXPECTED_VERSION,
        "managed_by": EXPECTED_MANAGER,
        "payload_fingerprint": EXPECTED_FINGERPRINT,
        "tool": "kimi-desktop",
        "transaction_id": EXPECTED_DEPLOY_TRANSACTION,
    }
    mismatches = {
        key: {"expected": value, "actual": marker.get(key)}
        for key, value in expected.items()
        if marker.get(key) != value
    }
    if mismatches:
        raise RetirementError(f"Legacy marker mismatch: {mismatches}")
    rows, fingerprint = payload_manifest(target)
    if fingerprint != EXPECTED_FINGERPRINT:
        raise RetirementError(
            f"Legacy payload drift: expected {EXPECTED_FINGERPRINT}, got {fingerprint}"
        )
    return marker, rows, fingerprint


def plan(appdata: Path, state_root: Path) -> dict[str, Any]:
    target, state_root = roots(appdata, state_root)
    _, rows, fingerprint = validate_target(target)
    return {
        "action": "plan",
        "status": "READY",
        "target": str(target),
        "state_root": str(state_root),
        "file_count": len(rows),
        "payload_fingerprint": fingerprint,
        "writes_performed": 0,
    }


def retire(appdata: Path, state_root: Path) -> dict[str, Any]:
    target, state_root = roots(appdata, state_root)
    _, rows, fingerprint = validate_target(target)
    transaction_id = uuid.uuid4().hex
    transaction_root = state_root / "transactions" / transaction_id
    backup = transaction_root / "backup" / CAPABILITY_ID
    receipt_path = transaction_root / "receipt.json"
    assert_plain_chain(state_root, "retirement state")
    backup.parent.mkdir(parents=True, exist_ok=False)
    try:
        os.replace(target, backup)
        receipt = {
            "action": "retire-kimi-loose-shadow",
            "status": "RETIRED",
            "transaction_id": transaction_id,
            "created_at": utc_now(),
            "target": str(target),
            "backup": str(backup),
            "payload_fingerprint": fingerprint,
            "file_count": len(rows),
            "previous_state": "managed-legacy-shadow",
            "rollback_state": "available",
            "writes_performed": 1,
        }
        write_json_atomic(receipt_path, receipt)
    except Exception:
        if backup.exists() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(backup, target)
        raise
    return {**receipt, "receipt_path": str(receipt_path)}


def verify_receipt(receipt_path: Path) -> dict[str, Any]:
    receipt = read_json(receipt_path, "retirement receipt")
    target = Path(str(receipt.get("target", "")))
    backup = Path(str(receipt.get("backup", "")))
    fingerprint = str(receipt.get("payload_fingerprint", ""))
    if receipt.get("status") == "RETIRED":
        if target.exists() or not backup.is_dir():
            raise RetirementError("Retired state does not match receipt")
        _, actual = payload_manifest(backup)
        if actual != fingerprint:
            raise RetirementError("Retirement backup drift")
    elif receipt.get("status") == "ROLLED_BACK":
        if not target.is_dir() or backup.exists():
            raise RetirementError("Rolled-back state does not match receipt")
        _, actual = payload_manifest(target)
        if actual != fingerprint:
            raise RetirementError("Restored legacy payload drift")
    else:
        raise RetirementError(f"Unsupported receipt status: {receipt.get('status')}")
    return {
        "action": "verify",
        "status": "PASS",
        "retirement_status": receipt["status"],
        "transaction_id": receipt["transaction_id"],
        "writes_performed": 0,
    }


def rollback(receipt_path: Path) -> dict[str, Any]:
    receipt = read_json(receipt_path, "retirement receipt")
    if receipt.get("status") != "RETIRED":
        raise RetirementError("Only a RETIRED transaction can be rolled back")
    target = Path(str(receipt["target"]))
    backup = Path(str(receipt["backup"]))
    if target.exists():
        raise RetirementError(f"Rollback target is no longer absent: {target}")
    if not backup.is_dir():
        raise RetirementError(f"Rollback backup is missing: {backup}")
    _, actual = payload_manifest(backup)
    if actual != receipt.get("payload_fingerprint"):
        raise RetirementError("Rollback backup drift")
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(backup, target)
    receipt["status"] = "ROLLED_BACK"
    receipt["rolled_back_at"] = utc_now()
    receipt["rollback_state"] = "consumed"
    receipt["writes_performed"] = int(receipt.get("writes_performed", 0)) + 1
    write_json_atomic(receipt_path, receipt)
    return {
        "action": "rollback",
        "status": "ROLLED_BACK",
        "transaction_id": receipt["transaction_id"],
        "target": str(target),
        "writes_performed": 1,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--appdata", type=Path, default=Path(os.environ.get("APPDATA", ""))
    )
    parser.add_argument(
        "--state-root",
        type=Path,
        default=Path.home() / ".agents/skill-retirements/agent-loop",
    )
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("plan")
    subparsers.add_parser("retire")
    for action in ("verify", "rollback"):
        item = subparsers.add_parser(action)
        item.add_argument("--receipt", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.action == "plan":
            result = plan(args.appdata, args.state_root)
        elif args.action == "retire":
            result = retire(args.appdata, args.state_root)
        elif args.action == "verify":
            result = verify_receipt(args.receipt)
        else:
            result = rollback(args.receipt)
        print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
        return 0
    except RetirementError as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
