"""Deterministic adaptive lane evaluator for Agent Fleet (policy v1, shadow).

Enforces the accepted lane contract without granting any authority:

- Lane ordinals are fixed: direct=0, compact=1, compact-recon=2, full=3.
- Hard risk wins first, before continuity or scope.
- ``requested_lane`` may escalate only. There is no ``lane_exception``, no
  waiver and no de-escalation path.
- MISSING, STALE or UNKNOWN continuity requires exactly one separate,
  allowlisted, terminal ``compact-recon`` pass with ephemeral evidence and a
  parent-issued receipt bound to a revision; the lane is recomputed afterwards.
  Durable continuity writes resolve to full.
- ``compact`` requires parent-verified D0 evidence carrying path, SHA-256 and
   a run identifier. The caller must also provide a trusted file verifier;
   caller-supplied booleans never authenticate evidence. Missing or
   unverifiable D0 resolves to full with ``D0_MISSING``.
- The only public Qwen model alias is ``qwen3.8-max``. An unresolved alias or
  a silent substitution blocks with ``MODEL_ALIAS_UNRESOLVED``. A dated
   provider snapshot may appear only as provenance after a trusted provider
   verifier accepts the observation. Caller-supplied verification booleans
   never authenticate a provider response.

The evaluator never accepts prompt contents, secrets, quota claims or
synthetic assessments as route input, never persists recon evidence, and
performs no state or provider writes. It stays inactive in the installed
runtime during the shadow period; the baseline kill switch and the per-lane
kill switches force full. Graph owns structure, Fleet owns routing, Loop owns
scheduling, gates audit, and the council handles material conflicts; nothing
here waives those responsibilities.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any, Callable, Mapping


LANE_POLICY_VERSION = "lane-policy-v1"
POLICY_SCHEMA_VERSION = 1
LANE_ORDINALS: dict[str, int] = {
    "direct": 0,
    "compact": 1,
    "compact-recon": 2,
    "full": 3,
}
FULL_LANE = "full"
RECON_LANE = "compact-recon"

CANONICAL_QWEN_ALIAS = "qwen3.8-max"
DATED_SNAPSHOT_PATTERN = re.compile(r"^qwen3\.8-max-[0-9]{4}$")
SAFE_EFFECT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SAFE_REVISION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

# Bounds are checked before schema evaluation and before the CLI parses a
# request into a decision.
MAX_REQUEST_BYTES = 256 * 1024
MAX_REQUEST_DEPTH = 8
MAX_REQUEST_STRING_LENGTH = 1024
MAX_REQUEST_COLLECTION_ITEMS = 64
MAX_REQUEST_OBJECT_KEYS = 64

HARD_RISK_FACTS: tuple[str, ...] = (
    "independent_review_required",
    "material_decision",
    "persistent_storage_or_migration",
    "production_or_external_effect",
    "restricted_data",
    "secrets_involved",
    "security_or_role_change",
)
ENVIRONMENTS = ("DEV", "PRODUCTION")
CONTINUITY_STATES = ("FRESH", "MISSING", "STALE", "UNKNOWN")
RECON_TRIGGER_STATES = frozenset({"MISSING", "STALE", "UNKNOWN"})

RECON_READ_ALLOWLIST: tuple[str, ...] = (
    "gate-evidence",
    "git-diff",
    "git-log",
    "git-status",
    "planning-contract",
    "prior-run-receipts",
    "release-manifests",
    "task-assessment-metadata",
)

# Structural refusals: none of these may ever enter a lane decision or the
# shadow telemetry. Refusal messages contain counts only, never input names
# or values supplied by the caller.
FORBIDDEN_REQUEST_KEYS: tuple[str, ...] = (
    "api_key",
    "assessment",
    "assessment_source",
    "cookie",
    "cookies",
    "fallback_assessment",
    "lane_exception",
    "prompt",
    "prompt_text",
    "quota",
    "quota_snapshot",
    "secret",
    "secrets",
    "task_text",
    "token",
    "tokens",
    "waiver",
)

REQUEST_KEYS: frozenset[str] = frozenset(
    {
        "policy_version",
        "requested_lane",
        "requested_model",
        "environment",
        "external_effects",
        "hard_risk",
        "coordinated_scope",
        "delegation_benefit",
        "continuity_state",
        "continuity_durable_write",
        "d0_evidence",
        "recon",
        "signals",
        "kill_switches",
        "provider_verification",
    }
)
D0_KEYS = frozenset({"path", "sha256", "run_id", "parent_verified"})
RECON_KEYS = frozenset(
    {
        "passes_used",
        "lane_used",
        "reads",
        "evidence_persistence",
        "continuity_state_after",
        "receipt_sha256",
        "revision",
    }
)
SIGNAL_KEYS = frozenset(
    {"stale_continuity", "provenance_drift", "gate_regression"}
)
KILL_LANE_KEYS = frozenset({"direct", "compact", "compact-recon"})
VERIFICATION_KEYS = frozenset({"verified", "served_model"})

BASELINE_KILL_SWITCH_ENV = "AGENT_FLEET_LANE_KILL_SWITCH"
LANE_KILL_SWITCH_ENV = {
    "direct": "AGENT_FLEET_LANE_KILL_DIRECT",
    "compact": "AGENT_FLEET_LANE_KILL_COMPACT",
    "compact-recon": "AGENT_FLEET_LANE_KILL_COMPACT_RECON",
}
TRUTHY_ENV_VALUES = frozenset({"1", "true", "on", "yes"})

GATE_EVENT_STATUSES = frozenset({"PASS", "FAIL", "BLOCKED"})
RETEST_GATE_NAMES = frozenset({"quality", "security", "tests", "parity", "rollback"})
RETEST_EVENT_KEYS = frozenset(
    {"gate", "status", "revision", "receipt_sha256", "parent_verified"}
)
NODE_VERDICTS = frozenset({"ACCEPT", "REJECT"})


class LanePolicyError(ValueError):
    """Structural refusal; the request never becomes a route decision."""


def _is_reparse_or_link(path: Path, metadata: os.stat_result) -> bool:
    """Reject links/reparse points before a request file can be opened."""
    reparse_point = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    is_junction = getattr(path, "is_junction", None)
    return bool(
        path.is_symlink()
        or (int(getattr(metadata, "st_file_attributes", 0)) & reparse_point)
        or bool(is_junction and is_junction())
    )


def load_bounded_request_file(
    path: Path, *, max_bytes: int = MAX_REQUEST_BYTES
) -> dict[str, Any]:
    """Read a regular request file with bounded, link-resistant I/O.

    ``O_NOFOLLOW`` is used where the host exposes it. Windows has no
    equivalent flag in ``os.open``; the lstat/fstat identity check closes the
    check/open race that can otherwise replace a validated path with a link.
    """
    candidate = Path(path)
    if max_bytes < 1:
        raise LanePolicyError("Lane policy request size limit is invalid")
    fd: int | None = None
    try:
        before = candidate.lstat()
        if not stat.S_ISREG(before.st_mode) or _is_reparse_or_link(candidate, before):
            raise LanePolicyError("Lane policy request file must be a regular file")
        flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        if no_follow:
            flags |= int(no_follow)
        fd = os.open(str(candidate), flags)
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise LanePolicyError("Lane policy request file must be a regular file")
        identity = (getattr(before, "st_dev", 0), getattr(before, "st_ino", 0))
        opened_identity = (
            getattr(opened, "st_dev", 0), getattr(opened, "st_ino", 0)
        )
        if identity != opened_identity and all(identity) and all(opened_identity):
            raise LanePolicyError("Lane policy request file changed during open")
        if opened.st_size > max_bytes:
            raise LanePolicyError("Lane policy request exceeds byte limit")
        with os.fdopen(fd, "rb", closefd=True) as handle:
            fd = None
            raw = handle.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise LanePolicyError("Lane policy request exceeds byte limit")
    except LanePolicyError:
        raise
    except (OSError, ValueError, OverflowError) as exc:
        raise LanePolicyError("Lane policy request file is unreadable") from exc
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (
        UnicodeError,
        json.JSONDecodeError,
        RecursionError,
        MemoryError,
        ValueError,
    ) as exc:
        raise LanePolicyError("Lane policy request JSON is invalid") from exc
    if not isinstance(value, dict):
        raise LanePolicyError("Lane policy request must be an object")
    return value


def _env_flag(environ: Mapping[str, str], name: str) -> bool:
    return str(environ.get(name, "")).strip().casefold() in TRUTHY_ENV_VALUES


def _redacted_token(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"<UNRESOLVED:sha256:{digest}>"


def _require_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise LanePolicyError(f"{label} must be boolean")
    return value


def _validate_request_bounds(value: Any, *, depth: int = 0) -> None:
    """Reject oversized or excessively nested untrusted request values."""
    if depth > MAX_REQUEST_DEPTH:
        raise LanePolicyError("Lane policy request exceeds structural limits")
    if isinstance(value, str):
        if len(value) > MAX_REQUEST_STRING_LENGTH:
            raise LanePolicyError("Lane policy request exceeds string limit")
        return
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_REQUEST_COLLECTION_ITEMS:
            raise LanePolicyError("Lane policy request exceeds collection limit")
        for item in value:
            _validate_request_bounds(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > MAX_REQUEST_OBJECT_KEYS:
            raise LanePolicyError("Lane policy request exceeds object-key limit")
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > MAX_REQUEST_STRING_LENGTH:
                raise LanePolicyError("Lane policy request contains an invalid key")
            _validate_request_bounds(item, depth=depth + 1)


def effective_kill_switches(
    requested: Mapping[str, Any] | None,
    environ: Mapping[str, str],
) -> dict[str, Any]:
    baseline = False
    lanes = {lane: False for lane in KILL_LANE_KEYS}
    if requested is not None:
        baseline = _require_bool(requested["baseline"], "kill_switches.baseline")
        requested_lanes = requested["lanes"]
        if (
            not isinstance(requested_lanes, dict)
            or set(requested_lanes) != set(KILL_LANE_KEYS)
        ):
            raise LanePolicyError(
                "kill_switches.lanes requires exactly: "
                + ", ".join(sorted(KILL_LANE_KEYS))
            )
        for lane in KILL_LANE_KEYS:
            lanes[lane] = _require_bool(
                requested_lanes[lane], f"kill_switches.lanes.{lane}"
            )
    if _env_flag(environ, BASELINE_KILL_SWITCH_ENV):
        baseline = True
    for lane, env_name in LANE_KILL_SWITCH_ENV.items():
        if _env_flag(environ, env_name):
            lanes[lane] = True
    return {"baseline": baseline, "lanes": lanes}


def validate_request(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LanePolicyError("Lane policy request must be an object")
    _validate_request_bounds(value)
    forbidden = sorted(set(value) & set(FORBIDDEN_REQUEST_KEYS))
    if forbidden:
        raise LanePolicyError("Lane policy refuses forbidden input fields")
    if set(value) != set(REQUEST_KEYS):
        missing = sorted(set(REQUEST_KEYS) - set(value))
        unexpected = sorted(set(value) - set(REQUEST_KEYS))
        raise LanePolicyError(
            "Lane policy request shape invalid; missing_count="
            + str(len(missing))
            + ";unexpected_count="
            + str(len(unexpected))
        )
    request = dict(value)
    if request["policy_version"] != POLICY_SCHEMA_VERSION:
        raise LanePolicyError("policy_version must be 1")
    requested_lane = request["requested_lane"]
    if requested_lane is not None and requested_lane not in LANE_ORDINALS:
        raise LanePolicyError("requested_lane is not a known lane ordinal name")
    requested_model = request["requested_model"]
    if not isinstance(requested_model, str) or not requested_model:
        raise LanePolicyError("requested_model must be a non-empty string")
    if request["environment"] not in ENVIRONMENTS:
        raise LanePolicyError("environment must be DEV or PRODUCTION")
    effects = request["external_effects"]
    if not isinstance(effects, list) or any(
        not isinstance(item, str) or not SAFE_EFFECT_PATTERN.fullmatch(item)
        for item in effects
    ):
        raise LanePolicyError(
            "external_effects must be a list of lowercase kebab-case identifiers"
        )
    hard_risk = request["hard_risk"]
    if not isinstance(hard_risk, dict) or set(hard_risk) != set(HARD_RISK_FACTS):
        raise LanePolicyError(
            "hard_risk requires exactly: " + ", ".join(sorted(HARD_RISK_FACTS))
        )
    for fact in HARD_RISK_FACTS:
        _require_bool(hard_risk[fact], f"hard_risk.{fact}")
    _require_bool(request["coordinated_scope"], "coordinated_scope")
    _require_bool(request["delegation_benefit"], "delegation_benefit")
    _require_bool(request["continuity_durable_write"], "continuity_durable_write")
    if request["continuity_state"] not in CONTINUITY_STATES:
        raise LanePolicyError(
            "continuity_state must be one of: " + ", ".join(CONTINUITY_STATES)
        )
    signals = request["signals"]
    if not isinstance(signals, dict) or set(signals) != set(SIGNAL_KEYS):
        raise LanePolicyError(
            "signals requires exactly: " + ", ".join(sorted(SIGNAL_KEYS))
        )
    for signal in SIGNAL_KEYS:
        _require_bool(signals[signal], f"signals.{signal}")
    d0 = request["d0_evidence"]
    if d0 is not None:
        if not isinstance(d0, dict) or set(d0) != set(D0_KEYS):
            raise LanePolicyError(
                "d0_evidence requires exactly: " + ", ".join(sorted(D0_KEYS))
            )
        if not isinstance(d0["path"], str) or not d0["path"].strip():
            raise LanePolicyError("d0_evidence.path must be a non-empty string")
        if not isinstance(d0["sha256"], str):
            raise LanePolicyError("d0_evidence.sha256 must be a string")
        if not isinstance(d0["run_id"], str) or not d0["run_id"].strip():
            raise LanePolicyError("d0_evidence.run_id must be a non-empty string")
        _require_bool(d0["parent_verified"], "d0_evidence.parent_verified")
    recon = request["recon"]
    if recon is not None:
        if not isinstance(recon, dict) or set(recon) != set(RECON_KEYS):
            raise LanePolicyError(
                "recon requires exactly: " + ", ".join(sorted(RECON_KEYS))
            )
        passes_used = recon["passes_used"]
        if isinstance(passes_used, bool) or not isinstance(passes_used, int):
            raise LanePolicyError("recon.passes_used must be an integer")
        if not isinstance(recon["lane_used"], str):
            raise LanePolicyError("recon.lane_used must be a string")
        reads = recon["reads"]
        if not isinstance(reads, list) or any(
            not isinstance(item, str) for item in reads
        ):
            raise LanePolicyError("recon.reads must be a list of strings")
        if not isinstance(recon["evidence_persistence"], str):
            raise LanePolicyError("recon.evidence_persistence must be a string")
        if recon["continuity_state_after"] not in CONTINUITY_STATES:
            raise LanePolicyError(
                "recon.continuity_state_after must be one of: "
                + ", ".join(CONTINUITY_STATES)
            )
        if not isinstance(recon["receipt_sha256"], str) or not SHA256_PATTERN.fullmatch(
            recon["receipt_sha256"]
        ):
            raise LanePolicyError("recon.receipt_sha256 must be a lowercase SHA-256")
        if not isinstance(recon["revision"], str) or not SAFE_REVISION_PATTERN.fullmatch(
            recon["revision"]
        ):
            raise LanePolicyError("recon.revision must be a safe non-empty identifier")
        if request["continuity_state"] not in RECON_TRIGGER_STATES:
            raise LanePolicyError(
                "recon requires MISSING, STALE or UNKNOWN continuity_state"
            )
    kill = request["kill_switches"]
    if kill is not None:
        if not isinstance(kill, dict) or set(kill) != {"baseline", "lanes"}:
            raise LanePolicyError(
                "kill_switches requires exactly: baseline, lanes"
            )
    verification = request["provider_verification"]
    if verification is not None:
        if (
            not isinstance(verification, dict)
            or set(verification) != set(VERIFICATION_KEYS)
        ):
            raise LanePolicyError(
                "provider_verification requires exactly: "
                + ", ".join(sorted(VERIFICATION_KEYS))
            )
        _require_bool(verification["verified"], "provider_verification.verified")
        if not isinstance(verification["served_model"], str):
            raise LanePolicyError(
                "provider_verification.served_model must be a string"
            )
    return request


def resolve_model(
    requested_model: str,
    provider_verification: Mapping[str, Any] | None,
    verify_provider: Callable[[Mapping[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    """Resolve the public alias; never substitute or trust self-attestation."""
    if requested_model != CANONICAL_QWEN_ALIAS:
        return {
            "blocked": True,
            "resolved_model": None,
            "provider_snapshot_provenance": None,
            "requested_model_echo": _redacted_token(requested_model),
            "reason_codes": ["MODEL_ALIAS_UNRESOLVED"],
        }
    if provider_verification is None:
        return {
            "blocked": False,
            "resolved_model": CANONICAL_QWEN_ALIAS,
            "provider_snapshot_provenance": None,
            "requested_model_echo": CANONICAL_QWEN_ALIAS,
            "reason_codes": ["MODEL_ALIAS_RESOLVED"],
        }
    served = str(provider_verification["served_model"])
    if provider_verification["verified"] is not True:
        return {
            "blocked": True,
            "resolved_model": None,
            "provider_snapshot_provenance": None,
            "requested_model_echo": CANONICAL_QWEN_ALIAS,
            "reason_codes": ["MODEL_ALIAS_UNRESOLVED"],
        }
    if served == CANONICAL_QWEN_ALIAS:
        if verify_provider is None or verify_provider(provider_verification) is not True:
            return {
                "blocked": True,
                "resolved_model": None,
                "provider_snapshot_provenance": None,
                "requested_model_echo": CANONICAL_QWEN_ALIAS,
                "reason_codes": [
                    "PROVIDER_ATTESTATION_UNVERIFIED",
                    "MODEL_ALIAS_UNRESOLVED",
                ],
            }
        return {
            "blocked": False,
            "resolved_model": CANONICAL_QWEN_ALIAS,
            "provider_snapshot_provenance": None,
            "requested_model_echo": CANONICAL_QWEN_ALIAS,
            "reason_codes": ["MODEL_IDENTITY_VERIFIED"],
        }
    if DATED_SNAPSHOT_PATTERN.fullmatch(served):
        if verify_provider is None or verify_provider(provider_verification) is not True:
            return {
                "blocked": True,
                "resolved_model": None,
                "provider_snapshot_provenance": None,
                "requested_model_echo": CANONICAL_QWEN_ALIAS,
                "reason_codes": [
                    "PROVIDER_ATTESTATION_UNVERIFIED",
                    "MODEL_ALIAS_UNRESOLVED",
                ],
            }
        return {
            "blocked": False,
            "resolved_model": served,
            "provider_snapshot_provenance": served,
            "requested_model_echo": CANONICAL_QWEN_ALIAS,
            "reason_codes": [
                "MODEL_IDENTITY_VERIFIED",
                "DATED_SNAPSHOT_PROVENANCE_ONLY",
            ],
        }
    return {
        "blocked": True,
        "resolved_model": None,
        "provider_snapshot_provenance": None,
        "requested_model_echo": CANONICAL_QWEN_ALIAS,
        "reason_codes": ["SUBSTITUTION_REJECTED", "MODEL_ALIAS_UNRESOLVED"],
    }


def d0_verified(
    evidence: Mapping[str, Any] | None,
    verify_d0_file: Callable[[Mapping[str, Any]], bool] | None = None,
) -> bool:
    if evidence is None:
        return False
    if not evidence["parent_verified"]:
        return False
    if not SHA256_PATTERN.fullmatch(str(evidence["sha256"])):
        return False
    if verify_d0_file is None:
        return False
    if verify_d0_file(evidence) is not True:
        return False
    return True


def recon_directive() -> dict[str, Any]:
    return {
        "lane": RECON_LANE,
        "terminal": True,
        "passes_required": 1,
        "max_passes": 1,
        "same_pass_lanes_forbidden": ["direct", "compact"],
        "read_allowlist": list(RECON_READ_ALLOWLIST),
        "evidence_persistence": "ephemeral",
        "durable_writes_forbidden": True,
        "recompute_lane_after": True,
    }


def _audit_recon(
    recon: Mapping[str, Any],
    verify_recon_receipt: Callable[[Mapping[str, Any]], bool] | None = None,
) -> tuple[str | None, list[str]]:
    """Return (restored_state_or_None, violation_reason_codes).

    A restored state means the recon pass was valid and continuity is FRESH;
    any violation fails closed to full.
    """
    reasons: list[str] = []
    passes_used = int(recon["passes_used"])
    if passes_used > 1:
        reasons.append("RECON_PASS_LIMIT_EXCEEDED")
    elif passes_used < 1:
        reasons.append("RECON_EVIDENCE_MISSING")
    if recon["lane_used"] != RECON_LANE:
        reasons.append("RECON_LANE_VIOLATION")
    outside = sorted(set(recon["reads"]) - set(RECON_READ_ALLOWLIST))
    if outside:
        reasons.append("RECON_ALLOWLIST_VIOLATION")
    if recon["evidence_persistence"] != "ephemeral":
        reasons.append("RECON_EVIDENCE_NOT_EPHEMERAL")
    if reasons:
        return None, reasons
    state_after = str(recon["continuity_state_after"])
    if state_after != "FRESH":
        return None, ["CONTINUITY_UNRESOLVED_AFTER_RECON"]
    try:
        receipt_verified = (
            verify_recon_receipt is not None
            and verify_recon_receipt(recon) is True
        )
    except Exception:
        receipt_verified = False
    if not receipt_verified:
        return None, ["RECON_EVIDENCE_UNVERIFIED"]
    return "FRESH", ["CONTINUITY_RESTORED_BY_RECON"]


def evaluate(
    value: Any,
    *,
    environ: Mapping[str, str] | None = None,
    verify_d0_file: Callable[[Mapping[str, Any]], bool] | None = None,
    verify_provider: Callable[[Mapping[str, Any]], bool] | None = None,
    verify_recon_receipt: Callable[[Mapping[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    request = validate_request(value)
    env = os.environ if environ is None else environ
    reasons: list[str] = []
    kill = effective_kill_switches(request["kill_switches"], env)

    model = resolve_model(
        request["requested_model"],
        request["provider_verification"],
        verify_provider,
    )
    reasons.extend(model["reason_codes"])

    forced_full = False
    if kill["baseline"]:
        reasons.append("KILL_SWITCH_BASELINE")
        forced_full = True
    for fact in HARD_RISK_FACTS:
        if request["hard_risk"][fact]:
            reasons.append(f"HARD_RISK_{fact.upper()}")
            forced_full = True
    if request["environment"] == "PRODUCTION":
        reasons.append("PRODUCTION_ENVIRONMENT")
        forced_full = True
    if request["external_effects"] and not request["hard_risk"][
        "production_or_external_effect"
    ]:
        reasons.append("HARD_RISK_MISDECLARED")
        forced_full = True
    signals = request["signals"]
    if signals["stale_continuity"]:
        reasons.append("STALE_CONTINUITY_FAIL_CLOSED")
        forced_full = True
    if signals["provenance_drift"]:
        reasons.append("PROVENANCE_DRIFT_FAIL_CLOSED")
        forced_full = True
    if signals["gate_regression"]:
        reasons.append("GATE_REGRESSION_FAIL_CLOSED")
        forced_full = True
    if request["continuity_durable_write"]:
        reasons.append("DURABLE_CONTINUITY_WRITE_RESOLVES_FULL")
        forced_full = True

    directive: dict[str, Any] | None = None
    continuity_effective = request["continuity_state"]
    if forced_full:
        resolved = FULL_LANE
    elif continuity_effective in RECON_TRIGGER_STATES:
        recon = request["recon"]
        if recon is None:
            resolved = RECON_LANE
            directive = recon_directive()
            reasons.append("LOCAL_CONTEXT_RECON_REQUIRED")
        else:
            restored, recon_reasons = _audit_recon(recon, verify_recon_receipt)
            reasons.extend(recon_reasons)
            if restored is None:
                resolved = FULL_LANE
            else:
                resolved = _base_lane(request, reasons, verify_d0_file)
    else:
        resolved = _base_lane(request, reasons, verify_d0_file)

    kill_switch_applied_to: str | None = None
    requested_lane = request["requested_lane"]
    if requested_lane is not None and not model["blocked"]:
        if LANE_ORDINALS[requested_lane] > LANE_ORDINALS[resolved]:
            resolved = requested_lane
            reasons.append("REQUESTED_LANE_ESCALATION")
        elif LANE_ORDINALS[requested_lane] < LANE_ORDINALS[resolved]:
            reasons.append("REQUESTED_LANE_DEESCALATION_REJECTED")

    if resolved == "compact" and not d0_verified(
        request["d0_evidence"], verify_d0_file
    ):
        # Escalation cannot bypass the compact D0 gate.
        resolved = FULL_LANE
        if "D0_MISSING" not in reasons:
            reasons.append("D0_MISSING")

    if resolved != FULL_LANE and kill["lanes"][resolved]:
        kill_switch_applied_to = resolved
        resolved = FULL_LANE
        reasons.append("KILL_SWITCH_LANE")

    if model["blocked"]:
        status = "BLOCKED"
        resolved = FULL_LANE
        directive = None
    elif directive is not None and resolved == RECON_LANE:
        status = "RECON_REQUIRED"
    else:
        status = "ROUTED"
        directive = None

    requested_model_echo = model["requested_model_echo"]
    telemetry = {
        "mode": "shadow",
        "runtime_activation": False,
        "lane_policy_version": LANE_POLICY_VERSION,
        "requested_lane": requested_lane,
        "resolved_lane": resolved,
        "requested_model": requested_model_echo,
        "resolved_model": model["resolved_model"],
        "reason_codes": list(reasons),
        "kill_switches": kill,
        "excluded_from_telemetry": [
            "prompt-content",
            "secrets",
            "stale-quota-claims",
            "synthetic-assessments",
        ],
    }
    return {
        "action": "lane-policy",
        "policy_version": POLICY_SCHEMA_VERSION,
        "lane_policy_version": LANE_POLICY_VERSION,
        "status": status,
        "requested_lane": requested_lane,
        "resolved_lane": resolved,
        "resolved_lane_ordinal": LANE_ORDINALS[resolved],
        "requested_model": requested_model_echo,
        "resolved_model": model["resolved_model"],
        "provider_snapshot_provenance": model["provider_snapshot_provenance"],
        "reason_codes": list(reasons),
        "recon_directive": directive,
        "kill_switch_applied_to": kill_switch_applied_to,
        "shadow_telemetry": telemetry,
        "node_acceptance_is_system_acceptance": False,
        "authorization_granted": False,
        "writes_performed": 0,
        "provider_calls": 0,
    }


def _base_lane(
    request: Mapping[str, Any],
    reasons: list[str],
    verify_d0_file: Callable[[Mapping[str, Any]], bool] | None,
) -> str:
    if request["coordinated_scope"]:
        reasons.append("COORDINATED_DELIVERY")
        return FULL_LANE
    if request["delegation_benefit"]:
        if d0_verified(request["d0_evidence"], verify_d0_file):
            reasons.append("D0_VERIFIED")
            return "compact"
        reasons.append("D0_MISSING")
        return FULL_LANE
    reasons.append("NO_DELEGATION_BENEFIT")
    return "direct"


def evaluate_after_recon(
    value: Any,
    recon: Mapping[str, Any],
    *,
    environ: Mapping[str, str] | None = None,
    verify_d0_file: Callable[[Mapping[str, Any]], bool] | None = None,
    verify_provider: Callable[[Mapping[str, Any]], bool] | None = None,
    verify_recon_receipt: Callable[[Mapping[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    """Recompute the lane after exactly one terminal compact-recon pass."""
    request = dict(value)
    request["recon"] = dict(recon)
    return evaluate(
        request,
        environ=environ,
        verify_d0_file=verify_d0_file,
        verify_provider=verify_provider,
        verify_recon_receipt=verify_recon_receipt,
    )


def acceptance_projection(
    decision: Mapping[str, Any], *, node_verdict: str
) -> dict[str, Any]:
    if node_verdict not in NODE_VERDICTS:
        raise LanePolicyError("node_verdict must be ACCEPT or REJECT")
    if decision.get("action") != "lane-policy":
        raise LanePolicyError("decision is not a lane-policy result")
    return {
        "action": "lane-acceptance-projection",
        "lane_policy_version": LANE_POLICY_VERSION,
        "resolved_lane": decision["resolved_lane"],
        "node_acceptance": node_verdict,
        "system_acceptance": "PENDING",
        "reason_codes": ["NODE_ACCEPTANCE_IS_NOT_SYSTEM_ACCEPTANCE"],
        "authorization_granted": False,
        "writes_performed": 0,
    }


def retest_policy(
    gate_events: Any,
    *,
    expected_revision: str | None = None,
    verify_event: Callable[[Mapping[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    if not isinstance(gate_events, list) or not gate_events:
        raise LanePolicyError("gate_events must be a non-empty list")
    invalidated_by: list[str] = []
    revisions: set[str] = set()
    seen_gates: set[str] = set()
    evidence_unverified = False
    duplicates = False
    for event in gate_events:
        if (
            not isinstance(event, dict)
            or set(event) != set(RETEST_EVENT_KEYS)
            or not isinstance(event["gate"], str)
            or event["gate"] not in RETEST_GATE_NAMES
            or event["status"] not in GATE_EVENT_STATUSES
            or not isinstance(event["revision"], str)
            or not SAFE_REVISION_PATTERN.fullmatch(event["revision"])
            or not isinstance(event["receipt_sha256"], str)
            or not SHA256_PATTERN.fullmatch(event["receipt_sha256"])
            or not isinstance(event["parent_verified"], bool)
        ):
            raise LanePolicyError("gate_events entries have an invalid shape")
        if event["gate"] in seen_gates:
            duplicates = True
        seen_gates.add(event["gate"])
        revisions.add(event["revision"])
        if event["status"] in {"FAIL", "BLOCKED"}:
            invalidated_by.append(event["gate"])
        try:
            verified = (
                event["parent_verified"]
                and verify_event is not None
                and verify_event(event) is True
            )
        except Exception:
            verified = False
        if not verified:
            evidence_unverified = True
    missing_gate_count = len(RETEST_GATE_NAMES - seen_gates)
    expected_revision_valid = bool(
        expected_revision and SAFE_REVISION_PATTERN.fullmatch(expected_revision)
    )
    revision_mismatch = (
        not expected_revision_valid
        or any(revision != expected_revision for revision in revisions)
    )
    if (
        invalidated_by
        or duplicates
        or missing_gate_count
        or len(revisions) != 1
        or revision_mismatch
        or evidence_unverified
    ):
        reasons: list[str] = []
        if invalidated_by:
            reasons.append("RETEST_GATE_FAILED")
        if duplicates:
            reasons.append("RETEST_DUPLICATE_GATE")
        if missing_gate_count:
            reasons.append("RETEST_GATE_SET_INCOMPLETE")
        if len(revisions) != 1 or revision_mismatch:
            reasons.append("RETEST_REVISION_UNVERIFIED")
        if evidence_unverified:
            reasons.append("RETEST_EVIDENCE_UNVERIFIED")
        return {
            "action": "lane-retest-policy",
            "prior_pass_evidence_valid": False,
            "requires_full_retest": True,
            "retest_scope": "current-revision",
            "invalidated_by": sorted(invalidated_by),
            "mixed_revisions": sorted(revisions) if len(revisions) != 1 else [],
            "reason_codes": reasons,
        }
    return {
        "action": "lane-retest-policy",
        "prior_pass_evidence_valid": True,
        "requires_full_retest": False,
        "retest_scope": None,
        "invalidated_by": [],
        "mixed_revisions": [],
        "reason_codes": ["RETEST_EVIDENCE_VERIFIED"],
    }


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Deterministic lane evaluation without profiles, provider calls, "
            "readiness checks or state writes"
        )
    )
    parser.add_argument("--request-file", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        request = load_bounded_request_file(args.request_file)
        decision = evaluate(request)
    except LanePolicyError:
        sys.stderr.buffer.write(
            (
                canonical_json(
                    {
                        "action": "lane-policy",
                        "status": "REFUSED",
                        "error": "REQUEST_REFUSED",
                        "writes_performed": 0,
                    }
                )
                + "\n"
            ).encode("ascii")
        )
        return 2
    except (OSError, UnicodeError, ValueError):
        sys.stderr.buffer.write(
            (
                canonical_json(
                    {
                        "action": "lane-policy",
                        "status": "REFUSED",
                        "error": "REQUEST_UNREADABLE",
                        "writes_performed": 0,
                    }
                )
                + "\n"
            ).encode("ascii")
        )
        return 2
    sys.stdout.buffer.write((canonical_json(decision) + "\n").encode("ascii"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
