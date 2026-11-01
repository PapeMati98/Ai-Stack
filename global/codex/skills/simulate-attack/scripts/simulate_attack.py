#!/usr/bin/env python3
"""Deterministic, fail-closed bounded adversarial audit helper.

Implements the read-only reconnaissance, bounded iterative Fleet-driven
entry-attempt loop and redacted remediation report described in the
``simulate-attack`` skill. Reuses the existing
``security-audit-cycle`` 14-day ledger through a thin import of
``security_audit.status_payload`` and never creates a second freshness
counter or probe.

Architecture:

* Every reasoning iteration invokes the real currently installed Agent
  Fleet runtime (package >= 0.8.32) through ``fleet_runner.py`` with the
  confined local-Qwen red-team profile (``qwen-local``) and the exact
  observed model ``huihui_ai/Qwen3.8-abliterated``.
* The Fleet runner returns a strict structured probe selected from a
  small named allowlist. A deterministic executor validates it. No
  arbitrary command, shell, Python expression or generic URL fetch is
  accepted.
* Fresh-token consumption is read from the real Fleet run telemetry
  (``token_usage.fresh_input_tokens``). Cache-read is reported as a
  separate field. The loop stops before the next iteration if the
  cumulative fresh-token ceiling would be exceeded.
* Active probes are allowed only on loopback targets (``localhost``,
  ``127.0.0.0/8``, ``::1``) or on an explicitly identified isolated
  synthetic target. RFC1918 LAN addresses are not loopback and are
  rejected. For the bundled fixture, the probe name is
  ``synthetic_harness_probe`` and the outcome is never reported as a
  successful real-world exploit.
* Reports persist only bounded finding metadata: identifiers, severity,
  location, hashes, bounded summaries, run IDs and token counters. No
  source snippets, secrets or attack payload bodies are persisted.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping


UTC = dt.timezone.utc

ALLOWED_TARGET_CLASSES = ("TEST", "STAGING", "isolated_candidate")
ALLOWED_TRIGGERS = (
    "next_substantive_work",
    "pre_production_release_candidate",
)
ALLOWED_VECTORS = (
    "auth-roles",
    "input-validation",
    "data-exposure",
    "configuration",
    "logging",
    "transport",
)
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
MAX_FILES = 4096
MAX_FILE_BYTES = 256 * 1024
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
TARGET_REGISTRY_NAME = "target-registry.json"
SYNTHETIC_TARGETS_DIR = "synthetic-targets"
REDACTION_PLACEHOLDER = "<redacted:{kind}@{path}>"
REDACTED_MARKER_RE = re.compile(r"<redacted:[^>]+@[^>]+>")
SENSITIVE_FILE_GLOBS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.pfx",
    "*.p12",
    "*credential*",
    "*secret*",
    "*token*",
    "*.db",
    "*.sqlite",
    "*.sqlite3",
    "*.log",
)

# Numeric audit IDs, ISO dates and hash fragments are common in reports. Treat
# digits as a phone number only when they carry an international ``+`` prefix
# or an explicit phone label; bare digit runs are too ambiguous to redact.
PHONE_NUMBER_RE = re.compile(
    r"(?ix)(?:"
    r"\+(?:\d[\s().-]?){6,14}\d\b"
    r"|\b(?:phone|mobile|tel(?:ephone)?|telefon)\s*[:=]\s*"
    r"\+?(?:\d[\s().-]?){6,14}\d\b"
    r")"
)

REDACT_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("openai-key", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}\b")),
    (
        "github-token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b"),
    ),
    ("huggingface-token", re.compile(r"\bhf_[A-Za-z0-9]{20,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    (
        "private-key",
        re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"),
    ),
    (
        "credential-uri",
        re.compile(r"\b[a-z][a-z0-9+.-]{2,12}://[^/\s:@]{2,64}:[^/\s@]{8,}@"),
    ),
    (
        "jwt",
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."
            r"[A-Za-z0-9_-]{5,}\b"
        ),
    ),
    (
        "cookie",
        re.compile(r"(?im)\b(?:set-cookie|cookie)\s*:\s*[^\r\n]+"),
    ),
    (
        "session-id",
        re.compile(
            r"(?i)\b(?:session(?:[_-]?id)?|sid)\s*[:=]\s*"
            r"[\"']?[A-Za-z0-9._~+/=-]{8,}[\"']?"
        ),
    ),
    (
        "password",
        re.compile(
            r"(?i)\b(?:password|passwd|pwd)\s*[:=]\s*"
            r"[\"']?[^\s\"';,]{8,}[\"']?"
        ),
    ),
    (
        "generic-api-key",
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret|token|password)\s*[:=]\s*[\"']?[A-Za-z0-9_\\-]{12,}[\"']?"
        ),
    ),
    (
        "email-address",
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+(?:\.[A-Za-z]{2,})?\b"),
    ),
    (
        "phone-number",
        PHONE_NUMBER_RE,
    ),
)


class PolicyError(RuntimeError):
    """Raised when a hard policy is violated; the helper never recovers."""


def utc_now() -> dt.datetime:
    return dt.datetime.now(UTC)


def iso_z(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-")
    return slug[:48] or "project"


def audit_root() -> Path:
    canonical = (Path.home() / ".agents" / "security-audits").resolve()
    configured = os.environ.get("SECURITY_AUDIT_ROOT")
    if not configured:
        return canonical
    if os.environ.get("SECURITY_AUDIT_TEST_MODE") != "1":
        raise PolicyError(
            "PATH_VIOLATION: SECURITY_AUDIT_ROOT is accepted only in explicit test mode"
        )
    candidate = Path(configured).expanduser().resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    try:
        candidate.relative_to(temp_root)
    except ValueError as exc:
        raise PolicyError(
            "PATH_VIOLATION: test audit root must stay under the system temp directory"
        ) from exc
    return candidate


def project_identity(project: Path) -> tuple[str, Path]:
    normalized = os.path.normcase(str(project.resolve()))
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    project_id = f"{slugify(project.name)}-{digest}"
    return project_id, audit_root() / project_id


def normalize_project(raw: str) -> Path:
    unresolved = Path(raw).expanduser().absolute()
    reject_reparse_components(unresolved)
    project = unresolved.resolve()
    if not project.exists() or not project.is_dir():
        raise ValueError(f"Project directory does not exist: {project}")
    return project


def is_reparse_point(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def reject_reparse_components(path: Path) -> None:
    """Fail closed when a target path traverses a symlink or Windows reparse point."""

    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if current.exists() and is_reparse_point(current):
            raise PolicyError(
                f"TARGET_UNTRUSTED: reparse or symlink path component: {current}"
            )


def _normalized_path(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def load_target_registry() -> list[dict[str, Any]]:
    path = audit_root() / TARGET_REGISTRY_NAME
    if not path.is_file():
        raise PolicyError(f"TARGET_UNTRUSTED: target registry is missing: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PolicyError(f"TARGET_UNTRUSTED: target registry is unreadable: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise PolicyError("TARGET_UNTRUSTED: target registry schema_version must be 1")
    targets = payload.get("targets")
    if not isinstance(targets, list):
        raise PolicyError("TARGET_UNTRUSTED: target registry targets must be a list")
    return [item for item in targets if isinstance(item, dict)]


def attest_target(project: Path, target_class: str) -> dict[str, Any]:
    """Bind target class to local filesystem evidence, never to a CLI assertion alone."""

    reject_reparse_components(project)
    if target_class == "TEST":
        synthetic_root = (audit_root() / SYNTHETIC_TARGETS_DIR).resolve()
        try:
            project.relative_to(synthetic_root)
        except ValueError as exc:
            raise PolicyError(
                "TARGET_UNTRUSTED: TEST targets must reside under "
                f"{synthetic_root}"
            ) from exc
        return {
            "source": "synthetic-target-root",
            "target_class": target_class,
            "path_sha256": hashlib.sha256(
                _normalized_path(project).encode("utf-8")
            ).hexdigest(),
        }

    matches = [
        item
        for item in load_target_registry()
        if _normalized_path(Path(str(item.get("path") or "")).expanduser())
        == _normalized_path(project)
        and item.get("target_class") == target_class
    ]
    if len(matches) != 1:
        raise PolicyError(
            "TARGET_UNTRUSTED: STAGING/isolated_candidate requires exactly one "
            "matching local registry attestation"
        )
    entry = matches[0]
    if entry.get("synthetic_data") is not True or entry.get("active_attack_allowed") is not True:
        raise PolicyError(
            "TARGET_UNTRUSTED: registry entry must attest synthetic_data=true "
            "and active_attack_allowed=true"
        )
    return {
        "source": "local-target-registry",
        "target_class": target_class,
        "path_sha256": hashlib.sha256(
            _normalized_path(project).encode("utf-8")
        ).hexdigest(),
    }


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


def report_root() -> Path:
    return audit_root()


def ensure_report_containment(path: Path) -> Path:
    """Reject any report path that escapes ~/.agents/security-audits."""

    root = report_root()
    absolute = path.expanduser().resolve()
    try:
        absolute.relative_to(root)
    except ValueError as exc:
        raise PolicyError(
            f"PATH_VIOLATION: report path escapes the security-audits root: {absolute}"
        ) from exc
    return absolute


def require_positive_int(name: str, value: Any) -> int:
    if value is None:
        raise PolicyError(f"BUDGET_EXHAUSTED: {name} is required")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise PolicyError(f"BUDGET_EXHAUSTED: {name} must be a positive integer") from exc
    if number <= 0:
        raise PolicyError(f"BUDGET_EXHAUSTED: {name} must be a positive integer")
    return number


def require_target_class(value: str) -> str:
    cleaned = (value or "").strip()
    if cleaned not in ALLOWED_TARGET_CLASSES:
        raise PolicyError(
            "BLOCKED: target class must be one of "
            f"{', '.join(ALLOWED_TARGET_CLASSES)}; got {value!r}"
        )
    return cleaned


def require_trigger(value: str) -> str:
    cleaned = (value or "").strip()
    if cleaned not in ALLOWED_TRIGGERS:
        raise PolicyError(
            "BLOCKED: trigger must be one of "
            f"{', '.join(ALLOWED_TRIGGERS)}; got {value!r}"
        )
    return cleaned


def require_target_name(value: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        raise PolicyError("BLOCKED: target name is required")
    lowered = cleaned.lower()
    if "prod" in lowered:
        raise PolicyError("PROD_LOCKED: target name contains 'prod'")
    if any(token in lowered for token in ("customer", "live", "real-user")):
        raise PolicyError(
            "PROD_LOCKED: target name suggests live customer data; "
            "owner_approval is required and is not accepted in this rollout"
        )
    return cleaned


def forbid_live_customer_data(args: argparse.Namespace) -> None:
    if getattr(args, "live_customer_data", False):
        raise PolicyError(
            "PROD_LOCKED: --live-customer-data requires owner_approval; "
            "this rollout unconditionally rejects live production data"
        )
    if getattr(args, "owner_approval", None):
        raise PolicyError(
            "PROD_LOCKED: explicit owner_approval is not accepted in this rollout"
        )


def require_budget(args: argparse.Namespace) -> tuple[int, int]:
    max_iterations = require_positive_int("max_iterations", args.max_iterations)
    max_fresh_tokens = require_positive_int("max_fresh_tokens", args.max_fresh_tokens)
    return max_iterations, max_fresh_tokens


def _security_audit_module_path() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "security-audit-cycle"
        / "scripts"
        / "security_audit.py"
    )


def _load_security_audit_status():
    """Thin import of the existing audytuj status implementation.

    The module is registered in ``sys.modules`` before execution so
    dataclass-style introspection inside the loaded module keeps working.
    Returns ``None`` when the helper is unavailable; the caller must fail
    closed and must never duplicate the freshness algorithm.
    """

    path = _security_audit_module_path()
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location("audytuj_security_audit", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_fleet_runner():
    """Thin import of the sibling ``fleet_runner`` module.

    Reuses an already-registered module so tests that inject a fake
    runner keep working; registers the module in ``sys.modules`` before
    execution for dataclass introspection.
    """

    existing = sys.modules.get("fleet_runner")
    if existing is not None:
        return existing
    path = Path(__file__).resolve().parent / "fleet_runner.py"
    spec = importlib.util.spec_from_file_location("fleet_runner", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def probe_audit_gate(project: Path, trigger: str) -> dict[str, Any]:
    """Read the existing 14-day ledger without writing anything new.

    Uses the sibling ``security-audit-cycle`` helper through a
    thin import of its ``status_payload``. There is no fallback probe and
    no duplicated freshness algorithm: when the sibling helper is missing
    or unreadable, the gate fails closed with ``PolicyError``.
    """

    sibling = _load_security_audit_status()
    if sibling is None:
        raise PolicyError(
            "BLOCKED: security-audit-cycle security_audit.py is "
            "unavailable; the sole 14-day ledger cannot be read"
        )
    resolved = Path(project).expanduser().resolve()
    try:
        payload = sibling.status_payload(
            resolved,
            sibling.DEFAULT_MAX_AGE_DAYS,
            sibling.DEFAULT_ACTIVE_WINDOW_DAYS,
        )
    except (OSError, ValueError) as exc:
        raise PolicyError(
            f"BLOCKED: cannot read the 14-day audit ledger: {exc}"
        ) from exc
    return {"source": "security-audit-cycle", "payload": payload}


def freshness_escalation_required(project: Path, trigger: str) -> dict[str, Any]:
    """Decide whether the existing gate requires an adversarial escalation.

    The decision is taken verbatim from the sibling ledger's
    ``adversarial_triggers`` mapping: ``next_substantive_work`` escalates
    when the recorded audit is missing or older than the freshness
    window, and ``pre_production_release_candidate`` always escalates.
    """

    probe = probe_audit_gate(project, trigger)
    payload = probe.get("payload") if isinstance(probe, dict) else None
    triggers = payload.get("adversarial_triggers") if isinstance(payload, dict) else None
    required = bool(triggers.get(trigger)) if isinstance(triggers, dict) else False
    state = payload.get("state") if isinstance(payload, dict) else None
    age_days = payload.get("age_days") if isinstance(payload, dict) else None
    return {
        "escalation_required": required,
        "trigger_source": (
            payload.get("escalation_source")
            if isinstance(payload, dict) and payload.get("escalation_source")
            else probe.get("source")
        ),
        "reason": f"gate_state={state} age_days={age_days} trigger={trigger}",
    }


def is_excluded_dir(name: str) -> bool:
    lowered = name.lower()
    if lowered in EXCLUDED_DIRS:
        return True
    return (
        lowered.startswith(".venv")
        or lowered.startswith("venv-")
        or lowered.endswith("-venv")
        or lowered.endswith("_venv")
    )


def is_sensitive_file(path: Path) -> bool:
    name = path.name.lower()
    for pattern in SENSITIVE_FILE_GLOBS:
        if fnmatch.fnmatch(name, pattern.lower()):
            return True
    return False


def iter_target_files(project: Path) -> Iterable[Path]:
    emitted = 0
    for root, dirs, files in os.walk(project, followlinks=False):
        dirs[:] = [
            name
            for name in dirs
            if not is_excluded_dir(name)
            and not is_reparse_point(Path(root) / name)
        ]
        for name in files:
            path = Path(root) / name
            if is_reparse_point(path):
                continue
            if is_sensitive_file(path):
                continue
            if path.suffix.lower() not in SOURCE_EXTENSIONS:
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


def build_sanitized_workspace(project: Path, destination: Path) -> dict[str, Any]:
    """Create the only workspace visible to the model from redacted allowlisted text."""

    destination.mkdir(parents=True, exist_ok=False)
    copied = 0
    total_bytes = 0
    for source in iter_target_files(project):
        relative = source.relative_to(project)
        reject_reparse_components(source)
        try:
            text = source.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        redacted = redact_text(text, relative.as_posix())
        encoded = redacted.encode("utf-8")
        if total_bytes + len(encoded) > MAX_SNAPSHOT_BYTES:
            break
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(redacted, encoding="utf-8", newline="\n")
        copied += 1
        total_bytes += len(encoded)
    if copied == 0:
        raise PolicyError("TARGET_UNTRUSTED: sanitized model workspace is empty")
    return {
        "file_count": copied,
        "total_bytes": total_bytes,
        "workspace_sha256": hash_sanitized_workspace(destination),
    }


def hash_sanitized_workspace(workspace: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in workspace.rglob("*") if item.is_file()):
        relative = path.relative_to(workspace).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


REDACTION_TOKEN_RE = re.compile("\x00REDACTED(\\d+)\x00")


def redact_text(text: str, source_path: str) -> str:
    """Apply redaction rules without producing nested redaction markers.

    Redaction markers - both the ones already present in the input and
    the ones produced by earlier rules in this same pass - are masked out
    as opaque tokens before each subsequent rule runs, then re-injected
    in a single order-independent substitution. The result is idempotent:
    ``redact_text(redact_text(text)) == redact_text(text)``.
    """

    if not text:
        return text
    masked: list[str] = []

    def mask_markers(value: str) -> str:
        pieces: list[str] = []
        last = 0
        for match in REDACTED_MARKER_RE.finditer(value):
            pieces.append(value[last:match.start()])
            masked.append(match.group(0))
            pieces.append(f"\x00REDACTED{len(masked) - 1}\x00")
            last = match.end()
        pieces.append(value[last:])
        return "".join(pieces)

    redacted = mask_markers(text)
    for kind, pattern in REDACT_RULES:
        placeholder = REDACTION_PLACEHOLDER.format(kind=kind, path=source_path)
        redacted = mask_markers(pattern.sub(placeholder, redacted))
    return REDACTION_TOKEN_RE.sub(
        lambda match: masked[int(match.group(1))], redacted
    )


def redact_value(value: Any, source_path: str = "result") -> Any:
    """Recursively redact every model-derived string before persistence."""

    if isinstance(value, str):
        return redact_text(value, source_path)
    if isinstance(value, list):
        return [redact_value(item, f"{source_path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, dict):
        return {
            str(key): redact_value(item, f"{source_path}.{key}")
            for key, item in value.items()
        }
    return value


def residual_secret_hits(text: str) -> list[str]:
    without_markers = REDACTED_MARKER_RE.sub("", text)
    return [kind for kind, pattern in REDACT_RULES if pattern.search(without_markers)]


def verify_redacted_artifacts(root: Path) -> None:
    """Mandatory last-mile scan before a report directory becomes durable."""

    leaks: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".md", ".json", ".jsonl", ".txt"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for kind in residual_secret_hits(text):
            leaks.append(f"{path.relative_to(root).as_posix()}:{kind}")
    if leaks:
        raise PolicyError(
            "REDACTION_FAILED: residual sensitive material in staged artifacts: "
            + ", ".join(leaks[:8])
        )


def contains_deny_action(command: str) -> str | None:
    """Expose the runner's executable probe policy through the public helper."""

    return _load_fleet_runner().contains_deny_action(command)


