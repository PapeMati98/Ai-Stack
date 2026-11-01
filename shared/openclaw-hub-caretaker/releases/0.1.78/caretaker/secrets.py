"""Secret gate for file sources (layers 1–3).

Verdicts never include inspected path fragments that look like secrets
in ``reason`` beyond a stable rule id. The scanned file body is never
copied into the verdict.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Union

from .filter import SECRET_PATTERNS, contains_secret

PathLike = Union[str, os.PathLike[str], Path]


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    rule_id: str
    reason: str


# Layer 1: directory names that must never be opened.
BLOCKED_DIR_NAMES = frozenset({
    ".ssh",
    ".aws",
    ".azure",
    ".gcloud",
    ".kube",
    ".gnupg",
    ".password-store",
    ".docker",
    ".terraform",
    "secrets",
    ".git",
    "node_modules",
    "venv",
    ".venv",
    "__pycache__",
    "vendor",
    "site-packages",
    ".cache",
})

# Extra content patterns required by SPEC-0.1.8 (extend filter.py set).
FILE_SECRET_PATTERNS = SECRET_PATTERNS + (
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"(?:sk|rk)_live_[A-Za-z0-9]{16,}"),
    re.compile(
        r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"
    ),
    re.compile(r"-----BEGIN (?:OPENSSH )?.*PRIVATE KEY-----"),
    re.compile(r"[a-z+]+://[^\s:@/]+:[^\s@/]+@"),
    re.compile(r"(?i)AccountKey="),
    re.compile(r"(?i)client_secret"),
)

_ENV_NAME = re.compile(r"^\.env(?:\..+)?$", re.IGNORECASE)
_KEY_SUFFIXES = (
    ".pem",
    ".key",
    ".ppk",
    ".pfx",
    ".p12",
    ".jks",
    ".keystore",
    ".kdbx",
    ".ovpn",
)
_ID_PREFIXES = ("id_rsa", "id_ed25519", "id_ecdsa")
_NAME_SUBSTRINGS = ("token", "secret", "haslo", "password")
_DOTFILES = frozenset({".netrc", ".npmrc", ".pypirc"})


def _reject(rule_id: str, reason: str) -> Verdict:
    return Verdict(allowed=False, rule_id=rule_id, reason=reason)


def _allow() -> Verdict:
    return Verdict(allowed=True, rule_id="", reason="allowed")


def _norm_parts(path: Path) -> tuple[str, ...]:
    try:
        raw = os.fspath(path)
    except TypeError:
        return ()
    # Do not resolve or open; split both separators.
    bits = raw.replace("\\", "/").split("/")
    return tuple(p for p in bits if p not in ("", "."))


def inspect_path(path_like: PathLike) -> Verdict:
    """Layers 1 and 2: blocked directories and filenames. Never opens the path."""
    try:
        path = Path(os.fspath(path_like))
    except (TypeError, ValueError, OSError):
        return _reject("path_unreadable", "path could not be inspected")

    parts = _norm_parts(path)
    lower_parts = tuple(p.lower() for p in parts)
    for part in lower_parts[:-1] if lower_parts else ():
        if part in BLOCKED_DIR_NAMES:
            return _reject("blocked_directory", "path is in a blocked directory")
    # Also reject if the last component is itself a blocked dir name
    # (inspecting the directory) or any ancestor including the name.
    for part in lower_parts:
        if part in BLOCKED_DIR_NAMES:
            return _reject("blocked_directory", "path is in a blocked directory")

    name = path.name
    if not name:
        return _reject("empty_name", "path has no file name")
    lower = name.lower()

    if _ENV_NAME.match(name) or _ENV_NAME.match(lower):
        return _reject("blocked_filename", "blocked environment file name")
    if any(lower.endswith(suf) for suf in _KEY_SUFFIXES):
        return _reject("blocked_filename", "blocked key or certificate filename")
    if any(lower.startswith(pref) for pref in _ID_PREFIXES):
        return _reject("blocked_filename", "blocked private key filename")
    if lower.startswith("credentials"):
        return _reject("blocked_filename", "blocked credentials filename")
    if re.match(r"^auth.*\.json$", lower):
        return _reject("blocked_filename", "blocked auth json filename")
    if lower in _DOTFILES:
        return _reject("blocked_filename", "blocked rc filename")
    if any(token in lower for token in _NAME_SUBSTRINGS):
        return _reject("blocked_filename", "blocked secret-like filename")
    return _allow()


def _text_has_secret(text: str) -> bool:
    if contains_secret(text):
        return True
    return any(p.search(text) for p in FILE_SECRET_PATTERNS)


def inspect_text(text, *, path: str = "") -> Verdict:
    """Layer 3: scan content. Fail-closed on non-UTF-8 / binary / doubt."""
    if path:
        path_verdict = inspect_path(path)
        if not path_verdict.allowed:
            return path_verdict

    if text is None:
        return _reject("empty_or_unreadable", "content could not be inspected")

    if isinstance(text, bytes):
        try:
            decoded = text.decode("utf-8")
        except UnicodeDecodeError:
            return _reject("not_utf8", "content is not valid UTF-8")
        if b"\x00" in text:
            return _reject("binary_content", "content looks binary")
        text = decoded
    elif isinstance(text, str):
        if "\x00" in text:
            return _reject("binary_content", "content looks binary")
        try:
            text.encode("utf-8")
        except UnicodeEncodeError:
            return _reject("not_utf8", "content is not valid UTF-8")
    else:
        return _reject("unsupported_type", "content type cannot be scanned")

    try:
        if _text_has_secret(text):
            return _reject("secret_in_content", "secret pattern matched")
    except Exception:
        return _reject("scan_error", "content scan failed")
    return _allow()
