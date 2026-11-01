from __future__ import annotations

import json
import io
import hashlib
import stat
from types import SimpleNamespace
from argparse import Namespace
from pathlib import Path

import pytest

from coordfs import office_cli_remote as remote
from coordfs.errors import CoordError
from coordfs.office_cli import build_parser
from coordfs.office_mcp import TOOL_ARGS
from coordfs.project_registry_view import REMOTE_PROJECTION_FIELD


@pytest.fixture
def config_path():
    # Transport tests replace the loader at its boundary.  No key path is
    # opened, and the parser tests do not need a writable temporary directory.
    return Path("trusted-remote-config.json")


@pytest.fixture
def transport(monkeypatch):
    calls: list[tuple[object, ...]] = []

    class StubClient:
        def __init__(self, config):
            calls.append(("config", config))

        def request(self, op, agent, params):
            calls.append(("request", op, agent, params))
            return {"status": "ok", "op": op}

        def relay_mcp(self, agent, params):
            calls.append(("relay", agent, params))
            return {"status": "ok"}

    monkeypatch.setattr(remote, "OfficeRemoteClient", StubClient)
    monkeypatch.setattr(
        remote,
        "load_remote_config",
        lambda _path: remote.OfficeRemoteConfig(
            host="office.example.test",
            user="office",
            identity_file="C:/keys/office-ed25519",
            known_hosts_file="C:/keys/known_hosts",
        ),
    )
    monkeypatch.setattr(
        remote.identity,
        "agent_identity",
        lambda harness, session_id=None: {
            "agent_id": "codex-host-session",
            "harness": harness,
            "host": "host",
            "session": session_id or "session",
            "strength": "strong",
            "source": "test",
        },
    )
    return calls


def make_args(config_path, command: str, **values):
    data = {
        "command": command,
        "remote_config": config_path,
        "home": None,
        "agent": None,
        "harness": "codex",
        "session_id": "session",
    }
    data.update(values)
    return Namespace(**data)


def request_call(calls):
    calls = [call for call in calls if call[0] == "request"]
    assert len(calls) == 1
    return calls[0]


def synthetic_v6_registry():
    return {
        "schema": "agent-stack/registry-v2",
        "version": "v6-synthetic-1.2",
        "generated_at": "2026-10-05T12:30:00+00:00",
        "organizations": [{
            "id": "synthetic-org",
            "kind": "synthetic-kind",
            "status": "confirmed",
            "aliases": [],
            "email_domains": ["EMAIL_DOMAIN_SENTINEL.invalid"],
        }],
        "workspace_groups": [],
        "projects": {
            "synthetic-project": {
                "title": "Synthetic project",
                "family": "synthetic-family",
                "aliases": [],
                "org_links": [{"org": "synthetic-org", "role": "owner", "status": "confirmed"}],
                "org_resolution": "synthetic-test",
                "folders": [],
                "hub_main_page": None,
                "hub_latest_page": None,
                "status": "active",
            }
        },
        "topics": [],
        "people": [{
            "id": "synthetic-person",
            "name": "PERSON_NAME_SENTINEL",
            "orgs": ["synthetic-org"],
            "projects": ["synthetic-project"],
        }],
        "sources": {"unrestricted-extension": {"value": "SOURCE_VALUE_SENTINEL"}},
    }


