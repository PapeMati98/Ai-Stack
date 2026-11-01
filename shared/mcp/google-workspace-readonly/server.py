"""Read-only stdio MCP server for Gmail + Google Drive metadata and text.

Speaks standard MCP stdio: one UTF-8 JSON-RPC object per line on stdout
(no Content-Length framing). All Google access is fixed REST GET endpoints
with Gmail/Drive read-only scopes and a verified policy account.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

# -I removes environment/current-directory import influence; re-add own dir first.
sys.path.insert(0, str(Path(__file__).resolve().parent))

PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")
SERVER_NAME = "google-workspace-readonly"
SERVER_VERSION = "0.1.0"
MAX_MESSAGE_BYTES = 128 * 1024
INPUT_LIMIT_BYTES = 128 * 1024
HTTP_RESPONSE_LIMIT_BYTES = 2 * 1024 * 1024
TOOL_CONTENT_LIMIT_BYTES = 64 * 1024
DEFAULT_TIMEOUT_SECONDS = 20.0
REFRESH_TIMEOUT_SECONDS = 30.0
ONE_PAGE_MAX = 50

SCOPES = frozenset({
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
})

POLICY_KEYS = ("account", "config_root", "gcloud_python_entrypoint")

ACCOUNT_RE = re.compile(r"^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$")
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
PAGE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_~\-/+=.]{1,4096}$")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

# Exact fixed GET allowlist: (host, path-regex). IDs are validated before use.
ALLOWLIST: tuple[tuple[str, str], ...] = (
    ("www.googleapis.com", r"/oauth2/v2/userinfo"),
    ("gmail.googleapis.com", r"/gmail/v1/users/me/profile"),
    ("gmail.googleapis.com", r"/gmail/v1/users/me/messages"),
    ("gmail.googleapis.com", r"/gmail/v1/users/me/messages/[A-Za-z0-9_-]{1,256}"),
    ("gmail.googleapis.com", r"/gmail/v1/users/me/labels"),
    ("www.googleapis.com", r"/drive/v3/about"),
    ("www.googleapis.com", r"/drive/v3/files"),
    ("www.googleapis.com", r"/drive/v3/files/[A-Za-z0-9_-]{1,256}"),
    ("www.googleapis.com", r"/drive/v3/files/[A-Za-z0-9_-]{1,256}/export"),
)

HEADER_ALLOW = ("From", "To", "Cc", "Date", "Subject")
MAX_MIME_DEPTH = 8
MAX_MIME_PARTS = 50

TEXT_EXPORTS = {
    "application/vnd.google-apps.document": ("text/plain", "Google Doc text export"),
    "application/vnd.google-apps.spreadsheet": ("text/csv", "first-sheet CSV export only"),
}
PLAIN_MIMES = {"text/plain"}


class PolicyError(Exception):
    def __init__(self, message: str = "policy rejected") -> None:
        super().__init__(message)
        self.public_message = message


class SafeToolError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.public_message = message


@dataclass(frozen=True)
class Policy:
    account: str
    config_root: str
    gcloud_python_entrypoint: str


def repository_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "shared" / "mcp" / "gcp-readonly").is_dir():
            return parent
    return here.parent


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate key")
        obj[key] = value
    return obj


def _load_json_object(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw, object_pairs_hook=_pairs)
    except (json.JSONDecodeError, ValueError) as exc:
        raise PolicyError("policy is not valid JSON") from exc
    if not isinstance(value, dict):
        raise PolicyError("policy must be a JSON object")
    return value


def _absolute_path(value: Any) -> Path:
    if not isinstance(value, str) or value == "":
        raise PolicyError("policy path rejected")
    path = Path(value)
    if not path.is_absolute():
        raise PolicyError("policy path must be absolute")
    return path


def _reject_link(path: Path) -> Path:
    # Reject symlinks / Windows reparse points anywhere on the path.
    for node in [path, *path.parents]:
        try:
            meta = node.lstat()
        except OSError:
            continue
        if node.is_symlink():
            raise PolicyError("policy path must not be a link")
        if getattr(meta, "st_file_attributes", 0) & 0x400:
            raise PolicyError("policy path must not be a link")
    return path


def load_policy(raw: str) -> Policy:
    data = _load_json_object(raw)
    if set(data) != set(POLICY_KEYS):
        raise PolicyError("unexpected policy property")
    account = data["account"]
    if not isinstance(account, str) or ACCOUNT_RE.fullmatch(account) is None:
        raise PolicyError("policy account rejected")
    config_root = _reject_link(_absolute_path(data["config_root"]))
    entrypoint = _reject_link(_absolute_path(data["gcloud_python_entrypoint"]))
    root = repository_root().resolve()
    for candidate in (config_root, entrypoint):
        resolved = candidate.resolve()
        if resolved == root or _is_relative_to(resolved, root):
            raise PolicyError("private paths must stay outside the repository")
    if entrypoint.name.lower() != "gcloud.py":
        raise PolicyError("gcloud entrypoint rejected")
    return Policy(account=account, config_root=str(config_root),
                  gcloud_python_entrypoint=str(entrypoint))


def load_policy_file(path: str) -> Policy:
    file_path = Path(path)
    if not file_path.is_absolute():
        raise PolicyError("policy path must be absolute")
    _reject_link(file_path)
    try:
        if not file_path.is_file():
            raise PolicyError("policy file is unavailable")
        if file_path.stat().st_size > MAX_MESSAGE_BYTES:
            raise PolicyError("policy file is unavailable")
        raw = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PolicyError("policy file is unavailable") from exc
    return load_policy(raw)


def _check_credential_path(policy: Policy, credential_file: str) -> Path:
    path = Path(credential_file)
    if not path.is_absolute():
        raise PolicyError("credential path must be absolute")
    _reject_link(path)
    resolved = path.resolve()
    root = repository_root().resolve()
    if resolved == root or _is_relative_to(resolved, root):
        raise PolicyError("credential path must stay outside the repository")
    config_root = Path(policy.config_root).resolve()
    if resolved != config_root and not _is_relative_to(resolved, config_root):
        raise PolicyError("credential path must stay inside config_root")
    return resolved


def _fail(message: str = "request failed") -> SafeToolError:
    return SafeToolError(message)


def check_granted_scopes(granted: Any) -> None:
    """Reject OAuth scope widening or narrowing against the exact set."""
    try:
        granted_set = set(granted or ())
    except TypeError:
        raise _fail("OAuth scopes rejected") from None
    if granted_set != set(SCOPES):
        raise _fail("OAuth scopes rejected")


# ---------------------------------------------------------------- transport ---

def _vendor_dir(policy: Policy) -> Path:
    parent = Path(policy.gcloud_python_entrypoint).resolve().parent
    direct = parent / "third_party"
    if (direct / "google" / "auth" / "__init__.py").is_file():
        return direct
    sibling = parent.parent / "lib" / "third_party"
    if (sibling / "google" / "auth" / "__init__.py").is_file():
        return sibling
    return direct


def production_session_factory(policy: Policy, credential_file: Path):
    """Build an AuthorizedSession from the trusted SDK-bundled libraries."""
    vendor = _vendor_dir(policy)
    if not (vendor / "google" / "auth" / "__init__.py").is_file():
        raise _fail("authentication library unavailable")
    sys.path.insert(0, str(vendor))
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import AuthorizedSession, Request
        import requests
    except Exception as exc:
        raise _fail("authentication library unavailable") from exc

    class ReadOnlyCredentials(Credentials):
        def refresh(self, request):
            try:
                super().refresh(request)
                check_granted_scopes(self.granted_scopes if self.granted_scopes is not None else self.scopes)
            except SafeToolError:
                raise
            except Exception:
                raise _fail("OAuth refresh requires attention") from None

    try:
        creds = ReadOnlyCredentials.from_authorized_user_file(
            str(credential_file), scopes=sorted(SCOPES))
    except Exception:
        raise _fail("credential file rejected") from None
    if not creds.refresh_token:
        raise _fail("persistent OAuth authorization required")

    def make_request():
        refresh_session = requests.Session()
        refresh_session.trust_env = False
        request = Request(session=refresh_session)

        def bounded(*args, **kwargs):
            kwargs["timeout"] = REFRESH_TIMEOUT_SECONDS
            return request(*args, **kwargs)

        return creds, bounded

    creds_holder, bounded = make_request()
    session = AuthorizedSession(creds_holder, auth_request=bounded)
    session.trust_env = False
    session._google_creds = creds_holder
    session._google_bounded_request = bounded
    session._google_refreshed = False
    return session


class GoogleBackend:
    """Fixed GET-only transport. No shell, no subprocess, no header override."""

    def __init__(self, policy: Policy, credential_file: str,
                 session_factory=None) -> None:
        self.policy = policy
        self.credential_file = _check_credential_path(policy, credential_file)
        self._factory = session_factory or production_session_factory
        self._transport = None

    def _session(self):
        if self._transport is None:
            self._transport = self._factory(self.policy, self.credential_file)
        return self._transport

    def _ensure_first_refresh(self) -> None:
        session = self._session()
        if getattr(session, "_google_refreshed", True):
            return
        creds = getattr(session, "_google_creds", None)
        bounded = getattr(session, "_google_bounded_request", None)
        if creds is None or bounded is None:
            session._google_refreshed = True
            return
        try:
            creds.refresh(bounded)  # force refresh on first authenticated call
        except SafeToolError:
            raise
        except Exception:
            raise _fail("OAuth refresh requires attention") from None
        session._google_refreshed = True

    def _check_url(self, url: str) -> None:
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.hostname is None
                or parts.port or parts.username or parts.password
                or parts.query or parts.fragment):
            raise _fail("endpoint rejected")
        for host, pattern in ALLOWLIST:
            if parts.hostname == host and re.fullmatch(pattern, parts.path):
                return
        raise _fail("endpoint rejected")

    def _fetch_raw(self, url: str, params: Mapping[str, Any],
                   timeout: float, maximum: int) -> bytes:
        self._check_url(url)
        if not isinstance(params, dict):
            raise _fail("request rejected")
        self._ensure_first_refresh()
        try:
            response = self._session().request(
                "GET", url, params=dict(params),
                timeout=min(float(timeout), REFRESH_TIMEOUT_SECONDS),
                allow_redirects=False, stream=True)
            try:
                if getattr(response, "status_code", 0) != 200:
                    raise _fail("Google read unavailable")
                if getattr(response, "is_redirect", False):
                    raise _fail("redirect rejected")
                history = getattr(response, "history", None)
                if history:
                    raise _fail("redirect rejected")
                content = bytearray()
                for chunk in response.iter_content(chunk_size=4096):
                    if chunk:
                        content.extend(chunk)
                    if len(content) > maximum:
                        raise _fail("response exceeds limit")
                return bytes(content)
            finally:
                try:
                    response.close()
                except Exception:
                    pass
        except SafeToolError:
            raise
        except Exception:
            raise _fail() from None

    def _get(self, url: str, params: Mapping[str, Any],
             timeout: float, maximum: int) -> Any:
        raw = self._fetch_raw(url, params, timeout, maximum)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise _fail("response rejected") from None
        try:
            return json.loads(text, object_pairs_hook=_pairs)
        except (json.JSONDecodeError, ValueError):
            raise _fail("response rejected") from None

    def _get_text(self, url: str, params: Mapping[str, Any],
                  timeout: float, maximum: int) -> str:
        """Raw text body for alt=media / export (no JSON decoding)."""
        raw = self._fetch_raw(url, params, timeout, maximum)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            raise _fail("response rejected") from None

    def identity(self, timeout: float, maximum: int) -> str:
        data = self._get("https://www.googleapis.com/oauth2/v2/userinfo",
                         {"fields": "email,verified_email"}, timeout, maximum)
        if not isinstance(data, dict):
            raise _fail("account verification failed")
        if data.get("email") != self.policy.account or data.get("verified_email") is not True:
            raise _fail("OAuth account does not match policy")
        return self.policy.account


# ------------------------------------------------------------------ helpers ---

def _bounded_str(value: Any, *, max_len: int = 4096, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise _fail("invalid argument")
    if len(value.encode("utf-8")) > INPUT_LIMIT_BYTES:
        raise _fail("argument exceeds limit")
    if len(value) > max_len:
        raise _fail("argument exceeds limit")
    if not allow_empty and value == "":
        raise _fail("invalid argument")
    if CONTROL_RE.search(value):
        raise _fail("invalid argument")
    return value


def _resource_id(value: Any) -> str:
    text = _bounded_str(value, max_len=256)
    if ID_RE.fullmatch(text) is None:
        raise _fail("invalid id")
    return text


def _page_args(arguments: Mapping[str, Any], query_key: str) -> tuple[str, int, str | None]:
    query = _bounded_str(arguments.get("query", ""), max_len=4096, allow_empty=True)
    raw_max = arguments.get("max_results", 20)
    if isinstance(raw_max, bool) or not isinstance(raw_max, int):
        raise _fail("invalid max_results")
    if not 1 <= raw_max <= ONE_PAGE_MAX:
        raise _fail("invalid max_results")
    token = arguments.get("page_token")
    if token is None:
        page_token = None
    else:
        page_token = _bounded_str(token, max_len=4096)
        if PAGE_TOKEN_RE.fullmatch(page_token) is None:
            raise _fail("invalid page_token")
    return query, raw_max, page_token


def _reject_unknown(arguments: Mapping[str, Any], allowed: set[str]) -> None:
    if not isinstance(arguments, dict):
        raise _fail("unknown arguments")
    if set(arguments) - allowed:
        raise _fail("unknown arguments")


def _cap_text(text: str) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) > TOOL_CONTENT_LIMIT_BYTES:
        truncated = encoded[:TOOL_CONTENT_LIMIT_BYTES]
        return truncated.decode("utf-8", errors="ignore")
    return text


def _clean_display(value: Any, max_len: int = 512) -> str | None:
    if not isinstance(value, str) or value == "" or len(value) > max_len:
        return None
    if CONTROL_RE.search(value):
        return None
    return value


def _gmail_profile_matches(data: Any, account: str) -> None:
    if not isinstance(data, dict) or data.get("emailAddress") != account:
        raise _fail("mailbox identity mismatch")


def _drive_owner_matches(data: Any, account: str) -> None:
    if not isinstance(data, dict):
        raise _fail("Drive identity mismatch")
    user = data.get("user")
    if not isinstance(user, dict):
        raise _fail("Drive identity mismatch")
    email = user.get("emailAddress")
    if not isinstance(email, str) or not email or email != account:
        raise _fail("Drive identity mismatch")


def _escape_drive_literal(value: str) -> str:
    """Escape caller data as one single-quoted Drive query literal."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _drive_text(session: Session, url: str, params: Mapping[str, Any]) -> str:
    """Fetch raw text bodies; accept legacy {"text": ...} test doubles."""
    getter = getattr(session.backend, "_get_text", None)
    if callable(getter):
        data = getter(url, params, session.timeout,
                      TOOL_CONTENT_LIMIT_BYTES + 1024)
        if not isinstance(data, str):
            raise _fail("response rejected")
        return data
    data = session.backend._get(url, params, session.timeout,
                                TOOL_CONTENT_LIMIT_BYTES + 1024)
    if isinstance(data, str):
        return data
    if isinstance(data, dict) and isinstance(data.get("text"), str):
        return data["text"]
    raise _fail("response rejected")


