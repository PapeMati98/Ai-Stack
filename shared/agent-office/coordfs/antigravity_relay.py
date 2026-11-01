"""Native Antigravity seam backed by the existing Office dispatch ledger.

No queue, scheduler, enrollment, retry or production configuration here.
Zero exit is not a native receipt. A durable unknown fence precedes submission.
"""
from __future__ import annotations

import hashlib
import json
import math
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .antigravity_transport import agentapi_command, attempt, validate_executable
from .atomic import exclusive_mutex
from .constants import MUTEX_WAIT_S
from .errors import CoordError
from .leases import parse_ts
from .wake import ANTIGRAVITY_RELAY_DRIVER, Wake

MAX_OBSERVATION_BYTES = 4096
OBSERVATION_MAX_AGE_S = 900
FUTURE_SKEW_S = 5


def conversation_digest(native_id: str) -> str:
    return hashlib.sha256(native_id.encode("utf-8")).hexdigest()


def invalidate_hook_state(wake: Wake, path: Path, *, native_digest: str) -> None:
    """A failed producer must not leave a previous fresh idle observation usable."""
    import re
    if not isinstance(native_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", native_digest):
        raise CoordError("invalid native digest", code="invalid_native_observation")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"schema_version": 1, "conversation_sha256": native_digest,
              "state": "unknown", "source_event": "ProducerError", "observed_at": wake.now()}
    with exclusive_mutex(path.with_name(path.name + ".lock"), timeout_s=MUTEX_WAIT_S):
        wake._atomic_replace_json(path, record)


class NativeObservation:
    """Read the trusted, workspace-scoped native hook's metadata only.

    The producer must be installed/verified separately. This class does not
    make an arbitrary model-written state file a trustworthy native proof.
    Stale/missing input stays unknown; it never defaults to idle.
    """
    def __init__(self, path: Path, native_id: str, *, now: Callable[[], float]):
        self.path = Path(path)
        self.expected = conversation_digest(native_id)
        self.now = now

    def state(self) -> str:
        try:
            with self.path.open("rb") as handle:
                raw = handle.read(MAX_OBSERVATION_BYTES + 1)
            if len(raw) > MAX_OBSERVATION_BYTES:
                return "unknown"
            value = json.loads(raw)
            if not isinstance(value, dict) or set(value) != {
                "schema_version", "conversation_sha256", "state", "source_event", "observed_at"
            } or value["schema_version"] != 1:
                return "unknown"
            if value["conversation_sha256"] != self.expected:
                return "unknown"
            observed = value["observed_at"]
            if isinstance(observed, bool) or not isinstance(observed, (int, float)) or not math.isfinite(observed):
                return "unknown"
            age = self.now() - observed
            if age < -FUTURE_SKEW_S or age >= OBSERVATION_MAX_AGE_S:
                return "unknown"
            if value["source_event"] == "Stop" and value["state"] in {"idle", "busy"}:
                return value["state"]
            if value["source_event"] == "PreInvocation" and value["state"] == "busy":
                return "busy"
        except (OSError, ValueError, TypeError, KeyError):
            pass
        return "unknown"


def record_hook_state(wake: Wake, path: Path, *, event: str, payload: dict) -> None:
    """Called only by a configured native hook; discard prompt/transcript fields."""
    from .antigravity_transport import _canonical_conversation_id

    native_id = _canonical_conversation_id(payload.get("conversationId"))
    if payload["conversationId"] != native_id:
        raise CoordError("native ID must have canonical spelling", code="invalid_native_observation")
    if event == "PreInvocation":
        number = payload.get("invocationNum")
        if isinstance(number, bool) or not isinstance(number, int) or number < 0:
            raise CoordError("invalid native invocation number", code="invalid_native_observation")
        state = "busy"
    elif event == "Stop":
        idle = payload.get("fullyIdle")
        if type(idle) is not bool:
            raise CoordError("native Stop requires fullyIdle", code="invalid_native_observation")
        state = "idle" if idle else "busy"
    else:
        raise CoordError("unsupported native observation event", code="invalid_native_observation")
    # A data-only observation cache, never a second queue or dedupe store.
    record = {"schema_version": 1, "conversation_sha256": conversation_digest(native_id),
              "state": state, "source_event": event, "observed_at": wake.now()}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_mutex(path.with_name(path.name + ".lock"), timeout_s=MUTEX_WAIT_S):
        wake._atomic_replace_json(path, record)