def test_route_inventory_is_closed_and_matches_the_actual_parser():
    parser = build_parser()
    subcommands = next(action for action in parser._actions if action.dest == "command").choices

    assert len(subcommands) == 93
    assert len(remote.DIRECT_ROUTES) == 20
    assert len(remote.NEW_WAKE_DIRECT_ROUTES) == 6
    assert len(remote.MCP_TOOL_ROUTES) == 49
    assert len(remote.LOCAL_ONLY_ROUTES) == 17
    assert set(remote.ROUTE_CLASSIFICATIONS) == set(subcommands)
    assert set(remote.DIRECT_ROUTES).isdisjoint(remote.NEW_WAKE_DIRECT_ROUTES)
    assert set(remote.DIRECT_ROUTES).isdisjoint(remote.MCP_TOOL_ROUTES)
    assert set(remote.NEW_WAKE_DIRECT_ROUTES).isdisjoint(remote.MCP_TOOL_ROUTES)
    assert set(remote.LOCAL_ONLY_ROUTES).isdisjoint(remote.MCP_TOOL_ROUTES)
    assert len(set(remote.MCP_TOOL_ROUTES.values())) == 49
    assert set(remote.MCP_TOOL_ROUTES.values()).issubset(TOOL_ARGS)
    assert remote.ROUTE_CLASSIFICATIONS["mcp"] == "mcp-relay"
    assert {
        command for command, route in remote.ROUTE_CLASSIFICATIONS.items() if route == "direct"
    } == remote.DIRECT_ROUTES
    assert {
        command for command, route in remote.ROUTE_CLASSIFICATIONS.items() if route == "direct-wake"
    } == remote.NEW_WAKE_DIRECT_ROUTES
    assert {
        command for command, route in remote.ROUTE_CLASSIFICATIONS.items() if route == "local-only"
    } == remote.LOCAL_ONLY_ROUTES
    assert {
        command for command, route in remote.ROUTE_CLASSIFICATIONS.items() if route == "tool-call"
    } == set(remote.MCP_TOOL_ROUTES)


def test_project_registry_import_strips_people_before_rpc_and_keeps_source_hash(config_path, transport):
    source = synthetic_v6_registry()
    encoded_source = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    expected_sha = hashlib.sha256(encoded_source).hexdigest()
    args = make_args(
        config_path,
        "project-view-import",
        record_json=json.dumps(source),
        record_file=None,
    )

    result = remote.dispatch_remote(args)

    assert result == {"status": "ok", "op": "tool-call"}
    rpc_call = request_call(transport)
    rpc_text = json.dumps(rpc_call)
    assert REMOTE_PROJECTION_FIELD in rpc_text
    for sentinel in ("PERSON_NAME_SENTINEL", "EMAIL_DOMAIN_SENTINEL.invalid", "SOURCE_VALUE_SENTINEL"):
        assert sentinel not in rpc_text
    payload = rpc_call[3]["arguments"]["data"]
    envelope = payload[REMOTE_PROJECTION_FIELD]
    assert envelope["source_sha256"] == expected_sha
    assert set(envelope) == {"schema", "version", "generated_at", "source_sha256", "projects"}
    assert set(envelope["projects"]["synthetic-project"]) == {
        "title", "family", "aliases", "org_links", "org_resolution", "folders",
        "hub_main_page", "hub_latest_page", "status",
    }

    before_requests = len([call for call in transport if call[0] == "request"])
    bad = json.loads(json.dumps(source))
    bad["sources"]["unrestricted-extension"]["prompt"] = "PROMPT_SENTINEL"
    rejected_args = make_args(
        config_path,
        "project-view-import",
        record_json=json.dumps(bad),
        record_file=None,
    )
    with pytest.raises(CoordError) as rejected:
        remote.dispatch_remote(rejected_args)
    assert "PROMPT_SENTINEL" not in str(rejected.value)
    assert len([call for call in transport if call[0] == "request"]) == before_requests


