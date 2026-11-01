#!/usr/bin/env python3
"""Read-only diagnostic evidence aggregator for systematic-debugging 1.0.0.

OPT-IN helper. Ingests captured text or JSONL produced by an external command
(test runner, log dump, diff output, etc.), derives bounded groups with
line-numbered examples, preserves the captured command exit status, redacts
sensitive key/value/header/URL-credential material, and writes a compact JSON
summary to stdout (and optionally a new JSON file). The captured command is
NEVER executed, and the helper does not discover inputs recursively, fetch
data, modify source, or invoke subprocesses/network.

Helper exit status is independent of the captured command result:
  helper exit 0  -> helper ran successfully (SUCCEEDED)
  helper exit 2  -> invalid input / contract violation
  helper exit 3  -> I/O error
  helper exit 4  -> internal runtime error

A helper exit 0 documents SUCCEEDED, never FIXED. The captured command's
status is reported separately as ``command.command_status`` and may be
``SUCCESS`` / ``FAILED`` / ``MATCHED`` / ``NO_MATCH`` / ``SAME`` / ``DIFFERENT`` /
``RG_ERROR`` / ``DIFF_ERROR`` depending on ``--exit-policy``.

Refuses known secret/credential input basenames, symlinks, and unsafe
``--output`` aliases that resolve to the input file. By default refuses to
overwrite an existing output file; pass ``--force`` to allow overwrite.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure bytecode is never written
sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

VERSION = "1.0.0"
SCHEMA_VERSION = 1
HELPER_NAME = "summarize-evidence"

EXIT_SUCCEEDED = 0
EXIT_INVALID_INPUT = 2
EXIT_IO_ERROR = 3
EXIT_RUNTIME_ERROR = 4

EXIT_POLICIES = ("command", "rg", "diff")
FORMATS = ("text", "jsonl")

DEFAULT_TOP_GROUPS = 20
DEFAULT_MAX_EXAMPLES = 3
ABSOLUTE_MAX_TOP = 1000
ABSOLUTE_MAX_EXAMPLES = 100

# Forbidden basenames: refuse to LOAD a file with any of these names.
FORBIDDEN_INPUT_NAMES = frozenset(
    {
        ".env",
        ".env.local",
        ".env.production",
        ".env.production.local",
        ".env.development",
        ".env.development.local",
        ".env.test",
        ".env.test.local",
        ".npmrc",
        ".netrc",
        ".pgpass",
        "credentials",
        "credentials.json",
        "credentials.yaml",
        "credentials.yml",
        "credential.json",
        "secret",
        "secrets",
        "secret.json",
        "secrets.json",
        "secrets.yaml",
        "secrets.yml",
        "service-account.json",
        "service_account.json",
        "service-account-key.json",
        "serviceaccountkey.json",
        "token",
        "tokens.json",
        "tokens.yaml",
        "passwords",
        "passwords.txt",
        "passwords.json",
        "password.txt",
        "password.json",
        "cookies",
        "cookies.txt",
        "cookies.json",
        "id_rsa",
        "id_rsa.pub",
        "id_ed25519",
        "id_ed25519.pub",
        "private.key",
        "private_key",
        "private_key.json",
        "client_secret.json",
    }
)

SENSITIVE_BASENAME_HINT = re.compile(
    r"(?:^|[._-])(?:secrets?|credentials?|tokens?|passwords?|cookies?|"
    r"private[-_]?key|api[-_]?key|sessions?|auth[-_]??tokens?)(?:$|[._-])",
    re.IGNORECASE,
)

SENSITIVE_KEY_NAMES = (
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "x-auth-token",
    "credential",
    "credentials",
    "password",
    "passwd",
    "pwd",
    "secret",
    "secrets",
    "token",
    "tokens",
    "api_key",
    "apikey",
    "api-key",
    "private_key",
    "privatekey",
    "private-key",
    "session_id",
    "sessionid",
    "session-id",
    "access_token",
    "refresh_token",
    "client_secret",
    "clientsecret",
    "client-secret",
)
_DQUOTE = chr(34)
_SQUOTE = "'"
# Lookbehind (not a consuming prefix) so keys wrapped in signatures such as
# ``message:password=...`` are still matched and the wrapper punctuation is
# preserved in the redacted output.
SENSITIVE_KEY_RE = re.compile(
    r"(?im)(?<![A-Za-z0-9_])"
    + r"(" + "|".join(re.escape(k) for k in SENSITIVE_KEY_NAMES) + r")"
    + r"(?:\s*[\"']?)\s*[:=]\s*"
    + r"(?:" + _DQUOTE + r"[^" + _DQUOTE + r"]*" + _DQUOTE
    + r"|" + _SQUOTE + r"[^" + _SQUOTE + r"]*" + _SQUOTE
    + r"|[^\s,;\"'\]}>]+)"
)

# Header-style keys carry multi-token values (``Authorization: Basic dXNlcjpwYXNz``,
# ``Cookie: theme=dark; session=supersecret``). Cutting those at the first space
# or semicolon leaves credentials in signatures, examples, stdout and --output.
# Value runs to end-of-line and MUST consume any earlier [REDACTED markers too:
# stopping at the first marker lets tail credentials survive
# (``Cookie: [REDACTED:jwt]; session=supersecret`` would still emit
# ``session=supersecret``). Specific Bearer/JWT/URL markers remain meaningful
# only outside these header keys.
SENSITIVE_HEADER_KEYS = (
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "x-auth-token",
)
SENSITIVE_HEADER_RE = re.compile(
    r"(?im)(?<![A-Za-z0-9_])"
    + r"(" + "|".join(re.escape(k) for k in SENSITIVE_HEADER_KEYS) + r")"
    + r"(?:\s*[\"']?\s*[:=]\s*)"
    + r"[^\r\n]*"
)

URL_USERINFO_RE = re.compile(
    r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)(?P<userinfo>[^\s/:@]+:[^\s@]+@)"
)
BEARER_RE = re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._\-+/=]+")
JWT_RE = re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
LONE_CR_RE = re.compile(r"\r(?!\n)")
WHITESPACE_RUN_RE = re.compile(r"\s+")

# JSONL signature field priority (preserves meaningful numeric codes).
JSONL_STATUS_FIELDS = ("status", "code", "error_code", "http_status", "errno")
JSONL_LEVEL_FIELDS = ("level", "severity")
JSONL_MESSAGE_FIELDS = ("error", "msg", "message", "detail", "reason")

MAX_EXAMPLE_TEXT_LEN = 240


class SummarizeError(RuntimeError):
    """Raised on invalid input, contract violation, or refused I/O."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def is_forbidden_input_name(name: str) -> bool:
    if not name:
        return False
    lower = name.lower()
    if lower in FORBIDDEN_INPUT_NAMES:
        return True
    if SENSITIVE_BASENAME_HINT.search(lower):
        return True
    return False


