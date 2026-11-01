"""Read-only routing ledger: routing decision -> outcome, from existing run artifacts.

Sources (never modified):
  * Fleet run records   ~/.agents/agent-fleet/runs/run-*.json      -> "dispatch"
  * TODO role folders   <...>/<task>/<round>/<role>/ (request.json,
    assessment.json, tier-selection.json, preflight-skipped.json,
    result.json)                                                   -> "selection"
  * Graph run state     <...>/state.json with plan_hash + nodes     -> "node"
  * JEV reports         report-like *.json with consultation_status -> "jev"

The ledger keeps only typed features, candidates, choice, outcome and cost.
Free text (goals, task text, reasons, summaries, findings, paths, handoffs)
never leaves the source: reasons become fixed codes, paths and task ids
become short sha256 digests. Output is a new JSONL + summary under
~/.agents/private-artifacts/routing-ledger/ and is never overwritten.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "routing-ledger/v1"
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_JEV_BYTES = 256 * 1024
JEV_NAME = re.compile(r"jev|report|advice|routing|summary|reused", re.IGNORECASE)
PRUNE_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", "site-packages",
    ".pytest_cache", ".mypy_cache", "worktree", "worktrees", ".muse-tmp",
    "skills", "dist", "build", ".next", "App-worktrees",
}
# Code checkouts, staging snapshots and scratch copies hold fixtures, not run evidence.
PRUNE_PREFIXES = ("agent-stack-worktrees", "agent-stack-workspaces", ".shared-skills-staging", "tmp")
TOKEN_FIELDS = ("input_tokens", "fresh_input_tokens", "output_tokens",
                "cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
                "total_tokens")
FAILED_RUN_STATUSES = {"FAILED", "PARTIAL", "BLOCKED"}
NODE_SUCCESS = {"ACCEPTED"}

# Ordered: first match wins. Codes are the only trace of free-text reasons.
REASON_CODES = (
    ("ZCODE_READ_ADMISSION_REFUSED", r"zcode read admission refused"),
    ("ZCODE_ADMISSION_UNAVAILABLE", r"zcode read admission unavailable"),
    ("ZCODE_ADMISSION_TOO_MANY_FILES", r"zcode admission exceeds"),
    ("OWNER_DENIED_MODEL", r"owner-denied"),
    ("CATALOG_DENIED", r"catalog-denied|allowed by the model catalog"),
    ("NO_QUALIFIED_CANDIDATE", r"no qualified"),
    ("POOL_AVAILABILITY", r"pool availability"),
    ("PREFLIGHT_GATES", r"provider/model preflight"),
    ("LINEAGE_CONFLICT", r"lineage absent or equal"),
    ("MODE_UNSUPPORTED", r"does not support mode"),
    ("UNKNOWN_AGENT", r"unknown candidate agent"),
    ("IDENTITY_UNAVAILABLE", r"identity unavailable"),
    ("TRANSPORT_OR_IDENTITY_UNVERIFIED", r"transport or identity not verified|identity was not verified"),
    ("SECRET_IN_INPUT", r"secret material"),
    ("HANDOFF_MALFORMED", r"malformed json|handoff.{0,40}(?:invalid|unparsed|does not parse)"),
    ("HANDOFF_MISSING", r"no structurally valid handoff|no handoff|empty handoff"),
    ("PROVIDER_FAILURE", r"provider failure"),
    ("TIMEOUT", r"timeout|timed out"),
    ("QUOTA_OR_RATE_LIMIT", r"rate.?limit|quota|\b429\b|\b402\b|exhausted|usage limit"),
    ("TOOL_BUDGET", r"tool.?(?:call )?budget|step.?cap"),
    ("SANDBOX_OR_PERMISSION", r"sandbox|permission denied|access is denied|acl"),
    ("CANCELLED", r"cancel"),
)
_REASON_RE = [(code, re.compile(pattern, re.IGNORECASE)) for code, pattern in REASON_CODES]
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,79}$")
_ISO_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})?$")
KNOWN_EXTENSIONS = frozenset((
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".json", ".md", ".txt", ".yml", ".yaml",
    ".toml", ".ini", ".cfg", ".ps1", ".psm1", ".cmd", ".bat", ".sh", ".sql", ".html", ".css", ".scss",
    ".cs", ".go", ".rs", ".java", ".kt", ".swift", ".c", ".h", ".cpp", ".hpp", ".rb", ".php", ".vue",
    ".svelte", ".csv", ".xlsx", ".xml", ".svg", ".png", ".lock", ".env.example", ".mmd", ".base",
    ".canvas", ".jsonl", ".ipynb", ".r", ".lua", ".dart"))
_FILE_LIKE = re.compile(r"\.(?:md|py|json|jsonl|txt|ts|tsx|js|csv|xlsx?|docx?|pdf|html?|ya?ml|ps1|sql|log)$",
                        re.IGNORECASE)
KNOWN_GATES = frozenset((
    "routing-enabled", "lifecycle", "runtime-readiness", "runtime-model", "mode", "tools",
    "capability-floors", "risk-ceiling", "context-window", "red-team-only", "anthropic-retention",
    "quota-observed", "quota-not-exhausted"))


def utc_now():
    return datetime.now(timezone.utc)


def stamp(moment):
    return moment.strftime("%Y%m%dT%H%M%SZ")


def short_hash(value):
    if value is None:
        return None
    return hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest()[:16]


def safe(value):
    """Keep only identifier-like strings (agent, status codes); never prose or paths."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return num(value)
    if isinstance(value, str) and _SAFE_TOKEN.match(value) and not _FILE_LIKE.search(value):
        return value
    return "REDACTED"


