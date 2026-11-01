from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import statistics
import stat
import uuid
from pathlib import Path
from typing import Any, Iterable

import mimo_runtime
import grok_usage


SCHEMA_VERSION = 1
TASK_PROFILE_V2_SCHEMA_VERSION = 2
TIE_BAND = 6.0
ORCHESTRATOR_WEIGHT = 0.25
ARENA_MAX_ADJUSTMENT = 2.5
MIN_LOCAL_SAMPLES = 5
DEFAULT_QUOTA_MAX_AGE_HOURS = 24
MAX_UNRESET_EXHAUSTION_HOURS = 6
VALID_RISKS = {"low", "medium", "high", "critical"}
VALID_DATA_CLASSIFICATIONS = {
    "unknown",
    "public",
    "internal",
    "confidential",
    "restricted",
}
VALID_DELEGATION_MODES = {"SKIP", "ONE", "COUNCIL", "PARALLEL_IMPLEMENT"}
VALID_CAPABILITIES = {
    "large-context",
    "multimodal",
    "web",
    "write",
    "independent-review",
    "security-review",
}
VALID_ALLOCATION_POLICIES = {
    "balanced",
    "burn-before-reset",
    "fast-lane",
    "parallel-exploration",
    "preserve-premium",
    "quality-first",
}
VALID_QUOTA_SOURCES = {"native-json", "provider-dashboard", "tui-assisted", "manual"}
AUTHORITATIVE_QUOTA_SOURCES = {"native-json", "provider-dashboard"}
ARENA_CONFIGS_BY_KIND = {
    "architecture": ("agent", "text_style_control"),
    "debugging": ("agent", "webdev"),
    "docs": ("document", "text_style_control"),
    "frontend": ("webdev", "vision_style_control", "agent"),
    "implementation": ("agent", "webdev"),
    "large-context": ("document", "agent"),
    "multimodal": ("vision_style_control", "document", "agent"),
    "repeatable": ("agent", "text_style_control"),
    "research": ("search_style_control", "text_style_control", "agent"),
    "review": ("agent", "text_style_control"),
}

# A model score can be about lane allocation rather than task fit.  Keep this
# matcher deliberately narrow: a sentence that mentions a missing capability,
# permission, readiness or entitlement is evidence for a real gate and must
# never be converted into a static-fit fallback.
_SELECTION_ONLY_REASON_RE = re.compile(
    r"^not selected for (?:this |the )?first wave implementation lane$"
)
_SELECTION_ONLY_DISQUALIFIERS = (
    "capabil",
    "missing",
    "lack",
    "without",
    "permission",
    "denied",
    "unauth",
    "readiness",
    "not ready",
    "lifecycle",
    "tool",
    "security",
    "risk",
    "quota",
    "entitlement",
    "unsupported",
    "cannot",
    "ineligible",
    "blocked",
    "insufficient",
    "below",
)


class AdaptiveError(RuntimeError):
    pass


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise AdaptiveError(f"Invalid timestamp: {value}") from exc
    if parsed.tzinfo is None:
        raise AdaptiveError("Timestamp must include a timezone")
    return parsed.astimezone(dt.timezone.utc)


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode(
        "ascii"
    )


