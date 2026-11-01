"""Shared READ-ONLY quota semantics for Fleet views and external consumers.

This module is the single source of truth for annotating quota views with
freshness and availability, split into two independent signals:

- numeric quota freshness (status fresh/stale/unknown, windows, resets,
  source, observed_at) - what the provider counters said and when;
- availability (available/exhausted/recheck-required/unknown) - whether the
  pool may receive work, from the bounded quota_availability cache and true
  authoritative exhaustion, never from a local token ledger.

Public API:

- annotate_view(agent_id, view, profile, now=None, function="coding",
  model=None) -> dict
      One annotated view. `view` is a row shaped like adaptive.latest_quotas
      output; an optional view["availability_state"] dict carries the cached
      quota_availability record for this pool (input convention; the key is
      consumed and never emitted). Advisory/legacy local-token-ledger rows are
      normalized through quota_collectors.normalize_inferred_bailian_ledger:
      they stay visible as estimates and never produce a numeric hardblock.
      A true independent native refusal (authoritative headroom <= 0, a cached
      native-provider-error) remains blocking.

- read_snapshot(root, profiles, ids=None, now=None) -> {agent: annotated view}
      READ-ONLY snapshot: adaptive.latest_quotas plus the bounded
      quota_availability cache read through cache_path/read_cache. It never
      runs a native check, never refreshes, never reads credentials, never
      writes, and never imports fleet.py.

Invariants:
- pool-key binding: cache and views resolve through the profile quota_pool_id;
- error sanitization: a missing/corrupt cache or store yields "unknown" with
  a generic reason, never a path or exception payload;
- last good observed_at is independent of the last failed attempt; a recheck
  timestamp never launderers into an observation timestamp;
- a fresh verified native-run proof (< 24 h) may report available while the
  numeric quota stays unknown; a newer refusal always wins over an older
  success; an elapsed reset schedules a fresh recheck, never availability;
- unknown usage is not zero and failure is not quota exhaustion; no allowance
  is ever invented.
"""
from __future__ import annotations

import datetime as dt
import inspect
import math
import time
from pathlib import Path
from typing import Any, Iterable

import adaptive
import quota_availability
import quota_collectors

AVAILABILITY_STATUSES = ("available", "exhausted", "recheck-required", "unknown")

# Window fields of a quota view: (fraction key, reset key, scope).
_VIEW_WINDOWS = (
    ("short_window_remaining_fraction", "short_window_reset_at", "short-window"),
    ("weekly_remaining_fraction", "weekly_reset_at", "weekly"),
    ("monthly_remaining_fraction", "monthly_reset_at", "monthly"),
)


def _now_seconds(now: Any) -> float:
    if now is None:
        return time.time()
    if isinstance(now, dt.datetime):
        return now.timestamp()
    if isinstance(now, bool) or not isinstance(now, (int, float)):
        raise ValueError("now must be a datetime, epoch seconds or None")
    return float(now)


def _now_datetime(now: Any) -> dt.datetime:
    if now is None:
        return dt.datetime.now(dt.timezone.utc)
    if isinstance(now, dt.datetime):
        return now
    if isinstance(now, bool) or not isinstance(now, (int, float)):
        raise ValueError("now must be a datetime, epoch seconds or None")
    return dt.datetime.fromtimestamp(float(now), dt.timezone.utc)


