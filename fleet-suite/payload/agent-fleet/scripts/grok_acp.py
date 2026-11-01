from __future__ import annotations

import fnmatch
import hashlib
import importlib
import json
import os
import queue
import re
import secrets
import shutil
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Iterable


TRANSPORT_ID = "native-cli-acp-stdio"
MAX_ACP_LINE_CHARS = 8 * 1024 * 1024
MAX_SAFE_TEXT_CHARS = 2 * 1024 * 1024
QUOTA_EXHAUSTED_EXIT_CODE = 86
GROK_QUOTA_EXHAUSTION_MARKERS = (
    "usage balance exhausted",
    "http 402",
    "402 payment required",
)
GROK_TURN_LIMITS = {"low": 64, "medium": 128, "high": 256}
GROK_TOOL_LIMITS = {"low": 64, "medium": 128, "high": 256}
GROK_ABSOLUTE_TOOL_LIMIT = 512
GROK_ABSOLUTE_REPAIR_PROMPT_LIMIT = 6
RUN_ID_PATTERN = re.compile(r"^run-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{10}$")

COMPATIBILITY_ENV_KEYS = tuple(
    f"GROK_{vendor}_{feature}_ENABLED"
    for vendor in ("CURSOR", "CLAUDE", "CODEX")
    for feature in ("SKILLS", "RULES", "AGENTS", "MCPS", "HOOKS", "SESSIONS")
)

CONTROLLED_ENV = {
    "GROK_MANAGED_MCPS_ENABLED": "false",
    "GROK_MANAGED_MCP_GATEWAY_TOOLS_ENABLED": "false",
    "GROK_MCP_RECURSIVE_CONFIG_WATCH": "false",
    "GROK_MEMORY": "0",
    "GROK_SUBAGENTS": "0",
    "GROK_WEB_FETCH": "0",
}

SENSITIVE_PROVIDER_ENV = {
    "GROK_API_KEY",
    "XAI_API_KEY",
}

GROK_SHELL_ALLOW_RULES = (
    "Bash(Get-Location*)",
    "Bash(Get-ChildItem*)",
    "Bash(Get-Item*)",
    "Bash(Get-Content*)",
    "Bash(Test-Path*)",
    "Bash(Resolve-Path*)",
    "Bash(Select-String*)",
    "Bash(Select-Object*)",
    "Bash(Sort-Object*)",
    "Bash(Measure-Object*)",
    "Bash(Get-FileHash*)",
    "Bash(pwd*)",
    "Bash(ls*)",
    "Bash(dir*)",
    "Bash(cat*)",
    "Bash(git status*)",
    "Bash(git diff*)",
    "Bash(git show*)",
    "Bash(git log*)",
    "Bash(git rev-parse*)",
    "Bash(git ls-files*)",
    "Bash(git -C project status*)",
    "Bash(git -C project diff*)",
    "Bash(git -C project show*)",
    "Bash(git -C project log*)",
    "Bash(git -C project rev-parse*)",
    "Bash(git -C project ls-files*)",
    "Bash(rg *)",
    "Bash(python -m *)",
    "Bash(python -I project/*)",
    "Bash(python project/*)",
    "Bash(py -m *)",
    "Bash(py -I project/*)",
    "Bash(py project/*)",
    "Bash(pytest *)",
    "Bash(node --test*)",
    "Bash(npm test*)",
    "Bash(npm run test*)",
    "Bash(npm run lint*)",
    "Bash(npm run build*)",
    "Bash(pnpm test*)",
    "Bash(pnpm run test*)",
    "Bash(pnpm run lint*)",
    "Bash(pnpm run build*)",
    "Bash(yarn test*)",
    "Bash(dotnet test*)",
    "Bash(cargo test*)",
    "Bash(go test*)",
    "Bash(pwsh -NoLogo -NoProfile -File project/.fleet/*)",
    "Bash(pwsh -NoLogo -NoProfile -File project/*)",
    "Bash(powershell -NoLogo -NoProfile -File project/.fleet/*)",
    "Bash(powershell -NoLogo -NoProfile -File project/*)",
)

GROK_SHELL_DENY_RULES = (
    "Bash(git push*)",
    "Bash(git commit*)",
    "Bash(git merge*)",
    "Bash(git rebase*)",
    "Bash(git reset*)",
    "Bash(git checkout*)",
    "Bash(git switch*)",
    "Bash(git clean*)",
    "Bash(rm *)",
    "Bash(rmdir *)",
    "Bash(del *)",
    "Bash(Remove-Item *)",
    "Bash(shutdown *)",
    "Bash(format *)",
)

_SHELL_CONTROL_PATTERN = re.compile(r"(?:&&|\|\||[;`\r\n<>]|\$\(|&)")

# Coordination CLIs the contractor may invoke, resolved at check time from the
# deployment (fleet.office_command_path / fleet.coord_command_path). Measured
# 2026-09-20: the coordination preamble told the contractor to run Office, but the
# shell allowlist carried no Office rule, so `session/request_permission` was denied
# and the contractor could never see who else works in the repo or warn them.
#
# `wait` blocks the process until timeout - a one-shot contractor whose parent waits
# on it would deadlock and pay for two model runs - and `mcp` starts a server, so
# neither is listed. `project-`, `journal-` and `work-` are frozen layers. This is an
# allowlist: any subcommand not listed here, including future ones, stays denied.
OFFICE_SHELL_SUBCOMMANDS = frozenset({"who", "read", "channels", "presence", "send"})
COORD_SHELL_SUBCOMMANDS = frozenset({"claim", "release", "renew", "ls", "who"})
COORDINATION_SHELL_BLOCKED_PREFIXES = ("project-", "journal-", "work-")
# The only coordination option that takes a separate value token, so its value must
# not be mistaken for the subcommand.
COORDINATION_HARNESS_OPTION = "--harness"
# A quoted install path can contain spaces (for example a directory name with a space),
# so tokens are split on quotes as well as on whitespace.
_COORDINATION_TOKEN_PATTERN = re.compile(r"""[^\s"']+|"[^"]*"|'[^']*'""")


def _coordination_command_paths() -> tuple[tuple[str, frozenset[str]], ...]:
    """Deployed coordination commands, as normalized (path, subcommands) pairs.

    The install root is machine-dependent, so the gate reads it from the fleet
    module at check time instead of carrying a hardcoded copy. A missing module or
    getter yields an empty tuple, which is the pre-change behaviour.
    """
    module = sys.modules.get("fleet")
    if module is None:
        try:
            module = importlib.import_module("fleet")
        except Exception:
            return ()
    resolved: list[tuple[str, frozenset[str]]] = []
    for getter_name, subcommands in (
        ("office_command_path", OFFICE_SHELL_SUBCOMMANDS),
        ("coord_command_path", COORD_SHELL_SUBCOMMANDS),
    ):
        getter = getattr(module, getter_name, None)
        if not callable(getter):
            continue
        try:
            path = getter()
        except Exception:
            continue
        if isinstance(path, (str, os.PathLike)) and str(path).strip():
            resolved.append((os.path.normcase(str(path)), subcommands))
    return tuple(resolved)


def _coordination_shell_allowed(
    segment: str, paths: tuple[tuple[str, frozenset[str]], ...]
) -> bool:
    """Allow an exact deployed coordination CLI with one listed subcommand.

    The leading token must be the deployed command itself, so no other .cmd, .bat
    or lookalike passes. The subcommand is the first token after the command that
    is neither an option nor the value of one, matching the
    `<office> --harness <agent> <subcommand>` shape the coordination preamble
    renders: the agent id after `--harness` is a value, not a subcommand, and
    reading it as one denied every preamble line (measured 2026-09-20).
    """
    tokens = [
        token[1:-1]
        if len(token) > 1 and token[0] == token[-1] and token[0] in "\"'"
        else token
        for token in _COORDINATION_TOKEN_PATTERN.findall(segment)
    ]
    if len(tokens) < 2 or not tokens[0]:
        return False
    invoked = os.path.normcase(tokens[0])
    for path, subcommands in paths:
        if invoked != path:
            continue
        expect_option_value = False
        for token in tokens[1:]:
            if expect_option_value:
                expect_option_value = False
                continue
            if token.startswith("-"):
                expect_option_value = token == COORDINATION_HARNESS_OPTION
                continue
            return (
                not token.startswith(COORDINATION_SHELL_BLOCKED_PREFIXES)
                and token in subcommands
            )
        return False
    return False


def _bash_rule_matches(rule: str, command: str) -> bool:
    if not rule.startswith("Bash(") or not rule.endswith(")"):
        return False
    pattern = rule[5:-1]
    candidate = command.lstrip()
    if pattern.endswith(":*"):
        return candidate.startswith(pattern[:-2])
    if any(character in pattern for character in "*?["):
        return fnmatch.fnmatchcase(candidate, pattern)
    return candidate.startswith(pattern)


def _bounded_shell_permission_allowed(command: Any) -> bool:
    if not isinstance(command, str) or not command.strip() or len(command) > 32768:
        return False
    candidate = command.lstrip()
    if _SHELL_CONTROL_PATTERN.search(candidate):
        return False
    normalized = candidate.replace("\\", "/")
    if "project/../" in normalized or normalized.endswith("project/.."):
        return False
    segments = [segment.strip() for segment in candidate.split("|")]
    if not all(segments):
        return False
    coordination_paths = _coordination_command_paths()
    for segment in segments:
        if any(_bash_rule_matches(rule, segment) for rule in GROK_SHELL_DENY_RULES):
            return False
        # Existing rejections above still win. A coordination command is only
        # considered once it survived them, so this cannot widen anything that was
        # already denied - in particular shell metacharacters, the deny rules and
        # `project/../` never reach this branch.
        if _coordination_shell_allowed(segment, coordination_paths):
            continue
        if not any(
            _bash_rule_matches(rule, segment) for rule in GROK_SHELL_ALLOW_RULES
        ):
            return False
    return True


