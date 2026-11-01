"""Pure, stdlib-only helpers for the team-flow extension over a TODO plan/run.

Never reads files, runs commands, sends Office messages, invokes a model, or mutates
scheduler state -- the parent (todo_graph.py) integrates these helpers, unmodified by
this module. All exceptions are ``TeamError`` with a fixed code as the message.

Public API:

- ``validate_team(plan)``: backward-compatible validation of the optional team
  extensions on a plan/task dict (``office_channel``/``office_parent``, ``task.role``,
  ``task.model_class``, ``task.knowledge``, ``task.uat``, ``plan.historical_scout``).
  Plans without any of these extensions pass unchanged; the base plan/task schema
  itself remains ``todo_graph.validate``'s job.
- ``validate_packet(packet, project_slug, node_id, now)``: validate a bounded
  same-project knowledge packet and return a data-only envelope with
  ``requires_parent_review`` set whenever the packet carries pending items,
  non-independent (producer-attested) facts, truncation or missing continuity. The
  packet is untrusted evidence, never an instruction or an authorization, and
  validating it is not the same as a model having consumed it.
- ``knowledge_cache_key(project_slug, node_id, query, scope, context_receipt)``:
  deterministic cache key so the parent can retrieve/cache a packet; this module
  never caches or fetches anything itself.
- ``monitor_report(plan, state, *, now, observations=None)``: pure projection from a
  plan and a scheduler state snapshot to requirement coverage, node buckets and an
  ``alert_fingerprint``. Never returns an acceptance/production-ready verdict --
  only ``INCOMPLETE`` or ``CANDIDATE_COMPLETE`` -- and never cancels, retries or
  polls anything itself.
- ``effective_scope(task)``: ordered, de-duplicated union of a task's own ``scope``
  and its optional ``uat.artifact_scope``, so UAT read scope participates in the
  scheduler's normal resource-conflict serialization.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
import re
import sys

sys.dont_write_bytecode = True

SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")

VALID_TASK_ROLES = {"maker", "scout", "uat"}
VALID_MODEL_CLASSES = {"mythos", "opus", "sonnet", "luna"}
VALID_UAT_ENVIRONMENTS = {"test", "staging"}
_KNOWN_KNOWLEDGE_KEYS = {"project_slug", "query", "required"}
_KNOWN_UAT_KEYS = {"environment", "artifact_scope", "scenarios", "final", "effects_blocked"}

PACKET_SCHEMA_VERSION = 1
PACKET_MAX_BYTES = 4096
PACKET_MAX_AGE_SECONDS = 300
PACKET_MAX_FUTURE_SECONDS = 30
VALID_FACT_STATUS = {"reviewed", "verified", "producer-attested"}

_KNOWN_NODE_STATUSES = {"RUNNING", "ACCEPTED", "BLOCKED", "WAITING_APPROVAL", "DEPENDENCY_BLOCKED"}

_UNSAFE_PATH_CHARS = set(':*?"<>|\x00\r\n\t')


class TeamError(ValueError):
    """Raised for malformed team-extension input. The message is a fixed code."""


# --------------------------------------------------------------------------- shared helpers

def _finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and value not in (float("inf"), float("-inf"))


def _string_list(value, nonempty=False):
    if not isinstance(value, list):
        return False
    if nonempty and not value:
        return False
    return all(isinstance(item, str) and item for item in value)


def _safe_relative_path(value):
    if not isinstance(value, str) or not value:
        return False
    if any(char in _UNSAFE_PATH_CHARS for char in value) or any(ord(char) < 32 for char in value):
        return False
    normalized = value.replace("\\", "/")
    if normalized.startswith("/"):
        return False
    parts = normalized.split("/")
    return not any(part in ("", ".", "..") for part in parts)


def _normalize_now(now):
    if not isinstance(now, datetime):
        raise TeamError("NOW_INVALID")
    return now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)


def _parse_zoned_iso(value, code):
    """Parse a timestamp that must carry an explicit UTC offset ("zoned" ISO 8601)."""
    if not isinstance(value, str) or not value:
        raise TeamError(code)
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise TeamError(code) from exc
    if parsed.tzinfo is None:
        raise TeamError(code)
    return parsed.astimezone(timezone.utc)


# --------------------------------------------------------------------------- validate_team

def _validate_safe_id(value, code):
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise TeamError(code)


def _validate_knowledge(knowledge):
    if not isinstance(knowledge, dict):
        raise TeamError("TASK_KNOWLEDGE_NOT_DICT")
    if set(knowledge) - _KNOWN_KNOWLEDGE_KEYS:
        raise TeamError("TASK_KNOWLEDGE_UNKNOWN_KEYS")
    _validate_safe_id(knowledge.get("project_slug"), "TASK_KNOWLEDGE_PROJECT_SLUG_INVALID")
    query = knowledge.get("query")
    if not isinstance(query, str) or not (1 <= len(query) <= 500):
        raise TeamError("TASK_KNOWLEDGE_QUERY_INVALID")
    if not isinstance(knowledge.get("required"), bool):
        raise TeamError("TASK_KNOWLEDGE_REQUIRED_INVALID")


def _validate_uat_block(uat):
    if not isinstance(uat, dict):
        raise TeamError("TASK_UAT_NOT_DICT")
    if set(uat) - _KNOWN_UAT_KEYS:
        raise TeamError("TASK_UAT_UNKNOWN_KEYS")
    environment = uat.get("environment")
    if not isinstance(environment, str) or environment not in VALID_UAT_ENVIRONMENTS:
        raise TeamError("TASK_UAT_ENVIRONMENT_INVALID")
    artifact_scope = uat.get("artifact_scope")
    if not _string_list(artifact_scope, nonempty=True) or not all(_safe_relative_path(p) for p in artifact_scope):
        raise TeamError("TASK_UAT_ARTIFACT_SCOPE_INVALID")
    if not _string_list(uat.get("scenarios"), nonempty=True):
        raise TeamError("TASK_UAT_SCENARIOS_INVALID")
    if not isinstance(uat.get("final"), bool):
        raise TeamError("TASK_UAT_FINAL_INVALID")
    if uat.get("effects_blocked") is not True:
        raise TeamError("TASK_UAT_EFFECTS_BLOCKED_INVALID")


def _validate_team_task(task):
    if not isinstance(task, dict):
        raise TeamError("TASK_NOT_DICT")
    role = task.get("role", "maker")
    if not isinstance(role, str) or role not in VALID_TASK_ROLES:
        raise TeamError("TASK_ROLE_INVALID")
    if role == "scout" and task.get("mode", "implement") != "advisory":
        raise TeamError("TASK_SCOUT_MUST_BE_ADVISORY")
    model_class = task.get("model_class")
    if model_class is not None and (not isinstance(model_class, str) or model_class not in VALID_MODEL_CLASSES):
        raise TeamError("TASK_MODEL_CLASS_INVALID")
    knowledge = task.get("knowledge")
    if knowledge is not None:
        _validate_knowledge(knowledge)
    uat = task.get("uat")
    if uat is not None:
        _validate_uat_block(uat)
    if role == "uat":
        if not task.get("checks"):
            raise TeamError("TASK_UAT_CHECKS_REQUIRED")
        if not task.get("depends_on"):
            raise TeamError("TASK_UAT_DEPENDS_ON_REQUIRED")
        if uat is None:
            raise TeamError("TASK_UAT_BLOCK_REQUIRED")


def validate_team(plan):
    """Validate the optional team extensions on ``plan``; absent extensions pass as-is.

    Does not re-validate or duplicate the base TODO schema owned by
    ``todo_graph.validate`` -- only the ``office_channel``/``office_parent`` plan
    fields and the ``role``/``model_class``/``knowledge``/``uat`` task fields
    introduced by team-flow. Returns ``plan`` unchanged on success.
    """
    if not isinstance(plan, dict):
        raise TeamError("PLAN_NOT_DICT")
    for field in ("office_channel", "office_parent", "context_receipt_agent", "context_receipt_session"):
        value = plan.get(field)
        if value is not None:
            _validate_safe_id(value, f"PLAN_{field.upper()}_INVALID")
    tasks = plan.get("tasks", [])
    if not isinstance(tasks, list):
        raise TeamError("PLAN_TASKS_INVALID")
    for task in tasks:
        _validate_team_task(task)
    if any('knowledge' in task for task in tasks):
        for field in ("context_receipt_agent", "context_receipt_session"):
            _validate_safe_id(plan.get(field), f"PLAN_{field.upper()}_INVALID")
    # Plan-level historical-scout contract (one advisory scout; consumers depend).
    # Absent contract with no scouts stays valid for legacy/pair/test plans.
    import history_scout
    try:
        history_scout.validate_plan_contract(plan)
    except history_scout.ScoutError as exc:
        raise TeamError(str(exc)) from None
    return plan


# --------------------------------------------------------------------------- validate_packet

def _validate_fact(fact):
    if not isinstance(fact, dict):
        raise TeamError("PACKET_FACT_NOT_DICT")
    text = fact.get("text")
    if not isinstance(text, str) or not text:
        raise TeamError("PACKET_FACT_TEXT_INVALID")
    source_ref = fact.get("source_ref")
    if not isinstance(source_ref, str) or not source_ref:
        raise TeamError("PACKET_FACT_SOURCE_REF_INVALID")
    status = fact.get("status")
    if not isinstance(status, str) or status not in VALID_FACT_STATUS:
        raise TeamError("PACKET_FACT_STATUS_INVALID")
    # A source may expose only a calendar day, or no observation date. Preserve
    # that precision; never invent midnight or use packet freshness as fact age.
    if "observed_at" not in fact:
        raise TeamError("PACKET_FACT_OBSERVED_AT_INVALID")
    observed = fact["observed_at"]
    if observed is None:
        return
    if isinstance(observed, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", observed):
        try:
            date.fromisoformat(observed)
        except ValueError:
            raise TeamError("PACKET_FACT_OBSERVED_AT_INVALID") from None
        return
    _parse_zoned_iso(observed, "PACKET_FACT_OBSERVED_AT_INVALID")


def _validate_pending(item):
    if not isinstance(item, dict):
        raise TeamError("PACKET_PENDING_ITEM_NOT_DICT")
    if not isinstance(item.get("summary"), str) or not item.get("summary"):
        raise TeamError("PACKET_PENDING_SUMMARY_INVALID")
    if not isinstance(item.get("source_ref"), str) or not item.get("source_ref"):
        raise TeamError("PACKET_PENDING_SOURCE_REF_INVALID")
    if not isinstance(item.get("handoff_integrity"), str) or not item.get("handoff_integrity"):
        raise TeamError("PACKET_PENDING_HANDOFF_INTEGRITY_INVALID")
    if not isinstance(item.get("live_verification_required"), bool):
        raise TeamError("PACKET_PENDING_LIVE_VERIFICATION_REQUIRED_INVALID")


def validate_packet(packet, project_slug, node_id, now):
    """Validate a bounded same-project knowledge packet; return a data-only envelope.

    Raises ``TeamError`` for any structural/type violation (size, schema version,
    project/node identity mismatch, timestamp shape, or malformed fact/pending
    entries). A packet that parses but carries pending items, a producer-attested
    (non-independent) fact, truncation, or a missing continuity status is not
    rejected -- it comes back with ``requires_parent_review: True`` so the parent,
    never this pure helper, decides what to do with it. Passing validation never
    means a model consumed the packet.
    """
    _validate_safe_id(project_slug, "PACKET_PROJECT_SLUG_ARG_INVALID")
    _validate_safe_id(node_id, "PACKET_NODE_ID_ARG_INVALID")
    now = _normalize_now(now)
    if not isinstance(packet, dict):
        raise TeamError("PACKET_NOT_DICT")
    try:
        size = len(json.dumps(packet, ensure_ascii=False, separators=(",", ":"),
                              allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise TeamError("PACKET_NOT_JSON_SERIALIZABLE") from exc
    if size > PACKET_MAX_BYTES:
        raise TeamError("PACKET_TOO_LARGE")
    if type(packet.get("schema_version")) is not int or packet.get("schema_version") != PACKET_SCHEMA_VERSION:
        raise TeamError("PACKET_SCHEMA_VERSION_INVALID")
    if packet.get("project_slug") != project_slug:
        raise TeamError("PACKET_PROJECT_SLUG_MISMATCH")
    if packet.get("node_id") != node_id:
        raise TeamError("PACKET_NODE_ID_MISMATCH")
    generated_at = packet.get("generated_at")
    parsed_generated_at = _parse_zoned_iso(generated_at, "PACKET_GENERATED_AT_INVALID")
    age_seconds = (now - parsed_generated_at).total_seconds()
    if age_seconds > PACKET_MAX_AGE_SECONDS:
        raise TeamError("PACKET_GENERATED_AT_TOO_OLD")
    if age_seconds < -PACKET_MAX_FUTURE_SECONDS:
        raise TeamError("PACKET_GENERATED_AT_FUTURE")
    context_receipt = packet.get("context_receipt")
    if not isinstance(context_receipt, str) or not context_receipt:
        raise TeamError("PACKET_CONTEXT_RECEIPT_INVALID")
    continuity_status = packet.get("continuity_status")
    if not isinstance(continuity_status, str) or not continuity_status:
        raise TeamError("PACKET_CONTINUITY_STATUS_INVALID")
    facts = packet.get("facts")
    if not isinstance(facts, list):
        raise TeamError("PACKET_FACTS_INVALID")
    for fact in facts:
        _validate_fact(fact)
    pending = packet.get("pending")
    if not isinstance(pending, list):
        raise TeamError("PACKET_PENDING_INVALID")
    for item in pending:
        _validate_pending(item)
    warnings = packet.get("warnings")
    if not _string_list(warnings):
        raise TeamError("PACKET_WARNINGS_INVALID")
    truncated = packet.get("truncated")
    if not isinstance(truncated, bool):
        raise TeamError("PACKET_TRUNCATED_INVALID")

    non_independent_fact = any(fact["status"] == "producer-attested" for fact in facts)
    missing_continuity = continuity_status.strip().upper() == "MISSING"
    limited_fact_dates = any(fact["observed_at"] is None or len(fact["observed_at"]) == 10
                             for fact in facts)
    requires_parent_review = (bool(pending) or non_independent_fact or truncated
                              or missing_continuity or limited_fact_dates
                              or any(w.startswith('context_receipt_stale:') for w in warnings))

    envelope_warnings = list(warnings)
    if limited_fact_dates and "FACT_DATE_PRECISION_LIMITED" not in envelope_warnings:
        envelope_warnings.append("FACT_DATE_PRECISION_LIMITED")
    if truncated and "TRUNCATED" not in envelope_warnings:
        envelope_warnings.append("TRUNCATED")

    return {
        "schema_version": PACKET_SCHEMA_VERSION,
        "project_slug": project_slug,
        "node_id": node_id,
        "generated_at": generated_at,
        "context_receipt": context_receipt,
        "continuity_status": continuity_status,
        "facts": facts,
        "pending": pending,
        "warnings": envelope_warnings,
        "truncated": truncated,
        "requires_parent_review": requires_parent_review,
    }


def knowledge_cache_key(project_slug, node_id, query, scope, context_receipt):
    """Deterministic cache key bound to project/node/query/scope and receipt.

    Only computes the key; retrieval and caching remain the parent's job.
    """
    _validate_safe_id(project_slug, "CACHE_KEY_PROJECT_SLUG_INVALID")
    _validate_safe_id(node_id, "CACHE_KEY_NODE_ID_INVALID")
    if not isinstance(query, str) or not query:
        raise TeamError("CACHE_KEY_QUERY_INVALID")
    if not _string_list(scope):
        raise TeamError("CACHE_KEY_SCOPE_INVALID")
    if not isinstance(context_receipt, str) or not context_receipt:
        raise TeamError("CACHE_KEY_CONTEXT_RECEIPT_INVALID")
    payload = {
        "project_slug": project_slug,
        "node_id": node_id,
        "query": query,
        "scope": sorted(scope),
        "context_receipt": context_receipt,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- monitor_report

def _task_status(nodes, task_id):
    node = nodes.get(task_id)
    if node is None:
        return "PENDING"
    status = node.get("status") if isinstance(node, dict) else None
    return status if isinstance(status, str) and status in _KNOWN_NODE_STATUSES else "UNKNOWN"


def monitor_report(plan, state, *, now, observations=None):
    """Pure projection of requirement coverage and node buckets; never an acceptance.

    ``status`` is only ever ``INCOMPLETE`` or ``CANDIDATE_COMPLETE`` -- never an
    ACCEPT or production-ready verdict. A requirement is ``satisfied`` only when
    every task covering it is ACCEPTED; missing evidence, duplicate/unknown
    requirements, unknown state nodes or a contradictory graph terminal status all
    force ``INCOMPLETE``. This never cancels, retries or polls a model; pending
    states remain pending. ``alert_fingerprint`` hashes only the actionable issues
    (never wall clock) so a caller can dedupe unchanged alerts across calls.
    """
    if not isinstance(plan, dict):
        raise TeamError("MONITOR_PLAN_NOT_DICT")
    tasks = plan.get("tasks")
    if not isinstance(tasks, list) or not all(
        isinstance(t, dict) and isinstance(t.get("id"), str) and t.get("id") for t in tasks
    ):
        raise TeamError("MONITOR_PLAN_TASKS_INVALID")
    requirements = plan.get("requirements")
    if not _string_list(requirements):
        raise TeamError("MONITOR_PLAN_REQUIREMENTS_INVALID")
    if not isinstance(state, dict):
        raise TeamError("MONITOR_STATE_NOT_DICT")
    nodes = state.get("nodes")
    if not isinstance(nodes, dict):
        raise TeamError("MONITOR_STATE_NODES_INVALID")
    _normalize_now(now)
    if observations is not None and not isinstance(observations, dict):
        raise TeamError("MONITOR_OBSERVATIONS_INVALID")

    task_ids = [t["id"] for t in tasks]
    task_id_set = set(task_ids)

    seen_requirements, duplicate_requirements = set(), []
    for requirement in requirements:
        if requirement in seen_requirements:
            duplicate_requirements.append(requirement)
        seen_requirements.add(requirement)

    covers_by_requirement = {requirement: [] for requirement in seen_requirements}
    unknown_requirements = set()
    for task in tasks:
        covers = task.get("covers", []) or []
        if not isinstance(covers, list) or not all(isinstance(r, str) for r in covers):
            raise TeamError("MONITOR_TASK_COVERS_INVALID")
        for requirement in covers:
            if requirement in covers_by_requirement:
                covers_by_requirement[requirement].append(task["id"])
            else:
                unknown_requirements.add(requirement)

    unknown_state_nodes = sorted(set(nodes) - task_id_set)
    statuses = {task_id: _task_status(nodes, task_id) for task_id in task_ids}
    unknown_status_tasks = sorted(t for t, s in statuses.items() if s == "UNKNOWN")

    requirement_report = {}
    for requirement, covering in covers_by_requirement.items():
        satisfied = bool(covering) and all(statuses[t] == "ACCEPTED" for t in covering)
        requirement_report[requirement] = {"covering_tasks": sorted(covering), "satisfied": satisfied}
    uncovered_requirements = sorted(r for r, covering in covers_by_requirement.items() if not covering)

    stalled_candidates = []
    for node_id, observation in (observations or {}).items():
        if not isinstance(observation, dict):
            raise TeamError("MONITOR_OBSERVATION_ENTRY_INVALID")
        elapsed, timeout = observation.get("elapsed_seconds"), observation.get("timeout_seconds")
        if elapsed is None or timeout is None:
            continue
        if not _finite_number(elapsed) or not _finite_number(timeout) or timeout <= 0:
            raise TeamError("MONITOR_OBSERVATION_TIMING_INVALID")
        if elapsed > timeout:
            stalled_candidates.append({
                "node_id": node_id, "elapsed_seconds": elapsed, "timeout_seconds": timeout,
                "delivery_state": observation.get("delivery_state"),
            })
    stalled_candidates.sort(key=lambda entry: entry["node_id"])

    issues = []
    if duplicate_requirements:
        issues.append("DUPLICATE_REQUIREMENTS")
    if unknown_requirements:
        issues.append("UNKNOWN_REQUIREMENTS")
    if unknown_state_nodes:
        issues.append("UNKNOWN_STATE_NODES")
    if unknown_status_tasks:
        issues.append("UNKNOWN_TASK_STATUS")
    if uncovered_requirements:
        issues.append("UNCOVERED_REQUIREMENTS")
    if stalled_candidates:
        issues.append("STALLED_CANDIDATES")

    all_satisfied = bool(requirement_report) and all(entry["satisfied"] for entry in requirement_report.values())
    contradictory_terminal = state.get("status") == "ACCEPTED_CANDIDATE" and not all_satisfied
    if contradictory_terminal:
        issues.append("CONTRADICTORY_TERMINAL_STATUS")

    candidate_complete = all_satisfied and not issues
    fingerprint_payload = {
        "issues": sorted(issues),
        "duplicate_requirements": sorted(duplicate_requirements),
        "unknown_requirements": sorted(unknown_requirements),
        "unknown_state_nodes": unknown_state_nodes,
        "unknown_status_tasks": unknown_status_tasks,
        "uncovered_requirements": uncovered_requirements,
        "stalled_node_ids": [c["node_id"] for c in stalled_candidates],
    }
    alert_fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
    ).hexdigest()

    return {
        "status": "CANDIDATE_COMPLETE" if candidate_complete else "INCOMPLETE",
        "requirements": requirement_report,
        "uncovered_requirements": uncovered_requirements,
        "unknown_requirements": sorted(unknown_requirements),
        "duplicate_requirements": sorted(set(duplicate_requirements)),
        "unknown_state_nodes": unknown_state_nodes,
        "unknown_status_tasks": unknown_status_tasks,
        "pending_tasks": sorted(t for t, s in statuses.items() if s == "PENDING"),
        "running_tasks": sorted(t for t, s in statuses.items() if s == "RUNNING"),
        "blocked_tasks": sorted(t for t, s in statuses.items() if s in ("BLOCKED", "WAITING_APPROVAL", "DEPENDENCY_BLOCKED")),
        "accepted_tasks": sorted(t for t, s in statuses.items() if s == "ACCEPTED"),
        "stalled_candidates": stalled_candidates,
        "issues": issues,
        "alert_fingerprint": alert_fingerprint,
    }


# --------------------------------------------------------------------------- effective_scope

def effective_scope(task):
    """Ordered, de-duplicated union of ``task['scope']`` and ``task['uat']['artifact_scope']``.

    Conservatively serializes conflicting read/write tests through the scheduler's
    existing resource-conflict check while leaving unrelated work free to run
    concurrently. Introduces no new scheduler and no fixed concurrency cap.
    """
    if not isinstance(task, dict):
        raise TeamError("TASK_NOT_DICT")
    scope = task.get("scope")
    if not _string_list(scope):
        raise TeamError("TASK_SCOPE_INVALID")
    artifact_scope = []
    uat = task.get("uat")
    if uat is not None:
        if not isinstance(uat, dict):
            raise TeamError("TASK_UAT_NOT_DICT")
        artifact_scope = uat.get("artifact_scope")
        if not _string_list(artifact_scope):
            raise TeamError("TASK_UAT_ARTIFACT_SCOPE_INVALID")
    ordered = []
    for item in list(scope) + list(artifact_scope):
        if item not in ordered:
            ordered.append(item)
    return ordered
