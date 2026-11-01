"""Standard newline-delimited MCP stdio client for the scoped HTTP broker.

Credentials come from a private operator file or environment, never tool args.
The stdio runtime freezes that configuration before reading its first message.
If that managed assignment is unavailable, tools remain discoverable and calls
return an explicit fail-closed blocker without attempting an HTTP connection.
stdout contains JSON-RPC only. The process does not spawn a shell or browser.
"""

from __future__ import annotations

import copy
import http.client
import json
import os
import re
import secrets
import sys
from pathlib import Path
from urllib.parse import urlsplit

from broker import ACTIONS, MAX_BODY, Reject, exact, ident, json_bytes, parse_json, tab_id, utf16_length

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
BLOCKER = "BLOCKED_MANAGED_BROWSER_UNAVAILABLE"
UNAVAILABLE_REASON_RE = re.compile(r"[a-z0-9_]{1,40}\Z")
UNAVAILABLE_INSTRUCTION = (
    "Agent browser (Fleet Chrome Relay) is not available in this session. "
    "Report this exact blocker to the owner and stop the browser part of the task. "
    "Do not switch to the owner's private Chrome (claude-in-chrome, Codex Chrome plugin, CDP) "
    "or any other browser tool to work around it. After the owner starts the relay, "
    "a new session (or MCP reconnect) picks it up."
)


def validate_client_info(info):
    # Standard implementation metadata is ignored: no URL/icon is fetched and
    # no identity, permission, profile or origin is derived from it.
    exact(info, ("name", "version"), ("title", "description", "websiteUrl", "icons"))
    for key, item in info.items():
        if key != "icons" and (not isinstance(item, str) or len(item) > 4096):
            raise Reject("params")
    if "icons" in info:
        icons = info["icons"]
        if not isinstance(icons, list) or len(icons) > 16:
            raise Reject("params")
        for icon in icons:
            exact(icon, ("src",), ("mimeType", "sizes", "theme"))
            if not isinstance(icon["src"], str) or len(icon["src"]) > 32768:
                raise Reject("params")
            if "mimeType" in icon and not isinstance(icon["mimeType"], str):
                raise Reject("params")
            if "theme" in icon and icon["theme"] not in ("light", "dark"):
                raise Reject("params")
            sizes = icon.get("sizes", [])
            if not isinstance(sizes, list) or len(sizes) > 32 or any(not isinstance(size, str) for size in sizes):
                raise Reject("params")
BASE_PROPERTIES = {"request_id": {"type": "string", "pattern": "^[A-Za-z0-9._:-]{1,128}$", "description": "Stable ID for this operation. Reuse only for identical retries; an unknown mutation is never replayed."}}


def tool_specs():
    definitions = [
        ("chrome_attachment", "Observe this session's attachment and reconcile an existing authorized technical connection.", {}, []),
        ("chrome_open", "Open an inactive owned tab within the fixed approved origin policy.", {"url": {"type": "string", "maxLength": 2048}}, ["url"]),
        ("chrome_read", "Read a bounded redacted excerpt and fresh opaque element references from an owned tab.", {}, []),
        ("chrome_click", "Click a fresh reference from this session's most recent read, within approved actions.", {"ref": {"type": "string", "maxLength": 32}}, ["ref"]),
        ("chrome_type", "Type ordinary text into a fresh reference; credentials, MFA and passkeys are denied.", {"ref": {"type": "string", "maxLength": 32}, "text": {"type": "string", "minLength": 1, "maxLength": 1000}}, ["ref", "text"]),
        ("chrome_wait", "Observe bounded text, role or name on an owned tab; at least one condition is required.", {key: {"type": "string", "minLength": 1, "maxLength": 200} for key in ("text", "role", "name")}, []),
        ("chrome_close", "Close only a tab owned by this session.", {}, []),
        ("chrome_saved_login", "Submit the owner-approved saved-password form on an owned login tab once. Takes no credentials; returns submitted with authentication unverified.", {}, []),
    ]
    result = []
    for name, description, properties, required in definitions:
        properties = {**copy.deepcopy(BASE_PROPERTIES), **properties}
        required = list(required)
        if name not in ("chrome_attachment", "chrome_open"):
            properties["tab_id"] = {"type": "integer", "minimum": 1, "maximum": 2147483647}
            required.insert(0, "tab_id")
        schema = {"type": "object", "properties": properties, "additionalProperties": False}
        if required:
            schema["required"] = required
        if name == "chrome_wait":
            schema["anyOf"] = [{"required": [key]} for key in ("text", "role", "name")]
        read_only = name in ("chrome_attachment", "chrome_read", "chrome_wait")
        result.append({"name": name, "description": description, "inputSchema": schema,
                       "annotations": {"readOnlyHint": read_only, "destructiveHint": not read_only, "idempotentHint": read_only, "openWorldHint": True}})
    return result


