"""RSI Helpdesk Triage Cycle: automated weekly / daily queue triage.

Runs a lightweight Worker (kind analysis) followed by Director (kind architecture)
to classify untriaged open tickets into DUPLIKAT, NIEAKTUALNE, or ZOSTAW with priority.
Strict fail-closed deterministic validation and safety gate protect secrets and
destructive tickets. Zero deletions, CREATE_NO_WINDOW for all subprocesses.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import re
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any, Callable, Iterable

sys.path.insert(0, str(Path(__file__).parent))
import helpdesk  # noqa: E402
import resolve_cycle  # noqa: E402

SAFETY_KEYWORDS_PATTERN = re.compile(
    # Polish password word forms of "haslo": haslo, hasla, haslem, hasle,
    # haslami (stem has + l) and the genitive plural, which folds to "hasel"
    # (stem has + el). Both stems are covered, so no inflected form of the
    # word escapes the gate.
    r"\b(?:secrets?|sekret\w*|credentials?|passwords?|has(?:l|el)\w*|"
    r"api[\s_-]+key|access[\s_-]+token|bearer|"
    # "iam" counts only as a standalone word: a host or domain name such as
    # "iam.googleapis.com" or "iam-proxy" is not an IAM decision, while
    # "IAM", "uprawnienia IAM" and "rola IAM" are.
    r"(?<![.\w-])iam(?![-\w])(?!\.[A-Za-z0-9])|"
    r"klucz[\s_-]+(?:api|prywatny|dostepu)|uprawnien(?:ia|ie)\s+dostepu|"
    r"kasow\w*|usun\w*|usuw\w*|delet\w*|rm\s*-\s*rf|"
    r"utrata\s+danych|data\s+loss|logik\w*\s+biznesow\w*|business\s+logic|"
    r"kontakt\s+z\s+(?:klientem|ludzmi)|"
    # "platn*" and "paid" are T2 markers only together with a cost word. A bare
    # "platne"/"platnosc"/"paid" is an ordinary billing remark, not a decision
    # reserved for the owner, so it must not drag a normal ticket into needs-owner.
    r"platn\w*\s+(?:\w+\s+){0,2}(?:koszt\w*|cost\w*)|"
    r"(?:koszt\w*|cost\w*)\s+(?:\w+\s+){0,2}platn\w*|"
    r"paid(?:\s+\w+){0,2}\s+(?:koszt\w*|cost\w*)|"
    r"produkcja\s+klienta)\b",
    re.IGNORECASE,
)

DEFAULT_MIN_UNTRIAGED = 10
DEFAULT_WORKER_CANDIDATES = [
    {"agent": "minimax", "model": "MiniMax-M3.1-Flash"},
    {"agent": "antigravity", "model": "gemini-3.8-flash-high"},
]
DEFAULT_WORKER_REVIEWER = [
    {"agent": "grok", "model": "grok-4.7"},
]
# Owner decision 2026-10-24: Kimi K3.1 Pro (k3.1-pro) is tier D and replaces K3
# as the default Director model; Gemini 4 Argon High moved down to tier M.
DEFAULT_DIRECTOR_CANDIDATES = [
    {"agent": "kimi", "model": "k3.1-pro"},
    {"agent": "muse", "model": "muse-wonder-full-1.0"},
]
DEFAULT_DIRECTOR_REVIEWER = [
    {"agent": "codex", "model": "gpt-6.1-sol"},
]


class TriageError(RuntimeError):
    """Base error for triage cycle issues."""


class LockBusyError(TriageError):
    """Another triage cycle is actively running."""


class ValidationError(TriageError):
    """Deterministic validation of worker/director triage proposals failed."""


# ---------------------------------------------------------------- State & Config

def state_path() -> Path:
    return helpdesk.home() / "triage-state.json"


def load_state() -> dict[str, Any]:
    path = state_path()
    if not path.is_file():
        return {"last_run": None, "last_result": None,
                "last_owner_queue_run": None, "owner_queue_last_result": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {
                "last_run": data.get("last_run"),
                "last_result": data.get("last_result"),
                "last_owner_queue_run": data.get("last_owner_queue_run"),
                "owner_queue_last_result": data.get(
                    "owner_queue_last_result", data.get("last_owner_queue_result")),
            }
    except (ValueError, OSError):
        pass
    return {"last_run": None, "last_result": None,
            "last_owner_queue_run": None, "owner_queue_last_result": None}


def save_result(result: str, status: str, *, successful_triage: bool = False,
                successful_owner_queue: bool = False, owner_queue_attempt: bool = False,
                owner_queue_record: dict[str, Any] | None = None,
                t2_waiting: list[str] | None = None,
                recorder_errors: list[str] | None = None) -> dict[str, Any]:
    """Persist a structured result without resetting either successful-run clock.

    ``owner_queue_record`` keeps the owner-queue outcome in its own state field
    (``owner_queue_last_result``); the triage outcome stays in ``last_result``.
    A combined cycle therefore never overwrites the owner-queue report with the
    triage report.
    """
    state = load_state()
    record: dict[str, Any] = {"status": status, "result": result}
    if t2_waiting is not None:
        record["t2_waiting"] = sorted(set(t2_waiting))
    if recorder_errors:
        record["recorder_errors"] = list(recorder_errors)
    state["last_result"] = record
    if successful_triage:
        state["last_run"] = helpdesk.iso(helpdesk.now())
    if successful_owner_queue:
        stamp = helpdesk.iso(helpdesk.now())
        state["last_owner_queue_run"] = stamp
    if owner_queue_record is not None:
        state["owner_queue_last_result"] = owner_queue_record
    elif owner_queue_attempt:
        state["owner_queue_last_result"] = record
    save_state(state)
    return state


def save_state(state: dict[str, Any]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=1)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def triage_config(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    merged_cfg = cfg or resolve_cycle.config()
    t_cfg = merged_cfg.get("triage")
    if not isinstance(t_cfg, dict):
        t_cfg = {}
    return t_cfg


def normalize_candidate(val: Any) -> dict[str, str] | None:
    if isinstance(val, dict) and "agent" in val and "model" in val:
        return {"agent": str(val["agent"]), "model": str(val["model"])}
    if isinstance(val, str) and "/" in val:
        parts = val.split("/", 1)
        return {"agent": parts[0].strip(), "model": parts[1].strip()}
    return None


def normalize_candidates(val: Any, default: list[dict[str, str]]) -> list[dict[str, str]]:
    if not val:
        return list(default)
    if isinstance(val, (list, tuple)):
        out = []
        for x in val:
            c = normalize_candidate(x)
            if c:
                out.append(c)
        return out if out else list(default)
    c = normalize_candidate(val)
    return [c] if c else list(default)


# ---------------------------------------------------------------- Due & Lock

def untriaged_open_tickets() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Returns (all_open, untriaged_open)."""
    all_open = [t for t in helpdesk.all_tickets() if t.get("status") == "open"]
    untriaged = [t for t in all_open if helpdesk.priority_of(t) is None]
    return all_open, untriaged


def triage_lock_is_busy() -> bool:
    """Unknown lock/process identity fails closed; a recycled PID is detected when possible."""
    lock_path = helpdesk.home() / "triage.lock"
    if not lock_path.exists():
        return False
    try:
        raw = lock_path.read_text(encoding="utf-8").lstrip("\0").strip()
        if not raw:
            return True
        lock_data = json.loads(raw)
        if not isinstance(lock_data, dict):
            return True
        if lock_data.get("status") != "active":
            return False
        pid = lock_data.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool):
            return True
        status, created = resolve_cycle.probe(pid)
        recorded_created = lock_data.get("created")
        if status == "unknown":
            return True
        return status == "alive" and (recorded_created is None or created == recorded_created)
    except (ValueError, OSError, AttributeError):
        return True


def is_due(cfg: dict[str, Any] | None = None, force_due: bool = False) -> tuple[bool, str]:
    """Check if triage should run according to due policy."""
    resolved_cfg = cfg or resolve_cycle.config()
    if (helpdesk.home() / "STOP").exists():
        return False, "stopped"
    if resolved_cfg.get("config_error"):
        return False, "config-error"
    if not resolved_cfg.get("enabled", True):
        return False, "stopped"

    if triage_lock_is_busy():
        return False, "locked: active or unreadable triage lock"

    all_open, untriaged = untriaged_open_tickets()
    untriaged_count = len(untriaged)

    if force_due:
        return True, f"forced: {untriaged_count} untriaged"

    if untriaged_count == 0:
        return False, "not-due: 0 untriaged"

    t_cfg = triage_config(resolved_cfg)
    min_untriaged = int(t_cfg.get("min_untriaged", DEFAULT_MIN_UNTRIAGED))

    state = load_state()
    last_run_iso = state.get("last_run")
    last_run_dt = helpdesk.parse_iso(last_run_iso) if last_run_iso else None
    now_dt = helpdesk.now()

    if last_run_dt is not None:
        hours_since = (now_dt - last_run_dt).total_seconds() / 3600.0
        days_since = (now_dt - last_run_dt).total_seconds() / 86400.0
    else:
        # If never run, check age of oldest untriaged ticket
        oldest_ts = None
        for t in untriaged:
            ts = helpdesk.parse_iso(t.get("first_seen"))
            if ts and (oldest_ts is None or ts < oldest_ts):
                oldest_ts = ts
        if oldest_ts:
            hours_since = (now_dt - oldest_ts).total_seconds() / 3600.0
            days_since = (now_dt - oldest_ts).total_seconds() / 86400.0
        else:
            hours_since = 999.0
            days_since = 999.0

    if hours_since >= 24.0 and untriaged_count >= min_untriaged:
        return True, f"due: {untriaged_count} untriaged >= {min_untriaged} and {hours_since:.1f}h >= 24h"
    if days_since >= 7.0 and untriaged_count >= 1:
        return True, f"due: {untriaged_count} untriaged >= 1 and {days_since:.1f}d >= 7d"

    return False, f"not-due: {untriaged_count} untriaged, {hours_since:.1f}h since last run"


