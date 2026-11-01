#!/usr/bin/env python3
"""Validated target registry and fixed-path resolver for Capability Hub V1.2."""

from __future__ import annotations

import hashlib
import os
import platform
import re
import stat
import string
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from capability_core import (
    SLUG,
    CapabilityError,
    absolute_plain_path,
    strict_json_loads,
)


REGISTRY_NAME = "capability-targets.json"
FIXTURE_PREFIX = "capability-rollout-fixture-"
GUARD_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
ALLOWED_PLATFORMS = {"windows", "linux"}
ALLOWED_TEMPLATE_KEYS = {
    "appdata",
    "capability_id",
    "home",
    "robert_config_root",
    "robert_profile_root",
    "robert_skills_root",
    "tool",
}


@dataclass(frozen=True)
class GuardSpec:
    guard_id: str
    template: str
    required: bool


@dataclass(frozen=True)
class TargetSpec:
    tool: str
    platform: str
    expected_host: str
    expected_user: str
    risk_gate: str
    scope: str
    scan_root_template: str
    state_root_template: str
    activation_boundary: str
    discovery_contract: str
    rules_mutation: bool
    guard_paths: tuple[GuardSpec, ...]


@dataclass(frozen=True)
class TargetRegistry:
    path: Path
    real_roots: dict[str, str]
    targets: dict[str, TargetSpec]


@dataclass(frozen=True)
class ResolvedTarget:
    spec: TargetSpec
    scan_root: Path
    target: Path
    state_root: Path
    guards: dict[str, Path]
    fixture_root: Path | None

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

    @property
    def operation_lease_path(self) -> Path:
        return self.state_root / "operation-lease.lock"


