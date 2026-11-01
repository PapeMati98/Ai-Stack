#!/usr/bin/env python3
"""Grok Office Gateway

Narrow HTTPS gateway so the cloud Grok Bot 'Agent Office' can exchange messages
bidirectionally with Agent Office under a dedicated grok-bot principal.
Standard library only (Python 3.11+).
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import inspect
import json
from pathlib import Path
import re
import socket
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.parse


ALLOWED_CONFIG_KEYS = {
    "listen_host",
    "listen_port",
    "path_prefix",
    "office_runtime",
    "office_home",
    "agent_id",
    "transport_principal",
    "token_sha256_file",
    "channel",
    "targets",
    "allowed_kinds",
    "max_body_bytes",
    "max_read_limit",
    "rate_limit_requests",
    "rate_limit_window_s",
    "identity",
}

REQUIRED_CONFIG_KEYS = {
    "listen_host",
    "listen_port",
    "path_prefix",
    "office_runtime",
    "office_home",
    "agent_id",
    "token_sha256_file",
    "channel",
    "targets",
}

FORBIDDEN_AUTHORITY_FIELDS = {
    "from",
    "sender",
    "agent_id",
    "principal",
    "transport_principal",
    "owner",
    "token",
    "secret",
    "authority",
    "credential",
}


class GatewayConfig:
    """Validated gateway configuration."""

    def __init__(self, raw: dict):
        if not isinstance(raw, dict):
            raise ValueError("Config must be a JSON object")

        unknown = set(raw.keys()) - ALLOWED_CONFIG_KEYS
        if unknown:
            raise ValueError(f"Unknown config key(s): {sorted(unknown)}")

        missing = REQUIRED_CONFIG_KEYS - set(raw.keys())
        if missing:
            raise ValueError(f"Missing required config key(s): {sorted(missing)}")

        self.listen_host = raw["listen_host"]
        if self.listen_host not in ("127.0.0.1", "::1"):
            raise ValueError(f"listen_host must be '127.0.0.1' or '::1', got {self.listen_host!r}")

        self.listen_port = int(raw["listen_port"])
        if not (0 <= self.listen_port <= 65535):
            raise ValueError(f"listen_port out of range (0..65535): {self.listen_port}")

        self.path_prefix = raw["path_prefix"]
        if (
            not isinstance(self.path_prefix, str)
            or not self.path_prefix.startswith("/")
            or self.path_prefix.endswith("/")
            or len(self.path_prefix) <= 1
        ):
            raise ValueError(f"path_prefix must start with '/' and have no trailing '/', got {self.path_prefix!r}")

        self.office_runtime = raw["office_runtime"]
        if not isinstance(self.office_runtime, str) or not Path(self.office_runtime).is_absolute():
            raise ValueError(f"office_runtime must be an absolute path: {self.office_runtime!r}")

        self.office_home = raw["office_home"]
        if not isinstance(self.office_home, str) or not Path(self.office_home).is_absolute():
            raise ValueError(f"office_home must be an absolute path: {self.office_home!r}")

        self.agent_id = raw["agent_id"]
        if not isinstance(self.agent_id, str) or not self.agent_id.strip():
            raise ValueError("agent_id must be a non-empty string")

        self.transport_principal = raw.get("transport_principal", "grok-bot")
        if not isinstance(self.transport_principal, str) or not self.transport_principal.strip():
            raise ValueError("transport_principal must be a non-empty string")

        self.token_sha256_file = raw["token_sha256_file"]
        if not isinstance(self.token_sha256_file, str) or not Path(self.token_sha256_file).is_absolute():
            raise ValueError(f"token_sha256_file must be an absolute path: {self.token_sha256_file!r}")

        self.channel = raw["channel"]
        if not isinstance(self.channel, str) or not self.channel.strip():
            raise ValueError("channel must be a non-empty string")

        self.targets = raw["targets"]
        if not isinstance(self.targets, dict):
            raise ValueError("targets must be a JSON object mapping alias to agent_id")
        for alias, target_id in self.targets.items():
            if not isinstance(alias, str) or not alias.strip():
                raise ValueError("targets alias must be a non-empty string")
            if not isinstance(target_id, str) or not target_id.strip():
                raise ValueError(f"target_id for alias {alias!r} must be a non-empty string")

        self.allowed_kinds = raw.get("allowed_kinds", ["note", "question"])
        if not isinstance(self.allowed_kinds, list):
            raise ValueError("allowed_kinds must be a list")
        valid_kinds = {"note", "question", "handoff"}
        if not self.allowed_kinds or not set(self.allowed_kinds).issubset(valid_kinds):
            raise ValueError(f"allowed_kinds must be a non-empty subset of {sorted(valid_kinds)}")

        self.max_body_bytes = int(raw.get("max_body_bytes", 8192))
        if not (1 <= self.max_body_bytes <= 16384):
            raise ValueError(f"max_body_bytes must be between 1 and 16384, got {self.max_body_bytes}")

        self.max_read_limit = int(raw.get("max_read_limit", 20))
        if self.max_read_limit < 1:
            raise ValueError("max_read_limit must be >= 1")

        self.rate_limit_requests = int(raw.get("rate_limit_requests", 120))
        if self.rate_limit_requests < 1:
            raise ValueError("rate_limit_requests must be >= 1")

        self.rate_limit_window_s = float(raw.get("rate_limit_window_s", 600.0))
        if self.rate_limit_window_s <= 0:
            raise ValueError("rate_limit_window_s must be > 0")

        self.identity = raw.get("identity")
        if self.identity is not None:
            if not isinstance(self.identity, dict) or set(self.identity) != {"harness", "host", "session_id"}:
                raise ValueError("identity must be an object with exactly harness, host and session_id")
            for key, value in self.identity.items():
                if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", value):
                    raise ValueError(f"identity.{key} must be a lowercase identifier")


class TokenValidator:
    """Validates presented Bearer tokens against a SHA-256 hash file.
    Re-reads the hash file at most every 30 seconds to support rotation.
    """

    def __init__(self, token_sha256_file: str, check_interval_s: float = 30.0):
        self.file_path = Path(token_sha256_file)
        self.check_interval_s = check_interval_s
        self._cached_hash: str | None = None
        self._last_checked: float = 0.0
        self._lock = threading.Lock()

    def invalidate(self) -> None:
        with self._lock:
            self._cached_hash = None
            self._last_checked = 0.0

    def _get_stored_hash(self) -> str | None:
        now = time.monotonic()
        with self._lock:
            if self._cached_hash is not None and (now - self._last_checked < self.check_interval_s):
                return self._cached_hash
            try:
                content = self.file_path.read_text(encoding="utf-8").strip().lower()
                if len(content) == 64 and all(c in "0123456789abcdef" for c in content):
                    self._cached_hash = content
                    self._last_checked = now
                    return self._cached_hash
                self._cached_hash = None
                self._last_checked = now
                return None
            except Exception:
                self._cached_hash = None
                self._last_checked = now
                return None

    def verify(self, presented_token: str) -> bool:
        if not isinstance(presented_token, str) or not presented_token:
            return False
        stored_hash = self._get_stored_hash()
        if not stored_hash:
            return False
        presented_hash = hashlib.sha256(presented_token.encode("utf-8")).hexdigest().lower()
        return hmac.compare_digest(presented_hash, stored_hash)


class SlidingWindowRateLimiter:
    """Sliding-window request rate limiter."""

    def __init__(self, max_requests: int, window_s: float):
        self.max_requests = max_requests
        self.window_s = window_s
        self.timestamps: list[float] = []
        self._lock = threading.Lock()

    def allow(self) -> bool:
        now = time.monotonic()
        with self._lock:
            cutoff = now - self.window_s
            self.timestamps = [ts for ts in self.timestamps if ts > cutoff]
            if len(self.timestamps) >= self.max_requests:
                return False
            self.timestamps.append(now)
            return True


class OfficeBackend:
    """Adapter for Office storage operations."""

    def __init__(self, office_instance, channel: str | None = None):
        self._office = office_instance
        self.channel = channel

    @classmethod
    def from_config(cls, cfg: GatewayConfig) -> OfficeBackend:
        if cfg.office_runtime and cfg.office_runtime not in sys.path:
            sys.path.insert(0, cfg.office_runtime)
        from coordfs.office import Office
        if cfg.identity is not None:
            # Host-qualified identity: Office refuses presence for an explicit
            # bare agent id under a transport principal (invalid_identity).
            office = Office(
                harness=cfg.identity["harness"],
                host=cfg.identity["host"],
                session_id=cfg.identity["session_id"],
                home=Path(cfg.office_home),
                transport_principal=cfg.transport_principal,
            )
            if office.agent_id != cfg.agent_id:
                raise ValueError(
                    f"identity resolves to {office.agent_id!r}, expected agent_id {cfg.agent_id!r}"
                )
        else:
            office = Office(
                agent_id=cfg.agent_id,
                home=Path(cfg.office_home),
                transport_principal=cfg.transport_principal,
            )
        backend = cls(office, channel=cfg.channel)
        if cfg.identity is not None:
            try:
                backend.presence(ttl_s=900, status="grok-bot gateway")
            except Exception:
                pass
        return backend

    def send(self, to=None, body="", kind="note", reply_to=None, channel=None, dedupe_key=None, channel_scope=None):
        scope = channel_scope if channel_scope is not None else ([self.channel] if self.channel else None)
        return self._office.send(
            to,
            body,
            kind,
            reply_to,
            channel=channel,
            dedupe_key=dedupe_key,
            channel_scope=scope,
            new_recipient=(to is not None),
        )

    def read(self, limit=20, peek=False, channel=None, max_body_bytes=8192, max_response_bytes=60000):
        read_kwargs = {
            "limit": limit,
            "peek": peek,
            "max_body_bytes": max_body_bytes,
        }
        if channel is not None:
            read_kwargs["channel"] = channel
        sig = inspect.signature(self._office.read).parameters
        if "max_response_bytes" in sig:
            read_kwargs["max_response_bytes"] = max_response_bytes
        elif "max_total_bytes" in sig:
            read_kwargs["max_total_bytes"] = max_response_bytes
        else:
            read_kwargs["max_response_bytes"] = max_response_bytes
        return self._office.read(**read_kwargs)

    def ack(self, message_id, note=None, channel_scope=None):
        scope = channel_scope if channel_scope is not None else ([self.channel] if self.channel else None)
        return self._office.ack(message_id, note, channel_scope=scope)

    def presence(self, ttl_s=900, status="grok-bot gateway"):
        return self._office.presence(ttl_s=ttl_s, status=status)


def _check_duplicate_keys(ordered_pairs):
    res = {}
    for k, v in ordered_pairs:
        if k in res:
            raise ValueError(f"duplicate_key:{k}")
        res[k] = v
    return res


def _has_authority_field(obj) -> bool:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in FORBIDDEN_AUTHORITY_FIELDS:
                return True
            if _has_authority_field(v):
                return True
    elif isinstance(obj, list):
        for item in obj:
            if _has_authority_field(item):
                return True
    return False


def _handle_office_exception(exc: Exception) -> tuple[int, dict]:
    exc_class_name = exc.__class__.__name__
    has_code = hasattr(exc, "code") and getattr(exc, "code") is not None
    if exc_class_name in ("OfficeReject", "CoordError") or has_code:
        code = getattr(exc, "code", None) or "office_reject"
        code_str = str(code)
        if code_str == "dedupe_conflict":
            return 409, {"status": "error", "code": "dedupe_conflict"}
        return 400, {"status": "error", "code": code_str}
    return 502, {"status": "error", "code": "office_error"}


class GrokOfficeHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the Grok Office Gateway."""

    server: GrokOfficeHTTPServer
    # Bound idle/slow client connections (slowloris) behind the public tunnel.
    timeout = 30

    def log_message(self, format, *args):
        # Suppress default access logging to stderr
        pass

    def log_error(self, format, *args):
        # Suppress default error logging to stderr
        pass

    def _log_request(self, route_name: str, status_code: int, duration_ms: float) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        client_ip = self.client_address[0] if (self.client_address and len(self.client_address) > 0) else "127.0.0.1"
        ip_hash = hashlib.sha256(client_ip.encode("utf-8")).hexdigest()[:8]
        method = self.command or "UNKNOWN"
        log_line = f"[{now_iso}] {method} {route_name} {status_code} {duration_ms:.2f}ms ip:{ip_hash}\n"
        sys.stderr.write(log_line)
        sys.stderr.flush()

    def _send_json(self, status_code: int, data: dict) -> None:
        body = json.dumps(data, separators=(",", ":")).encode("utf-8")
        if len(body) > 65536:
            err_data = {"status": "error", "code": "response_too_large"}
            body = json.dumps(err_data, separators=(",", ":")).encode("utf-8")
            status_code = 500

        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authenticate(self) -> bool:
        auth_header = self.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return False
        token = auth_header[7:].strip()
        if not token:
            return False
        return self.server.token_validator.verify(token)

    def _parse_json_body(self, allowed_keys: set[str]) -> tuple[dict | None, int | None, dict | None]:
        cfg = self.server.config
        content_type = self.headers.get("Content-Type", "")
        ct = content_type.split(";")[0].strip().lower()
        if ct != "application/json":
            return None, 400, {"status": "error", "code": "invalid_content_type"}

        content_length_str = self.headers.get("Content-Length")
        if not content_length_str:
            return None, 400, {"status": "error", "code": "missing_content_length"}

        try:
            content_length = int(content_length_str)
        except ValueError:
            return None, 400, {"status": "error", "code": "invalid_content_length"}

        if content_length < 0:
            return None, 400, {"status": "error", "code": "invalid_content_length"}

        if content_length > cfg.max_body_bytes + 2048:
            return None, 413, {"status": "error", "code": "payload_too_large"}

        raw_bytes = self.rfile.read(content_length)
        if len(raw_bytes) != content_length:
            return None, 400, {"status": "error", "code": "incomplete_body"}

        try:
            raw_str = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return None, 400, {"status": "error", "code": "invalid_utf8"}

        try:
            parsed = json.loads(raw_str, object_pairs_hook=_check_duplicate_keys)
        except ValueError as ve:
            if "duplicate_key" in str(ve):
                return None, 400, {"status": "error", "code": "duplicate_key"}
            return None, 400, {"status": "error", "code": "invalid_json"}
        except Exception:
            return None, 400, {"status": "error", "code": "invalid_json"}

        if not isinstance(parsed, dict):
            return None, 400, {"status": "error", "code": "invalid_json"}

        if _has_authority_field(parsed):
            return None, 400, {"status": "error", "code": "authority_field_refused"}

        unknown = set(parsed.keys()) - allowed_keys
        if unknown:
            return None, 400, {"status": "error", "code": "unknown_field"}

        return parsed, None, None

    def _resolve_route_name(self, path: str) -> str:
        cfg = self.server.config
        v1_prefix = cfg.path_prefix + "/v1/"
        if path.startswith(v1_prefix):
            subpath = path[len(v1_prefix):]
            if subpath in ("health", "send", "inbox", "channel", "ack", "advance"):
                return f"/v1/{subpath}"
        return "unknown"

    def handle_one_request(self) -> None:
        try:
            self.raw_requestline = self.rfile.readline(65537)
            if len(self.raw_requestline) > 65536:
                self.requestline = ""
                self.request_version = ""
                self.command = ""
                self.send_error(414)
                return
            if not self.raw_requestline:
                self.close_connection = True
                return
            if not self.parse_request():
                return

            if self.command in ("GET", "POST"):
                self._handle_request(self.command)
            else:
                self._handle_unsupported_method(self.command)
            self.wfile.flush()
        except TimeoutError as err:
            self.log_error("Request timed out: %r", err)
            self.close_connection = True
            return

    def do_GET(self) -> None:
        self._handle_request("GET")

    def do_POST(self) -> None:
        self._handle_request("POST")

    def do_HEAD(self) -> None:
        self._handle_unsupported_method("HEAD")

    def do_PUT(self) -> None:
        self._handle_unsupported_method("PUT")

    def do_DELETE(self) -> None:
        self._handle_unsupported_method("DELETE")

    def do_PATCH(self) -> None:
        self._handle_unsupported_method("PATCH")

    def do_OPTIONS(self) -> None:
        self._handle_unsupported_method("OPTIONS")

    def do_TRACE(self) -> None:
        self._handle_unsupported_method("TRACE")

    def do_CONNECT(self) -> None:
        self._handle_unsupported_method("CONNECT")

    def _handle_unsupported_method(self, method: str) -> None:
        start_time = time.monotonic()
        parsed_url = urllib.parse.urlparse(self.path)
        route_name = self._resolve_route_name(parsed_url.path)
        status_code = 405

        if method == "HEAD":
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Allow", "GET, POST")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            body = json.dumps({"status": "error", "code": "method_not_allowed"}, separators=(",", ":")).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Allow", "GET, POST")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        self._log_request(route_name, status_code, (time.monotonic() - start_time) * 1000.0)

    def _handle_request(self, method: str) -> None:
        start_time = time.monotonic()
        cfg = self.server.config
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path

        route_name = self._resolve_route_name(path)
        if route_name == "unknown":
            status_code = 404
            self._send_json(status_code, {"status": "error", "code": "not_found"})
            self._log_request(route_name, status_code, (time.monotonic() - start_time) * 1000.0)
            return

        subpath = route_name[len("/v1/"):]

        if not self._authenticate():
            status_code = 401
            self._send_json(status_code, {"status": "error", "code": "unauthorized"})
            self._log_request(route_name, status_code, (time.monotonic() - start_time) * 1000.0)
            return

        if not self.server.rate_limiter.allow():
            status_code = 429
            self._send_json(status_code, {"status": "error", "code": "rate_limited"})
            self._log_request(route_name, status_code, (time.monotonic() - start_time) * 1000.0)
            return

        if subpath == "health":
            if method != "GET":
                status_code = 405
                self._send_json(status_code, {"status": "error", "code": "method_not_allowed"})
            else:
                try:
                    res = self.server.backend.presence(ttl_s=900, status="grok-bot gateway")
                    if isinstance(res, dict) and res.get("status") in ("error", "degraded"):
                        code = res.get("code") or ("office_unavailable" if res.get("status") == "degraded" else "office_error")
                        http_code = 502 if res.get("status") == "degraded" else (409 if code == "dedupe_conflict" else 400)
                        self._send_json(http_code, {"status": "error", "code": str(code)})
                        return
                    status_code = 200
                    resp_data = {
                        "status": "ok",
                        "agent": cfg.agent_id,
                        "channel": cfg.channel,
                        "targets": list(cfg.targets.keys()),
                        "target_agents": dict(cfg.targets),
                    }
                    self._send_json(status_code, resp_data)
                except Exception as exc:
                    status_code, resp_data = _handle_office_exception(exc)
                    self._send_json(status_code, resp_data)

        elif subpath == "send":
            if method != "POST":
                status_code = 405
                self._send_json(status_code, {"status": "error", "code": "method_not_allowed"})
            else:
                body_dict, err_status, err_resp = self._parse_json_body(
                    {"target", "body", "kind", "reply_to", "dedupe_key"}
                )
                if err_status is not None:
                    status_code = err_status
                    self._send_json(status_code, err_resp)
                else:
                    status_code, resp_data = self._handle_send(body_dict)
                    self._send_json(status_code, resp_data)

        elif subpath in ("inbox", "channel"):
            if method != "GET":
                status_code = 405
                self._send_json(status_code, {"status": "error", "code": "method_not_allowed"})
            else:
                status_code, resp_data = self._handle_read(subpath, parsed_url.query)
                self._send_json(status_code, resp_data)

        elif subpath == "ack":
            if method != "POST":
                status_code = 405
                self._send_json(status_code, {"status": "error", "code": "method_not_allowed"})
            else:
                body_dict, err_status, err_resp = self._parse_json_body({"message_id", "note"})
                if err_status is not None:
                    status_code = err_status
                    self._send_json(status_code, err_resp)
                else:
                    status_code, resp_data = self._handle_ack(body_dict)
                    self._send_json(status_code, resp_data)

        elif subpath == "advance":
            if method != "POST":
                status_code = 405
                self._send_json(status_code, {"status": "error", "code": "method_not_allowed"})
            else:
                body_dict, err_status, err_resp = self._parse_json_body({"source", "through_id"})
                if err_status is not None:
                    status_code = err_status
                    self._send_json(status_code, err_resp)
                else:
                    status_code, resp_data = self._handle_advance(body_dict)
                    self._send_json(status_code, resp_data)

        self._log_request(route_name, status_code, (time.monotonic() - start_time) * 1000.0)

    def _handle_send(self, body_dict: dict) -> tuple[int, dict]:
        cfg = self.server.config
        target = body_dict.get("target")
        if not isinstance(target, str):
            return 400, {"status": "error", "code": "missing_target"}

        if target == "channel":
            to_agent = None
            use_channel = cfg.channel
        elif target in cfg.targets:
            to_agent = cfg.targets[target]
            use_channel = None
        else:
            return 400, {"status": "error", "code": "target_not_allowed"}

        body_str = body_dict.get("body")
        if not isinstance(body_str, str):
            return 400, {"status": "error", "code": "missing_body"}

        body_bytes = body_str.encode("utf-8")
        if len(body_bytes) < 1:
            return 400, {"status": "error", "code": "invalid_body"}
        if len(body_bytes) > cfg.max_body_bytes:
            return 413, {"status": "error", "code": "body_too_large"}

        kind = body_dict.get("kind", "note")
        if not isinstance(kind, str) or kind not in cfg.allowed_kinds:
            return 400, {"status": "error", "code": "kind_not_allowed"}

        reply_to = body_dict.get("reply_to")
        if reply_to is not None:
            if not isinstance(reply_to, str) or len(reply_to) != 32 or not all(c in "0123456789abcdef" for c in reply_to):
                return 400, {"status": "error", "code": "invalid_reply_to"}

        dedupe_key = body_dict.get("dedupe_key")
        if dedupe_key is not None:
            if not isinstance(dedupe_key, str) or not re.match(r"^[A-Za-z0-9._:-]{1,128}$", dedupe_key):
                return 400, {"status": "error", "code": "invalid_dedupe_key"}

        try:
            res = self.server.backend.send(
                to=to_agent,
                body=body_str,
                kind=kind,
                reply_to=reply_to,
                channel=use_channel,
                dedupe_key=dedupe_key,
            )
            if isinstance(res, dict):
                res_status = res.get("status")
                res_code = res.get("code")
                if res_status == "error":
                    if res_code == "dedupe_conflict":
                        return 409, {"status": "error", "code": "dedupe_conflict"}
                    return 400, {"status": "error", "code": str(res_code or "office_error")}
                if res_status == "degraded":
                    return 502, {"status": "error", "code": str(res_code or "office_unavailable")}

            if not isinstance(res, dict):
                return 502, {"status": "error", "code": "office_contract_violation"}

            msg_id = res.get("id")
            if not isinstance(msg_id, str) or len(msg_id) != 32 or not all(c in "0123456789abcdef" for c in msg_id):
                return 502, {"status": "error", "code": "office_contract_violation"}

            status = res.get("status", "ok")
            filtered = {
                "status": str(status) if status is not None else "ok",
                "id": str(msg_id),
            }
            if "ts" in res and res["ts"] is not None:
                filtered["ts"] = res["ts"]
            if "code" in res and res["code"] is not None:
                filtered["code"] = res["code"]
            return 200, filtered
        except Exception as exc:
            return _handle_office_exception(exc)

    def _handle_read(self, route_type: str, query_string: str) -> tuple[int, dict]:
        cfg = self.server.config
        query = urllib.parse.parse_qs(query_string)

        if "peek" in query:
            peek_raw = query["peek"][0].strip().lower()
            if peek_raw in ("0", "false", "no"):
                return 400, {"status": "error", "code": "consume_not_supported"}

        # Reads never consume implicitly
        peek = True

        reply_to_filter = None
        if route_type == "inbox" and "reply_to" in query:
            reply_to_filter = query["reply_to"][0].strip()
            if len(reply_to_filter) != 32 or not all(c in "0123456789abcdef" for c in reply_to_filter):
                return 400, {"status": "error", "code": "invalid_reply_to"}

        if reply_to_filter is not None:
            limit = 200
        else:
            limit_raw = query.get("limit", [str(cfg.max_read_limit)])[0]
            try:
                limit = int(limit_raw)
            except ValueError:
                limit = cfg.max_read_limit
            limit = min(max(1, limit), cfg.max_read_limit)

        # The reply_to filter runs before the 64 KiB client response cap, so the
        # internal scan needs its own budget: up to 200 full-size messages.
        scan_bytes = 200 * (cfg.max_body_bytes + 2048) if reply_to_filter is not None else 60000
        try:
            if route_type == "inbox":
                res = self.server.backend.read(
                    limit=limit,
                    peek=peek,
                    max_body_bytes=cfg.max_body_bytes,
                    max_response_bytes=scan_bytes,
                )
            else:
                res = self.server.backend.read(
                    limit=limit,
                    peek=peek,
                    channel=cfg.channel,
                    max_body_bytes=cfg.max_body_bytes,
                    max_response_bytes=60000,
                )

            raw_msgs = []
            remaining = 0
            cursor_advanced = False
            if isinstance(res, dict):
                res_status = res.get("status")
                res_code = res.get("code")
                if res_status == "error":
                    if res_code == "dedupe_conflict":
                        return 409, {"status": "error", "code": "dedupe_conflict"}
                    return 400, {"status": "error", "code": str(res_code or "office_error")}
                if res_status == "degraded":
                    return 502, {"status": "error", "code": str(res_code or "office_unavailable")}
                raw_msgs = res.get("messages", res.get("items", []))
                remaining = res.get("remaining", 0)
                cursor_advanced = res.get("cursor_advanced", False)
            else:
                raw_msgs = getattr(res, "messages", getattr(res, "items", []))
                remaining = getattr(res, "remaining", 0)
                cursor_advanced = getattr(res, "cursor_advanced", False)

            extracted = []
            for item in raw_msgs:
                if isinstance(item, dict):
                    untrusted = item.get("untrusted_message") or item
                else:
                    untrusted = getattr(item, "untrusted_message", item)

                def _extract_val(obj, key, alt=None):
                    if isinstance(obj, dict):
                        val = obj.get(key)
                        if val is not None:
                            return val
                        return obj.get(alt) if alt else None
                    val = getattr(obj, key, None)
                    if val is not None:
                        return val
                    return getattr(obj, alt, None) if alt else None

                msg_id = _extract_val(untrusted, "id") or _extract_val(item, "id")
                ts = _extract_val(untrusted, "ts") or _extract_val(item, "ts")
                from_agent = _extract_val(untrusted, "from", "sender") or _extract_val(item, "from", "sender")
                kind = _extract_val(untrusted, "kind") or _extract_val(item, "kind")
                reply_to = _extract_val(untrusted, "reply_to") or _extract_val(item, "reply_to")
                body = _extract_val(untrusted, "body") or _extract_val(item, "body")
                ack_of = _extract_val(untrusted, "ack_of") or _extract_val(item, "ack_of")

                extracted.append({
                    "id": msg_id,
                    "ts": ts,
                    "from": from_agent,
                    "kind": kind,
                    "reply_to": reply_to,
                    "ack_of": ack_of,
                    "body": body,
                    "trust": "untrusted",
                })

            if reply_to_filter is not None:
                # An ACK envelope also carries reply_to; it is a receipt, never a result.
                extracted = [
                    m for m in extracted
                    if m.get("reply_to") == reply_to_filter and not m.get("ack_of")
                ]

            body = {
                "status": "ok",
                "messages": extracted,
                "remaining": remaining,
                "cursor_advanced": cursor_advanced,
            }
            if reply_to_filter is not None:
                # Bounded scan, never consuming: if unread messages beyond the scan window
                # remain and nothing matched, say so explicitly instead of an empty "no reply".
                scanned = len(raw_msgs)
                body["scanned"] = scanned
                body["scan_incomplete"] = bool(not extracted and isinstance(remaining, int) and remaining > 0)
            return 200, body
        except Exception as exc:
            return _handle_office_exception(exc)

    def _handle_ack(self, body_dict: dict) -> tuple[int, dict]:
        message_id = body_dict.get("message_id")
        if not isinstance(message_id, str) or len(message_id) != 32 or not all(c in "0123456789abcdef" for c in message_id):
            return 400, {"status": "error", "code": "invalid_message_id"}

        note = body_dict.get("note")
        if note is not None and (not isinstance(note, str) or len(note) > 500):
            return 400, {"status": "error", "code": "invalid_note"}

        try:
            res = self.server.backend.ack(message_id=message_id, note=note)
            if isinstance(res, dict):
                res_status = res.get("status")
                res_code = res.get("code")
                if res_status == "error":
                    if res_code == "dedupe_conflict":
                        return 409, {"status": "error", "code": "dedupe_conflict"}
                    return 400, {"status": "error", "code": str(res_code or "office_error")}
                if res_status == "degraded":
                    return 502, {"status": "error", "code": str(res_code or "office_unavailable")}
            return 200, {"status": "ok"}
        except Exception as exc:
            return _handle_office_exception(exc)

    def _handle_advance(self, body_dict: dict) -> tuple[int, dict]:
        # Serialize the whole peek -> consume -> verify sequence: this gateway is the
        # only consumer of its own inbox/channel cursors, so a process-wide lock makes
        # concurrent advances for the same through_id atomic (the second gets 404).
        with self.server.advance_lock:
            return self._handle_advance_locked(body_dict)

    def _handle_advance_locked(self, body_dict: dict) -> tuple[int, dict]:
        cfg = self.server.config
        source = body_dict.get("source")
        if not isinstance(source, str) or source not in ("inbox", "channel"):
            return 400, {"status": "error", "code": "invalid_source"}

        through_id = body_dict.get("through_id")
        if not isinstance(through_id, str) or len(through_id) != 32 or not all(c in "0123456789abcdef" for c in through_id):
            return 400, {"status": "error", "code": "invalid_through_id"}

        use_channel = cfg.channel if source == "channel" else None

        try:
            # 1. Do a peek read (limit 100) of that source
            if source == "inbox":
                peek_res = self.server.backend.read(
                    limit=100,
                    peek=True,
                    max_body_bytes=cfg.max_body_bytes,
                    max_response_bytes=60000,
                )
            else:
                peek_res = self.server.backend.read(
                    limit=100,
                    peek=True,
                    channel=use_channel,
                    max_body_bytes=cfg.max_body_bytes,
                    max_response_bytes=60000,
                )

            if isinstance(peek_res, dict):
                res_status = peek_res.get("status")
                res_code = peek_res.get("code")
                if res_status == "error":
                    if res_code == "dedupe_conflict":
                        return 409, {"status": "error", "code": "dedupe_conflict"}
                    return 400, {"status": "error", "code": str(res_code or "office_error")}
                if res_status == "degraded":
                    return 502, {"status": "error", "code": str(res_code or "office_unavailable")}
                peek_msgs = peek_res.get("messages", peek_res.get("items", []))
            else:
                peek_msgs = getattr(peek_res, "messages", getattr(peek_res, "items", []))

            def _get_msg_id(item):
                if isinstance(item, dict):
                    untrusted = item.get("untrusted_message") or item
                    return untrusted.get("id") or item.get("id")
                untrusted = getattr(item, "untrusted_message", item)
                return getattr(untrusted, "id", None) or getattr(item, "id", None)

            peeked_ids = [_get_msg_id(m) for m in peek_msgs]

            # 2. Find the index of through_id among unread messages (absent -> 404 code not_in_unread)
            if through_id not in peeked_ids:
                return 404, {"status": "error", "code": "not_in_unread"}

            target_idx = peeked_ids.index(through_id)
            consume_limit = target_idx + 1

            # 3. Consuming read with limit=index+1
            if source == "inbox":
                consume_res = self.server.backend.read(
                    limit=consume_limit,
                    peek=False,
                    max_body_bytes=cfg.max_body_bytes,
                    max_response_bytes=60000,
                )
            else:
                consume_res = self.server.backend.read(
                    limit=consume_limit,
                    peek=False,
                    channel=use_channel,
                    max_body_bytes=cfg.max_body_bytes,
                    max_response_bytes=60000,
                )

            if isinstance(consume_res, dict):
                res_status = consume_res.get("status")
                res_code = consume_res.get("code")
                if res_status == "error":
                    if res_code == "dedupe_conflict":
                        return 409, {"status": "error", "code": "dedupe_conflict"}
                    return 400, {"status": "error", "code": str(res_code or "office_error")}
                if res_status == "degraded":
                    return 502, {"status": "error", "code": str(res_code or "office_unavailable")}
                consumed_msgs = consume_res.get("messages", consume_res.get("items", []))
            else:
                consumed_msgs = getattr(consume_res, "messages", getattr(consume_res, "items", []))

            consumed_ids = [_get_msg_id(m) for m in consumed_msgs]

            # Verify the returned ids equal the peeked prefix (mismatch -> 409 code cursor_race)
            expected_prefix = peeked_ids[:consume_limit]
            if consumed_ids != expected_prefix:
                return 409, {"status": "error", "code": "cursor_race"}

            return 200, {"status": "ok", "advanced": len(consumed_ids)}
        except Exception as exc:
            return _handle_office_exception(exc)