def safe_time(value):
    return value if isinstance(value, str) and _ISO_TIME.match(value) else None


MODEL_PREFIXES = frozenset(("vllm", "openrouter", "ollama", "typesafe", "openai", "anthropic",
                            "google", "qwen", "deepseek", "moonshotai", "z-ai", "x-ai", "mistralai"))


def safe_model(value):
    """Model ids may carry one known provider prefix (vllm/Qwen3.8-27B); nothing path-like."""
    if isinstance(value, str) and value.count("/") == 1:
        head, tail = value.split("/")
        if head in MODEL_PREFIXES and safe(tail) == tail:
            return value
        return "REDACTED"
    return safe(value)


def num(value):
    # Source JSON may carry Infinity/NaN (json.load accepts them); the ledger stays strict JSON.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:  # integers beyond float range
        return None


def classify(text):
    if not text:
        return None
    text = str(text)
    for code, regex in _REASON_RE:
        if regex.search(text):
            return code
    return "OTHER"


def completion_code(reason):
    """Fleet completion_reason is already a machine code; fold it into a few classes."""
    reason = str(reason or "")
    if reason == "partial":
        return "PARTIAL_HANDOFF"
    if reason == "blocked":
        return "MODEL_REPORTED_BLOCKED"
    if reason.startswith("process-exit-"):
        return "PROCESS_EXIT_0" if reason == "process-exit-0" else "PROCESS_EXIT_NONZERO"
    if reason.startswith("model-stopped-with-"):
        return "HANDOFF_MALFORMED"
    if "step-cap" in reason:
        return "TOOL_BUDGET"
    return None


def dispatch_failure(data):
    text = " ".join(str(data.get(k) or "") for k in ("handoff_parse_error", "provider_terminal_reason"))
    code = classify(text) if text.strip() else None
    if code in (None, "OTHER"):
        code = completion_code(data.get("completion_reason")) or classify(data.get("completion_reason")) or code
    return code or "OTHER"


def gate_code(value):
    """Fleet hard-gate codes come from a fixed set; anything else is not copied."""
    if isinstance(value, str) and value in KNOWN_GATES:
        return value
    return "other-gate" if value else None


def gate_codes(text):
    """'Provider/model preflight: a, b' -> known gate codes only, never reason fragments."""
    if not isinstance(text, str) or "preflight:" not in text:
        return []
    tail = text.split("preflight:", 1)[1]
    return [gate_code(g.strip()) for g in tail.split(",") if g.strip()][:20]


def count(value):
    return len(value) if isinstance(value, list) else 0


def str_list(value, limit=12):
    return [safe(t) for t in value if isinstance(t, str)][:limit] if isinstance(value, list) else []


def load_json(path):
    try:
        if path.stat().st_size > MAX_JSON_BYTES:
            return None
        with path.open("r", encoding="utf-8-sig") as stream:
            return json.load(stream)
    except (OSError, ValueError, UnicodeError, RecursionError):
        return None


