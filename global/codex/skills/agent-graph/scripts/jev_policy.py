"""Deterministic, read-only routing policy over a Jev decision and a capability snapshot.

Advisory only: never overrides Fleet/Graph/Office permissions, the planner or reviewer
gates. Pure functions, no file I/O, no network, no ambient state. See
docs/JEV-ROUTING-DESIGN.md for the input/output contract this implements.

Both ``task`` and ``snapshot`` dicts must carry ``schema_version: 1``. ``decision`` is the
dict produced by ``jev_client.parse_response`` (or ``None``/an empty dict when Jev was not
consulted for this task -- Jev is an advisory signal, not a requirement, so callers may
fall back to a candidate's own static ``fit`` scores).
"""
from datetime import datetime, timezone
import math
import re
import sys

sys.dont_write_bytecode = True

SCHEMA_VERSION = 1
CONFIDENCE_THRESHOLD = 0.70
STALE_SECONDS = 300
SCORE_STEP = 5
MAX_CONSULTANTS = 2

VALID_MODES = {"implement", "advisory", "review"}
VALID_BILLING = {"subscription", "local", "api"}
VALID_STATUS = {"available", "exhausted", "unknown"}
VALID_MODEL_CLASSES = {"mythos", "opus", "sonnet", "luna", "unknown"}
VALID_DIFFICULTY = {"low", "medium", "high"}
VALID_PARALLELISM_CHOICE = {"sequential", "parallel", "unknown"}
VALID_CONSULTATION_CHOICE = {"none", "subscription", "api_candidate"}

_DATED_SUFFIX = re.compile(r"^(?P<base>.+)-(?P<date>\d{8})$")


class PolicyError(ValueError):
    """Raised for malformed task/snapshot/decision input. The message is a fixed code."""


# --------------------------------------------------------------------------- validation

def _finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _string_list(value):
    return isinstance(value, list) and all(isinstance(item, str) and item for item in value)


def _parse_iso(value):
    if not isinstance(value, str) or not value:
        raise PolicyError("INVALID_TIMESTAMP")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise PolicyError("INVALID_TIMESTAMP") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _validate_task(task):
    if not isinstance(task, dict):
        raise PolicyError("TASK_NOT_DICT")
    if task.get("schema_version") != SCHEMA_VERSION:
        raise PolicyError("TASK_SCHEMA_VERSION")
    brief = task.get("brief")
    if not isinstance(brief, str) or not brief or len(brief) > 4000:
        raise PolicyError("TASK_BRIEF_INVALID")
    if task.get("mode") not in VALID_MODES:
        raise PolicyError("TASK_MODE_INVALID")
    for field in ("required_tools", "scope", "resources", "depends_on"):
        if not _string_list(task.get(field)):
            raise PolicyError(f"TASK_{field.upper()}_INVALID")
    tokens = task.get("estimated_context_tokens")
    if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens < 0:
        raise PolicyError("TASK_ESTIMATED_CONTEXT_TOKENS_INVALID")
    if not isinstance(task.get("domain"), str) or not task.get("domain"):
        raise PolicyError("TASK_DOMAIN_INVALID")
    if not isinstance(task.get("high_risk"), bool):
        raise PolicyError("TASK_HIGH_RISK_INVALID")


