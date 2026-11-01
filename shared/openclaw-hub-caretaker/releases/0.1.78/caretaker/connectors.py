"""Bounded, read-only connectors for completed cross-tool turns.

The caretaker's existing :mod:`caretaker.ingest` module consumes a small
``normalized_events`` JSONL contract.  This module is the boundary in front of
that reader.  It deliberately knows only seven locally observed formats:

* Codex rollout JSONL (the nested ``event_msg``/``payload`` dialect),
* Claude Code JSONL,
* Qwen Code JSONL,
* Grok JSONL,
* OpenCode SQLite,
* Hermes SQLite, and
* MiniMax local-runtime SQLite.

Collectors are terminal-only.  A source has to expose an explicit completed
turn marker and a safe, linked user record before an assistant answer is
emitted.  Tool calls/results, reasoning fields, credentials and user text are
never copied to the normalized output.  Redaction in ``filter.py`` remains the
downstream safety net; this module also drops unsafe text before it reaches the
task-owned JSONL file.

The module is intentionally independent from the pipeline and Hub.  It does
not invoke a CLI, network endpoint, model, Office, or Knowledge Hub.  SQLite
connections are opened with ``mode=ro`` and ``query_only`` and use an explicit
table/column allowlist.
"""

from __future__ import annotations
from caretaker import tick_budget

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar, Iterable, Iterator, Mapping, Sequence

try:  # The module is also useful as a directly imported script in fixtures.
    from .filter import contains_secret, has_reasoning_marker, looks_like_raw_tool_dump
except ImportError:  # pragma: no cover - fail closed if policy is unavailable.
    def contains_secret(value: str) -> bool:
        return True

    def has_reasoning_marker(value: str) -> bool:
        return True

    def looks_like_raw_tool_dump(value: str) -> bool:
        return True


SCHEMA_VERSION = 1
NORMALIZED_EVENT_TYPE = "completed_turn.v1"
CONNECTOR_SCAN_SCHEMA_VERSION = 2
DEFAULT_MAX_RECORDS = 128
DEFAULT_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_EXCERPT_BYTES = 1600
MAX_LINE_BYTES = 1024 * 1024
RAW_RECORD_MULTIPLIER = 256
MAX_RAW_RECORDS = 65_536
USER_MESSAGE_EVENT_TYPE = "user_message.v1"
USER_SCHEMA_VERSION = 2
USER_PROVENANCE = "untrusted_owner_statement"
FILE_EVENT_TYPE = "owner_document.v1"
FILE_SCHEMA_VERSION = 1
FILE_PROVENANCE = "untrusted_owner_document"
FILE_SPEAKER = "owner_document"
UNASSIGNED_PROJECT = "unassigned"
DEFAULT_FILE_EXTENSIONS: tuple[str, ...] = (".md", ".txt", ".rst", ".adoc")
DEFAULT_QUIET_MINUTES = 15
DEFAULT_MAX_FILE_BYTES = 262_144
DEFAULT_MAX_FILES_PER_TICK = 50

# These are the only sources admitted by this module.  OpenClaw itself is
# deliberately reported as unsupported below until its real terminal payload
# and linked user record are verified; the existing ingest reader remains a
# separate JSONL path.
SUPPORTED_TOOLS: tuple[str, ...] = (
    "codex",
    "claude",
    "qwen",
    "grok",
    "opencode",
    "hermes",
    "minimax",
    "files",
)

UNVERIFIED_SOURCES: tuple[str, ...] = (
    "kimi",
    "zcode",
    "gemini",
    "antigravity",
    "openclaw",
)

TERMINAL_VALUES = frozenset(
    {
        "complete",
        "completed",
        "done",
        "success",
        "succeeded",
        "finished",
        "finish",
        "end_turn",
        "stop",
        "stop_sequence",
        "terminal",
        "user_exit",
    }
)
ACTIVE_VALUES = frozenset(
    {
        "active",
        "in_progress",
        "in-progress",
        "streaming",
        "pending",
        "running",
        "started",
        "queued",
        "partial",
        "working",
    }
)
UNSAFE_FINISH_VALUES = frozenset(
    {
        "tool_use",
        "tool_calls",
        "function_call",
        "function_calls",
        "cancelled",
        "canceled",
        "error",
        "failed",
        "interrupted",
    }
)

BLOCKED_ITEM_TYPES = frozenset(
    {
        "system",
        "system_message",
        "developer",
        "developer_message",
        "environment",
        "environment_context",
        "env",
        "agents",
        "agents_md",
        "agent_context",
        "reasoning",
        "thinking",
        "thought",
        "tool",
        "tool_use",
        "tool_call",
        "tool_calls",
        "tool_result",
        "tool_results",
        "function_call",
        "function_call_output",
        "custom_tool_call",
        "custom_tool_call_output",
        "commandexecution",
        "mcp_tool_call",
        "collabagenttoolcall",
    }
)

MARKER_KEYS = (
    "status",
    "state",
    "stop_reason",
    "stopReason",
    "finish_reason",
    "finishReason",
    "completion",
    "completion_status",
    "completionStatus",
    "terminal",
    "active",
    "is_active",
    "isActive",
    "streaming",
    "completed",
    "is_final",
    "isFinal",
    "ended",
    "ended_at",
    "completed_at",
    "completedAt",
    "finish",
)

ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


@dataclass(frozen=True)
class SourceSpec:
    """An explicitly allowlisted source.

    ``path`` is supplied by the caller.  Connectors never discover broad
    user directories and never infer a path from credentials or environment
    configuration.  ``native_session_id`` is useful for exports whose stream
    itself does not repeat the session id (for example a Codex rollout).
    """

    tool: str
    path: str | os.PathLike[str]
    kind: str = "auto"  # ``jsonl`` or ``sqlite`` may be supplied explicitly.
    source_id: str | None = None
    native_session_id: str | None = None
    exclude_native_session_ids: tuple[str, ...] = ()
    exclude_caretaker_sessions: bool = True
    include_user_messages: bool = False
    max_records: int = DEFAULT_MAX_RECORDS
    max_bytes: int = DEFAULT_MAX_BYTES
    max_excerpt_bytes: int = DEFAULT_MAX_EXCERPT_BYTES
    roots: tuple[Any, ...] = ()
    extensions: tuple[str, ...] = ()
    quiet_minutes: int = DEFAULT_QUIET_MINUTES
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    max_files_per_tick: int = DEFAULT_MAX_FILES_PER_TICK
    state_dir: str = ""
    file_observation_mode: str = "mtime"
    exclude_directory_names: tuple[str, ...] = ()

    def normalized(self) -> "SourceSpec":
        tool = str(self.tool).strip().lower()
        path = str(self.path)
        kind = str(self.kind or "auto").strip().lower()
        if kind == "session_jsonl":
            kind = "jsonl"
        if kind == "opencode_sqlite" or kind == "hermes_sqlite":
            kind = "sqlite"
        if kind == "auto":
            suffix = Path(path).suffix.lower()
            kind = "sqlite" if suffix in {".db", ".sqlite", ".sqlite3"} else "jsonl"
        if kind not in {"jsonl", "sqlite", "files", "fleet_receipt"}:
            raise ValueError("source kind must be jsonl, sqlite, files, fleet_receipt, or auto")
        if kind != "files":
            if not path or "\0" in path:
                raise ValueError("source path must be non-empty and NUL-free")
        elif path and "\0" in path:
            raise ValueError("source path must be NUL-free")
        if self.max_records <= 0 or self.max_bytes <= 0 or self.max_excerpt_bytes <= 0:
            raise ValueError("source bounds must be positive")
        if self.file_observation_mode not in {"mtime", "scan_time"}:
            raise ValueError("file_observation_mode must be mtime or scan_time")
        roots = _normalize_file_roots(self.roots, path if kind == "files" else "")
        extensions = _normalize_file_extensions(self.extensions)
        return SourceSpec(
            tool=tool or ("files" if kind == "files" else tool),
            path=path,
            kind=kind,
            source_id=self.source_id,
            native_session_id=self.native_session_id,
            exclude_native_session_ids=tuple(
                _bounded_string(item, 256)
                for item in self.exclude_native_session_ids
                if _bounded_string(item, 256)
            ),
            exclude_caretaker_sessions=bool(self.exclude_caretaker_sessions),
            include_user_messages=bool(self.include_user_messages),
            max_records=min(int(self.max_records), DEFAULT_MAX_RECORDS * 16),
            max_bytes=min(int(self.max_bytes), DEFAULT_MAX_BYTES * 16),
            max_excerpt_bytes=min(int(self.max_excerpt_bytes), DEFAULT_MAX_EXCERPT_BYTES),
            roots=roots,
            extensions=extensions,
            quiet_minutes=max(0, int(self.quiet_minutes)),
            max_file_bytes=max(1, int(self.max_file_bytes)),
            max_files_per_tick=max(1, int(self.max_files_per_tick)),
            state_dir=_bounded_string(self.state_dir, 4096),
            file_observation_mode=self.file_observation_mode,
            exclude_directory_names=tuple(self.exclude_directory_names),
        )


def _normalize_file_roots(roots: Any, fallback_path: str = "") -> tuple[dict[str, Any], ...]:
    items: list[dict[str, Any]] = []
    raw = roots or ()
    if isinstance(raw, Mapping):
        raw = (raw,)
    for item in raw:
        tick_budget.check('connectors.py:metadata_or_work')
        if isinstance(item, Mapping):
            path = _bounded_string(item.get("path"), 4096)
            slug = item.get("project_slug", item.get("projectSlug", ""))
            slug = "" if slug is None else str(slug).strip()
            if "\0" in slug:
                raise ValueError("project_slug must be NUL-free")
            try:
                depth = int(item.get("max_depth", item.get("maxDepth", 4)))
            except (TypeError, ValueError) as exc:
                raise ValueError("root max_depth must be an integer") from exc
            if not path:
                raise ValueError("file root path must be non-empty")
            items.append({"path": path, "project_slug": slug[:128], "max_depth": depth})
        elif isinstance(item, str) and item.strip():
            items.append({"path": item.strip(), "project_slug": "", "max_depth": 4})
        else:
            raise ValueError("file root must be an object with path")
    if not items and fallback_path and fallback_path.strip():
        items.append({"path": fallback_path.strip(), "project_slug": "", "max_depth": 4})
    return tuple(items)


def _normalize_file_extensions(extensions: Any) -> tuple[str, ...]:
    if not extensions:
        return DEFAULT_FILE_EXTENSIONS
    out: list[str] = []
    seen: set[str] = set()
    for item in extensions:
        tick_budget.check('connectors.py:metadata_or_work')
        text = _bounded_string(item, 32).lower()
        if not text:
            raise ValueError("each extension must be a non-empty string")
        if not text.startswith("."):
            raise ValueError("extension must start with '.'")
        if text not in seen:
            seen.add(text)
            out.append(text)
    return tuple(out) if out else DEFAULT_FILE_EXTENSIONS


@dataclass(frozen=True)
class NormalizedEvent:
    """One safe ``completed_turn.v1`` event.

    ``excerpt`` contains only bounded assistant-visible text.  The linked user
    request is represented by its native cursor, never by its text.
    """

    tool: str
    native_session_id: str
    cursor: str
    observed_at: str
    completed_at: str | None
    completion_kind: str
    title: str
    excerpt: str
    source_ref: Mapping[str, str]
    linked_user_cursor: str
    provider: str | None = None
    model: str | None = None
    native_id: str | None = None
    project: str | None = None
    cwd: str | None = None
    schema_version: int = SCHEMA_VERSION
    event_type: str = NORMALIZED_EVENT_TYPE
    speaker: str = ""
    linked_assistant_cursor: str = ""
    parent_cursor: str = ""
    provenance: str = ""
    chunk_index: int = 0
    chunk_count: int = 1

    @property
    def event_id(self) -> str:
        seed = "\x1f".join(
            (self.event_type, self.tool, self.native_session_id, self.cursor)
        )
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        # Keep the shape understood by ingest._normalize_normalized_event.
        # Metadata fields are additive and contain no raw payload.
        base = {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "tool": self.tool,
            "native_session_id": self.native_session_id,
            "native_id": self.native_id or self.cursor,
            "cursor": self.cursor,
            "observed_at": self.observed_at,
            "completed_at": self.completed_at,
            "completion_kind": self.completion_kind,
            "title": self.title,
            "excerpt": self.excerpt,
            "linked_user_cursor": self.linked_user_cursor,
            "provider": self.provider,
            "model": self.model,
            "project": self.project,
            "cwd": self.cwd,
            "classification": "internal",
            "source_ref": dict(self.source_ref),
        }
        if self.event_type == USER_MESSAGE_EVENT_TYPE:
            # User evidence is a separate versioned event.  Keep its
            # provenance explicit while leaving completed_turn.v1 unchanged.
            base.update({
                "speaker": _bounded_string(self.speaker, 16) or "user",
                "linked_assistant_cursor": _bounded_string(self.linked_assistant_cursor, 128),
                "parent_cursor": _bounded_string(self.parent_cursor, 128),
                "provenance": _bounded_string(self.provenance, 64) or USER_PROVENANCE,
                "chunk_index": max(0, int(self.chunk_index)),
                "chunk_count": max(1, int(self.chunk_count)),
            })
        if self.event_type == FILE_EVENT_TYPE:
            ref = dict(self.source_ref)
            speaker = _bounded_string(self.speaker, 32) or FILE_SPEAKER
            if speaker in {"assistant", "user"}:
                speaker = FILE_SPEAKER
            base.update({
                "provenance": _bounded_string(self.provenance, 64) or FILE_PROVENANCE,
                "speaker": speaker,
                "relative_path": _bounded_string(ref.get("relative_path"), 1024),
                "sha256": _bounded_string(ref.get("sha256"), 64),
                "mtime": _bounded_string(ref.get("mtime"), 64),
                "source_id": _bounded_string(ref.get("source_id"), 128),
                "project": self.project or UNASSIGNED_PROJECT,
                "chunk_index": max(0, int(self.chunk_index)),
                "chunk_count": max(1, int(self.chunk_count)),
            })
            if ref.get("rejection_reason"):
                base["rejection_reason"] = _bounded_string(ref.get("rejection_reason"), 128)
                base["rule_id"] = _bounded_string(ref.get("rule_id"), 128)
                base["excerpt"] = ""
        return base

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "NormalizedEvent | None":
        """Load a previously exported event without trusting its raw body."""

        try:
            tool = _bounded_string(value.get("tool"), 32)
            _maybe_user_type = _bounded_string(value.get("event_type"), 64)
            if _maybe_user_type == USER_MESSAGE_EVENT_TYPE:
                return _user_event_from_mapping(value, tool)
            if _maybe_user_type == FILE_EVENT_TYPE:
                return _file_event_from_mapping(value, tool)
            session_id = _bounded_string(value.get("native_session_id"), 256)
            cursor = _bounded_string(value.get("cursor"), 128)
            observed = _bounded_string(value.get("observed_at"), 64)
            linked = _bounded_string(value.get("linked_user_cursor"), 128)
            excerpt = _safe_excerpt(value.get("excerpt"), DEFAULT_MAX_EXCERPT_BYTES)
            title = _safe_title(value.get("title"), tool or "completed turn")
            source_ref = value.get("source_ref")
            if not isinstance(source_ref, Mapping):
                source_ref = {}
            source_ref = {
                "path_or_store": _bounded_string(source_ref.get("path_or_store"), 1024),
                "table_or_stream": _bounded_string(source_ref.get("table_or_stream"), 256),
            }
            if not all((tool, session_id, cursor, observed, linked, excerpt)):
                return None
            event = cls(
                tool=tool,
                native_session_id=session_id,
                cursor=cursor,
                observed_at=observed,
                completed_at=_optional_timestamp(value.get("completed_at")),
                completion_kind=_bounded_string(
                    value.get("completion_kind"), 64
                )
                or "terminal",
                title=title,
                excerpt=excerpt,
                source_ref=source_ref,
                linked_user_cursor=linked,
                provider=_optional_metadata(value.get("provider")),
                model=_optional_metadata(value.get("model")),
                native_id=_optional_metadata(value.get("native_id")),
                project=_optional_metadata(value.get("project")),
                cwd=_optional_metadata(value.get("cwd")),
            )
            if value.get("event_id") and not _serialized_event_id_matches(event, value.get("event_id")):
                return None
            return event
        except (TypeError, ValueError):
            return None


def _serialized_event_id_matches(event: "NormalizedEvent", serialized_id: Any) -> bool:
    """Accept the canonical id plus the historical ids still present on disk.

    0.1.39 scoped Claude identities with ``path_or_store`` and earlier releases
    hashed only ``(tool, native_session_id, cursor)``.  Both are recomputed from
    the event's own fields, so a serialized id that matches neither the current
    nor a previously issued formula still rejects the record.
    """
    if not isinstance(serialized_id, str) or not serialized_id:
        return False
    if serialized_id == event.event_id:
        return True
    source_ref = event.source_ref if isinstance(event.source_ref, Mapping) else {}
    seeds = (
        "\x1f".join((
            event.tool,
            event.native_session_id,
            event.cursor,
            _bounded_string(source_ref.get("path_or_store"), 1024),
        )),
        "\x1f".join((event.tool, event.native_session_id, event.cursor)),
    )
    return any(
        hashlib.sha256(seed.encode("utf-8")).hexdigest() == serialized_id
        for seed in seeds
    )


INJECTED_LINE_MARKERS = ("agents.md", "injected context", "<injected", "</environment>", "<environment>")
INJECTED_LINE_PREFIXES = ("system:", "developer:", "environment:", "skills:", "agents:", "agents.md")
INJECTED_BLOCK_STARTS = (
    "<environment", "<system", "<developer", "<agents", "<injected",
    "```system", "```developer", "```environment", "```agents",
)
INJECTED_BLOCK_ENDS = (
    "</environment>", "</system>", "</developer>", "</agents>",
    "</injected>", "end agents.md", "end agents", "```",
)


def _strip_injected_lines(text: str) -> str:
    kept: list[str] = []
    in_injected_block = False
    for line in text.splitlines(keepends=True):
        tick_budget.check('connectors.py:metadata_or_work')
        low = line.strip().lower()
        if not low:
            if not in_injected_block:
                kept.append(line)
            continue
        if in_injected_block:
            if any(marker in low for marker in INJECTED_BLOCK_ENDS):
                in_injected_block = False
            continue
        if (
            (low == "agents.md" or low.startswith("begin agents.md")
             or low.startswith("begin agents"))
            and not any(marker in low for marker in INJECTED_BLOCK_ENDS)
        ):
            in_injected_block = True
            continue
        if any(low.startswith(marker) for marker in INJECTED_BLOCK_STARTS):
            if not any(marker in low for marker in INJECTED_BLOCK_ENDS):
                in_injected_block = True
            continue
        if low.startswith(INJECTED_LINE_PREFIXES):
            continue
        if any(marker in low for marker in INJECTED_LINE_MARKERS):
            continue
        kept.append(line)
    return "".join(kept)


