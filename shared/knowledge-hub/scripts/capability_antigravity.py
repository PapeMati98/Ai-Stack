#!/usr/bin/env python3
"""Pinned design-ui release planning and gated Antigravity first install."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from capability_core import (
    CAPABILITY_MARKER_NAME,
    GIT_OID,
    HEX_64,
    SECURITY_STATEMENT,
    CapabilityError,
    GitSource,
    absolute_plain_path,
    inspect_git_source,
    marker_v2,
    parse_marker_data,
    parse_iso_timestamp,
    read_marker,
    scan_directory,
    sha256_bytes,
    strict_json_loads,
    utc_now,
)


CAPABILITY_ID = "design-ui"
CAPABILITY_VERSION = "0.1.0"
TOOL = "antigravity"
SCOPE = "user"
SOURCE_PATH = f"capabilities/{CAPABILITY_ID}"
ACTIVATION_BOUNDARY = "new Antigravity task/session; no forced restart"
APPROVAL_KIND = "capability-v1.1b-antigravity-pilot"
FIXTURE_PREFIX = "capability-antigravity-fixture-"
APPROVAL_ID = re.compile(r"^[a-z0-9][a-z0-9-]{7,127}$")
TRANSACTION_ID = re.compile(r"^[0-9a-f]{32}$")
ALLOWED_ACTIONS = {
    "install",
    "recover-install",
    "rollback",
    "recover-rollback",
}
APPROVAL_KEYS = {
    "schema_version",
    "approval_kind",
    "approval_id",
    "approved_by",
    "approved_at",
    "expires_at",
    "owner_decision",
    "authorized_actions",
    "capability_id",
    "capability_version",
    "tool",
    "scope",
    "source_commit",
    "source_tree",
    "payload_fingerprint",
    "manifest_fingerprint",
    "target",
    "state_root",
    "config_path",
    "config_sha256",
    "rules_path",
    "rules_sha256",
    "approval_posture_sha256",
    "target_preexisting",
    "activation_boundary",
    "writes_scope",
}


class InjectedCrash(RuntimeError):
    """A fixture-only process-loss checkpoint used by the test suite."""


@dataclass(frozen=True)
class AntigravityPaths:
    profile: Path
    scan_root: Path
    target: Path
    state_root: Path
    config: Path
    rules: Path
    fixture_root: Path | None = None

    @property
    def operations_root(self) -> Path:
        return self.state_root / "operations"

    @property
    def quarantine_root(self) -> Path:
        return self.state_root / "quarantine"

    @property
    def lock_path(self) -> Path:
        return self.state_root / "target.lock.json"

    @property
    def active_path(self) -> Path:
        return self.state_root / "active-install.json"


@dataclass(frozen=True)
class ApprovalReceipt:
    data: dict[str, Any]
    raw: bytes
    sha256: str
    path: Path

    @property
    def approval_id(self) -> str:
        return str(self.data["approval_id"])


def canonical_json_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2).encode(
        "ascii"
    ) + b"\n"


def fsync_parent(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_bytes(path: Path, value: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        with temporary.open("xb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_parent(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    atomic_write_bytes(path, canonical_json_bytes(value))


def read_json(path: Path) -> dict[str, Any]:
    try:
        plain = absolute_plain_path(path, must_exist=True)
        if not stat.S_ISREG(plain.lstat().st_mode):
            raise CapabilityError(f"Transaction JSON is not a plain file: {plain}")
        raw = plain.read_bytes()
        value = strict_json_loads(raw.decode("utf-8", errors="strict"))
    except (OSError, UnicodeDecodeError, CapabilityError) as exc:
        raise CapabilityError(f"Invalid Antigravity transaction JSON: {path}") from exc
    if not isinstance(value, dict):
        raise CapabilityError(f"Antigravity transaction JSON must be an object: {path}")
    return value


def file_sha256(path: Path) -> str:
    plain = absolute_plain_path(path, must_exist=True)
    details = plain.lstat()
    if not stat.S_ISREG(details.st_mode):
        raise CapabilityError(f"Expected a plain file: {plain}")
    digest = hashlib.sha256()
    with plain.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def same_path(left: Path | str, right: Path | str) -> bool:
    return os.path.normcase(os.path.abspath(os.fspath(left))) == os.path.normcase(
        os.path.abspath(os.fspath(right))
    )


def assert_below(path: Path, root: Path, label: str) -> None:
    candidate = Path(os.path.abspath(path))
    approved = Path(os.path.abspath(root))
    try:
        candidate.relative_to(approved)
    except ValueError as exc:
        raise CapabilityError(f"{label} escapes its approved root: {candidate}") from exc
    if candidate == approved:
        raise CapabilityError(f"{label} must be below, not equal to, {approved}")


def assert_fixture_root(value: Path | str) -> Path:
    candidate = absolute_plain_path(value, must_exist=True)
    temp_root = absolute_plain_path(tempfile.gettempdir(), must_exist=True)
    assert_below(candidate, temp_root, "fixture root")
    if candidate.parent != temp_root or not candidate.name.startswith(FIXTURE_PREFIX):
        raise CapabilityError(
            f"Fixture root must be a unique {FIXTURE_PREFIX}* child of system TEMP"
        )
    return candidate


def resolve_paths(fixture_root: Path | str | None = None) -> AntigravityPaths:
    if fixture_root is None:
        if os.name != "nt":
            raise CapabilityError("Real Antigravity profile mode is supported only on Windows")
        profile = Path.home()
        fixture = None
    else:
        fixture = assert_fixture_root(fixture_root)
        profile = fixture / "profile"
    return AntigravityPaths(
        profile=profile,
        scan_root=profile / ".gemini/config/skills",
        target=profile / f".gemini/config/skills/{CAPABILITY_ID}",
        state_root=profile / f".agents/capability-state/{TOOL}/{CAPABILITY_ID}",
        config=profile / ".gemini/config/config.json",
        rules=profile / ".gemini/GEMINI.md",
        fixture_root=fixture,
    )


def nearest_existing(path: Path) -> Path:
    cursor = path
    while not cursor.exists():
        if cursor.parent == cursor:
            raise CapabilityError(f"No existing ancestor for path: {path}")
        cursor = cursor.parent
    return cursor


def same_volume(left: Path, right: Path) -> bool:
    if os.name == "nt":
        left_drive = os.path.splitdrive(os.path.abspath(left))[0].casefold()
        right_drive = os.path.splitdrive(os.path.abspath(right))[0].casefold()
        return bool(left_drive) and left_drive == right_drive
    return nearest_existing(left).stat().st_dev == nearest_existing(right).stat().st_dev


def validate_paths(paths: AntigravityPaths) -> None:
    scan_root = absolute_plain_path(paths.scan_root, must_exist=True)
    if not scan_root.is_dir():
        raise CapabilityError(f"Antigravity scan root is not a directory: {scan_root}")
    if not same_path(paths.target.parent, scan_root):
        raise CapabilityError("Antigravity target must be a direct scan-root child")
    absolute_plain_path(paths.target, must_exist=paths.target.exists())
    absolute_plain_path(paths.state_root, must_exist=paths.state_root.exists())
    file_sha256(paths.config)
    file_sha256(paths.rules)
    if not same_volume(paths.target, paths.state_root):
        raise CapabilityError("Target and transaction state are not on the same volume")
    try:
        Path(os.path.abspath(paths.state_root)).relative_to(
            Path(os.path.abspath(paths.scan_root))
        )
    except ValueError:
        pass
    else:
        raise CapabilityError("Transaction state must remain outside the skill scan root")
    if paths.fixture_root is not None:
        for candidate, label in (
            (paths.scan_root, "fixture scan root"),
            (paths.target, "fixture target"),
            (paths.state_root, "fixture state root"),
            (paths.config, "fixture config"),
            (paths.rules, "fixture rules"),
        ):
            assert_below(candidate, paths.fixture_root, label)


def backup_like_directories(scan_root: Path) -> list[str]:
    root = absolute_plain_path(scan_root, must_exist=True)
    found: list[str] = []
    with os.scandir(root) as iterator:
        entries = sorted(iterator, key=lambda item: item.name.casefold())
    for entry in entries:
        if ".backup-" not in entry.name.casefold():
            continue
        details = entry.stat(follow_symlinks=False)
        if entry.is_symlink() or not stat.S_ISDIR(details.st_mode):
            raise CapabilityError(f"Backup-like scan-root entry is not a plain directory: {entry.name}")
        found.append(entry.name)
    return found


def load_release(repo: Path | str, commit: str) -> GitSource:
    if not GIT_OID.fullmatch(commit):
        raise CapabilityError("Release commit must be a full Git object ID, not a ref")
    source = inspect_git_source(repo, commit, SOURCE_PATH, pilot_policy=True)
    if source.source_commit != commit:
        raise CapabilityError("Resolved release commit differs from the supplied full OID")
    manifest = source.capability_manifest or {}
    if manifest.get("capability_id") != CAPABILITY_ID:
        raise CapabilityError("Release capability_id does not match the Antigravity pilot")
    if manifest.get("capability_version") != CAPABILITY_VERSION:
        raise CapabilityError("Release capability_version is not the approved 0.1.0")
    if manifest.get("revoked_at") is not None:
        raise CapabilityError("Refusing a revoked capability release")
    if source.worktree_contamination:
        raise CapabilityError("Release path has worktree contamination")
    return source


def validate_expected_hash(value: str, label: str) -> None:
    if not HEX_64.fullmatch(value):
        raise CapabilityError(f"{label} must be a lowercase SHA-256")


def release_context(
    *,
    repo: Path | str,
    commit: str,
    paths: AntigravityPaths,
    expected_config_sha256: str,
    expected_rules_sha256: str,
    approval_posture_sha256: str,
    require_current_hashes: bool,
) -> tuple[GitSource, dict[str, Any]]:
    validate_expected_hash(expected_config_sha256, "expected config hash")
    validate_expected_hash(expected_rules_sha256, "expected rules hash")
    validate_expected_hash(approval_posture_sha256, "approval posture hash")
    validate_paths(paths)
    source = load_release(repo, commit)
    config_hash = file_sha256(paths.config)
    rules_hash = file_sha256(paths.rules)
    if require_current_hashes and config_hash != expected_config_sha256:
        raise CapabilityError("Antigravity config changed since owner review")
    if require_current_hashes and rules_hash != expected_rules_sha256:
        raise CapabilityError("Antigravity global rules changed since owner review")
    context = {
        "capability_id": CAPABILITY_ID,
        "capability_version": CAPABILITY_VERSION,
        "tool": TOOL,
        "scope": SCOPE,
        "source_commit": source.source_commit,
        "source_tree": source.source_tree,
        "payload_fingerprint": source.payload_fingerprint,
        "manifest_fingerprint": source.manifest_fingerprint,
        "target": str(Path(os.path.abspath(paths.target))),
        "state_root": str(Path(os.path.abspath(paths.state_root))),
        "config_path": str(Path(os.path.abspath(paths.config))),
        "config_sha256": expected_config_sha256,
        "rules_path": str(Path(os.path.abspath(paths.rules))),
        "rules_sha256": expected_rules_sha256,
        "approval_posture_sha256": approval_posture_sha256,
        "target_preexisting": False,
        "activation_boundary": ACTIVATION_BOUNDARY,
        "writes_scope": [
            str(Path(os.path.abspath(paths.target))),
            str(Path(os.path.abspath(paths.state_root))),
        ],
        "observed_config_sha256": config_hash,
        "observed_rules_sha256": rules_hash,
    }
    return source, context


def parse_timestamp(value: str, field: str) -> dt.datetime:
    parse_iso_timestamp(value, field)
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_approval(
    path: Path | str,
    *,
    context: dict[str, Any],
    required_action: str,
    paths: AntigravityPaths,
    allow_expired: bool = False,
) -> ApprovalReceipt:
    if required_action not in ALLOWED_ACTIONS:
        raise CapabilityError(f"Unsupported approval action: {required_action}")
    approval_path = absolute_plain_path(path, must_exist=True)
    if not approval_path.is_file():
        raise CapabilityError(f"Approval receipt is not a plain file: {approval_path}")
    for forbidden_root in (paths.scan_root, paths.state_root):
        try:
            approval_path.relative_to(Path(os.path.abspath(forbidden_root)))
        except ValueError:
            continue
        raise CapabilityError("Approval receipt must remain outside scan and state roots")
    raw = approval_path.read_bytes()
    try:
        data = strict_json_loads(raw.decode("ascii", errors="strict"))
    except (UnicodeDecodeError, CapabilityError) as exc:
        raise CapabilityError("Approval receipt must be canonical ASCII JSON") from exc
    if not isinstance(data, dict):
        raise CapabilityError("Approval receipt must contain a JSON object")
    unknown = sorted(set(data) - APPROVAL_KEYS)
    missing = sorted(APPROVAL_KEYS - set(data))
    if unknown or missing:
        raise CapabilityError(
            f"Approval receipt keys mismatch; missing={missing}, unknown={unknown}"
        )
    if raw != canonical_json_bytes(data):
        raise CapabilityError("Approval receipt is not in canonical JSON form")
    if data.get("schema_version") != 1 or data.get("approval_kind") != APPROVAL_KIND:
        raise CapabilityError("Approval receipt kind or schema is invalid")
    approval_id = data.get("approval_id")
    if not isinstance(approval_id, str) or not APPROVAL_ID.fullmatch(approval_id):
        raise CapabilityError("Approval receipt has an invalid approval_id")
    if data.get("approved_by") != "Mateusz":
        raise CapabilityError("V1.1B approval must be attributed to Mateusz")
    decision = data.get("owner_decision")
    if not isinstance(decision, str) or not decision.startswith("GO V1.1B:"):
        raise CapabilityError("Approval receipt does not record an explicit V1.1B GO")
    actions = data.get("authorized_actions")
    if (
        not isinstance(actions, list)
        or not actions
        or any(not isinstance(item, str) or item not in ALLOWED_ACTIONS for item in actions)
        or actions != sorted(set(actions))
        or required_action not in actions
    ):
        raise CapabilityError(f"Approval receipt does not authorize {required_action}")
    approved_at = parse_timestamp(str(data.get("approved_at", "")), "approved_at")
    expires_at = parse_timestamp(str(data.get("expires_at", "")), "expires_at")
    now = dt.datetime.now(dt.timezone.utc)
    if (
        expires_at <= approved_at
        or expires_at - approved_at > dt.timedelta(hours=24)
        or approved_at > now + dt.timedelta(minutes=5)
        or (now > expires_at and not allow_expired)
    ):
        raise CapabilityError("Approval receipt is expired or has an invalid time window")
    for key, expected in context.items():
        if key.startswith("observed_"):
            continue
        actual = data.get(key)
        if key.endswith("_path") or key in {"target", "state_root"}:
            if not isinstance(actual, str) or not same_path(actual, str(expected)):
                raise CapabilityError(f"Approval receipt path mismatch for {key}")
        elif key == "writes_scope":
            if not isinstance(actual, list) or len(actual) != len(expected):
                raise CapabilityError("Approval receipt writes_scope mismatch")
            if any(not same_path(left, right) for left, right in zip(actual, expected)):
                raise CapabilityError("Approval receipt writes_scope mismatch")
        elif actual != expected:
            raise CapabilityError(f"Approval receipt mismatch for {key}")
    return ApprovalReceipt(
        data=dict(data), raw=raw, sha256=sha256_bytes(raw), path=approval_path
    )


def materialize_release(source: GitSource, target: Path, marker: dict[str, Any]) -> None:
    if target.exists():
        raise CapabilityError(f"Staging path already exists: {target}")
    target.mkdir(parents=False)
    try:
        for entry in source.entries:
            destination = target.joinpath(*PurePosixPath(entry.relative_path).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as handle:
                handle.write(entry.content)
                handle.flush()
                os.fsync(handle.fileno())
        marker_path = target / CAPABILITY_MARKER_NAME
        with marker_path.open("xb") as handle:
            handle.write(canonical_json_bytes(marker))
            handle.flush()
            os.fsync(handle.fileno())
        scanned = scan_directory(target, exclude_marker=True, reject_hardlinks=True)
        if scanned["payload_fingerprint"] != source.payload_fingerprint:
            raise CapabilityError("Staged release fingerprint mismatch")
        if read_marker(marker_path).data != marker:
            raise CapabilityError("Staged release marker mismatch")
    except Exception:
        # Retain partial staging for forensic recovery; never guess at cleanup.
        raise


class AntigravityFirstInstall:
    """Target-specific first-install transaction with external retained state."""

    def __init__(
        self,
        *,
        repo: Path | str,
        commit: str,
        paths: AntigravityPaths,
        expected_config_sha256: str,
        expected_rules_sha256: str,
        approval_posture_sha256: str,
    ) -> None:
        self.repo = Path(repo)
        self.commit = commit
        self.paths = paths
        self.expected_config_sha256 = expected_config_sha256
        self.expected_rules_sha256 = expected_rules_sha256
        self.approval_posture_sha256 = approval_posture_sha256

    def _context(self, *, require_current_hashes: bool) -> tuple[GitSource, dict[str, Any]]:
        return release_context(
            repo=self.repo,
            commit=self.commit,
            paths=self.paths,
            expected_config_sha256=self.expected_config_sha256,
            expected_rules_sha256=self.expected_rules_sha256,
            approval_posture_sha256=self.approval_posture_sha256,
            require_current_hashes=require_current_hashes,
        )

    def plan(self) -> dict[str, Any]:
        source, context = self._context(require_current_hashes=True)
        if self.paths.target.exists():
            raise CapabilityError("First-install target must be absent")
        backups = backup_like_directories(self.paths.scan_root)
        if backups:
            raise CapabilityError(
                f"Backup-like directories remain in Antigravity scan root: {backups}"
            )
        if self.paths.lock_path.exists():
            raise CapabilityError("Per-target lock exists; automatic stale-lock removal is forbidden")
        return {
            "action": "plan",
            "status": "READY_FOR_V1.1B_APPROVAL",
            "strategy": "same-volume-first-install-journal",
            "writes_performed": 0,
            "real_profile": self.paths.fixture_root is None,
            "release": {
                "source_commit": source.source_commit,
                "source_tree": source.source_tree,
                "payload_fingerprint": source.payload_fingerprint,
                "manifest_fingerprint": source.manifest_fingerprint,
                "file_count": len(source.entries),
                "files": [entry.report() for entry in source.entries],
            },
            "target": context["target"],
            "target_preexisting": False,
            "scan_root": str(Path(os.path.abspath(self.paths.scan_root))),
            "backup_like_directories": backups,
            "state_root": context["state_root"],
            "state_root_preexisting": self.paths.state_root.exists(),
            "quarantine_root": str(Path(os.path.abspath(self.paths.quarantine_root))),
            "config_path": context["config_path"],
            "config_sha256": context["observed_config_sha256"],
            "rules_path": context["rules_path"],
            "rules_sha256": context["observed_rules_sha256"],
            "approval_posture_sha256": context["approval_posture_sha256"],
            "activation_boundary": ACTIVATION_BOUNDARY,
            "rules_or_config_mutation": False,
            "approval_required": APPROVAL_KIND,
            "security_statement": SECURITY_STATEMENT,
        }

    def _create_state_roots(self) -> None:
        self.paths.state_root.mkdir(parents=True, exist_ok=True)
        absolute_plain_path(self.paths.state_root, must_exist=True)
        self.paths.operations_root.mkdir(exist_ok=True)
        self.paths.quarantine_root.mkdir(exist_ok=True)
        absolute_plain_path(self.paths.operations_root, must_exist=True)
        absolute_plain_path(self.paths.quarantine_root, must_exist=True)

    def _operation_dir(self, transaction_id: str) -> Path:
        if not TRANSACTION_ID.fullmatch(transaction_id):
            raise CapabilityError("Invalid transaction ID")
        operation = self.paths.operations_root / transaction_id
        assert_below(operation, self.paths.operations_root, "operation path")
        if operation.exists():
            absolute_plain_path(operation, must_exist=True)
        return operation

    def _acquire(self, transaction_id: str, operation: str) -> None:
        data = {
            "schema_version": 1,
            "transaction_id": transaction_id,
            "operation": operation,
            "pid": os.getpid(),
            "created_at": utc_now(),
        }
        try:
            descriptor = os.open(
                self.paths.lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
            )
        except FileExistsError as exc:
            raise CapabilityError(
                "Per-target lock already exists; automatic stale-lock removal is forbidden"
            ) from exc
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical_json_bytes(data))
            handle.flush()
            os.fsync(handle.fileno())

    def _release(self, transaction_id: str) -> None:
        lock = read_json(self.paths.lock_path)
        if lock.get("transaction_id") != transaction_id:
            raise CapabilityError("Refusing to release a lock owned by another transaction")
        self.paths.lock_path.unlink()
        fsync_parent(self.paths.lock_path)

    def _checkpoint(
        self,
        journal_path: Path,
        journal: dict[str, Any],
        state: str,
        crash_after: str | None,
    ) -> None:
        journal["state"] = state
        journal["updated_at"] = utc_now()
        atomic_write_json(journal_path, journal)
        if crash_after == state:
            raise InjectedCrash(state)

    def _assert_current_hashes(self) -> None:
        if file_sha256(self.paths.config) != self.expected_config_sha256:
            raise CapabilityError("Antigravity config changed before activation")
        if file_sha256(self.paths.rules) != self.expected_rules_sha256:
            raise CapabilityError("Antigravity global rules changed before activation")

    def _verify_active(self, source: GitSource, expected_marker: dict[str, Any]) -> None:
        if not self.paths.target.is_dir():
            raise CapabilityError("Activated Antigravity target is missing")
        scanned = scan_directory(
            self.paths.target, exclude_marker=True, reject_hardlinks=True
        )
        if scanned["payload_fingerprint"] != source.payload_fingerprint:
            raise CapabilityError("Activated Antigravity payload fingerprint mismatch")
        marker = read_marker(self.paths.target / CAPABILITY_MARKER_NAME)
        if marker.data != expected_marker:
            raise CapabilityError("Activated Antigravity marker mismatch")

    def _approval_used_for_install(self, approval_id: str) -> bool:
        if not self.paths.operations_root.exists():
            return False
        root = absolute_plain_path(self.paths.operations_root, must_exist=True)
        with os.scandir(root) as iterator:
            entries = sorted(iterator, key=lambda item: item.name)
        for entry in entries:
            details = entry.stat(follow_symlinks=False)
            if entry.is_symlink() or not stat.S_ISDIR(details.st_mode):
                raise CapabilityError(f"Unexpected operations entry: {entry.name}")
            absolute_plain_path(Path(entry.path), must_exist=True)
            journal_path = Path(entry.path) / "journal.json"
            if not journal_path.exists():
                raise CapabilityError(f"Operation is missing its journal: {entry.name}")
            journal = read_json(journal_path)
            if journal.get("operation") == "install" and journal.get("approval_id") == approval_id:
                return True
        return False

    def _approval(
        self,
        approval_path: Path | str,
        *,
        context: dict[str, Any],
        action: str,
        owner_authorized: bool,
    ) -> ApprovalReceipt:
        if not owner_authorized:
            raise CapabilityError("Operation requires explicit owner authorization")
        return load_approval(
            approval_path,
            context=context,
            required_action=action,
            paths=self.paths,
            allow_expired=action.startswith("recover-"),
        )

    def _validate_marker_for_source(
        self, marker: Any, source: GitSource
    ) -> dict[str, Any]:
        parsed = parse_marker_data(marker)
        expected = {
            "schema_version": 2,
            "managed_by": "shared-agent-knowledge-hub",
            "capability_id": CAPABILITY_ID,
            "capability_version": CAPABILITY_VERSION,
            "tool": TOOL,
            "scope": SCOPE,
            "source_commit": source.source_commit,
            "source_tree": source.source_tree,
            "payload_fingerprint": source.payload_fingerprint,
            "manifest_fingerprint": source.manifest_fingerprint,
            "activation_boundary": ACTIVATION_BOUNDARY,
        }
        for key, value in expected.items():
            if parsed.data.get(key) != value:
                raise CapabilityError(f"Transaction marker mismatch for {key}")
        return parsed.data

    def _read_active(self) -> dict[str, Any]:
        active = read_json(self.paths.active_path)
        transaction_id = str(active.get("install_transaction_id", ""))
        if not TRANSACTION_ID.fullmatch(transaction_id):
            raise CapabilityError("Active-install record has an invalid transaction ID")
        expected = {
            "schema_version": 1,
            "capability_id": CAPABILITY_ID,
            "capability_version": CAPABILITY_VERSION,
            "tool": TOOL,
            "target": str(Path(os.path.abspath(self.paths.target))),
            "source_commit": self.commit,
        }
        for key, value in expected.items():
            actual = active.get(key)
            if key == "target":
                if not isinstance(actual, str) or not same_path(actual, value):
                    raise CapabilityError("Active-install target path mismatch")
            elif actual != value:
                raise CapabilityError(f"Active-install mismatch for {key}")
        return active

    def _clear_active(self, install_transaction_id: str) -> None:
        if not self.paths.active_path.exists():
            return
        active = self._read_active()
        if active.get("install_transaction_id") != install_transaction_id:
            raise CapabilityError("Refusing to clear another active install record")
        self.paths.active_path.unlink()
        fsync_parent(self.paths.active_path)

    def _validate_install_journal(
        self,
        journal: dict[str, Any],
        *,
        transaction_id: str,
        operation: Path,
        source: GitSource,
        approval: ApprovalReceipt,
        allow_missing_receipt: bool = False,
    ) -> dict[str, Any]:
        expected_paths = {
            "target": self.paths.target,
            "state_root": self.paths.state_root,
            "operation_dir": operation,
            "staged": operation / "staged",
            "quarantine": self.paths.quarantine_root
            / f"install-{transaction_id}-new-target",
        }
        if journal.get("schema_version") != 1 or journal.get("operation") != "install":
            raise CapabilityError("Install journal schema or operation is invalid")
        if journal.get("transaction_id") != transaction_id:
            raise CapabilityError("Install journal transaction ID mismatch")
        for key, expected in expected_paths.items():
            actual = journal.get(key)
            if not isinstance(actual, str) or not same_path(actual, expected):
                raise CapabilityError(f"Install journal path mismatch for {key}")
        expected_values = {
            "target_preexisting": False,
            "source_commit": source.source_commit,
            "source_tree": source.source_tree,
            "payload_fingerprint": source.payload_fingerprint,
            "manifest_fingerprint": source.manifest_fingerprint,
            "approval_id": approval.approval_id,
            "approval_sha256": approval.sha256,
            "config_sha256": self.expected_config_sha256,
            "rules_sha256": self.expected_rules_sha256,
            "approval_posture_sha256": self.approval_posture_sha256,
        }
        for key, value in expected_values.items():
            if journal.get(key) != value:
                raise CapabilityError(f"Install journal mismatch for {key}")
        marker = self._validate_marker_for_source(journal.get("marker"), source)
        stored_receipt = operation / "approval-receipt.json"
        if not stored_receipt.exists() and allow_missing_receipt:
            return marker
        if not stored_receipt.is_file() or stored_receipt.read_bytes() != approval.raw:
            raise CapabilityError("Stored install approval receipt does not match")
        return marker

    def install(
        self,
        approval_path: Path | str,
        *,
        owner_authorized: bool,
        crash_after: str | None = None,
        fail_before: str | None = None,
        after_prepare: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        source, context = self._context(require_current_hashes=True)
        if self.paths.target.exists():
            raise CapabilityError("First-install target must be absent")
        backups = backup_like_directories(self.paths.scan_root)
        if backups:
            raise CapabilityError("Backup-like directories block Antigravity install")
        if self.paths.lock_path.exists():
            raise CapabilityError("Per-target lock already exists")
        approval = self._approval(
            approval_path,
            context=context,
            action="install",
            owner_authorized=owner_authorized,
        )
        if self._approval_used_for_install(approval.approval_id):
            raise CapabilityError("Approval ID was already consumed by an install")

        self._create_state_roots()
        transaction_id = uuid.uuid4().hex
        self._acquire(transaction_id, "install")
        operation = self._operation_dir(transaction_id)
        journal_path = operation / "journal.json"
        try:
            if self._approval_used_for_install(approval.approval_id):
                raise CapabilityError("Approval ID was already consumed by an install")
            if self.paths.target.exists():
                raise CapabilityError("First-install target appeared before journaling")
            operation.mkdir()
            marker = marker_v2(
                capability_id=CAPABILITY_ID,
                capability_version=CAPABILITY_VERSION,
                tool=TOOL,
                scope=SCOPE,
                source=source,
                activation_boundary=ACTIVATION_BOUNDARY,
            )
            journal: dict[str, Any] = {
                "schema_version": 1,
                "operation": "install",
                "transaction_id": transaction_id,
                "target": str(Path(os.path.abspath(self.paths.target))),
                "state_root": str(Path(os.path.abspath(self.paths.state_root))),
                "operation_dir": str(Path(os.path.abspath(operation))),
                "staged": str(Path(os.path.abspath(operation / "staged"))),
                "quarantine": str(
                    Path(
                        os.path.abspath(
                            self.paths.quarantine_root
                            / f"install-{transaction_id}-new-target"
                        )
                    )
                ),
                "target_preexisting": False,
                "source_commit": source.source_commit,
                "source_tree": source.source_tree,
                "payload_fingerprint": source.payload_fingerprint,
                "manifest_fingerprint": source.manifest_fingerprint,
                "config_sha256": self.expected_config_sha256,
                "rules_sha256": self.expected_rules_sha256,
                "approval_posture_sha256": self.approval_posture_sha256,
                "approval_id": approval.approval_id,
                "approval_sha256": approval.sha256,
                "marker": marker,
                "created_at": utc_now(),
            }
            if fail_before == "journal":
                raise OSError("simulated journal write failure")
            self._checkpoint(journal_path, journal, "preparing", crash_after)
            atomic_write_bytes(operation / "approval-receipt.json", approval.raw)
            self._checkpoint(journal_path, journal, "approval_recorded", crash_after)
            if fail_before == "staging":
                raise OSError("simulated staging write failure")
            materialize_release(source, operation / "staged", marker)
            self._checkpoint(journal_path, journal, "staged", crash_after)
            atomic_write_json(
                operation / "prior-state.json",
                {
                    "schema_version": 1,
                    "transaction_id": transaction_id,
                    "target": str(Path(os.path.abspath(self.paths.target))),
                    "target_preexisting": False,
                    "verified_at": utc_now(),
                },
            )
            self._checkpoint(journal_path, journal, "prior_state_verified", crash_after)
            if after_prepare:
                after_prepare()
            if self.paths.target.exists():
                raise CapabilityError("First-install target appeared after preparation")
            self._assert_current_hashes()
            if backup_like_directories(self.paths.scan_root):
                raise CapabilityError("Backup-like directory appeared before activation")
            staged = scan_directory(
                operation / "staged", exclude_marker=True, reject_hardlinks=True
            )
            if staged["payload_fingerprint"] != source.payload_fingerprint:
                raise CapabilityError("Staged payload changed before activation")
            if read_marker(operation / "staged" / CAPABILITY_MARKER_NAME).data != marker:
                raise CapabilityError("Staged marker changed before activation")
            fresh_approval = self._approval(
                approval_path,
                context=context,
                action="install",
                owner_authorized=owner_authorized,
            )
            if fresh_approval.sha256 != approval.sha256:
                raise CapabilityError("Approval receipt changed before activation")
            self._checkpoint(journal_path, journal, "prepared", crash_after)
            if fail_before == "activate":
                raise OSError("simulated Antigravity target lock or permission denial")
            os.replace(operation / "staged", self.paths.target)
            fsync_parent(self.paths.target)
            if crash_after == "target_renamed":
                raise InjectedCrash("target_renamed")
            self._verify_active(source, marker)
            self._checkpoint(journal_path, journal, "target_activated", crash_after)
            active = {
                "schema_version": 1,
                "capability_id": CAPABILITY_ID,
                "capability_version": CAPABILITY_VERSION,
                "tool": TOOL,
                "target": str(Path(os.path.abspath(self.paths.target))),
                "source_commit": source.source_commit,
                "source_tree": source.source_tree,
                "payload_fingerprint": source.payload_fingerprint,
                "manifest_fingerprint": source.manifest_fingerprint,
                "install_transaction_id": transaction_id,
                "approval_id": approval.approval_id,
                "approval_sha256": approval.sha256,
                "activated_at": utc_now(),
            }
            atomic_write_json(self.paths.active_path, active)
            self._checkpoint(journal_path, journal, "active_recorded", crash_after)
            self._checkpoint(journal_path, journal, "committed", crash_after)
            self._release(transaction_id)
            return {
                "action": "install",
                "status": "COMMITTED",
                "transaction_id": transaction_id,
                "target": str(self.paths.target),
                "target_preexisting": False,
                "post_activate_fingerprint": source.payload_fingerprint,
                "marker_file": CAPABILITY_MARKER_NAME,
                "activation_boundary": ACTIVATION_BOUNDARY,
                "writes_scope": context["writes_scope"],
                "rules_or_config_mutation": False,
            }
        except Exception:
            if not journal_path.exists() and self.paths.lock_path.exists():
                self._release(transaction_id)
            raise

    def recover_install(
        self,
        approval_path: Path | str,
        *,
        owner_authorized: bool,
        crash_after: str | None = None,
    ) -> dict[str, Any]:
        source, context = self._context(require_current_hashes=False)
        if not self.paths.lock_path.is_file():
            raise CapabilityError("No interrupted Antigravity operation lock exists")
        lock = read_json(self.paths.lock_path)
        transaction_id = str(lock.get("transaction_id", ""))
        if lock.get("operation") != "install" or not TRANSACTION_ID.fullmatch(
            transaction_id
        ):
            raise CapabilityError("Active lock is not an install transaction")
        approval = self._approval(
            approval_path,
            context=context,
            action="recover-install",
            owner_authorized=owner_authorized,
        )
        operation = self._operation_dir(transaction_id)
        journal_path = operation / "journal.json"
        journal = read_json(journal_path)
        initial_state = str(journal.get("state", ""))
        marker = self._validate_install_journal(
            journal,
            transaction_id=transaction_id,
            operation=operation,
            source=source,
            approval=approval,
            allow_missing_receipt=initial_state == "preparing",
        )
        stored_receipt = operation / "approval-receipt.json"
        if not stored_receipt.exists():
            atomic_write_bytes(stored_receipt, approval.raw)
            self._checkpoint(journal_path, journal, "approval_recorded", crash_after)
        state = str(journal.get("state", ""))
        if state == "committed":
            self._verify_active(source, marker)
            active = self._read_active()
            if active.get("install_transaction_id") != transaction_id:
                raise CapabilityError("Committed install active record mismatch")
            self._release(transaction_id)
            return {
                "action": "recover-install",
                "status": "COMMITTED",
                "transaction_id": transaction_id,
                "target_present": True,
            }

        staged = operation / "staged"
        quarantine = self.paths.quarantine_root / f"install-{transaction_id}-new-target"
        if self.paths.target.exists():
            if staged.exists() or quarantine.exists():
                raise CapabilityError("Install recovery found an ambiguous target state")
            self._verify_active(source, marker)
            os.replace(self.paths.target, quarantine)
            fsync_parent(quarantine)
            if crash_after == "recovery_target_renamed":
                raise InjectedCrash("recovery_target_renamed")
            self._checkpoint(
                journal_path, journal, "rollback_new_quarantined", crash_after
            )
        elif quarantine.exists():
            scanned = scan_directory(
                quarantine, exclude_marker=True, reject_hardlinks=True
            )
            if scanned["payload_fingerprint"] != source.payload_fingerprint:
                raise CapabilityError("Install recovery quarantine changed")
            if read_marker(quarantine / CAPABILITY_MARKER_NAME).data != marker:
                raise CapabilityError("Install recovery quarantine marker changed")
        elif staged.exists():
            scanned = scan_directory(staged, exclude_marker=True, reject_hardlinks=True)
            if scanned["payload_fingerprint"] != source.payload_fingerprint:
                raise CapabilityError("Interrupted staging changed")
        elif state not in {"preparing", "approval_recorded"}:
            raise CapabilityError("Activated target, staging and quarantine are all missing")

        if self.paths.target.exists():
            raise CapabilityError("Install recovery did not restore target absence")
        self._clear_active(transaction_id)
        self._checkpoint(journal_path, journal, "rolled_back", crash_after)
        self._release(transaction_id)
        return {
            "action": "recover-install",
            "status": "ROLLED_BACK",
            "transaction_id": transaction_id,
            "target_preexisting": False,
            "target_present": False,
            "quarantine": str(quarantine) if quarantine.exists() else None,
            "quarantine_retained": quarantine.exists(),
        }

    def _load_committed_install(
        self, source: GitSource
    ) -> tuple[dict[str, Any], dict[str, Any], Path, dict[str, Any]]:
        if not self.paths.active_path.is_file():
            raise CapabilityError("No active Antigravity install record exists")
        active = self._read_active()
        install_id = str(active["install_transaction_id"])
        operation = self._operation_dir(install_id)
        journal = read_json(operation / "journal.json")
        if journal.get("state") != "committed":
            raise CapabilityError("Active Antigravity install is not committed")
        if journal.get("transaction_id") != install_id or journal.get("operation") != "install":
            raise CapabilityError("Active Antigravity install journal is invalid")
        expected = {
            "source_commit": source.source_commit,
            "source_tree": source.source_tree,
            "payload_fingerprint": source.payload_fingerprint,
            "manifest_fingerprint": source.manifest_fingerprint,
            "target_preexisting": False,
        }
        for key, value in expected.items():
            if journal.get(key) != value:
                raise CapabilityError(f"Committed install mismatch for {key}")
        if not same_path(str(journal.get("target", "")), self.paths.target):
            raise CapabilityError("Committed install target path mismatch")
        marker = self._validate_marker_for_source(journal.get("marker"), source)
        self._verify_active(source, marker)
        if active.get("payload_fingerprint") != source.payload_fingerprint:
            raise CapabilityError("Active-install payload fingerprint mismatch")
        return active, journal, operation, marker

    def _validate_rollback_journal(
        self,
        journal: dict[str, Any],
        *,
        transaction_id: str,
        operation: Path,
        source: GitSource,
        approval: ApprovalReceipt,
        allow_missing_receipt: bool = False,
    ) -> tuple[str, Path]:
        if journal.get("schema_version") != 1 or journal.get("operation") != "rollback":
            raise CapabilityError("Rollback journal schema or operation is invalid")
        if journal.get("transaction_id") != transaction_id:
            raise CapabilityError("Rollback journal transaction ID mismatch")
        original_id = str(journal.get("original_install_transaction_id", ""))
        if not TRANSACTION_ID.fullmatch(original_id):
            raise CapabilityError("Rollback journal original install ID is invalid")
        quarantine = self.paths.quarantine_root / f"rollback-{transaction_id}-target"
        expected_paths = {
            "target": self.paths.target,
            "state_root": self.paths.state_root,
            "operation_dir": operation,
            "quarantine": quarantine,
        }
        for key, expected in expected_paths.items():
            actual = journal.get(key)
            if not isinstance(actual, str) or not same_path(actual, expected):
                raise CapabilityError(f"Rollback journal path mismatch for {key}")
        expected_values = {
            "source_commit": source.source_commit,
            "source_tree": source.source_tree,
            "payload_fingerprint": source.payload_fingerprint,
            "manifest_fingerprint": source.manifest_fingerprint,
            "target_preexisting": False,
            "approval_id": approval.approval_id,
            "approval_sha256": approval.sha256,
        }
        for key, value in expected_values.items():
            if journal.get(key) != value:
                raise CapabilityError(f"Rollback journal mismatch for {key}")
        stored_receipt = operation / "approval-receipt.json"
        if not stored_receipt.exists() and allow_missing_receipt:
            return original_id, quarantine
        if not stored_receipt.is_file() or stored_receipt.read_bytes() != approval.raw:
            raise CapabilityError("Stored rollback approval receipt does not match")
        return original_id, quarantine

    def rollback(
        self,
        approval_path: Path | str,
        *,
        owner_authorized: bool,
        crash_after: str | None = None,
    ) -> dict[str, Any]:
        source, context = self._context(require_current_hashes=False)
        if self.paths.lock_path.exists():
            raise CapabilityError("Another Antigravity operation holds the target lock")
        approval = self._approval(
            approval_path,
            context=context,
            action="rollback",
            owner_authorized=owner_authorized,
        )
        active, _, _, _ = self._load_committed_install(source)
        original_id = str(active["install_transaction_id"])
        self._create_state_roots()
        transaction_id = uuid.uuid4().hex
        self._acquire(transaction_id, "rollback")
        operation = self._operation_dir(transaction_id)
        journal_path = operation / "journal.json"
        try:
            operation.mkdir()
            quarantine = self.paths.quarantine_root / f"rollback-{transaction_id}-target"
            journal: dict[str, Any] = {
                "schema_version": 1,
                "operation": "rollback",
                "transaction_id": transaction_id,
                "original_install_transaction_id": original_id,
                "target": str(Path(os.path.abspath(self.paths.target))),
                "state_root": str(Path(os.path.abspath(self.paths.state_root))),
                "operation_dir": str(Path(os.path.abspath(operation))),
                "quarantine": str(Path(os.path.abspath(quarantine))),
                "target_preexisting": False,
                "source_commit": source.source_commit,
                "source_tree": source.source_tree,
                "payload_fingerprint": source.payload_fingerprint,
                "manifest_fingerprint": source.manifest_fingerprint,
                "approval_id": approval.approval_id,
                "approval_sha256": approval.sha256,
                "created_at": utc_now(),
            }
            self._checkpoint(journal_path, journal, "rollback_preparing", crash_after)
            atomic_write_bytes(operation / "approval-receipt.json", approval.raw)
            self._checkpoint(journal_path, journal, "approval_recorded", crash_after)
            self._load_committed_install(source)
            fresh_approval = self._approval(
                approval_path,
                context=context,
                action="rollback",
                owner_authorized=owner_authorized,
            )
            if fresh_approval.sha256 != approval.sha256:
                raise CapabilityError("Rollback approval receipt changed before target move")
            self._checkpoint(journal_path, journal, "rollback_prepared", crash_after)
            os.replace(self.paths.target, quarantine)
            fsync_parent(quarantine)
            if crash_after == "rollback_target_renamed":
                raise InjectedCrash("rollback_target_renamed")
            if self.paths.target.exists() or not quarantine.is_dir():
                raise CapabilityError("Rollback did not restore exact target absence")
            scanned = scan_directory(
                quarantine, exclude_marker=True, reject_hardlinks=True
            )
            if scanned["payload_fingerprint"] != source.payload_fingerprint:
                raise CapabilityError("Rollback quarantine fingerprint mismatch")
            self._checkpoint(
                journal_path, journal, "rollback_target_quarantined", crash_after
            )
            self._clear_active(original_id)
            self._checkpoint(journal_path, journal, "active_cleared", crash_after)
            self._checkpoint(journal_path, journal, "rolled_back", crash_after)
            self._release(transaction_id)
            return {
                "action": "rollback",
                "status": "ROLLED_BACK",
                "transaction_id": transaction_id,
                "original_install_transaction_id": original_id,
                "target_preexisting": False,
                "target_present": False,
                "quarantine": str(quarantine),
                "quarantine_retained": True,
                "rules_or_config_mutation": False,
            }
        except Exception:
            if not journal_path.exists() and self.paths.lock_path.exists():
                self._release(transaction_id)
            raise

    def recover_rollback(
        self,
        approval_path: Path | str,
        *,
        owner_authorized: bool,
        crash_after: str | None = None,
    ) -> dict[str, Any]:
        source, context = self._context(require_current_hashes=False)
        if not self.paths.lock_path.is_file():
            raise CapabilityError("No interrupted Antigravity operation lock exists")
        lock = read_json(self.paths.lock_path)
        transaction_id = str(lock.get("transaction_id", ""))
        if lock.get("operation") != "rollback" or not TRANSACTION_ID.fullmatch(
            transaction_id
        ):
            raise CapabilityError("Active lock is not a rollback transaction")
        approval = self._approval(
            approval_path,
            context=context,
            action="recover-rollback",
            owner_authorized=owner_authorized,
        )
        operation = self._operation_dir(transaction_id)
        journal_path = operation / "journal.json"
        journal = read_json(journal_path)
        initial_state = str(journal.get("state", ""))
        original_id, quarantine = self._validate_rollback_journal(
            journal,
            transaction_id=transaction_id,
            operation=operation,
            source=source,
            approval=approval,
            allow_missing_receipt=initial_state == "rollback_preparing",
        )
        stored_receipt = operation / "approval-receipt.json"
        if not stored_receipt.exists():
            atomic_write_bytes(stored_receipt, approval.raw)
            self._checkpoint(journal_path, journal, "approval_recorded", crash_after)
        if self.paths.target.exists():
            if quarantine.exists():
                raise CapabilityError("Rollback target and quarantine both exist")
            active = self._read_active()
            if active.get("install_transaction_id") != original_id:
                raise CapabilityError("Rollback recovery active install mismatch")
            _, install, _, marker = self._load_committed_install(source)
            if install.get("transaction_id") != original_id:
                raise CapabilityError("Rollback recovery original journal mismatch")
            self._verify_active(source, marker)
            os.replace(self.paths.target, quarantine)
            fsync_parent(quarantine)
            if crash_after == "recovery_rollback_target_renamed":
                raise InjectedCrash("recovery_rollback_target_renamed")
            self._checkpoint(
                journal_path, journal, "rollback_target_quarantined", crash_after
            )
        elif not quarantine.is_dir():
            raise CapabilityError("Rollback target and quarantine are both missing")

        scanned = scan_directory(quarantine, exclude_marker=True, reject_hardlinks=True)
        if scanned["payload_fingerprint"] != source.payload_fingerprint:
            raise CapabilityError("Rollback recovery quarantine fingerprint mismatch")
        if self.paths.target.exists():
            raise CapabilityError("Rollback recovery did not restore target absence")
        self._clear_active(original_id)
        self._checkpoint(journal_path, journal, "active_cleared", crash_after)
        self._checkpoint(journal_path, journal, "rolled_back", crash_after)
        self._release(transaction_id)
        return {
            "action": "recover-rollback",
            "status": "ROLLED_BACK",
            "transaction_id": transaction_id,
            "original_install_transaction_id": original_id,
            "target_preexisting": False,
            "target_present": False,
            "quarantine": str(quarantine),
            "quarantine_retained": True,
        }


def emit(value: object, *, stream: Any = sys.stdout) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), file=stream)


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--expected-config-sha256", required=True)
    parser.add_argument("--expected-rules-sha256", required=True)
    parser.add_argument("--approval-posture-sha256", required=True)
    parser.add_argument(
        "--fixture-root",
        help=f"Unique {FIXTURE_PREFIX}* child of system TEMP; never a real profile",
    )


def add_mutating_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--approval-file", required=True)
    parser.add_argument("--owner-authorized", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    plan = subparsers.add_parser("plan", help="Read-only exact release and target plan")
    add_common_arguments(plan)
    for name, description in (
        ("install", "V1.1B-gated first install"),
        ("recover-install", "Owner-authorized interrupted-install recovery"),
        ("rollback", "Owner-authorized exact-absence rollback"),
        ("recover-rollback", "Owner-authorized interrupted-rollback recovery"),
    ):
        action = subparsers.add_parser(name, help=description)
        add_common_arguments(action)
        add_mutating_arguments(action)
    return parser


def installer_from_args(args: argparse.Namespace) -> AntigravityFirstInstall:
    return AntigravityFirstInstall(
        repo=args.repo,
        commit=args.commit,
        paths=resolve_paths(args.fixture_root),
        expected_config_sha256=args.expected_config_sha256,
        expected_rules_sha256=args.expected_rules_sha256,
        approval_posture_sha256=args.approval_posture_sha256,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        installer = installer_from_args(args)
        if args.action == "plan":
            result = installer.plan()
        elif args.action == "install":
            result = installer.install(
                args.approval_file, owner_authorized=args.owner_authorized
            )
        elif args.action == "recover-install":
            result = installer.recover_install(
                args.approval_file, owner_authorized=args.owner_authorized
            )
        elif args.action == "rollback":
            result = installer.rollback(
                args.approval_file, owner_authorized=args.owner_authorized
            )
        elif args.action == "recover-rollback":
            result = installer.recover_rollback(
                args.approval_file, owner_authorized=args.owner_authorized
            )
        else:
            raise CapabilityError(f"Unsupported action: {args.action}")
        emit(result)
        return 0
    except (CapabilityError, OSError) as exc:
        emit(
            {
                "action": args.action,
                "status": "REFUSED",
                "error": str(exc),
                "writes_performed": 0 if args.action == "plan" else "bounded-or-none",
                "security_statement": SECURITY_STATEMENT,
            },
            stream=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
