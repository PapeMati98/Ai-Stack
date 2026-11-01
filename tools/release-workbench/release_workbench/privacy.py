"""Bounded local privacy precheck for a candidate source tree.

This module reports rule, relative path, and line only. It is deliberately a
best-effort local precheck, not a complete secret scanner or publication gate.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path
from typing import Any


_VERSION = 1
_MAX_FILE_BYTES = 16 * 1024 * 1024
_MAX_POLICY_LITERALS = 256
_MAX_POLICY_LITERAL_LENGTH = 4096
_MAX_ALLOWLIST_ENTRIES = 1024
_REPARSE_POINT = 0x400
_LIMITATION = (
    "Local pattern precheck only; a clean result is not a complete secret or "
    "security audit, legal clearance, or permission to publish."
)

_RULES = {
    "privacy.private_key",
    "privacy.credential_assignment",
    "privacy.aws_access_key",
    "privacy.github_token",
    "privacy.owner_path",
    "privacy.forbidden_literal",
}
_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?:api[_-]?(?:key|token)|access[_-]?token|auth(?:orization)?|"
    r"credential(?:s)?|password|"
    r"passwd|secret|client[_-]?secret|refresh[_-]?token|private[_-]?key|"
    r"aws[_-]?(?:secret[_-]?)?access[_-]?key(?:[_-]?id)?|token)\b"
    r"\s*[:=]\s*(?P<value>\"[^\"\r\n]*\"|'[^'\r\n]*'|[^,;#\s]+)"
)
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----",
    re.IGNORECASE,
)
_AWS_ACCESS_KEY_RE = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_GITHUB_TOKEN_RE = re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")
_OWNER_PATH_RE = re.compile(
    r"(?i)(?:\b[A-Z]:\\+Users\\+(?P<winuser>[^\\\s\"'<>]+)"
    r"|(?<![A-Za-z0-9_])/(?:home|Users)/(?P<unixuser>[^/\s\"'<>]+)"
    r"|\b[A-Z]:\\+AL Lab\\+Agenci AI(?:\\+|$))"
)
_LOOPBACK_RE = re.compile(
    r"(?i)^(?:(?:https?|wss?)://)?(?:localhost|127(?:\.\d{1,3}){3}|"
    r"\[?::1\]?)(?::\d+)?(?:[/#?].*)?$"
)
_EXAMPLE_HOST_RE = re.compile(r"(?i)(?:^|[./@])example\.(?:com|invalid)(?:$|[/:?#])")
_ENV_REFERENCE_RE = re.compile(
    r"(?i)^(?:\$\{?[A-Z_][A-Z0-9_]*\}?|%[A-Z_][A-Z0-9_]*%|"
    r"\{\{[A-Z_][A-Z0-9_]*\}\}|(?:os\.)?environ(?:\[[^\]]+\])?)$"
)
_NATIVE_ENV_REFERENCE_RE = re.compile(r"\{env:[A-Za-z_][A-Za-z0-9_]*\}")
_OS_CREDENTIAL_REFERENCE_RE = re.compile(
    r"os-credential://(?:[A-Za-z][A-Za-z0-9_-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*"
    r"|<provider>/<handle>)?"
)
_AUTH_METHOD_METADATA_RE = re.compile(
    r"\{\s*(?P<key_quote>['\"])selectedType(?P=key_quote)\s*:\s*"
    r"(?P<value_quote>['\"])[A-Za-z][A-Za-z0-9_.-]*(?P=value_quote)\s*\}"
)
_PLACEHOLDERS = {
    "",
    "abc123",
    "changeme",
    "changemeplease",
    "dummy",
    "dummykey",
    "dummysecret",
    "example",
    "examplekey",
    "exampletoken",
    "insertvaluehere",
    "none",
    "notset",
    "null",
    "placeholder",
    "redacted",
    "replace",
    "replacewithvalue",
    "sample",
    "samplekey",
    "samplesecret",
    "sampletoken",
    "secret",
    "test",
    "testkey",
    "testsecret",
    "testtoken",
    "todo",
    "token",
    "undefined",
    "unset",
    "value",
    "yourapikey",
    "yourpassword",
    "yourtoken",
    "xxx",
    "xxxxxxxx",
}
_REFUSED_DIRECTORY_NAMES = {
    ".git",
    ".ssh",
    "cache",
    "logs",
    "node_modules",
    "proposals",
    "raw",
    "secrets",
    "state",
    "vault",
}
_REFUSED_KEY_FILE_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}
_CODE_SUFFIXES = {
    ".cjs",
    ".cs",
    ".go",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".kts",
    ".lua",
    ".mjs",
    ".php",
    ".py",
    ".pyi",
    ".pyw",
    ".rb",
    ".rs",
    ".scala",
    ".swift",
    ".ts",
    ".tsx",
}
# Narrow filename classification for legitimate source modules whose names
# merely resemble credential stores. An exact regular-file name match skips
# only the refuse-before-read path classification; the file is still opened and
# scanned against every sensitive-content rule. Directory components and
# lookalikes (for example secret_run.py.json) remain refused before read.
_RECOGNIZED_SOURCE_FILE_NAMES = {"secret_run.py"}
# This path is case-sensitive and literal; never normalize it to grant access.
_RECOGNIZED_CARETAKER_SOURCE = "shared/openclaw-hub-caretaker/releases/0.1.78/caretaker/secrets.py"
_PLACEHOLDER_TOKENS = {
    "access",
    "api",
    "apikey",
    "aws",
    "change",
    "changeme",
    "client",
    "credential",
    "credentials",
    "dummy",
    "example",
    "here",
    "insert",
    "key",
    "me",
    "none",
    "not",
    "notset",
    "null",
    "passwd",
    "password",
    "placeholder",
    "please",
    "private",
    "redacted",
    "refresh",
    "replace",
    "sample",
    "secret",
    "set",
    "test",
    "todo",
    "token",
    "undefined",
    "unset",
    "value",
    "with",
    "xxx",
    "xxxxxxxx",
    "your",
}
_KEY_ALT = (
    r"api[_-]?(?:key|token)"
    r"|access[_-]?token"
    r"|auth(?:orization)?"
    r"|credentials?"
    r"|password"
    r"|passwd"
    r"|secret"
    r"|client[_-]?secret"
    r"|refresh[_-]?token"
    r"|private[_-]?key"
    r"|aws[_-]?(?:secret[_-]?)?access[_-]?key(?:[_-]?id)?"
    r"|token"
)
_ASSIGN_START_RE = re.compile(
    r"(?i)(?:"
    r"(?P<kq>['\"])(?P<qkey>" + _KEY_ALT + r")(?P=kq)"
    r"|(?<![A-Za-z0-9_])(?P<bkey>" + _KEY_ALT + r")(?![A-Za-z0-9_])"
    r")"
    r"\s*"
    r"(?:"
    r"(?P<walrus>:=)"
    r"|(?P<equals>=+)"
    r"|(?P<colon>:)(?!=)"
    r")"
)
_STRING_START_RE = re.compile(
    r"(?i)(?P<prefix>fr|rf|br|rb|ur|ru|f|r|b|u)?(?P<quote>\"\"\"|'''|\"|')"
)
_ENV_TOKEN_RE = re.compile(
    r"(?i)(?:\$\{?[A-Z_][A-Z0-9_]*\}?|%[A-Z_][A-Z0-9_]*%|\{\{[A-Z_][A-Z0-9_]*\}\})"
)
_NON_SECRET_LITERALS = {"none", "null", "true", "false", "undefined", "nil"}


def _is_recognized_source_file(name: str, *, is_parent: bool, is_directory: bool) -> bool:
    """Exact-name classification of a legitimate credential-operation source file."""
    return not is_parent and not is_directory and name in _RECOGNIZED_SOURCE_FILE_NAMES


def _is_sensitive_path(relative: str, *, is_directory: bool) -> bool:
    """Identify paths that must be refused before opening their contents."""
    parts = relative.replace("\\", "/").split("/")
    for index, part in enumerate(parts):
        name = part.casefold()
        is_parent = index < len(parts) - 1
        if (is_parent or is_directory) and name in _REFUSED_DIRECTORY_NAMES:
            return True
        if name.startswith(".env"):
            return True
        if name.startswith("credentials") or (
            name.startswith("secret")
            and not _is_recognized_source_file(name, is_parent=is_parent, is_directory=is_directory)
            and not (not is_parent and not is_directory and relative == _RECOGNIZED_CARETAKER_SOURCE)
        ):
            return True
        if name.startswith("auth") and name.endswith(".json"):
            return True
        if not is_parent and Path(name).suffix in _REFUSED_KEY_FILE_SUFFIXES:
            return True
        if not is_parent and name in {"id_rsa", "id_ed25519", "id_ecdsa", "id_dsa"}:
            return True
    return False


def _validated_policy(policy: dict[str, Any]) -> tuple[list[str], set[tuple[str, str, str]]]:
    if not isinstance(policy, dict):
        raise ValueError("policy must be a dictionary")
    if set(policy) - {"version", "forbidden_literals", "allowlist"}:
        raise ValueError("policy contains unsupported fields")
    if policy.get("version") != _VERSION or isinstance(policy.get("version"), bool):
        raise ValueError("policy version must be 1")

    literals = policy.get("forbidden_literals", [])
    if not isinstance(literals, list) or len(literals) > _MAX_POLICY_LITERALS:
        raise ValueError("forbidden_literals must be a bounded list of strings")
    normalized_literals: list[str] = []
    seen_literals: set[str] = set()
    for literal in literals:
        if (
            not isinstance(literal, str)
            or not literal
            or len(literal) > _MAX_POLICY_LITERAL_LENGTH
            or "\x00" in literal
        ):
            raise ValueError("each forbidden literal must be a non-empty bounded string")
        folded = literal.casefold()
        if folded in seen_literals:
            raise ValueError("forbidden_literals must be unique")
        seen_literals.add(folded)
        normalized_literals.append(literal)

    entries = policy.get("allowlist", [])
    if not isinstance(entries, list) or len(entries) > _MAX_ALLOWLIST_ENTRIES:
        raise ValueError("allowlist must be a bounded list")
    allowlist: set[tuple[str, str, str]] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"rule", "path", "content_sha256"}:
            raise ValueError("each allowlist entry must contain rule, path, and content_sha256")
        rule = entry["rule"]
        path = entry["path"]
        digest = entry["content_sha256"]
        if not isinstance(rule, str) or rule not in _RULES:
            raise ValueError("allowlist rule is not recognized")
        if not isinstance(path, str) or not _valid_relative_path(path):
            raise ValueError("allowlist path must be a literal relative path")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("allowlist content_sha256 must be 64 lowercase hexadecimal characters")
        item = (rule, path, digest)
        if item in allowlist:
            raise ValueError("allowlist entries must be unique")
        allowlist.add(item)
    return normalized_literals, allowlist


def _valid_relative_path(value: str) -> bool:
    if not value or "\x00" in value or "\\" in value or ":" in value:
        return False
    if value.startswith("/"):
        return False
    parts = value.split("/")
    return all(part not in {"", ".", ".."} for part in parts)


def validate_policy(policy: dict) -> dict:
    """Validate a version-1 policy without echoing policy literals or paths."""
    literals, allowlist = _validated_policy(policy)
    return {
        "version": _VERSION,
        "status": "PASS",
        "forbidden_literal_count": len(literals),
        "allowlist_count": len(allowlist),
    }


def _is_reparse_point(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)


def _finding(rule: str, path: str, line: int | None) -> dict[str, Any]:
    return {"rule": rule, "path": path, "line": line}


def _credential_value_is_placeholder(value: str) -> bool:
    value = value.strip()
    if _NATIVE_ENV_REFERENCE_RE.fullmatch(value) or _OS_CREDENTIAL_REFERENCE_RE.fullmatch(value):
        return True
    value = value.strip("\"'`").strip()
    folded = value.casefold()
    compact = re.sub(r"[\s._-]+", "", folded)
    if not folded:
        return True
    if folded.startswith(("<", "{{", "${")) or _ENV_REFERENCE_RE.fullmatch(value):
        return True
    if compact in _PLACEHOLDERS or folded in _NON_SECRET_LITERALS:
        return True
    if _LOOPBACK_RE.fullmatch(value) or _EXAMPLE_HOST_RE.search(value):
        return True
    tokens = [token for token in re.split(r"[\s._/-]+", folded) if token]
    return bool(tokens) and all(token in _PLACEHOLDER_TOKENS or token in _PLACEHOLDERS for token in tokens)


def _owner_path_is_placeholder(match: re.Match[str]) -> bool:
    user = match.group("winuser") or match.group("unixuser")
    if user is None:
        return False
    return user.casefold() in {"user", "username", "yourname", "example", "placeholder", "<user>"}


def _is_code_path(path: str) -> bool:
    return Path(path).suffix.casefold() in _CODE_SUFFIXES


def _consume_quoted(text: str, start: int, quote: str) -> tuple[str, int]:
    characters: list[str] = []
    index = start
    size = len(text)
    while index < size:
        if text[index] == "\\" and index + 1 < size:
            characters.append(text[index + 1])
            index += 2
            continue
        if text.startswith(quote, index):
            return "".join(characters), index + len(quote)
        characters.append(text[index])
        index += 1
    return "".join(characters), size


def _quoted_spans(text: str):
    """Yield raw quote/content boundaries without excluding their contents from scans."""
    index = 0
    size = len(text)
    while index < size:
        if text[index] not in "'\"`":
            index += 1
            continue
        start = index
        character = text[index]
        quote = character * 3 if text.startswith(character * 3, index) else character
        index += len(quote)
        content_start = index
        while index < size:
            if text[index] == "\\" and index + 1 < size:
                index += 2
            elif text.startswith(quote, index):
                break
            else:
                index += 1
        content_end = index
        index = min(size, index + len(quote))
        yield start, content_start, content_end, index


def _is_sql_parameter(text: str, pos: int) -> bool:
    """Recognize an unquoted qmark only inside an enclosing SQL statement string."""
    if pos >= len(text) or text[pos] != "?":
        return False
    if pos + 1 < len(text) and text[pos + 1] not in " \t\r\n,;)]":
        return False
    if not re.match(
        r"(?is)\s*(?:UPDATE\b.*?\bSET\b|SELECT\b.*?\b(?:WHERE|ON)\b|"
        r"DELETE\b.*?\bWHERE\b|INSERT\b.*?\bVALUES\b)", text[:pos]
    ):
        return False
    # A qmark in a SQL string literal is data, not a bound parameter.
    return not any(start < pos < end for start, _, _, end in _quoted_spans(text[:pos + 1]))


def _balanced_end(text: str, start: int) -> int | None:
    pairs = {"(": ")", "[": "]", "{": "}"}
    if start >= len(text) or text[start] not in pairs:
        return None
    stack = [pairs[text[start]]]
    index = start + 1
    size = len(text)
    while index < size:
        character = text[index]
        if character in {"'", '"', "`"}:
            quote = character * 3 if text.startswith(character * 3, index) else character
            _, index = _consume_quoted(text, index + len(quote), quote)
            continue
        if character in pairs:
            stack.append(pairs[character])
            index += 1
            continue
        if character == stack[-1]:
            stack.pop()
            index += 1
            if not stack:
                return index
            continue
        index += 1
    return None


def _scalar_end(text: str, start: int) -> int:
    index = start
    size = len(text)
    depth = 0
    while index < size:
        character = text[index]
        if character in {"'", '"', "`"}:
            quote = character * 3 if text.startswith(character * 3, index) else character
            _, index = _consume_quoted(text, index + len(quote), quote)
            continue
        if character in "([{":
            depth += 1
            index += 1
            continue
        if character in ")]}":
            if depth == 0:
                break
            depth -= 1
            index += 1
            continue
        if depth == 0 and character in " \t\r\n,;#":
            break
        index += 1
    return index


def _expression_is_code(expr: str, *, code_syntax: bool) -> bool:
    if _ENV_REFERENCE_RE.fullmatch(expr) or _ENV_TOKEN_RE.fullmatch(expr):
        return True
    if expr.casefold() in _NON_SECRET_LITERALS:
        return True
    if re.match(r"(?i)(?:os\.(?:environ|getenv)|process\.env)\b", expr):
        return True
    match = re.match(r"[A-Za-z_][A-Za-z0-9_]*", expr)
    if not match:
        return False
    index = match.end()
    saw_call = False
    size = len(expr)
    while index < size:
        while index < size and expr[index] in " \t":
            index += 1
        if index >= size:
            break
        if expr.startswith("?.", index) or expr[index] == ".":
            attribute = re.match(r"\??\.\s*[A-Za-z_][A-Za-z0-9_]*", expr[index:])
            if not attribute:
                return False
            index += attribute.end()
            continue
        if expr[index] in "([":
            end = _balanced_end(expr, index)
            if end is None:
                return False
            index = end
            saw_call = True
            continue
        return False
    if index != size:
        return False
    # Calls and subscripts are code in every syntax. Bare names and attribute
    # chains are code only in source files; elsewhere they are scalar values.
    return True if saw_call else code_syntax


def _read_yaml_block(text: str, line_end: int) -> str:
    if line_end >= len(text):
        return ""
    index = line_end + 1
    lines: list[str] = []
    base_indent: int | None = None
    size = len(text)
    while index < size:
        next_break = text.find("\n", index)
        if next_break == -1:
            next_break = size
        line = text[index:next_break]
        if line.strip() == "":
            if base_indent is not None:
                lines.append("")
            index = next_break + 1
            continue
        indent = len(line) - len(line.lstrip(" "))
        if base_indent is None:
            if indent == 0:
                break
            base_indent = indent
        if indent < base_indent:
            break
        lines.append(line[base_indent:])
        index = next_break + 1
        if next_break == size:
            break
    return "\n".join(lines).strip()


def _consume_annotation(text: str, pos: int) -> int | None:
    line_end = len(text)
    for separator in ("\n", "\r"):
        hit = text.find(separator, pos)
        if hit != -1:
            line_end = min(line_end, hit)
    match = re.match(
        r"[A-Za-z_][A-Za-z0-9_]*(?:\s*\[[^\]\n]*\])?(?:\s*\|\s*[A-Za-z_][A-Za-z0-9_]*)*\s*=(?!=)",
        text[pos:line_end],
    )
    if not match:
        return None
    return pos + match.end()


def _classify_quoted_value(content: str, text: str, end: int) -> str:
    if _NATIVE_ENV_REFERENCE_RE.fullmatch(content) or _OS_CREDENTIAL_REFERENCE_RE.fullmatch(content):
        # An exact reference cannot make a concatenated literal credential safe.
        suffix = text[end:].lstrip(" \t")
        while suffix.startswith(")"):
            suffix = suffix[1:].lstrip(" \t")
        if suffix and suffix[0] not in "\r\n,;#]}":
            return "literal"
    return "code" if _credential_value_is_placeholder(content) else "literal"


def _parse_assigned_value(text: str, pos: int, *, code_syntax: bool) -> str:
    """Return 'literal' or 'code' for the value that begins at pos."""
    size = len(text)
    while True:
        while pos < size and text[pos] in " \t\r\n":
            pos += 1
        if pos < size and text[pos] == "(":
            pos += 1
            continue
        break
    if pos >= size:
        return "code"

    if text.startswith("`", pos):
        content, end = _consume_quoted(text, pos + 1, "`")
        if "${" in content:
            return "code"
        return _classify_quoted_value(content, text, end)

    string_match = _STRING_START_RE.match(text, pos)
    if string_match:
        quote = string_match.group("quote")
        prefix = string_match.group("prefix") or ""
        content, end = _consume_quoted(text, string_match.end(), quote)
        if "f" in prefix.casefold() and re.search(r"(?<!\{)\{(?!\{)", content):
            return "code"
        return _classify_quoted_value(content, text, end)

    line_end = text.find("\n", pos)
    segment = text[pos:] if line_end == -1 else text[pos:line_end]
    if re.fullmatch(r"[|>][+-]?\s*(?:#.*)?", segment):
        content = _read_yaml_block(text, line_end if line_end != -1 else len(text))
        return "code" if _credential_value_is_placeholder(content) else "literal"

    raw = text[pos:_scalar_end(text, pos)].strip()
    if not raw or _expression_is_code(raw, code_syntax=code_syntax):
        return "code"
    return "code" if _credential_value_is_placeholder(raw) else "literal"


def _assignment_lines(text: str, *, code_syntax: bool) -> set[int]:
    lines: set[int] = set()
    position = 0
    size = len(text)
    spans = iter(_quoted_spans(text))
    span = next(spans, None)
    while position < size:
        match = _ASSIGN_START_RE.search(text, position)
        if not match:
            break
        while span is not None and span[3] <= match.start():
            span = next(spans, None)
        # A quoted key starts at its delimiter. An embedded assignment starts
        # inside the string and must never borrow a value from the next string.
        embedded = span is not None and span[0] < match.start() < span[2]
        context_start = span[1] if embedded else 0
        context = text[context_start:span[2]] if embedded else text
        value_at = match.end() - context_start
        while value_at < len(context) and context[value_at] in " \t":
            value_at += 1
        key = (match.group("qkey") or match.group("bkey")).casefold()
        scheme = (
            key == "credential" and match.group("bkey") and match.group("colon")
            and text[max(0, match.start() - 3):match.start()] == "os-"
            and (match.start() == 3 or not re.match(r"[A-Za-z0-9_-]", text[match.start() - 4]))
            and context.startswith("//", value_at)
        )
        incomplete_header = (
            embedded and key == "authorization"
            and context[value_at:].strip().casefold() == "bearer"
        )
        metadata_end = _balanced_end(context, value_at) if key == "auth" else None
        metadata = (
            metadata_end is not None
            and _AUTH_METHOD_METADATA_RE.fullmatch(context[value_at:metadata_end]) is not None
            and (metadata_end == len(context) or context[metadata_end] in " \t\r\n,;#)]}")
        )
        if match.group("colon") and value_at < len(context) and context[value_at] not in "'\"`\r\n":
            annotated = _consume_annotation(context, value_at)
            if annotated is not None:
                value_at = annotated
        if not (scheme or metadata or incomplete_header or (embedded and _is_sql_parameter(context, value_at))) and (
            # Bare names inside source strings are data, not variable reads.
            _parse_assigned_value(context, value_at, code_syntax=code_syntax and not embedded) == "literal"
        ):
            lines.add(text.count("\n", 0, match.start()) + 1)
        next_position = match.end()
        position = next_position if next_position > position else position + 1
    return lines


def _scan_text(text: str, path: str, literals: list[str]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    assigned = _assignment_lines(text, code_syntax=_is_code_path(path))
    for line_number, line in enumerate(text.splitlines(), start=1):
        rules: set[str] = set()
        if _PRIVATE_KEY_RE.search(line):
            rules.add("privacy.private_key")
        if line_number in assigned:
            rules.add("privacy.credential_assignment")
        if _AWS_ACCESS_KEY_RE.search(line):
            rules.add("privacy.aws_access_key")
        if _GITHUB_TOKEN_RE.search(line):
            rules.add("privacy.github_token")
        if any(not _owner_path_is_placeholder(m) for m in _OWNER_PATH_RE.finditer(line)):
            rules.add("privacy.owner_path")
        folded_line = line.casefold()
        if any(literal.casefold() in folded_line for literal in literals):
            rules.add("privacy.forbidden_literal")
        findings.extend(_finding(rule, path, line_number) for rule in sorted(rules))
    return findings


def scan_tree(root, *, policy=None) -> dict:
    """Scan an explicit source directory without following links or reading OS state.

    Unknown, binary, oversized, or unreadable material is a failure finding.
    Allowlist entries suppress only the exact rule/path/full-file-hash triple.
    """
    literals, allowlist = _validated_policy({"version": _VERSION} if policy is None else policy)
    try:
        root_path = Path(root)
    except (TypeError, ValueError):
        raise ValueError("root must be an existing readable directory") from None
    try:
        root_info = root_path.lstat()
    except OSError:
        raise ValueError("root must be an existing readable directory") from None
    if stat.S_ISLNK(root_info.st_mode) or _is_reparse_point(root_info) or not stat.S_ISDIR(root_info.st_mode):
        raise ValueError("root must be a real directory, not a link or reparse point")

    raw_findings: list[dict[str, Any]] = []
    file_hashes: dict[str, str] = {}
    files_scanned = 0
    refused_files = 0
    directories = [root_path]

    def refuse(rule: str, relative: str) -> None:
        nonlocal refused_files
        refused_files += 1
        raw_findings.append(_finding(rule, relative, None))

    while directories:
        directory = directories.pop()
        relative_dir = directory.relative_to(root_path).as_posix()
        try:
            with os.scandir(directory) as scan:
                entries = sorted(list(scan), key=lambda item: item.name.casefold())
        except OSError:
            refuse("scan.unreadable_directory", relative_dir or ".")
            continue
        for entry in entries:
            child = Path(entry.path)
            relative = child.relative_to(root_path).as_posix()
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                refuse("scan.unreadable_entry", relative)
                continue
            if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info):
                refuse("scan.refused_link", relative)
                continue
            if stat.S_ISDIR(info.st_mode):
                if _is_sensitive_path(relative, is_directory=True):
                    refuse("scan.refused_sensitive_path", relative)
                    continue
                directories.append(child)
                continue
            if not stat.S_ISREG(info.st_mode):
                refuse("scan.unknown_file_type", relative)
                continue
            if _is_sensitive_path(relative, is_directory=False):
                refuse("scan.refused_sensitive_path", relative)
                continue
            if info.st_size > _MAX_FILE_BYTES:
                refuse("scan.file_too_large", relative)
                continue

            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            flags |= getattr(os, "O_NONBLOCK", 0)
            try:
                descriptor = os.open(child, flags)
                try:
                    opened_info = os.fstat(descriptor)
                    if (
                        not stat.S_ISREG(opened_info.st_mode)
                        or _is_reparse_point(opened_info)
                        or (info.st_ino and opened_info.st_ino != info.st_ino)
                        or (info.st_dev and opened_info.st_dev != info.st_dev)
                    ):
                        refuse("scan.changed_during_scan", relative)
                        continue
                    with os.fdopen(descriptor, "rb", closefd=False) as source:
                        payload = source.read(_MAX_FILE_BYTES + 1)
                finally:
                    os.close(descriptor)
            except OSError:
                refuse("scan.unreadable_file", relative)
                continue
            if len(payload) > _MAX_FILE_BYTES:
                refuse("scan.file_too_large", relative)
                continue
            if b"\x00" in payload:
                refuse("scan.binary_file", relative)
                continue
            try:
                text = payload.decode("utf-8-sig", errors="strict")
            except UnicodeDecodeError:
                refuse("scan.unknown_encoding", relative)
                continue

            files_scanned += 1
            file_hashes[relative] = hashlib.sha256(payload).hexdigest()
            raw_findings.extend(_scan_text(text, relative, literals))

    findings: list[dict[str, Any]] = []
    allowlisted_count = 0
    for item in raw_findings:
        digest = file_hashes.get(item["path"])
        if digest is not None and (item["rule"], item["path"], digest) in allowlist:
            allowlisted_count += 1
        else:
            findings.append(item)
    findings.sort(key=lambda item: (item["path"].casefold(), item["path"], item["line"] or 0, item["rule"]))
    return {
        "version": _VERSION,
        "status": "PASS" if not findings else "FAIL",
        "files_scanned": files_scanned,
        "files_refused": refused_files,
        "finding_count": len(findings),
        "allowlisted_findings": allowlisted_count,
        "findings": findings,
        "limitations": [_LIMITATION],
    }
