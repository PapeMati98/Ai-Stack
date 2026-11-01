#!/usr/bin/env python3
"""
owner_decisions.py - Dziennik decyzji wlasciciela (append-only, z lancuchem hashy).

Cel T7: strukturyzowane dane o decyzjach Mateusza jako material pod przyszly
lokalny model decyzyjny. Bez nowej ceremonii, bez sieci, bez modeli w kodzie.

Zasady (wg SPEC.md, sekcja V3):
1. CLI:
     record --project <slug> --question <tekst> --choice <tekst>
            [--options <a|b|c>] [--reason <tekst>]
            [--category business|technical|autonomy|deletion|contact|secrets|priority]
            [--source chat|telegram|office]
            [--decided-by mateusz|director:<model>] [--supersedes <id>]
     list [--project] [--category] [--since]
     export --out <plik.jsonl>
     verify
2. Zapis append-only do ~/.agents/owner-decisions/decisions.jsonl
   (katalog z env OWNER_DECISIONS_HOME dla testow). Rekordy nigdy nie sa
   edytowane ani usuwane; korekta = nowy rekord z polem `supersedes`.
   Kazdy rekord ma `prev` = sha256 poprzedniego rekordu oraz `sha256` = hash
   wlasnego kanonicznego JSON-a (liczonego z `prev`, bez samego pola `sha256`).
3. Rekord zawierajacy wzorce sekretow (klucz API, `op://`, `haslo=`) jest
   odrzucany: SECRET_LIKE_CONTENT.
4. Tylko stdlib Pythona, bez usuwania plikow, bez wywolanego procesu.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1
STORE_FILENAME = "decisions.jsonl"
DEFAULT_HOME = Path.home() / ".agents" / "owner-decisions"

CATEGORIES = (
    "business",
    "technical",
    "autonomy",
    "deletion",
    "contact",
    "secrets",
    "priority",
)
SOURCES = ("chat", "telegram", "office")

# Pola tekstowe, ktore podlegaja kontroli sekretow.
_SECRET_SENSITIVE_FIELDS = ("question", "choice", "reason", "options")

# Wzorce sekretow. Celowo szerokie: lepszy falszywy alarm niz zapis sekretu.
SECRET_PATTERNS = (
    re.compile(r"op://", re.IGNORECASE),
    re.compile(r"\b(?:haslo|hasło|password|passwd|pwd|secret|token|api[_ -]?key|apikey|access[_ -]?key|private[_ -]?key|klucz(?:[_ -]?api)?|credential)\s*[=:]\s*\S+", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{12,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\."),  # JWT
)


class DecisionError(Exception):
    """Blad biznesowy dziennika decyzji (ma kod i komunikat dla CLI)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict[str, Any]:
        return {"ok": False, "error": self.code, "message": self.message}


def get_home() -> Path:
    """Katalog dziennika: env OWNER_DECISIONS_HOME albo ~/.agents/owner-decisions."""
    env_home = os.environ.get("OWNER_DECISIONS_HOME")
    if env_home:
        return Path(env_home)
    return DEFAULT_HOME


def get_store_path() -> Path:
    return get_home() / STORE_FILENAME


def canonical_json(payload: dict[str, Any]) -> str:
    """Kanoniczna postać JSON-a liczona w lancuchu hashy (sortowanie kluczy)."""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest_record(payload: dict[str, Any]) -> str:
    """sha256 rekordu bez pola `sha256` (hash wlasny)."""
    body = {k: v for k, v in payload.items() if k != "sha256"}
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def find_secret_like(text: str) -> str | None:
    """Zwraca nazwe dopasowanego wzorca albo None."""
    for pattern in SECRET_PATTERNS:
        if pattern.search(text or ""):
            return pattern.pattern
    return None


