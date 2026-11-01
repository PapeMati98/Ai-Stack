from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Callable

import install_router_suite as suite


PLUGIN_ID = suite.PACKAGE_ID
PLUGIN_RELATIVE = suite.KIMI_PLUGIN_RELATIVE
REGISTRY_RELATIVE = Path("plugins") / "installed.json"
PLUGIN_TARGET_RELATIVE = Path("plugins") / "managed" / PLUGIN_ID
DEVICE_RELATIVE = Path("device_id")
ALLOWED_FILE_PREFIXES = (
    DEVICE_RELATIVE.as_posix(),
    REGISTRY_RELATIVE.as_posix(),
    PLUGIN_TARGET_RELATIVE.as_posix() + "/",
)
TERMINAL_STATES = {
    "DEPLOYED",
    "ROLLED_BACK",
    "AUTO_ROLLED_BACK",
    "RECOVERED_ROLLBACK",
}


def default_share_dir(appdata: Path) -> Path:
    return appdata / "kimi-desktop" / "daimon-share"


def default_bundle_root(appdata: Path) -> Path:
    return appdata / "kimi-desktop" / "daimon-bundle"


# Kimi Desktop renamed its install directory: 3.2.10 installs to Programs\Kimi,
# older builds used Programs\kimi-desktop. The tree below that directory is
# identical, so probe both names instead of pinning one. Measured 2026-09-20:
# node.exe present at Programs\Kimi\resources\resources\runtime\node.exe while
# the pinned path reported kimi-desktop-node-missing on a working installation.
KIMI_INSTALL_DIR_NAMES = ("Kimi", "kimi-desktop")


def candidate_nodes(localappdata: Path) -> list[Path]:
    return [
        localappdata
        / "Programs"
        / name
        / "resources"
        / "resources"
        / "runtime"
        / "node.exe"
        for name in KIMI_INSTALL_DIR_NAMES
    ]


def default_node(localappdata: Path) -> Path:
    candidates = candidate_nodes(localappdata)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    # Nothing found: return the first candidate so the blocker message names a
    # concrete path rather than an empty value.
    return candidates[0]


def kimi_home(share_dir: Path) -> Path:
    return share_dir / "daimon" / "runtime" / "kimi-code" / "home"


def sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_files(root: Path) -> dict[str, dict[str, Any]]:
    if not root.exists():
        return {}
    if not root.is_dir() or suite.is_filesystem_link(root):
        raise suite.SuiteError(f"Kimi Code home must be a plain directory: {root}")
    rows: dict[str, dict[str, Any]] = {}
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in list(directory_names):
            candidate = base / name
            if suite.is_filesystem_link(candidate):
                raise suite.SuiteError(
                    f"Linked directory in Kimi Code home is refused: {candidate}"
                )
        for name in file_names:
            candidate = base / name
            if suite.is_filesystem_link(candidate):
                raise suite.SuiteError(
                    f"Linked file in Kimi Code home is refused: {candidate}"
                )
            if not candidate.is_file():
                continue
            relative = candidate.relative_to(root).as_posix()
            rows[relative] = {
                "size": candidate.stat().st_size,
                "sha256": sha256_file(candidate),
            }
    return dict(sorted(rows.items(), key=lambda item: item[0].casefold()))


def snapshot_directories(root: Path) -> list[str]:
    if not root.exists():
        return []
    directories = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_dir() and not suite.is_filesystem_link(path)
    ]
    return sorted(directories, key=str.casefold)


def snapshot_controlled_files(home: Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    device = home / DEVICE_RELATIVE
    if device.is_file():
        rows[DEVICE_RELATIVE.as_posix()] = {
            "size": device.stat().st_size,
            "sha256": sha256_file(device),
        }
    registry = home / REGISTRY_RELATIVE
    if registry.is_file():
        rows[REGISTRY_RELATIVE.as_posix()] = {
            "size": registry.stat().st_size,
            "sha256": sha256_file(registry),
        }
    target = home / PLUGIN_TARGET_RELATIVE
    if target.exists():
        for relative, metadata in snapshot_files(target).items():
            rows[(PLUGIN_TARGET_RELATIVE / Path(relative)).as_posix()] = metadata
    return dict(sorted(rows.items(), key=lambda item: item[0].casefold()))


def cleanup_path_state(home: Path, share_dir: Path) -> list[dict[str, Any]]:
    paths: list[Path] = []
    cursor = share_dir
    while True:
        paths.append(cursor)
        if cursor == home:
            break
        if home == cursor or cursor not in home.parents:
            raise suite.SuiteError("Kimi home is outside the requested share directory")
        relative_parts = home.relative_to(cursor).parts
        cursor = cursor / relative_parts[0]
    paths.extend([home / "plugins", home / "plugins" / "managed"])
    return [{"path": str(path.resolve()), "existed": path.exists()} for path in paths]


def allowed_file(relative: str) -> bool:
    return relative in ALLOWED_FILE_PREFIXES[:2] or relative.startswith(
        ALLOWED_FILE_PREFIXES[2]
    )


def changed_files(
    before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]
) -> list[str]:
    keys = set(before) | set(after)
    return sorted(
        [key for key in keys if before.get(key) != after.get(key)],
        key=str.casefold,
    )


