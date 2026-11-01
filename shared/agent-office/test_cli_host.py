"""Synthetic candidate tests: no provider executable is ever run."""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordfs.cli_host import HostEnrollment, HostExecutor, native_runner
from coordfs.errors import CoordError
from coordfs.worker_profile import HostWorkerProfile


DISPATCH = "a" * 32
LEASE = "b" * 32


@pytest.fixture
def tmp_path():
    # pytest's Windows mode=0700 temp root is unreadable in the isolated runner.
    # Keep synthetic artifacts under this worktree, with no cleanup in this run.
    path = Path(__file__).parent / "host-test-data" / uuid.uuid4().hex
    path.mkdir(parents=True)
    return path


def enrollment(tmp_path: Path, *, binding_id: str = "one", native_id: str = "thread-one",
               driver: str = "codex-cli") -> HostEnrollment:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    executable = tmp_path / ("codex.exe" if driver == "codex-cli" else "claude.exe")
    executable.touch(exist_ok=True)
    return HostEnrollment(binding_id, 1, "ailab", "codex-actor", driver,
                          native_id, workspace, executable)


def target(item: HostEnrollment) -> dict:
    return {"binding_id": item.binding_id, "version": item.binding_version,
            "execution_host": item.execution_host, "executor_agent": item.executor_agent,
            "driver": item.driver, "native_thread_id": item.native_thread_id,
            "workspace": str(item.workspace), "workspace_flavor": "windows"}


class Remote:
    def __init__(self, item: HostEnrollment) -> None:
        self.item = item
        self.row = {"dispatch_id": DISPATCH, **target(item), "trigger": "message",
                    "trigger_ref": "m1", "correlation_id": None, "state": "pending",
                    "enqueued_at": "2026-09-28T00:00:00Z",
                    "expires_at": "2099-01-01T00:00:00Z"}
        self.claim_calls = []
        self.start_calls = []
        self.receipt_calls = []
        self.start_error = None
        self.receipt_error = None

    def pending(self, *, peer_host, executor_agent, limit):
        assert (peer_host, executor_agent) == ("ailab", "codex-actor")
        assert 1 <= limit <= 20
        return [self.row]

    def claim(self, dispatch_id, **kwargs):
        self.claim_calls.append((dispatch_id, kwargs))
        return {"status": "claimed", "dispatch_id": dispatch_id,
                "request_id": kwargs["request_id"], "lease_id": LEASE,
                "deadline_ts": time.time() + 60,
                "target": target(self.item), "replayed": False}

    def start(self, dispatch_id, **kwargs):
        self.start_calls.append((dispatch_id, kwargs))
        if self.start_error:
            raise self.start_error
        return {"status": "authorized", "dispatch_id": dispatch_id,
                "lease_id": kwargs["lease_id"], "first_start": True}

    def receipt(self, dispatch_id, **kwargs):
        self.receipt_calls.append((dispatch_id, kwargs))
        if self.receipt_error:
            raise self.receipt_error
        return {"status": kwargs["status"], "dispatch_id": dispatch_id,
                "lease_id": kwargs["lease_id"]}


def host(tmp_path, item, remote, runner, *, idle_probe=lambda _: True):
    return HostExecutor([item], journal_home=tmp_path / "journal", remote=remote,
                        idle_probe=idle_probe, runner=runner,
                        profiles={item.binding_id: HostWorkerProfile(item.binding_id, item.driver,
                            "gpt-6-luna" if item.driver == "codex-cli" else "claude-sonnet-5",
                            "read-only", "medium", "subscription")},
                        codex_config=tmp_path / "absent-config.toml",
                        auth_probe=lambda *_: True)


def test_one_launch_uses_owned_argv_and_static_stdin(tmp_path):
    item = enrollment(tmp_path)
    remote = Remote(item)
    calls = []
    executor = host(tmp_path, item, remote, lambda **kw: calls.append(kw) or
                    {"status": "accepted", "exit_code": 0})
    assert executor.run_once() == [{"dispatch_id": DISPATCH, "status": "accepted"}]
    assert len(calls) == 1
    assert calls[0]["argv"] == [str(item.executable), "exec", "resume",
        "--skip-git-repo-check", "--strict-config", "-m", "gpt-6-luna", "-c",
        'model_reasoning_effort="medium"', "-c", 'sandbox_mode="read-only"',
        "-c", 'approval_policy="never"', "thread-one", "-"]
    assert calls[0]["cwd"] == str(item.workspace)
    assert calls[0]["timeout"] == 120
    assert "Office" in calls[0]["stdin"] and DISPATCH not in calls[0]["stdin"]
    assert executor.run_once()[0]["status"] == "already_recorded"
    assert len(calls) == len(remote.claim_calls) == len(remote.start_calls) == 1


