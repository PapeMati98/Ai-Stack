"""Tier-based model selection for Agent Graph (Director / Manager / Kierownik / Worker).

Replaces token/quota tokenomics in candidate choice. Deterministic: no model calls.
Hard provider gates (identity, real exhaustion, lineage) stay in Fleet; this module only
filters by the owner's catalog and orders qualified candidates:

- DENY models (older generations, cost-inefficient) are never chosen;
- a node may require a minimum tier for the maker (min_tier_execute) and reviewer
  (reviewer_min_tier, default = min_tier_execute);
- among similar-fit candidates the less recently used model wins (recency balancing,
  decayed over the last week, measured within the same tier) to spread load over paid plans;
- Claude and Codex below Director tier get a conservation penalty;
- unknown or stale per-pool availability is NEVER promoted or demoted by recent_share;
  a fresh native Fleet run (<24 h, COMPLETED, exit 0, same pool/profile, trusted
  native runtime/identity) is availability evidence that survives numeric-snapshot
  expiry; a concurrent newer refusal still wins.

Catalog: ../references/model-catalog.json (override: AGENT_MODEL_CATALOG).
Usage: Fleet run records in ~/.agents/agent-fleet/runs (override: AGENT_FLEET_RUNS_DIR),
indexed in ~/.agents/model-tiers/usage-index.json (metadata only: agent, model, time).
Availability (optional, produced by OpenClaw): ~/.agents/model-availability/snapshot.json;
missing or older than 3 h means unknown, never blocked.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DEFAULT_CATALOG = HERE.parent / "references" / "model-catalog.json"
TIER_RANK = {"W": 1, "K": 2, "M": 3, "D": 4}
RUN_NAME = re.compile(r"^run-(\d{8}T\d{6})Z-[0-9a-f]+\.json$")
AVAILABILITY_MAX_AGE_S = 3 * 3600
# Native freshness evidence: a verified COMPLETED native run younger than this
# boundary is availability evidence for its pool; older means "not available".
NATIVE_FRESHNESS_S = 24 * 3600
NATIVE_FRESHNESS_FAILURE_REASONS = frozenset({
    "native-zero-token-failure",
    "native-quota-error",
    "provider-error",
    "transport-error",
})
# Reasons observed in quota_availability that prove a real provider refusal.
PROVIDER_REJECTION_REASONS = frozenset({
    "subscription-quota-exhausted",
    "usage-credits-exhausted",
    "plan-credits-exhausted",
    "grok-build-balance-exhausted-http402",
    "usage-limit",
    "kimi-usage-limit",
})
EXHAUSTION_SOURCES = {"provider-response-plan-limit", "provider-quota-endpoint", "owner-dashboard-reading"}
# Every bridge selection runs in its own subprocess, so picks are persisted as short-lived
# claims (agent, model, time) that recent_usage() counts at once. Parallel nodes of one graph
# (and concurrent graphs) then spread over a tier before their Fleet run records exist.
# A claim may overlap its later run record for up to PICK_TTL_S: deliberately biased to spreading.
PICK_TTL_S = 3600
PICK_WEIGHT = 1.0


def _home() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home())


def _json_safe(value: float) -> float | None:
    return value if isinstance(value, (int, float)) and math.isfinite(value) else None


def _normalized_model(model: str) -> str:
    """Model id without a provider/transport prefix (openai/gpt-5.6-luna -> gpt-5.6-luna)."""
    return model.rsplit("/", 1)[-1].strip().casefold()


def load_catalog(path: str | os.PathLike | None = None) -> dict[str, Any]:
    source = Path(path or os.environ.get("AGENT_MODEL_CATALOG") or DEFAULT_CATALOG)
    catalog = json.loads(source.read_text(encoding="utf-8"))
    validate_catalog(catalog)
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for model in catalog["models"]:
        for profile in model.get("profiles", []):
            index[(profile["agent"], profile["model"])] = model
    catalog["_index"] = index
    catalog["_deny_patterns"] = [(re.compile(rule["pattern"], re.IGNORECASE), rule["reason"])
                                 for rule in catalog.get("policy", {}).get("deny_model_patterns", [])]
    catalog["_path"] = str(source)
    return catalog


def validate_catalog(catalog: dict[str, Any]) -> None:
    if catalog.get("schema_version") != 1 or not isinstance(catalog.get("models"), list):
        raise ValueError("model catalog: schema_version 1 with a models list required")
    for rule in catalog.get("policy", {}).get("deny_model_patterns", []):
        if not isinstance(rule, dict) or not rule.get("reason") or not isinstance(rule.get("pattern"), str):
            raise ValueError("model catalog: deny_model_patterns entries need pattern and reason")
        re.compile(rule["pattern"])
    seen: set[tuple[str, str]] = set()
    for model in catalog["models"]:
        for key in ("model_id", "lineage", "tier", "lifecycle", "profiles"):
            if key not in model:
                raise ValueError(f"model catalog: {model.get('model_id')} lacks {key}")
        if model["tier"] not in {*TIER_RANK, "DENY"}:
            raise ValueError(f"model catalog: bad tier {model['tier']} for {model['model_id']}")
        if model["lifecycle"] not in {"candidate", "active", "deprecated"}:
            raise ValueError(f"model catalog: bad lifecycle for {model['model_id']}")
        if model["tier"] == "DENY" and not model.get("deny_reason"):
            raise ValueError(f"model catalog: DENY without reason for {model['model_id']}")
        for profile in model["profiles"]:
            key = (profile.get("agent"), profile.get("model"))
            if not all(isinstance(v, str) and v for v in key):
                raise ValueError(f"model catalog: bad profile in {model['model_id']}")
            if key in seen:
                raise ValueError(f"model catalog: duplicate profile {key}")
            seen.add(key)


def lookup(catalog: dict[str, Any], agent: str, model: str) -> dict[str, Any] | None:
    """Exact profile entry; otherwise a DENY pattern (retired generations) or None (unclassified)."""
    entry = catalog["_index"].get((agent, model))
    if entry is not None:
        return entry
    names = {model, _normalized_model(model)}
    for pattern, reason in catalog.get("_deny_patterns", []):
        if any(pattern.search(name) for name in names):
            return {"model_id": model, "lineage": "", "tier": "DENY", "lifecycle": "deprecated",
                    "deny_reason": reason, "profiles": []}
    return None


def _picks_file() -> Path:
    return Path(os.environ.get("AGENT_MODEL_TIERS_PICKS") or _home() / ".agents" / "model-tiers" / "picks.json")


def _locked(handle):
    """Best-effort exclusive lock on an open file (Windows msvcrt / POSIX fcntl)."""
    try:
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        return lambda: (handle.seek(0), msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1))
    except ImportError:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_picks(now: float) -> list[list]:
    try:
        rows = json.loads(_picks_file().read_text(encoding="utf-8") or "[]")
    except (OSError, ValueError):
        return []
    return [r for r in rows if isinstance(r, list) and len(r) == 3 and isinstance(r[2], (int, float))
            and 0 <= now - r[2] <= PICK_TTL_S]


def note_pick(agent: str, model: str, *, now: float | None = None) -> None:
    """Record a selection so concurrent bridge processes see it at once. Never raises."""
    now = time.time() if now is None else now
    path = _picks_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path.with_suffix(".lock"), "a+", encoding="utf-8") as lock:
            unlock = _locked(lock)
            try:
                rows = _read_picks(now)
                rows.append([agent, model, now])
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(rows), encoding="utf-8")
                os.replace(tmp, path)
            finally:
                unlock()
    except OSError:
        pass


def _parse_run_time(name: str) -> float | None:
    match = RUN_NAME.match(name)
    if not match:
        return None
    return datetime.strptime(match.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc).timestamp()


def recent_usage(
    *,
    runs_dir: str | os.PathLike | None = None,
    index_path: str | os.PathLike | None = None,
    now: float | None = None,
    window_hours: float | None = None,
    half_life_hours: float | None = None,
    catalog: dict[str, Any] | None = None,
) -> dict[tuple[str, str], float]:
    """Decayed recent call weight per (agent, model) from Fleet run records.

    Weight of one run = 0.5 ** (age_hours / half_life). Only run metadata is read."""
    now = time.time() if now is None else now
    policy = (catalog or {}).get("policy", {})
    window_hours = float(window_hours or policy.get("recency_window_hours", 168))
    half_life_hours = float(half_life_hours or policy.get("recency_half_life_hours", 48))
    runs = Path(runs_dir or os.environ.get("AGENT_FLEET_RUNS_DIR") or _home() / ".agents" / "agent-fleet" / "runs")
    index_file = Path(index_path or os.environ.get("AGENT_MODEL_TIERS_INDEX") or _home() / ".agents" / "model-tiers" / "usage-index.json")
    cache: dict[str, list] = {}
    if index_file.is_file():
        try:
            cache = json.loads(index_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cache = {}
    changed = False
    usage: dict[tuple[str, str], float] = {}
    if runs.is_dir():
        for entry in os.scandir(runs):
            started = _parse_run_time(entry.name)
            if started is None or now - started > window_hours * 3600 or started > now + 300:
                continue
            row = cache.get(entry.name)
            if row is None:
                try:
                    record = json.loads(Path(entry.path).read_text(encoding="utf-8"))
                    row = [str(record.get("agent") or ""), str(record.get("model") or "")]
                except (OSError, ValueError):
                    row = ["", ""]
                cache[entry.name] = row
                changed = True
            agent, model = row
            if not agent or not model:
                continue
            age_h = max(0.0, (now - started) / 3600)
            usage[(agent, model)] = usage.get((agent, model), 0.0) + 0.5 ** (age_h / half_life_hours)
    if changed:
        try:
            cutoff = now - window_hours * 3600
            cache = {k: v for k, v in cache.items() if (_parse_run_time(k) or 0) >= cutoff}
            index_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = index_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(cache), encoding="utf-8")
            os.replace(tmp, index_file)
        except OSError:
            pass
    for agent, model, _ in _read_picks(now):
        usage[(agent, model)] = usage.get((agent, model), 0.0) + PICK_WEIGHT
    return usage


def load_availability(path: str | os.PathLike | None = None, *, now: float | None = None) -> dict[str, str]:
    """pool_id -> available|exhausted|unknown. Stale or missing data means unknown."""
    now = time.time() if now is None else now
    source = Path(path or os.environ.get("AGENT_MODEL_AVAILABILITY") or _home() / ".agents" / "model-availability" / "snapshot.json")
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    states: dict[str, str] = {}
    for row in data.get("pools", []) if isinstance(data, dict) else []:
        try:
            observed = datetime.fromisoformat(str(row["observed_at"]).replace("Z", "+00:00")).timestamp()
            expires = datetime.fromisoformat(str(row["expires_at"]).replace("Z", "+00:00")).timestamp()
        except (KeyError, ValueError):
            continue
        state = row.get("state")
        if row.get('source') == 'local-token-ledger' or row.get('evidence_source') == 'local-token-ledger':
            state = 'unknown'
        # Only provider evidence may say exhausted; local ledgers/inferred capacity at most unknown.
        if state == "exhausted" and row.get("source") not in EXHAUSTION_SOURCES:
            state = "unknown"
        if not 0 <= now - observed < AVAILABILITY_MAX_AGE_S or now >= expires or state not in {"available", "exhausted", "unknown"}:
            state = "unknown"
        states[str(row.get("pool_id"))] = state
    return states


def plan_weight(catalog: dict[str, Any], agent: str) -> float:
    """Relative subscription size of an agent's plan (policy.plan_weights), default policy.default_plan_weight."""
    policy = catalog.get("policy", {})
    weights = policy.get("plan_weights", {}) if isinstance(policy.get("plan_weights"), dict) else {}
    value = weights.get(agent, policy.get("default_plan_weight", 1.0))
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 1.0
    return value if math.isfinite(value) and value > 0 else 1.0


