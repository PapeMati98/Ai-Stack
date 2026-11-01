"""Deterministic synthetic controller. No network, shell, tools or model fallback.

Store is the budget/lease authority. The private journal retains queued inputs
and interpretations; an interrupted inference keeps its full cost reservation.
An attempted effect is marked unknown BEFORE crossing the adapter boundary.
"""

from contextlib import closing
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

from .adapters import (
    MODEL_RESPONSE_COST_CODES,
    MODEL_RESPONSE_FAILURE_CODES,
    MODEL_RESPONSE_USAGE_CODES,
    ModelRateLimited,
    ModelTransportRejected,
    ModelResponseRejected,
    validate_decision,
    _SourceFailed,
    _SourceInconclusive,
    _live_gate,
)
from . import paced_transport
from .store import allowed_orgs, validate_source_org_map

OWNER_REPAIR_APPROVAL_REFERENCE: str | None = None

REASONS = frozenset({"noise", "relevant", "invoice", "needs-owner", "uncertain",
                     "duplicate", "missing-data", "invalid-data", "ok"})
SAFE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_PRIVATE_ARTIFACT_RE = re.compile(r"private-artifact:[0-9a-f]{64}\Z")
_INVOICE_SOURCE_FIELDS = frozenset({"number", "date", "amount", "currency"})
_INVOICE_REQUIRED_FIELDS = ("number", "date", "amount", "currency")
_SAFE_ENGINE_ERROR_CODES = MODEL_RESPONSE_FAILURE_CODES | frozenset({"cost_unknown", "interpretation_unknown", "preflight_rejected", "effect_lookup_limit", "TRANSPORT_TIMEOUT_UNKNOWN", "TRANSPORT_FAILED_UNKNOWN", "TRANSPORT_REJECTED_HTTP", "transport_reconciliation_changed"})


class _BatchRateLimited(Exception):
    """Stop this tick after a verified rate limit; no provider text retained."""


class _BatchDeferredBeforeHttp(Exception):
    """Stop this tick when the local pacer proves no HTTP request was issued."""


def identity(value):
    if not isinstance(value, str) or not SAFE.fullmatch(value):
        raise ValueError("invalid_identity")
    return value


def timestamp(value):
    if type(value) not in (int, float):
        raise ValueError("invalid_timestamp")
    try:
        if not math.isfinite(value):
            raise ValueError("invalid_timestamp")
        return float(value)
    except OverflowError:
        raise ValueError("invalid_timestamp") from None


def integer(value, minimum=0, maximum=(1 << 63) - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("invalid_integer")
    return value


def encoded(value):
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False,
                          allow_nan=False, separators=(",", ":"))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ValueError("invalid_json") from None


def digest(prefix, value):
    return prefix + hashlib.sha256(encoded(value).encode("utf-8")).hexdigest()[:48]


def template_config(kind, org, project, budget_micro_usd=2000000, **options):
    if kind not in ("observer", "collector", "supervisor"):
        raise ValueError("invalid_kind")
    config = {"kind": kind, "org": identity(org), "project": identity(project),
              "budget_micro_usd": integer(budget_micro_usd), "model": "synthetic",
              "interval_seconds": 900, "grant_version": 1, "allowed_peers": [],
              "allowed_actions": {"observer": ["alert", "needs_owner"],
                                  "collector": ["collect", "needs_owner"],
                                  "supervisor": ["needs_owner"]}[kind],
              "max_queue": 100, "max_payload_bytes": 16384,
              "max_call_micro_usd": 10, "max_attempts": 3,
              "max_state_records": 1000, "timezone": "UTC", "schedule": "interval"}
    config.update(options)
    validate_runtime(config)
    validate_source_org_map(config)
    return config


def validate_runtime(config):
    if config.get("timezone", "UTC") != "UTC" or config.get("schedule", "interval") != "interval":
        raise ValueError("unsupported_schedule")
    if config.get("model", "synthetic") != "synthetic":
        _live_gate(config)
    integer(config.get("max_call_micro_usd", 10), 1)
    integer(config.get("max_attempts", 3), 1, 3)
    integer(config.get("max_state_records", 1000), 1, 10000)
    if timestamp(config.get("interval_seconds", 900)) <= 0:
        raise ValueError("invalid_interval")


def invoice_fields(value, org):
    """Synthetic draft validation; never a tax/final-invoice approval."""
    try:
        if (not isinstance(value, dict)
                or set(value) - ({"org"} | _INVOICE_SOURCE_FIELDS)
                or not set(("org",) + _INVOICE_REQUIRED_FIELDS) <= set(value)):
            raise ValueError
        if value["org"] != org:
            raise ValueError
        number = value["number"]
        if not isinstance(number, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/_.-]{0,63}", number):
            raise ValueError
        day = value["date"]
        if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
            raise ValueError
        amount = Decimal(str(value["amount"]))
        if (type(value["amount"]) is bool or not amount.is_finite() or amount <= 0
                or amount > Decimal("1000000000") or amount.as_tuple().exponent < -2):
            raise ValueError
        if value["currency"] not in ("PLN", "EUR", "USD", "GBP", "CHF", "CZK"):
            raise ValueError
        result = {key: value[key] for key in ("org",) + _INVOICE_REQUIRED_FIELDS}
        result["amount"] = format(amount.quantize(Decimal("0.01")), "f")
        return result
    except (ValueError, TypeError, KeyError, InvalidOperation, OverflowError):
        raise ValueError("invalid_invoice") from None


