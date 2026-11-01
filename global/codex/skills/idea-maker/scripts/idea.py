"""Model-free evidence collection and manual lifecycle of skill proposals."""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import datetime as dt
import hashlib
import heapq
import json
import math
import os
import re
import subprocess
import sys
import unicodedata
import uuid
from pathlib import Path
from typing import Any

SKILL_ROOT = Path(__file__).resolve().parent.parent
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
OPEN = {"PROPOSED", "REVISE"}
STATUSES = OPEN | {"ACCEPTED", "REJECTED", "EXPIRED"}
MAX_ITEMS = 500
SOURCE_LIMITS = {
    "helpdesk": 100,
    "telemetry": 100,
    "catalog": 200,
    "candidates": 100,
    "extra": 20,
    "execution-audit": 100,
    "ab-lesson": 100,
}
MAX_SOURCE_BYTES = 1_000_000
TELEMETRY_TOKENS = ("input_tokens", "fresh_input_tokens", "output_tokens", "cache_read_tokens")
MAX_TELEMETRY_COUNTS = 100
DEFAULT_CONFIG = {
    "enabled": True,
    "workspace": None,  # Each ideator owns its run directory unless explicitly overridden.
    "max_minutes": 90,
    "extra_sources": [],
    "office_cli": str(Path(__file__).resolve().parents[5] / "shared" / "agent-office" / "office.py"),
    "usage_check": str(Path(__file__).resolve().parents[5] / "shared" / "delegation" / "usage_check.py"),
    "audit_cli": str(Path.home() / ".agents" / "skills" / "delivery-audit" / "scripts" / "audit.py"),
    "audit_state_dir": str(Path.home() / ".agents" / "delivery-audit"),
    "ideators": [
        {"name": "codex-sol", "agent": "codex", "model": "gpt-6.1-sol", "lineage": "openai",
         "argv": [r"%USERPROFILE%\.local\bin\codex.cmd", "exec", "-m", "gpt-6.1-sol",
                  "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check",
                  "-C", "{workspace}", "-o", "{run_dir}\\last-message.md", "-"]},
        {"name": "claude-fable", "agent": "claude", "model": "claude-fable-5-1", "lineage": "anthropic",
         "paid_claude": True,
         "argv": [r"%USERPROFILE%\.local\bin\claude.exe", "-p", "--model", "claude-fable-5-1",
                  "--dangerously-skip-permissions", "--add-dir", "{workspace}", "--output-format", "json"]},
    ],
}
SECRET = re.compile(
    r"-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?(?:-----END [^-]*PRIVATE KEY-----|$)"
    r"|\b(?:sk|pk|rk|ghp|gho|ghs|xox[abp]|glpat|AIza)[-_][A-Za-z0-9_-]{12,}"
    r"|\bBearer\s+[A-Za-z0-9._/-]+"
    r"|[\"']?\b(?:password|passwd|pwd|token|secret|api[_-]?key|cookie|authorization)[\"']?\s*[=:]\s*"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
    r"|\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
    r"|\b(?![0-9a-f]{40,}\b)(?=[A-Za-z0-9+/]*\d)(?=[A-Za-z0-9+/]*[A-Za-z])[A-Za-z0-9+/]{40,}={0,2}",
    re.I,
)


class IdeaError(ValueError):
    """Invalid local input: leave the existing store intact."""


class BusyError(IdeaError):
    pass


def home() -> Path:
    return Path(os.environ.get("IDEA_MAKER_HOME") or Path.home() / ".agents" / "idea-maker")


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(dt.timezone.utc) if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def clean(value: Any, limit: int = 160) -> str:
    # Nested values must never be stringified: they can contain prompts/secrets.
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ""
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", str(value))
    return SECRET.sub("<redacted>", text).strip()[:limit]


def flags(group: bool = False) -> int:
    return (CREATE_NO_WINDOW | (CREATE_NEW_PROCESS_GROUP if group else 0)) if os.name == "nt" else 0


def python_env() -> dict[str, str]:
    # Piped Python stdout otherwise inherits the Windows console code page.
    return os.environ.copy() | {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}


def read_json(path: Path) -> Any:
    try:
        if path.stat().st_size > MAX_SOURCE_BYTES:
            raise IdeaError(f"oversized JSON: {path.name}")
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, RecursionError) as exc:
        raise IdeaError(f"unreadable JSON: {path.name}") from exc


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as out:
        out.write(text)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary, path)


