"""Bounded JEV routing advisor client (advisory only, schema_version=1).

Fixed-endpoint OpenRouter Decisions client for the typesafe/jev-1.13 model.
No file I/O, no ambient credential reads, no redirects, no retries. All
errors raise JevError with a fixed sanitized code; messages never include
the request body, response body, prompt or API key.

Design source: docs/JEV-ROUTING-DESIGN.md (official API shape verified
2026-09-25 against the OpenRouter JEV tutorial and the models endpoints
pricing endpoint). Candidate only; nothing here is wired into live Fleet.
"""

import json
import math
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta

MODEL = "typesafe/jev-1.13"
PROVIDER = "TypeSafe"
ENDPOINT = "https://openrouter.ai/api/alpha/decisions"

SCHEMA_VERSION = 1
MAX_BRIEF_CHARS = 4000
MAX_REQUEST_BYTES = 24000
MAX_RESPONSE_BYTES = 256 * 1024
RESERVE_OUTPUT_TOKENS = 512

# Pinned pricing verified 2026-09-25 via
# https://openrouter.ai/api/v1/models/typesafe/jev-1.13/endpoints
# (input 0.000000042 USD/token, completion 0). The pre-send cap protects
# this bound only; provider repricing is not covered.
INPUT_USD_PER_TOKEN = 0.000000042
COMPLETION_USD_PER_TOKEN = 0.0
PRICING_BASIS = {
    "input_usd_per_token": INPUT_USD_PER_TOKEN,
    "completion_usd_per_token": COMPLETION_USD_PER_TOKEN,
    "verified_at": "2026-09-25",
    "source": "openrouter.ai/api/v1/models/typesafe/jev-1.13/endpoints",
    "note": "pinned estimate; provider repricing is not covered by the cap",
}

MODES = ("implement", "advisory", "review")
BILLING = ("subscription", "local", "api")
STATUSES = ("available", "exhausted", "unknown")
DIFFICULTY_CHOICES = ("low", "medium", "high")
PARALLELISM_CHOICES = ("sequential", "parallel", "unknown")
CONSULTATION_CHOICES = ("none", "subscription", "api_candidate")
SCORE_CRITERIA = ["unsuitable", "weak", "adequate", "strong", "excellent"]

_SAFE_ID = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
_DATED_SUFFIX = re.compile(r"-\d{8}")
_DRIVE_PATH = re.compile(r"[A-Za-z]:[\\/]")
_TOKEN_MARKER = re.compile(r"(?i)(\bbearer\s+\S|(?<![A-Za-z0-9_])sk-[A-Za-z0-9_-]{8,})")

_TASK_KEYS = {
    "schema_version", "brief", "mode", "required_tools", "scope",
    "resources", "depends_on", "estimated_context_tokens", "domain",
    "high_risk",
}
_SNAPSHOT_KEYS = {"captured_at", "candidates", "active_assignments", "capacity"}
_CANDIDATE_KEYS = {
    "id", "agent", "model", "lineage", "enabled", "training_allowed",
    "billing", "tools", "modes", "fit", "status", "quota_remaining",
    "quota_observed_at", "quota_reset_at", "context_window_tokens",
    "capacity_remaining",
}
_OPTIONAL_CANDIDATE_KEYS = {"model_class", "coding_rank", "tier", "recent_share", "conserve"}
_TIERS = {"D", "M", "K", "W", "DENY", None}
_MODEL_CLASSES = {"mythos", "opus", "sonnet", "luna", "unknown"}
_ASSIGNMENT_KEYS = {"candidate_id", "lineage", "scope", "resources"}


class JevError(Exception):
    """Sanitized failure; .code is a fixed machine-readable string."""

    def __init__(self, code):
        self.code = code
        super().__init__("jev_client: %s" % code)


def _fail(code):
    raise JevError(code)


def _is_finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value)


def _check_iso(value):
    if not isinstance(value, str) or not value:
        _fail("invalid_input")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        _fail("invalid_input")
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        _fail("invalid_input")


