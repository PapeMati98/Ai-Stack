#!/usr/bin/env python3
"""Transactional, source-locked installer for the local Agent Office.

The installer distributes only the explicit runtime payload and instruction
skill declared in ``package.json``. Durable Office data is deliberately not a
target. All configuration preimages and postimages stay in the private
transaction root outside the repository and outside the shared-skills backup.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from ctypes import wintypes

from coordfs.office_cli_remote import load_remote_config, parse_remote_config_bytes
from coordfs.atomic import CoordError


PACKAGE_ID = "agent-office"
PACKAGE_VERSION = "0.1.9"
MANAGED_BY = "agent-office-installer"
SOURCE_ROOT = Path(__file__).resolve().parent
PACKAGE_PATH = SOURCE_ROOT / "package.json"
SKILL_SOURCE_ROOT = SOURCE_ROOT / "skill" / PACKAGE_ID
DEFAULT_STATE_RELATIVE = Path(".agents") / "agent-office-deploy"
RUNTIME_RELATIVE = Path(".agents") / "runtime" / PACKAGE_ID
COORD_RELATIVE = Path(".agents") / "coord"
RUNTIME_MARKER = ".agent-office-runtime.json"
REMOTE_CONFIG_FILE = "office-remote.json"
SKILL_MARKER = ".agent-office-managed.json"
CLAUDE_TIMEOUT_DEFAULT_MS = 100_000_000
CLAUDE_TIMEOUT_MIN_MS = 300_000
CODEX_TOOL_TIMEOUT_S = 300
CODEX_STARTUP_TIMEOUT_S = 30
TRANSACTION_RE = re.compile(r"^[0-9a-f]{32}$")
SAFE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")
TOML_BEGIN = "# BEGIN AGENT-OFFICE MANAGED 0.1.0"
TOML_END = "# END AGENT-OFFICE MANAGED 0.1.0"
PRIVATE_FILE_SUFFIX = ".dpapi"
# Python writes an interpreter cache next to every imported module, so a deployed
# Office grows `coordfs/__pycache__/*.pyc` the first day it is used. Those files
# are not package payload, so deployment verification ignores them instead of
# deleting them: the Fleet package installer filters the same names in its own
# tree_manifest. Measured 2026-09-20, nine `.pyc` files made a live runtime tree
# hold 30 files against a 21-file receipt, and `upgrade` then refused with
# "Upgrade target drift blocks the transaction: runtime" - merely using Office
# blocked upgrading it until the owner moved the cache by hand.
BYTECODE_CACHE_DIR_NAMES = frozenset({"__pycache__"})
BYTECODE_FILE_SUFFIXES = (".pyc", ".pyo")
# The shared-skills ownership layer pins the derived skill fingerprint for this
# manager. It belongs to another layer: the installer reads it to report a stale
# pin and never writes it.
OWNERSHIP_RELATIVE = Path("global") / "codex" / "skills-ownership.json"
OWNERSHIP_MANAGER_KEY = MANAGED_BY
OWNERSHIP_PIN_FIELDS = ("marker_fingerprint", "receipt_fingerprint")
RECEIPT_STATUSES = frozenset(
    {
        "PREPARED",
        "APPLYING",
        "DEPLOYED",
        "AUTO_ROLLED_BACK",
        "ROLLING_BACK",
        "ROLLED_BACK",
        "RECOVERING",
        "FAILED",
    }
)

RUNTIME_SOURCE_MAP: tuple[tuple[str, str], ...] = (
    ("coord.cmd", "coord.cmd"),
    ("coord.py", "coord.py"),
    ("office.py", "office.py"),
    ("office_ssh_gate.py", "office_ssh_gate.py"),
    ("office_store_snapshot.py", "office_store_snapshot.py"),
    ("office_cli_host.py", "office_cli_host.py"),
    ("coordfs/cli_dispatch.py", "coordfs/cli_dispatch.py"),
    ("coordfs/cli_host.py", "coordfs/cli_host.py"),
    ("coordfs/worker_profile.py", "coordfs/worker_profile.py"),
    ("coordfs/native_model_evidence.py", "coordfs/native_model_evidence.py"),
    ("coordfs/native_process_snapshot.py", "coordfs/native_process_snapshot.py"),
    ("coordfs/__init__.py", "coordfs/__init__.py"),
    ("coordfs/__main__.py", "coordfs/__main__.py"),
    ("coordfs/atomic.py", "coordfs/atomic.py"),
    ("coordfs/cli.py", "coordfs/cli.py"),
    ("coordfs/conflicts.py", "coordfs/conflicts.py"),
    ("coordfs/constants.py", "coordfs/constants.py"),
    ("coordfs/doctor.py", "coordfs/doctor.py"),
    ("coordfs/errors.py", "coordfs/errors.py"),
    ("coordfs/events.py", "coordfs/events.py"),
    ("coordfs/hashing.py", "coordfs/hashing.py"),
    ("coordfs/identity.py", "coordfs/identity.py"),
    ("coordfs/checkout_claims.py", "coordfs/checkout_claims.py"),
    ("coordfs/journal_bridge.py", "coordfs/journal_bridge.py"),
    ("coordfs/knowledge_bridge.py", "coordfs/knowledge_bridge.py"),
    ("coordfs/antigravity_transport.py", "coordfs/antigravity_transport.py"),
    ("coordfs/antigravity_relay.py", "coordfs/antigravity_relay.py"),
    ("coordfs/wake.py", "coordfs/wake.py"),
    ("coordfs/wake_correlation.py", "coordfs/wake_correlation.py"),
    ("coordfs/codex_ack.py", "coordfs/codex_ack.py"),
    ("coordfs/codex_desktop_tool_host.py", "coordfs/codex_desktop_tool_host.py"),
    ("coordfs/codex_relay_cycle.py", "coordfs/codex_relay_cycle.py"),
    ("codex-relay-cycle.ps1", "codex-relay-cycle.ps1"),
    ("coordfs/desktop_adapters.py", "coordfs/desktop_adapters.py"),
    ("coordfs/zcode_desktop_tool_host.py", "coordfs/zcode_desktop_tool_host.py"),
    ("coordfs/leases.py", "coordfs/leases.py"),
    ("coordfs/office_cli.py", "coordfs/office_cli.py"),
    ("coordfs/office_mcp.py", "coordfs/office_mcp.py"),
    ("coordfs/native_mcp_identity.py", "coordfs/native_mcp_identity.py"),
    ("coordfs/office_pipe.py", "coordfs/office_pipe.py"),
    ("coordfs/office_remote.py", "coordfs/office_remote.py"),
    ("coordfs/office_cli_remote.py", "coordfs/office_cli_remote.py"),
    ("coordfs/office_ssh_gate.py", "coordfs/office_ssh_gate.py"),
    ("coordfs/office.py", "coordfs/office.py"),
    ("coordfs/thread_registry.py", "coordfs/thread_registry.py"),
    ("coordfs/project_registry_view.py", "coordfs/project_registry_view.py"),
    ("coordfs/project_goals.py", "coordfs/project_goals.py"),
    ("coordfs/projection.py", "coordfs/projection.py"),
    ("coordfs/projects.py", "coordfs/projects.py"),
    ("coordfs/relay_verify.py", "coordfs/relay_verify.py"),
    ("coordfs/store.py", "coordfs/store.py"),
    ("coordfs/work.py", "coordfs/work.py"),
    ("hooks/office_wake_stop_gate.py", "hooks/office_wake_stop_gate.py"),
    ("host/codex_app_transport.js", "host/codex_app_transport.js"),
)
SKILL_SOURCE_MAP: tuple[tuple[str, str], ...] = (
    ("skill/agent-office/SKILL.md", "SKILL.md"),
)

SKILL_TARGETS: tuple[tuple[str, str, Path, str], ...] = (
    ("skill-shared-agents", "shared-agents", Path(".agents/skills/agent-office"), "shared"),
    ("skill-claude-code", "claude-code", Path(".claude/skills/agent-office"), "claude"),
    ("skill-antigravity", "antigravity", Path(".gemini/config/skills/agent-office"), "antigravity"),
    ("skill-grok-cli", "grok-cli", Path(".grok/skills/agent-office"), "grok"),
    ("skill-qwen-code", "qwen-code", Path(".qwen/skills/agent-office"), "qwen"),
)

# Standalone-verify-only scope: these are exactly the three shared-config targets
# that already carry a dedicated Agent-Office semantic validator. A native tool
# appending its own unrelated key to one of these files (for example the Claude
# CLI writing a top-level "skillUsage" field into .claude.json) changes the
# whole-file hash while leaving the owned registration intact. ``verify_install``
# keeps such a target CURRENT only when the caller passes an explicit
# ``strict=False``, which only the read-only surfaces (standalone ``verify`` and
# ``dry-run``) do, and only while the owned-semantic validator still passes.
# Strict is the default, so install and recover never consult this table and
# their whole-file compare-and-set stays byte-exact, exactly like rollback and
# the runtime and skill targets.
OWNED_CONFIG_VERIFY: tuple[str, ...] = ("claude-settings", "claude-mcp", "codex-mcp")


class DeployError(RuntimeError):
    """A bounded installer failure that is safe to expose without payloads."""


class TransitionFailure(DeployError):
    def __init__(self, message: str, *, compensation_complete: bool, drift_ids: list[str]) -> None:
        super().__init__(message)
        self.compensation_complete = compensation_complete
        self.drift_ids = drift_ids


class DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


@dataclass(frozen=True)
class Package:
    manifest: dict[str, Any]
    fingerprint: str
    runtime_fingerprint: str
    skill_fingerprint: str
    runtime_files: Mapping[str, bytes]
    skill_files: Mapping[str, bytes]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"


def _data_blob(data: bytes) -> tuple[DataBlob, ctypes.Array[Any]]:
    buffer = ctypes.create_string_buffer(data or b"\0", max(1, len(data)))
    return DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def protect_private_bytes(data: bytes) -> bytes:
    """Encrypt a config image for the current Windows user without external dependencies."""
    if os.name != "nt":
        raise DeployError("Agent Office config images require Windows DPAPI")
    source, keepalive = _data_blob(data)
    destination = DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ctypes.set_last_error(0)
    if not crypt32.CryptProtectData(
        ctypes.byref(source),
        "agent-office transaction image",
        None,
        None,
        None,
        0x1,
        ctypes.byref(destination),
    ):
        error = ctypes.get_last_error()
        raise DeployError(f"DPAPI config-image encryption failed: winerror={error}")
    try:
        return ctypes.string_at(destination.pbData, destination.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(destination.pbData, ctypes.c_void_p))
        del keepalive


def unprotect_private_bytes(data: bytes) -> bytes:
    if os.name != "nt":
        raise DeployError("Agent Office config images require Windows DPAPI")
    source, keepalive = _data_blob(data)
    destination = DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ctypes.set_last_error(0)
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source),
        None,
        None,
        None,
        None,
        0x1,
        ctypes.byref(destination),
    ):
        error = ctypes.get_last_error()
        raise DeployError(f"DPAPI config-image decryption failed: winerror={error}")
    try:
        return ctypes.string_at(destination.pbData, destination.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(destination.pbData, ctypes.c_void_p))
        del keepalive


def path_exists(path: Path) -> bool:
    return os.path.lexists(os.fspath(path))


def is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise DeployError(f"Cannot inspect path metadata: {path}") from exc
    attributes = getattr(info, "st_file_attributes", 0)
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(attributes & flag)


def absolute_lexical(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def assert_plain_chain(path: Path, label: str) -> None:
    candidate = absolute_lexical(path)
    if not candidate.is_absolute():
        raise DeployError(f"{label} must be absolute: {candidate}")
    cursor = candidate
    while True:
        if path_exists(cursor) and is_reparse(cursor):
            raise DeployError(f"{label} contains a symlink/reparse point: {cursor}")
        if cursor.parent == cursor:
            break
        cursor = cursor.parent


def ensure_under(path: Path, root: Path, label: str, *, allow_root: bool = False) -> Path:
    candidate = absolute_lexical(path)
    boundary = absolute_lexical(root)
    try:
        relative = candidate.relative_to(boundary)
    except ValueError as exc:
        raise DeployError(f"{label} escapes its allowed root: {candidate}") from exc
    if not allow_root and not relative.parts:
        raise DeployError(f"{label} cannot be the allowed root itself: {candidate}")
    assert_plain_chain(candidate, label)
    return candidate


def safe_relative(value: str, label: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise DeployError(f"Unsafe {label} path in package manifest: {value!r}")
    if "\\" in value or ":" in value:
        raise DeployError(f"Non-portable {label} path in package manifest: {value!r}")
    return path


def safe_source_path(root: Path, value: str) -> Path:
    relative = safe_relative(value, "source")
    candidate = ensure_under(root.joinpath(*relative.parts), root, "package source")
    if not candidate.is_file():
        raise DeployError(f"Package source is missing or not a file: {value}")
    if is_reparse(candidate):
        raise DeployError(f"Package source is a symlink/reparse point: {value}")
    return candidate


def tree_fingerprint_rows(rows: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for row in sorted(rows, key=lambda item: str(item["path"])):
        digest.update(str(row["path"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(row["bytes"]).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(row["sha256"]).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def virtual_tree_snapshot(files: Mapping[str, bytes]) -> dict[str, Any]:
    rows = [
        {"path": path, "bytes": len(data), "sha256": sha256_bytes(data)}
        for path, data in sorted(files.items())
    ]
    return {"state": "tree", "files": len(rows), "fingerprint": tree_fingerprint_rows(rows)}


def is_bytecode_artifact(name: str) -> bool:
    """True for the interpreter cache names that are not package payload."""
    return name in BYTECODE_CACHE_DIR_NAMES or name.lower().endswith(BYTECODE_FILE_SUFFIXES)


def iter_plain_files(root: Path) -> Iterator[tuple[Path, str]]:
    """Walk a target tree, skipping interpreter bytecode caches.

    Bytecode is ignored, never removed: this walk only decides what counts as
    deployed payload. Every file the package owns is still counted, so a change,
    removal or replacement of any managed file still blocks the transaction; a
    foreign non-cache file inside a managed tree is still drift.
    """
    assert_plain_chain(root, "tree")
    if not root.is_dir():
        raise DeployError(f"Expected a directory tree: {root}")
    for current, dir_names, file_names in os.walk(root):
        current_path = Path(current)
        kept: list[str] = []
        for name in sorted(dir_names):
            if is_bytecode_artifact(name):
                continue
            child = current_path / name
            if is_reparse(child):
                raise DeployError(f"Tree contains a symlink/reparse point: {child}")
            kept.append(name)
        dir_names[:] = kept
        for name in sorted(file_names):
            if is_bytecode_artifact(name):
                continue
            child = current_path / name
            if is_reparse(child) or not child.is_file():
                raise DeployError(f"Tree contains a non-plain file: {child}")
            yield child, child.relative_to(root).as_posix()


def snapshot(path: Path) -> dict[str, Any]:
    assert_plain_chain(path, "target")
    if not path_exists(path):
        return {"state": "absent"}
    if path.is_file():
        data = path.read_bytes()
        return {"state": "file", "bytes": len(data), "sha256": sha256_bytes(data)}
    if path.is_dir():
        rows: list[dict[str, Any]] = []
        for child, relative in iter_plain_files(path):
            data = child.read_bytes()
            rows.append({"path": relative, "bytes": len(data), "sha256": sha256_bytes(data)})
        return {"state": "tree", "files": len(rows), "fingerprint": tree_fingerprint_rows(rows)}
    raise DeployError(f"Unsupported target type: {path}")


def load_json_file(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise DeployError(f"Missing {label}: {path}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DeployError(f"Invalid {label}: {path}") from exc
    if not isinstance(value, dict):
        raise DeployError(f"{label} must be a JSON object: {path}")
    return value


def _manifest_payload(rows: Any, expected: tuple[tuple[str, str], ...], kind: str) -> tuple[dict[str, bytes], list[dict[str, Any]]]:
    if not isinstance(rows, list):
        raise DeployError(f"Package {kind} manifest must be a list")
    expected_pairs = list(expected)
    actual_pairs: list[tuple[str, str]] = []
    payload: dict[str, bytes] = {}
    normalized_rows: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise DeployError(f"Package {kind} row {index} must be an object")
        source = str(row.get("source", ""))
        target = str(row.get("target", ""))
        source_path = safe_source_path(SOURCE_ROOT, source)
        safe_relative(target, "target")
        data = source_path.read_bytes()
        expected_hash = row.get("sha256")
        expected_bytes = row.get("bytes")
        if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            raise DeployError(f"Package {kind} row has an invalid SHA-256: {source}")
        if isinstance(expected_bytes, bool) or not isinstance(expected_bytes, int) or expected_bytes < 0:
            raise DeployError(f"Package {kind} row has an invalid byte count: {source}")
        if sha256_bytes(data) != expected_hash or len(data) != expected_bytes:
            raise DeployError(f"Package source hash/size mismatch: {source}")
        if target in payload:
            raise DeployError(f"Duplicate package target: {target}")
        payload[target] = data
        actual_pairs.append((source, target))
        normalized_rows.append({"source": source, "target": target, "bytes": expected_bytes, "sha256": expected_hash})
    if actual_pairs != expected_pairs:
        raise DeployError(f"Package {kind} source allowlist does not match the installer contract")
    return payload, normalized_rows


def seal_package() -> dict[str, Any]:
    """Refresh the package manifest digests from the current sources.

    This is a build step, not a trust anchor. The packaged file list stays pinned to
    the installer's own RUNTIME_SOURCE_MAP/SKILL_SOURCE_MAP, so sealing can never add,
    drop, or retarget a file; it only records what those declared sources currently
    contain. Nothing deployed is read or written and no path is ever removed - the
    single output is package.json.

    Without it, every edit under coordfs/ left the manifest stale and the installer
    tests failed with ~40 `Package source hash/size mismatch` errors that looked like
    the change had broken the office. On 2026-09-20 that misread a working change as
    a failure four times before the missing command was recognised as the cause.
    """
    assert_plain_chain(SOURCE_ROOT, "package root")
    manifest = load_json_file(PACKAGE_PATH, "Agent Office package manifest")
    if (
        manifest.get("schema_version") != 1
        or manifest.get("package_id") != PACKAGE_ID
        or manifest.get("package_version") != PACKAGE_VERSION
    ):
        raise DeployError("Unsupported Agent Office package identity")
    previous = {
        kind: {
            str(row.get("source", "")): row
            for row in manifest.get(kind, [])
            if isinstance(row, dict)
        }
        for kind in ("runtime", "skill")
    }
    resealed: list[str] = []
    for kind, expected in (("runtime", RUNTIME_SOURCE_MAP), ("skill", SKILL_SOURCE_MAP)):
        rows: list[dict[str, Any]] = []
        files: dict[str, bytes] = {}
        for source, target in expected:
            data = safe_source_path(SOURCE_ROOT, source).read_bytes()
            safe_relative(target, "target")
            if target in files:
                raise DeployError(f"Duplicate package target: {target}")
            files[target] = data
            digest = sha256_bytes(data)
            rows.append({"bytes": len(data), "sha256": digest, "source": source, "target": target})
            before = previous[kind].get(source)
            if not isinstance(before, dict) or before.get("sha256") != digest:
                resealed.append(source)
        manifest[kind] = rows
        manifest[f"{kind}_fingerprint"] = virtual_tree_snapshot(files)["fingerprint"]
    text = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    PACKAGE_PATH.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
    # Prove the freshly written manifest is one the deploy path accepts, so a bad
    # seal fails here instead of surfacing as a confusing install-time mismatch.
    package = verify_package()
    return {
        "action": "seal",
        "status": "READY" if resealed else "CURRENT",
        "passed": True,
        "manifest": str(PACKAGE_PATH),
        "resealed_sources": sorted(resealed),
        "package_fingerprint": package.fingerprint,
        "runtime_fingerprint": package.runtime_fingerprint,
        "skill_fingerprint": package.skill_fingerprint,
        # Any SKILL.md edit also invalidates the pin the ownership layer holds,
        # which is outside this installer's write scope. Report it here so the
        # stale pin is visible instead of surfacing as an unexplained test failure.
        "skill_pin": skill_pin_report(),
    }


def verify_package() -> Package:
    assert_plain_chain(SOURCE_ROOT, "package root")
    manifest = load_json_file(PACKAGE_PATH, "Agent Office package manifest")
    if (
        manifest.get("schema_version") != 1
        or manifest.get("package_id") != PACKAGE_ID
        or manifest.get("package_version") != PACKAGE_VERSION
    ):
        raise DeployError("Unsupported Agent Office package identity")
    runtime_files, runtime_rows = _manifest_payload(manifest.get("runtime"), RUNTIME_SOURCE_MAP, "runtime")
    skill_files, skill_rows = _manifest_payload(manifest.get("skill"), SKILL_SOURCE_MAP, "skill")
    runtime_fingerprint = virtual_tree_snapshot(runtime_files)["fingerprint"]
    skill_fingerprint = virtual_tree_snapshot(skill_files)["fingerprint"]
    declared_runtime = manifest.get("runtime_fingerprint")
    declared_skill = manifest.get("skill_fingerprint")
    if declared_runtime != runtime_fingerprint or declared_skill != skill_fingerprint:
        raise DeployError("Package source-tree fingerprint mismatch")
    fingerprint_input = {
        "schema_version": 1,
        "package_id": PACKAGE_ID,
        "package_version": PACKAGE_VERSION,
        "runtime_version": manifest.get("runtime_version"),
        "runtime": runtime_rows,
        "skill": skill_rows,
        "runtime_fingerprint": runtime_fingerprint,
        "skill_fingerprint": skill_fingerprint,
    }
    return Package(
        manifest=manifest,
        fingerprint=sha256_bytes(canonical_bytes(fingerprint_input)),
        runtime_fingerprint=runtime_fingerprint,
        skill_fingerprint=skill_fingerprint,
        runtime_files=runtime_files,
        skill_files=skill_files,
    )


def ownership_records_path() -> Path:
    """Where the ownership layer pins the derived skill fingerprint.

    The Agent Office installer only ever reads this file: repinning belongs to a
    different layer, so a stale pin is reported here, never repaired.
    """
    return SOURCE_ROOT.parents[1] / OWNERSHIP_RELATIVE


def skill_pin_report(records: Path | None = None) -> dict[str, Any]:
    """Report whether the ownership-layer skill pin still matches the skill source.

    Strictly read-only toward ``skills-ownership.json``. Any edit of
    ``skill/agent-office/SKILL.md`` changes the derived fingerprint and therefore
    the pin, which on 2026-09-20 made
    ``test_dedicated_manager_contract_matches_derived_skill_source`` fail three
    times in a row with no visible cause: the pin lives outside the installer's
    write scope, so the executor could neither fix nor even recognise it. This
    names the stale fields and the exact value to write; it never writes.
    """
    package = verify_package()
    derived = package.skill_fingerprint
    path = absolute_lexical(records if records is not None else ownership_records_path())
    report: dict[str, Any] = {
        "action": "skill-pin",
        "skill_source": str(SKILL_SOURCE_ROOT / "SKILL.md"),
        "derived_skill_fingerprint": derived,
        "ownership_records": str(path),
        "ownership_written": False,
    }
    try:
        payload = load_json_file(path, "shared-skills ownership records")
    except (DeployError, OSError, UnicodeError, ValueError) as exc:
        report.update(
            {
                "status": "UNKNOWN",
                "passed": False,
                "ownership_state": "unreadable",
                "message": f"Ownership records are unavailable; nothing was written: {exc}",
            }
        )
        return report
    contracts = payload.get("manager_contracts")
    contract = contracts.get(OWNERSHIP_MANAGER_KEY) if isinstance(contracts, dict) else None
    if not isinstance(contract, dict):
        report.update(
            {
                "status": "UNKNOWN",
                "passed": False,
                "ownership_state": "missing-manager-contract",
                "message": (
                    f"Ownership records have no {OWNERSHIP_MANAGER_KEY} manager contract; "
                    "nothing was written."
                ),
            }
        )
        return report
    pinned = {field: contract.get(field) for field in OWNERSHIP_PIN_FIELDS}
    stale = sorted(field for field, value in pinned.items() if value != derived)
    report["pinned"] = pinned
    report["stale_pin_fields"] = stale
    if not stale:
        report.update({"status": "CURRENT", "passed": True, "ownership_state": "pinned"})
        return report
    report.update(
        {
            "status": "STALE_PIN",
            "passed": False,
            "ownership_state": "stale-pin",
            "value_to_pin": derived,
            "message": (
                f"The pinned skill fingerprint in {path} is stale for {', '.join(stale)}. "
                "This installer does not write that file; repin it in the ownership layer."
            ),
            "remediation": {
                "file": str(path),
                "path": f"manager_contracts.{OWNERSHIP_MANAGER_KEY}",
                "fields": {field: derived for field in stale},
                "note": "Ownership layer: repin deliberately, the installer stays read-only here.",
            },
        }
    )
    return report


def pin_python(candidate: Path | None = None) -> Path:
    raw = candidate
    if raw is None:
        base = getattr(sys, "_base_executable", None)
        raw = Path(base) if base else Path(sys.executable)
    path = absolute_lexical(Path(raw))
    assert_plain_chain(path, "Python executable")
    if not path.is_file():
        raise DeployError(f"Python executable is unavailable: {path}")
    probe = (
        "import json,sys;"
        "print(json.dumps({'version':[sys.version_info.major,sys.version_info.minor],"
        "'executable':sys.executable}))"
    )
    try:
        result = subprocess.run(
            [str(path), "-I", "-c", probe],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=15,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        payload = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, UnicodeError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        raise DeployError(f"Python executable verification failed: {path}") from exc
    if not isinstance(payload, dict) or payload.get("version") not in ([3, 11], [3, 12], [3, 13], [3, 14]):
        version = payload.get("version") if isinstance(payload, dict) else None
        if not (
            isinstance(version, list)
            and len(version) == 2
            and all(isinstance(item, int) and not isinstance(item, bool) for item in version)
            and tuple(version) >= (3, 11)
        ):
            raise DeployError("Agent Office requires a verified Python 3.11+ executable")
    reported = payload.get("executable")
    if not isinstance(reported, str) or not Path(reported).is_absolute():
        raise DeployError("Python verification did not return an absolute executable")
    try:
        same = os.path.samefile(path, Path(reported))
    except OSError:
        same = os.path.normcase(str(path)) == os.path.normcase(str(absolute_lexical(Path(reported))))
    if not same:
        raise DeployError("Python verification resolved to a different executable")
    return path


JSON_DECODER = json.JSONDecoder()


def decode_json_document(data: bytes, label: str) -> tuple[str, bytes]:
    bom = b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b""
    try:
        text = data[len(bom) :].decode("utf-8")
        value = json.loads(text)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DeployError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise DeployError(f"{label} root must be a JSON object")
    return text, bom


def skip_ws(text: str, index: int) -> int:
    while index < len(text) and text[index] in " \t\r\n":
        index += 1
    return index


def root_object_span(text: str) -> tuple[int, int]:
    start = skip_ws(text, 0)
    if start >= len(text) or text[start] != "{":
        raise DeployError("JSON root is not an object")
    _, end = JSON_DECODER.raw_decode(text, start)
    if skip_ws(text, end) != len(text):
        raise DeployError("JSON has trailing content")
    return start, end


def object_members(text: str, span: tuple[int, int]) -> list[tuple[str, int, int]]:
    start, end = span
    if text[start] != "{" or text[end - 1] != "}":
        raise DeployError("Invalid JSON object span")
    index = skip_ws(text, start + 1)
    rows: list[tuple[str, int, int]] = []
    while index < end - 1:
        key, key_end = JSON_DECODER.raw_decode(text, index)
        if not isinstance(key, str):
            raise DeployError("JSON object key is not a string")
        colon = skip_ws(text, key_end)
        if colon >= end or text[colon] != ":":
            raise DeployError("JSON object member has no colon")
        value_start = skip_ws(text, colon + 1)
        _, value_end = JSON_DECODER.raw_decode(text, value_start)
        rows.append((key, value_start, value_end))
        index = skip_ws(text, value_end)
        if index < end - 1:
            if text[index] != ",":
                raise DeployError("JSON object member has no separator")
            index = skip_ws(text, index + 1)
    return rows


def member_span(text: str, object_span: tuple[int, int], key: str) -> tuple[int, int] | None:
    matches = [(start, end) for name, start, end in object_members(text, object_span) if name == key]
    if len(matches) > 1:
        raise DeployError(f"Duplicate JSON key: {key}")
    return matches[0] if matches else None


def value_at(text: str, span: tuple[int, int]) -> Any:
    value, end = JSON_DECODER.raw_decode(text, span[0])
    if end != span[1]:
        raise DeployError("Invalid JSON value span")
    return value


def object_span_at(text: str, span: tuple[int, int], label: str) -> tuple[int, int]:
    if text[span[0]] != "{" or not isinstance(value_at(text, span), dict):
        raise DeployError(f"{label} must be a JSON object")
    return span


def append_object_member(text: str, object_span: tuple[int, int], key: str, value: Any) -> str:
    if member_span(text, object_span, key) is not None:
        raise DeployError(f"JSON member already exists: {key}")
    start, end = object_span
    members = object_members(text, object_span)
    insert_at = end - 1
    while insert_at > start + 1 and text[insert_at - 1] in " \t\r\n":
        insert_at -= 1
    prefix = "," if members else ""
    encoded = json.dumps(key, ensure_ascii=True) + ":" + json.dumps(
        value, ensure_ascii=False, separators=(",", ":")
    )
    return text[:insert_at] + prefix + encoded + text[insert_at:]


def _new_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"


def normalize_remote_config(value: Mapping[str, Any]) -> dict[str, Any]:
    """Closed connection data; credential paths remain opaque and are never opened."""
    if not isinstance(value, Mapping):
        raise DeployError("Invalid remote Office connection configuration")
    try:
        raw = json.dumps(dict(value), allow_nan=False).encode("utf-8")
        return asdict(parse_remote_config_bytes(raw))
    except (CoordError, TypeError, ValueError):
        raise DeployError("Invalid remote Office connection configuration") from None


def receipt_remote_config(receipt: Mapping[str, Any]) -> dict[str, Any] | None:
    if "remote_config" not in receipt:
        return None
    return normalize_remote_config(receipt["remote_config"])


def require_requested_connection(current: Mapping[str, Any], home: Path, state_root: Path,
                                 requested: Mapping[str, Any] | None) -> None:
    if requested is None or current.get("status") != "CURRENT":
        return
    _, receipt = load_receipt(home, state_root, str(current["transaction_id"]))
    if receipt_remote_config(receipt) != normalize_remote_config(requested):
        raise DeployError("Changing the Office connection requires an explicit upgrade transaction")


def _connection_args(runtime_root: Path, coord_home: Path, remote_config: Mapping[str, Any] | None) -> list[str]:
    if remote_config is not None:
        normalize_remote_config(remote_config)
        return ["--remote-config", str(runtime_root / REMOTE_CONFIG_FILE)]
    return ["--home", str(coord_home)]


def claude_server(
    python_executable: Path, runtime_root: Path, coord_home: Path,
    *, remote_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "type": "stdio",
        "command": str(python_executable),
        "args": [
            "-I",
            str(runtime_root / "office.py"),
            *_connection_args(runtime_root, coord_home, remote_config),
            "--harness",
            "claude",
            "mcp",
        ],
    }


def codex_server(
    python_executable: Path, runtime_root: Path, coord_home: Path,
    *, remote_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "command": str(python_executable),
        "args": [
            "-I",
            str(runtime_root / "office.py"),
            *_connection_args(runtime_root, coord_home, remote_config),
            "--harness",
            "codex",
            "mcp",
        ],
        "startup_timeout_sec": CODEX_STARTUP_TIMEOUT_S,
        "tool_timeout_sec": CODEX_TOOL_TIMEOUT_S,
        "required": False,
    }


def patch_claude_mcp(data: bytes, desired: dict[str, Any],
                     *, owned_previous: dict[str, Any] | None = None) -> bytes:
    if not data:
        return _new_json({"mcpServers": {PACKAGE_ID: desired}})
    text, bom = decode_json_document(data, "Claude user MCP config")
    root = root_object_span(text)
    servers_span = member_span(text, root, "mcpServers")
    if servers_span is None:
        patched = append_object_member(text, root, "mcpServers", {PACKAGE_ID: desired})
    else:
        servers = object_span_at(text, servers_span, "Claude mcpServers")
        office_span = member_span(text, servers, PACKAGE_ID)
        if office_span is None:
            patched = append_object_member(text, servers, PACKAGE_ID, desired)
        elif value_at(text, office_span) == desired:
            return data
        elif owned_previous is not None and value_at(text, office_span) == owned_previous:
            replacement = json.dumps(desired, ensure_ascii=False, indent=2)
            if "\r\n" in text:
                replacement = replacement.replace("\n", "\r\n")
            patched = text[:office_span[0]] + replacement + text[office_span[1]:]
        else:
            raise DeployError("Existing Claude agent-office MCP entry differs; ownership is unknown")
    verified = json.loads(patched)
    if verified.get("mcpServers", {}).get(PACKAGE_ID) != desired:
        raise DeployError("Claude MCP patch verification failed")
    return bom + patched.encode("utf-8")


def _timeout_value(value: Any) -> int | None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9]+", value) is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def patch_claude_settings(data: bytes) -> bytes:
    if not data:
        return _new_json({"env": {"MCP_TOOL_TIMEOUT": str(CLAUDE_TIMEOUT_DEFAULT_MS)}})
    text, bom = decode_json_document(data, "Claude settings")
    root = root_object_span(text)
    env_span = member_span(text, root, "env")
    if env_span is None:
        patched = append_object_member(
            text, root, "env", {"MCP_TOOL_TIMEOUT": str(CLAUDE_TIMEOUT_DEFAULT_MS)}
        )
    else:
        env_object = object_span_at(text, env_span, "Claude settings env")
        aliases = [name for name, _, _ in object_members(text, env_object) if name.upper() == "MCP_TOOL_TIMEOUT"]
        if len(aliases) > 1 or (aliases and aliases[0] != "MCP_TOOL_TIMEOUT"):
            raise DeployError("Claude settings contain a conflicting MCP_TOOL_TIMEOUT override")
        timeout_span = member_span(text, env_object, "MCP_TOOL_TIMEOUT")
        if timeout_span is None:
            patched = append_object_member(
                text, env_object, "MCP_TOOL_TIMEOUT", str(CLAUDE_TIMEOUT_DEFAULT_MS)
            )
        else:
            value = value_at(text, timeout_span)
            numeric = _timeout_value(value)
            if numeric is None:
                raise DeployError("Claude MCP_TOOL_TIMEOUT must be a numeric string")
            if numeric < CLAUDE_TIMEOUT_MIN_MS:
                raise DeployError("Claude MCP_TOOL_TIMEOUT is below the required 300000 ms")
            return data
    parsed = json.loads(patched)
    timeout = parsed.get("env", {}).get("MCP_TOOL_TIMEOUT")
    if (_timeout_value(timeout) or -1) < CLAUDE_TIMEOUT_MIN_MS:
        raise DeployError("Claude timeout patch verification failed")
    return bom + patched.encode("utf-8")


def wake_stop_hook(python_executable: Path, runtime_root: Path) -> dict[str, Any]:
    """Pinned exec-form hook; no shell expansion of paths or prompt content."""
    return {
        "type": "command", "command": str(python_executable),
        "args": ["-I", str(runtime_root / "hooks" / "office_wake_stop_gate.py"),
                 "--office", str(runtime_root / "office.cmd")],
        "timeout": 20,
    }


def receipt_wake_stop_gate(receipt: Mapping[str, Any]) -> bool:
    value = receipt.get("wake_stop_gate_enabled", False)
    if not isinstance(value, bool):
        raise DeployError("Invalid Wake STOP gate receipt binding")
    return value


def _wake_stop_hooks(text: str) -> list[dict[str, Any]]:
    root = root_object_span(text)
    span = member_span(text, root, "hooks")
    if span is None:
        return []
    hooks_object = object_span_at(text, span, "Claude hooks")
    submit = member_span(text, hooks_object, "UserPromptSubmit")
    if submit is None:
        return []
    groups = value_at(text, submit)
    if not isinstance(groups, list):
        raise DeployError("Claude UserPromptSubmit hooks must be an array")
    matches = []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            raise DeployError("Invalid Claude UserPromptSubmit hook group")
        for hook in group["hooks"]:
            if not isinstance(hook, dict):
                raise DeployError("Invalid Claude UserPromptSubmit hook")
            # An entry referencing our script with a different command, matcher,
            # timeout or arguments is drift, not an unrelated hook to duplicate.
            if "office_wake_stop_gate.py" in json.dumps(hook, ensure_ascii=False).lower():
                matches.append({"group": group, "hook": hook})
    return matches


def validate_wake_stop_gate_bytes(data: bytes, desired: Mapping[str, Any]) -> None:
    text, _ = decode_json_document(data, "Claude settings")
    matches = _wake_stop_hooks(text)
    if len(matches) != 1 or matches[0]["group"] != {"hooks": [dict(desired)]}:
        raise DeployError("Owned Wake STOP hook is missing, duplicated or changed")


def patch_wake_stop_gate(
    data: bytes, desired: dict[str, Any], *, owned_previous: bool = False,
) -> bytes:
    """Append one exact hook without reserializing foreign settings or hooks."""
    text, bom = decode_json_document(data, "Claude settings")
    matches = _wake_stop_hooks(text)
    if matches:
        if not owned_previous:
            raise DeployError("Existing Wake STOP hook has no installer ownership")
        validate_wake_stop_gate_bytes(data, desired)
        return data
    if owned_previous:
        raise DeployError("Owned Wake STOP hook was removed")
    root = root_object_span(text)
    hooks_span = member_span(text, root, "hooks")
    group = {"hooks": [desired]}
    if hooks_span is None:
        patched = append_object_member(text, root, "hooks", {"UserPromptSubmit": [group]})
    else:
        hooks_object = object_span_at(text, hooks_span, "Claude hooks")
        submit = member_span(text, hooks_object, "UserPromptSubmit")
        if submit is None:
            patched = append_object_member(text, hooks_object, "UserPromptSubmit", [group])
        else:
            groups = value_at(text, submit)
            if not isinstance(groups, list):
                raise DeployError("Claude UserPromptSubmit hooks must be an array")
            # Keep every existing element's original bytes. Only insert before ].
            end = submit[1] - 1
            if text[end] != "]":
                raise DeployError("Invalid Claude hook array span")
            added = (", " if groups else "") + json.dumps(group, ensure_ascii=False)
            patched = text[:end] + added + text[end:]
    result = bom + patched.encode("utf-8")
    validate_wake_stop_gate_bytes(result, desired)
    return result


def decode_toml_document(data: bytes) -> tuple[str, bytes]:
    bom = b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b""
    try:
        text = data[len(bom) :].decode("utf-8")
        value = tomllib.loads(text)
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise DeployError("Codex config is not valid UTF-8 TOML") from exc
    if not isinstance(value, dict):
        raise DeployError("Codex config root must be a TOML table")
    return text, bom


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def patch_codex_mcp(data: bytes, desired: dict[str, Any],
                    *, owned_previous: dict[str, Any] | None = None) -> bytes:
    if data:
        text, bom = decode_toml_document(data)
        parsed = tomllib.loads(text)
    else:
        text, bom, parsed = "", b"", {}
    begin_count = text.count(TOML_BEGIN)
    end_count = text.count(TOML_END)
    if begin_count != end_count or begin_count > 1:
        raise DeployError("Codex config has malformed Agent Office managed markers")
    servers = parsed.get("mcp_servers", {})
    if servers is not None and not isinstance(servers, dict):
        raise DeployError("Codex mcp_servers must be a TOML table")
    existing = servers.get(PACKAGE_ID) if isinstance(servers, dict) else None
    owned_suffix = ""
    if existing is not None:
        if existing == desired:
            return data
        if owned_previous is None or existing != owned_previous or begin_count != 1:
            raise DeployError("Existing Codex agent-office MCP entry differs; ownership is unknown")
        start = text.index(TOML_BEGIN)
        end = text.index(TOML_END) + len(TOML_END)
        if end <= start or (start and text[start - 1] != "\n"):
            raise DeployError("Codex managed block is not a separate ordered block")
        previous_block = text[start:end]
        if tomllib.loads(previous_block) != {"mcp_servers": {PACKAGE_ID: owned_previous}}:
            raise DeployError("Codex managed block contains unowned configuration")
        owned_suffix = text[end:]
        text = text[:start]
    elif begin_count:
        raise DeployError("Codex config has Agent Office markers without the owned server entry")
    newline = "\r\n" if "\r\n" in text else "\n"
    prefix = text
    if prefix and not prefix.endswith(("\n", "\r")):
        prefix += newline
    if prefix and not prefix.endswith(newline * 2):
        prefix += newline
    args = ", ".join(toml_string(str(item)) for item in desired["args"])
    block = newline.join(
        (
            TOML_BEGIN,
            "[mcp_servers.agent-office]",
            f"command = {toml_string(str(desired['command']))}",
            f"args = [{args}]",
            f"startup_timeout_sec = {desired['startup_timeout_sec']}",
            f"tool_timeout_sec = {desired['tool_timeout_sec']}",
            "required = false",
            TOML_END,
            "",
        )
    )
    patched = prefix + block + owned_suffix
    try:
        verified = tomllib.loads(patched)
    except tomllib.TOMLDecodeError as exc:
        raise DeployError("Codex MCP patch would make the TOML invalid") from exc
    if verified.get("mcp_servers", {}).get(PACKAGE_ID) != desired:
        raise DeployError("Codex MCP patch verification failed")
    return bom + patched.encode("utf-8")


def runtime_marker(package: Package, transaction_id: str, python_executable: Path,
                   *, remote_config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if TRANSACTION_RE.fullmatch(transaction_id) is None:
        raise DeployError("Invalid transaction id for runtime marker")
    marker = {
        "schema_version": 1,
        "managed_by": MANAGED_BY,
        "package_id": PACKAGE_ID,
        "package_version": PACKAGE_VERSION,
        "package_fingerprint": package.fingerprint,
        "source_fingerprint": package.runtime_fingerprint,
        "tool": "runtime",
        "python_executable": str(python_executable),
        "transaction_id": transaction_id,
    }
    if remote_config is not None:
        marker["remote_config_sha256"] = sha256_bytes(json_bytes(normalize_remote_config(remote_config)))
    return marker


def skill_marker(package: Package, transaction_id: str, tool: str) -> dict[str, Any]:
    if TRANSACTION_RE.fullmatch(transaction_id) is None:
        raise DeployError("Invalid transaction id for skill marker")
    return {
        "schema_version": 1,
        "managed_by": MANAGED_BY,
        "capability_id": PACKAGE_ID,
        "capability_version": PACKAGE_VERSION,
        "package_id": PACKAGE_ID,
        "package_fingerprint": package.fingerprint,
        "source_fingerprint": package.skill_fingerprint,
        "tool": tool,
        "transaction_id": transaction_id,
    }


def cmd_quote_path(path: Path) -> str:
    value = str(path)
    if any(character in value for character in ('"', "\r", "\n")):
        raise DeployError("A wrapper path contains an unsafe command character")
    return '"' + value.replace("%", "%%") + '"'


def wrapper_bytes(python_executable: Path, runtime_root: Path, coord_home: Path,
                  *, remote_config: Mapping[str, Any] | None = None) -> bytes:
    if remote_config is not None:
        normalize_remote_config(remote_config)
        route = f"--remote-config {cmd_quote_path(runtime_root / REMOTE_CONFIG_FILE)}"
    else:
        route = f"--runtime-home {cmd_quote_path(coord_home)}"
    lines = (
        "@echo off",
        "setlocal",
        f"{cmd_quote_path(python_executable)} -I {cmd_quote_path(runtime_root / 'office.py')} "
        f"{route} %*",
        "exit /b %ERRORLEVEL%",
        "",
    )
    return "\r\n".join(lines).encode("utf-8")


def runtime_payload(
    package: Package,
    transaction_id: str,
    python_executable: Path,
    runtime_root: Path,
    coord_home: Path,
    *, remote_config: Mapping[str, Any] | None = None,
) -> dict[str, bytes]:
    files = dict(package.runtime_files)
    files["office.cmd"] = wrapper_bytes(python_executable, runtime_root, coord_home, remote_config=remote_config)
    files[RUNTIME_MARKER] = json_bytes(runtime_marker(package, transaction_id, python_executable, remote_config=remote_config))
    if remote_config is not None:
        files[REMOTE_CONFIG_FILE] = json_bytes(normalize_remote_config(remote_config))
    return files


def skill_payload(package: Package, transaction_id: str, tool: str) -> dict[str, bytes]:
    files = dict(package.skill_files)
    files[SKILL_MARKER] = json_bytes(skill_marker(package, transaction_id, tool))
    return files


def normalize_layout(
    home: Path, appdata: Path | None = None, state_root: Path | None = None
) -> tuple[Path, Path, Path]:
    normalized_home = absolute_lexical(home)
    assert_plain_chain(normalized_home, "home")
    if not normalized_home.is_dir():
        raise DeployError(f"Home directory is unavailable: {normalized_home}")
    normalized_appdata = absolute_lexical(appdata or (normalized_home / "AppData" / "Roaming"))
    ensure_under(normalized_appdata, normalized_home, "APPDATA")
    normalized_state = absolute_lexical(state_root or (normalized_home / DEFAULT_STATE_RELATIVE))
    ensure_under(normalized_state, normalized_home, "installer state root")
    coord_home = absolute_lexical(normalized_home / COORD_RELATIVE)
    runtime_root = absolute_lexical(normalized_home / RUNTIME_RELATIVE)
    protected = [
        (coord_home, "durable coord/Office data"),
        (runtime_root, "runtime code"),
    ]
    protected.extend(
        (absolute_lexical(normalized_home / relative), f"skill target {identifier}")
        for identifier, _, relative, _ in SKILL_TARGETS
    )
    protected.extend([
        (absolute_lexical(normalized_home / ".claude" / "settings.json"), "Claude settings"),
        (absolute_lexical(normalized_home / ".claude.json"), "Claude MCP config"),
        (absolute_lexical(normalized_home / ".codex" / "config.toml"), "Codex MCP config"),
        (absolute_lexical(SOURCE_ROOT), "source package"),
    ])
    for protected_path, label in protected:
        if (
            normalized_state == protected_path
            or normalized_state in protected_path.parents
            or protected_path in normalized_state.parents
        ):
            raise DeployError(f"Installer state must be disjoint from {label}")
    return normalized_home, normalized_appdata, normalized_state


def expected_target_defs(home: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [
        {
            "id": "runtime",
            "kind": "tree",
            "path": home / RUNTIME_RELATIVE,
            "target": "runtime",
            "tool": "runtime",
        }
    ]
    for identifier, target, relative, tool in SKILL_TARGETS:
        rows.append(
            {"id": identifier, "kind": "tree", "path": home / relative, "target": target, "tool": tool}
        )
    rows.extend(
        (
            {
                "id": "claude-settings",
                "kind": "file",
                "path": home / ".claude" / "settings.json",
                "target": "claude-settings",
                "tool": "claude",
            },
            {
                "id": "claude-mcp",
                "kind": "file",
                "path": home / ".claude.json",
                "target": "claude-mcp",
                "tool": "claude",
            },
            {
                "id": "codex-mcp",
                "kind": "file",
                "path": home / ".codex" / "config.toml",
                "target": "codex-mcp",
                "tool": "codex",
            },
        )
    )
    identifiers = [str(row["id"]) for row in rows]
    paths = [os.path.normcase(str(absolute_lexical(Path(row["path"])))) for row in rows]
    if len(identifiers) != len(set(identifiers)) or len(paths) != len(set(paths)):
        raise DeployError("Duplicate installer target id or path")
    for row in rows:
        row["path"] = ensure_under(Path(row["path"]), home, f"target {row['id']}")
    for left_index, left in enumerate(rows):
        left_path = Path(left["path"])
        for right in rows[left_index + 1 :]:
            right_path = Path(right["path"])
            if left_path in right_path.parents or right_path in left_path.parents:
                raise DeployError(f"Overlapping installer targets: {left['id']} and {right['id']}")
    return rows


def read_file_preimage(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    before = snapshot(path)
    if before["state"] == "absent":
        return before, b""
    if before["state"] != "file":
        raise DeployError(f"{label} exists but is not a plain file")
    data = path.read_bytes()
    observed = {"state": "file", "bytes": len(data), "sha256": sha256_bytes(data)}
    if observed != before:
        raise DeployError(f"{label} changed while its preimage was read")
    return before, data


def build_install_specs(
    package: Package,
    home: Path,
    python_executable: Path,
    transaction_id: str,
    *, remote_config: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    definitions = expected_target_defs(home)
    runtime_root = home / RUNTIME_RELATIVE
    coord_home = home / COORD_RELATIVE
    specs: list[dict[str, Any]] = []
    for definition in definitions:
        path = Path(definition["path"])
        identifier = str(definition["id"])
        if definition["kind"] == "tree":
            before = snapshot(path)
            if before["state"] != "absent":
                marker_name = RUNTIME_MARKER if identifier == "runtime" else SKILL_MARKER
                marker_path = path / marker_name
                if marker_path.is_file():
                    try:
                        marker = load_json_file(marker_path, f"{identifier} ownership marker")
                    except DeployError:
                        marker = {}
                    manager = marker.get("managed_by")
                    raise DeployError(
                        f"Target {identifier} is already present (manager={manager or 'unknown'}); "
                        "use verify/recover instead of replacing it"
                    )
                raise DeployError(f"Target {identifier} already exists without Agent Office ownership")
            if identifier == "runtime":
                desired = runtime_payload(
                    package, transaction_id, python_executable, runtime_root, coord_home, remote_config=remote_config
                )
            else:
                desired = skill_payload(package, transaction_id, str(definition["tool"]))
            specs.append({**definition, "before": before, "desired": desired})
            continue
        before, data = read_file_preimage(path, identifier)
        if identifier == "claude-settings":
            desired_bytes = patch_claude_settings(data)
        elif identifier == "claude-mcp":
            desired_bytes = patch_claude_mcp(
                data, claude_server(python_executable, runtime_root, coord_home, remote_config=remote_config)
            )
        elif identifier == "codex-mcp":
            desired_bytes = patch_codex_mcp(
                data, codex_server(python_executable, runtime_root, coord_home, remote_config=remote_config)
            )
        else:
            raise DeployError(f"Unknown installer target: {identifier}")
        specs.append({**definition, "before": before, "before_bytes": data, "desired": desired_bytes})
    order = [str(row["id"]) for row in specs]
    if not (order.index("claude-settings") < order.index("claude-mcp")):
        raise DeployError("Claude timeout must be staged before MCP activation")
    return specs


def build_upgrade_specs(
    package: Package,
    home: Path,
    python_executable: Path,
    transaction_id: str,
    previous_receipt: Mapping[str, Any],
    *, remote_config: Mapping[str, Any] | None = None,
    enable_wake_stop_gate: bool = False,
) -> list[dict[str, Any]]:
    """Build a new transaction over an exact, already-verified deployment.

    Tree targets must still equal the old receipt postimages. Config targets are
    read from their current live bytes and patched in place, so unrelated newer
    native settings are retained while owned Office drift remains a blocker.
    """
    if not isinstance(enable_wake_stop_gate, bool):
        raise DeployError("Wake STOP activation must be explicit boolean")
    previous_gate = receipt_wake_stop_gate(previous_receipt)
    gate_enabled = previous_gate or enable_wake_stop_gate
    previous_remote = receipt_remote_config(previous_receipt)
    remote_config = previous_remote if remote_config is None else normalize_remote_config(remote_config)
    definitions = expected_target_defs(home)
    old_rows = {str(row["id"]): row for row in previous_receipt["targets"]}
    specs: list[dict[str, Any]] = []
    runtime_root = home / RUNTIME_RELATIVE
    coord_home = home / COORD_RELATIVE
    for definition in definitions:
        path = Path(definition["path"])
        identifier = str(definition["id"])
        old_row = old_rows.get(identifier)
        if old_row is None:
            raise DeployError(f"Old receipt is missing upgrade target: {identifier}")
        before = snapshot(path)
        if definition["kind"] == "tree" and before != old_row["after"]:
            raise DeployError(f"Upgrade target drift blocks the transaction: {identifier}")
        if definition["kind"] == "tree":
            if identifier == "runtime":
                desired = runtime_payload(package, transaction_id, python_executable, runtime_root, coord_home, remote_config=remote_config)
            else:
                desired = skill_payload(package, transaction_id, str(definition["tool"]))
            specs.append({**definition, "before": before, "desired": desired})
            continue
        before_state, data = read_file_preimage(path, identifier)
        if before_state != before:
            raise DeployError(f"Upgrade config changed while its preimage was read: {identifier}")
        if identifier == "claude-settings":
            desired_bytes = patch_claude_settings(data)
            if gate_enabled:
                desired_bytes = patch_wake_stop_gate(
                    desired_bytes, wake_stop_hook(python_executable, runtime_root),
                    owned_previous=previous_gate,
                )
        elif identifier == "claude-mcp":
            desired_bytes = patch_claude_mcp(
                data, claude_server(python_executable, runtime_root, coord_home, remote_config=remote_config),
                owned_previous=claude_server(python_executable, runtime_root, coord_home, remote_config=previous_remote),
            )
        elif identifier == "codex-mcp":
            desired_bytes = patch_codex_mcp(
                data, codex_server(python_executable, runtime_root, coord_home, remote_config=remote_config),
                owned_previous=codex_server(python_executable, runtime_root, coord_home, remote_config=previous_remote),
            )
        else:
            raise DeployError(f"Unknown installer target: {identifier}")
        spec = {**definition, "before": before_state, "before_bytes": data, "desired": desired_bytes}
        if identifier == "claude-settings":
            spec["wake_stop_gate_enabled"] = gate_enabled
        specs.append(spec)
    return specs


def atomic_write(path: Path, data: bytes, *, private: bool = False) -> None:
    assert_plain_chain(path.parent, "write parent")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700 if private else 0o777)
    assert_plain_chain(path.parent, "write parent")
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if private:
            with contextlib.suppress(OSError):
                os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
        if private:
            with contextlib.suppress(OSError):
                os.chmod(path, 0o600)
    finally:
        if path_exists(temp_path):
            with contextlib.suppress(OSError):
                temp_path.unlink()


def atomic_write_json(path: Path, value: Mapping[str, Any], *, private: bool = False) -> None:
    atomic_write(path, json_bytes(value), private=private)


def write_tree(root: Path, files: Mapping[str, bytes], *, private: bool = False) -> None:
    if path_exists(root):
        raise DeployError(f"Refusing to stage over an existing path: {root}")
    assert_plain_chain(root.parent, "generated tree parent")
    root.mkdir(parents=True, mode=0o700 if private else 0o777)
    assert_plain_chain(root, "generated tree")
    for relative, data in sorted(files.items()):
        portable = safe_relative(relative, "generated tree")
        target = ensure_under(root.joinpath(*portable.parts), root, "generated tree file")
        atomic_write(target, data, private=private)


def copy_plain_tree(source: Path, destination: Path, *, private: bool = False) -> None:
    if path_exists(destination):
        raise DeployError(f"Refusing to copy over an existing tree: {destination}")
    assert_plain_chain(destination.parent, "copied tree parent")
    destination.mkdir(parents=True, mode=0o700 if private else 0o777)
    assert_plain_chain(destination, "copied tree")
    for child, relative in iter_plain_files(source):
        portable = safe_relative(relative, "copied tree")
        target = ensure_under(destination.joinpath(*portable.parts), destination, "copied tree file")
        atomic_write(target, child.read_bytes(), private=private)


def full_image(path: Path) -> dict[str, Any]:
    """Record every byte and empty directory, including ignored Python caches.

    The ordinary deployment snapshot intentionally ignores interpreter caches.
    A no-delete archive must instead account for everything it displaces.
    """
    assert_plain_chain(path, "preserved target")
    if not path_exists(path):
        return {"state": "absent"}
    if is_reparse(path):
        raise DeployError("Preserved target is a symlink/reparse point")
    if path.is_file():
        data = path.read_bytes()
        return {"state": "file", "bytes": len(data), "sha256": sha256_bytes(data)}
    if not path.is_dir():
        raise DeployError("Preserved target is not a plain file or directory")
    files: list[dict[str, Any]] = []
    directories: list[str] = []
    for current, dir_names, file_names in os.walk(path):
        parent = Path(current)
        for name in sorted(dir_names):
            child = parent / name
            if is_reparse(child) or not child.is_dir():
                raise DeployError("Preserved tree contains a non-plain directory")
            directories.append(child.relative_to(path).as_posix())
        for name in sorted(file_names):
            child = parent / name
            if is_reparse(child) or not child.is_file():
                raise DeployError("Preserved tree contains a non-plain file")
            data = child.read_bytes()
            files.append({"path": child.relative_to(path).as_posix(), "bytes": len(data), "sha256": sha256_bytes(data)})
    return {"state": "tree", "directories": sorted(directories), "files": sorted(files, key=lambda row: row["path"])}


def copy_full_image(source: Path, destination: Path) -> None:
    """Copy an archived tree with caches and empty directories intact."""
    expected = full_image(source)
    if expected["state"] != "tree" or path_exists(destination):
        raise DeployError("Full tree copy requires a plain source and an absent destination")
    assert_plain_chain(destination.parent, "full tree destination")
    destination.mkdir(parents=True, mode=0o700)
    for relative in expected["directories"]:
        portable = safe_relative(relative, "preserved directory")
        ensure_under(destination.joinpath(*portable.parts), destination, "preserved directory").mkdir(
            parents=True, exist_ok=True, mode=0o700
        )
    for row in expected["files"]:
        portable = safe_relative(row["path"], "preserved file")
        target = ensure_under(destination.joinpath(*portable.parts), destination, "preserved file")
        atomic_write(target, source.joinpath(*portable.parts).read_bytes(), private=True)
    if full_image(destination) != expected:
        raise DeployError("Full tree copy changed preserved bytes")


def remove_private_transaction(transaction_root: Path, transactions_root: Path) -> None:
    target = ensure_under(transaction_root, transactions_root, "transaction cleanup")
    if not path_exists(target):
        return
    if is_reparse(target) or not target.is_dir():
        raise DeployError("Transaction cleanup target is not a plain directory")
    shutil.rmtree(target)


def missing_parent_directories(path: Path, home: Path) -> list[str]:
    rows: list[Path] = []
    cursor = path.parent
    while cursor != home and home in cursor.parents:
        if not path_exists(cursor):
            rows.append(cursor)
        cursor = cursor.parent
    return [str(item) for item in reversed(rows)]


def image_path(transaction_root: Path, row: Mapping[str, Any], field: str) -> Path:
    if field not in {"before", "after"}:
        raise DeployError("Invalid transaction image field")
    identifier = str(row["id"])
    if SAFE_ID_RE.fullmatch(identifier) is None:
        raise DeployError("Receipt contains an unsafe target id")
    directory = "preimages" if field == "before" else "postimages"
    suffix = PRIVATE_FILE_SUFFIX if row[field]["state"] == "file" else ""
    return transaction_root / directory / f"{identifier}{suffix}"


def prepare_transaction(
    specs: list[dict[str, Any]],
    package: Package,
    home: Path,
    state_root: Path,
    transaction_id: str,
    python_executable: Path,
    *,
    no_delete: bool = False,
) -> tuple[Path, dict[str, Any]]:
    transactions_root = ensure_under(
        state_root / "transactions", state_root, "transactions root"
    )
    transactions_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    assert_plain_chain(transactions_root, "transactions root")
    transaction_root = ensure_under(
        transactions_root / transaction_id, transactions_root, "transaction root"
    )
    if path_exists(transaction_root):
        raise DeployError("Transaction id collision")
    transaction_root.mkdir(mode=0o700)
    rows: list[dict[str, Any]] = []
    created_directories: set[str] = set()
    try:
        for spec in specs:
            identifier = str(spec["id"])
            current = snapshot(Path(spec["path"]))
            if current != spec["before"]:
                raise DeployError(f"Preimage changed before transaction staging: {identifier}")
            if spec["kind"] == "tree":
                after = virtual_tree_snapshot(spec["desired"])
            else:
                desired_data = bytes(spec["desired"])
                after = {"state": "file", "bytes": len(desired_data), "sha256": sha256_bytes(desired_data)}
            row = {
                "id": identifier,
                "kind": str(spec["kind"]),
                "path": str(Path(spec["path"])),
                "target": str(spec["target"]),
                "tool": str(spec["tool"]),
                "before": spec["before"],
                "after": after,
            }
            if spec["before"]["state"] == "file":
                atomic_write(
                    image_path(transaction_root, row, "before"),
                    protect_private_bytes(bytes(spec["before_bytes"])),
                    private=True,
                )
            elif spec["before"]["state"] == "tree":
                copy_plain_tree(Path(spec["path"]), image_path(transaction_root, row, "before"), private=True)
            if spec["kind"] == "file":
                atomic_write(
                    image_path(transaction_root, row, "after"),
                    protect_private_bytes(bytes(spec["desired"])),
                    private=True,
                )
            else:
                write_tree(image_path(transaction_root, row, "after"), spec["desired"], private=True)
            rows.append(row)
            created_directories.update(missing_parent_directories(Path(spec["path"]), home))
        receipt: dict[str, Any] = {
            "schema_version": 1,
            "package_id": PACKAGE_ID,
            "package_version": PACKAGE_VERSION,
            "package_fingerprint": package.fingerprint,
            "runtime_fingerprint": package.runtime_fingerprint,
            "skill_fingerprint": package.skill_fingerprint,
            "transaction_id": transaction_id,
            "status": "PREPARED",
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "source": str(SOURCE_ROOT),
            "package_manifest": str(PACKAGE_PATH),
            "python_executable": str(python_executable),
            "coord_home": str(home / COORD_RELATIVE),
            "targets": rows,
            "created_directories": sorted(
                created_directories,
                key=lambda value: (len(Path(value).parts), value.lower()),
            ),
            "history": [{"action": "prepare", "status": "PREPARED", "at": utc_now()}],
        }
        if no_delete:
            receipt["preserve_mode"] = "no-delete"
            receipt["preserved_operations"] = []
        settings_spec = next(spec for spec in specs if spec["id"] == "claude-settings")
        if settings_spec.get("wake_stop_gate_enabled") is True:
            receipt["wake_stop_gate_enabled"] = True
        runtime_spec = next(spec for spec in specs if spec["id"] == "runtime")
        connection_bytes = runtime_spec["desired"].get(REMOTE_CONFIG_FILE)
        if connection_bytes is not None:
            receipt["remote_config"] = asdict(parse_remote_config_bytes(connection_bytes))
        atomic_write_json(transaction_root / "receipt.json", receipt, private=True)
        return transaction_root, receipt
    except Exception as exc:
        if no_delete:
            raise DeployError(
                "No-delete transaction preparation failed; private staging was preserved"
            ) from exc
        try:
            remove_private_transaction(transaction_root, transactions_root)
        except Exception as cleanup_exc:
            raise DeployError(
                f"Transaction preparation failed and private staging cleanup also failed: "
                f"{cleanup_exc.__class__.__name__}"
            ) from exc
        raise


@contextlib.contextmanager
def deployment_lock(state_root: Path) -> Iterator[None]:
    assert_plain_chain(state_root, "installer state root")
    state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    assert_plain_chain(state_root, "installer state root")
    lock = state_root / "deploy.lock"
    token = uuid.uuid4().hex.encode("ascii")
    try:
        descriptor = os.open(str(lock), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise DeployError("Another Agent Office installer transaction is active") from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(token)
            handle.flush()
            os.fsync(handle.fileno())
        yield
    finally:
        try:
            if lock.read_bytes() == token:
                lock.unlink()
        except OSError:
            pass


def valid_snapshot(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    state = value.get("state")
    if state == "absent":
        return set(value) == {"state"}
    if state == "file":
        return (
            set(value) == {"state", "bytes", "sha256"}
            and isinstance(value.get("bytes"), int)
            and not isinstance(value.get("bytes"), bool)
            and value["bytes"] >= 0
            and isinstance(value.get("sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", value["sha256"]) is not None
        )
    if state == "tree":
        return (
            set(value) == {"state", "files", "fingerprint"}
            and isinstance(value.get("files"), int)
            and not isinstance(value.get("files"), bool)
            and value["files"] >= 0
            and isinstance(value.get("fingerprint"), str)
            and re.fullmatch(r"[0-9a-f]{64}", value["fingerprint"]) is not None
        )
    return False


def valid_full_image(value: Any) -> bool:
    if valid_snapshot(value) and value["state"] in {"absent", "file"}:
        return True
    if not isinstance(value, dict) or set(value) != {"state", "directories", "files"} or value["state"] != "tree":
        return False
    directories, files = value["directories"], value["files"]
    if not isinstance(directories, list) or not isinstance(files, list):
        return False
    try:
        names = list(directories)
        if any(not isinstance(name, str) or safe_relative(name, "preserved directory").as_posix() != name for name in names):
            return False
        if names != sorted(set(names)):
            return False
        file_names: list[str] = []
        for row in files:
            if not isinstance(row, dict) or set(row) != {"path", "bytes", "sha256"}:
                return False
            name = row["path"]
            if not isinstance(name, str) or safe_relative(name, "preserved file").as_posix() != name:
                return False
            if not valid_snapshot({"state": "file", "bytes": row["bytes"], "sha256": row["sha256"]}):
                return False
            file_names.append(name)
        if file_names != sorted(set(file_names)):
            return False
        if set(names) & set(file_names):
            return False
    except DeployError:
        return False
    return True


def no_delete_receipt(receipt: Mapping[str, Any]) -> bool:
    return receipt.get("preserve_mode") == "no-delete"


def operation_path(transaction_root: Path, operation: Mapping[str, Any], part: str) -> Path:
    identifier = str(operation["target_id"])
    operation_id = str(operation["id"])
    if SAFE_ID_RE.fullmatch(identifier) is None or TRANSACTION_RE.fullmatch(operation_id) is None:
        raise DeployError("Unsafe preserved operation identity")
    if part == "archive":
        relative = PurePosixPath("preserved", operation_id, identifier)
    elif part == "stage":
        relative = PurePosixPath("activation", operation_id, identifier)
    elif part == "interrupted":
        relative = PurePosixPath("preserved", operation_id, f"{identifier}.interrupted")
    else:
        raise DeployError("Unknown preserved operation path")
    candidate = ensure_under(transaction_root.joinpath(*relative.parts), transaction_root, "preserved operation path")
    if operation.get(part) != relative.as_posix():
        raise DeployError("Preserved operation path binding mismatch")
    return candidate


def validate_preserved_operations(transaction_root: Path, receipt: Mapping[str, Any]) -> None:
    mode = receipt.get("preserve_mode")
    if mode is None:
        if "preserved_operations" in receipt or path_exists(transaction_root / "preserved"):
            raise DeployError("Legacy receipt has preserved upgrade artifacts")
        return
    if mode != "no-delete" or not isinstance(receipt.get("preserved_operations"), list):
        raise DeployError("Invalid preserved upgrade mode")
    rows = {str(row["id"]): row for row in receipt["targets"]}
    seen: set[str] = set()
    for operation in receipt["preserved_operations"]:
        if not isinstance(operation, dict) or set(operation) != {
            "id", "target_id", "from", "to", "archive", "stage", "interrupted",
            "displaced", "interrupted_image", "state",
        }:
            raise DeployError("Invalid preserved operation")
        operation_id = operation["id"]
        identifier = operation["target_id"]
        if not isinstance(operation_id, str) or TRANSACTION_RE.fullmatch(operation_id) is None or operation_id in seen:
            raise DeployError("Duplicate or unsafe preserved operation id")
        seen.add(operation_id)
        if (
            not isinstance(identifier, str) or identifier not in rows
            or not isinstance(operation["from"], str) or operation["from"] not in {"before", "after"}
            or not isinstance(operation["to"], str) or operation["to"] not in {"before", "after"}
            or operation["from"] == operation["to"]
        ):
            raise DeployError("Invalid preserved operation target or direction")
        if not isinstance(operation["state"], str) or operation["state"] not in {"INTENT", "COMPLETE", "COMPENSATED"}:
            raise DeployError("Invalid preserved operation state")
        if not valid_full_image(operation["displaced"]):
            raise DeployError("Invalid displaced target inventory")
        interrupted_image = operation["interrupted_image"]
        if interrupted_image is not None and not valid_full_image(interrupted_image):
            raise DeployError("Invalid interrupted target inventory")
        archive = operation_path(transaction_root, operation, "archive")
        stage = operation_path(transaction_root, operation, "stage")
        interrupted = operation_path(transaction_root, operation, "interrupted")
        if path_exists(stage):
            full_image(stage)
        if operation["displaced"]["state"] == "absent":
            if path_exists(archive):
                raise DeployError("Unexpected archive for an absent target")
        elif path_exists(archive):
            if full_image(archive) != operation["displaced"]:
                raise DeployError("Preserved archive differs from its exact inventory")
        elif operation["state"] == "COMPLETE":
            raise DeployError("Completed preserved operation lost its archive")
        if path_exists(interrupted):
            if interrupted_image is None or full_image(interrupted) != interrupted_image:
                raise DeployError("Interrupted archive differs from its exact inventory")
        elif interrupted_image is not None and operation["state"] == "COMPENSATED":
            raise DeployError("Compensated operation lost its interrupted archive")
    activation = ensure_under(transaction_root / "activation", transaction_root, "preserved activation root")
    if path_exists(activation):
        if is_reparse(activation) or not activation.is_dir():
            raise DeployError("Preserved activation root is unsafe")
        if any(child.name not in seen or is_reparse(child) or not child.is_dir() for child in activation.iterdir()):
            raise DeployError("Preserved activation contains an unknown operation")


def expected_target_map(home: Path) -> dict[str, dict[str, Any]]:
    return {str(row["id"]): row for row in expected_target_defs(home)}


def validate_receipt_images(transaction_root: Path, receipt: Mapping[str, Any]) -> None:
    for row in receipt["targets"]:
        for field in ("before", "after"):
            expected = row[field]
            image = image_path(transaction_root, row, field)
            if expected["state"] == "absent":
                if path_exists(image):
                    raise DeployError(f"Unexpected {field} image for absent target {row['id']}")
                continue
            if expected["state"] == "file":
                _read_file_image(transaction_root, row, field)
                continue
            actual = snapshot(image)
            if actual != expected:
                raise DeployError(f"Private {field} image mismatch for target {row['id']}")


def load_receipt(
    home: Path, state_root: Path, transaction_id: str, *, allow_package_drift: bool = False
) -> tuple[Path, dict[str, Any]]:
    if TRANSACTION_RE.fullmatch(transaction_id) is None:
        raise DeployError("Invalid transaction id")
    transaction_root = ensure_under(
        state_root / "transactions" / transaction_id,
        state_root,
        "transaction root",
    )
    receipt = load_json_file(transaction_root / "receipt.json", "Agent Office transaction receipt")
    if receipt.get("schema_version") != 1 or receipt.get("package_id") != PACKAGE_ID or receipt.get("transaction_id") != transaction_id:
        raise DeployError("Transaction receipt identity mismatch")
    if not allow_package_drift and receipt.get("package_version") != PACKAGE_VERSION:
        raise DeployError("Transaction receipt identity mismatch")
    if allow_package_drift and (
        not isinstance(receipt.get("package_version"), str)
        or not receipt["package_version"].strip()
    ):
        raise DeployError("Transaction receipt has an invalid package version")
    if receipt.get("status") not in RECEIPT_STATUSES:
        raise DeployError("Transaction receipt has an invalid status")
    for field in ("package_fingerprint", "runtime_fingerprint", "skill_fingerprint"):
        if not isinstance(receipt.get(field), str) or re.fullmatch(r"[0-9a-f]{64}", receipt[field]) is None:
            raise DeployError(f"Transaction receipt has an invalid {field}")
    if receipt.get("source") != str(SOURCE_ROOT) or receipt.get("package_manifest") != str(PACKAGE_PATH):
        raise DeployError("Transaction receipt source provenance mismatch")
    if receipt.get("coord_home") != str(home / COORD_RELATIVE):
        raise DeployError("Transaction receipt is bound to a different coord home")
    receipt_remote_config(receipt)
    receipt_wake_stop_gate(receipt)
    python_value = receipt.get("python_executable")
    if (
        not isinstance(python_value, str)
        or not Path(python_value).is_absolute()
        or python_value != str(absolute_lexical(Path(python_value)))
    ):
        raise DeployError("Transaction receipt has no absolute Python executable")
    rows = receipt.get("targets")
    if not isinstance(rows, list):
        raise DeployError("Transaction receipt targets must be a list")
    upgrade_receipt = any(
        isinstance(event, dict) and event.get("action") == "upgrade"
        for event in receipt.get("history", [])
    )
    if receipt.get("preserve_mode") is not None and not upgrade_receipt:
        raise DeployError("Preserved mode is valid only for an upgrade receipt")
    expected = expected_target_map(home)
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise DeployError("Transaction receipt target must be an object")
        identifier = row.get("id")
        if not isinstance(identifier, str) or SAFE_ID_RE.fullmatch(identifier) is None or identifier in seen:
            raise DeployError("Transaction receipt contains a duplicate or unsafe target id")
        definition = expected.get(identifier)
        if definition is None:
            raise DeployError(f"Transaction receipt contains an unknown target id: {identifier}")
        expected_path = os.path.normcase(str(absolute_lexical(Path(definition["path"]))))
        actual_path = row.get("path")
        if not isinstance(actual_path, str) or os.path.normcase(str(absolute_lexical(Path(actual_path)))) != expected_path:
            raise DeployError(f"Transaction receipt path binding mismatch for {identifier}")
        if (
            row.get("kind") != definition["kind"]
            or row.get("target") != definition["target"]
            or row.get("tool") != definition["tool"]
        ):
            raise DeployError(f"Transaction receipt provenance mismatch for {identifier}")
        if not valid_snapshot(row.get("before")) or not valid_snapshot(row.get("after")):
            raise DeployError(f"Transaction receipt snapshot is invalid for {identifier}")
        if definition["kind"] == "tree" and (
            (not upgrade_receipt and row["before"]["state"] != "absent")
            or row["after"]["state"] != "tree"
        ):
            raise DeployError(f"Transaction receipt has an invalid tree transition for {identifier}")
        if definition["kind"] == "file" and (
            row["before"]["state"] not in {"absent", "file"}
            or row["after"]["state"] != "file"
        ):
            raise DeployError(f"Transaction receipt has an invalid file transition for {identifier}")
        ensure_under(Path(actual_path), home, f"receipt target {identifier}")
        seen.add(identifier)
    if seen != set(expected):
        raise DeployError("Transaction receipt target set is incomplete")
    if [str(row["id"]) for row in rows] != list(expected):
        raise DeployError("Transaction receipt target order is invalid")
    created = receipt.get("created_directories")
    if (
        not isinstance(created, list)
        or any(not isinstance(item, str) for item in created)
        or len(created) != len(set(created))
    ):
        raise DeployError("Transaction receipt created_directories is invalid")
    allowed_created: set[str] = set()
    for definition in expected.values():
        cursor = Path(definition["path"]).parent
        while cursor != home and home in cursor.parents:
            allowed_created.add(os.path.normcase(str(absolute_lexical(cursor))))
            cursor = cursor.parent
    for item in created:
        checked = ensure_under(Path(item), home, "created directory")
        if os.path.normcase(str(checked)) not in allowed_created:
            raise DeployError("Transaction receipt contains an unrelated created directory")
    validate_receipt_images(transaction_root, receipt)
    validate_preserved_operations(transaction_root, receipt)
    return transaction_root, receipt


def _receipt_marker(receipt: Mapping[str, Any], row: Mapping[str, Any]) -> dict[str, Any]:
    identifier = str(row["id"])
    if identifier == "runtime":
        marker = {
            "schema_version": 1,
            "managed_by": MANAGED_BY,
            "package_id": PACKAGE_ID,
            "package_version": receipt["package_version"],
            "package_fingerprint": receipt["package_fingerprint"],
            "source_fingerprint": receipt["runtime_fingerprint"],
            "tool": "runtime",
            "python_executable": receipt["python_executable"],
            "transaction_id": receipt["transaction_id"],
        }
        remote = receipt_remote_config(receipt)
        if remote is not None:
            marker["remote_config_sha256"] = sha256_bytes(json_bytes(remote))
        return marker
    return {
        "schema_version": 1,
        "managed_by": MANAGED_BY,
        "capability_id": PACKAGE_ID,
        "capability_version": receipt["package_version"],
        "package_id": PACKAGE_ID,
        "package_fingerprint": receipt["package_fingerprint"],
        "source_fingerprint": receipt["skill_fingerprint"],
        "tool": row["tool"],
        "transaction_id": receipt["transaction_id"],
    }


def verify_upgrade_preimage(
    transaction_root: Path, receipt: Mapping[str, Any], home: Path
) -> None:
    """Require the old deployed receipt's exact live postimages and provenance."""
    if receipt.get("status") != "DEPLOYED":
        raise DeployError("Upgrade requires a DEPLOYED source receipt")
    if not no_delete_receipt(receipt):
        assert_activation_staging_clean(transaction_root)
    for row in receipt["targets"]:
        if row["kind"] == "tree" and snapshot(Path(row["path"])) != row["after"]:
            raise DeployError(f"Upgrade target drift blocks the transaction: {row['id']}")
    for row in receipt["targets"]:
        if row["kind"] != "tree":
            continue
        marker_name = RUNTIME_MARKER if row["id"] == "runtime" else SKILL_MARKER
        marker = load_json_file(Path(row["path"]) / marker_name, f"{row['id']} ownership marker")
        if marker != _receipt_marker(receipt, row):
            raise DeployError(f"Upgrade marker provenance differs from old receipt: {row['id']}")
    validate_config_semantics(receipt, home)


