#!/usr/bin/env python3
"""Local, read-only lifecycle security audit with a 14-day freshness gate."""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


UTC = dt.timezone.utc
DEFAULT_MAX_AGE_DAYS = 14
DEFAULT_ACTIVE_WINDOW_DAYS = 7
MAX_FILES = 20000
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_FINDINGS_PER_RULE = 100

EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".vscode",
    "node_modules",
    "vendor",
    "venv",
    ".venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".next",
    ".nuxt",
    "dist",
    "build",
    "out",
    "coverage",
    "cache",
    ".cache",
    "models",
    "datasets",
    "data",
    "logs",
    "log",
    "uploads",
    "backups",
    "backup",
    "tmp",
    "temp",
}

PYTHON_VENDOR_DIRS = {
    "site-packages",
    "dist-packages",
}

SENSITIVE_GLOBS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.pfx",
    "*.p12",
    "id_rsa*",
    "id_ed25519*",
    "*credential*",
    "*secret*",
    "*token*",
    ".jwt_secret",
    "*.db",
    "*.sqlite",
    "*.sqlite3",
    "*.log",
    "*cookie*",
    "storage_state*.json",
)

SOURCE_EXTENSIONS = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
    ".java",
    ".cs",
    ".go",
    ".rs",
    ".php",
    ".rb",
    ".ps1",
    ".sh",
    ".yaml",
    ".yml",
    ".json",
    ".toml",
    ".ini",
    ".conf",
    ".xml",
    ".html",
    ".vue",
    ".svelte",
    ".sql",
}

SPECIAL_SOURCE_NAMES = {
    "dockerfile",
    "containerfile",
    "nginx.conf",
    "requirements.txt",
    "pyproject.toml",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "composer.json",
    "cargo.toml",
    "go.mod",
}

MANIFEST_NAMES = {
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "requirements.txt",
    "pyproject.toml",
    "poetry.lock",
    "composer.json",
    "composer.lock",
    "cargo.toml",
    "cargo.lock",
    "go.mod",
    "go.sum",
}

