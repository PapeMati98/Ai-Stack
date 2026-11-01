"""Recurring production helpdesk workflow (read-only findings in).

Daily schedule: Europe/Warsaw 17:00, one run per project + local date +
schedule generation. Findings for example-crm and example-workspace prod are
read-only inputs: bounded sanitized summaries only. Dedup on
(stable_id, updated revision); quiet when nothing is new. New findings
create a proposed bounded repair request plus a notification outbox entry in
the same transaction as the daily run marker; no repair dispatch happens here
-- exact authenticated owner approval is required first (see approvals.py).
"""

from __future__ import annotations

import calendar
import hashlib
import json
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from openclaw_ops import models, sanitize
from openclaw_ops.notify import Outbox

try:
    from zoneinfo import ZoneInfo
    _WARSAW = ZoneInfo("Europe/Warsaw")
except Exception:  # pragma: no cover - tzdata missing, use EU rules below
    _WARSAW = None


# -- Warsaw local time (zoneinfo primary, embedded EU rules fallback) ----

def _last_sunday(year: int, month: int) -> int:
    last = calendar.monthrange(year, month)[1]
    dow = calendar.weekday(year, month, last)  # Monday=0
    return last - ((dow + 1) % 7)


def _eu_warsaw_offset_seconds(dt_utc: datetime) -> int:
    """EU DST rules: CEST (+2) from last Sun Mar 01:00 UTC to last Sun Oct."""
    y = dt_utc.year
    start = datetime(y, 3, _last_sunday(y, 3), 1, tzinfo=timezone.utc)
    end = datetime(y, 10, _last_sunday(y, 10), 1, tzinfo=timezone.utc)
    return 7200 if start <= dt_utc < end else 3600


def warsaw_local(ts: float) -> datetime:
    dt_utc = datetime.fromtimestamp(ts, tz=timezone.utc)
    if _WARSAW is not None:
        return dt_utc.astimezone(_WARSAW)
    # Keep the fallback result timezone-aware.  Adding a timedelta to a UTC
    # datetime changes the clock display but leaves its tzinfo at UTC, which
    # makes ``utcoffset()`` incorrectly report zero.
    offset = _eu_warsaw_offset_seconds(dt_utc)
    local_zone = timezone(timedelta(seconds=offset), name="Europe/Warsaw")
    return dt_utc.astimezone(local_zone)


def warsaw_offset_seconds(ts: float) -> int:
    loc = warsaw_local(ts)
    off = loc.utcoffset()
    if off is not None:
        return int(off.total_seconds())
    return _eu_warsaw_offset_seconds(
        datetime.fromtimestamp(ts, tz=timezone.utc))


# -- schedule ------------------------------------------------------------

def schedule_key(project: str, local_date: str, generation: int) -> str:
    """Unique run key: project + Warsaw local date + schedule generation."""
    return f"{project}:{local_date}:{generation}"


def is_due(now_wall: float) -> bool:
    """Due once the Warsaw wall clock reaches 17:00 on the current date."""
    loc = warsaw_local(now_wall)
    return (loc.hour, loc.minute) >= (models.HELPDESK_HOUR_WARSAW, 0)


# -- findings ------------------------------------------------------------

@dataclass
class Finding:
    project: str
    stable_id: str
    revision: str
    kind: str  # 'public' | 'internal' terminal summaries only
    summary: str
    proposed_scope: str = "candidate-code"


def scope_hash(scope: str) -> str:
    return hashlib.sha256(f"scope:{scope}".encode("utf-8")).hexdigest()


# -- issue revision digest (collection boundary, in-memory only) -----------

def normalize_text(value) -> str:
    """Deterministic NFC + strip normalisation for digest inputs."""
    if value is None:
        return ""
    return unicodedata.normalize("NFC", str(value)).strip()