DIRECT_CASES = [
    (
        "send",
        {
            "to": "lead", "channel": None, "body": "hello", "body_stdin": False,
            "kind": "handoff", "reply_to": "a" * 32, "refs": ["shared/file.py"],
            "dedupe_key": "retry-1", "new_recipient": True,
        },
        {
            "to": "lead", "body": "hello", "kind": "handoff", "reply_to": "a" * 32,
            "refs": ["shared/file.py"], "dedupe_key": "retry-1", "new_recipient": True,
        },
    ),
    ("ack", {"id": "b" * 32, "note": "seen"}, {"id": "b" * 32, "note": "seen"}),
    ("delivery", {"id": "c" * 32, "channel": "task"}, {"id": "c" * 32, "channel": "task"}),
    (
        "read",
        {"limit": 7, "peek": True, "channel": "task", "kind": "alert"},
        {"limit": 7, "peek": True, "channel": "task", "kind": "alert"},
    ),
    ("wait", {"timeout": 12.5, "channel": "task"}, {"timeout_s": 12.5, "channel": "task"}),
    ("channels", {}, {}),
    (
        "presence",
        {"ttl": 55, "status": "busy", "workspace": "D:/work", "release": False},
        {"ttl_s": 55, "status": "busy", "workspace": "D:/work", "release": False},
    ),
    ("who", {"workspace": "D:/work"}, {"workspace": "D:/work"}),
    (
        "wake-expect",
        {
            "binding_id": "bind", "task_id": "task", "correlation_id": "corr",
            "counterpart": "peer", "request_message_id": "d" * 32,
            "max_wakes": 3, "max_hops": 4, "ttl_s": 90,
        },
        {
            "binding_id": "bind", "task_id": "task", "correlation_id": "corr",
            "counterpart": "peer", "request_message_id": "d" * 32,
            "max_wakes": 3, "max_hops": 4, "ttl_s": 90,
        },
    ),
    (
        "wake-consume",
        {"correlation_id": "corr", "trigger_message_id": "e" * 32},
        {"correlation_id": "corr", "trigger_message_id": "e" * 32},
    ),
    ("wake-close", {"correlation_id": "corr"}, {"correlation_id": "corr"}),
    (
        "wake-relay-watch",
        {"binding_id": ["one", "two"], "interval_s": 4, "timeout_s": 80, "delay_s": 3},
        {"binding_id": ["one", "two"], "interval_s": 4, "timeout_s": 80, "delay_s": 3},
    ),
    ("wake-relay-pending", {"binding_id": "bind"}, {"binding_id": "bind", "include_owned": False}),
    (
        "wake-relay-ack",
        {"dispatch_id": "d" * 32, "driver": "antigravity-desktop-relay"},
        {"dispatch_id": "d" * 32, "driver": "antigravity-desktop-relay"},
    ),
    (
        "wake-relay-claim",
        {"dispatch_id": "dispatch", "relay_agent": "codex-host-session"},
        {"dispatch_id": "dispatch", "relay_agent": "codex-host-session"},
    ),
    (
        "wake-relay-receipt",
        {"dispatch_id": "dispatch", "status": "sent", "message_id": "f" * 32, "detail": "ok"},
        {"dispatch_id": "dispatch", "status": "sent", "message_id": "f" * 32, "detail": "ok"},
    ),
    (
        "wake-relay-observe",
        {"dispatch_id": "dispatch", "stage": "received"},
        {"dispatch_id": "dispatch", "stage": "received"},
    ),
    ("wake-relay-defer-busy", {"dispatch_id": "dispatch"}, {"dispatch_id": "dispatch"}),
    ("wake-list", {"limit": 64, "offset": 0}, {"limit": 64, "offset": 0}),
    ("wake-task-cancel", {"task_id": "task"}, {"task_id": "task"}),
    ("wake-correlations", {}, {}),
    ("wake-pull-disarm", {"binding_id": "bind"}, {"binding_id": "bind"}),
    ("wake-stop-status", {}, {}),
    ("wake-ledger", {"limit": 9, "binding_id": "bind"}, {"limit": 9, "binding_id": "bind"}),
]


@pytest.mark.parametrize(("command", "values", "expected"), DIRECT_CASES)
def test_each_direct_route_uses_the_closed_operation(config_path, transport, command, values, expected):
    result = remote.dispatch_remote(make_args(config_path, command, **values))

    assert result == {"status": "ok", "op": command}
    _, op, remote_identity, params = request_call(transport)
    assert op == command
    assert remote_identity == {"harness": "codex", "host": "host", "session_id": "session"}
    assert params == expected


