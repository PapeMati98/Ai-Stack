"""Synthetic H03 tests: no native model, provider, or credential is ever used."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

import pytest

from coordfs.cli_host import HostEnrollment
from coordfs.native_model_evidence import NativeModelObserver

DATA = Path(__file__).resolve().parent / "host-test-data"
SENTINEL_PROMPT = "SENTINEL-PROMPT-7f3a9c2e"
SENTINEL_RESPONSE = "SENTINEL-RESPONSE-4b1d88f0"


@pytest.fixture
def workdir():
    # Retained synthetic fixtures under this worktree; never cleaned up here.
    path = DATA / ("native-evidence-" + uuid.uuid4().hex)
    path.mkdir(parents=True)
    return path


def make_enrollment(workdir: Path, *, binding_id: str = "ev-one",
                    native_id: str = "thread-ev-one",
                    driver: str = "codex-cli") -> HostEnrollment:
    workspace = workdir / "workspace"
    workspace.mkdir(exist_ok=True)
    executable = workdir / ("codex.exe" if driver == "codex-cli" else "claude.exe")
    executable.touch(exist_ok=True)
    return HostEnrollment(binding_id, 1, "ailab", "codex-actor", driver,
                          native_id, workspace, executable)


def journal_for(workdir: Path, enrollment: HostEnrollment) -> Path:
    if enrollment.driver == "codex-cli":
        return workdir / ("sess-" + enrollment.native_thread_id + ".jsonl")
    return workdir / (enrollment.native_thread_id + ".jsonl")


def write_lines(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, separators=(",", ":")) + "\n")


def append_lines(path: Path, rows: list[dict]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, separators=(",", ":")) + "\n")


def codex_meta(enrollment: HostEnrollment) -> dict:
    return {"type": "session_meta",
            "payload": {"id": enrollment.native_thread_id,
                        "cwd": str(enrollment.workspace), "source": "exec"}}


def codex_turn(enrollment: HostEnrollment, turn_id: str, *, model: str | None,
               complete: bool = True, workspace: str | None = None) -> list[dict]:
    rows = [{"type": "event_msg",
             "payload": {"type": "task_started", "turn_id": turn_id}}]
    if model is not None:
        rows.append({"type": "turn_context",
                     "payload": {"turn_id": turn_id,
                                 "cwd": workspace or str(enrollment.workspace),
                                 "model": model}})
    if complete:
        rows.append({"type": "event_msg",
                     "payload": {"type": "task_complete", "turn_id": turn_id}})
    return rows


def claude_user(enrollment: HostEnrollment, uid: str, *,
                session: str | None = None, workspace: str | None = None) -> dict:
    return {"type": "user",
            "sessionId": session or enrollment.native_thread_id,
            "cwd": workspace or str(enrollment.workspace), "uuid": uid,
            "message": {"role": "user", "text": SENTINEL_PROMPT + "-" + uid}}


def claude_assistant(enrollment: HostEnrollment, aid: str, parent: str, *,
                     model: Any = "claude-sonnet-5", stop: str = "end_turn",
                     tool_use: bool = False, session: str | None = None) -> dict:
    content: list[dict] = [{"type": "text",
                            "text": SENTINEL_RESPONSE + "-" + aid}]
    if tool_use:
        content.append({"type": "tool_use", "name": "synthetic"})
    message: dict[str, Any] = {"role": "assistant", "id": "prov-" + aid,
                               "stop_reason": stop, "content": content}
    if model is not None:
        message["model"] = model
    return {"type": "assistant",
            "sessionId": session or enrollment.native_thread_id,
            "cwd": str(enrollment.workspace), "uuid": aid, "parentUuid": parent,
            "message": message}


# Group 1: Codex old turn -> arm -> NEW completed turn observes actual model.
def test_codex_fresh_turn_returns_actual_model(workdir: Path) -> None:
    enrollment = make_enrollment(workdir)
    journal = journal_for(workdir, enrollment)
    write_lines(journal, [codex_meta(enrollment),
                          *codex_turn(enrollment, "turn-old", model="gpt-6-luna")])
    observer = NativeModelObserver({enrollment.binding_id: journal})
    assert observer.arm(enrollment) is True
    append_lines(journal, codex_turn(enrollment, "turn-new", model="gpt-6-sol-nova"))
    assert observer.observe(enrollment) == "gpt-6-sol-nova"


# Group 2: Claude old terminal -> arm -> fresh user/assistant terminal.
def test_codex_turn_active_before_arm_cannot_be_fresh_proof(workdir: Path) -> None:
    enrollment = make_enrollment(workdir)
    journal = journal_for(workdir, enrollment)
    write_lines(journal, [codex_meta(enrollment),
                         *codex_turn(enrollment, "old", model="gpt-6-luna"),
                         *codex_turn(enrollment, "already-running", model="gpt-6-sol", complete=False)])
    observer = NativeModelObserver({enrollment.binding_id: journal})
    assert observer.arm(enrollment) is False
    append_lines(journal, [{"type": "event_msg", "payload": {
        "type": "task_complete", "turn_id": "already-running"}}])
    assert observer.observe(enrollment) is None
    # A subsequent owned terminal baseline still permits a genuinely new turn.
    assert observer.arm(enrollment) is True
    append_lines(journal, codex_turn(enrollment, "really-new", model="gpt-6-luna"))
    assert observer.observe(enrollment) == "gpt-6-luna"


def test_failed_rearm_clears_previously_armed_proof(workdir: Path) -> None:
    enrollment = make_enrollment(workdir)
    journal = journal_for(workdir, enrollment)
    write_lines(journal, [codex_meta(enrollment),
                         *codex_turn(enrollment, "old", model="gpt-6-luna")])
    observer = NativeModelObserver({enrollment.binding_id: journal})
    assert observer.arm(enrollment) is True
    append_lines(journal, codex_turn(enrollment, "active", model="gpt-6-sol", complete=False))
    assert observer.arm(enrollment) is False
    append_lines(journal, [{"type": "event_msg", "payload": {
        "type": "task_complete", "turn_id": "active"}}])
    assert observer.observe(enrollment) is None


def test_baseline_read_race_is_refused(workdir: Path, monkeypatch) -> None:
    from coordfs import native_model_evidence as module
    enrollment = make_enrollment(workdir)
    journal = journal_for(workdir, enrollment)
    write_lines(journal, [codex_meta(enrollment),
                         *codex_turn(enrollment, "old", model="gpt-6-luna")])
    original = module._helpers()
    def raced_read(path):
        result = original[0](path)
        append_lines(path, codex_turn(enrollment, "raced", model="gpt-6-sol"))
        return result
    monkeypatch.setattr(module, "_helpers", lambda: (raced_read, *original[1:]))
    observer = NativeModelObserver({enrollment.binding_id: journal})
    assert observer.arm(enrollment) is False
    assert observer.observe(enrollment) is None


def test_claude_fresh_terminal_returns_actual_model(workdir: Path) -> None:
    enrollment = make_enrollment(workdir, binding_id="ev-two",
                                 native_id="thread-ev-two",
                                 driver="claude-code-cli")
    journal = journal_for(workdir, enrollment)
    write_lines(journal, [claude_user(enrollment, "user-old"),
                          claude_assistant(enrollment, "asst-old", "user-old",
                                           model="claude-sonnet-4")])
    observer = NativeModelObserver({enrollment.binding_id: journal})
    assert observer.arm(enrollment) is True
    append_lines(journal, [claude_user(enrollment, "user-new"),
                           claude_assistant(enrollment, "asst-new", "user-new",
                                            model="claude-sonnet-5")])
    assert observer.observe(enrollment) == "claude-sonnet-5"
    assert set(name for name in dir(observer) if not name.startswith("_")) == \
        {"arm", "observe"}


# Group 3: negative matrix -> None, one-shot arm never reusable.
def test_observe_without_arm_returns_none(workdir: Path) -> None:
    enrollment = make_enrollment(workdir)
    journal = journal_for(workdir, enrollment)
    write_lines(journal, [codex_meta(enrollment),
                          *codex_turn(enrollment, "turn-a", model="gpt-6-luna")])
    assert NativeModelObserver({enrollment.binding_id: journal}).observe(
        enrollment) is None


def test_missing_map_file_and_unarmed_enrollment(workdir: Path) -> None:
    enrollment = make_enrollment(workdir)
    journal = journal_for(workdir, enrollment)
    write_lines(journal, [codex_meta(enrollment),
                          *codex_turn(enrollment, "turn-a", model="gpt-6-luna")])
    assert NativeModelObserver({}).arm(enrollment) is False
    assert NativeModelObserver({}).observe(enrollment) is None
    absent = make_enrollment(workdir, binding_id="ev-absent",
                             native_id="thread-ev-absent")
    absent_journal = journal_for(workdir, absent)
    assert not absent_journal.exists()
    assert NativeModelObserver(
        {absent.binding_id: absent_journal}).arm(absent) is False
    foreign_name = workdir / "other-thread.jsonl"
    write_lines(foreign_name, [codex_meta(enrollment),
                               *codex_turn(enrollment, "turn-a",
                                            model="gpt-6-luna")])
    assert NativeModelObserver(
        {enrollment.binding_id: foreign_name}).arm(enrollment) is False
    other = make_enrollment(workdir, binding_id="ev-other",
                            native_id="thread-ev-other")
    observer = NativeModelObserver({enrollment.binding_id: journal})
    assert observer.arm(enrollment) is True
    assert observer.observe(other) is None


def test_unchanged_journal_returns_none_and_consumes_arm(workdir: Path) -> None:
    enrollment = make_enrollment(workdir)
    journal = journal_for(workdir, enrollment)
    write_lines(journal, [codex_meta(enrollment),
                          *codex_turn(enrollment, "turn-a", model="gpt-6-luna")])
    observer = NativeModelObserver({enrollment.binding_id: journal})
    assert observer.arm(enrollment) is True
    assert observer.observe(enrollment) is None
    assert observer.observe(enrollment) is None


def test_codex_new_completion_without_model_returns_none(workdir: Path) -> None:
    enrollment = make_enrollment(workdir)
    journal = journal_for(workdir, enrollment)
    write_lines(journal, [codex_meta(enrollment),
                          *codex_turn(enrollment, "turn-a", model="gpt-6-luna")])
    observer = NativeModelObserver({enrollment.binding_id: journal})
    assert observer.arm(enrollment) is True
    append_lines(journal, codex_turn(enrollment, "turn-b", model=None))
    assert observer.observe(enrollment) is None


def test_claude_new_assistant_without_model_returns_none(workdir: Path) -> None:
    enrollment = make_enrollment(workdir, binding_id="ev-c2",
                                 native_id="thread-ev-c2",
                                 driver="claude-code-cli")
    journal = journal_for(workdir, enrollment)
    write_lines(journal, [claude_user(enrollment, "user-a"),
                          claude_assistant(enrollment, "asst-a", "user-a")])
    observer = NativeModelObserver({enrollment.binding_id: journal})
    assert observer.arm(enrollment) is True
    append_lines(journal, [claude_user(enrollment, "user-b"),
                           claude_assistant(enrollment, "asst-b", "user-b",
                                            model=None)])
    assert observer.observe(enrollment) is None


def test_codex_conflicting_models_returns_none(workdir: Path) -> None:
    enrollment = make_enrollment(workdir)
    journal = journal_for(workdir, enrollment)
    write_lines(journal, [codex_meta(enrollment),
                          *codex_turn(enrollment, "turn-a", model="gpt-6-luna")])
    observer = NativeModelObserver({enrollment.binding_id: journal})
    assert observer.arm(enrollment) is True
    rows = codex_turn(enrollment, "turn-b", model="gpt-6-luna")
    rows.insert(1, {"type": "turn_context",
                    "payload": {"turn_id": "turn-b",
                                "cwd": str(enrollment.workspace),
                                "model": "gpt-6-other"}})
    append_lines(journal, rows)
    assert observer.observe(enrollment) is None


def test_codex_wrong_latest_turn_without_proof_returns_none(workdir: Path) -> None:
    enrollment = make_enrollment(workdir)
    journal = journal_for(workdir, enrollment)
    write_lines(journal, [codex_meta(enrollment),
                          *codex_turn(enrollment, "turn-a", model="gpt-6-luna")])
    observer = NativeModelObserver({enrollment.binding_id: journal})
    assert observer.arm(enrollment) is True
    append_lines(journal, codex_turn(enrollment, "turn-b", model=None))
    assert observer.observe(enrollment) is None


def test_foreign_session_workspace_returns_none(workdir: Path) -> None:
    enrollment = make_enrollment(workdir)
    journal = journal_for(workdir, enrollment)
    write_lines(journal, [codex_meta(enrollment),
                          *codex_turn(enrollment, "turn-a", model="gpt-6-luna")])
    observer = NativeModelObserver({enrollment.binding_id: journal})
    assert observer.arm(enrollment) is True
    append_lines(journal, codex_turn(enrollment, "turn-b", model="gpt-6-luna",
                                     workspace=str(workdir / "elsewhere")))
    assert observer.observe(enrollment) is None

    claude = make_enrollment(workdir, binding_id="ev-c3",
                             native_id="thread-ev-c3",
                             driver="claude-code-cli")
    claude_journal = journal_for(workdir, claude)
    write_lines(claude_journal, [claude_user(claude, "user-a"),
                                 claude_assistant(claude, "asst-a", "user-a")])
    claude_observer = NativeModelObserver({claude.binding_id: claude_journal})
    assert claude_observer.arm(claude) is True
    append_lines(claude_journal, [claude_user(claude, "user-b"),
                                  claude_assistant(claude, "asst-b", "user-b",
                                                   session="thread-foreign")])
    assert claude_observer.observe(claude) is None


def test_unfinished_turn_returns_none(workdir: Path) -> None:
    enrollment = make_enrollment(workdir)
    journal = journal_for(workdir, enrollment)
    write_lines(journal, [codex_meta(enrollment),
                          *codex_turn(enrollment, "turn-a", model="gpt-6-luna")])
    observer = NativeModelObserver({enrollment.binding_id: journal})
    assert observer.arm(enrollment) is True
    append_lines(journal, codex_turn(enrollment, "turn-b", model="gpt-6-luna",
                                     complete=False))
    assert observer.observe(enrollment) is None

    claude = make_enrollment(workdir, binding_id="ev-c4",
                             native_id="thread-ev-c4",
                             driver="claude-code-cli")
    claude_journal = journal_for(workdir, claude)
    write_lines(claude_journal, [claude_user(claude, "user-a"),
                                 claude_assistant(claude, "asst-a", "user-a")])
    claude_observer = NativeModelObserver({claude.binding_id: claude_journal})
    assert claude_observer.arm(claude) is True
    append_lines(claude_journal, [claude_user(claude, "user-b"),
                                  claude_assistant(claude, "asst-b", "user-b",
                                                   stop="in_progress")])
    assert claude_observer.observe(claude) is None
    append_lines(claude_journal, [claude_user(claude, "user-c"),
                                  claude_assistant(claude, "asst-c", "user-c",
                                                   tool_use=True)])
    assert claude_observer.arm(claude) is False  # latest turn is still active
    assert claude_observer.observe(claude) is None


@pytest.mark.parametrize("bad", ["", "has space", "tab\there", "líne",
                                  "x" * 129, " bad", "bad "])
def test_codex_invalid_model_identifier_returns_none(workdir: Path,
                                                     bad: str) -> None:
    enrollment = make_enrollment(workdir)
    journal = journal_for(workdir, enrollment)
    write_lines(journal, [codex_meta(enrollment),
                          *codex_turn(enrollment, "turn-a", model="gpt-6-luna")])
    observer = NativeModelObserver({enrollment.binding_id: journal})
    assert observer.arm(enrollment) is True
    append_lines(journal, codex_turn(enrollment, "turn-b", model=bad))
    assert observer.observe(enrollment) is None


def test_one_shot_arm_cannot_be_reused_after_success(workdir: Path) -> None:
    enrollment = make_enrollment(workdir)
    journal = journal_for(workdir, enrollment)
    write_lines(journal, [codex_meta(enrollment),
                          *codex_turn(enrollment, "turn-a", model="gpt-6-luna")])
    observer = NativeModelObserver({enrollment.binding_id: journal})
    assert observer.arm(enrollment) is True
    append_lines(journal, codex_turn(enrollment, "turn-b", model="gpt-6-sol-nova"))
    assert observer.observe(enrollment) == "gpt-6-sol-nova"
    assert observer.observe(enrollment) is None
    assert observer.arm(enrollment) is True
    assert observer.observe(enrollment) is None


# Group 4: replacement, truncation, symlink/reparse, torn write -> None.
def test_truncated_journal_returns_none(workdir: Path) -> None:
    enrollment = make_enrollment(workdir)
    journal = journal_for(workdir, enrollment)
    write_lines(journal, [codex_meta(enrollment),
                          *codex_turn(enrollment, "turn-a", model="gpt-6-luna"),
                          *codex_turn(enrollment, "turn-b", model="gpt-6-sol")])
    observer = NativeModelObserver({enrollment.binding_id: journal})
    assert observer.arm(enrollment) is True
    write_lines(journal, [codex_meta(enrollment),
                          *codex_turn(enrollment, "turn-a", model="gpt-6-luna")])
    assert observer.observe(enrollment) is None


def test_replaced_journal_returns_none(workdir: Path) -> None:
    enrollment = make_enrollment(workdir)
    journal = journal_for(workdir, enrollment)
    write_lines(journal, [codex_meta(enrollment),
                          *codex_turn(enrollment, "turn-a", model="gpt-6-luna")])
    observer = NativeModelObserver({enrollment.binding_id: journal})
    assert observer.arm(enrollment) is True
    journal.unlink()
    write_lines(journal, [codex_meta(enrollment),
                          *codex_turn(enrollment, "turn-a", model="gpt-6-luna"),
                          *codex_turn(enrollment, "turn-b", model="gpt-6-sol"),
                          *codex_turn(enrollment, "turn-c", model="gpt-6-nova")])
    assert observer.observe(enrollment) is None


def test_symlink_journal_refused(workdir: Path) -> None:
    target = workdir / "real-sess-thread-ev-one.jsonl"
    enrollment = make_enrollment(workdir)
    journal = journal_for(workdir, enrollment)
    write_lines(target, [{"type": "session_meta",
                          "payload": {"id": enrollment.native_thread_id,
                                      "cwd": str(enrollment.workspace),
                                      "source": "exec"}}])
    try:
        os.symlink(target, journal)
    except OSError:
        pytest.skip("symlink creation unavailable")
    observer = NativeModelObserver({enrollment.binding_id: journal})
    assert observer.arm(enrollment) is False
    assert observer.observe(enrollment) is None


def test_torn_write_returns_none(workdir: Path) -> None:
    enrollment = make_enrollment(workdir)
    journal = journal_for(workdir, enrollment)
    write_lines(journal, [codex_meta(enrollment),
                          *codex_turn(enrollment, "turn-a", model="gpt-6-luna")])
    observer = NativeModelObserver({enrollment.binding_id: journal})
    assert observer.arm(enrollment) is True
    with journal.open("ab") as stream:
        stream.write(b'{"type": "event_msg", "payload": {"type": "task_start')
    assert observer.observe(enrollment) is None


# Group 5: large journal with an omitted gap.
def _codex_filler(count: int) -> list[dict]:
    return [{"type": "event_msg", "payload": {"type": "token_count",
                                              "tick": index}}
            for index in range(count)]


def test_codex_large_journal_accepts_fresh_tail_turn(workdir: Path) -> None:
    enrollment = make_enrollment(workdir)
    journal = journal_for(workdir, enrollment)
    write_lines(journal, [codex_meta(enrollment),
                          *codex_turn(enrollment, "turn-old", model="gpt-6-luna"),
                          *_codex_filler(7000),
                          *codex_turn(enrollment, "turn-idle-tail", model="gpt-6-luna")])
    assert journal.stat().st_size > 64 * 1024 + 256 * 1024
    observer = NativeModelObserver({enrollment.binding_id: journal})
    assert observer.arm(enrollment) is True
    append_lines(journal, codex_turn(enrollment, "turn-new", model="gpt-6-nova"))
    assert observer.observe(enrollment) == "gpt-6-nova"


def test_codex_large_journal_missing_tail_proof_returns_none(workdir: Path) -> None:
    enrollment = make_enrollment(workdir)
    journal = journal_for(workdir, enrollment)
    write_lines(journal, [codex_meta(enrollment),
                          *codex_turn(enrollment, "turn-old", model="gpt-6-luna")])
    observer = NativeModelObserver({enrollment.binding_id: journal})
    assert observer.arm(enrollment) is True
    append_lines(journal, [*codex_turn(enrollment, "turn-gap", model="gpt-6-x"),
                           *_codex_filler(7000)])
    append_lines(journal, [{"type": "event_msg",
                            "payload": {"type": "task_started",
                                        "turn_id": "turn-far"}},
                           {"type": "event_msg",
                            "payload": {"type": "task_complete",
                                        "turn_id": "turn-far"}}])
    assert journal.stat().st_size > 64 * 1024 + 256 * 1024
    assert observer.observe(enrollment) is None


def test_claude_large_journal_accepts_fresh_tail_pair(workdir: Path) -> None:
    enrollment = make_enrollment(workdir, binding_id="ev-c5",
                                 native_id="thread-ev-c5",
                                 driver="claude-code-cli")
    journal = journal_for(workdir, enrollment)
    filler = [{"type": "cost-state", "tick": index} for index in range(12000)]
    write_lines(journal, [claude_user(enrollment, "user-old"),
                          claude_assistant(enrollment, "asst-old", "user-old",
                                           model="claude-sonnet-4"),
                          *filler, claude_user(enrollment, "user-idle-tail"),
                          claude_assistant(enrollment, "asst-idle-tail", "user-idle-tail")])
    assert journal.stat().st_size > 64 * 1024 + 256 * 1024
    observer = NativeModelObserver({enrollment.binding_id: journal})
    assert observer.arm(enrollment) is True
    append_lines(journal, [claude_user(enrollment, "user-new"),
                           claude_assistant(enrollment, "asst-new", "user-new",
                                            model="claude-sonnet-5")])
    assert observer.observe(enrollment) == "claude-sonnet-5"


def test_claude_large_journal_missing_tail_parent_returns_none(
        workdir: Path) -> None:
    enrollment = make_enrollment(workdir, binding_id="ev-c6",
                                 native_id="thread-ev-c6",
                                 driver="claude-code-cli")
    journal = journal_for(workdir, enrollment)
    write_lines(journal, [claude_user(enrollment, "user-old"),
                          claude_assistant(enrollment, "asst-old", "user-old")])
    observer = NativeModelObserver({enrollment.binding_id: journal})
    assert observer.arm(enrollment) is True
    filler = [{"type": "cost-state", "tick": index} for index in range(12000)]
    append_lines(journal, [claude_user(enrollment, "user-far"), *filler,
                           claude_assistant(enrollment, "asst-far", "user-far")])
    assert journal.stat().st_size > 64 * 1024 + 256 * 1024
    assert observer.observe(enrollment) is None


# Group 6: no prompt/response retention; fixtures stay in this worktree.
def test_observer_state_holds_no_content(workdir: Path) -> None:
    enrollment = make_enrollment(workdir)
    journal = journal_for(workdir, enrollment)
    write_lines(journal, [codex_meta(enrollment),
                          *codex_turn(enrollment, "turn-a", model="gpt-6-luna")])
    journal_text = journal.read_text(encoding="utf-8")
    assert SENTINEL_PROMPT not in journal_text  # codex path keeps no content
    observer = NativeModelObserver({enrollment.binding_id: journal})
    assert observer.arm(enrollment) is True
    state = repr(vars(observer))
    assert SENTINEL_PROMPT not in state and SENTINEL_RESPONSE not in state

    claude = make_enrollment(workdir, binding_id="ev-c7",
                             native_id="thread-ev-c7",
                             driver="claude-code-cli")
    claude_journal = journal_for(workdir, claude)
    write_lines(claude_journal, [claude_user(claude, "user-a"),
                                 claude_assistant(claude, "asst-a", "user-a")])
    assert SENTINEL_PROMPT in claude_journal.read_text(encoding="utf-8")
    claude_observer = NativeModelObserver({claude.binding_id: claude_journal})
    assert claude_observer.arm(claude) is True
    assert claude_observer.observe(claude) is None  # unchanged journal
    state = repr(vars(claude_observer)) + repr(claude_observer._armed)
    assert SENTINEL_PROMPT not in state and SENTINEL_RESPONSE not in state
    assert journal.exists() and claude_journal.exists()
    assert DATA.exists() and workdir.exists()
