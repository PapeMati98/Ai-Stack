"""Cheap work-shape triage. This selects process weight, never grants authority."""
import argparse
import json
import math
from pathlib import Path


def decide(value):
    required = {"scope", "risk", "context_ready", "protected_change",
                "independent_review", "specialist_gain", "parallel_slices", "economics"}
    optional = {"parallel_disjoint"}
    if not isinstance(value, dict) or not required <= set(value) or set(value) - required - optional:
        raise ValueError("Policy requires exactly: " + ", ".join(sorted(required)))
    if value["scope"] not in {"bounded", "coordinated"}:
        raise ValueError("scope must be bounded or coordinated")
    if value["risk"] not in {"low", "medium", "high", "critical", "unknown"}:
        raise ValueError("risk is invalid")
    for field in ("context_ready", "protected_change", "independent_review", "specialist_gain"):
        if not isinstance(value[field], bool):
            raise ValueError(field + " must be boolean")
    if "parallel_disjoint" in value and not isinstance(value["parallel_disjoint"], bool):
        raise ValueError("parallel_disjoint must be boolean")
    count = value["parallel_slices"]
    if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 64:
        raise ValueError("parallel_slices must be an integer from 0 to 64")
    economics = value["economics"]
    gain = None
    if economics is not None:
        fields = {"local_minutes", "delegated_minutes", "coordination_minutes"}
        if not isinstance(economics, dict) or set(economics) != fields:
            raise ValueError("economics requires local, delegated and coordination minutes")
        if any(isinstance(n, bool) or not isinstance(n, (float, int)) or
               not math.isfinite(n) or n < 0 for n in economics.values()):
            raise ValueError("economics values must be finite nonnegative numbers")
        # Delegated time is the complete critical path, including tests/review/rework.
        gain = round(economics["local_minutes"] - economics["delegated_minutes"] -
                     economics["coordination_minutes"], 3)
    reasons = []
    review = value["independent_review"]
    full = value["protected_change"] or value["risk"] in {"high", "critical"}
    if full:
        reasons.append("PROTECTED_OR_HIGH_RISK")
    if value["scope"] == "coordinated":
        full = True
        reasons.append("COORDINATED_DELIVERY")
    if not value["context_ready"] or value["risk"] == "unknown":
        return {"action": "delegation-policy", "policy_version": 2,
                "lane": "full" if full else "compact-recon", "delegation_mode": "DEFER",
                "reason_codes": reasons + ["LOCAL_CONTEXT_RECON_REQUIRED"],
                "estimated_minutes_saved": gain, "next_action": "bounded-read-only-recon",
                "review_required": review or full, "authorization_granted": False,
                "writes_performed": 0, "provider_calls": 0}
    benefit = value["specialist_gain"] or review or (gain is not None and gain > 0)
    if value["specialist_gain"]:
        reasons.append("SPECIALIST_QUALITY_GAIN")
    if review:
        reasons.append("INDEPENDENT_REVIEW_NEEDED")
    if gain is not None and gain > 0:
        reasons.append("POSITIVE_DELIVERY_TIME_GAIN")
    if not benefit:
        reasons.append("NO_ESTABLISHED_DELEGATION_GAIN")
    if benefit and count >= 2:
        reasons.append("INDEPENDENT_PARALLEL_SLICES" if value.get("parallel_disjoint", False)
                       else "PARALLEL_DISJOINTNESS_UNCONFIRMED_USE_SERIAL")
    # Process weight follows the work's risk/scope, not the number of workers.
    lane = "full" if full else ("compact" if value["risk"] == "medium" else "direct")
    return {"action": "delegation-policy", "policy_version": 2,
            "lane": lane,
            "planning_contract_required": lane != "direct",
            "product_acceptance_required": True,
            "parent_acceptance_required": bool(benefit),
            "delegation_mode": "ONE" if benefit else "SKIP",
            "parallel_recommended": benefit and count >= 2 and value.get("parallel_disjoint", False),
            "delegation_value": 50 if benefit else 0,
            "reason_codes": reasons, "estimated_minutes_saved": gain,
            "next_action": "existing-plan-and-safety-gates" if lane != "direct" else
                           ("bounded-worker-and-review" if benefit else "local-work-and-proportionate-tests"),
            "review_required": review or full, "authorization_granted": False,
            "writes_performed": 0, "provider_calls": 0}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope-file", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(decide(json.loads(args.scope_file.read_text(encoding="utf-8-sig")))))


if __name__ == "__main__":
    main()
