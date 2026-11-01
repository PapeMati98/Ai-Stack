"""Typed guards for ZCode Desktop external-CU observations and lease proofs.

Pure validation only. No Wake, no local store, no UI or CU invocation.
"""

from __future__ import annotations

import math
import re
from types import MappingProxyType
from typing import Any, Mapping

USER_IDLE_REQUIRED_S = 120
OBSERVATION_SCHEMA = "zcode-desktop-app-observation/v1"
OBSERVATION_SOURCE = "zcode_desktop_host"
OBSERVATION_MAX_AGE_S = 60
FUTURE_SKEW_S = 5
MAX_LEASE_TTL_S = 180
DESKTOP_INPUT_RESOURCE = "data:desktop-input"

_HEX64_RE = re.compile(r"^[0-9a-f]{64}\Z")

_OBS_FIELDS = frozenset(
    {
        "schema",
        "source",
        "sampled_at",
        "observed_at",
        "user_idle_s",
        "app_activity",
        "draft",
        "lock",
        "quota",
        "cu_lease_owner",
        "cu_lease_binding_id",
        "native_session_id",
        "project_slug",
        "workspace",
    }
)

APP_ACTIVITY_OK = "idle"
APP_ACTIVITY_BUSY = frozenset({"busy", "active", "typing", "running"})
APP_ACTIVITY_UNKNOWN = frozenset({"unknown", "unavailable", ""})
QUOTA_OK = "ok"
QUOTA_BLOCKED = frozenset({"exhausted", "blocked", "denied"})
QUOTA_UNKNOWN = frozenset({"unknown", "unavailable", ""})

CLASSIFICATION_CODES: Mapping[str, str] = MappingProxyType(
    {
        "idle": "idle_ok",
        "invalid": "invalid_observation",
        "stale": "stale_observation",
        "future": "future_observation",
        "idle_absent": "idle_observation_invalid",
        "idle_invalid": "idle_observation_invalid",
        "idle_below_threshold": "idle_below_threshold",
        "busy": "target_busy",
        "draft": "target_draft",
        "lock": "target_lock",
        "quota": "target_quota",
        "unknown": "target_unknown",
        "mismatch": "target_mismatch",
        "source_mismatch": "source_mismatch",
        "cu_lease_foreign": "cu_lease_foreign",
    }
)