RULES = (
    {
        "id": "SEC001",
        "severity": "critical",
        "pattern": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
        "summary": "Prywatny klucz zapisany w pliku zrodlowym.",
        "recommendation": "Usun klucz z historii, uniewaznij go i uzyj secret store.",
    },
    {
        "id": "SEC002",
        "severity": "critical",
        "pattern": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        "summary": "Identyfikator klucza AWS zapisany w kodzie.",
        "recommendation": "Uniewaznij klucz i przenies sekret do bezpiecznego magazynu.",
    },
    {
        "id": "SEC003",
        "severity": "high",
        "pattern": re.compile(
            r"""(?ix)\b(api[_-]?key|client[_-]?secret|access[_-]?token|password)\b
                \s*[:=]\s*["'][^"'${}\s]{8,}["']"""
        ),
        "summary": "Prawdopodobny sekret zakodowany na stale.",
        "recommendation": "Zweryfikuj bez ujawniania wartosci i przenies do secret store.",
    },
    {
        "id": "SEC010",
        "severity": "high",
        "pattern": re.compile(r"(?i)\b(?:subprocess\.(?:run|Popen|call)|check_output)\s*\([^)]*shell\s*=\s*True"),
        "summary": "Uruchamianie polecenia przez powloke z shell=True.",
        "recommendation": "Uzyj listy argumentow i allowlisty danych wejsciowych.",
    },
    {
        "id": "SEC011",
        "severity": "high",
        "pattern": re.compile(r"(?i)\bos\.system\s*\("),
        "summary": "Bezposrednie wykonanie polecenia przez os.system.",
        "recommendation": "Zastap bezpiecznym wywolaniem bez powloki i waliduj argumenty.",
    },
    {
        "id": "SEC012",
        "severity": "high",
        "pattern": re.compile(r"(?i)\b(?:pickle|dill)\.loads?\s*\("),
        "summary": "Deserializacja formatu pozwalajacego wykonac kod.",
        "recommendation": "Nie deserializuj nieufnych danych; wybierz format bez wykonywania kodu.",
    },
    {
        "id": "SEC013",
        "severity": "high",
        "pattern": re.compile(r"(?i)verify_signature['\"]?\s*[:=]\s*False"),
        "summary": "Wylaczona weryfikacja podpisu tokenu.",
        "recommendation": "Wymus weryfikacje podpisu, issuer, audience i czasu waznosci.",
    },
    {
        "id": "SEC014",
        "severity": "high",
        "pattern": re.compile(r"(?i)NODE_TLS_REJECT_UNAUTHORIZED\s*=\s*['\"]?0"),
        "summary": "Globalnie wylaczona weryfikacja TLS.",
        "recommendation": "Przywroc weryfikacje certyfikatow i popraw zaufany lancuch CA.",
    },
    {
        "id": "SEC020",
        "severity": "medium",
        "pattern": re.compile(r"(?i)\b(?:eval|exec)\s*\("),
        "summary": "Dynamiczne wykonanie kodu wymaga recznej kontroli zrodla danych.",
        "recommendation": "Usun dynamiczne wykonanie albo zastosuj scisla allowliste.",
    },
    {
        "id": "SEC021",
        "severity": "medium",
        "pattern": re.compile(r"(?i)\byaml\.load\s*\([^)]*(?!SafeLoader)"),
        "summary": "Potencjalnie niebezpieczne ladowanie YAML.",
        "recommendation": "Uzyj safe_load albo jawnego SafeLoader.",
    },
    {
        "id": "SEC022",
        "severity": "medium",
        "pattern": re.compile(r"(?i)\bverify\s*=\s*False\b"),
        "summary": "Wylaczona weryfikacja certyfikatu TLS dla zadania HTTP.",
        "recommendation": "Przywroc weryfikacje TLS; wyjatek dokumentuj i ogranicz.",
    },
    {
        "id": "SEC023",
        "severity": "medium",
        "pattern": re.compile(
            r"""(?ix)(allow_origins\s*=\s*\[\s*["']\*["']\s*\]|
                     access-control-allow-origin\s*[:=]\s*["']?\*)"""
        ),
        "summary": "CORS dopuszcza dowolne origin.",
        "recommendation": "Ogranicz origin do jawnej listy zaufanych domen.",
    },
    {
        "id": "SEC024",
        "severity": "medium",
        "pattern": re.compile(r"(?i)\b(?:DEBUG|debug)\s*[:=]\s*True\b"),
        "summary": "Tryb debug moze byc aktywny.",
        "recommendation": "Wymus debug=false w konfiguracji produkcyjnej.",
    },
    {
        "id": "SEC025",
        "severity": "medium",
        "pattern": re.compile(r"(?i)\.innerHTML\s*="),
        "summary": "Bezposrednie ustawienie innerHTML wymaga kontroli XSS.",
        "recommendation": "Uzyj bezpiecznego renderowania albo sanitizacji z allowlista.",
    },
    {
        "id": "SEC030",
        "severity": "low",
        "pattern": re.compile(r"(?<![0-9])0\.0\.0\.0(?::\d+)?"),
        "summary": "Usluga moze nasluchiwac na wszystkich interfejsach.",
        "recommendation": "Potwierdz, ze ekspozycja jest zamierzona i chroniona firewallem/auth.",
    },
)


def utc_now() -> dt.datetime:
    return dt.datetime.now(UTC)