def resolve_input_path(value: str) -> Path | None:
    """Resolve --input as a path; return None when value is not an existing file."""
    if not value or value == "-":
        return None
    if "\n" in value or "\r" in value:
        return None
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = Path.cwd() / value
    try:
        resolved = candidate.resolve(strict=False)
    except OSError:
        return None
    if resolved.is_file():
        return resolved
    return None


def validate_input_path(path: Path) -> None:
    """Refuse symlinks and known secret/credential basenames."""
    try:
        is_link = path.is_symlink()
    except OSError:
        is_link = False
    if is_link:
        raise SummarizeError(f"Refusing to follow symlink input: {path}")
    name = path.name
    if is_forbidden_input_name(name):
        raise SummarizeError(
            f"Refusing to load input with secret/credential name: {name}"
        )


def load_input_text(input_arg: str) -> tuple[str, dict[str, Any]]:
    """Load input from an explicit existing captured file.

    ``--input`` must resolve to an existing file on disk. Missing paths and
    inline text are rejected with :class:`SummarizeError`; the helper never
    silently falls back to treating a non-file ``--input`` value as inline
    evidence.
    """
    path = resolve_input_path(input_arg)
    if path is None:
        raise SummarizeError(
            "--input does not resolve to an existing captured file"
        )
    # Validate the original (non-resolved) path for symlinks and forbidden
    # names.  resolve_input_path() follows symlinks via Path.resolve(), so
    # the symlink check must run against the caller-supplied path.
    original = Path(input_arg)
    if not original.is_absolute():
        original = Path.cwd() / input_arg
    validate_input_path(original)
    data = path.read_bytes()
    text = data.decode("utf-8", errors="replace")
    return text, {
        "source": "captured_file",
        "path": str(path),
        "size_bytes": len(data),
        "sha256": sha256_bytes(data),
    }