@contextlib.contextmanager
def triage_lock():
    """Exclusive triage lock: an OS byte lock plus a readable metadata file.

    The byte lock lives in its own guard file (triage.lock.guard) because a byte
    locked region cannot be read on Windows, and the metadata (pid, start time,
    process creation time) has to stay readable for the owner and for
    triage_lock_is_busy. The guard byte is locked before the metadata file is
    opened for writing, so a process that does not get the lock never writes or
    moves anything. No file is deleted: the lock is released by overwriting the
    metadata and unlocking the guard byte.
    """
    lock_path = helpdesk.home() / "triage.lock"
    guard_path = helpdesk.home() / "triage.lock.guard"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    # The guard file keeps one byte that is locked for the whole cycle.
    guard = open(guard_path, "a+b")
    handle = None
    got_lock = False
    active_written = False
    try:
        if os.name == "nt":
            import msvcrt
            import time
            guard.seek(0, os.SEEK_END)
            if guard.tell() == 0:
                guard.write(b"\0")
                guard.flush()
            for _ in range(20):
                try:
                    guard.seek(0)
                    msvcrt.locking(guard.fileno(), msvcrt.LK_NBLCK, 1)
                    got_lock = True
                    break
                except OSError:
                    time.sleep(0.05)
            else:
                raise LockBusyError("triage.lock OS byte lock busy")
        else:
            import fcntl
            try:
                fcntl.flock(guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                got_lock = True
            except OSError:
                raise LockBusyError("triage.lock flock busy")

        # Only the lock holder may open the metadata file for writing.
        try:
            handle = open(lock_path, "r+b")
        except FileNotFoundError:
            with open(lock_path, "a+b"):
                pass
            handle = open(lock_path, "r+b")
        handle.seek(0)
        prior_raw = handle.read().decode("utf-8", errors="replace").lstrip("\0")
        if prior_raw.strip():
            try:
                prior = json.loads(prior_raw)
            except ValueError as exc:
                raise LockBusyError("triage.lock metadata is unreadable") from exc
            if not isinstance(prior, dict):
                raise LockBusyError("triage.lock metadata is unreadable")
            if prior.get("status") == "active":
                pid = prior.get("pid")
                if not isinstance(pid, int) or isinstance(pid, bool):
                    raise LockBusyError("triage lock has no valid process identity")
                p_status, created = resolve_cycle.probe(pid)
                recorded_created = prior.get("created")
                if p_status == "unknown" or (p_status == "alive" and
                        (recorded_created is None or created == recorded_created)):
                    raise LockBusyError(f"triage lock held by active process {pid}")

        lock_info = {
            "pid": os.getpid(),
            "created": resolve_cycle.process_identity(os.getpid()),
            "status": "active",
            "started_at": helpdesk.iso(helpdesk.now()),
        }
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(lock_info).encode("utf-8"))
        handle.flush()
        active_written = True

        yield lock_info

    finally:
        if handle is not None and active_written:
            try:
                handle.seek(0)
                handle.truncate()
                handle.write(json.dumps({
                    "pid": os.getpid(),
                    "created": resolve_cycle.process_identity(os.getpid()),
                    "status": "released",
                    "released_at": helpdesk.iso(helpdesk.now()),
                }).encode("utf-8"))
                handle.flush()
            except OSError:
                pass
        if handle is not None:
            with contextlib.suppress(OSError):
                handle.close()
        if got_lock:
            if os.name == "nt":
                import msvcrt
                with contextlib.suppress(OSError):
                    guard.seek(0)
                    msvcrt.locking(guard.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                with contextlib.suppress(OSError):
                    fcntl.flock(guard.fileno(), fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            guard.close()


# ---------------------------------------------------------------- Safety Gate

# NFKD has no canonical decomposition for the stroked l, so a written "hasla" or
# "platne" would never match the ascii-only patterns below; fold it to "l".
_POLISH_LETTERS = str.maketrans({
    "ą": "a", "ć": "c", "ę": "e", "ł": "l", "ń": "n",
    "ó": "o", "ś": "s", "ź": "z", "ż": "z",
})


def _normalized_safety_text(value: Any) -> str:
    decomposed = unicodedata.normalize("NFKD", str(value or "")).casefold()
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    return stripped.translate(_POLISH_LETTERS)


def is_t2_ticket(ticket: dict[str, Any]) -> bool:
    """One T2 gate for triage and owner-queue, across fields and full history."""
    hist = ticket.get("history") or []
    notes = " ".join(str(h.get("note", "")) for h in hist if isinstance(h, dict))
    fields = [
        str(ticket.get("component") or ""),
        str(ticket.get("symptom") or ""),
        str(ticket.get("impact") or ""),
        str(ticket.get("workaround") or ""),
        notes,
    ]
    normalized = _normalized_safety_text(" ".join(fields))
    return bool(SAFETY_KEYWORDS_PATTERN.search(normalized))


def is_sensitive_ticket(ticket: dict[str, Any], last_note: str = "") -> bool:
    """Compatibility alias for the shared T2 gate; all history is always included."""
    if not last_note:
        return is_t2_ticket(ticket)
    augmented = dict(ticket)
    augmented["history"] = list(ticket.get("history") or []) + [{"note": last_note}]
    return is_t2_ticket(augmented)


def ticket_last_history_ts(ticket: dict[str, Any]) -> dt.datetime | None:
    hist = ticket.get("history") or []
    if hist and isinstance(hist[-1], dict) and hist[-1].get("ts"):
        return helpdesk.parse_iso(hist[-1]["ts"])
    if ticket.get("updated_at"):
        return helpdesk.parse_iso(ticket["updated_at"])
    if ticket.get("first_seen"):
        return helpdesk.parse_iso(ticket["first_seen"])
    return None


def is_older_than_24h(ticket: dict[str, Any], now_dt: dt.datetime) -> bool:
    ts = ticket_last_history_ts(ticket)
    if ts is None:
        return False
    return (now_dt - ts).total_seconds() >= 24.0 * 3600.0


def owner_queue_waiting_t2() -> list[dict[str, Any]]:
    """Parked T2 tickets are reported to the owner and never sent to Director."""
    return [t for t in helpdesk.all_tickets()
            if t.get("status") in {"candidate", "needs-owner"} and is_t2_ticket(t)]


def t2_waiting_ticket_ids() -> list[str]:
    """Ids of the T2 tickets that wait for the owner.

    Both the parked ones (candidate / needs-owner) and the still open ones are
    listed: the triage leaves an open T2 ticket untouched (no status, no
    priority) and only reports it, so the owner sees it in the daily report.
    """
    ids = [str(t.get("id") or "") for t in owner_queue_waiting_t2()]
    for t in helpdesk.all_tickets():
        if t.get("status") == "open" and is_t2_ticket(t):
            ids.append(str(t.get("id") or ""))
    return sorted({i for i in ids if i})


def is_owner_queue_due(cfg: dict[str, Any] | None = None) -> tuple[bool, str]:
    """Owner queue runs once per six hours when a non-T2 parked ticket is 24h old."""
    resolved_cfg = cfg or resolve_cycle.config()
    if (helpdesk.home() / "STOP").exists():
        return False, "stopped"
    if resolved_cfg.get("config_error"):
        return False, "config-error"
    if not resolved_cfg.get("enabled", True):
        return False, "stopped"
    if triage_lock_is_busy():
        return False, "locked: active or unreadable triage lock"
    candidates, _ = get_owner_queue_candidates()
    if not candidates:
        return False, "not-due: no aged non-T2 owner-queue tickets"
    last_iso = load_state().get("last_owner_queue_run")
    last_run = helpdesk.parse_iso(last_iso) if last_iso else None
    if last_run is not None:
        hours_since = (helpdesk.now() - last_run).total_seconds() / 3600.0
        if hours_since < 6.0:
            return False, f"not-due: owner-queue ran {hours_since:.1f}h ago (< 6h)"
    return True, f"due: {len(candidates)} aged non-T2 ticket(s); owner-queue interval >= 6h"


def get_owner_queue_candidates(now_dt: dt.datetime | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Returns (eligible_tickets_max_30, t2_skipped_tickets).
    Filters candidate and needs-owner tickets older than 24h, excludes T2 tickets, caps at 30.
    """
    now_ts = now_dt or helpdesk.now()
    matching: list[dict[str, Any]] = []
    for t in helpdesk.all_tickets():
        if t.get("status") in {"candidate", "needs-owner"} and not t.get("claim"):
            if is_older_than_24h(t, now_ts):
                matching.append(t)

    matching.sort(key=lambda x: str(x.get("id") or ""))

    eligible: list[dict[str, Any]] = []
    t2_skipped: list[dict[str, Any]] = []
    for t in matching:
        if is_t2_ticket(t):
            t2_skipped.append(t)
        else:
            eligible.append(t)

    return eligible[:30], t2_skipped


# ---------------------------------------------------------------- Workspace

BRIEF_CONTENT = """# RSI Helpdesk Triage Brief

Kategorie dla kazdego zgloszenia z untriaged=true:
1. DUPLIKAT:
   - Zgloszenie opisuje te sama usterke co inne zgloszenie open.
   - canonical: ID kanonicznego zgloszenia open.
   - priorytet: null.
2. NIEAKTUALNE:
   - Zgloszenie dotyczy problemu juz nieaktualnego / naprawionego / nieodtwarzalnego w biezacym stacku.
   - canonical: null.
   - priorytet: null.
   - BRAMKA BEZPIECZENSTWA: zgloszenia dotyczace sekretow, credentiali, IAM/uprawnien, kasowania/usuwania
     plikow/danych, logiki biznesowej, kontaktu z ludzmi i nowych kosztow NIGDY nie sa nieaktualne!
3. ZOSTAW:
   - Zgloszenie aktywne, wymaga rozwiazania.
   - canonical: null.
   - priorytet: P1, P2 lub P3 (ZOSTAW musi miec priorytet).

Format wyjsciowy: plik triage.json zawierajacy tablice obiektow:
[
  {
    "id": "HD-...",
    "kategoria": "DUPLIKAT" | "NIEAKTUALNE" | "ZOSTAW",
    "canonical": "HD-..." | null,
    "priorytet": "P1" | "P2" | "P3" | null,
    "powod": "krotki powod (max 160 znakow bez polskich znakow diakrytycznych)"
  }
]
"""


def prepare_workspace(triage_dir: Path, all_open: list[dict[str, Any]]) -> tuple[Path, Path]:
    """Create BRIEF.md, open-tickets.json, git init repo + commit, and detached worktree."""
    triage_dir.mkdir(parents=True, exist_ok=True)
    repo_dir = triage_dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    maker_worktree = triage_dir / "maker-worktree"

    # Write BRIEF.md
    brief_path = repo_dir / "BRIEF.md"
    brief_path.write_text(BRIEF_CONTENT, encoding="utf-8")

    # Write open-tickets.json
    open_tickets_payload = []
    for t in sorted(all_open, key=lambda x: str(x.get("id", ""))):
        hist = t.get("history") or []
        last_note = str(hist[-1].get("note") if hist and isinstance(hist[-1], dict) else "")[:200]
        symptom = str(t.get("symptom") or "")[:300]
        is_untriaged = helpdesk.priority_of(t) is None
        open_tickets_payload.append({
            "id": str(t.get("id") or ""),
            "signature": str(t.get("signature") or ""),
            "component": str(t.get("component") or ""),
            "model": t.get("model") or None,
            "first_seen": str(t.get("first_seen") or ""),
            "last_seen": str(t.get("last_seen") or ""),
            "count": int(t.get("count", 0) or 0),
            "symptom": symptom,
            "last_note": last_note,
            "untriaged": is_untriaged,
        })
    tickets_path = repo_dir / "open-tickets.json"
    tickets_path.write_text(json.dumps(open_tickets_payload, ensure_ascii=False, indent=1), encoding="utf-8")

    # Git init + commit + detached worktree
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = "RSI Triage"
    env["GIT_AUTHOR_EMAIL"] = "triage@localhost"
    env["GIT_COMMITTER_NAME"] = "RSI Triage"
    env["GIT_COMMITTER_EMAIL"] = "triage@localhost"
    cflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    subprocess.run(["git", "init"], cwd=repo_dir, env=env, check=True, capture_output=True,
                   creationflags=cflags, timeout=60)
    subprocess.run(["git", "add", "-A"], cwd=repo_dir, env=env, check=True, capture_output=True,
                   creationflags=cflags, timeout=60)
    subprocess.run(["git", "commit", "-m", "initial triage setup"], cwd=repo_dir, env=env, check=True,
                   capture_output=True, creationflags=cflags, timeout=60)
    subprocess.run(["git", "worktree", "add", "--detach", str(maker_worktree), "HEAD"], cwd=repo_dir, env=env,
                   check=True, capture_output=True, creationflags=cflags, timeout=60)

    return repo_dir, maker_worktree


# ---------------------------------------------------------------- Worker & Director Requests

def build_worker_request(maker_worktree: Path, cfg: dict[str, Any]) -> dict[str, Any]:
    t_cfg = triage_config(cfg)
    worker_candidates = normalize_candidates(t_cfg.get("worker"), DEFAULT_WORKER_CANDIDATES)
    worker_reviewer = normalize_candidates(t_cfg.get("worker_reviewer"), DEFAULT_WORKER_REVIEWER)
    return {
        "goal": "RSI Helpdesk porzadkowanie kolejki zgloszen (triaz)",
        "role": "maker",
        "workspace": str(maker_worktree),
        "task": {
            "text": (
                "Przeanalizuj open-tickets.json wedlug zasad w BRIEF.md i utworz triage.json w katalogu roboczym. "
                "triage.json ma zawierac dokladnie jeden obiekt dla kazdego zgloszenia z untriaged=true: "
                "{id, kategoria, canonical, priorytet, powod}. "
                "Kategorie: DUPLIKAT (canonical: ID innego open, priorytet: null), "
                "NIEAKTUALNE (canonical: null, priorytet: null), "
                "ZOSTAW (canonical: null, priorytet: P1|P2|P3). "
                "powod: maksymalnie 160 znakow bez polskich znakow diakrytycznych."
            ),
            "mode": "implement",
            "kind": "analysis",
            "scope": ["triage.json"],
            "inputs": ["open-tickets.json", "BRIEF.md"],
            "done_when": [
                "Plik triage.json istnieje i zawiera dokladnie jeden obiekt dla kazdego untriaged zgloszenia",
                "Kazdy obiekt ma wlasciwe pola id, kategoria, canonical, priorytet, powod",
            ],
            "constraints": [
                "Tylko zgloszenia z untriaged=true w triage.json",
                "powod do 160 znakow",
                "Nie zamykaj zgloszen o sekretach, IAM, kasowaniu plikow jako NIEAKTUALNE",
            ],
            "maker": worker_candidates,
            "reviewer": worker_reviewer,
        },
        "constraints": ["No file deletion"],
    }


def build_director_request(maker_worktree: Path, cfg: dict[str, Any]) -> dict[str, Any]:
    t_cfg = triage_config(cfg)
    director_candidates = normalize_candidates(t_cfg.get("director"), DEFAULT_DIRECTOR_CANDIDATES)
    director_reviewer = normalize_candidates(t_cfg.get("director_reviewer"), DEFAULT_DIRECTOR_REVIEWER)
    return {
        "goal": "RSI Helpdesk decyzja Dyrektora dla kandydata triazu zgloszen",
        "role": "director",
        "workspace": str(maker_worktree),
        "task": {
            "text": (
                "Przejrzyj propozycje triazu w triage-view.tsv dla otwartych zgloszen RSI Helpdesk. "
                "Wydaj werdykt: ZATWIERDZAM, ZATWIERDZAM Z POPRAWKAMI albo ODRZUCAM. "
                "Werdykt musi byc pierwsza linia summary, bez wstepu i bez opisu przed werdyktem. "
                "W przypadku ZATWIERDZAM Z POPRAWKAMI podaj tabele Markdown z poprawkami:\n"
                "| id | kategoria | canonical albo priorytet | powod |\n"
                "Kategorie: DUPLIKAT, NIEAKTUALNE, ZOSTAW (priorytety P1, P2, P3).\n"
                "Pamietaj o zasadach: nie zamykamy jako NIEAKTUALNE zgloszen dotyczacych sekretow/kasowania."
            ),
            "mode": "advisory",
            "kind": "architecture",
            "scope": ["triage-view.tsv"],
            "inputs": ["triage-view.tsv", "open-tickets.json", "BRIEF.md"],
            "done_when": [
                "Werdykt jako pierwsza linia summary: ZATWIERDZAM / ZATWIERDZAM Z POPRAWKAMI / ODRZUCAM",
            ],
            "constraints": [
                "Podaj jasny werdykt i ewentualna tabele poprawek",
            ],
            "maker": director_candidates,
            "reviewer": director_reviewer,
        },
        "constraints": ["No file deletion"],
    }


# ---------------------------------------------------------------- Validation

def validate_triage_plan(
    triage_data: Any,
    untriaged_tickets: list[dict[str, Any]],
    all_open_tickets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Strict deterministic validation of triage.json. Raises ValidationError on failure."""
    if not isinstance(triage_data, list):
        raise ValidationError("triage.json root must be a list")

    id_to_ticket = {t["id"]: t for t in all_open_tickets}
    sig_to_ticket = {t["signature"]: t for t in all_open_tickets}
    untriaged_canonical_ids = {t["id"] for t in untriaged_tickets}
    already_triaged_ids = {t["id"] for t in all_open_tickets if helpdesk.priority_of(t) is not None}

    validated_items: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for index, raw_item in enumerate(triage_data):
        if not isinstance(raw_item, dict):
            raise ValidationError(f"Item #{index} in triage.json is not an object")

        raw_id = str(raw_item.get("id") or "").strip()
        if not raw_id:
            raise ValidationError(f"Item #{index} missing id")

        # Resolve to canonical HD- ticket id
        if raw_id in id_to_ticket:
            ticket_id = raw_id
        elif raw_id in sig_to_ticket:
            ticket_id = sig_to_ticket[raw_id]["id"]
        else:
            raise ValidationError(f"Unknown ticket id in triage: {raw_id}")

        if ticket_id not in untriaged_canonical_ids:
            raise ValidationError(f"Ticket {ticket_id} is not an untriaged open ticket")

        if ticket_id in seen_ids:
            raise ValidationError(f"Duplicate entry for ticket {ticket_id} in triage.json")
        seen_ids.add(ticket_id)

        kat = str(raw_item.get("kategoria") or "").strip().upper()
        if kat not in {"DUPLIKAT", "NIEAKTUALNE", "ZOSTAW"}:
            raise ValidationError(f"Invalid category {kat!r} for ticket {ticket_id}")

        powod = str(raw_item.get("powod") or "").strip()
        if not powod:
            raise ValidationError(f"Missing reason (powod) for ticket {ticket_id}")
        if len(powod) > 160:
            raise ValidationError(f"Reason exceeds 160 chars in ticket {ticket_id}")

        priorytet = raw_item.get("priorytet")
        canonical = raw_item.get("canonical")

        if kat == "ZOSTAW":
            if priorytet not in {"P1", "P2", "P3"}:
                raise ValidationError(f"ZOSTAW requires priority P1|P2|P3, got {priorytet!r} in {ticket_id}")
            canonical = None

        elif kat == "NIEAKTUALNE":
            if priorytet is not None:
                raise ValidationError(f"NIEAKTUALNE must have priorytet=null, got {priorytet!r} in {ticket_id}")
            if canonical is not None and canonical != "":
                raise ValidationError(f"NIEAKTUALNE must have canonical=null, got {canonical!r} in {ticket_id}")
            canonical = None

        elif kat == "DUPLIKAT":
            if priorytet is not None:
                raise ValidationError(f"DUPLIKAT must have priorytet=null, got {priorytet!r} in {ticket_id}")
            canon_str = str(canonical or "").strip()
            if not canon_str:
                raise ValidationError(f"DUPLIKAT missing canonical ticket in {ticket_id}")

            # Resolve canonical
            if canon_str in id_to_ticket:
                canon_id = canon_str
            elif canon_str in sig_to_ticket:
                canon_id = sig_to_ticket[canon_str]["id"]
            else:
                raise ValidationError(f"Canonical {canon_str!r} for {ticket_id} does not exist among open tickets")

            if canon_id == ticket_id:
                raise ValidationError(f"Ticket {ticket_id} points to itself as canonical duplicate")
            canonical = canon_id

        validated_items.append({
            "id": ticket_id,
            "kategoria": kat,
            "canonical": canonical,
            "priorytet": priorytet,
            "powod": powod,
        })

    # Every untriaged ticket must appear exactly once
    if seen_ids != untriaged_canonical_ids:
        missing = sorted(untriaged_canonical_ids - seen_ids)
        extra = sorted(seen_ids - untriaged_canonical_ids)
        raise ValidationError(f"Untriaged tickets mismatch: missing={missing}, extra={extra}")

    # Validate canonical chains
    plan_map = {item["id"]: item for item in validated_items}
    for item in validated_items:
        if item["kategoria"] != "DUPLIKAT":
            continue
        curr = item["canonical"]
        chain = [item["id"]]
        visited = {item["id"]}

        while True:
            if curr in visited:
                raise ValidationError(f"Cycle in duplicate chain: {' -> '.join(chain + [curr])}")
            visited.add(curr)
            chain.append(curr)

            if curr in already_triaged_ids:
                # Resolved to an already triaged open ticket with priority
                break

            if curr in plan_map:
                target_item = plan_map[curr]
                if target_item["kategoria"] == "ZOSTAW":
                    # Resolved to ZOSTAW (which has priority)
                    break
                elif target_item["kategoria"] == "DUPLIKAT":
                    curr = target_item["canonical"]
                    continue
                else:
                    raise ValidationError(
                        f"Canonical chain for {item['id']} ends at {curr} with category {target_item['kategoria']} "
                        "(must end at ZOSTAW or an open ticket with priority)"
                    )
            else:
                raise ValidationError(f"Canonical ticket {curr} is not open")

    return validated_items


def build_triage_view_tsv(validated_items: list[dict[str, Any]]) -> str:
    """Generate TSV for Director review."""
    lines = ["id\tkategoria\tcanonical_albo_priorytet\tpowod"]
    for item in validated_items:
        kat = item["kategoria"]
        target = item["canonical"] if kat == "DUPLIKAT" else (item.get("priorytet") or "-")
        lines.append(f"{item['id']}\t{kat}\t{target}\t{item['powod']}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- Director Parsing

DIRECTOR_ROW_RE = re.compile(
    r"^\|\s*([A-Za-z0-9_-]+)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|",
    re.MULTILINE | re.IGNORECASE,
)

DIRECTOR_VERDICT_RE = re.compile(
    r"(ZATWIERDZAM\s+Z\s+POPRAWKAMI|ZATWIERDZAM|ODRZUCAM)(?=$|[\s:.,!?;*—-])",
    re.IGNORECASE,
)

DIRECTOR_VERDICT_NEGATION_RE = re.compile(
    r"^NIE\s+(?:ZATWIERDZAM|ODRZUCAM)\b",
    re.IGNORECASE,
)


def _first_response_line(text: str) -> str:
    """First non-empty line of a response field, without markdown decoration."""
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        return re.sub(r"^(?:(?:\*\*|#+|>)+\s*)+", "", stripped).lstrip()
    return ""


def _verdict_from_line(line: str) -> str | None:
    """The verdict a single response line starts with, if any."""
    match = DIRECTOR_VERDICT_RE.match(line)
    if not match:
        return None
    raw = match.group(1).upper()
    if "POPRAWKAMI" in raw:
        return "ZATWIERDZAM Z POPRAWKAMI"
    if "ODRZUCAM" in raw:
        return "ODRZUCAM"
    return "ZATWIERDZAM"


def parse_director_verdict(text: str | Iterable[str]) -> tuple[str | None, list[dict[str, str]]]:
    """Read the Director verdict from the first response field that carries one.

    ``text`` is either one response field or the ordered list of fields
    (handoff.summary, handoff.text, public-handoff.json). A field whose first
    non-empty line is only a preamble is skipped, not fatal: the next field is
    checked. A negation in the first line of any field ("NIE ZATWIERDZAM")
    rejects the whole response.
    """
    if isinstance(text, str):
        fields = [text]
    else:
        fields = [str(item) for item in text]
    fields = [item for item in fields if item.strip()]
    if not fields:
        return None, []

    for field in fields:
        if DIRECTOR_VERDICT_NEGATION_RE.match(_first_response_line(field)):
            return "ODRZUCAM", []

    verdict: str | None = None
    for field in fields:
        verdict = _verdict_from_line(_first_response_line(field))
        if verdict:
            break
    if not verdict:
        return None, []

    joined = "\n\n".join(fields)
    amendments: list[dict[str, str]] = []
    for row_match in DIRECTOR_ROW_RE.finditer(joined):
        t_id = row_match.group(1).strip()
        kat = row_match.group(2).strip().upper()
        target = row_match.group(3).strip()
        powod = row_match.group(4).strip()[:160]
        if all(re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in (t_id, kat, target, powod)):
            continue
        if t_id.lower() == "id":
            continue
        amendments.append({
            "id": t_id,
            "kategoria": kat,
            "target": target,
            "powod": powod,
        })

    return verdict, amendments


def apply_director_amendments(
    items: list[dict[str, Any]],
    amendments: list[dict[str, str]],
    all_open_tickets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Apply Director overrides and re-validate."""
    id_to_t = {t["id"]: t for t in all_open_tickets}
    sig_to_t = {t["signature"]: t for t in all_open_tickets}
    items_map = {it["id"]: it for it in items}
    seen_ids: set[str] = set()

    for am in amendments:
        raw_id = am["id"]
        if raw_id in seen_ids:
            raise ValidationError(f"Duplicate Director amendment for {raw_id}")
        seen_ids.add(raw_id)
        if raw_id not in items_map:
            raise ValidationError(f"Director amendment id is outside triage plan: {raw_id}")
        t_id = raw_id
        if t_id not in items_map:
            raise ValidationError(f"Director amendment id is outside triage plan: {raw_id}")

        kat = am["kategoria"].strip().upper()
        if kat not in {"DUPLIKAT", "NIEAKTUALNE", "ZOSTAW"}:
            raise ValidationError(f"Invalid Director amendment category {kat!r} for {t_id}")
        target = am["target"]
        powod = am["powod"] or items_map[t_id]["powod"]

        updated = {"id": t_id, "kategoria": kat, "powod": powod}
        if kat == "ZOSTAW":
            prio = target.upper()
            if prio not in {"P1", "P2", "P3"}:
                raise ValidationError(f"Invalid Director priority {target!r} for {t_id}")
            updated["priorytet"] = prio
            updated["canonical"] = None
        elif kat == "NIEAKTUALNE":
            if target.strip().lower() not in {"", "-", "null", "none"}:
                raise ValidationError(f"NIEAKTUALNE amendment cannot have canonical/priority {target!r} for {t_id}")
            updated["priorytet"] = None
            updated["canonical"] = None
        elif kat == "DUPLIKAT":
            if target not in id_to_t and target not in sig_to_t:
                raise ValidationError(f"Invalid Director canonical {target!r} for {t_id}")
            updated["priorytet"] = None
            updated["canonical"] = target
        items_map[t_id] = updated

    return list(items_map.values())


# ---------------------------------------------------------------- Application

def apply_triage_decisions(
    decisions: list[dict[str, Any]],
    director_model: str,
    all_open_tickets: list[dict[str, Any]],
    dry_run: bool = False,
) -> dict[str, Any]:
    """Apply snapshot decisions atomically; stale or claimed tickets are skipped."""
    id_to_ticket = {t["id"]: t for t in all_open_tickets}
    by_agent = f"rsi-triage:{director_model}"

    stats = {
        "closed_duplicates": 0,
        "closed_obsolete": 0,
        "prioritized": 0,
        "skipped_not_open": 0,
        "skipped_invalid": 0,
        "skipped_t2": 0,
        "safety_overrides": 0,
        "t2_waiting": [],
    }

    for item in decisions:
        t_id = item["id"]
        initial_ticket = id_to_ticket.get(t_id)
        if not initial_ticket:
            continue
        sig = initial_ticket["signature"]

        live_ticket = helpdesk.load(sig)
        if not live_ticket:
            stats["skipped_not_open"] += 1
            continue

        hist = live_ticket.get("history") or []
        last_note = str(hist[-1].get("note") if hist and isinstance(hist[-1], dict) else "")
        kat = item["kategoria"]
        powod = item["powod"]

        # T2 gate: a sensitive ticket is the owner's decision, never the
        # triage's. The triage changes neither its status nor its priority and
        # adds no note; the id is reported in t2_waiting so the ticket reaches
        # the owner's report instead of being silently closed or prioritized.
        if is_t2_ticket(live_ticket):
            stats["skipped_t2"] += 1
            stats["safety_overrides"] += 1
            stats["t2_waiting"].append(t_id)
            continue

        prio = item.get("priorytet")
        if kat == "ZOSTAW" and prio not in {"P1", "P2", "P3"}:
            # No invented default priority: a ZOSTAW item without a valid priority
            # is left untouched instead of silently becoming P2.
            stats["skipped_invalid"] += 1
            continue

        if dry_run:
            if kat == "DUPLIKAT":
                stats["closed_duplicates"] += 1
            elif kat == "NIEAKTUALNE":
                stats["closed_obsolete"] += 1
            elif kat == "ZOSTAW":
                stats["prioritized"] += 1
            continue

        if kat == "DUPLIKAT":
            canonical = item.get("canonical")
            note = f"triaz: DUPLIKAT {canonical}. {powod}. Warunek ponownego otwarcia: usterka wystapi niezaleznie od {canonical}."
            updated = helpdesk.update_if_unchanged(
                sig, initial_ticket.get("updated_at"), expected_status="open", by=by_agent,
                status="closed", note=note,
            )
            if updated is None:
                stats["skipped_not_open"] += 1
                continue
            stats["closed_duplicates"] += 1

        elif kat == "NIEAKTUALNE":
            note = f"triaz: NIEAKTUALNE. {powod}. Warunek ponownego otwarcia: ponowne zaobserwowanie objawu w aktualnej wersji."
            updated = helpdesk.update_if_unchanged(
                sig, initial_ticket.get("updated_at"), expected_status="open", by=by_agent,
                status="closed", note=note,
            )
            if updated is None:
                stats["skipped_not_open"] += 1
                continue
            stats["closed_obsolete"] += 1

        elif kat == "ZOSTAW":
            note = f"triaz: {powod}"
            updated = helpdesk.update_if_unchanged(
                sig, initial_ticket.get("updated_at"), expected_status="open", by=by_agent,
                priority=prio, note=note,
            )
            if updated is None:
                stats["skipped_not_open"] += 1
                continue
            stats["prioritized"] += 1

    return stats


# ---------------------------------------------------------------- Runner

RUNNER_SUCCESS_STATUSES = {"completed", "candidate"}


def runner_output_status(out: Any) -> str:
    """The status a runner reported in its out.json (empty when it reported none)."""
    if isinstance(out, dict):
        return str(out.get("status") or "").strip().lower()
    return ""


def runner_output_is_successful(out: Any) -> bool:
    """Fail closed: only an explicitly successful status counts as success.

    A runner that reported no status at all (missing key, empty string, or an
    out.json that is not an object) is a runner error, not a silent success:
    the cycle changes no ticket. The same rule applies to the worker, to the
    Director of the triage cycle and to the Director of the owner queue.
    """
    return runner_output_status(out) in RUNNER_SUCCESS_STATUSES


def default_fleet_runner(req_path: Path, out_path: Path) -> dict[str, Any]:
    """Default model runner invoking todo_fleet.py with timeout and CREATE_NO_WINDOW."""
    env_runner = os.environ.get("RSI_TRIAGE_FLEET_RUNNER")
    if env_runner:
        cmd = [sys.executable, env_runner, "--request", str(req_path), "--output", str(out_path)]
    else:
        # Locate todo_fleet.py
        candidate = Path(__file__).resolve().parents[2] / "agent-graph" / "scripts" / "todo_fleet.py"
        if not candidate.is_file():
            raise RuntimeError(f"todo_fleet.py not found at {candidate}")
        cmd = [sys.executable, str(candidate), "--request", str(req_path), "--output", str(out_path)]

    cflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    proc = subprocess.run(cmd, capture_output=True, timeout=3600, creationflags=cflags)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"Runner failed with exit code {proc.returncode}: {err}")
    if not out_path.is_file():
        raise RuntimeError("Runner did not produce out.json")
    try:
        result = json.loads(out_path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise RuntimeError(f"Runner produced invalid out.json: {exc}") from exc
    if not isinstance(result, dict):
        raise RuntimeError("Runner out.json must be an object")
    if runner_output_status(result) not in RUNNER_SUCCESS_STATUSES:
        raise RuntimeError(f"Runner out.json status is not successful: {result.get('status')!r}")
    return result


def director_response_fields(director_out: Any, workspace: Path | Iterable[Path]) -> list[str]:
    """Ordered Director response fields: handoff.summary, handoff.text, public-handoff.json.

    ``workspace`` is one directory or several: the Director publishes its answer in
    the workspace it was given (the maker worktree for triage, the owner-queue
    directory for the owner queue), so both are accepted here. One function, one
    set of fields: handoff.summary, handoff.text and public-handoff.json.
    """
    fields: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str) and value.strip() and value not in fields:
            fields.append(value.strip())

    if isinstance(director_out, dict):
        handoff = director_out.get("handoff")
        if isinstance(handoff, dict):
            add(handoff.get("summary"))
            add(handoff.get("text"))

    if isinstance(workspace, (str, Path)):
        workspaces = [Path(workspace)]
    else:
        workspaces = [Path(item) for item in workspace]
    for ws in workspaces:
        public_path = ws / "public-handoff.json"
        if not public_path.is_file():
            continue
        try:
            public = json.loads(public_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            public = None
        if isinstance(public, dict):
            add(public.get("summary"))
            add(public.get("text"))
            public_handoff = public.get("handoff")
            if isinstance(public_handoff, dict):
                add(public_handoff.get("summary"))
                add(public_handoff.get("text"))
    return fields


def director_response_text(director_out: Any, workspace: Path | Iterable[Path]) -> str:
    """Read the same public Director response fields for triage and owner-queue.

    The fields are joined in the fixed order (handoff.summary, handoff.text,
    public-handoff.json); the verdict is read from the fields one by one, so
    use ``director_response_fields`` when the order matters.
    """
    return "\n\n".join(director_response_fields(director_out, workspace))


# ---------------------------------------------------------------- Owner Queue

def build_owner_queue_director_request(
    workspace: Path,
    cfg: dict[str, Any],
) -> dict[str, Any]:
    t_cfg = triage_config(cfg)
    director_candidates = normalize_candidates(t_cfg.get("director"), DEFAULT_DIRECTOR_CANDIDATES)
    director_reviewer = normalize_candidates(t_cfg.get("director_reviewer"), DEFAULT_DIRECTOR_REVIEWER)
    return {
        "goal": "RSI Helpdesk decyzja Dyrektora dla kolejki candidate / needs-owner",
        "role": "director",
        "workspace": str(workspace),
        "task": {
            "text": (
                "Przejrzyj zgloszenia RSI Helpdesk w statusie candidate oraz needs-owner starsze niz 24h z pliku owner-queue-tickets.json. "
                "Dla kazdego id wydaj jedna z decyzji:\n"
                "A ZAMKNIJ (z warunkiem ponownego otwarcia)\n"
                "B WDROZ (wraca do kolejki resolvera z instrukcja wdrozenia: testy, ACCEPT innego rodowodu, kanarek, rollback)\n"
                "C TECHNICZNA (rozstrzygniecie dylematu, wraca do kolejki z decyzja)\n"
                "D MATEUSZ (tylko kategoria T2, z jednym prostym pytaniem)\n\n"
                "Odpowiedz musi zawierac dokladnie jedna tabele w formacie Markdown:\n"
                "| id | A/B/C/D | tresc decyzji | warunki |\n"
            ),
            "mode": "advisory",
            "kind": "architecture",
            "scope": ["owner-queue-tickets.json"],
            "inputs": ["owner-queue-tickets.json"],
            "done_when": [
                "Tabela Markdown z decyzjami dla kazdego zgloszenia: | id | A/B/C/D | tresc decyzji | warunki |",
            ],
            "constraints": [
                "Format tabeli: | id | A/B/C/D | tresc decyzji | warunki |",
                "Kategorie A, B, C lub D",
            ],
            "maker": director_candidates,
            "reviewer": director_reviewer,
        },
        "constraints": ["No file deletion"],
    }


DIRECTOR_OQ_ROW_RE = re.compile(
    r"^\|\s*([A-Za-z0-9_-]+)\s*\|\s*\*?([ABCD])\*?(?:\s*[:\-\s]\s*[A-Za-z]+)?\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|",
    re.MULTILINE | re.IGNORECASE,
)


def parse_owner_queue_director_table(text: str) -> list[dict[str, str]]:
    """Parse Markdown table rows for owner-queue decisions."""
    decisions: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for match in DIRECTOR_OQ_ROW_RE.finditer(text):
        raw_id = match.group(1).strip()
        code = match.group(2).strip().upper()
        tresc = match.group(3).strip()
        warunki = match.group(4).strip()
        if raw_id.lower() == "id":
            continue
        if raw_id in seen_ids:
            continue
        seen_ids.add(raw_id)
        decisions.append({
            "id": raw_id,
            "code": code,
            "tresc": tresc,
            "warunki": warunki,
        })
    return decisions


def default_decisions_recorder(
    cli_path: str | Path,
    *,
    project: str,
    category: str,
    decided_by: str,
    question: str,
    choice: str,
    reason: str | None = None,
) -> dict[str, Any]:
    cmd = [
        sys.executable,
        str(cli_path),
        "record",
        "--project", project,
        "--category", category,
        "--decided-by", decided_by,
        "--question", question,
        "--choice", choice,
    ]
    if reason:
        cmd.extend(["--reason", reason])
    cflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    proc = subprocess.run(cmd, capture_output=True, text=True, creationflags=cflags, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(f"decisions_cli failed with exit code {proc.returncode}: {proc.stderr[:200]}")
    try:
        return json.loads(proc.stdout)
    except Exception:
        return {"output": proc.stdout}


def invoke_decisions_recorder(
    recorder: Callable[..., Any],
    cli_path: str | Path,
    *,
    project: str,
    category: str,
    decided_by: str,
    question: str,
    choice: str,
    reason: str | None = None,
    ticket_id: str = "",
    code: str = "",
) -> Any:
    kwargs = {
        "cli_path": str(cli_path) if cli_path else "",
        "project": project,
        "category": category,
        "decided_by": decided_by,
        "question": question,
        "choice": choice,
        "reason": reason,
        "ticket_id": ticket_id,
        "code": code,
    }
    try:
        return recorder(**kwargs)
    except TypeError:
        try:
            return recorder(kwargs)
        except TypeError:
            return recorder(cli_path, project, category, decided_by, question, choice, reason)


def apply_owner_queue_decisions(
    decisions: list[dict[str, str]],
    director_model: str,
    eligible_tickets: list[dict[str, Any]],
    cfg: dict[str, Any],
    dry_run: bool = False,
    decisions_recorder: Callable[..., Any] | None = None,
) -> tuple[dict[str, Any], str]:
    """Applies Director decisions A, B, C, D to eligible tickets.
    Records applied decisions in decision journal if configured.
    Returns (stats, journal_note).
    """
    id_to_t = {t["id"]: t for t in eligible_tickets}
    sig_to_t = {t["signature"]: t for t in eligible_tickets}
    by_agent = f"rsi-director:{director_model}"

    stats = {
        "closed": 0,
        "reopened_wdroz": 0,
        "reopened_techniczna": 0,
        "needs_owner": 0,
        "skipped_status_changed": 0,
        "skipped_unknown": 0,
        "decisions_recorded": 0,
        "recorded_without_change": 0,
        "t2_waiting": [],
        "recorder_errors": [],
        "ignored_non_t2_mateusz": 0,
    }

    t_cfg = triage_config(cfg)
    decisions_cli = t_cfg.get("decisions_cli")
    journal_note = ""
    seen_sigs: set[str] = set()

    for item in decisions:
        raw_id = item["id"]
        init_ticket = id_to_t.get(raw_id) or sig_to_t.get(raw_id)
        if not init_ticket:
            stats["skipped_unknown"] += 1
            continue

        sig = init_ticket["signature"]
        if sig in seen_sigs:
            continue
        seen_sigs.add(sig)

        live_ticket = helpdesk.load(sig)
        if (not live_ticket or live_ticket.get("status") != init_ticket.get("status")
                or live_ticket.get("updated_at") != init_ticket.get("updated_at")
                or live_ticket.get("claim")):
            stats["skipped_status_changed"] += 1
            continue

        code = item["code"]
        tresc = item["tresc"]
        warunki = item["warunki"]

        if dry_run:
            if code == "A":
                stats["closed"] += 1
            elif code == "B":
                stats["reopened_wdroz"] += 1
            elif code == "C":
                stats["reopened_techniczna"] += 1
            elif code == "D":
                # Eligible owner-queue snapshots exclude T2: D for a normal
                # technical ticket is treated as no decision.
                stats["ignored_non_t2_mateusz"] += 1
            continue

        applied = True
        if code == "A":
            note = f"decyzja A (ZAMKNIJ): {tresc}. Warunek ponownego otwarcia: {warunki}".strip()
            updated = helpdesk.update_if_unchanged(
                sig, init_ticket.get("updated_at"), expected_status=str(init_ticket.get("status")),
                by=by_agent, status="closed", note=note,
            )
            if updated is None:
                stats["skipped_status_changed"] += 1
                continue
            stats["closed"] += 1
        elif code == "B":
            note = f"decyzja B (WDROZ): {tresc}. Instrukcja wdrozenia: {warunki}".strip()
            updated = helpdesk.update_if_unchanged(
                sig, init_ticket.get("updated_at"), expected_status=str(init_ticket.get("status")),
                by=by_agent, status="open", note=note,
            )
            if updated is None:
                stats["skipped_status_changed"] += 1
                continue
            stats["reopened_wdroz"] += 1
        elif code == "C":
            note = f"decyzja C (TECHNICZNA): {tresc}. Warunki: {warunki}".strip()
            updated = helpdesk.update_if_unchanged(
                sig, init_ticket.get("updated_at"), expected_status=str(init_ticket.get("status")),
                by=by_agent, status="open", note=note,
            )
            if updated is None:
                stats["skipped_status_changed"] += 1
                continue
            stats["reopened_techniczna"] += 1
        elif code == "D":
            # Eligible owner-queue snapshots exclude T2, so D here means the owner asked
            # a Mateusz-reserved question about a normal technical ticket. It is recorded
            # in the journal (the owner sees it in the daily report) but changes nothing.
            stats["ignored_non_t2_mateusz"] += 1
            applied = False

        question_text = f"Zgloszenie {init_ticket['id']}: {init_ticket.get('symptom', '')[:200]}"
        choice_text = f"{code}: {tresc[:200]}"
        reason_text = warunki[:200] if warunki else None
        if not applied:
            choice_text = f"{code} (bez zmian w zgloszeniu): {tresc[:200]}"

        if decisions_recorder is not None:
            try:
                invoke_decisions_recorder(
                    decisions_recorder,
                    decisions_cli or "",
                    project="agent-stack",
                    category="technical",
                    decided_by=f"director:{director_model}",
                    question=question_text,
                    choice=choice_text,
                    reason=reason_text,
                    ticket_id=init_ticket["id"],
                    code=code,
                )
                stats["recorded_without_change" if not applied else "decisions_recorded"] += 1
            except Exception as exc:
                stats["recorder_errors"].append(f"{init_ticket['id']}: {type(exc).__name__}: {str(exc)[:200]}")
        elif decisions_cli:
            try:
                default_decisions_recorder(
                    decisions_cli,
                    project="agent-stack",
                    category="technical",
                    decided_by=f"director:{director_model}",
                    question=question_text,
                    choice=choice_text,
                    reason=reason_text,
                )
                stats["recorded_without_change" if not applied else "decisions_recorded"] += 1
            except Exception as exc:
                stats["recorder_errors"].append(f"{init_ticket['id']}: {type(exc).__name__}: {str(exc)[:200]}")
        else:
            journal_note = "pominiecie rejestru decyzji (brak triage.decisions_cli)"

    return stats, journal_note


def run_owner_queue(
    dry_run: bool = False,
    fleet_runner: Callable[[Path, Path], dict[str, Any]] | None = None,
    decisions_recorder: Callable[..., Any] | None = None,
    lock_held: bool = False,
) -> dict[str, Any]:
    """Execute owner-queue cycle for candidate / needs-owner tickets older than 24h."""
    runner = fleet_runner or default_fleet_runner
    cfg = resolve_cycle.config()
    report: dict[str, Any] = {
        "at": helpdesk.iso(helpdesk.now()),
        "dry_run": dry_run,
    }

    if (helpdesk.home() / "STOP").exists():
        return report | {"result": "stopped", "status": "stopped"}
    if cfg.get("config_error"):
        return report | {"result": "config-error", "status": "config-error"}
    if not cfg.get("enabled", True):
        return report | {"result": "stopped", "status": "stopped"}

    eligible_tickets, t2_skipped = get_owner_queue_candidates()
    t2_waiting = t2_waiting_ticket_ids()
    report["eligible_count"] = len(eligible_tickets)
    report["t2_skipped_count"] = len(t2_skipped)
    report["t2_waiting"] = t2_waiting

    if not eligible_tickets:
        res_summary = "completed: 0 tickets for director"
        if t2_skipped:
            res_summary += f" ({len(t2_skipped)} T2 tickets skipped)"
        if dry_run:
            res_summary = "[dry-run] " + res_summary
        elif not lock_held:
            save_result(res_summary, "completed", successful_owner_queue=True,
                        owner_queue_attempt=True, t2_waiting=t2_waiting)
        return report | {"result": res_summary, "status": "completed"}

    lock_ctx = contextlib.nullcontext({"status": "held"}) if (dry_run or lock_held) else triage_lock()

    try:
        with lock_ctx:
            if (helpdesk.home() / "STOP").exists():
                return report | {"result": "stopped", "status": "stopped"}

            now_ts = helpdesk.now().strftime("%Y%m%dT%H%M%SZ")
            oq_dir = helpdesk.home() / "owner-queue" / now_ts
            if oq_dir.exists():
                now_ts = f"{now_ts}-{os.urandom(3).hex()}"
                oq_dir = helpdesk.home() / "owner-queue" / now_ts
            oq_dir.mkdir(parents=True, exist_ok=True)
            report["workspace"] = str(oq_dir)

            tickets_payload = []
            for t in eligible_tickets:
                hist = t.get("history") or []
                last_note = str(hist[-1].get("note") if hist and isinstance(hist[-1], dict) else "")[:200]
                tickets_payload.append({
                    "id": str(t.get("id") or ""),
                    "signature": str(t.get("signature") or ""),
                    "status": str(t.get("status") or ""),
                    "component": str(t.get("component") or ""),
                    "symptom": str(t.get("symptom") or "")[:300],
                    "impact": str(t.get("impact") or "")[:200],
                    "last_note": last_note,
                })
            tickets_file = oq_dir / "owner-queue-tickets.json"
            tickets_file.write_text(json.dumps(tickets_payload, ensure_ascii=False, indent=1), encoding="utf-8")

            director_req = build_owner_queue_director_request(oq_dir, cfg)
            director_req_path = oq_dir / "director-req.json"
            director_out_path = oq_dir / "director-out.json"
            director_req_path.write_text(json.dumps(director_req, ensure_ascii=False, indent=1), encoding="utf-8")

            try:
                director_out = runner(director_req_path, director_out_path)
            except Exception as exc:
                err_msg = f"director-runner-failed: {exc}"
                if not dry_run:
                    save_result(err_msg, "failed", owner_queue_attempt=True, t2_waiting=t2_waiting)
                return report | {"result": err_msg, "status": "failed"}

            if not runner_output_is_successful(director_out):
                err_msg = f"director-runner-failed: runner status is not successful: {runner_output_status(director_out)!r}"
                if not dry_run:
                    save_result(err_msg, "failed", owner_queue_attempt=True, t2_waiting=t2_waiting)
                return report | {"result": err_msg, "status": "failed"}

            text_to_search = director_response_text(director_out, oq_dir)

            decisions = parse_owner_queue_director_table(text_to_search)
            if not decisions:
                err_msg = "director-failed: brak tabeli decyzji A/B/C/D"
                if not dry_run:
                    save_result(err_msg, "failed", owner_queue_attempt=True, t2_waiting=t2_waiting)
                return report | {"result": err_msg, "status": "failed"}

            director_model = (
                str(director_out.get("model") or "")
                or str((director_out.get("candidate") or {}).get("model") or "")
                or "director"
            )
            apply_stats, journal_note = apply_owner_queue_decisions(
                decisions,
                director_model,
                eligible_tickets,
                cfg,
                dry_run=dry_run,
                decisions_recorder=decisions_recorder,
            )
            report["applied"] = apply_stats
            report["t2_waiting"] = t2_waiting

            result_summary = (
                f"completed: {apply_stats['closed']} closed, "
                f"{apply_stats['reopened_wdroz']} reopened (wdroz), "
                f"{apply_stats['reopened_techniczna']} reopened (techniczna), "
                f"{apply_stats['needs_owner']} kept needs-owner"
            )
            if apply_stats["skipped_status_changed"]:
                result_summary += f", {apply_stats['skipped_status_changed']} status changed skipped"
            if apply_stats["decisions_recorded"]:
                result_summary += f", {apply_stats['decisions_recorded']} decisions recorded"
            if apply_stats["ignored_non_t2_mateusz"]:
                result_summary += f", {apply_stats['ignored_non_t2_mateusz']} non-T2 D decisions ignored"
            if journal_note:
                result_summary += f" [{journal_note}]"
            if dry_run:
                result_summary = "[dry-run] " + result_summary

            if not dry_run and not lock_held:
                save_result(result_summary, "completed", successful_owner_queue=True,
                            owner_queue_attempt=True, t2_waiting=t2_waiting,
                            recorder_errors=apply_stats["recorder_errors"])

            report["result"] = result_summary
            report["recorder_errors"] = apply_stats["recorder_errors"]
            report["status"] = "completed"
            return report

    except LockBusyError as exc:
        return report | {"result": f"busy: {exc}", "status": "busy"}


# ---------------------------------------------------------------- Main Cycle

def run_cycle(
    dry_run: bool = False,
    force_due: bool = False,
    fleet_runner: Callable[[Path, Path], dict[str, Any]] | None = None,
    decisions_recorder: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Run one full triage cycle: Worker -> deterministic validation -> Director -> Apply."""
    runner = fleet_runner or default_fleet_runner
    cfg = resolve_cycle.config()
    report: dict[str, Any] = {
        "at": helpdesk.iso(helpdesk.now()),
        "dry_run": dry_run,
        "force_due": force_due,
    }

    # 1. Due check
    due, due_reason = is_due(cfg, force_due=force_due)
    owner_due, owner_due_reason = is_owner_queue_due(cfg)
    report["due"] = due
    report["due_reason"] = due_reason
    report["owner_queue_due"] = owner_due
    report["owner_queue_due_reason"] = owner_due_reason
    if not due:
        if owner_due:
            oq_res = run_owner_queue(dry_run=dry_run, fleet_runner=runner,
                                     decisions_recorder=decisions_recorder)
            report["owner_queue"] = oq_res
            report["t2_waiting"] = oq_res.get("t2_waiting", [])
            return report | {"result": oq_res.get("result", "owner-queue"),
                             "status": oq_res.get("status", "failed")}
        return report | {"result": due_reason}

    all_open, untriaged = untriaged_open_tickets()
    report["untriaged_count"] = len(untriaged)
    report["all_open_count"] = len(all_open)
    if not untriaged:
        if owner_due:
            oq_res = run_owner_queue(dry_run=dry_run, fleet_runner=runner,
                                     decisions_recorder=decisions_recorder)
            report["owner_queue"] = oq_res
            report["t2_waiting"] = oq_res.get("t2_waiting", [])
            return report | {"result": oq_res.get("result", "owner-queue"),
                             "status": oq_res.get("status", "failed")}
        return report | {"result": "no-untriaged"}

    t2_waiting = t2_waiting_ticket_ids()

    # 2. Acquire lock (in dry-run, use dummy context)
    lock_ctx = contextlib.nullcontext({"status": "dry-run"}) if dry_run else triage_lock()

    try:
        with lock_ctx:
            oq_res: dict[str, Any] | None = None
            oq_record: dict[str, Any] | None = None
            if owner_due:
                oq_res = run_owner_queue(
                    dry_run=dry_run,
                    fleet_runner=runner,
                    decisions_recorder=decisions_recorder,
                    lock_held=True,
                )
                report["owner_queue"] = oq_res
                t2_waiting = oq_res.get("t2_waiting", t2_waiting)
                oq_status = str(oq_res.get("status") or "failed")
                oq_record = {
                    "status": oq_status,
                    "result": str(oq_res.get("result") or "owner-queue"),
                    "t2_waiting": sorted(set(t2_waiting)),
                }
                oq_errors = (oq_res.get("applied") or {}).get("recorder_errors") or []
                if oq_errors:
                    oq_record["recorder_errors"] = list(oq_errors)
                report["owner_queue_record"] = oq_record
                if not dry_run:
                    save_result(
                        oq_record["result"], oq_status,
                        successful_owner_queue=(oq_status == "completed"),
                        owner_queue_attempt=True,
                        owner_queue_record=oq_record,
                        t2_waiting=t2_waiting,
                        recorder_errors=oq_errors,
                    )

            # 3. Create workspace
            now_ts = helpdesk.now().strftime("%Y%m%dT%H%M%SZ")
            triage_dir = helpdesk.home() / "triage" / now_ts
            if triage_dir.exists():
                now_ts = f"{now_ts}-{os.urandom(3).hex()}"
                triage_dir = helpdesk.home() / "triage" / now_ts
            repo_dir, maker_worktree = prepare_workspace(triage_dir, all_open)
            report["workspace"] = str(maker_worktree)

            # 4. Worker execution
            worker_req = build_worker_request(maker_worktree, cfg)
            worker_req_path = triage_dir / "worker-req.json"
            worker_out_path = triage_dir / "worker-out.json"
            worker_req_path.write_text(json.dumps(worker_req, ensure_ascii=False, indent=1), encoding="utf-8")

            try:
                worker_out = runner(worker_req_path, worker_out_path)
            except Exception as exc:
                err_msg = f"worker-runner-failed: {exc}"
                if not dry_run:
                    save_result(err_msg, "failed", t2_waiting=t2_waiting)
                return report | {"result": err_msg, "status": "failed"}

            if not runner_output_is_successful(worker_out):
                err_msg = f"worker-runner-failed: runner status is not successful: {runner_output_status(worker_out)!r}"
                if not dry_run:
                    save_result(err_msg, "failed", t2_waiting=t2_waiting)
                return report | {"result": err_msg, "status": "failed"}

            # Load worker triage.json
            triage_json_path = maker_worktree / "triage.json"
            if not triage_json_path.is_file():
                triage_json_path = triage_dir / "triage.json"
            if not triage_json_path.is_file():
                err_msg = "worker-failed: triage.json not generated"
                if not dry_run:
                    save_result(err_msg, "failed", t2_waiting=t2_waiting)
                return report | {"result": err_msg, "status": "failed"}

            try:
                raw_triage = json.loads(triage_json_path.read_text(encoding="utf-8"))
            except (ValueError, OSError) as exc:
                err_msg = f"worker-failed: triage.json invalid: {exc}"
                if not dry_run:
                    save_result(err_msg, "failed", t2_waiting=t2_waiting)
                return report | {"result": err_msg, "status": "failed"}

            # 5. Deterministic validation
            try:
                validated_items = validate_triage_plan(raw_triage, untriaged, all_open)
            except ValidationError as exc:
                err_msg = f"validation-error: {exc}"
                if not dry_run:
                    save_result(err_msg, "failed", t2_waiting=t2_waiting)
                return report | {"result": err_msg, "status": "failed"}

            # 6. Prepare Director inputs
            tsv_content = build_triage_view_tsv(validated_items)
            tsv_path = maker_worktree / "triage-view.tsv"
            tsv_path.write_text(tsv_content, encoding="utf-8")
            (triage_dir / "triage-view.tsv").write_text(tsv_content, encoding="utf-8")

            director_req = build_director_request(maker_worktree, cfg)
            director_req_path = triage_dir / "director-req.json"
            director_out_path = triage_dir / "director-out.json"
            director_req_path.write_text(json.dumps(director_req, ensure_ascii=False, indent=1), encoding="utf-8")

            try:
                director_out = runner(director_req_path, director_out_path)
            except Exception as exc:
                err_msg = f"director-runner-failed: {exc}"
                if not dry_run:
                    save_result(err_msg, "failed", t2_waiting=t2_waiting)
                return report | {"result": err_msg, "status": "failed"}

            if not runner_output_is_successful(director_out):
                err_msg = f"director-runner-failed: runner status is not successful: {runner_output_status(director_out)!r}"
                if not dry_run:
                    save_result(err_msg, "failed", t2_waiting=t2_waiting)
                return report | {"result": err_msg, "status": "failed"}

            # 7. Parse Director response
            response_fields = director_response_fields(director_out, [maker_worktree, triage_dir])
            verdict, amendments = parse_director_verdict(response_fields)
            report["director_verdict"] = verdict
            if not verdict or verdict == "ODRZUCAM":
                err_msg = f"director-verdict: {verdict or 'brak-werdyktu'}"
                if not dry_run:
                    save_result(err_msg, "failed", t2_waiting=t2_waiting)
                return report | {"result": err_msg, "status": "failed"}

            # If amendments provided, apply and re-validate
            final_items = validated_items
            if verdict == "ZATWIERDZAM Z POPRAWKAMI":
                try:
                    if not amendments:
                        raise ValidationError("ZATWIERDZAM Z POPRAWKAMI requires at least one parsed amendment")
                    final_items = apply_director_amendments(validated_items, amendments, all_open)
                    final_items = validate_triage_plan(final_items, untriaged, all_open)
                except ValidationError as exc:
                    err_msg = f"director-amendments-validation-error: {exc}"
                    if not dry_run:
                        save_result(err_msg, "failed", t2_waiting=t2_waiting)
                    return report | {"result": err_msg, "status": "failed"}

            # 8. Apply decisions
            director_model = (
                str(director_out.get("model") or "")
                or str((director_out.get("candidate") or {}).get("model") or "")
                or "director"
            )
            apply_stats = apply_triage_decisions(final_items, director_model, all_open, dry_run=dry_run)
            report["applied"] = apply_stats
            # T2 tickets the triage left untouched wait for the owner and are
            # reported in last_result.t2_waiting.
            t2_waiting = sorted(set(t2_waiting) | set(apply_stats.get("t2_waiting") or []))

            # 9. Owner queue (after triage)
            result_summary = (
                f"completed: {apply_stats['closed_duplicates']} duplicates closed, "
                f"{apply_stats['closed_obsolete']} obsolete closed, "
                f"{apply_stats['prioritized']} prioritized"
            )
            if apply_stats["safety_overrides"]:
                result_summary += f", {apply_stats['safety_overrides']} safety overrides"
            if apply_stats.get("skipped_t2"):
                result_summary += f", {apply_stats['skipped_t2']} T2 czeka na wlasciciela"
            if oq_res is not None:
                result_summary += f"; owner-queue: {oq_res.get('result', '')}"
            if dry_run:
                result_summary = "[dry-run] " + result_summary

            if not dry_run:
                oq_succeeded = bool(oq_res and oq_res.get("status") == "completed")
                oq_errors = ((oq_res.get("applied") or {}).get("recorder_errors") or []) if oq_res else []
                save_result(result_summary, "completed" if oq_succeeded or oq_res is None else "partial",
                            successful_triage=True,
                            successful_owner_queue=oq_succeeded,
                            owner_queue_attempt=oq_res is not None,
                            owner_queue_record=oq_record,
                            t2_waiting=t2_waiting,
                            recorder_errors=oq_errors)

            report["result"] = result_summary
            report["t2_waiting"] = t2_waiting
            report["status"] = "completed" if oq_res is None or oq_res.get("status") == "completed" else "partial"
            return report

    except LockBusyError as exc:
        return report | {"result": f"busy: {exc}", "status": "busy"}


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(prog="triage_cycle", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    r_parser = sub.add_parser("run", help="Run triage cycle")
    r_parser.add_argument("--dry-run", action="store_true", help="Evaluate without changing tickets or state")
    r_parser.add_argument("--force-due", action="store_true", help="Force due check to pass")

    oq_parser = sub.add_parser("owner-queue", help="Run owner-queue cycle for candidate / needs-owner tickets")
    oq_parser.add_argument("--dry-run", action="store_true", help="Evaluate without changing tickets or state")

    sub.add_parser("status", help="Show triage queue status and state")

    args = parser.parse_args(argv)

    if args.command == "status":
        cfg = resolve_cycle.config()
        all_open, untriaged = untriaged_open_tickets()
        due, due_reason = is_due(cfg)
        status_info = {
            "at": helpdesk.iso(helpdesk.now()),
            "stop": resolve_cycle.stop_reason(),
            "due": due,
            "due_reason": due_reason,
            "open_tickets": len(all_open),
            "untriaged_tickets": len(untriaged),
            "state": load_state(),
        }
        print(json.dumps(status_info, ensure_ascii=False, indent=1))
        return 0

    if args.command == "run":
        res = run_cycle(dry_run=args.dry_run, force_due=args.force_due)
        print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
        if res.get("status") in {"failed", "busy", "partial"}:
            return 1
        return 0

    if args.command == "owner-queue":
        res = run_owner_queue(dry_run=args.dry_run)
        print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
        if res.get("status") in {"failed", "busy"}:
            return 1
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
