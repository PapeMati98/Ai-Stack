#!/usr/bin/env python3
"""
owner_digest.py - dzienny digest dla Mateusza (Fala 3, wezel O6).

Buduje jeden krotki raport Markdown (po polsku) o tym, czy stack pracuje:
- runy Fleet w oknie czasowym (agent | model | statusy | czas | udzial runow),
- zgloszenia Helpdesk RSI (otwarte, nowe w oknie, pytania do wlasciciela),
- zgloszenia RSI odlozone do decyzji wlasciciela (T2) i raporty hookow Claude,
- decyzje wlasciciela z dziennika,
- probke limitu Claude,
- zadania TODO wg acceptance_status.

Zasady (wg docs/org-fala3-20261021/SPEC-O6.md):
1. Wszystkie zrodla sa opcjonalne: brak pliku albo zly plik = sekcja
   "brak danych" z powodem. Nigdy wyjatek, nigdy crash.
2. Tresci z plikow to DANE: nie wykonujemy ich, usuwamy znaki sterujace,
   skracamy do 160 znakow i maskujemy ciagi podobne do sekretow jako <ukryte>.
3. Zapis atomowy: plik tymczasowy w tym samym katalogu + os.replace.
4. Raport zostaje lokalnie. Wysylka jest osobna decyzja - jedynymi procesami
   sa dwa lokalne, tylko do odczytu raporty hookow Claude, bez okna konsoli.
5. Tylko stdlib. Zakaz usuwania plikow (poza wlasnym plikiem tymczasowym
   przy bledzie zapisu).
6. Digest ma byc KROTSKI: kazda lista pokazuje maksymalnie 10 pozycji,
   reszta jako "... i N wiecej"; nieudane runy ida tabela zbiorcza.
7. Wypisywanie na konsole musi byc odporne na kodowanie (cp1250 i inne):
   bezpieczny zapis z `errors="replace"`, glownie UTF-8.
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
import tempfile
from pathlib import Path
from typing import Any

SCHEMA = "owner_digest_v1"

# Miejsce maskowania sekretow.
MASK = "<ukryte>"
# Limit dlugosci kazdej wartosci tekstowej ze zrodla.
MAX_LEN = 160
# Probka limitu Claude starsza niz tyle minut jest "nieaktualna".
STALE_MINUTES = 60
# Statusy zgloszenia helpdesk, ktore nie sa juz otwarte.
TERMINAL_TICKET_STATUS = frozenset({"closed", "wontfix"})
# Status zgloszenia = realne pytanie do Mateusza.
NEEDS_OWNER_STATUS = "needs-owner"
# Status zgloszenia = kandydat czekajacy na akceptacje Mateusza.
CANDIDATE_STATUS = "candidate"
# Etykieta dla kandydata w sekcji "Wymaga Twojej decyzji".
CANDIDATE_LABEL = "kandydat czeka na akceptacje"
# Etykieta dla zwyklego zgloszenia czekajacego na wlasciciela.
AWAITING_LABEL = "czeka na Ciebie"
# Ile pozycji pokazac w kazdej liscie (reszta: "... i N wiecej").
MAX_LIST_ITEMS = 10
# Ile grup nieudanych runow pokazac w tabeli zbiorczej.
MAX_FAILED_GROUPS = 10
# Limit czasu lokalnego raportu hooka Claude.
GUARD_TIMEOUT_SECONDS = 30

CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
WHITESPACE = re.compile(r"\s+")

# Wzorce podobne do sekretow. Celowo szerokie: lepszy falszywy alarm niz
# przeciek w raporcie wlasciciela.
SECRET_PATTERNS = (
    # op://vault/sekret/klucz
    re.compile(r"op://[^\s\"'<>]+", re.IGNORECASE),
    # -----BEGIN ... PRIVATE KEY-----
    re.compile(r"-----BEGIN[ A-Z]*PRIVATE KEY-----.*?(?:-----END[ A-Z]*PRIVATE KEY-----)?", re.DOTALL),
    # sk-..., sk_live_..., ghp_..., AKIA..., AIza..., xox..., JWT
    re.compile(r"\bsk[-_][A-Za-z0-9_\-]{8,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat|glpat)[-_][A-Za-z0-9_\-]{8,}"),
    re.compile(r"\bAKIA[0-9A-Z]{12,}"),
    re.compile(r"\bAIza[A-Za-z0-9_\-]{12,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{8,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}"),
    # "Bearer <token>"
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{8,}", re.IGNORECASE),
)

# "api_key=...", "password: ...", "token = ..." - etykieta zostaje, wartosc znika.
KEYED_SECRET = re.compile(
    r"(?i)\b(password|passwd|pwd|api[_\- ]?key|apikey|access[_\- ]?key|secret[_\- ]?key|"
    r"secret|token|auth[_\- ]?token|authorization|credential|klucz|haslo)\b"
    r"(\s*[=:]\s*)"
    r"(\"[^\"]*\"|'[^']*'|\S+)"
)

# Nazwa katalogu runu: "run-<base>" albo "run-<base><sufiks>" (run-v2 vs run-v2b).
RUN_NAME = re.compile(r"^run-(?P<base>.+?)(?P<suffix>[a-z]{1,2})?$")


# --------------------------------------------------------------------------
# Narzedzia
# --------------------------------------------------------------------------


def agents_home() -> Path:
    """Katalog zrodel: env AGENTS_HOME albo ~/.agents."""
    env = os.environ.get("AGENTS_HOME")
    if env and env.strip():
        return Path(env.strip())
    return Path.home() / ".agents"


def iso(value: dt.datetime) -> str:
    """Znacznik czasu w UTC, format `...Z`."""
    return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value: Any) -> dt.datetime | None:
    """ISO -> aware datetime w UTC. Zly format = None (zamiast wyjatku)."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def mask_text(value: Any, limit: int = MAX_LEN) -> str:
    """Dane z pliku -> bezpieczny tekst: bez znakow sterujacych, bez sekretow,
    skrocony do `limit` znakow."""
    text = "" if value is None else str(value)
    text = CONTROL_CHARS.sub(" ", text)
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(MASK, text)
    text = KEYED_SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", text)
    text = WHITESPACE.sub(" ", text).strip()
    if limit and len(text) > limit:
        masked = MASK in text
        head = text[: max(1, limit - 3)]
        if masked and MASK not in head:
            # Znacznik maskowania wypadlby za oknem skrocenia (sekret siedzi
            # daleko w tescie) - zostawiamy go widocznego, inaczej skrocenie
            # wygladaloby jak brak sekretu.
            keep = max(1, limit - 3 - len(MASK) - 4)
            text = text[:keep] + " " + MASK + " ..."
        else:
            text = head + "..."
    return text


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def number_or_q(value: Any) -> Any:
    """Liczba albo "?" (brak pola = "?", nic nie zgadujemy)."""
    return value if is_number(value) else "?"