def deterministic_vector_seed(target_name: str, iteration: int, vector: str) -> str:
    payload = f"{target_name}|{iteration}|{vector}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def read_observation(path: Path, project_root: Path) -> dict[str, Any]:
    """Bounded observation metadata; never persists source snippets.

    The file content is read only to compute the hash and to verify that
    redaction applies. Persisted fields are limited to the relative path,
    size, content hash and redaction counters - no source text, secret
    value or payload body crosses into the report.
    """

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {
            "path": path.relative_to(project_root).as_posix(),
            "read_error": type(exc).__name__,
        }
    relative = path.relative_to(project_root).as_posix()
    redacted = redact_text(text, relative)
    redaction_count = len(REDACTED_MARKER_RE.findall(redacted))
    return {
        "path": relative,
        "size_bytes": path.stat().st_size,
        "sha256": hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest(),
        "redaction_count": redaction_count,
        "contains_redacted_material": redaction_count > 0,
    }


def perform_reconnaissance(project: Path, max_files: int = 16) -> dict[str, Any]:
    files = []
    observations: list[dict[str, Any]] = []
    for path in iter_target_files(project):
        files.append(path.relative_to(project).as_posix())
        observations.append(read_observation(path, project))
        if len(observations) >= max_files:
            break
    return {
        "scanned_files": len(files),
        "files": files,
        "observations": observations,
        "redaction_placeholder": REDACTION_PLACEHOLDER,
    }