@pytest.mark.parametrize("idle", [False, None])
def test_missing_idle_proof_defers_without_claim_or_journal(tmp_path, idle):
    item = enrollment(tmp_path)
    remote = Remote(item)
    executor = host(tmp_path, item, remote, lambda **_: pytest.fail("launched"),
                    idle_probe=lambda _: idle)
    assert executor.run_once()[0]["status"] == "not_idle"
    assert not remote.claim_calls
    assert list((tmp_path / "journal" / "dispatches").iterdir()) == []


@pytest.mark.parametrize("field,value", [
    ("version", 2), ("version", True), ("execution_host", "elsewhere"),
    ("executor_agent", "someone-else"), ("driver", "claude-code-cli"),
    ("native_thread_id", "another-thread"), ("workspace", "C:\\foreign"),
    ("workspace_flavor", "posix"),
])
def test_mismatched_pending_refuses_before_claim(tmp_path, field, value):
    item = enrollment(tmp_path)
    remote = Remote(item)
    remote.row = {**remote.row, field: value}
    executor = host(tmp_path, item, remote, lambda **_: pytest.fail("launched"))
    assert executor.run_once()[0]["status"] == "not_enrolled"
    assert not remote.claim_calls


def test_changed_claim_projection_cannot_start(tmp_path):
    item = enrollment(tmp_path)
    remote = Remote(item)
    original = remote.claim

    def changed(*args, **kwargs):
        response = original(*args, **kwargs)
        response["target"]["version"] = 2
        return response

    remote.claim = changed
    executor = host(tmp_path, item, remote, lambda **_: pytest.fail("launched"))
    assert executor.run_once()[0]["status"] == "claim_unknown"
    assert not remote.start_calls
    assert executor.run_once()[0]["status"] == "retained_unknown"


def test_claimed_session_becomes_busy_before_start(tmp_path):
    item = enrollment(tmp_path)
    remote = Remote(item)
    probes = iter((True, False))
    executor = host(tmp_path, item, remote, lambda **_: pytest.fail("launched"),
                    idle_probe=lambda _: next(probes))
    assert executor.run_once()[0]["status"] == "not_idle"
    assert len(remote.claim_calls) == 1
    assert not remote.start_calls
    assert host(tmp_path, item, remote, lambda **_: pytest.fail("rerun")).run_once()[0]["status"] == "retained_unknown"


def test_expired_claim_lease_never_starts(tmp_path):
    item = enrollment(tmp_path)
    remote = Remote(item)
    original = remote.claim

    def expired(*args, **kwargs):
        claim = original(*args, **kwargs)
        claim["deadline_ts"] = time.time() - 1
        return claim

    remote.claim = expired
    executor = host(tmp_path, item, remote, lambda **_: pytest.fail("launched"))
    assert executor.run_once()[0]["status"] == "claim_expired"
    assert not remote.start_calls
    assert executor.run_once()[0]["status"] == "retained_unknown"


def test_pending_cannot_carry_prompt_or_argv(tmp_path):
    item = enrollment(tmp_path)
    remote = Remote(item)
    remote.row["argv"] = ["unexpected"]
    executor = host(tmp_path, item, remote, lambda **_: pytest.fail("launched"))
    with pytest.raises(CoordError, match="unsupported fields"):
        executor.run_once()
    assert not remote.claim_calls


@pytest.mark.parametrize("field,value", [
    ("trigger_ref", "m1\nrun something"),
    ("trigger_ref", ""),
    ("correlation_id", "x" * 257),
    ("enqueued_at", "not-a-date"),
    ("expires_at", time.time() + 60),
])
def test_malformed_pending_metadata_refuses_before_claim(tmp_path, field, value):
    item = enrollment(tmp_path)
    remote = Remote(item)
    remote.row[field] = value
    executor = host(tmp_path, item, remote, lambda **_: pytest.fail("launched"))
    with pytest.raises(CoordError):
        executor.run_once()
    assert not remote.claim_calls


@pytest.mark.parametrize("start", [
    {"status": "authorized", "first_start": False},
    {"status": "stopped", "first_start": True},
    None,
])
def test_start_replay_stop_or_lost_response_never_launches(tmp_path, start):
    item = enrollment(tmp_path)
    remote = Remote(item)
    if start is None:
        remote.start_error = TimeoutError("lost start")
    else:
        remote.start = lambda dispatch_id, **kw: {
            **start, "dispatch_id": dispatch_id, "lease_id": kw["lease_id"]}
    executor = host(tmp_path, item, remote, lambda **_: pytest.fail("launched"))
    assert executor.run_once()[0]["status"] in ("start_unknown", "delivery_unknown")
    assert executor.run_once()[0]["status"] in (
        "already_recorded", "retained_unknown", "delivery_unknown", "receipt_unknown")
    assert len(remote.claim_calls) == 1


