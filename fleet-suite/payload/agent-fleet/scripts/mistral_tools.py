"""Fleet-bound native Vibe tools for the Mistral Large 4 maker lane.

Vibe 2.26.1 discovers this exact file through ``tool_paths`` only after the
run bootstrap activated a trusted policy that Fleet wrote outside the model
workspace. The policy binds the absolute workspace, mode, admitted scope and
inputs, declared check argv, limits and the pinned runtime; nothing the model
says can widen it. Pure policy/path/Docker helpers run on any Python; the
BaseTool classes exist only inside the pinned native interpreter.

Scope is an application boundary on these four tools, NOT an OS sandbox. The
model gets no shell, delete, rename, network, MCP or connector tool. Declared
checks run only in the pinned offline Docker image over a fresh read-only
snapshot of admitted files.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
import hashlib
import importlib.util
import sys
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
import subprocess
import threading
import time
from typing import Any, ClassVar

TOOL_READ = "fleet_read_file"
TOOL_LIST = "fleet_list_files"
TOOL_WRITE = "fleet_write_file"
TOOL_CHECK = "fleet_run_check"
READ_ONLY_TOOLS = (TOOL_READ, TOOL_LIST)
MAKER_TOOLS = (TOOL_READ, TOOL_LIST, TOOL_WRITE, TOOL_CHECK)
CHROME_TOOLS = tuple("fleet_chrome_" + name.removeprefix("chrome_") for name in ("chrome_attachment", "chrome_open", "chrome_read", "chrome_click", "chrome_type", "chrome_wait", "chrome_close", "chrome_saved_login"))
CHROME_READ_TOOLS = ("fleet_chrome_attachment", "fleet_chrome_open", "fleet_chrome_read", "fleet_chrome_wait", "fleet_chrome_close")
MAX_CHROME_RESULT_BYTES = 16_384
TOOL_MODES = ("advisory", "review", "implement")

POLICY_SCHEMA = "fleet-mistral-tool-policy/1"
POLICY_FILENAME = "mistral-tool-policy.json"
TELEMETRY_FILENAME = "mistral-tool-telemetry.jsonl"
SNAPSHOT_DIRNAME = "mistral-check-snapshots"
NATIVE_VERSION = "2.26.1"
DOCKER_EXECUTABLE = (Path.home() / "AppData/Local/Programs/DockerDesktop/resources/bin/docker.exe").as_posix()
DOCKER_IMAGE = "sha256:444ec9cb9b03c3be7c8b18d4a5e82e7cc908e2c0032c0a6e759338a48250b4de"
PINNED_RUNTIME = {"native_version": NATIVE_VERSION, "docker_executable": DOCKER_EXECUTABLE,
                  "docker_image": DOCKER_IMAGE}

DEFAULT_LIMITS = {
    "max_tool_calls": 200, "max_read_bytes": 262_144, "max_file_bytes": 1_048_576,
    "max_write_bytes": 524_288, "max_total_write_bytes": 8_388_608, "max_writes": 200,
    "max_list_entries": 2000, "max_search_matches": 200, "max_search_files": 2000,
    "max_check_runs": 12, "check_timeout_s": 900, "max_check_output_bytes": 65_536,
    "snapshot_max_bytes": 67_108_864, "snapshot_max_files": 5000,
    "container_pids": 256, "container_memory_mb": 2048, "container_cpus": 2,
}
LIMIT_CAPS = {
    "max_tool_calls": 1000, "max_read_bytes": 1_048_576, "max_file_bytes": 4_194_304,
    "max_write_bytes": 2_097_152, "max_total_write_bytes": 33_554_432, "max_writes": 1000,
    "max_list_entries": 10_000, "max_search_matches": 1000, "max_search_files": 10_000,
    "max_check_runs": 50, "check_timeout_s": 3600, "max_check_output_bytes": 262_144,
    "snapshot_max_bytes": 268_435_456, "snapshot_max_files": 20_000,
    "container_pids": 1024, "container_memory_mb": 8192, "container_cpus": 8,
}
MAX_ADMITTED_ENTRIES = 256
MAX_CHECKS = 16
MAX_CHECK_ARGS = 64
MAX_ARG_BYTES = 4096
MAX_CHECK_ARGV_BYTES = 16_384
MAX_POLICY_BYTES = 262_144
MAX_TELEMETRY_BYTES = 2_000_000
MAX_RELATIVE_PATH = 512


def _chrome_module():
    name = "fleet_mistral_chrome"
    path = Path(__file__).resolve().with_name("fleet_chrome_integration.py")
    current = sys.modules.get(name)
    if current is not None:
        if Path(current.__file__).resolve() != path: raise ToolPolicyError("chrome_assignment", "Chrome helper origin drift")
        return current
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None: raise ToolPolicyError("chrome_assignment", "Chrome helper unavailable")
    module = importlib.util.module_from_spec(spec);sys.modules[name]=module;spec.loader.exec_module(module)
    return module

_RESERVED_DEVICES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
                     *(f"COM{i}" for i in "0123456789¹²³"), *(f"LPT{i}" for i in "0123456789¹²³")}
_PROTECTED_NAMES = {
    ".git", ".vibe", ".agents", ".codex", ".claude", ".ssh", ".aws", ".azure", ".gnupg", ".docker", ".kube", ".netrc",
    "_netrc", ".npmrc", ".pypirc", ".pgpass", "auth.json", "token", "token.json", "tokens.json",
    "access_token", "refresh_token", "oauth_creds.json", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    ".git-credentials", "login data", "web data", "local state",
}
_PROTECTED_WORDS = ("secret", "credential", "password", "passwd", "cookie")
_PROTECTED_SUFFIXES = (".pem", ".key", ".pfx", ".p12", ".keystore", ".jks", ".kdbx", ".ppk", ".env")
_FORBIDDEN_CHARS = set('<>"|?*:')
_SKIPPED_DIRS = {"__pycache__"}
_HOST_ABSOLUTE = re.compile(r"(?:^|[\s'\"=(\[,])(?:[A-Za-z]:[\\/]|\\\\|//)")
_PYTHON_EXECUTABLE = re.compile(r"python(?:3(?:\.\d{1,2})?)?(?:\.exe)?")
_INTERPRETER_FLAGS = {"-B", "-u", "-I", "-E", "-s"}
_CHECK_MODULES = {"unittest", "pytest"}
_TMPFS = "/tmp:rw,noexec,nosuid,nodev,size=64m"
# Same run-id shape as Fleet; only its default evidence worktrees may live
# below the protected agent-state container. This never admits agent stores.
_FLEET_RUN_ID = re.compile(r"run-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{10}")


class ToolPolicyError(ValueError):
    """A visible, content-free refusal. ``code`` is the telemetry category."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def tools_for(mode: str, checks: list[list[str]] | tuple = (), chrome_assignment: dict[str, Any] | None = None) -> list[str]:
    if mode not in TOOL_MODES:
        raise ToolPolicyError("mode", "Unsupported native tool mode")
    tools = list(READ_ONLY_TOOLS) if mode != "implement" else list(MAKER_TOOLS) if checks else [TOOL_READ, TOOL_LIST, TOOL_WRITE]
    if chrome_assignment is not None:
        try:
            chrome = _chrome_module()
            route = chrome.validate_fleet_assignment(chrome_assignment, "mistral")
        except (ImportError, ValueError) as exc:
            raise ToolPolicyError("chrome_assignment", "Managed Chrome assignment refused") from exc
        if route["mode"] != mode or route["transport"] != "cli":
            raise ToolPolicyError("chrome_assignment", "Managed Chrome mode or transport mismatch")
        tools.extend(CHROME_READ_TOOLS if mode == "review" else CHROME_TOOLS)
    return tools


def is_protected_name(name: str) -> bool:
    low = name.casefold()
    return (low in _PROTECTED_NAMES or low.startswith(".env") or low.startswith("id_rsa")
            or bool(re.fullmatch(r"auth.*\.json", low))
            or low.endswith(_PROTECTED_SUFFIXES) or any(word in low for word in _PROTECTED_WORDS))


def normalize_relative(value: Any, *, _soft_secret: bool = False) -> str:
    """Workspace-relative POSIX path, or a refusal. Never resolves anything."""
    if type(value) is not str or not 1 <= len(value) <= MAX_RELATIVE_PATH:
        raise ToolPolicyError("path", "Path must be a non-empty workspace-relative string")
    if any(ord(char) < 32 or char in _FORBIDDEN_CHARS for char in value):
        raise ToolPolicyError("path", "Path contains drive, stream, wildcard or control characters")
    text = value.replace("\\", "/")
    if text.startswith("/"):
        raise ToolPolicyError("path", "Absolute, UNC and device paths are refused")
    text = text.rstrip("/")
    parts = text.split("/")
    for part in parts:
        if part in {"", ".", ".."}:
            raise ToolPolicyError("path", "Empty, current or parent path components are refused")
        # Only trailing dot/space and leading space are aliases on Windows; an
        # ordinary leading dot (.rsi, .github workflows) is a real file name.
        if part != part.rstrip(" .") or part.startswith(" "):
            raise ToolPolicyError("path", "Leading/trailing dot or space aliases are refused")
        if part.split(".")[0].rstrip().upper() in _RESERVED_DEVICES:
            raise ToolPolicyError("path", "Reserved device names are refused")
        if re.search(r"~\d", part):
            raise ToolPolicyError("path", "Short-name aliases are refused")
        protected = is_protected_name(part)
        if _soft_secret and "secret" in part.casefold():
            low = part.casefold()
            protected = (low in _PROTECTED_NAMES or low.startswith((".env", "id_rsa"))
                         or bool(re.fullmatch(r"auth.*\.json", low))
                         or low.endswith(_PROTECTED_SUFFIXES)
                         or any(word in low for word in _PROTECTED_WORDS if word != "secret"))
        if protected:
            raise ToolPolicyError("protected", "Protected repository, agent or credential path refused")
    if _soft_secret and (not parts[-1].casefold().endswith('.py')
                         or 'secret' not in parts[-1].casefold()
                         or any(is_protected_name(part) for part in parts[:-1])
                         or any(part.casefold().startswith(('auth', '.env')) or 'key' in part.casefold()
                                for part in parts)):
        raise ToolPolicyError("protected", "Only exact Python source filenames may be admitted")
    return "/".join(parts)


