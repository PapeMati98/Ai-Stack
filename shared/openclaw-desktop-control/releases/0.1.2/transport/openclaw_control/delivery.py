"""Dispatch lifecycle over ControlStore with capability-gated adapters.

ACK, turn start, and result are three distinct facts. An uncertain ACK
becomes delivery_unknown (never an automatic resend); the owner
reconciles it explicitly. No model call, no text rewrite, no truncation
anywhere on this path.
"""

from __future__ import annotations

from typing import Any, Optional

from openclaw_control import models
from openclaw_control.adapters import (
    AdapterBusyError,
    AdapterRefusalError,
    AdapterUnavailableError,
    ControlAdapter,
    deliver_checked,
    require_capable,
)
from openclaw_control.store import (
    ClaimMismatchError,
    ControlStore,
    StoppedError,
)


class DeliveryError(Exception):
    """Base error for the dispatch lifecycle."""


class UnboundTargetError(DeliveryError):
    """No explicit target bound outside the prompt; dispatch refused."""


class StaleStatusError(Exception):
    """Native status read is older than the freshness bound."""


def enqueue_prompt(
    store: ControlStore,
    task_id: str,
    generation: int,
    provider: str,
    native_thread: str,
    raw_text: str,
    entities: Any,
    now: float,
    target_binding: str = "",
    correction_of: str = "",
) -> dict:
    """Enqueue one immutable prompt envelope (exact Unicode preserved)."""
    if not isinstance(raw_text, str):
        raise DeliveryError("raw_text must be exact str")
    return store.enqueue_dispatch(
        task_id=task_id,
        generation=int(generation),
        provider=provider,
        native_thread=native_thread,
        raw_text=raw_text,
        entities=entities,
        now=now,
        target_binding=target_binding,
        correction_of=correction_of,
    )


def claim(store: ControlStore, worker_id: str, now: float) -> Optional[dict]:
    return store.claim_one(worker_id, now)


def deliver(
    store: ControlStore,
    adapter: ControlAdapter,
    dispatch_id: str,
    now: float,
    claim_token: Optional[str] = None,
) -> dict:
    """Deliver one claimed dispatch. Returns the updated dispatch row."""
    dispatch = store.require_dispatch(dispatch_id)
    if not dispatch["task_id"] or not dispatch["target_binding"]:
        raise UnboundTargetError(
            "dispatch %r has no explicit target; refused"
            % (dispatch_id,)
        )
    if dispatch["state"] != models.DISPATCHING:
        raise ClaimMismatchError(
            "deliver needs dispatching, has %r" % (dispatch["state"],)
        )
    # Recheck STOP / claim token immediately before any adapter side effect.
    if store.is_stopped(dispatch.get("task_id") or ""):
        store.unclaim(dispatch_id, now)
        raise StoppedError("STOP active; send refused")
    if claim_token is not None and claim_token != dispatch.get("claim_token"):
        raise ClaimMismatchError("claim token mismatch")
    try:
        outcome = deliver_checked(adapter, dispatch)
    except AdapterRefusalError:
        return store.mark_blocked(dispatch_id, now, "adapter-refusal")
    except (AdapterBusyError, AdapterUnavailableError):
        raise
    except Exception:
        return store.mark_unknown(dispatch_id, now)
    return store.record_ack(dispatch_id, outcome.ack, now, claim_token)


def start_turn(
    store: ControlStore,
    adapter: ControlAdapter,
    dispatch_id: str,
    fencing_token: int,
    now: float,
) -> dict:
    """Confirm the native turn started (fencing enforced by the adapter)."""
    require_capable(adapter)
    dispatch = store.require_dispatch(dispatch_id)
    receipt = adapter.start_turn(dispatch, int(fencing_token))
    row = store.record_start(dispatch_id, now)
    row["turn_receipt"] = receipt
    return row


def finish(
    store: ControlStore,
    dispatch_id: str,
    result: str,
    now: float,
    detail: str = "",
) -> dict:
    return store.record_result(dispatch_id, result, now, detail)


def recover(store: ControlStore, now: float) -> list[dict]:
    """Restart/crash recovery: orphaned claims become delivery_unknown."""
    return store.recover_crashed(now)


def pop_alarm(store: ControlStore, now: float) -> Optional[dict]:
    """At most one uncertain dispatch due for its single 120s alarm."""
    return store.pop_unknown_alarm(now)


def reconcile(
    store: ControlStore, dispatch_id: str, delivered: bool, now: float
) -> dict:
    return store.reconcile(dispatch_id, delivered, now)


def cancel(
    store: ControlStore,
    adapter: ControlAdapter,
    dispatch_id: str,
    now: float,
) -> dict:
    """Owner cancel: pending first, cancelled only on adapter confirmation.

    No confirmation -> stays cancel_pending (never reported as stopped).
    """
    require_capable(adapter)
    row = store.request_cancel(dispatch_id, now)
    try:
        confirmed = bool(adapter.stop_turn(row))
    except Exception:
        confirmed = False
    return store.confirm_cancel(dispatch_id, confirmed, now)


def takeover(
    store: ControlStore,
    task_id: str,
    host_session: str,
    now: float,
    note: str = "",
) -> None:
    """Explicit ownership move note. Fresh native re-read is mandatory next."""
    store.record_takeover(task_id, host_session, now, note)


def require_fresh_status(observed_wall: float, now: float) -> None:
    """Reject a native status read that is stale OR in the future."""
    observed = float(observed_wall)
    now = float(now)
    if (observed - now) > models.STATUS_FUTURE_SKEW:
        raise StaleStatusError(
            "native status is in the future; re-read before acting"
        )
    if (now - observed) > models.STATUS_FRESHNESS:
        raise StaleStatusError(
            "native status is stale; re-read before acting"
        )


def redacted_summary(dispatch: dict) -> dict:
    """Diagnostic view: references and hashes ONLY, never raw text."""
    return {
        "dispatch_id": dispatch.get("dispatch_id"),
        "task_id": dispatch.get("task_id"),
        "generation": dispatch.get("generation"),
        "target_binding": dispatch.get("target_binding"),
        "provider": dispatch.get("provider"),
        "text_sha256": dispatch.get("text_sha256"),
        "state": dispatch.get("state"),
        "ack": dispatch.get("ack"),
        "correction_of": dispatch.get("correction_of"),
        "alarm_sent": dispatch.get("alarm_sent"),
        "updated_at": dispatch.get("updated_at"),
    }