def test_lost_receipt_replays_only_terminal_receipt_after_restart(tmp_path):
    item = enrollment(tmp_path)
    remote = Remote(item)
    remote.receipt_error = TimeoutError("lost receipt")
    calls = []
    first = host(tmp_path, item, remote, lambda **kw: calls.append(kw) or
                 {"status": "accepted", "exit_code": 0})
    assert first.run_once()[0]["status"] == "receipt_unknown"
    remote.receipt_error = None
    second = host(tmp_path, item, remote, lambda **_: pytest.fail("rerun"))
    assert second.run_once()[0]["status"] == "accepted"
    assert second.run_once()[0]["status"] == "already_recorded"
    assert len(calls) == len(remote.claim_calls) == len(remote.start_calls) == 1
    assert len(remote.receipt_calls) == 2


def test_lost_receipt_replays_when_pending_no_longer_lists_dispatch(tmp_path):
    item = enrollment(tmp_path)
    remote = Remote(item)
    remote.receipt_error = TimeoutError("lost receipt")
    calls = []
    first = host(tmp_path, item, remote, lambda **kw: calls.append(kw) or
                 {"status": "accepted", "exit_code": 0})
    assert first.run_once()[0]["status"] == "receipt_unknown"
    remote.receipt_error = None
    remote.pending = lambda **_: []
    recovered = host(tmp_path, item, remote, lambda **_: pytest.fail("rerun"))
    assert recovered.run_once() == [{"dispatch_id": DISPATCH, "status": "accepted"}]
    assert recovered.run_once() == []
    assert len(calls) == len(remote.claim_calls) == len(remote.start_calls) == 1
    assert len(remote.receipt_calls) == 2


def test_changed_local_enrollment_cannot_replay_retained_receipt(tmp_path):
    item = enrollment(tmp_path)
    remote = Remote(item)
    remote.receipt_error = TimeoutError("lost receipt")
    assert host(tmp_path, item, remote, lambda **_: {"status": "accepted"}).run_once()[0]["status"] == "receipt_unknown"
    remote.receipt_error = None
    remote.pending = lambda **_: []
    changed = HostEnrollment(item.binding_id, 2, item.execution_host,
                             item.executor_agent, item.driver, item.native_thread_id,
                             item.workspace, item.executable)
    assert host(tmp_path, changed, remote, lambda **_: pytest.fail("rerun")).run_once() == []
    assert len(remote.receipt_calls) == 1


def test_corrupt_retained_identity_never_sends_receipt(tmp_path):
    item = enrollment(tmp_path)
    remote = Remote(item)
    remote.receipt_error = TimeoutError("lost receipt")
    executor = host(tmp_path, item, remote, lambda **_: {"status": "accepted"})
    assert executor.run_once()[0]["status"] == "receipt_unknown"
    journal, _ = executor._paths(DISPATCH, item)
    lines = journal.read_text(encoding="utf-8").splitlines()
    import json
    first = json.loads(lines[0])
    first["dispatch_id"] = "c" * 32
    lines[0] = json.dumps(first)
    journal.write_text("\n".join(lines) + "\n", encoding="utf-8")
    remote.receipt_error = None
    remote.pending = lambda **_: []
    assert executor.run_once() == [{"dispatch_id": DISPATCH, "status": "host_refused"}]
    assert len(remote.receipt_calls) == 1


def test_interrupted_runner_retains_unknown_and_never_reruns(tmp_path):
    item = enrollment(tmp_path)
    remote = Remote(item)
    def interrupted(**_):
        raise KeyboardInterrupt()
    executor = host(tmp_path, item, remote, interrupted)
    assert executor.run_once()[0]["status"] == "delivery_unknown"
    replay = host(tmp_path, item, remote, lambda **_: pytest.fail("rerun"))
    assert replay.run_once()[0]["status"] == "delivery_unknown"
    assert len(remote.start_calls) == 1