def _tier_rank(tier: str | None) -> int:
    return TIER_RANK.get(tier or "", 0)


def required_tier(task: dict[str, Any], role: str) -> str | None:  # null means no floor
    if role == "maker":
        return task.get("min_tier_execute")
    return task.get("reviewer_min_tier") or task.get("min_tier_execute")


def availability_state(quota, *, now=None):
    """Use Fleet's per-pool view, including native evidence, without numeric invention."""
    now = time.time() if now is None else now
    state = quota.get('availability') or {}
    local_state = (state.get('source') != 'native-provider-error' and
                   (str(state.get('source', '')).strip() == 'local-token-ledger'
                    or state.get('evidence_source') == 'local-token-ledger'))
    if not local_state and state.get('status') in {'exhausted', 'recheck-required'}:
        return state['status']
    if not local_state and state.get('status') == 'available':
        try:
            observed = datetime.fromisoformat(state['observed_at'].replace('Z', '+00:00')).timestamp()
        except (KeyError, TypeError, ValueError):
            return 'unknown'
        lifetime = NATIVE_FRESHNESS_S if state.get('source') == 'native-run' else AVAILABILITY_MAX_AGE_S
        if 0 <= now - observed < lifetime:
            return 'available'
    if (str(quota.get('source', '')).strip() != 'local-token-ledger' and quota.get('status') == 'fresh'
            and quota.get('current') is True and quota.get('authority') == 'authoritative'):
        return 'available'
    return 'unknown'