def tokens_of(usage):
    usage = usage if isinstance(usage, dict) else {}
    out = {"status": safe(usage.get("status")) or "missing"}
    for field in TOKEN_FIELDS:
        out[field] = num(usage.get(field))
    out["cost_usd"] = num(usage.get("cost_usd"))
    return out


def total(tokens):
    value = (tokens or {}).get("total_tokens")
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def fresh(tokens):
    """Non-cached input + output: closer to quota burn than totals that include cache reads."""
    tokens = tokens or {}
    inp = num(tokens.get("fresh_input_tokens"))
    if inp is None:
        inp = num(tokens.get("input_tokens"))
        cached = num(tokens.get("cache_read_tokens"))
        if inp is not None and cached is not None and cached <= inp:
            inp -= cached
    out = num(tokens.get("output_tokens"))
    if inp is None or out is None:
        return None  # half-known usage stays unknown instead of undercounting
    return inp + out


# ---------------------------------------------------------------- dispatch

def dispatch_record(data):
    route = data.get("route_event") if isinstance(data.get("route_event"), dict) else {}
    gates = route.get("gate_results") if isinstance(route.get("gate_results"), list) else []
    quota = data.get("quota_before") if isinstance(data.get("quota_before"), dict) else {}
    availability = quota.get("availability") if isinstance(quota.get("availability"), dict) else {}
    transport = data.get("transport_outcome") if isinstance(data.get("transport_outcome"), dict) else {}
    handoff = data.get("handoff") if isinstance(data.get("handoff"), dict) else {}
    run_status = safe(data.get("run_status"))
    return {
        "schema": SCHEMA,
        "type": "dispatch",
        "run_id": safe(data.get("run_id")),
        "started_at": safe_time(data.get("started_at")),
        "completed_at": safe_time(data.get("completed_at")),
        "root_task": short_hash(data.get("root_task_id")),
        "task_sha256": safe(data.get("task_sha256")),
        "features": {
            "kind": safe(data.get("kind")),
            "mode": safe(data.get("mode")),
            "complexity": safe(data.get("complexity")),
            "reasoning_level": safe(data.get("reasoning_level")),
            "prompt_bytes": num(data.get("prompt_bytes")),
            "web_allowed": data.get("web_allowed") if isinstance(data.get("web_allowed"), bool) else None,
        },
        "choice": {
            "agent": safe(data.get("agent")),
            "model": safe_model(data.get("model")),
            "lineage": safe(data.get("model_provider_id")),
            "adapter": safe(data.get("adapter")),
            "routing_mode": safe(route.get("routing_mode")),
            "assessment_source": safe(route.get("assessment_source")),
            "assignment_score": num(route.get("assignment_score")),
            "failed_gates": [gate_code(g.get("code")) for g in gates
                             if isinstance(g, dict) and g.get("passed") is False],
            "quota_status": safe(quota.get("status")),
            "quota_headroom": num(quota.get("headroom")),
            "availability": safe(availability.get("status")),
        },
        "outcome": {
            "run_status": run_status,
            "completion_reason": safe(data.get("completion_reason")),
            "transport_status": safe(transport.get("status")),
            "transport_reason": safe(transport.get("reason")),
            "handoff_valid": data.get("handoff_valid") if isinstance(data.get("handoff_valid"), bool) else None,
            "handoff_status": safe(data.get("handoff_status") or handoff.get("status")),
            "timed_out": data.get("timed_out") if isinstance(data.get("timed_out"), bool) else None,
            "exit_code": num(data.get("exit_code")),
            "failure_code": dispatch_failure(data) if run_status in FAILED_RUN_STATUSES else None,
            "continuations": num(data.get("continuation_count")),
            "model_tool_calls": num(data.get("model_tool_calls")),
        },
        "cost": {
            "tokens": tokens_of(data.get("token_usage")),
            "duration_s": num(data.get("duration_seconds")),
            "provider_execution_s": num(data.get("provider_execution_seconds")),
        },
    }


def linked_folders(output_dir):
    """Fleet writes <role>/fleet/<run>; the graph run sits a few levels above the role.

    Paths stay in memory only: they locate evidence across project roots and are
    never written to the ledger.
    """
    if not isinstance(output_dir, str) or not output_dir:
        return None, None
    base = Path(output_dir)
    role = next((p for p in (base, base.parent, base.parent.parent)
                 if (p / "request.json").is_file()), None)
    if role is None:
        return None, None
    graph = next((q for q in list(role.parents)[:5] if (q / "state.json").is_file()), None)
    return role, graph


