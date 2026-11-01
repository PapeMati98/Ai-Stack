"""Uninstalled candidate contract for Fleet's assigned managed Chrome relay.

The TODO node carries only web="managed-chrome". Relay paths and harness
identity come from the fixed Fleet-side binding file, never the node, prompt,
environment, or tool arguments.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
import struct
import time
import zlib
from pathlib import Path
import queue
import re
import secrets
import subprocess
import sys
import threading
from typing import Any

SERVER = "fleet-chrome-relay"
OFFICE_SERVER = "office"
OPT_IN = "managed-chrome"
BINDING_SCHEMA = "fleet-chrome-binding/1"
CLI_MAX_LINE = 65_536
CLI_RESPONSE_TIMEOUT_S = 20.0
CHROME_CALL_CAP = 12
BUDGET_STATE_NAME = 'fleet-chrome-call-budget.v1'
BUDGET_MAGIC = b'FCHBUD1\n'
BUDGET_SLOT = struct.Struct('>QII16sI')
BUDGET_FILE_SIZE = len(BUDGET_MAGIC) + 2 * BUDGET_SLOT.size
STATIC_MCP_ADAPTERS = frozenset(("minimax", "muse", "antigravity"))
MCP_TICKET_ENV = "FLEET_CHROME_RUN_TICKET"
MCP_TICKET_SCHEMA = "fleet-chrome-run-ticket/1"

# Fixed existing Fleet adapter IDs -> owner-enrolled relay harness/transport.
# There is deliberately no inference or fallback for an unknown adapter.
ROUTES = {
    "codex": ("codex", "mcp"),
    "claude": ("claude", "mcp"),
    "qwen": ("qwen", "mcp"),
    "mistral": ("mistral", "cli"),
    "zcode": ("glm", "cli"),
    "codebuddy": ("codebuddy", "mcp"),
    "kimi": ("kimi", "mcp"),
    "grok": ("grok", "mcp"),
    "mimo": ("mimo", "mcp"),
    "minimax": ("minimax", "mcp"),
    "muse": ("muse", "mcp"),
    "antigravity": ("antigravity", "mcp"),
    "openclaw": ("openclaw", "cli"),
}
MCP_FLEET_ADAPTERS = frozenset(("codex", "claude", "qwen", "codebuddy", "kimi", "grok", "mimo", *STATIC_MCP_ADAPTERS))

BASE = {
    "request_id": {
        "type": "string",
        "pattern": "^[A-Za-z0-9._:-]{1,128}$",
        "description": "Stable ID for this operation. Reuse only for identical retries; an unknown mutation is never replayed.",
    }
}
_RAW_TOOLS = [
    ("chrome_attachment", "Observe this session's attachment and reconcile an existing authorized technical connection.", {}, []),
    ("chrome_open", "Open an inactive owned tab within the fixed approved origin policy.", {"url": {"type": "string", "maxLength": 2048}}, ["url"]),
    ("chrome_read", "Read a bounded redacted excerpt and fresh opaque element references from an owned tab.", {}, []),
    ("chrome_click", "Click a fresh reference from this session's most recent read, within approved actions.", {"ref": {"type": "string", "maxLength": 32}}, ["ref"]),
    ("chrome_type", "Type ordinary text into a fresh reference; credentials, MFA and passkeys are denied.", {"ref": {"type": "string", "maxLength": 32}, "text": {"type": "string", "minLength": 1, "maxLength": 1000}}, ["ref", "text"]),
    ("chrome_wait", "Observe bounded text, role or name on an owned tab; at least one condition is required.", {key: {"type": "string", "minLength": 1, "maxLength": 200} for key in ("text", "role", "name")}, []),
    ("chrome_close", "Close only a tab owned by this session.", {}, []),
    ("chrome_saved_login", "Submit the owner-approved saved-password form on an owned login tab once. Takes no credentials; returns submitted with authentication unverified.", {}, []),
]
MCP_TOOLS = tuple(row[0] for row in _RAW_TOOLS)
_MUTATING = frozenset(("chrome_click", "chrome_type", "chrome_close", "chrome_saved_login"))
_OBSERVATIONS = frozenset(("chrome_attachment", "chrome_read", "chrome_wait"))
# Reviewers use independent owned tabs; no access to a maker's tabs is inferred.
# Opening/closing their own tabs is inspection, not a claim of idempotent input.
_READ_ONLY = frozenset((*_OBSERVATIONS, "chrome_open", "chrome_close"))
NATIVE_PREFIX = "fleet_chrome_"
NATIVE_TO_RELAY = {NATIVE_PREFIX + name.removeprefix("chrome_"): name for name in MCP_TOOLS}
NATIVE_READ_ONLY_TOOLS = tuple(NATIVE_PREFIX + name.removeprefix("chrome_") for name in MCP_TOOLS if name in _READ_ONLY)


class Reject(ValueError):
    """Fail-closed integration or call rejection."""


def tool_specs(*, read_only: bool = False) -> list[dict[str, Any]]:
    """Return the fixed schemas from the relay's public MCP contract."""
    result = []
    for name, description, properties, required in _RAW_TOOLS:
        if read_only and name not in _READ_ONLY:
            continue
        props = {**copy.deepcopy(BASE), **copy.deepcopy(properties)}
        needs = list(required)
        if name not in ("chrome_attachment", "chrome_open"):
            props["tab_id"] = {"type": "integer", "minimum": 1, "maximum": 2147483647}
            needs.insert(0, "tab_id")
        schema: dict[str, Any] = {
            "type": "object",
            "properties": props,
            "additionalProperties": False,
        }
        if needs:
            schema["required"] = needs
        if name == "chrome_wait":
            schema["anyOf"] = [{"required": [key]} for key in ("text", "role", "name")]
        is_read_only = name in _OBSERVATIONS
        result.append({
            "name": name,
            "description": description,
            "inputSchema": schema,
            "annotations": {
                "readOnlyHint": is_read_only,
                "destructiveHint": not is_read_only,
                "idempotentHint": is_read_only,
                "openWorldHint": True,
            },
        })
    return result


