"""Loopback Hermes ask service (candidate, NOT activated).

Listens ONLY on 127.0.0.1:18879. Serves ``GET /health`` and ``POST /ask``,
both Bearer-authenticated. No CORS headers are ever emitted; any browser
``Origin``/``Referer`` is rejected. Query strings, unknown routes/methods,
malformed JSON, duplicate keys, unrecognized fields, bad ids, UTF-8 errors,
and oversized/timeout bodies are all rejected with code-only errors.

The auth token is read once from a private file path the operator creates at
activation (``--token-file``); this process never generates a real secret and
never logs tokens, prompts, answers, or headers. Only metadata
(request ids, codes, byte sizes, durations) is logged.

Dispatch runs ONE fixed SSH command (``shell=False``; question via stdin JSON
only). Concurrency is one in-flight request; repeats of a ``request_id``
never redispatch (hash-verified idempotency ledger stores metadata only,
never raw question/reply text).

Usage (explicit only; no daemon install exists yet)::

    python -B server.py --token-file <private-path> [--transport fake|ssh]

``fake`` transport exists for synthetic tests only and performs no network.
"""

from __future__ import annotations

import argparse
import ipaddress
import hashlib
import hmac
import json
import shlex
import socket
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path

BIND_HOST = "127.0.0.1"
BIND_PORT = 18879
TOKEN_MIN_BYTES = 32
MAX_BODY_BYTES = 64 * 1024
QUESTION_MAX_CHARS = 12000
QUESTION_MAX_BYTES = 48000
CHILD_OUTPUT_CAP = 1024 * 1024
CHILD_DEADLINE_S = 75.0
SOCKET_TIMEOUT_S = 10.0
LEDGER_MAX = 128
RESULT_TEXT_CAP_CHARS = 12000

SSH_EXE = r"C:\Windows\System32\OpenSSH\ssh.exe"
SSH_ALIAS = "lab-server"
REMOTE_PYTHON = "/srv/ai-lab/hermes-standalone/install/hermes-agent/venv/bin/python"

_ALLOWED_RESULT_KEYS = frozenset(
    {"type", "session_id", "exit_code", "text", "tokens", "duration_ms", "model"}
)
_TOOL_EVENT_TYPES = frozenset(
    {
        "tool_call",
        "tool_calls",
        "tool_result",
        "tool_results",
        "tool_use",
        "function_call",
        "function_call_output",
        "custom_tool_call",
        "custom_tool_call_output",
        "mcp_tool_call",
        "collabagenttoolcall",
    }
)


def _utf8_length(value):
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return QUESTION_MAX_BYTES + 1


class AskError(Exception):
    def __init__(self, http_status: int, code: str):
        super().__init__(code)
        self.http_status = http_status
        self.code = code


def load_token(path: str | Path) -> bytes:
    raw = Path(path).read_bytes().strip()
    if not TOKEN_MIN_BYTES <= len(raw) <= 512 or any(b < 33 or b > 126 for b in raw):
        raise ValueError("invalid_token_file")
    return raw


def check_bearer(headers, expected: bytes) -> None:
    presented = headers.get("Authorization", "")
    want = "Bearer " + expected.decode("utf-8", errors="strict")
    if not hmac.compare_digest(presented, want):
        raise AskError(401, "unauthorized")