_USER_ATTACHMENT_TYPES = frozenset({"image", "image_url", "file", "audio", "video", "attachment", "attachments"})


def _extract_user_text(value: Any, depth: int = 0) -> str:
    if depth > 8 or value is None:
        return ""
    if isinstance(value, str):
        return value.replace("\0", "")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return ""
    if isinstance(value, list):
        pieces: list[str] = []
        for item in value:
            tick_budget.check('connectors.py:metadata_or_work')
            text = _extract_user_text(item, depth + 1)
            if text:
                pieces.append(text)
        return "\n".join(pieces)
    if not isinstance(value, Mapping):
        return ""
    role = _bounded_string(value.get("role"), 32).lower()
    if role and role != "user":
        # A structured system/developer/assistant/tool block nested inside a
        # user message is not user evidence and must not be reclassified as
        # one merely because it contains a text field.
        return ""
    item_type = _bounded_string(_first_value(value, "type", "kind", "block_type"), 80).lower().replace("-", "_")
    if item_type.replace("_", "") in {x.replace("_", "") for x in BLOCKED_ITEM_TYPES}:
        return ""
    if item_type in _USER_ATTACHMENT_TYPES:
        return ""
    pieces = []
    for key in ("text", "content", "message", "prompt", "input"):
        tick_budget.check('connectors.py:metadata_or_work')
        if key in value:
            text = _extract_user_text(value[key], depth + 1)
            if text:
                pieces.append(text)
    return "\n".join(pieces)


def _sanitize_user_text(text: str) -> str | None:
    if not text or not text.strip():
        return ""
    if contains_secret(text) or has_reasoning_marker(text) or looks_like_raw_tool_dump(text):
        return None
    cleaned = _strip_injected_lines(text)
    if not cleaned:
        return ""
    return cleaned


def _chunk_utf8_lossless(text: str, max_bytes: int) -> list[str]:
    if max_bytes <= 0:
        return []
    chunks: list[str] = []
    current: list[str] = []
    current_bytes = 0
    for char in text:
        tick_budget.check('connectors.py:metadata_or_work')
        size = len(char.encode("utf-8"))
        if current and current_bytes + size > max_bytes:
            chunks.append("".join(current))
            current = []
            current_bytes = 0
        current.append(char)
        current_bytes += size
    if current:
        chunks.append("".join(current))
    return chunks


def _user_row_text(row: _Row) -> str:
    pieces: list[str] = []
    for field_name in ("content", "text", "message", "prompt", "input"):
        tick_budget.check('connectors.py:metadata_or_work')
        if field_name in row.payload:
            text = _extract_user_text(row.payload[field_name])
            if text:
                pieces.append(text)
    if not pieces and isinstance(row.payload.get("message"), Mapping):
        for field_name in ("content", "text"):
            tick_budget.check('connectors.py:metadata_or_work')
            if field_name in row.payload["message"]:
                text = _extract_user_text(row.payload["message"][field_name])
                if text:
                    pieces.append(text)
    return "\n".join(pieces)


def _user_timestamp(value: Any) -> str:
    """Normalize a user timestamp while retaining second precision when supplied."""

    normalized = _timestamp(value)
    if not normalized:
        return ""
    if isinstance(value, str):
        raw = _bounded_string(value, 64)
        if raw and "." not in raw.split("T", 1)[-1].split("Z", 1)[0]:
            if normalized.endswith(".000Z"):
                return normalized[:-5] + "Z"
    return normalized


def _user_row_is_restricted(row: _Row) -> bool:
    for key in ("classification", "data_classification", "dataClassification"):
        tick_budget.check('connectors.py:metadata_or_work')
        value = _bounded_string(row.payload.get(key), 32).lower()
        if value == "restricted":
            return True
    return row.payload.get("restricted") is True


def _emit_user_events(spec: SourceSpec, *, candidates: Sequence[_Row], session_id: str, turn_id: str = "", assistant_cursor: str = "", table_or_stream: str = "", completed_at: Any = None) -> tuple[list[NormalizedEvent], list[ConnectorIssue]]:
    events: list[NormalizedEvent] = []
    issues: list[ConnectorIssue] = []
    seen: set[str] = set()
    for row in candidates:
        tick_budget.check('connectors.py:metadata_or_work')
        if row.role != "user":
            continue
        if row.session_id != session_id:
            continue
        if turn_id and row.turn_id and row.turn_id != turn_id:
            continue
        base = _row_cursor(row, "user")
        if not base or base in seen:
            continue
        seen.add(base)
        if _user_row_is_restricted(row):
            issues.append(_issue(spec, "skipped", "user_restricted_rejected", row.line_no or None))
            continue
        raw = _user_row_text(row)
        if not raw:
            continue
        cleaned = _sanitize_user_text(raw)
        if cleaned is None:
            issues.append(_issue(spec, "skipped", "user_unsafe_rejected", row.line_no or None))
            continue
        if not cleaned:
            continue
        chunks = _chunk_utf8_lossless(cleaned, spec.max_excerpt_bytes)
        if not chunks:
            continue
        total = len(chunks)
        observed = _user_timestamp(row.observed)
        if not observed:
            issues.append(_issue(spec, "skipped", "user_missing_timestamp", row.line_no or None))
            continue
        base_title = _safe_title(row.title or "user message", "user message")
        title = _bounded_string("[user] " + base_title, 200)
        for index, piece in enumerate(chunks):
            tick_budget.check('connectors.py:metadata_or_work')
            excerpt = _safe_user_chunk(piece, spec.max_excerpt_bytes)
            if not excerpt:
                issues.append(_issue(spec, "skipped", "user_unsafe_rejected", row.line_no or None))
                continue
            cursor = base if total == 1 else f"{base}:c{index + 1:04d}of{total:04d}"
            cursor = _safe_identifier(cursor, "user")
            events.append(NormalizedEvent(tool=spec.tool, native_session_id=session_id, cursor=cursor, observed_at=observed, completed_at=_optional_timestamp(completed_at), completion_kind="user_statement", title=title, excerpt=excerpt, source_ref={"path_or_store": _bounded_string(str(spec.path), 1024), "table_or_stream": _bounded_string(table_or_stream, 256)}, linked_user_cursor="", speaker="user", linked_assistant_cursor=_bounded_string(assistant_cursor, 128), parent_cursor=base, provenance=USER_PROVENANCE, chunk_index=index, chunk_count=total, schema_version=USER_SCHEMA_VERSION, event_type=USER_MESSAGE_EVENT_TYPE))
    return events, issues


def _jsonl_user_evidence(spec: SourceSpec, rows: Sequence[_Row], fallback_session: str) -> tuple[list[NormalizedEvent], list[ConnectorIssue]]:
    events: list[NormalizedEvent] = []
    issues: list[ConnectorIssue] = []
    previous_terminal_order = -1
    for row in rows:
        tick_budget.check('connectors.py:metadata_or_work')
        if row.role != "assistant":
            continue
        terminal, _marker = _has_terminal_marker(row.payload, _bounded_string(row.payload.get("type") or row.outer_type, 80))
        if not terminal:
            continue
        session_id = _row_session(row, spec, fallback_session)
        if _session_excluded(spec, session_id):
            continue
        candidate_floor = max(
            previous_terminal_order,
            _latest_gap_order(rows, row.order),
        )
        candidates = [
            item for item in rows
            if item.role == "user"
            and candidate_floor < item.order < row.order
        ]
        more_events, more_issues = _emit_user_events(spec, candidates=candidates, session_id=session_id, turn_id=row.turn_id, assistant_cursor=_row_cursor(row, "assistant"), table_or_stream=f"jsonl:{row.outer_type or row.payload.get('type', 'record')}:user", completed_at=_first_value(row.payload, "completed_at", "completedAt"))
        events.extend(more_events)
        issues.extend(more_issues)
        previous_terminal_order = max(previous_terminal_order, row.order)
    return events, issues


def _codex_user_evidence(spec: SourceSpec, users: Sequence[_Row], terminals: Sequence[_Row], gaps: Sequence[int] = ()) -> tuple[list[NormalizedEvent], list[ConnectorIssue]]:
    events: list[NormalizedEvent] = []
    issues: list[ConnectorIssue] = []
    previous_terminal_order = -1
    for terminal in terminals:
        tick_budget.check('connectors.py:metadata_or_work')
        tid = terminal.turn_id or _bounded_string(_first_value(terminal.payload, "turn_id", "turnId"), 256)
        session_id = _bounded_string(terminal.session_id, 256)
        if not session_id or _session_excluded(spec, session_id):
            continue
        candidate_floor = max(
            previous_terminal_order,
            max((order for order in gaps if order < terminal.order), default=-1),
        )
        cursor_raw = tid or terminal.cursor_raw or terminal.native_id
        assistant_cursor = _safe_identifier(cursor_raw, "turn")
        candidates = [
            item for item in users
            if candidate_floor < item.order < terminal.order
        ]
        more_events, more_issues = _emit_user_events(spec, candidates=candidates, session_id=session_id, turn_id=tid, assistant_cursor=assistant_cursor, table_or_stream="jsonl:event_msg.task_complete:user", completed_at=terminal.completed)
        events.extend(more_events)
        issues.extend(more_issues)
        previous_terminal_order = max(previous_terminal_order, terminal.order)
    return events, issues


def _tabular_user_evidence(spec: SourceSpec, rows: Sequence[_Row]) -> tuple[list[NormalizedEvent], list[ConnectorIssue]]:
    events: list[NormalizedEvent] = []
    issues: list[ConnectorIssue] = []
    previous_terminal_order = -1
    for row in rows:
        tick_budget.check('connectors.py:metadata_or_work')
        if row.role != "assistant" or not row.terminal:
            continue
        if _session_excluded(spec, row.session_id):
            continue
        candidates = [
            item for item in rows
            if item.role == "user"
            and previous_terminal_order < item.order < row.order
        ]
        more_events, more_issues = _emit_user_events(spec, candidates=candidates, session_id=row.session_id, turn_id=row.turn_id, assistant_cursor=(_safe_identifier(row.turn_id, "turn") if row.turn_id else _row_cursor(row, "message")), table_or_stream=f"{row.source_table or 'messages'}:user", completed_at=row.completed)
        events.extend(more_events)
        issues.extend(more_issues)
        previous_terminal_order = max(previous_terminal_order, row.order)
    return events, issues


def event_to_dict(event: NormalizedEvent) -> dict[str, Any]:
    # Maintenance tests and older adapters may provide a compatible event
    # object without the additive user fields.  Preserve their v1 shape.
    if getattr(event, "event_type", NORMALIZED_EVENT_TYPE) != USER_MESSAGE_EVENT_TYPE:
        return event.to_dict()
    return event.to_dict()


def _user_event_from_mapping(value: Mapping[str, Any], tool: str) -> NormalizedEvent | None:
    try:
        session_id = _bounded_string(value.get("native_session_id"), 256)
        cursor = _bounded_string(value.get("cursor"), 128)
        observed = _bounded_string(value.get("observed_at"), 64)
        excerpt = _safe_user_chunk(value.get("excerpt"), DEFAULT_MAX_EXCERPT_BYTES)
        title = _safe_title(value.get("title"), "user message")
        source_ref = value.get("source_ref")
        if not isinstance(source_ref, Mapping):
            source_ref = {}
        source_ref = {"path_or_store": _bounded_string(source_ref.get("path_or_store"), 1024), "table_or_stream": _bounded_string(source_ref.get("table_or_stream"), 256)}
        if not all((tool, session_id, cursor, observed, excerpt)):
            return None
        try:
            chunk_index = max(0, int(value.get("chunk_index", 0)))
        except (TypeError, ValueError):
            chunk_index = 0
        try:
            chunk_count = max(1, int(value.get("chunk_count", 1)))
        except (TypeError, ValueError):
            chunk_count = 1
        event = NormalizedEvent(tool=tool, native_session_id=session_id, cursor=cursor, observed_at=observed, completed_at=_optional_timestamp(value.get("completed_at")), completion_kind=_bounded_string(value.get("completion_kind"), 64) or "user_statement", title=title, excerpt=excerpt, source_ref=source_ref, linked_user_cursor="", speaker="user", linked_assistant_cursor=_bounded_string(value.get("linked_assistant_cursor"), 128), parent_cursor=_bounded_string(value.get("parent_cursor"), 128) or cursor, provenance=USER_PROVENANCE, chunk_index=chunk_index, chunk_count=chunk_count, provider=_optional_metadata(value.get("provider")), model=_optional_metadata(value.get("model")), native_id=_optional_metadata(value.get("native_id")), project=_optional_metadata(value.get("project")), cwd=_optional_metadata(value.get("cwd")), schema_version=USER_SCHEMA_VERSION, event_type=USER_MESSAGE_EVENT_TYPE)
        if value.get("event_id") and not _serialized_event_id_matches(event, value.get("event_id")):
            return None
        return event
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class ConnectorIssue:
    source_key: str
    tool: str
    status: str  # unsupported | skipped | info | unavailable | error
    reason: str
    line_no: int | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "source_key": self.source_key,
            "tool": self.tool,
            "status": self.status,
            "reason": self.reason,
        }
        if self.line_no is not None:
            result["line_no"] = self.line_no
        return result


@dataclass(frozen=True)
class ConnectorCollection:
    events: tuple[NormalizedEvent, ...] = ()
    issues: tuple[ConnectorIssue, ...] = ()
    records_seen: int = 0


@dataclass(frozen=True)
class SourceDiscovery:
    tool: str
    kind: str
    path: str
    status: str  # supported | unsupported | unavailable | error
    schema_keys: tuple[str, ...] = ()
    tables: tuple[str, ...] = ()
    terminal_markers: tuple[str, ...] = ()
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "kind": self.kind,
            "path": self.path,
            "status": self.status,
            "schema_keys": list(self.schema_keys),
            "tables": list(self.tables),
            "terminal_markers": list(self.terminal_markers),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ExportResult:
    output_path: str
    manifest_path: str
    cursor_path: str
    events: tuple[NormalizedEvent, ...]
    issues: tuple[ConnectorIssue, ...]
    new_records: int
    total_records: int


@dataclass
class _Row:
    order: int
    line_no: int = 0
    outer_type: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    session_id: str = ""
    native_id: str = ""
    cursor_raw: str = ""
    turn_id: str = ""
    parent_id: str = ""
    role: str = ""
    observed: Any = None
    completed: Any = None
    title: str = ""
    provider: str = ""
    model: str = ""
    project: str = ""
    cwd: str = ""
    source_table: str = ""
    terminal: bool = False
    gap: bool = False


def _bounded_string(value: Any, max_chars: int) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        return ""
    value = value.replace("\0", "").strip()
    if not value:
        return ""
    return value[:max_chars]


def _optional_metadata(value: Any) -> str | None:
    value = _bounded_string(value, 256)
    if value and (
        contains_secret(value)
        or has_reasoning_marker(value)
        or looks_like_raw_tool_dump(value)
    ):
        return None
    return value or None