def revision_digest(*, title: str = "", body: str = "", status: str = "",
                    severity: str = "", comments=None) -> str:
    """SHA256 over normalised title/body/status/severity/permitted comments.

    Computed IN MEMORY at the collection boundary; only the hex digest (or
    a short suffix of it) leaves this function.  Raw bodies and comments
    are never returned, stored or logged here.

    ``comments is None`` means comments were not collected: they are labelled
    unavailable and must not be hashed as a complete empty list.
    """
    payload = {
        "title": normalize_text(title),
        "body": normalize_text(body),
        "status": normalize_text(status),
        "severity": normalize_text(severity),
        "comments_coverage": (
            "hashed" if comments is not None else "unavailable"),
    }
    if comments is not None:
        payload["comments"] = [normalize_text(c) for c in comments]
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def derive_revision(updated_at, *, title: str = "", body: str = "",
                    status: str = "", severity: str = "",
                    comments=None) -> str:
    """Meaningful revision for one collected issue.

    With no content fields the source timestamp passes through unchanged
    (old behaviour).  When the collector supplies richer source fields they
    are folded into a short content suffix, so a material change with an
    unchanged ``updated_at`` still yields a new revision and a new proposal.
    Unavailable comments (``comments is None``) are not treated as complete.
    """
    base = "" if updated_at is None else str(updated_at)
    if not any([title, body, status, severity, comments]):
        return base
    digest = revision_digest(title=title, body=body, status=status,
                             severity=severity, comments=comments)
    return f"{base}#c={digest[:16]}"