def iso_z(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def audit_root() -> Path:
    canonical = (Path.home() / ".agents" / "security-audits").resolve()
    configured = os.environ.get("SECURITY_AUDIT_ROOT")
    if not configured:
        return canonical
    if os.environ.get("SECURITY_AUDIT_TEST_MODE") != "1":
        raise ValueError(
            "SECURITY_AUDIT_ROOT is accepted only in explicit test mode"
        )
    candidate = Path(configured).expanduser().resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    try:
        candidate.relative_to(temp_root)
    except ValueError as exc:
        raise ValueError(
            "test audit root must stay under the system temp directory"
        ) from exc
    return candidate


def normalize_project(raw: str) -> Path:
    project = Path(raw).expanduser().resolve()
    if not project.exists() or not project.is_dir():
        raise ValueError(f"Project directory does not exist: {project}")
    return project


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-")
    return slug[:48] or "project"


def project_identity(project: Path) -> tuple[str, Path]:
    normalized = os.path.normcase(str(project))
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    project_id = f"{slugify(project.name)}-{digest}"
    return project_id, audit_root() / project_id


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


class ProjectLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = None

    def __enter__(self) -> "ProjectLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise RuntimeError(f"Audit already running or lock not cleared: {self.path}") from exc
        os.write(
            self.fd,
            json.dumps({"pid": os.getpid(), "started_at": iso_z(utc_now())}).encode("utf-8"),
        )
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.fd is not None:
            os.close(self.fd)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def is_sensitive(path: Path) -> bool:
    name = path.name.lower()
    return any(fnmatch.fnmatch(name, pattern.lower()) for pattern in SENSITIVE_GLOBS)


def is_source(path: Path) -> bool:
    return path.suffix.lower() in SOURCE_EXTENSIONS or path.name.lower() in SPECIAL_SOURCE_NAMES


def is_excluded_dir(name: str) -> bool:
    lowered = name.lower()
    if lowered in EXCLUDED_DIRS or lowered in PYTHON_VENDOR_DIRS:
        return True
    return (
        lowered.startswith(".venv")
        or lowered.startswith("venv-")
        or lowered.endswith("-venv")
        or lowered.endswith("_venv")
    )


def iter_files(project: Path) -> Iterable[Path]:
    emitted = 0
    for root, dirs, files in os.walk(project, followlinks=False):
        dirs[:] = [
            name
            for name in dirs
            if not is_excluded_dir(name)
            and not (Path(root) / name).is_symlink()
        ]
        for name in files:
            path = Path(root) / name
            if path.is_symlink() or is_sensitive(path) or not is_source(path):
                continue
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            yield path
            emitted += 1
            if emitted >= MAX_FILES:
                return


def git_summary(project: Path) -> dict[str, Any]:
    def run_git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(project), *args],
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
            encoding="utf-8",
            errors="replace",
        )

    inside = run_git("rev-parse", "--is-inside-work-tree")
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return {"is_git": False, "dirty": None, "changed_entries": None, "head": None}
    status = run_git("status", "--porcelain")
    head = run_git("rev-parse", "--short=12", "HEAD")
    entries = [line for line in status.stdout.splitlines() if line.strip()]
    return {
        "is_git": True,
        "dirty": bool(entries),
        "changed_entries": len(entries),
        "head": head.stdout.strip() if head.returncode == 0 else None,
    }