def scan_dispatches(runs_dir):
    """Return dispatch records keyed by run_id plus linked role/graph folders."""
    records, roles, graphs = {}, set(), set()
    runs_dir = Path(runs_dir)
    if not runs_dir.is_dir():
        return records, roles, graphs
    for path in sorted(runs_dir.glob("run-*.json")):
        data = load_json(path)
        if not isinstance(data, dict) or not data.get("run_id"):
            continue
        record = dispatch_record(data)
        records[record["run_id"]] = record
        role, graph = linked_folders(data.get("output_dir"))
        if role is not None:
            roles.add(role)
        if graph is not None:
            graphs.add(graph)
    return records, roles, graphs


# ---------------------------------------------------------------- selection

def _ext_profile(paths):
    counts = Counter()
    for item in paths if isinstance(paths, list) else []:
        if isinstance(item, str):
            suffix = Path(item.replace("\\", "/")).suffix.lower()
            counts[suffix if suffix in KNOWN_EXTENSIONS else ("(none)" if not suffix else "(other)")] += 1
    return dict(counts.most_common(8))


def _candidates(rows):
    out = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict):
            out.append({"agent": safe(row.get("agent")), "model": safe_model(row.get("model")),
                        "score": num(row.get("score"))})
    return out[:20]


def _tier_rows(tier):
    rows = []
    ordered = tier.get("ordered") if isinstance(tier, dict) else None
    for row in ordered if isinstance(ordered, list) else []:
        if not isinstance(row, dict):
            continue
        rows.append({
            "agent": safe(row.get("agent")), "model": safe_model(row.get("model")),
            "tier": safe(row.get("tier")), "allowed": row.get("allowed") is True,
            "base": num(row.get("base")), "effective": num(row.get("effective")),
            "recent_share": num(row.get("recent_share")),
            "recency_penalty": num(row.get("recency_penalty")),
            "conserve_penalty": num(row.get("conserve_penalty")),
            "over_tier_penalty": num(row.get("over_tier_penalty")),
        })
    return rows[:20]


