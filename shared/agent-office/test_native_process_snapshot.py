"""Synthetic-only Windows argv and Codex process identity tests; no CLI execution."""

from __future__ import annotations

import pytest

from coordfs.native_process_snapshot import (
    classify_codex_process,
    reduce_snapshot,
    split_windows_argv,
)


TARGET = "11111111-2222-3333-4444-555555555555"
OTHER = "99999999-aaaa-bbbb-cccc-dddddddddddd"


@pytest.mark.parametrize(
    ("command_line", "expected"),
    [
        ('codex.exe "hello world"', ["codex.exe", "hello world"]),
        ('codex.exe "say ""hi"""', ["codex.exe", 'say "hi"']),
        ('codex.exe "say \\"hi\\""', ["codex.exe", 'say "hi"']),
        ('codex.exe "" word', ["codex.exe", "", "word"]),
        ('codex.exe "zażółć gęślą"', ["codex.exe", "zażółć gęślą"]),
        ('codex.exe "unclosed quote', ["codex.exe", "unclosed quote"]),
        ('codex.exe --model=gpt-6-sol', ["codex.exe", "--model=gpt-6-sol"]),
        ('codex.exe C:\\temp\\', ["codex.exe", "C:\\temp\\"]),
        ('  codex.exe\tresume', ["", "codex.exe", "resume"]),
        ('codex.exe "a' + '\\' * 2 + '" b', ["codex.exe", "a\\", "b"]),
        ('codex.exe "a' + '\\' * 3 + '" b"', ["codex.exe", 'a\\" b']),
    ],
)
def test_synthetic_split_windows_argv(command_line: str, expected: list[str]) -> None:
    assert split_windows_argv(command_line) == expected


@pytest.mark.parametrize(
    ("command_line", "expected"),
    [
        (f"codex.exe exec resume {TARGET}", "target"),
        (f"codex e resume {TARGET.upper()}", "target"),
        (f"codex resume {TARGET}", "target"),
        (f"codex.exe exec resume {OTHER}", "other"),
        (f'codex resume --all -m gpt-6-sol {OTHER} "mention {TARGET}"', "other"),
        (f"codex exec resume -c owner={TARGET} {OTHER}", "other"),
        (f"codex exec resume --output-last-message {TARGET} {OTHER}", "other"),
        (f"codex exec resume --model=gpt-6-sol {TARGET}", "target"),
        (f"codex exec resume --image first.png --image second.png {OTHER}", "other"),
        (f"codex resume -a never {OTHER}", "other"),
        (f"codex exec --json resume --thread-source office {OTHER}", "other"),
        (f"codex exec resume --last {TARGET}", "ambiguous"),
        (f"codex exec resume {OTHER} '--last in prompt'", "ambiguous"),
        (f"codex exec fork {TARGET}", "ambiguous"),
        (f"codex fork {TARGET}", "ambiguous"),
        (f'codex exec "mention {TARGET}"', "ambiguous"),
        (f'codex "mention {TARGET}"', "ambiguous"),
        (f"codex exec resume -c owner={TARGET}", "ambiguous"),
        (f"codex exec resume thread-name", "ambiguous"),
        ("codex resume", "ambiguous"),
        (f"codex exec resume --tag run {TARGET}", "ambiguous"),
        (f"codex exec resume --output-last-msg file {TARGET}", "ambiguous"),
        (f"codex --worktree exec resume {TARGET}", "ambiguous"),
        (f"codex --help exec resume {TARGET}", "ambiguous"),
        (f"codex exec resume -mgpt-6-sol {TARGET}", "ambiguous"),
        (f"codex exec --image first.png resume {TARGET}", "ambiguous"),
        (f"codex resume --image first.png {TARGET}", "ambiguous"),
        (f"codex exec resume {TARGET} extra prompt", "ambiguous"),
        (f'codex exec resume {TARGET} "unclosed', "ambiguous"),
        (f"codex app-server {TARGET}", "ambiguous"),
        (f"codex agents {TARGET}", "ambiguous"),
        (f"codex queue {TARGET}", "ambiguous"),
        (f"codex review {TARGET}", "ambiguous"),
        (f"codex apply {TARGET}", "ambiguous"),
        (f"not-codex.exe exec resume {TARGET}", "ambiguous"),
        ("", "ambiguous"),
        ("codex " + "x" * 32768, "ambiguous"),
    ],
    ids=lambda value: "oversized-command" if isinstance(value, str) and len(value) > 1000 else None,
)
def test_synthetic_codex_process_truth_table(command_line: str, expected: str) -> None:
    assert classify_codex_process(command_line, target_native_id=TARGET) == expected


def test_synthetic_realistic_command_from_codex_01561_help() -> None:
    # Full Windows executable path and options documented by exec and exec resume help.
    command_line = (
        '"C:\\Synthetic Tools\\Codex\\codex.exe" '
        'exec --json resume -c model="gpt-6-sol" -m gpt-6-sol '
        f'--output-last-message result.txt {OTHER} "Continue the task"'
    )
    assert classify_codex_process(command_line, target_native_id=TARGET) == "other"


@pytest.mark.parametrize(
    ("verdicts", "expected"),
    [
        ([], "clear"),
        (["other", "other"], "clear"),
        (["other", "ambiguous"], "unknown"),
        (["ambiguous", "target", "other"], "busy"),
        (["target", "ambiguous"], "busy"),
        (["other", "unexpected"], "unknown"),
    ],
)
def test_synthetic_reduce_snapshot_ordering(verdicts: list[str], expected: str) -> None:
    assert reduce_snapshot(verdicts) == expected
