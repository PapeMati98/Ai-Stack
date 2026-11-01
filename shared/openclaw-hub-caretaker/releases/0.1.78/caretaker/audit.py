"""Durable autonomous publication audit: grouping, threshold/age trigger, frozen cycles.

A global eligible backlog triggers a durable audit cycle at threshold OR at
age. A triggered cycle freezes the admitted proposal set (IDs, paths, hashes);
bounded per-project batches drain over ticks regardless of remaining count and
new arrivals wait for the next cycle. Empty work never invokes a model.
A project_allowed predicate can retire an ACTIVE cycle that has no in-scope
pending member to SCOPED_DRAINED without rewriting those rows, then freeze
only allowed proposals that are not already frozen in the lane. Identical
failed, quarantined, unknown, published, and accounted rows are not copied.
Pending rows on a SCOPED_DRAINED cycle stay evidence and are reactivated in
place only when a later predicate authorizes their project.

Grouping is conservative: actual metadata project+producer/agent AND
episode/session when both are present, else exact source IDs. Never semantic
guessing. Producer/agent alone must not collapse distinct sources. All
selected group originals stay accounted; oversized groups are split into
ordered max_batch chunks rather than blocked or dropped. Each project candidate still
requires its own independent exact-manifest review; a global cycle is never
one model review.
"""

from __future__ import annotations
from caretaker import tick_budget

import datetime as dt
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .publication import (PublicationError, canonical_project_slug, fm, _ids,
                          merge_project_proof, no_durable_session_project,
                          proposal_project_identity, proposal_target_pages,
                          proposal_text_project_proof, safe_file, sha,
                          source_closeout_project)
from .filter import contains_secret, has_reasoning_marker, looks_like_raw_tool_dump
from .registry import read_registry


MIN_READY_GROUPS = 10
MAX_WAIT_HOURS = 24
MAX_BATCH = 8

POLICY_DEFAULTS = {
    "min_ready_groups": MIN_READY_GROUPS,
    "max_wait_hours": MAX_WAIT_HOURS,
    "max_batch": MAX_BATCH,
}


def policy_from(control_pub: Dict[str, Any]) -> Dict[str, int]:
    """Explicit publication policy with truthful defaults."""
    def _int(key: str, default: int, lo: int, hi: int) -> int:
        try:
            value = int(control_pub.get(key, default))
        except (TypeError, ValueError):
            return default
        return min(hi, max(lo, value))
    return {
        "min_ready_groups": _int("min_ready_groups", MIN_READY_GROUPS, 1, 100000),
        "max_wait_hours": _int("max_wait_hours", MAX_WAIT_HOURS, 1, 8760),
        "max_batch": _int("max_batch", MAX_BATCH, 1, 8),
    }


def _project_of(text: str, targets: List[str], registry: dict | None = None) -> str:
    if registry is not None:
        identity = proposal_project_identity(text, registry)
        if identity:
            return identity
    slug = fm(text, "project_slug")
    if isinstance(slug, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,100}", slug):
        return (canonical_project_slug(slug, registry) or slug) if registry else slug
    for target in targets:
        tick_budget.check('audit.py:metadata_or_work')
        m = re.fullmatch(r"(?:wiki/)?projects/([a-z0-9][a-z0-9-]*)(?:\.md)?", target)
        if m:
            return ((canonical_project_slug(m.group(1), registry) or m.group(1))
                    if registry else m.group(1))
    title_project = no_durable_session_project(text)
    return ((canonical_project_slug(title_project, registry) or "_unscoped")
            if registry else (title_project or "_unscoped"))


def _agent_from_path(name: str) -> str:
    parts = name.split("/")
    if len(parts) >= 3 and parts[0] == "proposals" and parts[1]:
        return parts[1]
    return ""