def selection_record(folder, files):
    request = load_json(folder / "request.json") if "request.json" in files else None
    if not isinstance(request, dict) or not isinstance(request.get("task"), dict):
        return None
    task = request["task"]
    role = request.get("role") if request.get("role") in ("maker", "reviewer") else (
        folder.name if folder.name in ("maker", "reviewer") else "unknown")
    assessment = load_json(folder / "assessment.json") if "assessment.json" in files else None
    assessment = assessment if isinstance(assessment, dict) else {}
    tier = load_json(folder / "tier-selection.json") if "tier-selection.json" in files else None
    skipped = load_json(folder / "preflight-skipped.json") if "preflight-skipped.json" in files else None
    result = load_json(folder / "result.json") if "result.json" in files else None
    result = result if isinstance(result, dict) else {}
    candidates_key = "maker" if role == "maker" else "reviewer"
    preflight = []
    for row in (skipped if isinstance(skipped, list) else []) + \
            (result.get("preflight") if isinstance(result.get("preflight"), list) else []):
        if isinstance(row, dict):
            preflight.append({"agent": safe(row.get("agent")), "model": safe_model(row.get("model")),
                              "code": classify(row.get("reason")),
                              "gates": gate_codes(row.get("reason"))})
    substitution = (tier or {}).get("substitution") if isinstance(tier, dict) else None
    picked = (substitution or {}).get("picked") if isinstance(substitution, dict) else None
    round_hint = folder.parent.name if folder.parent.name.isdigit() else None
    findings = result.get("findings")
    tokens = tokens_of(result.get("tokens"))
    status = safe(result.get("status"))
    return {
        "schema": SCHEMA,
        "type": "selection",
        "decision": short_hash(folder.resolve()),
        # <graph-run>/<task>/<round>/<role>: joins maker and reviewer of one round.
        "graph_run": short_hash(folder.resolve().parents[2]) if round_hint else None,
        "task": short_hash(task.get("id")),
        "role": safe(role),
        "round": int(round_hint) if round_hint else None,
        "features": {
            "mode": safe(task.get("mode") or ("review" if role == "reviewer" else "implement")),
            "kind": safe(task.get("kind")),
            "complexity": safe(assessment.get("complexity")),
            "risk": safe(assessment.get("risk")),
            "delegation_mode": safe(assessment.get("delegation_mode")),
            "delegation_value": num(assessment.get("delegation_value")),
            "required_tools": str_list(assessment.get("required_tools")),
            "required_capabilities": str_list(assessment.get("required_capabilities")),
            "workload_weights": {safe(k): num(v) for k, v in
                                 (assessment.get("workload_weights") or {}).items()}
            if isinstance(assessment.get("workload_weights"), dict) else {},
            "min_tier_execute": safe(task.get("min_tier_execute")),
            "reviewer_min_tier": safe(task.get("reviewer_min_tier")),
            "scope_files": count(task.get("scope")),
            "scope_ext": _ext_profile(task.get("scope")),
            "inputs": count(task.get("inputs")),
            "checks": count(task.get("checks")),
            "done_when": count(task.get("done_when")),
            "depends_on": count(task.get("depends_on")),
            "feedback_items": count(request.get("feedback")),
            "maker_lineage": safe((request.get("maker") or {}).get("lineage"))
            if isinstance(request.get("maker"), dict) else None,
        },
        "candidates": _candidates(task.get(candidates_key)),
        "tier_order": _tier_rows(tier),
        "tier_routing": bool(isinstance(tier, dict) and tier.get("enabled")),
        "substituted": bool(picked),
        "preflight_skipped": preflight[:20],
        "choice": {"agent": safe(result.get("agent")), "model": safe_model(result.get("model")),
                   "lineage": safe(result.get("lineage")), "run_id": safe(result.get("run_id"))},
        "outcome": {
            "status": status,
            "verdict": safe(result.get("verdict")),
            "findings": len(findings) if isinstance(findings, list) else None,
            "cause": safe(result.get("cause")),
            "failure_code": classify(result.get("reason")) if result.get("reason") else None,
            "model_tool_calls": num(result.get("model_tool_calls")),
        },
        "cost": {"tokens": tokens},
    }


# ---------------------------------------------------------------- node

def node_records(path, state):
    run = short_hash(path.parent.resolve())
    out = []
    for key, node in state.get("nodes", {}).items():
        if not isinstance(node, dict):
            continue
        steps, tokens_sum, fresh_sum, unknown = [], 0, 0, 0
        for entry in node.get("history", []) if isinstance(node.get("history"), list) else []:
            if not isinstance(entry, dict):
                continue
            result = entry.get("result") if isinstance(entry.get("result"), dict) else {}
            role = safe(entry.get("role"))
            usage = tokens_of(result.get("tokens")) if result else None
            value = total(usage)
            value_fresh = fresh(usage)
            if result and value is None and value_fresh is None and role in ("maker", "reviewer"):
                unknown += 1
            tokens_sum += value or 0
            fresh_sum += value_fresh or 0
            steps.append({"role": role, "agent": safe(result.get("agent")),
                          "model": safe_model(result.get("model")), "status": safe(result.get("status")),
                          "verdict": safe(result.get("verdict")), "run_id": safe(result.get("run_id")),
                          "cause": safe(result.get("cause")),
                          "failure_code": classify(result.get("reason")) if result.get("reason") else None,
                          "total_tokens": value, "fresh_tokens": value_fresh})
        status = safe(node.get("status"))
        out.append({
            "schema": SCHEMA, "type": "node", "graph_run": run, "task": short_hash(key),
            "status": status, "rounds": num(node.get("round")),
            "failure_code": classify(node.get("reason")) if node.get("reason") else None,
            "repairs": sum(1 for s in steps if s["verdict"] == "REPAIR"),
            "steps": steps,
            "known_tokens": tokens_sum, "fresh_tokens": fresh_sum, "unknown_token_steps": unknown,
            "production_deployed": state.get("production_deployed") is True,
        })
    return out


# ---------------------------------------------------------------- jev