TOOL_CASES = [
    (
        "dilemma-open",
        {
            "channel": "task", "question": "which?", "body_stdin": False,
            "options": ["yes=use it", "no=wait"], "correlation_id": "corr", "to": "lead",
            "refs": ["docs/decision.md"],
        },
        "office_dilemma_open",
        {
            "channel": "task", "question": "which?", "options": {"yes": "use it", "no": "wait"},
            "correlation_id": "corr", "to": "lead", "refs": ["docs/decision.md"],
        },
    ),
    (
        "dilemma-answer",
        {"id": "a" * 32, "choice": "yes", "rationale": "because"},
        "office_dilemma_answer",
        {"message_id": "a" * 32, "choice": "yes", "rationale": "because"},
    ),
    ("dilemma-status", {"id": "b" * 32}, "office_dilemma_status", {"message_id": "b" * 32}),
    (
        "ask",
        {"to": "lead", "channel": None, "body": "question", "body_stdin": False, "timeout": 4.5},
        "office_ask",
        {"to": "lead", "body": "question", "timeout_s": 4.5},
    ),
    (
        "wake-pull-arm",
        {"binding_id": "bind", "max_pulls": 3, "lost": True},
        "office_wake_pull_arm",
        {"binding_id": "bind", "max_pulls": 3, "lost": True},
    ),
    ("wake-pull", {"binding_id": "bind", "token": "token"}, "office_wake_pull", {"binding_id": "bind", "token": "token"}),
    (
        "wake-pull-confirm",
        {"binding_id": "bind", "token": "token", "job_id": "job"},
        "office_wake_pull_confirm",
        {"binding_id": "bind", "token": "token", "job_id": "job"},
    ),
    (
        "project-register",
        {"project_id": "project", "checkout": "D:/checkout", "alias": ["p"]},
        "office_project_register",
        {"project_id": "project", "checkout": "D:/checkout", "aliases": ["p"]},
    ),
    ("project-resolve", {"project": "p"}, "office_project_resolve", {"project": "p"}),
    (
        "project-stream",
        {"project": "p", "task": "t", "thread": "th", "run": "r", "sender_harness": "ignored"},
        "office_project_stream",
        {"project": "p", "task_id": "t", "thread_id": "th", "run_id": "r"},
    ),
    (
        "project-activity",
        {
            "project_id": "p", "task": "t", "thread": "th", "run": "r",
            "activity_json": '{"source_event_id":"event","provider_id":"codex","kind":"run","raw_method":"x"}',
            "activity_file": None, "sender_harness": "codex", "sender_host": "host", "sender_session": "session",
        },
        "office_project_activity",
        {
            "project_id": "p", "task_id": "t", "thread_id": "th", "run_id": "r",
            "activity": {"source_event_id": "event", "provider_id": "codex", "kind": "run", "raw_method": "x"},
            "harness": "codex", "host": "host", "session_id": "session",
        },
    ),
    (
        "project-activity-read",
        {"project_id": "p", "task": "t", "thread": "th", "run": "r", "limit": 8},
        "office_project_activity_read",
        {"project_id": "p", "task_id": "t", "thread_id": "th", "run_id": "r", "limit": 8},
    ),
    ("project-audits", {"project": "p"}, "office_project_audits", {"project": "p"}),
    (
        "project-send",
        {
            "project_id": "p", "task": "t", "thread": "th", "run": "r",
            "recipient_json": '{"harness":"claude","host":"h","session_id":"s"}',
            "body": "body", "kind": "note", "ack_ttl": 9,
        },
        "office_project_send",
        {
            "project_id": "p", "task_id": "t", "thread_id": "th", "run_id": "r",
            "recipient": {"harness": "claude", "host": "h", "session_id": "s"},
            "body": "body", "kind": "note", "ack_ttl_s": 9,
        },
    ),
    (
        "project-send-channel",
        {
            "project_id": "p", "task": "t", "thread": "th", "run": "r", "channel": "task",
            "body": "body", "kind": "alert", "ack_ttl": 6,
        },
        "office_project_send_channel",
        {
            "project_id": "p", "task_id": "t", "thread_id": "th", "run_id": "r", "channel": "task",
            "body": "body", "kind": "alert", "ack_ttl_s": 6,
        },
    ),
    (
        "project-read",
        {"project_id": "p", "task": "t", "thread": "th", "run": "r", "limit": 5, "peek": True},
        "office_project_read",
        {"project_id": "p", "task_id": "t", "thread_id": "th", "run_id": "r", "limit": 5, "peek": True},
    ),
    (
        "project-read-channel",
        {"project_id": "p", "task": "t", "thread": "th", "run": "r", "channel": "task", "limit": 4, "peek": False},
        "office_project_read_channel",
        {"project_id": "p", "task_id": "t", "thread_id": "th", "run_id": "r", "channel": "task", "limit": 4, "peek": False},
    ),
    (
        "project-wait",
        {"project_id": "p", "task": "t", "thread": "th", "run": "r", "timeout": 2.5},
        "office_project_wait",
        {"project_id": "p", "task_id": "t", "thread_id": "th", "run_id": "r", "timeout_s": 2.5},
    ),
    (
        "project-wait-channel",
        {"project_id": "p", "task": "t", "thread": "th", "run": "r", "channel": "task", "timeout": 3.5},
        "office_project_wait_channel",
        {"project_id": "p", "task_id": "t", "thread_id": "th", "run_id": "r", "channel": "task", "timeout_s": 3.5},
    ),
    (
        "project-acknowledge",
        {"project_id": "p", "delivery": "delivery"},
        "office_project_acknowledge",
        {"project_id": "p", "delivery_id": "delivery"},
    ),
    (
        "project-delivery",
        {"project_id": "p", "delivery": "delivery"},
        "office_project_delivery",
        {"project_id": "p", "delivery_id": "delivery"},
    ),
    (
        "project-cancel",
        {"project_id": "p", "delivery": "delivery"},
        "office_project_cancel",
        {"project_id": "p", "delivery_id": "delivery"},
    ),
    (
        "journal-export",
        {"record_json": '{"task":"t"}'},
        "office_journal_export",
        {"record": {"task": "t"}},
    ),
    (
        "journal-checkpoint",
        {"record_json": '{"checkpoint":"c"}'},
        "office_journal_checkpoint",
        {"record": {"checkpoint": "c"}},
    ),
    (
        "journal-read-checkpoint",
        {"project_id": "p", "task_id": "t", "checkpoint_id": "c"},
        "office_journal_read_checkpoint",
        {"project_id": "p", "task_id": "t", "checkpoint_id": "c"},
    ),
    ("journal-audit", {}, "office_journal_audit", {}),
    ("journal-reconcile", {}, "office_journal_reconcile", {}),
    (
        "work-register",
        {
            "project": "p", "task": "t", "checkout": "D:/checkout", "scope": ["src/"],
            "dependency": ["dep"], "base_sha": "a" * 40, "commit_intent": "intent",
            "journal_ref": ["j"], "status": "active", "unknown_reasons_json": '{"why":"unknown"}',
            "deferred_reason": "later",
        },
        "office_work_register",
        {
            "project": "p", "task_id": "t", "checkout": "D:/checkout", "scopes": ["src/"],
            "dependencies": ["dep"], "base_sha": "a" * 40, "commit_intent": "intent",
            "journal_refs": ["j"], "status": "active", "unknown_reasons": {"why": "unknown"},
            "deferred_reason": "later",
        },
    ),
    ("work-get", {"project": "p", "task": "t"}, "office_work_get", {"project": "p", "task_id": "t"}),
    (
        "work-list",
        {"project": "p", "checkout": "D:/checkout"},
        "office_work_list",
        {"project": "p", "checkout": "D:/checkout"},
    ),
    (
        "work-update",
        {
            "project": "p", "task": "t", "checkout": "D:/next", "scope": ["src/"],
            "dependency": ["dep"], "base_sha": "b" * 40, "commit_intent": "intent",
            "journal_ref": ["j"], "status": "blocked", "unknown_reasons_json": '{"why":"missing"}',
            "deferred_reason": "later",
        },
        "office_work_update",
        {
            "project": "p", "task_id": "t", "checkout": "D:/next", "scopes": ["src/"],
            "dependencies": ["dep"], "base_sha": "b" * 40, "commit_intent": "intent",
            "journal_refs": ["j"], "status": "blocked", "unknown_reasons": {"why": "missing"},
            "deferred_reason": "later",
        },
    ),
    (
        "work-blocker-add",
        {"project": "p", "task": "t", "blocker": "blocked", "reason": "reason", "dependency": ["dep"]},
        "office_work_blocker_add",
        {"project": "p", "task_id": "t", "blocker_id": "blocked", "reason": "reason", "dependencies": ["dep"]},
    ),
    (
        "work-blocker-transition",
        {"project": "p", "task": "t", "blocker": "blocked", "status": "resolved", "reason": "fixed"},
        "office_work_blocker_transition",
        {"project": "p", "task_id": "t", "blocker_id": "blocked", "status": "resolved", "reason": "fixed"},
    ),
    (
        "work-plan-record",
        {"project": "p", "task": "t", "from_agent": "maker", "to_agent": "reviewer", "scope": ["src/"], "summary": "sum", "journal_ref": ["j"]},
        "office_work_plan_record",
        {"project": "p", "task_id": "t", "from_agent": "maker", "to_agent": "reviewer", "scopes": ["src/"], "summary": "sum", "journal_refs": ["j"]},
    ),
    (
        "work-plan-list",
        {"project": "p", "task": "t"},
        "office_work_plan_list",
        {"project": "p", "task_id": "t"},
    ),
    (
        "work-plan-acknowledge",
        {"project": "p", "task": "t", "plan": "plan", "ack_agent": "reviewer", "status": "accepted", "note": "ok"},
        "office_work_plan_acknowledge",
        {"project": "p", "task_id": "t", "plan_id": "plan", "agent_id": "reviewer", "status": "accepted", "note": "ok"},
    ),
    (
        "conflict-advise",
        {"lease_json": '{"status":"held"}', "lineage_json": '{"agent":"codex"}'},
        "office_conflict_advise",
        {"lease_result": {"status": "held"}, "requester_lineage": {"agent": "codex"}},
    ),
    (
        "work-projection",
        {"project_id": "p", "records_json": '[{"task":"t"}]'},
        "office_work_projection",
        {"project_id": "p", "records": [{"task": "t"}]},
    ),
]