def native_runner(argv: list[str], timeout_s: int):
    """Fixed argv, no console, stdin or provider output collection."""
    return subprocess.run(agentapi_command(argv), timeout=timeout_s, stdin=subprocess.DEVNULL,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          shell=False, check=False,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _result(status: str, reason: str, *, calls: int = 0, digest: str | None = None) -> dict:
    result = {"status": status, "reason": reason, "calls": calls}
    if digest is not None:
        result["prompt_sha256"] = digest
    return result


def submit_dispatch(wake: Wake, dispatch_id: str, *, relay_agent: str,
                    executable: str, local_host: str, channel: str,
                    observation: NativeObservation, runner=native_runner) -> dict:
    """One attempt, using Office's actual sender/claim/binding/STOP state.

    Caller supplies operator-bound host/channel/executable and a separately
    verified native observation path. None is taken from an Office body.
    """
    validate_executable(executable)
    dispatch = wake._load_dispatch(dispatch_id)
    binding = wake._load_binding(str(dispatch["binding_id"]))
    if binding["driver"] != ANTIGRAVITY_RELAY_DRIVER:
        return _result("REFUSED", "wrong_driver")
    if observation.expected != conversation_digest(binding["native_thread_id"]):
        return _result("REFUSED", "wrong_observer")
    if dispatch["status"] != "pending":
        return _result("REFUSED", "dispatch_fenced")
    reason = wake._relay_eligibility_reason(dispatch)
    if reason is not None:
        return _result("REFUSED", reason)
    state = observation.state()
    if state == "busy":
        wake.relay_defer_busy(dispatch_id, driver=ANTIGRAVITY_RELAY_DRIVER)
        return _result("DEFERRED", "target_busy")
    if state != "idle":
        return _result("REFUSED", "target_unknown")
    rows = [row for row in wake._ledger_entries()
            if row.get("dispatch_id") == dispatch_id and row.get("trigger") == "message"
            and row.get("decision") == "dispatched"]
    if len(rows) != 1 or dispatch["trigger"] != "message":
        return _result("REFUSED", "missing_authoritative_message_origin")
    sender = rows[0].get("trigger_from")
    if sender not in binding["allowed_wakers"]:
        return _result("REFUSED", "wrong_sender")
    issued = parse_ts(dispatch["created_at"])
    expires = min(parse_ts(binding["expires_at"]),
                  parse_ts(dispatch["deadline_at"]) if "deadline_at" in dispatch else issued + 10800)
    iso = lambda stamp: datetime.fromtimestamp(stamp, timezone.utc).isoformat()
    request = {"conversation_id": binding["native_thread_id"], "sender": sender,
               "channel": channel, "prompt": dispatch["text"], "dedupe_key": dispatch_id,
               "issued_at": iso(issued), "expires_at": iso(expires), "timeout_s": 30}
    transport_binding = {"conversation_id": binding["native_thread_id"], "host": local_host,
                         "allowed_senders": list(binding["allowed_wakers"]), "allowed_channels": [channel]}
    # Validate before claiming; malformed input must not leave an owned claim.
    from .antigravity_transport import validate_binding, validate_request
    validate_binding(transport_binding)
    validate_request(request)
    wake.relay_claim(dispatch_id, relay_agent=relay_agent, driver=ANTIGRAVITY_RELAY_DRIVER)

    def stopped():
        return wake._stop_is_present()

    def revoked():
        current = wake._load_binding(str(dispatch["binding_id"]))
        return (current["status"] != "active" or not current["verified"]
                or current["version"] != binding["version"]
                or current["native_thread_id"] != binding["native_thread_id"]
                or current["driver"] != ANTIGRAVITY_RELAY_DRIVER
                or wake.now() >= parse_ts(current["expires_at"]))

    crossed = False

    def fenced_runner(argv, timeout_s):
        nonlocal crossed
        # Last guard is at the actual seam, after durable claim. No UI interrupts.
        if observation.state() != "idle":
            raise CoordError("native target no longer idle", code="target_not_idle")
        wake.relay_native_begin(dispatch_id, relay_agent=relay_agent, driver=ANTIGRAVITY_RELAY_DRIVER)
        crossed = True
        return runner(argv, timeout_s)

    receipt = attempt(request, transport_binding, executable=executable, local_host=local_host,
                      observe_stop=stopped, observe_target=observation.state, observe_revoked=revoked,
                      known_dedupe_keys=(), runner=fenced_runner, now=wake.now())
    if not crossed:
        wake.relay_native_unsubmitted(dispatch_id, relay_agent=relay_agent, driver=ANTIGRAVITY_RELAY_DRIVER)
        if receipt.status == "DEFERRED" or observation.state() == "busy":
            wake.relay_defer_busy(dispatch_id, driver=ANTIGRAVITY_RELAY_DRIVER)
    # Never expose provider output, prompt, raw conversation ID or exception text.
    return _result(receipt.status if crossed else "DEFERRED" if receipt.status == "DEFERRED" else "REFUSED",
                   receipt.reason, calls=1 if crossed else 0, digest=receipt.prompt_digest)