def _validate_candidate(candidate, seen_ids):
    if not isinstance(candidate, dict):
        raise PolicyError("CANDIDATE_NOT_DICT")
    candidate_id = candidate.get("id")
    if not isinstance(candidate_id, str) or not candidate_id:
        raise PolicyError("CANDIDATE_ID_INVALID")
    if candidate_id in seen_ids:
        raise PolicyError("CANDIDATE_ID_DUPLICATE")
    seen_ids.add(candidate_id)
    for field in ("agent", "model", "lineage"):
        if not isinstance(candidate.get(field), str) or not candidate.get(field):
            raise PolicyError(f"CANDIDATE_{field.upper()}_INVALID")
    for field in ("enabled", "training_allowed"):
        if not isinstance(candidate.get(field), bool):
            raise PolicyError(f"CANDIDATE_{field.upper()}_INVALID")
    if candidate.get("billing") not in VALID_BILLING:
        raise PolicyError("CANDIDATE_BILLING_INVALID")
    for field in ("tools", "modes"):
        if not _string_list(candidate.get(field)):
            raise PolicyError(f"CANDIDATE_{field.upper()}_INVALID")
    fit = candidate.get("fit")
    if not isinstance(fit, dict) or not all(
        isinstance(key, str) and _finite_number(val) and 0 <= val <= 1
        for key, val in fit.items()
    ):
        raise PolicyError("CANDIDATE_FIT_INVALID")
    if candidate.get("status") not in VALID_STATUS:
        raise PolicyError("CANDIDATE_STATUS_INVALID")
    quota_remaining = candidate.get("quota_remaining")
    if quota_remaining is not None and (
        not _finite_number(quota_remaining) or not (0.0 <= quota_remaining <= 1.0)
    ):
        raise PolicyError("CANDIDATE_QUOTA_REMAINING_INVALID")
    for field in ("quota_observed_at", "quota_reset_at"):
        value = candidate.get(field)
        if value is not None:
            _parse_iso(value)
    context_window_tokens = candidate.get("context_window_tokens")
    if context_window_tokens is not None and (
        not isinstance(context_window_tokens, int)
        or isinstance(context_window_tokens, bool)
        or context_window_tokens < 0
    ):
        raise PolicyError("CANDIDATE_CONTEXT_WINDOW_TOKENS_INVALID")
    capacity_remaining = candidate.get("capacity_remaining")
    if capacity_remaining is not None and (
        not isinstance(capacity_remaining, int)
        or isinstance(capacity_remaining, bool)
        or capacity_remaining < 0
    ):
        raise PolicyError("CANDIDATE_CAPACITY_REMAINING_INVALID")
    if "model_class" in candidate:
        value = candidate["model_class"]
        if not isinstance(value, str) or value not in VALID_MODEL_CLASSES:
            raise PolicyError("CANDIDATE_MODEL_CLASS_INVALID")
    if "coding_rank" in candidate:
        value = candidate["coding_rank"]
        if value is not None and (not _finite_number(value) or value <= 0):
            raise PolicyError("CANDIDATE_CODING_RANK_INVALID")


def _validate_assignment(assignment):
    if not isinstance(assignment, dict):
        raise PolicyError("ASSIGNMENT_NOT_DICT")
    if not isinstance(assignment.get("candidate_id"), str) or not assignment.get("candidate_id"):
        raise PolicyError("ASSIGNMENT_CANDIDATE_ID_INVALID")
    if not isinstance(assignment.get("lineage"), str) or not assignment.get("lineage"):
        raise PolicyError("ASSIGNMENT_LINEAGE_INVALID")
    for field in ("scope", "resources"):
        if not _string_list(assignment.get(field)):
            raise PolicyError(f"ASSIGNMENT_{field.upper()}_INVALID")


def _validate_snapshot(snapshot):
    if not isinstance(snapshot, dict):
        raise PolicyError("SNAPSHOT_NOT_DICT")
    if snapshot.get("schema_version") != SCHEMA_VERSION:
        raise PolicyError("SNAPSHOT_SCHEMA_VERSION")
    _parse_iso(snapshot.get("captured_at"))
    candidates = snapshot.get("candidates")
    if not isinstance(candidates, list):
        raise PolicyError("SNAPSHOT_CANDIDATES_INVALID")
    seen_ids = set()
    for candidate in candidates:
        _validate_candidate(candidate, seen_ids)
    assignments = snapshot.get("active_assignments")
    if not isinstance(assignments, list):
        raise PolicyError("SNAPSHOT_ACTIVE_ASSIGNMENTS_INVALID")
    for assignment in assignments:
        _validate_assignment(assignment)
    capacity = snapshot.get("capacity")
    if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity < 0:
        raise PolicyError("SNAPSHOT_CAPACITY_INVALID")


def _validate_choice_field(decision, field, valid_values):
    entry = decision.get(field)
    if entry is None:
        return
    if not isinstance(entry, dict) or entry.get("value") not in valid_values:
        raise PolicyError(f"DECISION_{field.upper()}_INVALID")
    confidence = entry.get("confidence")
    if not _finite_number(confidence) or not (0.0 <= confidence <= 1.0):
        raise PolicyError(f"DECISION_{field.upper()}_CONFIDENCE_INVALID")