_VERIFIED_SENTINEL = object()


class VerifiedReadInputs:
    def __init__(self, sentinel: object, binding: tuple, records: list[dict[str, Any]]) -> None:
        if sentinel is not _VERIFIED_SENTINEL:
            raise ToolPolicyError("policy", "Verified input capability cannot be constructed")
        self.binding = binding
        self.records = records


def _source_scan(data: bytes, relative: str) -> None:
    try:
        path = Path(__file__).resolve().with_name("content_policy.py")
        spec = importlib.util.spec_from_file_location("fleet_trusted_content_policy", path)
        if spec is None or spec.loader is None:
            raise RuntimeError("scanner unavailable")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if module.scan_sensitive_source(data, relative):
            raise ToolPolicyError("protected", "Sensitive source content refused")
    except ToolPolicyError:
        raise
    except Exception as exc:
        raise ToolPolicyError("protected", "Source scanner failed") from exc


def validate_preflight_paths(workspace, entries) -> None:
    """Check lexical and filesystem denials before controller reads input bytes."""
    root = validate_workspace(str(workspace))
    for value in entries:
        try:
            rel = normalize_relative(value)
        except ToolPolicyError:
            rel = normalize_relative(value, _soft_secret=True)
        current = root
        for part in rel.split('/'):
            names = _entries(current)
            if part not in names:
                raise ToolPolicyError("missing", "Preflight path unavailable or case alias")
            current = current / part
            info = os.lstat(current)
            if _is_link(current, info) or (stat.S_ISREG(info.st_mode) and info.st_nlink > 1):
                raise ToolPolicyError("link", "Linked preflight path refused")


def _verified_source(policy: dict[str, Any], relative: str, path: Path) -> bytes:
    record = next((r for r in policy.get("read_sources", []) if r["path"] == relative), None)
    if record is None:
        raise ToolPolicyError("protected", "Source capability missing")
    # Resolve each component again around opening; compare the file identity and
    # bytes to the controller's original preflight, never accept a new hash.
    _, checked, exists = resolve_admitted(policy, relative)
    if not exists or checked != path:
        raise ToolPolicyError("missing", "Verified source is unavailable")
    info = _regular_file(path, policy["limits"]["max_file_bytes"])
    if [info.st_dev, info.st_ino] != record["identity"]:
        raise ToolPolicyError("link", "Verified source identity drift")
    data = _read_bytes(path, info, policy["limits"]["max_file_bytes"], verify_handle=True)
    _, again, exists = resolve_admitted(policy, relative)
    if not exists or again != path or [os.lstat(path).st_dev, os.lstat(path).st_ino] != record["identity"]:
        raise ToolPolicyError("link", "Verified source path drift")
    if len(data) != record["size_bytes"] or hashlib.sha256(data).hexdigest() != record["sha256"]:
        raise ToolPolicyError("protected", "Verified source content drift")
    _source_scan(data, relative)
    return data


def create_verified_read_inputs(*, workspace, run_dir, mode, scope, inputs, records) -> VerifiedReadInputs:
    workspace = validate_workspace(str(workspace))
    run = Path(run_dir).absolute()
    if not run.is_dir() or run.resolve().is_relative_to(workspace):
        raise ToolPolicyError("policy", "Invalid capability run directory")
    _check_directory_chain(run)
    if mode not in TOOL_MODES or not isinstance(records, list):
        raise ToolPolicyError("policy", "Invalid capability preflight")
    scopes = [normalize_relative(p) for p in scope]
    sources = []
    for item in inputs:
        try:
            normalize_relative(item)
            continue
        except ToolPolicyError:
            rel = normalize_relative(item, _soft_secret=True)
        if _under(rel, scopes) or any(root.startswith(rel + "/") for root in scopes):
            raise ToolPolicyError("policy", "Readonly source overlaps writable scope")
        matches = [r for r in records if isinstance(r, dict) and r.get("path") == rel]
        if len(matches) != 1 or set(matches[0]) != {"path", "sha256", "size_bytes"}:
            raise ToolPolicyError("policy", "Missing or ambiguous preflight record")
        rec = matches[0]
        if type(rec["sha256"]) is not str or not re.fullmatch(r"[a-f0-9]{64}", rec["sha256"]) or type(rec["size_bytes"]) is not int:
            raise ToolPolicyError("policy", "Invalid preflight digest")
        # Existing resolver checks every lexical component before opening.
        temp = {"workspace": str(workspace), "scope": [], "inputs": [rel], "read_sources": [{"path": rel}]}
        _, path, exists = resolve_admitted(temp, rel)
        if not exists:
            raise ToolPolicyError("missing", "Preflight source missing")
        info = _regular_file(path, DEFAULT_LIMITS["max_file_bytes"])
        entry = {**rec, "identity": [info.st_dev, info.st_ino]}
        temp["read_sources"] = [entry]
        temp["limits"] = DEFAULT_LIMITS
        _verified_source(temp, rel, path)
        sources.append(entry)
    return VerifiedReadInputs(_VERIFIED_SENTINEL, (str(workspace), str(run.resolve()), mode, tuple(scope), tuple(inputs)), sources)