def strip_control_chars(text: str) -> str:
    text = OSC_RE.sub("", text)
    text = ANSI_RE.sub("", text)
    text = LONE_CR_RE.sub("", text)
    return text


def redact_sensitive(text: str) -> tuple[str, int]:
    """Redact sensitive patterns; return (redacted_text, total_replacements).

    Order matters: URL userinfo and Bearer/JWT tokens are replaced first so
    their specific markers survive outside header keys, then header-style keys
    consume the entire multi-token value through end-of-line (including any
    earlier [REDACTED markers, so tail credentials cannot leak), then
    single-token key=value pairs. Counts come only from ``subn`` return values.
    """
    count = 0

    def url_repl(match: re.Match[str]) -> str:
        return f"{match.group('scheme')}[REDACTED:url-userinfo]@"

    text, c = URL_USERINFO_RE.subn(url_repl, text)
    count += c

    def bearer_repl(match: re.Match[str]) -> str:
        return f"{match.group(1)}[REDACTED:bearer]"

    text, c = BEARER_RE.subn(bearer_repl, text)
    count += c

    text, c = JWT_RE.subn("[REDACTED:jwt]", text)
    count += c

    def header_repl(match: re.Match[str]) -> str:
        return f"{match.group(1)}=[REDACTED]"

    text, c = SENSITIVE_HEADER_RE.subn(header_repl, text)
    count += c

    def key_repl(match: re.Match[str]) -> str:
        return f"{match.group(1)}=[REDACTED]"

    text, c = SENSITIVE_KEY_RE.subn(key_repl, text)
    count += c
    return text, count


def canonicalize_line_for_signature(text: str) -> str:
    """Produce a stable signature that preserves meaningful numeric codes.

    Strips ANSI/OSC/CRLF progress, lowercases, collapses whitespace, preserves
    UTF-8, and keeps 3-digit numeric codes intact so that 404 and 500 remain
    distinct signatures.
    """
    text = strip_control_chars(text)
    text = WHITESPACE_RUN_RE.sub(" ", text)
    text = text.strip().lower()
    return text


def truncate_example(text: str, limit: int = MAX_EXAMPLE_TEXT_LEN) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "\u2026"


def get_case_insensitive(obj: dict[str, Any], key: str) -> Any:
    if key in obj:
        return obj[key]
    target = key.lower()
    for k, v in obj.items():
        if isinstance(k, str) and k.lower() == target:
            return v
    return None


def derive_jsonl_signature(obj: dict[str, Any]) -> tuple[str, str]:
    """Derive a JSONL signature preserving meaningful numeric codes."""
    for key in JSONL_STATUS_FIELDS:
        v = get_case_insensitive(obj, key)
        if v is not None:
            return (
                f"status:{json.dumps(v, sort_keys=True, ensure_ascii=False)}",
                f"jsonl.field.{key}",
            )
    for key in JSONL_LEVEL_FIELDS:
        v = get_case_insensitive(obj, key)
        if isinstance(v, str) and v.strip():
            return (
                f"level:{json.dumps(v, sort_keys=True, ensure_ascii=False)}",
                f"jsonl.field.{key}",
            )
    for key in JSONL_MESSAGE_FIELDS:
        v = get_case_insensitive(obj, key)
        if isinstance(v, str) and v.strip():
            sig = canonicalize_line_for_signature(v)
            return f"message:{sig}", f"jsonl.field.{key}"
    canonical = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return f"json:{sha256_text(canonical)}", "jsonl.hash"