def project_activity(project: Path, active_window_days: int) -> dict[str, Any]:
    git = git_summary(project)
    if git["dirty"]:
        return {"active": True, "reason": "dirty_git_worktree", "latest_source_mtime": None, "git": git}
    cutoff = time.time() - active_window_days * 86400
    latest = 0.0
    for path in iter_files(project):
        try:
            latest = max(latest, path.stat().st_mtime)
        except OSError:
            continue
    active = latest >= cutoff
    return {
        "active": active,
        "reason": "recent_source_change" if active else "no_recent_source_change",
        "latest_source_mtime": (
            iso_z(dt.datetime.fromtimestamp(latest, UTC)) if latest else None
        ),
        "git": git,
    }


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def status_payload(project: Path, max_age_days: int, active_window_days: int) -> dict[str, Any]:
    project_id, project_dir = project_identity(project)
    activity = project_activity(project, active_window_days)
    latest_path = project_dir / "latest.json"
    latest = load_json(latest_path) if latest_path.exists() else None
    now = utc_now()
    age_days: float | None = None
    if latest and latest.get("completed_at"):
        age_days = (now - parse_time(latest["completed_at"])).total_seconds() / 86400

    if not activity["active"]:
        state, exit_code = "NOT_ACTIVE", 0
    elif latest is None:
        state, exit_code = "AUDIT_REQUIRED", 3
    elif age_days is None or age_days > max_age_days:
        state, exit_code = "AUDIT_REQUIRED", 3
    else:
        state, exit_code = "AUDIT_FRESH", 0

    escalation_required = bool(latest is None or (age_days is not None and age_days > max_age_days))
    return {
        "schema_version": 1,
        "state": state,
        "exit_code": exit_code,
        "project": str(project),
        "project_id": project_id,
        "audit_root": str(project_dir),
        "active": activity["active"],
        "activity_reason": activity["reason"],
        "active_window_days": active_window_days,
        "max_age_days": max_age_days,
        "latest_audit": latest,
        "age_days": round(age_days, 3) if age_days is not None else None,
        "escalation_required": escalation_required,
        "adversarial_triggers": {
            "next_substantive_work": escalation_required,
            "pre_production_release_candidate": True,
        },
        "escalation_source": "security-audit-cycle",
    }


def inventory(project: Path) -> tuple[list[Path], dict[str, Any]]:
    files = list(iter_files(project))
    manifests = sorted(
        {
            path.relative_to(project).as_posix()
            for path in files
            if path.name.lower() in MANIFEST_NAMES
        }
    )
    extension_counts = Counter(path.suffix.lower() or path.name.lower() for path in files)
    return files, {
        "scanned_files": len(files),
        "file_limit": MAX_FILES,
        "truncated_by_file_limit": len(files) >= MAX_FILES,
        "extensions": dict(extension_counts.most_common()),
        "manifests": manifests,
        "git": git_summary(project),
        "sensitive_content_policy": "Sensitive file patterns were excluded and never read.",
    }


def scan_project(project: Path) -> dict[str, Any]:
    files, inv = inventory(project)
    findings: list[dict[str, Any]] = []
    per_rule = Counter()
    read_errors = 0
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            read_errors += 1
            continue
        relative = path.relative_to(project).as_posix()
        for line_no, line in enumerate(text.splitlines(), 1):
            for rule in RULES:
                if per_rule[rule["id"]] >= MAX_FINDINGS_PER_RULE:
                    continue
                if rule["pattern"].search(line):
                    findings.append(
                        {
                            "id": f"{rule['id']}-{per_rule[rule['id']] + 1:03d}",
                            "rule_id": rule["id"],
                            "severity": rule["severity"],
                            "path": relative,
                            "line": line_no,
                            "summary": rule["summary"],
                            "recommendation": rule["recommendation"],
                            "evidence": "Pattern matched; matched value intentionally omitted.",
                            "confidence": "medium",
                            "review_status": "requires_manual_review",
                        }
                    )
                    per_rule[rule["id"]] += 1
    counts = Counter(item["severity"] for item in findings)
    return {
        "schema_version": 1,
        "inventory": inv,
        "read_errors": read_errors,
        "findings": findings,
        "counts": {
            "critical": counts["critical"],
            "high": counts["high"],
            "medium": counts["medium"],
            "low": counts["low"],
            "total": len(findings),
        },
    }


def legacy_codex_security_status() -> dict[str, Any]:
    """Record the tombstone without executing the deprecated wrapper."""

    return {
        "status": "deprecated",
        "executed": False,
        "cloud_upload": False,
        "replaced_by": "simulate-attack",
    }


def verdict_for(counts: dict[str, int]) -> str:
    if counts["critical"]:
        return "NO-GO"
    if counts["high"] or counts["medium"]:
        return "REVISE"
    return "PASS"