def _read_file_image(transaction_root: Path, row: Mapping[str, Any], field: str) -> bytes:
    path = image_path(transaction_root, row, field)
    assert_plain_chain(path, "private file image")
    if is_reparse(path) or not path.is_file():
        raise DeployError(f"Private {field} file image is missing or not plain for target {row['id']}")
    data = unprotect_private_bytes(path.read_bytes())
    expected = row[field]
    actual = {"state": "file", "bytes": len(data), "sha256": sha256_bytes(data)}
    if actual != expected:
        raise DeployError(f"Private {field} file image mismatch for target {row['id']}")
    return data


def _remove_exact_target(path: Path, expected: Mapping[str, Any], home: Path) -> None:
    ensure_under(path, home, "remove target")
    if snapshot(path) != expected:
        raise DeployError(f"Refusing to remove a target whose bytes changed: {path}")
    if expected["state"] == "file":
        path.unlink()
    elif expected["state"] == "tree":
        shutil.rmtree(path)
    elif expected["state"] != "absent":
        raise DeployError("Unsupported removal snapshot")


def _install_tree_image(
    image: Path,
    target: Path,
    expected_current: Mapping[str, Any],
    desired: Mapping[str, Any],
    home: Path,
    transaction_root: Path,
    identifier: str,
) -> None:
    if expected_current["state"] not in {"absent", "tree"}:
        raise DeployError("Refusing to replace a non-tree target")
    ensure_under(target, home, "tree target")
    target.parent.mkdir(parents=True, exist_ok=True)
    assert_plain_chain(target.parent, "tree target parent")
    activation_root = ensure_under(
        transaction_root / "activation", transaction_root, "tree activation root"
    )
    activation_root.mkdir(exist_ok=True, mode=0o700)
    assert_plain_chain(activation_root, "tree activation root")
    stage = ensure_under(
        activation_root / identifier, activation_root, "tree activation stage"
    )
    backup = ensure_under(
        activation_root / f"{identifier}.previous", activation_root, "tree activation backup"
    )
    if path_exists(stage):
        raise DeployError(f"Tree activation stage already exists: {stage}")
    if path_exists(backup):
        raise DeployError(f"Tree activation backup already exists: {backup}")
    moved_previous = False
    installed = False
    try:
        copy_plain_tree(image, stage)
        if snapshot(stage) != desired:
            raise DeployError(f"Staged tree hash mismatch for target {identifier}")
        if snapshot(target) != expected_current:
            raise DeployError(f"Target changed immediately before tree activation: {identifier}")
        if expected_current["state"] == "tree":
            os.replace(target, backup)
            moved_previous = True
        os.replace(stage, target)
        installed = True
        if moved_previous:
            shutil.rmtree(backup)
    finally:
        if path_exists(stage):
            if is_reparse(stage) or not stage.is_dir():
                raise DeployError(f"Tree activation stage became unsafe: {identifier}")
            shutil.rmtree(stage)
        if moved_previous and path_exists(backup):
            if installed and snapshot(target) == desired:
                shutil.rmtree(target)
            if not path_exists(target):
                os.replace(backup, target)
        if path_exists(activation_root):
            activation_root.rmdir()


