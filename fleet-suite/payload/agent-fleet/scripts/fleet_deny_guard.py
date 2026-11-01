"""Check Fleet's final model against the owner tier catalog before provider I/O.

The live Graph catalog wins. A sealed package snapshot keeps known DENY rows
effective when the live file is unavailable. If neither can classify a model,
refuse the run; this policy never substitutes for Fleet's data-sharing gates.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any


class CatalogModelDenied(ValueError):
    pass


class CatalogPolicyUnavailable(ValueError):
    pass


def _catalog_path() -> Path:
    profile_home = Path(os.environ.get("USERPROFILE") or Path.home())
    return profile_home / ".agents" / "skills" / "agent-graph" / "references" / "model-catalog.json"


def _bundled_catalog_path() -> Path:
    return Path(__file__).resolve().parents[1] / "references" / "model-catalog.json"


def _read_catalog(path: Path) -> tuple[dict[tuple[str, str], dict[str, Any]], list[tuple[re.Pattern[str], str]], bool]:
    if not path.is_file() or path.is_symlink() or path.is_junction():
        raise ValueError("catalog-file-unavailable")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("models"), list):
        raise ValueError("catalog-schema-invalid")
    exact: dict[tuple[str, str], dict[str, Any]] = {}
    for row in data["models"]:
        if not isinstance(row, dict) or row.get("tier") not in {"W", "K", "M", "D", "DENY"} or not isinstance(row.get("profiles"), list):
            raise ValueError("catalog-row-invalid")
        if row["tier"] == "DENY" and not row.get("deny_reason"):
            raise ValueError("catalog-deny-reason-missing")
        for profile in row["profiles"]:
            if not isinstance(profile, dict) or not isinstance(profile.get("agent"), str) or not isinstance(profile.get("model"), str):
                raise ValueError("catalog-profile-invalid")
            key = (profile["agent"], profile["model"])
            if not all(key) or key in exact:
                raise ValueError("catalog-profile-duplicate-or-empty")
            exact[key] = row
    policy = data.get("policy", {})
    if not isinstance(policy, dict) or not isinstance(policy.get("deny_model_patterns", []), list):
        raise ValueError("catalog-pattern-policy-invalid")
    patterns: list[tuple[re.Pattern[str], str]] = []
    pattern_invalid = False
    for rule in policy.get("deny_model_patterns", []):
        if not isinstance(rule, dict) or not isinstance(rule.get("pattern"), str) or not isinstance(rule.get("reason"), str) or not rule["reason"]:
            pattern_invalid = True
            continue
        try:
            patterns.append((re.compile(rule["pattern"], re.IGNORECASE), rule["reason"]))
        except re.error:
            pattern_invalid = True
    return exact, patterns, pattern_invalid


def _decide(path: Path, agent: str, model: str, source: str) -> dict[str, Any]:
    exact, patterns, pattern_invalid = _read_catalog(path)
    entry = exact.get((agent, model))
    if entry is None:
        names = {model, model.rsplit("/", 1)[-1].strip().casefold()}
        for pattern, reason in patterns:
            if any(pattern.search(name) for name in names):
                return {"status": "denied", "reason": reason[:200], "source": source,
                        "warning": "invalid-pattern-skipped" if pattern_invalid else None}
        if pattern_invalid or source == "bundled":
            return {"status": "unavailable", "reason": "model-unclassified-with-incomplete-policy", "source": source}
        return {"status": "unclassified", "reason": "model-not-in-catalog", "source": source}
    warning = "invalid-pattern-skipped" if pattern_invalid else None
    if entry["tier"] == "DENY":
        return {"status": "denied", "reason": str(entry["deny_reason"])[:200], "source": source, "warning": warning}
    return {"status": "allowed", "tier": entry["tier"], "source": source, "warning": warning}


def _graph_module_path() -> Path:
    """Compatibility helper for older isolated tests; not used to run code."""
    return _catalog_path().parent.parent / "scripts" / "model_tiers.py"


def catalog_decision(
    agent: str,
    model: str,
    *,
    graph_module_path: Path | None = None,
    catalog_path: Path | None = None,
    fallback_catalog_path: Path | None = None,
) -> dict[str, Any]:
    """Read catalog data only; never import or execute the Graph loader."""
    if not isinstance(agent, str) or not agent or not isinstance(model, str) or not model:
        return {"status": "unavailable", "reason": "missing-model-identity", "source": None}
    # graph_module_path is accepted only for compatibility with old tests. It
    # cannot override the policy file or cause execution of foreign code.
    _ = graph_module_path
    primary = Path(catalog_path) if catalog_path is not None else _catalog_path()
    fallback = Path(fallback_catalog_path) if fallback_catalog_path is not None else _bundled_catalog_path()
    try:
        return _decide(primary, agent, model, "live")
    except (OSError, ValueError, TypeError):
        pass
    try:
        decision = _decide(fallback, agent, model, "bundled")
    except (OSError, ValueError, TypeError):
        return {"status": "unavailable", "reason": "live-and-bundled-catalog-unavailable", "source": None}
    if decision["status"] != "unavailable":
        decision["warning"] = "live-catalog-unavailable;bundled-policy-used"
    return decision


def require_catalog_allowed(agent: str, model: str, **kwargs: Any) -> dict[str, Any]:
    result = catalog_decision(agent, model, **kwargs)
    if result["status"] == "denied":
        raise CatalogModelDenied(
            f"Fleet model {model} for {agent} is DENY in the {result['source']} tier catalog: "
            + result["reason"]
        )
    if result["status"] == "unavailable":
        raise CatalogPolicyUnavailable(
            f"Fleet tier policy unavailable for {agent}/{model}: " + result["reason"]
        )
    return result