@pytest.mark.parametrize(("command", "values", "tool", "expected"), TOOL_CASES)
def test_each_tool_route_sends_structured_allowlisted_arguments(config_path, transport, command, values, tool, expected):
    result = remote.dispatch_remote(make_args(config_path, command, **values))

    assert result == {"status": "ok", "op": "tool-call"}
    _, op, remote_identity, envelope = request_call(transport)
    assert op == "tool-call"
    assert remote_identity == {"harness": "codex", "host": "host", "session_id": "session"}
    assert envelope == {"name": tool, "arguments": expected}


def test_body_stdin_is_unicode_multiline_and_read_once(config_path, transport, monkeypatch):
    import coordfs.office_cli as office_cli

    reads = []

    def read_once():
        reads.append(True)
        return "zażółć\n第二行"

    monkeypatch.setattr(office_cli, "read_body_from_stdin", read_once)
    args = make_args(
        config_path,
        "send",
        to="lead",
        channel=None,
        body=None,
        body_stdin=True,
        kind="note",
        reply_to=None,
        refs=None,
        dedupe_key=None,
        new_recipient=False,
    )

    remote.dispatch_remote(args)

    assert reads == [True]
    assert request_call(transport)[3]["body"] == "zażółć\n第二行"


@pytest.mark.parametrize("command", ["send", "ask"])
def test_body_and_stdin_conflict_is_refused_without_transport(config_path, transport, command):
    if command == "send":
        args = make_args(
            config_path, command, to="lead", channel=None, body="one", body_stdin=True,
            kind="note", reply_to=None, refs=None, dedupe_key=None, new_recipient=False,
        )
    else:
        args = make_args(config_path, command, to="lead", channel=None, body="one", body_stdin=True, timeout=1.0)

    with pytest.raises(CoordError, match="mutually exclusive"):
        remote.dispatch_remote(args)

    assert not [call for call in transport if call[0] == "request"]