def _validate_decision(decision):
    if decision is None:
        return {}
    if not isinstance(decision, dict):
        raise PolicyError("DECISION_NOT_DICT")
    _validate_choice_field(decision, "difficulty", VALID_DIFFICULTY)
    _validate_choice_field(decision, "parallelism", VALID_PARALLELISM_CHOICE)
    _validate_choice_field(decision, "consultation", VALID_CONSULTATION_CHOICE)
    fit = decision.get("fit")
    if fit is not None:
        if not isinstance(fit, dict):
            raise PolicyError("DECISION_FIT_INVALID")
        for candidate_id, entry in fit.items():
            if not isinstance(candidate_id, str) or not isinstance(entry, dict):
                raise PolicyError("DECISION_FIT_INVALID")
            score = entry.get("score")
            confidence = entry.get("confidence")
            if not _finite_number(score) or not (0.0 <= score <= 4.0):
                raise PolicyError("DECISION_FIT_SCORE_INVALID")
            if not _finite_number(confidence) or not (0.0 <= confidence <= 1.0):
                raise PolicyError("DECISION_FIT_CONFIDENCE_INVALID")
    return decision


def _normalize_now(now):
    if now is None:
        return datetime.now(timezone.utc)
    if not isinstance(now, datetime):
        raise PolicyError("NOW_INVALID")
    return now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)


_KNOWN_OPENROUTER_PROVIDER_PREFIXES = {
    "openai", "anthropic", "google", "meta-llama", "mistralai", "cohere",
    "deepseek", "qwen", "x-ai", "perplexity", "typesafe", "amazon", "microsoft",
}


def _normalize_model_id(model_id):
    """Strip a dated suffix and, only for a known OpenRouter provider prefix, the
    provider segment -- so identity comparison recognizes ``openai/gpt-6-astra`` as the
    same model as a bare ``gpt-6-astra`` candidate without collapsing an entire
    provider's model family into one identity (the remaining base name must still
    match exactly)."""
    if not isinstance(model_id, str):
        return model_id
    match = _DATED_SUFFIX.match(model_id)
    base = match.group("base") if match else model_id
    if "/" in base:
        prefix, rest = base.split("/", 1)
        if prefix in _KNOWN_OPENROUTER_PROVIDER_PREFIXES:
            base = rest
    # Exact, evidenced spelling difference between installed Fleet and the
    # existing OpenRouter roster; do not normalize whole providers/families.
    base = {"claude-opus-5.5": "claude-opus-5-5",
            "claude-fable-5.1": "claude-fable-5-1"}.get(base, base)
    return base


# --------------------------------------------------------------------------- quota/fit

def _effective_quota(candidate, now):
    """Quota is only usable as fresh free capacity if recently observed; else unknown.

    Explicit ``status == "exhausted"`` is handled separately by the caller and is never
    cleared just because the quota observation itself is unknown or stale.
    """
    observed_at = candidate.get("quota_observed_at")
    if observed_at is None:
        return None
    if abs((now - _parse_iso(observed_at)).total_seconds()) > STALE_SECONDS:
        return None
    return candidate.get("quota_remaining")


def _domain_fit(candidate, domain):
    value = candidate.get("fit", {}).get(domain)
    if not _finite_number(value):
        return None
    return max(0.0, min(1.0, float(value)))


def _jev_fit(decision, candidate_id):
    entry = (decision or {}).get("fit", {}).get(candidate_id)
    if not isinstance(entry, dict):
        return None
    score, confidence = entry.get("score"), entry.get("confidence")
    if not _finite_number(score) or not _finite_number(confidence):
        return None
    return float(score), float(confidence)


