from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import uuid
from pathlib import Path
from typing import Any, Iterator


PACKAGE_ID = "agent-fleet-suite"
PACKAGE_LOCK_NAME = "package-lock.json"
RUNTIME_MARKER = ".agent-fleet-suite-runtime.json"
ADAPTER_MARKER = ".agent-fleet-suite-adapter.json"
LEGACY_MARKER = ".router-v2-managed.json"
SKILLS = ("agent-fleet", "model-radar", "agent-cli", "agent-loop")
EXPECTED_RUNTIME_PATTERN = re.compile(
    r'\$expectedRuntime\s*=\s*"([0-9a-f]{64})"'
)
EXPECTED_PACKAGE_VERSION_PATTERN = re.compile(
    r"--package-version\s+([0-9]+\.[0-9]+\.[0-9]+)"
)
LOCAL_TOOLS = ("shared", "claude", "antigravity", "grok")
TRANSACTION_ID_PATTERN = re.compile(r"[a-f0-9]{32}")
KIMI_PLUGIN_RELATIVE = Path("platform") / "kimi-desktop" / PACKAGE_ID
IGNORED_DIRS = {"__pycache__", ".ruff_cache", ".pytest_cache"}
IGNORED_SUFFIXES = {".pyc", ".pyo"}
EXECUTABLE_SOURCE_SUFFIXES = {
    ".bat",
    ".cmd",
    ".com",
    ".cjs",
    ".exe",
    ".js",
    ".mjs",
    ".ps1",
    ".py",
    ".sh",
}
TERMINAL_STATES = {
    "DEPLOYED",
    "ROLLED_BACK",
    "AUTO_ROLLED_BACK",
    "RECOVERED_ROLLBACK",
}


class SuiteError(RuntimeError):
    pass


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2) + "\n"
    ).encode("ascii")


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("xb") as handle:
        handle.write(canonical_json(value))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="ascii"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SuiteError(f"Cannot read JSON metadata {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SuiteError(f"JSON metadata must be an object: {path}")
    return value


def is_filesystem_link(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise SuiteError(f"Cannot inspect path {path}: {exc}") from exc
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction) and is_junction():
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def ensure_plain_directory(path: Path, *, create: bool = False) -> None:
    if create and not path.exists():
        path.mkdir(parents=True)
    if not path.is_dir() or is_filesystem_link(path):
        raise SuiteError(f"Expected a plain directory: {path}")


def tree_files(root: Path, *, marker_names: set[str] | None = None) -> list[Path]:
    ensure_plain_directory(root)
    ignored_markers = marker_names or set()
    files: list[Path] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        relative = path.relative_to(root)
        if ".pytest_cache" in relative.parts:
            if is_filesystem_link(path):
                raise SuiteError(f"Linked or reparse payload path is refused: {path}")
            if (
                path.is_file()
                and path.suffix.casefold() in EXECUTABLE_SOURCE_SUFFIXES
            ):
                raise SuiteError(
                    "Executable source is present in an ignored cache directory"
                )
            continue
        cache_parts = set(relative.parts) & IGNORED_DIRS
        if cache_parts:
            if (
                path.is_file()
                and "__pycache__" in cache_parts
                and path.suffix.casefold() not in IGNORED_SUFFIXES
            ):
                raise SuiteError(
                    "Unsealed source is present in an ignored bytecode directory"
                )
            if (
                path.is_file()
                and (".ruff_cache" in cache_parts or ".pytest_cache" in cache_parts)
                and path.suffix.casefold() in EXECUTABLE_SOURCE_SUFFIXES
            ):
                raise SuiteError(
                    "Executable source is present in an ignored cache directory"
                )
            continue
        if path.name in ignored_markers or path.suffix.casefold() in IGNORED_SUFFIXES:
            continue
        if is_filesystem_link(path):
            raise SuiteError(f"Linked or reparse payload path is refused: {path}")
        if path.is_file():
            files.append(path)
    return files


def tree_manifest(
    root: Path, *, marker_names: set[str] | None = None
) -> tuple[list[dict[str, Any]], str]:
    rows: list[dict[str, Any]] = []
    for path in tree_files(root, marker_names=marker_names):
        payload = path.read_bytes()
        rows.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    fingerprint = hashlib.sha256(canonical_json(rows)).hexdigest()
    return rows, fingerprint


def copy_tree(source: Path, target: Path) -> None:
    if target.exists():
        raise SuiteError(f"Staging target already exists: {target}")
    target.mkdir(parents=True)
    for source_file in tree_files(
        source, marker_names={RUNTIME_MARKER, ADAPTER_MARKER, LEGACY_MARKER}
    ):
        relative = source_file.relative_to(source)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, destination)


def full_tree_manifest(root: Path) -> tuple[list[dict[str, Any]], str]:
    ensure_plain_directory(root)
    rows: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if is_filesystem_link(path):
            raise SuiteError(f"Linked or reparse path is refused: {path}")
        if path.is_file():
            payload = path.read_bytes()
            rows.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "size": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            )
    fingerprint = hashlib.sha256(canonical_json(rows)).hexdigest()
    return rows, fingerprint


def copy_tree_exact(source: Path, target: Path) -> str:
    if target.exists():
        raise SuiteError(f"Exact-copy target already exists: {target}")
    full_tree_manifest(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, copy_function=shutil.copy2)
    _, source_fingerprint = full_tree_manifest(source)
    _, target_fingerprint = full_tree_manifest(target)
    if target_fingerprint != source_fingerprint:
        raise SuiteError(f"Exact-copy verification failed: {target}")
    return target_fingerprint


