"""ZCode Desktop external-CU tool host.

This module speaks only to an injected Office client ``request(op, identity, args)``.
It never imports Wake, never opens a local Office store, and never invokes UI or
official CU tools. The parent UAT makes that one external official CU call.

Prepare path: trusted 120 s idle observation -> server desktop-input lease ->
STOP -> remote claim -> STOP -> remote native-begin -> one fenced one-use intent.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping

from coordfs.desktop_adapters import (
    DESKTOP_INPUT_RESOURCE,
    MAX_LEASE_TTL_S,
    USER_IDLE_REQUIRED_S,
    classify_observation,
    is_opaque_lease_digest,
    validate_lease_claim,
)
from coordfs.errors import CoordError

DRIVER = "zcode-desktop-relay"
OFFICIAL_CU_TOOL = "computer_use"
OFFICIAL_CU_ARGUMENT_KEYS = frozenset(
    {"session_id", "project_slug", "workspace", "text"}
)
RECEIPT_SCHEMA = "zcode-external-cu-receipt/v1"
MAX_RECEIPT_BYTES = 4096
MAX_RELAY_TEXT_CHARS = 8192
MAX_PENDING_ROWS = 20
SUBMIT_SETTLE_DEADLINE_S = 60

HOST_OPS = frozenset(
    {
        "wake-stop-status",
        "wake-relay-pending",
        "wake-relay-claim",
        "wake-relay-native-begin",
        "wake-relay-native-unsubmitted",
        "wake-relay-receipt",
        "wake-relay-ack",
        "wake-relay-observe",
        "wake-desktop-input-claim",
        "wake-desktop-input-release",
    }
)
RELAY_OPS = frozenset(
    {
        "wake-relay-pending",
        "wake-relay-claim",
        "wake-relay-native-begin",
        "wake-relay-native-unsubmitted",
        "wake-relay-receipt",
        "wake-relay-ack",
        "wake-relay-observe",
    }
)

_BINDING_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}\Z")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_NATIVE_SESSION_RE = re.compile(r"^sess_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_CODEX_RELAY_RE = re.compile(r"^codex-[a-z0-9][a-z0-9-]{0,63}-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
_HEX32_RE = re.compile(r"^[0-9a-f]{32}\Z")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}\Z")
_AGENT_RE = re.compile(
    r"^[a-z][a-z0-9-]{0,63}-[a-z0-9][a-z0-9-]{0,63}-[a-z0-9][a-z0-9._-]{0,255}\Z"
)

_IMMUTABLE = MappingProxyType(
    {
        "desktop_uat": "not_run",
        "python_invokes_official_cu": False,
        "idle_policy_s": USER_IDLE_REQUIRED_S,
        "idle_policy_override": False,
    }
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
_STATUS_RE = re.compile(r"^[a-z][a-z_]{0,31}\Z")


def _fail(code: str, message: str) -> None:
    raise CoordError(message, code=code)


def _agent(value: Any, code: str) -> str:
    if not isinstance(value, str) or not _AGENT_RE.fullmatch(value):
        _fail(code, "Office agent must be a canonical strong id")
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
        path_parts = []
    if any(part in {"", ".", ".."} for part in path_parts) or not (windows or posix):
        _fail("invalid_workspace", "workspace must be an absolute path without traversal")
    return value


def _result(
    status: str,
    code: str,
    *,
    started: bool = False,
    completed: bool = False,
    submit: Mapping[str, Any] | None = None,
    **fields: Any,
) -> dict[str, Any]:
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


def map_cu_receipt(payload: Any) -> dict[str, Any]:
    """Parse one external CU caller result without inventing delivery.

    Missing authentic id or status stays unknown and is never resent. Stored or
    queued tokens are not a started CU call.
    """
    parsed = _result("ok", "receipt_unknown")
    data = _payload_object(payload)
    if data is None:
        return parsed
    call_id = data.get("call_id")
    if not isinstance(call_id, str) or _UUID_RE.fullmatch(call_id) is None:
        call_id = None
    turn_id = data.get("turn_id")
    if not isinstance(turn_id, str) or _UUID_RE.fullmatch(turn_id) is None:
        turn_id = None
    status = _status_token(data)
    if (call_id is None and turn_id is None) or status is None:
        return parsed
    office_status = "error" if status in FAILURE_RECEIPT_STATUSES else "unconfirmed"
    return _result(
        "ok",
        "receipt_" + office_status,
        call_id=call_id,
        turn_id=turn_id,
        status_token=status,
        delivery="error" if office_status == "error" else "unproven",
        office_status=office_status,
    )


@dataclass(frozen=True)
class ZCodeDesktopTarget:
    """Operator-owned route. An Office message body cannot create or change one."""

    binding_id: str
    native_session_id: str
    office_agent: str
    project_slug: str
    workspace: str
    relay_identity: str
    authorized: bool

    def __post_init__(self) -> None:
        if not isinstance(self.binding_id, str) or not _BINDING_RE.fullmatch(self.binding_id):
            _fail("invalid_binding_id", "binding_id must be a lowercase token")
        if not isinstance(self.native_session_id, str) or not _NATIVE_SESSION_RE.fullmatch(self.native_session_id):
            _fail("invalid_native_session_id", "native session id must be a full sess_ UUID")
        office_agent = _agent(self.office_agent, "invalid_office_agent")
        relay_identity = _agent(self.relay_identity, "invalid_relay_identity")
        if not re.fullmatch(
            r"zcode-ailab-" + re.escape(self.native_session_id),
            office_agent,
        ):
            _fail("invalid_office_agent", "Office target must be the same strong ZCode native session")
        if not _CODEX_RELAY_RE.fullmatch(relay_identity):
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

    def __init__(self, targets: Iterable[ZCodeDesktopTarget]) -> None:
        items = tuple(targets)
        if not items:
            _fail("invalid_target_registry", "registry requires a target")
        by_binding: dict[str, ZCodeDesktopTarget] = {}
        by_session: dict[str, ZCodeDesktopTarget] = {}
        for item in items:
            if type(item) is not ZCodeDesktopTarget:
                _fail("invalid_target_registry", "registry entries must be targets")
            if item.binding_id in by_binding or item.native_session_id in by_session:
                _fail("invalid_target_registry", "registry ids must be unique")
            by_binding[item.binding_id] = item
            by_session[item.native_session_id] = item
        self._by_binding = MappingProxyType(by_binding)
        self._by_session = MappingProxyType(by_session)

    def get(self, binding_id: str) -> ZCodeDesktopTarget | None:
        if not isinstance(binding_id, str):
            return None
        return self._by_binding.get(binding_id)

    @property
    def targets(self) -> Mapping[str, ZCodeDesktopTarget]:
        return self._by_binding


class ZCodeDesktopToolHost:
    """Prepare one fenced external-CU intent. The official CU call stays outside Python."""

    def __init__(
        self,
        client: Any,
        registry: TargetRegistry,
        *,
        now: Callable[[], float] | None = None,
    ) -> None:
        request = getattr(client, "request", None)
        if not callable(request) or type(registry) is not TargetRegistry:
            _fail("invalid_office_client", "host requires an Office request client and registry")
        self._client = client
        self._registry = registry
        self._now = time.time if now is None else now
        self._claim_attempted: set[str] = set()
        self._begin_attempted: set[str] = set()
        self._fenced: dict[str, dict[str, Any]] = {}
        self._settled: dict[str, dict[str, Any]] = {}
        self._leases: dict[str, dict[str, str]] = {}
        self._observed_stages: dict[str, set[str]] = {}
        self._fenced_at: dict[str, float] = {}
        self._submit_calls: dict[str, int] = {}
        self._restored: dict[str, bool] = {}
        self._drifted: set[str] = set()

    def observe(self, binding_id: str, app_observation: Mapping[str, Any]) -> dict[str, Any]:
        """Read-only classification. No lease, no claim, no native-begin."""
        target = self._known(binding_id)
        if target is None:
            return _result("refused", "unknown_binding")
        if target.authorized is not True:
            return _result("refused", "unverified_project", binding_id=target.binding_id)
        classified = self._classification(target, app_observation)
        if classified["kind"] != "idle":
            return _result(
                "refused",
                classified["code"],
                binding_id=target.binding_id,
                observation_kind=classified["kind"],
            )
        stop = self._stop(target)
        if stop != "clear":
            return _result("refused", stop, binding_id=target.binding_id)
        return _result(
            "ok",
            "observed_idle",
            binding_id=target.binding_id,
            user_idle_s=classified.get("user_idle_s"),
            action="eligible_for_one_prepare",
        )

    def prepare(self, binding_id: str, app_observation: Mapping[str, Any]) -> dict[str, Any]:
        """Validate a trusted idle sample and fence at most one external CU intent."""
        target = self._known(binding_id)
        if target is None:
            return _result("refused", "unknown_binding")
        if target.authorized is not True:
            return _result("refused", "unverified_project", binding_id=target.binding_id)
        classified = self._classification(target, app_observation)
        if classified["kind"] != "idle":
            return _result(
                "refused",
                classified["code"],
                binding_id=target.binding_id,
                observation_kind=classified["kind"],
            )
        stop = self._stop(target)
        if stop != "clear":
            return _result("refused", stop, binding_id=target.binding_id)
        selected = self._select(target)
        if isinstance(selected, str):
            return _result("refused", selected, binding_id=target.binding_id)
        dispatch_id = selected["dispatch_id"]
        if dispatch_id in self._claim_attempted or dispatch_id in self._begin_attempted:
            return _result("refused", "duplicate_submit", binding_id=target.binding_id, dispatch_id=dispatch_id)

        lease = self._acquire_lease(target)
        if isinstance(lease, str):
            # Exclusive desktop-input lease is required before wake-relay-claim.
            return _result("refused", lease, binding_id=target.binding_id, dispatch_id=dispatch_id)

        stop = self._stop(target)
        if stop != "clear":
            self._release_lease(target, lease)
            return _result("refused", stop, binding_id=target.binding_id, dispatch_id=dispatch_id)

        self._claim_attempted.add(dispatch_id)
        claimed = self._request(
            "wake-relay-claim",
            target.relay_identity,
            {"dispatch_id": dispatch_id, "relay_agent": target.relay_identity, "driver": DRIVER},
        )
        if not self._dispatch_code(claimed, "wake_relay_claimed", "claimed", dispatch_id):
            self._release_lease(target, lease)
            return _result("refused", "claim_unconfirmed", binding_id=target.binding_id, dispatch_id=dispatch_id)

        stop = self._stop(target)
        if stop != "clear":
            # Release only this caller's pre-submit claim; never un-fence a begin.
            self._request(
                "wake-relay-native-unsubmitted",
                target.relay_identity,
                {"dispatch_id": dispatch_id, "relay_agent": target.relay_identity, "driver": DRIVER},
            )
            self._release_lease(target, lease)
            return _result(
                "refused",
                stop,
                binding_id=target.binding_id,
                dispatch_id=dispatch_id,
                pre_submit="released",
            )

        self._begin_attempted.add(dispatch_id)
        begun = self._request(
            "wake-relay-native-begin",
            target.relay_identity,
            {"dispatch_id": dispatch_id, "relay_agent": target.relay_identity, "driver": DRIVER},
        )
        if not self._dispatch_code(begun, "wake_native_submission_fenced", "delivery_unknown", dispatch_id):
            self._release_lease(target, lease)
            return _result(
                "refused",
                "begin_unconfirmed",
                binding_id=target.binding_id,
                dispatch_id=dispatch_id,
                pre_submit="unconfirmed",
            )

        self._leases[dispatch_id] = lease
        self._fenced_at[dispatch_id] = float(self._now())
        self._submit_calls[dispatch_id] = 0
        self._restored[dispatch_id] = False
        self._drifted.discard(dispatch_id)
        self._fenced[dispatch_id] = {
            "binding_id": target.binding_id,
            "relay_identity": target.relay_identity,
            "trigger_ref": selected["trigger_ref"],
            "native_session_id": target.native_session_id,
            "project_slug": target.project_slug,
            "workspace": target.workspace,
            "text": selected["text"],
        }
        submit = {
            "tool": OFFICIAL_CU_TOOL,
            "once": True,
            "binding_id": target.binding_id,
            "dispatch_id": dispatch_id,
            "relay_identity": target.relay_identity,
            "arguments": {
                "session_id": target.native_session_id,
                "project_slug": target.project_slug,
                "workspace": target.workspace,
                "text": selected["text"],
            },
        }
        return _result(
            "ready",
            "submit_once",
            binding_id=target.binding_id,
            dispatch_id=dispatch_id,
            pre_submit="delivery_unknown",
            submit=submit,
            continuity={
                "event_1": "invoke_official_cu_once_outside_python",
                "event_2": "settle_actual_caller_result",
                "event_2_proven": False,
            },
        )

    def settle(self, dispatch_id: str, cu_receipt: Any) -> dict[str, Any]:
        """Record the actual authenticated caller CU result. Never resend unknown."""
        if not isinstance(dispatch_id, str) or dispatch_id not in self._fenced:
            return _result("refused", "not_fenced")
        if previous_settled := self._settled.get(dispatch_id):
            previous = previous_settled
        else:
            previous = None
            if self._expired(dispatch_id):
                self._settled[dispatch_id] = {
                    "call_id": None,
                    "turn_id": None,
                    "office_status": "delivery_unknown",
                    "delivery": "unknown",
                    "binding_id": self._fenced[dispatch_id]["binding_id"],
                    "dispatch_id": dispatch_id,
                }
                self._release_lease_for(dispatch_id)
                return _result(
                    "refused",
                    "lease_expired",
                    dispatch_id=dispatch_id,
                    delivery="unknown",
                    office_receipt="unwritten",
                    resent=False,
                )
        parsed = map_cu_receipt(cu_receipt) if cu_receipt is not None else _result(
            "ok", "receipt_unknown", delivery="unknown", office_status="delivery_unknown"
        )
        if previous is not None:
            previous_unknown = previous.get("office_status") == "delivery_unknown"
            incoming_known = parsed.get("office_status") in {"unconfirmed", "error"}
            if previous_unknown and not incoming_known:
                return _result(
                    "ok",
                    "receipt_idempotent",
                    dispatch_id=dispatch_id,
                    delivery="unknown",
                    office_receipt="delivery_unknown",
                    resent=False,
                )
            same = (
                previous.get("call_id") == parsed.get("call_id")
                and previous.get("office_status") == parsed.get("office_status")
            )
            return _result(
                "ok" if same else "refused",
                "receipt_idempotent" if same else "receipt_conflict",
                dispatch_id=dispatch_id,
                delivery=previous.get("delivery") or "unknown",
                office_receipt=previous.get("office_status") or "unwritten",
            )

        fence = self._fenced[dispatch_id]
        office_status = parsed.get("office_status")
        call_id = parsed.get("call_id")
        turn_id = parsed.get("turn_id")
        if office_status not in {"unconfirmed", "error"}:
            # Missing or ambiguous CU result is durably fenced and never resent.
            self._settled[dispatch_id] = {
                "call_id": None,
                "turn_id": None,
                "office_status": "delivery_unknown",
                "delivery": "unknown",
                "binding_id": fence["binding_id"],
                "dispatch_id": dispatch_id,
            }
            self._release_lease_for(dispatch_id)
            return _result(
                "ok",
                "receipt_unknown",
                dispatch_id=dispatch_id,
                delivery="unknown",
                office_receipt="unwritten",
                resent=False,
            )
        if not isinstance(call_id, str) and not isinstance(turn_id, str):
            self._settled[dispatch_id] = {
                "call_id": None,
                "turn_id": None,
                "office_status": "delivery_unknown",
                "delivery": "unknown",
                "binding_id": fence["binding_id"],
                "dispatch_id": dispatch_id,
            }
            self._release_lease_for(dispatch_id)
            return _result(
                "ok",
                "receipt_unknown",
                dispatch_id=dispatch_id,
                delivery="unknown",
                office_receipt="unwritten",
                resent=False,
            )
        receipt_args = {
            "dispatch_id": dispatch_id,
            "status": office_status,
            "detail": "tool_error" if office_status == "error" else "authenticated_caller_result",
            "driver": DRIVER,
        }
        if call_id is not None:
            receipt_args["message_id"] = call_id
        if turn_id is not None:
            receipt_args["turn_id"] = turn_id
        response = self._request(
            "wake-relay-receipt",
            fence["relay_identity"],
            receipt_args,
        )
        if not self._ok(response, "wake_relay_receipt"):
            return _result(
                "refused",
                "receipt_unwritten",
                dispatch_id=dispatch_id,
                delivery="unknown",
                office_receipt="unwritten",
            )
        stored = {
            "call_id": call_id,
            "turn_id": turn_id,
            "office_status": office_status,
            "delivery": parsed.get("delivery") or "unknown",
            "binding_id": fence["binding_id"],
            "dispatch_id": dispatch_id,
        }
        self._settled[dispatch_id] = stored
        self._release_lease_for(dispatch_id)
        return _result(
            "ok",
            "receipt_recorded",
            dispatch_id=dispatch_id,
            delivery=stored["delivery"],
            call_id=call_id,
            turn_id=turn_id,
            status_token=parsed.get("status_token"),
            office_receipt=office_status,
        )

    def reconcile(self, dispatch_id: str, stage: str) -> dict[str, Any]:
        """Correlated idempotent observe after settle. No claim and no resend."""
        if not isinstance(dispatch_id, str) or dispatch_id not in self._settled:
            return _result("refused", "settlement_missing")
        if stage not in {"target_started", "target_finished"}:
            return _result("refused", "invalid_stage", dispatch_id=dispatch_id)
        fence = self._fenced[dispatch_id]
        settled = self._settled[dispatch_id]
        if settled.get("office_status") == "delivery_unknown":
            # External CU returns no native send_message identifier. Reconcile
            # only through the server's metadata-only proof of an actual target
            # reply correlated to this dispatch's exact Office trigger.
            proof = self._request(
                "wake-relay-ack",
                fence["relay_identity"],
                {"dispatch_id": dispatch_id, "driver": DRIVER},
            )
            if not self._ok(proof, "wake_relay_ack"):
                return _result(
                    "refused",
                    "ack_unavailable",
                    dispatch_id=dispatch_id,
                    delivery="unknown",
                    resent=False,
                )
            ack_id = proof.get("ack_id") if isinstance(proof, Mapping) else None
            if (proof.get("acknowledged") is not True
                    or not isinstance(ack_id, str)
                    or _HEX32_RE.fullmatch(ack_id) is None):
                return _result(
                    "refused",
                    "ack_missing",
                    dispatch_id=dispatch_id,
                    delivery="unknown",
                    office_receipt="unwritten",
                    resent=False,
                )
            receipt = self._request(
                "wake-relay-receipt",
                fence["relay_identity"],
                {
                    "dispatch_id": dispatch_id,
                    "status": "delivered",
                    "message_id": ack_id,
                    "detail": "verified_correlated_office_reply",
                    "driver": DRIVER,
                },
            )
            if not self._ok(receipt, "wake_relay_receipt"):
                return _result(
                    "refused",
                    "receipt_unwritten",
                    dispatch_id=dispatch_id,
                    delivery="unknown",
                    office_receipt="unwritten",
                    resent=False,
                )
            settled = {
                **settled,
                "call_id": ack_id,
                "turn_id": None,
                "office_status": "delivered",
                "delivery": "delivered",
                "binding_id": fence["binding_id"],
                "dispatch_id": dispatch_id,
            }
            self._settled[dispatch_id] = settled
        stages = self._observed_stages.setdefault(dispatch_id, set())
        if stage in stages:
            return _result(
                "ok",
                "observe_idempotent",
                dispatch_id=dispatch_id,
                stage=stage,
                started=True,
                completed="target_finished" in stages,
            )
        observed = self._request(
            "wake-relay-observe",
            fence["relay_identity"],
            {"dispatch_id": dispatch_id, "stage": stage, "driver": DRIVER},
        )
        if not self._ok(observed, "wake_relay_observed"):
            return _result("refused", "observe_rejected", dispatch_id=dispatch_id)
        stages.add(stage)
        return _result(
            "ok",
            "observed",
            dispatch_id=dispatch_id,
            stage=stage,
            started=True,
            completed="target_finished" in stages,
        )

    def _known(self, binding_id: str) -> ZCodeDesktopTarget | None:
        if not isinstance(binding_id, str):
            return None
        return self._registry.get(binding_id)

    def _classification(self, target: ZCodeDesktopTarget, observation: Mapping[str, Any]) -> dict[str, Any]:
        return classify_observation(
            observation,
            native_session_id=target.native_session_id,
            project_slug=target.project_slug,
            workspace=target.workspace,
            binding_id=target.binding_id,
            now=float(self._now()),
        )

    def _stop(self, target: ZCodeDesktopTarget) -> str:
        response = self._request("wake-stop-status", target.relay_identity, {})
        if not isinstance(response, dict) or response.get("status") != "ok":
            return "stop_unavailable"
        if response.get("stop_active") is not False:
            return "global_stop"
        return "clear"

    def _select(self, target: ZCodeDesktopTarget) -> dict[str, str] | str:
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
            if (
                not isinstance(dispatch_id, str)
                or not _HEX32_RE.fullmatch(dispatch_id)
                or not isinstance(trigger_ref, str)
                or not trigger_ref
                or len(trigger_ref) > 1024
                or "\x00" in trigger_ref
            ):
                return "invalid_relay_dispatch"
            if row.get("target_local_id") != target.native_session_id:
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

    def _acquire_lease(self, target: ZCodeDesktopTarget) -> dict[str, str] | str:
        # Actor identity is never a request argument; only the authenticated
        # OfficeRemoteClient identity supplies the holder.
        response = self._request(
            "wake-desktop-input-claim",
            target.relay_identity,
            {"binding_id": target.binding_id, "ttl_s": MAX_LEASE_TTL_S},
        )
        checked = validate_lease_claim(
            response,
            binding_id=target.binding_id,
            holder=target.relay_identity,
        )
        if checked is None:
            return "lease_unavailable"
        return {
            "binding_id": target.binding_id,
            "lease_digest": checked["lease_digest"],
            "resource_id": DESKTOP_INPUT_RESOURCE,
        }

    def _release_lease(self, target: ZCodeDesktopTarget, lease: Mapping[str, str]) -> None:
        self._request(
            "wake-desktop-input-release",
            target.relay_identity,
            {
                "binding_id": lease["binding_id"],
                "expected_lease_digest": lease["lease_digest"],
            },
        )

    def _release_lease_for(self, dispatch_id: str) -> bool:
        lease = self._leases.get(dispatch_id)
        if lease is None:
            return False
        fence = self._fenced.get(dispatch_id)
        if fence is None:
            return False
        # Ownership guard: release only the exact own lease digest held for
        # this binding. A foreign/drifted lease is never released.
        # The stored digest is an authenticated own claim. Server-side CAS
        # rechecks current actor/binding; drift forbids submit, not own cleanup.
        if lease.get("binding_id") != fence.get("binding_id"):
            return False
        if not is_opaque_lease_digest(lease.get("lease_digest")):
            return False
        if self._submit_calls.get(dispatch_id, 0) and not self._restored.get(dispatch_id):
            return False
        released = self._request(
            "wake-desktop-input-release",
            fence["relay_identity"],
            {
                "binding_id": lease["binding_id"],
                "expected_lease_digest": lease["lease_digest"],
            },
        )
        if not self._ok(released, "released"):
            return False
        self._leases.pop(dispatch_id, None)
        return True

    def deadline_remaining_s(self, dispatch_id: str) -> float | None:
        """Remaining seconds of the bounded submit/settle lifetime, or None."""
        started = self._fenced_at.get(dispatch_id)
        if started is None or dispatch_id not in self._fenced:
            return None
        return (started + SUBMIT_SETTLE_DEADLINE_S) - float(self._now())

    def _expired(self, dispatch_id: str) -> bool:
        remaining = self.deadline_remaining_s(dispatch_id)
        return remaining is not None and remaining <= 0

    def renew_lease(self, dispatch_id: str) -> dict[str, Any]:
        """Re-validate the owned desktop-input lease inside the 60 s lifetime.

        A fresh claim with a different digest/binding means the lease drifted
        to a foreign owner: the dispatch is flagged, the foreign lease is
        never released, and no retry is scheduled.
        """
        if not isinstance(dispatch_id, str) or dispatch_id not in self._fenced:
            return _result("refused", "not_fenced")
        if dispatch_id in self._drifted:
            # Foreign lease drift already fenced: never claim again, never
            # touch the foreign lease, never retry.
            if self._expired(dispatch_id):
                self._release_lease_for(dispatch_id)
            return _result(
                "refused",
                "lease_drift",
                dispatch_id=dispatch_id,
                delivery="unknown",
                resent=False,
            )
        if dispatch_id in self._settled:
            # Any settled outcome (including fenced delivery_unknown)
            # forbids a fresh claim: the old digest was already released
            # and a new claim would abandon a live lease on drift.
            return _result("refused", "already_settled", dispatch_id=dispatch_id)
        if self._expired(dispatch_id):
            self._release_lease_for(dispatch_id)
            return _result(
                "refused",
                "lease_expired",
                dispatch_id=dispatch_id,
                delivery="unknown",
                resent=False,
            )
        fence = self._fenced[dispatch_id]
        target = self._registry.get(fence["binding_id"])
        if target is None:
            return _result("refused", "unknown_binding")
        if dispatch_id not in self._leases:
            return _result("refused", "lease_unavailable", dispatch_id=dispatch_id, resent=False)
        response = self._request(
            "wake-desktop-input-claim",
            fence["relay_identity"],
            {"binding_id": fence["binding_id"], "ttl_s": MAX_LEASE_TTL_S},
        )
        checked = validate_lease_claim(
            response,
            binding_id=fence["binding_id"],
            holder=fence["relay_identity"],
        )
        if checked is None:
            return _result(
                "refused", "lease_unavailable", dispatch_id=dispatch_id, resent=False
            )
        held = self._leases.get(dispatch_id)
        if held is None or checked["lease_digest"] != held.get("lease_digest"):
            # Validation above proves this NEW digest belongs to this exact
            # actor/binding. Retire that own claim by CAS, not a foreign lease.
            self._leases[dispatch_id] = {**held, "lease_digest": checked["lease_digest"]}
            self._drifted.add(dispatch_id)
            self._release_lease_for(dispatch_id)
            return _result(
                "refused",
                "lease_drift",
                dispatch_id=dispatch_id,
                delivery="unknown",
                resent=False,
            )
        return _result(
            "ok",
            "lease_renewed",
            dispatch_id=dispatch_id,
            remaining_s=self.deadline_remaining_s(dispatch_id),
        )

    def run_external_submit(
        self,
        dispatch_id: str,
        submit_fn: Callable[[Mapping[str, Any]], Any],
        restore_fn: Callable[[], Any] | None = None,
    ) -> dict[str, Any]:
        """Deterministic submit -> restore-foreground -> release-own-lease trace.

        The host never runs CU itself: ``submit_fn`` is the single external
        official-CU call receiving the fenced submit dict. ``restore_fn``
        restores the original foreground and always runs before the owned
        lease release, including on submit errors/timeouts. The submit is
        attempted at most once; failure/unknown never resubmits.
        """
        if not isinstance(dispatch_id, str) or dispatch_id not in self._fenced:
            return _result("refused", "not_fenced")
        if not callable(submit_fn):
            return _result("refused", "invalid_submit_callback", dispatch_id=dispatch_id)
        if restore_fn is not None and not callable(restore_fn):
            return _result("refused", "invalid_restore_callback", dispatch_id=dispatch_id)
        if self._expired(dispatch_id):
            self._release_lease_for(dispatch_id)
            return _result(
                "refused",
                "lease_expired",
                dispatch_id=dispatch_id,
                delivery="unknown",
                resent=False,
            )
        if dispatch_id in self._drifted:
            # Foreign drift fenced: never submit against another owner's lease.
            return _result(
                "refused",
                "lease_drift",
                dispatch_id=dispatch_id,
                delivery="unknown",
                resent=False,
            )
        if dispatch_id in self._settled:
            # A direct settle already fenced this dispatch (unknown, failure
            # or recorded): a later callback must never perform a new submit.
            settled = self._settled.get(dispatch_id) or {}
            return _result(
                "refused",
                "duplicate_submit",
                dispatch_id=dispatch_id,
                delivery=settled.get("delivery", "unknown"),
                resent=False,
            )
        if self._submit_calls.get(dispatch_id, 0) >= 1:
            settled = self._settled.get(dispatch_id)
            return _result(
                "refused",
                "duplicate_submit",
                dispatch_id=dispatch_id,
                delivery=(settled or {}).get("delivery", "unknown"),
                resent=False,
            )
        fence = self._fenced[dispatch_id]
        submit = self._submit_for(dispatch_id)
        if submit is None:
            return _result("refused", "not_fenced")
        # Revalidate the owned lease immediately before the one external send.
        # The authoritative server state may have moved since prepare/renew.
        held = self._leases.get(dispatch_id)
        if held is None:
            return _result("refused", "lease_unavailable", dispatch_id=dispatch_id, resent=False)
        fresh = self._request(
            "wake-desktop-input-claim",
            fence["relay_identity"],
            {"binding_id": fence["binding_id"], "ttl_s": MAX_LEASE_TTL_S},
        )
        checked = validate_lease_claim(
            fresh,
            binding_id=fence["binding_id"],
            holder=fence["relay_identity"],
        )
        if checked is None:
            return _result(
                "refused",
                "lease_unavailable",
                dispatch_id=dispatch_id,
                delivery="unknown",
                resent=False,
            )
        if held is None or checked["lease_digest"] != held.get("lease_digest"):
            self._leases[dispatch_id] = {**held, "lease_digest": checked["lease_digest"]}
            self._drifted.add(dispatch_id)
            self._release_lease_for(dispatch_id)
            return _result(
                "refused",
                "lease_drift",
                dispatch_id=dispatch_id,
                delivery="unknown",
                resent=False,
            )
        if not callable(restore_fn):
            return _result("refused", "restore_required", dispatch_id=dispatch_id, resent=False)
        target = self._registry.get(fence["binding_id"])
        stopped = self._stop(target) if target is not None else "unknown_binding"
        if stopped != "clear":
            return _result("refused", stopped, dispatch_id=dispatch_id, resent=False)
        # RPCs can consume the remaining lifetime. Check again at the send seam.
        if self._expired(dispatch_id):
            self._release_lease_for(dispatch_id)
            return _result("refused", "lease_expired", dispatch_id=dispatch_id,
                           delivery="unknown", resent=False)
        phases: list[str] = ["fenced"]
        self._submit_calls[dispatch_id] = 1
        try:
            receipt_payload = submit_fn(submit)
            submit_error: str | None = None
        except Exception as exc:
            receipt_payload = None
            submit_error = type(exc).__name__
        phases.append("submitted")
        if restore_fn is not None:
            try:
                if restore_fn() is False:
                    raise RuntimeError("foreground restoration unconfirmed")
                restore_status = "restored"
                self._restored[dispatch_id] = True
                phases.append("foreground_restored")
            except Exception:
                # Original foreground not confirmed: keep the owned lease
                # until confirmed restoration or natural expiry. No settle,
                # no release, no retry from this callback.
                restore_status = "restore_failed"
                self._restored[dispatch_id] = False
                phases.append("restore_failed")
                return _result(
                    "error",
                    "restore_failed",
                    dispatch_id=dispatch_id,
                    binding_id=fence["binding_id"],
                    phases=phases,
                    restore=restore_status,
                    submit_error=submit_error,
                    settle_code="unsettled",
                    delivery="unknown",
                    resent=False,
                )
        else:
            # No restore callback: settle truthfully. The foreground claim is
            # recorded as unconfirmed, never as restored.
            restore_status = "no_restore_callback"
            self._restored[dispatch_id] = False
            phases.append("foreground_restore_skipped")
        had_lease = dispatch_id in self._leases
        settled = self.settle(dispatch_id, receipt_payload)
        released = had_lease and dispatch_id not in self._leases and dispatch_id not in self._drifted
        phases.append("lease_released" if released else "lease_retained")
        return _result(
            "ok" if submit_error is None else "error",
            "submit_trace" if submit_error is None else "submit_failed",
            dispatch_id=dispatch_id,
            binding_id=fence["binding_id"],
            phases=phases,
            restore=restore_status,
            submit_error=submit_error,
            settle_code=settled.get("code"),
            delivery=settled.get("delivery", "unknown"),
            resent=False,
        )

    def _submit_for(self, dispatch_id: str) -> dict[str, Any] | None:
        fence = self._fenced.get(dispatch_id)
        if fence is None:
            return None
        return {
            "tool": OFFICIAL_CU_TOOL,
            "once": True,
            "binding_id": fence["binding_id"],
            "dispatch_id": dispatch_id,
            "relay_identity": fence["relay_identity"],
            "arguments": {
                "session_id": fence["native_session_id"],
                "project_slug": fence["project_slug"],
                "workspace": fence["workspace"],
                "text": fence["text"],
            },
        }

    def _request(self, op: str, relay_identity: str, args: Mapping[str, Any]) -> Any:
        if op not in HOST_OPS or (op in RELAY_OPS and args.get("driver") != DRIVER):
            _fail("invalid_office_op", "host op is outside the Office protocol seam")
        if op == "wake-desktop-input-claim":
            if set(args) != {"binding_id", "ttl_s"}:
                _fail("invalid_office_op", "desktop-input claim takes only binding_id and ttl_s")
            ttl = args.get("ttl_s")
            if isinstance(ttl, bool) or not isinstance(ttl, int) or not (1 <= ttl <= MAX_LEASE_TTL_S):
                _fail("invalid_office_op", "desktop-input claim ttl_s must be <= 180")
        if op == "wake-desktop-input-release":
            if set(args) != {"binding_id", "expected_lease_digest"}:
                _fail("invalid_office_op", "desktop-input release takes binding_id and the lease digest")
            if not is_opaque_lease_digest(args.get("expected_lease_digest")):
                _fail("invalid_office_op", "desktop-input release requires an opaque lease digest")
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