def operation(name, arguments):
    if name not in ACTIONS or not isinstance(arguments, dict):
        raise Reject("tool")
    allowed = set(BASE_PROPERTIES)
    required = set()
    if name not in ("chrome_attachment", "chrome_open"):
        allowed.add("tab_id")
        required.add("tab_id")
    if name == "chrome_open":
        allowed.add("url")
        required.add("url")
    elif name in ("chrome_click", "chrome_type"):
        allowed.add("ref")
        required.add("ref")
        if name == "chrome_type":
            allowed.add("text")
            required.add("text")
    elif name == "chrome_wait":
        allowed.update(("role", "name", "text"))
    exact(arguments, required, allowed - required)
    rid = ident(arguments["request_id"]) if "request_id" in arguments else secrets.token_hex(16)
    result = {"request_id": rid, "action": name, "args": {key: value for key, value in arguments.items() if key not in ("request_id", "tab_id")}}
    if "tab_id" in arguments:
        result["tab_id"] = tab_id(arguments["tab_id"])
    # Check strings before they can reach a request header or encoder.
    args = result["args"]
    if name == "chrome_open" and (not isinstance(args["url"], str) or not 1 <= len(args["url"]) <= 2048):
        raise Reject("bound")
    if name in ("chrome_click", "chrome_type") and (not isinstance(args["ref"], str) or not re.fullmatch(r"ref_[a-f0-9]{24}", args["ref"])):
        raise Reject("stale_ref")
    if name == "chrome_type" and (not isinstance(args["text"], str) or not 1 <= utf16_length(args["text"]) <= 1000):
        raise Reject("bound")
    if name == "chrome_wait" and (not args or any(not isinstance(value, str) or not 1 <= utf16_length(value) <= 200 for value in args.values())):
        raise Reject("bound")
    if len(json_bytes(result)) > MAX_BODY:
        raise Reject("bound")
    return result


class Client:
    def __init__(self, broker_base, session_id, token, timeout=15):
        try:
            parsed = urlsplit(broker_base)
            if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port or parsed.path not in ("", "/") or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
                raise ValueError()
            self.port = parsed.port
            self.sid = ident(session_id)
            if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9._~+/-]{16,256}={0,2}", token) or not 0.1 <= timeout <= 65:
                raise ValueError()
            self.token, self.timeout = token, timeout
        except (ValueError, TypeError, Reject):
            raise Reject("client_config") from None

    @classmethod
    def from_environment(cls):
        path = os.environ.get("CHROME_RELAY_CLIENT_FILE")
        if path:
            try:
                with Path(path).open("rb") as stream:
                    value = parse_json(stream.read(MAX_BODY + 1))
                exact(value, ("broker_base", "session_id", "token"))
                return cls(value["broker_base"], value["session_id"], value["token"])
            except (OSError, ValueError, Reject, TypeError):
                raise Reject("client_config") from None
        return cls(os.environ.get("CHROME_RELAY_BROKER", ""), os.environ.get("CHROME_RELAY_SESSION_ID", ""), os.environ.get("CHROME_RELAY_TOKEN", ""))

    def call(self, name, arguments):
        command = operation(name, arguments)
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=self.timeout)
        try:
            connection.request("POST", "/mcp", json_bytes(command), {"Content-Type": "application/json", "Authorization": "Bearer " + self.token, "X-Session-Id": self.sid})
            response = connection.getresponse()
            raw = response.read(MAX_BODY + 1)
            value = parse_json(raw)
            if response.status != 200:
                # Never return arbitrary error bodies or echoes from a service.
                reason = value.get("error") if isinstance(value, dict) else None
                reason = reason if reason in ("authorization", "request_id_conflict", "action", "ownership", "origin", "oauth", "unknown_field", "stale_ref", "bound", "expired") else "broker_rejected"
                return {"request_id": command["request_id"], "status": "denied", "data": {"reason": reason}}
            exact(value, ("request_id", "session_id", "profile_id", "status", "data"), ("tab_id",))
            if value["request_id"] != command["request_id"] or value["session_id"] != self.sid or value["status"] not in ("ok", "denied", "unknown") or not isinstance(value["data"], dict):
                raise Reject("broker_response")
            # Defense against a wrong localhost service reflecting client secrets.
            if self.token.encode() in raw:
                raise Reject("broker_response")
            return value
        except (OSError, http.client.HTTPException, Reject, ValueError, TypeError):
            # Includes client timeout: never retry a mutation automatically.
            return {"request_id": command["request_id"], "status": "unknown", "data": {"reason": "transport"}}
        finally:
            connection.close()