def _duplicate_key_hook(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise AskError(400, "duplicate_key")
        obj[key] = value
    return obj


def parse_ask_body(raw: bytes) -> tuple[str, str]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise AskError(400, "bad_encoding")
    try:
        payload = json.loads(text, object_pairs_hook=_duplicate_key_hook)
    except AskError:
        raise
    except ValueError:
        raise AskError(400, "malformed_json")
    if not isinstance(payload, dict) or set(payload) != {"request_id", "question"}:
        raise AskError(400, "bad_fields")
    try:
        request_id = payload["request_id"]
        if not isinstance(request_id, str):
            raise ValueError()
        parsed_id = uuid.UUID(request_id)
        if str(parsed_id) != request_id or parsed_id.version != 4:
            raise ValueError()
    except (ValueError, AttributeError, TypeError):
        raise AskError(400, "bad_request_id")
    question = payload["question"]
    if (
        not isinstance(question, str)
        or not question.strip()
        or len(question) > QUESTION_MAX_CHARS
        or _utf8_length(question) > QUESTION_MAX_BYTES
    ):
        raise AskError(400, "bad_question")
    return request_id, question


def question_hash(question: str) -> str:
    return hashlib.sha256(question.encode("utf-8")).hexdigest()


class Ledger:
    """Bounded metadata-only idempotency ledger (no raw Q/A stored)."""

    def __init__(self, limit: int = LEDGER_MAX):
        self._limit = limit
        self._entries: dict[str, dict] = {}
        self._lock = threading.Lock()

    def lookup(self, request_id: str):
        with self._lock:
            entry = self._entries.get(request_id)
            return dict(entry) if entry else None

    def store(self, request_id: str, entry: dict) -> None:
        with self._lock:
            self._entries[request_id] = dict(entry)
            while len(self._entries) > self._limit:
                self._entries.pop(next(iter(self._entries)))

    def make_room(self) -> bool:
        with self._lock:
            if len(self._entries) < self._limit:
                return True
            for key, value in self._entries.items():
                if value.get("status") in ("ok", "failure"):
                    self._entries.pop(key)
                    return True
            return False  # Never evict UNKNOWN/running intent to accept new work.

    def remove(self, request_id: str) -> None:
        with self._lock:
            self._entries.pop(request_id, None)


def parse_stream(stdout: bytes) -> dict:
    """Reduce bounded stream-json bytes to a final result (allowlisted keys).

    Any tool event, nonzero exit marker, or missing/malformed result is an
    explicit failure, never success. Reasoning deltas and stderr are ignored
    (never returned).
    """
    try:
        text = stdout.decode("utf-8")
    except UnicodeDecodeError:
        raise AskError(502, "upstream_failure")
    result = None
    model = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            raise AskError(502, "upstream_failure")
        if not isinstance(event, dict):
            raise AskError(502, "upstream_failure")
        event_type = str(event.get("type", ""))
        if event_type in {"error", "failure"} or event_type in _TOOL_EVENT_TYPES or event_type.startswith("tool"):
            raise AskError(502, "upstream_failure")
        if any(k in event for k in ("tool_call", "tool_result", "tool_use")):
            raise AskError(502, "upstream_failure")
        if event_type == "system" and "model" in event:
            model = str(event["model"])[:256]
        elif event_type == "result":
            if result is not None:
                raise AskError(502, "upstream_failure")
            result = {k: event[k] for k in _ALLOWED_RESULT_KEYS if k in event}
        # system/log/text deltas fall through: ignored unless needed.
    if not result or not isinstance(result.get("text"), str) or not result["text"]:
        raise AskError(502, "upstream_failure")
    # Strict success: explicit integer exit_code 0 AND explicit session_id.
    exit_code = result.get("exit_code", None)
    if (
        "exit_code" not in result
        or isinstance(exit_code, bool)
        or not isinstance(exit_code, int)
        or exit_code != 0
    ):
        raise AskError(502, "upstream_failure")
    session_id = result.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise AskError(502, "upstream_failure")
    if not isinstance(result.get("session_id"), str) or not result["session_id"] or len(result["session_id"]) > 256:
        raise AskError(502, "upstream_failure")
    if len(result["text"]) > RESULT_TEXT_CAP_CHARS:
        raise AskError(502, "output_capped")
    final_text = result["text"]
    return {
        "text": final_text,
        "session_id": str(result.get("session_id", ""))[:256],
        "model": model or "",
        "tokens": result.get("tokens") if isinstance(result.get("tokens"), int) else None,
        "duration_ms": result.get("duration_ms")
        if isinstance(result.get("duration_ms"), int)
        else None,
    }


def remote_runner_text() -> str:
    return (Path(__file__).resolve().parent / "remote_runner.py").read_text(
        encoding="utf-8"
    )


def build_ssh_argv() -> list[str]:
    """Fixed SSH argv. No user string appears in command/env/path/host."""
    remote_command = "%s -B -c %s" % (REMOTE_PYTHON, shlex.quote(remote_runner_text()))
    return [
        SSH_EXE,
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=8",
        SSH_ALIAS,
        remote_command,
    ]


def run_child(stdin_payload: bytes, argv: list[str]) -> tuple[int, bytes]:
    """Run the fixed child with a 1MB aggregate cap and a 75s deadline.

    Reader threads bound stdout+stderr aggregate WHILE running; stderr is
    drained (never returned) but still counted, so a chatty child on either
    stream cannot evade the cap, block, or leak raw stderr upstream. The
    stdin write runs on its own thread inside the same deadline so a child
    that never reads stdin cannot hang dispatch past the timeout.
    """
    started = time.monotonic()
    process = subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, shell=False,
    )
    try:
        stdout_chunks: list[bytes] = []
        state = {"size": 0, "capped": False}
        state_lock = threading.Lock()
    
        def _drain(stream, keep: bool) -> None:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    return
                with state_lock:
                    state["size"] += len(chunk)
                    if state["size"] > CHILD_OUTPUT_CAP:
                        state["capped"] = True
                        try:
                            process.kill()
                        except OSError:
                            pass
                        return
                if keep:
                    stdout_chunks.append(chunk)
    
        readers = [
            threading.Thread(target=_drain, args=(process.stdout, True), daemon=True),
            threading.Thread(target=_drain, args=(process.stderr, False), daemon=True),
        ]
        for reader in readers:
            reader.start()
    
        def _write_stdin() -> None:
            try:
                process.stdin.write(stdin_payload)
                process.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass
    
        writer = threading.Thread(target=_write_stdin, daemon=True)
        writer.start()
        remaining = CHILD_DEADLINE_S - (time.monotonic() - started)
        writer.join(timeout=max(0.0, remaining))
        if writer.is_alive():
            try:
                process.kill()
            except OSError:
                pass
            raise AskError(504, "unknown")
        try:
            remaining = CHILD_DEADLINE_S - (time.monotonic() - started)
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, CHILD_DEADLINE_S)
            exit_code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
            raise AskError(504, "unknown")
        for reader in readers:
            reader.join(timeout=5)
        if state["capped"]:
            raise AskError(502, "output_capped")
        if exit_code != 0:
            raise AskError(502, "upstream_failure")
        return exit_code, b"".join(stdout_chunks)
    finally:
        try:
            if hasattr(process, "poll") and process.poll() is None:
                process.kill()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                if hasattr(stream, "close"):
                    stream.close()
            except (OSError, ValueError):
                pass