def _check_string_list(value):
    if not isinstance(value, list):
        _fail("invalid_input")
    for item in value:
        if not isinstance(item, str) or not item:
            _fail("invalid_input")


def _check_no_sensitive_text(value):
    """Reject path-like and token-like content in any nested string."""
    if isinstance(value, str):
        if _DRIVE_PATH.search(value) or value.startswith("\\\\") \
                or _TOKEN_MARKER.search(value):
            _fail("privacy_refused")
    elif isinstance(value, list):
        for item in value:
            _check_no_sensitive_text(item)
    elif isinstance(value, dict):
        for item in value.values():
            _check_no_sensitive_text(item)


def _validate_task(task):
    if not isinstance(task, dict) or set(task) != _TASK_KEYS:
        _fail("invalid_input")
    if task["schema_version"] != SCHEMA_VERSION:
        _fail("invalid_input")
    if not isinstance(task["brief"], str) or not task["brief"] \
            or len(task["brief"]) > MAX_BRIEF_CHARS:
        _fail("invalid_input")
    if task["mode"] not in MODES:
        _fail("invalid_input")
    _check_string_list(task["required_tools"])
    _check_string_list(task["scope"])
    _check_string_list(task["resources"])
    _check_string_list(task["depends_on"])
    if not isinstance(task["estimated_context_tokens"], int) \
            or isinstance(task["estimated_context_tokens"], bool) \
            or task["estimated_context_tokens"] < 0:
        _fail("invalid_input")
    if not isinstance(task["domain"], str) or not task["domain"]:
        _fail("invalid_input")
    if not isinstance(task["high_risk"], bool):
        _fail("invalid_input")
    _check_no_sensitive_text(task)


def _validate_candidate(candidate):
    if not isinstance(candidate, dict) or not _CANDIDATE_KEYS <= set(candidate) \
            or set(candidate) - _CANDIDATE_KEYS - _OPTIONAL_CANDIDATE_KEYS:
        _fail("invalid_input")
    if not isinstance(candidate["id"], str) \
            or not _SAFE_ID.match(candidate["id"]):
        _fail("invalid_input")
    for key in ("agent", "model", "lineage"):
        if not isinstance(candidate[key], str) or not candidate[key]:
            _fail("invalid_input")
    if not isinstance(candidate["enabled"], bool) \
            or not isinstance(candidate["training_allowed"], bool):
        _fail("invalid_input")
    if candidate["billing"] not in BILLING:
        _fail("invalid_input")
    _check_string_list(candidate["tools"])
    _check_string_list(candidate["modes"])
    if not candidate["modes"] or not set(candidate["modes"]) <= set(MODES):
        _fail("invalid_input")
    if "model_class" in candidate and (not isinstance(candidate["model_class"], str)
                                       or candidate["model_class"] not in _MODEL_CLASSES):
        _fail("invalid_input")
    if "coding_rank" in candidate:
        rank = candidate["coding_rank"]
        if rank is not None and (not _is_finite_number(rank) or rank <= 0):
            _fail("invalid_input")
    if "tier" in candidate and candidate["tier"] not in _TIERS:
        _fail("invalid_input")
    if "recent_share" in candidate and not (_is_finite_number(candidate["recent_share"])
                                            and 0 <= candidate["recent_share"] <= 1):
        _fail("invalid_input")
    if "conserve" in candidate and not isinstance(candidate["conserve"], bool):
        _fail("invalid_input")
    if not isinstance(candidate["fit"], dict):
        _fail("invalid_input")
    for score in candidate["fit"].values():
        if not _is_finite_number(score) or not 0 <= score <= 1:
            _fail("invalid_input")
    if candidate["status"] not in STATUSES:
        _fail("invalid_input")
    quota = candidate["quota_remaining"]
    if quota is not None and not (_is_finite_number(quota) and 0 <= quota <= 1):
        _fail("invalid_input")
    for key in ("quota_observed_at", "quota_reset_at"):
        if candidate[key] is not None:
            _check_iso(candidate[key])
    for key in ("context_window_tokens", "capacity_remaining"):
        value = candidate[key]
        if value is not None and (not isinstance(value, int)
                                  or isinstance(value, bool) or value < 0):
            _fail("invalid_input")
    _check_no_sensitive_text(candidate)