def _combined_score(candidate, task, decision):
    """Blend Jev's per-candidate assessment with the candidate's static domain fit.

    Missing Jev output degrades to the static fit alone with confidence 0.0 (never a
    fabricated confidence) so the caller's low-confidence escalation still fires.
    """
    jev = _jev_fit(decision, candidate["id"])
    domain_fit = _domain_fit(candidate, task["domain"])
    if jev is not None:
        jev_score, confidence = jev
        jev_component = jev_score / 4.0
        combined = 0.7 * jev_component + 0.3 * domain_fit if domain_fit is not None else jev_component
        source = "JEV_FIT"
    elif domain_fit is not None:
        combined, confidence, source = domain_fit, 0.0, "STATIC_FIT_ONLY"
    else:
        combined, confidence, source = 0.0, 0.0, "NO_FIT_DATA"
    combined = max(0.0, min(1.0, combined))
    score_100 = max(0, min(100, int(round((combined * 100) / SCORE_STEP)) * SCORE_STEP))
    return score_100, confidence, source


# --------------------------------------------------------------------------- eligibility

def _ineligibility_reasons(candidate, task, now, required_mode):
    reasons = []
    if candidate["enabled"] is not True:
        reasons.append("DISABLED")
    if candidate["training_allowed"] is not False:
        reasons.append("TRAINING_NOT_ALLOWED")
    if required_mode not in candidate["modes"]:
        reasons.append("MODE_UNSUPPORTED")
    if required_mode == task["mode"]:
        if not set(task["required_tools"]).issubset(set(candidate["tools"])):
            reasons.append("TOOLS_UNSUPPORTED")
    elif required_mode in ("review", "advisory"):
        # A reviewer/consultant is never evaluated against the task's own
        # required_tools (those describe the maker's job), but it still needs a
        # baseline read capability to actually review/advise -- enabled status
        # alone is not sufficient.
        if not ({"read", "filesystem-read"} & set(candidate["tools"])):
            reasons.append("TOOLS_UNSUPPORTED")
    context_window = candidate.get("context_window_tokens")
    if context_window is None:
        reasons.append("CONTEXT_UNKNOWN")
    elif task["estimated_context_tokens"] > context_window:
        reasons.append("CONTEXT_INSUFFICIENT")
    if candidate["status"] == "exhausted":
        reasons.append("EXHAUSTED")
    if candidate.get("capacity_remaining") == 0:
        reasons.append("CAPACITY_ZERO")
    quota_effective = _effective_quota(candidate, now)
    if quota_effective is not None and quota_effective <= 0:
        reasons.append("QUOTA_DEPLETED")
    return reasons


def _consultant_eligible(candidate, task, now):
    if candidate["enabled"] is not True or candidate["training_allowed"] is not False:
        return False
    # A consultant reads and advises, never writes; require the same baseline
    # read/advisory capability as a reviewer, not merely an enabled candidate.
    if not any(mode in candidate["modes"] for mode in ("review", "advisory")):
        return False
    if not ({"read", "filesystem-read"} & set(candidate["tools"])):
        return False
    window = candidate.get("context_window_tokens")
    if window is None or window < task["estimated_context_tokens"]:
        return False
    if candidate["status"] == "exhausted" or candidate.get("capacity_remaining") == 0:
        return False
    if candidate.get("tier") == "DENY":
        return False
    quota_effective = _effective_quota(candidate, now)
    return not (quota_effective is not None and quota_effective <= 0)


# --------------------------------------------------------------------------- parallelism

def _canon_path(raw):
    """Casefold + slash-normalize; return None for traversal/absolute/unknown paths."""
    if not isinstance(raw, str) or not raw:
        return None
    normalized = raw.replace("\\", "/").strip().casefold()
    if normalized.startswith("/") or any(char in normalized for char in ':*?[]\x00\r\n'):
        return None
    parts = normalized.split("/")
    if any(part in ("", ".", "..") or part.endswith(('.', ' ')) for part in parts):
        return None
    return "/".join(parts)


def _is_ancestor_or_equal(parts_a, parts_b):
    n = min(len(parts_a), len(parts_b))
    return parts_a[:n] == parts_b[:n]


def _paths_conflict(list_a, list_b):
    """A directory scope conflicts with anything nested under it, not only an exact
    string match -- ``src`` conflicts with ``src/parser.py``."""
    canon_a = [_canon_path(item) for item in list_a]
    canon_b = [_canon_path(item) for item in list_b]
    if any(item is None for item in canon_a) or any(item is None for item in canon_b):
        return True
    for a in canon_a:
        parts_a = a.split("/")
        for b in canon_b:
            if _is_ancestor_or_equal(parts_a, b.split("/")):
                return True
    return False


