"""Pure calculations for subscription consumption pace from Fleet evidence."""

from __future__ import annotations

import calendar
import datetime as dt
import math
from typing import Any, Iterable


UTC = dt.timezone.utc
WINDOW_FIELDS = {
    "monthly": ("monthly_remaining_fraction", "monthly_reset_at"),
    "weekly": ("weekly_remaining_fraction", "weekly_reset_at"),
    "short": ("short_window_remaining_fraction", "short_window_reset_at"),
}


def _timestamp(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _window_start(kind: str, reset: dt.datetime, short_window_hours: float | None) -> dt.datetime | None:
    if kind == "weekly":
        return reset - dt.timedelta(days=7)
    if kind == "short":
        if short_window_hours is None or short_window_hours <= 0:
            return None
        return reset - dt.timedelta(hours=short_window_hours)
    month = reset.month - 1 or 12
    year = reset.year - (1 if reset.month == 1 else 0)
    day = min(reset.day, calendar.monthrange(year, month)[1])
    return reset.replace(year=year, month=month, day=day)


def _fraction(row: dict[str, Any], kind: str, capacity: float | None) -> float | None:
    value = _number(row.get(WINDOW_FIELDS[kind][0]))
    if value is None and kind == "monthly" and capacity is not None and capacity > 0:
        credits = _number(row.get("credits_remaining"))
        if credits is not None:
            value = credits / capacity
    return value if value is not None and 0.0 <= value <= 1.0 else None


def _pool_for_row(row: dict[str, Any], pool_ids: dict[str, str]) -> str:
    """A known agent's configured pool overrides historical row labels."""
    agent = str(row.get("agent", ""))
    if agent in pool_ids:
        return pool_ids[agent]
    for key in ("quota_pool_id", "pool_id", "shared_pool_id"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return agent


def _tokens_between(
    runs: Iterable[dict[str, Any]], pool_id: str, pool_ids: dict[str, str],
    after: dt.datetime, until: dt.datetime,
) -> float:
    total = 0.0
    for run in runs:
        if not isinstance(run, dict):
            continue
        if _pool_for_row(run, pool_ids) != pool_id:
            continue
        completed = _timestamp(run.get("completed_at"))
        usage = run.get("token_usage")
        tokens = _number(usage.get("total_tokens")) if isinstance(usage, dict) else None
        if completed is not None and after < completed <= until and tokens is not None and tokens > 0:
            total += tokens
    return total


def _pool_run_samples(
    runs: Iterable[dict[str, Any]], pool_id: str, pool_ids: dict[str, str],
    after: dt.datetime, until: dt.datetime,
) -> list[tuple[dt.datetime, float]]:
    samples: list[tuple[dt.datetime, float]] = []
    for run in runs:
        if not isinstance(run, dict) or _pool_for_row(run, pool_ids) != pool_id:
            continue
        completed = _timestamp(run.get("completed_at"))
        usage = run.get("token_usage")
        tokens = _number(usage.get("total_tokens")) if isinstance(usage, dict) else None
        if completed is not None and after < completed <= until and tokens is not None and tokens > 0:
            samples.append((completed, tokens))
    return sorted(samples)


def _brake_release_at(
    samples: list[tuple[dt.datetime, float]], *, now: dt.datetime,
    reset: dt.datetime, remaining: float, fraction_per_token: float,
    window_hours: int, burst_factor: float,
) -> dt.datetime:
    """Find the first continuous allowance crossing or token-expiry event."""
    window = dt.timedelta(hours=window_hours)
    tokens = sum(value for _, value in samples)
    cursor = now
    for completed, value in [*samples, (reset, 0.0)]:
        boundary = min(completed + window, reset) if value else reset
        if boundary <= cursor:
            tokens -= value
            continue
        consumption = tokens * fraction_per_token
        if consumption > 0:
            crossing = reset - dt.timedelta(
                hours=remaining * window_hours * burst_factor / consumption
            )
            if cursor < crossing <= boundary:
                return crossing
        if boundary == reset:
            return reset
        tokens -= value
        cursor = boundary
        if tokens * fraction_per_token <= (
            remaining / ((reset - cursor).total_seconds() / 3600.0)
            * window_hours * burst_factor
        ):
            return cursor
    return reset


def calculate_pool_brake(
    pool_id: str,
    *,
    snapshots: Iterable[dict[str, Any]],
    runs: Iterable[dict[str, Any]],
    pool_ids: dict[str, str],
    now: dt.datetime,
    config: dict[str, Any],
    plan_capacity_credits: float | None = None,
    short_window_hours: float | None = None,
) -> dict[str, Any]:
    """Recompute a reversible pool brake solely from readings and run tokens."""
    current = now.astimezone(UTC)
    window_hours = config["window_hours"]
    burst_factor = config["burst_factor"]
    result: dict[str, Any] = {
        "active": False, "until": None, "reason": "no-active-window-evidence",
        "allowed_fraction_window": None, "consumed_fraction_window": None,
        "window_hours": window_hours,
        "evaluated_at": current.isoformat().replace("+00:00", "Z"),
    }
    evidence = [row for row in snapshots if isinstance(row, dict)
                and _pool_for_row(row, pool_ids) == pool_id]
    run_rows = list(runs)
    pace = calculate_pool_pace(
        pool_id, snapshots=evidence, runs=run_rows, pool_ids=pool_ids,
        now=current, plan_capacity_credits=plan_capacity_credits,
        short_window_hours=short_window_hours,
    )
    kind = pace.get("window")
    reset = _timestamp(pace.get("reset_at"))
    if kind not in WINDOW_FIELDS or reset is None:
        resets = [_timestamp(row.get(field[1])) for row in evidence
                  for field in WINDOW_FIELDS.values()]
        if any(value is not None and value <= current for value in resets):
            result["reason"] = "reset-window-passed"
        return result
    if reset <= current:
        result["reason"] = "reset-window-passed"
        return result
    if kind == "short":
        matching_hours = [(_timestamp(row.get("observed_at")), _number(row.get("short_window_hours")))
                          for row in evidence if _timestamp(row.get("short_window_reset_at")) == reset]
        valid_hours = [(observed, hours) for observed, hours in matching_hours
                       if observed is not None and hours is not None and hours > 0]
        selected_hours = max(valid_hours)[1] if valid_hours else short_window_hours
    else:
        selected_hours = short_window_hours
    start = _window_start(kind, reset, selected_hours)
    if start is None:
        return result
    _, reset_field = WINDOW_FIELDS[kind]
    observations: dict[dt.datetime, float] = {}
    capacity = _number(plan_capacity_credits)
    for row in evidence:
        observed = _timestamp(row.get("observed_at"))
        own_reset = _timestamp(row.get(reset_field))
        fraction = _fraction(row, kind, capacity)
        if (observed is not None and start <= observed <= current and observed < reset
                and (own_reset is None or own_reset == reset) and fraction is not None):
            observations[observed] = fraction
    ordered = sorted(observations.items())
    if not ordered:
        return result
    latest_at, remaining = ordered[-1]
    age_hours = (current - latest_at).total_seconds() / 3600.0
    if age_hours > config["max_reading_age_hours"]:
        result["reason"] = "stale-reading"
        return result
    samples = _pool_run_samples(
        run_rows, pool_id, pool_ids, current - dt.timedelta(hours=window_hours), current
    )
    if not samples:
        result["reason"] = "no-ledger-tokens"
        return result
    fraction_per_token = None
    for (earlier_at, earlier_fraction), (later_at, later_fraction) in reversed(
        list(zip(ordered, ordered[1:]))
    ):
        drop = earlier_fraction - later_fraction
        between = _tokens_between(run_rows, pool_id, pool_ids, earlier_at, later_at)
        if drop > 0 and between > 0:
            fraction_per_token = drop / between
            break
    if fraction_per_token is None:
        result["reason"] = "missing-calibration"
        return result
    remaining = max(
        0.0, remaining - fraction_per_token
        * _tokens_between(run_rows, pool_id, pool_ids, latest_at, current)
    )
    hours_to_reset = (reset - current).total_seconds() / 3600.0
    allowed = remaining / hours_to_reset * window_hours * burst_factor
    consumed = sum(tokens for _, tokens in samples) * fraction_per_token
    result["allowed_fraction_window"] = round(allowed, 9)
    result["consumed_fraction_window"] = round(consumed, 9)
    result["active"] = consumed > allowed
    result["reason"] = (
        "window-consumption-exceeds-allowance" if result["active"] else "within-allowance"
    )
    if result["active"]:
        result["until"] = _brake_release_at(
            samples, now=current, reset=reset, remaining=remaining,
            fraction_per_token=fraction_per_token,
            window_hours=window_hours, burst_factor=burst_factor,
        ).isoformat().replace("+00:00", "Z")
    return result


def _pace_clock(
    pace: dict[str, Any], now: dt.datetime
) -> tuple[dt.datetime, dt.timedelta, float] | None:
    """(window start, window length, consumed fraction) behind a pace.

    A projected pace is measured at `now`, a direct reading at observed_at.
    Either way consumed = tempo x elapsed stays fixed while the pool is idle,
    so a measured tempo is a snapshot of that moment, not of `now`.
    """
    reset = _timestamp(pace.get("reset_at"))
    tempo = _number(pace.get("tempo"))
    elapsed = _number(pace.get("elapsed_fraction"))
    anchor = now if pace.get("source") == "fleet-projection" else _timestamp(pace.get("observed_at"))
    if (reset is None or anchor is None or tempo is None or elapsed is None
            or not 0.0 < elapsed < 1.0 or anchor >= reset):
        return None
    duration = (reset - anchor) / (1.0 - elapsed)
    return reset - duration, duration, tempo * elapsed


def _idle_tempo(pace: dict[str, Any], now: dt.datetime) -> float | None:
    """The tempo at `now` if the pool used nothing since the reading."""
    clock = _pace_clock(pace, now)
    if clock is None:
        return _number(pace.get("tempo"))
    start, duration, consumed = clock
    elapsed = (now - start) / duration
    return round(consumed / elapsed, 6) if elapsed > 0 else _number(pace.get("tempo"))


def _tempo_release_at(pace: dict[str, Any], throttle: float, now: dt.datetime) -> str | None:
    """Earliest time the tempo falls to `throttle` if the pool stays idle."""
    reset = _timestamp(pace.get("reset_at"))
    if reset is None:
        return None
    clock = _pace_clock(pace, now)
    if clock is None:
        return reset.isoformat().replace("+00:00", "Z")
    start, duration, consumed = clock
    release = start + duration * (consumed / throttle)
    return max(now, min(release, reset)).isoformat().replace("+00:00", "Z")


def apply_tempo_thresholds(
    brake: dict[str, Any],
    pace: dict[str, Any],
    config: dict[str, Any],
    *,
    now: dt.datetime,
    windows: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Owner WARN/throttle levels on the pool tempo; absent knobs change nothing.

    Tempo is the consumed fraction divided by the elapsed fraction of a
    window (1.0 is exactly on pace). WARN is advisory and never blocks.
    Throttle activates the same reversible brake as an allowance overrun:
    auto-routing skips the pool and an explicit run needs
    --override-pace-brake. An unknown tempo, or one whose confidence is below
    tempo_min_confidence (default 0.5), stays unknown and never brakes.

    Levels compare `tempo_now`, the tempo at evaluation time with no usage
    since the reading, not the tempo frozen at the reading: otherwise an
    idle pool would stay braked past its own `until`. A reading older than
    the brake's max_reading_age_hours is stale: it never brakes and never
    counts as clear, it is listed in `tempo_stale_windows`.

    `windows` are all active windows of the pool (calculate_pool_pace_windows);
    `pace` is the limiting one. Staleness is decided per window BEFORE any
    window is picked, so an old reading with a higher tempo cannot hide a
    fresh window over the throttle. The brake holds while any fresh window
    is at or over the throttle and releases when the last of them lets go.
    """
    warn, throttle = config.get("tempo_warn"), config.get("tempo_throttle")
    if warn is None and throttle is None:
        return brake
    current = now.astimezone(UTC)
    result = dict(brake, tempo=pace.get("tempo"), tempo_now=None, tempo_window=None,
                  tempo_warn=warn, tempo_throttle=throttle, tempo_level="unknown",
                  tempo_stale_windows=[])
    min_confidence = float(config.get("tempo_min_confidence", 0.5))
    max_age = _number(config.get("max_reading_age_hours"))
    fresh: list[tuple[float, dict[str, Any]]] = []
    stale: list[str] = []
    for window in [pace, *(windows or ())]:
        if not isinstance(window, dict):
            continue
        confidence = _number(window.get("confidence")) or 0.0
        if _number(window.get("tempo")) is None or confidence < min_confidence:
            continue
        observed = _timestamp(window.get("observed_at"))
        if (observed is not None and max_age is not None
                and (current - observed).total_seconds() / 3600.0 > max_age):
            kind = str(window.get("window") or "unknown")
            if kind not in stale:
                stale.append(kind)
            continue
        tempo_now = _idle_tempo(window, current)
        if tempo_now is not None:
            fresh.append((tempo_now, window))
    result["tempo_stale_windows"] = stale
    if not fresh:
        if stale:
            result["tempo_level"] = "stale"
        return result
    tempo, limiting = max(fresh, key=lambda item: item[0])
    result.update(tempo=limiting.get("tempo"), tempo_now=tempo,
                  tempo_window=limiting.get("window"))
    over = [window for value, window in fresh
            if throttle is not None and value >= throttle]
    if over:
        result["tempo_level"] = "throttle"
        releases = [_timestamp(_tempo_release_at(window, float(throttle), current))
                    for window in over]
        known = [value for value in releases if value is not None]
        release = (max(known).isoformat().replace("+00:00", "Z") if known else None)
        if not result.get("active"):
            result.update(active=True, reason="tempo-exceeds-throttle", until=release)
        else:
            # Both brakes hold: publish when the later of the two lets go.
            held, tempo_end = _timestamp(result.get("until")), _timestamp(release)
            if held is not None and tempo_end is not None and tempo_end > held:
                result["until"] = release
    elif warn is not None and tempo >= warn:
        result["tempo_level"] = "warn"
    else:
        result["tempo_level"] = "within-pace"
    return result


def calculate_pool_pace(
    pool_id: str,
    *,
    snapshots: Iterable[dict[str, Any]],
    runs: Iterable[dict[str, Any]],
    pool_ids: dict[str, str],
    now: dt.datetime,
    plan_capacity_credits: float | None = None,
    short_window_hours: float | None = None,
) -> dict[str, Any]:
    """Return the limiting active window's pace, or neutral unknown evidence.

    A snapshot without a reset joins only a known window containing its observation.
    No rate is inferred from credits or Fleet tokens without a matching window.
    """
    candidates = calculate_pool_pace_windows(
        pool_id, snapshots=snapshots, runs=runs, pool_ids=pool_ids, now=now,
        plan_capacity_credits=plan_capacity_credits, short_window_hours=short_window_hours,
    )
    if not candidates:
        return {"tempo": None, "confidence": 0.0, "source": "none", "reason": "no-active-window-evidence"}
    return max(candidates, key=lambda item: (item["tempo"], item["confidence"]))


def calculate_pool_pace_windows(
    pool_id: str,
    *,
    snapshots: Iterable[dict[str, Any]],
    runs: Iterable[dict[str, Any]],
    pool_ids: dict[str, str],
    now: dt.datetime,
    plan_capacity_credits: float | None = None,
    short_window_hours: float | None = None,
) -> list[dict[str, Any]]:
    """Every active window's pace for the pool, regardless of reading age.

    calculate_pool_pace keeps only the highest tempo; a threshold check must
    see every window so that it can drop stale readings before comparing.
    """
    current = now.astimezone(UTC)
    capacity = _number(plan_capacity_credits)
    short_hours = _number(short_window_hours)
    evidence = [
        row for row in snapshots
        if isinstance(row, dict)
        and _pool_for_row(row, pool_ids) == pool_id
    ]
    # A snapshot row that measured the short window itself (e.g. the Codex
    # rollout collector or the Kimi native usage windows) declares its own
    # window length; it beats the profile-level short_window_hours fallback.
    # When several rows disagree on the length of the same window (same
    # reset_at), the newest observation wins - an older row's length must not
    # move the window start away from the latest sample.
    short_hours_by_reset: dict[dt.datetime, tuple[dt.datetime, float]] = {}
    for row in evidence:
        row_reset = _timestamp(row.get("short_window_reset_at"))
        row_hours = _number(row.get("short_window_hours"))
        if row_reset is None or row_hours is None or row_hours <= 0:
            continue
        row_observed = _timestamp(row.get("observed_at"))
        if row_observed is None:
            row_observed = dt.datetime.min.replace(tzinfo=UTC)
        current_pick = short_hours_by_reset.get(row_reset)
        if current_pick is None or row_observed >= current_pick[0]:
            short_hours_by_reset[row_reset] = (row_observed, row_hours)
    run_rows = list(runs)
    candidates: list[dict[str, Any]] = []
    for kind, (_, reset_field) in WINDOW_FIELDS.items():
        anchors: set[dt.datetime] = set()
        for row in evidence:
            reset = _timestamp(row.get(reset_field))
            if reset is not None:
                anchors.add(reset)
        for reset in anchors:
            if kind == "short":
                picked = short_hours_by_reset.get(reset)
                hours = picked[1] if picked is not None else short_hours
            else:
                hours = short_hours
            start = _window_start(kind, reset, hours)
            if start is None or not start <= current < reset:
                continue
            observations: list[tuple[dt.datetime, float, dict[str, Any]]] = []
            for row in evidence:
                observed = _timestamp(row.get("observed_at"))
                own_reset = _timestamp(row.get(reset_field))
                fraction = _fraction(row, kind, capacity)
                if (
                    observed is not None and start <= observed <= current and observed < reset
                    and (own_reset is None or own_reset == reset)
                    and fraction is not None
                ):
                    observations.append((observed, fraction, row))
            if not observations:
                continue
            observations.sort(key=lambda item: (item[0], item[2].get(reset_field) is not None))
            observed, fraction, latest = observations[-1]
            duration = (reset - start).total_seconds()
            elapsed_at_observation = (observed - start).total_seconds() / duration
            if elapsed_at_observation <= 0:
                continue
            age_hours = max(0.0, (current - observed).total_seconds() / 3600.0)
            age_confidence = 1.0 if age_hours <= 12 else max(0.0, (72.0 - age_hours) / 60.0)
            projected = False
            prior = next((item for item in reversed(observations[:-1]) if item[0] < observed), None)
            if prior is not None:
                earlier, prior_fraction, _ = prior
                drop = prior_fraction - fraction
                tokens_between = _tokens_between(run_rows, pool_id, pool_ids, earlier, observed)
                if drop > 0 and tokens_between > 0:
                    later_tokens = _tokens_between(run_rows, pool_id, pool_ids, observed, current)
                    if later_tokens > 0:
                        fraction = max(0.0, fraction - drop / tokens_between * later_tokens)
                        projected = True
            elapsed = (current - start).total_seconds() / duration if projected else elapsed_at_observation
            confidence = 0.8 if projected else age_confidence
            confidence *= min(1.0, elapsed / 0.05)
            candidates.append({
                "tempo": round((1.0 - fraction) / elapsed, 6),
                "confidence": round(confidence, 6),
                "source": "fleet-projection" if projected else str(latest.get("source") or "quota-snapshot"),
                "reason": f"{kind}-{'projected-from-fleet-tokens' if projected else 'measured'}",
                "window": kind,
                "observed_at": latest.get("observed_at"),
                "reset_at": reset.isoformat().replace("+00:00", "Z"),
                "remaining_fraction": round(fraction, 6),
                "elapsed_fraction": round(elapsed, 6),
            })
    return candidates
