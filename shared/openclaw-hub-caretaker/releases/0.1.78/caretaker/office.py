"""Office connector wrapper.

Scoped target/session, rate bound, dedupe, reply correlation, no autoplay
reply loops. The wrapper mirrors the canonical ``OfficeConnector`` shape
from ``evidence-input/clawops`` but uses only the Python standard library
and an injectable transport so the slice never spawns the real runtime.
"""

from __future__ import annotations

import hashlib
import json
import locale
import math
import os
import subprocess
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class OfficeIdentity:
    harness: str
    host: str
    session_id: str

    def validate(self) -> None:
        if not (self.harness and self.host and self.session_id):
            raise ValueError("office identity fields must be non-empty")
        if any("\0" in s for s in (self.harness, self.host, self.session_id)):
            raise ValueError("office identity fields must not contain NUL")


@dataclass(frozen=True)
class OfficeSendRequest:
    dedup_id: str
    body: str
    kind: str  # "note" | "question" | "handoff"
    to: Optional[str] = None
    channel: Optional[str] = None
    reply_to: Optional[str] = None


@dataclass(frozen=True)
class OfficeSendResult:
    status: str             # "sent" | "deduplicated" | "offline" | "rejected"
    dedup_id: Optional[str] = None
    message_id: Optional[str] = None
    reason: Optional[str] = None


class OfficeTransport:
    """Abstract transport; tests inject a fake."""

    def send(self, request: OfficeSendRequest, *,
             identity: OfficeIdentity) -> OfficeSendResult:
        raise NotImplementedError


class FakeOfficeTransport(OfficeTransport):
    def __init__(self, *, responses: Optional[List[OfficeSendResult]] = None):
        self._responses = list(responses or [])
        self.calls: List[Dict[str, Any]] = []

    def send(self, request: OfficeSendRequest, *,
             identity: OfficeIdentity) -> OfficeSendResult:
        self.calls.append({
            "dedup_id": request.dedup_id,
            "kind": request.kind,
            "to": request.to,
            "channel": request.channel,
            "reply_to": request.reply_to,
            "body_bytes": len(request.body.encode("utf-8")),
            "identity": identity,
        })
        if not self._responses:
            return OfficeSendResult(status="offline", reason="no_response")
        idx = min(len(self.calls) - 1, len(self._responses) - 1)
        return self._responses[idx]


OfficeRunner = Callable[..., Tuple[int, str, str]]