def load_registry(path: Path, *, allow_absent: bool = False) -> dict[str, Any] | None:
    if not path.exists() and allow_absent:
        return None
    value = suite.load_json(path)
    if set(value) != {"version", "plugins"} or value["version"] != 1:
        raise suite.SuiteError(f"Unsupported Kimi plugin registry: {path}")
    plugins = value["plugins"]
    if not isinstance(plugins, list) or any(not isinstance(item, dict) for item in plugins):
        raise suite.SuiteError(f"Invalid Kimi plugin registry entries: {path}")
    identifiers = [item.get("id") for item in plugins]
    if any(not isinstance(value, str) or not value for value in identifiers):
        raise suite.SuiteError(f"Invalid Kimi plugin identity in registry: {path}")
    if len(identifiers) != len(set(identifiers)):
        raise suite.SuiteError(f"Duplicate Kimi plugin identity in registry: {path}")
    return value


def plugin_entries(registry: dict[str, Any] | None) -> list[dict[str, Any]]:
    if registry is None:
        return []
    return [item for item in registry["plugins"] if item.get("id") == PLUGIN_ID]


def registry_entry_fingerprint(entry: dict[str, Any]) -> str:
    """Pin only this plugin's installed.json entry, not another plugin's bytes."""
    return hashlib.sha256(suite.canonical_json(entry)).hexdigest()


def resolved_equal(left: str | Path, right: Path) -> bool:
    try:
        return Path(left).expanduser().resolve() == right.expanduser().resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return False


def plugin_fingerprint(path: Path) -> str | None:
    if not path.exists():
        return None
    if not path.is_dir() or suite.is_filesystem_link(path):
        raise suite.SuiteError(f"Kimi plugin target must be a plain directory: {path}")
    _, fingerprint = suite.tree_manifest(path)
    return fingerprint


def inspect_plan(
    *,
    package_root: Path,
    share_dir: Path,
    bundle_root: Path,
    node: Path,
    adopt_existing: bool,
    expected_package_fingerprint: str | None,
) -> dict[str, Any]:
    manifest, lock = suite.load_verified_package(package_root)
    if (
        expected_package_fingerprint
        and lock["package_fingerprint"] != expected_package_fingerprint
    ):
        raise suite.SuiteError(
            "Expected package fingerprint does not match the sealed package"
        )
    source = (package_root / PLUGIN_RELATIVE).resolve()
    home = kimi_home(share_dir).resolve()
    target = (home / PLUGIN_TARGET_RELATIVE).resolve()
    registry_path = (home / REGISTRY_RELATIVE).resolve()
    launcher = (bundle_root / "bin" / "kimi-daimon.ps1").resolve()
    blockers: list[str] = []
    if not launcher.is_file():
        blockers.append("kimi-daimon-launcher-missing")
    if not node.is_file():
        blockers.append("kimi-desktop-node-missing")
    registry = load_registry(registry_path, allow_absent=True)
    entries = plugin_entries(registry)
    current_fingerprint = plugin_fingerprint(target)
    registered = (
        len(entries) == 1
        and resolved_equal(entries[0].get("root", ""), target)
        and entries[0].get("enabled") is True
    )
    existing = target.exists() or bool(entries)
    adoption_required = existing and not registered
    if adoption_required and not adopt_existing:
        blockers.append("adoption-required:kimi-desktop-plugin")
    change_required = (
        current_fingerprint != lock["kimi_desktop_plugin"]["fingerprint"]
        or not registered
    )
    return {
        "action": "kimi-plan",
        "status": "BLOCKED" if blockers else "READY",
        "package_id": suite.PACKAGE_ID,
        "package_version": manifest["package_version"],
        "release_state": manifest["release_state"],
        "package_fingerprint": lock["package_fingerprint"],
        "plugin_version": lock["kimi_desktop_plugin"]["version"],
        "plugin_fingerprint": lock["kimi_desktop_plugin"]["fingerprint"],
        "source": str(source),
        "share_dir": str(share_dir.resolve()),
        "kimi_home": str(home),
        "target": str(target),
        "registry": str(registry_path),
        "launcher": str(launcher),
        "node": str(node.resolve()),
        "current_fingerprint": current_fingerprint,
        "registered": registered,
        "adoption_required": adoption_required,
        "adopt_existing": adopt_existing,
        "change_required": change_required,
        "activation": "restart-required-if-kimi-desktop-is-running",
        "blockers": blockers,
        "writes_performed": 0,
    }


