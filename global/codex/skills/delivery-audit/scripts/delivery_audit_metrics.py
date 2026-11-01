"""Passive delivery lead-time and ceremony_ratio telemetry for delivery-audit.

Computes lead_time_s, model_exec_s, review_s, check_s, ceremony_s, and
ceremony_ratio = round(ceremony_s / max(lead_time_s, 1.0), 4) from existing
todo_graph/todo_pair state.json and telemetry.jsonl files. Zero new per-task
ceremony files are created. Flags high_ceremony_alert=True when ceremony_ratio > 0.25.
Provides a 'benchmark' subcommand evaluating S (inline TODO), M (single todo_pair),
and L (parallel todo_graph) trace profiles.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
import json
import math
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping, Sequence

# Windows subprocess creation flag for windowless background execution
_SUBPROCESS_KWARGS: dict[str, Any] = {}
if sys.platform == "win32":
    _SUBPROCESS_KWARGS["creationflags"] = subprocess.CREATE_NO_WINDOW

CEREMONY_ALERT_THRESHOLD = 0.25

BENCHMARK_PROFILES: dict[str, dict[str, Any]] = {
    "S": {
        "description": "S: Inline TODO (low ceremony, single session, no graph/pair subprocess)",
        "lead_time_s": 15.0,
        "model_exec_s": 12.0,
        "review_s": 2.0,
        "check_s": 0.5,
        "ceremony_s": 0.5,
        "profile": "S",
    },
    "M": {
        "description": "M: Single todo_pair (moderate ceremony, maker/reviewer without graph scheduler overhead)",
        "lead_time_s": 85.0,
        "model_exec_s": 55.0,
        "review_s": 18.0,
        "check_s": 4.0,
        "ceremony_s": 8.0,
        "profile": "M",
    },
    "L": {
        "description": "L: Parallel todo_graph (orchestrated multi-task DAG with parallel workers)",
        "lead_time_s": 240.0,
        "model_exec_s": 160.0,
        "review_s": 45.0,
        "check_s": 15.0,
        "ceremony_s": 20.0,
        "profile": "L",
    },
}


@dataclass(frozen=True)
class DeliveryMetrics:
    """Delivery efficiency metrics passively computed from task execution traces."""

    lead_time_s: float
    model_exec_s: float
    review_s: float
    check_s: float
    ceremony_s: float
    ceremony_ratio: float
    high_ceremony_alert: bool
    profile: str = "custom"
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lead_time_s": self.lead_time_s,
            "model_exec_s": self.model_exec_s,
            "review_s": self.review_s,
            "check_s": self.check_s,
            "ceremony_s": self.ceremony_s,
            "ceremony_ratio": self.ceremony_ratio,
            "high_ceremony_alert": self.high_ceremony_alert,
            "profile": self.profile,
            "details": self.details,
        }


def compute_ceremony_ratio(ceremony_s: float, lead_time_s: float) -> float:
    """Compute ceremony_ratio = round(ceremony_s / max(lead_time_s, 1.0), 4)."""
    denom = max(float(lead_time_s), 1.0)
    return round(float(ceremony_s) / denom, 4)


def is_high_ceremony(ceremony_ratio: float) -> bool:
    """Flag high ceremony alert when ceremony_ratio > 0.25."""
    return ceremony_ratio > CEREMONY_ALERT_THRESHOLD


def _parse_timestamp(val: Any) -> float | None:
    """Safely parse timestamp from number, numeric string, or ISO datetime string."""
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        val = val.strip()
        try:
            return float(val)
        except ValueError:
            pass
        try:
            from datetime import datetime
            return datetime.fromisoformat(val).timestamp()
        except Exception:
            pass
    return None


def _extract_duration(obj: Any) -> float:
    """Safely extract duration in seconds from number, numeric string, or mapping."""
    if isinstance(obj, (int, float)):
        return max(0.0, float(obj))
    if isinstance(obj, str):
        try:
            return max(0.0, float(obj.strip()))
        except ValueError:
            pass
    if isinstance(obj, Mapping):
        for key in ("duration_s", "wall_seconds", "wall_time_s", "duration", "elapsed_s", "seconds", "sec"):
            val = obj.get(key)
            if isinstance(val, (int, float)):
                return max(0.0, float(val))
            if isinstance(val, str):
                try:
                    return max(0.0, float(val.strip()))
                except ValueError:
                    pass
    return 0.0


def compute_delivery_metrics(
    state: Mapping[str, Any] | None = None,
    telemetry: Sequence[Mapping[str, Any]] | None = None,
    *,
    profile: str = "custom",
    override_lead_time_s: float | None = None,
) -> DeliveryMetrics:
    """Passively compute delivery metrics from state mapping and telemetry records.

    Zero new ceremony files are created.
    """
    model_exec_s = 0.0
    review_s = 0.0
    check_s = 0.0
    explicit_ceremony_s = 0.0
    lead_time_s = 0.0
    details: dict[str, Any] = {}

    min_ts: float | None = None
    max_ts: float | None = None

    # 1. Parse telemetry events if available
    telemetry_has_durations = False
    if telemetry:
        for event in telemetry:
            if not isinstance(event, Mapping):
                continue

            # Check timestamps for overall span
            for ts_key in ("timestamp", "ts", "time", "created_at"):
                ts_val = _parse_timestamp(event.get(ts_key))
                if ts_val is not None:
                    min_ts = ts_val if min_ts is None else min(min_ts, ts_val)
                    max_ts = ts_val if max_ts is None else max(max_ts, ts_val)

            # Check start/finish spans on individual event
            start_val = _parse_timestamp(event.get("started_at") or event.get("start_time"))
            end_val = _parse_timestamp(event.get("finished_at") or event.get("end_time") or event.get("completed_at"))
            if start_val is not None and end_val is not None:
                span = max(0.0, end_val - start_val)
                min_ts = start_val if min_ts is None else min(min_ts, start_val)
                max_ts = end_val if max_ts is None else max(max_ts, end_val)
            else:
                span = 0.0

            dur = _extract_duration(event) or span
            if dur > 0:
                telemetry_has_durations = True

            role = str(event.get("role", "")).lower()
            phase = str(event.get("phase", "")).lower()
            evt = str(event.get("event", "")).lower()
            cat = str(event.get("category", "")).lower()

            words = set()
            for s in (role, phase, evt, cat):
                if s:
                    for part in s.replace("_", " ").replace("-", " ").split():
                        words.add(part.lower())

            # Classify event
            if any(k in words for k in ("maker", "model", "execution", "worker", "planner")) or any(k in (role, phase, evt, cat) for k in ("model_exec", "maker")):
                model_exec_s += dur
            elif any(k in words for k in ("reviewer", "review", "critique", "auditor")) or any(k in (role, phase, evt, cat) for k in ("audit_node", "review")):
                review_s += dur
            elif any(k in words for k in ("check", "checks", "test", "tests", "verification", "uat", "lint", "build")):
                check_s += dur
            elif any(k in words for k in ("ceremony", "overhead", "preflight", "closeout", "checkpoint", "office", "presence", "schedule", "dispatch", "lock", "handshake", "coordination")):
                explicit_ceremony_s += dur

    # 2. Parse state.json if available
    if state and isinstance(state, Mapping):
        # Direct metric keys if telemetry didn't provide durations
        if not telemetry_has_durations:
            state_model = state.get("model_exec_s") or state.get("model_time_s")
            if isinstance(state_model, (int, float)):
                model_exec_s = float(state_model)

            state_review = state.get("review_s") or state.get("review_time_s")
            if isinstance(state_review, (int, float)):
                review_s = float(state_review)

            state_check = state.get("check_s") or state.get("check_time_s")
            if isinstance(state_check, (int, float)):
                check_s = float(state_check)

            state_ceremony = state.get("ceremony_s") or state.get("ceremony_time_s")
            if isinstance(state_ceremony, (int, float)):
                explicit_ceremony_s = float(state_ceremony)

        # Inspect tasks / nodes if present
        raw_tasks = state.get("tasks") or state.get("nodes")
        task_list: list[Mapping[str, Any]] = []
        if isinstance(raw_tasks, list):
            task_list = [t for t in raw_tasks if isinstance(t, Mapping)]
        elif isinstance(raw_tasks, dict):
            task_list = [t for t in raw_tasks.values() if isinstance(t, Mapping)]

        # Check nested metrics mapping if present
        state_metrics = state.get("metrics")
        if isinstance(state_metrics, Mapping) and not telemetry_has_durations:
            if model_exec_s == 0.0 and "model_exec_s" in state_metrics:
                model_exec_s = _extract_duration(state_metrics["model_exec_s"])
            if review_s == 0.0 and "review_s" in state_metrics:
                review_s = _extract_duration(state_metrics["review_s"])
            if check_s == 0.0 and "check_s" in state_metrics:
                check_s = _extract_duration(state_metrics["check_s"])
            if explicit_ceremony_s == 0.0 and "ceremony_s" in state_metrics:
                explicit_ceremony_s = _extract_duration(state_metrics["ceremony_s"])
            if lead_time_s == 0.0:
                lead_time_s = _extract_duration(state_metrics)

        if task_list and not telemetry_has_durations and (model_exec_s == 0.0 and review_s == 0.0):
            for t in task_list:
                model_exec_s += (
                    _extract_duration(t.get("maker"))
                    or _extract_duration(t.get("maker_timing"))
                    or _extract_duration(t.get("maker_duration_s"))
                    or _extract_duration(t.get("maker_time_s"))
                )

                review_s += (
                    _extract_duration(t.get("reviewer"))
                    or _extract_duration(t.get("reviewer_timing"))
                    or _extract_duration(t.get("reviewer_duration_s"))
                    or _extract_duration(t.get("reviewer_time_s"))
                )

                checks_val = t.get("checks")
                if isinstance(checks_val, list):
                    for c in checks_val:
                        check_s += _extract_duration(c)
                else:
                    check_s += (
                        _extract_duration(checks_val)
                        or _extract_duration(t.get("check_s"))
                        or _extract_duration(t.get("check_duration_s"))
                        or _extract_duration(t.get("check_time_s"))
                    )

                explicit_ceremony_s += (
                    _extract_duration(t.get("ceremony_s"))
                    or _extract_duration(t.get("ceremony_time_s"))
                    or _extract_duration(t.get("ceremony_duration_s"))
                )

        # Check state lead time or wall seconds
        state_lead = state.get("lead_time_s") or state.get("wall_seconds") or state.get("duration_s")
        if isinstance(state_lead, (int, float)):
            lead_time_s = float(state_lead)
        else:
            # Check timestamps in state
            started = _parse_timestamp(state.get("started_at") or state.get("created_at"))
            completed = _parse_timestamp(state.get("completed_at") or state.get("finished_at"))
            if started is not None and completed is not None:
                lead_time_s = max(0.0, completed - started)

    # 3. Determine lead_time_s
    if override_lead_time_s is not None:
        lead_time_s = max(0.0, float(override_lead_time_s))
    elif lead_time_s <= 0.0:
        if min_ts is not None and max_ts is not None and max_ts >= min_ts:
            lead_time_s = max_ts - min_ts
        else:
            # Sum components as fallback
            lead_time_s = model_exec_s + review_s + check_s + explicit_ceremony_s

    # 4. Determine ceremony_s
    # If explicit ceremony was recorded, prioritize it; otherwise unallocated lead time
    if explicit_ceremony_s > 0.0:
        ceremony_s = explicit_ceremony_s
    else:
        useful_work = model_exec_s + review_s + check_s
        ceremony_s = max(0.0, lead_time_s - useful_work)

    ceremony_s = round(ceremony_s, 4)
    model_exec_s = round(model_exec_s, 4)
    review_s = round(review_s, 4)
    check_s = round(check_s, 4)
    lead_time_s = round(lead_time_s, 4)

    ratio = compute_ceremony_ratio(ceremony_s, lead_time_s)
    alert = is_high_ceremony(ratio)

    details["unallocated_time_s"] = max(0.0, round(lead_time_s - (model_exec_s + review_s + check_s), 4))
    details["explicit_ceremony_s"] = round(explicit_ceremony_s, 4)

    return DeliveryMetrics(
        lead_time_s=lead_time_s,
        model_exec_s=model_exec_s,
        review_s=review_s,
        check_s=check_s,
        ceremony_s=ceremony_s,
        ceremony_ratio=ratio,
        high_ceremony_alert=alert,
        profile=profile,
        details=details,
    )


def load_state(path: str | Path) -> dict[str, Any]:
    """Read existing state.json safely without side effects."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"state file not found: {p}")
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_telemetry(path: str | Path) -> list[dict[str, Any]]:
    """Read existing telemetry.jsonl safely without side effects."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"telemetry file not found: {p}")
    records = []
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def load_and_compute_metrics(
    state_path: str | Path | None = None,
    telemetry_path: str | Path | None = None,
    run_dir: str | Path | None = None,
    profile: str = "custom",
) -> DeliveryMetrics:
    """Load existing state/telemetry files and passively compute metrics."""
    state_data = None
    telemetry_data = None

    if run_dir:
        rd = Path(run_dir)
        sp = rd / "state.json"
        tp = rd / "telemetry.jsonl"
        if sp.is_file():
            state_data = load_state(sp)
        if tp.is_file():
            telemetry_data = load_telemetry(tp)

    if state_path:
        state_data = load_state(state_path)
    if telemetry_path:
        telemetry_data = load_telemetry(telemetry_path)

    return compute_delivery_metrics(state=state_data, telemetry=telemetry_data, profile=profile)


def run_benchmark(profiles: Sequence[str] | None = None) -> dict[str, DeliveryMetrics]:
    """Evaluate S (inline TODO), M (single todo_pair), and L (parallel todo_graph) trace profiles."""
    selected_keys = list(profiles) if profiles else ["S", "M", "L"]
    results: dict[str, DeliveryMetrics] = {}
    for key in selected_keys:
        if key not in BENCHMARK_PROFILES:
            raise KeyError(f"Unknown benchmark profile: {key}. Available: {list(BENCHMARK_PROFILES.keys())}")
        spec = BENCHMARK_PROFILES[key]
        metrics = compute_delivery_metrics(
            state={
                "lead_time_s": spec["lead_time_s"],
                "model_exec_s": spec["model_exec_s"],
                "review_s": spec["review_s"],
                "check_s": spec["check_s"],
                "ceremony_s": spec["ceremony_s"],
            },
            profile=key,
        )
        results[key] = metrics
    return results


def format_benchmark_table(results: Mapping[str, DeliveryMetrics]) -> str:
    """Format benchmark results into a clean markdown/text table."""
    lines = [
        "| Profile | Lead Time (s) | Model Exec (s) | Review (s) | Check (s) | Ceremony (s) | Ceremony Ratio | High Ceremony Alert |",
        "|:--------|--------------:|---------------:|-----------:|----------:|-------------:|---------------:|:--------------------|",
    ]
    for key, m in results.items():
        alert_str = "ALERT (>0.25)" if m.high_ceremony_alert else "PASS"
        lines.append(
            f"| {key:<7} | {m.lead_time_s:>13.2f} | {m.model_exec_s:>14.2f} | {m.review_s:>10.2f} | {m.check_s:>9.2f} | {m.ceremony_s:>12.2f} | {m.ceremony_ratio:>14.4f} | {alert_str:<19} |"
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Passive delivery lead-time and ceremony_ratio telemetry.",
        prog="delivery_audit_metrics.py",
    )
    parser.add_argument(
        "--benchmark",
        action="store_true",
        help="Run benchmark evaluating S, M, L profiles.",
    )
    parser.add_argument(
        "--profile",
        "-p",
        choices=["S", "M", "L", "all"],
        default=None,
        help="Profile to evaluate (default: all).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output benchmark results as JSON.",
    )
    subparsers = parser.add_subparsers(dest="subcommand")

    # Benchmark subcommand
    bench_parser = subparsers.add_parser("benchmark", help="Evaluate S, M, L trace profiles.")
    bench_parser.add_argument(
        "profile_pos",
        nargs="?",
        choices=["S", "M", "L", "all"],
        default=None,
        help="Profile to evaluate (optional positional, e.g. S, M, L, all).",
    )
    bench_parser.add_argument(
        "--profile",
        "-p",
        choices=["S", "M", "L", "all"],
        default="all",
        help="Profile to evaluate (default: all).",
    )
    bench_parser.add_argument(
        "--json",
        action="store_true",
        help="Output benchmark results as JSON.",
    )

    # Compute subcommand
    compute_parser = subparsers.add_parser("compute", help="Compute metrics from state/telemetry files.")
    compute_parser.add_argument("--state", type=str, default=None, help="Path to state.json.")
    compute_parser.add_argument("--telemetry", type=str, default=None, help="Path to telemetry.jsonl.")
    compute_parser.add_argument("--run-dir", type=str, default=None, help="Path to directory containing state/telemetry.")
    compute_parser.add_argument("--profile", type=str, default="custom", help="Profile tag.")
    compute_parser.add_argument("--json", action="store_true", help="Output metrics as JSON.")

    args = parser.parse_args(argv)

    if args.subcommand == "benchmark" or getattr(args, "benchmark", False):
        chosen_profile = getattr(args, "profile_pos", None) or getattr(args, "profile", None) or "all"
        profile_keys = ["S", "M", "L"] if chosen_profile == "all" else [chosen_profile]
        results = run_benchmark(profile_keys)
        if getattr(args, "json", False):
            print(json.dumps({k: v.to_dict() for k, v in results.items()}, indent=2))
        else:
            print(format_benchmark_table(results))
        return 0

    if args.subcommand == "compute":
        metrics = load_and_compute_metrics(
            state_path=args.state,
            telemetry_path=args.telemetry,
            run_dir=args.run_dir,
            profile=args.profile,
        )
        if args.json:
            print(json.dumps(metrics.to_dict(), indent=2))
        else:
            alert_str = "ALERT (>0.25)" if metrics.high_ceremony_alert else "PASS"
            print(f"Profile: {metrics.profile}")
            print(f"Lead Time (s):   {metrics.lead_time_s:.2f}")
            print(f"Model Exec (s):  {metrics.model_exec_s:.2f}")
            print(f"Review (s):      {metrics.review_s:.2f}")
            print(f"Check (s):       {metrics.check_s:.2f}")
            print(f"Ceremony (s):    {metrics.ceremony_s:.2f}")
            print(f"Ceremony Ratio:  {metrics.ceremony_ratio:.4f}")
            print(f"Status:          {alert_str}")
        return 0

    # Default if no subcommand: run benchmark
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