def _require_string(value: dict[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise CapabilityError(f"Target registry field {key} must be a non-empty string")
    return item


def _template_keys(value: str) -> set[str]:
    keys: set[str] = set()
    try:
        for _, field_name, _, _ in string.Formatter().parse(value):
            if field_name:
                keys.add(field_name)
    except ValueError as exc:
        raise CapabilityError(f"Invalid target registry path template: {value}") from exc
    unknown = keys - ALLOWED_TEMPLATE_KEYS
    if unknown:
        raise CapabilityError(f"Unknown target registry template keys: {sorted(unknown)}")
    return keys


def load_registry(
    path: Path | str,
    *,
    raw: bytes | None = None,
) -> TargetRegistry:
    if raw is None:
        registry_path = absolute_plain_path(path, must_exist=True)
        if not stat.S_ISREG(registry_path.lstat().st_mode):
            raise CapabilityError(f"Target registry is not a plain file: {registry_path}")
        raw = registry_path.read_bytes()
    else:
        registry_path = Path(str(path))
    try:
        value = strict_json_loads(raw.decode("ascii", errors="strict"))
    except (OSError, UnicodeDecodeError, CapabilityError) as exc:
        raise CapabilityError(f"Invalid target registry: {registry_path}") from exc
    if not isinstance(value, dict) or set(value) != {
        "real_roots",
        "schema_version",
        "targets",
    }:
        raise CapabilityError("Target registry top-level keys are invalid")
    if value.get("schema_version") != 1:
        raise CapabilityError("Unsupported target registry schema")
    roots = value.get("real_roots")
    if not isinstance(roots, dict) or set(roots) != {
        "robert_config_root",
        "robert_profile_root",
        "robert_skills_root",
    }:
        raise CapabilityError("Target registry real_roots are invalid")
    real_roots: dict[str, str] = {}
    for key, item in roots.items():
        if not isinstance(item, str) or not item.startswith("/"):
            raise CapabilityError(f"Target registry root {key} must be absolute")
        real_roots[key] = item

    rows = value.get("targets")
    if not isinstance(rows, list) or len(rows) != 7:
        raise CapabilityError("Target registry must contain exactly seven targets")
    targets: dict[str, TargetSpec] = {}
    expected_keys = {
        "activation_boundary",
        "discovery_contract",
        "expected_host",
        "expected_user",
        "guard_paths",
        "platform",
        "risk_gate",
        "rules_mutation",
        "scan_root_template",
        "scope",
        "state_root_template",
        "tool",
    }
    for row in rows:
        if not isinstance(row, dict) or set(row) != expected_keys:
            raise CapabilityError("Target registry row keys are invalid")
        tool = _require_string(row, "tool")
        if not SLUG.fullmatch(tool) or tool in targets:
            raise CapabilityError(f"Invalid or duplicate target tool: {tool}")
        platform = _require_string(row, "platform")
        if platform not in ALLOWED_PLATFORMS:
            raise CapabilityError(f"Unsupported target platform: {platform}")
        scope = _require_string(row, "scope")
        scan_root = _require_string(row, "scan_root_template")
        state_root = _require_string(row, "state_root_template")
        _template_keys(scan_root)
        _template_keys(state_root)
        if row.get("rules_mutation") is not False:
            raise CapabilityError("V1.2 registry forbids rules/config mutation")
        guards_value = row.get("guard_paths")
        if not isinstance(guards_value, list) or not guards_value:
            raise CapabilityError(f"Target {tool} must declare at least one guard path")
        guards: list[GuardSpec] = []
        guard_ids: set[str] = set()
        for guard in guards_value:
            if not isinstance(guard, dict) or set(guard) != {
                "id",
                "required",
                "template",
            }:
                raise CapabilityError(f"Target {tool} has an invalid guard row")
            guard_id = _require_string(guard, "id")
            if not GUARD_ID.fullmatch(guard_id) or guard_id in guard_ids:
                raise CapabilityError(f"Target {tool} has an invalid guard id")
            template = _require_string(guard, "template")
            _template_keys(template)
            if guard.get("required") is not True:
                raise CapabilityError("V1.2 guard paths must fail closed when absent")
            guard_ids.add(guard_id)
            guards.append(GuardSpec(guard_id, template, True))
        targets[tool] = TargetSpec(
            tool=tool,
            platform=platform,
            expected_host=_require_string(row, "expected_host"),
            expected_user=_require_string(row, "expected_user"),
            risk_gate=_require_string(row, "risk_gate"),
            scope=scope,
            scan_root_template=scan_root,
            state_root_template=state_root,
            activation_boundary=_require_string(row, "activation_boundary"),
            discovery_contract=_require_string(row, "discovery_contract"),
            rules_mutation=False,
            guard_paths=tuple(guards),
        )
        if targets[tool].risk_gate not in {
            "standard",
            "robert-identity-or-explicit-same-user",
        }:
            raise CapabilityError(f"Target {tool} has an invalid risk gate")
        if (tool == "hermes") != (
            targets[tool].risk_gate == "robert-identity-or-explicit-same-user"
        ):
            raise CapabilityError("Only Hermes may use the Robert identity risk gate")
    expected_tools = {
        "antigravity",
        "claude",
        "codex",
        "grok",
        "hermes",
        "kimi",
        "kimi-work",
    }
    if set(targets) != expected_tools:
        raise CapabilityError(
            f"Target registry tools mismatch: {sorted(set(targets) ^ expected_tools)}"
        )
    return TargetRegistry(registry_path, real_roots, targets)


def _assert_below(path: Path, root: Path, label: str) -> None:
    candidate = Path(os.path.abspath(path))
    approved = Path(os.path.abspath(root))
    try:
        candidate.relative_to(approved)
    except ValueError as exc:
        raise CapabilityError(f"{label} escapes fixture root: {candidate}") from exc
    if candidate == approved:
        raise CapabilityError(f"{label} must be below the fixture root")


def assert_fixture_root(value: Path | str) -> Path:
    root = absolute_plain_path(value, must_exist=True)
    temp_root = absolute_plain_path(tempfile.gettempdir(), must_exist=True)
    _assert_below(root, temp_root, "fixture root")
    if root.parent != temp_root or not root.name.startswith(FIXTURE_PREFIX):
        raise CapabilityError(
            f"Fixture root must be a unique {FIXTURE_PREFIX}* child of system TEMP"
        )
    return root


def _render(template: str, variables: dict[str, str]) -> Path:
    try:
        rendered = template.format_map(variables)
    except KeyError as exc:
        raise CapabilityError(f"Missing target registry template value: {exc}") from exc
    path = Path(rendered)
    if not path.is_absolute():
        raise CapabilityError(f"Resolved target registry path is not absolute: {path}")
    return Path(os.path.abspath(path))


def current_os_identity() -> tuple[str, str]:
    host = platform.node()
    if not host:
        raise CapabilityError("Current host identity is unavailable")
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        size = wintypes.DWORD(0)
        ctypes.windll.advapi32.GetUserNameW(None, ctypes.byref(size))
        buffer = ctypes.create_unicode_buffer(size.value)
        if not ctypes.windll.advapi32.GetUserNameW(buffer, ctypes.byref(size)):
            raise CapabilityError("Current Windows user identity is unavailable")
        user = buffer.value
    else:
        import pwd

        user = pwd.getpwuid(os.geteuid()).pw_name
    if not user:
        raise CapabilityError("Current user identity is unavailable")
    return host, user


def resolve_target(
    registry: TargetRegistry,
    tool: str,
    capability_id: str,
    *,
    fixture_root: Path | str | None = None,
) -> ResolvedTarget:
    if tool not in registry.targets:
        raise CapabilityError(f"Unknown target tool: {tool}")
    if not SLUG.fullmatch(capability_id):
        raise CapabilityError("capability_id must be a lowercase slug")
    spec = registry.targets[tool]
    fixture: Path | None = None
    if fixture_root is None:
        current_platform = "windows" if os.name == "nt" else "linux"
        if current_platform != spec.platform:
            raise CapabilityError(
                f"Real target {tool} requires {spec.platform}, current host is {current_platform}"
            )
        current_host, current_user = current_os_identity()
        if current_host.casefold() != spec.expected_host.casefold():
            raise CapabilityError(
                f"Real target {tool} requires host {spec.expected_host}, "
                f"current host is {current_host}"
            )
        if current_user.casefold() != spec.expected_user.casefold():
            raise CapabilityError(
                f"Real target {tool} requires user {spec.expected_user}, "
                f"current user is {current_user}"
            )
        home = Path.home()
        appdata = Path(os.environ.get("APPDATA", home / "AppData/Roaming"))
        variables = {
            "home": str(home),
            "appdata": str(appdata),
            "tool": tool,
            "capability_id": capability_id,
            **registry.real_roots,
        }
    else:
        fixture = assert_fixture_root(fixture_root)
        variables = {
            "home": str(fixture / "profile"),
            "appdata": str(fixture / "profile/AppData/Roaming"),
            "robert_config_root": str(fixture / "robert/config"),
            "robert_profile_root": str(fixture / "robert/profile"),
            "robert_skills_root": str(fixture / "robert/skills"),
            "tool": tool,
            "capability_id": capability_id,
        }
    scan_root = _render(spec.scan_root_template, variables)
    state_root = _render(spec.state_root_template, variables)
    target = scan_root / capability_id
    guards = {
        guard.guard_id: _render(guard.template, variables) for guard in spec.guard_paths
    }
    if target.parent != scan_root:
        raise CapabilityError("Capability target must be a direct scan-root child")
    try:
        state_root.relative_to(scan_root)
    except ValueError:
        pass
    else:
        raise CapabilityError("Capability transaction state must be outside scan root")
    if fixture is not None:
        for candidate, label in (
            (scan_root, "scan root"),
            (target, "target"),
            (state_root, "state root"),
            *[(path, f"guard {guard_id}") for guard_id, path in guards.items()],
        ):
            _assert_below(candidate, fixture, label)
    return ResolvedTarget(spec, scan_root, target, state_root, guards, fixture)


def hash_plain_file(path: Path) -> str:
    plain = absolute_plain_path(path, must_exist=True)
    if not stat.S_ISREG(plain.lstat().st_mode):
        raise CapabilityError(f"Guard path is not a plain file: {plain}")
    digest = hashlib.sha256()
    with plain.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def guard_hashes(target: ResolvedTarget) -> dict[str, str]:
    return {
        guard_id: hash_plain_file(path)
        for guard_id, path in sorted(target.guards.items())
    }