def jev_record(path, data):
    decision = data.get("decision") if isinstance(data.get("decision"), dict) else {}
    usage = decision.get("usage") if isinstance(decision.get("usage"), dict) else {}
    rec = data.get("recommendation") if isinstance(data.get("recommendation"), dict) else {}
    fit = decision.get("fit") if isinstance(decision.get("fit"), dict) else {}
    return {
        "schema": SCHEMA, "type": "jev", "report": short_hash(path.resolve()),
        "created_at": safe_time(data.get("created_at")),
        "task": short_hash((data.get("binding") or {}).get("task_id"))
        if isinstance(data.get("binding"), dict) else None,
        "decision_id": safe(decision.get("decision_id")),
        "consultation_status": safe(data.get("consultation_status")),
        "error_code": safe(data.get("consultation_error_code")),
        "difficulty": safe(decision.get("difficulty")),
        "parallelism": safe(decision.get("parallelism")),
        "consultation": safe(decision.get("consultation")),
        "fit": {safe(k): num(v if not isinstance(v, dict) else v.get("score")) for k, v in fit.items()},
        "requires_parent_review": rec.get("requires_parent_review") is True,
        "cost_usd": num(usage.get("cost_usd") if "cost_usd" in usage else usage.get("cost")),
    }


# ---------------------------------------------------------------- walk

def walk(roots, folders=(), skip=None):
    """Walk roots (pruned), then single linked folders; each folder is visited once."""
    visited = {os.path.normcase(os.path.abspath(skip))} if skip else set()
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for current, dirs, files in os.walk(root, onerror=lambda _e: None):
            dirs[:] = [d for d in dirs if d not in PRUNE_DIRS
                       and not d.startswith(PRUNE_PREFIXES)]
            key = os.path.normcase(os.path.abspath(current))
            if key in visited:
                dirs[:] = []
                continue
            visited.add(key)
            yield Path(current), set(files)
    for folder in folders:
        key = os.path.normcase(os.path.abspath(folder))
        if key in visited:
            continue
        visited.add(key)
        try:
            files = {entry.name for entry in os.scandir(folder) if entry.is_file()}
        except OSError:
            continue
        yield Path(folder), files


def scan_tree(roots, folders=(), skip=None):
    selections, nodes, jevs = {}, [], {}
    seen_states = set()
    for folder, files in walk(roots, folders, skip):
        if "request.json" in files and ({"tier-selection.json", "result.json",
                                         "preflight-skipped.json"} & files):
            record = selection_record(folder, files)
            if record is not None:
                # Mirrors copy a role folder; one Fleet run is one decision.
                key = record["choice"]["run_id"] or json.dumps(
                    {k: v for k, v in record.items() if k not in ("decision", "graph_run")},
                    sort_keys=True)
                selections.setdefault(key, record)
        if "state.json" in files:
            state = load_json(folder / "state.json")
            if isinstance(state, dict) and "plan_hash" in state and isinstance(state.get("nodes"), dict):
                key = hashlib.sha256(json.dumps([state.get("plan_hash"), state["nodes"]], sort_keys=True,
                                                default=str).encode("utf-8", "replace")).hexdigest()
                if key not in seen_states:
                    seen_states.add(key)
                    nodes.extend(node_records(folder / "state.json", state))
        for name in files:
            if JEV_NAME.search(name) and name.endswith(".json") and "example" not in name \
                    and name != "jev-funding.json":
                path = folder / name
                try:
                    if path.stat().st_size > MAX_JEV_BYTES:
                        continue
                except OSError:
                    continue
                data = load_json(path)
                if isinstance(data, dict) and "consultation_status" in data and "binding" in data:
                    record = jev_record(path, data)
                    # Wrappers copy one API answer into reused/summary/variant reports.
                    usable_id = record["decision_id"] not in (None, "REDACTED")
                    jevs.setdefault(record["decision_id"] if usable_id else json.dumps(
                        {k: v for k, v in record.items() if k != "report"}, sort_keys=True), record)
    return list(selections.values()), nodes, list(jevs.values())


# ---------------------------------------------------------------- summary