def _paths_known(paths):
    return all(_canon_path(item) is not None for item in paths)


def _parallelism(task, snapshot, decision, now, stale_snapshot):
    if task["depends_on"]:
        return {"recommendation": "sequential", "max_parallel": 1, "reasons": ["UNRESOLVED_DEPENDENCIES"]}
    if not task["scope"]:
        # A totally empty own scope/resources is invalid input for an independence
        # judgement, not proof of nothing-to-conflict-with; it must serialize even
        # when no other work is active.
        return {"recommendation": "sequential", "max_parallel": 1, "reasons": ["UNKNOWN_TASK_SCOPE_OR_RESOURCES"]}
    if not (_paths_known(task["scope"]) and _paths_known(task["resources"])):
        # Own scope/resources must canonicalize before independence can even be
        # considered -- otherwise there is nothing to compare active assignments
        # against, and an empty active_assignments list must not be read as proof
        # of disjointness.
        return {"recommendation": "sequential", "max_parallel": 1, "reasons": ["UNKNOWN_TASK_SCOPE_OR_RESOURCES"]}
    active = snapshot["active_assignments"]
    if any(not a['scope'] or not _paths_known(a['scope']) or not _paths_known(a['resources']) for a in active):
        return {"recommendation": "sequential", "max_parallel": 1, "reasons": ["UNKNOWN_ACTIVE_SCOPE_OR_RESOURCES"]}
    conflict = any(
        _paths_conflict(task["scope"], assignment["scope"])
        or _paths_conflict(task["resources"], assignment["resources"])
        for assignment in active
    )
    if conflict:
        return {"recommendation": "sequential", "max_parallel": 1, "reasons": ["ACTIVE_SCOPE_OR_RESOURCE_CONFLICT"]}
    if stale_snapshot:
        # A stale snapshot's "no conflict" observation is not current evidence; it
        # must never be used to claim safe parallelism.
        return {"recommendation": "sequential", "max_parallel": 1, "reasons": ["STALE_SNAPSHOT_NO_PARALLEL_CLAIM"]}
    if not active:
        # No known concurrent work at all means there is nothing verified to run
        # in parallel with -- capacity and a confident Jev signal alone are not
        # evidence of disjoint running work.
        return {"recommendation": "sequential", "max_parallel": 1, "reasons": ["NO_KNOWN_CONCURRENT_WORK"]}
    signal = (decision or {}).get("parallelism")
    capacity = snapshot["capacity"]
    if (
        isinstance(signal, dict)
        and signal.get("value") == "parallel"
        and signal.get("confidence", 0) >= CONFIDENCE_THRESHOLD
        and capacity > 1
    ):
        return {"recommendation": "parallel", "max_parallel": capacity, "reasons": ["DISJOINT_SCOPE_AND_CAPACITY"]}
    return {"recommendation": "sequential", "max_parallel": 1, "reasons": ["DEFAULT_SEQUENTIAL"]}


# --------------------------------------------------------------------------- public API

# Tier routing (owner 2026-09-29): among similar fits prefer the less recently used model of
# the same tier, conserve Claude/Codex below Director, never route catalog-denied models.
# Quota numbers are no longer a ranking signal (tokenomics off); exhaustion stays a hard gate.
# Same scale as model-catalog.json policy (score_100 grid, step 5): full recent share costs 15,
# conservation 8, together capped at the similar-fit band of 10, so a clearly better fit wins.
RECENCY_WEIGHT = 15.0
CONSERVE_WEIGHT = 8.0
SIMILAR_FIT_BAND = 10.0


def _tier_adjusted(score, candidate):
    share = candidate.get("recent_share")
    penalty = RECENCY_WEIGHT * float(share) if _finite_number(share) else 0.0
    if candidate.get("conserve") is True:
        penalty += CONSERVE_WEIGHT
    return score - min(penalty, SIMILAR_FIT_BAND)