def _is_link(path: Path, info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    junction = getattr(os.path, "isjunction", None)
    return (stat.S_ISLNK(info.st_mode) or bool(attributes & reparse)
            or bool(junction is not None and junction(path)))


def _under(relative: str, roots: list[str]) -> bool:
    return any(relative == root or relative.startswith(root + "/") for root in roots)


def _norm(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _is_default_fleet_worktree(path: Path, home: Path) -> bool:
    """Recognize a Fleet checkout, including a project subdir or retained retry.

    The caller binds the workspace; model-relative paths still reject .agents.
    Evidence, run directories and arbitrary children of agent state are not
    workspaces. Link checks remain mandatory for the entire directory chain.
    """
    try:
        relative = path.relative_to(home / ".agents" / "agent-fleet" / "evidence")
    except ValueError:
        return False
    return (len(relative.parts) >= 2 and bool(_FLEET_RUN_ID.fullmatch(relative.parts[0]))
            and relative.parts[1] == "worktree")


def validate_workspace(value: Any) -> Path:
    if type(value) is not str or not value or "\0" in value:
        raise ToolPolicyError("workspace", "Tool workspace must be an absolute path")
    if value.startswith(("\\\\", "//")):
        raise ToolPolicyError("workspace", "UNC/device workspaces are refused")
    path = Path(value)
    if not path.is_absolute() or not path.is_dir():
        raise ToolPolicyError("workspace", "Tool workspace must be an existing absolute directory")
    resolved = path.resolve()
    if _norm(resolved) != _norm(path):
        raise ToolPolicyError("workspace", "Tool workspace path must not traverse links")
    if resolved.parent == resolved:
        raise ToolPolicyError("workspace", "A filesystem root cannot be a tool workspace")
    home = Path.home().resolve()
    if _norm(home) == _norm(resolved) or home.is_relative_to(resolved):
        raise ToolPolicyError("workspace", "Home and its parents cannot be a tool workspace")
    fleet_worktree = _is_default_fleet_worktree(resolved, home)
    agent_container = home / ".agents"
    if any(is_protected_name(parent.name) and not (fleet_worktree and parent == agent_container)
           for parent in (resolved, *resolved.parents)):
        raise ToolPolicyError("workspace", "Tool workspace lies inside a protected directory")
    _check_directory_chain(path)
    return resolved


def _prospective_directory_chain(path: Path, *, code: str, message: str) -> None:
    """lstat every lexical component of a prospective path before resolve.

    A missing component is allowed only when lstat raises FileNotFoundError.
    A symlink, junction, or other reparse point is refused, including when the
    link is dangling. Any other lstat error is refused. Resolve runs only for
    the deepest existing component that has already passed the link check.
    """
    components: list[Path] = []
    current = path
    while True:
        components.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    missing = False
    existing: Path | None = None
    for component in reversed(components):
        try:
            info = os.lstat(component)
        except FileNotFoundError:
            missing = True
            continue
        except OSError as exc:
            raise ToolPolicyError(code, message) from exc
        if _is_link(component, info):
            raise ToolPolicyError("link", "Linked directory boundary refused")
        if missing:
            raise ToolPolicyError(code, message)
        existing = component
    if existing is None:
        return
    try:
        resolved = existing.resolve()
    except OSError as exc:
        raise ToolPolicyError(code, message) from exc
    if _norm(resolved) != _norm(existing):
        raise ToolPolicyError(code, message)


def validate_preview_workspace(value: Any) -> Path:
    if type(value) is not str or not value or "\0" in value:
        raise ToolPolicyError("workspace", "Tool workspace must be an absolute path")
    if value.startswith(("\\\\", "//")):
        raise ToolPolicyError("workspace", "UNC/device workspaces are refused")
    path = Path(value)
    if not path.is_absolute():
        raise ToolPolicyError("workspace", "Tool workspace must be an absolute path")
    _prospective_directory_chain(
        path, code="workspace", message="Tool workspace path must not traverse links")
    if path.exists() and not path.is_dir():
        raise ToolPolicyError("workspace", "Tool workspace must be an existing absolute directory")
    resolved = path.resolve()
    if path.exists() and _norm(resolved) != _norm(path):
        raise ToolPolicyError("workspace", "Tool workspace path must not traverse links")
    if resolved.parent == resolved:
        raise ToolPolicyError("workspace", "A filesystem root cannot be a tool workspace")
    home = Path.home().resolve()
    if _norm(home) == _norm(resolved) or home.is_relative_to(resolved):
        raise ToolPolicyError("workspace", "Home and its parents cannot be a tool workspace")
    fleet_worktree = _is_default_fleet_worktree(resolved, home)
    agent_container = home / ".agents"
    if any(is_protected_name(parent.name) and not (fleet_worktree and parent == agent_container)
           for parent in (resolved, *resolved.parents)):
        raise ToolPolicyError("workspace", "Tool workspace lies inside a protected directory")
    return resolved


def _check_directory_chain(path: Path) -> None:
    for parent in (path, *path.parents):
        if _is_link(parent, os.lstat(parent)):
            raise ToolPolicyError("link", "Linked directory boundary refused")


def _validate_checks(value: Any) -> list[list[str]]:
    if not isinstance(value, list) or len(value) > MAX_CHECKS:
        raise ToolPolicyError("checks", "Declared checks must be a bounded argv list")
    checks = []
    for argv in value:
        if (not isinstance(argv, list) or not 1 <= len(argv) <= MAX_CHECK_ARGS
                or any(type(arg) is not str or "\0" in arg or len(arg.encode("utf-8")) > MAX_ARG_BYTES
                       for arg in argv)
                or sum(len(arg.encode("utf-8")) for arg in argv) > MAX_CHECK_ARGV_BYTES):
            raise ToolPolicyError("checks", "Declared check argv is invalid or exceeds its bound")
        checks.append(list(argv))
    return checks


def _validate_limits(value: Any) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != set(DEFAULT_LIMITS):
        raise ToolPolicyError("limits", "Tool limits are incomplete")
    for key, item in value.items():
        if type(item) is not int or not 1 <= item <= LIMIT_CAPS[key]:
            raise ToolPolicyError("limits", "Tool limit is outside its bound")
    return dict(value)


def build_policy(*, workspace: Path | str, run_dir: Path | str, mode: str, scope: list[str],
                 inputs: list[str], checks: list[list[str]],
                 max_tool_calls: int | None = None,
                 chrome_assignment: dict[str, Any] | None = None,
                 preview: bool = False,
                 source_workspace: Path | str | None = None,
                 verified_read_inputs: VerifiedReadInputs | None = None) -> dict[str, Any]:
    limits = dict(DEFAULT_LIMITS)
    if max_tool_calls is not None:
        limits["max_tool_calls"] = max_tool_calls
    policy = {
        "schema": POLICY_SCHEMA, "mode": mode, "workspace": str(Path(workspace).absolute()),
        "run_dir": str(Path(run_dir).absolute()), "scope": list(scope), "inputs": list(inputs),
        "checks": [list(argv) for argv in checks], "limits": limits, "runtime": dict(PINNED_RUNTIME),
        "tools": tools_for(mode, checks, chrome_assignment),
    }
    if chrome_assignment is not None:
        policy["chrome_assignment"] = chrome_assignment
    if verified_read_inputs is not None:
        if type(verified_read_inputs) is not VerifiedReadInputs or verified_read_inputs.binding != (
                str(Path(workspace).resolve()), str(Path(run_dir).resolve()), mode, tuple(scope), tuple(inputs)):
            raise ToolPolicyError("policy", "Verified input capability binding mismatch")
        if preview:
            raise ToolPolicyError("policy", "Preview cannot activate verified inputs")
        if verified_read_inputs.records:
            policy["read_sources"] = [dict(r) for r in verified_read_inputs.records]
    if preview:
        policy["preview"] = True
    return validate_policy(policy, preview=preview, source_workspace=source_workspace)


def preview_policy(*, workspace: Path | str, run_dir: Path | str, mode: str, scope: list[str],
                   inputs: list[str], checks: list[list[str]],
                   source_workspace: Path | str | None = None,
                   max_tool_calls: int | None = None,
                   chrome_assignment: dict[str, Any] | None = None) -> dict[str, Any]:
    return build_policy(workspace=workspace, run_dir=run_dir, mode=mode, scope=scope,
                        inputs=inputs, checks=checks, max_tool_calls=max_tool_calls,
                        chrome_assignment=chrome_assignment, preview=True,
                        source_workspace=source_workspace)


def _enforce_writable_size_admission(policy: dict[str, Any], workspace: Path, scope: list[str],
                                     inputs: list[str], limits: dict[str, int]) -> None:
    """Refuse implement scopes that admit existing files the unchanged writer cannot edit.

    The native full-file writer truncates and rewrites each in-scope file with
    content bounded by ``max_write_bytes``. The model must be able to read the
    full existing content of every writable file and produce a complete
    replacement within the same bound, so any scope entry that names an existing
    file larger than ``max_write_bytes`` cannot be safely dispatched.

    Missing or yet-to-be-created scope paths are preserved. Read-only inputs and
    advisory/review modes never reach the writer and are not size-checked.
    Protected, secret, auth, linked and hard-linked entries are filtered by the
    existing ``_walk`` rules; this helper never opens them. A bounded walk is
    used; truncation always refuses so an unchecked writable scope cannot slip
    through silently.
    """
    if policy["mode"] != "implement":
        return
    if not scope:
        return
    write_bound = limits["max_write_bytes"]
    # The walk reuses the existing _admitted_files/_walk protection filters
    # (lstat-only, skips protected names, links and hard-links) and is bounded
    # by snapshot_max_files. The minimal dict only carries fields those helpers
    # read; no caller mutation propagates back into the policy.
    minimal: dict[str, Any] = {"workspace": str(workspace), "scope": list(scope),
                               "inputs": [], "limits": dict(limits)}
    found, truncated = _admitted_files(minimal, "", limits["snapshot_max_files"])
    if truncated:
        raise ToolPolicyError("not_admitted",
                              "Writable scope walk was truncated; size admission cannot be guaranteed")
    for rel, path in found:
        if not _under(rel, scope):
            continue  # Read-only input; not in the writable scope.
        try:
            info = os.lstat(path)
        except OSError:
            continue
        if info.st_size > write_bound:
            raise ToolPolicyError("not_admitted",
                                  "Writable file exceeds the writer's per-write bound")


def validate_policy(policy: Any, *, preview: bool = False,
                    source_workspace: Path | str | None = None) -> dict[str, Any]:
    """Fail closed on any field Fleet did not bind exactly."""
    if not isinstance(policy, dict):
        raise ToolPolicyError("policy", "Native tool policy shape is invalid")
    is_preview = bool(preview)
    if not is_preview:
        if "preview" in policy or "source_workspace" in policy:
            raise ToolPolicyError("policy", "Preview keys are forbidden in non-preview tool policy")
    base_keys = {"schema", "mode", "workspace", "run_dir", "scope", "inputs", "checks", "limits",
                 "runtime", "tools"}
    policy_keys = set(policy)
    if "read_sources" in policy_keys:
        if is_preview:
            raise ToolPolicyError("policy", "Preview cannot carry source grants")
        base_keys.add("read_sources")
    if is_preview:
        allowed_keys = (
            base_keys,
            base_keys | {"chrome_assignment"},
            base_keys | {"preview"},
            base_keys | {"chrome_assignment", "preview"},
            base_keys | {"source_workspace"},
            base_keys | {"chrome_assignment", "source_workspace"},
            base_keys | {"preview", "source_workspace"},
            base_keys | {"chrome_assignment", "preview", "source_workspace"},
        )
        if policy_keys not in allowed_keys or policy.get("schema") != POLICY_SCHEMA:
            raise ToolPolicyError("policy", "Native tool policy shape is invalid")
    else:
        if policy_keys not in (base_keys, base_keys | {"chrome_assignment"}) or policy.get("schema") != POLICY_SCHEMA:
            raise ToolPolicyError("policy", "Native tool policy shape is invalid")
    mode = policy["mode"]
    if mode not in TOOL_MODES:
        raise ToolPolicyError("mode", "Unsupported native tool mode")
    run_dir = policy["run_dir"]
    if type(run_dir) is not str or not run_dir or "\0" in run_dir:
        raise ToolPolicyError("policy", "Tool run directory must be an absolute path")
    if run_dir.startswith(("\\\\", "//")):
        raise ToolPolicyError("policy", "UNC/device run directories are refused")
    run_path = Path(run_dir)
    if not run_path.is_absolute():
        raise ToolPolicyError("policy", "Tool run directory must be an absolute path")

    if is_preview:
        workspace = validate_preview_workspace(policy["workspace"])
        if source_workspace is not None:
            source_workspace_path = validate_workspace(str(source_workspace))
        elif "source_workspace" in policy:
            source_workspace_path = validate_workspace(str(policy["source_workspace"]))
        else:
            if Path(policy["workspace"]).is_dir():
                source_workspace_path = validate_workspace(str(policy["workspace"]))
            else:
                raise ToolPolicyError("workspace", "Preview requires an existing source workspace for size admission")
        _prospective_directory_chain(
            run_path, code="policy", message="Tool run directory path traverses links")
        if run_path.exists() and not run_path.is_dir():
            raise ToolPolicyError("policy", "Tool run directory must be a directory")
        run_resolved = run_path.resolve()
        if run_resolved.parent == run_resolved:
            raise ToolPolicyError("policy", "A filesystem root cannot be a tool run directory")
        home = Path.home().resolve()
        if _norm(home) == _norm(run_resolved) or home.is_relative_to(run_resolved):
            raise ToolPolicyError("policy", "Home and its parents cannot be a tool run directory")
        if run_resolved == workspace or run_resolved.is_relative_to(workspace):
            raise ToolPolicyError("policy", "Tool policy and telemetry must live outside the model workspace")
        if run_resolved == source_workspace_path or run_resolved.is_relative_to(source_workspace_path):
            raise ToolPolicyError("policy", "Tool policy and telemetry must live outside the model workspace")
    else:
        workspace = validate_workspace(policy["workspace"])
        if not run_path.is_dir():
            raise ToolPolicyError("policy", "Tool run directory must exist")
        run_resolved = run_path.resolve()
        if run_resolved.is_relative_to(workspace):
            raise ToolPolicyError("policy", "Tool policy and telemetry must live outside the model workspace")
        _check_directory_chain(run_path)

    entries = {}
    for field in ("scope", "inputs"):
        value = policy[field]
        if not isinstance(value, list) or len(value) > MAX_ADMITTED_ENTRIES:
            raise ToolPolicyError("policy", f"Tool {field} must be a bounded list")
        normalized = []
        source_paths = {r.get("path") for r in policy.get("read_sources", []) if isinstance(r, dict)}
        for item in value:
            try:
                normalized.append(normalize_relative(item))
            except ToolPolicyError:
                if field != "inputs" or (not is_preview and item not in source_paths):
                    raise
                normalized.append(normalize_relative(item, _soft_secret=True))
        if len({item.casefold() for item in normalized}) != len(set(normalized)):
            raise ToolPolicyError("case_alias", "Case-alias admissions refused")
        entries[field] = list(dict.fromkeys(normalized))
    if mode == "implement" and not entries["scope"]:
        raise ToolPolicyError("policy", "Implementation requires a non-empty writable scope")
    if not entries["scope"] and not entries["inputs"]:
        raise ToolPolicyError("policy", "Tool policy admits no workspace path")
    sources = policy.get("read_sources", [])
    if not isinstance(sources, list) or len(sources) > MAX_ADMITTED_ENTRIES:
        raise ToolPolicyError("policy", "Invalid source records")
    seen = set()
    for rec in sources:
        if (not isinstance(rec, dict) or set(rec) != {"path", "sha256", "size_bytes", "identity"}
                or rec["path"] not in entries["inputs"] or rec["path"] in seen
                or _under(rec["path"], entries["scope"])
                or any(root.startswith(rec["path"] + "/") for root in entries["scope"])
                or type(rec["sha256"]) is not str or not re.fullmatch(r"[a-f0-9]{64}", rec["sha256"])
                or type(rec["size_bytes"]) is not int or rec["size_bytes"] < 0
                or not isinstance(rec["identity"], list) or len(rec["identity"]) != 2
                or any(type(x) is not int for x in rec["identity"])):
            raise ToolPolicyError("policy", "Invalid verified source record")
        normalize_relative(rec["path"], _soft_secret=True)
        seen.add(rec["path"])
    if is_preview:
        for rel in entries["inputs"]:
            if _under(rel, entries["scope"]) or any(root.startswith(rel + "/") for root in entries["scope"]):
                try:
                    normalize_relative(rel)
                except ToolPolicyError:
                    raise ToolPolicyError("policy", "Readonly source overlaps writable scope")
    checks = _validate_checks(policy["checks"])
    limits = _validate_limits(policy["limits"])
    if policy["runtime"] != PINNED_RUNTIME:
        raise ToolPolicyError("runtime", "Native tool runtime/Docker pin drift")
    assignment = policy.get("chrome_assignment")
    expected_tools = tools_for(mode, checks, assignment)
    if policy["tools"] != expected_tools:
        raise ToolPolicyError("tools", "Native tool set does not match the mode")

    if is_preview:
        _enforce_writable_size_admission(policy, source_workspace_path, entries["scope"], entries["inputs"], limits)
    else:
        _enforce_writable_size_admission(policy, workspace, entries["scope"], entries["inputs"], limits)

    result = {"schema": POLICY_SCHEMA, "mode": mode, "workspace": str(workspace),
              "run_dir": str(run_resolved), "scope": entries["scope"],
              "inputs": entries["inputs"], "checks": checks, "limits": limits,
              "runtime": dict(PINNED_RUNTIME), "tools": expected_tools}
    if assignment is not None:
        result["chrome_assignment"] = assignment
    if sources:
        result["read_sources"] = [dict(r) for r in sources]
    if is_preview:
        result["preview"] = True
        result["source_workspace"] = str(source_workspace_path)
    return result


def write_policy(policy: dict[str, Any]) -> Path:
    if isinstance(policy, dict) and policy.get("preview") is True:
        raise ToolPolicyError("policy", "Preview tool policy cannot be written to disk")
    policy = validate_policy(policy)
    path = Path(policy["run_dir"]) / POLICY_FILENAME
    with path.open("x", encoding="utf-8") as stream:
        json.dump(policy, stream, ensure_ascii=False, sort_keys=True)
    return path


def load_policy(path: Path | str, expected_sha256: str | None = None) -> dict[str, Any]:
    path = Path(path)
    try:
        info = os.lstat(path)
        if _is_link(path, info) or info.st_nlink > 1 or not stat.S_ISREG(info.st_mode) or info.st_size > MAX_POLICY_BYTES:
            raise ToolPolicyError("policy", "Native tool policy file is not a bounded regular file")
        raw = _read_bytes(path, info, MAX_POLICY_BYTES)
        if expected_sha256 is not None and (type(expected_sha256) is not str
                or not re.fullmatch(r"[a-f0-9]{64}", expected_sha256)
                or hashlib.sha256(raw).hexdigest() != expected_sha256):
            raise ToolPolicyError("policy", "Trusted bootstrap policy digest mismatch")
        value = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        raise ToolPolicyError("policy", "Native tool policy is unreadable") from exc
    if isinstance(value, dict) and (value.get("preview") is True or "source_workspace" in value):
        raise ToolPolicyError("policy", "Persisted tool policy cannot be a preview policy")
    if isinstance(value, dict) and value.get("read_sources"):
        if (type(expected_sha256) is not str or not re.fullmatch(r"[a-f0-9]{64}", expected_sha256)
                or hashlib.sha256(raw).hexdigest() != expected_sha256):
            raise ToolPolicyError("policy", "Trusted bootstrap policy digest required")
    policy = validate_policy(value)
    if _norm(Path(policy["run_dir"]) / POLICY_FILENAME) != _norm(path.resolve()):
        raise ToolPolicyError("policy", "Native tool policy is not in its bound run directory")
    return policy


class RunState:
    """Per-run counters. One instance per activated native process."""

    def __init__(self, policy: dict[str, Any], *, telemetry: Path | None = None) -> None:
        self.policy = policy
        self.calls = 0
        self.writes = 0
        self.written_bytes = 0
        self.check_runs = 0
        self.telemetry = telemetry
        self.lock = threading.RLock()
        self.chrome_cli = None
        assignment = policy.get("chrome_assignment")
        if assignment is not None:
            chrome = _chrome_module()
            self.chrome_cli = chrome.ManagedCLI(assignment, max_calls=policy["limits"]["max_tool_calls"])

    def begin(self, tool: str) -> None:
        self.calls += 1
        if tool not in self.policy["tools"]:
            raise ToolPolicyError("mode", "Tool is not admitted in this run mode")
        if self.calls > self.policy["limits"]["max_tool_calls"]:
            raise ToolPolicyError("budget", "Per-run native tool-call budget exhausted")

    def record(self, tool: str, outcome: str, code: str | None, metadata: dict[str, Any]) -> None:
        if self.telemetry is None:
            return
        if self.telemetry.stat().st_size >= MAX_TELEMETRY_BYTES:
            raise ToolPolicyError("budget", "Native telemetry bound exhausted")
        row = {"seq": self.calls, "tool": tool, "outcome": outcome, "code": code, "result": metadata}
        with self.telemetry.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, sort_keys=True) + "\n")