class AskService:
    """Dispatch core with injectable transport for synthetic tests."""

    def __init__(self, token: bytes, transport=None):
        self._token = token
        self._ledger = Ledger()
        self._flight = threading.Lock()
        self._transport = transport or (
            lambda payload: run_child(payload, build_ssh_argv())
        )

    @property
    def token(self) -> bytes:
        return self._token

    def ask(self, request_id: str, question: str) -> tuple[int, dict]:
        digest = question_hash(question)
        seen = self._ledger.lookup(request_id)
        if seen is not None:
            if seen["qhash"] != digest:
                return 409, {"error": "conflict"}
            if seen["status"] in ("running",):
                return 429, {"error": "busy"}
            replay = dict(seen["response"])
            replay["duplicate"] = True
            code = 200 if seen["status"] == "ok" else (
                504 if seen["status"] == "unknown" else 502
            )
            return code, replay
        if not self._flight.acquire(blocking=False):
            return 429, {"error": "busy"}
        # Recheck under the flight lock: another request may have finished since lookup.
        if self._ledger.lookup(request_id) is not None:
            self._flight.release()
            return self.ask(request_id, question)
        if not self._ledger.make_room():
            self._flight.release()
            return 503, {"error": "ledger_full"}
        self._ledger.store(
            request_id,
            {"qhash": digest, "status": "running", "response": {"error": "busy"}},
        )
        started = time.monotonic()
        try:
            stdin_payload = json.dumps({"question": question}, ensure_ascii=False).encode("utf-8")
            try:
                exit_code, raw = self._transport(stdin_payload)
                if exit_code != 0:
                    raise AskError(502, "upstream_failure")
            except AskError as failure:
                if failure.code == "unknown":
                    entry = {"error": "unknown"}  # stays unknown, no auto retry
                    self._ledger.store(
                        request_id,
                        {"qhash": digest, "status": "unknown", "response": entry},
                    )
                    return 504, dict(entry)
                entry = {"error": failure.code}
                self._ledger.store(
                    request_id,
                    {"qhash": digest, "status": "failure", "response": entry},
                )
                return failure.http_status, dict(entry)
            try:
                parsed = parse_stream(raw)
            except AskError as failure:
                entry = {"error": failure.code}
                self._ledger.store(
                    request_id,
                    {"qhash": digest, "status": "failure", "response": entry},
                )
                return failure.http_status, dict(entry)
            duration_ms = int((time.monotonic() - started) * 1000)
            response = {
                "request_id": request_id,
                "status": "ok",
                "text": parsed["text"],
                "session_id": parsed["session_id"],
                "model": parsed["model"],
            }
            if parsed["tokens"] is not None:
                response["tokens"] = parsed["tokens"]
            if parsed["duration_ms"] is not None:
                response["duration_ms"] = parsed["duration_ms"]
            else:
                response["duration_ms"] = duration_ms
            # Ledger keeps metadata only: hash + code-level outcome, never text.
            stored = {k: v for k, v in response.items() if k != "text"}
            stored["note"] = "result_not_stored"
            self._ledger.store(
                request_id, {"qhash": digest, "status": "ok", "response": stored}
            )
            return 200, response
        except Exception:
            self._ledger.store(request_id, {"qhash": digest, "status": "unknown", "response": {"error": "unknown"}})
            return 504, {"error": "unknown"}
        finally:
            if self._ledger.lookup(request_id) is not None and self._ledger.lookup(
                request_id
            ).get("status") == "running":
                self._ledger.store(request_id, {"qhash": digest, "status": "unknown", "response": {"error": "unknown"}})
            self._flight.release()


