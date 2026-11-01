#!/usr/bin/env python3
"""Four-tool, session-confined MCP broker over the approved Docker core.
Startup verifies code/image/ownership pins. The materializer exposes this only
in implement mode. Model arguments cannot select sessions or host paths/env.
Core child processes are hidden; Pythonw stdio has no console. Volumes remain
as evidence; normal shutdown stops only this run's created labeled container.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Environment contract -- injected at materialization time, never by the model
# ---------------------------------------------------------------------------
ENV_CORE_SOURCE = "FLEET_SANDBOX_CORE_SOURCE"
ENV_CORE_HASH = "FLEET_SANDBOX_CORE_HASH"
ENV_SESSION = "FLEET_SANDBOX_SESSION"
ENV_RUN_HOME = "FLEET_SANDBOX_RUN_HOME"
ENV_IMAGE = "FLEET_SANDBOX_IMAGE"
ENV_IMAGE_ID = "FLEET_SANDBOX_IMAGE_ID"
ENV_BROKER_HASH = "FLEET_SANDBOX_BROKER_HASH"
CORE_PATH = Path(__file__).resolve().parents[4] / "global" / "tools" / "sandbox-terminal" / "sandbox_terminal.py"
CORE_HASH = "790496b84847a5a35b09ef5e7b35bd1710b43cf9d908876db32460436daa190b"
IMAGE_NAME = "python:3.13.7-slim-bookworm"
IMAGE_ID = "sha256:444ec9cb9b03c3be7c8b18d4a5e82e7cc908e2c0032c0a6e759338a48250b4de"
DOCKER_PATH = str(Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
                  / "Programs" / "DockerDesktop" / "resources" / "bin" / "docker.exe")
DOCKER_HOST = "npipe:////./pipe/dockerDesktopLinuxEngine"
MAX_MESSAGE = 2 * 1024 * 1024

SESSION_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")

# OpenClaw tool-name hygiene: lowercase identifier, [A-Za-z0-9_-], <= 30 chars
# pre-prefix and <= 64 total after prefix; bare tool names below comply.
TOOL_NAMES: tuple[str, ...] = ("exec", "write_file", "read_file", "status")
TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "exec": {
        "description": "Run a bounded shell command inside the per-run sandbox.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {
                    "anyOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                },
                "timeout": {"type": "integer", "minimum": 1, "maximum": 120},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
    "write_file": {
        "description": "Write a file inside the per-run sandbox workspace.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
    "read_file": {
        "description": "Read a file inside the per-run sandbox workspace.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    "status": {
        "description": "Probe the per-run sandbox status without side effects.",
        "inputSchema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    },
}

MAX_OUTPUT = 64 * 1024
MAX_FILE = 1024 * 1024
MAX_TIMEOUT = 120
DEFAULT_TIMEOUT = 30


class BrokerError(Exception):
    """Raised on any breach of the run-owned pinning contract."""


# ---------------------------------------------------------------------------
# Core loader (strict)
# ---------------------------------------------------------------------------
def _has_reparse_point(path: Path) -> bool:
    try:
        attrs = path.lstat().st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(int(attrs) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _path_is_link_free(path: Path) -> bool:
    if path.is_symlink():
        return False
    for ancestor in (path, *path.parents):
        if not ancestor.exists():
            continue
        if ancestor.is_symlink() or _has_reparse_point(ancestor):
            return False
    return True


def _verify_self_hash() -> None:
    """Materializer-supplied fingerprint, checked before importing the core."""
    expected = os.environ.get(ENV_BROKER_HASH)
    if not expected or not re.fullmatch(r"[a-f0-9]{64}", expected):
        raise BrokerError("missing broker fingerprint")
    script = Path(__file__)
    if not _path_is_link_free(script):
        raise BrokerError("linked broker script")
    actual = hashlib.sha256(script.read_bytes()).hexdigest()
    if actual != expected:
        raise BrokerError(
            "fleet_sandbox broker hash mismatch: pinned="
            f"{expected[:16]} actual={actual[:16]}"
        )


def _load_core() -> Any:
    core_path_raw = os.environ.get(ENV_CORE_SOURCE)
    core_hash = os.environ.get(ENV_CORE_HASH)
    if not core_path_raw or not core_hash:
        raise BrokerError("broker missing pinned core path or hash env")
    core_path = Path(core_path_raw)
    if core_path != CORE_PATH or core_hash != CORE_HASH:
        raise BrokerError("core identity differs from the approved source")
    if not core_path.is_file():
        raise BrokerError(f"pinned core path is not a file: {core_path}")
    if not _path_is_link_free(core_path):
        raise BrokerError(f"pinned core path is a link/junction: {core_path}")
    actual_hash = hashlib.sha256(core_path.read_bytes()).hexdigest()
    if actual_hash != core_hash:
        raise BrokerError(
            "pinned core hash mismatch: pinned="
            f"{core_hash[:16]} actual={actual_hash[:16]}"
        )
    spec = importlib.util.spec_from_file_location("fleet_sandbox_core", core_path)
    if spec is None or spec.loader is None:
        raise BrokerError(f"cannot import pinned core from {core_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Sanity check: the loaded module must expose the four pinned ops.
    required = ("create", "exec_command", "write_file", "read_file", "status")
    missing = tuple(name for name in required if not hasattr(module, name))
    if missing:
        raise BrokerError(
            f"pinned core missing required ops: {', '.join(missing)}"
        )
    return module


def _load_session() -> str:
    session = os.environ.get(ENV_SESSION, "").strip()
    if not session or not SESSION_RE.fullmatch(session):
        raise BrokerError("FLEET_SANDBOX_SESSION is missing or invalid")
    return session


def _load_run_home() -> Path:
    raw = os.environ.get(ENV_RUN_HOME, "").strip()
    if not raw:
        raise BrokerError("FLEET_SANDBOX_RUN_HOME is missing")
    home = Path(raw)
    if not home.is_absolute() or not home.is_dir() or not _path_is_link_free(home):
        raise BrokerError(f"FLEET_SANDBOX_RUN_HOME is not a directory: {home}")
    return home


def _load_image_pin() -> tuple[str | None, str | None]:
    pins = (os.environ.get(ENV_IMAGE), os.environ.get(ENV_IMAGE_ID))
    if pins != (IMAGE_NAME, IMAGE_ID):
        raise BrokerError("image identity differs from the approved local image")
    return pins


# ---------------------------------------------------------------------------
# Validation of model-supplied arguments
# ---------------------------------------------------------------------------
_BOOL_TYPES: dict[str, type] = {"string": str, "integer": int}
_ARRAY_TYPES: dict[str, type] = {"array": list}


def _coerce_type(label: str, value: Any, prop: dict[str, Any]) -> Any:
    json_type = prop.get("type")
    if json_type == "string" and not isinstance(value, str):
        raise BrokerError(f"{label} must be a string")
    if json_type == "integer" and (
        isinstance(value, bool) or not isinstance(value, int)
    ):
        raise BrokerError(f"{label} must be an integer")
    if json_type == "array" and not isinstance(value, list):
        raise BrokerError(f"{label} must be an array")
    if json_type == "object" and not isinstance(value, dict):
        raise BrokerError(f"{label} must be an object")
    if json_type == "boolean" and not isinstance(value, bool):
        raise BrokerError(f"{label} must be a boolean")
    return value


def _validate_arguments(tool_name: str, arguments: Any) -> dict:
    if not isinstance(arguments, dict):
        raise BrokerError(f"{tool_name} arguments must be an object")
    schema = TOOL_SCHEMAS[tool_name]["inputSchema"]
    properties = schema.get("properties", {}) or {}
    required = schema.get("required", []) or []
    cleaned: dict = {}
    for key, value in arguments.items():
        if not isinstance(key, str):
            raise BrokerError(f"{tool_name} got non-string key in arguments")
        if key not in properties:
            raise BrokerError(f"{tool_name} got unknown argument {key!r}")
        cleaned[key] = _coerce_type(f"{tool_name}.{key}", value, properties[key])
    for key in required:
        if key not in cleaned:
            raise BrokerError(f"{tool_name} missing required argument {key!r}")
    if (
        schema.get("additionalProperties") is False
        and any(k not in properties for k in arguments)
    ):
        raise BrokerError(f"{tool_name} forbids unknown properties")
    if tool_name == "exec":
        command = cleaned["command"]
        if isinstance(command, list):
            if not command or not all(isinstance(x, str) and x and "\x00" not in x for x in command):
                raise BrokerError("command must contain nonempty strings")
        elif not isinstance(command, str) or not command.strip() or "\x00" in command:
            raise BrokerError("command must be a string or string array")
        if len(json.dumps(command).encode("utf-8")) > MAX_OUTPUT:
            raise BrokerError("command too large")
        timeout = cleaned.get("timeout", DEFAULT_TIMEOUT)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= MAX_TIMEOUT:
            raise BrokerError("invalid timeout")
    if "path" in cleaned and (not cleaned["path"] or len(cleaned["path"].encode("utf-8")) > 4096):
        raise BrokerError("invalid file path")
    if "content" in cleaned and len(cleaned["content"].encode("utf-8")) > MAX_FILE:
        raise BrokerError("file too large")
    return cleaned


def _session_only_for_run(session: str, run_home: Path) -> None:
    sentinel = run_home / ".fleet-sandbox-owned"
    if not sentinel.is_file() or not _path_is_link_free(sentinel):
        # The materializer writes a sentinel at broker spawn time. Absence
        # means someone replaced the run home out from under the broker; the
        # only safe response is to refuse.
        raise BrokerError("run home is not fleet-owned; refusing to operate")
    try:
        owner = json.loads(sentinel.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BrokerError("unreadable ownership marker") from exc
    if owner != {"session": session, "core_hash": CORE_HASH, "image_id": IMAGE_ID}:
        raise BrokerError("session/ownership marker mismatch")


# ---------------------------------------------------------------------------
# JSON-RPC stdio loop
# ---------------------------------------------------------------------------
def _result_ok(payload: Any) -> dict:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "isError": False,
    }


def _result_err(payload: Any) -> dict:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "isError": True,
    }


def _make_handler(session: str, core: Any, image_pin: tuple[str | None, str | None],
                  ensured: dict, run_home: Path) -> Callable[[str, dict], dict]:
    """Return a callable that routes one tool call to the pinned core."""

    def ensure_container() -> None:
        if ensured["done"]:
            return
        core.create(session)
        ensured["done"] = True

    def status_tool(args: dict) -> Any:
        result = core.status(session)
        result.setdefault("image_pinned", image_pin[0])
        result.setdefault("image_id_pinned", image_pin[1])
        result.setdefault("owner_session", session)
        return result

    def exec_tool(args: dict) -> Any:
        timeout = int(args.get("timeout", DEFAULT_TIMEOUT))
        if timeout < 1 or timeout > MAX_TIMEOUT:
            raise BrokerError(f"timeout must be 1..{MAX_TIMEOUT}")
        ensure_container()
        return core.exec_command(session, args["command"], timeout)

    def write_tool(args: dict) -> Any:
        ensure_container()
        result = core.write_file(session, args["path"], args["content"])
        return result

    def read_tool(args: dict) -> Any:
        # Reading is safe even before lazy create(); core refuses unlabeled
        # containers, which is exactly what we want.
        return core.read_file(session, args["path"])

    return {
        "exec": exec_tool,
        "write_file": write_tool,
        "read_file": read_tool,
        "status": status_tool,
    }  # type: ignore[return-value]


def _handle_request(req: dict, *, session: str, core: Any,
                    image_pin: tuple[str | None, str | None],
                    ensured: dict, run_home: Path) -> dict | None:
    method = req.get("method", "")
    req_id = req.get("id")
    if req.get("jsonrpc") != "2.0" or not isinstance(method, str):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
    if not isinstance(req.get("params", {}), dict):
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": "params must be an object"}}
    if method.startswith("notifications/"):
        return None
    if req_id is not None and (isinstance(req_id, bool) or not isinstance(req_id, (int, str))):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request id"}}
    if req_id is None:
        # JSON-RPC requests must have an id; notifications without ids are
        # already filtered above. Anything else is a malformed caller.
        return None
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": (req.get("params") or {})
                .get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fleet_sandbox", "version": "1.0.0"},
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method == "tools/list":
        tools = [
            {"name": name, "description": TOOL_SCHEMAS[name]["description"],
             "inputSchema": TOOL_SCHEMAS[name]["inputSchema"]}
            for name in TOOL_NAMES
        ]
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools}}
    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name", "")
        if name not in TOOL_NAMES:
            return {"jsonrpc": "2.0", "id": req_id,
                    "result": _result_err({"error": f"unknown tool: {name!r}"})}
        try:
            arguments = _validate_arguments(name, params.get("arguments"))
        except BrokerError as exc:
            return {"jsonrpc": "2.0", "id": req_id,
                    "result": _result_err({"error": str(exc)})}
        try:
            handler = _make_handler(session, core, image_pin, ensured, run_home)
        except BrokerError as exc:
            return {"jsonrpc": "2.0", "id": req_id,
                    "result": _result_err({"error": str(exc)})}
        try:
            payload = handler[name](arguments)
        except BrokerError as exc:
            return {"jsonrpc": "2.0", "id": req_id,
                    "result": _result_err({"error": str(exc)})}
        except Exception as exc:  # noqa: BLE001 - reported through MCP isError
            return {"jsonrpc": "2.0", "id": req_id,
                    "result": _result_err({"error": f"core error: {exc}"})}
        return {"jsonrpc": "2.0", "id": req_id, "result": _result_ok(payload)}
    return {"jsonrpc": "2.0", "id": req_id,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


def serve(stdin=None, stdout=None) -> int:
    """Blocking JSON-RPC stdio loop.

    ``stdin``/``stdout`` default to ``sys.stdin``/``sys.stdout``; tests pass
    ``io.StringIO`` objects. The pin checks happen once at the top; the
    per-call dispatch reads no env and trusts only what's loaded.
    """
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    _verify_self_hash()
    session = _load_session()
    run_home = _load_run_home()
    _session_only_for_run(session, run_home)
    core = _load_core()
    image_pin = _load_image_pin()
    # The core owns Docker argument validation and hidden descendants. Its
    # actual environment names must be set explicitly; FLEET_* alone is not
    # a pin. Never inherit remote Docker or source/image overrides.
    ambient = {key.casefold(): value for key, value in os.environ.items()}
    os.environ.clear()
    for key in ("SystemRoot", "WINDIR", "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PATH"):
        if key.casefold() in ambient:
            os.environ[key] = ambient[key.casefold()]
    os.environ.update(SANDBOX_TERMINAL_HOME=str(run_home),
                      SANDBOX_TERMINAL_IMAGE=image_pin[1],
                      SANDBOX_TERMINAL_DOCKER=DOCKER_PATH, DOCKER_HOST=DOCKER_HOST)
    ensured: dict = {"done": False}
    try:
        while True:
            raw = stdin.readline(MAX_MESSAGE + 1)
            if not raw:
                break
            if len(raw.encode("utf-8")) > MAX_MESSAGE:
                raise BrokerError("MCP message exceeds limit")
            line = raw.strip() if hasattr(raw, "strip") else raw
            if not line:
                continue
            try:
                req = json.loads(line)
            except json.JSONDecodeError:
                stdout.write(json.dumps({"jsonrpc": "2.0", "id": None,
                                         "error": {"code": -32700,
                                                   "message": "parse error"}}) + "\n")
                stdout.flush()
                continue
            if not isinstance(req, dict):
                stdout.write(json.dumps({"jsonrpc": "2.0", "id": None,
                                         "error": {"code": -32600,
                                                   "message": "invalid request"}}) + "\n")
                stdout.flush()
                continue
            response = _handle_request(
                req,
                session=session,
                core=core,
                image_pin=image_pin,
                ensured=ensured,
                run_home=run_home,
            )
            if response is None:
                continue
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()
    finally:
        if ensured["done"]:
            # Stop only this created labeled session, retain its named volume.
            core.status(session)
            core._docker(["stop", "-t", "1", core.container_name(session)], timeout=5)
    return 0


def main() -> int:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