def _entries(directory: Path) -> set[str]:
    try:
        return set(os.listdir(directory))
    except OSError as exc:
        raise ToolPolicyError("missing", "Admitted directory is unavailable") from exc


def resolve_admitted(policy: dict[str, Any], value: Any, *, write: bool = False) -> tuple[str, Path, bool]:
    """Return (relative, path, exists). Refuse aliases, links and unadmitted paths."""
    try:
        relative = normalize_relative(value)
    except ToolPolicyError:
        if write or value not in {r["path"] for r in policy.get("read_sources", [])}:
            raise
        relative = normalize_relative(value, _soft_secret=True)
    roots = policy["scope"] if write else policy["scope"] + policy["inputs"]
    if not _under(relative, roots):
        raise ToolPolicyError("not_admitted", "Path is outside the admitted scope/inputs")
    current = Path(policy["workspace"])
    _check_directory_chain(current)
    parts = relative.split("/")
    for index, part in enumerate(parts):
        names = _entries(current)
        if part not in names:
            if part.casefold() in {name.casefold() for name in names}:
                raise ToolPolicyError("case_alias", "Case-alias path refused")
            return relative, current.joinpath(*parts[index:]), False
        current = current / part
        info = os.lstat(current)
        if _is_link(current, info):
            raise ToolPolicyError("link", "Symlink, junction or reparse-point path refused")
    return relative, current, True


def _regular_file(path: Path, limit: int) -> os.stat_result:
    info = os.lstat(path)
    if _is_link(path, info) or not stat.S_ISREG(info.st_mode):
        raise ToolPolicyError("type", "Path is not a regular file")
    if info.st_nlink > 1:
        raise ToolPolicyError("link", "Hard-linked file refused")
    if info.st_size > limit:
        raise ToolPolicyError("bounds", "File exceeds the per-file bound")
    return info