def _decode_part_data(data: Any, acc: list[str], budget: list[int]) -> None:
    if not isinstance(data, str) or not data:
        return
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (binascii.Error, ValueError):
        return
    try:
        text = raw.decode("utf-8", errors="ignore")
    except Exception:
        return
    room = TOOL_CONTENT_LIMIT_BYTES - budget[0]
    if room <= 0:
        return
    take = text[:room]
    acc.append(take)
    budget[0] += len(take.encode("utf-8"))


def _collect_plain_text(payload: Any, depth: int, counter: list[int],
                        acc: list[str], budget: list[int]) -> None:
    if not isinstance(payload, dict) or depth > MAX_MIME_DEPTH:
        return
    counter[0] += 1
    if counter[0] > MAX_MIME_PARTS:
        return
    mime = payload.get("mimeType")
    body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
    if isinstance(mime, str) and mime.startswith("text/plain"):
        _decode_part_data(body.get("data"), acc, budget)
    for part in payload.get("parts", []) if isinstance(payload.get("parts"), list) else []:
        if counter[0] >= MAX_MIME_PARTS or budget[0] >= TOOL_CONTENT_LIMIT_BYTES:
            return
        _collect_plain_text(part, depth + 1, counter, acc, budget)


def gmail_text(payload: Any) -> str:
    acc: list[str] = []
    _collect_plain_text(payload if isinstance(payload, dict) else {}, 0, [0], acc, [0])
    return _cap_text("".join(acc))