def assert_activation_staging_clean(transaction_root: Path) -> None:
    activation_root = ensure_under(
        transaction_root / "activation", transaction_root, "tree activation root"
    )
    if path_exists(activation_root):
        raise DeployError("Private tree activation staging is not clean; use recover")


def cleanup_activation_staging(
    transaction_root: Path, receipt: Mapping[str, Any]
) -> None:
    activation_root = ensure_under(
        transaction_root / "activation", transaction_root, "tree activation root"
    )
    if not path_exists(activation_root):
        return
    if is_reparse(activation_root) or not activation_root.is_dir():
        raise DeployError("Private tree activation root is not a plain directory")
    expected = {
        str(row["id"])
        for row in receipt["targets"]
        if row["kind"] == "tree"
    }
    children = list(activation_root.iterdir())
    for child in children:
        if child.name not in expected or is_reparse(child) or not child.is_dir():
            raise DeployError("Private tree activation staging contains an unknown entry")
    for child in children:
        shutil.rmtree(child)
    activation_root.rmdir()


def _activate_target(
    row: Mapping[str, Any],
    transaction_root: Path,
    from_field: str,
    to_field: str,
    home: Path,
) -> None:
    path = ensure_under(Path(str(row["path"])), home, f"activation target {row['id']}")
    expected_current = row[from_field]
    desired = row[to_field]
    current = snapshot(path)
    if current != expected_current:
        raise DeployError(f"Target changed immediately before mutation: {row['id']}")
    if desired == expected_current:
        return
    state = desired["state"]
    if state == "absent":
        _remove_exact_target(path, expected_current, home)
    elif state == "file":
        if expected_current["state"] not in {"absent", "file"}:
            raise DeployError(f"Refusing to replace a non-file target: {row['id']}")
        data = _read_file_image(transaction_root, row, to_field)
        if snapshot(path) != expected_current:
            raise DeployError(f"Target changed immediately before atomic replace: {row['id']}")
        atomic_write(path, data)
    elif state == "tree":
        image = image_path(transaction_root, row, to_field)
        _install_tree_image(
            image,
            path,
            expected_current,
            desired,
            home,
            transaction_root,
            str(row["id"]),
        )
    else:
        raise DeployError(f"Unsupported desired target state: {state}")
    if snapshot(path) != desired:
        raise DeployError(f"Target postimage verification failed: {row['id']}")