class HelpdeskStore:
    def __init__(self, path: str) -> None:
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.isolation_level = None
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs(
              key TEXT PRIMARY KEY, finished_at REAL NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS sightings(
              project TEXT NOT NULL, stable_id TEXT NOT NULL,
              revision TEXT NOT NULL, summary_ref TEXT NOT NULL DEFAULT '',
              first_seen REAL NOT NULL DEFAULT 0,
              last_seen REAL NOT NULL DEFAULT 0,
              PRIMARY KEY(project, stable_id, revision));
            CREATE TABLE IF NOT EXISTS repair_requests(
              request_id TEXT PRIMARY KEY, project TEXT NOT NULL,
              stable_id TEXT NOT NULL, revision TEXT NOT NULL,
              scope TEXT NOT NULL, scope_hash TEXT NOT NULL,
              summary_ref TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL, created_at REAL NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS collector_state(
              project TEXT PRIMARY KEY,
              consecutive_failures INTEGER NOT NULL DEFAULT 0,
              last_status TEXT NOT NULL DEFAULT '',
              last_reason TEXT NOT NULL DEFAULT '',
              last_alarm_hash TEXT NOT NULL DEFAULT '',
              updated_at REAL NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS alarms(
              alarm_key TEXT PRIMARY KEY, project TEXT NOT NULL,
              reason TEXT NOT NULL, status TEXT NOT NULL,
              created_at REAL NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS run_claims(
              key TEXT PRIMARY KEY, claimed_at REAL NOT NULL DEFAULT 0);
            """
        )
        self.outbox = Outbox(self.conn)

    def close(self) -> None:
        self.conn.close()

    def has_run(self, key: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM runs WHERE key=?", (key,)).fetchone()
            is not None
        )

    def list_requests(self, status: Optional[str] = None) -> list[dict]:
        if status is None:
            rows = self.conn.execute(
                "SELECT * FROM repair_requests ORDER BY request_id")
        else:
            rows = self.conn.execute(
                "SELECT * FROM repair_requests WHERE status=? ORDER BY"
                " request_id", (status,))
        return [dict(r) for r in rows.fetchall()]


def process_day(store: HelpdeskStore, project: str, generation: int,
                findings: list[Finding], now: float, *,
                failure_injector: Optional[Callable[[str], None]] = None
                ) -> dict:
    """Run one daily helpdesk pass. Returns counts only, never raw bodies."""
    if project not in models.HELPDESK_PROJECTS:
        raise ValueError(f"unknown helpdesk project {project!r}")
    local_date = warsaw_local(now).date().isoformat()
    key = schedule_key(project, local_date, generation)
    if not is_due(now):
        return {"key": key, "action": "not-due", "new": 0,
                "repeated": 0, "forbidden": 0}
    new, repeated, forbidden = 0, 0, 0
    # A run marker is meaningful only when every sighting, repair request and
    # owner notification is durable.  Serialise competing daily workers and
    # make a failure roll back the entire run so a later pass can recover it.
    store.conn.execute("BEGIN IMMEDIATE")
    try:
        if store.has_run(key):
            store.conn.execute("COMMIT")
            return {"key": key, "action": "quiet-duplicate-run",
                    "new": 0, "repeated": 0, "forbidden": 0}
        for f in findings:
            if f.project != project:
                continue
            admitted = sanitize.admit_summary(f.summary, f.kind)
            digest = hashlib.sha256(admitted.encode("utf-8")).hexdigest()
            cur = store.conn.execute(
                "INSERT OR IGNORE INTO sightings(project, stable_id, revision,"
                " summary_ref, first_seen, last_seen)"
                " VALUES(?,?,?,?,?,?)",
                (project, f.stable_id, f.revision, digest, now, now),
            )
            if failure_injector is not None:
                failure_injector("after-sighting")
            if cur.rowcount == 0:
                store.conn.execute(
                    "UPDATE sightings SET last_seen=? WHERE project=?"
                    " AND stable_id=? AND revision=?",
                    (now, project, f.stable_id, f.revision),
                )
                repeated += 1
                continue
            request_id = f"RR-{project}-{f.stable_id}-{f.revision}"
            if f.proposed_scope in models.CANDIDATE_SCOPES:
                status = "proposed"
            else:
                # Deploy/IAM/deletion/customer mutation need a separate
                # explicit scope: recorded, never dispatched from here.
                status = "forbidden-needs-explicit-scope"
                forbidden += 1
            store.conn.execute(
                "INSERT OR IGNORE INTO repair_requests(request_id, project,"
                " stable_id, revision, scope, scope_hash, summary_ref, status,"
                " created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (request_id, project, f.stable_id, f.revision,
                 f.proposed_scope, scope_hash(f.proposed_scope), digest,
                 status, now),
            )
            if failure_injector is not None:
                failure_injector("after-request")
            if status == "proposed":
                store.outbox.enqueue(f"helpdesk:{request_id}", "owner",
                                    request_id, digest)
                if failure_injector is not None:
                    failure_injector("after-outbox")
                new += 1
        store.conn.execute(
            "INSERT OR IGNORE INTO runs(key, finished_at) VALUES(?,?)",
            (key, now),
        )
        if failure_injector is not None:
            failure_injector("before-commit")
        store.conn.execute("COMMIT")
    except BaseException:
        if store.conn.in_transaction:
            store.conn.execute("ROLLBACK")
        raise
    action = "quiet-no-new-items" if new == 0 and forbidden == 0 else "proposed"
    return {"key": key, "action": action, "new": new,
            "repeated": repeated, "forbidden": forbidden}


# -- missed-run / catch-up across reboot (bounded, T6b) --------------------

def business_date(ts: float) -> str:
    """Warsaw business date (ISO) for a wall-clock instant."""
    return warsaw_local(ts).date().isoformat()


def latest_due_date(now: float) -> str:
    """Latest Warsaw business date whose 17:00 run is already due.

    Today when the wall clock has reached 17:00, otherwise yesterday.
    DST transitions resolve through the same Warsaw clock as the daily run.
    """
    if is_due(now):
        return business_date(now)
    return (warsaw_local(now).date() - timedelta(days=1)).isoformat()


def missed_catchup_date(store: HelpdeskStore, project: str, generation: int,
                        now: float) -> Optional[str]:
    """Bounded single catch-up date, or None when nothing is owed.

    Only the latest due day is ever returned: a multi-day outage yields one
    catch-up, never a historical storm.  When ``now`` is already due, the
    normal daily path owns today and this returns None.
    """
    if project not in models.HELPDESK_PROJECTS:
        raise ValueError(f"unknown helpdesk project {project!r}")
    if is_due(now):
        return None
    due = latest_due_date(now)
    if store.has_run(schedule_key(project, due, generation)):
        return None
    return due


def _run_for_date(store: HelpdeskStore, project: str, generation: int,
                  findings: list[Finding], now: float, local_date: str, *,
                  failure_injector: Optional[Callable[[str], None]] = None
                  ) -> dict:
    """Transactional run body for one explicit business date.

    Same exactly-once marker contract as process_day
    (``project:date:generation``), shared by the daily pass and the bounded
    catch-up.  Returns counts only, never raw bodies.
    """
    key = schedule_key(project, local_date, generation)
    new, repeated, forbidden = 0, 0, 0
    # A run marker is meaningful only when every sighting, repair request and
    # owner notification is durable.  Serialise competing daily workers and
    # make a failure roll back the entire run so a later pass can recover it.
    store.conn.execute("BEGIN IMMEDIATE")
    try:
        if store.has_run(key):
            store.conn.execute("COMMIT")
            return {"key": key, "action": "quiet-duplicate-run",
                    "new": 0, "repeated": 0, "forbidden": 0,
                    "catchup": True, "catchup_date": local_date}
        for f in findings:
            if f.project != project:
                continue
            admitted = sanitize.admit_summary(f.summary, f.kind)
            digest = hashlib.sha256(admitted.encode("utf-8")).hexdigest()
            cur = store.conn.execute(
                "INSERT OR IGNORE INTO sightings(project, stable_id, revision,"
                " summary_ref, first_seen, last_seen)"
                " VALUES(?,?,?,?,?,?)",
                (project, f.stable_id, f.revision, digest, now, now),
            )
            if failure_injector is not None:
                failure_injector("after-sighting")
            if cur.rowcount == 0:
                store.conn.execute(
                    "UPDATE sightings SET last_seen=? WHERE project=?"
                    " AND stable_id=? AND revision=?",
                    (now, project, f.stable_id, f.revision),
                )
                repeated += 1
                continue
            request_id = f"RR-{project}-{f.stable_id}-{f.revision}"
            if f.proposed_scope in models.CANDIDATE_SCOPES:
                status = "proposed"
            else:
                # Deploy/IAM/deletion/customer mutation need a separate
                # explicit scope: recorded, never dispatched from here.
                status = "forbidden-needs-explicit-scope"
                forbidden += 1
            store.conn.execute(
                "INSERT OR IGNORE INTO repair_requests(request_id, project,"
                " stable_id, revision, scope, scope_hash, summary_ref, status,"
                " created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (request_id, project, f.stable_id, f.revision,
                 f.proposed_scope, scope_hash(f.proposed_scope), digest,
                 status, now),
            )
            if failure_injector is not None:
                failure_injector("after-request")
            if status == "proposed":
                store.outbox.enqueue(f"helpdesk:{request_id}", "owner",
                                    request_id, digest)
                if failure_injector is not None:
                    failure_injector("after-outbox")
                new += 1
        store.conn.execute(
            "INSERT OR IGNORE INTO runs(key, finished_at) VALUES(?,?)",
            (key, now),
        )
        if failure_injector is not None:
            failure_injector("before-commit")
        store.conn.execute("COMMIT")
    except BaseException:
        if store.conn.in_transaction:
            store.conn.execute("ROLLBACK")
        raise
    action = "quiet-no-new-items" if new == 0 and forbidden == 0 else "proposed"
    return {"key": key, "action": action, "new": new,
            "repeated": repeated, "forbidden": forbidden,
            "catchup": True, "catchup_date": local_date}


def claim_schedule(store: HelpdeskStore, key: str, now: float, *,
                   ttl: float = models.LEASE_TTL) -> bool:
    """Claim exclusive collect+run rights for one project/date/generation.

    Returns True if this caller owns the claim.  False if the run already
    finished or another unexpired claim is live -- callers must skip the
    source read.  Expired claims (authority-host ``now`` vs ``claimed_at``,
    TTL ``LEASE_TTL``) are stealable so a crashed collector can recover.
    A clock that jumps backwards beyond ``CLOCK_SKEW_LIMIT`` cannot steal a
    live claim.  The claim is not a successful run marker: release it on
    collector failure so the next tick can retry.
    """
    store.conn.execute("BEGIN IMMEDIATE")
    try:
        if store.has_run(key):
            store.conn.execute("COMMIT")
            return False
        row = store.conn.execute(
            "SELECT claimed_at FROM run_claims WHERE key=?", (key,)
        ).fetchone()
        if row is not None:
            claimed_at = float(row["claimed_at"])
            if now + models.CLOCK_SKEW_LIMIT < claimed_at:
                store.conn.execute("COMMIT")
                return False
            if now < claimed_at + float(ttl):
                store.conn.execute("COMMIT")
                return False
        store.conn.execute(
            "INSERT OR REPLACE INTO run_claims(key, claimed_at) VALUES(?,?)",
            (key, now),
        )
        store.conn.execute("COMMIT")
        return True
    except BaseException:
        if store.conn.in_transaction:
            store.conn.execute("ROLLBACK")
        raise


def release_schedule_claim(store: HelpdeskStore, key: str) -> None:
    """Drop a collect claim so a later tick can recover a failed source."""
    store.conn.execute("DELETE FROM run_claims WHERE key=?", (key,))


def process_catchup(store: HelpdeskStore, project: str, generation: int,
                    findings: list[Finding], now: float, *,
                    failure_injector: Optional[Callable[[str], None]] = None
                    ) -> dict:
    """Run at most one catch-up for the latest missed due day.

    Intended for ticks before 17:00 after a reboot/outage.  Exactly one run
    per project/date/generation via the same durable marker as the daily
    pass; repeated calls stay quiet (``nothing-missed``).  A BLOCKED source
    must never reach this function: only validated findings are accepted, so
    no empty successful run is ever persisted for an unavailable source.
    """
    if project not in models.HELPDESK_PROJECTS:
        raise ValueError(f"unknown helpdesk project {project!r}")
    if is_due(now):
        key = schedule_key(project, business_date(now), generation)
        return {"key": key, "action": "deferred-to-daily", "new": 0,
                "repeated": 0, "forbidden": 0,
                "catchup": False, "catchup_date": None}
    due = missed_catchup_date(store, project, generation, now)
    if due is None:
        key = schedule_key(project, latest_due_date(now), generation)
        return {"key": key, "action": "nothing-missed", "new": 0,
                "repeated": 0, "forbidden": 0,
                "catchup": True, "catchup_date": None}
    return _run_for_date(store, project, generation, findings, now, due,
                         failure_injector=failure_injector)


# -- failed-collector retry + durable state-change alarms ------------------

COLLECTOR_MAX_RETRIES = 3


def read_with_retry(reader: Callable[[], dict], *,
                    attempts: int = COLLECTOR_MAX_RETRIES) -> dict:
    """Call a collector with a bounded retry budget.

    Only a RAISING reader is retried (transient failure); a returned BLOCKED
    result is final and never retried, so an unauthorised source cannot cause
    a retry storm.  Exhaustion re-raises the last exception for the caller to
    record as BLOCKED.
    """
    last_exc: Optional[BaseException] = None
    for _ in range(max(1, int(attempts))):
        try:
            return reader()
        except Exception as exc:
            last_exc = exc
    assert last_exc is not None
    raise last_exc


def record_collector_outcome(store: HelpdeskStore, project: str, *,
                             ok: bool, reason: str = "", now: float = 0.0,
                             audit_hook=None) -> dict:
    """Durable collector health with exactly-once state-change alarms.

    Emits an alarm dict only on the transition into a NEW failure reason; a
    repeated identical failure and a steady OK stay quiet, so reboots and
    reticks never repeat an unchanged notice.  Alarms live in the dedicated
    ``alarms`` table (never the owner outbox): one row per project+reason,
    durable across restarts.  ``audit_hook``, when given, receives only
    non-empty alarm payloads -- never an empty success.
    """
    if project not in models.HELPDESK_PROJECTS:
        raise ValueError(f"unknown helpdesk project {project!r}")
    owns_transaction = not store.conn.in_transaction
    alarm = None
    if owns_transaction:
        store.conn.execute("BEGIN IMMEDIATE")
    try:
        row = store.conn.execute(
            "SELECT * FROM collector_state WHERE project=?", (project,)
        ).fetchone()
        prev = dict(row) if row is not None else None
        if ok:
            store.conn.execute(
                "INSERT OR REPLACE INTO collector_state(project,"
                " consecutive_failures, last_status, last_reason,"
                " last_alarm_hash, updated_at)"
                " VALUES(?,?,?,?,?,?)",
                (project, 0, "OK", "",
                 prev["last_alarm_hash"] if prev else "", now),
            )
        else:
            failures = int(prev["consecutive_failures"]) + 1 if prev else 1
            alarm_hash = hashlib.sha256(
                f"alarm:{project}:{reason}".encode("utf-8")).hexdigest()
            if not (prev and prev["last_status"] == "BLOCKED"
                    and prev["last_reason"] == reason):
                alarm_key = f"alarm:{project}:{alarm_hash[:16]}"
                cur = store.conn.execute(
                    "INSERT OR IGNORE INTO alarms(alarm_key, project, reason,"
                    " status, created_at) VALUES(?,?,?,?,?)",
                    (alarm_key, project, reason, "BLOCKED", now),
                )
                if cur.rowcount == 1:
                    alarm = {"alarm_key": alarm_key, "project": project,
                             "reason": reason, "status": "BLOCKED"}
            store.conn.execute(
                "INSERT OR REPLACE INTO collector_state(project,"
                " consecutive_failures, last_status, last_reason,"
                " last_alarm_hash, updated_at)"
                " VALUES(?,?,?,?,?,?)",
                (project, failures, "BLOCKED", reason, alarm_hash, now),
            )
        if owns_transaction:
            store.conn.execute("COMMIT")
    except BaseException:
        if owns_transaction and store.conn.in_transaction:
            store.conn.execute("ROLLBACK")
        raise
    if alarm is not None and audit_hook is not None:
        audit_hook({"kind": "collector-alarm", **alarm})
    return {"project": project, "ok": ok, "reason": reason, "alarm": alarm}


def get_collector_state(store: HelpdeskStore, project: str) -> Optional[dict]:
    """Read-only view of one collector's durable health (no writes)."""
    row = store.conn.execute(
        "SELECT project, consecutive_failures, last_status, last_reason,"
        " updated_at FROM collector_state WHERE project=?",
        (project,)).fetchone()
    return dict(row) if row is not None else None