def _read_bytes(path: Path, info: os.stat_result, limit: int, *, verify_handle: bool = False) -> bytes:
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_ino, opened.st_dev) != (info.st_ino, info.st_dev) or opened.st_nlink > 1:
            raise ToolPolicyError("link", "File changed identity while opening")
        if verify_handle and os.name == "nt":
            import ctypes
            import msvcrt
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            fn = kernel.GetFinalPathNameByHandleW
            fn.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong]
            fn.restype = ctypes.c_ulong
            buffer = ctypes.create_unicode_buffer(32768)
            count = fn(msvcrt.get_osfhandle(stream.fileno()), buffer, len(buffer), 0)
            actual = buffer.value
            if not count or count >= len(buffer) or not actual.startswith("\\\\?\\"):
                raise ToolPolicyError("link", "Cannot verify opened source path")
            if _norm(actual[4:]) != _norm(path):
                raise ToolPolicyError("link", "Opened source path differs from admission")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ToolPolicyError("bounds", "File exceeds the per-file bound")
    return data


def _decode(data: bytes) -> str:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolPolicyError("encoding", "File is not UTF-8 text") from exc
    if "\0" in text:
        raise ToolPolicyError("encoding", "File is not text")
    return text


def _clip(text: str, limit: int) -> tuple[str, bool]:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text, False
    return raw[:limit].decode("utf-8", errors="ignore"), True


def read_text(policy: dict[str, Any], path: Any, offset: int = 0, limit: int | None = None,
              *, state: RunState | None = None) -> dict[str, Any]:
    relative, target, exists = resolve_admitted(policy, path)
    if not exists:
        raise ToolPolicyError("missing", "Admitted file does not exist")
    limits = policy["limits"]
    info = _regular_file(target, limits["max_file_bytes"])
    data = (_verified_source(policy, relative, target) if any(r["path"] == relative for r in policy.get("read_sources", []))
            else _read_bytes(target, info, limits["max_file_bytes"]))
    lines = _decode(data).splitlines(keepends=True)
    if type(offset) is not int or offset < 0 or (limit is not None and (type(limit) is not int or limit < 1)):
        raise ToolPolicyError("args", "Line window is invalid")
    window = lines[offset:] if limit is None else lines[offset:offset + limit]
    content, truncated = _clip("".join(window), limits["max_read_bytes"])
    return {"path": relative, "content": content, "start_line": offset + 1 if window else 0,
            "line_count": len(content.splitlines()), "total_lines": len(lines), "truncated": truncated}