def gmail_headers(payload: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    headers = payload.get("headers") if isinstance(payload, dict) else None
    if not isinstance(headers, list):
        return result
    for item in headers:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        value = item.get("value")
        if name in HEADER_ALLOW and isinstance(value, str):
            cleaned = _clean_display(value)
            if cleaned is not None:
                result[name] = cleaned
    return result


def drive_file_projection(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    file_id = item.get("id")
    name = item.get("name")
    if not isinstance(file_id, str) or ID_RE.fullmatch(file_id) is None:
        return None
    if not isinstance(name, str) or _clean_display(name) is None:
        return None
    out: dict[str, Any] = {"id": file_id, "name": _clean_display(name)}
    for key in ("mimeType", "modifiedTime"):
        value = item.get(key)
        if isinstance(value, str) and _clean_display(value, 256) is not None:
            out[key] = value
    size = item.get("size")
    if isinstance(size, str) and size.isdigit() and len(size) <= 20:
        out["size"] = size
    checksum = item.get("md5Checksum")
    if isinstance(checksum, str) and re.fullmatch(r"[a-f0-9]{32}", checksum) is not None:
        out["md5Checksum"] = checksum
    return out


# -------------------------------------------------------------------- tools ---

class Session:
    def __init__(self, policy: Policy, backend: Any,
                 timeout: float = DEFAULT_TIMEOUT_SECONDS,
                 max_output: int = HTTP_RESPONSE_LIMIT_BYTES,
                 allowed_tools: tuple[str, ...] | None = None) -> None:
        self.policy = policy
        self.backend = backend
        self.timeout = timeout
        self.max_output = max_output
        self.initialized = False
        self.allowed_tools = tuple(TOOLS) if allowed_tools is None else tuple(allowed_tools)
        if not self.allowed_tools or any(name not in TOOLS for name in self.allowed_tools):
            raise PolicyError("tool allowlist rejected")


def _auth(session: Session) -> str:
    return session.backend.identity(session.timeout, session.max_output)


def workspace_context(session: Session, arguments: Mapping[str, Any]) -> dict[str, Any]:
    _reject_unknown(arguments, set())
    email = _auth(session)
    return {"account": email, "scopes": sorted(SCOPES),
            "readonly": True, "tools": sorted(session.allowed_tools)}


def gmail_search(session: Session, arguments: Mapping[str, Any]) -> dict[str, Any]:
    _reject_unknown(arguments, {"query", "max_results", "page_token"})
    query, limit, token = _page_args(arguments, "query")
    email = _auth(session)
    profile = session.backend._get(
        "https://gmail.googleapis.com/gmail/v1/users/me/profile",
        {"fields": "emailAddress"}, session.timeout, session.max_output)
    _gmail_profile_matches(profile, email)
    params: dict[str, Any] = {"q": query, "maxResults": limit,
                              "fields": "messages(id,threadId),nextPageToken,resultSizeEstimate"}
    if token is not None:
        params["pageToken"] = token
    data = session.backend._get(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages",
        params, session.timeout, session.max_output)
    if not isinstance(data, dict):
        raise _fail("response rejected")
    messages = data.get("messages", [])
    if not isinstance(messages, list):
        raise _fail("response rejected")
    cleaned = []
    for item in messages[:limit]:
        if isinstance(item, dict) and isinstance(item.get("id"), str) \
                and ID_RE.fullmatch(item["id"]):
            entry: dict[str, Any] = {"id": item["id"]}
            if isinstance(item.get("threadId"), str) and ID_RE.fullmatch(item["threadId"]):
                entry["threadId"] = item["threadId"]
            cleaned.append(entry)
    out: dict[str, Any] = {"messages": cleaned}
    next_token = data.get("nextPageToken")
    if isinstance(next_token, str) and PAGE_TOKEN_RE.fullmatch(next_token):
        out["nextPageToken"] = next_token
    return out


def gmail_read(session: Session, arguments: Mapping[str, Any]) -> dict[str, Any]:
    _reject_unknown(arguments, {"message_id"})
    message_id = _resource_id(arguments.get("message_id"))
    email = _auth(session)
    profile = session.backend._get(
        "https://gmail.googleapis.com/gmail/v1/users/me/profile",
        {"fields": "emailAddress"}, session.timeout, session.max_output)
    _gmail_profile_matches(profile, email)
    data = session.backend._get(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/" + message_id,
        {"format": "full", "fields": "id,threadId,payload(headers,mimeType,body/data,parts)"},
        session.timeout, session.max_output)
    if not isinstance(data, dict) or data.get("id") != message_id:
        raise _fail("message identity mismatch")
    payload = data.get("payload", {})
    result: dict[str, Any] = {"id": message_id}
    if isinstance(data.get("threadId"), str):
        result["threadId"] = data["threadId"]
    result["headers"] = gmail_headers(payload)
    result["text"] = gmail_text(payload)
    return result


def gmail_labels(session: Session, arguments: Mapping[str, Any]) -> dict[str, Any]:
    _reject_unknown(arguments, set())
    email = _auth(session)
    profile = session.backend._get(
        "https://gmail.googleapis.com/gmail/v1/users/me/profile",
        {"fields": "emailAddress"}, session.timeout, session.max_output)
    _gmail_profile_matches(profile, email)
    data = session.backend._get(
        "https://gmail.googleapis.com/gmail/v1/users/me/labels",
        {"fields": "labels(id,name,type)"}, session.timeout, session.max_output)
    if not isinstance(data, dict):
        raise _fail("response rejected")
    labels = data.get("labels", [])
    if not isinstance(labels, list):
        raise _fail("response rejected")
    cleaned = []
    for item in labels[:100]:
        if not isinstance(item, dict):
            continue
        label_id = item.get("id")
        name = item.get("name")
        if not isinstance(label_id, str) or _clean_display(label_id, 128) is None:
            continue
        if not isinstance(name, str) or _clean_display(name, 128) is None:
            continue
        cleaned.append({"id": label_id, "name": name})
    return {"labels": cleaned}


def drive_search(session: Session, arguments: Mapping[str, Any]) -> dict[str, Any]:
    _reject_unknown(arguments, {"query", "max_results", "page_token"})
    query, limit, token = _page_args(arguments, "query")
    _auth(session)
    clause = ("trashed = false and fullText contains "
                f"'{_escape_drive_literal(query)}'") if query else "trashed = false"
    params: dict[str, Any] = {
        "q": clause,
        "pageSize": limit,
        "fields": "files(id,name,mimeType,size,modifiedTime,md5Checksum,trashed),nextPageToken",
    }
    if token is not None:
        params["pageToken"] = token
    data = session.backend._get("https://www.googleapis.com/drive/v3/files",
                                params, session.timeout, session.max_output)
    if not isinstance(data, dict):
        raise _fail("response rejected")
    files = data.get("files", [])
    if not isinstance(files, list):
        raise _fail("response rejected")
    cleaned = []
    for item in files[:limit]:
        if isinstance(item, dict) and item.get("trashed") is True:
            continue
        projected = drive_file_projection(item)
        if projected is not None:
            cleaned.append(projected)
    out: dict[str, Any] = {"files": cleaned}
    next_token = data.get("nextPageToken")
    if isinstance(next_token, str) and PAGE_TOKEN_RE.fullmatch(next_token):
        out["nextPageToken"] = next_token
    return out


def drive_metadata(session: Session, arguments: Mapping[str, Any]) -> dict[str, Any]:
    _reject_unknown(arguments, {"file_id"})
    file_id = _resource_id(arguments.get("file_id"))
    _auth(session)
    data = session.backend._get(
        "https://www.googleapis.com/drive/v3/files/" + file_id,
        {"fields": "id,name,mimeType,size,modifiedTime,md5Checksum,trashed"},
        session.timeout, session.max_output)
    if not isinstance(data, dict) or data.get("id") != file_id:
        raise _fail("file identity mismatch")
    if data.get("trashed") is True:
        raise _fail("file is trashed")
    projected = drive_file_projection(data)
    if projected is None:
        raise _fail("response rejected")
    return projected


def drive_read_text(session: Session, arguments: Mapping[str, Any]) -> dict[str, Any]:
    _reject_unknown(arguments, {"file_id"})
    file_id = _resource_id(arguments.get("file_id"))
    _auth(session)
    meta = session.backend._get(
        "https://www.googleapis.com/drive/v3/files/" + file_id,
        {"fields": "id,name,mimeType,size,modifiedTime,md5Checksum,trashed"},
        session.timeout, session.max_output)
    if not isinstance(meta, dict) or meta.get("id") != file_id:
        raise _fail("file identity mismatch")
    if meta.get("trashed") is True:
        raise _fail("file is trashed")
    mime = meta.get("mimeType")
    if not isinstance(mime, str):
        raise _fail("response rejected")
    projected = drive_file_projection(meta)
    if mime in PLAIN_MIMES:
        text = _drive_text(
            session,
            "https://www.googleapis.com/drive/v3/files/" + file_id,
            {"alt": "media"})
        return {"file": projected, "text": _cap_text(text),
                "truncated": len(text.encode("utf-8")) > TOOL_CONTENT_LIMIT_BYTES}
    if mime in TEXT_EXPORTS:
        export_mime, note = TEXT_EXPORTS[mime]
        text = _drive_text(
            session,
            "https://www.googleapis.com/drive/v3/files/" + file_id + "/export",
            {"mimeType": export_mime})
        result: dict[str, Any] = {"file": projected, "text": _cap_text(text), "note": note,
                                 "truncated": len(text.encode("utf-8")) > TOOL_CONTENT_LIMIT_BYTES}
        return result
    raise _fail("unsupported format: metadata available, text export unavailable")


def drive_storage(session: Session, arguments: Mapping[str, Any]) -> dict[str, Any]:
    _reject_unknown(arguments, set())
    email = _auth(session)
    data = session.backend._get(
        "https://www.googleapis.com/drive/v3/about",
        {"fields": "user(displayName,emailAddress),storageQuota"},
        session.timeout, session.max_output)
    if not isinstance(data, dict):
        raise _fail("response rejected")
    _drive_owner_matches(data, email)
    quota = data.get("storageQuota")
    if not isinstance(quota, dict):
        raise _fail("response rejected")
    cleaned: dict[str, str] = {}
    for key in ("limit", "usage", "usageInDrive", "usageInDriveTrash"):
        value = quota.get(key)
        if isinstance(value, str) and value.isdigit() and len(value) <= 20:
            cleaned[key] = value
    return {"account": email, "storageQuota": cleaned}


TOOLS: dict[str, Callable[[Session, Mapping[str, Any]], dict[str, Any]]] = {
    "workspace_context": workspace_context,
    "gmail_search": gmail_search,
    "gmail_read": gmail_read,
    "gmail_labels": gmail_labels,
    "drive_search": drive_search,
    "drive_metadata": drive_metadata,
    "drive_read_text": drive_read_text,
    "drive_storage": drive_storage,
}

TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "workspace_context": {"description": "Report the verified account, readonly scopes and tool allowlist.",
                          "properties": {}, "required": []},
    "gmail_search": {"description": "Search Gmail messages; returns message/thread IDs and pagination.",
                     "properties": {"query": {"type": "string", "maxLength": 4096},
                                    "max_results": {"type": "integer", "minimum": 1, "maximum": 50},
                                    "page_token": {"type": "string", "maxLength": 4096}},
                     "required": ["query"]},
    "gmail_read": {"description": "Read one Gmail message: selected headers plus bounded plain text, no attachments.",
                   "properties": {"message_id": {"type": "string", "maxLength": 256}}, "required": ["message_id"]},
    "gmail_labels": {"description": "List Gmail label IDs and names.",
                     "properties": {}, "required": []},
    "drive_search": {"description": "Search Drive files; projected metadata, trashed excluded.",
                     "properties": {"query": {"type": "string", "maxLength": 4096},
                                    "max_results": {"type": "integer", "minimum": 1, "maximum": 50},
                                    "page_token": {"type": "string", "maxLength": 4096}},
                     "required": ["query"]},
    "drive_metadata": {"description": "Return projected metadata for one Drive file.",
                       "properties": {"file_id": {"type": "string", "maxLength": 256}}, "required": ["file_id"]},
    "drive_read_text": {"description": "Read bounded plain text or Google Doc/Sheet text export for one file.",
                        "properties": {"file_id": {"type": "string", "maxLength": 256}}, "required": ["file_id"]},
    "drive_storage": {"description": "Return Drive user and storageQuota counters only.",
                      "properties": {}, "required": []},
}


def tool_descriptors(allowed_tools=None) -> list[dict[str, Any]]:
    items = []
    for name, schema in TOOL_SCHEMAS.items():
        if allowed_tools is not None and name not in allowed_tools:
            continue
        items.append({"name": name, "description": schema["description"],
                      "inputSchema": {"type": "object", "properties": schema["properties"],
                                      "required": schema["required"], "additionalProperties": False},
                      "annotations": {"readOnlyHint": True, "destructiveHint": False}})
    return items


def _text_result(payload: Any, is_error: bool = False) -> dict[str, Any]:
    text = payload if isinstance(payload, str) else json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def dispatch_tool(session: Session, name: Any, arguments: Any) -> dict[str, Any]:
    if not isinstance(name, str) or name not in TOOLS or name not in session.allowed_tools:
        raise SafeToolError("unknown tool")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise SafeToolError("unknown arguments")
    if len(json.dumps(arguments, separators=(",", ":")).encode("utf-8")) > INPUT_LIMIT_BYTES:
        raise SafeToolError("argument exceeds limit")
    return TOOLS[name](session, arguments)


def handle_message(message: Any, session: Session) -> dict[str, Any] | None:
    if not isinstance(message, dict):
        return _rpc_error(None, -32600, "invalid request")
    if message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return _rpc_error(message.get("id"), -32600, "invalid request")
    method = message["method"]
    has_id = "id" in message
    request_id = message.get("id")
    if method in {"notifications/initialized", "initialized"} and not has_id:
        session.initialized = True
        return None
    if not has_id:
        return None
    if method == "initialize":
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        requested = params.get("protocolVersion")
        version = requested if isinstance(requested, str) and requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        session.initialized = True
        return _rpc_result(request_id, {"protocolVersion": version,
                                        "capabilities": {"tools": {"listChanged": False}},
                                        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}})
    if method == "ping":
        return _rpc_result(request_id, {})
    if not session.initialized:
        return _rpc_error(request_id, -32600, "server not initialized")
    if method == "tools/list":
        return _rpc_result(request_id, {"tools": tool_descriptors(session.allowed_tools)})
    if method == "tools/call":
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if set(params) - {"name", "arguments", "_meta"}:
            return _rpc_error(request_id, -32602, "unknown arguments")
        if "_meta" in params:
            meta = params["_meta"]
            try:
                valid_meta = isinstance(meta, dict) and len(
                    json.dumps(meta, separators=(",", ":")).encode("utf-8")) <= 8192
            except (TypeError, ValueError, RecursionError):
                valid_meta = False
            if not valid_meta:
                return _rpc_error(request_id, -32602, "invalid request metadata")
        try:
            payload = dispatch_tool(session, params.get("name"), params.get("arguments"))
        except SafeToolError as exc:
            return _rpc_result(request_id, _text_result(exc.public_message, is_error=True))
        return _rpc_result(request_id, _text_result(payload, is_error=False))
    return _rpc_error(request_id, -32601, "method not found")


