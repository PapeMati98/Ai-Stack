"""Deterministic owner/architect escalation policy for team-flow (Task B).

Pure functions, no I/O, no network, no model dispatch. Public API:

- ``validate_case(case)``: strict validation of an escalation case
  (``schema_version: 1``). Raises ``EscalationError`` with a fixed code. Case
  fields are parent-reviewed metadata, never proof of owner consent; a string
  ``"false"`` never passes as a bool, and free text (summary/ticket/LLM answer)
  can never supply ``approved_gates`` or any authorization.
- ``categorize(case, *, jev_signal=None)``: deterministic baseline route
  (``OWNER_DECISION`` / ``OWNER_CLARIFICATION`` / ``TEAM_FIX`` / ``ARCHITECT`` /
  ``LEAD``) plus a separate hard-action gate. Unapproved hard gates keep
  ``action_gate == "OWNER_APPROVAL_REQUIRED"`` regardless of the route; this
  never blocks read-only diagnosis or candidate work, and the result never
  carries ``authorization=True``. The optional JEV signal is SHADOW ONLY: it is
  recorded (including disagreement) and never alters route or action gate. No
  keyword gate pretends to understand business policy from free text.
- ``select_architect(candidates, required_context_tokens, *, now=None)``: pick
  an on-demand architect consultant. Eligible subscription/local ``mythos``
  class advisory/read models first, else eligible ``opus`` by explicit finite
  positive ``coding_rank`` (lower is stronger). Unknown quota stays unknown,
  confirmed-exhausted is excluded, unknown context window or missing read
  capability excludes. Model class and rank must be explicit profile data --
  never guessed from model names. No API fallback, no promotion of unknown
  classes, no invented quota ceilings. The architect is a consultation, never a
  default worker/reviewer; the result dispatches nothing.
- ``calibration_report(labelled_rows)``: offline comparison of labelled
  expectations against baseline/shadow routes. Reporting only: it never
  activates routing at any sample size or confidence value, and synthetic rows
  stay labelled synthetic -- never portrayed as real Helpdesk validation.
- ``guarded_route(case, *, jev_signal=None, now=None)``: pure, deterministic
  overlay on top of the unchanged ``categorize()`` baseline. It can only
  promote a clearly technical (``business_change is False`` and
  ``scope_change is False``, technical category) ``LEAD`` baseline to
  ``ARCHITECT``, and only above the explicit, conservative, uncalibrated
  ``GUARDED_PROMOTION_THRESHOLD`` confidence. It never produces or weakens an
  owner category, a hard-action gate, ``authorization`` or ``no_dispatch``,
  and it never derives ``TEAM_FIX`` from a signal -- that route is only ever
  reached through ``categorize()``'s own reproduced/restores-baseline check.
  A high-confidence signal that conflicts with the baseline (attempting a
  downgrade or a bypass of missing baseline evidence) never changes the
  effective route; it only sets ``parent_review`` so the parent lead resolves the
  conflict. An "owner" signal against already-declared technical metadata
  (``business_change`` and ``scope_change`` both ``False``) is treated as
  that same kind of conflict at ANY confidence level, not only above the
  promotion threshold: a low-confidence owner claim against clear technical
  metadata is a signed disagreement, not noise to discard.
"""
from datetime import datetime, timezone
import sys

from jev_policy import _effective_quota, _finite_number, _parse_iso

sys.dont_write_bytecode = True

SCHEMA_VERSION = 1
SMALL_SAMPLE_THRESHOLD = 30
# Explicit, conservative, uncalibrated: raised only by a labelled calibration
# run against real Helpdesk outcomes, never by model self-report.
GUARDED_PROMOTION_THRESHOLD = 0.85
POLICY_MODES = {"shadow", "guarded"}

OWNER_CATEGORIES = {
    "business_logic", "process_flow", "data_requirements",
    "access_scope", "product_strategy",
}
TECHNICAL_CATEGORIES = {"technical_bug", "technical_design"}
VALID_CATEGORIES = OWNER_CATEGORIES | TECHNICAL_CATEGORIES | {"unknown"}

VALID_HARD_GATES = {
    "production", "secrets", "delete", "external_send", "customer_data", "iam",
}

