"""Deterministic watchdog: compare ledger intent vs native provider status.

Dispatch requires ALL of: live owner grant, a capable adapter (idempotent
lookup AND fencing), fresh provider status (<=10s), an eligible terminal
state, transient cause with cause marked resolved, attempts and backoff
budget, an acquired lease, and a post-lease cancellation recheck.
Anything else yields an observe-only decision and no dispatch.
"""

from __future__ import annotations

import hashlib
import json
from typing import Optional

from openclaw_ops import models
from openclaw_ops.adapters import (
    Adapter,
    StaleFenceError,
    UnsupportedResumeError,
)
from openclaw_ops.clock import Clock
from openclaw_ops.ledger import Ledger

WATCHDOG_OWNER = "watchdog"


def checkpoint_digest(manifest: dict) -> str:
    """SHA256 over a versioned manifest of approved work (never secrets)."""
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _dec(task: dict, decision: str, reason: str, **extra) -> dict:
    return {
        "task_id": task["task_id"],
        "generation": task["generation"],
        "decision": decision,
        "reason": reason,
        **extra,
    }


def _capable(adapter: Optional[Adapter]) -> bool:
    return bool(
        adapter
        and adapter.supports_idempotent_lookup
        and adapter.supports_fencing
    )


def tick_one(ledger: Ledger, registry: dict, clock: Clock, task: dict,
             now: float, owner: str = WATCHDOG_OWNER) -> dict:
    # Tasks without an explicit owner grant are never auto-recovered:
    # no replay of arbitrary old conversations.
    if not task["owner_grant_ref"]:
        return _dec(task, "needs-owner", "no-owner-grant")
    if now > float(task["grant_expires"]):
        # Grant expiry cannot be extended by model text or messages.
        return _dec(task, "grant-expired", "owner-grant-expired")

    adapter = registry.get(task["provider"])
    if adapter is None:
        return _dec(task, "unconnected", f"no-adapter:{task['provider']}")
    if not _capable(adapter):
        return _dec(task, "observe-only",
                    f"adapter '{adapter.name}' lacks lookup+fencing")

    try:
        status = adapter.get_status(task)
    except Exception as exc:  # failed telemetry -> unknown
        return _dec(task, "unknown", f"telemetry-error:{type(exc).__name__}")
    if status is None:
        return _dec(task, "unknown", "provider-unreachable")
    if now - float(status.observed_wall) > models.STATUS_FRESHNESS:
        return _dec(task, "unknown", "stale-status")
    if abs(float(status.provider_wall or now) - now) > models.CLOCK_SKEW_LIMIT:
        return _dec(task, "deferred",
                    "clock-skew: waiting for fresh bounded observation")

    state = status.state
    if state == models.STATE_TERMINAL_COMPLETED:
        ledger.mark_state(task["task_id"], models.TASK_COMPLETED, now)
        return _dec(task, "completed", "provider-terminal-completed")
    if state == models.STATE_CANCELLED:
        ledger.mark_state(task["task_id"], models.TASK_CANCELLED, now)
        return _dec(task, "cancelled", "provider-cancelled")
    if state in (models.STATE_WAITING_USER, models.STATE_APPROVAL_WAIT):
        ledger.mark_state(task["task_id"], models.TASK_NEEDS_OWNER, now)
        return _dec(task, "needs-owner", f"provider-state:{state}")
    if state in (models.STATE_RUNNING, models.STATE_BUSY):
        # A live report or an active writer is not proof of progress, but a
        # missing heartbeat alone never proves failure either: observe only.
        _, _, stable = ledger.observe_checkpoint(
            task["task_id"], status.checkpoint or state, now)
        if stable and not status.writer_active:
            ledger.mark_state(task["task_id"], models.TASK_SUSPECTED, now)
            return _dec(task, "suspected-stall",
                        "checkpoint-quiet>=30s: monitoring, no dispatch")
        return _dec(task, "monitor",
                    "provider-live: false-stall guard, no dispatch")
    if state != models.STATE_TERMINAL_FAILED:
        return _dec(task, "unknown", f"unexpected-provider-state:{state}")

    # Provider reports terminal failure: retry transient causes only.
    if not status.cause_transient:
        ledger.mark_state(task["task_id"], models.TASK_NEEDS_OWNER, now)
        return _dec(task, "needs-owner", "non-transient-cause")
    if not task["cause_resolved"]:
        return _dec(task, "needs-owner", "cause-not-marked-resolved")
    if int(task["attempts"]) >= int(task["max_attempts"]):
        return _dec(task, "exhausted", "max-attempts-reached")
    if now < float(task["next_retry_at"]):
        return _dec(task, "backoff-wait", "retry-not-due")

    attempt = int(task["attempts"]) + 1
    token = ledger.acquire_lease(
        task["task_id"], int(task["generation"]), owner, now)
    if token is None:
        # Another watchdog holds the lease: exactly one owner recovers.
        return _dec(task, "lease-held", "concurrent-watchdog holds lease")

    try:
        # Post-lease recheck of the native cancellation/revision marker.
        try:
            recheck = adapter.get_status(task)
        except Exception:
            recheck = None
        if recheck is not None and recheck.revision != status.revision:
            return _dec(task, "aborted",
                        "revision-changed-after-lease")
        if recheck is not None and recheck.state in (
            models.STATE_CANCELLED, models.STATE_WAITING_USER,
            models.STATE_APPROVAL_WAIT, models.STATE_TERMINAL_COMPLETED,
        ):
            ledger.mark_state(task["task_id"], models.TASK_NEEDS_OWNER
                              if recheck.state != models.STATE_CANCELLED
                              else models.TASK_CANCELLED, now)
            return _dec(task, "aborted",
                        f"state-after-lease:{recheck.state}")

        key = Ledger.intent_key(task["task_id"], int(task["generation"]),
                                attempt)
        intent, created = ledger.record_intent(
            key, task["task_id"], int(task["generation"]), attempt, now)
        if not created and intent.get("native_run_id"):
            # Crash happened after intent but before ACK: reconcile via
            # idempotent lookup, never blindly resend.
            found = adapter.lookup(key)
            if found:
                ledger.set_intent(key, found, "acked")
                return _dec(task, "adopted-no-resend",
                            "crash-after-intent: lookup found run",
                            native_run_id=found)
            ledger.set_intent(key, intent.get("native_run_id", ""),
                              "unknown")
            return _dec(task, "unknown-dispatch",
                        "prior intent without findable run: owner must"
                        " reconcile (UNKNOWN_DISPATCH)")

        try:
            result = adapter.resume(task, key, token)
        except (UnsupportedResumeError, StaleFenceError) as exc:
            ledger.set_intent(key, "", "unknown")
            return _dec(task, "unknown-dispatch", f"{type(exc).__name__}")
        if result.ack == "unknown":
            # Uncertain ACK: mark unknown, reconcile later, no auto resend.
            ledger.set_intent(key, result.native_run_id, "unknown")
            return _dec(task, "dispatched-uncertain",
                        "ack-unknown: reconcile before any resend",
                        native_run_id=result.native_run_id)
        ledger.set_intent(key, result.native_run_id, "acked")
        backoff = models.BACKOFFS[min(attempt - 1, len(models.BACKOFFS) - 1)]
        ledger.note_attempt(task["task_id"], attempt, now + backoff, now)
        return _dec(task, "recovered", "resume-acked",
                    native_run_id=result.native_run_id,
                    next_retry_in=backoff)
    finally:
        ledger.release_lease(task["task_id"], owner, now)


def tick(ledger: Ledger, registry: dict, clock: Clock,
         now: Optional[float] = None,
         owner: str = WATCHDOG_OWNER) -> list[dict]:
    """Run one bounded watchdog pass; renew owned live leases first."""
    now = clock.wall() if now is None else now
    for task in ledger.list_tasks():
        if (task["lease_owner"] == owner
                and float(task["lease_expires"]) > now):
            ledger.renew_lease(task["task_id"], int(task["generation"]),
                               owner, int(task["lease_token"]), now)
    out = []
    for task in ledger.list_tasks():
        if task["state"] in (models.TASK_COMPLETED, models.TASK_CANCELLED):
            continue
        out.append(tick_one(ledger, registry, clock, task, now, owner))
    return out