def atomic_json(path: Path, data: Any) -> None:
    atomic_text(path, json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


@contextlib.contextmanager
def lock(name: str):
    path = home() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            if handle.read(1) == b"":
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
    except OSError as exc:
        handle.close()
        raise BusyError("busy") from exc
    try:
        yield
    finally:
        if acquired and os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        handle.close()


def config() -> dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        path = home() / "config.json"
        if path.exists():
            override = read_json(path)
            if not isinstance(override, dict):
                raise IdeaError("config root must be an object")
            cfg.update(override)
        if not isinstance(cfg["enabled"], bool):
            raise IdeaError("enabled must be boolean")
        if type(cfg["max_minutes"]) is not int or cfg["max_minutes"] <= 0:
            raise IdeaError("max_minutes must be a positive integer")
        if cfg["workspace"] is not None and (not isinstance(cfg["workspace"], str) or not cfg["workspace"].strip()):
            raise IdeaError("workspace must be null or a non-empty path")
        for key in ("office_cli", "usage_check"):
            if not isinstance(cfg[key], str) or not cfg[key].strip():
                raise IdeaError(f"{key} must be a non-empty path")
        for key in ("audit_cli", "audit_state_dir"):
            if key in cfg and (not isinstance(cfg[key], str) or not cfg[key].strip()):
                raise IdeaError(f"{key} must be a non-empty path")
        if not isinstance(cfg["extra_sources"], list) or not all(
                isinstance(p, str) and p.strip() for p in cfg["extra_sources"]):
            raise IdeaError("extra_sources must be paths")
        if not isinstance(cfg["ideators"], list) or not cfg["ideators"]:
            raise IdeaError("ideators must be a non-empty list")
        for entry in cfg["ideators"]:
            if not isinstance(entry, dict) or not all(
                    isinstance(entry.get(k), str) and entry[k].strip()
                    for k in ("name", "agent", "model", "lineage")):
                raise IdeaError("ideator requires name, agent, model, lineage")
            if not isinstance(entry.get("argv"), list) or not entry["argv"] or not all(
                    isinstance(a, str) and a.strip() for a in entry["argv"]):
                raise IdeaError("ideator argv must be a non-empty list of strings")
            for paid_key in ("paid", "paid_claude"):
                if paid_key in entry and not isinstance(entry[paid_key], bool):
                    raise IdeaError(f"{paid_key} must be boolean")
            for arg in entry["argv"]:
                arg.format(workspace=cfg["workspace"] or "run-dir", run_dir="run-dir", run_id="run-id")
    except (IdeaError, KeyError, IndexError, TypeError, ValueError) as exc:
        return {"enabled": False, "config_error": clean(str(exc), 300)}
    return cfg


def stop_reason(cfg: dict[str, Any] | None = None) -> str | None:
    if (home() / "STOP").exists():
        return "stopped"
    cfg = config() if cfg is None else cfg
    if cfg.get("config_error"):
        return "config-error"
    return None if cfg.get("enabled") else "stopped"


def safe_id(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,119}", value):
        raise IdeaError("invalid id")
    return value


def is_safe_id(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,119}", value))


def is_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-fA-F]{64}", value))


def load_state() -> dict[str, Any]:
    path = home() / "state.json"
    state = read_json(path) if path.exists() else {}
    if not isinstance(state, dict):
        raise IdeaError("state must be an object")
    for key, default in (("last_completed_at", None), ("last_started_at", None),
                         ("active", None), ("rotation", 0), ("history", [])):
        state.setdefault(key, default)
    if type(state["rotation"]) is not int or state["rotation"] < 0 or not isinstance(state["history"], list):
        raise IdeaError("invalid state rotation/history")
    for key in ("last_completed_at", "last_started_at"):
        if state[key] is not None and parse_iso(state[key]) is None:
            raise IdeaError(f"invalid state {key}")
    for record in state["history"]:
        if not isinstance(record, dict) or record.get("status") not in ("COMPLETED", "FAILED") \
                or parse_iso(record.get("finished_at")) is None:
            raise IdeaError("invalid state history entry")
    active = state["active"]
    if active is not None:
        if not isinstance(active, dict) or parse_iso(active.get("started_at")) is None:
            raise IdeaError("invalid active run")
        safe_id(active.get("run_id"))
        if active.get("pid") is not None and (type(active["pid"]) is not int or active["pid"] <= 0):
            raise IdeaError("invalid active pid")
        if active.get("created") is not None and type(active["created"]) is not int:
            raise IdeaError("invalid process identity")
    return state


def save_state(state: dict[str, Any]) -> None:
    state["history"] = state["history"][-100:]
    atomic_json(home() / "state.json", state)


def probe(pid: int) -> tuple[str, int | None]:
    """Access errors mean unknown; only a positively exited process is gone."""
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return "alive", 0
        except ProcessLookupError:
            return "gone", None
        except OSError:
            return "unknown", None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return ("gone", None) if ctypes.get_last_error() == 87 else ("unknown", None)
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code)):
            return "unknown", None
        if code.value != 259:
            return "gone", None
        times = [ctypes.c_ulonglong() for _ in range(4)]
        if not kernel32.GetProcessTimes(ctypes.c_void_p(handle), *[ctypes.byref(t) for t in times]):
            return "unknown", None
        return "alive", int(times[0].value)
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def identity(active: dict[str, Any]) -> str:
    if active.get("pid") is None:
        return "unknown"
    status, created = probe(active["pid"])
    if status == "gone" or (status == "alive" and active.get("created") is not None
                            and created != active["created"]):
        return "ended"
    return "alive" if status == "alive" and active.get("created") is not None else "unknown"