def recommend(task, snapshot, decision, *, now=None):
    _validate_task(task)
    _validate_snapshot(snapshot)
    decision = _validate_decision(decision)
    now = _normalize_now(now)

    escalations = []
    captured_at = _parse_iso(snapshot["captured_at"])
    stale_snapshot = abs((now - captured_at).total_seconds()) > STALE_SECONDS
    if stale_snapshot:
        escalations.append("STALE_SNAPSHOT")

    load_by_id, load_by_lineage = {}, {}
    for assignment in snapshot["active_assignments"]:
        load_by_id[assignment["candidate_id"]] = load_by_id.get(assignment["candidate_id"], 0) + 1
        load_by_lineage[assignment["lineage"]] = load_by_lineage.get(assignment["lineage"], 0) + 1

    def build_ranked(required_mode):
        excluded, ranked = [], []
        for candidate in snapshot["candidates"]:
            reasons = _ineligibility_reasons(candidate, task, now, required_mode)
            if candidate.get("tier") == "DENY":
                reasons = list(reasons) + ["CATALOG_DENY"]
            if reasons:
                excluded.append({"id": candidate["id"], "agent": candidate["agent"], "reasons": reasons})
                continue
            score, confidence, source = _combined_score(candidate, task, decision)
            # Keep the reported fit score on the TODO 0..100/5 grid; tier/recency only reorders.
            rank = _tier_adjusted(score, candidate)
            quota_effective = _effective_quota(candidate, now)
            load = load_by_id.get(candidate["id"], 0) + load_by_lineage.get(candidate["lineage"], 0)
            entry = {
                "id": candidate["id"],
                "agent": candidate["agent"],
                "model": candidate["model"],
                "lineage": candidate["lineage"],
                "billing": candidate["billing"],
                "score": score,
                "confidence": confidence,
                "reason": source,
                "_quota_effective": quota_effective,
                "_load": load,
                "_rank": rank,
            }
            for field in ("model_class", "coding_rank", "tier", "recent_share"):
                if field in candidate:
                    entry[field] = candidate[field]
            ranked.append(entry)
        ranked.sort(key=lambda entry: (
            -entry["_rank"],
            0 if entry["billing"] in ("subscription", "local") else 1,
            entry["_load"],
            entry["id"],
        ))
        return ranked, excluded

    maker_ranked, maker_excluded = build_ranked(task["mode"])
    reviewer_ranked, reviewer_excluded = build_ranked("review")

    top_maker = maker_ranked[0] if maker_ranked else None
    if top_maker is None:
        escalations.append("NO_ELIGIBLE_MAKER")
        reviewer_selected = []
    else:
        if top_maker["confidence"] < CONFIDENCE_THRESHOLD:
            escalations.append("LOW_CONFIDENCE")
        reviewer_selected = [
            entry for entry in reviewer_ranked
            if entry["id"] != top_maker["id"] and entry["lineage"] != top_maker["lineage"]
        ]
        reviewer_excluded.extend(
            {"id": entry["id"], "agent": entry["agent"], "reasons": ["SAME_MODEL_LINEAGE"]}
            for entry in reviewer_ranked if entry["lineage"] == top_maker["lineage"]
        )
        if not reviewer_selected:
            escalations.append("NO_DISTINCT_REVIEWER")
        elif any(entry["confidence"] < CONFIDENCE_THRESHOLD for entry in reviewer_selected):
            # Selection confidence must cover the reviewer too -- a low-confidence
            # reviewer is not a safe automatic pass just because the maker was fine.
            escalations.append("REVIEWER_LOW_CONFIDENCE")

    if task["high_risk"]:
        escalations.append("HIGH_RISK_TASK")

    def strip(entries):
        return [{key: val for key, val in entry.items() if not key.startswith("_")} for entry in entries]

    return {
        "maker": strip(maker_ranked),
        "reviewer": strip(reviewer_selected),
        "excluded": maker_excluded,
        "reviewer_excluded": reviewer_excluded,
        "requires_parent_review": bool(escalations),
        "requires_parent_review_reasons": escalations,
        "parallelism": _parallelism(task, snapshot, decision, now, stale_snapshot),
        "consultation": consultation_policy(task, snapshot, decision, now=now),
        "stale_snapshot": stale_snapshot,
    }