DECISION_ROUTES = {"OWNER_DECISION", "OWNER_CLARIFICATION", "TEAM_FIX", "ARCHITECT", "LEAD"}
_OWNER_ROUTE_GROUP = {"OWNER_DECISION": "OWNER", "OWNER_CLARIFICATION": "OWNER",
                      "TEAM_FIX": "TEAM", "LEAD": "LEAD", "ARCHITECT": "ARCHITECT"}
_JEV_ROUTE_GROUP = {"self": "TEAM", "lead": "LEAD", "architect": "ARCHITECT",
                    "owner": "OWNER", "unknown": None}
VALID_JEV_ROUTES = set(_JEV_ROUTE_GROUP)

VALID_MODEL_CLASSES = {"mythos", "opus", "sonnet", "luna"}
_VALID_BILLING = {"subscription", "local", "api"}
_VALID_STATUS = {"available", "exhausted", "unknown"}
_VALID_TIERS = {"D", "M", "K", "W", "DENY", None}
# Owner decision 2026-09-29: escalate to a Manager first, a Director only when no Manager qualifies.
ESCALATION_ORDER = ("M", "D")

_NEXT_STEP = {
    "OWNER_DECISION": "ASK_OWNER_DECISION",
    "OWNER_CLARIFICATION": "ASK_OWNER_CLARIFICATION",
    "TEAM_FIX": "TEAM_FIX_CANDIDATE",
    "ARCHITECT": "CONSULT_ARCHITECT",
    "LEAD": "TEAM_LEAD_REVIEW",
}

_CASE_FIELDS = {
    "schema_version", "id", "summary", "category", "baseline_reference",
    "business_change", "scope_change", "restores_baseline", "reproduced",
    "hard_gates", "approved_gates", "technical_uncertainty",
}


class EscalationError(ValueError):
    """Raised for malformed case/candidate/row input. The message is a fixed code."""


# --------------------------------------------------------------------------- case validation

def _strict_bool(value):
    return isinstance(value, bool)


def _gate_list(value):
    return (
        isinstance(value, list)
        and all(isinstance(item, str) and item in VALID_HARD_GATES for item in value)
        and len(set(value)) == len(value)
    )


def validate_case(case):
    if not isinstance(case, dict):
        raise EscalationError("CASE_NOT_DICT")
    unknown = set(case) - _CASE_FIELDS
    if unknown:
        raise EscalationError("CASE_UNKNOWN_FIELD")
    if type(case.get("schema_version")) is not int or case.get("schema_version") != SCHEMA_VERSION:
        raise EscalationError("CASE_SCHEMA_VERSION")
    if not isinstance(case.get("id"), str) or not case["id"] or len(case["id"]) > 200:
        raise EscalationError("CASE_ID_INVALID")
    summary = case.get("summary")
    if not isinstance(summary, str) or len(summary) > 2000:
        raise EscalationError("CASE_SUMMARY_INVALID")
    if not isinstance(case.get("category"), str) or case.get("category") not in VALID_CATEGORIES:
        raise EscalationError("CASE_CATEGORY_INVALID")
    baseline_reference = case.get("baseline_reference")
    if not isinstance(baseline_reference, str) or len(baseline_reference) > 500:
        raise EscalationError("CASE_BASELINE_REFERENCE_INVALID")
    for field in ("business_change", "scope_change"):
        value = case.get(field)
        if value is not None and not _strict_bool(value):
            raise EscalationError(f"CASE_{field.upper()}_INVALID")
    for field in ("restores_baseline", "reproduced", "technical_uncertainty"):
        if not _strict_bool(case.get(field)):
            raise EscalationError(f"CASE_{field.upper()}_INVALID")
    if not _gate_list(case.get("hard_gates")):
        raise EscalationError("CASE_HARD_GATES_INVALID")
    approved = case.get("approved_gates")
    if not _gate_list(approved):
        raise EscalationError("CASE_APPROVED_GATES_INVALID")
    if not set(approved).issubset(set(case["hard_gates"])):
        raise EscalationError("CASE_APPROVED_GATE_NOT_DECLARED")
    return case


