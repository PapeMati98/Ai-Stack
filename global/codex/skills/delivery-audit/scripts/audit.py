"""Asynchronous metadata-only completion audit controller.

Public commands: validate, enqueue, run-one, status, export-confirmed.
Enqueue, TEST_ONLY runs and transport delivery are separate states.
No command bypasses STOP or pin verification through environment variables.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET

sys.dont_write_bytecode = True
SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import audit_core
import audit_graph
import delegation_policy

EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_BLOCKED = 0, 1, 2, 3
HIDDEN_LAUNCH_PATH = audit_graph.HIDDEN_LAUNCH_PATH
HIDDEN_LAUNCH_SHA = audit_graph.HIDDEN_LAUNCH_SHA
FLEET_INSTALL = audit_graph.FLEET_INSTALL
FLEET_FINGERPRINT_SHA = audit_graph.FLEET_FINGERPRINT_SHA
GRAPH_PATH = audit_graph.GRAPH_PATH
OFFICE_ENTRY = Path.home() / ".agents" / "runtime" / "agent-office" / "office.py"
OFFICE_CONFIG = OFFICE_ENTRY.parent / "office-remote.json"
HELPDESK_ENTRY = Path.home() / ".agents" / "skills" / "rsi-helpdesk" / "scripts" / "helpdesk.py"
POWERSHELL = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
CONFIG_FILENAME = "audit-config.json"
OFFICE_CHANNEL = "delivery-audit"
RETRY_BACKOFF_MIN = 30
RETRY_BACKOFF_MAX = 900


@dataclass(frozen=True)
class Runtime:
    """Tests inject effects, never production bypasses or synthetic confirmation."""
    process_runner: object = None
    native_executor: object = None
    office_sink: object = None
    rsi_sink: object = None


@dataclass(frozen=True)
class RunnerConfig:
    enabled: bool = False
    state: str = "STOP"
    office_enabled: bool = False
    rsi_enabled: bool = False
    test_only: bool = False
    office_channel: str = OFFICE_CHANNEL
    rsi_reporter: str = "delivery-audit"
    max_parallel: int = 1
    source: str = "default"
    path: Path | None = None

    @classmethod
    def defaults(cls):
        return cls()

    @classmethod
    def from_env(cls):
        return cls.defaults()  # Ambient environment is never authorization.

    @classmethod
    def from_file(cls, path):
        raw = audit_graph.read_json(_absolute_path(str(path), "config"), audit_core.MAX_BYTES)
        allowed = {"enabled", "state", "office_enabled", "rsi_enabled", "test_only",
                   "office_channel", "rsi_reporter", "max_parallel"}
        if type(raw) is not dict or set(raw) - allowed:
            raise ValueError("AUDIT_CONFIG_INVALID")
        values = {key: raw.get(key, getattr(cls(), key)) for key in allowed}
        for key in ("enabled", "office_enabled", "rsi_enabled", "test_only"):
            if type(values[key]) is not bool:
                raise ValueError("AUDIT_CONFIG_INVALID")
        if (values["state"] not in {"STOP", "RUNNING"} or values["max_parallel"] != 1
                or type(values["max_parallel"]) is not int or values["office_channel"] != OFFICE_CHANNEL):
            raise ValueError("AUDIT_CONFIG_INVALID")
        audit_core._identifier(values["rsi_reporter"])
        if values["test_only"] and (values["office_enabled"] or values["rsi_enabled"]):
            raise ValueError("AUDIT_TEST_TRANSPORT_FORBIDDEN")
        return cls(**values, source="file", path=Path(path))

    @classmethod
    def load(cls, state_dir, override=None):
        if audit_core._safe_path(state_dir / "STOP").exists():
            return cls(source="STOP-file")
        path = _absolute_path(override, "config") if override else state_dir / CONFIG_FILENAME
        if not path.exists():
            if override:
                raise ValueError("AUDIT_CONFIG_UNAVAILABLE")
            return cls.defaults()
        return cls.from_file(path)

    def is_running(self):
        return self.enabled and self.state == "RUNNING"


def _absolute_path(value, label="path"):
    if type(value) is not str or not value or any(ord(c) < 32 for c in value):
        raise ValueError("AUDIT_PATH_INVALID")
    return audit_core._safe_path(value)


def _state_path(value):
    path = _absolute_path(value, "state-dir")
    if not path.is_dir():
        raise ValueError("AUDIT_STATE_UNAVAILABLE")
    if any((ancestor / ".git").exists() for ancestor in (path, *path.parents)):
        raise ValueError("AUDIT_STATE_INSIDE_SOURCE_CHECKOUT")
    return path


def _json_input(payload):
    if len(payload) > audit_core.MAX_BYTES:
        raise ValueError("AUDIT_INPUT_TOO_LARGE")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("AUDIT_DUPLICATE_FIELD")
            result[key] = value
        return result
    try:
        packet = json.loads(payload, object_pairs_hook=unique)
    except (ValueError, UnicodeError):
        raise ValueError("AUDIT_PACKET_INVALID") from None
    return audit_core.validate_packet(packet)


def _read_packet(source):
    if source == "-":
        stream = getattr(sys.stdin, "buffer", sys.stdin)
        payload = stream.read(audit_core.MAX_BYTES + 1)
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
    else:
        path = _absolute_path(source, "packet")
        if not path.is_file() or path.stat().st_size > audit_core.MAX_BYTES:
            raise ValueError("AUDIT_PACKET_UNAVAILABLE")
        payload = path.read_bytes()
    return _json_input(payload)


def verify_runtime_pins():
    return audit_graph.verify_runtime_pins()


def _read_records(state_dir, limit=256, finished_only=True):
    """Read atomic snapshots without creating locks, claims or directories."""
    store = audit_core.AuditStore(state_dir / "state")
    directory = audit_core._safe_path(store.root / "records")
    if not directory.is_dir():
        return []
    records = []
    paths = [audit_core._safe_path(path) for path in directory.glob("*.json")]
    for path in sorted(paths, key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)[:256]:
        record = store._load(audit_core._safe_path(path))
        if not finished_only or record["status"] in audit_core.FINAL_STATUSES:
            records.append(record)
    return sorted(records, key=lambda r: (r["updated_at"], r["audit_id"]), reverse=True)[:limit]


def _accepted_records(state_dir, limit=64):
    records = []
    for record in _read_records(state_dir):
        try:
            audit_graph.load_accepted(state_dir, record)
        except (ValueError, OSError, KeyError, TypeError):
            continue
        record["native_accepted"] = True
        records.append(record)
        if len(records) == limit:
            break
    return records


def _run_process(argv, *, cwd, input=None, runtime=None, timeout=30):
    runtime = runtime or Runtime()
    return (runtime.process_runner or subprocess.run)(
        list(argv), cwd=str(cwd), input=input, capture_output=True, timeout=timeout,
        check=False, env=audit_graph.native_env(),
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)


def build_launch_argv(state_dir, audit_id, action="run-one", owner="audit-worker", config_path=None):
    state_dir = _state_path(str(state_dir))
    audit_core._sha(audit_id)
    audit_core._identifier(owner)
    if action != "run-one":
        raise ValueError("AUDIT_LAUNCH_ACTION_INVALID")
    directory = audit_core._safe_path(state_dir / "launches" / audit_id)
    directory.mkdir(parents=True, exist_ok=True)
    # Detached workers must opt into the existing safe inspect/resolve path so
    # that Graph state with a RUNNING node resumes through the installed public
    # Graph rather than a blind replay or any foreign process/lease change.
    child = ["-B", str(Path(__file__).resolve()), "run-one", "--drain",
             "--recover-interrupted",
             "--state-dir", str(state_dir), "--owner", owner]
    if config_path is not None:
        child.extend(["--config", str(_absolute_path(str(config_path), "config"))])
    root = ET.Element("hidden-launch", {"version": "1", "log-directory": str(directory / "logs"),
                                       "isolate-desktop": "true"})
    ET.SubElement(root, "action", {"executable": str(_absolute_path(sys.executable, "python")),
                                   "arguments": subprocess.list2cmdline(child), "directory": str(state_dir)})
    spec = directory / ("action-" + uuid.uuid4().hex + ".xml")
    with spec.open("xb") as stream:
        stream.write(ET.tostring(root, encoding="utf-8", xml_declaration=True))
        stream.flush()
        os.fsync(stream.fileno())
    return [str(HIDDEN_LAUNCH_PATH), str(spec)]


def detached_launch(argv, *, dry_run=False, runtime=None):
    """WMI service owns the pinned GUI launcher after this caller exits."""
    if (len(argv) != 2 or Path(argv[0]) != HIDDEN_LAUNCH_PATH
            or audit_graph.sha_file(HIDDEN_LAUNCH_PATH) != HIDDEN_LAUNCH_SHA):
        raise ValueError("AUDIT_LAUNCH_PIN_FAILED")
    spec = _absolute_path(argv[1], "launch-spec")
    if not spec.is_file():
        raise ValueError("AUDIT_LAUNCH_SPEC_MISSING")
    if dry_run:
        return {"status": "TEST_ONLY", "mechanism": "NOT_LAUNCHED"}
    if os.name != "nt":
        raise ValueError("AUDIT_WINDOWS_WMI_REQUIRED")
    literal = subprocess.list2cmdline(list(argv)).replace("'", "''")
    script = ("$auditResult=Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
              "-Arguments @{CommandLine='" + literal + "'}; "
              "$auditResult|Select-Object ReturnValue,ProcessId|ConvertTo-Json")
    result = _run_process([str(POWERSHELL), "-NoProfile", "-NonInteractive", "-Command", script],
                          cwd=spec.parent, runtime=runtime, timeout=15)
    try:
        receipt = json.loads(result.stdout.decode("utf-8-sig") if isinstance(result.stdout, bytes) else result.stdout)
    except (ValueError, UnicodeError):
        raise ValueError("AUDIT_WMI_OUTCOME_UNKNOWN") from None
    if result.returncode != 0 or receipt.get("ReturnValue") != 0 or type(receipt.get("ProcessId")) is not int or receipt["ProcessId"] <= 0:
        raise ValueError("AUDIT_WMI_LAUNCH_FAILED")
    return {"status": "LAUNCHED", "mechanism": "WMI-HiddenLaunch-private-desktop",
            "process_id": receipt["ProcessId"], "launcher_sha256": HIDDEN_LAUNCH_SHA}


@contextmanager
def _worker_lock(state_dir):
    lock_path = audit_core._safe_path(state_dir / "worker.lock")
    with lock_path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if not stream.tell():
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError("AUDIT_WORKER_BUSY") from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _release_retryable(store, job):
    with store._lock():
        record = store._load(store._path("records", job["audit_id"] + ".json"))
        if record["status"] == "RUNNING" and record["claim"]["claim_token"] == job["claim_token"]:
            # Existing lease field doubles as a retry-not-before deadline once
            # this owned attempt has returned. Revisions bound the backoff
            # without a retry schema, timer, service or provider polling.
            failures = min(5, max(0, (record["revision"] - 2) // 2))
            delay = min(RETRY_BACKOFF_MAX, RETRY_BACKOFF_MIN * (1 << failures))
            record["claim"]["expires_at"] = audit_core.time.time() + delay
            store._save(record)
            return delay
    return None


def _is_launch_outcome_unknown(exc):
    """Native Graph ``AUDIT_GRAPH_LAUNCH_OUTCOME_UNKNOWN`` is non-retryable.

    The collaborator exposes the same code as ``AuditGraphLaunchUnknown(ValueError)``
    with ``retryable=False`` and ``recovery="inspect-owned-attempt"``; the legacy
    plain ``ValueError("AUDIT_GRAPH_LAUNCH_OUTCOME_UNKNOWN")`` form is also matched
    so the typed schema and the test-only native distinction keep one rule.
    """
    if not isinstance(exc, ValueError):
        return False
    if getattr(exc, "code", None) == "AUDIT_GRAPH_LAUNCH_OUTCOME_UNKNOWN":
        return True
    return str(exc) == "AUDIT_GRAPH_LAUNCH_OUTCOME_UNKNOWN"


def _retained_launch_unknown(store, job, *, code="AUDIT_GRAPH_LAUNCH_OUTCOME_UNKNOWN"):
    """Retain the record as BLOCKED for the launch-unknown failure.

    The finished status makes the record ineligible for ``AuditStore.claim`` while
    keeping every journal/retained snapshot so the owner can attach later proof
    via the bounded inspect/resolve recovery; no blind replay, no silent drop.
    The public diagnostic identifies launch-unknown; original native attempt
    evidence remains untouched. finish owns the single record/token lock.
    """
    identifier = job["audit_id"]
    audit_core._identifier(code)
    packet = job["packet"]
    minimal = {"schema_version": 1,
               "execution_findings": [], "recurrence_findings": [], "findings": [],
               "evidence_refs": [], "runs": [], "model_run_ids": [],
               "harness": packet["harness"]}
    safe_report = audit_core._report(minimal, packet, identifier)
    return store.finish(identifier, job["claim_token"], "BLOCKED", safe_report)


def _transport_json(result):
    if result.returncode != 0:
        raise ValueError("AUDIT_TRANSPORT_UNAVAILABLE")
    try:
        return json.loads(result.stdout)
    except (ValueError, UnicodeError):
        raise ValueError("AUDIT_TRANSPORT_OUTCOME_UNKNOWN") from None


def publish_office(sink, *, channel=OFFICE_CHANNEL, disabled=False, record=None, runtime=None, state_dir=None):
    if disabled:
        return {"status": "DISABLED"}
    if channel != OFFICE_CHANNEL or record is None or state_dir is None:
        raise ValueError("AUDIT_OFFICE_INPUT_INVALID")
    audit_core._sha(sink["dedupe_key"])
    body = {"schema_version": 1, "audit_id": record["audit_id"],
            "task_id": record["packet"]["task_id"], "classification": "accepted-audit",
            "execution_findings": record["report"]["execution_findings"],
            "recurrence_findings": record["report"]["recurrence_findings"],
            "model_run_ids": record["report"]["model_run_ids"], "release_authority": False}
    audit_core._bounded(body)
    result = _run_process([sys.executable, "-I", str(OFFICE_ENTRY), "--remote-config", str(OFFICE_CONFIG),
                           "--harness", "agent-graph", "--session-id", "job-" + record["audit_id"][:48],
                           "send", "--channel", channel, "--kind", "note", "--body-stdin",
                           "--dedupe-key", sink["dedupe_key"]],
                          cwd=state_dir, input=audit_core._json_bytes(body), runtime=runtime)
    receipt = _transport_json(result)
    if type(receipt) is not dict:
        raise ValueError("AUDIT_OFFICE_RECEIPT_INVALID")
    message = receipt.get("message", receipt)
    ref = message.get("id") or message.get("message_id") or receipt.get("message_id")
    if type(ref) is not str or not ref:
        raise ValueError("AUDIT_OFFICE_RECEIPT_INVALID")
    return {"status": "SENT", "receipt_ref": "office-" + hashlib.sha256(ref.encode()).hexdigest()}


def _rsi_symptom(finding):
    return "Confirmed repeated audit finding: " + finding["code"]


def _rsi_signature(finding):
    subject = finding["component"].strip().lower()
    symptom = re.sub(r"\d+", "<n>", _rsi_symptom(finding).lower())
    return hashlib.sha256((subject + "|" + symptom).encode()).hexdigest()[:12]


def _rsi_show(finding, state_dir, runtime):
    result = _run_process([sys.executable, "-B", str(HELPDESK_ENTRY), "show", _rsi_signature(finding)],
                          cwd=state_dir, runtime=runtime)
    if result.returncode == 1 and result.stdout in (b"", ""):
        rows = _transport_json(_run_process([sys.executable, "-B", str(HELPDESK_ENTRY), "list", "--all"],
                                            cwd=state_dir, runtime=runtime))
        if type(rows) is not list or any(not isinstance(row, dict) for row in rows):
            raise ValueError("AUDIT_RSI_STATE_UNKNOWN")
        if not any(row.get("signature") == _rsi_signature(finding) for row in rows):
            return None
        raise ValueError("AUDIT_RSI_STATE_UNKNOWN")
    ticket = _transport_json(result)
    if type(ticket) is not dict or ticket.get("signature") != _rsi_signature(finding):
        raise ValueError("AUDIT_RSI_STATE_UNKNOWN")
    return ticket


def file_rsi_ticket(sink, *, reporter="delivery-audit", disabled=False, record=None, runtime=None, state_dir=None, delivery=None):
    """At-most-once intent; unknown filing reconciles and never blindly replays."""
    if disabled:
        return {"status": "DISABLED"}
    if record is None or state_dir is None or delivery is None:
        raise ValueError("AUDIT_RSI_INPUT_INVALID")
    audit_core._identifier(reporter)
    candidates = [f for f in record["report"]["recurrence_findings"] if f.get("pattern_id") == sink["pattern_id"]
                  and f.get("classification") == "systemic" and f["code"] not in audit_core.TRANSIENT]
    current = {(f["code"], f["component"]) for f in audit_core.execution_findings(record["packet"])}
    if len(candidates) != 1 or (candidates[0]["code"], candidates[0]["component"]) not in current:
        raise ValueError("AUDIT_RSI_CURRENT_FINDING_UNPROVEN")
    finding = candidates[0]
    marker = "filing-" + audit_core._sha(sink["dedupe_key"])
    ticket = _rsi_show(finding, state_dir, runtime)
    if ticket is not None:
        if marker in ticket.get("evidence", []):
            return {"status": "SENT", "receipt_ref": "rsi-" + _rsi_signature(finding)}
        # Existing CLI has no compare-and-set guard; never deliberately reopen
        # or increment an already known ticket, including closed history.
        return {"status": "SUPPRESSED_EXISTING", "receipt_ref": "rsi-" + _rsi_signature(finding)}
    if delivery.get("status") in {"CALLING", "UNKNOWN"}:
        return {"status": "UNKNOWN"}
    delivery["status"] = "CALLING"
    audit_graph.write_json(Path(state_dir) / "delivery" / (sink["dedupe_key"] + ".json"), delivery)
    result = _run_process([sys.executable, "-B", str(HELPDESK_ENTRY), "file", "--component", finding["component"],
                           "--symptom", _rsi_symptom(finding), "--evidence", marker,
                           "--evidence", "audit-" + record["audit_id"],
                           "--impact", "Repeated bounded task audit evidence; manual product state unchanged",
                           "--workaround", "Use the existing RSI repair cycle after independent review",
                           "--reporter", reporter], cwd=state_dir, runtime=runtime)
    receipt = _transport_json(result)
    if type(receipt) is not dict or receipt.get("signature") != _rsi_signature(finding):
        raise ValueError("AUDIT_RSI_OUTCOME_UNKNOWN")
    return {"status": "SENT", "receipt_ref": "rsi-" + _rsi_signature(finding)}


def dispatch_outbox(state_dir, config, runtime):
    """Durable sidecar delivery state; original core outbox is immutable."""
    outcomes = []
    if config.test_only or not config.is_running():
        return outcomes
    for record in _accepted_records(state_dir, limit=256):
        for sink in record["outbox"]:
            kind, key = sink["sink"], sink["dedupe_key"]
            if (kind == "office" and not config.office_enabled) or (kind == "rsi" and not config.rsi_enabled):
                continue
            path = audit_core._safe_path(state_dir / "delivery" / (key + ".json"))
            value = audit_graph.read_json(path, audit_core.MAX_BYTES) if path.exists() else {
                "schema_version": 1, "dedupe_key": key, "audit_id": record["audit_id"], "sink": kind,
                "status": "PENDING", "attempts": 0, "receipt_ref": None}
            if (value.get("dedupe_key") != key or value.get("audit_id") != record["audit_id"]
                    or value.get("sink") != kind or type(value.get("attempts")) is not int
                    or not 0 <= value["attempts"] <= audit_core.MAX_INTEGER
                    or value.get("schema_version") != 1
                    or set(value) != {"schema_version", "dedupe_key", "audit_id", "sink", "status", "attempts", "receipt_ref"}
                    or value.get("status") not in {"PENDING", "CALLING", "UNKNOWN", "SENT", "READ", "ACK", "SUPPRESSED_EXISTING"}):
                raise ValueError("AUDIT_DELIVERY_JOURNAL_INVALID")
            if value["receipt_ref"] is not None:
                audit_core._identifier(value["receipt_ref"])
            if value["status"] in {"SENT", "READ", "ACK", "SUPPRESSED_EXISTING"}:
                continue
            value["attempts"] += 1
            audit_graph.write_json(path, value)
            try:
                if kind == "office":
                    effect = runtime.office_sink or publish_office
                    outcome = effect(sink, channel=config.office_channel, disabled=False, record=record,
                                     runtime=runtime, state_dir=state_dir)
                else:
                    effect = runtime.rsi_sink or file_rsi_ticket
                    outcome = effect(sink, reporter=config.rsi_reporter, disabled=False, record=record,
                                     runtime=runtime, state_dir=state_dir, delivery=value)
                if (type(outcome) is not dict or outcome.get("status") not in {"SENT", "SUPPRESSED_EXISTING", "UNKNOWN"}
                        or set(outcome) - {"status", "receipt_ref"}):
                    raise ValueError("AUDIT_SINK_RECEIPT_INVALID")
                if outcome.get("receipt_ref") is not None:
                    audit_core._identifier(outcome["receipt_ref"])
                value.update(outcome)
            except (ValueError, OSError, subprocess.SubprocessError, TypeError, KeyError):
                value["status"] = "UNKNOWN" if kind == "rsi" and value["status"] == "CALLING" else "PENDING"
            audit_graph.write_json(path, value)
            outcomes.append({"sink": kind, "dedupe_key": key, "status": value["status"]})
    return outcomes


def _print(value):
    print(json.dumps(value, ensure_ascii=True, sort_keys=True))

def cmd_validate(args):
    packet = _read_packet(args.packet)
    delegation_policy.assess_delegation(packet)
    _print({"status": "VALID", "schema_version": 1, "audit_id": audit_core.audit_id(packet),
            "should_audit": audit_core.should_audit(packet)})
    return EXIT_OK


def cmd_enqueue(args):
    packet = _read_packet(args.packet)
    state_dir = _state_path(args.state_dir)
    if not audit_core.should_audit(packet):
        _print({"audit_id": audit_core.audit_id(packet), "status": "SKIPPED", "native_completed": False})
        return EXIT_OK
    config = RunnerConfig.load(state_dir, args.config)
    if not config.is_running():
        _print({"status": "BLOCKED", "code": "AUDIT_CONTROLLER_STOPPED"})
        return EXIT_BLOCKED
    if args.no_launch and not config.test_only:
        raise ValueError("AUDIT_NO_LAUNCH_REQUIRES_TEST_CONFIG")
    pins = verify_runtime_pins()
    if any(value != "present" for value in pins.values()):
        _print({"status": "BLOCKED", "code": "AUDIT_RUNTIME_PIN_FAILED", "pins": pins})
        return EXIT_BLOCKED
    result = audit_core.AuditStore(state_dir / "state").enqueue(packet)
    launch = {"status": "NOT_LAUNCHED"}
    if result["status"] == "QUEUED" and not args.no_launch:
        if config.test_only:
            raise ValueError("AUDIT_TEST_LAUNCH_FORBIDDEN")
        if not RunnerConfig.load(state_dir, args.config).is_running():
            _print({**result, "launch": {"status": "BLOCKED"}, "native_completed": False})
            return EXIT_BLOCKED
        try:
            launch = detached_launch(build_launch_argv(state_dir, result["audit_id"], owner=args.launch_owner,
                                                       config_path=config.path), runtime=args.runtime)
        except (ValueError, OSError, subprocess.SubprocessError):
            launch = {"status": "BLOCKED", "code": "AUDIT_BACKGROUND_LAUNCH_UNAVAILABLE"}
    _print({**result, "launch": launch, "pins": pins, "native_completed": False,
            "verification_class": "TEST_ONLY" if config.test_only else "ENQUEUE_ONLY"})
    return EXIT_BLOCKED if launch["status"] == "BLOCKED" else EXIT_OK


def cmd_run_one(args):
    args._last_claimed = None
    state_dir = _state_path(args.state_dir)
    config = RunnerConfig.load(state_dir, args.config)
    if not config.is_running():
        _print({"status": "BLOCKED", "code": "AUDIT_CONTROLLER_STOPPED"})
        return EXIT_BLOCKED
    if args.dry_run:
        if not config.test_only:
            raise ValueError("AUDIT_DRY_RUN_REQUIRES_TEST_CONFIG")
        _print({"status": "TEST_ONLY", "native_completed": False,
                "queued_records": sum(r["status"] == "QUEUED" for r in _read_records(state_dir, finished_only=False))})
        return EXIT_OK
    pins = verify_runtime_pins()
    if any(value != "present" for value in pins.values()):
        _print({"status": "BLOCKED", "code": "AUDIT_RUNTIME_PIN_FAILED", "pins": pins})
        return EXIT_BLOCKED
    if config.test_only and args.runtime.native_executor is None:
        raise ValueError("AUDIT_TEST_EXECUTOR_REQUIRED")
    audit_core._identifier(args.owner)
    if type(args.ttl) is not int or not 9000 <= args.ttl <= 86400:
        raise ValueError("AUDIT_CLAIM_TTL_INVALID")
    with _worker_lock(state_dir):
        outbox = dispatch_outbox(state_dir, config, args.runtime)
        store = audit_core.AuditStore(state_dir / "state")
        job = store.claim(args.owner, ttl_seconds=args.ttl,
                          excluded_ids=getattr(args, "_drain_failed", ()))
        if job is None:
            _print({"status": "IDLE", "outbox": outbox, "native_completed": False})
            return EXIT_OK
        args._last_claimed = job["audit_id"]
        def allowed():
            try:
                return RunnerConfig.load(state_dir, args.config).is_running()
            except (ValueError, OSError):
                return False
        try:
            effect = args.runtime.native_executor or audit_graph.run_native
            options = {"launch_allowed": allowed}
            if args.recover_interrupted:
                options["recover_interrupted"] = True
            report, proof = effect(job["packet"], _accepted_records(state_dir), state_dir, **options)
            test_only = config.test_only or args.runtime.native_executor is not None or proof.get("verification_class") != "NATIVE_CLI"
            if not allowed():
                raise ValueError("AUDIT_CONTROLLER_STOPPED")
            if test_only:
                finished = store.finish(job["audit_id"], job["claim_token"], "BLOCKED", report)
                _print({"audit_id": job["audit_id"], "status": "TEST_ONLY", "record_status": finished["status"],
                        "native_completed": False, "outbox": []})
                return EXIT_OK
            base = state_dir / "native" / job["audit_id"]
            real_report, real_proof = audit_graph.collect_accepted(base, job["packet"])
            if (report != real_report or proof != real_proof
                    or audit_graph.read_json(base / "acceptance.json", audit_core.MAX_BYTES) != real_proof):
                raise ValueError("AUDIT_RESULT_UNBOUND")
            finished = store.finish(job["audit_id"], job["claim_token"], "COMPLETED", report)
            audit_graph.load_accepted(state_dir, finished)
        except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            if hasattr(args, "_drain_failed"):
                args._drain_failed.add(job["audit_id"])
            if _is_launch_outcome_unknown(exc):
                # Non-retryable: retain the attempt and the BLOCKED status so the
                # owner can attach inspect/resolve proof later. Do not release the
                # claim for re-dispatch and do not blind-replay against Graph or
                # foreign services.
                try:
                    _retained_launch_unknown(store, job)
                except (ValueError, OSError, KeyError, TypeError):
                    pass
                _print({"audit_id": job["audit_id"], "status": "BLOCKED",
                        "code": "AUDIT_GRAPH_LAUNCH_OUTCOME_UNKNOWN",
                        "retryable": False, "recovery": "inspect-owned-attempt",
                        "native_completed": False, "outbox": outbox})
                return EXIT_BLOCKED
            delay = _release_retryable(store, job)
            _print({"audit_id": job["audit_id"], "status": "BLOCKED", "code": "AUDIT_NATIVE_EVIDENCE_UNAVAILABLE",
                    "native_completed": False, "retryable": True, "retry_after_seconds": delay, "outbox": outbox})
            return EXIT_BLOCKED
        outbox.extend(dispatch_outbox(state_dir, RunnerConfig.load(state_dir, args.config), args.runtime))
        _print({"audit_id": finished["audit_id"], "status": "COMPLETED", "verification_class": "NATIVE_CLI",
                "native_completed": True, "outbox": outbox})
        return EXIT_OK


def confirmed_feed(state_dir, limit=64):
    records = []
    for record in _accepted_records(state_dir, limit=limit):
        packet = record["packet"]
        proof = audit_graph.load_accepted(state_dir, record)
        node = next(n for n in proof["nodes"] if n["node_id"] == "recurrence-audit")
        current = {(f["code"], f["component"]) for f in record["report"]["execution_findings"]}
        for finding in record["report"]["recurrence_findings"]:
            if (finding["classification"] != "systemic" or finding["code"] in audit_core.TRANSIENT
                    or (finding["code"], finding["component"]) not in current
                    or not packet["metrics"]["evidence_complete"]):
                continue
            records.append({"schema_version": 1, "producer": "delivery-audit",
                            "record_id": audit_graph.digest([record["audit_id"], finding["pattern_id"]]),
                            "audit_id": record["audit_id"], "project_slug": packet["project_slug"],
                            "task_id": packet["task_id"], "task_family": packet["task_family"], "harness": packet["harness"],
                            "observed_at": datetime.fromtimestamp(record["created_at"], timezone.utc).isoformat().replace("+00:00", "Z"),
                            "input_sha256": record["input_sha256"], "classification": "systemic",
                            "code": finding["code"], "component": finding["component"], "evidence_refs": finding["evidence_refs"],
                            "support": {"tasks": finding["unique_tasks"], "workspaces": finding["unique_workspaces"],
                                        "actor_provider_pairs": finding["actor_provider_pairs"]},
                            "verification": {"status": "CONFIRMED", "recorded_run_ids": node["run_ids"],
                                             "maker_provider": node["maker_provider"], "reviewer_provider": node["reviewer_provider"]},
                            "release_authority": False})
    return {"schema_version": 1, "producer": "delivery-audit", "records": records[:256]}


def cmd_export_confirmed(args):
    state_dir, output = _state_path(args.state_dir), _absolute_path(args.output, "output")
    if type(args.limit) is not int or not 0 <= args.limit <= 256:
        raise ValueError("AUDIT_EXPORT_LIMIT_INVALID")
    protected = (state_dir / "state", state_dir / "native", state_dir / "delivery", state_dir / "launches")
    if (output.exists() or any(output.is_relative_to(root) for root in protected)
            or output == state_dir / CONFIG_FILENAME or output == state_dir / "worker.lock"):
        raise ValueError("AUDIT_EXPORT_TARGET_INVALID")
    payload = confirmed_feed(state_dir, args.limit) if args.limit else {"schema_version": 1, "producer": "delivery-audit", "records": []}
    if not output.parent.is_dir():
        raise ValueError("AUDIT_EXPORT_PARENT_MISSING")
    with output.open("xb") as stream:
        stream.write(audit_core._json_bytes(payload))
    _print({"status": "EXPORTED", "record_count": len(payload["records"]), "native_completed": False})
    return EXIT_OK


def cmd_status(args):
    state_dir = _state_path(args.state_dir)
    try:
        config = RunnerConfig.load(state_dir, args.config)
        mode = "RUNNING" if config.is_running() else "STOP"
    except (ValueError, OSError):
        mode = "UNREADABLE"
    records = _read_records(state_dir, finished_only=False)
    _print({"status": mode, "records": {s: sum(r["status"] == s for r in records)
                                      for s in ("QUEUED", "RUNNING", *sorted(audit_core.FINAL_STATUSES))},
            "native_completed": sum(r["status"] == "COMPLETED" for r in _accepted_records(state_dir))})
    return EXIT_OK


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate", help="Validate typed metadata; no effects")
    validate.add_argument("--packet", required=True)
    validate.set_defaults(func=cmd_validate)
    enqueue = sub.add_parser("enqueue", help="Persist once and launch a detached background worker")
    enqueue.add_argument("--packet", required=True)
    enqueue.add_argument("--state-dir", required=True)
    enqueue.add_argument("--config")
    enqueue.add_argument("--no-launch", action="store_true", help="TEST_ONLY config required")
    enqueue.add_argument("--launch-owner", default="audit-worker")
    enqueue.set_defaults(func=cmd_enqueue)
    worker = sub.add_parser("run-one", aliases=["worker"], help="Run one native audit or resume retained accepted nodes")
    worker.add_argument("--state-dir", required=True)
    worker.add_argument("--config")
    worker.add_argument("--owner", default="audit-worker")
    worker.add_argument("--ttl", type=int, default=14400)
    worker.add_argument("--dry-run", action="store_true", help="TEST_ONLY config required; no claim/model")
    worker.add_argument("--drain", action="store_true", help="Background worker drains queued jobs; stops on STOP/failure")
    worker.add_argument("--recover-interrupted", action="store_true", help="Use installed hash-bound Graph inspect/resolve; never blind replay")
    worker.set_defaults(func=cmd_run_one)
    status = sub.add_parser("status", help="Read-only bounded status")
    status.add_argument("--state-dir", required=True)
    status.add_argument("--config")
    status.set_defaults(func=cmd_status)
    export = sub.add_parser("export-confirmed", aliases=["export"], help="Read-only feed of accepted native systemic observations")
    export.add_argument("--state-dir", required=True)
    export.add_argument("--output", required=True)
    export.add_argument("--limit", type=int, default=64)
    export.set_defaults(func=cmd_export_confirmed)
    return parser


def main(argv=None, *, runtime=None):
    args = build_parser().parse_args(argv)
    args.runtime = runtime or Runtime()
    if args.command in {"run-one", "worker"} and args.drain and not args.dry_run:
        args._drain_failed = set()
    try:
        result = int(args.func(args))
        if args.command in {"run-one", "worker"} and args.drain and not args.dry_run:
            # Drain currently eligible work without waiting for retry deadlines.
            # Each failed audit gets at most one attempt in this process; new
            # queued work and other due recoveries can still make progress.
            while True:
                try:
                    state_dir = _state_path(args.state_dir)
                    config_running = RunnerConfig.load(state_dir, args.config).is_running()
                except (ValueError, OSError):
                    break
                if result != EXIT_OK and args._last_claimed is None:
                    break
                now = audit_core.time.time()
                if not config_running:
                    break
                store = audit_core.AuditStore(state_dir / "state")
                with store._lock():
                    # Queue eligibility has no recent-history limit: failed
                    # recent records cannot hide older unattempted QUEUED work.
                    pending = any(r["audit_id"] not in args._drain_failed
                                  and (r["status"] == "QUEUED" or (r["status"] == "RUNNING"
                                       and r["claim"]["expires_at"] <= now))
                                  for r in store._all())
                if not pending:
                    break
                result = int(args.func(args))
        return result
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError):
        _print({"status": "BLOCKED" if args.command in {"enqueue", "run-one", "worker"} else "INVALID",
                "code": "AUDIT_INPUT_OR_RUNTIME_UNAVAILABLE"})
        return EXIT_BLOCKED if args.command in {"enqueue", "run-one", "worker"} else EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