def pct(part: int, total: int) -> float:
    if not total:
        return 0.0
    return round(part * 100.0 / total, 1)


def run_prefix(name: str) -> str:
    """Prefiks nazwy runu: run-v2 i run-v2b -> "v2" (ten sam prefix)."""
    match = RUN_NAME.match(name or "")
    if not match:
        return name or ""
    return match.group("base") or (name or "")


def read_json_file(path: Path) -> tuple[Any, str | None]:
    """(dane, blad). Blad jest opisem, nie wyjatkiem."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        return None, f"nie mozna odczytac pliku ({exc.strerror or exc})"
    try:
        return json.loads(text), None
    except ValueError as exc:
        return None, f"niepoprawny JSON ({exc})"


def empty_section(reason: str) -> dict[str, Any]:
    """Pusty stub sekcji (używany, gdy zrodla nie ma w oknie)."""
    return {"status": "no_data", "reason": reason}


# --------------------------------------------------------------------------
# Bezpieczny zapis na konsole (Windows cp1250 itp.)
# --------------------------------------------------------------------------


def _safe_stream(stream: Any) -> Any:
    """Przygotowuje strumien do bezpiecznego zapisu (UTF-8, 'replace')."""
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(encoding="utf-8", errors="replace")
            return stream
        except (AttributeError, OSError, ValueError, TypeError, LookupError):
            try:
                reconfigure(errors="replace")
            except (AttributeError, OSError, ValueError, TypeError, LookupError):
                pass
    return stream


def safe_write(stream: Any, text: str) -> None:
    """Zapis tekstu bez UnicodeEncodeError (nawet w konsoli cp1250)."""
    _safe_stream(stream)
    try:
        stream.write(text)
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "utf-8"
        try:
            stream.write(text.encode(encoding, "replace").decode(encoding, "replace"))
        except (LookupError, UnicodeEncodeError, AttributeError):
            stream.write(text.encode("utf-8", "replace").decode("utf-8", "replace"))
    try:
        stream.flush()
    except (OSError, ValueError, AttributeError):
        pass


def write_stdout(text: str) -> None:
    safe_write(sys.stdout, text)


def write_stderr(text: str) -> None:
    safe_write(sys.stderr, text)


# --------------------------------------------------------------------------
# Zrodlo 1: Fleet
# --------------------------------------------------------------------------


def read_fleet(home: Path, since: dt.datetime, until: dt.datetime) -> dict[str, Any]:
    """Runy Fleet z okna: agent | model | statusy | czas | udzial runow."""
    runs_dir = home / "agent-fleet" / "runs"
    section: dict[str, Any] = {
        "status": "no_data",
        "reason": None,
        "rows": [],
        "rows_total": 0,
        "other_total": 0,
        "total_runs": 0,
        "skipped_files": 0,
        "failed_groups": [],
        "failed_groups_total": 0,
        "failed_total": 0,
        "path": str(runs_dir),
    }
    if not runs_dir.is_dir():
        section["status"] = "missing"
        section["reason"] = f"brak katalogu {runs_dir}"
        return section

    groups: dict[tuple[str, str], dict[str, Any]] = {}
    total = 0
    skipped = 0
    failed_groups: dict[tuple[str, str, str, str], int] = {}

    for path in sorted(runs_dir.glob("run-*.json")):
        data, error = read_json_file(path)
        if error is not None or not isinstance(data, dict):
            skipped += 1
            continue
        started = parse_iso(data.get("started_at"))
        if started is None:
            skipped += 1  # brak poprawnego started_at: nie da sie umiescic w oknie
            continue
        if not (since <= started <= until):
            continue

        total += 1
        agent = mask_text(data.get("agent")) or "?"
        model = mask_text(data.get("model")) or "?"
        status = str(data.get("run_status") or "").strip().upper()
        key = (agent, model)
        group = groups.setdefault(
            key,
            {
                "agent": agent,
                "model": model,
                "runs": 0,
                "completed": 0,
                "partial": 0,
                "failed_blocked": 0,
                "other": 0,
                "duration_seconds": 0.0,
                "duration_known": False,
                "modes": [],
            },
        )
        group["runs"] += 1
        if status == "COMPLETED":
            group["completed"] += 1
        elif status == "PARTIAL":
            group["partial"] += 1
        elif status in ("FAILED", "BLOCKED"):
            group["failed_blocked"] += 1
        else:
            group["other"] += 1
        duration = data.get("duration_seconds")
        if is_number(duration):
            group["duration_seconds"] += float(duration)
            group["duration_known"] = True
        mode = mask_text(data.get("mode"))
        if mode and mode not in group["modes"]:
            group["modes"].append(mode)
        if status in ("FAILED", "BLOCKED"):
            reason = mask_text(data.get("completion_reason")) or "?"
            failed_key = (agent, model, status, reason)
            failed_groups[failed_key] = failed_groups.get(failed_key, 0) + 1

    rows = []
    for group in groups.values():
        minutes = round(group["duration_seconds"] / 60.0, 1) if group["duration_known"] else None
        rows.append(
            {
                "agent": group["agent"],
                "model": group["model"],
                "runs": group["runs"],
                "completed": group["completed"],
                "partial": group["partial"],
                "failed_blocked": group["failed_blocked"],
                "other": group["other"],
                "minutes": minutes,
                "minutes_display": f"{minutes}" if minutes is not None else "?",
                "share_pct": pct(group["runs"], total),
                "modes": sorted(group["modes"]),
            }
        )
    rows.sort(key=lambda row: (-row["runs"], row["agent"], row["model"]))

    failed_rows = [
        {
            "agent": agent,
            "model": model,
            "run_status": status,
            "completion_reason": reason,
            "count": count,
        }
        for (agent, model, status, reason), count in failed_groups.items()
    ]
    failed_rows.sort(
        key=lambda row: (
            -row["count"],
            row["agent"],
            row["model"],
            row["run_status"],
            row["completion_reason"],
        )
    )

    section["rows"] = rows[:MAX_LIST_ITEMS]
    section["rows_total"] = len(rows)
    section["other_total"] = sum(row["other"] for row in rows)
    section["total_runs"] = total
    section["skipped_files"] = skipped
    section["failed_groups"] = failed_rows[:MAX_FAILED_GROUPS]
    section["failed_groups_total"] = len(failed_rows)
    section["failed_total"] = sum(row["count"] for row in failed_rows)
    if total == 0:
        section["status"] = "no_data"
        section["reason"] = "brak runow w oknie"
    else:
        section["status"] = "ok"
        section["reason"] = None
    return section


# --------------------------------------------------------------------------
# Zrodlo 2: Helpdesk RSI
# --------------------------------------------------------------------------


def normalize_status(value: Any) -> str:
    """Status zrodla w postaci porownywalnej: 'needs-owner' == 'needs_owner'."""
    return (mask_text(value) or "").strip().lower().replace("_", "-").replace(" ", "-")


def last_needs_owner_note(data: dict[str, Any]) -> str:
    """Pytanie do Mateusza: `note` z OSTATNIEGO wpisu `history` o statusie
    'needs-owner' (skrocone i zamaskowane). Brak -> "?"."""
    history = data.get("history")
    note: Any = None
    if isinstance(history, list):
        for entry in history:
            if not isinstance(entry, dict):
                continue
            if normalize_status(entry.get("status")) == NEEDS_OWNER_STATUS:
                note = entry.get("note")
    return mask_text(note) or "?"


def read_helpdesk(home: Path, since: dt.datetime, until: dt.datetime) -> dict[str, Any]:
    """Zgloszenia RSI: otwarte, nowe w oknie, pytania do wlasciciela."""
    tickets_dir = home / "helpdesk" / "tickets"
    section: dict[str, Any] = {
        "status": "no_data",
        "reason": None,
        "open": [],
        "open_total": 0,
        "new": [],
        "new_total": 0,
        "awaiting_owner": [],
        "awaiting_owner_total": 0,
        "needs_owner": [],
        "needs_owner_total": 0,
        "counts": {},
        "skipped_files": 0,
        "path": str(tickets_dir),
    }
    if not tickets_dir.is_dir():
        section["status"] = "missing"
        section["reason"] = f"brak katalogu {tickets_dir}"
        return section

    open_tickets: list[dict[str, Any]] = []
    new_tickets: list[dict[str, Any]] = []
    awaiting: list[dict[str, Any]] = []
    needs_owner: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    skipped = 0

    for path in sorted(tickets_dir.glob("*.json")):
        data, error = read_json_file(path)
        if error is not None or not isinstance(data, dict):
            skipped += 1
            continue
        status = mask_text(data.get("status")) or "?"
        status_key = status.strip().lower()
        status_norm = normalize_status(data.get("status"))
        count_value = data.get("count")
        record = {
            "id": mask_text(data.get("id")) or "?",
            "status": status,
            "component": mask_text(data.get("component")) or "?",
            "symptom": mask_text(data.get("symptom")) or "?",
            "last_seen": mask_text(data.get("last_seen")) or "?",
            "count": count_value if is_number(count_value) else "?",
            "awaiting_owner": bool(data.get("awaiting_owner")),
        }
        counts[status] = counts.get(status, 0) + 1
        if status_key not in TERMINAL_TICKET_STATUS:
            open_tickets.append(record)
        first_seen = parse_iso(data.get("first_seen"))
        last_seen = parse_iso(data.get("last_seen"))
        stamp = first_seen or last_seen
        if stamp is not None and since <= stamp <= until:
            new_tickets.append(record)

        if status_norm == NEEDS_OWNER_STATUS:
            # Realne pytanie do Mateusza - zawsze na gorze sekcji decyzji.
            record["question"] = last_needs_owner_note(data)
            record["label"] = "pytanie do wlasciciela"
            needs_owner.append(record)
        elif record["awaiting_owner"]:
            record["question"] = ""
            record["label"] = (
                CANDIDATE_LABEL if status_norm == CANDIDATE_STATUS else AWAITING_LABEL
            )
            awaiting.append(record)

    section["open"] = open_tickets[:MAX_LIST_ITEMS]
    section["open_total"] = len(open_tickets)
    section["new"] = new_tickets[:MAX_LIST_ITEMS]
    section["new_total"] = len(new_tickets)
    section["awaiting_owner"] = awaiting[:MAX_LIST_ITEMS]
    section["awaiting_owner_total"] = len(awaiting)
    section["needs_owner"] = needs_owner[:MAX_LIST_ITEMS]
    section["needs_owner_total"] = len(needs_owner)
    section["counts"] = counts
    section["skipped_files"] = skipped
    if not (open_tickets or new_tickets or awaiting or needs_owner):
        section["status"] = "no_data"
        section["reason"] = "brak zgloszen w oknie"
    else:
        section["status"] = "ok"
    return section


# --------------------------------------------------------------------------
# Zrodlo 3: raporty hookow Claude (tylko odczyt)
# --------------------------------------------------------------------------


def _unavailable_guard(kind: str, reason: str) -> dict[str, Any]:
    """Wspolny bezpieczny wynik, gdy lokalny raport hooka jest niedostepny."""
    section: dict[str, Any] = {"status": "unavailable", "reason": reason}
    if kind == "delegation":
        section.update(
            {
                "sessions": "?",
                "code_edits": "?",
                "delegations": "?",
                "exceptions_total": "?",
                "exceptions": [],
                "exceptions_listed_total": 0,
                "over_limit": "?",
            }
        )
    else:
        section.update(
            {
                "sessions": "?",
                "blocked": "?",
                "allowed": "?",
                "grants": "?",
                "grants_list": [],
                "grants_listed_total": 0,
            }
        )
    return section


def read_claude_guards(home: Path) -> dict[str, Any]:
    """Tylko odczyt: uruchamia raport JSON obu lokalnych hookow Claude."""
    result: dict[str, Any] = {}
    try:
        configured_dir = os.environ.get("OWNER_DIGEST_HOOKS_DIR", "").strip()
        hooks_dir = (
            Path(configured_dir)
            if configured_dir
            else Path(__file__).resolve().parents[2] / "global" / "claude" / "hooks"
        )
        child_env = os.environ.copy()
        child_env["DELEGATION_GUARD_HOME"] = str(home / "delegation-guard")
        child_env["BROWSER_GUARD_HOME"] = str(home / "browser-guard")
        # Stdout dziecka w potoku na Windows to cp125x: znak spoza strony kodowej
        # w powodzie wyjatku wywalal raport. Wymuszamy UTF-8 (czytamy jako UTF-8).
        child_env["PYTHONIOENCODING"] = "utf-8"

        specs = (
            (
                "delegation",
                "delegation_guard.py",
                "DELEGATION_GUARD_HOME",
            ),
            ("browser", "browser_guard.py", "BROWSER_GUARD_HOME"),
        )
        for kind, filename, _home_env in specs:
            script = hooks_dir / filename
            if not script.is_file():
                result[kind] = _unavailable_guard(kind, f"brak skryptu {filename}")
                continue
            argv = [sys.executable, str(script), "report", "--days", "1", "--json"]
            try:
                completed = subprocess.run(
                    argv,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=GUARD_TIMEOUT_SECONDS,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    env=child_env,
                    shell=False,
                )
            except subprocess.TimeoutExpired:
                result[kind] = _unavailable_guard(kind, "timeout")
                continue
            except Exception as exc:
                result[kind] = _unavailable_guard(
                    kind, f"blad uruchomienia: {mask_text(exc)}"
                )
                continue

            if completed.returncode != 0:
                result[kind] = _unavailable_guard(
                    kind, f"kod wyjscia {mask_text(completed.returncode)}"
                )
                continue
            try:
                data = json.loads(completed.stdout)
            except (TypeError, ValueError):
                result[kind] = _unavailable_guard(kind, "niepoprawny JSON")
                continue
            if not isinstance(data, dict):
                result[kind] = _unavailable_guard(kind, "JSON nie jest obiektem")
                continue
            totals = data.get("totals")
            if not isinstance(totals, dict):
                result[kind] = _unavailable_guard(kind, "brak lub zle totals")
                continue
            sessions = data.get("sessions")
            if not isinstance(sessions, list):
                sessions = []

            if kind == "delegation":
                exceptions: list[dict[str, str]] = []
                over_limit = 0
                for session in sessions:
                    if not isinstance(session, dict):
                        continue
                    edits = session.get("code_edits")
                    limit = session.get("limit")
                    if is_number(edits) and is_number(limit) and edits > limit:
                        over_limit += 1
                    rows = session.get("exceptions")
                    if not isinstance(rows, list):
                        continue
                    session_id = mask_text(str(session.get("session") or "?")[:8]) or "?"
                    for item in rows:
                        if not isinstance(item, dict):
                            continue
                        exceptions.append(
                            {
                                "session": session_id,
                                "ts": mask_text(item.get("ts")) or "?",
                                "grant": mask_text(item.get("grant")) or "?",
                                "reason": mask_text(item.get("reason")) or "?",
                            }
                        )
                result[kind] = {
                    "status": "ok",
                    "reason": None,
                    "sessions": number_or_q(totals.get("sessions")),
                    "code_edits": number_or_q(totals.get("code_edits")),
                    "delegations": number_or_q(totals.get("delegations")),
                    "exceptions_total": number_or_q(totals.get("exceptions")),
                    "exceptions": exceptions[:MAX_LIST_ITEMS],
                    "exceptions_listed_total": len(exceptions),
                    "over_limit": over_limit,
                }
            else:
                grants: list[dict[str, str]] = []
                for session in sessions:
                    if not isinstance(session, dict):
                        continue
                    rows = session.get("grants")
                    if not isinstance(rows, list):
                        continue
                    session_id = mask_text(str(session.get("session") or "?")[:8]) or "?"
                    for item in rows:
                        if not isinstance(item, dict):
                            continue
                        grants.append(
                            {
                                "session": session_id,
                                "ts": mask_text(item.get("ts")) or "?",
                                "expires_at": mask_text(item.get("expires_at")) or "?",
                                "reason": mask_text(item.get("reason")) or "?",
                            }
                        )
                result[kind] = {
                    "status": "ok",
                    "reason": None,
                    "sessions": number_or_q(totals.get("sessions")),
                    "blocked": number_or_q(totals.get("blocked")),
                    "allowed": number_or_q(totals.get("allowed")),
                    "grants": number_or_q(totals.get("grants")),
                    "grants_list": grants[:MAX_LIST_ITEMS],
                    "grants_listed_total": len(grants),
                }
    except Exception as exc:
        # Nigdy nie blokujemy digestu problemem z uruchomieniem raportu.
        for kind in ("delegation", "browser"):
            result.setdefault(kind, _unavailable_guard(kind, f"blad raportu: {mask_text(exc)}"))
    return result


# --------------------------------------------------------------------------
# Zrodlo 4: kolejka RSI T2
# --------------------------------------------------------------------------


T2_TICKET_ID = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


def read_rsi_t2(home: Path) -> dict[str, Any]:
    """Kolejka RSI czekajaca na decyzje wlasciciela, z bezpiecznym wzbogaceniem."""
    path = home / "helpdesk" / "triage-state.json"
    section: dict[str, Any] = {
        "status": "unavailable",
        "reason": None,
        "items": [],
        "items_total": 0,
        "contexts": [],
        "path": str(path),
    }
    try:
        if not path.is_file():
            section["reason"] = f"brak pliku {path}"
            return section
        state, error = read_json_file(path)
        if error is not None or not isinstance(state, dict):
            section["reason"] = error or "plik nie jest obiektem JSON"
            return section

        records = (
            ("triage", state.get("last_result")),
            ("owner-queue", state.get("owner_queue_last_result")),
        )
        ids: dict[str, set[str]] = {}
        contexts: list[dict[str, str]] = []
        for source, record in records:
            if not isinstance(record, dict):
                contexts.append({"source": source, "status": "?", "result": "?"})
                continue
            contexts.append(
                {
                    "source": source,
                    "status": mask_text(record.get("status")) or "?",
                    "result": mask_text(record.get("result")) or "?",
                }
            )
            waiting = record.get("t2_waiting")
            if not isinstance(waiting, list):
                continue
            for ticket_id in waiting:
                if isinstance(ticket_id, str):
                    ids.setdefault(ticket_id, set()).add(source)

        items: list[dict[str, str | list[str]]] = []
        for ticket_id in sorted(ids):
            safe_path_id = bool(T2_TICKET_ID.fullmatch(ticket_id)) and ticket_id not in (".", "..")
            ticket_data: Any = None
            if safe_path_id:
                ticket_path = home / "helpdesk" / "tickets" / f"{ticket_id}.json"
                try:
                    if ticket_path.is_file():
                        ticket_data, ticket_error = read_json_file(ticket_path)
                        if ticket_error is not None or not isinstance(ticket_data, dict):
                            ticket_data = None
                except Exception:
                    # Problem z pojedynczym biletem nie uniewaznia calej kolejki.
                    ticket_data = None
            items.append(
                {
                    "id": mask_text(ticket_id) or "?",
                    "sources": [
                        source
                        for source in ("triage", "owner-queue")
                        if source in ids[ticket_id]
                    ],
                    "source": "/".join(
                        source
                        for source in ("triage", "owner-queue")
                        if source in ids[ticket_id]
                    ),
                    "component": mask_text(ticket_data.get("component")) or "?"
                    if isinstance(ticket_data, dict)
                    else "?",
                    "symptom": mask_text(ticket_data.get("symptom")) or "?"
                    if isinstance(ticket_data, dict)
                    else "?",
                    "status": mask_text(ticket_data.get("status")) or "?"
                    if isinstance(ticket_data, dict)
                    else "?",
                }
            )
        section.update(
            {
                "status": "ok",
                "reason": None,
                "items": items,
                "items_total": len(items),
                "contexts": contexts,
            }
        )
        return section
    except Exception as exc:
        section["reason"] = f"blad odczytu: {mask_text(exc)}"
        return section


# --------------------------------------------------------------------------
# Zrodlo 5: Decyzje wlasciciela
# --------------------------------------------------------------------------


def read_decisions(home: Path, since: dt.datetime, until: dt.datetime) -> dict[str, Any]:
    """Dziennik decyzji: pytanie -> wybor, tylko z okna."""
    store = home / "owner-decisions" / "decisions.jsonl"
    section: dict[str, Any] = {
        "status": "no_data",
        "reason": None,
        "items": [],
        "items_total": 0,
        "skipped_lines": 0,
        "path": str(store),
    }
    if not store.is_file():
        section["status"] = "missing"
        section["reason"] = f"brak pliku {store}"
        return section
    try:
        text = store.read_text(encoding="utf-8-sig")
    except OSError as exc:
        section["status"] = "invalid"
        section["reason"] = f"nie mozna odczytac pliku ({exc.strerror or exc})"
        return section

    items: list[dict[str, Any]] = []
    skipped = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            data = json.loads(stripped)
        except ValueError:
            skipped += 1
            continue
        if not isinstance(data, dict):
            skipped += 1
            continue
        stamp = parse_iso(data.get("ts"))
        if stamp is None or not (since <= stamp <= until):
            continue
        items.append(
            {
                "ts": iso(stamp),
                "project": mask_text(data.get("project")) or "?",
                "category": mask_text(data.get("category")) or "?",
                "question": mask_text(data.get("question")) or "?",
                "choice": mask_text(data.get("choice")) or "?",
            }
        )

    items.sort(key=lambda item: item["ts"])
    section["items"] = items[:MAX_LIST_ITEMS]
    section["items_total"] = len(items)
    section["skipped_lines"] = skipped
    if not items:
        section["status"] = "no_data"
        section["reason"] = "brak decyzji w oknie"
    else:
        section["status"] = "ok"
    return section


# --------------------------------------------------------------------------
# Zrodlo 6: Limit Claude
# --------------------------------------------------------------------------


def read_claude_limit(home: Path, until: dt.datetime) -> dict[str, Any]:
    """Probka limitu Claude + wiek probki (starsza niz 60 min = nieaktualna)."""
    path = home / "usage" / "claude-plan-usage.json"
    section: dict[str, Any] = {
        "status": "no_data",
        "reason": None,
        "observed_at": None,
        "age_minutes": None,
        "stale": None,
        "five_hour_pct": None,
        "weekly_all_pct": None,
        "extra_usage_enabled": None,
        "path": str(path),
    }
    if not path.is_file():
        section["status"] = "missing"
        section["reason"] = f"brak pliku {path}"
        return section
    data, error = read_json_file(path)
    if error is not None or not isinstance(data, dict):
        section["status"] = "invalid"
        section["reason"] = error or "plik nie jest obiektem JSON"
        return section

    observed = parse_iso(data.get("observed_at"))
    if observed is None:
        section["status"] = "invalid"
        section["reason"] = "brak poprawnego observed_at"
        return section
    if observed > until + dt.timedelta(minutes=1):
        section["status"] = "invalid"
        section["reason"] = "observed_at w przyszlosci"
        return section

    age = max(0.0, (until - observed).total_seconds() / 60.0)
    stale = age > STALE_MINUTES
    section["status"] = "ok"
    section["observed_at"] = iso(observed)
    section["age_minutes"] = round(age, 1)
    section["stale"] = stale
    section["five_hour_pct"] = data.get("five_hour_pct") if is_number(data.get("five_hour_pct")) else None
    section["weekly_all_pct"] = (
        data.get("weekly_all_pct") if is_number(data.get("weekly_all_pct")) else None
    )
    extra = data.get("extra_usage_enabled")
    section["extra_usage_enabled"] = extra if isinstance(extra, bool) else None
    return section


# --------------------------------------------------------------------------
# Zrodlo 7: Zadania TODO
# --------------------------------------------------------------------------


def _superseded_by_accepted(
    path: Path,
    mtime: dt.datetime,
    accepted: list[tuple[Path, dt.datetime]],
) -> bool:
    """Czy BLOCKED zostal nadpisany pozniejszym ACCEPTED?

    Nadpisuje, gdy w tym samym katalogu zadania (task-work/<task>/) jest pozniejszy
    result.json z ACCEPTED:
    - dla tego samego task id (nazwa katalogu trzymajacego result.json), albo
    - dla tego samego prefiksu runu (run-v2 vs run-v2b).
    """
    role_dir = path.parent
    run_dir = path.parent.parent
    role_name = role_dir.name
    prefix = run_prefix(run_dir.name)
    for other_path, other_mtime in accepted:
        if other_mtime <= mtime:
            continue
        if other_path.parent.name == role_name:
            return True
        if run_prefix(other_path.parent.parent.name) == prefix:
            return True
    return False


def read_todo(home: Path, since: dt.datetime, until: dt.datetime) -> dict[str, Any]:
    """Zadania TODO: result.json w oknie (po mtime), acceptance_status.

    BLOCKED nadpisany pozniejszym ACCEPTED (ten sam task id lub ten sam prefiks
    runu) jest pomijany - inaczej lista "Wymaga Twojej decyzji" zalewa sie
    szumem. Zduplikowane wpisy (ten sam katalog runu i ten sam reason) tez.
    """
    task_root = home / "task-work"
    section: dict[str, Any] = {
        "status": "no_data",
        "reason": None,
        "counts": {},
        "blocked": [],
        "blocked_total": 0,
        "total": 0,
        "skipped_files": 0,
        "superseded": 0,
        "path": str(task_root),
    }
    if not task_root.is_dir():
        section["status"] = "missing"
        section["reason"] = f"brak katalogu {task_root}"
        return section

    counts: dict[str, int] = {}
    blocked: list[dict[str, Any]] = []
    total = 0
    skipped = 0
    superseded = 0

    try:
        task_dirs = sorted(p for p in task_root.iterdir() if p.is_dir())
    except OSError as exc:
        section["status"] = "invalid"
        section["reason"] = f"nie mozna przejsc katalogu ({exc.strerror or exc})"
        return section

    for task_dir in task_dirs:
        records: list[tuple[Path, dt.datetime, dict[str, Any]]] = []
        for path in sorted(task_dir.glob("run-*/*/result.json")):
            try:
                mtime = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)
            except OSError:
                skipped += 1
                continue
            data, error = read_json_file(path)
            if error is not None or not isinstance(data, dict):
                if since <= mtime <= until:
                    skipped += 1
                continue
            records.append((path, mtime, data))

        accepted = [
            (path, mtime)
            for path, mtime, data in records
            if (mask_text(data.get("acceptance_status")) or "?").strip().upper() == "ACCEPTED"
        ]

        for path, mtime, data in records:
            if not (since <= mtime <= until):
                continue
            status = mask_text(data.get("acceptance_status")) or "?"
            key = status.strip().upper()
            if key == "BLOCKED":
                if _superseded_by_accepted(path, mtime, accepted):
                    superseded += 1
                    continue
                run_dir = path.parent.parent
                try:
                    run_display = str(run_dir.relative_to(home))
                except ValueError:
                    run_display = str(run_dir)
                blocked.append(
                    {
                        "run": run_display,
                        "status": status,
                        "reason": mask_text(data.get("reason")) or "?",
                        "modified_at": iso(mtime),
                    }
                )
            total += 1
            counts[key] = counts.get(key, 0) + 1

    # Deduplikacja identycznych wpisow (ten sam katalog runu + ten sam reason).
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for entry in sorted(blocked, key=lambda item: item["modified_at"], reverse=True):
        dedup_key = (entry["run"], entry["reason"])
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        unique.append(entry)

    section["counts"] = counts
    section["blocked"] = unique[:MAX_LIST_ITEMS]
    section["blocked_total"] = len(unique)
    section["total"] = total
    section["skipped_files"] = skipped
    section["superseded"] = superseded
    if total == 0:
        section["status"] = "no_data"
        section["reason"] = "brak zadan w oknie"
    else:
        section["status"] = "ok"
    return section


# --------------------------------------------------------------------------
# Budowa digestu
# --------------------------------------------------------------------------


def build_digest(
    *,
    home: Path | str | None = None,
    since_hours: float = 24.0,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Zwraca komplet danych digestu (to samo, co --json)."""
    root = Path(home) if home is not None else agents_home()
    until = now or dt.datetime.now(dt.timezone.utc)
    if until.tzinfo is None:
        until = until.replace(tzinfo=dt.timezone.utc)
    until = until.astimezone(dt.timezone.utc)
    since = until - dt.timedelta(hours=float(since_hours))

    fleet = read_fleet(root, since, until)
    helpdesk = read_helpdesk(root, since, until)
    claude_guards = read_claude_guards(root)
    rsi_t2 = read_rsi_t2(root)
    decisions = read_decisions(root, since, until)
    limit = read_claude_limit(root, until)
    todo = read_todo(root, since, until)

    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "generated_at": iso(until),
        "date": until.date().isoformat(),
        "window": {"since": iso(since), "until": iso(until), "hours": float(since_hours)},
        "home": str(root),
        "sources": {
            "fleet": fleet,
            "helpdesk": helpdesk,
            "claude_guards": claude_guards,
            "rsi_t2": rsi_t2,
            "decisions": decisions,
            "claude_limit": limit,
            "todo": todo,
        },
    }
    needs_owner_total = int(helpdesk.get("needs_owner_total") or 0)
    awaiting_owner_total = int(helpdesk.get("awaiting_owner_total") or 0)
    blocked_total = int(todo.get("blocked_total") or 0)
    helpdesk_decision_ids = {
        ticket.get("id")
        for field in ("needs_owner", "awaiting_owner")
        for ticket in (helpdesk.get(field) or [])
        if isinstance(ticket, dict)
    }
    t2_decisions = [
        item
        for item in (rsi_t2.get("items") or [])
        if item.get("id") not in helpdesk_decision_ids
    ]
    payload["decision_needed"] = {
        "helpdesk_needs_owner": helpdesk.get("needs_owner") or [],
        "helpdesk_awaiting_owner": helpdesk.get("awaiting_owner") or [],
        "todo_blocked": todo.get("blocked") or [],
        "rsi_t2_waiting": t2_decisions[:MAX_LIST_ITEMS],
        "helpdesk_needs_owner_total": needs_owner_total,
        "helpdesk_awaiting_owner_total": awaiting_owner_total,
        "todo_blocked_total": blocked_total,
        "rsi_t2_waiting_total": len(t2_decisions),
        "count": needs_owner_total + awaiting_owner_total + blocked_total + len(t2_decisions),
    }
    return payload