# --------------------------------------------------------------------------- categorize

def _shadow_signal(jev_signal, baseline_route):
    """Record the JEV route signal without ever applying it.

    Missing or malformed input yields an explicit ``unavailable`` status; a
    numeric confidence is never silently converted into truth and disagreement
    with the deterministic baseline is recorded, not resolved in JEV's favour.
    """
    if jev_signal is None:
        return {"status": "unavailable", "reason": "SIGNAL_MISSING", "applied": False}
    route = jev_signal.get("route") if isinstance(jev_signal, dict) else None
    confidence = jev_signal.get("confidence") if isinstance(jev_signal, dict) else None
    if not isinstance(route, str) or route not in VALID_JEV_ROUTES or not _finite_number(confidence) or not (0.0 <= confidence <= 1.0):
        return {"status": "unavailable", "reason": "SIGNAL_MALFORMED", "applied": False}
    jev_group = _JEV_ROUTE_GROUP[route]
    agrees = None if jev_group is None else jev_group == _OWNER_ROUTE_GROUP[baseline_route]
    return {
        "status": "recorded",
        "route": route,
        "confidence": float(confidence),
        "agrees_with_baseline": agrees,
        "applied": False,
    }


def categorize(case, *, jev_signal=None):
    case = validate_case(case)
    reasons = []

    if case["category"] in OWNER_CATEGORIES:
        route = "OWNER_DECISION"
        reasons.append("OWNER_CATEGORY")
    elif case.get("business_change") is True:
        route = "OWNER_DECISION"
        reasons.append("BUSINESS_CHANGE_DECLARED")
    elif case.get("scope_change") is True:
        route = "OWNER_DECISION"
        reasons.append("SCOPE_CHANGE_DECLARED")
    elif case["category"] == "unknown":
        route = "OWNER_CLARIFICATION"
        reasons.append("CATEGORY_UNKNOWN")
    elif case.get("business_change") is None or case.get("scope_change") is None:
        # Unknown business semantics must be clarified by the owner; technical
        # low confidence alone never forces this route.
        route = "OWNER_CLARIFICATION"
        reasons.append("BUSINESS_SCOPE_FLAGS_UNKNOWN")
    elif (
        case["category"] == "technical_bug"
        and case["reproduced"]
        and case["restores_baseline"]
        and bool(case["baseline_reference"].strip())
        and case["business_change"] is False
        and case["scope_change"] is False
    ):
        # An obvious reproduced regression that restores a known baseline stays
        # in the team; JEV or ticket text cannot downgrade or upgrade this.
        route = "TEAM_FIX"
        reasons.append("REPRODUCED_BASELINE_REGRESSION")
    else:
        if case["category"] == "technical_bug" and not case["baseline_reference"].strip():
            reasons.append("BASELINE_REFERENCE_MISSING")
        if case["technical_uncertainty"]:
            route = "ARCHITECT"
            reasons.append("TECHNICAL_UNCERTAINTY_ARCHITECT")
        else:
            route = "LEAD"
            reasons.append("TECHNICAL_LEAD")

    unapproved = [gate for gate in case["hard_gates"] if gate not in case["approved_gates"]]
    if unapproved:
        # Hard gates keep their own owner-approval requirement regardless of the
        # decision route; read-only diagnosis and candidate work stay possible.
        action_gate = "OWNER_APPROVAL_REQUIRED"
        reasons.append("HARD_GATE_REQUIRES_OWNER_APPROVAL")
    else:
        action_gate = "NONE"
    if case["approved_gates"]:
        # approved_gates only annotates pre-existing owner evidence supplied as
        # parent-reviewed metadata; this function never verifies or creates it.
        reasons.append("APPROVED_GATES_ANNOTATION_ONLY")

    return {
        "schema_version": SCHEMA_VERSION,
        "case_id": case["id"],
        "decision_route": route,
        "action_gate": action_gate,
        "unapproved_gates": unapproved,
        "approved_gates": list(case["approved_gates"]),
        "reasoning_codes": reasons,
        "next_step": _NEXT_STEP[route],
        "no_dispatch": True,
        "authorization": False,
        "jev_shadow": _shadow_signal(jev_signal, route),
    }


