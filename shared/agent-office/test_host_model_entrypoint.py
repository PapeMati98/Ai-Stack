"""Exercise the actual host entrypoint and executor; no provider/network calls."""
import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

import office_cli_host
import coordfs.cli_host as cli_host
from coordfs.errors import CoordError
from coordfs.worker_profile import HostWorkerProfile
from test_cli_host import Remote, DISPATCH, enrollment
from test_native_model_evidence import codex_meta, codex_turn, write_lines, append_lines


@pytest.fixture
def case_dir():
    path = Path(__file__).parent / "entrypoint-test-data" / uuid.uuid4().hex
    path.mkdir(parents=True)
    return path


def prepare(monkeypatch, root, *, observed="gpt-6-luna", new_turn=True, initial_complete=True):
    item = enrollment(root)
    native = root / ("owned-" + item.native_thread_id + ".jsonl")
    write_lines(native, [codex_meta(item), *codex_turn(item, "old", model="gpt-6-luna",
                                                    complete=initial_complete)])
    remote = Remote(item)
    calls = []
    profile = HostWorkerProfile(item.binding_id, item.driver, "gpt-6-luna", "read-only",
                                "medium", "subscription")
    def runner(**kwargs):
        calls.append(kwargs)
        if new_turn:
            append_lines(native, codex_turn(item, "fresh", model=observed))
        return {"status": "accepted", "exit_code": 0}
    monkeypatch.setattr(office_cli_host, "load_remote_config", lambda _: {})
    monkeypatch.setattr(office_cli_host, "OfficeRemoteClient", lambda config: config)
    monkeypatch.setattr(office_cli_host, "CliHostRemoteAdapter", lambda *_, **__: remote)
    monkeypatch.setattr(office_cli_host, "load_enrollments", lambda *_, **__: [item])
    monkeypatch.setattr(office_cli_host, "load_worker_profiles", lambda *_, **__: {item.binding_id: profile})
    monkeypatch.setattr(office_cli_host, "load_native_evidence", lambda *_, **__: {item.binding_id: native})
    # Only the process query is simulated. Native evidence uses the real parser.
    monkeypatch.setattr(office_cli_host, "owned_session_idle", lambda *_, **__: True)
    monkeypatch.setattr(cli_host, "native_runner", runner)
    monkeypatch.setattr(cli_host, "codex_subscription_probe", lambda *_, **__: True)
    monkeypatch.setenv("USERPROFILE", str(root))
    args = ["--remote-config", str(root / "remote.json"), "--enrollments", str(root / "enroll.json"),
            "--worker-profiles", str(root / "profiles.json"), "--native-evidence", str(root / "native.json"),
            "--journal-home", str(root / "host-journal"), "--execution-host", item.execution_host,
            "--executor-agent", item.executor_agent, "--harness", "codex", "--session-id", "synthetic"]
    return item, remote, calls, args


def phases(root):
    return [json.loads(line)["phase"] for line in
            (root / "host-journal/dispatches" / (DISPATCH + ".jsonl")).read_text(encoding="utf-8").splitlines()]


@pytest.mark.skipif(os.name != "nt", reason="Actual entrypoint requires Windows")
@pytest.mark.parametrize("observed,expected", [("gpt-6-luna", "profile_verified"),
                                              ("gpt-6-sol", "profile_mismatch")])
def test_entrypoint_wires_fresh_model_evidence(monkeypatch, case_dir, capsys, observed, expected):
    _, remote, calls, args = prepare(monkeypatch, case_dir, observed=observed)
    assert office_cli_host.main(args) == 0
    capsys.readouterr()
    assert expected in phases(case_dir)
    assert len(calls) == len(remote.claim_calls) == len(remote.start_calls) == 1
    assert len(remote.receipt_calls) == 1


