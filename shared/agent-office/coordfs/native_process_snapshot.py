"""Pure, conservative Codex CLI 0.156.1 Windows process classification.

Supported identity grammar (as printed by Codex CLI 0.156.1 ``codex --help``,
``exec --help``, ``exec resume --help`` and ``resume --help``):
``codex [OPTIONS] (exec|e) [OPTIONS] resume [OPTIONS] SESSION_ID [PROMPT]``
or ``codex [OPTIONS] resume [OPTIONS] SESSION_ID [PROMPT]``. Only the explicit
SESSION_ID positional in these two chains identifies a session. A canonical
UUID there can safely distinguish the target from a different execution;
thread-name aliases, absent IDs and all other command shapes cannot.

Value-taking options honored from that help, at their documented levels:
``-c/--config``, ``--enable``, ``--disable``, ``-i/--image``,
``-m/--model``, ``--local-provider``, ``-p/--profile``,
``-s/--sandbox``, ``-C/--cd``, ``--add-dir``,
``-a/--ask-for-approval``, ``--remote``, ``--remote-auth-token-env``,
``--thread-source``, ``--output-schema``, ``--color`` and
``-o/--output-last-message``. Each is honored only at levels where the help
lists it. The installed help spells out ``--ask-for-approval`` and
``--output-last-message``; abbreviations such as ``--ask-for`` and
``--output-last-msg`` and an undocumented ``--tag`` are unknown. Long
``--option=value`` is supported; combined/attached short options are
intentionally unsupported. Help lists ``--image <FILE>...`` at the top-level,
exec and interactive-resume levels, so an image immediately followed by a bare
token has no provable positional boundary and is ambiguous. Exec-resume help
lists one file per repeatable ``--image`` occurrence.

Fresh exec, fork, --last, app-server and all other commands intentionally
remain ambiguous. A UUID in a prompt, option value or config value is never
session identity. Unknown syntax fails closed. No I/O occurs in this module.
"""

from __future__ import annotations

import re
from collections.abc import Iterable


_UUID = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\Z")
_MAX_COMMAND_LINE = 32767

_COMMON_VALUE = {"-c", "--config", "--enable", "--disable", "-m", "--model"}
_COMMON_FLAG = {"--strict-config", "--dangerously-bypass-approvals-and-sandbox",
                "--dangerously-bypass-hook-trust"}
_INTERACTIVE_VALUE = {"-i", "--image", "--local-provider", "-p", "--profile",
                      "-s", "--sandbox", "-C", "--cd", "--add-dir",
                      "-a", "--ask-for-approval", "--remote", "--remote-auth-token-env"}
_INTERACTIVE_FLAG = {"--oss", "--approve-for-me", "--search",
                     "--no-alt-screen", "--no-daemon"}
_TOP_VALUE = _COMMON_VALUE | _INTERACTIVE_VALUE
_TOP_FLAG = _COMMON_FLAG | _INTERACTIVE_FLAG
_EXEC_VALUE = _COMMON_VALUE | {"-i", "--image", "--local-provider", "-p", "--profile",
                               "-s", "--sandbox", "-C", "--cd", "--add-dir",
                               "--thread-source", "--output-schema", "--color",
                               "-o", "--output-last-message"}
_EXEC_FLAG = _COMMON_FLAG | {"--oss", "--approve-for-me", "--json", "--ephemeral",
                             "--worktree", "--skip-git-repo-check", "--ignore-user-config",
                             "--ignore-rules"}
_EXEC_RESUME_VALUE = _COMMON_VALUE | {"-i", "--image", "--thread-source",
                                      "--output-schema", "-o", "--output-last-message"}
_EXEC_RESUME_FLAG = _COMMON_FLAG | {"--all", "--ephemeral", "--json", "--worktree",
                                   "--skip-git-repo-check", "--ignore-user-config",
                                   "--ignore-rules"}
_RESUME_VALUE = _TOP_VALUE
_RESUME_FLAG = _TOP_FLAG | {"--all", "--include-non-interactive"}