def _producer_of(text: str, name: str) -> str:
    for key in ("agent", "producer", "reporter"):
        tick_budget.check('audit.py:metadata_or_work')
        value = fm(text, key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return _agent_from_path(name)


def _episode_of(text: str) -> str:
    for key in ("episode", "session", "session_id", "native_session_id"):
        tick_budget.check('audit.py:metadata_or_work')
        value = fm(text, key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _source_session_index(vault: Path) -> Dict[str, str]:
    """Optional session identity from source metadata; missing files are skipped."""
    index: Dict[str, str] = {}
    root = vault / "raw" / "sources"
    if not root.exists():
        return index
    for path in root.glob("*/*/*/source.json"):
        tick_budget.check('audit.py:metadata_or_work')
        try:
            rel = path.relative_to(vault).as_posix()
            meta = json.loads(safe_file(vault, rel).read_text(encoding="utf-8"))
        except (OSError, ValueError, PublicationError, TypeError):
            continue
        if not isinstance(meta, dict):
            continue
        sid = meta.get("source_id")
        if not isinstance(sid, str) or not sid:
            continue
        session = (meta.get("native_session_id") or meta.get("session_id")
                   or meta.get("session") or meta.get("episode"))
        if isinstance(session, str) and session.strip():
            index[sid] = session.strip()
        # Gateway session closeouts carry bounded provenance in original.md,
        # not source.json. Integrity is checked before using it for grouping.
        if sid not in index and isinstance(meta.get('original_file'), str):
            try:
                original = safe_file(vault, (path.parent / meta['original_file']).relative_to(vault).as_posix())
                data = original.read_bytes()
                if sha(data) != meta.get('sha256') or len(data) != meta.get('byte_length'):
                    continue
                head = data.decode('utf-8-sig').split('\n\n', 2)
                text = data.decode('utf-8-sig')[:2000]
                if not text.startswith('# Agent session closeout\n'):
                    continue
                fields = dict(re.findall(r'^(Agent|Project|Session SHA-256): ([^\r\n]+)$', text, re.M))
                session_hash = fields.get('Session SHA-256','')
                if re.fullmatch('[0-9a-f]{64}', session_hash) and fields.get('Agent') and fields.get('Project'):
                    index[sid] = (fields['Agent'], fields['Project'], session_hash)
            except (OSError, ValueError, UnicodeError, PublicationError):
                continue
    return index


def _episode_from_sources(source_ids: Sequence[str], sessions: Dict[str, str], producer="", project="") -> str:
    found = []
    for sid in source_ids:
        tick_budget.check('audit.py:metadata_or_work')
        value = sessions.get(sid)
        if isinstance(value, tuple):
            if value[:2] != (producer, project):
                return ''
            value = value[2]
        if value:
            found.append(value)
    if len(found) != len(source_ids) or not found:
        return ""
    if len(set(found)) != 1:
        return ""
    return found[0]


def accounted_proposal_ids(vault: Path) -> set:
    """Proposal IDs already dispositioned by a readable curation ledger.

    Unreadable or malformed ledgers fail closed so published work cannot
    appear unaccounted.
    """
    vault = Path(vault)
    accounted = set()
    ledger_dir = vault / "governance" / "curations"
    if not ledger_dir.exists():
        return accounted
    try:
        paths = sorted(ledger_dir.glob("*.json"))
    except OSError as exc:
        raise PublicationError("unreadable_curation_ledger") from exc
    for path in paths:
        tick_budget.check('audit.py:metadata_or_work')
        try:
            rel = path.relative_to(vault).as_posix()
            raw = safe_file(vault, rel).read_text(encoding="utf-8")
            row = json.loads(raw)
        except (OSError, ValueError, PublicationError, TypeError, UnicodeError) as exc:
            raise PublicationError("unreadable_curation_ledger") from exc
        if not isinstance(row, dict):
            raise PublicationError("unreadable_curation_ledger")
        items = row.get("proposals", [])
        if items is None:
            items = []
        if not isinstance(items, list):
            raise PublicationError("unreadable_curation_ledger")
        for item in items:
            tick_budget.check('audit.py:metadata_or_work')
            if not isinstance(item, dict):
                raise PublicationError("unreadable_curation_ledger")
            if item.get("disposition") in {"synthesized", "historical", "unresolved", "orphan"}:
                accounted.add(item.get("proposal_id"))
    return accounted


def eligible_proposals(vault: Path) -> Tuple[List[Dict[str, Any]], int]:
    """Read eligible proposals with identity metadata; count unsupported."""
    vault = Path(vault)
    registry, _ = read_registry(vault)
    accounted = accounted_proposal_ids(vault)
    sessions = _source_session_index(vault)
    eligible: List[Dict[str, Any]] = []
    unsupported = 0
    proposals_root = vault / "proposals"
    if not proposals_root.exists():
        return eligible, unsupported
    for path in sorted(proposals_root.glob("*/*.md")):
        tick_budget.check('audit.py:metadata_or_work')
        name = path.relative_to(vault).as_posix()
        try:
            data = safe_file(vault, name).read_bytes()
            text = data.decode("utf-8-sig").replace("\r\n", "\n")
            pid = fm(text, "id")
            if pid in accounted or fm(text, "classification") not in {"internal", "public"}:
                continue
            targets = proposal_target_pages(text)
            source_ids = _ids(text, "source_ids")
            if not source_ids:
                unsupported += 1
                continue
            if not isinstance(pid, str) or not re.fullmatch(r"prop-[A-Za-z0-9._-]+", pid):
                unsupported += 1
                continue
            project = _project_of(text, targets, registry)
            if project == '_unscoped':
                unsupported += 1
                continue
            producer = _producer_of(text, name)
            episode = _episode_of(text) or _episode_from_sources(source_ids, sessions, producer, project)
            stat = path.stat()
            eligible.append({
                "proposal_id": pid,
                "path": name,
                "sha256": sha(data),
                "bytes": len(data),
                "mtime": stat.st_mtime,
                "project": project,
                "producer": producer,
                "episode": episode,
                "source_ids": sorted(source_ids),
                "target_pages": sorted(targets),
            })
        except (PublicationError, OSError, ValueError, UnicodeError):
            unsupported += 1
            continue
    eligible.sort(key=lambda r: (r["project"], r["proposal_id"], r["path"]))
    return eligible, unsupported


#: The publication freeze bounds every frozen input to 64 kB. The pre-check
#: uses the same bound so a member it accepts can still be frozen and a
#: member it rejects is rejected for the reason the freeze itself gives.
FROZEN_INPUT_MAX_BYTES = 64000

#: Stable, content-free disposition codes for a member whose dependency
#: closure cannot be frozen. They name the violated rule only; no source or
#: proposal text ever reaches a stored reason.
MEMBER_INELIGIBLE_CODES = frozenset({
    "proposal_path_invalid",
    "proposal_unreadable",
    "proposal_oversized",
    "proposal_metadata_invalid",
    "proposal_not_internal_or_public",
    "proposal_identity_invalid",
    "proposal_lacks_sources",
    "proposal_project_evidence_invalid",
    "proposal_targets_another_project",
    "proposal_already_accounted",
    "source_missing",
    "source_metadata_unreadable",
    "source_metadata_malformed",
    "source_metadata_oversized",
    "source_metadata_duplicate",
    "source_not_publishable",
    "source_unreadable",
    "source_oversized",
    "source_integrity_mismatch",
    "source_content_not_eligible",
})


def _reason_token(value: object, limit: int = 64) -> str:
    """Content-free token for an untrusted identifier used in a reason."""
    return re.sub(r"[^A-Za-z0-9._:-]", "_", str(value or ""))[:limit]


def _member_reason(code: str, proposal_sha: str = "", source_id: str = "",
                   source_sha: str = "") -> str:
    """A hash-specific, content-free outcome for one isolated member.

    The proposal hash and the offending source hash bind the outcome to the
    exact frozen inputs, so a changed input is a different outcome and never
    a silently reused disposition.
    """
    parts = [code]
    if proposal_sha:
        parts.append("p=" + str(proposal_sha))
    if source_id:
        parts.append("s=" + _reason_token(source_id))
    if source_sha:
        parts.append("h=" + str(source_sha))
    return " ".join(parts)


def source_metadata_index(vault: Path) -> Dict[str, Dict[str, Any]]:
    """One bounded index of source metadata for this invocation.

    The index is built once and reused for every member of the batch, so a
    tick never rescans the source tree per proposal. A source whose metadata
    is unreadable, malformed, oversized or declared twice is recorded as an
    error entry: the member that cites it fails closed instead of entering a
    frozen batch. A missing identifier is simply absent, exactly as the
    publication freeze sees it.
    """
    vault = Path(vault)
    index: Dict[str, Dict[str, Any]] = {}
    root = vault / "raw" / "sources"
    if not root.exists():
        return index
    for path in sorted(root.glob("*/*/*/source.json")):
        tick_budget.check('audit.py:metadata_or_work')
        rel = path.relative_to(vault).as_posix()
        folder = path.parent.name
        try:
            data = safe_file(vault, rel).read_bytes()
        except (OSError, PublicationError, ValueError):
            index.setdefault(folder, {"error": "source_metadata_unreadable", "path": rel})
            continue
        if len(data) > FROZEN_INPUT_MAX_BYTES:
            index.setdefault(folder, {"error": "source_metadata_oversized", "path": rel})
            continue
        try:
            meta = json.loads(data.decode("utf-8-sig"))
        except (ValueError, UnicodeError):
            index.setdefault(folder, {"error": "source_metadata_malformed", "path": rel})
            continue
        if not isinstance(meta, dict):
            index.setdefault(folder, {"error": "source_metadata_malformed", "path": rel})
            continue
        sid = meta.get("source_id")
        if not isinstance(sid, str) or not sid:
            index.setdefault(folder, {"error": "source_metadata_malformed", "path": rel})
            continue
        existing = index.get(sid)
        if existing is not None:
            # The freeze is last-wins by source_id, and identical metadata can
            # still point to different original bytes. Any duplicate is thus
            # ambiguous; keep the error sticky for every later occurrence.
            if not existing.get("error"):
                index[sid] = {"error": "source_metadata_duplicate", "path": rel}
            continue
        index[sid] = {"meta": meta, "path": rel, "dir": str(path.parent)}
    return index


def member_eligibility(vault: Path, member: Dict[str, Any], project: str,
                        registry: dict | None,
                        index: Dict[str, Dict[str, Any]],
                        accounted: Optional[set] = None) -> Dict[str, Any]:
    """Dependency-closure eligibility for one frozen batch member.

    The closure of a member is the proposal plus every source it cites. A
    member enters a batch only when its whole closure is publishable, so one
    unsafe, missing, hash-bad, oversized or local-only source isolates
    exactly the members that depend on it. The checks mirror the publication
    freeze, so a member accepted here can still be frozen unchanged, and the
    source proof, the accounted ledger and the privacy checks are never
    waived.
    """
    vault = Path(vault)
    name = str(member.get("path") or "")
    proposal_sha = str(member.get("sha256") or "")

    def reject(code: str, source_id: str = "", source_sha: str = "") -> Dict[str, Any]:
        return {
            "eligible": False,
            "code": code,
            "reason": _member_reason(code, proposal_sha, source_id, source_sha),
            "source_id": _reason_token(source_id) if source_id else "",
        }

    if not name.startswith("proposals/") or not name.endswith(".md"):
        return reject("proposal_path_invalid")
    try:
        data = safe_file(vault, name).read_bytes()
    except (OSError, PublicationError, ValueError):
        return reject("proposal_unreadable")
    if len(data) > FROZEN_INPUT_MAX_BYTES:
        return reject("proposal_oversized")
    try:
        ptext = data.decode("utf-8-sig").replace("\r\n", "\n")
    except UnicodeError:
        return reject("proposal_unreadable")
    if not proposal_sha:
        proposal_sha = sha(data)
    try:
        classification = fm(ptext, "classification")
    except PublicationError:
        return reject("proposal_metadata_invalid")
    if classification not in {"internal", "public"}:
        return reject("proposal_not_internal_or_public")
    try:
        text_proof = proposal_text_project_proof(ptext, registry)
        ids = _ids(ptext, "source_ids")
        pid = fm(ptext, "id")
    except PublicationError:
        return reject("proposal_metadata_invalid")
    if not ids:
        return reject("proposal_lacks_sources")
    if not isinstance(pid, str) or not re.fullmatch(r"prop-[A-Za-z0-9._-]+", pid):
        return reject("proposal_identity_invalid")
    bodies: List[str] = []
    for sid in ids:
        entry = index.get(sid)
        if entry is None:
            return reject("source_missing", source_id=sid)
        if entry.get("error"):
            return reject(str(entry["error"]), source_id=sid)
        meta = entry.get("meta") or {}
        if meta.get("classification") not in {"internal", "public"} or meta.get("local_only") is True:
            return reject("source_not_publishable", source_id=sid)
        original_name = meta.get("original_file")
        if not isinstance(original_name, str) or not original_name:
            return reject("source_metadata_malformed", source_id=sid)
        try:
            original = safe_file(Path(entry["dir"]), original_name)
            body_bytes = original.read_bytes()
        except (OSError, PublicationError, ValueError):
            return reject("source_unreadable", source_id=sid)
        if len(body_bytes) > FROZEN_INPUT_MAX_BYTES:
            return reject("source_oversized", source_id=sid)
        source_sha = sha(body_bytes)
        if source_sha != meta.get("sha256") or len(body_bytes) != meta.get("byte_length"):
            return reject("source_integrity_mismatch", source_id=sid, source_sha=source_sha)
        try:
            body = body_bytes.decode("utf-8-sig")
        except UnicodeError:
            return reject("source_unreadable", source_id=sid)
        if contains_secret(body) or has_reasoning_marker(body) or looks_like_raw_tool_dump(body):
            return reject("source_content_not_eligible", source_id=sid, source_sha=source_sha)
        bodies.append(body.replace("\r\n", "\n"))
    try:
        proof = merge_project_proof(
            text_proof,
            source_closeout_project(bodies, registry, text_proof.get("canonical")),
        )
    except PublicationError:
        return reject("proposal_project_evidence_invalid")
    if proof.get("canonical") is None or proof.get("canonical") != project:
        return reject("proposal_targets_another_project")
    if accounted and pid in accounted:
        return reject("proposal_already_accounted")
    return {"eligible": True, "code": "", "reason": "", "proposal_id": pid,
            "source_ids": list(ids)}


def partition_batch_members(vault: Path, members: List[Dict[str, Any]], project: str,
                             registry: dict | None,
                             index: Optional[Dict[str, Dict[str, Any]]] = None,
                             accounted: Optional[set] = None
                             ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split one batch into members that may be frozen and isolated members.

    Returns (eligible, isolated). An isolated member keeps its frozen
    proposal id, path and hash, so its preserved outcome stays bound to the
    exact inputs that were rejected. Nothing is dropped and nothing that
    still needs review is marked historical, synthesized or accounted.
    """
    vault = Path(vault)
    if index is None:
        index = source_metadata_index(vault)
    if accounted is None:
        accounted = accounted_proposal_ids(vault)
    eligible: List[Dict[str, Any]] = []
    isolated: List[Dict[str, Any]] = []
    for member in members:
        tick_budget.check('audit.py:metadata_or_work')
        outcome = member_eligibility(vault, member, project, registry, index, accounted)
        if outcome.get("eligible"):
            eligible.append(member)
            continue
        record = dict(member)
        record["reason"] = outcome.get("reason") or ""
        record["code"] = outcome.get("code") or ""
        record["source_id"] = outcome.get("source_id") or ""
        isolated.append(record)
    return eligible, isolated


def group_key(meta: Dict[str, Any]) -> str:
    """Conservative group identity: complete episode identity, else exact sources.

    Meta grouping requires both a producer/agent AND an episode/session.
    Producer alone must not collapse every distinct source from an agent.
    """
    producer = str(meta.get("producer") or "").strip()
    episode = str(meta.get("episode") or "").strip()
    project = str(meta.get("project") or "")
    if producer and episode:
        return "meta\x1f" + "\x1f".join((project, producer, episode))
    source_ids = meta.get("source_ids") or []
    return "sources\x1f" + project + "\x1f" + ",".join(source_ids)


def build_groups(eligible: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Group eligible proposals; every original stays accounted in its group."""
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for meta in eligible:
        tick_budget.check('audit.py:metadata_or_work')
        buckets.setdefault(group_key(meta), []).append(meta)
    groups = []
    for key in sorted(buckets):
        tick_budget.check('audit.py:metadata_or_work')
        members = sorted(buckets[key], key=lambda m: (m["proposal_id"], m["path"]))
        source_ids = sorted({sid for m in members for sid in m["source_ids"]})
        groups.append({
            "group_key": key,
            "project": members[0]["project"],
            "proposal_ids": [m["proposal_id"] for m in members],
            "paths": [m["path"] for m in members],
            "source_ids": source_ids,
            "member_source_ids": [list(m["source_ids"]) for m in members],
            "size": len(members),
            "oldest_mtime": min(m["mtime"] for m in members),
        })
    return groups


def summarize(vault: Path, policy: Dict[str, int], now: Optional[float] = None,
              excluded=(), eligible_filter=None) -> Dict[str, Any]:
    """Expose raw/group counts, oldest age, trigger, deferred/unsupported.

    Counts come from proposal files and curation ledgers, never from raw
    intake events.
    """
    now_ts = float(now) if now is not None else dt.datetime.now(dt.timezone.utc).timestamp()
    eligible, unsupported = eligible_proposals(vault)
    if eligible_filter is not None:
        eligible = [m for m in eligible if eligible_filter(m)]
        # Unsupported legacy files cannot be attributed to this lane.
        unsupported = 0
    exclusions = set(excluded)
    raw_total = len(eligible)
    eligible = [m for m in eligible if (m['proposal_id'],m['path'],m['sha256']) not in exclusions]
    quarantined = raw_total - len(eligible)
    groups = build_groups(eligible)
    max_batch = int(policy.get("max_batch", MAX_BATCH))
    deferred = 0
    deferred_groups: List[str] = []
    ready_groups: List[Dict[str, Any]] = []
    for group in groups:
        tick_budget.check('audit.py:metadata_or_work')
        if group["size"] > max_batch:
            deferred += group["size"]
            deferred_groups.append(group["group_key"])
        else:
            ready_groups.append(group)
    if eligible:
        oldest_mtime = min(m["mtime"] for m in eligible)
        oldest_age_hours = (now_ts - oldest_mtime) / 3600.0
    else:
        oldest_mtime = None
        oldest_age_hours = 0.0
    if ready_groups:
        oldest_ready_mtime = min(g["oldest_mtime"] for g in ready_groups)
        oldest_ready_age_hours = (now_ts - oldest_ready_mtime) / 3600.0
    else:
        oldest_ready_age_hours = 0.0
    trigger = None
    min_ready = int(policy.get("min_ready_groups", MIN_READY_GROUPS))
    max_wait = float(policy.get("max_wait_hours", MAX_WAIT_HOURS))
    if ready_groups and len(ready_groups) >= min_ready:
        trigger = "threshold"
    elif ready_groups and oldest_ready_age_hours >= max_wait:
        trigger = "age"
    return {
        "raw_eligible": len(eligible),
        "quarantined_proposals": quarantined,
        "group_count": len(groups),
        "ready_group_count": len(ready_groups),
        "oldest_mtime": oldest_mtime,
        "oldest_age_hours": oldest_age_hours,
        "oldest_ready_age_hours": oldest_ready_age_hours,
        "trigger": trigger,
        "deferred": deferred,
        "deferred_groups": deferred_groups,
        "unsupported": unsupported,
        "groups": groups,
        "eligible": eligible,
        "count_source": "proposals",
        "policy": dict(policy),
    }


AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_cycles(
    id TEXT PRIMARY KEY,
    trigger TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,
    counts TEXT NOT NULL,
    lane TEXT NOT NULL DEFAULT 'legacy'
);
CREATE TABLE IF NOT EXISTS audit_members(
    cycle_id TEXT NOT NULL,
    proposal_id TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    project TEXT NOT NULL,
    group_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    reason TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(cycle_id, proposal_id, path)
);
"""


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(AUDIT_SCHEMA)
    cycle_cols = {row[1] for row in conn.execute("PRAGMA table_info(audit_cycles)").fetchall()}
    if "lane" not in cycle_cols:
        conn.execute("ALTER TABLE audit_cycles ADD COLUMN lane TEXT NOT NULL DEFAULT 'legacy'")
    cols = {row[1] for row in conn.execute("PRAGMA table_info(audit_members)").fetchall()}
    if "group_key" not in cols:
        conn.execute("ALTER TABLE audit_members ADD COLUMN group_key TEXT NOT NULL DEFAULT ''")
    if "reason" not in cols:
        conn.execute("ALTER TABLE audit_members ADD COLUMN reason TEXT NOT NULL DEFAULT ''")
    conn.commit()


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _cells(row: Any) -> tuple:
    """Index-stable row view; independent of ambient sqlite3.row_factory."""
    if row is None:
        return ()
    return tuple(row)


def _member_from_row(row: Any) -> Dict[str, Any]:
    cells = _cells(row)
    return {
        "proposal_id": cells[0],
        "path": cells[1],
        "sha256": cells[2],
        "project": cells[3],
        "group_key": cells[4] if len(cells) > 4 else "",
        "status": cells[5] if len(cells) > 5 else "pending",
        "reason": cells[6] if len(cells) > 6 else "",
    }


def active_cycle(conn: sqlite3.Connection, lane: str = 'legacy') -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT id, trigger, created_at, status, counts FROM audit_cycles "
        "WHERE status='ACTIVE' AND lane=? ORDER BY created_at LIMIT 1", (lane,)
    ).fetchone()
    if row is None:
        return None
    cells = _cells(row)
    members = conn.execute(
        "SELECT proposal_id, path, sha256, project, group_key, status, reason "
        "FROM audit_members WHERE cycle_id=? ORDER BY project, proposal_id, path",
        (cells[0],),
    ).fetchall()
    counts_raw = cells[4]
    try:
        counts = json.loads(counts_raw) if counts_raw else {}
    except (TypeError, ValueError):
        counts = {}
    return {
        "id": cells[0],
        "trigger": cells[1],
        "created_at": cells[2],
        "status": cells[3],
        "lane": lane,
        "counts": counts,
        "members": [_member_from_row(m) for m in members],
    }


def _has_in_scope_pending(cycle: Dict[str, Any], predicate) -> bool:
    for member in cycle.get("members") or []:
        tick_budget.check('audit.py:metadata_or_work')
        if member.get("status") == "pending" and predicate(member.get("project")):
            return True
    return False


def _lane_member_rows(conn: sqlite3.Connection, lane: str) -> List[tuple]:
    return conn.execute(
        "SELECT m.proposal_id, m.path, m.sha256, m.status "
        "FROM audit_members m JOIN audit_cycles c ON c.id=m.cycle_id WHERE c.lane=?",
        (lane,),
    ).fetchall()


def _identity_is_fresh(rows: List[tuple], meta: Dict[str, Any]) -> bool:
    """Same lane identity is not frozen twice. A changed sha256 is a new input."""
    proposal_id = meta.get("proposal_id")
    path = meta.get("path")
    digest = meta.get("sha256")
    if not proposal_id or not path or not digest:
        return False
    matches = [row for row in rows if row[0] == proposal_id and row[1] == path]
    if not matches:
        return True
    if any(row[3] == "pending" for row in matches):
        return False
    if any(row[2] == digest for row in matches):
        return False
    return True


def _fresh_allowed(conn: sqlite3.Connection, summary: Dict[str, Any], lane: str, predicate) -> List[Dict[str, Any]]:
    rows = _lane_member_rows(conn, lane)
    repaired = {
        (item.get("proposal_id"), item.get("path"), item.get("sha256"))
        for item in summary.get("repaired_retry") or [] if isinstance(item, dict)
    }
    fresh: List[Dict[str, Any]] = []
    seen = set()
    for meta in summary.get("eligible") or []:
        tick_budget.check('audit.py:metadata_or_work')
        if not isinstance(meta, dict) or not predicate(meta.get("project")):
            continue
        identity = (meta.get("proposal_id"), meta.get("path"), meta.get("sha256"))
        if identity in seen:
            continue
        seen.add(identity)
        retry_history = [row for row in rows if row[0] == identity[0] and row[1] == identity[1]]
        retry_override = (
            identity in repaired
            and not any(row[3] == "pending" for row in retry_history)
            and any(row[2] == identity[2] and row[3] in {"failed", "quarantined"}
                    for row in retry_history)
        )
        if _identity_is_fresh(rows, meta) or retry_override:
            fresh.append(meta)
    return fresh


def _restorable_scoped_cycle(conn: sqlite3.Connection, lane: str, predicate) -> Optional[str]:
    cycles = conn.execute(
        "SELECT id FROM audit_cycles WHERE lane=? AND status='SCOPED_DRAINED' ORDER BY created_at, id",
        (lane,),
    ).fetchall()
    for (cycle_id,) in cycles:
        tick_budget.check('audit.py:metadata_or_work')
        pending = conn.execute(
            "SELECT project FROM audit_members WHERE cycle_id=? AND status='pending'",
            (cycle_id,),
        ).fetchall()
        if any(predicate(project) for (project,) in pending):
            return cycle_id
    return None


def _activate_scoped(conn: sqlite3.Connection, retire_id: Optional[str], activate_id: str) -> None:
    """Swap the ACTIVE flag. Member rows are not rewritten."""
    with conn:
        if retire_id:
            conn.execute(
                "UPDATE audit_cycles SET status='SCOPED_DRAINED' WHERE id=? AND status='ACTIVE'",
                (retire_id,),
            )
        conn.execute(
            "UPDATE audit_cycles SET status='ACTIVE' WHERE id=? AND status='SCOPED_DRAINED'",
            (activate_id,),
        )


def _open_cycle(conn: sqlite3.Connection, summary: Dict[str, Any], members: List[Dict[str, Any]],
                now_iso: Optional[str], lane: str, bound: int,
                predecessor_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    created = now_iso or _now_iso()
    trigger = summary.get("trigger") or "scoped"
    full = list(summary.get("eligible") or [])
    payload = [trigger, [[m["proposal_id"], m["path"], m["sha256"]] for m in members], created]
    if predecessor_id or members != full:
        payload.append(["scoped-successor", predecessor_id or ""])
    seed = json.dumps(payload, sort_keys=True)
    cycle_id = ("audit-new-" if lane == "new" else "audit-") + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]
    selected_identities = {
        (m.get("proposal_id"), m.get("path"), m.get("sha256")) for m in members
    }
    retry_metadata = [
        item for item in summary.get("repaired_retry") or []
        if isinstance(item, dict)
        and (item.get("proposal_id"), item.get("path"), item.get("sha256")) in selected_identities
    ]
    if predecessor_id is None and members == full:
        counts = {
            "raw_eligible": summary["raw_eligible"],
            "group_count": summary["group_count"],
            "ready_group_count": summary.get("ready_group_count"),
            "oldest_age_hours": summary["oldest_age_hours"],
            "deferred": summary["deferred"],
            "unsupported": summary["unsupported"],
            "count_source": "proposals",
        }
    else:
        counts = {
            "raw_eligible": len(members),
            "group_count": len({group_key(m) for m in members}),
            "ready_group_count": len({group_key(m) for m in members}),
            "oldest_age_hours": summary.get("oldest_age_hours", 0),
            "deferred": 0,
            "unsupported": summary.get("unsupported", 0),
            "count_source": "scoped_allowed_successor",
            "predecessor": predecessor_id or "",
        }
    if retry_metadata:
        counts["repaired_retry"] = retry_metadata
        from .repaired_retry import ensure_retry_table
        ensure_retry_table(conn)
    try:
        with conn:
            for item in retry_metadata:
                tick_budget.check('audit.py:metadata_or_work')
                inserted = conn.execute(
                    "INSERT OR IGNORE INTO repaired_retry_tokens("
                    "token,cycle_id,publication_sha256,code_version,proposal_id,path,sha256,previous_batch) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (item["token"], cycle_id, item["publication_sha256"], item["code_version"],
                     item["proposal_id"], item["path"], item["sha256"], item["previous_batch"]),
                )
                if inserted.rowcount != 1:
                    raise _RetryTokenAlreadyConsumed()
            if predecessor_id:
                conn.execute(
                    "UPDATE audit_cycles SET status='SCOPED_DRAINED' WHERE id=? AND status='ACTIVE'",
                    (predecessor_id,),
                )
            conn.execute(
                "INSERT OR IGNORE INTO audit_cycles(id, trigger, created_at, status, counts, lane) VALUES(?,?,?,?,?,?)",
                (cycle_id, trigger, created, "ACTIVE", json.dumps(counts, sort_keys=True), lane),
            )
            for meta in members:
                tick_budget.check('audit.py:metadata_or_work')
                conn.execute(
                    "INSERT OR IGNORE INTO audit_members("
                    "cycle_id, proposal_id, path, sha256, project, group_key, status, reason) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (cycle_id, meta["proposal_id"], meta["path"], meta["sha256"], meta["project"],
                     group_key(meta), "pending", ""),
                )
    except _RetryTokenAlreadyConsumed:
        return active_cycle(conn, lane)
    opened = active_cycle(conn, lane)
    return apply_oversize_blocks(conn, opened, bound) if opened else None


class _RetryTokenAlreadyConsumed(Exception):
    pass


def maybe_start_cycle(conn: sqlite3.Connection, summary: Dict[str, Any], now_iso: Optional[str] = None,
                      lane: str = 'legacy', project_allowed=None) -> Optional[Dict[str, Any]]:
    """Freeze the admitted set when threshold OR age triggers; empty never triggers.

    Without project_allowed this is the 0.1.50 contract: an ACTIVE cycle is
    returned as-is. With the predicate, no in-scope pending member plus fresh
    allowed work retires that cycle to SCOPED_DRAINED and freezes only the
    allowed successor. Held rows stay pending. No successor is opened when
    nothing newly authorized is waiting. A later authorizing predicate
    reactivates the oldest SCOPED_DRAINED cycle that still has pending rows.
    """
    ensure_tables(conn)
    bound = int(summary.get("policy", {}).get("max_batch", MAX_BATCH))
    current = active_cycle(conn, lane)
    if project_allowed is None:
        if current is not None:
            return apply_oversize_blocks(conn, current, bound)
        if not summary.get("trigger") or not summary.get("eligible"):
            return None
        return _open_cycle(conn, summary, list(summary["eligible"]), now_iso, lane, bound)
    if current is not None and _has_in_scope_pending(current, project_allowed):
        return apply_oversize_blocks(conn, current, bound)
    restorable = _restorable_scoped_cycle(conn, lane, project_allowed)
    if restorable is not None and (current is None or restorable != current["id"]):
        _activate_scoped(conn, current["id"] if current is not None else None, restorable)
        revived = active_cycle(conn, lane)
        return apply_oversize_blocks(conn, revived, bound) if revived else None
    fresh = _fresh_allowed(conn, summary, lane, project_allowed)
    if current is not None:
        if not fresh:
            return apply_oversize_blocks(conn, current, bound)
        return _open_cycle(conn, summary, fresh, now_iso, lane, bound, predecessor_id=current["id"])
    if not summary.get("trigger") or not fresh:
        return None
    return _open_cycle(conn, summary, fresh, now_iso, lane, bound)


def project_batches(cycle: Dict[str, Any], max_batch: int) -> Tuple[Dict[str, List[List[Dict[str, Any]]]], List[Dict[str, Any]]]:
    """Slice pending frozen members into bounded per-project batches.

    Groups that fit in max_batch are never packed across a cut. A 3-member
    group plus a 7-member group with max_batch=8 stay 3 and 7, never 8+2.
    An oversized group is split deterministically into consecutive chunks
    of at most max_batch, ordered by proposal_id then path. Every member
    stays accounted exactly once; nothing is blocked or dropped.
    """
    by_project: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    for member in cycle.get("members", []):
        tick_budget.check('audit.py:metadata_or_work')
        if member.get("status") != "pending":
            continue
        project = member.get("project") or "_unscoped"
        key = member.get("group_key") or ""
        by_project.setdefault(project, {}).setdefault(key, []).append(member)
    batches: Dict[str, List[List[Dict[str, Any]]]] = {}
    blocked: List[Dict[str, Any]] = []
    for project in sorted(by_project):
        tick_budget.check('audit.py:metadata_or_work')
        packed: List[List[Dict[str, Any]]] = []
        current: List[Dict[str, Any]] = []
        for key in sorted(by_project[project]):
            tick_budget.check('audit.py:metadata_or_work')
            items = sorted(
                by_project[project][key],
                key=lambda m: (m["proposal_id"], m["path"]),
            )
            if len(items) > max_batch:
                if current:
                    packed.append(current)
                    current = []
                for offset in range(0, len(items), max_batch):
                    tick_budget.check('audit.py:metadata_or_work')
                    packed.append(items[offset:offset + max_batch])
                continue
            if current and len(current) + len(items) > max_batch:
                packed.append(current)
                current = []
            current.extend(items)
        if current:
            packed.append(current)
        if packed:
            batches[project] = packed
    return batches, blocked


def mark_members(conn: sqlite3.Connection, cycle_id: str,
                 items: Iterable[Tuple[str, str]], status: str,
                 reason: str = "") -> None:
    items = list(items)
    if not items:
        return
    with conn:
        for proposal_id, path in items:
            tick_budget.check('audit.py:metadata_or_work')
            conn.execute(
                "UPDATE audit_members SET status=?, reason=? "
                "WHERE cycle_id=? AND proposal_id=? AND path=?",
                (status, reason, cycle_id, proposal_id, path),
            )
        remaining = conn.execute(
            "SELECT COUNT(*) FROM audit_members WHERE cycle_id=? AND status='pending'",
            (cycle_id,),
        ).fetchone()
        if remaining is not None and _cells(remaining)[0] == 0:
            conn.execute(
                "UPDATE audit_cycles SET status='DRAINED' WHERE id=?",
                (cycle_id,),
            )


def mark_member(conn: sqlite3.Connection, cycle_id: str, proposal_id: str,
                path: str, status: str, reason: str = "") -> None:
    mark_members(conn, cycle_id, [(proposal_id, path)], status, reason)


def apply_oversize_blocks(conn: sqlite3.Connection, cycle: Dict[str, Any],
                          max_batch: int) -> Optional[Dict[str, Any]]:
    """No-op for split batches; kept so callers still refresh the active cycle."""
    if cycle is None:
        return None
    _, blocked = project_batches(cycle, max_batch)
    for item in blocked:
        tick_budget.check('audit.py:metadata_or_work')
        mark_members(
            conn,
            cycle["id"],
            [(m["proposal_id"], m["path"]) for m in item["members"]],
            "blocked",
            item["reason"],
        )
    return active_cycle(conn, cycle.get('lane', 'legacy'))


def finish_cycle(conn: sqlite3.Connection, cycle_id: str) -> None:
    with conn:
        conn.execute("UPDATE audit_cycles SET status='DRAINED' WHERE id=?", (cycle_id,))


def cycle_projects(cycle: Optional[Dict[str, Any]]) -> List[str]:
    """Pending projects only, first-seen in member order.

    Terminal members must not keep an exhausted allowed project selected.
    A missing status counts as pending.
    """
    if not cycle:
        return []
    seen: List[str] = []
    for member in cycle.get("members") or []:
        tick_budget.check('audit.py:metadata_or_work')
        if member.get("status", "pending") != "pending":
            continue
        project = member.get("project")
        if project and project not in seen:
            seen.append(project)
    return seen
