"""Fail-closed native Grok availability probe.

A single content-free, tool-free prompt on the real subscription ACP
transport, run only when quota_availability.check() already needs a
recheck (a cached exhaustion or a stale cache entry) -- never for a
fresh, never-blocked pool. Success requires proof of a genuine native
completion (exact session, exact model identity, exact assistant text,
zero tool calls, zero approved permissions, zero ambient MCP servers),
not merely that the provider process started. Anything short of that
raises ValueError so the caller's existing block stays in place.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import tempfile
import uuid
from pathlib import Path
from typing import Any

import grok_acp

PROBE_PROMPT = "Reply OK only. Do not use tools."

# Empty schema: the probe carries no task content and demands OK text only.
AVAILABILITY_HANDOFF_SCHEMA: dict[str, Any] = {}


def _fresh_run_id() -> str:
    run_id = (
        f"run-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-"
        f"{uuid.uuid4().hex[:10]}"
    )
    if not grok_acp.RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError("Generated Grok availability probe run ID is malformed")
    return run_id


def _is_exact_int(value: Any, expected: int) -> bool:
    return type(value) is int and value == expected


def _write_probe_metadata(attempt_root: Path, payload: dict[str, Any]) -> None:
    try:
        path = attempt_root / "probe-metadata.json"
        path.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
    except OSError:
        pass


def native_probe(fleet: Any, profile: dict[str, Any], root: Path) -> dict[str, Any]:
    """One bounded native Grok prompt with zero tools and no task input."""
    executable = fleet.resolve_executable(profile)
    if not executable:
        raise ValueError("Grok executable not found for availability probe")
    model = profile["models"]["low"]

    directory = Path(root) / "availability-probes"
    directory.mkdir(parents=True, exist_ok=True)
    attempt_dir = tempfile.mkdtemp(prefix="grok-availability-", dir=directory)
    attempt_root = Path(attempt_dir)
    run_id = _fresh_run_id()
    run_dir = attempt_root / "run"
    workspace = attempt_root / "empty-workspace"
    proxy_base = attempt_root / "proxy"
    run_dir.mkdir()
    workspace.mkdir()
    proxy_base.mkdir()

    env = fleet.profile_runtime_environment(
        profile, inherit_api_keys=False, native_subagent_limit=0
    )
    try:
        env = grok_acp.build_environment(env, run_dir=run_dir)
    except grok_acp.GrokAcpError as exc:
        _write_probe_metadata(
            attempt_root,
            {
                "run_id": run_id,
                "model": model,
                "status": "failed",
                "error_type": type(exc).__name__,
                "observed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
            },
        )
        raise ValueError(
            f"Grok availability probe environment failed: {exc}"
        ) from exc

    command = grok_acp.build_command(
        executable,
        run_dir=run_dir,
        run_id=run_id,
        mode="availability",
        model=model,
        reasoning_effort="low",
        complexity="low",
        handoff_schema=AVAILABILITY_HANDOFF_SCHEMA,
        max_model_tool_calls=1,
        proxy_base=proxy_base,
    )

    try:
        result = grok_acp.run_session(
            command,
            run_dir=run_dir,
            run_id=run_id,
            workspace=workspace,
            mode="availability",
            prompt=PROBE_PROMPT,
            timeout=25,
            max_model_tool_calls=1,
            max_additional_prompts=0,
            env=env,
            continuation_decider=lambda text: None,
            proxy_base=proxy_base,
            retain_runtime=True,
        )

        if result.get("quota_exhausted"):
            row: dict[str, Any] = {
                "status": "exhausted",
                "source": "native-cli-check",
                "scope": "credits",
                "reset_at": None,
                "clear_requires": "native-cli-check",
            }
            _write_probe_metadata(
                attempt_root,
                {
                    "run_id": run_id,
                    "model": model,
                    "status": "exhausted",
                    "source": "native-cli-check",
                    "scope": "credits",
                    "observed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
                },
            )
            return row

        if result.get("transport") != grok_acp.TRANSPORT_ID:
            raise ValueError("Grok availability probe used an unexpected transport")
        if result.get("timed_out"):
            raise ValueError("Grok availability probe timed out")
        if not _is_exact_int(result.get("exit_code"), 0):
            raise ValueError(
                f"Grok availability probe exited non-zero: {result.get('exit_code')!r}"
            )
        if not _is_exact_int(result.get("prompt_count"), 1):
            raise ValueError("Grok availability probe did not run exactly one prompt")
        if not _is_exact_int(result.get("model_tool_calls"), 0):
            raise ValueError("Grok availability probe recorded an unexpected model tool call")

        session_id = result.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("Grok availability probe returned no session ID")

        try:
            envelope = json.loads(result.get("stdout", ""))
        except (json.JSONDecodeError, RecursionError) as exc:
            raise ValueError(
                f"Grok availability probe envelope is invalid: {exc}"
            ) from exc
        if not isinstance(envelope, dict):
            raise ValueError("Grok availability probe envelope is not an object")
        if envelope.get("sessionId") != session_id:
            raise ValueError("Grok availability probe session ID mismatch")
        if envelope.get("stopReason") != "end_turn":
            raise ValueError("Grok availability probe did not report end_turn")

        identity = fleet.grok_model_identity(
            result.get("stdout", ""), expected_model=model
        )
        if identity.get("status") != "verified":
            raise ValueError(
                "Grok availability probe model identity "
                f"{identity.get('status')}: {identity.get('error')}"
            )

        text = envelope.get("text")
        if text != "OK":
            raise ValueError(
                "Grok availability probe assistant text was not exactly 'OK'"
            )

        fleet_meta = envelope.get("_fleet")
        if not isinstance(fleet_meta, dict):
            raise ValueError("Grok availability probe envelope missing _fleet metadata")
        if not _is_exact_int(fleet_meta.get("tool_calls"), 0):
            raise ValueError("Grok availability probe recorded an unexpected tool call")
        if not _is_exact_int(fleet_meta.get("permission_requests_approved"), 0):
            raise ValueError("Grok availability probe approved a tool permission")
        if not _is_exact_int(fleet_meta.get("ambient_mcp_servers"), 0):
            raise ValueError("Grok availability probe observed ambient MCP servers")

        row = {
            "status": "available",
            "source": "native-cli-check",
            "covered_scopes": ["credits"],
        }
        usage = envelope.get("usage")
        if isinstance(usage, dict):
            total = usage.get("total_tokens", usage.get("totalTokens"))
            if (
                isinstance(total, (int, float))
                and not isinstance(total, bool)
                and math.isfinite(total)
                and total > 0
            ):
                row["probe_tokens"] = int(total) if isinstance(total, int) else total

        meta: dict[str, Any] = {
            "run_id": run_id,
            "model": model,
            "status": "available",
            "source": "native-cli-check",
            "observed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        if "probe_tokens" in row:
            meta["probe_tokens"] = row["probe_tokens"]
        _write_probe_metadata(attempt_root, meta)
        return row
    except Exception as exc:
        _write_probe_metadata(
            attempt_root,
            {
                "run_id": run_id,
                "model": model,
                "status": "failed",
                "error_type": type(exc).__name__,
                "observed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
            },
        )
        raise