def atomic_write_bytes(path: Path, payload: bytes, *, mode: int = 0o666) -> None:
    """Flush the complete temporary payload to disk before publishing it.

    A failed write/fsync leaves the previous target untouched. Temporary files
    are created exclusively in the target directory; no user state is deleted
    or moved. The default mode respects umask, as Path.write_bytes did; private
    checkpoint/incident callers retain their previous 0600 mode.
    Windows does not support directory fsync, so only the file is synced.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), mode)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def atomic_write(path: Path, value: Any) -> None:
    atomic_write_bytes(path, canonical_json(value))


def read_state_json(path: Path, *, max_bytes: int, encoding: str = "utf-8") -> tuple[Any, str | None]:
    """Read bounded, non-linked state without exposing invalid file contents."""
    try:
        metadata = path.lstat()
        if (path.is_symlink() or not stat.S_ISREG(metadata.st_mode)
                or getattr(metadata, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024)
                or metadata.st_size > max_bytes):
            return None, "corrupt-cache-ignored"
        with path.open("rb") as stream:
            data = stream.read(min(max_bytes, metadata.st_size) + 1)
        if len(data) > max_bytes or len(data) > metadata.st_size:
            return None, "corrupt-cache-ignored"
        return json.loads(data.decode(encoding)), None
    except FileNotFoundError:
        return None, "missing"
    except (OSError, ValueError, RecursionError):
        return None, "corrupt-cache-ignored"


class StateRecords(list):
    """List-compatible evidence with aggregate diagnostics for ignored files."""
    def __init__(self):
        super().__init__()
        self.ignored_count = 0

    def ignore(self, reason: str | None = None) -> None:
        if reason != "missing":
            self.ignored_count += 1


def load_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdaptiveError(f"Cannot load {label}: {path}: {exc}") from exc


def integer_score(value: Any, label: str, *, multiple: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
        raise AdaptiveError(f"{label} must be an integer between 0 and 100")
    if multiple and value % multiple:
        raise AdaptiveError(f"{label} must be a multiple of {multiple}")
    return value


def _validate_data_handling(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, dict) or set(value) != {
        "classification",
        "anthropic_zdr_authorization",
    }:
        raise AdaptiveError("Task assessment data_handling is invalid")
    if value["classification"] not in VALID_DATA_CLASSIFICATIONS:
        raise AdaptiveError("Task assessment data_handling.classification is invalid")
    authorization = value["anthropic_zdr_authorization"]
    if authorization is None:
        return
    required = {
        "status",
        "scope",
        "models",
        "verified_at",
        "expires_at",
        "evidence_ref",
    }
    if not isinstance(authorization, dict) or set(authorization) != required:
        raise AdaptiveError(
            "Task assessment data_handling.anthropic_zdr_authorization is invalid"
        )
    if authorization["status"] != "authorized" or authorization["scope"] != "account":
        raise AdaptiveError("Anthropic ZDR authorization must be account-authorized")
    models = authorization["models"]
    if (
        not isinstance(models, list)
        or not models
        or len(models) != len(set(models))
        or any(not isinstance(item, str) or not item.strip() for item in models)
    ):
        raise AdaptiveError("Anthropic ZDR authorization models are invalid")
    verified_at = parse_timestamp(str(authorization["verified_at"]))
    expires_at = parse_timestamp(str(authorization["expires_at"]))
    if expires_at <= verified_at:
        raise AdaptiveError("Anthropic ZDR authorization expiry is invalid")
    if not isinstance(authorization["evidence_ref"], str) or not authorization[
        "evidence_ref"
    ].strip():
        raise AdaptiveError("Anthropic ZDR authorization evidence_ref is required")


def _validate_required_tools(value: Any, *, label: str) -> None:
    if (
        not isinstance(value, list)
        or len(value) != len(set(value))
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        raise AdaptiveError(f"{label} required_tools are invalid")


def _validate_red_team_intent(value: Any, *, label: str) -> None:
    if value is None:
        return
    if (
        not isinstance(value, dict)
        or set(value) != {"mode", "explicit"}
        or value.get("mode") != "red-team"
        or value.get("explicit") is not True
    ):
        raise AdaptiveError(f"{label} red_team_intent is invalid")


def validate_assessment(
    value: Any,
    *,
    profile_ids: Iterable[str],
    valid_kinds: set[str],
    valid_complexities: set[str],
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AdaptiveError("Task assessment must be an object")
    if value.get("schema_version") == TASK_PROFILE_V2_SCHEMA_VERSION:
        return validate_task_profile_v2(
            value,
            profile_ids=profile_ids,
            valid_kinds=valid_kinds,
            valid_complexities=valid_complexities,
        )
    if value.get("schema_version") != SCHEMA_VERSION:
        raise AdaptiveError("Task assessment schema_version must be 1 or 2")
    required = {
        "schema_version",
        "task_summary",
        "substantive",
        "delegation_value",
        "delegation_mode",
        "complexity",
        "risk",
        "workload_weights",
        "required_capabilities",
        "required_tools",
        "red_team_intent",
        "model_scores",
        "preflight",
    }
    optional = {"data_handling"}
    if not required.issubset(value) or set(value) - optional != required:
        raise AdaptiveError("Task assessment fields do not match the schema")
    if not isinstance(value["task_summary"], str) or not value["task_summary"].strip():
        raise AdaptiveError("Task assessment task_summary is required")
    if not isinstance(value["substantive"], bool):
        raise AdaptiveError("Task assessment substantive must be boolean")
    integer_score(value["delegation_value"], "delegation_value")
    if value["delegation_mode"] not in VALID_DELEGATION_MODES:
        raise AdaptiveError("Task assessment delegation_mode is invalid")
    if value["complexity"] not in valid_complexities:
        raise AdaptiveError("Task assessment complexity is invalid")
    if value["risk"] not in VALID_RISKS:
        raise AdaptiveError("Task assessment risk is invalid")
    weights = value["workload_weights"]
    if not isinstance(weights, dict) or not weights or not set(weights).issubset(valid_kinds):
        raise AdaptiveError("Task assessment workload_weights are invalid")
    for kind, weight in weights.items():
        integer_score(weight, f"workload_weights.{kind}")
    if sum(weights.values()) != 100:
        raise AdaptiveError("Task assessment workload_weights must sum to 100")
    capabilities = value["required_capabilities"]
    if (
        not isinstance(capabilities, list)
        or len(capabilities) != len(set(capabilities))
        or not set(capabilities).issubset(VALID_CAPABILITIES)
    ):
        raise AdaptiveError("Task assessment required_capabilities are invalid")
    _validate_required_tools(value["required_tools"], label="Task assessment")
    _validate_red_team_intent(value["red_team_intent"], label="Task assessment")
    model_scores = value["model_scores"]
    expected_profiles = set(profile_ids)
    if not isinstance(model_scores, dict) or set(model_scores) != expected_profiles:
        raise AdaptiveError(model_scores_mismatch_error(model_scores, profile_ids))
    for profile_id, score_row in model_scores.items():
        if not isinstance(score_row, dict) or set(score_row) != {"score", "reason"}:
            raise AdaptiveError(f"Task assessment model_scores.{profile_id} is invalid")
        integer_score(score_row["score"], f"model_scores.{profile_id}.score", multiple=5)
        if not isinstance(score_row["reason"], str) or not score_row["reason"].strip():
            raise AdaptiveError(f"Task assessment model_scores.{profile_id}.reason is required")
    preflight = value["preflight"]
    if not isinstance(preflight, dict) or set(preflight) != {"skill", "status", "reason"}:
        raise AdaptiveError("Task assessment preflight is invalid")
    if preflight["skill"] not in {"new-task", "new-training", "none"}:
        raise AdaptiveError("Task assessment preflight skill is invalid")
    if preflight["status"] not in {"applied", "not-applicable"}:
        raise AdaptiveError("Task assessment preflight status is invalid")
    if not isinstance(preflight["reason"], str) or not preflight["reason"].strip():
        raise AdaptiveError("Task assessment preflight reason is required")
    if value["substantive"] and preflight["skill"] == "none":
        raise AdaptiveError("Substantive task assessment must apply a preflight skill")
    if value["delegation_mode"] == "SKIP" and value["delegation_value"] >= 50:
        raise AdaptiveError("SKIP conflicts with delegation_value >= 50")
    if value["delegation_mode"] != "SKIP" and value["delegation_value"] < 40:
        raise AdaptiveError("Delegation requires delegation_value >= 40")
    _validate_data_handling(value.get("data_handling"))
    return dict(value)


def _one_to_five(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5:
        raise AdaptiveError(f"{label} must be an integer between 1 and 5")
    return value


def model_scores_mismatch_error(value: Any, profile_ids: Iterable[str]) -> str:
    expected_profiles = {str(item) for item in profile_ids}
    actual = set(value) if isinstance(value, dict) else set()
    missing = sorted(expected_profiles - actual)
    extra = sorted(actual - expected_profiles)
    return (
        "Task assessment must score every configured profile exactly once; "
        f"missing configured profile IDs: {missing or '[]'}; "
        f"extra unknown profile IDs: {extra or '[]'}"
    )


def _validate_model_scores(
    value: Any,
    *,
    profile_ids: Iterable[str],
) -> None:
    expected_profiles = set(profile_ids)
    if not isinstance(value, dict) or set(value) != expected_profiles:
        raise AdaptiveError(model_scores_mismatch_error(value, profile_ids))
    for profile_id, score_row in value.items():
        if not isinstance(score_row, dict) or set(score_row) != {"score", "reason"}:
            raise AdaptiveError(f"Task assessment model_scores.{profile_id} is invalid")
        integer_score(score_row["score"], f"model_scores.{profile_id}.score", multiple=5)
        if not isinstance(score_row["reason"], str) or not score_row["reason"].strip():
            raise AdaptiveError(f"Task assessment model_scores.{profile_id}.reason is required")


def _validate_preflight(value: Any, *, substantive: bool) -> None:
    if not isinstance(value, dict) or set(value) != {"skill", "status", "reason"}:
        raise AdaptiveError("Task assessment preflight is invalid")
    if value["skill"] not in {"new-task", "new-training", "none"}:
        raise AdaptiveError("Task assessment preflight skill is invalid")
    if value["status"] not in {"applied", "not-applicable"}:
        raise AdaptiveError("Task assessment preflight status is invalid")
    if not isinstance(value["reason"], str) or not value["reason"].strip():
        raise AdaptiveError("Task assessment preflight reason is required")
    if substantive and value["skill"] == "none":
        raise AdaptiveError("Substantive task assessment must apply a preflight skill")


def validate_task_profile_v2(
    value: dict[str, Any],
    *,
    profile_ids: Iterable[str],
    valid_kinds: set[str],
    valid_complexities: set[str],
) -> dict[str, Any]:
    required = {
        "schema_version",
        "task_summary",
        "substantive",
        "delegation_value",
        "delegation_mode",
        "complexity",
        "complexity_score",
        "ambiguity",
        "risk",
        "verifiability",
        "visual_sensitivity",
        "context_load",
        "time_horizon",
        "urgency",
        "workload_weights",
        "required_capabilities",
        "required_tools",
        "red_team_intent",
        "minimum_capabilities",
        "verification",
        "delegation_economics",
        "allocation_policy",
        "model_scores",
        "preflight",
    }
    optional = {"data_handling"}
    if not required.issubset(value) or set(value) - optional != required:
        raise AdaptiveError("Task Profile v2 fields do not match the schema")
    if not isinstance(value["task_summary"], str) or not value["task_summary"].strip():
        raise AdaptiveError("Task Profile v2 task_summary is required")
    if not isinstance(value["substantive"], bool):
        raise AdaptiveError("Task Profile v2 substantive must be boolean")
    integer_score(value["delegation_value"], "delegation_value")
    if value["delegation_mode"] not in VALID_DELEGATION_MODES:
        raise AdaptiveError("Task Profile v2 delegation_mode is invalid")
    if value["complexity"] not in valid_complexities:
        raise AdaptiveError("Task Profile v2 complexity is invalid")
    _one_to_five(value["complexity_score"], "complexity_score")
    _one_to_five(value["ambiguity"], "ambiguity")
    if value["risk"] not in VALID_RISKS:
        raise AdaptiveError("Task Profile v2 risk is invalid")
    if value["verifiability"] not in {"low", "medium", "high"}:
        raise AdaptiveError("Task Profile v2 verifiability is invalid")
    if value["visual_sensitivity"] not in {"low", "medium", "high"}:
        raise AdaptiveError("Task Profile v2 visual_sensitivity is invalid")
    if value["time_horizon"] not in {"short", "medium", "long"}:
        raise AdaptiveError("Task Profile v2 time_horizon is invalid")
    if value["urgency"] not in {"low", "medium", "high"}:
        raise AdaptiveError("Task Profile v2 urgency is invalid")
    if value["allocation_policy"] not in VALID_ALLOCATION_POLICIES:
        raise AdaptiveError("Task Profile v2 allocation_policy is invalid")
    weights = value["workload_weights"]
    if not isinstance(weights, dict) or not weights or not set(weights).issubset(valid_kinds):
        raise AdaptiveError("Task Profile v2 workload_weights are invalid")
    for kind, weight in weights.items():
        integer_score(weight, f"workload_weights.{kind}")
    if sum(weights.values()) != 100:
        raise AdaptiveError("Task Profile v2 workload_weights must sum to 100")
    capabilities = value["required_capabilities"]
    if (
        not isinstance(capabilities, list)
        or len(capabilities) != len(set(capabilities))
        or not set(capabilities).issubset(VALID_CAPABILITIES)
    ):
        raise AdaptiveError("Task Profile v2 required_capabilities are invalid")
    _validate_required_tools(value["required_tools"], label="Task Profile v2")
    _validate_red_team_intent(value["red_team_intent"], label="Task Profile v2")
    minimums = value["minimum_capabilities"]
    if not isinstance(minimums, dict):
        raise AdaptiveError("Task Profile v2 minimum_capabilities are invalid")
    for capability, score in minimums.items():
        if not isinstance(capability, str) or not capability.strip():
            raise AdaptiveError("Task Profile v2 capability name is invalid")
        integer_score(score, f"minimum_capabilities.{capability}")
    context = value["context_load"]
    if not isinstance(context, dict) or set(context) != {
        "estimated_tokens",
        "file_count",
        "repository_scope",
    }:
        raise AdaptiveError("Task Profile v2 context_load is invalid")
    for key in ("estimated_tokens", "file_count"):
        raw = context[key]
        if raw is not None and (
            isinstance(raw, bool) or not isinstance(raw, int) or raw < 0
        ):
            raise AdaptiveError(f"Task Profile v2 context_load.{key} is invalid")
    if context["repository_scope"] not in {"single-file", "module", "repository", "multi-repository"}:
        raise AdaptiveError("Task Profile v2 context_load.repository_scope is invalid")
    verification = value["verification"]
    if not isinstance(verification, dict) or set(verification) != {
        "automatic_tests",
        "independent_review",
        "human_review",
    }:
        raise AdaptiveError("Task Profile v2 verification is invalid")
    if any(not isinstance(item, bool) for item in verification.values()):
        raise AdaptiveError("Task Profile v2 verification values must be boolean")
    economics = value["delegation_economics"]
    if not isinstance(economics, dict) or set(economics) != {
        "estimated_parent_minutes",
        "estimated_contractor_minutes",
        "coordination_minutes",
        "parallelizable_fraction",
    }:
        raise AdaptiveError("Task Profile v2 delegation_economics is invalid")
    for key in (
        "estimated_parent_minutes",
        "estimated_contractor_minutes",
        "coordination_minutes",
    ):
        raw = economics[key]
        if (
            isinstance(raw, bool)
            or not isinstance(raw, (int, float))
            or raw < 0
        ):
            raise AdaptiveError(f"Task Profile v2 delegation_economics.{key} is invalid")
    fraction = economics["parallelizable_fraction"]
    if (
        isinstance(fraction, bool)
        or not isinstance(fraction, (int, float))
        or not 0 <= fraction <= 1
    ):
        raise AdaptiveError(
            "Task Profile v2 delegation_economics.parallelizable_fraction is invalid"
        )
    _validate_model_scores(value["model_scores"], profile_ids=profile_ids)
    _validate_preflight(value["preflight"], substantive=value["substantive"])
    _validate_data_handling(value.get("data_handling"))
    if value["delegation_mode"] == "SKIP" and value["delegation_value"] >= 50:
        raise AdaptiveError("SKIP conflicts with delegation_value >= 50")
    if value["delegation_mode"] != "SKIP" and value["delegation_value"] < 40:
        raise AdaptiveError("Delegation requires delegation_value >= 40")
    return dict(value)


def normalize_task_profile(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("schema_version") == TASK_PROFILE_V2_SCHEMA_VERSION:
        return dict(value)
    workload = value.get("workload_weights", {})
    visual = "high" if "frontend" in workload or "multimodal" in workload else "low"
    required = list(value.get("required_capabilities", []))
    minimums = {
        capability: 50
        for capability in required
        if capability in {
            "independent-review",
            "multimodal",
            "security-review",
            "web",
            "write",
        }
    }
    complexity = str(value.get("complexity", "medium"))
    return {
        **value,
        "source_schema_version": value.get("schema_version", 1),
        "complexity_score": {"low": 2, "medium": 3, "high": 5}[complexity],
        "ambiguity": 3,
        "verifiability": "medium",
        "visual_sensitivity": visual,
        "context_load": {
            "estimated_tokens": None,
            "file_count": None,
            "repository_scope": "repository",
        },
        "time_horizon": "medium",
        "urgency": "medium",
        "required_tools": list(value.get("required_tools", [])),
        "red_team_intent": value.get("red_team_intent"),
        "minimum_capabilities": minimums,
        "verification": {
            "automatic_tests": any(
                kind in workload for kind in ("debugging", "implementation", "frontend")
            ),
            "independent_review": "independent-review" in required,
            "human_review": value.get("risk") in {"high", "critical"},
        },
        "delegation_economics": {
            "estimated_parent_minutes": 30,
            "estimated_contractor_minutes": 30,
            "coordination_minutes": 8,
            "parallelizable_fraction": 0.5,
        },
        "allocation_policy": (
            "quality-first"
            if value.get("risk") in {"high", "critical"}
            else "balanced"
        ),
    }


def load_assessment(
    path: Path,
    *,
    profile_ids: Iterable[str],
    valid_kinds: set[str],
    valid_complexities: set[str],
) -> dict[str, Any]:
    return validate_assessment(
        load_json(path, "task assessment"),
        profile_ids=profile_ids,
        valid_kinds=valid_kinds,
        valid_complexities=valid_complexities,
    )


def fallback_assessment(
    *,
    kind: str,
    complexity: str,
    profiles: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "task_summary": "Deterministic fallback assessment; no orchestrator assessment supplied.",
        "substantive": False,
        "delegation_value": 50,
        "delegation_mode": "ONE",
        "complexity": complexity,
        "risk": "medium",
        "data_handling": {
            "classification": "unknown",
            "anthropic_zdr_authorization": None,
        },
        "workload_weights": {kind: 100},
        "required_capabilities": [],
        "required_tools": [],
        "red_team_intent": None,
        "model_scores": {
            profile_id: {
                "score": int(round(int(profile["fit"][kind]) / 5.0) * 5),
                "reason": "Fallback to the static capability profile.",
            }
            for profile_id, profile in profiles.items()
        },
        "preflight": {
            "skill": "none",
            "status": "not-applicable",
            "reason": "Fallback assessment is used only for backward-compatible direct CLI calls.",
        },
        "assessment_source": "fallback",
    }


def static_fit(profile: dict[str, Any], workload_weights: dict[str, int]) -> float:
    return round(
        sum(float(profile["fit"][kind]) * weight for kind, weight in workload_weights.items())
        / 100.0,
        3,
    )


def is_selection_only_score_reason(reason: Any) -> bool:
    """Return whether a score explanation is only a lane-selection statement.

    This is intentionally an exact, normalized match for the first-wave lane
    wording.  It does not treat arbitrary "not selected" prose as a task-fit
    explanation because model capability and permission denials remain hard
    evidence elsewhere in routing.
    """
    if not isinstance(reason, str) or not reason.strip():
        return False
    normalized = reason.casefold().replace("\u2010", "-")
    normalized = normalized.replace("\u2011", "-").replace("\u2012", "-")
    normalized = normalized.replace("\u2013", "-").replace("\u2014", "-")
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized).strip()
    if any(marker in normalized for marker in _SELECTION_ONLY_DISQUALIFIERS):
        return False
    return bool(_SELECTION_ONLY_REASON_RE.fullmatch(normalized))


def task_fit_evidence(
    profile: dict[str, Any],
    *,
    workload_weights: dict[str, int],
    score_row: Any,
) -> dict[str, Any]:
    """Resolve task-fit evidence without replacing the submitted score.

    Assessment scores remain immutable evidence.  When the assessment only
    says that a profile was not selected for the first-wave lane, use the
    profile's static fit for the actual fit calculation and expose both values
    to the caller.  All other score reasons remain authoritative input.
    """
    static_score = static_fit(profile, workload_weights)
    submitted_score = None
    submitted_reason = None
    if isinstance(score_row, dict):
        raw_score = score_row.get("score")
        if (
            isinstance(raw_score, (int, float))
            and not isinstance(raw_score, bool)
            and 0.0 <= float(raw_score) <= 100.0
        ):
            submitted_score = raw_score
        if isinstance(score_row.get("reason"), str):
            submitted_reason = score_row["reason"]

    fallback_applied = is_selection_only_score_reason(submitted_reason)
    if fallback_applied:
        used_score = static_score
        source = "static-fit-fallback"
        fallback_reason = "selection-only-score-explanation"
    elif submitted_score is None:
        used_score = float(round(static_score / 5.0) * 5)
        source = "static-fit-default"
        fallback_reason = None
    else:
        used_score = float(submitted_score)
        source = "assessment-score"
        fallback_reason = None
    return {
        "static_score": static_score,
        "submitted_score": submitted_score,
        "submitted_reason": submitted_reason,
        "used_score": round(used_score, 3),
        "source": source,
        "fallback_applied": fallback_applied,
        "fallback_reason": fallback_reason,
        # Stable aliases make the preservation contract explicit in route
        # diagnostics without changing the original assessment object.
        "original_score": submitted_score,
        "original_reason": submitted_reason,
    }


def load_benchmark(path: str | Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        return None
    try:
        value = json.loads(resolved.read_text(encoding="ascii"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) and value.get("schema_version") == 1 else None


def arena_percentile(
    benchmark: dict[str, Any] | None,
    *,
    profile_id: str,
    model: str,
    workload_weights: dict[str, int],
) -> tuple[float | None, list[str]]:
    if not benchmark:
        return None, []
    entry = benchmark.get("models", {}).get(f"{profile_id}@{model}")
    if not isinstance(entry, dict) or entry.get("alias_confidence") != "verified":
        return None, []
    metrics = entry.get("benchmarks")
    if not isinstance(metrics, dict):
        return None, []
    weighted: list[tuple[float, float, str]] = []
    for kind, task_weight in workload_weights.items():
        configs = ARENA_CONFIGS_BY_KIND.get(kind, ())
        for priority, config in enumerate(configs):
            metric = metrics.get(config)
            percentile = metric.get("percentile") if isinstance(metric, dict) else None
            if isinstance(percentile, (int, float)):
                source_weight = float(task_weight) / float(priority + 1)
                weighted.append((float(percentile), source_weight, config))
                break
    if not weighted:
        return None, []
    total_weight = sum(item[1] for item in weighted)
    value = sum(item[0] * item[1] for item in weighted) / total_weight
    return round(value, 3), sorted({item[2] for item in weighted})


def blended_fit(
    *,
    static_score: float,
    orchestrator_score: int,
    arena_score: float | None,
) -> tuple[float, float]:
    base = (1.0 - ORCHESTRATOR_WEIGHT) * static_score + ORCHESTRATOR_WEIGHT * orchestrator_score
    adjustment = 0.0
    if arena_score is not None:
        adjustment = max(
            -ARENA_MAX_ADJUSTMENT,
            min(ARENA_MAX_ADJUSTMENT, (arena_score - 50.0) / 20.0),
        )
    return round(max(0.0, min(100.0, base + adjustment)), 3), round(adjustment, 3)


def quota_dir(root: Path) -> Path:
    return root / "quota"


def record_quota(
    root: Path,
    *,
    agent: str,
    short_remaining_fraction: float | None,
    weekly_remaining_fraction: float | None,
    credits_remaining: float | None,
    short_reset_at: str | None,
    weekly_reset_at: str | None,
    observed_at: str | None,
    source: str,
    confidence: float,
    note: str,
    valid_for_hours: int,
    quota_pool_id: str | None = None,
    monthly_remaining_fraction: float | None = None,
    monthly_reset_at: str | None = None,
) -> tuple[dict[str, Any], Path]:
    if source not in VALID_QUOTA_SOURCES:
        raise AdaptiveError("Quota source is invalid")
    if not 0.0 <= confidence <= 1.0:
        raise AdaptiveError("Quota confidence must be between 0 and 1")
    if valid_for_hours < 1 or valid_for_hours > 168:
        raise AdaptiveError("Quota valid_for_hours must be between 1 and 168")
    values = (short_remaining_fraction, weekly_remaining_fraction, monthly_remaining_fraction)
    if any(value is not None and not 0.0 <= value <= 1.0 for value in values):
        raise AdaptiveError("Quota remaining fractions must be between 0 and 1")
    if credits_remaining is not None and not math.isfinite(credits_remaining):
        raise AdaptiveError("Quota credits_remaining must be a finite number")
    if credits_remaining is not None and credits_remaining < 0:
        raise AdaptiveError("Quota credits_remaining cannot be negative")
    if all(value is None for value in (*values, credits_remaining)):
        raise AdaptiveError("Quota snapshot needs at least one remaining value")
    timestamp = observed_at or utc_now()
    parse_timestamp(timestamp)
    if short_reset_at:
        parse_timestamp(short_reset_at)
    if weekly_reset_at:
        parse_timestamp(weekly_reset_at)
    if monthly_reset_at:
        parse_timestamp(monthly_reset_at)
    snapshot_id = f"quota-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:10]}"
    snapshot = {
        "schema_version": 1,
        "snapshot_id": snapshot_id,
        "agent": agent,
        "quota_pool_id": quota_pool_id.strip() if isinstance(quota_pool_id, str) and quota_pool_id.strip() else agent,
        "observed_at": timestamp,
        "valid_for_hours": valid_for_hours,
        "source": source,
        "confidence": round(confidence, 3),
        "short_window_remaining_fraction": short_remaining_fraction,
        "short_window_reset_at": short_reset_at,
        "weekly_remaining_fraction": weekly_remaining_fraction,
        "weekly_reset_at": weekly_reset_at,
        "monthly_remaining_fraction": monthly_remaining_fraction,
        "monthly_reset_at": monthly_reset_at,
        "credits_remaining": credits_remaining,
        "note": note.strip(),
    }
    path = quota_dir(root) / f"{snapshot_id}.json"
    atomic_write(path, snapshot)
    return snapshot, path


def read_quota_records(root: Path) -> list[dict[str, Any]]:
    directory = quota_dir(root)
    if not directory.is_dir():
        return []
    records = StateRecords()
    for path in directory.glob("quota-*.json"):
        # Preserve this loader's read_text interface (also used by synthetic
        # store readers), while treating bad encoding/JSON as absent evidence.
        try:
            row = json.loads(path.read_text(encoding="ascii"))
        except FileNotFoundError:
            continue
        except (OSError, ValueError, RecursionError):
            records.ignore()
            continue
        try:
            if not isinstance(row, dict) or type(row.get("schema_version")) is not int or row["schema_version"] != 1:
                raise ValueError("Invalid quota schema")
            if not isinstance(row.get("agent"), str):
                raise ValueError("Invalid quota agent")
            for key in ("quota_pool_id", "source", "snapshot_id"):
                if row.get(key) is not None and not isinstance(row[key], str):
                    raise ValueError("Invalid quota field")
            parse_timestamp(str(row["observed_at"]))
            hours = row.get("valid_for_hours", DEFAULT_QUOTA_MAX_AGE_HOURS)
            if isinstance(hours, bool) or not 1 <= int(hours) <= 168:
                raise ValueError("Invalid quota lifetime")
            for key in ("confidence", "credits_remaining", "short_window_remaining_fraction",
                        "weekly_remaining_fraction", "monthly_remaining_fraction", "short_window_hours"):
                number = row.get(key)
                if number is not None and (isinstance(number, bool) or not isinstance(number, (int, float))
                        or not math.isfinite(number) or number < 0
                        or (key in {"confidence", "short_window_remaining_fraction",
                                    "weekly_remaining_fraction", "monthly_remaining_fraction"} and number > 1)):
                    raise ValueError("Invalid quota number")
            for key in ("short_window_reset_at", "weekly_reset_at", "monthly_reset_at"):
                if row.get(key) is not None:
                    if not isinstance(row[key], str):
                        raise ValueError("Invalid quota reset")
                    if row[key]:
                        parse_timestamp(row[key])
            # Validate every field consumed by latest_quotas before returning
            # the record to either routing or pace calculations.
            _quota_view(row, current=dt.datetime.now(dt.timezone.utc))
        except (KeyError, ValueError, TypeError, AttributeError, OverflowError, AdaptiveError):
            records.ignore()
            continue
        records.append(row)
    return records


def _quota_fractions(row: dict[str, Any]) -> list[float]:
    return [
        float(value)
        for value in (
            row.get("short_window_remaining_fraction"),
            row.get("weekly_remaining_fraction"),
            row.get("monthly_remaining_fraction"),
        )
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]


def _quota_expiry(
    row: dict[str, Any], observed: dt.datetime
) -> tuple[dt.datetime, float]:
    requested_hours = int(
        row.get("valid_for_hours", DEFAULT_QUOTA_MAX_AGE_HOURS)
    )
    expiry = observed + dt.timedelta(hours=requested_hours)
    fractions = _quota_fractions(row)
    if fractions and min(fractions) <= 0.0:
        exhausted_deadlines: list[dt.datetime] = []
        for value_key, reset_key in (
            ("short_window_remaining_fraction", "short_window_reset_at"),
            ("weekly_remaining_fraction", "weekly_reset_at"),
            ("monthly_remaining_fraction", "monthly_reset_at"),
        ):
            value = row.get(value_key)
            reset = row.get(reset_key)
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and float(value) <= 0.0
            ):
                exhausted_deadlines.append(
                    parse_timestamp(reset)
                    if isinstance(reset, str) and reset.strip()
                    else observed
                    + dt.timedelta(hours=MAX_UNRESET_EXHAUSTION_HOURS)
                )
        expiry = min(expiry, min(exhausted_deadlines))
    effective_hours = max(0.0, (expiry - observed).total_seconds() / 3600.0)
    return expiry, round(effective_hours, 3)


def _quota_view(
    row: dict[str, Any], *, current: dt.datetime
) -> dict[str, Any]:
    observed = parse_timestamp(str(row["observed_at"]))
    age_hours = max(0.0, (current - observed).total_seconds() / 3600.0)
    expiry, effective_hours = _quota_expiry(row, observed)
    stale = current >= expiry
    fractions = _quota_fractions(row)
    reported_headroom = min(fractions) if fractions else None
    authoritative = row.get("source") in AUTHORITATIVE_QUOTA_SOURCES
    status = "stale" if stale else "fresh"
    headroom = reported_headroom if status == "fresh" else None
    return {
        **row,
        "status": status,
        "authority": "authoritative" if authoritative else "advisory",
        "age_hours": round(age_hours, 3),
        "effective_valid_for_hours": effective_hours,
        "effective_expires_at": expiry.isoformat().replace("+00:00", "Z"),
        "reported_headroom": reported_headroom,
        "headroom": headroom,
        "hard_blocking": bool(
            authoritative
            and status == "fresh"
            and isinstance(headroom, (int, float))
            and not isinstance(headroom, bool)
            and float(headroom) <= 0.0
        ),
    }


def latest_quotas(
    root: Path,
    *,
    profile_ids: Iterable[str],
    now: dt.datetime | None = None,
    pool_ids: dict[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    current = now or dt.datetime.now(dt.timezone.utc)
    current = current.astimezone(dt.timezone.utc)
    profile_list = [str(agent) for agent in profile_ids]
    by_agent: dict[str, list[dict[str, Any]]] = {agent: [] for agent in profile_list}
    for row in read_quota_records(root):
        if row.get("agent") in by_agent:
            by_agent[str(row["agent"])].append(row)
    pool_members: dict[str, set[str]] = {}
    pool_rows: dict[str, list[dict[str, Any]]] = {}
    for agent in profile_list:
        rows = by_agent[agent]
        configured_pool = (pool_ids or {}).get(agent)
        pool_id = str(configured_pool).strip() if configured_pool else None
        if pool_id is None:
            pool_id = next(
                (
                    str(row["quota_pool_id"]).strip()
                    for row in rows
                    if isinstance(row.get("quota_pool_id"), str)
                    and row["quota_pool_id"].strip()
                ),
                agent,
            )
        pool_members.setdefault(pool_id, set()).add(agent)
        for row in rows:
            pool_rows.setdefault(pool_id, []).append(row)
    result: dict[str, dict[str, Any]] = {}
    for pool_id, members in pool_members.items():
        rows = pool_rows.get(pool_id, [])
        if not rows:
            selected = {
                "quota_pool_id": pool_id,
                "status": "unknown",
                "authority": "none",
                "headroom": None,
                "reported_headroom": None,
                "hard_blocking": False,
                "confidence": 0.0,
            }
            views: list[dict[str, Any]] = []
        else:
            views = [_quota_view(row, current=current) for row in rows]
            selected = max(views, key=_observation_priority)
            top_priority = _observation_priority(selected)
            peers = [row for row in views if _observation_priority(row) == top_priority]
            signatures = {
                (
                    row.get("status"),
                    row.get("headroom"),
                    row.get("reported_headroom"),
                    row.get("hard_blocking"),
                )
                for row in peers
            }
            if len(signatures) > 1:
                selected = {
                    **selected,
                    "status": "unknown",
                    "authority": "unknown",
                    "headroom": None,
                    "reported_headroom": None,
                    "hard_blocking": False,
                    "quota_conflict": True,
                }
            else:
                selected = {**selected, "quota_conflict": False}
        fresh_advisory = [
            row
            for row in views
            if row["status"] == "fresh" and row["authority"] == "advisory"
        ]
        selected_at = _observation_timestamp(selected)
        newer_advisory = [
            row
            for row in fresh_advisory
            if selected_at is not None
            and _observation_timestamp(row) is not None
            and _observation_timestamp(row) > selected_at
        ]
        selected = {
            **selected,
            "quota_pool_id": pool_id,
            "quota_observation_count": len(rows),
            "quota_duplicate_suppressed": max(0, len(rows) - 1),
            "ignored_newer_advisory_count": len(newer_advisory),
            "newest_ignored_advisory_at": (
                max(str(row["observed_at"]) for row in newer_advisory)
                if newer_advisory
                else None
            ),
        }
        for agent in members:
            result[agent] = {**selected, "effective_agent": agent}
    return result


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def pool_id_for_profile(
    profile_id: str,
    profile: dict[str, Any] | None = None,
    quota: dict[str, Any] | None = None,
) -> str:
    for row in (quota, profile):
        if not isinstance(row, dict):
            continue
        for key in ("quota_pool_id", "pool_id", "shared_pool_id"):
            value = row.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return str(profile_id)


def _observation_timestamp(row: dict[str, Any]) -> dt.datetime | None:
    raw = row.get("observed_at")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return parse_timestamp(raw)
    except AdaptiveError:
        return None


def _observation_priority(row: dict[str, Any]) -> tuple[int, int, dt.datetime]:
    status = str(row.get("status", "unknown")).casefold()
    status_rank = {"unknown": 0, "stale": 1, "fresh": 2}.get(status, 0)
    authority = str(row.get("authority", "unknown")).casefold()
    authority_rank = {"unknown": 0, "advisory": 1, "authoritative": 2}.get(
        authority, 0
    )
    timestamp = _observation_timestamp(row) or dt.datetime.min.replace(
        tzinfo=dt.timezone.utc
    )
    return status_rank, authority_rank, timestamp


def _iter_pool_rows(value: Any) -> Iterable[tuple[str | None, dict[str, Any]]]:
    if isinstance(value, dict):
        if any(
            key in value
            for key in (
                "pool_id",
                "quota_pool_id",
                "status",
                "active_count",
                "active_occupancy",
                "active_runs",
                "capacity",
            )
        ):
            yield None, dict(value)
            return
        for key, row in value.items():
            if isinstance(row, dict):
                yield (str(key) if key is not None else None), dict(row)
        return
    if isinstance(value, (list, tuple)):
        for row in value:
            if isinstance(row, dict):
                yield None, dict(row)


def _active_runs_count(value: Any) -> int | None:
    if isinstance(value, dict):
        value = list(value.values())
    if not isinstance(value, list):
        return None
    identifiers: set[str] = set()
    count = 0
    for item in value:
        if isinstance(item, dict):
            identity = next(
                (
                    item.get(key)
                    for key in ("run_id", "root_run_id", "worker_id", "id")
                    if item.get(key) is not None
                ),
                None,
            )
            if identity is not None:
                identity_text = str(identity)
                if identity_text in identifiers:
                    continue
                identifiers.add(identity_text)
        count += 1
    return count


def _normalize_pool_observation(
    value: dict[str, Any], *, pool_id: str
) -> dict[str, Any]:
    raw_status = str(value.get("status", "unknown")).casefold()
    status = raw_status if raw_status in {"fresh", "stale", "unknown"} else "unknown"
    authority = str(value.get("authority", "unknown")).casefold()
    if authority not in {"authoritative", "advisory", "unknown"}:
        authority = "unknown"
    active_count = None
    active_count_source = None
    for key in (
        "active_count",
        "observed_active_count",
        "concurrent_active",
        "active_occupancy",
        "observed_concurrent_count",
    ):
        candidate = _nonnegative_int(value.get(key))
        if candidate is not None:
            active_count = candidate
            active_count_source = key
            break
    active_runs_count = _active_runs_count(value.get("active_runs"))
    if active_count is None and active_runs_count is not None:
        active_count = active_runs_count
        active_count_source = "active_runs"
    nested_active_count = _nonnegative_int(value.get("nested_active_count"))
    if active_count is None and nested_active_count is not None:
        active_count = nested_active_count
        active_count_source = "nested_active_count"
    active_count_conflict = bool(
        active_count is not None
        and active_runs_count is not None
        and active_count_source != "active_runs"
        and active_count != active_runs_count
    )
    capacity = None
    capacity_source = None
    for key in (
        "capacity",
        "max_concurrent",
        "max_concurrency",
        "concurrent_capacity",
        "slot_capacity",
    ):
        candidate = _nonnegative_int(value.get(key))
        if candidate is not None:
            capacity = candidate
            capacity_source = key
            break
    raw_visibility = str(value.get("visibility", "unknown")).casefold()
    visibility = {
        "full": "complete",
        "all": "complete",
        "complete": "complete",
        "partial": "partial",
        "incomplete": "partial",
    }.get(raw_visibility, "unknown")
    timestamp = value.get("observed_at") if isinstance(value.get("observed_at"), str) else None
    selection_utility = 50.0
    utility_basis = "unknown-or-stale-observation"
    usable = (
        status == "fresh"
        and active_count is not None
        and capacity is not None
        and capacity > 0
        and not active_count_conflict
    )
    # A zero count with incomplete visibility does not prove that the shared
    # pool is idle.  Known active work remains useful as a lower-bound signal.
    if usable and not (active_count == 0 and visibility != "complete"):
        selection_utility = round(
            max(0.0, min(100.0, 100.0 * (capacity - active_count) / capacity)),
            3,
        )
        utility_basis = "fresh-observed-concurrency"
    elif status == "fresh" and active_count is not None and capacity is None:
        utility_basis = "fresh-active-count-without-capacity"
    elif status == "fresh" and active_count is None:
        utility_basis = "fresh-observation-without-active-count"
    elif status == "fresh" and active_count_conflict:
        utility_basis = "conflicting-active-counts"
    return {
        "pool_id": pool_id,
        "status": status,
        "authority": authority,
        "observed_at": timestamp,
        "active_count": active_count,
        "active_count_source": active_count_source,
        "active_runs_count": active_runs_count,
        "nested_active_count": nested_active_count,
        "nested_count_observed": nested_active_count is not None,
        "active_count_conflict": active_count_conflict,
        "capacity": capacity,
        "capacity_source": capacity_source,
        "visibility": visibility,
        "visibility_gap": visibility != "complete",
        "selection_utility": selection_utility,
        "utility_basis": utility_basis,
        "observation_source": value.get("source"),
    }


def unknown_pool_observation(pool_id: str, *, reason: str = "no-observation") -> dict[str, Any]:
    return {
        "pool_id": str(pool_id),
        "status": "unknown",
        "authority": "unknown",
        "observed_at": None,
        "active_count": None,
        "active_count_source": None,
        "active_runs_count": None,
        "nested_active_count": None,
        "nested_count_observed": False,
        "active_count_conflict": False,
        "capacity": None,
        "capacity_source": None,
        "visibility": "unknown",
        "visibility_gap": True,
        "selection_utility": 50.0,
        "utility_basis": reason,
        "observation_source": None,
        "observation_count": 0,
        "duplicate_suppressed": 0,
        "observation_conflict": False,
    }


def _embedded_pool_observation(
    row: dict[str, Any], *, pool_id: str
) -> dict[str, Any] | None:
    for key in (
        "pool_observation",
        "pool_occupancy",
        "occupancy",
        "active_occupancy",
        "concurrent_occupancy",
    ):
        nested = row.get(key)
        if isinstance(nested, dict):
            result = dict(nested)
            result.setdefault("pool_id", pool_id)
            return result
    occupancy_keys = {
        "active_count",
        "observed_active_count",
        "concurrent_active",
        "active_occupancy",
        "observed_concurrent_count",
        "active_runs",
        "nested_active_count",
        "capacity",
        "max_concurrent",
        "max_concurrency",
        "concurrent_capacity",
        "slot_capacity",
    }
    if occupancy_keys.intersection(row):
        result = {key: row[key] for key in occupancy_keys if key in row}
        for key in ("status", "authority", "observed_at", "source", "visibility"):
            if key in row:
                result[key] = row[key]
        result["pool_id"] = pool_id
        return result
    return None


def normalize_pool_observations(
    observations: Any = None,
    *,
    quota_rows: dict[str, dict[str, Any]] | None = None,
    profiles: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Deduplicate explicit active-run observations by shared pool.

    Freshness is accepted only when the source explicitly says ``fresh``.
    Quota rows are inspected only for an embedded occupancy observation; token
    usage, completed counts and headroom never become active occupancy.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}

    def add(pool_hint: str | None, row: dict[str, Any]) -> None:
        pool_value = row.get("pool_id") or row.get("quota_pool_id") or pool_hint
        if not isinstance(pool_value, str) or not pool_value.strip():
            return
        pool_id = pool_value.strip()
        row.setdefault("pool_id", pool_id)
        grouped.setdefault(pool_id, []).append(row)

    for pool_hint, row in _iter_pool_rows(observations):
        if (
            pool_hint in (profiles or {})
            and not row.get("pool_id")
            and not row.get("quota_pool_id")
        ):
            pool_hint = pool_id_for_profile(
                str(pool_hint), (profiles or {}).get(str(pool_hint), {})
            )
        add(pool_hint, row)
    for agent, quota in (quota_rows or {}).items():
        if not isinstance(quota, dict):
            continue
        profile = (profiles or {}).get(agent, {})
        pool_id = pool_id_for_profile(agent, profile, quota)
        embedded = _embedded_pool_observation(quota, pool_id=pool_id)
        if embedded is not None:
            add(pool_id, embedded)
    for agent, profile in (profiles or {}).items():
        if not isinstance(profile, dict):
            continue
        pool_id = pool_id_for_profile(agent, profile)
        embedded = _embedded_pool_observation(profile, pool_id=pool_id)
        if embedded is not None:
            add(pool_id, embedded)

    result: dict[str, dict[str, Any]] = {}
    for pool_id, rows in grouped.items():
        normalized = [
            _normalize_pool_observation(row, pool_id=pool_id) for row in rows
        ]
        selected = max(normalized, key=_observation_priority)
        top_priority = _observation_priority(selected)
        peers = [row for row in normalized if _observation_priority(row) == top_priority]
        signatures = {
            (row.get("active_count"), row.get("capacity"), row.get("visibility"))
            for row in peers
        }
        conflict = len(signatures) > 1
        if conflict:
            selected = {
                **selected,
                "status": "unknown",
                "authority": "unknown",
                "selection_utility": 50.0,
                "utility_basis": "conflicting-shared-pool-observations",
                "observation_conflict": True,
            }
        else:
            selected = {**selected, "observation_conflict": False}
        selected["observation_count"] = len(rows)
        selected["duplicate_suppressed"] = max(0, len(rows) - 1)
        selected["visibility_gap"] = bool(selected.get("visibility_gap", True))
        result[pool_id] = selected
    return result


def deduplicate_shared_quota_views(
    quotas: dict[str, dict[str, Any]] | None,
    profiles: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Reuse one current quota observation for every member of a shared pool."""
    rows = quotas if isinstance(quotas, dict) else {}
    profiles = profiles or {}
    groups: dict[str, list[str]] = {}
    all_agents = set(profiles) | set(rows)
    for agent in all_agents:
        profile = profiles.get(agent, {})
        quota = rows.get(agent)
        pool_id = pool_id_for_profile(agent, profile, quota)
        groups.setdefault(pool_id, []).append(agent)
    result: dict[str, dict[str, Any]] = {}
    for pool_id, agents in groups.items():
        candidates = [rows[agent] for agent in agents if isinstance(rows.get(agent), dict)]
        if candidates:
            selected = max(candidates, key=_observation_priority)
            top_priority = _observation_priority(selected)
            peers = [row for row in candidates if _observation_priority(row) == top_priority]
            signatures = {
                (
                    row.get("status"),
                    row.get("headroom"),
                    row.get("reported_headroom"),
                    row.get("hard_blocking"),
                )
                for row in peers
            }
            conflict = len(signatures) > 1
            if conflict:
                selected = {
                    **selected,
                    "status": "unknown",
                    "authority": "unknown",
                    "headroom": None,
                    "reported_headroom": None,
                    "hard_blocking": False,
                    "quota_conflict": True,
                }
            else:
                selected = {**selected, "quota_conflict": False}
        else:
            selected = {
                "status": "unknown",
                "authority": "unknown",
                "headroom": None,
                "reported_headroom": None,
                "hard_blocking": False,
                "confidence": 0.0,
                "quota_conflict": False,
            }
        selected = {
            **selected,
            "quota_pool_id": pool_id,
            "quota_observation_count": len(candidates),
            "quota_duplicate_suppressed": max(0, len(candidates) - 1),
        }
        for agent in agents:
            result[agent] = {**selected, "effective_agent": agent}
    return result