def consultation_policy(task, snapshot, decision, *, api_budget_usd=0, justification="", explicit_api_models=(), now=None):
    """Subscription-first council policy. API dispatch never follows automatically from a
    model recommendation: it always requires an explicit allowlist, a nonempty incremental-
    benefit justification and a positive finite budget from the caller, regardless of what
    Jev's own consultation signal said."""
    _validate_task(task)
    _validate_snapshot(snapshot)
    decision = _validate_decision(decision)
    now = _normalize_now(now)
    if not isinstance(explicit_api_models, (list, tuple)) or len(explicit_api_models) > MAX_CONSULTANTS:
        raise PolicyError("API_MODEL_ALLOWLIST_INVALID")
    if any(not isinstance(model, str) or len(model) > 200
           or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", model) is None
           for model in explicit_api_models):
        raise PolicyError("API_MODEL_ALLOWLIST_INVALID")
    if len({_normalize_model_id(model) for model in explicit_api_models}) != len(explicit_api_models):
        raise PolicyError("API_MODEL_ALLOWLIST_DUPLICATE")

    signal = (decision or {}).get("consultation")
    jev_signal = (
        isinstance(signal, dict)
        and signal.get("value") in ("subscription", "api_candidate")
        and signal.get("confidence", 0) >= CONFIDENCE_THRESHOLD
    )
    if not (task["high_risk"] or jev_signal):
        return {"recommendation": "none", "consultants": [], "api_consultants": [], "reasons": ["ROUTINE_TASK_NO_CONSULTATION"]}

    reasons = []
    if task["high_risk"]:
        reasons.append("HIGH_RISK_TASK")
    if jev_signal:
        reasons.append("JEV_CONSULTATION_SIGNAL")

    eligible = [c for c in snapshot["candidates"] if _consultant_eligible(c, task, now)]
    load = {}
    for assignment in snapshot['active_assignments']:
        load[assignment['lineage']] = load.get(assignment['lineage'], 0) + 1
    subscription_pool = sorted(
        (c for c in eligible if c["billing"] in ("subscription", "local")),
        # Same class-based ordering as maker/reviewer routing; quota headroom is not a ranking signal.
        key=lambda c: (-_tier_adjusted(_combined_score(c, task, decision)[0], c), load.get(c['lineage'], 0),
                       c['id']),
    )
    seen_lineage, consultants = set(), []
    for candidate in subscription_pool:
        if candidate["lineage"] in seen_lineage:
            continue
        seen_lineage.add(candidate["lineage"])
        consultants.append({
            "id": candidate["id"], "agent": candidate["agent"], "model": candidate["model"],
            "lineage": candidate["lineage"], "billing": candidate["billing"], "reason": "SUBSCRIPTION_CONSULTANT",
        })
        if len(consultants) >= MAX_CONSULTANTS:
            break

    api_consultants = []
    if explicit_api_models:
        subscription_models = {_normalize_model_id(c["model"]) for c in eligible if c["billing"] in ("subscription", "local")}
        budget_ok = _finite_number(api_budget_usd) and api_budget_usd > 0
        justification_ok = isinstance(justification, str) and justification.strip() != ""
        if not budget_ok:
            reasons.append("API_BUDGET_MISSING_OR_INVALID")
        if not justification_ok:
            reasons.append("API_JUSTIFICATION_MISSING")
        if budget_ok and justification_ok:
            for model_id in explicit_api_models:
                if not isinstance(model_id, str) or not model_id:
                    reasons.append("API_MODEL_ID_INVALID")
                elif _normalize_model_id(model_id) in subscription_models:
                    reasons.append("API_REFUSED_SUBSCRIPTION_AVAILABLE")
                else:
                    api_consultants.append({
                        "model": model_id, "reason": "EXPLICIT_API_ALLOWLIST",
                        "justification": justification,
                    })
    elif api_budget_usd or justification:
        reasons.append("API_MODEL_ALLOWLIST_REQUIRED")

    if consultants:
        recommendation = "subscription"
    elif api_consultants:
        recommendation = "api"
    else:
        recommendation = "none"
        reasons.append("NO_ELIGIBLE_CONSULTANTS")

    return {"recommendation": recommendation, "consultants": consultants, "api_consultants": api_consultants,
            "api_budget_total_usd": api_budget_usd if api_consultants else 0,
            "execution": "NOT_DISPATCHED", "reasons": reasons}