def _default_office_runner(*, executable: str, argv: Sequence[str],
                           timeout: int) -> Tuple[int, str, str]:
    try:
        child_env = os.environ.copy()
        # ``office.cmd`` owns the Python invocation on Windows. Keep its
        # canonical wrapper while forcing UTF-8 in the child interpreter.
        child_env["PYTHONUTF8"] = "1"
        completed = subprocess.run(
            [executable, *argv], capture_output=True, timeout=timeout,
            check=False, env=child_env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OSError(f"office wrapper failed: {exc}") from exc
    return (completed.returncode, _decode_output(completed.stdout),
            _decode_output(completed.stderr))


def _decode_output(data: bytes) -> str:
    """Decode wrapper output without corrupting non-ASCII JSON on Windows."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode(locale.getpreferredencoding(False), errors="replace")


class CanonicalOfficeArgvTransport(OfficeTransport):
    """Invoke the installed ``office.cmd`` wrapper with the real CLI shape.

    The runner is injectable for tests. The default path never invents a
    delivery result: it accepts only the canonical ``status=ok, code=sent``
    response and returns an offline/rejected result for every other outcome.
    """

    MAX_OUTPUT_BYTES = 256 * 1024

    def __init__(self, *, executable: str, home: str, harness: str,
                 session_id: str, timeout: int = 30,
                 python_executable: str = "", script: str = "",
                 run_callable: Optional[OfficeRunner] = None):
        if not executable or not home or not harness or not session_id:
            raise ValueError("office transport requires executable, home and identity")
        self.executable = executable
        self.home = home
        self.harness = harness
        self.session_id = session_id
        self.python_executable = str(python_executable or "")
        self.script = str(script or "")
        self.timeout = max(1, int(timeout))
        self._run = run_callable or _default_office_runner

    @staticmethod
    def _agent_id(identity: OfficeIdentity) -> str:
        return f"{identity.harness}-{identity.host}-{identity.session_id}"

    def _argv(self, request: OfficeSendRequest,
              identity: OfficeIdentity) -> List[str]:
        argv = [
            "--home", self.home,
            "--harness", identity.harness,
            "--session-id", identity.session_id,
            "--agent", self._agent_id(identity),
            "send",
        ]
        if request.to is not None:
            argv.extend(("--to", request.to))
        elif request.channel is not None:
            argv.extend(("--channel", request.channel))
        else:
            raise ValueError("office request requires to or channel")
        argv.extend(("--body", request.body, "--kind", request.kind))
        if request.reply_to:
            argv.extend(("--reply-to", request.reply_to))
        return argv

    def send(self, request: OfficeSendRequest, *,
             identity: OfficeIdentity) -> OfficeSendResult:
        identity.validate()
        try:
            argv = self._argv(request, identity)
            executable = self.executable
            # Direct Python mode is useful for an explicit script transport
            # and makes the encoding contract testable. The configured
            # ``office.cmd`` path remains the production canonical wrapper.
            if (self.python_executable and self.script
                    and Path(self.executable).suffix.lower() in {".py", ".pyw"}):
                executable = self.python_executable
                argv = ["-X", "utf8", self.script, *argv]
            code, stdout, stderr = self._run(
                executable=executable, argv=argv, timeout=self.timeout,
            )
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            return OfficeSendResult(status="offline", dedup_id=request.dedup_id,
                                     reason=f"transport_failed:{type(exc).__name__}")
        except Exception as exc:
            # An injected or wrapper implementation failure is an offline
            # result; never let it become an implicit successful send.
            return OfficeSendResult(status="offline", dedup_id=request.dedup_id,
                                    reason=f"transport_failed:{type(exc).__name__}")
        if not isinstance(stdout, str) or not isinstance(stderr, str):
            return OfficeSendResult(status="offline", dedup_id=request.dedup_id,
                                    reason="response_not_text")
        if len(stdout.encode("utf-8", errors="replace")) > self.MAX_OUTPUT_BYTES:
            return OfficeSendResult(status="offline", dedup_id=request.dedup_id,
                                    reason="response_too_large")
        if code != 0:
            detail = (stderr or stdout).strip().replace("\r", " ").replace("\n", " ")
            return OfficeSendResult(status="offline", dedup_id=request.dedup_id,
                                    reason=f"wrapper_exit:{code}:{detail[:256]}")
        try:
            payload = json.loads(stdout or "")
        except json.JSONDecodeError:
            return OfficeSendResult(status="offline", dedup_id=request.dedup_id,
                                    reason="response_not_json")
        if not isinstance(payload, dict):
            return OfficeSendResult(status="offline", dedup_id=request.dedup_id,
                                    reason="response_not_object")
        status = payload.get("status")
        result_code = payload.get("code")
        if status == "ok" and result_code == "sent":
            message_id = payload.get("id")
            if not isinstance(message_id, str) or not message_id.strip():
                return OfficeSendResult(status="offline", dedup_id=request.dedup_id,
                                        reason="sent_response_missing_id")
            return OfficeSendResult(status="sent", dedup_id=request.dedup_id,
                                    message_id=message_id)
        if status == "error" and result_code in {"invalid_target", "invalid_agent",
                                                   "invalid_body", "invalid_kind",
                                                   "invalid_reply_to", "secret_detected"}:
            return OfficeSendResult(status="rejected", dedup_id=request.dedup_id,
                                    reason=str(result_code))
        return OfficeSendResult(status="offline", dedup_id=request.dedup_id,
                                reason=f"office_{status or 'unknown'}:{result_code or 'unknown'}")


class RateLimiter:
    """Simple sliding-window rate limiter."""

    MAX_BYTES = 256 * 1024
    MAX_VALUES = 4096

    def __init__(self, *, max_per_minute: int,
                 path: Optional[str | os.PathLike[str]] = None):
        if max_per_minute < 1:
            raise ValueError("rate_per_minute must be >= 1")
        self.max = max_per_minute
        self.window: Deque[float] = deque()
        self.path = Path(path) if path else None
        self._load()

    def _load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            if self.path.stat().st_size > self.MAX_BYTES:
                raise ValueError("rate ledger exceeds size cap")
            values = json.loads(self.path.read_text("utf-8"))
            if not isinstance(values, list) or len(values) > self.MAX_VALUES:
                raise ValueError("rate ledger must be a list")
            parsed = []
            for value in values:
                if (isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not math.isfinite(float(value))):
                    raise ValueError("rate ledger contains an invalid timestamp")
                parsed.append(float(value))
            self.window.extend(parsed[-self.max:])
        except (OSError, UnicodeError, ValueError, TypeError):
            # A corrupt limiter must fail closed for this process rather than
            # silently discarding the persisted rate history.
            self.window.extend([time.time()] * self.max)

    def _persist(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(self.path, list(self.window))

    def allow(self, now: Optional[float] = None) -> bool:
        t = now if now is not None else time.time()
        cutoff = t - 60.0
        while self.window and self.window[0] < cutoff:
            self.window.popleft()
        if len(self.window) >= self.max:
            return False
        self.window.append(t)
        self._persist()
        return True


@dataclass
class OfficeDedupStore:
    """Stable-id dedup store. Mirrors the canonical shape."""

    records: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    path: Optional[str | os.PathLike[str]] = None
    load_error: Optional[str] = field(default=None, init=False)

    MAX_BYTES = 1024 * 1024
    MAX_RECORDS = 4096

    def __post_init__(self) -> None:
        if self.path is None:
            return
        path = Path(self.path)
        if not path.is_file():
            return
        try:
            if path.stat().st_size > self.MAX_BYTES:
                raise ValueError("dedup ledger exceeds size cap")
            loaded = json.loads(path.read_text("utf-8"))
            if (isinstance(loaded, dict) and len(loaded) <= self.MAX_RECORDS
                    and all(isinstance(k, str) and isinstance(v, dict)
                            and isinstance(v.get("payload_hash"), str)
                            and isinstance(v.get("recorded_at"), str)
                            for k, v in loaded.items())):
                self.records.update(loaded)
            else:
                raise ValueError("dedup ledger shape invalid")
        except (OSError, UnicodeError, ValueError, TypeError):
            # A damaged dedup ledger must fail closed. Sending anyway could
            # duplicate an Office message after a crash or torn write.
            self.load_error = "dedup_ledger_unreadable"

    def _persist(self) -> None:
        if self.path is None:
            return
        path = Path(self.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(path, self.records)

    def get(self, dedup_id: str) -> Optional[Dict[str, Any]]:
        return self.records.get(dedup_id)

    def put(self, dedup_id: str, payload_hash: str,
            message_id: Optional[str], recorded_at: str,
            reply_to: Optional[str] = None) -> None:
        self.records[dedup_id] = {
            "dedup_id": dedup_id,
            "payload_hash": payload_hash,
            "message_id": message_id,
            "recorded_at": recorded_at,
            "reply_to": reply_to,
        }
        self._persist()

    def reserve(self, dedup_id: str, payload_hash: str,
                recorded_at: str, reply_to: Optional[str] = None) -> None:
        """Persist an in-flight send before touching the external wrapper."""
        self.records[dedup_id] = {
            "dedup_id": dedup_id,
            "payload_hash": payload_hash,
            "message_id": None,
            "recorded_at": recorded_at,
            "reply_to": reply_to,
            "status": "pending",
        }
        self._persist()

    def has_message_id(self, message_id: str) -> bool:
        return any(row.get("message_id") == message_id
                   for row in self.records.values())


def canonicalize(value: Any) -> str:
    if value is None or isinstance(value, (str, int, float, bool)):
        return json_primitive(value)
    if isinstance(value, list):
        return "[" + ",".join(canonicalize(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(
            json_primitive(k) + ":" + canonicalize(value[k])
            for k in sorted(value.keys())) + "}"
    return json_primitive(repr(value))


def json_primitive(v: Any) -> str:
    import json
    return json.dumps(v, sort_keys=False, ensure_ascii=False)


def _atomic_write_json(path: Path, value: Any) -> None:
    """Replace one task-owned ledger atomically after a complete write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        # Preserve an unknown state for the next load; never claim delivery.
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def payload_hash(request: OfficeSendRequest) -> str:
    h = hashlib.sha256()
    payload = {
        "id": request.dedup_id,
        "body": request.body,
        "kind": request.kind,
        "to": request.to,
        "channel": request.channel,
        "reply_to": request.reply_to,
    }
    h.update(canonicalize(payload).encode("utf-8"))
    return h.hexdigest()


class OfficeConnector:
    """Scoped Office boundary.

    - Requires an explicit identity and scoped target.
    - Rate bound via ``RateLimiter``; over-limit sends return ``offline``.
    - Dedup keyed by ``dedup_id``; payload-hash mismatch is ``id_conflict``.
    - No autoplay reply loops: a send that includes ``reply_to`` matching a
      previous outbound message of the same ``dedup_id`` is rejected.
    """

    MAX_BODY_BYTES = 16 * 1024
    ALLOWED_KINDS = ("note", "question", "handoff")

    def __init__(self, *, identity: OfficeIdentity, transport: OfficeTransport,
                 dedup: OfficeDedupStore, rate: RateLimiter,
                 no_autoplay_reply: bool = True,
                 now: Optional[Callable[[], str]] = None):
        identity.validate()
        self.identity = identity
        self.transport = transport
        self.dedup = dedup
        self.rate = rate
        self.no_autoplay_reply = no_autoplay_reply
        self._now = now or (lambda: time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        self._reply_correlation: Dict[str, str] = {}

    def _scoped_id(self, dedup_id: str) -> str:
        return (f"{self.identity.harness}:{self.identity.host}:"
                f"{self.identity.session_id}:{dedup_id}")

    def publish(self, request: OfficeSendRequest) -> OfficeSendResult:
        if self.dedup.load_error:
            return OfficeSendResult(status="offline", reason=self.dedup.load_error,
                                    dedup_id=request.dedup_id or None)
        if not request.dedup_id or "\0" in request.dedup_id:
            return OfficeSendResult(status="rejected", reason="invalid_request",
                                    dedup_id=request.dedup_id or None)
        if request.kind not in self.ALLOWED_KINDS:
            return OfficeSendResult(status="rejected", reason="invalid_request",
                                    dedup_id=request.dedup_id)
        if (not isinstance(request.body, str) or not request.body
                or "\0" in request.body):
            return OfficeSendResult(status="rejected", reason="invalid_request",
                                    dedup_id=request.dedup_id)
        if len(request.body.encode("utf-8")) > self.MAX_BODY_BYTES:
            return OfficeSendResult(status="rejected",
                                    reason="invalid_request",
                                    dedup_id=request.dedup_id)
        if (request.to is None) == (request.channel is None):
            return OfficeSendResult(status="rejected", reason="invalid_request",
                                    dedup_id=request.dedup_id)
        if request.to is not None and not request.to.strip():
            return OfficeSendResult(status="rejected", reason="invalid_request",
                                    dedup_id=request.dedup_id)
        if request.channel is not None and not request.channel.strip():
            return OfficeSendResult(status="rejected", reason="invalid_request",
                                     dedup_id=request.dedup_id)
        if request.reply_to is not None and (
                not isinstance(request.reply_to, str) or "\0" in request.reply_to):
            return OfficeSendResult(status="rejected", reason="invalid_request",
                                    dedup_id=request.dedup_id)
        if (self.no_autoplay_reply and request.reply_to
                and (self._reply_correlation.get(request.dedup_id)
                     == request.reply_to
                     or self.dedup.has_message_id(request.reply_to))):
            return OfficeSendResult(status="rejected",
                                    reason="autoplay_loop",
                                    dedup_id=request.dedup_id)

        digest = payload_hash(request)
        scoped_id = self._scoped_id(request.dedup_id)
        existing = self.dedup.get(scoped_id)
        if existing is not None:
            if existing["payload_hash"] != digest:
                return OfficeSendResult(status="rejected",
                                        reason="id_conflict",
                                        dedup_id=request.dedup_id)
            if existing.get("status") == "pending":
                return OfficeSendResult(status="offline",
                                        reason="delivery_unknown",
                                        dedup_id=request.dedup_id)
            return OfficeSendResult(status="deduplicated",
                                    dedup_id=request.dedup_id,
                                    message_id=existing.get("message_id"))

        if not self.rate.allow():
            return OfficeSendResult(status="offline", reason="rate_limited",
                                    dedup_id=request.dedup_id)

        try:
            # Reserve before the external write. A crash after the wrapper
            # commits but before ``put`` then fails closed instead of sending
            # a duplicate on the next process invocation.
            self.dedup.reserve(scoped_id, digest, self._now(), request.reply_to)
        except Exception as exc:
            return OfficeSendResult(status="offline",
                                    reason=f"dedup_store_failed:{type(exc).__name__}",
                                    dedup_id=request.dedup_id)
        try:
            result = self.transport.send(request, identity=self.identity)
        except Exception as exc:
            return OfficeSendResult(status="offline",
                                    reason=f"transport_failed:{type(exc).__name__}",
                                    dedup_id=request.dedup_id)
        if not isinstance(result, OfficeSendResult):
            return OfficeSendResult(status="offline",
                                    reason="transport_invalid_result",
                                    dedup_id=request.dedup_id)
        if result.status == "sent":
            if not result.message_id:
                return OfficeSendResult(status="offline",
                                        reason="sent_missing_message_id",
                                        dedup_id=request.dedup_id)
            try:
                self.dedup.put(scoped_id, digest, result.message_id,
                               self._now(), request.reply_to)
            except Exception as exc:
                # The external message may already exist; leave the pending
                # reservation in place and force manual reconciliation.
                return OfficeSendResult(status="offline",
                                        reason=f"dedup_store_failed:{type(exc).__name__}",
                                        dedup_id=request.dedup_id)
            if request.reply_to:
                self._reply_correlation[request.dedup_id] = request.reply_to
        return result