def mirror_tree_in_place(source: Path, target: Path) -> str:
    """Mirror a plain tree while preserving the target directory object."""
    ensure_plain_directory(source)
    ensure_plain_directory(target, create=True)

    source_dirs: dict[str, Path] = {}
    source_files: dict[str, Path] = {}
    for path in sorted(source.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if is_filesystem_link(path):
            raise SuiteError(f"Linked or reparse source path is refused: {path}")
        relative = path.relative_to(source)
        key = relative.as_posix().casefold()
        if path.is_dir():
            source_dirs[key] = relative
        elif path.is_file():
            source_files[key] = relative

    target_dirs: dict[str, Path] = {}
    target_files: dict[str, Path] = {}
    for path in sorted(target.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if is_filesystem_link(path):
            raise SuiteError(f"Linked or reparse target path is refused: {path}")
        relative = path.relative_to(target)
        key = relative.as_posix().casefold()
        if path.is_dir():
            target_dirs[key] = relative
        elif path.is_file():
            target_files[key] = relative

    for key, relative in target_files.items():
        if key not in source_files:
            (target / relative).unlink()
    for key, relative in sorted(
        target_dirs.items(), key=lambda item: len(item[1].parts), reverse=True
    ):
        if key not in source_dirs:
            (target / relative).rmdir()

    for relative in sorted(source_dirs.values(), key=lambda item: len(item.parts)):
        (target / relative).mkdir(parents=True, exist_ok=True)

    marker_names = {RUNTIME_MARKER, ADAPTER_MARKER, LEGACY_MARKER}
    ordered_files = sorted(
        source_files.values(),
        key=lambda item: (item.name in marker_names, item.as_posix().casefold()),
    )
    for relative in ordered_files:
        source_file = source / relative
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            shutil.copy2(source_file, temporary)
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()

    _, fingerprint = full_tree_manifest(target)
    return fingerprint


def strict_package_manifest(package_root: Path) -> dict[str, Any]:
    manifest = load_json(package_root / "package.json")
    expected_keys = {
        "schema_version",
        "package_id",
        "package_version",
        "kind",
        "release_state",
        "platforms",
        "runtime_target",
        "components",
        "adapter_ids",
        "declared_effects",
        "prohibited_effects",
    }
    if set(manifest) != expected_keys:
        raise SuiteError("package.json has an unexpected key set")
    if (
        manifest["schema_version"] != 1
        or manifest["package_id"] != PACKAGE_ID
        or manifest["kind"] != "executable-agent-suite"
        or manifest["release_state"] not in {"candidate", "released"}
        or manifest["platforms"] != ["windows"]
        or manifest["runtime_target"]
        != "~/.agents/runtime/agent-fleet-suite/lean-0.9.142"
        or manifest["adapter_ids"] != list(SKILLS)
    ):
        raise SuiteError("package.json identity or platform contract is invalid")
    version = manifest["package_version"]
    if (
        not isinstance(version, str)
        or len(version.split(".")) != 3
        or not all(part.isdigit() for part in version.split("."))
    ):
        raise SuiteError("package_version must be numeric semver")
    components = manifest["components"]
    if not isinstance(components, list) or len(components) != len(SKILLS):
        raise SuiteError("package components are incomplete")
    component_ids = [item.get("component_id") for item in components if isinstance(item, dict)]
    if component_ids != list(SKILLS):
        raise SuiteError("package component order or identity is invalid")
    for key in ("declared_effects", "prohibited_effects"):
        values = manifest[key]
        if (
            not isinstance(values, list)
            or not values
            or any(not isinstance(value, str) or not value for value in values)
        ):
            raise SuiteError(f"{key} must be a non-empty string list")
    return manifest


def build_package_lock(package_root: Path) -> dict[str, Any]:
    manifest = strict_package_manifest(package_root)
    distribution_rows, distribution_fingerprint = tree_manifest(
        package_root,
        marker_names={
            PACKAGE_LOCK_NAME,
            RUNTIME_MARKER,
            ADAPTER_MARKER,
            LEGACY_MARKER,
        },
    )
    payload_root = package_root / "payload"
    adapters_root = package_root / "adapters"
    runtime_rows, runtime_fingerprint = tree_manifest(
        payload_root,
        marker_names={RUNTIME_MARKER, ADAPTER_MARKER, LEGACY_MARKER},
    )
    if not runtime_rows:
        raise SuiteError("Runtime payload is empty")
    adapters: dict[str, Any] = {}
    for skill in SKILLS:
        adapter_root = adapters_root / skill
        rows, fingerprint = tree_manifest(
            adapter_root,
            marker_names={RUNTIME_MARKER, ADAPTER_MARKER, LEGACY_MARKER},
        )
        if [row["path"] for row in rows] != ["SKILL.md"]:
            raise SuiteError(f"Adapter {skill} must contain only SKILL.md")
        adapter_text = (adapter_root / "SKILL.md").read_text(encoding="utf-8")
        embedded_fingerprints = EXPECTED_RUNTIME_PATTERN.findall(adapter_text)
        if embedded_fingerprints != [runtime_fingerprint]:
            raise SuiteError(
                f"Adapter {skill} runtime fingerprint does not match payload"
            )
        embedded_versions = EXPECTED_PACKAGE_VERSION_PATTERN.findall(adapter_text)
        if embedded_versions != [manifest["package_version"]]:
            raise SuiteError(
                f"Adapter {skill} package version does not match release"
            )
        adapters[skill] = {
            "file_count": len(rows),
            "fingerprint": fingerprint,
            "files": rows,
        }
    kimi_plugin_root = package_root / KIMI_PLUGIN_RELATIVE
    kimi_manifest = load_json(kimi_plugin_root / "kimi.plugin.json")
    if (
        kimi_manifest.get("name") != PACKAGE_ID
        or kimi_manifest.get("version") != manifest["package_version"]
        or kimi_manifest.get("skills") != "./skills/"
    ):
        raise SuiteError("Kimi Desktop plugin identity is invalid")
    kimi_rows, kimi_fingerprint = tree_manifest(kimi_plugin_root)
    expected_kimi_paths = sorted(
        ["kimi.plugin.json"]
        + [f"skills/{skill}/SKILL.md" for skill in SKILLS],
        key=str.casefold,
    )
    if [row["path"] for row in kimi_rows] != expected_kimi_paths:
        raise SuiteError("Kimi Desktop plugin has an unexpected file set")
    for skill in SKILLS:
        rows, fingerprint = tree_manifest(kimi_plugin_root / "skills" / skill)
        if rows != adapters[skill]["files"] or fingerprint != adapters[skill]["fingerprint"]:
            raise SuiteError(f"Kimi Desktop adapter differs from canonical: {skill}")
    manifest_sha256 = hashlib.sha256(
        (package_root / "package.json").read_bytes()
    ).hexdigest()
    identity = {
        "distribution_fingerprint": distribution_fingerprint,
    }
    return {
        "schema_version": 1,
        "package_id": PACKAGE_ID,
        "package_version": manifest["package_version"],
        "runtime_release_version": manifest["components"][0]["component_version"],
        "sealed_at": utc_now(),
        "manifest_sha256": manifest_sha256,
        "distribution": {
            "file_count": len(distribution_rows),
            "fingerprint": distribution_fingerprint,
            "files": distribution_rows,
        },
        "runtime": {
            "file_count": len(runtime_rows),
            "fingerprint": runtime_fingerprint,
            "files": runtime_rows,
        },
        "adapters": adapters,
        "kimi_desktop_plugin": {
            "file_count": len(kimi_rows),
            "fingerprint": kimi_fingerprint,
            "files": kimi_rows,
            "version": kimi_manifest["version"],
        },
        "package_fingerprint": hashlib.sha256(canonical_json(identity)).hexdigest(),
    }


def seal_package(package_root: Path) -> dict[str, Any]:
    lock = build_package_lock(package_root)
    atomic_json(package_root / PACKAGE_LOCK_NAME, lock)
    return {
        "action": "seal",
        "status": "SEALED",
        "package_fingerprint": lock["package_fingerprint"],
        "package_version": lock["package_version"],
        "runtime_file_count": lock["runtime"]["file_count"],
        "kimi_plugin_file_count": lock["kimi_desktop_plugin"]["file_count"],
        "writes_performed": 1,
    }


def load_verified_package(package_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = strict_package_manifest(package_root)
    lock = load_json(package_root / PACKAGE_LOCK_NAME)
    rebuilt = build_package_lock(package_root)
    for key in (
        "schema_version",
        "package_id",
        "package_version",
        "runtime_release_version",
        "manifest_sha256",
        "distribution",
        "runtime",
        "adapters",
        "kimi_desktop_plugin",
        "package_fingerprint",
    ):
        if lock.get(key) != rebuilt.get(key):
            raise SuiteError(f"Package lock mismatch: {key}")
    return manifest, lock


def default_runtime_target(home: Path) -> Path:
    return home / ".agents" / "runtime" / PACKAGE_ID / "lean-0.9.142"


def default_state_root(home: Path) -> Path:
    return home / ".agents" / "router-suite-deploy"


def target_roots(home: Path, appdata: Path) -> dict[str, Path]:
    del appdata
    return {
        "shared": home / ".agents" / "skills",
        "claude": home / ".claude" / "skills",
        "antigravity": home / ".gemini" / "config" / "skills",
        "grok": home / ".grok" / "skills",
    }


def parse_tools(value: str) -> list[str]:
    if value == "runtime-only":
        return []
    tools = [item.strip() for item in value.split(",") if item.strip()]
    if not tools or len(set(tools)) != len(tools):
        raise SuiteError("Tool list is empty or contains duplicates")
    unknown = sorted(set(tools) - set(LOCAL_TOOLS))
    if unknown:
        raise SuiteError(f"Unknown local tools: {', '.join(unknown)}")
    return tools


def marker_manager(path: Path) -> str:
    runtime_marker = path / RUNTIME_MARKER
    if runtime_marker.is_file():
        marker = load_json(runtime_marker)
        return str(marker.get("managed_by") or "runtime-marker-invalid")
    suite_marker = path / ADAPTER_MARKER
    if suite_marker.is_file():
        marker = load_json(suite_marker)
        return str(marker.get("managed_by") or "suite-marker-invalid")
    legacy_marker = path / LEGACY_MARKER
    if legacy_marker.is_file():
        marker = load_json(legacy_marker)
        return str(marker.get("managed_by") or "legacy-marker-invalid")
    return "unmanaged"


def current_tree_fingerprint(path: Path, *, marker: str) -> str | None:
    if not path.exists():
        return None
    _, fingerprint = tree_manifest(path, marker_names={marker, LEGACY_MARKER})
    return fingerprint


def runtime_marker_value(
    *, transaction_id: str, lock: dict[str, Any]
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "managed_by": "agent-fleet-suite-v2-installer",
        "package_id": PACKAGE_ID,
        "package_version": lock["runtime_release_version"],
        "distribution_package_version": lock["package_version"],
        "package_fingerprint": lock["package_fingerprint"],
        "payload_fingerprint": lock["runtime"]["fingerprint"],
        "payload_file_count": lock["runtime"]["file_count"],
        "transaction_id": transaction_id,
        "installed_at": utc_now(),
    }


def adapter_marker_value(
    *,
    transaction_id: str,
    lock: dict[str, Any],
    skill: str,
    tool: str,
) -> dict[str, Any]:
    adapter = lock["adapters"][skill]
    return {
        "schema_version": 1,
        "managed_by": "agent-fleet-suite-v2-installer",
        "package_id": PACKAGE_ID,
        "package_version": lock["package_version"],
        "package_fingerprint": lock["package_fingerprint"],
        "adapter_id": skill,
        "adapter_fingerprint": adapter["fingerprint"],
        "adapter_file_count": adapter["file_count"],
        "tool": tool,
        "transaction_id": transaction_id,
        "installed_at": utc_now(),
    }


def expected_marker_identity(
    entry: dict[str, Any], lock: dict[str, Any]
) -> dict[str, Any]:
    if entry["kind"] == "replace-runtime":
        return {
            "schema_version": 1,
            "managed_by": "agent-fleet-suite-v2-installer",
            "package_id": PACKAGE_ID,
            "package_version": lock["runtime_release_version"],
            "distribution_package_version": lock["package_version"],
            "package_fingerprint": lock["package_fingerprint"],
            "payload_fingerprint": lock["runtime"]["fingerprint"],
            "payload_file_count": lock["runtime"]["file_count"],
        }
    adapter = lock["adapters"][entry["skill"]]
    return {
        "schema_version": 1,
        "managed_by": "agent-fleet-suite-v2-installer",
        "package_id": PACKAGE_ID,
        "package_version": lock["package_version"],
        "package_fingerprint": lock["package_fingerprint"],
        "adapter_id": entry["skill"],
        "adapter_fingerprint": adapter["fingerprint"],
        "adapter_file_count": adapter["file_count"],
        "tool": entry["tool"],
    }


def marker_semantics_valid(
    target: Path, entry: dict[str, Any], lock: dict[str, Any]
) -> bool:
    marker_path = target / entry["marker_name"]
    if not marker_path.is_file():
        return False
    marker = load_json(marker_path)
    expected = expected_marker_identity(entry, lock)
    return all(marker.get(key) == value for key, value in expected.items()) and bool(
        marker.get("transaction_id")
    )


def planned_entries(
    *,
    package_root: Path,
    home: Path,
    appdata: Path,
    tools: list[str],
    lock: dict[str, Any],
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = [
        {
            "entry_id": "runtime",
            "kind": "replace-runtime",
            "source": str((package_root / "payload").resolve()),
            "target": str(default_runtime_target(home).resolve()),
            "expected_fingerprint": lock["runtime"]["fingerprint"],
            "marker_name": RUNTIME_MARKER,
        }
    ]
    roots = target_roots(home, appdata)
    for tool in tools:
        root = roots[tool]
        for skill in SKILLS:
            entries.append(
                {
                    "entry_id": f"adapter:{tool}:{skill}",
                    "kind": "replace-adapter",
                    "tool": tool,
                    "skill": skill,
                    "source": str((package_root / "adapters" / skill).resolve()),
                    "target": str((root / skill).resolve()),
                    "expected_fingerprint": lock["adapters"][skill]["fingerprint"],
                    "marker_name": ADAPTER_MARKER,
                }
            )
    if "shared" in tools:
        for skill in SKILLS:
            entries.append(
                {
                    "entry_id": f"retire:codex:{skill}",
                    "kind": "retire-legacy",
                    "tool": "codex",
                    "skill": skill,
                    "source": None,
                    "target": str((home / ".codex" / "skills" / skill).resolve()),
                    "expected_fingerprint": None,
                    "marker_name": LEGACY_MARKER,
                }
            )
    return entries


def inspect_plan(
    *,
    package_root: Path,
    home: Path,
    appdata: Path,
    tools: list[str],
    adopt_existing: bool,
    expected_package_fingerprint: str | None,
) -> dict[str, Any]:
    manifest, lock = load_verified_package(package_root)
    if (
        expected_package_fingerprint
        and lock["package_fingerprint"] != expected_package_fingerprint
    ):
        raise SuiteError("Expected package fingerprint does not match the sealed package")
    entries = planned_entries(
        package_root=package_root,
        home=home,
        appdata=appdata,
        tools=tools,
        lock=lock,
    )
    rows: list[dict[str, Any]] = []
    blockers: list[str] = []
    for entry in entries:
        target = Path(entry["target"])
        semantics_valid: bool | None = None
        if target.exists() and (not target.is_dir() or is_filesystem_link(target)):
            blockers.append(f"non-plain-target:{entry['entry_id']}")
            manager = "invalid"
            fingerprint = None
        else:
            manager = marker_manager(target) if target.exists() else "absent"
            fingerprint = (
                current_tree_fingerprint(target, marker=entry["marker_name"])
                if target.exists()
                else None
            )
            if entry["kind"] != "retire-legacy" and target.exists():
                semantics_valid = marker_semantics_valid(target, entry, lock)
        adoption_required = target.exists() and (
            (
                entry["kind"] in {"replace-adapter", "replace-runtime"}
                and manager != "agent-fleet-suite-v2-installer"
            )
            or (
                entry["kind"] == "retire-legacy"
                and manager
                not in {
                    "agent-fleet-suite-v2-installer",
                    "router-v2-transactional-deployer",
                }
            )
        )
        if adoption_required and not adopt_existing:
            blockers.append(f"adoption-required:{entry['entry_id']}")
        rows.append(
            {
                **entry,
                "exists": target.exists(),
                "manager": manager,
                "current_fingerprint": fingerprint,
                "marker_semantics_valid": semantics_valid,
                "adoption_required": adoption_required,
                "change_required": (
                    entry["kind"] == "retire-legacy"
                    and target.exists()
                )
                or (
                    entry["kind"] != "retire-legacy"
                    and (
                        fingerprint != entry["expected_fingerprint"]
                        or adoption_required
                        or semantics_valid is not True
                    )
                ),
            }
        )
    return {
        "action": "plan",
        "status": "BLOCKED" if blockers else "READY",
        "package_id": PACKAGE_ID,
        "package_version": lock["package_version"],
        "release_state": manifest["release_state"],
        "package_fingerprint": lock["package_fingerprint"],
        "tools": tools,
        "adopt_existing": adopt_existing,
        "blockers": blockers,
        "entries": rows,
        "writes_performed": 0,
    }


@contextlib.contextmanager
def operation_lock(state_root: Path) -> Iterator[None]:
    state_root.mkdir(parents=True, exist_ok=True)
    lock_path = state_root / "operation.lock"
    handle = lock_path.open("a+b")
    try:
        handle.seek(0)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise SuiteError("Another router-suite operation holds the lock") from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise SuiteError("Another router-suite operation holds the lock") from exc
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def verify_staged(
    path: Path,
    *,
    expected_fingerprint: str,
    marker_name: str,
    transaction_id: str,
    expected_marker: dict[str, Any],
) -> None:
    _, fingerprint = tree_manifest(path, marker_names={marker_name, LEGACY_MARKER})
    if fingerprint != expected_fingerprint:
        raise SuiteError(f"Byte verification failed: {path}")
    marker = load_json(path / marker_name)
    if marker.get("transaction_id") != transaction_id:
        raise SuiteError(f"Transaction marker mismatch: {path}")
    mismatches = {
        key: (value, marker.get(key))
        for key, value in expected_marker.items()
        if marker.get(key) != value
    }
    if mismatches:
        raise SuiteError(f"Marker semantic mismatch: {path}: {mismatches}")


def prepare_transaction(
    *,
    package_root: Path,
    home: Path,
    appdata: Path,
    tools: list[str],
    state_root: Path,
    adopt_existing: bool,
    expected_package_fingerprint: str,
) -> tuple[Path | None, dict[str, Any]]:
    plan = inspect_plan(
        package_root=package_root,
        home=home,
        appdata=appdata,
        tools=tools,
        adopt_existing=adopt_existing,
        expected_package_fingerprint=expected_package_fingerprint,
    )
    if plan["status"] != "READY":
        raise SuiteError("Deployment plan is blocked: " + ", ".join(plan["blockers"]))
    if plan["release_state"] != "released":
        raise SuiteError("Only a released package may be deployed")
    changed_entries = [
        entry for entry in plan["entries"] if entry["change_required"]
    ]
    if not changed_entries:
        return None, {
            "status": "CURRENT",
            "package_fingerprint": plan["package_fingerprint"],
            "entries": [],
        }
    transaction_id = uuid.uuid4().hex
    lock = load_json(package_root / PACKAGE_LOCK_NAME)
    transaction_root = state_root / "transactions" / transaction_id
    staging_root = transaction_root / "staging"
    transaction_root.mkdir(parents=True, exist_ok=False)
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "transaction_id": transaction_id,
        "package_id": PACKAGE_ID,
        "package_version": plan["package_version"],
        "package_fingerprint": plan["package_fingerprint"],
        "created_at": utc_now(),
        "status": "PREPARING",
        "home": str(home.resolve()),
        "appdata": str(appdata.resolve()),
        "tools": tools,
        "entries": [],
    }
    atomic_json(transaction_root / "receipt.json", receipt)
    for index, plan_entry in enumerate(changed_entries):
        entry = {
            **plan_entry,
            "index": index,
            "phase": "PLANNED",
            "backup": str((transaction_root / "backups" / f"{index:03d}").resolve()),
            "quarantine": str(
                (transaction_root / "quarantine" / f"{index:03d}").resolve()
            ),
            "stage": None,
        }
        if entry["kind"] != "retire-legacy":
            stage = staging_root / f"{index:03d}"
            copy_tree(Path(entry["source"]), stage)
            if entry["kind"] == "replace-runtime":
                marker = runtime_marker_value(
                    transaction_id=transaction_id,
                    lock=lock,
                )
            else:
                marker = adapter_marker_value(
                    transaction_id=transaction_id,
                    lock=lock,
                    skill=entry["skill"],
                    tool=entry["tool"],
                )
            atomic_json(stage / entry["marker_name"], marker)
            entry["expected_marker"] = marker
            verify_staged(
                stage,
                expected_fingerprint=entry["expected_fingerprint"],
                marker_name=entry["marker_name"],
                transaction_id=transaction_id,
                expected_marker=marker,
            )
            entry["stage"] = str(stage.resolve())
        entry["phase"] = "PREPARED"
        receipt["entries"].append(entry)
        atomic_json(transaction_root / "receipt.json", receipt)
    receipt["status"] = "PREPARED"
    atomic_json(transaction_root / "receipt.json", receipt)
    return transaction_root, receipt


def resolved_path_equal(value: Any, expected: Path) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        return os.path.normcase(str(Path(value).expanduser().resolve())) == os.path.normcase(
            str(expected.expanduser().resolve())
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return False


def validate_receipt_context(
    receipt_path: Path,
    *,
    state_root: Path,
    package_root: Path,
    home: Path,
    appdata: Path,
) -> dict[str, Any]:
    """Bind every mutating receipt path to trusted CLI inputs and the sealed package."""
    receipt = load_json(receipt_path)
    transaction_id = receipt.get("transaction_id")
    if not isinstance(transaction_id, str) or not TRANSACTION_ID_PATTERN.fullmatch(
        transaction_id
    ):
        raise SuiteError("Receipt transaction_id is invalid")
    state_root = state_root.expanduser().resolve()
    transaction_root = (state_root / "transactions" / transaction_id).resolve()
    expected_receipt = transaction_root / "receipt.json"
    if receipt_path.expanduser().resolve() != expected_receipt:
        raise SuiteError("Receipt path is outside its canonical transaction directory")
    for candidate in (transaction_root, expected_receipt):
        if candidate.exists() and is_filesystem_link(candidate):
            raise SuiteError(f"Linked transaction metadata is refused: {candidate}")

    _, lock = load_verified_package(package_root)
    if (
        receipt.get("schema_version") != 1
        or receipt.get("package_id") != PACKAGE_ID
        or receipt.get("package_version") != lock["package_version"]
        or receipt.get("package_fingerprint") != lock["package_fingerprint"]
        or not resolved_path_equal(receipt.get("home"), home)
        or not resolved_path_equal(receipt.get("appdata"), appdata)
    ):
        raise SuiteError("Receipt deployment context does not match trusted inputs")
    tools = receipt.get("tools")
    if (
        not isinstance(tools, list)
        or any(not isinstance(tool, str) for tool in tools)
        or len(tools) != len(set(tools))
        or any(tool not in LOCAL_TOOLS for tool in tools)
    ):
        raise SuiteError("Receipt tool set is invalid")
    expected_entries = {
        entry["entry_id"]: entry
        for entry in planned_entries(
            package_root=package_root,
            home=home,
            appdata=appdata,
            tools=tools,
            lock=lock,
        )
    }
    entries = receipt.get("entries")
    if not isinstance(entries, list) or len(entries) > len(expected_entries):
        raise SuiteError("Receipt entries are invalid")
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or entry.get("index") != index:
            raise SuiteError("Receipt entry order is invalid")
        entry_id = entry.get("entry_id")
        if not isinstance(entry_id, str) or entry_id in seen or entry_id not in expected_entries:
            raise SuiteError("Receipt contains an unknown or duplicate entry")
        seen.add(entry_id)
        expected = expected_entries[entry_id]
        for key in ("kind", "tool", "skill", "expected_fingerprint", "marker_name"):
            if entry.get(key) != expected.get(key):
                raise SuiteError(f"Receipt entry {entry_id} has untrusted {key}")
        if entry.get("source") != expected.get("source"):
            raise SuiteError(f"Receipt entry {entry_id} has an untrusted source")
        if not resolved_path_equal(entry.get("target"), Path(expected["target"])):
            raise SuiteError(f"Receipt entry {entry_id} has an untrusted target")
        backup = transaction_root / "backups" / f"{index:03d}"
        quarantine = transaction_root / "quarantine" / f"{index:03d}"
        if not resolved_path_equal(entry.get("backup"), backup) or not resolved_path_equal(
            entry.get("quarantine"), quarantine
        ):
            raise SuiteError(f"Receipt entry {entry_id} has untrusted recovery paths")
        expected_stage = (
            None
            if expected["kind"] == "retire-legacy"
            else transaction_root / "staging" / f"{index:03d}"
        )
        if expected_stage is None:
            if entry.get("stage") is not None:
                raise SuiteError(f"Receipt entry {entry_id} has an untrusted stage")
        elif not resolved_path_equal(entry.get("stage"), expected_stage):
            raise SuiteError(f"Receipt entry {entry_id} has an untrusted stage")
        if expected["kind"] != "retire-legacy":
            marker = entry.get("expected_marker")
            marker_identity = expected_marker_identity(expected, lock)
            if (
                not isinstance(marker, dict)
                or set(marker) != set(marker_identity) | {"transaction_id", "installed_at"}
                or marker.get("transaction_id") != transaction_id
                or not isinstance(marker.get("installed_at"), str)
                or any(marker.get(key) != value for key, value in marker_identity.items())
            ):
                raise SuiteError(f"Receipt entry {entry_id} has an untrusted marker")
    return receipt


def restore_entry(entry: dict[str, Any], transaction_id: str) -> None:
    target = Path(entry["target"])
    backup = Path(entry["backup"])
    quarantine = Path(entry["quarantine"])
    kind = entry["kind"]
    if entry.get("mutation_mode") == "in-place-copy":
        if not entry.get("in_place_activation_started"):
            return
        if not entry.get("in_place_backup_complete") or not backup.is_dir():
            raise SuiteError(f"Complete in-place backup is missing: {backup}")
        if target.exists():
            quarantine.parent.mkdir(parents=True, exist_ok=True)
            if quarantine.exists():
                raise SuiteError(f"Rollback quarantine already exists: {quarantine}")
            copy_tree_exact(target, quarantine)
        restored_fingerprint = mirror_tree_in_place(backup, target)
        if restored_fingerprint != entry.get("backup_fingerprint"):
            raise SuiteError(f"In-place rollback verification failed: {target}")
        return
    if kind != "retire-legacy" and target.exists():
        marker_path = target / entry["marker_name"]
        if marker_path.is_file():
            marker = load_json(marker_path)
            if marker.get("transaction_id") == transaction_id:
                quarantine.parent.mkdir(parents=True, exist_ok=True)
                if quarantine.exists():
                    raise SuiteError(f"Rollback quarantine already exists: {quarantine}")
                os.replace(target, quarantine)
            elif backup.exists():
                raise SuiteError(f"Active target ownership changed: {target}")
    if kind == "retire-legacy" and target.exists() and backup.exists():
        raise SuiteError(f"Retired target was recreated during transaction: {target}")
    if backup.exists():
        if target.exists():
            raise SuiteError(f"Cannot restore backup over existing target: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(backup, target)


def deploy(
    *,
    package_root: Path,
    home: Path,
    appdata: Path,
    tools: list[str],
    state_root: Path,
    adopt_existing: bool,
    expected_package_fingerprint: str,
    fail_after: int | None = None,
    no_delete: bool = False,
) -> dict[str, Any]:
    with operation_lock(state_root):
        transaction_root, receipt = prepare_transaction(
            package_root=package_root,
            home=home,
            appdata=appdata,
            tools=tools,
            state_root=state_root,
            adopt_existing=adopt_existing,
            expected_package_fingerprint=expected_package_fingerprint,
        )
        if transaction_root is None:
            return {
                "action": "deploy",
                "status": "CURRENT",
                "package_fingerprint": receipt["package_fingerprint"],
                "entry_count": 0,
                "writes_performed": 0,
            }
        receipt_path = transaction_root / "receipt.json"
        receipt["status"] = "DEPLOYING"
        atomic_json(receipt_path, receipt)
        activated = 0
        mutated = 0
        try:
            for entry in receipt["entries"]:
                target = Path(entry["target"])
                backup = Path(entry["backup"])
                entry["phase"] = "MUTATING"
                mutated += 1
                atomic_json(receipt_path, receipt)
                if target.exists():
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        os.replace(target, backup)
                        entry["mutation_mode"] = "rename"
                    except PermissionError as exc:
                        if no_delete:
                            raise SuiteError(
                                f"No-delete deploy refuses in-place replacement: {target}"
                            ) from exc
                        if entry["kind"] not in {"replace-runtime", "replace-adapter"}:
                            raise
                        entry["mutation_mode"] = "in-place-copy"
                        entry["in_place_backup_complete"] = False
                        entry["in_place_activation_started"] = False
                        atomic_json(receipt_path, receipt)
                        entry["backup_fingerprint"] = copy_tree_exact(target, backup)
                        entry["in_place_backup_complete"] = True
                        atomic_json(receipt_path, receipt)
                if entry["kind"] != "retire-legacy":
                    stage = Path(entry["stage"])
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if entry.get("mutation_mode") == "in-place-copy":
                        entry["in_place_activation_started"] = True
                        atomic_json(receipt_path, receipt)
                        mirror_tree_in_place(stage, target)
                    else:
                        os.replace(stage, target)
                    verify_staged(
                        target,
                        expected_fingerprint=entry["expected_fingerprint"],
                        marker_name=entry["marker_name"],
                        transaction_id=receipt["transaction_id"],
                        expected_marker=entry["expected_marker"],
                    )
                entry["phase"] = "ACTIVATED"
                activated += 1
                atomic_json(receipt_path, receipt)
                if fail_after is not None and activated >= fail_after:
                    raise SuiteError("Injected deployment failure")
            receipt["status"] = "DEPLOYED"
            receipt["completed_at"] = utc_now()
            atomic_json(receipt_path, receipt)
        except Exception as exc:
            rollback_errors: list[str] = []
            for entry in reversed(receipt["entries"][:mutated]):
                try:
                    restore_entry(entry, receipt["transaction_id"])
                    entry["phase"] = "RESTORED"
                    atomic_json(receipt_path, receipt)
                except Exception as rollback_exc:  # pragma: no cover - emergency path
                    rollback_errors.append(
                        f"{entry['entry_id']}: {type(rollback_exc).__name__}: {rollback_exc}"
                    )
            receipt["status"] = (
                "ROLLBACK_FAILED" if rollback_errors else "AUTO_ROLLED_BACK"
            )
            receipt["error"] = f"{type(exc).__name__}: {exc}"
            receipt["rollback_errors"] = rollback_errors
            atomic_json(receipt_path, receipt)
            raise SuiteError(
                f"Deployment failed; transaction={receipt['transaction_id']}; "
                f"status={receipt['status']}: {exc}"
            ) from exc
        return {
            "action": "deploy",
            "status": "DEPLOYED",
            "transaction_id": receipt["transaction_id"],
            "receipt_path": str(receipt_path.resolve()),
            "package_fingerprint": receipt["package_fingerprint"],
            "entry_count": len(receipt["entries"]),
            "writes_performed": len(receipt["entries"]),
        }


def verify_receipt(
    receipt_path: Path,
    state_root: Path,
    *,
    package_root: Path,
    home: Path,
    appdata: Path,
) -> dict[str, Any]:
    receipt = validate_receipt_context(
        receipt_path,
        state_root=state_root,
        package_root=package_root,
        home=home,
        appdata=appdata,
    )
    if receipt.get("status") != "DEPLOYED":
        raise SuiteError(f"Receipt is not active: {receipt.get('status')}")
    rows = []
    for entry in receipt["entries"]:
        target = Path(entry["target"])
        if entry["kind"] == "retire-legacy":
            passed = not target.exists()
            fingerprint = None
        else:
            if not target.is_dir():
                passed = False
                fingerprint = None
            else:
                marker = load_json(target / entry["marker_name"])
                _, fingerprint = tree_manifest(
                    target, marker_names={entry["marker_name"], LEGACY_MARKER}
                )
                expected_marker = entry.get("expected_marker")
                if not isinstance(expected_marker, dict):
                    if entry["kind"] == "replace-runtime":
                        expected_marker = {
                            "managed_by": "agent-fleet-suite-v2-installer",
                            "package_id": PACKAGE_ID,
                            "distribution_package_version": receipt["package_version"],
                            "package_fingerprint": receipt["package_fingerprint"],
                            "payload_fingerprint": entry["expected_fingerprint"],
                        }
                    else:
                        expected_marker = {
                            "managed_by": "agent-fleet-suite-v2-installer",
                            "package_id": PACKAGE_ID,
                            "package_version": receipt["package_version"],
                            "package_fingerprint": receipt["package_fingerprint"],
                            "adapter_id": entry["skill"],
                            "adapter_fingerprint": entry["expected_fingerprint"],
                            "tool": entry["tool"],
                        }
                marker_mismatches = {
                    key: (value, marker.get(key))
                    for key, value in expected_marker.items()
                    if marker.get(key) != value
                }
                passed = (
                    marker.get("transaction_id") == receipt["transaction_id"]
                    and fingerprint == entry["expected_fingerprint"]
                    and not marker_mismatches
                )
        rows.append(
            {
                "entry_id": entry["entry_id"],
                "target": entry["target"],
                "passed": passed,
                "fingerprint": fingerprint,
                "marker_semantics_valid": (
                    passed if entry["kind"] != "retire-legacy" else None
                ),
            }
        )
    return {
        "action": "verify",
        "status": "PASS" if all(row["passed"] for row in rows) else "FAIL",
        "transaction_id": receipt["transaction_id"],
        "entries": rows,
        "writes_performed": 0,
    }


def rollback(
    receipt_path: Path,
    state_root: Path,
    *,
    package_root: Path,
    home: Path,
    appdata: Path,
) -> dict[str, Any]:
    with operation_lock(state_root):
        receipt = validate_receipt_context(
            receipt_path,
            state_root=state_root,
            package_root=package_root,
            home=home,
            appdata=appdata,
        )
        if receipt.get("status") != "DEPLOYED":
            raise SuiteError(f"Receipt is not in DEPLOYED state: {receipt.get('status')}")
        verification = verify_receipt(
            receipt_path,
            state_root,
            package_root=package_root,
            home=home,
            appdata=appdata,
        )
        if verification["status"] != "PASS":
            raise SuiteError("Active drift blocks exact rollback")
        for entry in receipt["entries"]:
            backup = Path(entry["backup"])
            if entry["exists"] and not backup.exists():
                raise SuiteError(f"Required backup is missing: {backup}")
        receipt["status"] = "ROLLING_BACK"
        atomic_json(receipt_path, receipt)
        for entry in reversed(receipt["entries"]):
            restore_entry(entry, receipt["transaction_id"])
            entry["phase"] = "RESTORED"
            atomic_json(receipt_path, receipt)
        receipt["status"] = "ROLLED_BACK"
        receipt["rolled_back_at"] = utc_now()
        atomic_json(receipt_path, receipt)
        return {
            "action": "rollback",
            "status": "ROLLED_BACK",
            "transaction_id": receipt["transaction_id"],
            "writes_performed": len(receipt["entries"]),
        }


def recover_deploy(
    receipt_path: Path,
    state_root: Path,
    *,
    package_root: Path,
    home: Path,
    appdata: Path,
) -> dict[str, Any]:
    with operation_lock(state_root):
        receipt = validate_receipt_context(
            receipt_path,
            state_root=state_root,
            package_root=package_root,
            home=home,
            appdata=appdata,
        )
        if receipt.get("status") in TERMINAL_STATES:
            return {
                "action": "recover-deploy",
                "status": receipt["status"],
                "transaction_id": receipt["transaction_id"],
                "writes_performed": 0,
            }
        for entry in reversed(receipt.get("entries", [])):
            restore_entry(entry, receipt["transaction_id"])
            entry["phase"] = "RESTORED"
            atomic_json(receipt_path, receipt)
        receipt["status"] = "RECOVERED_ROLLBACK"
        receipt["recovered_at"] = utc_now()
        atomic_json(receipt_path, receipt)
        return {
            "action": "recover-deploy",
            "status": "RECOVERED_ROLLBACK",
            "transaction_id": receipt["transaction_id"],
            "writes_performed": len(receipt.get("entries", [])),
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Seal and transactionally deploy the Agent Fleet shared suite."
    )
    parser.add_argument(
        "--package-root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument(
        "--appdata",
        type=Path,
        default=Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")),
    )
    parser.add_argument("--state-root", type=Path)
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("seal")
    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("--tools", default=",".join(LOCAL_TOOLS))
    plan_parser.add_argument("--adopt-existing", action="store_true")
    plan_parser.add_argument("--expected-package-fingerprint")
    deploy_parser = subparsers.add_parser("deploy")
    deploy_parser.add_argument("--tools", default=",".join(LOCAL_TOOLS))
    deploy_parser.add_argument("--adopt-existing", action="store_true")
    deploy_parser.add_argument("--expected-package-fingerprint", required=True)
    deploy_parser.add_argument(
        "--no-delete", action="store_true",
        help="Refuse the in-place fallback that removes files when a target cannot be renamed.",
    )
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--receipt", type=Path, required=True)
    rollback_parser = subparsers.add_parser("rollback")
    rollback_parser.add_argument("--receipt", type=Path, required=True)
    recover_parser = subparsers.add_parser("recover-deploy")
    recover_parser.add_argument("--receipt", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        package_root = args.package_root.expanduser().resolve()
        home = args.home.expanduser().resolve()
        appdata = args.appdata.expanduser().resolve()
        state_root = (
            args.state_root.expanduser().resolve()
            if args.state_root
            else default_state_root(home)
        )
        if args.action == "seal":
            result = seal_package(package_root)
        elif args.action == "plan":
            result = inspect_plan(
                package_root=package_root,
                home=home,
                appdata=appdata,
                tools=parse_tools(args.tools),
                adopt_existing=args.adopt_existing,
                expected_package_fingerprint=args.expected_package_fingerprint,
            )
        elif args.action == "deploy":
            result = deploy(
                package_root=package_root,
                home=home,
                appdata=appdata,
                tools=parse_tools(args.tools),
                state_root=state_root,
                adopt_existing=args.adopt_existing,
                expected_package_fingerprint=args.expected_package_fingerprint,
                no_delete=args.no_delete,
            )
        elif args.action == "verify":
            result = verify_receipt(
                args.receipt.expanduser().resolve(),
                state_root,
                package_root=package_root,
                home=home,
                appdata=appdata,
            )
        elif args.action == "rollback":
            result = rollback(
                args.receipt.expanduser().resolve(),
                state_root,
                package_root=package_root,
                home=home,
                appdata=appdata,
            )
        else:
            result = recover_deploy(
                args.receipt.expanduser().resolve(),
                state_root,
                package_root=package_root,
                home=home,
                appdata=appdata,
            )
        sys.stdout.buffer.write(canonical_json(result))
        return 0 if result.get("status") not in {"BLOCKED", "FAIL"} else 2
    except SuiteError as exc:
        sys.stderr.buffer.write(
            canonical_json(
                {
                    "action": args.action,
                    "status": "REFUSED",
                    "error": str(exc),
                    "writes_performed": 0,
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
