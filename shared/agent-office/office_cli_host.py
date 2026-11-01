"""Uninstalled B5 Windows host candidate: one bounded native dispatch poll.

The owner pins SSH transport, actor and native sessions in local configuration.
This entrypoint neither installs a service nor schedules another poll.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

from coordfs.cli_host import HostEnrollment, HostExecutor, _checked_path
from coordfs.errors import CoordError
from coordfs.office_cli_remote import CliHostRemoteAdapter, load_remote_config
from coordfs.office_remote import OfficeRemoteClient
from coordfs.worker_profile import load_worker_profiles
from coordfs.native_model_evidence import NativeModelObserver
from coordfs.native_process_snapshot import classify_codex_process, reduce_snapshot


_IDLE_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
try {
    $name = $env:OFFICE_CLI_DRIVER_EXE
    $target = $env:OFFICE_CLI_NATIVE_ID
    $exe = $env:OFFICE_CLI_EXECUTABLE
    $items = @(Get-CimInstance Win32_Process -Filter "Name='$name'" -ErrorAction Stop)
    $pinned = @()
    foreach ($item in $items) {
        if ([string]::IsNullOrEmpty($item.ExecutablePath) -or
            [string]::IsNullOrEmpty($item.CommandLine)) { 'unknown'; exit 0 }
        if ($item.ExecutablePath -ieq $exe) { $pinned += $item.CommandLine }
    }
    if ($name -ieq 'claude.exe') {
        foreach ($command in $pinned) {
            $pattern = '(?<![A-Za-z0-9._:-])' + [regex]::Escape($target) + '(?![A-Za-z0-9._:-])'
            if ($command -match $pattern) { 'busy'; exit 0 }
        }
        if ($pinned.Count -gt 0) { 'unknown'; exit 0 }
        'clear'; exit 0
    }
    if ($pinned.Count -gt 64) { 'unknown'; exit 0 }
    $json = ConvertTo-Json -InputObject @($pinned) -Compress -Depth 2
    if ([Text.Encoding]::UTF8.GetByteCount($json) -gt 32768) { 'unknown'; exit 0 }
    $json
} catch { 'unknown' }
"""


def classify_native_processes(
    rows: Iterable[Mapping[str, Any]], *, target: str, executable: str,
    terminal_evidence: bool = False,
) -> str:
    """Classify a successfully enumerated snapshot and observed terminal proof."""
    pinned: list[str] = []
    is_claude = str(executable).replace("/", "\\").rsplit("\\", 1)[-1].lower() == "claude.exe"
    for row in rows:
        path = row.get("ExecutablePath")
        command = row.get("CommandLine")
        if is_claude:
            if not path or not command:
                return "unknown"
            if str(path).lower() == str(executable).lower():
                pinned.append(str(command))
        else:
            if not isinstance(path, str) or not path or \
                    not isinstance(command, str) or not command:
                return "unknown"
            if path.lower() == str(executable).lower():
                pinned.append(command)
    if is_claude:
        token = re.compile(
            r"(?<![A-Za-z0-9._:-])" + re.escape(target) + r"(?![A-Za-z0-9._:-])"
        )
        for command in pinned:
            if token.search(command):
                return "busy"
        if pinned:
            return "unknown"
    else:
        if len(pinned) > 64 or len(json.dumps(pinned).encode("utf-8")) > 32 * 1024:
            return "unknown"
        snapshot = reduce_snapshot(classify_codex_process(
            command, target_native_id=target) for command in pinned)
        if snapshot == "busy":
            return "busy"
        if snapshot != "clear":
            return "unknown"
    return "idle" if terminal_evidence is True else "unknown"


_EVIDENCE_FIELDS = {"binding_id", "native_thread_id", "driver", "workspace", "journal_path"}
_CODEX_BOOKKEEPING = {"item_started", "item_completed", "item_updated", "token_count",
                      "thread_settings_applied"}
_CLAUDE_BOOKKEEPING = {"attachment", "queue-operation", "last-prompt", "cost-state",
                       "atis-latch", "mode"}