# Native write variants observed from Grok CLI ACP (2026-09-29, run-20260929T112438Z-4365fea609):
# SearchReplace edits an existing file; Write creates or replaces a whole file and carries
# rawInput keys {variant, file_path, content}. Only observed variants are listed; anything else
# (including a future Edit/Delete/Move) stays refused until its native schema is observed.
PROJECT_WRITE_VARIANTS = frozenset({"SearchReplace", "Write"})
_WINDOWS_RESERVED_NAME = re.compile(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?$", re.IGNORECASE)


def _drive_absolute_to_project_segments(
    normalized: str, drive_match: re.Match[str], workspace_root: Path
) -> list[str] | None:
    """Map a Windows drive-absolute path lexically inside workspace_root to project segments.

    Returns None for drive-relative, UNC/device/extended-length, different drive, root alone,
    sibling-prefix lookalikes, or any unsafe segment. No resolve() of the caller path.
    Case-insensitive Windows containment with either separator."""
    drive = drive_match.group(1)
    rest = drive_match.group(2)
    # Native Windows containment guard: this whole mapping is a Windows-only lexical
    # rewrite, so on a non-Windows host (or under a test that simulates one) we refuse
    # the path before doing any Windows-specific parsing.
    if os.name != "nt":
        return None
    # Drive-relative ("D:foo" with no separator) and root-only ("D:" or "D:/") are refused.
    if not rest or not rest.startswith("/"):
        return None
    # "//..." after the drive letter would mean a UNC-style or device/extended-length path.
    if rest.startswith("//"):
        return None
    # Validate every raw segment BEFORE handing the path to PureWindowsPath, so the
    # parsing layer never sees an empty, dot, dotdot, ADS-colon, trailing-dot-or-space
    # or reserved-name segment.
    for segment in rest[1:].split("/"):
        if (
            not segment
            or segment in {".", ".."}
            or ":" in segment
            or segment.endswith((".", " "))
            or _WINDOWS_RESERVED_NAME.match(segment)
        ):
            return None
    # The trusted workspace root must itself be a drive-absolute Windows path; refuse
    # UNC (`\\host\share`), device (`\\?\…`), extended-length, drive-relative or
    # relative roots before computing containment.
    try:
        root_str = os.fspath(workspace_root)
    except (TypeError, ValueError):
        return None
    root_lower = root_str.replace("\\", "/").lower()
    if root_lower.endswith("/"):
        root_lower = root_lower[:-1]
    if (
        len(root_lower) < 3
        or root_lower[1] != ":"
        or root_lower[0] != drive.lower()
    ):
        return None
    # Containment via PureWindowsPath.relative_to replaces the previous string
    # startswith check; sibling-prefix lookalikes (e.g. `project_other` vs.
    # `project`) raise ValueError because the normalized paths differ.
    try:
        tail = PureWindowsPath(normalized).relative_to(PureWindowsPath(os.fspath(workspace_root)))
    except (ValueError, OSError, TypeError):
        return None
    # Root alone (e.g. the workspace path itself) maps to an empty tail and is not a write target.
    if not tail.parts:
        return None
    return ["project", *tail.parts]


def _resolve_project_segments_with_source(
    raw_path: str,
    workspace_root: Path,
    *,
    proxy_project_root: Path | None = None,
) -> tuple[list[str], bool] | None:
    """Map a raw path to project-relative segments and report whether the alias was used.

    Returns (segments, came_from_alias) or None. Accepts `project/<...>` (relative) and
    Windows drive-absolute paths lexically inside workspace_root (native Windows only,
    case-insensitive, either separator). When `proxy_project_root` is given, a
    drive-absolute path that fails the workspace_root check is retried against the
    exact per-session alias <proxy_root>/project (the junction the broker itself
    created to the assigned workspace) - same lexical rules as the workspace_root
    pass. Everything else returns None: POSIX absolute, UNC, device/extended-length,
    drive-relative, sibling-prefix, traversal, ADS colons, trailing dots/spaces,
    reserved names, controls, oversize."""
    if not isinstance(raw_path, str) or not raw_path or len(raw_path) > 4096:
        return None
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw_path):
        return None
    normalized = raw_path.replace("\\", "/")
    drive_match = re.match(r"^([A-Za-z]):(.*)$", normalized)
    if drive_match:
        mapped = _drive_absolute_to_project_segments(normalized, drive_match, workspace_root)
        if mapped is not None:
            return mapped, False
        if proxy_project_root is not None:
            mapped = _drive_absolute_to_project_segments(normalized, drive_match, proxy_project_root)
            if mapped is not None:
                return mapped, True
        return None
    if normalized.startswith("//"):
        return None
    if normalized.startswith("/"):
        return None
    segments = normalized.split("/")
    if not segments or segments[0] != "project":
        return None
    return segments, False


def _resolve_project_segments(
    raw_path: str,
    workspace_root: Path,
    *,
    proxy_project_root: Path | None = None,
) -> list[str] | None:
    """Return project-relative segments (with "project" prefix) or None.

    Accepts `project/<...>` (relative) and Windows drive-absolute paths lexically inside
    workspace_root (native Windows only, case-insensitive, either separator). When
    `proxy_project_root` is given, a drive-absolute path that fails the workspace_root
    check is retried against the exact per-session alias <proxy_root>/project (the
    junction the broker itself created to the assigned workspace). Everything else -
    POSIX absolute, UNC, device/extended-length, drive-relative, sibling-prefix,
    traversal, ADS colons, trailing dots/spaces, reserved names, controls, oversize -
    returns None."""
    result = _resolve_project_segments_with_source(
        raw_path, workspace_root, proxy_project_root=proxy_project_root
    )
    if result is None:
        return None
    return result[0]


def _alias_trusted(proxy_project_root: Path, root: Path) -> bool:
    """Trust the broker-created alias only when it is a link to the assigned workspace.

    Required: proxy_project_root is an existing directory; it is the broker
    junction/link (never a plain directory); its resolved target equals the assigned
    physical workspace_root, with `os.path.samefile` as an additional equality check
    when resolve() disagrees on case. No drive, prefix or string containment: the
    alias must point to exactly the assigned physical workspace."""
    try:
        alias_resolved = proxy_project_root.expanduser().resolve(strict=True)
    except OSError:
        return False
    if not alias_resolved.is_dir() or not _is_filesystem_link(proxy_project_root):
        return False
    if alias_resolved == root:
        return True
    try:
        return os.path.samefile(str(alias_resolved), str(root))
    except OSError:
        return False


def _project_local_write_allowed(
    raw_input: Any,
    *,
    workspace_root: Path,
    proxy_project_root: Path | None = None,
) -> bool:
    if not isinstance(raw_input, dict) or raw_input.get("variant") not in PROJECT_WRITE_VARIANTS:
        return False
    if raw_input.get("variant") == "Write" and not isinstance(raw_input.get("content"), str):
        return False
    raw_path = raw_input.get("file_path")
    if not isinstance(raw_path, str) or not raw_path or len(raw_path) > 4096:
        return False
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw_path):
        return False
    # Map the raw path to project-relative segments; this accepts the existing
    # `project/<...>` shape, a fully qualified Windows drive-absolute path that is
    # lexically inside the exact assigned workspace_root, and - when
    # `proxy_project_root` is supplied - the real absolute alias path
    # <proxy_root>/project/<...> that the grok CLI emits from its cwd.
    resolution = _resolve_project_segments_with_source(
        raw_path, workspace_root, proxy_project_root=proxy_project_root
    )
    if resolution is None:
        return False
    segments, came_from_alias = resolution
    # Structural containment: at least one trailing segment beyond the workspace root is
    # required for both the relative (`project`) form and any drive-absolute rewrite.
    # Bare `project` or the absolute workspace root is never a write target.
    if len(segments) < 2:
        return False
    # Repository metadata (project/.git, file or directory) is never a write target; ordinary
    # project files such as .gitignore, .gitattributes and .github/ stay editable. Under project/.fleet (Fleet evidence area, a
    # legitimate target) only script types that the shell allowlist executes via
    # `pwsh -File project/.fleet/*` are refused.
    if len(segments) > 1 and segments[1].lower() == ".git":
        return False
    if (
        len(segments) > 2
        and segments[1].lower() == ".fleet"
        and segments[-1].lower().endswith((".ps1", ".psm1", ".psd1", ".cmd", ".bat"))
    ):
        return False
    if any(_WINDOWS_RESERVED_NAME.match(segment) for segment in segments[1:]):
        return False
    if any(
        not segment
        or segment in {".", ".."}
        or ":" in segment
        or segment.endswith((".", " "))
        for segment in segments
    ):
        return False
    try:
        root = workspace_root.expanduser().resolve(strict=True)
    except OSError:
        return False
    if not root.is_dir() or _is_filesystem_link(root):
        return False
    if came_from_alias:
        # The alias is trusted only when it is the broker's own junction/link for this
        # session that resolves to the assigned physical workspace. Every other
        # condition - sibling sessions, plain directories, foreign links, traversal
        # beyond the alias - is refused here and the existing guards below never run.
        if proxy_project_root is None or not _alias_trusted(proxy_project_root, root):
            return False
    candidate = root
    try:
        for segment in segments[1:]:
            candidate = candidate / segment
            try:
                info = os.lstat(candidate)
            except FileNotFoundError:
                continue  # a new component: nothing to resolve
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                return False  # symlink or any reparse point (junction)
            resolved = candidate.resolve(strict=True)
            if resolved != root and root not in resolved.parents:
                return False
            # A file with more than one hard link shares its data with another name that may
            # lie outside project/: never write through it.
            if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                return False
    except (OSError, ValueError):
        return False
    # A whole-file Write may create a new file or replace an existing regular file, never a
    # directory or any other object type.
    try:
        if raw_input.get("variant") == "Write" and candidate.exists() and not candidate.is_file():
            return False
    except (OSError, ValueError):
        return False
    return True


