"""Typed metadata audit API; no subprocess, model, transport, or source reads.

The packet follows the 15 fields enumerated in task-inputs/design.md.
COMPLETED describes an audit result, never transport delivery or RSI repair.
The transport owner receives persistent PENDING outbox entries in records().
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import threading
import time
import uuid

_policy_name = __name__ + "_delegation_policy"
_policy_spec = importlib.util.spec_from_file_location(
    _policy_name, Path(__file__).with_name("delegation_policy.py"))
_policy = importlib.util.module_from_spec(_policy_spec)
sys.modules[_policy_name] = _policy
_policy_spec.loader.exec_module(_policy)

SCHEMA_VERSION = 1
MAX_BYTES = 65536
MAX_RECORDS = 256
MAX_DEPTH = 12
MAX_INTEGER = (1 << 63) - 1
PACKET_FIELDS = frozenset((
    "schema_version", "task_id", "session_id", "project_slug", "harness",
    "task_kind", "task_family", "outcome", "workspace_id", "requirements",
    "runs", "metrics", "delegation", "observations", "evidence_refs",
))
HARNESS = frozenset((
    "codex", "claude", "antigravity", "zcode", "kimi", "grok", "qwen",
    "minimax", "mimo", "mistral", "muse", "openclaw", "other",
))
TASK_KINDS = frozenset((
    "delivery", "repair", "audit", "audit-review", "subtask", "status",
    "metadata-only",
))
TASK_FAMILIES = frozenset(("implementation", "research", "operations", "skill", "other"))
OUTCOMES = frozenset(("COMPLETED", "BLOCKED", "INTERRUPTED", "NO_DURABLE_CHANGE"))
TIERS = frozenset(("D", "M", "K", "W", "unknown"))
CODES = frozenset((
    "missing-delegation", "duplicate-work", "repeat-repair", "missing-checks",
    "missing-uat", "context-snowball", "transport-failure", "schema-mismatch",
    "quota-reset", "provider-transient", "other",
))
TRANSIENT = frozenset(("quota-reset", "provider-transient"))
CLASSES = frozenset(("systemic", "task-specific", "needs-evidence", "transient"))
FINAL_STATUSES = frozenset(("COMPLETED", "FAILED", "BLOCKED", "INTERRUPTED"))
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}", re.ASCII)
_SHA = re.compile(r"[a-f0-9]{64}", re.ASCII)
_PRIVATE = re.compile(
    r"^(?:sk[-_]|pk[-_]|rk[-_]|gh[pousr]_|github_pat_|xox[baprs]-|"
    r"glpat-|AIza|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]+\.|"
    r"Bearer[.:_-]|password[.:_-]|api[_-]?key[.:_-]|cookie[.:_-])", re.I,
)
_IP = re.compile(r"(?:[0-9]{1,3}\.){3}[0-9]{1,3}", re.ASCII)
_RUN_FIELDS = frozenset((
    "run_id", "actor_id", "model_id", "provider", "tier", "role", "status",
    "verified", "input_tokens", "cached_input_tokens", "output_tokens", "tool_calls",
))
_METRIC_FIELDS = frozenset((
    "wall_seconds", "repair_rounds", "duplicate_work", "uat_required",
    "uat_status", "evidence_complete",
))
_DELEGATION_FIELDS = frozenset(("parent_tier", "eligible", "exception", "planned_tasks"))
_COUNT_FIELDS = frozenset((
    "unique_tasks", "unique_workspaces", "unique_actors", "unique_providers",
    "actor_provider_pairs", "total_tasks", "unknown_tasks",
))
_FINDING_FIELDS = frozenset((
    "code", "component", "classification", "evidence_refs", "pattern_id",
)) | _COUNT_FIELDS
_REPORT_FIELDS = frozenset((
    "schema_version", "audit_id", "input_sha256", "execution_findings",
    "recurrence_findings", "findings", "evidence_refs", "runs", "model_run_ids",
    "harness",
))
_THREAD_LOCKS = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _error():
    # Never echo a rejected value, field, path, exception, or private text.
    raise ValueError("Invalid bounded audit metadata")


def _json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def _bounded(value):
    """Check types, cycles, depth and size before serializing untrusted objects."""
    count = 0
    active = set()

    def visit(item, depth):
        nonlocal count
        if depth > MAX_DEPTH:
            _error()
        if type(item) in (dict, list):
            if id(item) in active or len(item) > MAX_RECORDS:
                _error()
            active.add(id(item))
            if type(item) is dict:
                for key, child in item.items():
                    if type(key) is not str or len(key) > 120:
                        _error()
                    visit(child, depth + 1)
            else:
                for child in item:
                    if type(child) is dict:
                        count += 1
                        if count > MAX_RECORDS:
                            _error()
                    visit(child, depth + 1)
            active.remove(id(item))
        elif type(item) is str:
            if len(item) > 120 or not item.isascii():
                _error()
        elif type(item) is int:
            if not 0 <= item <= MAX_INTEGER:
                _error()
        elif type(item) is float:
            if not math.isfinite(item) or not 0 <= item <= MAX_INTEGER:
                _error()
        elif item is not None and type(item) is not bool:
            _error()

    visit(value, 0)
    if len(_json_bytes(value)) > MAX_BYTES:
        _error()


def _fields(value, names):
    if type(value) is not dict or set(value) != set(names):
        _error()


def _identifier(value):
    if (type(value) is not str or not _ID.fullmatch(value) or ".." in value
            or _PRIVATE.search(value) or _IP.fullmatch(value)
            or re.match(r"^[A-Za-z]:", value)):
        _error()
    return value


def _sha(value):
    if type(value) is not str or not _SHA.fullmatch(value):
        _error()
    return value


def _enum(value, choices):
    if type(value) is not str or value not in choices:
        _error()


def _boolean(value, nullable=False):
    if type(value) is not bool and not (nullable and value is None):
        _error()


def _integer(value, nullable=False, upper=MAX_INTEGER):
    if nullable and value is None:
        return
    if type(value) is not int or not 0 <= value <= upper:
        _error()


def _references(value, known=None):
    if type(value) is not list or len(value) > MAX_RECORDS:
        _error()
    for ref in value:
        _identifier(ref)
    if len(value) != len(set(value)) or (known is not None and not set(value) <= known):
        _error()


def _component(value):
    _identifier(value)
    if value in ("fleet", "agent-cli", "agent-graph", "office", "hub"):
        return
    prefix, separator, suffix = value.partition(":")
    if not separator or prefix not in ("skill", "runtime", "launcher", "fleet-adapter"):
        _error()
    _identifier(suffix)


def _runs(runs):
    if type(runs) is not list or len(runs) > MAX_RECORDS:
        _error()
    seen = set()
    for run in runs:
        _fields(run, _RUN_FIELDS)
        for field in ("run_id", "actor_id", "model_id", "provider"):
            _identifier(run[field])
        if run["run_id"] in seen:
            _error()
        seen.add(run["run_id"])
        _enum(run["tier"], TIERS)
        _enum(run["role"], ("planner", "maker", "reviewer", "scout", "integrator"))
        _enum(run["status"], ("completed", "partial", "failed", "blocked", "unknown"))
        _boolean(run["verified"])
        for field in ("input_tokens", "cached_input_tokens", "output_tokens", "tool_calls"):
            _integer(run[field], nullable=True)
        if (run["input_tokens"] is not None and run["cached_input_tokens"] is not None
                and run["cached_input_tokens"] > run["input_tokens"]):
            _error()


def validate_packet(packet):
    """Return an independent canonical dict or raise ValueError before effects."""
    _bounded(packet)
    _fields(packet, PACKET_FIELDS)
    if type(packet["schema_version"]) is not int or packet["schema_version"] != 1:
        _error()
    for field in ("task_id", "session_id", "project_slug"):
        _identifier(packet[field])
    _sha(packet["workspace_id"])
    _enum(packet["harness"], HARNESS)
    _enum(packet["task_kind"], TASK_KINDS)
    _enum(packet["task_family"], TASK_FAMILIES)
    _enum(packet["outcome"], OUTCOMES)
    _references(packet["evidence_refs"])
    known = set(packet["evidence_refs"])
    requirements = packet["requirements"]
    if type(requirements) is not list:
        _error()
    seen = set()
    for requirement in requirements:
        _fields(requirement, ("id", "status", "evidence_refs"))
        _identifier(requirement["id"])
        if requirement["id"] in seen:
            _error()
        seen.add(requirement["id"])
        _enum(requirement["status"], ("pass", "fail", "unknown"))
        _references(requirement["evidence_refs"], known)
    _runs(packet["runs"])
    metrics = packet["metrics"]
    _fields(metrics, _METRIC_FIELDS)
    wall = metrics["wall_seconds"]
    if wall is not None and (type(wall) not in (int, float) or
                             not math.isfinite(wall) or not 0 <= wall <= MAX_INTEGER):
        _error()
    _integer(metrics["repair_rounds"], nullable=True)
    _boolean(metrics["duplicate_work"], nullable=True)
    _boolean(metrics["uat_required"], nullable=True)
    # Null is unknown, not false and never grounds an allegation.
    _boolean(metrics["evidence_complete"], nullable=True)
    _enum(metrics["uat_status"], ("pass", "fail", "unknown", "not-applicable"))
    delegation = packet["delegation"]
    _fields(delegation, _DELEGATION_FIELDS)
    _enum(delegation["parent_tier"], TIERS)
    _boolean(delegation["eligible"], nullable=True)
    _enum(delegation["exception"], (
        "none", "trivial", "credentials", "irreversible", "capability-unavailable",
        "quota-exhausted", "explicit-owner-model", "worker-node",
    ))
    _integer(delegation["planned_tasks"], nullable=True)
    if type(packet["observations"]) is not list:
        _error()
    for observation in packet["observations"]:
        _fields(observation, ("code", "component", "evidence_refs"))
        _enum(observation["code"], CODES)
        _component(observation["component"])
        _references(observation["evidence_refs"], known)
    return json.loads(_json_bytes(packet))


def audit_id(packet):
    """Bind all canonical input metadata, independently of mapping key order."""
    return hashlib.sha256(_json_bytes(validate_packet(packet))).hexdigest()


def should_audit(packet):
    packet = validate_packet(packet)
    return (packet["task_kind"] in ("delivery", "repair")
            and packet["outcome"] == "COMPLETED"
            and packet["delegation"]["exception"] != "worker-node")


def _verified_run(run):
    return (run["verified"] and run["status"] == "completed"
            and run["model_id"] != "unknown" and run["provider"] != "unknown"
            and run["actor_id"] != "unknown" and run["tier"] != "unknown")


def _grounded(packet):
    metrics = packet["metrics"]
    return (metrics["evidence_complete"] is True and bool(packet["evidence_refs"])
            and all(r["status"] != "unknown" and bool(r["evidence_refs"])
                    for r in packet["requirements"])
            and all(_verified_run(run) for run in packet["runs"]))


def execution_findings(packet):
    """Advisory findings from typed facts only; no new acceptance or UAT run."""
    packet = validate_packet(packet)
    findings = {}
    grounded = _grounded(packet)

    def add(code, component, refs):
        key = (code, component)
        refs = sorted(set(refs))
        classification = ("transient" if code in TRANSIENT else
                          "task-specific" if grounded and refs and code != "other"
                          else "needs-evidence")
        if key in findings:
            refs = sorted(set(findings[key]["evidence_refs"]) | set(refs))
        findings[key] = dict(code=code, component=component,
                             classification=classification, evidence_refs=refs)

    for observation in packet["observations"]:
        add(observation["code"], observation["component"], observation["evidence_refs"])
    refs = packet["evidence_refs"]
    metrics = packet["metrics"]
    delegation = packet["delegation"]
    assessment = _policy.assess_delegation(packet)
    if (packet["task_kind"] in ("delivery", "repair")
            and grounded and assessment.finding_code == "missing-delegation"):
        add("missing-delegation", "agent-cli", refs)
    if metrics["duplicate_work"] is True:
        add("duplicate-work", "agent-graph", refs)
    if metrics["repair_rounds"] is not None and metrics["repair_rounds"] >= 2:
        add("repeat-repair", "agent-graph", refs)
    for requirement in packet["requirements"]:
        if requirement["status"] != "pass":
            add("missing-checks", "agent-graph", requirement["evidence_refs"])
    if metrics["evidence_complete"] is not True:
        add("missing-checks", "agent-graph", refs)
    if metrics["uat_required"] and metrics["uat_status"] != "pass":
        add("missing-uat", "agent-graph", refs)
        if metrics["uat_status"] in ("unknown", "not-applicable"):
            findings[("missing-uat", "agent-graph")]["classification"] = "needs-evidence"
    return [findings[key] for key in sorted(findings)]


def _pattern_id(code, component):
    return hashlib.sha256(_json_bytes([code, component])).hexdigest()


def recurrence_findings(current_packet, previous_packets):
    """Only current patterns qualify; historical scans cannot reopen an issue.

    total_tasks is the matching-task denominator; unknown_tasks includes
    conflicting snapshots of the same task. Different CLI/harness labels do
    not establish actor/provider independence.
    """
    current = validate_packet(current_packet)
    if type(previous_packets) is not list or len(previous_packets) > MAX_RECORDS:
        _error()
    previous = [validate_packet(packet) for packet in previous_packets]
    packets = {current["task_id"]: current}
    ambiguous = set()
    for packet in previous:
        task = packet["task_id"]
        if task in packets:
            if audit_id(packet) != audit_id(packets[task]):
                ambiguous.add(task)
        else:
            packets[task] = packet
    current_findings = execution_findings(current)
    all_findings = {task: {(f["code"], f["component"]): f for f in execution_findings(p)}
                    for task, p in packets.items()}
    results = []
    for finding in current_findings:
        key = (finding["code"], finding["component"])
        matching = {task: p for task, p in packets.items() if key in all_findings[task]}
        confirmed = {
            task: p for task, p in matching.items()
            if task not in ambiguous and all_findings[task][key]["classification"] == "task-specific"
        }
        actors = set()
        providers = set()
        pairs = set()
        for packet in confirmed.values():
            for run in packet["runs"]:
                if _verified_run(run):
                    actors.add(run["actor_id"])
                    providers.add(run["provider"])
                    pairs.add((run["actor_id"], run["provider"]))
        tasks = len(confirmed)
        workspaces = len({p["workspace_id"] for p in confirmed.values()})
        current_confirmed = current["task_id"] in confirmed
        if finding["code"] in TRANSIENT:
            classification = "transient"
        elif not current_confirmed:
            classification = "needs-evidence"
        elif tasks >= 3 and workspaces >= 2 and len(actors) >= 2 and len(providers) >= 2:
            classification = "systemic"
        elif tasks >= 3:
            classification = "needs-evidence"
        else:
            classification = "task-specific"
        refs = sorted({ref for task in confirmed for ref in all_findings[task][key]["evidence_refs"]})
        results.append(dict(
            code=finding["code"], component=finding["component"],
            classification=classification, evidence_refs=refs[:MAX_RECORDS],
            pattern_id=_pattern_id(*key), unique_tasks=tasks, unique_workspaces=workspaces,
            unique_actors=len(actors), unique_providers=len(providers),
            actor_provider_pairs=len(pairs), total_tasks=len(matching),
            unknown_tasks=len(matching) - tasks,
        ))
    return results


def _report(report, packet, identifier):
    _bounded(report)
    if type(report) is not dict or not set(report) <= _REPORT_FIELDS:
        _error()
    known = set(packet["evidence_refs"])
    for key, value in report.items():
        if key in ("audit_id", "input_sha256"):
            if _sha(value) != identifier:
                _error()
        elif key == "schema_version":
            if type(value) is not int or value != 1:
                _error()
        elif key == "harness":
            _enum(value, HARNESS)
        elif key == "runs":
            _runs(value)
        elif key in ("evidence_refs", "model_run_ids"):
            _references(value, known if key == "evidence_refs" else None)
        else:
            if type(value) is not list:
                _error()
            for finding in value:
                required = {"code", "component", "classification", "evidence_refs"}
                if (type(finding) is not dict or not required <= set(finding)
                        or not set(finding) <= _FINDING_FIELDS):
                    _error()
                _enum(finding["code"], CODES)
                _component(finding["component"])
                _enum(finding["classification"], CLASSES)
                _references(finding["evidence_refs"],
                            None if key == "recurrence_findings" else known)
                if "pattern_id" in finding and finding["pattern_id"] != _pattern_id(
                        finding["code"], finding["component"]):
                    _error()
                for count in _COUNT_FIELDS & set(finding):
                    _integer(finding[count], upper=MAX_RECORDS * MAX_RECORDS)
                if finding["code"] in TRANSIENT and finding["classification"] != "transient":
                    _error()
    result = copy.deepcopy(report)
    result["input_sha256"] = identifier
    return result


def _safe_path(path):
    """Reject UNC, traversal, links and Windows junction/reparse ancestors."""
    raw = os.fspath(path)
    if (type(raw) is not str or raw.startswith(("\\\\", "//")) or "\x00" in raw
            or any(part in (".", "..") for part in re.split(r"[\\/]", raw))
            or (os.name == "nt" and ":" in raw[2:])):
        _error()
    path = Path(raw)
    if not path.is_absolute():
        _error()
    for ancestor in (path, *path.parents):
        try:
            info = ancestor.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400):
            _error()
    return path


class AuditStore:
    """One exclusive lock, retained inputs/history/results and retryable sinks.

    Construction has no effects. An invalid packet never creates root or lock.
    Stale claims rotate tokens while retaining every journal snapshot. A replay
    of an identical finish retains the original outbox and never repeats work.
    """
    def __init__(self, root):
        self.root = _safe_path(root)
        key = os.path.normcase(str(self.root))
        with _THREAD_LOCKS_GUARD:
            self._thread_lock = _THREAD_LOCKS.setdefault(key, threading.RLock())

    def _path(self, *parts):
        return _safe_path(self.root.joinpath(*parts))

    def _prepare(self):
        for parts in ((), ("inputs",), ("records",), ("journal",)):
            self._path(*parts).mkdir(parents=True, exist_ok=True)

    @contextlib.contextmanager
    def _lock(self):
        with self._thread_lock:
            self._prepare()
            with self._path("store.lock").open("a+b") as lock:
                lock.seek(0, os.SEEK_END)
                if lock.tell() == 0:
                    lock.write(b"\0")
                    lock.flush()
                    os.fsync(lock.fileno())
                deadline = time.monotonic() + 15
                while True:
                    try:
                        lock.seek(0)
                        if os.name == "nt":
                            import msvcrt
                            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise ValueError("Audit store lock unavailable") from None
                        time.sleep(0.02)
                try:
                    yield
                finally:
                    lock.seek(0)
                    if os.name == "nt":
                        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _sync_directory(self, directory):
        if os.name != "nt":
            descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def _immutable(self, path, payload):
        _safe_path(path)
        if path.exists():
            if path.read_bytes() != _json_bytes(payload):
                raise ValueError("Audit immutable snapshot mismatch")
            return
        # Publish only a fully fsynced file; retain pre-publish crash orphans.
        self._atomic(path, payload)

    def _atomic(self, path, payload):
        _safe_path(path)
        temporary = self._path(*path.relative_to(self.root).parts[:-1],
                               path.name + "." + uuid.uuid4().hex + ".tmp")
        with temporary.open("xb") as stream:
            stream.write(_json_bytes(payload))
            stream.flush()
            os.fsync(stream.fileno())
        _safe_path(path)
        os.replace(temporary, path)
        self._sync_directory(path.parent)

    def _save(self, record):
        record["revision"] += 1
        record["updated_at"] = time.time()
        snapshot = self._path("journal", record["audit_id"] + "." +
                              str(record["revision"]) + "." + uuid.uuid4().hex + ".json")
        self._immutable(snapshot, record)
        self._atomic(self._path("records", record["audit_id"] + ".json"), record)

    def _read(self, path):
        _safe_path(path)
        if path.stat().st_size > MAX_BYTES * 4:
            raise ValueError("Audit record exceeds bound")
        try:
            return json.loads(path.read_bytes())
        except (ValueError, UnicodeError):
            raise ValueError("Audit record corrupt") from None

    def _load(self, path):
        record = self._read(path)
        try:
            _fields(record, (
                "schema_version", "audit_id", "input_sha256", "packet", "status",
                "claim", "report", "outbox", "revision", "created_at", "updated_at",
            ))
            identifier = _sha(record["audit_id"])
            if (record["schema_version"] != 1 or path.name != identifier + ".json"
                    or record["input_sha256"] != identifier
                    or audit_id(record["packet"]) != identifier):
                _error()
            if self._read(self._path("inputs", identifier + ".json")) != record["packet"]:
                _error()
            _enum(record["status"], ("QUEUED", "RUNNING", *FINAL_STATUSES))
            _integer(record["revision"])
            for field in ("created_at", "updated_at"):
                if type(record[field]) not in (int, float) or not math.isfinite(record[field]):
                    _error()
            claim = record["claim"]
            if record["status"] != "QUEUED":
                _fields(claim, ("owner", "claim_token", "expires_at"))
                _identifier(claim["owner"])
                _sha(claim["claim_token"])
                if type(claim["expires_at"]) not in (int, float) or not math.isfinite(claim["expires_at"]):
                    _error()
            elif claim is not None:
                _error()
            if record["status"] in FINAL_STATUSES:
                if _report(record["report"], record["packet"], identifier) != record["report"]:
                    _error()
            elif record["report"] is not None or record["outbox"]:
                _error()
            if type(record["outbox"]) is not list or len(record["outbox"]) > MAX_RECORDS:
                _error()
            for sink in record["outbox"]:
                _fields(sink, ("sink", "dedupe_key", "status", "audit_id", "task_id", "pattern_id"))
                _enum(sink["sink"], ("office", "rsi"))
                _enum(sink["status"], ("PENDING", "SENT", "READ", "ACK"))
                _sha(sink["dedupe_key"])
                if sink["audit_id"] != identifier or sink["task_id"] != record["packet"]["task_id"]:
                    _error()
                if sink["pattern_id"] is not None:
                    _sha(sink["pattern_id"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("Audit record corrupt") from None
        return record

    def _all(self):
        # Temporary and journal files are never queue entries.
        paths = sorted(self._path("records").glob("*.json"))
        return [self._load(path) for path in paths]

    def enqueue(self, packet):
        packet = validate_packet(packet)
        identifier = audit_id(packet)
        if not should_audit(packet):
            return dict(audit_id=identifier, status="SKIPPED")
        with self._lock():
            path = self._path("records", identifier + ".json")
            if path.exists():
                self._load(path)
                return dict(audit_id=identifier, status="DUPLICATE")
            self._immutable(self._path("inputs", identifier + ".json"), packet)
            stamp = time.time()
            record = dict(
                schema_version=1, audit_id=identifier, input_sha256=identifier,
                packet=packet, status="QUEUED", claim=None, report=None, outbox=[],
                revision=0, created_at=stamp, updated_at=stamp,
            )
            self._save(record)
        return dict(audit_id=identifier, status="QUEUED")

    def claim(self, owner, ttl_seconds=1800, *, excluded_ids=()):
        _identifier(owner)
        if (type(ttl_seconds) not in (int, float) or not math.isfinite(ttl_seconds)
                or not 0 < ttl_seconds <= 86400):
            _error()
        if type(excluded_ids) not in (tuple, list, set, frozenset):
            _error()
        excluded = {_sha(identifier) for identifier in excluded_ids}
        with self._lock():
            now = time.time()
            # New queued audits make progress before retrying expired attempts;
            # the drain's in-memory exclusions prevent a failure from spinning.
            for record in sorted(self._all(), key=lambda r: (r["status"] != "QUEUED", r["created_at"], r["audit_id"])):
                if record["audit_id"] in excluded:
                    continue
                if (record["status"] == "QUEUED" or
                        (record["status"] == "RUNNING" and record["claim"]["expires_at"] <= now)):
                    record["status"] = "RUNNING"
                    record["claim"] = dict(
                        owner=owner, claim_token=uuid.uuid4().hex + uuid.uuid4().hex,
                        expires_at=now + ttl_seconds,
                    )
                    self._save(record)
                    job = copy.deepcopy(record)
                    job["claim_token"] = record["claim"]["claim_token"]
                    return job
        return None

    def _outbox(self, record, report):
        identifier = record["audit_id"]
        task = record["packet"]["task_id"]

        def sink(name, pattern=None):
            key = [name, identifier] if name == "office" else [name, pattern, task]
            return dict(
                sink=name, dedupe_key=hashlib.sha256(_json_bytes(key)).hexdigest(),
                status="PENDING", audit_id=identifier, task_id=task, pattern_id=pattern,
            )

        outbox = [sink("office")]
        prior = self._all()
        observed = {(p["packet"]["task_id"], f["code"], f["component"])
                    for p in prior if p["status"] in FINAL_STATUSES
                    for f in execution_findings(p["packet"])}
        # A model's systemic assertion alone can never qualify for dispatch.
        grounded = {(f["code"], f["component"])
                    for f in recurrence_findings(record["packet"],
                        [p["packet"] for p in prior if p["status"] == "COMPLETED"][-MAX_RECORDS:])
                    if f["classification"] == "systemic"}
        patterns = set()
        if record["status"] == "COMPLETED":
            for finding in report.get("recurrence_findings", []):
                code, component = finding["code"], finding["component"]
                if (finding["classification"] == "systemic" and (code, component) in grounded
                        and (task, code, component) not in observed):
                    patterns.add(_pattern_id(code, component))
        for pattern in sorted(patterns):
            outbox.append(sink("rsi", pattern))
        return outbox

    def finish(self, audit_id, claim_token, status, report):
        _sha(audit_id)
        _sha(claim_token)
        _enum(status, FINAL_STATUSES)
        _bounded(report)
        with self._lock():
            path = self._path("records", audit_id + ".json")
            if not path.exists():
                raise ValueError("Audit claim unavailable")
            record = self._load(path)
            claim = record["claim"]
            if claim is None or claim["claim_token"] != claim_token:
                raise ValueError("Audit claim unavailable")
            safe_report = _report(report, record["packet"], audit_id)
            if record["status"] in FINAL_STATUSES:
                if record["status"] == status and record["report"] == safe_report:
                    return copy.deepcopy(record)
                raise ValueError("Audit result already retained")
            if record["status"] != "RUNNING" or claim["expires_at"] <= time.time():
                raise ValueError("Audit claim unavailable")
            record["status"] = status
            record["report"] = safe_report
            record["outbox"] = self._outbox(record, safe_report)
            self._save(record)
            return copy.deepcopy(record)

    def records(self, limit=64):
        _integer(limit, upper=MAX_RECORDS)
        if limit == 0:
            return []
        with self._lock():
            finished = [r for r in self._all() if r["status"] in FINAL_STATUSES]
            return copy.deepcopy(sorted(finished, key=lambda r: (
                r["updated_at"], r["audit_id"]), reverse=True)[:limit])
