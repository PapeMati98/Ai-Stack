"""Native Codex MCP identity binding for the Agent Office stdio server.

A Codex app-server exposes the authoritative native thread UUID on the real
MCP wire as ``params._meta.threadId``, but an Office built without a native
session id falls back to ``process-fallback`` (``strength=weak``) and would
otherwise read an empty fallback inbox. This module binds that weak Codex
identity to the native thread UUID on the first valid ``tools/call``.

Routing claim only: the metadata selects which inbox this server reads and
which sender its messages carry. It is never cryptographic authentication and
grants no additional authority. Only ``params._meta.threadId`` is read; a
sender/session override inside tool arguments is never accepted (tool
arguments are validated separately before this gate runs).
"""

from __future__ import annotations

import re
import threading
from typing import Any

#: Source recorded on the backend identity after a native bind.
NATIVE_SOURCE = "mcp-native-thread-meta"
#: The only harness eligible for auto-binding.
NATIVE_HARNESS = "codex"
#: The only weak source eligible for auto-binding.
WEAK_SOURCE = "process-fallback"

#: Canonical 36-character UUID ``8-4-4-4-12`` hex, case-insensitive on input.
THREAD_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)


def normalize_thread_id(value: Any) -> str | None:
    """Canonical lower-case UUID, or None when `value` is not one."""
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if THREAD_ID_RE.match(text) is None:
        return None
    return text


def extract_thread_id(params: Any) -> tuple[str | None, str]:
    """The native thread id from wire params.

    Returns ``(normalized, "ok")``, ``(None, "missing")`` or
    ``(None, "invalid")``. Only ``params._meta.threadId`` is read; tool
    arguments (including any project ``thread_id`` stream field) never
    contribute.
    """
    meta = params.get("_meta") if isinstance(params, dict) else None
    if not isinstance(meta, dict) or "threadId" not in meta:
        return None, "missing"
    normalized = normalize_thread_id(meta["threadId"])
    if normalized is None:
        return None, "invalid"
    return normalized, "ok"


def is_weak_codex_fallback(identity: Any) -> bool:
    """True only for an explicitly weak process-fallback Codex identity."""
    if not isinstance(identity, dict):
        return False
    return (
        identity.get("harness") == NATIVE_HARNESS
        and identity.get("source") == WEAK_SOURCE
        and identity.get("strength") == "weak"
    )


def bind_office_to_thread(office: Any, thread_id: str) -> str:
    """Bind an eligible Office backend to `thread_id` in place and return the id.

    The new agent id reuses the existing harness/host with the native UUID as
    the session; home, clock, transport principal, profile, channels, peers
    and every contractor counter stay untouched. The same Office object is
    mutated (no remote backend is replaced with a local one); when the backend
    carries no harness/host to bind with, ValueError is raised so the caller
    reports a visible error instead of a fake success.
    """
    identity = getattr(office, "identity", None)
    harness = identity.get("harness") if isinstance(identity, dict) else None
    host = identity.get("host") if isinstance(identity, dict) else None
    if not harness or not host:
        raise ValueError("office backend has no harness/host to bind a native thread to")
    agent_id = f"{harness}-{host}-{thread_id}"
    office.identity = {
        "agent_id": agent_id,
        "harness": harness,
        "host": host,
        "session": thread_id,
        "strength": "strong",
        "source": NATIVE_SOURCE,
    }
    office.agent_id = agent_id
    return agent_id


def _error(agent: Any, op: str, code: str, message: str) -> dict[str, Any]:
    return {"status": "error", "code": code, "agent": agent, "op": op,
            "message": message}


class NativeIdentityGate:
    """Thread-safe pin for one MCP server's native Codex identity.

    Eligibility is latched at construction from the initial backend identity,
    so the bind itself (which flips the backend to strong) never disables the
    pin. At most one thread id is ever bound; concurrent attempts race on one
    lock and exactly one wins.
    """

    def __init__(self, identity: Any) -> None:
        self.active = is_weak_codex_fallback(identity)
        self._lock = threading.Lock()
        self._thread_id: str | None = None
        self.just_bound = False

    @property
    def bound(self) -> str | None:
        with self._lock:
            return self._thread_id

    def defers_heartbeat(self) -> bool:
        """True while a weak Codex server must not yet advertise presence."""
        return self.active and self.bound is None

    def evaluate_wire(self, office: Any, params: Any, op: str) -> dict[str, Any] | None:
        """Bind-or-check for a wire ``tools/call``. None when it may proceed."""
        if not self.active:
            return None
        with self._lock:
            thread_id, state = extract_thread_id(params)
            if self._thread_id is None:
                if state == "missing":
                    return _error(
                        office.agent_id, op, "native_identity_required",
                        "this Codex session has no bound native identity: send "
                        "params._meta.threadId (canonical UUID) on every tools/call",
                    )
                if state == "invalid":
                    return _error(
                        office.agent_id, op, "native_identity_invalid",
                        "params._meta.threadId must be a canonical 36-character UUID",
                    )
                try:
                    bind_office_to_thread(office, thread_id)
                except ValueError as exc:
                    return _error(
                        office.agent_id, op, "native_identity_unsupported", str(exc))
                self._thread_id = thread_id
                self.just_bound = True
                return None
            if state == "missing":
                return _error(
                    office.agent_id, op, "native_identity_required",
                    "this Codex session is bound to a native thread: send the same "
                    "params._meta.threadId on every tools/call",
                )
            if state == "invalid":
                return _error(
                    office.agent_id, op, "native_identity_invalid",
                    "params._meta.threadId must be a canonical 36-character UUID",
                )
            if thread_id != self._thread_id:
                return _error(
                    office.agent_id, op, "native_identity_mismatch",
                    "params._meta.threadId names a different native thread than the "
                    "one bound to this server; it is refused before any operation",
                )
            return None

    def evaluate_direct(self, office: Any, op: str) -> dict[str, Any] | None:
        """In-process ``call_tool`` without wire metadata. None: may proceed."""
        if not self.active:
            return None
        with self._lock:
            if self._thread_id is None:
                return _error(
                    office.agent_id, op, "native_identity_required",
                    "this Codex session has no bound native identity: send "
                    "params._meta.threadId (canonical UUID) on every tools/call",
                )
            return None

    def pop_bind_event(self) -> bool:
        """True once per binding, so the server starts its heartbeat exactly once."""
        with self._lock:
            if self.just_bound:
                self.just_bound = False
                return True
            return False