def load_synthetic_fixture() -> dict[str, Any] | None:
    """Return the bundled dry-run fixture manifest, if present."""

    candidate = (
        Path(__file__).resolve().parents[1]
        / "tests"
        / "fixtures"
        / "synthetic-target"
        / "manifest.json"
    )
    if not candidate.exists():
        return None
    try:
        return json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def bounded_fleet_iteration(
    *,
    project_id: str,
    target_name: str,
    iteration: int,
    vector: str,
    previous_summary: str,
    fixture: Mapping[str, Any] | None,
    fleet_runner: Any,
    fleet_marker: Mapping[str, Any] | None,
    cumulative_fresh_tokens: int,
    max_fresh_tokens: int,
    workspace: Path | None = None,
) -> tuple[dict[str, Any], int, int, int, str]:
    """Invoke Fleet once and update the cumulative fresh-token counter.

    Returns ``(iteration_record, fresh_tokens, cache_read_tokens,
    cumulative_fresh_tokens, fleet_status)``. Raises ``PolicyError`` when
    the Fleet release, profile, handoff or telemetry is missing.
    """

    if cumulative_fresh_tokens > max_fresh_tokens:
        raise PolicyError(
            "BUDGET_EXHAUSTED: cumulative fresh tokens already exceed ceiling"
        )
    try:
        result = fleet_runner.invoke_iteration(
            project_id=project_id,
            iteration=iteration,
            vector=vector,
            previous_summary=previous_summary,
            fixture=fixture,
            marker=fleet_marker,
            workspace=str(workspace) if workspace is not None else None,
        )
    except fleet_runner.FleetError as exc:
        raise PolicyError(f"BLOCKED: Fleet iteration failed closed: {exc}") from exc
    projected_fresh = cumulative_fresh_tokens + result.fresh_tokens
    if projected_fresh > max_fresh_tokens:
        raise PolicyError(
            f"BUDGET_EXHAUSTED: projected fresh tokens {projected_fresh} "
            f"exceed ceiling {max_fresh_tokens}"
        )
    iteration_record = {
        "iteration": iteration,
        "vector": vector,
        "fleet_run_id": result.run_id,
        "observed_model": result.observed_model,
        "fresh_tokens": result.fresh_tokens,
        "cache_read_tokens": result.cache_read_tokens,
        "cumulative_fresh_tokens": projected_fresh,
        "external_effect_count": 0,
        "redacted": True,
        "proposed_probe": redact_value(result.proposed_probe, "result.probe"),
        "finding_status": result.finding_status,
        "findings": redact_value(result.findings, "result.findings"),
        "evidence": redact_value(result.evidence, "result.evidence"),
        "redacted_observation": {
            "fleet_status": result.status,
            "fleet_summary_hash": hashlib.sha256(
                result.summary.encode("utf-8")
            ).hexdigest(),
            "previous_summary_hash": hashlib.sha256(
                previous_summary.encode("utf-8")
            ).hexdigest(),
        },
    }
    return (
        iteration_record,
        result.fresh_tokens,
        result.cache_read_tokens,
        projected_fresh,
        result.status,
    )