def _validate_snapshot(snapshot):
    if not isinstance(snapshot, dict) or set(snapshot) != _SNAPSHOT_KEYS:
        _fail("invalid_input")
    _check_iso(snapshot["captured_at"])
    if not isinstance(snapshot["candidates"], list):
        _fail("invalid_input")
    seen = set()
    for candidate in snapshot["candidates"]:
        _validate_candidate(candidate)
        if candidate["id"] in seen:
            _fail("invalid_input")
        seen.add(candidate["id"])
    if not isinstance(snapshot["active_assignments"], list):
        _fail("invalid_input")
    for assignment in snapshot["active_assignments"]:
        if not isinstance(assignment, dict) \
                or set(assignment) != _ASSIGNMENT_KEYS:
            _fail("invalid_input")
        if not isinstance(assignment["candidate_id"], str) \
                or not _SAFE_ID.match(assignment["candidate_id"]):
            _fail("invalid_input")
        if not isinstance(assignment["lineage"], str):
            _fail("invalid_input")
        _check_string_list(assignment["scope"])
        _check_string_list(assignment["resources"])
    if not isinstance(snapshot["capacity"], int) \
            or isinstance(snapshot["capacity"], bool) \
            or snapshot["capacity"] < 0:
        _fail("invalid_input")
    _check_no_sensitive_text(snapshot)


def _build_questions(candidate_ids):
    questions = {
        "difficulty": {
            "type": "choice",
            "instructions": "Overall difficulty of the task",
            "criteria": {
                "low": "Routine, well-specified, single-file or cosmetic work",
                "medium": "Multi-step work needing care but no novel design",
                "high": "Hard reasoning, cross-module change or high uncertainty",
            },
        },
        "parallelism": {
            "type": "choice",
            "instructions": "Can this task run in parallel with other tasks",
            "criteria": {
                "sequential": "Shares scope, resources or dependencies; run alone",
                "parallel": "Disjoint scope and resources; safe to run alongside",
                "unknown": "Not enough evidence to decide",
            },
        },
        "consultation": {
            "type": "choice",
            "instructions": "Does this task benefit from an advisor council",
            "criteria": {
                "none": "Routine small deterministic task; no consultation",
                "subscription": "Consult eligible subscription or local advisors",
                "api_candidate": "May justify a bounded paid API consultation",
            },
        },
    }
    for cid in candidate_ids:
        questions["fit_%s" % cid] = {
            "type": "score",
            "instructions": "Fit of candidate %s for this task" % cid,
            "criteria": list(SCORE_CRITERIA),
        }
    return questions


def build_request(task, snapshot):
    """Validate inputs and build the Decisions request body (dict)."""
    _validate_task(task)
    _validate_snapshot(snapshot)
    candidate_ids = [c["id"] for c in snapshot["candidates"]]
    body = {
        "model": MODEL,
        "provider": {
            "data_collection": "deny",
            "allow_fallbacks": False,
            "only": [PROVIDER],
        },
        "state": {
            "task": task,
            "candidates": snapshot["candidates"],
            "captured_at": snapshot["captured_at"],
            "active_assignments": snapshot["active_assignments"],
            "capacity": snapshot["capacity"],
        },
        "questions": _build_questions(candidate_ids),
    }
    if len(_serialize(body)) > MAX_REQUEST_BYTES:
        _fail("input_too_large")
    return body


def _serialize(body):
    return json.dumps(body, ensure_ascii=True, separators=(",", ":")) \
        .encode("utf-8")


def _check_model_id(value):
    if not isinstance(value, str):
        _fail("invalid_response")
    if value == MODEL:
        return
    if value.startswith(MODEL) and _DATED_SUFFIX.fullmatch(value[len(MODEL):]):
        return
    _fail("model_mismatch")


def _check_confidence(value):
    if not _is_finite_number(value) or not 0 <= value <= 1:
        _fail("invalid_response")
    return float(value)


