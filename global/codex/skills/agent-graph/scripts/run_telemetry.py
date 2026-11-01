"""Metadata-only, best-effort TODO journal. No provider calls or write-time index."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import itertools
import json
import math
import os
from pathlib import Path
import queue
import re
import threading
import time
import uuid

VERSION = 1
MAX_EVENT = 4096
MAX_FILE = 8 * 1024 * 1024
NUMBERS = {"seconds", "exit_code", "round", "input_tokens", "output_tokens", "total_tokens",
           "cached_input_tokens", "reasoning_tokens", "pid", "tasks", "accepted", "parallel",
           "quota_before_confidence", "quota_after_confidence",
           "fresh_input", "cache_read", "cache_write", "cache_ttl_seconds",
           "fresh_input_tokens", "cache_read_tokens", "cache_write_tokens"}
TEXT = {"node", "role", "stage", "status", "code", "agent", "model", "lineage", "provider_run",
        "recovery", "wait_reason", "scheduler_version", "quota_before_status", "quota_after_status",
        "cache_input_source"}
SAFE = re.compile(r"[a-zA-Z0-9_.:/-]{1,160}\Z")

# Passive native cache accounting: no foreign installs, no active cache warmer,
# no new scheduler/provider call, no price estimate, no inferred semantics.
# The three cache input components drive the per-provider ratio; output_tokens
# and cache_ttl_seconds are aggregated for coverage but do not enter the ratio.
CACHE_INPUT_COMPONENTS = ("fresh_input", "cache_read", "cache_write")
CACHE_COMPONENTS = CACHE_INPUT_COMPONENTS + ("output_tokens", "cache_ttl_seconds")
CACHE_INPUT_SOURCES = frozenset({"codex_inclusive", "claude_exclusive", "normalized_explicit"})


def _is_normalized_number(value):
    """Numeric, not bool, finite, non-negative token/TTL count.

    Booleans subclass ``int`` and would otherwise leak True/False as counts.
    NaN/Inf are non-finite; negatives are not a count. The journal filters
    non-finite values at emit too; rejecting here keeps the journal line
    clean and lets the report count invalid samples as ``unknown`` instead of
    silently coercing them to zero.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if not math.isfinite(value):
        return False
    if value < 0:
        return False
    return True


def require_absolute_run_dir(folder):
    """Refuse a relative evidence path instead of resolving it against cwd.

    Measured 2026-09-21: Path(folder).resolve() plus mkdir planted relative
    run directories in the shared-skill origin whenever cwd was that origin.
    The same relative string hashes differently per cwd in root_for(), so one
    logical run would also get split journals. Refuse; do not re-anchor.
    """
    path = Path(folder)
    if not folder or not path.is_absolute():
        raise ValueError(
            "Relative --run-dir is refused, not resolved against the current working "
            "directory. Pass an absolute evidence path."
        )
    return path


def root_for(evidence):
    # Same refusal as the scheduler: hashing a relative path would bind the
    # journal to cwd, splitting one logical run across different logs.
    evidence = require_absolute_run_dir(evidence)
    key = hashlib.sha256(str(Path(evidence).resolve()).casefold().encode()).hexdigest()[:24]
    return Path.home() / ".agents/telemetry/todo" / key