def due(days: int = 14, state: dict[str, Any] | None = None) -> dict[str, Any]:
    if type(days) is not int or days < 1:
        raise IdeaError("days must be positive")
    state = load_state() if state is None else state
    report = {"due": False, "last_completed_at": state["last_completed_at"]}
    if state["active"] is not None and identity(state["active"]) != "ended":
        return report | {"reason": "active-run"}
    current = now()
    completed = parse_iso(state["last_completed_at"])
    if completed is not None and current - completed <= dt.timedelta(days=days):
        return report | {"reason": "not-due"}
    failures = [parse_iso(r["finished_at"]) for r in state["history"] if r["status"] == "FAILED"]
    last_failure = max(failures, default=None)
    if last_failure is not None and current - last_failure < dt.timedelta(hours=24):
        return report | {"reason": "failure-backoff"}
    return report | {"due": True, "reason": "never-completed" if completed is None else "older-than-window"}


def sensitive_path(path: Path) -> bool:
    return any(part.lower() in {".ssh", ".aws", "secrets", "auth", "credentials"}
               or part.lower().startswith((".env", "auth", "credentials"))
               or part.lower().endswith((".pem", ".key", ".pfx")) for part in path.parts)


def telemetry_summary(root: Path, current: dt.datetime) -> tuple[list[dict[str, Any]], int, int]:
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    cutoff = current - dt.timedelta(days=14)
    invalid = rows = 0

    def tally(counts: dict[str, int], value: str) -> None:
        if value in counts or len(counts) < MAX_TELEMETRY_COUNTS:
            counts[value] = counts.get(value, 0) + 1

    for path in sorted(root.glob("*/*.jsonl"))[:MAX_ITEMS]:
        # Identity never crosses journal boundaries, even for identical node IDs.
        makers: dict[tuple[str, int], tuple[str, str, str]] = {}
        rounds: dict[str, int] = {}
        records: list[dict[str, Any]] = []
        try:
            with path.open("r", encoding="utf-8-sig") as stream:
                for _ in range(10000):
                    line = stream.readline(65537)
                    if not line:
                        break
                    if len(line) > 65536:
                        raise IdeaError("oversized telemetry line")
                    try:
                        row = json.loads(line)
                    except ValueError:
                        invalid += 1
                        continue
                    if not isinstance(row, dict):
                        invalid += 1
                        continue
                    stamp = parse_iso(row.get("utc"))
                    event = row.get("event")
                    if stamp is None or stamp > current or not isinstance(event, str) or event not in {
                            "provider_result", "repair", "autopsy", "stage_error", "node_terminal", "stage_start"}:
                        continue
                    node = clean(row.get("node"), 100)
                    round_number = row.get("round")
                    if type(round_number) is int and round_number >= 0:
                        if node:
                            rounds[node] = round_number
                    else:
                        # stage_error/node_terminal can omit round: use that node's
                        # current round (including stage_start before a failed call).
                        round_number = rounds.get(node) if "round" not in row else None
                    if event == "stage_start":
                        continue
                    record = {"utc": stamp, "event": event, "node": node, "round": round_number,
                              "status": clean(row.get("status"), 100) or "unknown",
                              "code": clean(row.get("code"), 100) or "unknown"}
                    if event == "provider_result":
                        identity = tuple(clean(row.get(k), 100) or "unknown" for k in ("agent", "model", "role"))
                        record["identity"] = identity
                        record.update({key: row[key] for key in TELEMETRY_TOKENS
                                       if type(row.get(key)) is int and row[key] >= 0})
                        if identity[2] == "maker" and node and round_number is not None:
                            makers[(node, round_number)] = identity
                    # Retain only whitelist metadata, never the raw row/prompt/diff.
                    records.append(record)
        except (OSError, UnicodeError, IdeaError):
            invalid += 1
        for record in records:
            if record["utc"] < cutoff:
                continue
            event = record["event"]
            key = record.get("identity") if event == "provider_result" else makers.get(
                (record["node"], record["round"]), ("unknown", "unknown", "maker"))
            if key not in groups and len(groups) >= MAX_ITEMS:
                continue
            group = groups.setdefault(key, dict(zip(("agent", "model", "role"), key)) | {
                "runs": 0, "status_counts": {}, "node_status_counts": {},
                "repair": 0, "autopsy": 0, "stage_error": 0,
                "repair_codes": {}, "autopsy_codes": {}, "stage_error_codes": {},
                **dict.fromkeys(TELEMETRY_TOKENS),
            })
            if event == "provider_result":
                group["runs"] += 1
                tally(group["status_counts"], record["status"])
                for token in TELEMETRY_TOKENS:
                    if token in record:
                        group[token] = (group[token] or 0) + record[token]
            elif event == "node_terminal":
                tally(group["node_status_counts"], record["status"])
            else:
                group[event] += 1
                tally(group[event + "_codes"], record["code"])
            rows += 1
    return list(groups.values()), rows, invalid