def _optional_timestamp(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return _timestamp(value)


def _first_value(value: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        tick_budget.check('connectors.py:metadata_or_work')
        if key in value and value[key] not in (None, ""):
            return value[key]
    return None


def _nested_mapping(value: Any, *keys: str) -> Mapping[str, Any] | None:
    current: Any = value
    for key in keys:
        tick_budget.check('connectors.py:metadata_or_work')
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current if isinstance(current, Mapping) else None


def _timestamp(value: Any) -> str:
    """Return a bounded RFC3339 value for common native timestamps."""

    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 100_000_000_000:  # milliseconds since epoch
            number /= 1000.0
        try:
            return (
                datetime.fromtimestamp(number, tz=timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )
        except (OverflowError, OSError, ValueError):
            return ""
    text = _bounded_string(value, 64)
    if not text:
        return ""
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return _timestamp(float(text))
    # Accept native ISO-like timestamps only.  A finish reason such as
    # ``end_turn`` is a completion marker, not a timestamp.
    try:
        candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _clip_utf8(text: str, max_bytes: int) -> str:
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= max_bytes:
        return text
    return raw[:max_bytes].decode("utf-8", errors="ignore").rstrip()


def _safe_excerpt(value: Any, max_bytes: int) -> str:
    text = _bounded_string(value, max(max_bytes * 4, max_bytes))
    if not text:
        return ""
    # Connectors reject unsafe content before writing.  The existing filter
    # still runs after ingest and remains the authoritative downstream gate.
    if contains_secret(text) or has_reasoning_marker(text) or looks_like_raw_tool_dump(text):
        return ""
    # Do not turn a partial sentence into a durable fact.  Oversized terminal
    # answers are reported as needing a full-source follow-up and are skipped
    # by the caller rather than truncated here.
    if len(text.encode("utf-8", errors="replace")) > max_bytes:
        return ""
    return text


def _safe_user_chunk(value: Any, max_bytes: int) -> str:
    """Validate a pre-sized user chunk without trimming its boundary bytes."""

    if not isinstance(value, str):
        return ""
    text = value.replace("\0", "")
    if not text:
        return ""
    if contains_secret(text) or has_reasoning_marker(text) or looks_like_raw_tool_dump(text):
        return ""
    if len(text.encode("utf-8", errors="replace")) > max_bytes:
        return ""
    return text


def _safe_title(value: Any, fallback: str) -> str:
    text = _bounded_string(value, 200)
    if not text or contains_secret(text) or has_reasoning_marker(text) or looks_like_raw_tool_dump(text):
        return _bounded_string(fallback, 200) or "completed turn"
    return text


def _answer_needs_full_source(value: Any, max_bytes: int) -> bool:
    if not isinstance(value, str):
        return False
    return len(value.encode("utf-8", errors="replace")) > max_bytes


def _safe_identifier(value: Any, prefix: str) -> str:
    raw = _bounded_string(value, 512)
    if not raw:
        return ""
    if ID_RE.fullmatch(raw):
        return raw
    return f"{prefix}-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]}"


def _extract_text(value: Any, depth: int = 0) -> str:
    """Extract only text-shaped assistant blocks from a structured value."""

    if depth > 8 or value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return ""
    if isinstance(value, list):
        pieces: list[str] = []
        for item in value:
            tick_budget.check('connectors.py:metadata_or_work')
            text = _extract_text(item, depth + 1)
            if text:
                pieces.append(text)
        return "\n".join(pieces).strip()
    if not isinstance(value, Mapping):
        return ""
    item_type = _bounded_string(
        _first_value(value, "type", "kind", "block_type"), 80
    ).lower().replace("-", "_")
    if item_type.replace("_", "") in {x.replace("_", "") for x in BLOCKED_ITEM_TYPES}:
        return ""
    # Never walk generic tool/result fields.  The explicit set is kept narrow
    # so an answer can still contain ordinary words such as "tool".
    blocked_keys = {
        "tool_calls",
        "tool_call",
        "tool_results",
        "tool_result",
        "tool_input",
        "tool_output",
        "arguments",
        "raw_arguments",
        "stdout",
        "stderr",
        "result",
    }
    pieces: list[str] = []
    for key in ("text", "content", "answer", "response", "message", "output"):
        tick_budget.check('connectors.py:metadata_or_work')
        if key in value and key not in blocked_keys:
            text = _extract_text(value[key], depth + 1)
            if text:
                pieces.append(text)
    return "\n".join(pieces).strip()


def _extract_from_fields(value: Mapping[str, Any], fields: Sequence[str]) -> str:
    pieces: list[str] = []
    for field_name in fields:
        tick_budget.check('connectors.py:metadata_or_work')
        if field_name in value:
            text = _extract_text(value[field_name])
            if text:
                pieces.append(text)
    return "\n".join(pieces).strip()


def _marker_containers(value: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    yield value
    for key in ("payload", "message", "time", "metadata", "info", "terminal"):
        tick_budget.check('connectors.py:metadata_or_work')
        nested = value.get(key)
        if isinstance(nested, Mapping):
            yield nested


def _claude_tool_use_stop(value: Mapping[str, Any]) -> bool:
    return any(
        _bounded_string(container.get(key), 80).lower().replace("-", "_")
        in {"tool_use", "tooluse"}
        for container in _marker_containers(value)
        for key in ("stop_reason", "stopReason")
    )


def _has_terminal_marker(value: Mapping[str, Any], event_type: str = "") -> tuple[bool, str]:
    lowered_type = event_type.lower().replace("_", ".")
    values: list[tuple[str, Any]] = []
    for container in _marker_containers(value):
        tick_budget.check('connectors.py:metadata_or_work')
        for key in MARKER_KEYS:
            tick_budget.check('connectors.py:metadata_or_work')
            if key in container:
                values.append((key, container[key]))
    # An explicit active state always wins over a weaker neighboring marker.
    for key, raw in values:
        tick_budget.check('connectors.py:metadata_or_work')
        if key in {"active", "is_active", "isActive", "streaming"} and (
            raw is True or raw == 1 or raw == "1"
        ):
            return False, "active"
        if isinstance(raw, str) and raw.strip().lower() in ACTIVE_VALUES:
            return False, "active"
    if lowered_type in {
        "task.complete",
        "turn.completed",
        "assistant.completed",
        "message.completed",
        "run.terminal.completed",
    } or lowered_type.endswith(".completed") and "tool" not in lowered_type:
        return True, f"event:{lowered_type}"
    for key, raw in values:
        tick_budget.check('connectors.py:metadata_or_work')
        if isinstance(raw, bool):
            if raw and key in {"completed", "is_final", "isFinal", "ended", "terminal"}:
                return True, f"{key}:true"
            continue
        text = _bounded_string(raw, 80).lower().replace("-", "_")
        if not text:
            continue
        if text in UNSAFE_FINISH_VALUES:
            return False, f"{key}:{text}"
        if text in TERMINAL_VALUES:
            return True, f"{key}:{text}"
        # A timestamp under one of these keys is a completion proof.  OpenCode
        # stores this as ``time.completed`` in some revisions.
        if key in {"completed", "completed_at", "completedAt", "ended_at"}:
            if key == "completed" and isinstance(raw, bool):
                continue
            return True, f"{key}:present"
    return False, "missing_terminal_marker"


def _row_from_json(payload: Mapping[str, Any], line_no: int, order: int) -> _Row:
    outer_type = _bounded_string(payload.get("type"), 80)
    nested = payload.get("payload")
    body = dict(nested) if isinstance(nested, Mapping) else dict(payload)
    # Retain only structured fields in this in-memory row.  It is never
    # serialized directly; final output is built from the allowlisted fields.
    message = body.get("message") if isinstance(body.get("message"), Mapping) else {}
    item = body.get("item") if isinstance(body.get("item"), Mapping) else {}
    role = _bounded_string(
        _first_value(body, "role") or _first_value(message, "role") or _first_value(item, "role"),
        32,
    ).lower()
    if not role:
        type_lower = _bounded_string(
            _first_value(body, "type") or outer_type, 80
        ).lower()
        if type_lower in {"assistant", "agent_message", "agentmessage"}:
            role = "assistant"
        elif type_lower in {"user", "user_message", "usermessage"}:
            role = "user"
    session_id = _bounded_string(
        _first_value(body, "session_id", "sessionId", "native_session_id", "conversation_id", "thread_id")
        or _first_value(message, "session_id", "sessionId"),
        256,
    )
    native_id = _bounded_string(
        _first_value(body, "id", "message_id", "messageId", "uuid", "request_id", "requestId", "anthropic_request_id")
        or _first_value(message, "id", "message_id", "messageId", "uuid"),
        512,
    )
    cursor_raw = _bounded_string(
        _first_value(body, "cursor", "seq", "sequence", "ordinal"), 256
    )
    turn_id = _bounded_string(
        _first_value(body, "turn_id", "turnId", "root_turn_id", "rootTurnId"), 256
    )
    parent_id = _bounded_string(
        _first_value(body, "parent_uuid", "parentUuid", "parent_id", "parentId")
        or _first_value(message, "parent_uuid", "parentUuid", "parent_id", "parentId"),
        512,
    )
    observed = _first_value(
        body, "timestamp", "observed_at", "observedAt", "created_at", "createdAt", "time_created"
    )
    if observed is None:
        observed = payload.get("timestamp")
    completed = _first_value(
        body, "completed_at", "completedAt", "finish_reason", "finishReason", "stop_reason", "stopReason"
    )
    if completed is None:
        completed = _first_value(message, "completed_at", "completedAt", "finish_reason", "finishReason", "stop_reason", "stopReason")
    title = _bounded_string(
        _first_value(body, "title", "customTitle", "name") or _first_value(message, "title"), 200
    )
    provider = _bounded_string(
        _first_value(body, "provider", "provider_id", "providerId", "model_provider", "modelProvider")
        or _first_value(message, "provider", "provider_id", "providerId"),
        128,
    )
    model = _bounded_string(
        _first_value(body, "model", "model_id", "modelId") or _first_value(message, "model", "model_id", "modelId"),
        128,
    )
    cwd = _bounded_string(
        _first_value(body, "cwd", "working_directory", "workingDirectory")
        or _first_value(message, "cwd", "working_directory"),
        1024,
    )
    project = _bounded_string(
        _first_value(body, "project", "project_id", "projectId", "workspace")
        or _first_value(message, "project", "project_id", "projectId"),
        256,
    )
    marker_type = _bounded_string(body.get("type") or outer_type, 80).lower()
    terminal, _ = _has_terminal_marker(body, marker_type)
    if outer_type.lower() == "event_msg" and marker_type not in {
        "task_complete", "task.complete", "turn_completed", "turn.completed"
    }:
        terminal = False
    return _Row(
        order=order,
        line_no=line_no,
        outer_type=outer_type,
        payload=body,
        session_id=session_id,
        native_id=native_id,
        cursor_raw=cursor_raw,
        turn_id=turn_id,
        parent_id=parent_id,
        role=role,
        observed=observed,
        completed=completed,
        title=title,
        provider=provider,
        model=model,
        project=project,
        cwd=cwd,
        terminal=terminal,
    )


def _row_terminal_boundary(row: _Row, tool: str = "") -> bool:
    """Whether a JSONL row closes an assistant turn for scan bounding."""

    marker_type = _bounded_string(
        row.payload.get("type") or row.outer_type, 80
    ).lower()
    if tool == "codex" or row.outer_type.lower() == "event_msg":
        return marker_type in {
            "task_complete", "task.complete", "turn_completed", "turn.completed"
        } and _has_terminal_marker(row.payload, marker_type)[0]
    if tool == "claude" and _claude_tool_use_stop(row.payload):
        return False
    terminal, _ = _has_terminal_marker(row.payload, marker_type)
    if not terminal or row.role == "user":
        return False
    if row.role == "assistant":
        return True
    return marker_type in {
        "task_complete",
        "task.complete",
        "turn_completed",
        "turn.completed",
        "assistant_completed",
        "assistant.completed",
        "message_completed",
        "message.completed",
    }


def _source_key(spec: SourceSpec) -> str:
    if spec.source_id:
        return _bounded_string(spec.source_id, 256)
    return f"{spec.tool}:{spec.kind}:{os.path.abspath(str(spec.path))}"


def _session_excluded(spec: SourceSpec, session_id: str) -> bool:
    """Keep root/current and caretaker sessions out of cross-tool export."""

    if not session_id:
        return False
    if session_id in spec.exclude_native_session_ids:
        return True
    if spec.exclude_caretaker_sessions:
        lowered = session_id.lower()
        if lowered.startswith("caretaker") or "hub-caretaker" in lowered:
            return True
    return False


def _source_excluded(spec: SourceSpec) -> bool:
    if not spec.exclude_caretaker_sessions:
        return False
    normalized_path = str(spec.path).replace("\\", "/").lower()
    return "/hub-caretaker/" in f"/{normalized_path.strip('/')}/"


def _issue(spec: SourceSpec, status: str, reason: str, line_no: int | None = None) -> ConnectorIssue:
    return ConnectorIssue(_source_key(spec), spec.tool, status, reason, line_no)


def _read_json_rows(spec: SourceSpec) -> tuple[list[_Row], list[ConnectorIssue], int]:
    path = Path(str(spec.path))
    if not path.is_file():
        return [], [_issue(spec, "unavailable", "source_missing")], 0

    # ``max_records`` is a terminal-turn bound.  A rollout may contain many
    # deltas, tool state records, and metadata rows before its completion
    # marker, so using it as a raw-line limit can stop immediately before the
    # only trustworthy record.  The multiplier is still finite; a source that
    # needs more room is reported as coverage-limited instead of being silently
    # presented as complete history.
    raw_record_cap = min(
        MAX_RAW_RECORDS,
        max(spec.max_records * RAW_RECORD_MULTIPLIER, spec.max_records + 32),
    )

    def _discard_oversized_line(
        handle: Any, initial: bytes, remaining_budget: int
    ) -> tuple[int, bool]:
        """Drop one overlarge JSONL line without retaining or parsing it.

        ``readline`` is used for each bounded piece so a delimiter is never
        consumed together with the next record.  If the existing byte budget
        ends before the delimiter, the caller stops with a visible gap rather
        than guessing where a later JSON object begins.
        """

        consumed = len(initial)
        if initial.endswith((b"\n", b"\r")):
            return consumed, True
        while consumed < max(0, remaining_budget):
            tick_budget.check('connectors.py:metadata_or_work')
            piece = handle.readline(
                min(64 * 1024, remaining_budget - consumed)
            )
            if not piece:
                return consumed, True
            consumed += len(piece)
            if piece.endswith((b"\n", b"\r")):
                return consumed, True
        return consumed, False

    def consume(
        handle: Any,
        byte_budget: int,
        *,
        line_no_start: int = 0,
        order_start: int = 0,
        prefix_only: bool = False,
    ) -> tuple[list[_Row], list[ConnectorIssue], int, int, str]:
        rows: list[_Row] = []
        issues: list[ConnectorIssue] = []
        line_no = line_no_start
        records_seen = 0
        bytes_seen = 0
        terminal_boundaries = 0
        line_cap = min(MAX_LINE_BYTES, max(1, byte_budget))
        stop_reason = "eof"
        while records_seen < raw_record_cap and bytes_seen < byte_budget:
            tick_budget.check('connectors.py:metadata_or_work')
            raw = handle.readline(line_cap + 1)
            if not raw:
                break
            line_no += 1
            remaining_budget = byte_budget - bytes_seen
            if len(raw) > line_cap:
                consumed, line_complete = _discard_oversized_line(
                    handle, raw, remaining_budget
                )
                bytes_seen += consumed
                records_seen += 1
                rows.append(
                    _Row(
                        order=order_start + len(rows),
                        line_no=line_no,
                        gap=True,
                    )
                )
                issues.append(_issue(spec, "skipped", "line_too_large", line_no))
                if not line_complete:
                    stop_reason = "line_too_large"
                    break
                if records_seen >= raw_record_cap:
                    stop_reason = "raw_record_bound"
                    break
                if bytes_seen >= byte_budget:
                    stop_reason = "byte_budget_exhausted"
                    break
                continue
            if bytes_seen + len(raw) > byte_budget:
                bytes_seen += len(raw)
                if raw.strip():
                    records_seen += 1
                    rows.append(
                        _Row(
                            order=order_start + len(rows),
                            line_no=line_no,
                            gap=True,
                        )
                    )
                issues.append(_issue(spec, "skipped", "byte_budget_exhausted", line_no))
                stop_reason = "byte_budget_exhausted"
                break
            bytes_seen += len(raw)
            if not raw.strip():
                continue
            records_seen += 1
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                issues.append(_issue(spec, "skipped", "invalid_json", line_no))
                continue
            if not isinstance(value, Mapping):
                issues.append(_issue(spec, "skipped", "non_object_record", line_no))
                continue
            row = _row_from_json(value, line_no, order_start + len(rows))
            if prefix_only:
                # A large-file prefix is an identity/user anchor only.  Do not
                # spend the prefix budget on old assistant answers when the
                # newest terminal turns are read from the tail below.
                kind = _bounded_string(
                    row.payload.get("type") or row.outer_type, 80
                ).lower()
                if row.role != "user" and kind not in {"session_meta", "turn_context"}:
                    continue
            rows.append(row)
            if not prefix_only:
                if _row_terminal_boundary(row, spec.tool):
                    terminal_boundaries += 1
                    if terminal_boundaries >= spec.max_records:
                        stop_reason = "terminal_batch_bounded"
                        break
        else:
            if records_seen >= raw_record_cap:
                stop_reason = "raw_record_bound"
            elif bytes_seen >= byte_budget:
                stop_reason = "byte_budget_exhausted"
        if records_seen >= raw_record_cap and stop_reason == "eof":
            stop_reason = "raw_record_bound"
        if bytes_seen >= byte_budget and stop_reason == "eof":
            stop_reason = "byte_budget_exhausted"
        return rows, issues, records_seen, line_no, stop_reason

    rows: list[_Row] = []
    issues: list[ConnectorIssue] = []
    records_seen = 0
    try:
        file_size = path.stat().st_size
        with path.open("rb") as handle:
            if file_size <= spec.max_bytes:
                segment_rows, segment_issues, segment_seen, _, stop_reason = consume(
                    handle, spec.max_bytes
                )
                rows.extend(segment_rows)
                issues.extend(segment_issues)
                records_seen += segment_seen
                if stop_reason != "eof":
                    issues.append(_issue(spec, "skipped", "coverage_limited"))
            else:
                # Prefer the newest complete turns when a growing rollout is
                # larger than one bounded read.  Keep a small prefix for
                # session metadata and safe user cursors needed for linkage.
                prefix_budget = min(64 * 1024, spec.max_bytes // 4)
                suffix_budget = spec.max_bytes - prefix_budget
                if suffix_budget <= 0:
                    prefix_budget = 0
                    suffix_budget = spec.max_bytes
                if prefix_budget:
                    prefix_rows, prefix_issues, prefix_seen, prefix_line, prefix_stop = consume(
                        handle, prefix_budget, prefix_only=True
                    )
                else:
                    prefix_rows, prefix_issues, prefix_seen, prefix_line, prefix_stop = (
                        [], [], 0, 0, "eof"
                    )
                rows.extend(prefix_rows)
                issues.extend(prefix_issues)
                records_seen += prefix_seen
                if prefix_stop != "eof":
                    issues.append(_issue(spec, "skipped", "coverage_limited"))

                suffix_start = max(0, file_size - suffix_budget)
                handle.seek(suffix_start)
                suffix_consumed = 0
                suffix_line_start = prefix_line
                if suffix_start:
                    # Align to a complete JSONL record.  The discarded prefix
                    # is never retained, and its contents are not exposed.
                    handle.seek(suffix_start - 1)
                    previous = handle.read(1)
                    handle.seek(suffix_start)
                    if previous not in {b"\n", b"\r"}:
                        partial_cap = min(MAX_LINE_BYTES, suffix_budget)
                        partial = handle.readline(partial_cap + 1)
                        if partial:
                            suffix_consumed, partial_complete = _discard_oversized_line(
                                handle, partial, suffix_budget
                            )
                            suffix_line_start += 1
                            # The window began in an unseen record.  Keep a
                            # gap marker even when that record was below the
                            # line cap so stale user text cannot link across
                            # the discarded boundary.
                            rows.append(
                                _Row(
                                    order=len(rows),
                                    line_no=suffix_line_start,
                                    gap=True,
                                )
                            )
                            if len(partial) > partial_cap:
                                issues.append(_issue(spec, "skipped", "line_too_large", suffix_line_start))
                            if not partial_complete:
                                issues.append(_issue(spec, "skipped", "byte_budget_exhausted", suffix_line_start))
                                suffix_consumed = max(suffix_consumed, suffix_budget)
                suffix_remaining = suffix_budget - suffix_consumed
                if suffix_remaining > 0:
                    suffix_rows, suffix_issues, suffix_seen, _, suffix_stop = consume(
                        handle,
                        suffix_remaining,
                        line_no_start=suffix_line_start,
                        order_start=len(rows),
                    )
                else:
                    suffix_rows, suffix_issues, suffix_seen, suffix_stop = (
                        [], [], 0, "byte_budget_exhausted"
                    )
                rows.extend(suffix_rows)
                issues.extend(suffix_issues)
                records_seen += suffix_seen
                # A tail window is inherently partial source coverage.  Make
                # it visible to the runner and manifest on every such read.
                issues.append(_issue(spec, "skipped", "coverage_limited"))
    except (OSError, UnicodeError):
        issues.append(_issue(spec, "error", "source_read_error"))
    return rows, issues, records_seen


def _row_cursor(row: _Row, prefix: str) -> str:
    raw = row.cursor_raw or row.native_id
    return _safe_identifier(raw, prefix)


def _row_session(row: _Row, spec: SourceSpec, fallback: str = "") -> str:
    return _bounded_string(row.session_id or fallback or spec.native_session_id, 256)


def _row_text(row: _Row, fields: Sequence[str]) -> str:
    text = _extract_from_fields(row.payload, fields)
    if not text and isinstance(row.payload.get("message"), Mapping):
        text = _extract_from_fields(row.payload["message"], fields)
    return text


def _row_is_safe_user(row: _Row, max_bytes: int) -> bool:
    text = _row_text(row, ("content", "text", "message", "prompt", "input"))
    return bool(_safe_excerpt(text, max_bytes))


def _latest_gap_order(rows: Sequence[_Row], before_order: int) -> int:
    return max(
        (row.order for row in rows if row.gap and row.order < before_order),
        default=-1,
    )


def _linked_user(rows: Sequence[_Row], assistant: _Row, max_bytes: int) -> str:
    gap_order = _latest_gap_order(rows, assistant.order)
    candidates = [
        row
        for row in rows
        if gap_order < row.order < assistant.order
        and row.role == "user"
        and row.session_id == assistant.session_id
    ]
    if assistant.parent_id:
        for row in reversed(candidates):
            tick_budget.check('connectors.py:metadata_or_work')
            if row.native_id == assistant.parent_id and _row_is_safe_user(row, max_bytes):
                return _row_cursor(row, "user")
    if assistant.turn_id:
        for row in reversed(candidates):
            tick_budget.check('connectors.py:metadata_or_work')
            if row.turn_id == assistant.turn_id and _row_is_safe_user(row, max_bytes):
                return _row_cursor(row, "user")
    for row in reversed(candidates):
        tick_budget.check('connectors.py:metadata_or_work')
        if _row_is_safe_user(row, max_bytes):
            return _row_cursor(row, "user")
    return ""


def _make_event(
    spec: SourceSpec,
    row: _Row,
    *,
    session_id: str,
    cursor: str,
    answer: str,
    linked_user_cursor: str,
    completion_kind: str,
    table_or_stream: str,
    title: str = "",
    provider: str = "",
    model: str = "",
    project: str = "",
    cwd: str = "",
    completed_at: Any = None,
) -> NormalizedEvent | None:
    session_id = _bounded_string(session_id, 256)
    cursor = _safe_identifier(cursor, "turn")
    observed = _timestamp(row.observed)
    if not session_id or not cursor or not observed or not linked_user_cursor:
        return None
    excerpt = _safe_excerpt(answer, spec.max_excerpt_bytes)
    if not excerpt:
        return None
    completed = _optional_timestamp(completed_at if completed_at is not None else row.completed)
    return NormalizedEvent(
        tool=spec.tool,
        native_session_id=session_id,
        cursor=cursor,
        observed_at=observed,
        completed_at=completed,
        completion_kind=_bounded_string(completion_kind, 64) or "terminal",
        title=_safe_title(title or row.title, f"{spec.tool} completed turn"),
        excerpt=excerpt,
        source_ref={
            "path_or_store": _bounded_string(str(spec.path), 1024),
            "table_or_stream": _bounded_string(table_or_stream, 256),
        },
        linked_user_cursor=linked_user_cursor,
        provider=_optional_metadata(provider or row.provider),
        model=_optional_metadata(model or row.model),
        native_id=_optional_metadata(row.native_id or cursor),
        project=_optional_metadata(project or row.project),
        cwd=_optional_metadata(cwd or row.cwd),
    )


def _deduplicate_events(events: Iterable[NormalizedEvent]) -> tuple[NormalizedEvent, ...]:
    by_id: dict[str, NormalizedEvent] = {}
    for event in events:
        tick_budget.check('connectors.py:metadata_or_work')
        by_id[event.event_id] = event
    return tuple(
        sorted(
            by_id.values(),
            key=lambda item: (item.tool, item.native_session_id, item.cursor, item.event_id),
        )
    )


class BaseConnector:
    tool: ClassVar[str] = ""

    def collect(self, spec: SourceSpec) -> ConnectorCollection:
        raise NotImplementedError


_SECRET_REJECTION_REASONS = frozenset({"secret_path_blacklisted", "secret_detected"})


def _file_native_session_id(
    source_id: str, relative_path: str, project_slug: str = ""
) -> str:
    sid = _bounded_string(source_id, 128) or "files"
    rel = _bounded_string(relative_path, 1024) or "file"
    slug = _bounded_string(project_slug, 128)
    identity = f"{slug}:{rel}" if slug else rel
    combined = f"{sid}:{identity}"
    if len(combined.encode("utf-8")) <= 256:
        return combined
    return f"{sid}:{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"


def _event_matches_source(event: NormalizedEvent, spec: SourceSpec) -> bool:
    if spec.kind == "files" or spec.tool == "files":
        event_sid = event.source_ref.get("source_id") or ""
        spec_sid = _bounded_string(spec.source_id, 128)
        if spec_sid and event_sid == spec_sid:
            return True
        if spec_sid and event.native_session_id.startswith(f"{spec_sid}:"):
            return True
        return False
    return event.source_ref.get("path_or_store") == str(spec.path)


def _file_mtime_iso(mtime: float) -> str:
    try:
        return (
            datetime.fromtimestamp(float(mtime), tz=timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    except (OverflowError, OSError, ValueError):
        return ""


def _file_source_config(spec: SourceSpec):
    from .files import FileSourceConfig

    source_id = _bounded_string(spec.source_id, 128) or "files"
    mapping = {
        "roots": [dict(item) for item in spec.roots],
        "extensions": list(spec.extensions or DEFAULT_FILE_EXTENSIONS),
        "quiet_minutes": spec.quiet_minutes,
        "max_file_bytes": spec.max_file_bytes,
        "max_files_per_tick": spec.max_files_per_tick,
        "exclude_directory_names": list(spec.exclude_directory_names),
    }
    return FileSourceConfig.from_mapping(mapping, source_id=source_id)


def _file_event_from_mapping(value: Mapping[str, Any], tool: str) -> NormalizedEvent | None:
    try:
        source_ref = value.get("source_ref")
        if not isinstance(source_ref, Mapping):
            source_ref = {}
        relative_path = _bounded_string(
            value.get("relative_path") or source_ref.get("relative_path"), 1024
        )
        sha256 = _bounded_string(value.get("sha256") or source_ref.get("sha256"), 64)
        source_id = _bounded_string(value.get("source_id") or source_ref.get("source_id"), 128)
        observed = _bounded_string(value.get("observed_at"), 64)
        if not all((tool, relative_path, observed)):
            return None
        project_raw = value.get("project")
        if project_raw in (None, ""):
            project = UNASSIGNED_PROJECT
        else:
            project = _optional_metadata(project_raw) or UNASSIGNED_PROJECT
        ref = {
            "path_or_store": _bounded_string(source_ref.get("path_or_store") or relative_path, 1024),
            "table_or_stream": _bounded_string(source_ref.get("table_or_stream") or "files", 256),
            "relative_path": relative_path,
            "sha256": sha256,
            "mtime": _bounded_string(value.get("mtime") or source_ref.get("mtime"), 64),
            "source_id": source_id,
        }
        if value.get("rejection_reason") or source_ref.get("rejection_reason"):
            ref["rejection_reason"] = _bounded_string(
                value.get("rejection_reason") or source_ref.get("rejection_reason"), 128
            )
            ref["rule_id"] = _bounded_string(value.get("rule_id") or source_ref.get("rule_id"), 128)
        excerpt = ""
        if not ref.get("rejection_reason"):
            excerpt = _safe_user_chunk(value.get("excerpt"), DEFAULT_MAX_EXCERPT_BYTES)
        speaker = _bounded_string(value.get("speaker"), 32) or FILE_SPEAKER
        if speaker in {"assistant", "user"}:
            speaker = FILE_SPEAKER
        try:
            chunk_index = max(0, int(value.get("chunk_index", 0)))
        except (TypeError, ValueError):
            chunk_index = 0
        try:
            chunk_count = max(1, int(value.get("chunk_count", 1)))
        except (TypeError, ValueError):
            chunk_count = 1
        event = NormalizedEvent(
            tool=tool or "files",
            native_session_id=_bounded_string(value.get("native_session_id"), 256) or _file_native_session_id(source_id, relative_path),
            cursor=_bounded_string(value.get("cursor"), 128) or sha256,
            observed_at=observed,
            completed_at=_optional_timestamp(value.get("completed_at")),
            completion_kind=_bounded_string(value.get("completion_kind"), 64) or "owner_document",
            title=_safe_title(value.get("title"), relative_path or "owner document"),
            excerpt=excerpt,
            source_ref=ref,
            linked_user_cursor="",
            project=project,
            schema_version=FILE_SCHEMA_VERSION,
            event_type=FILE_EVENT_TYPE,
            speaker=speaker,
            provenance=FILE_PROVENANCE,
            chunk_index=chunk_index,
            chunk_count=chunk_count,
            parent_cursor=sha256,
        )
        if value.get("event_id") and not _serialized_event_id_matches(event, value.get("event_id")):
            return None
        return event
    except (TypeError, ValueError):
        return None


def _excerpt_admitted_file(candidate: Any, spec: SourceSpec) -> tuple[list[str] | None, str | None]:
    """Re-read admitted bytes under the size cap and re-run the secret gate.

    Returns ``(chunks, skip_reason)``. ``skip_reason`` is set when the file
    must not become an admitted event (changed, oversize, unreadable, secret,
    or content that cannot be transferred without loss). A secret anywhere
    in the file rejects the whole file before any chunk is emitted.
    """

    from .secrets import inspect_text

    path = Path(candidate.absolute_path)
    max_bytes = int(spec.max_file_bytes or DEFAULT_MAX_FILE_BYTES)
    try:
        size = path.stat().st_size
        if size > max_bytes:
            return None, "max_file_bytes_exceeded"
        data = path.read_bytes()
    except OSError:
        return None, "file_unreadable"
    if len(data) > max_bytes:
        return None, "max_file_bytes_exceeded"
    digest = hashlib.sha256(data).hexdigest()
    if digest != candidate.sha256:
        return None, "file_changed_after_admission"
    verdict = inspect_text(data, path=str(path))
    if not verdict.allowed:
        return None, "secret_detected"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, "file_unreadable"
    cleaned = _strip_injected_lines(text)
    if hashlib.sha256(data).hexdigest() != candidate.sha256:
        return None, "file_changed_after_admission"
    if not cleaned.strip():
        return None, "document_untransferable"
    pieces = _chunk_utf8_lossless(cleaned, spec.max_excerpt_bytes)
    if not pieces:
        return None, "document_untransferable"
    chunks: list[str] = []
    for piece in pieces:
        tick_budget.check('connectors.py:metadata_or_work')
        excerpt = _safe_user_chunk(piece, spec.max_excerpt_bytes)
        if not excerpt:
            return None, "document_untransferable"
        chunks.append(excerpt)
    if "".join(chunks) != cleaned:
        return None, "document_untransferable"
    return chunks, None


def _admission_state_path(spec: SourceSpec, source_id: str) -> Path | None:
    """Per-source admission cursor under the source/spec state directory."""

    raw = _bounded_string(getattr(spec, "state_dir", ""), 4096)
    if not raw:
        return None
    safe_id = _safe_identifier(source_id, "source") or "source"
    return Path(raw) / f"files-admission-{safe_id}.json"


def _rejection_state_path(spec: SourceSpec, source_id: str) -> Path | None:
    """Per-source durable cursor for secret-rejection traces."""

    raw = _bounded_string(getattr(spec, "state_dir", ""), 4096)
    if not raw:
        return None
    safe_id = _safe_identifier(source_id, "source") or "source"
    return Path(raw) / f"files-rejection-{safe_id}.json"


def _empty_rejection_cursor() -> dict[str, Any]:
    return {"rejected": {}}


def _read_rejection_state(path: Path) -> tuple[dict[str, Any], str | None]:
    """Load rejection traces. Corrupt or oversized state is treated as missing."""

    if not path.is_file():
        return _empty_rejection_cursor(), None
    try:
        if path.stat().st_size > 8 * 1024 * 1024:
            return _empty_rejection_cursor(), "rejection_state_unreadable"
        value = json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return _empty_rejection_cursor(), "rejection_state_unreadable"
    if not isinstance(value, Mapping):
        return _empty_rejection_cursor(), "rejection_state_unreadable"
    rejected = value.get("rejected")
    if rejected is None:
        rejected = {}
    if not isinstance(rejected, Mapping):
        return _empty_rejection_cursor(), "rejection_state_unreadable"
    cleaned: dict[str, Any] = {}
    for key, item in rejected.items():
        tick_budget.check('connectors.py:metadata_or_work')
        stored_key = _bounded_rejection_key(key)
        if not stored_key or not isinstance(item, Mapping):
            continue
        fingerprint = _bounded_string(item.get("fingerprint"), 64)
        reason = _bounded_string(item.get("reason"), 128)
        rule = _bounded_string(item.get("rule"), 128)
        if not fingerprint:
            continue
        cleaned[stored_key] = {
            "fingerprint": fingerprint,
            "reason": reason,
            "rule": rule,
        }
    return {"rejected": cleaned}, None


def _bounded_rejection_key(value: Any) -> str:
    """Preserve unit-separator keys. Do not strip: U+001F is whitespace in CPython."""

    if not isinstance(value, str):
        return ""
    value = value.replace("\0", "")
    if not value:
        return ""
    return value[:2048]


def _write_rejection_state(path: Path, cursor: Mapping[str, Any]) -> str | None:
    payload = {
        "rejected": {
            _bounded_rejection_key(key): {
                "fingerprint": _bounded_string(item.get("fingerprint"), 64),
                "reason": _bounded_string(item.get("reason"), 128),
                "rule": _bounded_string(item.get("rule"), 128),
            }
            for key, item in (cursor.get("rejected") or {}).items()
            if isinstance(item, Mapping)
            and _bounded_rejection_key(key)
            and _bounded_string(item.get("fingerprint"), 64)
        }
    }
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    try:
        _atomic_write(path, text)
    except OSError:
        return "rejection_state_write_error"
    return None


def _rejection_cursor_key(relative_path: str, root_path: str = "") -> str:
    """Root-qualified cursor key so two roots sharing a relative path do not collide."""

    rel = _bounded_string(relative_path, 1024).replace("\\", "/") or "rejected"
    root = str(root_path or "").replace("\\", "/")[:1024]
    return f"{root}\x1f{rel}"


def _iter_rejected_candidates(
    spec: SourceSpec,
    relative_path: str,
    project_slug: str = "",
    *,
    claimed_roots: set[str] | None = None,
) -> list[tuple[str, Path]]:
    """Metadata-only candidates under configured roots. Never opens the file."""

    rel = _bounded_string(relative_path, 1024).replace("\\", "/")
    if not rel or rel.startswith("/") or ".." in Path(rel).parts:
        return []
    slug = _bounded_string(project_slug, 128)
    claimed = claimed_roots or set()
    found: list[tuple[str, Path]] = []
    for root in spec.roots:
        tick_budget.check('connectors.py:metadata_or_work')
        if not isinstance(root, Mapping):
            continue
        root_slug = _bounded_string(root.get("project_slug"), 128)
        if root_slug != slug:
            continue
        root_path = Path(str(root.get("path", "")))
        root_key = str(root_path).replace("\\", "/")
        if root_key in claimed:
            continue
        candidate = root_path / rel
        try:
            if candidate.is_file():
                found.append((root_key, candidate))
        except OSError:
            continue
    return found


def _select_rejected_file(
    spec: SourceSpec,
    relative_path: str,
    absolute_path: str = "",
    project_slug: str = "",
    *,
    claimed_roots: set[str] | None = None,
) -> tuple[str, Path | None]:
    """One root+path for identity. Never opens the file."""

    abs_s = _bounded_string(absolute_path, 4096)
    if abs_s:
        path = Path(abs_s)
        root_key = ""
        for root in spec.roots:
            tick_budget.check('connectors.py:metadata_or_work')
            if not isinstance(root, Mapping):
                continue
            candidate_root = str(root.get("path", "")).replace("\\", "/")
            try:
                if path.is_relative_to(Path(str(root.get("path", "")))):
                    root_key = candidate_root
                    break
            except (OSError, ValueError, AttributeError):
                abs_norm = str(path).replace("\\", "/")
                if abs_norm.startswith(candidate_root.rstrip("/") + "/"):
                    root_key = candidate_root
                    break
        return root_key or str(path.parent).replace("\\", "/"), path
    matches = _iter_rejected_candidates(
        spec, relative_path, project_slug, claimed_roots=claimed_roots
    )
    if not matches:
        return "", None
    return matches[0]


def _resolve_rejected_path(
    spec: SourceSpec,
    relative_path: str,
    absolute_path: str = "",
    project_slug: str = "",
) -> Path | None:
    """Locate a rejected file for metadata-only identity. Never opens the file."""

    _root, path = _select_rejected_file(
        spec, relative_path, absolute_path, project_slug
    )
    return path


def _stat_rejection_identity(path: Path | None) -> str:
    """Size + mtime identity. Does not read file contents."""

    if path is None:
        return "nostat"
    try:
        st = path.stat()
    except OSError:
        return "nostat"
    mtime_ns = int(getattr(st, "st_mtime_ns", int(float(st.st_mtime) * 1_000_000_000)))
    return f"{int(st.st_size)}:{mtime_ns}"


def _rejected_identity(
    spec: SourceSpec,
    relative_path: str,
    absolute_path: str = "",
    project_slug: str = "",
    *,
    claimed_roots: set[str] | None = None,
) -> str:
    """Size+mtime of one rejected file. Never reads content."""

    _root, path = _select_rejected_file(
        spec,
        relative_path,
        absolute_path,
        project_slug,
        claimed_roots=claimed_roots,
    )
    return _stat_rejection_identity(path)


def _rejection_fingerprint(
    relative_path: str,
    identity: str,
    reason: str,
    rule: str,
    project_slug: str = "",
    root_path: str = "",
) -> str:
    seed = "\x1f".join(
        (
            str(root_path or "").replace("\\", "/")[:1024],
            _bounded_string(project_slug, 128),
            _bounded_string(relative_path, 1024),
            _bounded_string(identity, 1024),
            _bounded_string(reason, 128),
            _bounded_string(rule, 128),
        )
    )
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _remember_rejection(
    cursor: dict[str, Any],
    relative_path: str,
    fingerprint: str,
    reason: str,
    rule: str,
    project_slug: str = "",
    root_path: str = "",
) -> bool:
    """Return True when this (root, path, file state, rule) pair is new and should emit."""

    key = _rejection_cursor_key(relative_path, root_path)
    rejected = cursor.setdefault("rejected", {})
    previous = rejected.get(key)
    if isinstance(previous, Mapping) and previous.get("fingerprint") == fingerprint:
        return False
    rejected[key] = {
        "fingerprint": fingerprint,
        "reason": _bounded_string(reason, 128),
        "rule": _bounded_string(rule, 128),
    }
    return True


def _read_admission_state(path: Path):
    """Load a cursor file. Unreadable/corrupt state is treated as missing."""

    from .files import FileAdmissionCursor

    if not path.is_file():
        return FileAdmissionCursor(), None
    try:
        if path.stat().st_size > 8 * 1024 * 1024:
            return FileAdmissionCursor(), "admission_state_unreadable"
        value = json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return FileAdmissionCursor(), "admission_state_unreadable"
    if not isinstance(value, Mapping):
        return FileAdmissionCursor(), "admission_state_unreadable"
    pending = value.get("pending")
    stable = value.get("stable")
    if pending is not None and not isinstance(pending, Mapping):
        return FileAdmissionCursor(), "admission_state_unreadable"
    if stable is not None and not isinstance(stable, Mapping):
        return FileAdmissionCursor(), "admission_state_unreadable"
    return FileAdmissionCursor.from_dict(value), None


def _write_admission_state(path: Path, cursor: Any) -> str | None:
    payload = cursor.to_dict()
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    try:
        _atomic_write(path, text)
    except OSError:
        return "admission_state_write_error"
    return None


class FilesConnector(BaseConnector):
    """Allowlisted owner-document folders. Content is untrusted data, never a command."""

    tool: ClassVar[str] = "files"
    _admission_state: ClassVar[dict[str, dict[str, Any]]] = {}
    _rejection_state: ClassVar[dict[str, dict[str, Any]]] = {}

    def collect(self, spec: SourceSpec) -> ConnectorCollection:
        spec = spec.normalized()
        try:
            config = _file_source_config(spec)
        except ValueError:
            return ConnectorCollection((), (_issue(spec, "error", "invalid_source"),), 0)

        from .files import FileAdmissionCursor, scan_files

        source_id = config.source_id
        issues: list[ConnectorIssue] = []
        state_path = _admission_state_path(spec, source_id)
        cursor = FileAdmissionCursor.from_dict(self._admission_state.get(source_id))
        if state_path is not None:
            loaded, load_reason = _read_admission_state(state_path)
            if load_reason:
                issues.append(_issue(spec, "error", load_reason))
                cursor = FileAdmissionCursor()
            else:
                cursor = loaded
        rejection_path = _rejection_state_path(spec, source_id)
        rejection_cursor = dict(self._rejection_state.get(source_id) or _empty_rejection_cursor())
        if "rejected" not in rejection_cursor or not isinstance(rejection_cursor.get("rejected"), dict):
            rejection_cursor = _empty_rejection_cursor()
        else:
            rejection_cursor = {"rejected": dict(rejection_cursor["rejected"])}
        if rejection_path is not None:
            loaded_rej, rej_load_reason = _read_rejection_state(rejection_path)
            if rej_load_reason:
                issues.append(_issue(spec, "error", rej_load_reason))
                rejection_cursor = _empty_rejection_cursor()
            else:
                rejection_cursor = loaded_rej
        candidates, rejections, stats = scan_files(config, cursor, now=time.time())
        snapshot = cursor.to_dict()
        self._admission_state[source_id] = snapshot
        if state_path is not None:
            write_reason = _write_admission_state(state_path, cursor)
            if write_reason:
                issues.append(_issue(spec, "error", write_reason))

        events: list[NormalizedEvent] = []

        if stats.files_deferred_overflow:
            issues.append(_issue(spec, "skipped", "deferred_overflow"))

        for candidate in candidates:
            tick_budget.check('connectors.py:metadata_or_work')
            project = candidate.project_slug.strip() or UNASSIGNED_PROJECT
            mtime_iso = _file_mtime_iso(candidate.mtime)
            observed = (datetime.now(timezone.utc).isoformat(timespec="milliseconds")
                        .replace("+00:00", "Z") if spec.file_observation_mode == "scan_time"
                        else mtime_iso) or (
                datetime.fromtimestamp(0, tz=timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )
            chunks, skip_reason = _excerpt_admitted_file(candidate, spec)
            if skip_reason == "secret_detected":
                identity = f"{int(candidate.size)}:{candidate.mtime}:{candidate.sha256}"
                fingerprint = _rejection_fingerprint(
                    candidate.relative_path,
                    identity,
                    "secret_detected",
                    "secret_in_content",
                    candidate.project_slug,
                    candidate.root_path,
                )
                if _remember_rejection(
                    rejection_cursor,
                    candidate.relative_path,
                    fingerprint,
                    "secret_detected",
                    "secret_in_content",
                    candidate.project_slug,
                    candidate.root_path,
                ):
                    events.append(
                        NormalizedEvent(
                            tool="files",
                            native_session_id=_file_native_session_id(
                                source_id, candidate.relative_path, candidate.project_slug
                            ),
                            cursor=_safe_identifier(
                                _rejection_cursor_key(
                                    candidate.relative_path, candidate.root_path
                                ),
                                "file",
                            ),
                            observed_at=observed,
                            completed_at=None,
                            completion_kind="secret_rejected",
                            title="rejected file",
                            excerpt="",
                            source_ref={
                                "path_or_store": candidate.relative_path,
                                "table_or_stream": "files",
                                "relative_path": candidate.relative_path,
                                "sha256": "",
                                "mtime": "",
                                "source_id": source_id,
                                "rejection_reason": "secret_detected",
                                "rule_id": "secret_in_content",
                            },
                            linked_user_cursor="",
                            project=project,
                            schema_version=FILE_SCHEMA_VERSION,
                            event_type=FILE_EVENT_TYPE,
                            speaker=FILE_SPEAKER,
                            provenance=FILE_PROVENANCE,
                        )
                    )
                continue
            if skip_reason is not None:
                issues.append(_issue(spec, "skipped", skip_reason))
                continue
            pieces = chunks or []
            total = len(pieces)
            if total == 0:
                issues.append(_issue(spec, "skipped", "document_untransferable"))
                continue
            base_cursor = candidate.sha256
            for index, piece in enumerate(pieces):
                tick_budget.check('connectors.py:metadata_or_work')
                if not piece:
                    issues.append(_issue(spec, "skipped", "document_untransferable"))
                    events = [event for event in events if event.source_ref.get("sha256") != candidate.sha256]
                    break
                cursor = base_cursor if total == 1 else f"{base_cursor}:c{index + 1:04d}of{total:04d}"
                events.append(
                    NormalizedEvent(
                        tool="files",
                        native_session_id=_file_native_session_id(source_id, candidate.relative_path),
                        cursor=cursor,
                        observed_at=observed,
                        completed_at=mtime_iso or None,
                        completion_kind="owner_document",
                        title=_safe_title(Path(candidate.relative_path).name, "owner document"),
                        excerpt=piece,
                        source_ref={
                            "path_or_store": candidate.relative_path,
                            "table_or_stream": "files",
                            "relative_path": candidate.relative_path,
                            "sha256": candidate.sha256,
                            "mtime": mtime_iso,
                            "source_id": source_id,
                        },
                        linked_user_cursor="",
                        project=project,
                        schema_version=FILE_SCHEMA_VERSION,
                        event_type=FILE_EVENT_TYPE,
                        speaker=FILE_SPEAKER,
                        provenance=FILE_PROVENANCE,
                        chunk_index=index,
                        chunk_count=total,
                        parent_cursor=base_cursor,
                    )
                )

        claimed_rejection_roots: set[str] = set()
        for rejection in rejections:
            tick_budget.check('connectors.py:metadata_or_work')
            observed = (
                datetime.fromtimestamp(0, tz=timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )
            if rejection.reason in _SECRET_REJECTION_REASONS:
                root_key, rejected_path = _select_rejected_file(
                    spec,
                    rejection.relative_path,
                    rejection.absolute_path,
                    rejection.project_slug,
                    claimed_roots=claimed_rejection_roots,
                )
                if root_key:
                    claimed_rejection_roots.add(root_key)
                identity = _stat_rejection_identity(rejected_path)
                fingerprint = _rejection_fingerprint(
                    rejection.relative_path,
                    identity,
                    rejection.reason,
                    rejection.rule,
                    rejection.project_slug,
                    root_key,
                )
                if _remember_rejection(
                    rejection_cursor,
                    rejection.relative_path,
                    fingerprint,
                    rejection.reason,
                    rejection.rule,
                    rejection.project_slug,
                    root_key,
                ):
                    events.append(
                        NormalizedEvent(
                            tool="files",
                            native_session_id=_file_native_session_id(
                                source_id, rejection.relative_path, rejection.project_slug
                            ),
                            cursor=_safe_identifier(
                                _rejection_cursor_key(
                                    rejection.relative_path, root_key
                                ),
                                "file",
                            ),
                            observed_at=observed,
                            completed_at=None,
                            completion_kind="secret_rejected",
                            title="rejected file",
                            excerpt="",
                            source_ref={
                                "path_or_store": rejection.relative_path,
                                "table_or_stream": "files",
                                "relative_path": rejection.relative_path,
                                "sha256": "",
                                "mtime": "",
                                "source_id": source_id,
                                "rejection_reason": rejection.reason,
                                "rule_id": rejection.rule,
                            },
                            linked_user_cursor="",
                            project=rejection.project_slug.strip() or UNASSIGNED_PROJECT,
                            schema_version=FILE_SCHEMA_VERSION,
                            event_type=FILE_EVENT_TYPE,
                            speaker=FILE_SPEAKER,
                            provenance=FILE_PROVENANCE,
                        )
                    )
            else:
                issues.append(
                    ConnectorIssue(
                        _source_key(spec),
                        "files",
                        "skipped" if rejection.reason != "unsupported_extension" else "unsupported",
                        rejection.reason,
                    )
                )

        self._rejection_state[source_id] = {
            "rejected": dict(rejection_cursor.get("rejected") or {})
        }
        if rejection_path is not None:
            rej_write = _write_rejection_state(rejection_path, rejection_cursor)
            if rej_write:
                issues.append(_issue(spec, "error", rej_write))

        records_seen = len(candidates) + len(rejections)
        return ConnectorCollection(tuple(events), tuple(issues), records_seen)


class JsonlConnector(BaseConnector):
    """Shared terminal/user-link handling for Claude, Qwen and Grok."""

    terminal_types: ClassVar[frozenset[str]] = frozenset()
    assistant_types: ClassVar[frozenset[str]] = frozenset({"assistant", "agent_message", "agentmessage"})
    user_types: ClassVar[frozenset[str]] = frozenset({"user", "user_message", "usermessage"})

    def _is_assistant(self, row: _Row) -> bool:
        kind = (row.outer_type or _bounded_string(row.payload.get("type"), 80)).lower()
        return row.role == "assistant" or kind in self.assistant_types

    def _is_user(self, row: _Row) -> bool:
        kind = (row.outer_type or _bounded_string(row.payload.get("type"), 80)).lower()
        return row.role == "user" or kind in self.user_types

    def _answer(self, row: _Row) -> str:
        # ``output`` is accepted only through _extract_text, which rejects
        # structured tool/result blocks.  The role and terminal checks happen
        # before this function is called.
        return _row_text(
            row,
            ("content", "text", "answer", "response", "message", "output"),
        )

    def collect(self, spec: SourceSpec) -> ConnectorCollection:
        rows, issues, records_seen = _read_json_rows(spec)
        # Fill the session id from an explicit spec or the first observed row;
        # exports with no session id remain unsupported at event level.
        fallback_session = spec.native_session_id or next(
            (row.session_id for row in rows if row.session_id), ""
        )
        for row in rows:
            tick_budget.check('connectors.py:metadata_or_work')
            if not row.session_id:
                row.session_id = fallback_session
            if self._is_user(row):
                row.role = "user"
            elif self._is_assistant(row):
                row.role = "assistant"

        events: list[NormalizedEvent] = []
        last_user_order: dict[str, int] = {}
        reported_turn_issues: set[tuple[str, str, str]] = set()

        def report_issue(row: _Row, reason: str) -> None:
            if self.tool == "claude":
                session = _row_session(row, spec, fallback_session)
                turn = row.turn_id or str(last_user_order.get(session, 0))
                key = (session, turn, reason)
                if key in reported_turn_issues:
                    return
                reported_turn_issues.add(key)
            issues.append(_issue(spec, "skipped", reason, row.line_no or None))

        for row in rows:
            tick_budget.check('connectors.py:metadata_or_work')
            if self.tool == "claude" and row.role == "user":
                last_user_order[_row_session(row, spec, fallback_session)] = row.order
            if row.role != "assistant":
                continue
            if _session_excluded(spec, _row_session(row, spec, fallback_session)):
                issues.append(_issue(spec, "skipped", "excluded_session", row.line_no or None))
                continue
            if self.tool == "claude" and _claude_tool_use_stop(row.payload):
                continue
            terminal, marker = _has_terminal_marker(
                row.payload, _bounded_string(row.payload.get("type") or row.outer_type, 80)
            )
            if not terminal:
                if self.tool != "claude" or marker.lower() not in {
                    "stop_reason:tool_use", "stopreason:tool_use"
                }:
                    report_issue(row, marker)
                continue
            if self.tool == "claude" and not _timestamp(row.payload.get("timestamp")):
                # Keep an independently supplied observation when the native
                # timestamp is malformed; it still cannot prove completion.
                alternate = _timestamp(row.payload.get("observed_at"))
                if alternate:
                    row.observed = alternate
            linked = _linked_user(rows, row, spec.max_excerpt_bytes)
            answer = self._answer(row)
            cursor = _row_cursor(row, "assistant")
            if not linked or not answer or not cursor:
                reason = "missing_linked_user" if not linked else "missing_final_answer"
                if answer and _answer_needs_full_source(answer, spec.max_excerpt_bytes):
                    reason = "answer_needs_full_source"
                report_issue(row, reason)
                continue
            completed_at = _first_value(row.payload, "completed_at", "completedAt")
            if completed_at is None and self.tool == "claude" and marker.startswith("stop_reason:"):
                # Claude Code closes a turn on the assistant row itself.  Only
                # its native, timezone-aware timestamp proves completion here;
                # an alternative observed_at field is not that proof.
                native_time = row.payload.get("timestamp")
                if isinstance(native_time, str) and re.fullmatch(
                    r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)", native_time
                ):
                    completed_at = _optional_timestamp(native_time) or None
            event = _make_event(
                spec,
                row,
                session_id=_row_session(row, spec, fallback_session),
                cursor=cursor,
                answer=answer,
                linked_user_cursor=linked,
                completion_kind=marker,
                table_or_stream=f"jsonl:{row.outer_type or row.payload.get('type', 'record')}",
                completed_at=completed_at,
            )
            if event:
                events.append(event)
            else:
                report_issue(row, "unsafe_or_invalid_record")
        if spec.include_user_messages:
            _extra_events, _extra_issues = _jsonl_user_evidence(spec, rows, fallback_session)
            events.extend(_extra_events)
            issues.extend(_extra_issues)
        return ConnectorCollection(_deduplicate_events(events), tuple(issues), records_seen)


class ClaudeConnector(JsonlConnector):
    tool = "claude"


class QwenConnector(JsonlConnector):
    tool = "qwen"


class GrokConnector(JsonlConnector):
    tool = "grok"


class CodexConnector(BaseConnector):
    """Reader for the actual nested Codex rollout JSONL dialect."""

    tool = "codex"

    def collect(self, spec: SourceSpec) -> ConnectorCollection:
        rows, issues, records_seen = _read_json_rows(spec)
        session_meta: dict[str, Mapping[str, Any]] = {}
        turn_context: dict[str, tuple[int, str, str, str]] = {}
        fallback_session = spec.native_session_id or ""
        current_turn = ""
        users: list[_Row] = []
        assistant_items: list[_Row] = []
        terminals: list[_Row] = []

        for row in rows:
            # ``session_meta`` and ``turn_context`` use the outer type in
            # current Codex rollouts, while ``task_complete`` is nested under
            # event_msg.payload.  Accept both placements.
            tick_budget.check('connectors.py:metadata_or_work')
            nested_type = _bounded_string(
                row.payload.get("type") or row.outer_type, 80
            ).lower()
            if nested_type == "session_meta":
                sid = _bounded_string(
                    _first_value(row.payload, "session_id", "sessionId"), 256
                )
                if sid:
                    session_meta[sid] = row.payload
                    fallback_session = fallback_session or sid
                continue
            if nested_type == "turn_context":
                tid = _bounded_string(
                    _first_value(row.payload, "turn_id", "turnId", "root_turn_id"), 256
                )
                if tid:
                    current_turn = tid
                    turn_context[tid] = (
                        row.order,
                        _bounded_string(row.provider or row.payload.get("model_provider"), 128),
                        _bounded_string(row.model or row.payload.get("model"), 128),
                        _safe_title(row.title or row.payload.get("summary"), "codex completed turn"),
                    )
                continue
            if not row.turn_id and current_turn:
                row.turn_id = current_turn
            if not row.session_id:
                row.session_id = fallback_session
            # A real Codex response item is nested under payload and uses
            # role=assistant/user.  item_completed carries the same item one
            # level deeper and is associated through its turn_id.
            item = row.payload.get("item")
            if isinstance(item, Mapping):
                item_type = _bounded_string(item.get("type"), 80).lower()
                item_role = _bounded_string(item.get("role"), 32).lower()
                if item_type.replace("_", "") in {"usermessage", "user_message"}:
                    item_role = "user"
                elif item_type.replace("_", "") in {"agentmessage", "assistant"}:
                    item_role = "assistant"
                if item_role:
                    row.role = item_role
                if not row.native_id:
                    row.native_id = _bounded_string(item.get("id"), 512)
                if not row.payload.get("content") and "content" in item:
                    row.payload["content"] = item.get("content")
                if not row.payload.get("phase") and "phase" in item:
                    row.payload["phase"] = item.get("phase")
            if row.role == "user":
                users.append(row)
            elif row.role == "assistant":
                phase = _bounded_string(row.payload.get("phase"), 64).lower()
                if not phase or phase in {"final_answer", "final", "answer"}:
                    assistant_items.append(row)
            # Item completions can carry generic terminal flags, but they do
            # not close a Codex turn.
            if nested_type in {"task_complete", "task.complete", "turn_completed", "turn.completed"}:
                terminal, marker = _has_terminal_marker(row.payload, nested_type)
                if terminal:
                    row.terminal = True
                    row.completed = _first_value(
                        row.payload, "completed_at", "completedAt"
                    )
                    row.payload["_completion_marker"] = marker
                    terminals.append(row)

        # A rollout normally starts with session_meta, but bounded captures
        # can begin at a later line.  Fill missing ids after the metadata pass
        # so a linked user record is never lost only because it preceded the
        # first metadata line.
        for row in rows:
            tick_budget.check('connectors.py:metadata_or_work')
            if not row.session_id:
                row.session_id = fallback_session

        # Session metadata is attached after rows are built.  No session meta
        # text is copied except a bounded title/model/provider scalar.
        events: list[NormalizedEvent] = []
        for terminal in terminals:
            tick_budget.check('connectors.py:metadata_or_work')
            tid = terminal.turn_id or _bounded_string(
                _first_value(terminal.payload, "turn_id", "turnId"), 256
            )
            sid = _row_session(terminal, spec, fallback_session)
            if not sid and len(session_meta) == 1:
                sid = next(iter(session_meta))
            if _session_excluded(spec, sid):
                issues.append(_issue(spec, "skipped", "excluded_session", terminal.line_no or None))
                continue
            candidate_floor = _latest_gap_order(rows, terminal.order)
            candidates = [
                row
                for row in assistant_items
                if candidate_floor < row.order < terminal.order
                and row.session_id == sid
                and (not tid or not row.turn_id or row.turn_id == tid)
            ]
            answer = _extract_from_fields(
                terminal.payload,
                ("last_agent_message", "final_answer", "answer", "content", "text"),
            )
            if not answer and candidates:
                answer = _extract_from_fields(
                    candidates[-1].payload,
                    ("content", "text", "message", "answer", "response"),
                )
            # Keep gap sentinels in the linkage view.  A skipped oversized
            # record must cut both the assistant-answer and user relations.
            link_rows = rows
            terminal_for_link = terminal
            terminal_for_link.session_id = sid
            terminal_for_link.turn_id = tid
            linked = _linked_user(link_rows, terminal_for_link, spec.max_excerpt_bytes)
            if not linked:
                meta = session_meta.get(sid, {})
                subagent = (
                    _bounded_string(meta.get("thread_source"), 80).lower() == "subagent"
                    or bool(meta.get("parent_thread_id"))
                )
                issues.append(_issue(spec, "info" if subagent else "skipped", "missing_linked_user", terminal.line_no or None))
                continue
            if answer and _answer_needs_full_source(answer, spec.max_excerpt_bytes):
                issues.append(_issue(spec, "skipped", "answer_needs_full_source", terminal.line_no or None))
                continue
            cursor_raw = tid or terminal.cursor_raw or terminal.native_id
            if not cursor_raw:
                issues.append(_issue(spec, "skipped", "missing_native_cursor", terminal.line_no or None))
                continue
            meta = session_meta.get(sid, {})
            provider = terminal.provider or _bounded_string(
                _first_value(meta, "model_provider", "provider", "provider_id"), 128
            )
            model = terminal.model or _bounded_string(
                _first_value(meta, "model", "model_id"), 128
            )
            title = terminal.title or _safe_title(meta.get("title"), "codex completed turn")
            if tid in turn_context:
                _, context_provider, context_model, context_title = turn_context[tid]
                provider = provider or context_provider
                model = model or context_model
                title = title or context_title
            event = _make_event(
                spec,
                terminal,
                session_id=sid,
                cursor=_safe_identifier(cursor_raw, "turn"),
                answer=answer,
                linked_user_cursor=linked,
                completion_kind=_bounded_string(terminal.payload.get("_completion_marker"), 64) or "terminal",
                table_or_stream="jsonl:event_msg.task_complete",
                title=title,
                provider=provider,
                model=model,
                project=_first_value(meta, "project", "project_id", "projectId", "workspace") or "",
                cwd=_first_value(meta, "cwd", "working_directory", "workingDirectory") or "",
                completed_at=terminal.completed,
            )
            if event:
                events.append(event)
            else:
                issues.append(_issue(spec, "skipped", "unsafe_or_invalid_record", terminal.line_no or None))
        if spec.include_user_messages:
            _extra_events, _extra_issues = _codex_user_evidence(
                spec,
                users,
                terminals,
                tuple(row.order for row in rows if row.gap),
            )
            events.extend(_extra_events)
            issues.extend(_extra_issues)
        return ConnectorCollection(_deduplicate_events(events), tuple(issues), records_seen)


SQLITE_TABLES: dict[str, frozenset[str]] = {
    "opencode": frozenset({"session", "session_message", "message", "part", "event"}),
    "hermes": frozenset({"sessions", "messages"}),
    "minimax": frozenset(
        {
            "local_runtime_sessions",
            "local_runtime_message_rows",
            "local_runtime_session_agent_state",
            "local_runtime_turn_ingress",
        }
    ),
}

SQLITE_COLUMN_ALLOWLIST: dict[str, dict[str, tuple[str, ...]]] = {
    "opencode": {
        "session": (
            "id",
            "title",
            "model",
            "agent",
            "project_id",
            "directory",
            "time_created",
            "time_updated",
        ),
        "session_message": (
            "id",
            "session_id",
            "type",
            "seq",
            "time_created",
            "time_updated",
            "data",
        ),
        "message": ("id", "session_id", "time_created", "time_updated", "data"),
        "part": ("id", "message_id", "session_id", "time_created", "time_updated", "data"),
        "event": ("id", "aggregate_id", "seq", "type", "data"),
    },
    "hermes": {
        "sessions": (
            "id",
            "source",
            "session_key",
            "display_name",
            "model",
            "title",
            "started_at",
            "ended_at",
            "end_reason",
            "last_activity_at",
            "cwd",
            "git_repo_root",
            "billing_provider",
        ),
        # Deliberately omit reasoning, tool calls, auth and API mirror fields.
        "messages": (
            "id",
            "session_id",
            "role",
            "content",
            "timestamp",
            "finish_reason",
            "active",
            "compacted",
            "display_kind",
            "display_order",
        ),
    },
    "minimax": {
        "local_runtime_sessions": (
            "session_id",
            "agent_name",
            "runtime",
            "session_type",
            "status",
            "workspace_dir",
            "project_workspace_dir",
            "title",
            "created_at_ms",
            "project_id",
        ),
        "local_runtime_message_rows": (
            "id",
            "session_id",
            "msg_id",
            "role",
            "turn_id",
            "created_at_ms",
            "data_json",
            "source",
        ),
        "local_runtime_session_agent_state": (
            "session_id",
            "turn_id",
            "turn_sequence",
            "runtime_seq",
            "event_id",
            "terminal_outcome",
            "updated_at_ms",
        ),
        # input_json is intentionally excluded: it contains the user request
        # rather than a cursor-only link.
        "local_runtime_turn_ingress": (
            "turn_id",
            "session_id",
            "status",
            "accepted_at_ms",
            "accepted_sequence",
            "completed_at_ms",
            "input_digest",
        ),
    },
}

SQLITE_TEXT_COLUMNS = frozenset(
    {
        "agent",
        "answer",
        "billing_provider",
        "content",
        "cwd",
        "data",
        "data_json",
        "directory",
        "display_kind",
        "display_name",
        "end_reason",
        "finish_reason",
        "git_repo_root",
        "model",
        "path",
        "project_id",
        "role",
        "session_key",
        "source",
        "title",
        "type",
    }
)


def _quote_identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError("unsafe SQLite identifier")
    return '"' + value + '"'


def _sqlite_connection(path: str | os.PathLike[str]) -> sqlite3.Connection:
    absolute = os.path.abspath(str(path))
    connection = sqlite3.connect(
        f"file:{absolute.replace(chr(92), '/') }?mode=ro",
        uri=True,
        timeout=0.5,
    )
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=500")
    return connection


def _table_names(connection: sqlite3.Connection) -> tuple[str, ...]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    return tuple(str(row[0]) for row in rows if row and isinstance(row[0], str))


def _table_columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    quoted = _quote_identifier(table)
    return tuple(str(row[1]) for row in connection.execute(f"PRAGMA table_info({quoted})").fetchall())


def _select_sqlite_rows(
    connection: sqlite3.Connection,
    table: str,
    columns: Sequence[str],
    *,
    limit: int,
    order_by: str | None = None,
    value_limit: int | None = None,
) -> list[dict[str, Any]]:
    available = set(_table_columns(connection, table))
    selected = [column for column in columns if column in available]
    if not selected:
        return []
    parameters: list[Any] = []
    select_parts: list[str] = []
    for column in selected:
        tick_budget.check('connectors.py:metadata_or_work')
        if value_limit and column in SQLITE_TEXT_COLUMNS:
            select_parts.append(
                f"substr({_quote_identifier(column)}, 1, ?) AS {_quote_identifier(column)}"
            )
            parameters.append(int(value_limit))
        else:
            select_parts.append(_quote_identifier(column))
    select = ", ".join(select_parts)
    sql = f"SELECT {select} FROM {_quote_identifier(table)}"
    if order_by and order_by in available:
        sql += f" ORDER BY {_quote_identifier(order_by)} ASC"
    sql += " LIMIT ?"
    parameters.append(int(limit))
    rows = connection.execute(sql, tuple(parameters)).fetchall()
    return [dict(zip(selected, row)) for row in rows]


def _json_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _sqlite_value_limit(spec: SourceSpec) -> int:
    # A terminal excerpt never needs the whole native body.  Keep enough room
    # for a structured row, while preventing many bounded rows from multiplying
    # an 8 MiB source budget into unbounded process memory.
    return min(spec.max_bytes, max(64 * 1024, spec.max_excerpt_bytes * 16))


def _sqlite_session_metadata(rows: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        tick_budget.check('connectors.py:metadata_or_work')
        sid = _bounded_string(row.get("id"), 256)
        if sid:
            result[sid] = row
    return result


class OpenCodeSQLiteConnector(BaseConnector):
    tool = "opencode"

    def collect(self, spec: SourceSpec) -> ConnectorCollection:
        path = Path(str(spec.path))
        if not path.is_file():
            return ConnectorCollection(issues=(_issue(spec, "unavailable", "source_missing"),))
        rows: list[_Row] = []
        issues: list[ConnectorIssue] = []
        records_seen = 0
        try:
            connection = _sqlite_connection(path)
            tables = set(_table_names(connection))
            if "session" not in tables:
                connection.close()
                return ConnectorCollection(
                    issues=(_issue(spec, "error", "unsupported_sqlite_schema"),)
                )
            session_rows = _select_sqlite_rows(
                connection,
                "session",
                SQLITE_COLUMN_ALLOWLIST["opencode"]["session"],
                limit=spec.max_records,
                order_by="time_created",
                value_limit=_sqlite_value_limit(spec),
            )
            sessions = _sqlite_session_metadata(session_rows)
            order = 0
            for table in ("session_message", "message"):
                tick_budget.check('connectors.py:metadata_or_work')
                if table not in tables:
                    continue
                selected = _select_sqlite_rows(
                    connection,
                    table,
                    SQLITE_COLUMN_ALLOWLIST["opencode"][table],
                    limit=spec.max_records,
                    order_by="seq" if table == "session_message" else "time_created",
                    value_limit=_sqlite_value_limit(spec),
                )
                records_seen += len(selected)
                for raw in selected:
                    tick_budget.check('connectors.py:metadata_or_work')
                    data = _json_mapping(raw.get("data"))
                    info = data.get("info") if isinstance(data.get("info"), Mapping) else {}
                    if info:
                        merged = dict(info)
                        merged.update(data)
                        data = merged
                    sid = _bounded_string(
                        raw.get("session_id") or _first_value(data, "sessionID", "session_id", "conversationID"),
                        256,
                    )
                    msg_id = _bounded_string(raw.get("id") or data.get("id"), 512)
                    role = _bounded_string(raw.get("type") or data.get("role"), 32).lower()
                    if role not in {"assistant", "user"}:
                        role = _bounded_string(data.get("role"), 32).lower()
                    session = sessions.get(sid, {})
                    completed = _first_value(data, "completed_at", "completedAt")
                    time_value = data.get("time")
                    if isinstance(time_value, Mapping):
                        completed = completed or time_value.get("completed")
                    row = _Row(
                        order=order,
                        outer_type=role or table,
                        payload=data,
                        session_id=sid,
                        native_id=msg_id,
                        cursor_raw=_bounded_string(raw.get("seq"), 128) or msg_id,
                        parent_id=_bounded_string(
                            _first_value(data, "parentID", "parentId", "parent_id"), 512
                        ),
                        role=role,
                        observed=raw.get("time_created") or _first_value(data, "created_at", "createdAt") or session.get("time_created"),
                        completed=completed or _first_value(data, "finish", "finish_reason", "finishReason"),
                        title=_bounded_string(session.get("title"), 200),
                        model=_bounded_string(session.get("model"), 128),
                        provider=_bounded_string(session.get("provider"), 128),
                        project=_bounded_string(session.get("project_id"), 256),
                        cwd=_bounded_string(session.get("directory"), 1024),
                        source_table=table,
                    )
                    row.terminal, _ = _has_terminal_marker(data, role)
                    rows.append(row)
                    order += 1

            # Parts contain assistant text in some OpenCode versions.  The
            # allowlist accepts only text-shaped parts; tool parts are ignored.
            parts_by_message: dict[str, list[str]] = {}
            if "part" in tables:
                part_rows = _select_sqlite_rows(
                    connection,
                    "part",
                    SQLITE_COLUMN_ALLOWLIST["opencode"]["part"],
                    limit=spec.max_records * 4,
                    order_by="time_created",
                    value_limit=_sqlite_value_limit(spec),
                )
                records_seen += len(part_rows)
                for raw in part_rows:
                    tick_budget.check('connectors.py:metadata_or_work')
                    data = _json_mapping(raw.get("data"))
                    item_type = _bounded_string(data.get("type"), 80).lower()
                    if item_type.replace("_", "") in {
                        x.replace("_", "") for x in BLOCKED_ITEM_TYPES
                    }:
                        continue
                    text = _extract_from_fields(data, ("text", "content"))
                    message_id = _bounded_string(raw.get("message_id"), 512)
                    if text and message_id:
                        bucket = parts_by_message.setdefault(message_id, [])
                        current_bytes = sum(
                            len(item.encode("utf-8", errors="replace")) for item in bucket
                        )
                        remaining = _sqlite_value_limit(spec) - current_bytes
                        if remaining > 0:
                            bucket.append(_clip_utf8(text, remaining))

            # Some OpenCode revisions only expose the terminal transition in
            # ``event``.  Read that table through the same allowlist and keep
            # only events that identify an assistant or contain an explicit
            # completed marker.  Credential/account/share tables are never
            # queried.
            if "event" in tables:
                event_rows = _select_sqlite_rows(
                    connection,
                    "event",
                    SQLITE_COLUMN_ALLOWLIST["opencode"]["event"],
                    limit=spec.max_records,
                    order_by="seq",
                    value_limit=_sqlite_value_limit(spec),
                )
                records_seen += len(event_rows)
                for raw in event_rows:
                    tick_budget.check('connectors.py:metadata_or_work')
                    data = _json_mapping(raw.get("data"))
                    properties = data.get("properties") if isinstance(data.get("properties"), Mapping) else {}
                    if properties:
                        merged = dict(properties)
                        merged.update(data)
                        data = merged
                    message = data.get("message") if isinstance(data.get("message"), Mapping) else {}
                    sid = _bounded_string(
                        raw.get("aggregate_id")
                        or _first_value(data, "sessionID", "session_id", "conversationID")
                        or _first_value(message, "sessionID", "session_id"),
                        256,
                    )
                    role = _bounded_string(
                        _first_value(data, "role") or _first_value(message, "role"), 32
                    ).lower()
                    event_type = _bounded_string(raw.get("type") or data.get("type"), 96).lower()
                    if not role and any(token in event_type for token in ("assistant", "message.completed", "turn.completed")):
                        role = "assistant"
                    if role not in {"assistant", "user"}:
                        continue
                    msg_id = _bounded_string(
                        raw.get("id") or data.get("id") or message.get("id"), 512
                    )
                    event_row = _Row(
                        order=order,
                        outer_type=event_type or "event",
                        payload=data,
                        session_id=sid,
                        native_id=msg_id,
                        cursor_raw=_bounded_string(raw.get("seq"), 128) or msg_id,
                        parent_id=_bounded_string(
                            _first_value(data, "parentID", "parentId", "parent_id"), 512
                        ),
                        role=role,
                        observed=_first_value(data, "timestamp", "created_at", "createdAt")
                        or _first_value(sessions.get(sid, {}), "time_updated", "time_created"),
                        completed=_first_value(data, "completed_at", "completedAt", "finish", "finish_reason", "finishReason"),
                        title=_bounded_string(sessions.get(sid, {}).get("title"), 200),
                        model=_bounded_string(sessions.get(sid, {}).get("model"), 128),
                        source_table="event",
                    )
                    event_row.terminal, _ = _has_terminal_marker(data, event_type)
                    rows.append(event_row)
                    order += 1
            connection.close()
        except (OSError, sqlite3.Error, ValueError):
            return ConnectorCollection(issues=(_issue(spec, "error", "sqlite_read_error"),))

        for row in rows:
            tick_budget.check('connectors.py:metadata_or_work')
            if not _row_text(row, ("content", "text", "answer", "response", "message", "output")):
                message_parts = parts_by_message.get(row.native_id, [])
                if message_parts:
                    row.payload["content"] = "\n".join(message_parts)
            if not row.session_id and spec.native_session_id:
                row.session_id = spec.native_session_id
        events: list[NormalizedEvent] = []
        for row in rows:
            tick_budget.check('connectors.py:metadata_or_work')
            if row.role != "user":
                continue
            # User rows are retained only in memory for the link check.
        for row in rows:
            tick_budget.check('connectors.py:metadata_or_work')
            if row.role != "assistant":
                continue
            if _session_excluded(spec, row.session_id):
                issues.append(_issue(spec, "skipped", "excluded_session"))
                continue
            terminal, marker = _has_terminal_marker(row.payload, row.role)
            if not terminal:
                issues.append(_issue(spec, "skipped", marker))
                continue
            linked = _linked_user(rows, row, spec.max_excerpt_bytes)
            answer = _row_text(row, ("content", "text", "answer", "response", "message", "output"))
            cursor = _row_cursor(row, "message")
            if not linked or not answer or not cursor:
                issues.append(_issue(spec, "skipped", "missing_linked_user" if not linked else "missing_final_answer"))
                continue
            if _answer_needs_full_source(answer, spec.max_excerpt_bytes):
                issues.append(_issue(spec, "skipped", "answer_needs_full_source"))
                continue
            event = _make_event(
                spec,
                row,
                session_id=row.session_id,
                cursor=cursor,
                answer=answer,
                linked_user_cursor=linked,
                completion_kind=marker,
                table_or_stream=row.source_table or "session_message",
                title=row.title,
                provider=row.provider,
                model=row.model,
                completed_at=row.completed,
            )
            if event:
                events.append(event)
        if spec.include_user_messages:
            _extra_events, _extra_issues = _tabular_user_evidence(spec, rows)
            events.extend(_extra_events)
            issues.extend(_extra_issues)
        return ConnectorCollection(_deduplicate_events(events), tuple(issues), records_seen)


class HermesSQLiteConnector(BaseConnector):
    tool = "hermes"

    @staticmethod
    def _session_is_complete(row: Mapping[str, Any]) -> bool:
        if row.get("ended_at") in (None, ""):
            return False
        reason = _bounded_string(row.get("end_reason"), 80).lower()
        if not reason:
            return False
        return reason not in {
            "active",
            "pending",
            "running",
            "error",
            "failed",
            "cancelled",
            "canceled",
            "interrupted",
        }

    def collect(self, spec: SourceSpec) -> ConnectorCollection:
        path = Path(str(spec.path))
        if not path.is_file():
            return ConnectorCollection(issues=(_issue(spec, "unavailable", "source_missing"),))
        issues: list[ConnectorIssue] = []
        records_seen = 0
        rows: list[_Row] = []
        try:
            connection = _sqlite_connection(path)
            tables = set(_table_names(connection))
            if not {"sessions", "messages"}.issubset(tables):
                connection.close()
                return ConnectorCollection(issues=(_issue(spec, "error", "unsupported_sqlite_schema"),))
            session_rows = _select_sqlite_rows(
                connection,
                "sessions",
                SQLITE_COLUMN_ALLOWLIST["hermes"]["sessions"],
                limit=spec.max_records,
                order_by="started_at",
                value_limit=_sqlite_value_limit(spec),
            )
            sessions = {
                _bounded_string(row.get("id"), 256): row
                for row in session_rows
                if _bounded_string(row.get("id"), 256)
                and self._session_is_complete(row)
            }
            message_rows = _select_sqlite_rows(
                connection,
                "messages",
                SQLITE_COLUMN_ALLOWLIST["hermes"]["messages"],
                limit=spec.max_records * 2,
                order_by="display_order",
                value_limit=_sqlite_value_limit(spec),
            )
            records_seen += len(session_rows) + len(message_rows)
            for order, raw in enumerate(message_rows):
                tick_budget.check('connectors.py:metadata_or_work')
                sid = _bounded_string(raw.get("session_id"), 256)
                session = sessions.get(sid)
                if session is None:
                    continue
                role = _bounded_string(raw.get("role"), 32).lower()
                if role not in {"assistant", "user"}:
                    continue
                display_kind = _bounded_string(raw.get("display_kind"), 64).lower()
                if display_kind in {"reasoning", "thinking", "tool", "tool_result", "tool_call"}:
                    continue
                payload = {
                    "role": role,
                    "content": raw.get("content"),
                    "finish_reason": raw.get("finish_reason"),
                    "active": raw.get("active"),
                    "compacted": raw.get("compacted"),
                }
                row = _Row(
                    order=order,
                    outer_type=role,
                    payload=payload,
                    session_id=sid,
                    native_id=_bounded_string(raw.get("id"), 256),
                    cursor_raw=_bounded_string(raw.get("id"), 256),
                    role=role,
                    observed=raw.get("timestamp") or session.get("started_at"),
                    completed=session.get("ended_at"),
                    title=_bounded_string(session.get("title") or session.get("display_name"), 200),
                    provider=_bounded_string(session.get("billing_provider") or session.get("source"), 128),
                    model=_bounded_string(session.get("model"), 128),
                    project=_bounded_string(session.get("git_repo_root"), 256),
                    cwd=_bounded_string(session.get("cwd"), 1024),
                    source_table="messages",
                )
                if role == "assistant":
                    finish = _bounded_string(raw.get("finish_reason"), 80).lower()
                    row.terminal = finish in TERMINAL_VALUES
                    if bool(raw.get("active")) or bool(raw.get("compacted")):
                        row.terminal = False
                rows.append(row)
            connection.close()
        except (OSError, sqlite3.Error, ValueError):
            return ConnectorCollection(issues=(_issue(spec, "error", "sqlite_read_error"),))

        events: list[NormalizedEvent] = []
        for row in rows:
            tick_budget.check('connectors.py:metadata_or_work')
            if row.role != "assistant":
                continue
            if _session_excluded(spec, row.session_id):
                issues.append(_issue(spec, "skipped", "excluded_session"))
                continue
            if not row.terminal:
                issues.append(_issue(spec, "skipped", "missing_terminal_marker"))
                continue
            linked = _linked_user(rows, row, spec.max_excerpt_bytes)
            answer = _row_text(row, ("content", "text", "answer", "response"))
            cursor = _row_cursor(row, "message")
            if not linked or not answer or not cursor:
                issues.append(_issue(spec, "skipped", "missing_linked_user" if not linked else "missing_final_answer"))
                continue
            if _answer_needs_full_source(answer, spec.max_excerpt_bytes):
                issues.append(_issue(spec, "skipped", "answer_needs_full_source"))
                continue
            event = _make_event(
                spec,
                row,
                session_id=row.session_id,
                cursor=cursor,
                answer=answer,
                linked_user_cursor=linked,
                completion_kind=f"finish_reason:{_bounded_string(row.payload.get('finish_reason'), 64)}",
                table_or_stream="messages",
                title=row.title,
                provider=row.provider,
                model=row.model,
                completed_at=row.completed,
            )
            if event:
                events.append(event)
        if spec.include_user_messages:
            _extra_events, _extra_issues = _tabular_user_evidence(spec, rows)
            events.extend(_extra_events)
            issues.extend(_extra_issues)
        return ConnectorCollection(_deduplicate_events(events), tuple(issues), records_seen)


class MiniMaxSQLiteConnector(BaseConnector):
    """Reader for MiniMax local-runtime completed turns.

    The installed runtime uses ``local_runtime_*`` tables.  The final message
    field is the native ``msg_content`` key inside ``data_json``; completion is
    admitted only when the same native ``turn_id`` is marked completed by the
    turn-ingress/state tables and the message's ``finish_reason`` is ``stop``.
    """

    tool = "minimax"

    @staticmethod
    def _completed_turns(
        ingress_rows: Sequence[Mapping[str, Any]],
        state_rows: Sequence[Mapping[str, Any]],
    ) -> dict[str, tuple[str, Any, str]]:
        result: dict[str, tuple[str, Any, str]] = {}
        for raw in ingress_rows:
            tick_budget.check('connectors.py:metadata_or_work')
            turn_id = _bounded_string(raw.get("turn_id"), 256)
            session_id = _bounded_string(raw.get("session_id"), 256)
            status = _bounded_string(raw.get("status"), 64).lower()
            if turn_id and session_id and status == "completed":
                result[turn_id] = (
                    session_id,
                    raw.get("completed_at_ms"),
                    "turn_ingress:completed",
                )
        for raw in state_rows:
            tick_budget.check('connectors.py:metadata_or_work')
            turn_id = _bounded_string(raw.get("turn_id"), 256)
            session_id = _bounded_string(raw.get("session_id"), 256)
            outcome = _bounded_string(raw.get("terminal_outcome"), 64).lower()
            if (
                turn_id
                and session_id
                and outcome in {"completed", "complete", "terminal"}
                and turn_id not in result
            ):
                result[turn_id] = (
                    session_id,
                    raw.get("updated_at_ms"),
                    f"session_state:{outcome}",
                )
        return result

    def collect(self, spec: SourceSpec) -> ConnectorCollection:
        path = Path(str(spec.path))
        if not path.is_file():
            return ConnectorCollection(
                issues=(_issue(spec, "unavailable", "source_missing"),)
            )
        issues: list[ConnectorIssue] = []
        rows: list[_Row] = []
        records_seen = 0
        try:
            connection = _sqlite_connection(path)
            tables = set(_table_names(connection))
            required = {
                "local_runtime_sessions",
                "local_runtime_message_rows",
                "local_runtime_turn_ingress",
            }
            if not required.issubset(tables):
                connection.close()
                return ConnectorCollection(
                    issues=(_issue(spec, "error", "unsupported_sqlite_schema"),)
                )
            value_limit = _sqlite_value_limit(spec)
            session_rows = _select_sqlite_rows(
                connection,
                "local_runtime_sessions",
                SQLITE_COLUMN_ALLOWLIST["minimax"]["local_runtime_sessions"],
                limit=spec.max_records * 2,
                order_by="created_at_ms",
                value_limit=value_limit,
            )
            sessions = {
                _bounded_string(raw.get("session_id"), 256): raw
                for raw in session_rows
                if _bounded_string(raw.get("session_id"), 256)
            }
            ingress_rows = _select_sqlite_rows(
                connection,
                "local_runtime_turn_ingress",
                SQLITE_COLUMN_ALLOWLIST["minimax"]["local_runtime_turn_ingress"],
                limit=spec.max_records * 4,
                order_by="accepted_at_ms",
                value_limit=value_limit,
            )
            state_rows: list[dict[str, Any]] = []
            if "local_runtime_session_agent_state" in tables:
                state_rows = _select_sqlite_rows(
                    connection,
                    "local_runtime_session_agent_state",
                    SQLITE_COLUMN_ALLOWLIST["minimax"]["local_runtime_session_agent_state"],
                    limit=spec.max_records * 2,
                    order_by="updated_at_ms",
                    value_limit=value_limit,
                )
            completed_turns = self._completed_turns(ingress_rows, state_rows)
            turn_activity: dict[tuple[str, str], datetime] = {}
            for raw, field in ((row, "accepted_at_ms") for row in ingress_rows):
                tick_budget.check('connectors.py:metadata_or_work')
                stamp = _timestamp(raw.get(field))
                if stamp:
                    key = (_bounded_string(raw.get("session_id"), 256),
                           _bounded_string(raw.get("turn_id"), 256))
                    observed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                    turn_activity[key] = max(observed, turn_activity.get(key, observed))
            for raw in state_rows:
                tick_budget.check('connectors.py:metadata_or_work')
                stamp = _timestamp(raw.get("updated_at_ms"))
                if stamp:
                    key = (_bounded_string(raw.get("session_id"), 256),
                           _bounded_string(raw.get("turn_id"), 256))
                    observed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                    turn_activity[key] = max(observed, turn_activity.get(key, observed))
            message_rows = _select_sqlite_rows(
                connection,
                "local_runtime_message_rows",
                SQLITE_COLUMN_ALLOWLIST["minimax"]["local_runtime_message_rows"],
                limit=spec.max_records * 8,
                order_by="created_at_ms",
                value_limit=value_limit,
            )
            records_seen += len(session_rows) + len(ingress_rows) + len(state_rows) + len(message_rows)
            for order, raw in enumerate(message_rows):
                tick_budget.check('connectors.py:metadata_or_work')
                session_id = _bounded_string(raw.get("session_id"), 256)
                if not session_id:
                    continue
                session = sessions.get(session_id, {})
                data = _json_mapping(raw.get("data_json"))
                role = _bounded_string(
                    raw.get("role") or data.get("role"), 32
                ).lower()
                if role not in {"assistant", "user"}:
                    continue
                turn_id = _bounded_string(
                    raw.get("turn_id") or data.get("turn_id") or data.get("turnId"),
                    256,
                )
                msg_id = _bounded_string(
                    raw.get("msg_id") or data.get("msg_id") or data.get("msgId") or raw.get("id"),
                    512,
                )
                finish_reason = _bounded_string(data.get("finish_reason"), 64).lower()
                payload = {
                    "role": role,
                    # MiniMax's native final/user text key.  Reasoning and
                    # tool keys are deliberately omitted from this mapping.
                    "content": data.get("msg_content"),
                    "finish_reason": finish_reason,
                }
                completion = completed_turns.get(turn_id)
                row = _Row(
                    order=order,
                    outer_type=role,
                    payload=payload,
                    session_id=session_id,
                    native_id=msg_id,
                    cursor_raw=msg_id,
                    turn_id=turn_id,
                    role=role,
                    observed=raw.get("created_at_ms") or data.get("timestamp") or session.get("created_at_ms"),
                    completed=completion[1] if completion else None,
                    title=_bounded_string(session.get("title"), 200),
                    project=_bounded_string(session.get("project_id"), 256),
                    cwd=_bounded_string(
                        session.get("workspace_dir") or session.get("project_workspace_dir"),
                        1024,
                    ),
                    source_table="local_runtime_message_rows",
                )
                row.terminal = bool(
                    role == "assistant"
                    and completion
                    and completion[0] == session_id
                    and finish_reason == "stop"
                )
                rows.append(row)
            connection.close()
        except (OSError, sqlite3.Error, ValueError):
            return ConnectorCollection(
                issues=(_issue(spec, "error", "sqlite_read_error"),)
            )

        events: list[NormalizedEvent] = []
        terminal_turns = {(row.session_id, row.turn_id) for row in rows
                          if row.role == "assistant" and row.turn_id
                          and row.payload.get("finish_reason") == "stop"}
        incomplete_turns: dict[tuple[str, str], datetime] = {}
        for row in rows:
            tick_budget.check('connectors.py:metadata_or_work')
            if row.role != "assistant" or not row.turn_id:
                continue
            key = (row.session_id, row.turn_id)
            if key in terminal_turns or _session_excluded(spec, row.session_id):
                continue
            stamp = _timestamp(row.observed)
            if stamp:
                observed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                incomplete_turns[key] = max(observed, incomplete_turns.get(key, observed),
                                            turn_activity.get(key, observed))
        quiet_cutoff = datetime.now(timezone.utc).timestamp() - spec.quiet_minutes * 60
        for observed in incomplete_turns.values():
            tick_budget.check('connectors.py:metadata_or_work')
            if observed.timestamp() < quiet_cutoff:
                issues.append(_issue(spec, "info", "turn_not_completed"))
        for row in rows:
            tick_budget.check('connectors.py:metadata_or_work')
            if row.role != "assistant":
                continue
            if _session_excluded(spec, row.session_id):
                issues.append(_issue(spec, "skipped", "excluded_session"))
                continue
            if not row.terminal:
                continue
            linked = _linked_user(rows, row, spec.max_excerpt_bytes)
            answer = _row_text(row, ("content",))
            if not linked or not answer or not row.turn_id:
                issues.append(
                    _issue(
                        spec,
                        "skipped",
                        "missing_linked_user" if not linked else "missing_final_answer",
                    )
                )
                continue
            if _answer_needs_full_source(answer, spec.max_excerpt_bytes):
                issues.append(_issue(spec, "skipped", "answer_needs_full_source"))
                continue
            event = _make_event(
                spec,
                row,
                session_id=row.session_id,
                cursor=_safe_identifier(row.turn_id, "turn"),
                answer=answer,
                linked_user_cursor=linked,
                completion_kind="turn_ingress:completed;finish_reason:stop",
                table_or_stream="local_runtime_message_rows",
                title=row.title,
                project=row.project,
                cwd=row.cwd,
                completed_at=row.completed,
            )
            if event:
                events.append(event)
            else:
                issues.append(_issue(spec, "skipped", "unsafe_or_invalid_record"))
        if spec.include_user_messages:
            _extra_events, _extra_issues = _tabular_user_evidence(spec, rows)
            events.extend(_extra_events)
            issues.extend(_extra_issues)
        return ConnectorCollection(_deduplicate_events(events), tuple(issues), records_seen)


CONNECTOR_TYPES: dict[str, type[BaseConnector]] = {
    "codex": CodexConnector,
    "claude": ClaudeConnector,
    "qwen": QwenConnector,
    "grok": GrokConnector,
    "opencode": OpenCodeSQLiteConnector,
    "hermes": HermesSQLiteConnector,
    "minimax": MiniMaxSQLiteConnector,
    "files": FilesConnector,
}


def _mapping_source_identity(value: Any) -> tuple[str, str]:
    """Return (source_key, tool) from a mapping or spec without raising."""

    if isinstance(value, SourceSpec):
        tool = _bounded_string(value.tool, 32)
        key = _bounded_string(value.source_id, 256)
        return key, tool
    if isinstance(value, Mapping):
        key = _bounded_string(value.get("source_id") or value.get("sourceId") or value.get("id"), 256)
        tool = _bounded_string(value.get("tool"), 32)
        return key, tool
    return "", ""


def _mapping_kind(value: Any) -> str:
    if isinstance(value, SourceSpec):
        return str(value.kind or "auto").strip().lower()
    if isinstance(value, Mapping):
        return str(value.get("kind", "auto") or "auto").strip().lower()
    return "auto"


def _files_mapping_config_issue(value: Mapping[str, Any]) -> ConnectorIssue | None:
    """Validate kind=files mappings against SPEC-0.1.8-FILES.

    ``glob`` belongs to jsonl/sqlite sources and must never be required here.
    Missing files-specific required fields become a visible source issue
    instead of an exception that aborts the whole tick.
    """

    source_key, tool = _mapping_source_identity(value)
    source_key = source_key or "files"
    tool = tool or "files"
    # Explicitly ignore glob: it is not a files-source field.
    _ = value.get("glob")
    if "extensions" not in value or value.get("extensions") in (None, "", (), []):
        return ConnectorIssue(
            source_key,
            tool,
            "error",
            "missing_required_field:extensions",
        )
    raw_ext = value.get("extensions")
    if not isinstance(raw_ext, (list, tuple)):
        return ConnectorIssue(source_key, tool, "error", "invalid_files_config")
    if "roots" in value and not isinstance(value.get("roots"), (list, tuple, Mapping)):
        return ConnectorIssue(source_key, tool, "error", "invalid_files_config")
    return None


def _coerce_spec(value: SourceSpec | Mapping[str, Any]) -> SourceSpec:
    if isinstance(value, SourceSpec):
        return value.normalized()
    if not isinstance(value, Mapping):
        raise TypeError("sources must contain SourceSpec or mapping values")
    kind = str(value.get("kind", "auto") or "auto").strip().lower()
    # jsonl/sqlite control entries historically use ``glob``; files sources
    # use ``roots`` and must not KeyError when ``glob`` is absent.
    if kind == "files":
        path = value.get("path", "") or ""
    else:
        path = value.get("path", "") or value.get("glob", "") or ""
    return SourceSpec(
        tool=str(value.get("tool", "")),
        path=path,
        kind=kind,
        source_id=value.get("source_id") or value.get("sourceId") or value.get("id"),
        native_session_id=value.get("native_session_id") or value.get("nativeSessionId"),
        exclude_native_session_ids=tuple(
            value.get("exclude_native_session_ids", value.get("excludeNativeSessionIds", ())) or ()
        ),
        exclude_caretaker_sessions=bool(
            value.get("exclude_caretaker_sessions", value.get("excludeCaretakerSessions", True))
        ),
        include_user_messages=bool(value.get("include_user_messages", value.get("includeUserMessages", False))),
        max_records=int(value.get("max_records", value.get("maxRecords", DEFAULT_MAX_RECORDS))),
        max_bytes=int(value.get("max_bytes", value.get("maxBytes", DEFAULT_MAX_BYTES))),
        max_excerpt_bytes=int(value.get("max_excerpt_bytes", value.get("maxExcerptBytes", DEFAULT_MAX_EXCERPT_BYTES))),
        roots=tuple(value.get("roots") or ()),
        extensions=tuple(value.get("extensions") or ()),
        quiet_minutes=int(value.get("quiet_minutes", value.get("quietMinutes", DEFAULT_QUIET_MINUTES))),
        max_file_bytes=int(value.get("max_file_bytes", value.get("maxFileBytes", DEFAULT_MAX_FILE_BYTES))),
        max_files_per_tick=int(value.get("max_files_per_tick", value.get("maxFilesPerTick", DEFAULT_MAX_FILES_PER_TICK))),
        state_dir=str(value.get("state_dir", value.get("stateDir", "")) or ""),
        file_observation_mode=str(value.get("file_observation_mode", "mtime")),
        exclude_directory_names=tuple(value.get("exclude_directory_names") or ()),
    ).normalized()


def _stable_source_exception_reason(exc: BaseException) -> str:
    """Map an unexpected per-source failure to a stable, non-secret reason code."""

    name = type(exc).__name__.strip()
    if name == "KeyError":
        return "source_config_key_error"
    lowered = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return f"source_runtime_error:{lowered or 'exception'}"


def _collect_one_source(spec: SourceSpec) -> ConnectorCollection:
    if spec.kind == "files" or spec.tool == "files":
        return FilesConnector().collect(spec)
    if spec.kind == "fleet_receipt":
        return ConnectorCollection(issues=(_issue(spec, "unsupported", "fleet_receipt_not_collected_here"),))
    connector_type = CONNECTOR_TYPES.get(spec.tool)
    if connector_type is None:
        return ConnectorCollection(issues=(_issue(spec, "unsupported", "unverified_source"),))
    if _source_excluded(spec):
        return ConnectorCollection(issues=(_issue(spec, "skipped", "excluded_source"),))
    if spec.kind == "sqlite" and spec.tool not in {"opencode", "hermes", "minimax"}:
        return ConnectorCollection(issues=(_issue(spec, "unsupported", "source_kind_not_supported"),))
    if spec.kind == "jsonl" and spec.tool in {"opencode", "hermes", "minimax"}:
        return ConnectorCollection(issues=(_issue(spec, "unsupported", "source_kind_not_supported"),))
    return connector_type().collect(spec)


def collect_sources(
    sources: Iterable[SourceSpec | Mapping[str, Any]],
) -> ConnectorCollection:
    """Collect bounded events from explicit sources without writing anything.

    A configuration error or unexpected exception in one source is reported
    against that source and does not abort remaining sources in the same tick.
    """

    events: list[NormalizedEvent] = []
    issues: list[ConnectorIssue] = []
    records_seen = 0
    for value in sources:
        tick_budget.check('connectors.py:metadata_or_work')
        source_key, tool = _mapping_source_identity(value)
        try:
            if isinstance(value, Mapping) and _mapping_kind(value) == "files":
                config_issue = _files_mapping_config_issue(value)
                if config_issue is not None:
                    issues.append(config_issue)
                    continue
            spec = _coerce_spec(value)
            collection = _collect_one_source(spec)
        except (TypeError, ValueError):
            issues.append(
                ConnectorIssue(source_key or "invalid-source", tool, "error", "invalid_source")
            )
            continue
        except tick_budget.TickBudgetExceeded:
            raise
        except Exception as exc:
            issues.append(
                ConnectorIssue(
                    source_key or "invalid-source",
                    tool,
                    "error",
                    _stable_source_exception_reason(exc),
                )
            )
            continue
        events.extend(collection.events)
        issues.extend(collection.issues)
        records_seen += collection.records_seen
    return ConnectorCollection(_deduplicate_events(events), tuple(issues), records_seen)


def discover_source(source: SourceSpec | Mapping[str, Any]) -> SourceDiscovery:
    """Inspect keys/tables only; never read or return conversation text."""

    try:
        spec = _coerce_spec(source)
    except (TypeError, ValueError):
        return SourceDiscovery("", "unknown", "", "error", reason="invalid_source")
    path = str(spec.path)
    if spec.kind == "files" or spec.tool == "files":
        roots = spec.roots
        if not roots and path:
            roots = ({"path": path, "project_slug": "", "max_depth": 4},)
        if not roots:
            return SourceDiscovery(
                spec.tool or "files",
                "files",
                path,
                "supported",
                schema_keys=("relative_path", "sha256", "mtime", "source_id"),
                reason="empty_allowlist",
            )
        existing = False
        missing = False
        for root in roots:
            tick_budget.check('connectors.py:metadata_or_work')
            root_path = Path(str(root.get("path", "") if isinstance(root, Mapping) else root))
            if root_path.is_dir():
                existing = True
            else:
                missing = True
        status = "supported" if existing else "unavailable"
        reason = None if existing else "source_missing"
        if existing and missing:
            reason = "partial_roots"
        return SourceDiscovery(
            spec.tool or "files",
            "files",
            path or str(roots[0].get("path") if isinstance(roots[0], Mapping) else roots[0]),
            status,
            schema_keys=("relative_path", "sha256", "mtime", "source_id", "project"),
            reason=reason,
        )
    if spec.kind == "fleet_receipt":
        if not Path(path).is_file():
            return SourceDiscovery(spec.tool or "fleet", "fleet_receipt", path, "unavailable", reason="source_missing")
        return SourceDiscovery(spec.tool or "fleet", "fleet_receipt", path, "supported")
    if spec.tool not in CONNECTOR_TYPES:
        return SourceDiscovery(
            spec.tool,
            spec.kind,
            path,
            "unsupported",
            reason="unverified_source",
        )
    if not Path(path).is_file():
        return SourceDiscovery(spec.tool, spec.kind, path, "unavailable", reason="source_missing")
    if spec.kind == "jsonl":
        keys: set[str] = set()
        markers: set[str] = set()
        count = 0
        try:
            with Path(path).open("rb") as handle:
                while count < min(spec.max_records, 64):
                    tick_budget.check('connectors.py:metadata_or_work')
                    raw = handle.readline(min(MAX_LINE_BYTES, spec.max_bytes) + 1)
                    if not raw:
                        break
                    if len(raw) > min(MAX_LINE_BYTES, spec.max_bytes):
                        continue
                    try:
                        value = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if not isinstance(value, Mapping):
                        continue
                    count += 1
                    keys.update(str(key) for key in value.keys())
                    for key in MARKER_KEYS:
                        tick_budget.check('connectors.py:metadata_or_work')
                        if key in value:
                            markers.add(key)
                    for container_name in ("payload", "message", "item", "time"):
                        tick_budget.check('connectors.py:metadata_or_work')
                        nested = value.get(container_name)
                        if isinstance(nested, Mapping):
                            keys.update(f"{container_name}.{key}" for key in nested.keys())
                            for key in MARKER_KEYS:
                                tick_budget.check('connectors.py:metadata_or_work')
                                if key in nested:
                                    markers.add(f"{container_name}.{key}")
        except (OSError, UnicodeError):
            return SourceDiscovery(spec.tool, spec.kind, path, "error", reason="source_read_error")
        return SourceDiscovery(
            spec.tool,
            spec.kind,
            path,
            "supported",
            schema_keys=tuple(sorted(keys)),
            terminal_markers=tuple(sorted(markers)),
        )
    # SQLite discovery intentionally stops at sqlite_master/PRAGMA metadata.
    try:
        connection = _sqlite_connection(path)
        tables = _table_names(connection)
        keys: set[str] = set()
        allowed_tables = SQLITE_TABLES.get(spec.tool, frozenset())
        for table in tables:
            tick_budget.check('connectors.py:metadata_or_work')
            if table not in allowed_tables:
                continue
            for column in _table_columns(connection, table):
                tick_budget.check('connectors.py:metadata_or_work')
                keys.add(f"{table}.{column}")
        connection.close()
        if spec.tool == "opencode":
            required = {"session"}
        elif spec.tool == "hermes":
            required = {"sessions", "messages"}
        else:
            required = {
                "local_runtime_sessions",
                "local_runtime_message_rows",
                "local_runtime_turn_ingress",
            }
        status = "supported" if required.issubset(set(tables)) else "error"
        reason = None if status == "supported" else "unsupported_sqlite_schema"
        return SourceDiscovery(
            spec.tool,
            spec.kind,
            path,
            status,
            schema_keys=tuple(sorted(keys)),
            tables=tuple(sorted(table for table in tables if table in allowed_tables)),
            terminal_markers=tuple(
                sorted(
                    key
                    for key in MARKER_KEYS
                    if any(key.lower().replace("_", "") in column.lower().replace("_", "") for column in keys)
                )
            ),
            reason=reason,
        )
    except (OSError, sqlite3.Error, ValueError):
        return SourceDiscovery(spec.tool, spec.kind, path, "error", reason="sqlite_read_error")


def discover_sources(
    sources: Iterable[SourceSpec | Mapping[str, Any]],
) -> tuple[SourceDiscovery, ...]:
    return tuple(discover_source(source) for source in sources)


def support_matrix() -> tuple[dict[str, str], ...]:
    """Return the honest connector coverage used by reports and dry-runs."""

    return (
        {"tool": "codex", "kind": "jsonl", "status": "supported", "note": "nested task_complete + linked user; user_message.v1 opt-in via include_user_messages"},
        {"tool": "claude", "kind": "jsonl", "status": "supported", "note": "terminal assistant record + linked user; user_message.v1 opt-in via include_user_messages"},
        {"tool": "qwen", "kind": "jsonl", "status": "conditional", "note": "fixture adapter; explicit terminal marker required"},
        {"tool": "grok", "kind": "jsonl", "status": "conditional", "note": "fixture adapter; explicit terminal marker required"},
        {"tool": "opencode", "kind": "sqlite", "status": "conditional", "note": "fixture terminal event; read-only session/message/part/event allowlist"},
        {"tool": "hermes", "kind": "sqlite", "status": "conditional", "note": "fixture terminal message; ended session required"},
        {"tool": "minimax", "kind": "sqlite", "status": "supported", "note": "local_runtime completed turn + msg_content + finish_reason=stop"},
        {"tool": "openclaw", "kind": "delegated", "status": "unsupported", "note": "use existing caretaker.ingest.py"},
        {"tool": "kimi", "kind": "unknown", "status": "unsupported", "note": "terminal export schema unverified"},
        {"tool": "zcode", "kind": "unknown", "status": "unsupported", "note": "no confirmed CLI or turn store"},
        {"tool": "gemini", "kind": "unknown", "status": "unsupported", "note": "no confirmed local source"},
        {"tool": "antigravity", "kind": "sqlite", "status": "unsupported", "note": "steps payload is unverified raw trajectory"},
    )


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, str(path))
        temporary_name = None
    finally:
        # The temporary file is created by this call in the task-owned output
        # directory.  Remove only that exact temporary path on failed replace.
        if temporary_name:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass


def _existing_events(path: Path, max_records: int) -> dict[str, NormalizedEvent]:
    if not path.is_file():
        return {}
    result: dict[str, NormalizedEvent] = {}
    try:
        with path.open("rb") as handle:
            for _ in range(max_records):
                tick_budget.check('connectors.py:metadata_or_work')
                raw = handle.readline(min(MAX_LINE_BYTES, DEFAULT_MAX_BYTES) + 1)
                if not raw:
                    break
                try:
                    value = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(value, Mapping):
                    continue
                event = NormalizedEvent.from_mapping(value)
                if event:
                    result[event.event_id] = event
    except (OSError, UnicodeError):
        return {}
    return result


def _existing_cursors(path: Path, max_sources: int) -> dict[str, dict[str, str]]:
    """Load only scalar cursor metadata from a previous atomic checkpoint."""

    if not path.is_file():
        return {}
    try:
        # Cursor files are generated by this module and therefore remain small;
        # the size guard keeps a corrupted/replaced file from becoming an
        # unbounded read.
        if path.stat().st_size > max(1, max_sources * 512):
            return {}
        value = json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    source_values = value.get("sources") if isinstance(value, Mapping) else None
    if not isinstance(source_values, Mapping):
        return {}
    result: dict[str, dict[str, str]] = {}
    for raw_key, raw_cursor in list(source_values.items())[:max_sources]:
        tick_budget.check('connectors.py:metadata_or_work')
        key = _bounded_string(raw_key, 1024)
        if not key or not isinstance(raw_cursor, Mapping):
            continue
        cursor = _bounded_string(raw_cursor.get("cursor"), 128)
        event_id = _bounded_string(raw_cursor.get("event_id"), 128)
        if not cursor or not event_id:
            continue
        result[key] = {
            "tool": _bounded_string(raw_cursor.get("tool"), 32),
            "cursor": cursor,
            "event_id": event_id,
            "observed_at": _bounded_string(raw_cursor.get("observed_at"), 64),
        }
    return result


def export_normalized_events(
    sources: Iterable[SourceSpec | Mapping[str, Any]],
    output_path: str | os.PathLike[str],
    *,
    manifest_path: str | os.PathLike[str] | None = None,
    cursor_path: str | os.PathLike[str] | None = None,
    max_records: int = DEFAULT_MAX_RECORDS * 16,
) -> ExportResult:
    """Idempotently write normalized JSONL plus atomic manifest/cursors.

    ``output_path`` and the optional sidecar paths must point to a directory
    owned by the caller for this run.  Existing generated events are merged by
    deterministic ``event_id`` so a restart never appends duplicates.
    """

    source_values = tuple(sources)
    output = Path(str(output_path))
    if output.exists() and output.is_dir():
        output = output / "normalized_events.jsonl"
    manifest = Path(str(manifest_path)) if manifest_path else output.with_suffix(".manifest.json")
    cursors = Path(str(cursor_path)) if cursor_path else output.with_suffix(".cursors.json")
    if max_records <= 0:
        raise ValueError("max_records must be positive")
    collected = collect_sources(source_values)
    existing = _existing_events(output, max_records * 4)
    before_ids = set(existing)
    for event in collected.events:
        tick_budget.check('connectors.py:metadata_or_work')
        existing[event.event_id] = event
    all_events = tuple(
        sorted(
            existing.values(),
            key=lambda item: (item.tool, item.native_session_id, item.cursor, item.event_id),
        )
    )
    if len(all_events) > max_records:
        # Keep the export bounded and deterministic.  The next run can recover
        # omitted source records because source cursors are not advanced by
        # this module; callers can raise the bound when they need a larger batch.
        all_events = all_events[:max_records]
    output_text = "".join(
        json.dumps(event_to_dict(event), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for event in all_events
    )
    _atomic_write(output, output_text)

    source_summaries: list[dict[str, Any]] = []
    for spec_value in source_values:
        tick_budget.check('connectors.py:metadata_or_work')
        try:
            spec = _coerce_spec(spec_value)
        except (TypeError, ValueError):
            continue
        source_events = [
            event for event in collected.events if _event_matches_source(event, spec)
        ]
        source_summaries.append(
            {
                "source_key": _source_key(spec),
                "tool": spec.tool,
                "kind": spec.kind,
                "records_emitted": len(source_events),
            }
        )
    issue_dicts = sorted(
        {json.dumps(issue.to_dict(), sort_keys=True): issue.to_dict() for issue in collected.issues}.values(),
        key=lambda item: (item.get("source_key", ""), item.get("reason", ""), item.get("line_no", 0)),
    )
    new_records = sum(1 for event in all_events if event.event_id not in before_ids)
    manifest_value = {
        "schema_version": SCHEMA_VERSION,
        "event_schema": NORMALIZED_EVENT_TYPE,
        "output": output.name,
        "record_count": len(all_events),
        # The sidecar is a deterministic checkpoint.  Per-call delta counts
        # belong to ExportResult; embedding them here would make a retry mutate
        # an otherwise identical manifest.
        "collection_records": len(collected.events),
        "records_seen": collected.records_seen,
        "sources": sorted(source_summaries, key=lambda item: item["source_key"]),
        "issues": issue_dicts,
    }
    _atomic_write(manifest, json.dumps(manifest_value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")

    cursor_values = _existing_cursors(cursors, max_records * 4)
    for event in all_events:
        tick_budget.check('connectors.py:metadata_or_work')
        source_ref = event.source_ref.get("path_or_store", "")
        source_matches = []
        for spec_value in source_values:
            tick_budget.check('connectors.py:metadata_or_work')
            try:
                spec = _coerce_spec(spec_value)
            except (TypeError, ValueError):
                continue
            if _event_matches_source(event, spec) or str(spec.path) == source_ref:
                source_matches.append(spec)
        for spec in source_matches:
            tick_budget.check('connectors.py:metadata_or_work')
            key = _source_key(spec)
            previous = cursor_values.get(key)
            candidate = {"tool": event.tool, "cursor": event.cursor, "event_id": event.event_id}
            if previous is None or (event.observed_at, event.event_id) > (previous.get("observed_at", ""), previous["event_id"]):
                candidate["observed_at"] = event.observed_at
                cursor_values[key] = candidate
    cursor_value = {"schema_version": SCHEMA_VERSION, "sources": dict(sorted(cursor_values.items()))}
    _atomic_write(cursors, json.dumps(cursor_value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    return ExportResult(
        output_path=str(output),
        manifest_path=str(manifest),
        cursor_path=str(cursors),
        events=all_events,
        issues=collected.issues,
        new_records=new_records,
        total_records=len(all_events),
    )


# Short aliases make the boundary convenient for a future pipeline adapter
# without coupling this module to pipeline.py today.
export_events = export_normalized_events
collect_events = collect_sources


__all__ = [
    "SCHEMA_VERSION",
    "CONNECTOR_SCAN_SCHEMA_VERSION",
    "NORMALIZED_EVENT_TYPE",
    "SUPPORTED_TOOLS",
    "UNVERIFIED_SOURCES",
    "SourceSpec",
    "NormalizedEvent",
    "ConnectorIssue",
    "ConnectorCollection",
    "SourceDiscovery",
    "ExportResult",
    "CodexConnector",
    "ClaudeConnector",
    "QwenConnector",
    "GrokConnector",
    "OpenCodeSQLiteConnector",
    "HermesSQLiteConnector",
    "MiniMaxSQLiteConnector",
    "collect_sources",
    "collect_events",
    "discover_source",
    "discover_sources",
    "support_matrix",
    "USER_MESSAGE_EVENT_TYPE",
    "USER_SCHEMA_VERSION",
    "USER_PROVENANCE",
    "event_to_dict",
    "export_normalized_events",
    "export_events",
]