def web_opt_in(task: Any) -> bool:
    """Absent field preserves baseline. Unknown values and override attempts reject."""
    if not isinstance(task, dict):
        raise Reject("todo_node")
    value = task.get("web")
    if "web" not in task:
        return False
    if value is False:
        return False
    if value != OPT_IN:
        raise Reject("web_opt_in")
    approvals = task.get("requires_approval", [])
    if not isinstance(approvals, list) or any(type(item) is not str for item in approvals):
        raise Reject("approval_shape")
    if task.get("STOP") is True or task.get("stop") is True:
        raise Reject("stop")
    forbidden_names = {
        "mcp_servers", "mcp_config", "harness", "transport", "launcher",
        "config", "config_path", "config_file", "owner_config", "profile", "profile_id",
        "origin", "origins", "sid", "session_id", "token", "access_token", "environment",
        "env", "env_overrides", "chrome_assignment", "browser_assignment",
    }
    forbidden = {key for key in task if type(key) is str and (
        key.casefold().startswith("chrome_") or key.casefold().startswith("browser_")
        or key.casefold() in forbidden_names
    )}
    if forbidden:
        raise Reject("web_override")
    if task.get("STOP") or task.get("stop") or "STOP" in approvals:
        raise Reject("stop")
    return True


def _absolute_path(value: Any, label: str, *, basename: str | None = None) -> str:
    if (type(value) is not str or not value or any(ord(char) < 32 for char in value)
            or not Path(value).is_absolute() or ".." in Path(value).parts):
        raise Reject(label)
    path = Path(value)
    if basename is not None and path.name.casefold() != basename.casefold():
        raise Reject(label)
    return str(path)


def validate_binding(value: Any) -> dict[str, str]:
    """Validate only routing metadata; never open the owner-managed config."""
    required = {"schema", "python", "managed", "config"}
    if not isinstance(value, dict) or set(value) != required or value.get("schema") != BINDING_SCHEMA:
        raise Reject("binding_shape")
    python = _absolute_path(value["python"], "python")
    managed = _absolute_path(value["managed"], "managed", basename="managed.py")
    config = _absolute_path(value["config"], "config")
    return {"schema": BINDING_SCHEMA, "python": python, "managed": managed, "config": config}


def load_binding(path: Path | str) -> dict[str, str]:
    """The caller supplies only Fleet's fixed binding path, never a TODO value."""
    target = Path(path)
    if not target.is_absolute() or target.is_symlink() or not target.is_file() or target.stat().st_size > 4096:
        raise Reject("binding_unavailable")
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        raise Reject("binding_unavailable") from None
    return validate_binding(value)


def assignment(adapter_id: Any, task: Any, trusted_binding: Any) -> dict[str, str] | None:
    """Build the exact enrolled assignment from Fleet profile identity + trusted binding."""
    if not web_opt_in(task):
        return None
    if type(adapter_id) is not str or adapter_id not in ROUTES:
        raise Reject("unknown_harness")
    mode = task.get("fleet_mode", task.get("mode", "implement"))
    if type(mode) is not str:
        raise Reject("mode")
    return fleet_assignment(adapter_id, trusted_binding, mode=mode)


def fleet_assignment(adapter_id: Any, trusted_binding: Any, *, mode: str) -> dict[str, str]:
    """Build one exact existing adapter assignment, bound to Fleet's run mode."""
    if type(adapter_id) is not str or adapter_id not in ROUTES:
        raise Reject("adapter_not_integrated")
    if type(mode) is not str or mode not in {"implement", "review"}:
        raise Reject("mode")
    if adapter_id in STATIC_MCP_ADAPTERS and mode != "implement":
        # Their pinned builders cannot prove reviewer MCP isolation. The static
        # entry stays inert without an implementation ticket.
        raise Reject("review_web_unsupported")
    binding = validate_binding(trusted_binding)
    harness, transport = ROUTES[adapter_id]
    return {**binding, "adapter": adapter_id, "harness": harness,
            "transport": transport, "mode": mode}


def fleet_mcp_assignment(adapter_id: Any, trusted_binding: Any, *, mode: str) -> dict[str, str]:
    if type(adapter_id) is not str or adapter_id not in MCP_FLEET_ADAPTERS:
        raise Reject("adapter_not_integrated")
    result = fleet_assignment(adapter_id, trusted_binding, mode=mode)
    return validate_fleet_assignment(result, adapter_id)


def fleet_cli_assignment(adapter_id: Any, trusted_binding: Any, *, mode: str) -> dict[str, str]:
    if adapter_id not in {"mistral", "zcode", "openclaw"}:
        raise Reject("adapter_not_integrated")
    result = fleet_assignment(adapter_id, trusted_binding, mode=mode)
    return validate_fleet_assignment(result, adapter_id)


def _validate_assignment(value: Any, transport: str | None = None) -> dict[str, str]:
    base_fields = {"schema", "python", "managed", "config", "adapter", "harness", "transport", "mode"}
    budget_fields = {"budget_state", "budget_nonce", "budget_limit"}
    if not isinstance(value, dict) or set(value) not in (base_fields, base_fields | budget_fields):
        raise Reject("assignment_shape")
    binding = validate_binding({key: value[key] for key in ("schema", "python", "managed", "config")})
    if type(value["adapter"]) is not str:
        raise Reject("assignment_route")
    route = ROUTES.get(value["adapter"])
    if route is None or (value["harness"], value["transport"]) != route:
        raise Reject("assignment_route")
    if type(value["mode"]) is not str or value["mode"] not in {"implement", "review"}:
        raise Reject("assignment_mode")
    if value["adapter"] == "zcode" and value["mode"] != "review":
        raise Reject("adapter_mode_not_supported")
    if transport is not None and value["transport"] != transport:
        raise Reject("assignment_transport")
    result = {**binding, "adapter": value["adapter"], "harness": value["harness"],
              "transport": value["transport"], "mode": value["mode"]}
    budget_fields = {"budget_state", "budget_nonce", "budget_limit"}
    base_fields = {"schema", "python", "managed", "config", "adapter", "harness", "transport", "mode"}
    if set(value) == base_fields | budget_fields:
        state_path = _absolute_path(value["budget_state"], "budget_state", basename=BUDGET_STATE_NAME)
        nonce = value["budget_nonce"]
        limit = value["budget_limit"]
        if type(nonce) is not str or re.fullmatch(r"[a-f0-9]{32}", nonce) is None:
            raise Reject("budget_binding")
        if type(limit) is not int or not 1 <= limit <= CHROME_CALL_CAP:
            raise Reject("budget_binding")
        result.update({"budget_state": state_path, "budget_nonce": nonce, "budget_limit": limit})
    elif set(value) != base_fields:
        raise Reject("assignment_shape")
    return result