def performance_stats(
    records: Iterable[dict[str, Any]],
    *,
    kind: str,
    profile_ids: Iterable[str],
    profile_models: dict[str, str] | None = None,
    min_samples: int = MIN_LOCAL_SAMPLES,
) -> dict[str, dict[str, Any]]:
    rows = {profile_id: [] for profile_id in profile_ids}
    for record in records:
        evaluation = record.get("evaluation")
        agent = str(record.get("agent", ""))
        expected_model = (profile_models or {}).get(agent)
        model_matches = expected_model is None or record.get("model") == expected_model
        if (
            record.get("kind") == kind
            and agent in rows
            and model_matches
            and isinstance(evaluation, dict)
        ):
            rows[agent].append((record, evaluation))
    result: dict[str, dict[str, Any]] = {}
    medians: dict[str, float] = {}
    for agent, samples in rows.items():
        passes = [bool(evaluation.get("criteria_pass")) for _, evaluation in samples]
        qualities = [
            float(evaluation["quality_score"])
            for _, evaluation in samples
            if isinstance(evaluation.get("quality_score"), (int, float))
        ]
        tokens = [
            float(record["token_usage"]["total_tokens"])
            for record, evaluation in samples
            if evaluation.get("criteria_pass") is True
            and isinstance(record.get("token_usage"), dict)
            and isinstance(record["token_usage"].get("total_tokens"), (int, float))
            and record["token_usage"]["total_tokens"] > 0
        ]
        active = len(samples) >= min_samples
        if active and tokens:
            medians[agent] = statistics.median(tokens)
        result[agent] = {
            "sample_count": len(samples),
            "active": active,
            "quality_mean": round(statistics.mean(qualities), 3) if qualities else None,
            "reliability_score": round(100.0 * sum(passes) / len(passes), 3) if passes else 50.0,
            "median_total_tokens": round(statistics.median(tokens), 3) if tokens else None,
            "efficiency_score": 50.0,
        }
    if len(medians) >= 2:
        low, high = min(medians.values()), max(medians.values())
        for agent, median in medians.items():
            result[agent]["efficiency_score"] = (
                50.0 if math.isclose(low, high) else round(100.0 * (high - median) / (high - low), 3)
            )
    return result


