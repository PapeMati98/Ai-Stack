"""Original synthetic delivery-evidence library.

Synthetic-only helpers with no network, provider, Office, or Hub access,
no subprocesses, no environment/account access, and no file side effects.
Standard library only. Inputs are never mutated.
"""
from collections.abc import Mapping

_STATUSES = ("COMPLETED", "FAILED", "PARTIAL", "CANCELLED", "UNKNOWN")
_QUALITIES = ("ACCEPT", "REPAIR", "unknown")
_TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "total_tokens",
)


def _is_bool(value):
    return isinstance(value, bool)


def _check_token_value(value):
    if value is None:
        return None
    if _is_bool(value) or not isinstance(value, int) or value < 0:
        raise ValueError("invalid token value")
    return value


def _check_duration_value(value):
    if value is None:
        return None
    if _is_bool(value) or not isinstance(value, (int, float)):
        raise ValueError("invalid duration_seconds")
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError("invalid duration_seconds")
    if value < 0:
        raise ValueError("invalid duration_seconds")
    return value


def normalize_run(record):
    """Normalize one synthetic run record without mutating it."""
    if not isinstance(record, Mapping):
        raise ValueError("record must be a mapping")
    if "run_id" not in record or record["run_id"] is None:
        raise ValueError("run_id is required")
    run_id = record["run_id"]
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_id must be a nonblank string")

    status = record.get("status", None)
    if status is None:
        status = "UNKNOWN"
    if status not in _STATUSES:
        raise ValueError("invalid status")

    quality = record.get("quality", None)
    if quality is None:
        quality = "unknown"
    if quality not in _QUALITIES:
        raise ValueError("invalid quality")

    raw_tokens = record.get("tokens", None)
    if raw_tokens is None:
        tokens = {key: None for key in _TOKEN_KEYS}
    else:
        if not isinstance(raw_tokens, Mapping):
            raise ValueError("tokens must be a mapping")
        tokens = {}
        for key in _TOKEN_KEYS:
            tokens[key] = _check_token_value(raw_tokens.get(key, None))

    duration = _check_duration_value(record.get("duration_seconds", None))

    return {
        "run_id": run_id,
        "status": status,
        "quality": quality,
        "tokens": tokens,
        "duration_seconds": duration,
    }


def summarize_runs(records):
    """Summarize normalized runs; never derive missing provider totals."""
    if records is None or isinstance(records, (str, bytes, bytearray, Mapping)):
        raise ValueError("records must be an iterable of mappings")
    try:
        rows = list(records)
    except TypeError:
        raise ValueError("records must be an iterable of mappings")
    normalized = [normalize_run(record) for record in rows]
    seen = set()
    for row in normalized:
        if row["run_id"] in seen:
            raise ValueError("duplicate run_id")
        seen.add(row["run_id"])

    status_counts = {key: 0 for key in _STATUSES}
    quality_counts = {"ACCEPT": 0, "REPAIR": 0, "unknown": 0}
    token_sums = {key: 0 for key in _TOKEN_KEYS}
    token_known = {key: 0 for key in _TOKEN_KEYS}
    token_unknown = {key: 0 for key in _TOKEN_KEYS}
    duration_sum = 0
    duration_known = 0
    duration_unknown = 0

    for row in normalized:
        status_counts[row["status"]] += 1
        quality_counts[row["quality"]] += 1
        for key in _TOKEN_KEYS:
            value = row["tokens"][key]
            if value is None:
                token_unknown[key] += 1
            else:
                token_sums[key] += value
                token_known[key] += 1
        if row["duration_seconds"] is None:
            duration_unknown += 1
        else:
            try:
                duration_sum += row["duration_seconds"]
            except OverflowError:
                raise ValueError("duration sum overflowed to non-finite value")
            duration_known += 1

    if duration_known and (
        duration_sum != duration_sum
        or duration_sum in (float("inf"), float("-inf"))
    ):
        raise ValueError("duration sum overflowed to non-finite value")

    tokens = {}
    for key in _TOKEN_KEYS:
        tokens[key] = {
            "known_sum": token_sums[key] if token_known[key] else None,
            "unknown_count": token_unknown[key],
        }
    return {
        "run_count": len(normalized),
        "status_counts": status_counts,
        "quality_counts": quality_counts,
        "tokens": tokens,
        "duration_seconds": {
            "known_sum": duration_sum if duration_known else None,
            "unknown_count": duration_unknown,
        },
    }