def collect(out: Path) -> dict[str, Any]:
    cfg = config()
    if cfg.get("config_error"):
        raise IdeaError(cfg["config_error"])
    items: list[dict[str, Any]] = []
    sources: dict[str, dict[str, Any]] = {}

    def emit(kind: str, data: dict[str, Any]) -> None:
        # A large ticket backlog cannot displace the catalog/other evidence kinds.
        if sum(item["source_kind"] == kind for item in items) >= SOURCE_LIMITS.get(kind, 100):
            return
        digest = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
        eid = f"{kind}-{digest}"
        if not any(item["evidence_id"] == eid for item in items):
            items.append({"evidence_id": eid, "source_kind": kind, "untrusted": True, **data})

    def source(kind: str, count: int, invalid: int = 0, available: bool | None = None) -> None:
        is_avail = available if available is not None else (count > 0)
        sources[kind] = {"status": "available" if is_avail else "unavailable", "count": count, "invalid": invalid}

    tickets = Path(os.environ.get("RSI_HELPDESK_HOME") or Path.home() / ".agents" / "helpdesk") / "tickets"
    count = invalid = 0
    for path in sorted(tickets.glob("*.json"))[:MAX_ITEMS]:
        try:
            row = read_json(path)
            if not isinstance(row, dict):
                raise IdeaError("ticket is not an object")
            data = {key: clean(row.get(key), 160 if key == "symptom" else 100)
                    for key in ("id", "component", "model", "status", "last_seen", "symptom")}
            data["count"] = row.get("count") if type(row.get("count")) is int else 0
            emit("helpdesk", data)
            count += 1
        except IdeaError:
            invalid += 1
    source("helpdesk", count, invalid)

    telemetry = Path(os.environ.get("IDEA_MAKER_TELEMETRY") or Path.home() / ".agents" / "telemetry" / "todo")
    current = now()
    groups, rows, invalid = telemetry_summary(telemetry, current)
    for group in groups:
        emit("telemetry", group)
    source("telemetry", rows, invalid)

    catalog = Path(os.environ.get("IDEA_MAKER_CATALOG") or
                   SKILL_ROOT.parent / "select-skills" / "scripts" / "skill_catalog.py")
    count = invalid = 0
    if catalog.is_file():
        try:
            result = subprocess.run([sys.executable, "-B", str(catalog), "--inventory"],
                                    capture_output=True, timeout=60, creationflags=flags(), env=python_env())
            if result.returncode != 0:
                raise IdeaError("catalog failed")
            inventory = json.loads(result.stdout.decode("utf-8"))
            skills = inventory.get("skills") if isinstance(inventory, dict) else None
            if not isinstance(skills, list):
                raise IdeaError("invalid catalog")
            for row in skills[:MAX_ITEMS]:
                if isinstance(row, dict) and isinstance(row.get("name"), str):
                    emit("catalog", {"name": clean(row["name"], 100),
                                     "description": clean(row.get("description"), 1000)})
                    count += 1
        except (OSError, ValueError, subprocess.SubprocessError):
            invalid += 1
    source("catalog", count, invalid)

    count = invalid = 0
    for path in sorted((home() / "candidates").glob("*.json"))[:MAX_ITEMS]:
        try:
            row = read_json(path)
            if not isinstance(row, dict):
                raise IdeaError("invalid candidate")
            emit("candidates", {key: clean(row.get(key), 200 if key == "title" else 100)
                                for key in ("id", "title", "status", "fingerprint")})
            count += 1
        except IdeaError:
            invalid += 1
    source("candidates", count, invalid)

    count = invalid = 0
    for filename in cfg["extra_sources"][:20]:
        path = Path(filename)
        try:
            if sensitive_path(path):
                raise IdeaError("sensitive source refused")
            with path.open("r", encoding="utf-8", errors="replace") as stream:
                text = stream.read(4000)
            # Extra reports are plain text, but a report carrying structured raw
            # prompt/diff fields must not smuggle those fields into evidence.
            if re.search(r"[\"']?\b(?:prompt|system_prompt|messages|diff|patch|password|passwd|token|secret|"
                         r"credentials|cookie|authorization|api[_-]?key)[\"']?\s*[:=]", text, re.I):
                text = "[omitted: sensitive/raw fields in report]"
            emit("extra", {"name": clean(path.name, 100), "text": clean(text, 4000)})
            count += 1
        except (OSError, IdeaError):
            invalid += 1
    source("extra", count, invalid)

    # execution-audit: confirmed audit records via audit.py export-confirmed
    audit_cli_raw = os.environ.get("IDEA_MAKER_AUDIT_CLI") or cfg.get("audit_cli") or str(
        Path.home() / ".agents" / "skills" / "delivery-audit" / "scripts" / "audit.py")
    audit_state_raw = os.environ.get("IDEA_MAKER_AUDIT_STATE") or cfg.get("audit_state_dir") or str(
        Path.home() / ".agents" / "delivery-audit")
    audit_cli = Path(audit_cli_raw)
    audit_state_dir = Path(audit_state_raw)
    count = invalid = 0
    if audit_cli.is_file():
        out.parent.mkdir(parents=True, exist_ok=True)
        audit_out = out.parent / f"audit-export-{uuid.uuid4().hex}.json"
        try:
            result = subprocess.run(
                [sys.executable, "-B", str(audit_cli), "export-confirmed",
                 "--state-dir", str(audit_state_dir.resolve()), "--output", str(audit_out.resolve())],
                capture_output=True, timeout=120, creationflags=flags(), env=python_env()
            )
            if result.returncode != 0:
                raise IdeaError("audit export failed")
            payload = read_json(audit_out)
            if not isinstance(payload, dict):
                raise IdeaError("audit payload not an object")
            if payload.get("schema_version") != 1 or payload.get("producer") != "delivery-audit":
                raise IdeaError("invalid audit feed envelope")
            records = payload.get("records")
            if not isinstance(records, list):
                raise IdeaError("audit records not a list")
            seen_record_ids: set[str] = set()
            for row in records[:256]:
                try:
                    if not isinstance(row, dict):
                        invalid += 1
                        continue
                    if (row.get("schema_version") != 1
                            or type(row.get("schema_version")) is not int
                            or row.get("producer") != "delivery-audit"):
                        invalid += 1
                        continue
                    rec_id = row.get("record_id")
                    if not is_sha256(rec_id):
                        invalid += 1
                        continue
                    if rec_id in seen_record_ids or any(item.get("record_id") == rec_id for item in items):
                        continue

                    audit_id = row.get("audit_id")
                    input_sha256 = row.get("input_sha256")
                    if not is_sha256(audit_id) or not is_sha256(input_sha256):
                        invalid += 1
                        continue

                    project_slug = row.get("project_slug")
                    task_id = row.get("task_id")
                    if not is_safe_id(project_slug) or not is_safe_id(task_id):
                        invalid += 1
                        continue

                    if row.get("release_authority") is not False:
                        invalid += 1
                        continue

                    task_family = clean(row.get("task_family"), 50)
                    harness = clean(row.get("harness"), 50)
                    code = clean(row.get("code"), 100)
                    component = clean(row.get("component"), 100)
                    obs_raw = row.get("observed_at")
                    obs_stamp = parse_iso(obs_raw)

                    if (not task_family or not harness or not code or not component
                            or row.get("classification") != "systemic"
                            or obs_stamp is None or obs_stamp > current):
                        invalid += 1
                        continue

                    refs = row.get("evidence_refs")
                    if not isinstance(refs, list) or not all(is_safe_id(r) for r in refs):
                        invalid += 1
                        continue

                    support_raw = row.get("support")
                    if not isinstance(support_raw, dict):
                        invalid += 1
                        continue
                    tasks = support_raw.get("tasks")
                    workspaces = support_raw.get("workspaces")
                    actor_pairs = support_raw.get("actor_provider_pairs")
                    if not (type(tasks) is int and tasks >= 0
                            and type(workspaces) is int and workspaces >= 0
                            and type(actor_pairs) is int and actor_pairs >= 0):
                        invalid += 1
                        continue
                    support = {"tasks": tasks, "workspaces": workspaces, "actor_provider_pairs": actor_pairs}

                    ver = row.get("verification")
                    if not isinstance(ver, dict):
                        invalid += 1
                        continue
                    if ver.get("status") != "CONFIRMED":
                        invalid += 1
                        continue
                    maker_provider = ver.get("maker_provider")
                    reviewer_provider = ver.get("reviewer_provider")
                    if not is_safe_id(maker_provider) or not is_safe_id(reviewer_provider):
                        invalid += 1
                        continue
                    run_ids = ver.get("recorded_run_ids")
                    if not isinstance(run_ids, list) or len(run_ids) == 0 or not all(is_safe_id(r) for r in run_ids):
                        invalid += 1
                        continue
                    ver_data = {
                        "status": "CONFIRMED",
                        "recorded_run_ids": run_ids[:20],
                        "maker_provider": maker_provider,
                        "reviewer_provider": reviewer_provider,
                    }

                    seen_record_ids.add(rec_id)
                    data = {
                        "schema_version": 1,
                        "producer": "delivery-audit",
                        "record_id": rec_id,
                        "audit_id": audit_id,
                        "project_slug": project_slug,
                        "task_id": task_id,
                        "task_family": task_family,
                        "harness": harness,
                        "observed_at": clean(obs_raw, 50),
                        "input_sha256": input_sha256,
                        "classification": "systemic",
                        "code": code,
                        "component": component,
                        "evidence_refs": refs[:20],
                        "support": support,
                        "verification": ver_data,
                        "release_authority": False,
                    }
                    emit("execution-audit", data)
                    count += 1
                except Exception:
                    invalid += 1
            available = (count > 0) or (len(records) == 0 and invalid == 0)
            source("execution-audit", count, invalid, available=available)
        except (OSError, ValueError, OverflowError, RecursionError, subprocess.SubprocessError, IdeaError):
            source("execution-audit", 0, invalid=1, available=False)
    else:
        source("execution-audit", 0, invalid=0, available=False)

    # ab-lesson: lessons and helped/hurt edits from skill-ab-testing report.json files within 60 days
    ab_home_raw = os.environ.get("TESTY_AB_HOME") or str(Path.home() / ".agents" / "skill-ab-testing")
    ab_home = Path(ab_home_raw)
    count = invalid = 0
    if ab_home.is_dir():
        cutoff = current - dt.timedelta(days=60)
        report_buffer: list[tuple[dt.datetime, str, Path, dict[str, Any]]] = []
        for path in ab_home.glob("**/report.json"):
            try:
                row = read_json(path)
                if not isinstance(row, dict):
                    raise IdeaError("report is not an object")
                stamp = None
                has_stamp_key = False
                for key in ("reported_at", "created_at", "finished_at", "utc", "timestamp"):
                    if key in row:
                        has_stamp_key = True
                        stamp = parse_iso(row.get(key))
                        if stamp is not None:
                            break
                if stamp is None:
                    if has_stamp_key:
                        invalid += 1
                        continue
                    try:
                        stamp = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)
                    except (OSError, OverflowError, ValueError):
                        invalid += 1
                        continue
                if not (cutoff <= stamp <= current):
                    continue
                entry = (stamp, str(path), path, row)
                if len(report_buffer) < 200:
                    heapq.heappush(report_buffer, entry)
                elif (entry[0], entry[1]) > (report_buffer[0][0], report_buffer[0][1]):
                    heapq.heappushpop(report_buffer, entry)
            except (OSError, IdeaError, OverflowError, RecursionError, ValueError):
                invalid += 1
        sorted_reports = sorted(report_buffer, key=lambda item: (item[0], item[1]), reverse=True)
        for stamp, str_path, path, row in sorted_reports:
            try:
                trial_id = clean(row.get("trial_id"), 100)
                target_skill = clean(row.get("target_skill"), 100)

                lesson = row.get("lesson")
                if isinstance(lesson, dict):
                    ev_list = lesson.get("evidence")
                    lesson_data = {
                        "kind": "lesson",
                        "trial_id": trial_id,
                        "target_skill": target_skill,
                        "cue": clean(lesson.get("cue"), 200),
                        "lesson": clean(lesson.get("lesson"), 500),
                        "component": clean(lesson.get("component"), 100),
                        "confidence": clean(lesson.get("confidence"), 50),
                        "evidence": [clean(e, 100) for e in ev_list[:20] if isinstance(e, (str, int, float))]
                                    if isinstance(ev_list, list) else [],
                    }
                    emit("ab-lesson", lesson_data)
                    count += 1

                edits = row.get("edits")
                if isinstance(edits, list):
                    for edit in edits[:50]:
                        if not isinstance(edit, dict):
                            continue
                        verdict = str(edit.get("verdict", "")).strip().lower()
                        if verdict in ("helped", "hurt"):
                            comps = edit.get("components")
                            edit_data = {
                                "kind": "edit",
                                "trial_id": trial_id,
                                "target_skill": target_skill,
                                "variant": clean(edit.get("variant"), 100),
                                "verdict": verdict,
                                "components": [clean(c, 100) for c in comps[:20] if isinstance(c, (str, int, float))]
                                              if isinstance(comps, list) else [],
                            }
                            obs = edit.get("observed")
                            if isinstance(obs, dict):
                                obs_data = {}
                                delta = obs.get("score_delta")
                                if (isinstance(delta, (int, float))
                                        and not isinstance(delta, bool)
                                        and math.isfinite(delta)):
                                    obs_data["score_delta"] = delta
                                if "regressions" in obs:
                                    raw_reg = obs["regressions"]
                                    if isinstance(raw_reg, list):
                                        obs_data["regressions"] = [clean(r, 100) for r in raw_reg[:20]
                                                                   if isinstance(r, (str, int, float))]
                                    elif isinstance(raw_reg, int) and not isinstance(raw_reg, bool):
                                        obs_data["regressions"] = raw_reg
                                edit_data["observed"] = obs_data
                            emit("ab-lesson", edit_data)
                            count += 1
            except (OSError, IdeaError, OverflowError, RecursionError, ValueError):
                invalid += 1
        source("ab-lesson", count, invalid, available=(count > 0))
    else:
        source("ab-lesson", 0, invalid=0, available=False)
    evidence = {"collected_at": iso(current), "sources": sources, "items": items}
    atomic_json(out, evidence)
    return evidence