def markdown_escape(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def report_text(metadata: dict[str, Any], baseline: dict[str, Any]) -> str:
    counts = baseline["counts"]
    findings = baseline["findings"]
    lines = [
        "# Raport cyklicznego audytu bezpieczenstwa",
        "",
        f"- Audit ID: `{metadata['audit_id']}`",
        f"- Projekt: `{metadata['project']}`",
        f"- Agent: `{metadata['agent']}`",
        f"- Klasyfikacja: `{metadata['classification']}`",
        f"- Zakres: {metadata['scope']}",
        f"- Rozpoczecie: `{metadata['started_at']}`",
        f"- Zakonczenie: `{metadata['completed_at']}`",
        f"- Nastepny audyt najpozniej: `{metadata['next_due_at']}`",
        "- Kod opuscil komputer: **NIE**",
        "- Tryb: lokalny, read-only",
        "",
        "## Werdykt",
        "",
        f"**{metadata['verdict']}**",
        "",
        (
            f"Findings: critical={counts['critical']}, high={counts['high']}, "
            f"medium={counts['medium']}, low={counts['low']}, total={counts['total']}."
        ),
        "",
        "## Zakres i wykluczenia",
        "",
        "Przeskanowano kod oraz jawne pliki konfiguracji. Celowo nie odczytano "
        "plikow `.env`, kluczy, tokenow, baz danych, logow, uploadow, backupow, "
        "modeli, cache ani danych klientow.",
        "",
        "Nie wykonano instalacji, migracji, deployu, restartu ani skanu chmurowego.",
        "",
        "## Inwentaryzacja",
        "",
        f"- Pliki przeskanowane: {baseline['inventory']['scanned_files']}",
        f"- Bledy odczytu: {baseline['read_errors']}",
        f"- Git: `{json.dumps(baseline['inventory']['git'], ensure_ascii=False)}`",
        "- Manifesty:",
    ]
    manifests = baseline["inventory"]["manifests"]
    lines.extend([f"  - `{item}`" for item in manifests] or ["  - brak wykrytych"])
    lines.extend(["", "## Findings", ""])
    if findings:
        lines.extend(
            [
                "| ID | Severity | Lokalizacja | Ustalenie | Rekomendacja | Status |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
        )
        for finding in findings:
            location = f"{finding['path']}:{finding['line']}"
            lines.append(
                "| {id} | {severity} | `{location}` | {summary} | {recommendation} | {status} |".format(
                    id=markdown_escape(finding["id"]),
                    severity=markdown_escape(finding["severity"]),
                    location=markdown_escape(location),
                    summary=markdown_escape(finding["summary"]),
                    recommendation=markdown_escape(finding["recommendation"]),
                    status=markdown_escape(finding["review_status"]),
                )
            )
    else:
        lines.append("Brak automatycznych findingow w zbadanym zakresie.")
    lines.extend(
        [
            "",
            "## Reczna weryfikacja",
            "",
            "Wszystkie dopasowania automatu wymagaja oceny kontekstu. Ten przebieg "
            "nie zmienial statusow false positive; pozostaja `requires_manual_review`.",
            "",
            "## Ograniczenia i ryzyko rezydualne",
            "",
            "Baseline nie dowodzi poprawnosci RBAC/IDOR, izolacji tenantow, konfiguracji "
            "uruchomionej produkcji, aktualnych CVE zaleznosci ani infrastruktury poza "
            "katalogiem. PASS nie jest gwarancja bezpieczenstwa.",
            "",
            "## Dowody",
            "",
            "- `metadata.json` - tozsamosc i status przebiegu.",
            "- `baseline.json` - ustrukturyzowane wyniki bez wartosci sekretow.",
            "- `baseline.json` - lokalny baseline; deprecated Codex Security CLI nie jest uruchamiany.",
            "",
        ]
    )
    return "\n".join(lines)


def cmd_status(args: argparse.Namespace) -> int:
    project = normalize_project(args.project)
    payload = status_payload(project, args.max_age_days, args.active_window_days)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return payload["exit_code"]


def cmd_run_local(args: argparse.Namespace) -> int:
    project = normalize_project(args.project)
    gate = status_payload(project, args.max_age_days, args.active_window_days)
    if gate["state"] == "AUDIT_FRESH" and not args.force:
        print(json.dumps({"skipped": True, "reason": "AUDIT_FRESH", **gate}, ensure_ascii=False, indent=2))
        return 0

    project_id, project_dir = project_identity(project)
    project_dir.mkdir(parents=True, exist_ok=True)
    with ProjectLock(project_dir / ".audit.lock"):
        started = utc_now()
        safe_agent = slugify(args.agent)
        audit_id = started.strftime("%Y%m%dT%H%M%S%fZ") + f"__{safe_agent}"
        audit_dir = project_dir / "audits" / audit_id
        audit_dir.mkdir(parents=True, exist_ok=False)
        metadata_path = audit_dir / "metadata.json"
        metadata: dict[str, Any] = {
            "schema_version": 1,
            "audit_id": audit_id,
            "project_id": project_id,
            "project": str(project),
            "agent": args.agent,
            "classification": args.classification,
            "scope": args.scope,
            "status": "running",
            "started_at": iso_z(started),
            "completed_at": None,
            "next_due_at": None,
            "verdict": None,
            "cloud_upload": False,
            "project_modified": False,
            "reviewers": [],
        }
        atomic_json(metadata_path, metadata)
        atomic_json(
            project_dir / "project.json",
            {
                "schema_version": 1,
                "project_id": project_id,
                "project": str(project),
                "created_or_seen_at": iso_z(started),
            },
        )
        try:
            baseline = scan_project(project)
            baseline["legacy_codex_security_cli"] = legacy_codex_security_status()
            atomic_json(audit_dir / "baseline.json", baseline)
            completed = utc_now()
            metadata.update(
                {
                    "status": "completed",
                    "completed_at": iso_z(completed),
                    "next_due_at": iso_z(completed + dt.timedelta(days=args.max_age_days)),
                    "verdict": verdict_for(baseline["counts"]),
                    "finding_counts": baseline["counts"],
                }
            )
            atomic_json(metadata_path, metadata)
            (audit_dir / "REPORT.md").write_text(
                report_text(metadata, baseline), encoding="utf-8"
            )
            latest = {
                "audit_id": audit_id,
                "completed_at": metadata["completed_at"],
                "next_due_at": metadata["next_due_at"],
                "verdict": metadata["verdict"],
                "agent": args.agent,
                "report": str(audit_dir / "REPORT.md"),
                "finding_counts": baseline["counts"],
                "cloud_upload": False,
            }
            atomic_json(project_dir / "latest.json", latest)
            append_jsonl(
                project_dir / "ledger.jsonl",
                {"event": "audit_completed", **latest},
            )
            print(
                json.dumps(
                    {
                        "audit_id": audit_id,
                        "verdict": metadata["verdict"],
                        "finding_counts": baseline["counts"],
                        "audit_dir": str(audit_dir),
                        "report": str(audit_dir / "REPORT.md"),
                        "next_due_at": metadata["next_due_at"],
                        "cloud_upload": False,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        except Exception as exc:
            metadata.update(
                {
                    "status": "failed",
                    "completed_at": iso_z(utc_now()),
                    "error_class": type(exc).__name__,
                    "error": str(exc),
                }
            )
            atomic_json(metadata_path, metadata)
            append_jsonl(
                project_dir / "ledger.jsonl",
                {
                    "event": "audit_failed",
                    "audit_id": audit_id,
                    "completed_at": metadata["completed_at"],
                    "error_class": type(exc).__name__,
                },
            )
            raise


def cmd_attach_review(args: argparse.Namespace) -> int:
    project = normalize_project(args.project)
    _, project_dir = project_identity(project)
    audit_dir = project_dir / "audits" / args.audit_id
    metadata_path = audit_dir / "metadata.json"
    report_path = audit_dir / "REPORT.md"
    source = Path(args.source).expanduser().resolve()
    if not metadata_path.exists() or not report_path.exists():
        raise ValueError(f"Audit not found: {args.audit_id}")
    if not source.exists() or not source.is_file():
        raise ValueError(f"Review file not found: {source}")
    if source.stat().st_size > 5 * 1024 * 1024:
        raise ValueError("Review file exceeds 5 MiB limit.")

    with ProjectLock(project_dir / ".audit.lock"):
        evidence_dir = audit_dir / "evidence"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        target = evidence_dir / f"review-{slugify(args.agent)}.md"
        shutil.copy2(source, target)
        metadata = load_json(metadata_path)
        reviewers = metadata.setdefault("reviewers", [])
        record = {
            "agent": args.agent,
            "attached_at": iso_z(utc_now()),
            "file": target.name,
        }
        reviewers.append(record)
        atomic_json(metadata_path, metadata)
        with report_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(
                "\n## Niezalezny review\n\n"
                f"- Agent: `{args.agent}`\n"
                f"- Dolaczono: `{record['attached_at']}`\n"
                f"- Dowod: `evidence/{target.name}`\n"
                "- Werdykt glownego raportu nie zostal automatycznie zmieniony.\n"
            )
        append_jsonl(
            project_dir / "ledger.jsonl",
            {
                "event": "review_attached",
                "audit_id": args.audit_id,
                **record,
            },
        )
    print(json.dumps({"attached": True, "target": str(target)}, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local lifecycle security audit with dated evidence."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="Check the 14-day audit freshness gate.")
    status.add_argument("--project", required=True)
    status.add_argument("--max-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS)
    status.add_argument(
        "--active-window-days", type=int, default=DEFAULT_ACTIVE_WINDOW_DAYS
    )
    status.set_defaults(func=cmd_status)

    run_local = sub.add_parser("run-local", help="Run a local read-only baseline.")
    run_local.add_argument("--project", required=True)
    run_local.add_argument("--agent", default="codex")
    run_local.add_argument(
        "--classification",
        choices=("public", "internal", "confidential", "restricted"),
        default="internal",
    )
    run_local.add_argument("--scope", default="full project, read-only")
    run_local.add_argument("--max-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS)
    run_local.add_argument(
        "--active-window-days", type=int, default=DEFAULT_ACTIVE_WINDOW_DAYS
    )
    run_local.add_argument("--force", action="store_true")
    run_local.set_defaults(func=cmd_run_local)

    attach = sub.add_parser("attach-review", help="Attach an external read-only review.")
    attach.add_argument("--project", required=True)
    attach.add_argument("--audit-id", required=True)
    attach.add_argument("--agent", required=True)
    attach.add_argument("--source", required=True)
    attach.set_defaults(func=cmd_attach_review)

    adversarial = sub.add_parser(
        "adversarial-status",
        help="Read the freshness gate escalation state without modifying the ledger.",
    )
    adversarial.add_argument("--project", required=True)
    adversarial.add_argument("--max-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS)
    adversarial.add_argument(
        "--active-window-days", type=int, default=DEFAULT_ACTIVE_WINDOW_DAYS
    )
    adversarial.add_argument(
        "--trigger",
        choices=("next_substantive_work", "pre_production_release_candidate"),
        default="next_substantive_work",
    )
    adversarial.set_defaults(func=cmd_adversarial_status)
    return parser


def cmd_adversarial_status(args: argparse.Namespace) -> int:
    project = normalize_project(args.project)
    payload = status_payload(project, args.max_age_days, args.active_window_days)
    trigger = args.trigger
    triggers = payload.get("adversarial_triggers", {})
    required = bool(triggers.get(trigger))
    print(
        json.dumps(
            {
                "schema_version": 1,
                "escalation_required": required,
                "trigger": trigger,
                "trigger_source": payload.get("escalation_source"),
                "gate_state": payload["state"],
                "age_days": payload["age_days"],
                "adversarial_triggers": triggers,
                "audit_root": payload["audit_root"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if required else 4


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(
            json.dumps(
                {"error": type(exc).__name__, "message": str(exc)},
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