def _unix_time(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _fresh_window(stamp: float, *, now: float) -> str:
    age = now - stamp
    if age >= OBSERVATION_MAX_AGE_S:
        return "stale"
    if age < -FUTURE_SKEW_S:
        return "future"
    return "ok"


def classify_observation(
    observation: Mapping[str, Any],
    *,
    native_session_id: str,
    project_slug: str,
    workspace: str,
    binding_id: str,
    now: float,
) -> dict[str, Any]:
    """Classify one strict host observation before any wake-relay-claim.

    The idle policy threshold is fixed at :data:`USER_IDLE_REQUIRED_S` and has
    no target or configuration override. Absent, unknown, bool, fractional,
    stale, future and below-threshold samples are refused here.
    """
    if type(observation) is not dict:
        return {"kind": "invalid", "code": CLASSIFICATION_CODES["invalid"]}
    if "user_idle_s" not in observation:
        return {"kind": "idle_absent", "code": CLASSIFICATION_CODES["idle_absent"]}
    if set(observation) != _OBS_FIELDS:
        return {"kind": "invalid", "code": CLASSIFICATION_CODES["invalid"]}
    if observation.get("schema") != OBSERVATION_SCHEMA:
        return {"kind": "invalid", "code": CLASSIFICATION_CODES["invalid"]}
    if observation.get("source") != OBSERVATION_SOURCE:
        return {"kind": "source_mismatch", "code": CLASSIFICATION_CODES["source_mismatch"]}

    sampled = _unix_time(observation.get("sampled_at"))
    observed = _unix_time(observation.get("observed_at"))
    if sampled is None or observed is None:
        return {"kind": "invalid", "code": CLASSIFICATION_CODES["invalid"]}
    for stamp in (sampled, observed):
        window = _fresh_window(stamp, now=now)
        if window != "ok":
            return {"kind": window, "code": CLASSIFICATION_CODES[window]}

    idle_raw = observation.get("user_idle_s")
    if idle_raw is None or isinstance(idle_raw, bool):
        return {"kind": "idle_invalid", "code": CLASSIFICATION_CODES["idle_invalid"]}
    if isinstance(idle_raw, float) or not isinstance(idle_raw, int):
        # Unknown tokens, strings and fractional numbers are never idle proof.
        return {"kind": "idle_invalid", "code": CLASSIFICATION_CODES["idle_invalid"]}
    if idle_raw < USER_IDLE_REQUIRED_S:
        return {"kind": "idle_below_threshold", "code": CLASSIFICATION_CODES["idle_below_threshold"]}

    if (observation.get("native_session_id") != native_session_id
            or observation.get("project_slug") != project_slug
            or observation.get("workspace") != workspace):
        return {"kind": "mismatch", "code": CLASSIFICATION_CODES["mismatch"]}

    activity = observation.get("app_activity")
    if not isinstance(activity, str) or activity in APP_ACTIVITY_UNKNOWN:
        return {"kind": "unknown", "code": CLASSIFICATION_CODES["unknown"]}
    if activity in APP_ACTIVITY_BUSY or activity != APP_ACTIVITY_OK:
        return {"kind": "busy", "code": CLASSIFICATION_CODES["busy"]}

    draft = observation.get("draft")
    if type(draft) is not bool:
        return {"kind": "unknown", "code": CLASSIFICATION_CODES["unknown"]}
    if draft is True:
        return {"kind": "draft", "code": CLASSIFICATION_CODES["draft"]}

    lock = observation.get("lock")
    if type(lock) is not bool:
        return {"kind": "unknown", "code": CLASSIFICATION_CODES["unknown"]}
    if lock is True:
        return {"kind": "lock", "code": CLASSIFICATION_CODES["lock"]}

    quota = observation.get("quota")
    if not isinstance(quota, str) or quota in QUOTA_UNKNOWN:
        return {"kind": "unknown", "code": CLASSIFICATION_CODES["unknown"]}
    if quota in QUOTA_BLOCKED or quota != QUOTA_OK:
        return {"kind": "quota", "code": CLASSIFICATION_CODES["quota"]}

    owner = observation.get("cu_lease_owner")
    lease_binding = observation.get("cu_lease_binding_id")
    if owner is None and lease_binding is None:
        return {"kind": "idle", "code": CLASSIFICATION_CODES["idle"], "user_idle_s": idle_raw}
    if not isinstance(owner, str) or not owner or not isinstance(lease_binding, str) or not lease_binding:
        return {"kind": "unknown", "code": CLASSIFICATION_CODES["unknown"]}
    if lease_binding != binding_id:
        return {"kind": "cu_lease_foreign", "code": CLASSIFICATION_CODES["cu_lease_foreign"]}
    return {"kind": "idle", "code": CLASSIFICATION_CODES["idle"], "user_idle_s": idle_raw}


def is_opaque_lease_digest(value: Any) -> bool:
    return isinstance(value, str) and _HEX64_RE.fullmatch(value) is not None


def validate_lease_claim(
    response: Any,
    *,
    binding_id: str,
    holder: str,
) -> dict[str, Any] | None:
    """Accept only a complete server lease for this binding and authenticated holder."""
    if not isinstance(response, dict) or response.get("status") != "ok":
        return None
    lease = response.get("lease")
    digest = response.get("lease_digest")
    if not isinstance(lease, dict) or not is_opaque_lease_digest(digest):
        return None
    if lease.get("resource_id") != DESKTOP_INPUT_RESOURCE:
        return None
    if lease.get("holder_agent") != holder:
        return None
    meta = lease.get("meta")
    if not isinstance(meta, dict) or meta.get("binding_id") != binding_id:
        return None
    if meta.get("actor") != holder:
        return None
    ttl = lease.get("ttl_s")
    if isinstance(ttl, bool) or not isinstance(ttl, int) or not (1 <= ttl <= MAX_LEASE_TTL_S):
        return None
    return {"lease": dict(lease), "lease_digest": digest}
