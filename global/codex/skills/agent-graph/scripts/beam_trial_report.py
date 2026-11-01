"""Beam trial metadata reporting — closed allowlist, metadata only.

Reads bounded Fleet run receipts and explicitly supplied TODO pair/state
receipts. Only exact Beam rows are emitted; native handoff and independently
verified product review remain separate facts. No model, shell, credential,
transcript, usage-record, or stdout-path access is performed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import stat
import sys

VERSION = 1
ALLOW_AGENT = "opencode-beam"
ALLOW_MODEL = "Beam-1.0-501B-A23B"
ALLOW_PROVIDER = "reflection"
TRIAL_DEFAULT_SINCE = "2026-10-15T00:00:00+00:00"
DEFAULT_RUNS_ROOT = Path.home() / ".agents" / "agent-fleet" / "runs"
MAX_FILE_BYTES = 4 * 1024 * 1024
RUN_ID_RE = re.compile(r"^run-(\d{8}T\d{6}Z)-([0-9a-fA-F]{1,64})$")
ISO_DATE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
)
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
TOKEN_FIELDS = (
    "fresh_input_tokens", "output_tokens", "cache_read_tokens",
    "cache_write_tokens", "reasoning_tokens", "total_tokens",
)
QUALITY_ACCEPT = "accepted"
QUALITY_REPAIR = "repair"
QUALITY_UNKNOWN = "unknown"
QUALITY_VALUES = {QUALITY_ACCEPT, QUALITY_REPAIR, QUALITY_UNKNOWN}
RUN_STATUSES = {"COMPLETED", "FAILED", "BLOCKED", "CANCELLED", "PARTIAL"}


def _is_link_or_reparse(path: Path) -> bool:
    """Inspect the original path component without resolving through it."""
    try:
        info = path.lstat()
    except (OSError, ValueError):
        return False
    attrs = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(info.st_mode) or bool(attrs & reparse_flag)


def _resolve_strict(path):
    """Resolve an existing path only after rejecting links in every component."""
    try:
        original = Path(os.path.abspath(os.fspath(path)))
        components = list(reversed(original.parents)) + [original]
        for component in components:
            if _is_link_or_reparse(component):
                return None
        return original.resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _contained(child: Path, parent: Path) -> bool:
    try:
        return os.path.commonpath((os.path.normcase(str(child)),
                                   os.path.normcase(str(parent)))) == os.path.normcase(str(parent))
    except (OSError, ValueError):
        return False


def _parse_iso(value):
    if not isinstance(value, str) or not ISO_DATE_RE.fullmatch(value):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _valid_run_id(value):
    if not isinstance(value, str):
        return False
    match = RUN_ID_RE.fullmatch(value)
    if not match:
        return False
    try:
        datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ")
    except ValueError:
        return False
    return True


def _valid_sha256(value):
    return isinstance(value, str) and bool(SHA256_RE.fullmatch(value))


def _coerce_number(value):
    """Return a finite, nonnegative float; large integers fail closed safely."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    if isinstance(value, int):
        try:
            value = float(value)
        except (OverflowError, ValueError):
            return None
    if not math.isfinite(value):
        return None
    return float(value)