def _fingerprint(info: os.stat_result) -> tuple[int, int, int, int]:
    # Windows path stat and handle fstat can disagree on st_ctime_ns for the
    # same file; file id, size and modification time are stable across both.
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def load_native_evidence(path: Path | None,
                         enrollments: list[HostEnrollment]) -> dict[str, Path]:
    """Load only host-pinned native journal paths; reject the whole invalid file."""
    if path is None:
        return {}
    _checked_path(path, kind="file")
    before = path.lstat()
    if before.st_size > 32 * 1024:
        raise CoordError("native evidence too large", code="invalid_native_evidence")
    try:
        with path.open("rb") as stream:
            data = stream.read(32 * 1024 + 1)
        _checked_path(path, kind="file")
        if _fingerprint(path.lstat()) != _fingerprint(before) or len(data) != before.st_size:
            raise ValueError("native evidence changed")
        value = json.loads(data.decode("utf-8"))
        if not isinstance(value, dict) or set(value) != {"schema_version", "sessions"} or \
                type(value["schema_version"]) is not int or value["schema_version"] != 1 or \
                not isinstance(value["sessions"], list) or len(value["sessions"]) > 20:
            raise ValueError("invalid native evidence schema")
        enrolled = {row.binding_id: row for row in enrollments}
        if len(enrolled) != len(enrollments):
            raise ValueError("duplicate enrollment")
        result: dict[str, Path] = {}
        native_ids: set[tuple[str, str]] = set()
        for row in value["sessions"]:
            if not isinstance(row, dict) or set(row) != _EVIDENCE_FIELDS or \
                    any(not isinstance(item, str) or not item for item in row.values()):
                raise ValueError("invalid native evidence row")
            owner = enrolled.get(row["binding_id"])
            if owner is None or row["binding_id"] in result or \
                    (row["driver"], row["native_thread_id"]) in native_ids or \
                    (row["native_thread_id"], row["driver"], row["workspace"]) != \
                    (owner.native_thread_id, owner.driver, str(owner.workspace)):
                raise ValueError("foreign or duplicate native evidence")
            journal = Path(row["journal_path"])
            _checked_path(journal, kind="file")
            if journal.suffix.lower() != ".jsonl" or \
                    (owner.driver == "claude-code-cli" and
                     journal.name != owner.native_thread_id + ".jsonl") or \
                    (owner.driver == "codex-cli" and
                     not journal.name.endswith("-" + owner.native_thread_id + ".jsonl")):
                raise ValueError("foreign native journal name")
            result[owner.binding_id] = journal
            native_ids.add((owner.driver, owner.native_thread_id))
        return result
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise CoordError("invalid native evidence", code="invalid_native_evidence") from exc


