"""
agent-loop V0.1 - security contract (Grok adversarial reference).

Pure, stdlib-only primitives that the canonical controller MUST satisfy.
This is NOT a full controller and does NOT route models. It encodes:
  - path containment / path-claim collision
  - idempotent budget accounting by run_id
  - circuit breaker (kill switch, attempts, repeated failure, no-progress, timeout, budget)
  - ambient secret scrubbing for child processes
  - L1 write guard (operational state only)
  - atomic state replace helpers

The canonical controller imports this module directly. Keep these primitives
stdlib-only and independently testable.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

# Align with Agent Fleet sensitive keys (skill-workbench/agent-fleet/scripts/fleet.py).
SENSITIVE_ENV_KEYS: frozenset[str] = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AZURE_OPENAI_API_KEY",
        "BAILIAN_CODING_PLAN_API_KEY",
        "CODEX_API_KEY",
        "DASHSCOPE_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GROK_API_KEY",
        "KIMI_API_KEY",
        "MOONSHOT_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "QWEN_API_KEY",
        "XAI_API_KEY",
        # Extra ambient secrets commonly present on owner host
        "ELEVENLABS_API_KEY",
        "HF_TOKEN",
        "HUGGING_FACE_HUB_TOKEN",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "NPM_TOKEN",
        "TWINE_PASSWORD",
        "PGPASSWORD",
        "DATABASE_URL",
        "REDIS_URL",
    }
)

# Name patterns: any matching environment key is always stripped.
SENSITIVE_NAME_RE = re.compile(
    r"(?:^|_)(API[_-]?KEY|TOKEN|SECRET|PASSWORD|PASSWD|PRIVATE[_-]?KEY)(?:$|_)",
    re.IGNORECASE,
)

# Value patterns that must never appear in logs or child env dumps.
SECRET_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----", re.I),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
)

ALLOWLISTED_ENV_EXACT: frozenset[str] = frozenset(
    {
        "ALLUSERSPROFILE",
        "APPDATA",
        "COMPUTERNAME",
        "COMSPEC",
        "HOME",
        "HOSTNAME",
        "LANG",
        "LOCALAPPDATA",
        "LOGNAME",
        "NO_COLOR",
        "NUMBER_OF_PROCESSORS",
        "OLDPWD",
        "OS",
        "PATH",
        "PATHEXT",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONIOENCODING",
        "PYTHONUTF8",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PUBLIC",
        "PWD",
        "SHELL",
        "SHLVL",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TERM",
        "TMP",
        "TMPDIR",
        "USER",
        "USERNAME",
        "USERPROFILE",
        "WINDIR",
        "AGENT_FLEET_DEPTH",
        "AGENT_FLEET_ROUTING_MODE",
    }
)

ALLOWLISTED_ENV_PREFIXES: tuple[str, ...] = (
    "LC_",
    "PROCESSOR_",
)

DEFAULT_VERIFIER_RESULT = "REJECT"


class SecurityError(Exception):
    """Base for security-contract violations."""


class PathEscapeError(SecurityError):
    pass


class PathClaimCollisionError(SecurityError):
    pass


class BudgetError(SecurityError):
    pass


class CircuitOpenError(SecurityError):
    pass


class L1WriteViolation(SecurityError):
    pass


class KillSwitchActive(CircuitOpenError):
    pass


# ---------------------------------------------------------------------------
# Path containment
# ---------------------------------------------------------------------------


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_reparse(stat_result: os.stat_result) -> bool:
    attributes = getattr(stat_result, "st_file_attributes", 0)
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & marker)


def assert_no_reparse_chain(root: Path, target: Path) -> None:
    """Reject existing symlink, junction, or reparse components below root."""
    if not _is_relative_to(target, root):
        raise PathEscapeError(f"path escapes project root: {target}")
    cursor = root
    relative = target.relative_to(root)
    for part in relative.parts:
        cursor = cursor / part
        if not cursor.exists():
            continue
        details = cursor.lstat()
        if cursor.is_symlink() or _is_reparse(details):
            raise PathEscapeError(f"path crosses symlink or reparse point: {cursor}")


def resolve_under_root(project_root: Path | str, candidate: str | Path) -> Path:
    """
    Resolve candidate under project_root. Rejects absolute paths outside root,
    `..` traversal, and (when present) symlink/reparse targets that escape root.
    """
    root = Path(project_root).resolve(strict=False)
    raw = Path(candidate)

    if raw.is_absolute():
        resolved = raw.resolve(strict=False)
        if not _is_relative_to(resolved, root):
            raise PathEscapeError(f"absolute path escapes project root: {candidate!r}")
        assert_no_reparse_chain(root, resolved)
        return resolved

    # Lexical rejection of `..` segments before resolve (defense in depth).
    parts = raw.parts
    if ".." in parts:
        # Still allow if final resolve stays under root, but only after check.
        pass

    joined = (root / raw).resolve(strict=False)
    if not _is_relative_to(joined, root):
        raise PathEscapeError(f"path escapes project root: {candidate!r} -> {joined}")
    assert_no_reparse_chain(root, joined)
    return joined


def assert_paths_under_root(
    project_root: Path | str, paths: Sequence[str | Path]
) -> list[Path]:
    return [resolve_under_root(project_root, p) for p in paths]


def normalize_claim(project_root: Path | str, claim: str) -> str:
    """
    Normalize a path claim to a posix-relative pattern under root.

    Supports literal paths and simple trailing globs (`**`, `*`). Glob
    metacharacters are preserved after validating the non-glob prefix.
    """
    raw = claim.replace("\\", "/").strip()
    if raw in {"", ".", "./**", "**", "/**"}:
        return ""

    # Split into stable prefix (no glob) and optional trailing pattern.
    parts = [p for p in raw.split("/") if p and p != "."]
    prefix_parts: list[str] = []
    rest_parts: list[str] = []
    seen_glob = False
    for p in parts:
        if seen_glob or any(ch in p for ch in "*?[]"):
            seen_glob = True
            rest_parts.append(p)
        else:
            prefix_parts.append(p)

    if prefix_parts:
        # Validate prefix cannot escape root.
        resolve_under_root(project_root, "/".join(prefix_parts))
    elif rest_parts and any(p == ".." for p in rest_parts):
        raise PathEscapeError(f"claim escapes project root: {claim!r}")

    if any(p == ".." for p in parts):
        raise PathEscapeError(f"claim escapes project root: {claim!r}")

    normalized = "/".join(parts)
    return normalized


def _claim_prefixes(claim: str) -> list[str]:
    """Return comparable prefixes for overlap (strip trailing globs)."""
    if claim == "":
        return [""]
    parts = claim.split("/")
    out: list[str] = []
    acc: list[str] = []
    for p in parts:
        if p in {"**", "*"} or any(ch in p for ch in "*?[]"):
            # Everything under acc is claimed.
            out.append("/".join(acc) if acc else "")
            return out
        acc.append(p)
        out.append("/".join(acc))
    out.append("/".join(acc))
    return out


def claims_overlap(a: str, b: str) -> bool:
    """True if two posix-relative claims collide (equal, ancestor, or glob cover)."""
    if a == b:
        return True
    if a == "" or b == "":
        return True

    def covers(claim: str, other: str) -> bool:
        # claim covers other if other is under claim's non-glob prefix
        # or equal to a concrete path prefix of claim.
        c_parts = claim.split("/")
        o_parts = other.split("/")
        # Strip trailing pure-glob segments from claim for prefix compare.
        c_prefix: list[str] = []
        for p in c_parts:
            if p in {"**", "*"} or any(ch in p for ch in "*?[]"):
                break
            c_prefix.append(p)
        # If claim is exactly a directory tree (ends with **), prefix must match.
        if not c_prefix:
            return True  # claim is ** / *
        if len(o_parts) < len(c_prefix):
            # other is ancestor of claim prefix -> still a collision
            return o_parts == c_prefix[: len(o_parts)] and not any(
                ch in other for ch in "*?[]"
            )
        return o_parts[: len(c_prefix)] == c_prefix

    # Normalize other side if it also has globs: compare prefixes both ways.
    a_concrete = "/".join(
        p
        for p in a.split("/")
        if p not in {"**", "*"} and not any(ch in p for ch in "*?[]")
    )
    b_concrete = "/".join(
        p
        for p in b.split("/")
        if p not in {"**", "*"} and not any(ch in p for ch in "*?[]")
    )
    if a_concrete and b_concrete:
        if a_concrete == b_concrete:
            return True
        # ancestor relationship on concrete prefixes
        ap, bp = a_concrete.split("/"), b_concrete.split("/")
        n = min(len(ap), len(bp))
        if ap[:n] == bp[:n]:
            return True
    return covers(a, b) or covers(b, a)


@dataclass
class PathClaimRegistry:
    """In-memory multi-loop path claim registry (process-local model)."""

    # loop_id -> set of normalized claims
    claims: dict[str, set[str]] = field(default_factory=dict)

    def acquire(
        self, loop_id: str, project_root: Path | str, raw_claims: Sequence[str]
    ) -> None:
        normalized = {normalize_claim(project_root, c) for c in raw_claims}
        for other_id, other_claims in self.claims.items():
            if other_id == loop_id:
                continue
            for c in normalized:
                for o in other_claims:
                    if claims_overlap(c, o):
                        raise PathClaimCollisionError(
                            f"loop {loop_id!r} claim {c!r} collides with "
                            f"loop {other_id!r} claim {o!r}"
                        )
        self.claims[loop_id] = normalized

    def release(self, loop_id: str) -> None:
        self.claims.pop(loop_id, None)


# ---------------------------------------------------------------------------
# Budget - idempotent by run_id
# ---------------------------------------------------------------------------


@dataclass
class BudgetLedger:
    """
    Daily / per-run budget with idempotent settlement.

    The same run_id is charged at most once. status/plan/recovery actions
    must call `note_non_billable` and never charge tokens.
    """

    max_tokens_per_run: int
    max_tokens_per_day: int
    max_runs_per_day: int
    settled_run_ids: set[str] = field(default_factory=set)
    tokens_charged_by_run: dict[str, int] = field(default_factory=dict)
    day_token_total: int = 0
    day_run_total: int = 0
    non_billable_events: int = 0

    def note_non_billable(self, action: str) -> None:
        if action not in {"status", "plan", "recovery", "doctor", "validate"}:
            raise BudgetError(f"action not non-billable: {action}")
        self.non_billable_events += 1

    def settle(self, run_id: str, tokens: int | None) -> dict[str, Any]:
        if not run_id or not isinstance(run_id, str):
            raise BudgetError("run_id required")
        if run_id in self.settled_run_ids:
            # Idempotent: do not re-charge.
            return {
                "run_id": run_id,
                "charged": False,
                "reason": "already_settled",
                "tokens": self.tokens_charged_by_run.get(run_id, 0),
                "day_token_total": self.day_token_total,
                "day_run_total": self.day_run_total,
            }

        # Missing telemetry -> charge 0 tokens but still count the run once.
        charge = 0 if tokens is None else int(tokens)
        if charge < 0:
            raise BudgetError("tokens cannot be negative")
        if charge > self.max_tokens_per_run:
            raise BudgetError(
                f"per-run budget exceeded: {charge} > {self.max_tokens_per_run}"
            )
        if self.day_run_total + 1 > self.max_runs_per_day:
            raise BudgetError("max runs per day exceeded")
        if self.day_token_total + charge > self.max_tokens_per_day:
            raise BudgetError(
                f"daily token budget exceeded: "
                f"{self.day_token_total + charge} > {self.max_tokens_per_day}"
            )

        self.settled_run_ids.add(run_id)
        self.tokens_charged_by_run[run_id] = charge
        self.day_token_total += charge
        self.day_run_total += 1
        return {
            "run_id": run_id,
            "charged": True,
            "reason": "settled",
            "tokens": charge,
            "tokens_display": "unknown" if tokens is None else charge,
            "day_token_total": self.day_token_total,
            "day_run_total": self.day_run_total,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_tokens_per_run": self.max_tokens_per_run,
            "max_tokens_per_day": self.max_tokens_per_day,
            "max_runs_per_day": self.max_runs_per_day,
            "settled_run_ids": sorted(self.settled_run_ids),
            "tokens_charged_by_run": dict(self.tokens_charged_by_run),
            "day_token_total": self.day_token_total,
            "day_run_total": self.day_run_total,
            "non_billable_events": self.non_billable_events,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "BudgetLedger":
        return cls(
            max_tokens_per_run=int(data["max_tokens_per_run"]),
            max_tokens_per_day=int(data["max_tokens_per_day"]),
            max_runs_per_day=int(data["max_runs_per_day"]),
            settled_run_ids=set(data.get("settled_run_ids") or []),
            tokens_charged_by_run={
                str(k): int(v)
                for k, v in (data.get("tokens_charged_by_run") or {}).items()
            },
            day_token_total=int(data.get("day_token_total") or 0),
            day_run_total=int(data.get("day_run_total") or 0),
            non_billable_events=int(data.get("non_billable_events") or 0),
        )


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


@dataclass
class CircuitBreaker:
    max_attempts: int
    repeated_failure_threshold: int = 3
    no_progress_threshold: int = 3
    timeout_seconds: float = 600.0

    kill_switch: bool = False
    attempt_count: int = 0
    consecutive_failures: int = 0
    consecutive_no_progress: int = 0
    open_reason: str | None = None
    last_progress_fingerprint: str | None = None

    def arm_kill_switch(self, reason: str = "manual") -> None:
        self.kill_switch = True
        self.open_reason = f"kill_switch:{reason}"

    def clear_kill_switch(self) -> None:
        """Only human/operator may clear; tests call this explicitly."""
        self.kill_switch = False
        if self.open_reason and self.open_reason.startswith("kill_switch:"):
            self.open_reason = None

    def ensure_closed(self) -> None:
        if self.kill_switch:
            raise KillSwitchActive(self.open_reason or "kill_switch")
        if self.open_reason:
            raise CircuitOpenError(self.open_reason)
        if self.attempt_count >= self.max_attempts:
            self.open_reason = "max_attempts"
            raise CircuitOpenError(self.open_reason)

    def begin_attempt(self) -> int:
        self.ensure_closed()
        self.attempt_count += 1
        return self.attempt_count

    def record_success(self, progress_fingerprint: str) -> None:
        if (
            self.last_progress_fingerprint is not None
            and progress_fingerprint == self.last_progress_fingerprint
        ):
            self.consecutive_no_progress += 1
            if self.consecutive_no_progress >= self.no_progress_threshold:
                self.open_reason = "no_progress"
                raise CircuitOpenError(self.open_reason)
        else:
            self.consecutive_no_progress = 0
            self.last_progress_fingerprint = progress_fingerprint
        self.consecutive_failures = 0

    def record_failure(self, kind: str = "error") -> None:
        self.consecutive_failures += 1
        if kind == "timeout":
            self.open_reason = "timeout"
            raise CircuitOpenError(self.open_reason)
        if kind == "budget":
            self.open_reason = "budget_exceeded"
            raise CircuitOpenError(self.open_reason)
        if self.consecutive_failures >= self.repeated_failure_threshold:
            self.open_reason = "repeated_failure"
            raise CircuitOpenError(self.open_reason)

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_attempts": self.max_attempts,
            "repeated_failure_threshold": self.repeated_failure_threshold,
            "no_progress_threshold": self.no_progress_threshold,
            "timeout_seconds": self.timeout_seconds,
            "kill_switch": self.kill_switch,
            "attempt_count": self.attempt_count,
            "consecutive_failures": self.consecutive_failures,
            "consecutive_no_progress": self.consecutive_no_progress,
            "open_reason": self.open_reason,
            "last_progress_fingerprint": self.last_progress_fingerprint,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CircuitBreaker":
        return cls(
            max_attempts=int(data["max_attempts"]),
            repeated_failure_threshold=int(data.get("repeated_failure_threshold") or 3),
            no_progress_threshold=int(data.get("no_progress_threshold") or 3),
            timeout_seconds=float(data.get("timeout_seconds") or 600.0),
            kill_switch=bool(data.get("kill_switch") or False),
            attempt_count=int(data.get("attempt_count") or 0),
            consecutive_failures=int(data.get("consecutive_failures") or 0),
            consecutive_no_progress=int(data.get("consecutive_no_progress") or 0),
            open_reason=data.get("open_reason"),
            last_progress_fingerprint=data.get("last_progress_fingerprint"),
        )


# ---------------------------------------------------------------------------
# Env scrub
# ---------------------------------------------------------------------------


def _is_allowlisted_env_name(name: str) -> bool:
    upper = name.upper()
    if upper in ALLOWLISTED_ENV_EXACT:
        return True
    for prefix in ALLOWLISTED_ENV_PREFIXES:
        if upper.startswith(prefix):
            return True
    return False


def scrub_environment(
    source: Mapping[str, str] | None = None,
    *,
    extra_deny: Iterable[str] = (),
) -> dict[str, str]:
    """
    Build a child-process environment without ambient secrets.

    Policy: start from source (default os.environ), drop known sensitive keys
    and name-pattern matches, drop values matching secret patterns. Keep a
    minimal allowlist of process essentials. Never pass full ambient env.
    """
    src = dict(source if source is not None else os.environ)
    deny = set(SENSITIVE_ENV_KEYS) | {d.upper() for d in extra_deny}
    out: dict[str, str] = {}

    for key, value in src.items():
        ukey = key.upper()
        if ukey in deny:
            continue
        if SENSITIVE_NAME_RE.search(key):
            continue
        if any(p.search(value) for p in SECRET_VALUE_PATTERNS):
            continue
        # Prefer allowlist for unknown keys that look like secrets; otherwise
        # only pass through allowlisted operational vars to minimize ambient leak.
        if _is_allowlisted_env_name(key):
            out[key] = value
        # Non-allowlisted keys are dropped (deny-by-default for ambient).

    out.setdefault("NO_COLOR", "1")
    out["AGENT_LOOP_SCRUBBED"] = "1"
    return out


def assert_no_secrets_in_text(
    text: str, *, labels: Mapping[str, str] | None = None
) -> None:
    """Raise if known secret values leak into logs/artifacts."""
    if labels:
        for name, secret in labels.items():
            if secret and secret in text:
                raise SecurityError(f"secret leaked into text: {name}")
    for pat in SECRET_VALUE_PATTERNS:
        if pat.search(text):
            raise SecurityError(f"secret pattern leaked: {pat.pattern}")


# ---------------------------------------------------------------------------
# L1 write guard
# ---------------------------------------------------------------------------


def loop_state_root(project_root: Path | str, loop_id: str) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,63}", loop_id or ""):
        raise SecurityError(f"invalid loop_id: {loop_id!r}")
    root = Path(project_root).resolve(strict=False)
    return root / ".agents" / "loops" / loop_id


def assert_l1_write_allowed(
    project_root: Path | str,
    loop_id: str,
    target: str | Path,
    *,
    watched_globs: Sequence[str] | None = None,
) -> Path:
    """
    L1 may write only under .agents/loops/<loop_id>/ (state + reports).
    Any path outside that tree is rejected. Watched project files are hard-deny.
    """
    allowed_root = loop_state_root(project_root, loop_id).resolve(strict=False)
    resolved = resolve_under_root(project_root, target)
    if not _is_relative_to(resolved, allowed_root):
        raise L1WriteViolation(f"L1 write outside operational state: {target!r}")
    # Defense: still reject if somehow inside allowed_root but matches watched
    # (should not happen for .agents/loops).
    if watched_globs:
        rel = resolved.relative_to(Path(project_root).resolve(strict=False)).as_posix()
        for g in watched_globs:
            # Minimal glob: suffix / exact / ** prefix
            if g.endswith("/**") and rel.startswith(g[:-3]):
                raise L1WriteViolation(f"L1 write hits watched scope {g}: {rel}")
            if g == rel or g == f"**/{Path(rel).name}":
                raise L1WriteViolation(f"L1 write hits watched scope {g}: {rel}")
    return resolved


def default_verifier_result() -> str:
    return DEFAULT_VERIFIER_RESULT


# ---------------------------------------------------------------------------
# Atomic JSON state + OS lock (Windows + POSIX)
# ---------------------------------------------------------------------------


def atomic_write_json(path: Path | str, data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def append_jsonl(path: Path | str, record: Mapping[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(dict(record), sort_keys=True, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())


class LoopLock:
    """
    Process-held exclusive lock for a single loop directory.
    Uses msvcrt on Windows and fcntl on POSIX.
    """

    def __init__(self, lock_path: Path | str) -> None:
        self.lock_path = Path(lock_path)
        self._fh: Any = None

    def acquire(self, *, blocking: bool = False) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.lock_path, "a+", encoding="utf-8")
        try:
            if os.name == "nt":
                import msvcrt

                # Lock one byte; non-blocking raises OSError if held.
                mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
                self._fh.seek(0)
                if self._fh.read(1) == "":
                    self._fh.write("0")
                    self._fh.flush()
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), mode, 1)
            else:
                import fcntl

                flags = fcntl.LOCK_EX
                if not blocking:
                    flags |= fcntl.LOCK_NB
                fcntl.flock(self._fh.fileno(), flags)
        except OSError as exc:
            self.release()
            raise CircuitOpenError(f"loop_lock_busy: {exc}") from exc

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                try:
                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl

                try:
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None

    def __enter__(self) -> "LoopLock":
        self.acquire(blocking=False)
        return self

    def __exit__(self, *args: object) -> None:
        self.release()


def new_run_id() -> str:
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"run-{ts}-{secrets.token_hex(4)}"


def validate_loop_json(doc: Mapping[str, Any]) -> list[str]:
    """Validate the strict nested V0.1 contract without external packages."""
    errors: list[str] = []
    required = {
        "schema_version",
        "loop_id",
        "goal",
        "non_goals",
        "watched_scope",
        "cadence",
        "timezone",
        "maturity",
        "delegation",
        "limits",
        "stop_conditions",
        "human_gates",
        "kill_switch",
        "retention",
        "write_policy",
    }
    for key in sorted(required):
        if key not in doc:
            errors.append(f"missing:{key}")
    unknown = set(doc) - required
    for key in sorted(unknown):
        errors.append(f"unknown:{key}")
    if doc.get("schema_version") != 1:
        errors.append("invalid:schema_version")
    loop_id = doc.get("loop_id")
    if not isinstance(loop_id, str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9-]{1,63}", loop_id
    ):
        errors.append("invalid:loop_id")
    if not isinstance(doc.get("goal"), str) or not str(doc.get("goal")).strip():
        errors.append("invalid:goal")
    if not isinstance(doc.get("non_goals"), list):
        errors.append("invalid:non_goals")
    if not isinstance(doc.get("watched_scope"), list) or not doc.get("watched_scope"):
        errors.append("invalid:watched_scope")
    if doc.get("maturity") not in {None, "L1", "L2"} and "maturity" in doc:
        errors.append("invalid:maturity")
    if doc.get("maturity") == "L3":
        errors.append("L3_out_of_scope_v0_1")

    cadence = doc.get("cadence")
    if not isinstance(cadence, Mapping) or cadence.get("kind") not in {
        "manual",
        "interval",
        "cron",
    }:
        errors.append("invalid:cadence")
    delegation = doc.get("delegation")
    if not isinstance(delegation, Mapping):
        errors.append("invalid:delegation")
    else:
        if delegation.get("via") != "agent-fleet":
            errors.append("invalid:delegation.via")
        if delegation.get("mode") not in {"advisory", "review"}:
            errors.append("invalid:delegation.mode")
        for key in ("task_file", "assessment_file"):
            if not isinstance(delegation.get(key), str) or not delegation.get(key):
                errors.append(f"invalid:delegation.{key}")

    limits = doc.get("limits")
    if not isinstance(limits, Mapping):
        errors.append("invalid:limits")
    else:
        for key in ("max_attempts_per_run", "run_timeout_seconds", "budget"):
            if key not in limits:
                errors.append(f"missing:limits.{key}")
        budget = limits.get("budget")
        if not isinstance(budget, Mapping):
            errors.append("invalid:limits.budget")
        else:
            if not isinstance(budget.get("runs_per_day"), int):
                errors.append("invalid:limits.budget.runs_per_day")
            if budget.get("unknown_token_policy") not in {"stop", "allow"}:
                errors.append("invalid:limits.budget.unknown_token_policy")
            if "token_policy" in budget and budget.get("token_policy") not in {"hard", "estimate"}:
                errors.append("invalid:limits.budget.token_policy")

    stop_conditions = doc.get("stop_conditions")
    if not isinstance(stop_conditions, Mapping):
        errors.append("invalid:stop_conditions")
    else:
        for key in ("no_progress_runs", "repeated_failure_runs", "budget_stop"):
            if key not in stop_conditions:
                errors.append(f"missing:stop_conditions.{key}")

    kill_switch = doc.get("kill_switch")
    if not isinstance(kill_switch, Mapping):
        errors.append("invalid:kill_switch")
    else:
        if not isinstance(kill_switch.get("file"), str):
            errors.append("invalid:kill_switch.file")
        if not isinstance(kill_switch.get("env"), str):
            errors.append("invalid:kill_switch.env")

    write_policy = doc.get("write_policy")
    if not isinstance(write_policy, Mapping):
        errors.append("invalid:write_policy")
    else:
        if write_policy.get("forbid_watched_writes") is not True:
            errors.append("invalid:write_policy.forbid_watched_writes")
        if not isinstance(write_policy.get("allowed_write_areas"), list):
            errors.append("invalid:write_policy.allowed_write_areas")
        if not isinstance(write_policy.get("path_claims"), list):
            errors.append("invalid:write_policy.path_claims")
    return errors


__all__ = [
    "SENSITIVE_ENV_KEYS",
    "SecurityError",
    "PathEscapeError",
    "PathClaimCollisionError",
    "BudgetError",
    "CircuitOpenError",
    "L1WriteViolation",
    "KillSwitchActive",
    "resolve_under_root",
    "assert_no_reparse_chain",
    "assert_paths_under_root",
    "normalize_claim",
    "claims_overlap",
    "PathClaimRegistry",
    "BudgetLedger",
    "CircuitBreaker",
    "scrub_environment",
    "assert_no_secrets_in_text",
    "loop_state_root",
    "assert_l1_write_allowed",
    "default_verifier_result",
    "atomic_write_json",
    "append_jsonl",
    "LoopLock",
    "new_run_id",
    "validate_loop_json",
]