def private_invoice_evidence(item, expected_org):
    """Validate source-extracted partial invoice data and opaque archive handles."""
    if not isinstance(item, dict):
        raise ValueError("invalid_invoice_evidence")
    source_invoice = item.get("invoice")
    if source_invoice is not None:
        if (not isinstance(source_invoice, dict)
                or set(source_invoice) - ({"org"} | _INVOICE_SOURCE_FIELDS)
                or source_invoice.get("org", expected_org) != expected_org):
            raise ValueError("invalid_invoice_evidence")
        invoice = {"org": expected_org}
        for key in _INVOICE_SOURCE_FIELDS:
            value = source_invoice.get(key)
            if value is not None and (not isinstance(value, str) or len(value) > 256
                                      or any(ord(char) < 32 for char in value)):
                raise ValueError("invalid_invoice_evidence")
            invoice[key] = value
    else:
        invoice = {"org": expected_org, **{key: None for key in _INVOICE_SOURCE_FIELDS}}
    attachments = item.get("attachments", [])
    if not isinstance(attachments, list) or len(attachments) > 10:
        raise ValueError("invalid_invoice_evidence")
    safe_attachments = []
    seen = set()
    allowed_mime = {
        "application/pdf", "application/xml", "text/csv", "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "image/jpeg", "image/png",
    }
    for attachment in attachments:
        if (not isinstance(attachment, dict) or set(attachment) != {"handle", "mime_type"}
                or not isinstance(attachment.get("handle"), str)
                or not _PRIVATE_ARTIFACT_RE.fullmatch(attachment["handle"])
                or attachment.get("mime_type") not in allowed_mime
                or attachment["handle"] in seen):
            raise ValueError("invalid_invoice_evidence")
        seen.add(attachment["handle"])
        safe_attachments.append(dict(attachment))
    if not isinstance(item.get("content"), str) or type(item.get("content_truncated")) is not bool:
        raise ValueError("invalid_invoice_evidence")
    if len(item["content"].encode("utf-8")) > 4096:
        raise ValueError("invalid_invoice_evidence")
    created_ts = item.get("created_ts")
    if (not isinstance(created_ts, str) or len(created_ts) > 40
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", created_ts)):
        raise ValueError("invalid_invoice_evidence")
    try:
        date_part, time_part = created_ts[:-1].split("T", 1)
        date.fromisoformat(date_part)
        # Strict lexical shape above; datetime validates clock ranges.
        from datetime import datetime
        datetime.fromisoformat(date_part + "T" + time_part)
    except ValueError:
        raise ValueError("invalid_invoice_evidence") from None
    missing = [key for key in _INVOICE_REQUIRED_FIELDS if not invoice.get(key)]
    if invoice.get("amount"):
        try:
            amount = Decimal(invoice["amount"].replace(",", "."))
            if not amount.is_finite() or amount <= 0:
                missing.append("amount")
        except (InvalidOperation, ValueError):
            missing.append("amount")
    if invoice.get("date"):
        try:
            if date.fromisoformat(invoice["date"]).isoformat() != invoice["date"]:
                missing.append("date")
        except ValueError:
            missing.append("date")
    if invoice.get("currency") and invoice["currency"] not in ("PLN", "EUR", "USD", "GBP", "CHF", "CZK"):
        missing.append("currency")
    if invoice.get("number") and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/_.-]{0,63}", invoice["number"]):
        missing.append("number")
    return {
        "invoice": invoice,
        "attachments": safe_attachments,
        "missing_fields": sorted(set(missing)),
        "created_ts": created_ts,
        "content": item["content"],
        "content_truncated": item["content_truncated"],
    }


def private_approval_payload(item, expected_org, dot_id, project, reason_code):
    evidence = private_invoice_evidence(item, expected_org)
    return {
        "org": expected_org,
        "project": project,
        "dot_id": dot_id,
        "source_alias": item["source_alias"],
        "source_ref": identity(item.get("id")),
        "created_ts": evidence["created_ts"],
        "content": evidence["content"],
        "content_truncated": evidence["content_truncated"],
        "invoice": evidence["invoice"],
        "attachments": evidence["attachments"],
        "missing_fields": evidence["missing_fields"],
        "queue_status": "pending_approval",
        "reason_code": reason_code,
    }


