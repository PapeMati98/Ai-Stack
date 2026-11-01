"""Read verified terminal Fleet handoffs without exporting prompt material.

Fleet receipts are a bounded bridge for provider runs that do not expose a
native session store. The session value emitted here is explicitly logical;
it must never be presented as a provider-native session identifier.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from .filter import contains_secret, has_reasoning_marker, \
    looks_like_raw_tool_dump


MAX_RECEIPT_BYTES = 2 * 1024 * 1024
MAX_SUMMARY_BYTES = 12_000
MAX_RISKS = 16
MAX_RISK_BYTES = 1_000
MAX_RUN_ID = 128
MAX_TOOL = 32
MAX_METADATA_BYTES = 2_048
LOGICAL_SESSION_PREFIX = "logical:fleet-receipt:"


@dataclass(frozen=True)
class ReceiptInspection:
    """Sanitized receipt outcome used by maintenance for visible reporting."""

    event: Optional[dict[str, Any]]
    status: str  # accepted | rejected | error
    reason: str


def _inspection(event: Optional[dict[str, Any]], status: str,
                reason: str) -> ReceiptInspection:
    return ReceiptInspection(event=event, status=status, reason=reason)


def _text(value: Any, limit: int, *, allow_lines: bool = False
          ) -> Optional[str]:
    if not isinstance(value, str) or not value:
        return None
    if len(value.encode("utf-8", errors="replace")) > limit or "\0" in value:
        return None
    for char in value:
        if ord(char) < 32 and (not allow_lines or char not in "\r\n\t"):
            return None
    return value


def _safe_metadata(value: Any, limit: int = MAX_METADATA_BYTES
                   ) -> Optional[str]:
    text = _text(value, limit)
    if text is None:
        return None
    if (contains_secret(text) or has_reasoning_marker(text)
            or looks_like_raw_tool_dump(text)):
        return None
    return text


def _is_reparse(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        checker = getattr(path, "is_junction", None)
        return bool(checker and checker())
    except OSError:
        return True


def _read_bounded(path: Path) -> tuple[Optional[bytes], str]:
    if not isinstance(path, Path):
        path = Path(path)
    if _is_reparse(path):
        return None, "reparse_path"
    try:
        if not path.is_file():
            return None, "source_unavailable"
        with path.open("rb") as handle:
            data = handle.read(MAX_RECEIPT_BYTES + 1)
    except (OSError, ValueError):
        return None, "source_read_error"
    if len(data) > MAX_RECEIPT_BYTES:
        return None, "receipt_too_large"
    return data, ""


def inspect_receipt(path: Path) -> ReceiptInspection:
    """Return a redacted terminal event or a stable rejection reason.

    Unknown top-level receipt keys are ignored. In particular, prompt,
    stdout, credentials, and arbitrary transport payloads never enter the
    returned event.
    """
    data, read_reason = _read_bounded(path)
    if data is None:
        status = "rejected" if read_reason in {"reparse_path",
                                                "receipt_too_large"} \
            else "error"
        return _inspection(None, status, read_reason)
    try:
        value = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _inspection(None, "rejected", "invalid_json")
    if not isinstance(value, Mapping):
        return _inspection(None, "rejected", "invalid_shape")

    if value.get("run_status") != "COMPLETED":
        return _inspection(None, "rejected", "run_not_completed")
    if value.get("handoff_valid") is not True:
        return _inspection(None, "rejected", "handoff_invalid")
    exit_code = value.get("exit_code")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool) \
            or exit_code != 0:
        return _inspection(None, "rejected", "nonzero_exit")
    timed_out = value.get("timed_out", False)
    if not isinstance(timed_out, bool):
        return _inspection(None, "rejected", "timeout_flag_invalid")
    if timed_out:
        return _inspection(None, "rejected", "timed_out")

    transport = value.get("transport_outcome")
    if not isinstance(transport, Mapping) \
            or transport.get("status") != "complete":
        return _inspection(None, "rejected", "transport_incomplete")
    identity = value.get("model_identity")
    if not isinstance(identity, Mapping) \
            or identity.get("status") not in {"verified",
                                                "verified_unknown_usage"}:
        return _inspection(None, "rejected", "model_unverified")

    handoff = value.get("handoff")
    if not isinstance(handoff, Mapping) \
            or handoff.get("status") != "completed":
        return _inspection(None, "rejected", "handoff_missing")
    summary = _text(handoff.get("summary"), MAX_SUMMARY_BYTES,
                    allow_lines=True)
    if summary is None:
        return _inspection(None, "rejected", "summary_invalid")
    risks = handoff.get("risks", [])
    if not isinstance(risks, list) or len(risks) > MAX_RISKS:
        return _inspection(None, "rejected", "risks_invalid")
    safe_risks: list[str] = []
    for risk in risks:
        safe = _text(risk, MAX_RISK_BYTES, allow_lines=False)
        if safe is None:
            return _inspection(None, "rejected", "risk_invalid")
        safe_risks.append(safe)
    text = summary
    if safe_risks:
        text += "\nReported limitations: " + "; ".join(safe_risks)
    if (len(text.encode("utf-8")) > MAX_SUMMARY_BYTES
            or contains_secret(text) or has_reasoning_marker(text)
            or looks_like_raw_tool_dump(text)):
        return _inspection(None, "rejected", "summary_unsafe_or_too_large")

    run_id = _text(value.get("run_id"), MAX_RUN_ID)
    tool = _text(value.get("adapter"), MAX_TOOL)
    completed = _text(value.get("completed_at"), 64)
    if run_id is None:
        return _inspection(None, "rejected", "run_id_invalid")
    if tool is None or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", tool):
        return _inspection(None, "rejected", "adapter_invalid")
    if completed is None:
        return _inspection(None, "rejected", "completed_at_invalid")
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", run_id):
        return _inspection(None, "rejected", "run_id_invalid")

    provider_session_id = value.get("provider_session_id")
    if provider_session_id is not None \
            and _safe_metadata(provider_session_id, 256) is None:
        return _inspection(None, "rejected", "provider_session_id_invalid")
    workspace = value.get("workspace")
    if workspace is not None and _text(workspace, MAX_METADATA_BYTES) is None:
        return _inspection(None, "rejected", "workspace_invalid")
    model = identity.get("model", value.get("model"))
    if model is not None and _safe_metadata(model, 256) is None:
        return _inspection(None, "rejected", "model_metadata_invalid")

    try:
        source_path = str(path)
    except (OSError, ValueError):
        return _inspection(None, "error", "source_path_error")
    if _text(source_path, MAX_METADATA_BYTES) is None:
        return _inspection(None, "rejected", "source_path_invalid")

    # The normalized-event field is required by the downstream contract, but
    # this value is deliberately a logical Fleet identity rather than a
    # claimed native provider session.
    event: dict[str, Any] = {
        "tool": tool,
        "native_session_id": LOGICAL_SESSION_PREFIX + run_id,
        "native_session_id_kind": "logical",
        "logical_session_id": LOGICAL_SESSION_PREFIX + run_id,
        "cursor": run_id,
        "observed_at": completed,
        "completed_at": completed,
        "completion_kind": "verified_fleet_handoff",
        "title": "Terminal " + tool + " report",
        "excerpt": text,
        "classification": "internal",
        "source_ref": {
            "path_or_store": source_path,
            "table_or_stream": "handoff.summary+risks",
        },
        "source_scope": "fleet_executions_only",
    }
    if provider_session_id is not None:
        event["provider_session_id"] = provider_session_id
    if workspace is not None:
        event["cwd"] = workspace
    if model is not None:
        event["model"] = model
    return _inspection(event, "accepted", "")


def read_receipt(path: Path) -> dict[str, Any] | None:
    """Compatibility wrapper returning only accepted sanitized events."""
    return inspect_receipt(path).event


__all__ = [
    "LOGICAL_SESSION_PREFIX",
    "MAX_RECEIPT_BYTES",
    "MAX_SUMMARY_BYTES",
    "ReceiptInspection",
    "inspect_receipt",
    "read_receipt",
]