def _split_with_balance(command_line: str) -> tuple[list[str], bool]:
    """Return CommandLineToArgvW tokens and whether every quote was closed."""
    args: list[str] = []
    n = len(command_line)
    i = 0
    if i < n and command_line[i] in " \t":
        # CommandLineToArgvW reserves an empty first argument for leading space.
        args.append("")
    while i < n:
        while i < n and command_line[i] in " \t":
            i += 1
        if i == n:
            break
        parts: list[str] = []
        quoted = False
        while i < n and (quoted or command_line[i] not in " \t"):
            if command_line[i] == "\\":
                start = i
                while i < n and command_line[i] == "\\":
                    i += 1
                count = i - start
                if i < n and command_line[i] == '"':
                    parts.append("\\" * (count // 2))
                    if count % 2:
                        parts.append('"')
                        i += 1
                    else:
                        quoted = not quoted
                        i += 1
                else:
                    parts.append("\\" * count)
            elif command_line[i] == '"':
                if quoted and i + 1 < n and command_line[i + 1] == '"':
                    parts.append('"')
                    i += 2
                else:
                    quoted = not quoted
                    i += 1
            else:
                parts.append(command_line[i])
                i += 1
        args.append("".join(parts))
        if quoted:
            return args, False
    return args, True


def split_windows_argv(command_line: str) -> list[str]:
    """Split as CommandLineToArgvW: space/tab delimit outside quotes only.

    Quotes toggle grouping and are removed; paired quotes inside a quoted
    argument produce one literal quote. Before a quote, 2n backslashes produce
    n literal backslashes and toggle quoting, while 2n+1 produce n literal
    backslashes and one literal quote. Backslashes elsewhere, including at the
    end outside quotes, are literal. ``""`` preserves an empty argument;
    leading whitespace yields an empty first argument. An unclosed quote takes
    the rest of the line as one argument, as CommandLineToArgvW does.
    """
    return _split_with_balance(command_line)[0]


def _read_option(args: list[str], index: int, values: set[str], flags: set[str],
                 *, variadic_image: bool) -> tuple[int, bool]:
    token = args[index]
    name, sep, attached = token.partition("=")
    if name in flags and not sep:
        return index + 1, True
    if name not in values or (sep and not name.startswith("--")):
        return index, False
    if sep:
        if not attached or (variadic_image and name == "--image" and
                            index + 1 < len(args) and not args[index + 1].startswith("-")):
            return index, False
        return index + 1, True
    if index + 1 >= len(args) or not args[index + 1] or args[index + 1].startswith("-"):
        return index, False
    after = index + 2
    if variadic_image and name in {"-i", "--image"} and after < len(args):
        if not args[after].startswith("-"):
            return index, False
    return after, True


def _command_index(args: list[str], index: int, values: set[str], flags: set[str],
                   *, variadic_image: bool = False) -> int | None:
    while index < len(args):
        if not args[index].startswith("-"):
            return index
        next_index, valid = _read_option(args, index, values, flags,
                                         variadic_image=variadic_image)
        if not valid:
            return None
        index = next_index
    return None


def classify_codex_process(command_line: str, *, target_native_id: str) -> str:
    """Return 'target', 'other' or 'ambiguous' for one pinned Codex process."""
    if (not isinstance(command_line, str) or not command_line or
            len(command_line) > _MAX_COMMAND_LINE or "\x00" in command_line or
            not isinstance(target_native_id, str) or not _UUID.fullmatch(target_native_id)):
        return "ambiguous"
    args, balanced = _split_with_balance(command_line)
    if not balanced or not args:
        return "ambiguous"
    executable = args[0].replace("/", "\\").rsplit("\\", 1)[-1].lower()
    if executable not in {"codex", "codex.exe", "codex.cmd"}:
        return "ambiguous"
    if any("--last" in arg or "\x00" in arg for arg in args[1:]):
        return "ambiguous"

    command = _command_index(args, 1, _TOP_VALUE, _TOP_FLAG, variadic_image=True)
    if command is None:
        return "ambiguous"
    if args[command] in {"exec", "e"}:
        subcommand = _command_index(args, command + 1, _EXEC_VALUE, _EXEC_FLAG,
                                    variadic_image=True)
        if subcommand is None or args[subcommand] != "resume":
            return "ambiguous"
        index = subcommand + 1
        values, flags = _EXEC_RESUME_VALUE, _EXEC_RESUME_FLAG
        variadic_image = False
    elif args[command] == "resume":
        index = command + 1
        values, flags = _RESUME_VALUE, _RESUME_FLAG
        variadic_image = True
    else:
        return "ambiguous"

    positionals: list[str] = []
    while index < len(args):
        token = args[index]
        if token.startswith("-"):
            next_index, valid = _read_option(args, index, values, flags,
                                             variadic_image=variadic_image)
            if not valid:
                return "ambiguous"
            index = next_index
        else:
            positionals.append(token)
            if len(positionals) > 2:
                return "ambiguous"
            index += 1
    if not positionals or not _UUID.fullmatch(positionals[0]):
        return "ambiguous"
    return "target" if positionals[0].lower() == target_native_id.lower() else "other"


def reduce_snapshot(verdicts: Iterable[str]) -> str:
    """Reduce one successful snapshot: target > ambiguity > distinct peers."""
    uncertain = False
    try:
        for verdict in verdicts:
            if verdict == "target":
                return "busy"
            if verdict != "other":
                uncertain = True
    except TypeError:
        return "unknown"
    return "unknown" if uncertain else "clear"