def _budget_stat(path: Path):
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise Reject("budget_state") from exc
    if (stat.S_ISLNK(info.st_mode)
            or bool(getattr(info, "st_file_attributes", 0) & 0x400)
            or not stat.S_ISREG(info.st_mode)):
        raise Reject("budget_reparse")
    return info


def _slot(generation: int, used: int, limit: int, nonce_digest: bytes) -> bytes:
    body = struct.pack(">QII16s", generation, used, limit, nonce_digest)
    return body + struct.pack(">I", zlib.crc32(body) & 0xffffffff)


def _decode_slot(raw: bytes):
    if len(raw) != BUDGET_SLOT.size:
        raise Reject("budget_corrupt")
    generation, used, limit, nonce_digest, checksum = BUDGET_SLOT.unpack(raw)
    if (zlib.crc32(raw[:-4]) & 0xffffffff) != checksum:
        raise Reject("budget_corrupt")
    return generation, used, limit, nonce_digest


def initialize_call_budget(run_dir: Path | str, nonce: str, *, limit: int = CHROME_CALL_CAP) -> str:
    """Create the fixed-size per-run ledger before the native client starts."""
    root = Path(run_dir)
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise Reject("budget_root")
    root_stat = os.lstat(root)
    if bool(getattr(root_stat, "st_file_attributes", 0) & 0x400):
        raise Reject("budget_root")
    if (type(nonce) is not str or re.fullmatch(r"[a-f0-9]{32}", nonce) is None
            or type(limit) is not int or not 1 <= limit <= CHROME_CALL_CAP):
        raise Reject("budget_binding")
    path = root / BUDGET_STATE_NAME
    digest = hashlib.sha256(nonce.encode("ascii")).digest()[:16]
    initial = BUDGET_MAGIC + _slot(0, 0, limit, digest) * 2
    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | int(getattr(os, "O_BINARY", 0))
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
        try:
            _budget_stat(path)
            if os.write(fd, initial) != len(initial):
                raise Reject("budget_write")
            os.fsync(fd)
            if os.fstat(fd).st_size != BUDGET_FILE_SIZE:
                raise Reject("budget_write")
        finally:
            os.close(fd)
    except FileExistsError as exc:
        raise Reject("budget_already_initialized") from exc
    return str(path.resolve())


def _lock_budget_fd(fd: int) -> None:
    deadline = time.monotonic() + 1.0
    if os.name == "nt":
        import msvcrt
        while True:
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise Reject("budget_lock") from exc
                time.sleep(0.01)
    else:
        import fcntl
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise Reject("budget_lock") from exc
                time.sleep(0.01)


def _unlock_budget_fd(fd: int) -> None:
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


def consume_call_budget(state_path: str, nonce: str, *, limit: int = CHROME_CALL_CAP) -> int:
    """Locked, fixed-size ledger shared across MCP children and process restarts."""
    if (type(state_path) is not str or not Path(state_path).is_absolute()
            or Path(state_path).name != BUDGET_STATE_NAME
            or type(nonce) is not str or re.fullmatch(r"[a-f0-9]{32}", nonce) is None
            or type(limit) is not int or not 1 <= limit <= CHROME_CALL_CAP):
        raise Reject("budget_binding")
    path = Path(state_path)
    before = _budget_stat(path)
    flags = os.O_RDWR | int(getattr(os, "O_BINARY", 0))
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise Reject("budget_open") from exc
    locked = False
    try:
        opened = os.fstat(fd)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_size != BUDGET_FILE_SIZE
                or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)):
            raise Reject("budget_race_or_size")
        _lock_budget_fd(fd)
        locked = True
        current = _budget_stat(path)
        opened = os.fstat(fd)
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise Reject("budget_race")
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, BUDGET_FILE_SIZE + 1)
        if len(raw) != BUDGET_FILE_SIZE or raw[:len(BUDGET_MAGIC)] != BUDGET_MAGIC:
            raise Reject("budget_corrupt")
        offset = len(BUDGET_MAGIC)
        slots = [_decode_slot(raw[offset:offset+BUDGET_SLOT.size]),
                 _decode_slot(raw[offset+BUDGET_SLOT.size:offset+2*BUDGET_SLOT.size])]
        digest = hashlib.sha256(nonce.encode("ascii")).digest()[:16]
        if any(item[2] != limit or item[3] != digest or item[0] != item[1]
               or item[1] > limit for item in slots):
            raise Reject("budget_binding_or_corrupt")
        if slots[0][0] == slots[1][0]:
            if slots[0] != slots[1]:
                raise Reject("budget_corrupt")
            newest = 0
        else:
            newer = 0 if slots[0][0] > slots[1][0] else 1
            older = 1 - newer
            if slots[newer][0] != slots[older][0] + 1 or slots[newer][1] != slots[older][1] + 1:
                raise Reject("budget_corrupt")
            newest = newer
        generation, used, actual_limit, _ = slots[newest]
        if used >= actual_limit:
            raise Reject("budget_exhausted")
        target = 1 - newest
        updated = _slot(generation + 1, used + 1, actual_limit, digest)
        os.lseek(fd, offset + target * BUDGET_SLOT.size, os.SEEK_SET)
        if os.write(fd, updated) != BUDGET_SLOT.size:
            raise Reject("budget_write")
        os.fsync(fd)
        after = os.stat(path)
        if (os.fstat(fd).st_size != BUDGET_FILE_SIZE
                or (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)):
            raise Reject("budget_race_or_size")
        return used + 1
    except OSError as exc:
        raise Reject("budget_io") from exc
    finally:
        if locked:
            try:
                _unlock_budget_fd(fd)
            except OSError:
                pass
        os.close(fd)


def bind_run_budget(value: Any, run_dir: Path | str, *, create: bool, limit: int) -> dict[str, Any]:
    route = _validate_assignment(value)
    nonce = secrets.token_hex(16)
    root = Path(run_dir)
    if not root.is_absolute():
        raise Reject("budget_root")
    state_path = (initialize_call_budget(root, nonce, limit=limit) if create
                  else str(root / BUDGET_STATE_NAME))
    return {**route, "budget_state": state_path, "budget_nonce": nonce, "budget_limit": limit}