def test_dilemma_open_preserves_existing_stdin_precedence(config_path, transport, monkeypatch):
    import coordfs.office as office

    reads = []
    monkeypatch.setattr(office, "read_body_from_stdin", lambda: reads.append(True) or "stdin wins")
    args = make_args(
        config_path,
        "dilemma-open",
        channel="task",
        question="argument loses",
        body_stdin=True,
        options=["a=A", "b=B"],
        correlation_id=None,
        to=None,
        refs=None,
    )

    remote.dispatch_remote(args)

    assert reads == [True]
    assert request_call(transport)[3]["arguments"]["question"] == "stdin wins"


def test_project_activity_file_is_parsed_locally_not_sent_as_a_path(config_path, transport, monkeypatch):
    activity = Path("activity.json")
    monkeypatch.setattr(
        remote,
        "_parse_json_file",
        lambda path, label: {"source_event_id": "e", "provider_id": "codex", "kind": "run", "raw_method": "m"},
    )
    args = make_args(
        config_path,
        "project-activity",
        project_id="p",
        task="t",
        thread="th",
        run="r",
        activity_json=None,
        activity_file=activity,
    )

    remote.dispatch_remote(args)

    arguments = request_call(transport)[3]["arguments"]
    assert arguments["activity"] == {"source_event_id": "e", "provider_id": "codex", "kind": "run", "raw_method": "m"}
    assert str(activity) not in repr(arguments)


