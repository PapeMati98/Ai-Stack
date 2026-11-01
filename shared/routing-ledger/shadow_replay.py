"""Step 2a: offline replay of historical routing decisions on a local model.

Reads only the private routing ledger (codes, hashes, counts) and asks the local
vLLM Qwen to score the candidates of each round-1 maker decision. Compares that
score with two baselines on a time-ordered split (train = earliest 70%):

  B0  planner score of the chosen candidate (current deterministic routing)
  B1  smoothed acceptance rates per pool learned on the train period only
  B2  local Qwen zero-shot fit for the chosen candidate (no training)

Label: 0 when the round-1 maker failed (BLOCKED/PARTIAL/FAILED) or the round-1
reviewer asked for REPAIR; 1 when the reviewer ACCEPTed a maker that did not fail.
Never changes routing, never calls a paid API.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import statistics
import sys
import time
import re
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ENDPOINT = "http://127.0.0.1:11440/v1"
MODEL = "Qwen3.8-27B"
TRAIN_FRACTION = 0.7
PRIOR_STRENGTH = 10.0
PROMPT_FEATURES = ("kind", "mode", "complexity", "risk", "delegation_mode", "required_tools",
                   "required_capabilities", "min_tier_execute", "scope_files", "scope_ext", "inputs",
                   "checks", "done_when", "depends_on")
MAKER_FAILED = frozenset(("BLOCKED", "PARTIAL", "FAILED"))
# Prompt fields are closed vocabularies: an unexpected value becomes "other", never free text.
ENUMS = {
    "kind": {"implementation", "docs", "script-task", "coding", "code", "review", "frontend", "backend",
             "architecture", "research", "debugging", "testing", "refactor"},
    "mode": {"implement", "advisory", "review"},
    "complexity": {"low", "medium", "high"},
    "risk": {"low", "medium", "high"},
    "delegation_mode": {"ONE", "MANY", "NONE"},
    "min_tier_execute": {"D", "M", "K", "W"},
}
LIST_ENUMS = {
    "required_tools": {"filesystem-read", "filesystem-write", "shell", "web", "network", "git", "browser"},
    "required_capabilities": {"read", "write", "shell", "web"},
}
COUNT_FIELDS = ("scope_files", "inputs", "checks", "done_when", "depends_on")
TIERS = {"D", "M", "K", "W", "DENY"}
# K4 failure population exactly as SHADOW-PLAN.md: PROVIDER_FAILURE, HANDOFF_*, TIMEOUT, ZCODE_*.
ROUTING_FAILURES = frozenset(("PROVIDER_FAILURE", "HANDOFF_MALFORMED", "HANDOFF_MISSING", "TIMEOUT",
                              "ZCODE_READ_ADMISSION_REFUSED", "ZCODE_ADMISSION_UNAVAILABLE",
                              "ZCODE_ADMISSION_TOO_MANY_FILES"))
LOW_QUANTILE = 0.2
_POOL = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")
_MODEL = re.compile(r"^(?:[a-z0-9-]{2,20}/)?[A-Za-z0-9][A-Za-z0-9._-]{1,59}$")
SYSTEM = ("You route software tasks to AI coding agents. For each candidate, estimate the probability "
          "(0-100) that its work on this task is ACCEPTED by an independent reviewer in the first round "
          "without repair, and is not blocked by a transport or handoff failure. Use only the given fields. "
          "Return JSON only.")


# ---------------------------------------------------------------- dataset

def load_ledger(path):
    selections, dispatches = [], {}
    with open(path, encoding="utf-8") as stream:
        for line in stream:
            record = json.loads(line)
            if record["type"] == "selection":
                selections.append(record)
            elif record["type"] == "dispatch":
                dispatches[record["run_id"]] = record
    return selections, dispatches


def build_dataset(selections, dispatches):
    """Round-1 maker decisions with a label and a dispatch time, oldest first."""
    verdict = {}
    for s in selections:
        if s["role"] == "reviewer" and s["outcome"]["verdict"] in ("ACCEPT", "REPAIR"):
            verdict[(s["graph_run"], s["task"], s["round"])] = s["outcome"]["verdict"]
    rows = []
    for s in selections:
        if s["role"] != "maker" or s["round"] != 1 or not s["choice"]["agent"]:
            continue
        v = verdict.get((s["graph_run"], s["task"], 1))
        if s["outcome"]["status"] in MAKER_FAILED or v == "REPAIR":
            label = 0  # a failed round-1 maker is never a clean first-round acceptance
        elif v == "ACCEPT":
            label = 1
        else:
            continue
        dispatch = dispatches.get(s["choice"]["run_id"])
        if not dispatch or not dispatch.get("started_at"):
            continue
        features = dict(s["features"])
        # The dispatch carries the planner's kind/complexity when the role folder did not.
        for key in ("kind", "mode", "complexity"):
            if not features.get(key):
                features[key] = dispatch["features"].get(key)
        failed = s["outcome"]["status"] in MAKER_FAILED
        rows.append({"id": s["decision"], "graph": s["graph_run"], "time": dispatch["started_at"], "label": label,
                     # evaluation-only fields; never part of the prompt
                     "routing_failure": failed and s["outcome"].get("failure_code") in ROUTING_FAILURES,
                     "gate_failed": sorted({(p["agent"], p["model"]) for p in s.get("preflight_skipped") or []}),
                     "features": features, "candidates": s["candidates"], "tier_order": s["tier_order"],
                     "chosen": {"agent": s["choice"]["agent"], "model": s["choice"]["model"]}})
    rows.sort(key=lambda r: (r["time"], r["id"]))
    return rows


def split(rows, fraction=TRAIN_FRACTION):
    """Time split; rows sharing the boundary timestamp all go to test (train strictly older)."""
    cut = int(len(rows) * fraction)
    while 0 < cut < len(rows) and rows[cut - 1]["time"] == rows[cut]["time"]:
        cut -= 1
    return rows[:cut], rows[cut:]


# ---------------------------------------------------------------- candidates and prompt

def candidate_table(row):
    """c1..cn from the planner list (+ the chosen pool if it was substituted); no outcome fields."""
    tiers = {(t["agent"], t["model"]): t for t in row["tier_order"]}
    seen, table = set(), []
    for c in row["candidates"]:
        key = (c["agent"], c["model"])
        if key in seen or not c["agent"]:
            continue
        seen.add(key)
        table.append({"agent": c["agent"], "model": c["model"], "planner_score": c["score"]})
    chosen = (row["chosen"]["agent"], row["chosen"]["model"])
    if chosen not in seen:
        table.append({"agent": chosen[0], "model": chosen[1], "planner_score": None})
    for i, c in enumerate(table, 1):
        t = tiers.get((c["agent"], c["model"]), {})
        c.update(id=f"c{i}", tier=t.get("tier"), tier_effective=t.get("effective"),
                 recent_share=t.get("recent_share"))
    return table


def chosen_id(row, table):
    key = (row["chosen"]["agent"], row["chosen"]["model"])
    return next(c["id"] for c in table if (c["agent"], c["model"]) == key)


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def task_fields(features):
    """Closed-vocabulary projection of ledger features; no field can carry free text."""
    out = {}
    for key, allowed in ENUMS.items():
        value = features.get(key)
        out[key] = value if value in allowed else (None if value is None else "other")
    for key, allowed in LIST_ENUMS.items():
        values = features.get(key) if isinstance(features.get(key), list) else []
        out[key] = sorted({v if v in allowed else "other" for v in values if isinstance(v, str)})
    for key in COUNT_FIELDS:
        out[key] = features.get(key) if isinstance(features.get(key), int) and not isinstance(features.get(key), bool) else 0
    ext = features.get("scope_ext") if isinstance(features.get("scope_ext"), dict) else {}
    out["scope_ext"] = {k: v for k, v in ext.items()
                        if isinstance(k, str) and re.fullmatch(r"\.[a-z0-9]{1,8}|\((?:none|other)\)", k)
                        and isinstance(v, int)}
    return out


def candidate_fields(c):
    return {"id": c["id"],
            "agent": c["agent"] if isinstance(c["agent"], str) and _POOL.match(c["agent"]) else "other",
            "model": c["model"] if isinstance(c["model"], str) and _MODEL.match(c["model"]) else "other",
            "tier": c.get("tier") if c.get("tier") in TIERS else None,
            "planner_score": _number(c.get("planner_score")),
            "tier_effective": _number(c.get("tier_effective")),
            "recent_share": _number(c.get("recent_share"))}


def build_prompt(row, table):
    task = task_fields(row["features"])
    table = [candidate_fields(c) for c in table]
    payload = {"task": task, "candidates": table}
    return json.dumps(payload, ensure_ascii=True, sort_keys=True)


def schema_for(table):
    props = {c["id"]: {"type": "integer", "minimum": 0, "maximum": 100} for c in table}
    return {"type": "object", "properties": {"fit": {"type": "object", "properties": props,
                                                     "required": list(props), "additionalProperties": False}},
            "required": ["fit"], "additionalProperties": False}


# ---------------------------------------------------------------- local model

def queue_waiting(endpoint=ENDPOINT, timeout=5):
    """Requests waiting in vLLM; OpenClaw has priority, so the replay yields to a queue."""
    url = endpoint.rsplit("/v1", 1)[0] + "/metrics"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            for line in response.read().decode("utf-8", "replace").splitlines():
                if line.startswith("vllm:num_requests_waiting{"):
                    return float(line.rsplit(" ", 1)[1])
    except (OSError, ValueError):
        return None
    return 0.0


def ask_local(prompt, table, *, endpoint=ENDPOINT, timeout=120):
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost") \
            or parsed.username or parsed.password or "@" in parsed.netloc:
        raise ValueError("LOCAL_ENDPOINT_ONLY")
    body = {"model": MODEL, "temperature": 0, "max_tokens": 40 + 12 * len(table),
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "fit", "schema": schema_for(table)}}}
    request = urllib.request.Request(endpoint + "/chat/completions", data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    latency = time.monotonic() - started
    fit = json.loads(data["choices"][0]["message"]["content"])["fit"]
    ids = {c["id"] for c in table}
    if set(fit) != ids or not all(isinstance(v, int) and 0 <= v <= 100 for v in fit.values()):
        raise ValueError("SCHEMA_MISMATCH")
    usage = data.get("usage") or {}
    return fit, latency, usage.get("prompt_tokens"), usage.get("completion_tokens")


def replay(rows, out_path, *, ask=ask_local, waiting=queue_waiting, pause=30.0, log=print):
    """Append one result per decision; resumable (done ids are skipped)."""
    done = set()
    if out_path.exists():
        with open(out_path, encoding="utf-8") as stream:
            done = {json.loads(line)["id"] for line in stream if line.strip()}
    with open(out_path, "a", encoding="utf-8", newline="\n") as stream:
        for n, row in enumerate(rows, 1):
            if row["id"] in done:
                continue
            while (waiting() or 0) > 0:
                time.sleep(pause)
            table = candidate_table(row)
            record = {"id": row["id"], "chosen": chosen_id(row, table), "n_candidates": len(table)}
            try:
                fit, latency, p_tok, c_tok = ask(build_prompt(row, table), table)
                record.update(status="OK", fit=fit, latency_s=round(latency, 3),
                              prompt_tokens=p_tok, completion_tokens=c_tok)
            except Exception as exc:  # recorded, never retried silently
                record.update(status="ERROR", error=type(exc).__name__ + ":" + str(exc)[:80])
            stream.write(json.dumps(record, sort_keys=True) + "\n")
            stream.flush()
            if n % 25 == 0:
                log(f"{n}/{len(rows)}")


# ---------------------------------------------------------------- baselines and metrics

def b0_score(row):
    """Planner score of the chosen pool; a pool the planner did not list scores 0 (lowest preference)."""
    for c in row["candidates"]:
        if (c["agent"], c["model"]) == (row["chosen"]["agent"], row["chosen"]["model"]) \
                and isinstance(c["score"], (int, float)):
            return float(c["score"])
    return 0.0


def fit_b1(train):
    """Empirical-Bayes rates: global -> pool -> pool x complexity -> pool x complexity x kind."""
    glob = sum(r["label"] for r in train) / len(train)
    stats = defaultdict(lambda: [0, 0])
    for r in train:
        for key in b1_keys(r):
            stats[key][0] += r["label"]
            stats[key][1] += 1
    return glob, dict(stats)


def scope_bucket(n):
    n = n if isinstance(n, int) else 0
    return "0" if n == 0 else "1-2" if n <= 2 else "3-5" if n <= 5 else "6+"


def b1_keys(row):
    """Back-off chain from the pool down to the task shape named in SHADOW-PLAN (B1)."""
    f, chain, keys = row["features"], [row["chosen"]["agent"]], []
    for part in (f.get("mode"), f.get("kind"), f.get("complexity"), f.get("risk"), scope_bucket(f.get("scope_files"))):
        keys.append(tuple(chain))
        chain.append(part)
    keys.append(tuple(chain))
    return keys


def b1_score(row, model):
    glob, stats = model
    p = glob
    for key in b1_keys(row):
        pos, n = stats.get(key, (0, 0))
        p = (pos + PRIOR_STRENGTH * p) / (n + PRIOR_STRENGTH)
    return p


def auc(scores, labels):
    """Mann-Whitney AUC with ties counted as half."""
    pairs = sorted(zip(scores, labels))
    pos = sum(labels)
    neg = len(labels) - pos
    if not pos or not neg:
        return None
    rank_sum, i = 0.0, 0
    while i < len(pairs):
        j = i
        while j < len(pairs) and pairs[j][0] == pairs[i][0]:
            j += 1
        mid = (i + 1 + j) / 2
        rank_sum += mid * sum(1 for k in range(i, j) if pairs[k][1] == 1)
        i = j
    return (rank_sum - pos * (pos + 1) / 2) / (pos * neg)


def bootstrap_diff(a, b, labels, *, clusters=None, iterations=2000, seed=7):
    """Paired AUC difference; with clusters, whole graphs are resampled (correlated decisions)."""
    rnd = random.Random(seed)
    n, diffs = len(labels), []
    groups = defaultdict(list)
    for i in range(n):
        groups[clusters[i] if clusters else i].append(i)
    keys = list(groups)
    for _ in range(iterations):
        idx = [i for _k in range(len(keys)) for i in groups[keys[rnd.randrange(len(keys))]]]
        la = [labels[i] for i in idx]
        x, y = auc([a[i] for i in idx], la), auc([b[i] for i in idx], la)
        if x is not None and y is not None:
            diffs.append(x - y)
    if not diffs:
        return None
    diffs.sort()
    return {"mean": statistics.fmean(diffs), "ci95": [diffs[int(0.025 * len(diffs))], diffs[int(0.975 * len(diffs)) - 1]]}


def evaluate(train, test, results):
    by_id = {r["id"]: r for r in results}
    model = fit_b1(train)
    ok = [r for r in test if by_id.get(r["id"], {}).get("status") == "OK"]
    labels = [r["label"] for r in ok]
    s0 = [b0_score(r) for r in ok]
    s1 = [b1_score(r, model) for r in ok]
    s2 = [float(by_id[r["id"]]["fit"][by_id[r["id"]]["chosen"]]) for r in ok]
    graphs = [r.get("graph") or r["id"] for r in ok]
    deny_top = gate_top = 0
    for r in ok:
        fit, table = by_id[r["id"]]["fit"], {c["id"]: c for c in candidate_table(r)}
        best = max(fit.values())
        tops = [table.get(cid, {}) for cid, value in fit.items() if value == best]  # every tied maximum
        gated = [list(g) for g in r.get("gate_failed", [])]
        deny_top += any(t.get("tier") == "DENY" for t in tops)
        gate_top += any([t.get("agent"), t.get("model")] in gated for t in tops)
    routing_failed = [bool(r.get("routing_failure")) for r in ok]

    def k4(scores):
        """Tie-neutral precision of exactly the bottom 20%: rows tied at the cut count fractionally."""
        if not scores:
            return None
        k = max(1, round(LOW_QUANTILE * len(scores)))
        cut = sorted(scores)[k - 1]
        below = [f for sc, f in zip(scores, routing_failed) if sc < cut]
        at = [f for sc, f in zip(scores, routing_failed) if sc == cut]
        weight = (k - len(below)) / len(at)
        hits = sum(below) + weight * sum(at)
        return {"threshold": cut, "n_low": k, "n_below": len(below), "n_tied_at_cut": len(at),
                "precision": hits / k}
    lat = sorted(by_id[r["id"]]["latency_s"] for r in ok)
    attempted = [r for r in test if r["id"] in by_id]
    multi = [r for r in ok if by_id[r["id"]]["n_candidates"] > 1]
    top = Counter()
    for r in multi:
        fit = by_id[r["id"]]["fit"]
        is_top = fit[by_id[r["id"]]["chosen"]] == max(fit.values())
        top[(r["label"], is_top)] += 1
    kinds = {r["features"].get("kind") or r["features"].get("mode") for r in ok}
    pools = {r["chosen"]["agent"] for r in ok}
    out = {
        "n_train": len(train), "n_attempted": len(attempted),
        "n_substituted_chosen": sum(1 for r in ok if by_id[r["id"]]["chosen"] not in
                                    {c["id"] for c in candidate_table(r) if c["planner_score"] is not None}),
        "test_kinds": len(kinds), "test_pools": len(pools), "n_test": len(test), "n_test_scored": len(ok),
        "test_positive_rate": sum(labels) / len(labels) if labels else None,
        "train_period": [train[0]["time"], train[-1]["time"]] if train else None,
        "test_period": [test[0]["time"], test[-1]["time"]] if test else None,
        "schema_valid_rate": len(ok) / len(attempted) if attempted else None,
        "latency_p50_s": lat[len(lat) // 2] if lat else None,
        "latency_p95_s": lat[int(0.95 * (len(lat) - 1))] if lat else None,
        "auc": {"B0_planner": auc(s0, labels), "B1_rates": auc(s1, labels), "B2_qwen": auc(s2, labels)},
        "auc_diff": {"B2-B0": bootstrap_diff(s2, s0, labels), "B2-B1": bootstrap_diff(s2, s1, labels),
                     "B1-B0": bootstrap_diff(s1, s0, labels)} if len(set(labels)) == 2 else None,
        "n_graph_clusters": len(set(graphs)),
        "auc_diff_graph_clustered": {
            "B2-B0": bootstrap_diff(s2, s0, labels, clusters=graphs),
            "B2-B1": bootstrap_diff(s2, s1, labels, clusters=graphs),
            "B1-B0": bootstrap_diff(s1, s0, labels, clusters=graphs)} if len(set(labels)) == 2 else None,
        "qwen_top_is_deny_tier": deny_top,
        "qwen_top_failed_gate": gate_top,
        "routing_failure_rate": sum(routing_failed) / len(routing_failed) if routing_failed else None,
        "k4_low_rated_precision": {"B0_planner": k4(s0), "B1_rates": k4(s1), "B2_qwen": k4(s2)},
        "multi_candidate": {"n": len(multi),
                            "qwen_top_is_chosen_when_accepted": top[(1, True)] / max(1, top[(1, True)] + top[(1, False)]),
                            "qwen_top_is_chosen_when_rejected": top[(0, True)] / max(1, top[(0, True)] + top[(0, False)])},
    }
    best_base = max(out["auc"]["B0_planner"] or 0, out["auc"]["B1_rates"] or 0)
    diff = out["auc_diff_graph_clustered"] or {}  # conservative: correlated decisions in one graph
    lows = [(diff.get(k) or {}).get("ci95", [None])[0] for k in ("B2-B0", "B2-B1")]
    lower_ok = all(isinstance(x, float) and x > 0 for x in lows)
    sample_ok = len(ok) >= 300 and len(kinds) >= 4 and len(pools) >= 5
    # Guided decoding restricts ids to the table, so "outside the list" is a schema error by construction.
    out["verdict"] = {
        "K1_schema_ok": len(attempted) >= 300 and (out["schema_valid_rate"] or 0) >= 0.99,
        "K2_latency_ok": (out["latency_p95_s"] or 1e9) <= 15,
        "K2_openclaw_errors": "not_measured_offline",
        "K3_sample_ok": sample_ok,
        "K3_beats_baselines": sample_ok and (out["auc"]["B2_qwen"] or 0) >= best_base + 0.05 and lower_ok,
        "K4_avoids_routing_failures": ((out_k4 := k4(s2)) or {}).get("precision") is not None
        and out_k4["precision"] >= 0.6,
        "K5_no_deny_or_gate_failed_top": deny_top == 0 and gate_top == 0,
    }
    return out


# ---------------------------------------------------------------- cli

def digest(rows):
    return hashlib.sha256(json.dumps([r["id"] for r in rows]).encode()).hexdigest()[:16]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ledger", default=None, help="ledger JSONL (default: newest)")
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--evaluate-only", action="store_true")
    args = parser.parse_args(argv)
    base = Path.home() / ".agents" / "private-artifacts" / "routing-ledger"
    ledger = Path(args.ledger) if args.ledger else sorted(base.glob("ledger-*.jsonl"))[-1]
    out = Path(args.out_dir) if args.out_dir else base / ("shadow-replay-" + ledger.stem.split("-", 1)[1])
    out.mkdir(parents=True, exist_ok=True)
    rows = build_dataset(*load_ledger(ledger))
    train, test = split(rows)
    meta = {"ledger": ledger.name, "model": MODEL, "endpoint": ENDPOINT, "train_ids": digest(train),
            "test_ids": digest(test), "created_at": datetime.now(timezone.utc).isoformat()}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    with open(out / f"meta-{stamp}.json", "x", encoding="utf-8") as stream:
        json.dump(meta, stream, indent=2)
    results_path = out / "qwen-results.jsonl"
    if not args.evaluate_only:
        replay(test, results_path)
    results = [json.loads(line) for line in open(results_path, encoding="utf-8") if line.strip()]
    metrics = evaluate(train, test, results)
    with open(out / f"metrics-{stamp}.json", "x", encoding="utf-8") as stream:
        json.dump(metrics, stream, indent=2)
    print(json.dumps(metrics, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
