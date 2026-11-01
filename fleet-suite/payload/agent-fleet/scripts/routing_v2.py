from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
import statistics
from typing import Any, Iterable

import adaptive


SCORE_VERSION = "assignment-v2.0"
ACTIVE_LIFECYCLES = {"ACTIVE", "LIMITED"}
EXPLICIT_RUN_LIFECYCLES = ACTIVE_LIFECYCLES | {"CANARY"}
ANTHROPIC_FABLE_5_1_MODEL_ID = "claude-fable-5-1"
ANTHROPIC_OPUS_5_5_MODEL_ID = "claude-opus-5-5"
ANTHROPIC_HAIKU_5_5_MODEL_ID = "claude-haiku-5-5"
ANTHROPIC_EXACT_ID_MODELS = frozenset({ANTHROPIC_FABLE_5_1_MODEL_ID, ANTHROPIC_OPUS_5_5_MODEL_ID, ANTHROPIC_HAIKU_5_5_MODEL_ID})
LOW_SENSITIVITY_DATA_CLASSES = {"public", "internal"}
RESTRICTED_DATA_CLASSES = {"confidential", "restricted"}
SECURITY_REVIEW_QUALIFICATION = {
    "policy_id": "security-review-qualification-v1",
    "required_lifecycle": "ACTIVE",
    "min_independent_review": 95,
    "min_instruction_following": 91,
    "min_security_review": 95,
    "min_tool_use": 95,
    "min_verifiability": 83,
    "min_evidence_confidence": 0.7,
    "required_review_tools": ("filesystem-read", "independent-review"),
    "read_only_auditor_tools": (
        "filesystem-read",
        "independent-review",
        "security-review",
        "tool-use",
    ),
    "primary_execution_profile_id": "opencode-glm-5-3-flash-high",
    "require_runtime_model_id": True,
    "require_review_mode": True,
    "require_synthetic_handoff_canary": True,
}
ABLITERATED_LOCAL_MODEL_ID = "huihui_ai/Qwen3.8-abliterated"
LOCAL_QWEN_PROFILE_IDS = ("qwen-local", "qwen-local-5070ti")
RED_TEAM_LOCAL_PROFILE_IDS: tuple[str, ...] = ()


def lifecycle_allows_explicit_run(value: Any) -> bool:
    return str(value or "").upper() in EXPLICIT_RUN_LIFECYCLES


def permits_explicit_canary(
    execution_profile: dict[str, Any], gate_results: Iterable[dict[str, Any]]
) -> bool:
    failed = [
        str(gate.get("code"))
        for gate in gate_results
        if not bool(gate.get("passed"))
    ]
    return (
        str(execution_profile.get("lifecycle", "")).upper() == "CANARY"
        and failed == ["lifecycle"]
    )