def _rpc_result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def read_message(stdin: Any, max_bytes: int = MAX_MESSAGE_BYTES) -> dict[str, Any] | None:
    body = stdin.readline(max_bytes + 2)
    if body == b"":
        return None
    if len(body) > max_bytes + 1 or not body.endswith(b"\n"):
        raise SafeToolError("framing rejected")
    try:
        message = json.loads(body.decode("utf-8"), object_pairs_hook=_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise SafeToolError("parse error") from exc
    if not isinstance(message, dict):
        raise SafeToolError("invalid request")
    return message


def write_message(stdout: Any, payload: Mapping[str, Any]) -> None:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    stdout.write(body + b"\n")
    stdout.flush()


def serve(stdin: Any, stdout: Any, session: Session) -> int:
    while True:
        try:
            message = read_message(stdin)
        except SafeToolError as exc:
            if exc.public_message == "parse error":
                write_message(stdout, _rpc_error(None, -32700, "parse error"))
                continue
            if exc.public_message == "invalid request":
                write_message(stdout, _rpc_error(None, -32600, "invalid request"))
                continue
            return 1
        if message is None:
            return 0
        try:
            response = handle_message(message, session)
        except Exception:
            response = _rpc_error(message.get("id"), -32603, "tool failed")
        if response is not None:
            write_message(stdout, response)


def run_self_test() -> int:
    import unittest
    start = str(Path(__file__).resolve().parent)
    suite = unittest.defaultTestLoader.discover(start, pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Gmail/Drive MCP server")
    parser.add_argument("--policy", help="Absolute path to the trusted policy JSON")
    parser.add_argument("--credentials-file", help="Absolute path to the trusted OAuth file")
    parser.add_argument("--self-test", action="store_true", help="Run offline unit tests")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else 2
    if args.self_test:
        if args.policy is not None or args.credentials_file is not None:
            sys.stderr.write("policy rejected\n")
            return 2
        return run_self_test()
    if not args.policy or not args.credentials_file:
        sys.stderr.write("policy and credentials paths must be absolute\n")
        return 2
    try:
        policy = load_policy_file(args.policy)
        backend = GoogleBackend(policy, args.credentials_file)
        session = Session(policy, backend)
    except PolicyError as exc:
        sys.stderr.write(exc.public_message + "\n")
        return 2
    except SafeToolError as exc:
        sys.stderr.write(exc.public_message + "\n")
        return 2
    return serve(sys.stdin.buffer, sys.stdout.buffer, session)


if __name__ == "__main__":
    sys.exit(main())
