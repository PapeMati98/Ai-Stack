"""Canonical resource identity for paths, Agent Graph write scopes, and data: ids."""

from __future__ import annotations

import os
import re
import socket
import sys
import unicodedata
import uuid
from collections.abc import Mapping
from pathlib import Path

from .errors import CoordError

WIN_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")
SCOPE_WILDCARD_RE = re.compile(r'[*?"<>|]')
DRIVE_RE = re.compile(r"^[A-Za-z]:")
DATA_PREFIX = "data:"
SCOPE_PREFIX = "scope:"
AGENT_PREFIX = "agent:"
MAX_AGENT_CHARS = 400
MAX_RESOURCE_CHARS = 4096
AGENT_COMPONENT_RE = re.compile(r"[^a-z0-9._-]+")
# Windows reserved device names. An agent id becomes the inbox file name
# (`office/inbox/<id>.jsonl`), and classic DOS semantics map `nul.jsonl` to the NUL
# device, which would swallow messages while reporting status=ok. Measured on this
# machine (Windows 11 Pro 10.0.26200, 2026-09-02): the mapping did NOT occur; nul.jsonl,
# con.jsonl and lpt1.jsonl all behaved as ordinary files. The denylist is defence in
# depth because the behaviour depends on the Windows version and on how the path is
# built. Do not remove it as "unneeded" and do not cite it as evidence that the mapping
# ever happened here. The check applies to the segment before the first dot, matching
# how Windows treats `NUL.txt`.
WINDOWS_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul", "clock", "clock$", "conin", "conin$", "conout", "conout$"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)
_PROCESS_SESSION = f"process-{os.getpid()}-{uuid.uuid4().hex[:12]}"
SESSION_ENV_BY_HARNESS = {
    "claude": ("CLAUDE_CODE_SESSION_ID",),
    "claude-code": ("CLAUDE_CODE_SESSION_ID",),
    # Codex exposes the current task/session to child MCP processes. Prefer
    # the session id and use the thread id only for older client builds.
    "codex": ("CODEX_SESSION_ID", "CODEX_THREAD_ID"),
}


def _agent_component(value: str, label: str) -> str:
    normalized = _nfc(value.strip()).lower()
    normalized = AGENT_COMPONENT_RE.sub("-", normalized).strip("-._")
    if not normalized or len(normalized) > 128:
        raise CoordError(f"{label} must normalize to 1..128 safe characters", code="invalid_agent")
    return normalized