TOKEN_KEYS = {
    "inputtokens": "input_tokens",
    "prompttokens": "input_tokens",
    "prompttokencount": "input_tokens",
    "outputtokens": "output_tokens",
    "completiontokens": "output_tokens",
    "candidatestokencount": "output_tokens",
    "cachereadinputtokens": "cache_read_tokens",
    "cachereadtokens": "cache_read_tokens",
    "cachedinputtokens": "cache_read_tokens",
    "cachecreationinputtokens": "cache_write_tokens",
    "cachewritetokens": "cache_write_tokens",
    "reasoningtokens": "reasoning_tokens",
    "reasoningoutputtokens": "reasoning_tokens",
    "thoughtstokencount": "reasoning_tokens",
    "totaltokens": "total_tokens",
    "totaltokencount": "total_tokens",
}
COST_KEYS = {"costusd", "totalcostusd", "totalcost"}


def normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def json_values(text: str) -> Iterable[Any]:
    stripped = text.strip()
    if not stripped:
        return
    try:
        yield json.loads(stripped)
    except (json.JSONDecodeError, RecursionError):
        pass
    for line in stripped.splitlines():
        try:
            yield json.loads(line)
        except (json.JSONDecodeError, RecursionError):
            continue


def dictionaries(value: Any, *, limit: int = 50_000) -> Iterable[dict[str, Any]]:
    stack = [value]
    visited = 0
    while stack and visited < limit:
        current = stack.pop()
        visited += 1
        if isinstance(current, dict):
            yield current
            stack.extend(reversed(list(current.values())))
        elif isinstance(current, list):
            stack.extend(reversed(current))