def validate_fleet_assignment(value: Any, adapter_id: Any) -> dict[str, str]:
    route = _validate_assignment(value)
    if type(adapter_id) is not str or route["adapter"] != adapter_id:
        raise Reject("assignment_adapter")
    return route


def mcp_tool_names(value: Any) -> tuple[str, ...]:
    route = _validate_assignment(value, "mcp")
    names = tuple(name for name in MCP_TOOLS if route["mode"] != "review" or name in _READ_ONLY)
    return names


def mcp_entry(value: Any) -> dict[str, Any]:
    """One fixed stdio server; only Fleet-owned run budget crosses argv."""
    route = _validate_assignment(value, "mcp")
    if "budget_state" not in route:
        raise Reject("run_budget_required")
    return {
        "command": route["python"],
        "args": ["-B", str(Path(__file__).resolve()), "--managed-chrome-mcp",
                 "--harness", route["harness"], "--mode", route["mode"],
                 "--budget-state", route["budget_state"],
                 "--budget-nonce", route["budget_nonce"],
                 "--budget-limit", str(route["budget_limit"])],
    }


def static_mcp_entry(adapter_id: Any, trusted_binding: Any) -> dict[str, Any]:
    """Persistent native config entry; the per-run grant arrives only via Fleet env."""
    if type(adapter_id) is not str or adapter_id not in STATIC_MCP_ADAPTERS:
        raise Reject("adapter_not_integrated")
    route = fleet_mcp_assignment(adapter_id, trusted_binding, mode="implement")
    return {
        "command": route["python"],
        "args": ["-B", str(Path(__file__).resolve()), "--managed-chrome-mcp-static",
                 "--harness", route["harness"]],
    }


def static_mcp_config(adapter_id: Any, trusted_binding: Any) -> dict[str, Any]:
    """Render a single-server fragment in the pinned native client's public schema."""
    entry = static_mcp_entry(adapter_id, trusted_binding)
    if adapter_id == "muse":
        # Muse clears non-allowlisted child env. Expand only this per-run ticket;
        # outside Fleet the empty default must leave the server inert.
        return {"mcpServers": {SERVER: {"type": "stdio", **entry, "required": True,
                                      "env": {MCP_TICKET_ENV: "${" + MCP_TICKET_ENV + ":-}"}}}}
    return {"mcpServers": {SERVER: entry}}


def merge_static_mcp_config(adapter_id: Any, existing: Any, trusted_binding: Any) -> dict[str, Any]:
    """Pure candidate merge: retain settings and foreign MCP entries; reject name clashes."""
    if not isinstance(existing, dict):
        raise Reject("native_settings_shape")
    key = "mcpServers"
    if adapter_id == "muse":
        if "mcpServers" in existing and "mcp_servers" in existing:
            raise Reject("native_mcp_ambiguous")
        if "mcp_servers" in existing:
            key = "mcp_servers"
    merged = copy.deepcopy(existing)
    servers = merged.get(key, {})
    if not isinstance(servers, dict):
        raise Reject("native_mcp_shape")
    expected = static_mcp_config(adapter_id, trusted_binding)["mcpServers"][SERVER]
    current = servers.get(SERVER)
    if current is not None and current != expected:
        raise Reject("native_mcp_name_conflict")
    servers[SERVER] = expected
    merged[key] = servers
    return merged


def merge_antigravity_chrome_permissions(existing: Any) -> dict[str, Any]:
    """Native headless approvals only for the eight fixed relay tools."""
    if not isinstance(existing, dict):
        raise Reject("native_settings_shape")
    merged = copy.deepcopy(existing)
    permissions = merged.setdefault("permissions", {})
    if not isinstance(permissions, dict):
        raise Reject("native_permissions_shape")
    for key in ("allow", "deny", "ask"):
        if key in permissions and (not isinstance(permissions[key], list)
                                   or any(type(v) is not str for v in permissions[key])):
            raise Reject("native_permissions_shape")
    if any(v.startswith("mcp(") for v in permissions.get("deny", []) + permissions.get("ask", [])):
        # Preserve any deliberate MCP Ask/Deny policy for operator reconciliation.
        raise Reject("native_mcp_permission_conflict")
    allowed = permissions.setdefault("allow", [])
    for name in MCP_TOOLS:
        rule = "mcp(" + SERVER + "/" + name + ")"
        if rule not in allowed:
            allowed.append(rule)
    return merged


def _ticket_from_assignment(value: Any, adapter_id: Any) -> dict[str, Any]:
    route = _validate_assignment(value, "mcp")
    if route["adapter"] not in STATIC_MCP_ADAPTERS or route["adapter"] != adapter_id:
        raise Reject("ticket_adapter")
    if route["mode"] != "implement" or "budget_state" not in route:
        raise Reject("ticket_scope")
    ticket = {
        "schema": MCP_TICKET_SCHEMA,
        "adapter": route["adapter"],
        "harness": route["harness"],
        "mode": route["mode"],
        "budget_state": route["budget_state"],
        "budget_nonce": route["budget_nonce"],
        "budget_limit": route["budget_limit"],
    }
    if len(json.dumps(ticket, separators=(",", ":"), ensure_ascii=True)) > 4096:
        raise Reject("ticket_size")
    return ticket


def sanitize_static_mcp_environment(environ: Any, assignment: Any, adapter_id: Any) -> dict[str, str]:
    """Remove inherited ticket-shaped env, then issue only Fleet's exact run ticket."""
    if type(adapter_id) is not str or adapter_id not in STATIC_MCP_ADAPTERS:
        raise Reject("adapter_not_integrated")
    if not isinstance(environ, dict) or any(type(k) is not str or type(v) is not str for k, v in environ.items()):
        raise Reject("environment_shape")
    result = {key: value for key, value in environ.items()
              if not key.upper().startswith("FLEET_CHROME_")}
    if assignment is None:
        return result
    ticket = _ticket_from_assignment(assignment, adapter_id)
    result[MCP_TICKET_ENV] = json.dumps(ticket, separators=(",", ":"), ensure_ascii=True)
    return result


