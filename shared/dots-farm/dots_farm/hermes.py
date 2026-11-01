"""Hermes supervisor candidate configuration planner.

Returns declarative candidate dicts only. Never clones an existing profile,
loads credentials, touches Robert, or installs/enables services. Live Hermes
runtime compatibility is UNQUALIFIED until bounded live tests.
"""

import math
import re
from pathlib import Path

from .adapters import _valid_model_id

_SAFE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SUPERVISOR_BUDGET_DEFAULT = 1000000


def _dot_id(dot_config):
    if not isinstance(dot_config, dict):
        raise ValueError("hermes-bad-config")
    dot_id = dot_config.get("id") or dot_config.get("dot_id")
    if not isinstance(dot_id, str) or not _SAFE_RE.fullmatch(dot_id):
        raise ValueError("hermes-bad-dot-id")
    return dot_id


def profile_plan(dot_config, base_path):
    """Plan a NEW per-dot HERMES_HOME. Single writer, fresh memory, no legacy."""
    dot_id = _dot_id(dot_config)
    if not isinstance(base_path, str) or not base_path:
        raise ValueError("hermes-bad-base-path")
    home = str(Path(base_path) / ("hermes-%s" % dot_id))
    model = dot_config.get("model", "synthetic")
    if not _valid_model_id(model):
        raise ValueError("hermes-bad-model")
    kind = dot_config.get("kind", "observer")
    if not isinstance(kind, str) or kind not in ("observer", "collector", "supervisor"):
        raise ValueError("hermes-bad-kind")
    interval = dot_config.get("interval_seconds", 900)
    if (isinstance(interval, bool) or not isinstance(interval, (int, float))
            or interval <= 0):
        raise ValueError("hermes-bad-interval")
    try:
        if not math.isfinite(interval):
            raise ValueError("hermes-bad-interval")
    except OverflowError:
        raise ValueError("hermes-bad-interval") from None
    secret_ref = dot_config.get("secret_reference", "dots-secret-%s" % dot_id)
    if (not isinstance(secret_ref, str) or not secret_ref.strip() or len(secret_ref) > 256
            or secret_ref == "openrouter_management_key"
            or any(ord(char) < 32 for char in secret_ref)):
        raise ValueError("hermes-bad-secret-reference")
    budget = dot_config.get("budget_micro_usd", 0)
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 0:
        raise ValueError("hermes-bad-budget")
    return {
        "candidate": True,
        "install_status": "not-installed-unqualified",
        "live_qualification": "BLOCKED",
        "hermes_home": home,
        "cloned_from_existing": False,
        "credentials_loaded": False,
        "existing_profiles_touched": [],
        "dot_id": dot_id,
        "kind": kind,
        "budget_micro_usd": budget,
        "wake": {
            "type": "cron",
            "script_only": True,
            "no_agent": True,
            "schedule": interval,
        },
        "model_pin": {"model": model, "no_global_fallback": True},
        "secret_reference": secret_ref,
        "memory_reference": str(Path(home) / "memories"),
        "global_keys_used": False,
        "memories": {"fresh": True, "imported": []},
        "writers": [dot_id],
        "supervisor_powers": {
            "suggest_cadence": True,
            "suggest_repair": True,
            "grant_tools": False,
            "change_budgets": False,
            "release_holds": False,
            "bypass_stop": False,
        },
        "notes": (
            "Candidate configuration only; Hermes acceptance, OS isolation and "
            "VM deployment remain unqualified; paid/provider/live host "
            "qualification is BLOCKED. Robert left untouched."
        ),
    }


def supervisor_plan(base_path, budget_micro_usd=_SUPERVISOR_BUDGET_DEFAULT):
    """Plan the supervisor candidate profile with a bounded synthetic budget."""
    if isinstance(budget_micro_usd, bool) or not isinstance(
        budget_micro_usd, int
    ):
        raise ValueError("hermes-bad-budget")
    if budget_micro_usd < 0:
        raise ValueError("hermes-bad-budget")
    return profile_plan(
        {
            "id": "supervisor",
            "kind": "supervisor",
            "model": "synthetic",
            "interval_seconds": 900,
            "budget_micro_usd": budget_micro_usd,
        },
        base_path,
    )