def test_failed_before_claim_persist_never_claims(tmp_path, monkeypatch):
    item = enrollment(tmp_path)
    remote = Remote(item)
    executor = host(tmp_path, item, remote, lambda **_: pytest.fail("launched"))
    original = executor._append

    def fail_first(path, event):
        if event["phase"] == "before_claim":
            raise CoordError("synthetic fsync failure", code="invalid_journal")
        return original(path, event)

    monkeypatch.setattr(executor, "_append", fail_first)
    assert executor.run_once()[0]["status"] == "host_refused"
    assert not remote.claim_calls and not remote.start_calls


def test_failed_started_intent_persist_never_launches_after_restart(tmp_path, monkeypatch):
    item = enrollment(tmp_path)
    remote = Remote(item)
    executor = host(tmp_path, item, remote, lambda **_: pytest.fail("launched"))
    original = executor._append

    def fail_intent(path, event):
        if event["phase"] == "started_intent":
            raise CoordError("synthetic fsync failure", code="invalid_journal")
        return original(path, event)

    monkeypatch.setattr(executor, "_append", fail_intent)
    assert executor.run_once()[0]["status"] == "delivery_unknown"
    assert len(remote.claim_calls) == len(remote.start_calls) == 1
    restarted = host(tmp_path, item, remote, lambda **_: pytest.fail("rerun"))
    assert restarted.run_once()[0]["status"] == "retained_unknown"
    assert len(remote.start_calls) == 1


def test_two_executors_and_native_alias_share_file_fence(tmp_path):
    item = enrollment(tmp_path)
    alias = enrollment(tmp_path, binding_id="alias", native_id="thread-one")
    remote = Remote(item)
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def blocking_runner(**kw):
        calls.append(kw)
        entered.set()
        assert release.wait(5)
        return {"status": "accepted"}

    first = host(tmp_path, item, remote, blocking_runner)
    alias_remote = Remote(alias)
    alias_remote.row["dispatch_id"] = "c" * 32
    second = host(tmp_path, alias, alias_remote, blocking_runner)
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(first.run_once)
        assert entered.wait(5)
        assert second.run_once()[0]["status"] == "session_busy"
        release.set()
        assert future.result()[0]["status"] == "accepted"
    assert len(calls) == 1
    assert not alias_remote.claim_calls
    assert first._paths(DISPATCH, item)[1] == first._paths(DISPATCH, alias)[1]


def test_native_alias_fence_is_case_insensitive(tmp_path):
    item = enrollment(tmp_path, native_id="Thread-One")
    alias = enrollment(tmp_path, binding_id="alias", native_id="thread-one")
    executor = host(tmp_path, item, Remote(item), lambda **_: pytest.fail("launched"))
    assert executor._paths(DISPATCH, item)[1] == executor._paths(DISPATCH, alias)[1]


def test_corrupt_journal_and_symlink_home_fail_closed(tmp_path):
    item = enrollment(tmp_path)
    remote = Remote(item)
    executor = host(tmp_path, item, remote, lambda **_: pytest.fail("launched"))
    journal, _ = executor._paths(DISPATCH, item)
    journal.write_text('{"phase":"before_claim"}', encoding="utf-8")
    assert executor.run_once()[0]["status"] == "host_refused"
    assert not remote.claim_calls
    link = tmp_path / "linked"
    try:
        link.symlink_to(tmp_path / "journal", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    with pytest.raises(CoordError):
        HostExecutor([item], journal_home=link, remote=remote, idle_probe=lambda _: True)


def test_reparse_ancestry_is_refused_without_os_symlink_privilege(tmp_path, monkeypatch):
    item = enrollment(tmp_path)
    journal = tmp_path / "journal"
    journal.mkdir()
    original = Path.lstat

    def flagged(path):
        result = original(path)
        if path == journal:
            return SimpleNamespace(st_mode=result.st_mode, st_file_attributes=0x400)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(Path, "lstat", flagged)
        with pytest.raises(CoordError, match="reparse"):
            HostExecutor([item], journal_home=journal, remote=Remote(item),
                         idle_probe=lambda _: True)


@pytest.mark.parametrize("native_id", ["-bad", "id;evil", "a\nline"])
def test_unsafe_native_id_refused(tmp_path, native_id):
    with pytest.raises(CoordError):
        enrollment(tmp_path, native_id=native_id)


def test_script_executable_refused_even_with_fake_runner(tmp_path):
    item = enrollment(tmp_path)
    script = tmp_path / "codex.py"
    script.touch()
    with pytest.raises(CoordError):
        HostEnrollment(item.binding_id, 1, item.execution_host, item.executor_agent,
                       item.driver, item.native_thread_id, item.workspace, script)


def test_enrollment_rejects_control_and_parent_traversal_paths(tmp_path):
    item = enrollment(tmp_path)
    for workspace in (str(item.workspace) + "\n", str(item.workspace / ".." / "workspace")):
        with pytest.raises(CoordError, match="path must be absolute"):
            HostEnrollment(item.binding_id, 1, item.execution_host,
                           item.executor_agent, item.driver, item.native_thread_id,
                           workspace, item.executable)


def test_default_runner_uses_no_shell_and_discards_output(monkeypatch, tmp_path):
    import subprocess
    observed = []
    def fake_run(argv, **kw):
        observed.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0)
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert native_runner(argv=["codex.exe", "exec"], cwd=str(tmp_path),
                         stdin="fixed", timeout=10, env={"PATH": "synthetic"})["status"] == "accepted"
    assert observed[0][1]["shell"] is False
    assert observed[0][1]["stdout"] is subprocess.DEVNULL
    assert observed[0][1]["stderr"] is subprocess.DEVNULL
    assert observed[0][1]["env"] == {"PATH": "synthetic"}