def _parse_static_ticket(raw: Any, harness: Any) -> dict[str, Any] | None:
    if raw is None or raw == "":
        if any(key.upper().startswith("FLEET_CHROME_") and key.upper() != MCP_TICKET_ENV
               for key in os.environ):
            raise Reject("ticket_incomplete")
        return None
    if type(raw) is not str or len(raw) > 4096:
        raise Reject("ticket_shape")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise Reject("ticket_duplicate")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=unique_object,
                           parse_constant=lambda _value: (_ for _ in ()).throw(Reject("ticket_number")))
    except (ValueError, TypeError, RecursionError):
        raise Reject("ticket_json") from None
    required = {"schema", "adapter", "harness", "mode", "budget_state", "budget_nonce", "budget_limit"}
    if not isinstance(value, dict) or set(value) != required:
        raise Reject("ticket_shape")
    if (value["schema"] != MCP_TICKET_SCHEMA or value["adapter"] != harness
            or value["harness"] != harness or value["mode"] != "implement"):
        raise Reject("ticket_scope")
    return value


def _valid_mcp_client_info(info: Any) -> bool:
    # Ignored standard implementation metadata never changes the assignment.
    if (not isinstance(info, dict) or not {"name", "version"} <= set(info)
            or set(info) - {"name", "version", "title", "description", "websiteUrl", "icons"}):
        return False
    for key, value in info.items():
        if key != "icons" and (not isinstance(value, str) or len(value) > 4096):
            return False
    icons = info.get("icons", [])
    if not isinstance(icons, list) or len(icons) > 16:
        return False
    for icon in icons:
        if (not isinstance(icon, dict) or "src" not in icon
                or set(icon) - {"src", "mimeType", "sizes", "theme"}
                or not isinstance(icon["src"], str) or len(icon["src"]) > 32768
                or ("mimeType" in icon and not isinstance(icon["mimeType"], str))
                or ("theme" in icon and icon["theme"] not in ("light", "dark"))):
            return False
        sizes = icon.get("sizes", [])
        if not isinstance(sizes, list) or len(sizes) > 32 or any(not isinstance(size, str) for size in sizes):
            return False
    return True


def _serve_empty_mcp(stdin=None, stdout=None) -> int:
    """Static user entry without a trusted Fleet ticket exposes zero tools."""
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    initialized = False
    ready = False
    protocol_versions = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

    def reply(value):
        stdout.write(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("utf-8") + b"\n")
        stdout.flush()

    def error(rid, code, message):
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}

    def unique_object(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate")
            result[key] = item
        return result

    while True:
        line = stdin.readline(65_538)
        if not line:
            return 0
        rid = None
        try:
            if len(line) > 65_537 or (len(line) > 65_536 and not line.endswith(b"\n")):
                reply(error(None, -32700, "Message exceeds limit"))
                return 1
            value = json.loads(line.rstrip(b"\r\n"), object_pairs_hook=unique_object,
                               parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("constant")))
            if isinstance(value, dict) and type(value.get("id")) in (str, int):
                rid = value.get("id")
            if (not isinstance(value, dict) or set(value) - {"jsonrpc", "id", "method", "params"}
                    or value.get("jsonrpc") != "2.0" or type(value.get("method")) is not str
                    or ("id" in value and type(value["id"]) not in (str, int))):
                reply(error(rid, -32600, "Invalid Request"))
                continue
            method = value["method"]
            params = value.get("params", {})
            notification = "id" not in value
            if not isinstance(params, dict):
                if not notification:
                    reply(error(rid, -32602, "Invalid params"))
                continue
            if notification:
                if (method == "notifications/initialized" and initialized
                        and not set(params) - {"_meta"}
                        and ("_meta" not in params or isinstance(params["_meta"], dict))):
                    ready = True
                continue
            if method == "initialize":
                if (initialized or set(params) - {"protocolVersion", "capabilities", "clientInfo", "_meta"}
                        or not {"protocolVersion", "capabilities", "clientInfo"} <= set(params)
                        or type(params.get("protocolVersion")) is not str
                        or not isinstance(params.get("capabilities"), dict)
                        or not _valid_mcp_client_info(params.get("clientInfo"))
                        or ("_meta" in params and not isinstance(params["_meta"], dict))):
                    reply(error(rid, -32602, "Invalid params"))
                    continue
                initialized = True
                reply({"jsonrpc": "2.0", "id": rid, "result": {
                    "protocolVersion": params["protocolVersion"] if params["protocolVersion"] in protocol_versions else protocol_versions[0],
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER, "version": "1.2.0"},
                }})
            elif method == "ping":
                reply({"jsonrpc": "2.0", "id": rid, "result": {}})
            elif not ready:
                reply(error(rid, -32000, "MCP initialization required"))
            elif method == "tools/list":
                if set(params) - {"cursor", "_meta"} or ("cursor" in params and params["cursor"] != ""):
                    reply(error(rid, -32602, "Invalid params"))
                else:
                    reply({"jsonrpc": "2.0", "id": rid, "result": {"tools": []}})
            elif method == "tools/call":
                reply(error(rid, -32601, "Method not found"))
            else:
                reply(error(rid, -32601, "Method not found"))
        except (ValueError, TypeError, RecursionError):
            reply(error(rid, -32700, "Parse error"))


def run_managed_mcp_static(harness: str):
    if harness not in STATIC_MCP_ADAPTERS:
        raise Reject("unknown_harness")
    ticket = _parse_static_ticket(os.environ.get(MCP_TICKET_ENV), harness)
    if ticket is None:
        return _serve_empty_mcp()
    return run_managed_mcp(harness, ticket["mode"], ticket["budget_state"],
                           ticket["budget_nonce"], ticket["budget_limit"])


def kimi_mcp_config(value: Any) -> dict[str, Any]:
    route = _validate_assignment(value, "mcp")
    if route["adapter"] != "kimi":
        raise Reject("adapter_not_integrated")
    return {"mcpServers": {SERVER: mcp_entry(route)}}


def mimo_mcp_config_entry(value: Any) -> dict[str, Any]:
    route = _validate_assignment(value, "mcp")
    if route["adapter"] != "mimo":
        raise Reject("adapter_not_integrated")
    entry = mcp_entry(route)
    return {"type": "local", "command": [entry["command"], *entry["args"]], "enabled": True}


