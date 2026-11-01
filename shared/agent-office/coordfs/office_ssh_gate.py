"""Forced-command Agent Office transport. The SSH key selects the principal.

One bounded JSON line selects an operation; only MCP may use the remaining binary
stream. Neither the header nor its arguments are interpreted as a command line.
"""

from __future__ import annotations

import argparse
from contextlib import redirect_stderr
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, BinaryIO, Mapping

from .errors import CoordError
from .identity import _agent_component, agent_identity
from .office import DEFAULT_PRESENCE_TTL_S, DEFAULT_READ_LIMIT, MESSAGE_KINDS, Office, _bare_agent
from .office_mcp import ContractorLimits, OfficeMcpServer, TOOLS
from .wake import (
    EXTERNAL_RELAY_DRIVERS,
    MAX_LEDGER_LIMIT,
    RELAY_OBSERVATION_STAGES,
    RELAY_RECEIPT_STATUSES,
    Wake,
    _BASELINE_UNSET,
)

MAX_HEADER_BYTES = 64 * 1024
MAX_UNARY_RESPONSE_BYTES = 64 * 1024
DEFAULT_WAKE_LIST_LIMIT = 20
MAX_WAKE_LIST_LIMIT = 100
MAX_WAKE_LIST_OFFSET = 1_000_000
PRINCIPALS = frozenset({"ws-office", "hermes"})
# F2 (UAT ade7273f): the key-owned principal also bounds which harness names a
# caller may present, so a workstation key cannot mint look-alike ids of another
# principal (e.g. hermes-ailab-*). Names are compared after identity
# normalization; an unlisted harness is refused (fail-closed). Adding a new CLI
# to the workstation requires an explicit entry here (a test pins every harness
# with a session binding in identity.py and every runtime in projects.py). "unknown" is the documented default
# wire harness for clients without COORDFS_HARNESS; it groups them per host.
PRINCIPAL_HARNESSES = {
    "hermes": frozenset({"hermes"}),
    "ws-office": frozenset({
        "agent-graph", "antigravity", "claude", "claude-code", "codebuddy", "codex",
        "fleet", "gemini", "grok", "kimi", "kimi-code", "kimi-desktop", "mimo",
        "minimax", "muse", "office", "openclaw", "openclaw-local-qwen", "opencode",
        "qwen", "qwen-code", "qwen-local", "qwen-local-5070ti", "unknown", "zcode",
    }),
}
# Legacy bare ids carry no harness; they still cannot use another principal's prefix.
RESERVED_AGENT_PREFIXES = {"ws-office": ("hermes-",), "hermes": ()}
# W2 unary parity: OPS is the complete documented non-owner remote surface,
# including the closed tool-call and the six ordinary Wake reads/cancels of the
# local CLI. Owner and server operations (enroll, verify, intake, stop, start,
# tick, pull, relay expect/reconcile) stay outside the remote surface.
OPS = frozenset({
    "presence", "read", "read-correlated-replies", "send", "ack", "wait", "who", "channels", "delivery",
    "wake-expect", "wake-consume", "wake-close", "wake-relay-watch",
    "wake-relay-pending", "wake-relay-claim", "wake-relay-receipt",
    "wake-relay-observe", "wake-relay-defer-busy", "wake-list",
    "wake-task-cancel", "wake-correlations", "wake-pull-disarm",
    "wake-stop-status", "wake-ledger", "tool-call", "mcp",
    "wake-cli-pending", "wake-cli-claim", "wake-cli-start", "wake-cli-receipt",
    "wake-relay-native-begin", "wake-relay-native-unsubmitted",
    "wake-relay-ack",
    "wake-desktop-input-claim", "wake-desktop-input-release",
})
# The parity additions beyond the originally reviewed set, kept for audit.
PARITY_OPS = frozenset({
    "wake-list", "wake-task-cancel", "wake-correlations", "wake-pull-disarm",
    "wake-stop-status", "wake-ledger", "tool-call",
})
assert PARITY_OPS <= OPS
_ALLOWED_OPS = OPS
MCP_TOOL_NAMES = frozenset({
    "office_ask", "office_send", "office_read", "office_wait", "office_ack",
    "office_read_correlated_replies",
    "office_delivery", "office_dilemma_open", "office_dilemma_answer",
    "office_dilemma_status", "office_channels", "office_presence", "office_who",
    "office_thread_register", "office_thread_resolve", "office_thread_list",
    "office_project_view_import", "office_project_view_get", "office_project_view_list",
    "office_goal_add", "office_goal_update", "office_goal_get", "office_goal_list",
    "office_checkpoint",
    "office_project_register", "office_project_resolve", "office_project_stream",
    "office_project_activity", "office_project_activity_read", "office_project_audits",
    "office_project_send", "office_project_send_channel", "office_project_read",
    "office_project_read_channel", "office_project_wait", "office_project_wait_channel",
    "office_project_acknowledge", "office_project_delivery", "office_project_cancel",
    "office_journal_export", "office_journal_checkpoint", "office_journal_read_checkpoint",
    "office_journal_audit", "office_journal_reconcile", "office_work_register",
    "office_work_get", "office_work_list", "office_work_update", "office_work_blocker_add",
    "office_work_blocker_transition", "office_work_plan_record", "office_work_plan_list",
    "office_work_plan_acknowledge", "office_conflict_advise", "office_work_projection",
    "office_wake_expect", "office_wake_pull_arm", "office_wake_pull",
    "office_wake_pull_confirm", "office_wake_relay_defer_busy", "office_wake_close",
    "office_wake_consume",
})
_COMPONENT = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")