# --------------------------------------------------------------------------
# Renderowanie Markdown
# --------------------------------------------------------------------------


def cell(value: Any) -> str:
    """Tekst bezpieczny dla wiersza tabeli."""
    text = str(value) if value is not None else "?"
    return text.replace("|", "\\|")


def render_markdown(payload: dict[str, Any]) -> str:
    """Markdown z tych samych danych co --json. Kazda lista: max 10 pozycji."""
    window = payload["window"]
    sources = payload["sources"]
    needed = payload["decision_needed"]
    lines: list[str] = []

    lines.append(f"# Digest dla Mateusza - {payload['date']}")
    lines.append("")
    lines.append(f"- Okno: {window['since']} -> {window['until']} ({_fmt_hours(window['hours'])})")
    lines.append(f"- Wygenerowano: {payload['generated_at']}")
    lines.append("- Raport lokalny, nic nie zostalo wyslane.")
    lines.append("")

    # --- 1. Wymaga Twojej decyzji (zawsze na gorze) -------------------------
    lines.append("## Wymaga Twojej decyzji")
    lines.append("")
    items: list[str] = []

    shown = needed["helpdesk_needs_owner"]
    more = max(0, needed.get("helpdesk_needs_owner_total", len(shown)) - len(shown))
    for ticket in shown:
        question = ticket.get("question") or ticket.get("symptom") or "?"
        items.append(
            f"- Helpdesk {cell(ticket['id'])} (pytanie do wlasciciela): "
            f"{cell(question)} [{cell(ticket['component'])}]"
        )
    if more:
        items.append(f"- ... i {more} wiecej zgloszen needs-owner")

    shown = needed["helpdesk_awaiting_owner"]
    more = max(0, needed.get("helpdesk_awaiting_owner_total", len(shown)) - len(shown))
    for ticket in shown:
        label = ticket.get("label") or AWAITING_LABEL
        items.append(
            f"- Helpdesk {cell(ticket['id'])} ({label}): "
            f"{cell(ticket['symptom'])} [{cell(ticket['component'])}]"
        )
    if more:
        items.append(f"- ... i {more} wiecej zgloszen czekajacych na akceptacje")

    shown = needed["todo_blocked"]
    more = max(0, needed.get("todo_blocked_total", len(shown)) - len(shown))
    for task in shown:
        items.append(f"- TODO zablokowane {cell(task['run'])}: {cell(task['reason'])}")
    if more:
        items.append(f"- ... i {more} wiecej zablokowanych zadan")

    shown = needed.get("rsi_t2_waiting", [])
    more = max(0, needed.get("rsi_t2_waiting_total", len(shown)) - len(shown))
    for ticket in shown:
        items.append(
            f"- RSI T2 {cell(ticket['id'])}: {cell(ticket['symptom'])} "
            f"[{cell(ticket['component'])}] (czeka na Twoja decyzje)"
        )
    if more:
        items.append(f"- ... i {more} wiecej RSI T2")

    if items:
        lines.extend(items)
    else:
        lines.append("- Brak - nic nie czeka na Twoja decyzje.")
    lines.append("")

    # --- 2. Fleet -----------------------------------------------------------
    fleet = sources["fleet"]
    lines.append("## Fleet - runy w oknie")
    lines.append("")
    if fleet["status"] != "ok":
        lines.append(f"brak danych: {fleet['reason']}")
        lines.append("")
    else:
        shown = fleet["rows"]
        more = max(0, fleet.get("rows_total", len(shown)) - len(shown))
        lines.append(
            "| agent | model | runy | COMPLETED | PARTIAL | FAILED/BLOCKED | czas (min) | udzial % |"
        )
        lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |")
        for row in shown:
            lines.append(
                "| {agent} | {model} | {runs} | {completed} | {partial} | {failed_blocked} | "
                "{minutes} | {share} |".format(
                    agent=cell(row["agent"]),
                    model=cell(row["model"]),
                    runs=row["runs"],
                    completed=row["completed"],
                    partial=row["partial"],
                    failed_blocked=row["failed_blocked"],
                    minutes=cell(row["minutes_display"]),
                    share=f"{row['share_pct']}%",
                )
            )
        if more:
            lines.append(f"... i {more} wiecej")
        lines.append("")
        lines.append(f"Razem runow w oknie: {fleet['total_runs']}.")
        other_total = fleet.get("other_total", sum(row["other"] for row in fleet["rows"]))
        if other_total:
            lines.append(f"Stany inne niz COMPLETED/PARTIAL/FAILED/BLOCKED: {other_total}.")
        if fleet["skipped_files"]:
            lines.append(f"Pominieto pliki nieczytelne: {fleet['skipped_files']}.")

        groups = fleet["failed_groups"]
        more_groups = max(0, fleet.get("failed_groups_total", len(groups)) - len(groups))
        if groups:
            lines.append("")
            lines.append(
                f"Nieudane runy w oknie (zgrupowane, razem {fleet['failed_total']}):"
            )
            lines.append("")
            lines.append("| agent | model | run_status | completion_reason | liczba |")
            lines.append("| --- | --- | --- | --- | ---: |")
            for group in groups:
                lines.append(
                    "| {agent} | {model} | {status} | {reason} | {count} |".format(
                        agent=cell(group["agent"]),
                        model=cell(group["model"]),
                        status=cell(group["run_status"]),
                        reason=cell(group["completion_reason"]),
                        count=group["count"],
                    )
                )
            if more_groups:
                lines.append(f"... i {more_groups} wiecej grup")
        lines.append("")

    # --- 3. Helpdesk --------------------------------------------------------
    helpdesk = sources["helpdesk"]
    lines.append("## Helpdesk RSI")
    lines.append("")
    if helpdesk["status"] == "missing":
        lines.append(f"brak danych: {helpdesk['reason']}")
        lines.append("")
    else:
        lines.append(
            f"- Otwarte zgloszenia: {helpdesk.get('open_total', len(helpdesk['open']))}"
            f" | nowe w oknie: {helpdesk.get('new_total', len(helpdesk['new']))}"
            f" | pytania do Ciebie: "
            f"{helpdesk.get('needs_owner_total', len(helpdesk['needs_owner']))}"
            f" | czeka na akceptacje: "
            f"{helpdesk.get('awaiting_owner_total', len(helpdesk['awaiting_owner']))}"
        )
        if helpdesk["skipped_files"]:
            lines.append(f"- Pominieto pliki nieczytelne: {helpdesk['skipped_files']}.")
        if helpdesk["open"]:
            lines.append("")
            lines.append("| id | status | komponent | objaw | ostatnio | licznik |")
            lines.append("| --- | --- | --- | --- | --- | ---: |")
            shown = helpdesk["open"]
            more = max(0, helpdesk.get("open_total", len(shown)) - len(shown))
            for ticket in shown:
                lines.append(
                    "| {id} | {status} | {component} | {symptom} | {last_seen} | {count} |".format(
                        id=cell(ticket["id"]),
                        status=cell(ticket["status"]),
                        component=cell(ticket["component"]),
                        symptom=cell(ticket["symptom"]),
                        last_seen=cell(ticket["last_seen"]),
                        count=cell(ticket["count"]),
                    )
                )
            if more:
                lines.append(f"... i {more} wiecej")
        else:
            lines.append("- Brak otwartych zgloszen.")
        lines.append("")

    # --- 4. RSI T2 ----------------------------------------------------------
    rsi_t2 = sources["rsi_t2"]
    lines.append("## RSI - czeka na Twoja decyzje (T2)")
    lines.append("")
    if rsi_t2["status"] != "ok":
        lines.append(f"niedostepne: {cell(rsi_t2['reason'])}")
    elif not rsi_t2["items"]:
        lines.append("- Brak - triaz nic nie odlozyl do Twojej decyzji.")
    else:
        contexts = rsi_t2.get("contexts", [])
        if contexts:
            context_text = " | ".join(
                f"{cell(item['source'])}: {cell(item['status'])} ({cell(item['result'])})"
                for item in contexts
            )
            lines.append(f"- Kontekst: {context_text}")
        shown = rsi_t2["items"][:MAX_LIST_ITEMS]
        for ticket in shown:
            lines.append(
                f"- {cell(ticket['id'])} [{cell(ticket['component'])}] "
                f"{cell(ticket['symptom'])} (zrodlo: {cell(ticket['source'])})"
            )
        more = max(0, rsi_t2.get("items_total", len(rsi_t2["items"])) - len(shown))
        if more:
            lines.append(f"... i {more} wiecej")
    lines.append("")

    # --- 5. Hooki Claude ----------------------------------------------------
    guards = sources["claude_guards"]
    lines.append("## Hooki Claude (ostatnia doba)")
    lines.append("")
    delegation = guards["delegation"]
    if delegation["status"] != "ok":
        lines.append(f"delegation_guard: niedostepne: {cell(delegation['reason'])}")
    else:
        lines.append(
            "- Delegacje: sesje {sessions} | wlasne edycje kodu {edits} | "
            "delegacje {delegations} | wyjatki {exceptions} | sesje ponad limit {over_limit}".format(
                sessions=number_or_q(delegation["sessions"]),
                edits=number_or_q(delegation["code_edits"]),
                delegations=number_or_q(delegation["delegations"]),
                exceptions=number_or_q(delegation["exceptions_total"]),
                over_limit=number_or_q(delegation["over_limit"]),
            )
        )
        shown = delegation["exceptions"]
        for item in shown:
            lines.append(
                f"- wyjatek {cell(item['ts'])} sesja {cell(item['session'])} "
                f"(grant {cell(item['grant'])}): {cell(item['reason'])}"
            )
        more = max(
            0, delegation.get("exceptions_listed_total", len(shown)) - len(shown)
        )
        if more:
            lines.append(f"... i {more} wiecej")

    browser = guards["browser"]
    if browser["status"] != "ok":
        lines.append(f"browser_guard: niedostepne: {cell(browser['reason'])}")
    else:
        lines.append(
            "- Prywatny Chrome: blokady {blocked} | dozwolone {allowed} | granty {grants}".format(
                blocked=number_or_q(browser["blocked"]),
                allowed=number_or_q(browser["allowed"]),
                grants=number_or_q(browser["grants"]),
            )
        )
        shown = browser["grants_list"]
        for item in shown:
            lines.append(
                f"- grant {cell(item['ts'])} sesja {cell(item['session'])} "
                f"(wazny do {cell(item['expires_at'])}): {cell(item['reason'])}"
            )
        more = max(0, browser.get("grants_listed_total", len(shown)) - len(shown))
        if more:
            lines.append(f"... i {more} wiecej")
    lines.append("")

    # --- 6. Decyzje ---------------------------------------------------------
    decisions = sources["decisions"]
    lines.append("## Decyzje wlasciciela")
    lines.append("")
    if decisions["status"] != "ok":
        lines.append(f"brak danych: {decisions['reason']}")
        lines.append("")
    else:
        lines.append("| czas | projekt | kategoria | pytanie | wybor |")
        lines.append("| --- | --- | --- | --- | --- |")
        shown = decisions["items"]
        more = max(0, decisions.get("items_total", len(shown)) - len(shown))
        for item in shown:
            lines.append(
                "| {ts} | {project} | {category} | {question} | {choice} |".format(
                    ts=cell(item["ts"]),
                    project=cell(item["project"]),
                    category=cell(item["category"]),
                    question=cell(item["question"]),
                    choice=cell(item["choice"]),
                )
            )
        if more:
            lines.append(f"... i {more} wiecej")
        lines.append("")

    # --- 7. Limit Claude ----------------------------------------------------
    limit = sources["claude_limit"]
    lines.append("## Limit Claude")
    lines.append("")
    if limit["status"] != "ok":
        lines.append(f"brak danych: {limit['reason']}")
        lines.append("")
    else:
        freshness = "nieaktualna" if limit["stale"] else "aktualna"
        extra = limit["extra_usage_enabled"]
        extra_text = "?" if extra is None else ("tak" if extra else "nie")
        lines.append(
            "- Okno 5h: {five} | tydzien (all models): {weekly} | dodatkowe uzycie: {extra}".format(
                five=number_or_q(limit["five_hour_pct"]),
                weekly=number_or_q(limit["weekly_all_pct"]),
                extra=extra_text,
            )
        )
        lines.append(
            "- Probka z {observed} (wiek {age} min) - {freshness}".format(
                observed=limit["observed_at"],
                age=limit["age_minutes"],
                freshness=freshness,
            )
        )
        lines.append("")

    # --- 8. TODO ------------------------------------------------------------
    todo = sources["todo"]
    lines.append("## Zadania TODO")
    lines.append("")
    if todo["status"] != "ok":
        lines.append(f"brak danych: {todo['reason']}")
        lines.append("")
    else:
        counts = ", ".join(f"{status}: {count}" for status, count in sorted(todo["counts"].items()))
        lines.append(f"- Zadania w oknie: {todo['total']} ({counts})")
        if todo["skipped_files"]:
            lines.append(f"- Pominieto pliki nieczytelne: {todo['skipped_files']}.")
        if todo.get("superseded"):
            lines.append(
                f"- Pominieto zablokowane nadpisane pozniejszym ACCEPTED: {todo['superseded']}."
            )
        if todo["blocked"]:
            lines.append("")
            lines.append("Zablokowane:")
            shown = todo["blocked"]
            more = max(0, todo.get("blocked_total", len(shown)) - len(shown))
            for task in shown:
                lines.append(f"- {cell(task['run'])}: {cell(task['reason'])}")
            if more:
                lines.append(f"... i {more} wiecej")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _fmt_hours(hours: float) -> str:
    if float(hours).is_integer():
        return f"{int(hours)} h"
    return f"{hours} h"