def persist_preserved_operations(transaction_root: Path, receipt: dict[str, Any], operations: list[dict[str, Any]]) -> None:
    updated = dict(receipt)
    updated["preserved_operations"] = operations
    updated["updated_at"] = utc_now()
    atomic_write_json(transaction_root / "receipt.json", updated, private=True)
    receipt.clear()
    receipt.update(updated)


def move_without_replace(source: Path, destination: Path) -> None:
    """Windows rename refuses an occupied destination, including a late race."""
    if os.name != "nt":
        raise DeployError("No-delete activation requires Windows no-overwrite rename semantics")
    assert_plain_chain(source, "no-delete move source")
    assert_plain_chain(destination.parent, "no-delete move destination parent")
    if path_exists(destination):
        raise DeployError("No-delete move destination already exists")
    os.rename(source, destination)


def preserved_tree_source(
    receipt: Mapping[str, Any], transaction_root: Path, row: Mapping[str, Any], field: str
) -> Path:
    for operation in reversed(receipt["preserved_operations"]):
        if operation["target_id"] != row["id"] or operation["from"] != field:
            continue
        archive = operation_path(transaction_root, operation, "archive")
        if not path_exists(archive):
            continue
        if full_image(archive) != operation["displaced"] or snapshot(archive) != row[field]:
            raise DeployError(f"Preserved tree source changed: {row['id']}")
        return archive
    if field == "before":
        raise DeployError(f"Original tree archive is unavailable: {row['id']}")
    return image_path(transaction_root, row, field)


