"""Dry-run and status reporter."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from .config import CaretakerConfig
from .store import Store


def dry_run_plan(config: CaretakerConfig) -> Dict[str, Any]:
    """Describe the planned pipeline without touching the store."""
    inputs = []
    for spec in config.inputs:
        p = Path(spec.path)
        inputs.append({
            "path": spec.path,
            "kind": spec.kind,
            "tool": spec.tool,
            "exists": p.is_file(),
            "size_bytes": (p.stat().st_size if p.is_file() else 0),
        })
    return {
        "dry_run": True,
        "project_slug": config.project_slug,
        "agent": config.agent,
        "session_id": config.session_id,
        "state_dir": config.state_dir,
        "allowlist": {
            "tools": list(config.allowlist.tools),
            "max_body_bytes": config.allowlist.max_body_bytes,
            "max_excerpt_bytes": config.allowlist.max_excerpt_bytes,
        },
        "primary": {
            "kind": config.primary.kind,
            "executable": config.primary.executable,
            "argv": list(config.primary.argv),
            "allowed_backends": list(config.primary.allowed_backends),
            "allowed_models": list(config.primary.allowed_models),
            "subscription_only": config.primary.subscription_only,
        },
        "fallback": (None if config.fallback is None else {
            "kind": config.fallback.kind,
            "executable": config.fallback.executable,
            "argv": list(config.fallback.argv),
            "allowed_backends": list(config.fallback.allowed_backends),
            "allowed_models": list(config.fallback.allowed_models),
            "subscription_only": config.fallback.subscription_only,
            "forbidden_backends": list(config.fallback.forbidden_backends),
        }),
        "hub": {
            "require_context": config.hub.require_context,
            "min_health": config.hub.min_health,
            "min_lint": config.hub.min_lint,
        },
        "office": {
            "rate_per_minute": config.office.rate_per_minute,
            "dedup": config.office.dedup,
            "no_autoplay_reply": config.office.no_autoplay_reply,
            "session_id": config.office.session_id,
        },
        "limits": {
            "max_records_per_run": config.limits.max_records_per_run,
            "max_prompt_bytes": config.limits.max_prompt_bytes,
            "incident_cooldown_seconds":
                config.limits.incident_cooldown_seconds,
        },
        "inputs": inputs,
    }


def status_report(store: Store) -> Dict[str, Any]:
    """Render a JSON status report from the durable store."""
    summary = store.run_summary()
    pending = store.outbox_pending()
    summary["outbox_pending"] = [
        {"dedup_id": r.dedup_id, "status": r.status, "reason": r.reason,
         "recorded_at": r.recorded_at}
        for r in pending
    ]
    return summary
