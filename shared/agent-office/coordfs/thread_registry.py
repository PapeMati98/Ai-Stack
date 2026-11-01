"""Additive, metadata-only Office thread crosswalk.

This is deliberately separate from the legacy inbox and project journals. A
registry result is advisory until the caller validates the exact recipient at
send time; neither a title nor a remembered Desktop id is a transport address.
"""

from __future__ import annotations

import copy
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .atomic import exclusive_mutex, replace_with_backoff
from .errors import CoordError
from .store import assert_writable


SCHEMA = "office-threads/v1"
MAX_STORE_BYTES = 4 * 1024 * 1024
MAX_RECORDS = 4096
AGENT_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}-[a-z0-9][a-z0-9-]{0,63}-[a-z0-9][a-z0-9._-]{0,255}\Z")
TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}\Z")
NATIVE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
LOCAL_RE = re.compile(r"^local_[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
REFERENCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#-]{0,511}\Z")
KEY_RE = re.compile(r"^[0-9a-f]{32}\Z")
NATIVE_FIELDS = frozenset({"local_id", "cli_session_id", "thread_id", "sess_id"})
RECORD_FIELDS = frozenset({
    "thread_key", "office_agent", "tool", "surface", "native_ids", "title",
    "project_slug", "topic", "status", "last_seen", "group", "cwd", "observed_at",
    "identity_links", "live_endpoint", "endpoint_expires_at", "endpoint_history", "provenance",
})
LINK_FIELDS = frozenset({"a", "b", "evidence", "observed_at", "confidence"})
PROVENANCE_FIELDS = frozenset({"imported_by", "evidence"})
FORBIDDEN_FIELDS = frozenset({"body", "prompt", "contacts", "style", "message", "transcript"})
SURFACES = frozenset({"desktop", "cli", "fleet", "server"})
STATUSES = frozenset({"active", "inactive", "archived", "expired", "unknown"})


def _result(status: str, code: str, **payload: Any) -> dict[str, Any]:
    return {"status": status, "code": code, **payload}


def _error(exc: CoordError) -> dict[str, Any]:
    return _result("error", exc.code, message=str(exc), **exc.payload)


def _fail(code: str, message: str, **payload: Any) -> None:
    raise CoordError(message, code=code, payload=payload)


def _text(value: Any, field: str, limit: int, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value or len(value) > limit or "\x00" in value:
        _fail("invalid_record", f"{field} must be bounded text")
    try:
        value.encode("utf-8")
    except UnicodeError:
        _fail("invalid_record", f"{field} must be UTF-8")
    return value


def _timestamp(value: Any, field: str, *, optional: bool = True) -> str | None:
    text = _text(value, field, 40, optional=optional)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone required")
    except ValueError:
        _fail("invalid_record", f"{field} requires an ISO timestamp with timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _instant(value: str) -> datetime:
    """Compare normalized timestamps as instants, including fractional seconds."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _newer(observed: str | None, previous: str | None) -> bool:
    return observed is not None and (previous is None or _instant(observed) > _instant(previous))


def _repeated_harness(address: str) -> bool:
    """Recognize repeated Office prefixes, including hyphenated harness names."""
    harness, separator, suffix = address.partition("-ailab-")
    return bool(separator and ("-ailab-" in suffix or suffix.startswith("ailab-") or
                               suffix.startswith(harness + "-")))


def _agent(value: Any, *, field: str = "office_agent") -> str:
    text = _text(value, field, 400)
    if _repeated_harness(text):
        _fail("double_prefix", f"{field} contains a repeated identity prefix")
    if not AGENT_RE.fullmatch(text) or text != text.lower() or "/" in text or "\\" in text:
        _fail("invalid_record", f"{field} must be a full canonical Office agent id")
    return text


def _token(value: Any, field: str) -> str:
    text = _text(value, field, 128)
    if not TOKEN_RE.fullmatch(text):
        _fail("invalid_record", f"{field} must be an exact safe token")
    return text


def _native(value: Any, field: str) -> str:
    text = _text(value, field, 256)
    if not NATIVE_RE.fullmatch(text):
        _fail("invalid_record", f"{field} must be a bounded native id")
    if field == "local_id" and not LOCAL_RE.fullmatch(text):
        _fail("invalid_record", "local_id must be a full local UUID")
    return text


def _reference(value: Any, field: str) -> str:
    text = _text(value, field, 512)
    if not REFERENCE_RE.fullmatch(text) or ".." in text.split("/"):
        _fail("invalid_record", f"{field} must be a reference, not content")
    return text


def _normal_link(value: Any, native: set[str]) -> dict[str, str]:
    if type(value) is not dict or set(value) != LINK_FIELDS:
        _fail("invalid_record", "identity_link has unapproved fields")
    a = _native(value.get("a"), "link.a")
    b = _native(value.get("b"), "link.b")
    if a == b or a not in native or b not in native:
        _fail("identity_conflict", "identity link must join distinct supplied native ids")
    confidence = value.get("confidence")
    if confidence not in {"strong", "verified"}:
        _fail("weak_identity", "identity link requires strong evidence")
    return {"a": a, "b": b, "evidence": _reference(value.get("evidence"), "evidence"),
            "observed_at": _timestamp(value.get("observed_at"), "observed_at", optional=False),
            "confidence": confidence}


def _normal_record(value: Any, *, stored: bool = False) -> dict[str, Any]:
    if type(value) is not dict or FORBIDDEN_FIELDS & value.keys() or set(value) - RECORD_FIELDS:
        _fail("invalid_record", "record has unapproved fields")
    if not stored and "endpoint_history" in value:
        _fail("invalid_record", "endpoint history is maintained by the registry")
    if stored and set(value) != RECORD_FIELDS:
        _fail("corrupt_store", "stored thread has missing fields")
    key = value.get("thread_key") or uuid.uuid4().hex
    if not isinstance(key, str) or not KEY_RE.fullmatch(key):
        _fail("invalid_record", "thread_key must be a 32-hex id")
    office_agent = _agent(value.get("office_agent"))
    tool = _token(value.get("tool"), "tool")
    if office_agent.startswith("claude-code-ailab-") and tool != "claude":
        _fail("identity_conflict", "claude-code identity requires the Claude tool")
    surface = value.get("surface")
    if surface not in SURFACES:
        _fail("invalid_record", "surface is invalid")
    native_raw = value.get("native_ids", {})
    if type(native_raw) is not dict or set(native_raw) - NATIVE_FIELDS:
        _fail("invalid_record", "native_ids has unapproved fields")
    native = {name: _native(item, name) for name, item in native_raw.items() if item is not None}
    if len(native) != len(native_raw):
        _fail("invalid_record", "native_ids cannot contain null")
    title = _text(value.get("title"), "title", 300)
    project = value.get("project_slug")
    if project is not None:
        project = _token(project, "project_slug")
    status = value.get("status", "unknown")
    if status not in STATUSES:
        _fail("invalid_record", "status is invalid")
    history_raw = value.get("endpoint_history", [])
    if type(history_raw) is not list or len(history_raw) > 64:
        _fail("invalid_record", "endpoint_history must be a bounded list")
    history = []
    for item in history_raw:
        if type(item) is not dict or set(item) != {"office_agent", "cli_session_id", "retired_at", "expires_at"}:
            _fail("invalid_record", "endpoint_history has unapproved fields")
        history.append({"office_agent": _agent(item["office_agent"]),
                        "cli_session_id": _native(item["cli_session_id"], "cli_session_id"),
                        "retired_at": _timestamp(item["retired_at"], "retired_at", optional=False),
                        "expires_at": _timestamp(item["expires_at"], "expires_at")})
    if len({item["office_agent"] for item in history}) != len(history):
        _fail("identity_conflict", "historical endpoint is duplicated")
    links_raw = value.get("identity_links", [])
    if type(links_raw) is not list or len(links_raw) > 64:
        _fail("invalid_record", "identity_links must be a bounded list")
    all_native = set(native.values()) | {item["cli_session_id"] for item in history}
    links = [_normal_link(link, all_native) for link in links_raw]
    seen_pairs = set()
    for link in links:
        pair = tuple(sorted((link["a"], link["b"])))
        if pair in seen_pairs:
            _fail("identity_conflict", "identity link is duplicated")
        seen_pairs.add(pair)
    provenance_raw = value.get("provenance")
    provenance = None
    if provenance_raw is not None:
        if type(provenance_raw) is not dict or set(provenance_raw) != PROVENANCE_FIELDS:
            _fail("invalid_record", "provenance has unapproved fields")
        provenance = {"imported_by": _agent(provenance_raw["imported_by"], field="imported_by"),
                      "evidence": _reference(provenance_raw["evidence"], "provenance.evidence")}
    live_endpoint = value.get("live_endpoint", office_agent)
    live_endpoint = _agent(live_endpoint, field="live_endpoint")
    if live_endpoint.split("-", 2)[:2] != office_agent.split("-", 2)[:2]:
        _fail("identity_conflict", "live endpoint changes harness or host")
    if live_endpoint != office_agent:
        cli_id = native.get("cli_session_id")
        if not cli_id or not live_endpoint.endswith("-" + cli_id) or not links:
            _fail("identity_conflict", "rotated endpoint requires linked cli_session_id")
    if any(item["office_agent"] == live_endpoint for item in history):
        _fail("identity_conflict", "retired endpoint cannot become live again")
    return {
        "thread_key": key, "office_agent": office_agent, "tool": tool, "surface": surface,
        "native_ids": native, "title": title, "project_slug": project,
        "topic": _text(value.get("topic"), "topic", 300, optional=True), "status": status,
        "last_seen": _timestamp(value.get("last_seen"), "last_seen"),
        "group": _text(value.get("group"), "group", 300, optional=True),
        "cwd": _text(value.get("cwd"), "cwd", 1024, optional=True),
        "observed_at": _timestamp(value.get("observed_at"), "observed_at"),
        "identity_links": links, "live_endpoint": live_endpoint, "endpoint_history": history,
        "endpoint_expires_at": _timestamp(value.get("endpoint_expires_at"), "endpoint_expires_at"),
        "provenance": provenance,
    }


class ThreadRegistry:
    """One locked, bounded crosswalk under an explicitly selected Office home."""

    def __init__(self, home: Path, *, known_project_slugs: frozenset[str] | None = None) -> None:
        self.home = Path(home).expanduser().resolve()
        self.path = self.home / "office" / "thread-registry.json"
        self.mutex = self.home / "office" / "thread-registry.lock"
        # Immutable exact set injected by Office. When set, register with a non-null
        # project_slug outside this set fails closed with unknown_project before any
        # write. None (default) means the caller has not restricted the slug space,
        # so existing behaviour is preserved. A null slug remains null regardless.
        self.known_project_slugs: frozenset[str] | None = (
            frozenset(known_project_slugs) if known_project_slugs is not None else None
        )

    def _read(self) -> list[dict[str, Any]]:
        assert_writable(self.home)
        if self.path.is_symlink():
            _fail("corrupt_store", "thread registry path is a symlink")
        try:
            with self.path.open("rb") as handle:
                raw = handle.read(MAX_STORE_BYTES + 1)
        except FileNotFoundError:
            return []
        if len(raw) > MAX_STORE_BYTES:
            _fail("corrupt_store", "thread registry exceeds size limit")
        try:
            def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
                result: dict[str, Any] = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate JSON key")
                    result[key] = value
                return result

            data = json.loads(raw, object_pairs_hook=unique_pairs)
        except (UnicodeError, ValueError):
            _fail("corrupt_store", "thread registry is not valid JSON")
        if type(data) is not dict or set(data) != {"schema", "records"} or data["schema"] != SCHEMA:
            _fail("corrupt_store", "thread registry has an unknown schema")
        rows = data["records"]
        if type(rows) is not list or len(rows) > MAX_RECORDS:
            _fail("corrupt_store", "thread registry records are invalid")
        try:
            result = [_normal_record(row, stored=True) for row in rows]
            self._check_unique(result)
        except CoordError as exc:
            _fail("corrupt_store", f"thread registry record invalid: {exc.code}")
        return result

    @staticmethod
    def _check_unique(rows: list[dict[str, Any]]) -> None:
        keys: set[str] = set()
        agents: set[str] = set()
        endpoints: dict[str, str] = {}
        natives: dict[tuple[str, str, str], str] = {}
        for row in rows:
            if row["thread_key"] in keys or row["office_agent"] in agents:
                _fail("identity_conflict", "duplicate thread key or canonical Office agent")
            keys.add(row["thread_key"])
            agents.add(row["office_agent"])
            for endpoint in (row["office_agent"], row["live_endpoint"],
                             *(item["office_agent"] for item in row["endpoint_history"])):
                prior_endpoint = endpoints.setdefault(endpoint, row["thread_key"])
                if prior_endpoint != row["thread_key"]:
                    _fail("identity_conflict", "endpoint belongs to another thread")
            for field, native_id in row["native_ids"].items():
                identity = (row["tool"], field, native_id)
                prior = natives.setdefault(identity, row["thread_key"])
                if prior != row["thread_key"]:
                    _fail("identity_conflict", "native alias belongs to another thread")
            for old in row["endpoint_history"]:
                identity = (row["tool"], "cli_session_id", old["cli_session_id"])
                prior = natives.setdefault(identity, row["thread_key"])
                if prior != row["thread_key"]:
                    _fail("identity_conflict", "historical alias belongs to another thread")

    @staticmethod
    def _claude_code_alias_row(address: str,
                                rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Find only Claude rows for a documented exact UUID harness alias."""
        prefix = next((item for item in ("claude-code-ailab-", "claude-ailab-")
                       if address.startswith(item)), None)
        if prefix is None:
            return []
        uuid_part = address[len(prefix):]
        if not LOCAL_RE.fullmatch("local_" + uuid_part):
            return []
        matches: list[dict[str, Any]] = []
        local_suffix = "local_" + uuid_part
        for row in rows:
            if row["tool"] != "claude":
                continue
            native = row["native_ids"]
            if native.get("cli_session_id") == uuid_part:
                matches.append(row)
            elif native.get("local_id") == local_suffix:
                matches.append(row)
        return matches

    @staticmethod
    def _linked_live_endpoint(row: dict[str, Any]) -> bool:
        native = row["native_ids"]
        local, cli = native.get("local_id"), native.get("cli_session_id")
        return bool(local and cli and row["live_endpoint"].endswith("-" + cli) and
                    any({link["a"], link["b"]} == {local, cli}
                        for link in row["identity_links"]))

    @staticmethod
    def _prefix_suggestions(address: str, rows: list[dict[str, Any]]) -> list[str]:
        candidates = dict.fromkeys(endpoint for row in rows for endpoint in
                                   (row["office_agent"], row["live_endpoint"])
                                   if endpoint.startswith(address) and endpoint != address)
        return list(candidates)[:10]

    def _write(self, rows: list[dict[str, Any]]) -> None:
        assert_writable(self.home)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps({"schema": SCHEMA, "records": rows}, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(data) > MAX_STORE_BYTES:
            _fail("invalid_record", "thread registry would exceed size limit")
        temporary = self.path.with_name(self.path.name + ".tmp-" + uuid.uuid4().hex)
        try:
            with temporary.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if not replace_with_backoff(temporary, self.path):
                _fail("busy", "thread registry replace remained busy")
        finally:
            # This temporary was created by this call; release only if replace failed.
            if temporary.exists():
                temporary.unlink()

    def register(self, record: dict[str, Any]) -> dict[str, Any]:
        try:
            candidate = _normal_record(record)
            # unknown_project guard runs BEFORE any write. Known slugs is an
            # immutable set injected by the Office caller (typically ProjectRegistry
            # I1). A null slug is accepted as null; a non-null slug outside the set
            # fails closed so callers cannot widen the slug space by register.
            if self.known_project_slugs is not None:
                slug = candidate.get("project_slug")
                if slug is not None and slug not in self.known_project_slugs:
                    _fail(
                        "unknown_project",
                        "project_slug is not in the known slugs injected by Office",
                        project_slug=slug,
                        known_count=len(self.known_project_slugs),
                    )
            assert_writable(self.home)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with exclusive_mutex(self.mutex):
                rows = self._read()
                existing = next((row for row in rows if row["thread_key"] == candidate["thread_key"]
                                 or row["office_agent"] == candidate["office_agent"]), None)
                if existing is None:
                    if len(rows) >= MAX_RECORDS:
                        _fail("invalid_record", "thread registry is full")
                    if candidate["live_endpoint"] != candidate["office_agent"] and not self._linked_live_endpoint(candidate):
                        _fail("identity_conflict", "live endpoint lacks current local-to-CLI evidence")
                    rows.append(candidate)
                    result = candidate
                else:
                    if existing["office_agent"] != candidate["office_agent"] or (
                        "thread_key" in record and existing["thread_key"] != candidate["thread_key"]
                    ):
                        _fail("identity_conflict", "canonical thread identity cannot be re-pinned")
                    if "live_endpoint" not in record:
                        # A metadata upsert does not rotate a previously linked endpoint.
                        candidate["live_endpoint"] = existing["live_endpoint"]
                    result = copy.deepcopy(existing)
                    for field in ("tool", "surface"):
                        if candidate[field] != existing[field]:
                            _fail("identity_conflict", f"{field} cannot change")
                    for name, native_id in candidate["native_ids"].items():
                        old = result["native_ids"].get(name)
                        if old and old != native_id and name != "cli_session_id":
                            _fail("identity_conflict", "native alias cannot be rebound")
                        if old and old != native_id:
                            # A CLI rotation must be backed by a newer link to the stable local id.
                            local = result["native_ids"].get("local_id")
                            link = next((x for x in candidate["identity_links"] if
                                         {x["a"], x["b"]} == {local, native_id}), None)
                            if not local or link is None or not _newer(
                                candidate["observed_at"], result["observed_at"]
                            ) or not _newer(link["observed_at"], result["observed_at"]):
                                _fail("identity_conflict", "CLI rotation needs newer local-to-CLI evidence")
                            if "live_endpoint" not in record or candidate["live_endpoint"] == existing["live_endpoint"]:
                                _fail("identity_conflict", "CLI rotation requires an explicit new endpoint")
                            if any(item["cli_session_id"] == native_id for item in result["endpoint_history"]):
                                _fail("identity_conflict", "retired CLI endpoint cannot become live again")
                            result["native_ids"][name] = native_id
                        else:
                            result["native_ids"][name] = native_id
                    for link in candidate["identity_links"]:
                        prior_index = next((index for index, prior in enumerate(result["identity_links"])
                                            if {prior["a"], prior["b"]} == {link["a"], link["b"]}), None)
                        if prior_index is None:
                            result["identity_links"].append(link)
                        elif link != result["identity_links"][prior_index]:
                            if not _newer(link["observed_at"],
                                          result["identity_links"][prior_index]["observed_at"]):
                                _fail("identity_conflict", "identity link evidence cannot be replaced by stale data")
                            result["identity_links"][prior_index] = link
                    if candidate["live_endpoint"] != existing["live_endpoint"]:
                        if candidate["live_endpoint"] == existing["office_agent"] or not _newer(
                            candidate["observed_at"], existing["observed_at"]
                        ) or any(item["office_agent"] == candidate["live_endpoint"]
                                 for item in result["endpoint_history"]):
                            _fail("identity_conflict", "stale endpoint cannot replace the live endpoint")
                        old_cli = existing["native_ids"].get("cli_session_id")
                        if not old_cli:
                            _fail("identity_conflict", "retiring an endpoint requires its CLI identity")
                        result["endpoint_history"].append({
                            "office_agent": existing["live_endpoint"],
                            "cli_session_id": old_cli,
                            "retired_at": candidate["observed_at"],
                            "expires_at": existing["endpoint_expires_at"],
                        })
                        result["live_endpoint"] = candidate["live_endpoint"]
                    if not self._linked_live_endpoint(result) and result["live_endpoint"] != result["office_agent"]:
                        _fail("identity_conflict", "live endpoint lacks current local-to-CLI evidence")
                    for field in ("title", "project_slug", "topic", "status", "last_seen", "group", "cwd",
                                  "observed_at", "endpoint_expires_at", "provenance"):
                        if field in record:
                            if field in {"status", "endpoint_expires_at"} and candidate[field] != existing[field] and (
                                not _newer(candidate["observed_at"], existing["observed_at"])
                            ):
                                _fail("identity_conflict", "status or expiry needs newer observation")
                            if field in {"project_slug", "observed_at"} and (
                                existing["observed_at"] and candidate["observed_at"] and
                                _instant(candidate["observed_at"]) < _instant(existing["observed_at"])
                            ):
                                _fail("identity_conflict", "stale metadata cannot overwrite newer metadata")
                            result[field] = candidate[field]
                    rows[rows.index(existing)] = result
                self._check_unique(rows)
                changed = existing is None or result != existing
                if changed:
                    self._write(rows)
                return _result("ok", "registered", record=copy.deepcopy(result), idempotent=not changed)
        except CoordError as exc:
            return _error(exc)

    def list(self, *, project_slug: str | None = None, tool: str | None = None,
             surface: str | None = None, status: str | None = None,
             limit: int = 20, offset: int = 0) -> dict[str, Any]:
        try:
            if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
                _fail("invalid_page", "limit must be 1..100 and offset non-negative")
            for field, value in (("project_slug", project_slug), ("tool", tool)):
                if value is not None:
                    _token(value, field)
            if surface is not None and surface not in SURFACES:
                _fail("invalid_record", "surface filter is invalid")
            if status is not None and status not in STATUSES:
                _fail("invalid_record", "status filter is invalid")
            rows = [copy.deepcopy(row) for row in self._read() if
                    (project_slug is None or row["project_slug"] == project_slug) and
                    (tool is None or row["tool"] == tool) and
                    (surface is None or row["surface"] == surface) and
                    (status is None or row["status"] == status)]
            # Append order is stable: registration updates an existing row in place.
            # Bound the entire payload, leaving room for Office's transport metadata.
            page = rows[offset:offset + limit]
            while True:
                next_offset = offset + len(page)
                more = next_offset < len(rows)
                result = _result("ok", "threads", count=len(page), total_count=len(rows),
                                 records=page, limit=limit, offset=offset,
                                 next_offset=next_offset if more else None, has_more=more)
                encoded = (json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n").encode("utf8")
                if len(encoded) <= 60 * 1024:
                    return result
                if len(page) <= 1:
                    _fail("response_too_large", "one metadata record exceeds the response budget")
                page.pop()
        except CoordError as exc:
            return _error(exc)

    def resolve(self, office_agent: str | None = None, *, project_slug: str | None = None,
                tool: str | None = None, surface: str | None = None,
                title: str | None = None) -> dict[str, Any]:
        try:
            rows = self._read()
            if office_agent is not None:
                if any(value is not None for value in (project_slug, tool, surface, title)):
                    _fail("not_an_address", "full id and tuple cannot be combined")
                if not isinstance(office_agent, str):
                    _fail("not_an_address", "address must be text")
                if office_agent.startswith("local_"):
                    if not LOCAL_RE.fullmatch(office_agent):
                        _fail("truncated", "local id is incomplete")
                    matches = [row for row in rows if row["native_ids"].get("local_id") == office_agent]
                    if matches and not all(self._linked_live_endpoint(row) for row in matches):
                        _fail("weak_identity", "local id lacks current live CLI evidence")
                else:
                    if _repeated_harness(office_agent):
                        _fail("double_prefix", "thread selector has a repeated identity prefix")
                    if "-process-" in office_agent or "-run-" in office_agent:
                        _fail("weak_identity", "weak process or run id cannot select an address")
                    if not AGENT_RE.fullmatch(office_agent):
                        suggestions = self._prefix_suggestions(office_agent, rows)
                        if suggestions:
                            _fail("truncated", "Office id is incomplete", suggestions=suggestions)
                        _fail("not_an_address", "selector is not a full Office id")
                    matches = [row for row in rows if row["office_agent"] == office_agent or
                                row["live_endpoint"] == office_agent or
                                any(item["office_agent"] == office_agent for item in row["endpoint_history"])]
                    if not matches and office_agent.startswith(("claude-code-ailab-", "claude-ailab-")):
                        matches = self._claude_code_alias_row(office_agent, rows)
                        if matches and not all(self._linked_live_endpoint(row) for row in matches):
                            _fail("weak_identity", "Claude alias lacks current live CLI evidence")
                    if matches and office_agent.startswith("claude-code-ailab-") and any(
                        row["tool"] != "claude" or not self._linked_live_endpoint(row)
                        for row in matches
                    ):
                        _fail("weak_identity", "Claude alias lacks current live CLI evidence")
            else:
                if not all(isinstance(value, str) and value for value in
                           (project_slug, tool, surface, title)):
                    _fail("not_an_address", "resolve requires a full project/tool/surface/title tuple")
                _token(project_slug, "project_slug")
                _token(tool, "tool")
                if surface not in SURFACES:
                    _fail("not_an_address", "surface is invalid")
                _text(title, "title", 300)
                matches = [row for row in rows if (row["project_slug"], row["tool"],
                           row["surface"], row["title"]) == (project_slug, tool, surface, title)]
            if not matches:
                if office_agent is not None:
                    suggestions = self._prefix_suggestions(office_agent, rows)
                    if suggestions:
                        _fail("truncated", "Office id is incomplete", suggestions=suggestions)
                _fail("not_found", "thread was not found")
            if len(matches) != 1:
                _fail("ambiguous", "thread selector is ambiguous", candidates=[
                    {"thread_key": row["thread_key"], "office_agent": row["office_agent"]}
                    for row in matches[:10]])
            return _result("ok", "resolved", record=copy.deepcopy(matches[0]))
        except CoordError as exc:
            return _error(exc)

    def validate_recipient(self, address: str) -> dict[str, Any]:
        """Validate before *any* send write, including legacy known-agent sends."""
        try:
            rows = self._read()
            if not isinstance(address, str) or not address or len(address) > 400:
                _fail("not_an_address", "recipient is not a full address")
            if _repeated_harness(address):
                suggestions = [row["live_endpoint"] for row in rows if
                               row["live_endpoint"] in address or row["office_agent"] in address][:10]
                _fail("double_prefix", "recipient contains a repeated identity prefix",
                      suggestions=suggestions)
            if address.startswith("local_"):
                if not LOCAL_RE.fullmatch(address):
                    _fail("truncated", "local id is incomplete")
                matched = [row for row in rows if row["native_ids"].get("local_id") == address]
                if not matched:
                    _fail("not_found", "local id has no registered thread")
                if len(matched) != 1:
                    _fail("identity_conflict", "local id belongs to multiple threads")
                row = matched[0]
                if not self._linked_live_endpoint(row):
                    _fail("weak_identity", "local id lacks a linked live transport endpoint")
                address = row["live_endpoint"]
            elif not AGENT_RE.fullmatch(address):
                _fail("not_an_address", "recipient is not a full canonical Office id")
            if "-process-" in address or "-run-" in address:
                _fail("weak_identity", "process fallback and incomplete run identities are not addressable")
            exact = [row for row in rows if address in {row["office_agent"], row["live_endpoint"]} or
                     any(item["office_agent"] == address for item in row["endpoint_history"])]
            if len(exact) > 1:
                _fail("identity_conflict", "recipient endpoint belongs to multiple threads")
            if exact:
                row = exact[0]
                if row["live_endpoint"] != row["office_agent"] and not self._linked_live_endpoint(row):
                    _fail("weak_identity", "live endpoint lacks current local-to-CLI evidence")
                if address.startswith("claude-code-ailab-") and (
                    row["tool"] != "claude" or not self._linked_live_endpoint(row)
                ):
                    _fail("weak_identity", "Claude alias lacks current live CLI evidence")
                if address != row["live_endpoint"] or row["status"] in {"expired", "archived"} or (
                    row["endpoint_expires_at"] and
                    _instant(row["endpoint_expires_at"]) <= datetime.now(timezone.utc)
                ):
                    _fail("endpoint_expired", "the exact endpoint is no longer live")
                return _result("ok", "recipient", office_agent=address, thread_key=row["thread_key"])
            # Metadata-evidenced alias for documented harness variants. Exact match
            # already failed; only an address whose UUID appears in a row's native
            # ids can resolve here. Canonical office_agent is never repinned; we
            # return the row's live_endpoint so the caller's send targets the
            # currently live transport. Two matches is a closed failure.
            alias_matches = self._claude_code_alias_row(address, rows)
            if len(alias_matches) > 1:
                _fail("identity_conflict",
                      "claude-code alias matches multiple threads; alias is evidence-only")
            if alias_matches:
                row = alias_matches[0]
                if not self._linked_live_endpoint(row):
                    _fail("weak_identity", "Claude alias lacks current live CLI evidence")
                if row["status"] in {"expired", "archived"}:
                    _fail("endpoint_expired",
                          "alias maps to an archived or expired thread",
                          alias_evidence="uuid_match")
                if row["endpoint_expires_at"] and (
                    _instant(row["endpoint_expires_at"]) <= datetime.now(timezone.utc)
                ):
                    _fail("endpoint_expired",
                          "alias maps to an expired endpoint",
                          alias_evidence="uuid_match")
                return _result("ok", "recipient", office_agent=row["live_endpoint"],
                                thread_key=row["thread_key"], alias_evidence="uuid_match")
            candidates = self._prefix_suggestions(address, rows)
            if candidates:
                _fail("truncated", "recipient is a prefix of a known full id",
                      suggestions=candidates)
            if address.startswith("claude-code-ailab-"):
                _fail("not_found", "Claude alias has no evidenced thread")
            # Legacy Office addresses remain valid when they are known; this registry
            # never creates, changes, or deletes their registration files.
            known = self.home / "office" / "known-agents" / address
            if known.is_file() and not known.is_symlink():
                return _result("ok", "recipient", office_agent=address, legacy=True)
            _fail("not_found", "recipient is not a registered full Office id")
        except CoordError as exc:
            return _error(exc)