class GateReject(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _DiscardDiagnostics:
    """Keep Office diagnostics and tracebacks off the SSH protocol stream."""

    def write(self, value: str) -> int:
        return len(value)

    def flush(self) -> None:
        pass


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise GateReject("duplicate_key")
        result[key] = value
    return result


def _invalid_constant(_value: str) -> None:
    raise GateReject("invalid_json")


def _header(stdin: BinaryIO) -> tuple[str, dict[str, Any], str, dict[str, Any]]:
    line = stdin.readline(MAX_HEADER_BYTES + 2)
    if not line or not line.endswith(b"\n") or len(line[:-1].removesuffix(b"\r")) > MAX_HEADER_BYTES:
        raise GateReject("invalid_framing")
    try:
        data = json.loads(line.decode("utf-8", errors="strict"),
                          object_pairs_hook=_unique_pairs, parse_constant=_invalid_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise GateReject("invalid_json") from exc
    if not isinstance(data, dict) or set(data) != {"v", "op", "identity", "args"}:
        raise GateReject("invalid_header")
    if type(data["v"]) is not int or data["v"] != 1:
        raise GateReject("invalid_version")
    op = data["op"]
    if not isinstance(op, str) or op not in _ALLOWED_OPS:
        raise GateReject("unknown_op")
    identity = data["identity"]
    if not isinstance(identity, dict):
        raise GateReject("invalid_identity")
    if set(identity) == {"agent_id"}:
        agent = identity["agent_id"]
        if not isinstance(agent, str):
            raise GateReject("invalid_identity")
        try:
            agent_id = _bare_agent(agent)
        except CoordError as exc:
            raise GateReject("invalid_identity") from exc
    elif set(identity) in ({"harness", "session_id"}, {"harness", "host", "session_id"}):
        if any(not isinstance(value, str) or _COMPONENT.fullmatch(value) is None
               for value in identity.values()):
            raise GateReject("invalid_identity")
        try:
            agent_id = agent_identity(identity["harness"], host=identity.get("host"),
                                      session_id=identity["session_id"], env={})["agent_id"]
        except CoordError as exc:
            raise GateReject("invalid_identity") from exc
    else:
        raise GateReject("invalid_identity")
    args = data["args"]
    if not isinstance(args, dict):
        raise GateReject("invalid_args")
    return op, args, agent_id, identity


def _args(args: dict[str, Any], required: dict[str, type], optional: dict[str, type] | None = None
          ) -> dict[str, Any]:
    allowed = required | (optional or {})
    if not set(required) <= set(args) or not set(args) <= set(allowed):
        raise GateReject("invalid_args")
    for key, value in args.items():
        expected = allowed[key]
        if expected is int:
            valid = type(value) is int
        elif expected is float:
            valid = type(value) in (int, float)
        elif expected is list:
            valid = isinstance(value, list) and all(isinstance(item, str) for item in value)
        else:
            valid = isinstance(value, expected)
        if not valid:
            raise GateReject("invalid_args")
    return args


def _range(value: int | float, low: int | float, high: int | float) -> None:
    if not low <= value <= high:
        raise GateReject("invalid_args")


def _mcp_options(args: dict[str, Any]) -> dict[str, Any]:
    a = _args(args, {}, {"profile": str, "channels": list, "peers": list,
                         "heartbeat_ttl_s": int, "contractor_limits": dict})
    profile = a.get("profile", "full")
    if profile not in {"full", "contractor"}:
        raise GateReject("invalid_args")
    channels = a.get("channels", [])
    peers = a.get("peers", [])
    if len(channels) > 16 or len(peers) > 16:
        raise GateReject("invalid_args")
    if profile == "full" and (channels or peers or "contractor_limits" in a):
        raise GateReject("invalid_args")
    if profile == "contractor" and not channels:
        raise GateReject("invalid_args")
    limits = None
    if "contractor_limits" in a:
        raw = a["contractor_limits"]
        if not set(raw) <= set(ContractorLimits._fields):
            raise GateReject("invalid_args")
        try:
            limits = ContractorLimits(**raw)
        except (TypeError, ValueError) as exc:
            raise GateReject("invalid_args") from exc
    ttl = a.get("heartbeat_ttl_s", DEFAULT_PRESENCE_TTL_S)
    _range(ttl, 0, 86400)
    return {"profile": profile, "channels": channels, "peers": peers,
            "contractor_limits": limits, "heartbeat_ttl_s": ttl or None}


def _dispatch(op: str, args: dict[str, Any], office: Office,
              peer_host: str | None = None) -> dict[str, Any]:
    if op in {"wake-cli-pending", "wake-cli-claim", "wake-cli-start", "wake-cli-receipt"}:
        if peer_host is None or office.transport_principal != "ws-office":
            raise GateReject("peer_host_required")
        dispatches = Wake(home=office.home).cli_dispatches()
        caller = office.agent_id
        if op == "wake-cli-pending":
            a = _args(args, {}, {"limit": int})
            return {"status": "ok", "rows": dispatches.pending(
                peer_host=peer_host, executor_agent=caller, limit=a.get("limit", 20))}
        if op == "wake-cli-claim":
            a = _args(args, {"dispatch_id": str, "request_id": str})
            return dispatches.claim(a["dispatch_id"], peer_host=peer_host,
                                    executor_agent=caller, request_id=a["request_id"])
        if op == "wake-cli-start":
            a = _args(args, {"dispatch_id": str, "lease_id": str, "request_id": str})
            return dispatches.start(a["dispatch_id"], peer_host=peer_host,
                                    executor_agent=caller, lease_id=a["lease_id"],
                                    request_id=a["request_id"])
        a = _args(args, {"dispatch_id": str, "lease_id": str, "status": str},
                  {"exit_code": int})
        return dispatches.receipt(a["dispatch_id"], peer_host=peer_host,
                                  executor_agent=caller, lease_id=a["lease_id"],
                                  status=a["status"], exit_code=a.get("exit_code"))
    # Every branch has its own closed argument schema. In particular no caller-supplied
    # sender, principal, owner confirmation, home or arbitrary method name reaches an API.
    if op == "presence":
        a = _args(args, {}, {"ttl_s": int, "status": str, "workspace": str, "release": bool})
        _range(a.get("ttl_s", DEFAULT_PRESENCE_TTL_S), 1, 86400)
        return office.leave() if a.get("release", False) else office.presence(
            ttl_s=a.get("ttl_s", DEFAULT_PRESENCE_TTL_S), status=a.get("status"),
            workspace=a.get("workspace"))
    if op == "read":
        a = _args(args, {}, {"limit": int, "peek": bool, "channel": str, "kind": str})
        _range(a.get("limit", DEFAULT_READ_LIMIT), 1, 500)
        if "kind" in a and a["kind"] not in MESSAGE_KINDS:
            raise GateReject("invalid_args")
        return office.read(limit=a.get("limit", DEFAULT_READ_LIMIT), peek=a.get("peek", False),
                           channel=a.get("channel"), kind=a.get("kind"),
                           max_response_bytes=MAX_UNARY_RESPONSE_BYTES)
    if op == "read-correlated-replies":
        a = _args(args, {"project_slug": str, "parent_receipts": list}, {"limit": int})
        _range(a.get("limit", 5), 1, 50)
        return office.read_correlated_replies(
            a["project_slug"], a["parent_receipts"], limit=a.get("limit", 5)
        )
    if op == "send":
        a = _args(args, {"body": str}, {"to": str, "channel": str, "kind": str,
                                       "reply_to": str, "refs": list, "dedupe_key": str,
                                       "new_recipient": bool})
        if "kind" in a and a["kind"] not in MESSAGE_KINDS:
            raise GateReject("invalid_args")
        return office.send(a.get("to"), a["body"], kind=a.get("kind"),
                           reply_to=a.get("reply_to"), channel=a.get("channel"),
                           refs=a.get("refs"), dedupe_key=a.get("dedupe_key"),
                           new_recipient=a.get("new_recipient", False))
    if op == "ack":
        a = _args(args, {"id": str}, {"note": str})
        return office.ack(a["id"], note=a.get("note"))
    if op == "wait":
        a = _args(args, {}, {"timeout_s": float, "channel": str})
        _range(a.get("timeout_s", 30), 0, 240)
        return office.wait(timeout_s=a.get("timeout_s", 30), channel=a.get("channel"))
    if op == "who":
        a = _args(args, {}, {"workspace": str})
        return office.who(workspace=a.get("workspace"))
    if op == "channels":
        _args(args, {})
        return office.channels()
    if op == "delivery":
        a = _args(args, {"id": str, "channel": str})
        return office.delivery(a["id"], channel=a["channel"])
    if op == "wake-expect":
        a = _args(args, {"binding_id": str, "task_id": str, "correlation_id": str,
                         "counterpart": str, "request_message_id": str},
                  {"max_wakes": int, "max_hops": int, "ttl_s": int})
        for key, default, high in (("max_wakes", 6, 100), ("max_hops", 12, 100),
                                   ("ttl_s", 10800, 86400)):
            _range(a.get(key, default), 1, high)
        return Wake(home=office.home).expect(binding_id=a["binding_id"], caller=office.agent_id,
                                             task_id=a["task_id"], correlation_id=a["correlation_id"],
                                             counterpart=a["counterpart"],
                                             request_message_id=a["request_message_id"],
                                             max_wakes=a.get("max_wakes", 6), max_hops=a.get("max_hops", 12),
                                             ttl_s=a.get("ttl_s", 10800))
    if op == "wake-consume":
        a = _args(args, {"correlation_id": str, "trigger_message_id": str})
        return Wake(home=office.home).correlations.consume(
            a["correlation_id"], caller=office.agent_id,
            trigger_message_id=a["trigger_message_id"])
    if op == "wake-close":
        a = _args(args, {"correlation_id": str})
        return Wake(home=office.home).correlations.close(a["correlation_id"], caller=office.agent_id)
    if op in {"wake-desktop-input-claim", "wake-desktop-input-release"}:
        from .leases import CoordBoard, lease_digest, parse_ts
        from .wake import ZCODE_RELAY_DRIVER
        if op == "wake-desktop-input-claim":
            a = _args(args, {"binding_id": str}, {"ttl_s": int})
            _range(a.get("ttl_s", 180), 1, 180)
        else:
            a = _args(args, {"binding_id": str, "expected_lease_digest": str})
            if re.fullmatch(r"[0-9a-f]{64}", a["expected_lease_digest"]) is None:
                raise GateReject("invalid_args")
        wake = Wake(home=office.home)
        binding = wake._relay_binding_for_driver(a["binding_id"], ZCODE_RELAY_DRIVER)
        actor = office.agent_id
        if (re.fullmatch(r"codex-ailab-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", actor) is None
                or actor not in binding["allowed_wakers"]):
            raise GateReject("identity_mismatch")
        board = CoordBoard(home=office.home)
        if op == "wake-desktop-input-release":
            return board.release("data:desktop-input", actor, kind="data",
                                 expected_lease_digest=a["expected_lease_digest"],
                                 expected_meta={"binding_id": a["binding_id"], "actor": actor})
        if (binding["status"] != "active" or binding["verified"] is not True
                or wake.now() >= parse_ts(binding["expires_at"]) or wake._stop_is_present()):
            raise GateReject("binding_unavailable")
        result = board.claim("data:desktop-input", actor, kind="data", ttl_s=a.get("ttl_s", 180),
                             meta={"binding_id": a["binding_id"], "actor": actor})
        lease = result.get("lease")
        if result.get("status") == "ok" and isinstance(lease, dict):
            if (lease.get("meta") != {"binding_id": a["binding_id"], "actor": actor}
                    or not 1 <= lease.get("ttl_s", 0) <= 180):
                return {"status": "refused", "code": "desktop_input_foreign_binding"}
            result["lease_digest"] = lease_digest(lease)
        return result
    if op == "wake-relay-watch":
        a = _args(args, {"binding_id": list}, {"interval_s": int, "timeout_s": int,
                                               "delay_s": int, "driver": str})
        if not 1 <= len(a["binding_id"]) <= 16:
            raise GateReject("invalid_args")
        _range(a.get("interval_s", 20), 1, 240)
        _range(a.get("timeout_s", 3600), 1, 3600)
        _range(a.get("delay_s", 0), 0, 3600)
        driver = a.get("driver", "claude-desktop-relay")
        if driver not in EXTERNAL_RELAY_DRIVERS:
            raise GateReject("invalid_args")
        return Wake(home=office.home).relay_watch(binding_id=a["binding_id"],
                                                  interval_s=a.get("interval_s", 20),
                                                  timeout_s=a.get("timeout_s", 3600),
                                                  delay_s=a.get("delay_s", 0),
                                                  driver=driver)
    if op == "wake-relay-pending":
        a = _args(args, {}, {"binding_id": str, "driver": str, "include_owned": bool,
                             "relay_agent": str})
        driver = a.get("driver", "claude-desktop-relay")
        if driver not in EXTERNAL_RELAY_DRIVERS:
            raise GateReject("invalid_args")
        include_owned = a.get("include_owned", False)
        if include_owned:
            if "binding_id" not in a or "relay_agent" not in a:
                raise GateReject("invalid_args")
            if a["relay_agent"] != office.agent_id:
                raise GateReject("identity_mismatch")
            if driver != "codex-desktop-relay":
                raise GateReject("invalid_args")
        return Wake(home=office.home).relay_pending(
            binding_id=a.get("binding_id"), driver=driver,
            include_owned=include_owned,
            relay_agent=a.get("relay_agent"),
        )
    if op == "wake-relay-claim":
        a = _args(args, {"dispatch_id": str, "relay_agent": str}, {"driver": str})
        if a["relay_agent"] != office.agent_id:
            raise GateReject("identity_mismatch")
        driver = a.get("driver", "claude-desktop-relay")
        if driver not in EXTERNAL_RELAY_DRIVERS:
            raise GateReject("invalid_args")
        return Wake(home=office.home).relay_claim(a["dispatch_id"], relay_agent=office.agent_id, driver=driver)
    if op == "wake-relay-receipt":
        a = _args(args, {"dispatch_id": str, "status": str},
                  {"message_id": str, "turn_id": str, "detail": str, "driver": str})
        if a["status"] not in RELAY_RECEIPT_STATUSES:
            raise GateReject("invalid_args")
        driver = a.get("driver", "claude-desktop-relay")
        if driver not in EXTERNAL_RELAY_DRIVERS:
            raise GateReject("invalid_args")
        return Wake(home=office.home).relay_receipt(a["dispatch_id"], status=a["status"],
                                                    message_id=a.get("message_id"),
                                                    turn_id=a.get("turn_id"),
                                                    detail=a.get("detail"), driver=driver,
                                                    relay_agent=office.agent_id)
    if op == "wake-relay-observe":
        a = _args(args, {"dispatch_id": str, "stage": str}, {"driver": str, "native_proof": dict})
        if a["stage"] not in RELAY_OBSERVATION_STAGES:
            raise GateReject("invalid_args")
        driver = a.get("driver", "claude-desktop-relay")
        if driver not in EXTERNAL_RELAY_DRIVERS:
            raise GateReject("invalid_args")
        return Wake(home=office.home).relay_observe(
            a["dispatch_id"], stage=a["stage"], driver=driver,
            relay_agent=office.agent_id,
            native_proof=a.get("native_proof"),
        )
    if op == "wake-relay-ack":
        a = _args(args, {"dispatch_id": str, "driver": str}, {})
        if a["driver"] == "antigravity-desktop-relay":
            from .codex_ack import owned_antigravity_ack
            return owned_antigravity_ack(office.home, a["dispatch_id"], office.agent_id)
        if a["driver"] == "zcode-desktop-relay":
            from .codex_ack import owned_zcode_reply
            return owned_zcode_reply(office.home, a["dispatch_id"], office.agent_id)
        if a["driver"] != "codex-desktop-relay":
            raise GateReject("invalid_args")
        from .codex_ack import owned_codex_ack
        return owned_codex_ack(office.home, a["dispatch_id"], office.agent_id)
    if op == "wake-relay-defer-busy":
        a = _args(args, {"dispatch_id": str}, {"driver": str})
        driver = a.get("driver", "claude-desktop-relay")
        if driver not in EXTERNAL_RELAY_DRIVERS:
            raise GateReject("invalid_args")
        return Wake(home=office.home).relay_defer_busy(a["dispatch_id"], driver=driver)
    if op == "wake-relay-native-begin":
        a = _args(args, {"dispatch_id": str, "relay_agent": str},
                  {"driver": str, "prior_turn_id": (str, type(None)), "baseline_observed_at": float})
        if a["relay_agent"] != office.agent_id:
            raise GateReject("identity_mismatch")
        driver = a.get("driver", "claude-desktop-relay")
        if driver not in EXTERNAL_RELAY_DRIVERS:
            raise GateReject("invalid_args")
        prior_turn_id = a.get("prior_turn_id", _BASELINE_UNSET)
        baseline_observed_at = a.get("baseline_observed_at", _BASELINE_UNSET)
        if "prior_turn_id" in a and a["prior_turn_id"] is not None:
            prior_turn_id = a["prior_turn_id"]
        return Wake(home=office.home).relay_native_begin(
            a["dispatch_id"], relay_agent=office.agent_id, driver=driver,
            prior_turn_id=prior_turn_id,
            baseline_observed_at=baseline_observed_at,
        )
    if op == "wake-relay-native-unsubmitted":
        a = _args(args, {"dispatch_id": str, "relay_agent": str}, {"driver": str})
        if a["relay_agent"] != office.agent_id:
            raise GateReject("identity_mismatch")
        driver = a.get("driver", "claude-desktop-relay")
        if driver not in EXTERNAL_RELAY_DRIVERS:
            raise GateReject("invalid_args")
        return Wake(home=office.home).relay_native_unsubmitted(a["dispatch_id"],
                                                               relay_agent=office.agent_id,
                                                               driver=driver)
    if op == "wake-list":
        a = _args(args, {}, {"limit": int, "offset": int})
        limit = a.get("limit", DEFAULT_WAKE_LIST_LIMIT)
        offset = a.get("offset", 0)
        _range(limit, 1, MAX_WAKE_LIST_LIMIT)
        _range(offset, 0, MAX_WAKE_LIST_OFFSET)
        return Wake(home=office.home).list_bindings(limit=limit, offset=offset)
    if op == "wake-task-cancel":
        a = _args(args, {"task_id": str})
        return Wake(home=office.home).correlations.cancel_task(a["task_id"],
                                                               caller=office.agent_id)
    if op == "wake-correlations":
        _args(args, {})
        return Wake(home=office.home).correlations.list()
    if op == "wake-pull-disarm":
        a = _args(args, {"binding_id": str})
        return Wake(home=office.home).pull_disarm(binding_id=a["binding_id"],
                                                  caller=office.agent_id)
    if op == "wake-stop-status":
        _args(args, {})
        return Wake(home=office.home).stop_status()
    if op == "wake-ledger":
        a = _args(args, {}, {"limit": int, "binding_id": str})
        _range(a.get("limit", 50), 1, MAX_LEDGER_LIMIT)
        return Wake(home=office.home).ledger(limit=a.get("limit", 50),
                                             binding_id=a.get("binding_id"))
    if op == "tool-call":
        a = _args(args, {"name": str, "arguments": dict})
        if a["name"] not in MCP_TOOL_NAMES:
            raise GateReject("unknown_tool")
        # The same exact inventory as the reviewed mcp stream: a new tool must be
        # explicitly assessed before it can cross the SSH boundary in unary form.
        if {tool["name"] for tool in TOOLS} != MCP_TOOL_NAMES:
            raise GateReject("mcp_tools_refused")
        # The server owns the transport principal, sender checks and profile; the
        # caller supplies only a static tool name and its closed arguments object,
        # never a principal, owner confirmation, home or arbitrary method name.
        return OfficeMcpServer(
            office, response_budget_bytes=MAX_UNARY_RESPONSE_BYTES
        ).call_tool(a["name"], a["arguments"])
    raise GateReject("unknown_op")


def _emit(stdout: BinaryIO, result: dict[str, Any]) -> None:
    stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
    stdout.flush()


def _unary_budgeted_response(op: str, args: Mapping[str, Any]) -> bool:
    if op in {"read", "read-correlated-replies", "wake-list"}:
        return True
    if op != "tool-call":
        return False
    return args.get("name") in {
        "office_read", "office_read_correlated_replies", "office_project_read", "office_project_read_channel",
    }


def _response_line_size(result: Mapping[str, Any]) -> int:
    return len(json.dumps(result, ensure_ascii=False, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")) + 1


def run(stdin: BinaryIO, stdout: BinaryIO, *, principal: str, home: Path | None = None,
        environ: Mapping[str, str] | None = None, peer_host: str | None = None) -> int:
    """Handle a connection with key-owned principal and optional host binding.

    The production entrypoint requires a host binding. The absent binding is
    retained only for direct, legacy in-process callers, never an RPC option.
    """
    try:
        if "SSH_ORIGINAL_COMMAND" in (os.environ if environ is None else environ):
            raise GateReject("ssh_command_refused")
        if principal not in PRINCIPALS:
            raise GateReject("unknown_principal")
        if peer_host is not None and (
            not isinstance(peer_host, str) or _COMPONENT.fullmatch(peer_host) is None
        ):
            raise GateReject("invalid_peer_host")
        op, args, agent_id, identity = _header(stdin)
        if "agent_id" in identity:
            if agent_id.startswith(RESERVED_AGENT_PREFIXES[principal]):
                raise GateReject("identity_mismatch")
        else:
            try:
                harness = _agent_component(identity["harness"], "harness")
            except CoordError:
                raise GateReject("harness_not_allowed") from None
            if harness not in PRINCIPAL_HARNESSES[principal]:
                raise GateReject("harness_not_allowed")
        if peer_host is not None:
            # A key may host several CLI harnesses/sessions, but cannot claim
            # another machine. Bare legacy IDs lack verifiable host metadata.
            if "agent_id" in identity or identity.get("host", peer_host) != peer_host:
                raise GateReject("peer_host_mismatch")
            identity = {**identity, "host": peer_host}
            agent_id = agent_identity(identity["harness"], host=peer_host,
                                      session_id=identity["session_id"], env={})["agent_id"]
        if principal == "hermes" and not agent_id.startswith("hermes-"):
            raise GateReject("identity_mismatch")
        if op == "mcp":
            mcp_options = _mcp_options(args)
        else:
            # A second JSON line, binary tail, or any other trailing byte is a refusal.
            # Check this before creating Office, so an invalid request cannot mutate home.
            if stdin.read(1):
                raise GateReject("trailing_data")
        if "agent_id" in identity:
            office = Office(agent_id=agent_id, home=home, transport_principal=principal)
        else:
            office = Office(harness=identity["harness"], host=identity.get("host"),
                            session_id=identity["session_id"], home=home,
                            transport_principal=principal)
        if op == "mcp":
            # Exact tool inventory is the reviewed remote surface. A new tool must
            # be explicitly assessed before it can cross the SSH boundary.
            if {tool["name"] for tool in TOOLS} != MCP_TOOL_NAMES:
                raise GateReject("mcp_tools_refused")
            with redirect_stderr(_DiscardDiagnostics()):
                OfficeMcpServer(office, **mcp_options).serve(stdin, stdout)
            return 0
        with redirect_stderr(_DiscardDiagnostics()):
            result = _dispatch(op, args, office, peer_host=peer_host)
        if _unary_budgeted_response(op, args) and _response_line_size(result) > MAX_UNARY_RESPONSE_BYTES:
            result = {"status": "error", "code": "response_too_large"}
        if result.get("status") in {"error", "degraded"}:
            raw_code = result.get("code")
            code = raw_code if isinstance(raw_code, str) and re.fullmatch(r"[a-z0-9_]{1,64}", raw_code) else "office_error"
            _emit(stdout, {"status": result["status"], "code": code})
            return 1 if result["status"] == "error" else 0
        _emit(stdout, result)
        return 0
    except GateReject as exc:
        _emit(stdout, {"status": "error", "code": exc.code})
    except CoordError as exc:
        code = exc.code if re.fullmatch(r"[a-z0-9_]{1,64}", exc.code) else "office_error"
        _emit(stdout, {"status": "error", "code": code})
    except Exception:
        _emit(stdout, {"status": "error", "code": "internal_error"})
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="office-ssh-gate")
    parser.add_argument("--principal", required=True)
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument("--peer-host", required=True,
                        help="canonical, unique logical host label pinned by this SSH key")
    args = parser.parse_args(argv)
    return run(sys.stdin.buffer, sys.stdout.buffer, principal=args.principal, home=args.home,
               peer_host=args.peer_host)


if __name__ == "__main__":
    raise SystemExit(main())