def run_native_install(
    *,
    source: Path,
    share_dir: Path,
    launcher: Path,
    node: Path,
) -> dict[str, Any]:
    command = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(launcher),
        "--node",
        str(node),
        "kimi-plugin",
        "install",
        str(source),
        "--share-dir",
        str(share_dir),
        "--json",
    ]
    environment = os.environ.copy()
    environment["KIMI_SHARE_DIR"] = str(share_dir)
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        timeout=180,
        check=False,
        creationflags=(
            getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        ),
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise suite.SuiteError(
            f"Kimi native plugin install failed ({completed.returncode}): "
            f"{detail[:500]}"
        )
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise suite.SuiteError("Kimi native plugin install returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise suite.SuiteError("Kimi native plugin install result must be an object")
    return result


def semantic_registry_is_prior_plus_plugin(
    current: dict[str, Any] | None,
    prior: dict[str, Any] | None,
) -> bool:
    if current is None:
        return prior is None
    remaining = [
        item for item in current["plugins"] if item.get("id") != PLUGIN_ID
    ]
    normalized = {"version": current["version"], "plugins": remaining}
    expected = prior or {"version": 1, "plugins": []}
    return normalized == expected and len(plugin_entries(current)) == 1


def write_receipt(path: Path, receipt: dict[str, Any]) -> None:
    suite.atomic_json(path, receipt)


def backup_path(source: Path, target: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, target, copy_function=shutil.copy2)
    elif source.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def move_to_quarantine(source: Path, destination: Path) -> None:
    if not source.exists() and not source.is_symlink():
        return
    if destination.exists() or destination.is_symlink():
        raise suite.SuiteError(
            f"Existing Kimi quarantine blocks no-delete recovery: {destination.name}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, destination)


def restore_prior_state(
    receipt: dict[str, Any],
    *,
    receipt_path: Path,
    allow_semantic_registry: bool,
) -> None:
    home = Path(receipt["kimi_home"])
    share_dir = Path(receipt["share_dir"])
    target = Path(receipt["target"])
    registry_path = Path(receipt["registry"])
    device_path = home / DEVICE_RELATIVE
    backup_root = receipt_path.parent / "backups"
    quarantine_root = receipt_path.parent / "quarantine"
    prior = receipt["prior"]
    no_delete = receipt.get("no_delete", False)

    current_snapshot = snapshot_controlled_files(home)
    unexpected_changes = [
        relative
        for relative in changed_files(prior["snapshot"], current_snapshot)
        if not allowed_file(relative)
    ]
    for relative in unexpected_changes:
        if relative in prior["snapshot"]:
            raise suite.SuiteError(
                f"Unexpected existing Kimi file drift blocks rollback: {relative}"
            )
        unexpected_path = home / Path(relative)
        if unexpected_path.is_file():
            quarantine_path = quarantine_root / "unexpected" / Path(relative)
            quarantine_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(unexpected_path, quarantine_path)

    current_target_fingerprint = plugin_fingerprint(target)
    recognized_targets = {
        None,
        prior["plugin_fingerprint"],
        receipt["expected_plugin_fingerprint"],
    }
    if current_target_fingerprint not in recognized_targets:
        raise suite.SuiteError("Kimi plugin target drift blocks exact rollback")

    current_registry = load_registry(registry_path, allow_absent=True)
    current_registry_sha = sha256_file(registry_path)
    recognized_registry = current_registry_sha in {
        None,
        prior["registry_sha256"],
        receipt.get("post", {}).get("registry_sha256"),
    }
    if (
        not recognized_registry
        and not (
            allow_semantic_registry
            and semantic_registry_is_prior_plus_plugin(
                current_registry, prior.get("registry_json")
            )
        )
    ):
        raise suite.SuiteError("Kimi plugin registry drift blocks exact rollback")

    current_device_sha = sha256_file(device_path)
    recognized_device = current_device_sha in {
        None,
        prior["device_sha256"],
        receipt.get("post", {}).get("device_sha256"),
    }
    if not recognized_device:
        raise suite.SuiteError("Kimi device identity drift blocks exact rollback")

    quarantine_root.mkdir(parents=True, exist_ok=True)
    if no_delete:
        preinstall_plugin = quarantine_root / "preinstall-plugin"
        target_is_prior = (
            prior["plugin_exists"]
            and current_target_fingerprint == prior["plugin_fingerprint"]
        )
        if target.exists() and not (target_is_prior and not preinstall_plugin.exists()):
            move_to_quarantine(target, quarantine_root / "active-plugin")
        if prior["plugin_exists"]:
            if preinstall_plugin.exists() or preinstall_plugin.is_symlink():
                if target.exists() or target.is_symlink():
                    raise suite.SuiteError(
                        "Kimi plugin target drift blocks no-delete restoration"
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(preinstall_plugin, target)
            elif not target_is_prior:
                backup = backup_root / "plugin"
                if not backup.exists():
                    raise suite.SuiteError(
                        "Kimi plugin backup is missing during no-delete restoration"
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                if backup.is_dir():
                    shutil.copytree(backup, target, copy_function=shutil.copy2)
                else:
                    shutil.copy2(backup, target)
    else:
        if target.exists():
            os.replace(target, quarantine_root / "active-plugin")
        if prior["plugin_exists"]:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(backup_root / "plugin", target)

    registry_is_prior = (
        current_registry_sha == prior["registry_sha256"]
        if prior["registry_exists"]
        else current_registry_sha is None
    )
    if no_delete:
        if registry_path.exists() and not registry_is_prior:
            move_to_quarantine(
                registry_path, quarantine_root / "active-installed.json"
            )
        if prior["registry_exists"] and not registry_is_prior:
            backup = backup_root / "installed.json"
            if not backup.is_file():
                raise suite.SuiteError(
                    "Kimi registry backup is missing during no-delete restoration"
                )
            registry_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, registry_path)
    else:
        if registry_path.exists():
            os.replace(registry_path, quarantine_root / "active-installed.json")
        if prior["registry_exists"]:
            registry_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(backup_root / "installed.json", registry_path)

    if no_delete:
        if prior["device_exists"]:
            if current_device_sha != prior["device_sha256"]:
                if device_path.exists():
                    move_to_quarantine(
                        device_path, quarantine_root / "active-device-id"
                    )
                backup = backup_root / "device_id"
                if not backup.is_file():
                    raise suite.SuiteError(
                        "Kimi device identity backup is missing during no-delete restoration"
                    )
                device_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup, device_path)
        elif device_path.exists():
            move_to_quarantine(
                device_path, quarantine_root / "created-device-id"
            )
    elif prior["device_exists"]:
        if current_device_sha != prior["device_sha256"]:
            if device_path.exists():
                os.replace(device_path, quarantine_root / "active-device-id")
            os.replace(backup_root / "device_id", device_path)
    elif device_path.exists():
        os.replace(device_path, quarantine_root / "created-device-id")

    del share_dir
    if not no_delete:
        for path_state in reversed(prior["cleanup_paths"]):
            cleanup_path = Path(path_state["path"])
            if not path_state["existed"] and cleanup_path.is_dir():
                try:
                    cleanup_path.rmdir()
                except OSError:
                    pass


def validate_receipt_context(
    receipt_path: Path,
    *,
    state_root: Path,
    package_root: Path,
    share_dir: Path,
    bundle_root: Path,
    node: Path,
) -> dict[str, Any]:
    """Reject receipt-controlled paths before verify, rollback, or recovery writes."""
    receipt = suite.load_json(receipt_path)
    transaction_id = receipt.get("transaction_id")
    if not isinstance(transaction_id, str) or not suite.TRANSACTION_ID_PATTERN.fullmatch(
        transaction_id
    ):
        raise suite.SuiteError("Kimi receipt transaction_id is invalid")
    state_root = state_root.expanduser().resolve()
    transaction_root = (state_root / "transactions" / transaction_id).resolve()
    if receipt_path.expanduser().resolve() != transaction_root / "receipt.json":
        raise suite.SuiteError("Kimi receipt path is outside its canonical transaction directory")
    for candidate in (transaction_root, receipt_path.expanduser().resolve()):
        if candidate.exists() and suite.is_filesystem_link(candidate):
            raise suite.SuiteError(f"Linked Kimi transaction metadata is refused: {candidate}")

    manifest, lock = suite.load_verified_package(package_root)
    expected_home = kimi_home(share_dir).resolve()
    expected_paths = {
        "source": (package_root / PLUGIN_RELATIVE).resolve(),
        "share_dir": share_dir.resolve(),
        "kimi_home": expected_home,
        "target": (expected_home / PLUGIN_TARGET_RELATIVE).resolve(),
        "registry": (expected_home / REGISTRY_RELATIVE).resolve(),
        "launcher": (bundle_root / "bin" / "kimi-daimon.ps1").resolve(),
        "node": node.resolve(),
    }
    if (
        receipt.get("schema_version") != 1
        or receipt.get("package_id") != suite.PACKAGE_ID
        or receipt.get("package_version") != manifest["package_version"]
        or receipt.get("package_fingerprint") != lock["package_fingerprint"]
        or receipt.get("expected_plugin_fingerprint")
        != lock["kimi_desktop_plugin"]["fingerprint"]
    ):
        raise suite.SuiteError("Kimi receipt package context does not match trusted inputs")
    for key, expected in expected_paths.items():
        if not suite.resolved_path_equal(receipt.get(key), expected):
            raise suite.SuiteError(f"Kimi receipt has an untrusted {key} path")

    prior = receipt.get("prior")
    if not isinstance(prior, dict):
        raise suite.SuiteError("Kimi receipt prior state is invalid")
    if "no_delete" in receipt and not isinstance(receipt["no_delete"], bool):
        raise suite.SuiteError("Kimi receipt no_delete flag is invalid")
    cleanup_paths = prior.get("cleanup_paths")
    expected_cleanup = cleanup_path_state(expected_home, share_dir.resolve())
    if not isinstance(cleanup_paths, list) or len(cleanup_paths) != len(expected_cleanup):
        raise suite.SuiteError("Kimi receipt cleanup paths are invalid")
    for actual, expected in zip(cleanup_paths, expected_cleanup):
        if (
            not isinstance(actual, dict)
            or set(actual) != {"path", "existed"}
            or not isinstance(actual.get("existed"), bool)
            or not suite.resolved_path_equal(actual.get("path"), Path(expected["path"]))
        ):
            raise suite.SuiteError("Kimi receipt contains an untrusted cleanup path")
    snapshot = prior.get("snapshot")
    if not isinstance(snapshot, dict):
        raise suite.SuiteError("Kimi receipt snapshot is invalid")
    for relative in snapshot:
        if not isinstance(relative, str):
            raise suite.SuiteError("Kimi receipt snapshot path is invalid")
        parsed = PurePosixPath(relative)
        if (
            parsed.is_absolute()
            or ".." in parsed.parts
            or parsed.as_posix() != relative
            or not allowed_file(relative)
        ):
            raise suite.SuiteError("Kimi receipt snapshot path escapes the managed home")
    post = receipt.get("post")
    if isinstance(post, dict):
        changed = post.get("changed_files")
        if not isinstance(changed, list) or any(
            not isinstance(relative, str)
            or PurePosixPath(relative).is_absolute()
            or ".." in PurePosixPath(relative).parts
            or not allowed_file(relative)
            for relative in changed
        ):
            raise suite.SuiteError("Kimi receipt changed-file list is invalid")
    return receipt


def deploy(
    *,
    package_root: Path,
    share_dir: Path,
    bundle_root: Path,
    node: Path,
    state_root: Path,
    adopt_existing: bool,
    expected_package_fingerprint: str,
    native_runner: Callable[..., dict[str, Any]] = run_native_install,
    no_delete: bool = False,
) -> dict[str, Any]:
    with suite.operation_lock(state_root):
        plan = inspect_plan(
            package_root=package_root,
            share_dir=share_dir,
            bundle_root=bundle_root,
            node=node,
            adopt_existing=adopt_existing,
            expected_package_fingerprint=expected_package_fingerprint,
        )
        if plan["status"] != "READY":
            raise suite.SuiteError(
                "Kimi deployment plan is blocked: " + ", ".join(plan["blockers"])
            )
        if plan["release_state"] != "released":
            raise suite.SuiteError("Only a released package may be deployed")
        if not plan["change_required"]:
            return {
                "action": "kimi-deploy",
                "status": "CURRENT",
                "package_fingerprint": plan["package_fingerprint"],
                "writes_performed": 0,
            }

        transaction_id = uuid.uuid4().hex
        transaction_root = state_root / "transactions" / transaction_id
        transaction_root.mkdir(parents=True, exist_ok=False)
        receipt_path = transaction_root / "receipt.json"
        home = Path(plan["kimi_home"])
        target = Path(plan["target"])
        registry_path = Path(plan["registry"])
        device_path = home / DEVICE_RELATIVE
        registry_json = load_registry(registry_path, allow_absent=True)
        prior_snapshot = snapshot_controlled_files(home)
        prior = {
            "home_exists": home.exists(),
            "cleanup_paths": cleanup_path_state(
                home, Path(plan["share_dir"])
            ),
            "plugin_exists": target.exists(),
            "plugin_fingerprint": plugin_fingerprint(target),
            "registry_exists": registry_path.exists(),
            "registry_sha256": sha256_file(registry_path),
            "registry_json": registry_json,
            "device_exists": device_path.exists(),
            "device_sha256": sha256_file(device_path),
            "snapshot": prior_snapshot,
        }
        backup_root = transaction_root / "backups"
        if target.exists():
            backup_path(target, backup_root / "plugin")
        if registry_path.exists():
            backup_path(registry_path, backup_root / "installed.json")
        if device_path.exists():
            backup_path(device_path, backup_root / "device_id")
        receipt: dict[str, Any] = {
            "schema_version": 1,
            "transaction_id": transaction_id,
            "package_id": suite.PACKAGE_ID,
            "package_version": plan["package_version"],
            "package_fingerprint": plan["package_fingerprint"],
            "expected_plugin_fingerprint": plan["plugin_fingerprint"],
            "created_at": suite.utc_now(),
            "status": "PREPARED",
            "source": plan["source"],
            "share_dir": plan["share_dir"],
            "kimi_home": plan["kimi_home"],
            "target": plan["target"],
            "registry": plan["registry"],
            "launcher": plan["launcher"],
            "node": plan["node"],
            "no_delete": no_delete,
            "prior": prior,
        }
        write_receipt(receipt_path, receipt)
        try:
            receipt["status"] = "INSTALLING"
            write_receipt(receipt_path, receipt)
            if no_delete and target.exists():
                move_to_quarantine(
                    target, transaction_root / "quarantine" / "preinstall-plugin"
                )
            native_result = native_runner(
                source=Path(plan["source"]),
                share_dir=Path(plan["share_dir"]),
                launcher=Path(plan["launcher"]),
                node=Path(plan["node"]),
            )
            receipt["native_result"] = {
                "installed": native_result.get("installed"),
                "id": native_result.get("id"),
                "version": native_result.get("version"),
                "skill_count": native_result.get("skillCount"),
                "state": native_result.get("state"),
                "existing_process_behavior": native_result.get(
                    "existingProcessBehavior"
                ),
            }
            current_snapshot = snapshot_controlled_files(home)
            unexpected = [
                relative
                for relative in changed_files(prior_snapshot, current_snapshot)
                if not allowed_file(relative)
            ]
            if unexpected:
                raise suite.SuiteError(
                    "Kimi native installer changed unexpected paths: "
                    + ", ".join(unexpected[:10])
                )
            current_registry = load_registry(registry_path)
            entries = plugin_entries(current_registry)
            if (
                plugin_fingerprint(target) != plan["plugin_fingerprint"]
                or len(entries) != 1
                or not resolved_equal(entries[0].get("root", ""), target)
                or entries[0].get("enabled") is not True
            ):
                raise suite.SuiteError("Kimi native plugin verification failed")
            receipt["post"] = {
                "plugin_fingerprint": plugin_fingerprint(target),
                "registry_sha256": sha256_file(registry_path),
                "registry_entry_fingerprint": registry_entry_fingerprint(entries[0]),
                "registry_other_ids": sorted(
                    item["id"] for item in current_registry["plugins"]
                    if item["id"] != PLUGIN_ID
                ),
                "device_sha256": sha256_file(device_path),
                "changed_files": changed_files(prior_snapshot, current_snapshot),
            }
            receipt["status"] = "DEPLOYED"
            receipt["completed_at"] = suite.utc_now()
            write_receipt(receipt_path, receipt)
        except Exception as exc:
            rollback_error = None
            try:
                restore_prior_state(
                    receipt,
                    receipt_path=receipt_path,
                    allow_semantic_registry=True,
                )
            except Exception as rollback_exc:  # pragma: no cover - emergency path
                rollback_error = (
                    f"{type(rollback_exc).__name__}: {rollback_exc}"
                )
            receipt["status"] = (
                "ROLLBACK_FAILED" if rollback_error else "AUTO_ROLLED_BACK"
            )
            receipt["error"] = f"{type(exc).__name__}: {exc}"
            receipt["rollback_error"] = rollback_error
            write_receipt(receipt_path, receipt)
            raise suite.SuiteError(
                f"Kimi deployment failed; transaction={transaction_id}; "
                f"status={receipt['status']}: {exc}"
            ) from exc
        return {
            "action": "kimi-deploy",
            "status": "DEPLOYED",
            "transaction_id": transaction_id,
            "receipt_path": str(receipt_path.resolve()),
            "package_fingerprint": plan["package_fingerprint"],
            "plugin_fingerprint": plan["plugin_fingerprint"],
            "activation": plan["activation"],
            "writes_performed": 1,
        }


def verify_receipt(
    receipt_path: Path,
    state_root: Path,
    *,
    package_root: Path,
    share_dir: Path,
    bundle_root: Path,
    node: Path,
) -> dict[str, Any]:
    receipt = validate_receipt_context(
        receipt_path,
        state_root=state_root,
        package_root=package_root,
        share_dir=share_dir,
        bundle_root=bundle_root,
        node=node,
    )
    if receipt.get("status") != "DEPLOYED":
        raise suite.SuiteError(f"Receipt is not active: {receipt.get('status')}")
    target = Path(receipt["target"])
    registry_path = Path(receipt["registry"])
    device_path = Path(receipt["kimi_home"]) / DEVICE_RELATIVE
    registry = load_registry(registry_path, allow_absent=True)
    entries = plugin_entries(registry)
    expected_entry = receipt["post"].get("registry_entry_fingerprint")
    registry_entry_valid = (
        len(entries) == 1
        and resolved_equal(entries[0].get("root", ""), target)
        and entries[0].get("enabled") is True
        and (
            expected_entry is None  # older receipts did not pin the full entry
            or registry_entry_fingerprint(entries[0]) == expected_entry
        )
    )
    checks = {
        "plugin_bytes": plugin_fingerprint(target)
        == receipt["expected_plugin_fingerprint"],
        "registry_bytes": sha256_file(registry_path)
        == receipt["post"]["registry_sha256"],
        "registry_entry": registry_entry_valid,
        "device_identity": sha256_file(device_path)
        == receipt["post"]["device_sha256"],
    }
    other_ids = sorted(
        item["id"] for item in registry["plugins"] if item["id"] != PLUGIN_ID
    ) if registry is not None else []
    required_checks = ("plugin_bytes", "registry_entry", "device_identity")
    return {
        "action": "kimi-verify",
        "status": "PASS" if all(checks[key] for key in required_checks) else "FAIL",
        "transaction_id": receipt["transaction_id"],
        "checks": checks,
        "registry_entry_basis": "exact" if expected_entry is not None else "legacy-fields",
        "registry_other_ids": other_ids,
        "rollback_safe": all(checks.values()),
        "writes_performed": 0,
    }


def rollback(
    receipt_path: Path,
    state_root: Path,
    *,
    package_root: Path,
    share_dir: Path,
    bundle_root: Path,
    node: Path,
    no_delete: bool = False,
) -> dict[str, Any]:
    with suite.operation_lock(state_root):
        receipt = validate_receipt_context(
            receipt_path,
            state_root=state_root,
            package_root=package_root,
            share_dir=share_dir,
            bundle_root=bundle_root,
            node=node,
        )
        if receipt.get("status") != "DEPLOYED":
            raise suite.SuiteError(
                f"Receipt is not in DEPLOYED state: {receipt.get('status')}"
            )
        verification = verify_receipt(
            receipt_path,
            state_root,
            package_root=package_root,
            share_dir=share_dir,
            bundle_root=bundle_root,
            node=node,
        )
        if verification["status"] != "PASS" or not verification["rollback_safe"]:
            raise suite.SuiteError("Active Kimi plugin drift blocks exact rollback")
        receipt["no_delete"] = bool(receipt.get("no_delete", False) or no_delete)
        receipt["status"] = "ROLLING_BACK"
        write_receipt(receipt_path, receipt)
        restore_prior_state(
            receipt,
            receipt_path=receipt_path,
            allow_semantic_registry=False,
        )
        receipt["status"] = "ROLLED_BACK"
        receipt["rolled_back_at"] = suite.utc_now()
        write_receipt(receipt_path, receipt)
        return {
            "action": "kimi-rollback",
            "status": "ROLLED_BACK",
            "transaction_id": receipt["transaction_id"],
            "writes_performed": 1,
        }


def recover_deploy(
    receipt_path: Path,
    state_root: Path,
    *,
    package_root: Path,
    share_dir: Path,
    bundle_root: Path,
    node: Path,
    no_delete: bool = False,
) -> dict[str, Any]:
    with suite.operation_lock(state_root):
        receipt = validate_receipt_context(
            receipt_path,
            state_root=state_root,
            package_root=package_root,
            share_dir=share_dir,
            bundle_root=bundle_root,
            node=node,
        )
        if receipt.get("status") in TERMINAL_STATES:
            return {
                "action": "kimi-recover-deploy",
                "status": receipt["status"],
                "transaction_id": receipt["transaction_id"],
                "writes_performed": 0,
            }
        receipt["no_delete"] = bool(receipt.get("no_delete", False) or no_delete)
        write_receipt(receipt_path, receipt)
        restore_prior_state(
            receipt,
            receipt_path=receipt_path,
            allow_semantic_registry=True,
        )
        receipt["status"] = "RECOVERED_ROLLBACK"
        receipt["recovered_at"] = suite.utc_now()
        write_receipt(receipt_path, receipt)
        return {
            "action": "kimi-recover-deploy",
            "status": "RECOVERED_ROLLBACK",
            "transaction_id": receipt["transaction_id"],
            "writes_performed": 1,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Transactionally install Agent Fleet as a native Kimi Desktop plugin."
    )
    parser.add_argument(
        "--package-root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument(
        "--appdata",
        type=Path,
        default=Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")),
    )
    parser.add_argument(
        "--localappdata",
        type=Path,
        default=Path(
            os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")
        ),
    )
    parser.add_argument("--share-dir", type=Path)
    parser.add_argument("--bundle-root", type=Path)
    parser.add_argument("--node", type=Path)
    parser.add_argument("--state-root", type=Path)
    subparsers = parser.add_subparsers(dest="action", required=True)
    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("--adopt-existing", action="store_true")
    plan_parser.add_argument("--expected-package-fingerprint")
    deploy_parser = subparsers.add_parser("deploy")
    deploy_parser.add_argument("--adopt-existing", action="store_true")
    deploy_parser.add_argument("--no-delete", action="store_true")
    deploy_parser.add_argument("--expected-package-fingerprint", required=True)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--receipt", type=Path, required=True)
    rollback_parser = subparsers.add_parser("rollback")
    rollback_parser.add_argument("--receipt", type=Path, required=True)
    rollback_parser.add_argument("--no-delete", action="store_true")
    recover_parser = subparsers.add_parser("recover-deploy")
    recover_parser.add_argument("--receipt", type=Path, required=True)
    recover_parser.add_argument("--no-delete", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        package_root = args.package_root.expanduser().resolve()
        appdata = args.appdata.expanduser().resolve()
        localappdata = args.localappdata.expanduser().resolve()
        share_dir = (
            args.share_dir.expanduser().resolve()
            if args.share_dir
            else default_share_dir(appdata).resolve()
        )
        bundle_root = (
            args.bundle_root.expanduser().resolve()
            if args.bundle_root
            else default_bundle_root(appdata).resolve()
        )
        node = (
            args.node.expanduser().resolve()
            if args.node
            else default_node(localappdata).resolve()
        )
        state_root = (
            args.state_root.expanduser().resolve()
            if args.state_root
            else suite.default_state_root(Path.home()).resolve()
        )
        if args.action == "plan":
            result = inspect_plan(
                package_root=package_root,
                share_dir=share_dir,
                bundle_root=bundle_root,
                node=node,
                adopt_existing=args.adopt_existing,
                expected_package_fingerprint=args.expected_package_fingerprint,
            )
        elif args.action == "deploy":
            result = deploy(
                package_root=package_root,
                share_dir=share_dir,
                bundle_root=bundle_root,
                node=node,
                no_delete=args.no_delete,
                state_root=state_root,
                adopt_existing=args.adopt_existing,
                expected_package_fingerprint=args.expected_package_fingerprint,
            )
        elif args.action == "verify":
            result = verify_receipt(
                args.receipt.expanduser().resolve(),
                state_root,
                package_root=package_root,
                share_dir=share_dir,
                bundle_root=bundle_root,
                node=node,
            )
        elif args.action == "rollback":
            result = rollback(
                args.receipt.expanduser().resolve(),
                state_root,
                package_root=package_root,
                share_dir=share_dir,
                bundle_root=bundle_root,
                node=node,
                no_delete=args.no_delete,
            )
        else:
            result = recover_deploy(
                args.receipt.expanduser().resolve(),
                state_root,
                package_root=package_root,
                share_dir=share_dir,
                bundle_root=bundle_root,
                node=node,
                no_delete=args.no_delete,
            )
        sys.stdout.buffer.write(suite.canonical_json(result))
        return 0 if result.get("status") not in {"BLOCKED", "FAIL"} else 2
    except suite.SuiteError as exc:
        sys.stderr.buffer.write(
            suite.canonical_json(
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