def _numeric_capability(execution: dict[str, Any], name: str) -> float:
    capabilities = execution.get("capabilities") or {}
    value = capabilities.get(name, 0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def assessment_is_red_team(assessment: dict[str, Any] | None) -> bool:
    payload = assessment or {}
    intent = payload.get("red_team_intent")
    return (
        payload.get("schema_version") in {1, 2}
        and isinstance(intent, dict)
        and set(intent) == {"mode", "explicit"}
        and intent.get("mode") == "red-team"
        and intent.get("explicit") is True
    )


def profile_is_red_team_only(profile: dict[str, Any], execution: dict[str, Any]) -> bool:
    return bool(profile.get("red_team_only")) or bool(execution.get("red_team_only"))


def security_review_qualification_reasons(
    execution: dict[str, Any],
) -> list[str]:
    policy = SECURITY_REVIEW_QUALIFICATION
    reasons: list[str] = []
    lifecycle = str(execution.get("lifecycle", "")).upper()
    if lifecycle != str(policy["required_lifecycle"]).upper():
        reasons.append("lifecycle-not-active")
    if _numeric_capability(execution, "independent-review") < float(
        policy["min_independent_review"]
    ):
        reasons.append("independent-review-below-95")
    if _numeric_capability(execution, "instruction-following") < float(
        policy["min_instruction_following"]
    ):
        reasons.append("instruction-following-below-91")
    if _numeric_capability(execution, "security-review") < float(
        policy["min_security_review"]
    ):
        reasons.append("security-review-below-95")
    if _numeric_capability(execution, "tool-use") < float(policy["min_tool_use"]):
        reasons.append("tool-use-below-95")
    priors = execution.get("efficiency_priors") or {}
    verifiability = priors.get("verifiability", 0)
    if (
        isinstance(verifiability, bool)
        or not isinstance(verifiability, (int, float))
        or float(verifiability) < float(policy["min_verifiability"])
    ):
        reasons.append("verifiability-below-83")
    confidence = execution.get("evidence_confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or float(confidence) < float(policy["min_evidence_confidence"])
    ):
        reasons.append("evidence-confidence-below-0.7")
    tools = {str(item) for item in execution.get("tools", []) if item}
    missing_review_tools = [
        name
        for name in policy["required_review_tools"]
        if name not in tools
    ]
    if missing_review_tools:
        reasons.append(
            "missing-read-only-review-tools=" + ",".join(missing_review_tools)
        )
    allowed_tools = {str(item) for item in policy["read_only_auditor_tools"]}
    disallowed_tools = sorted(tools - allowed_tools)
    if disallowed_tools:
        reasons.append(
            "non-read-only-review-tools=" + ",".join(disallowed_tools)
        )
    missing_allowed_tools = sorted(allowed_tools - tools)
    if missing_allowed_tools:
        reasons.append(
            "inexact-read-only-review-toolset=" + ",".join(missing_allowed_tools)
        )
    modes = {str(item) for item in execution.get("supported_modes", []) if item}
    if "review" not in modes:
        reasons.append("review-mode-unsupported")
    runtime_model_id = str(execution.get("runtime_model_id") or "").strip()
    if not runtime_model_id:
        reasons.append("missing-runtime-model-id")
    return reasons


def execution_qualifies_for_security_review(execution: dict[str, Any]) -> bool:
    return not security_review_qualification_reasons(execution)


def qualified_security_review_executions(
    profiles: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    qualified: list[dict[str, Any]] = []
    for agent_id, profile in profiles.items():
        execution_profiles = profile.get("execution_profiles") or {}
        if not isinstance(execution_profiles, dict):
            continue
        for execution_id, row in execution_profiles.items():
            if not isinstance(row, dict):
                continue
            execution = {
                **row,
                "agent": agent_id,
                "adapter": profile.get("adapter"),
                "cli_provider_id": profile.get("cli_provider_id"),
                "model_provider_id": row.get(
                    "model_provider_id", profile.get("cli_provider_id")
                ),
                "execution_profile_id": execution_id,
            }
            reasons = security_review_qualification_reasons(execution)
            if reasons:
                continue
            qualified.append(execution)
    qualified.sort(
        key=lambda item: (
            str(item["agent"]),
            str(item["execution_profile_id"]),
        )
    )
    return qualified


VALID_ALLOCATION_POLICIES = {
    "balanced",
    "burn-before-reset",
    "fast-lane",
    "parallel-exploration",
    "preserve-premium",
    "quality-first",
}
RISK_LEVEL = {"low": 1, "medium": 2, "high": 3, "critical": 4}
DEFAULT_DENY_PATTERNS = (
    "not authenticated",
    "not logged in",
    "authentication required",
    "login required",
    "please log in",
    "subscription required",
)
SEMVER_PATTERN = re.compile(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)")
GENERAL_CAPABILITIES = {
    "independent-review",
    "instruction-following",
    "multimodal",
    "speed",
    "tool-use",
    "web",
    "write",
}
LOW_RISK_WEIGHTS = {
    "capability_fit": 0.40,
    "quota_utility": 0.12,
    "speed": 0.12,
    "first_pass": 0.12,
    "verifiability": 0.10,
    "concurrency": 0.06,
    "subscription_utility": 0.08,
}
HIGH_RISK_WEIGHTS = {
    "capability_fit": 0.65,
    "quota_utility": 0.04,
    "speed": 0.03,
    "first_pass": 0.15,
    "verifiability": 0.08,
    "concurrency": 0.02,
    "subscription_utility": 0.03,
}


class RoutingV2Error(RuntimeError):
    pass


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return normalized or "default"


def _bounded(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _canonical_fingerprint(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def synthesize_execution_profiles(
    profile_id: str, profile: dict[str, Any]
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    configured = profile.get("execution_profiles")
    configured_map = profile.get("execution_profile_map")
    if isinstance(configured, dict) and configured and isinstance(configured_map, dict):
        return configured, configured_map

    profiles: dict[str, dict[str, Any]] = {}
    mapping: dict[str, str] = {}
    model_to_id: dict[str, str] = {}
    for complexity in ("low", "medium", "high"):
        model = str(profile["models"][complexity])
        execution_id = model_to_id.get(model)
        if execution_id is None:
            execution_id = f"{profile_id}-{_slug(model)}"
            suffix = 2
            while execution_id in profiles:
                execution_id = f"{profile_id}-{_slug(model)}-{suffix}"
                suffix += 1
            model_to_id[model] = execution_id
            profiles[execution_id] = {
                "model": model,
                "runtime_model_id": model,
                "reasoning_level": complexity,
                "lifecycle": "ACTIVE" if profile.get("routing_enabled") else "DISCOVERED",
                "max_risk": "critical",
                "supported_modes": list(profile["supported_modes"]),
                "tools": _default_tools(profile),
                "context_window_tokens": None,
                "capabilities": _derived_capabilities(profile),
                "efficiency_priors": {
                    "speed": 50,
                    "first_pass": 50,
                    "verifiability": 50,
                    "expected_overhead_minutes": 8,
                    "expected_repair_minutes": 20,
                    "uncertainty": 0.5,
                },
                "premium": complexity == "high",
                "evidence_confidence": 0.35,
            }
        mapping[complexity] = execution_id
    return profiles, mapping


def _default_tools(profile: dict[str, Any]) -> list[str]:
    tools = {"filesystem-read", "shell"}
    modes = set(profile.get("supported_modes", []))
    if "implement" in modes:
        tools.update({"filesystem-write", "write"})
    tools.update({"independent-review", "tool-use"})
    if profile.get("adapter") != "qwen":
        tools.add("web")
    return sorted(tools)


def _derived_capabilities(profile: dict[str, Any]) -> dict[str, int]:
    fit = profile.get("fit", {})
    return {
        "independent-review": int(fit.get("review", 50)),
        "instruction-following": int(
            round(
                (
                    float(fit.get("implementation", 50))
                    + float(fit.get("repeatable", 50))
                    + float(fit.get("review", 50))
                )
                / 3.0
            )
        ),
        "multimodal": int(fit.get("multimodal", 50)),
        "speed": int(fit.get("repeatable", 50)),
        "tool-use": int(
            round(
                (
                    float(fit.get("implementation", 50))
                    + float(fit.get("debugging", 50))
                )
                / 2.0
            )
        ),
        "web": int(fit.get("research", 50)),
        "write": 100 if "implement" in profile.get("supported_modes", []) else 0,
    }


def prepare_execution_registry(
    profiles: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    registry: dict[str, dict[str, Any]] = {}
    for profile_id, profile in profiles.items():
        execution_profiles, mapping = synthesize_execution_profiles(profile_id, profile)
        normalized_profiles: dict[str, dict[str, Any]] = {}
        renamed: dict[str, str] = {}
        for execution_id, row in execution_profiles.items():
            target_id = execution_id
            if target_id in registry or target_id in normalized_profiles:
                target_id = f"{profile_id}-{execution_id}"
                suffix = 2
                while target_id in registry or target_id in normalized_profiles:
                    target_id = f"{profile_id}-{execution_id}-{suffix}"
                    suffix += 1
            renamed[execution_id] = target_id
            normalized_profiles[target_id] = row
        execution_profiles = normalized_profiles
        mapping = {
            complexity: renamed.get(execution_id, execution_id)
            for complexity, execution_id in mapping.items()
        }
        if set(mapping) != {"low", "medium", "high"}:
            raise RoutingV2Error(
                f"Profile {profile_id} needs an execution profile for every complexity"
            )
        for complexity, execution_id in mapping.items():
            if execution_id not in execution_profiles:
                raise RoutingV2Error(
                    f"Profile {profile_id} maps {complexity} to missing {execution_id}"
                )
        profile["execution_profiles"] = execution_profiles
        profile["execution_profile_map"] = mapping
        configured_mode_map = profile.get("execution_profile_map_by_mode", {})
        if configured_mode_map:
            if not isinstance(configured_mode_map, dict):
                raise RoutingV2Error(
                    f"Profile {profile_id} execution_profile_map_by_mode is invalid"
                )
            normalized_mode_map: dict[str, dict[str, str]] = {}
            for mode, mode_mapping in configured_mode_map.items():
                if mode not in {"advisory", "review", "implement"}:
                    raise RoutingV2Error(
                        f"Profile {profile_id} has an invalid mode map: {mode}"
                    )
                if not isinstance(mode_mapping, dict) or set(mode_mapping) != {
                    "low",
                    "medium",
                    "high",
                }:
                    raise RoutingV2Error(
                        f"Profile {profile_id}/{mode} needs a profile for every complexity"
                    )
                normalized_mode_map[mode] = {
                    complexity: renamed.get(str(execution_id), str(execution_id))
                    for complexity, execution_id in mode_mapping.items()
                }
                for complexity, execution_id in normalized_mode_map[mode].items():
                    if execution_id not in execution_profiles:
                        raise RoutingV2Error(
                            f"Profile {profile_id}/{mode} maps {complexity} to missing {execution_id}"
                        )
            profile["execution_profile_map_by_mode"] = normalized_mode_map
        for execution_id, row in execution_profiles.items():
            if execution_id in registry:
                raise RoutingV2Error(f"Duplicate execution profile ID: {execution_id}")
            _validate_execution_profile(profile_id, execution_id, row)
            registry[execution_id] = {
                **row,
                "execution_profile_id": execution_id,
                "agent": profile_id,
                "adapter": profile["adapter"],
            }
    return registry


def _validate_execution_profile(
    profile_id: str, execution_id: str, row: Any
) -> None:
    if not isinstance(row, dict):
        raise RoutingV2Error(f"Execution profile {execution_id} is invalid")
    required = {
        "model",
        "reasoning_level",
        "lifecycle",
        "max_risk",
        "supported_modes",
        "tools",
        "capabilities",
        "efficiency_priors",
    }
    if not required.issubset(row):
        missing = ", ".join(sorted(required - set(row)))
        raise RoutingV2Error(f"Execution profile {execution_id} misses: {missing}")
    if str(row["lifecycle"]).upper() not in {
        "DISCOVERED",
        "PROFILED",
        "CANARY",
        "LIMITED",
        "ACTIVE",
        "DEPRECATED",
    }:
        raise RoutingV2Error(f"Execution profile {execution_id} has invalid lifecycle")
    if row["max_risk"] not in RISK_LEVEL:
        raise RoutingV2Error(f"Execution profile {execution_id} has invalid max_risk")
    if not isinstance(row["supported_modes"], list) or not row["supported_modes"]:
        raise RoutingV2Error(f"Execution profile {execution_id} has no modes")
    if not isinstance(row["tools"], list):
        raise RoutingV2Error(f"Execution profile {execution_id} tools are invalid")
    capabilities = row["capabilities"]
    if not isinstance(capabilities, dict):
        raise RoutingV2Error(f"Execution profile {execution_id} capabilities are invalid")
    for capability, score in capabilities.items():
        if (
            not isinstance(capability, str)
            or isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not 0 <= score <= 100
        ):
            raise RoutingV2Error(
                f"Execution profile {profile_id}/{execution_id} capability is invalid"
            )


def configured_models(profile: dict[str, Any]) -> set[str]:
    return {
        str(row["model"])
        for row in profile.get("execution_profiles", {}).values()
        if isinstance(row, dict) and "model" in row
    }


def resolve_execution_profile(
    profile_id: str,
    profile: dict[str, Any],
    *,
    complexity: str,
    mode: str,
    required_tools: Iterable[str],
    model_override: str | None = None,
    execution_profile_override: str | None = None,
) -> dict[str, Any]:
    execution_profiles = profile["execution_profiles"]
    if execution_profile_override:
        selected_id = execution_profile_override
        if selected_id not in execution_profiles:
            raise RoutingV2Error(
                f"Unknown execution profile for {profile_id}: {selected_id}"
            )
    elif model_override:
        matches = [
            execution_id
            for execution_id, row in execution_profiles.items()
            if str(row["model"]) == model_override
        ]
        if not matches:
            raise RoutingV2Error(
                f"Model {model_override} is not configured for {profile_id}"
            )
        preferred = (
            profile.get("execution_profile_map_by_mode", {})
            .get(mode, {})
            .get(complexity, profile["execution_profile_map"].get(complexity))
        )
        selected_id = preferred if preferred in matches else sorted(matches)[0]
    else:
        selected_id = str(
            profile.get("execution_profile_map_by_mode", {})
            .get(mode, {})
            .get(complexity, profile["execution_profile_map"][complexity])
        )
    base = dict(execution_profiles[selected_id])
    if model_override and str(base["model"]) != model_override:
        raise RoutingV2Error(
            f"Execution profile {selected_id} does not use model {model_override}"
        )
    tools = sorted({str(item) for item in required_tools})
    concrete = {
        "agent": profile_id,
        "adapter": profile["adapter"],
        "base_execution_profile_id": selected_id,
        "model": str(base["model"]),
        "runtime_model_id": str(base.get("runtime_model_id", base["model"])),
        "reasoning_level": str(base["reasoning_level"]),
        "mode": mode,
        "required_tools": tools,
    }
    return {
        **base,
        **concrete,
        "execution_profile_id": selected_id,
        "execution_profile_fingerprint": _canonical_fingerprint(concrete),
    }


def readiness_from_probes(
    profile: dict[str, Any],
    *,
    executable: str | None,
    version_probe: dict[str, Any] | None,
    deep_probe: dict[str, Any] | None,
) -> dict[str, Any]:
    reasons: list[str] = []
    if executable is None:
        reasons.append("executable-not-found")
    if not version_probe or not version_probe.get("launchable"):
        reasons.append("version-probe-failed")
    readiness = profile.get("readiness", {})
    require_deep = bool(readiness.get("require_deep_probe", True))
    if require_deep and (not deep_probe or not deep_probe.get("launchable")):
        reasons.append("deep-probe-failed")
    output = "\n".join(
        str((probe or {}).get("output", ""))
        for probe in (version_probe, deep_probe)
    ).casefold()
    patterns = tuple(
        str(item).casefold()
        for item in readiness.get("deny_patterns", DEFAULT_DENY_PATTERNS)
    )
    matched = sorted(pattern for pattern in patterns if pattern and pattern in output)
    if matched:
        reasons.append("authentication-or-entitlement-denied")
    minimum_version = str(readiness.get("minimum_version", "")).strip() or None
    observed_match = SEMVER_PATTERN.search(
        str((version_probe or {}).get("output", ""))
    )
    observed_version = observed_match.group(0) if observed_match else None
    if minimum_version:
        minimum_match = SEMVER_PATTERN.fullmatch(minimum_version)
        if minimum_match is None:
            reasons.append("minimum-version-policy-invalid")
        elif observed_match is None:
            reasons.append("runtime-version-unparseable")
        elif tuple(map(int, observed_match.groups())) < tuple(
            map(int, minimum_match.groups())
        ):
            reasons.append("minimum-version-not-met")
    fatal_patterns = tuple(
        str(item).casefold()
        for item in readiness.get("fatal_patterns", [])
        if str(item).strip()
    )
    matched_fatal = sorted(
        pattern for pattern in fatal_patterns if pattern and pattern in output
    )
    if matched_fatal:
        reasons.append("runtime-probe-reported-fatal-error")
    required_lines = readiness.get("required_deep_probe_lines", [])
    observed_lines = {line.strip().casefold() for line in str((deep_probe or {}).get("output", "")).splitlines()}
    if any(str(line).strip().casefold() not in observed_lines for line in required_lines):
        reasons.append("required-runtime-probe-confirmation-missing")
    required_substrings = readiness.get("required_deep_probe_substrings", [])
    deep_output = str((deep_probe or {}).get("output", ""))
    if any(
        str(item).strip() and str(item).strip() not in deep_output
        for item in required_substrings
    ):
        reasons.append("required-runtime-probe-confirmation-missing")
    return {
        "ready": not reasons,
        "reasons": sorted(set(reasons)),
        "matched_deny_patterns": matched,
        "matched_fatal_patterns": matched_fatal,
        "minimum_version": minimum_version,
        "observed_version": observed_version,
        "executable": executable,
        "version_probe": version_probe,
        "deep_probe": deep_probe,
    }


def runtime_model_visible(
    execution: dict[str, Any],
    *,
    profile: dict[str, Any],
    readiness: dict[str, Any],
) -> tuple[bool, str]:
    policy = profile.get("readiness", {})
    if readiness.get("synthetic"):
        return True, "inventory-check-skipped-in-pure-ranking"
    if not policy.get("enforce_model_inventory"):
        return True, "inventory-check-not-enforced"
    local_config = profile.get("local_runtime", {})
    if profile.get("adapter") == "openclaw" and local_config.get("kind") == "openclaw-vllm":
        # CLI --version is not a model inventory. Use the bounded live backend
        # probe attached by live_readiness, never the configured name alone.
        state = readiness.get("local_runtime", {})
        probe = state.get("probe") or {}
        observed = probe.get("observed_model_id")
        runtime_id = execution.get("runtime_model_id", execution.get("model", ""))
        expected = "vllm/Qwen3.8-27B"
        visible = (
            state.get("status") == "READY"
            and state.get("routing_ready") is True
            and probe.get("context_bound_satisfied") is True
            and observed == "Qwen3.8-27B"
            and runtime_id == expected
        )
        return visible, ("runtime-model-live-probe=" if visible else "runtime-model-live-probe-rejected=") + expected
    deep_probe = readiness.get("deep_probe")
    output = (
        str(deep_probe.get("output", ""))
        if isinstance(deep_probe, dict)
        else ""
    ).casefold()
    runtime_model_id = str(
        execution.get("runtime_model_id", execution.get("model", ""))
    ).casefold()
    if not runtime_model_id:
        return False, "runtime-model-id-missing"
    pattern = re.compile(
        rf"(?<![a-z0-9]){re.escape(runtime_model_id)}(?![a-z0-9])"
    )
    visible = bool(pattern.search(output))
    return visible, (
        f"runtime-model-visible={runtime_model_id}"
        if visible
        else f"runtime-model-not-listed={runtime_model_id}"
    )


def _capability_score(
    execution: dict[str, Any],
    *,
    profile: dict[str, Any],
    workload_weights: dict[str, int],
    orchestrator_score: float,
    arena_score: float | None,
    submitted_orchestrator_score: float | None = None,
) -> tuple[float, dict[str, Any]]:
    static_score = adaptive.static_fit(profile, workload_weights)
    blended, arena_adjustment = adaptive.blended_fit(
        static_score=static_score,
        orchestrator_score=orchestrator_score,
        arena_score=arena_score,
    )
    return blended, {
        "static": static_score,
        "orchestrator": (
            submitted_orchestrator_score
            if submitted_orchestrator_score is not None
            else orchestrator_score
        ),
        "orchestrator_used": orchestrator_score,
        "arena_percentile": arena_score,
        "arena_adjustment": arena_adjustment,
    }


def _gate(
    gates: list[dict[str, Any]],
    code: str,
    passed: bool,
    evidence: str,
) -> None:
    gates.append(
        {"code": code, "passed": bool(passed), "hard": True, "evidence": evidence}
    )


# Context-window gate. A Task Profile may carry context_load.estimated_tokens;
# Fleet also estimates the routed task as ceil(UTF-8 bytes / 4) and the gate
# uses the larger of the two known estimates. Four
# bytes per token is the usual BPE rate for English prose and code, and
# multi-byte text only raises the byte count. The task text is a floor of the
# delegated prompt (Fleet adds its preamble), so routing never refuses a task
# that could fit; the send gate repeats the comparison on the real prompt
# bytes. An unknown window stays unknown and never blocks.
CONTEXT_BYTES_PER_TOKEN = 4
CONTEXT_WINDOW_FILL = 0.9


def estimate_context_tokens(byte_count: Any) -> int | None:
    if isinstance(byte_count, bool) or not isinstance(byte_count, int) or byte_count < 0:
        return None
    return -(-byte_count // CONTEXT_BYTES_PER_TOKEN)


def task_context_estimate(task: str) -> dict[str, Any]:
    size = len(str(task).encode("utf-8"))
    return {
        "method": "utf8-bytes-div-4",
        "task_bytes": size,
        "estimated_tokens": estimate_context_tokens(size),
    }


def context_prompt_byte_limit(window_tokens: Any) -> int | None:
    """Largest prompt in bytes that the context-window gate lets through."""
    if (
        isinstance(window_tokens, bool)
        or not isinstance(window_tokens, int)
        or window_tokens <= 0
    ):
        return None
    return int(window_tokens * CONTEXT_WINDOW_FILL) * CONTEXT_BYTES_PER_TOKEN


def context_window_gate(
    execution: dict[str, Any] | None, assessment: dict[str, Any] | None
) -> dict[str, Any]:
    """The hard context-window gate shared by adaptive and baseline routing."""
    assessment = assessment if isinstance(assessment, dict) else {}
    context = assessment.get("context_load", {})
    fallback = assessment.get("context_estimate")
    fallback = fallback if isinstance(fallback, dict) else {}
    # Both are lower bounds of the delegated prompt: an understated assessment
    # may not shrink the task-text estimate, so the larger known one counts.
    candidates = [
        (value, source)
        for value, source in (
            (
                context.get("estimated_tokens") if isinstance(context, dict) else None,
                "assessment",
            ),
            (
                fallback.get("estimated_tokens"),
                str(fallback.get("method", "estimate")),
            ),
        )
        if isinstance(value, int) and not isinstance(value, bool)
    ]
    estimated_tokens, estimate_source = (
        max(candidates, key=lambda item: item[0]) if candidates else (None, "unknown")
    )
    window = (execution or {}).get("context_window_tokens")
    if isinstance(window, bool) or not isinstance(window, int):
        window = None
    context_ok = not (
        isinstance(estimated_tokens, int)
        and window is not None
        and estimated_tokens > int(window * CONTEXT_WINDOW_FILL)
    )
    gates: list[dict[str, Any]] = []
    _gate(
        gates,
        "context-window",
        context_ok,
        f"estimated={estimated_tokens};source={estimate_source};window={window}",
    )
    return gates[0]


def anthropic_retention_gate(
    execution: dict[str, Any],
    *,
    profile: dict[str, Any],
    assessment: dict[str, Any],
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    runtime_model_id = str(
        execution.get("runtime_model_id", execution.get("model", ""))
    )
    if runtime_model_id not in ANTHROPIC_EXACT_ID_MODELS:
        return {
            "code": "anthropic-retention",
            "passed": True,
            "hard": True,
            "evidence": "not-applicable",
        }
    policy = profile.get("data_policy")
    if not isinstance(policy, dict) or runtime_model_id not in policy.get(
        "applies_to_models", []
    ):
        return {
            "code": "anthropic-retention",
            "passed": False,
            "hard": True,
            "evidence": "model-policy-missing",
        }
    handling = assessment.get("data_handling")
    classification = (
        str(handling.get("classification", "unknown"))
        if isinstance(handling, dict)
        else "unknown"
    )
    if classification in LOW_SENSITIVITY_DATA_CLASSES:
        return {
            "code": "anthropic-retention",
            "passed": True,
            "hard": True,
            "evidence": f"classification={classification};zdr-not-required",
        }
    if classification not in RESTRICTED_DATA_CLASSES:
        return {
            "code": "anthropic-retention",
            "passed": False,
            "hard": True,
            "evidence": "classification-required",
        }
    authorization = (
        handling.get("anthropic_zdr_authorization")
        if isinstance(handling, dict)
        else None
    )
    if not isinstance(authorization, dict):
        return {
            "code": "anthropic-retention",
            "passed": False,
            "hard": True,
            "evidence": f"classification={classification};authorization=missing",
        }
    try:
        verified_at = adaptive.parse_timestamp(str(authorization["verified_at"]))
        expires_at = adaptive.parse_timestamp(str(authorization["expires_at"]))
    except (KeyError, adaptive.AdaptiveError):
        return {
            "code": "anthropic-retention",
            "passed": False,
            "hard": True,
            "evidence": f"classification={classification};authorization=invalid",
        }
    current = now or dt.datetime.now(dt.timezone.utc)
    models = authorization.get("models", [])
    authorized = (
        authorization.get("status") == "authorized"
        and authorization.get("scope") == "account"
        and isinstance(models, list)
        and runtime_model_id in models
        and bool(str(authorization.get("evidence_ref", "")).strip())
        and verified_at <= current < expires_at
    )
    return {
        "code": "anthropic-retention",
        "passed": authorized,
        "hard": True,
        "evidence": (
            f"classification={classification};authorization="
            + ("current" if authorized else "invalid-or-expired")
        ),
    }


# Every gate code quota_hard_gates can emit. A quality-floor fallback may
# relax other gates but never one of these.
QUOTA_GATE_CODES = frozenset({
    "quota-observed", "quota-not-exhausted", "quota-unknown-capped", "pace-brake-clear",
})


def quota_hard_gates(
    profile: dict[str, Any], quota: dict[str, Any], *,
    override_pace_brake: bool = False,
    mode: str | None = None,
) -> list[dict[str, Any]]:
    gates: list[dict[str, Any]] = []
    collection = profile.get("quota_collection")
    collection = collection if isinstance(collection, dict) else {}
    requires_fresh = bool(collection.get("require_fresh_snapshot", False))
    minimum_confidence = collection.get("minimum_confidence", 0.0)
    if (
        isinstance(minimum_confidence, bool)
        or not isinstance(minimum_confidence, (int, float))
        or not 0.0 <= float(minimum_confidence) <= 1.0
    ):
        raise RoutingV2Error("quota_collection.minimum_confidence is invalid")
    headroom = quota.get("headroom")
    confidence = quota.get("confidence", 0.0)
    # A fresh credit balance is a numeric observation even without a plan
    # fraction (CodeBuddy dashboard readings may carry only credits).
    observed_value = (
        headroom if headroom is not None else quota.get("credits_remaining")
    )
    quota_observed = (
        quota.get("status") == "fresh"
        and isinstance(observed_value, (int, float))
        and not isinstance(observed_value, bool)
        # NaN/Infinity is not a trustworthy balance or fraction.
        and math.isfinite(float(observed_value))
        and isinstance(confidence, (int, float))
        and not isinstance(confidence, bool)
        and float(confidence) >= float(minimum_confidence)
    )
    _gate(
        gates,
        "quota-observed",
        quota_observed or not requires_fresh,
        (
            f"required={str(requires_fresh).lower()};"
            f"status={quota.get('status', 'unknown')};"
            f"confidence={confidence};minimum={minimum_confidence}"
        ),
    )
    # An exhausted flag is actionable only while its observation is fresh.
    # Stale/manual rows must not turn a past exhaustion into a permanent deny;
    # unknown telemetry remains unknown and is handled by quota-observed above.
    explicit_blocking = (
        quota.get("hard_blocking") if quota.get("status") == "fresh" else None
    )
    exhausted = (
        bool(explicit_blocking)
        if isinstance(explicit_blocking, bool)
        else (
            quota.get("status") == "fresh"
            and isinstance(headroom, (int, float))
            and not isinstance(headroom, bool)
            and float(headroom) <= 0.0
        )
    )
    # A fresh non-advisory zero balance is exhausted even when a plan fraction
    # still shows headroom and no provider refusal has been stored yet.
    credits = quota.get("credits_remaining")
    credits_exhausted = (
        quota.get("status") == "fresh"
        and quota.get("authority") != "advisory"
        and isinstance(credits, (int, float))
        and not isinstance(credits, bool)
        and float(credits) <= 0.0
    )
    exhausted = exhausted or credits_exhausted
    # Availability survives numeric-snapshot expiry. A fresh native check can
    # clear it without inventing a token balance for unreported windows.
    if quota.get('availability_verified') is True:
        exhausted = False
    if quota.get('availability_blocked') is True:
        exhausted = True
    availability = quota.get("availability")
    availability = availability if isinstance(availability, dict) else {}
    _gate(
        gates,
        "quota-not-exhausted",
        not exhausted,
        (
            f"status={quota.get('status', 'unknown')};headroom={headroom};"
            f"authority={quota.get('authority', 'legacy')};"
            f"hard_blocking={str(exhausted).lower()}"
            + (f";credits_remaining={credits}" if credits_exhausted else "")
            + (
                # Name the provider refusal instead of a bare unknown status.
                f";availability_blocked={availability.get('status', 'unknown')}:"
                f"{availability.get('reason') or availability.get('error_type') or 'unspecified'}"
                if quota.get('availability_blocked') is True
                else ""
            )
        ),
    )
    # Owner policy for unobserved quota, per pool (quota_collection). The
    # default fail-open keeps the historic behaviour; fail-capped admits only
    # single-step advisory/review work while the pool has no fresh observation.
    if collection.get("unknown_quota_mode", "fail-open") == "fail-capped":
        capped = not quota_observed and mode == "implement"
        _gate(
            gates,
            "quota-unknown-capped",
            not capped,
            (
                f"policy=fail-capped;observed={str(quota_observed).lower()};"
                f"mode={mode or 'unknown'};allowed=advisory,review"
            ),
        )
    brake_config = collection.get("pace_brake")
    if isinstance(brake_config, dict) and brake_config.get("enabled") is True:
        brake = quota.get("pace_brake")
        brake = brake if isinstance(brake, dict) else {}
        _gate(
            gates,
            "pace-brake-clear",
            brake.get("active") is not True or override_pace_brake,
            (
                f"reason={brake.get('reason', 'missing-brake-view')};"
                f"until={brake.get('until')};"
                f"override={str(override_pace_brake).lower()}"
            ),
        )
    return gates


def hard_gates(
    execution: dict[str, Any],
    *,
    profile: dict[str, Any],
    assessment: dict[str, Any],
    readiness: dict[str, Any],
    quota: dict[str, Any],
    mode: str,
    override_pace_brake: bool = False,
) -> list[dict[str, Any]]:
    gates: list[dict[str, Any]] = []
    _gate(
        gates,
        "routing-enabled",
        bool(profile.get("routing_enabled")),
        str(profile.get("routing_blocker") or "enabled"),
    )
    lifecycle = str(execution.get("lifecycle", "DISCOVERED")).upper()
    _gate(
        gates,
        "lifecycle",
        lifecycle in ACTIVE_LIFECYCLES,
        lifecycle,
    )
    _gate(
        gates,
        "runtime-readiness",
        bool(readiness.get("ready")),
        ",".join(readiness.get("reasons", [])) or "ready",
    )
    model_visible, model_evidence = runtime_model_visible(
        execution,
        profile=profile,
        readiness=readiness,
    )
    _gate(gates, "runtime-model", model_visible, model_evidence)
    supported_modes = set(execution.get("supported_modes", []))
    _gate(gates, "mode", mode in supported_modes, f"{mode} in {sorted(supported_modes)}")
    required_tools = set(assessment.get("required_tools", []))
    if "write" in assessment.get("required_capabilities", []):
        required_tools.add("write")
    if "web" in assessment.get("required_capabilities", []):
        required_tools.add("web")
    available_tools = set(execution.get("tools", []))
    missing_tools = sorted(required_tools - available_tools)
    _gate(
        gates,
        "tools",
        not missing_tools,
        "missing=" + ",".join(missing_tools) if missing_tools else "all-required-tools",
    )
    capabilities = {
        **{
            str(key): float(value)
            for key, value in profile.get("fit", {}).items()
        },
        **_derived_capabilities(profile),
        **{
            str(key): float(value)
            for key, value in execution.get("capabilities", {}).items()
        },
    }
    missing_capabilities = []
    for capability in assessment.get("required_capabilities", []):
        if capability in {
            "write",
            "web",
            "independent-review",
            "security-review",
            "multimodal",
        }:
            if float(capabilities.get(capability, 0)) < 50:
                missing_capabilities.append(f"{capability}<50")
    for capability, floor in assessment.get("minimum_capabilities", {}).items():
        if float(capabilities.get(capability, 0)) < float(floor):
            missing_capabilities.append(
                f"{capability}={capabilities.get(capability, 0)}<{floor}"
            )
    _gate(
        gates,
        "capability-floors",
        not missing_capabilities,
        ";".join(missing_capabilities) if missing_capabilities else "met",
    )
    task_risk = str(assessment.get("risk", "medium"))
    max_risk = str(execution.get("max_risk", "critical"))
    _gate(
        gates,
        "risk-ceiling",
        RISK_LEVEL[task_risk] <= RISK_LEVEL[max_risk],
        f"task={task_risk};max={max_risk}",
    )
    gates.append(context_window_gate(execution, assessment))
    red_team_only = profile_is_red_team_only(profile, execution)
    red_team_task = assessment_is_red_team(assessment)
    _gate(
        gates,
        "red-team-only",
        (not red_team_only) or red_team_task,
        "red-team-task" if red_team_task else "general-purpose-rejected",
    )
    gates.append(
        anthropic_retention_gate(
            execution,
            profile=profile,
            assessment=assessment,
        )
    )
    gates.extend(quota_hard_gates(
        profile, quota, override_pace_brake=override_pace_brake, mode=mode
    ))
    return gates


def _reset_bonus(quota: dict[str, Any], now: dt.datetime | None = None) -> float:
    if quota.get("status") != "fresh":
        return 0.0
    current = now or dt.datetime.now(dt.timezone.utc)
    bonuses: list[float] = []
    for key in ("short_window_reset_at", "weekly_reset_at"):
        raw = quota.get(key)
        if not raw:
            continue
        try:
            reset = adaptive.parse_timestamp(str(raw))
        except adaptive.AdaptiveError:
            continue
        hours = (reset - current).total_seconds() / 3600.0
        if 0 <= hours <= 24:
            bonuses.append((24.0 - hours) / 24.0 * 20.0)
    return max(bonuses, default=0.0)


def local_execution_stats(
    records: Iterable[dict[str, Any]],
    *,
    execution_profile_id: str,
    agent: str,
    model: str,
    kind: str,
) -> dict[str, Any]:
    by_root: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        if (
            record.get("agent") != agent
            or record.get("model") != model
            or record.get("kind") != kind
            or not isinstance(record.get("evaluation"), dict)
        ):
            continue
        record_execution_id = record.get("execution_profile_id")
        if record_execution_id and record_execution_id != execution_profile_id:
            continue
        root_id = str(record.get("root_task_id") or record.get("run_id") or id(record))
        by_root.setdefault(root_id, []).append(record)
    samples = []
    for attempts in by_root.values():
        selected = max(
            attempts,
            key=lambda item: (
                str(item.get("completed_at", "")),
                str(item.get("run_id", "")),
            ),
        )
        samples.append({**selected, "_root_attempt_count": len(attempts)})
    evaluations = []
    for row in samples:
        evaluation = dict(row["evaluation"])
        if int(row.get("_root_attempt_count", 1)) > 1:
            evaluation["first_pass"] = False
        followups = row.get("followups", [])
        if isinstance(followups, list):
            reopened = any(
                isinstance(item, dict) and item.get("reopened_7d") is True
                for item in followups
            )
            regressions = sum(
                int(item.get("regressions_found", 0))
                for item in followups
                if isinstance(item, dict)
                and isinstance(item.get("regressions_found"), int)
            )
            if reopened or regressions:
                evaluation["criteria_pass"] = False
                if isinstance(evaluation.get("quality_score"), (int, float)):
                    evaluation["quality_score"] = min(
                        float(evaluation["quality_score"]), 60.0
                    )
        evaluations.append(evaluation)
    qualities = [
        float(item["quality_score"])
        for item in evaluations
        if isinstance(item.get("quality_score"), (int, float))
    ]
    passes = [item.get("criteria_pass") is True for item in evaluations]
    first_pass = [
        item.get("first_pass") is True
        for item in evaluations
        if isinstance(item.get("first_pass"), bool)
    ]
    durations = [
        float(row["duration_seconds"])
        for row in samples
        if isinstance(row.get("duration_seconds"), (int, float))
    ]
    accepted_tokens = [
        float(row["token_usage"]["total_tokens"])
        for row in samples
        if row["evaluation"].get("criteria_pass") is True
        and isinstance(row.get("token_usage"), dict)
        and isinstance(row["token_usage"].get("total_tokens"), (int, float))
    ]
    count = len(samples)
    active = count >= 5
    weight = count / (count + 20.0) if count else 0.0
    return {
        "sample_count": count,
        "active": active,
        "shrinkage_weight": round(weight, 4),
        "quality_mean": round(statistics.mean(qualities), 3) if qualities else None,
        "reliability_score": (
            round(100.0 * sum(passes) / len(passes), 3) if passes else None
        ),
        "first_pass_score": (
            round(100.0 * sum(first_pass) / len(first_pass), 3)
            if first_pass
            else None
        ),
        "median_duration_seconds": (
            round(statistics.median(durations), 3) if durations else None
        ),
        "median_accepted_tokens": (
            round(statistics.median(accepted_tokens), 3)
            if accepted_tokens
            else None
        ),
    }


def assignment_score(
    *,
    capability_fit: float,
    execution: dict[str, Any],
    assessment: dict[str, Any],
    quota: dict[str, Any],
    underuse_score: float,
    local: dict[str, Any],
    pool_occupancy: dict[str, Any] | None = None,
    pace: dict[str, Any] | None = None,
) -> tuple[float, dict[str, Any], dict[str, float], list[str]]:
    priors = execution.get("efficiency_priors", {})
    speed = float(priors.get("speed", 50))
    first_pass = float(priors.get("first_pass", 50))
    verifiability = float(priors.get("verifiability", 50))
    local_weight = float(local.get("shrinkage_weight", 0.0))
    local_cap = 5.0 if int(local.get("sample_count", 0)) < 20 else 12.0
    local_adjustments: list[str] = []
    if local.get("active"):
        local_quality = local.get("quality_mean")
        if isinstance(local_quality, (int, float)):
            delta = _bounded(
                (float(local_quality) - capability_fit) * local_weight,
                -local_cap,
                local_cap,
            )
            capability_fit = _bounded(capability_fit + delta)
            local_adjustments.append(f"capability_fit={delta:+.3f}")
        local_first_pass = local.get("first_pass_score")
        if isinstance(local_first_pass, (int, float)):
            first_pass = (
                (1.0 - local_weight) * first_pass
                + local_weight * float(local_first_pass)
            )
            local_adjustments.append("first_pass=shrunk-local")
        duration = local.get("median_duration_seconds")
        if isinstance(duration, (int, float)):
            observed_speed = 100.0 / (1.0 + float(duration) / 600.0)
            speed = (1.0 - local_weight) * speed + local_weight * observed_speed
            local_adjustments.append("speed=shrunk-local")
    headroom = quota.get("headroom")
    quota_utility = (
        100.0 * float(headroom)
        if quota.get("status") == "fresh"
        and isinstance(headroom, (int, float))
        else 50.0
    )
    policy = str(assessment.get("allocation_policy", "balanced"))
    if policy == "burn-before-reset":
        quota_utility = _bounded(quota_utility + _reset_bonus(quota))
    if policy == "preserve-premium" and execution.get("premium"):
        quota_utility = _bounded(quota_utility - 30.0)
    if policy == "fast-lane":
        speed = _bounded(speed + 10.0)
    occupancy = pool_occupancy or adaptive.unknown_pool_observation(
        str(quota.get("quota_pool_id") or "unknown"),
        reason="no-active-occupancy-observation",
    )
    concurrency = float(occupancy.get("selection_utility", 50.0))
    if (
        isinstance(concurrency, bool)
        or not isinstance(concurrency, (int, float))
        or not 0.0 <= float(concurrency) <= 100.0
    ):
        concurrency = 50.0
    pace = pace if isinstance(pace, dict) else {}
    tempo = pace.get("tempo")
    confidence = pace.get("confidence")
    if (
        isinstance(tempo, bool) or not isinstance(tempo, (int, float))
        or not math.isfinite(tempo) or tempo < 0
        or isinstance(confidence, bool) or not isinstance(confidence, (int, float))
        or not math.isfinite(confidence) or not 0 <= confidence <= 1
    ):
        tempo, confidence = None, 0.0
    pace_utility = 50.0 + 50.0 * _bounded(1.0 - tempo, -1.0, 1.0) if tempo is not None else 50.0
    subscription_utility = (1.0 - confidence) * underuse_score + confidence * pace_utility
    components = {
        "capability_fit": round(capability_fit, 3),
        "quota_utility": round(quota_utility, 3),
        "speed": round(speed, 3),
        "first_pass": round(first_pass, 3),
        "verifiability": round(verifiability, 3),
        "concurrency": round(concurrency, 3),
        "subscription_utility": round(subscription_utility, 3),
        "pace_utility": round(pace_utility, 3),
    }
    risk = str(assessment.get("risk", "medium"))
    weights = HIGH_RISK_WEIGHTS if risk in {"high", "critical"} else LOW_RISK_WEIGHTS
    if policy == "quality-first":
        weights = HIGH_RISK_WEIGHTS
    base = sum(weights[key] * components[key] for key in weights)
    overhead_minutes = float(priors.get("expected_overhead_minutes", 8))
    repair_minutes = float(priors.get("expected_repair_minutes", 20))
    economics = assessment.get("delegation_economics", {})
    coordination_minutes = float(economics.get("coordination_minutes", 0))
    parent_minutes = float(economics.get("estimated_parent_minutes", 0))
    contractor_minutes = float(
        economics.get("estimated_contractor_minutes", 0)
    )
    parallelizable = _bounded(
        float(economics.get("parallelizable_fraction", 0.0)), 0.0, 1.0
    )
    net_time_loss = max(
        0.0,
        contractor_minutes
        + coordination_minutes
        - parent_minutes
        - parent_minutes * parallelizable,
    )
    uncertainty = _bounded(float(priors.get("uncertainty", 0.5)), 0.0, 1.0)
    impact = {1: 2.0, 2: 5.0, 3: 9.0, 4: 14.0}[RISK_LEVEL[risk]]
    penalties = {
        "delegation_overhead": min(
            15.0,
            overhead_minutes / 4.0
            + coordination_minutes / 5.0
            + net_time_loss / 10.0,
        ),
        "uncertainty_risk": uncertainty * impact,
        "expected_rework": min(
            15.0, (100.0 - first_pass) / 100.0 * repair_minutes / 2.0
        ),
    }
    if tempo is not None and tempo > 1.25:
        penalties["pace_overburn"] = confidence * min(35.0, 20.0 * math.log2(tempo))
    score = _bounded(base - sum(penalties.values()))
    return round(score, 3), components, {
        key: round(value, 3) for key, value in penalties.items()
    }, local_adjustments


def rank_execution_profiles(
    profiles: dict[str, dict[str, Any]],
    *,
    kind: str,
    complexity: str,
    counts: dict[str, int],
    readiness: dict[str, dict[str, Any]],
    assessment: dict[str, Any],
    quotas: dict[str, dict[str, Any]],
    records: Iterable[dict[str, Any]],
    benchmark: dict[str, Any] | None,
    mode: str,
    routing_mode: str,
    pool_observations: Any = None,
) -> list[dict[str, Any]]:
    workload_weights = dict(assessment["workload_weights"])
    quota_views = adaptive.deduplicate_shared_quota_views(quotas, profiles)
    pool_views = adaptive.normalize_pool_observations(
        pool_observations,
        quota_rows=quota_views,
        profiles=profiles,
    )
    successful_total = sum(counts.values())
    target_total = sum(
        float(profile["target_share"])
        for profile in profiles.values()
        if profile["routing_enabled"]
    )
    rows: list[dict[str, Any]] = []
    for profile_id, profile in profiles.items():
        execution = resolve_execution_profile(
            profile_id,
            profile,
            complexity=complexity,
            mode=mode,
            required_tools=assessment.get("required_tools", []),
        )
        model = str(execution["model"])
        orchestrator = assessment.get("model_scores", {}).get(profile_id, {})
        task_fit = adaptive.task_fit_evidence(
            profile,
            workload_weights=workload_weights,
            score_row=orchestrator,
        )
        orchestrator_score = float(task_fit["used_score"])
        submitted_orchestrator_score = task_fit.get("submitted_score")
        arena_score, arena_configs = adaptive.arena_percentile(
            benchmark,
            profile_id=profile_id,
            model=model,
            workload_weights=workload_weights,
        )
        capability_fit, fit_components = _capability_score(
            execution,
            profile=profile,
            workload_weights=workload_weights,
            orchestrator_score=orchestrator_score,
            arena_score=arena_score,
            submitted_orchestrator_score=(
                float(submitted_orchestrator_score)
                if isinstance(submitted_orchestrator_score, (int, float))
                and not isinstance(submitted_orchestrator_score, bool)
                else None
            ),
        )
        fit_components["arena_configs"] = arena_configs
        fit_components.update(
            {
                "orchestrator_score_submitted": task_fit["submitted_score"],
                "orchestrator_reason_submitted": task_fit["submitted_reason"],
                "task_fit_source": task_fit["source"],
                "task_fit_fallback_applied": task_fit["fallback_applied"],
                "task_fit_fallback_reason": task_fit["fallback_reason"],
                "task_fit_used": task_fit["used_score"],
            }
        )
        quota = quota_views.get(
            profile_id,
            {"status": "unknown", "headroom": None, "confidence": 0.0},
        )
        pace = quota.get("pace") if isinstance(quota.get("pace"), dict) else {
            "tempo": None, "confidence": 0.0, "source": "none", "reason": "no-active-window-evidence"
        }
        pool_id = adaptive.pool_id_for_profile(profile_id, profile, quota)
        pool_occupancy = pool_views.get(pool_id) or adaptive.unknown_pool_observation(
            pool_id,
            reason="no-active-occupancy-observation",
        )
        gates = hard_gates(
            execution,
            profile=profile,
            assessment=assessment,
            readiness=readiness.get(profile_id, {"ready": False}),
            quota=quota,
            mode=mode,
        )
        eligible = all(bool(gate["passed"]) for gate in gates)
        desired_share = (
            float(profile["target_share"]) / target_total if target_total else 0.0
        )
        actual_share = (
            counts.get(profile_id, 0) / successful_total if successful_total else 0.0
        )
        underuse_score = (
            50.0
            if successful_total == 0
            else _bounded(50.0 + (desired_share - actual_share) * 250.0)
        )
        local = local_execution_stats(
            records,
            execution_profile_id=str(execution["execution_profile_id"]),
            agent=profile_id,
            model=model,
            kind=kind,
        )
        score, components, penalties, local_adjustments = assignment_score(
            capability_fit=capability_fit,
            execution=execution,
            assessment=assessment,
            quota=quota,
            underuse_score=underuse_score,
            local=local,
            pool_occupancy=pool_occupancy,
            pace=pace,
        )
        gate_failures = [
            str(gate["code"]) for gate in gates if not bool(gate["passed"])
        ]
        baseline_score = capability_fit
        row = {
            "agent": profile_id,
            "adapter": profile["adapter"],
            "display_name": profile["display_name"],
            "available": bool(readiness.get(profile_id, {}).get("ready")),
            "routing_enabled": bool(profile["routing_enabled"]),
            "launchable": bool(readiness.get(profile_id, {}).get("ready"))
            and bool(profile["routing_enabled"]),
            "eligible": eligible,
            "fit": round(capability_fit, 3),
            "fit_components": fit_components,
            "assessment_score": task_fit["submitted_score"],
            "assessment_reason": task_fit["submitted_reason"],
            "task_fit_fallback_applied": task_fit["fallback_applied"],
            "capability_evidence": {
                "static_score": task_fit["static_score"],
                "submitted_score": task_fit["submitted_score"],
                "submitted_reason": task_fit["submitted_reason"],
                "used_score": task_fit["used_score"],
                "source": task_fit["source"],
                "fallback_applied": task_fit["fallback_applied"],
                "fallback_reason": task_fit["fallback_reason"],
            },
            "task_fit_evidence": task_fit,
            "quality_floor": None,
            "score": score,
            "baseline_score": round(baseline_score, 3),
            "assignment_score": score,
            "score_version": SCORE_VERSION,
            "assignment_components": components,
            "penalties": penalties,
            "gate_results": gates,
            "gate_failures": gate_failures,
            "resource_score": round(
                (
                    components["quota_utility"]
                    + components["subscription_utility"]
                    + components["speed"]
                )
                / 3.0,
                3,
            ),
            "resource_confidence": round(
                (
                    float(quota.get("confidence", 0.0))
                    + min(1.0, int(local["sample_count"]) / 20.0)
                )
                / 2.0,
                3,
            ),
            "resource_components": {
                "quota_pool_id": pool_id,
                "quota_headroom": quota.get("headroom"),
                "quota_observation_count": quota.get(
                    "quota_observation_count", 0
                ),
                "quota_duplicate_suppressed": quota.get(
                    "quota_duplicate_suppressed", 0
                ),
                "quota_conflict": bool(quota.get("quota_conflict", False)),
                "quota_score": components["quota_utility"],
                "underuse_score": round(underuse_score, 3),
                "pace_utility": components["pace_utility"],
                "pace": pace,
                "efficiency_score": components["speed"],
                "reliability_score": components["first_pass"],
                "local_sample_count": local["sample_count"],
                "local_signal_active": local["active"],
                "occupancy_score": components["concurrency"],
                "occupancy_status": pool_occupancy.get("status", "unknown"),
                "occupancy_active_count_source": pool_occupancy.get(
                    "active_count_source"
                ),
                "occupancy_active_count": pool_occupancy.get("active_count"),
                "occupancy_nested_active_count": pool_occupancy.get(
                    "nested_active_count"
                ),
                "occupancy_capacity": pool_occupancy.get("capacity"),
                "occupancy_visibility": pool_occupancy.get("visibility", "unknown"),
                "occupancy_visibility_gap": bool(
                    pool_occupancy.get("visibility_gap", True)
                ),
                "occupancy_observation_count": pool_occupancy.get(
                    "observation_count", 0
                ),
                "occupancy_duplicate_suppressed": pool_occupancy.get(
                    "duplicate_suppressed", 0
                ),
            },
            "quota": quota,
            "pace": pace,
            "quota_pool_id": pool_id,
            "pool_occupancy": pool_occupancy,
            "suggested_model": model,
            "selected_execution_profile": execution,
            "execution_profile_id": execution["execution_profile_id"],
            "execution_profile_fingerprint": execution[
                "execution_profile_fingerprint"
            ],
            "local_performance": local,
            "reasons": [
                f"capability_fit={components['capability_fit']}",
                f"assignment_score={score}",
                f"policy={assessment.get('allocation_policy', 'balanced')}",
                f"quota_status={quota.get('status', 'unknown')}",
                f"quota_pool={pool_id}",
                f"pace_tempo={pace.get('tempo')}",
                f"pace_confidence={pace.get('confidence')}",
                f"pace_source={pace.get('source')}",
                f"pace_reason={pace.get('reason')}",
                f"occupancy_status={pool_occupancy.get('status', 'unknown')}",
                f"occupancy_active={pool_occupancy.get('active_count')}",
                f"occupancy_visibility={pool_occupancy.get('visibility', 'unknown')}",
                *(
                    [
                        "routing-disabled="
                        + str(profile.get("routing_blocker", "unspecified"))
                    ]
                    if not profile.get("routing_enabled")
                    else []
                ),
                *(
                    [
                        "task-fit-fallback=static-fit",
                        "task-fit-original-score="
                        + str(task_fit["submitted_score"]),
                        "task-fit-original-reason="
                        + str(task_fit["submitted_reason"]),
                    ]
                    if task_fit["fallback_applied"]
                    else []
                ),
                *(
                    ["occupancy-visibility-gap"]
                    if pool_occupancy.get("visibility_gap", True)
                    else []
                ),
                *(
                    [
                        "nested-occupancy-observed="
                        + str(pool_occupancy.get("nested_active_count"))
                    ]
                    if pool_occupancy.get("nested_count_observed")
                    else []
                ),
                *(
                    [
                        "occupancy-observation-deduped="
                        + str(pool_occupancy.get("duplicate_suppressed", 0))
                    ]
                    if pool_occupancy.get("duplicate_suppressed", 0)
                    else []
                ),
                *(
                    [
                        "quota-observation-deduped="
                        + str(quota.get("quota_duplicate_suppressed", 0))
                    ]
                    if quota.get("quota_duplicate_suppressed", 0)
                    else []
                ),
                *(
                    ["quota-conflict-neutralized"]
                    if quota.get("quota_conflict")
                    else []
                ),
                *(
                    ["occupancy-conflict-neutralized"]
                    if pool_occupancy.get("observation_conflict")
                    else []
                ),
                *[f"gate-failed={item}" for item in gate_failures],
                *[f"local={item}" for item in local_adjustments],
            ],
        }
        rows.append(row)
    baseline_order = sorted(
        rows,
        key=lambda row: (
            not bool(row["eligible"]),
            -float(row["baseline_score"]),
            str(row["agent"]),
        ),
    )
    for index, row in enumerate(baseline_order, start=1):
        row["baseline_rank"] = index
    top_eligible_fit = max(
        (float(row["fit"]) for row in rows if row["eligible"]),
        default=None,
    )
    for row in rows:
        row["near_tie"] = bool(
            row["eligible"]
            and top_eligible_fit is not None
            and top_eligible_fit - float(row["fit"]) <= adaptive.TIE_BAND
        )
    selected_order = (
        baseline_order
        if routing_mode == "baseline"
        else sorted(
            rows,
            key=lambda row: (
                not bool(row["eligible"]),
                not bool(row["near_tie"]),
                -float(row["assignment_score"]),
                -float(row["fit"]),
                str(row["agent"]),
            ),
        )
    )
    for index, row in enumerate(selected_order, start=1):
        row["adaptive_rank"] = index
        row["routing_mode"] = routing_mode
    return selected_order
