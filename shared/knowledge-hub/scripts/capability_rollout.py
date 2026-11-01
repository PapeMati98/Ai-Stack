#!/usr/bin/env python3
"""Gated V1.2 install, update, recovery and rollback for registered targets."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import re
import secrets
import stat
import sys
import uuid
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
    canonical_json_hash,
    find_managed_marker,
    git_executable_identity,
    git_is_ancestor,
    inspect_git_source,
    marker_v2,
    parse_iso_timestamp,
    read_marker,
    scan_directory,
    sha256_bytes,
    strict_json_loads,
    run_git,
    utc_now,
)
from capability_targets import (
    REGISTRY_NAME,
    ResolvedTarget,
    guard_hashes,
    load_registry,
    resolve_target,
)
from capability_matrix import MATRIX_NAME, load_matrix, scan_root_inventory
from capability_revocations import (
    ENTRY_KEYS as REVOCATION_ENTRY_KEYS,
    REVOCATION_NAME,
    RevocationCatalog,
    load_revocations,
    matching_revocation,
)


APPROVAL_KIND = "capability-v1.2-target-mutation"
ENGINE_COMPATIBILITY_VERSION = 1
ENGINE_PATHS = (
    "scripts/capability_core.py",
    "scripts/capability_matrix.py",
    "scripts/capability_revocations.py",
    "scripts/capability_rollout.py",
    "scripts/capability_targets.py",
)
APPROVAL_ID = re.compile(r"^[a-z0-9][a-z0-9-]{7,127}$")
TRANSACTION_ID = re.compile(r"^[0-9a-f]{32}$")
ATOMIC_TEMP = re.compile(r"^\.atomic-.+\.tmp-[0-9a-f]{32}$")
REPLACE_TEMP = re.compile(r"^\..+\.tmp-[0-9a-f]{32}$")
FETCH_HEAD_LINE = re.compile(
    r"^(?P<oid>[0-9a-f]{40}(?:[0-9a-f]{24})?)\t\t"
    r"branch 'main' of (?P<remote>[^\r\n]+)$"
)
FETCH_HEAD_MAX_BYTES = 16 * 1024
ALLOWED_ACTIONS = {
    "deploy",
    "emergency-rollback",
    "record-discovery",
    "recover-deploy",
    "rollback",
    "recover-rollback",
}
APPROVAL_KEYS = {
    "approval_id",
    "approval_kind",
    "approved_at",
    "approved_by",
    "authorized_actions",
    "context",
    "context_sha256",
    "expires_at",
    "owner_decision",
    "schema_version",
}
ACTIVE_KEYS = {
    "capability_id",
    "context_sha256",
    "deploy_transaction_id",
    "discovery_challenge",
    "manifest_fingerprint",
    "scan_root_inventory_sha256",
    "operation_kind",
    "payload_fingerprint",
    "schema_version",
    "source_commit",
    "source_tree",
    "tool",
    "updated_at",
}
LOCK_KEYS = {
    "approval_id",
    "approval_sha256",
    "context",
    "context_sha256",
    "created_at",
    "operation",
    "pid",
    "requested_action",
    "transaction_id",
}
LEGACY_LOCK_KEYS = LOCK_KEYS - {"requested_action"}
ROLLBACK_AUTHORIZATION_KEYS = {
    "active_release_revocation",
    "approval_posture_evidence",
    "approval_posture_sha256",
    "authorization_scope",
    "capability_id",
    "deploy_context",
    "deploy_context_sha256",
    "deploy_transaction_id",
    "engine",
    "git_executable",
    "guard_hashes",
    "matrix_scan_root_inventory_sha256",
    "matrix_sha256",
    "matrix_status",
    "policy_commit",
    "prior_release_revocation",
    "registry_sha256",
    "revocation_catalog_sha256",
    "revocation_catalog_updated_at",
    "revocation_checked_at",
    "risk_gate",
    "robert_same_user_risk_accepted",
    "scan_root_inventory_sha256",
    "schema_version",
    "state_root",
    "target",
    "tool",
    "writes_scope",
}
RECOVERY_RESULT_KEYS = {
    "action",
    "approval_sha256",
    "result",
    "result_sha256",
    "schema_version",
    "transaction_id",
}
MUTATION_RESULT_KEYS = {
    "action",
    "approval_sha256",
    "result",
    "result_sha256",
    "schema_version",
    "transaction_id",
}
DISCOVERY_EVIDENCE_KEYS = {
    "activation_boundary",
    "capability_id",
    "capability_version",
    "deploy_transaction_id",
    "discovered_skill_path",
    "discovery_challenge",
    "distinctive_rule",
    "distinctive_rule_sha256",
    "fresh_session",
    "observed_at",
    "observed_output",
    "observed_output_sha256",
    "observer",
    "runtime_version",
    "schema_version",
    "source_commit",
    "source_tree",
    "tool",
}
DISCOVERY_RECORD_KEYS = {
    "approval_id",
    "approval_sha256",
    "context_sha256",
    "deploy_transaction_id",
    "evidence_sha256",
    "recorded_at",
    "schema_version",
}
TERMINAL_RESULT_KEYS = {
    ("deploy", "COMMITTED"): {
        "action",
        "activation_boundary",
        "discovery_required",
        "operation_kind",
        "payload_fingerprint",
        "prior_target_retained",
        "rules_or_config_mutation",
        "status",
        "target",
        "transaction_id",
    },
    ("rollback", "ROLLED_BACK"): {
        "action",
        "deploy_transaction_id",
        "prior_state_restored",
        "quarantine",
        "quarantine_retained",
        "rules_or_config_mutation",
        "status",
        "target_present",
        "transaction_id",
    },
    ("emergency-rollback", "ROLLED_BACK"): {
        "action",
        "deploy_transaction_id",
        "prior_state_restored",
        "quarantine",
        "quarantine_retained",
        "rules_or_config_mutation",
        "status",
        "target_present",
        "transaction_id",
    },
    ("rollback", "REMOVED_WITHOUT_RESTORE"): {
        "action",
        "backup_retained",
        "deploy_transaction_id",
        "prior_release_revocation",
        "prior_state_restored",
        "quarantine",
        "quarantine_retained",
        "revoked_prior_quarantine",
        "revoked_prior_quarantine_retained",
        "rules_or_config_mutation",
        "status",
        "target_present",
        "transaction_id",
    },
    ("emergency-rollback", "REMOVED_WITHOUT_RESTORE"): {
        "action",
        "backup_retained",
        "deploy_transaction_id",
        "prior_release_revocation",
        "prior_state_restored",
        "quarantine",
        "quarantine_retained",
        "revoked_prior_quarantine",
        "revoked_prior_quarantine_retained",
        "rules_or_config_mutation",
        "status",
        "target_present",
        "transaction_id",
    },
    ("recover-deploy", "COMMITTED"): {
        "action",
        "status",
        "target_present",
        "transaction_id",
    },
    ("recover-deploy", "ROLLED_BACK"): {
        "action",
        "prior_state_restored",
        "quarantine",
        "quarantine_retained",
        "status",
        "target_present",
        "transaction_id",
    },
    ("recover-deploy", "REMOVED_WITHOUT_RESTORE"): {
        "action",
        "new_quarantine",
        "new_quarantine_retained",
        "prior_backup_retained",
        "prior_release_revocation",
        "prior_state_restored",
        "revoked_prior_quarantine",
        "status",
        "target_present",
        "transaction_id",
    },
    ("recover-rollback", "ROLLED_BACK"): {
        "action",
        "deploy_transaction_id",
        "prior_state_restored",
        "quarantine",
        "quarantine_retained",
        "status",
        "target_present",
        "transaction_id",
    },
    ("recover-deploy", "ABORTED_NO_WRITES"): {
        "action",
        "status",
        "target_present",
        "transaction_id",
        "writes_scope",
    },
    ("recover-rollback", "ABORTED_NO_WRITES"): {
        "action",
        "status",
        "target_present",
        "transaction_id",
        "writes_scope",
    },
    ("recover-rollback", "ABORTED_PRECONDITION_DRIFT"): {
        "action",
        "reason",
        "status",
        "target_present",
        "transaction_id",
        "writes_scope",
    },
    ("recover-rollback", "REMOVED_WITHOUT_RESTORE"): {
        "action",
        "backup_retained",
        "deploy_transaction_id",
        "prior_release_revocation",
        "prior_state_restored",
        "quarantine",
        "quarantine_retained",
        "revoked_prior_quarantine",
        "revoked_prior_quarantine_retained",
        "status",
        "target_present",
        "transaction_id",
    },
}


class InjectedCrash(RuntimeError):
    """Fixture-only process-loss checkpoint."""


def canonical_json_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2).encode(
        "ascii"
    ) + b"\n"


def engine_identity(repo: Path | str) -> dict[str, Any]:
    root = absolute_plain_path(Path(repo), must_exist=True)
    if not root.is_dir():
        raise CapabilityError("Capability engine repository is not a directory")
    files: dict[str, str] = {}
    for relative in ENGINE_PATHS:
        path = absolute_plain_path(root / PurePosixPath(relative), must_exist=True)
        if not stat.S_ISREG(path.lstat().st_mode):
            raise CapabilityError(f"Capability engine path is not a plain file: {relative}")
        files[relative] = sha256_bytes(path.read_bytes())
    identity = {
        "compatibility_version": ENGINE_COMPATIBILITY_VERSION,
        "files": files,
    }
    return {
        **identity,
        "fingerprint": canonical_json_hash(identity),
    }


def validate_engine_identity(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "compatibility_version",
        "files",
        "fingerprint",
    }:
        raise CapabilityError(f"{label} engine identity is invalid")
    files = value.get("files")
    if (
        value.get("compatibility_version") != ENGINE_COMPATIBILITY_VERSION
        or not isinstance(files, dict)
        or set(files) != set(ENGINE_PATHS)
        or any(not isinstance(item, str) or not HEX_64.fullmatch(item) for item in files.values())
    ):
        raise CapabilityError(f"{label} engine identity is incompatible or invalid")
    expected = canonical_json_hash(
        {
            "compatibility_version": value["compatibility_version"],
            "files": files,
        }
    )
    if value.get("fingerprint") != expected:
        raise CapabilityError(f"{label} engine fingerprint is invalid")
    return dict(value)


def require_engine_at_ref(repo: Path | str, ref: str) -> dict[str, Any]:
    identity = engine_identity(repo)
    for relative, digest in identity["files"].items():
        committed = run_git(repo, ["show", f"{ref}:{relative}"])
        if sha256_bytes(committed) != digest:
            raise CapabilityError(
                f"Local capability engine differs from {ref}: {relative}"
            )
    return identity


def validate_git_identity(value: Any, label: str) -> dict[str, str]:
    if (
        not isinstance(value, dict)
        or set(value) != {"path", "sha256"}
        or not isinstance(value.get("path"), str)
        or not value["path"].strip()
        or not isinstance(value.get("sha256"), str)
        or not HEX_64.fullmatch(value["sha256"])
    ):
        raise CapabilityError(f"{label} Git executable identity is invalid")
    return {"path": value["path"], "sha256": value["sha256"]}


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
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        fsync_parent(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    atomic_write_bytes(path, canonical_json_bytes(value))


def atomic_publish_no_replace(temporary: Path, destination: Path) -> None:
    if os.name == "nt":
        import ctypes

        move_file = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
        move_file.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        move_file.restype = ctypes.c_int
        if not move_file(str(temporary), str(destination), 0x00000008):
            error = ctypes.get_last_error()
            if error in {80, 183}:
                raise FileExistsError(str(destination))
            raise CapabilityError(
                f"Atomic no-replace publication failed with Windows error {error}"
            )
    else:
        os.link(temporary, destination, follow_symlinks=False)
        temporary.unlink()
    fsync_parent(destination)


def immutable_write_bytes(path: Path, value: bytes, label: str) -> None:
    if path.exists():
        plain = absolute_plain_path(path, must_exist=True)
        if not stat.S_ISREG(plain.lstat().st_mode) or plain.read_bytes() != value:
            raise CapabilityError(f"Existing immutable {label} differs")
        return
    temporary = path.with_name(f".atomic-{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        with temporary.open("xb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        fsync_parent(temporary)
        try:
            atomic_publish_no_replace(temporary, path)
        except FileExistsError:
            plain = absolute_plain_path(path, must_exist=True)
            if not stat.S_ISREG(plain.lstat().st_mode) or plain.read_bytes() != value:
                raise CapabilityError(f"Existing immutable {label} differs")
    finally:
        if temporary.exists():
            temporary.unlink()
            fsync_parent(temporary)


def read_json(path: Path, label: str = "transaction") -> dict[str, Any]:
    try:
        plain = absolute_plain_path(path, must_exist=True)
        if not stat.S_ISREG(plain.lstat().st_mode):
            raise CapabilityError(f"{label} JSON is not a plain file: {plain}")
        value = strict_json_loads(plain.read_text(encoding="ascii"))
    except (OSError, UnicodeDecodeError, CapabilityError) as exc:
        raise CapabilityError(f"Invalid {label} JSON: {path}") from exc
    if not isinstance(value, dict):
        raise CapabilityError(f"{label} JSON must be an object: {path}")
    return value


def same_path(left: Path | str, right: Path | str) -> bool:
    return os.path.normcase(os.path.abspath(os.fspath(left))) == os.path.normcase(
        os.path.abspath(os.fspath(right))
    )


def allowed_transaction_temporary(name: str, final_names: set[str]) -> bool:
    if not (ATOMIC_TEMP.fullmatch(name) or REPLACE_TEMP.fullmatch(name)):
        return False
    return any(
        name.startswith(f".{final_name}.tmp-")
        or name.startswith(f".atomic-{final_name}.tmp-")
        for final_name in final_names
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


def backup_like_directories(scan_root: Path) -> list[str]:
    root = absolute_plain_path(scan_root, must_exist=True)
    if not root.is_dir():
        raise CapabilityError(f"Capability scan root is not a directory: {root}")
    found: list[str] = []
    with os.scandir(root) as iterator:
        entries = sorted(iterator, key=lambda item: item.name.casefold())
    for entry in entries:
        if ".backup-" not in entry.name.casefold():
            continue
        details = entry.stat(follow_symlinks=False)
        if entry.is_symlink() or not stat.S_ISDIR(details.st_mode):
            raise CapabilityError(
                f"Backup-like scan-root entry is not a plain directory: {entry.name}"
            )
        found.append(entry.name)
    return found


def semver_key(value: str) -> tuple[int, int, int, int, str]:
    main = value.split("+", 1)[0]
    base, separator, suffix = main.partition("-")
    try:
        major, minor, patch = (int(item) for item in base.split("."))
    except (ValueError, TypeError) as exc:
        raise CapabilityError(f"Invalid semantic version: {value}") from exc
    return major, minor, patch, 1 if not separator else 0, suffix


def load_release(
    repo: Path | str,
    commit: str,
    capability_id: str,
    *,
    require_clean_worktree: bool = True,
) -> GitSource:
    if not GIT_OID.fullmatch(commit):
        raise CapabilityError("Release commit must be a full Git object ID, not a ref")
    source_path = f"capabilities/{capability_id}"
    source = inspect_git_source(repo, commit, source_path, pilot_policy=True)
    if source.source_commit != commit:
        raise CapabilityError("Resolved release commit differs from supplied full OID")
    manifest = source.capability_manifest or {}
    if manifest.get("capability_id") != capability_id:
        raise CapabilityError("Release capability_id does not match requested capability")
    if manifest.get("revoked_at") is not None:
        raise CapabilityError("Refusing a revoked capability release")
    if require_clean_worktree and source.worktree_contamination:
        raise CapabilityError("Release path has worktree contamination")
    return source


def validate_paths(target: ResolvedTarget) -> None:
    scan_root = absolute_plain_path(target.scan_root, must_exist=True)
    if not scan_root.is_dir():
        raise CapabilityError(f"Capability scan root is not a directory: {scan_root}")
    if not same_path(target.target.parent, scan_root):
        raise CapabilityError("Capability target must be a direct scan-root child")
    absolute_plain_path(target.target, must_exist=target.target.exists())
    absolute_plain_path(target.state_root, must_exist=target.state_root.exists())
    for path in target.guards.values():
        absolute_plain_path(path, must_exist=True)
    if not same_volume(target.target, target.state_root):
        raise CapabilityError("Target and transaction state are not on the same volume")
    try:
        Path(os.path.abspath(target.state_root)).relative_to(
            Path(os.path.abspath(target.scan_root))
        )
    except ValueError:
        pass
    else:
        raise CapabilityError("Transaction state must remain outside skill scan root")


def snapshot_directory(
    path: Path,
    *,
    capability_id: str,
    tool: str,
) -> dict[str, Any]:
    if not path.exists():
        return {"present": False}
    marker_path = find_managed_marker(path)
    if marker_path is None:
        raise CapabilityError("Existing target is unmanaged; overwrite is forbidden")
    if marker_path.name != CAPABILITY_MARKER_NAME:
        raise CapabilityError("Existing target uses a legacy marker; migration is required")
    marker = read_marker(marker_path)
    if marker.schema_version != 2:
        raise CapabilityError("Existing target marker is not schema v2")
    data = marker.data
    if data.get("capability_id") != capability_id:
        raise CapabilityError("Existing target capability_id does not match expected ID")
    if data.get("tool") != tool:
        raise CapabilityError("Existing target marker tool does not match target registry")
    scanned = scan_directory(
        path, exclude_marker=True, reject_hardlinks=True
    )
    if scanned["payload_fingerprint"] != data.get("payload_fingerprint"):
        raise CapabilityError("Existing target payload does not match its managed marker")
    marker_raw = marker_path.read_bytes()
    return {
        "present": True,
        "capability_id": data["capability_id"],
        "capability_version": data["capability_version"],
        "source_commit": data["source_commit"],
        "source_tree": data["source_tree"],
        "payload_fingerprint": data["payload_fingerprint"],
        "manifest_fingerprint": data["manifest_fingerprint"],
        "marker": data,
        "marker_sha256": sha256_bytes(marker_raw),
    }


def snapshot_target(target: ResolvedTarget) -> dict[str, Any]:
    return snapshot_directory(
        target.target,
        capability_id=target.target.name,
        tool=target.spec.tool,
    )


def materialize_release(source: GitSource, target: Path, marker: dict[str, Any]) -> None:
    if target.exists():
        raise CapabilityError(f"Staging path already exists: {target}")
    target.mkdir(parents=False)
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


def parse_timestamp(value: str, field: str) -> dt.datetime:
    parse_iso_timestamp(value, field)
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def git_commit_at_ref(repo: Path | str, ref: str) -> str:
    value = run_git(
        repo,
        ["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
    ).decode("ascii", errors="strict").strip()
    if not GIT_OID.fullmatch(value):
        raise CapabilityError(f"Git ref {ref} did not resolve to a full commit OID")
    return value


def _origin_reflog_timestamp(
    repo: Path | str, ref: str, expected_oid: str
) -> dt.datetime:
    value = run_git(
        repo,
        [
            "reflog",
            "show",
            "-1",
            "--date=iso-strict",
            "--format=%H%x00%gD",
            ref,
        ],
    )
    fields = value.rstrip(b"\r\n").split(b"\x00")
    if len(fields) != 2:
        raise CapabilityError(f"Cannot derive the last fetch timestamp for {ref}")
    observed_oid = fields[0].decode("ascii", errors="strict")
    if observed_oid != expected_oid:
        raise CapabilityError(f"Latest {ref} reflog entry has a different OID")
    selector = fields[1].decode("ascii", errors="strict")
    match = re.fullmatch(r".+@\{(.+)\}", selector)
    if not match:
        raise CapabilityError(f"Cannot derive the last fetch timestamp for {ref}")
    return parse_timestamp(match.group(1), f"{ref} reflog timestamp")


def _fetch_remote_display(value: str) -> str:
    normalized = value.rstrip("/\\")
    if normalized.lower().endswith(".git"):
        normalized = normalized[:-4]
    return normalized


def _verified_fetch_head_timestamp(
    repo: Path | str, expected_oid: str
) -> dt.datetime:
    raw_path = run_git(
        repo,
        ["rev-parse", "--path-format=absolute", "--git-path", "FETCH_HEAD"],
    )
    path_lines = raw_path.decode("utf-8", errors="strict").splitlines()
    if len(path_lines) != 1 or not path_lines[0]:
        raise CapabilityError("FETCH_HEAD path is invalid")
    fetch_head = absolute_plain_path(path_lines[0], must_exist=True)
    with fetch_head.open("rb") as handle:
        before = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size < 1
            or before.st_size > FETCH_HEAD_MAX_BYTES
        ):
            raise CapabilityError("FETCH_HEAD is not a bounded plain file")
        raw = handle.read(FETCH_HEAD_MAX_BYTES + 1)
        after = os.fstat(handle.fileno())
    def identity(details: os.stat_result) -> tuple[int, int, int, int, int, int]:
        return (
            details.st_dev,
            details.st_ino,
            details.st_mode,
            details.st_nlink,
            details.st_size,
            details.st_mtime_ns,
        )
    if identity(before) != identity(after) or len(raw) != before.st_size:
        raise CapabilityError("FETCH_HEAD changed while it was read")
    current = fetch_head.lstat()
    if identity(before) != identity(current):
        raise CapabilityError("FETCH_HEAD changed after it was read")
    lines = raw.decode("utf-8", errors="strict").splitlines()
    if len(lines) != 1:
        raise CapabilityError("FETCH_HEAD must contain one exact fetch record")
    match = FETCH_HEAD_LINE.fullmatch(lines[0])
    if match is None or match.group("oid") != expected_oid:
        raise CapabilityError("FETCH_HEAD does not prove the current origin/main")
    remote_raw = run_git(
        repo, ["config", "--local", "--get-all", "remote.origin.url"]
    )
    remotes = remote_raw.decode("utf-8", errors="strict").splitlines()
    if len(remotes) != 1 or not remotes[0]:
        raise CapabilityError("origin must have one local fetch URL")
    if _fetch_remote_display(match.group("remote")) != _fetch_remote_display(
        remotes[0]
    ):
        raise CapabilityError("FETCH_HEAD does not match the origin fetch URL")
    return dt.datetime.fromtimestamp(before.st_mtime, tz=dt.timezone.utc)


def remote_fetch_timestamp(
    repo: Path | str,
    *,
    expected_oid: str,
    ref: str = "refs/remotes/origin/main",
) -> str:
    if not GIT_OID.fullmatch(expected_oid):
        raise CapabilityError("Remote fetch evidence requires a full expected OID")
    if git_commit_at_ref(repo, ref) != expected_oid:
        raise CapabilityError("origin/main changed before fetch evidence validation")
    candidates: list[dt.datetime] = []
    for loader in (
        lambda: _origin_reflog_timestamp(repo, ref, expected_oid),
        lambda: _verified_fetch_head_timestamp(repo, expected_oid),
    ):
        try:
            candidates.append(loader())
        except (CapabilityError, OSError, UnicodeError, ValueError):
            continue
    if not candidates:
        raise CapabilityError(
            "Cannot derive fresh origin/main fetch evidence from reflog or FETCH_HEAD"
        )
    if git_commit_at_ref(repo, ref) != expected_oid:
        raise CapabilityError("origin/main changed while fetch evidence was validated")
    latest = max(candidates).astimezone(dt.timezone.utc)
    return latest.isoformat().replace("+00:00", "Z")


def ensure_non_elevated_real_mode(target: ResolvedTarget) -> None:
    if target.fixture_root is not None:
        return
    if os.name == "nt":
        import ctypes

        elevated = bool(ctypes.windll.shell32.IsUserAnAdmin())
    else:
        elevated = os.geteuid() == 0
    if elevated:
        raise CapabilityError("Real capability rollout refuses an elevated/root process")


def load_approval(
    path: Path | str,
    *,
    context: dict[str, Any],
    required_action: str,
    target: ResolvedTarget,
    allow_expired: bool = False,
    allow_state_receipt: bool = False,
) -> tuple[dict[str, Any], bytes, str]:
    if required_action not in ALLOWED_ACTIONS:
        raise CapabilityError(f"Unsupported approval action: {required_action}")
    approval_path = absolute_plain_path(path, must_exist=True)
    if not stat.S_ISREG(approval_path.lstat().st_mode):
        raise CapabilityError("Approval receipt must be a plain file")
    if not allow_state_receipt:
        for forbidden in (target.scan_root, target.state_root):
            try:
                approval_path.relative_to(Path(os.path.abspath(forbidden)))
            except ValueError:
                continue
            raise CapabilityError(
                "Approval receipt must remain outside scan and state roots"
            )
    raw = approval_path.read_bytes()
    try:
        data = strict_json_loads(raw.decode("ascii", errors="strict"))
    except (UnicodeDecodeError, CapabilityError) as exc:
        raise CapabilityError("Approval receipt must be canonical ASCII JSON") from exc
    if not isinstance(data, dict) or set(data) != APPROVAL_KEYS:
        raise CapabilityError("Approval receipt keys mismatch")
    if raw != canonical_json_bytes(data):
        raise CapabilityError("Approval receipt is not canonical JSON")
    if data.get("schema_version") != 1 or data.get("approval_kind") != APPROVAL_KIND:
        raise CapabilityError("Approval receipt kind or schema is invalid")
    approval_id = data.get("approval_id")
    if not isinstance(approval_id, str) or not APPROVAL_ID.fullmatch(approval_id):
        raise CapabilityError("Approval receipt has invalid approval_id")
    if data.get("approved_by") != "Mateusz":
        raise CapabilityError("Capability mutation approval must be attributed to Mateusz")
    decision = data.get("owner_decision")
    if not isinstance(decision, str) or not decision.startswith("GO V1.2:"):
        raise CapabilityError("Approval receipt does not record an explicit GO V1.2")
    if (
        target.spec.risk_gate == "robert-identity-or-explicit-same-user"
        and context.get("robert_same_user_risk_accepted") is True
        and "ROBERT SAME-USER RISK ACCEPTED" not in decision
    ):
        raise CapabilityError(
            "Robert approval must explicitly record ROBERT SAME-USER RISK ACCEPTED"
        )
    if (
        required_action == "emergency-rollback"
        and "EMERGENCY ROLLBACK WITHOUT DISCOVERY" not in decision
    ):
        raise CapabilityError(
            "Emergency rollback approval must explicitly record "
            "EMERGENCY ROLLBACK WITHOUT DISCOVERY"
        )
    actions = data.get("authorized_actions")
    if (
        not isinstance(actions, list)
        or not actions
        or actions != sorted(set(actions))
        or any(item not in ALLOWED_ACTIONS for item in actions)
        or required_action not in actions
    ):
        raise CapabilityError(f"Approval receipt does not authorize {required_action}")
    companion = {
        "deploy": "recover-deploy",
        "emergency-rollback": "recover-rollback",
        "rollback": "recover-rollback",
    }.get(required_action)
    if companion is not None and companion not in actions:
        raise CapabilityError(
            f"Approval receipt must also authorize recovery action {companion}"
        )
    approved_at = parse_timestamp(str(data.get("approved_at", "")), "approved_at")
    expires_at = parse_timestamp(str(data.get("expires_at", "")), "expires_at")
    now = dt.datetime.now(dt.timezone.utc)
    if (
        expires_at <= approved_at
        or expires_at - approved_at > dt.timedelta(hours=24)
        or approved_at > now + dt.timedelta(minutes=5)
        or (now > expires_at and not allow_expired)
    ):
        raise CapabilityError("Approval receipt is expired or has invalid time window")
    if data.get("context") != context:
        raise CapabilityError("Approval receipt context mismatch")
    if data.get("context_sha256") != canonical_json_hash(context):
        raise CapabilityError("Approval receipt context hash mismatch")
    return dict(data), raw, sha256_bytes(raw)


def load_discovery_evidence(
    path: Path | str,
    *,
    target: ResolvedTarget,
    source: GitSource,
    context: dict[str, Any],
    deploy_transaction_id: str,
    discovery_challenge: str,
    activated_at: str,
    allow_state_evidence: bool = False,
) -> tuple[dict[str, Any], bytes]:
    evidence_path = absolute_plain_path(path, must_exist=True)
    if not stat.S_ISREG(evidence_path.lstat().st_mode):
        raise CapabilityError("Discovery evidence must be a plain file")
    if not allow_state_evidence:
        for forbidden in (target.scan_root, target.state_root):
            try:
                evidence_path.relative_to(Path(os.path.abspath(forbidden)))
            except ValueError:
                continue
            raise CapabilityError(
                "Discovery evidence must remain outside scan and state roots"
            )
    raw = evidence_path.read_bytes()
    try:
        value = strict_json_loads(raw.decode("ascii", errors="strict"))
    except (UnicodeDecodeError, CapabilityError) as exc:
        raise CapabilityError("Discovery evidence must be canonical ASCII JSON") from exc
    if (
        not isinstance(value, dict)
        or set(value) != DISCOVERY_EVIDENCE_KEYS
        or raw != canonical_json_bytes(value)
        or value.get("schema_version") != 2
        or value.get("fresh_session") is not True
    ):
        raise CapabilityError("Discovery evidence schema or encoding is invalid")
    expected = {
        "activation_boundary": target.spec.activation_boundary,
        "capability_id": context["capability_id"],
        "capability_version": context["capability_version"],
        "deploy_transaction_id": deploy_transaction_id,
        "discovery_challenge": discovery_challenge,
        "source_commit": source.source_commit,
        "source_tree": source.source_tree,
        "tool": target.spec.tool,
    }
    for key, expected_value in expected.items():
        if value.get(key) != expected_value:
            raise CapabilityError(f"Discovery evidence mismatch for {key}")
    skill_path = target.target / "SKILL.md"
    if not same_path(str(value.get("discovered_skill_path", "")), skill_path):
        raise CapabilityError("Discovery evidence skill path mismatch")
    if not TRANSACTION_ID.fullmatch(deploy_transaction_id):
        raise CapabilityError("Discovery deploy transaction ID is invalid")
    if not HEX_64.fullmatch(discovery_challenge):
        raise CapabilityError("Discovery challenge is invalid")
    rule = value.get("distinctive_rule")
    output = value.get("observed_output")
    if (
        not isinstance(rule, str)
        or not rule
        or rule.strip() != rule
        or len(rule) > 500
        or any(character in rule for character in "\r\n\x00")
    ):
        raise CapabilityError("Discovery distinctive rule is invalid")
    if not isinstance(output, str) or not output.strip() or len(output) > 16384:
        raise CapabilityError("Discovery observed output is invalid")
    skill_raw = run_git(
        source.repo,
        ["show", f"{source.source_commit}:{source.source_path}/SKILL.md"],
    )
    try:
        skill_text = skill_raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise CapabilityError("Committed SKILL.md is not valid UTF-8") from exc
    if rule not in skill_text:
        raise CapabilityError("Discovery distinctive rule is absent from SKILL.md")
    if value.get("distinctive_rule_sha256") != sha256_bytes(rule.encode("utf-8")):
        raise CapabilityError("Discovery distinctive rule hash does not match preimage")
    if value.get("observed_output_sha256") != sha256_bytes(output.encode("utf-8")):
        raise CapabilityError("Discovery observed output hash does not match preimage")
    for fragment in (
        str(skill_path),
        context["capability_version"],
        deploy_transaction_id,
        discovery_challenge,
        rule,
    ):
        if fragment not in output:
            raise CapabilityError(
                "Discovery observed output lacks deployment-bound preimage"
            )
    for key in ("distinctive_rule_sha256", "observed_output_sha256"):
        if not isinstance(value.get(key), str) or not HEX_64.fullmatch(value[key]):
            raise CapabilityError(f"Discovery evidence {key} is invalid")
    for key in ("observer", "runtime_version"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise CapabilityError(f"Discovery evidence {key} is invalid")
    observed_at = parse_timestamp(str(value.get("observed_at", "")), "observed_at")
    activated = parse_timestamp(activated_at, "activated_at")
    if observed_at < activated:
        raise CapabilityError("Discovery evidence predates target activation")
    if not allow_state_evidence:
        now = dt.datetime.now(dt.timezone.utc)
        if observed_at > now + dt.timedelta(
            minutes=5
        ) or now - observed_at > dt.timedelta(hours=24):
            raise CapabilityError("Discovery evidence is stale or in the future")
    return dict(value), raw


class CapabilityRollout:
    """Recoverable registered-target deployment with retained prior state."""

    def __init__(
        self,
        *,
        repo: Path | str,
        commit: str,
        capability_id: str,
        target: ResolvedTarget,
        approval_posture: str,
        registry_sha256: str,
        matrix_sha256: str,
        matrix_status: str,
        scan_root_inventory_sha256: str,
        policy_commit: str,
        revocation_catalog: RevocationCatalog | None,
        revocation_checked_at: str | None,
        robert_same_user_risk_accepted: bool = False,
        enforce_freshness: bool = True,
        allow_blocked_removal: bool = False,
    ) -> None:
        if not approval_posture or len(approval_posture) > 500:
            raise CapabilityError("approval posture must be a bounded non-empty string")
        if not HEX_64.fullmatch(registry_sha256):
            raise CapabilityError("registry hash must be a lowercase SHA-256")
        if not HEX_64.fullmatch(matrix_sha256):
            raise CapabilityError("matrix hash must be a lowercase SHA-256")
        if matrix_status not in {"blocked", "eligible", "pilot-verified"}:
            raise CapabilityError("Capability matrix status is invalid")
        if matrix_status == "blocked" and not allow_blocked_removal:
            raise CapabilityError("Capability matrix does not authorize target rollout")
        if not HEX_64.fullmatch(scan_root_inventory_sha256):
            raise CapabilityError("Matrix inventory hash must be a lowercase SHA-256")
        if (
            target.spec.risk_gate == "robert-identity-or-explicit-same-user"
            and not robert_same_user_risk_accepted
        ):
            raise CapabilityError(
                "Robert requires separate identity or explicit same-user risk acceptance"
            )
        if not GIT_OID.fullmatch(policy_commit):
            raise CapabilityError("Policy commit must be a full Git object ID")
        if enforce_freshness:
            if revocation_catalog is None or revocation_checked_at is None:
                raise CapabilityError("Current revocation catalog evidence is required")
            fetched_at = parse_timestamp(
                revocation_checked_at, "revocation_checked_at"
            )
            now = dt.datetime.now(dt.timezone.utc)
            if (
                fetched_at > now + dt.timedelta(minutes=5)
                or now - fetched_at > dt.timedelta(hours=24)
            ):
                raise CapabilityError(
                    "Revocation catalog fetch evidence is stale or in the future"
                )
        ensure_non_elevated_real_mode(target)
        self.repo = Path(repo)
        self.commit = commit
        self.capability_id = capability_id
        self.target = target
        self.approval_posture = approval_posture
        self.registry_sha256 = registry_sha256
        self.matrix_sha256 = matrix_sha256
        self.matrix_status = matrix_status
        self.scan_root_inventory_sha256 = scan_root_inventory_sha256
        self.policy_commit = policy_commit
        self.revocation_catalog = revocation_catalog
        self.revocation_checked_at = revocation_checked_at
        self.robert_same_user_risk_accepted = robert_same_user_risk_accepted
        self.enforce_freshness = enforce_freshness
        self.engine_root = Path(__file__).resolve().parents[1]
        self._lease_token = object()

    def _validate_policy_snapshot(self) -> RevocationCatalog | None:
        if not git_is_ancestor(self.repo, self.commit, self.policy_commit):
            raise CapabilityError("Release commit is not an ancestor of policy commit")
        if self.target.fixture_root is not None or not self.enforce_freshness:
            return self.revocation_catalog
        if git_commit_at_ref(self.repo, "refs/heads/main") != self.policy_commit:
            raise CapabilityError("Pinned policy commit is no longer current local main")
        require_engine_at_ref(self.repo, "refs/heads/main")
        origin_main = git_commit_at_ref(self.repo, "refs/remotes/origin/main")
        if not git_is_ancestor(self.repo, origin_main, self.policy_commit):
            raise CapabilityError("origin/main is not an ancestor of the policy commit")
        fetched_at_text = remote_fetch_timestamp(
            self.repo, expected_oid=origin_main
        )
        fetched_at = parse_timestamp(fetched_at_text, "revocation_checked_at")
        now = dt.datetime.now(dt.timezone.utc)
        if (
            fetched_at > now + dt.timedelta(minutes=5)
            or now - fetched_at > dt.timedelta(hours=24)
        ):
            raise CapabilityError(
                "Revocation catalog fetch evidence is stale or in the future"
            )
        committed_registry = run_git(
            self.repo, ["show", f"{self.policy_commit}:{REGISTRY_NAME}"]
        )
        committed_matrix = run_git(
            self.repo, ["show", f"{self.policy_commit}:{MATRIX_NAME}"]
        )
        committed_revocations = run_git(
            self.repo, ["show", f"{self.policy_commit}:{REVOCATION_NAME}"]
        )
        for path, committed, label in (
            (self.repo / REGISTRY_NAME, committed_registry, "target registry"),
            (self.repo / MATRIX_NAME, committed_matrix, "capability matrix"),
            (
                self.repo / REVOCATION_NAME,
                committed_revocations,
                "revocation catalog",
            ),
        ):
            if absolute_plain_path(path, must_exist=True).read_bytes() != committed:
                raise CapabilityError(f"Local {label} differs from policy commit")
        if sha256_bytes(committed_registry) != self.registry_sha256:
            raise CapabilityError("Current target registry differs from rollout policy")
        if sha256_bytes(committed_matrix) != self.matrix_sha256:
            raise CapabilityError("Current capability matrix differs from rollout policy")
        current_catalog = load_revocations(
            f"git-{self.policy_commit}-{REVOCATION_NAME}",
            raw=committed_revocations,
        )
        if (
            self.revocation_catalog is None
            or current_catalog.raw_sha256 != self.revocation_catalog.raw_sha256
            or current_catalog.updated_at != self.revocation_catalog.updated_at
        ):
            raise CapabilityError(
                "Current revocation catalog differs from rollout policy"
            )
        return current_catalog

    def _approval_posture_evidence(
        self, current_guards: dict[str, str]
    ) -> tuple[dict[str, Any], str]:
        evidence: dict[str, Any] = {
            "guard_hashes": current_guards,
            "matrix_approval_posture": self.approval_posture,
        }
        return evidence, canonical_json_hash(evidence)

    def _assert_release_not_revoked(
        self,
        source: GitSource,
        catalog: RevocationCatalog | None = None,
    ) -> None:
        selected = catalog if catalog is not None else self.revocation_catalog
        if selected is None:
            if self.enforce_freshness:
                raise CapabilityError("Current revocation catalog is unavailable")
            return
        revoked = matching_revocation(
            selected, self.capability_id, source
        )
        if revoked is not None:
            raise CapabilityError(
                "Refusing a release listed in the current revocation catalog"
            )

    def _validate_historical_policy(self, context: dict[str, Any]) -> None:
        for key in (
            "matrix_sha256",
            "registry_sha256",
            "scan_root_inventory_sha256",
        ):
            value = context.get(key)
            if not isinstance(value, str) or not HEX_64.fullmatch(value):
                raise CapabilityError(
                    f"Historical deployment policy field is invalid: {key}"
                )
        policy_commit = context.get("revocation_catalog_commit")
        if not isinstance(policy_commit, str) or not GIT_OID.fullmatch(
            policy_commit
        ):
            raise CapabilityError("Historical deployment policy commit is invalid")
        revocation_sha = context.get("revocation_catalog_sha256")
        if revocation_sha is not None and (
            not isinstance(revocation_sha, str)
            or not HEX_64.fullmatch(revocation_sha)
        ):
            raise CapabilityError(
                "Historical deployment revocation catalog hash is invalid"
            )
        if not isinstance(context.get("matrix_status"), str):
            raise CapabilityError("Historical deployment matrix status is invalid")
        validate_engine_identity(context.get("engine"), "Historical deployment")
        validate_git_identity(context.get("git_executable"), "Historical deployment")

    def _validate_state_layout(self) -> None:
        for path, label in (
            (self.target.state_root, "state root"),
            (self.target.operations_root, "operations root"),
            (self.target.quarantine_root, "quarantine root"),
        ):
            if not path.exists():
                continue
            plain = absolute_plain_path(path, must_exist=True)
            if not plain.is_dir():
                raise CapabilityError(f"Capability {label} is not a plain directory")
            if self.target.fixture_root is None and os.name != "nt":
                details = plain.stat()
                if details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) & 0o077:
                    raise CapabilityError(
                        f"Capability {label} must be owned by the runtime user and mode 0700"
                    )
        for path, label in (
            (self.target.lock_path, "lock"),
            (self.target.active_path, "active record"),
            (self.target.operation_lease_path, "operation lease"),
        ):
            if not path.exists():
                continue
            plain = absolute_plain_path(path, must_exist=True)
            if not stat.S_ISREG(plain.lstat().st_mode):
                raise CapabilityError(f"Capability {label} is not a plain file")
            if self.target.fixture_root is None and os.name != "nt":
                details = plain.stat()
                if details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) & 0o077:
                    raise CapabilityError(
                        f"Capability {label} must be owned by the runtime user and mode 0600"
                    )

    def _validate_active_shape(self, active: dict[str, Any]) -> str:
        if set(active) != ACTIVE_KEYS or active.get("schema_version") != 1:
            raise CapabilityError("Active-install record schema or keys are invalid")
        if active.get("capability_id") != self.capability_id:
            raise CapabilityError("Active-install capability_id mismatch")
        if active.get("tool") != self.target.spec.tool:
            raise CapabilityError("Active-install tool mismatch")
        transaction_id = active.get("deploy_transaction_id")
        if not isinstance(transaction_id, str) or not TRANSACTION_ID.fullmatch(
            transaction_id
        ):
            raise CapabilityError("Active-install transaction ID is invalid")
        if active.get("operation_kind") not in {"install", "update"}:
            raise CapabilityError("Active-install operation kind is invalid")
        if not isinstance(active.get("discovery_challenge"), str) or not HEX_64.fullmatch(
            active["discovery_challenge"]
        ):
            raise CapabilityError("Active-install discovery challenge is invalid")
        for key in ("context_sha256", "manifest_fingerprint", "payload_fingerprint"):
            if not isinstance(active.get(key), str) or not HEX_64.fullmatch(active[key]):
                raise CapabilityError(f"Active-install {key} is invalid")
        if not isinstance(active.get("scan_root_inventory_sha256"), str) or not HEX_64.fullmatch(
            active["scan_root_inventory_sha256"]
        ):
            raise CapabilityError("Active-install scan-root inventory hash is invalid")
        for key in ("source_commit", "source_tree"):
            if not isinstance(active.get(key), str) or not GIT_OID.fullmatch(active[key]):
                raise CapabilityError(f"Active-install {key} is invalid")
        updated_at = active.get("updated_at")
        if not isinstance(updated_at, str):
            raise CapabilityError("Active-install updated_at is invalid")
        parse_timestamp(updated_at, "active-install updated_at")
        return transaction_id

    def _validate_active_link(
        self, active: dict[str, Any], snapshot: dict[str, Any]
    ) -> None:
        transaction_id = self._validate_active_shape(active)
        operation = self._operation_dir(transaction_id)
        journal = read_json(operation / "journal.json")
        context = journal.get("context")
        if (
            journal.get("schema_version") != 1
            or journal.get("operation") != "deploy"
            or journal.get("transaction_id") != transaction_id
            or journal.get("state") != "committed"
            or not same_path(str(journal.get("operation_dir", "")), operation)
            or not isinstance(context, dict)
            or journal.get("context_sha256") != canonical_json_hash(context)
            or active["context_sha256"] != journal.get("context_sha256")
            or active["scan_root_inventory_sha256"]
            != journal.get("scan_root_inventory_sha256")
            or active["discovery_challenge"]
            != journal.get("discovery_challenge")
        ):
            raise CapabilityError("Active-install deploy journal link is invalid")
        activated_at = parse_timestamp(
            str(journal.get("activated_at", "")), "activated_at"
        )
        updated_at = parse_timestamp(str(active.get("updated_at", "")), "updated_at")
        if updated_at < activated_at:
            raise CapabilityError("Active-install record predates target activation")
        expected = {
            "capability_id": snapshot["capability_id"],
            "manifest_fingerprint": snapshot["manifest_fingerprint"],
            "operation_kind": context.get("operation_kind"),
            "payload_fingerprint": snapshot["payload_fingerprint"],
            "source_commit": snapshot["source_commit"],
            "source_tree": snapshot["source_tree"],
            "tool": self.target.spec.tool,
        }
        for key, value in expected.items():
            if active.get(key) != value or context.get(key) != value:
                raise CapabilityError(f"Active-install journal mismatch for {key}")
        if journal.get("marker") != snapshot.get("marker"):
            raise CapabilityError("Active-install marker differs from deploy journal")
        receipt = operation / "approval-receipt.json"
        if (
            not receipt.is_file()
            or not isinstance(journal.get("approval_sha256"), str)
            or sha256_bytes(receipt.read_bytes()) != journal["approval_sha256"]
        ):
            raise CapabilityError("Active-install deploy receipt link is invalid")

    def _source_and_context(
        self,
        *,
        allow_lock: bool = False,
        require_clean_worktree: bool = True,
    ) -> tuple[GitSource, dict[str, Any]]:
        if self.matrix_status == "blocked":
            raise CapabilityError("Blocked capability policy permits removal only")
        validate_paths(self.target)
        self._validate_state_layout()
        backups = backup_like_directories(self.target.scan_root)
        if backups:
            raise CapabilityError(
                f"Backup-like directories in scan root require owner cleanup: {backups}"
            )
        if self.target.lock_path.exists() and not allow_lock:
            raise CapabilityError("Target lock exists; recovery is required")
        current_catalog = self._validate_policy_snapshot()
        source = load_release(
            self.repo,
            self.commit,
            self.capability_id,
            require_clean_worktree=require_clean_worktree,
        )
        self._assert_release_not_revoked(source, current_catalog)
        manifest = source.capability_manifest or {}
        version = str(manifest["capability_version"])
        prior = snapshot_target(self.target)
        if prior["present"] and not self.target.active_path.exists():
            raise CapabilityError(
                "Managed target lacks an active-install record; migration is required"
            )
        if self.target.active_path.exists():
            active = read_json(self.target.active_path, "active install")
            if not prior["present"]:
                raise CapabilityError(
                    "Active-install record exists while capability target is absent"
                )
            self._validate_active_link(active, prior)
            expected_active = {
                "capability_id": prior["capability_id"],
                "manifest_fingerprint": prior["manifest_fingerprint"],
                "payload_fingerprint": prior["payload_fingerprint"],
                "source_commit": prior["source_commit"],
                "source_tree": prior["source_tree"],
                "tool": self.target.spec.tool,
            }
            for key, value in expected_active.items():
                if active.get(key) != value:
                    raise CapabilityError(
                        f"Active-install record does not match target for {key}"
                    )
        inventory_sha256 = scan_root_inventory(self.target.scan_root)
        matrix_inventory_sha256 = (
            scan_root_inventory(
                self.target.scan_root,
                exclude_name=self.target.target.name,
            )
            if prior["present"]
            else inventory_sha256
        )
        if matrix_inventory_sha256 != self.scan_root_inventory_sha256:
            raise CapabilityError(
                "Scan-root inventory differs from the approved capability matrix"
            )
        if prior["present"]:
            if (
                prior["source_commit"] == source.source_commit
                and prior["source_tree"] == source.source_tree
                and prior["payload_fingerprint"] == source.payload_fingerprint
                and prior["manifest_fingerprint"] == source.manifest_fingerprint
            ):
                operation_kind = "noop"
            else:
                if semver_key(version) <= semver_key(str(prior["capability_version"])):
                    raise CapabilityError("Update must increase capability_version")
                operation_kind = "update"
        else:
            operation_kind = "install"
        current_guards = guard_hashes(self.target)
        posture_evidence, posture_sha256 = self._approval_posture_evidence(
            current_guards
        )
        catalog = current_catalog
        context = {
            "activation_boundary": self.target.spec.activation_boundary,
            "approval_posture_evidence": posture_evidence,
            "approval_posture_sha256": posture_sha256,
            "capability_id": self.capability_id,
            "capability_version": version,
            "discovery_contract": self.target.spec.discovery_contract,
            "engine": engine_identity(self.engine_root),
            "git_executable": git_executable_identity(),
            "guard_hashes": current_guards,
            "manifest_fingerprint": source.manifest_fingerprint,
            "matrix_sha256": self.matrix_sha256,
            "matrix_status": self.matrix_status,
            "operation_kind": operation_kind,
            "payload_fingerprint": source.payload_fingerprint,
            "prior_state": prior,
            "registry_sha256": self.registry_sha256,
            "revocation_catalog_commit": self.policy_commit,
            "revocation_catalog_sha256": (
                catalog.raw_sha256 if catalog is not None else None
            ),
            "revocation_catalog_updated_at": (
                catalog.updated_at if catalog is not None else None
            ),
            "revocation_checked_at": self.revocation_checked_at,
            "rules_mutation": False,
            "risk_gate": self.target.spec.risk_gate,
            "robert_same_user_risk_accepted": self.robert_same_user_risk_accepted,
            "scope": self.target.spec.scope,
            "scan_root_inventory_sha256": inventory_sha256,
            "source_commit": source.source_commit,
            "source_tree": source.source_tree,
            "state_root": str(Path(os.path.abspath(self.target.state_root))),
            "target": str(Path(os.path.abspath(self.target.target))),
            "tool": self.target.spec.tool,
            "writes_scope": [
                str(Path(os.path.abspath(self.target.target))),
                str(Path(os.path.abspath(self.target.state_root))),
            ],
        }
        return source, context

    def _verified_active_state(self) -> tuple[
        GitSource,
        dict[str, Any],
        Path,
        dict[str, Any],
        dict[str, Any],
    ]:
        validate_paths(self.target)
        self._validate_state_layout()
        backups = backup_like_directories(self.target.scan_root)
        if backups:
            raise CapabilityError(
                f"Backup-like directories in scan root require owner cleanup: {backups}"
            )
        if self.target.lock_path.exists():
            raise CapabilityError("Target lock exists; current-state verification refused")
        current_catalog = self._validate_policy_snapshot()
        source = load_release(
            self.repo,
            self.commit,
            self.capability_id,
            require_clean_worktree=False,
        )
        self._assert_release_not_revoked(source, current_catalog)
        active, operation, journal, deployed_context = self._load_active_deploy(
            source
        )
        self._validate_historical_policy(deployed_context)
        current_guards = guard_hashes(self.target)
        if deployed_context.get("guard_hashes") != current_guards:
            raise CapabilityError("Runtime guard files drifted since deployment")
        posture_evidence, posture_sha256 = self._approval_posture_evidence(
            current_guards
        )
        expected_context_values = {
            "approval_posture_evidence": posture_evidence,
            "approval_posture_sha256": posture_sha256,
            "matrix_sha256": self.matrix_sha256,
            "registry_sha256": self.registry_sha256,
        }
        for key, value in expected_context_values.items():
            if deployed_context.get(key) != value:
                raise CapabilityError(f"Active deployment context drift for {key}")
        current_inventory = scan_root_inventory(self.target.scan_root)
        if current_inventory != active["scan_root_inventory_sha256"]:
            raise CapabilityError("Scan-root inventory drifted since deployment")
        return source, active, operation, journal, deployed_context

    def _matching_prior_revocation(
        self,
        prior: dict[str, Any],
        catalog: RevocationCatalog | None = None,
    ) -> dict[str, str] | None:
        selected = catalog if catalog is not None else self.revocation_catalog
        if not prior.get("present") or selected is None:
            return None
        expected = (
            self.capability_id,
            prior.get("source_tree"),
            prior.get("manifest_fingerprint"),
            prior.get("payload_fingerprint"),
        )
        for row in selected.entries:
            identity = (
                row["capability_id"],
                row["source_tree"],
                row["manifest_fingerprint"],
                row["payload_fingerprint"],
            )
            if identity == expected:
                return dict(row)
        return None

    def _validate_rollback_authorization_shape(
        self,
        authorization: dict[str, Any],
        deploy_context: dict[str, Any],
        deploy_transaction_id: str,
    ) -> None:
        if (
            set(authorization) != ROLLBACK_AUTHORIZATION_KEYS
            or authorization.get("schema_version") != 1
            or authorization.get("authorization_scope")
            != "post-activation-rollback"
            or authorization.get("deploy_context") != deploy_context
            or authorization.get("deploy_context_sha256")
            != canonical_json_hash(deploy_context)
            or authorization.get("deploy_transaction_id")
            != deploy_transaction_id
        ):
            raise CapabilityError("Rollback authorization context is invalid")
        expected = {
            "capability_id": self.capability_id,
            "risk_gate": self.target.spec.risk_gate,
            "state_root": str(Path(os.path.abspath(self.target.state_root))),
            "target": str(Path(os.path.abspath(self.target.target))),
            "tool": self.target.spec.tool,
            "writes_scope": deploy_context.get("writes_scope"),
        }
        for key, value in expected.items():
            if authorization.get(key) != value:
                raise CapabilityError(
                    f"Rollback authorization context mismatch for {key}"
                )
        prior = deploy_context.get("prior_state")
        prior_revocation = authorization.get("prior_release_revocation")
        if prior_revocation is not None:
            if (
                not isinstance(prior, dict)
                or prior.get("present") is not True
                or not isinstance(prior_revocation, dict)
                or set(prior_revocation) != REVOCATION_ENTRY_KEYS
                or prior_revocation.get("capability_id") != self.capability_id
                or prior_revocation.get("source_tree") != prior.get("source_tree")
                or prior_revocation.get("manifest_fingerprint")
                != prior.get("manifest_fingerprint")
                or prior_revocation.get("payload_fingerprint")
                != prior.get("payload_fingerprint")
            ):
                raise CapabilityError(
                    "Rollback prior release revocation evidence is invalid"
                )
        active_revocation = authorization.get("active_release_revocation")
        if active_revocation is not None:
            if (
                not isinstance(active_revocation, dict)
                or set(active_revocation) != REVOCATION_ENTRY_KEYS
                or active_revocation.get("capability_id") != self.capability_id
                or active_revocation.get("source_tree")
                != deploy_context.get("source_tree")
                or active_revocation.get("manifest_fingerprint")
                != deploy_context.get("manifest_fingerprint")
                or active_revocation.get("payload_fingerprint")
                != deploy_context.get("payload_fingerprint")
            ):
                raise CapabilityError("Active release revocation evidence is invalid")
            for key in ("source_commit", "source_tree"):
                value = active_revocation.get(key)
                if not isinstance(value, str) or not GIT_OID.fullmatch(value):
                    raise CapabilityError(
                        f"Active release revocation {key} is invalid"
                    )
            for key in ("manifest_fingerprint", "payload_fingerprint"):
                value = active_revocation.get(key)
                if not isinstance(value, str) or not HEX_64.fullmatch(value):
                    raise CapabilityError(
                        f"Active release revocation {key} is invalid"
                    )
            reason = active_revocation.get("reason")
            if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
                raise CapabilityError("Active release revocation reason is invalid")
            parse_timestamp(
                str(active_revocation.get("revoked_at", "")),
                "active release revoked_at",
            )
        validate_engine_identity(
            authorization.get("engine"), "Rollback authorization"
        )
        validate_git_identity(
            authorization.get("git_executable"), "Rollback authorization"
        )
        guard_values = authorization.get("guard_hashes")
        if (
            not isinstance(guard_values, dict)
            or any(
                not isinstance(key, str)
                or not isinstance(value, str)
                or not HEX_64.fullmatch(value)
                for key, value in guard_values.items()
            )
        ):
            raise CapabilityError("Rollback authorization guard hashes are invalid")
        posture = authorization.get("approval_posture_evidence")
        if (
            not isinstance(posture, dict)
            or authorization.get("approval_posture_sha256")
            != canonical_json_hash(posture)
        ):
            raise CapabilityError("Rollback authorization posture is invalid")
        for key in (
            "matrix_scan_root_inventory_sha256",
            "matrix_sha256",
            "registry_sha256",
            "revocation_catalog_sha256",
            "scan_root_inventory_sha256",
        ):
            value = authorization.get(key)
            if not isinstance(value, str) or not HEX_64.fullmatch(value):
                raise CapabilityError(f"Rollback authorization {key} is invalid")
        if (
            not isinstance(authorization.get("policy_commit"), str)
            or not GIT_OID.fullmatch(authorization["policy_commit"])
            or not isinstance(authorization.get("matrix_status"), str)
            or not isinstance(
                authorization.get("robert_same_user_risk_accepted"), bool
            )
        ):
            raise CapabilityError("Rollback authorization policy fields are invalid")
        parse_timestamp(
            str(authorization.get("revocation_catalog_updated_at", "")),
            "rollback revocation catalog updated_at",
        )
        parse_timestamp(
            str(authorization.get("revocation_checked_at", "")),
            "rollback revocation checked_at",
        )

    def _validate_rollback_authorization_policy(
        self,
        authorization: dict[str, Any],
        *,
        require_freshness_match: bool = True,
    ) -> None:
        expected = {
            "matrix_scan_root_inventory_sha256": self.scan_root_inventory_sha256,
            "matrix_sha256": self.matrix_sha256,
            "matrix_status": self.matrix_status,
            "policy_commit": self.policy_commit,
            "registry_sha256": self.registry_sha256,
            "revocation_catalog_sha256": (
                self.revocation_catalog.raw_sha256
                if self.revocation_catalog is not None
                else None
            ),
            "revocation_catalog_updated_at": (
                self.revocation_catalog.updated_at
                if self.revocation_catalog is not None
                else None
            ),
            "robert_same_user_risk_accepted": self.robert_same_user_risk_accepted,
        }
        if require_freshness_match:
            expected["revocation_checked_at"] = self.revocation_checked_at
        for key, value in expected.items():
            if authorization.get(key) != value:
                raise CapabilityError(
                    f"Rollback authorization policy mismatch for {key}"
                )

    def _rollback_authorization_context(
        self,
        source: GitSource,
        active: dict[str, Any],
        deploy_context: dict[str, Any],
        deploy_transaction_id: str,
        catalog: RevocationCatalog | None = None,
    ) -> dict[str, Any]:
        prior = deploy_context.get("prior_state")
        if not isinstance(prior, dict) or not isinstance(prior.get("present"), bool):
            raise CapabilityError("Rollback deploy prior state is invalid")
        selected_catalog = (
            catalog if catalog is not None else self.revocation_catalog
        )
        prior_revocation = self._matching_prior_revocation(
            prior, selected_catalog
        )
        current_guards = guard_hashes(self.target)
        posture_evidence, posture_sha256 = self._approval_posture_evidence(
            current_guards
        )
        current_inventory = scan_root_inventory(self.target.scan_root)
        if current_inventory != active.get("scan_root_inventory_sha256"):
            raise CapabilityError("Rollback scan-root inventory drifted")
        authorization = {
            "active_release_revocation": (
                matching_revocation(
                    selected_catalog,
                    self.capability_id,
                    source,
                )
                if selected_catalog is not None
                else None
            ),
            "approval_posture_evidence": posture_evidence,
            "approval_posture_sha256": posture_sha256,
            "authorization_scope": "post-activation-rollback",
            "capability_id": self.capability_id,
            "deploy_context": deploy_context,
            "deploy_context_sha256": canonical_json_hash(deploy_context),
            "deploy_transaction_id": deploy_transaction_id,
            "engine": engine_identity(self.engine_root),
            "git_executable": git_executable_identity(),
            "guard_hashes": current_guards,
            "matrix_scan_root_inventory_sha256": (
                self.scan_root_inventory_sha256
            ),
            "matrix_sha256": self.matrix_sha256,
            "matrix_status": self.matrix_status,
            "policy_commit": self.policy_commit,
            "prior_release_revocation": prior_revocation,
            "registry_sha256": self.registry_sha256,
            "revocation_catalog_sha256": (
                self.revocation_catalog.raw_sha256
                if self.revocation_catalog is not None
                else None
            ),
            "revocation_catalog_updated_at": (
                self.revocation_catalog.updated_at
                if self.revocation_catalog is not None
                else None
            ),
            "revocation_checked_at": self.revocation_checked_at,
            "risk_gate": self.target.spec.risk_gate,
            "robert_same_user_risk_accepted": (
                self.robert_same_user_risk_accepted
            ),
            "scan_root_inventory_sha256": current_inventory,
            "schema_version": 1,
            "state_root": str(Path(os.path.abspath(self.target.state_root))),
            "target": str(Path(os.path.abspath(self.target.target))),
            "tool": self.target.spec.tool,
            "writes_scope": deploy_context.get("writes_scope"),
        }
        self._validate_rollback_authorization_shape(
            authorization, deploy_context, deploy_transaction_id
        )
        self._validate_rollback_authorization_policy(authorization)
        return authorization

    def _rollback_state(
        self, *, allow_lock: bool = False
    ) -> tuple[
        GitSource,
        dict[str, Any],
        Path,
        dict[str, Any],
        dict[str, Any],
        dict[str, Any],
    ]:
        validate_paths(self.target)
        self._validate_state_layout()
        backups = backup_like_directories(self.target.scan_root)
        if backups:
            raise CapabilityError(
                f"Backup-like directories in scan root require owner cleanup: {backups}"
            )
        if self.target.lock_path.exists() and not allow_lock:
            raise CapabilityError("Target lock exists; rollback planning refused")
        current_catalog = self._validate_policy_snapshot()
        source = load_release(
            self.repo,
            self.commit,
            self.capability_id,
            require_clean_worktree=False,
        )
        active, operation, journal, deploy_context = self._load_active_deploy(
            source
        )
        deploy_transaction_id = str(active["deploy_transaction_id"])
        authorization = self._rollback_authorization_context(
            source,
            active,
            deploy_context,
            deploy_transaction_id,
            current_catalog,
        )
        return (
            source,
            active,
            operation,
            journal,
            deploy_context,
            authorization,
        )

    def _rollback_action_from_approval(
        self, approval: dict[str, Any]
    ) -> str:
        actions = approval.get("authorized_actions")
        if actions == ["recover-rollback", "rollback"]:
            return "rollback"
        if actions == ["emergency-rollback", "recover-rollback"]:
            return "emergency-rollback"
        raise CapabilityError("Rollback approval action scope is invalid")

    def _rollback_action_for_lock(
        self,
        lock: dict[str, Any],
        approval: dict[str, Any],
        journal: dict[str, Any] | None = None,
    ) -> str:
        inferred = self._rollback_action_from_approval(approval)
        requested = lock.get("requested_action")
        if requested is not None and requested != inferred:
            raise CapabilityError("Rollback lock requested action differs")
        if journal is not None:
            journal_action = journal.get("mutation_action")
            if journal_action is not None and journal_action != inferred:
                raise CapabilityError("Rollback journal action differs from approval")
        return inferred

    def _rollback_contexts_from_lock(
        self, lock: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        approval_context = lock.get("context")
        if not isinstance(approval_context, dict):
            raise CapabilityError("Rollback lock context is invalid")
        if set(approval_context) == ROLLBACK_AUTHORIZATION_KEYS:
            deploy_context = approval_context.get("deploy_context")
            deploy_id = str(
                approval_context.get("deploy_transaction_id", "")
            )
            if not isinstance(deploy_context, dict):
                raise CapabilityError("Rollback lock deploy context is invalid")
            self._validate_rollback_authorization_shape(
                approval_context, deploy_context, deploy_id
            )
            return deploy_context, approval_context
        self._validate_historical_policy(approval_context)
        return approval_context, approval_context

    def _rollback_contexts_from_journal(
        self, journal: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        deploy_context = journal.get("context")
        if not isinstance(deploy_context, dict):
            raise CapabilityError("Rollback journal deploy context is invalid")
        approval_context = journal.get("approval_context")
        approval_context_sha = journal.get("approval_context_sha256")
        if approval_context is None and approval_context_sha is None:
            self._validate_historical_policy(deploy_context)
            return deploy_context, deploy_context
        if (
            not isinstance(approval_context, dict)
            or approval_context_sha != canonical_json_hash(approval_context)
        ):
            raise CapabilityError("Rollback journal approval context is invalid")
        self._validate_rollback_authorization_shape(
            approval_context,
            deploy_context,
            str(approval_context.get("deploy_transaction_id", "")),
        )
        return deploy_context, approval_context

    def plan(self) -> dict[str, Any]:
        source, context = self._source_and_context()
        status = "CURRENT" if context["operation_kind"] == "noop" else "READY"
        return {
            "action": "plan",
            "status": status,
            "source_entry_count": len(source.entries),
            "approval_context": context,
            "approval_context_sha256": canonical_json_hash(context),
            "writes_performed": 0,
            "security_statement": SECURITY_STATEMENT,
        }

    def _discovery_context(
        self,
        deploy_context: dict[str, Any],
        transaction_id: str,
        evidence_raw: bytes,
    ) -> dict[str, Any]:
        context = dict(deploy_context)
        context.update(
            {
                "discovery_approval_scope": "record-discovery",
                "discovery_deploy_context_sha256": canonical_json_hash(
                    deploy_context
                ),
                "discovery_deploy_transaction_id": transaction_id,
                "discovery_evidence_sha256": sha256_bytes(evidence_raw),
            }
        )
        return context

    def plan_discovery(self, evidence_path: Path | str) -> dict[str, Any]:
        source, active, _, journal, deploy_context = self._verified_active_state()
        transaction_id = str(active["deploy_transaction_id"])
        _, evidence_raw = load_discovery_evidence(
            evidence_path,
            target=self.target,
            source=source,
            context=deploy_context,
            deploy_transaction_id=transaction_id,
            discovery_challenge=str(active["discovery_challenge"]),
            activated_at=str(journal.get("activated_at", "")),
        )
        context = self._discovery_context(
            deploy_context, transaction_id, evidence_raw
        )
        return {
            "action": "plan-discovery",
            "status": "READY",
            "approval_context": context,
            "approval_context_sha256": canonical_json_hash(context),
            "deploy_transaction_id": transaction_id,
            "discovery_evidence_sha256": sha256_bytes(evidence_raw),
            "writes_performed": 0,
            "security_statement": SECURITY_STATEMENT,
        }

    def _discovery_status(
        self,
        operation: Path,
        source: GitSource,
        deploy_context: dict[str, Any],
        transaction_id: str,
        deploy_journal: dict[str, Any],
    ) -> dict[str, Any]:
        evidence_path = operation / "discovery-evidence.json"
        approval_path = operation / "discovery-approval-receipt.json"
        record_path = operation / "discovery-record.json"
        present = [path.exists() for path in (evidence_path, approval_path, record_path)]
        if not any(present):
            return {"status": "REQUIRED"}
        if not all(present):
            raise CapabilityError("Discovery evidence record is incomplete")
        _, evidence_raw = load_discovery_evidence(
            evidence_path,
            target=self.target,
            source=source,
            context=deploy_context,
            deploy_transaction_id=transaction_id,
            discovery_challenge=str(deploy_journal.get("discovery_challenge", "")),
            activated_at=str(deploy_journal.get("activated_at", "")),
            allow_state_evidence=True,
        )
        context = self._discovery_context(
            deploy_context, transaction_id, evidence_raw
        )
        approval, _, approval_sha256 = load_approval(
            approval_path,
            context=context,
            required_action="record-discovery",
            target=self.target,
            allow_expired=True,
            allow_state_receipt=True,
        )
        record = read_json(record_path, "discovery record")
        if (
            set(record) != DISCOVERY_RECORD_KEYS
            or record.get("schema_version") != 1
            or record.get("approval_id") != approval.get("approval_id")
            or record.get("approval_sha256") != approval_sha256
            or record.get("context_sha256") != canonical_json_hash(context)
            or record.get("deploy_transaction_id") != transaction_id
            or record.get("evidence_sha256") != sha256_bytes(evidence_raw)
        ):
            raise CapabilityError("Discovery record identity or integrity is invalid")
        parse_timestamp(str(record.get("recorded_at", "")), "recorded_at")
        return {
            "approval_id": record["approval_id"],
            "context_sha256": record["context_sha256"],
            "evidence_sha256": record["evidence_sha256"],
            "recorded_at": record["recorded_at"],
            "status": "RECORDED",
        }

    def record_discovery(
        self,
        approval_path: Path | str,
        evidence_path: Path | str,
        *,
        owner_authorized: bool,
        _lease_token: object | None = None,
    ) -> dict[str, Any]:
        if not owner_authorized:
            raise CapabilityError("Discovery recording requires owner authorization")
        source, active, operation, journal, deploy_context = self._verified_active_state()
        transaction_id = str(active["deploy_transaction_id"])
        _, evidence_raw = load_discovery_evidence(
            evidence_path,
            target=self.target,
            source=source,
            context=deploy_context,
            deploy_transaction_id=transaction_id,
            discovery_challenge=str(active["discovery_challenge"]),
            activated_at=str(journal.get("activated_at", "")),
        )
        context = self._discovery_context(
            deploy_context, transaction_id, evidence_raw
        )
        approval, approval_raw, approval_sha256 = load_approval(
            approval_path,
            context=context,
            required_action="record-discovery",
            target=self.target,
        )
        if _lease_token is not self._lease_token:
            with self._operation_lease():
                return self.record_discovery(
                    approval_path,
                    evidence_path,
                    owner_authorized=owner_authorized,
                    _lease_token=self._lease_token,
                )
        state_paths = (
            operation / "discovery-evidence.json",
            operation / "discovery-approval-receipt.json",
            operation / "discovery-record.json",
        )
        if all(path.exists() for path in state_paths):
            existing = self._discovery_status(
                operation, source, deploy_context, transaction_id, journal
            )
            if (
                existing["approval_id"] != approval["approval_id"]
                or existing["evidence_sha256"] != sha256_bytes(evidence_raw)
                or existing["context_sha256"] != canonical_json_hash(context)
            ):
                raise CapabilityError("A different discovery record already exists")
            return {
                "action": "record-discovery",
                "status": "RECORDED",
                "deploy_transaction_id": transaction_id,
                "evidence_sha256": existing["evidence_sha256"],
                "target": str(self.target.target),
                "writes_scope": "none-already-recorded",
            }
        record_path = operation / "discovery-record.json"
        recorded_at = utc_now()
        if record_path.exists():
            partial_record = read_json(record_path, "discovery record")
            recorded_at = str(partial_record.get("recorded_at", ""))
            parse_timestamp(recorded_at, "recorded_at")
        record = {
            "approval_id": approval["approval_id"],
            "approval_sha256": approval_sha256,
            "context_sha256": canonical_json_hash(context),
            "deploy_transaction_id": transaction_id,
            "evidence_sha256": sha256_bytes(evidence_raw),
            "recorded_at": recorded_at,
            "schema_version": 1,
        }
        immutable_write_bytes(
            operation / "discovery-evidence.json",
            evidence_raw,
            "discovery evidence",
        )
        immutable_write_bytes(
            operation / "discovery-approval-receipt.json",
            approval_raw,
            "discovery approval receipt",
        )
        immutable_write_bytes(
            record_path,
            canonical_json_bytes(record),
            "discovery record",
        )
        status = self._discovery_status(
            operation, source, deploy_context, transaction_id, journal
        )
        return {
            "action": "record-discovery",
            "status": status["status"],
            "deploy_transaction_id": transaction_id,
            "evidence_sha256": status["evidence_sha256"],
            "target": str(self.target.target),
            "writes_scope": "deploy-operation-state-only",
        }

    def verify(self) -> dict[str, Any]:
        source, active, operation, journal, deployed_context = (
            self._verified_active_state()
        )
        discovery = self._discovery_status(
            operation,
            source,
            deployed_context,
            str(active["deploy_transaction_id"]),
            journal,
        )
        return {
            "action": "verify",
            "status": "CURRENT",
            "active_record": str(self.target.active_path),
            "deploy_operation": str(operation),
            "deploy_transaction_id": active["deploy_transaction_id"],
            "discovery": discovery,
            "journal_state": journal["state"],
            "manifest_fingerprint": source.manifest_fingerprint,
            "marker_schema": 2,
            "payload_fingerprint": source.payload_fingerprint,
            "registry_sha256": self.registry_sha256,
            "matrix_sha256": self.matrix_sha256,
            "policy_commit": self.policy_commit,
            "revocation_catalog_sha256": (
                self.revocation_catalog.raw_sha256
                if self.revocation_catalog is not None
                else None
            ),
            "revocation_checked_at": self.revocation_checked_at,
            "source_commit": source.source_commit,
            "source_tree": source.source_tree,
            "source_matches_installed": (
                deployed_context["source_commit"] == source.source_commit
                and deployed_context["source_tree"] == source.source_tree
            ),
            "target": str(self.target.target),
            "tool": self.target.spec.tool,
            "writes_performed": 0,
            "security_statement": SECURITY_STATEMENT,
        }

    def _create_state_roots(self) -> None:
        self.target.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.target.operations_root.mkdir(exist_ok=True, mode=0o700)
        self.target.quarantine_root.mkdir(exist_ok=True, mode=0o700)
        if self.target.fixture_root is None and os.name != "nt":
            for path in (
                self.target.state_root,
                self.target.operations_root,
                self.target.quarantine_root,
            ):
                os.chmod(path, 0o700)
        if not self.target.operation_lease_path.exists():
            immutable_write_bytes(
                self.target.operation_lease_path,
                b"\x00",
                "operation lease",
            )
        self._validate_state_layout()

    @contextlib.contextmanager
    def _operation_lease(self) -> Any:
        self._validate_state_layout()
        if not self.target.operation_lease_path.is_file():
            raise CapabilityError("Operation lease file is missing")
        handle = self.target.operation_lease_path.open("r+b")
        locked = False
        try:
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError as exc:
                raise CapabilityError(
                    "Another live mutation or recovery holds the operation lease"
                ) from exc
            handle.seek(0)
            if handle.read() != b"\x00":
                raise CapabilityError("Operation lease file content is invalid")
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

    def _operation_dir(self, transaction_id: str) -> Path:
        if not TRANSACTION_ID.fullmatch(transaction_id):
            raise CapabilityError("Invalid transaction ID")
        return self.target.operations_root / transaction_id

    def _lock_intent_path(self, transaction_id: str) -> Path:
        if not TRANSACTION_ID.fullmatch(transaction_id):
            raise CapabilityError("Invalid transaction ID")
        return self.target.state_root / f".target-lock-intent-{transaction_id}.json"

    def _publish_lock_intent(self, temporary: Path) -> None:
        atomic_publish_no_replace(temporary, self.target.lock_path)

    def _acquire(
        self,
        transaction_id: str,
        operation: str,
        *,
        approval_id: str,
        approval_sha256: str,
        context: dict[str, Any],
        requested_action: str | None = None,
    ) -> None:
        context_sha256 = canonical_json_hash(context)
        action = requested_action or operation
        if operation == "deploy" and action != "deploy":
            raise CapabilityError("Deploy lock requested action is invalid")
        if operation == "rollback" and action not in {
            "rollback",
            "emergency-rollback",
        }:
            raise CapabilityError("Rollback lock requested action is invalid")
        data = {
            "approval_id": approval_id,
            "approval_sha256": approval_sha256,
            "context": context,
            "context_sha256": context_sha256,
            "created_at": utc_now(),
            "operation": operation,
            "pid": os.getpid(),
            "requested_action": action,
            "transaction_id": transaction_id,
        }
        raw = canonical_json_bytes(data)
        temporary = self._lock_intent_path(transaction_id)
        created = False
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError as exc:
            raise CapabilityError(
                "Transaction lock intent already exists; owner review is required"
            ) from exc
        created = True
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            if os.name != "nt":
                os.chmod(temporary, 0o600)
            fsync_parent(temporary)
            try:
                self._publish_lock_intent(temporary)
            except FileExistsError as exc:
                raise CapabilityError(
                    "Per-target lock exists; automatic stale-lock removal is forbidden"
                ) from exc
        finally:
            if created and temporary.exists():
                temporary.unlink()
                fsync_parent(temporary)

    def _read_lock(self) -> dict[str, Any]:
        lock = read_json(self.target.lock_path, "lock")
        lock_keys = frozenset(lock)
        if lock_keys not in {frozenset(LOCK_KEYS), frozenset(LEGACY_LOCK_KEYS)}:
            raise CapabilityError("Capability lock keys are invalid")
        transaction_id = lock.get("transaction_id")
        if not isinstance(transaction_id, str) or not TRANSACTION_ID.fullmatch(
            transaction_id
        ):
            raise CapabilityError("Capability lock transaction ID is invalid")
        if lock.get("operation") not in {"deploy", "rollback"}:
            raise CapabilityError("Capability lock operation is invalid")
        if lock_keys == frozenset(LOCK_KEYS):
            requested_action = lock.get("requested_action")
            if (
                lock.get("operation") == "deploy"
                and requested_action != "deploy"
            ) or (
                lock.get("operation") == "rollback"
                and requested_action not in {"rollback", "emergency-rollback"}
            ):
                raise CapabilityError("Capability lock requested action is invalid")
        if not isinstance(lock.get("approval_id"), str) or not APPROVAL_ID.fullmatch(
            lock["approval_id"]
        ):
            raise CapabilityError("Capability lock approval ID is invalid")
        for key in ("approval_sha256", "context_sha256"):
            if not isinstance(lock.get(key), str) or not HEX_64.fullmatch(lock[key]):
                raise CapabilityError(f"Capability lock {key} is invalid")
        context = lock.get("context")
        if (
            not isinstance(context, dict)
            or lock.get("context_sha256") != canonical_json_hash(context)
        ):
            raise CapabilityError("Capability lock context is invalid")
        parse_timestamp(str(lock.get("created_at", "")), "lock created_at")
        if not isinstance(lock.get("pid"), int) or lock["pid"] <= 0:
            raise CapabilityError("Capability lock PID is invalid")
        if self.target.lock_path.read_bytes() != canonical_json_bytes(lock):
            raise CapabilityError("Capability lock is not canonical JSON")
        return lock

    def _release(self, transaction_id: str) -> None:
        lock = self._read_lock()
        if lock.get("transaction_id") != transaction_id:
            raise CapabilityError("Refusing to release another transaction's lock")
        intent = self._lock_intent_path(transaction_id)
        if intent.exists():
            plain = absolute_plain_path(intent, must_exist=True)
            if not stat.S_ISREG(plain.lstat().st_mode):
                raise CapabilityError("Published lock intent is not a plain file")
            if plain.read_bytes() != canonical_json_bytes(lock):
                raise CapabilityError("Published lock intent differs from active lock")
        self.target.lock_path.unlink()
        if intent.exists():
            intent.unlink()
        fsync_parent(self.target.lock_path)

    def _validate_lock_intent(
        self,
        lock: dict[str, Any],
        approval: dict[str, Any],
        approval_sha256: str,
        context: dict[str, Any],
    ) -> None:
        if (
            lock.get("approval_id") != approval.get("approval_id")
            or lock.get("approval_sha256") != approval_sha256
            or lock.get("context") != context
            or lock.get("context_sha256") != canonical_json_hash(context)
        ):
            raise CapabilityError("Lock-only recovery intent does not match approval")

    def _validate_terminal_result_shape(
        self,
        action: str,
        result: dict[str, Any],
        transaction_id: str,
    ) -> None:
        status = result.get("status")
        expected_keys = TERMINAL_RESULT_KEYS.get((action, str(status)))
        if (
            expected_keys is None
            or set(result) != expected_keys
            or result.get("action") != action
            or result.get("transaction_id") != transaction_id
        ):
            raise CapabilityError("Terminal result schema or identity is invalid")
        if action in {"deploy", "emergency-rollback", "rollback"} and result.get(
            "rules_or_config_mutation"
        ) is not False:
            raise CapabilityError("Terminal result rules mutation flag is invalid")
        if status in {
            "ABORTED_NO_WRITES",
            "ABORTED_PRECONDITION_DRIFT",
            "REMOVED_WITHOUT_RESTORE",
            "ROLLED_BACK",
        } and not isinstance(
            result.get("target_present"), bool
        ):
            raise CapabilityError("Terminal result target presence is invalid")
        if status in {
            "ABORTED_NO_WRITES",
            "ABORTED_PRECONDITION_DRIFT",
        } and result.get("writes_scope") != "transaction-state-only":
            raise CapabilityError("Lock-only terminal result write scope is invalid")
        if status == "ABORTED_PRECONDITION_DRIFT" and result.get(
            "reason"
        ) not in {
            "discovery-drift",
            "guard-drift",
            "scan-root-inventory-drift",
        }:
            raise CapabilityError("Rollback recovery drift reason is invalid")

    def _validate_prior_active_restored(
        self,
        deploy_operation: Path,
        deploy_journal: dict[str, Any],
        deploy_transaction_id: str,
        prior: dict[str, Any],
    ) -> None:
        expected_hash = deploy_journal.get("prior_active_sha256")
        prior_active_path = deploy_operation / "prior-active.json"
        if expected_hash is None:
            if self.target.active_path.exists():
                raise CapabilityError("Restored state unexpectedly has an active record")
            return
        if not isinstance(expected_hash, str) or not HEX_64.fullmatch(expected_hash):
            raise CapabilityError("Prior active-install hash is invalid")
        if not prior_active_path.is_file():
            raise CapabilityError("Prior active-install evidence is missing")
        raw = prior_active_path.read_bytes()
        if sha256_bytes(raw) != expected_hash:
            raise CapabilityError("Prior active-install evidence changed")
        if not self.target.active_path.is_file():
            raise CapabilityError("Prior active-install record was not restored")
        if self.target.active_path.read_bytes() != raw:
            raise CapabilityError("Restored active-install record differs")
        active = read_json(self.target.active_path, "active install")
        if active.get("deploy_transaction_id") == deploy_transaction_id:
            raise CapabilityError("Restored active-install still points at rolled state")
        self._validate_active_link(active, prior)

    def _validate_restored_scan_inventory(
        self, context: dict[str, Any], prior: dict[str, Any]
    ) -> None:
        if prior.get("present"):
            active = read_json(self.target.active_path, "active install")
            expected = active.get("scan_root_inventory_sha256")
        else:
            expected = context.get("scan_root_inventory_sha256")
        if (
            not isinstance(expected, str)
            or not HEX_64.fullmatch(expected)
            or scan_root_inventory(self.target.scan_root) != expected
        ):
            raise CapabilityError("Restored scan-root inventory differs")

    def _validate_terminal_state(
        self,
        operation: Path,
        *,
        action: str,
        result: dict[str, Any],
        approval_path: Path,
    ) -> None:
        transaction_id = operation.name
        self._validate_terminal_result_shape(action, result, transaction_id)
        journal = read_json(operation / "journal.json")
        base_operation = "deploy" if action in {"deploy", "recover-deploy"} else "rollback"
        context = journal.get("context")
        if (
            journal.get("schema_version") != 1
            or journal.get("operation") != base_operation
            or journal.get("transaction_id") != transaction_id
            or not same_path(str(journal.get("operation_dir", "")), operation)
            or not isinstance(context, dict)
            or journal.get("context_sha256") != canonical_json_hash(context)
        ):
            raise CapabilityError("Terminal journal identity or context is invalid")
        if action in {"rollback", "emergency-rollback"} and journal.get(
            "mutation_action"
        ) != action:
            raise CapabilityError("Rollback terminal action differs from journal")
        if action == "recover-rollback" and journal.get("state") != "aborted_no_writes":
            if journal.get("mutation_action") not in {
                "rollback",
                "emergency-rollback",
            }:
                raise CapabilityError("Rollback recovery journal action is invalid")
        approval_context = context
        if base_operation == "rollback":
            stored_authorization = journal.get("approval_context")
            stored_authorization_sha = journal.get("approval_context_sha256")
            if stored_authorization is not None or stored_authorization_sha is not None:
                if (
                    not isinstance(stored_authorization, dict)
                    or stored_authorization_sha
                    != canonical_json_hash(stored_authorization)
                ):
                    raise CapabilityError(
                        "Rollback journal approval context is invalid"
                    )
                approval_context = stored_authorization
                self._validate_rollback_authorization_shape(
                    approval_context,
                    context,
                    str(approval_context.get("deploy_transaction_id", "")),
                )
            else:
                self._validate_historical_policy(context)
        else:
            self._validate_historical_policy(context)
        approval, approval_raw, approval_sha256 = load_approval(
            approval_path,
            context=approval_context,
            required_action=action,
            target=self.target,
            allow_expired=True,
        )
        stored = operation / "approval-receipt.json"
        if (
            journal.get("approval_id") != approval.get("approval_id")
            or journal.get("approval_sha256") != approval_sha256
            or not stored.is_file()
            or stored.read_bytes() != approval_raw
        ):
            raise CapabilityError("Terminal journal approval link is invalid")
        source = load_release(
            self.repo,
            self.commit,
            self.capability_id,
            require_clean_worktree=False,
        )
        status = str(result["status"])
        if status in {
            "ABORTED_NO_WRITES",
            "ABORTED_PRECONDITION_DRIFT",
        }:
            allowed = {
                "approval-receipt.json",
                "journal.json",
                "recovery-result.json",
            }
            expected_state = (
                "aborted_no_writes"
                if status == "ABORTED_NO_WRITES"
                else "recovery_aborted_precondition_drift"
            )
            if journal.get("state") != expected_state or any(
                item.name not in allowed
                and not allowed_transaction_temporary(item.name, allowed)
                for item in operation.iterdir()
            ):
                raise CapabilityError("Lock-only abort state is invalid")
            if action == "recover-deploy":
                prior = context.get("prior_state")
                if not isinstance(prior, dict) or not self._snapshot_matches(prior):
                    raise CapabilityError("Lock-only deploy abort changed target state")
                if prior.get("present"):
                    if not self.target.active_path.is_file():
                        raise CapabilityError("Lock-only deploy abort lost active state")
                    self._validate_active_link(
                        read_json(self.target.active_path, "active install"), prior
                    )
                elif self.target.active_path.exists():
                    raise CapabilityError("Lock-only deploy abort created active state")
                self._validate_restored_scan_inventory(context, prior)
            else:
                active, active_operation, _, active_context = (
                    self._load_active_deploy(source)
                )
                if (
                    active_context != context
                    or active_operation.name
                    != str(active.get("deploy_transaction_id", ""))
                ):
                    raise CapabilityError("Lock-only rollback abort changed active state")
                if status == "ABORTED_PRECONDITION_DRIFT" and journal.get(
                    "recovery_abort_reason"
                ) != result.get("reason"):
                    raise CapabilityError(
                        "Rollback recovery drift result differs from journal"
                    )
                quarantine = self.target.quarantine_root / (
                    f"rollback-{transaction_id}-current-target"
                )
                if quarantine.exists():
                    raise CapabilityError(
                        "Rollback recovery drift abort mutated the target"
                    )
            if result["target_present"] != self.target.target.exists():
                raise CapabilityError("Lock-only abort result target state differs")
            return
        if base_operation == "deploy":
            deploy_context, marker, _, backup = self._validate_deploy_journal(
                journal, operation, transaction_id, source
            )
            if deploy_context != context:
                raise CapabilityError("Terminal deploy context changed")
            if status == "COMMITTED":
                if journal.get("state") != "committed":
                    raise CapabilityError("Committed result lacks committed journal")
                active, active_operation, active_journal, active_context = (
                    self._load_active_deploy(source)
                )
                if (
                    not same_path(active_operation, operation)
                    or active_journal != journal
                    or active_context != context
                    or active.get("deploy_transaction_id") != transaction_id
                ):
                    raise CapabilityError("Committed result active link differs")
                self._validate_prior_backup(
                    context, backup, label="Committed deploy result"
                )
                if scan_root_inventory(self.target.scan_root) != active.get(
                    "scan_root_inventory_sha256"
                ):
                    raise CapabilityError("Committed result scan-root state drifted")
                if result.get("target_present") is not None and result.get(
                    "target_present"
                ) is not True:
                    raise CapabilityError("Committed result target state is invalid")
                if action == "deploy":
                    expected = {
                        "activation_boundary": self.target.spec.activation_boundary,
                        "discovery_required": True,
                        "operation_kind": context["operation_kind"],
                        "payload_fingerprint": source.payload_fingerprint,
                        "prior_target_retained": backup.exists(),
                        "target": str(self.target.target),
                    }
                    for key, value in expected.items():
                        if result.get(key) != value:
                            raise CapabilityError(
                                f"Committed deploy result mismatch for {key}"
                            )
                return
            if status == "REMOVED_WITHOUT_RESTORE":
                if journal.get("state") != "prior_revoked_not_restored":
                    raise CapabilityError(
                        "Revoked-prior deploy recovery journal is not terminal"
                    )
                prior = context.get("prior_state")
                if not isinstance(prior, dict) or prior.get("present") is not True:
                    raise CapabilityError(
                        "Revoked-prior deploy recovery lacks a prior release"
                    )
                revocation = journal.get("prior_release_revocation")
                if (
                    not isinstance(revocation, dict)
                    or set(revocation) != REVOCATION_ENTRY_KEYS
                    or revocation.get("capability_id") != self.capability_id
                    or revocation.get("source_tree") != prior.get("source_tree")
                    or revocation.get("manifest_fingerprint")
                    != prior.get("manifest_fingerprint")
                    or revocation.get("payload_fingerprint")
                    != prior.get("payload_fingerprint")
                ):
                    raise CapabilityError(
                        "Revoked-prior deploy recovery evidence is invalid"
                    )
                new_quarantine = self.target.quarantine_root / (
                    f"deploy-{transaction_id}-new-target"
                )
                revoked_prior_quarantine = self.target.quarantine_root / (
                    f"deploy-{transaction_id}-revoked-prior"
                )
                if new_quarantine.exists() and not self._matches_directory_new(
                    new_quarantine, source, marker
                ):
                    raise CapabilityError(
                        "Revoked-prior deploy recovery new quarantine changed"
                    )
                if revoked_prior_quarantine.exists() and snapshot_directory(
                    revoked_prior_quarantine,
                    capability_id=self.capability_id,
                    tool=self.target.spec.tool,
                ) != prior:
                    raise CapabilityError(
                        "Revoked-prior deploy recovery quarantine changed"
                    )
                if backup.exists() and snapshot_directory(
                    backup,
                    capability_id=self.capability_id,
                    tool=self.target.spec.tool,
                ) != prior:
                    raise CapabilityError(
                        "Revoked-prior deploy recovery backup changed"
                    )
                if not backup.exists() and not revoked_prior_quarantine.exists():
                    raise CapabilityError(
                        "Revoked-prior deploy recovery lost retained prior"
                    )
                if self.target.target.exists() or self.target.active_path.exists():
                    raise CapabilityError(
                        "Revoked-prior deploy recovery reactivated a release"
                    )
                expected = {
                    "new_quarantine": (
                        str(new_quarantine) if new_quarantine.exists() else None
                    ),
                    "new_quarantine_retained": new_quarantine.exists(),
                    "prior_backup_retained": backup.exists(),
                    "prior_release_revocation": revocation,
                    "prior_state_restored": False,
                    "revoked_prior_quarantine": (
                        str(revoked_prior_quarantine)
                        if revoked_prior_quarantine.exists()
                        else None
                    ),
                    "target_present": False,
                }
                for key, value in expected.items():
                    if result.get(key) != value:
                        raise CapabilityError(
                            f"Revoked-prior deploy recovery result mismatch for {key}"
                        )
                return
            if status != "ROLLED_BACK" or journal.get("state") != "recovered_rollback":
                raise CapabilityError("Recovered deploy rollback journal is not terminal")
            prior = context.get("prior_state")
            if not isinstance(prior, dict) or not self._snapshot_matches(prior):
                raise CapabilityError("Recovered deploy prior target differs")
            if backup.exists():
                raise CapabilityError("Recovered deploy retained an ambiguous prior backup")
            self._validate_prior_active_restored(
                operation, journal, transaction_id, prior
            )
            self._validate_restored_scan_inventory(context, prior)
            quarantine = self.target.quarantine_root / (
                f"deploy-{transaction_id}-new-target"
            )
            retained = quarantine.exists()
            if retained and not self._matches_directory_new(
                quarantine, source, marker
            ):
                raise CapabilityError("Recovered deploy quarantine changed")
            if (
                result.get("prior_state_restored") is not True
                or result.get("target_present") != bool(prior["present"])
                or result.get("quarantine_retained") != retained
                or result.get("quarantine")
                != (str(quarantine) if retained else None)
            ):
                raise CapabilityError("Recovered deploy result differs from disk state")
            return
        if status == "REMOVED_WITHOUT_RESTORE":
            if journal.get("state") != "prior_revoked_not_restored":
                raise CapabilityError(
                    "Revoked-prior recovery result journal is not terminal"
                )
            deploy_id = str(journal.get("deploy_transaction_id", ""))
            deploy_operation = self._operation_dir(deploy_id)
            if not same_path(
                str(journal.get("deploy_operation_dir", "")), deploy_operation
            ):
                raise CapabilityError("Rollback result deploy path mismatch")
            deploy_journal = read_json(deploy_operation / "journal.json")
            deploy_context, marker, _, backup = self._validate_deploy_journal(
                deploy_journal, deploy_operation, deploy_id, source
            )
            if journal.get("context") != deploy_context or context != deploy_context:
                raise CapabilityError("Rollback result deploy context differs")
            prior = context.get("prior_state")
            if not isinstance(prior, dict) or prior.get("present") is not True:
                raise CapabilityError("Revoked-prior recovery lacks a prior release")
            revoked_prior_quarantine = self.target.quarantine_root / (
                f"rollback-{transaction_id}-revoked-prior"
            )
            if backup.exists() and (
                not backup.is_dir()
                or snapshot_directory(
                    backup,
                    capability_id=self.capability_id,
                    tool=self.target.spec.tool,
                )
                != prior
            ):
                raise CapabilityError("Revoked prior backup changed")
            if revoked_prior_quarantine.exists() and (
                not revoked_prior_quarantine.is_dir()
                or snapshot_directory(
                    revoked_prior_quarantine,
                    capability_id=self.capability_id,
                    tool=self.target.spec.tool,
                )
                != prior
            ):
                raise CapabilityError("Revoked prior quarantine changed")
            if backup.exists() == revoked_prior_quarantine.exists():
                raise CapabilityError(
                    "Revoked prior must be retained in exactly one location"
                )
            revocation = journal.get("prior_release_revocation")
            if (
                not isinstance(revocation, dict)
                or set(revocation) != REVOCATION_ENTRY_KEYS
                or revocation.get("capability_id") != self.capability_id
                or revocation.get("source_tree") != prior.get("source_tree")
                or revocation.get("manifest_fingerprint")
                != prior.get("manifest_fingerprint")
                or revocation.get("payload_fingerprint")
                != prior.get("payload_fingerprint")
            ):
                raise CapabilityError("Revoked prior evidence is invalid")
            quarantine = self.target.quarantine_root / (
                f"rollback-{transaction_id}-current-target"
            )
            if not quarantine.is_dir() or not self._matches_directory_new(
                quarantine, source, marker
            ):
                raise CapabilityError("Rollback result quarantine is missing or changed")
            if self.target.target.exists() or self.target.active_path.exists():
                raise CapabilityError("Revoked prior was unexpectedly reactivated")
            expected = {
                "backup_retained": backup.exists(),
                "deploy_transaction_id": deploy_id,
                "prior_release_revocation": revocation,
                "prior_state_restored": False,
                "quarantine": str(quarantine),
                "quarantine_retained": True,
                "revoked_prior_quarantine": (
                    str(revoked_prior_quarantine)
                    if revoked_prior_quarantine.exists()
                    else None
                ),
                "revoked_prior_quarantine_retained": (
                    revoked_prior_quarantine.exists()
                ),
                "target_present": False,
            }
            for key, value in expected.items():
                if result.get(key) != value:
                    raise CapabilityError(
                        f"Revoked-prior recovery result mismatch for {key}"
                    )
            return
        if status != "ROLLED_BACK" or journal.get("state") != "rolled_back":
            raise CapabilityError("Rollback result journal is not terminal")
        deploy_id = str(journal.get("deploy_transaction_id", ""))
        deploy_operation = self._operation_dir(deploy_id)
        if not same_path(
            str(journal.get("deploy_operation_dir", "")), deploy_operation
        ):
            raise CapabilityError("Rollback result deploy path mismatch")
        deploy_journal = read_json(deploy_operation / "journal.json")
        deploy_context, marker, _, backup = self._validate_deploy_journal(
            deploy_journal, deploy_operation, deploy_id, source
        )
        self._validate_fresh_rollback_approval(
            approval, deploy_journal, str(journal["mutation_action"])
        )
        if journal.get("context") != deploy_context or context != deploy_context:
            raise CapabilityError("Rollback result deploy context differs")
        prior = context.get("prior_state")
        if not isinstance(prior, dict) or not self._snapshot_matches(prior):
            raise CapabilityError("Rollback result prior target differs")
        if backup.exists():
            raise CapabilityError("Rollback result retained an ambiguous prior backup")
        self._validate_prior_active_restored(
            deploy_operation, deploy_journal, deploy_id, prior
        )
        self._validate_restored_scan_inventory(context, prior)
        quarantine = self.target.quarantine_root / (
            f"rollback-{transaction_id}-current-target"
        )
        if not quarantine.is_dir() or not self._matches_directory_new(
            quarantine, source, marker
        ):
            raise CapabilityError("Rollback result quarantine is missing or changed")
        expected = {
            "deploy_transaction_id": deploy_id,
            "prior_state_restored": True,
            "quarantine": str(quarantine),
            "quarantine_retained": True,
            "target_present": bool(prior["present"]),
        }
        for key, value in expected.items():
            if result.get(key) != value:
                raise CapabilityError(f"Rollback terminal result mismatch for {key}")

    def _record_mutation_result(
        self,
        operation: Path,
        *,
        action: str,
        approval_sha256: str,
        result: dict[str, Any],
    ) -> None:
        if action not in {"deploy", "emergency-rollback", "rollback"}:
            raise CapabilityError("Mutation result action is invalid")
        self._validate_terminal_result_shape(action, result, operation.name)
        value = {
            "action": action,
            "approval_sha256": approval_sha256,
            "result": result,
            "result_sha256": canonical_json_hash(result),
            "schema_version": 1,
            "transaction_id": operation.name,
        }
        immutable_write_bytes(
            operation / "mutation-result.json",
            canonical_json_bytes(value),
            "mutation result",
        )

    def _record_recovery_result(
        self,
        operation: Path,
        *,
        action: str,
        approval_sha256: str,
        result: dict[str, Any],
    ) -> None:
        transaction_id = operation.name
        if action not in {"recover-deploy", "recover-rollback"}:
            raise CapabilityError("Recovery result action is invalid")
        self._validate_terminal_result_shape(action, result, transaction_id)
        value = {
            "action": action,
            "approval_sha256": approval_sha256,
            "result": result,
            "result_sha256": canonical_json_hash(result),
            "schema_version": 1,
            "transaction_id": transaction_id,
        }
        immutable_write_bytes(
            operation / "recovery-result.json",
            canonical_json_bytes(value),
            "recovery result",
        )

    def _read_terminal_result(
        self,
        operation: Path,
        *,
        filename: str,
        wrapper_keys: set[str],
        action: str,
        approval_path: Path,
    ) -> dict[str, Any] | None:
        path = operation / filename
        if not path.exists():
            return None
        value = read_json(path, "recovery result")
        result = value.get("result")
        approval_raw = approval_path.read_bytes()
        approval_sha256 = sha256_bytes(approval_raw)
        if (
            set(value) != wrapper_keys
            or value.get("schema_version") != 1
            or value.get("transaction_id") != operation.name
            or not isinstance(result, dict)
            or value.get("result_sha256") != canonical_json_hash(result)
        ):
            raise CapabilityError("Recovery result identity or integrity is invalid")
        if (
            value.get("action") != action
            or value.get("approval_sha256") != approval_sha256
        ):
            return None
        stored = operation / "approval-receipt.json"
        if not stored.is_file() or stored.read_bytes() != approval_raw:
            raise CapabilityError("Terminal result receipt does not match")
        self._validate_terminal_state(
            operation,
            action=action,
            result=result,
            approval_path=approval_path,
        )
        replayed = dict(result)
        replayed["replayed"] = True
        return replayed

    def _replay_terminal_result(
        self,
        approval_path: Path | str,
        *,
        action: str,
        filename: str,
        wrapper_keys: set[str],
        allow_missing: bool = False,
    ) -> dict[str, Any] | None:
        approval = absolute_plain_path(approval_path, must_exist=True)
        if not stat.S_ISREG(approval.lstat().st_mode):
            raise CapabilityError("Approval receipt must be a plain file")
        for forbidden in (self.target.scan_root, self.target.state_root):
            try:
                approval.relative_to(Path(os.path.abspath(forbidden)))
            except ValueError:
                continue
            raise CapabilityError("Approval receipt must remain outside scan and state roots")
        matches: list[dict[str, Any]] = []
        if self.target.operations_root.is_dir():
            for operation in self.target.operations_root.iterdir():
                if not operation.is_dir() or not TRANSACTION_ID.fullmatch(operation.name):
                    continue
                result = self._read_terminal_result(
                    operation,
                    filename=filename,
                    wrapper_keys=wrapper_keys,
                    action=action,
                    approval_path=approval,
                )
                if result is not None:
                    matches.append(result)
        if not matches and allow_missing:
            return None
        if len(matches) != 1:
            raise CapabilityError("No unique completed terminal result matches receipt")
        return matches[0]

    def _replay_recovery_result(
        self,
        approval_path: Path | str,
        *,
        action: str,
    ) -> dict[str, Any]:
        result = self._replay_terminal_result(
            approval_path,
            action=action,
            filename="recovery-result.json",
            wrapper_keys=RECOVERY_RESULT_KEYS,
        )
        if result is None:
            raise CapabilityError("No completed recovery result matches receipt")
        return result

    def _replay_mutation_result(
        self,
        approval_path: Path | str,
        *,
        action: str,
    ) -> dict[str, Any] | None:
        return self._replay_terminal_result(
            approval_path,
            action=action,
            filename="mutation-result.json",
            wrapper_keys=MUTATION_RESULT_KEYS,
            allow_missing=True,
        )

    def _record_lock_only_abort(
        self,
        *,
        lock: dict[str, Any],
        context: dict[str, Any],
        approval_raw: bytes,
        approval_sha256: str,
        approval_context: dict[str, Any] | None = None,
        mutation_action: str | None = None,
    ) -> dict[str, Any]:
        effective_approval_context = approval_context or context
        transaction_id = str(lock["transaction_id"])
        operation = self._operation_dir(transaction_id)
        operation.mkdir(exist_ok=True, mode=0o700)
        receipt = operation / "approval-receipt.json"
        journal_path = operation / "journal.json"
        allowed = {receipt.name, journal_path.name, "recovery-result.json"}
        if any(
            item.name not in allowed
            and not allowed_transaction_temporary(item.name, allowed)
            for item in operation.iterdir()
        ):
            raise CapabilityError("Lock-only operation directory contains unknown state")
        if receipt.exists() and receipt.read_bytes() != approval_raw:
            raise CapabilityError("Lock-only stored approval receipt mismatch")
        if not receipt.exists():
            atomic_write_bytes(receipt, approval_raw)
        journal = {
            "approval_id": lock["approval_id"],
            "approval_sha256": approval_sha256,
            "context": context,
            "context_sha256": canonical_json_hash(context),
            "created_at": lock["created_at"],
            "operation": lock["operation"],
            "operation_dir": str(Path(os.path.abspath(operation))),
            "schema_version": 1,
            "state": "aborted_no_writes",
            "transaction_id": transaction_id,
            "updated_at": utc_now(),
        }
        if lock["operation"] == "rollback":
            if mutation_action not in {"rollback", "emergency-rollback"}:
                raise CapabilityError("Lock-only rollback action is invalid")
            if set(effective_approval_context) == ROLLBACK_AUTHORIZATION_KEYS:
                journal["approval_context"] = effective_approval_context
                journal["approval_context_sha256"] = canonical_json_hash(
                    effective_approval_context
                )
            elif effective_approval_context != context:
                raise CapabilityError(
                    "Legacy rollback approval context differs from deploy context"
                )
            journal["mutation_action"] = mutation_action
        if journal_path.exists():
            existing = read_json(journal_path)
            comparable = dict(journal)
            comparable["updated_at"] = existing.get("updated_at")
            if existing != comparable:
                raise CapabilityError("Lock-only abort journal already differs")
            journal = existing
        else:
            atomic_write_json(journal_path, journal)
        result = {
            "action": f"recover-{lock['operation']}",
            "status": "ABORTED_NO_WRITES",
            "transaction_id": transaction_id,
            "target_present": self.target.target.exists(),
            "writes_scope": "transaction-state-only",
        }
        self._record_recovery_result(
            operation,
            action=str(result["action"]),
            approval_sha256=approval_sha256,
            result=result,
        )
        self._release(transaction_id)
        return result

    def _checkpoint(
        self,
        path: Path,
        journal: dict[str, Any],
        state: str,
        crash_after: str | None,
    ) -> None:
        journal["state"] = state
        journal["updated_at"] = utc_now()
        atomic_write_json(path, journal)
        if crash_after == state:
            raise InjectedCrash(state)

    def _approval_used(self, approval_id: str) -> bool:
        if not self.target.operations_root.exists():
            return False
        for operation in self.target.operations_root.iterdir():
            journal_path = operation / "journal.json"
            if not journal_path.is_file():
                continue
            journal = read_json(journal_path)
            if journal.get("approval_id") == approval_id:
                return True
        return False

    def _marker(self, source: GitSource, context: dict[str, Any]) -> dict[str, Any]:
        return marker_v2(
            capability_id=self.capability_id,
            capability_version=str(context["capability_version"]),
            tool=self.target.spec.tool,
            scope=self.target.spec.scope,
            source=source,
            activation_boundary=self.target.spec.activation_boundary,
        )

    def _verify_new_target(
        self, source: GitSource, marker: dict[str, Any]
    ) -> None:
        if not self.target.target.is_dir():
            raise CapabilityError("Activated capability target is missing")
        scanned = scan_directory(
            self.target.target, exclude_marker=True, reject_hardlinks=True
        )
        if scanned["payload_fingerprint"] != source.payload_fingerprint:
            raise CapabilityError("Activated capability payload fingerprint mismatch")
        if read_marker(self.target.target / CAPABILITY_MARKER_NAME).data != marker:
            raise CapabilityError("Activated capability marker mismatch")

    def _snapshot_matches(self, expected: dict[str, Any]) -> bool:
        try:
            return snapshot_target(self.target) == expected
        except CapabilityError:
            return False

    def _restore_prior_active(
        self,
        operation: Path,
        journal: dict[str, Any],
        transaction_id: str,
    ) -> None:
        prior_active = operation / "prior-active.json"
        expected_hash = journal.get("prior_active_sha256")
        if expected_hash is not None:
            if self.target.active_path.exists():
                current_raw = self.target.active_path.read_bytes()
                if sha256_bytes(current_raw) == expected_hash:
                    return
            if not prior_active.is_file():
                raise CapabilityError("Prior active-install record is missing")
            raw = prior_active.read_bytes()
            if sha256_bytes(raw) != expected_hash:
                raise CapabilityError("Prior active-install record changed")
            if self.target.active_path.exists():
                current = read_json(self.target.active_path, "active install")
                if current.get("deploy_transaction_id") != transaction_id:
                    raise CapabilityError("Unexpected active-install record blocks restore")
                self.target.active_path.unlink()
            atomic_write_bytes(self.target.active_path, raw)
        elif self.target.active_path.exists():
            current = read_json(self.target.active_path, "active install")
            if current.get("deploy_transaction_id") != transaction_id:
                raise CapabilityError("Unexpected active-install record blocks cleanup")
            self.target.active_path.unlink()

    def deploy(
        self,
        approval_path: Path | str,
        *,
        owner_authorized: bool,
        crash_after: str | None = None,
        before_activate: Callable[[], None] | None = None,
        _lease_token: object | None = None,
    ) -> dict[str, Any]:
        if not owner_authorized:
            raise CapabilityError("Deploy requires explicit owner authorization")
        if crash_after and self.target.fixture_root is None:
            raise CapabilityError("Failure injection is allowed only in a TEMP fixture")
        if not self.target.lock_path.exists():
            replayed = self._replay_mutation_result(
                approval_path, action="deploy"
            )
            if replayed is not None:
                return replayed
        source, context = self._source_and_context()
        if context["operation_kind"] == "noop":
            raise CapabilityError("Requested release is already current")
        approval, approval_raw, approval_sha = load_approval(
            approval_path,
            context=context,
            required_action="deploy",
            target=self.target,
        )
        if _lease_token is not self._lease_token:
            self._create_state_roots()
            with self._operation_lease():
                return self.deploy(
                    approval_path,
                    owner_authorized=owner_authorized,
                    crash_after=crash_after,
                    before_activate=before_activate,
                    _lease_token=self._lease_token,
                )
        transaction_id = uuid.uuid4().hex
        context_sha = canonical_json_hash(context)
        self._acquire(
            transaction_id,
            "deploy",
            approval_id=str(approval["approval_id"]),
            approval_sha256=approval_sha,
            context=context,
        )
        if crash_after == "lock_acquired":
            raise InjectedCrash("lock_acquired")
        operation = self._operation_dir(transaction_id)
        journal_path = operation / "journal.json"
        try:
            fresh_source, fresh_context = self._source_and_context(allow_lock=True)
            fresh, _, fresh_sha = load_approval(
                approval_path,
                context=context,
                required_action="deploy",
                target=self.target,
            )
            if (
                fresh_context != context
                or fresh_source.source_tree != source.source_tree
                or fresh_sha != approval_sha
                or fresh.get("approval_id") != approval.get("approval_id")
            ):
                raise CapabilityError(
                    "Release, target, guard or approval state changed before journal"
                )
            if self._approval_used(str(approval["approval_id"])):
                raise CapabilityError("Approval ID has already authorized a deploy")
            operation.mkdir(mode=0o700)
            staged = operation / "staged"
            backup = operation / "prior-target"
            marker = self._marker(source, context)
            discovery_challenge = secrets.token_hex(32)
            prior_active_sha: str | None = None
            if self.target.active_path.exists():
                active_raw = self.target.active_path.read_bytes()
                prior_active_sha = sha256_bytes(active_raw)
            journal: dict[str, Any] = {
                "approval_id": approval["approval_id"],
                "approval_sha256": approval_sha,
                "backup": str(Path(os.path.abspath(backup))),
                "context": context,
                "context_sha256": context_sha,
                "created_at": utc_now(),
                "discovery_challenge": discovery_challenge,
                "marker": marker,
                "operation": "deploy",
                "operation_dir": str(Path(os.path.abspath(operation))),
                "prior_active_sha256": prior_active_sha,
                "schema_version": 1,
                "staged": str(Path(os.path.abspath(staged))),
                "transaction_id": transaction_id,
            }
            self._checkpoint(journal_path, journal, "preparing", crash_after)
            atomic_write_bytes(operation / "approval-receipt.json", approval_raw)
            if prior_active_sha is not None:
                atomic_write_bytes(
                    operation / "prior-active.json",
                    self.target.active_path.read_bytes(),
                )
            self._checkpoint(journal_path, journal, "approval_recorded", crash_after)
            materialize_release(source, staged, marker)
            self._checkpoint(journal_path, journal, "staged", crash_after)
            fresh_source, fresh_context = self._source_and_context(allow_lock=True)
            if fresh_context != context or fresh_source.source_tree != source.source_tree:
                raise CapabilityError("Release, target or guard state changed under lock")
            fresh, _, fresh_sha = load_approval(
                approval_path,
                context=context,
                required_action="deploy",
                target=self.target,
            )
            if (
                fresh_sha != approval_sha
                or fresh.get("approval_id") != approval.get("approval_id")
            ):
                raise CapabilityError("Approval receipt changed under lock")
            if before_activate:
                before_activate()
            final_source, final_context = self._source_and_context(allow_lock=True)
            if (
                final_context != context
                or final_source.source_tree != source.source_tree
            ):
                raise CapabilityError(
                    "Release, target, guard or inventory changed before activation"
                )
            self._checkpoint(journal_path, journal, "prepared", crash_after)
            if context["prior_state"]["present"]:
                os.replace(self.target.target, backup)
                fsync_parent(backup)
                if snapshot_directory(
                    backup,
                    capability_id=self.capability_id,
                    tool=self.target.spec.tool,
                ) != context["prior_state"]:
                    raise CapabilityError("Retained prior target verification failed")
                self._checkpoint(journal_path, journal, "prior_moved", crash_after)
            elif self.target.target.exists():
                raise CapabilityError("Install target appeared before activation")
            os.replace(staged, self.target.target)
            fsync_parent(self.target.target)
            if crash_after == "target_renamed":
                raise InjectedCrash("target_renamed")
            self._verify_new_target(source, marker)
            installed_inventory_sha256 = scan_root_inventory(self.target.scan_root)
            journal["activated_at"] = utc_now()
            journal["scan_root_inventory_sha256"] = installed_inventory_sha256
            self._checkpoint(journal_path, journal, "target_activated", crash_after)
            active = {
                "capability_id": self.capability_id,
                "context_sha256": canonical_json_hash(context),
                "deploy_transaction_id": transaction_id,
                "discovery_challenge": discovery_challenge,
                "manifest_fingerprint": source.manifest_fingerprint,
                "operation_kind": context["operation_kind"],
                "payload_fingerprint": source.payload_fingerprint,
                "scan_root_inventory_sha256": installed_inventory_sha256,
                "schema_version": 1,
                "source_commit": source.source_commit,
                "source_tree": source.source_tree,
                "tool": self.target.spec.tool,
                "updated_at": utc_now(),
            }
            if self.target.active_path.exists():
                existing = self.target.active_path
                if (
                    prior_active_sha is None
                    or sha256_bytes(existing.read_bytes()) != prior_active_sha
                ):
                    raise CapabilityError("Active-install record changed before activation")
                existing.unlink()
            atomic_write_json(self.target.active_path, active)
            self._checkpoint(journal_path, journal, "active_recorded", crash_after)
            self._checkpoint(journal_path, journal, "committed", crash_after)
            result = {
                "action": "deploy",
                "status": "COMMITTED",
                "operation_kind": context["operation_kind"],
                "transaction_id": transaction_id,
                "target": str(self.target.target),
                "payload_fingerprint": source.payload_fingerprint,
                "prior_target_retained": backup.exists(),
                "rules_or_config_mutation": False,
                "activation_boundary": self.target.spec.activation_boundary,
                "discovery_required": True,
            }
            self._record_mutation_result(
                operation,
                action="deploy",
                approval_sha256=approval_sha,
                result=result,
            )
            self._release(transaction_id)
            return result
        except InjectedCrash:
            raise
        except Exception:
            if not journal_path.exists() and self.target.lock_path.exists():
                self._release(transaction_id)
            raise

    def _validate_deploy_journal(
        self,
        journal: dict[str, Any],
        operation: Path,
        transaction_id: str,
        source: GitSource,
    ) -> tuple[dict[str, Any], dict[str, Any], Path, Path]:
        if (
            journal.get("schema_version") != 1
            or journal.get("operation") != "deploy"
            or journal.get("transaction_id") != transaction_id
        ):
            raise CapabilityError("Deploy journal identity is invalid")
        if not same_path(str(journal.get("operation_dir", "")), operation):
            raise CapabilityError("Deploy journal operation path mismatch")
        staged = operation / "staged"
        backup = operation / "prior-target"
        if not same_path(str(journal.get("staged", "")), staged) or not same_path(
            str(journal.get("backup", "")), backup
        ):
            raise CapabilityError("Deploy journal bounded paths mismatch")
        context = journal.get("context")
        marker = journal.get("marker")
        if not isinstance(context, dict) or not isinstance(marker, dict):
            raise CapabilityError("Deploy journal context or marker is invalid")
        if journal.get("context_sha256") != canonical_json_hash(context):
            raise CapabilityError("Deploy journal context hash mismatch")
        inventory_sha256 = journal.get("scan_root_inventory_sha256")
        if inventory_sha256 is not None and (
            not isinstance(inventory_sha256, str)
            or not HEX_64.fullmatch(inventory_sha256)
        ):
            raise CapabilityError("Deploy journal scan-root inventory hash is invalid")
        challenge = journal.get("discovery_challenge")
        if not isinstance(challenge, str) or not HEX_64.fullmatch(challenge):
            raise CapabilityError("Deploy journal discovery challenge is invalid")
        activated_at = journal.get("activated_at")
        activation_states = {"target_activated", "active_recorded", "committed"}
        state = journal.get("state")
        if state in activation_states:
            if not isinstance(activated_at, str):
                raise CapabilityError("Activated deploy journal lacks activation time")
            activated = parse_timestamp(activated_at, "activated_at")
            created = parse_timestamp(str(journal.get("created_at", "")), "created_at")
            if activated < created:
                raise CapabilityError("Deploy activation predates transaction creation")
        elif state == "recovered_rollback" and activated_at is not None:
            activated = parse_timestamp(str(activated_at), "activated_at")
            created = parse_timestamp(str(journal.get("created_at", "")), "created_at")
            if activated < created:
                raise CapabilityError("Deploy activation predates transaction creation")
        elif state in {
            "prior_revocation_recorded",
            "prior_revoked_not_restored",
        } and activated_at is not None:
            activated = parse_timestamp(str(activated_at), "activated_at")
            created = parse_timestamp(str(journal.get("created_at", "")), "created_at")
            if activated < created:
                raise CapabilityError("Deploy activation predates transaction creation")
        elif activated_at is not None:
            raise CapabilityError("Pre-activation deploy journal has activation time")
        validate_engine_identity(context.get("engine"), "Deploy journal")
        validate_git_identity(context.get("git_executable"), "Deploy journal")
        expected_source = {
            "capability_id": self.capability_id,
            "source_commit": source.source_commit,
            "source_tree": source.source_tree,
            "state_root": str(Path(os.path.abspath(self.target.state_root))),
            "target": str(Path(os.path.abspath(self.target.target))),
            "tool": self.target.spec.tool,
            "payload_fingerprint": source.payload_fingerprint,
            "manifest_fingerprint": source.manifest_fingerprint,
        }
        for key, value in expected_source.items():
            if context.get(key) != value:
                raise CapabilityError(f"Deploy journal source mismatch for {key}")
        expected_marker = marker_v2(
            capability_id=self.capability_id,
            capability_version=str(context.get("capability_version", "")),
            tool=self.target.spec.tool,
            scope=self.target.spec.scope,
            source=source,
            activation_boundary=self.target.spec.activation_boundary,
            installed_at=str(marker.get("installed_at", "")),
        )
        if marker != expected_marker:
            raise CapabilityError("Deploy journal marker mismatch")
        return context, marker, staged, backup

    def _matches_new(self, source: GitSource, marker: dict[str, Any]) -> bool:
        try:
            self._verify_new_target(source, marker)
            return True
        except CapabilityError:
            return False

    def _validate_prior_backup(
        self,
        context: dict[str, Any],
        backup: Path,
        *,
        label: str,
    ) -> dict[str, Any]:
        prior = context.get("prior_state")
        if not isinstance(prior, dict) or not isinstance(prior.get("present"), bool):
            raise CapabilityError("Deploy context prior state is invalid")
        if prior["present"]:
            if not backup.is_dir():
                raise CapabilityError(f"{label} prior-target backup is missing")
            if snapshot_directory(
                backup,
                capability_id=self.capability_id,
                tool=self.target.spec.tool,
            ) != prior:
                raise CapabilityError(f"{label} prior-target backup changed")
        elif backup.exists():
            raise CapabilityError(f"{label} has an unexpected prior-target backup")
        return prior

    def _validate_prior_active_evidence(
        self,
        context: dict[str, Any],
        deploy_operation: Path,
        deploy_journal: dict[str, Any],
        deploy_transaction_id: str,
        *,
        label: str,
    ) -> None:
        prior = context.get("prior_state")
        if not isinstance(prior, dict) or not isinstance(prior.get("present"), bool):
            raise CapabilityError("Deploy context prior state is invalid")
        expected_hash = deploy_journal.get("prior_active_sha256")
        prior_active_path = deploy_operation / "prior-active.json"
        if not prior["present"]:
            if expected_hash is not None or prior_active_path.exists():
                raise CapabilityError(f"{label} has unexpected prior-active evidence")
            return
        if not isinstance(expected_hash, str) or not HEX_64.fullmatch(expected_hash):
            raise CapabilityError(f"{label} prior-active hash is missing or invalid")
        if not prior_active_path.is_file():
            raise CapabilityError(f"{label} prior-active evidence is missing")
        raw = prior_active_path.read_bytes()
        if sha256_bytes(raw) != expected_hash:
            raise CapabilityError(f"{label} prior-active evidence changed")
        prior_active = read_json(prior_active_path, "prior active install")
        if prior_active.get("deploy_transaction_id") == deploy_transaction_id:
            raise CapabilityError(f"{label} prior-active points at current deploy")
        self._validate_active_link(prior_active, prior)

    def _validate_prior_active_recovery_evidence(
        self,
        context: dict[str, Any],
        deploy_operation: Path,
        deploy_journal: dict[str, Any],
        deploy_transaction_id: str,
        crash_after: str | None = None,
    ) -> None:
        prior = context.get("prior_state")
        if not isinstance(prior, dict) or not isinstance(prior.get("present"), bool):
            raise CapabilityError("Deploy recovery prior state is invalid")
        prior_active_path = deploy_operation / "prior-active.json"
        if not prior["present"] or prior_active_path.exists():
            self._validate_prior_active_evidence(
                context,
                deploy_operation,
                deploy_journal,
                deploy_transaction_id,
                label="Deploy recovery",
            )
            return
        expected_hash = deploy_journal.get("prior_active_sha256")
        if not isinstance(expected_hash, str) or not HEX_64.fullmatch(expected_hash):
            raise CapabilityError("Deploy recovery prior-active hash is invalid")
        if not self._snapshot_matches(prior) or not self.target.active_path.is_file():
            raise CapabilityError("Deploy recovery prior-active evidence is missing")
        raw = self.target.active_path.read_bytes()
        if sha256_bytes(raw) != expected_hash:
            raise CapabilityError("Deploy recovery prior active record changed")
        active = read_json(self.target.active_path, "active install")
        if active.get("deploy_transaction_id") == deploy_transaction_id:
            raise CapabilityError("Deploy recovery prior-active evidence is missing")
        self._validate_active_link(active, prior)
        immutable_write_bytes(
            prior_active_path,
            raw,
            "deploy recovery prior-active evidence",
        )
        if crash_after == "recovery_prior_active_recorded":
            raise InjectedCrash("recovery_prior_active_recorded")

    def _restore_prior_target(
        self,
        *,
        context: dict[str, Any],
        backup: Path,
        new_source: GitSource,
        marker: dict[str, Any],
        quarantine: Path,
    ) -> None:
        prior = context.get("prior_state")
        if not isinstance(prior, dict) or not isinstance(prior.get("present"), bool):
            raise CapabilityError("Deploy context prior state is invalid")
        target_is_prior = bool(
            prior["present"]
            and self.target.target.exists()
            and self._snapshot_matches(prior)
        )
        if target_is_prior:
            if backup.exists():
                raise CapabilityError("Recovery found both active and retained prior target")
        else:
            self._validate_prior_backup(
                context, backup, label="Deploy recovery"
            )
            if self.target.target.exists():
                if not self._matches_new(new_source, marker):
                    raise CapabilityError("Recovery found an unknown active target state")
                if quarantine.exists():
                    raise CapabilityError("Recovery quarantine already exists")
                self._validate_policy_snapshot()
                os.replace(self.target.target, quarantine)
                fsync_parent(quarantine)
        if prior["present"]:
            if self.target.target.exists():
                if backup.exists() or not self._snapshot_matches(prior):
                    raise CapabilityError("Recovery found ambiguous prior target state")
            else:
                self._validate_policy_snapshot()
                os.replace(backup, self.target.target)
                fsync_parent(self.target.target)
            if not self._snapshot_matches(prior):
                raise CapabilityError("Restored prior target verification failed")
        elif self.target.target.exists():
            raise CapabilityError("Recovery did not restore exact target absence")
        if quarantine.exists() and not self._matches_directory_new(
            quarantine, new_source, marker
        ):
            raise CapabilityError("Recovery quarantine verification failed")

    def _matches_directory_new(
        self,
        path: Path,
        source: GitSource,
        marker: dict[str, Any],
    ) -> bool:
        try:
            scanned = scan_directory(path, exclude_marker=True, reject_hardlinks=True)
            return (
                scanned["payload_fingerprint"] == source.payload_fingerprint
                and read_marker(path / CAPABILITY_MARKER_NAME).data == marker
            )
        except (CapabilityError, OSError):
            return False

    def _recover_deploy_without_revoked_prior(
        self,
        *,
        operation: Path,
        journal_path: Path,
        journal: dict[str, Any],
        context: dict[str, Any],
        source: GitSource,
        marker: dict[str, Any],
        backup: Path,
        approval_sha256: str,
        prior_revocation: dict[str, str],
        crash_after: str | None,
    ) -> dict[str, Any]:
        current_catalog = self._validate_policy_snapshot()
        transaction_id = str(journal["transaction_id"])
        prior = context.get("prior_state")
        if (
            not isinstance(prior, dict)
            or prior.get("present") is not True
            or set(prior_revocation) != REVOCATION_ENTRY_KEYS
            or prior_revocation.get("capability_id") != self.capability_id
            or prior_revocation.get("source_tree") != prior.get("source_tree")
            or prior_revocation.get("manifest_fingerprint")
            != prior.get("manifest_fingerprint")
            or prior_revocation.get("payload_fingerprint")
            != prior.get("payload_fingerprint")
        ):
            raise CapabilityError("Deploy recovery prior revocation is invalid")
        if self._matching_prior_revocation(prior, current_catalog) != prior_revocation:
            raise CapabilityError(
                "Deploy recovery prior revocation changed before safe removal"
            )
        stored_revocation = journal.get("prior_release_revocation")
        if stored_revocation is None:
            journal["prior_release_revocation"] = prior_revocation
            self._checkpoint(
                journal_path,
                journal,
                "prior_revocation_recorded",
                crash_after,
            )
        elif stored_revocation != prior_revocation:
            raise CapabilityError("Deploy recovery prior revocation changed")

        new_quarantine = (
            self.target.quarantine_root
            / f"deploy-{transaction_id}-new-target"
        )
        revoked_prior_quarantine = (
            self.target.quarantine_root
            / f"deploy-{transaction_id}-revoked-prior"
        )
        active_raw: bytes | None = None
        if self.target.active_path.exists():
            if not self.target.active_path.is_file():
                raise CapabilityError("Deploy recovery active record is not a plain file")
            active_raw = self.target.active_path.read_bytes()
            active = read_json(self.target.active_path, "active install")
            active_id = str(active.get("deploy_transaction_id", ""))
            prior_active = operation / "prior-active.json"
            if active_id == transaction_id:
                self._validate_active_shape(active)
            elif not prior_active.is_file() or active_raw != prior_active.read_bytes():
                raise CapabilityError(
                    "Deploy recovery active record is neither current nor prior"
                )

        if self.target.target.exists():
            if self._matches_new(source, marker):
                if new_quarantine.exists():
                    raise CapabilityError(
                        "Deploy recovery new target and quarantine are both present"
                    )
                self._validate_policy_snapshot()
                os.replace(self.target.target, new_quarantine)
                fsync_parent(new_quarantine)
            elif self._snapshot_matches(prior):
                if revoked_prior_quarantine.exists():
                    raise CapabilityError(
                        "Deploy recovery revoked prior and quarantine are both present"
                    )
                self._validate_policy_snapshot()
                os.replace(self.target.target, revoked_prior_quarantine)
                fsync_parent(revoked_prior_quarantine)
            else:
                raise CapabilityError(
                    "Deploy recovery target is neither current nor revoked prior"
                )
        if new_quarantine.exists() and not self._matches_directory_new(
            new_quarantine, source, marker
        ):
            raise CapabilityError("Deploy recovery new quarantine changed")
        if revoked_prior_quarantine.exists() and snapshot_directory(
            revoked_prior_quarantine,
            capability_id=self.capability_id,
            tool=self.target.spec.tool,
        ) != prior:
            raise CapabilityError("Deploy recovery revoked-prior quarantine changed")
        if backup.exists() and snapshot_directory(
            backup,
            capability_id=self.capability_id,
            tool=self.target.spec.tool,
        ) != prior:
            raise CapabilityError("Deploy recovery revoked prior backup changed")
        if not backup.exists() and not revoked_prior_quarantine.exists():
            raise CapabilityError("Deploy recovery lost retained revoked prior")
        if self.target.active_path.exists():
            if active_raw is None or self.target.active_path.read_bytes() != active_raw:
                raise CapabilityError("Deploy recovery active record changed")
            self._validate_policy_snapshot()
            self.target.active_path.unlink()
            fsync_parent(self.target.active_path)
        if self.target.target.exists():
            raise CapabilityError("Deploy recovery reactivated a revoked prior")
        self._checkpoint(
            journal_path,
            journal,
            "prior_revoked_not_restored",
            crash_after,
        )
        result = {
            "action": "recover-deploy",
            "new_quarantine": (
                str(new_quarantine) if new_quarantine.exists() else None
            ),
            "new_quarantine_retained": new_quarantine.exists(),
            "prior_backup_retained": backup.exists(),
            "prior_release_revocation": prior_revocation,
            "prior_state_restored": False,
            "revoked_prior_quarantine": (
                str(revoked_prior_quarantine)
                if revoked_prior_quarantine.exists()
                else None
            ),
            "status": "REMOVED_WITHOUT_RESTORE",
            "target_present": False,
            "transaction_id": transaction_id,
        }
        self._record_recovery_result(
            operation,
            action="recover-deploy",
            approval_sha256=approval_sha256,
            result=result,
        )
        self._release(transaction_id)
        return result

    def recover_deploy(
        self,
        approval_path: Path | str,
        *,
        owner_authorized: bool,
        crash_after: str | None = None,
    ) -> dict[str, Any]:
        if not owner_authorized:
            raise CapabilityError("Deploy recovery requires explicit owner authorization")
        if not self.target.lock_path.is_file():
            return self._replay_recovery_result(
                approval_path, action="recover-deploy"
            )
        with self._operation_lease():
            if not self.target.lock_path.is_file():
                return self._replay_recovery_result(
                    approval_path, action="recover-deploy"
                )
            return self._recover_deploy_locked(
                approval_path,
                owner_authorized=owner_authorized,
                crash_after=crash_after,
            )

    def _recover_deploy_locked(
        self,
        approval_path: Path | str,
        *,
        owner_authorized: bool,
        crash_after: str | None = None,
    ) -> dict[str, Any]:
        if not owner_authorized:
            raise CapabilityError("Deploy recovery requires explicit owner authorization")
        if crash_after and self.target.fixture_root is None:
            raise CapabilityError("Failure injection is allowed only in a TEMP fixture")
        validate_paths(self.target)
        current_catalog = self._validate_policy_snapshot()
        if not self.target.lock_path.is_file():
            raise CapabilityError("No interrupted capability operation lock exists")
        lock = self._read_lock()
        transaction_id = str(lock.get("transaction_id", ""))
        if (
            lock.get("operation") != "deploy"
            or not TRANSACTION_ID.fullmatch(transaction_id)
        ):
            raise CapabilityError("Active lock is not a deploy transaction")
        operation = self._operation_dir(transaction_id)
        journal_path = operation / "journal.json"
        if (operation / "recovery-result.json").exists():
            result = self._replay_recovery_result(
                approval_path, action="recover-deploy"
            )
            self._release(transaction_id)
            return result
        if not journal_path.exists():
            context = dict(lock["context"])
            self._validate_historical_policy(context)
            source = load_release(
                self.repo,
                self.commit,
                self.capability_id,
                require_clean_worktree=False,
            )
            expected_source = {
                "capability_id": self.capability_id,
                "manifest_fingerprint": source.manifest_fingerprint,
                "payload_fingerprint": source.payload_fingerprint,
                "source_commit": source.source_commit,
                "source_tree": source.source_tree,
                "tool": self.target.spec.tool,
            }
            for key, value in expected_source.items():
                if context.get(key) != value:
                    raise CapabilityError(f"Lock-only deploy source mismatch for {key}")
            prior = context.get("prior_state")
            if not isinstance(prior, dict) or not self._snapshot_matches(prior):
                raise CapabilityError("Lock-only deploy target state changed")
            if prior.get("present"):
                if not self.target.active_path.is_file():
                    raise CapabilityError("Lock-only deploy active state is missing")
                self._validate_active_link(
                    read_json(self.target.active_path, "active install"), prior
                )
            elif self.target.active_path.exists():
                raise CapabilityError("Lock-only deploy active state appeared")
            self._validate_restored_scan_inventory(context, prior)
            approval, raw, approval_sha = load_approval(
                approval_path,
                context=context,
                required_action="recover-deploy",
                target=self.target,
                allow_expired=True,
            )
            self._validate_lock_intent(lock, approval, approval_sha, context)
            return self._record_lock_only_abort(
                lock=lock,
                context=context,
                approval_raw=raw,
                approval_sha256=approval_sha,
            )
        journal = read_json(journal_path)
        if journal.get("state") == "aborted_no_writes":
            context = journal.get("context")
            if not isinstance(context, dict):
                raise CapabilityError("Lock-only deploy abort context is invalid")
            self._validate_historical_policy(context)
            approval, raw, approval_sha = load_approval(
                approval_path,
                context=context,
                required_action="recover-deploy",
                target=self.target,
                allow_expired=True,
            )
            self._validate_lock_intent(lock, approval, approval_sha, context)
            return self._record_lock_only_abort(
                lock=lock,
                context=context,
                approval_raw=raw,
                approval_sha256=approval_sha,
            )
        source = load_release(
            self.repo,
            self.commit,
            self.capability_id,
            require_clean_worktree=False,
        )
        context, marker, _, backup = self._validate_deploy_journal(
            journal, operation, transaction_id, source
        )
        self._validate_historical_policy(context)
        approval, raw, approval_sha = load_approval(
            approval_path,
            context=context,
            required_action="recover-deploy",
            target=self.target,
            allow_expired=True,
        )
        if (
            journal.get("approval_id") != approval.get("approval_id")
            or journal.get("approval_sha256") != approval_sha
        ):
            raise CapabilityError("Deploy recovery approval mismatch")
        stored = operation / "approval-receipt.json"
        if not stored.exists() and journal.get("state") == "preparing":
            atomic_write_bytes(stored, raw)
            self._checkpoint(
                journal_path, journal, "approval_recorded", crash_after
            )
        elif not stored.is_file() or stored.read_bytes() != raw:
            raise CapabilityError("Stored deploy approval receipt mismatch")
        current_catalog = self._validate_policy_snapshot()
        source_revocation = (
            matching_revocation(
                current_catalog,
                self.capability_id,
                source,
            )
            if current_catalog is not None
            else None
        )
        if journal.get("state") == "committed" and source_revocation is None:
            current_catalog = self._validate_policy_snapshot()
            self._assert_release_not_revoked(source, current_catalog)
            active, active_operation, active_journal, active_context = (
                self._load_active_deploy(source)
            )
            if (
                active.get("deploy_transaction_id") != transaction_id
                or not same_path(active_operation, operation)
                or active_journal != journal
                or active_context != context
            ):
                raise CapabilityError("Committed deploy active link mismatch")
            self._validate_historical_policy(active_context)
            self._validate_prior_backup(
                context, backup, label="Committed deploy recovery"
            )
            self._validate_prior_active_evidence(
                context,
                operation,
                journal,
                transaction_id,
                label="Committed deploy recovery",
            )
            if scan_root_inventory(self.target.scan_root) != active.get(
                "scan_root_inventory_sha256"
            ):
                raise CapabilityError("Committed deploy scan-root state drifted")
            result = {
                "action": "recover-deploy",
                "status": "COMMITTED",
                "transaction_id": transaction_id,
                "target_present": True,
            }
            self._record_recovery_result(
                operation,
                action="recover-deploy",
                approval_sha256=approval_sha,
                result=result,
            )
            self._release(transaction_id)
            return result
        self._validate_prior_active_recovery_evidence(
            context,
            operation,
            journal,
            transaction_id,
            crash_after,
        )
        prior = context.get("prior_state")
        if not isinstance(prior, dict):
            raise CapabilityError("Deploy recovery prior state is invalid")
        prior_revocation = journal.get("prior_release_revocation")
        if prior_revocation is None:
            current_catalog = self._validate_policy_snapshot()
            prior_revocation = self._matching_prior_revocation(
                prior, current_catalog
            )
        if prior_revocation is not None:
            if not isinstance(prior_revocation, dict):
                raise CapabilityError("Deploy recovery prior revocation is invalid")
            return self._recover_deploy_without_revoked_prior(
                operation=operation,
                journal_path=journal_path,
                journal=journal,
                context=context,
                source=source,
                marker=marker,
                backup=backup,
                approval_sha256=approval_sha,
                prior_revocation=prior_revocation,
                crash_after=crash_after,
            )
        current_catalog = self._validate_policy_snapshot()
        if self._matching_prior_revocation(prior, current_catalog) is not None:
            raise CapabilityError(
                "Prior release became revoked before deploy recovery restore"
            )
        quarantine = (
            self.target.quarantine_root / f"deploy-{transaction_id}-new-target"
        )
        self._restore_prior_target(
            context=context,
            backup=backup,
            new_source=source,
            marker=marker,
            quarantine=quarantine,
        )
        if crash_after == "recovery_target_restored":
            raise InjectedCrash("recovery_target_restored")
        current_catalog = self._validate_policy_snapshot()
        prior_revocation = self._matching_prior_revocation(
            prior, current_catalog
        )
        if prior_revocation is not None:
            return self._recover_deploy_without_revoked_prior(
                operation=operation,
                journal_path=journal_path,
                journal=journal,
                context=context,
                source=source,
                marker=marker,
                backup=backup,
                approval_sha256=approval_sha,
                prior_revocation=prior_revocation,
                crash_after=crash_after,
            )
        self._restore_prior_active(operation, journal, transaction_id)
        current_catalog = self._validate_policy_snapshot()
        prior_revocation = self._matching_prior_revocation(
            prior, current_catalog
        )
        if prior_revocation is not None:
            return self._recover_deploy_without_revoked_prior(
                operation=operation,
                journal_path=journal_path,
                journal=journal,
                context=context,
                source=source,
                marker=marker,
                backup=backup,
                approval_sha256=approval_sha,
                prior_revocation=prior_revocation,
                crash_after=crash_after,
            )
        self._checkpoint(
            journal_path, journal, "recovered_rollback", crash_after
        )
        result = {
            "action": "recover-deploy",
            "status": "ROLLED_BACK",
            "transaction_id": transaction_id,
            "prior_state_restored": True,
            "target_present": bool(context["prior_state"]["present"]),
            "quarantine": str(quarantine) if quarantine.exists() else None,
            "quarantine_retained": quarantine.exists(),
        }
        self._record_recovery_result(
            operation,
            action="recover-deploy",
            approval_sha256=approval_sha,
            result=result,
        )
        self._release(transaction_id)
        return result

    def _load_active_deploy(
        self, source: GitSource
    ) -> tuple[dict[str, Any], Path, dict[str, Any], dict[str, Any]]:
        if not self.target.active_path.is_file():
            raise CapabilityError("No active generic capability install record exists")
        active = read_json(self.target.active_path, "active install")
        snapshot = snapshot_target(self.target)
        if not snapshot.get("present"):
            raise CapabilityError("Active capability target is absent")
        self._validate_active_link(active, snapshot)
        transaction_id = self._validate_active_shape(active)
        operation = self._operation_dir(transaction_id)
        journal = read_json(operation / "journal.json")
        context, marker, _, _ = self._validate_deploy_journal(
            journal, operation, transaction_id, source
        )
        if journal.get("state") != "committed":
            raise CapabilityError("Active deploy transaction is not committed")
        expected = {
            "capability_id": self.capability_id,
            "context_sha256": canonical_json_hash(context),
            "manifest_fingerprint": source.manifest_fingerprint,
            "operation_kind": context.get("operation_kind"),
            "payload_fingerprint": source.payload_fingerprint,
            "schema_version": 1,
            "source_commit": source.source_commit,
            "source_tree": source.source_tree,
            "tool": self.target.spec.tool,
        }
        for key, value in expected.items():
            if active.get(key) != value:
                raise CapabilityError(f"Active install mismatch for {key}")
        self._verify_new_target(source, marker)
        return active, operation, journal, context

    def _validate_fresh_rollback_approval(
        self,
        approval: dict[str, Any],
        deploy_journal: dict[str, Any],
        mutation_action: str,
    ) -> None:
        if mutation_action not in {"rollback", "emergency-rollback"}:
            raise CapabilityError("Rollback approval action is invalid")
        expected_actions = sorted([mutation_action, "recover-rollback"])
        if approval.get("authorized_actions") != expected_actions:
            raise CapabilityError(
                "Rollback approval must authorize only the requested rollback "
                "and recover-rollback"
            )
        if approval.get("approval_id") == deploy_journal.get("approval_id"):
            raise CapabilityError("Rollback requires a new post-activation approval ID")
        activated_at = parse_timestamp(
            str(deploy_journal.get("activated_at", "")), "activated_at"
        )
        approved_at = parse_timestamp(
            str(approval.get("approved_at", "")), "approved_at"
        )
        if approved_at < activated_at:
            raise CapabilityError("Rollback approval predates target activation")
        transaction_id = str(deploy_journal.get("transaction_id", ""))
        decision_fragment = f"ROLLBACK DEPLOY {transaction_id} AFTER ACTIVATION"
        if decision_fragment not in str(approval.get("owner_decision", "")):
            raise CapabilityError(
                f"Rollback owner decision must contain {decision_fragment}"
            )
        approval_context = approval.get("context")
        if (
            isinstance(approval_context, dict)
            and approval_context.get("prior_release_revocation") is not None
        ):
            removal_fragment = (
                f"REMOVE ACTIVE WITHOUT RESTORE {transaction_id}"
            )
            if removal_fragment not in str(approval.get("owner_decision", "")):
                raise CapabilityError(
                    "Revoked-prior rollback owner decision must contain "
                    f"{removal_fragment}"
                )

    def _rollback_recovery_precondition_drift(
        self,
        *,
        approval_context: dict[str, Any],
        source: GitSource,
        deploy_operation: Path,
        deploy_journal: dict[str, Any],
        deploy_context: dict[str, Any],
        deploy_transaction_id: str,
        mutation_action: str,
    ) -> str | None:
        try:
            current_guards = guard_hashes(self.target)
        except CapabilityError:
            return "guard-drift"
        if approval_context.get("guard_hashes") != current_guards:
            return "guard-drift"
        try:
            current_inventory = scan_root_inventory(self.target.scan_root)
        except CapabilityError:
            return "scan-root-inventory-drift"
        if current_inventory != approval_context.get(
            "scan_root_inventory_sha256"
        ):
            return "scan-root-inventory-drift"
        if mutation_action == "rollback":
            try:
                discovery = self._discovery_status(
                    deploy_operation,
                    source,
                    deploy_context,
                    deploy_transaction_id,
                    deploy_journal,
                )
            except CapabilityError:
                return "discovery-drift"
            if discovery.get("status") != "RECORDED":
                return "discovery-drift"
        return None

    def _finish_rollback_recovery_precondition_abort(
        self,
        *,
        operation: Path,
        journal_path: Path,
        journal: dict[str, Any],
        approval_sha256: str,
        reason: str,
        crash_after: str | None,
    ) -> dict[str, Any]:
        if reason not in {
            "discovery-drift",
            "guard-drift",
            "scan-root-inventory-drift",
        }:
            raise CapabilityError("Rollback recovery drift reason is invalid")
        if journal.get("state") == "recovery_aborted_precondition_drift":
            if journal.get("recovery_abort_reason") != reason:
                raise CapabilityError("Rollback recovery drift reason changed")
        else:
            journal["recovery_abort_reason"] = reason
            self._checkpoint(
                journal_path,
                journal,
                "recovery_aborted_precondition_drift",
                crash_after,
            )
        result = {
            "action": "recover-rollback",
            "reason": reason,
            "status": "ABORTED_PRECONDITION_DRIFT",
            "transaction_id": str(journal["transaction_id"]),
            "target_present": self.target.target.exists(),
            "writes_scope": "transaction-state-only",
        }
        self._record_recovery_result(
            operation,
            action="recover-rollback",
            approval_sha256=approval_sha256,
            result=result,
        )
        self._release(str(journal["transaction_id"]))
        return result

    def plan_rollback(self, *, emergency: bool = False) -> dict[str, Any]:
        (
            source,
            active,
            operation,
            journal,
            deploy_context,
            authorization,
        ) = self._rollback_state()
        transaction_id = str(active["deploy_transaction_id"])
        mutation_action = "emergency-rollback" if emergency else "rollback"
        discovery = (
            {"status": "BYPASSED_EMERGENCY"}
            if emergency
            else self._discovery_status(
                operation,
                source,
                deploy_context,
                transaction_id,
                journal,
            )
        )
        status = (
            "READY"
            if emergency or discovery.get("status") == "RECORDED"
            else "BLOCKED_DISCOVERY"
        )
        prior_revocation = authorization["prior_release_revocation"]
        rollback_fragment = (
            f"ROLLBACK DEPLOY {transaction_id} AFTER ACTIVATION"
        )
        removal_fragment = (
            f"REMOVE ACTIVE WITHOUT RESTORE {transaction_id}"
            if prior_revocation is not None
            else None
        )
        return {
            "action": (
                "plan-emergency-rollback" if emergency else "plan-rollback"
            ),
            "active_release_revocation": authorization[
                "active_release_revocation"
            ],
            "active_release_revoked": (
                authorization["active_release_revocation"] is not None
            ),
            "prior_release_revocation": prior_revocation,
            "prior_release_revoked": prior_revocation is not None,
            "remove_without_restore": prior_revocation is not None,
            "status": status,
            "approval_context": authorization,
            "approval_context_sha256": canonical_json_hash(authorization),
            "deploy_approval_id_forbidden": journal["approval_id"],
            "deploy_transaction_id": transaction_id,
            "discovery": discovery,
            "required_actions": sorted([mutation_action, "recover-rollback"]),
            "required_owner_decision_fragment": rollback_fragment,
            "required_owner_decision_fragments": [
                fragment
                for fragment in (rollback_fragment, removal_fragment)
                if fragment is not None
            ],
            "required_remove_without_restore_fragment": removal_fragment,
            "rollback_approval_not_before": journal["activated_at"],
            "writes_performed": 0,
            "security_statement": SECURITY_STATEMENT,
        }

    def emergency_rollback(
        self,
        approval_path: Path | str,
        *,
        owner_authorized: bool,
        crash_after: str | None = None,
    ) -> dict[str, Any]:
        return self.rollback(
            approval_path,
            owner_authorized=owner_authorized,
            crash_after=crash_after,
            _mutation_action="emergency-rollback",
        )

    def rollback(
        self,
        approval_path: Path | str,
        *,
        owner_authorized: bool,
        crash_after: str | None = None,
        _lease_token: object | None = None,
        _mutation_action: str = "rollback",
    ) -> dict[str, Any]:
        if _mutation_action not in {"rollback", "emergency-rollback"}:
            raise CapabilityError("Rollback mutation action is invalid")
        if not owner_authorized:
            raise CapabilityError("Rollback requires explicit owner authorization")
        if crash_after and self.target.fixture_root is None:
            raise CapabilityError("Failure injection is allowed only in a TEMP fixture")
        if not self.target.lock_path.exists():
            replayed = self._replay_mutation_result(
                approval_path, action=_mutation_action
            )
            if replayed is not None:
                return replayed
        (
            source,
            active,
            deploy_operation,
            deploy_journal,
            deploy_context,
            authorization,
        ) = self._rollback_state()
        if _mutation_action == "rollback":
            discovery = self._discovery_status(
                deploy_operation,
                source,
                deploy_context,
                str(active["deploy_transaction_id"]),
                deploy_journal,
            )
            if discovery.get("status") != "RECORDED":
                raise CapabilityError(
                    "Standard rollback requires recorded fresh-session discovery; "
                    "use separately approved emergency-rollback when discovery failed"
                )
        backup = deploy_operation / "prior-target"
        prior = self._validate_prior_backup(
            deploy_context, backup, label="Rollback"
        )
        self._validate_prior_active_evidence(
            deploy_context,
            deploy_operation,
            deploy_journal,
            str(active["deploy_transaction_id"]),
            label="Rollback",
        )
        approval, raw, approval_sha = load_approval(
            approval_path,
            context=authorization,
            required_action=_mutation_action,
            target=self.target,
        )
        self._validate_fresh_rollback_approval(
            approval, deploy_journal, _mutation_action
        )
        if _lease_token is not self._lease_token:
            with self._operation_lease():
                return self.rollback(
                    approval_path,
                    owner_authorized=owner_authorized,
                    crash_after=crash_after,
                    _lease_token=self._lease_token,
                    _mutation_action=_mutation_action,
                )
        (
            fresh_source,
            fresh_active,
            fresh_deploy_operation,
            fresh_deploy_journal,
            fresh_deploy_context,
            fresh_authorization,
        ) = self._rollback_state()
        fresh, _, fresh_sha = load_approval(
            approval_path,
            context=fresh_authorization,
            required_action=_mutation_action,
            target=self.target,
        )
        self._validate_fresh_rollback_approval(
            fresh, fresh_deploy_journal, _mutation_action
        )
        if (
            fresh_source.source_tree != source.source_tree
            or fresh_active != active
            or not same_path(fresh_deploy_operation, deploy_operation)
            or fresh_deploy_journal != deploy_journal
            or fresh_deploy_context != deploy_context
            or fresh_authorization != authorization
            or fresh_sha != approval_sha
            or fresh.get("approval_id") != approval.get("approval_id")
        ):
            raise CapabilityError(
                "Active target, policy or approval changed before rollback lock"
            )
        if self._approval_used(str(approval["approval_id"])):
            raise CapabilityError("Approval ID has already authorized a mutation")
        transaction_id = uuid.uuid4().hex
        context_sha = canonical_json_hash(deploy_context)
        self._acquire(
            transaction_id,
            "rollback",
            approval_id=str(approval["approval_id"]),
            approval_sha256=approval_sha,
            context=authorization,
            requested_action=_mutation_action,
        )
        if crash_after == "lock_acquired":
            raise InjectedCrash("lock_acquired")
        operation = self._operation_dir(transaction_id)
        journal_path = operation / "journal.json"
        try:
            (
                fresh_source,
                fresh_active,
                fresh_deploy_operation,
                fresh_deploy_journal,
                fresh_deploy_context,
                fresh_authorization,
            ) = self._rollback_state(allow_lock=True)
            fresh, _, fresh_sha = load_approval(
                approval_path,
                context=fresh_authorization,
                required_action=_mutation_action,
                target=self.target,
            )
            self._validate_fresh_rollback_approval(
                fresh, fresh_deploy_journal, _mutation_action
            )
            self._validate_prior_backup(
                deploy_context, backup, label="Rollback"
            )
            self._validate_prior_active_evidence(
                deploy_context,
                deploy_operation,
                deploy_journal,
                str(active["deploy_transaction_id"]),
                label="Rollback",
            )
            if (
                fresh_source.source_tree != source.source_tree
                or fresh_active != active
                or not same_path(fresh_deploy_operation, deploy_operation)
                or fresh_deploy_journal != deploy_journal
                or fresh_deploy_context != deploy_context
                or fresh_authorization != authorization
                or fresh_sha != approval_sha
                or fresh.get("approval_id") != approval.get("approval_id")
            ):
                raise CapabilityError(
                    "Active target, policy or approval changed before rollback journal"
                )
            operation.mkdir(mode=0o700)
            quarantine = (
                self.target.quarantine_root
                / f"rollback-{transaction_id}-current-target"
            )
            journal: dict[str, Any] = {
                "approval_id": approval["approval_id"],
                "approval_sha256": approval_sha,
                "approval_context": authorization,
                "approval_context_sha256": canonical_json_hash(authorization),
                "context": deploy_context,
                "context_sha256": context_sha,
                "created_at": utc_now(),
                "deploy_operation_dir": str(Path(os.path.abspath(deploy_operation))),
                "deploy_transaction_id": active["deploy_transaction_id"],
                "mutation_action": _mutation_action,
                "operation": "rollback",
                "operation_dir": str(Path(os.path.abspath(operation))),
                "prior_release_revocation": authorization[
                    "prior_release_revocation"
                ],
                "quarantine": str(Path(os.path.abspath(quarantine))),
                "schema_version": 1,
                "transaction_id": transaction_id,
            }
            self._checkpoint(journal_path, journal, "rollback_preparing", crash_after)
            atomic_write_bytes(operation / "approval-receipt.json", raw)
            self._checkpoint(journal_path, journal, "approval_recorded", crash_after)
            (
                final_source,
                final_active,
                final_deploy_operation,
                final_deploy_journal,
                final_deploy_context,
                final_authorization,
            ) = self._rollback_state(allow_lock=True)
            if (
                final_source.source_tree != source.source_tree
                or final_active != active
                or not same_path(final_deploy_operation, deploy_operation)
                or final_deploy_journal != deploy_journal
                or final_deploy_context != deploy_context
                or final_authorization != authorization
            ):
                raise CapabilityError(
                    "Active target or rollback authorization changed before mutation"
                )
            if _mutation_action == "rollback":
                final_discovery = self._discovery_status(
                    deploy_operation,
                    source,
                    deploy_context,
                    str(active["deploy_transaction_id"]),
                    deploy_journal,
                )
                if final_discovery.get("status") != "RECORDED":
                    raise CapabilityError(
                        "Standard rollback discovery changed before mutation"
                    )
            self._validate_prior_backup(
                deploy_context, backup, label="Rollback"
            )
            self._validate_prior_active_evidence(
                deploy_context,
                deploy_operation,
                deploy_journal,
                str(active["deploy_transaction_id"]),
                label="Rollback",
            )
            self._checkpoint(journal_path, journal, "rollback_prepared", crash_after)
            mutation_catalog = self._validate_policy_snapshot()
            if self._matching_prior_revocation(
                prior, mutation_catalog
            ) != authorization["prior_release_revocation"]:
                raise CapabilityError(
                    "Prior release revocation changed before rollback mutation"
                )
            os.replace(self.target.target, quarantine)
            fsync_parent(quarantine)
            if crash_after == "rollback_target_renamed":
                raise InjectedCrash("rollback_target_renamed")
            marker = deploy_journal["marker"]
            if not self._matches_directory_new(quarantine, source, marker):
                raise CapabilityError("Rollback quarantine verification failed")
            self._checkpoint(
                journal_path, journal, "current_quarantined", crash_after
            )
            prior_revocation = authorization["prior_release_revocation"]
            if prior_revocation is not None:
                if self.target.target.exists():
                    raise CapabilityError(
                        "Revoked-prior rollback target state is ambiguous"
                    )
                if not backup.is_dir():
                    raise CapabilityError(
                        "Revoked-prior rollback retained backup is missing"
                    )
                if self.target.active_path.exists():
                    if not self.target.active_path.is_file():
                        raise CapabilityError(
                            "Revoked-prior rollback active record is not a plain file"
                        )
                    current_active = read_json(
                        self.target.active_path, "active install"
                    )
                    if current_active != active:
                        raise CapabilityError(
                            "Revoked-prior rollback active record changed"
                        )
                    self._validate_policy_snapshot()
                    self.target.active_path.unlink()
                    fsync_parent(self.target.active_path)
                self._checkpoint(
                    journal_path,
                    journal,
                    "prior_revoked_not_restored",
                    crash_after,
                )
                result = {
                    "action": _mutation_action,
                    "backup_retained": True,
                    "deploy_transaction_id": active["deploy_transaction_id"],
                    "prior_release_revocation": prior_revocation,
                    "prior_state_restored": False,
                    "quarantine": str(quarantine),
                    "quarantine_retained": True,
                    "revoked_prior_quarantine": None,
                    "revoked_prior_quarantine_retained": False,
                    "rules_or_config_mutation": False,
                    "status": "REMOVED_WITHOUT_RESTORE",
                    "target_present": False,
                    "transaction_id": transaction_id,
                }
                self._record_mutation_result(
                    operation,
                    action=_mutation_action,
                    approval_sha256=approval_sha,
                    result=result,
                )
                self._release(transaction_id)
                return result
            if prior["present"]:
                os.replace(backup, self.target.target)
                fsync_parent(self.target.target)
                if not self._snapshot_matches(prior):
                    raise CapabilityError("Rollback restored target verification failed")
            elif self.target.target.exists():
                raise CapabilityError("Rollback did not restore exact target absence")
            self._checkpoint(journal_path, journal, "prior_restored", crash_after)
            self._restore_prior_active(
                deploy_operation, deploy_journal, str(active["deploy_transaction_id"])
            )
            self._checkpoint(journal_path, journal, "active_restored", crash_after)
            self._checkpoint(journal_path, journal, "rolled_back", crash_after)
            result = {
                "action": _mutation_action,
                "status": "ROLLED_BACK",
                "transaction_id": transaction_id,
                "deploy_transaction_id": active["deploy_transaction_id"],
                "target_present": bool(prior["present"]),
                "prior_state_restored": True,
                "quarantine": str(quarantine),
                "quarantine_retained": True,
                "rules_or_config_mutation": False,
            }
            self._record_mutation_result(
                operation,
                action=_mutation_action,
                approval_sha256=approval_sha,
                result=result,
            )
            self._release(transaction_id)
            return result
        except InjectedCrash:
            raise
        except Exception:
            if not journal_path.exists() and self.target.lock_path.exists():
                self._release(transaction_id)
            raise

    def recover_rollback(
        self,
        approval_path: Path | str,
        *,
        owner_authorized: bool,
        crash_after: str | None = None,
    ) -> dict[str, Any]:
        if not owner_authorized:
            raise CapabilityError("Rollback recovery requires owner authorization")
        if not self.target.lock_path.is_file():
            return self._replay_recovery_result(
                approval_path, action="recover-rollback"
            )
        with self._operation_lease():
            if not self.target.lock_path.is_file():
                return self._replay_recovery_result(
                    approval_path, action="recover-rollback"
                )
            return self._recover_rollback_locked(
                approval_path,
                owner_authorized=owner_authorized,
                crash_after=crash_after,
            )

    def _recover_rollback_locked(
        self,
        approval_path: Path | str,
        *,
        owner_authorized: bool,
        crash_after: str | None = None,
    ) -> dict[str, Any]:
        if not owner_authorized:
            raise CapabilityError("Rollback recovery requires owner authorization")
        if crash_after and self.target.fixture_root is None:
            raise CapabilityError("Failure injection is allowed only in a TEMP fixture")
        validate_paths(self.target)
        current_catalog = self._validate_policy_snapshot()
        if not self.target.lock_path.is_file():
            raise CapabilityError("No interrupted capability operation lock exists")
        lock = self._read_lock()
        transaction_id = str(lock.get("transaction_id", ""))
        if (
            lock.get("operation") != "rollback"
            or not TRANSACTION_ID.fullmatch(transaction_id)
        ):
            raise CapabilityError("Active lock is not a rollback transaction")
        operation = self._operation_dir(transaction_id)
        journal_path = operation / "journal.json"
        if (operation / "recovery-result.json").exists():
            result = self._replay_recovery_result(
                approval_path, action="recover-rollback"
            )
            self._release(transaction_id)
            return result
        if not journal_path.exists():
            source = load_release(
                self.repo,
                self.commit,
                self.capability_id,
                require_clean_worktree=False,
            )
            deploy_context, approval_context = (
                self._rollback_contexts_from_lock(lock)
            )
            _, deploy_operation, deploy_journal, active_context = (
                self._load_active_deploy(source)
            )
            if active_context != deploy_context:
                raise CapabilityError("Lock-only rollback active context changed")
            self._validate_prior_backup(
                deploy_context,
                deploy_operation / "prior-target",
                label="Rollback recovery",
            )
            active = read_json(self.target.active_path, "active install")
            self._validate_prior_active_evidence(
                deploy_context,
                deploy_operation,
                deploy_journal,
                str(active["deploy_transaction_id"]),
                label="Rollback recovery",
            )
            approval, raw, approval_sha = load_approval(
                approval_path,
                context=approval_context,
                required_action="recover-rollback",
                target=self.target,
                allow_expired=True,
            )
            mutation_action = self._rollback_action_for_lock(lock, approval)
            self._validate_fresh_rollback_approval(
                approval, deploy_journal, mutation_action
            )
            self._validate_lock_intent(
                lock, approval, approval_sha, approval_context
            )
            return self._record_lock_only_abort(
                lock=lock,
                context=deploy_context,
                approval_raw=raw,
                approval_sha256=approval_sha,
                approval_context=approval_context,
                mutation_action=mutation_action,
            )
        journal = read_json(journal_path)
        if journal.get("state") == "aborted_no_writes":
            deploy_context, approval_context = (
                self._rollback_contexts_from_journal(journal)
            )
            approval, raw, approval_sha = load_approval(
                approval_path,
                context=approval_context,
                required_action="recover-rollback",
                target=self.target,
                allow_expired=True,
            )
            source = load_release(
                self.repo,
                self.commit,
                self.capability_id,
                require_clean_worktree=False,
            )
            _, _, deploy_journal, active_context = self._load_active_deploy(source)
            if active_context != deploy_context:
                raise CapabilityError("Lock-only rollback active context changed")
            mutation_action = self._rollback_action_for_lock(
                lock, approval, journal
            )
            self._validate_fresh_rollback_approval(
                approval, deploy_journal, mutation_action
            )
            self._validate_lock_intent(
                lock, approval, approval_sha, approval_context
            )
            return self._record_lock_only_abort(
                lock=lock,
                context=deploy_context,
                approval_raw=raw,
                approval_sha256=approval_sha,
                approval_context=approval_context,
                mutation_action=mutation_action,
            )
        if (
            journal.get("schema_version") != 1
            or journal.get("operation") != "rollback"
            or journal.get("mutation_action")
            not in {"rollback", "emergency-rollback"}
            or journal.get("transaction_id") != transaction_id
            or not same_path(str(journal.get("operation_dir", "")), operation)
        ):
            raise CapabilityError("Rollback journal identity is invalid")
        deploy_id = str(journal.get("deploy_transaction_id", ""))
        deploy_operation = self._operation_dir(deploy_id)
        if not same_path(
            str(journal.get("deploy_operation_dir", "")), deploy_operation
        ):
            raise CapabilityError("Rollback journal deploy path mismatch")
        deploy_journal = read_json(deploy_operation / "journal.json")
        context, approval_context = self._rollback_contexts_from_journal(
            journal
        )
        if (
            deploy_journal.get("context") != context
            or journal.get("context_sha256") != canonical_json_hash(context)
        ):
            raise CapabilityError("Rollback recovery context mismatch")
        source = load_release(
            self.repo,
            self.commit,
            self.capability_id,
            require_clean_worktree=False,
        )
        _, marker, _, backup = self._validate_deploy_journal(
            deploy_journal, deploy_operation, deploy_id, source
        )
        self._validate_prior_active_evidence(
            context,
            deploy_operation,
            deploy_journal,
            deploy_id,
            label="Rollback recovery",
        )
        approval, raw, approval_sha = load_approval(
            approval_path,
            context=approval_context,
            required_action="recover-rollback",
            target=self.target,
            allow_expired=True,
        )
        mutation_action = self._rollback_action_for_lock(
            lock, approval, journal
        )
        self._validate_fresh_rollback_approval(
            approval, deploy_journal, mutation_action
        )
        self._validate_lock_intent(
            lock, approval, approval_sha, approval_context
        )
        stored = operation / "approval-receipt.json"
        if not stored.exists() and journal.get("state") == "rollback_preparing":
            atomic_write_bytes(stored, raw)
            self._checkpoint(
                journal_path, journal, "approval_recorded", crash_after
            )
        if (
            journal.get("approval_id") != approval.get("approval_id")
            or journal.get("approval_sha256") != approval_sha
            or not stored.is_file()
            or stored.read_bytes() != raw
        ):
            raise CapabilityError("Rollback recovery approval mismatch")
        if journal.get("state") == "recovery_aborted_precondition_drift":
            return self._finish_rollback_recovery_precondition_abort(
                operation=operation,
                journal_path=journal_path,
                journal=journal,
                approval_sha256=approval_sha,
                reason=str(journal.get("recovery_abort_reason", "")),
                crash_after=crash_after,
            )
        quarantine = Path(str(journal.get("quarantine", "")))
        expected_quarantine = (
            self.target.quarantine_root
            / f"rollback-{transaction_id}-current-target"
        )
        if not same_path(quarantine, expected_quarantine):
            raise CapabilityError("Rollback recovery quarantine path mismatch")
        prior = context["prior_state"]
        if not isinstance(prior, dict) or not isinstance(
            prior.get("present"), bool
        ):
            raise CapabilityError("Rollback recovery prior state is invalid")
        current_catalog = self._validate_policy_snapshot()
        current_prior_revocation = self._matching_prior_revocation(
            prior, current_catalog
        )
        prior_revocation = journal.get("prior_release_revocation")
        if prior_revocation is not None:
            if (
                not isinstance(prior_revocation, dict)
                or set(prior_revocation) != REVOCATION_ENTRY_KEYS
                or prior_revocation.get("capability_id") != self.capability_id
                or prior_revocation.get("source_tree") != prior.get("source_tree")
                or prior_revocation.get("manifest_fingerprint")
                != prior.get("manifest_fingerprint")
                or prior_revocation.get("payload_fingerprint")
                != prior.get("payload_fingerprint")
            ):
                raise CapabilityError(
                    "Rollback recovery stored prior revocation is invalid"
                )
            if (
                current_prior_revocation is not None
                and current_prior_revocation != prior_revocation
            ):
                raise CapabilityError(
                    "Rollback recovery prior revocation changed"
                )
        else:
            prior_revocation = current_prior_revocation
            if prior_revocation is not None:
                journal["prior_release_revocation"] = prior_revocation
                self._checkpoint(
                    journal_path,
                    journal,
                    "prior_revocation_recorded",
                    crash_after,
                )

        revoked_prior_quarantine = self.target.quarantine_root / (
            f"rollback-{transaction_id}-revoked-prior"
        )
        if self.target.target.exists():
            target_is_current = self._matches_new(source, marker)
            target_is_prior = bool(
                prior.get("present") and self._snapshot_matches(prior)
            )
            if target_is_current:
                self._validate_prior_backup(
                    context, backup, label="Rollback recovery"
                )
                if quarantine.exists():
                    raise CapabilityError(
                        "Rollback target and quarantine are both current"
                    )
                drift = self._rollback_recovery_precondition_drift(
                    approval_context=approval_context,
                    source=source,
                    deploy_operation=deploy_operation,
                    deploy_journal=deploy_journal,
                    deploy_context=context,
                    deploy_transaction_id=deploy_id,
                    mutation_action=mutation_action,
                )
                if drift is not None:
                    return self._finish_rollback_recovery_precondition_abort(
                        operation=operation,
                        journal_path=journal_path,
                        journal=journal,
                        approval_sha256=approval_sha,
                        reason=drift,
                        crash_after=crash_after,
                    )
                self._validate_policy_snapshot()
                os.replace(self.target.target, quarantine)
                fsync_parent(quarantine)
                if crash_after == "recovery_rollback_target_renamed":
                    raise InjectedCrash("recovery_rollback_target_renamed")
            elif target_is_prior and prior_revocation is not None:
                if backup.exists() or revoked_prior_quarantine.exists():
                    raise CapabilityError(
                        "Rollback recovery revoked prior retention is ambiguous"
                    )
                self._validate_policy_snapshot()
                os.replace(self.target.target, revoked_prior_quarantine)
                fsync_parent(revoked_prior_quarantine)
                if crash_after == "recovery_revoked_prior_quarantined":
                    raise InjectedCrash("recovery_revoked_prior_quarantined")
            elif not target_is_prior:
                raise CapabilityError(
                    "Rollback recovery target is neither current nor prior"
                )
        if not quarantine.is_dir() or not self._matches_directory_new(
            quarantine, source, marker
        ):
            raise CapabilityError("Rollback recovery quarantine is missing or changed")
        if prior_revocation is not None:
            if backup.exists() and (
                not backup.is_dir()
                or snapshot_directory(
                    backup,
                    capability_id=self.capability_id,
                    tool=self.target.spec.tool,
                )
                != prior
            ):
                raise CapabilityError("Rollback recovery revoked prior backup changed")
            if revoked_prior_quarantine.exists() and (
                not revoked_prior_quarantine.is_dir()
                or snapshot_directory(
                    revoked_prior_quarantine,
                    capability_id=self.capability_id,
                    tool=self.target.spec.tool,
                )
                != prior
            ):
                raise CapabilityError(
                    "Rollback recovery revoked prior quarantine changed"
                )
            if backup.exists() == revoked_prior_quarantine.exists():
                raise CapabilityError(
                    "Rollback recovery must retain revoked prior exactly once"
                )
            if self.target.target.exists():
                raise CapabilityError(
                    "Rollback recovery revoked prior target state is ambiguous"
                )
            if self.target.active_path.exists():
                if not self.target.active_path.is_file():
                    raise CapabilityError(
                        "Rollback recovery active record is not a plain file"
                    )
                active_raw = self.target.active_path.read_bytes()
                active = read_json(self.target.active_path, "active install")
                if active.get("deploy_transaction_id") == deploy_id:
                    self._validate_active_shape(active)
                else:
                    prior_active = deploy_operation / "prior-active.json"
                    expected_hash = deploy_journal.get("prior_active_sha256")
                    if (
                        not isinstance(expected_hash, str)
                        or not prior_active.is_file()
                        or sha256_bytes(active_raw) != expected_hash
                        or active_raw != prior_active.read_bytes()
                    ):
                        raise CapabilityError(
                            "Rollback recovery active record is neither current nor prior"
                        )
                self._validate_policy_snapshot()
                self.target.active_path.unlink()
                fsync_parent(self.target.active_path)
            self._checkpoint(
                journal_path,
                journal,
                "prior_revoked_not_restored",
                crash_after,
            )
            result = {
                "action": "recover-rollback",
                "backup_retained": backup.exists(),
                "deploy_transaction_id": deploy_id,
                "prior_release_revocation": prior_revocation,
                "prior_state_restored": False,
                "quarantine": str(quarantine),
                "quarantine_retained": True,
                "revoked_prior_quarantine": (
                    str(revoked_prior_quarantine)
                    if revoked_prior_quarantine.exists()
                    else None
                ),
                "revoked_prior_quarantine_retained": (
                    revoked_prior_quarantine.exists()
                ),
                "status": "REMOVED_WITHOUT_RESTORE",
                "target_present": False,
                "transaction_id": transaction_id,
            }
            self._record_recovery_result(
                operation,
                action="recover-rollback",
                approval_sha256=approval_sha,
                result=result,
            )
            self._release(transaction_id)
            return result
        if prior["present"]:
            if self.target.target.exists():
                if backup.exists() or not self._snapshot_matches(prior):
                    raise CapabilityError("Rollback recovery prior state is ambiguous")
            else:
                if not backup.is_dir():
                    raise CapabilityError("Rollback recovery prior backup is missing")
                if snapshot_directory(
                    backup,
                    capability_id=self.capability_id,
                    tool=self.target.spec.tool,
                ) != prior:
                    raise CapabilityError("Rollback recovery prior backup changed")
                current_catalog = self._validate_policy_snapshot()
                if self._matching_prior_revocation(prior, current_catalog) is not None:
                    raise CapabilityError(
                        "Prior release became revoked before rollback recovery restore"
                    )
                os.replace(backup, self.target.target)
                fsync_parent(self.target.target)
            if not self._snapshot_matches(prior):
                raise CapabilityError("Rollback recovery prior target verification failed")
        elif self.target.target.exists():
            raise CapabilityError("Rollback recovery did not restore exact absence")
        self._checkpoint(journal_path, journal, "prior_restored", crash_after)
        current_catalog = self._validate_policy_snapshot()
        newly_revoked = self._matching_prior_revocation(prior, current_catalog)
        if newly_revoked is not None:
            journal["prior_release_revocation"] = newly_revoked
            self._checkpoint(
                journal_path,
                journal,
                "prior_revocation_recorded",
                crash_after,
            )
            return self._recover_rollback_locked(
                approval_path,
                owner_authorized=owner_authorized,
                crash_after=crash_after,
            )
        self._restore_prior_active(deploy_operation, deploy_journal, deploy_id)
        self._checkpoint(journal_path, journal, "active_restored", crash_after)
        current_catalog = self._validate_policy_snapshot()
        newly_revoked = self._matching_prior_revocation(prior, current_catalog)
        if newly_revoked is not None:
            journal["prior_release_revocation"] = newly_revoked
            self._checkpoint(
                journal_path,
                journal,
                "prior_revocation_recorded",
                crash_after,
            )
            return self._recover_rollback_locked(
                approval_path,
                owner_authorized=owner_authorized,
                crash_after=crash_after,
            )
        self._checkpoint(journal_path, journal, "rolled_back", crash_after)
        result = {
            "action": "recover-rollback",
            "status": "ROLLED_BACK",
            "transaction_id": transaction_id,
            "deploy_transaction_id": deploy_id,
            "target_present": bool(prior["present"]),
            "prior_state_restored": True,
            "quarantine": str(quarantine),
            "quarantine_retained": True,
        }
        self._record_recovery_result(
            operation,
            action="recover-rollback",
            approval_sha256=approval_sha,
            result=result,
        )
        self._release(transaction_id)
        return result


def emit(value: object, *, stream: Any = sys.stdout) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), file=stream)


def default_registry(repo: str) -> str:
    return str(Path(repo) / REGISTRY_NAME)


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--policy-commit", required=True)
    parser.add_argument("--capability-id", required=True)
    parser.add_argument("--tool", required=True)
    parser.add_argument("--registry")
    parser.add_argument("--matrix")
    parser.add_argument("--revocations")
    parser.add_argument("--accept-robert-same-user-risk", action="store_true")
    parser.add_argument("--fixture-root")


def add_mutating_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--approval-file", required=True)
    parser.add_argument("--owner-authorized", action="store_true")
    parser.add_argument("--crash-after", help=argparse.SUPPRESS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    plan = subparsers.add_parser("plan", help="Read-only exact target plan")
    add_common_arguments(plan)
    verify = subparsers.add_parser(
        "verify", help="Read-only active target, marker and journal verification"
    )
    add_common_arguments(verify)
    discovery_plan = subparsers.add_parser(
        "plan-discovery", help="Read-only approval context for exact discovery evidence"
    )
    add_common_arguments(discovery_plan)
    discovery_plan.add_argument("--evidence-file", required=True)
    discovery = subparsers.add_parser(
        "record-discovery", help="Owner-gated fresh-session discovery evidence"
    )
    add_common_arguments(discovery)
    add_mutating_arguments(discovery)
    discovery.add_argument("--evidence-file", required=True)
    for name, help_text in (
        ("plan-rollback", "Read-only standard rollback approval context"),
        (
            "plan-emergency-rollback",
            "Read-only emergency rollback approval context",
        ),
    ):
        action = subparsers.add_parser(name, help=help_text)
        add_common_arguments(action)
    for name, help_text in (
        ("deploy", "Owner-gated first install or version update"),
        (
            "emergency-rollback",
            "Owner-gated rollback without recorded discovery",
        ),
        ("recover-deploy", "Owner-gated interrupted deploy recovery"),
        ("rollback", "Owner-gated committed deploy rollback"),
        ("recover-rollback", "Owner-gated interrupted rollback recovery"),
    ):
        action = subparsers.add_parser(name, help=help_text)
        add_common_arguments(action)
        add_mutating_arguments(action)
    return parser


def rollout_from_args(args: argparse.Namespace) -> CapabilityRollout:
    repo_path = Path(os.path.abspath(args.repo))
    registry_path = Path(args.registry or default_registry(args.repo))
    matrix_path = Path(args.matrix or (repo_path / MATRIX_NAME))
    revocation_path = Path(args.revocations or (repo_path / REVOCATION_NAME))
    rollback_or_safety_actions = {
        "emergency-rollback",
        "plan-emergency-rollback",
        "plan-rollback",
        "recover-deploy",
        "recover-rollback",
        "rollback",
        "verify",
    }
    strict_current_view = args.action in {
        "deploy",
        "emergency-rollback",
        "plan",
        "plan-discovery",
        "plan-emergency-rollback",
        "plan-rollback",
        "record-discovery",
        "recover-deploy",
        "recover-rollback",
        "rollback",
        "verify",
    }
    enforce_freshness = strict_current_view
    if not GIT_OID.fullmatch(args.commit) or not GIT_OID.fullmatch(
        args.policy_commit
    ):
        raise CapabilityError("Release and policy commits must be full Git object IDs")
    if not git_is_ancestor(repo_path, args.commit, args.policy_commit):
        raise CapabilityError("Release commit is not an ancestor of policy commit")
    if args.fixture_root is None:
        canonical_repo = Path(__file__).resolve().parents[1]
        plain_repo = absolute_plain_path(repo_path, must_exist=True)
        if not plain_repo.is_dir() or not same_path(plain_repo, canonical_repo):
            raise CapabilityError(
                "Real mode requires the canonical Capability Hub repository"
            )
        require_engine_at_ref(repo_path, "refs/heads/main")
        expected_registry = repo_path / REGISTRY_NAME
        expected_matrix = repo_path / MATRIX_NAME
        expected_revocations = repo_path / REVOCATION_NAME
        if not same_path(registry_path, expected_registry):
            raise CapabilityError(
                "Real mode requires the canonical capability-targets.json registry"
            )
        if not same_path(matrix_path, expected_matrix):
            raise CapabilityError(
                "Real mode requires the canonical capability-matrix-v1.2.json"
            )
        if not same_path(revocation_path, expected_revocations):
            raise CapabilityError(
                "Real mode requires the canonical capability-revocations.json"
            )
        if strict_current_view and git_commit_at_ref(
            repo_path, "refs/heads/main"
        ) != args.policy_commit:
            raise CapabilityError("Current operations require policy commit at local main")
        committed_registry = run_git(
            repo_path, ["show", f"{args.policy_commit}:{REGISTRY_NAME}"]
        )
        committed_matrix = run_git(
            repo_path, ["show", f"{args.policy_commit}:{MATRIX_NAME}"]
        )
        committed_revocations = run_git(
            repo_path, ["show", f"{args.policy_commit}:{REVOCATION_NAME}"]
        )
        if strict_current_view:
            comparisons = (
                (registry_path, committed_registry, "target registry"),
                (matrix_path, committed_matrix, "capability matrix"),
                (revocation_path, committed_revocations, "revocation catalog"),
            )
            for path, committed, label in comparisons:
                if absolute_plain_path(path, must_exist=True).read_bytes() != committed:
                    raise CapabilityError(f"Local {label} differs from policy commit")
        if enforce_freshness:
            origin_main = git_commit_at_ref(repo_path, "refs/remotes/origin/main")
            if not git_is_ancestor(repo_path, origin_main, args.policy_commit):
                raise CapabilityError(
                    "origin/main is not an ancestor of the current policy commit"
                )
            revocation_checked_at = remote_fetch_timestamp(
                repo_path, expected_oid=origin_main
            )
        else:
            revocation_checked_at = None
        registry = load_registry(
            f"git-{args.policy_commit}-{REGISTRY_NAME}",
            raw=committed_registry,
        )
        matrix = load_matrix(
            f"git-{args.policy_commit}-{MATRIX_NAME}",
            registry,
            raw=committed_matrix,
        )
        revocations = load_revocations(
            f"git-{args.policy_commit}-{REVOCATION_NAME}",
            raw=committed_revocations,
        )
    else:
        registry_raw = absolute_plain_path(
            registry_path, must_exist=True
        ).read_bytes()
        registry = load_registry(registry_path, raw=registry_raw)
        matrix_raw = absolute_plain_path(matrix_path, must_exist=True).read_bytes()
        matrix = load_matrix(matrix_path, registry, raw=matrix_raw)
        revocation_raw = absolute_plain_path(
            revocation_path, must_exist=True
        ).read_bytes()
        revocations = load_revocations(revocation_path, raw=revocation_raw)
        committed_registry = registry_raw
        committed_matrix = matrix_raw
        revocation_checked_at = (
            revocations.updated_at if enforce_freshness else None
        )
    registry_sha256 = sha256_bytes(committed_registry)
    matrix_sha256 = sha256_bytes(committed_matrix)
    if matrix.capability_id != args.capability_id:
        raise CapabilityError("Capability matrix does not cover requested capability")
    matrix_row = matrix.targets.get(args.tool)
    if not matrix_row or (
        matrix_row["rollout_status"] not in {"eligible", "pilot-verified"}
        and args.action not in rollback_or_safety_actions
    ):
        raise CapabilityError("Capability matrix blocks this target")
    target = resolve_target(
        registry,
        args.tool,
        args.capability_id,
        fixture_root=args.fixture_root,
    )
    if args.fixture_root is None:
        matrix_scan_root = str(matrix_row["scan_root"])
        if not same_path(matrix_scan_root, target.scan_root):
            raise CapabilityError("Capability matrix scan root differs from registry")
    if (
        args.fixture_root is None
        and strict_current_view
        and args.action not in rollback_or_safety_actions
    ):
        now = dt.datetime.now(dt.timezone.utc)
        verified = parse_timestamp(matrix.verified_at, "matrix verified_at")
        observed = parse_timestamp(
            str(matrix_row["observed_at"]), "matrix observed_at"
        )
        for label, timestamp in (("matrix", verified), ("target row", observed)):
            if (
                timestamp > now + dt.timedelta(minutes=5)
                or now - timestamp > dt.timedelta(days=7)
            ):
                raise CapabilityError(f"{label} evidence is stale or in the future")
    return CapabilityRollout(
        repo=args.repo,
        commit=args.commit,
        capability_id=args.capability_id,
        target=target,
        approval_posture=str(matrix_row["approval_posture"]),
        registry_sha256=registry_sha256,
        matrix_sha256=matrix_sha256,
        matrix_status=str(matrix_row["rollout_status"]),
        scan_root_inventory_sha256=str(
            matrix_row["scan_root_inventory_sha256"]
        ),
        policy_commit=args.policy_commit,
        revocation_catalog=revocations,
        revocation_checked_at=revocation_checked_at,
        robert_same_user_risk_accepted=args.accept_robert_same_user_risk,
        enforce_freshness=enforce_freshness,
        allow_blocked_removal=args.action in rollback_or_safety_actions,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        rollout = rollout_from_args(args)
        if args.action == "plan":
            result = rollout.plan()
        elif args.action == "plan-rollback":
            result = rollout.plan_rollback()
        elif args.action == "plan-emergency-rollback":
            result = rollout.plan_rollback(emergency=True)
        elif args.action == "plan-discovery":
            result = rollout.plan_discovery(args.evidence_file)
        elif args.action == "verify":
            result = rollout.verify()
        elif args.action == "record-discovery":
            result = rollout.record_discovery(
                args.approval_file,
                args.evidence_file,
                owner_authorized=args.owner_authorized,
            )
        elif args.action == "deploy":
            result = rollout.deploy(
                args.approval_file,
                owner_authorized=args.owner_authorized,
                crash_after=args.crash_after,
            )
        elif args.action == "recover-deploy":
            result = rollout.recover_deploy(
                args.approval_file,
                owner_authorized=args.owner_authorized,
                crash_after=args.crash_after,
            )
        elif args.action == "rollback":
            result = rollout.rollback(
                args.approval_file,
                owner_authorized=args.owner_authorized,
                crash_after=args.crash_after,
            )
        elif args.action == "emergency-rollback":
            result = rollout.emergency_rollback(
                args.approval_file,
                owner_authorized=args.owner_authorized,
                crash_after=args.crash_after,
            )
        elif args.action == "recover-rollback":
            result = rollout.recover_rollback(
                args.approval_file,
                owner_authorized=args.owner_authorized,
                crash_after=args.crash_after,
            )
        else:
            raise CapabilityError(f"Unsupported action: {args.action}")
        emit(result)
        return 0
    except (CapabilityError, InjectedCrash, OSError) as exc:
        emit(
            {
                "action": args.action,
                "error": str(exc),
                "security_statement": SECURITY_STATEMENT,
                "status": "REFUSED",
                "writes_performed": (
                    0
                    if args.action
                    in {
                        "plan",
                        "plan-discovery",
                        "plan-emergency-rollback",
                        "plan-rollback",
                        "verify",
                    }
                    else "bounded-or-none"
                ),
            },
            stream=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