# --------------------------------------------------------------------------
# Zapis
# --------------------------------------------------------------------------


def write_atomic(path: Path, text: str) -> Path:
    """Zapis atomowy: plik tymczasowy w tym samym katalogu + os.replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle_fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f"{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        # Usuwamy wylacznie wlasny plik tymczasowy (zadna inna dana nie jest ruszana).
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise
    return path


def default_output_path(home: Path, date: str) -> Path:
    return Path(home) / "digest" / f"{date}.md"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="owner_digest.py",
        description="Dzienny digest dla Mateusza (Fala 3, O6). Wszystko lokalnie.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="zbuduj digest")
    build.add_argument("--since-hours", type=float, default=24.0, help="okno w godzinach (domyslnie 24)")
    build.add_argument("--now", default=None, help="znacznik czasu ISO (domyslnie teraz, UTC)")
    build.add_argument("--out", default=None, help="plik wyjsciowy .md (domyslnie <AGENTS_HOME>/digest/<data>.md)")
    build.add_argument("--json", action="store_true", help="wypisz dane strukturalnie (JSON)")

    args = parser.parse_args(argv)
    if args.command != "build":
        parser.error("nieznana podkomenda")
    if args.since_hours <= 0:
        write_stderr("blad: --since-hours musi byc dodatni\n")
        return 2

    now = None
    if args.now:
        now = parse_iso(args.now)
        if now is None:
            write_stderr(f"blad: nieczytelny --now: {args.now}\n")
            return 2

    home = agents_home()
    payload = build_digest(home=home, since_hours=args.since_hours, now=now)
    markdown = render_markdown(payload)

    target = Path(args.out) if args.out else default_output_path(home, payload["date"])
    try:
        write_atomic(target, markdown)
    except OSError as exc:
        write_stderr(f"blad: nie udalo sie zapisac {target} ({exc.strerror or exc})\n")
        return 2

    if args.json:
        # Prosba o JSON: na stdout struktura, na stderr sciezka zapisu.
        write_stdout(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        write_stderr(f"zapisano: {target}\n")
    elif args.out:
        # Plik zapisany - na stdout wypada TYLKO sciezka (nie caly markdown:
        # na konsoli cp1250 dlugi tekst z polskimi znakami wywalal Pythona).
        write_stdout(f"zapisano: {target}\n")
    else:
        write_stdout(markdown)
        write_stderr(f"zapisano: {target}\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