def _permission_allow_once_option(
    message: dict[str, Any],
    *,
    allow_writes: bool = False,
    workspace_root: Path | None = None,
    allowed_mcp_tools: tuple[str, ...] = (),
    mcp_session_id: str | None = None,
    proxy_project_root: Path | None = None,
) -> str | None:
    params = message.get("params")
    if not isinstance(params, dict):
        return None
    tool_call = params.get("toolCall")
    if not isinstance(tool_call, dict):
        return None
    raw_input = tool_call.get("rawInput")
    if not isinstance(raw_input, dict):
        return None
    variant = raw_input.get("variant")
    assigned_mcp = (
        variant == "UseTool"
        and bool(allowed_mcp_tools)
        and mcp_session_id is not None
        and params.get("sessionId") == mcp_session_id
        and set(raw_input) == {"variant", "tool_name", "tool_input"}
        and raw_input.get("tool_name") in allowed_mcp_tools
        and isinstance(raw_input.get("tool_input"), dict)
        and len(_canonical_json(raw_input["tool_input"])) <= 65536
    )
    allowed = (
        variant == "Bash"
        and _bounded_shell_permission_allowed(raw_input.get("command"))
    ) or (
        allow_writes
        and workspace_root is not None
        and _project_local_write_allowed(
            raw_input,
            workspace_root=workspace_root,
            proxy_project_root=proxy_project_root,
        )
    ) or assigned_mcp
    if not allowed:
        return None
    options = params.get("options")
    if not isinstance(options, list):
        return None
    for option in options:
        if not isinstance(option, dict) or option.get("kind") != "allow_once":
            continue
        option_id = option.get("optionId")
        if isinstance(option_id, str) and option_id:
            return option_id
    return None


_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,40}$")
PERMISSION_DENIAL_RECORD_LIMIT = 20


def _safe_token(value: Any) -> str | None:
    return value if isinstance(value, str) and _SAFE_TOKEN.match(value) else None


def _path_shape(
    raw_path: Any,
    *,
    workspace_root: Path | None = None,
    proxy_project_root: Path | None = None,
) -> tuple[str, str | None]:
    """Shape of a requested path plus, only for clean project-relative paths, the path itself.

    Never returns an absolute path, a drive, a UNC host or anything outside `project/`.
    When `workspace_root` is provided, a Windows drive-absolute path lexically inside it
    is rewritten as the equivalent `project/<...>` form so the diagnostic exposes only the
    clean project-relative segments. When `workspace_root` mapping fails AND
    `proxy_project_root` is supplied, the per-session alias path <proxy_root>/project
    is mapped to the same redacted `project/<...>` form. Drive-absolute paths outside
    the workspace and alias, all UNC, POSIX-absolute, drive-relative and
    device/extended-length forms stay unexposed."""
    if not isinstance(raw_path, str) or not raw_path:
        return "none", None
    if len(raw_path) > 4096:
        return "oversized", None
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw_path):
        return "control_characters", None
    normalized = raw_path.replace("\\", "/")
    if normalized.startswith("//"):
        return "unc", None
    drive_match = re.match(r"^([A-Za-z]):(.*)$", normalized)
    if drive_match:
        if workspace_root is not None:
            mapped = _drive_absolute_to_project_segments(normalized, drive_match, workspace_root)
            if mapped is not None and mapped[0] == "project":
                # Defensive: only ever expose the canonical project/... rewrite.
                relative = "/".join(mapped)
                if all(
                    s
                    and s not in {".", ".."}
                    and ":" not in s
                    and not s.endswith((".", " "))
                    and not _WINDOWS_RESERVED_NAME.match(s)
                    for s in mapped
                ) and len(relative) <= 200:
                    return "project_relative", relative
        if proxy_project_root is not None:
            mapped = _drive_absolute_to_project_segments(normalized, drive_match, proxy_project_root)
            if mapped is not None and mapped[0] == "project":
                # Defensive: only ever expose the canonical project/... rewrite.
                relative = "/".join(mapped)
                if all(
                    s
                    and s not in {".", ".."}
                    and ":" not in s
                    and not s.endswith((".", " "))
                    and not _WINDOWS_RESERVED_NAME.match(s)
                    for s in mapped
                ) and len(relative) <= 200:
                    return "project_relative", relative
        return "drive_absolute", None
    if normalized.startswith("/"):
        return "posix_absolute", None
    segments = normalized.split("/")
    if any(segment in {".", ".."} for segment in segments):
        return "traversal", None
    if segments[0] != "project":
        return "relative_outside_project", None
    if any(not s or ":" in s or s.endswith((".", " ")) for s in segments) or len(normalized) > 200:
        return "project_relative_unsafe", None
    return "project_relative", normalized


def classify_permission_denial(
    message: dict[str, Any],
    *,
    allow_writes: bool,
    workspace_root: Path | None,
    deny_all_tools: bool,
    proxy_project_root: Path | None = None,
) -> dict[str, Any]:
    """Redacted description of a denied session/request_permission (diagnostics only).

    Carries names and shapes, never file contents, commands, environment or full payloads."""
    record: dict[str, Any] = {"reason": "unknown"}
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    tool_call = params.get("toolCall") if isinstance(params.get("toolCall"), dict) else {}
    raw_input = tool_call.get("rawInput")
    record["tool_kind"] = _safe_token(tool_call.get("kind"))
    options = params.get("options") if isinstance(params.get("options"), list) else []
    record["option_kinds"] = sorted({k for k in (_safe_token(o.get("kind")) for o in options[:20] if isinstance(o, dict)) if k})
    if not isinstance(raw_input, dict):
        record["reason"] = "raw_input_missing"
        return record
    variant = raw_input.get("variant")
    record["variant"] = _safe_token(variant)
    record["raw_input_keys"] = sorted(k for k in (_safe_token(key) for key in list(raw_input)[:20]) if k)
    path_value = next((raw_input.get(k) for k in ("file_path", "path", "filePath", "target") if isinstance(raw_input.get(k), str)), None)
    shape, relative = _path_shape(
        path_value, workspace_root=workspace_root, proxy_project_root=proxy_project_root
    )
    record["path_shape"] = shape
    if relative is not None:
        record["project_relative_path"] = relative
    if deny_all_tools:
        record["reason"] = "deny_all_tools_mode"
    elif variant == "Bash":
        record["reason"] = "shell_command_not_allowed"
    elif not allow_writes:
        record["reason"] = "writes_not_allowed_in_mode"
    elif variant not in PROJECT_WRITE_VARIANTS:
        record["reason"] = "write_variant_not_allowed"
    elif variant == "Write" and not isinstance(raw_input.get("content"), str):
        record["reason"] = "write_content_missing"
    elif shape != "project_relative":
        record["reason"] = "path_" + shape
    elif workspace_root is None or not _project_local_write_allowed(
        raw_input, workspace_root=workspace_root, proxy_project_root=proxy_project_root
    ):
        record["reason"] = "path_protected_resolves_outside_or_link"
    elif "allow_once" not in record["option_kinds"]:
        record["reason"] = "no_allow_once_option"
    return record

SYSTEM_PROMPT = (
    "You are an isolated Agent Fleet coding contractor. Obey only the supplied "
    "delegated task. The workspace is mounted at the relative path project/. "
    "Use only the tools advertised by the active agent profile: read_file, "
    "grep_search, list_dir, and the bounded Bash tool; implement mode may "
    "additionally use search_replace. Bash runs under dontAsk with explicit "
    "command allow rules and a strict sandbox. Invoke commands from the proxy "
    "root against project/ paths instead of changing directory. In review or "
    "advisory mode, use Bash only for read-only inspection and verification. In "
    "implement mode, repository-local tests and verification scripts are also "
    "permitted, but edit files through search_replace. Run repository Python "
    "scripts directly as `python -I project/<script> [args]` or "
    "`python project/<script> [args]`; never probe with `python -c`. If a command is outside "
    "the allow rules, adapt verification and disclose the limitation instead of "
    "requesting permission. The project/ directory is the only project workspace. "
    "Do not discover or follow ambient skills, rules, hooks, plugins, MCP servers, "
    "memory, or subagents. Never access credentials or files outside the project "
    "workspace. Do not commit, push, deploy, or alter remotes. Return exactly one "
    "terminal JSON handoff matching the supplied schema."
)
AVAILABILITY_SYSTEM_PROMPT = "Reply OK only."


class GrokAcpError(RuntimeError):
    def __init__(self, message: str, *, exit_code: int = 70) -> None:
        super().__init__(message)
        self.exit_code = exit_code


