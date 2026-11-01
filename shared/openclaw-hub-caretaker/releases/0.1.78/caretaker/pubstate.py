"""Recoverable durable publication state: backoff, quarantine, reconciliation.

- Known transient model transport outages get bounded exponential backoff;
  ordinary outages never permanently freeze after two errors.
- Deterministic input/policy/semantic failures quarantine the exact
  fingerprint until the input, code, or policy fingerprint changes; the model
  is never retried forever on the same bytes.
- A RUNNING row after a crash is never blindly republished: it reconciles
  against the canonical committed event/transaction or stays explicit
  UNKNOWN requiring safe reconciliation.
- Old publication rows are preserved; columns are added compatibly.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


TRANSIENT_CODES = frozenset({
    "native_timeout",
    "process_timeout",
    "nonzero_exit",
    "spawn_failed",
    "model_error",
    # 0.1.16: Fleet failures split by cause. A quota or a gate refusal clears
    # with time; a handoff-contract miss is retried after the format repair.
    "provider_quota",
    "fleet_refused",
    "handoff_contract",
    "proposals_accounted_elsewhere",
    "tick_time_budget_exhausted",
})

BACKOFF_BASE_SECONDS = 300
BACKOFF_CAP_SECONDS = 4 * 3600
MAX_DETERMINISTIC_ATTEMPTS = 2
MAX_TRANSIENT_ATTEMPTS = 5


def ensure_pub_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS publications(batch TEXT PRIMARY KEY,status TEXT,failures INTEGER,result TEXT)"
    )
    existing = {row[1] for row in conn.execute("PRAGMA table_info(publications)").fetchall()}
    for column, ddl in (
        ("attempt", "ALTER TABLE publications ADD COLUMN attempt INTEGER NOT NULL DEFAULT 0"),
        ("next_due", "ALTER TABLE publications ADD COLUMN next_due TEXT NOT NULL DEFAULT ''"),
        ("category", "ALTER TABLE publications ADD COLUMN category TEXT NOT NULL DEFAULT ''"),
        ("fingerprint", "ALTER TABLE publications ADD COLUMN fingerprint TEXT NOT NULL DEFAULT ''"),
        ("updated_at", "ALTER TABLE publications ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''"),
    ):
        if column not in existing:
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError:
                pass
    conn.commit()


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def code_version() -> str:
    """Fingerprint of the publisher code that produced a batch.

    Changes when publication/curation behavior changes so quarantined
    batches are honestly re-admitted after a fix, never by epoch tricks.
    """
    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for name in ("publication.py", "audit.py", "pubstate.py", "fleet_auditor.py"):
        path = root / name
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(name.encode("utf-8"))
    try:
        import curate_once  # noqa: F401
        import inspect
        digest.update(inspect.getsource(curate_once).encode("utf-8"))
    except (ImportError, OSError, TypeError):
        pass
    return digest.hexdigest()[:32]


def batch_key(project_slug: str, paths: list) -> str:
    """Stable batch identity from scope and frozen paths only.

    Deliberately excludes any retry epoch: resets happen only through a
    changed input, code, or policy fingerprint, never epoch relabeling.
    """
    return hashlib.sha256(
        json.dumps([project_slug, sorted(paths)], sort_keys=True).encode("utf-8")
    ).hexdigest()


def fingerprint(project_slug: str, paths: list, hashes: list, policy: dict, version: Optional[str] = None) -> str:
    payload = {
        "project": project_slug,
        "paths": sorted(paths),
        "hashes": sorted(hashes),
        "policy": {k: policy.get(k) for k in ("min_ready_groups", "max_wait_hours", "max_batch")},
        "code": version or code_version(),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def classify_failure(reason: str) -> str:
    """Map a failure reason to a retry category."""
    text = str(reason)
    if text in TRANSIENT_CODES:
        return "transient"
    if any(code in text.lower() for code in (
        "curator_lock", "baseline_changed", "transaction rolled back", "lock_held"
    )):
        return "transient"
    if text.startswith("independent_review_deferred:"):
        return "transient"
    if text in {"hub_preflight_failed"}:
        return "transient"
    if text.startswith("canonical:"):
        parts = text.split(":", 2)
        code_part = parts[1].lower() if len(parts) > 1 else ""
        reason_part = parts[2].lower() if len(parts) > 2 else ""
        if any(tok in code_part or tok in reason_part for tok in (
            "curator_lock", "baseline_changed", "transaction rolled back",
            "transaction_rollback", "lock_held", "proposals_accounted_elsewhere",
            "accounted_elsewhere", "collision"
        )):
            return "transient"
    return "deterministic"


def backoff_due_iso(attempt: int, base: int = BACKOFF_BASE_SECONDS, cap: int = BACKOFF_CAP_SECONDS,
                     now: Optional[dt.datetime] = None) -> str:
    moment = now or dt.datetime.now(dt.timezone.utc)
    delay = min(cap, base * (2 ** min(20, max(0, attempt - 1))))
    return (moment + dt.timedelta(seconds=delay)).isoformat()


def read_row(conn: sqlite3.Connection, key: str) -> Optional[Dict[str, Any]]:
    ensure_pub_tables(conn)
    row = conn.execute(
        "SELECT batch, status, failures, result, attempt, next_due, category, fingerprint, updated_at"
        " FROM publications WHERE batch=?",
        (key,),
    ).fetchone()
    if row is None:
        return None
    return {
        "batch": row[0], "status": row[1], "failures": row[2], "result": row[3],
        "attempt": row[4] or 0, "next_due": row[5] or "", "category": row[6] or "",
        "fingerprint": row[7] or "", "updated_at": row[8] or "",
    }


def due_to_run(row: Optional[Dict[str, Any]], now_iso: str, current_fingerprint: str) -> Tuple[bool, str]:
    """Decide whether a batch may invoke the model on this tick."""
    if row is None:
        return True, "new"
    status = row.get("status") or ""
    if status == "PUBLISHED":
        return False, "already_published"
    if status == "RUNNING":
        # Crash-unsafe: reconcile before any republish decision.
        return False, "running_needs_reconciliation"
    if status == "UNKNOWN_REQUIRES_RECONCILIATION":
        return False, "unknown_needs_reconciliation"
    if status in ("QUARANTINED", "BLOCKED_REQUIRES_DIAGNOSIS"):
        if row.get("fingerprint") == current_fingerprint:
            return False, "quarantined_same_fingerprint"
        return True, "fingerprint_changed"
    if row.get("next_due") and row["next_due"] > now_iso:
        return False, "backoff_not_due"
    return True, "due"


def record_start(conn: sqlite3.Connection, key: str, now_iso: str, current_fingerprint: str,
                 meta: Optional[dict] = None) -> None:
    ensure_pub_tables(conn)
    row = read_row(conn, key)
    failures = int(row["failures"]) if row else 0
    attempt = int(row["attempt"] or 0) if row else 0
    if meta is not None:
        payload = dict(meta)
        payload["status"] = "RUNNING"
        result_text = json.dumps(payload, ensure_ascii=False)
    else:
        result_text = row["result"] if row and row["result"] else "{}"
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO publications(batch, status, failures, result, attempt, next_due, category, fingerprint, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (key, "RUNNING", failures, result_text,
             attempt, "", (row["category"] if row and row["category"] else ""), current_fingerprint, now_iso),
        )


def record_success(conn: sqlite3.Connection, key: str, result: dict) -> None:
    ensure_pub_tables(conn)
    row = read_row(conn, key)
    attempt = int(row["attempt"] or 0) if row else 0
    with conn:
        conn.execute(
            "UPDATE publications SET status=?, failures=?, result=?, attempt=?, next_due=?, category=?, updated_at=? WHERE batch=?",
            (result.get("status", "PUBLISHED"), int(row["failures"]) if row else 0,
             json.dumps(result, ensure_ascii=False), attempt + 1, "", "none", _now_iso(), key),
        )


def record_failure(conn: sqlite3.Connection, key: str, reason: str, result: dict,
                   current_fingerprint: str, now: Optional[dt.datetime] = None,
                   max_transient_attempts: Optional[int] = None) -> Dict[str, Any]:
    """Persist a failure with bounded backoff or quarantine; never delete history."""
    ensure_pub_tables(conn)
    moment = now or dt.datetime.now(dt.timezone.utc)
    now_iso = moment.isoformat()
    row = read_row(conn, key)
    failures = (int(row["failures"]) if row else 0) + 1
    attempt = (int(row["attempt"] or 0) if row else 0) + 1
    original_reason = reason
    category = classify_failure(reason)
    exhausted = bool(result.get("format_repair_exhausted") or result.get("review_repair_exhausted"))
    if category == "transient":
        limit = MAX_TRANSIENT_ATTEMPTS if max_transient_attempts is None else int(max_transient_attempts)
        if limit < 1:
            limit = MAX_TRANSIENT_ATTEMPTS
        if attempt >= limit and original_reason != "proposals_accounted_elsewhere":
            status = "QUARANTINED"
            next_due = ""
            reason = f"transient_attempts_exhausted:{original_reason}"
        else:
            status = "BACKOFF_DEFERRED"
            next_due = backoff_due_iso(attempt, now=moment)
    else:
        next_due = ""
        if exhausted or attempt >= MAX_DETERMINISTIC_ATTEMPTS:
            status = "QUARANTINED"
        else:
            status = result.get("status") or "BLOCKED"
            if status in {"PENDING", "REVIEWED_CANDIDATE", "RUNNING", "PUBLISHED"}:
                status = "BLOCKED"
    stored = dict(result)
    stored["status"] = status
    stored["reason"] = reason
    stored["original_reason"] = original_reason
    with conn:
        conn.execute(
            "UPDATE publications SET status=?, failures=?, result=?, attempt=?, next_due=?, category=?, fingerprint=?, updated_at=? WHERE batch=?",
            (status, failures, json.dumps(stored, ensure_ascii=False), attempt,
             next_due, category, current_fingerprint, now_iso, key),
        )
        changed = conn.execute("SELECT changes()").fetchone()
        if changed is None or int(tuple(changed)[0] or 0) == 0:
            conn.execute(
                "INSERT OR REPLACE INTO publications(batch, status, failures, result, attempt, next_due, category, fingerprint, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (key, status, failures, json.dumps(stored, ensure_ascii=False), attempt,
                 next_due, category, current_fingerprint, now_iso),
            )
    return {"status": status, "category": category, "attempt": attempt, "next_due": next_due}


def reconcile_running(conn: sqlite3.Connection, key: str, verdict: str, evidence: Optional[dict] = None) -> Dict[str, Any]:
    """Reconcile a RUNNING row after a crash.

    verdict is 'committed' (canonical journal proves publication),
    'absent' (canonical journal proves nothing was committed), or anything
    else. Only 'committed' republishes state; otherwise the row stays
    explicit UNKNOWN requiring safe reconciliation. Never auto-republishes.
    """
    ensure_pub_tables(conn)
    row = read_row(conn, key)
    if row is None or row.get("status") != "RUNNING":
        return {"status": row["status"] if row else "missing", "republished": False}
    if verdict == "committed":
        result = {"status": "PUBLISHED", "reconciled": True, "evidence": evidence or {}}
        journal_path, event_id = (evidence or {}).get("journal_path"), (evidence or {}).get("event_id")
        if journal_path and event_id:
            # 0.1.17: the run ended after the canonical publication, so its local
            # Git checkpoint may be missing. Leave it PENDING; the drain completes
            # it (or finds it already committed) before any new publication.
            result["publication"] = {"status": "PUBLISHED", "transaction": journal_path, "event_id": event_id}
            result["checkpoint"] = {"status": "PENDING", "reason": "reconciled_after_interrupted_run",
                                    "receipt_dir": str(Path(journal_path).parents[2])}
        with conn:
            conn.execute(
                "UPDATE publications SET status=?, result=?, updated_at=? WHERE batch=?",
                ("PUBLISHED", json.dumps(result, ensure_ascii=False), _now_iso(), key),
            )
        return {"status": "PUBLISHED", "republished": False}
    with conn:
        conn.execute(
            "UPDATE publications SET status=?, result=?, updated_at=? WHERE batch=?",
            ("UNKNOWN_REQUIRES_RECONCILIATION",
             json.dumps({"status": "UNKNOWN_REQUIRES_RECONCILIATION", "previous": row.get("result"),
                         "evidence": evidence or {}}, ensure_ascii=False),
             _now_iso(), key),
        )
    return {"status": "UNKNOWN_REQUIRES_RECONCILIATION", "republished": False}


def _load_json_file(path: Path) -> Optional[dict]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def canonical_verdict(vault: Path, row: Optional[Dict[str, Any]]) -> Tuple[str, Dict[str, Any]]:
    """Require matching committed journal, manifest, event and immutable ledger."""
    from .publication import safe_file, sha, PublicationError
    import re
    try:
        stored = json.loads((row or {}).get('result') or '{}')
    except (ValueError, TypeError):
        stored = {}
    if not isinstance(stored, dict):
        stored = {}
    evidence = {'ignored_status_string': stored.get('status')}
    pids, paths, hashes = (stored.get(k) or [] for k in ('proposal_ids','paths','hashes'))
    if not pids or len(pids) != len(paths) or len(paths) != len(hashes):
        return 'uncertain', {**evidence, 'reason':'missing_frozen_input_identity'}
    expected = dict(zip(pids, zip(paths, hashes)))
    if len(expected) != len(pids):
        return 'uncertain', {**evidence, 'reason':'duplicate_frozen_identity'}
    state = stored.get('state_dir') or stored.get('run_path')
    if not isinstance(state, str):
        return 'uncertain', {**evidence, 'reason':'missing_owned_state_root'}
    state = Path(state).resolve()
    vault = Path(vault).resolve()
    partial = False
    for path in sorted((vault/'governance/curations').glob('*.json')):
        doc = _load_json_file(path)
        if not isinstance(doc, dict) or not isinstance(doc.get('proposals'), list):
            return 'uncertain', {**evidence, 'reason':'unreadable_curation_ledger'}
        matched = {x.get('proposal_id'):x for x in doc['proposals'] if isinstance(x,dict) and x.get('proposal_id') in expected}
        if not matched:
            continue
        partial = True
        if set(matched) != set(expected):
            continue
        if any((matched[pid].get('path'),matched[pid].get('sha256')) != expected[pid] for pid in expected):
            continue
        eid, tid = doc.get('event_id'), doc.get('transaction')
        if not isinstance(eid,str) or not re.fullmatch(r'evt-[A-Za-z0-9-]+',eid):
            continue
        if not isinstance(tid,str) or not re.fullmatch(r'tx-[a-f0-9]+',tid):
            continue
        journals = list(state.glob('curation/*/candidate*/.curation-transactions/'+tid+'.json'))
        journals += list(state.glob('candidate*/.curation-transactions/'+tid+'.json'))
        for jp in journals:
            try:
                jp = safe_file(state,jp.relative_to(state).as_posix())
                journal = _load_json_file(jp)
                if not journal or journal.get('status')!='committed' or journal.get('event_id')!=eid or Path(journal.get('vault','')).resolve()!=vault:
                    continue
                manifest_path = safe_file(state,Path(journal['manifest']).relative_to(state).as_posix())
                manifest_bytes = manifest_path.read_bytes()
                digest = sha(manifest_bytes)
                manifest = json.loads(manifest_bytes)
                if digest != journal.get('manifest_sha256') or digest != doc.get('manifest_sha256') or set(manifest.get('proposal_ids',[]))!=set(pids):
                    continue
                review = doc.get('review') or {}
                if review.get('verdict')!='PASS' or review.get('manifest_sha256')!=digest:
                    continue
                events = list((vault/'events').glob('*/*/'+eid+'.json'))
                if len(events)!=1:
                    continue
                event_path=safe_file(vault,events[0].relative_to(vault).as_posix())
                ledger_path=safe_file(vault,path.relative_to(vault).as_posix())
                event = _load_json_file(event_path)
                if not event or event.get('action')!='record-curation' or event.get('event_id')!=eid or set(event.get('proposal_ids',[]))!=set(pids):
                    continue
                outputs = {x['path']:x['after'] for x in journal['files']}
                if any(outputs.get(p.relative_to(vault).as_posix())!=sha(p.read_bytes()) for p in (event_path,ledger_path)):
                    continue
                return 'committed', {**evidence,'journal_path':str(jp),'event_id':eid,'manifest_sha256':digest,'immutable_output_hashes_verified':True}
            except (OSError,ValueError,KeyError,TypeError,PublicationError):
                continue
    return ('uncertain' if partial else 'absent'), {**evidence,'reason':'no_complete_committed_proof'}


def reconcile_all_running(conn: sqlite3.Connection, vault: Path) -> List[Dict[str, Any]]:
    """Reconcile every RUNNING row against canonical evidence; never republish blindly."""
    ensure_pub_tables(conn)
    rows = conn.execute("SELECT batch FROM publications WHERE status='RUNNING'").fetchall()
    out: List[Dict[str, Any]] = []
    for row in rows:
        key = tuple(row)[0]
        data = read_row(conn, key)
        verdict, evidence = canonical_verdict(vault, data)
        result = reconcile_running(conn, key, verdict, evidence)
        result["batch"] = key
        result["verdict"] = verdict
        out.append(result)
    return out


def record_budget_deferred(conn, key, current_fingerprint):
    """A cooperative stop before publication is capacity, not model failure."""
    row = read_row(conn, key)
    value = row.get('result') if row else None
    stored = json.loads(value) if isinstance(value, str) else dict(value or {})
    stored.update(status='DEFERRED_BUDGET', reason='tick_time_budget_exhausted')
    with conn:
        conn.execute("UPDATE publications SET status=?,result=?,category=?,next_due='',fingerprint=?,updated_at=? WHERE batch=?",
                     ('DEFERRED_BUDGET', json.dumps(stored, ensure_ascii=False), 'capacity', current_fingerprint, _now_iso(), key))
    return stored


def record_prepared_deferred(conn, key, current_fingerprint, result):
    """Keep a verified pre-effect candidate due without charging a failure.

    The phase cache is the authority for whether this result can resume. This
    row only keeps the normal publication batch eligible on the next tick; it
    must not turn a prepared candidate into a failed/quarantined audit member.
    """
    ensure_pub_tables(conn)
    row = read_row(conn, key)
    if row is None or row.get("status") != "RUNNING":
        raise ValueError("prepared_deferral_requires_running_batch")
    stored = dict(result)
    stored.update(status="PREPARED_DEFERRED", pre_effect=True)
    with conn:
        conn.execute(
            "UPDATE publications SET status=?, result=?, next_due=?, category=?, "
            "fingerprint=?, updated_at=? WHERE batch=?",
            ("PREPARED_DEFERRED", json.dumps(stored, ensure_ascii=False), "",
             "prepared", current_fingerprint, _now_iso(), key),
        )
    return stored
