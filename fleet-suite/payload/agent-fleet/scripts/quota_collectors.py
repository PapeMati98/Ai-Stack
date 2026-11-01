from __future__ import annotations

import datetime as dt
import hashlib
import heapq
import json
import math
import os
from pathlib import Path
import sys
from typing import Any, Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import quota_windows  # noqa: E402


class QuotaCollectorError(ValueError):
    pass


def _utc_timestamp(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> dt.datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise QuotaCollectorError(f"Invalid quota timestamp: {value}") from exc
    if parsed.tzinfo is None:
        raise QuotaCollectorError("Quota timestamps must include a timezone")
    return parsed.astimezone(dt.timezone.utc)


def fixed_window(
    *,
    anchor: dt.datetime,
    window_seconds: int,
    now: dt.datetime,
) -> tuple[dt.datetime, dt.datetime]:
    if window_seconds < 60:
        raise QuotaCollectorError("Quota window_seconds must be at least 60")
    anchor_utc = anchor.astimezone(dt.timezone.utc)
    now_utc = now.astimezone(dt.timezone.utc)
    offset = math.floor((now_utc - anchor_utc).total_seconds() / window_seconds)
    start = anchor_utc + dt.timedelta(seconds=offset * window_seconds)
    return start, start + dt.timedelta(seconds=window_seconds)


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise QuotaCollectorError(f"{label} must be a positive integer")
    return value


_INFERRED_BAILIAN_POOL = "bailian-shared-flat-rate"


def _profile_inferred_bailian_ledger(
    profile: dict[str, Any] | None,
) -> tuple[str, str] | None:
    """Pool and capacity source when this profile config is the inferred ledger."""
    if not isinstance(profile, dict):
        return None
    pool = str(profile.get("quota_pool_id", "")).strip()
    config = profile.get("quota_collection")
    if pool != _INFERRED_BAILIAN_POOL or not isinstance(config, dict):
        return None
    if str(config.get("collector_mode", "")).strip() != "local-token-ledger":
        return None
    capacity = config.get("capacity_source")
    if not isinstance(capacity, str) or not capacity.strip().startswith("inferred-"):
        return None
    return pool, capacity.strip()


def _preserved_estimate(row: dict[str, Any], estimated_key: str, actual_key: str) -> Any:
    if estimated_key in row:
        return row.get(estimated_key)
    return row.get(actual_key)


def normalize_inferred_bailian_ledger(
    row: dict[str, Any],
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a copy where every local token ledger is advisory, not a balance.

    used_tokens and capacity_tokens stay. Prior remaining, fraction, and reset
    move under estimated_* fields. Real remaining, headroom, weekly fraction,
    and weekly reset stay unknown. Positive estimates are unverified too.
    A matching inferred-ledger profile
    supplies pool and capacity_source when a legacy row omits them. Estimated
    fields already present are kept on a later pass.
    """
    updated = dict(row)
    if str(updated.get("source", "")).strip() != "local-token-ledger":
        return updated
    # Every local ledger is an estimate, including legacy rows with missing
    # inferred-capacity tags and rows claiming authoritative metadata.
    borrowed = _profile_inferred_bailian_ledger(profile)
    if borrowed:
        updated.setdefault("quota_pool_id", borrowed[0])
        updated.setdefault("capacity_source", borrowed[1])
    updated["estimated_remaining_tokens"] = _preserved_estimate(
        updated, "estimated_remaining_tokens", "remaining_tokens"
    )
    updated["estimated_weekly_remaining_fraction"] = _preserved_estimate(
        updated, "estimated_weekly_remaining_fraction", "weekly_remaining_fraction"
    )
    updated["estimated_weekly_reset_at"] = _preserved_estimate(
        updated, "estimated_weekly_reset_at", "weekly_reset_at"
    )
    updated["remaining_tokens"] = None
    for key in ('reported_headroom', 'headroom', 'short_window_remaining_fraction',
                'monthly_remaining_fraction', 'credits_remaining'):
        if key in updated:
            updated.setdefault('estimated_' + key, updated[key])
            updated[key] = None
    updated["headroom"] = None
    updated["weekly_remaining_fraction"] = None
    updated["weekly_reset_at"] = None
    updated["status"] = "unknown"
    updated["authority"] = "advisory"
    updated["hard_blocking"] = False
    updated["availability_blocked"] = False
    return updated


def collect_local_token_ledger(
    config: dict[str, Any],
    *,
    pool_id: str,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    directory_value = config.get("ledger_dir")
    if not isinstance(directory_value, str) or not directory_value.strip():
        raise QuotaCollectorError("local-token-ledger requires ledger_dir")
    directory = Path(os.path.expandvars(directory_value)).expanduser()
    if not directory.is_dir():
        raise QuotaCollectorError(f"Quota ledger directory is unavailable: {directory}")

    pattern = config.get("ledger_glob", "token-usage-*.jsonl")
    if (
        not isinstance(pattern, str)
        or not pattern.strip()
        or Path(pattern).name != pattern
    ):
        raise QuotaCollectorError("ledger_glob must be a filename-only pattern")
    files = sorted(path for path in directory.glob(pattern) if path.is_file())
    max_files = _positive_int(config.get("max_files", 256), "max_files")
    if not files:
        raise QuotaCollectorError("Quota ledger has no matching JSONL files")
    if len(files) > max_files:
        raise QuotaCollectorError(
            f"Quota ledger file limit exceeded: {len(files)}>{max_files}"
        )

    capacity = _positive_int(
        config.get("window_capacity_tokens"), "window_capacity_tokens"
    )
    window_seconds = _positive_int(config.get("window_seconds"), "window_seconds")
    anchor_value = config.get("window_reset_anchor")
    if not isinstance(anchor_value, str):
        raise QuotaCollectorError("local-token-ledger requires window_reset_anchor")
    anchor = _parse_timestamp(anchor_value)
    current = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    window_start, window_reset = fixed_window(
        anchor=anchor,
        window_seconds=window_seconds,
        now=current,
    )

    max_records = _positive_int(config.get("max_records", 2_000_000), "max_records")
    parsed_records = 0
    invalid_records = 0
    duplicate_records = 0
    used_tokens = 0
    seen_ids: set[str] = set()
    for path in files:
        try:
            handle = path.open("r", encoding="utf-8", errors="replace")
        except OSError as exc:
            raise QuotaCollectorError(f"Cannot read quota ledger: {path}") from exc
        with handle:
            for line in handle:
                if not line.strip():
                    continue
                if parsed_records + invalid_records + duplicate_records >= max_records:
                    raise QuotaCollectorError(
                        f"Quota ledger record limit exceeded: {max_records}"
                    )
                try:
                    row = json.loads(line)
                    timestamp = _parse_timestamp(str(row["timestamp"]))
                    total_tokens = row["totalTokens"]
                    if (
                        isinstance(total_tokens, bool)
                        or not isinstance(total_tokens, (int, float))
                        or total_tokens < 0
                    ):
                        raise ValueError("invalid totalTokens")
                    record_id = row.get("id")
                except (KeyError, TypeError, ValueError, json.JSONDecodeError, QuotaCollectorError):
                    invalid_records += 1
                    continue
                if isinstance(record_id, str) and record_id:
                    if record_id in seen_ids:
                        duplicate_records += 1
                        continue
                    seen_ids.add(record_id)
                parsed_records += 1
                if window_start <= timestamp < window_reset:
                    used_tokens += int(total_tokens)

    if parsed_records == 0:
        raise QuotaCollectorError("Quota ledger has no valid usage records")
    remaining_tokens = max(0, capacity - used_tokens)
    remaining_fraction = remaining_tokens / capacity
    confidence = config.get("confidence", 0.7)
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0.0 <= float(confidence) <= 1.0
    ):
        raise QuotaCollectorError("Quota collector confidence must be between 0 and 1")
    capacity_source = str(config.get("capacity_source", "unspecified")).strip()
    observed_at = _utc_timestamp(current)
    fingerprint_input = "|".join(
        (
            pool_id,
            observed_at,
            str(used_tokens),
            str(capacity),
            _utc_timestamp(window_start),
        )
    )
    return normalize_inferred_bailian_ledger({
        "schema_version": "live-1",
        "snapshot_id": "live-" + hashlib.sha256(
            fingerprint_input.encode("utf-8")
        ).hexdigest()[:20],
        "agent": None,
        "quota_pool_id": pool_id,
        "observed_at": observed_at,
        "source": "local-token-ledger",
        "confidence": round(float(confidence), 3),
        "short_window_remaining_fraction": None,
        "short_window_reset_at": None,
        "weekly_remaining_fraction": round(remaining_fraction, 9),
        "weekly_reset_at": _utc_timestamp(window_reset),
        "status": "fresh",
        "age_hours": 0.0,
        "headroom": round(remaining_fraction, 9),
        "used_tokens": used_tokens,
        "remaining_tokens": remaining_tokens,
        "capacity_tokens": capacity,
        "window_start_at": _utc_timestamp(window_start),
        "ledger_file_count": len(files),
        "parsed_records": parsed_records,
        "invalid_records": invalid_records,
        "duplicate_records": duplicate_records,
        "capacity_source": capacity_source,
        "note": (
            "Read-only local Qwen token ledger; shared Bailian pool capacity is "
            f"{capacity_source}."
        ),
    })


def collect_local_unmetered(
    config: dict[str, Any],
    *,
    pool_id: str,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    confidence = config.get("confidence", 1.0)
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0.0 <= float(confidence) <= 1.0
    ):
        raise QuotaCollectorError("Local unmetered confidence must be between 0 and 1")
    current = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    observed_at = _utc_timestamp(current)
    snapshot_id = "local-" + hashlib.sha256(
        f"{pool_id}|{observed_at}".encode("utf-8")
    ).hexdigest()[:20]
    return {
        "schema_version": "live-1",
        "snapshot_id": snapshot_id,
        "agent": None,
        "quota_pool_id": pool_id,
        "observed_at": observed_at,
        "source": "local-unmetered",
        "confidence": round(float(confidence), 3),
        "short_window_remaining_fraction": 1.0,
        "short_window_reset_at": None,
        "weekly_remaining_fraction": 1.0,
        "weekly_reset_at": None,
        "status": "fresh",
        "age_hours": 0.0,
        "headroom": 1.0,
        "used_tokens": None,
        "remaining_tokens": None,
        "capacity_tokens": None,
        "provider_cost_pln": 0.0,
        "provider_cost_display": "0 zł",
        "note": (
            "Local inference has no provider token quota; electricity and hardware "
            "costs are outside Agent Fleet telemetry."
        ),
    }


def _fraction_in_window(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0.0 <= float(value) <= 100.0
    ):
        raise QuotaCollectorError(f"{label} must be a number between 0 and 100")
    return float(value)


def collect_claude_native_usage(
    config: dict[str, Any],
    *,
    pool_id: str,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Read the Claude desktop native usage cache as an authoritative snapshot.

    The source file is the local `plan-usage-history.json` written by the Claude
    desktop app about every five minutes. `observed_at` is always the timestamp
    of the newest valid cached sample, never the read time, so a stale cache is
    reported as stale instead of being presented as fresh usage.
    """
    path_value = config.get("usage_history_path")
    if not isinstance(path_value, str) or not path_value.strip():
        raise QuotaCollectorError("claude-native-usage requires usage_history_path")
    path = Path(os.path.expandvars(path_value)).expanduser()
    if not path.is_file():
        raise QuotaCollectorError(
            f"Claude native usage history is unavailable: {path}"
        )
    max_bytes = _positive_int(config.get("max_bytes", 8_388_608), "max_bytes")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise QuotaCollectorError(
            f"Cannot stat Claude native usage history: {path}"
        ) from exc
    if size > max_bytes:
        raise QuotaCollectorError(
            f"Claude native usage history exceeds {max_bytes} bytes"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QuotaCollectorError(
            f"Claude native usage history is not valid JSON: {path}"
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("samples"), list):
        raise QuotaCollectorError(
            "Claude native usage history has no samples array"
        )
    current = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    latest: tuple[dt.datetime, float, float] | None = None
    for sample in payload["samples"]:
        if not isinstance(sample, dict):
            continue
        raw_t = sample.get("t")
        usage = sample.get("u")
        if (
            isinstance(raw_t, bool)
            or not isinstance(raw_t, (int, float))
            or raw_t <= 0
            or not isinstance(usage, dict)
        ):
            continue
        try:
            observed = dt.datetime.fromtimestamp(
                float(raw_t) / 1000.0, tz=dt.timezone.utc
            )
            five_hour = _fraction_in_window(usage.get("fh"), "sample.fh")
            seven_day = _fraction_in_window(usage.get("sd"), "sample.sd")
        except (OverflowError, OSError, ValueError, QuotaCollectorError):
            continue
        if latest is None or observed > latest[0]:
            latest = (observed, five_hour, seven_day)
    if latest is None:
        raise QuotaCollectorError(
            "Claude native usage history has no valid usage samples"
        )
    observed, five_hour_used, seven_day_used = latest
    if observed > current + dt.timedelta(seconds=300):
        raise QuotaCollectorError(
            "Claude native usage history has a future sample timestamp"
        )
    age_seconds = max(0.0, (current - observed).total_seconds())
    max_sample_age_seconds = _positive_int(
        config.get("max_sample_age_seconds", 3600), "max_sample_age_seconds"
    )
    fresh = age_seconds <= max_sample_age_seconds
    short_remaining = round(1.0 - five_hour_used / 100.0, 9)
    weekly_remaining = round(1.0 - seven_day_used / 100.0, 9)
    confidence = config.get("confidence", 0.9)
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0.0 <= float(confidence) <= 1.0
    ):
        raise QuotaCollectorError("Quota collector confidence must be between 0 and 1")
    observed_at = _utc_timestamp(observed)
    fingerprint_input = "|".join(
        (
            pool_id,
            observed_at,
            str(five_hour_used),
            str(seven_day_used),
        )
    )
    headroom = min(short_remaining, weekly_remaining)
    return {
        "schema_version": "live-1",
        "snapshot_id": "claude-native-" + hashlib.sha256(
            fingerprint_input.encode("utf-8")
        ).hexdigest()[:20],
        "agent": None,
        "quota_pool_id": pool_id,
        "observed_at": observed_at,
        "source": "native-json",
        "confidence": round(float(confidence), 3),
        "short_window_remaining_fraction": short_remaining,
        "short_window_reset_at": None,
        "weekly_remaining_fraction": weekly_remaining,
        "weekly_reset_at": None,
        "status": "fresh" if fresh else "stale",
        "age_hours": round(age_seconds / 3600.0, 3),
        "headroom": headroom if fresh else None,
        "reported_headroom": headroom,
        "cache_read": True,
        "sample_age_seconds": round(age_seconds, 3),
        "max_sample_age_seconds": max_sample_age_seconds,
        "note": (
            "Read-only Claude desktop native usage cache; observed_at is the "
            "cached sample time, not the read time. The cache reports limit "
            "usage, not the billing source."
        ),
    }


def _codex_window_kind(window_minutes: float) -> str | None:
    return quota_windows.window_kind_from_minutes(window_minutes)


def _codex_rate_limit_sample(
    event: dict[str, Any],
) -> tuple[dt.datetime, str | None, dict[str, tuple[float, dt.datetime, float]]] | None:
    """Extract (observed_at, plan_type, windows) from one rollout event.

    Only counters, timestamps and plan_type leave this function; line content,
    session file paths and credit balances never do.
    """
    raw_timestamp = event.get("timestamp")
    if not isinstance(raw_timestamp, str):
        return None
    try:
        observed = _parse_timestamp(raw_timestamp)
    except QuotaCollectorError:
        return None
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    # Only token_count events carry an authoritative rate_limits block; a
    # foreign event (e.g. response_item) with a lookalike block is not a
    # quota sample.
    if payload.get("type") != "token_count":
        return None
    info = payload.get("info")
    if not isinstance(info, dict):
        info = {}
    rate_limits = payload.get("rate_limits")
    if not isinstance(rate_limits, dict):
        rate_limits = info.get("rate_limits")
    if not isinstance(rate_limits, dict):
        return None
    plan_type = rate_limits.get("plan_type")
    if not isinstance(plan_type, str) or not plan_type.strip():
        plan_type = info.get("plan_type")
    if not isinstance(plan_type, str) or not plan_type.strip():
        plan_type = payload.get("plan_type")
    plan = plan_type.strip() if isinstance(plan_type, str) and plan_type.strip() else None
    windows: dict[str, tuple[float, dt.datetime, float]] = {}
    for key in ("primary", "secondary"):
        window = rate_limits.get(key)
        if not isinstance(window, dict):
            continue
        used_raw = window.get("used_percent")
        if (
            isinstance(used_raw, bool)
            or not isinstance(used_raw, (int, float))
            or not math.isfinite(float(used_raw))
            or float(used_raw) < 0
        ):
            continue
        used_percent = float(used_raw)
        minutes = window.get("window_minutes")
        if (
            isinstance(minutes, bool)
            or not isinstance(minutes, (int, float))
            or not math.isfinite(float(minutes))
        ):
            continue
        kind = _codex_window_kind(float(minutes))
        if kind is None or kind in windows:
            continue
        reset, _ = quota_windows.parse_reset(window.get("resets_at"), observed_at=observed)
        relative = window.get("resets_in_seconds")
        if reset is None and not isinstance(relative, bool) and isinstance(relative, (int, float)) and math.isfinite(relative) and relative >= 0:
            try:
                reset = observed + dt.timedelta(seconds=relative)
            except OverflowError:
                pass
        # A known counter does not disappear because its recovery date is
        # malformed/missing. In particular zero capacity must remain blocking.
        remaining = round(min(1.0, max(0.0, 1.0 - used_percent / 100.0)), 9)
        windows[kind] = (remaining, reset, round(float(minutes) / 60.0, 6))
    if not windows:
        return None
    return observed, plan, windows


def _newest_rollout_files(
    directory: Path,
    *,
    max_files: int,
    max_entries: int,
) -> list[tuple[float, str]]:
    """Newest rollout-*.jsonl files by mtime, newest first.

    The collector runs on every routing decision, so the walk is bounded in
    memory and I/O: entries are streamed out of os.scandir (a directory
    listing is never materialized or sorted as a whole), a size-capped heap
    keeps only max_files candidates in memory, and max_entries caps how many
    directory entries are visited overall. The budget is checked per entry,
    including inside a single directory, so one oversized directory is never
    scanned whole: within it selection is by mtime among the visited entries
    only, and an over-budget flat directory can miss unvisited files (the
    default budget of 2048 entries is far above a real Codex day partition).
    Codex names session partitions with ISO timestamps (YYYY/MM/DD), so
    sibling directories are explored newest-name-first - only their names
    are sorted, their count per directory is bounded by the same entry
    budget, and directories are few by construction - and the budget
    truncates the oldest partitions, not the newest files.
    Paths stay inside this function; only the files returned here are ever
    opened. An OSError on the top-level directory propagates; unreadable
    subdirectories are skipped.
    """
    heap: list[tuple[float, str]] = []
    visited = 0
    stack: list[Path] = [directory]
    top_level = True
    while stack:
        if visited >= max_entries:
            break
        current_dir = stack.pop()
        try:
            listing = os.scandir(current_dir)
        except OSError:
            if top_level:
                raise
            top_level = False
            continue
        subdirectories: list[str] = []
        with listing:
            for entry in listing:
                if visited >= max_entries:
                    # Per-entry budget check: a single huge directory stops
                    # at the same declared cap instead of being read whole.
                    break
                visited += 1
                try:
                    if entry.is_dir(follow_symlinks=False):
                        subdirectories.append(entry.path)
                        continue
                    if not (
                        entry.name.startswith("rollout-")
                        and entry.name.endswith(".jsonl")
                    ):
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    mtime = entry.stat(follow_symlinks=False).st_mtime
                except OSError:
                    continue
                candidate = (mtime, entry.path)
                if len(heap) < max_files:
                    heapq.heappush(heap, candidate)
                elif candidate > heap[0]:
                    heapq.heapreplace(heap, candidate)
        # LIFO stack: append oldest-name-first so the newest partition
        # directory (ISO names sort chronologically) is explored next.
        stack.extend(sorted(subdirectories))
        top_level = False
    heap.sort(reverse=True)
    return heap


def collect_codex_rollout_rate_limits(
    config: dict[str, Any],
    *,
    pool_id: str,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Read the newest Codex rollout rate_limits sample as a live snapshot.

    Codex CLI session rollouts carry token_count events with a rate_limits
    block (primary/secondary windows: used_percent, window_minutes, resets_at
    or resets_in_seconds). Only the newest sample by event timestamp is used.
    A missing directory, missing files or missing samples yield an "unknown"
    row with a reason, never an exception.
    """
    current = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    directory_value = config.get(
        "sessions_dir", "%USERPROFILE%\\.codex\\sessions"
    )
    if not isinstance(directory_value, str) or not directory_value.strip():
        directory_value = "%USERPROFILE%\\.codex\\sessions"
    directory = Path(os.path.expandvars(directory_value)).expanduser()
    confidence = config.get("confidence", 0.9)
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0.0 <= float(confidence) <= 1.0
    ):
        raise QuotaCollectorError("Quota collector confidence must be between 0 and 1")
    max_sample_age_seconds = _positive_int(
        config.get("max_sample_age_seconds", 3600), "max_sample_age_seconds"
    )

    def unknown(reason: str) -> dict[str, Any]:
        observed_at = _utc_timestamp(current)
        snapshot_id = "codex-rollout-" + hashlib.sha256(
            f"{pool_id}|{observed_at}|{reason}".encode("utf-8")
        ).hexdigest()[:20]
        return {
            "schema_version": "live-1",
            "snapshot_id": snapshot_id,
            "agent": None,
            "quota_pool_id": pool_id,
            "observed_at": observed_at,
            "source": "codex-rollout-rate-limits",
            "confidence": round(float(confidence), 3),
            "short_window_remaining_fraction": None,
            "short_window_reset_at": None,
            "short_window_hours": None,
            "weekly_remaining_fraction": None,
            "weekly_reset_at": None,
            "monthly_remaining_fraction": None,
            "monthly_reset_at": None,
            "status": "unknown",
            "reason": reason,
            "age_hours": None,
            "headroom": None,
            "max_sample_age_seconds": max_sample_age_seconds,
            "note": (
                "Read-only Codex rollout rate-limit collector; only counters, "
                "timestamps and plan_type leave the session files."
            ),
        }

    if not directory.is_dir():
        return unknown("sessions-dir-unavailable")
    max_files = _positive_int(config.get("max_files", 8), "max_files")
    max_bytes = _positive_int(
        config.get("max_bytes_per_file", 262_144), "max_bytes_per_file"
    )
    max_scan_entries = _positive_int(
        config.get("max_scan_entries", 2048), "max_scan_entries"
    )
    try:
        candidates = _newest_rollout_files(
            directory, max_files=max_files, max_entries=max_scan_entries
        )
    except OSError:
        return unknown("sessions-dir-unreadable")
    if not candidates:
        return unknown("no-rollout-files")

    latest: tuple[
        dt.datetime, str | None, dict[str, tuple[float, dt.datetime, float]]
    ] | None = None
    for _, path in candidates:
        try:
            with Path(path).open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - max_bytes))
                raw = handle.read(max_bytes)
        except OSError:
            continue
        for line in raw.decode("utf-8", errors="replace").splitlines():
            if '"rate_limits"' not in line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            sample = _codex_rate_limit_sample(event)
            if sample is None:
                continue
            if latest is None or sample[0] > latest[0]:
                latest = sample
    if latest is None:
        return unknown("no-rate-limits-samples")

    observed, plan_type, windows = latest
    if observed > current + dt.timedelta(seconds=300):
        return unknown("future-sample-timestamp")
    active = {
        kind: value for kind, value in windows.items() if value[1] is None or value[1] > current
    }
    age_seconds = max(0.0, (current - observed).total_seconds())
    fresh = age_seconds <= max_sample_age_seconds and bool(active)
    reported_headroom = (
        round(min(value[0] for value in active.values()), 9) if active else None
    )
    # Only a fresh sample publishes window fields. A stale row keeps its
    # timestamps and reported_headroom for diagnostics but carries no
    # fractions or resets, so quota_pace and routing penalties never run on
    # evidence the age ceiling already rejected.
    emitted = active if fresh else {}
    observed_at = _utc_timestamp(observed)
    fingerprint_input = "|".join(
        (
            pool_id,
            observed_at,
            *(f"{kind}:{emitted[kind][0]}" for kind in sorted(emitted)),
        )
    )
    row = unknown("no-active-window")
    row.update(
        {
            "snapshot_id": "codex-rollout-" + hashlib.sha256(
                fingerprint_input.encode("utf-8")
            ).hexdigest()[:20],
            "observed_at": observed_at,
            "status": "fresh" if fresh else "stale",
            "age_hours": round(age_seconds / 3600.0, 3),
            "headroom": reported_headroom if fresh else None,
            "reported_headroom": reported_headroom,
            "plan_type": plan_type,
            "sample_age_seconds": round(age_seconds, 3),
        }
    )
    row.pop("reason", None)
    for kind, (remaining, reset, hours) in emitted.items():
        row[f"{kind}_remaining_fraction" if kind != "short" else "short_window_remaining_fraction"] = remaining
        row[f"{kind}_reset_at" if kind != "short" else "short_window_reset_at"] = _utc_timestamp(reset) if reset is not None else None
        if kind == "short":
            row["short_window_hours"] = hours
    return row


# Collector modes that produce a machine-readable snapshot without any manual
# or TUI step. Everything else is advisory-only evidence for routing.
MACHINE_READABLE_COLLECTOR_MODES = {
    "local-token-ledger",
    "local-unmetered",
    "claude-native-usage",
    "codex-rollout-rate-limits",
}


def collector_mode_blocker(collector_mode: str) -> str | None:
    """Exact routing blocker for pools without a machine-readable source."""
    if collector_mode in MACHINE_READABLE_COLLECTOR_MODES:
        return None
    if collector_mode == "tui-assisted":
        return (
            "tui-only: quota evidence requires a manual TUI /usage snapshot; "
            "TUI-assisted snapshots are advisory and never current"
        )
    if collector_mode == "provider-dashboard-manual":
        return (
            "manual-only: quota evidence requires a manual provider-dashboard "
            "quota-record; no machine-readable refresh exists"
        )
    if not collector_mode:
        return "no-source: profile declares no quota_collection source"
    return f"no-source: unsupported collector_mode {collector_mode!r}"


def collect_profile_quotas(
    profiles: dict[str, dict[str, Any]],
    *,
    agent_ids: Iterable[str] | None = None,
    now: dt.datetime | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    targets = set(agent_ids or profiles)
    unknown = sorted(targets - set(profiles))
    if unknown:
        raise QuotaCollectorError("Unknown quota profiles: " + ", ".join(unknown))
    target_pools = {
        str(profiles[agent].get("quota_pool_id", agent)) for agent in targets
    }
    pool_rows: dict[str, dict[str, Any]] = {}
    diagnostics: dict[str, dict[str, Any]] = {}
    for agent, profile in profiles.items():
        pool_id = str(profile.get("quota_pool_id", agent))
        if pool_id not in target_pools or pool_id in pool_rows:
            continue
        config = profile.get("quota_collection")
        collector_mode = (
            str(config.get("collector_mode", "")).strip()
            if isinstance(config, dict)
            else ""
        )
        blocker = collector_mode_blocker(collector_mode)
        if blocker is not None:
            diagnostics.setdefault(
                pool_id,
                {
                    "status": "NOT_MACHINE_READABLE",
                    "collector_mode": collector_mode or None,
                    "blocker": blocker,
                },
            )
            continue
        try:
            if collector_mode == "local-token-ledger":
                row = collect_local_token_ledger(config, pool_id=pool_id, now=now)
            elif collector_mode == "claude-native-usage":
                row = collect_claude_native_usage(config, pool_id=pool_id, now=now)
            elif collector_mode == "codex-rollout-rate-limits":
                row = collect_codex_rollout_rate_limits(
                    config, pool_id=pool_id, now=now
                )
            else:
                row = collect_local_unmetered(config, pool_id=pool_id, now=now)
        except (OSError, OverflowError, QuotaCollectorError) as exc:
            diagnostics[pool_id] = {
                "status": "ERROR",
                "collector_mode": collector_mode,
                "error": str(exc),
                "blocker": f"collector-error: {exc}",
            }
            continue
        row["agent"] = agent
        pool_rows[pool_id] = row
        diagnostics[pool_id] = {
            "status": {
                "fresh": "FRESH",
                "stale": "STALE",
                "unknown": "UNKNOWN",
            }.get(str(row["status"]), "STALE"),
            "collector_mode": collector_mode,
            "snapshot_id": row["snapshot_id"],
            "observed_at": row["observed_at"],
            "age_hours": row["age_hours"],
            "headroom": row["headroom"],
            # Not every collector counts provider tokens; the native Claude
            # usage cache reports window fractions only, so token fields are
            # optional in collector results.
            "used_tokens": row.get("used_tokens"),
            "remaining_tokens": row.get("remaining_tokens"),
            **(
                {
                    "blocker": (
                        f"stale-cache: newest cached sample observed_at="
                        f"{row['observed_at']} age_hours={row['age_hours']} exceeds "
                        f"max_sample_age_seconds={row.get('max_sample_age_seconds')}"
                    )
                }
                if row["status"] == "stale"
                else {}
            ),
            **(
                {
                    "blocker": (
                        f"unknown: {row.get('reason', 'no usable quota sample')}"
                    )
                }
                if row["status"] == "unknown"
                else {}
            ),
        }

    result: dict[str, dict[str, Any]] = {}
    for agent in targets:
        pool_id = str(profiles[agent].get("quota_pool_id", agent))
        if pool_id in pool_rows:
            result[agent] = {**pool_rows[pool_id], "effective_agent": agent}
    return result, diagnostics
