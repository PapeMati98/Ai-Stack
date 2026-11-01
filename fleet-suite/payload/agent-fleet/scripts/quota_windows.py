"""Robust pure parsing and normalization of AIUsage quota windows.

This module is the single source of truth for the three rules that the
AI Panel/Fleet quota signal has historically violated:

1. **Window label comes from duration, not from position.** Codex "primary"
   and "secondary" are positional aliases; only the actual window_minutes
   decides whether a window is short/weekly/monthly. Kimi's "short-window"
   in the legacy proto3 shape is a 5-hour window, never a 7-day one.
2. **Malformed fields cannot imply availability or exhaustion.**
   NaN, bool, None, empty string, missing required key — each must produce an
   explicit "unknown" branch. NaN that survives math.isfinite() and a
   present-but-bool 0 are the common false-exhaustion traps.
3. **Multiple blocking windows choose the latest known reset.** A single
   recovered block must not invent a reset for an unknown or elapsed one;
   the answer in that case is "no promised recovery", not a synthetic
   best-case timestamp.

The module is intentionally pure (no I/O, no global state, no logging)
so it can be exercised directly from tests, and so that the existing
Codex/Kimi parsers can import individual helpers without dragging in the
whole collector pipeline.

Optional offline CLI:

    python quota_windows.py --provider minimax --account-or-pool <pool> --observed-at <ISO> --input <file.json>

This CLI consumes an *already-redacted* quota JSON file. It requires
explicit provider, account-or-pool binding and observed timestamp; it
never reads credentials, opens a cache, performs network I/O, or falls
back across regions automatically. It exists to give downstream
collectors a deterministic normalization path; the manual native
collector remains manual until real endpoint integration is scoped.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import sys
from typing import Any, Iterable, Mapping


UTC = dt.timezone.utc

# Window-length envelopes (hours). Generous on the outer bound to match the
# real Codex/Kimi/MiniMax variants we have measured; everything outside the
# envelopes is treated as "unknown window" and dropped from the label.
SHORT_WINDOW_MAX_HOURS = 24.0
WEEKLY_WINDOW_MIN_HOURS = 6 * 24.0
WEEKLY_WINDOW_MAX_HOURS = 8 * 24.0
MONTHLY_WINDOW_MIN_HOURS = 28 * 24.0
MONTHLY_WINDOW_MAX_HOURS = 31 * 24.0

# Plausible epoch bounds (seconds). Anything outside is treated as malformed.
EPOCH_MIN = 0
EPOCH_MAX = 4_102_444_800  # year 2100-01-01

# Plausible epoch bounds (milliseconds).
EPOCH_MS_MAX = 13 * 10**12  # well under year 2286


class QuotaWindowError(ValueError):
    """A malformed window that cannot be normalized to a known kind."""


def _to_float(value: Any) -> float | None:
    """Validate a numeric scalar: reject bool/None/NaN/string/non-finite."""
    if isinstance(value, bool) or value is None:
        return None
    if not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    return number


def sanitize_fraction(value: Any, *, label: str) -> float | None:
    """Validate a 0..1 fraction. Returns None for any non-conforming value."""
    number = _to_float(value)
    if number is None:
        return None
    if number < 0.0 or number > 1.0:
        return None
    return number


def sanitize_percent(value: Any, *, label: str) -> float | None:
    """Validate a 0..100 percentage. Returns None for any non-conforming value."""
    number = _to_float(value)
    if number is None:
        return None
    if number < 0.0 or number > 100.0:
        return None
    return number


def sanitize_non_negative(value: Any, *, label: str) -> float | None:
    """Validate a non-negative count. NaN/bool/None/missing ⇒ None."""
    number = _to_float(value)
    if number is None:
        return None
    if number < 0:
        return None
    return number


def sanitize_positive(value: Any, *, label: str) -> float | None:
    """Validate a strictly positive value (limit, capacity). Returns None on bad input."""
    number = _to_float(value)
    if number is None:
        return None
    if number <= 0:
        return None
    return number


def window_kind_from_duration(hours: Any) -> str | None:
    """Pick short/weekly/monthly from the actual window length, not its label.

    A None input or a value outside any envelope returns None; positional
    aliases ("primary", "secondary") must not be passed here.
    """
    number = _to_float(hours)
    if number is None or number <= 0:
        return None
    if number <= SHORT_WINDOW_MAX_HOURS:
        return "short"
    if WEEKLY_WINDOW_MIN_HOURS <= number <= WEEKLY_WINDOW_MAX_HOURS:
        return "weekly"
    if MONTHLY_WINDOW_MIN_HOURS <= number <= MONTHLY_WINDOW_MAX_HOURS:
        return "monthly"
    return None


def window_kind_from_minutes(minutes: Any) -> str | None:
    return window_kind_from_duration(
        None if _to_float(minutes) is None else minutes / 60.0
    )


def parse_iso_zoned(value: Any) -> dt.datetime | None:
    """Parse an ISO 8601 timestamp with explicit timezone; reject naive values."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    try:
        return parsed.astimezone(UTC)
    except (OverflowError, OSError):
        return None


