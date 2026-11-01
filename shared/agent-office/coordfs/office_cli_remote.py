"""Remote dispatch half of the Agent Office CLI.

``office_cli`` owns parsing and the local implementation.  The parent CLI calls
``dispatch_remote`` before it constructs an ``Office`` when ``--remote-config``
is present, so this module must never fall through to a local store.  The remote
gate is a deliberately closed protocol: direct operations are named below and
the remaining public operations cross it through the reviewed ``tool-call``
bridge.
"""

from __future__ import annotations

import json
import math
import os
from os import stat as _config_stat
import stat
import sys
from dataclasses import fields
from pathlib import Path
from typing import Any

from . import identity
from .errors import CoordError
from .office_remote import OfficeRemoteClient, OfficeRemoteConfig, require_native_connection_paths


class CliHostRemoteAdapter:
    """Closed B5 unary adapter; SSH key context, never RPC fields, owns the actor."""

    def __init__(self, client: OfficeRemoteClient, *, execution_host: str,
                 executor_agent: str, harness: str, session_id: str) -> None:
        claimed = identity.agent_identity(harness, host=execution_host,
                                          session_id=session_id, env={})["agent_id"]
        if claimed != executor_agent:
            raise CoordError("host actor does not match pinned enrollment", code="invalid_host_actor")
        self.client = client
        self.host = execution_host
        self.actor = executor_agent
        self.identity = {"harness": harness, "host": execution_host, "session_id": session_id}

    def _identity(self, host: str, actor: str) -> None:
        if host != self.host or actor != self.actor:
            raise CoordError("host actor mismatch", code="invalid_host_actor")

    def _request(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        result = self.client.request(op, self.identity, args)
        if not isinstance(result, dict) or result.get("status") in ("error", "degraded"):
            raise CoordError("host dispatch transport unavailable", code="cli_host_transport")
        return result

    def pending(self, *, peer_host: str, executor_agent: str, limit: int = 20) -> list[dict[str, Any]]:
        self._identity(peer_host, executor_agent)
        result = self._request("wake-cli-pending", {"limit": limit})
        rows = result.get("rows")
        if result.get("status") != "ok" or not isinstance(rows, list):
            raise CoordError("invalid host pending response", code="cli_host_transport")
        return rows

    def claim(self, dispatch_id: str, *, peer_host: str, executor_agent: str,
              request_id: str) -> dict[str, Any]:
        self._identity(peer_host, executor_agent)
        return self._request("wake-cli-claim", {"dispatch_id": dispatch_id,
                                                 "request_id": request_id})

    def start(self, dispatch_id: str, *, peer_host: str, executor_agent: str,
              lease_id: str, request_id: str) -> dict[str, Any]:
        self._identity(peer_host, executor_agent)
        return self._request("wake-cli-start", {"dispatch_id": dispatch_id,
                                                 "lease_id": lease_id,
                                                 "request_id": request_id})

    def receipt(self, dispatch_id: str, *, peer_host: str, executor_agent: str,
                lease_id: str, status: str, exit_code: int | None = None) -> dict[str, Any]:
        self._identity(peer_host, executor_agent)
        args: dict[str, Any] = {"dispatch_id": dispatch_id, "lease_id": lease_id,
                                "status": status}
        if exit_code is not None:
            args["exit_code"] = exit_code
        return self._request("wake-cli-receipt", args)


MAX_REMOTE_CONFIG_BYTES = 16 * 1024
MAX_REMOTE_CONFIG_STRING_CHARS = 4096

# These routes are implemented as closed protocol operations by the SSH gate.
# The six Wake operations are added by the parallel protocol change; keeping
# them explicit here avoids accidentally sending them to a local Wake store.
DIRECT_ROUTES = frozenset(
    {
        "presence",
        "read",
        "read-correlated-replies",
        "send",
        "ack",
        "wait",
        "who",
        "channels",
        "delivery",
        "wake-expect",
        "wake-consume",
        "wake-close",
        "wake-relay-watch",
        "wake-relay-pending",
        "wake-relay-claim",
        "wake-relay-ack",
        "wake-relay-receipt",
        "wake-relay-observe",
        "wake-relay-defer-busy",
        "wake-relay-native-begin",
        "wake-relay-native-unsubmitted",
    }
)

NEW_WAKE_DIRECT_ROUTES = frozenset(
    {
        "wake-list",
        "wake-task-cancel",
        "wake-correlations",
        "wake-pull-disarm",
        "wake-stop-status",
        "wake-ledger",
    }
)

# These commands need an owner-controlled server or a server-local process.
# Refusing them before loading configuration or resolving stdin/files is
# intentional: remote mode cannot be an indirect owner administration path.
LOCAL_ONLY_ROUTES = frozenset(
    {
        "wake-relay-verify",
        "wake-enroll",
        "wake-verify",
        "wake-pause",
        "wake-revoke",
        "wake-intake",
        "wake-stop",
        "wake-start",
        "wake-tick",
        "wake-relay-expect",
        "wake-monitor",
        "wake-relay-reconcile",
        "wake-schedule-add",
        "wake-schedule-list",
        "wake-schedule-disable",
        "wake-schedule-enable",
        "knowledge-sync",
    }
)

# Exact public MCP names, rather than a name transformation, make every
# cross-host operation reviewable and fail closed when the local CLI grows.
MCP_TOOL_ROUTES: dict[str, str] = {
    "thread-register": "office_thread_register",
    "thread-resolve": "office_thread_resolve",
    "thread-list": "office_thread_list",
    "project-view-import": "office_project_view_import",
    "project-view-get": "office_project_view_get",
    "project-view-list": "office_project_view_list",
    "goal-add": "office_goal_add",
    "goal-update": "office_goal_update",
    "goal-get": "office_goal_get",
    "goal-list": "office_goal_list",
    "checkpoint": "office_checkpoint",
    "dilemma-open": "office_dilemma_open",
    "dilemma-answer": "office_dilemma_answer",
    "dilemma-status": "office_dilemma_status",
    "ask": "office_ask",
    "wake-pull-arm": "office_wake_pull_arm",
    "wake-pull": "office_wake_pull",
    "wake-pull-confirm": "office_wake_pull_confirm",
    "project-register": "office_project_register",
    "project-resolve": "office_project_resolve",
    "project-stream": "office_project_stream",
    "project-activity": "office_project_activity",
    "project-activity-read": "office_project_activity_read",
    "project-audits": "office_project_audits",
    "project-send": "office_project_send",
    "project-send-channel": "office_project_send_channel",
    "project-read": "office_project_read",
    "project-read-channel": "office_project_read_channel",
    "project-wait": "office_project_wait",
    "project-wait-channel": "office_project_wait_channel",
    "project-acknowledge": "office_project_acknowledge",
    "project-delivery": "office_project_delivery",
    "project-cancel": "office_project_cancel",
    "journal-export": "office_journal_export",
    "journal-checkpoint": "office_journal_checkpoint",
    "journal-read-checkpoint": "office_journal_read_checkpoint",
    "journal-audit": "office_journal_audit",
    "journal-reconcile": "office_journal_reconcile",
    "work-register": "office_work_register",
    "work-get": "office_work_get",
    "work-list": "office_work_list",
    "work-update": "office_work_update",
    "work-blocker-add": "office_work_blocker_add",
    "work-blocker-transition": "office_work_blocker_transition",
    "work-plan-record": "office_work_plan_record",
    "work-plan-list": "office_work_plan_list",
    "work-plan-acknowledge": "office_work_plan_acknowledge",
    "conflict-advise": "office_conflict_advise",
    "work-projection": "office_work_projection",
}

ROUTE_CLASSIFICATIONS: dict[str, str] = {
    **{command: "direct" for command in DIRECT_ROUTES},
    **{command: "direct-wake" for command in NEW_WAKE_DIRECT_ROUTES},
    **{command: "local-only" for command in LOCAL_ONLY_ROUTES},
    **{command: "tool-call" for command in MCP_TOOL_ROUTES},
    "mcp": "mcp-relay",
}


def _invalid_config() -> CoordError:
    # Never include parser details, paths, or the original JSON text: a config
    # often names credentials even though this module never reads key material.
    return CoordError("invalid remote configuration", code="invalid_remote_config")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if not isinstance(key, str) or key in value:
            raise ValueError("invalid object")
        value[key] = item
    return value


def _invalid_constant(_value: str) -> None:
    raise ValueError("invalid JSON constant")


def load_remote_config(value: object) -> OfficeRemoteConfig:
    """Load only bounded JSON fields accepted by ``OfficeRemoteConfig``.

    Paths are opaque configuration strings.  In particular this function does
    not stat or open ``identity_file``/``known_hosts_file`` and does not apply
    environment substitutions.
    """

    if not isinstance(value, (str, Path)):
        raise _invalid_config()
    # A configured pipe/device can block before the bounded JSON read starts.
    # Check only the explicit config file; credential path values stay opaque.
    config_path = str(value).replace("/", "\\")
    if config_path.startswith(("\\\\.\\", "\\\\?\\")):
        raise _invalid_config()
    try:
        if not stat.S_ISREG(_config_stat(value).st_mode):
            raise _invalid_config()
        with Path(value).open("rb") as stream:
            raw = stream.read(MAX_REMOTE_CONFIG_BYTES + 1)
    except (OSError, TypeError, ValueError):
        raise _invalid_config() from None
    return parse_remote_config_bytes(raw)


def parse_remote_config_bytes(raw: bytes) -> OfficeRemoteConfig:
    """Validate connection data without reading credential paths or the network."""
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_REMOTE_CONFIG_BYTES:
        raise _invalid_config()
    try:
        decoded = raw.decode("utf-8")
        data = json.loads(
            decoded,
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
        raise _invalid_config() from None
    if not isinstance(data, dict):
        raise _invalid_config()

    names = {field.name for field in fields(OfficeRemoteConfig)}
    required = {"host", "user", "identity_file", "known_hosts_file", "ssh_executable"}
    if set(data) - names or not required.issubset(data):
        raise _invalid_config()
    for name in ("host", "user", "identity_file", "known_hosts_file", "ssh_executable"):
        if name in data and (
            not isinstance(data[name], str) or len(data[name]) > MAX_REMOTE_CONFIG_STRING_CHARS
        ):
            raise _invalid_config()
    for name in ("port", "connect_timeout_s"):
        if name in data and (isinstance(data[name], bool) or not isinstance(data[name], int)):
            raise _invalid_config()
    if "request_timeout_s" in data and (
        isinstance(data["request_timeout_s"], bool)
        or not isinstance(data["request_timeout_s"], (int, float))
        or not math.isfinite(data["request_timeout_s"])
    ):
        raise _invalid_config()
    try:
        config = OfficeRemoteConfig(**data)
        require_native_connection_paths(config)
        return config
    except (TypeError, ValueError):
        raise _invalid_config() from None


def _remote_identity(args: Any) -> dict[str, str]:
    explicit = getattr(args, "agent", None)
    if explicit is not None:
        # Remote Office refuses bare agent ids because they carry no host proof.
        # Re-express only an explicit canonical id for this runtime's harness and
        # strong session as structured claims; the server still binds its host
        # component to the authenticated SSH peer. Other explicit ids remain
        # bare and are rejected by that gate.
        harness = getattr(args, "harness", None) or os.environ.get("COORDFS_HARNESS") or "unknown"
        native = identity.agent_identity(harness, session_id=getattr(args, "session_id", None))
        if native.get("strength") == "strong" and isinstance(explicit, str):
            prefix = f"{native['harness']}-"
            suffix = f"-{native['session']}"
            if explicit.startswith(prefix) and explicit.endswith(suffix):
                host = explicit[len(prefix):len(explicit) - len(suffix)]
                try:
                    canonical_host = identity._agent_component(host, "host")
                except CoordError:
                    canonical_host = None
                if host and host == canonical_host and explicit == (
                    f"{native['harness']}-{canonical_host}-{native['session']}"
                ):
                    return {
                        "harness": native["harness"],
                        "host": canonical_host,
                        "session_id": native["session"],
                    }
        return {"agent_id": explicit}
    harness = getattr(args, "harness", None) or os.environ.get("COORDFS_HARNESS") or "unknown"
    native = identity.agent_identity(harness, session_id=getattr(args, "session_id", None))
    return {
        "harness": native["harness"],
        "host": native["host"],
        "session_id": native["session"],
    }


def _without_none(**values: Any) -> dict[str, Any]:
    return {name: value for name, value in values.items() if value is not None}


def _sender(args: Any) -> dict[str, Any]:
    """Pass explicit project sender claims verbatim for gate-side enforcement."""

    return _without_none(
        harness=getattr(args, "sender_harness", None),
        host=getattr(args, "sender_host", None),
        session_id=getattr(args, "sender_session", None),
    )


def _resolve_body(args: Any) -> str:
    # Import lazily to avoid a CLI/remote dispatch import cycle.  This is the
    # existing one-read, mutual-exclusion validation path.
    from .office_cli import _resolve_body as resolve_body

    return resolve_body(args)


def _read_dilemma_question(args: Any) -> Any:
    if not bool(getattr(args, "body_stdin", False)):
        return getattr(args, "question", None)
    from .office import read_body_from_stdin

    # Local dilemma-open deliberately gives stdin precedence over --question.
    return read_body_from_stdin()


def _parse_json(value: Any, label: str) -> Any:
    from .office_cli import _parse_json as parse_json

    return parse_json(value, label)


def _parse_json_file(value: Any, label: str) -> Any:
    from .office_cli import _parse_json_file as parse_json_file

    return parse_json_file(value, label)


def _direct_params(command: str, args: Any) -> dict[str, Any]:
    if command == "send":
        return _without_none(
            to=args.to,
            channel=args.channel,
            body=_resolve_body(args),
            kind=args.kind,
            reply_to=args.reply_to,
            refs=args.refs,
            dedupe_key=args.dedupe_key,
            new_recipient=bool(args.new_recipient),
        )
    if command == "ack":
        return _without_none(id=args.id, note=args.note)
    if command == "delivery":
        return {"id": args.id, "channel": args.channel}
    if command == "read":
        return _without_none(
            limit=args.limit,
            peek=bool(args.peek),
            channel=args.channel,
            kind=args.kind,
        )
    if command == "read-correlated-replies":
        return {
            "project_slug": args.project_slug,
            "parent_receipts": list(args.parent_receipt),
            "limit": args.limit,
        }
    if command == "wait":
        return _without_none(timeout_s=args.timeout, channel=args.channel)
    if command == "channels":
        return {}
    if command == "presence":
        return _without_none(
            ttl_s=args.ttl,
            status=args.status,
            workspace=args.workspace,
            release=bool(args.release),
        )
    if command == "who":
        return _without_none(workspace=args.workspace)
    if command == "wake-expect":
        return {
            "binding_id": args.binding_id,
            "task_id": args.task_id,
            "correlation_id": args.correlation_id,
            "counterpart": args.counterpart,
            "request_message_id": args.request_message_id,
            "max_wakes": args.max_wakes,
            "max_hops": args.max_hops,
            "ttl_s": args.ttl_s,
        }
    if command == "wake-consume":
        return {"correlation_id": args.correlation_id, "trigger_message_id": args.trigger_message_id}
    if command == "wake-close":
        return {"correlation_id": args.correlation_id}
    if command == "wake-relay-watch":
        return _without_none(
            binding_id=args.binding_id,
            interval_s=args.interval_s,
            timeout_s=args.timeout_s,
            delay_s=args.delay_s,
            driver=getattr(args, "driver", None),
        )
    if command == "wake-relay-pending":
        return _without_none(binding_id=args.binding_id, driver=getattr(args, "driver", None),
                             include_owned=getattr(args,'include_owned',False),
                             relay_agent=getattr(args,'relay_agent',None))
    if command == "wake-relay-claim":
        return _without_none(dispatch_id=args.dispatch_id, relay_agent=args.relay_agent,
                             driver=getattr(args, "driver", None))
    if command == "wake-relay-ack":
        return {"dispatch_id": args.dispatch_id, "driver": args.driver}
    if command == "wake-relay-receipt":
        return _without_none(
            dispatch_id=args.dispatch_id,
            status=args.status,
            message_id=getattr(args, "message_id", None),
            turn_id=getattr(args, "turn_id", None),
            detail=args.detail,
            driver=getattr(args, "driver", None),
        )
    if command == "wake-relay-observe":
        return _without_none(dispatch_id=args.dispatch_id, stage=args.stage,
                             driver=getattr(args, "driver", None),
                             native_proof=getattr(args,'native_proof_json',None))
    if command == "wake-relay-defer-busy":
        return _without_none(dispatch_id=args.dispatch_id, driver=getattr(args, "driver", None))
    if command == "wake-relay-native-begin":
        params = _without_none(dispatch_id=args.dispatch_id, relay_agent=args.relay_agent,
                               driver=getattr(args, "driver", None))
        prior = getattr(args,'prior_turn_id',None)
        if prior is not None:
            params['prior_turn_id'] = None if prior == 'null' else prior
        stamp = getattr(args,'baseline_observed_at',None)
        if stamp is not None:
            params['baseline_observed_at'] = stamp
        return params
    if command == "wake-relay-native-unsubmitted":
        return _without_none(dispatch_id=args.dispatch_id, relay_agent=args.relay_agent,
                             driver=getattr(args, "driver", None))
    if command == "wake-list":
        return _without_none(limit=args.limit, offset=args.offset)
    if command == "wake-task-cancel":
        return {"task_id": args.task_id}
    if command == "wake-correlations":
        return {}
    if command == "wake-pull-disarm":
        return {"binding_id": args.binding_id}
    if command == "wake-stop-status":
        return {}
    if command == "wake-ledger":
        return _without_none(limit=args.limit, binding_id=args.binding_id)
    raise CoordError(f"unknown remote direct command: {command}", code="invalid_command")


def _dilemma_options(values: list[str]) -> dict[str, str]:
    options: dict[str, str] = {}
    for raw in values:
        key, separator, text = raw.partition("=")
        if not separator:
            raise SystemExit("--option must be KEY=TEXT")
        options[key.strip()] = text.strip()
    return options


def _tool_params(command: str, args: Any) -> dict[str, Any]:
    if command in {"thread-register", "project-view-import", "goal-add", "goal-update"}:
        if (args.record_json is None) == (args.record_file is None):
            raise CoordError("supply exactly one record payload", code="invalid_args")
        record = (_parse_json(args.record_json, "--record-json") if args.record_json is not None
                  else _parse_json_file(args.record_file, "--record-file"))
        if command == "project-view-import":
            # Validate the complete source in this process. Only the allowlisted
            # project projection and its full-source digest cross the RPC boundary.
            from .project_registry_view import prepare_remote_registry_import

            return {"data": prepare_remote_registry_import(record)}
        if command == "goal-update":
            return {"goal_id": args.goal_id, "changes": record}
        return {"record": record}
    if command in {"thread-resolve", "thread-list"}:
        fields = dict(project_slug=args.project_slug, tool=args.tool, surface=args.surface)
        if command == "thread-resolve":
            fields.update(office_agent=args.office_agent, title=args.title)
        else:
            fields.update(status=args.status, limit=args.limit, offset=args.offset)
        return _without_none(**fields)
    if command in {"project-view-get", "goal-list"}:
        return _without_none(project_slug=args.project_slug)
    if command == "goal-get":
        return {"goal_id": args.goal_id}
    if command in {"project-view-list", "checkpoint"}:
        return {}
    if command == "dilemma-open":
        return _without_none(
            channel=args.channel,
            question=_read_dilemma_question(args),
            options=_dilemma_options(args.options),
            correlation_id=args.correlation_id,
            to=args.to,
            refs=args.refs,
        )
    if command == "dilemma-answer":
        return {"message_id": args.id, "choice": args.choice, "rationale": args.rationale}
    if command == "dilemma-status":
        return {"message_id": args.id}
    if command == "ask":
        return _without_none(
            to=args.to, channel=args.channel, body=_resolve_body(args), timeout_s=args.timeout
        )
    if command == "wake-pull-arm":
        return _without_none(binding_id=args.binding_id, max_pulls=args.max_pulls, lost=bool(args.lost))
    if command == "wake-pull":
        return {"binding_id": args.binding_id, "token": args.token}
    if command == "wake-pull-confirm":
        return {"binding_id": args.binding_id, "token": args.token, "job_id": args.job_id}
    if command == "project-register":
        return {"project_id": args.project_id, "checkout": args.checkout, "aliases": args.alias}
    if command == "project-resolve":
        return {"project": args.project}
    if command == "project-stream":
        # The local parser deliberately has no --sender-* flags for this route.
        return {"project": args.project, "task_id": args.task, "thread_id": args.thread, "run_id": args.run}
    if command == "project-activity":
        activity = _parse_json(args.activity_json, "--activity-json")
        if args.activity_file is not None:
            activity = _parse_json_file(args.activity_file, "--activity-file")
        return {
            "project_id": args.project_id,
            "task_id": args.task,
            "thread_id": args.thread,
            "run_id": args.run,
            "activity": activity,
            **_sender(args),
        }
    if command == "project-activity-read":
        return {
            "project_id": args.project_id,
            "task_id": args.task,
            "thread_id": args.thread,
            "run_id": args.run,
            "limit": args.limit,
            **_sender(args),
        }
    if command == "project-audits":
        return {"project": args.project}
    if command == "project-send":
        return _without_none(
            project_id=args.project_id,
            task_id=args.task,
            thread_id=args.thread,
            run_id=args.run,
            recipient=_parse_json(args.recipient_json, "--recipient-json"),
            body=args.body,
            kind=args.kind,
            ack_ttl_s=args.ack_ttl,
            **_sender(args),
        )
    if command == "project-send-channel":
        return _without_none(
            project_id=args.project_id,
            task_id=args.task,
            thread_id=args.thread,
            run_id=args.run,
            channel=args.channel,
            body=args.body,
            kind=args.kind,
            ack_ttl_s=args.ack_ttl,
            **_sender(args),
        )
    if command in {"project-read", "project-read-channel"}:
        values = _without_none(
            project_id=args.project_id,
            task_id=args.task,
            thread_id=args.thread,
            run_id=args.run,
            limit=args.limit,
            peek=bool(args.peek),
            **_sender(args),
        )
        if command == "project-read-channel":
            values["channel"] = args.channel
        return values
    if command in {"project-wait", "project-wait-channel"}:
        values = _without_none(
            project_id=args.project_id,
            task_id=args.task,
            thread_id=args.thread,
            run_id=args.run,
            timeout_s=args.timeout,
            **_sender(args),
        )
        if command == "project-wait-channel":
            values["channel"] = args.channel
        return values
    if command in {"project-acknowledge", "project-delivery", "project-cancel"}:
        return _without_none(project_id=args.project_id, delivery_id=args.delivery, **_sender(args))
    if command in {"journal-export", "journal-checkpoint"}:
        return {"record": _parse_json(args.record_json, "--record-json")}
    if command == "journal-read-checkpoint":
        return _without_none(
            project_id=args.project_id, task_id=args.task_id, checkpoint_id=args.checkpoint_id
        )
    if command in {"journal-audit", "journal-reconcile"}:
        return {}
    if command == "work-register":
        return _without_none(
            project=args.project,
            task_id=args.task,
            checkout=args.checkout,
            scopes=args.scope,
            dependencies=args.dependency,
            base_sha=args.base_sha,
            commit_intent=args.commit_intent,
            journal_refs=args.journal_ref,
            status=args.status,
            unknown_reasons=_parse_json(args.unknown_reasons_json, "--unknown-reasons-json"),
            deferred_reason=args.deferred_reason,
        )
    if command == "work-get":
        return {"project": args.project, "task_id": args.task}
    if command == "work-list":
        return _without_none(project=args.project, checkout=args.checkout)
    if command == "work-update":
        values = {"project": args.project, "task_id": args.task}
        optional = {
            "checkout": args.checkout,
            "scopes": args.scope,
            "dependencies": args.dependency,
            "base_sha": args.base_sha,
            "commit_intent": args.commit_intent,
            "journal_refs": args.journal_ref,
            "status": args.status,
            "unknown_reasons": _parse_json(args.unknown_reasons_json, "--unknown-reasons-json"),
            "deferred_reason": args.deferred_reason,
        }
        values.update(_without_none(**optional))
        return values
    if command == "work-blocker-add":
        return {
            "project": args.project,
            "task_id": args.task,
            "blocker_id": args.blocker,
            "reason": args.reason,
            "dependencies": args.dependency,
        }
    if command == "work-blocker-transition":
        return {
            "project": args.project,
            "task_id": args.task,
            "blocker_id": args.blocker,
            "status": args.status,
            "reason": args.reason,
        }
    if command == "work-plan-record":
        return {
            "project": args.project,
            "task_id": args.task,
            "from_agent": args.from_agent,
            "to_agent": args.to_agent,
            "scopes": args.scope,
            "summary": args.summary,
            "journal_refs": args.journal_ref,
        }
    if command == "work-plan-list":
        return {"project": args.project, "task_id": args.task}
    if command == "work-plan-acknowledge":
        return _without_none(
            project=args.project,
            task_id=args.task,
            plan_id=args.plan,
            agent_id=args.ack_agent,
            status=args.status,
            note=args.note,
        )
    if command == "conflict-advise":
        return _without_none(
            lease_result=_parse_json(args.lease_json, "--lease-json"),
            requester_lineage=_parse_json(args.lineage_json, "--lineage-json"),
        )
    if command == "work-projection":
        return {"project_id": args.project_id, "records": _parse_json(args.records_json, "--records-json")}
    raise CoordError(f"unknown remote tool command: {command}", code="invalid_command")


def _mcp_relay_args(args: Any) -> dict[str, Any]:
    from .office_cli import contractor_limits_from_args

    limits = contractor_limits_from_args(args)
    values: dict[str, Any] = {
        "profile": args.profile,
        "heartbeat_ttl_s": 0 if bool(args.no_heartbeat) else args.ttl,
    }
    if args.profile == "contractor":
        values["channels"] = list(args.mcp_channels)
        values["peers"] = list(args.mcp_peers)
        if limits is not None:
            values["contractor_limits"] = limits._asdict()
    return values


def _relay_mcp(client: OfficeRemoteClient, remote_identity: dict[str, str], args: Any) -> None:
    result = client.relay_mcp(remote_identity, _mcp_relay_args(args))
    if result.get("status") == "ok":
        return
    # MCP stdout belongs exclusively to JSON-RPC.  The client itself discards
    # SSH stderr; emit only a closed, sanitized status on local stderr.
    reason = result.get("reason")
    safe_reasons = {"ssh_unavailable", "relay_error", "invalid_response", "timeout"}
    if reason not in safe_reasons:
        reason = "transport_failure"
    sys.stderr.write(f"agent-office remote MCP degraded: {reason}\n")
    sys.stderr.flush()


def dispatch_remote(args: Any) -> dict[str, Any] | None:
    """Dispatch one already-parsed remote CLI invocation without local fallback."""

    command = getattr(args, "command", None)
    route = ROUTE_CLASSIFICATIONS.get(command)
    if route is None:
        raise CoordError(f"unknown command: {command}", code="invalid_command")
    if route == "local-only":
        return {"status": "error", "code": "remote_local_only"}
    if getattr(args, "home", None) is not None:
        raise CoordError("--remote-config cannot be combined with --home", code="remote_home_conflict")

    # This is the first I/O for every non-refused route.  It is configuration
    # only; no Office, Wake, board or local runtime directory is constructed.
    config = load_remote_config(getattr(args, "remote_config", None))
    client = OfficeRemoteClient(config)
    remote_identity = _remote_identity(args)

    if route == "mcp-relay":
        _relay_mcp(client, remote_identity, args)
        return None
    if route in {"direct", "direct-wake"}:
        return client.request(command, remote_identity, _direct_params(command, args))
    if route == "tool-call":
        return client.request(
            "tool-call",
            remote_identity,
            {"name": MCP_TOOL_ROUTES[command], "arguments": _tool_params(command, args)},
        )
    raise CoordError(f"unknown remote route: {route}", code="invalid_command")