class _WindowsJob:
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

    def __init__(self) -> None:
        if os.name != "nt":
            raise GrokAcpError("Windows job containment is unavailable")

        import ctypes
        from ctypes import wintypes

        class _BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class _ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimitInformation),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise GrokAcpError(
                f"Cannot create Grok ACP Windows job: {ctypes.get_last_error()}"
            )
        information = _ExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = (
            self.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        if not kernel32.SetInformationJobObject(
            handle,
            self.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            raise GrokAcpError(f"Cannot configure Grok ACP Windows job: {error}")
        self._ctypes = ctypes
        self._wintypes = wintypes
        self._kernel32 = kernel32
        self._handle = handle

    def assign(self, process: subprocess.Popen[str]) -> None:
        if self._handle is None:
            raise GrokAcpError("Grok ACP Windows job is already closed")
        process_handle = getattr(process, "_handle", None)
        if process_handle is None or not self._kernel32.AssignProcessToJobObject(
            self._handle, self._wintypes.HANDLE(int(process_handle))
        ):
            error = self._ctypes.get_last_error()
            raise GrokAcpError(f"Cannot contain Grok ACP process in Windows job: {error}")

    def close(self) -> None:
        if self._handle is None:
            return
        handle = self._handle
        self._handle = None
        if not self._kernel32.CloseHandle(handle):
            raise GrokAcpError(
                f"Cannot close Grok ACP Windows job: {self._ctypes.get_last_error()}"
            )


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_filesystem_link(path: Path) -> bool:
    try:
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0))
    except OSError:
        attributes = 0
    reparse_point = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    is_junction = getattr(path, "is_junction", None)
    return bool(
        path.is_symlink()
        or attributes & reparse_point
        or (is_junction and is_junction())
    )


def _has_link_component(path: Path) -> bool:
    absolute = Path(os.path.abspath(path))
    return any(_is_filesystem_link(candidate) for candidate in (absolute, *absolute.parents))


def _validated_tool_limit(value: Any) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= GROK_ABSOLUTE_TOOL_LIMIT
    ):
        raise GrokAcpError(
            f"Grok ACP tool limit must be an integer from 1 to {GROK_ABSOLUTE_TOOL_LIMIT}"
        )
    return value


def _copy_tree_without_links(source: Path, target: Path) -> None:
    if _is_filesystem_link(source) or not source.is_dir():
        raise GrokAcpError(f"Unsafe Grok ACP snapshot source: {source}")
    target.mkdir(parents=True, exist_ok=False)
    for item in sorted(source.iterdir(), key=lambda candidate: candidate.name.casefold()):
        if _is_filesystem_link(item):
            raise GrokAcpError(f"Linked file refused in Grok ACP snapshot: {item}")
        destination = target / item.name
        if item.is_dir():
            _copy_tree_without_links(item, destination)
        elif item.is_file():
            shutil.copy2(item, destination)


def _create_directory_link(source: Path, link: Path) -> None:
    try:
        os.symlink(source, link, target_is_directory=True)
        return
    except OSError as symlink_error:
        if os.name != "nt":
            raise GrokAcpError("Cannot create the isolated Grok project link") from symlink_error
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(source)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        check=False,
    )
    if completed.returncode != 0 or not _is_filesystem_link(link):
        raise GrokAcpError("Cannot create the isolated Grok project junction")


def _remove_directory_link(link: Path) -> None:
    if not link.exists() and not _is_filesystem_link(link):
        return
    if not _is_filesystem_link(link):
        raise GrokAcpError(f"Refusing to remove unexpected Grok project path: {link}")
    if link.is_symlink():
        link.unlink()
    else:
        os.rmdir(link)


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
    else:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _close_process_streams(process: subprocess.Popen[str]) -> None:
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None and not stream.closed:
            stream.close()


def _remove_tree_with_retry(path: Path) -> None:
    """Remove a runtime tree after Windows releases terminated-job handles."""
    transient_windows_errors = {5, 32, 145}
    attempts = 20 if os.name == "nt" else 1
    for attempt in range(attempts):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            winerror = getattr(exc, "winerror", None)
            if attempt + 1 >= attempts or winerror not in transient_windows_errors:
                raise
            time.sleep(0.05)


def _windows_job_command(
    command: list[str], *, home: Path, env: dict[str, str]
) -> tuple[list[str], dict[str, str], Path, str]:
    launcher = Path(__file__).with_name("grok_acp_job_launcher.py").resolve(strict=True)
    if _is_filesystem_link(launcher) or not launcher.is_file():
        raise GrokAcpError("Unsafe Grok ACP Windows job launcher")
    gate = home / ".job-ready"
    if gate.exists() or _is_filesystem_link(gate):
        raise GrokAcpError("Unsafe Grok ACP Windows job gate")
    token = secrets.token_hex(32)
    child_env = dict(env)
    isolated_home_str = str(home)
    for key in list(child_env):
        if key.upper() in {"HOME", "USERPROFILE"}:
            child_env.pop(key, None)
    child_env["HOME"] = isolated_home_str
    child_env["USERPROFILE"] = isolated_home_str
    child_env["AGENT_FLEET_GROK_JOB_GATE"] = str(gate)
    child_env["AGENT_FLEET_GROK_JOB_TOKEN"] = token
    return [sys.executable, "-B", str(launcher), "--", *command], child_env, gate, token


def quota_exhaustion_detected(*texts: Any) -> bool:
    """True when native CLI output reports the HTTP 402 balance stop."""
    blob = "\n".join(str(text) for text in texts if text).casefold()
    return any(marker in blob for marker in GROK_QUOTA_EXHAUSTION_MARKERS)


def quota_exhaustion_error(detail: str | None = None) -> str:
    # The provider detail is intentionally dropped: raw exception text,
    # response bodies, headers and credentials must never reach the bounded
    # diagnostic or persisted evidence. Callers keep the optional parameter
    # so existing call sites stay source-compatible.
    _ = detail
    return (
        "Grok quota exhausted: the native CLI reported 'Grok Build usage "
        "balance exhausted' (HTTP 402) before prompt execution; zero tokens "
        "were generated. Renew or top up the Grok Build subscription, then "
        "re-run `fleet.py doctor --deep` before routing to Grok again."
    )


def _suppressed_provider_stderr(lines: list[str]) -> str:
    raw = "".join(lines)
    if not raw:
        return ""
    return (
        "[Grok ACP provider stderr suppressed: "
        f"chars={len(raw)}, sha256={_sha256_text(raw)}]"
    )


def default_proxy_base() -> Path:
    configured = os.environ.get("AGENT_FLEET_GROK_ACP_PROXY_BASE")
    if configured:
        return Path(configured).expanduser().resolve()
    program_data = os.environ.get("PROGRAMDATA")
    if program_data:
        return (Path(program_data) / "AgentFleet" / "grok-acp").resolve()
    return (Path.home() / ".agents" / "grok-acp-proxy").resolve()


def planned_proxy_root(run_id: str, *, proxy_base: Path | None = None) -> Path:
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise GrokAcpError("Malformed run ID for Grok ACP proxy")
    return (proxy_base or default_proxy_base()).expanduser().resolve() / run_id


def planned_prompt_workspace() -> Path:
    return Path("project")


def planned_prompt_knowledge() -> Path:
    return Path("knowledge-snapshot")


def profile_path(run_dir: Path) -> Path:
    return run_dir / "grok-acp-agent.md"


def safe_home_path(run_dir: Path) -> Path:
    return run_dir / "grok-acp-home"


def _agent_profile(mode: str, chrome_assignment: dict[str, Any] | None = None) -> str:
    if mode not in {"review", "advisory", "implement", "availability"}:
        raise GrokAcpError(f"Unsupported Grok ACP mode: {mode}")
    if mode == "availability":
        tools: list[str] = []
    else:
        tools = ["read_file", "grep_search", "list_dir", "bash"]
        if mode == "implement":
            tools.append("search_replace")
    chrome_tools = ()
    if chrome_assignment is not None:
        import fleet_chrome_integration as chrome
        chrome.validate_fleet_assignment(chrome_assignment, "grok")
        if chrome_assignment["mode"] != mode:
            raise GrokAcpError("Managed Chrome mode does not match Grok mode")
        chrome_tools = tuple(f"fleet-chrome-relay__{name}" for name in chrome.mcp_tool_names(chrome_assignment))
        tools.extend(chrome_tools)
        # Native Grok exposes MCP via these two built-in catalog tools.
        # Exact server attestation and MCPTool allow rules still restrict calls.
        tools.extend(("search_tool", "use_tool"))
    disallowed = [
        "web_search",
        "web_fetch",
        "todo_write",
        "task",
        "kill_task",
        "get_task_output",
        "memory_search",
        "memory_get",
        "search_tool",
        "use_tool",
        "lsp",
    ]
    if mode == "availability":
        disallowed = sorted(
            set(disallowed) | {"read_file", "grep_search", "list_dir", "bash", "search_replace"}
        )
    elif chrome_assignment is not None:
        disallowed = [name for name in disallowed if name not in {"search_tool", "use_tool"}]
    frontmatter = [
        "---",
        "name: agent-fleet-isolated-contractor",
        "description: Isolated bounded contractor owned by Agent Fleet.",
        "permission_mode: dontAsk",
        "tools:",
        *[f"  - {tool}" for tool in tools],
        "disallowedTools:",
        *[f"  - {tool}" for tool in disallowed],
        "---",
    ]
    system_prompt = AVAILABILITY_SYSTEM_PROMPT if mode == "availability" else SYSTEM_PROMPT
    if chrome_tools:
        system_prompt += " The fixed fleet-chrome-relay server is explicitly assigned, not ambient. Discover its exact qualified names with search_tool, then invoke use_tool with tool_name and inline tool_input. Assigned names: " + ", ".join(chrome_tools) + ". Never use shell, files, argument files or environment to discover browser tools."
    return "\n".join(frontmatter) + "\n" + system_prompt + "\n"


def materialize_profile(run_dir: Path, *, mode: str, chrome_assignment: dict[str, Any] | None = None) -> Path:
    path = profile_path(run_dir)
    if path.exists():
        raise GrokAcpError(f"Grok ACP profile already exists: {path}")
    path.write_text(_agent_profile(mode, chrome_assignment), encoding="utf-8", newline="\n")
    return path


def _tool_names(mode: str) -> list[str]:
    if mode == "availability":
        return []
    tools = ["read_file", "grep_search", "list_dir", "bash"]
    if mode == "implement":
        tools.append("search_replace")
    return tools