def split_text_lines(text: str) -> list[str]:
    """Split text into lines, normalizing CRLF and lone CR to LF.

    A terminal newline is a delimiter, not an extra phantom record.
    Input ending with ``\\n`` (or lone ``\\r`` already normalized) is split
    and the trailing empty fragment is removed. Empty input yields zero
    records; internal blank lines remain in the output as records that the
    caller counts and skips explicitly.
    """
    if not text:
        return []
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    # Strip only the single trailing empty introduced by a terminal newline.
    # Internal blanks must survive so coverage counts them.
    if lines and lines[-1] == "" and text.endswith("\n"):
        lines.pop()
    return lines


def summarize_text(
    text: str,
    *,
    top: int,
    max_examples: int,
    input_truncated: bool,
    redact_counter: list[int],
) -> dict[str, Any]:
    """Aggregate text-mode input into a bounded, redacted summary."""
    raw_lines = split_text_lines(text)
    total_records = len(raw_lines)
    parsed_records = 0
    skipped_records = 0
    invalid_records = 0
    groups: dict[str, dict[str, Any]] = {}

    for index, raw in enumerate(raw_lines):
        line_no = index + 1
        stripped = raw.strip()
        if not stripped:
            skipped_records += 1
            continue
        redacted, count = redact_sensitive(stripped)
        redact_counter[0] += count
        sig = canonicalize_line_for_signature(redacted)
        if not sig:
            skipped_records += 1
            continue
        parsed_records += 1
        group = groups.get(sig)
        if group is None:
            group = {"signature": sig, "count": 0, "examples": []}
            groups[sig] = group
        group["count"] += 1
        if len(group["examples"]) < max_examples:
            safe_example = strip_control_chars(redacted)
            group["examples"].append(
                {"line": line_no, "text": truncate_example(safe_example)}
            )

    sorted_groups = sorted(
        groups.values(), key=lambda v: (-v["count"], v["signature"])
    )
    bounded = sorted_groups[:top]
    total_groups = len(sorted_groups)
    omitted = max(0, total_groups - len(bounded))
    return {
        "coverage": {
            "total_records": total_records,
            "parsed_records": parsed_records,
            "skipped_records": skipped_records,
            "invalid_records": invalid_records,
            "input_complete": not input_truncated,
        },
        "groups": bounded,
        "diagnostics": {
            "input_truncated": input_truncated,
            "total_groups": total_groups,
            "displayed_groups": len(bounded),
            "omitted_groups": omitted,
            "redactions_applied": redact_counter[0],
            "format": "text",
        },
    }