def stage_preserved_target(
    receipt: Mapping[str, Any], transaction_root: Path, row: Mapping[str, Any],
    field: str, stage: Path,
) -> None:
    desired = row[field]
    if desired["state"] == "absent":
        return
    if path_exists(stage):
        raise DeployError("Preserved activation stage already exists")
    stage.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    assert_plain_chain(stage.parent, "preserved activation stage parent")
    if desired["state"] == "tree":
        source = preserved_tree_source(receipt, transaction_root, row, field)
        copy_full_image(source, stage)
    else:
        atomic_write(stage, _read_file_image(transaction_root, row, field), private=True)
    if snapshot(stage) != desired:
        raise DeployError(f"Preserved activation stage hash mismatch: {row['id']}")


def activate_preserved_target(
    receipt: dict[str, Any], transaction_root: Path, row: Mapping[str, Any],
    from_field: str, to_field: str, home: Path,
) -> None:
    path = ensure_under(Path(str(row["path"])), home, f"preserved activation target {row['id']}")
    expected, desired = row[from_field], row[to_field]
    if snapshot(path) != expected:
        raise DeployError(f"Preserved target changed before mutation: {row['id']}")
    if expected == desired:
        return
    operation_id = uuid.uuid4().hex
    identifier = str(row["id"])
    operation: dict[str, Any] = {
        "id": operation_id, "target_id": identifier, "from": from_field, "to": to_field,
        "archive": f"preserved/{operation_id}/{identifier}",
        "stage": f"activation/{operation_id}/{identifier}",
        "interrupted": f"preserved/{operation_id}/{identifier}.interrupted",
        "displaced": full_image(path), "interrupted_image": None, "state": "INTENT",
    }
    stage = operation_path(transaction_root, operation, "stage")
    archive = operation_path(transaction_root, operation, "archive")
    operations = [*receipt["preserved_operations"], operation]
    persist_preserved_operations(transaction_root, receipt, operations)
    stage_preserved_target(receipt, transaction_root, row, to_field, stage)
    if snapshot(path) != expected or full_image(path) != operation["displaced"]:
        raise DeployError(f"Preserved target changed before archival: {identifier}")
    if expected["state"] != "absent":
        archive.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        assert_plain_chain(archive.parent, "preserved archive parent")
        if path_exists(archive):
            raise DeployError("Preserved archive path already exists")
        move_without_replace(path, archive)
        if full_image(archive) != operation["displaced"]:
            raise DeployError(f"Displaced target changed during archival: {identifier}")
    if desired["state"] != "absent":
        if path_exists(path):
            raise DeployError(f"Preserved target reappeared during activation: {identifier}")
        path.parent.mkdir(parents=True, exist_ok=True)
        move_without_replace(stage, path)
    if snapshot(path) != desired:
        raise DeployError(f"Preserved target postimage mismatch: {identifier}")
    completed = dict(operation)
    completed["state"] = "COMPLETE"
    persist_preserved_operations(transaction_root, receipt, [*receipt["preserved_operations"][:-1], completed])