def score_candidate(
    candidate: dict[str, Any],
    *,
    role: str,
    task: dict[str, Any],
    catalog: dict[str, Any],
    usage: dict[tuple[str, str], float],
) -> dict[str, Any]:
    """Effective score and explanation for one qualified candidate (never raises)."""
    policy = catalog.get("policy", {})
    agent, model = candidate["agent"], candidate["model"]
    entry = lookup(catalog, agent, model)
    base = float(candidate.get("score", 0))
    result: dict[str, Any] = {"agent": agent, "model": model, "base": base, "allowed": True, "reasons": []}
    result['availability'] = candidate.get('availability', 'unknown')
    if result['availability'] in {'exhausted', 'recheck-required'}:
        result.update(allowed=False, effective=None)
        result['reasons'].append('pool-' + result['availability'])
        return result
    if entry is None:
        result["tier"] = None
        if required_tier(task, role):
            # A declared class floor needs evidence of class; an unlisted model has none.
            result.update(allowed=False, effective=None)
            result["reasons"].append("unclassified-below-tier-floor")
            return result
        # No floor: usable (fail-open for new models), but a classified model of similar fit wins.
        result["effective"] = base - float(policy.get("unclassified_penalty", 10))
        result["reasons"].append("unclassified-in-catalog")
        return result
    tier = entry["tier"]
    result["tier"] = tier
    if tier == "DENY":
        result.update(allowed=False, effective=None)
        result["reasons"].append("catalog-deny: " + entry.get("deny_reason", ""))
        return result
    need = required_tier(task, role)
    if need and _tier_rank(tier) < _tier_rank(need):
        result.update(allowed=False, effective=None)
        result["reasons"].append(f"below-required-tier {tier}<{need}")
        return result
    # Recency balancing within the same tier: share of decayed recent calls.
    same_tier = [
        (p["agent"], p["model"]) for m in catalog["models"] if m["tier"] == tier and m["lifecycle"] != "deprecated"
        for p in m["profiles"]
    ]
    # Bigger subscriptions should carry proportionally more load: use per plan-weight usage.
    tier_total = sum(usage.get(key, 0.0) / plan_weight(catalog, key[0]) for key in same_tier)
    own = usage.get((agent, model), 0.0) / plan_weight(catalog, agent)
    share = min(1.0, own / tier_total) if tier_total > 0 else 0.0
    recency_penalty = (float(policy.get("recency_max_penalty", 15)) * share
                       if result['availability'] == 'available' else 0.0)
    conserve = 0.0
    # No "substituted" bypass: substitutes are scored from the raw tier base by
    # order_substitutes(), so the existing recency/conservation policy applies
    # exactly once, to every candidate alike.
    if entry["lineage"] in policy.get("conserve_lineages_below_director", []) and tier != "D" and need != "D":
        conserve = float(policy.get("conserve_penalty", 8))
    surplus = 0.0
    if need:
        surplus = float(policy.get("over_tier_penalty", 5)) * (_tier_rank(tier) - _tier_rank(need))
    band = float(policy.get("similar_score_band", 10))
    effective = base - min(recency_penalty + conserve + surplus, band)
    result.update(effective=effective, recent_share=round(share, 3), recency_penalty=round(recency_penalty, 2),
                  conserve_penalty=conserve, over_tier_penalty=surplus)
    if recency_penalty:
        result["reasons"].append(f"recent-share {share:.0%} in tier {tier}")
    if conserve:
        result["reasons"].append("conserve-claude-codex-below-director")
    if surplus:
        result["reasons"].append(f"above-required-tier {tier}>{need}")
    return result