def _walk(policy: dict[str, Any], relative: str, path: Path, *, limit: int) -> tuple[list[tuple[str, Path]], bool]:
    """Admitted regular files under one resolved entry; links and protected names skipped."""
    found: list[tuple[str, Path]] = []
    if path.is_file():
        info = os.lstat(path)
        if any(r["path"] == relative for r in policy.get("read_sources", [])):
            _verified_source(policy, relative, path)
        return ([(relative, path)], False) if info.st_nlink == 1 and not _is_link(path, info) else ([], False)
    stack = [(relative, path)]
    visited = 0
    while stack:
        rel_dir, directory = stack.pop()
        try:
            resolve_admitted(policy, rel_dir)
            children = []
            with os.scandir(directory) as scan:
                for entry in scan:
                    visited += 1
                    if visited > policy["limits"]["snapshot_max_files"] * 4:
                        return found, True
                    children.append(entry)
            children.sort(key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in children:
            if is_protected_name(entry.name) or entry.name in _SKIPPED_DIRS:
                continue
            child = Path(entry.path)
            try:
                info = os.lstat(child)
            except OSError:
                continue
            if _is_link(child, info) or (stat.S_ISREG(info.st_mode) and info.st_nlink > 1):
                continue
            rel_child = f"{rel_dir}/{entry.name}"
            try:
                normalize_relative(rel_child)
            except ToolPolicyError:
                continue
            if stat.S_ISDIR(info.st_mode):
                stack.append((rel_child, child))
            elif stat.S_ISREG(info.st_mode):
                if len(found) >= limit:
                    return found, True
                found.append((rel_child, child))
    return found, False


def _admitted_files(policy: dict[str, Any], path: Any, limit: int) -> tuple[list[tuple[str, Path]], bool]:
    if path in (None, "", "."):
        roots = sorted(set(policy["scope"] + policy["inputs"]))
    else:
        roots = [resolve_admitted(policy, path)[0]]
    files: list[tuple[str, Path]] = []
    truncated = False
    for root in roots:
        relative, target, exists = resolve_admitted(policy, root)
        if not exists:
            if any(r["path"] == relative for r in policy.get("read_sources", [])):
                raise ToolPolicyError("missing", "Verified source missing")
            if path not in (None, "", "."):
                raise ToolPolicyError("missing", "Admitted path does not exist")
            continue
        found, clipped = _walk(policy, relative, target, limit=limit - len(files))
        files.extend(found)
        if clipped:
            truncated = True
            break
    unique = list(dict.fromkeys(rel for rel, _ in files))
    paths = dict(files)
    return [(rel, paths[rel]) for rel in unique], truncated


def list_files(policy: dict[str, Any], path: Any = "", query: str | None = None,
               *, state: RunState | None = None) -> dict[str, Any]:
    limits = policy["limits"]
    if query is not None and (type(query) is not str or not 1 <= len(query) <= 200 or "\n" in query):
        raise ToolPolicyError("args", "Search text must be one line of 1..200 characters")
    cap = limits["max_list_entries"] if query is None else limits["max_search_files"]
    files, truncated = _admitted_files(policy, path, cap)
    if query is None:
        entries, clipped = _bounded_strings([rel for rel, _ in files], limits["max_read_bytes"])
        return {"entries": entries, "matches": [], "truncated": truncated or clipped}
    matches: list[str] = []
    for relative, target in files:
        try:
            _, target, _ = resolve_admitted(policy, relative)
            info = _regular_file(target, limits["max_file_bytes"])
            text = _decode(_verified_source(policy, relative, target) if any(r["path"] == relative for r in policy.get("read_sources", [])) else _read_bytes(target, info, limits["max_file_bytes"]))
        except (ToolPolicyError, OSError):
            if any(r["path"] == relative for r in policy.get("read_sources", [])):
                raise
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if query in line:
                if len(matches) >= limits["max_search_matches"]:
                    matches, _ = _bounded_strings(matches, limits["max_read_bytes"])
                    return {"entries": [], "matches": matches, "truncated": True}
                matches.append(f"{relative}:{number}: {line.strip()[:240]}")
    matches, clipped = _bounded_strings(matches, limits["max_read_bytes"])
    return {"entries": [], "matches": matches, "truncated": truncated or clipped}


def _bounded_strings(values: list[str], limit: int) -> tuple[list[str], bool]:
    retained = []
    used = 2
    for value in values:
        used += len(json.dumps(value, ensure_ascii=False).encode("utf-8")) + 1
        if used > limit:
            return retained, True
        retained.append(value)
    return retained, False


def write_text(policy: dict[str, Any], path: Any, content: Any, overwrite: bool = False,
               *, state: RunState | None = None) -> dict[str, Any]:
    if policy["mode"] != "implement":
        raise ToolPolicyError("mode", "Writes are unavailable in read-only modes")
    limits = policy["limits"]
    if type(content) is not str or "\0" in content:
        raise ToolPolicyError("encoding", "Content must be text without NUL")
    try:
        data = content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ToolPolicyError("encoding", "Content is not valid UTF-8 text") from exc
    if len(data) > limits["max_write_bytes"]:
        raise ToolPolicyError("bounds", "Content exceeds the per-write bound")
    if state is not None and (state.writes + 1 > limits["max_writes"]
                              or state.written_bytes + len(data) > limits["max_total_write_bytes"]):
        raise ToolPolicyError("budget", "Per-run write budget exhausted")
    relative, target, exists = resolve_admitted(policy, path, write=True)
    workspace = Path(policy["workspace"])
    if exists:
        if overwrite is not True:
            raise ToolPolicyError("exists", "File exists; pass overwrite=true to update it")
        info = _regular_file(target, limits["max_file_bytes"])
        _decode(_read_bytes(target, info, limits["max_file_bytes"]))
        with target.open("r+b") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_ino, opened.st_dev) != (info.st_ino, info.st_dev) or opened.st_nlink > 1:
                raise ToolPolicyError("link", "File changed identity while opening")
            stream.truncate(0)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    else:
        parts = relative.split("/")
        current = workspace
        for index, part in enumerate(parts[:-1]):
            current = current / part
            if current.exists():
                if _is_link(current, os.lstat(current)):
                    raise ToolPolicyError("link", "Linked write parent refused")
                continue
            if not _under("/".join(parts[:index + 1]), policy["scope"]):
                raise ToolPolicyError("not_admitted", "Missing parent directory is outside the writable scope")
            current.mkdir()
        _, target, exists = resolve_admitted(policy, relative, write=True)
        if exists:
            raise ToolPolicyError("exists", "Create target appeared during the operation")
        with target.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _regular_file(target, limits["max_file_bytes"])
    if state is not None:
        state.writes += 1
        state.written_bytes += len(data)
    return {"path": relative, "created": not exists, "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest()}


def _host_path_to_container(policy: dict[str, Any], value: str) -> str:
    workspace = Path(policy["workspace"])
    host = PureWindowsPath(value) if re.match(r"[A-Za-z]:[\\/]", value) else Path(value)
    try:
        relative = Path(os.path.abspath(str(host))).relative_to(workspace)
    except ValueError:
        try:
            relative = Path(_norm(host)).relative_to(_norm(workspace))
        except ValueError as exc:
            raise ToolPolicyError("unsupported_check", "Check argument names a path outside the workspace") from exc
    if not relative.parts:
        return "/workspace"
    rel = resolve_admitted(policy, relative.as_posix())[0]
    if not _under(rel, policy["scope"] + policy["inputs"]):
        # A parent of admitted entries is visible in the snapshot; anything else is not.
        if not any(root.startswith(rel + "/") for root in policy["scope"] + policy["inputs"]):
            raise ToolPolicyError("unsupported_check", "Check argument names an unadmitted path")
    return "/workspace/" + rel


def _map_argument(policy: dict[str, Any], arg: str) -> str:
    if re.match(r"[A-Za-z]:[\\/]", arg) or arg.startswith(("/", "\\\\")):
        return _host_path_to_container(policy, arg)
    if "=" in arg and arg.startswith("-"):
        flag, value = arg.split("=", 1)
        if re.match(r"[A-Za-z]:[\\/]", value) or value.startswith(("/", "\\\\")):
            return flag + "=" + _host_path_to_container(policy, value)
    if any(part == ".." for part in re.split(r"[\\/]", arg)):
        raise ToolPolicyError("unsupported_check", "Check argument traverses outside the workspace")
    return arg.replace("\\", "/") if re.search(r"\.py$|[\\/]", arg) and not arg.startswith("-") else arg


def convert_check_argv(policy: dict[str, Any], argv: list[str]) -> list[str]:
    """Declared host Python check argv to the container argv. Anything else is refused."""
    if not argv or not isinstance(argv[0], str):
        raise ToolPolicyError("unsupported_check", "Check argv is empty")
    executable = argv[0]
    name = (PureWindowsPath(executable) if "\\" in executable or ":" in executable
            else PurePosixPath(executable)).name.casefold()
    if not _PYTHON_EXECUTABLE.fullmatch(name):
        raise ToolPolicyError("unsupported_check", "Only declared Python unittest/pytest/script checks are supported")
    out = ["python"]
    index = 1
    while index < len(argv) and argv[index] in _INTERPRETER_FLAGS:
        if argv[index] not in out:
            out.append(argv[index])
        index += 1
    if "-B" not in out:
        out.insert(1, "-B")
    if index >= len(argv):
        raise ToolPolicyError("unsupported_check", "Interactive Python is not a check")
    head = argv[index]
    if head == "-m" and index + 1 < len(argv) and argv[index + 1] in _CHECK_MODULES:
        module = argv[index + 1]
        out += ["-m", module] + (["-p", "no:cacheprovider"] if module == "pytest" else [])
        index += 2
    elif head == "-c" and index + 1 < len(argv):
        if _HOST_ABSOLUTE.search(argv[index + 1]):
            raise ToolPolicyError("unsupported_check", "Inline check code names a host path")
        out += ["-c", argv[index + 1]]
        index += 2
    elif head.endswith(".py") and not head.startswith("-"):
        mapped = _map_argument(policy, head)
        relative = mapped[len("/workspace/"):] if mapped.startswith("/workspace/") else mapped
        resolve_admitted(policy, relative)
        if not _under(relative, policy["scope"] + policy["inputs"]):
            raise ToolPolicyError("unsupported_check", "Check script is not admitted")
        out.append(relative)
        index += 1
    else:
        raise ToolPolicyError("unsupported_check", "Only python -m unittest/pytest, -c or an admitted script are supported")
    out += [_map_argument(policy, arg) for arg in argv[index:]]
    return out


def docker_argv(policy: dict[str, Any], snapshot: Path, container_argv: list[str], name: str) -> list[str]:
    """The complete Docker argv. No shell, model-controlled flag, env, socket, home or device."""
    limits = policy["limits"]
    source = str(Path(snapshot).resolve())
    if any(char in source for char in ',"\n\r') or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", name):
        raise ToolPolicyError("sandbox", "Snapshot mount or container name is not representable safely")
    if not Path(source).is_relative_to(Path(policy["run_dir"]) / SNAPSHOT_DIRNAME):
        raise ToolPolicyError("sandbox", "Only the run's fresh check snapshot can be mounted")
    return [DOCKER_EXECUTABLE, "--config", str(Path(policy["run_dir"]) / "mistral-docker-config"),
            "--host", "npipe:////./pipe/dockerDesktopLinuxEngine",
            "run", "--rm", "--name", name, "--pull=never", "--network=none",
            "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--user=65534:65534", f"--pids-limit={limits['container_pids']}",
            f"--memory={limits['container_memory_mb']}m",
            f"--memory-swap={limits['container_memory_mb']}m",
            f"--cpus={limits['container_cpus']}", "--tmpfs", _TMPFS,
            "--mount", f"type=bind,source={source},target=/workspace,readonly",
            "--workdir", "/workspace", DOCKER_IMAGE, *container_argv]


def materialize_snapshot(policy: dict[str, Any], destination: Path) -> dict[str, int]:
    """Fresh copy of admitted regular files only: no .git, .env, link or unadmitted file."""
    limits = policy["limits"]
    destination = Path(destination)
    destination.mkdir(parents=False, exist_ok=False)
    files, truncated = _admitted_files(policy, "", limits["snapshot_max_files"])
    if truncated:
        raise ToolPolicyError("bounds", "Check snapshot exceeds the file-count bound")
    if not {r["path"] for r in policy.get("read_sources", [])}.issubset({rel for rel, _ in files}):
        raise ToolPolicyError("missing", "Required verified snapshot source missing")
    total = 0
    for relative, source in files:
        _, source, exists = resolve_admitted(policy, relative)
        if not exists:
            raise ToolPolicyError("missing", "Snapshot input disappeared")
        info = os.lstat(source)
        if _is_link(source, info) or not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
            raise ToolPolicyError("link", "Snapshot input changed identity")
        total += info.st_size
        if total > limits["snapshot_max_bytes"]:
            raise ToolPolicyError("bounds", "Check snapshot exceeds the byte bound")
        data = (_verified_source(policy, relative, source) if any(r["path"] == relative for r in policy.get("read_sources", []))
                else _read_bytes(source, info, limits["snapshot_max_bytes"]))
        target = destination.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(data)
    return {"files": len(files), "bytes": total}


def _docker_env() -> dict[str, str]:
    keep = {"systemroot", "windir", "comspec", "path", "pathext", "temp", "tmp", "userprofile",
            "localappdata", "appdata", "programdata", "programfiles"}
    return {key: value for key, value in os.environ.items() if key.casefold() in keep}


def _bounded_output(raw: bytes, limit: int) -> tuple[str, bool]:
    if len(raw) <= limit:
        return raw.decode("utf-8", errors="ignore"), False
    half = limit // 2
    head = raw[:half].decode("utf-8", errors="ignore")
    tail = raw[-half:].decode("utf-8", errors="ignore")
    return head + tail, True


def _run_bounded(argv: list[str], *, timeout: int, output_limit: int) -> subprocess.CompletedProcess:
    """Drain Docker output continuously with bounded memory and no console window."""
    child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, env=_docker_env(),
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    retained = bytearray()
    total = 0
    def drain() -> None:
        nonlocal total
        assert child.stdout is not None
        while block := child.stdout.read(8192):
            total += len(block)
            retained.extend(block)
            if len(retained) > output_limit:
                del retained[output_limit // 2:len(retained) - output_limit // 2]
    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    try:
        child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=10)
        raise
    finally:
        reader.join(timeout=10)
        if not reader.is_alive() and child.stdout is not None:
            child.stdout.close()
    if reader.is_alive():
        raise ToolPolicyError("sandbox", "Docker output stream did not close")
    # One sentinel byte conveys truncation; retained bytes remain bounded.
    raw = bytes(retained) + (b" " if total > output_limit else b"")
    return subprocess.CompletedProcess(argv, child.returncode, stdout=raw)


def run_check(policy: dict[str, Any], check_id: Any, *, state: RunState | None = None,
              runner: Any = None) -> dict[str, Any]:
    if policy["mode"] != "implement":
        raise ToolPolicyError("mode", "Checks are unavailable in read-only modes")
    checks = policy["checks"]
    if type(check_id) is not int or not 0 <= check_id < len(checks):
        raise ToolPolicyError("args", "Unknown declared check id")
    limits = policy["limits"]
    container_argv = convert_check_argv(policy, checks[check_id])
    if state is not None:
        if state.check_runs + 1 > limits["max_check_runs"]:
            raise ToolPolicyError("budget", "Per-run check budget exhausted")
        state.check_runs += 1
        sequence = state.check_runs
    else:
        sequence = 1
    if not Path(DOCKER_EXECUTABLE).is_file():
        raise ToolPolicyError("sandbox_unavailable", "Pinned Docker sandbox is unavailable; no host fallback")
    root = Path(policy["run_dir"]) / SNAPSHOT_DIRNAME
    root.mkdir(exist_ok=True)
    _check_directory_chain(root)
    config_dir = Path(policy["run_dir"]) / "mistral-docker-config"
    config_dir.mkdir(exist_ok=True)
    _check_directory_chain(config_dir)
    if any(config_dir.iterdir()):
        raise ToolPolicyError("sandbox", "Docker config must be empty; ambient credentials/contexts refused")
    snapshot = root / f"{sequence:03d}-{os.getpid()}"
    materialize_snapshot(policy, snapshot)
    token = hashlib.sha256(policy["run_dir"].encode("utf-8")).hexdigest()[:12]
    name = f"fleet-mistral-check-{token}-{sequence:03d}-{os.getpid()}"
    argv = docker_argv(policy, snapshot, container_argv, name)
    started = time.monotonic()
    try:
        child = (runner or _run_bounded)(argv, timeout=limits["check_timeout_s"],
                                        output_limit=limits["max_check_output_bytes"])
    except subprocess.TimeoutExpired:
        try:
            # Stops only this run's own --rm container; never removes anything.
            (runner or _run_bounded)([*argv[:5], "kill", name], timeout=30,
                                     output_limit=4096)
        except (OSError, subprocess.SubprocessError):
            pass
        return {"check_id": check_id, "status": "timeout", "exit_code": None, "output": "",
                "output_truncated": False, "duration_ms": int((time.monotonic() - started) * 1000)}
    except OSError as exc:
        raise ToolPolicyError("sandbox_unavailable", "Pinned Docker sandbox could not start") from exc
    output, truncated = _bounded_output(child.stdout or b"", limits["max_check_output_bytes"])
    code = child.returncode
    status = "passed" if code == 0 else "sandbox_error" if code in {125, 126, 127} else "failed"
    return {"check_id": check_id, "status": status, "exit_code": code, "output": output,
            "output_truncated": truncated, "duration_ms": int((time.monotonic() - started) * 1000)}


def check_catalog(policy: dict[str, Any]) -> str:
    rows = []
    for index, argv in enumerate(policy["checks"]):
        try:
            convert_check_argv(policy, argv)
            support = "supported"
        except ToolPolicyError as exc:
            support = f"unsupported: {exc}"
        rows.append(f"- {index}: {' '.join(argv)[:300]} ({support})")
    return "\n".join(rows) if rows else "- none declared"


def _metadata(tool: str, result: dict[str, Any]) -> dict[str, Any]:
    if tool == TOOL_READ:
        return {"bytes": len(result["content"].encode("utf-8")), "lines": result["line_count"],
                "truncated": result["truncated"]}
    if tool == TOOL_LIST:
        return {"entries": len(result["entries"]), "matches": len(result["matches"]),
                "truncated": result["truncated"]}
    if tool == TOOL_WRITE:
        return {"bytes": result["bytes"], "created": result["created"]}
    if tool in CHROME_TOOLS:
        return {"status": result.get("status"), "response_bytes": len(json.dumps(result, ensure_ascii=False).encode("utf-8"))}
    return {"check_id": result["check_id"], "status": result["status"], "exit_code": result["exit_code"],
            "output_bytes": len(result["output"].encode("utf-8")), "duration_ms": result["duration_ms"]}


def invoke(state: RunState, tool: str, function: Any, *args: Any) -> dict[str, Any]:
    """Budget, dispatch and content-free telemetry for one model tool call."""
    with state.lock:
        return _invoke_locked(state, tool, function, *args)


def _invoke_locked(state: RunState, tool: str, function: Any, *args: Any) -> dict[str, Any]:
    try:
        state.begin(tool)
        result = function(state.policy, *args, state=state)
    except ToolPolicyError as exc:
        state.record(tool, "refused", exc.code, {})
        raise
    except Exception as exc:
        state.record(tool, "error", type(exc).__name__, {})
        raise ToolPolicyError("error", f"{tool} failed ({type(exc).__name__}); details withheld") from exc
    state.record(tool, "ok", None, _metadata(tool, result))
    return result


def read_telemetry(run_dir: Path | str) -> dict[str, Any] | None:
    """Bounded summary of the run's own telemetry. Never file content or paths."""
    path = Path(run_dir).resolve() / TELEMETRY_FILENAME
    try:
        info = os.lstat(path)
        if _is_link(path, info) or info.st_nlink > 1 or not stat.S_ISREG(info.st_mode) or info.st_size > MAX_TELEMETRY_BYTES:
            return None
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError):
        return None
    summary: dict[str, Any] = {"calls": 0, "by_tool": {}, "outcomes": {}, "checks": [], "written_bytes": 0,
                               "budget_exceeded": False}
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            return None
        if not isinstance(row, dict) or row.get("tool") not in set(MAKER_TOOLS) | set(CHROME_TOOLS):
            return None
        summary["calls"] += 1
        summary["by_tool"][row["tool"]] = summary["by_tool"].get(row["tool"], 0) + 1
        outcome = str(row.get("outcome"))
        if row.get("code") == "budget":
            summary["budget_exceeded"] = True
        summary["outcomes"][outcome] = summary["outcomes"].get(outcome, 0) + 1
        result = row.get("result") if isinstance(row.get("result"), dict) else {}
        if row["tool"] == TOOL_CHECK and outcome == "ok":
            summary["checks"].append({key: result.get(key) for key in ("check_id", "status", "exit_code")})
        if row["tool"] == TOOL_WRITE and outcome == "ok" and type(result.get("bytes")) is int:
            summary["written_bytes"] += result["bytes"]
    return summary