def build_command(
    executable: str,
    *,
    run_dir: Path,
    run_id: str,
    mode: str,
    model: str,
    reasoning_effort: str,
    complexity: str,
    handoff_schema: dict[str, Any],
    max_model_tool_calls: int | None,
    proxy_base: Path | None = None,
    executable_args: Iterable[str] = (),
    chrome_assignment: dict[str, Any] | None = None,
) -> list[str]:
    if complexity not in GROK_TURN_LIMITS:
        raise GrokAcpError(f"Unsupported Grok ACP complexity: {complexity}")
    proxy_root = planned_proxy_root(run_id, proxy_base=proxy_base)
    tool_limit = _validated_tool_limit(
        max_model_tool_calls or GROK_TOOL_LIMITS[complexity]
    )
    tools = _tool_names(mode)
    availability = mode == "availability"
    chrome_tools: tuple[str, ...] = ()
    if chrome_assignment is not None:
        try:
            import fleet_chrome_integration as chrome
            chrome.validate_fleet_assignment(chrome_assignment, "grok")
            if chrome_assignment["mode"] != mode:
                raise GrokAcpError("Managed Chrome mode does not match Grok mode")
            chrome_tools = chrome.mcp_tool_names(chrome_assignment)
            tools = (*tools, "search_tool", "use_tool", *(f"fleet-chrome-relay__{name}" for name in chrome_tools))
        except (ImportError, ValueError) as exc:
            raise GrokAcpError("Invalid managed Chrome assignment") from exc
    disallowed_tools = (
        "read_file,grep_search,list_dir,bash,search_replace,"
        "web_search,web_fetch,todo_write,task,kill_task,get_task_output,"
        "memory_search,memory_get,search_tool,use_tool,lsp"
        if availability
        else "web_search,web_fetch,todo_write,task,kill_task,get_task_output,"
        "memory_search,memory_get,search_tool,use_tool,lsp"
    )
    if chrome_assignment is not None:
        disallowed_tools = ",".join(name for name in disallowed_tools.split(",") if name not in {"search_tool", "use_tool"})
    command = [
        executable,
        *list(executable_args),
        "--cwd",
        str(proxy_root),
        "--permission-mode",
        "dontAsk",
        "--sandbox",
        "strict",
        "--max-turns",
        "1" if availability else str(GROK_TURN_LIMITS[complexity]),
        "--no-subagents",
        "--disable-web-search",
        "--verbatim",
        "--tools",
        ",".join(tools),
        "--disallowed-tools",
        disallowed_tools,
        *([] if chrome_assignment is not None else ["--deny", "MCPTool(*)"]),
        "--deny",
        "WebFetch(*)",
        "--deny",
        "Read(../**)",
        "--deny",
        "Read(~/**)",
        "--system-prompt-override",
        AVAILABILITY_SYSTEM_PROMPT if availability else SYSTEM_PROMPT + (
            " The explicitly assigned fleet-chrome-relay MCP server is not ambient. "
            "Discover its exact qualified names with search_tool, then invoke use_tool with tool_name and inline tool_input. Assigned names: " + ", ".join(f"fleet-chrome-relay__{name}" for name in chrome_tools) +
            ". Do not use files, argument files, shells, environment or other servers to discover browser tools."
            if chrome_tools else ""),
        "--json-schema",
        _canonical_json(handoff_schema),
    ]
    if not availability:
        command.extend(
            [
                "--allow",
                "Read(project/**)",
                "--allow",
                "Grep(project/**)",
            ]
        )
        for rule in GROK_SHELL_ALLOW_RULES:
            command.extend(["--allow", rule])
    for rule in GROK_SHELL_DENY_RULES:
        command.extend(["--deny", rule])
    for tool_name in chrome_tools:
        command.extend(["--allow", f"MCPTool(fleet-chrome-relay__{tool_name})"])
    if mode == "implement":
        command.extend(
            [
                "--allow",
                "Edit(project/**)",
                "--allow",
                "Write(project/**)",
                "--deny",
                "Edit(../**)",
                "--deny",
                "Write(../**)",
                "--deny",
                "Edit(~/**)",
                "--deny",
                "Write(~/**)",
            ]
        )
    else:
        command.extend(["--deny", "Edit", "--deny", "Write"])
    command.extend(
        [
            "agent",
            "--model",
            model,
            "--reasoning-effort",
            reasoning_effort,
            "--no-leader",
            "--agent-profile",
            str(profile_path(run_dir)),
            "stdio",
        ]
    )
    return command


def build_environment(
    base_env: dict[str, str], *, run_dir: Path, auth_path: Path | None = None
) -> dict[str, str]:
    env = dict(base_env)
    raw_auth_env = next(
        (v for k, v in env.items() if k.upper() == "GROK_AUTH_PATH"), None
    )
    auth_candidate = Path(
        auth_path
        if auth_path is not None
        else (raw_auth_env if raw_auth_env is not None else Path.home() / ".grok" / "auth.json")
    ).expanduser()
    if _has_link_component(auth_candidate):
        raise GrokAcpError(
            "The existing Grok subscription authentication file is unavailable or unsafe"
        )
    try:
        resolved_auth = auth_candidate.resolve(strict=True)
    except OSError as exc:
        raise GrokAcpError(
            "The existing Grok subscription authentication file is unavailable or unsafe"
        ) from exc
    if not resolved_auth.is_file() or _has_link_component(resolved_auth):
        raise GrokAcpError(
            "The existing Grok subscription authentication file is unavailable or unsafe"
        )
    sensitive_keys = {k.upper() for k in SENSITIVE_PROVIDER_ENV}
    blocked_keys = {"GROK_AGENT", "GROK_CONFIG", "GROK_CONFIG_PATH", "GROK_AUTH_PATH"}
    for key in list(env):
        upper_key = key.upper()
        if upper_key in sensitive_keys or upper_key in blocked_keys:
            env.pop(key, None)

    isolated_home = str(safe_home_path(run_dir))
    env["GROK_HOME"] = isolated_home
    env["GROK_AUTH_PATH"] = str(resolved_auth)

    for key in list(env):
        if key.upper() in {"HOME", "USERPROFILE"}:
            env.pop(key, None)
    env["HOME"] = isolated_home
    env["USERPROFILE"] = isolated_home

    for key in COMPATIBILITY_ENV_KEYS:
        env[key] = "false"
    env.update(CONTROLLED_ENV)
    env["NO_COLOR"] = "1"
    return env


def _extract_safe_scalar(value: Any, *, limit: int = 256) -> str | None:
    if not isinstance(value, (str, int, float, bool)):
        return None
    return str(value)[:limit]