def aggregate_claude_model_usage(
    value: Any, *, top_level_only: bool = False
) -> list[dict[str, Any]]:
    aggregates: list[dict[str, Any]] = []
    for row in dictionaries(value):
        if top_level_only and (
            row is not value or value.get("type", "result") != "result"
        ):
            # Only a genuine top-level/native result envelope may aggregate;
            # dictionaries() yields the top-level dict first, so every later
            # row is nested user/tool content, never a Claude assistant or
            # result envelope. Bare legacy JSON docs without a "type" key
            # stay accepted as top-level envelopes.
            break
        model_usage = next(
            (
                raw
                for key, raw in row.items()
                if normalized_key(str(key)) == "modelusage"
            ),
            None,
        )
        if not isinstance(model_usage, dict) or not model_usage:
            continue
        aggregate = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
            "cost_usd": 0.0,
        }
        model_count = 0
        has_reasoning = False
        for raw_usage in model_usage.values():
            if not isinstance(raw_usage, dict):
                continue
            parsed: dict[str, Any] = {}
            for key, raw in raw_usage.items():
                normalized = normalized_key(str(key))
                target = TOKEN_KEYS.get(normalized)
                if (
                    target
                    and isinstance(raw, (int, float))
                    and not isinstance(raw, bool)
                    and raw >= 0
                ):
                    parsed[target] = int(raw)
                elif (
                    normalized in COST_KEYS
                    and isinstance(raw, (int, float))
                    and not isinstance(raw, bool)
                    and raw >= 0
                ):
                    parsed["cost_usd"] = float(raw)
            if not any(
                key in parsed
                for key in (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_tokens",
                    "cache_write_tokens",
                    "reasoning_tokens",
                )
            ):
                continue
            model_count += 1
            for key in (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "reasoning_tokens",
            ):
                if key in parsed:
                    aggregate[key] += int(parsed[key])
            has_reasoning = has_reasoning or "reasoning_tokens" in parsed
            aggregate["cost_usd"] += float(parsed.get("cost_usd", 0.0))
        if not model_count:
            continue
        top_level_costs = [
            float(raw)
            for key, raw in row.items()
            if normalized_key(str(key)) in COST_KEYS
            and isinstance(raw, (int, float))
            and not isinstance(raw, bool)
            and raw >= 0
        ]
        if top_level_costs:
            aggregate["cost_usd"] = max(top_level_costs)
        aggregate["cost_usd"] = round(float(aggregate["cost_usd"]), 8)
        aggregate["total_tokens"] = (
            int(aggregate["input_tokens"]) + int(aggregate["output_tokens"])
        )
        aggregate["model_count"] = model_count
        aggregate["aggregation"] = "claude-modelUsage-sum"
        if not has_reasoning:
            aggregate["reasoning_tokens"] = None
        aggregates.append(aggregate)
    return aggregates