def _coerce_count(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _read_json(path, parent=None):
    """Read one bounded regular JSON file after link and containment checks."""
    resolved = _resolve_strict(path)
    if resolved is None:
        return None
    if parent is not None:
        resolved_parent = _resolve_strict(parent)
        if resolved_parent is None or not _contained(resolved, resolved_parent):
            return None
    try:
        info = resolved.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or info.st_size > MAX_FILE_BYTES:
            return None
        raw = resolved.read_text(encoding="utf-8-sig")
        value = json.loads(raw, parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
    except (OSError, RuntimeError, TypeError, ValueError, UnicodeError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _matches_allowlist(record):
    return (isinstance(record, dict)
            and record.get("agent") == ALLOW_AGENT
            and record.get("model") == ALLOW_MODEL
            and record.get("model_provider_id") == ALLOW_PROVIDER)


def _normalized_status(value):
    return value if isinstance(value, str) and value in RUN_STATUSES else "UNKNOWN"


def _extract_tokens(record):
    usage = record.get("token_usage") if isinstance(record, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    values = {field: _coerce_count(usage.get(field)) for field in TOKEN_FIELDS}
    values["cost_usd"] = _coerce_number(usage.get("cost_usd"))
    return values


def _is_run_after(record, since_dt):
    started = _parse_iso(record.get("started_at") if isinstance(record, dict) else None)
    if started is None:
        return False
    return since_dt is None or started >= since_dt


def _native_runtime_ok(record):
    attestation = record.get("runtime_attestation") if isinstance(record, dict) else None
    return isinstance(attestation, dict) and attestation.get("status") == "VERIFIED"


def _native_identity_verified(record):
    identity = record.get("model_identity") if isinstance(record, dict) else None
    return isinstance(identity, dict) and identity.get("status") == "verified"


def _native_identity_ok(record):
    identity = record.get("model_identity") if isinstance(record, dict) else None
    if not isinstance(identity, dict):
        return False
    status = identity.get("status")
    return status == "verified" or (
        isinstance(status, str)
        and status in {"provider-attested", "provider_attested"}
    )


def _native_reviewer_ok(review, native_index):
    run_id = review.get("run_id")
    if not _valid_run_id(run_id):
        return False
    records = native_index.get(run_id, [])
    if len(records) != 1:
        return False
    record = records[0]
    review_agent = review.get("agent")
    review_model = review.get("model")
    review_provider = review.get("lineage")
    return (
        isinstance(review_agent, str) and bool(review_agent)
        and isinstance(review_model, str) and bool(review_model)
        and isinstance(review_provider, str) and bool(review_provider)
        and record.get("agent") == review_agent
        and record.get("model") == review_model
        and record.get("model_provider_id") == review_provider
        and record.get("run_status") == "COMPLETED"
        and _parse_iso(record.get("started_at")) is not None
        and _parse_iso(record.get("completed_at")) is not None
        and _native_identity_ok(record)
        and _native_runtime_ok(record)
    )


def _history_for_node(node):
    if not isinstance(node, dict):
        return None
    history = node.get("history")
    if isinstance(history, list):
        return history
    result = node.get("result")
    if isinstance(result, dict) and isinstance(result.get("history"), list):
        return result["history"]
    return None


def _metadata_for_node(node):
    if not isinstance(node, dict):
        return None
    result = node.get("result") if isinstance(node.get("result"), dict) else {}
    artifact = node.get("artifact", result.get("artifact"))
    checks = node.get("checks_digest", result.get("checks_digest"))
    inputs = node.get("inputs_digest", result.get("inputs_digest"))
    return {
        "status": node.get("status"),
        "artifact": artifact,
        "checks_digest": checks,
        "inputs_digest": inputs,
        "history": _history_for_node(node),
    }


def _pair_metadata(pair):
    if not isinstance(pair, dict) or not isinstance(pair.get("result"), dict):
        return None
    result = pair["result"]
    return {
        "status": result.get("status"),
        "artifact": result.get("artifact"),
        "checks_digest": result.get("checks_digest"),
        "inputs_digest": result.get("inputs_digest"),
        "history": result.get("history"),
    }


def _actual_maker(entry):
    if not isinstance(entry, dict) or entry.get("role") != "maker":
        return None
    result = entry.get("result")
    if not isinstance(result, dict) or not _valid_run_id(result.get("run_id")):
        return None
    return entry, result


def _evaluate_history(metadata, expected_run_id, native_index):
    """Evaluate only the latest maker and the latest subsequent reviewer."""
    history = metadata.get("history")
    if not isinstance(history, list) or not history:
        return QUALITY_UNKNOWN

    makers = [(i, _actual_maker(entry)) for i, entry in enumerate(history)]
    makers = [(i, item) for i, item in makers if item is not None]
    if not makers:
        return QUALITY_UNKNOWN
    maker_index, (maker_entry, maker) = makers[-1]
    if (maker.get("run_id") != expected_run_id
            or maker.get("agent") != ALLOW_AGENT
            or maker.get("model") != ALLOW_MODEL
            or maker.get("lineage") != ALLOW_PROVIDER):
        return QUALITY_UNKNOWN

    reviews = [entry for entry in history[maker_index + 1:]
               if isinstance(entry, dict) and entry.get("role") == "reviewer"]
    if not reviews:
        return QUALITY_UNKNOWN
    reviewer_entry = reviews[-1]
    reviewer = reviewer_entry.get("result")
    if not isinstance(reviewer, dict):
        return QUALITY_UNKNOWN

    artifact = metadata.get("artifact")
    if not _valid_sha256(artifact):
        return QUALITY_UNKNOWN
    if not _valid_sha256(metadata.get("checks_digest")):
        return QUALITY_UNKNOWN
    if not _valid_sha256(metadata.get("inputs_digest")):
        return QUALITY_UNKNOWN
    maker_artifact = maker_entry.get("artifact", maker.get("artifact"))
    if maker_artifact is not None and maker_artifact != artifact:
        return QUALITY_UNKNOWN
    if reviewer_entry.get("artifact") != artifact or not _valid_sha256(reviewer_entry.get("artifact")):
        return QUALITY_UNKNOWN
    if reviewer.get("status") != "REVIEWED":
        return QUALITY_UNKNOWN
    maker_lineage = maker.get("lineage")
    reviewer_lineage = reviewer.get("lineage")
    if not isinstance(reviewer_lineage, str) or not reviewer_lineage or reviewer_lineage == maker_lineage:
        return QUALITY_UNKNOWN
    if not _native_reviewer_ok(reviewer, native_index):
        return QUALITY_UNKNOWN

    verdict = reviewer.get("verdict")
    findings = reviewer.get("findings")
    if verdict == "ACCEPT" and findings == []:
        tests = reviewer_entry.get("tests")
        if (metadata.get("status") != "ACCEPTED"
                or not isinstance(tests, list) or not tests
                or any(not isinstance(test, dict)
                       or type(test.get("exit_code")) is not int
                       or test.get("exit_code") != 0 for test in tests)):
            return QUALITY_UNKNOWN
        return QUALITY_ACCEPT
    if verdict == "REPAIR" and isinstance(findings, list) and findings:
        return QUALITY_REPAIR
    return QUALITY_UNKNOWN


def _state_nodes(state):
    if not isinstance(state, dict):
        return []
    nodes = state.get("nodes")
    return list(nodes.values()) if isinstance(nodes, dict) else []


def _quality_for_run(row, todo_index, native_index):
    candidates = []
    for evidence in todo_index.values():
        state = evidence.get("state")
        state_nodes = _state_nodes(state)
        state_matched = False
        for node in state_nodes:
            metadata = _metadata_for_node(node)
            history = metadata.get("history") if metadata else None
            if not isinstance(history, list):
                continue
            if any((item := _actual_maker(entry)) is not None
                   and item[1].get("run_id") == row["run_id"] for entry in history):
                state_matched = True
                candidates.append(_evaluate_history(metadata, row["run_id"], native_index))
        if state_matched:
            continue
        metadata = _pair_metadata(evidence.get("pair"))
        if metadata:
            history = metadata.get("history")
            if isinstance(history, list) and any(
                (item := _actual_maker(entry)) is not None
                and item[1].get("run_id") == row["run_id"] for entry in history
            ):
                candidates.append(_evaluate_history(metadata, row["run_id"], native_index))
    if len(candidates) != 1:
        return QUALITY_UNKNOWN
    return candidates[0] if candidates[0] in QUALITY_VALUES else QUALITY_UNKNOWN


def _row_for(record, todo_index, native_index):
    row = {
        "run_id": record["run_id"],
        "started_at": record["started_at"],
        "completed_at": record["completed_at"],
        "run_status": _normalized_status(record.get("run_status")),
        "duration_seconds": _coerce_number(record.get("duration_seconds")),
        "tokens": _extract_tokens(record),
        "agent": ALLOW_AGENT,
        "model": ALLOW_MODEL,
        "lineage": ALLOW_PROVIDER,
        "native_runtime_verified": bool(_native_runtime_ok(record)),
        "native_model_identity_verified": bool(_native_identity_verified(record)),
        "native_handoff_valid": record.get("handoff_valid")
            if isinstance(record.get("handoff_valid"), bool) else None,
        "quality": QUALITY_UNKNOWN,
    }
    row["quality"] = _quality_for_run(row, todo_index, native_index)
    return row


def _iter_runs(root):
    if not isinstance(root, Path) or not root.is_dir():
        return
    try:
        children = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError:
        return
    for child in children:
        if not child.name.startswith("run-") or not child.name.endswith(".json"):
            continue
        if _is_link_or_reparse(child):
            continue
        try:
            if child.parent != root or not stat.S_ISREG(child.lstat().st_mode):
                continue
        except OSError:
            continue
        yield child


def _load_native_index(root):
    index = {}
    for path in _iter_runs(root) or ():
        record = _read_json(path, parent=root)
        if not isinstance(record, dict) or not _valid_run_id(record.get("run_id")):
            continue
        if not _parse_iso(record.get("started_at")):
            continue
        if record.get("completed_at") is not None and not _parse_iso(record.get("completed_at")):
            continue
        index.setdefault(record["run_id"], []).append(record)
    return index


def _index_todo(folders):
    index = {}
    for folder in folders or []:
        resolved = _resolve_strict(folder)
        if resolved is None or not resolved.is_dir():
            continue
        pair = _read_json(resolved / "pair.json", parent=resolved)
        state = _read_json(resolved / "state.json", parent=resolved)
        if pair is None and state is None:
            continue
        index[resolved] = {"pair": pair, "state": state}
    return index


def _aggregate(rows):
    quality = {QUALITY_ACCEPT: 0, QUALITY_REPAIR: 0, QUALITY_UNKNOWN: 0}
    native_success = native_failure = 0
    token_known = token_unknown = 0
    durations = []
    costs = []
    for row in rows:
        quality[row["quality"]] += 1
        if row["run_status"] == "COMPLETED" and row["native_handoff_valid"] is True:
            native_success += 1
        elif row["run_status"] in {"FAILED", "BLOCKED", "CANCELLED", "PARTIAL"}:
            native_failure += 1
        for field, value in row["tokens"].items():
            if value is None:
                token_unknown += 1
            else:
                token_known += 1
        duration = row["duration_seconds"]
        if duration is not None:
            durations.append(duration)
        if row["tokens"]["cost_usd"] is not None:
            costs.append(row["tokens"]["cost_usd"])
    try:
        cost_sum = math.fsum(costs) if costs else None
        if cost_sum is not None and not math.isfinite(cost_sum):
            cost_sum = None
    except (OverflowError, ValueError):
        cost_sum = None
    try:
        wall_seconds = math.fsum(durations) if durations else 0.0
        if not math.isfinite(wall_seconds):
            wall_seconds = None
    except (OverflowError, ValueError):
        wall_seconds = None
    return {
        "rows": len(rows),
        "quality_counts": quality,
        "native_successes": native_success,
        "native_failures": native_failure,
        "token_known_fields": token_known,
        "token_unknown_fields": token_unknown,
        "native_wall_seconds": round(wall_seconds, 6) if wall_seconds is not None else None,
        "cost_known_rows": len(costs),
        "cost_unknown_rows": len(rows) - len(costs),
        "cost_usd_sum": cost_sum,
    }


def collect(runs_root, since_dt, todo_folders):
    root = _resolve_strict(runs_root) if runs_root else None
    todo_index = _index_todo(todo_folders or [])
    native_index = _load_native_index(root) if root is not None and root.is_dir() else {}
    rows = []
    seen = set()
    if root is not None:
        for run_path in _iter_runs(root) or ():
            record = _read_json(run_path, parent=root)
            if record is None or not _matches_allowlist(record):
                continue
            run_id = record.get("run_id")
            if not _valid_run_id(run_id) or run_id in seen:
                continue
            if not _is_run_after(record, since_dt):
                continue
            started_at = record.get("started_at")
            completed_at = record.get("completed_at")
            rows.append(_row_for({
                **record,
                "run_id": run_id,
                "started_at": started_at,
                "completed_at": completed_at if _parse_iso(completed_at) else None,
            }, todo_index, native_index))
            seen.add(run_id)
    return {
        "version": VERSION,
        "allowlist": {"agent": ALLOW_AGENT, "model": ALLOW_MODEL,
                      "lineage": ALLOW_PROVIDER},
        "since": since_dt.isoformat() if since_dt else None,
        "rows": rows,
        "aggregate": _aggregate(rows),
    }


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-root", default=str(DEFAULT_RUNS_ROOT),
                        help="Fleet runs directory (default: ~/.agents/agent-fleet/runs)")
    parser.add_argument("--since", default=TRIAL_DEFAULT_SINCE,
                        help="ISO timestamp lower bound for started_at (default: 2026-10-15)")
    parser.add_argument("--todo-run", action="append", default=[],
                        help="Directory with explicitly supplied pair.json or state.json (repeatable)")
    parser.add_argument("--output", default=None,
                        help="Optional output file; stdout otherwise")
    return parser.parse_args(argv)


def _parse_since(value):
    return _parse_iso(value)


def main(argv=None):
    args = _parse_args(argv)
    runs_root = _resolve_strict(Path(args.runs_root))
    since_dt = _parse_since(args.since)
    if runs_root is None or not runs_root.is_dir():
        print(json.dumps({"error": "refused-runs-root"}), file=sys.stderr)
        return 2
    if since_dt is None:
        print(json.dumps({"error": "invalid-since"}), file=sys.stderr)
        return 2
    payload = collect(runs_root, since_dt, [Path(item) for item in args.todo_run])
    output = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    if args.output:
        requested_out = Path(os.path.abspath(args.output))
        safe_parent = _resolve_strict(requested_out.parent)
        if (safe_parent is not None and safe_parent.is_dir()
                and not _is_link_or_reparse(requested_out)):
            destination = safe_parent / requested_out.name
        else:
            destination = None
    else:
        destination = None
    if destination is not None and not destination.exists():
        try:
            destination.write_text(output, encoding="utf-8")
        except OSError:
            sys.stdout.write(output + "\n")
    else:
        sys.stdout.write(output + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
