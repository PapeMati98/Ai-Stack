"""Redaction and filtering.

Drops restricted payloads, secret-shaped text, raw reasoning dumps, and raw
tool dumps. Computes a stable content hash for downstream dedup. The filter
is intentionally conservative: when in doubt it rejects the record.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple


# Conservative patterns. Names are intentionally narrow: bearer tokens,
# OpenAI/Anthropic/Muse keys, PEM blocks, and obvious password assignments.
SECRET_PATTERNS: Tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"muse-[A-Za-z0-9_\-]{20,}", re.IGNORECASE),
    re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----"),
    re.compile(r"(?i)\bpassword\s*[:=]\s*[^\s]{4,}"),
    re.compile(r"(?i)\bapi[_-]?key\s*[:=]\s*[^\s]{8,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"(?i)\bsecret\s*[:=]\s*[^\s]{6,}"),
)

# Reasoning/tool raw markers. Matched in body text and at JSON field level.
REASONING_MARKERS = (
    "chain_of_thought",
    "reasoning_trace",
    "reasoning trace",
    "thinking_trace",
    "scratchpad",
    "internal_monologue",
    "internal monologue",
)

INJECTED_CONTENT_MARKERS = (
    "agents.md",
    "injected context",
    "<injected",
    "</environment>",
    "<environment>",
)

INJECTED_CONTENT_PREFIXES = (
    "system:",
    "developer:",
    "skills:",
    "agents:",
    "agents.md",
)

TOOL_RAW_FIELDS = (
    "tool_calls",
    "tool_results",
    "raw_payload",
    "raw_arguments",
    "tool_input",
    "tool_output",
    "arguments",
)

# Publication text may discuss tool fields in prose. Only field-shaped content
# is a dump there; source intake keeps its broader substring check below.
_TOOL_FIELD_NAMES = "|".join(re.escape(field) for field in TOOL_RAW_FIELDS)
_TOOL_DUMP_STRUCTURE = re.compile(
    rf"(?:[\"'](?:{_TOOL_FIELD_NAMES})[\"']\s*:"
    rf"|(?:^|[{{,])[ \t]*(?:{_TOOL_FIELD_NAMES})[ \t]*[:=]"
    rf"|<\s*/?\s*(?:{_TOOL_FIELD_NAMES})(?=[\s/>])[^>]*>)",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass(frozen=True)
class FilterOutcome:
    record_index: int
    accepted: bool
    reason: str
    content_hash: Optional[str] = None
    excerpt: Optional[str] = None


def contains_secret(text: str) -> bool:
    if not text:
        return False
    return any(p.search(text) for p in SECRET_PATTERNS)


def has_reasoning_marker(body: str) -> bool:
    if not body:
        return False
    low = body.lower()
    return any(marker in low for marker in REASONING_MARKERS)


def looks_like_raw_tool_dump(record_body: str) -> bool:
    if not record_body:
        return False
    low = record_body.lower()
    return any(field in low for field in TOOL_RAW_FIELDS)


def looks_like_tool_dump_structure(text: str) -> bool:
    """Detect field-shaped tool dumps in publication bodies, not prose mentions."""
    return bool(text and _TOOL_DUMP_STRUCTURE.search(text))


def content_hash(*parts: str) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


def redact_excerpt(body: str, max_bytes: int, *, preserve_whitespace: bool = False) -> str:
    """Bound text and drop dump-like lines, optionally retaining edge spaces."""
    if not body:
        return ""
    if len(body.encode("utf-8")) <= max_bytes:
        text = body
    else:
        text = body.encode("utf-8")[:max_bytes].decode("utf-8",
                                                       errors="replace")
    # Drop lines that look like reasoning/tool traces; we never want those
    # propagated into excerpts.
    kept: List[str] = []
    for line in text.splitlines():
        low = line.lower()
        if any(marker in low for marker in REASONING_MARKERS):
            continue
        if any(field in low for field in TOOL_RAW_FIELDS):
            continue
        if any(marker in low for marker in INJECTED_CONTENT_MARKERS):
            continue
        if low.strip().startswith(INJECTED_CONTENT_PREFIXES):
            continue
        kept.append(line)
    out = "\n".join(kept)
    if not preserve_whitespace:
        out = out.strip()
    if len(out.encode("utf-8")) > max_bytes:
        out = out.encode("utf-8")[:max_bytes].decode("utf-8", errors="replace")
    return out


def filter_records(records: Iterable, *, allowlist_tools: Tuple[str, ...],
                   max_body_bytes: int, max_excerpt_bytes: int
                   ) -> List[FilterOutcome]:
    """Filter ``records`` and produce one ``FilterOutcome`` per input.

    The index in the outcome matches the index in ``records`` enumeration.
    """
    out: List[FilterOutcome] = []
    for idx, rec in enumerate(records):
        if rec.identity_tool not in allowlist_tools:
            out.append(FilterOutcome(idx, False, "tool_not_allowlisted"))
            continue
        if rec.classification == "restricted":
            out.append(FilterOutcome(idx, False, "restricted_payload"))
            continue
        scan_target = f"{rec.title}\n{rec.body}\n{rec.identity_tool}"
        if contains_secret(scan_target):
            out.append(FilterOutcome(idx, False, "secret_detected"))
            continue
        if has_reasoning_marker(rec.body):
            out.append(FilterOutcome(idx, False, "reasoning_dump"))
            continue
        if looks_like_raw_tool_dump(rec.body):
            out.append(FilterOutcome(idx, False, "tool_raw_dump"))
            continue
        body_bytes = len(rec.body.encode("utf-8"))
        if body_bytes > max_body_bytes:
            out.append(FilterOutcome(idx, False, "body_too_large",
                                     content_hash=None))
            continue
        excerpt = redact_excerpt(
            rec.body,
            max_excerpt_bytes,
            preserve_whitespace=getattr(rec, "speaker", "") == "user",
        )
        if not excerpt or not excerpt.strip():
            out.append(FilterOutcome(idx, False, "empty_after_redact"))
            continue
        digest = content_hash(rec.identity_tool, rec.native_session_id,
                              rec.cursor, rec.title, excerpt)
        out.append(FilterOutcome(idx, True, "accepted",
                                 content_hash=digest, excerpt=excerpt))
    return out
