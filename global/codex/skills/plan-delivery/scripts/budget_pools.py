#!/usr/bin/env python3
"""Typed owner-ceiling pools for planning/review versus graph execution."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

POOL_PLANNING = "planning_and_review"
POOL_EXECUTION = "execution"
POOLS = (POOL_PLANNING, POOL_EXECUTION)
ROUTING_QUALITY = "quality"
ROUTING_COST_ABOVE_FLOOR = "cost_above_capability_floor"
UNKNOWN_USAGE_POLICY = "preserve_unknown"
SUPPORTED_UNKNOWN_USAGE_POLICIES = {UNKNOWN_USAGE_POLICY, "pessimistic_max_tokens"}
CAPABILITY_FLOOR_SOURCE = "profiles.capabilities"
PLANNING_RUN_KINDS = {
    "fidelity",
    "sufficiency",
    "planning-review",
    "contract-audit",
    "council",
    "model-council",
    "independent-review",
    "repair-review",
    "gate-review",
    "canary-planning",
}
RECOMMENDED_OWNER_SPLIT = {
    "owner_total_tokens": 5000000,
    POOL_PLANNING: 1500000,
    POOL_EXECUTION: 3500000,
    "prior_ceiling_tokens": 4000000,
    "prior_used_tokens": 3915597,
    "raised_ceiling_tokens": 5000000,
    "raised_used_tokens": 4988626,
    "execution_runs": 0,
    "planning_fleet_runs": 9,
}
RECOMMENDED_CAPABILITY_FLOOR = {
    "source": CAPABILITY_FLOOR_SOURCE,
    "minimum_scores": {
        "instruction-following": 88,
        "tool-use": 80,
        "write": 80,
    },
    "minimum_first_pass": 80,
    "minimum_evidence_confidence": 0.5,
}


class BudgetPoolError(ValueError):
    pass


def _require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BudgetPoolError(f"{label} must be an object")
    return value


def _exact_keys(value: Any, required: set[str], label: str, optional: set[str] | None = None) -> dict[str, Any]:
    item = _require_object(value, label)
    missing = sorted(required - set(item))
    extra = sorted(set(item) - required - (optional or set()))
    if missing or extra:
        raise BudgetPoolError(f"{label} keys mismatch: missing={missing}; extra={extra}")
    return item


def _int_value(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not (minimum <= value <= maximum):
        raise BudgetPoolError(f"{label} must be an integer in {minimum}..{maximum}")
    return value


def _ratio_value(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BudgetPoolError(f"{label} must be a number in 0..1")
    number = float(value)
    if not (0.0 <= number <= 1.0):
        raise BudgetPoolError(f"{label} must be a number in 0..1")
    return number


def _parse_utc(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise BudgetPoolError(f"Invalid UTC timestamp: {value}") from exc
    if parsed.tzinfo is None:
        raise BudgetPoolError("Timestamps must include a timezone")
    return parsed.astimezone(timezone.utc)


def example_owner_ceiling(*, planning_and_review: int, execution: int) -> dict[str, Any]:
    return {
        "token_policy": "estimate",
        POOL_PLANNING: planning_and_review,
        POOL_EXECUTION: execution,
        "unknown_usage_policy": UNKNOWN_USAGE_POLICY,
        "quota_snapshot_max_age_hours": 24,
        "routing": {
            POOL_PLANNING: ROUTING_QUALITY,
            POOL_EXECUTION: ROUTING_COST_ABOVE_FLOOR,
        },
        "capability_floor": {
            "source": RECOMMENDED_CAPABILITY_FLOOR["source"],
            "minimum_scores": dict(RECOMMENDED_CAPABILITY_FLOOR["minimum_scores"]),
            "minimum_first_pass": RECOMMENDED_CAPABILITY_FLOOR["minimum_first_pass"],
            "minimum_evidence_confidence": RECOMMENDED_CAPABILITY_FLOOR[
                "minimum_evidence_confidence"
            ],
        },
    }


def validate_owner_ceiling(value: Any) -> dict[str, Any]:
    ceiling = _exact_keys(
        value,
        {
            POOL_PLANNING,
            POOL_EXECUTION,
            "unknown_usage_policy",
            "quota_snapshot_max_age_hours",
            "routing",
            "capability_floor",
        },
        "owner_ceiling",
        {"token_policy"},
    )
    if ceiling.get("token_policy", "hard") not in {"hard", "estimate"}:
        raise BudgetPoolError("owner_ceiling.token_policy must be hard or estimate")
    _int_value(ceiling[POOL_PLANNING], f"owner_ceiling.{POOL_PLANNING}", 0, 100000000)
    _int_value(ceiling[POOL_EXECUTION], f"owner_ceiling.{POOL_EXECUTION}", 1, 100000000)
    if ceiling["unknown_usage_policy"] not in SUPPORTED_UNKNOWN_USAGE_POLICIES:
        raise BudgetPoolError(
            "owner_ceiling.unknown_usage_policy must preserve unknown telemetry; silent zero is forbidden"
        )
    _int_value(
        ceiling["quota_snapshot_max_age_hours"],
        "owner_ceiling.quota_snapshot_max_age_hours",
        1,
        168,
    )
    routing = _exact_keys(
        ceiling["routing"],
        {POOL_PLANNING, POOL_EXECUTION},
        "owner_ceiling.routing",
    )
    if routing[POOL_PLANNING] != ROUTING_QUALITY:
        raise BudgetPoolError("owner_ceiling.routing.planning_and_review must be quality")
    if routing[POOL_EXECUTION] != ROUTING_COST_ABOVE_FLOOR:
        raise BudgetPoolError(
            "owner_ceiling.routing.execution must be cost_above_capability_floor"
        )
    floor = _exact_keys(
        ceiling["capability_floor"],
        {
            "source",
            "minimum_scores",
            "minimum_first_pass",
            "minimum_evidence_confidence",
        },
        "owner_ceiling.capability_floor",
    )
    if floor["source"] != CAPABILITY_FLOOR_SOURCE:
        raise BudgetPoolError(
            "owner_ceiling.capability_floor.source must be profiles.capabilities"
        )
    scores = _exact_keys(
        floor["minimum_scores"],
        {"instruction-following", "tool-use", "write"},
        "owner_ceiling.capability_floor.minimum_scores",
    )
    for name, score in scores.items():
        _int_value(score, f"owner_ceiling.capability_floor.minimum_scores.{name}", 0, 100)
    _int_value(
        floor["minimum_first_pass"],
        "owner_ceiling.capability_floor.minimum_first_pass",
        0,
        100,
    )
    _ratio_value(
        floor["minimum_evidence_confidence"],
        "owner_ceiling.capability_floor.minimum_evidence_confidence",
    )
    return ceiling


def empty_ledger() -> dict[str, Any]:
    return {
        pool: {
            "attested_tokens": 0,
            "estimated_tokens": 0,
            "known_tokens": 0,
            "unknown_runs": 0,
            "runs": [],
        }
        for pool in POOLS
    }


def classify_run_pool(record: dict[str, Any]) -> str:
    explicit = record.get("budget_pool")
    if explicit in POOLS:
        return explicit
    if record.get("graph_node_id"):
        return POOL_EXECUTION
    kind = record.get("kind")
    if kind in PLANNING_RUN_KINDS:
        return POOL_PLANNING
    mode = record.get("mode")
    if mode in {"advisory", "review"}:
        return POOL_PLANNING
    if mode == "implement":
        return POOL_EXECUTION
    raise BudgetPoolError(
        f"Run {record.get('run_id', '<unknown>')} cannot be classified into a budget pool"
    )


def charge_token_usage(
    *,
    total_tokens: int | None,
    token_status: str | None,
    max_tokens: int | None,
    policy: str = UNKNOWN_USAGE_POLICY,
) -> dict[str, Any]:
    if policy not in SUPPORTED_UNKNOWN_USAGE_POLICIES:
        raise BudgetPoolError("Unsupported unknown-usage policy")
    known = (
        token_status == "known"
        and isinstance(total_tokens, int)
        and not isinstance(total_tokens, bool)
    )
    if known:
        if total_tokens < 0:
            raise BudgetPoolError("total_tokens cannot be negative")
        return {
            "attested_tokens": total_tokens,
            "estimated": False,
            "status": "known",
            "source": "adapter-telemetry",
        }
    return {
        "attested_tokens": None,
        "estimated": False,
        "status": "unknown",
        "source": "adapter-telemetry-unknown",
    }


def remaining_tokens(ceiling: dict[str, Any], ledger: dict[str, Any], pool: str) -> int:
    if pool not in POOLS:
        raise BudgetPoolError(f"Unknown budget pool: {pool}")
    return int(ceiling[pool]) - int(ledger[pool]["attested_tokens"])


def authorize_pool_spend(
    ceiling: dict[str, Any],
    ledger: dict[str, Any],
    pool: str,
    requested_tokens: int,
) -> dict[str, Any]:
    if pool not in POOLS:
        raise BudgetPoolError(f"Unknown budget pool: {pool}")
    if (
        isinstance(requested_tokens, bool)
        or not isinstance(requested_tokens, int)
        or requested_tokens < 0
    ):
        raise BudgetPoolError("requested_tokens must be a non-negative integer")
    other = POOL_EXECUTION if pool == POOL_PLANNING else POOL_PLANNING
    other_remaining = remaining_tokens(ceiling, ledger, other)
    pool_remaining = remaining_tokens(ceiling, ledger, pool)
    policy = validate_owner_ceiling(ceiling).get("token_policy", "hard")
    overrun = requested_tokens > pool_remaining
    if overrun and policy == "hard":
        raise BudgetPoolError(
            f"{pool} token pool exhausted: remaining={pool_remaining} requested={requested_tokens}"
        )
    return {
        "status": "AUTHORIZED",
        "pool": pool,
        "remaining_after": pool_remaining - requested_tokens,
        "other_pool": other,
        "other_pool_remaining": other_remaining,
        "borrowed_from_other_pool": False,
        "token_policy": policy,
        "estimate_warning": overrun,
        "additional_estimate_tokens": max(0, requested_tokens - pool_remaining),
        "owner_approval_required": False,
    }


def apply_charge(
    ledger: dict[str, Any],
    record: dict[str, Any],
    charge: dict[str, Any],
) -> dict[str, Any]:
    pool = classify_run_pool(record)
    entry = {
        "run_id": record.get("run_id"),
        "pool": pool,
        "kind": record.get("kind"),
        "mode": record.get("mode"),
        "graph_node_id": record.get("graph_node_id"),
        "attested_tokens": charge["attested_tokens"],
        "estimated": charge["estimated"],
        "charge_status": charge["status"],
        "charge_source": charge["source"],
    }
    bucket = ledger[pool]
    if charge["attested_tokens"] is None:
        bucket["unknown_runs"] += 1
    elif charge["estimated"]:
        bucket["estimated_tokens"] += charge["attested_tokens"]
        bucket["attested_tokens"] += charge["attested_tokens"]
    else:
        bucket["known_tokens"] += charge["attested_tokens"]
        bucket["attested_tokens"] += charge["attested_tokens"]
    bucket["runs"].append(entry)
    return entry


def reconcile_runs(
    ceiling: dict[str, Any],
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    ceiling = validate_owner_ceiling(ceiling)
    ledger = empty_ledger()
    assignments: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            raise BudgetPoolError("Each run record must be an object")
        usage = record.get("token_usage") if isinstance(record.get("token_usage"), dict) else {}
        charge = charge_token_usage(
            total_tokens=usage.get("total_tokens"),
            token_status=usage.get("status"),
            max_tokens=record.get("max_tokens"),
            policy=ceiling["unknown_usage_policy"],
        )
        assignments.append(apply_charge(ledger, record, charge))
    return {
        "schema_version": 1,
        "generated_from": "run-records",
        "token_policy": ceiling.get("token_policy", "hard"),
        "owner_ceiling": {
            POOL_PLANNING: ceiling[POOL_PLANNING],
            POOL_EXECUTION: ceiling[POOL_EXECUTION],
        },
        "pools": {
            pool: {
                "ceiling": ceiling[pool],
                "attested_tokens": ledger[pool]["attested_tokens"],
                "known_tokens": ledger[pool]["known_tokens"],
                "estimated_tokens": ledger[pool]["estimated_tokens"],
                "unknown_runs": ledger[pool]["unknown_runs"],
                "remaining_tokens": remaining_tokens(ceiling, ledger, pool),
                "run_count": len(ledger[pool]["runs"]),
            }
            for pool in POOLS
        },
        "assignments": assignments,
        "isolation": {
            "borrow_forbidden": True,
            "planning_blocks_execution": False,
            "execution_blocks_planning": False,
        },
    }


def render_reconciliation_markdown(report: dict[str, Any]) -> str:
    planning = report["pools"][POOL_PLANNING]
    execution = report["pools"][POOL_EXECUTION]
    lines = [
        "# Budget reconciliation",
        "",
        "Generated from run records. This file is not an owner declaration.",
        "",
        "| Pool | Ceiling | Attested | Known | Estimated | Remaining | Runs |",
        "|---|---:|---:|---:|---:|---:|---:|",
        (
            f"| `{POOL_PLANNING}` | {planning['ceiling']} | {planning['attested_tokens']} | "
            f"{planning['known_tokens']} | {planning['estimated_tokens']} | "
            f"{planning['remaining_tokens']} | {planning['run_count']} |"
        ),
        (
            f"| `{POOL_EXECUTION}` | {execution['ceiling']} | {execution['attested_tokens']} | "
            f"{execution['known_tokens']} | {execution['estimated_tokens']} | "
            f"{execution['remaining_tokens']} | {execution['run_count']} |"
        ),
        "",
        "Pools cannot borrow. Exhausted planning does not block approved execution. "
        + ("Estimates warn without an owner approval request; no ceiling was raised."
           if report.get("token_policy") == "estimate" else "Legacy/owner ceilings remain hard."),
        "",
        "| Run | Pool | Kind | Attested | Charge |",
        "|---|---|---|---:|---|",
    ]
    for item in report["assignments"]:
        lines.append(
            f"| `{item['run_id']}` | `{item['pool']}` | {item.get('kind') or ''} | "
            f"{item['attested_tokens']} | {item['charge_status']} |"
        )
    lines.append("")
    return "\n".join(lines)


def profile_meets_capability_floor(profile: dict[str, Any], floor: dict[str, Any]) -> bool:
    capabilities = profile.get("capabilities")
    if not isinstance(capabilities, dict):
        return False
    for name, minimum in floor["minimum_scores"].items():
        score = capabilities.get(name)
        if not isinstance(score, int) or isinstance(score, bool) or score < int(minimum):
            return False
    priors = profile.get("efficiency_priors")
    if not isinstance(priors, dict):
        return False
    first_pass = priors.get("first_pass")
    if not isinstance(first_pass, int) or isinstance(first_pass, bool):
        return False
    if first_pass < int(floor["minimum_first_pass"]):
        return False
    confidence = profile.get("evidence_confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        return False
    if float(confidence) < float(floor["minimum_evidence_confidence"]):
        return False
    return True


def routing_policy_for_pool(ceiling: dict[str, Any], pool: str) -> str:
    ceiling = validate_owner_ceiling(ceiling)
    if pool not in POOLS:
        raise BudgetPoolError(f"Unknown budget pool: {pool}")
    return ceiling["routing"][pool]


def _profile_cost(profile: dict[str, Any]) -> float:
    for key in ("provider_cost_pln", "cost_pln"):
        value = profile.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    if profile.get("premium") is True:
        return 10000.0
    return 1000.0


def _capability_fit(profile: dict[str, Any]) -> int:
    capabilities = profile.get("capabilities")
    if not isinstance(capabilities, dict):
        return 0
    return sum(
        int(value)
        for value in capabilities.values()
        if isinstance(value, int) and not isinstance(value, bool)
    )


def select_profile(
    profiles: list[dict[str, Any]],
    ceiling: dict[str, Any],
    pool: str,
) -> dict[str, Any]:
    ceiling = validate_owner_ceiling(ceiling)
    policy = routing_policy_for_pool(ceiling, pool)
    floor = ceiling["capability_floor"]
    eligible = [item for item in profiles if profile_meets_capability_floor(item, floor)]
    if not eligible:
        raise BudgetPoolError("No execution profile remains above the capability floor")
    if policy == ROUTING_QUALITY:
        return max(eligible, key=_capability_fit)
    return min(eligible, key=lambda item: (_profile_cost(item), -_capability_fit(item)))


def evaluate_quota_snapshot(
    snapshot: dict[str, Any] | None,
    *,
    now: datetime,
    max_age_hours: int,
    collector_mode: str | None,
) -> dict[str, Any]:
    current = now.astimezone(timezone.utc)
    if not isinstance(snapshot, dict) or snapshot.get("observed_at") in {None, ""}:
        return {
            "fresh": False,
            "stale": True,
            "unknown": True,
            "routing_signal": "stale-not-neutral",
            "should_refresh_before_routing": True,
            "refresh_action": _refresh_action(collector_mode),
            "age_hours": None,
            "attestable": False,
        }
    observed = _parse_utc(str(snapshot["observed_at"]))
    age_hours = (current - observed).total_seconds() / 3600.0
    stale = age_hours > float(max_age_hours)
    unknown = snapshot.get("status") == "unknown"
    if unknown or stale:
        return {
            "fresh": False,
            "stale": stale,
            "unknown": unknown,
            "routing_signal": "stale-not-neutral",
            "should_refresh_before_routing": True,
            "refresh_action": _refresh_action(collector_mode),
            "age_hours": round(age_hours, 3),
            "attestable": False,
        }
    return {
        "fresh": True,
        "stale": False,
        "unknown": False,
        "routing_signal": "fresh",
        "should_refresh_before_routing": False,
        "refresh_action": "use-current",
        "age_hours": round(age_hours, 3),
        "attestable": True,
    }


def _refresh_action(collector_mode: str | None) -> str:
    if collector_mode == "local-token-ledger":
        return "refresh-local-token-ledger"
    if collector_mode == "tui-assisted":
        return "mark-stale-provider-console-required"
    return "mark-stale-manual-refresh-required"
