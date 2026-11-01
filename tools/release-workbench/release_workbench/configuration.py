"""Strict, local-only configuration helpers for the private release workbench.

Configuration paths are literal POSIX-style relative paths below an explicitly
provided owned root. Credential references use the opaque
``os-credential://<provider>/<handle>`` form; this module validates their shape
and never reads an operating-system credential store.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any


_CONFIG_KEYS = frozenset(
    {
        "version",
        "stack_root",
        "state_root",
        "runtime_root",
        "enabled_clis",
        "external_integrations",
        "network_integrations_enabled",
        "credential_references",
    }
)
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_PLACEHOLDER = re.compile(r"\{\{([A-Z][A-Z0-9_]*)\}\}")
_ENV_REFERENCE = re.compile(r"(?:\$[A-Za-z_][A-Za-z0-9_]*|%[A-Za-z_][A-Za-z0-9_]*%)")
_OWNER_PATH_PATTERNS = (
    # Absolute Windows drive path (``C:\...`` or ``D:/...``). The leading
    # local-text boundary keeps drive-relative (``C:relative``), relative,
    # and URL (``https://...`` including ``...archive-C:/...`` and
    # ``...archive=C:/...``) text clean, while an optional ``KEY=`` assignment
    # prefix keeps bare local assignments (``KEY=C:\...``) detected.
    re.compile(r"(?i)(?:^|[\s'\"`(\[])(?:[A-Za-z0-9_.-]*=)?[A-Za-z]:[\\/]"),
    # Unix home roots (``/home/<user>/...`` or ``/Users/<user>/...``). The
    # leading boundary covers Markdown (backticks, links) and bare ``KEY=``
    # assignments while keeping URL and relative paths clean.
    re.compile(r"(?i)(?:^|[\s'\"`(\[])(?:[A-Za-z0-9_.-]*=)?/(?:home|Users)(?:/|$)"),
)
_REPARSE_POINT = 0x400


def _error(message: str) -> None:
    raise ValueError(message)


def _is_reparse_point(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _path_has_linked_ancestor(path: Path) -> bool:
    """Check existing components without following a link or junction."""
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current = current / component
        try:
            info = current.lstat()
        except FileNotFoundError:
            # A missing component cannot have an existing descendant.
            break
        except OSError:
            _error("path ancestors could not be inspected")
        if _is_reparse_point(info):
            return True
    return False


def _owned_root(value: str | os.PathLike[str]) -> Path:
    try:
        supplied = Path(value)
    except (TypeError, ValueError, OSError):
        _error("owned_root must be an explicit absolute directory")
    if not supplied.is_absolute():
        _error("owned_root must be an explicit absolute directory")
    absolute = Path(os.path.abspath(os.fspath(supplied)))
    if _path_has_linked_ancestor(absolute):
        _error("owned_root has a symlink or reparse-point ancestor")
    try:
        root = absolute.resolve(strict=True)
        if not root.is_dir():
            _error("owned_root must be an existing directory")
    except (OSError, RuntimeError):
        _error("owned_root must be an existing directory")
    return root


def _relative_parts(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or value != value.strip():
        _error(f"{field} must be a non-empty relative path")
    if "\x00" in value or any(ord(char) < 32 for char in value):
        _error(f"{field} contains an invalid path character")
    if "\\" in value or ":" in value or value.startswith("/"):
        _error(f"{field} must use a literal relative path")
    if PureWindowsPath(value).drive or PurePosixPath(value).is_absolute():
        _error(f"{field} must use a literal relative path")
    if _ENV_REFERENCE.search(value) or value.startswith("~"):
        _error(f"{field} may not use environment or home expansion")
    parts = tuple(value.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        _error(f"{field} may not contain empty, dot, or traversal components")
    return parts


def _validate_owned_directory(root: Path, value: Any, field: str) -> str:
    parts = _relative_parts(value, field)
    candidate = root
    missing = False
    for index, component in enumerate(parts):
        candidate = candidate / component
        if missing:
            continue
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            missing = True
            continue
        except OSError:
            _error(f"{field} path could not be inspected")
        if _is_reparse_point(info):
            _error(f"{field} may not have a symlink or reparse-point ancestor")
        if not stat.S_ISDIR(info.st_mode):
            _error(f"{field} must refer to a directory path")
        # The check applies to every existing component, including ancestors.
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            _error(f"{field} has a non-directory ancestor")
    try:
        resolved = candidate.resolve(strict=False)
        common = os.path.commonpath((os.fspath(root), os.fspath(resolved)))
    except (OSError, RuntimeError, ValueError):
        _error(f"{field} path could not be resolved safely")
    if os.path.normcase(common) != os.path.normcase(os.fspath(root)):
        _error(f"{field} escapes owned_root")
    return "/".join(parts)


def _is_identifier(value: Any) -> bool:
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


def _validate_credential_reference(value: Any) -> bool:
    if not isinstance(value, str) or not value.startswith("os-credential://"):
        return False
    remainder = value[len("os-credential://") :]
    if any(char in remainder for char in "?#@\\\x00"):
        return False
    pieces = remainder.split("/")
    if len(pieces) < 2 or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", pieces[0]):
        return False
    return all(
        piece not in {"", ".", ".."}
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", piece) is not None
        for piece in pieces[1:]
    )


def validate_config(data: dict, *, owned_root: str | os.PathLike[str]) -> dict:
    """Validate the exact version-1 configuration without resolving secrets."""
    if not isinstance(data, dict):
        _error("config must be an object")
    if set(data) != _CONFIG_KEYS:
        _error("config has missing or unknown fields")
    if type(data["version"]) is not int or data["version"] != 1:
        _error("config version must be integer 1")

    root = _owned_root(owned_root)
    roots = {
        field: _validate_owned_directory(root, data[field], field)
        for field in ("stack_root", "state_root", "runtime_root")
    }

    enabled_clis = data["enabled_clis"]
    if not isinstance(enabled_clis, list):
        _error("enabled_clis must be a list")
    agents: set[str] = set()
    for entry in enabled_clis:
        if not isinstance(entry, dict) or set(entry) != {"agent", "model"}:
            _error("each enabled_cli must contain exactly agent and model")
        if not _is_identifier(entry["agent"]) or not _is_identifier(entry["model"]):
            _error("enabled_cli agent and model must be identifiers")
        agent_key = entry["agent"].casefold()
        if agent_key in agents:
            _error("enabled_cli agents must be unique")
        agents.add(agent_key)

    external = data["external_integrations"]
    if not isinstance(external, list):
        _error("external_integrations must be a list")
    integration_ids: set[str] = set()
    for entry in external:
        if not isinstance(entry, dict) or set(entry) != {"id", "enabled"}:
            _error("each external integration must contain exactly id and enabled")
        if not _is_identifier(entry["id"]):
            _error("external integration id must be an identifier")
        if entry["enabled"] is not False:
            _error("external integrations must remain disabled")
        integration_id = entry["id"].casefold()
        if integration_id in integration_ids:
            _error("external integration ids must be unique")
        integration_ids.add(integration_id)

    if data["network_integrations_enabled"] is not False:
        _error("network integrations must remain disabled")

    credential_references = data["credential_references"]
    if not isinstance(credential_references, dict):
        _error("credential_references must be a mapping")
    for agent, reference in credential_references.items():
        if not _is_identifier(agent) or agent.casefold() not in agents:
            _error("credential reference agent must name an enabled CLI")
        if not _validate_credential_reference(reference):
            _error("credential reference must be an opaque OS credential reference")

    return {
        "version": 1,
        "status": "PASS",
        "findings": [],
        "roots": roots,
        "enabled_cli_count": len(enabled_clis),
        "external_integration_count": len(external),
        "credential_reference_count": len(credential_references),
    }


def _template_value_is_unsafe(value: str) -> bool:
    if "{{" in value or "}}" in value or _ENV_REFERENCE.search(value):
        return True
    if value.startswith("~"):
        return True
    if re.search(r"(?:^|[/\\])\.\.(?:[/\\]|$)", value):
        return True
    if value.startswith(("/", "\\")):
        return True
    if re.match(r"^[A-Za-z]:[\\/](?!/)", value):
        return True
    windows_path = PureWindowsPath(value)
    if windows_path.drive and not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value):
        return True
    return False


def render_template(text: str, variables: dict) -> str:
    """Replace exactly the supplied uppercase placeholders in one pass."""
    if not isinstance(text, str):
        _error("template must be text")
    if not isinstance(variables, dict):
        _error("template variables must be a mapping")
    if any(not isinstance(key, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) for key in variables):
        _error("template variable names must be uppercase identifiers")
    if any(not isinstance(value, str) for value in variables.values()):
        _error("template variable values must be strings")

    matches = list(_PLACEHOLDER.finditer(text))
    remainder = _PLACEHOLDER.sub("", text)
    if "{{" in remainder or "}}" in remainder:
        _error("template contains a malformed placeholder")
    names = {match.group(1) for match in matches}
    if names != set(variables):
        _error("template placeholders and supplied variables must match exactly")
    if any(_template_value_is_unsafe(value) for value in variables.values()):
        _error("template values may not contain traversal, expansion, or nested placeholders")
    return _PLACEHOLDER.sub(lambda match: variables[match.group(1)], text)


def _without_url_tokens(line: str) -> str:
    """Hide syntactic URL tokens for local-path matching, preserving boundaries.

    A scheme must contain at least two characters so a Windows drive followed
    by two slashes remains a local path. Quotes, backticks, angle brackets and
    whitespace delimit tokens; ordinary URL punctuation remains inside them.
    """
    return re.sub(
        r"(?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]+://[^\s\"'`<>]*",
        lambda match: " " * len(match.group()),
        line,
    )


def _scan_root(value: str | os.PathLike[str]) -> Path:
    try:
        supplied = Path(value)
    except (TypeError, ValueError, OSError):
        _error("root must be an explicit absolute directory")
    if not supplied.is_absolute():
        _error("root must be an explicit absolute directory")
    absolute = Path(os.path.abspath(os.fspath(supplied)))
    if _path_has_linked_ancestor(absolute):
        _error("root has a symlink or reparse-point ancestor")
    try:
        root = absolute.resolve(strict=True)
        if not root.is_dir():
            _error("root must be an existing directory")
    except (OSError, RuntimeError):
        _error("root must be an existing directory")
    return root


def verify_portability(
    root: str | os.PathLike[str], *, forbidden_literals: list[str] | tuple[str, ...] | None = None
) -> dict:
    """Find known owner-path and supplied-topology literals without echoing them."""
    scan_root = _scan_root(root)
    literals = [] if forbidden_literals is None else forbidden_literals
    if not isinstance(literals, (list, tuple)):
        _error("forbidden_literals must be a list of non-empty strings")
    if any(
        not isinstance(value, str) or not value or "\x00" in value or "\n" in value or "\r" in value
        for value in literals
    ):
        _error("forbidden_literals must be a list of non-empty single-line strings")

    findings: set[tuple[str, str, int | None]] = set()
    files_scanned = 0
    pending = [scan_root]
    while pending:
        directory = pending.pop()
        relative_directory = directory.relative_to(scan_root).as_posix()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name.casefold())
        except OSError:
            findings.add(("unscannable", relative_directory or ".", None))
            continue
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(scan_root).as_posix()
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                findings.add(("unscannable", relative, None))
                continue
            if _is_reparse_point(info):
                findings.add(("unsafe_link", relative, None))
            elif stat.S_ISDIR(info.st_mode):
                pending.append(path)
            elif stat.S_ISREG(info.st_mode):
                try:
                    raw = path.read_bytes()
                    if b"\x00" in raw:
                        raise UnicodeError("binary")
                    content = raw.decode("utf-8")
                except (OSError, UnicodeError):
                    findings.add(("unscannable", relative, None))
                    continue
                files_scanned += 1
                for line_number, line in enumerate(content.splitlines(), start=1):
                    if any(pattern.search(_without_url_tokens(line)) for pattern in _OWNER_PATH_PATTERNS):
                        findings.add(("owner_path", relative, line_number))
                    folded = line.casefold()
                    for index, literal in enumerate(literals, start=1):
                        if literal.casefold() in folded:
                            findings.add((f"topology_literal_{index}", relative, line_number))

    ordered = [
        {"rule": rule, "path": path, "line": line}
        for rule, path, line in sorted(
            findings,
            key=lambda item: (item[1].casefold(), item[2] if item[2] is not None else 0, item[0]),
        )
    ]
    return {
        "version": 1,
        "status": "FAIL" if ordered else "PASS",
        "files_scanned": files_scanned,
        "finding_count": len(ordered),
        "findings": ordered,
        "limitations": [
            "Pattern matching detects generic machine-local path syntax outside syntactic URL tokens and caller-supplied literals; it is not a complete portability or privacy audit."
        ],
    }