def parse_epoch_seconds(value: Any) -> dt.datetime | None:
    number = _to_float(value)
    if number is None or number < EPOCH_MIN or number > EPOCH_MAX:
        return None
    try:
        return dt.datetime.fromtimestamp(number, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def parse_epoch_milliseconds(value: Any) -> dt.datetime | None:
    number = _to_float(value)
    if number is None or number < EPOCH_MIN or number > EPOCH_MS_MAX:
        return None
    try:
        return dt.datetime.fromtimestamp(number / 1000.0, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


_RELATIVE_RE = re.compile(
    r"^\s*in\s+(\d+)\s+(seconds?|minutes?|hours?|days?|weeks?|months?)\s*$",
    re.IGNORECASE,
)
_RELATIVE_UNITS = {
    "second": 1,
    "seconds": 1,
    "minute": 60,
    "minutes": 60,
    "hour": 3600,
    "hours": 3600,
    "day": 86400,
    "days": 86400,
    "week": 7 * 86400,
    "weeks": 7 * 86400,
    "month": 30 * 86400,  # approximate; pace only uses relative as fallback
    "months": 30 * 86400,
}


def parse_relative_seconds(value: Any, *, now: dt.datetime) -> dt.datetime | None:
    """Parse a "in N seconds/minutes/hours/days/weeks/months" string.

    The relative form requires an explicit `now`. Without it the parser
    returns None — a relative timestamp cannot be anchored to wall-clock
    time by the parser alone.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    match = _RELATIVE_RE.match(value)
    if match is None:
        return None
    count = int(match.group(1))
    unit_seconds = _RELATIVE_UNITS.get(match.group(2).lower())
    if unit_seconds is None:
        return None
    try:
        return (now + dt.timedelta(seconds=count * unit_seconds)).astimezone(UTC)
    except OverflowError:
        return None


def parse_reset(
    value: Any,
    *,
    observed_at: dt.datetime | None = None,
    now: dt.datetime | None = None,
) -> tuple[dt.datetime | None, str | None]:
    """Normalize a reset timestamp from any of ISO/seconds/ms/relative.

    The second return slot is a short reason when parsing failed:
    "missing", "not-string", "bad-format", "out-of-range". An elapsed
    reset (parsed moment not strictly after now) is *not* a parse error;
    the caller decides whether to keep it as stale or drop it.

    observed_at is only used to constrain relative parsing: when relative
    is given without explicit `now`, the observed moment is the anchor
    (the only honest default for "in 5 hours" without a clock).
    """
    anchor = now if now is not None else observed_at
    if anchor is not None:
        anchor = anchor.astimezone(UTC) if anchor.tzinfo is not None else anchor.replace(tzinfo=UTC)

    if value is None:
        return None, "missing"
    if isinstance(value, bool):
        return None, "not-string"
    parsed = parse_iso_zoned(value)
    if parsed is not None:
        return parsed, None
    number = _to_float(value)
    parsed = (parse_epoch_milliseconds(value) if number is not None and number > EPOCH_MAX
              else parse_epoch_seconds(value))
    if parsed is not None:
        return parsed, None
    parsed = parse_relative_seconds(value, now=anchor) if anchor is not None else None
    if parsed is not None:
        return parsed, None
    return None, "bad-format"


def is_reset_elapsed(moment: dt.datetime | None, *, now: dt.datetime | None = None) -> bool:
    """A reset moment strictly behind now is elapsed (no recovery proof)."""
    if moment is None:
        return False
    check = now if now is not None else dt.datetime.now(UTC)
    if check.tzinfo is None:
        check = check.replace(tzinfo=UTC)
    else:
        check = check.astimezone(UTC)
    moment_utc = moment.astimezone(UTC) if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    return moment_utc <= check


# --- Kimi parser ---------------------------------------------------------

# Kimi exposes two parallel shapes:
#  - Legacy proto3 detail rows with explicit `used`, `limit`, `resetTime`.
#  - Current 0..1 ratios under `usages.<key>.used_ratio` with reset_time.
#
# The legacy detail shape preserves proto3 omitted-zero semantics ONLY for
# the documented `used`/`limit`/`resetTime` triple: an absent `used` is
# derived from `remaining` when present and is otherwise the proto3
# default 0. An absent `limit` stays unknown and the whole answer stays
# unknown. Arbitrary missing fields are not silently treated as zero.
#
# A spent window whose reset has already passed is a stale counter, not
# a current exhaustion. Multiple blocking windows choose the latest known
# reset; if no blocking window has a reset, the answer carries no
# promised recovery timestamp.

KIMI_RATIO_WINDOWS: tuple[tuple[str, str, float | None], ...] = (
    ("short-window", "limit_5h", 5.0),
    ("weekly", "limit_7d", 168.0),
    ("monthly", "limit_month_total", None),
)


def _kimi_legacy_detail(raw: Any) -> tuple[float, float, dt.datetime | None]:
    """Return (used, limit, reset) for one proto3-JSON Kimi detail row.

    Raises QuotaWindowError when the row is not a usable Kimi window.
    """
    if not isinstance(raw, Mapping):
        raise QuotaWindowError("Kimi detail row is not an object")
    # Protobuf JSON encodes int64 counts as decimal strings. Accept them only
    # in this documented detail shape; general quota scalars remain strict.
    raw = dict(raw)
    for key in ("limit", "used", "remaining"):
        value = raw.get(key)
        if isinstance(value, str) and re.fullmatch(r"[0-9]+", value) and len(value) <= 20:
            raw[key] = int(value)
    limit_raw = raw.get("limit")
    if limit_raw is None:
        raise QuotaWindowError("Kimi detail row has no readable limit")
    limit = sanitize_positive(limit_raw, label="kimi.limit")
    if limit is None:
        raise QuotaWindowError("Kimi detail row limit is not a positive number")
    if "used" in raw:
        used = sanitize_non_negative(raw["used"], label="kimi.used")
        if used is None:
            raise QuotaWindowError("Kimi detail row used is not a non-negative number")
    elif "remaining" in raw:
        remaining = sanitize_non_negative(raw["remaining"], label="kimi.remaining")
        if remaining is None:
            raise QuotaWindowError("Kimi detail row remaining is not a non-negative number")
        used = max(0.0, limit - remaining)
    else:
        # proto3 omitted-zero for the documented `used` field only; arbitrary
        # unknown fields never default to zero.
        used = 0.0
    reset, _ = parse_reset(raw.get("resetTime"))
    return used, limit, reset


def parse_kimi_payload(
    payload: Any,
    *,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Normalize a Kimi /usages payload to {status, windows, ...}.

    The shape preserves the legacy (used/limit/resetTime) and current
    (used_ratio) views; supported aliases and used ratios keep current
    consumers fed. Block selection uses the latest known reset among
    multiple blocking windows and returns no recovery timestamp when no
    blocking window has a reset.
    """
    if not isinstance(payload, Mapping):
        raise QuotaWindowError("Invalid Kimi usage payload")
    check = now if now is not None else dt.datetime.now(UTC)
    check = check.astimezone(UTC) if check.tzinfo is not None else check.replace(tzinfo=UTC)
    rows: list[dict[str, Any]] = []
    summary_used = summary_limit = None
    summary_reset: dt.datetime | None = None
    if "usage" in payload and isinstance(payload["usage"], Mapping):
        used, limit, reset = _kimi_legacy_detail(payload["usage"])
        summary_used, summary_limit, summary_reset = used, limit, reset
        rows.append(
            {
                "scope": "weekly",
                "used": used,
                "limit": limit,
                "reset": reset,
                "window_hours": None,
                "kind": "legacy-summary",
            }
        )
    limits = payload.get("limits")
    if "limits" in payload and not isinstance(limits, list):
        raise QuotaWindowError("Kimi limits is not a list")
    if isinstance(limits, list):
        for item in limits:
            if not isinstance(item, Mapping):
                raise QuotaWindowError("Kimi limit entry is not an object")
            window = item.get("window")
            if not isinstance(window, Mapping) or not window:
                raise QuotaWindowError("Kimi limit entry has no readable window")
            unit = window.get("timeUnit")
            duration = window.get("duration")
            hours = _window_hours_from_unit_duration(unit, duration)
            scope = _kimi_scope_from_window(window, hours)
            try:
                used, limit, reset = _kimi_legacy_detail(item.get("detail"))
            except QuotaWindowError:
                raise
            rows.append(
                {
                    "scope": scope,
                    "used": used,
                    "limit": limit,
                    "reset": reset,
                    "window_hours": hours,
                    "kind": "legacy-limit",
                }
            )
    usages = payload.get("usages")
    if isinstance(usages, Mapping):
        covered = {row["scope"] for row in rows}
        for scope, key, hours in KIMI_RATIO_WINDOWS:
            entry = usages.get(key)
            if scope in covered or key not in usages:
                continue
            if not isinstance(entry, Mapping):
                raise QuotaWindowError("Kimi ratio window is not an object")
            ratio = entry.get("used_ratio")
            if ratio is None:
                raise QuotaWindowError("Kimi ratio window has no readable ratio")
            # Preserve Kimi's prior numeric-string ratio compatibility only
            # in this explicit field; general quota scalars remain strict.
            if isinstance(ratio, str) and len(ratio) <= 32 and re.fullmatch(r"(?:0(?:\.[0-9]+)?|1(?:\.0+)?)", ratio):
                ratio = float(ratio)
            ratio_value = sanitize_fraction(ratio, label=f"kimi.{key}.used_ratio")
            if ratio_value is None:
                raise QuotaWindowError("Kimi ratio window ratio is malformed")
            used = round(ratio_value * 100.0, 6)
            reset, _ = parse_reset(entry.get("reset_time"))
            rows.append(
                {
                    "scope": scope,
                    "used": used,
                    "limit": 100.0,
                    "reset": reset,
                    "window_hours": hours,
                    "kind": "ratio",
                }
            )
    if not rows:
        raise QuotaWindowError("Kimi usage has no readable window")
    windows: list[dict[str, Any]] = []
    blocking: list[dict[str, Any]] = []
    for row in rows:
        remaining = max(0.0, row["limit"] - row["used"])
        remaining = round(min(1.0, remaining / row["limit"]), 9) if row["limit"] > 0 else 0.0
        window = {
            "scope": row["scope"],
            "used": row["used"],
            "limit": row["limit"],
            "remaining": remaining if row["limit"] > 0 else None,
            "reset_at": row["reset"].isoformat().replace("+00:00", "Z") if row["reset"] else None,
            "window_hours": row["window_hours"],
            "kind": row["kind"],
        }
        windows.append(window)
        if row["used"] >= row["limit"] and not is_reset_elapsed(row["reset"], now=check):
            blocking.append(window)
    expired = any(is_reset_elapsed(parse_iso_zoned(w["reset_at"]), now=check) for w in windows)
    if blocking:
        # Latest known reset wins; an unknown reset never invents a recovery time.
        recoverable = [w for w in blocking if w["reset_at"]]
        unknown_reset = len(recoverable) != len(blocking) or expired
        pick = (
            max(
                recoverable,
                key=lambda w: parse_iso_zoned(w["reset_at"]) or check,
            )
            if recoverable
            else blocking[0]
        )
        return {
            "status": "exhausted",
            "source": "kimi-native-usage",
            "scope": pick["scope"],
            "reset_at": None if unknown_reset else pick["reset_at"],
            "covered_scopes": sorted({row["scope"] for row in rows}),
            "windows": windows,
            "reason": "usage-limit",
            "blocking_count": len(blocking),
            "no_promised_recovery": unknown_reset or pick["reset_at"] is None,
        }
    if expired:
        return {"status": "unknown", "source": "kimi-native-usage", "scope": None,
                "reset_at": None, "windows": windows, "reason": "window-reset-requires-recheck",
                "covered_scopes": sorted({row["scope"] for row in rows}),
                "blocking_count": 0, "no_promised_recovery": True}
    return {
        "status": "available",
        "source": "kimi-native-usage",
        "scope": None,
        "reset_at": None,
        "covered_scopes": sorted({row["scope"] for row in rows}),
        "windows": windows,
        "reason": "remaining-plan-capacity",
        "blocking_count": 0,
        "no_promised_recovery": False,
    }


def _window_hours_from_unit_duration(unit: Any, duration: Any) -> float | None:
    """Kimi/TIME_UNIT_* duration in hours (TIME_UNIT_MONTH ≈ 30 d)."""
    units = {
        "TIME_UNIT_MINUTE": 1.0 / 60.0,
        "TIME_UNIT_HOUR": 1.0,
        "TIME_UNIT_DAY": 24.0,
        "TIME_UNIT_WEEK": 168.0,
        "TIME_UNIT_MONTH": 30.0 * 24.0,
    }
    if not isinstance(unit, str) or unit not in units:
        return None
    count = _to_float(duration)
    if count is None or count <= 0:
        return None
    return count * units[unit]


def _kimi_scope_from_window(window: Mapping[str, Any], hours: float | None) -> str:
    unit = window.get("timeUnit") if isinstance(window, Mapping) else None
    if unit == "TIME_UNIT_WEEK":
        return "weekly"
    if unit == "TIME_UNIT_MONTH":
        return "monthly"
    if unit == "TIME_UNIT_DAY":
        return "daily"
    if hours is not None:
        kind = window_kind_from_duration(hours)
        if kind == "weekly":
            return "weekly"
        if kind == "monthly":
            return "monthly"
        if kind == "short":
            return "short-window"
    return "short-window"


# --- Codex parser --------------------------------------------------------

# Codex rate_limits sends positional aliases (primary / secondary). The
# only authoritative signal is window_minutes; Code Review's "primary"
# window is independently 5 h, while ordinary coding's "primary" can be
# weekly. Per-model/family windows stay distinct: the parser returns
# every real window it can read, and never rewrites labels based on
# positional order. The shortest known window is preserved even when the
# caller only asks for one.

def parse_codex_rate_limit_window(
    rate_limits: Mapping[str, Any],
    *,
    code_review: bool = False,
    observed_at: dt.datetime | None = None,
) -> dict[str, Any]:
    """Normalize one Codex rate_limits block to per-window rows.

    Returns a dict with the windows (per kind) and the known "shortest"
    context for the function family. Code Review is independent: the
    "code_review" flag changes nothing about the resolution logic, but is
    recorded so callers that want per-family windows can keep them
    distinct.
    """
    if not isinstance(rate_limits, Mapping):
        raise QuotaWindowError("Codex rate_limits is not an object")
    plan_type = rate_limits.get("plan_type")
    plan_label = plan_type.strip() if isinstance(plan_type, str) and plan_type.strip() else None
    windows: dict[str, dict[str, Any]] = {}
    for key in ("primary", "secondary"):
        window = rate_limits.get(key)
        if not isinstance(window, Mapping):
            continue
        try:
            parsed = _parse_codex_window_dict(window, observed_at=observed_at)
        except QuotaWindowError:
            continue
        if parsed is None:
            continue
        kind = parsed["kind"]
        if kind in windows:
            # Per-family distinct: keep both rows when the same kind comes
            # from primary and secondary. A duplicated kind means one
            # positional alias disagrees with the other; we preserve both
            # rather than collapse, so callers can mark a disagreement.
            code_review_key = "code_review" if code_review else "coding"
            bucket_key = f"{kind}:{code_review_key}"
            windows[bucket_key] = {**parsed, "kind": bucket_key}
            continue
        windows[kind] = parsed
    if not windows:
        raise QuotaWindowError("Codex rate_limits has no readable window")
    shortest = min(
        ((k, v) for k, v in windows.items() if not k.endswith(":code_review")),
        key=lambda item: item[1].get("window_hours") or float("inf"),
        default=(None, None),
    )
    return {
        "plan_type": plan_label,
        "windows": windows,
        "shortest": shortest[0],
        "code_review": code_review,
    }


def _parse_codex_window_dict(window: Mapping[str, Any], *, observed_at=None) -> dict[str, Any] | None:
    used_raw = window.get("used_percent")
    used_percent = sanitize_percent(used_raw, label="codex.used_percent")
    if used_percent is None:
        return None
    minutes = window.get("window_minutes")
    minutes_value = _to_float(minutes)
    if minutes_value is None or minutes_value <= 0:
        return None
    kind = window_kind_from_minutes(minutes_value)
    if kind is None:
        return None
    resets_at = window.get("resets_at")
    resets_in = window.get("resets_in_seconds")
    reset, reason = parse_reset(resets_at, observed_at=observed_at)
    seconds = _to_float(resets_in)
    if reset is None and observed_at is not None and seconds is not None and seconds >= 0:
        try:
            reset = observed_at + dt.timedelta(seconds=seconds)
            reason = None
        except OverflowError:
            pass
    remaining = round(min(1.0, max(0.0, 1.0 - used_percent / 100.0)), 9)
    return {
        "kind": kind,
        "used_percent": used_percent,
        "remaining": remaining,
        "reset": reset,
        "reset_reason": reason,
        "window_hours": round(minutes_value / 60.0, 6),
        "window_minutes": minutes_value,
    }


# --- MiniMax parser ------------------------------------------------------

# MiniMax's plan-quota JSON often carries:
#  - `token_plan` / `remains` (counts); absent fields are unknown, not zero.
#  - `model_remains` (per-model object); `general` and `video` are
#    distinct buckets and must never be aggregated as one pool.
#  - `interval_remaining_percent` / `weekly_remaining_percent`: trusted
#    even when counts are 0; these are the only authoritative fractions.
#  - `base_resp.status_code` non-zero is an error: even HTTP 200 with
#    status_code != 0 must surface as UNKNOWN.
#
# The offline CLI ships a deterministic normalization entry point that
# requires explicit provider/account-or-pool/observed_at; it never reads
# credentials, opens a cache, performs network I/O or auto-region fallback.

MINIMAX_GENERAL = "general"
MINIMAX_VIDEO = "video"
MINIMAX_KNOWN_BUCKETS = frozenset({MINIMAX_GENERAL, MINIMAX_VIDEO})


def parse_minimax_payload(
    payload: Any,
    *,
    provider: str,
    account_or_pool: str,
    observed_at: dt.datetime | None = None,
) -> dict[str, Any]:
    """Normalize an already-redacted MiniMax quota JSON file.

    Required bindings are explicit so this parser cannot silently accept
    an empty identity or skip an observed_at. Percent fields are honored
    even when the count fields are zero or missing. base_resp errors
    surface as UNKNOWN; a normal 429 must not surface as a global
    exhaustion.
    """
    if not isinstance(payload, Mapping):
        raise QuotaWindowError("MiniMax payload is not an object")
    if provider != "minimax":
        raise QuotaWindowError("MiniMax requires explicit provider")
    if not account_or_pool or not isinstance(account_or_pool, str):
        raise QuotaWindowError("MiniMax requires explicit account_or_pool binding")
    if not isinstance(observed_at, dt.datetime) or observed_at.tzinfo is None:
        raise QuotaWindowError("MiniMax requires explicit zoned observed_at")
    anchor = observed_at.astimezone(UTC)
    for key, expected in (("provider", provider), ("account_or_pool", account_or_pool), ("account", account_or_pool)):
        if key in payload and payload[key] != expected:
            raise QuotaWindowError("MiniMax identity mismatch")

    base_resp = payload.get("base_resp")
    if isinstance(base_resp, Mapping):
        status_code = base_resp.get("status_code")
        if _to_float(status_code) != 0:
            return _minimax_unknown(provider, account_or_pool, anchor, "base_resp-error")

    error_code = _to_float(payload.get("error_code"))
    if error_code not in (None, 0):
        return _minimax_unknown(provider, account_or_pool, anchor, "error-code")

    # Actual token_plan/remains response: one list row per resource family.
    # general is the coding plan; a spent video family cannot block coding.
    native_rows = payload.get("model_remains")
    if isinstance(native_rows, list):
        general = [r for r in native_rows if isinstance(r, Mapping)
                   and r.get("model_name") == "general"]
        if len(general) != 1:
            return _minimax_unknown(provider, account_or_pool, anchor, "missing-or-ambiguous-general-family")
        def window(row, prefix, reset_key):
            pct = sanitize_percent(row.get(prefix + "remaining_percent"), label=prefix + "remaining_percent")
            total = sanitize_non_negative(row.get(prefix + "total_count"), label=prefix + "total_count")
            used = sanitize_non_negative(row.get(prefix + "usage_count"), label=prefix + "usage_count")
            fraction = pct / 100.0 if pct is not None else (
                max(0.0, min(1.0, 1.0 - used / total))
                if total is not None and total > 0 and used is not None else None)
            reset, _ = parse_reset(row.get(reset_key), observed_at=anchor)
            return {"remaining_fraction": fraction,
                    "reset_at": reset.isoformat().replace("+00:00", "Z") if reset else None}
        def family(row):
            return {"interval": window(row, "current_interval_", "end_time"),
                    "weekly": window(row, "current_weekly_", "weekly_end_time")}
        buckets = {r["model_name"]: family(r) for r in native_rows
                   if isinstance(r, Mapping) and r.get("model_name") in MINIMAX_KNOWN_BUCKETS}
        coding = buckets["general"]
        has_fraction = any(w["remaining_fraction"] is not None for w in coding.values())
        result = {"provider": provider, "account_or_pool": account_or_pool,
                  "observed_at": anchor.isoformat().replace("+00:00", "Z"),
                  "status": "fresh" if has_fraction else "unknown",
                  "interval": coding["interval"], "weekly": coding["weekly"],
                  "model_remains": buckets, "has_fraction_evidence": has_fraction}
        if not has_fraction:
            result["reason"] = "missing-fraction-evidence"
        return result
    interval_pct = sanitize_percent(
        payload.get("interval_remaining_percent"),
        label="minimax.interval_remaining_percent",
    )
    weekly_pct = sanitize_percent(
        payload.get("weekly_remaining_percent"),
        label="minimax.weekly_remaining_percent",
    )
    interval_pct = interval_pct / 100.0 if interval_pct is not None else None
    weekly_pct = weekly_pct / 100.0 if weekly_pct is not None else None
    interval_reset, _ = parse_reset(payload.get("interval_reset_time"), now=anchor)
    weekly_reset, _ = parse_reset(payload.get("weekly_reset_time"), now=anchor)

    model_remains = payload.get("model_remains")
    if not isinstance(model_remains, Mapping):
        model_remains = {}
    buckets: dict[str, dict[str, Any]] = {}
    for bucket_name in MINIMAX_KNOWN_BUCKETS:
        bucket = model_remains.get(bucket_name)
        if not isinstance(bucket, Mapping):
            continue
        total = sanitize_non_negative(
            bucket.get("token_plan"),
            label=f"minimax.model_remains.{bucket_name}.token_plan",
        )
        remains_count = sanitize_non_negative(
            bucket.get("remains"),
            label=f"minimax.model_remains.{bucket_name}.remains",
        )
        if total is None and remains_count is None:
            continue
        fraction = None
        if total is not None and remains_count is not None and total > 0:
            fraction = round(min(1.0, max(0.0, remains_count / total)), 9)
        buckets[bucket_name] = {
            "token_plan": total,
            "remains": remains_count,
            "remaining_fraction": fraction,
        }

    # Fractions are authoritative even when counts are zero; no synthetic
    # token total is fabricated.
    interval = {
        "remaining_fraction": interval_pct,
        "reset_at": interval_reset.isoformat().replace("+00:00", "Z")
        if interval_reset
        else None,
    }
    weekly = {
        "remaining_fraction": weekly_pct,
        "reset_at": weekly_reset.isoformat().replace("+00:00", "Z")
        if weekly_reset
        else None,
    }
    has_fraction = interval_pct is not None or weekly_pct is not None
    return {
        "provider": provider,
        "account_or_pool": account_or_pool,
        "observed_at": anchor.isoformat().replace("+00:00", "Z"),
        "status": "fresh" if has_fraction else "unknown",
        "interval": interval,
        "weekly": weekly,
        "model_remains": buckets,
        "has_fraction_evidence": has_fraction,
    }


def _minimax_unknown(
    provider: str,
    account_or_pool: str,
    observed_at: dt.datetime,
    reason: str,
) -> dict[str, Any]:
    return {
        "provider": provider,
        "account_or_pool": account_or_pool,
        "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
        "status": "unknown",
        "reason": reason,
        "interval": {"remaining_fraction": None, "reset_at": None},
        "weekly": {"remaining_fraction": None, "reset_at": None},
        "model_remains": {},
        "has_fraction_evidence": False,
    }


def parse_response_error(
    *,
    status_code: Any,
    body: Any,
) -> dict[str, Any]:
    """Type a fetch error so the consumer never sees a false 0%/100%.

    401/403/404/429/500/timeout/empty/schema each get an explicit reason;
    an ordinary 429 must not be conflated with global exhaustion.
    """
    code = _to_float(status_code)
    body_text = "" if body is None else str(body)
    if code is None:
        reason = "schema-error" if body_text else "empty"
        return {"status": "unknown", "reason": reason, "global_exhausted": False}
    if code in (401.0, 403.0):
        return {"status": "unknown", "reason": "auth", "global_exhausted": False}
    if code == 404.0:
        return {"status": "unknown", "reason": "not-found", "global_exhausted": False}
    if code == 429.0:
        # Not a global block; only a per-feature limit. Caller decides scope.
        return {"status": "rate-limited", "reason": "ordinary-429", "global_exhausted": False}
    if code == 500.0:
        return {"status": "unknown", "reason": "server", "global_exhausted": False}
    if code >= 500.0:
        return {"status": "unknown", "reason": "server", "global_exhausted": False}
    return {"status": "unknown", "reason": "schema-error", "global_exhausted": False}


def last_good_normalization(
    current: Mapping[str, Any] | None,
    error: Mapping[str, Any] | None,
    *,
    observed_at: dt.datetime | None = None,
) -> dict[str, Any]:
    """Carry a failed-refresh result alongside the last good snapshot.

    The current sample is the preferred record; the prior good sample
    stays attached as `last_good`, and the attempt error as `attempt_error`,
    so consumers can never mistake a failed refresh for fresh evidence.
    """
    now = observed_at if observed_at is not None else dt.datetime.now(UTC)
    now = now.astimezone(UTC) if now.tzinfo is not None else now.replace(tzinfo=UTC)
    out: dict[str, Any] = {
        "status": "unknown",
        "observed_at": now.isoformat().replace("+00:00", "Z"),
        "last_good": dict(current) if isinstance(current, Mapping) else None,
        "attempt_error": dict(error) if isinstance(error, Mapping) else None,
        "last_attempt_at": now.isoformat().replace("+00:00", "Z"),
    }
    if isinstance(current, Mapping):
        out["status"] = "stale" if isinstance(error, Mapping) else current.get("status", "unknown")
        out["observed_at"] = current.get("observed_at", out["observed_at"])
    return out


def validate_identity(
    *,
    sample: Mapping[str, Any],
    expected_provider: str,
    expected_account_or_pool: str,
) -> None:
    """Reject a sample whose provider/account/workspace doesn't match the binding.

    A reused cache row from a different account must never silently pass;
    the caller has to either drop the row or rebind it explicitly.
    """
    if not isinstance(sample, Mapping):
        raise QuotaWindowError("Sample is not an object")
    sample_provider = sample.get("provider")
    sample_account = sample.get("account_or_pool") or sample.get("account")
    if sample_provider is None and sample_account is None:
        # Legacy snapshots without identity fields: still allowed when the
        # caller did not bind an expected identity.
        if expected_provider or expected_account_or_pool:
            raise QuotaWindowError(
                "Sample has no provider/account_or_pool binding; cannot verify identity"
            )
        return
    if expected_provider and sample_provider != expected_provider:
        raise QuotaWindowError(
            f"Provider mismatch: sample={sample_provider!r} expected={expected_provider!r}"
        )
    if expected_account_or_pool and sample_account != expected_account_or_pool:
        raise QuotaWindowError(
            f"Account/pool mismatch: sample={sample_account!r} expected={expected_account_or_pool!r}"
        )


# --- Offline CLI ---------------------------------------------------------


def _cli_normalize(args: argparse.Namespace) -> int:
    if not args.provider or not args.account_or_pool:
        print(
            "error: --provider and --account-or-pool are required",
            file=sys.stderr,
        )
        return 2
    if not args.input:
        print("error: --input is required", file=sys.stderr)
        return 2
    if not args.observed_at:
        print("error: --observed-at is required", file=sys.stderr)
        return 2
    observed_at = None
    if args.observed_at:
        observed_at = parse_iso_zoned(args.observed_at)
        if observed_at is None:
            print(
                f"error: --observed-at must be an ISO-zoned timestamp: {args.observed_at!r}",
                file=sys.stderr,
            )
            return 2
    try:
        with open(args.input, "r", encoding="utf-8") as handle:
            raw = handle.read(2 * 1024 * 1024)
    except OSError as exc:
        print(f"error: cannot read input: {exc}", file=sys.stderr)
        return 2
    if len(raw) >= 2 * 1024 * 1024:
        print("error: input too large (> 2 MiB)", file=sys.stderr)
        return 2
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        print(f"error: input is not JSON: {exc}", file=sys.stderr)
        return 2
    try:
        normalized = parse_minimax_payload(
            payload,
            provider=args.provider,
            account_or_pool=args.account_or_pool,
            observed_at=observed_at,
        )
    except QuotaWindowError as exc:
        print(f"error: normalization failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(normalized, indent=2, sort_keys=True))
    return 0


def build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quota_windows",
        description="Offline normalization of an already-redacted MiniMax quota JSON file.",
    )
    parser.add_argument("--provider", help="Provider id, e.g. 'minimax'.")
    parser.add_argument(
        "--account-or-pool",
        help="Explicit account or pool binding for the snapshot.",
    )
    parser.add_argument(
        "--observed-at",
        help="ISO-zoned UTC moment (e.g. 2026-10-05T12:00:00Z).",
    )
    parser.add_argument(
        "--input",
        help="Path to a JSON file with the MiniMax quota payload.",
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_argparser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    return _cli_normalize(args)


if __name__ == "__main__":
    raise SystemExit(main())