def order_candidates(
    candidates: list[dict[str, Any]],
    *,
    role: str,
    task: dict[str, Any],
    catalog: dict[str, Any],
    usage: dict[tuple[str, str], float],
) -> list[dict[str, Any]]:
    """Qualified candidates, best first. Disallowed ones are returned last with allowed=False."""
    scored = [score_candidate(c, role=role, task=task, catalog=catalog, usage=usage) for c in candidates]
    best = max((r['base'] for r in scored if r['allowed']), default=0)
    band = max(1, float(catalog.get('policy', {}).get('similar_score_band', 10)))
    return sorted(scored, key=lambda r: (r["allowed"], -int(max(0, best - r['base']) / (band + 1e-9)),
                                         r['availability'] == 'available',
                                         r["effective"] if r["effective"] is not None else -1e9,
                                         r["base"]), reverse=True)


def propose(
    *,
    role: str,
    min_tier: str,
    catalog: dict[str, Any],
    usage: dict[tuple[str, str], float],
    availability: dict[str, str] | None = None,
    exclude_lineages: set[str] | None = None,
    pools: dict[str, str] | None = None,
    limit: int | None = 4,
) -> list[dict[str, Any]]:
    """Candidate list for a TODO node: smallest sufficient tier first, balanced by recent use.

    Output entries follow the TODO candidate format {agent, model, score, reason}."""
    availability = availability or {}
    exclude_lineages = exclude_lineages or set()
    pools = pools or {}
    need = _tier_rank(min_tier)
    rows = []
    for model in catalog["models"]:
        if model["tier"] == "DENY" or model["lifecycle"] != "active" or _tier_rank(model["tier"]) < need:
            continue
        if model["lineage"] in exclude_lineages:
            continue
        roles = model.get("roles")
        if roles and ("review" if role == "reviewer" else "execute") not in roles:
            continue
        for profile in model["profiles"]:
            # Availability is keyed by pool id; without an explicit map the pool is the agent.
            pool = pools.get(profile["agent"], profile["agent"])
            if availability.get(pool) in {"exhausted", "recheck-required"}:
                continue
            # Smallest sufficient tier gets the highest base; one tier above loses 15 points.
            base = 90 - 15 * (_tier_rank(model["tier"]) - need)
            candidate = {"agent": profile["agent"], "model": profile["model"], "score": int(base),
                         "availability": availability.get(pool, 'unknown')}
            scored = score_candidate(candidate, role=role, task={"min_tier_execute": min_tier,
                                     "reviewer_min_tier": min_tier}, catalog=catalog, usage=usage)
            rows.append((scored["effective"], model["tier"], candidate, scored))
    rows.sort(key=lambda r: (TIER_RANK[r[1]] == need,
                            r[3]['availability'] == 'available', r[0]), reverse=True)
    out = []
    for effective, tier, candidate, scored in (rows if limit is None else rows[:limit]):
        score = max(0, min(100, int(round(effective / 5.0)) * 5))
        reason = f"tier {tier}; " + ("; ".join(scored["reasons"]) or "balanced")
        out.append({"agent": candidate["agent"], "model": candidate["model"], "score": score, "reason": reason, "effective": effective})
    return out