def restore_full_image(source: Path, target: Path) -> None:
    expected = full_image(source)
    if path_exists(target):
        raise DeployError("Cannot restore preserved bytes over a live target")
    stage = source.parent / f"{source.name}.restore.{uuid.uuid4().hex}"
    if expected["state"] == "tree":
        copy_full_image(source, stage)
    elif expected["state"] == "file":
        atomic_write(stage, source.read_bytes(), private=True)
    else:
        raise DeployError("Preserved source is absent")
    move_without_replace(stage, target)
    if full_image(target) != expected:
        raise DeployError("Restored preserved bytes differ from archive")


def normalize_pending_preserved(
    receipt: dict[str, Any], transaction_root: Path, home: Path,
) -> None:
    for index, operation in enumerate(list(receipt["preserved_operations"])):
        if operation["state"] != "INTENT":
            continue
        row = next(row for row in receipt["targets"] if row["id"] == operation["target_id"])
        path = ensure_under(Path(str(row["path"])), home, "pending preserved target")
        before, after = row[operation["from"]], row[operation["to"]]
        archive = operation_path(transaction_root, operation, "archive")
        interrupted = operation_path(transaction_root, operation, "interrupted")
        archived = path_exists(archive)
        current = snapshot(path)
        if archived and full_image(archive) != operation["displaced"]:
            raise DeployError("Pending preserved archive changed")
        if before["state"] != "absent" and not archived and current != before:
            raise DeployError("Pending preserved original is missing")
        if current != before and current != after and current["state"] != "absent":
            raise DeployError("Pending preserved target has unknown bytes")
        if current == after and current != before:
            if operation["interrupted_image"] is None:
                updated = dict(operation)
                updated["interrupted_image"] = full_image(path)
                operations = list(receipt["preserved_operations"])
                operations[index] = updated
                persist_preserved_operations(transaction_root, receipt, operations)
                operation = updated
            if path_exists(interrupted):
                raise DeployError("Pending preserved interrupted archive already exists")
            interrupted.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            move_without_replace(path, interrupted)
            if full_image(interrupted) != operation["interrupted_image"]:
                raise DeployError("Interrupted preserved archive changed")
            current = snapshot(path)
        if current["state"] == "absent" and before["state"] != "absent":
            if not archived:
                raise DeployError("Pending preserved original archive is missing")
            restore_full_image(archive, path)
        elif current != before:
            raise DeployError("Pending preserved target cannot be normalized")
        updated = dict(operation)
        updated["state"] = "COMPENSATED"
        operations = list(receipt["preserved_operations"])
        operations[index] = updated
        persist_preserved_operations(transaction_root, receipt, operations)


def compensate_preserved(
    receipt: dict[str, Any], transaction_root: Path,
    from_field: str, to_field: str, home: Path,
) -> tuple[list[str], list[str]]:
    drift: list[str] = []
    errors: list[str] = []
    try:
        normalize_pending_preserved(receipt, transaction_root, home)
    except Exception:
        errors.append("pending-operation")
        return drift, errors
    rows = list(receipt["targets"])
    if from_field == "before":
        rows.reverse()
    for row in rows:
        try:
            current = snapshot(Path(str(row["path"])))
            if current == row[from_field]:
                continue
            if current == row[to_field]:
                activate_preserved_target(receipt, transaction_root, row, to_field, from_field, home)
            else:
                drift.append(str(row["id"]))
        except Exception:
            errors.append(str(row["id"]))
    return drift, errors


def transition_all_preserved(
    receipt: dict[str, Any], transaction_root: Path,
    from_field: str, to_field: str, home: Path,
) -> None:
    assert_all_match(receipt, from_field)
    try:
        rows = list(receipt["targets"])
        if to_field == "before":
            rows.reverse()
        for row in rows:
            activate_preserved_target(receipt, transaction_root, row, from_field, to_field, home)
        assert_all_match(receipt, to_field)
    except Exception as exc:
        drift, errors = compensate_preserved(receipt, transaction_root, from_field, to_field, home)
        complete = not drift and not errors and all_match(receipt, from_field)
        raise TransitionFailure(
            f"Preserved target transition failed; compensation={'complete' if complete else 'incomplete'}",
            compensation_complete=complete,
            drift_ids=sorted(set(drift + errors)),
        ) from exc


def all_match(receipt: Mapping[str, Any], field: str) -> bool:
    return all(snapshot(Path(str(row["path"]))) == row[field] for row in receipt["targets"])


def assert_all_match(receipt: Mapping[str, Any], field: str) -> None:
    for row in receipt["targets"]:
        if snapshot(Path(str(row["path"]))) != row[field]:
            raise DeployError(f"Target drift blocks the transaction: {row['id']} != {field}")


def unknown_transition_targets(receipt: Mapping[str, Any]) -> list[str]:
    unknown: list[str] = []
    for row in receipt["targets"]:
        current = snapshot(Path(str(row["path"])))
        if current != row["before"] and current != row["after"]:
            unknown.append(str(row["id"]))
    return unknown


def compensate_transition(
    receipt: Mapping[str, Any],
    transaction_root: Path,
    from_field: str,
    to_field: str,
    home: Path,
) -> tuple[list[str], list[str]]:
    drift_ids: list[str] = []
    errors: list[str] = []
    rows = list(receipt["targets"])
    if from_field == "before":
        rows.reverse()
    for row in rows:
        try:
            current = snapshot(Path(str(row["path"])))
            if current == row[from_field]:
                continue
            if current == row[to_field]:
                _activate_target(row, transaction_root, to_field, from_field, home)
            else:
                drift_ids.append(str(row["id"]))
        except Exception:
            errors.append(str(row["id"]))
    return drift_ids, errors


def transition_all(
    receipt: Mapping[str, Any],
    transaction_root: Path,
    from_field: str,
    to_field: str,
    home: Path,
) -> None:
    assert_all_match(receipt, from_field)
    try:
        rows = list(receipt["targets"])
        if to_field == "before":
            rows.reverse()
        for row in rows:
            _activate_target(row, transaction_root, from_field, to_field, home)
        assert_all_match(receipt, to_field)
    except Exception as exc:
        drift_ids, errors = compensate_transition(receipt, transaction_root, from_field, to_field, home)
        try:
            cleanup_activation_staging(transaction_root, receipt)
        except Exception:
            errors.append("activation-staging")
        complete = not drift_ids and not errors and all_match(receipt, from_field)
        detail = "complete" if complete else "incomplete"
        raise TransitionFailure(
            f"Target transition failed; compensation={detail}",
            compensation_complete=complete,
            drift_ids=sorted(set(drift_ids + errors)),
        ) from exc