def _journal_records(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """Read complete records from bounded head/tail; reject replacement and torn writes."""
    _checked_path(path, kind="file")
    before = path.lstat()
    try:
        with path.open("rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode) or \
                    _fingerprint(os.fstat(stream.fileno())) != _fingerprint(before):
                raise ValueError("journal replaced")
            size = before.st_size
            head = stream.read(min(size, 64 * 1024))
            gap = size > 64 * 1024 + 256 * 1024
            if size <= 64 * 1024:
                tail = b""
            elif gap:
                stream.seek(size - 256 * 1024)
                tail = stream.read(256 * 1024)
            else:
                tail = stream.read(size - len(head))
            if _fingerprint(os.fstat(stream.fileno())) != _fingerprint(before):
                raise ValueError("journal changed")
        _checked_path(path, kind="file")
        if _fingerprint(path.lstat()) != _fingerprint(before):
            raise ValueError("journal replaced")
        if size == 0 or (tail or head)[-1:] != b"\n":
            raise ValueError("incomplete journal")
        if gap:
            if not head.endswith(b"\n"):
                head = head[:head.rfind(b"\n") + 1]
            boundary = tail.find(b"\n")
            if boundary < 0:
                raise ValueError("oversized journal record")
            tail = tail[boundary + 1:]
        elif tail:
            # The contiguous head and tail form one record stream.
            head += tail
            tail = b""

        def decode(chunk: bytes) -> list[dict[str, Any]]:
            rows = []
            for line in chunk.splitlines():
                if len(line) > 256 * 1024:
                    raise ValueError("oversized journal record")
                item = json.loads(line.decode("utf-8"))
                if not isinstance(item, dict):
                    raise ValueError("invalid journal record")
                rows.append(item)
            return rows

        return decode(head), decode(tail), gap
    except (OSError, UnicodeError, ValueError, CoordError) as exc:
        raise CoordError("native journal unavailable", code="invalid_native_evidence") from exc


def _codex_terminal(head: list[dict[str, Any]], tail: list[dict[str, Any]],
                    gap: bool, enrollment: HostEnrollment) -> bool:
    metas = [row.get("payload") for row in head if row.get("type") == "session_meta"]
    expected = (enrollment.native_thread_id, str(enrollment.workspace), "exec")
    if not metas or any(not isinstance(meta, dict) or
                        (meta.get("id"), meta.get("cwd"), meta.get("source")) != expected
                        for meta in metas):
        return False
    if gap:
        for row in head:
            if row.get("type") != "event_msg":
                continue
            payload = row.get("payload")
            if not isinstance(payload, dict) or payload.get("type") not in \
                    (_CODEX_BOOKKEEPING | {"task_started", "task_complete"}):
                return False
            if payload.get("type") in ("task_started", "task_complete") and \
                    (not isinstance(payload.get("turn_id"), str) or
                     not payload["turn_id"]):
                return False
    active: str | None = None
    terminal = False
    seen_start = False
    prefix_completion: str | None = None
    for row in (tail if gap else head):
        kind = row.get("type")
        payload = row.get("payload")
        if kind == "session_meta":
            if not isinstance(payload, dict) or \
                    (payload.get("id"), payload.get("cwd"), payload.get("source")) != \
                    (enrollment.native_thread_id, str(enrollment.workspace), "exec"):
                return False
        elif kind == "event_msg":
            if not isinstance(payload, dict):
                return False
            event = payload.get("type")
            if event == "task_started":
                turn = payload.get("turn_id")
                if not isinstance(turn, str) or not turn or active is not None or \
                        turn == prefix_completion:
                    return False
                seen_start = True
                active, terminal = turn, False
            elif event == "task_complete":
                turn = payload.get("turn_id")
                if not isinstance(turn, str) or not turn:
                    return False
                if active is None:
                    # A bounded tail may begin inside one older turn. Its
                    # completion is not proof; only a later fully visible pair
                    # can establish terminal state. Contiguous or malformed
                    # lifecycle records must never take this exception.
                    if gap and not seen_start and prefix_completion is None:
                        prefix_completion = turn
                        continue
                    return False
                if turn != active:
                    return False
                active, terminal = None, True
            elif event not in _CODEX_BOOKKEEPING:
                return False
        elif kind not in {"turn_context", "response_item", "token_usage_record", "world_state"}:
            return False
    return terminal and active is None


def _claude_terminal(head: list[dict[str, Any]], tail: list[dict[str, Any]],
                     gap: bool, enrollment: HostEnrollment) -> bool:
    head_turns = [row for row in head if row.get("type") in ("user", "assistant")]
    if gap and (not head_turns or any(
            row.get("sessionId") != enrollment.native_thread_id or
            row.get("cwd") != str(enrollment.workspace) or
            not isinstance(row.get("message"), dict) or
            row["message"].get("role") != row["type"] for row in head_turns)):
        return False
    if gap and any(row.get("type") not in (_CLAUDE_BOOKKEEPING | {"user", "assistant"})
                   for row in head):
        return False
    terminal = False
    for row in (tail if gap else head):
        kind = row.get("type")
        if kind in ("user", "assistant"):
            message = row.get("message")
            if row.get("sessionId") != enrollment.native_thread_id or \
                    row.get("cwd") != str(enrollment.workspace) or \
                    not isinstance(message, dict) or message.get("role") != kind:
                return False
            content = message.get("content")
            if kind == "assistant":
                if not isinstance(content, list):
                    return False
                tool_use = any(
                    isinstance(part, dict) and part.get("type") == "tool_use"
                    for part in content
                )
                if tool_use and message.get("stop_reason") == "end_turn":
                    return False
                terminal = message.get("stop_reason") == "end_turn" and not tool_use
            else:
                terminal = False
        elif kind not in _CLAUDE_BOOKKEEPING:
            return False
    return terminal


def native_terminal_observed(enrollment: HostEnrollment, journal: Path | None) -> bool:
    if journal is None:
        return False
    try:
        head, tail, gap = _journal_records(journal)
        if enrollment.driver == "codex-cli":
            return _codex_terminal(head, tail, gap, enrollment)
        if enrollment.driver == "claude-code-cli":
            return _claude_terminal(head, tail, gap, enrollment)
    except (CoordError, OSError, ValueError):
        pass
    return False


def owned_session_idle(enrollment: HostEnrollment, *,
                       evidence: Mapping[str, Path] | None = None) -> bool | None:
    """Require observed native terminal metadata and a clear process query."""
    if os.name != "nt":
        return None
    child_env = dict(os.environ)
    child_env.update(OFFICE_CLI_DRIVER_EXE=("codex.exe" if enrollment.driver == "codex-cli"
                                              else "claude.exe"),
                     OFFICE_CLI_NATIVE_ID=enrollment.native_thread_id,
                     OFFICE_CLI_EXECUTABLE=str(enrollment.executable))
    child_env.pop("OFFICE_CLI_IDLE_PROOF", None)
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _IDLE_SCRIPT],
            shell=False, env=child_env, capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
        )
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not isinstance(result.stdout, str):
        return None
    output = result.stdout.strip()
    if enrollment.driver == "claude-code-cli":
        if output not in ("clear", "busy"):
            return None
        if output == "busy":
            return False
    else:
        try:
            if len(result.stdout.encode("utf-8")) > 32 * 1024:
                return None
            pinned = json.loads(output)
        except (TypeError, UnicodeError, ValueError):
            return None
        if not isinstance(pinned, list) or len(pinned) > 64 or any(
                not isinstance(command, str) or not command for command in pinned):
            return None
        snapshot = reduce_snapshot(classify_codex_process(
            command, target_native_id=enrollment.native_thread_id) for command in pinned)
        if snapshot == "busy":
            return False
        if snapshot != "clear":
            return None
    journal = evidence.get(enrollment.binding_id) if evidence else None
    return True if native_terminal_observed(enrollment, journal) else None


