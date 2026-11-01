"""Synthetic B5 integration checks. No native provider, network or service is started."""

from __future__ import annotations

import hashlib
import io
import json
import time
import uuid
from datetime import datetime
from pathlib import Path

import pytest

from coordfs.errors import CoordError
from coordfs.cli_host import HostEnrollment, HostExecutor
from coordfs.leases import format_ts
from coordfs.office import Office
from coordfs.office_cli_remote import CliHostRemoteAdapter
from coordfs.office_remote import build_request
from coordfs.office_ssh_gate import run
from coordfs.wake import Wake, _host_workspace


TARGET = "codex-host-a-target"
SENDER = "codex-host-a-sender"
EXECUTOR = "codex-host-a-executor"
IDENTITY = {"harness": "codex", "host": "host-a", "session_id": "executor"}
PIN_TARGET = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def root() -> Path:
    # Retained synthetic state allows inspection when a gate fails in the isolated run.
    path = Path(__file__).parent / "host-integration-test-data" / uuid.uuid4().hex
    path.mkdir(parents=True)
    return path


def setup_host(root: Path, triggers: list[str], *, workspace: str | None = None) -> tuple[Wake, Path]:
    home = root / "coord"
    wake = Wake(home=home)
    wake.enroll(binding_id="native", office_agent=TARGET, driver="codex-cli",
                native_thread_id=PIN_TARGET,
                workspace=workspace or str(root / "owned-session"),
                enrolled_by="owner", allowed_wakers=[SENDER],
                allowed_triggers=triggers, wake_kinds=["question", "handoff"],
                expires_at=format_ts(time.time() + 3600), owner_confirm=True,
                execution_mode="host", execution_host="host-a",
                executor_agent=EXECUTOR, workspace_flavor="windows")
    wake.verify("native", owner_confirm=True)
    for agent in (TARGET, SENDER, EXECUTOR):
        Office(agent_id=agent, home=home)._remember_canonical_agent()
    return wake, home


def gate(home: Path, op: str, args: dict, *, peer_host: str | None = "host-a",
         identity: dict | None = None) -> tuple[int, dict]:
    frame = build_request(op, identity or IDENTITY, args)
    output = io.BytesIO()
    code = run(io.BytesIO(frame), output, principal="ws-office", home=home,
               peer_host=peer_host, environ={})
    return code, json.loads(output.getvalue())


def test_message_transport_wrong_host_actor_stop_and_single_start(root: Path) -> None:
    # The path is deliberately absent: a server-side Windows path probe would block it.
    wake, home = setup_host(root, ["message"], workspace=r"Z:\absent\owned-session")
    sent = Office(agent_id=SENDER, home=home).send(TARGET, "synthetic question", kind="question")
    assert sent["status"] == "ok"
    result = wake.tick()
    assert any(row.get("reason") == "host_pending" for row in result["decisions"])
    assert gate(home, "wake-cli-pending", {}, peer_host=None)[1]["code"] == "peer_host_required"
    assert gate(home, "wake-cli-pending", {}, peer_host="foreign-host")[1]["code"] == "peer_host_mismatch"
    assert gate(home, "wake-cli-pending", {}, identity={**IDENTITY, "session_id": "other"})[1]["rows"] == []
    code, pending = gate(home, "wake-cli-pending", {})
    assert code == 0 and len(pending["rows"]) == 1
    dispatch_id = pending["rows"][0]["dispatch_id"]
    assert pending["rows"][0]["trigger"] == "message"
    assert "body" not in pending["rows"][0]
    request_id = uuid.uuid4().hex
    assert gate(home, "wake-cli-claim", {"dispatch_id": dispatch_id,
                                          "request_id": request_id},
                identity={**IDENTITY, "session_id": "other"})[1]["code"] == "forbidden"
    _, claim = gate(home, "wake-cli-claim", {"dispatch_id": dispatch_id,
                                               "request_id": request_id})
    assert claim["status"] == "claimed" and claim["replayed"] is False
    args = {"dispatch_id": dispatch_id, "request_id": request_id,
            "lease_id": claim["lease_id"]}
    wake.stop()
    assert gate(home, "wake-cli-start", args)[1]["code"] == "refused"
    wake.start()
    assert gate(home, "wake-cli-start", args)[1]["first_start"] is True
    assert gate(home, "wake-cli-start", args)[1]["first_start"] is False
    assert gate(home, "wake-cli-receipt", {"dispatch_id": dispatch_id,
                                           "lease_id": claim["lease_id"],
                                           "status": "accepted"})[1]["status"] == "accepted"