# --------------------------------------------------------------------------- guarded overlay



def guarded_route(case, *, jev_signal=None, now=None):
    """Pure guarded overlay: the ``categorize()`` baseline plus a narrow,
    high-confidence LEAD<->ARCHITECT promotion/confirmation for clearly
    technical cases. ``now`` is accepted for interface symmetry with
    ``select_architect`` and is currently unused (routing has no time-based
    rule); it is not validated beyond that no exception leaks a case detail.

    Never touches ``action_gate``, ``authorization`` or ``no_dispatch`` --
    those come from ``categorize()`` unchanged. Never derives ``TEAM_FIX`` or
    an owner route from a signal. A conflicting high-confidence signal keeps
    the baseline and flags ``parent_review`` instead of resolving itself. An
    "owner" signal against already clearly-technical metadata flags
    ``parent_review`` at any confidence level, since that is a declared
    business-vs-technical disagreement, not a routing-confidence question.
    """
    baseline = categorize(case, jev_signal=jev_signal)
    baseline_route = baseline["decision_route"]
    shadow = baseline["jev_shadow"]

    effective_route = baseline_route
    applied = False
    parent_review = False
    reasons = []

    signal_usable = shadow["status"] == "recorded"
    high_confidence = signal_usable and shadow["confidence"] >= GUARDED_PROMOTION_THRESHOLD
    is_clear_technical = (
        case.get("business_change") is False
        and case.get("scope_change") is False
        and case.get("category") in TECHNICAL_CATEGORIES
    )
    eligible_baseline = baseline_route in ("LEAD", "ARCHITECT")

    if not signal_usable:
        reasons.append("JEV_SIGNAL_UNAVAILABLE_BASELINE_RETAINED")
    elif not is_clear_technical:
        # Business/scope semantics are not both declared False (or the
        # category itself is an owner category): no technical-metadata
        # conflict is possible here, so this is just a route not eligible for
        # the overlay (OWNER_*/TEAM_FIX/LEAD/ARCHITECT baselines the signal
        # cannot re-derive or is not clearly technical enough to overlay).
        reasons.append("JEV_ROUTE_NOT_ELIGIBLE_BASELINE_RETAINED" if not eligible_baseline
                       else "JEV_NOT_CLEAR_TECHNICAL_BASELINE_RETAINED")
    elif shadow["route"] == "owner":
        # An "owner" claim against already-declared technical metadata
        # (business_change and scope_change both False) is always a conflict
        # that needs parent review -- at ANY confidence level, and against ANY
        # technical baseline (TEAM_FIX/LEAD/ARCHITECT): a low-confidence
        # owner signal must not be silently discarded as "not eligible" just
        # because the baseline itself is not overlay-eligible, and it must
        # never invent or authorize a business route on its own.
        reasons.append("JEV_OWNER_SIGNAL_CONFLICTS_WITH_TECHNICAL_BASELINE")
        parent_review = True
    elif not eligible_baseline:
        # OWNER_*/TEAM_FIX baselines are never re-derived from a non-owner
        # signal here; only categorize() itself may reach them.
        reasons.append("JEV_ROUTE_NOT_ELIGIBLE_BASELINE_RETAINED")
    elif not high_confidence:
        reasons.append("JEV_LOW_CONFIDENCE_BASELINE_RETAINED")
    elif shadow["route"] == "architect" and baseline_route == "LEAD":
        effective_route = "ARCHITECT"
        applied = True
        reasons.append("JEV_HIGH_CONFIDENCE_ARCHITECT_PROMOTION")
    elif shadow["route"] == "lead" and baseline_route == "LEAD":
        reasons.append("JEV_HIGH_CONFIDENCE_LEAD_CONFIRMED")
    elif shadow["route"] == "architect" and baseline_route == "ARCHITECT":
        reasons.append("JEV_HIGH_CONFIDENCE_ARCHITECT_CONFIRMED")
    else:
        # A "lead"/"self" signal cannot downgrade a known ARCHITECT route
        # (technical_uncertainty already established), and neither a "self",
        # "lead", "owner" nor "unknown" signal against an eligible baseline
        # bypasses missing baseline evidence or invents an owner decision.
        # The baseline is kept; a human resolves the disagreement.
        reasons.append("JEV_CONFLICT_BASELINE_RETAINED")
        parent_review = True

    return {
        "schema_version": SCHEMA_VERSION,
        "case_id": baseline["case_id"],
        "policy_mode": "guarded",
        "baseline_decision_route": baseline_route,
        "decision_route": effective_route,
        "action_gate": baseline["action_gate"],
        "unapproved_gates": baseline["unapproved_gates"],
        "approved_gates": baseline["approved_gates"],
        "next_step": _NEXT_STEP[effective_route],
        "applied": applied,
        "reasons": reasons,
        "parent_review": parent_review,
        "jev_shadow": shadow,
        "no_dispatch": True,
        "authorization": False,
    }


