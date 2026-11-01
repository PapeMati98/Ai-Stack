"""Optional public-source classification for quality/security capture.

Default sensitive-path rejection is unchanged. A matching manifest row may
classify an eligible source file as public only after immutable GitHub raw
equality, ancestor/link checks, and both secret scanners succeed.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import stat
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlparse


FILE_LIMIT = 2 * 1024 * 1024
GROUP_LIMIT = 32 * 1024 * 1024
MANIFEST_LIMIT = 128 * 1024
MAX_ROWS = 64
OPERATION_TIMEOUT = 20.0
FETCH_TIMEOUT = 8.0
PUBLIC_HOST = "raw.githubusercontent.com"
COMMIT_RE = re.compile(r"^[a-f0-9]{40}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
RUN_ID_RE = re.compile(r"^run-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{10}$")
OWNER_RE = re.compile(r"^[A-Za-z0-9._-]+$")
ELIGIBLE_SOURCE_SUFFIXES = frozenset(
    {
        ".py",
        ".pyi",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".mts",
        ".cts",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".kts",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".hpp",
        ".cs",
        ".rb",
        ".php",
        ".swift",
        ".m",
        ".mm",
        ".scala",
        ".sh",
        ".bash",
        ".zsh",
        ".ps1",
        ".md",
        ".rst",
        ".toml",
        ".yaml",
        ".yml",
        ".json",
        ".jsonc",
        ".css",
        ".scss",
        ".less",
        ".html",
        ".vue",
        ".svelte",
        ".xml",
        ".txt",
        ".cfg",
        ".ini",
        ".cmake",
        ".gradle",
        ".r",
        ".lua",
        ".pl",
        ".ex",
        ".exs",
        ".hs",
        ".sql",
        ".zig",
        ".nim",
    }
)
FORBIDDEN_SUFFIXES = frozenset(
    {
        ".env",
        ".pem",
        ".key",
        ".pfx",
        ".p12",
        ".securestring",
        ".kdbx",
        ".db",
        ".sqlite",
        ".sqlite3",
        ".mdb",
        ".accdb",
    }
)
CREDENTIAL_FILENAMES = frozenset(
    {
        "credentials",
        "credentials.json",
        "credentials.xml",
        "credentials.yml",
        "credentials.yaml",
        ".netrc",
        "secrets.json",
        "secret.json",
    }
)
DEFAULT_SENSITIVE_SUFFIXES = frozenset({".pem", ".key", ".pfx", ".p12"})
DEFAULT_SENSITIVE_PARTS = frozenset({".env", "credentials", "secrets"})

Transport = Callable[..., bytes]
Clock = Callable[[], float]


class PublicSourceError(ValueError):
    """Fail-closed public-source classification error."""


@dataclass
class PublicSourceIndex:
    manifest_sha256: str
    rows: list[dict[str, str]]
    by_key: dict[tuple[str, str], dict[str, str]]


def digest(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def normalize_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise PublicSourceError("Invalid public-source relative path")
    value = value.replace("\\", "/")
    if value.startswith("/") or any(
        ord(c) < 32 or 127 <= ord(c) <= 159 or c in "\u2028\u2029" for c in value
    ):
        raise PublicSourceError("Unsafe public-source relative path")
    parts = value.split("/")
    if not parts or any(
        not part
        or part in {".", ".."}
        or any(c in part for c in ':<>"|?*')
        or part.endswith((".", " "))
        or re.match(r"^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(?:\.|$)", part, re.I)
        for part in parts
    ):
        raise PublicSourceError("Public-source path traversal or reserved name")
    return value


def default_sensitive_path(relative: Path) -> bool:
    lowered = {part.casefold() for part in relative.parts}
    return bool(
        ".env" in lowered
        or "credentials" in lowered
        or "secrets" in lowered
        or relative.suffix.casefold() in DEFAULT_SENSITIVE_SUFFIXES
    )


def always_forbidden_reason(relative: Path) -> str | None:
    suffix = relative.suffix.casefold()
    name = relative.name.casefold()
    for part in relative.parts:
        folded = part.casefold()
        if folded == ".env" or folded.startswith(".env."):
            return ".env"
    if suffix in FORBIDDEN_SUFFIXES or name.startswith(".env"):
        return "forbidden-suffix"
    if name in CREDENTIAL_FILENAMES:
        return "credential-container"
    return None


def eligible_source_path(relative: Path) -> bool:
    return relative.suffix.casefold() in ELIGIBLE_SOURCE_SUFFIXES


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise PublicSourceError("Duplicate public-source JSON key")
        value[key] = item
    return value


def assert_plain_ancestors(fleet: Any, path: Path, stop_at: Path) -> None:
    current = path
    seen: set[str] = set()
    while True:
        key = str(current)
        if key in seen:
            raise PublicSourceError("Public-source path ancestor cycle")
        seen.add(key)
        if fleet.is_filesystem_link(current):
            raise PublicSourceError("Linked or reparse public-source ancestor")
        if current == stop_at:
            return
        parent = current.parent
        if parent == current:
            raise PublicSourceError("Public-source path escaped its worktree")
        current = parent


def _identity(path: Path) -> tuple[int, int, int, int, int]:
    st = path.lstat()
    return (
        int(getattr(st, "st_dev", 0)),
        int(getattr(st, "st_ino", 0)),
        int(st.st_size),
        int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))),
        int(st.st_mode),
    )


def read_plain_file(
    fleet: Any,
    path: Path,
    *,
    worktree: Path,
    limit: int,
    source_path: str,
) -> bytes:
    io_path = fleet.windows_extended_io_path(path)
    assert_plain_ancestors(fleet, path, worktree)
    if not path.is_file() or fleet.is_filesystem_link(path):
        raise PublicSourceError("Public-source candidate is not a plain file")
    before = _identity(path)
    with io_path.open("rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise PublicSourceError("Public-source candidate exceeds the 2 MiB file limit")
        data = handle.read(limit + 1)
        after_fd = os.fstat(handle.fileno())
    if len(data) > limit:
        raise PublicSourceError("Public-source candidate exceeds the 2 MiB file limit")
    if (info.st_size, getattr(info, "st_mtime_ns", 0), getattr(info, "st_ino", 0)) != (
        after_fd.st_size,
        getattr(after_fd, "st_mtime_ns", 0),
        getattr(after_fd, "st_ino", 0),
    ):
        raise PublicSourceError("Public-source candidate changed during read")
    assert_plain_ancestors(fleet, path, worktree)
    if _identity(path) != before:
        raise PublicSourceError("Public-source candidate identity changed during read")
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise PublicSourceError("Public-source candidate is not UTF-8 text") from exc
    if "\x00" in text:
        raise PublicSourceError("Public-source candidate contains binary material")
    raw_findings = fleet.scan_sensitive_task(text)
    source_findings = fleet.scan_sensitive_source(data, source_path)
    if raw_findings or source_findings:
        findings = sorted(set(raw_findings + source_findings))
        raise PublicSourceError(
            "Public-source candidate appears to contain secret material: "
            + source_path
            + ": "
            + ", ".join(findings)
        )
    return data


def validate_public_url(url: Any) -> tuple[str, str]:
    if not isinstance(url, str) or not url or len(url) > 2048:
        raise PublicSourceError("Invalid public-source URL")
    if any(ord(c) < 32 or c in " \t\r\n\\" or c == "%" for c in url):
        raise PublicSourceError("Public-source URL contains unsafe characters")
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise PublicSourceError("Public source fetch requires HTTPS")
    if parsed.username or parsed.password:
        raise PublicSourceError("Public source URL must not include userinfo")
    if parsed.port is not None:
        raise PublicSourceError("Public source URL must not include a port")
    if parsed.hostname != PUBLIC_HOST or parsed.netloc != PUBLIC_HOST:
        raise PublicSourceError("Public source host is not raw.githubusercontent.com")
    if parsed.query or parsed.fragment:
        raise PublicSourceError("Public source URL must not include a query or fragment")
    parts = parsed.path.split("/")
    if (
        len(parts) < 5
        or parts[0] != ""
        or not OWNER_RE.fullmatch(parts[1] or "")
        or not OWNER_RE.fullmatch(parts[2] or "")
        or not COMMIT_RE.fullmatch(parts[3] or "")
    ):
        raise PublicSourceError("Public source URL is not an immutable 40-character commit raw path")
    if any(part in {"", ".", ".."} for part in parts[4:]):
        raise PublicSourceError("Public source URL path is unsafe")
    return url, parts[3]


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        raise PublicSourceError("Public source fetch refuses redirects")


class _RefuseHTTP(urllib.request.HTTPHandler):
    def http_open(self, req):  # type: ignore[override]
        raise PublicSourceError("Public source fetch requires HTTPS")


def _bounded_read(response: Any, *, max_bytes: int, timeout: float, started: float) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        if _clock() - started > timeout:
            raise PublicSourceError("Public-source operation exceeded its time bound")
        piece = response.read(65536)
        if not piece:
            break
        total += len(piece)
        if total > max_bytes:
            raise PublicSourceError("Public source blob exceeds the 2 MiB file limit")
        chunks.append(piece)
        if _clock() - started > timeout:
            raise PublicSourceError("Public-source operation exceeded its time bound")
    data = b"".join(chunks)
    if _clock() - started > timeout:
        raise PublicSourceError("Public-source operation exceeded its time bound")
    return data


def default_fetch(url: str, *, timeout: float, max_bytes: int) -> bytes:
    validated, _commit = validate_public_url(url)
    if timeout <= 0:
        raise PublicSourceError("Public-source operation exceeded its time bound")
    started = _clock()
    context = ssl.create_default_context()
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _RefuseRedirect,
        _RefuseHTTP,
        urllib.request.HTTPSHandler(context=context),
    )
    request = urllib.request.Request(
        validated,
        method="GET",
        headers={
            "Accept": "application/octet-stream",
            "User-Agent": "agent-fleet-public-source/0.8.68",
        },
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            if getattr(response, "url", validated) != validated:
                raise PublicSourceError("Public source fetch refuses redirects")
            length = response.headers.get("Content-Length")
            if length is not None:
                try:
                    declared = int(length)
                except ValueError as exc:
                    raise PublicSourceError("Public source Content-Length is invalid") from exc
                if declared > max_bytes:
                    raise PublicSourceError("Public source blob exceeds the 2 MiB file limit")
            return _bounded_read(
                response, max_bytes=max_bytes, timeout=timeout, started=started
            )
    except PublicSourceError:
        raise
    except urllib.error.URLError as exc:
        raise PublicSourceError("Public source blob unavailable") from exc
    except (OSError, TimeoutError, ValueError) as exc:
        raise PublicSourceError("Public source fetch failed") from exc


_transport: Transport = default_fetch
_clock: Clock = time.monotonic


def set_transport(transport: Transport | None) -> Transport:
    global _transport
    previous = _transport
    _transport = default_fetch if transport is None else transport
    return previous


def set_clock(clock: Clock | None) -> Clock:
    global _clock
    previous = _clock
    _clock = time.monotonic if clock is None else clock
    return previous


def fetch_public_blob(
    url: str,
    *,
    timeout: float = FETCH_TIMEOUT,
    max_bytes: int = FILE_LIMIT,
    transport: Transport | None = None,
    deadline: float | None = None,
    clock: Clock | None = None,
) -> bytes:
    now_fn = clock if clock is not None else _clock
    started = now_fn()
    if deadline is not None and started > deadline:
        raise PublicSourceError("Public-source operation exceeded its time bound")
    remaining = timeout
    if deadline is not None:
        remaining = min(timeout, deadline - started)
    if remaining <= 0:
        raise PublicSourceError("Public-source operation exceeded its time bound")
    worker = transport or _transport
    data = worker(url, timeout=remaining, max_bytes=max_bytes)
    finished = now_fn()
    if deadline is not None and finished > deadline:
        raise PublicSourceError("Public-source operation exceeded its time bound")
    if finished - started > timeout:
        raise PublicSourceError("Public-source operation exceeded its time bound")
    if not isinstance(data, (bytes, bytearray)):
        raise PublicSourceError("Public source transport returned non-bytes")
    if len(data) > max_bytes:
        raise PublicSourceError("Public source blob exceeds the 2 MiB file limit")
    return bytes(data)


def load_manifest(fleet: Any, manifest_path: Path) -> PublicSourceIndex:
    path = Path(manifest_path).expanduser()
    if not path.is_absolute():
        raise PublicSourceError("Public-source manifest path must be absolute")
    if fleet.is_filesystem_link(path) or any(fleet.is_filesystem_link(part) for part in path.parents):
        raise PublicSourceError("Linked public-source manifest is refused")
    if not path.is_file():
        raise PublicSourceError("Public-source manifest is not a plain file")
    data = path.read_bytes()
    if len(data) > MANIFEST_LIMIT:
        raise PublicSourceError("Public-source manifest exceeds 128 KiB")
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_object)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PublicSourceError("Public-source manifest is not valid JSON") from exc
    keys = {"schema_version", "files"}
    if not isinstance(value, dict) or set(value) != keys:
        raise PublicSourceError("Invalid public-source manifest schema")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise PublicSourceError("Invalid public-source manifest schema version")
    files = value["files"]
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_ROWS:
        raise PublicSourceError("Public-source files must contain 1..64 entries")
    rows: list[dict[str, str]] = []
    by_key: dict[tuple[str, str], dict[str, str]] = {}
    seen_urls: set[str] = set()
    for item in files:
        if not isinstance(item, dict) or set(item) != {
            "worker_run_id",
            "path",
            "sha256",
            "url",
        }:
            raise PublicSourceError("Invalid public-source file entry")
        worker_run_id = item["worker_run_id"]
        if not isinstance(worker_run_id, str) or not RUN_ID_RE.fullmatch(worker_run_id):
            raise PublicSourceError("Invalid public-source worker run ID")
        relative = normalize_relative_path(item["path"])
        expected = item["sha256"]
        if not isinstance(expected, str) or not SHA256_RE.fullmatch(expected):
            raise PublicSourceError("Invalid public-source file hash")
        url, commit = validate_public_url(item["url"])
        key = (worker_run_id, relative.casefold())
        if key in by_key:
            raise PublicSourceError("Duplicate public-source manifest row")
        if url in seen_urls:
            raise PublicSourceError("Duplicate public-source manifest URL")
        seen_urls.add(url)
        relative_path = Path(relative)
        forbidden = always_forbidden_reason(relative_path)
        if forbidden:
            raise PublicSourceError(
                "Public-source manifest lists a forbidden credential or key path"
            )
        if not eligible_source_path(relative_path):
            raise PublicSourceError("Public-source path is not an eligible source suffix")
        row = {
            "worker_run_id": worker_run_id,
            "path": relative,
            "sha256": expected,
            "url": url,
            "commit": commit,
        }
        by_key[key] = row
        rows.append(row)
    return PublicSourceIndex(manifest_sha256=digest(data), rows=rows, by_key=by_key)


def lookup(
    index: PublicSourceIndex | None, worker_run_id: str, relative_text: str
) -> dict[str, str] | None:
    if index is None:
        return None
    try:
        relative = normalize_relative_path(relative_text)
    except PublicSourceError:
        return None
    return index.by_key.get((worker_run_id, relative.casefold()))


def capture_listed_file(
    fleet: Any,
    *,
    worktree: Path,
    row: dict[str, str],
    worker_run_id: str,
    relative_text: str,
    deadline: float,
    transport: Transport | None = None,
) -> tuple[bytes, dict[str, Any]]:
    if _clock() > deadline:
        raise PublicSourceError("Public-source operation exceeded its time bound")
    if row["worker_run_id"] != worker_run_id:
        raise PublicSourceError("Public-source worker run ID does not match the candidate")
    relative = normalize_relative_path(relative_text)
    if relative.casefold() != row["path"].casefold() or relative != row["path"]:
        raise PublicSourceError("Public-source path does not match the candidate")
    relative_path = Path(relative)
    forbidden = always_forbidden_reason(relative_path)
    if forbidden:
        raise PublicSourceError("Public-source path remains forbidden")
    if not eligible_source_path(relative_path):
        raise PublicSourceError("Public-source path is not an eligible source suffix")
    lexical = worktree / relative_path
    assert_plain_ancestors(fleet, lexical, worktree)
    resolved_worktree = worktree.resolve()
    source = lexical.resolve()
    try:
        source.relative_to(resolved_worktree)
    except ValueError as exc:
        raise PublicSourceError("Public-source path escapes its worktree") from exc
    local = read_plain_file(
        fleet, source, worktree=resolved_worktree, limit=FILE_LIMIT, source_path=relative
    )
    if digest(local) != row["sha256"]:
        raise PublicSourceError("Public-source candidate hash mismatch: " + relative)
    public = fetch_public_blob(
        row["url"],
        timeout=FETCH_TIMEOUT,
        max_bytes=FILE_LIMIT,
        transport=transport,
        deadline=deadline,
    )
    if public != local:
        raise PublicSourceError("Public source blob does not equal the candidate file")
    if digest(public) != row["sha256"]:
        raise PublicSourceError("Public source blob hash mismatch: " + relative)
    assert_plain_ancestors(fleet, lexical, worktree)
    again = read_plain_file(
        fleet, source, worktree=resolved_worktree, limit=FILE_LIMIT, source_path=relative
    )
    if again != local:
        raise PublicSourceError("Public-source candidate changed after read")
    assert_plain_ancestors(fleet, lexical, worktree)
    assert_plain_ancestors(fleet, source, resolved_worktree)
    provenance = {
        "worker_run_id": worker_run_id,
        "source_path": relative,
        "sha256": row["sha256"],
        "url": row["url"],
        "commit": row["commit"],
        "classification": "public-source",
        "authorship_identity": False,
        "quality_verdict": False,
        "size_bytes": len(local),
    }
    return local, provenance


def refuse_unmatched(
    index: PublicSourceIndex | None,
    consumed: Iterable[tuple[str, str]],
    worker_ids: Iterable[str],
) -> None:
    if index is None:
        return
    seen = {(worker, path.casefold()) for worker, path in consumed}
    workers = set(worker_ids)
    for row in index.rows:
        key = (row["worker_run_id"], row["path"].casefold())
        if row["worker_run_id"] not in workers:
            raise PublicSourceError(
                "Public-source manifest lists a worker that is not in this gate"
            )
        if key not in seen:
            raise PublicSourceError(
                "Public-source manifest row did not match a captured candidate path"
            )
