#!/usr/bin/env python3
"""Per-session Docker sandbox terminal (stdlib only, no image pull, no network)."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

LABEL_KEY = "agent-stack.sandbox-terminal"
LABEL = f"{LABEL_KEY}=1"
DEFAULT_IMAGE = "python:3.13.7-slim-bookworm"
MAX_OUTPUT = 64 * 1024
MAX_FILE = 1024 * 1024
DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 120
SESSION_RE = re.compile(r"^[a-z0-9-]{1,32}$")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class SandboxError(Exception):
    pass


def docker_bin() -> str:
    return os.environ.get("SANDBOX_TERMINAL_DOCKER") or shutil.which("docker") or "docker"


def image_name() -> str:
    return os.environ.get("SANDBOX_TERMINAL_IMAGE") or DEFAULT_IMAGE


def state_home() -> str:
    return os.environ.get("SANDBOX_TERMINAL_HOME") or os.path.join(
        os.path.expanduser("~"), ".agents", "sandbox-terminal"
    )


def validate_session(session: str) -> str:
    if not isinstance(session, str) or not SESSION_RE.match(session):
        raise SandboxError(f"invalid session name: {session!r}")
    return session


def container_name(session: str) -> str:
    return f"sbx-{validate_session(session)}"


def volume_name(session: str) -> str:
    return f"sbx-{validate_session(session)}-ws"


def _run(args, *, input_bytes=None, timeout=None, check=False):
    try:
        return subprocess.run(
            args,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            creationflags=_NO_WINDOW,
        )
    except OSError as exc:
        raise SandboxError(f"docker executable not available: {args[0]}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise SandboxError("docker command timed out") from exc


def _docker(args, **kw):
    return _run([docker_bin(), *args], **kw)


def _inspect(name: str):
    r = _docker(["inspect", name])
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout.decode("utf-8", "replace"))
        return data[0] if data else None
    except Exception:
        return None


def _require_labeled(session: str) -> dict:
    name = container_name(session)
    info = _inspect(name)
    if info is None:
        raise SandboxError(f"session not found: {session}")
    labels = ((info.get("Config") or {}).get("Labels") or {})
    if labels.get(LABEL_KEY) != "1":
        raise SandboxError(f"refusing to operate on unlabeled container: {name}")
    return info


def validate_workspace_path(path: str) -> str:
    """Return normalized absolute path under /workspace or raise SandboxError."""
    if not isinstance(path, str) or not path or "\x00" in path:
        raise SandboxError("invalid path")
    # Reject traversal explicitly (also caught by normalization below).
    parts_raw = path.replace("\\", "/").split("/")
    if ".." in parts_raw:
        raise SandboxError("path traversal rejected")
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        candidate = path.replace("\\", "/")
    else:
        candidate = "/workspace/" + path.replace("\\", "/")
    # Normalize . and duplicate slashes without resolving symlinks.
    parts = []
    for part in candidate.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise SandboxError("path traversal rejected")
        parts.append(part)
    normalized = "/" + "/".join(parts)
    if normalized != "/workspace" and not normalized.startswith("/workspace/"):
        raise SandboxError("path outside /workspace rejected")
    return normalized


def truncate_output(data: bytes):
    if data is None:
        data = b""
    if len(data) > MAX_OUTPUT:
        return data[:MAX_OUTPUT], True
    return data, False


# ---------------------------------------------------------------- operations
def _volume_labels(result) -> dict:
    """Labels of one inspected volume; any unexpected payload shape fails closed."""
    try:
        data = json.loads(result.stdout.decode("utf-8", "replace"))
    except ValueError:
        raise SandboxError("unreadable docker volume inspect output") from None
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise SandboxError("unexpected docker volume inspect output")
    labels = data[0].get("Labels")
    if labels is None:
        return {}
    if not isinstance(labels, dict):
        raise SandboxError("unexpected docker volume labels")
    return labels


def _volume_missing(result) -> bool:
    """True only when inspect consistently reports that the volume does not exist.

    Docker prints `[]` on stdout and "no such volume" on stderr with a non-zero
    exit for a missing volume; any other stdout is ambiguous and not "missing".
    """
    if result.returncode == 0 or b"no such volume" not in (result.stderr or b"").lower():
        return False
    try:
        return json.loads((result.stdout or b"").decode("utf-8", "replace") or "null") == []
    except ValueError:
        return False


def _ensure_workspace_volume(session: str) -> bool:
    """Create the session volume if needed. Returns True if created here.

    Never adopts (and later chowns or destroys) a same-named volume this tool
    did not label: an ambiguous inspect fails closed, and the label is checked
    again after `volume create`, which succeeds on an existing volume too.
    """
    vol = volume_name(session)
    found = _docker(["volume", "inspect", vol])
    created = False
    if found.returncode != 0:
        if not _volume_missing(found):
            raise SandboxError(f"docker volume inspect failed: {vol}")
        r = _docker(["volume", "create", "--label", LABEL, vol])
        if r.returncode != 0:
            raise SandboxError(r.stderr.decode("utf-8", "replace").strip() or "docker volume create failed")
        found = _docker(["volume", "inspect", vol])
        if found.returncode != 0:
            raise SandboxError(f"docker volume inspect failed after create: {vol}")
        created = True
    if _volume_labels(found).get(LABEL_KEY) != "1":
        # Created concurrently by someone else or pre-existing: not ours, never cleaned up.
        raise SandboxError(f"volume name already used by unlabeled volume: {vol}")
    return created


def _chown_workspace_volume(session: str) -> None:
    """Make the volume root writable by uid 10001.

    A fresh Docker named volume is owned by root, while the session
    container runs as 10001:10001 with a read-only rootfs, so without this
    step nothing could ever write to /workspace. This must run AFTER the
    session container is created: while the volume is still empty (no
    files), Docker re-populates it from the image at session container
    creation and resets the root directory to root ownership, undoing any
    earlier chown. A disposable one-off container (same local image, no
    network, CAP_CHOWN only) chowns the volume root instead; the session
    container itself never runs as root.
    """
    vol = volume_name(session)
    args = [
        "run", "--rm",
        "--pull", "never",
        "--label", LABEL,
        "--network", "none",
        "--user", "0:0",
        "--read-only",
        "--cap-drop", "ALL",
        "--cap-add", "CHOWN",
        "--security-opt", "no-new-privileges",
        "--pids-limit", "16",
        "--memory", "128m",
        "--mount", f"type=volume,source={vol},target=/workspace",
        image_name(),
        "chown", "10001:10001", "/workspace",
    ]
    r = _docker(args)
    if r.returncode != 0:
        raise SandboxError(r.stderr.decode("utf-8", "replace").strip() or "workspace volume init failed")


def create(session: str) -> dict:
    validate_session(session)
    name = container_name(session)
    vol = volume_name(session)
    existing = _inspect(name)
    if existing is not None:
        labels = ((existing.get("Config") or {}).get("Labels") or {})
        if labels.get(LABEL_KEY) != "1":
            raise SandboxError(f"container name already used by unlabeled container: {name}")
        # Heal volumes created before the ownership fix: re-assert ownership.
        _ensure_workspace_volume(session)
        _chown_workspace_volume(session)
        return status(session)
    created_volume = _ensure_workspace_volume(session)
    args = [
        "run", "-d",
        "--pull", "never",
        "--name", name,
        "--label", LABEL,
        "--network", "none",
        "--user", "10001:10001",
        "--read-only",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--pids-limit", "128",
        "--memory", "512m",
        "--cpus", "1",
        "--mount", f"type=volume,source={vol},target=/workspace",
        "--workdir", "/workspace",
        image_name(),
        "sleep", "infinity",
    ]
    r = _docker(args)
    if r.returncode == 0:
        try:
            _chown_workspace_volume(session)
        except SandboxError:
            _docker(["rm", "-f", name])
            if created_volume:
                _docker(["volume", "rm", "-f", vol])
            raise
        return status(session)
    if created_volume:
        _docker(["volume", "rm", "-f", vol])
    raise SandboxError(r.stderr.decode("utf-8", "replace").strip() or "docker run failed")


def status(session: str) -> dict:
    info = _require_labeled(session)
    state = info.get("State") or {}
    return {
        "session": session,
        "container": container_name(session),
        "volume": volume_name(session),
        "image": (info.get("Config") or {}).get("Image"),
        "running": bool(state.get("Running")),
        "status": state.get("Status", ""),
        "labels": ((info.get("Config") or {}).get("Labels") or {}),
    }


def list_sessions() -> dict:
    r = _docker(["ps", "-a", "--filter", f"label={LABEL}", "--format", "{{.Names}}"])
    if r.returncode != 0:
        raise SandboxError(r.stderr.decode("utf-8", "replace").strip() or "docker ps failed")
    sessions = []
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if line.startswith("sbx-"):
            sessions.append(line[4:])
    return {"sessions": sorted(sessions)}


def destroy(session: str) -> dict:
    validate_session(session)
    _require_labeled(session)  # refuse unlabeled containers
    name = container_name(session)
    vol = volume_name(session)
    # Decide about the volume BEFORE removing anything: a same-named volume this
    # tool did not label (e.g. swapped in while the container was stopped) is
    # never deleted, and an ambiguous inspect refuses the whole destroy.
    inspected = _docker(["volume", "inspect", vol])
    if inspected.returncode != 0:
        if not _volume_missing(inspected):
            raise SandboxError(f"refusing destroy: volume inspect failed: {vol}")
        remove_volume = False  # already gone; only the container remains
    elif _volume_labels(inspected).get(LABEL_KEY) != "1":
        raise SandboxError(f"refusing destroy: unlabeled volume: {vol}")
    else:
        remove_volume = True
    r = _docker(["rm", "-f", name])
    if r.returncode != 0:
        raise SandboxError(r.stderr.decode("utf-8", "replace").strip() or "docker rm failed")
    removed = None
    if remove_volume:
        rv = _docker(["volume", "rm", vol])
        if rv.returncode != 0:
            raise SandboxError(rv.stderr.decode("utf-8", "replace").strip() or "docker volume rm failed")
        removed = vol
    return {"session": session, "container_removed": name,
            "volume_removed": removed, "destroyed": True}


def _normalize_command(command):
    if isinstance(command, str):
        if not command:
            raise SandboxError("empty command")
        return ["sh", "-c", command]
    if isinstance(command, (list, tuple)) and command and all(isinstance(c, str) for c in command):
        return list(command)
    raise SandboxError("command must be a string or list of strings")


def exec_command(session: str, command, timeout: int = DEFAULT_TIMEOUT) -> dict:
    _require_labeled(session)
    if timeout is None:
        timeout = DEFAULT_TIMEOUT
    try:
        timeout = int(timeout)
    except Exception as exc:
        raise SandboxError("invalid timeout") from exc
    if timeout < 1 or timeout > MAX_TIMEOUT:
        raise SandboxError(f"timeout must be 1..{MAX_TIMEOUT}")
    argv = _normalize_command(command)
    name = container_name(session)
    # Inner `timeout` (coreutils in the Debian image) kills the in-container
    # process; outer timeout guards a hung docker client.
    wrapped = ["timeout", "-s", "KILL", str(timeout), *argv]
    try:
        proc = subprocess.Popen(
            [docker_bin(), "exec", name, *wrapped],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=_NO_WINDOW,
        )
    except OSError as exc:
        raise SandboxError(f"docker executable not available: {exc}") from exc
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout + 10)
        if proc.returncode in (124, 137):
            timed_out = True
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            proc.kill()
        except Exception:
            pass
        try:
            out, err = proc.communicate(timeout=5)
        except Exception:
            out, err = b"", b""
        # Best-effort kill of any remaining in-container processes (not PID 1).
        try:
            _docker(["exec", name, "sh", "-c",
                     "for p in /proc/[0-9]*; do pid=${p#/proc/}; "
                     "[ \"$pid\" != \"1\" ] && kill -9 $pid 2>/dev/null; done; true"],
                    timeout=5)
        except Exception:
            pass
    out_b, out_trunc = truncate_output(out or b"")
    err_b, err_trunc = truncate_output(err or b"")
    return {
        "session": session,
        "exit_code": None if timed_out else proc.returncode,
        "timed_out": timed_out,
        "stdout": out_b.decode("utf-8", "replace"),
        "stderr": err_b.decode("utf-8", "replace"),
        "truncated": bool(out_trunc or err_trunc),
        "stdout_truncated": out_trunc,
        "stderr_truncated": err_trunc,
    }


_WRITE_SCRIPT = (
    "import sys,os; p=sys.argv[1]; d=sys.stdin.buffer.read(); "
    "os.makedirs(os.path.dirname(p) or '/', exist_ok=True); "
    "open(p,'wb').write(d); print(len(d))"
)
_READ_SCRIPT = (
    "import sys; d=open(sys.argv[1],'rb').read(); "
    "sys.stdout.buffer.write(d)"
)


def write_file(session: str, path: str, content) -> dict:
    _require_labeled(session)
    dest = validate_workspace_path(path)
    if isinstance(content, str):
        data = content.encode("utf-8")
    elif isinstance(content, (bytes, bytearray)):
        data = bytes(content)
    else:
        raise SandboxError("content must be a string")
    if len(data) > MAX_FILE:
        raise SandboxError("file exceeds 1 MiB limit")
    if dest == "/workspace":
        raise SandboxError("path must name a file under /workspace")
    r = _docker(["exec", "-i", container_name(session), "python3", "-c", _WRITE_SCRIPT, dest],
                input_bytes=data)
    if r.returncode != 0:
        raise SandboxError(r.stderr.decode("utf-8", "replace").strip() or "write_file failed")
    return {"session": session, "path": dest, "size": len(data)}


def read_file(session: str, path: str) -> dict:
    _require_labeled(session)
    dest = validate_workspace_path(path)
    if dest == "/workspace":
        raise SandboxError("path must name a file under /workspace")
    r = _docker(["exec", container_name(session), "python3", "-c", _READ_SCRIPT, dest])
    if r.returncode != 0:
        raise SandboxError(r.stderr.decode("utf-8", "replace").strip() or "read_file failed")
    if len(r.stdout) > MAX_FILE:
        raise SandboxError("file exceeds 1 MiB limit")
    return {"session": session, "path": dest, "size": len(r.stdout),
            "content": r.stdout.decode("utf-8", "replace")}


# ---------------------------------------------------------------- MCP stdio
TOOLS = [
    {"name": "create", "description": "Create a sandbox session container",
     "inputSchema": {"type": "object", "properties": {"session": {"type": "string"}}, "required": ["session"]}},
    {"name": "exec", "description": "Run a command in a sandbox session",
     "inputSchema": {"type": "object", "properties": {
         "session": {"type": "string"},
         "command": {"anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
         "timeout": {"type": "integer"}}, "required": ["session", "command"]}},
    {"name": "write_file", "description": "Write a file under /workspace",
     "inputSchema": {"type": "object", "properties": {
         "session": {"type": "string"}, "path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["session", "path", "content"]}},
    {"name": "read_file", "description": "Read a file under /workspace",
     "inputSchema": {"type": "object", "properties": {
         "session": {"type": "string"}, "path": {"type": "string"}},
        "required": ["session", "path"]}},
    {"name": "status", "description": "Session status",
     "inputSchema": {"type": "object", "properties": {"session": {"type": "string"}}, "required": ["session"]}},
    {"name": "destroy", "description": "Destroy a labeled session and its volume",
     "inputSchema": {"type": "object", "properties": {"session": {"type": "string"}}, "required": ["session"]}},
    {"name": "list", "description": "List labeled sessions",
     "inputSchema": {"type": "object", "properties": {}}},
]


def _call_tool(name: str, args: dict) -> dict:
    args = args or {}
    if name == "create":
        return create(args["session"])
    if name == "exec":
        return exec_command(args["session"], args["command"], args.get("timeout", DEFAULT_TIMEOUT))
    if name == "write_file":
        return write_file(args["session"], args["path"], args["content"])
    if name == "read_file":
        return read_file(args["session"], args["path"])
    if name == "status":
        return status(args["session"])
    if name == "destroy":
        return destroy(args["session"])
    if name == "list":
        return list_sessions()
    raise SandboxError(f"unknown tool: {name}")


def serve_mcp(stdin=None, stdout=None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        method = req.get("method", "")
        req_id = req.get("id")
        if method.startswith("notifications/"):
            continue
        if req_id is None:
            continue
        if method == "initialize":
            result = {
                "protocolVersion": (req.get("params") or {}).get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "sandbox-terminal", "version": "1.0.0"},
            }
            resp = {"jsonrpc": "2.0", "id": req_id, "result": result}
        elif method == "ping":
            resp = {"jsonrpc": "2.0", "id": req_id, "result": {}}
        elif method == "tools/list":
            resp = {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}
        elif method == "tools/call":
            params = req.get("params") or {}
            try:
                result = _call_tool(params.get("name", ""), params.get("arguments") or {})
                resp = {"jsonrpc": "2.0", "id": req_id, "result": {
                    "content": [{"type": "text", "text": json.dumps(result)}],
                    "isError": False}}
            except Exception as exc:  # noqa: BLE001 - reported to MCP client
                resp = {"jsonrpc": "2.0", "id": req_id, "result": {
                    "content": [{"type": "text", "text": json.dumps({"error": str(exc)})}],
                    "isError": True}}
        else:
            resp = {"jsonrpc": "2.0", "id": req_id,
                    "error": {"code": -32601, "message": f"method not found: {method}"}}
        stdout.write(json.dumps(resp) + "\n")
        stdout.flush()
    return 0


# ---------------------------------------------------------------- CLI
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="sandbox_terminal")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for cmd in ("create", "status", "destroy"):
        p = sub.add_parser(cmd); p.add_argument("session")
    p = sub.add_parser("exec"); p.add_argument("session")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    p.add_argument("command", nargs=argparse.REMAINDER)
    for cmd in ("write_file", "write-file"):
        p = sub.add_parser(cmd); p.add_argument("session"); p.add_argument("path")
        p.add_argument("content", nargs="?", default=None)
        p.add_argument("--content-file", default=None)
    for cmd in ("read_file", "read-file"):
        p = sub.add_parser(cmd); p.add_argument("session"); p.add_argument("path")
    sub.add_parser("list")
    sub.add_parser("mcp")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "mcp":
            return serve_mcp()
        if args.cmd == "create":
            result = create(args.session)
        elif args.cmd == "status":
            result = status(args.session)
        elif args.cmd == "destroy":
            result = destroy(args.session)
        elif args.cmd == "list":
            result = list_sessions()
        elif args.cmd == "exec":
            cmd = args.command
            if cmd and cmd[0] == "--":
                cmd = cmd[1:]
            if not cmd:
                raise SandboxError("exec requires a command")
            # Single-token invocation is treated as a shell string for convenience.
            command = cmd[0] if len(cmd) == 1 else cmd
            result = exec_command(args.session, command, args.timeout)
        elif args.cmd in ("write_file", "write-file"):
            if args.content_file:
                with open(args.content_file, "rb") as fh:
                    data = fh.read()
                if len(data) > MAX_FILE:
                    raise SandboxError("file exceeds 1 MiB limit")
                content = data.decode("utf-8")
            elif args.content is not None:
                content = args.content
            else:
                content = sys.stdin.read()
            result = write_file(args.session, args.path, content)
        elif args.cmd in ("read_file", "read-file"):
            result = read_file(args.session, args.path)
        else:  # pragma: no cover
            raise SandboxError(f"unknown command {args.cmd}")
    except SandboxError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    except KeyError as exc:
        print(json.dumps({"ok": False, "error": f"missing argument: {exc}"}))
        return 1
    print(json.dumps({"ok": True, **result} if isinstance(result, dict) else {"ok": True, "result": result}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