def prune_created_directories(receipt: Mapping[str, Any], home: Path) -> None:
    rows = sorted(
        (ensure_under(Path(item), home, "created directory") for item in receipt.get("created_directories", [])),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for path in rows:
        try:
            path.rmdir()
        except (FileNotFoundError, OSError):
            continue


def validate_claude_mcp_bytes(data: bytes, desired: Mapping[str, Any]) -> None:
    text, _ = decode_json_document(data, "Claude user MCP config")
    root = root_object_span(text)
    servers_span = member_span(text, root, "mcpServers")
    if servers_span is None:
        raise DeployError("Claude mcpServers is missing")
    servers = object_span_at(text, servers_span, "Claude mcpServers")
    office_span = member_span(text, servers, PACKAGE_ID)
    if office_span is None or value_at(text, office_span) != desired:
        raise DeployError("Claude agent-office MCP entry is not current")


def validate_claude_settings_bytes(data: bytes) -> None:
    text, _ = decode_json_document(data, "Claude settings")
    root = root_object_span(text)
    env_span = member_span(text, root, "env")
    if env_span is None:
        raise DeployError("Claude settings env is missing")
    env = object_span_at(text, env_span, "Claude settings env")
    aliases = [name for name, _, _ in object_members(text, env) if name.upper() == "MCP_TOOL_TIMEOUT"]
    if aliases != ["MCP_TOOL_TIMEOUT"]:
        raise DeployError("Claude settings contain a missing or conflicting MCP_TOOL_TIMEOUT")
    timeout_span = member_span(text, env, "MCP_TOOL_TIMEOUT")
    numeric = _timeout_value(value_at(text, timeout_span)) if timeout_span else None
    if numeric is None or numeric < CLAUDE_TIMEOUT_MIN_MS:
        raise DeployError("Claude MCP_TOOL_TIMEOUT is not an effective >=300000 ms value")


def validate_codex_mcp_bytes(data: bytes, desired: Mapping[str, Any]) -> None:
    text, _ = decode_toml_document(data)
    parsed = tomllib.loads(text)
    servers = parsed.get("mcp_servers", {})
    if not isinstance(servers, dict) or servers.get(PACKAGE_ID) != desired:
        raise DeployError("Codex agent-office MCP entry or timeout values are not current")


def validate_owned_config_semantics(
    identifier: str, receipt: Mapping[str, Any], home: Path
) -> None:
    """Re-check only the Agent-Office-owned semantics of one shared-config target.

    The live file is re-read and validated against the exact fields the installer
    owns: the Claude ``mcpServers.agent-office`` entry, the Claude
    ``env.MCP_TOOL_TIMEOUT`` value, or the Codex ``mcp_servers.agent-office``
    table. Unrelated foreign keys are ignored by design, so a native tool that
    appends its own metadata still validates. Owned-field mutation or removal,
    malformed/duplicate JSON or TOML, and a missing file all raise
    ``DeployError`` and therefore still fail.
    """
    by_id = {str(row["id"]): row for row in receipt["targets"]}
    path = Path(str(by_id[identifier]["path"]))
    if identifier == "claude-settings":
        validate_claude_settings_bytes(path.read_bytes())
        if receipt_wake_stop_gate(receipt):
            validate_wake_stop_gate_bytes(
                path.read_bytes(),
                wake_stop_hook(Path(str(receipt["python_executable"])), home / RUNTIME_RELATIVE),
            )
    elif identifier == "claude-mcp":
        validate_claude_mcp_bytes(
            path.read_bytes(),
            claude_server(
                Path(str(receipt["python_executable"])),
                home / RUNTIME_RELATIVE,
                home / COORD_RELATIVE,
                remote_config=receipt_remote_config(receipt),
            ),
        )
    elif identifier == "codex-mcp":
        validate_codex_mcp_bytes(
            path.read_bytes(),
            codex_server(
                Path(str(receipt["python_executable"])),
                home / RUNTIME_RELATIVE,
                home / COORD_RELATIVE,
                remote_config=receipt_remote_config(receipt),
            ),
        )
    else:
        raise DeployError(f"{identifier} has no owned-config semantic validator")


def validate_config_semantics(receipt: Mapping[str, Any], home: Path) -> None:
    for identifier in OWNED_CONFIG_VERIFY:
        validate_owned_config_semantics(identifier, receipt, home)


def update_receipt(
    transaction_root: Path,
    receipt: dict[str, Any],
    *,
    status: str,
    action: str,
    details: Mapping[str, Any] | None = None,
) -> None:
    if status not in RECEIPT_STATUSES:
        raise DeployError(f"Invalid receipt status transition: {status}")
    updated = dict(receipt)
    updated["status"] = status
    updated["updated_at"] = utc_now()
    event: dict[str, Any] = {"action": action, "status": status, "at": utc_now()}
    if details:
        event.update(details)
    history = list(receipt.get("history", []))
    history.append(event)
    updated["history"] = history
    atomic_write_json(transaction_root / "receipt.json", updated, private=True)
    receipt.clear()
    receipt.update(updated)


def _marker_from_runtime(runtime_root: Path) -> dict[str, Any]:
    if not runtime_root.is_dir():
        raise DeployError("Agent Office runtime target is not a directory")
    return load_json_file(runtime_root / RUNTIME_MARKER, "Agent Office runtime marker")


def validate_runtime_marker_static(
    package: Package, marker: Mapping[str, Any]
) -> tuple[str, Path]:
    raw_transaction = marker.get("transaction_id")
    if not isinstance(raw_transaction, str) or TRANSACTION_RE.fullmatch(raw_transaction) is None:
        raise DeployError("Runtime marker has an invalid transaction binding")
    marker_python = marker.get("python_executable")
    if not isinstance(marker_python, str) or not Path(marker_python).is_absolute():
        raise DeployError("Runtime marker has no absolute Python binding")
    lexical_python = absolute_lexical(Path(marker_python))
    expected = runtime_marker(package, raw_transaction, lexical_python)
    if "remote_config_sha256" in marker:
        digest = marker["remote_config_sha256"]
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise DeployError("Runtime marker has an invalid remote connection digest")
        expected["remote_config_sha256"] = digest
    if marker != expected:
        raise DeployError("Runtime marker provenance differs from the current source package")
    return raw_transaction, lexical_python


def _verification_error(message: str, *, transaction_id: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action": "verify",
        "status": "DRIFT",
        "passed": False,
        "diagnostics": [{"code": "verification-error", "message": message}],
    }
    if transaction_id:
        payload["transaction_id"] = transaction_id
    return payload


def verify_install(
    home: Path,
    appdata: Path | None = None,
    state_root: Path | None = None,
    python_executable: Path | None = None,
    *,
    strict: bool = True,
) -> dict[str, Any]:
    """Verify the live deployment against the transaction receipt.

    ``strict`` defaults to ``True``, so every target must still match its recorded
    image byte for byte: that is the compare-and-set install and recover rely on
    before and after they mutate. Only the read-only surfaces pass
    ``strict=False``, which reports unrelated foreign fields in the three owned
    shared configs as visible non-equality instead of a failed install.
    """
    normalized_home, _, normalized_state = normalize_layout(home, appdata, state_root)
    runtime_root = normalized_home / RUNTIME_RELATIVE
    if not path_exists(runtime_root):
        return {
            "action": "verify",
            "status": "NOT_INSTALLED",
            "passed": False,
            "runtime": str(runtime_root),
        }
    transaction_id: str | None = None
    try:
        package = verify_package()
        marker = _marker_from_runtime(runtime_root)
        transaction_id, marker_python = validate_runtime_marker_static(package, marker)
        transaction_root, receipt = load_receipt(normalized_home, normalized_state, transaction_id)
        if marker != _receipt_marker(receipt, {"id": "runtime"}):
            raise DeployError("Runtime marker remote connection differs from its receipt")
        if not no_delete_receipt(receipt):
            assert_activation_staging_clean(transaction_root)
        if receipt.get("status") != "DEPLOYED":
            raise DeployError(f"Transaction receipt is not DEPLOYED: {receipt.get('status')}")
        if (
            receipt.get("package_fingerprint") != package.fingerprint
            or receipt.get("runtime_fingerprint") != package.runtime_fingerprint
            or receipt.get("skill_fingerprint") != package.skill_fingerprint
            or receipt.get("python_executable") != str(marker_python)
        ):
            raise DeployError("Transaction receipt differs from the current package or Python binding")
        requested_python = absolute_lexical(python_executable) if python_executable else marker_python
        if os.path.normcase(str(requested_python)) != os.path.normcase(str(marker_python)):
            raise DeployError("Requested Python executable differs from the installed binding")
        pinned = pin_python(requested_python)
        if os.path.normcase(str(pinned)) != os.path.normcase(str(marker_python)):
            raise DeployError("Verified Python executable differs from the installed binding")
        by_id = {str(row["id"]): row for row in receipt["targets"]}
        target_reports: list[dict[str, Any]] = []
        for row in receipt["targets"]:
            identifier = str(row["id"])
            current = snapshot(Path(str(row["path"])))
            equal = current == row["after"]
            report: dict[str, Any] = {
                "id": identifier,
                "path": row["path"],
                "tool": row["tool"],
                "current": current,
                "expected": row["after"],
                "passed": equal,
            }
            if not equal:
                # The whole-file bytes differ from the recorded postimage. Only an
                # explicitly non-strict caller - the read-only standalone verify and
                # dry-run - may tolerate that, and only for the three explicitly owned
                # shared-config targets while their Agent-Office-owned semantics still
                # validate exactly; unrelated foreign fields a native tool appends
                # (such as Claude's top-level "skillUsage") are then reported as visible
                # non-equality rather than a failed install. Strict mode is the default,
                # so every install and recover check and the whole rollback CAS fail
                # closed here on any byte difference, for those targets as well as for
                # a non-file state, an owned-field mutation/removal, or a
                # malformed/duplicate document.
                if strict or identifier not in OWNED_CONFIG_VERIFY or current["state"] != "file":
                    raise DeployError(
                        f"Installed target bytes differ from the transaction postimage: {identifier}"
                    )
                validate_owned_config_semantics(identifier, receipt, normalized_home)
                report["passed"] = True
                report["whole_file_equal"] = False
                report["owned_semantics_current"] = True
                report["unrelated_field_drift"] = True
            target_reports.append(report)
        foreign_drift_ids = [
            str(item["id"]) for item in target_reports if item.get("unrelated_field_drift")
        ]
        expected_runtime = virtual_tree_snapshot(
            runtime_payload(
                package,
                transaction_id,
                pinned,
                runtime_root,
                normalized_home / COORD_RELATIVE,
                remote_config=receipt_remote_config(receipt),
            )
        )
        if snapshot(runtime_root) != expected_runtime or by_id["runtime"]["after"] != expected_runtime:
            raise DeployError("Runtime code bytes differ from the explicit package allowlist")
        for identifier, _, relative, tool in SKILL_TARGETS:
            target = normalized_home / relative
            expected_skill = virtual_tree_snapshot(skill_payload(package, transaction_id, tool))
            if snapshot(target) != expected_skill or by_id[identifier]["after"] != expected_skill:
                raise DeployError(f"Skill code/marker provenance mismatch: {identifier}")
            marker_value = load_json_file(target / SKILL_MARKER, f"{identifier} marker")
            if marker_value != skill_marker(package, transaction_id, tool):
                raise DeployError(f"Skill marker tool/source binding mismatch: {identifier}")
        validate_receipt_images(transaction_root, receipt)
        validate_config_semantics(receipt, normalized_home)
        return {
            "action": "verify",
            "status": "CURRENT",
            "passed": True,
            "package_id": PACKAGE_ID,
            "package_version": PACKAGE_VERSION,
            "package_fingerprint": package.fingerprint,
            "transaction_id": transaction_id,
            "python_executable": str(pinned),
            "runtime": str(runtime_root),
            "coord_home": str(normalized_home / COORD_RELATIVE),
            "targets": target_reports,
            "unrelated_config_drift": foreign_drift_ids,
            "deployment_mode": "remote" if receipt_remote_config(receipt) is not None else "local",
        }
    except DeployError as exc:
        return _verification_error(str(exc), transaction_id=transaction_id)
    except (OSError, UnicodeError, ValueError) as exc:
        return _verification_error(
            f"Filesystem/config verification failed ({exc.__class__.__name__})",
            transaction_id=transaction_id,
        )


def dry_run(
    home: Path,
    appdata: Path | None = None,
    state_root: Path | None = None,
    python_executable: Path | None = None,
    *, remote_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_home, normalized_appdata, normalized_state = normalize_layout(home, appdata, state_root)
    # Dry-run writes nothing, so it reports through the same tolerant read-only
    # verify surface as the standalone verify command.
    current = verify_install(
        normalized_home, normalized_appdata, normalized_state, python_executable, strict=False
    )
    if current.get("status") == "CURRENT":
        require_requested_connection(current, normalized_home, normalized_state, remote_config)
        return {**current, "action": "dry-run"}
    runtime_root = normalized_home / RUNTIME_RELATIVE
    if path_exists(runtime_root):
        return {
            "action": "dry-run",
            "status": "BLOCKED",
            "passed": False,
            "verify": current,
        }
    package = verify_package()
    pinned = pin_python(python_executable)
    specs = build_install_specs(package, normalized_home, pinned, "0" * 32, remote_config=remote_config)
    targets: list[dict[str, Any]] = []
    for spec in specs:
        if spec["kind"] == "tree":
            after = virtual_tree_snapshot(spec["desired"])
        else:
            data = bytes(spec["desired"])
            after = {"state": "file", "bytes": len(data), "sha256": sha256_bytes(data)}
        targets.append(
            {
                "id": spec["id"],
                "path": str(spec["path"]),
                "tool": spec["tool"],
                "current": spec["before"],
                "planned": after,
                "will_change": spec["before"] != after,
            }
        )
    return {
        "action": "dry-run",
        "status": "READY",
        "passed": True,
        "writes_performed": 0,
        "package_id": PACKAGE_ID,
        "package_version": PACKAGE_VERSION,
        "package_fingerprint": package.fingerprint,
        "python_executable": str(pinned),
        "runtime": str(runtime_root),
        "coord_home": str(normalized_home / COORD_RELATIVE),
        "state_root": str(normalized_state),
        "deployment_mode": "remote" if remote_config is not None else "local",
        "targets": targets,
    }


def _record_transition_failure(
    transaction_root: Path,
    receipt: dict[str, Any],
    action: str,
    failure: TransitionFailure,
    compensated_status: str,
) -> None:
    status = compensated_status if failure.compensation_complete else "FAILED"
    details: dict[str, Any] = {"compensation": "complete" if failure.compensation_complete else "incomplete"}
    if failure.drift_ids:
        details["drift_target_ids"] = failure.drift_ids
    update_receipt(transaction_root, receipt, status=status, action=action, details=details)


def compensate_failed_deployment(
    transaction_root: Path,
    receipt: dict[str, Any],
    home: Path,
    action: str,
) -> TransitionFailure:
    if no_delete_receipt(receipt):
        drift_ids, errors = compensate_preserved(receipt, transaction_root, "before", "after", home)
    else:
        drift_ids, errors = compensate_transition(receipt, transaction_root, "before", "after", home)
        try:
            cleanup_activation_staging(transaction_root, receipt)
        except Exception:
            errors.append("activation-staging")
    combined = sorted(set(drift_ids + errors))
    complete = not combined and all_match(receipt, "before")
    if complete and not no_delete_receipt(receipt):
        prune_created_directories(receipt, home)
    failure = TransitionFailure(
        f"{action.capitalize()} failed; target compensation="
        f"{'complete' if complete else 'incomplete'}",
        compensation_complete=complete,
        drift_ids=combined,
    )
    try:
        _record_transition_failure(
            transaction_root, receipt, action, failure, "AUTO_ROLLED_BACK"
        )
    except Exception as receipt_exc:
        raise TransitionFailure(
            f"{action.capitalize()} failed; target compensation="
            f"{'complete' if complete else 'incomplete'}; receipt update failed",
            compensation_complete=complete,
            drift_ids=combined,
        ) from receipt_exc
    return failure


def install(
    home: Path,
    appdata: Path | None = None,
    state_root: Path | None = None,
    python_executable: Path | None = None,
    *, remote_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_home, normalized_appdata, normalized_state = normalize_layout(home, appdata, state_root)
    initial = verify_install(
        normalized_home, normalized_appdata, normalized_state, python_executable, strict=True
    )
    require_requested_connection(initial, normalized_home, normalized_state, remote_config)
    if initial.get("status") != "CURRENT" and path_exists(normalized_home / RUNTIME_RELATIVE):
        raise DeployError("Existing Agent Office runtime is not current; refusing an ownership overwrite")
    package = verify_package()
    pinned = (
        Path(str(initial["python_executable"]))
        if initial.get("status") == "CURRENT"
        else pin_python(python_executable)
    )
    with deployment_lock(normalized_state):
        raced = verify_install(
            normalized_home, normalized_appdata, normalized_state, pinned, strict=True
        )
        if raced.get("status") == "CURRENT":
            require_requested_connection(raced, normalized_home, normalized_state, remote_config)
            return {**raced, "action": "install"}
        if path_exists(normalized_home / RUNTIME_RELATIVE):
            raise DeployError("Agent Office runtime appeared during install preflight")
        transaction_id = uuid.uuid4().hex
        specs = build_install_specs(package, normalized_home, pinned, transaction_id, remote_config=remote_config)
        transaction_root, receipt = prepare_transaction(
            specs, package, normalized_home, normalized_state, transaction_id, pinned
        )
        try:
            update_receipt(transaction_root, receipt, status="APPLYING", action="install")
        except Exception as exc:
            transactions_root = normalized_state / "transactions"
            try:
                remove_private_transaction(transaction_root, transactions_root)
            except Exception as cleanup_exc:
                raise DeployError(
                    "Install activation did not start, but private transaction cleanup failed"
                ) from cleanup_exc
            raise DeployError("Install activation did not start; private transaction was removed") from exc
        try:
            transition_all(receipt, transaction_root, "before", "after", normalized_home)
            validate_config_semantics(receipt, normalized_home)
            update_receipt(transaction_root, receipt, status="DEPLOYED", action="install")
            final = verify_install(
                normalized_home, normalized_appdata, normalized_state, pinned, strict=True
            )
            if final.get("status") != "CURRENT":
                raise DeployError("Install final verification did not return CURRENT")
        except Exception as exc:
            failure = compensate_failed_deployment(
                transaction_root, receipt, normalized_home, "install"
            )
            raise failure from exc
        return receipt


def upgrade(
    home: Path,
    appdata: Path | None,
    state_root: Path | None,
    transaction_id: str,
    python_executable: Path | None = None,
    *,
    no_delete: bool = False,
    preview: bool = False,
    remote_config: Mapping[str, Any] | None = None,
    enable_wake_stop_gate: bool = False,
) -> dict[str, Any]:
    """Upgrade one exact old deployment into a new transactional receipt."""
    normalized_home, normalized_appdata, normalized_state = normalize_layout(home, appdata, state_root)
    if preview:
        if not no_delete:
            raise DeployError("Upgrade preview requires explicit no-delete mode")
        old_root, old_receipt = load_receipt(
            normalized_home, normalized_state, transaction_id, allow_package_drift=True
        )
        verify_upgrade_preimage(old_root, old_receipt, normalized_home)
        package = verify_package()
        receipt_python = absolute_lexical(Path(str(old_receipt["python_executable"])))
        requested_python = absolute_lexical(python_executable) if python_executable else receipt_python
        if os.path.normcase(str(requested_python)) != os.path.normcase(str(receipt_python)):
            raise DeployError("Upgrade Python binding differs from the source transaction")
        pinned = pin_python(requested_python)
        if os.path.normcase(str(pinned)) != os.path.normcase(str(receipt_python)):
            raise DeployError("Upgrade verified Python binding differs from the source transaction")
        specs = build_upgrade_specs(
            package, normalized_home, pinned, "0" * 32, old_receipt,
            remote_config=remote_config, enable_wake_stop_gate=enable_wake_stop_gate,
        )
        targets = []
        for spec in specs:
            exact = full_image(Path(spec["path"]))
            targets.append({
                "id": spec["id"], "path": str(spec["path"]),
                "current": spec["before"], "preserved_file_count": len(exact["files"]) if exact["state"] == "tree" else (1 if exact["state"] == "file" else 0),
                "preserved_bytes": sum(row["bytes"] for row in exact["files"]) if exact["state"] == "tree" else exact.get("bytes", 0),
            })
        return {
            "action": "upgrade-preview", "status": "READY", "passed": True,
            "writes_performed": 0, "preserve_mode": "no-delete",
            "previous_transaction_id": transaction_id,
            "package_fingerprint": package.fingerprint,
            "state_root": str(normalized_state), "targets": targets,
            "deployment_mode": "remote" if (remote_config is not None or receipt_remote_config(old_receipt) is not None) else "local",
            "wake_stop_gate_enabled": receipt_wake_stop_gate(old_receipt) or enable_wake_stop_gate,
        }
    with deployment_lock(normalized_state):
        old_root, old_receipt = load_receipt(
            normalized_home, normalized_state, transaction_id, allow_package_drift=True
        )
        verify_upgrade_preimage(old_root, old_receipt, normalized_home)
        package = verify_package()
        receipt_python = absolute_lexical(Path(str(old_receipt["python_executable"])))
        requested_python = absolute_lexical(python_executable) if python_executable else receipt_python
        if os.path.normcase(str(requested_python)) != os.path.normcase(str(receipt_python)):
            raise DeployError("Upgrade Python binding differs from the source transaction")
        pinned = pin_python(requested_python)
        if os.path.normcase(str(pinned)) != os.path.normcase(str(receipt_python)):
            raise DeployError("Upgrade verified Python binding differs from the source transaction")
        new_transaction_id = uuid.uuid4().hex
        specs = build_upgrade_specs(
            package, normalized_home, pinned, new_transaction_id, old_receipt,
            remote_config=remote_config, enable_wake_stop_gate=enable_wake_stop_gate,
        )
        transaction_root, receipt = prepare_transaction(
            specs, package, normalized_home, normalized_state, new_transaction_id, pinned,
            no_delete=no_delete,
        )
        try:
            update_receipt(
                transaction_root,
                receipt,
                status="APPLYING",
                action="upgrade",
                details={"previous_transaction_id": transaction_id},
            )
        except Exception as exc:
            if no_delete:
                raise DeployError(
                    "Upgrade activation did not start; private transaction was preserved"
                ) from exc
            transactions_root = normalized_state / "transactions"
            try:
                remove_private_transaction(transaction_root, transactions_root)
            except Exception as cleanup_exc:
                raise DeployError(
                    "Upgrade activation did not start, but private transaction cleanup failed"
                ) from cleanup_exc
            raise DeployError("Upgrade activation did not start; private transaction was removed") from exc
        try:
            if no_delete:
                transition_all_preserved(receipt, transaction_root, "before", "after", normalized_home)
            else:
                transition_all(receipt, transaction_root, "before", "after", normalized_home)
            validate_config_semantics(receipt, normalized_home)
            update_receipt(transaction_root, receipt, status="DEPLOYED", action="upgrade")
            final = verify_install(
                normalized_home, normalized_appdata, normalized_state, pinned, strict=True
            )
            if final.get("status") != "CURRENT":
                raise DeployError(
                    "Upgrade final verification did not return CURRENT: "
                    f"{final.get('diagnostics', 'unknown verification failure')}"
                )
        except Exception as exc:
            failure = compensate_failed_deployment(
                transaction_root, receipt, normalized_home, "upgrade"
            )
            raise failure from exc
        return receipt


def rollback(
    home: Path,
    appdata: Path | None,
    state_root: Path | None,
    transaction_id: str,
) -> dict[str, Any]:
    normalized_home, _, normalized_state = normalize_layout(home, appdata, state_root)
    with deployment_lock(normalized_state):
        transaction_root, receipt = load_receipt(normalized_home, normalized_state, transaction_id)
        preserved = no_delete_receipt(receipt)
        if preserved:
            normalize_pending_preserved(receipt, transaction_root, normalized_home)
        else:
            cleanup_activation_staging(transaction_root, receipt)
        status = receipt.get("status")
        if status == "ROLLED_BACK":
            assert_all_match(receipt, "before")
            return receipt
        if preserved and status in {"PREPARED", "APPLYING", "FAILED", "AUTO_ROLLED_BACK"}:
            unknown = unknown_transition_targets(receipt)
            if unknown:
                raise DeployError(f"Interrupted preserved upgrade contains unknown live bytes: {','.join(unknown)}")
            drift_ids, errors = compensate_preserved(receipt, transaction_root, "before", "after", normalized_home)
            if drift_ids or errors or not all_match(receipt, "before"):
                raise DeployError("Interrupted preserved upgrade could not be rolled back safely")
            update_receipt(transaction_root, receipt, status="ROLLED_BACK", action="rollback")
            return receipt
        if status == "ROLLING_BACK":
            if all_match(receipt, "before"):
                if not preserved:
                    prune_created_directories(receipt, normalized_home)
                update_receipt(
                    transaction_root,
                    receipt,
                    status="ROLLED_BACK",
                    action="rollback-resume",
                )
                return receipt
            unknown = unknown_transition_targets(receipt)
            if unknown:
                raise DeployError(
                    f"Interrupted rollback contains unknown live bytes: {','.join(unknown)}"
                )
            if preserved:
                drift_ids, errors = compensate_preserved(receipt, transaction_root, "after", "before", normalized_home)
            else:
                drift_ids, errors = compensate_transition(receipt, transaction_root, "after", "before", normalized_home)
            if drift_ids or errors or not all_match(receipt, "after"):
                blocked = sorted(set(drift_ids + errors))
                raise DeployError(
                    f"Interrupted rollback contains unknown live bytes: {','.join(blocked)}"
                )
            update_receipt(
                transaction_root,
                receipt,
                status="DEPLOYED",
                action="rollback-resume-normalize",
            )
            status = "DEPLOYED"
        if status != "DEPLOYED":
            raise DeployError(f"Rollback requires DEPLOYED status, got {status}")
        # Global comparison happens before the first write. Any unrelated live
        # config edit therefore blocks rollback without partially changing code.
        assert_all_match(receipt, "after")
        update_receipt(transaction_root, receipt, status="ROLLING_BACK", action="rollback")
        try:
            if preserved:
                transition_all_preserved(receipt, transaction_root, "after", "before", normalized_home)
            else:
                transition_all(receipt, transaction_root, "after", "before", normalized_home)
            assert_all_match(receipt, "before")
        except TransitionFailure as exc:
            _record_transition_failure(transaction_root, receipt, "rollback", exc, "DEPLOYED")
            raise
        if not preserved:
            prune_created_directories(receipt, normalized_home)
        update_receipt(transaction_root, receipt, status="ROLLED_BACK", action="rollback")
        if preserved:
            _, checked = load_receipt(normalized_home, normalized_state, transaction_id)
            return checked
        return load_json_file(transaction_root / "receipt.json", "Agent Office transaction receipt")


def recover(
    home: Path,
    appdata: Path | None,
    state_root: Path | None,
    transaction_id: str,
    python_executable: Path | None = None,
) -> dict[str, Any]:
    normalized_home, normalized_appdata, normalized_state = normalize_layout(home, appdata, state_root)
    package = verify_package()
    with deployment_lock(normalized_state):
        transaction_root, receipt = load_receipt(normalized_home, normalized_state, transaction_id)
        preserved = no_delete_receipt(receipt)
        if preserved:
            normalize_pending_preserved(receipt, transaction_root, normalized_home)
        else:
            cleanup_activation_staging(transaction_root, receipt)
        if (
            receipt.get("package_fingerprint") != package.fingerprint
            or receipt.get("runtime_fingerprint") != package.runtime_fingerprint
            or receipt.get("skill_fingerprint") != package.skill_fingerprint
        ):
            raise DeployError("Recover source package differs from the transaction source")
        receipt_python = absolute_lexical(Path(str(receipt["python_executable"])))
        requested_python = absolute_lexical(python_executable) if python_executable else receipt_python
        if os.path.normcase(str(requested_python)) != os.path.normcase(str(receipt_python)):
            raise DeployError("Recover Python binding differs from the transaction")
        pinned = pin_python(requested_python)
        if os.path.normcase(str(pinned)) != os.path.normcase(str(receipt_python)):
            raise DeployError("Recover verified Python binding differs from the transaction")
        if receipt.get("status") == "DEPLOYED" and all_match(receipt, "after"):
            current = verify_install(
                normalized_home, normalized_appdata, normalized_state, pinned, strict=True
            )
            if current.get("status") != "CURRENT":
                raise DeployError("Receipt says DEPLOYED but live verification is not current")
            return {**current, "action": "recover"}
        allowed = {
            "PREPARED",
            "APPLYING",
            "DEPLOYED",
            "AUTO_ROLLED_BACK",
            "FAILED",
            "ROLLED_BACK",
            "ROLLING_BACK",
            "RECOVERING",
        }
        if receipt.get("status") not in allowed:
            raise DeployError(f"Recover cannot continue from status {receipt.get('status')}")
        unknown = unknown_transition_targets(receipt)
        if unknown:
            raise DeployError(f"Recover found unknown live bytes in targets: {','.join(unknown)}")
        if preserved:
            drift_ids, errors = compensate_preserved(receipt, transaction_root, "before", "after", normalized_home)
        else:
            drift_ids, errors = compensate_transition(receipt, transaction_root, "before", "after", normalized_home)
        if drift_ids or errors or not all_match(receipt, "before"):
            blocked = sorted(set(drift_ids + errors))
            raise DeployError(f"Recover found unknown live bytes in targets: {','.join(blocked)}")
        update_receipt(transaction_root, receipt, status="RECOVERING", action="recover")
        try:
            if preserved:
                transition_all_preserved(receipt, transaction_root, "before", "after", normalized_home)
            else:
                transition_all(receipt, transaction_root, "before", "after", normalized_home)
            validate_config_semantics(receipt, normalized_home)
            update_receipt(transaction_root, receipt, status="DEPLOYED", action="recover")
            current = verify_install(
                normalized_home, normalized_appdata, normalized_state, pinned, strict=True
            )
            if current.get("status") != "CURRENT":
                raise DeployError("Recover final verification did not return CURRENT")
        except Exception as exc:
            failure = compensate_failed_deployment(
                transaction_root, receipt, normalized_home, "recover"
            )
            raise failure from exc
        return receipt


def cli_paths(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    if args.home is None:
        home = Path.home()
        appdata = args.appdata or Path(os.environ.get("APPDATA", str(home / "AppData" / "Roaming")))
    else:
        home = args.home
        appdata = args.appdata or (home / "AppData" / "Roaming")
    state = args.state_root or (home / DEFAULT_STATE_RELATIVE)
    return normalize_layout(home, appdata, state)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="install-agent-office",
        description="Install the source-locked local Agent Office transactionally.",
    )
    parser.add_argument("--home", type=Path, help="Synthetic/explicit user home; default is the current user home.")
    parser.add_argument("--appdata", type=Path, help="Explicit APPDATA paired with --home.")
    parser.add_argument("--state-root", type=Path, help="Private receipt root under the selected home.")
    parser.add_argument("--python-executable", type=Path, help="Absolute Python 3.11+ executable to verify and pin.")
    parser.add_argument("--remote-config", type=Path,
                        help="Explicit bounded connection JSON to enroll CLI and native MCP in one remote Office.")
    parser.add_argument("--dry-run", action="store_true", help="Alias for the dry-run command; performs no writes.")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("seal", help="Refresh package.json digests from the sources. Touches nothing deployed.")
    commands.add_parser(
        "skill-pin",
        help="Report whether the shared-skills ownership pin still matches SKILL.md. Read-only.",
    )
    commands.add_parser("dry-run", help="Verify sources and preflight every target without writes.")
    commands.add_parser("install", help="Install runtime, skill, timeouts, and native registrations.")
    commands.add_parser("verify", help="Verify live bytes, source hashes, markers, receipt, and registrations.")
    rollback_parser = commands.add_parser("rollback", help="Restore exact transaction preimages.")
    rollback_parser.add_argument("--transaction-id", required=True)
    upgrade_parser = commands.add_parser("upgrade", help="Upgrade one deployed transaction from its exact old receipt.")
    upgrade_parser.add_argument("--transaction-id", required=True)
    upgrade_parser.add_argument("--no-delete", action="store_true", help="Preserve every displaced live target in private transaction storage.")
    upgrade_parser.add_argument("--preview", action="store_true", help="Read-only preflight for --no-delete upgrade.")
    upgrade_parser.add_argument("--enable-wake-stop-gate", action="store_true",
                                help="Explicit opt-in: transactionally add the pinned Claude UserPromptSubmit STOP hook.")
    recover_parser = commands.add_parser("recover", help="Re-verify and restore a transaction deployment.")
    recover_parser.add_argument("--transaction-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.dry_run and args.command is not None:
        parser.error("--dry-run cannot be combined with a command")
    command = "dry-run" if args.dry_run else args.command
    if command is None:
        parser.error(
            "choose --dry-run or one of: seal, skill-pin, dry-run, install, verify, "
            "upgrade, rollback, recover"
        )
    try:
        remote_config = None
        if args.remote_config is not None:
            if command not in {"dry-run", "install", "upgrade"}:
                raise DeployError("--remote-config is only valid for dry-run, install or upgrade")
            if not args.remote_config.is_absolute():
                raise DeployError("Remote connection configuration must have an absolute path")
            assert_plain_chain(args.remote_config, "remote connection configuration")
            try:
                remote_config = asdict(load_remote_config(args.remote_config))
            except CoordError:
                raise DeployError("Invalid remote Office connection configuration") from None
        # Sealing and the skill-pin report are source-side only: they must not
        # resolve or touch a deployment home, so they run before any layout is
        # computed. Both are read-only toward the ownership records.
        if command in {"seal", "skill-pin"}:
            result = seal_package() if command == "seal" else skill_pin_report()
            sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            return 0 if result.get("status") in {"READY", "CURRENT", "UNKNOWN"} else 1
        home, appdata, state_root = cli_paths(args)
        if command == "dry-run":
            result = dry_run(home, appdata, state_root, args.python_executable, remote_config=remote_config)
        elif command == "install":
            result = install(home, appdata, state_root, args.python_executable, remote_config=remote_config)
        elif command == "verify":
            # The standalone verify command is read-only and is the only surface
            # that reports unrelated foreign config fields without failing.
            result = verify_install(
                home, appdata, state_root, args.python_executable, strict=False
            )
        elif command == "rollback":
            result = rollback(home, appdata, state_root, args.transaction_id)
        elif command == "upgrade":
            result = upgrade(
                home, appdata, state_root, args.transaction_id, args.python_executable,
                no_delete=args.no_delete, preview=args.preview,
                remote_config=remote_config,
                enable_wake_stop_gate=args.enable_wake_stop_gate,
            )
        elif command == "recover":
            result = recover(home, appdata, state_root, args.transaction_id, args.python_executable)
        else:
            raise DeployError(f"Unknown command: {command}")
    except DeployError as exc:
        result = {"action": command, "status": "ERROR", "passed": False, "message": str(exc)}
    except (OSError, UnicodeError, ValueError) as exc:
        result = {
            "action": command,
            "status": "ERROR",
            "passed": False,
            "message": f"Installer operation failed ({exc.__class__.__name__})",
        }
    except Exception as exc:
        result = {
            "action": command,
            "status": "ERROR",
            "passed": False,
            "message": f"Installer failed closed ({exc.__class__.__name__})",
        }
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return 0 if result.get("status") in {"READY", "CURRENT", "DEPLOYED", "ROLLED_BACK"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