def summarize_jsonl(
    text: str,
    *,
    top: int,
    max_examples: int,
    input_truncated: bool,
    redact_counter: list[int],
) -> dict[str, Any]:
    """Aggregate JSONL-mode input into a bounded, redacted summary."""
    raw_lines = split_text_lines(text)
    total_records = len(raw_lines)
    parsed_records = 0
    skipped_records = 0
    invalid_records = 0
    groups: dict[str, dict[str, Any]] = {}

    for index, raw in enumerate(raw_lines):
        line_no = index + 1
        stripped = raw.strip()
        if not stripped:
            skipped_records += 1
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            # JSONL parse errors must increase invalid_records without
            # printing raw parse-error data.
            invalid_records += 1
            continue
        if not isinstance(obj, dict):
            sig, source = "jsonl:non-object", "jsonl.shape"
            parsed_records += 1
        else:
            sig, source = derive_jsonl_signature(obj)
            parsed_records += 1
        redacted_sig, c1 = redact_sensitive(sig)
        redact_counter[0] += c1
        redacted_example, c2 = redact_sensitive(stripped)
        redact_counter[0] += c2
        group = groups.get(redacted_sig)
        if group is None:
            group = {
                "signature": redacted_sig,
                "count": 0,
                "source": source,
                "examples": [],
            }
            groups[redacted_sig] = group
        group["count"] += 1
        if len(group["examples"]) < max_examples:
            safe_example = strip_control_chars(redacted_example)
            group["examples"].append(
                {"line": line_no, "text": truncate_example(safe_example)}
            )

    sorted_groups = sorted(
        groups.values(), key=lambda v: (-v["count"], v["signature"])
    )
    bounded = sorted_groups[:top]
    total_groups = len(sorted_groups)
    omitted = max(0, total_groups - len(bounded))
    return {
        "coverage": {
            "total_records": total_records,
            "parsed_records": parsed_records,
            "skipped_records": skipped_records,
            "invalid_records": invalid_records,
            "input_complete": not input_truncated,
        },
        "groups": bounded,
        "diagnostics": {
            "input_truncated": input_truncated,
            "total_groups": total_groups,
            "displayed_groups": len(bounded),
            "omitted_groups": omitted,
            "redactions_applied": redact_counter[0],
            "format": "jsonl",
        },
    }


def derive_command_status(exit_policy: str, exit_code: int) -> tuple[str, str]:
    if exit_policy == "command":
        if exit_code == 0:
            return "SUCCESS", "command.success"
        return "FAILED", "command.failure"
    if exit_policy == "rg":
        if exit_code == 0:
            return "MATCHED", "rg.matched"
        if exit_code == 1:
            return "NO_MATCH", "rg.no_match"
        return "RG_ERROR", "rg.error"
    if exit_policy == "diff":
        if exit_code == 0:
            return "SAME", "diff.same"
        if exit_code == 1:
            return "DIFFERENT", "diff.different"
        return "DIFF_ERROR", "diff.error"
    return "UNKNOWN", "unknown.policy"


def validate_bounds(top: int, max_examples: int) -> None:
    if not (1 <= top <= ABSOLUTE_MAX_TOP):
        raise SummarizeError(
            f"--top must be in 1..{ABSOLUTE_MAX_TOP}: {top}"
        )
    if not (1 <= max_examples <= ABSOLUTE_MAX_EXAMPLES):
        raise SummarizeError(
            f"--max-examples must be in 1..{ABSOLUTE_MAX_EXAMPLES}: {max_examples}"
        )


def validate_command_exit_code(value: int) -> int:
    """Validate a captured exit code; preserve any signed integer, including
    Windows NTSTATUS values such as ``-1073741819`` (STATUS_ACCESS_VIOLATION).

    argparse already coerces the CLI value to ``int`` and rejects non-numeric
    strings, so this validator only has to filter out ``bool`` (a subclass of
    ``int``) and confirm the value is a real integer. No range restriction is
    applied: a captured Windows crash exit code like ``-1073741819`` must
    flow through unchanged so the caller can mark it ``FAILED``.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise SummarizeError(f"--command-exit-code must be an integer: {value!r}")
    return value


def safe_int(value: str) -> int:
    """argparse type: coerce to int without echoing the raw token on failure.

    ``type=int`` lets argparse format ``invalid int value: '<token>'`` before
    any main-level redaction. A credential-shaped non-integer such as
    ``--command-exit-code <secret>`` must never reach stdout/stderr.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("must be an integer") from None