# --------------------------------------------------------------------------- architect selection

def _validate_candidate(candidate, seen_ids):
    if not isinstance(candidate, dict):
        raise EscalationError("CANDIDATE_NOT_DICT")
    candidate_id = candidate.get("id")
    if not isinstance(candidate_id, str) or not candidate_id:
        raise EscalationError("CANDIDATE_ID_INVALID")
    if candidate_id in seen_ids:
        raise EscalationError("CANDIDATE_ID_DUPLICATE")
    seen_ids.add(candidate_id)
    for field in ("agent", "model", "lineage"):
        if not isinstance(candidate.get(field), str) or not candidate.get(field):
            raise EscalationError(f"CANDIDATE_{field.upper()}_INVALID")
    if not isinstance(candidate.get("model_class"), str):
        raise EscalationError("CANDIDATE_MODEL_CLASS_INVALID")
    coding_rank = candidate.get("coding_rank")
    if coding_rank is not None and (not _finite_number(coding_rank) or coding_rank <= 0):
        raise EscalationError("CANDIDATE_CODING_RANK_INVALID")
    for field in ("enabled", "training_allowed"):
        if not _strict_bool(candidate.get(field)):
            raise EscalationError(f"CANDIDATE_{field.upper()}_INVALID")
    if not isinstance(candidate.get("billing"), str) or candidate.get("billing") not in _VALID_BILLING:
        raise EscalationError("CANDIDATE_BILLING_INVALID")
    if not isinstance(candidate.get("status"), str) or candidate.get("status") not in _VALID_STATUS:
        raise EscalationError("CANDIDATE_STATUS_INVALID")
    quota_remaining = candidate.get("quota_remaining")
    if quota_remaining is not None and (
        not _finite_number(quota_remaining) or not (0.0 <= quota_remaining <= 1.0)
    ):
        raise EscalationError("CANDIDATE_QUOTA_REMAINING_INVALID")
    observed_at = candidate.get("quota_observed_at")
    if observed_at is not None:
        _parse_iso(observed_at)
    capacity = candidate.get("capacity_remaining")
    if capacity is not None and (
        not isinstance(capacity, int) or isinstance(capacity, bool) or capacity < 0
    ):
        raise EscalationError("CANDIDATE_CAPACITY_REMAINING_INVALID")
    for field in ("modes", "tools"):
        value = candidate.get(field)
        if not (isinstance(value, list) and all(isinstance(item, str) and item for item in value)):
            raise EscalationError(f"CANDIDATE_{field.upper()}_INVALID")
    window = candidate.get("context_window_tokens")
    if window is not None and (
        not isinstance(window, int) or isinstance(window, bool) or window < 0
    ):
        raise EscalationError("CANDIDATE_CONTEXT_WINDOW_TOKENS_INVALID")
    if candidate.get("tier") not in _VALID_TIERS:
        raise EscalationError("CANDIDATE_TIER_INVALID")
    share = candidate.get("recent_share")
    if share is not None and (not _finite_number(share) or not (0.0 <= share <= 1.0)):
        raise EscalationError("CANDIDATE_RECENT_SHARE_INVALID")


def _normalize_now(now):
    if now is None:
        return datetime.now(timezone.utc)
    if not isinstance(now, datetime):
        raise EscalationError("NOW_INVALID")
    return now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)


