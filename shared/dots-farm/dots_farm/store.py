"""Transactional controller state; namespaces are not an OS security sandbox.

Every operation uses its own SQLite connection. Budget reservations and lease
claims serialize with BEGIN IMMEDIATE, including between controller processes.
Memory and effect payloads are private state and never grant capabilities.
"""

from contextlib import contextmanager
import json
import math
from pathlib import Path
import re
import sqlite3
import time


_IDENTITY = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_SQLITE_MAX = (1 << 63) - 1
_MAX_QUEUE = 10_000
_MAX_PAYLOAD = 1_048_576
_COVERAGE = {"empty", "ok", "failed", "inconclusive", "pending"}
_EFFECT_STATES = {"pending": 0, "unknown": 1, "confirmed": 2}
SOURCE_ORG_MAPS = {
    "observer": {"energy-nomination": "example-energy"},
    "collector": {
        "microsoft-example-technology": "example-technology",
        "google-example-finance": "example-finance",
    },
}
_CALENDAR_WEEK = "calendar_week_monday_utc"
_ROLLING_PERIOD = "rolling"
_WEEK_SECONDS = 7 * 24 * 60 * 60
_MONDAY_EPOCH_OFFSET = 4 * 24 * 60 * 60


def validate_source_org_map(config):
    """Validate the pilot's exact public mailbox-alias to legal-org mapping."""
    mapping = config.get("source_org_map")
    if mapping is None:
        return None
    expected = SOURCE_ORG_MAPS.get(config.get("kind"))
    if (expected is None or type(mapping) is not dict or mapping != expected
            or config.get("org") not in set(expected.values())):
        raise ValueError("invalid_source_org_map")
    return dict(expected)


def allowed_orgs(config):
    mapping = validate_source_org_map(config)
    return set(mapping.values()) if mapping else {config["org"]}