def agent_identity(
    harness: str,
    *,
    host: str | None = None,
    session_id: str | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Derive the one board/office identity for the current agent session."""
    environment = os.environ if env is None else env
    harness_id = _agent_component(harness, "harness")
    host_id = _agent_component(host or socket.gethostname(), "host")
    source = "explicit-session-id"
    raw_session = session_id
    if not raw_session:
        for name in ("COORDFS_SESSION_ID", "AGENT_SESSION_ID"):
            candidate = environment.get(name)
            if candidate:
                raw_session = candidate
                source = name
                break
    if not raw_session:
        for name in SESSION_ENV_BY_HARNESS.get(harness_id, ()):
            candidate = environment.get(name)
            if candidate:
                raw_session = candidate
                source = name
                break
    strength = "strong"
    if not raw_session:
        raw_session = _PROCESS_SESSION
        source = "process-fallback"
        strength = "weak"
    session = _agent_component(raw_session, "session")
    return {
        "agent_id": f"{harness_id}-{host_id}-{session}",
        "harness": harness_id,
        "host": host_id,
        "session": session,
        "strength": strength,
        "source": source,
    }


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def canonicalize_agent(raw: str) -> str:
    """Canonical `agent:<id>` presence/office address. Same charset as agent_identity()."""
    text = _nfc(raw.strip())
    if text.lower().startswith(AGENT_PREFIX):
        text = text[len(AGENT_PREFIX) :]
    if text.startswith(DATA_PREFIX) or text.startswith(SCOPE_PREFIX):
        raise CoordError("agent resource cannot use the data: or scope: prefix", code="invalid_agent")
    normalized = AGENT_COMPONENT_RE.sub("-", text.strip().lower()).strip("-._")
    if not normalized or len(normalized) > MAX_AGENT_CHARS:
        raise CoordError(
            f"agent id must normalize to 1..{MAX_AGENT_CHARS} safe characters",
            code="invalid_agent",
        )
    if normalized.split(".", 1)[0] in WINDOWS_RESERVED_NAMES:
        raise CoordError(
            f"agent id {normalized!r} is a Windows reserved device name",
            code="invalid_agent",
        )
    return AGENT_PREFIX + normalized


def agent_id_from_resource(resource_id: str) -> str | None:
    if resource_id.startswith(AGENT_PREFIX):
        return resource_id[len(AGENT_PREFIX) :]
    return None


def canonicalize_data(raw: str) -> str:
    text = _nfc(raw.strip())
    if text.lower().startswith(SCOPE_PREFIX):
        raise CoordError("data resource cannot use the scope: prefix", code="invalid_resource")
    if not text.startswith(DATA_PREFIX):
        text = DATA_PREFIX + text
    asset = text[len(DATA_PREFIX) :].strip()
    if not asset:
        raise CoordError("data resource requires a non-empty asset-id", code="invalid_resource")
    if any(sep in asset for sep in ("\\", "/")):
        raise CoordError("data asset-id cannot contain path separators", code="invalid_resource")
    return DATA_PREFIX + asset


def canonicalize_scope(raw: str) -> str:
    text = _nfc(raw.strip())
    if text.startswith(DATA_PREFIX):
        raise CoordError("scope resource cannot use the data: prefix", code="invalid_resource")
    if text.startswith(SCOPE_PREFIX):
        text = text[len(SCOPE_PREFIX) :]
    text = text.strip()
    if not text or len(text) > 512:
        raise CoordError("write-scope prefix must be 1..512 characters", code="invalid_resource")
    if "\\" in text or SCOPE_WILDCARD_RE.search(text):
        raise CoordError(
            "write-scope prefix must use normalized POSIX syntax without wildcards",
            code="invalid_resource",
        )
    if text.startswith("/") or DRIVE_RE.match(text):
        raise CoordError("write-scope prefix must be repository-relative", code="invalid_resource")
    is_directory = text.endswith("/")
    body = text[:-1] if is_directory else text
    parts = body.split("/")
    if not body or any(part in {"", ".", ".."} for part in parts):
        raise CoordError(
            "write-scope prefix contains an empty, dot, or parent segment",
            code="invalid_resource",
        )
    return body + ("/" if is_directory else "")


def _expand_win_long_path(path: Path) -> Path:
    if sys.platform != "win32" or not path.exists():
        return path
    import ctypes

    get_long = ctypes.windll.kernel32.GetLongPathNameW
    get_long.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
    get_long.restype = ctypes.c_uint
    buf = ctypes.create_unicode_buffer(32768)
    length = get_long(str(path), buf, 32768)
    if length == 0:
        return path
    return Path(buf.value)


def canonicalize_path(raw: str, cwd: Path | None = None) -> str:
    text = _nfc(raw.strip())
    if text.startswith(DATA_PREFIX) or text.startswith(SCOPE_PREFIX):
        raise CoordError("filesystem resource cannot use data: or scope: prefixes", code="invalid_resource")
    base = cwd if cwd is not None else Path.cwd()
    path = Path(text)
    if not path.is_absolute():
        path = base / path
    resolved = _expand_win_long_path(path.resolve())
    canonical = os.path.normcase(os.path.abspath(str(resolved)))
    if not canonical:
        raise CoordError("filesystem resource resolved empty", code="invalid_resource")
    return canonical


def looks_like_windows_path(text: str) -> bool:
    return bool(WIN_ABS_RE.match(text) or text.startswith("\\\\") or "\\" in text)


def looks_like_scope(text: str) -> bool:
    try:
        canonicalize_scope(text)
        return True
    except CoordError:
        return False


def detect_kind(raw: str, cwd: Path | None = None) -> str:
    text = raw.strip()
    if text.startswith(DATA_PREFIX):
        return "data"
    if text.startswith(SCOPE_PREFIX):
        return "scope"
    if text.startswith(AGENT_PREFIX):
        return "agent"
    if looks_like_windows_path(text) or WIN_ABS_RE.match(text) or text.startswith("//"):
        return "path"
    if text.endswith("/") and looks_like_scope(text):
        return "scope"
    if looks_like_scope(text):
        return "scope"
    return "path"


def canonicalize_resource(
    raw: str,
    *,
    kind: str | None = None,
    cwd: Path | None = None,
) -> str:
    text = raw.strip()
    if not text:
        raise CoordError("resource_id is empty", code="invalid_resource")
    detected = kind or detect_kind(text, cwd)
    if detected == "data":
        value = canonicalize_data(text)
    elif detected == "scope":
        value = canonicalize_scope(text)
    elif detected == "path":
        value = canonicalize_path(text, cwd)
    elif detected == "agent":
        value = canonicalize_agent(text)
    else:
        raise CoordError(f"unknown resource kind: {detected}", code="invalid_resource")
    if len(value) > MAX_RESOURCE_CHARS:
        raise CoordError("canonical resource_id exceeds 4096 characters", code="invalid_resource")
    return value


def is_filesystem_id(resource_id: str) -> bool:
    return bool(
        WIN_ABS_RE.match(resource_id)
        or resource_id.startswith("\\\\")
        or resource_id.startswith("//")
        or (resource_id.startswith("/") and not resource_id.startswith(DATA_PREFIX))
    )