def _check_probabilities(value, expected_keys):
    if not isinstance(value, dict) or set(value) != set(expected_keys):
        _fail("invalid_response")
    for probability in value.values():
        if not _is_finite_number(probability) or not 0 <= probability <= 1:
            _fail("invalid_response")


def _parse_choice(answer, allowed):
    if not isinstance(answer, dict) or answer.get("type") != "choice":
        _fail("invalid_response")
    choice = answer.get("choice")
    if choice not in allowed:
        _fail("invalid_response")
    confidence = _check_confidence(answer.get("confidence"))
    _check_probabilities(answer.get("probabilities"), allowed)
    return {"value": choice, "confidence": confidence}


def _parse_score(answer):
    if not isinstance(answer, dict) or answer.get("type") != "score":
        _fail("invalid_response")
    score = answer.get("score")
    if not _is_finite_number(score) or not 0 <= score <= 4:
        _fail("invalid_response")
    confidence = _check_confidence(answer.get("confidence"))
    _check_probabilities(answer.get("probabilities"),
                         [str(n) for n in range(5)])
    return {"score": float(score), "confidence": confidence}


def _parse_usage(value):
    if not isinstance(value, dict):
        _fail("invalid_response")
    usage = {"input_tokens": None, "output_tokens": None, "cost_usd": None}
    for key in ("input_tokens", "output_tokens"):
        token_count = value.get(key)
        if token_count is not None:
            if not isinstance(token_count, int) \
                    or isinstance(token_count, bool) or token_count < 0:
                _fail("invalid_response")
            usage[key] = token_count
    cost = value.get("cost")
    if cost is not None:
        # Unknown cost stays None; never coerced to zero.
        if not _is_finite_number(cost) or cost < 0:
            _fail("invalid_response")
        usage["cost_usd"] = float(cost)
    return usage


def parse_response(response, request):
    """Validate and normalize a Decisions response against its request."""
    if not isinstance(response, dict):
        _fail("invalid_response")
    if not isinstance(request, dict) or not isinstance(request.get("state"), dict):
        _fail("invalid_input")
    if request.get("provider") != {
            "data_collection": "deny",
            "allow_fallbacks": False,
            "only": [PROVIDER]}:
        _fail("invalid_input")
    decision_id = response.get("id")
    if not isinstance(decision_id, str) or not decision_id:
        _fail("invalid_response")
    _check_model_id(response.get("model"))
    provider = response.get("provider")
    if provider != PROVIDER:
        _fail("provider_mismatch")
    answers = response.get("answers")
    if not isinstance(answers, dict):
        _fail("invalid_response")
    candidates = request["state"].get("candidates")
    if not isinstance(candidates, list):
        _fail("invalid_input")
    candidate_ids = [c["id"] for c in candidates
                     if isinstance(c, dict) and isinstance(c.get("id"), str)]
    expected = {"difficulty", "parallelism", "consultation"}
    expected.update("fit_%s" % cid for cid in candidate_ids)
    if set(answers) != expected:
        _fail("invalid_response")
    fit = {}
    for cid in candidate_ids:
        fit[cid] = _parse_score(answers["fit_%s" % cid])
    return {
        "model": response["model"],
        "provider": provider,
        "decision_id": decision_id,
        "difficulty": _parse_choice(answers["difficulty"], DIFFICULTY_CHOICES),
        "parallelism": _parse_choice(answers["parallelism"], PARALLELISM_CHOICES),
        "consultation": _parse_choice(answers["consultation"], CONSULTATION_CHOICES),
        "fit": fit,
        "usage": _parse_usage(response.get("usage")),
        "pricing_basis": dict(PRICING_BASIS),
    }