_CLAUDE_STREAM_COUNTER_MAX = 1_000_000_000_000


def _claude_stream_counter(value: Any) -> int | None:
    # Bounded validation: nonnegative finite ints only. Booleans, non-integral
    # or non-finite floats and unreasonably large values are not token counters.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None
    number = int(value)
    if number < 0 or number > _CLAUDE_STREAM_COUNTER_MAX:
        return None
    return number


def _has_claude_final_result(values: list[Any]) -> bool:
    for value in values:
        if not isinstance(value, dict) or value.get("type") != "result":
            continue
        usage = value.get("usage")
        if isinstance(usage, dict) and any(
            TOKEN_KEYS.get(normalized_key(str(key))) for key in usage
        ):
            return True
    return False


def _claude_legacy_candidate_rows(value: Any) -> Iterable[dict[str, Any]]:
    # Which dicts of one parsed value may feed the generic Claude candidate
    # scan. A native stream envelope (top-level dict carrying a "type" key)
    # is provider-owned only at its own top level plus, for a result
    # envelope, its direct "usage" object; nested message/content and
    # tool_result payloads are untrusted and are never scraped as Claude
    # telemetry. Legacy bare JSON docs without a "type" key keep the
    # historical full nested scan.
    if not isinstance(value, dict):
        yield from dictionaries(value)
        return
    if "type" not in value:
        yield from dictionaries(value)
        return
    yield value
    if value.get("type") == "result":
        usage = value.get("usage")
        if isinstance(usage, dict):
            yield usage


