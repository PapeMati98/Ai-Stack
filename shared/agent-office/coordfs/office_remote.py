"""Bounded OpenSSH transport for the isolated Agent Office W2 protocol.

The caller supplies transport settings from trusted local configuration.  This
module never reads an identity key and never passes a remote command to ssh.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable


MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
MAX_OPERATION_TIMEOUT_S = 240.0
MAX_CALL_TIMEOUT_S = 300.0
MAX_RELAY_WATCH_TIMEOUT_S = 3600
MAX_RELAY_WATCH_CALL_TIMEOUT_S = 3670.0
IO_CHUNK_BYTES = 64 * 1024

ALLOWED_OPS = frozenset(
    {
        "presence",
        "read",
        "read-correlated-replies",
        "send",
        "ack",
        "wait",
        "who",
        "channels",
        "delivery",
        "wake-expect",
        "wake-consume",
        "wake-close",
        "wake-relay-watch",
        "wake-relay-pending",
        "wake-relay-claim",
        "wake-relay-receipt",
        "wake-relay-observe",
        "wake-relay-defer-busy",
        "wake-relay-native-begin",
        "wake-relay-native-unsubmitted",
        "wake-relay-ack",
        "wake-desktop-input-claim",
        "wake-desktop-input-release",
        "wake-list",
        "wake-task-cancel",
        "wake-correlations",
        "wake-pull-disarm",
        "wake-stop-status",
        "wake-ledger",
        "wake-cli-pending",
        "wake-cli-claim",
        "wake-cli-start",
        "wake-cli-receipt",
        "tool-call",
        "mcp",
    }
)

# Blocking MCP tools of the closed unary tool-call: their timeout_s argument is a
# response-time hint, bounded exactly like the local wait (zero polls once).
# office_ask writes its question first and then blocks for the reply; without the
# hint the client would time out after the write and report a transport failure.
TOOL_CALL_BLOCKING_TOOLS = frozenset(
    {"office_ask", "office_wait", "office_project_wait", "office_project_wait_channel"}
)

_HOST_RE = re.compile(r"(?:[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?|\[[0-9A-Fa-f:.]+\])\Z")
_USER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,63}\Z")


def _config_text(value: str | os.PathLike[str], label: str, *, reject_comma: bool = False) -> str:
    try:
        result = os.fspath(value)
    except TypeError:
        raise ValueError(f"invalid {label} configuration") from None
    if not isinstance(result, str) or not result or "\x00" in result:
        raise ValueError(f"invalid {label} configuration")
    if reject_comma and "," in result:
        # OpenSSH treats commas in UserKnownHostsFile as a list separator.
        raise ValueError(f"invalid {label} configuration")
    return result


@dataclass(frozen=True)
class OfficeRemoteConfig:
    """Trusted, fixed connection settings; key material stays with OpenSSH."""

    host: str
    user: str
    identity_file: str | os.PathLike[str]
    known_hosts_file: str | os.PathLike[str]
    ssh_executable: str | os.PathLike[str] = "ssh"
    port: int = 22
    connect_timeout_s: int = 10
    request_timeout_s: float = 30.0

    def __post_init__(self) -> None:
        if not isinstance(self.host, str) or not _HOST_RE.fullmatch(self.host):
            raise ValueError("invalid host configuration")
        if not isinstance(self.user, str) or not _USER_RE.fullmatch(self.user):
            raise ValueError("invalid user configuration")
        object.__setattr__(self, "identity_file", _config_text(self.identity_file, "identity path"))
        object.__setattr__(
            self,
            "known_hosts_file",
            _config_text(self.known_hosts_file, "known-hosts path", reject_comma=True),
        )
        object.__setattr__(self, "ssh_executable", _config_text(self.ssh_executable, "ssh executable"))
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not 1 <= self.port <= 65535:
            raise ValueError("invalid port configuration")
        if (
            isinstance(self.connect_timeout_s, bool)
            or not isinstance(self.connect_timeout_s, int)
            or not 1 <= self.connect_timeout_s <= 60
        ):
            raise ValueError("invalid connect timeout configuration")
        if not _valid_timeout(self.request_timeout_s, MAX_CALL_TIMEOUT_S):
            raise ValueError("invalid request timeout configuration")


def _valid_timeout(value: Any, maximum: float) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0 < value <= maximum
    )


def require_native_connection_paths(config: OfficeRemoteConfig) -> None:
    """Native launch has no executable search or checkout-relative credential paths.

    A custom Python transport factory is a trusted adapter seam, not an RPC
    field. Native CLI/MCP and the default subprocess transport always enforce
    these paths without reading key material or using PATH/shutil.which.
    """
    for field in ("ssh_executable", "identity_file", "known_hosts_file"):
        text = os.fspath(getattr(config, field))
        path = Path(text)
        # Windows treats either slash kind as a separator, including mixed
        # UNC/device prefixes. Normalize only for this rejection check.
        if (not path.is_absolute() or text.replace("\\", "/").startswith("//")
                or re.search(r"%[^%]+%|\$\{|\$[A-Za-z_]", text)
                or any(character in text for character in ("\r", "\n"))
                or any(part in {".", ".."} for part in path.parts)):
            raise ValueError("native SSH connection requires explicit absolute paths")


def _tool_call_wait_hint(args: Mapping[str, Any]) -> float | None:
    """Blocking hint of the closed unary tool-call; zero is a legal local poll."""

    name = args.get("name")
    arguments = args.get("arguments")
    if not isinstance(name, str) or not isinstance(arguments, Mapping):
        raise ValueError("tool-call requires a tool name and an arguments object")
    if name not in TOOL_CALL_BLOCKING_TOOLS:
        return None
    hint = arguments.get("timeout_s", 30)
    if (
        isinstance(hint, bool)
        or not isinstance(hint, (int, float))
        or not math.isfinite(hint)
        or not 0 <= hint <= MAX_OPERATION_TIMEOUT_S
    ):
        raise ValueError("wait timeout is out of bounds")
    return float(hint)


def build_request(op: str, identity: Mapping[str, Any], args: Mapping[str, Any]) -> bytes:
    """Serialize one newline-framed JSON request, including its size bound."""

    if not isinstance(op, str) or op not in ALLOWED_OPS:
        raise ValueError("operation is not allowed")
    if not isinstance(identity, Mapping) or not isinstance(args, Mapping):
        raise ValueError("identity and args must be JSON objects")

    # identity is a caller-declared claim, not the authenticated SSH principal.
    request = {"v": 1, "op": op, "identity": dict(identity), "args": dict(args)}
    try:
        encoded = json.dumps(request, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        raise ValueError("request is not valid JSON") from None
    frame = encoded + b"\n"
    if len(frame) > MAX_REQUEST_BYTES:
        raise ValueError("request exceeds size limit")
    return frame


def _read_chunks(stream: BinaryIO, size: int = IO_CHUNK_BYTES) -> bytes:
    read1 = getattr(stream, "read1", None)
    return read1(size) if callable(read1) else stream.read(size)


def _write_all(stream: BinaryIO, data: bytes | memoryview) -> None:
    view = memoryview(data)
    while view:
        written = stream.write(view)
        if written is None:
            return
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _degraded(reason: str) -> dict[str, str]:
    # Keep transport failures deliberately generic; stderr and request data can
    # contain private details and are never copied into the result.
    return {"status": "degraded", "reason": reason}


def _clean_child_environment() -> dict[str, str]:
    child_env = dict(os.environ)
    # The gate rejects SSH_ORIGINAL_COMMAND.  No SSH config is loaded and this
    # local variable is never forwarded as an SSH environment request.
    child_env.pop("SSH_ORIGINAL_COMMAND", None)
    return child_env


class OfficeRemoteClient:
    """One SSH handshake per request, or one long-lived MCP stdio relay."""

    def __init__(
        self,
        config: OfficeRemoteConfig,
        *,
        popen_factory: Callable[..., Any] | None = None,
    ) -> None:
        if popen_factory is None:
            require_native_connection_paths(config)
        self.config = config
        self._popen = popen_factory or subprocess.Popen

    def _argv(self) -> list[str]:
        config = self.config
        return [
            os.fspath(config.ssh_executable),
            "-T",
            "-F",
            "none",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={config.known_hosts_file}",
            "-o",
            "GlobalKnownHostsFile=none",
            "-o",
            "UpdateHostKeys=no",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "IdentityAgent=none",
            "-o",
            "PreferredAuthentications=publickey",
            "-o",
            "PasswordAuthentication=no",
            "-o",
            "KbdInteractiveAuthentication=no",
            "-o",
            f"ConnectTimeout={config.connect_timeout_s}",
            "-o",
            "ControlMaster=no",
            "-o",
            "ControlPath=none",
            "-o",
            "ClearAllForwardings=yes",
            "-o",
            "ForwardAgent=no",
            "-o",
            "ForwardX11=no",
            "-o",
            "PermitLocalCommand=no",
            "-i",
            os.fspath(config.identity_file),
            "-p",
            str(config.port),
            "--",
            f"{config.user}@{config.host}",
        ]

    def _start(self) -> Any | None:
        try:
            return self._popen(
                self._argv(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                shell=False,
                close_fds=True,
                bufsize=0,
                env=_clean_child_environment(),
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            return None

    @staticmethod
    def _stop(process: Any) -> None:
        try:
            process.kill()
        except (OSError, ProcessLookupError):
            pass
        try:
            process.wait(timeout=1.0)
        except (OSError, subprocess.SubprocessError, TimeoutError):
            pass

    @staticmethod
    def _close_pipes(process: Any) -> None:
        for stream in (getattr(process, "stdin", None), getattr(process, "stdout", None)):
            if stream is None:
                continue
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    @staticmethod
    def _response_timeout(
        op: str,
        args: Mapping[str, Any],
        configured_timeout_s: float,
        requested_timeout_s: float | None,
        connect_timeout_s: int,
    ) -> float:
        timeout = float(configured_timeout_s)
        maximum_call = MAX_RELAY_WATCH_CALL_TIMEOUT_S if op == "wake-relay-watch" else MAX_CALL_TIMEOUT_S
        wait_hints: list[float] = []
        if op == "wait":
            for name in ("timeout_s", "timeout", "timeout_seconds"):
                hint = args.get(name)
                if hint is not None:
                    if not _valid_timeout(hint, MAX_OPERATION_TIMEOUT_S):
                        raise ValueError("wait timeout is out of bounds")
                    wait_hints.append(float(hint))
        if op == "tool-call":
            hint = _tool_call_wait_hint(args)
            if hint is not None:
                wait_hints.append(hint)
        if op == "wake-relay-watch":
            watch_timeout = args.get("timeout_s", MAX_RELAY_WATCH_TIMEOUT_S)
            if type(watch_timeout) is not int or not 1 <= watch_timeout <= MAX_RELAY_WATCH_TIMEOUT_S:
                raise ValueError("watch timeout is out of bounds")
            # A scheduled relay must remain in the same background command for
            # the complete watch. A short client timeout would reawaken its
            # model every few minutes despite an event-driven server wait.
            # The single SSH connect budget is added once, with every hint below.
            wait_hints.append(float(watch_timeout + 10))
        if requested_timeout_s is not None:
            if not _valid_timeout(requested_timeout_s, maximum_call):
                raise ValueError("request timeout is out of bounds")
            timeout = float(requested_timeout_s)
        if wait_hints:
            timeout = max(timeout, max(wait_hints) + connect_timeout_s)
        if timeout > maximum_call:
            raise ValueError("request timeout is out of bounds")
        return timeout

    def request(
        self,
        op: str,
        identity: Mapping[str, Any],
        args: Mapping[str, Any],
        *,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """Send one JSON request and parse exactly one bounded JSON object."""

        frame = build_request(op, identity, args)
        if op == "mcp":
            raise ValueError("use relay_mcp for the streaming MCP operation")
        timeout = self._response_timeout(
            op,
            args,
            self.config.request_timeout_s,
            timeout_s,
            self.config.connect_timeout_s,
        )
        process = self._start()
        if process is None:
            return _degraded("ssh_unavailable")

        captured = bytearray()
        state = {"overflow": False, "read_error": False}

        def collect_stdout() -> None:
            try:
                while True:
                    # Retain at most the limit plus one byte; drain excess data
                    # so a full stdout pipe cannot deadlock the child process.
                    read_size = min(IO_CHUNK_BYTES, MAX_RESPONSE_BYTES + 1 - len(captured))
                    if read_size <= 0:
                        read_size = IO_CHUNK_BYTES
                        state["overflow"] = True
                    chunk = process.stdout.read(read_size)
                    if not chunk:
                        break
                    remaining = MAX_RESPONSE_BYTES + 1 - len(captured)
                    if len(chunk) > remaining:
                        captured.extend(chunk[:remaining])
                        state["overflow"] = True
                    else:
                        captured.extend(chunk)
                    if len(captured) > MAX_RESPONSE_BYTES:
                        state["overflow"] = True
            except Exception:
                state["read_error"] = True

        reader = threading.Thread(target=collect_stdout, name="office-remote-response", daemon=True)
        reader.start()
        try:
            _write_all(process.stdin, frame)
            process.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            self._stop(process)
            reader.join(timeout=1.0)
            self._close_pipes(process)
            return _degraded("ssh_unavailable")

        try:
            return_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._stop(process)
            reader.join(timeout=1.0)
            self._close_pipes(process)
            return _degraded("timeout")
        except (OSError, subprocess.SubprocessError):
            self._stop(process)
            reader.join(timeout=1.0)
            self._close_pipes(process)
            return _degraded("ssh_unavailable")

        reader.join(timeout=1.0)
        self._close_pipes(process)
        if return_code not in (0, 1):
            return _degraded("ssh_unavailable")
        if reader.is_alive() or state["read_error"] or state["overflow"]:
            return _degraded("invalid_response")
        try:
            response = json.loads(bytes(captured).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _degraded("invalid_response")
        if not isinstance(response, dict):
            return _degraded("invalid_response")
        if return_code == 1:
            # The forced gate uses exit 1 for a semantic refusal, while OpenSSH
            # connection failures use 255. Preserve only the gate's deliberately
            # sanitized error schema; never leak unexpected diagnostic fields.
            if (set(response) != {"status", "code"} or response.get("status") != "error"
                    or not isinstance(response.get("code"), str)
                    or re.fullmatch(r"[a-z0-9_]{1,64}", response["code"]) is None):
                return _degraded("invalid_response")
        return response

    def relay_mcp(
        self,
        identity: Mapping[str, Any],
        args: Mapping[str, Any] | None = None,
        *,
        input_stream: BinaryIO | None = None,
        output_stream: BinaryIO | None = None,
    ) -> dict[str, str]:
        """Relay MCP bytes in bounded chunks after the newline-framed JSON header."""

        frame = build_request("mcp", identity, {} if args is None else args)
        process = self._start()
        if process is None:
            return _degraded("ssh_unavailable")
        source = input_stream if input_stream is not None else sys.stdin.buffer
        sink = output_stream if output_stream is not None else sys.stdout.buffer
        relay_errors: list[str] = []

        def upload() -> None:
            try:
                _write_all(process.stdin, frame)
                while True:
                    chunk = _read_chunks(source)
                    if not chunk:
                        break
                    _write_all(process.stdin, chunk)
            except (BrokenPipeError, OSError, ValueError):
                relay_errors.append("input")
            except Exception:
                relay_errors.append("input")
            finally:
                try:
                    process.stdin.close()
                except (OSError, ValueError):
                    pass

        def download() -> None:
            try:
                while True:
                    chunk = _read_chunks(process.stdout)
                    if not chunk:
                        break
                    _write_all(sink, chunk)
                    flush = getattr(sink, "flush", None)
                    if callable(flush):
                        flush()
            except Exception:
                relay_errors.append("output")
                self._stop(process)

        uploader = threading.Thread(target=upload, name="office-remote-mcp-input", daemon=True)
        downloader = threading.Thread(target=download, name="office-remote-mcp-output", daemon=True)
        uploader.start()
        downloader.start()
        try:
            return_code = process.wait()
        except (OSError, subprocess.SubprocessError):
            self._stop(process)
            return _degraded("ssh_unavailable")

        # The SSH process closing its pipes ends the output pump.  The input
        # pump is daemonized because a local stdio producer may still be blocked
        # in read(); it retains no session history beyond its current chunk.
        downloader.join(timeout=1.0)
        uploader.join(timeout=0.1)
        if return_code != 0:
            return _degraded("ssh_unavailable")
        if relay_errors or downloader.is_alive():
            return _degraded("relay_error")
        return {"status": "ok"}