def build_finding_id(iteration: int, rule: str) -> str:
    return f"FIND-{iteration:03d}-{slugify(rule)[:24]}"


def build_remediation(findings: list[dict[str, Any]]) -> list[dict[str, str]]:
    remediation: list[dict[str, str]] = []
    for finding in findings:
        remediation.append(
            {
                "severity": finding.get("severity", "low"),
                "location": finding.get("location", "n/a"),
                "summary": finding.get("summary", "n/a"),
                "recommendation": finding.get("recommendation", "n/a"),
            }
        )
    if not remediation:
        remediation.append(
            {
                "severity": "info",
                "location": "n/a",
                "summary": "No high-severity automated findings in synthetic scope.",
                "recommendation": "Continue periodic read-only audits and follow secure-by-default guidance.",
            }
        )
    return remediation


def render_remediation_markdown(remediation: list[dict[str, str]]) -> str:
    lines = ["# Remediation", ""]
    for item in remediation:
        lines.append(
            f"- [{item['severity']}] {item['location']} - {item['summary']}"
        )
        lines.append(f"  - Recommendation: {item['recommendation']}")
    return "\n".join(lines) + "\n"


def render_report(metadata: dict[str, Any], recon: dict[str, Any]) -> str:
    lines = [
        "# Adversarial audit report",
        "",
        f"- Audit ID: `{metadata['audit_id']}`",
        f"- Trigger: `{metadata['trigger']}`",
        f"- Target: `{metadata['target_ref']}`",
        f"- Environment: `{metadata['target_environment']}`",
        f"- Iterations: {metadata['iteration_count']} / {metadata['budget_max_iterations']}",
        f"- Fresh tokens charged: {metadata['fresh_token_count']} / {metadata['budget_max_fresh_tokens']}",
        f"- Cache-read tokens reported: {metadata['cache_read_token_count']}",
        f"- Verdict: **{metadata['verdict']}**",
        f"- Redaction status: `{metadata['redaction_status']}`",
        f"- External effect count: {metadata['external_effect_count']}",
        f"- Trigger source: `{metadata['trigger_source']}`",
        f"- Fleet release: `{metadata['fleet_release']}`",
        f"- Fleet profile: `{metadata['fleet_profile']}`",
        f"- Observed model: `{metadata['observed_model']}`",
        f"- Probe allowlist: `{', '.join(metadata['probe_allowlist'])}`",
        f"- Started at: `{metadata['started_at']}`",
        f"- Completed at: `{metadata['completed_at']}`",
        "",
        "## Reconnaissance",
        "",
        f"- Scanned files: {recon['scanned_files']}",
        "- Files (redacted):",
    ]
    for entry in recon["files"][:16]:
        lines.append(f"  - `{entry}`")
    lines.append("")
    lines.append("## Findings")
    lines.append("")
    if metadata["findings"]:
        lines.append("| Severity | Location | Summary | Recommendation |")
        lines.append("| --- | --- | --- | --- |")
        for finding in metadata["findings"]:
            lines.append(
                "| {severity} | `{location}` | {summary} | {recommendation} |".format(
                    severity=finding.get("severity", "low"),
                    location=finding.get("location", "n/a"),
                    summary=finding.get("summary", "n/a"),
                    recommendation=finding.get("recommendation", "n/a"),
                )
            )
    else:
        lines.append(
            "No automated findings beyond the synthetic read-only scope. "
            "The bundled fixture was exercised as a ``synthetic_harness_probe``."
        )
    lines.append("")
    lines.append("## Limitations")
    lines.append("")
    lines.append(
        "Synthetic bounded audit does not prove absence of vulnerabilities. "
        "It records one redacted observation per iteration plus bounded Fleet "
        "telemetry and a remediation queue for the owner."
    )
    return "\n".join(lines) + "\n"


