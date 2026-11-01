#!/usr/bin/env python3
"""Registry v2 (I1) page-frontmatter lint.

Stdlib-only standalone helper for the knowledge-gateway integrator. Validates one
page's frontmatter against an I1 registry (path or already-parsed object) and
returns structured issues. Does not import knowledge.py, does not mutate the
registry, and does not invent identifiers.

Parent hook:
    from registry_v2_lint import validate_page
    issues = validate_page(page_content, registry, stage="F1")
    # issue["severity"] is "error" or "warning"; map into lint() counters.
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

ALLOWED_TYPES = frozenset({
    "system", "person", "organization", "project", "product",
    "concept", "process", "decision", "event", "source-summary", "synthesis", "evidence-ledger",
})

# Frontmatter list field -> I1 registry collection it must resolve against.
REGISTRY_LIST_FIELDS = {
    "org": "organizations",
    "topics": "topics",
    "people": "people",
}

ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
STAGES = frozenset({"F1", "F2"})
MAX_REGISTRY_BYTES = 4 * 1024 * 1024

_NUMBER_PATTERN = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?\Z")
_FRONTMATTER_PATTERN = re.compile(
    r"\A(?:\ufeff)?---[ \t]*\r?\n(?P<body>.*?)(?:\r?\n)---[ \t]*(?:\r?\n|\Z)",
    re.DOTALL,
)
# YAML block sequences accept any indent greater than the parent key, including a
# single space or tab, and may sit at the parent key's own indent (column 0).
_BLOCK_ITEM_PATTERN = re.compile(r"^[ \t]*-(?:[ \t]+(?P<value>.*))?\Z")
_CONTINUATION_PATTERN = re.compile(r"^(?:[ \t]|-(?:[ \t]|$))")
_NULL_WORDS = frozenset({"null", "Null", "NULL", "~"})
_BOOL_WORDS = frozenset({"true", "True", "TRUE", "false", "False", "FALSE"})


def _is_blank_or_comment(line: str) -> bool:
    stripped = line.strip()
    return stripped == "" or stripped.startswith("#")


def _issue(code: str, message: str, field: str | None, *, semantic: bool, stage: str) -> dict:
    # Semantic findings stay warnings in F1 and only become errors in F2. The
    # caller passes the stage; nothing here promotes it.
    severity = ("warning" if stage == "F1" else "error") if semantic else "error"
    return {"severity": severity, "code": code, "field": field, "message": message}


def _short(value: object, limit: int = 80) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def frontmatter_body(content: str) -> str | None:
    match = _FRONTMATTER_PATTERN.match(content)
    return match.group("body") if match else None


def _strip_comment(value: str) -> str:
    """Drop a trailing YAML comment without touching '#' inside quotes."""
    index = 0
    length = len(value)
    while index < length:
        char = value[index]
        if char == "'":
            cursor = index + 1
            while cursor < length:
                if value[cursor] == "'":
                    if cursor + 1 < length and value[cursor + 1] == "'":
                        cursor += 2
                        continue
                    break
                cursor += 1
            index = cursor + 1
            continue
        if char == '"':
            cursor = index + 1
            while cursor < length:
                if value[cursor] == "\\":
                    cursor += 2
                    continue
                if value[cursor] == '"':
                    break
                cursor += 1
            index = cursor + 1
            continue
        if char == "#" and (index == 0 or value[index - 1] in " \t"):
            return value[:index].rstrip()
        index += 1
    return value.rstrip()


def _decode_scalar(raw: str) -> tuple[bool, str | None]:
    text = raw.strip()
    if text == "" or text in _NULL_WORDS or text in _BOOL_WORDS:
        return False, None
    if len(text) >= 2 and text[0] == text[-1] == "'":
        return True, text[1:-1].replace("''", "'")
    if len(text) >= 2 and text[0] == text[-1] == '"':
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            return True, text[1:-1]
        return (True, decoded) if isinstance(decoded, str) else (False, None)
    if _NUMBER_PATTERN.fullmatch(text) or text[0] in "[{":
        return False, None
    return True, text


def _locate_field(block: str, name: str) -> tuple[str, list[str]] | None:
    match = re.search(
        rf"^{re.escape(name)}[ \t]*:[ \t]*(?P<inline>[^\r\n]*)\r?$",
        block,
        re.MULTILINE,
    )
    if match is None:
        return None
    following: list[str] = []
    for line in block[match.end():].splitlines():
        if _is_blank_or_comment(line):
            # Blank and comment-only lines are never content and never end a block.
            continue
        if _CONTINUATION_PATTERN.match(line):
            following.append(line)
            continue
        break
    return match.group("inline"), following


def _split_flow_items(inner: str) -> list[str]:
    items: list[str] = []
    buffer: list[str] = []
    index = 0
    length = len(inner)
    while index < length:
        char = inner[index]
        if char == ",":
            items.append("".join(buffer))
            buffer = []
            index += 1
            continue
        if char == "'":
            cursor = index + 1
            while cursor < length:
                if inner[cursor] == "'":
                    if cursor + 1 < length and inner[cursor + 1] == "'":
                        cursor += 2
                        continue
                    break
                cursor += 1
            buffer.append(inner[index : cursor + 1])
            index = cursor + 1
            continue
        if char == '"':
            cursor = index + 1
            while cursor < length:
                if inner[cursor] == "\\":
                    cursor += 2
                    continue
                if inner[cursor] == '"':
                    break
                cursor += 1
            buffer.append(inner[index : cursor + 1])
            index = cursor + 1
            continue
        buffer.append(char)
        index += 1
    items.append("".join(buffer))
    return items


def _json_element(value: object) -> tuple[bool, str | None]:
    return (True, value) if isinstance(value, str) else (False, None)


def _parse_flow(inner: str) -> tuple[str, list[tuple[bool, str | None]]]:
    if inner.strip() == "":
        return "empty", []
    elements: list[tuple[bool, str | None]] = []
    for part in _split_flow_items(inner):
        elements.append(_decode_scalar(part))
    return "list", elements


def _parse_inline_value(text: str) -> tuple[str, list[tuple[bool, str | None]]]:
    if text == "[]":
        return "empty", []
    if not text.startswith("["):
        return "malformed", []
    if not text.endswith("]"):
        return "malformed", []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
        json_ok = False
    else:
        json_ok = True
    if json_ok:
        if not isinstance(data, list):
            return "malformed", []
        return "list", [_json_element(item) for item in data]
    return _parse_flow(text[1:-1])


def _parse_block_items(following: list[str]) -> tuple[str, list[tuple[bool, str | None]]]:
    elements: list[tuple[bool, str | None]] = []
    saw_non_item = False
    for line in following:
        if _is_blank_or_comment(line):
            continue
        match = _BLOCK_ITEM_PATTERN.match(line.rstrip("\r"))
        if match is None:
            saw_non_item = True
            continue
        raw = match.group("value")
        elements.append((False, None) if raw is None else _decode_scalar(_strip_comment(raw)))
    if saw_non_item:
        # Keep already-parsed siblings so their IDs are still checked.
        return "malformed", elements
    if not elements:
        return "empty", []
    return "list", elements


def _field_list(block: str, name: str) -> tuple[str, list[tuple[bool, str | None]]] | None:
    """Return None when absent, else (status, elements) with status empty|list|malformed."""
    located = _locate_field(block, name)
    if located is None:
        return None
    inline, following = located
    text = _strip_comment(inline).strip()
    if text:
        status, elements = _parse_inline_value(text)
        if following:
            # Inline value plus block junk: report malformed but keep inline IDs.
            return "malformed", elements
        return status, elements
    return _parse_block_items(following)


def _field_scalar(block: str, name: str) -> tuple[str, str | None] | None:
    """Return None when absent, else (status, value) with status empty|scalar|malformed."""
    located = _locate_field(block, name)
    if located is None:
        return None
    inline, following = located
    text = _strip_comment(inline).strip()
    if text:
        if following or text.startswith("[") or text.startswith("{"):
            return "malformed", None
        is_string, value = _decode_scalar(text)
        return ("scalar", value) if is_string else ("malformed", None)
    if any(line.strip() for line in following):
        return "malformed", None
    return "empty", None


def _load_registry(registry: object) -> tuple[dict | None, str]:
    """Return (parsed, status) with status ok|missing|unreadable|malformed."""
    if registry is None:
        return None, "missing"
    if isinstance(registry, dict):
        return registry, "ok"
    if isinstance(registry, (str, Path)):
        return _read_registry_path(Path(registry))
    return None, "malformed"


def _read_registry_path(path: Path) -> tuple[dict | None, str]:
    try:
        info = path.lstat()
    except OSError:
        return None, "missing"
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return None, "unreadable"
    if info.st_size > MAX_REGISTRY_BYTES:
        return None, "unreadable"
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None, "unreadable"
    try:
        chunk = os.read(descriptor, MAX_REGISTRY_BYTES + 1)
        if len(chunk) > MAX_REGISTRY_BYTES:
            return None, "unreadable"
        payload = json.loads(chunk.decode("utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None, "malformed"
    finally:
        os.close(descriptor)
    if not isinstance(payload, dict):
        return None, "malformed"
    return payload, "ok"


def _collection_ids(parsed: dict) -> dict[str, set[str]] | None:
    ids: dict[str, set[str]] = {}
    for key in ("organizations", "topics", "people"):
        rows = parsed.get(key)
        if not isinstance(rows, list):
            return None
        bucket: set[str] = set()
        for row in rows:
            if isinstance(row, dict) and isinstance(row.get("id"), str):
                bucket.add(row["id"])
        ids[key] = bucket
    projects = parsed.get("projects")
    ids["projects"] = set(projects) if isinstance(projects, dict) else set()
    return ids


def validate_page(content: str, registry: object, stage: str = "F1") -> list[dict]:
    """Validate page frontmatter against registry v2 (I1).

    ``registry`` may be a filesystem path or a parent-trusted parsed object.
    Returns a list of issues: {"severity", "code", "field", "message"}.
    Structural problems are errors in both stages. Unknown or wrong-category
    IDs and a missing/unreadable registry are F1 warnings and F2 errors.
    """
    if stage not in STAGES:
        raise ValueError(f"stage must be one of {sorted(STAGES)}")
    issues: list[dict] = []
    block = frontmatter_body(content)
    if block is None:
        return issues

    type_field = _field_scalar(block, "type")
    if type_field is not None:
        status, value = type_field
        if status != "scalar":
            issues.append(_issue(
                "malformed-type", "frontmatter 'type' must be a non-empty string",
                "type", semantic=False, stage=stage,
            ))
        elif value not in ALLOWED_TYPES:
            issues.append(_issue(
                "invalid-type", f"frontmatter type {_short(value)!r} is not an allowed page type",
                "type", semantic=False, stage=stage,
            ))

    parsed, registry_status = _load_registry(registry)
    collections = _collection_ids(parsed) if registry_status == "ok" and parsed is not None else None
    if registry_status == "ok" and collections is None:
        registry_status = "malformed"
    known_everywhere: set[str] = set()
    if collections is not None:
        for bucket in collections.values():
            known_everywhere |= bucket

    needs_lookup = False
    for field, collection in REGISTRY_LIST_FIELDS.items():
        parsed_list = _field_list(block, field)
        if parsed_list is None:
            continue
        status, elements = parsed_list
        if status == "malformed":
            issues.append(_issue(
                "malformed-list",
                f"frontmatter '{field}' must be a YAML block list or JSON inline array",
                field, semantic=False, stage=stage,
            ))
        for is_string, value in elements:
            if not is_string:
                issues.append(_issue(
                    "non-string-element",
                    f"frontmatter '{field}' list elements must be strings, found {_short(value)!r}",
                    field, semantic=False, stage=stage,
                ))
                continue
            assert value is not None
            if not ID_PATTERN.fullmatch(value):
                issues.append(_issue(
                    "bad-id-syntax",
                    f"frontmatter '{field}' value {_short(value)!r} is not a canonical slug ID",
                    field, semantic=False, stage=stage,
                ))
                continue
            needs_lookup = True
            if collections is None:
                continue
            if value in collections[collection]:
                continue
            if value in known_everywhere:
                issues.append(_issue(
                    "wrong-category",
                    f"ID {_short(value)!r} exists in the registry but not in '{collection}'",
                    field, semantic=True, stage=stage,
                ))
            else:
                issues.append(_issue(
                    "unknown-id",
                    f"ID {_short(value)!r} is not in registry collection '{collection}'",
                    field, semantic=True, stage=stage,
                ))

    if needs_lookup and collections is None:
        issues.append(_issue(
            "registry-unavailable",
            f"registry is {registry_status}; cannot resolve canonical IDs",
            None, semantic=True, stage=stage,
        ))
    return issues