def required_text(data: dict[str, Any], key: str, limit: int) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise IdeaError(f"{key}: required text, max {limit}")
    return clean(value, limit)


def object_field(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise IdeaError(f"{key}: required object")
    return value


def actor(data: dict[str, Any]) -> dict[str, str]:
    return {key: required_text(data, key, 100) for key in ("agent", "model", "lineage")}


def fingerprint(title: str, problem: str) -> str:
    normalized = [" ".join(unicodedata.normalize("NFKC", text).casefold().split()) for text in (title, problem)]
    return hashlib.sha256(json.dumps(normalized, ensure_ascii=False).encode()).hexdigest()[:16]


def validate_candidate(data: Any, evidence: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or not isinstance(evidence, dict) or not isinstance(evidence.get("items"), list):
        raise IdeaError("candidate/evidence must be objects with evidence items")
    candidate: dict[str, Any] = {key: required_text(data, key, limit)
                               for key, limit in (("title", 200), ("problem", 2000))}
    if data.get("kind") not in ("new", "extend"):
        raise IdeaError("kind must be new or extend")
    candidate["kind"] = data["kind"]
    if data["kind"] == "extend":
        candidate["extend_skill"] = required_text(data, "extend_skill", 100)
    ids = data.get("evidence_ids")
    known = {e.get("evidence_id"): e.get("source_kind") for e in evidence["items"] if isinstance(e, dict)
             and isinstance(e.get("evidence_id"), str) and isinstance(e.get("source_kind"), str)}
    if not isinstance(ids, list) or not 2 <= len(ids) <= 30 or not all(isinstance(i, str) for i in ids) \
            or len(set(ids)) != len(ids) or not set(ids) <= known.keys() \
            or len({known[i] for i in ids}) < 2:
        raise IdeaError("evidence_ids: >=2 unique known ids from >=2 source kinds")
    candidate["evidence_ids"] = ids
    overlap = object_field(data, "overlap")
    nearest = overlap.get("nearest_skills")
    if not isinstance(nearest, list) or len(nearest) > 20 or not all(
            isinstance(s, str) and s.strip() and len(s) <= 100 for s in nearest):
        raise IdeaError("nearest_skills must be a list of skill names")
    candidate["overlap"] = {"nearest_skills": [clean(s, 100) for s in nearest],
                            "why_not_existing": required_text(overlap, "why_not_existing", 2000)}
    gain = object_field(data, "expected_gain")
    candidate["expected_gain"] = {k: required_text(gain, k, 1000) for k in ("metric", "measure_how")}
    anti = object_field(data, "anti_bureaucracy")
    counters = ("new_triggers", "new_reads", "new_model_calls")
    if not all(type(anti.get(k)) is int and anti[k] >= 0 for k in counters):
        raise IdeaError("anti_bureaucracy counters must be non-negative integers")
    candidate["anti_bureaucracy"] = {k: anti[k] for k in counters} | {
        "verdict": required_text(anti, "verdict", 2000)}
    ideator = actor(object_field(data, "ideator"))
    candidate["ideator"] = ideator
    council = object_field(data, "council")
    consultants = council.get("consultants")
    if not isinstance(consultants, list) or len(consultants) != 2 or not all(isinstance(c, dict) for c in consultants):
        raise IdeaError("exactly two consultants required")
    opinions = []
    for consultant in consultants:
        person = actor(consultant)
        if consultant.get("recommendation") not in ("adopt", "revise", "reject"):
            raise IdeaError("invalid consultant recommendation")
        opinions.append(person | {"recommendation": consultant["recommendation"],
                                  "note": required_text(consultant, "note", 2000)})
    lineages = {unicodedata.normalize("NFKC", c["lineage"]).casefold().strip() for c in [ideator, *opinions]}
    if len(lineages) != 3:
        raise IdeaError("consultants must have two distinct lineages other than the ideator")
    if council.get("recommendation") not in ("adopt", "revise", "reject"):
        raise IdeaError("invalid council recommendation")
    candidate["council"] = {"consultants": opinions, "recommendation": council["recommendation"]}
    return candidate


def candidates() -> list[dict[str, Any]]:
    result = []
    for path in sorted((home() / "candidates").glob("*.json")):
        data = read_json(path)
        if not isinstance(data, dict) or not isinstance(data.get("status"), str) or data["status"] not in STATUSES \
                or parse_iso(data.get("expires_at")) is None or not isinstance(data.get("history"), list):
            raise IdeaError(f"invalid stored candidate: {path.name}")
        overlap = object_field(data, "overlap")
        if not isinstance(overlap.get("nearest_skills"), list) or not all(
                isinstance(s, str) for s in overlap["nearest_skills"]):
            raise IdeaError("invalid stored overlap")
        required_text(data, "title", 200)
        required_text(data, "problem", 2000)
        if not isinstance(data.get("evidence_ids"), list) or not all(isinstance(s, str) for s in data["evidence_ids"]):
            raise IdeaError("invalid stored evidence_ids")
        if not all(isinstance(event, dict) for event in data["history"]):
            raise IdeaError("invalid stored history")
        if safe_id(data.get("id")) != path.stem:
            raise IdeaError("candidate id/path mismatch")
        result.append(data)
    return result


def expire(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        if row["status"] in OPEN and parse_iso(row["expires_at"]) <= now():
            row["status"] = "EXPIRED"
            row["history"].append({"at": iso(now()), "status": "EXPIRED", "by": "expiry", "note": "60-day expiry"})
            atomic_json(home() / "candidates" / f"{row['id']}.json", row)


def add(file: Path, evidence_path: Path, run_id: str) -> dict[str, Any]:
    safe_id(run_id)
    # Read only the supplied evidence, never trust extra fields in the candidate.
    candidate = validate_candidate(read_json(file), read_json(evidence_path))
    fp = fingerprint(candidate["title"], candidate["problem"])
    with lock("store.lock"):
        rows = candidates()
        expire(rows)
        if sum(event.get("status") == "PROPOSED" and event.get("run_id") == run_id
               for row in rows for event in row["history"]) >= 3:
            raise IdeaError("max 3 candidates per run_id")
        for row in rows:
            if row.get("fingerprint") != fp:
                continue
            if row["status"] in OPEN:
                raise IdeaError("duplicate open fingerprint")
            rejections = [event.get("evidence_ids", row["evidence_ids"]) for event in row["history"]
                          if event.get("status") == "REJECTED"]
            if row["status"] == "REJECTED":
                rejections.append(row["evidence_ids"])
            if any(not set(candidate["evidence_ids"]) - set(ids) for ids in rejections):
                raise IdeaError("rejected fingerprint requires new evidence")
        created = now()
        cid = f"IDEA-{created:%Y%m%d}-{fp[:6]}"
        path = home() / "candidates" / f"{cid}.json"
        history = []
        # Keep the specified ID format even for a same-day resubmission. Preserve
        # the entire old proposal in the run archive and carry its decision history.
        if path.exists():
            previous = next(row for row in rows if row["id"] == cid)
            if previous["fingerprint"] != fp:
                raise IdeaError("id hash collision")
            archive = home() / "runs" / safe_id(previous["run_id"]) / "candidate-revisions" / (cid + "-" + uuid.uuid4().hex + ".json")
            atomic_json(archive, previous)
            history = previous["history"][:]
        history.append({"at": iso(created), "status": "PROPOSED", "by": run_id,
                        "run_id": run_id, "evidence_ids": candidate["evidence_ids"]})
        candidate.update(id=cid, fingerprint=fp, status="PROPOSED", created_at=iso(created),
                         expires_at=iso(created + dt.timedelta(days=60)), run_id=run_id, history=history)
        atomic_json(path, candidate)
    return candidate


def list_candidates(related: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
    if status and status.upper() not in STATUSES:
        raise IdeaError("invalid candidate status")
    with lock("store.lock"):
        rows = candidates()
        expired = {r["id"] for r in rows if r["status"] in OPEN and parse_iso(r["expires_at"]) <= now()}
        expire(rows)
        rows = [r for r in rows if r["status"] == status.upper()] if status else [
            r for r in rows if r["status"] in OPEN or r["id"] in expired]
        if related:
            key = related.casefold()
            rows = [r for r in rows if key == str(r.get("extend_skill", "")).casefold()
                    or key in [s.casefold() for s in r["overlap"]["nearest_skills"]]
                    or key in r["title"].casefold()]
    return rows


def decide(cid: str, status: str, note: str, by: str) -> dict[str, Any]:
    if status.lower() not in {"accepted", "revise", "rejected"} or not note.strip() or not by.strip():
        raise IdeaError("manual decision requires accepted/revise/rejected, note and by")
    with lock("store.lock"):
        row = next((r for r in candidates() if r["id"] == safe_id(cid)), None)
        if row is None:
            raise IdeaError("candidate not found")
        row["status"] = status.upper()
        row["history"].append({"at": iso(now()), "status": row["status"],
                               "note": clean(note, 2000), "by": clean(by, 100), "evidence_ids": row["evidence_ids"]})
        atomic_json(home() / "candidates" / (cid + ".json"), row)
    return row


def announce(cid: str, dry_run: bool = False) -> dict[str, Any]:
    row = next((r for r in candidates() if r["id"] == safe_id(cid)), None)
    if row is None:
        raise IdeaError("candidate not found")
    body = (f"[{row['id']}] {clean(row['title'], 200)} ({row['status']}, {row['kind']})\n"
            f"Problem: {clean(row['problem'], 300)}\n"
            f"Korzysc: {clean(row['expected_gain']['metric'], 200)}\n"
            f"Dowody: {len(row['evidence_ids'])}; opinia: {row['council']['recommendation']}. "
            "Propozycja do decyzji Mateusza; bez tworzenia, instalacji i deployu skilla.")
    if dry_run:
        return {"result": "dry-run", "body": body}
    cfg = config()
    reason = stop_reason(cfg)
    if reason:
        return {"result": reason}
    cli = Path(cfg["office_cli"])
    if not cli.is_file():
        return {"result": "announce-failed", "reason": "office-unavailable"}
    try:
        result = subprocess.run([sys.executable, "-B", str(cli), "--agent", "idea-maker", "--harness", "script",
                                 "send", "--channel", "skill-candidates", "--kind", "note", "--body-stdin"],
                                input=body.encode("utf-8"), capture_output=True, timeout=60, creationflags=flags(),
                                env=python_env())
        return {"result": "announced" if result.returncode == 0 else "announce-failed",
                "returncode": result.returncode}
    except (OSError, subprocess.SubprocessError):
        return {"result": "announce-failed", "reason": "office-error"}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("due")
    p.add_argument("--days", type=int, default=14)
    p = commands.add_parser("collect")
    p.add_argument("--out", type=Path, required=True)
    p = commands.add_parser("add")
    p.add_argument("--file", type=Path, required=True)
    p.add_argument("--evidence", type=Path, required=True)
    p.add_argument("--run-id", default=os.environ.get("IDEA_MAKER_RUN_ID"))
    p = commands.add_parser("list")
    p.add_argument("--related")
    p.add_argument("--status")
    p.add_argument("--json", action="store_true")
    p = commands.add_parser("decide")
    p.add_argument("--id", required=True)
    p.add_argument("--status", required=True, choices=["accepted", "revise", "rejected"])
    p.add_argument("--note", required=True)
    p.add_argument("--by", required=True)
    p = commands.add_parser("announce")
    p.add_argument("--id", required=True)
    p.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "due":
            result = due(args.days)
        elif args.command == "collect":
            result = collect(args.out)
        elif args.command == "add":
            result = add(args.file, args.evidence, args.run_id or args.evidence.parent.name)
        elif args.command == "list":
            result = list_candidates(args.related, args.status)
            if not args.json:
                for row in result:
                    print(f"{row['id']} | {row['status']} | {row['title']}")
                return 0
        elif args.command == "decide":
            result = decide(args.id, args.status, args.note, args.by)
        else:
            result = announce(args.id, args.dry_run)
            if args.dry_run:
                print(result["body"])
                return 0
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if isinstance(result, dict) and result.get("result") == "announce-failed" else 0
    except (IdeaError, OSError, OverflowError, ValueError) as exc:
        print(json.dumps({"error": clean(str(exc), 300)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