# Quoted tokens in argparse error templates (``%(value)r``, ``%r``) may carry
# raw CLI values. Python repr escapes an embedded apostrophe as ``\'`` when
# the token also contains a double quote; a naive ``'[^']*'`` stops at that
# escaped quote and leaks the tail. Match backslash-escaped content so
# mixed-quote tokens are fully replaced before the error line is written.
_QUOTED_TOKEN_RE = re.compile(
    r"'(?:\\.|[^'\\])*'"
    r'|"(?:\\.|[^"\\])*"'
)

# Unquoted ``--opt=value`` fragments (ambiguous-option errors use the
# unquoted ``%(option)s`` template) embed the raw argv element. Keep the
# option name and omit the value.
_OPTION_VALUE_RE = re.compile(r"(--?[A-Za-z0-9][A-Za-z0-9-]*)=[^\s]+")


class SafeArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that never echoes raw CLI tokens on the error path.

    Default argparse errors interpolate the caller-supplied token (invalid
    int/choice values, ignored explicit flag values, unrecognized argv,
    ambiguous option abbreviations) BEFORE ``main`` redaction runs. Invalid
    arguments must still exit nonzero (2) without raw sensitive values on
    stdout/stderr.

    ``allow_abbrev`` is forced off so ``--in=VALUE`` cannot trigger the
    unquoted ``ambiguous option: %(option)s`` leak; such tokens become
    unrecognized arguments and are omitted by :meth:`parse_args`.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs["allow_abbrev"] = False
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> None:  # noqa: D102
        safe = _QUOTED_TOKEN_RE.sub("'[OMITTED]'", message)
        safe = _OPTION_VALUE_RE.sub(r"\1=[OMITTED]", safe)
        safe, _ = redact_sensitive(safe)
        self.exit(EXIT_INVALID_INPUT, f"{self.prog}: error: {safe}\n")

    def _check_value(self, action: argparse.Action, value: Any) -> None:
        if action.choices is not None and value not in action.choices:
            allowed = ", ".join(str(choice) for choice in action.choices)
            raise argparse.ArgumentError(
                action, f"invalid choice (choose from {allowed})"
            )

    def parse_args(
        self,
        args: list[str] | None = None,
        namespace: argparse.Namespace | None = None,
    ) -> argparse.Namespace:
        parsed, extras = self.parse_known_args(args, namespace)
        if extras:
            # Never list leftover tokens: values may be credential-shaped.
            self.error("unrecognized arguments (tokens omitted)")
        return parsed


def write_summary_to_path(path: Path, summary: dict[str, Any], *, force: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not force:
        raise SummarizeError(
            f"Refusing to overwrite existing output file: {path}"
        )
    payload = json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=False) + "\n"
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def reject_output_alias(input_meta: dict[str, Any], output: Path) -> None:
    input_path = input_meta.get("path")
    if not input_path:
        return
    try:
        if output.resolve() == Path(input_path).resolve():
            raise SummarizeError(
                f"Refusing to write --output that aliases the input file: {output}"
            )
    except SummarizeError:
        raise
    except OSError:
        # If the resolve fails, defer to the actual write which will surface a
        # clearer error.
        return