@pytest.mark.skipif(os.name != "nt", reason="Actual entrypoint requires Windows")
def test_incomplete_native_baseline_never_claims(monkeypatch, case_dir, capsys):
    _, remote, calls, args = prepare(monkeypatch, case_dir, initial_complete=False)
    assert office_cli_host.main(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["outcomes"] == [{"dispatch_id": DISPATCH, "status": "not_idle"}]
    assert not remote.claim_calls and not remote.start_calls and not calls


@pytest.mark.skipif(os.name != "nt", reason="Actual entrypoint requires Windows")
def test_successful_runner_without_new_model_proof_stays_unverified(monkeypatch, case_dir, capsys):
    _, _, calls, args = prepare(monkeypatch, case_dir, new_turn=False)
    assert office_cli_host.main(args) == 0
    capsys.readouterr()
    assert "profile_unverified" in phases(case_dir)
    assert "profile_verified" not in phases(case_dir)
    assert len(calls) == 1


@pytest.mark.skipif(os.name != "nt", reason="Actual entrypoint requires Windows")
def test_model_mismatch_receipt_retry_never_relaunches(monkeypatch, case_dir, capsys):
    _, remote, calls, args = prepare(monkeypatch, case_dir, observed="gpt-6-sol")
    remote.receipt_error = CoordError("synthetic lost receipt", code="transport_error")
    assert office_cli_host.main(args) == 0
    capsys.readouterr()
    assert "profile_mismatch" in phases(case_dir)
    remote.receipt_error = None
    assert office_cli_host.main(args) == 0
    capsys.readouterr()
    assert "receipt_sent" in phases(case_dir)
    assert len(calls) == len(remote.claim_calls) == len(remote.start_calls) == 1
    assert len(remote.receipt_calls) == 2


@pytest.mark.skipif(os.name != "nt", reason="Actual entrypoint requires Windows")
def test_arm_and_observe_execute_under_owned_session_fence(monkeypatch, case_dir, capsys):
    prepare_result = prepare(monkeypatch, case_dir)
    _, _, _, args = prepare_result
    real_fence = cli_host._session_fence
    real_observer = office_cli_host.NativeModelObserver
    held = [False]
    callbacks = []

    @contextmanager
    def fence(path):
        with real_fence(path) as acquired:
            held[0] = acquired
            try:
                yield acquired
            finally:
                held[0] = False

    class Observer(real_observer):
        def arm(self, item):
            assert held[0], "Arming must not race outside the session fence"
            callbacks.append("arm")
            return super().arm(item)

        def observe(self, item):
            assert held[0], "Observation must finish before the fence is released"
            callbacks.append("observe")
            return super().observe(item)

    monkeypatch.setattr(cli_host, "_session_fence", fence)
    monkeypatch.setattr(office_cli_host, "NativeModelObserver", Observer)
    assert office_cli_host.main(args) == 0
    capsys.readouterr()
    assert callbacks == ["arm", "arm", "observe"]
    assert not held[0]


@pytest.mark.skipif(os.name != "nt", reason="Actual entrypoint requires Windows")
def test_real_entrypoint_rechecks_busy_after_claim(monkeypatch, case_dir, capsys):
    _, remote, calls, args = prepare(monkeypatch, case_dir)
    sequence = iter([True, False])
    monkeypatch.setattr(office_cli_host, "owned_session_idle", lambda *_, **__: next(sequence))
    assert office_cli_host.main(args) == 0
    assert json.loads(capsys.readouterr().out)["outcomes"][0]["status"] == "not_idle"
    assert len(remote.claim_calls) == 1
    assert not remote.start_calls and not calls


@pytest.mark.skipif(os.name != "nt", reason="Actual entrypoint requires Windows")
def test_second_arm_rejects_active_journal_even_when_process_probe_is_clear(monkeypatch, case_dir, capsys):
    item, remote, calls, args = prepare(monkeypatch, case_dir)
    original_claim = remote.claim
    native = case_dir / ("owned-" + item.native_thread_id + ".jsonl")

    def claimed_then_foreign_active(*arguments, **kwargs):
        result = original_claim(*arguments, **kwargs)
        append_lines(native, codex_turn(item, "appeared-after-claim", model="gpt-6-luna", complete=False))
        return result

    remote.claim = claimed_then_foreign_active
    assert office_cli_host.main(args) == 0
    assert json.loads(capsys.readouterr().out)["outcomes"][0]["status"] == "not_idle"
    assert len(remote.claim_calls) == 1
    assert not remote.start_calls and not calls


@pytest.mark.skipif(os.name != "nt", reason="Actual entrypoint requires Windows")
def test_entrypoint_quarantined_profile_cannot_claim_new_dispatch(monkeypatch, case_dir, capsys):
    _, remote, calls, args = prepare(monkeypatch, case_dir, observed="gpt-6-sol")
    assert office_cli_host.main(args) == 0
    capsys.readouterr()
    remote.row = {**remote.row, "dispatch_id": "c" * 32}
    assert office_cli_host.main(args) == 0
    assert json.loads(capsys.readouterr().out)["outcomes"] == [
        {"dispatch_id": "c" * 32, "status": "profile_quarantined"}]
    assert len(calls) == len(remote.claim_calls) == len(remote.start_calls) == 1