def aggregate_claude_stream_usage(values: list[Any]) -> dict[str, Any] | None:
    # Fallback for an interrupted native stream without a type=result envelope.
    # Sums per-request counters across distinct assistant message IDs, keeping
    # the maximum observed value per message because snapshots repeat. Only
    # top-level native assistant envelopes count; nested user/tool content is
    # never scraped as Claude assistant usage.
    messages: dict[str, dict[str, int]] = {}
    for value in values:
        if not isinstance(value, dict) or value.get("type") != "assistant":
            continue
        message = value.get("message")
        if not isinstance(message, dict):
            continue
        message_id = message.get("id")
        if not isinstance(message_id, str) or not message_id.strip():
            continue
        if message.get("role") != "assistant":
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        row: dict[str, int] = {}
        for key, raw in usage.items():
            target = TOKEN_KEYS.get(normalized_key(str(key)))
            if target not in (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
            ):
                continue
            counter = _claude_stream_counter(raw)
            if counter is None:
                continue
            row[target] = counter
        if not row:
            continue
        prior = messages.setdefault(message_id, {})
        for key, counter in row.items():
            prior[key] = max(prior.get(key, 0), counter)
    if not messages:
        return None
    input_total = sum(row.get("input_tokens", 0) for row in messages.values())
    return {
        "status": "known",
        "source": "provider-json",
        "confidence": 0.5,
        "input_tokens": input_total,
        "fresh_input_tokens": input_total,
        "output_tokens": None,
        "observed_output_tokens": sum(
            row.get("output_tokens", 0) for row in messages.values()
        ),
        "cache_read_tokens": sum(
            row.get("cache_read_tokens", 0) for row in messages.values()
        ),
        "cache_write_tokens": sum(
            row.get("cache_write_tokens", 0) for row in messages.values()
        ),
        "reasoning_tokens": None,
        "total_tokens": None,
        "cost_usd": None,
        "adapter": "claude",
        "total_derived": False,
        "aggregation": "claude-stream-distinct-messages",
        "message_count": len(messages),
        "incomplete": True,
        "incomplete_reason": (
            "stream ended without a final result envelope; output_tokens, "
            "total_tokens and cost_usd are unknown and observed counters are "
            "a lower bound over distinct assistant message IDs"
        ),
    }


def aggregate_opencode_usage(value: Any) -> list[dict[str, Any]]:
    aggregates: list[dict[str, Any]] = []
    for row in dictionaries(value):
        tokens = row.get("tokens")
        if not isinstance(tokens, dict):
            continue
        input_tokens = tokens.get("input")
        output_tokens = tokens.get("output")
        reasoning_tokens = tokens.get("reasoning", 0)
        if any(
            not isinstance(item, int) or isinstance(item, bool) or item < 0
            for item in (input_tokens, output_tokens, reasoning_tokens)
        ):
            continue
        cache = tokens.get("cache")
        cache_read = cache.get("read", 0) if isinstance(cache, dict) else 0
        cache_write = cache.get("write", 0) if isinstance(cache, dict) else 0
        if any(
            not isinstance(item, int) or isinstance(item, bool) or item < 0
            for item in (cache_read, cache_write)
        ):
            continue
        total = tokens.get("total")
        total_derived = (
            not isinstance(total, int) or isinstance(total, bool) or total < 0
        )
        if total_derived:
            total = input_tokens + output_tokens + reasoning_tokens
        cost = row.get("cost")
        aggregates.append(
            {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "reasoning_tokens": reasoning_tokens,
                "cache_read_tokens": cache_read,
                "cache_write_tokens": cache_write,
                "total_tokens": total,
                "total_derived": total_derived,
                "cost_usd": (
                    round(float(cost), 8)
                    if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0
                    else None
                ),
            }
        )
    return aggregates


def aggregate_stream_usage(values: list[Any], *, adapter: str) -> dict[str, Any] | None:
    # Provider completion events are per message/step, not cumulative run totals.
    rows: dict[str, dict[str, Any]] = {}
    for index, event in enumerate(values):
        if not isinstance(event, dict):
            continue
        if adapter == "qwen" and event.get("type") == "assistant":
            message = event.get("message")
            if not isinstance(message, dict) or not isinstance(message.get("id"), str):
                continue
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue
            row = {
                TOKEN_KEYS[normalized_key(str(key))]: int(value)
                for key, value in usage.items()
                if normalized_key(str(key)) in TOKEN_KEYS
                and isinstance(value, (int, float)) and not isinstance(value, bool)
                and value >= 0
            }
            if not {"input_tokens", "output_tokens"} <= row.keys():
                continue
            identity = message["id"]
        elif adapter == "opencode" and event.get("type") == "step_finish":
            part = event.get("part")
            if not isinstance(part, dict):
                continue
            parsed = aggregate_opencode_usage(part)
            if not parsed:
                continue
            row = parsed[0]
            identity = str(part.get("id") or event.get("id") or f"event-{index}")
        else:
            continue
        prior = rows.get(identity, {})
        rows[identity] = {
            key: max(prior.get(key, 0) or 0, value)
            if isinstance(value, (int, float)) and not isinstance(value, bool)
            else value
            for key, value in (prior | row).items()
        }
    if not rows:
        return None
    fields = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")
    summed = {key: sum(row.get(key, 0) or 0 for row in rows.values()) for key in fields}
    total = sum(row.get("total_tokens", row["input_tokens"] + row["output_tokens"]) for row in rows.values())
    costs = [row.get("cost_usd") for row in rows.values()]
    derived = any(row.get("total_derived", "total_tokens" not in row) for row in rows.values())
    return {
        **summed, "status": "known", "source": "provider-json",
        "confidence": 0.8 if derived else 1.0,
        "fresh_input_tokens": sum(
            max(0, row["input_tokens"] - row.get("cache_read_tokens", 0))
            if adapter == "qwen" else row["input_tokens"] for row in rows.values()
        ),
        "total_tokens": total, "total_derived": derived,
        "cost_usd": round(sum(costs), 8) if all(isinstance(cost, (int, float)) for cost in costs) else None,
        "adapter": adapter,
        "aggregation": "distinct-assistant-message-ids" if adapter == "qwen" else "distinct-step-finish-ids",
    }