def build_parser() -> argparse.ArgumentParser:
    parser = SafeArgumentParser(
        prog=HELPER_NAME,
        description=(
            "Read-only diagnostic evidence aggregator. Ingests captured text or "
            "JSONL produced by an external command and produces a bounded, "
            "redacted JSON summary. The captured command is never executed."
        ),
    )
    parser.add_argument(
        "--input",
        required=True,
        help=(
            "Path to an existing captured text/JSONL file. Resolved against "
            "the current working directory. Must resolve to a real file on "
            "disk; missing paths and inline text are rejected."
        ),
    )
    parser.add_argument(
        "--format",
        required=True,
        choices=FORMATS,
        help="Input format: 'text' (line-based) or 'jsonl' (one JSON object per line).",
    )
    parser.add_argument(
        "--command-exit-code",
        required=True,
        type=safe_int,
        help=(
            "Integer exit code reported by the captured command. Any signed "
            "integer is accepted; Windows NTSTATUS values such as "
            "-1073741819 (STATUS_ACCESS_VIOLATION) must be preserved so the "
            "summary can mark them as FAILED."
        ),
    )
    parser.add_argument(
        "--exit-policy",
        required=True,
        choices=EXIT_POLICIES,
        help=(
            "How to map --command-exit-code to command_status. 'command': "
            "0=SUCCESS, non-zero=FAILED. 'rg': 1=NO_MATCH. 'diff': 1=DIFFERENT."
        ),
    )
    parser.add_argument(
        "--input-truncated",
        action="store_true",
        help=(
            "Mark the captured input as truncated by an external buffer; "
            "coverage.input_complete becomes false."
        ),
    )
    parser.add_argument(
        "--top",
        type=safe_int,
        default=DEFAULT_TOP_GROUPS,
        help=(
            f"Maximum number of groups in the summary (1..{ABSOLUTE_MAX_TOP}, "
            f"default {DEFAULT_TOP_GROUPS})."
        ),
    )
    parser.add_argument(
        "--max-examples",
        type=safe_int,
        default=DEFAULT_MAX_EXAMPLES,
        help=(
            f"Maximum example lines per group (1..{ABSOLUTE_MAX_EXAMPLES}, "
            f"default {DEFAULT_MAX_EXAMPLES})."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        help=(
            "Optional path for the redacted JSON summary. Refuses to overwrite "
            "an existing file by default; pass --force to allow overwrite. "
            "Aliases to the input file are rejected."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Allow overwriting an existing --output file.",
    )
    return parser


def run(args: argparse.Namespace) -> dict[str, Any]:
    validate_bounds(args.top, args.max_examples)
    exit_code = validate_command_exit_code(args.command_exit_code)
    text, input_meta = load_input_text(args.input)

    redact_counter = [0]
    if args.format == "text":
        body = summarize_text(
            text,
            top=args.top,
            max_examples=args.max_examples,
            input_truncated=args.input_truncated,
            redact_counter=redact_counter,
        )
    else:
        body = summarize_jsonl(
            text,
            top=args.top,
            max_examples=args.max_examples,
            input_truncated=args.input_truncated,
            redact_counter=redact_counter,
        )

    command_status, status_kind = derive_command_status(args.exit_policy, exit_code)
    summary: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now_iso(),
        "helper": {
            "name": HELPER_NAME,
            "version": VERSION,
            "exit_status": "SUCCEEDED",
        },
        "command": {
            "exit_code": exit_code,
            "exit_policy": args.exit_policy,
            "command_status": command_status,
            "status_kind": status_kind,
        },
        "input": input_meta,
        "coverage": body["coverage"],
        "groups": body["groups"],
        "diagnostics": body["diagnostics"],
    }

    if args.output is not None:
        reject_output_alias(input_meta, args.output)
        # Pre-populate the output section. We deliberately omit the file's
        # own SHA-256 to avoid a serialization self-reference; the path
        # alone documents the produced artifact.
        summary["output"] = {
            "path": str(args.output.resolve()),
        }
        write_summary_to_path(args.output, summary, force=args.force)

    return summary


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        summary = run(args)
    except SummarizeError as exc:
        # Brief redacted error message on stderr; no raw input content.
        sys.stderr.write(f"{HELPER_NAME}: {exc}\n")
        return EXIT_INVALID_INPUT
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else EXIT_RUNTIME_ERROR
    except OSError as exc:
        sys.stderr.write(f"{HELPER_NAME}: I/O error: {exc}\n")
        return EXIT_IO_ERROR
    except Exception as exc:  # pragma: no cover - defensive
        sys.stderr.write(f"{HELPER_NAME}: internal error: {exc}\n")
        return EXIT_RUNTIME_ERROR

    json.dump(summary, sys.stdout, ensure_ascii=True, indent=2, sort_keys=False)
    sys.stdout.write("\n")
    return EXIT_SUCCEEDED


if __name__ == "__main__":
    sys.exit(main())