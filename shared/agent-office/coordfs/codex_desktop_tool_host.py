"""Remote Codex Desktop tool host.

This module speaks only to an injected Office client ``request(op, identity, args)``.
It never imports Wake, never opens a local Office store, and never calls
``mcp__codex_app__send_message_to_thread``. The in-host Codex session makes that
one official call. Synthetic results are not Desktop readiness.
"""

from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping

from coordfs.errors import CoordError

DRIVER = "codex-desktop-relay"
OFFICIAL_SEND_TOOL = "mcp__codex_app__send_message_to_thread"
OFFICIAL_ARGUMENT_KEYS = frozenset({"threadId", "hostId", "prompt"})
OBSERVATION_SCHEMA = "codex-desktop-app-observation/v1"
PROOF_SCHEMA = "codex-desktop-thread-proof/v1"
ACK_SCHEMA = "codex-desktop-office-ack/v1"
OBSERVATION_SOURCE = "codex_app_api"
OBSERVATION_MAX_AGE_S = 60
FUTURE_SKEW_S = 5
MAX_RECEIPT_BYTES = 4096
MAX_RELAY_TEXT_CHARS = 8192
MAX_PENDING_ROWS = 20

HOST_OPS = frozenset(
    {
        "wake-stop-status",
        "wake-relay-pending",
        "wake-relay-defer-busy",
        "wake-relay-claim",
        "wake-relay-native-begin",
        "wake-relay-receipt",
        "wake-relay-observe",
        "delivery",
    }
)
RELAY_OPS = frozenset(
    {
        "wake-relay-pending",
        "wake-relay-defer-busy",
        "wake-relay-claim",
        "wake-relay-native-begin",
        "wake-relay-receipt",
        "wake-relay-observe",
        "wake-relay-watch",
    }
)

_BINDING_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}\Z")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_HEX32_RE = re.compile(r"^[0-9a-f]{32}\Z")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}\Z")
_AGENT_RE = re.compile(
    r"^[a-z][a-z0-9-]{0,63}-[a-z0-9][a-z0-9-]{0,63}-[a-z0-9][a-z0-9._-]{0,255}\Z"
)
_CHANNEL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}\Z")
_STATUS_RE = re.compile(r"^[a-z][a-z_]{0,31}\Z")
_CODE_RE = re.compile(r"^[a-z0-9_]{1,64}\Z")
_OBS_FIELDS = frozenset(
    {
        "schema",
        "native_thread_id",
        "project_slug",
        "workspace",
        "observed_at",
        "thread_state",
        "source",
        "latest_turn_id",
    }
)
_PROOF_FIELDS = frozenset(
    {
        "schema",
        "native_thread_id",
        "message_id",
        "turn_id",
        "phase",
        "thread_ended",
        "observed_at",
    }
)
_ACK_FIELDS = frozenset({"schema", "message_id", "ack_id", "from_agent", "channel"})
_DISPATCH_KEYS = (
    "dispatch_id",
    "binding_id",
    "target_local_id",
    "text",
    "status",
    "trigger_ref",
    "created_at",
)
UNPROVEN_RECEIPT_STATUSES = frozenset(
    {
        "stored",
        "queued",
        "pending",
        "accepted",
        "submitted",
        "ok",
        "success",
        "started",
        "running",
        "in_progress",
        "completed",
        "finished",
        "ended",
        "done",
    }
)
FAILURE_RECEIPT_STATUSES = frozenset({"error", "failed", "rejected", "undelivered"})
KNOWN_RECEIPT_STATUSES = UNPROVEN_RECEIPT_STATUSES | FAILURE_RECEIPT_STATUSES
_IMMUTABLE = MappingProxyType(
    {
        "desktop_uat": "not_run",
        "tool_wait_continuation": "unproven",
        "receipt_schema": "unverified",
        "python_invokes_official_tool": False,
    }
)


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    return value


