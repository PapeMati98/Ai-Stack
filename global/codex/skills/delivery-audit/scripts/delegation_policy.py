"""Typed, deterministic delegation assessment for delivery-audit packets."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
import re
from typing import Mapping, Sequence


class DelegationValidationError(ValueError):
    """Raised when the packet fields used by this policy are malformed."""


class DelegationStatus(str, Enum):
    DELEGATED = "delegated"
    UNDERUSED = "underused"
    EXCEPTION = "exception"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not-applicable"


Tier = str
ExceptionCode = str

_TIERS = frozenset({"D", "M", "K", "W", "unknown"})
_EXCEPTIONS = frozenset(
    {
        "none",
        "trivial",
        "credentials",
        "irreversible",
        "capability-unavailable",
        "quota-exhausted",
        "explicit-owner-model",
        "worker-node",
    }
)
_RUN_ROLES = frozenset({"planner", "maker", "reviewer", "scout", "integrator"})
_RUN_STATUSES = frozenset({"completed", "partial", "failed", "blocked", "unknown"})
_RUN_KEYS = frozenset(
    {
        "run_id",
        "actor_id",
        "model_id",
        "provider",
        "tier",
        "role",
        "status",
        "verified",
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "tool_calls",
    }
)
_DELEGATION_KEYS = frozenset({"parent_tier", "eligible", "exception", "planned_tasks"})
_METRIC_KEYS = frozenset(
    {"wall_seconds", "repair_rounds", "duplicate_work", "uat_required", "uat_status", "evidence_complete"}
)
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,119}\Z", re.ASCII)
_UAT_STATUSES = frozenset({"pass", "fail", "unknown", "not-applicable"})


@dataclass(frozen=True)
class DelegationAssessment:
    status: DelegationStatus
    parent_tier: Tier
    eligible: bool | None
    exception: ExceptionCode
    evidence_complete: bool | None
    planned_tasks: int | None
    completed_child_runs: tuple[str, ...] = ()
    attempted_child_runs: tuple[str, ...] = ()
    reason: str = ""

    @property
    def finding_code(self) -> str | None:
        """Return the safe finding enum only when evidence proves underuse."""
        if self.status is DelegationStatus.UNDERUSED:
            return "missing-delegation"
        return None


def assess_delegation(packet: Mapping[str, object]) -> DelegationAssessment:
    """Assess eligible M/D work from typed packet metadata and actual receipts.

    This helper validates every field it reads. The packet producer must still
    run the complete packet through the core packet validator before enqueue.
    """
    if not isinstance(packet, Mapping):
        raise DelegationValidationError("packet must be a mapping")

    delegation = _mapping_with_exact_keys(packet.get("delegation"), _DELEGATION_KEYS, "delegation")
    metrics = _mapping_with_exact_keys(packet.get("metrics"), _METRIC_KEYS, "metrics")

    parent_tier = _enum_value(delegation["parent_tier"], _TIERS, "delegation.parent_tier")
    eligible = _optional_bool(delegation["eligible"], "delegation.eligible")
    exception = _enum_value(delegation["exception"], _EXCEPTIONS, "delegation.exception")
    planned_tasks = _optional_nonnegative_int(delegation["planned_tasks"], "delegation.planned_tasks")
    evidence_complete = _optional_bool(metrics["evidence_complete"], "metrics.evidence_complete")
    _validate_metrics(metrics)
    runs = _validate_runs(packet.get("runs"))

    base = {
        "parent_tier": parent_tier,
        "eligible": eligible,
        "exception": exception,
        "evidence_complete": evidence_complete,
        "planned_tasks": planned_tasks,
    }

    if parent_tier == "unknown":
        return DelegationAssessment(DelegationStatus.UNKNOWN, reason="parent-tier-unknown", **base)
    if parent_tier not in {"D", "M"}:
        return DelegationAssessment(DelegationStatus.NOT_APPLICABLE, reason="parent-not-M-or-D", **base)
    if exception != "none":
        return DelegationAssessment(DelegationStatus.EXCEPTION, reason=exception, **base)
    if eligible is False:
        return DelegationAssessment(DelegationStatus.NOT_APPLICABLE, reason="no-eligible-subtask", **base)
    if eligible is None:
        return DelegationAssessment(DelegationStatus.UNKNOWN, reason="eligibility-unknown", **base)
    if evidence_complete is not True:
        reason = "evidence-incomplete" if evidence_complete is False else "evidence-completeness-unknown"
        return DelegationAssessment(DelegationStatus.UNKNOWN, reason=reason, **base)

    parent_actor_ids = {
        run["actor_id"]
        for run in runs
        if run["tier"] == parent_tier and run["role"] in {"planner", "integrator"}
    }
    parent_session = packet.get("session_id")
    if parent_session is not None:
        if not isinstance(parent_session, str) or not _SAFE_ID.fullmatch(parent_session):
            raise DelegationValidationError("session_id invalid")
        parent_actor_ids.add(parent_session)
    attempted: list[str] = []
    completed: list[str] = []
    incomplete_child_receipt = False
    for run in runs:
        if run["role"] != "maker" or run["tier"] not in {"K", "W"}:
            continue
        if run["actor_id"] in parent_actor_ids:
            continue
        if run["verified"] and run["status"] in {"completed", "partial", "failed", "blocked"}:
            attempted.append(run["run_id"])
        identified = all(run[key] != "unknown" for key in ("model_id", "provider", "actor_id", "tier"))
        if run["verified"] and run["status"] == "completed" and identified:
            completed.append(run["run_id"])
        else:
            incomplete_child_receipt = True

    common = {**base, "attempted_child_runs": tuple(attempted)}
    if completed:
        return DelegationAssessment(
            DelegationStatus.DELEGATED,
            completed_child_runs=tuple(completed),
            reason="verified-completed-child-maker",
            **common,
        )
    if incomplete_child_receipt:
        return DelegationAssessment(
            DelegationStatus.UNKNOWN,
            reason="child-attempt-without-verified-completion",
            **common,
        )
    return DelegationAssessment(
        DelegationStatus.UNDERUSED,
        reason="no-verified-completed-child-maker",
        **common,
    )


def _mapping_with_exact_keys(value: object, keys: frozenset[str], name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise DelegationValidationError(f"{name} must be a mapping")
    actual = set(value.keys())
    if actual != keys:
        missing = sorted(keys - actual)
        extra = sorted(str(key) for key in actual - keys)
        raise DelegationValidationError(f"{name} keys mismatch: missing={missing}, extra={extra}")
    if not all(isinstance(key, str) for key in actual):
        raise DelegationValidationError(f"{name} keys must be strings")
    return value


def _enum_value(value: object, allowed: frozenset[str], name: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise DelegationValidationError(f"{name} has an invalid value")
    return value


def _optional_bool(value: object, name: str) -> bool | None:
    if value is None or type(value) is bool:
        return value
    raise DelegationValidationError(f"{name} must be boolean or null")


def _optional_nonnegative_int(value: object, name: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise DelegationValidationError(f"{name} must be a non-negative integer or null")
    return value


def _validate_metrics(metrics: Mapping[str, object]) -> None:
    _optional_number(metrics["wall_seconds"], "metrics.wall_seconds")
    _optional_nonnegative_int(metrics["repair_rounds"], "metrics.repair_rounds")
    _optional_bool(metrics["duplicate_work"], "metrics.duplicate_work")
    if type(metrics["uat_required"]) is not bool:
        raise DelegationValidationError("metrics.uat_required must be boolean")
    _enum_value(metrics["uat_status"], _UAT_STATUSES, "metrics.uat_status")
    _optional_bool(metrics["evidence_complete"], "metrics.evidence_complete")


def _optional_number(value: object, name: str) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DelegationValidationError(f"{name} must be a finite non-negative number or null")
    if not math.isfinite(value) or value < 0:
        raise DelegationValidationError(f"{name} must be a finite non-negative number or null")
    return value


def _validate_runs(value: object) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise DelegationValidationError("runs must be a sequence")
    if len(value) > 256:
        raise DelegationValidationError("runs exceeds the 256-record packet limit")

    validated: list[dict[str, object]] = []
    for index, raw_run in enumerate(value):
        run = _mapping_with_exact_keys(raw_run, _RUN_KEYS, f"runs[{index}]")
        for field in ("run_id", "actor_id", "model_id", "provider"):
            _safe_id(run[field], f"runs[{index}].{field}")
        _enum_value(run["tier"], _TIERS, f"runs[{index}].tier")
        _enum_value(run["role"], _RUN_ROLES, f"runs[{index}].role")
        _enum_value(run["status"], _RUN_STATUSES, f"runs[{index}].status")
        if type(run["verified"]) is not bool:
            raise DelegationValidationError(f"runs[{index}].verified must be boolean")
        for field in ("input_tokens", "cached_input_tokens", "output_tokens", "tool_calls"):
            _optional_nonnegative_int(run[field], f"runs[{index}].{field}")
        validated.append(dict(run))
    return validated


def _safe_id(value: object, name: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise DelegationValidationError(f"{name} must be a bounded safe ASCII ID")
    return value