class _AcpState:
    def __init__(self, *, max_tool_calls: int, expected_mcp_servers: tuple[str, ...] = (), expected_mcp_tool_count: int | None = None, allowed_mcp_tools: tuple[str, ...] = ()) -> None:
        self.max_tool_calls = max_tool_calls
        self.current_message_chunks: list[str] = []
        self.thought_chunk_count = 0
        self.tool_calls: dict[str, dict[str, Any]] = {}
        self.permission_requests_approved = 0
        self.permission_requests_denied = 0
        self.prompt_permission_requests_denied = 0
        self.reverse_requests_denied = 0
        self.permission_denials: list[dict[str, Any]] = []
        self.ambient_mcp_servers = 0
        self.expected_mcp_servers = tuple(expected_mcp_servers)
        self.observed_mcp_servers: tuple[str, ...] | None = None
        self.mcp_inventory_mismatch = False
        self.mcp_catalog_observed = False
        self.mcp_catalog_shapes: list[dict[str, Any]] = []
        self.protocol_shapes: list[dict[str, Any]] = []
        self.mcp_status_shapes: list[dict[str, Any]] = []
        self.expected_mcp_tool_count = expected_mcp_tool_count
        self.allowed_mcp_tools = tuple(allowed_mcp_tools)
        self.expected_session_id: str | None = None
        self.session_mcp_reports: dict[str, dict[str, Any]] = {}
        self.latest_usage: dict[str, Any] | None = None
        self.latest_model_id: str | None = None
        self.observed_model_ids: list[str] = []

    def begin_prompt(self) -> None:
        self.current_message_chunks = []
        self.prompt_permission_requests_denied = 0

    @property
    def session_mcp_attested(self) -> bool:
        report = self.session_mcp_reports.get(self.expected_session_id or "", {})
        return (self.expected_mcp_tool_count is not None
            and report.get("total") == len(self.expected_mcp_servers)
            and report.get("connected") == len(self.expected_mcp_servers)
            and report.get("tool_count") == self.expected_mcp_tool_count
            and report.get("ready") == set(self.expected_mcp_servers))

    @property
    def tool_budget_exceeded(self) -> bool:
        return len(self.tool_calls) > self.max_tool_calls

    def _capture_usage(self, value: Any) -> None:
        if not isinstance(value, dict):
            return
        meta = value.get("_meta")
        candidates = [value, meta] if isinstance(meta, dict) else [value]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            usage = candidate.get("usage")
            if isinstance(usage, dict):
                self.latest_usage = usage
            model_id = candidate.get("modelId", candidate.get("model_id"))
            if isinstance(model_id, str) and model_id.strip():
                self.latest_model_id = model_id.strip()
                if self.latest_model_id not in self.observed_model_ids:
                    self.observed_model_ids.append(self.latest_model_id)

    def observe(self, message: dict[str, Any]) -> None:
        self._capture_usage(message)
        method = message.get("method")
        params = message.get("params")
        if len(self.protocol_shapes) < 32:
            self.protocol_shapes.append({"method": method if isinstance(method,str) else None,
                "params_keys": sorted(params)[:16] if isinstance(params,dict) else [],
                "result_keys": sorted(message["result"])[:16] if isinstance(message.get("result"),dict) else [],
                "has_error": isinstance(message.get("error"),dict)})
        if not isinstance(method, str) or not isinstance(params, dict):
            return
        if method == "_x.ai/notification" and isinstance(params.get("method"), str):
            method = str(params["method"])
            nested = params.get("params")
            params = nested if isinstance(nested, dict) else {}
        if method.startswith("_x.ai/"):
            # Both direct native events and the legacy notification envelope.
            method = method[1:]
        if method in {"x.ai/mcp/server_status", "x.ai/mcp_initialized", "x.ai/mcp/init_progress"}:
            tools = params.get("tools")
            shape = {"method": method, "expected_name": params.get("name") in self.expected_mcp_servers,
                "status": params.get("status") if params.get("status") in {"connected", "ready", "failed", "error", "disconnected", "connecting"} else "other",
                "has_reason": bool(params.get("reason")), "tools_type": type(tools).__name__,
                "tools_count": len(tools) if isinstance(tools,list) else None,
                "tools_keys": sorted(tools[0])[:16] if isinstance(tools,list) and tools and isinstance(tools[0],dict) else [],
                "mcpToolCount": params.get("mcpToolCount") if type(params.get("mcpToolCount")) is int else None,
                "connected": params.get("connected") if type(params.get("connected")) is int else None,
                "total": params.get("total") if type(params.get("total")) is int else None}
            if len(self.mcp_status_shapes) < 16:
                self.mcp_status_shapes.append(shape)
            sid = params.get("sessionId")
            if isinstance(sid, str) and 0 < len(sid) <= 256:
                if sid not in self.session_mcp_reports and len(self.session_mcp_reports) >= 16:
                    self.mcp_inventory_mismatch = True
                else:
                    report = self.session_mcp_reports.setdefault(sid, {"ready": set()})
                    if method == "x.ai/mcp/init_progress":
                        for key in ("total", "connected"):
                            if type(params.get(key)) is int:
                                report[key] = params[key]
                    elif method == "x.ai/mcp_initialized" and type(params.get("mcpToolCount")) is int:
                        report["tool_count"] = params["mcpToolCount"]
                    elif method == "x.ai/mcp/server_status":
                        name = params.get("name")
                        if not isinstance(name,str) or name not in self.expected_mcp_servers:
                            self.mcp_inventory_mismatch = True
                        elif params.get("status") == "ready":
                            report["ready"].add(name)
                        else:
                            report["ready"].discard(name)
        if method == "x.ai/mcp/servers_updated":
            servers = params.get("mcpServers", params.get("mcp_servers", params.get("servers")))
            if len(self.mcp_catalog_shapes) < 16:
                self.mcp_catalog_shapes.append({"keys": sorted(params)[:16], "servers_type": type(servers).__name__,
                    "item_keys": [sorted(item)[:16] for item in servers[:8] if isinstance(item, dict)] if isinstance(servers, list) else []})
            self.mcp_catalog_observed = True
            names = []
            if isinstance(servers, list):
                for item in servers:
                    if not isinstance(item, dict) or type(item.get("name")) is not str:
                        self.mcp_inventory_mismatch = True
                        continue
                    names.append(item["name"])
            else:
                self.mcp_inventory_mismatch = True
            self.observed_mcp_servers = tuple(names)
            self.ambient_mcp_servers = len(names)
            # An empty startup catalog is pending, never attestation of the
            # assigned server. Foreign and duplicate servers are terminal.
            self.mcp_inventory_mismatch = self.mcp_inventory_mismatch or len(set(names)) != len(names) or any(name not in self.expected_mcp_servers for name in names)
            return
        if method != "session/update":
            return
        update = params.get("update")
        if not isinstance(update, dict):
            return
        self._capture_usage(update)
        kind = str(update.get("sessionUpdate", ""))
        if kind == "agent_message_chunk":
            content = update.get("content")
            text = content.get("text") if isinstance(content, dict) else None
            if isinstance(text, str):
                current_size = sum(len(item) for item in self.current_message_chunks)
                if current_size < MAX_SAFE_TEXT_CHARS:
                    self.current_message_chunks.append(
                        text[: max(0, MAX_SAFE_TEXT_CHARS - current_size)]
                    )
        elif kind == "agent_thought_chunk":
            self.thought_chunk_count += 1
        elif kind in {"tool_call", "tool_call_update"}:
            identity = update.get("toolCallId", update.get("tool_call_id", update.get("id")))
            if not isinstance(identity, str) or not identity:
                identity = _sha256_text(_canonical_json(update))[:24]
            existing = self.tool_calls.setdefault(identity, {"id": identity})
            for source, target in (
                ("title", "title"),
                ("kind", "kind"),
                ("status", "status"),
                ("tool", "tool"),
            ):
                safe = _extract_safe_scalar(update.get(source))
                if safe is not None:
                    existing[target] = safe


class _AcpConnection:
    def __init__(
        self,
        process: subprocess.Popen[str],
        *,
        deadline: float,
        state: _AcpState,
        allow_writes: bool,
        workspace_root: Path,
        deny_all_tools: bool = False,
        proxy_project_root: Path | None = None,
    ) -> None:
        self.process = process
        self.deadline = deadline
        self.state = state
        self.allow_writes = allow_writes
        self.workspace_root = workspace_root
        self.deny_all_tools = deny_all_tools
        self.proxy_project_root = proxy_project_root
        self.messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self.stderr_lines: list[str] = []
        self.reader_error: str | None = None
        self.next_id = 1
        self.stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self.stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self.stdout_thread.start()
        self.stderr_thread.start()

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            if len(line) > MAX_ACP_LINE_CHARS:
                self.reader_error = "Grok ACP emitted an oversized JSON-RPC line"
                return
            try:
                value = json.loads(line)
            except (json.JSONDecodeError, RecursionError):
                self.reader_error = "Grok ACP emitted invalid JSON-RPC"
                return
            if not isinstance(value, dict):
                self.reader_error = "Grok ACP emitted a non-object JSON-RPC message"
                return
            self.messages.put(value)

    def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        total = 0
        for line in self.process.stderr:
            if total >= MAX_SAFE_TEXT_CHARS:
                break
            clipped = line[: MAX_SAFE_TEXT_CHARS - total]
            self.stderr_lines.append(clipped)
            total += len(clipped)

    def _send(self, message: dict[str, Any]) -> None:
        if self.process.stdin is None or self.process.poll() is not None:
            raise GrokAcpError("Grok ACP process exited before request delivery")
        self.process.stdin.write(_canonical_json(message) + "\n")
        self.process.stdin.flush()

    def _handle_reverse_request(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        method = str(message.get("method", ""))
        # `getattr` keeps existing tests that bypass __init__ (FakeConn with hand-set
        # attributes) working: they never set proxy_project_root, so it defaults to
        # None and the validator behaves exactly as on 0.9.107.
        proxy_project_root = getattr(self, "proxy_project_root", None)
        if method == "session/request_permission":
            try:
                option_id = (
                    None
                    if self.deny_all_tools
                    else _permission_allow_once_option(
                        message,
                        allow_writes=self.allow_writes,
                        workspace_root=self.workspace_root,
                        allowed_mcp_tools=(self.state.allowed_mcp_tools if not self.state.mcp_inventory_mismatch and self.state.session_mcp_attested else ()),
                        mcp_session_id=self.state.expected_session_id,
                        proxy_project_root=proxy_project_root,
                    )
                )
            except Exception:  # fail closed: an unexpected validator error is a refusal
                option_id = None
            if option_id is not None:
                self.state.permission_requests_approved += 1
                result = {
                    "outcome": {"outcome": "selected", "optionId": option_id}
                }
                self._send({"jsonrpc": "2.0", "id": request_id, "result": result})
                return
            self.state.permission_requests_denied += 1
            self.state.prompt_permission_requests_denied += 1
            if len(self.state.permission_denials) < PERMISSION_DENIAL_RECORD_LIMIT:
                try:
                    self.state.permission_denials.append(
                        classify_permission_denial(
                            message,
                            allow_writes=self.allow_writes,
                            workspace_root=self.workspace_root,
                            deny_all_tools=self.deny_all_tools,
                            proxy_project_root=proxy_project_root,
                        )
                    )
                except Exception:  # diagnostics must never change the protocol answer
                    self.state.permission_denials.append({"reason": "classifier_error"})
            result = {"outcome": {"outcome": "cancelled"}}
            self._send({"jsonrpc": "2.0", "id": request_id, "result": result})
            return
        self.state.reverse_requests_denied += 1
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32601,
                    "message": "Agent Fleet ACP client capability is disabled",
                },
            }
        )

    def _next_message(self, *, deadline: float | None = None) -> dict[str, Any]:
        while True:
            if self.state.tool_budget_exceeded:
                raise GrokAcpError("Grok ACP tool-call budget exceeded", exit_code=125)
            if self.state.mcp_inventory_mismatch or (self.state.ambient_mcp_servers and not self.state.expected_mcp_servers):
                message = ("Grok ACP MCP server discovery was not isolated" if not self.state.expected_mcp_servers
                           else "Grok MCP inventory differs from the fixed Fleet assignment")
                raise GrokAcpError(message, exit_code=77)
            if self.reader_error is not None:
                raise GrokAcpError(self.reader_error)
            remaining = min(self.deadline, deadline if deadline is not None else self.deadline) - time.monotonic()
            if remaining <= 0:
                raise GrokAcpError("Grok ACP parent timeout", exit_code=124)
            try:
                return self.messages.get(timeout=min(remaining, 0.2))
            except queue.Empty:
                if self.process.poll() is not None:
                    raise GrokAcpError(
                        f"Grok ACP process exited before response: {self.process.returncode}"
                    )

    def wait_for_mcp_inventory(self, timeout: float = 15.0) -> None:
        """Drain startup notifications without issuing a model prompt."""
        deadline = min(self.deadline, time.monotonic() + timeout)
        while not ((self.state.mcp_catalog_observed and self.state.observed_mcp_servers == self.state.expected_mcp_servers) or self.state.session_mcp_attested):
            message = self._next_message(deadline=deadline)
            if "id" in message and "method" in message:
                self._handle_reverse_request(message)
            else:
                self.state.observe(message)
        if self.state.mcp_inventory_mismatch:
            raise GrokAcpError("Grok MCP inventory differs from the fixed Fleet assignment", exit_code=77)

    def request(self, method: str, params: dict[str, Any]) -> Any:
        request_id = self.next_id
        self.next_id += 1
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        while True:
            message = self._next_message()
            if "id" in message and "method" in message:
                self._handle_reverse_request(message)
                continue
            self.state.observe(message)
            if message.get("id") != request_id:
                continue
            if isinstance(message.get("error"), dict):
                error = message["error"]
                code = _extract_safe_scalar(error.get("code")) or "unknown"
                detail = _extract_safe_scalar(error.get("message")) or "provider error"
                raise GrokAcpError(f"Grok ACP {method} failed ({code}): {detail}")
            return message.get("result", {})

    def close(self) -> tuple[int, str]:
        if self.process.stdin is not None and not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _terminate_process_tree(self.process)
        self.stdout_thread.join(timeout=5)
        self.stderr_thread.join(timeout=5)
        _close_process_streams(self.process)
        return int(self.process.returncode or 0), _suppressed_provider_stderr(
            self.stderr_lines
        )