# ---------------------------------------------------------------------------
# Native Vibe tool classes: defined only inside the pinned native interpreter.
# ---------------------------------------------------------------------------

_STATE: RunState | None = None


def activate(policy_path: Path | str, expected_sha256: str | None = None) -> dict[str, Any]:
    """Bind this process to one trusted policy and a fresh telemetry file."""
    global _STATE
    if _STATE is not None:
        raise ToolPolicyError("policy", "Native tool policy is already active")
    policy = load_policy(policy_path, expected_sha256=expected_sha256)
    telemetry = Path(policy["run_dir"]) / TELEMETRY_FILENAME
    with telemetry.open("x", encoding="utf-8"):
        pass
    if _NATIVE:
        FleetRunCheck.description = _CHECK_DESCRIPTION + "\nDeclared checks:\n" + check_catalog(policy)
    _STATE = RunState(policy, telemetry=telemetry)
    if _STATE.chrome_cli is not None:
        import atexit
        atexit.register(_STATE.chrome_cli.close)
    return policy


def _admitted(name: str) -> bool:
    return _STATE is not None and name in _STATE.policy["tools"]


try:
    from pydantic import BaseModel, ConfigDict, Field
    from vibe.core.tools.base import BaseTool, BaseToolConfig, BaseToolState, InvokeContext, ToolError
    from vibe.core.tools.models import ToolPermission
    from vibe.core.tools.permissions import PermissionContext
    from vibe.core.types import ToolStreamEvent
    _NATIVE = True
except ImportError:
    _NATIVE = False

_CHECK_DESCRIPTION = (
    "Run one Fleet-declared check by integer check_id inside the pinned offline Docker "
    "sandbox over a fresh read-only snapshot of the admitted files. No other command can run."
)