def score_substitute(
    candidate: dict[str, Any],
    *,
    role: str = "maker",
    min_tier: str = "K",
    catalog: dict[str, Any] | None = None,
    usage: dict[tuple[str, str], float] | None = None,
) -> dict[str, Any]:
    """Score a qualified substitute candidate under existing catalog policy.

    Uses the tier's original base score so that conservation and recency
    penalties are applied exactly once (never twice) and evaluates with
    the candidate's actual post-qualification availability.
    """
    catalog = catalog or load_catalog()
    usage = usage if usage is not None else recent_usage(catalog=catalog)
    need = _tier_rank(min_tier)
    agent = candidate["agent"]
    model = candidate["model"]
    entry = lookup(catalog, agent, model)
    tier = entry["tier"] if entry else min_tier
    base = 90 - 15 * (_tier_rank(tier) - need)
    cand = dict(candidate, score=int(base))
    scored = score_candidate(
        cand,
        role=role,
        task={"min_tier_execute": min_tier, "reviewer_min_tier": min_tier},
        catalog=catalog,
        usage=usage,
    )
    effective = scored.get("effective", base)
    score = max(0, min(100, int(round(effective / 5.0)) * 5))
    reason = f"tier {tier}; " + ("; ".join(scored.get("reasons", [])) or "balanced")
    result = dict(candidate)
    result.update({
        "score": score,
        "effective": effective,
        "reason": reason,
        "tier": tier,
        "allowed": scored.get("allowed", True),
        "availability": candidate.get("availability", "unknown"),
        "base": base,
    })
    return result