def extract_codebuddy_token_usage(text: str) -> dict[str, Any]:
    unknown = {
        "status": "unknown",
        "source": "provider-output",
        "confidence": 0.0,
        "input_tokens": None,
        "fresh_input_tokens": None,
        "output_tokens": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "reasoning_tokens": None,
        "total_tokens": None,
        "cost_usd": None,
        "credits": None,
        "adapter": "codebuddy",
        "cost_semantics": "codebuddy_plan_credits_not_usd",
    }
    try:
        envelope = json.loads(text.strip())
    except (json.JSONDecodeError, RecursionError, AttributeError):
        return unknown
    events = envelope if isinstance(envelope, list) else (
        [envelope] if isinstance(envelope, dict) else []
    )
    input_tokens = output_tokens = cache_read = cache_write = reasoning = total = None
    credits = 0.0
    saw_credit = False
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("type") == "result" and isinstance(event.get("usage"), dict):
            usage = event["usage"]
            if isinstance(usage.get("input_tokens"), int) and not isinstance(usage.get("input_tokens"), bool):
                input_tokens = int(usage["input_tokens"])
            if isinstance(usage.get("output_tokens"), int) and not isinstance(usage.get("output_tokens"), bool):
                output_tokens = int(usage["output_tokens"])
            if isinstance(usage.get("cache_read_input_tokens"), int) and not isinstance(
                usage.get("cache_read_input_tokens"), bool
            ):
                cache_read = int(usage["cache_read_input_tokens"])
            if isinstance(usage.get("cache_creation_input_tokens"), int) and not isinstance(
                usage.get("cache_creation_input_tokens"), bool
            ):
                cache_write = int(usage["cache_creation_input_tokens"])
            if isinstance(usage.get("total_tokens"), int) and not isinstance(usage.get("total_tokens"), bool):
                total = int(usage["total_tokens"])
        if event.get("type") == "message" and event.get("role") == "assistant":
            provider_data = event.get("providerData")
            if not isinstance(provider_data, dict):
                continue
            raw_usage = provider_data.get("rawUsage")
            if isinstance(raw_usage, dict):
                credit = raw_usage.get("credit")
                if isinstance(credit, (int, float)) and not isinstance(credit, bool) and credit >= 0:
                    credits += float(credit)
                    saw_credit = True
                thinking = raw_usage.get("completion_thinking_tokens")
                if isinstance(thinking, int) and not isinstance(thinking, bool) and thinking >= 0:
                    reasoning = (0 if reasoning is None else reasoning) + thinking
            usage = provider_data.get("usage")
            if isinstance(usage, dict):
                for key in ("inputTokens", "input_tokens"):
                    value = usage.get(key)
                    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                        input_tokens = (0 if input_tokens is None else input_tokens) + value
                        break
                for key in ("outputTokens", "output_tokens"):
                    value = usage.get(key)
                    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                        output_tokens = (0 if output_tokens is None else output_tokens) + value
                        break
                details = usage.get("outputTokensDetails")
                if isinstance(details, list):
                    for row in details:
                        if isinstance(row, dict):
                            value = row.get("reasoning_tokens")
                            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                                reasoning = (0 if reasoning is None else reasoning) + value
    if input_tokens is None and output_tokens is None and not saw_credit:
        return unknown
    if total is None and (input_tokens is not None or output_tokens is not None):
        total = int(input_tokens or 0) + int(output_tokens or 0)
    return {
        "status": "known",
        "source": "provider-json",
        "confidence": 1.0,
        "input_tokens": input_tokens,
        "fresh_input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "reasoning_tokens": reasoning,
        "total_tokens": total,
        "cost_usd": None,
        "credits": round(credits, 8) if saw_credit else None,
        "adapter": "codebuddy",
        "cost_semantics": "codebuddy_plan_credits_not_usd",
    }


def extract_token_usage(text: str, *, adapter: str) -> dict[str, Any]:
    if adapter == "codebuddy":
        return extract_codebuddy_token_usage(text)
    if adapter == "mimo":
        # MiMo Token Plan accounting: credits are not tokens and not USD, and
        # empty/error/timed-out output stays UNKNOWN instead of becoming zero.
        return mimo_runtime.extract_mimo_token_usage(text)
    if adapter == "openclaw":
        # OpenClaw's agent-exec envelope reports cumulative run usage with
        # short keys. The generic parser intentionally does not treat a bare
        # `input` or `output` in arbitrary nested JSON as token telemetry.
        # Read only the top-level, provider-owned usage object here.
        for value in json_values(text):
            if not isinstance(value, dict) or not isinstance(value.get("usage"), dict):
                continue
            usage = value["usage"]
            keys = ("input", "output", "cacheRead", "cacheWrite", "total")
            if not all(type(usage.get(key)) is int and usage[key] >= 0 for key in keys):
                continue
            if usage["total"] < usage["input"] + usage["output"]:
                continue
            cost = value.get("costUsd")
            cost = (
                round(float(cost), 8)
                if isinstance(cost, (int, float)) and not isinstance(cost, bool)
                and math.isfinite(float(cost)) and cost >= 0
                else None
            )
            return {
                "status": "known",
                "source": "provider-json",
                "confidence": 1.0,
                "input_tokens": usage["input"],
                "fresh_input_tokens": max(0, usage["input"] - usage["cacheRead"]),
                "output_tokens": usage["output"],
                "cache_read_tokens": usage["cacheRead"],
                "cache_write_tokens": usage["cacheWrite"],
                "reasoning_tokens": None,
                "total_tokens": usage["total"],
                "cost_usd": cost,
                "adapter": adapter,
                "total_derived": False,
            }
        return {
            "status": "unknown", "source": "provider-output", "confidence": 0.0,
            "input_tokens": None, "fresh_input_tokens": None,
            "output_tokens": None, "cache_read_tokens": None,
            "cache_write_tokens": None, "reasoning_tokens": None,
            "total_tokens": None, "cost_usd": None, "adapter": adapter,
        }
    if adapter == "grok":
        # Native run totals are root-owned. Never sum mirrored modelUsage or
        # reinterpret input as fresh tokens without provider evidence.
        try:
            envelope = json.loads(text.strip())
        except (json.JSONDecodeError, RecursionError, AttributeError):
            envelope = None
        native_usage = grok_usage.extract_native_grok_usage(envelope)
        if native_usage is not None:
            return native_usage
    values = list(json_values(text))
    if adapter in {"qwen", "opencode"}:
        stream_usage = aggregate_stream_usage(values, adapter=adapter)
        if stream_usage is not None:
            return stream_usage
    candidates: list[dict[str, Any]] = []
    for value in values:
        if adapter == "opencode":
            aggregates = aggregate_opencode_usage(value)
            if aggregates:
                selected = max(aggregates, key=lambda row: int(row["total_tokens"]))
                return {
                    "status": "known",
                    "source": "provider-json",
                    "confidence": 1.0,
                    "input_tokens": selected["input_tokens"],
                    "fresh_input_tokens": selected["input_tokens"],
                    "output_tokens": selected["output_tokens"],
                    "cache_read_tokens": selected["cache_read_tokens"],
                    "cache_write_tokens": selected["cache_write_tokens"],
                    "reasoning_tokens": selected["reasoning_tokens"],
                    "total_tokens": selected["total_tokens"],
                    "cost_usd": selected["cost_usd"],
                    "adapter": adapter,
                    "total_derived": selected["total_derived"],
                }
        if adapter == "claude":
            aggregates = aggregate_claude_model_usage(value, top_level_only=True)
            if aggregates:
                selected = max(
                    aggregates,
                    key=lambda row: (
                        int(row["model_count"]),
                        int(row["total_tokens"]),
                        int(row["cache_read_tokens"])
                        + int(row["cache_write_tokens"]),
                    ),
                )
                return {
                    "status": "known",
                    "source": "provider-json",
                    "confidence": 1.0,
                    "input_tokens": selected["input_tokens"],
                    "fresh_input_tokens": selected["input_tokens"],
                    "output_tokens": selected["output_tokens"],
                    "cache_read_tokens": selected["cache_read_tokens"],
                    "cache_write_tokens": selected["cache_write_tokens"],
                    "reasoning_tokens": selected["reasoning_tokens"],
                    "total_tokens": selected["total_tokens"],
                    "cost_usd": selected["cost_usd"],
                    "adapter": adapter,
                    "total_derived": True,
                    "aggregation": selected["aggregation"],
                    "model_count": selected["model_count"],
                }
        candidate_rows = (
            _claude_legacy_candidate_rows(value)
            if adapter == "claude"
            else dictionaries(value)
        )
        for row in candidate_rows:
            parsed: dict[str, Any] = {}
            for key, raw in row.items():
                normalized = normalized_key(str(key))
                target = TOKEN_KEYS.get(normalized)
                if target and isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw >= 0:
                    parsed[target] = int(raw)
                elif normalized in COST_KEYS and isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw >= 0:
                    parsed["cost_usd"] = round(float(raw), 8)
            if parsed:
                candidates.append(parsed)
    if adapter == "claude" and not _has_claude_final_result(values):
        stream_usage = aggregate_claude_stream_usage(values)
        if stream_usage is not None:
            return stream_usage
    if not candidates:
        return {
            "status": "unknown",
            "source": "provider-output",
            "confidence": 0.0,
            "input_tokens": None,
            "fresh_input_tokens": None,
            "output_tokens": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
            "reasoning_tokens": None,
            "total_tokens": None,
            "cost_usd": None,
            "adapter": adapter,
        }
    selected = max(
        candidates,
        key=lambda row: (
            len(row),
            int(row.get("total_tokens", 0)),
            int(row.get("input_tokens", 0)) + int(row.get("output_tokens", 0)),
        ),
    )
    if "cost_usd" not in selected:
        costs = [
            float(candidate["cost_usd"])
            for candidate in candidates
            if isinstance(candidate.get("cost_usd"), (int, float))
        ]
        if costs:
            selected["cost_usd"] = max(costs)
    derived_total = False
    if "total_tokens" not in selected and (
        "input_tokens" in selected or "output_tokens" in selected
    ):
        selected["total_tokens"] = int(selected.get("input_tokens", 0)) + int(
            selected.get("output_tokens", 0)
        )
        derived_total = True
    fresh_input_tokens = None if adapter == "grok" else selected.get("input_tokens")
    if (
        adapter in {"codex", "qwen"}
        and isinstance(fresh_input_tokens, int)
        and isinstance(selected.get("cache_read_tokens"), int)
    ):
        fresh_input_tokens = max(
            0, fresh_input_tokens - int(selected["cache_read_tokens"])
        )
    return {
        "status": "known",
        "source": "provider-json",
        "confidence": 0.8 if derived_total else 1.0,
        "input_tokens": selected.get("input_tokens"),
        "fresh_input_tokens": fresh_input_tokens,
        "output_tokens": selected.get("output_tokens"),
        "cache_read_tokens": selected.get("cache_read_tokens"),
        "cache_write_tokens": selected.get("cache_write_tokens"),
        "reasoning_tokens": selected.get("reasoning_tokens"),
        "total_tokens": selected.get("total_tokens"),
        "cost_usd": selected.get("cost_usd"),
        "adapter": adapter,
        "total_derived": derived_total,
    }