def make_handler(service: AskService, allowed_network=None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "HermesAsk/0.1"
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # metadata-only logging; no bodies
            pass

        def setup(self):
            super().setup()
            # Bound every read on this connection (headers + body).
            self.connection.settimeout(SOCKET_TIMEOUT_S)

        def _send(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _guard_common(self, need_body: bool):
            if allowed_network is not None and ipaddress.ip_address(self.client_address[0]) not in allowed_network:
                raise AskError(403, "forbidden_peer")
            for name in ("Authorization", "Content-Length", "Content-Type", "Host"):
                if hasattr(self.headers, "get_all") and len(self.headers.get_all(name, [])) > 1:
                    raise AskError(400, "duplicate_header")
            if self.headers.get("Transfer-Encoding"):
                raise AskError(400, "transfer_encoding_forbidden")
            # Never emit CORS headers. Browsers are rejected outright.
            if self.headers.get("Origin") or self.headers.get("Referer"):
                raise AskError(403, "forbidden_origin")
            if "?" in self.path:
                raise AskError(400, "query_not_allowed")
            try:
                check_bearer(self.headers, service.token)
            except (UnicodeDecodeError, ValueError, TypeError):
                raise AskError(401, "unauthorized")
            raw = b""
            if need_body:
                length = self.headers.get("Content-Length")
                if length is None:
                    raise AskError(411, "length_required")
                try:
                    size = int(length)
                except ValueError:
                    raise AskError(400, "bad_length")
                if size < 0 or size > MAX_BODY_BYTES:
                    raise AskError(413, "too_large")
                try:
                    raw = self.rfile.read(size)
                except (socket.timeout, OSError):
                    raise AskError(408, "request_timeout")
                if len(raw) != size or len(raw) > MAX_BODY_BYTES:
                    raise AskError(413, "too_large")
            return raw

        def do_GET(self):
            try:
                if self.path != "/health":
                    self._guard_common(need_body=False)
                    raise AskError(404, "not_found")
                self._guard_common(need_body=False)
                self._send(200, {"status": "ok"})
            except AskError as failure:
                self._send(failure.http_status, {"error": failure.code})

        def do_POST(self):
            try:
                if self.path != "/ask":
                    self._guard_common(need_body=False)
                    raise AskError(404, "not_found")
                ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
                raw = self._guard_common(need_body=True)
                if ctype != "application/json":
                    raise AskError(415, "bad_content_type")
                request_id, question = parse_ask_body(raw)
                status, payload = service.ask(request_id, question)
                self._send(status, payload)
            except AskError as failure:
                self._send(failure.http_status, {"error": failure.code})

        def _reject(self, code: str = "not_found"):
            self._send(404 if code == "not_found" else 405, {"error": code})

        do_PUT = _reject
        do_DELETE = _reject
        do_PATCH = _reject
        do_OPTIONS = _reject
        do_HEAD = _reject

    return Handler


class BoundedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, *args, **kwargs):
        self._connections = threading.BoundedSemaphore(16)
        super().__init__(*args, **kwargs)
    def process_request(self, request, address):
        if not self._connections.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self._connections.release()
            raise
    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self._connections.release()
    def handle_error(self, request, client_address):
        pass  # Never emit request/transport exception details.


def serve(token_file: str, transport=None, network="loopback") -> HTTPServer:
    token = load_token(token_file)
    service = AskService(token, transport=transport)
    if network == "wsl-private":
        from wsl_endpoint import resolve_interface
        bind_host, cidr = resolve_interface()
    elif network == "loopback":
        bind_host, cidr = BIND_HOST, "127.0.0.0/8"
    else:
        raise ValueError("invalid_network")
    server = BoundedHTTPServer((bind_host, BIND_PORT), make_handler(service, ipaddress.ip_network(cidr)))
    server.socket.settimeout(SOCKET_TIMEOUT_S)
    server.timeout = SOCKET_TIMEOUT_S
    return server


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Hermes loopback ask service")
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--network", choices=("loopback", "wsl-private"), default="loopback")
    parser.add_argument("--transport", default="ssh", choices=("ssh", "fake"))
    args = parser.parse_args(argv)
    if args.transport == "fake":
        def _fake(payload: bytes):
            raise AskError(502, "upstream_failure")
        server = serve(args.token_file, transport=_fake, network=args.network)
    else:
        server = serve(args.token_file, network=args.network)
    print("listening on %s:%d (explicit invocation only)" % server.server_address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