def test_empty_poll_performs_no_probe_claim_or_runner(tmp_path):
    item = enrollment(tmp_path)
    remote = Remote(item)
    remote.pending = lambda **_: []
    executor = host(tmp_path, item, remote, lambda **_: pytest.fail("launched"),
                    idle_probe=lambda _: pytest.fail("probed"))
    assert executor.run_once() == []


def test_frozen_dispatch_interface_synthetic_delivery(tmp_path):
    from coordfs.cli_dispatch import CliDispatches

    item = enrollment(tmp_path)
    server = CliDispatches(tmp_path / "server", eligibility=lambda _: None)
    queued = server.enqueue(target(item), trigger="message", trigger_ref="synthetic-id")
    calls = []
    executor = host(tmp_path, item, server, lambda **kw: calls.append(kw) or
                    {"status": "accepted", "exit_code": 0})
    assert executor.run_once() == [{"dispatch_id": queued["dispatch_id"], "status": "accepted"}]
    assert len(calls) == 1
    assert server.pending(peer_host="ailab", executor_agent="codex-actor") == []


def test_frozen_dispatch_interface_replays_lost_receipt_without_pending(tmp_path):
    from coordfs.cli_dispatch import CliDispatches

    item = enrollment(tmp_path)
    server = CliDispatches(tmp_path / "server", eligibility=lambda _: None)
    queued = server.enqueue(target(item), trigger="schedule", trigger_ref="synthetic-tick")

    class LostReply:
        pending = server.pending
        claim = server.claim
        start = server.start

        def __init__(self):
            self.lose_once = True

        def receipt(self, *args, **kwargs):
            result = server.receipt(*args, **kwargs)
            if self.lose_once:
                self.lose_once = False
                raise TimeoutError("reply lost after server commit")
            return result

    remote = LostReply()
    calls = []
    first = host(tmp_path, item, remote, lambda **kw: calls.append(kw) or
                 {"status": "accepted", "exit_code": 0})
    assert first.run_once() == [{"dispatch_id": queued["dispatch_id"],
                                 "status": "receipt_unknown"}]
    assert server.pending(peer_host="ailab", executor_agent="codex-actor") == []
    restarted = host(tmp_path, item, remote, lambda **_: pytest.fail("rerun"))
    assert restarted.run_once() == [{"dispatch_id": queued["dispatch_id"],
                                     "status": "accepted"}]
    assert len(calls) == 1


def test_frozen_dispatch_lost_start_reply_after_server_commit_never_launches(tmp_path):
    from coordfs.cli_dispatch import CliDispatches

    item = enrollment(tmp_path)
    server = CliDispatches(tmp_path / "server", eligibility=lambda _: None)
    queued = server.enqueue(target(item), trigger="child-terminal", trigger_ref="synthetic-child")

    class LostStartReply:
        pending = server.pending
        claim = server.claim
        receipt = server.receipt

        def start(self, *args, **kwargs):
            server.start(*args, **kwargs)
            raise TimeoutError("reply lost after server commit")

    remote = LostStartReply()
    first = host(tmp_path, item, remote, lambda **_: pytest.fail("launched"))
    assert first.run_once() == [{"dispatch_id": queued["dispatch_id"],
                                 "status": "delivery_unknown"}]
    restarted = host(tmp_path, item, remote, lambda **_: pytest.fail("rerun"))
    assert restarted.run_once() == []
    journal, _ = restarted._paths(queued["dispatch_id"], item)
    assert [event["phase"] for event in restarted._events(journal)] == [
        "before_claim", "claimed", "terminal", "receipt_sent"]
    assert server.pending(peer_host="ailab", executor_agent="codex-actor") == []