class Journal:
    _INIT_TIMEOUT_SECONDS = 10.0

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self._INIT_TIMEOUT_SECONDS
        while True:
            try:
                with closing(self.connect()) as conn, conn:
                    # Retry initialization on fresh connections, without retaining
                    # a read lock while another constructor switches to WAL.
                    # The deadline belongs only to schema setup, never effects.
                    conn.execute("PRAGMA busy_timeout=0")
                    if conn.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                        conn.execute("PRAGMA journal_mode=WAL").fetchone()
                    conn.execute("""CREATE TABLE IF NOT EXISTS jobs (
                        dot_id TEXT NOT NULL, item_key TEXT NOT NULL, item_json TEXT NOT NULL,
                        state TEXT NOT NULL DEFAULT 'ready', attempts INTEGER NOT NULL DEFAULT 0,
                        deliveries INTEGER NOT NULL DEFAULT 0, grant_version INTEGER, effect_id TEXT,
                        reservation TEXT, decision_json TEXT, actual INTEGER,
                        reason TEXT NOT NULL DEFAULT 'queued', usage_input_tokens INTEGER,
                        usage_output_tokens INTEGER, PRIMARY KEY(dot_id,item_key))""")
                    job_columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
                    if "usage_input_tokens" not in job_columns:
                        conn.execute("ALTER TABLE jobs ADD COLUMN usage_input_tokens INTEGER")
                    if "usage_output_tokens" not in job_columns:
                        conn.execute("ALTER TABLE jobs ADD COLUMN usage_output_tokens INTEGER")
                    conn.execute("CREATE TABLE IF NOT EXISTS invoice_claims (identity TEXT PRIMARY KEY, invoice_json TEXT NOT NULL)")
                return
            except sqlite3.OperationalError as error:
                code = getattr(error, "sqlite_errorcode", 0) & 0xff
                remaining = deadline - time.monotonic()
                if code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or remaining <= 0:
                    raise
                time.sleep(min(0.05, remaining))

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def add(self, dot_id, key, item, config):
        blob = encoded(item)
        if len(blob.encode("utf-8")) > config["max_payload_bytes"]:
            raise ValueError("payload_limit")
        with closing(self.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT item_json FROM jobs WHERE dot_id=? AND item_key=?", (dot_id, key)
            ).fetchone()
            if existing:
                if existing["item_json"] != blob:
                    raise ValueError("source_event_conflict")
                return False
            count = conn.execute("SELECT COUNT(*) FROM jobs WHERE dot_id=?", (dot_id,)).fetchone()[0]
            pending = conn.execute("SELECT COUNT(*) FROM jobs WHERE dot_id=? AND state!='done'", (dot_id,)).fetchone()[0]
            if count >= config.get("max_state_records", 1000) or pending >= config["max_queue"]:
                raise ValueError("journal_limit")
            conn.execute("INSERT INTO jobs(dot_id,item_key,item_json) VALUES(?,?,?)", (dot_id, key, blob))
            return True

    def jobs(self, dot_id, pending=True):
        with closing(self.connect()) as conn:
            query = "SELECT * FROM jobs WHERE dot_id=?"
            if pending:
                query += " AND state!='done'"
            return [dict(row) for row in conn.execute(query + " ORDER BY rowid", (dot_id,))]

    def update(self, job, **fields):
        allowed = {"state", "attempts", "deliveries", "grant_version", "effect_id",
                   "reservation", "decision_json", "actual", "reason",
                   "usage_input_tokens", "usage_output_tokens"}
        if not fields or set(fields) - allowed:
            raise ValueError("invalid_journal_update")
        with closing(self.connect()) as conn, conn:
            conn.execute("UPDATE jobs SET " + ",".join(name + "=?" for name in fields)
                         + " WHERE dot_id=? AND item_key=?",
                         [*fields.values(), job["dot_id"], job["item_key"]])
        job.update(fields)

    def preserve_model_attempt(self, job):
        """Private immutable evidence of a failed classification, never an effect."""
        blob = encoded(job)
        with closing(self.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('''CREATE TABLE IF NOT EXISTS model_attempt_history (
                reservation TEXT PRIMARY KEY, dot_id TEXT NOT NULL,
                item_key TEXT NOT NULL, prior_job_json TEXT NOT NULL)''')
            previous = conn.execute('SELECT prior_job_json FROM model_attempt_history WHERE reservation=?', (job['reservation'],)).fetchone()
            if previous:
                if previous[0] != blob:
                    raise ValueError('model_attempt_history_conflict')
                return
            if conn.execute('SELECT COUNT(*) FROM model_attempt_history').fetchone()[0] >= 10000:
                raise ValueError('model_attempt_history_limit')
            conn.execute('INSERT INTO model_attempt_history VALUES(?,?,?,?)', (job['reservation'], job['dot_id'], job['item_key'], blob))

    def safe_errors(self, dot_id=None):
        """Aggregate allowlisted blocked model failures without loading job inputs."""
        codes = sorted(_SAFE_ENGINE_ERROR_CODES)
        marks = ",".join("?" for _ in codes)
        query = ("SELECT reason,actual,usage_input_tokens,usage_output_tokens FROM jobs "
                 "WHERE state='blocked' AND reason IN (%s)" % marks)
        params = list(codes)
        if dot_id is not None:
            query += " AND dot_id=?"
            params.append(identity(dot_id))
        with closing(self.connect()) as conn:
            rows = conn.execute(query, params).fetchall()
        totals = {}
        for row in rows:
            code = row["reason"]
            result = totals.setdefault(code, {
                "count": 0,
                "verified_cost_calls": 0,
                "verified_cost_micro_usd": 0,
                "verified_usage_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
            })
            result["count"] += 1
            if row["actual"] is not None:
                result["verified_cost_calls"] += 1
                result["verified_cost_micro_usd"] += row["actual"]
            if row["usage_input_tokens"] is not None and row["usage_output_tokens"] is not None:
                result["verified_usage_calls"] += 1
                result["input_tokens"] += row["usage_input_tokens"]
                result["output_tokens"] += row["usage_output_tokens"]
        return {"by_code": totals}

    def claim_invoice(self, invoice):
        key = digest("invoice-", [invoice["org"], invoice["number"], invoice["date"]])
        blob = encoded(invoice)
        with closing(self.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT invoice_json FROM invoice_claims WHERE identity=?", (key,)).fetchone()
            if row is not None and row[0] != blob:
                raise ValueError("ambiguous_invoice")
            conn.execute("INSERT OR IGNORE INTO invoice_claims VALUES(?,?)", (key, blob))


class _LeaseHeartbeat:
    """Keep one controller lease alive during bounded synchronous adapters."""

    def __init__(self, store, dot_id, owner, token, ttl, clock):
        self.store, self.dot_id, self.owner = store, dot_id, owner
        self.token, self.ttl, self.clock = token, ttl, clock
        self.interval = max(0.001, min(float(ttl) / 3, 5.0))
        self.stop_event = threading.Event()
        self.lost = threading.Event()
        self.thread = threading.Thread(target=self._run, name="dots-lease-" + dot_id, daemon=True)

    def start(self):
        self.thread.start()

    def _run(self):
        while not self.stop_event.wait(self.interval):
            try:
                self.store.renew(self.dot_id, self.owner, self.token,
                                 timestamp(self.clock()), self.ttl)
            except Exception:
                self.lost.set()
                return

    def stop(self):
        self.stop_event.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=11.0)


class Engine:
    def __init__(self, store, source, model, target, office, *, clock=time.time,
                 owner=None, lease_seconds=60, journal_path=None, max_model_calls_per_dot=None):
        self.store, self.source, self.model = store, source, model
        self.target, self.office, self.clock = target, office, clock
        self.owner = identity(owner or ("worker-" + uuid.uuid4().hex))
        self.lease_seconds = timestamp(lease_seconds)
        if self.lease_seconds <= 0:
            raise ValueError("invalid_lease_ttl")
        self.journal = Journal(journal_path or store.path.with_name("journal.sqlite3"))
        self.max_model_calls_per_dot = (None if max_model_calls_per_dot is None
                                       else integer(max_model_calls_per_dot, 1, 8))
        self._lease_heartbeats = {}
        self._lease_heartbeat_lock = threading.Lock()

    def _guard(self, dot_id, snapshot, token, action=None):
        with self._lease_heartbeat_lock:
            heartbeat = self._lease_heartbeats.get((dot_id, token))
        if heartbeat is not None and heartbeat.lost.is_set():
            raise PermissionError("invalid_lease")
        self.store.assert_lease(dot_id, self.owner, token, timestamp(self.clock()))
        current = self.store.get_dot(dot_id)
        if current["grant_version"] != snapshot["grant_version"] or current["config"] != snapshot["config"]:
            raise PermissionError("grant_changed")
        if action is not None and action not in current["config"]["allowed_actions"]:
            raise PermissionError("action_denied")

    def _source(self, dot_id):
        return self.source[dot_id] if isinstance(self.source, dict) else self.source

    def _model(self, dot_id):
        return self.model[dot_id] if isinstance(self.model, dict) else self.model

    def _target(self, dot_id):
        return self.target[dot_id] if isinstance(self.target, dict) else self.target

    def tick(self, dot_id=None, *, force=False):
        ids = [identity(dot_id)] if dot_id else [dot["id"] for dot in self.store.list_dots()]
        return [self._tick_one(key, force) for key in ids]

    def _tick_one(self, dot_id, force):
        now = timestamp(self.clock())
        dot = self.store.get_dot(dot_id)
        config = dot["config"]
        result = {"dot_id": dot_id, "outcome": "paused", "model_calls": 0,
                  "new_items": 0, "pending": 0}
        if dot["paused"]:
            return result
        if not force and dot["last_attempt"] is not None and now < dot["last_attempt"] + config["interval_seconds"]:
            result["outcome"] = "not_due"
            return result
        token = self.store.claim(dot_id, self.owner, now, self.lease_seconds)
        if token is None:
            result["outcome"] = "leased"
            return result
        heartbeat = _LeaseHeartbeat(self.store, dot_id, self.owner, token,
                                    self.lease_seconds, self.clock)
        with self._lease_heartbeat_lock:
            self._lease_heartbeats[(dot_id, token)] = heartbeat
        status = "inconclusive"
        try:
            heartbeat.start()
            self._guard(dot_id, dot, token)
            validate_runtime(config)
            self.store.update_coverage(dot_id, "pending", now)
            if config["kind"] == "supervisor":
                raise PermissionError("supervisor_unqualified")
            source = self._source(dot_id)
            # Accepted source bounds items, but reads the whole file: guard its
            # file size here as well. Other source transports need qualification.
            if hasattr(source, "path") and source.path.stat().st_size > min(1048576, config["max_queue"] * config["max_payload_bytes"] + 4096):
                raise _SourceInconclusive("SOURCE_INCONCLUSIVE")
            items = source.poll(config)
            if not isinstance(items, list) or len(items) > config["max_queue"]:
                raise _SourceInconclusive("SOURCE_INCONCLUSIVE")
            mapped_orgs = validate_source_org_map(config)
            for item in items:
                if not isinstance(item, dict):
                    raise _SourceInconclusive("SOURCE_INCONCLUSIVE")
                if mapped_orgs:
                    expected_org = mapped_orgs.get(item.get("source_alias"))
                    if expected_org is None or item.get("org") != expected_org:
                        raise PermissionError("source_scope")
                elif (item.get("source_alias") is not None
                      or item.get("org") != config["org"]):
                    raise PermissionError("source_scope")
                identity(item.get("id"))
                if mapped_orgs:
                    key = digest("i-", {name: item.get(name)
                                         for name in ("source_alias", "id", "org")})
                    if self.journal.add(dot_id, key, item, config):
                        result["new_items"] += 1
                else:
                    key = digest("i-", {name: item.get(name)
                                         for name in ("id", "org", "content", "invoice")})
                    if not self.store.seen(dot_id, key):
                        self.journal.add(dot_id, key, item, config)
                        result["new_items"] += 1
            # Reuse the supervisor's outer run key so all DOTS in one scheduler
            # tick share the transport cap; direct calls fall back to this lease.
            paced_run_id = paced_transport.current_run_id() or f"{dot_id}:{token}"
            with paced_transport.run_scope(paced_run_id):
                for job in self.journal.jobs(dot_id):
                    if (job['state'] in ('ready', 'calling')
                            and self.max_model_calls_per_dot is not None
                            and result['model_calls'] >= self.max_model_calls_per_dot):
                        continue
                    self._guard(dot_id, dot, token)
                    try:
                        result["model_calls"] += self._interpret(job, dot, token)
                    except _BatchRateLimited:
                        # The failed request is accounted as an attempted call,
                        # while its reservation remains held for explicit cost
                        # reconciliation. Do not spend the rest of this batch.
                        result["model_calls"] += 1
                        break
                    except _BatchDeferredBeforeHttp:
                        break
                    if job["state"] == "decided":
                        self._apply(job, dot, token, now)
            pending = self.journal.jobs(dot_id)
            result["pending"] = len(pending)
            status = "pending" if pending else ("ok" if items else "empty")
        except _SourceFailed:
            status = "failed"
        except (_SourceInconclusive, ValueError, PermissionError, OSError, sqlite3.Error, KeyError):
            status = "inconclusive"
        finally:
            heartbeat.stop()
            # A stale writer must not overwrite the replacement writer's coverage.
            try:
                self._guard(dot_id, dot, token)
                self.store.update_coverage(dot_id, status, now, success=status in ("ok", "empty"))
            except (PermissionError, ValueError):
                status = "stopped_or_fenced"
            try:
                self.store.release(dot_id, self.owner, token)
            finally:
                with self._lease_heartbeat_lock:
                    self._lease_heartbeats.pop((dot_id, token), None)
        result["outcome"] = status
        pending = self.journal.jobs(dot_id)
        result["pending"] = len(pending)
        result["pending_blocked"] = sum(job['state'] == 'blocked' for job in pending)
        result["deferred_only"] = bool(pending) and all(
            job['state'] == 'ready' and job['reason'] in
            ('queued', 'operator_reclassification', 'deferred_before_http', 'pre_inference_recovered')
            for job in pending)
        result['transient_retries_waiting'] = sum(job['reason'] == 'transient_transport_deferred' for job in pending)
        return result

    def _interpret(self, job, dot, token):
        dot_id, config = dot["id"], dot["config"]
        if job["state"] == "calling":
            if job["reason"] in MODEL_RESPONSE_FAILURE_CODES:
                # A typed response rejection is journaled before settlement. If
                # the worker stopped between those writes, finish the same
                # exact settlement and stay terminal; never infer again here.
                actual = job["actual"]
                if actual is not None:
                    integer(actual)
                self.store.settle(job["reservation"], actual)
                if actual is not None and actual > config.get("max_call_micro_usd", 10):
                    self.store.pause(dot_id)
                self.journal.update(job, state="blocked")
            else:
                try:
                    self.store.settle(job["reservation"], None)
                except ValueError as error:
                    if str(error) == "settlement_conflict" and job["decision_json"] is None:
                        # Cancellation can settle zero before its ready transition
                        # commits. Idempotent zero confirms that exact state; a
                        # nonzero settlement still raises, never becoming a free retry.
                        self.store.settle(job["reservation"], 0)
                    elif str(error) != "unknown_reservation":
                        raise
                    # decide starts only after reserve and the cancellation guard.
                    # No Store row or a settled-zero cancellation means no call;
                    # retain attempts and acquire a fresh hold under current caps.
                    self.journal.update(job, state="ready", reservation=None,
                                        reason="pre_inference_recovered")
                else:
                    # A real hold can include an interrupted call. Never repeat an
                    # inference of unknown disposition without owner settlement.
                    self.journal.update(job, state="blocked", reason="cost_unknown")
        if job["state"] != "ready":
            return 0
        if job['reason'] == 'transient_transport_deferred':
            # Only a controlled network failure before any valid decision can
            # retry. Its original full hold and private snapshot are permanent.
            # Connection retries are bounded by the same max_attempts and cap;
            # crash recovery, bad response/metering and effects never auto replay.
            with self.store._connection(write=False) as conn:
                prior = conn.execute('SELECT dot_id,status,created_at FROM reservations WHERE id=?', (job['reservation'],)).fetchone()
            if not prior or prior['dot_id'] != dot_id or prior['status'] != 'unknown':
                self.journal.update(job, state='blocked', reason='transport_reconciliation_changed')
                return 0
            delay = min(3600, 60 * (2 ** max(0, job['attempts'] - 1)))
            if timestamp(self.clock()) < prior['created_at'] + delay:
                return 0
        if job["attempts"] >= config.get("max_attempts", 3):
            self.journal.update(job, state="blocked", reason="retry_limit")
            return 0
        item = json.loads(job["item_json"])
        try:
            source_org_map = validate_source_org_map(config)
            if source_org_map and len(set(source_org_map.values())) > 1:
                # One collector DOT covers two legal entities. Keep its durable
                # memory separate from every other DOT, but do not cross-feed
                # entity-specific memory between those two source organizations.
                memory = []
            else:
                memory = self.store.retrieve(dot_id, item["content"][:1000], limit=5, max_bytes=4096)
        except (ValueError, OSError, sqlite3.Error):
            self.journal.update(job, reason="retrieval_failed")
            return 0
        # The live adapter can prove local rejection before HTTP. Do this before
        # any reservation/attempt: source-policy or secret lookup errors are
        # operational failures, never fictitious unknown paid model calls.
        from .adapters import OpenRouterModel
        model = self._model(dot_id)
        if isinstance(model, OpenRouterModel):
            try:
                model.preflight(config, item, memory)
            except (ValueError, PermissionError, OSError):
                self.journal.update(job, state="blocked", reason="preflight_rejected", actual=0)
                return 0
        amount = config.get("max_call_micro_usd", 10)
        # Cancellation before inference settles zero without consuming an
        # attempt. A later ready transition needs a fresh hold, not that settled
        # ID. Crash recovery of calling jobs still uses the persisted ID below.
        reservation = digest("r-", [dot_id, job["item_key"], uuid.uuid4().hex])
        self.journal.update(job, state="calling", reservation=reservation, grant_version=dot["grant_version"])
        try:
            self._guard(dot_id, dot, token)
            self.store.reserve(dot_id, reservation, amount, now=timestamp(self.clock()))
        except (ValueError, PermissionError):
            self.journal.update(job, state="ready", reason="budget_or_stop")
            return 0
        try:
            self._guard(dot_id, dot, token)
        except PermissionError:
            self.store.settle(reservation, 0)
            self.journal.update(job, state="ready", reason="stopped")
            raise
        if item.get("content_truncated") is True:
            # Never ask a model to decide from silently incomplete source text.
            # The owner-review path can retain attachments and extracted fields.
            self.store.settle(reservation, 0)
            decision = {"action": "needs_owner", "reason_code": "missing-data"}
            self.journal.update(job, state="decided", decision_json=encoded(decision),
                                actual=0, reason="source_truncated")
            return 0
        prior_attempts = job["attempts"]
        self.journal.update(job, attempts=prior_attempts + 1)
        try:
            response = self._model(dot_id).decide(config, item, memory)
            actual = response["cost_micro_usd"]
            if actual is not None:
                integer(actual)
            decision = validate_decision(response["decision"], config["kind"])
            usage = response["usage"]
            integer(usage["input_tokens"])
            integer(usage["output_tokens"])
            self.journal.update(job, state="decided", decision_json=encoded(decision),
                                actual=actual, reason="interpreted",
                                usage_input_tokens=usage["input_tokens"],
                                usage_output_tokens=usage["output_tokens"])
            self.store.settle(reservation, actual)
            if actual is None:
                self.journal.update(job, state="blocked", reason="cost_unknown")
            elif actual > amount:
                self.store.pause(dot_id)
                self.journal.update(job, state="blocked", reason="cost_overrun")
        except ModelRateLimited:
            # The request reached the provider boundary, so keep the full
            # reservation unknown and require explicit reconciliation. The
            # shared pacer records the cooldown; this tick must stop here.
            self.store.settle(reservation, None)
            self.journal.update(job, state="blocked", reason="cost_unknown")
            raise _BatchRateLimited() from None
        except (paced_transport.PacedCooldownActive,
                paced_transport.PacedCallLimitExceeded,
                paced_transport.PacedTransportUnavailable):
            # Local pacing rejected the call before HTTP. Release the hold at
            # zero and restore the attempt count so a deferred job stays ready.
            self.store.settle(reservation, 0)
            self.journal.update(job, state="ready", attempts=prior_attempts,
                                reservation=None, actual=0,
                                reason="deferred_before_http")
            raise _BatchDeferredBeforeHttp() from None
        except ModelResponseRejected as error:
            code = error.code if isinstance(error.code, str) and error.code in MODEL_RESPONSE_FAILURE_CODES else "MODEL_RESPONSE_REJECTED"
            actual = error.cost_micro_usd
            if (code not in MODEL_RESPONSE_COST_CODES or type(actual) is not int
                    or not 0 <= actual <= (1 << 63) - 1):
                actual = None
            usage = error.usage if code in MODEL_RESPONSE_USAGE_CODES else None
            input_tokens = output_tokens = None
            if isinstance(usage, dict):
                candidate_input = usage.get("input_tokens")
                candidate_output = usage.get("output_tokens")
                if (type(candidate_input) is int and 0 <= candidate_input <= (1 << 63) - 1
                        and type(candidate_output) is int and 0 <= candidate_output <= (1 << 63) - 1):
                    input_tokens, output_tokens = candidate_input, candidate_output
            # Persist only allowlisted diagnostics and validated numeric
            # accounting before settling so restart recovery can finish safely.
            self.journal.update(job, actual=actual, reason=code,
                                usage_input_tokens=input_tokens,
                                usage_output_tokens=output_tokens)
            self.store.settle(reservation, actual)
            if actual is not None and actual > amount:
                self.store.pause(dot_id)
            self.journal.update(job, state="blocked")
        except ModelTransportRejected:
            self.store.settle(reservation, None)
            self.journal.update(job, state='blocked', actual=None, reason='TRANSPORT_REJECTED_HTTP')
        except (TimeoutError, ConnectionError) as error:
            # Classification has no business effect until a metered, validated
            # reply. A lost reply can still be billed, so keep its full hold.
            # Never count that unknown call as free, or replay an Office effect.
            code = ('TRANSPORT_TIMEOUT_UNKNOWN' if isinstance(error, TimeoutError)
                    else 'TRANSPORT_FAILED_UNKNOWN')
            self.store.settle(reservation, None)
            self.journal.update(job, actual=None, reason=code)
            self.journal.preserve_model_attempt(job)
            self.journal.update(job, state=('blocked' if job['attempts'] >= config.get('max_attempts', 3) else 'ready'),
                                reason=(code if job['attempts'] >= config.get('max_attempts', 3) else 'transient_transport_deferred'))
        except Exception:
            # No exception text/prompt/provider body enters status or Office.
            self.store.settle(reservation, None)
            self.journal.update(job, state="blocked", reason="interpretation_unknown")
        return 1

    def _apply(self, job, dot, token, now):
        dot_id, config = dot["id"], dot["config"]
        # Reopening a decided job after a crash recovers settlement exactly once.
        self.store.settle(job["reservation"], job["actual"])
        # The reply can be durable before _interpret enforces its cost policy.
        # Recovery must enforce the same policy before dispatching any effect.
        if job["actual"] is None:
            self.journal.update(job, state="blocked", reason="cost_unknown")
            return
        if job["actual"] > config.get("max_call_micro_usd", 10):
            self.store.pause(dot_id)
            self.journal.update(job, state="blocked", reason="cost_overrun")
            return
        if job["grant_version"] != dot["grant_version"]:
            self.journal.update(job, state="blocked", reason="grant_changed")
            return
        decision = json.loads(job["decision_json"])
        action = decision["action"]
        if action == "ignore":
            item = json.loads(job["item_json"])
            # Source-identified finance evidence is never discarded solely
            # because a classifier says "ignore". Stage it for owner review.
            invoice_values = item.get("invoice")
            real_invoice_fields = (isinstance(invoice_values, dict)
                                   and any(invoice_values.get(k) for k in ("number", "date", "amount", "currency")))
            document_hint = any(a.get("mime_type") in ("application/pdf", "application/xml")
                                for a in item.get("attachments", []) if isinstance(a, dict))
            if not (config["kind"] == "collector" and (real_invoice_fields or document_hint)):
                self._guard(dot_id, dot, token)
                self.store.mark_seen(dot_id, job["item_key"])
                self.journal.update(job, state="done", reason="noise")
                return
            action = "needs_owner"
        item = json.loads(job["item_json"])
        invoice = None
        source_org_map = validate_source_org_map(config)
        item_org = item.get("org", config["org"])
        expected_org = (source_org_map.get(item.get("source_alias"))
                        if source_org_map else config["org"])
        approval_payload = None
        if action == "collect":
            try:
                if expected_org is None or item_org != expected_org:
                    raise ValueError("invalid_invoice")
                if decision.get("invoice") is not None:
                    raise ValueError("model_invoice_forbidden")
                invoice = invoice_fields(item.get("invoice"), expected_org)
                if "invoice" in item and invoice_fields(item["invoice"], expected_org) != invoice:
                    raise ValueError("ambiguous_invoice")
                # The accepted target exposes an opaque lookup, not payload
                # comparison. Detect identity conflicts in current private Store.
                for other in self.store.list_dots():
                    if invoice["org"] not in allowed_orgs(other["config"]):
                        continue
                    for prior in self.store.effects(other["id"]):
                        previous = prior["payload"].get("invoice")
                        if (previous and (previous["org"], previous["number"], previous["date"])
                                == (invoice["org"], invoice["number"], invoice["date"])
                                and previous != invoice):
                            raise ValueError("ambiguous_invoice")
                self.journal.claim_invoice(invoice)
                if source_org_map and config["kind"] == "collector":
                    evidence = private_invoice_evidence(item, expected_org)
                    source_invoice = {"org": expected_org, **evidence["invoice"]}
                    if invoice_fields(source_invoice, expected_org) != invoice:
                        raise ValueError("invoice_source_conflict")
                    approval_payload = private_approval_payload(
                        item, expected_org, dot_id, config["project"], "invoice")
            except ValueError:
                action = "needs_owner"
        if (source_org_map and config["kind"] == "collector"
                and action == "needs_owner"
                and ("invoice" in item or item.get("attachments"))):
            try:
                if expected_org is None or item_org != expected_org:
                    raise ValueError("invoice_source_scope")
                evidence = private_invoice_evidence(item, expected_org)
                missing = evidence["missing_fields"]
                reason = "missing-data" if missing or evidence["content_truncated"] else "uncertain"
                approval_payload = private_approval_payload(
                    item, expected_org, dot_id, config["project"], reason)
            except (ValueError, KeyError, UnicodeEncodeError):
                action = "needs_owner"
                approval_payload = None
        self._guard(dot_id, dot, token, action)
        if approval_payload is not None:
            effect_id = digest("e-", [dot_id, item["source_alias"], item["id"], "approval"])
            payload = approval_payload
            target_ref = config.get("approval_target_ref", "synthetic-drafts")
        elif action == "collect":
            effect_id = (digest("e-", [dot_id, item["source_alias"], item["id"], "approval"])
                         if source_org_map else
                         digest("e-", ["invoice", invoice["org"], invoice["number"], invoice["date"]]))
            payload = {"org": invoice["org"], "project": config["project"],
                       "invoice": invoice, "draft": True}
            if source_org_map:
                payload.update({"dot_id": dot_id, "source_alias": item["source_alias"]})
            if item.get("simulate_timeout_after_write") is True:
                payload["simulate_timeout_after_write"] = True
            target_ref = config.get("approval_target_ref", "synthetic-drafts")
        else:
            # Explicit operator recovery has its own stable effect generation.
            # Old expired/unknown Office effects remain untouched; restart of
            # this generation still uses exactly the same id and dedupe key.
            generation = self._classification_generation(dot_id, job["item_key"])
            identity_parts = [dot_id, job["item_key"], action]
            if generation is not None:
                identity_parts.append(generation)
            effect_id = digest("e-", identity_parts)
            reason = decision["reason_code"] if decision["reason_code"] in REASONS else "uncertain"
            if decision["action"] == "collect":
                reason = "invalid-data"
            payload = {"event_id": effect_id, "dot_id": dot_id, "org": item_org,
                       "project": config["project"], "kind": action.replace("_", "-"),
                       "reason_code": reason, "opaque_ref": job["item_key"],
                       "hops": 0, "expires_at": now + 3600}
            target_ref = config.get("office_target_ref", "synthetic-office")
        # Use original TTL/payload when replaying a committed intent.
        existing = next((effect for effect in self.store.effects(dot_id) if effect["id"] == effect_id), None)
        effect = existing or self.store.intent(dot_id, effect_id, action, target_ref, payload, dot["grant_version"])
        self.journal.update(job, effect_id=effect_id)
        if effect["action"] != action or effect["grant_version"] != dot["grant_version"]:
            raise PermissionError("intent_version_changed")
        if effect["status"] != "confirmed":
            if job["deliveries"] >= config.get("max_attempts", 3):
                self.journal.update(job, state="blocked", reason="effect_lookup_limit")
                return
            self.journal.update(job, deliveries=job["deliveries"] + 1)
        if self._deliver(effect, dot, token):
            self.store.mark_seen(dot_id, job["item_key"])
            self.journal.update(job, state="done", reason="confirmed")

    def _deliver(self, effect, dot, token):
        if effect["status"] == "confirmed":
            return True
        dot_id, effect_id = dot["id"], effect["id"]
        config = dot["config"]
        approval_ref = config.get("approval_target_ref", "synthetic-drafts")
        office_ref = config.get("office_target_ref", "synthetic-office")
        self._guard(dot_id, dot, token, effect["action"])
        receipt = None
        if effect["target_ref"] == approval_ref:
            try:
                self._guard(dot_id, dot, token, effect["action"])
                receipt = self._target(dot_id).lookup(effect_id)
            except Exception:
                return False
        elif effect["target_ref"] == office_ref and hasattr(self.office, "lookup"):
            try:
                receipt = self.office.lookup(effect_id)
            except Exception:
                return False
        elif effect["target_ref"] != office_ref:
            raise PermissionError("unknown_target")
        if receipt is None and effect["status"] == "unknown":
            return False  # missing receipt is never permission to reissue
        if receipt is None:
            if effect["target_ref"] == office_ref and effect["payload"]["expires_at"] <= timestamp(self.clock()):
                return False
            self._guard(dot_id, dot, token, effect["action"])
            self.store.effect_status(dot_id, effect_id, "unknown")
            try:
                self._guard(dot_id, dot, token, effect["action"])
                if effect["target_ref"] == approval_ref:
                    receipt = self._target(dot_id).put(effect_id, effect["payload"])
                else:
                    message = self.office.send(effect["payload"])
                    receipt = {"receipt": message.get("receipt", message["event_id"]),
                               "status": "confirmed"}
            except Exception:
                if effect["target_ref"] == approval_ref:
                    try:
                        self._guard(dot_id, dot, token, effect["action"])
                        receipt = self._target(dot_id).lookup(effect_id)
                    except Exception:
                        return False
                elif hasattr(self.office, "lookup"):
                    try:
                        receipt = self.office.lookup(effect_id)
                    except Exception:
                        return False
        if receipt is not None and receipt.get("status") == "confirmed":
            self.store.effect_status(dot_id, effect_id, "confirmed", receipt["receipt"])
            return True
        return False

    def reconcile(self, dot_id):
        """Lookup only. Never retries writes or invents unknown inference costs."""
        dot = self.store.get_dot(identity(dot_id))
        now = timestamp(self.clock())
        token = self.store.claim(dot_id, self.owner, now, self.lease_seconds)
        if token is None:
            return {"dot_id": dot_id, "confirmed": 0, "outcome": "paused_or_leased"}
        confirmed = 0
        try:
            for effect in self.store.effects(dot_id):
                approval_ref = dot["config"].get("approval_target_ref", "synthetic-drafts")
                office_ref = dot["config"].get("office_target_ref", "synthetic-office")
                if effect["target_ref"] == approval_ref:
                    lookup = self._target(dot_id).lookup
                elif effect["target_ref"] == office_ref and hasattr(self.office, "lookup"):
                    lookup = self.office.lookup
                else:
                    continue
                self._guard(dot_id, dot, token, effect["action"])
                if effect["grant_version"] != dot["grant_version"]:
                    continue
                receipt = None if effect["status"] == "confirmed" else lookup(effect["id"])
                if receipt and receipt.get("status") == "confirmed":
                    self.store.effect_status(dot_id, effect["id"], "confirmed", receipt["receipt"])
                    confirmed += 1
                if effect["status"] == "confirmed" or (receipt and receipt.get("status") == "confirmed"):
                    for job in self.journal.jobs(dot_id):
                        if job["effect_id"] == effect["id"] and job["grant_version"] == dot["grant_version"]:
                            self._guard(dot_id, dot, token, effect["action"])
                            self.store.mark_seen(dot_id, job["item_key"])
                            self.journal.update(job, state="done", reason="confirmed")
        finally:
            self.store.release(dot_id, self.owner, token)
        return {"dot_id": dot_id, "confirmed": confirmed, "outcome": "lookup_only"}

    def _classification_generation(self, dot_id, item_key):
        with closing(self.journal.connect()) as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='classification_recovery'").fetchone():
                return None
            row = conn.execute("SELECT repair_id FROM classification_recovery WHERE dot_id=? AND item_key=? ORDER BY rowid DESC LIMIT 1", (dot_id, item_key)).fetchone()
            return row[0] if row else None

    def resume_classification(self, dot_id, repair_id, *, approval_reference, max_jobs=64, max_generations=1):
        """Explicit finite owner repair; no model call or old cost settlement. Historical reference only.

        Prior complete job rows remain immutable in the private journal. Old
        paid/unknown ledger reservations are never reset or reused. The next
        inference acquires a fresh reservation under the same aggregate cap.
        This is not automatic replay and is not exposed as a model tool.
        """
        if OWNER_REPAIR_APPROVAL_REFERENCE is None or approval_reference != OWNER_REPAIR_APPROVAL_REFERENCE:
            raise PermissionError('operator_repair_required')
        dot_id, repair_id = identity(dot_id), identity(repair_id)
        integer(max_jobs, 1, 64)
        integer(max_generations, 1, 2)
        dot = self.store.get_dot(dot_id)
        now = timestamp(self.clock())
        token = self.store.claim(dot_id, self.owner, now, self.lease_seconds)
        if token is None:
            raise PermissionError('paused_or_leased')
        count = 0
        try:
            self._guard(dot_id, dot, token)
            with closing(self.journal.connect()) as conn, conn:
                conn.execute('BEGIN IMMEDIATE')
                conn.execute('''CREATE TABLE IF NOT EXISTS classification_recovery (
                    repair_id TEXT NOT NULL, dot_id TEXT NOT NULL, item_key TEXT NOT NULL,
                    prior_job_json TEXT NOT NULL, recovered_at REAL NOT NULL,
                    PRIMARY KEY(repair_id,dot_id,item_key))''')
                rows = conn.execute("SELECT * FROM jobs WHERE dot_id=? AND state='blocked' ORDER BY item_key LIMIT 65", (dot_id,)).fetchall()
                if len(rows) > 64:
                    raise ValueError('repair_scope_limit')
                for raw in rows:
                    job = dict(raw)
                    if count >= max_jobs:
                        break
                    previous = conn.execute('SELECT COUNT(*) FROM classification_recovery WHERE dot_id=? AND item_key=?', (dot_id, job['item_key'])).fetchone()[0]
                    if previous >= max_generations or conn.execute('SELECT 1 FROM classification_recovery WHERE repair_id=? AND dot_id=? AND item_key=?', (repair_id, dot_id, job['item_key'])).fetchone():
                        continue  # finite operator generation; default remains one
                    if job['grant_version'] not in (None, dot['grant_version']):
                        continue
                    reasons = {'interpretation_unknown', 'cost_unknown', 'preflight_rejected', 'TRANSPORT_TIMEOUT_UNKNOWN', 'TRANSPORT_FAILED_UNKNOWN'} | MODEL_RESPONSE_FAILURE_CODES
                    if job['reason'] not in reasons and job['reason'] != 'effect_lookup_limit':
                        continue
                    if job['effect_id']:
                        if job['reason'] != 'effect_lookup_limit' or not hasattr(self.office, 'delivery_status'):
                            continue
                        if self.office.delivery_status(job['effect_id']) != 'expired':
                            continue  # never replay an unexpired/confirmed effect
                    self._guard(dot_id, dot, token)
                    conn.execute('INSERT INTO classification_recovery VALUES(?,?,?,?,?)', (repair_id, dot_id, job['item_key'], encoded(job), now))
                    conn.execute("""UPDATE jobs SET state='ready',attempts=0,deliveries=0,
                        reservation=NULL,decision_json=NULL,actual=NULL,effect_id=NULL,
                        reason='operator_reclassification',usage_input_tokens=NULL,usage_output_tokens=NULL
                        WHERE dot_id=? AND item_key=? AND state='blocked'""", (dot_id, job['item_key']))
                    count += 1
        finally:
            self.store.release(dot_id, self.owner, token)
        return {'dot_id': dot_id, 'requeued': count, 'old_cost_reservations_preserved': True, 'model_calls': 0}

    def reconcile_cost(self, dot_id, reservation_id, actual):
        """Explicit owner settlement using a measured SYNTHETIC cost; no call."""
        dot_id, actual = identity(dot_id), integer(actual)
        job = next((row for row in self.journal.jobs(dot_id) if row["reservation"] == reservation_id), None)
        if (not job or not (job["reason"] in ("cost_unknown", "interpretation_unknown")
                            or (job["reason"] in MODEL_RESPONSE_FAILURE_CODES
                                and job["actual"] is None))):
            raise ValueError("unknown_job_reservation")
        try:
            self.store.settle(reservation_id, actual)
        except ValueError as error:
            # Older recovery could block a pre-reserve crash as cost_unknown.
            # The absent hold and absent reply permit explicit zero-cost repair,
            # never a fictional charge or a replay of a saved model decision.
            if (str(error) != "unknown_reservation" or actual != 0
                    or job["reason"] != "cost_unknown" or job["decision_json"] is not None):
                raise
            self.journal.update(job, state="ready", reservation=None,
                                actual=0, reason="pre_inference_recovered")
            return {"dot_id": dot_id, "outcome": "pre_inference_recovered"}
        config = self.store.get_dot(dot_id)["config"]
        state = "decided" if job["decision_json"] else "ready"
        if actual > config.get("max_call_micro_usd", 10):
            self.store.pause(dot_id)
            state = "blocked"
        self.journal.update(job, state=state, actual=actual, reason="cost_reconciled")
        return {"dot_id": dot_id, "outcome": "cost_reconciled"}

    def send_peer(self, sender, peer, message):
        """Optional metadata advice only; no recipient tool dispatch exists."""
        dot, other = self.store.get_dot(sender), self.store.get_dot(peer)
        config, recipient = dot["config"], other["config"]
        if (peer not in config["allowed_peers"] or sender not in recipient["allowed_peers"]
                or (config["org"], config["project"]) != (recipient["org"], recipient["project"])
                or "peer" not in recipient["allowed_actions"] or other["paused"]):
            raise PermissionError("peer_denied")
        if not isinstance(message, dict) or set(message) != {"event_id", "dot_id", "org", "project", "kind", "reason_code", "opaque_ref", "hops", "expires_at"}:
            raise ValueError("invalid_peer_message")
        now = timestamp(self.clock())
        if (message["dot_id"] != sender or message["org"] != config["org"]
                or message["project"] != config["project"] or message["kind"] != "peer"
                or message["reason_code"] not in REASONS):
            raise PermissionError("peer_scope")
        integer(message["hops"], 0, 2)
        expiry = timestamp(message["expires_at"])
        if not now < expiry <= now + 3600:
            raise ValueError("peer_ttl")
        identity(message["event_id"])
        identity(message["opaque_ref"])
        effects = self.store.effects(sender)
        if len(effects) >= config.get("max_state_records", 1000) and not any(e["id"] == message["event_id"] for e in effects):
            raise ValueError("journal_limit")
        token = self.store.claim(sender, self.owner, now, self.lease_seconds)
        if token is None:
            raise PermissionError("peer_paused_or_leased")
        try:
            self._guard(sender, dot, token, "peer")
            effect = self.store.intent(sender, message["event_id"], "peer", "synthetic-office", message, dot["grant_version"])
            return self._deliver(effect, dot, token)
        finally:
            self.store.release(sender, self.owner, token)

    def status(self, dot_id=None):
        dots = [self.store.get_dot(identity(dot_id))] if dot_id else self.store.list_dots()
        rows = []
        for dot in dots:
            jobs = self.journal.jobs(dot["id"])
            effects = self.store.effects(dot["id"])
            rows.append({name: dot[name] for name in ("id", "paused", "last_success", "last_attempt", "coverage_status", "grant_version")})
            rows[-1].update({"kind": dot["config"]["kind"], "model": "synthetic" if dot["config"]["model"] == "synthetic" else "unqualified",
                             "interval_seconds": dot["config"]["interval_seconds"],
                             "pending": len(jobs), "pending_reasons": sorted({job["reason"] for job in jobs}),
                             "unknown_reservations": [
                                 job["reservation"] for job in jobs
                                 if (job["reason"] in ("cost_unknown", "interpretation_unknown")
                                     or (job["reason"] in MODEL_RESPONSE_FAILURE_CODES
                                         and job["actual"] is None))
                             ],
                             "unconfirmed_effects": sum(e["status"] != "confirmed" for e in effects),
                             "budget": self.store.budget_status(dot["id"])})
        return {"candidate": True, "live_qualification": "BLOCKED", "dots": rows,
                "global_budget": self.store.budget_status()}

    def safe_errors(self, dot_id=None):
        """Return allowlisted aggregate model diagnostics, never item-level data."""
        if dot_id is not None:
            dot_id = identity(dot_id)
        return self.journal.safe_errors(dot_id)