class Journal:
    def __init__(self, root, *, enabled=True, capacity=1024, max_bytes=MAX_FILE):
        self.enabled = enabled and os.environ.get("AGENT_TODO_TELEMETRY", "on") != "off"
        self.path = Path(root) / (uuid.uuid4().hex + ".jsonl")
        self.q = queue.Queue(maxsize=capacity)
        self.stop = threading.Event()
        self.seq = itertools.count(1)
        self.dropped = 0
        self.errors = 0
        self.written = 0
        self.max_bytes = max_bytes
        self.thread = None
        self.warning_sent = False
        if self.enabled:
            try:
                self.thread = threading.Thread(target=self._writer, daemon=True, name="todo-telemetry")
                self.thread.start()
            except RuntimeError:
                self.errors += 1
                self.thread = None

    def emit(self, event, **fields):
        if not self.enabled:
            return
        value = {"schema": VERSION, "event": event, "sequence": next(self.seq),
                 "utc": datetime.now(timezone.utc).isoformat(), "monotonic": time.monotonic()}
        if not isinstance(event, str) or not SAFE.fullmatch(event):
            self.dropped += 1
            return
        for key, item in fields.items():
            if key in TEXT and isinstance(item, str) and SAFE.fullmatch(item):
                value[key] = item
            elif key in NUMBERS and type(item) in (int, float) and math.isfinite(item):
                value[key] = item
        try:
            self.q.put_nowait(value)
        except queue.Full:
            self.dropped += 1

    def _writer(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            size = 0
            with self.path.open("xb", buffering=8192) as handle:
                while not self.stop.is_set() or not self.q.empty():
                    try:
                        value = self.q.get(timeout=0.05)
                    except queue.Empty:
                        handle.flush()
                        continue
                    raw = (json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
                    if len(raw) > MAX_EVENT or size + len(raw) > self.max_bytes:
                        self.dropped += 1
                    else:
                        handle.write(raw)
                        size += len(raw)
                        self.written += 1
                    if self.q.empty() or self.written % 16 == 0:
                        handle.flush()
                # A missing footer means unconfirmed coverage, including a process crash.
                footer = {"schema": VERSION, "event": "telemetry_closed", "written": self.written,
                          "dropped": self.dropped, "errors": self.errors}
                handle.write((json.dumps(footer) + "\n").encode())
        except (OSError, ValueError):
            self.errors += 1

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=0.25)
        incomplete = self.enabled and (self.errors or self.dropped or self.thread is None or self.thread.is_alive())
        if incomplete and not self.warning_sent:
            self.warning_sent = True
            try:
                print("TODO_TELEMETRY_INCOMPLETE (product execution is unaffected)", file=__import__("sys").stderr)
            except OSError:
                pass
        return {"enabled": self.enabled, "complete": bool(self.enabled and not incomplete),
                "dropped": self.dropped, "errors": self.errors,
                "path": str(self.path) if self.enabled else None}


@contextmanager
def stage(journal, node, name, **fields):
    began = time.monotonic()
    journal.emit("stage_start", node=node, stage=name, **fields)
    try:
        yield
    except Exception as exc:
        # Exception messages/argv can contain credentials; never persist them here.
        journal.emit("stage_error", node=node, stage=name, code=type(exc).__name__, seconds=time.monotonic() - began)
        raise
    else:
        journal.emit("stage_end", node=node, stage=name, seconds=time.monotonic() - began)


def provider_result(journal, node, role, result, round_number):
    fields = {k: result[k] for k in ("agent", "model", "lineage") if k in result}
    fields.update(node=node, role=role, status=result.get("status"), provider_run=result.get("run_id"), round=round_number)
    usage = result.get("tokens")
    if isinstance(usage, dict):
        # Legacy token fields (key ending with "tokens"); the journal filters
        # non-finite values at emit time, preserving prior behaviour.
        for k, v in usage.items():
            if k in NUMBERS and k.endswith("tokens"):
                fields[k] = v
        # Normalized cache components with strict validation. No inference:
        # if fresh_input is missing we count the attempt as 'unknown' for that
        # component rather than subtracting cache_read from input_tokens
        # (Codex/Claude report input differently).
        for k in ("fresh_input", "cache_read", "cache_write", "cache_ttl_seconds"):
            native_key = k + "_tokens" if k != "cache_ttl_seconds" else k
            if native_key in usage:
                v = usage[native_key]
            else:
                v = usage.get(k)
            if _is_normalized_number(v):
                fields[k] = v
        # Legacy alias: cached_input -> cache_read (backward compatibility).
        if "cache_read_tokens" not in usage and "cache_read" not in usage:
            v = usage.get("cached_input_tokens", usage.get("cached_input"))
            if _is_normalized_number(v):
                fields["cache_read"] = v
        # Cache input source semantic: only known native conventions and the
        # explicit normalized-component label are recorded. Other free-form
        # provider labels, prompt-shaped strings) is dropped so the journal
        # never logs arbitrary text, keys, or prompts.
        source = usage.get("cache_input_source")
        if source is not None and not isinstance(source, str):
            source = "inconsistent"
        if source is None and any(k in usage for k in ("fresh_input_tokens", "cache_read_tokens", "cache_write_tokens")):
            adapter = usage.get("adapter", result.get("agent"))
            if not isinstance(adapter, str):
                adapter = ""
            source = {"codex": "codex_inclusive", "claude": "claude_exclusive"}.get(adapter, "normalized_explicit")
        # Validate only explicitly known native input conventions. Missing
        # fresh input is never inferred, including for an unknown provider.
        if source == "codex_inclusive" and all(k in fields for k in CACHE_INPUT_COMPONENTS) and _is_normalized_number(usage.get("input_tokens")):
            if usage["input_tokens"] != fields["fresh_input"] + fields["cache_read"]:
                source = "inconsistent"
        if source == "claude_exclusive" and "fresh_input" in fields and _is_normalized_number(usage.get("input_tokens")):
            if usage["input_tokens"] != fields["fresh_input"]:
                source = "inconsistent"
        if source in CACHE_INPUT_SOURCES or source == "inconsistent":
            fields["cache_input_source"] = source
    recovery = result.get("recovery")
    if isinstance(recovery, dict):
        fields["recovery"] = recovery.get("method")
    for key in ("quota_before", "quota_after"):
        quota = result.get(key)
        if isinstance(quota, dict):
            fields[key + "_status"] = quota.get("status", "unknown")
            fields[key + "_confidence"] = quota.get("confidence")
        else:
            fields[key + "_status"] = "unknown"
    journal.emit("provider_result", **fields)


def report(root):
    """Read only journal metadata on demand, never request files or native stdout."""
    rows, corrupt, incomplete, dropped, oversized = [], 0, 0, 0, 0
    journals = list(Path(root).glob("*.jsonl"))
    for path in journals:
        closed = False
        try:
            if path.stat().st_size > MAX_FILE + MAX_EVENT:
                oversized += 1
                incomplete += 1
                continue
            with path.open("rb") as handle:
                while True:
                    line = handle.readline(MAX_EVENT + 1)
                    if not line:
                        break
                    if len(line) > MAX_EVENT or not line.endswith(b"\n"):
                        corrupt += 1
                        break
                    try:
                        value = json.loads(line)
                        if not isinstance(value, dict) or value.get("schema") != VERSION:
                            raise ValueError()
                        if value.get("event") == "telemetry_closed":
                            closed = True
                            dropped += int(value.get("dropped", 0))
                        else:
                            value["journal"] = path.stem
                            rows.append(value)
                    except (ValueError, TypeError, UnicodeError):
                        corrupt += 1
        except OSError:
            corrupt += 1
        if not closed:
            incomplete += 1
    durations = Counter()
    outcomes = Counter()
    errors = Counter()
    wait_seconds, waiting = Counter(), {}
    providers, seen_runs, wall_times = {}, set(), []
    for row in rows:
        if row.get("event") in {"stage_end", "stage_error"} and type(row.get("seconds")) in (int, float):
            durations[row.get("stage", "unknown")] += row["seconds"]
        if row.get("event") == "node_terminal":
            outcomes[row.get("status", "unknown")] += 1
        if row.get("event") == "stage_error":
            errors[f"{row.get('stage')}/{row.get('code')}"] += 1
        key = (row.get("journal"), row.get("node"))
        if row.get("event") in {"node_wait", "node_start", "node_terminal"}:
            prior = waiting.pop(key, None)
            if prior and type(row.get("monotonic")) in (int, float):
                wait_seconds[prior[0]] += max(0, row["monotonic"] - prior[1])
            if row.get("event") == "node_wait" and type(row.get("monotonic")) in (int, float):
                waiting[key] = (row.get("wait_reason", "unknown"), row["monotonic"])
        if row.get("event") == "run_terminal" and type(row.get("seconds")) in (int, float):
            wall_times.append(round(row["seconds"], 3))
        if row.get("event") == "provider_result":
            # Director clarification: same provider_run duplicate ignored,
            # different actually executed attempts counted.
            run = row.get("provider_run")
            if run and run in seen_runs:
                continue
            if run:
                seen_runs.add(run)
            identity = row.get("agent", "unknown") + "/" + row.get("model", "unknown")
            stats = providers.setdefault(identity, {
                "results": 0,
                "unknown_usage": 0,
                "reported_total_tokens": None,
                "components": {},
                "cache_ratio": None,
                "cache_input_source": None,
                "cache_components_complete": False,
            })
            stats["results"] += 1
            if type(row.get("total_tokens")) in (int, float):
                stats["reported_total_tokens"] = (stats["reported_total_tokens"] or 0) + row["total_tokens"]
            else:
                stats["unknown_usage"] += 1
            # Cache components aggregation with per-component known/unknown
            # coverage. Invalid (negative/bool/NaN/Inf) samples and missing
            # keys both count as 'unknown' so the counters never silently
            # coerce to zero. True zeros (cache_read=0) are reported.
            for comp_key in CACHE_COMPONENTS:
                comp = stats["components"].setdefault(comp_key, {"sum": None, "reported": 0, "unknown": 0})
                val = row.get(comp_key)
                if _is_normalized_number(val):
                    comp["sum"] = (comp["sum"] or 0) + val
                    comp["reported"] += 1
                else:
                    comp["unknown"] += 1
            # Cache input source semantic: per-provider consistency check.
            # Codex reports input that includes cached input; Claude reports
            # fresh input that excludes cache, with cache_read/cache_write
            # separate. Mixed semantics within one provider are recorded as
            # 'inconsistent' and reject ratio inference for that provider.
            source = row.get("cache_input_source")
            if source == "inconsistent":
                stats["cache_input_source"] = "inconsistent"
            elif source in CACHE_INPUT_SOURCES:
                if stats["cache_input_source"] is None:
                    stats["cache_input_source"] = source
                elif stats["cache_input_source"] != source:
                    stats["cache_input_source"] = "inconsistent"
            # Per-provider cache ratio: only when all three cache input
            # components are reported for every attempt AND the input
            # semantic is known and consistent. Inconsistent input semantics,
            # partial components, or zero denominators leave the ratio null.
            # Re-evaluated after each row so a stale ratio from a previous
            # attempt is cleared once coverage becomes uneven or the semantic
            # flips to 'inconsistent'.
            if stats["cache_input_source"] in CACHE_INPUT_SOURCES:
                comp_complete = all(
                    stats["components"].get(k, {}).get("reported", 0) == stats["results"]
                    for k in CACHE_INPUT_COMPONENTS
                )
                if comp_complete:
                    fresh = stats["components"]["fresh_input"]["sum"]
                    read = stats["components"]["cache_read"]["sum"]
                    write = stats["components"]["cache_write"]["sum"]
                    denom = fresh + read + write
                    if denom > 0:
                        stats["cache_ratio"] = round(read / denom, 6)
                        stats["cache_components_complete"] = True
                    else:
                        stats["cache_ratio"] = None
                        stats["cache_components_complete"] = False
                else:
                    stats["cache_ratio"] = None
                    stats["cache_components_complete"] = False
            else:
                # Semantic missing or inconsistent: no ratio can be inferred.
                stats["cache_ratio"] = None
                stats["cache_components_complete"] = False
    # Cross-provider cache ratio aggregate: only across the complete-component
    # population (per-provider cache_components_complete=True). Mixed sources
    # within a single provider already block its per-provider ratio, so the
    # aggregate is restricted to providers with a single consistent semantic.
    # Different metric from quota, availability, or subscription billing; no
    # price estimate; no active warming; no new scheduler/provider call.
    agg_fresh = agg_read = agg_write = 0
    agg_providers = 0
    for stats in providers.values():
        if stats.get("cache_components_complete"):
            agg_fresh += stats["components"]["fresh_input"]["sum"]
            agg_read += stats["components"]["cache_read"]["sum"]
            agg_write += stats["components"]["cache_write"]["sum"]
            agg_providers += 1
    agg_denom = agg_fresh + agg_read + agg_write
    aggregate = {
        "fresh_input_sum": agg_fresh,
        "cache_read_sum": agg_read,
        "cache_write_sum": agg_write,
        "providers_in_population": agg_providers,
        "cache_ratio": round(agg_read / agg_denom, 6) if agg_providers and agg_denom > 0 else None,
    }
    return {
        "journals": len(journals),
        "events": len(rows),
        "incomplete_journals": incomplete,
        "dropped": dropped,
        "malformed": corrupt,
        "oversized": oversized,
        "coverage": "complete" if journals and not (incomplete or dropped or corrupt or oversized) else "incomplete",
        "stage_worker_seconds": {k: round(v, 3) for k, v in durations.items()},
        "observed_wait_seconds": {k: round(v, 3) for k, v in wait_seconds.items()},
        "unfinished_waits": len(waiting),
        "run_wall_seconds": wall_times,
        "providers": providers,
        "cache_ratio_aggregate": aggregate,
        "cache_ratio_definition": (
            "Per-provider cache_ratio = cache_read / (fresh_input + cache_read + cache_write); "
            "computed only when all three cache input components (fresh_input, cache_read, cache_write) "
            "are reported for every attempt and cache_input_source is consistent "
            "(codex_inclusive, claude_exclusive or normalized_explicit). Inconsistent input semantics or missing components "
            "reject inference. cache_ttl_seconds is aggregated for coverage only and does not enter the "
            "ratio; cache_ttl_seconds is recorded only if explicitly observed (otherwise unknown). "
            "Different metric from quota, availability, or subscription billing; no price estimate, "
            "no active warming, no new scheduler/provider call."
        ),
        "note": "Worker seconds overlap; not project wall time. No inferred token price or quota attribution.",
        "node_attempt_outcomes": dict(outcomes),
        "errors": dict(errors),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, help="Absolute TODO evidence directory (only its path is hashed)")
    args = parser.parse_args()
    print(json.dumps(report(root_for(args.run_dir)), indent=2))