def quota_view_blocker_text(
    agent_id: str,
    view: dict[str, Any],
    profile: dict[str, Any] | None,
) -> str | None:
    """Exact blocker text for quota states that must never look current.

    Single source of the text fleet.quota_view_blocker historically produced;
    the panel keeps an equivalent replica verified by an equivalence test.
    """
    status = str(view.get("status", "unknown"))
    config = profile.get("quota_collection") if isinstance(profile, dict) else None
    collector_mode = (
        str(config.get("collector_mode", "")).strip()
        if isinstance(config, dict)
        else ""
    )
    source_blocker = quota_collectors.collector_mode_blocker(collector_mode)
    if status == "unknown":
        detail = (
            f"{source_blocker}; " if source_blocker else ""
        )
        conflict = "konflikt rekordow quota; " if view.get("quota_conflict") is True else ""
        return (
            f"unknown: {conflict}no usable quota observation for {agent_id}; {detail}"
            "headroom is not known and must not be treated as current"
        )
    if status == "stale":
        observed_at = view.get("observed_at")
        age_hours = view.get("age_hours")
        return (
            f"stale: latest {view.get('source', 'unknown-source')} snapshot "
            f"observed_at={observed_at} age_hours={age_hours} exceeded its "
            "validity; refresh quota evidence before trusting headroom"
        )
    if source_blocker is not None:
        return (
            f"{source_blocker}; recorded {view.get('source', 'unknown')} "
            "snapshot is advisory only"
        )
    return None


def _pool_matches(evidence: dict[str, Any], pool: str | None) -> bool:
    return all(evidence.get(key) in (None, pool) for key in
               ("quota_pool_id", "pool_id", "account_or_pool")) if pool else True


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _window_relevant(window: dict[str, Any], function: str, model: str | None,
                     pool: str | None = None) -> bool:
    """A window blocks only work it actually governs.

    A window tagged for another function (e.g. code review) or another model
    must not block the requested function/model. An untagged provider window
    governs the whole pool. A model-tagged window is not evidence against an
    unspecified model either - relevance is never guessed.
    """
    if not _pool_matches(window, pool):
        return False
    w_function = window.get("function")
    if w_function is None and window.get("scope") in {"coding", "code_review"}:
        w_function = window["scope"]
    if w_function is not None and w_function != function:
        return False
    w_model = window.get("model")
    if w_model is not None and w_model != model:
        return False
    if model is None and w_model is not None:
        return False
    return True