def order_substitutes(
    candidates: list[dict[str, Any]],
    *,
    role: str = "maker",
    min_tier: str = "K",
    catalog: dict[str, Any] | None = None,
    usage: dict[tuple[str, str], float] | None = None,
) -> list[dict[str, Any]]:
    """Order qualified substitute candidates deterministically under existing catalog policy.

    Applies conservation/recency penalties exactly once from the tier base,
    respects actual post-qualification availability (available beats unknown),
    and prefers higher effective scores.
    """
    catalog = catalog or load_catalog()
    usage = usage if usage is not None else recent_usage(catalog=catalog)
    scored = [score_substitute(c, role=role, min_tier=min_tier, catalog=catalog, usage=usage)
              for c in candidates]
    best = max((r.get('base', 0) for r in scored if r.get('allowed', True)), default=0)
    band = max(1, float(catalog.get('policy', {}).get('similar_score_band', 10)))
    return sorted(
        scored,
        key=lambda r: (
            r.get("allowed", True),
            -int(max(0, best - r.get('base', 0)) / (band + 1e-9)),
            r.get("availability") == "available",
            r["effective"] if r.get("effective") is not None else -1e9,
            r.get("score", 0),
            r.get("base", 0),
        ),
        reverse=True,
    )


VALID_CANDIDATE_POLICIES = frozenset({"open", "pin", "pinned"})


def validate_candidate_policy_value(val, label="candidate_policy"):
    if val is None:
        return
    if isinstance(val, str):
        if val.strip().casefold() not in VALID_CANDIDATE_POLICIES:
            raise ValueError(f"{label} must be 'open' or 'pin', got {val!r}")
        return
    if isinstance(val, dict):
        if not val or set(val) - {"maker", "reviewer"}:
            raise ValueError(f"{label} keys must be 'maker' or 'reviewer', got {list(val)}")
        for lane, lane_val in val.items():
            if not isinstance(lane_val, str) or lane_val.strip().casefold() not in VALID_CANDIDATE_POLICIES:
                raise ValueError(f"{label}[{lane!r}] must be 'open' or 'pin', got {lane_val!r}")
        return
    raise ValueError(f"{label} must be 'open' or 'pin', got {type(val).__name__}")


def resolve_candidate_policy(task: dict[str, Any], role: str, request: dict[str, Any] | None = None) -> str | None:
    """Resolve candidate policy for role: 'open', 'pin', or None (legacy absent)."""
    task_policy = task.get("candidate_policy") if isinstance(task, dict) else None
    validate_candidate_policy_value(task_policy, "task candidate_policy")

    req_policy = request.get("candidate_policy") if isinstance(request, dict) else None
    validate_candidate_policy_value(req_policy, "request candidate_policy")

    plan = request.get("plan") if isinstance(request, dict) else None
    plan_policy = plan.get("candidate_policy") if isinstance(plan, dict) else None
    validate_candidate_policy_value(plan_policy, "plan candidate_policy")

    def _extract_lane(val):
        if val is None:
            return None
        if isinstance(val, str):
            norm = val.strip().casefold()
            return "pin" if norm in {"pin", "pinned"} else "open"
        if isinstance(val, dict):
            lane_val = val.get(role)
            if lane_val is None:
                return None
            norm = lane_val.strip().casefold()
            return "pin" if norm in {"pin", "pinned"} else "open"
        return None

    policy = _extract_lane(task_policy)
    if policy is None:
        policy = _extract_lane(req_policy)
    if policy is None:
        policy = _extract_lane(plan_policy)
    return policy