HOST_STATE_MAPPING = _freeze(
    {
        "schema": "codex-desktop-tool-host-state/v1",
        "official_tool": OFFICIAL_SEND_TOOL,
        "official_tool_invoked_by": "in_host_codex_session",
        "python_invokes_official_tool": False,
        "receipt_schema": "unverified",
        "desktop_uat": "not_run",
        "tool_wait_continuation": "unproven",
        "app_thread_state": {
            "idle": "eligible_for_one_prepare",
            "busy": "defer_before_send",
            "quota": "refuse_before_send",
            "unknown": "refuse_before_send",
        },
        "office_stop": "refuse_before_send",
        "unverified_project": "refuse_before_send",
        "stored_or_queued_tool_receipt": "not_started",
        "missing_authentic_id_or_status": "unknown",
        "real_started": "separate_thread_proof_plus_office_ack",
        "real_completed": "ended_thread_proof_plus_office_ack",
    }
)


def _fail(code: str, message: str) -> None:
    raise CoordError(message, code=code)


def _agent(value: Any, code: str) -> str:
    if not isinstance(value, str) or not _AGENT_RE.fullmatch(value):
        _fail(code, "Office agent must be a canonical strong id")
    harness, separator, suffix = value.partition("-ailab-")
    if separator and (
        "-ailab-" in suffix or suffix.startswith("ailab-") or suffix.startswith(harness + "-")
    ):
        _fail(code, "Office agent repeats an identity prefix")
    return value


def _workspace(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1024
        or "\x00" in value
        or any(ord(char) < 32 for char in value)
    ):
        _fail("invalid_workspace", "workspace must be bounded text")
    parts = re.split(r"[\\/]", value)
    windows = bool(re.match(r"^[A-Za-z]:[\\/]", value)) and not value.startswith("\\\\")
    posix = value.startswith("/") and not value.startswith("//")
    path_parts = parts[1:]
    if len(path_parts) == 1 and path_parts[0] == "":
        path_parts = []  # An attested drive root (D:\\) or POSIX root is valid metadata.
    if any(part in {"", ".", ".."} for part in path_parts) or not (windows or posix):
        _fail("invalid_workspace", "workspace must be an absolute path without traversal")
    return value


def _result(status: str, code: str, *, started: bool = False, completed: bool = False,
            submit: Mapping[str, Any] | None = None, **fields: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "status": status,
        "code": code,
        "started": started,
        "completed": completed,
        "submit": None if submit is None else dict(submit),
    }
    value.update(fields)
    value.update(_IMMUTABLE)
    return value