class GrokOfficeHTTPServer(ThreadingHTTPServer):
    """Threading HTTP Server storing gateway state and backend references."""

    def __init__(self, server_address, RequestHandlerClass, config: GatewayConfig, backend=None):
        self.config = config
        self.token_validator = TokenValidator(config.token_sha256_file)
        self.rate_limiter = SlidingWindowRateLimiter(config.rate_limit_requests, config.rate_limit_window_s)
        self.advance_lock = threading.Lock()
        if backend is not None:
            self.backend = backend
        else:
            self.backend = OfficeBackend.from_config(config)
        super().__init__(server_address, RequestHandlerClass)


def create_server(config: GatewayConfig, backend=None) -> GrokOfficeHTTPServer:
    """Create a configured GrokOfficeHTTPServer."""
    class _CustomServer(GrokOfficeHTTPServer):
        address_family = socket.AF_INET6 if config.listen_host == "::1" else socket.AF_INET

    return _CustomServer((config.listen_host, config.listen_port), GrokOfficeHandler, config, backend=backend)


def hash_token_stdin() -> None:
    """Reads a token from stdin and prints only its SHA-256 hex."""
    token = sys.stdin.read().strip()
    if not token:
        sys.stderr.write("Error: empty token provided\n")
        sys.exit(1)
    sha256_hex = hashlib.sha256(token.encode("utf-8")).hexdigest().lower()
    sys.stdout.write(sha256_hex + "\n")
    sys.stdout.flush()


def main(argv=None) -> None:
    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser(description="Grok Office Gateway")
    parser.add_argument("--config", help="Path to config JSON file")
    parser.add_argument(
        "--hash-token-stdin",
        action="store_true",
        help="Read token from stdin, print SHA-256 hex, and exit",
    )
    args = parser.parse_args(argv)

    if args.hash_token_stdin:
        hash_token_stdin()
        return

    if not args.config:
        parser.print_help(sys.stderr)
        sys.exit(1)

    with open(args.config, "r", encoding="utf-8") as f:
        raw_cfg = json.load(f)

    cfg = GatewayConfig(raw_cfg)
    server = create_server(cfg)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
