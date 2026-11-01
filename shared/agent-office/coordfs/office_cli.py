"""Mirrored office CLI. JSON output; `office mcp` starts the stdio MCP server.

Project, journal, active-work, conflict, and projection boundaries are
subcommands here (never a second store): complex values travel as inline JSON
(`--recipient-json`, `--record-json`, `--records-json`, `--lease-json`,
`--lineage-json`, `--unknown-reasons-json`). The sender is the process
identity (or an explicit --sender-harness/--sender-host/--sender-session
triple); no command accepts a `from` field. File-backed JSON is available for
the activity ingress because the Windows `.cmd` wrapper cannot preserve
embedded JSON quotes reliably.

`send` and `ask` take their body either as `--body <text>` or -- for text a
Windows command line cannot carry -- as `--body-stdin`, which reads standard
input. stdin is never read unless that flag is given.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from .errors import CoordError
from .knowledge_bridge import sync_knowledge
from .office import (
    DEFAULT_MESSAGE_KIND,
    DEFAULT_PRESENCE_TTL_S,
    DEFAULT_READ_LIMIT,
    MAX_READ_LIMIT,
    MAX_STATUS_CHARS,
    MAX_WORKSPACE_CHARS,
    MESSAGE_KINDS,
    OFFICE_VERSION,
    Office,
    _bare_channel,
    detect_secret,
    read_body_from_stdin,
)
from .identity import agent_identity
from .leases import validate_ttl
from .office_mcp import (
    CONTRACTOR_MAX_SEND_BYTES,
    CONTRACTOR_MAX_WAITS,
    CONTRACTOR_READ_BODY_BYTES,
    CONTRACTOR_READ_BUDGET_BYTES,
    CONTRACTOR_READ_PAGE,
    ContractorLimits,
    serve_stdio,
)
from .relay_verify import load_allow, verify_dispatch
from .wake import (
    DEFAULT_ALLOWED_TRIGGERS,
    DEFAULT_BUDGET_PER_DAY,
    DEFAULT_MAX_ATTEMPTS_PER_TRIGGER,
    DEFAULT_SCHEDULE_GRACE_MINUTES,
    DEFAULT_WAKE_KINDS,
    EXTERNAL_RELAY_DRIVERS,
    RELAY_OBSERVATION_STAGES,
    RELAY_RECEIPT_STATUSES,
    SUPPORTED_DRIVERS,
    SUPPORTED_TRIGGERS,
    Wake,
)

# cmd.exe truncates an argument at its first newline (measured 2026-09-21: a
# three-line --body arrived as one line, both through `python office.py` and
# through `office.cmd`, while the same call outside cmd.exe kept all three).
# stdin is not part of the command line, so it is the only channel cmd.exe
# leaves alone -- and it is read only when this flag asks for it.
BODY_STDIN_HELP = (
    "Read the body from standard input (UTF-8, max 16 KB) instead of --body. "
    "Use this for multi-line text under cmd.exe: `--body \"L1<newline>L2\"` is cut "
    "at its first line before the launcher even starts. Mutually exclusive with --body."
)
BODY_ARG_HELP = "Message text (max 16 KB). Omit it and pass --body-stdin to read the text from stdin."
ASK_BODY_ARG_HELP = "Question text (max 16 KB). Omit it and pass --body-stdin to read the text from stdin."


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _exit_code(payload: dict[str, Any], *, remote: bool = False) -> int:
    # Local degraded remains fail-open. A remote transport failure must be
    # visible to shell callers so they cannot mistake an outage for delivery.
    if remote and payload.get("status") == "degraded" and payload.get("reason") in {
        "ssh_unavailable", "timeout", "invalid_response", "relay_error",
    }:
        return 1
    if payload.get("status") in {"ok", "warning", "degraded"}:
        return 0
    if payload.get("code") == "wake_manual_intake_unknown":
        return 0
    if (
        payload.get("decision") in {"accepted", "blocked", "unknown"}
        and isinstance(payload.get("idempotency_key_hash"), str)
    ):
        return 0
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="office",
        description="Agent Office: local, stdio-only agent-to-agent messaging on top of coordfs.",
    )
    parser.add_argument("--home", type=Path, help="Override runtime home (default %%USERPROFILE%%/.agents/coord).")
    # The installed wrapper binds a local default. Remote selection ignores it,
    # whereas an explicit caller --home remains an incompatible store override.
    parser.add_argument("--runtime-home", type=Path, help=argparse.SUPPRESS)
    parser.add_argument(
        "--remote-config", type=Path,
        help="Use the explicit SSH remote configuration; never fall back to local Office state.",
    )
    parser.add_argument("--agent", help="Explicit agent id. Overrides --harness derivation.")
    parser.add_argument(
        "--harness",
        "--agent-id",
        dest="harness",
        help="Harness name used to derive <harness>-<host>-<session> (default COORDFS_HARNESS or unknown).",
    )
    parser.add_argument("--session-id", help="Runtime session id. Prefer COORDFS_SESSION_ID set by the adapter.")
    parser.add_argument("--version", action="version", version=f"agent-office {OFFICE_VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    send = sub.add_parser(
        "send", help="Send to an agent's inbox or to a channel. `from` is derived by the server, never supplied."
    )
    target = send.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--to",
        help="Recipient agent id, or a harness name when it has exactly one live session.",
    )
    target.add_argument("--channel", help="Channel name (same charset and denylist as an agent id).")
    send.add_argument("--body", help=BODY_ARG_HELP)
    send.add_argument("--body-stdin", action="store_true", help=BODY_STDIN_HELP)
    send.add_argument(
        "--kind",
        choices=MESSAGE_KINDS,
        default=DEFAULT_MESSAGE_KIND,
        help="Message kind; a label only, no kind triggers an automatic action.",
    )
    send.add_argument("--reply-to", help="Id of the message being answered.")
    send.add_argument(
        "--new-recipient",
        action="store_true",
        help="Deliberate first contact with a canonical agent id that has not joined Office yet "
        "(no presence or send history); the message is marked first_contact.",
    )
    send.add_argument(
        "--ref",
        dest="refs",
        action="append",
        default=None,
        metavar="REF",
        help=(
            "Reference for the envelope `refs` list (repeatable, at most 16): a "
            "repo-relative path or a 32-hex id. A hint for the reader -- Office never "
            "opens the file it names. Required for --kind handoff (code refs_required)."
        ),
    )
    send.add_argument(
        "--dedupe-key",
        help=(
            "Retry key: resending with the same key and payload returns the stored message "
            "(duplicate=true) instead of a second copy; another payload is refused."
        ),
    )
    ack = sub.add_parser(
        "ack", help="Explicitly acknowledge one received message (idempotent; authorizes nothing)."
    )
    ack.add_argument("--id", required=True, help="Id of the acknowledged message (32 hex).")
    ack.add_argument("--note", help="Short statement of the effect on your work.")
    delivery = sub.add_parser(
        "delivery", help="Read-only written/received/acknowledged state of one channel post."
    )
    delivery.add_argument("--id", required=True)
    delivery.add_argument("--channel", required=True)
    dopen = sub.add_parser(
        "dilemma-open",
        help="Post one dilemma packet (question + 2-6 options) on a task channel; one open per agent and channel.",
    )
    dopen.add_argument("--channel", required=True)
    dopen.add_argument("--question", help="Question text (or --body-stdin).")
    dopen.add_argument("--body-stdin", action="store_true", help=BODY_STDIN_HELP)
    dopen.add_argument("--option", dest="options", action="append", default=[], metavar="KEY=TEXT",
                       help="Option (repeatable, 2-6).")
    dopen.add_argument("--correlation-id", help="Stable id; the same id and payload is a safe retry.")
    dopen.add_argument("--to", help="Also copy the packet to this agent's inbox (architect or lead).")
    dopen.add_argument("--ref", dest="refs", action="append", default=None, metavar="REF")
    danswer = sub.add_parser("dilemma-answer", help="Answer one dilemma once (kind=decision reply).")
    danswer.add_argument("--id", required=True)
    danswer.add_argument("--choice", required=True, help="Option key or other.")
    danswer.add_argument("--rationale", required=True)
    dstatus = sub.add_parser("dilemma-status", help="Read-only state of one dilemma.")
    dstatus.add_argument("--id", required=True)
    ask = sub.add_parser("ask", help="Queue a durable question (offline recipients read it later).")
    ask_target = ask.add_mutually_exclusive_group(required=True)
    ask_target.add_argument(
        "--to",
        help="Recipient agent id, or a harness name when it has exactly one live session.",
    )
    ask_target.add_argument("--channel", help="Channel name.")
    ask.add_argument("--body", help=ASK_BODY_ARG_HELP)
    ask.add_argument("--body-stdin", action="store_true", help=BODY_STDIN_HELP)
    ask.add_argument("--timeout", type=float, default=30.0, help="Seconds to await a correlated reply.")

    read = sub.add_parser("read", help="Read new messages as untrusted_message frames.")
    read.add_argument("--channel", help="Read this channel instead of the own inbox (own cursor per channel).")
    read.add_argument("--limit", type=int, default=DEFAULT_READ_LIMIT)
    read.add_argument("--peek", action="store_true", help="Do not advance the cursor.")
    read.add_argument(
        "--kind",
        choices=MESSAGE_KINDS,
        default=None,
        help="Return only messages of this kind (a query: the cursor is not advanced).",
    )

    correlated = sub.add_parser(
        "read-correlated-replies", help="Read retained handoff replies for declared parent receipts."
    )
    correlated.add_argument("--project-slug", required=True)
    correlated.add_argument("--parent-receipt", action="append", required=True,
                             help="Canonical 32-hex parent message id; repeat 1..64 times.")
    correlated.add_argument("--limit", type=int, default=5)

    checkpoint = sub.add_parser(
        "checkpoint",
        help="Refresh presence and read the private inbox plus one task channel.",
    )
    checkpoint.add_argument("--channel", required=True, help="Exact task channel to read.")
    checkpoint.add_argument("--workspace", required=True, help="Working directory to announce.")
    checkpoint.add_argument("--ttl", type=int, default=900, help="Presence lease duration in seconds.")
    checkpoint.add_argument("--limit", type=int, default=5, help="Maximum messages from each stream (1..500).")
    checkpoint.add_argument("--status", default="busy", help="Presence status text (default: busy).")
    checkpoint.add_argument("--peek", action="store_true", help="Read both streams without advancing cursors.")

    wake_enroll = sub.add_parser(
        "wake-enroll", help="Enroll one CLI thread in Office Wake; verification remains false."
    )
    wake_enroll.add_argument("--binding-id", required=True)
    wake_enroll.add_argument("--office-agent", required=True)
    wake_enroll.add_argument("--driver", required=True, choices=SUPPORTED_DRIVERS)
    wake_enroll.add_argument("--native-thread-id", required=True)
    wake_enroll.add_argument("--workspace", required=True, help="Existing absolute workspace path.")
    wake_enroll.add_argument("--execution-mode", choices=("local", "host"), default="local")
    wake_enroll.add_argument("--execution-host")
    wake_enroll.add_argument("--executor-agent")
    wake_enroll.add_argument("--workspace-flavor", choices=("windows", "posix"))
    wake_enroll.add_argument("--enrolled-by", required=True)
    wake_enroll.add_argument("--allowed-waker", action="append", required=True)
    wake_enroll.add_argument(
        "--allowed-trigger", action="append", choices=SUPPORTED_TRIGGERS, default=None
    )
    wake_enroll.add_argument("--wake-kind", action="append", choices=MESSAGE_KINDS, default=None)
    wake_enroll.add_argument("--budget-per-day", type=int, default=DEFAULT_BUDGET_PER_DAY)
    wake_enroll.add_argument(
        "--max-attempts-per-trigger", type=int, default=DEFAULT_MAX_ATTEMPTS_PER_TRIGGER
    )
    wake_enroll.add_argument("--expires-at", required=True, help="ISO-8601 expiry timestamp.")
    wake_enroll.add_argument(
        "--correlation-required",
        action="store_true",
        help="Wake v2: wake only on replies to this session's open task correlations.",
    )
    wake_enroll.add_argument(
        "--owner-confirm",
        action="store_true",
        help="Explicit owner confirmation required for enrollment.",
    )

    wake_verify = sub.add_parser("wake-verify", help="Mark one enrolled binding verified.")
    wake_verify.add_argument("--binding-id", required=True)
    wake_verify.add_argument(
        "--owner-confirm",
        action="store_true",
        help="Explicit owner confirmation required for verification.",
    )

    wake_pause = sub.add_parser("wake-pause", help="Pause one Office Wake binding.")
    wake_pause.add_argument("--binding-id", required=True)

    wake_revoke = sub.add_parser("wake-revoke", help="Revoke one Office Wake binding.")
    wake_revoke.add_argument("--binding-id", required=True)

    wake_list = sub.add_parser("wake-list", help="List one page of Office Wake bindings.")
    wake_list.add_argument("--limit", type=int, default=20)
    wake_list.add_argument("--offset", type=int, default=0)
    wake_expect = sub.add_parser(
        "wake-expect",
        help="Wake v2: expect a reply to a message YOU sent (caller = the bound session).",
    )
    wake_expect.add_argument("--binding-id", required=True)
    wake_expect.add_argument("--task-id", required=True)
    wake_expect.add_argument("--correlation-id", required=True)
    wake_expect.add_argument("--counterpart", required=True, help="Office address expected to reply.")
    wake_expect.add_argument("--request-message-id", required=True, help="Id of the message you sent.")
    wake_expect.add_argument("--max-wakes", type=int, default=6)
    wake_expect.add_argument("--max-hops", type=int, default=12)
    wake_expect.add_argument("--ttl-s", type=int, default=3 * 60 * 60)
    wake_consume = sub.add_parser(
        "wake-consume", help="Wake v2: record that you processed one reply (ACK/read never implies it)."
    )
    wake_consume.add_argument("--correlation-id", required=True)
    wake_consume.add_argument("--trigger-message-id", required=True)
    wake_close = sub.add_parser("wake-close", help="Wake v2: close your correlation.")
    wake_close.add_argument("--correlation-id", required=True)
    wake_task_cancel = sub.add_parser(
        "wake-task-cancel", help="Wake v2: bump the task epoch; older replies stop waking."
    )
    wake_task_cancel.add_argument("--task-id", required=True)
    sub.add_parser("wake-correlations", help="Wake v2: list correlations and their state.")
    wake_pull_arm = sub.add_parser(
        "wake-pull-arm",
        help="Self-pull: arm one idle-only timer for your open correlation (deduplicated).",
    )
    wake_pull_arm.add_argument("--binding-id", required=True)
    wake_pull_arm.add_argument("--max-pulls", type=int, default=None)
    wake_pull_arm.add_argument(
        "--lost",
        action="store_true",
        help="Replace a confirmed timer only after CronList shows no job with its token.",
    )
    wake_pull = sub.add_parser(
        "wake-pull", help="Self-pull: one check from your timer; follow its action field."
    )
    wake_pull.add_argument("--binding-id", required=True)
    wake_pull.add_argument("--token", required=True)
    wake_pull_confirm = sub.add_parser(
        "wake-pull-confirm", help="Self-pull: bind the created timer job; delete_job means CronDelete it."
    )
    wake_pull_confirm.add_argument("--binding-id", required=True)
    wake_pull_confirm.add_argument("--token", required=True)
    wake_pull_confirm.add_argument("--job-id", required=True)
    wake_pull_disarm = sub.add_parser("wake-pull-disarm", help="Self-pull: stop your timer chain.")
    wake_pull_disarm.add_argument("--binding-id", required=True)
    wake_intake = sub.add_parser(
        "wake-intake",
        help=(
            "Owner-confirm one prompt to an enrolled manual binding. Disabled unless "
            "OFFICE_WAKE_MANUAL_INTAKE_ENABLED=1."
        ),
    )
    wake_intake.add_argument("--binding-id", required=True)
    wake_intake.add_argument("--expected-version", required=True, type=int)
    wake_intake.add_argument("--idempotency-key", required=True)
    wake_intake.add_argument(
        "--owner-confirm",
        action="store_true",
        help="Assert owner confirmation; this flag alone is not an identity credential.",
    )
    wake_intake.add_argument(
        "--prompt-stdin",
        action="store_true",
        required=True,
        help="Read the UTF-8 prompt from stdin (max 16 KiB); no prompt argument is accepted.",
    )
    sub.add_parser("wake-stop", help="Create the global Office Wake STOP file.")
    sub.add_parser("wake-start", help="Clear STOP by atomically archiving its receipt.")
    sub.add_parser(
        "wake-stop-status",
        help="Read-only: report whether the global Wake STOP is active (no writes, no tick).",
    )

    wake_tick = sub.add_parser("wake-tick", help="Run one bounded pass over all Wake bindings.")
    wake_tick.add_argument(
        "--dry-run", action="store_true", help="Show gate decisions without invoking a driver."
    )
    wake_tick.add_argument(
        "--binding-id", help="Only this binding's message trigger; no child events, no schedules."
    )

    wake_ledger = sub.add_parser("wake-ledger", help="Read the newest Office Wake ledger rows.")
    wake_ledger.add_argument("--limit", type=int, default=50)
    wake_ledger.add_argument("--binding-id")

    wake_relay_pending = sub.add_parser(
        "wake-relay-pending",
        help="List eligible pending Desktop relay dispatches.",
    )
    wake_relay_pending.add_argument("--binding-id", help="Only dispatches of this binding.")
    wake_relay_pending.add_argument('--include-owned', action='store_true')
    wake_relay_pending.add_argument('--relay-agent')
    wake_relay_pending.add_argument("--driver", default="claude-desktop-relay",
                                     choices=sorted(EXTERNAL_RELAY_DRIVERS))
    wake_relay_verify = sub.add_parser(
        "wake-relay-verify", help="Verify a relay dispatch against a local allow file."
    )
    wake_relay_verify.add_argument("--allow-file", required=True, type=Path)
    wake_relay_source = wake_relay_verify.add_mutually_exclusive_group(required=True)
    wake_relay_source.add_argument("--dispatch-json")
    wake_relay_source.add_argument("--dispatch-file", type=Path)
    wake_relay_watch = sub.add_parser(
        "wake-relay-watch",
        help="Event-driven relay: block (no model) until one of the relay bindings has a pending dispatch.",
    )
    wake_relay_watch.add_argument("--binding-id", required=True, action="append",
                                  help="Relay binding to watch; repeat for several (max 16).")
    wake_relay_watch.add_argument("--interval-s", type=int, default=20)
    wake_relay_watch.add_argument("--timeout-s", type=int, default=3600)
    wake_relay_watch.add_argument(
        "--delay-s", type=int, default=0, help="Wait before the first check (after a busy deferral)."
    )
    wake_relay_watch.add_argument("--driver", default="claude-desktop-relay",
                                   choices=sorted(EXTERNAL_RELAY_DRIVERS))
    wake_relay_expect = sub.add_parser(
        "wake-relay-expect",
        help="Owner: mark (or --off unmark) a relay binding as served by an event-driven watcher.",
    )
    wake_relay_expect.add_argument("--binding-id", required=True)
    wake_relay_expect.add_argument("--off", action="store_true")
    wake_relay_expect.add_argument("--owner-confirm", action="store_true")
    wake_relay_expect.add_argument("--recover", action="store_true",
                                   help="Move an unreadable marker aside and start a new list.")
    wake_monitor = sub.add_parser(
        "wake-monitor", help="One no-model monitor pass; --post sends each finding as an alert."
    )
    wake_monitor.add_argument("--post", action="store_true",
                              help="Post findings as kind=alert to the channel (deduplicated).")
    wake_monitor.add_argument("--channel", default="ops")
    wake_relay_claim = sub.add_parser(
        "wake-relay-claim", help="Atomically claim one pending relay dispatch."
    )
    wake_relay_claim.add_argument("--dispatch-id", required=True)
    wake_relay_claim.add_argument("--relay-agent", required=True)
    wake_relay_claim.add_argument("--driver", default="claude-desktop-relay",
                                   choices=sorted(EXTERNAL_RELAY_DRIVERS))
    wake_relay_ack = sub.add_parser(
        "wake-relay-ack", help="Read a correlated recipient ACK for an owned native dispatch."
    )
    wake_relay_ack.add_argument("--dispatch-id", required=True)
    wake_relay_ack.add_argument("--driver", required=True,
                               choices=["codex-desktop-relay", "zcode-desktop-relay",
                                        "antigravity-desktop-relay"])
    wake_relay_defer_busy = sub.add_parser(
        "wake-relay-defer-busy",
        help=(
            "Record a target-busy deferral of a pending dispatch before claim (bounded "
            "backoff; the sixth call cancels it as target_busy_limit)."
        ),
    )
    wake_relay_defer_busy.add_argument("--dispatch-id", required=True)
    wake_relay_defer_busy.add_argument("--driver", default="claude-desktop-relay",
                                        choices=sorted(EXTERNAL_RELAY_DRIVERS))
    wake_relay_receipt = sub.add_parser(
        "wake-relay-receipt", help="Record the native send_message result for a claim."
    )
    wake_relay_receipt.add_argument("--dispatch-id", required=True)
    wake_relay_receipt.add_argument("--status", required=True, choices=RELAY_RECEIPT_STATUSES)
    wake_relay_receipt.add_argument("--message-id", default=None,
                                     help="Native send_message message id (when available).")
    wake_relay_receipt.add_argument("--turn-id", default=None,
                                     help="Native turn id when no message_id exists (Codex Desktop).")
    wake_relay_receipt.add_argument("--detail")
    wake_relay_receipt.add_argument("--driver", default="claude-desktop-relay",
                                     choices=sorted(EXTERNAL_RELAY_DRIVERS))
    wake_relay_observe = sub.add_parser(
        "wake-relay-observe", help="Record a later observed target session stage."
    )
    wake_relay_observe.add_argument("--dispatch-id", required=True)
    wake_relay_observe.add_argument('--native-proof-json', type=json.loads)
    wake_relay_observe.add_argument(
        "--stage", required=True, choices=RELAY_OBSERVATION_STAGES
    )
    wake_relay_observe.add_argument("--driver", default="claude-desktop-relay",
                                    choices=sorted(EXTERNAL_RELAY_DRIVERS))
    wake_relay_reconcile = sub.add_parser(
        "wake-relay-reconcile",
        help=(
            "Owner-reconcile a delivery_unknown dispatch, or an unconfirmed/error one "
            "with a native message id; never resends."
        ),
    )
    wake_relay_reconcile.add_argument("--dispatch-id", required=True)
    wake_relay_reconcile.add_argument(
        "--outcome", required=True, choices=("delivered", "not_delivered")
    )
    wake_relay_reconcile.add_argument(
        "--owner-confirm",
        action="store_true",
        help="Explicit owner confirmation required for delivery reconciliation.",
    )
    wake_relay_native_begin = sub.add_parser(
        "wake-relay-native-begin",
        help="Fence a native submission durably BEFORE crossing the provider seam.",
    )
    wake_relay_native_begin.add_argument("--dispatch-id", required=True)
    wake_relay_native_begin.add_argument("--relay-agent", required=True)
    wake_relay_native_begin.add_argument('--prior-turn-id', help='Full UUID or literal null for a proven empty baseline.')
    wake_relay_native_begin.add_argument('--baseline-observed-at', type=float)
    wake_relay_native_begin.add_argument("--driver", default="claude-desktop-relay",
                                          choices=sorted(EXTERNAL_RELAY_DRIVERS))
    wake_relay_native_unsubmitted = sub.add_parser(
        "wake-relay-native-unsubmitted",
        help="Return only this caller's pre-seam claim to pending.",
    )
    wake_relay_native_unsubmitted.add_argument("--dispatch-id", required=True)
    wake_relay_native_unsubmitted.add_argument("--relay-agent", required=True)
    wake_relay_native_unsubmitted.add_argument("--driver", default="claude-desktop-relay",
                                                choices=sorted(EXTERNAL_RELAY_DRIVERS))

    wake_schedule_add = sub.add_parser(
        "wake-schedule-add", help="Add one owner-approved local Office Wake schedule."
    )
    wake_schedule_add.add_argument("--schedule-id", required=True)
    wake_schedule_add.add_argument("--binding-id", required=True)
    wake_schedule_add.add_argument("--time", required=True, help="Local time in HH:MM format.")
    wake_schedule_add.add_argument("--days", required=True, choices=("daily", "weekdays"))
    wake_schedule_add.add_argument("--task-text", required=True)
    wake_schedule_add.add_argument(
        "--grace-minutes", type=int, default=DEFAULT_SCHEDULE_GRACE_MINUTES
    )
    wake_schedule_add.add_argument(
        "--owner-confirm",
        action="store_true",
        help="Explicit owner confirmation required for schedule creation.",
    )
    sub.add_parser("wake-schedule-list", help="List Office Wake schedules.")
    wake_schedule_disable = sub.add_parser(
        "wake-schedule-disable", help="Disable one Office Wake schedule."
    )
    wake_schedule_disable.add_argument("--schedule-id", required=True)
    wake_schedule_enable = sub.add_parser(
        "wake-schedule-enable", help="Enable one Office Wake schedule."
    )
    wake_schedule_enable.add_argument("--schedule-id", required=True)

    knowledge = sub.add_parser(
        "knowledge-sync",
        help="Send pending channel knowledge to Knowledge Hub through its canonical gateway.",
    )
    knowledge.add_argument("--project-slug", required=True)
    knowledge.add_argument(
        "--channel",
        dest="channels",
        action="append",
        default=None,
        help="Channel to scan (repeatable; defaults to tablica).",
    )
    knowledge.add_argument(
        "--dry-run",
        action="store_true",
        help="Render the pending digest without calling Knowledge Hub or moving the bridge cursor.",
    )

    wait = sub.add_parser("wait", help="Wait using a local Windows named-pipe wake signal.")
    wait.add_argument("--agent", dest="agent", default=argparse.SUPPRESS,
                      help="Explicit agent id (also accepted before the command).")
    wait.add_argument("--channel", help="Wait on this channel instead of the own inbox.")
    wait.add_argument("--timeout", type=float, default=30.0, help="Seconds to wait.")

    sub.add_parser(
        "channels",
        help="List every channel on disk plus the default board `tablica` (pending is per reader).",
    )

    presence = sub.add_parser("presence", help="Announce/refresh presence as a lease on agent:<id>.")
    presence.add_argument("--ttl", type=int, default=DEFAULT_PRESENCE_TTL_S)
    presence.add_argument("--status", help="Short free-text status.")
    presence.add_argument(
        "--workspace",
        help=(
            "Working directory to announce; stored canonicalized as `workspace` in the presence "
            "lease meta. When it lies inside a repository, the repository id is stored as `repo` "
            "-- read from `.git` (its `gitdir:`/`commondir` files) with no process started, so "
            "two worktrees of one repository share it; outside a repository or on any read "
            "failure the field is simply left out."
        ),
    )
    presence.add_argument("--release", action="store_true", help="Release presence instead of claiming.")

    who = sub.add_parser("who", help="List agents with a live presence lease.")
    who.add_argument(
        "--workspace",
        help=(
            "List only agents whose announced workspace is this directory, a parent of it, or "
            "inside it -- or, when those are unrelated, another worktree of the same repository "
            "(`relation: same-repo`)."
        ),
    )

    mcp = sub.add_parser("mcp", help="Run the stdio MCP server (no network).")
    mcp.add_argument("--ttl", type=int, default=DEFAULT_PRESENCE_TTL_S, help="Presence TTL for the heartbeat.")
    mcp.add_argument("--no-heartbeat", action="store_true", help="Do not auto-claim presence.")
    mcp.add_argument(
        "--profile",
        choices=("full", "contractor"),
        default="full",
        help=(
            "contractor: only read/send/presence/wait/ack/delivery/dilemma, limited to --channel "
            "task channels, the own inbox and --peer recipients, with a per-run message, read and "
            "wait budget (see the --max-* and --read-* flags)."
        ),
    )
    mcp.add_argument("--channel", dest="mcp_channels", action="append", default=[],
                     help="Task channel allowed in the contractor profile (repeatable).")
    mcp.add_argument("--peer", dest="mcp_peers", action="append", default=[],
                     help="Agent id a contractor may message directly (repeatable).")
    # Contractor budget overrides. Absent flags keep the defaults in office_mcp; any of them
    # with another profile is refused, so a tuning flag never silently does nothing.
    mcp.add_argument("--max-send-bytes", type=int, default=None,
                     help=f"Contractor: largest message body in UTF-8 bytes (default {CONTRACTOR_MAX_SEND_BYTES}).")
    mcp.add_argument("--read-page", type=int, default=None,
                     help=f"Contractor: most messages one office_read returns (default {CONTRACTOR_READ_PAGE}).")
    mcp.add_argument("--read-body-bytes", type=int, default=None,
                     help=f"Contractor: each read body is cut to this many bytes (default {CONTRACTOR_READ_BODY_BYTES}).")
    mcp.add_argument("--max-read-bytes", type=int, default=None,
                     help=f"Contractor: read budget per run in bytes (default {CONTRACTOR_READ_BUDGET_BYTES}).")
    mcp.add_argument("--max-waits", type=int, default=None,
                     help=f"Contractor: office_wait calls per run (default {CONTRACTOR_MAX_WAITS}).")

    thread_reg = sub.add_parser("thread-register", help="Register one evidenced thread.")
    thread_reg.add_argument("--record-json")
    thread_reg.add_argument("--record-file", type=Path)
    thread_res = sub.add_parser("thread-resolve", help="Resolve a full ID or exact tuple.")
    thread_res.add_argument("--office-agent")
    for name in ("project-slug", "tool", "surface", "title"):
        thread_res.add_argument("--" + name)
    thread_list = sub.add_parser("thread-list", help="List registered threads.")
    for name in ("project-slug", "tool", "surface", "status"):
        thread_list.add_argument("--" + name)
    thread_list.add_argument("--limit", type=int, default=20)
    thread_list.add_argument("--offset", type=int, default=0)
    view_import = sub.add_parser("project-view-import", help="Import validated readonly project metadata.")
    view_import.add_argument("--record-json")
    view_import.add_argument("--record-file", type=Path)
    view_get = sub.add_parser("project-view-get", help="Read one project metadata row.")
    view_get.add_argument("--project-slug", required=True)
    sub.add_parser("project-view-list", help="List imported project metadata.")
    goal_add = sub.add_parser("goal-add", help="Add one project goal.")
    goal_add.add_argument("--record-json")
    goal_add.add_argument("--record-file", type=Path)
    goal_update = sub.add_parser("goal-update", help="Update one project goal.")
    goal_update.add_argument("--goal-id", required=True)
    goal_update.add_argument("--record-json")
    goal_update.add_argument("--record-file", type=Path)
    goal_get = sub.add_parser("goal-get", help="Read one project goal.")
    goal_get.add_argument("--goal-id", required=True)
    goal_list = sub.add_parser("goal-list", help="List project goals.")
    goal_list.add_argument("--project-slug")

    reg = sub.add_parser("project-register", help="Register a project id bound to one checkout.")
    reg.add_argument("--project-id", required=True)
    reg.add_argument("--checkout", required=True, help="Absolute checkout path.")
    reg.add_argument("--alias", action="append", default=[], help="Repeatable alias.")

    res = sub.add_parser("project-resolve", help="Read one canonical project record.")
    res.add_argument("--project", required=True, help="Project id or alias.")

    stream = sub.add_parser("project-stream", help="Register a task/thread/run stream.")
    stream.add_argument("--project", required=True)
    stream.add_argument("--task", required=True)
    stream.add_argument("--thread", required=True)
    stream.add_argument("--run", required=True)

    activity = sub.add_parser(
        "project-activity",
        help="Record one bounded typed provider event; prompts, tool output and secrets are refused.",
    )
    _add_stream(activity)
    activity_input = activity.add_mutually_exclusive_group(required=True)
    activity_input.add_argument(
        "--activity-json", help="Typed activity object, no raw body or transcript."
    )
    activity_input.add_argument(
        "--activity-file", help="UTF-8 JSON file containing one typed activity object (max 64 KiB)."
    )

    activity_read = sub.add_parser(
        "project-activity-read", help="Read bounded typed provider activity for one project stream."
    )
    _add_stream(activity_read)
    activity_read.add_argument("--limit", type=int, default=50)

    audits = sub.add_parser("project-audits", help="List authority-claim audit rejections.")
    audits.add_argument("--project", required=True)

    psend = sub.add_parser("project-send", help="Send one direct project message (delivery is not an ACK).")
    _add_stream(psend)
    psend.add_argument("--recipient-json", required=True, help='Recipient identity, e.g. {"harness":"codex","host":"h","session_id":"s"}.')
    psend.add_argument("--body", required=True)
    psend.add_argument("--kind", choices=MESSAGE_KINDS, default=DEFAULT_MESSAGE_KIND)
    psend.add_argument("--ack-ttl", type=int, help="Application ACK TTL in seconds.")

    pcsend = sub.add_parser("project-send-channel", help="Send one project channel message.")
    _add_stream(pcsend, channel=True)
    pcsend.add_argument("--body", required=True)
    pcsend.add_argument("--kind", choices=MESSAGE_KINDS, default=DEFAULT_MESSAGE_KIND)
    pcsend.add_argument("--ack-ttl", type=int, help="Application ACK TTL in seconds.")

    pread = sub.add_parser("project-read", help="Read one project stream as untrusted frames.")
    _add_stream(pread)
    pread.add_argument("--limit", type=int, default=None)
    pread.add_argument("--peek", action="store_true")

    prread = sub.add_parser("project-read-channel", help="Read one project stream channel.")
    _add_stream(prread, channel=True)
    prread.add_argument("--limit", type=int, default=None)
    prread.add_argument("--peek", action="store_true")

    pwait = sub.add_parser("project-wait", help="Wait on one deterministic project inbox.")
    _add_stream(pwait)
    pwait.add_argument("--timeout", type=float, default=30.0)

    pcwait = sub.add_parser("project-wait-channel", help="Wait on one deterministic project channel.")
    _add_stream(pcwait, channel=True)
    pcwait.add_argument("--timeout", type=float, default=30.0)

    for name, text in (
        ("project-acknowledge", "Record the application ACK for one project delivery."),
        ("project-delivery", "Inspect one project delivery without side effects."),
        ("project-cancel", "Cancel one project delivery (never retries)."),
    ):
        cmd = sub.add_parser(name, help=text)
        cmd.add_argument("--project-id", required=True)
        cmd.add_argument("--delivery", required=True, help="Delivery id to act on.")
        _add_sender(cmd)

    jexport = sub.add_parser("journal-export", help="Append one work receipt to the journal.")
    jexport.add_argument("--record-json", required=True, help="One office-work.v1 shaped record.")

    jcommit = sub.add_parser("journal-checkpoint", help="Write one immutable journal checkpoint.")
    jcommit.add_argument("--record-json", required=True, help="One checkpoint-carrying record.")

    jread = sub.add_parser("journal-read-checkpoint", help="Read a checkpoint receipt (never reserves work).")
    jread.add_argument("--project-id", required=True)
    jread.add_argument("--task-id", required=True)
    jread.add_argument("--checkpoint-id", help="Newest checkpoint when omitted.")

    sub.add_parser("journal-audit", help="Read bounded journal audit events.")
    sub.add_parser("journal-reconcile", help="Inspect journal consistency without repairing.")

    wreg = sub.add_parser("work-register", help="Register one active-work lifecycle task.")
    wreg.add_argument("--project", required=True)
    wreg.add_argument("--task", required=True)
    wreg.add_argument("--checkout", required=True, help="Absolute existing checkout directory.")
    wreg.add_argument("--scope", action="append", required=True, help="Repeatable write scope.")
    wreg.add_argument("--dependency", action="append", default=[])
    wreg.add_argument("--base-sha", help="40 or 64 hex commit sha, when known.")
    wreg.add_argument("--commit-intent")
    wreg.add_argument("--journal-ref", action="append", default=[])
    wreg.add_argument("--status", default="active")
    wreg.add_argument("--unknown-reasons-json", help="JSON mapping for missing commit evidence.")
    wreg.add_argument("--deferred-reason")

    wget = sub.add_parser("work-get", help="Read one active-work record.")
    wget.add_argument("--project", required=True)
    wget.add_argument("--task", required=True)

    wlist = sub.add_parser("work-list", help="List active-work records.")
    wlist.add_argument("--project")
    wlist.add_argument("--checkout")

    wupdate = sub.add_parser("work-update", help="Update supplied active-work fields only.")
    wupdate.add_argument("--project", required=True)
    wupdate.add_argument("--task", required=True)
    wupdate.add_argument("--checkout")
    wupdate.add_argument("--scope", action="append")
    wupdate.add_argument("--dependency", action="append")
    wupdate.add_argument("--base-sha")
    wupdate.add_argument("--commit-intent")
    wupdate.add_argument("--journal-ref", action="append")
    wupdate.add_argument("--status")
    wupdate.add_argument("--unknown-reasons-json")
    wupdate.add_argument("--deferred-reason")

    wbadd = sub.add_parser("work-blocker-add", help="Attach one named blocker to a task.")
    wbadd.add_argument("--project", required=True)
    wbadd.add_argument("--task", required=True)
    wbadd.add_argument("--blocker", required=True)
    wbadd.add_argument("--reason", required=True)
    wbadd.add_argument("--dependency", action="append", default=[])

    wbt = sub.add_parser("work-blocker-transition", help="Move one blocker to a new state.")
    wbt.add_argument("--project", required=True)
    wbt.add_argument("--task", required=True)
    wbt.add_argument("--blocker", required=True)
    wbt.add_argument("--status", required=True)
    wbt.add_argument("--reason", required=True)

    wprec = sub.add_parser("work-plan-record", help="Record one scoped integration handoff.")
    wprec.add_argument("--project", required=True)
    wprec.add_argument("--task", required=True)
    wprec.add_argument("--from-agent", required=True)
    wprec.add_argument("--to-agent", required=True)
    wprec.add_argument("--scope", action="append", required=True)
    wprec.add_argument("--summary", required=True)
    wprec.add_argument("--journal-ref", action="append", default=[])

    wplist = sub.add_parser("work-plan-list", help="List handoffs proposed for one task.")
    wplist.add_argument("--project", required=True)
    wplist.add_argument("--task", required=True)

    wpack = sub.add_parser("work-plan-acknowledge", help="Acknowledge one handoff (evidence only).")
    wpack.add_argument("--project", required=True)
    wpack.add_argument("--task", required=True)
    wpack.add_argument("--plan", required=True, help="Plan id to acknowledge.")
    wpack.add_argument("--ack-agent", required=True, help="Acknowledging agent id.")
    wpack.add_argument("--status", required=True)
    wpack.add_argument("--note")

    cadvise = sub.add_parser("conflict-advise", help="Warning-only advice for one observed lease result.")
    cadvise.add_argument("--lease-json", required=True, help="Already-observed lease result object.")
    cadvise.add_argument("--lineage-json", help="Requester lineage object.")

    wproj = sub.add_parser("work-projection", help="Project approved work records (no I/O).")
    wproj.add_argument("--project-id", required=True)
    wproj.add_argument("--records-json", required=True, help="JSON array of candidate records.")
    return parser


def _resolve_body(args: argparse.Namespace) -> str:
    """The one body of `send`/`ask`: --body or --body-stdin, never both, never neither.

    Both at once is a refusal, not a silent choice, and stdin is read only when
    --body-stdin asks for it -- otherwise an invocation with no stdin redirect
    would hang. The text from stdin is returned raw: `send`/`ask` validate it
    through the same `_validate_body` as an argument, so the 16 KB cap, the
    empty-body refusal and the secret preflight are one path for both inputs.
    """
    from_stdin = bool(getattr(args, "body_stdin", False))
    if from_stdin and args.body is not None:
        raise CoordError(
            "--body and --body-stdin are mutually exclusive: give the body once",
            code="invalid_body",
        )
    if from_stdin:
        return read_body_from_stdin()
    if args.body is None:
        raise CoordError(
            "no body given: pass --body <text>, or --body-stdin to read it from stdin",
            code="invalid_body",
        )
    return args.body


def _parse_json(value: str | None, label: str) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise CoordError(f"{label} must be JSON: {exc}", code="invalid_json") from exc


def _parse_json_file(value: str | Path | None, label: str, *, max_bytes: int = 64 * 1024) -> Any:
    if value is None:
        return None
    path = Path(value)
    try:
        if not path.is_file():
            raise CoordError(f"{label} must point to a regular file", code="invalid_json")
        raw = path.read_bytes()
    except CoordError:
        raise
    except OSError as exc:
        raise CoordError(f"{label} could not be read: {exc}", code="invalid_json") from exc
    if len(raw) > max_bytes:
        raise CoordError(f"{label} exceeds the {max_bytes} byte limit", code="invalid_json")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CoordError(f"{label} must be UTF-8 JSON: {exc}", code="invalid_json") from exc
    return _parse_json(text, label)


def _add_sender(parser: argparse.ArgumentParser) -> None:
    """Explicit project-transport sender triple; falls back to the office identity."""
    parser.add_argument("--sender-harness", help="Explicit sender harness (needs --sender-host and --sender-session too).")
    parser.add_argument("--sender-host", help="Explicit sender host.")
    parser.add_argument("--sender-session", help="Explicit sender session id.")


def _sender(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "harness": getattr(args, "sender_harness", None),
        "host": getattr(args, "sender_host", None),
        "session_id": getattr(args, "sender_session", None),
    }


def _add_stream(parser: argparse.ArgumentParser, *, channel: bool = False) -> None:
    parser.add_argument("--project-id", required=True, help="Registered project id.")
    parser.add_argument("--task", required=True, help="Task correlation component.")
    parser.add_argument("--thread", required=True, help="Thread correlation component.")
    parser.add_argument("--run", required=True, help="Run correlation component.")
    if channel:
        parser.add_argument("--channel", required=True, help="Stream channel name.")
    _add_sender(parser)


CONTRACTOR_LIMIT_FLAGS = (
    ("max_send_bytes", "max_send_bytes", "--max-send-bytes"),
    ("read_page", "read_page", "--read-page"),
    ("read_body_bytes", "read_body_bytes", "--read-body-bytes"),
    ("max_read_bytes", "read_budget_bytes", "--max-read-bytes"),
    ("max_waits", "max_waits", "--max-waits"),
)


def contractor_limits_from_args(args: argparse.Namespace) -> ContractorLimits | None:
    """Contractor budget from `mcp` flags: None keeps the defaults of the contractor profile.

    Validated before the server starts, so a bad value is a JSON error with exit 1 instead
    of a server that never comes up.
    """
    given = {field: getattr(args, attr) for attr, field, _flag in CONTRACTOR_LIMIT_FLAGS
             if getattr(args, attr, None) is not None}
    if not given:
        return None
    if args.profile != "contractor":
        flags = ", ".join(flag for attr, _field, flag in CONTRACTOR_LIMIT_FLAGS
                          if getattr(args, attr, None) is not None)
        raise CoordError(f"{flags} apply only to --profile contractor", code="invalid_mcp_limits")
    try:
        return ContractorLimits(**given)
    except ValueError as exc:
        raise CoordError(str(exc), code="invalid_mcp_limits") from exc


def build_office(args: argparse.Namespace) -> Office:
    home = args.home if args.home is not None else getattr(args, "runtime_home", None)
    return Office(agent_id=args.agent, harness=args.harness, session_id=args.session_id, home=home)


def _checkpoint_identity(args: argparse.Namespace) -> None:
    """Require the session-bound identity path used by native adapters."""
    if args.agent is not None:
        raise CoordError(
            "checkpoint requires a native session identity; do not override it with --agent",
            code="native_identity_required",
        )
    harness = args.harness or os.environ.get("COORDFS_HARNESS") or "unknown"
    identity = agent_identity(harness, session_id=args.session_id)
    if identity.get("strength") != "strong" or identity.get("source") == "process-fallback":
        raise CoordError(
            "checkpoint requires an existing native session id; pass --session-id or use "
            "the adapter session environment",
            code="native_identity_required",
        )


def _checkpoint_preflight(args: argparse.Namespace, *, remote: bool) -> str:
    """Validate every caller-controlled value before refreshing presence."""
    validate_ttl(args.ttl)
    if isinstance(args.limit, bool) or not isinstance(args.limit, int) or not (1 <= args.limit <= MAX_READ_LIMIT):
        raise CoordError(f"limit must be an integer in 1..{MAX_READ_LIMIT}", code="invalid_limit")
    channel = _bare_channel(args.channel)

    workspace = args.workspace
    if not isinstance(workspace, str) or not workspace.strip():
        raise CoordError("workspace must be a non-empty path", code="invalid_workspace")
    if len(workspace) > MAX_WORKSPACE_CHARS:
        raise CoordError(
            f"workspace must be at most {MAX_WORKSPACE_CHARS} characters", code="invalid_workspace"
        )
    secret = detect_secret(workspace)
    if secret is not None:
        raise CoordError(
            f"workspace matches a high-confidence secret pattern ({secret}); refused before write",
            code="secret_detected",
        )

    status = args.status
    if not isinstance(status, str) or len(status) > MAX_STATUS_CHARS or "\x00" in status:
        raise CoordError(f"status must be a string up to {MAX_STATUS_CHARS} characters", code="invalid_status")
    secret = detect_secret(status)
    if secret is not None:
        raise CoordError(
            f"status matches a high-confidence secret pattern ({secret}); refused before write",
            code="secret_detected",
        )

    if remote:
        if args.home is not None:
            raise CoordError("--remote-config cannot be combined with --home", code="remote_home_conflict")
    else:
        office = build_office(args)
        canonical_workspace = office._presence_workspace(workspace)
        office._presence_meta(status, canonical_workspace)
    return channel


def _checkpoint_step(operation: Any) -> dict[str, Any]:
    try:
        result = operation()
    except CoordError as exc:
        result = {"status": "error", "code": exc.code, "message": str(exc), **exc.payload}
    if not isinstance(result, dict):
        return {"status": "error", "code": "invalid_response", "message": "checkpoint step returned no object"}
    return result


def _checkpoint_result(
    channel_name: str,
    presence: dict[str, Any],
    inbox: dict[str, Any],
    channel: dict[str, Any],
) -> dict[str, Any]:
    failed = [
        name
        for name, result in (("presence", presence), ("inbox", inbox), ("channel", channel))
        if result.get("status") != "ok"
    ]
    return {
        "status": "partial" if failed else "ok",
        "code": "checkpoint_partial" if failed else "checkpoint_complete",
        "channel_name": channel_name,
        "failed_steps": failed,
        "presence": presence,
        "inbox": inbox,
        "channel": channel,
    }


def _dispatch_checkpoint_local(args: argparse.Namespace, channel_name: str) -> dict[str, Any]:
    office = build_office(args)
    presence = _checkpoint_step(
        lambda: office.presence(ttl_s=args.ttl, status=args.status, workspace=args.workspace)
    )
    inbox = _checkpoint_step(lambda: office.read(limit=args.limit, peek=bool(args.peek), channel=None))
    channel = _checkpoint_step(
        lambda: office.read(limit=args.limit, peek=bool(args.peek), channel=channel_name)
    )
    result = _checkpoint_result(channel_name, presence, inbox, channel)
    result["project_banner"] = office.checkpoint().get("project_banner")
    return result


def _dispatch_checkpoint_remote(args: argparse.Namespace, channel_name: str) -> dict[str, Any]:
    from .office_cli_remote import dispatch_remote, load_remote_config

    # Validate connection configuration before the first remote mutation. Each step still
    # uses the existing closed `presence` / `read` protocol routes and never falls back local.
    load_remote_config(args.remote_config)

    def call(command: str, *, channel: str | None = None) -> dict[str, Any]:
        step_args = argparse.Namespace(**vars(args))
        step_args.command = command
        if command == "presence":
            step_args.release = False
            step_args.channel = None
        else:
            step_args.channel = channel
            step_args.kind = None
        return dispatch_remote(step_args)

    presence = _checkpoint_step(lambda: call("presence"))
    inbox = _checkpoint_step(lambda: call("read", channel=None))
    channel = _checkpoint_step(lambda: call("read", channel=channel_name))
    result = _checkpoint_result(channel_name, presence, inbox, channel)
    banner = _checkpoint_step(lambda: call("checkpoint"))
    result["project_banner"] = banner.get("project_banner")
    if banner.get("status") != "ok":
        result["project_banner_status"] = banner.get("code", "unavailable")
    return result


def dispatch(args: argparse.Namespace) -> dict[str, Any] | None:
    if args.command == "checkpoint":
        _checkpoint_identity(args)
        channel_name = _checkpoint_preflight(args, remote=args.remote_config is not None)
        if args.remote_config is not None:
            return _dispatch_checkpoint_remote(args, channel_name)
        return _dispatch_checkpoint_local(args, channel_name)
    if getattr(args, "remote_config", None) is not None:
        from .office_cli_remote import dispatch_remote

        return dispatch_remote(args)
    command = args.command
    if command == "wake-relay-verify":
        allow = load_allow(args.allow_file)
        try:
            value = (_parse_json(args.dispatch_json, "--dispatch-json")
                     if args.dispatch_json is not None
                     else _parse_json_file(args.dispatch_file, "--dispatch-file"))
        except CoordError:
            value = None
        return verify_dispatch(value, allow)
    office = build_office(args)
    if command in {"thread-register", "project-view-import", "goal-add", "goal-update"}:
        if (args.record_json is None) == (args.record_file is None):
            raise CoordError("supply exactly one of --record-json or --record-file", code="invalid_args")
        record = (_parse_json(args.record_json, "--record-json") if args.record_json is not None
                  else _parse_json_file(args.record_file, "--record-file"))
        if command == "thread-register":
            return office.thread_register(record)
        if command == "project-view-import":
            return office.project_view_import(record)
        if command == "goal-add":
            return office.goal_add(record)
        return office.goal_update(args.goal_id, record)
    if command == "thread-resolve":
        return office.thread_resolve(args.office_agent, project_slug=args.project_slug,
                                     tool=args.tool, surface=args.surface, title=args.title)
    if command == "thread-list":
        return office.thread_list(project_slug=args.project_slug, tool=args.tool,
                                  surface=args.surface, status=args.status,
                                  limit=args.limit, offset=args.offset)
    if command == "project-view-get":
        return office.project_view_get(args.project_slug)
    if command == "project-view-list":
        return office.project_view_list()
    if command == "goal-get":
        return office.goal_get(args.goal_id)
    if command == "goal-list":
        return office.goal_list(args.project_slug)
    if command == "send":
        return office.send(
            args.to,
            _resolve_body(args),
            kind=args.kind,
            reply_to=args.reply_to,
            channel=args.channel,
            refs=args.refs,
            dedupe_key=args.dedupe_key,
            new_recipient=bool(args.new_recipient),
        )
    if command == "ack":
        return office.ack(args.id, note=args.note)
    if command == "delivery":
        return office.delivery(args.id, channel=args.channel)
    if command == "dilemma-open":
        options: dict[str, str] = {}
        for raw in args.options:
            key, sep, text = raw.partition("=")
            if not sep:
                raise SystemExit("--option must be KEY=TEXT")
            options[key.strip()] = text.strip()
        question = read_body_from_stdin() if args.body_stdin else args.question
        return office.dilemma_open(
            args.channel, question, options, correlation_id=args.correlation_id, to=args.to, refs=args.refs
        )
    if command == "dilemma-answer":
        return office.dilemma_answer(args.id, args.choice, args.rationale)
    if command == "dilemma-status":
        return office.dilemma_status(args.id)
    if command == "ask":
        return office.ask(args.to, _resolve_body(args), channel=args.channel, timeout_s=args.timeout)
    if command == "read":
        return office.read(
            limit=args.limit, peek=bool(args.peek), channel=args.channel, kind=args.kind
        )
    if command == "read-correlated-replies":
        return office.read_correlated_replies(
            args.project_slug, args.parent_receipt, limit=args.limit
        )
    if command == "wake-enroll":
        return Wake(home=office.home).enroll(
            binding_id=args.binding_id,
            office_agent=args.office_agent,
            driver=args.driver,
            native_thread_id=args.native_thread_id,
            workspace=args.workspace,
            enrolled_by=args.enrolled_by,
            allowed_wakers=args.allowed_waker,
            allowed_triggers=args.allowed_trigger or list(DEFAULT_ALLOWED_TRIGGERS),
            wake_kinds=args.wake_kind or list(DEFAULT_WAKE_KINDS),
            budget_per_day=args.budget_per_day,
            max_attempts_per_trigger=args.max_attempts_per_trigger,
            expires_at=args.expires_at,
            owner_confirm=bool(args.owner_confirm),
            correlation_required=bool(args.correlation_required),
            execution_mode=args.execution_mode,
            execution_host=args.execution_host,
            executor_agent=args.executor_agent,
            workspace_flavor=args.workspace_flavor,
        )
    if command == "wake-expect":
        return Wake(home=office.home).expect(
            binding_id=args.binding_id,
            caller=office.agent_id,
            task_id=args.task_id,
            correlation_id=args.correlation_id,
            counterpart=args.counterpart,
            request_message_id=args.request_message_id,
            max_wakes=args.max_wakes,
            max_hops=args.max_hops,
            ttl_s=args.ttl_s,
        )
    if command == "wake-consume":
        return Wake(home=office.home).correlations.consume(
            args.correlation_id, caller=office.agent_id, trigger_message_id=args.trigger_message_id
        )
    if command == "wake-close":
        return Wake(home=office.home).correlations.close(args.correlation_id, caller=office.agent_id)
    if command == "wake-task-cancel":
        return Wake(home=office.home).correlations.cancel_task(args.task_id, caller=office.agent_id)
    if command == "wake-correlations":
        return Wake(home=office.home).correlations.list()
    if command == "wake-pull-arm":
        return Wake(home=office.home).pull_arm(
            binding_id=args.binding_id, caller=office.agent_id, max_pulls=args.max_pulls,
            lost=bool(args.lost),
        )
    if command == "wake-pull":
        return Wake(home=office.home).pull(
            binding_id=args.binding_id, caller=office.agent_id, token=args.token
        )
    if command == "wake-pull-confirm":
        return Wake(home=office.home).pull_confirm(
            binding_id=args.binding_id, caller=office.agent_id, token=args.token, job_id=args.job_id
        )
    if command == "wake-pull-disarm":
        return Wake(home=office.home).pull_disarm(binding_id=args.binding_id, caller=office.agent_id)
    if command == "wake-verify":
        return Wake(home=office.home).verify(
            args.binding_id, owner_confirm=bool(args.owner_confirm)
        )
    if command == "wake-pause":
        return Wake(home=office.home).pause(args.binding_id)
    if command == "wake-revoke":
        return Wake(home=office.home).revoke(args.binding_id)
    if command == "wake-list":
        return Wake(home=office.home).list_bindings(limit=args.limit, offset=args.offset)
    if command == "wake-intake":
        if not args.owner_confirm:
            raise CoordError(
                "this operation requires explicit --owner-confirm",
                code="owner_confirmation_required",
            )
        return Wake(home=office.home).manual_intake(
            binding_id=args.binding_id,
            expected_version=args.expected_version,
            idempotency_key=args.idempotency_key,
            owner_confirm=bool(args.owner_confirm),
            prompt=read_body_from_stdin(),
        )
    if command == "wake-stop":
        return Wake(home=office.home).stop()
    if command == "wake-start":
        return Wake(home=office.home).start()
    if command == "wake-stop-status":
        return Wake(home=office.home).stop_status()
    if command == "wake-tick":
        return Wake(home=office.home).tick(dry_run=bool(args.dry_run), binding_id=args.binding_id)
    if command == "wake-ledger":
        return Wake(home=office.home).ledger(limit=args.limit, binding_id=args.binding_id)
    if command == "wake-relay-pending":
        if args.include_owned and args.relay_agent != office.agent_id:
            raise CoordError('pending requires the authenticated relay identity', code='identity_mismatch')
        return Wake(home=office.home).relay_pending(binding_id=args.binding_id,
                                                     driver=args.driver, include_owned=args.include_owned,
                                                     relay_agent=args.relay_agent)
    if command == "wake-relay-expect":
        return Wake(home=office.home).relay_watch_expect(
            binding_id=args.binding_id, enabled=not args.off, owner_confirm=bool(args.owner_confirm),
            recover=bool(args.recover),
        )
    if command == "wake-monitor":
        result = Wake(home=office.home).monitor()
        if args.post:
            posted = []
            for finding in result["findings"]:
                sent = office.send(None, f"[Wake monitor] {finding['kind']}: {finding['detail']}",
                                   kind="alert", channel=args.channel,
                                   dedupe_key=finding["dedupe_key"])
                posted.append({"key": finding["key"], "status": sent.get("status"),
                               "duplicate": bool(sent.get("duplicate"))})
            result["posted"] = posted
        return result
    if command == "wake-relay-watch":
        return Wake(home=office.home).relay_watch(
            binding_id=args.binding_id, interval_s=args.interval_s, timeout_s=args.timeout_s,
            delay_s=args.delay_s, driver=args.driver,
        )
    if command == "wake-relay-claim":
        return Wake(home=office.home).relay_claim(
            args.dispatch_id, relay_agent=args.relay_agent, driver=args.driver,
        )
    if command == "wake-relay-ack":
        from .codex_ack import owned_antigravity_ack, owned_codex_ack, owned_zcode_reply
        lookup = {"codex-desktop-relay": owned_codex_ack,
                  "zcode-desktop-relay": owned_zcode_reply,
                  "antigravity-desktop-relay": owned_antigravity_ack}[args.driver]
        return lookup(office.home, args.dispatch_id, office.agent_id)
    if command == "wake-relay-defer-busy":
        return Wake(home=office.home).relay_defer_busy(args.dispatch_id, driver=args.driver)
    if command == "wake-relay-receipt":
        return Wake(home=office.home).relay_receipt(
            args.dispatch_id,
            status=args.status,
            message_id=args.message_id,
            turn_id=args.turn_id,
            detail=args.detail,
            driver=args.driver,
        )
    if command == "wake-relay-observe":
        return Wake(home=office.home).relay_observe(
            args.dispatch_id, stage=args.stage, driver=args.driver,
            native_proof=args.native_proof_json, relay_agent=office.agent_id,
        )
    if command == "wake-relay-native-begin":
        baseline = {}
        if args.prior_turn_id is not None:
            baseline['prior_turn_id'] = None if args.prior_turn_id == 'null' else args.prior_turn_id
        if args.baseline_observed_at is not None:
            baseline['baseline_observed_at'] = args.baseline_observed_at
        return Wake(home=office.home).relay_native_begin(
            args.dispatch_id, relay_agent=args.relay_agent, driver=args.driver,
            **baseline,
        )
    if command == "wake-relay-native-unsubmitted":
        return Wake(home=office.home).relay_native_unsubmitted(
            args.dispatch_id, relay_agent=args.relay_agent, driver=args.driver,
        )
    if command == "wake-relay-reconcile":
        return Wake(home=office.home).relay_reconcile(
            args.dispatch_id,
            outcome=args.outcome,
            owner_confirm=bool(args.owner_confirm),
        )
    if command == "wake-schedule-add":
        return Wake(home=office.home).add_schedule(
            schedule_id=args.schedule_id,
            binding_id=args.binding_id,
            time=args.time,
            days=args.days,
            task_text=args.task_text,
            created_by=office.agent_id,
            grace_minutes=args.grace_minutes,
            owner_confirm=bool(args.owner_confirm),
        )
    if command == "wake-schedule-list":
        return Wake(home=office.home).list_schedules()
    if command == "wake-schedule-disable":
        return Wake(home=office.home).disable_schedule(args.schedule_id)
    if command == "wake-schedule-enable":
        return Wake(home=office.home).enable_schedule(args.schedule_id)
    if command == "knowledge-sync":
        return sync_knowledge(
            args.project_slug,
            channels=args.channels,
            dry_run=bool(args.dry_run),
            home=office.home,
        )
    if command == "wait":
        return office.wait(timeout_s=args.timeout, channel=args.channel)
    if command == "channels":
        return office.channels()
    if command == "presence":
        if args.release:
            return office.leave()
        return office.presence(ttl_s=args.ttl, status=args.status, workspace=args.workspace)
    if command == "who":
        return office.who(workspace=args.workspace)
    if command == "mcp":
        serve_stdio(
            office,
            heartbeat_ttl_s=None if args.no_heartbeat else args.ttl,
            profile=args.profile,
            channels=args.mcp_channels,
            peers=args.mcp_peers,
            contractor_limits=contractor_limits_from_args(args),
        )
        return None
    if command == "project-register":
        return office.project_register(args.project_id, args.checkout, aliases=args.alias)
    if command == "project-resolve":
        return office.project_resolve(args.project)
    if command == "project-stream":
        return office.project_stream(args.project, args.task, args.thread, args.run)
    if command == "project-activity":
        activity = _parse_json(args.activity_json, "--activity-json")
        if args.activity_file is not None:
            activity = _parse_json_file(args.activity_file, "--activity-file")
        return office.project_activity(
            args.project_id,
            args.task,
            args.thread,
            args.run,
            activity,
            **_sender(args),
        )
    if command == "project-activity-read":
        return office.project_activity_read(
            args.project_id,
            args.task,
            args.thread,
            args.run,
            limit=args.limit,
            **_sender(args),
        )
    if command == "project-audits":
        return office.project_audits(args.project)
    if command == "project-send":
        return office.project_send(
            args.project_id, args.task, args.thread, args.run,
            _parse_json(args.recipient_json, "--recipient-json"), args.body,
            kind=args.kind, ack_ttl_s=args.ack_ttl, **_sender(args))
    if command == "project-send-channel":
        return office.project_send_channel(
            args.project_id, args.task, args.thread, args.run, args.channel, args.body,
            kind=args.kind, ack_ttl_s=args.ack_ttl, **_sender(args))
    if command == "project-read":
        return office.project_read(
            args.project_id, args.task, args.thread, args.run,
            limit=args.limit, peek=bool(args.peek), **_sender(args))
    if command == "project-read-channel":
        return office.project_read_channel(
            args.project_id, args.task, args.thread, args.run, args.channel,
            limit=args.limit, peek=bool(args.peek), **_sender(args))
    if command == "project-wait":
        return office.project_wait(
            args.project_id, args.task, args.thread, args.run,
            timeout_s=args.timeout, **_sender(args))
    if command == "project-wait-channel":
        return office.project_wait_channel(
            args.project_id, args.task, args.thread, args.run, args.channel,
            timeout_s=args.timeout, **_sender(args))
    if command == "project-acknowledge":
        return office.project_acknowledge(
            args.project_id, args.delivery, **_sender(args))
    if command == "project-delivery":
        return office.project_delivery_state(
            args.project_id, args.delivery, **_sender(args))
    if command == "project-cancel":
        return office.project_cancel_delivery(
            args.project_id, args.delivery, **_sender(args))
    if command == "journal-export":
        return office.journal_export(_parse_json(args.record_json, "--record-json"))
    if command == "journal-checkpoint":
        return office.journal_commit_checkpoint(_parse_json(args.record_json, "--record-json"))
    if command == "journal-read-checkpoint":
        return office.journal_read_checkpoint(args.project_id, args.task_id, args.checkpoint_id)
    if command == "journal-audit":
        return office.journal_audit_events()
    if command == "journal-reconcile":
        return office.journal_reconcile()
    if command == "work-register":
        return office.work_register_task(
            args.project, args.task, args.checkout, args.scope,
            dependencies=args.dependency, base_sha=args.base_sha,
            commit_intent=args.commit_intent, journal_refs=args.journal_ref,
            status=args.status,
            unknown_reasons=_parse_json(args.unknown_reasons_json, "--unknown-reasons-json"),
            deferred_reason=args.deferred_reason)
    if command == "work-get":
        return office.work_get_task(args.project, args.task)
    if command == "work-list":
        return office.work_list_tasks(project=args.project, checkout=args.checkout)
    if command == "work-update":
        optional: dict[str, Any] = {}
        if args.checkout is not None:
            optional["checkout"] = args.checkout
        if args.scope is not None:
            optional["scopes"] = args.scope
        if args.dependency is not None:
            optional["dependencies"] = args.dependency
        if args.base_sha is not None:
            optional["base_sha"] = args.base_sha
        if args.commit_intent is not None:
            optional["commit_intent"] = args.commit_intent
        if args.journal_ref is not None:
            optional["journal_refs"] = args.journal_ref
        if args.status is not None:
            optional["status"] = args.status
        reasons = _parse_json(args.unknown_reasons_json, "--unknown-reasons-json")
        if reasons is not None:
            optional["unknown_reasons"] = reasons
        if args.deferred_reason is not None:
            optional["deferred_reason"] = args.deferred_reason
        return office.work_update_task(args.project, args.task, **optional)
    if command == "work-blocker-add":
        return office.work_add_blocker(
            args.project, args.task, args.blocker, args.reason,
            dependencies=args.dependency)
    if command == "work-blocker-transition":
        return office.work_transition_blocker(
            args.project, args.task, args.blocker, args.status, args.reason)
    if command == "work-plan-record":
        return office.work_record_plan(
            args.project, args.task, args.from_agent, args.to_agent,
            args.scope, args.summary, journal_refs=args.journal_ref)
    if command == "work-plan-list":
        return office.work_list_plans(args.project, args.task)
    if command == "work-plan-acknowledge":
        return office.work_acknowledge_plan(
            args.project, args.task, args.plan, args.ack_agent, args.status,
            note=args.note)
    if command == "conflict-advise":
        return office.conflict_advise(
            _parse_json(args.lease_json, "--lease-json"),
            requester_lineage=_parse_json(args.lineage_json, "--lineage-json"))
    if command == "work-projection":
        return office.work_projection(
            _parse_json(args.records_json, "--records-json"), args.project_id)
    raise CoordError(f"unknown command: {command}", code="invalid_command")


def default_remote_config(runtime_root: Path) -> Path | None:
    """Select only the installer-pinned remote configuration; never a loose file."""
    config = runtime_root / "office-remote.json"
    marker = runtime_root / ".agent-office-runtime.json"
    try:
        metadata = json.loads(marker.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if not config.exists():
            return None
        raise CoordError("Unpinned remote configuration; local fallback refused", code="invalid_remote_config")
    except (OSError, ValueError):
        raise CoordError("Invalid installation marker; local fallback refused", code="invalid_remote_config")
    if not isinstance(metadata, dict):
        raise CoordError("Invalid installation marker", code="invalid_remote_config")
    pin = metadata.get("remote_config_sha256")
    if pin is None:
        if not config.exists():
            # Local-only installed/test profiles have no remote route to pin.
            # Any loose remote file still refuses fallback.
            return None
        raise CoordError("Installed Office runtime has no remote configuration pin; local fallback refused",
                         code="invalid_remote_config")
    try:
        valid = isinstance(pin, str) and len(pin) == 64 and hashlib.sha256(config.read_bytes()).hexdigest() == pin
    except OSError:
        valid = False
    if not valid:
        raise CoordError("Remote configuration pin mismatch; local fallback refused", code="invalid_remote_config")
    return config


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.remote_config is None and args.home is None and args.runtime_home is None:
            args.remote_config = default_remote_config(Path(__file__).resolve().parents[1])
        payload = dispatch(args)
    except CoordError as exc:
        payload = {"status": "error", "code": exc.code, "message": str(exc), **exc.payload}
    if payload is None:
        return 0
    stream = getattr(sys.stdout, "buffer", None)
    if stream is not None:
        stream.write(_json(payload).encode("utf-8"))
        stream.flush()
    elif sys.stdout is not None:
        sys.stdout.write(_json(payload))
    # else: no console (pythonw under Task Scheduler) - the result is not printed, the exit
    # code still reports it.
    return _exit_code(payload, remote=args.remote_config is not None)