def check_secret_like(fields: dict[str, Any]) -> None:
    """SECRET_LIKE_CONTENT gdy ktorekolwiek pole tekstowe wyglada na sekret."""
    for name in _SECRET_SENSITIVE_FIELDS:
        value = fields.get(name)
        if not isinstance(value, str) or not value:
            continue
        hit = find_secret_like(value)
        if hit:
            raise DecisionError(
                "SECRET_LIKE_CONTENT",
                f"Pole '{name}' zawiera wzorzec sekretu ({hit}). "
                "Sekretow nie zapisujemy w dzienniku decyzji.",
            )


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: str) -> datetime:
    """Akceptuje `2026-10-21`, `2026-10-21T10:00:00Z` i `...+02:00`."""
    text = (value or "").strip()
    if not text:
        raise DecisionError("BAD_TIMESTAMP", "Pusty znacznik czasu.")
    if len(text) == 10:
        text += "T00:00:00+00:00"
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise DecisionError("BAD_TIMESTAMP", f"Nieczytelny czas: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def read_entries(path: Path | None = None) -> list[dict[str, Any]]:
    """Wczytuje rekordy w kolejnosci zapisu. Brak pliku = pusta lista."""
    target = path or get_store_path()
    if not target.exists():
        return []
    entries: list[dict[str, Any]] = []
    with target.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise DecisionError(
                    "CORRUPT_STORE", f"Linia {line_no} w {target} nie jest poprawnym JSON-em."
                ) from exc
    return entries


def verify_chain(entries: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Sprawdza lancuch: spojnosc `prev` i poprawnosc hashy wlasnych."""
    previous_hash: str | None = None
    problems: list[dict[str, Any]] = []
    checked = 0
    for index, entry in enumerate(entries):
        checked += 1
        expected_prev = entry.get("prev")
        if expected_prev != previous_hash:
            problems.append(
                {
                    "index": index,
                    "id": entry.get("id"),
                    "code": "PREV_MISMATCH",
                    "detail": f"prev={expected_prev!r}, oczekiwano {previous_hash!r}",
                }
            )
        stored = entry.get("sha256")
        computed = digest_record(entry)
        if stored != computed:
            problems.append(
                {
                    "index": index,
                    "id": entry.get("id"),
                    "code": "SHA_MISMATCH",
                    "detail": f"sha256={stored!r}, wyliczono {computed}",
                }
            )
        previous_hash = stored if stored is not None else computed
    return {"ok": not problems, "checked": checked, "problems": problems}


def _validate_choice(value: str, name: str, allowed: tuple[str, ...]) -> str:
    text = (value or "").strip()
    if text not in allowed:
        raise DecisionError("BAD_VALUE", f"{name} musi naleziec do: {', '.join(allowed)}")
    return text


def build_record(
    *,
    project: str,
    question: str,
    choice: str,
    options: str | None = None,
    reason: str | None = None,
    category: str | None = None,
    source: str | None = None,
    decided_by: str = "mateusz",
    supersedes: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Buduje (ale nie zapisuje) rekord decyzji."""
    if not (project or "").strip():
        raise DecisionError("BAD_VALUE", "Pole --project jest wymagane.")
    if not (question or "").strip():
        raise DecisionError("BAD_VALUE", "Pole --question jest wymagane.")
    if not (choice or "").strip():
        raise DecisionError("BAD_VALUE", "Pole --choice jest wymagane.")

    if category is not None:
        category = _validate_choice(category, "--category", CATEGORIES)
    if source is not None:
        source = _validate_choice(source, "--source", SOURCES)

    who = (decided_by or "").strip()
    if not who:
        raise DecisionError("BAD_VALUE", "Pole --decided-by jest wymagane.")
    if who != "mateusz" and not who.startswith("director:"):
        raise DecisionError("BAD_VALUE", "--decided-by: 'mateusz' albo 'director:<model>'.")
    if who.startswith("director:") and not who.split(":", 1)[1].strip():
        raise DecisionError("BAD_VALUE", "--decided-by director: wymaga nazwy modelu.")

    fields = {
        "question": question.strip(),
        "choice": choice.strip(),
        "options": (options.strip() if options else None),
        "reason": (reason.strip() if reason else None),
    }
    check_secret_like(fields)

    record: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "id": str(uuid.uuid4()),
        "ts": now or utc_now(),
        "project": project.strip(),
        "question": fields["question"],
        "choice": fields["choice"],
        "options": fields["options"],
        "reason": fields["reason"],
        "category": category,
        "source": source,
        "decided_by": who,
        "supersedes": (supersedes.strip() if supersedes else None),
    }
    return record


def append_record(record: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    """Dopisuje rekord do dziennika i zwraca rekord z polami `prev` i `sha256`."""
    target = path or get_store_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    existing = read_entries(target)
    check_secret_like(
        {
            "question": record.get("question"),
            "choice": record.get("choice"),
            "reason": record.get("reason"),
            "options": record.get("options"),
        }
    )
    if record.get("supersedes"):
        known = {entry.get("id") for entry in existing}
        if record["supersedes"] not in known:
            raise DecisionError(
                "UNKNOWN_SUPERSEDES",
                f"Rekord {record['supersedes']} nie istnieje w dzienniku.",
            )

    record = dict(record)
    record["prev"] = existing[-1].get("sha256") if existing else None
    record["sha256"] = digest_record(record)

    line = canonical_json(record) + "\n"
    with target.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    return record


def record_decision(
    *,
    project: str,
    question: str,
    choice: str,
    options: str | None = None,
    reason: str | None = None,
    category: str | None = None,
    source: str | None = None,
    decided_by: str = "mateusz",
    supersedes: str | None = None,
    now: str | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    return append_record(
        build_record(
            project=project,
            question=question,
            choice=choice,
            options=options,
            reason=reason,
            category=category,
            source=source,
            decided_by=decided_by,
            supersedes=supersedes,
            now=now,
        ),
        path=path,
    )


def filter_entries(
    entries: Iterable[dict[str, Any]],
    *,
    project: str | None = None,
    category: str | None = None,
    since: str | None = None,
    superseded_only: bool = False,
) -> list[dict[str, Any]]:
    """Filtruje rekordy: projekt, kategoria, czas od `--since`, tylko aktualne."""
    since_dt = parse_ts(since) if since else None
    selected: list[dict[str, Any]] = []
    for entry in entries:
        if project and entry.get("project") != project:
            continue
        if category and entry.get("category") != category:
            continue
        if since_dt is not None:
            try:
                if parse_ts(str(entry.get("ts"))) < since_dt:
                    continue
            except DecisionError:
                continue
        selected.append(entry)
    if superseded_only:
        replaced = {e.get("supersedes") for e in selected if e.get("supersedes")}
        selected = [e for e in selected if e.get("id") not in replaced]
    return selected


def export_entries(entries: Iterable[dict[str, Any]], out_path: Path) -> int:
    """Eksport do JSONL. Zapis poza dziennikiem, bez ruszania istniejacych plikow."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with out_path.open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(canonical_json(entry) + "\n")
            count += 1
    return count


def _emit(payload: dict[str, Any], as_json: bool = True) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="owner_decisions.py",
        description="Dziennik decyzji wlasciciela (append-only, lancuch sha256).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    rec = sub.add_parser("record", help="dopisz decyzje (append-only)")
    rec.add_argument("--project", required=True)
    rec.add_argument("--question", required=True)
    rec.add_argument("--choice", required=True)
    rec.add_argument("--options", default=None)
    rec.add_argument("--reason", default=None)
    rec.add_argument("--category", default=None, choices=list(CATEGORIES))
    rec.add_argument("--source", default=None, choices=list(SOURCES))
    rec.add_argument("--decided-by", default="mateusz")
    rec.add_argument("--supersedes", default=None)

    lst = sub.add_parser("list", help="wyswietl rekordy")
    lst.add_argument("--project", default=None)
    lst.add_argument("--category", default=None, choices=list(CATEGORIES))
    lst.add_argument("--since", default=None)

    exp = sub.add_parser("export", help="eksport do JSONL")
    exp.add_argument("--out", required=True)
    exp.add_argument("--project", default=None)
    exp.add_argument("--category", default=None, choices=list(CATEGORIES))
    exp.add_argument("--since", default=None)

    ver = sub.add_parser("verify", help="sprawdz integralnosc lancucha")

    args = parser.parse_args(argv)
    try:
        if args.command == "record":
            entry = record_decision(
                project=args.project,
                question=args.question,
                choice=args.choice,
                options=args.options,
                reason=args.reason,
                category=args.category,
                source=args.source,
                decided_by=args.decided_by,
                supersedes=args.supersedes,
            )
            _emit({"ok": True, "id": entry["id"], "sha256": entry["sha256"]})
        elif args.command == "list":
            entries = filter_entries(
                read_entries(), project=args.project, category=args.category, since=args.since
            )
            _emit({"ok": True, "count": len(entries), "entries": entries})
        elif args.command == "export":
            entries = filter_entries(
                read_entries(), project=args.project, category=args.category, since=args.since
            )
            count = export_entries(entries, Path(args.out))
            _emit({"ok": True, "exported": count, "out": args.out})
        elif args.command == "verify":
            result = verify_chain(read_entries())
            _emit(result)
            return 0 if result["ok"] else 1
    except DecisionError as exc:
        _emit(exc.to_dict())
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())