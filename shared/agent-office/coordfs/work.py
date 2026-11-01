"""Small, local lifecycle ledger for active coordination work.

The ledger records intent, blockers, and scoped integration handoffs beneath a
coordfs home.  It deliberately is *not* an authority engine: it never invokes
Git, changes a checkout, claims or releases a lease, retries a conflict, or
changes permissions.  A later integration caller can use the returned records
as evidence, but remains responsible for every real-world action.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .atomic import exclusive_create, exclusive_mutex, replace_with_backoff, unlink_with_backoff
from .checkout_claims import checkout_claim, claim_key, local_host as detected_local_host, validate_claim
from .errors import CoordError
from .identity import AGENT_PREFIX, canonicalize_agent, canonicalize_resource
from .office import detect_secret
from .leases import format_ts
from .store import ensure_home


WORK_DIRNAME = "work"
WORK_SCHEMA_VERSION = 2
LEGACY_WORK_SCHEMA_VERSION = 1
WORK_STATUSES = frozenset({"active", "blocked", "deferred", "ready", "complete"})
BLOCKER_STATUSES = frozenset({"open", "deferred", "resolved"})
PLAN_STATUSES = frozenset({"proposed", "accepted", "needs_changes", "declined", "cancelled"})
_PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}\Z")
_TASK_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,191}\Z")
_SHA_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
_UNSET = object()

NowFn = Callable[[], float]


def _text(value: Any, label: str, *, maximum: int = 2000) -> str:
    if not isinstance(value, str):
        raise CoordError(f"{label} must be a string", code=f"invalid_{label}")
    text = unicodedata.normalize("NFC", value.strip())
    if not text or len(text) > maximum or "\x00" in text or any(ord(char) < 32 for char in text):
        raise CoordError(f"{label} must be 1..{maximum} printable characters", code=f"invalid_{label}")
    secret = detect_secret(text)
    if secret is not None:
        raise CoordError(
            f"{label} matches a high-confidence secret pattern ({secret}); refused before write",
            code="secret_detected",
        )
    return text


def _identifier(value: Any, label: str, pattern: re.Pattern[str]) -> str:
    text = _text(value, label, maximum=192).lower()
    if not pattern.match(text):
        raise CoordError(f"{label} must use lowercase letters, digits, dots, underscores, or dashes", code=f"invalid_{label}")
    return text


def _project(value: Any) -> str:
    return _identifier(value, "project", _PROJECT_RE)


def _task_id(value: Any) -> str:
    return _identifier(value, "task_id", _TASK_RE)


def _reason(value: Any, label: str = "reason") -> str:
    return _text(value, label, maximum=2000)


def _checkout(value: Any) -> str:
    if not isinstance(value, (str, Path)):
        raise CoordError("checkout must be an absolute existing directory", code="invalid_checkout")
    raw = Path(str(value)).expanduser()
    if not raw.is_absolute() or not raw.is_dir():
        raise CoordError("checkout must be an absolute existing directory", code="invalid_checkout")
    try:
        return canonicalize_resource(str(raw), kind="path")
    except CoordError as exc:
        raise CoordError(str(exc), code="invalid_checkout") from exc


def _scopes(value: Any) -> list[str]:
    if isinstance(value, str):
        source: Sequence[Any] = [value]
    elif isinstance(value, Sequence):
        source = value
    else:
        raise CoordError("scopes must be a non-empty sequence", code="invalid_scope")
    normalized: list[str] = []
    for item in source:
        if not isinstance(item, str):
            raise CoordError("each scope must be a string", code="invalid_scope")
        try:
            scope = canonicalize_resource(item, kind="scope")
        except CoordError as exc:
            raise CoordError(str(exc), code="invalid_scope") from exc
        if scope not in normalized:
            normalized.append(scope)
    if not normalized:
        raise CoordError("scopes must be non-empty", code="invalid_scope")
    return normalized


def _dependencies(value: Any, task_id: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        source: Sequence[Any] = [value]
    elif isinstance(value, Sequence):
        source = value
    else:
        raise CoordError("dependencies must be a sequence", code="invalid_dependency")
    normalized: list[str] = []
    for item in source:
        dependency = _task_id(item)
        if dependency == task_id:
            raise CoordError("a task cannot depend on itself", code="invalid_dependency")
        if dependency not in normalized:
            normalized.append(dependency)
    return normalized


def _journal_refs(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        source: Sequence[Any] = [value]
    elif isinstance(value, Sequence):
        source = value
    else:
        raise CoordError("journal_refs must be a sequence", code="invalid_journal_ref")
    normalized: list[str] = []
    for item in source:
        reference = _text(item, "journal_ref", maximum=512)
        if reference not in normalized:
            normalized.append(reference)
    return normalized


def _unknown_reasons(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise CoordError("unknown_reasons must be an object", code="invalid_unknown_reason")
    normalized: dict[str, str] = {}
    for key, reason in value.items():
        if not isinstance(key, str) or not _TASK_RE.match(key.lower()):
            raise CoordError("unknown reason keys must be safe identifiers", code="invalid_unknown_reason")
        normalized[key.lower()] = _reason(reason, "unknown_reason")
    return normalized


def _base_sha(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _SHA_RE.match(value):
        raise CoordError("base_sha must be a 40- or 64-character hexadecimal digest", code="invalid_base_sha")
    return value.lower()


def _commit_intent(value: Any) -> str | None:
    if value is None:
        return None
    return _text(value, "commit_intent", maximum=2000)


def _status(value: Any, *, blocker: bool = False) -> str:
    allowed = BLOCKER_STATUSES if blocker else WORK_STATUSES
    if not isinstance(value, str) or value not in allowed:
        label = "blocker_status" if blocker else "status"
        raise CoordError(f"{label} must be one of {', '.join(sorted(allowed))}", code=f"invalid_{label}")
    return value


def _agent(value: Any) -> str:
    try:
        return canonicalize_agent(_text(value, "agent_id", maximum=400))[len(AGENT_PREFIX) :]
    except CoordError as exc:
        raise CoordError(str(exc), code="invalid_agent") from exc


def _scope_covers(bound: str, proposed: str) -> bool:
    return proposed.startswith(bound) if bound.endswith("/") else proposed == bound


class WorkBoard:
    """Persist active-work coordination records with immutable project/checkout/scope bindings.

    All state changes use a local O_EXCL mutex.  Records describe collaboration
    only; this class never opens a network listener, performs a VCS operation,
    switches a checkout, or treats a lifecycle status as permission to act.
    """

    def __init__(
        self,
        home: Path | None = None,
        now: NowFn | None = None,
        local_host: str | None = None,
        allow_native_checkout: bool = True,
    ) -> None:
        self.home = ensure_home(home)
        self._now = now
        self.allow_native_checkout = allow_native_checkout
        # This verifies an injected test/server host with the same primitive
        # used for persisted claims without touching a checkout path.
        self.local_host = checkout_claim(
            "/", host=detected_local_host() if local_host is None else local_host, path_style="posix"
        )["host"]

    def _timestamp(self) -> str:
        import time

        return format_ts(float(self._now() if self._now is not None else time.time()))

    def _directory(self) -> Path:
        path = self.home / WORK_DIRNAME
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _digest(project: str, task_id: str) -> str:
        return hashlib.sha256(f"{project}\0{task_id}".encode("utf-8")).hexdigest()

    def _paths(self, project: str, task_id: str) -> tuple[Path, Path]:
        directory = self._directory()
        stem = self._digest(project, task_id)
        return directory / f"{stem}.json", directory / f"{stem}.mutex"

    @staticmethod
    def _validate_record(record: Any, project: str, task_id: str) -> dict[str, Any]:
        if not isinstance(record, dict) or record.get("project") != project or record.get("task_id") != task_id:
            raise CoordError("active work record does not match its binding", code="invalid_record")
        schema = record.get("schema_version")
        if schema not in {LEGACY_WORK_SCHEMA_VERSION, WORK_SCHEMA_VERSION}:
            raise CoordError("active work record has an unsupported schema", code="invalid_record")
        if schema == WORK_SCHEMA_VERSION:
            try:
                validate_claim(record.get("checkout_claim"))
            except CoordError as exc:
                raise CoordError("active work record has an invalid checkout claim", code="invalid_record") from exc
        return record

    def _read_locked(self, path: Path, project: str, task_id: str) -> dict[str, Any]:
        try:
            raw = path.read_text(encoding="utf-8")
            record = json.loads(raw)
        except FileNotFoundError as exc:
            raise CoordError("active work record was not found", code="not_found") from exc
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CoordError("active work record is unreadable", code="invalid_record") from exc
        return self._validate_record(record, project, task_id)

    def _write_locked(self, path: Path, record: dict[str, Any]) -> None:
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        temp = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex}")
        try:
            exclusive_create(temp, payload)
            if not replace_with_backoff(temp, path):
                raise CoordError("active work record remained busy", code="busy")
        finally:
            if temp.exists():
                try:
                    unlink_with_backoff(temp)
                except OSError:
                    pass

    def _checkout_binding(self, checkout: Any, checkout_host: str | None) -> tuple[str, dict[str, str] | None]:
        """Return legacy provenance plus an optional host-qualified claim.

        Omitting the host deliberately retains the original direct API: it
        validates a native existing directory and stores schema 1 provenance.
        An explicit foreign host or a remote transport is lexical only and must
        not inspect a path the local Office server does not own.
        """
        if checkout_host is None:
            return _checkout(checkout), None
        raw = str(checkout) if isinstance(checkout, (str, Path)) else checkout
        claim = checkout_claim(raw, host=checkout_host)
        if self.allow_native_checkout and claim["host"] == self.local_host:
            return _checkout(checkout), claim
        return raw, claim

    def _assert_binding(
        self,
        record: Mapping[str, Any],
        checkout: Any,
        checkout_host: str | None,
        scopes: Any,
    ) -> None:
        if checkout is not _UNSET:
            checkout_id, proposed_claim = self._checkout_binding(checkout, checkout_host)
            if proposed_claim is None:
                # Schema 2 has a pinned host-qualified identity.  A legacy
                # direct call has no caller-host component, so it cannot
                # prove equality with that identity merely by reusing the
                # provenance string.
                matches = (
                    record.get("schema_version") == LEGACY_WORK_SCHEMA_VERSION
                    and checkout_id == record.get("checkout")
                )
            elif record.get("schema_version") == LEGACY_WORK_SCHEMA_VERSION:
                raise CoordError("legacy checkout requires explicit migration", code="migration_required")
            else:
                matches = claim_key(proposed_claim) == claim_key(record.get("checkout_claim"))
            if not matches:
                raise CoordError("checkout binding cannot change after registration", code="binding_mismatch")
        if scopes is not _UNSET and _scopes(scopes) != record.get("scopes"):
            raise CoordError("scope binding cannot change after registration", code="binding_mismatch")

    @staticmethod
    def _assert_unknown_reasons(base_sha: str | None, commit_intent: str | None, reasons: Mapping[str, str]) -> None:
        if base_sha is None and not reasons.get("base_sha"):
            raise CoordError("base_sha is unknown without an explicit reason", code="missing_unknown_reason")
        if commit_intent is None and not reasons.get("commit_intent"):
            raise CoordError("commit_intent is unknown without an explicit reason", code="missing_unknown_reason")

    @staticmethod
    def _transition_allowed(current: str, target: str) -> bool:
        allowed = {
            "active": {"active", "blocked", "deferred", "ready"},
            "blocked": {"active", "blocked", "deferred", "ready"},
            "deferred": {"active", "blocked", "deferred"},
            "ready": {"active", "blocked", "ready", "complete"},
            "complete": {"complete"},
        }
        return target in allowed.get(current, set())

    def register_task(
        self,
        *,
        project: str,
        task_id: str,
        checkout: str | Path,
        scopes: Sequence[str] | str,
        checkout_host: str | None = None,
        dependencies: Sequence[str] | str | None = None,
        base_sha: str | None = None,
        commit_intent: str | None = None,
        journal_refs: Sequence[str] | str | None = None,
        status: str = "active",
        unknown_reasons: Mapping[str, str] | None = None,
        deferred_reason: str | None = None,
    ) -> dict[str, Any]:
        """Register one task; bindings are immutable and missing fields need explicit reasons."""
        project_id = _project(project)
        task = _task_id(task_id)
        checkout_id, checkout_claim_id = self._checkout_binding(checkout, checkout_host)
        scope_ids = _scopes(scopes)
        reason_map = _unknown_reasons(unknown_reasons)
        sha = _base_sha(base_sha)
        intent = _commit_intent(commit_intent)
        self._assert_unknown_reasons(sha, intent, reason_map)
        work_status = _status(status)
        deferred = _reason(deferred_reason, "deferred_reason") if deferred_reason is not None else None
        if work_status == "deferred" and not deferred:
            raise CoordError("deferred status requires deferred_reason", code="missing_deferred_reason")
        path, mutex = self._paths(project_id, task)
        now = self._timestamp()
        record: dict[str, Any] = {
            "schema_version": WORK_SCHEMA_VERSION if checkout_claim_id is not None else LEGACY_WORK_SCHEMA_VERSION,
            "project": project_id,
            "task_id": task,
            "checkout": checkout_id,
            "scopes": scope_ids,
            "dependencies": _dependencies(dependencies, task),
            "base_sha": sha,
            "commit_intent": intent,
            "journal_refs": _journal_refs(journal_refs),
            "unknown_reasons": reason_map,
            "status": work_status,
            "deferred_reason": deferred if work_status == "deferred" else None,
            "blockers": [],
            "integration_plans": [],
            "created_at": now,
            "updated_at": now,
            "revision": 1,
        }
        if checkout_claim_id is not None:
            record["checkout_claim"] = checkout_claim_id
        with exclusive_mutex(mutex):
            if path.exists():
                raise CoordError("active work record is already registered", code="already_registered")
            self._write_locked(path, record)
        return {"status": "ok", "code": "registered", "task": record}

    register = register_task

    def get_task(self, *, project: str, task_id: str) -> dict[str, Any]:
        """Read one coordination record without interpreting it as authority."""
        project_id = _project(project)
        task = _task_id(task_id)
        path, mutex = self._paths(project_id, task)
        with exclusive_mutex(mutex):
            record = self._read_locked(path, project_id, task)
        return {"status": "ok", "code": "found", "task": record}

    def list_tasks(
        self,
        *,
        project: str | None = None,
        checkout: str | Path | None = None,
        checkout_host: str | None = None,
    ) -> dict[str, Any]:
        """List readable records; corrupt foreign files are visible as warnings, never repaired."""
        project_id = _project(project) if project is not None else None
        checkout_id: str | None = None
        checkout_claim_id: dict[str, str] | None = None
        if checkout is not None:
            checkout_id, checkout_claim_id = self._checkout_binding(checkout, checkout_host)
        tasks: list[dict[str, Any]] = []
        warnings: list[dict[str, str]] = []
        for path in sorted(self._directory().glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                warnings.append({"code": "invalid_record", "record": path.name})
                continue
            if not isinstance(record, dict) or record.get("schema_version") not in {
                LEGACY_WORK_SCHEMA_VERSION,
                WORK_SCHEMA_VERSION,
            }:
                warnings.append({"code": "invalid_record", "record": path.name})
                continue
            if record.get("schema_version") == WORK_SCHEMA_VERSION:
                try:
                    validate_claim(record.get("checkout_claim"))
                except CoordError:
                    warnings.append({"code": "invalid_record", "record": path.name})
                    continue
            if project_id is not None and record.get("project") != project_id:
                continue
            if checkout_id is not None:
                if checkout_claim_id is None:
                    # Keep the original direct API available for schema 1
                    # records only.  A schema 2 filter must compare the full
                    # claim key; its legacy checkout string is provenance,
                    # not an unqualified ownership key.
                    if (
                        record.get("schema_version") != LEGACY_WORK_SCHEMA_VERSION
                        or record.get("checkout") != checkout_id
                    ):
                        continue
                elif record.get("schema_version") == LEGACY_WORK_SCHEMA_VERSION:
                    # A schema 1 string has no host ownership information.  It
                    # cannot satisfy a host-qualified filter until migration.
                    warnings.append({"code": "migration_required", "record": path.name})
                    continue
                elif claim_key(record.get("checkout_claim")) != claim_key(checkout_claim_id):
                    continue
            tasks.append(record)
        tasks.sort(key=lambda item: (str(item.get("project", "")), str(item.get("task_id", ""))))
        return {"status": "ok", "code": "listed", "tasks": tasks, "count": len(tasks), "warnings": warnings}

    list = list_tasks

    def update_task(
        self,
        *,
        project: str,
        task_id: str,
        checkout: str | Path | object = _UNSET,
        checkout_host: str | None = None,
        scopes: Sequence[str] | str | object = _UNSET,
        dependencies: Sequence[str] | str | None | object = _UNSET,
        base_sha: str | None | object = _UNSET,
        commit_intent: str | None | object = _UNSET,
        journal_refs: Sequence[str] | str | None | object = _UNSET,
        status: str | object = _UNSET,
        unknown_reasons: Mapping[str, str] | None | object = _UNSET,
        deferred_reason: str | None | object = _UNSET,
    ) -> dict[str, Any]:
        """Update mutable lifecycle fields while refusing project/checkout/scope drift."""
        project_id = _project(project)
        task = _task_id(task_id)
        path, mutex = self._paths(project_id, task)
        with exclusive_mutex(mutex):
            record = self._read_locked(path, project_id, task)
            self._assert_binding(record, checkout, checkout_host, scopes)
            reasons = dict(record.get("unknown_reasons", {}))
            if unknown_reasons is not _UNSET:
                reasons = _unknown_reasons(unknown_reasons)
            next_sha = record.get("base_sha") if base_sha is _UNSET else _base_sha(base_sha)
            next_intent = record.get("commit_intent") if commit_intent is _UNSET else _commit_intent(commit_intent)
            if next_sha is not None:
                reasons.pop("base_sha", None)
            if next_intent is not None:
                reasons.pop("commit_intent", None)
            self._assert_unknown_reasons(next_sha, next_intent, reasons)
            next_status = record.get("status") if status is _UNSET else _status(status)
            current_status = str(record.get("status", ""))
            if not self._transition_allowed(current_status, next_status):
                raise CoordError(f"cannot transition from {current_status} to {next_status}", code="invalid_transition")
            if dependencies is not _UNSET:
                record["dependencies"] = _dependencies(dependencies, task)
            if journal_refs is not _UNSET:
                record["journal_refs"] = _journal_refs(journal_refs)
            if next_status == "deferred":
                deferred = record.get("deferred_reason") if deferred_reason is _UNSET else deferred_reason
                if deferred is None:
                    raise CoordError("deferred status requires deferred_reason", code="missing_deferred_reason")
                record["deferred_reason"] = _reason(deferred, "deferred_reason")
            else:
                record["deferred_reason"] = None
            record["base_sha"] = next_sha
            record["commit_intent"] = next_intent
            record["unknown_reasons"] = reasons
            record["status"] = next_status
            record["updated_at"] = self._timestamp()
            record["revision"] = int(record.get("revision", 0)) + 1
            self._write_locked(path, record)
        return {"status": "ok", "code": "updated", "task": record}

    update = update_task

    def add_blocker(
        self,
        *,
        project: str,
        task_id: str,
        blocker_id: str,
        reason: str,
        dependencies: Sequence[str] | str | None = None,
    ) -> dict[str, Any]:
        """Record an open blocker; it changes only the local lifecycle label to `blocked`."""
        project_id = _project(project)
        task = _task_id(task_id)
        blocker = _task_id(blocker_id)
        path, mutex = self._paths(project_id, task)
        with exclusive_mutex(mutex):
            record = self._read_locked(path, project_id, task)
            if record.get("status") == "complete":
                raise CoordError("cannot add a blocker to a complete task", code="invalid_transition")
            blockers = list(record.get("blockers", []))
            if any(item.get("blocker_id") == blocker for item in blockers if isinstance(item, Mapping)):
                raise CoordError("blocker is already recorded", code="already_exists")
            now = self._timestamp()
            blockers.append(
                {
                    "blocker_id": blocker,
                    "status": "open",
                    "reason": _reason(reason),
                    "dependencies": _dependencies(dependencies, task),
                    "created_at": now,
                    "updated_at": now,
                    "resolution": None,
                }
            )
            record["blockers"] = blockers
            record["status"] = "blocked"
            record["deferred_reason"] = None
            record["updated_at"] = now
            record["revision"] = int(record.get("revision", 0)) + 1
            self._write_locked(path, record)
        return {"status": "ok", "code": "blocker_added", "task": record}

    def transition_blocker(
        self,
        *,
        project: str,
        task_id: str,
        blocker_id: str,
        status: str,
        reason: str,
    ) -> dict[str, Any]:
        """Resolve or defer a blocker using an explicit reason; no external work is attempted."""
        project_id = _project(project)
        task = _task_id(task_id)
        blocker = _task_id(blocker_id)
        target = _status(status, blocker=True)
        path, mutex = self._paths(project_id, task)
        with exclusive_mutex(mutex):
            record = self._read_locked(path, project_id, task)
            blockers = list(record.get("blockers", []))
            selected: dict[str, Any] | None = None
            for item in blockers:
                if isinstance(item, dict) and item.get("blocker_id") == blocker:
                    selected = item
                    break
            if selected is None:
                raise CoordError("blocker was not found", code="not_found")
            current = selected.get("status")
            allowed = {
                "open": {"open", "deferred", "resolved"},
                "deferred": {"open", "deferred", "resolved"},
                "resolved": {"resolved"},
            }
            if target not in allowed.get(current, set()):
                raise CoordError(f"cannot transition blocker from {current} to {target}", code="invalid_transition")
            now = self._timestamp()
            explicit_reason = _reason(reason)
            selected["status"] = target
            selected["reason"] = explicit_reason
            selected["updated_at"] = now
            selected["resolution"] = explicit_reason if target == "resolved" else None
            active_states = {item.get("status") for item in blockers if isinstance(item, Mapping)}
            if "open" in active_states:
                record["status"] = "blocked"
                record["deferred_reason"] = None
            elif "deferred" in active_states:
                record["status"] = "deferred"
                record["deferred_reason"] = explicit_reason
            elif record.get("status") != "complete":
                record["status"] = "active"
                record["deferred_reason"] = None
            record["blockers"] = blockers
            record["updated_at"] = now
            record["revision"] = int(record.get("revision", 0)) + 1
            self._write_locked(path, record)
        return {"status": "ok", "code": "blocker_updated", "task": record}

    def record_integration_plan(
        self,
        *,
        project: str,
        task_id: str,
        from_agent: str,
        to_agent: str,
        scopes: Sequence[str] | str,
        summary: str,
        journal_refs: Sequence[str] | str | None = None,
    ) -> dict[str, Any]:
        """Store a proposed scoped handoff; it neither grants a write nor performs one."""
        project_id = _project(project)
        task = _task_id(task_id)
        path, mutex = self._paths(project_id, task)
        with exclusive_mutex(mutex):
            record = self._read_locked(path, project_id, task)
            if record.get("status") == "complete":
                raise CoordError("cannot add an integration plan to a complete task", code="invalid_transition")
            plan_scopes = _scopes(scopes)
            bindings = record.get("scopes")
            if not isinstance(bindings, list) or not all(
                any(_scope_covers(bound, candidate) for bound in bindings if isinstance(bound, str))
                for candidate in plan_scopes
            ):
                raise CoordError("integration plan scope is outside the task binding", code="invalid_scope_binding")
            now = self._timestamp()
            plan = {
                "plan_id": uuid.uuid4().hex,
                "from_agent": _agent(from_agent),
                "to_agent": _agent(to_agent),
                "scopes": plan_scopes,
                "summary": _text(summary, "summary", maximum=4000),
                "journal_refs": _journal_refs(journal_refs),
                "status": "proposed",
                "created_at": now,
                "updated_at": now,
                "response": None,
            }
            plans = list(record.get("integration_plans", []))
            plans.append(plan)
            record["integration_plans"] = plans
            record["updated_at"] = now
            record["revision"] = int(record.get("revision", 0)) + 1
            self._write_locked(path, record)
        return {"status": "ok", "code": "integration_plan_recorded", "plan": plan, "task": record}

    def list_integration_plans(self, *, project: str, task_id: str) -> dict[str, Any]:
        """Return the durable integration-plan exchange for one task without acting on it."""
        found = self.get_task(project=project, task_id=task_id)
        plans = list(found["task"].get("integration_plans", []))
        return {"status": "ok", "code": "integration_plans_listed", "plans": plans, "count": len(plans)}

    def acknowledge_integration_plan(
        self,
        *,
        project: str,
        task_id: str,
        plan_id: str,
        agent_id: str,
        status: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Record the named recipient's response; acceptance is not authority to integrate."""
        project_id = _project(project)
        task = _task_id(task_id)
        if not isinstance(plan_id, str) or not re.fullmatch(r"[0-9a-f]{32}", plan_id):
            raise CoordError("plan_id must be a 32-character lowercase hexadecimal id", code="invalid_plan_id")
        if status not in PLAN_STATUSES - {"proposed"}:
            raise CoordError("plan response status is invalid", code="invalid_plan_status")
        responder = _agent(agent_id)
        response_note = _text(note, "note", maximum=2000) if note is not None else None
        path, mutex = self._paths(project_id, task)
        with exclusive_mutex(mutex):
            record = self._read_locked(path, project_id, task)
            plans = list(record.get("integration_plans", []))
            selected: dict[str, Any] | None = None
            for plan in plans:
                if isinstance(plan, dict) and plan.get("plan_id") == plan_id:
                    selected = plan
                    break
            if selected is None:
                raise CoordError("integration plan was not found", code="not_found")
            if selected.get("to_agent") != responder:
                raise CoordError("only the named recipient can acknowledge this plan", code="invalid_agent")
            if selected.get("status") != "proposed":
                raise CoordError("integration plan already has a response", code="invalid_transition")
            now = self._timestamp()
            selected["status"] = status
            selected["updated_at"] = now
            selected["response"] = {"agent_id": responder, "status": status, "note": response_note, "at": now}
            record["integration_plans"] = plans
            record["updated_at"] = now
            record["revision"] = int(record.get("revision", 0)) + 1
            self._write_locked(path, record)
        return {"status": "ok", "code": "integration_plan_acknowledged", "plan": selected, "task": record}

    def _legacy_backup_locked(self, path: Path, raw: bytes) -> Path:
        """Snapshot schema 1 bytes without overwriting an earlier recovery copy."""
        for _ in range(16):
            backup = path.with_name(f"{path.stem}.{uuid.uuid4().hex}.legacy-backup")
            try:
                exclusive_create(backup, raw)
            except FileExistsError:
                continue
            return backup
        raise CoordError("could not allocate a unique legacy backup", code="backup_collision")

    def migrate_legacy(self, *, source_host: str) -> dict[str, Any]:
        """Offline-only conversion of schema 1 task bindings to explicit claims.

        ``source_host`` is deliberately supplied by the owner.  The migration
        neither derives it from the checkout spelling nor examines that host's
        filesystem.  Each record keeps its exact legacy ``checkout`` string as
        provenance; its original bytes are durably backed up before replacement.
        """
        source = checkout_claim("/", host=source_host, path_style="posix")["host"]
        backups: list[str] = []
        hashes: list[str] = []
        for path in sorted(self._directory().glob("*.json")):
            # The record filename is the same digest stem used by _paths(), so
            # its sibling mutex is the existing per-task lock even before the
            # JSON can be trusted enough to obtain project/task identifiers.
            mutex = path.with_suffix(".mutex")
            with exclusive_mutex(mutex):
                try:
                    raw = path.read_bytes()
                    record = json.loads(raw.decode("utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise CoordError("active work record is unreadable", code="invalid_record") from exc
                if not isinstance(record, dict):
                    raise CoordError("active work record does not match its binding", code="invalid_record")
                project = _project(record.get("project"))
                task_id = _task_id(record.get("task_id"))
                expected_path, expected_mutex = self._paths(project, task_id)
                if path != expected_path or mutex != expected_mutex:
                    raise CoordError("active work record does not match its binding", code="invalid_record")
                schema = record.get("schema_version")
                if schema == WORK_SCHEMA_VERSION:
                    self._validate_record(record, project, task_id)
                    continue
                if schema != LEGACY_WORK_SCHEMA_VERSION:
                    raise CoordError("active work record has an unsupported schema", code="invalid_record")
                claim = checkout_claim(record.get("checkout"), host=source)
                backup = self._legacy_backup_locked(path, raw)
                converted = dict(record)
                converted["schema_version"] = WORK_SCHEMA_VERSION
                converted["checkout_claim"] = claim
                self._write_locked(path, converted)
                backups.append(backup.name)
                hashes.append(hashlib.sha256(raw).hexdigest())
        return {
            "status": "ok",
            "code": "legacy_migrated",
            "count": len(backups),
            "backups": backups,
            "hashes": hashes,
        }


ActiveWorkBoard = WorkBoard

__all__ = ["ActiveWorkBoard", "WorkBoard", "WORK_SCHEMA_VERSION", "WORK_STATUSES"]
