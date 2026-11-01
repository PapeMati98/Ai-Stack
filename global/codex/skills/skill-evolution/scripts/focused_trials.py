#!/usr/bin/env python3
"""focused_trials.py - Bounded focused and blind trial control plane for skill evolution.

Features:
1. Anonymous A/B labels: variants are labeled anonymously (variant_a / variant_b).
2. Sealed mapping: variant-to-identity mapping is sealed and stored separately from evaluation packets.
3. Four focused case types: persistence, topic-switch, stop, comprehension.
4. Independent evaluator identity: evaluator must differ in actor_id and provider_lineage from proposers.
5. No per-agent permanent style override: evaluations focus strictly on functional capabilities.
6. Critical correctness/security veto: critical failure vetoes a candidate despite higher mean score.
7. Telemetry robustness: missing costs do not automatically veto.
8. Deterministic pairing: reproducible variant assignment based on trial ID and condition hash.
"""
from __future__ import annotations

import sys
sys.dont_write_bytecode = True

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Set, Tuple


VERSION = "0.2.0"
SCHEMA_VERSION = 1

CASE_TYPES = frozenset({
    "persistence",
    "topic-switch",
    "stop",
    "comprehension",
})

ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,99}$")
SHA_RE = re.compile(r"^[a-f0-9]{64}$")

# Opt-in outcome evaluation for coding/skill-revision tasks. Three explicit
# signals per anonymous variant; see references/outcome-evidence.md.
OUTCOME_SIGNALS = ("discovery", "action", "correctness")
# Evidence is opaque metadata only: bounded strings/hashes, never executed.
# Prompt/code/transcript/diff material is rejected, not stored.
OUTCOME_FORBIDDEN_KEYS = frozenset({
    "prompt", "code", "transcript", "diff", "patch", "command",
    "script", "executable", "secret",
})
OUTCOME_DETAIL_MAX = 500
OUTCOME_REF_MAX = 200


class FocusedTrialError(Exception):
    """Fail-closed error for focused trial validation."""
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def canonical_bytes(value: Any) -> bytes:
    text = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return text.encode("utf-8")


