"""Fail-open terminal run event for ClawOps Watch.

Only the closed metadata contract is sent. Office messages are untrusted hints;
the receiver must verify the run state and its own parent binding.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Mapping

import office_bridge


EVENT_SCHEMA = "clawops.child-terminal/v1"
RECEIPT_SCHEMA = "clawops.child-terminal-emission/v1"
WATCH_CHANNEL = "watch-events"
SOURCES = frozenset({"agent-graph", "fleet"})
MAX_TOTAL_SECONDS = 3.0
MAX_PATH_CHARS = 4096
CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def event_id(run_id: str, terminal_state: str) -> str:
    return hashlib.sha256((run_id + terminal_state).encode("utf-8")).hexdigest()


def _runtime_root() -> Path:
    return Path.home() / ".agents" / "runtime" / "agent-office"


def _runtime_command() -> tuple[list[str], Path] | None:
    """Resolve the same attested direct-Python Office runtime as office_bridge."""
    root = _runtime_root()
    marker_path = root / ".agent-office-runtime.json"
    office_py = root / "office.py"
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        python_executable = Path(marker["python_executable"])
        if (
            marker.get("managed_by") != "agent-office-installer"
            or marker.get("package_id") != "agent-office"
            or marker.get("schema_version") != 1
            or not office_bridge.SEMVER.fullmatch(str(marker.get("package_version", "")))
            or marker.get("tool") != "runtime"
            or not office_bridge.HEX64.fullmatch(str(marker.get("package_fingerprint", "")))
            or not office_bridge.HEX64.fullmatch(str(marker.get("source_fingerprint", "")))
            or not python_executable.is_absolute()
            or not python_executable.is_file()
            or not office_py.is_file()
        ):
            return None
        if office_bridge._runtime_source_fingerprint(root) != marker["source_fingerprint"]:
            return None
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None
    return [str(python_executable), "-B", "-I", str(office_py)], root


def _safe_component(value: Any, field: str) -> str:
    if isinstance(value, str) and value != value.strip():
        raise ValueError(field + " contains surrounding whitespace")
    return office_bridge._safe_component(value, field)


def _safe_path(value: Any, field: str) -> str:
    if not isinstance(value, (str, os.PathLike)):
        raise ValueError(field + " must be an absolute path")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(field + " must be an absolute path")
    text = str(path.resolve())
    if len(text) > MAX_PATH_CHARS or CONTROL.search(text):
        raise ValueError(field + " contains forbidden path/control data")
    if any(pattern.search(text) for pattern in office_bridge.SECRET_SHAPES):
        raise ValueError(field + " contains a secret-shaped value")
    return text


def _utc(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(field + " must be a UTC ISO 8601 timestamp")
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
    except ValueError as exc:
        raise ValueError(field + " must be a UTC ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(field + " must include a UTC offset")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parent_session(environment: Mapping[str, str]) -> tuple[str | None, str | None]:
    claude = environment.get("CLAUDE_CODE_SESSION_ID")
    if isinstance(claude, str) and claude.strip():
        return _safe_component(claude, "parent_session"), "claude"
    for name in ("CODEX_THREAD_ID", "CODEX_SESSION_ID"):
        codex = environment.get(name)
        if isinstance(codex, str) and codex.strip():
            return _safe_component(codex, "parent_session"), "codex"
    return None, None


def _parent_from_who(payload: Any) -> str | None:
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return None
    online = payload.get("online")
    if not isinstance(online, list):
        return None
    self_entries = [item for item in online if isinstance(item, dict) and item.get("self") is True]
    candidate_entries = self_entries or [item for item in online if isinstance(item, dict)]
    candidates = []
    for item in candidate_entries:
        try:
            candidates.append(_safe_component(item.get("agent"), "parent_agent"))
        except ValueError:
            continue
    return candidates[0] if len(candidates) == 1 else None


def _call(command: list[str], args: list[str], location: list[str], deadline: float, per_call: float = 0.9) -> dict[str, Any] | None:
    timeout = min(per_call, deadline - time.monotonic())
    return office_bridge._run(command, args, location, timeout)


def _receipt(identifier: str | None = None) -> dict[str, Any]:
    return {
        "schema": RECEIPT_SCHEMA,
        "event_id": identifier,
        "status": "error",
        "attempted": True,
        "channel_sent": False,
        "parent_requested": False,
        "parent_sent": False,
        "errors": [],
    }


def emit(
    run_id: str,
    source: str,
    terminal_state: str,
    result_ref: str | os.PathLike[str],
    workspace: str | os.PathLike[str],
    started_at: str,
    *,
    environment: Mapping[str, str] | None = None,
    office_home: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Send one terminal event and return a bounded receipt without raising."""
    environment = os.environ if environment is None else environment
    receipt = _receipt()
    # Kill switch for test suites and offline use: a disabled emitter never
    # touches the live Office board, so fixtures cannot leak fake events to Watch.
    if str(environment.get("AGENT_GRAPH_WATCH_EVENTS", "1")).strip() == "0":
        receipt.update(status="disabled", attempted=False)
        return receipt
    try:
        run = _safe_component(run_id, "run_id")
        state = _safe_component(terminal_state, "terminal_state")
        if source not in SOURCES:
            raise ValueError("source must be agent-graph or fleet")
        source_value = _safe_component(source, "source")
        result_path = _safe_path(result_ref, "result_ref")
        workspace_path = _safe_path(workspace, "workspace")
        began = _utc(started_at, "started_at")
        parent_session, parent_harness = _parent_session(environment)
        explicit_parent = environment.get("OFFICE_PARENT_AGENT")
        parent_agent = (
            _safe_component(explicit_parent, "parent_agent")
            if isinstance(explicit_parent, str) and explicit_parent.strip()
            else None
        )
        identifier = event_id(run, state)
        receipt = _receipt(identifier)
    except (OSError, ValueError, TypeError):
        receipt["errors"].append("invalid_input")
        return receipt

    command_info = _runtime_command()
    if command_info is None:
        receipt["errors"].append("office_runtime_unavailable")
        return receipt
    command, _runtime = command_info
    location = office_bridge._office_location(office_home)
    if location is None:
        # Never fall back to the frozen workstation store: that would split Office.
        receipt["errors"].append("office_store_unavailable")
        return receipt
    home, remote = location
    per_call = office_bridge.REMOTE_CALL_SECONDS if remote else 0.9
    deadline = time.monotonic() + (office_bridge.REMOTE_TOTAL_SECONDS if remote else MAX_TOTAL_SECONDS)

    if parent_agent is None and parent_session is not None and parent_harness is not None:
        who = _call(
            command,
            ["--harness", parent_harness, "--session-id", parent_session, "who"],
            home,
            deadline,
            per_call,
        )
        parent_agent = _parent_from_who(who)
        if parent_agent is None:
            receipt["errors"].append("parent_resolution_failed")

    observed = utc_now()
    event = {
        "schema": EVENT_SCHEMA,
        "event_id": identifier,
        "run_id": run,
        "source": source_value,
        "terminal_state": state,
        "result_ref": result_path,
        "parent_agent": parent_agent,
        "parent_session": parent_session,
        "parent_harness": parent_harness,
        "workspace": workspace_path,
        "started_at": began,
        "observed_at": observed,
    }
    receipt["event"] = event
    body = json.dumps(event, ensure_ascii=True, separators=(",", ":"))
    sender = ["--harness", source_value, "--session-id", run, "send"]
    channel = _call(
        command,
        sender + ["--channel", WATCH_CHANNEL, "--body", body, "--kind", "note"],
        home,
        deadline,
        per_call,
    )
    if isinstance(channel, dict) and channel.get("status") == "ok":
        receipt["channel_sent"] = True
    else:
        receipt["errors"].append("channel_send_failed")

    if parent_agent is not None:
        receipt["parent_requested"] = True
        direct = _call(
            command,
            sender + ["--to", parent_agent, "--body", body, "--kind", "note"],
            home,
            deadline,
            per_call,
        )
        if isinstance(direct, dict) and direct.get("status") == "ok":
            receipt["parent_sent"] = True
        else:
            receipt["errors"].append("parent_send_failed")

    required_sent = receipt["channel_sent"] and (
        not receipt["parent_requested"] or receipt["parent_sent"]
    )
    if required_sent and not receipt["errors"]:
        receipt["status"] = "sent"
    elif receipt["channel_sent"] or receipt["parent_sent"]:
        receipt["status"] = "partial"
    return receipt


__all__ = ["emit", "event_id", "utc_now"]