def validate_normalized_decision(decision, snapshot):
    """Check a stored decision's shape and candidate ids; this is not authentication."""
    expected = {"model", "provider", "decision_id", "difficulty", "parallelism",
                "consultation", "fit", "usage", "pricing_basis"}
    if not isinstance(decision, dict) or set(decision) != expected:
        _fail("invalid_response")
    _check_model_id(decision["model"])
    if decision["provider"] != PROVIDER or not isinstance(decision["decision_id"], str) \
            or not decision["decision_id"]:
        _fail("invalid_response")
    for field, choices in (("difficulty", DIFFICULTY_CHOICES),
                           ("parallelism", PARALLELISM_CHOICES),
                           ("consultation", CONSULTATION_CHOICES)):
        entry = decision[field]
        if not isinstance(entry, dict) or set(entry) != {"value", "confidence"} \
                or entry["value"] not in choices:
            _fail("invalid_response")
        _check_confidence(entry["confidence"])
    candidate_ids = {c["id"] for c in snapshot["candidates"]}
    fit = decision["fit"]
    if not isinstance(fit, dict) or set(fit) != candidate_ids:
        _fail("invalid_response")
    for entry in fit.values():
        if not isinstance(entry, dict) or set(entry) != {"score", "confidence"} \
                or not _is_finite_number(entry["score"]) or not 0 <= entry["score"] <= 4:
            _fail("invalid_response")
        _check_confidence(entry["confidence"])
    usage = decision["usage"]
    if not isinstance(usage, dict) or set(usage) != {"input_tokens", "output_tokens", "cost_usd"}:
        _fail("invalid_response")
    for field in ("input_tokens", "output_tokens"):
        value = usage[field]
        if value is not None and (type(value) is not int or value < 0):
            _fail("invalid_response")
    cost = usage["cost_usd"]
    if cost is not None and (not _is_finite_number(cost) or cost < 0):
        _fail("invalid_response")
    if decision["pricing_basis"] != PRICING_BASIS:
        _fail("invalid_response")
    return decision


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _cost_bound(request_bytes):
    return (len(request_bytes) + RESERVE_OUTPUT_TOKENS) * INPUT_USD_PER_TOKEN


def post_decision(payload, api_key, *, max_cost_usd, timeout=15, opener=None):
    """Send one bounded Decisions POST and return the raw parsed JSON body.

    Fixed-endpoint transport shared by every Jev caller in this skill: no
    redirects, no retries, sanitized fixed error codes. This function does not
    know about any particular question set -- callers build their own
    ``payload`` bytes and parse the returned dict against their own schema
    (see :func:`request_decision` for the routing question set, or
    ``escalation_advisor.py`` for the escalation route question).
    """
    if not isinstance(api_key, str) or not api_key:
        _fail("invalid_input")
    if not _is_finite_number(max_cost_usd) or max_cost_usd <= 0:
        _fail("budget_exceeded")
    if not _is_finite_number(timeout) or timeout <= 0:
        _fail("invalid_input")
    if _cost_bound(payload) > max_cost_usd:
        _fail("budget_exceeded")
    request = urllib.request.Request(
        ENDPOINT, data=payload, method="POST",
        headers={
            "Authorization": "Bearer %s" % api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
    if opener is None:
        opener = urllib.request.build_opener(_NoRedirect())
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        code = error.code
        if type(code) is not int or not 100 <= code <= 599:
            _fail("transport_error")
        if 300 <= code < 400:
            _fail("redirect_refused")
        _fail("http_error_%d" % code)
    except Exception:
        _fail("transport_error")
    try:
        with response:
            final_url = getattr(response, "url", None) or ENDPOINT
            if final_url != ENDPOINT:
                _fail("redirect_refused")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except JevError:
        raise
    except Exception:
        _fail("transport_error")
    if not isinstance(raw, bytes):
        _fail("invalid_response")
    if len(raw) > MAX_RESPONSE_BYTES:
        _fail("response_too_large")
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        _fail("invalid_response")


def request_decision(task, snapshot, api_key, *, max_cost_usd, timeout=15,
                     opener=None):
    """Send one bounded Decisions request and return the normalized decision.

    No redirects, no retries. The privacy block (data_collection deny,
    no fallbacks, TypeSafe only) is part of the request; if the endpoint
    refuses it the error propagates and the fields are never stripped.
    """
    body = build_request(task, snapshot)
    payload = _serialize(body)
    parsed = post_decision(payload, api_key, max_cost_usd=max_cost_usd,
                           timeout=timeout, opener=opener)
    return parse_response(parsed, body)
