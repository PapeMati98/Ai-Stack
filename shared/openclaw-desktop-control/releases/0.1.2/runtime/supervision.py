"""T5 supervision gates. No unsafe auto-resume. Idle is not goal completion."""

from __future__ import annotations

from typing import Any


def consider_resume(
    store: Any,
    adapter: Any,
    target_id: str,
    thread: str,
    now: float,
    *,
    auto_resume: bool,
    grant_ref: str,
    grant_expires: float,
    max_no_progress: int = 2,
    unfinished_goal: bool = False,
    last_turn_continuity: bool = False,
    blocker: bool = False,
    approval_wait: bool = False,
    quota: bool = False,
) -> dict:
    """Return an observe-only or resume decision with a precise reason."""
    if store.is_stopped():
        return _obs(target_id, "stopped")
    if not auto_resume:
        return _obs(target_id, "auto-resume-not-granted")
    if not grant_ref:
        return _obs(target_id, "no-resume-grant")
    if grant_expires and now > float(grant_expires):
        return _obs(target_id, "resume-grant-expired")
    takeover = store.get_takeover(target_id)
    if takeover:
        return _obs(target_id, "owner-takeover")
    try:
        status = adapter.status_of(thread)
    except Exception as exc:
        return _obs(target_id, "status-unavailable:%s" % type(exc).__name__)
    state = status.get("state")
    if state != "idle":
        return _obs(target_id, "not-idle:%s" % state)
    if blocker:
        return _obs(target_id, "blocker")
    if approval_wait:
        return _obs(target_id, "approval-wait")
    if quota:
        return _obs(target_id, "quota")
    if not unfinished_goal:
        return _obs(target_id, "no-unfinished-goal-evidence")
    if not last_turn_continuity:
        return _obs(target_id, "no-last-turn-continuity")
    # Idle is execution-idle, not semantic goal completion.
    row = store.get_supervision(target_id) or {}
    attempts = int(row.get("no_progress_attempts") or 0)
    if attempts >= int(max_no_progress):
        store.put_supervision(
            target_id,
            now,
            grant_ref=grant_ref,
            grant_expires=grant_expires,
            no_progress_attempts=attempts,
            last_turn_id=str(status.get("last_turn_id") or ""),
            last_reason="diagnosis-no-progress",
        )
        return {
            "target_id": target_id,
            "decision": "diagnosis",
            "reason": "two-no-progress-attempts",
            "auto_resume": False,
        }
    return {
        "target_id": target_id,
        "decision": "resume-eligible",
        "reason": "grant-and-evidence",
        "last_turn_id": status.get("last_turn_id"),
        "supervisor_prefix_required": True,
    }


def note_no_progress(store: Any, target_id: str, now: float) -> dict:
    row = store.get_supervision(target_id) or {
        "grant_ref": "",
        "grant_expires": 0,
        "no_progress_attempts": 0,
        "last_turn_id": "",
    }
    attempts = int(row.get("no_progress_attempts") or 0) + 1
    return store.put_supervision(
        target_id,
        now,
        grant_ref=row.get("grant_ref") or "",
        grant_expires=float(row.get("grant_expires") or 0),
        no_progress_attempts=attempts,
        last_turn_id=row.get("last_turn_id") or "",
        last_reason="no-progress",
    )


def _obs(target_id: str, reason: str) -> dict:
    return {
        "target_id": target_id,
        "decision": "observe-only",
        "reason": reason,
        "auto_resume": False,
    }