def same_class_candidates(
    catalog: dict[str, Any],
    *,
    tier: str,
    role: str,
    base_score: int = 90,
    exclude_lineages: set[str] | None = None,
    excluded_candidates: set[tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Return active, qualified same-class alternative candidate models from the catalog.

    Only models matching the given tier and lifecycle='active' are returned.
    Catalog DENY entries, pattern denials, mismatched roles, excluded lineages
    and excluded candidate profiles are filtered out.
    """
    exclude_lineages = exclude_lineages or set()
    excluded_candidates = excluded_candidates or set()
    needed_role = "review" if role == "reviewer" else "execute"
    results = []
    seen = set()
    for model in catalog.get("models", []):
        if model.get("tier") != tier:
            continue
        if model.get("lifecycle") != "active":
            continue
        if model.get("lineage"):
            m_lin = str(model["lineage"]).strip().casefold()
            if any(m_lin == l.casefold() or m_lin == l.split("/")[0].strip().casefold() or l.casefold().startswith(m_lin + "/") for l in exclude_lineages):
                continue
        roles = model.get("roles")
        if roles and needed_role not in roles:
            continue
        for profile in model.get("profiles", []):
            agent = profile.get("agent")
            model_id = profile.get("model")
            if not agent or not model_id:
                continue
            if (agent, model_id) in excluded_candidates:
                continue
            entry = lookup(catalog, agent, model_id)
            if entry is None or entry.get("tier") == "DENY":
                continue
            key = (agent, model_id)
            if key not in seen:
                seen.add(key)
                results.append({
                    "agent": agent,
                    "model": model_id,
                    "score": base_score,
                    "reason": f"same-class {tier} alternative from catalog",
                })
    return results


def _cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate-catalog")
    sub.add_parser("usage")
    p = sub.add_parser("propose")
    p.add_argument("--role", choices=["maker", "reviewer"], required=True)
    p.add_argument("--min-tier", choices=list(TIER_RANK), required=True)
    p.add_argument("--exclude-lineage", action="append", default=[])
    p.add_argument("--limit", type=int, default=4)
    s = sub.add_parser("shadow", help="compare the planner's first choice with tier ordering for a todo.json")
    s.add_argument("--todo", required=True)
    args = parser.parse_args()
    catalog = load_catalog()
    if args.cmd == "validate-catalog":
        print(json.dumps({"status": "ok", "models": len(catalog["models"]), "path": catalog["_path"]}))
        return 0
    usage = recent_usage(catalog=catalog)
    if args.cmd == "usage":
        rows = sorted(((round(v, 2), a, m, (lookup(catalog, a, m) or {}).get("tier")) for (a, m), v in usage.items()), reverse=True)
        print(json.dumps([{"weight": w, "agent": a, "model": m, "tier": t} for w, a, m, t in rows], indent=1))
        return 0
    if args.cmd == "propose":
        print(json.dumps(propose(role=args.role, min_tier=args.min_tier, catalog=catalog, usage=usage,
                                 availability=load_availability(), exclude_lineages=set(args.exclude_lineage),
                                 limit=args.limit), indent=1, ensure_ascii=False))
        return 0
    plan = json.loads(Path(args.todo).read_text(encoding="utf-8"))
    report = []
    for task in plan.get("tasks", []):
        for role in ("maker", "reviewer"):
            cands = task.get(role) or []
            if not isinstance(cands, list) or not cands:
                continue
            ordered = order_candidates(cands, role=role, task=task, catalog=catalog, usage=usage)
            planner_first = max(cands, key=lambda c: c.get("score", 0))
            report.append({"task": task.get("id"), "role": role,
                           "planner_first": f"{planner_first['agent']}/{planner_first['model']}",
                           "tier_first": f"{ordered[0]['agent']}/{ordered[0]['model']}" if ordered[0]["allowed"] else None,
                           "ordered": ordered})
    print(json.dumps(report, indent=1, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