def _safe_result_text(result: Any, chunks: list[str]) -> str:
    if isinstance(result, dict) and isinstance(result.get("text"), str):
        return str(result["text"])[:MAX_SAFE_TEXT_CHARS]
    return "".join(chunks)[:MAX_SAFE_TEXT_CHARS]


def _structured_output(result: Any) -> Any:
    if not isinstance(result, dict):
        return None
    for source in (result, result.get("_meta")):
        if not isinstance(source, dict):
            continue
        for key in ("structuredOutput", "structured_output"):
            if key in source:
                return source[key]
    return None


def _normalize_envelope(
    *,
    result: Any,
    state: _AcpState,
    session_id: str | None,
    prompt_count: int,
    attempts: list[dict[str, Any]],
    continuation_triggers: list[str],
    continuation_skip_reason: str | None,
) -> dict[str, Any]:
    result_dict = result if isinstance(result, dict) else {}
    stop_reason = str(result_dict.get("stopReason", "error"))
    envelope: dict[str, Any] = {
        "stopReason": stop_reason,
        "sessionId": session_id,
        "num_turns": prompt_count,
        "text": _safe_result_text(result, state.current_message_chunks),
        "structuredOutput": _structured_output(result),
        "usage": state.latest_usage,
        "modelId": state.latest_model_id,
        "modelIds": list(state.observed_model_ids),
        "_fleet": {
            "transport": TRANSPORT_ID,
            "prompt_count": prompt_count,
            "tool_calls": len(state.tool_calls),
            "tool_events": list(state.tool_calls.values()),
            "thought_chunks_discarded": state.thought_chunk_count,
            "permission_requests_approved": state.permission_requests_approved,
            "permission_requests_denied": state.permission_requests_denied,
            "permission_requests_denied_in_prompt": state.prompt_permission_requests_denied,
            "reverse_requests_denied": state.reverse_requests_denied,
            "permission_denials": list(state.permission_denials),
            "ambient_mcp_servers": state.ambient_mcp_servers,
            "mcp_catalog_observed": state.mcp_catalog_observed,
            "session_mcp_attested": state.session_mcp_attested,
            "windows_os_sandbox_enforced": os.name != "nt",
            "attempts": attempts,
            "continuation_triggers": continuation_triggers,
            "continuation_skip_reason": continuation_skip_reason,
        },
    }
    return envelope


def _prepare_runtime(
    *,
    run_dir: Path,
    run_id: str,
    workspace: Path,
    mode: str,
    knowledge_snapshot: Path | None,
    proxy_base: Path | None,
    retain_runtime: bool = False,
    chrome_assignment: dict[str, Any] | None = None,
) -> tuple[Path, Path]:
    proxy_root = planned_proxy_root(run_id, proxy_base=proxy_base)
    base = proxy_root.parent
    base.mkdir(parents=True, exist_ok=True)
    if _is_filesystem_link(base) or proxy_root.exists() or _is_filesystem_link(proxy_root):
        raise GrokAcpError(f"Unsafe or stale Grok ACP proxy path: {proxy_root}")
    home = safe_home_path(run_dir)
    if home.exists() or _is_filesystem_link(home):
        raise GrokAcpError(f"Unsafe or stale Grok ACP home: {home}")
    proxy_root.mkdir()
    home.mkdir()
    try:
        _create_directory_link(workspace.expanduser().resolve(), proxy_root / "project")
        if knowledge_snapshot is not None:
            _copy_tree_without_links(
                knowledge_snapshot.expanduser().resolve(), proxy_root / "knowledge-snapshot"
            )
        materialize_profile(run_dir, mode=mode, chrome_assignment=chrome_assignment)
    except Exception:
        if retain_runtime:
            raise
        try:
            _remove_directory_link(proxy_root / "project")
            if proxy_root.exists():
                shutil.rmtree(proxy_root)
            if home.exists():
                shutil.rmtree(home)
        except OSError:
            pass
        raise
    return proxy_root, home


def _cleanup_runtime(
    *, run_dir: Path, run_id: str, proxy_base: Path | None
) -> list[str]:
    errors: list[str] = []
    proxy_root = planned_proxy_root(run_id, proxy_base=proxy_base)
    home = safe_home_path(run_dir)
    try:
        _remove_directory_link(proxy_root / "project")
        if proxy_root.exists():
            if _is_filesystem_link(proxy_root) or proxy_root.parent != planned_proxy_root(
                run_id, proxy_base=proxy_base
            ).parent:
                raise GrokAcpError(f"Refusing unsafe Grok ACP proxy cleanup: {proxy_root}")
            _remove_tree_with_retry(proxy_root)
    except (OSError, GrokAcpError) as exc:
        errors.append(f"Grok ACP proxy cleanup: {exc}")
    try:
        if home.exists():
            if _is_filesystem_link(home) or home.parent.resolve() != run_dir.resolve():
                raise GrokAcpError(f"Refusing unsafe Grok ACP home cleanup: {home}")
            _remove_tree_with_retry(home)
    except (OSError, GrokAcpError) as exc:
        errors.append(f"Grok ACP home cleanup: {exc}")
    return errors


ContinuationDecider = Callable[[str], tuple[str, str] | None]


def session_mcp_servers(chrome_assignment: dict[str, Any] | None) -> list[dict[str, Any]]:
    """ACP v1 session/new stdio config, with exactly zero or one fixed server."""
    if chrome_assignment is None:
        return []
    try:
        import fleet_chrome_integration as chrome
        chrome.validate_fleet_assignment(chrome_assignment, "grok")
        entry = chrome.mcp_entry(chrome_assignment)
    except (ImportError, ValueError) as exc:
        raise GrokAcpError("Invalid managed Chrome assignment") from exc
    return [{"name": chrome.SERVER, "command": entry["command"],
             "args": list(entry["args"]), "env": []}]