def sha256_value(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _actor(value: Any, label: str) -> Dict[str, str]:
    if not isinstance(value, dict):
        raise FocusedTrialError("schema-type", f"{label} must be an object")
    for key in ("actor_id", "provider_lineage"):
        if key not in value or not isinstance(value[key], str) or not ID_RE.fullmatch(value[key]):
            raise FocusedTrialError("schema-value", f"{label}.{key} has an invalid identifier")
    return {
        "actor_id": value["actor_id"],
        "provider_lineage": value["provider_lineage"],
    }


def _validate_outcome_value(value: Any, label: str) -> Any:
    """Validate one outcome signal value: exactly True, False, or "unknown".

    Rejects booleans-in-disguise (0/1), truthy strings ("true"/"yes"),
    None, NaN/inf floats, and every other non-conforming type.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value == "unknown":
        return value
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        raise FocusedTrialError("invalid-outcome-value", f"{label} is NaN or infinite")
    raise FocusedTrialError(
        "invalid-outcome-value",
        f"{label} must be exactly true, false, or \"unknown\" (got: {type(value).__name__})",
    )


def _validate_outcome_signal(signal: Any, label: str) -> Dict[str, Any]:
    """Validate one outcome signal object with bounded opaque metadata."""
    if not isinstance(signal, dict):
        raise FocusedTrialError("schema-type", f"{label} must be an object")
    if "value" not in signal:
        raise FocusedTrialError("schema-value", f"{label} is missing required 'value'")
    for forbidden in OUTCOME_FORBIDDEN_KEYS:
        if forbidden in signal:
            raise FocusedTrialError(
                "outcome-forbidden-content",
                f"{label} must not carry '{forbidden}': evidence is metadata/hash only",
            )
    validated: Dict[str, Any] = {"value": _validate_outcome_value(signal["value"], f"{label}.value")}
    if "detail" in signal:
        detail = signal["detail"]
        if not isinstance(detail, str) or len(detail) > OUTCOME_DETAIL_MAX:
            raise FocusedTrialError(
                "schema-value",
                f"{label}.detail must be a string of at most {OUTCOME_DETAIL_MAX} chars",
            )
        validated["detail"] = detail
    if "evidence_ref" in signal:
        ref = signal["evidence_ref"]
        if not isinstance(ref, str) or len(ref) == 0 or len(ref) > OUTCOME_REF_MAX:
            raise FocusedTrialError(
                "schema-value",
                f"{label}.evidence_ref must be a non-empty string of at most {OUTCOME_REF_MAX} chars",
            )
        validated["evidence_ref"] = ref
    # Evidence is opaque: stored, never executed or interpreted.
    return validated


def _validate_outcome_observation(
    observation: Any,
    label: str,
    expected_variant: str,
    packet_condition: str,
) -> Dict[str, Any]:
    """Validate one per-variant outcome observation.

    Binds the observation to the anonymous variant label, the trial
    condition hash, and the current tested artifact hash. A naked boolean
    without this binding is insufficient and fails closed here.
    """
    if not isinstance(observation, dict):
        raise FocusedTrialError("schema-type", f"{label} must be an object")
    for forbidden in OUTCOME_FORBIDDEN_KEYS:
        if forbidden in observation:
            raise FocusedTrialError(
                "outcome-forbidden-content",
                f"{label} must not carry '{forbidden}': evidence is metadata/hash only",
            )
    variant = observation.get("variant")
    if variant is not None and variant != expected_variant:
        raise FocusedTrialError(
            "outcome-variant-mismatch",
            f"{label}.variant '{variant}' does not match enclosing {expected_variant}",
        )
    condition = observation.get("condition_sha256")
    if condition is not None:
        if not isinstance(condition, str) or not SHA_RE.fullmatch(condition):
            raise FocusedTrialError("schema-value", f"{label}.condition_sha256 must be a lowercase 64-char hex SHA-256")
        if condition != packet_condition:
            raise FocusedTrialError(
                "condition-mismatch",
                f"{label}.condition_sha256 does not match the trial condition",
            )
    artifact = observation.get("artifact_sha256")
    if artifact is not None:
        if not isinstance(artifact, str) or not SHA_RE.fullmatch(artifact):
            raise FocusedTrialError("schema-value", f"{label}.artifact_sha256 must be a lowercase 64-char hex SHA-256")
    validated: Dict[str, Any] = {}
    if variant is not None:
        validated["variant"] = expected_variant
    if condition is not None:
        validated["condition_sha256"] = condition
    if artifact is not None:
        validated["artifact_sha256"] = artifact
    for signal_name in OUTCOME_SIGNALS:
        if signal_name in observation:
            validated[signal_name] = _validate_outcome_signal(
                observation[signal_name], f"{label}.{signal_name}"
            )
    return validated


def validate_trial_spec(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Validate input trial specification."""
    if not isinstance(spec, dict):
        raise FocusedTrialError("schema-type", "Trial spec must be an object")

    trial_id = spec.get("trial_id")
    if not isinstance(trial_id, str) or not ID_RE.fullmatch(trial_id):
        raise FocusedTrialError("schema-value", f"Invalid trial_id: {trial_id}")

    target_skill = spec.get("target_skill")
    if not isinstance(target_skill, str) or not ID_RE.fullmatch(target_skill):
        raise FocusedTrialError("schema-value", f"Invalid target_skill: {target_skill}")

    baseline_proposer = _actor(spec.get("baseline_proposer"), "baseline_proposer")
    candidate_proposer = _actor(spec.get("candidate_proposer"), "candidate_proposer")

    condition_sha = spec.get("condition_sha256")
    if not isinstance(condition_sha, str) or not SHA_RE.fullmatch(condition_sha):
        raise FocusedTrialError("schema-value", "condition_sha256 must be a lowercase 64-char hex SHA-256")

    raw_cases = spec.get("cases")
    if not isinstance(raw_cases, list) or len(raw_cases) == 0 or len(raw_cases) > 64:
        raise FocusedTrialError("schema-value", "cases must be a non-empty list of at most 64 items")

    validated_cases: List[Dict[str, Any]] = []
    seen_ids: Set[str] = set()
    seen_types: Set[str] = set()
    outcome_trial = spec.get("outcome_trial", False)
    if type(outcome_trial) is not bool:
        raise FocusedTrialError("schema-type", "outcome_trial must be boolean when present")

    for idx, c in enumerate(raw_cases):
        if not isinstance(c, dict):
            raise FocusedTrialError("schema-type", f"Case {idx} must be an object")
        cid = c.get("case_id")
        if not isinstance(cid, str) or not ID_RE.fullmatch(cid):
            raise FocusedTrialError("schema-value", f"Invalid case_id at index {idx}: {cid}")
        if cid in seen_ids:
            raise FocusedTrialError("duplicate-case", f"Duplicate case_id: {cid}")
        seen_ids.add(cid)

        ctype = c.get("case_type")
        if ctype not in CASE_TYPES:
            raise FocusedTrialError("invalid-case-type", f"case_type '{ctype}' must be one of {sorted(CASE_TYPES)}")
        seen_types.add(ctype)

        prompt = c.get("prompt")
        expected_behavior = c.get("expected_behavior")
        baseline_ans = c.get("baseline_answer")
        candidate_ans = c.get("candidate_answer")

        if not all(isinstance(f, str) and len(f.strip()) > 0 for f in (prompt, expected_behavior, baseline_ans, candidate_ans)):
            raise FocusedTrialError("schema-value", f"Case {cid} has empty prompt/expected/answer fields")

        validated_cases.append({
            "case_id": cid,
            "case_type": ctype,
            "prompt": prompt,
            "expected_behavior": expected_behavior,
            "baseline_answer": baseline_ans,
            "candidate_answer": candidate_ans,
        })
        if outcome_trial:
            for kind in ("baseline", "candidate"):
                key = f"{kind}_artifact_sha256"
                value = c.get(key)
                if not isinstance(value, str) or not SHA_RE.fullmatch(value):
                    raise FocusedTrialError("schema-value", f"Case {cid}.{key} must freeze a lowercase SHA-256 before evaluation")
                validated_cases[-1][key] = value

    return {
        "schema_version": SCHEMA_VERSION,
        "trial_id": trial_id,
        "target_skill": target_skill,
        "baseline_proposer": baseline_proposer,
        "candidate_proposer": candidate_proposer,
        "condition_sha256": condition_sha,
        "cases": validated_cases,
        **({"outcome_trial": True} if outcome_trial else {}),
    }


def create_blind_packet(trial_spec: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Generate anonymous blind evaluation packet and separate sealed mapping.

    Deterministic pairing assigns variant_a and variant_b reproducibly using condition hash
    and case ID, ensuring blindness without leaking identity.
    """
    spec = validate_trial_spec(trial_spec)
    trial_id = spec["trial_id"]
    condition_sha = spec["condition_sha256"]

    blind_cases: List[Dict[str, Any]] = []
    sealed_mapping_cases: Dict[str, Dict[str, str]] = {}
    sealed_artifact_hashes: Dict[str, Dict[str, str]] = {}

    for c in spec["cases"]:
        cid = c["case_id"]
        # Deterministic pairing hash
        pair_digest = hashlib.sha256(f"{trial_id}:{condition_sha}:{cid}".encode("utf-8")).digest()
        is_variant_a_baseline = (pair_digest[0] % 2 == 0)

        if is_variant_a_baseline:
            variant_a_text = c["baseline_answer"]
            variant_b_text = c["candidate_answer"]
            sealed_mapping_cases[cid] = {
                "variant_a": "baseline",
                "variant_b": "candidate",
            }
        else:
            variant_a_text = c["candidate_answer"]
            variant_b_text = c["baseline_answer"]
            sealed_mapping_cases[cid] = {
                "variant_a": "candidate",
                "variant_b": "baseline",
            }

        # Blind case contains ONLY anonymous variant_a and variant_b
        blind_cases.append({
            "case_id": cid,
            "case_type": c["case_type"],
            "prompt": c["prompt"],
            "expected_behavior": c["expected_behavior"],
            "variant_a_answer": variant_a_text,
            "variant_b_answer": variant_b_text,
        })
        if spec.get("outcome_trial"):
            expected = {kind: c[f"{kind}_artifact_sha256"] for kind in ("baseline", "candidate")}
            sealed_artifact_hashes[cid] = expected
            blind_cases[-1]["artifact_sha256"] = {
                variant: expected[kind] for variant, kind in sealed_mapping_cases[cid].items()
            }

    blind_packet = {
        "schema_version": SCHEMA_VERSION,
        "trial_id": trial_id,
        "target_skill": spec["target_skill"],
        "condition_sha256": condition_sha,
        "blind": True,
        "cases": blind_cases,
    }

    sealed_mapping = {
        "schema_version": SCHEMA_VERSION,
        "trial_id": trial_id,
        "condition_sha256": condition_sha,
        "baseline_proposer": spec["baseline_proposer"],
        "candidate_proposer": spec["candidate_proposer"],
        "case_mappings": sealed_mapping_cases,
    }

    if spec.get("outcome_trial"):
        blind_packet["outcome_trial"] = True
        sealed_mapping["outcome_trial"] = True
        sealed_mapping["artifact_hashes"] = sealed_artifact_hashes

    return blind_packet, sealed_mapping


def validate_sealed_mapping(
    sealed_mapping: Dict[str, Any],
    blind_packet: Dict[str, Any],
) -> Dict[str, Any]:
    """Validate sealed mapping and strictly bind it to the blind packet.

    Enforces:
    1. trial_id must match blind_packet.trial_id.
    2. condition_sha256 must match blind_packet.condition_sha256.
    3. baseline_proposer and candidate_proposer must be valid actor objects.
    4. case_mappings must be an object with valid mappings for all cases in blind_packet.
    """
    if not isinstance(sealed_mapping, dict):
        raise FocusedTrialError("schema-type", "Sealed mapping must be an object")

    sealed_trial_id = sealed_mapping.get("trial_id")
    packet_trial_id = blind_packet.get("trial_id")
    if not isinstance(sealed_trial_id, str) or sealed_trial_id != packet_trial_id:
        raise FocusedTrialError(
            "trial-mismatch",
            f"Sealed mapping trial_id '{sealed_trial_id}' does not match blind packet trial_id '{packet_trial_id}'",
        )

    sealed_cond = sealed_mapping.get("condition_sha256")
    packet_cond = blind_packet.get("condition_sha256")
    if not isinstance(sealed_cond, str) or sealed_cond != packet_cond:
        raise FocusedTrialError(
            "condition-mismatch",
            f"Sealed mapping condition_sha256 '{sealed_cond}' does not match blind packet condition_sha256 '{packet_cond}'",
        )

    base_prop = _actor(sealed_mapping.get("baseline_proposer"), "sealed_mapping.baseline_proposer")
    cand_prop = _actor(sealed_mapping.get("candidate_proposer"), "sealed_mapping.candidate_proposer")

    raw_mappings = sealed_mapping.get("case_mappings")
    if not isinstance(raw_mappings, dict):
        raise FocusedTrialError("schema-type", "Sealed mapping case_mappings must be an object")

    validated_mappings: Dict[str, Dict[str, str]] = {}
    planned_cases = blind_packet.get("cases", [])
    if not isinstance(planned_cases, list) or len(planned_cases) == 0:
        raise FocusedTrialError("schema-value", "Blind packet cases must be a non-empty list")

    for c in planned_cases:
        if not isinstance(c, dict):
            raise FocusedTrialError("schema-type", "Blind packet case must be an object")
        cid = c.get("case_id")
        if not isinstance(cid, str):
            raise FocusedTrialError("schema-value", "Blind packet case_id must be a string")
        if cid not in raw_mappings:
            raise FocusedTrialError("missing-case-mapping", f"Sealed mapping missing case mapping for '{cid}'")

        m = raw_mappings[cid]
        if not isinstance(m, dict):
            raise FocusedTrialError("schema-type", f"Case mapping for '{cid}' must be an object")
        va = m.get("variant_a")
        vb = m.get("variant_b")
        if {va, vb} != {"baseline", "candidate"}:
            raise FocusedTrialError(
                "invalid-case-mapping",
                f"Case mapping for '{cid}' must assign variant_a and variant_b to 'baseline' and 'candidate' (got: {m})",
            )
        validated_mappings[cid] = {"variant_a": str(va), "variant_b": str(vb)}

    packet_flag = blind_packet.get("outcome_trial", False)
    sealed_flag = sealed_mapping.get("outcome_trial", False)
    if type(packet_flag) is not bool or type(sealed_flag) is not bool or packet_flag != sealed_flag:
        raise FocusedTrialError("condition-mismatch", "Outcome declaration differs between blind packet and sealed mapping")
    validated_artifacts: Dict[str, Dict[str, str]] = {}
    if packet_flag:
        frozen = sealed_mapping.get("artifact_hashes")
        if not isinstance(frozen, dict):
            raise FocusedTrialError("artifact-binding-mismatch", "Outcome trial is missing frozen artifact hashes")
        for c in planned_cases:
            cid = c["case_id"]
            actual = frozen.get(cid)
            anonymous = c.get("artifact_sha256")
            if (not isinstance(actual, dict) or not isinstance(anonymous, dict)
                    or set(actual) != {"baseline", "candidate"}
                    or set(anonymous) != {"variant_a", "variant_b"}
                    or any(not isinstance(v, str) or not SHA_RE.fullmatch(v) for v in actual.values())
                    or any(anonymous[v] != actual[kind] for v, kind in validated_mappings[cid].items())):
                raise FocusedTrialError("artifact-binding-mismatch", f"Frozen artifact hashes differ for case '{cid}'")
            validated_artifacts[cid] = dict(actual)
    return {
        "schema_version": sealed_mapping.get("schema_version", SCHEMA_VERSION),
        "trial_id": sealed_trial_id,
        "condition_sha256": sealed_cond,
        "baseline_proposer": base_prop,
        "candidate_proposer": cand_prop,
        "case_mappings": validated_mappings,
        **({"outcome_trial": True, "artifact_hashes": validated_artifacts} if packet_flag else {}),
    }


def validate_evaluation_submission(
    submission: Dict[str, Any],
    blind_packet: Dict[str, Any],
    sealed_mapping: Dict[str, Any],
) -> Dict[str, Any]:
    """Validate evaluation submission and enforce independent evaluator lineage."""
    if not isinstance(submission, dict):
        raise FocusedTrialError("schema-type", "Evaluation submission must be an object")

    if submission.get("trial_id") != blind_packet["trial_id"]:
        raise FocusedTrialError("trial-mismatch", "Submission trial_id does not match blind packet")

    sub_cond = submission.get("condition_sha256")
    if sub_cond is not None and sub_cond != blind_packet.get("condition_sha256"):
        raise FocusedTrialError("condition-mismatch", "Submission condition_sha256 does not match blind packet")

    evaluator = _actor(submission.get("evaluator"), "evaluator")
    base_prop = sealed_mapping["baseline_proposer"]
    cand_prop = sealed_mapping["candidate_proposer"]

    # Enforce independent lineage
    for label, prop in (("baseline", base_prop), ("candidate", cand_prop)):
        if evaluator["actor_id"] == prop["actor_id"] or evaluator["provider_lineage"] == prop["provider_lineage"]:
            raise FocusedTrialError(
                "self-review",
                f"Evaluator must be independent of {label} proposer in both actor_id and provider_lineage "
                f"(evaluator: {evaluator}, {label}: {prop})",
            )

    eval_cases = submission.get("case_evaluations")
    planned_cases = blind_packet.get("cases", [])
    if not isinstance(eval_cases, list) or len(eval_cases) != len(planned_cases):
        raise FocusedTrialError("incomplete-evaluation", "Evaluation must cover all planned trial cases")

    planned_ids = {c["case_id"] for c in planned_cases}
    indexed_evals: Dict[str, Dict[str, Any]] = {}

    for item in eval_cases:
        if not isinstance(item, dict):
            raise FocusedTrialError("schema-type", "Case evaluation must be an object")
        cid = item.get("case_id")
        if cid not in planned_ids:
            raise FocusedTrialError("unknown-case", f"Evaluation contains unknown case_id: {cid}")
        if cid in indexed_evals:
            raise FocusedTrialError("duplicate-case", f"Duplicate case evaluation for: {cid}")

        for var_key in ("variant_a", "variant_b"):
            v_eval = item.get(var_key)
            if not isinstance(v_eval, dict):
                raise FocusedTrialError("schema-type", f"Case {cid} lacks {var_key} evaluation object")
            score = v_eval.get("score")
            if not isinstance(score, (int, float)) or isinstance(score, bool) or score < 0 or score > 10:
                raise FocusedTrialError("invalid-score", f"Case {cid} {var_key} score must be between 0 and 10")
            if isinstance(score, float) and (math.isnan(score) or math.isinf(score)):
                raise FocusedTrialError("invalid-score", f"Case {cid} {var_key} score must not be NaN or infinite")
            crit = v_eval.get("critical_failure", False)
            if not isinstance(crit, bool):
                raise FocusedTrialError("schema-type", f"Case {cid} {var_key} critical_failure must be boolean")
            if "outcome" in v_eval:
                v_eval = dict(v_eval)
                v_eval["outcome"] = _validate_outcome_observation(
                    v_eval["outcome"],
                    f"Case {cid} {var_key}.outcome",
                    var_key,
                    str(blind_packet.get("condition_sha256")),
                )
                item[var_key] = v_eval

        indexed_evals[cid] = item

    outcome_flag = submission.get("outcome_trial", False)
    if not isinstance(outcome_flag, bool):
        raise FocusedTrialError("schema-type", "outcome_trial must be boolean when present")
    packet_flag = blind_packet.get("outcome_trial", False)
    sealed_flag = sealed_mapping.get("outcome_trial", False)
    if type(packet_flag) is not bool or type(sealed_flag) is not bool or packet_flag != sealed_flag:
        raise FocusedTrialError("condition-mismatch", "Outcome declaration differs between blind packet and sealed mapping")
    outcome_declared = packet_flag or bool(outcome_flag) or any(
        "outcome" in (eval_item.get("variant_a", {}) or {})
        or "outcome" in (eval_item.get("variant_b", {}) or {})
        for eval_item in indexed_evals.values()
    )

    return {
        "trial_id": submission["trial_id"],
        "evaluator": evaluator,
        "indexed_evaluations": indexed_evals,
        "outcome_trial": outcome_declared,
    }


def resolve_trial(
    blind_packet: Dict[str, Any],
    sealed_mapping: Dict[str, Any],
    evaluation_submission: Dict[str, Any],
) -> Dict[str, Any]:
    """Resolve blind trial by unsealing mappings, computing scores, and applying critical veto."""
    if not isinstance(blind_packet, dict):
        raise FocusedTrialError("schema-type", "Blind packet must be an object")
    if "trial_id" not in blind_packet or "condition_sha256" not in blind_packet:
        raise FocusedTrialError("schema-value", "Blind packet missing trial_id or condition_sha256")

    validated_sealed = validate_sealed_mapping(sealed_mapping, blind_packet)
    validated_eval = validate_evaluation_submission(evaluation_submission, blind_packet, validated_sealed)

    evaluator = validated_eval["evaluator"]
    indexed_evals = validated_eval["indexed_evaluations"]
    case_mappings = validated_sealed["case_mappings"]
    outcome_trial = bool(validated_eval.get("outcome_trial", False))

    candidate_scores: List[float] = []
    baseline_scores: List[float] = []
    candidate_critical_failures: List[Dict[str, Any]] = []
    baseline_critical_failures: List[Dict[str, Any]] = []
    case_breakdown: List[Dict[str, Any]] = []
    outcome_artifact_hashes: Dict[str, Optional[str]] = {}
    outcome_artifact_bindings: Dict[str, Dict[str, Optional[str]]] = {}
    outcome_incomplete_cases: List[str] = []
    outcome_false_action_cases: List[str] = []
    outcome_false_correctness_cases: List[str] = []
    outcome_false_discovery_cases: List[str] = []

    for c in blind_packet["cases"]:
        cid = c["case_id"]
        ctype = c["case_type"]
        if cid not in case_mappings:
            raise FocusedTrialError("missing-case-mapping", f"Sealed mapping missing case mapping for '{cid}'")
        mapping = case_mappings[cid]
        if cid not in indexed_evals:
            raise FocusedTrialError("incomplete-evaluation", f"Missing evaluation for case '{cid}'")
        eval_item = indexed_evals[cid]

        # Map anonymous scores to actual entities
        if mapping["variant_a"] == "candidate":
            cand_eval = eval_item["variant_a"]
            base_eval = eval_item["variant_b"]
        else:
            cand_eval = eval_item["variant_b"]
            base_eval = eval_item["variant_a"]

        cand_score = float(cand_eval["score"])
        base_score = float(base_eval["score"])
        candidate_scores.append(cand_score)
        baseline_scores.append(base_score)

        cand_crit = cand_eval.get("critical_failure", False)
        base_crit = base_eval.get("critical_failure", False)

        if cand_crit:
            candidate_critical_failures.append({
                "case_id": cid,
                "case_type": ctype,
                "notes": cand_eval.get("notes", "Critical correctness or security failure"),
            })
        if base_crit:
            baseline_critical_failures.append({
                "case_id": cid,
                "case_type": ctype,
                "notes": base_eval.get("notes", "Critical failure in baseline"),
            })

        # Telemetry: cost telemetry is parsed if present, but never vetos if missing
        cost_telemetry = eval_item.get("cost_telemetry")

        # Opt-in outcome gate: the candidate observation must be complete and
        # bound to this variant, condition, and artifact. Partial telemetry
        # (missing outcome, missing artifact hash, or unknown action /
        # correctness) is recorded as incomplete, never assumed.
        cand_outcome = cand_eval.get("outcome")
        cand_outcome_summary: Optional[Dict[str, Any]] = None
        if outcome_trial:
            comparable = True
            frozen = validated_sealed.get("artifact_hashes", {}).get(cid, {})
            outcome_artifact_bindings[cid] = {
                f"{kind}_sha256": frozen.get(kind) for kind in ("baseline", "candidate")
            }
            for variant in ("variant_a", "variant_b"):
                observed = eval_item[variant].get("outcome")
                observed_hash = observed.get("artifact_sha256") if isinstance(observed, dict) else None
                outcome_artifact_bindings[cid][f"observed_{mapping[variant]}_sha256"] = observed_hash
                if (not isinstance(observed, dict)
                        or observed.get("variant") != variant
                        or observed.get("condition_sha256") != blind_packet["condition_sha256"]
                        or not frozen.get(mapping[variant])
                        or observed_hash != frozen.get(mapping[variant])
                        or any(type(observed.get(signal, {}).get("value")) is not bool
                               for signal in OUTCOME_SIGNALS)):
                    comparable = False
            if not comparable:
                outcome_incomplete_cases.append(cid)
            if not isinstance(cand_outcome, dict):
                if cid not in outcome_incomplete_cases:
                    outcome_incomplete_cases.append(cid)
            else:
                artifact_hash = cand_outcome.get("artifact_sha256")
                outcome_artifact_hashes[cid] = artifact_hash if isinstance(artifact_hash, str) else None
                action = cand_outcome.get("action", {})
                correctness = cand_outcome.get("correctness", {})
                discovery = cand_outcome.get("discovery", {})
                action_v = action.get("value") if isinstance(action, dict) else None
                correctness_v = correctness.get("value") if isinstance(correctness, dict) else None
                discovery_v = discovery.get("value") if isinstance(discovery, dict) else None
                cand_outcome_summary = {
                    "artifact_sha256": outcome_artifact_hashes[cid],
                    "discovery": discovery_v,
                    "action": action_v,
                    "correctness": correctness_v,
                }
                if comparable:
                    if discovery_v is False:
                        outcome_false_discovery_cases.append(cid)
                    if action_v is False:
                        outcome_false_action_cases.append(cid)
                    if correctness_v is False:
                        outcome_false_correctness_cases.append(cid)

        case_breakdown.append({
            "case_id": cid,
            "case_type": ctype,
            "candidate_score": cand_score,
            "baseline_score": base_score,
            "candidate_critical_failure": cand_crit,
            "baseline_critical_failure": base_crit,
            "cost_telemetry": cost_telemetry,
            "candidate_outcome": cand_outcome_summary,
        })

    cand_mean = sum(candidate_scores) / len(candidate_scores) if candidate_scores else 0.0
    base_mean = sum(baseline_scores) / len(baseline_scores) if baseline_scores else 0.0

    # Decision logic:
    # 1. Critical veto: Any candidate critical correctness/security failure vetoes candidate despite mean score.
    # 2. Missing costs: Missing cost telemetry does NOT veto.
    # 3. Score improvement: Candidate mean score > baseline mean score without critical failure passes.
    # 4. Opt-in outcome gate: when outcome_trial is set, SHADOW_ACCEPTED also
    #    requires complete outcome evidence with no false action/correctness.
    outcome_veto = False
    outcome_veto_reason = None
    reason_codes_outcome: List[str] = []
    if outcome_trial:
        if outcome_incomplete_cases:
            outcome_veto = True
            outcome_veto_reason = (
                "Outcome evidence incomplete on case(s): "
                f"{outcome_incomplete_cases}. Missing outcome, artifact hash, "
                "unfrozen/stale artifact binding, or unknown signals cannot support acceptance."
            )
            reason_codes_outcome = ["outcome-evidence-incomplete"]
        elif outcome_false_action_cases or outcome_false_correctness_cases or outcome_false_discovery_cases:
            outcome_veto = True
            outcome_veto_reason = (
                f"Candidate outcome reported action=false on {outcome_false_action_cases} "
                f"or correctness=false on {outcome_false_correctness_cases} "
                f"or necessary discovery=false on {outcome_false_discovery_cases}."
            )
            reason_codes_outcome = ["outcome-action-false"]
        else:
            reason_codes_outcome = []

    decision = "NEEDS_EVIDENCE"
    reason_codes: List[str] = []
    veto = False
    veto_reason = None

    if candidate_critical_failures:
        decision = "VETOED"
        veto = True
        veto_reason = (
            f"Candidate suffered {len(candidate_critical_failures)} critical correctness/security failure(s) "
            f"on case(s): {[item['case_id'] for item in candidate_critical_failures]}. Vetoed despite mean score "
            f"({cand_mean:.2f} vs baseline {base_mean:.2f})."
        )
        reason_codes.append("critical-failure-veto")
    elif outcome_trial and outcome_veto:
        decision = "NEEDS_EVIDENCE"
        veto_reason = outcome_veto_reason
        reason_codes.extend(reason_codes_outcome)
    elif cand_mean > base_mean:
        decision = "SHADOW_ACCEPTED"
        reason_codes.append("focused-trial-improvement")
        if outcome_trial:
            reason_codes.append("outcome-evidence-complete")
    elif cand_mean == base_mean:
        decision = "REJECTED"
        reason_codes.append("no-improvement")
    else:
        decision = "REJECTED"
        reason_codes.append("candidate-regression")

    return {
        "status": "TRIAL_RESOLVED",
        "trial_id": blind_packet["trial_id"],
        "target_skill": blind_packet.get("target_skill"),
        "condition_sha256": blind_packet["condition_sha256"],
        "evaluator": evaluator,
        "decision": decision,
        "veto": veto,
        "veto_reason": veto_reason,
        "reason_codes": reason_codes,
        "metrics": {
            "candidate_mean_score": round(cand_mean, 4),
            "baseline_mean_score": round(base_mean, 4),
            "score_delta": round(cand_mean - base_mean, 4),
            "candidate_critical_failure_count": len(candidate_critical_failures),
            "baseline_critical_failure_count": len(baseline_critical_failures),
            "cases_evaluated_count": len(case_breakdown),
            "outcome_trial": outcome_trial,
            "outcome_incomplete_case_count": len(outcome_incomplete_cases),
            "outcome_false_action_case_count": len(outcome_false_action_cases),
            "outcome_false_correctness_case_count": len(outcome_false_correctness_cases),
            "outcome_false_discovery_case_count": len(outcome_false_discovery_cases),
        },
        "outcome": {
            "enabled": outcome_trial,
            "artifact_bindings": outcome_artifact_bindings,
            "incomplete_cases": outcome_incomplete_cases,
            "false_action_cases": outcome_false_action_cases,
            "false_correctness_cases": outcome_false_correctness_cases,
            "false_discovery_cases": outcome_false_discovery_cases,
        },
        "candidate_critical_failures": candidate_critical_failures,
        "case_breakdown": case_breakdown,
        "release_authority": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Focused and blind trial control plane.")
    sub = parser.add_subparsers(dest="command", required=True)

    create_p = sub.add_parser("create-packet")
    create_p.add_argument("--spec", required=True, help="Path to trial spec JSON file")
    create_p.add_argument("--packet-out", required=True, help="Path to write blind evaluation packet JSON")
    create_p.add_argument("--sealed-out", required=True, help="Path to write sealed mapping JSON")

    resolve_p = sub.add_parser("resolve")
    resolve_p.add_argument("--packet", required=True, help="Path to blind packet JSON")
    resolve_p.add_argument("--sealed", required=True, help="Path to sealed mapping JSON")
    resolve_p.add_argument("--evaluation", required=True, help="Path to evaluation submission JSON")
    resolve_p.add_argument("--output", help="Optional path to write resolution result JSON")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "create-packet":
            spec_data = json.loads(Path(args.spec).read_text(encoding="utf-8"))
            packet, sealed = create_blind_packet(spec_data)
            Path(args.packet_out).write_text(json.dumps(packet, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            Path(args.sealed_out).write_text(json.dumps(sealed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            sys.stdout.write(json.dumps({"status": "PACKET_CREATED", "trial_id": packet["trial_id"]}, indent=2) + "\n")
            return 0

        elif args.command == "resolve":
            packet_data = json.loads(Path(args.packet).read_text(encoding="utf-8"))
            sealed_data = json.loads(Path(args.sealed).read_text(encoding="utf-8"))
            eval_data = json.loads(Path(args.evaluation).read_text(encoding="utf-8"))
            result = resolve_trial(packet_data, sealed_data, eval_data)
            out_json = json.dumps(result, ensure_ascii=False, indent=2)
            if args.output:
                Path(args.output).write_text(out_json + "\n", encoding="utf-8")
            sys.stdout.write(out_json + "\n")
            return 0 if result["decision"] in ("SHADOW_ACCEPTED", "TRIAL_RESOLVED") else 1

    except FocusedTrialError as exc:
        sys.stderr.write(json.dumps({"status": "ERROR", "code": exc.code, "message": exc.message}) + "\n")
        return 2
    except Exception as exc:
        sys.stderr.write(json.dumps({"status": "ERROR", "code": "internal", "message": str(exc)}) + "\n")
        return 3

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
