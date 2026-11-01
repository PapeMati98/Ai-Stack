"""Default-off, headless Hermes access to correlated owner replies and consumed-reply ACK.

This adapter is deliberately narrow: its only remote operations are the pure
own-inbox lookup and caller-owned consumed-reply acknowledgment.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
import re
from typing import Any

from .identity import agent_identity
from .office import HANDLING_TEXT, frame_untrusted, parse_envelope
from .office_remote import OfficeRemoteClient, OfficeRemoteConfig

OP = "read-correlated-replies"
ACK_OP = "ack"
CODE = "correlated_owner_replies.v1"
ACK_CODE = "ack_consumed_reply"
HERMES_HARNESS = "hermes"
HERMES_HOST = "lab-server"
HERMES_SESSION_ID = "office-owner-reply-port"
W3_HARNESS = "hermes"
W3_HOST = "dots-win-pilot"
W3_SESSION_ID = "0cab8bac-b101-57f2-a4ff-268ad78b5bdb"
W3_AGENT_ID = "hermes-dots-win-pilot-0cab8bac-b101-57f2-a4ff-268ad78b5bdb"
W3_WIRE_IDENTITY = {
    "harness": W3_HARNESS,
    "host": W3_HOST,
    "session_id": W3_SESSION_ID,
}
_MESSAGE_ID_RE = re.compile(r"[0-9a-f]{32}")
_IDENTITY = agent_identity(
    HERMES_HARNESS, host=HERMES_HOST, session_id=HERMES_SESSION_ID, env={}
)
_WIRE_IDENTITY = {
    "harness": HERMES_HARNESS,
    "host": HERMES_HOST,
    "session_id": HERMES_SESSION_ID,
}


class OwnerReplyPort:
    """A validated Hermes identity and allowlisted queries, disabled by default."""

    def __init__(
        self,
        client: Any = None,
        *,
        enabled: bool = False,
        identity: Mapping[str, Any] | None = None,
        harness: str | None = None,
        host: str | None = None,
        session_id: str | None = None,
    ) -> None:
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be a boolean")
        self._client = client
        self.enabled = enabled

        cfg_harness = harness
        cfg_host = host
        cfg_session_id = session_id
        if identity is not None:
            if not isinstance(identity, Mapping):
                raise TypeError("identity must be a mapping")
            if "agent_id" in identity and not ("harness" in identity and "session_id" in identity):
                aid = identity["agent_id"]
                if aid == W3_AGENT_ID:
                    cfg_harness, cfg_host, cfg_session_id = W3_HARNESS, W3_HOST, W3_SESSION_ID
                elif aid == _IDENTITY["agent_id"]:
                    cfg_harness, cfg_host, cfg_session_id = HERMES_HARNESS, HERMES_HOST, HERMES_SESSION_ID
                else:
                    raise ValueError(f"unknown or untrusted agent_id: {aid}")
            else:
                cfg_harness = identity.get("harness", cfg_harness)
                cfg_host = identity.get("host", cfg_host)
                cfg_session_id = identity.get("session_id", cfg_session_id)

        if cfg_harness is None:
            cfg_harness = HERMES_HARNESS
        if cfg_host is None:
            cfg_host = HERMES_HOST
        if cfg_session_id is None:
            cfg_session_id = HERMES_SESSION_ID

        if cfg_harness != HERMES_HARNESS:
            raise ValueError(f"harness must be {HERMES_HARNESS!r}")
        if not isinstance(cfg_host, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", cfg_host):
            raise ValueError("invalid host")
        if not isinstance(cfg_session_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", cfg_session_id):
            raise ValueError("invalid session_id")

        self.harness = cfg_harness
        self.host = cfg_host
        self.session_id = cfg_session_id
        self._wire_identity = {
            "harness": self.harness,
            "host": self.host,
            "session_id": self.session_id,
        }
        self._identity = agent_identity(
            self.harness, host=self.host, session_id=self.session_id, env={}
        )
        self._agent_id = self._identity["agent_id"]

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def wire_identity(self) -> dict[str, str]:
        return dict(self._wire_identity)

    @classmethod
    def for_w3(
        cls,
        client: Any = None,
        *,
        enabled: bool = False,
        config: OfficeRemoteConfig | None = None,
        popen_factory: Any = None,
    ) -> "OwnerReplyPort":
        """Explicit pinned factory for the trusted W3 Hermes caller tuple."""
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be a boolean")
        w3_identity = dict(W3_WIRE_IDENTITY)
        if not enabled:
            return cls(enabled=False, identity=w3_identity)
        if client is not None:
            return cls(client, enabled=True, identity=w3_identity)
        if not isinstance(config, OfficeRemoteConfig):
            return cls(enabled=True, identity=w3_identity)
        client = OfficeRemoteClient(config, popen_factory=popen_factory)
        return cls(client, enabled=True, identity=w3_identity)

    @classmethod
    def from_config(
        cls,
        config: OfficeRemoteConfig | None = None,
        *,
        enabled: bool = False,
        popen_factory: Any = None,
        identity: Mapping[str, Any] | None = None,
    ) -> "OwnerReplyPort":
        """Build only from an already-validated config; OFF performs no path lookup."""
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be a boolean")
        if not enabled:
            return cls(enabled=False, identity=identity)
        if not isinstance(config, OfficeRemoteConfig):
            return cls(enabled=True, identity=identity)
        # The optional Python seam is for in-process tests. No callable or module name is
        # accepted in RPC/JSON, and OfficeRemoteClient still owns its fixed request framing.
        client = OfficeRemoteClient(config, popen_factory=popen_factory)
        return cls(client, enabled=True, identity=identity)

    def read_owner_replies(
        self,
        project_slug: Any = None,
        parent_receipts: Any = None,
        limit: int = 5,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if project_slug is None and "project_slug" in kwargs:
            project_slug = kwargs["project_slug"]
        if parent_receipts is None and "parent_receipts" in kwargs:
            parent_receipts = kwargs["parent_receipts"]
        if "limit" in kwargs:
            limit = kwargs["limit"]
        query = {
            "project_slug": project_slug,
            "parent_receipts": list(parent_receipts) if isinstance(parent_receipts, (list, tuple)) else parent_receipts,
            "limit": limit,
        }
        if not self.enabled:
            return self._unavailable("absent", "owner_reply_port_disabled")
        if self._client is None or not callable(getattr(self._client, "request", None)):
            return self._unavailable("unavailable", "owner_reply_port_unavailable")
        invalid = self._validate_query(query)
        if invalid is not None:
            return self._unavailable("error", invalid)
        identity = dict(self._wire_identity)
        try:
            response = self._client.request(OP, identity, query)
        except Exception:  # noqa: BLE001 - the adapter is fail-closed and never exposes transport data
            return self._unavailable("unavailable", "office_remote_unavailable")
        return self._validate_response(response, query)

    def ack_consumed_reply(
        self,
        *args: Any,
        identity: Mapping[str, Any] | None = None,
        message_id: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Acknowledge one consumed reply using the existing Office ack RPC."""
        if len(args) == 1:
            if message_id is None and isinstance(args[0], str):
                message_id = args[0]
            elif identity is None and isinstance(args[0], Mapping):
                identity = args[0]
        elif len(args) >= 2:
            if identity is None and isinstance(args[0], Mapping):
                identity = args[0]
            if message_id is None and isinstance(args[1], str):
                message_id = args[1]
        if message_id is None and "message_id" in kwargs:
            message_id = kwargs["message_id"]
        if message_id is None and "reply_receipt" in kwargs:
            message_id = kwargs["reply_receipt"]
        if identity is None and "identity" in kwargs:
            identity = kwargs["identity"]

        if not self.enabled:
            return self._ack_unavailable("absent", "owner_reply_port_disabled", message_id)
        if self._client is None or not callable(getattr(self._client, "request", None)):
            return self._ack_unavailable("unavailable", "owner_reply_port_unavailable", message_id)

        # Validate message_id
        if not isinstance(message_id, str) or _MESSAGE_ID_RE.fullmatch(message_id) is None:
            return self._ack_unavailable("error", "invalid_message_id", message_id)

        # Validate caller-supplied identity
        if identity is None or not isinstance(identity, Mapping):
            return self._ack_unavailable("error", "invalid_identity", message_id)

        if "agent_id" in identity and identity["agent_id"] != self._agent_id:
            return self._ack_unavailable("error", "identity_mismatch", message_id)
        if "harness" in identity or "host" in identity or "session_id" in identity:
            if (identity.get("harness") != self.harness
                    or identity.get("host") != self.host
                    or identity.get("session_id") != self.session_id):
                return self._ack_unavailable("error", "identity_mismatch", message_id)
        if "agent_id" not in identity and not ("harness" in identity and "session_id" in identity):
            return self._ack_unavailable("error", "invalid_identity", message_id)

        # Send existing ack RPC
        wire_identity = dict(self._wire_identity)
        try:
            response = self._client.request(ACK_OP, wire_identity, {"id": message_id})
        except Exception:  # noqa: BLE001 - fail-closed, no transport details leaked
            return self._ack_unavailable("unavailable", "office_remote_unavailable", message_id)

        if (not isinstance(response, dict)
                or response.get("status") != "ok"
                or response.get("code") != "sent"
                or response.get("agent") != self._agent_id
                or response.get("ack_of") != message_id
                or not isinstance(response.get("id"), str)
                or _MESSAGE_ID_RE.fullmatch(response["id"]) is None):
            return self._ack_unavailable("unavailable", "office_remote_unavailable", message_id)

        return {
            "status": "ok",
            "code": ACK_CODE,
            "agent": self._agent_id,
            "message_id": message_id,
            "confirmed": True,
        }

    def _ack_unavailable(self, status: str, code: str, message_id: str | None = None) -> dict[str, Any]:
        return {
            "status": status,
            "code": code,
            "agent": self._agent_id,
            "message_id": message_id if isinstance(message_id, str) else None,
            "confirmed": False,
        }

    @staticmethod
    def _validate_query(query: dict[str, Any]) -> str | None:
        slug = query["project_slug"]
        if not isinstance(slug, str) or re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", slug) is None:
            return "invalid_project_slug"
        parents = query["parent_receipts"]
        if not isinstance(parents, list) or not 1 <= len(parents) <= 64:
            return "invalid_parent_receipts"
        if any(not isinstance(item, str) or re.fullmatch(r"[0-9a-f]{32}", item) is None for item in parents):
            return "invalid_parent_receipt"
        if len(set(parents)) != len(parents):
            return "duplicate_parent_receipt"
        limit = query["limit"]
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            return "invalid_limit"
        return None

    def _unavailable(self, status: str, code: str) -> dict[str, Any]:
        return {
            "status": status,
            "code": code,
            "agent": getattr(self, "_agent_id", _IDENTITY["agent_id"]),
            "messages": [],
            "count": 0,
            "remaining": None,
            "complete": False,
            "actionable": False,
        }

    def _validate_response(self, response: Any, query: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(response, dict) or response.get("status") != "ok":
            return self._unavailable("unavailable", "office_remote_unavailable")
        expected_fields = {
            "status", "code", "agent", "project_slug", "parent_receipts", "messages", "count",
            "remaining", "remaining_matched", "complete", "kind", "peek", "cursor_advanced",
            "gap_detected", "skipped_malformed", "budget_stopped", "scan_budget_stopped",
            "response_budget_stopped", "scanned_bytes",
        }
        if (response.get("code") != CODE or response.get("agent") != self._agent_id
                or response.get("project_slug") != query["project_slug"]
                or response.get("parent_receipts") != query["parent_receipts"]
                or response.get("kind") != "handoff" or response.get("peek") is not True
                or response.get("cursor_advanced") is not False
                or set(response) != expected_fields):
            return self._unavailable("unavailable", "invalid_owner_reply_response")

        messages = response.get("messages")
        count = response.get("count")
        remaining = response.get("remaining")
        remaining_matched = response.get("remaining_matched")
        complete = response.get("complete")
        gap = response.get("gap_detected")
        skipped = response.get("skipped_malformed")
        budget_stopped = response.get("budget_stopped")
        scan_stopped = response.get("scan_budget_stopped")
        response_stopped = response.get("response_budget_stopped")
        scanned_bytes = response.get("scanned_bytes")
        if (not isinstance(messages, list) or len(messages) > query["limit"]
                or isinstance(count, bool) or not isinstance(count, int) or count != len(messages)
                or not isinstance(complete, bool)
                or not isinstance(gap, bool) or isinstance(skipped, bool) or not isinstance(skipped, int)
                or skipped < 0 or not isinstance(budget_stopped, bool)
                or not isinstance(scan_stopped, bool) or not isinstance(response_stopped, bool)
                or isinstance(scanned_bytes, bool) or not isinstance(scanned_bytes, int) or scanned_bytes < 0):
            return self._unavailable("unavailable", "invalid_owner_reply_response")

        if scan_stopped:
            if messages or count != 0 or remaining is not None or remaining_matched is not None:
                return self._unavailable("unavailable", "invalid_owner_reply_response")
        elif (isinstance(remaining, bool) or not isinstance(remaining, int) or remaining < 0
              or isinstance(remaining_matched, bool) or not isinstance(remaining_matched, int)
              or remaining_matched != remaining):
            return self._unavailable("unavailable", "invalid_owner_reply_response")

        normalized: list[dict[str, Any]] = []
        parent_set = set(query["parent_receipts"])
        allowed_fields = {
            "id", "ts", "from", "to", "kind", "body", "reply_to", "trust",
            "refs", "dedupe_key", "ack_of", "transport_principal",
        }
        for frame in messages:
            if (not isinstance(frame, dict) or set(frame) != {"untrusted_message", "handling"}
                    or frame.get("handling") != HANDLING_TEXT):
                return self._unavailable("unavailable", "invalid_owner_reply_response")
            envelope = frame.get("untrusted_message")
            if (not isinstance(envelope, dict) or not set(envelope) <= allowed_fields
                    or envelope.get("trust") != "untrusted"):
                return self._unavailable("unavailable", "invalid_owner_reply_response")
            try:
                parsed = parse_envelope(json.dumps(envelope, ensure_ascii=False, allow_nan=False))
            except (TypeError, ValueError, UnicodeEncodeError):
                parsed = None
            if (parsed is None or re.fullmatch(r"[0-9a-f]{32}", parsed["id"]) is None
                    or parsed["kind"] != "handoff" or parsed.get("reply_to") not in parent_set
                    or parsed["to"] != self._agent_id or parsed["trust"] != "untrusted"):
                return self._unavailable("unavailable", "invalid_owner_reply_response")
            normalized.append(frame_untrusted(parsed))

        if complete and (remaining != 0 or gap or skipped != 0 or budget_stopped
                         or scan_stopped or response_stopped):
            return self._unavailable("unavailable", "invalid_owner_reply_response")
        if budget_stopped != (scan_stopped or response_stopped):
            return self._unavailable("unavailable", "invalid_owner_reply_response")

        result = dict(response)
        result["messages"] = normalized
        result["status"] = "ok" if complete else "incomplete"
        result["actionable"] = bool(complete)
        return result