def test_explicit_agent_is_preserved_and_project_sender_is_not_rewritten(config_path, transport):
    args = make_args(
        config_path,
        "project-activity-read",
        agent="manual-agent",
        project_id="p",
        task="t",
        thread="th",
        run="r",
        limit=5,
        sender_harness="other",
        sender_host="other-host",
        sender_session="other-session",
    )

    remote.dispatch_remote(args)

    _, _, remote_identity, envelope = request_call(transport)
    assert remote_identity == {"agent_id": "manual-agent"}
    assert envelope["arguments"].items() >= {
        "harness": "other", "host": "other-host", "session_id": "other-session"
    }.items()


def test_invalid_config_is_generic_and_does_not_expose_secret(monkeypatch):
    monkeypatch.setattr(remote, "_config_stat", lambda *_args: SimpleNamespace(st_mode=stat.S_IFREG))
    secret = "super-secret-value-must-not-appear"
    payload = json.dumps({"host": "office.example.test", "api_token": secret}).encode("utf-8")
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: io.BytesIO(payload))

    with pytest.raises(CoordError) as raised:
        remote.load_remote_config(Path("bad.json"))

    assert raised.value.code == "invalid_remote_config"
    assert secret not in str(raised.value)


def test_config_loader_accepts_only_the_dataclass_json_shape(monkeypatch):
    monkeypatch.setattr(remote, "_config_stat", lambda *_args: SimpleNamespace(st_mode=stat.S_IFREG))
    raw = json.dumps(
        {
            "host": "office.example.test",
            "user": "office",
            "identity_file": "C:/keys/office-ed25519",
            "known_hosts_file": "C:/keys/known_hosts",
            "ssh_executable": "C:/Windows/System32/OpenSSH/ssh.exe",
            "port": 2222,
            "connect_timeout_s": 9,
            "request_timeout_s": 12.5,
        }
    ).encode("utf-8")
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: io.BytesIO(raw))

    config = remote.load_remote_config(Path("trusted.json"))

    assert config.host == "office.example.test"
    assert config.port == 2222
    assert config.request_timeout_s == 12.5


@pytest.mark.parametrize(
    "raw",
    [
        b"[]",
        b'{"host":"office.example.test","user":"office","identity_file":"key","known_hosts_file":"hosts","port":true}',
        b'{"host":"office.example.test","host":"other","user":"office","identity_file":"key","known_hosts_file":"hosts"}',
    ],
)
def test_config_loader_rejects_invalid_json_types_and_duplicate_fields(monkeypatch, raw):
    monkeypatch.setattr(remote, "_config_stat", lambda *_args: SimpleNamespace(st_mode=stat.S_IFREG))
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: io.BytesIO(raw))

    with pytest.raises(CoordError) as raised:
        remote.load_remote_config(Path("invalid.json"))

    assert raised.value.code == "invalid_remote_config"