def run_iterations(
    project: Path,
    target_name: str,
    max_iterations: int,
    max_fresh_tokens: int,
    fixture: Mapping[str, Any] | None,
    fleet_runner: Any,
    fleet_marker: Mapping[str, Any] | None,
    model_workspace: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, int, str, str]:
    """Execute bounded Fleet-driven iterations and accumulate telemetry."""

    iterations: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    previous_summary = ""
    cumulative_fresh = 0
    cache_read_total = 0
    fleet_release = ""
    observed_model = ""
    project_id, _ = project_identity(project)
    for index in range(1, max_iterations + 1):
        vector = ALLOWED_VECTORS[(index - 1) % len(ALLOWED_VECTORS)]
        try:
            record, fresh, cache_read, cumulative_fresh, fleet_status = (
                bounded_fleet_iteration(
                    project_id=project_id,
                    target_name=target_name,
                    iteration=index,
                    vector=vector,
                    previous_summary=previous_summary,
                    fixture=fixture if index == 1 else None,
                    fleet_runner=fleet_runner,
                    fleet_marker=fleet_marker,
                    cumulative_fresh_tokens=cumulative_fresh,
                    max_fresh_tokens=max_fresh_tokens,
                    workspace=model_workspace,
                )
            )
        except PolicyError:
            raise
        iterations.append(record)
        for finding in record["findings"]:
            findings.append(
                {
                    "finding_id": build_finding_id(index, finding["finding_id"]),
                    "severity": finding["severity"],
                    "location": finding["location"],
                    "summary": finding["summary"],
                    "recommendation": finding["recommendation"],
                    "evidence_ref": finding["evidence_ref"],
                    "iteration": index,
                }
            )
        cache_read_total += cache_read
        fleet_release = fleet_release or str(
            fleet_marker.get("package_version") if fleet_marker else "unknown"
        )
        observed_model = observed_model or record["observed_model"]
        previous_summary = json.dumps(
            {
                "vector": vector,
                "fleet_status": fleet_status,
                "finding_status": record["finding_status"],
                "findings": record["findings"],
                "evidence": record["evidence"],
                "probe": record["proposed_probe"],
            },
            ensure_ascii=True,
            separators=(",", ":"),
        )
    return (
        iterations,
        findings,
        cumulative_fresh,
        cache_read_total,
        fleet_release,
        observed_model,
    )