def _monday_week_start(timestamp):
    value = float(timestamp)
    return ((value - _MONDAY_EPOCH_OFFSET) // _WEEK_SECONDS) * _WEEK_SECONDS + _MONDAY_EPOCH_OFFSET


def _identity(value):
    if not isinstance(value, str) or not _IDENTITY.fullmatch(value):
        raise ValueError("invalid_identity")
    return value


def _integer(value, *, minimum=0, maximum=_SQLITE_MAX):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("invalid_integer")
    return value


def _timestamp(value, *, positive=False):
    if type(value) not in (int, float):
        raise ValueError("invalid_timestamp")
    try:
        result = float(value)
    except (OverflowError, ValueError):
        raise ValueError("invalid_timestamp") from None
    if not math.isfinite(result) or (positive and result <= 0):
        raise ValueError("invalid_timestamp")
    return result


def _boolean(value):
    if type(value) is not bool:
        raise ValueError("invalid_boolean")
    return value


def _text(value, maximum, *, empty=False):
    if not isinstance(value, str) or (not empty and not value):
        raise ValueError("invalid_text")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        raise ValueError("invalid_text") from None
    if size > maximum or "\x00" in value:
        raise ValueError("text_limit")
    return value


def _json(value, maximum):
    # json.dumps alone silently converts non-string keys and tuples. Persist only
    # JSON-compatible input, with finite numbers and fixed, payload-free errors.
    def check(item):
        if item is None or type(item) in (bool, int, str):
            return
        if type(item) is float and math.isfinite(item):
            return
        if type(item) is list:
            for child in item:
                check(child)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for child in item.values():
                check(child)
            return
        raise ValueError("invalid_json")

    try:
        check(value)
        result = json.dumps(value, ensure_ascii=False, allow_nan=False,
                            sort_keys=True, separators=(",", ":"))
        size = len(result.encode("utf-8"))
    except (ValueError, TypeError, OverflowError, RecursionError, UnicodeError):
        raise ValueError("invalid_json") from None
    if size > maximum:
        raise ValueError("payload_limit")
    return result


class Store:
    """A durable SQLite ledger with per-DOTS logical namespaces."""

    def __init__(self, path):
        try:
            self.path = Path(path).resolve()
            if str(path) == ":memory:":
                raise ValueError("persistent_path_required")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._connection() as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS budget (
                        id INTEGER PRIMARY KEY CHECK(id=1),
                        cap INTEGER NOT NULL DEFAULT 0,
                        emergency_reserve INTEGER NOT NULL DEFAULT 0,
                        period_start REAL NOT NULL,
                        period_seconds REAL NOT NULL,
                        last_now REAL NOT NULL,
                        configured INTEGER NOT NULL DEFAULT 0,
                        period_mode TEXT NOT NULL DEFAULT 'rolling'
                    );
                    CREATE TABLE IF NOT EXISTS dots (
                        id TEXT PRIMARY KEY,
                        config TEXT NOT NULL,
                        paused INTEGER NOT NULL DEFAULT 0,
                        last_success REAL,
                        last_attempt REAL,
                        coverage_status TEXT NOT NULL DEFAULT 'pending',
                        grant_version INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS leases (
                        dot_id TEXT PRIMARY KEY REFERENCES dots(id),
                        owner TEXT,
                        token INTEGER NOT NULL,
                        expires_at REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS reservations (
                        id TEXT PRIMARY KEY,
                        dot_id TEXT NOT NULL REFERENCES dots(id),
                        amount INTEGER NOT NULL,
                        emergency INTEGER NOT NULL,
                        created_at REAL NOT NULL,
                        status TEXT NOT NULL,
                        actual INTEGER
                    );
                    CREATE INDEX IF NOT EXISTS reservations_dot
                        ON reservations(dot_id, created_at);
                    CREATE TABLE IF NOT EXISTS memories (
                        dot_id TEXT NOT NULL REFERENCES dots(id),
                        id TEXT NOT NULL,
                        text TEXT NOT NULL,
                        source_ref TEXT NOT NULL,
                        observed_at REAL NOT NULL,
                        trust TEXT NOT NULL,
                        revision INTEGER NOT NULL,
                        PRIMARY KEY(dot_id, id)
                    );
                    CREATE TABLE IF NOT EXISTS seen_items (
                        dot_id TEXT NOT NULL REFERENCES dots(id),
                        id TEXT NOT NULL,
                        PRIMARY KEY(dot_id, id)
                    );
                    CREATE TABLE IF NOT EXISTS effects (
                        dot_id TEXT NOT NULL REFERENCES dots(id),
                        id TEXT NOT NULL,
                        action TEXT NOT NULL,
                        target_ref TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        grant_version INTEGER NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending',
                        target_receipt TEXT,
                        PRIMARY KEY(dot_id, id)
                    );
                """)
                columns = {row["name"] for row in conn.execute("PRAGMA table_info(budget)")}
                if "period_mode" not in columns:
                    conn.execute("ALTER TABLE budget ADD COLUMN period_mode TEXT NOT NULL DEFAULT 'rolling'")
                now = time.time()
                conn.execute("""INSERT OR IGNORE INTO budget
                    (id, period_start, period_seconds, last_now)
                    VALUES (1, ?, 2592000, ?)""", (now, now))
        except (OSError, TypeError, sqlite3.Error):
            raise ValueError("store_unavailable") from None

    @contextmanager
    def _connection(self, *, write=None):
        conn = None
        try:
            conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=FULL")
            if write is not None:
                conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield conn
            if conn.in_transaction:
                conn.commit()
        except sqlite3.Error:
            if conn is not None and conn.in_transaction:
                conn.rollback()
            raise ValueError("store_unavailable") from None
        except BaseException:
            if conn is not None and conn.in_transaction:
                conn.rollback()
            raise
        finally:
            if conn is not None:
                conn.close()

    @staticmethod
    def _dot(conn, dot_id):
        row = conn.execute("SELECT * FROM dots WHERE id=?", (dot_id,)).fetchone()
        if row is None:
            raise ValueError("unknown_dot")
        return row

    @staticmethod
    def _dot_result(row):
        return {"id": row["id"], "config": json.loads(row["config"]),
                "paused": bool(row["paused"]), "last_success": row["last_success"],
                "last_attempt": row["last_attempt"],
                "coverage_status": row["coverage_status"],
                "grant_version": row["grant_version"]}

    @staticmethod
    def _reference(conn, config, reference):
        _text(reference, 1024)
        if any(ord(char) < 32 for char in reference):
            raise ValueError("invalid_reference")
        if reference.startswith("org:") and reference[4:] != config["org"]:
            raise PermissionError("reference_scope")
        peer = reference[4:] if reference.startswith("dot:") else reference
        row = conn.execute("SELECT config FROM dots WHERE id=?", (peer,)).fetchone()
        if row is not None:
            other = json.loads(row["config"])
            if other["org"] != config["org"] or other["project"] != config["project"]:
                raise PermissionError("reference_scope")

    @staticmethod
    def _payload_scope(payload, dot_id, config):
        if isinstance(payload, dict):
            mapping = validate_source_org_map(config)
            if mapping and "invoice" in payload:
                invoice = payload.get("invoice")
                if (not isinstance(invoice, dict)
                        or mapping.get(payload.get("source_alias")) != invoice.get("org")):
                    raise PermissionError("payload_scope")
            for key, value in payload.items():
                if key == "org" and value not in allowed_orgs(config):
                    raise PermissionError("payload_scope")
                if key == "project" and value != config["project"]:
                    raise PermissionError("payload_scope")
                if key == "dot_id" and value != dot_id:
                    raise PermissionError("payload_scope")
                Store._payload_scope(value, dot_id, config)
        elif isinstance(payload, list):
            for value in payload:
                Store._payload_scope(value, dot_id, config)

    def create_dot(self, dot_id, config):
        dot_id = _identity(dot_id)
        if type(config) is not dict:
            raise ValueError("invalid_config")
        config = json.loads(_json(config, _MAX_PAYLOAD))
        if config.get("kind") not in ("observer", "collector", "supervisor"):
            raise ValueError("invalid_kind")
        for field in ("org", "project"):
            _identity(config.get(field))
        validate_source_org_map(config)
        _integer(config.get("budget_micro_usd"))
        config.setdefault("grant_version", 1)
        config.setdefault("model", "synthetic")
        config.setdefault("interval_seconds", 900)
        config.setdefault("allowed_actions", [])
        config.setdefault("allowed_peers", [])
        config.setdefault("max_queue", 100)
        config.setdefault("max_payload_bytes", 16384)
        _integer(config["grant_version"], minimum=1)
        _text(config["model"], 256)
        _timestamp(config["interval_seconds"], positive=True)
        _integer(config["max_queue"], minimum=1, maximum=_MAX_QUEUE)
        _integer(config["max_payload_bytes"], minimum=1, maximum=_MAX_PAYLOAD)
        for field in ("allowed_actions", "allowed_peers"):
            values = config[field]
            if type(values) is not list or len(values) > 100:
                raise ValueError("invalid_grants")
            for value in values:
                _identity(value)
            if len(values) != len(set(values)):
                raise ValueError("invalid_grants")
        encoded = _json(config, _MAX_PAYLOAD)
        with self._connection(write=True) as conn:
            if conn.execute("SELECT 1 FROM dots WHERE id=?", (dot_id,)).fetchone():
                raise ValueError("dot_exists")
            for peer in config["allowed_peers"]:
                self._reference(conn, config, peer)
            # Unknown peer IDs do not confer a usable capability. Creating the
            # peer later must still pass the same organization/project check.
            for row in conn.execute("SELECT config FROM dots"):
                existing = json.loads(row["config"])
                if dot_id in existing["allowed_peers"]:
                    if (existing["org"], existing["project"]) != (
                            config["org"], config["project"]):
                        raise PermissionError("peer_scope")
            conn.execute("INSERT INTO dots(id, config, grant_version) VALUES(?,?,?)",
                         (dot_id, encoded, config["grant_version"]))
            return self._dot_result(self._dot(conn, dot_id))

    def get_dot(self, dot_id):
        dot_id = _identity(dot_id)
        with self._connection(write=False) as conn:
            return self._dot_result(self._dot(conn, dot_id))

    def list_dots(self):
        with self._connection(write=False) as conn:
            return [self._dot_result(row) for row in conn.execute(
                "SELECT * FROM dots ORDER BY id")]

    def pause(self, dot_id, paused=True):
        dot_id, paused = _identity(dot_id), _boolean(paused)
        with self._connection(write=True) as conn:
            self._dot(conn, dot_id)
            conn.execute("UPDATE dots SET paused=? WHERE id=?", (int(paused), dot_id))
            if paused:
                conn.execute("UPDATE leases SET owner=NULL, expires_at=0 WHERE dot_id=?",
                             (dot_id,))

    def update_coverage(self, dot_id, status, now, success=False):
        dot_id, now, success = _identity(dot_id), _timestamp(now), _boolean(success)
        if not isinstance(status, str) or status not in _COVERAGE:
            raise ValueError("invalid_coverage")
        if success and status not in ("empty", "ok"):
            raise ValueError("invalid_success")
        with self._connection(write=True) as conn:
            dot = self._dot(conn, dot_id)
            if dot["last_attempt"] is not None and now < dot["last_attempt"]:
                raise ValueError("stale_coverage")
            conn.execute("""UPDATE dots SET coverage_status=?, last_attempt=?,
                last_success=? WHERE id=?""",
                         (status, now, now if success else dot["last_success"], dot_id))

    def claim(self, dot_id, owner, now, ttl):
        dot_id, owner = _identity(dot_id), _identity(owner)
        now, ttl = _timestamp(now), _timestamp(ttl, positive=True)
        expires_at = _timestamp(now + ttl)
        with self._connection(write=True) as conn:
            if self._dot(conn, dot_id)["paused"]:
                return None
            row = conn.execute("SELECT * FROM leases WHERE dot_id=?", (dot_id,)).fetchone()
            if row is not None and row["owner"] is not None and row["expires_at"] > now:
                return None
            token = _integer(1 if row is None else row["token"] + 1, minimum=1)
            conn.execute("""INSERT INTO leases(dot_id, owner, token, expires_at)
                VALUES(?,?,?,?) ON CONFLICT(dot_id) DO UPDATE SET
                owner=excluded.owner, token=excluded.token, expires_at=excluded.expires_at""",
                         (dot_id, owner, token, expires_at))
            return token

    def renew(self, dot_id, owner, token, now, ttl):
        """Extend one live lease without changing its fencing token."""
        dot_id, owner = _identity(dot_id), _identity(owner)
        token = _integer(token, minimum=1)
        now, ttl = _timestamp(now), _timestamp(ttl, positive=True)
        requested_expiry = _timestamp(now + ttl)
        with self._connection(write=True) as conn:
            if self._dot(conn, dot_id)["paused"]:
                raise PermissionError("invalid_lease")
            row = conn.execute("SELECT owner, token, expires_at FROM leases WHERE dot_id=?",
                               (dot_id,)).fetchone()
            if (row is None or row["owner"] != owner or row["token"] != token
                    or row["expires_at"] <= now):
                raise PermissionError("invalid_lease")
            expires_at = max(float(row["expires_at"]), requested_expiry)
            cursor = conn.execute("""UPDATE leases SET expires_at=?
                WHERE dot_id=? AND owner=? AND token=? AND expires_at>?""",
                                 (expires_at, dot_id, owner, token, now))
            if cursor.rowcount != 1:
                raise PermissionError("invalid_lease")
            return True

    def assert_lease(self, dot_id, owner, token, now):
        dot_id, owner = _identity(dot_id), _identity(owner)
        token, now = _integer(token, minimum=1), _timestamp(now)
        with self._connection(write=False) as conn:
            dot = self._dot(conn, dot_id)
            row = conn.execute("SELECT * FROM leases WHERE dot_id=?", (dot_id,)).fetchone()
            if (dot["paused"] or row is None or row["owner"] != owner
                    or row["token"] != token or row["expires_at"] <= now):
                raise PermissionError("invalid_lease")

    def release(self, dot_id, owner, token):
        dot_id, owner, token = _identity(dot_id), _identity(owner), _integer(token, minimum=1)
        with self._connection(write=True) as conn:
            self._dot(conn, dot_id)
            cursor = conn.execute("""UPDATE leases SET owner=NULL, expires_at=0
                WHERE dot_id=? AND owner=? AND token=?""", (dot_id, owner, token))
            return cursor.rowcount == 1

    @staticmethod
    def _advance_budget(conn, now):
        budget = conn.execute("SELECT * FROM budget WHERE id=1").fetchone()
        clock = max(now, budget["last_now"])
        if budget["period_mode"] == _CALENDAR_WEEK:
            start = max(budget["period_start"], _monday_week_start(clock))
        else:
            start = max(budget["period_start"], clock - budget["period_seconds"])
        conn.execute("UPDATE budget SET period_start=?, last_now=? WHERE id=1",
                     (start, clock))
        return clock

    @staticmethod
    def _totals(conn, start, dot_id=None, ordinary=False):
        spent = held = 0
        query, args = "SELECT * FROM reservations", []
        filters = []
        if dot_id is not None:
            filters.append("dot_id=?")
            args.append(dot_id)
        if ordinary:
            filters.append("emergency=0")
        if filters:
            query += " WHERE " + " AND ".join(filters)
        for row in conn.execute(query, args):
            if row["status"] in ("reserved", "unknown"):
                held += row["amount"]
            elif row["created_at"] >= start:
                spent += row["actual"]
        return spent, held

    @staticmethod
    def _stop_overruns(conn):
        budget = conn.execute("SELECT * FROM budget WHERE id=1").fetchone()
        spent, held = Store._totals(conn, budget["period_start"])
        if spent + held > budget["cap"]:
            conn.execute("UPDATE dots SET paused=1")
            conn.execute("UPDATE leases SET owner=NULL, expires_at=0")
            return
        for row in conn.execute("SELECT id, config FROM dots").fetchall():
            spent, held = Store._totals(conn, budget["period_start"], row["id"])
            if spent + held > json.loads(row["config"])["budget_micro_usd"]:
                conn.execute("UPDATE dots SET paused=1 WHERE id=?", (row["id"],))
                conn.execute("UPDATE leases SET owner=NULL, expires_at=0 WHERE dot_id=?",
                             (row["id"],))

    def configure_budget(self, global_cap, emergency_reserve=0, period_start=None,
                         period_seconds=2592000, period_mode=None):
        cap, emergency = _integer(global_cap), _integer(emergency_reserve)
        seconds = _timestamp(period_seconds, positive=True)
        start = None if period_start is None else _timestamp(period_start)
        if period_mode is not None and period_mode not in (_ROLLING_PERIOD, _CALENDAR_WEEK):
            raise ValueError("invalid_period_mode")
        if emergency > cap:
            raise ValueError("invalid_emergency_reserve")
        with self._connection(write=True) as conn:
            current = conn.execute("SELECT * FROM budget WHERE id=1").fetchone()
            mode = period_mode or current["period_mode"]
            if not current["configured"]:
                clock = time.time() if start is None else start
                start = _monday_week_start(clock) if mode == _CALENDAR_WEEK else clock
                # Even the first explicit configuration must retain any cost
                # already reported against the initial zero-cap ledger.
                if conn.execute("SELECT 1 FROM reservations LIMIT 1").fetchone():
                    start = min(start, current["period_start"])
                    clock = max(clock, current["last_now"])
                conn.execute("""UPDATE budget SET cap=?, emergency_reserve=?,
                    period_start=?, period_seconds=?, last_now=?, configured=1,
                    period_mode=? WHERE id=1""",
                             (cap, emergency, start, seconds, clock, mode))
            else:
                clock = current["last_now"]
                if mode == _CALENDAR_WEEK:
                    clock = max(clock, time.time() if start is None else start)
                    week_start = _monday_week_start(clock)
                    if current["period_mode"] == _CALENDAR_WEEK:
                        start = max(current["period_start"], week_start)
                    else:
                        # Switching an existing rolling ledger keeps the most
                        # recent week of spend before aligning to Monday.
                        start = max(current["period_start"], clock - _WEEK_SECONDS)
                elif start is None:
                    start = current["period_start"]
                elif start > current["period_start"]:
                    clock = max(clock, start)
                    start = max(current["period_start"], clock - seconds)
                conn.execute("""UPDATE budget SET cap=?, emergency_reserve=?,
                    period_start=?, period_seconds=?, last_now=?, period_mode=? WHERE id=1""",
                             (cap, emergency, start, seconds, clock, mode))
            self._stop_overruns(conn)

    def reserve(self, dot_id, reservation_id, amount, emergency=False, now=None):
        dot_id, reservation_id = _identity(dot_id), _identity(reservation_id)
        amount, emergency = _integer(amount), _boolean(emergency)
        now = _timestamp(time.time() if now is None else now)
        with self._connection(write=True) as conn:
            dot = self._dot(conn, dot_id)
            previous = conn.execute("SELECT * FROM reservations WHERE id=?",
                                    (reservation_id,)).fetchone()
            if previous is not None:
                if (previous["dot_id"], previous["amount"], bool(previous["emergency"])) != (
                        dot_id, amount, emergency):
                    raise ValueError("reservation_conflict")
                return {"id": reservation_id, "amount": amount, "status": previous["status"]}
            if dot["paused"]:
                raise PermissionError("dot_paused")
            created_at = self._advance_budget(conn, now)
            budget = conn.execute("SELECT * FROM budget WHERE id=1").fetchone()
            spent, held = self._totals(conn, budget["period_start"])
            own_spent, own_held = self._totals(conn, budget["period_start"], dot_id)
            ordinary_spent, ordinary_held = self._totals(
                conn, budget["period_start"], ordinary=True)
            if (spent + held + amount > budget["cap"]
                    or own_spent + own_held + amount > json.loads(dot["config"])["budget_micro_usd"]
                    or (not emergency and ordinary_spent + ordinary_held + amount
                        > budget["cap"] - budget["emergency_reserve"])):
                raise ValueError("budget_exhausted")
            conn.execute("""INSERT INTO reservations
                (id, dot_id, amount, emergency, created_at, status) VALUES(?,?,?,?,?,'reserved')""",
                         (reservation_id, dot_id, amount, int(emergency), created_at))
            return {"id": reservation_id, "amount": amount, "status": "reserved"}

    def record_external_spend(self, dot_id, reservation_id, actual):
        """Idempotently account for a measured provider call made before bootstrap."""
        dot_id = _identity(dot_id)
        reservation_id = _identity(reservation_id)
        actual = _integer(actual, minimum=1)
        now = _timestamp(time.time())
        with self._connection(write=True) as conn:
            self._dot(conn, dot_id)
            row = conn.execute("SELECT * FROM reservations WHERE id=?",
                               (reservation_id,)).fetchone()
            if row is not None:
                if (row["dot_id"], row["amount"], row["actual"], row["status"]) != (
                        dot_id, actual, actual, "settled"):
                    raise ValueError("seed_spend_conflict")
                return {"id": reservation_id, "amount": actual,
                        "status": "settled", "actual": actual}
            created_at = self._advance_budget(conn, now)
            conn.execute("""INSERT INTO reservations
                (id, dot_id, amount, emergency, created_at, status, actual)
                VALUES(?,?,?,0,?,'settled',?)""",
                         (reservation_id, dot_id, actual, created_at, actual))
            self._stop_overruns(conn)
            return {"id": reservation_id, "amount": actual,
                    "status": "settled", "actual": actual}

    def record_external_hold(self, dot_id, reservation_id, amount):
        """Idempotently retain an unknown pre-bootstrap request reservation."""
        dot_id = _identity(dot_id)
        reservation_id = _identity(reservation_id)
        amount = _integer(amount, minimum=1)
        now = _timestamp(time.time())
        with self._connection(write=True) as conn:
            self._dot(conn, dot_id)
            row = conn.execute("SELECT * FROM reservations WHERE id=?",
                               (reservation_id,)).fetchone()
            if row is not None:
                if (row["dot_id"], row["amount"], bool(row["emergency"])) != (
                        dot_id, amount, False):
                    raise ValueError("seed_hold_conflict")
                return {"id": reservation_id, "amount": row["amount"],
                        "status": row["status"], "actual": row["actual"]}
            created_at = self._advance_budget(conn, now)
            conn.execute("""INSERT INTO reservations
                (id, dot_id, amount, emergency, created_at, status)
                VALUES(?,?,?,0,?,'unknown')""",
                         (reservation_id, dot_id, amount, created_at))
            self._stop_overruns(conn)
            return {"id": reservation_id, "amount": amount,
                    "status": "unknown", "actual": None}

    def settle(self, reservation_id, actual=None):
        reservation_id = _identity(reservation_id)
        if actual is not None:
            _integer(actual)
        with self._connection(write=True) as conn:
            row = conn.execute("SELECT * FROM reservations WHERE id=?",
                               (reservation_id,)).fetchone()
            if row is None:
                raise ValueError("unknown_reservation")
            if row["status"] == "settled":
                if actual != row["actual"]:
                    raise ValueError("settlement_conflict")
            else:
                conn.execute("UPDATE reservations SET status=?, actual=? WHERE id=?",
                             ("unknown" if actual is None else "settled", actual, reservation_id))
                self._stop_overruns(conn)
            return {"id": reservation_id, "amount": row["amount"],
                    "status": "unknown" if actual is None else "settled", "actual": actual}

    def budget_status(self, dot_id=None):
        if dot_id is not None:
            _identity(dot_id)
        with self._connection(write=False) as conn:
            budget = conn.execute("SELECT * FROM budget WHERE id=1").fetchone()
            cap = budget["cap"] if dot_id is None else json.loads(
                self._dot(conn, dot_id)["config"])["budget_micro_usd"]
            spent, held = self._totals(conn, budget["period_start"], dot_id)
            return {"cap": cap, "spent": spent, "held": held,
                    "remaining": cap - spent - held,
                    "emergency_reserve": budget["emergency_reserve"] if dot_id is None else 0,
                    "period_start": budget["period_start"],
                    "period_seconds": budget["period_seconds"],
                    "period_mode": budget["period_mode"], "overrun": spent + held > cap}

    @staticmethod
    def _memory_result(row):
        return {key: row[key] for key in (
            "id", "dot_id", "text", "source_ref", "observed_at", "trust", "revision")}

    def remember(self, dot_id, memory_id, text, source_ref, observed_at, trust="untrusted"):
        dot_id, memory_id = _identity(dot_id), _identity(memory_id)
        observed_at, trust = _timestamp(observed_at), _identity(trust)
        with self._connection(write=True) as conn:
            config = json.loads(self._dot(conn, dot_id)["config"])
            _text(text, config["max_payload_bytes"])
            self._reference(conn, config, source_ref)
            row = conn.execute("SELECT * FROM memories WHERE dot_id=? AND id=?",
                               (dot_id, memory_id)).fetchone()
            if row is not None:
                if (row["text"], row["source_ref"], row["observed_at"], row["trust"]) != (
                        text, source_ref, observed_at, trust):
                    raise ValueError("memory_conflict_use_correct")
                return self._memory_result(row)
            count = conn.execute("SELECT COUNT(*) FROM memories WHERE dot_id=?",
                                 (dot_id,)).fetchone()[0]
            if count >= config["max_queue"]:
                raise ValueError("memory_limit")
            conn.execute("""INSERT INTO memories
                (dot_id, id, text, source_ref, observed_at, trust, revision) VALUES(?,?,?,?,?,?,1)""",
                         (dot_id, memory_id, text, source_ref, observed_at, trust))
            return self._memory_result(conn.execute(
                "SELECT * FROM memories WHERE dot_id=? AND id=?", (dot_id, memory_id)).fetchone())

    def retrieve(self, dot_id, query, limit=5, max_bytes=4096):
        dot_id = _identity(dot_id)
        _text(query, _MAX_PAYLOAD, empty=True)
        limit = _integer(limit, minimum=1, maximum=100)
        max_bytes = _integer(max_bytes, minimum=1, maximum=_MAX_PAYLOAD)
        terms = set(re.findall(r"\w+", query.casefold()))
        with self._connection(write=False) as conn:
            self._dot(conn, dot_id)
            if not terms:
                return []
            ranked = []
            for row in conn.execute("SELECT * FROM memories WHERE dot_id=?", (dot_id,)):
                words = set(re.findall(r"\w+", row["text"].casefold()))
                score = len(terms & words)
                if score:
                    ranked.append((score, row["observed_at"], row["id"], self._memory_result(row)))
            ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
            result = []
            used_bytes = 2  # JSON array brackets; UTF-8 metadata counts as well.
            for _, _, _, record in ranked:
                record_bytes = len(_json(record, _MAX_PAYLOAD + 4096).encode("utf-8"))
                candidate_bytes = used_bytes + record_bytes + bool(result)
                if candidate_bytes <= max_bytes:
                    result.append(record)
                    used_bytes = candidate_bytes
                    if len(result) >= limit:
                        break
            return result

    def correct(self, dot_id, memory_id, text, source_ref="owner"):
        dot_id, memory_id = _identity(dot_id), _identity(memory_id)
        with self._connection(write=True) as conn:
            config = json.loads(self._dot(conn, dot_id)["config"])
            _text(text, config["max_payload_bytes"])
            self._reference(conn, config, source_ref)
            row = conn.execute("SELECT * FROM memories WHERE dot_id=? AND id=?",
                               (dot_id, memory_id)).fetchone()
            if row is None:
                raise ValueError("unknown_memory")
            revision = _integer(row["revision"] + 1, minimum=1)
            conn.execute("UPDATE memories SET text=?, source_ref=?, revision=? WHERE dot_id=? AND id=?",
                         (text, source_ref, revision, dot_id, memory_id))
            return self._memory_result(conn.execute(
                "SELECT * FROM memories WHERE dot_id=? AND id=?", (dot_id, memory_id)).fetchone())

    def seen(self, dot_id, item_id):
        dot_id, item_id = _identity(dot_id), _identity(item_id)
        with self._connection(write=False) as conn:
            self._dot(conn, dot_id)
            return conn.execute("SELECT 1 FROM seen_items WHERE dot_id=? AND id=?",
                                (dot_id, item_id)).fetchone() is not None

    def mark_seen(self, dot_id, item_id):
        dot_id, item_id = _identity(dot_id), _identity(item_id)
        with self._connection(write=True) as conn:
            self._dot(conn, dot_id)
            conn.execute("INSERT OR IGNORE INTO seen_items(dot_id, id) VALUES(?,?)",
                         (dot_id, item_id))

    @staticmethod
    def _effect_result(row):
        return {"id": row["id"], "dot_id": row["dot_id"], "action": row["action"],
                "target_ref": row["target_ref"], "payload": json.loads(row["payload"]),
                "grant_version": row["grant_version"], "status": row["status"]}

    def intent(self, dot_id, effect_id, action, target_ref, payload, grant_version):
        dot_id, effect_id, action = _identity(dot_id), _identity(effect_id), _identity(action)
        grant_version = _integer(grant_version, minimum=1)
        with self._connection(write=True) as conn:
            dot = self._dot(conn, dot_id)
            config = json.loads(dot["config"])
            encoded = _json(payload, config["max_payload_bytes"])
            self._reference(conn, config, target_ref)
            self._payload_scope(json.loads(encoded), dot_id, config)
            row = conn.execute("SELECT * FROM effects WHERE dot_id=? AND id=?",
                               (dot_id, effect_id)).fetchone()
            if row is not None:
                if (row["action"], row["target_ref"], row["payload"], row["grant_version"]) != (
                        action, target_ref, encoded, grant_version):
                    raise ValueError("intent_conflict")
                return self._effect_result(row)
            if action not in config["allowed_actions"] or grant_version != dot["grant_version"]:
                raise PermissionError("intent_grant")
            if dot["paused"]:
                raise PermissionError("dot_paused")
            conn.execute("""INSERT INTO effects
                (dot_id, id, action, target_ref, payload, grant_version) VALUES(?,?,?,?,?,?)""",
                         (dot_id, effect_id, action, target_ref, encoded, grant_version))
            return self._effect_result(conn.execute(
                "SELECT * FROM effects WHERE dot_id=? AND id=?", (dot_id, effect_id)).fetchone())

    def effects(self, dot_id, status=None):
        dot_id = _identity(dot_id)
        if status is not None and (not isinstance(status, str) or status not in _EFFECT_STATES):
            raise ValueError("invalid_effect_status")
        with self._connection(write=False) as conn:
            self._dot(conn, dot_id)
            query = "SELECT * FROM effects WHERE dot_id=?"
            args = [dot_id]
            if status is not None:
                query += " AND status=?"
                args.append(status)
            return [self._effect_result(row) for row in conn.execute(query + " ORDER BY id", args)]

    def effect_status(self, dot_id, effect_id, status, target_receipt=None):
        dot_id, effect_id = _identity(dot_id), _identity(effect_id)
        if not isinstance(status, str) or status not in _EFFECT_STATES:
            raise ValueError("invalid_effect_status")
        if target_receipt is not None:
            _text(target_receipt, 1024)
            if any(ord(char) < 32 for char in target_receipt):
                raise ValueError("invalid_receipt")
            if status != "confirmed":
                raise ValueError("receipt_requires_confirmation")
        with self._connection(write=True) as conn:
            self._dot(conn, dot_id)
            row = conn.execute("SELECT * FROM effects WHERE dot_id=? AND id=?",
                               (dot_id, effect_id)).fetchone()
            if row is None:
                raise ValueError("unknown_effect")
            if _EFFECT_STATES[status] < _EFFECT_STATES[row["status"]]:
                raise ValueError("effect_state_regression")
            if (target_receipt is not None and row["target_receipt"] is not None
                    and target_receipt != row["target_receipt"]):
                raise ValueError("receipt_conflict")
            conn.execute("UPDATE effects SET status=?, target_receipt=? WHERE dot_id=? AND id=?",
                         (status, row["target_receipt"] if target_receipt is None
                          else target_receipt, dot_id, effect_id))
            return self._effect_result(conn.execute(
                "SELECT * FROM effects WHERE dot_id=? AND id=?", (dot_id, effect_id)).fetchone())