if _NATIVE:
    def _native_call(tool: str, function: Any, *args: Any) -> dict[str, Any]:
        if _STATE is None:
            raise ToolError("Fleet tool policy is not active")
        try:
            return invoke(_STATE, tool, function, *args)
        except ToolPolicyError as exc:
            raise ToolError(f"{tool} refused: {exc}") from None

    def _permission(name: str) -> PermissionContext:
        if not _admitted(name):
            return PermissionContext(permission=ToolPermission.NEVER, reason="Tool not admitted for this run")
        return PermissionContext(permission=ToolPermission.ALWAYS)

    class FleetToolConfig(BaseToolConfig):
        permission: ToolPermission = ToolPermission.ALWAYS

    class FleetToolState(BaseToolState):
        pass

    class ChromeBaseArgs(BaseModel):
        model_config = ConfigDict(extra="forbid")
        request_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._:-]{1,128}$", max_length=128)

    class FleetChromeAttachmentArgs(ChromeBaseArgs):
        pass
    class FleetChromeOpenArgs(ChromeBaseArgs):
        url: str = Field(min_length=1, max_length=2048)
    class FleetChromeReadArgs(ChromeBaseArgs):
        tab_id: int = Field(ge=1, le=2147483647)
    class FleetChromeClickArgs(ChromeBaseArgs):
        tab_id: int = Field(ge=1, le=2147483647)
        ref: str = Field(pattern=r"^ref_[a-f0-9]{24}$", max_length=32)
    class FleetChromeTypeArgs(ChromeBaseArgs):
        tab_id: int = Field(ge=1, le=2147483647)
        ref: str = Field(pattern=r"^ref_[a-f0-9]{24}$", max_length=32)
        text: str = Field(min_length=1, max_length=1000)
    class FleetChromeWaitArgs(ChromeBaseArgs):
        model_config = ConfigDict(extra="forbid", json_schema_extra={"anyOf": [{"required": ["text"]}, {"required": ["role"]}, {"required": ["name"]}]})
        tab_id: int = Field(ge=1, le=2147483647)
        text: str | None = Field(default=None, min_length=1, max_length=200)
        role: str | None = Field(default=None, min_length=1, max_length=200)
        name: str | None = Field(default=None, min_length=1, max_length=200)
    class FleetChromeCloseArgs(ChromeBaseArgs):
        tab_id: int = Field(ge=1, le=2147483647)

    class FleetChromeSavedLoginArgs(ChromeBaseArgs):
        tab_id: int = Field(ge=1, le=2147483647)

    class FleetChromeResult(BaseModel):
        status: str
        response: str

    def _chrome_call(tool: str, args: Any) -> FleetChromeResult:
        if _STATE is None or _STATE.chrome_cli is None:
            raise ToolError("Managed Chrome is not assigned to this run")
        arguments = args.model_dump(exclude_none=True)
        try:
            _STATE.begin(tool)
            result = _STATE.chrome_cli.call(tool, arguments)
            raw = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
            if len(raw.encode("utf-8")) > MAX_CHROME_RESULT_BYTES:
                _STATE.record(tool, "error", "result_bound", {})
                raise ToolError("Managed Chrome response exceeded the fixed bound")
            status = result.get("status")
            outcome = "ok" if status == "ok" else "unknown" if status == "unknown" else "refused"
            _STATE.record(tool, outcome, None if status == "ok" else status, {})
            return FleetChromeResult(status=status, response=raw)
        except ToolPolicyError as exc:
            _STATE.record(tool, "refused", exc.code, {})
            raise ToolError(f"{tool} refused: {exc}") from None
        except Exception as exc:
            if isinstance(exc, ToolError): raise
            _STATE.record(tool, "error", type(exc).__name__, {})
            raise ToolError("Managed Chrome result is uncertain; do not replay the operation") from None

    class _ChromeToolMixin:
        @classmethod
        def is_available(cls, config: Any = None) -> bool: return _admitted(cls.get_name())
        def resolve_permission(self, args: Any) -> PermissionContext: return _permission(self.get_name())
        async def run(self, args: Any, ctx: InvokeContext | None = None) -> AsyncGenerator[ToolStreamEvent | FleetChromeResult, None]:
            yield await asyncio.to_thread(_chrome_call, self.get_name(), args)

    class FleetChromeAttachment(_ChromeToolMixin, BaseTool[FleetChromeAttachmentArgs, FleetChromeResult, FleetToolConfig, FleetToolState]):
        description: ClassVar[str] = "Read attachment status for the fixed managed Chrome session."

        async def run(self, args: FleetChromeAttachmentArgs, ctx: InvokeContext | None = None) -> AsyncGenerator[ToolStreamEvent | FleetChromeResult, None]:
            async for result in _ChromeToolMixin.run(self, args, ctx):
                yield result
    class FleetChromeOpen(_ChromeToolMixin, BaseTool[FleetChromeOpenArgs, FleetChromeResult, FleetToolConfig, FleetToolState]):
        description: ClassVar[str] = "Open an inactive owned tab within the owner-approved origins."

        async def run(self, args: FleetChromeOpenArgs, ctx: InvokeContext | None = None) -> AsyncGenerator[ToolStreamEvent | FleetChromeResult, None]:
            async for result in _ChromeToolMixin.run(self, args, ctx):
                yield result
    class FleetChromeRead(_ChromeToolMixin, BaseTool[FleetChromeReadArgs, FleetChromeResult, FleetToolConfig, FleetToolState]):
        description: ClassVar[str] = "Read a bounded redacted excerpt and fresh opaque element references."

        async def run(self, args: FleetChromeReadArgs, ctx: InvokeContext | None = None) -> AsyncGenerator[ToolStreamEvent | FleetChromeResult, None]:
            async for result in _ChromeToolMixin.run(self, args, ctx):
                yield result
    class FleetChromeClick(_ChromeToolMixin, BaseTool[FleetChromeClickArgs, FleetChromeResult, FleetToolConfig, FleetToolState]):
        description: ClassVar[str] = "Click only a fresh opaque reference admitted by the managed relay."

        async def run(self, args: FleetChromeClickArgs, ctx: InvokeContext | None = None) -> AsyncGenerator[ToolStreamEvent | FleetChromeResult, None]:
            async for result in _ChromeToolMixin.run(self, args, ctx):
                yield result
    class FleetChromeType(_ChromeToolMixin, BaseTool[FleetChromeTypeArgs, FleetChromeResult, FleetToolConfig, FleetToolState]):
        description: ClassVar[str] = "Type ordinary text only; the managed relay denies credentials and MFA."

        async def run(self, args: FleetChromeTypeArgs, ctx: InvokeContext | None = None) -> AsyncGenerator[ToolStreamEvent | FleetChromeResult, None]:
            async for result in _ChromeToolMixin.run(self, args, ctx):
                yield result
    class FleetChromeWait(_ChromeToolMixin, BaseTool[FleetChromeWaitArgs, FleetChromeResult, FleetToolConfig, FleetToolState]):
        description: ClassVar[str] = "Wait for a bounded text, role or name condition on an owned tab."

        async def run(self, args: FleetChromeWaitArgs, ctx: InvokeContext | None = None) -> AsyncGenerator[ToolStreamEvent | FleetChromeResult, None]:
            async for result in _ChromeToolMixin.run(self, args, ctx):
                yield result
    class FleetChromeClose(_ChromeToolMixin, BaseTool[FleetChromeCloseArgs, FleetChromeResult, FleetToolConfig, FleetToolState]):
        description: ClassVar[str] = "Close only a tab owned by this managed Chrome session."

        async def run(self, args: FleetChromeCloseArgs, ctx: InvokeContext | None = None) -> AsyncGenerator[ToolStreamEvent | FleetChromeResult, None]:
            async for result in _ChromeToolMixin.run(self, args, ctx):
                yield result

    class FleetChromeSavedLogin(_ChromeToolMixin, BaseTool[FleetChromeSavedLoginArgs, FleetChromeResult, FleetToolConfig, FleetToolState]):
        description: ClassVar[str] = "Submit an owner-bound login using Chrome saved autofill; never read or supply credentials. One attempt per owned tab."

        async def run(self, args: FleetChromeSavedLoginArgs, ctx: InvokeContext | None = None) -> AsyncGenerator[ToolStreamEvent | FleetChromeResult, None]:
            async for result in _ChromeToolMixin.run(self, args, ctx):
                yield result

    class FleetReadArgs(BaseModel):
        model_config = ConfigDict(extra="forbid")
        path: str = Field(description="Workspace-relative path of an admitted file.", max_length=MAX_RELATIVE_PATH)
        offset: int = Field(default=0, ge=0, le=10_000_000, description="Zero-based first line to return.")
        limit: int | None = Field(default=None, ge=1, le=100_000, description="Maximum number of lines.")

    class FleetReadResult(BaseModel):
        path: str
        content: str
        start_line: int
        line_count: int
        total_lines: int
        truncated: bool

    class FleetListArgs(BaseModel):
        model_config = ConfigDict(extra="forbid")
        path: str = Field(default="", max_length=MAX_RELATIVE_PATH,
                          description="Admitted workspace-relative file or directory; empty lists every admitted entry.")
        query: str | None = Field(default=None, max_length=200,
                                  description="Optional literal text to search for in admitted UTF-8 files.")

    class FleetListResult(BaseModel):
        entries: list[str]
        matches: list[str]
        truncated: bool

    class FleetWriteArgs(BaseModel):
        model_config = ConfigDict(extra="forbid")
        path: str = Field(description="Workspace-relative file inside the writable scope.", max_length=MAX_RELATIVE_PATH)
        content: str = Field(description="Complete new UTF-8 text content of the file.")
        overwrite: bool = Field(default=False, description="Must be true to update an existing file.")

    class FleetWriteResult(BaseModel):
        path: str
        created: bool
        bytes: int
        sha256: str

    class FleetCheckArgs(BaseModel):
        model_config = ConfigDict(extra="forbid")
        check_id: int = Field(ge=0, lt=MAX_CHECKS, description="Integer id of a Fleet-declared check.")

    class FleetCheckResult(BaseModel):
        check_id: int
        status: str
        exit_code: int | None
        output: str
        output_truncated: bool
        duration_ms: int

    class FleetReadFile(BaseTool[FleetReadArgs, FleetReadResult, FleetToolConfig, FleetToolState]):
        description: ClassVar[str] = (
            "Read UTF-8 text from an admitted workspace file (Fleet scope or inputs only), "
            "optionally a line window. Paths are workspace-relative.")

        @classmethod
        def is_available(cls, config: Any = None) -> bool:
            return _admitted(cls.get_name())

        def resolve_permission(self, args: Any) -> PermissionContext:
            return _permission(self.get_name())

        async def run(self, args: FleetReadArgs, ctx: InvokeContext | None = None
                      ) -> AsyncGenerator[ToolStreamEvent | FleetReadResult, None]:
            yield FleetReadResult(**_native_call(TOOL_READ, read_text, args.path, args.offset, args.limit))

    class FleetListFiles(BaseTool[FleetListArgs, FleetListResult, FleetToolConfig, FleetToolState]):
        description: ClassVar[str] = (
            "List admitted workspace files, or search them for literal text. Only Fleet scope "
            "and inputs are visible; protected and linked paths are never listed.")

        @classmethod
        def is_available(cls, config: Any = None) -> bool:
            return _admitted(cls.get_name())

        def resolve_permission(self, args: Any) -> PermissionContext:
            return _permission(self.get_name())

        async def run(self, args: FleetListArgs, ctx: InvokeContext | None = None
                      ) -> AsyncGenerator[ToolStreamEvent | FleetListResult, None]:
            yield FleetListResult(**_native_call(TOOL_LIST, list_files, args.path, args.query))

    class FleetWriteFile(BaseTool[FleetWriteArgs, FleetWriteResult, FleetToolConfig, FleetToolState]):
        description: ClassVar[str] = (
            "Create or update one UTF-8 text file inside the Fleet writable scope with its complete "
            "content. Updating an existing file requires overwrite=true. No delete or rename.")

        @classmethod
        def is_available(cls, config: Any = None) -> bool:
            return _admitted(cls.get_name())

        def resolve_permission(self, args: Any) -> PermissionContext:
            return _permission(self.get_name())

        async def run(self, args: FleetWriteArgs, ctx: InvokeContext | None = None
                      ) -> AsyncGenerator[ToolStreamEvent | FleetWriteResult, None]:
            yield FleetWriteResult(**_native_call(TOOL_WRITE, write_text, args.path, args.content, args.overwrite))

    class FleetRunCheck(BaseTool[FleetCheckArgs, FleetCheckResult, FleetToolConfig, FleetToolState]):
        description: ClassVar[str] = _CHECK_DESCRIPTION

        @classmethod
        def is_available(cls, config: Any = None) -> bool:
            return _admitted(cls.get_name())

        def resolve_permission(self, args: Any) -> PermissionContext:
            return _permission(self.get_name())

        async def run(self, args: FleetCheckArgs, ctx: InvokeContext | None = None
                      ) -> AsyncGenerator[ToolStreamEvent | FleetCheckResult, None]:
            result = await asyncio.to_thread(_native_call, TOOL_CHECK, run_check, args.check_id)
            yield FleetCheckResult(**result)

    NATIVE_TOOL_CLASSES = (FleetReadFile, FleetListFiles, FleetWriteFile, FleetRunCheck, FleetChromeAttachment, FleetChromeOpen, FleetChromeRead, FleetChromeClick, FleetChromeType, FleetChromeWait, FleetChromeClose, FleetChromeSavedLogin)
else:
    NATIVE_TOOL_CLASSES = ()