def _windows(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    explicit = evidence.get("windows")
    if isinstance(explicit, dict):
        return [w for w in explicit.values() if isinstance(w, dict)]
    if isinstance(explicit, list):
        return [w for w in explicit if isinstance(w, dict)]
    return [{"scope": scope, "remaining_fraction": evidence.get(fraction),
             "reset_at": evidence.get(reset), "function": evidence.get("function"),
             "model": evidence.get("model")}
            for fraction, reset, scope in _VIEW_WINDOWS if fraction in evidence]


def _remaining(window: dict[str, Any]) -> float | None:
    for key in ("remaining_fraction", "remaining"):
        if key in window:
            value = _number(window[key])
            return value if value is not None and value <= 1 else None
    used, limit = _number(window.get("used")), _number(window.get("limit"))
    if used is not None and used >= 0 and limit is not None and limit > 0:
        return 1 - used / limit
    used_percent = _number(window.get("used_percent"))
    if used_percent is not None and used_percent >= 0:
        return 1 - used_percent / 100
    return None


def _fresh(evidence: dict[str, Any], now_s: float) -> bool:
    observed = quota_availability.seconds(evidence.get("observed_at"))
    if not 0 < observed <= now_s:
        return False
    for key in ("effective_expires_at", "expires_at"):
        if key in evidence and quota_availability.seconds(evidence[key]) <= now_s:
            return False
    return True


def _window_current(window: dict[str, Any], now_s: float) -> bool:
    if window.get("status") in {"stale", "unknown"}:
        return False
    if "observed_at" in window and not _fresh(window, now_s):
        return False
    return all(quota_availability.seconds(window[key]) > now_s
               for key in ("expires_at", "effective_expires_at") if key in window)


def _exhausted_relevant_windows(
    view: dict[str, Any],
    state: dict[str, Any],
    function: str,
    model: str | None,
    now_s: float | None = None,
    pool: str | None = None,
) -> list[dict[str, Any]]:
    """Exhausted (remaining <= 0) function/model-relevant windows, both sources."""
    candidates = []
    for evidence in (view, state):
        if quota_availability.local_ledger_evidence(evidence):
            continue
        for window in _windows(evidence):
            remaining = _remaining(window)
            if (remaining is not None and remaining <= 0
                    and _window_relevant(window, function, model, pool)
                    and (now_s is None or _window_current(window, now_s))):
                candidates.append(window)
    if (state.get("source") == "native-provider-error"
            and state.get("status") in {"exhausted", "recheck-required"}
            and _window_relevant(state, function, model, pool)):
        # The actual refusal can govern an additional monthly/credit limit,
        # even when a later numeric snapshot exposes only a short window.
        candidates.append({**state, "remaining_fraction": 0})
    return candidates


def _blocking_reset(
    view: dict[str, Any],
    state: dict[str, Any],
    function: str,
    model: str | None,
    now_s: float,
    pool: str | None = None,
) -> tuple[str | None, bool]:
    """(reset_at, recheck_required) for simultaneously blocking windows.

    The reset promised to callers is the MAXIMUM reset among windows that are
    exhausted right now and govern this function/model: a short window freeing
    in 20 minutes must not hide a weekly window blocking for 3 days. A reset
    that already elapsed (or is unknown) authorizes nothing - it only means a
    fresh recheck is required.
    """
    exhausted = _exhausted_relevant_windows(view, state, function, model, now_s, pool)
    future: list[float] = []
    stale_only = False
    for window in exhausted:
        reset = quota_availability.seconds(window.get("reset_at", window.get("reset")))
        if reset <= 0:
            # An unknown weekly reset must not promise the known short reset.
            return None, True
        if reset > now_s:
            future.append(reset)
        else:
            stale_only = True
    if future:
        return quota_availability.stamp(max(future)), stale_only
    if exhausted:
        return None, True
    if isinstance(state, dict):
        reset = quota_availability.seconds(state.get("reset_at"))
        if reset > now_s:
            return quota_availability.stamp(reset), stale_only
    return None, False


def derive_availability(
    view: dict[str, Any],
    state: dict[str, Any],
    *,
    now: Any = None,
    function: str = "coding",
    model: str | None = None,
    pool: str | None = None,
) -> tuple[dict[str, Any], bool, bool, str | None]:
    """Read-only availability verdict: (info, verified, blocked, blocking_reset_at).

    Mirrors the read side of quota_availability.check/overlay without any
    write, probe or native check. `state` is the cached record for the pool
    ({} when absent). `view` must already be ledger-normalized.
    """
    now_s = _now_seconds(now)
    state = state if isinstance(state, dict) else {}
    if (quota_availability.local_ledger_evidence(state)
            or not _window_relevant(state, function, model, pool)):
        # A contaminated local estimate never vetoes dispatch.
        state = {}
    state_windows = state.get("windows")
    if (isinstance(state_windows, (dict, list))
            and state.get("status") in {"exhausted", "recheck-required"}
            and state.get("source") != "native-provider-error"
            and not _exhausted_relevant_windows({}, state, function, model, pool=pool)):
        state = {}
    row_at = quota_availability.seconds(view.get("observed_at"))
    state_at = quota_availability.seconds(state.get("observed_at"))

    status = str(state.get("status", "unknown")) if state else "unknown"
    if status not in AVAILABILITY_STATUSES:
        status = "unknown"
    source = state.get("source") or ("no-cached-refusal" if not state else None)
    reason = state.get("reason")
    scope = state.get("scope")
    recheck_at = state.get("recheck_at")
    reset_at = state.get("reset_at")
    recheck_due = False

    if status == "available":
        # Native-run proof stays valid 24 h; any other availability proof 3 h.
        # An expired proof is unknown, never available and never exhausted.
        lifetime = 86400 if state.get("source") == "native-run" else 10800
        accepted = {"native-run", "native-cli-check", "quota-snapshot", "kimi-native-usage"}
        deadline = state.get("expires_at")
        if deadline is None and source == "kimi-native-usage":
            deadline = state.get("recheck_at")
        if (source not in accepted or state_at <= 0
                or not 0 <= now_s - state_at < lifetime
                or (source == "kimi-native-usage" and deadline is None)
                or (deadline is not None and quota_availability.seconds(deadline) <= now_s)):
            status = "unknown"
            reason = "available-proof-expired"
    elif status in {"exhausted", "recheck-required"}:
        if (quota_availability.seconds(recheck_at) <= now_s
                or ("expires_at" in state and quota_availability.seconds(state["expires_at"]) <= now_s)
                or (reset_at is not None and quota_availability.seconds(reset_at) <= now_s)):
            # The refusal's own horizon elapsed: a fresh recheck is required,
            # never an automatic release.
            status = "recheck-required"
            recheck_due = True

    view_exhausted = (view.get("status") == "fresh" and _fresh(view, now_s)
                      and quota_availability.is_exhaustion(view))
    if view_exhausted and row_at >= state_at:
        # A true authoritative exhaustion (never a ledger row - normalization
        # removed its numeric headroom) is newer evidence than any cached proof.
        if source != "native-provider-error" or status not in {"exhausted", "recheck-required"}:
            status = "exhausted"
            source = "quota-snapshot"
            reason = reason or "usage-limit"
            scope = scope or "unknown"
            state_at = row_at
            recheck_at = view.get("observed_at")
    selected_view = view if view_exhausted and row_at >= state_at else {}
    selected_state = state if source != "quota-snapshot" or not selected_view else {}
    blocking_reset, window_recheck = _blocking_reset(
        selected_view, selected_state, function, model, now_s, pool)
    exhausted = _exhausted_relevant_windows(selected_view, selected_state,
                                           function, model, now_s, pool)
    if selected_view and exhausted and all(
            0 < quota_availability.seconds(w.get("reset_at", w.get("reset"))) <= now_s for w in exhausted):
        status, reason = "recheck-required", "exhaustion-reset-elapsed"
        recheck_due = True
    if status in {"exhausted", "recheck-required"}:
        reset_at = blocking_reset
        recheck_due = recheck_due or window_recheck
    else:
        reset_at = blocking_reset = None
    if status == "unknown" and reason is None and not state:
        reason = "no-cached-refusal"

    verified = (
        status == "available"
        and state.get("source") in {"native-run", "native-cli-check", "quota-snapshot", "kimi-native-usage"}
    )
    info = {
        "status": status,
        "source": source,
        "reason": reason,
        "scope": scope,
        "observed_at": state.get("observed_at"),
        "recheck_at": recheck_at,
        "reset_at": reset_at,
        "recheck_due": recheck_due,
    }
    if selected_view and source == "quota-snapshot":
        info["observed_at"] = view.get("observed_at")
    for key in ("windows", "last_good", "attempt_error", "last_attempt_at", "expires_at"):
        if key in state:
            info[key] = state[key]
    blocked = status in {"exhausted", "recheck-required"}
    return info, verified, blocked, blocking_reset


def annotate_view(
    agent_id: str,
    view: dict[str, Any],
    profile: dict[str, Any] | None,
    now: Any = None,
    function: str = "coding",
    model: str | None = None,
) -> dict[str, Any]:
    """Annotate one quota view with freshness, blocker and availability.

    Backwards compatible with the historical fleet.annotate_quota_views row
    (all original fields plus observed_at, age_hours, current, blocker) and
    adds separate availability fields: availability (plain dict),
    availability_status, availability_blocked, availability_verified and
    blocking_reset_at. Advisory ledgers stay estimates; a fresh verified
    native-run proof clears only the availability veto, never invents numeric
    allowance.
    """
    raw = view if isinstance(view, dict) else {}
    state = raw.get("availability_state", raw.get("availability"))
    row = quota_collectors.normalize_inferred_bailian_ledger(raw, profile)
    row.pop("availability_state", None)
    row.setdefault("observed_at", None)
    row.setdefault("age_hours", None)
    pool = str(profile.get("quota_pool_id") or agent_id) if isinstance(profile, dict) else agent_id
    now_s = _now_seconds(now)
    if not _window_relevant(row, function, model, pool):
        row.update(status="unknown", headroom=None, reported_headroom=None, hard_blocking=False)
        for fraction, _, _ in _VIEW_WINDOWS:
            if fraction in row:
                row[fraction] = None
        row["quota_evidence_reason"] = "pool-or-scope-mismatch"
    elif isinstance(row.get("windows"), (dict, list)) and not quota_availability.local_ledger_evidence(row):
        fractions = [_remaining(w) for w in _windows(row)
                     if _window_relevant(w, function, model, pool) and _window_current(w, now_s)]
        fractions = [v for v in fractions if v is not None]
        row["reported_headroom"] = min(fractions) if fractions else None
        row["headroom"] = row["reported_headroom"] if row.get("status") == "fresh" else None
        row["hard_blocking"] = bool(row.get("authority") == "authoritative"
                                    and row.get("status") == "fresh" and fractions and min(fractions) <= 0)
    for key in ("headroom", "reported_headroom"):
        if key in row and _number(row[key]) is None:
            row[key] = None
    if row.get("status") == "fresh" and not _fresh(row, now_s):
        row.update(status="stale", headroom=None, hard_blocking=False)
    blocker = quota_view_blocker_text(agent_id, row, profile)
    row["current"] = blocker is None
    row["blocker"] = blocker
    info, verified, blocked, blocking_reset = derive_availability(
        row, state if isinstance(state, dict) else {},
        now=now, function=function, model=model, pool=pool,
    )
    row["availability"] = info
    row["availability_status"] = info["status"]
    row["availability_blocked"] = blocked
    row["availability_verified"] = verified
    row["blocking_reset_at"] = blocking_reset
    for key in ("last_good", "attempt_error", "last_attempt_at"):
        if key in info:
            row.setdefault(key, info[key])
    if verified:
        # Clear only the availability veto, not invent numeric remaining tokens.
        row["hard_blocking"] = False
    return row


def read_availability_state(root: Path, pool: str) -> dict[str, Any]:
    """Bounded READ-ONLY cache read; corrupt or absent state is empty, sanitized."""
    try:
        return quota_availability.read_cache(quota_availability.cache_path(root, pool))
    except (OSError, ValueError, TypeError, AttributeError, RecursionError):
        return {}


def read_snapshot(
    root: Path,
    profiles: dict[str, dict[str, Any]],
    ids: Iterable[str] | None = None,
    now: Any = None,
) -> dict[str, dict[str, Any]]:
    """READ-ONLY annotated snapshot for `ids` (default: all `profiles` keys).

    Combines adaptive.latest_quotas with the bounded availability cache.
    No native check, no refresh, no secret read, no write, no fleet.py import.
    """
    root = Path(root)
    profiles = profiles if isinstance(profiles, dict) else {}
    agent_ids = [str(a) for a in (ids if ids is not None else profiles)]
    now_dt = _now_datetime(now)
    pool_ids = {
        a: str(profiles[a]["quota_pool_id"])
        for a in agent_ids
        if isinstance(profiles.get(a), dict) and profiles[a].get("quota_pool_id")
    }
    params = inspect.signature(adaptive.latest_quotas).parameters
    kwargs: dict[str, Any] = {"profile_ids": agent_ids, "now": now_dt}
    if "pool_ids" in params:
        kwargs["pool_ids"] = pool_ids or None
    read_error = False
    try:
        views = adaptive.latest_quotas(root, **kwargs)
    except (OSError, ValueError, TypeError, KeyError, adaptive.AdaptiveError):
        views = {}
        read_error = True
    snapshot: dict[str, dict[str, Any]] = {}
    for agent_id in agent_ids:
        profile = profiles.get(agent_id)
        pool = (
            str(profile.get("quota_pool_id") or agent_id)
            if isinstance(profile, dict)
            else agent_id
        )
        view = dict((views or {}).get(agent_id) or {})
        if read_error:
            view.update(status="unknown", headroom=None,
                        quota_read_error="quota-store-unreadable")
        view["availability_state"] = read_availability_state(root, pool)
        snapshot[agent_id] = annotate_view(agent_id, view, profile, now=now_dt)
    return snapshot
