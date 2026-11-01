"""Deterministic Codex Desktop relay cycle.

The cycle owns all Office decisions: it issues exact, pinned tool arguments
and forwards only the raw MCP ``CallToolResult`` payloads that the host feeds
back.  It performs no model reasoning, no extra queueing, no native IPC, no
secret reads, no transcription of turn contents and no extra sends per cycle.

Transport layout
----------------
The script is one process driven by an external host (PowerShell wrapper).  It
reads newline-framed JSON from stdin and writes newline-framed JSON to stdout.
An output ``{"action":"call", "token":..., "tool":..., "arguments":...}`` is
the host's cue to invoke exactly one MCP tool and feed the raw result back
as ``{"token":..., "result":<raw CallToolResult>}``.  Any other JSON line on
stdin ends the cycle.  The terminal output is ``{"action":"done", "status":...,
"reason":...}`` -- ``status`` is one of ``no_work``, ``delivered``, ``deferred``,
``unknown`` or ``error``.

Office calls use :class:`OfficeRemoteClient` with the fixed Codex host identity
so SSH framing stays identical to the rest of the office family.  When
``--binding-id`` is omitted, active verified ``codex-desktop-relay`` bindings
from ``wake-list`` are merged with the Core ids (``codex-core``,
``codex-hub-root``, ``codex-rozwoj``), capped at 16, and any discovery error
falls back to those Core ids. A host may explicitly select enrolled bindings
with ``--binding-id``. The driver remains ``codex-desktop-relay``. A pending dispatch
that the previous cycle fenced but did not settle is reconciled through the
``owned_dispatches`` block returned by ``wake-relay-pending``; it is never
resent.

Deadlines and budgets
---------------------
The cycle has a single overall deadline (default 240 s); 15 s are reserved for
shutdown so no work begins in the last 15 s.  Each ``OfficeRemoteClient`` call
must finish within the remaining budget.  ``wait_threads`` and ``read_thread``
app-tool waits are bounded to 45 s of host wall-clock so a stuck Codex session
never starves the cycle.  Stdin close / EOF ends the cycle as
``{"status":"unknown","reason":"stdin_closed"}`` (the host intentionally
withholds new results); bad JSON ends it as ``error``.

No transcripts
--------------
``read_thread.turns[*].items`` is discarded on the way out.  Only turn ids,
timestamps and the thread id / cwd verification reach Office.  ``send_message``
is issued at most once per cycle.  The Office short wake prompt template is the
only text the cycle may place on a ``send_message`` ``prompt`` argument and it
must originate from a known pending dispatch -- never fabricated.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import math
import queue
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping

if __package__ in (None, ''):
    # -I removes cwd; discover only this installed script's own trusted runtime.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from coordfs.office_cli_remote import load_remote_config
    from coordfs.office_remote import OfficeRemoteClient, require_native_connection_paths
else:
    from .office_cli_remote import load_remote_config
    from .office_remote import OfficeRemoteClient, require_native_connection_paths


# Minimal protocol for Office RPC: any object with a ``request`` method
# matching the OfficeRemoteClient contract can drive it.  The cycle only
# depends on the method shape; the production wiring still routes through
# ``OfficeRemoteClient``.
OfficeRPC = Any


DRIVER = "codex-desktop-relay"
SEND_TOOL = "mcp__codex_app__send_message_to_thread"
READ_TOOL = "mcp__codex_app__read_thread"
WAIT_TOOL = "mcp__codex_app__wait_threads"

# Fixed Codex host identity.  One Codex harness session, no per-launch drift.
HARNESS = "codex"
HOST = "ailab"
SESSION_ID = "01a10b42-94e2-78e1-a908-c6549d6b9668"
AGENT_ID = f"{HARNESS}-{HOST}-{SESSION_ID}"

# Default Core bindings. Explicit host configuration may select project bindings;
# the authoritative Office binding/claim gates still control native delivery.
BINDING_IDS: tuple[str, ...] = ("codex-core", "codex-hub-root", "codex-rozwoj")
_BINDING_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}\Z")


def validate_binding_ids(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError("binding_ids must be a list of distinct binding ids")
    bindings = tuple(values)
    if (not 1 <= len(bindings) <= 16
            or any(not isinstance(value, str) or _BINDING_ID_RE.fullmatch(value) is None
                   for value in bindings)
            or len(set(bindings)) != len(bindings)):
        raise ValueError("binding_ids must contain 1..16 distinct valid binding ids")
    return bindings


def discover_active_codex_bindings(client, identity) -> tuple[str, ...]:
    """Active verified Codex relay bindings merged with the Core defaults.

    Discovery order is kept, then any missing ``BINDING_IDS``. The result is
    capped at 16. A transport, shape, or validation error returns ``BINDING_IDS``.
    """
    try:
        discovered: list[str] = []
        seen: set[str] = set()
        offset = 0
        now_ts = time.time()
        for _ in range(100):
            response = client.request("wake-list", identity, {"limit": 10, "offset": offset})
            if not isinstance(response, Mapping):
                return BINDING_IDS
            if response.get("status", "ok") != "ok":
                return BINDING_IDS
            rows = response.get("bindings")
            if not isinstance(rows, list):
                return BINDING_IDS
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                if (row.get("driver") != DRIVER or row.get("status") != "active"
                        or row.get("verified") is not True):
                    continue
                expires_at = row.get("expires_at")
                if isinstance(expires_at, str) and expires_at:
                    try:
                        exp_ts = datetime.fromisoformat(
                            f"{expires_at[:-1]}+00:00" if expires_at.endswith("Z") else expires_at
                        ).timestamp()
                        if exp_ts <= now_ts:
                            continue
                    except ValueError:
                        continue
                binding_id = row.get("binding_id")
                if (not isinstance(binding_id, str) or binding_id in seen
                        or _BINDING_ID_RE.fullmatch(binding_id) is None):
                    continue
                seen.add(binding_id)
                discovered.append(binding_id)
            following = response.get("next_offset")
            if not response.get("has_more") or type(following) is not int or following <= offset:
                break
            offset = following
        merged: list[str] = []
        merged_seen: set[str] = set()
        for binding_id in (*discovered, *BINDING_IDS):
            if binding_id in merged_seen:
                continue
            merged_seen.add(binding_id)
            merged.append(binding_id)
            if len(merged) == 16:
                break
        return validate_binding_ids(merged)
    except Exception:
        return BINDING_IDS

# Deadlines.
CYCLE_DEADLINE_S = 240.0
DEADLINE_RESERVE_S = 15.0
MAX_TOOL_WAIT_S = 45.0
MAX_OFFICE_CALL_S = 60.0
DEFAULT_WATCH_TIMEOUT_S = 5

# Argument schemas enforced verbatim before the host is asked to run the tool.
_SEND_ARG_KEYS = frozenset({"threadId", "hostId", "prompt"})
_READ_ARG_KEYS = frozenset({"threadId", "hostId", "turnLimit", "includeOutputs", "maxOutputCharsPerItem"})
_WAIT_ARG_KEYS = frozenset({"targets", "timeoutMs"})

# Result-text patterns.  Strict: reject anything that doesn't match the schema
# or that contains anything outside the expected top-level JSON object.
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_HEX32_RE = re.compile(r"^[0-9a-f]{32}\Z")
_THREAD_ID_RE = _UUID_RE
_TURN_ID_RE = _UUID_RE

# Send result schema: ``{"threadId": "<uuid>"}`` (no claim of started).
_SEND_RESULT_RE = re.compile(r"^[ \t\r\n]*\{[\s\S]*\}[ \t\r\n]*\Z")

# Bounded buffer sizes for stdin lines and Office call JSON.
_MAX_INPUT_BYTES = 64 * 1024
_MAX_OUTPUT_BYTES = 64 * 1024
_MAX_RESULT_CHARS = 8 * 1024 * 1024
_RESULT_CHUNK_CHARS = 480

# Bounded dispatch / tool-wait budget.
_MAX_OBSERVATIONS_PER_CYCLE = 4


# -- status and error helpers --------------------------------------------------


STATUSES: tuple[str, ...] = ("no_work", "delivered", "deferred", "unknown", "error")


class CycleError(Exception):
    """Cycle-internal fault that ends the run with a fixed status + reason."""

    def __init__(self, status: str, reason: str) -> None:
        if status not in STATUSES:
            raise ValueError(f"invalid cycle status: {status!r}")
        if not isinstance(reason, str) or not reason:
            raise ValueError("cycle reason must be non-empty")
        super().__init__(reason)
        self.status = status
        self.reason = reason


# -- identity / binding constants -----------------------------------------------


def build_identity() -> dict[str, str]:
    """Return the fixed Codex host identity used for every Office call."""
    return {
        "harness": HARNESS,
        "host": HOST,
        "session_id": SESSION_ID,
    }


# -- argument builders ---------------------------------------------------------


def send_arguments(*, thread_id: str, prompt: str) -> dict[str, Any]:
    """Build the exact, pinned ``send_message_to_thread`` argument object."""
    if not isinstance(thread_id, str) or _THREAD_ID_RE.fullmatch(thread_id) is None:
        raise CycleError("unknown", "send_thread_id_invalid")
    if not isinstance(prompt, str) or not prompt or len(prompt) > 8192:
        raise CycleError("unknown", "send_prompt_invalid")
    return {
        "threadId": thread_id,
        "hostId": "local",
        "prompt": prompt,
    }


def read_arguments(thread_id: str) -> dict[str, Any]:
    """Build the exact, pinned ``read_thread`` argument object."""
    if not isinstance(thread_id, str) or _THREAD_ID_RE.fullmatch(thread_id) is None:
        raise CycleError("unknown", "read_thread_id_invalid")
    return {
        "threadId": thread_id,
        "hostId": "local",
        "turnLimit": 2,
        "includeOutputs": False,
        "maxOutputCharsPerItem": 1,
    }


def wait_arguments(*, thread_id: str, after_cursor: str | None,
                   timeout_ms: int) -> dict[str, Any]:
    """Build the exact, pinned ``wait_threads`` argument object."""
    if not isinstance(thread_id, str) or _THREAD_ID_RE.fullmatch(thread_id) is None:
        raise CycleError("unknown", "wait_thread_id_invalid")
    if after_cursor is not None and (
        not isinstance(after_cursor, str) or not 1 <= len(after_cursor) <= 512
        or any(ord(c) < 32 for c in after_cursor)
    ):
        raise CycleError("unknown", "wait_cursor_invalid")
    if not isinstance(timeout_ms, int) or not 0 < timeout_ms <= MAX_TOOL_WAIT_S * 1000:
        raise CycleError("unknown", "wait_timeout_invalid")
    target: dict[str, str] = {"threadId": thread_id, "hostId": "local"}
    if after_cursor is not None:
        target["afterCursor"] = after_cursor
    return {"targets": [target], "timeoutMs": timeout_ms}


# -- result verifiers ----------------------------------------------------------


def _content_text(raw: Mapping[str, Any]) -> str:
    """Extract the first ``content[0].text`` payload from a raw result.

    Any shape deviation raises ``CycleError("unknown", "raw_result_invalid")``.
    The raw payload itself is never returned or persisted.
    """
    if not isinstance(raw, Mapping):
        raise CycleError("unknown", "raw_result_invalid")
    if raw.get('isError') is True:
        raise CycleError('unknown', 'app_tool_error')
    content = raw.get("content")
    if not isinstance(content, list) or not content:
        raise CycleError("unknown", "raw_result_invalid")
    first = content[0]
    if not isinstance(first, Mapping):
        raise CycleError("unknown", "raw_result_invalid")
    text = first.get("text")
    if not isinstance(text, str):
        raise CycleError("unknown", "raw_result_invalid")
    return text


def parse_send_result(raw: Mapping[str, Any], *, expected_thread_id: str) -> dict[str, str]:
    """Validate the raw ``send_message_to_thread`` result.

    Returns ``{"threadId": <exact>}`` parsed from ``content[0].text``.  Any
    divergence from the expected thread id (case, whitespace, extra keys) is a
    refuse; the script never assumes the message started just because the call
    returned an HTTP-shaped success.
    """
    text = _content_text(raw)
    if _SEND_RESULT_RE.fullmatch(text) is None:
        raise CycleError("unknown", "send_result_shape_invalid")
    try:
        payload = json.loads(text, object_pairs_hook=_strict_pairs)
    except (UnicodeDecodeError, ValueError):
        raise CycleError("unknown", "send_result_json_invalid") from None
    if not isinstance(payload, dict):
        raise CycleError("unknown", "send_result_json_invalid")
    if set(payload) != {"threadId"}:
        raise CycleError("unknown", "send_result_keys_invalid")
    thread_id = payload.get("threadId")
    if thread_id != expected_thread_id:
        raise CycleError("unknown", "send_result_thread_mismatch")
    return {"threadId": thread_id}


@dataclass(frozen=True)
class _ReadTurn:
    turn_id: str
    status: str
    started_at: float
    completed_at: float | None


@dataclass(frozen=True)
class _ReadMetadata:
    thread_id: str
    cwd: str
    state: str
    active_flags: tuple[str, ...]
    turns: tuple[_ReadTurn, ...]
    page_cursor: str
    age_s: float


def parse_read_result(raw: Mapping[str, Any], *, expected_thread_id: str,
                      now: float) -> _ReadMetadata:
    """Validate the raw ``read_thread`` result and discard ``turns[*].items``.

    Raises :class:`CycleError` if the payload is not the canonical
    ``CallToolResult`` with ``schemaVersion:1`` and an exact thread id.
    """
    text = _content_text(raw)
    try:
        payload = json.loads(text, object_pairs_hook=_strict_pairs)
    except (UnicodeDecodeError, ValueError):
        raise CycleError("unknown", "read_result_json_invalid") from None
    if not isinstance(payload, dict):
        raise CycleError("unknown", "read_result_json_invalid")
    if payload.get("schemaVersion") != 1:
        raise CycleError("unknown", "read_result_schema_invalid")
    thread = payload.get("thread")
    if not isinstance(thread, Mapping):
        raise CycleError("unknown", "read_result_thread_invalid")
    thread_id = thread.get("id")
    if thread_id != expected_thread_id:
        raise CycleError("unknown", "read_result_thread_mismatch")
    if thread.get('hostId') != 'local':
        raise CycleError('unknown', 'read_result_host_mismatch')
    cwd = thread.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        raise CycleError("unknown", "read_result_cwd_invalid")
    status = thread.get("status")
    if not isinstance(status, Mapping):
        raise CycleError("unknown", "read_result_status_invalid")
    state = status.get("type")
    if not isinstance(state, str) or not state:
        raise CycleError("unknown", "read_result_state_invalid")
    flags = status.get("activeFlags")
    if isinstance(flags, list):
        if any(not isinstance(item, str) for item in flags):
            raise CycleError('unknown', 'read_result_flags_invalid')
        active_flags = tuple(item for item in flags if isinstance(item, str))
    elif flags is None:
        active_flags = ()
    else:
        raise CycleError("unknown", "read_result_flags_invalid")
    raw_turns = payload.get("turns")
    if not isinstance(raw_turns, list):
        raise CycleError("unknown", "read_result_turns_invalid")
    turns: list[_ReadTurn] = []
    for item in raw_turns:
        if not isinstance(item, Mapping):
            raise CycleError("unknown", "read_result_turn_invalid")
        turn_id = item.get("id")
        if not isinstance(turn_id, str) or _TURN_ID_RE.fullmatch(turn_id) is None:
            raise CycleError("unknown", "read_result_turn_id_invalid")
        turn_status = item.get("status")
        if turn_status not in {"inProgress", "completed", "failed", "interrupted"}:
            raise CycleError("unknown", "read_result_turn_status_invalid")
        started_at = item.get("startedAt")
        if not isinstance(started_at, (int, float)) or isinstance(started_at, bool) or not math.isfinite(started_at):
            raise CycleError("unknown", "read_result_started_at_invalid")
        completed_at = item.get("completedAt")
        if completed_at is not None and (
            not isinstance(completed_at, (int, float)) or isinstance(completed_at, bool) or not math.isfinite(completed_at)
        ):
            raise CycleError("unknown", "read_result_completed_at_invalid")
        if turn_status == 'completed' and (completed_at is None or completed_at < started_at):
            raise CycleError('unknown', 'read_result_completed_at_invalid')
        if turn_status == 'inProgress' and completed_at is not None:
            raise CycleError('unknown', 'read_result_completed_at_invalid')
        # ``items`` is intentionally discarded -- never read, never persisted.
        turns.append(_ReadTurn(
            turn_id=turn_id, status=turn_status,
            started_at=float(started_at),
            completed_at=None if completed_at is None else float(completed_at),
        ))
    if turns and (not isinstance(payload.get('page'), Mapping)
                  or payload['page'].get('order') != 'newest_first'):
        raise CycleError('unknown', 'read_result_order_unproven')
    if any(turns[i].started_at < turns[i+1].started_at for i in range(len(turns)-1)):
        raise CycleError('unknown', 'read_result_order_invalid')
    # notLoaded describes host residency, not an active turn. Only a proven
    # terminal history without flags establishes an inactive unloaded thread.
    # Conversely, a running turn overrides an idle/unloaded host snapshot.
    if state in {'idle', 'notLoaded'} and any(t.status == 'inProgress' for t in turns):
        state = 'busy'
    elif state == 'notLoaded' and not active_flags and turns:
        state = 'idle'  # All statuses were validated above and none is running.
    # Read pagination and wait revision cursors are unrelated. Never interchange.
    cursor = ""
    age_s = 0.0  # Freshness belongs to this read, not to historical last activity.
    return _ReadMetadata(
        thread_id=thread_id, cwd=cwd, state=state, active_flags=active_flags,
        turns=tuple(turns), page_cursor=cursor, age_s=age_s,
    )


def parse_wait_result(raw: Mapping[str, Any], *, expected_thread_id: str | None = None) -> dict[str, Any]:
    try:
        payload = json.loads(_content_text(raw), object_pairs_hook=_strict_pairs)
    except ValueError:
        raise CycleError('unknown', 'wait_result_json_invalid') from None
    if not isinstance(payload, Mapping) or type(payload.get('timedOut')) is not bool:
        raise CycleError('unknown', 'wait_result_schema_invalid')
    polls = payload.get('polls')
    if not isinstance(polls, list) or not 1 <= len(polls) <= 8:
        raise CycleError('unknown', 'wait_result_polls_invalid')
    rows = []
    for poll in polls:
        if not isinstance(poll, Mapping) or poll.get('schemaVersion') != 1:
            raise CycleError('unknown', 'wait_poll_schema_invalid')
        thread = poll.get('thread')
        if (not isinstance(thread, Mapping) or thread.get('hostId') != 'local'
                or (expected_thread_id is not None and thread.get('id') != expected_thread_id)):
            raise CycleError('unknown', 'wait_thread_mismatch')
        cursor = poll.get('cursor')
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 512 or any(ord(c) < 32 for c in cursor):
            raise CycleError('unknown', 'wait_cursor_invalid')
        rows.append({'thread_id': thread['id'], 'cursor': cursor})
    return {'timedOut': payload['timedOut'], 'polls': rows}


def _strict_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if not isinstance(key, str) or key in out:
            raise ValueError("duplicate or non-string key")
        out[key] = value
    return out


# -- office call helpers -------------------------------------------------------


def _office_call(client: OfficeRPC, op: str, args: Mapping[str, Any],
                  *, identity: Mapping[str, str], budget_s: float) -> dict[str, Any]:
    """Run one Office call bounded by the remaining deadline budget."""
    timeout = min(budget_s, MAX_OFFICE_CALL_S)
    if timeout <= 0:
        raise CycleError("unknown", "office_call_deadline_exceeded")
    response = client.request(op, identity, dict(args), timeout_s=timeout)
    if not isinstance(response, dict):
        raise CycleError("unknown", f"{op}_invalid_response")
    status = response.get("status")
    if status == "degraded":
        reason = response.get("reason") or "transport"
        raise CycleError("unknown", f"{op}_degraded_{_safe_token(reason)}")
    if status == "error":
        code = response.get("code") or "error"
        raise CycleError("unknown", f"{op}_error_{_safe_token(code)}")
    if status != "ok":
        raise CycleError("unknown", f"{op}_status_{_safe_token(str(status))}")
    return response


def _safe_token(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9_]+", "_", value.strip().casefold())
    return cleaned[:48] or "unknown"


# -- the cycle -----------------------------------------------------------------


class CodexRelayCycle:
    """One deterministic Codex Desktop relay cycle.

    The cycle is single-shot: ``run`` either settles one pending dispatch (or
    reconciles one fenced dispatch) or reports ``no_work``.  ``stop`` halts the
    run early with status ``unknown``.
    """

    def __init__(
        self,
        client: OfficeRPC,
        *,
        identity: Mapping[str, str] | None = None,
        binding_ids: Iterable[str] = BINDING_IDS,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], float] = time.time,
        host_factory: "HostIOFactory | None" = None,
        deadline_s: float = CYCLE_DEADLINE_S,
        reserve_s: float = DEADLINE_RESERVE_S,
        max_tool_wait_s: float = MAX_TOOL_WAIT_S,
        watch_timeout_s: int = DEFAULT_WATCH_TIMEOUT_S,
    ) -> None:
        request = getattr(client, "request", None)
        if not callable(request):
            raise TypeError("client must provide a callable request(op, identity, args, timeout_s=...)")
        if not 0 < deadline_s <= CYCLE_DEADLINE_S:
            raise ValueError("deadline_s must be positive")
        if not 0 <= reserve_s < deadline_s:
            raise ValueError("reserve_s must be a non-negative slice of deadline_s")
        if not 0 < max_tool_wait_s <= MAX_TOOL_WAIT_S:
            raise ValueError("max_tool_wait_s must be positive")
        if type(watch_timeout_s) is not int or not 1 <= watch_timeout_s <= 240:
            raise ValueError("watch_timeout_s must be an integer between 1 and 240")
        self.client = client
        self.identity = MappingProxyType(dict(identity or build_identity()))
        self.relay_agent = self.identity.get('agent_id', AGENT_ID)
        self.binding_ids = validate_binding_ids(binding_ids)
        self.monotonic = monotonic
        self.now = now
        self.host = (host_factory or HostIOFactory.from_stdio())()
        self.deadline_s = deadline_s
        self.reserve_s = reserve_s
        self.max_tool_wait_s = max_tool_wait_s
        self.watch_timeout_s = watch_timeout_s
        self.stopped = False
        self.terminated: tuple[str, str] | None = None
        self.native_send_attempted = False
        self.blocked_bindings: list[dict[str, str]] = []

    # -- main entry point -----------------------------------------------------

    def run(self) -> dict[str, Any]:
        started = self.monotonic()
        deadline = started + self.deadline_s
        deferred = None
        blocked = None
        if hasattr(self.host, 'set_deadline'):
            self.host.set_deadline(deadline, self.max_tool_wait_s)
        try:
            if self.stopped or self.monotonic() > deadline - self.reserve_s:
                self._set_terminated("unknown", "deadline")
                status, reason = self.terminated
                return self._done(status, reason)
            self._prepare_watch(deadline)
            if self.terminated is not None:
                status, reason = self.terminated
                return self._done(status, reason)
            for binding_id in self.binding_ids:
                if self.stopped or self.monotonic() > deadline - self.reserve_s:
                    self._set_terminated("unknown", "deadline")
                    break
                self._process_binding(binding_id, deadline)
                if (self.terminated is not None and self.terminated[0] == 'unknown'
                        and not self.native_send_attempted and not self.stopped
                        and self.terminated[1] in {'owned_fence_unverifiable', 'owned_checkout_mismatch',
                                                  'owned_turn_unconfirmed', 'new_turn_not_finished',
                                                  'multiple_new_turns_ambiguous'}):
                    # An old unknown fences this binding only. Never clear or
                    # retry it; another independently verified binding may work.
                    self.blocked_bindings.append({'binding_id': binding_id, 'reason': self.terminated[1]})
                    blocked, self.terminated = self.terminated, None
                    continue
                if self.terminated is not None and self.terminated[0] == 'deferred':
                    # Deferral made no native send. A busy Core must not starve
                    # a different ready approved binding in the same cycle.
                    deferred, self.terminated = self.terminated, None
                    continue
                if self.terminated is not None:
                    break
            status, reason = self.terminated or blocked or deferred or ("no_work", "no_pending")
            return self._done(status, reason)
        except CycleError as exc:
            return self._done(exc.status, exc.reason)
        finally:
            try:
                self.host.close()
            except (OSError, ValueError):
                pass

    def stop(self, *, status: str = "unknown", reason: str = "stopped") -> None:
        """Halt the cycle from another thread."""
        self.stopped = True
        self._set_terminated(status, reason)

    # -- helpers --------------------------------------------------------------

    def _done(self, status: str, reason: str) -> dict[str, Any]:
        if status not in STATUSES:
            status = "unknown"
        payload = {"action": "done", "status": status, "reason": reason}
        if self.blocked_bindings:
            payload['blocked_bindings'] = list(self.blocked_bindings)
        self.host.write_json_line(payload)
        return payload

    def _set_terminated(self, status: str, reason: str) -> None:
        """Record the terminal status without emitting it."""
        if self.terminated is not None:
            return
        self.terminated = (status, reason)

    def _terminate(self, status: str, reason: str) -> None:
        """Record a terminal status (the final ``done`` is emitted by ``run``)."""
        self._set_terminated(status, reason)

    def _remaining(self, deadline: float) -> float:
        return max(0.0, deadline - self.monotonic())

    def _watch_allowance_s(self) -> float:
        connect_timeout = float(getattr(getattr(self.client, 'config', None), 'connect_timeout_s', 10.0))
        request_timeout = float(getattr(getattr(self.client, 'config', None), 'request_timeout_s', 0.0))
        hint_allowance = float(self.watch_timeout_s + 10) + connect_timeout
        return max(hint_allowance, request_timeout)

    def _prepare_watch(self, deadline: float) -> None:
        if self.stopped:
            self._set_terminated("unknown", "stopped")
            return
        allowance = self._watch_allowance_s()
        budget = self._remaining(deadline) - self.reserve_s
        if budget < allowance:
            self._set_terminated("unknown", "deadline")
            return
        watch_args: dict[str, Any] = {
            "binding_id": list(self.binding_ids),
            "driver": DRIVER,
            "timeout_s": self.watch_timeout_s,
            "interval_s": self.watch_timeout_s,
        }
        res = _office_call(
            self.client,
            "wake-relay-watch",
            watch_args,
            identity=self.identity,
            budget_s=max(0.0, budget),
        )
        if not isinstance(res, dict) or res.get("code") != "wake_relay_watch":
            raise CycleError("unknown", "watch_result_invalid")
        result = res.get("result")
        if result == "stopped":
            self._terminate("deferred", "global_stop")
            return
        if result not in {"pending", "timeout"}:
            raise CycleError("unknown", "watch_result_invalid")

    def _process_binding(self, binding_id: str, deadline: float) -> None:
        budget = self._remaining(deadline) - self.reserve_s
        snap = _office_call(
            self.client, "wake-relay-pending",
            {
                "binding_id": binding_id,
                "driver": DRIVER,
                "include_owned": True,
                "relay_agent": self.relay_agent,
            },
            identity=self.identity, budget_s=max(0.0, budget),
        )
        if snap.get("code") != "wake_relay_pending":
            return
        binding = snap.get("binding") if isinstance(snap.get("binding"), dict) else None
        if binding is None:
            # Default (non-extended) snapshot: the cycle cannot verify the
            # thread id or workspace and must defer without inventing data.
            if not isinstance(snap.get("dispatches"), list) or not snap["dispatches"]:
                return
            self._terminate("deferred", "binding_metadata_unavailable")
            return
        thread_id = binding.get("native_thread_id")
        workspace = binding.get("workspace")
        version = binding.get("version")
        if (not isinstance(thread_id, str) or _THREAD_ID_RE.fullmatch(thread_id) is None
                or not isinstance(workspace, str) or not workspace
                or type(version) is not int or version < 1
                or binding.get('binding_id') != binding_id
                or binding.get('verified') is not True
                or binding.get('status') != 'active'):
            self._terminate("unknown", "binding_projection_invalid")
            return
        owned = snap.get("owned_dispatches")
        if not isinstance(owned, list):
            owned = []
        if len(owned) > 10:
            owned = owned[:10]
        for owned_row in owned:
            if self.stopped or self._remaining(deadline) <= self.reserve_s:
                return
            self._reconcile_owned(owned_row, deadline, thread_id, workspace, version)
            if self.terminated is not None:
                return
        if self.stopped:
            return
        dispatches = snap.get("dispatches")
        if not isinstance(dispatches, list) or not dispatches:
            return
        chosen = self._pick_dispatch(dispatches, binding_id)
        if not isinstance(chosen, dict):
            return
        self._handle_pending(chosen, thread_id, workspace, deadline)

    @staticmethod
    def _pick_dispatch(rows: list[Mapping[str, Any]], binding_id: str) -> dict[str, Any] | None:
        chosen: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            if row.get("binding_id") != binding_id or row.get("status") != "pending":
                continue
            dispatch_id = row.get("dispatch_id")
            text = row.get("text")
            trigger_ref = row.get("trigger_ref")
            if (not isinstance(dispatch_id, str)
                    or _HEX32_RE.fullmatch(dispatch_id) is None
                    or not isinstance(text, str) or not text
                    or not isinstance(trigger_ref, str) or not trigger_ref):
                continue
            chosen.append({
                "dispatch_id": dispatch_id,
                "binding_id": binding_id,
                "text": text,
                "trigger_ref": trigger_ref,
                "created_at": row.get("created_at", "") if isinstance(row.get("created_at"), str) else "",
            })
        if not chosen:
            return None
        chosen.sort(key=lambda item: (item["created_at"], item["dispatch_id"]))
        return chosen[0]

    # -- reconcile a fenced dispatch from a previous cycle ------------------

    def _reconcile_owned(self, owned: Mapping[str, Any], deadline: float,
                         thread_id: str, workspace: str, version: int) -> None:
        did = owned.get('dispatch_id')
        if (not isinstance(did, str) or not _HEX32_RE.fullmatch(did)
                or owned.get('relay_agent') != self.relay_agent
                or owned.get('target_local_id') != thread_id
                or owned.get('binding_version') != version
                or 'native_baseline_observed_at' not in owned):
            self._terminate('unknown', 'owned_fence_unverifiable')
            return
        observation = self._read(thread_id)
        if _cwd_incompatible(observation.cwd, workspace):
            self._terminate('unknown', 'owned_checkout_mismatch')
            return
        baseline = owned.get('native_baseline_turn_id')
        fence = _timestamp(owned.get('unknown_at'))
        try:
            turn = _pick_new_turn(observation.turns, baseline, fence=fence)
        except CycleError as exc:
            if exc.reason != 'multiple_new_turns_ambiguous':
                raise
            # Keep this old submission fenced without starving other bindings.
            # A new submission's ambiguity still exits the cycle without retry.
            self._terminate(exc.status, exc.reason)
            return
        if turn is None:
            self._terminate('unknown', 'owned_turn_unconfirmed')
            return
        # Never send from this path, even if the original raw send result was lost.
        self._record_turn(did, thread_id, turn, deadline, receipt_present=owned.get('receipt_status') is not None)


    # -- handle a fresh pending dispatch --------------------------------------

    def _handle_pending(self, dispatch: Mapping[str, Any], thread_id: str,
                        workspace: str, deadline: float) -> None:
        did, text = str(dispatch['dispatch_id']), str(dispatch['text'])
        # Observe before uptake/fence. An invalid/busy baseline never sends.
        observation = self._read(thread_id)
        if _cwd_incompatible(observation.cwd, workspace):
            self._terminate('deferred', 'cwd_mismatch')
            return
        if observation.state != 'idle' or observation.active_flags:
            if observation.state in {'active', 'busy'}:
                self._call('wake-relay-defer-busy', {'dispatch_id': did}, deadline)
            self._terminate('deferred', 'target_not_idle')
            return
        prior = observation.turns[0].turn_id if observation.turns else None
        baseline_at = self.now()
        claimed = self._call('wake-relay-claim', {'dispatch_id': did, 'relay_agent': self.relay_agent}, deadline)
        if claimed.get('code') != 'wake_relay_claimed':
            raise CycleError('unknown', 'claim_unconfirmed')
        begun = self._call('wake-relay-native-begin', {
            'dispatch_id': did, 'relay_agent': self.relay_agent,
            'prior_turn_id': prior, 'baseline_observed_at': baseline_at}, deadline)
        if begun.get('code') != 'wake_native_submission_fenced':
            raise CycleError('unknown', 'begin_unconfirmed')
        # Generation/version/STOP/consumed guards are rechecked by native-begin.
        fence = _timestamp(begun.get('dispatch', {}).get('unknown_at', baseline_at))
        self.native_send_attempted = True
        token = self.host.write_call(SEND_TOOL, send_arguments(thread_id=thread_id, prompt=text))
        parse_send_result(self.host.read_result(token), expected_thread_id=thread_id)
        observation = self._await_turn(thread_id, prior, deadline, fence=fence)
        if observation is None:
            self._terminate('unknown', 'observation_timeout')
            return
        if _cwd_incompatible(observation.cwd, workspace):
            raise CycleError('unknown', 'cwd_mismatch')
        turn = _pick_new_turn(observation.turns, prior, fence=fence)
        if turn is None:
            raise CycleError('unknown', 'new_turn_unconfirmed')
        self._record_turn(did, thread_id, turn, deadline)

    def _call(self, op: str, args: Mapping[str, Any], deadline: float) -> dict[str, Any]:
        return _office_call(self.client, op, {'driver': DRIVER, **args},
                            identity=self.identity, budget_s=self._remaining(deadline) - self.reserve_s)

    def _read(self, thread_id: str) -> _ReadMetadata:
        token = self.host.write_call(READ_TOOL, read_arguments(thread_id))
        return parse_read_result(self.host.read_result(token), expected_thread_id=thread_id, now=self.now())

    def _record_turn(self, did: str, thread_id: str, turn: _ReadTurn, deadline: float,
                     *, receipt_present: bool = False) -> None:
        if not receipt_present:
            # Native completion and task ACK are separate. Receipt is unconfirmed
            # until the exact fresh native metadata is validated by Office.
            self._call('wake-relay-receipt', {'dispatch_id': did, 'status': 'unconfirmed',
                                             'turn_id': turn.turn_id}, deadline)
        if turn.status not in {'completed', 'inProgress'}:
            raise CycleError('unknown', 'new_turn_not_successful')
        proof = {'schema': 'codex-native-turn/v1', 'source': READ_TOOL,
                 'thread_id': thread_id, 'turn_id': turn.turn_id,
                 'started_at': turn.started_at, 'completed_at': turn.completed_at,
                 'status': turn.status, 'observed_at': self.now()}
        observed = self._call('wake-relay-observe', {'dispatch_id': did,
                               'stage': 'target_finished' if turn.status == 'completed' else 'target_started',
                               'native_proof': proof}, deadline)
        ack = self._call('wake-relay-ack', {'dispatch_id': did}, deadline)
        settled = observed.get('dispatch', {}).get('reconciliation_outcome') == 'delivered'
        if settled and turn.status == 'completed':
            self._terminate('delivered', 'completed_new_turn')
        elif ack.get('acknowledged') is True and turn.status == 'completed':
            self._terminate('delivered', 'completed_acknowledged_turn')
        else:
            self._terminate('unknown', 'new_turn_not_finished')


    # -- host IO -------------------------------------------------------------

    def _baseline_turn_id(self, thread_id: str, deadline: float) -> tuple[str | None, float] | None:
        observation = self._read(thread_id)
        if observation.state != 'idle' or observation.active_flags:
            return None
        return (observation.turns[0].turn_id if observation.turns else None, self.now())

    def _await_turn(self, thread_id: str, baseline_turn_id: str | None,
                    deadline: float, *, fence: float) -> _ReadMetadata | None:
        cursor = None
        for _ in range(_MAX_OBSERVATIONS_PER_CYCLE):
            left = self._remaining(deadline) - self.reserve_s
            if left <= 0:
                return None
            token = self.host.write_call(WAIT_TOOL, wait_arguments(thread_id=thread_id,
                        after_cursor=cursor, timeout_ms=max(1, int(min(self.max_tool_wait_s, left) * 1000))))
            wait = parse_wait_result(self.host.read_result(token), expected_thread_id=thread_id)
            cursor = wait['polls'][0]['cursor']
            observed = self._read(thread_id)
            turn = _pick_new_turn(observed.turns, baseline_turn_id, fence=fence)
            if turn is not None and turn.status == 'completed':
                return observed
            if observed.state not in {'idle', 'active', 'busy'}:
                return None
        return observed if turn is not None else None


def _cwd_incompatible(observed_cwd: str, expected_workspace: str) -> bool:
    if not isinstance(observed_cwd, str) or not isinstance(expected_workspace, str):
        return True
    normalize = lambda p: p.replace('\\', '/').rstrip('/').casefold()
    return normalize(observed_cwd) != normalize(expected_workspace)


def _timestamp(value: Any) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        except ValueError:
            pass
    raise CycleError('unknown', 'native_fence_invalid')


def _pick_new_turn(turns: tuple[_ReadTurn, ...], baseline: str | None,
                   *, fence: float) -> _ReadTurn | None:
    eligible = [t for t in turns if t.turn_id != baseline and t.started_at >= fence - 1.0]
    if len(eligible) > 1:
        raise CycleError('unknown', 'multiple_new_turns_ambiguous')
    return eligible[0] if eligible else None


# -- host IO abstraction -------------------------------------------------------


class HostIOFactory:
    """Build a HostIO over the process stdio or any pair of streams."""

    @classmethod
    def from_stdio(cls) -> "HostIOFactory":
        return cls(input_stream=sys.stdin.buffer, output_stream=sys.stdout.buffer,
                   deadline_factory=time.monotonic)

    def __init__(self, *, input_stream: Any, output_stream: Any,
                 deadline_factory: Callable[[], float]) -> None:
        self.input_stream = input_stream
        self.output_stream = output_stream
        self.deadline_factory = deadline_factory

    def __call__(self) -> "HostIO":
        return HostIO(self.input_stream, self.output_stream, self.deadline_factory)


class HostIO:
    """JSON-lines host transport.

    ``write_call`` emits a ``call`` action with a unique correlation token and
    returns that token.  ``read_result`` blocks until the matching token comes
    back on stdin.  Other shapes / mismatched tokens / EOF end the cycle.
    """

    def __init__(self, input_stream: Any, output_stream: Any,
                 deadline_factory: Callable[[], float]) -> None:
        self.input = input_stream
        self.output = output_stream
        self.deadline_factory = deadline_factory
        self._buffered: bytes = b""
        self._closed = False
        self._deadline = deadline_factory() + CYCLE_DEADLINE_S
        self._max_wait = MAX_TOOL_WAIT_S
        self._chunks = queue.Queue(maxsize=2)
        self._reader = None

    def set_deadline(self, deadline: float, maximum_wait: float) -> None:
        self._deadline, self._max_wait = deadline, maximum_wait

    def write_json_line(self, payload: Mapping[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
        if len(encoded) > _MAX_OUTPUT_BYTES:
            raise CycleError("error", "host_output_oversized")
        try:
            self.output.write(encoded + b"\n")
            flush = getattr(self.output, "flush", None)
            if callable(flush):
                flush()
        except (OSError, ValueError):
            self._closed = True
            raise CycleError("error", "host_output_closed") from None

    def write_call(self, tool: str, arguments: Mapping[str, Any]) -> str:
        token = uuid.uuid4().hex
        payload = {"action": "call", "token": token, "tool": str(tool),
                   "arguments": dict(arguments)}
        self.write_json_line(payload)
        return token

    def read_result(self, expected_token: str) -> dict[str, Any]:
        """Read until we see the matching token; any other input ends the cycle."""
        parts: list[str] = []
        size = 0
        while True:
            line = self._next_line()
            if line is None:
                raise CycleError("unknown", "stdin_closed")
            if not line.strip():
                continue
            try:
                payload = json.loads(line.decode("utf-8"), object_pairs_hook=_strict_pairs)
            except (UnicodeDecodeError, ValueError):
                raise CycleError("error", "stdin_invalid_json") from None
            if not isinstance(payload, Mapping):
                raise CycleError("error", "stdin_invalid_object")
            token = payload.get("token")
            if token != expected_token:
                raise CycleError("error", "stdin_token_mismatch")
            if 'part' in payload:
                if set(payload) != {'token','part','data','final'}:
                    raise CycleError('error', 'stdin_chunk_shape_invalid')
                part, data, final = payload['part'], payload['data'], payload['final']
                if (type(part) is not int or part != len(parts) or type(final) is not bool
                    or not isinstance(data,str) or not 1 <= len(data) <= _RESULT_CHUNK_CHARS
                    or not data.isascii()):
                    raise CycleError('error', 'stdin_chunk_invalid')
                size += len(data)
                if size > _MAX_RESULT_CHARS:
                    raise CycleError('error', 'stdin_result_oversized')
                parts.append(data)
                if not final:
                    continue
                try:
                    result=json.loads(''.join(parts),object_pairs_hook=_strict_pairs)
                except ValueError:
                    raise CycleError('error','stdin_chunk_result_invalid') from None
                if not isinstance(result,Mapping):
                    raise CycleError('error','stdin_result_invalid')
                return result
            if parts:
                raise CycleError('error','stdin_chunk_interrupted')
            result = payload.get("result")
            if not isinstance(result, Mapping):
                raise CycleError("error", "stdin_result_invalid")
            return result

    def _next_line(self) -> bytes | None:
        deadline = self.deadline_factory()
        while True:
            newline = self._buffered.find(b"\n")
            if newline >= 0:
                line = self._buffered[:newline]
                self._buffered = self._buffered[newline + 1:]
                return line
            chunk = self._read_chunk()
            if not chunk:
                if self._buffered:
                    line, self._buffered = self._buffered, b""
                    return line
                return None
            self._buffered += chunk
            if len(self._buffered) > _MAX_INPUT_BYTES:
                raise CycleError("error", "stdin_oversized")

    def _read_chunk(self) -> bytes:
        if self._closed:
            return b""
        if self._reader is None:
            def collect():
                try:
                    read1 = getattr(self.input, 'read1', None)
                    while True:
                        chunk = read1(4096) if callable(read1) else self.input.read(4096)
                        self._chunks.put(chunk)
                        if not chunk:
                            return
                except (OSError, ValueError):
                    self._chunks.put(b'')
            self._reader = threading.Thread(target=collect, daemon=True)
            self._reader.start()
        left = self._deadline - self.deadline_factory()
        if left <= 0:
            raise CycleError('unknown', 'stdin_deadline')
        try:
            chunk = self._chunks.get(timeout=min(self._max_wait, left))
        except queue.Empty:
            raise CycleError('unknown', 'stdin_deadline') from None
        if not chunk:
            self._closed = True
        return chunk

    def close(self) -> None:
        self._closed = True
        # Closing a pipe while its daemon reader blocks can itself block forever.
        close = getattr(self.input, "close", None) if self._reader is None or not self._reader.is_alive() else None
        if callable(close):
            try:
                close()
            except (OSError, ValueError):
                pass


# -- module entry point -------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-relay-cycle",
        description="Deterministic Codex Desktop relay cycle (Office decisions only).",
    )
    parser.add_argument(
        "--remote-config", type=str,
        help=("Path to office-remote.json. Default: located beside the installed "
              "office.py as '<runtime>/office-remote.json'."))
    parser.add_argument(
        "--binding-id", action="append",
        help="Exact owner-enrolled Codex relay binding; repeat up to 16. Defaults to the three Core bindings.")
    parser.add_argument(
        "--runtime-home", type=Path,
        help="Override the Agent Office coord home (informational; defaults to %%USERPROFILE%%/.agents/coord).")
    parser.add_argument(
        "--deadline", type=float, default=CYCLE_DEADLINE_S,
        help=f"Overall cycle deadline in seconds (default {CYCLE_DEADLINE_S}).")
    parser.add_argument(
        "--reserve", type=float, default=DEADLINE_RESERVE_S,
        help=f"Deadline slice reserved for shutdown (default {DEADLINE_RESERVE_S}).")
    parser.add_argument(
        "--max-tool-wait", type=float, default=MAX_TOOL_WAIT_S,
        help=f"Maximum app-tool wait per round in seconds (default {MAX_TOOL_WAIT_S}).")
    parser.add_argument(
        "--watch-timeout", type=int, default=DEFAULT_WATCH_TIMEOUT_S,
        help=f"Short finite watch budget in seconds (default {DEFAULT_WATCH_TIMEOUT_S}).")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Validate arguments and exit; no Office call, no host IO.")
    parser.add_argument(
        "--version", action="version", version="codex-relay-cycle 0.3.1")
    return parser


def _resolve_remote_config(explicit: str | os.PathLike[str] | None) -> Path:
    """Locate ``office-remote.json`` beside the installed ``office.py``.

    A frozen local fallback is explicitly refused: a missing config means
    "not installed" and the cycle exits with ``no_work``.
    """
    if explicit is not None:
        path = Path(explicit)
        if not path.is_file():
            raise FileNotFoundError(str(path))
        return path
    here = Path(__file__).resolve().parent.parent  # .../coordfs -> ...
    candidate = here / "office-remote.json"
    if candidate.is_file():
        return candidate
    raise FileNotFoundError(str(candidate))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.binding_id is not None:
        try:
            binding_ids = validate_binding_ids(args.binding_id)
        except ValueError as exc:
            parser.error(str(exc))
    if not 0 < args.deadline <= CYCLE_DEADLINE_S:
        parser.error("--deadline must be positive")
    if not 0 <= args.reserve < args.deadline:
        parser.error("--reserve must be a non-negative slice of --deadline")
    if not 0 < args.max_tool_wait <= MAX_TOOL_WAIT_S:
        parser.error("--max-tool-wait must be positive")
    if not 1 <= args.watch_timeout <= 240:
        parser.error("--watch-timeout must be between 1 and 240")
    if args.dry_run:
        try:
            config_path = _resolve_remote_config(args.remote_config)
        except FileNotFoundError:
            return 0
        load_remote_config(config_path)
        return 0
    try:
        config_path = _resolve_remote_config(args.remote_config)
    except FileNotFoundError:
        # Not installed: the host treats this as no_work and stops retrying.
        sys.stdout.buffer.write(
            json.dumps({"action": "done", "status": "no_work",
                        "reason": "remote_config_missing"}).encode("utf-8") + b"\n")
        sys.stdout.buffer.flush()
        return 0
    try:
        config = load_remote_config(config_path)
        require_native_connection_paths(config)
    except (OSError, ValueError):
        print(json.dumps({'action': 'done', 'status': 'unknown', 'reason': 'remote_config_invalid'}), flush=True)
        return 0
    popen_factory = None
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        def _factory(argv, **kwargs):
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | creationflags
            kwargs["shell"] = False
            return subprocess.Popen(argv, **kwargs)

        popen_factory = _factory
    else:
        popen_factory = subprocess.Popen
    client = OfficeRemoteClient(config, popen_factory=popen_factory)
    # The Office client holds the Popen factory used for SSH subprocess.  On
    # Windows, ``CREATE_NO_WINDOW`` keeps the host shell silent.  The cycle
    # itself is subprocess-free (only stdin/stdout JSON lines).
    identity = build_identity()
    if args.binding_id is None:
        binding_ids = discover_active_codex_bindings(client, identity)
    host_io = HostIOFactory.from_stdio()
    cycle = CodexRelayCycle(
        client, identity=identity, binding_ids=binding_ids, host_factory=host_io,
        deadline_s=args.deadline, reserve_s=args.reserve,
        max_tool_wait_s=args.max_tool_wait,
        watch_timeout_s=args.watch_timeout,
    )
    result = cycle.run()
    return 0 if result.get("status") in {"no_work", "delivered", "deferred", "unknown", "error"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