def execute_run(args: argparse.Namespace) -> int:
    forbid_live_customer_data(args)
    project = normalize_project(args.project)
    max_iterations, max_fresh_tokens = require_budget(args)
    target_class = require_target_class(args.target_class)
    target_name = require_target_name(args.target_name)
    trigger = require_trigger(args.trigger)
    target_attestation = attest_target(project, target_class)

    escalation = freshness_escalation_required(project, trigger)
    if not escalation.get("escalation_required"):
        print(
            json.dumps(
                {
                    "state": "BLOCKED",
                    "decision": "deny",
                    "reason": "freshness gate did not require adversarial escalation",
                    "trigger": trigger,
                    "trigger_source": escalation.get("trigger_source"),
                    "details": escalation,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 4

    runner_module = _load_fleet_runner()

    try:
        fleet_marker = runner_module.resolve_fleet()
    except runner_module.FleetError as exc:
        print(
            json.dumps(
                {
                    "state": "BLOCKED",
                    "decision": "deny",
                    "reason": str(exc),
                    "fleet_release_required": runner_module.MINIMUM_FLEET_VERSION,
                    "fleet_agent_required": runner_module.RED_TEAM_AGENT,
                    "observed_model_required": runner_module.EXPECTED_OBSERVED_MODEL,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 4

    run_id = (
        utc_now().strftime("%Y%m%dT%H%M%S%fZ")
        + f"__{slugify(args.agent or 'redteam')}__redteam"
    )
    project_id, project_dir = project_identity(project)
    audit_dir = ensure_report_containment(project_dir / "audits" / run_id)
    staging_dir = ensure_report_containment(
        project_dir / ".staging" / f"{run_id}-{uuid.uuid4().hex}"
    )
    staging_dir.mkdir(parents=True, exist_ok=False)
    model_workspace = staging_dir / ".model-workspace"
    fixture = load_synthetic_fixture() if target_class == "TEST" else None
    started = utc_now()
    try:
        snapshot = build_sanitized_workspace(project, model_workspace)
        recon = perform_reconnaissance(project)
        (
            iterations,
            findings,
            fresh_token_count,
            cache_read_token_count,
            fleet_release,
            observed_model,
        ) = run_iterations(
            project,
            target_name,
            max_iterations,
            max_fresh_tokens,
            fixture,
            runner_module,
            fleet_marker,
            model_workspace,
        )
        shutil.rmtree(model_workspace)
    except (PolicyError, OSError) as exc:
        shutil.rmtree(staging_dir, ignore_errors=True)
        print(
            json.dumps(
                {"state": "BLOCKED", "decision": "deny", "reason": str(exc)},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 4

    verdict = "REVISE" if findings else "PASS"
    metadata: dict[str, Any] = {
        "schema_version": 1,
        "audit_id": run_id,
        "project_id": project_id,
        "project_path_sha256": hashlib.sha256(
            _normalized_path(project).encode("utf-8")
        ).hexdigest(),
        "target_ref": target_name,
        "target_environment": target_class,
        "target_attestation": target_attestation,
        "model_workspace": snapshot,
        "trigger": trigger,
        "trigger_source": escalation.get("trigger_source"),
        "iteration_count": len(iterations),
        "budget_max_iterations": max_iterations,
        "budget_max_fresh_tokens": max_fresh_tokens,
        "fresh_token_count": fresh_token_count,
        "cache_read_token_count": cache_read_token_count,
        "findings": findings,
        "redaction_status": "verified-redacted",
        "external_effect_count": 0,
        "verdict": verdict,
        "agent": args.agent or "redteam",
        "fleet_release": fleet_release or "unknown",
        "fleet_profile": "red-team-only",
        "fleet_agent": runner_module.RED_TEAM_AGENT,
        "observed_model": observed_model or runner_module.EXPECTED_OBSERVED_MODEL,
        "probe_allowlist": list(runner_module.ALLOWED_PROBES),
        "started_at": iso_z(started),
        "completed_at": iso_z(utc_now()),
    }
    completed_at = metadata["completed_at"]
    metadata = redact_value(metadata, "metadata")
    recon = redact_value(recon, "recon")
    iterations = redact_value(iterations, "iterations")
    remediation = redact_value(build_remediation(findings), "remediation")
    try:
        atomic_json(staging_dir / "metadata.json", metadata)
        atomic_json(staging_dir / "recon.json", recon)
        iterations_path = staging_dir / "iterations.jsonl"
        for iteration in iterations:
            append_jsonl(iterations_path, iteration)
        (staging_dir / "remediation.md").write_text(
            render_remediation_markdown(remediation), encoding="utf-8"
        )
        (staging_dir / "REPORT.md").write_text(
            render_report(metadata, recon), encoding="utf-8"
        )
        verify_redacted_artifacts(staging_dir)
    except (OSError, PolicyError) as exc:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise PolicyError(f"REDACTION_FAILED: staged report was not published: {exc}") from exc

    latest = {
        "audit_id": run_id,
        "completed_at": metadata["completed_at"],
        "next_due_at": iso_z(parse_time(completed_at) + dt.timedelta(days=14)),
        "verdict": verdict,
        "agent": metadata["agent"],
        "report": str(audit_dir / "REPORT.md"),
        "redaction_status": "verified-redacted",
        "external_effect_count": 0,
        "trigger": trigger,
        "fresh_token_count": fresh_token_count,
        "cache_read_token_count": cache_read_token_count,
    }
    sibling = _load_security_audit_status()
    if sibling is None or not hasattr(sibling, "ProjectLock"):
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise PolicyError("BLOCKED: shared audit ProjectLock is unavailable")
    try:
        with sibling.ProjectLock(project_dir / ".lock"):
            audit_dir.parent.mkdir(parents=True, exist_ok=True)
            if audit_dir.exists():
                raise PolicyError(f"BLOCKED: audit directory already exists: {audit_dir}")
            os.replace(staging_dir, audit_dir)
            append_jsonl(
                project_dir / "adversarial-ledger.jsonl",
                {
                    "event": "adversarial_audit_completed",
                    "audit_id": run_id,
                    "completed_at": metadata["completed_at"],
                    "verdict": verdict,
                    "iteration_count": len(iterations),
                    "fresh_token_count": fresh_token_count,
                    "cache_read_token_count": cache_read_token_count,
                    "external_effect_count": 0,
                    "fleet_release": metadata["fleet_release"],
                    "observed_model": metadata["observed_model"],
                },
            )
            atomic_json(project_dir / "adversarial-latest.json", latest)
    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise

    print(
        json.dumps(
            {
                "state": verdict,
                "decision": "proceed",
                "audit_id": run_id,
                "report": str(audit_dir / "REPORT.md"),
                "audit_dir": str(audit_dir),
                "trigger": trigger,
                "external_effect_count": 0,
                "fresh_token_count": fresh_token_count,
                "cache_read_token_count": cache_read_token_count,
                "fleet_release": metadata["fleet_release"],
                "observed_model": metadata["observed_model"],
                "verdict": verdict,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if verdict == "PASS" else 3


def execute_preflight(args: argparse.Namespace) -> int:
    try:
        forbid_live_customer_data(args)
        project = normalize_project(args.project)
        max_iterations, max_fresh_tokens = require_budget(args)
        target_class = require_target_class(args.target_class)
        target_name = require_target_name(args.target_name)
        trigger = require_trigger(args.trigger)
        target_attestation = attest_target(project, target_class)
    except PolicyError as exc:
        print(
            json.dumps(
                {"state": "BLOCKED", "decision": "deny", "reason": str(exc)},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 4
    except ValueError as exc:
        print(
            json.dumps(
                {"state": "BLOCKED", "decision": "deny", "reason": str(exc)},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 4

    escalation = freshness_escalation_required(project, trigger)
    if not escalation.get("escalation_required"):
        print(
            json.dumps(
                {
                    "state": "BLOCKED",
                    "decision": "deny",
                    "reason": "freshness gate did not require adversarial escalation",
                    "trigger": trigger,
                    "trigger_source": escalation.get("trigger_source"),
                    "details": escalation,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 4

    runner_module = _load_fleet_runner()

    try:
        fleet_marker = runner_module.resolve_fleet()
    except runner_module.FleetError as exc:
        print(
            json.dumps(
                {
                    "state": "BLOCKED",
                    "decision": "deny",
                    "reason": str(exc),
                    "fleet_release_required": runner_module.MINIMUM_FLEET_VERSION,
                    "fleet_agent_required": runner_module.RED_TEAM_AGENT,
                    "observed_model_required": runner_module.EXPECTED_OBSERVED_MODEL,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 4

    print(
        json.dumps(
            {
                "state": "PRECHECK_OK",
                "decision": "proceed",
                "trigger": trigger,
                "trigger_source": escalation.get("trigger_source"),
                "max_iterations": max_iterations,
                "max_fresh_tokens": max_fresh_tokens,
                "target_class": target_class,
                "target_name": target_name,
                "target_attestation": target_attestation,
                "details": escalation,
                "fleet_release": fleet_marker.get("package_version"),
                "fleet_agent": fleet_marker.get("agent"),
                "observed_model": fleet_marker.get("expected_model"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def execute_status(args: argparse.Namespace) -> int:
    project = normalize_project(args.project)
    escalation = freshness_escalation_required(
        project, args.trigger or "next_substantive_work"
    )
    print(
        json.dumps(
            {
                "trigger_source": escalation.get("trigger_source"),
                "escalation_required": escalation.get("escalation_required"),
                "details": escalation,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def execute_verify(args: argparse.Namespace) -> int:
    path = Path(args.report).expanduser().resolve()
    audit_dir = ensure_report_containment(path if path.is_dir() else path.parent)
    leaked: list[dict[str, str]] = []
    for file in sorted(audit_dir.rglob("*")):
        if not file.is_file() or file.suffix.lower() not in {".md", ".json", ".jsonl", ".txt"}:
            continue
        try:
            text = file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for kind, pattern in REDACT_RULES:
            if pattern.search(text):
                leaked.append({"path": str(file), "kind": kind})
    if leaked:
        print(
            json.dumps(
                {"state": "REVISE", "leaked": leaked},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 3
    print(
        json.dumps(
            {"state": "PASS", "leaked": [], "audit_dir": str(audit_dir)},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bounded adversarial audit for the existing 14-day gate."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--project", required=True)
        subparser.add_argument("--target-class", required=True)
        subparser.add_argument("--target-name", required=True)
        # Budgets are validated by the fail-closed policy gate (exit 4),
        # not by argparse, so a missing or zero budget is a policy denial.
        subparser.add_argument("--max-iterations", type=int, default=None)
        subparser.add_argument("--max-fresh-tokens", type=int, default=None)
        subparser.add_argument("--trigger", required=True)
        subparser.add_argument("--agent", default="redteam")
        subparser.add_argument(
            "--live-customer-data", action="store_true", help="hard-deny in this rollout"
        )
        subparser.add_argument(
            "--owner-approval",
            default=None,
            help="hard-deny in this rollout; required for live production data",
        )

    preflight = sub.add_parser("preflight", help="Validate the adversarial run parameters.")
    add_common(preflight)

    run = sub.add_parser("run", help="Run a bounded adversarial audit.")
    add_common(run)

    status = sub.add_parser("status", help="Show the freshness gate trigger state.")
    status.add_argument("--project", required=True)
    status.add_argument(
        "--trigger",
        default="next_substantive_work",
        choices=ALLOWED_TRIGGERS,
    )

    verify = sub.add_parser("verify", help="Verify a report is fully redacted.")
    verify.add_argument("--report", required=True)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "preflight":
            return execute_preflight(args)
        if args.command == "run":
            return execute_run(args)
        if args.command == "status":
            return execute_status(args)
        if args.command == "verify":
            return execute_verify(args)
    except PolicyError as exc:
        print(
            json.dumps(
                {"state": "BLOCKED", "decision": "deny", "reason": str(exc)},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 4
    except ValueError as exc:
        print(
            json.dumps(
                {"state": "BLOCKED", "decision": "deny", "reason": str(exc)},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 4
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