def summarize(dispatches, selections, nodes, jevs):
    """Totals use fresh tokens (non-cached input + output); adapters disagree on totals."""
    by_status = Counter(d["outcome"]["run_status"] for d in dispatches)
    failed = [d for d in dispatches if d["outcome"]["run_status"] in FAILED_RUN_STATUSES]

    def fr(d):
        return fresh(d["cost"]["tokens"])

    waste_by_code, waste_by_agent, waste_by_status = Counter(), Counter(), Counter()
    failures_by_agent = Counter()
    for d in failed:
        value = fr(d) or 0
        waste_by_code[d["outcome"]["failure_code"] or "UNCLASSIFIED"] += value
        waste_by_agent[d["choice"]["agent"]] += value
        waste_by_status[d["outcome"]["run_status"]] += value
        failures_by_agent[d["choice"]["agent"]] += 1
    skipped, skipped_by_agent = Counter(), Counter()
    for s in selections:
        for row in s["preflight_skipped"]:
            skipped[row["code"]] += 1
            skipped_by_agent[(row["agent"], row["code"])] += 1
    zcode = "ZCODE_READ_ADMISSION_REFUSED"
    # Resumed graphs copy earlier history into a new state.json: count each Fleet run once.
    def unique_fresh(step_rows):
        seen, value = set(), 0
        for step in step_rows:
            key = step["run_id"] or json.dumps(step, sort_keys=True)
            if key not in seen:
                seen.add(key)
                value += step["fresh_tokens"] or 0
        return value

    rework_steps = []
    for n in nodes:
        steps = n["steps"]
        for i, step in enumerate(steps):
            if step["role"] == "maker":
                later = next((s for s in steps[i + 1:] if s["role"] == "reviewer"), None)
                if later and later["verdict"] == "REPAIR":
                    rework_steps.append(step)
    zcode_nodes = [n for n in nodes if n["failure_code"] == zcode
                   or any(s["failure_code"] == zcode for s in n["steps"])]
    zcode_stranded = [s for n in zcode_nodes for s in n["steps"]
                      if s["role"] == "maker" and s["fresh_tokens"]]
    agent_outcomes = defaultdict(Counter)
    for d in dispatches:
        agent_outcomes[d["choice"]["agent"]][d["outcome"]["run_status"]] += 1
    dispatch_by_run = {d["run_id"]: d for d in dispatches}
    linked = sum(1 for s in selections if s["choice"]["run_id"] in dispatch_by_run)
    return {
        "schema": SCHEMA,
        "token_metric": "fresh = non-cached input + output (cache reads excluded)",
        "counts": {
            "dispatches": len(dispatches),
            "selections": len(selections),
            "selections_linked_to_dispatch": linked,
            "selections_never_dispatched": sum(1 for s in selections if not s["choice"]["run_id"]),
            "selections_with_tier_order": sum(1 for s in selections if s["tier_routing"]),
            "selections_substituted": sum(1 for s in selections if s["substituted"]),
            "selections_with_preflight_skips": sum(1 for s in selections if s["preflight_skipped"]),
            "graph_nodes": len(nodes),
            "jev_reports": len(jevs),
            "jev_available": sum(1 for j in jevs if j["consultation_status"] == "AVAILABLE"),
        },
        "dispatch_status": dict(by_status.most_common()),
        "dispatch_failure_codes": dict(Counter(d["outcome"]["failure_code"] or "UNCLASSIFIED"
                                               for d in failed).most_common()),
        "preflight_skips": dict(skipped.most_common()),
        "preflight_skips_by_agent": {f"{a}|{c}": n for (a, c), n in skipped_by_agent.most_common(30)},
        "zcode_read_admission_refused": {
            "preflight_skips": skipped.get(zcode, 0),
            "dispatch_failures": sum(1 for d in dispatches if d["outcome"]["failure_code"] == zcode),
            "graph_node_records": len(zcode_nodes),
            "graph_unique_tasks": len({n["task"] for n in zcode_nodes}),
            "stranded_maker_fresh_tokens": unique_fresh(zcode_stranded),
            "stranded_maker_runs": len({s["run_id"] for s in zcode_stranded if s["run_id"]}),
            "blocked_role_selections": sum(1 for s in selections if s["outcome"]["failure_code"] == zcode),
        },
        "selection_failure_codes": dict(Counter(
            f'{s["role"]}|{s["outcome"]["failure_code"]}' for s in selections
            if s["outcome"]["failure_code"]).most_common()),
        "tokens": {
            "fresh_total": sum(fr(d) or 0 for d in dispatches),
            "reported_total_incl_cache_where_adapter_counts_it": sum(total(d["cost"]["tokens"]) or 0
                                                                     for d in dispatches),
            "dispatches_with_known_tokens": sum(1 for d in dispatches if fr(d) is not None),
            "wasted_fresh_failed_partial_blocked": sum(waste_by_status.values()),
            "wasted_fresh_by_status": dict(waste_by_status.most_common()),
            "wasted_unknown_token_dispatches": sum(1 for d in failed if fr(d) is None),
            "wasted_fresh_by_failure_code": dict(waste_by_code.most_common()),
            "wasted_fresh_by_agent": dict(waste_by_agent.most_common()),
            "rework_fresh_maker_before_repair": unique_fresh(rework_steps),
            "graph_unaccepted_node_fresh": unique_fresh(
                [s for n in nodes if str(n["status"]).upper() not in NODE_SUCCESS for s in n["steps"]]),
        },
        "failures_by_agent": dict(failures_by_agent.most_common()),
        "agent_outcomes": {str(a): dict(c) for a, c in sorted(agent_outcomes.items(), key=lambda x: str(x[0]))},
        "graph_node_status": dict(Counter(n["status"] for n in nodes).most_common()),
        "review_verdicts": dict(Counter(str(s["outcome"]["verdict"]) for s in selections
                                        if s["role"] == "reviewer").most_common()),
        "jev_cost_usd_known": sum(j["cost_usd"] or 0 for j in jevs),
    }