def load_enrollments(path: Path, *, execution_host: str,
                     executor_agent: str) -> list[HostEnrollment]:
    _checked_path(path, kind="file")
    if path.stat().st_size > 32 * 1024:
        raise CoordError("enrollment file too large", code="invalid_enrollment")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise CoordError("invalid enrollment file", code="invalid_enrollment") from exc
    fields = {"binding_id", "binding_version", "execution_host", "executor_agent",
              "driver", "native_thread_id", "workspace", "executable"}
    if not isinstance(value, list) or not 1 <= len(value) <= 20:
        raise CoordError("invalid enrollment list", code="invalid_enrollment")
    enrollments: list[HostEnrollment] = []
    for row in value:
        if not isinstance(row, dict) or set(row) != fields or \
                row["execution_host"] != execution_host or row["executor_agent"] != executor_agent:
            raise CoordError("enrollment does not match pinned actor", code="invalid_enrollment")
        enrollments.append(HostEnrollment(**row))
    return enrollments


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="office-cli-host")
    parser.add_argument("--remote-config", required=True, type=Path)
    parser.add_argument("--enrollments", required=True, type=Path)
    parser.add_argument("--worker-profiles", required=True, type=Path)
    parser.add_argument("--native-evidence", type=Path)
    parser.add_argument("--journal-home", required=True, type=Path)
    parser.add_argument("--execution-host", required=True)
    parser.add_argument("--executor-agent", required=True)
    parser.add_argument("--harness", required=True)
    parser.add_argument("--session-id", required=True)
    args = parser.parse_args(argv)
    try:
        if os.name != "nt":
            raise CoordError("native host requires Windows", code="unsupported_host")
        config = load_remote_config(args.remote_config)
        adapter = CliHostRemoteAdapter(
            OfficeRemoteClient(config), execution_host=args.execution_host,
            executor_agent=args.executor_agent, harness=args.harness,
            session_id=args.session_id,
        )
        enrollments = load_enrollments(args.enrollments, execution_host=args.execution_host,
                                       executor_agent=args.executor_agent)
        profiles = load_worker_profiles(args.worker_profiles, enrollments)
        evidence = load_native_evidence(args.native_evidence, enrollments)
        observer = NativeModelObserver(evidence)

        def idle_probe(item: HostEnrollment) -> bool | None:
            state = owned_session_idle(item, evidence=evidence)
            if state is not True:
                return state
            # HostExecutor calls this while holding the owned-session fence,
            # before claim and again before start. Never arm an active session.
            return True if observer.arm(item) else None

        outcomes = HostExecutor(enrollments, journal_home=args.journal_home, remote=adapter,
                                profiles=profiles,
                                idle_probe=idle_probe,
                                observed_model=observer.observe).run_once()
        print(json.dumps({"status": "ok", "outcomes": outcomes}, separators=(",", ":")))
        return 0
    except (CoordError, ValueError, OSError):
        print(json.dumps({"status": "error", "code": "cli_host_refused"}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
