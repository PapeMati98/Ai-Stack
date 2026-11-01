"""H03 native model evidence: fresh-turn proof for an already owned CLI turn.

The observer captures a metadata-only baseline (arm) before an idle owned
session is resumed, then consumes it one-shot (observe) after the runner
finishes. It returns the model identifier observed on a NEW completed native
turn, or None when proof is missing or ambiguous.

All journal reads reuse the H01 guarded reader and terminal parsers from the
entrypoint module through a lazy import: the parent adds a top-level import of
this module in the entrypoint, so an eager import would be circular. No
prompts, responses, or message content are retained, returned, or exported;
only file identity/size/mtime and turn identifiers are kept between calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:  # pragma: no cover
    from .cli_host import HostEnrollment

__all__ = ["NativeModelObserver"]

_DRIVERS = ("codex-cli", "claude-code-cli")
_MODEL_MAX_CHARS = 128
_BASELINE_ID_CAP = 5000

_helpers_cache: Any = None


def _helpers() -> tuple[Any, ...]:
    """Lazily load the H01 guarded reader/terminal helpers, never a copy."""
    global _helpers_cache
    if _helpers_cache is not None:
        return _helpers_cache
    try:
        from office_cli_host import (
            _checked_path,
            _claude_terminal,
            _codex_terminal,
            _fingerprint,
            _journal_records,
        )
    except ImportError:
        import importlib.util

        candidate = Path(__file__).resolve().parent.parent / "office_cli_host.py"
        spec = importlib.util.spec_from_file_location("office_cli_host", candidate)
        if spec is None or spec.loader is None:  # pragma: no cover
            raise
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _helpers_cache = (
            module._journal_records,
            module._codex_terminal,
            module._claude_terminal,
            module._checked_path,
            module._fingerprint,
        )
    else:
        _helpers_cache = (
            _journal_records,
            _codex_terminal,
            _claude_terminal,
            _checked_path,
            _fingerprint,
        )
    return _helpers_cache


def _valid_model(value: Any) -> bool:
    """Accept only nonempty bounded ASCII identifiers, no whitespace/control."""
    return (
        isinstance(value, str)
        and 1 <= len(value) <= _MODEL_MAX_CHARS
        and all(32 < ord(char) < 127 for char in value)
    )


def _binding_ok(enrollment: Any) -> bool:
    try:
        binding = enrollment.binding_id
        driver = enrollment.driver
        native = enrollment.native_thread_id
        workspace = str(enrollment.workspace)
    except AttributeError:
        return False
    return (
        isinstance(binding, str)
        and bool(binding)
        and driver in _DRIVERS
        and isinstance(native, str)
        and bool(native)
        and bool(workspace)
    )


def _journal_name_ok(enrollment: Any, journal: Path) -> bool:
    if journal.suffix.lower() != ".jsonl":
        return False
    native = enrollment.native_thread_id
    if enrollment.driver == "codex-cli":
        return journal.name.endswith("-" + native + ".jsonl")
    if enrollment.driver == "claude-code-cli":
        return journal.name == native + ".jsonl"
    return False


def _codex_completed(records: list[dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for row in records:
        if row.get("type") != "event_msg":
            continue
        payload = row.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "task_complete":
            continue
        turn = payload.get("turn_id")
        if isinstance(turn, str) and turn:
            out.add(turn)
    return out


def _codex_started(records: list[dict[str, Any]]) -> set[str]:
    """All started turn ids, including turns still active at arm time."""
    out: set[str] = set()
    for row in records:
        if row.get("type") != "event_msg":
            continue
        payload = row.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "task_started":
            continue
        turn = payload.get("turn_id")
        if isinstance(turn, str) and turn:
            out.add(turn)
    return out


def _claude_ids(records: list[dict[str, Any]]) -> tuple[set[str], set[str]]:
    users: set[str] = set()
    assistants: set[str] = set()
    for row in records:
        kind = row.get("type")
        uuid = row.get("uuid")
        if not isinstance(uuid, str) or not uuid:
            continue
        if kind == "user":
            users.add(uuid)
        elif kind == "assistant":
            assistants.add(uuid)
    return users, assistants


def _cap(ids: set[str]) -> frozenset[str]:
    return frozenset(sorted(ids)[:_BASELINE_ID_CAP])


@dataclass(frozen=True)
class _Baseline:
    dev: int
    ino: int
    size: int
    mtime_ns: int
    driver: str
    codex_turns: frozenset[str]
    codex_started: frozenset[str]
    claude_users: frozenset[str]
    claude_assistants: frozenset[str]
    fingerprint: tuple[int, int, int, int]


class NativeModelObserver:
    """One-shot fresh-turn model proof backed by the host-pinned evidence map."""

    def __init__(self, evidence: Mapping[str, Path]) -> None:
        self._evidence = dict(evidence or {})
        self._armed: dict[str, _Baseline] = {}

    def _journal(self, enrollment: Any) -> Path:
        raw = self._evidence[enrollment.binding_id]
        journal = raw if isinstance(raw, Path) else Path(raw)
        if not _journal_name_ok(enrollment, journal):
            raise ValueError("foreign native journal name")
        return journal

    def arm(self, enrollment: HostEnrollment) -> bool:
        """Snapshot metadata-only baseline before the owned session resumes.

        The baseline requires an owned idle/terminal journal: a turn already
        active at arm time must never verify later. A failed arm disarms any
        previous proof for that binding id.
        """
        try:
            key = enrollment.binding_id
        except AttributeError:
            return False
        try:
            if not _binding_ok(enrollment):
                self._armed.pop(key, None)
                return False
            journal = self._journal(enrollment)
            (_journal_records, _codex_terminal, _claude_terminal,
             _checked_path, _fingerprint) = _helpers()
            _checked_path(journal, kind="file")
            before = journal.lstat()
            head, tail, gap = _journal_records(journal)
            if _fingerprint(journal.lstat()) != _fingerprint(before):
                self._armed.pop(key, None)
                return False
            records = head + tail
            if enrollment.driver == "codex-cli":
                if not _codex_terminal(head, tail, gap, enrollment):
                    self._armed.pop(key, None)
                    return False
                baseline = _Baseline(
                    before.st_dev, before.st_ino, before.st_size,
                    before.st_mtime_ns, enrollment.driver,
                    _cap(_codex_completed(records)),
                    _cap(_codex_started(records)),
                    frozenset(), frozenset(),
                    _fingerprint(before),
                )
            else:
                if not _claude_terminal(head, tail, gap, enrollment):
                    self._armed.pop(key, None)
                    return False
                users, assistants = _claude_ids(records)
                baseline = _Baseline(
                    before.st_dev, before.st_ino, before.st_size,
                    before.st_mtime_ns, enrollment.driver,
                    frozenset(), frozenset(),
                    _cap(users), _cap(assistants),
                    _fingerprint(before),
                )
            self._armed[key] = baseline
            return True
        except Exception:
            try:
                self._armed.pop(key, None)
            except Exception:
                pass
            return False

    def observe(self, enrollment: HostEnrollment) -> str | None:
        """Consume the one-shot arm; return the NEW turn's model or None."""
        try:
            baseline = self._armed.pop(enrollment.binding_id, None)
        except AttributeError:
            return None
        if baseline is None or not _binding_ok(enrollment):
            return None
        if baseline.driver != enrollment.driver:
            return None
        try:
            journal = self._journal(enrollment)
            (_journal_records, _codex_terminal, _claude_terminal,
             _checked_path, _fingerprint) = _helpers()
            _checked_path(journal, kind="file")
            before = journal.lstat()
            if (before.st_dev, before.st_ino) != (baseline.dev, baseline.ino):
                return None
            if before.st_size <= baseline.size:
                return None
            head, tail, gap = _journal_records(journal)
            if _fingerprint(journal.lstat()) != _fingerprint(before):
                return None
            scope = tail if gap else head
            if enrollment.driver == "codex-cli":
                if not _codex_terminal(head, tail, gap, enrollment):
                    return None
                return self._codex_model(scope, str(enrollment.workspace),
                                         baseline.codex_turns,
                                         baseline.codex_started)
            if not _claude_terminal(head, tail, gap, enrollment):
                return None
            return self._claude_model(scope, enrollment, baseline)
        except Exception:
            return None

    @staticmethod
    def _codex_model(scope: list[dict[str, Any]], workspace: str,
                     known_completed: frozenset[str],
                     known_started: frozenset[str] | None = None) -> str | None:
        latest: str | None = None
        for row in scope:
            if row.get("type") != "event_msg":
                continue
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue
            if payload.get("type") == "task_complete":
                turn = payload.get("turn_id")
                if isinstance(turn, str) and turn:
                    latest = turn
        if latest is None or latest in known_completed:
            return None
        if known_started is not None and latest in known_started:
            # Started before the arm, completed after: not a new turn.
            return None
        started = False
        models: set[Any] = set()
        for row in scope:
            kind = row.get("type")
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue
            if kind == "event_msg":
                if payload.get("type") == "task_started" and \
                        payload.get("turn_id") == latest:
                    started = True
            elif kind == "turn_context" and payload.get("turn_id") == latest:
                if payload.get("cwd") != workspace:
                    return None
                models.add(payload.get("model"))
        if not started or len(models) != 1:
            return None
        (model,) = models
        return model if _valid_model(model) else None

    @staticmethod
    def _claude_model(scope: list[dict[str, Any]], enrollment: Any,
                      baseline: _Baseline) -> str | None:
        native = enrollment.native_thread_id
        workspace = str(enrollment.workspace)
        users: dict[str, dict[str, Any]] = {}
        for row in scope:
            if row.get("type") == "user":
                uuid = row.get("uuid")
                if isinstance(uuid, str) and uuid:
                    users[uuid] = row
        last: dict[str, Any] | None = None
        for row in scope:
            if row.get("type") != "assistant":
                continue
            message = row.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            if message.get("stop_reason") != "end_turn":
                continue
            if any(isinstance(part, dict) and part.get("type") == "tool_use"
                   for part in content):
                continue
            last = row
        if last is None:
            return None
        uuid_b = last.get("uuid")
        parent = last.get("parentUuid")
        message = last.get("message")
        if (not isinstance(uuid_b, str) or not uuid_b
                or uuid_b in baseline.claude_assistants):
            return None
        if (not isinstance(parent, str) or not parent
                or parent in baseline.claude_users):
            return None
        owner = users.get(parent)
        if owner is None or owner.get("sessionId") != native \
                or owner.get("cwd") != workspace:
            return None
        owner_message = owner.get("message")
        if not isinstance(owner_message, dict) or owner_message.get("role") != "user":
            return None
        if not isinstance(message, dict):
            return None
        provider_id = message.get("id")
        if not isinstance(provider_id, str) or not provider_id:
            return None
        return message.get("model") if _valid_model(message.get("model")) else None