class MCP:
    def __init__(self, client=None, unavailable_reason="client_config"):
        self.client = client
        self.unavailable_reason = unavailable_reason
        self.initialized = False
        self.ready = False

    @staticmethod
    def error(rid, code, message):
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}

    def handle(self, value):
        rid = value.get("id") if isinstance(value, dict) else None
        valid_id = type(rid) in (int, str)
        try:
            exact(value, ("jsonrpc", "method"), ("id", "params"))
            if value["jsonrpc"] != "2.0" or not isinstance(value["method"], str) or ("id" in value and not valid_id):
                raise Reject("request")
        except Reject:
            return self.error(rid if valid_id else None, -32600, "Invalid Request")
        method, params = value["method"], value.get("params", {})
        notification = "id" not in value
        if notification:
            if (method == "notifications/initialized" and self.initialized and isinstance(params, dict)
                    and not set(params) - {"_meta"} and ("_meta" not in params or isinstance(params["_meta"], dict))):
                self.ready = True
            # Standard notifications, including cancellation, receive no reply.
            return None
        try:
            if method == "initialize":
                exact(params, ("protocolVersion", "capabilities", "clientInfo"), ("_meta",))
                validate_client_info(params["clientInfo"])
                if (self.initialized or not isinstance(params["protocolVersion"], str) or not isinstance(params["capabilities"], dict)
                        or ("_meta" in params and not isinstance(params["_meta"], dict))):
                    raise Reject("params")
                version = params["protocolVersion"] if params["protocolVersion"] in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
                self.initialized = True
                result = {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "fleet-chrome-relay", "version": "1.2.0"}}
            elif method == "ping":
                exact(params, (), ("_meta",))
                result = {}
            elif not self.ready:
                return self.error(rid, -32000, "MCP initialization required")
            elif method == "tools/list":
                exact(params, (), ("cursor", "_meta"))
                if "cursor" in params and (not isinstance(params["cursor"], str) or params["cursor"]):
                    raise Reject("params")
                result = {"tools": tool_specs()}
            elif method == "tools/call":
                exact(params, ("name",), ("arguments", "_meta"))
                if "_meta" in params and not isinstance(params["_meta"], dict):
                    raise Reject("params")
                # Invalid tool parameters are protocol errors, not HTTP effects.
                command = operation(params["name"], params.get("arguments", {}))
                if self.client is None:
                    response = {
                        "request_id": command["request_id"],
                        "status": "denied",
                        "data": {
                            "reason": "relay_unavailable",
                            "blocker": BLOCKER,
                            "detail": self.unavailable_reason,
                            "instruction": UNAVAILABLE_INSTRUCTION,
                        },
                    }
                else:
                    response = self.client.call(params["name"], params.get("arguments", {}))
                result = {"content": [{"type": "text", "text": json_bytes(response).decode("utf-8")}], "isError": response["status"] != "ok"}
            else:
                return self.error(rid, -32601, "Method not found")
        except (Reject, ValueError, TypeError, UnicodeError):
            return self.error(rid, -32602, "Invalid params")
        return {"jsonrpc": "2.0", "id": rid, "result": result}


def serve(stdin=None, stdout=None):
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    if "CHROME_RELAY_UNAVAILABLE" in os.environ:
        reason = os.environ["CHROME_RELAY_UNAVAILABLE"]
        if not UNAVAILABLE_REASON_RE.fullmatch(reason):
            reason = "invalid_reason"
        server = MCP(None, reason)
    else:
        try:
            client = Client.from_environment()
            server = MCP(client)
        except Reject:
            server = MCP(None, "client_config")  # Missing/invalid startup scope stays denied until restart.
    while True:
        line = stdin.readline(MAX_BODY + 2)
        if not line:
            return 0
        if len(line) > MAX_BODY + 1 or (len(line) > MAX_BODY and not line.endswith(b"\n")):
            reply = MCP.error(None, -32700, "Message exceeds limit")
            stdout.write(json_bytes(reply) + b"\n")
            stdout.flush()
            return 1
        try:
            value = parse_json(line.rstrip(b"\r\n"))
            reply = server.handle(value)
        except Reject:
            reply = MCP.error(None, -32700, "Parse error")
        if reply is not None:
            stdout.write(json_bytes(reply) + b"\n")
            stdout.flush()


if __name__ == "__main__":
    raise SystemExit(serve())