def _architect_exclusion_reasons(candidate, required_context_tokens, now):
    reasons = []
    if candidate["enabled"] is not True:
        reasons.append("DISABLED")
    if candidate["training_allowed"] is not False:
        reasons.append("TRAINING_NOT_ALLOWED")
    if candidate["billing"] == "api":
        reasons.append("API_BILLING_NO_FALLBACK")
    if candidate.get("tier") == "DENY":
        reasons.append("CATALOG_DENY")
    if candidate["model_class"] not in VALID_MODEL_CLASSES and candidate.get("tier") not in ESCALATION_ORDER:
        # Class must be explicit data (Fleet profile class or owner catalog tier); never guessed.
        reasons.append("CLASS_UNKNOWN")
    if not any(mode in candidate["modes"] for mode in ("advisory", "review")):
        reasons.append("MODE_UNSUPPORTED")
    if not ({"read", "filesystem-read"} & set(candidate["tools"])):
        reasons.append("NO_READ_CAPABILITY")
    window = candidate.get("context_window_tokens")
    if window is None:
        reasons.append("CONTEXT_UNKNOWN")
    elif window < required_context_tokens:
        reasons.append("CONTEXT_INSUFFICIENT")
    if candidate["status"] == "exhausted":
        reasons.append("EXHAUSTED")
    if candidate.get("capacity_remaining") == 0:
        reasons.append("CAPACITY_ZERO")
    quota_effective = _effective_quota(candidate, now)
    if quota_effective is not None and quota_effective <= 0:
        reasons.append("QUOTA_DEPLETED")
    return reasons


def select_architect(candidates, required_context_tokens, *, now=None):
    if not isinstance(candidates, list):
        raise EscalationError("CANDIDATES_NOT_LIST")
    if (
        not isinstance(required_context_tokens, int)
        or isinstance(required_context_tokens, bool)
        or required_context_tokens < 0
    ):
        raise EscalationError("REQUIRED_CONTEXT_TOKENS_INVALID")
    now = _normalize_now(now)
    seen_ids = set()
    for candidate in candidates:
        _validate_candidate(candidate, seen_ids)

    eligible, excluded = [], []
    for candidate in candidates:
        reasons = _architect_exclusion_reasons(candidate, required_context_tokens, now)
        if reasons:
            excluded.append({"id": candidate["id"], "agent": candidate["agent"], "reasons": reasons})
        elif candidate["billing"] in ("subscription", "local"):
            eligible.append(candidate)

    def rank_key(candidate):
        rank = candidate.get("coding_rank")
        return (0 if rank is not None else 1, rank if rank is not None else 0, candidate["id"])

    mythos_pool = sorted(
        (c for c in eligible if c["model_class"] == "mythos"), key=rank_key,
    )
    opus_pool = sorted(
        (c for c in eligible if c["model_class"] == "opus" and c.get("coding_rank") is not None),
        key=rank_key,
    )

    tiered = [c for c in eligible if c.get("tier") in ESCALATION_ORDER]
    chosen = None
    if tiered:
        # Catalog tiers present: Manager first, Director only as escalation; inside a tier the
        # less recently used model, then explicit coding rank.
        for tier in ESCALATION_ORDER:
            pool = sorted((c for c in tiered if c["tier"] == tier),
                          key=lambda c: (c.get("recent_share") or 0.0, rank_key(c)))
            if pool:
                chosen = pool[0]
                fallback = tier != ESCALATION_ORDER[0]
                reason = "MANAGER_FIRST" if not fallback else "DIRECTOR_ESCALATION_NO_MANAGER"
                break
        if chosen is None:
            fallback, reason = False, "NO_ELIGIBLE_ARCHITECT"
    elif mythos_pool:
        chosen, fallback, reason = mythos_pool[0], False, "MYTHOS_SUBSCRIPTION_AVAILABLE"
    elif opus_pool:
        chosen, fallback, reason = opus_pool[0], True, "OPUS_RANK_FALLBACK"
    else:
        chosen, fallback, reason = None, False, "NO_ELIGIBLE_ARCHITECT"

    selected = None
    if chosen is not None:
        selected = {
            "id": chosen["id"],
            "agent": chosen["agent"],
            "model": chosen["model"],
            "lineage": chosen["lineage"],
            "model_class": chosen["model_class"],
            "billing": chosen["billing"],
            "coding_rank": chosen.get("coding_rank"),
            "tier": chosen.get("tier"),
            "consultation_only": True,
        }
    return {
        "status": "SELECTED" if selected is not None else "UNAVAILABLE",
        "selected": selected,
        "fallback": fallback,
        "reason": reason,
        "excluded": excluded,
        "no_dispatch": True,
        "authorization": False,
    }