def _unix_time(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _fresh(value: Any, *, now: float) -> bool:
    stamp = _unix_time(value)
    if stamp is None:
        return False
    age = now - stamp
    return -FUTURE_SKEW_S <= age < OBSERVATION_MAX_AGE_S


def _uuid_field(data: Mapping[str, Any], names: tuple[str, ...]) -> str | None:
    found: str | None = None
    seen = False
    for name in names:
        if name not in data:
            continue
        seen = True
        value = data[name]
        if not isinstance(value, str) or _UUID_RE.fullmatch(value) is None:
            return None
        if found is not None and found != value:
            return None
        found = value
    return found if seen else None


def _status_token(data: Mapping[str, Any]) -> str | None:
    present = [name for name in ("status", "state") if name in data]
    if len(present) != 1 or not isinstance(data[present[0]], str):
        return None
    raw = data[present[0]]
    if raw != raw.strip():
        return None
    token = raw.lower()
    if _STATUS_RE.fullmatch(token) is None or token not in KNOWN_RECEIPT_STATUSES:
        return None
    return token


def _payload_object(payload: Any) -> dict[str, Any] | None:
    if isinstance(payload, str):
        if len(payload.encode("utf-8")) > MAX_RECEIPT_BYTES:
            return None
        try:
            payload = json.loads(payload)
        except (UnicodeError, json.JSONDecodeError, ValueError):
            return None
    if type(payload) is not dict:
        return None
    return payload


def map_tool_receipt(payload: Any) -> dict[str, Any]:
    """Parse one synthetic or real tool receipt without promoting it to delivery.

    The Codex app receipt schema is unverified. Stored and queued tokens never
    become a started turn. A missing authentic id or status stays unknown.
    """

    parsed = _result("ok", "receipt_unknown")
    data = _payload_object(payload)
    if data is None:
        return parsed
    message_id = _uuid_field(data, ("message_id", "messageId"))
    turn_id = _uuid_field(data, ("turn_id", "turnId"))
    status = _status_token(data)
    authentic = message_id or turn_id
    if authentic is None or status is None:
        return parsed
    office_status = "error" if status in FAILURE_RECEIPT_STATUSES else "unconfirmed"
    return _result(
        "ok",
        "receipt_" + office_status,
        message_id=message_id,
        turn_id=turn_id,
        status_token=status,
        delivery="error" if office_status == "error" else "unproven",
        office_status=office_status,
        office_message_id=message_id,
    )


@dataclass(frozen=True)
class CodexDesktopTarget:
    """Operator-owned route. An Office message body cannot create or change one."""

    binding_id: str
    native_thread_id: str
    office_agent: str
    project_slug: str
    workspace: str
    relay_identity: str
    authorized: bool

    def __post_init__(self) -> None:
        if not isinstance(self.binding_id, str) or not _BINDING_RE.fullmatch(self.binding_id):
            _fail("invalid_binding_id", "binding_id must be a lowercase token")
        if not isinstance(self.native_thread_id, str) or not _UUID_RE.fullmatch(self.native_thread_id):
            _fail("invalid_native_thread_id", "native thread id must be a full lowercase UUID")
        office_agent = _agent(self.office_agent, "invalid_office_agent")
        relay_identity = _agent(self.relay_identity, "invalid_relay_identity")
        if not re.fullmatch(r"codex-[a-z0-9][a-z0-9-]{0,63}-" + re.escape(self.native_thread_id), office_agent):
            _fail("invalid_office_agent", "Office target must be the same strong Codex native thread")
        if not re.fullmatch(r"codex-[a-z0-9][a-z0-9-]{0,63}-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", relay_identity):
            _fail("invalid_relay_identity", "relay must have a strong Codex native session id")
        if office_agent == relay_identity:
            _fail("invalid_relay_identity", "relay identity must differ from the target agent")
        if not isinstance(self.project_slug, str) or not _SLUG_RE.fullmatch(self.project_slug):
            _fail("invalid_project_slug", "project_slug must be a lowercase token")
        object.__setattr__(self, "workspace", _workspace(self.workspace))
        if type(self.authorized) is not bool:
            _fail("invalid_target_registry", "authorized must be a boolean")


class TargetRegistry:
    """Immutable full-id registry. No mutators and no message-derived routes."""

    def __init__(self, targets: Iterable[CodexDesktopTarget]) -> None:
        items = tuple(targets)
        if not items:
            _fail("invalid_target_registry", "registry requires a target")
        by_binding: dict[str, CodexDesktopTarget] = {}
        by_thread: dict[str, CodexDesktopTarget] = {}
        for item in items:
            if type(item) is not CodexDesktopTarget:
                _fail("invalid_target_registry", "registry entries must be targets")
            if item.binding_id in by_binding or item.native_thread_id in by_thread:
                _fail("invalid_target_registry", "registry ids must be unique")
            by_binding[item.binding_id] = item
            by_thread[item.native_thread_id] = item
        self._by_binding = MappingProxyType(by_binding)
        self._by_thread = MappingProxyType(by_thread)

    def get(self, binding_id: str) -> CodexDesktopTarget | None:
        if not isinstance(binding_id, str):
            return None
        return self._by_binding.get(binding_id)

    @property
    def targets(self) -> Mapping[str, CodexDesktopTarget]:
        return self._by_binding


class CodexDesktopToolHost:
    """Prepare one fenced submit intent. The official tool call stays outside Python."""

    def __init__(self, client: Any, registry: TargetRegistry, *,
                 now: Callable[[], float] | None = None) -> None:
        request = getattr(client, "request", None)
        if not callable(request) or type(registry) is not TargetRegistry:
            _fail("invalid_office_client", "host requires an Office request client and registry")
        self._client = client
        self._registry = registry
        self._now = time.time if now is None else now
        self._watch_ready: set[str] = set()
        self._claim_attempted: set[str] = set()
        self._begin_attempted: set[str] = set()
        self._fenced: dict[str, dict[str, str]] = {}
        self._settled: dict[str, dict[str, str | None]] = {}
        self._proof_phase: dict[str, str] = {}

    def watch_request(self, binding_id: str, *, interval_s: int = 20, timeout_s: int = 3600,
                      delay_s: int = 0) -> dict[str, Any]:
        """Describe the existing watch call. This host does not run it or listen."""

        target = self._known(binding_id)
        if target is None:
            return _result("refused", "unknown_binding")
        if target.authorized is not True:
            return _result("refused", "unverified_project", binding_id=target.binding_id)
        bounds = (("interval_s", interval_s, 1, 240), ("timeout_s", timeout_s, 1, 3600),
                  ("delay_s", delay_s, 0, 3600))
        for _name, value, low, high in bounds:
            if type(value) is not int or not low <= value <= high:
                return _result("refused", "invalid_watch", binding_id=target.binding_id)
        return _result(
            "ok",
            "watch_request",
            binding_id=target.binding_id,
            executes=False,
            listener=False,
            op="wake-relay-watch",
            args={
                "binding_id": [target.binding_id],
                "interval_s": interval_s,
                "timeout_s": timeout_s,
                "delay_s": delay_s,
                "driver": DRIVER,
            },
        )

    def accept_watch(self, binding_id: str, watch_result: Mapping[str, Any]) -> dict[str, Any]:
        """Arm prepare only for one known pending watch event on this binding."""

        target = self._known(binding_id)
        if target is None:
            return _result("refused", "unknown_binding")
        if target.authorized is not True:
            return _result("refused", "unverified_project", binding_id=target.binding_id)
        event = self._watch_event(target.binding_id, watch_result)
        if event == "pending":
            self._watch_ready.add(target.binding_id)
            return _result(
                "ok",
                "watch_pending",
                binding_id=target.binding_id,
                event="pending",
                prepare_allowed=True,
                continuity={"event_1": "invoke_official_tool_once",
                            "event_2": "settle_actual_tool_receipt", "event_2_proven": False},
            )
        self._watch_ready.discard(target.binding_id)
        code = {"timeout": "watch_timeout", "stopped": "watch_stopped"}.get(event or "", "watch_unknown")
        return _result("refused", code, binding_id=target.binding_id, event=event or "unknown",
                       prepare_allowed=False)

    def observe(self, binding_id: str, app_observation: Mapping[str, Any]) -> dict[str, Any]:
        """Read-only Office snapshot plus the fresh app observation. No claim and no send."""

        return self._observe(binding_id, app_observation, mutate=False)

    def prepare(self, binding_id: str, app_observation: Mapping[str, Any]) -> dict[str, Any]:
        """After a known watch event, fence at most one intent. Do not invoke the tool."""

        target = self._known(binding_id)
        if target is None:
            return _result("refused", "unknown_binding")
        if target.authorized is not True:
            return _result("refused", "unverified_project", binding_id=target.binding_id)
        if target.binding_id not in self._watch_ready:
            return _result("refused", "watch_required", binding_id=target.binding_id)
        self._watch_ready.discard(target.binding_id)
        return self._observe(target.binding_id, app_observation, mutate=True)

    def settle(self, dispatch_id: str, tool_receipt: Any) -> dict[str, Any]:
        """Record the actual tool receipt. Never treats stored or queued as started."""

        if not isinstance(dispatch_id, str) or dispatch_id not in self._fenced:
            return _result("refused", "not_fenced")
        if tool_receipt is None:
            return _result("refused", "receipt_missing", dispatch_id=dispatch_id, delivery="unknown")
        parsed = map_tool_receipt(tool_receipt)
        previous = self._settled.get(dispatch_id)
        if previous is not None:
            same = previous.get("message_id") == parsed.get("office_message_id") and previous.get(
                "office_status") == parsed.get("office_status")
            return _result(
                "ok" if same else "refused",
                "receipt_idempotent" if same else "receipt_conflict",
                dispatch_id=dispatch_id,
                delivery=previous.get("delivery") or "unknown",
                message_id=previous.get("message_id"),
                office_receipt=previous.get("office_status") or "unwritten",
            )
        office_status = parsed.get("office_status")
        message_id = parsed.get("office_message_id")
        if office_status not in {"unconfirmed", "error"}:
            actual = _payload_object(tool_receipt)
            fence = self._fenced[dispatch_id]
            if (actual is not None and set(actual) == {"threadId"}
                    and actual.get("threadId") == fence["thread_id"]):
                self._settled[dispatch_id] = {
                    "message_id": None, "turn_id": None,
                    "office_status": "delivery_unknown", "delivery": "unknown",
                    "binding_id": fence["binding_id"], "dispatch_id": dispatch_id,
                }
            return _result("ok", "receipt_unknown", dispatch_id=dispatch_id, delivery="unknown",
                           office_receipt="unwritten")
        turn_id = parsed.get("turn_id")
        if not isinstance(message_id, str) and not isinstance(turn_id, str):
            return _result("refused", "receipt_missing", dispatch_id=dispatch_id, delivery="unknown")
        receipt_args = {
            "dispatch_id": dispatch_id, "status": office_status,
            "detail": "tool_error" if office_status == "error" else "unverified_tool_receipt",
            "driver": DRIVER,
        }
        if message_id is not None:
            receipt_args["message_id"] = message_id
        if turn_id is not None:
            receipt_args["turn_id"] = turn_id
        response = self._request(
            "wake-relay-receipt",
            self._fenced[dispatch_id]["relay_identity"],
            receipt_args,
        )
        if not self._ok(response, "wake_relay_receipt"):
            return _result("refused", "receipt_unwritten", dispatch_id=dispatch_id, delivery="unknown",
                           office_receipt="unwritten")
        stored = {
            "message_id": message_id,
            "turn_id": parsed.get("turn_id"),
            "office_status": office_status,
            "delivery": parsed.get("delivery") or "unknown",
            "binding_id": self._fenced[dispatch_id]["binding_id"],
            "dispatch_id": dispatch_id,
        }
        self._settled[dispatch_id] = stored
        return _result(
            "ok",
            "receipt_recorded",
            dispatch_id=dispatch_id,
            delivery=stored["delivery"],
            message_id=message_id,
            turn_id=stored["turn_id"],
            status_token=parsed.get("status_token"),
            office_receipt=office_status,
        )

    def reconcile(self, dispatch_id: str, proof: Mapping[str, Any],
                  office_ack: Mapping[str, Any]) -> dict[str, Any]:
        """Accept real start or an ended thread only with a correlated Office ACK.

        A matching proof is host acceptance of the supplied metadata. It does not
        mark Desktop UAT as run, and it does not call the official tool.
        """

        if not isinstance(dispatch_id, str) or dispatch_id not in self._settled:
            return _result("refused", "settlement_missing")
        settled = self._settled[dispatch_id]
        if settled.get("office_status") not in {"unconfirmed", "delivery_unknown", "delivered"}:
            return _result("refused", "not_reconcilable", dispatch_id=dispatch_id)
        target = self._registry.get(str(settled.get("binding_id")))
        checked = None if target is None else self._proof(target, settled, proof)
        if target is None or checked is None:
            return _result("refused", "invalid_proof", dispatch_id=dispatch_id)
        phase = checked
        previous = self._proof_phase.get(dispatch_id)
        if previous == "completed" or previous == phase:
            return _result(
                "ok",
                "proof_idempotent",
                dispatch_id=dispatch_id,
                started=True,
                completed=previous == "completed",
                ack_correlated=True,
            )
        if previous == "started" and phase != "completed":
            return _result("refused", "proof_conflict", dispatch_id=dispatch_id, started=True)
        ack = self._ack(target, dispatch_id, office_ack)
        if ack is None:
            return _result("refused", "ack_correlation_mismatch", dispatch_id=dispatch_id)
        delivery = self._request(
            "delivery",
            target.relay_identity,
            {"id": ack["message_id"], "channel": ack["channel"]},
        )
        if not self._delivery_matches(delivery, ack, target.office_agent):
            return _result("refused", "ack_missing", dispatch_id=dispatch_id)
        if settled.get("office_status") == "delivery_unknown":
            # The real app send returned only threadId. Promote only after a fresh
            # different native turn AND the exact target's durable Office ACK.
            receipt = self._request("wake-relay-receipt", target.relay_identity, {
                "dispatch_id": dispatch_id, "status": "delivered",
                "turn_id": proof["turn_id"], "detail": "correlated_office_ack_and_new_native_turn",
                "driver": DRIVER,
            })
            if not self._ok(receipt, "wake_relay_receipt"):
                return _result("refused", "receipt_unwritten", dispatch_id=dispatch_id)
            settled["office_status"] = "delivered"
            settled["turn_id"] = proof["turn_id"]
        stage = "target_finished" if phase == "completed" else "target_started"
        observed = self._request(
            "wake-relay-observe",
            target.relay_identity,
            {"dispatch_id": dispatch_id, "stage": stage, "driver": DRIVER},
        )
        if not self._ok(observed, "wake_relay_observed"):
            return _result("refused", "observe_rejected", dispatch_id=dispatch_id)
        self._proof_phase[dispatch_id] = phase
        return _result(
            "ok",
            "proof_accepted",
            dispatch_id=dispatch_id,
            started=True,
            completed=phase == "completed",
            delivery=phase,
            ack_correlated=True,
        )

    def _known(self, binding_id: str) -> CodexDesktopTarget | None:
        if not isinstance(binding_id, str):
            return None
        return self._registry.get(binding_id)

    def _watch_event(self, binding_id: str, watch_result: Mapping[str, Any]) -> str | None:
        if type(watch_result) is not dict or watch_result.get("status") != "ok":
            return None
        if watch_result.get("code") != "wake_relay_watch":
            return None
        if watch_result.get("binding_ids") != [binding_id]:
            return None
        event = watch_result.get("result")
        if event not in {"pending", "timeout", "stopped"}:
            return None
        return event

    def _observe(self, binding_id: str, app_observation: Mapping[str, Any], *,
                 mutate: bool) -> dict[str, Any]:
        target = self._known(binding_id)
        if target is None:
            return _result("refused", "unknown_binding")
        if target.authorized is not True:
            return _result("refused", "unverified_project", binding_id=target.binding_id)
        kind = self._classification(target, app_observation)
        if kind != "idle" and not (mutate and kind in {"busy", "quota"}):
            code = {
                "stale": "stale_observation",
                "mismatch": "target_mismatch",
                "unverified": "unverified_project",
                "invalid": "invalid_observation",
                "quota": "target_quota",
                "busy": "target_busy",
            }.get(kind, "target_unknown")
            return _result("refused", code, binding_id=target.binding_id, thread_state=kind)
        if kind == "quota":
            stop = self._stop(target)
            if stop != "clear":
                return _result("refused", stop, binding_id=target.binding_id)
            return _result("refused", "target_quota", binding_id=target.binding_id)
        stop = self._stop(target)
        if stop != "clear":
            return _result("refused", stop, binding_id=target.binding_id)
        selected = self._select(target)
        if isinstance(selected, str):
            return _result("refused", selected, binding_id=target.binding_id)
        if not mutate:
            return _result(
                "ok",
                "observed",
                binding_id=target.binding_id,
                dispatch_id=selected["dispatch_id"],
                thread_state=kind,
                action="eligible_for_one_prepare" if kind == "idle" else "defer_before_send",
            )
        if kind == "busy":
            deferred = self._request(
                "wake-relay-defer-busy",
                target.relay_identity,
                {"dispatch_id": selected["dispatch_id"], "driver": DRIVER},
            )
            code = deferred.get("code") if isinstance(deferred, dict) else None
            if code == "wake_relay_busy_limit":
                return _result("refused", "target_busy_limit", binding_id=target.binding_id,
                               dispatch_id=selected["dispatch_id"], terminal=True)
            if code != "wake_relay_deferred_busy":
                return _result("refused", "defer_failed", binding_id=target.binding_id,
                               dispatch_id=selected["dispatch_id"])
            return _result("deferred", "target_busy", binding_id=target.binding_id,
                           dispatch_id=selected["dispatch_id"])
        return self._fence(target, selected, app_observation.get("latest_turn_id"))

    def _classification(self, target: CodexDesktopTarget, observation: Mapping[str, Any]) -> str:
        if type(observation) is not dict or set(observation) != _OBS_FIELDS:
            return "invalid"
        prior_turn = observation.get("latest_turn_id")
        if prior_turn is not None and (not isinstance(prior_turn, str) or not _UUID_RE.fullmatch(prior_turn)):
            return "invalid"
        if (observation.get("schema") != OBSERVATION_SCHEMA
                or observation.get("source") != OBSERVATION_SOURCE):
            return "invalid"
        if not _fresh(observation.get("observed_at"), now=float(self._now())):
            return "stale"
        if (observation.get("project_slug") != target.project_slug
                or observation.get("workspace") != target.workspace):
            return "unverified"
        if observation.get("native_thread_id") != target.native_thread_id:
            return "mismatch"
        state = observation.get("thread_state")
        if state in {"idle", "busy", "quota"}:
            return state
        return "unknown"

    def _stop(self, target: CodexDesktopTarget) -> str:
        response = self._request("wake-stop-status", target.relay_identity, {})
        if not isinstance(response, dict) or response.get("status") != "ok":
            return "stop_unavailable"
        if response.get("stop_active") is not False:
            return "global_stop"
        return "clear"

    def _select(self, target: CodexDesktopTarget) -> dict[str, str] | str:
        response = self._request(
            "wake-relay-pending",
            target.relay_identity,
            {"binding_id": target.binding_id, "driver": DRIVER},
        )
        if not self._ok(response, "wake_relay_pending"):
            return "office_unavailable"
        rows = response.get("dispatches")
        if not isinstance(rows, list) or len(rows) > MAX_PENDING_ROWS:
            return "office_unavailable"
        chosen: list[dict[str, str]] = []
        for row in rows:
            if type(row) is not dict or row.get("binding_id") != target.binding_id:
                continue
            if row.get("status") != "pending":
                continue
            dispatch_id = row.get("dispatch_id")
            trigger_ref = row.get("trigger_ref")
            text = row.get("text")
            if (not isinstance(dispatch_id, str) or not _HEX32_RE.fullmatch(dispatch_id)
                    or not isinstance(trigger_ref, str) or not trigger_ref
                    or len(trigger_ref) > 1024 or "\x00" in trigger_ref):
                return "invalid_relay_dispatch"
            if row.get("target_local_id") != target.native_thread_id:
                return "target_mismatch"
            if not self._relay_text(text):
                return "invalid_relay_text"
            created = row.get("created_at")
            chosen.append(
                {
                    "dispatch_id": dispatch_id,
                    "trigger_ref": trigger_ref,
                    "text": text,
                    "created_at": created if isinstance(created, str) else "",
                }
            )
        if not chosen:
            return "no_pending"
        chosen.sort(key=lambda item: (item["created_at"], item["dispatch_id"]))
        return chosen[0]

    def _fence(self, target: CodexDesktopTarget, selected: Mapping[str, str], prior_turn: str | None) -> dict[str, Any]:
        dispatch_id = selected["dispatch_id"]
        if dispatch_id in self._claim_attempted or dispatch_id in self._begin_attempted:
            return _result("refused", "duplicate_submit", binding_id=target.binding_id,
                           dispatch_id=dispatch_id)
        self._claim_attempted.add(dispatch_id)
        claimed = self._request(
            "wake-relay-claim",
            target.relay_identity,
            {"dispatch_id": dispatch_id, "relay_agent": target.relay_identity, "driver": DRIVER},
        )
        if not self._dispatch_code(claimed, "wake_relay_claimed", "claimed", dispatch_id):
            return _result("refused", "claim_unconfirmed", binding_id=target.binding_id,
                           dispatch_id=dispatch_id)
        self._begin_attempted.add(dispatch_id)
        begun = self._request(
            "wake-relay-native-begin",
            target.relay_identity,
            {"dispatch_id": dispatch_id, "relay_agent": target.relay_identity, "driver": DRIVER},
        )
        if not self._dispatch_code(begun, "wake_native_submission_fenced", "delivery_unknown",
                                   dispatch_id):
            return _result("refused", "begin_unconfirmed", binding_id=target.binding_id,
                           dispatch_id=dispatch_id, pre_submit="unconfirmed")
        self._fenced[dispatch_id] = {
            "binding_id": target.binding_id,
            "relay_identity": target.relay_identity,
            "trigger_ref": selected["trigger_ref"],
            "thread_id": target.native_thread_id,
            "prior_turn_id": prior_turn,
        }
        submit = {
            "tool": OFFICIAL_SEND_TOOL,
            "arguments": {"threadId": target.native_thread_id, "hostId": "local", "prompt": selected["text"]},
            "once": True,
        }
        return _result(
            "ready",
            "submit_once",
            binding_id=target.binding_id,
            dispatch_id=dispatch_id,
            pre_submit="delivery_unknown",
            submit=submit,
            continuity={"event_1": "invoke_official_tool_once",
                        "event_2": "settle_actual_tool_receipt", "event_2_proven": False},
        )

    def _proof(self, target: CodexDesktopTarget, settled: Mapping[str, str | None],
               proof: Mapping[str, Any]) -> str | None:
        if type(proof) is not dict or set(proof) not in {_PROOF_FIELDS, _PROOF_FIELDS - {"turn_id"}}:
            return None
        if proof.get("schema") != PROOF_SCHEMA or not _fresh(proof.get("observed_at"), now=float(self._now())):
            return None
        if proof.get("native_thread_id") != target.native_thread_id:
            return None
        if proof.get("message_id") != settled.get("message_id"):
            return None
        turn = proof.get("turn_id") if "turn_id" in proof else None
        if "turn_id" in proof and (not isinstance(turn, str) or _UUID_RE.fullmatch(turn) is None):
            return None
        settled_turn = settled.get("turn_id")
        if isinstance(settled_turn, str) and turn not in {None, settled_turn}:
            return None
        if settled.get("message_id") is None:
            fence = self._fenced[settled["dispatch_id"]]
            if turn is None or turn == fence.get("prior_turn_id"):
                return None
        phase = proof.get("phase")
        ended = proof.get("thread_ended")
        if phase == "started" and ended is False:
            return phase
        if phase == "completed" and ended is True:
            return phase
        return None

    def _ack(self, target: CodexDesktopTarget, dispatch_id: str,
             office_ack: Mapping[str, Any]) -> dict[str, str] | None:
        if type(office_ack) is not dict or set(office_ack) != _ACK_FIELDS:
            return None
        if office_ack.get("schema") != ACK_SCHEMA:
            return None
        message_id = office_ack.get("message_id")
        ack_id = office_ack.get("ack_id")
        channel = office_ack.get("channel")
        trigger_ref = self._fenced[dispatch_id]["trigger_ref"]
        if (not isinstance(message_id, str) or message_id != trigger_ref
                or not _HEX32_RE.fullmatch(message_id)
                or not isinstance(ack_id, str) or not _HEX32_RE.fullmatch(ack_id)
                or not isinstance(channel, str) or not _CHANNEL_RE.fullmatch(channel)):
            return None
        if office_ack.get("from_agent") != target.office_agent:
            return None
        return {"message_id": message_id, "ack_id": ack_id, "channel": channel}

    def _delivery_matches(self, response: Any, ack: Mapping[str, str], agent: str) -> bool:
        if not isinstance(response, dict) or response.get("status") != "ok":
            return False
        if response.get("state") != "acknowledged":
            return False
        acknowledged = response.get("acknowledged_by")
        rows = response.get("acks")
        if not isinstance(acknowledged, list) or agent not in acknowledged or not isinstance(rows, list):
            return False
        return any(
            isinstance(row, dict) and row.get("agent") == agent and row.get("ack_id") == ack["ack_id"]
            for row in rows
        )

    def _request(self, op: str, relay_identity: str, args: Mapping[str, Any]) -> Any:
        if op not in HOST_OPS or (op in RELAY_OPS and args.get("driver") != DRIVER):
            _fail("invalid_office_op", "host op is outside the Office protocol seam")
        identity = {"agent_id": relay_identity}
        try:
            return self._client.request(op, identity, dict(args))
        except Exception:
            return {"status": "degraded", "reason": "office_unavailable"}

    @staticmethod
    def _ok(response: Any, code: str) -> bool:
        return isinstance(response, dict) and response.get("status") == "ok" and response.get("code") == code

    @staticmethod
    def _dispatch_code(response: Any, code: str, status: str, dispatch_id: str) -> bool:
        if not isinstance(response, dict) or response.get("status") != "ok" or response.get("code") != code:
            return False
        dispatch = response.get("dispatch")
        return (
            isinstance(dispatch, dict)
            and dispatch.get("dispatch_id") == dispatch_id
            and dispatch.get("status") == status
        )

    @staticmethod
    def _relay_text(value: Any) -> bool:
        if not isinstance(value, str) or not value or len(value) > MAX_RELAY_TEXT_CHARS or "\x00" in value:
            return False
        return not any(ord(char) < 32 and char not in "\t\r\n" for char in value)
