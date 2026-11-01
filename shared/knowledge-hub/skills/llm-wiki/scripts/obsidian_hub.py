#!/usr/bin/env python3
"""Read-only Obsidian CLI bridge for the governed Knowledge Hub vault."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
TRUSTED_ROOT = "wiki"
SENSITIVE_ENV = re.compile(
    r"(?:api[_-]?key|(?:^|[_-])key(?:$|[_-])|token|secret|password|passwd|credential|cookie|private[_-]?key|bearer|authorization|oauth|refresh[_-]?token|access[_-]?token)",
    re.IGNORECASE,
)
READ_ONLY_ACTIONS = {
    "backlinks",
    "base-query",
    "file-info",
    "files",
    "links",
    "outline",
    "properties",
    "property-read",
    "read",
    "search",
    "search-context",
    "tags",
    "tasks",
    "vault-info",
    "wordcount",
}


class BridgeError(RuntimeError):
    pass


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BridgeError(f"Missing JSON file: {path}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BridgeError(f"Invalid JSON file: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BridgeError(f"JSON root must be an object: {path}")
    return value


def default_knowledge_config() -> Path:
    return Path.home() / ".agents" / "knowledge-config.json"


def resolve_vault(config_path: Path) -> Path:
    config = read_json(config_path)
    if config.get("managed_by") != "shared-agent-knowledge-hub":
        raise BridgeError("knowledge-config.json is not managed by shared-agent-knowledge-hub")
    raw_vault = config.get("vault_path")
    if not isinstance(raw_vault, str) or not raw_vault.strip():
        raise BridgeError("knowledge-config.json has no vault_path")
    vault = Path(raw_vault).expanduser().resolve(strict=True)
    reject_reparse_chain(vault, "Knowledge Hub vault")
    marker = vault / "knowledge-vault.json"
    if not marker.is_file():
        raise BridgeError(f"Knowledge Hub marker is missing: {marker}")
    return vault


def default_obsidian_config() -> Path:
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise BridgeError("APPDATA is not set")
        return Path(appdata) / "obsidian" / "obsidian.json"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "obsidian" / "obsidian.json"
    return Path.home() / ".config" / "obsidian" / "obsidian.json"


def resolve_cli() -> Path | None:
    candidates: list[Path] = []
    if os.name == "nt":
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            candidates.append(Path(local_appdata) / "Programs" / "Obsidian" / "Obsidian.com")
    else:
        candidates.append(Path.home() / ".local" / "bin" / "obsidian")
    discovered = shutil.which("obsidian")
    if discovered:
        candidates.append(Path(discovered))
    for candidate in candidates:
        if candidate.is_file() and not candidate.is_symlink():
            metadata = candidate.lstat()
            attributes = getattr(metadata, "st_file_attributes", 0)
            if attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
                continue
            return candidate.resolve()
    return None


def normalized_path(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path.resolve(strict=False))))


def reject_reparse_chain(path: Path, label: str) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        metadata = current.lstat()
        attributes = getattr(metadata, "st_file_attributes", 0)
        if current.is_symlink() or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            raise BridgeError(f"{label} crosses a symlink, junction, or reparse point: {current}")


def obsidian_state(config_path: Path, vault: Path) -> tuple[bool, str | None]:
    config = read_json(config_path)
    vaults = config.get("vaults")
    if not isinstance(vaults, dict):
        raise BridgeError(f"Obsidian config has no vault registry: {config_path}")
    expected = normalized_path(vault)
    for vault_id, row in vaults.items():
        if not isinstance(vault_id, str) or not isinstance(row, dict):
            continue
        raw_path = row.get("path")
        if isinstance(raw_path, str) and normalized_path(Path(raw_path)) == expected:
            return bool(config.get("cli")), vault_id
    return bool(config.get("cli")), None


def trusted_relative_path(vault: Path, raw_path: str, suffixes: set[str]) -> str:
    if not raw_path or "\x00" in raw_path:
        raise BridgeError("A non-empty trusted path is required")
    candidate_text = raw_path.replace("\\", "/")
    if any(character in candidate_text for character in ':*?"<>|[]'):
        raise BridgeError("ADS, glob, and shell-significant path characters are not allowed")
    candidate = Path(candidate_text)
    if candidate.is_absolute() or candidate.drive:
        raise BridgeError("Absolute paths are not allowed")
    if any(part in {"", ".", ".."} for part in candidate.parts):
        raise BridgeError("Path traversal and ambiguous path segments are not allowed")
    if candidate.parts[0].lower() != TRUSTED_ROOT:
        raise BridgeError("Obsidian Hub reads are restricted to wiki/")
    current = vault
    try:
        for part in candidate.parts:
            current = current / part
            metadata = current.lstat()
            attributes = getattr(metadata, "st_file_attributes", 0)
            if current.is_symlink() or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
                raise BridgeError("Symlinks, junctions, and reparse points are not allowed")
    except FileNotFoundError as exc:
        raise BridgeError(f"Trusted path does not exist: {candidate_text}") from exc
    except OSError as exc:
        raise BridgeError(f"Trusted path cannot be inspected: {candidate_text}: {exc}") from exc
    resolved = (vault / candidate).resolve(strict=True)
    trusted_root = (vault / TRUSTED_ROOT).resolve(strict=True)
    try:
        resolved.relative_to(trusted_root)
    except ValueError as exc:
        raise BridgeError("Resolved path escapes wiki/") from exc
    if not resolved.is_file():
        raise BridgeError(f"Trusted path is not a file: {candidate_text}")
    if resolved.stat().st_nlink > 1:
        raise BridgeError("Files with multiple hard links are not allowed")
    if suffixes and resolved.suffix.lower() not in suffixes:
        allowed = ", ".join(sorted(suffixes))
        raise BridgeError(f"Unsupported file type; expected one of: {allowed}")
    return resolved.relative_to(vault).as_posix()


def require_text(value: str | None, field: str, maximum: int = 500) -> str:
    if value is None or not value.strip():
        raise BridgeError(f"{field} is required")
    if len(value) > maximum:
        raise BridgeError(f"{field} exceeds {maximum} characters")
    if "\x00" in value or "\r" in value or "\n" in value:
        raise BridgeError(f"{field} contains a forbidden control character")
    return value


def build_cli_args(args: argparse.Namespace, vault: Path, vault_id: str) -> list[str]:
    action = args.action
    if action not in READ_ONLY_ACTIONS:
        raise BridgeError(f"Unsupported read-only action: {action}")
    prefix = [f"vault={vault_id}"]
    markdown_or_base = {".md", ".base"}

    if action == "vault-info":
        return prefix + ["vault", "info=path"]
    if action == "files":
        command = prefix + ["files", f"folder={TRUSTED_ROOT}"]
        if args.extension:
            command.append(f"ext={args.extension}")
        return command
    if action in {"search", "search-context"}:
        query = require_text(args.query, "query")
        command_name = "search:context" if action == "search-context" else "search"
        command = prefix + [
            command_name,
            f"query={query}",
            f"path={TRUSTED_ROOT}",
            f"limit={args.limit}",
            "format=json",
        ]
        if args.case_sensitive:
            command.append("case")
        return command

    suffixes = {".base"} if action == "base-query" else markdown_or_base
    path = trusted_relative_path(vault, require_text(args.path, "path", 1000), suffixes)
    if action == "read":
        return prefix + ["read", f"path={path}"]
    if action == "file-info":
        return prefix + ["file", f"path={path}"]
    if action == "backlinks":
        return prefix + ["backlinks", f"path={path}", "counts", "format=json"]
    if action == "links":
        return prefix + ["links", f"path={path}"]
    if action == "outline":
        return prefix + ["outline", f"path={path}", "format=json"]
    if action == "properties":
        return prefix + ["properties", f"path={path}", "format=json"]
    if action == "property-read":
        name = require_text(args.name, "name", 200)
        return prefix + ["property:read", f"name={name}", f"path={path}"]
    if action == "tags":
        return prefix + ["tags", f"path={path}", "format=json"]
    if action == "tasks":
        return prefix + ["tasks", f"path={path}", "verbose", "format=json"]
    if action == "wordcount":
        return prefix + ["wordcount", f"path={path}"]
    if action == "base-query":
        command = prefix + ["base:query", f"path={path}", f"format={args.output_format}"]
        if args.view:
            command.append(f"view={require_text(args.view, 'view', 200)}")
        return command
    raise BridgeError(f"No command mapping for action: {action}")


def scrubbed_environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if not SENSITIVE_ENV.search(key)}


def emit(payload: dict[str, Any], exit_code: int) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return exit_code


def doctor(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser()
    vault = resolve_vault(config_path)
    obsidian_config = Path(args.obsidian_config).expanduser()
    cli = resolve_cli()
    cli_enabled = False
    vault_id: str | None = None
    config_error: str | None = None
    app_running = (obsidian_config.parent / "lockfile").is_file()
    try:
        cli_enabled, vault_id = obsidian_state(obsidian_config, vault)
    except BridgeError as exc:
        config_error = str(exc)
    ready = (
        cli is not None
        and cli_enabled
        and vault_id is not None
        and config_error is None
        and app_running
    )
    if ready:
        status = "READY"
    elif cli is None:
        status = "CLI_NOT_INSTALLED"
    elif config_error:
        status = "OBSIDIAN_CONFIG_INVALID"
    elif not cli_enabled:
        status = "CLI_INSTALLED_NOT_ENABLED"
    elif vault_id is None:
        status = "VAULT_NOT_REGISTERED"
    else:
        status = "APP_NOT_RUNNING"
    return emit(
        {
            "action": "doctor",
            "app_running": app_running,
            "cli_enabled": cli_enabled,
            "cli_path": str(cli) if cli else None,
            "config_error": config_error,
            "knowledge_config": str(config_path.resolve(strict=False)),
            "mode": "read-only",
            "obsidian_config": str(obsidian_config.resolve(strict=False)),
            "ready": ready,
            "schema_version": SCHEMA_VERSION,
            "status": status,
            "vault_id": vault_id,
            "vault_path": str(vault),
            "local_obsidian_state_may_change": False,
            "writes_performed": 0,
        },
        0 if ready else 3,
    )


def execute(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser()
    vault = resolve_vault(config_path)
    obsidian_config = Path(args.obsidian_config).expanduser()
    cli = resolve_cli()
    if cli is None:
        raise BridgeError("Obsidian CLI executable was not found")
    cli_enabled, vault_id = obsidian_state(obsidian_config, vault)
    if not cli_enabled:
        raise BridgeError("Obsidian CLI is installed but not enabled")
    if vault_id is None:
        raise BridgeError("Knowledge Hub vault is not registered in Obsidian")
    command = [str(cli)] + build_cli_args(args, vault, vault_id)
    try:
        completed = subprocess.run(
            command,
            cwd=vault,
            env=scrubbed_environment(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            timeout=args.timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise BridgeError(f"Obsidian CLI timed out after {args.timeout} seconds") from exc
    stdout = completed.stdout.strip()
    stderr = completed.stderr.strip()
    known_blocker = "command line interface is not enabled" in (stdout + " " + stderr).lower()
    status = "PASS" if completed.returncode == 0 and not known_blocker else "FAILED"
    return emit(
        {
            "action": args.action,
            "argv": command[1:],
            "exit_code": completed.returncode,
            "mode": "read-only",
            "schema_version": SCHEMA_VERSION,
            "status": status,
            "stderr": stderr,
            "stdout": stdout,
            "vault_id": vault_id,
            "vault_path": str(vault),
            "local_obsidian_state_may_change": True,
            "writes_performed": 0,
        },
        0 if status == "PASS" else 4,
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("action", choices=["doctor"] + sorted(READ_ONLY_ACTIONS))
    result.add_argument("--config", default=str(default_knowledge_config()))
    result.add_argument("--obsidian-config", default=str(default_obsidian_config()))
    result.add_argument("--query")
    result.add_argument("--path")
    result.add_argument("--name")
    result.add_argument("--view")
    result.add_argument("--extension", choices=["base", "canvas", "md"])
    result.add_argument("--output-format", choices=["csv", "json", "md", "paths", "tsv"], default="json")
    result.add_argument("--limit", type=int, choices=range(1, 101), default=20, metavar="1..100")
    result.add_argument("--case-sensitive", action="store_true")
    result.add_argument("--timeout", type=int, choices=range(1, 121), default=45, metavar="1..120")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        if args.action == "doctor":
            return doctor(args)
        return execute(args)
    except BridgeError as exc:
        return emit(
            {
                "action": args.action,
                "error": str(exc),
                "mode": "read-only",
                "schema_version": SCHEMA_VERSION,
                "status": "REFUSED",
                "writes_performed": 0,
            },
            2,
        )


if __name__ == "__main__":
    raise SystemExit(main())
