"""Synthetic Windows process snapshots; no native CLI or Office service is started."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import office_cli_host as host


TARGET = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
CODEX = r"C:\cli\codex.exe"
CLAUDE = r"C:\cli\claude.exe"


def row(path: str | None, command: str | None) -> dict[str, str | None]:
    return {"ExecutablePath": path, "CommandLine": command}


def classify(commands: list[str], *, evidence: bool = True) -> str:
    return host.classify_native_processes(
        [row(CODEX, command) for command in commands], target=TARGET,
        executable=CODEX, terminal_evidence=evidence,
    )


def test_synthetic_foreign_resumes_and_prompt_only_target_allow_idle() -> None:
    commands = [f"codex.exe exec resume {OTHER} --json",
                f'codex.exe exec resume {OTHER} "prompt mentioning {TARGET}"']
    assert classify(commands) == "idle"
    assert classify(commands, evidence=False) == "unknown"


def test_synthetic_target_resume_stays_busy_with_or_without_terminal_evidence() -> None:
    command = f"codex.exe exec resume {TARGET} --json"
    assert classify([command]) == "busy"
    assert classify([command], evidence=False) == "busy"
    assert classify([f"codex.exe exec resume {OTHER}", command]) == "busy"


@pytest.mark.parametrize("command", [
    "codex.exe exec do work",
    "codex.exe exec resume --last",
    f"codex.exe exec fork {TARGET}",
    "codex.exe app-server",
    f"codex.exe --unknown-option exec resume {TARGET}",
    f'codex.exe exec resume {OTHER} "unclosed prompt',
])
def test_synthetic_ambiguous_codex_commands_never_allow_idle(command: str) -> None:
    assert classify([command]) == "unknown"


def test_synthetic_missing_metadata_and_snapshot_bounds_fail_closed() -> None:
    assert host.classify_native_processes(
        [row(CODEX, None)], target=TARGET, executable=CODEX,
        terminal_evidence=True) == "unknown"
    assert host.classify_native_processes(
        [row(None, f"codex.exe exec resume {OTHER}")], target=TARGET,
        executable=CODEX, terminal_evidence=True) == "unknown"
    assert classify([f"codex.exe exec resume {OTHER}"] * 65) == "unknown"
    assert classify([f'codex.exe exec resume {OTHER} "' + "x" * 32768 + '"']) == "unknown"


def test_synthetic_claude_keeps_raw_token_semantics_without_argv_parser(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host, "classify_codex_process",
                        lambda *args, **kwargs: pytest.fail("Claude used Codex parser"))
    target_command = f'claude.exe --prompt "text containing {TARGET}"'
    assert host.classify_native_processes(
        [row(CLAUDE, target_command)], target=TARGET, executable=CLAUDE,
        terminal_evidence=True) == "busy"
    assert host.classify_native_processes(
        [row(CLAUDE, f"claude.exe --resume {OTHER}")], target=TARGET,
        executable=CLAUDE, terminal_evidence=True) == "unknown"
    assert host.classify_native_processes(
        [], target=TARGET, executable=CLAUDE,
        terminal_evidence=True) == "idle"


def synthetic_probe(monkeypatch: pytest.MonkeyPatch, *, driver: str = "codex-cli"):
    """Return an enrollment and a mutable synthetic PowerShell result."""
    monkeypatch.setattr(host, "os", SimpleNamespace(name="nt", environ={}))
    enrollment = SimpleNamespace(
        driver=driver, native_thread_id=TARGET, binding_id="synthetic",
        executable=CODEX if driver == "codex-cli" else CLAUDE,
    )
    result = SimpleNamespace(stdout="[]", returncode=0)
    calls: list[dict] = []

    def fake_run(_argv, **kwargs):
        calls.append(kwargs)
        return result

    monkeypatch.setattr(host.subprocess, "run", fake_run)
    monkeypatch.setattr(host, "native_terminal_observed", lambda *_: True)
    return enrollment, result, calls


def test_synthetic_owned_probe_foreign_peers_clear_and_target_busy(
        monkeypatch: pytest.MonkeyPatch) -> None:
    enrollment, result, calls = synthetic_probe(monkeypatch)
    result.stdout = json.dumps([f"codex.exe exec resume {OTHER} --json",
                                f'codex.exe exec resume {OTHER} "{TARGET} in prompt"'])
    assert host.owned_session_idle(enrollment, evidence={"synthetic": Path("synthetic.jsonl")}) is True
    assert calls[-1]["timeout"] == 15
    result.stdout = json.dumps([f"codex.exe exec resume {TARGET} --json"])
    assert host.owned_session_idle(enrollment, evidence={"synthetic": Path("synthetic.jsonl")}) is False


@pytest.mark.parametrize("output", [
    "unknown", "not JSON", "{}", '"busy"', '[null]', "\ud800", json.dumps(["x"] * 65),
    json.dumps(["x" * 32768]),
    json.dumps(["codex.exe exec resume --last"]),
], ids=["unknown", "malformed", "object", "scalar", "null-row", "bad-unicode", "too-many",
        "too-large", "ambiguous"])
def test_synthetic_probe_bad_or_ambiguous_snapshot_is_unknown(
        monkeypatch: pytest.MonkeyPatch, output: str) -> None:
    enrollment, result, _ = synthetic_probe(monkeypatch)
    result.stdout = output
    assert host.owned_session_idle(enrollment, evidence={"synthetic": Path("synthetic.jsonl")}) is None


def test_synthetic_probe_nonzero_timeout_and_decode_error_are_unknown(
        monkeypatch: pytest.MonkeyPatch) -> None:
    enrollment, result, _ = synthetic_probe(monkeypatch)
    result.returncode = 1
    assert host.owned_session_idle(enrollment) is None
    for error in (subprocess.TimeoutExpired("powershell.exe", 15),
                  OSError("synthetic failure"), UnicodeError("synthetic decode failure")):
        def fail(*_args, **_kwargs):
            raise error
        monkeypatch.setattr(host.subprocess, "run", fail)
        assert host.owned_session_idle(enrollment) is None


def test_synthetic_claude_probe_preserves_legacy_outputs(
        monkeypatch: pytest.MonkeyPatch) -> None:
    enrollment, result, _ = synthetic_probe(monkeypatch, driver="claude-code-cli")
    monkeypatch.setattr(host, "classify_codex_process",
                        lambda *args, **kwargs: pytest.fail("Claude used Codex parser"))
    result.stdout = "busy\n"
    assert host.owned_session_idle(enrollment) is False
    result.stdout = "unknown\n"
    assert host.owned_session_idle(enrollment) is None
    result.stdout = "clear\n"
    assert host.owned_session_idle(enrollment, evidence={"synthetic": Path("synthetic.jsonl")}) is True