def write_new(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(text)


def check_out_dir(out_dir, roots, runs_dir):
    out = Path(out_dir).resolve()
    runs = Path(runs_dir).resolve()
    if out == runs or runs in out.parents or out in runs.parents:
        raise ValueError("OUT_DIR_OVERLAPS_FLEET_RUNS")
    for root in roots:
        if out == Path(root).resolve():
            raise ValueError("OUT_DIR_IS_A_SOURCE_ROOT")
    return out


def build(roots, runs_dir, out_dir, *, now=None):
    moment = now or utc_now()
    out_resolved = check_out_dir(out_dir, roots, runs_dir)
    found, roles, graphs = scan_dispatches(runs_dir)
    dispatches = list(found.values())
    # Graph runs live in many project roots; walk each linked run, then any unwalked role folder.
    selections, nodes, jevs = scan_tree(list(roots) + sorted(graphs), sorted(roles), skip=out_resolved)
    summary = summarize(dispatches, selections, nodes, jevs)
    summary["generated_at"] = moment.isoformat().replace("+00:00", "Z")
    summary["source_roots"] = [short_hash(Path(r).resolve()) for r in roots]
    out_dir = Path(out_dir)
    tag = stamp(moment)
    ledger = out_dir / f"ledger-{tag}.jsonl"
    lines = [json.dumps(r, ensure_ascii=True, sort_keys=True, allow_nan=False)
             for r in dispatches + selections + nodes + jevs]
    write_new(ledger, "\n".join(lines) + ("\n" if lines else ""))
    summary["ledger_file"] = ledger.name
    summary_path = out_dir / f"summary-{tag}.json"
    write_new(summary_path, json.dumps(summary, ensure_ascii=True, indent=2, allow_nan=False) + "\n")
    return ledger, summary_path, summary


def default_roots():
    stack = Path(__file__).resolve().parents[2]
    roots = [Path.home() / ".agents", stack / "docs", stack / "outputs", stack.parent / "research-runs"]
    return [root for root in roots if root.is_dir()]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="scan artifacts and write a new ledger + summary")
    b.add_argument("--root", action="append", dest="roots",
                   help="directory to scan for TODO role folders/graph state (repeatable)")
    b.add_argument("--runs", default=str(Path.home() / ".agents" / "agent-fleet" / "runs"))
    b.add_argument("--out-dir", default=str(Path.home() / ".agents" / "private-artifacts" / "routing-ledger"))
    args = parser.parse_args(argv)
    roots = args.roots or default_roots()
    ledger, summary_path, summary = build(roots, args.runs, args.out_dir)
    print(json.dumps({"ledger": str(ledger), "summary": str(summary_path),
                      "counts": summary["counts"],
                      "zcode_read_admission_refused": summary["zcode_read_admission_refused"],
                      "tokens": {k: summary["tokens"][k] for k in (
                          "fresh_total", "wasted_fresh_failed_partial_blocked",
                          "wasted_unknown_token_dispatches",
                          "rework_fresh_maker_before_repair", "graph_unaccepted_node_fresh")}},
                     indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