def run_session(
    command: list[str],
    *,
    run_dir: Path,
    run_id: str,
    workspace: Path,
    mode: str,
    prompt: str,
    timeout: int,
    max_model_tool_calls: int,
    max_additional_prompts: int,
    env: dict[str, str],
    continuation_decider: ContinuationDecider,
    knowledge_snapshot: Path | None = None,
    proxy_base: Path | None = None,
    retain_runtime: bool = False,
    chrome_assignment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    max_model_tool_calls = _validated_tool_limit(max_model_tool_calls)
    if (
        isinstance(max_additional_prompts, bool)
        or not isinstance(max_additional_prompts, int)
        or not 0 <= max_additional_prompts <= GROK_ABSOLUTE_REPAIR_PROMPT_LIMIT
    ):
        raise GrokAcpError(
            "Grok ACP additional prompt limit must be an integer from 0 to "
            f"{GROK_ABSOLUTE_REPAIR_PROMPT_LIMIT}"
        )
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 1:
        raise GrokAcpError("Grok ACP timeout must be a positive integer")
    started = time.monotonic()
    deadline = started + timeout
    process: subprocess.Popen[str] | None = None
    windows_job: _WindowsJob | None = None
    connection: _AcpConnection | None = None
    session_id: str | None = None
    prompt_count = 0
    attempts: list[dict[str, Any]] = []
    continuation_triggers: list[str] = []
    continuation_skip_reason: str | None = None
    last_result: Any = {}
    mcp_servers = session_mcp_servers(chrome_assignment)
    expected_mcp = ("fleet-chrome-relay",) if chrome_assignment is not None else ()
    if chrome_assignment is not None:
        import fleet_chrome_integration as chrome
        allowed_mcp_tools = tuple(f"fleet-chrome-relay__{name}" for name in chrome.mcp_tool_names(chrome_assignment))
    else:
        allowed_mcp_tools = ()
    state = _AcpState(max_tool_calls=max_model_tool_calls, expected_mcp_servers=expected_mcp, allowed_mcp_tools=allowed_mcp_tools)
    exit_code = 70
    timed_out = False
    tool_budget_exceeded = False
    stderr = ""
    proxy_prepared = False
    error_message: str | None = None
    try:
        proxy_root, home = _prepare_runtime(
            run_dir=run_dir,
            run_id=run_id,
            workspace=workspace,
            mode=mode,
            knowledge_snapshot=knowledge_snapshot,
            proxy_base=proxy_base,
            retain_runtime=retain_runtime,
            chrome_assignment=chrome_assignment,
        )
        proxy_prepared = True
        launch_command = command
        launch_env = dict(env)
        isolated_home_str = str(home)
        for key in list(launch_env):
            if key.upper() in {"HOME", "USERPROFILE"}:
                launch_env.pop(key, None)
        launch_env["HOME"] = isolated_home_str
        launch_env["USERPROFILE"] = isolated_home_str
        job_gate: Path | None = None
        job_token: str | None = None
        if os.name == "nt":
            windows_job = _WindowsJob()
            launch_command, launch_env, job_gate, job_token = _windows_job_command(
                command, home=home, env=launch_env
            )
        creationflags = (int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) | int(getattr(subprocess, "CREATE_NO_WINDOW", 0))) if os.name == "nt" else 0
        process = subprocess.Popen(
            launch_command,
            cwd=str(proxy_root),
            env=launch_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
        if windows_job is not None:
            windows_job.assign(process)
            assert job_gate is not None and job_token is not None
            job_gate.write_text(job_token, encoding="ascii", newline="")
        connection = _AcpConnection(
            process,
            deadline=deadline,
            state=state,
            allow_writes=mode == "implement",
            workspace_root=workspace.expanduser().resolve(),
            deny_all_tools=mode == "availability",
            proxy_project_root=proxy_root / "project",
        )
        initialize = connection.request(
            "initialize",
            {
                "protocolVersion": "1",
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                    "_meta": {
                        "startupHints": {
                            "clientType": "agent-fleet",
                            "nonInteractive": True,
                            "skipGitStatus": True,
                            "skipProjectLayout": True,
                        }
                    },
                },
            },
        )
        if chrome_assignment is not None and (not isinstance(initialize, dict) or type(initialize.get("protocolVersion")) not in {int, str} or initialize.get("protocolVersion") not in (1, "1")):
            raise GrokAcpError("Grok ACP did not negotiate protocol v1 for stdio MCP")
        auth_methods = initialize.get("authMethods", []) if isinstance(initialize, dict) else []
        cached_auth_available = any(
            isinstance(item, dict) and item.get("id") == "cached_token"
            for item in auth_methods
        )
        if not cached_auth_available:
            raise GrokAcpError("Grok ACP did not advertise cached subscription authentication")
        connection.request(
            "authenticate",
            {"methodId": "cached_token", "_meta": {"headless": True}},
        )
        session = connection.request(
            "session/new",
            {
                "cwd": str(proxy_root),
                "mcpServers": mcp_servers,
                "_meta": {
                    "startupHints": {
                        "clientType": "agent-fleet",
                        "nonInteractive": True,
                        "skipGitStatus": True,
                        "skipProjectLayout": True,
                    },
                    "yoloMode": False,
                    "autoMode": False,
                },
            },
        )
        session_id_value = session.get("sessionId") if isinstance(session, dict) else None
        if not isinstance(session_id_value, str) or not session_id_value.strip():
            raise GrokAcpError("Grok ACP session/new returned no session ID")
        session_id = session_id_value.strip()
        state.expected_session_id = session_id
        if chrome_assignment is not None:
            import fleet_chrome_integration as chrome
            state.expected_mcp_tool_count = len(chrome.mcp_tool_names(chrome_assignment))
        if chrome_assignment is not None:
            try:
                connection.wait_for_mcp_inventory()
            finally:
                (run_dir / "grok-mcp-diagnostic.json").write_text(json.dumps({
                    "catalog_observed": state.mcp_catalog_observed,
                    "catalog_shapes": state.mcp_catalog_shapes,
                    "protocol_shapes": state.protocol_shapes,
                    "mcp_status_shapes": state.mcp_status_shapes,
                    "session_mcp_attested": state.session_mcp_attested,
                    "expected_count": len(state.expected_mcp_servers),
                    "observed_count": len(state.observed_mcp_servers or ()),
                    "inventory_mismatch": state.mcp_inventory_mismatch,
                }, indent=2), encoding="utf-8")
        if quota_exhaustion_detected("".join(connection.stderr_lines)):
            # The native CLI reports the HTTP 402 balance stop at startup;
            # never spend a prompt on an exhausted subscription.
            raise GrokAcpError(
                quota_exhaustion_error(), exit_code=QUOTA_EXHAUSTED_EXIT_CODE
            )
        next_prompt = prompt
        max_prompts = 1 + max_additional_prompts
        while prompt_count < max_prompts:
            state.begin_prompt()
            prompt_started = time.monotonic()
            try:
                last_result = connection.request(
                    "session/prompt",
                    {
                        "sessionId": session_id,
                        "prompt": [{"type": "text", "text": next_prompt}],
                        "_meta": {"promptMode": "agent"},
                    },
                )
            except GrokAcpError as exc:
                if quota_exhaustion_detected(
                    str(exc), "".join(connection.stderr_lines)
                ):
                    raise GrokAcpError(
                        quota_exhaustion_error(),
                        exit_code=QUOTA_EXHAUSTED_EXIT_CODE,
                    ) from exc
                raise
            prompt_count += 1
            state._capture_usage(last_result)
            current_envelope = _normalize_envelope(
                result=last_result,
                state=state,
                session_id=session_id,
                prompt_count=prompt_count,
                attempts=attempts,
                continuation_triggers=continuation_triggers,
                continuation_skip_reason=continuation_skip_reason,
            )
            decision = continuation_decider(_canonical_json(current_envelope))
            attempts.append(
                {
                    "prompt": prompt_count,
                    "kind": "initial" if prompt_count == 1 else "same-session-repair",
                    "prompt_sha256": _sha256_text(next_prompt),
                    "duration_seconds": round(time.monotonic() - prompt_started, 3),
                    "stop_reason": current_envelope["stopReason"],
                    "handoff_requires_continuation": decision is not None,
                    "tool_calls_cumulative": len(state.tool_calls),
                }
            )
            if decision is None:
                break
            trigger, next_prompt = decision
            continuation_triggers.append(trigger)
            if prompt_count >= max_prompts:
                continuation_skip_reason = "same-session-prompt-budget-exhausted"
                break
            if deadline - time.monotonic() < 15:
                continuation_skip_reason = "parent-time-budget-exhausted"
                break
        envelope = _normalize_envelope(
            result=last_result,
            state=state,
            session_id=session_id,
            prompt_count=prompt_count,
            attempts=attempts,
            continuation_triggers=continuation_triggers,
            continuation_skip_reason=continuation_skip_reason,
        )
        exit_code, stderr = connection.close()
        connection = None
    except GrokAcpError as exc:
        error_message = str(exc)
        exit_code = exc.exit_code
        timed_out = exit_code == 124
        tool_budget_exceeded = exit_code == 125
        if process is not None:
            _terminate_process_tree(process)
        if exit_code != QUOTA_EXHAUSTED_EXIT_CODE and quota_exhaustion_detected(
            error_message,
            "".join(connection.stderr_lines) if connection is not None else "",
        ):
            # The provider stopped on HTTP 402 (for example the process exited
            # before answering); classify it as quota exhaustion, never as a
            # generic transport failure.
            exit_code = QUOTA_EXHAUSTED_EXIT_CODE
            error_message = quota_exhaustion_error()
        if exit_code == QUOTA_EXHAUSTED_EXIT_CODE:
            # Quota exhaustion happens before prompt execution; no provider
            # tokens may be attributed to this attempt and no continuation
            # may be recorded or retried.
            state.latest_usage = None
            prompt_count = 0
            attempts.clear()
            continuation_triggers.clear()
        envelope = _normalize_envelope(
            result={"stopReason": "error", "text": ""},
            state=state,
            session_id=session_id,
            prompt_count=prompt_count,
            attempts=attempts,
            continuation_triggers=continuation_triggers,
            continuation_skip_reason=continuation_skip_reason,
        )
        stderr = error_message
        if connection is not None:
            connection.stdout_thread.join(timeout=2)
            connection.stderr_thread.join(timeout=2)
            captured = "".join(connection.stderr_lines)
            if captured:
                stderr += "\n" + _suppressed_provider_stderr(
                    connection.stderr_lines
                )
            if process is not None:
                _close_process_streams(process)
            connection = None
    finally:
        if windows_job is not None:
            try:
                windows_job.close()
            except GrokAcpError as exc:
                stderr = (stderr + "\n" + str(exc)).strip()
                if exit_code == 0:
                    exit_code = exc.exit_code
        if process is not None and process.poll() is None:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _terminate_process_tree(process)
        if process is not None:
            _close_process_streams(process)
        cleanup_errors = (
            _cleanup_runtime(run_dir=run_dir, run_id=run_id, proxy_base=proxy_base)
            if proxy_prepared and not retain_runtime
            else []
        )
        if cleanup_errors:
            stderr = (stderr + "\n" + "\n".join(cleanup_errors)).strip()
            if exit_code == 0:
                exit_code = 70
    return {
        "exit_code": exit_code,
        "stdout": _canonical_json(envelope),
        "stderr": stderr,
        "timed_out": timed_out,
        "quota_exhausted": exit_code == QUOTA_EXHAUSTED_EXIT_CODE,
        "model_tool_calls": len(state.tool_calls),
        "tool_budget_exceeded": tool_budget_exceeded or state.tool_budget_exceeded,
        "session_id": session_id,
        "prompt_count": prompt_count,
        "attempts": attempts,
        "continuation_triggers": continuation_triggers,
        "continuation_skip_reason": continuation_skip_reason,
        "duration_seconds": round(time.monotonic() - started, 3),
        "transport": TRANSPORT_ID,
    }