# --------------------------------------------------------------------------- calibration

_ROW_FIELDS = {"expected_route", "baseline_route", "shadow_route", "synthetic", "row_id"}


def _validate_row(row):
    if not isinstance(row, dict):
        raise EscalationError("ROW_NOT_DICT")
    if set(row) - _ROW_FIELDS:
        raise EscalationError("ROW_UNKNOWN_FIELD")
    if not isinstance(row.get("expected_route"), str) or row.get("expected_route") not in DECISION_ROUTES:
        raise EscalationError("ROW_EXPECTED_ROUTE_INVALID")
    if not isinstance(row.get("baseline_route"), str) or row.get("baseline_route") not in DECISION_ROUTES:
        raise EscalationError("ROW_BASELINE_ROUTE_INVALID")
    shadow = row.get("shadow_route")
    if shadow is not None and (not isinstance(shadow, str) or shadow not in VALID_JEV_ROUTES):
        raise EscalationError("ROW_SHADOW_ROUTE_INVALID")
    if not _strict_bool(row.get("synthetic")):
        raise EscalationError("ROW_SYNTHETIC_INVALID")
    row_id = row.get("row_id")
    if row_id is not None and (not isinstance(row_id, str) or not row_id):
        raise EscalationError("ROW_ID_INVALID")


def calibration_report(labelled_rows):
    """Confusion counts and agreement for labelled rows. Reporting only: no
    sample size or confidence value ever activates routing, and synthetic rows
    are never portrayed as real Helpdesk validation."""
    if not isinstance(labelled_rows, list):
        raise EscalationError("ROWS_NOT_LIST")
    for row in labelled_rows:
        _validate_row(row)

    sample_size = len(labelled_rows)
    synthetic_count = sum(1 for row in labelled_rows if row["synthetic"])
    confusion = {}
    matches = 0
    owner_false_negatives = 0
    unknown_count = 0
    shadow_disagreements = 0
    for row in labelled_rows:
        expected, baseline = row["expected_route"], row["baseline_route"]
        confusion.setdefault(expected, {})[baseline] = confusion.setdefault(expected, {}).get(baseline, 0) + 1
        if expected == baseline:
            matches += 1
        if _OWNER_ROUTE_GROUP[expected] == "OWNER" and _OWNER_ROUTE_GROUP[baseline] != "OWNER":
            owner_false_negatives += 1
        shadow = row.get("shadow_route")
        if shadow is None or shadow == "unknown":
            unknown_count += 1
        elif _JEV_ROUTE_GROUP[shadow] != _OWNER_ROUTE_GROUP[baseline]:
            shadow_disagreements += 1

    return {
        "sample_size": sample_size,
        "synthetic_count": synthetic_count,
        "real_count": sample_size - synthetic_count,
        "synthetic_only": sample_size > 0 and synthetic_count == sample_size,
        "validation_basis": "SYNTHETIC_NOT_HELPDESK" if synthetic_count else "LABELLED_ROWS",
        "agreement": (matches / sample_size) if sample_size else None,
        "confusion": confusion,
        "owner_false_negatives": owner_false_negatives,
        "unknown_count": unknown_count,
        "shadow_disagreements": shadow_disagreements,
        "small_sample": sample_size < SMALL_SAMPLE_THRESHOLD,
        "caveat": "SMALL_SAMPLE_NO_ACTIVATION" if sample_size < SMALL_SAMPLE_THRESHOLD else "NO_AUTOMATIC_ACTIVATION",
        "activation": "NEVER_AUTOMATIC",
    }
