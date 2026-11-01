"""Session JSONL ingestion.

Reads allowlisted session JSONL: Codex / OpenClaw / Claude native schemas or
normalized events. Other schemas are reported separately and skipped. The
reader preserves ``source``, ``tool``, ``native_session_id``, and ``cursor``
provenance so downstream stages can build faithful content references.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple


SOURCE_NATIVE_SCHEMA = "native"
SOURCE_NORMALIZED = "normalized"
SOURCE_UNSUPPORTED = "unsupported"

SUPPORTED_NATIVE_TOOLS = ("codex", "openclaw", "claude")


@dataclass(frozen=True)
class RawRecord:
    """One line read from an allowlisted session JSONL."""

    identity_tool: str
    source: str            # SOURCE_NATIVE_SCHEMA | SOURCE_NORMALIZED
    native_session_id: str
    cursor: str
    observed_at: str
    title: str
    body: str
    deleted: bool = False
    classification: str = "internal"  # "internal" | "restricted"
    speaker: str = ""  # "" | "user" — explicit source speaker for synthesis
    event_type: str = ""  # e.g. completed_turn.v1 | user_message.v1
    line_no: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IngestStats:
    files_total: int = 0
    files_read: int = 0
    lines_total: int = 0
    lines_admitted: int = 0
    lines_skipped_unsupported: int = 0
    lines_skipped_blank: int = 0
    lines_skipped_invalid: int = 0
    lines_skipped_restricted: int = 0
    lines_skipped_deleted: int = 0
    unsupported_tools: Tuple[str, ...] = ()


def _nonempty_str(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s or "\0" in s:
        return None
    return s


def _detect_native_tool(payload: Dict[str, Any]) -> Optional[str]:
    """Detect Codex / OpenClaw / Claude native schema by structural cues."""
    if not isinstance(payload, dict):
        return None
    # Codex CLI: top-level {"type":"session","timestamp":...,"session_id":...}
    # or {"type":"response","session_id":...}
    ptype = _nonempty_str(payload.get("type"))
    if ptype in ("session", "response", "event", "message", "assistant",
                 "user") and _nonempty_str(payload.get("session_id")):
        # Heuristic: Codex and Claude both emit session-scoped events. The
        # tool name is resolved from the explicit tool marker when present;
        # otherwise we default to "openclaw" when the run.model.configured
        # shape appears, "codex" when response/codex_* fields appear, or
        # "claude" for assistant/user pairings with Anthropic headers.
        if _nonempty_str(payload.get("run_id")) and ptype in (
                "session", "event"):
            return "openclaw"
        if _nonempty_str(payload.get("codex_request_id")) or \
                _nonempty_str(payload.get("codex_run_id")):
            return "codex"
        if _nonempty_str(payload.get("anthropic_request_id")) or \
                ptype in ("assistant", "user"):
            return "claude"
        return None
    # Codex native session_meta / response_item / event_msg
    if ptype in ("session_meta", "response_item", "event_msg"):
        return "codex"
    # OpenClaw JSONL: payload_type + nested payload (muse canary shape).
    if _nonempty_str(payload.get("payload_type")) and isinstance(
            payload.get("payload"), dict):
        return "openclaw"
    return None


def _normalize(payload: Dict[str, Any], tool: str, line_no: int) -> Optional[
        RawRecord]:
    """Map a native payload to a RawRecord or return None to skip."""
    ptype = _nonempty_str(payload.get("type"))
    native_id = _nonempty_str(payload.get("id")) or _nonempty_str(
        payload.get("message_id")) or ""
    session_id = (_nonempty_str(payload.get("session_id"))
                  or _nonempty_str(payload.get("native_session_id"))
                  or _nonempty_str(payload.get("conversation_id"))
                  or "")
    observed = (_nonempty_str(payload.get("timestamp"))
                or _nonempty_str(payload.get("created_at"))
                or _nonempty_str(payload.get("observed_at"))
                or "")
    cursor = _nonempty_str(payload.get("cursor")) or _nonempty_str(
        payload.get("seq"))

    # Pre-flight: skip still-active / uncompleted turns for the real Codex
    # native schemas. The session may end mid-write; an unfinished item
    # would otherwise be admitted as an incomplete fact.
    if tool == "codex" and ptype in ("response_item", "event_msg",
                                       "session_meta"):
        status = _nonempty_str(payload.get("status"))
        if status and status.lower() in {"active", "in_progress",
                                          "streaming", "pending"}:
            return None
        # Reasoning-only items, tool-output items, and tool-call items
        # never become facts. They are useful for replay but must never
        # leak into the prompt or synthesis.
        if ptype == "response_item":
            rtype = _nonempty_str(payload.get("role"))
            if rtype and rtype.lower() in {"reasoning", "tool", "tool_call",
                                            "tool_result"}:
                return None
        if ptype == "event_msg":
            event_kind = _nonempty_str(payload.get("kind"))
            if event_kind and event_kind.lower() in {
                "tool_started", "tool_completed", "tool_failed",
                "token_count", "agent_reasoning", "agent_thinking",
            }:
                return None

    # OpenClaw stores identity under ``run_stream.id`` and the cursor under
    # ``command_id``. Recover them when present so the canonical canary
    # fixture is admitted instead of silently dropped. The cursor is
    # disambiguated by ``payload_type`` so multiple events that share the
    # same command_id (e.g. ``run.terminal.completed``) remain distinct.
    if tool == "openclaw":
        # The native OpenClaw JSONL dialect nests run_stream and command_id
        # inside payload (the canary fixture follows this shape). Accept a
        # top-level copy too for older normalized captures.
        pl = payload.get("payload")
        run_stream = (pl.get("run_stream") if isinstance(pl, dict)
                      else None) or payload.get("run_stream")
        if isinstance(run_stream, dict) and not session_id:
            session_id = _nonempty_str(run_stream.get("id")) or ""
        if isinstance(pl, dict) and not cursor:
            cmd = _nonempty_str(pl.get("command_id"))
            payload_type = _nonempty_str(payload.get("payload_type"))
            if cmd and payload_type:
                cursor = f"{cmd}:{payload_type}"
            elif cmd:
                cursor = cmd
    elif tool == "claude" and not cursor:
        # Claude events typically only carry ``anthropic_request_id`` and
        # ``message_id``; treat either as the canonical cursor.
        cursor = (_nonempty_str(payload.get("anthropic_request_id"))
                  or _nonempty_str(payload.get("message_id"))
                  or "")
    elif tool == "codex" and not cursor:
        # Codex emits ``codex_run_id`` / ``codex_request_id`` which together
        # uniquely identify one assistant/user turn in a session.
        run_id = _nonempty_str(payload.get("codex_run_id"))
        req_id = _nonempty_str(payload.get("codex_request_id"))
        if run_id and req_id:
            cursor = f"{run_id}:{req_id}"
        elif run_id:
            cursor = run_id
        elif req_id:
            cursor = req_id
        # Real Codex native schemas carry their own stable ids. Prefer those
        # so the cursor reflects the actual turn and is replay-stable.
        if ptype == "session_meta" and native_id:
            cursor = f"session_meta:{native_id}"
        elif ptype == "response_item" and native_id:
            cursor = f"response_item:{native_id}"
        elif ptype == "event_msg" and _nonempty_str(payload.get("id")):
            cursor = f"event_msg:{native_id}"

    title = _nonempty_str(payload.get("title")) or ptype or tool

    # Body extraction differs per tool; we deliberately pull *content* text,
    # never reasoning traces or raw tool dumps.
    body = ""
    if tool == "openclaw":
        pl = payload.get("payload") if isinstance(payload.get("payload"),
                                                  dict) else None
        if pl:
            text = pl.get("text")
            if _nonempty_str(text):
                body = text  # type: ignore[assignment]
            else:
                # For run.model.configured, fall back to display_label.
                label = pl.get("display_label")
                if _nonempty_str(label):
                    body = label  # type: ignore[assignment]
    if tool == "codex" and ptype in ("session_meta", "response_item",
                                       "event_msg"):
        # Real Codex schemas carry a structured ``content`` array.  Walk it
        # and extract only assistant-visible text (skip reasoning/tool items).
        content_field = payload.get("content")
        if isinstance(content_field, list):
            pieces: List[str] = []
            for entry in content_field:
                if not isinstance(entry, dict):
                    continue
                etype = _nonempty_str(entry.get("type"))
                if etype and etype.lower() in {
                    "reasoning", "tool_use", "tool_result",
                    "tool_call", "tool_calls",
                }:
                    continue
                text = entry.get("text") or entry.get("content")
                if _nonempty_str(text):
                    pieces.append(text)  # type: ignore[arg-type]
            body = "\n".join(pieces)
        elif isinstance(content_field, str):
            body = content_field
        # session_meta and event_msg without content leave body empty.
    if not body and isinstance(payload.get("content"), str):
        body = payload["content"]
    if not body and isinstance(payload.get("message"), str):
        body = payload["message"]
    if not body and isinstance(payload.get("text"), str):
        body = payload["text"]

    classification = (_nonempty_str(payload.get("classification"))
                      or "internal").lower()

    if not session_id or not observed or not cursor:
        return None

    return RawRecord(
        identity_tool=tool,
        source=SOURCE_NATIVE_SCHEMA,
        native_session_id=session_id,
        cursor=cursor,
        observed_at=observed,
        title=title,
        body=body,
        deleted=bool(payload.get("deleted", False)),
        classification=classification,
        line_no=line_no,
        raw=payload,
    )


def _normalize_normalized_event(payload: Dict[str, Any], tool: str,
                                line_no: int) -> Optional[RawRecord]:
    """Map a normalized event (already shaped) to a RawRecord."""
    if not isinstance(payload, dict):
        return None
    session_id = _nonempty_str(payload.get("native_session_id"))
    cursor = _nonempty_str(payload.get("cursor"))
    observed = _nonempty_str(payload.get("observed_at"))
    title = _nonempty_str(payload.get("title")) or "normalized"
    body = payload.get("excerpt") or payload.get("body") or ""
    speaker = payload.get("speaker") if isinstance(payload.get("speaker"), str) else ""
    speaker = speaker.strip().lower()
    event_type = payload.get("event_type") if isinstance(payload.get("event_type"), str) else ""
    event_type = event_type.strip()[:64]
    if event_type == "owner_document.v1":
        if speaker in ("assistant", "user", ""):
            speaker = "owner_document"
    elif speaker not in ("user", "assistant"):
        speaker = "user" if event_type == "user_message.v1" else ""
    if not isinstance(body, str):
        return None
    classification = (_nonempty_str(payload.get("classification"))
                      or "internal").lower()
    if not session_id or not observed or not cursor:
        return None
    return RawRecord(
        identity_tool=tool,
        source=SOURCE_NORMALIZED,
        native_session_id=session_id,
        cursor=cursor,
        observed_at=observed,
        title=title,
        body=body,
        deleted=bool(payload.get("deleted", False)),
        classification=classification,
        speaker=speaker,
        event_type=event_type,
        line_no=line_no,
        raw=payload,
    )


def _stable_cursor(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def _looks_like_incomplete_json(text: str) -> bool:
    """Detect an unfinished final JSON object without repairing its bytes.

    A writer can expose a final JSONL line between writes. The caretaker must
    leave that tail unconsumed so a later run can parse it after the writer
    completes. This is deliberately only a structural check; malformed or
    ambiguous content is still skipped and never guessed into a record.
    """
    stack: List[str] = []
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            stack.append(char)
        elif char == "]":
            if not stack or stack[-1] != "[":
                return False
            stack.pop()
        elif char == "}":
            if not stack or stack[-1] != "{":
                return False
            stack.pop()
    return bool(stack) or in_string or escaped


def read_file(path: str | os.PathLike[str], declared_tool: str,
              declared_kind: str) -> Tuple[List[RawRecord], List[str],
                                           IngestStats]:
    """Read one JSONL file and return admitted records + unsupported markers.

    ``unsupported`` is a list of (line_no, observed_tool) pairs reported as
    strings for human inspection; the caller logs and continues.
    """
    stats = IngestStats(files_total=1)
    admitted: List[RawRecord] = []
    unsupported: List[str] = []

    p = Path(path)
    if not p.is_file():
        stats = IngestStats(files_total=1, files_read=0,
                            lines_skipped_invalid=1)
        return admitted, [f"missing:{path}"], stats
    stats = IngestStats(files_total=1, files_read=1)

    line_no = 0
    file_text = p.read_text("utf-8", errors="replace")
    lines = file_text.splitlines()
    for raw_line in lines:
        line_no += 1
        stats = IngestStats(**{**stats.__dict__, "lines_total":
                               stats.lines_total + 1})
        if not raw_line.strip():
            stats = IngestStats(**{**stats.__dict__, "lines_skipped_blank":
                                   stats.lines_skipped_blank + 1})
            continue
        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError:
            # Do not advance a cursor for an unfinished final object. The
            # next run rereads the file and admits it once it is complete.
            if line_no == len(lines) and _looks_like_incomplete_json(raw_line):
                continue
            stats = IngestStats(**{**stats.__dict__,
                                   "lines_skipped_invalid":
                                       stats.lines_skipped_invalid + 1})
            continue

        if declared_kind == "normalized_events":
            rec = _normalize_normalized_event(payload, declared_tool, line_no)
            if rec is None:
                stats = IngestStats(
                    **{**stats.__dict__, "lines_skipped_invalid":
                        stats.lines_skipped_invalid + 1})
                continue
        else:
            detected = _detect_native_tool(payload)
            if detected is None:
                unsupported.append(
                    f"{path}:{line_no}:unknown_schema:{declared_tool}")
                stats = IngestStats(
                    **{**stats.__dict__,
                       "lines_skipped_unsupported":
                           stats.lines_skipped_unsupported + 1})
                continue
            if detected not in SUPPORTED_NATIVE_TOOLS:
                unsupported.append(
                    f"{path}:{line_no}:unsupported_tool:{detected}")
                stats = IngestStats(
                    **{**stats.__dict__,
                       "lines_skipped_unsupported":
                           stats.lines_skipped_unsupported + 1})
                continue
            rec = _normalize(payload, detected, line_no)
            if rec is None:
                stats = IngestStats(
                    **{**stats.__dict__,
                       "lines_skipped_invalid":
                           stats.lines_skipped_invalid + 1})
                continue

        # A tombstone is a deletion signal, not content to admit.  Drop it
        # before filtering/admission so a later synthesis cannot resurrect a
        # record that the native session explicitly removed.
        if rec.deleted:
            stats = IngestStats(**{**stats.__dict__,
                                   "lines_skipped_deleted":
                                       stats.lines_skipped_deleted + 1})
            continue

        if rec.classification == "restricted":
            stats = IngestStats(**{**stats.__dict__,
                                   "lines_skipped_restricted":
                                       stats.lines_skipped_restricted + 1})
            continue

        admitted.append(rec)
        stats = IngestStats(**{**stats.__dict__, "lines_admitted":
                               stats.lines_admitted + 1})

    return admitted, unsupported, stats


def merge_stats(acc: IngestStats, add: IngestStats) -> IngestStats:
    return IngestStats(
        files_total=acc.files_total + add.files_total,
        files_read=acc.files_read + add.files_read,
        lines_total=acc.lines_total + add.lines_total,
        lines_admitted=acc.lines_admitted + add.lines_admitted,
        lines_skipped_unsupported=acc.lines_skipped_unsupported
            + add.lines_skipped_unsupported,
        lines_skipped_blank=acc.lines_skipped_blank + add.lines_skipped_blank,
        lines_skipped_invalid=acc.lines_skipped_invalid
            + add.lines_skipped_invalid,
        lines_skipped_restricted=acc.lines_skipped_restricted
            + add.lines_skipped_restricted,
        lines_skipped_deleted=acc.lines_skipped_deleted
            + add.lines_skipped_deleted,
        unsupported_tools=tuple(sorted(set(acc.unsupported_tools)
                                       | set(add.unsupported_tools))),
    )


def read_inputs(inputs: Iterable[Tuple[str, str, str]]
                ) -> Tuple[List[RawRecord], List[str], IngestStats]:
    """Read every input as ``(path, kind, tool)`` triples."""
    all_records: List[RawRecord] = []
    unsupported: List[str] = []
    stats = IngestStats()
    for path, kind, tool in inputs:
        recs, unsup, st = read_file(path, tool, kind)
        all_records.extend(recs)
        unsupported.extend(unsup)
        stats = merge_stats(stats, st)
    return all_records, unsupported, stats
