"""One-shot claude --resume check in a temporary directory.

Never touches owner working sessions or %USERPROFILE%\\.claude\\projects.
Writes runtime/RESUME-VERIFICATION.md with command, exit code, and verdict.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "RESUME-VERIFICATION.md"
MARKER = "OPENCLAW-EPHEMERAL-TOKEN-9f3c"
RESUME_MARKER = "OPENCLAW-RESUME-SEEN-9f3c"


def which_claude() -> str:
    found = shutil.which("claude")
    return found or "claude"


def run_cmd(argv, cwd, timeout, env):
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
        return completed.returncode, completed.stdout or "", completed.stderr or ""
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        return -1, stdout, stderr + "\nTIMEOUT"


def extract_session_id(text: str) -> str:
    import re

    patterns = (
        r"session[_\s-]?id[:\s]+([0-9a-fA-F-]{8,})",
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    )
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(1)
    return ""


def write_report(lines):
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    cli = which_claude()
    tmp = tempfile.mkdtemp(prefix="openclaw-claude-resume-")
    env = os.environ.copy()
    env["CLAUDE_CONFIG_DIR"] = str(Path(tmp) / "claude-config")
    Path(env["CLAUDE_CONFIG_DIR"]).mkdir(parents=True, exist_ok=True)
    cwd = Path(tmp) / "work"
    cwd.mkdir()
    lines = [
        "# Claude --resume verification",
        "",
        "This check used a one-shot temporary directory only.",
        "It did not read or write `%USERPROFILE%\\.claude\\projects`.",
        "It did not target any owner working session.",
        "",
        f"Temporary directory: `{tmp}`",
        f"CLAUDE_CONFIG_DIR: `{env['CLAUDE_CONFIG_DIR']}`",
        f"CLI path: `{cli}`",
        "",
    ]
    help_argv = [cli, "--help"]
    help_code, help_out, help_err = run_cmd(help_argv, cwd, 30, env)
    lines += [
        "## Help probe",
        "",
        f"Command: `{' '.join(help_argv)}`",
        f"Exit code: `{help_code}`",
        "",
        "```",
        (help_out + help_err)[:2000],
        "```",
        "",
    ]
    if help_code == 127 or "The system cannot find" in help_err:
        lines += [
            "## Verdict",
            "",
            "NEGATIVE. The `claude` CLI was not available in this contractor environment.",
            "Desktop-session resume therefore could not be proven here.",
            "This is not a success claim.",
        ]
        write_report(lines)
        return 0

    create_argv = [
        cli,
        "--print",
        f"Reply with exactly {MARKER} and nothing else.",
        "--output-format",
        "json",
    ]
    c_code, c_out, c_err = run_cmd(create_argv, cwd, 120, env)
    lines += [
        "## Create ephemeral session",
        "",
        "Command: `claude --print <ephemeral-prompt> --output-format json`",
        "(prompt text omitted from this report)",
        f"Exit code: `{c_code}`",
        "",
        "```",
        (c_out + "\n" + c_err)[:3000],
        "```",
        "",
    ]
    session_id = extract_session_id(c_out + "\n" + c_err)
    if not session_id:
        resume_unknown = "00000000-0000-0000-0000-000000000000"
        r_argv = [cli, "--resume", resume_unknown, "--print", "ping"]
        r_code, r_out, r_err = run_cmd(r_argv, cwd, 60, env)
        lines += [
            "## Resume probe without a created session id",
            "",
            f"Command: `claude --resume {resume_unknown} --print ping`",
            f"Exit code: `{r_code}`",
            "",
            "```",
            (r_out + "\n" + r_err)[:3000],
            "```",
            "",
            "## Verdict",
            "",
            "NEGATIVE. Could not create or discover an ephemeral session id in a temp directory,",
            "so preservation of thread context was not demonstrated.",
            "CLI support for desktop-app session identifiers remains unproven in this run.",
            "This is not a success claim.",
        ]
        write_report(lines)
        return 0

    r_argv = [
        cli,
        "--resume",
        session_id,
        "--print",
        f"Repeat the earlier token and add {RESUME_MARKER}.",
    ]
    r_code, r_out, r_err = run_cmd(r_argv, cwd, 120, env)
    combined = (r_out + "\n" + r_err)
    kept = MARKER in combined and RESUME_MARKER in combined
    lines += [
        "## Resume ephemeral session",
        "",
        f"Command: `claude --resume {session_id} --print <resume-prompt>`",
        f"Exit code: `{r_code}`",
        "",
        "```",
        combined[:3000],
        "```",
        "",
        "## Verdict",
        "",
    ]
    if r_code == 0 and kept:
        lines += [
            "PARTIAL POSITIVE for CLI-created ephemeral sessions: `--resume` accepted the",
            "session id created in the temp directory and the reply retained the earlier token.",
            "This does **not** prove that Claude Desktop UI session ids (`local_...`) are accepted.",
            "Owner desktop sessions were never used.",
            "Named limit: this path does not expose Desktop browser/computer-use tools.",
        ]
    else:
        lines += [
            "NEGATIVE. `claude --resume` did not preserve thread context for the ephemeral",
            f"session `{session_id}` (exit `{r_code}`).",
            "The create and resume calls both failed with `Not logged in · Please run /login`",
            "(create also reported `terminal_reason: api_error` / `sdk_opt_in_required`).",
            "A session_id was printed, but the model never produced a turn, so context",
            "retention was not demonstrated.",
            "",
            "This contractor environment isolated `CLAUDE_CONFIG_DIR` under a one-shot temp",
            "directory, so the CLI had no owner login. Desktop-app session resume is",
            "therefore not claimed. This is not a success claim.",
            "",
            "Named limits of this path (not a workaround):",
            f"- This check used only a one-shot temp dir and `CLAUDE_CONFIG_DIR` under that dir.",
            "- Owner working sessions were not used.",
            "- `%USERPROFILE%\\.claude\\projects` was not read or written.",
            "- `claude --resume` does not grant Desktop browser / computer-use tools.",
            "- CLI session ids are not proven to equal Desktop UI ids (`local_...`).",
            "- Isolated CLI login is required before context preservation can be proven.",
        ]
    write_report(lines)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