def cli_argv(value: Any) -> list[str]:
    route = _validate_assignment(value, "cli")
    return [route["python"], "-B", route["managed"], "cli", "--config", route["config"], "--harness", route["harness"]]


def codex_inline_table(value: Any) -> str:
    entry = mcp_entry(value)
    tools = sorted(mcp_tool_names(value))
    quoted_args = ",".join(json.dumps(item) for item in entry["args"])
    quoted_tools = ",".join(json.dumps(item) for item in tools)
    return (
        "{command=" + json.dumps(entry["command"]) + ",args=[" + quoted_args + "]"
        + ',default_tools_approval_mode="approve",enabled_tools=[' + quoted_tools + "]}"
    )


def codex_chrome_args(value: Any) -> list[str]:
    return ["-c", f"mcp_servers.{SERVER}=" + codex_inline_table(value)]


def claude_mcp_config(value: Any, office_entry: dict[str, Any] | None = None) -> tuple[dict[str, Any], list[str]]:
    servers: dict[str, Any] = {}
    if office_entry is not None:
        if not isinstance(office_entry, dict) or set(office_entry) != {"command", "args"}:
            raise Reject("office_entry")
        servers[OFFICE_SERVER] = copy.deepcopy(office_entry)
    servers[SERVER] = mcp_entry(value)
    allowed = []
    if office_entry is not None:
        allowed.append(f"mcp__{OFFICE_SERVER}__office_read")
    allowed.extend(f"mcp__{SERVER}__{name}" for name in mcp_tool_names(value))
    return {"mcpServers": servers}, allowed


def qwen_mcp_servers(value: Any, office_entry: dict[str, Any] | None = None) -> dict[str, Any]:
    servers: dict[str, Any] = {}
    if office_entry is not None:
        if not isinstance(office_entry, dict) or set(office_entry) != {"command", "args"}:
            raise Reject("office_entry")
        servers[OFFICE_SERVER] = copy.deepcopy(office_entry)
    entry = mcp_entry(value)
    servers[SERVER] = {key: entry[key] for key in ("command", "args")}
    return servers


def codebuddy_mcp_config(value: Any) -> dict[str, Any]:
    """Exact strict MCP config supported by Fleet's existing CodeBuddy argv."""
    route = _validate_assignment(value, "mcp")
    if route["adapter"] != "codebuddy":
        raise Reject("adapter_not_integrated")
    entry = mcp_entry(route)
    return {"mcpServers": {SERVER: {key: entry[key] for key in ("command", "args")}}}


def managed_server(relay_mcp, client, *, read_only: bool, budget_assignment):
    """Fixed schemas and one cross-child, per-run server-side call ceiling."""
    route = _validate_assignment(budget_assignment, "mcp")
    if "budget_state" not in route:
        raise Reject("run_budget_required")
    class CappedClient:
        def call(self, name, arguments):
            if read_only and name not in _READ_ONLY:
                raise relay_mcp.Reject("read_only")
            try:
                consume_call_budget(route["budget_state"], route["budget_nonce"],
                                    limit=route["budget_limit"])
            except Reject as exc:
                raise relay_mcp.Reject("budget") from exc
            return client.call(name, arguments)

    class FixedMCP(relay_mcp.MCP):
        def __init__(self):
            super().__init__(CappedClient())

        def handle(self, value):
            reply = super().handle(value)
            if (read_only and isinstance(value, dict) and value.get("method") == "tools/list"
                    and isinstance(reply, dict) and isinstance(reply.get("result"), dict)):
                reply["result"]["tools"] = [
                    tool for tool in reply["result"].get("tools", [])
                    if isinstance(tool, dict) and tool.get("name") in _READ_ONLY
                ]
            return reply

    return FixedMCP()


def serve_managed_mcp(relay_mcp, server, stdin, stdout):
    """Strict bounded MCP framing; uncertainty ends this owned process."""
    while True:
        line = stdin.readline(relay_mcp.MAX_BODY + 2)
        if not line:
            return 0
        if len(line) > relay_mcp.MAX_BODY + 1 or (len(line) > relay_mcp.MAX_BODY and not line.endswith(b"\n")):
            stdout.write(relay_mcp.json_bytes(server.error(None, -32700, "Message exceeds limit")) + b"\n")
            stdout.flush()
            return 1
        try:
            message = relay_mcp.parse_json(line.rstrip(b"\r\n"))
            reply = server.handle(message)
        except relay_mcp.Reject:
            reply = server.error(None, -32700, "Parse error")
        if reply is not None:
            stdout.write(relay_mcp.json_bytes(reply) + b"\n")
            stdout.flush()


