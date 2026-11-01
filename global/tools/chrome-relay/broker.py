"""Scoped Fleet Chrome relay. Python 3.13 stdlib; loopback HTTP only.

Policy is operator-owned setup material, never supplied by an MCP tool call.
The extension is a separately authenticated executor, not an arbitrary JS/CDP
proxy. No upstream code has been copied into this implementation.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import MappingProxyType
from urllib.parse import parse_qsl, unquote, urlsplit

MAX_BODY = 64 * 1024
MAX_READ = 4096
MAX_REQUESTS = 10000  # Shared memory budget, reserved equally per native session.
MAX_OBSERVATIONS = 256  # Retry cache for observations and undispatched denials.
MAX_STATE_BYTES = 64 * 1024 * 1024
MAX_TABS = 20
ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
REF_RE = re.compile(r"r[1-9][0-9]*_[0-9]+\Z")
# Optional chrome_read diagnostics: counts/enums only, never page body/title/URL.
READER_STATUSES = frozenset(("ok", "empty", "loading", "partial"))
READY_STATES = frozenset(("loading", "interactive", "complete"))
DIAGNOSTIC_KEYS = ("reader_status", "ready_state", "document_count", "same_origin_frame_count", "blocked_frame_count", "main_excerpt_chars", "child_excerpt_chars")
DIAGNOSTIC_DOCUMENT_KEYS = ("document_count", "same_origin_frame_count", "blocked_frame_count")
DIAGNOSTIC_CHARS_KEYS = ("main_excerpt_chars", "child_excerpt_chars")
MAX_DIAGNOSTIC_DOCUMENTS = 64
MAX_DIAGNOSTIC_CHARS = 6000
# An optional ref URL is navigation metadata: exact assigned origin, no credentials.
REF_URL_CREDENTIAL_PARAM_RE = re.compile(r"(?:^|[&;?])(?:access_token|id_token|refresh_token|auth_token|session_token|bearer|token|api_key|apikey|secret|client_secret|password|passwd|pwd|authorization|credential|credentials|session|session_id|sessionid|signature|sig|code|auth)(?:=|$|[&;?])", re.I)
REF_URL_BEARER_RE = re.compile(r"(?:bearer|basic)(?:%20|\+|\s)[A-Za-z0-9\-._~+/]+=*", re.I)
REF_URL_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}")
REF_URL_SECRET_RE = re.compile(r"\b(?:[A-Fa-f0-9]{32,}|[A-Za-z0-9+/]{40,}={0,2})\b")
REF_URL_SECRET_PREFIX_RE = re.compile(r"\b(?:sk-|gh[pousr]_|github_pat_|AKIA)[a-z0-9_-]{8,}", re.I)
# Substring stems for decoded credential-key detection. Only stems of length
# >= 5 are matched as substrings so short exact-only names (sig, code, auth,
# pwd) cannot over-block benign keys such as "author" or "postcode"; those
# exact names remain covered by REF_URL_CREDENTIAL_PARAM_RE above.
REF_URL_CREDENTIAL_KEY_PARTS = ("token", "secret", "session", "passwd", "password",
                                "credential", "bearer", "signature", "apikey", "oauth",
                                "authorization")
# Auth/consent URL stems. Each stem is matched as a complete path/fragment
# segment (delimited by "/" / end-of-segment). The hyphen ("sign-in") and
# underscore ("sign_in") forms live side by side so Rails/Devise
# underscore-joined route paths are
# blocked the same way as the existing "signin" / "log-in" stems without
# the look-arounds having to spell out either separator.
_AUTH_STEMS = (
    "oauth", "oauth2", "authorize", "consent", "signin", "sign-in", "sign_in",
    "login", "log-in", "log_in", "password", "saml", "passkey", "mfa", "challenge",
)
_AUTH_STEM_PATTERN = r"(?:" + "|".join(_AUTH_STEMS) + r")"
# An auth stem is followed by a path-segment terminator: "/" / end-of-string,
# an Angular-style ";" path-parameter, or a static-file "." extension. The
# terminator set stays narrow on purpose so benign segments joined with "-"
# or "_" (e.g. "login-history", "authorize-foo", "signin_settings") keep
# their safe URL.
_AUTH_PATH_RE = re.compile(r"/" + _AUTH_STEM_PATTERN + r"(?:/|$|[;.])")
_AUTH_FRAGMENT_RE = re.compile(r"(?:^|/)" + _AUTH_STEM_PATTERN + r"(?:/|$|[;.])")
# Decode and screen optional metadata without losing text or opaque refs.
# Auth routes use the segment-aware matcher so login-history stays usable.
REF_URL_SENSITIVE_TERM_RE = re.compile(
    r"(?:^|[^a-z0-9])(?:api[_-]?key|secret|(?:access[_-]?|refresh[_-]?|id[_-]?)?token|"
    r"password|passwd|pwd|authorization|credential|bearer|session(?:[_-]?id)?|auth|"
    r"code|signature|client[_-]?id|redirect[_-]?uri|response[_-]?type)"
    r"(?=$|[^a-z0-9])", re.I)
# Bilingual login-name detection: English "sign in"/"log in" plus common
# Hungarian auth labels exercised by synthetic tests. The filter omits these so a translated
# login button cannot slip past as a clickable ref on the same-origin path.
_CREDENTIAL_LABEL_RE = re.compile(
    r"\b(?:password|passwd|passkey|passcode|otp|mfa|totp|consent|authorize|authorise|credential)\b"
    r"|\b(?:sign|log)\s*in\b"
    r"|\bbejelentkez\w*\b"
    r"|\bjelsz[aáoó]\w*\b"
    r"|\bhozzáférés\w*\b",
    re.I,
)


def _has_credential_key(text):
    # Fail-closed substring check over decoded query/fragment key tokens.
    # Catches suffixed keys (oauth_token, csrf_token, reset_token) and
    # percent-encoded keys (%74oken) that an exact-name match misses.
    if not isinstance(text, str) or not text:
        return False
    for segment in re.split(r"[&;]", text):
        key = segment.split("=", 1)[0]
        try:
            for _ in range(3):
                key = unquote(key)
            key = key.lower()
        except (ValueError, UnicodeError):
            return True
        if not key:
            continue
        if REF_URL_CREDENTIAL_PARAM_RE.search(key):
            return True
        if any(part in key for part in REF_URL_CREDENTIAL_KEY_PARTS):
            return True
    return False
ACTIONS = frozenset(("chrome_attachment", "chrome_open", "chrome_read", "chrome_click", "chrome_type", "chrome_wait", "chrome_close", "chrome_saved_login"))
MUTATIONS = frozenset(("chrome_open", "chrome_click", "chrome_type", "chrome_close", "chrome_saved_login"))
# Saved login is one owner-bound native submit per owned tab. It is opt-in and
# is never part of an action list unless the policy carries a saved_login binding.
LOGIN_ACTION = "chrome_saved_login"
LOGIN_PATH_RE = re.compile(r"/[A-Za-z0-9_./-]{0,200}\Z")
SUCCESS_FLAGS = {"chrome_open": "owned", "chrome_click": "clicked", "chrome_type": "typed", "chrome_close": "closed", LOGIN_ACTION: "submitted"}
REASONS = frozenset(("unknown_field", "profile", "action", "ownership", "origin", "oauth", "permission", "bound", "stopped", "technical_block", "expired", "duplicate", "cancelled", "worker_restart", "unknown", "stale_ref", "credential", "ambiguous", "not_observed", "rejected", "timeout", "broker_restart", "request_id_conflict", "state_io", "busy", "login_attempted", "needs_owner", "form_changed", "unsupported_login", "login_route"))


class Reject(ValueError):
    """Safe machine-readable rejection; never holds request input."""

    def __init__(self, reason: str, status: int = 400):
        self.reason, self.status = reason, status
        super().__init__(reason)


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result or key in ("__proto__", "prototype", "constructor"):
            raise Reject("json")
        result[key] = value
    return result


def _validate_tree(value, depth=0):
    if depth > 16:
        raise Reject("bound")
    if isinstance(value, str):
        try:
            value.encode("utf-8", "strict")
        except UnicodeError:
            raise Reject("utf8") from None
    elif isinstance(value, dict):
        if len(value) > 1000:
            raise Reject("bound")
        for key, item in value.items():
            _validate_tree(key, depth + 1)
            _validate_tree(item, depth + 1)
    elif isinstance(value, list):
        if len(value) > 1000:
            raise Reject("bound")
        for item in value:
            _validate_tree(item, depth + 1)


def parse_json(body: bytes):
    if len(body) > MAX_BODY:
        raise Reject("bound", 413)
    try:
        value = json.loads(body.decode("utf-8", "strict"), object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(Reject("json")))
        _validate_tree(value)
        return value
    except Reject:
        raise
    except (ValueError, UnicodeError, RecursionError):
        raise Reject("json") from None


def exact(value, required=(), optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() or not value.keys() <= set(required) | set(optional):
        raise Reject("unknown_field")


def ident(value):
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise Reject("identity")
    return value


def tab_id(value, zero=False):
    if type(value) is not int or not (0 if zero else 1) <= value <= 2147483647:
        raise Reject("ownership")
    return value


def utf16_length(value):
    return len(value.encode("utf-16-le")) // 2


def _url(value):
    if not isinstance(value, str) or not value or len(value) > 2048 or re.search(r"[\s\\\x00-\x1f\x7f]", value):
        raise Reject("origin")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
        if parsed.username is not None or parsed.password is not None or not host:
            raise ValueError()
        host.encode("ascii")
        if parsed.scheme not in ("https", "http") or not re.fullmatch(r"[a-zA-Z0-9.-]+", host):
            raise ValueError()
        host = host.lower()
        default = 443 if parsed.scheme == "https" else 80
        origin = parsed.scheme + "://" + host + (":" + str(port) if port is not None and port != default else "")
        return parsed, origin
    except (ValueError, UnicodeError):
        raise Reject("origin") from None


def credential_url(parsed):
    host = parsed.hostname.lower()
    if host in ("accounts.google.com", "login.microsoftonline.com", "login.live.com", "appleid.apple.com") or re.search(r"(^|\.)(auth0|okta)\.com$", host):
        return True
    path = unquote(unquote(parsed.path)).lower()
    if _AUTH_PATH_RE.search(path):
        return True
    # Auth/consent routes carried in the URL fragment (SPA hash routing such
    # as "/#/login", "/#/consent", "/#/signin", "/#/oauth/callback",
    # "/#/account/login", "/#/mfa/challenge", hashbang "#!/login",
    # "#!/mfa/challenge", Angular-style returnUrl "/#/login;returnUrl=...",
    # or static-file extensions such as "/#/login.html") must also fail
    # closed. The fragment path component is decoded twice like the rest of
    # the URL, split off any query/credentials and stripped of leading "/"
    # or hashbang "!" prefixes that SPA frameworks prepend, so the
    # auth/consent stem is matched at any segment rather than only the
    # first. Without the hashbang strip, any-segment check, ";" path-param
    # terminator and file-extension "." terminator a same-origin URL such
    # as "http://127.0.0.1:8123/#/account/login;returnUrl=%2Fdashboard"
    # would pass through the per-segment fragment check and be emitted as
    # a ref url.
    fragment = unquote(unquote(parsed.fragment)).lower()
    if fragment:
        fragment_part = fragment.split("?", 1)[0].split("&", 1)[0]
        normalized = re.sub(r"^[/!]+", "", fragment_part)
        if normalized and _AUTH_FRAGMENT_RE.search(normalized):
            return True
    for key, value in parse_qsl(parsed.query):
        key = key.lower()
        if key in ("client_id", "redirect_uri", "response_type"):
            return True
        if key in ("action", "page", "route", "mode", "view"):
            route = unquote(unquote(value)).lower().lstrip("/!")
            if _AUTH_FRAGMENT_RE.search(route) or route == "oauth2":
                return True
    return False


def credential_label(name):
    return bool(_CREDENTIAL_LABEL_RE.search(name))


def saved_login_binding(value, allowed_origins):
    """Validate the exact owner binding; no wildcard, query, fragment or IdP."""
    if isinstance(value, MappingProxyType):
        value = dict(value)
    exact(value, ("origin", "pages", "posts"))
    origin = value["origin"]
    parsed, canonical = _url(origin)
    if parsed.scheme != "https" or origin != canonical or canonical not in allowed_origins or credential_url(urlsplit(canonical + "/")):
        raise Reject("policy")
    paths = {}
    for key in ("pages", "posts"):
        items = value[key]
        if not isinstance(items, (list, tuple)) or not 1 <= len(items) <= 8 or len(items) != len(set(items)):
            raise Reject("policy")
        for item in items:
            if (not isinstance(item, str) or not LOGIN_PATH_RE.fullmatch(item) or ".." in item or "//" in item
                    or re.search(r"/(oauth2?|authorize|authorise|consent|saml|passkey|mfa|challenge)(/|$)", item, re.I)):
                raise Reject("policy")
        paths[key] = tuple(sorted(items))
    return MappingProxyType({"origin": canonical, "pages": paths["pages"], "posts": paths["posts"]})


def saved_login_digest(binding):
    # Canonical digest; added to fingerprints only when a binding exists.
    return hashlib.sha256(json_bytes(["saved_login/1", binding["origin"], list(binding["pages"]), list(binding["posts"])])).hexdigest()


@dataclass(frozen=True)
class Policy:
    profile_id: str
    allowed_origins: frozenset
    allowed_actions: frozenset
    sessions: object
    extension_token: str = field(repr=False)
    extension_id: str
    expires_at: float
    synthetic: bool = False
    saved_login: object = None

    def __post_init__(self):
        object.__setattr__(self, "sessions", MappingProxyType(dict(self.sessions)))
        object.__setattr__(self, "allowed_origins", frozenset(self.allowed_origins))
        object.__setattr__(self, "allowed_actions", frozenset(self.allowed_actions))
        if self.saved_login is not None:
            object.__setattr__(self, "saved_login", saved_login_binding(self.saved_login, self.allowed_origins))
        if (self.saved_login is not None) != (LOGIN_ACTION in self.allowed_actions):
            raise Reject("policy")

    @classmethod
    def from_dict(cls, value):
        exact(value, ("profile_id", "allowed_origins", "allowed_actions", "sessions", "extension_token", "extension_id", "expires_at"), ("schema", "synthetic", "saved_login"))
        if value.get("schema", 1) != 1 or type(value.get("synthetic", False)) is not bool:
            raise Reject("policy")
        profile = ident(value["profile_id"])
        synthetic = value.get("synthetic", False)
        if not isinstance(value["allowed_origins"], list) or not 1 <= len(value["allowed_origins"]) <= 20:
            raise Reject("policy")
        origins = []
        for raw in value["allowed_origins"]:
            parsed, origin = _url(raw)
            if parsed.path not in ("", "/") or parsed.query or parsed.fragment or credential_url(parsed):
                raise Reject("policy")
            if parsed.scheme == "http" and not (synthetic and parsed.hostname in ("127.0.0.1", "localhost")):
                raise Reject("policy")
            if origin in origins:
                raise Reject("policy")
            origins.append(origin)
        actions = value["allowed_actions"]
        if not isinstance(actions, list) or not actions or not all(isinstance(item, str) for item in actions) or len(actions) != len(set(actions)) or not set(actions) <= ACTIONS:
            raise Reject("policy")
        if not isinstance(value["sessions"], dict) or not 1 <= len(value["sessions"]) <= 100:
            raise Reject("policy")
        sessions = {}
        tokens = [value["extension_token"]]
        for sid, token in value["sessions"].items():
            sessions[ident(sid)] = token
            tokens.append(token)
        for token in tokens:
            if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9._~+/-]{16,256}={0,2}", token):
                raise Reject("policy")
        if len(tokens) != len(set(tokens)):
            raise Reject("policy")
        extension_id = value["extension_id"]
        if not isinstance(extension_id, str) or not re.fullmatch(r"[a-p]{32}", extension_id):
            raise Reject("policy")
        expires = value["expires_at"]
        try:
            if isinstance(expires, str):
                stamp = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    raise ValueError()
                expires = stamp.timestamp()
            elif type(expires) in (int, float):
                expires = float(expires)
                if expires > 100000000000:  # JS Date.now() compatibility.
                    expires /= 1000
            else:
                raise ValueError()
            if not math.isfinite(expires) or expires <= 0:
                raise ValueError()
        except ValueError:
            raise Reject("policy") from None
        return cls(profile, frozenset(origins), frozenset(actions), MappingProxyType(sessions), value["extension_token"], extension_id, expires, synthetic, value.get("saved_login"))

    def permits_url(self, value, *, saved_login_page=False):
        parsed, origin = _url(value)
        login = self.saved_login
        approved_login = (saved_login_page is True and login is not None
                          and origin == login["origin"] and parsed.path in login["pages"]
                          and not parsed.query and not parsed.fragment)
        if credential_url(parsed) and not approved_login:
            raise Reject("oauth")
        if origin not in self.allowed_origins or (parsed.scheme == "http" and not self.synthetic):
            raise Reject("origin")
        return value

    def fingerprint(self):
        # Tokens are hashed; state never contains capability material.
        data = [self.profile_id, sorted(self.allowed_origins), sorted(self.allowed_actions),
                [(sid, hashlib.sha256(token.encode()).hexdigest()) for sid, token in sorted(self.sessions.items())],
                hashlib.sha256(self.extension_token.encode()).hexdigest(), self.extension_id, self.expires_at, self.synthetic]
        if self.saved_login is not None:
            data.append(saved_login_digest(self.saved_login))
        return hashlib.sha256(json_bytes(data)).hexdigest()

    def wire_generation(self):
        # Matches the extension's compact UTF-8 JSON and integer millisecond
        # expiry. This opaque generation never replaces bearer authentication.
        data = [self.profile_id, self.extension_token, math.floor(self.expires_at * 1000)]
        if self.saved_login is not None:
            data.append(saved_login_digest(self.saved_login))
        return hashlib.sha256(json_bytes(data)).hexdigest()[:32]


@dataclass
class Request:
    sid: str
    rid: str
    wire_id: str
    action: str
    args: dict
    tab: int | None
    fingerprint: str
    epoch: int
    deadline: float
    dispatched: bool = False
    result: dict | None = None
    event: threading.Event = field(default_factory=threading.Event)


class Broker:
    def __init__(self, policy, state_path=None, audit_path=None, request_timeout=10.0, poll_timeout=20.0):
        if not isinstance(policy, Policy):
            raise Reject("policy")
        if not 0.01 <= request_timeout <= 60 or not 0 <= poll_timeout <= 25:
            raise Reject("bound")
        self.policy = policy
        self.generation = policy.wire_generation()
        self.state_path = Path(state_path) if state_path is not None else None
        self.audit_path = Path(audit_path) if audit_path is not None else None
        self.request_timeout, self.poll_timeout = request_timeout, poll_timeout
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.stop, self.authorized, self.technical_block, self.manual_tab = True, False, True, 0
        self.epoch = 0
        self.owned = self._fresh_owned()
        self.refs = {}  # (native SID, tab) -> client opaque ref -> wire ref/expiry.
        self.requests = {}
        self.completed_observations = deque()
        self.dirty_mutations = {}
        self.policy_hash = policy.fingerprint()
        self.persisted_metadata = None
        self.state_separator = False
        self.loaded_policy_hash = None
        self.wires = {}
        self.queue = deque()
        self.audit_records = deque(maxlen=200)
        self.closed = False
        self.expired = False
        self.state_failed = False
        self._load()

    def _wire_id(self):
        return self.generation + ":" + uuid.uuid4().hex

    # Session hooks: the static policy owns its sessions. A managed subclass
    # (managed.py) substitutes controller-registered clients without changing
    # the request, journal, or executor path below.
    def _fresh_owned(self):
        return {sid: set() for sid in self.policy.sessions}

    def _session_known(self, sid):
        return sid in self.policy.sessions

    def _session_matches(self, sid, token):
        expected = self.policy.sessions.get(sid)
        return expected is not None and hmac.compare_digest(token, expected)

    def _capability_tokens(self):
        return (self.policy.extension_token, *self.policy.sessions.values())

    def _session_budget(self):
        return max(1, MAX_REQUESTS // len(self.policy.sessions))

    def _redact(self, value):
        for token in self._capability_tokens():
            value = value.replace(token, "[redacted]")
        value = re.sub(r"\b(?:api[_-]?key|secret|token|password|passwd|pwd|authorization|credential)\b\s*[:=]\s*['\"]?[^\s'\"]+", "[redacted]", value, flags=re.I)
        value = re.sub(r"\bbearer\s+[a-z0-9\-._~+/]+=*", "[redacted]", value, flags=re.I)
        value = re.sub(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}", "[redacted]", value)
        return re.sub(r"\b(?:[A-Fa-f0-9]{32,}|[A-Za-z0-9+/]{40,}={0,2})\b", "[redacted]", value)

    def _audit(self, event, status, request=None, count=0):
        record = {"event": event, "status": status, "op": request.action if request else "", "count": count}
        if request:
            record["request_id"] = hashlib.sha256(request.rid.encode()).hexdigest()[:16]
            record["session_id"] = hashlib.sha256(request.sid.encode()).hexdigest()[:16]
            record["tab_id"] = request.tab or 0
        self.audit_records.append(record)
        if self.audit_path:
            try:
                self.audit_path.parent.mkdir(parents=True, exist_ok=True)
                with self.audit_path.open("ab") as stream:
                    stream.write(json_bytes(record) + b"\n")
            except OSError:
                # Audit failure fails closed, without printing a path or body.
                self.state_failed = True
                self.stop, self.technical_block = True, True

    def _response(self, req, status, reason=None, data=None, tab=None):
        result = {"request_id": req.rid, "session_id": req.sid, "profile_id": self.policy.profile_id, "status": status,
                  "data": data if data is not None else {"reason": reason or "rejected"}}
        if tab is not None:
            result["tab_id"] = tab
        return result

    def _finish(self, req, result):
        req.result = result
        self.wires.pop(req.wire_id, None)
        if not req.dispatched:
            try:
                self.queue.remove(req)
            except ValueError:
                pass
        req.args = {}  # Do not retain URLs/form text after completion.
        if req.action in MUTATIONS and req.dispatched:
            self.dirty_mutations[(req.sid, req.rid)] = req
        else:
            # No executor has seen an undispatched mutation. Its denial cannot
            # require a lifetime tombstone or consume another session's budget.
            self.completed_observations.append(req)
            while len(self.completed_observations) > MAX_OBSERVATIONS:
                self._evict_observation()
        req.event.set()
        self._audit("result", result["status"], req)
        self.condition.notify_all()

    def _evict_observation(self, req=None):
        if req is None:
            req = self.completed_observations.popleft()
        else:
            self.completed_observations.remove(req)
        key = (req.sid, req.rid)
        if self.requests.get(key) is req:
            self.requests.pop(key)

    def _reserve_request(self, sid):
        limit = self._session_budget()
        count = sum(key[0] == sid for key in self.requests)
        while count >= limit:
            cached = next((req for req in self.completed_observations if req.sid == sid), None)
            if cached is None:
                raise Reject("bound", 429)
            self._evict_observation(cached)
            count -= 1
        while len(self.requests) >= MAX_REQUESTS and self.completed_observations:
            self._evict_observation()
        if len(self.requests) >= MAX_REQUESTS:
            raise Reject("bound", 429)

    def _login_attempted(self, tab, current):
        # Durable one-shot tombstone: a pending saved-login, or a dispatched
        # one which the executor did not deny before native input (ok or
        # unknown, journaled), blocks every other request ID on this tab, also
        # after a broker, worker or browser restart. There is no reset tool.
        return any(req is not current and req.action == LOGIN_ACTION and req.tab == tab and
                   (req.result is None or (req.dispatched and req.result["status"] != "denied"))
                   for req in self.requests.values())

    def _sweep_deadlines(self):
        # Caller/handler disappearance must not keep a tab lock or table slot.
        # Dispatched mutations still retain a durable unknown, never a replay.
        now = time.monotonic()
        expired = [req for req in self.wires.values() if req.result is None and req.deadline <= now]
        for req in expired:
            self._finish(req, self._response(req, "unknown", "timeout", tab=req.tab))
        if expired:
            self._persist()

    def _cancel(self, reason, stop=True):
        self.epoch += 1
        if stop:
            self.stop = True
        self.technical_block = True
        self.refs.clear()
        for req in list(self.requests.values()):
            if req.result is None:
                self._finish(req, self._response(req, "unknown" if req.dispatched else "denied", reason, tab=req.tab))
        self.queue.clear()

    def _expire(self):
        if self.expired or time.time() >= self.policy.expires_at:
            if not self.expired:
                self.expired = True
                self._cancel("expired")
                self._persist()
            return True
        return False

    def _state_metadata(self):
        return {"schema": 2, "profile_id": self.policy.profile_id, "policy_hash": self.policy_hash, "stop": self.stop,
                "authorized": self.authorized, "manual_tab": self.manual_tab, "epoch": self.epoch,
                "owned": {sid: sorted(ids) for sid, ids in self.owned.items()}}

    @staticmethod
    def _mutation_record(req):
        result = req.result
        record = {"sid": req.sid, "rid": req.rid, "action": req.action, "tab": req.tab, "fingerprint": req.fingerprint,
                  "status": result["status"] if result else "pending", "dispatched": req.dispatched,
                  "result_tab": result.get("tab_id") if result else None,
                  "reason": result.get("data", {}).get("reason", "broker_restart") if result else "broker_restart"}
        if result and result["status"] == "ok":
            record["mutation_data"] = result["data"]  # Only booleans/typed length, no form text.
        return record

    def _persist(self):
        if not self.state_path or self.state_failed:
            self.dirty_mutations.clear()
            return
        metadata = self._state_metadata()
        if not self.dirty_mutations and metadata == self.persisted_metadata:
            return
        # Append only changed mutation tombstones/latches, never lifetime history
        # or observations. Flush before dispatch; a torn journal fails closed.
        records = [self._mutation_record(req) for req in self.dirty_mutations.values()]
        identity = {key: metadata[key] for key in ("schema", "profile_id", "policy_hash")}
        changes = dict(identity)
        for key, value in metadata.items():
            if self.persisted_metadata is None or value != self.persisted_metadata[key]:
                if key == "owned" and self.persisted_metadata is not None:
                    value = {sid: ids for sid, ids in value.items() if ids != self.persisted_metadata[key].get(sid)}
                changes[key] = value
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            with self.state_path.open("ab") as stream:
                if self.state_separator:
                    stream.write(b"\n")  # Preserve a legacy schema-1 snapshot.
                for index, record in enumerate(records or [None]):
                    stream.write(json_bytes({**(changes if index == 0 else identity), "requests": [record] if record else []}) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            self.state_separator = False
            self.persisted_metadata = metadata
            self.dirty_mutations.clear()
        except (OSError, ValueError):
            self.state_failed = True
            self._cancel("state_io")

    def _load(self):
        if not self.state_path or not self.state_path.exists():
            return
        try:
            total, first = 0, True
            with self.state_path.open("rb") as stream:
                while True:
                    # The first record may be the retained legacy snapshot.
                    bound = 8 * 1024 * 1024 if first else MAX_BODY
                    raw = stream.readline(bound + 1)
                    if not raw:
                        break
                    total += len(raw)
                    if len(raw) > bound or total > MAX_STATE_BYTES:
                        raise Reject("state")
                    saved = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
                    self._restore_state(saved, first)
                    self.state_separator = not raw.endswith(b"\n")
                    first = False
            if first:
                raise Reject("state")
            self.epoch += 1
        except (OSError, ValueError, KeyError, TypeError, Reject):
            self.stop, self.authorized, self.technical_block = True, False, True
            self.owned = self._fresh_owned()
            self.requests.clear()
            self.state_failed = True

    def _restore_state(self, saved, first):
        if not isinstance(saved, dict) or saved.get("schema") not in (1, 2) or (saved["schema"] == 1 and not first) or saved.get("profile_id") != self.policy.profile_id:
            raise Reject("state")
        # STOP survives policy rotation. Tombstones belong to the exact policy.
        if saved.get("policy_hash") != self.policy_hash:
            self.stop, self.authorized, self.manual_tab = True, False, 0
            self.owned = self._fresh_owned()
            self.requests.clear()
            self.loaded_policy_hash = None
            return
        if self.loaded_policy_hash is None and not {"stop", "authorized", "manual_tab", "epoch", "owned"} <= saved.keys():
            raise Reject("state")
        self.loaded_policy_hash = self.policy_hash
        if "stop" in saved:
            self.stop = saved["stop"] is not False
        if "authorized" in saved:
            self.authorized = saved["authorized"] is True
        if "manual_tab" in saved:
            self.manual_tab = tab_id(saved["manual_tab"], zero=True)
        if "epoch" in saved:
            self.epoch = int(saved["epoch"])
        saved_owned = saved.get("owned", {})
        if not isinstance(saved_owned, dict):
            raise Reject("state")
        owned = {sid: set(ids) for sid, ids in self.owned.items()}
        for sid, ids in saved_owned.items():
            if sid not in owned or not isinstance(ids, list) or len(ids) > MAX_TABS:
                raise Reject("state")
            owned[sid] = {tab_id(item) for item in ids if item != self.manual_tab}
        seen = set()
        for ids in owned.values():
            if seen & ids:
                raise Reject("state")
            seen.update(ids)
        self.owned = owned
        records = saved.get("requests", [])
        if not isinstance(records, list) or len(records) > (MAX_REQUESTS if saved["schema"] == 1 else 1):
            raise Reject("state")
        for record in records:
            sid, rid = ident(record["sid"]), ident(record["rid"])
            if sid not in self.owned or record["action"] not in ACTIONS:
                raise Reject("state")
            if record["action"] not in MUTATIONS:
                if saved["schema"] != 1:
                    raise Reject("state")
                continue  # Restored observations cannot supply current refs/state.
            key = (sid, rid)
            existing = self.requests.get(key)
            if existing and (existing.fingerprint != record["fingerprint"] or existing.action != record["action"]):
                raise Reject("state")
            req = Request(sid, rid, self._wire_id(), record["action"], {}, record["tab"], record["fingerprint"], self.epoch, 0)
            status = record["status"]
            dispatched = record.get("dispatched", status in ("ok", "unknown"))
            if type(dispatched) is not bool or (status == "ok" and not dispatched):
                raise Reject("state")
            if status == "pending":
                status = "unknown" if dispatched else "denied"
            if status not in ("ok", "denied", "unknown"):
                raise Reject("state")
            if not dispatched:
                # Older journals retained zero-effect denials/queued requests.
                # Restore only IDs which could have reached the executor.
                continue
            req.dispatched = True
            reason = record.get("reason")
            reason = reason if reason in REASONS else "broker_restart"
            req.result = self._response(req, status, reason, tab=record.get("result_tab"))
            if status == "ok":
                payload = record.get("mutation_data", {})
                flag = SUCCESS_FLAGS[req.action]
                exact(payload, (flag, "length") if req.action == "chrome_type" else (flag, "authentication") if req.action == LOGIN_ACTION else (flag,))
                if payload[flag] is not True or (req.action == "chrome_type" and (type(payload["length"]) is not int or not 1 <= payload["length"] <= 1000)):
                    raise Reject("state")
                if req.action == LOGIN_ACTION and payload["authentication"] != "unverified":
                    raise Reject("state")
                req.result["data"] = payload
            req.event.set()
            self.requests[key] = req
            if len(self.requests) > MAX_REQUESTS:
                raise Reject("state")

    def authenticate(self, headers, extension=False):
        authorization = headers.get("Authorization", "")
        if len(headers.get_all("Authorization", [])) != 1 or not authorization.startswith("Bearer ") or len(authorization) > 270:
            raise Reject("authorization", 401)
        token = authorization[7:]
        if extension:
            if len(headers.get_all("X-Profile-Id", [])) != 1 or headers.get("X-Profile-Id") != self.policy.profile_id or not hmac.compare_digest(token, self.policy.extension_token):
                raise Reject("authorization", 401)
            return None
        sid = headers.get("X-Session-Id", "")
        if len(headers.get_all("X-Session-Id", [])) != 1 or not self._session_matches(sid, token):
            raise Reject("authorization", 401)
        return sid

    def _validate_operation(self, value):
        exact(value, ("request_id", "action", "args"), ("tab_id",))
        rid = ident(value["request_id"])
        if any(token in rid for token in self._capability_tokens()):
            raise Reject("identity")
        action = value["action"]
        if not isinstance(action, str) or action not in ACTIONS:
            raise Reject("action")
        args = value["args"]
        tab = value.get("tab_id")
        if action in ("chrome_attachment", "chrome_open"):
            if "tab_id" in value:
                raise Reject("ownership")
        else:
            tab = tab_id(tab)
        if action in ("chrome_attachment", "chrome_read", "chrome_close", LOGIN_ACTION):
            exact(args)
        elif action == "chrome_open":
            exact(args, ("url",))
            self.policy.permits_url(args["url"], saved_login_page=True)
        elif action in ("chrome_click", "chrome_type"):
            exact(args, ("ref", "text") if action == "chrome_type" else ("ref",))
            if not isinstance(args["ref"], str) or not re.fullmatch(r"ref_[a-f0-9]{24}", args["ref"]):
                raise Reject("stale_ref")
            if action == "chrome_type" and (not isinstance(args["text"], str) or not 1 <= utf16_length(args["text"]) <= 1000):
                raise Reject("bound")
        else:
            exact(args, (), ("role", "name", "text"))
            if not args or any(not isinstance(item, str) or not 1 <= utf16_length(item) <= 200 for item in args.values()):
                raise Reject("bound")
        return rid, action, args, tab

    def submit(self, sid, value):
        if not self._session_known(sid):
            raise Reject("authorization", 401)
        rid, action, args, tab = self._validate_operation(value)
        digest = hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        key = (sid, rid)
        with self.condition:
            self._expire()
            self._sweep_deadlines()
            if not self._session_known(sid):
                raise Reject("authorization", 401)  # Revoked while waiting for the lock.
            existing = self.requests.get(key)
            if existing:
                if existing.fingerprint != digest:
                    raise Reject("request_id_conflict", 409)
                req = existing
            else:
                self._reserve_request(sid)
                req = Request(sid, rid, self._wire_id(), action, copy.deepcopy(args), tab, digest, self.epoch, time.monotonic() + self.request_timeout)
                self.requests[key] = req
                reason = None
                if action not in self.policy.allowed_actions:
                    reason = "action"
                elif self.closed or self.state_failed:
                    reason = "state_io"
                elif self.expired:
                    reason = "expired"
                elif action != "chrome_attachment" and (self.stop or not self.authorized):
                    reason = "stopped"
                elif action != "chrome_attachment" and self.technical_block:
                    reason = "technical_block"
                elif tab is not None and (tab not in self.owned[sid] or tab == self.manual_tab):
                    reason = "ownership"
                elif action == "chrome_open" and len(self.owned[sid]) >= MAX_TABS:
                    reason = "bound"
                elif action == LOGIN_ACTION and self._login_attempted(tab, req):
                    reason = "login_attempted"
                if reason is None and action in ("chrome_click", "chrome_type"):
                    ref = self.refs.get((sid, tab), {}).get(args["ref"])
                    if not ref or ref[1] < time.monotonic():
                        reason = "stale_ref"
                    else:
                        req.args["ref"] = ref[0]
                        self.refs.pop((sid, tab), None)
                if reason:
                    self._finish(req, self._response(req, "denied", reason, tab=tab))
                else:
                    self.queue.append(req)
                    self.wires[req.wire_id] = req
                    self._audit("queued", "pending", req)
                    self.condition.notify_all()
                self._persist()
            if req.result is not None:
                return copy.deepcopy(req.result)
        req.event.wait(max(0, min(req.deadline - time.monotonic(), self.policy.expires_at - time.time())))
        with self.condition:
            self._expire()
            if req.result is None:
                self._finish(req, self._response(req, "unknown", "timeout", tab=req.tab))
                self._persist()
            return copy.deepcopy(req.result)

    def poll(self):
        deadline = time.monotonic() + self.poll_timeout
        with self.condition:
            while True:
                if self._expire() or self.closed or self.state_failed:
                    return None
                self._sweep_deadlines()
                if self.state_failed:
                    return None
                active_tabs = {(req.sid, req.tab) for req in self.wires.values() if req.dispatched and req.tab is not None}
                for _ in range(len(self.queue)):
                    req = self.queue.popleft()
                    if req.result is not None:
                        continue
                    if time.monotonic() >= req.deadline:
                        self._finish(req, self._response(req, "unknown", "timeout", tab=req.tab))
                        self._persist()
                        continue
                    if req.tab is not None and (req.sid, req.tab) in active_tabs:
                        self.queue.append(req)
                        continue
                    if req.action != "chrome_attachment" and (self.stop or self.technical_block or not self.authorized):
                        self._finish(req, self._response(req, "denied", "stopped" if self.stop else "technical_block", tab=req.tab))
                        continue
                    req.dispatched = True
                    if req.action in MUTATIONS:
                        self.dirty_mutations[(req.sid, req.rid)] = req
                    self._persist()  # Durable ID before handing any mutation to executor.
                    if self.state_failed:
                        return None
                    result = {"request_id": req.wire_id, "session_id": req.sid, "profile_id": self.policy.profile_id, "action": req.action, "args": copy.deepcopy(req.args)}
                    if req.tab is not None:
                        result["tab_id"] = req.tab
                    self._audit("dispatched", "pending", req)
                    return result
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                next_deadline = min((req.deadline for req in self.wires.values()), default=deadline)
                self.condition.wait(min(remaining, max(0.001, next_deadline - time.monotonic()),
                                        max(0.01, self.policy.expires_at - time.time())))

    def extension_event(self, value):
        exact(value, ("type", "profile_id", "session_id", "tab_id", "reason"))
        if value["profile_id"] != self.policy.profile_id or value["session_id"] != "":
            raise Reject("identity")
        tid = tab_id(value["tab_id"], zero=True)
        kind, reason = value["type"], value["reason"]
        if not isinstance(kind, str) or not isinstance(reason, str):
            raise Reject("event")
        with self.condition:
            if self._expire():
                raise Reject("expired", 403)
            if kind == "attach" and reason == "owner_attach" and tid:
                if any(tid in ids for ids in self.owned.values()) or self.state_failed:
                    raise Reject("ownership", 409)
                # Only this explicit owner event may release persisted STOP.
                self._cancel("cancelled")
                self.stop, self.authorized, self.technical_block, self.manual_tab = False, True, False, tid
            elif kind == "stop" and reason in (
                "owner_stop", "canceled_by_user", "expired", "origin", "manual_closed", "browser_restart"
            ):
                self._cancel("stopped")
            elif kind == "disconnect" and reason == "technical_disconnect":
                self._cancel("technical_block", stop=False)
            else:
                raise Reject("event")
            self._audit("extension_" + kind, "ok")
            self._persist()
            self.condition.notify_all()

    def _safe_diagnostics(self, value):
        # Optional bounded reader metadata: counts/enums only, never page content.
        exact(value, DIAGNOSTIC_KEYS)
        status, ready = value["reader_status"], value["ready_state"]
        if not isinstance(status, str) or status not in READER_STATUSES or not isinstance(ready, str) or ready not in READY_STATES:
            raise Reject("result")
        for key in DIAGNOSTIC_DOCUMENT_KEYS:
            if type(value[key]) is not int or not 0 <= value[key] <= MAX_DIAGNOSTIC_DOCUMENTS:
                raise Reject("result")
        for key in DIAGNOSTIC_CHARS_KEYS:
            if type(value[key]) is not int or not 0 <= value[key] <= MAX_DIAGNOSTIC_CHARS:
                raise Reject("result")
        return {key: value[key] for key in DIAGNOSTIC_KEYS}

    def _safe_ref_url(self, value):
        # Optional navigation metadata: exact assigned origin, http(s), no
        # credentials or secret-looking values. A risky URL is omitted
        # (None: entry kept, opaque ref stays clickable, read stays ok),
        # never rewritten and never a whole-read failure. Unparseable and
        # non-HTTP values (javascript:, data:, file:, chrome:, non-string)
        # fail inside _url and omit through the same path. Structural ref
        # faults (id, role, name, counts, bounds) still reject in _safe_data.
        try:
            parsed, origin = _url(value)
        except Reject:
            return None
        if origin not in self.policy.allowed_origins or credential_url(parsed):
            return None
        # Inspect the same bounded decoded representation as the JS reader.
        # Return only the exact observed URL, never its decoded/rebuilt form.
        try:
            decoded = value
            for _ in range(3):
                next_value = unquote(decoded, errors="strict")
                if next_value == decoded:
                    break
                decoded = next_value
            if re.search(r"%[a-f0-9]{2}|\s", decoded, re.I):
                return None
            decoded_url, decoded_origin = _url(decoded)
            if decoded_origin != origin or credential_url(decoded_url):
                return None
        except (Reject, ValueError, UnicodeError):
            return None
        if (REF_URL_SENSITIVE_TERM_RE.search(decoded) or REF_URL_BEARER_RE.search(decoded)
                or REF_URL_JWT_RE.search(decoded) or REF_URL_SECRET_RE.search(decoded)
                or REF_URL_SECRET_PREFIX_RE.search(decoded)
                or self._redact(decoded) != decoded):
            return None
        if REF_URL_CREDENTIAL_PARAM_RE.search(parsed.query) or REF_URL_CREDENTIAL_PARAM_RE.search(parsed.fragment):
            return None
        try:
            _decoded_ref_path = parsed.path
            for _ in range(3):
                _decoded_ref_path = unquote(_decoded_ref_path)
        except (ValueError, UnicodeError):
            return None
        if ";" in parsed.path or ";" in _decoded_ref_path:
            return None
        if _has_credential_key(parsed.query) or _has_credential_key(parsed.fragment):
            return None
        if REF_URL_BEARER_RE.search(value) or REF_URL_JWT_RE.search(value) or REF_URL_SECRET_RE.search(value):
            return None
        if self._redact(value) != value:
            return None
        return value

    def _safe_data(self, req, status, data):
        if status != "ok":
            exact(data, ("reason",), ("truncated",))
            reason = data["reason"] if isinstance(data["reason"], str) and data["reason"] in REASONS else "rejected"
            if "truncated" in data and type(data["truncated"]) is not bool:
                raise Reject("result")
            return {"reason": reason}
        if req.action == "chrome_read":
            exact(data, ("excerpt", "refs", "truncated"), ("diagnostics",))
            if not isinstance(data["excerpt"], str) or type(data["truncated"]) is not bool or not isinstance(data["refs"], list) or len(data["refs"]) > 40 or len(json_bytes(data)) > MAX_READ:
                raise Reject("bound")
            diagnostics = self._safe_diagnostics(data["diagnostics"]) if "diagnostics" in data else None
            clean, refs, seen = [], {}, set()
            for item in data["refs"]:
                exact(item, ("ref", "role", "name"), ("url",))
                if not isinstance(item["ref"], str) or len(item["ref"]) > 32 or not REF_RE.fullmatch(item["ref"]) or item["ref"] in seen:
                    raise Reject("result")
                seen.add(item["ref"])
                if not isinstance(item["role"], str) or not isinstance(item["name"], str) or len(item["role"]) > 40 or utf16_length(item["name"]) > 120:
                    raise Reject("result")
                name = self._redact(item["name"])
                if credential_label(name) or name != item["name"]:
                    continue
                url = None
                if "url" in item:
                    # URL metadata is optional. Omit a rejected URL instead of
                    # turning useful page text into a transport disconnect.
                    try:
                        url = self._safe_ref_url(item["url"])
                    except Reject:
                        pass
                opaque = "ref_" + secrets.token_hex(12)
                refs[opaque] = (item["ref"], time.monotonic() + 60)
                entry = {"ref": opaque, "role": self._redact(item["role"]), "name": name}
                if url is not None:
                    entry["url"] = url
                clean.append(entry)
            fitted = {"excerpt": self._redact(data["excerpt"]), "refs": clean, "truncated": data["truncated"]}
            if diagnostics is not None:
                fitted["diagnostics"] = diagnostics
            minimum_excerpt = min(len(fitted["excerpt"]), 256)
            while len(json_bytes(fitted)) > MAX_READ:
                fitted["truncated"] = True
                if diagnostics is not None and diagnostics["reader_status"] in ("ok", "empty"):
                    diagnostics["reader_status"] = "partial"
                if len(fitted["excerpt"]) > minimum_excerpt:
                    fitted["excerpt"] = fitted["excerpt"][:max(minimum_excerpt, len(fitted["excerpt"]) * 3 // 4 - 1)]
                elif fitted["refs"]:
                    removed = fitted["refs"].pop()
                    refs.pop(removed["ref"], None)
                else:
                    fitted["excerpt"] = fitted["excerpt"][:-1]
            return fitted, refs
        if req.action == "chrome_attachment":
            exact(data, ("stopped", "authorized", "technical_block", "owner_attached", "manual_tab_id", "owned_tab_ids", "profile_id"))
            if any(type(data[key]) is not bool for key in ("stopped", "authorized", "technical_block", "owner_attached")) or data["profile_id"] != self.policy.profile_id:
                raise Reject("identity")
            manual = tab_id(data["manual_tab_id"], zero=True)
            ids = data["owned_tab_ids"]
            if not isinstance(ids, list) or len(ids) > MAX_TABS or len(ids) != len(set(ids)):
                raise Reject("ownership")
            owned = {tab_id(item) for item in ids}
            if manual in owned or any(owned & tabs for sid, tabs in self.owned.items() if sid != req.sid):
                raise Reject("ownership")
            return {**data, "owned_tab_ids": sorted(owned)}
        if req.action == "chrome_wait":
            exact(data, ("matched",), ("reason",))
            if type(data["matched"]) is not bool or ("reason" in data and data["reason"] != "not_observed"):
                raise Reject("result")
            return {"matched": data["matched"], **({"reason": "not_observed"} if "reason" in data else {})}
        key = SUCCESS_FLAGS[req.action]
        if req.action == LOGIN_ACTION:
            # Submission is not authentication; the page outcome stays unverified.
            exact(data, (key, "authentication"))
            if data[key] is not True or data["authentication"] != "unverified":
                raise Reject("result")
            return {key: True, "authentication": "unverified"}
        exact(data, (key, "length") if req.action == "chrome_type" else (key,))
        if data[key] is not True or (req.action == "chrome_type" and (type(data["length"]) is not int or data["length"] != utf16_length(req.args["text"]))):
            raise Reject("result")
        return data

    def extension_result(self, value):
        exact(value, ("request_id", "session_id", "profile_id", "status"), ("tab_id", "data"))
        wire = ident(value["request_id"])
        with self.condition:
            self._expire()
            self._sweep_deadlines()
            req = self.wires.get(wire)
            if not req or not req.dispatched or req.result is not None or req.epoch != self.epoch:
                raise Reject("unsolicited_result", 409)
            if value["session_id"] != req.sid or value["profile_id"] != self.policy.profile_id:
                raise Reject("identity", 409)
            status = value["status"]
            if status not in ("ok", "denied", "unknown"):
                raise Reject("result")
            tid = tab_id(value["tab_id"], zero=True) if "tab_id" in value else None
            if req.tab is not None and tid not in (None, 0, req.tab):
                raise Reject("ownership", 409)
            if status == "ok" and req.action not in ("chrome_attachment", "chrome_open") and tid != req.tab:
                raise Reject("ownership", 409)
            if req.action == "chrome_attachment" and tid not in (None, 0):
                raise Reject("ownership", 409)
            if req.action == "chrome_open" and status == "ok":
                if not tid or tid == self.manual_tab or any(tid in tabs for tabs in self.owned.values()) or len(self.owned[req.sid]) >= MAX_TABS:
                    raise Reject("ownership", 409)
            data = self._safe_data(req, status, value.get("data", {}))
            if self.stop and req.action != "chrome_attachment":
                raise Reject("stopped", 409)
            if status == "ok":
                if req.action == "chrome_attachment":
                    if data["stopped"]:
                        # Do not cancel this status observation, only the other work.
                        self.wires.pop(req.wire_id, None)
                        req.result = self._response(req, "ok", data={})
                        self._cancel("stopped")
                    elif not self.stop and self.authorized and data["authorized"] and not data["technical_block"] and data["manual_tab_id"] == self.manual_tab:
                        self.technical_block = False
                        self.owned[req.sid] = set(data["owned_tab_ids"])
                        self.refs = {key: refs for key, refs in self.refs.items() if key[0] != req.sid}
                    else:
                        self.technical_block = True
                    data = {"stopped": self.stop, "authorized": self.authorized, "technical_block": self.technical_block,
                            "owner_attached": self.authorized and not self.stop and not self.technical_block,
                            "profile_id": self.policy.profile_id, "owned_tab_ids": sorted(self.owned[req.sid])}
                elif req.action == "chrome_open":
                    self.owned[req.sid].add(tid)
                elif req.action == "chrome_close":
                    self.owned[req.sid].discard(req.tab)
                    self.refs.pop((req.sid, req.tab), None)
                elif req.action == "chrome_read":
                    data, refs = data
                    self.refs[(req.sid, req.tab)] = refs
            self._finish(req, self._response(req, status, data=data, tab=tid if tid else None))
            self._persist()

    def close(self):
        with self.condition:
            self.closed = True
            self._cancel("technical_block", stop=False)
            self._persist()
            self.condition.notify_all()


class Handler(BaseHTTPRequestHandler):
    server_version = "ChromeRelay/1"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass  # Base handler logs URLs and caller-controlled strings by default.

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def send_error(self, code, message=None, explain=None):
        self._send(code, {"error": "http"})

    def _send(self, status, value=None):
        self.close_connection = True
        data = b"" if value is None else json_bytes(value)
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            if data:
                self.wfile.write(data)
        except OSError:
            pass  # Windows can report ConnectionAbortedError on early close.

    def _guard(self):
        if len(self.headers.get_all("Host", [])) != 1 or self.headers.get("Host") != "127.0.0.1:" + str(self.server.server_address[1]):
            raise Reject("host", 403)
        if sum(len(key) + len(value) for key, value in self.headers.items()) > 8192:
            raise Reject("headers", 431)
        if self.path not in ("/mcp", "/extension/poll", "/extension/result", "/extension/event"):
            raise Reject("route", 404)
        extension = self.path.startswith("/extension/")
        origins = self.headers.get_all("Origin", [])
        if len(origins) > 1 or (origins and (not extension or origins[0] != "chrome-extension://" + self.server.broker.policy.extension_id)):
            raise Reject("origin", 403)
        if self.headers.get_all("Transfer-Encoding", []):
            raise Reject("transfer_encoding")
        return self.server.broker.authenticate(self.headers, extension)

    def _body(self):
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,7}", lengths[0]):
            raise Reject("content_length", 411)
        size = int(lengths[0])
        if size > MAX_BODY:
            raise Reject("bound", 413)
        types = self.headers.get_all("Content-Type", [])
        if len(types) != 1 or types[0].lower() not in ("application/json", "application/json; charset=utf-8"):
            raise Reject("content_type", 415)
        self.body_read_started = True
        body = self.rfile.read(size)
        if len(body) != size:
            raise Reject("incomplete")
        return parse_json(body)

    def _discard_rejected_body(self):
        # A rejected POST can arrive as headers followed by a separate send.
        # Closing with unread body bytes resets Windows TCP before the caller
        # receives the denial. Discard only bounded, unambiguous framing; never
        # parse/dispatch it or wait indefinitely for an unauthenticated caller.
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get_all("Transfer-Encoding", []) or len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,7}", lengths[0]):
            return
        size = int(lengths[0])
        if not 0 < size <= MAX_BODY:
            return
        previous_timeout = self.connection.gettimeout()
        try:
            deadline = time.monotonic() + 0.25
            remaining = size
            while remaining:
                timeout = deadline - time.monotonic()
                if timeout <= 0:
                    break
                self.connection.settimeout(timeout)
                chunk = self.rfile.read1(min(remaining, 8192))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            pass
        finally:
            self.connection.settimeout(previous_timeout)

    def do_POST(self):
        self.body_read_started = False
        try:
            sid = self._guard()
            if self.path == "/extension/poll":
                raise Reject("method", 405)
            value = self._body()
            broker = self.server.broker
            if self.path == "/mcp":
                result = broker.submit(sid, value)
            elif self.path == "/extension/result":
                broker.extension_result(value)
                result = {"accepted": True}
            else:
                broker.extension_event(value)
                result = {"accepted": True}
            self._send(200, result)
        except Reject as error:
            if not self.body_read_started:
                self._discard_rejected_body()
            self._send(error.status, {"error": error.reason})
        except (OSError, ValueError, TypeError, RecursionError):
            self._send(400, {"error": "request_failed"})

    def do_GET(self):
        try:
            self._guard()
            if self.path != "/extension/poll":
                raise Reject("method", 405)
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) > 1 or (lengths and lengths[0] != "0"):
                raise Reject("content_length")
            result = self.server.broker.poll()
            self._send(200 if result else 204, result)
        except Reject as error:
            self._send(error.status, {"error": error.reason})
        except (OSError, ValueError, TypeError):
            self._send(400, {"error": "request_failed"})

    def do_OPTIONS(self):
        self._send(405, {"error": "method"})


class RelayServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 32

    def handle_error(self, request, client_address):
        # Do not emit stack traces, paths, request data, or capability material.
        with self.broker.lock:
            self.broker._audit("connection_error", "unknown")


def make_server(broker, port=0):
    if not isinstance(broker, Broker) or type(port) is not int or not 0 <= port <= 65535:
        raise Reject("server")
    server = RelayServer(("127.0.0.1", port), Handler)
    server.broker = broker
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description="Scoped loopback Chrome relay; operator setup only")
    parser.add_argument("--policy", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--audit", required=True)
    parser.add_argument("--port", type=int, default=9333)
    args = parser.parse_args(argv)
    try:
        with Path(args.policy).open("rb") as stream:
            policy = Policy.from_dict(parse_json(stream.read(MAX_BODY + 1)))
        broker = Broker(policy, args.state, args.audit)
        server = make_server(broker, args.port)
        server.serve_forever(poll_interval=0.2)
    except (Reject, OSError, ValueError, TypeError):
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        if "server" in locals():
            server.server_close()
        if "broker" in locals():
            broker.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