def test_child_and_schedule_are_queued_without_local_resume(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wake, home = setup_host(root, ["child-terminal", "schedule"])
    monkeypatch.setattr(wake, "_run_driver_prompt", lambda *_: pytest.fail("server-local resume"))
    workspace = root / "child-workspace"
    workspace.mkdir()
    state = workspace / "state.json"
    state.write_text('{"status":"ACCEPTED_CANDIDATE"}', encoding="utf-8")
    event = {"schema": "clawops.child-terminal/v1",
             "event_id": hashlib.sha256(b"child-runACCEPTED_CANDIDATE").hexdigest(),
             "run_id": "child-run", "source": "agent-graph",
             "terminal_state": "ACCEPTED_CANDIDATE", "result_ref": str(state),
             "parent_agent": TARGET, "parent_session": "synthetic", "parent_harness": "codex",
             "workspace": str(workspace), "started_at": format_ts(time.time() - 60),
             "observed_at": format_ts(time.time())}
    body = json.dumps(event, separators=(",", ":"))
    Office(agent_id=SENDER, home=home).send(None, body, channel="watch-events", kind="note")
    Office(agent_id=SENDER, home=home).send(TARGET, body, kind="note")
    child = wake.tick()
    assert any(row.get("reason") == "host_pending" for row in child["decisions"])
    local_now = datetime.fromtimestamp(time.time())
    added = wake.add_schedule(schedule_id="daily", binding_id="native",
                              time=local_now.strftime("%H:%M"), days="daily",
                              task_text="synthetic", created_by="owner",
                              owner_confirm=True)
    assert added["status"] == "ok"
    schedule = wake._tick_due_schedule(added["schedule"], local_now.date().isoformat(), dry_run=False)
    assert schedule["reason"] == "host_pending"
    _, pending = gate(home, "wake-cli-pending", {})
    assert {row["trigger"] for row in pending["rows"]} == {"child-terminal", "schedule"}


def test_host_adapter_uses_transport_once_and_unknown_idle_defers(root: Path) -> None:
    owned = root / "owned-session"
    owned.mkdir()
    executable = root / "codex.exe"
    executable.write_bytes(b"synthetic pin; runner is injected")
    wake, home = setup_host(root, ["message"])
    Office(agent_id=SENDER, home=home).send(TARGET, "synthetic", kind="question")
    assert any(row.get("reason") == "host_pending" for row in wake.tick()["decisions"])

    class LoopbackClient:
        def request(self, op: str, identity: dict, args: dict) -> dict:
            return gate(home, op, args, identity=identity)[1]

    adapter = CliHostRemoteAdapter(LoopbackClient(), execution_host="host-a",
                                   executor_agent=EXECUTOR, harness="codex",
                                   session_id="executor")
    enrollment = HostEnrollment(binding_id="native", binding_version=2,
                                execution_host="host-a", executor_agent=EXECUTOR,
                                driver="codex-cli", native_thread_id=PIN_TARGET,
                                workspace=owned, executable=executable)
    assert gate(home, "wake-cli-pending", {})[1]["rows"][0]["workspace"] == str(enrollment.workspace)
    row = gate(home, "wake-cli-pending", {})[1]["rows"][0]
    assert {key: row[key] for key in HostExecutor._projection(enrollment)} == HostExecutor._projection(enrollment)
    runs: list[list[str]] = []
    idle = [None]
    from coordfs.worker_profile import HostWorkerProfile
    profile = HostWorkerProfile(enrollment.binding_id, enrollment.driver, "gpt-6-luna",
                                "read-only", "medium", "subscription")
    executor = HostExecutor([enrollment], journal_home=root / "journal", remote=adapter,
                            profiles={enrollment.binding_id: profile},
                            codex_config=root / "absent-config.toml",
                            auth_probe=lambda *_: True,
                            idle_probe=lambda _item: idle[0],
                            runner=lambda **kwargs: (runs.append(kwargs["argv"])
                                                     or {"status": "accepted", "exit_code": 0}))
    assert executor.run_once()[0]["status"] == "not_idle"
    assert runs == []
    idle[0] = True
    assert executor.run_once()[0]["status"] == "accepted"
    assert len(runs) == 1
    assert executor.run_once() == []
    assert len(runs) == 1
    with pytest.raises(CoordError):
        adapter.pending(peer_host="foreign-host", executor_agent=EXECUTOR)


@pytest.mark.parametrize("path,flavor", [(r"C:\work\..\escape", "windows"),
                                           ("/srv/work/../escape", "posix"),
                                           ("relative/path", "posix")])
def test_host_workspace_rejects_traversal_and_relative(path: str, flavor: str) -> None:
    with pytest.raises(CoordError):
        _host_workspace(path, flavor)


PIN_EXE = r"C:\cli\codex.exe"


def rows(*items: tuple[str | None, str | None]) -> list[dict]:
    return [{"ExecutablePath": path, "CommandLine": command}
            for path, command in items]


def test_idle_classifier_requires_terminal_and_clear_processes() -> None:
    from office_cli_host import classify_native_processes

    quiet = rows((r"C:\other\tool.exe", "other command"))
    busy = rows((PIN_EXE, f"codex.exe exec resume {PIN_TARGET} --json"))
    ambiguous = rows((PIN_EXE, "codex.exe app-server"))
    assert classify_native_processes([], target=PIN_TARGET, executable=PIN_EXE) == "unknown"
    assert classify_native_processes([], target=PIN_TARGET, executable=PIN_EXE,
                                     terminal_evidence=True) == "idle"
    assert classify_native_processes(quiet, target=PIN_TARGET, executable=PIN_EXE) == "unknown"
    assert classify_native_processes(quiet, target=PIN_TARGET, executable=PIN_EXE,
                                     terminal_evidence=True) == "idle"
    assert classify_native_processes(busy, target=PIN_TARGET, executable=PIN_EXE,
                                     terminal_evidence=True) == "busy"
    assert classify_native_processes(ambiguous, target=PIN_TARGET, executable=PIN_EXE,
                                     terminal_evidence=True) == "unknown"
    assert classify_native_processes(rows((PIN_EXE, None)), target=PIN_TARGET,
                                     executable=PIN_EXE, terminal_evidence=True) == "unknown"


def test_idle_script_reports_only_process_snapshot() -> None:
    from office_cli_host import _IDLE_SCRIPT

    assert "ConvertTo-Json" in _IDLE_SCRIPT
    assert "$pinned.Count -gt 64" in _IDLE_SCRIPT
    assert "GetByteCount($json) -gt 32768" in _IDLE_SCRIPT
    assert "'clear'" in _IDLE_SCRIPT
    assert "OFFICE_CLI_IDLE_PROOF" not in _IDLE_SCRIPT
    assert "'idle'" not in _IDLE_SCRIPT


def _native_enrollment(root: Path, driver: str = "codex-cli") -> HostEnrollment:
    workspace = root / "owned-session"
    workspace.mkdir(exist_ok=True)
    executable = root / ("codex.exe" if driver == "codex-cli" else "claude.exe")
    executable.write_bytes(b"synthetic executable")
    return HostEnrollment(binding_id="native", binding_version=2,
                          execution_host="host-a", executor_agent=EXECUTOR,
                          driver=driver, native_thread_id=PIN_TARGET,
                          workspace=workspace, executable=executable)


def _write_records(path: Path, records: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")


def _codex_records(enrollment: HostEnrollment, *, completed: bool = True) -> list[dict]:
    records = [{"type": "session_meta", "payload": {
        "id": enrollment.native_thread_id, "cwd": str(enrollment.workspace), "source": "exec"}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-1"}}]
    if completed:
        records.append({"type": "event_msg", "payload": {
            "type": "task_complete", "turn_id": "turn-1"}})
    return records


def _claude_records(enrollment: HostEnrollment, *, completed: bool = True) -> list[dict]:
    common = {"sessionId": enrollment.native_thread_id, "cwd": str(enrollment.workspace)}
    return [{"type": "user", **common, "message": {"role": "user", "content": "synthetic"}},
            {"type": "assistant", **common, "message": {
                "role": "assistant", "content": [{"type": "text", "text": "synthetic"}],
                "stop_reason": "end_turn" if completed else None}}]


@pytest.mark.parametrize("driver", ["codex-cli", "claude-code-cli"])
def test_native_terminal_shapes_and_newer_turns(root: Path, driver: str) -> None:
    from office_cli_host import native_terminal_observed

    enrollment = _native_enrollment(root, driver)
    journal = root / ("rollout-" + PIN_TARGET + ".jsonl" if driver == "codex-cli"
                      else PIN_TARGET + ".jsonl")
    records = (_codex_records(enrollment) if driver == "codex-cli"
               else _claude_records(enrollment))
    _write_records(journal, records)
    assert native_terminal_observed(enrollment, journal)
    _write_records(journal, records[:-1] if driver == "codex-cli"
                   else _claude_records(enrollment, completed=False))
    assert not native_terminal_observed(enrollment, journal)
    _write_records(journal, records)
    if driver == "codex-cli":
        records.append({"type": "event_msg", "payload": {
            "type": "task_started", "turn_id": "turn-2"}})
    else:
        records.append({"type": "user", "sessionId": PIN_TARGET,
                        "cwd": str(enrollment.workspace), "message": {
                            "role": "user", "content": "new turn"}})
    _write_records(journal, records)
    assert not native_terminal_observed(enrollment, journal)
    if driver == "claude-code-cli":
        records[-1] = {"type": "assistant", "sessionId": PIN_TARGET,
                       "cwd": str(enrollment.workspace), "message": {
                           "role": "assistant", "stop_reason": "end_turn",
                           "content": [{"type": "tool_use"}]}}
        _write_records(journal, records)
        assert not native_terminal_observed(enrollment, journal)


@pytest.mark.parametrize("field,value", [
    ("id", "foreign-native"), ("cwd", "foreign-workspace"), ("source", "interactive")])
def test_codex_foreign_metadata_defers(root: Path, field: str, value: str) -> None:
    from office_cli_host import native_terminal_observed

    enrollment = _native_enrollment(root)
    journal = root / ("rollout-" + PIN_TARGET + ".jsonl")
    records = _codex_records(enrollment)
    records[0]["payload"][field] = value
    _write_records(journal, records)
    assert not native_terminal_observed(enrollment, journal)


def test_codex_requires_matching_started_turn_and_bounded_tail(root: Path) -> None:
    from office_cli_host import native_terminal_observed

    enrollment = _native_enrollment(root)
    journal = root / ("rollout-" + PIN_TARGET + ".jsonl")
    records = _codex_records(enrollment)
    records[-1]["payload"]["turn_id"] = "other-turn"
    _write_records(journal, records)
    assert not native_terminal_observed(enrollment, journal)
    # A large journal qualifies only if its bounded tail contains the actual
    # latest start and completion; the first session metadata stays in the head.
    records = [_codex_records(enrollment)[0]]
    records.extend({"type": "response_item", "payload": {"type": "message",
                    "padding": "x" * 500}} for _ in range(700))
    records.extend(_codex_records(enrollment)[1:])
    _write_records(journal, records)
    assert journal.stat().st_size > 320 * 1024
    assert native_terminal_observed(enrollment, journal)


@pytest.mark.parametrize("field,value", [
    ("sessionId", "foreign-native"), ("cwd", "foreign-workspace")])
def test_claude_foreign_metadata_defers(root: Path, field: str, value: str) -> None:
    from office_cli_host import native_terminal_observed

    enrollment = _native_enrollment(root, "claude-code-cli")
    journal = root / (PIN_TARGET + ".jsonl")
    records = _claude_records(enrollment)
    records[-1][field] = value
    _write_records(journal, records)
    assert not native_terminal_observed(enrollment, journal)


def test_malformed_truncated_and_oversized_journal_defers(root: Path) -> None:
    from office_cli_host import native_terminal_observed

    enrollment = _native_enrollment(root)
    journal = root / ("rollout-" + PIN_TARGET + ".jsonl")
    for content in ("{bad}\n", json.dumps(_codex_records(enrollment)[0]),
                    json.dumps(_codex_records(enrollment)[0]) + "\n" +
                    json.dumps({"type": "response_item", "payload": "x" * (256 * 1024)} ) + "\n"):
        journal.write_text(content, encoding="utf-8")
        assert not native_terminal_observed(enrollment, journal)


def test_native_evidence_schema_duplicate_foreign_and_reparse(
        root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import office_cli_host

    enrollment = _native_enrollment(root)
    journal = root / ("rollout-" + PIN_TARGET + ".jsonl")
    _write_records(journal, _codex_records(enrollment))
    config = root / "evidence.json"
    row = {"binding_id": enrollment.binding_id, "native_thread_id": PIN_TARGET,
           "driver": enrollment.driver, "workspace": str(enrollment.workspace),
           "journal_path": str(journal)}
    def load(rows: list[dict]) -> dict:
        config.write_text(json.dumps({"schema_version": 1, "sessions": rows}), encoding="utf-8")
        return office_cli_host.load_native_evidence(config, [enrollment])
    assert load([row]) == {"native": journal}
    for bad in ([row, row], [{**row, "binding_id": "foreign"}],
                [{**row, "workspace": "foreign"}],
                [{**row, "journal_path": str(root / "other.jsonl")}]):
        with pytest.raises(CoordError):
            load(bad)
    assert office_cli_host.load_native_evidence(None, [enrollment]) == {}
    original = office_cli_host._checked_path
    def reparse(path: Path, *, kind: str | None = None) -> Path:
        if path == journal:
            raise CoordError("synthetic reparse", code="invalid_path")
        return original(path, kind=kind)
    monkeypatch.setattr(office_cli_host, "_checked_path", reparse)
    with pytest.raises(CoordError):
        load([row])
    assert not office_cli_host.native_terminal_observed(enrollment, journal)


def test_native_journal_replacement_race_defers(
        root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import office_cli_host

    enrollment = _native_enrollment(root)
    journal = root / ("rollout-" + PIN_TARGET + ".jsonl")
    _write_records(journal, _codex_records(enrollment))
    original = Path.lstat
    count = [0]
    def changed(self: Path, *args, **kwargs):
        info = original(self, *args, **kwargs)
        if self == journal:
            count[0] += 1
            if count[0] >= 2:
                from types import SimpleNamespace
                return SimpleNamespace(st_dev=info.st_dev, st_ino=info.st_ino,
                    st_size=info.st_size + 1, st_mtime_ns=info.st_mtime_ns,
                    st_ctime_ns=info.st_ctime_ns, st_mode=info.st_mode,
                    st_file_attributes=getattr(info, "st_file_attributes", 0))
        return info
    monkeypatch.setattr(Path, "lstat", changed)
    assert not office_cli_host.native_terminal_observed(enrollment, journal)


def test_owned_session_idle_requires_journal_and_clear_snapshot(
        root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import office_cli_host

    enrollment = _native_enrollment(root)
    journal = root / ("rollout-" + PIN_TARGET + ".jsonl")
    _write_records(journal, _codex_records(enrollment))
    monkeypatch.setattr(office_cli_host.os, "name", "nt")
    monkeypatch.setenv("OFFICE_CLI_IDLE_PROOF", PIN_TARGET)
    calls: list[dict] = []
    class Completed:
        def __init__(self, output: str):
            self.stdout, self.returncode = output, 0
    output = ["[]\n"]
    def fake_run(argv, **kwargs):
        calls.append(kwargs)
        return Completed(output[0])
    monkeypatch.setattr(office_cli_host.subprocess, "run", fake_run)
    assert office_cli_host.owned_session_idle(enrollment) is None
    assert office_cli_host.owned_session_idle(enrollment,
        evidence={"native": journal}) is True
    assert "OFFICE_CLI_IDLE_PROOF" not in calls[-1]["env"]
    assert calls[-1]["shell"] is False
    output[0] = json.dumps([f"codex.exe exec resume {PIN_TARGET} --json"])
    assert office_cli_host.owned_session_idle(enrollment,
        evidence={"native": journal}) is False
    output[0] = "unknown\n"
    assert office_cli_host.owned_session_idle(enrollment,
        evidence={"native": journal}) is None


def test_executor_defers_without_native_evidence(
        root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import office_cli_host

    enrollment = _native_enrollment(root)
    journal = root / ("rollout-" + PIN_TARGET + ".jsonl")
    _write_records(journal, _codex_records(enrollment))
    wake, home = setup_host(root, ["message"])
    Office(agent_id=SENDER, home=home).send(TARGET, "synthetic", kind="question")
    wake.tick()
    class LoopbackClient:
        def request(self, op, identity, args):
            return gate(home, op, args, identity=identity)[1]
    adapter = CliHostRemoteAdapter(LoopbackClient(), execution_host="host-a",
                                   executor_agent=EXECUTOR, harness="codex",
                                   session_id="executor")
    monkeypatch.setattr(office_cli_host.os, "name", "nt")
    class Completed:
        returncode = 0
        stdout = "[]\n"
    monkeypatch.setattr(office_cli_host.subprocess, "run",
                        lambda *args, **kwargs: Completed())
    runs = []
    from coordfs.worker_profile import HostWorkerProfile
    profile = HostWorkerProfile(enrollment.binding_id, enrollment.driver, "gpt-6-luna",
                                "read-only", "medium", "subscription")
    executor = HostExecutor([enrollment], journal_home=root / "journal", remote=adapter,
        profiles={enrollment.binding_id: profile},
        codex_config=root / "absent-config.toml", auth_probe=lambda *_: True,
        idle_probe=lambda item: office_cli_host.owned_session_idle(item),
        runner=lambda **kwargs: runs.append(kwargs))
    assert executor.run_once()[0]["status"] == "not_idle"
    assert runs == []