def run_managed_mcp(harness: str, mode: str, budget_state: str, budget_nonce: str, budget_limit: int):
    """One leased relay SID; exact binding and shared run budget are Fleet-owned."""
    if harness not in MCP_FLEET_ADAPTERS:
        raise Reject("unknown_harness")
    if mode not in {"implement", "review"}:
        raise Reject("unknown_mode")
    binding = load_binding(Path(__file__).resolve().with_name("fleet-chrome-binding.json"))
    budget_assignment = fleet_assignment(harness, binding, mode=mode)
    budget_assignment.update({"budget_state": budget_state, "budget_nonce": budget_nonce,
                              "budget_limit": budget_limit})
    budget_assignment = _validate_assignment(budget_assignment, "mcp")
    runtime = Path(binding["managed"]).parent.resolve()
    sys.path.insert(0, str(runtime))
    modules = {}
    for name in ("broker", "mcp", "managed"):
        module = __import__(name)
        if Path(module.__file__).resolve() != runtime / (name + ".py"):
            raise Reject("runtime_identity")
        modules[name] = module
    managed, relay_mcp = modules["managed"], modules["mcp"]
    config = managed.load_config(binding["config"])
    name, enrolled = managed._enrolled(config, harness, "mcp")
    controller = managed.Controller(config)
    sid, token = controller.register(name, "mcp", managed.native_session(enrolled, os.environ))
    with managed.Lease(controller, sid, token, max(5, config.client_ttl_s // 3)):
        client = relay_mcp.Client(config.broker_base, sid, token)
        server = managed_server(relay_mcp, client, read_only=mode == "review",
                               budget_assignment=budget_assignment)
        return serve_managed_mcp(relay_mcp, server, sys.stdin.buffer, sys.stdout.buffer)


def run_managed_cli_jsonl(mode: str, budget_state: str, budget_nonce: str, budget_limit: int):
    """One long-lived fixed OpenClaw semantic proxy; no tool can choose routing."""
    if mode not in {"implement", "review"}:
        raise Reject("unknown_mode")
    binding = load_binding(Path(__file__).resolve().with_name("fleet-chrome-binding.json"))
    route = fleet_assignment("openclaw", binding, mode=mode)
    route.update({"budget_state": budget_state, "budget_nonce": budget_nonce,
                  "budget_limit": budget_limit})
    route = _validate_assignment(route, "cli")
    client = ManagedCLI(route, max_calls=budget_limit, environ=minimal_environment())
    try:
        while True:
            line = sys.stdin.buffer.readline(CLI_MAX_LINE + 1)
            if not line:
                return 0
            if len(line) > CLI_MAX_LINE or not line.endswith(b"\n"):
                return 1
            try:
                request = json.loads(line, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("constant")))
                if (not isinstance(request, dict) or set(request) != {"request_id", "name", "arguments"}
                        or type(request["request_id"]) is not str
                        or re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", request["request_id"]) is None
                        or request["name"] not in NATIVE_TO_RELAY or not isinstance(request["arguments"], dict)):
                    return 1
                result = client.call(request["name"], request["arguments"])
                payload = json.dumps({"request_id": request["request_id"], "result": result},
                                     ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
                if len(payload) > CLI_MAX_LINE:
                    return 1
                sys.stdout.buffer.write(payload)
                sys.stdout.buffer.flush()
            except (OSError, ValueError, TypeError, RecursionError, Reject):
                return 1
    finally:
        client.close()


def run_qwen_read_only():
    raise Reject("run_budget_required")


def run_codebuddy_read_only():
    raise Reject("run_budget_required")


MCP_CANDIDATE_ADAPTERS = frozenset(("codex", "claude", "qwen", "codebuddy", "kimi", "grok", "mimo", *STATIC_MCP_ADAPTERS))


def discovery_policy(adapter_id: Any, task: Any, mode: Any) -> dict[str, Any]:
    """Absent web keeps baseline; exact opt-in admits only wired constructors."""
    if not web_opt_in(task):
        return {"status": "baseline", "adapter": adapter_id}
    if type(mode) is not str or mode not in {"implement", "review"}:
        raise Reject("mode")
    if adapter_id in STATIC_MCP_ADAPTERS:
        if mode == "review":
            raise Reject("review_web_unsupported")
        return {"status": "candidate-argv", "transport": "mcp", "native_discovery": "unverified",
                "binding": "static-config-plus-fleet-ticket"}
    if adapter_id in MCP_CANDIDATE_ADAPTERS:
        return {"status": "candidate-argv", "transport": "mcp", "native_discovery": "unverified"}
    if adapter_id in {"mistral", "openclaw", "openclaw-local-qwen"}:
        return {"status": "candidate-argv", "transport": "cli", "native_discovery": "unverified"}
    key = "openclaw-local-qwen" if adapter_id in {"openclaw", "openclaw-local-qwen"} else adapter_id
    raise Reject("adapter_not_integrated")


def exact_enabled_servers(observed: Any, expected: Any) -> bool:
    if (not isinstance(observed, list) or not isinstance(expected, list)
            or any(type(item) is not str for item in observed + expected)):
        raise Reject("server_inventory")
    if len(set(observed)) != len(observed) or len(set(expected)) != len(expected):
        raise Reject("server_inventory")
    return sorted(observed) == sorted(expected)


def validate_call(name: Any, arguments: Any, *, read_only: bool = False) -> dict[str, Any]:
    """Bounded semantic request, with server-side relay validation as final gate."""
    if name not in MCP_TOOLS or not isinstance(arguments, dict):
        raise Reject("tool")
    if read_only and name not in _READ_ONLY:
        raise Reject("read_only")
    allowed = {"request_id"}
    required: set[str] = set()
    if name not in ("chrome_attachment", "chrome_open"):
        allowed.add("tab_id")
        required.add("tab_id")
    if name == "chrome_open":
        allowed.add("url")
        required.add("url")
    elif name in ("chrome_click", "chrome_type"):
        allowed.add("ref")
        required.add("ref")
        if name == "chrome_type":
            allowed.add("text")
            required.add("text")
    elif name == "chrome_wait":
        allowed.update(("role", "name", "text"))
    if set(arguments) - allowed or required - set(arguments):
        raise Reject("tool_arguments")
    result = dict(arguments)
    if "request_id" in result and (type(result["request_id"]) is not str or
            re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", result["request_id"]) is None):
        raise Reject("request_id")
    if "tab_id" in result and (type(result["tab_id"]) is not int or not 1 <= result["tab_id"] <= 2147483647):
        raise Reject("tab_id")
    if name == "chrome_open" and (type(result["url"]) is not str or not 1 <= len(result["url"]) <= 2048):
        raise Reject("url")
    if name in ("chrome_click", "chrome_type") and (
            type(result["ref"]) is not str or re.fullmatch(r"ref_[a-f0-9]{24}", result["ref"]) is None):
        raise Reject("ref")
    if name == "chrome_type" and (type(result["text"]) is not str or not 1 <= len(result["text"].encode("utf-16-le")) // 2 <= 1000):
        raise Reject("text")
    if name == "chrome_wait":
        values = [result[key] for key in ("role", "name", "text") if key in result]
        if not values or any(type(item) is not str or not 1 <= len(item.encode("utf-16-le")) // 2 <= 200 for item in values):
            raise Reject("wait")
    return result


def cli_request(name: Any, arguments: Any, *, read_only: bool = False,
                request_id: str | None = None) -> bytes:
    checked = validate_call(name, arguments, read_only=read_only)
    if request_id is None:
        request_id = f"fc-{secrets.token_hex(16)}"
    if type(request_id) is not str or re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", request_id) is None:
        raise Reject("request_id")
    checked["request_id"] = request_id
    payload = json.dumps({"name": name, "arguments": checked}, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(payload) > CLI_MAX_LINE:
        raise Reject("cli_bound")
    return payload


def minimal_environment(environ: dict[str, str] | None = None) -> dict[str, str]:
    source = os.environ if environ is None else environ
    allow = {"systemroot", "windir", "temp", "tmp"}
    return {key: value for key, value in source.items() if key.casefold() in allow}


class ManagedCLI:
    """One fixed CLI assignment per adapter run; no shell and no retry on uncertainty."""

    def __init__(self, value: Any, *, max_calls: int, popen=subprocess.Popen, environ=None):
        self.assignment = _validate_assignment(value, "cli")
        if type(max_calls) is not int or not 1 <= max_calls <= 1000:
            raise Reject("tool_budget")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.process = popen(
            cli_argv(self.assignment), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, shell=False, close_fds=True,
            creationflags=flags, env=minimal_environment(environ),
        )
        if self.process.stdin is None or self.process.stdout is None:
            self._failed = True
            self._closed = True
            try:
                self.process.terminate()
            except (OSError, AttributeError):
                pass
            raise Reject("cli_pipes")
        self.max_calls = max_calls
        self.calls = 0
        self._sequence = 0
        self._nonce = secrets.token_hex(8)
        self._lock = threading.Lock()
        self._responses: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)
        self._failed = False
        self._closed = False
        self._reader = threading.Thread(target=self._read_loop, name="fleet-managed-chrome-reader", daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        while True:
            try:
                line = self.process.stdout.readline(CLI_MAX_LINE + 1)
                self._responses.put(("line", line))
                if not line:
                    return
            except BaseException as exc:
                try:
                    self._responses.put(("error", exc))
                except BaseException:
                    pass
                return

    def _fail_stop(self) -> None:
        self._failed = True
        if self._closed:
            return
        self._closed = True
        try:
            if self.process.stdin and not self.process.stdin.closed:
                self.process.stdin.close()
        except (OSError, AttributeError):
            pass
        try:
            self.process.terminate()
            self.process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired, AttributeError):
            try:
                self.process.kill()
                self.process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired, AttributeError):
                pass

    def call(self, name: Any, arguments: Any) -> dict[str, Any]:
        with self._lock:
            if self._failed or self._closed or self.process.poll() is not None or self.calls >= self.max_calls:
                raise Reject("cli_unavailable_or_budget")
            native_name = name
            if type(name) is str and name in NATIVE_TO_RELAY:
                native_name = NATIVE_TO_RELAY[name]
            self._sequence += 1
            request_id = f"fc-{self._nonce}-{self._sequence:08x}"
            try:
                wire = cli_request(native_name, arguments,
                                   read_only=self.assignment["mode"] == "review",
                                   request_id=request_id)
                if "budget_state" not in self.assignment:
                    raise Reject("run_budget_required")
                consume_call_budget(self.assignment["budget_state"], self.assignment["budget_nonce"],
                                    limit=self.assignment["budget_limit"])
                self.calls += 1
                self.process.stdin.write(wire)
                self.process.stdin.flush()
                try:
                    kind, payload = self._responses.get(timeout=CLI_RESPONSE_TIMEOUT_S)
                except queue.Empty:
                    self._fail_stop()
                    raise Reject("cli_timeout") from None
                if kind != "line":
                    raise Reject("cli_reader")
                line = payload
                if not isinstance(line, bytes) or not line or len(line) > CLI_MAX_LINE or not line.endswith(b"\n"):
                    raise Reject("cli_response")
                result = json.loads(line, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("constant")))
                required = {"request_id", "session_id", "profile_id", "status", "data"}
                if (not isinstance(result, dict) or set(result) not in (required, required | {"tab_id"})
                        or result.get("request_id") != request_id
                        or type(result.get("session_id")) is not str
                        or re.fullmatch(r"c-[a-f0-9]{32}", result["session_id"]) is None
                        or type(result.get("profile_id")) is not str
                        or re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", result["profile_id"]) is None
                        or result.get("status") not in {"ok", "denied", "unknown"}
                        or not isinstance(result.get("data"), dict)):
                    raise Reject("cli_response")
                if "tab_id" in result and (type(result["tab_id"]) is not int or not 1 <= result["tab_id"] <= 2147483647):
                    raise Reject("cli_response")
                return result
            except (OSError, ValueError, TypeError, RecursionError, Reject):
                # A malformed, oversized, EOF, timed-out or mismatched response
                # desynchronizes this process; stop it and refuse every later call.
                self._fail_stop()
                raise Reject("cli_unknown_result") from None

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self.process.stdin and not self.process.stdin.closed:
                self.process.stdin.close()
            try:
                self.process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    self.process.terminate()
                    self.process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        self.process.kill()
                        self.process.wait(timeout=2)
                    except (OSError, subprocess.TimeoutExpired):
                        pass


if __name__ == "__main__":
    # No model-supplied paths, roles, endpoints or transport arguments.
    args = sys.argv[1:]
    if (len(args) == 3 and args[0] == "--managed-chrome-mcp-static"
            and args[1] == "--harness"):
        try:
            raise SystemExit(run_managed_mcp_static(args[2]))
        except Exception:
            # A malformed or untrusted run ticket is never printed or repaired.
            raise SystemExit(1) from None
    if (len(args) == 9 and args[0] == "--managed-chrome-jsonl"
            and args[1] == "--mode" and args[3] == "--budget-state"
            and args[5] == "--budget-nonce" and args[7] == "--budget-limit"):
        try:
            if not args[8].isdigit():
                raise Reject("budget_limit")
            raise SystemExit(run_managed_cli_jsonl(args[2], args[4], args[6], int(args[8])))
        except Exception:
            raise SystemExit(1) from None
    if (len(args) != 11 or args[0] != "--managed-chrome-mcp"
            or args[1] != "--harness" or args[3] != "--mode"
            or args[5] != "--budget-state" or args[7] != "--budget-nonce"
            or args[9] != "--budget-limit"):
        raise SystemExit(2)
    try:
        if not args[10].isdigit():
            raise Reject("budget_limit")
        raise SystemExit(run_managed_mcp(args[2], args[4], args[6], args[8], int(args[10])))
    except Exception:
        # Configuration/controller errors must not print credentials or paths.
        raise SystemExit(1) from None