def test_ssh_failure_is_degraded_without_creating_or_using_a_local_home(config_path, monkeypatch):
    state_home = Path("would-be-local-home")

    class FailingClient:
        def __init__(self, config):
            self.config = config

        def request(self, op, agent, params):
            return {"status": "degraded", "reason": "ssh_unavailable"}

    monkeypatch.setattr(remote, "OfficeRemoteClient", FailingClient)
    monkeypatch.setattr(
        remote,
        "load_remote_config",
        lambda _path: remote.OfficeRemoteConfig(
            host="office.example.test", user="office", identity_file="key", known_hosts_file="hosts"
        ),
    )
    import coordfs.office as office

    monkeypatch.setattr(office, "Office", lambda **_kwargs: pytest.fail("local Office instantiated"))
    result = remote.dispatch_remote(
        make_args(config_path, "channels", home=None)
    )

    assert result == {"status": "degraded", "reason": "ssh_unavailable"}
    assert not state_home.exists()


def test_local_only_refusal_happens_before_config_or_stdin_access(config_path, monkeypatch):
    monkeypatch.setattr(remote, "load_remote_config", lambda _value: pytest.fail("config read"))
    import coordfs.office as office

    monkeypatch.setattr(office, "read_body_from_stdin", lambda: pytest.fail("stdin read"))
    args = make_args(
        config_path,
        "wake-intake",
        binding_id="bind",
        expected_version=1,
        idempotency_key="key",
        owner_confirm=True,
        prompt_stdin=True,
    )

    assert remote.dispatch_remote(args) == {"status": "error", "code": "remote_local_only"}


def test_remote_home_conflict_is_visible_without_opening_config(config_path, monkeypatch):
    monkeypatch.setattr(remote, "load_remote_config", lambda _value: pytest.fail("config read"))
    args = make_args(config_path, "channels", home=Path("home"))

    with pytest.raises(CoordError) as raised:
        remote.dispatch_remote(args)

    assert raised.value.code == "remote_home_conflict"


def test_mcp_relay_preserves_contractor_settings_ttl_and_keeps_stdout_clean(config_path, transport, capsys, monkeypatch):
    class DegradedClient:
        def __init__(self, config):
            transport.append(("config", config))

        def relay_mcp(self, agent, params):
            transport.append(("relay", agent, params))
            return {"status": "degraded", "reason": "ssh_unavailable"}

    monkeypatch.setattr(remote, "OfficeRemoteClient", DegradedClient)
    args = make_args(
        config_path,
        "mcp",
        ttl=91,
        no_heartbeat=False,
        profile="contractor",
        mcp_channels=["task"],
        mcp_peers=["lead"],
        max_send_bytes=100,
        read_page=5,
        read_body_bytes=50,
        max_read_bytes=200,
        max_waits=2,
    )

    assert remote.dispatch_remote(args) is None

    _, remote_identity, params = [call for call in transport if call[0] == "relay"][0]
    assert remote_identity == {"harness": "codex", "host": "host", "session_id": "session"}
    assert params == {
        "profile": "contractor",
        "heartbeat_ttl_s": 91,
        "channels": ["task"],
        "peers": ["lead"],
        "contractor_limits": {
            "max_send_bytes": 100,
            "read_page": 5,
            "read_body_bytes": 50,
            "read_budget_bytes": 200,
            "max_waits": 2,
        },
    }
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "agent-office remote MCP degraded: ssh_unavailable\n"


def test_mcp_no_heartbeat_uses_zero_ttl(config_path, transport):
    args = make_args(
        config_path,
        "mcp",
        ttl=91,
        no_heartbeat=True,
        profile="full",
        mcp_channels=[],
        mcp_peers=[],
        max_send_bytes=None,
        read_page=None,
        read_body_bytes=None,
        max_read_bytes=None,
        max_waits=None,
    )

    assert remote.dispatch_remote(args) is None
    assert [call for call in transport if call[0] == "relay"][0][2] == {
        "profile": "full", "heartbeat_ttl_s": 0
    }
