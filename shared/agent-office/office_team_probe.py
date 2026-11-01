"""SYNTHETIC bounded cross-process Office cooperation probe.

Runs a scripted, metadata-only journey through the real `office.py` CLI, one
real child process per call, to demonstrate that three separate sessions
(leader/maker/reviewer) can cooperate through presence, direct messages,
channels, acknowledgements and a referenced handoff. Everything here is
synthetic test data; nothing talks to a real user, model, or network.

Usage:
    python office_team_probe.py --output PATH [--home-root DIR] [--step-timeout SECONDS]

Exit code 0 iff every step in the journey passed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
OFFICE_PY = SCRIPT_DIR / "office.py"

HARNESS = "probe-team"
ROLES = ("leader", "maker", "reviewer")
SESSION_IDS = {role: f"probe-{role}" for role in ROLES}

MAX_MESSAGES = 20
JOURNEY_BUDGET_S = 220.0  # bounded well under ~4 minutes


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def body_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class Budget:
    def __init__(self, total_s: float) -> None:
        self.deadline = time.monotonic() + total_s

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())


class MessageCounter:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.count = 0

    def add(self, n: int = 1) -> None:
        self.count += n
        if self.count > self.limit:
            raise RuntimeError(f"message budget exceeded: {self.count} > {self.limit}")


class CliError(RuntimeError):
    pass


def run_cli(role: str, args: list[str], env: dict[str, str], timeout: float) -> dict[str, Any]:
    """One real child process invocation of office.py for `role`. Returns parsed JSON."""
    argv = [
        sys.executable,
        str(OFFICE_PY),
        "--harness",
        HARNESS,
        "--session-id",
        SESSION_IDS[role],
        *args,
    ]
    started = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            cwd=str(SCRIPT_DIR),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        # subprocess.run already killed and reaped this probe's own child on timeout.
        raise CliError(f"role={role} args={args} timed out after {timeout}s") from exc
    duration = time.monotonic() - started
    stdout = proc.stdout.strip()
    try:
        parsed = json.loads(stdout) if stdout else {}
    except json.JSONDecodeError as exc:
        raise CliError(
            f"role={role} args={args} produced non-JSON stdout: {stdout[:200]!r}"
        ) from exc
    parsed["_returncode"] = proc.returncode
    parsed["_duration_s"] = round(duration, 3)
    return parsed


def sanitize_message(msg: dict[str, Any]) -> dict[str, Any]:
    """Strip any raw body text from an untrusted_message frame; keep only its hash."""
    body = msg.get("body", "")
    return {
        "id": msg.get("id"),
        "from": msg.get("from"),
        "to": msg.get("to"),
        "kind": msg.get("kind"),
        "reply_to": msg.get("reply_to"),
        "ts": msg.get("ts"),
        "body_sha256_16": body_hash(body) if isinstance(body, str) else None,
    }


def find_message(read_response: dict[str, Any], message_id: str) -> dict[str, Any] | None:
    for entry in read_response.get("messages", []):
        um = entry.get("untrusted_message", {})
        if um.get("id") == message_id:
            return um
    return None


def step_record(
    step_id: str,
    expected: Any,
    actual: Any,
    passed: bool,
    message_ids: list[str],
    agent_ids: list[str],
    timestamps: list[str],
) -> dict[str, Any]:
    return {
        "id": step_id,
        "expected": expected,
        "actual": actual,
        "pass": bool(passed),
        "message_ids": message_ids,
        "agent_ids": agent_ids,
        "timestamps": timestamps,
    }


def run_journey(env: dict[str, str], step_timeout: float, home_root: str) -> dict[str, Any]:
    counter = MessageCounter(MAX_MESSAGES)
    steps: list[dict[str, Any]] = []
    agent_ids: dict[str, str] = {}
    raw_bodies_used: list[str] = []

    def cli(role: str, *args: str) -> dict[str, Any]:
        return run_cli(role, list(args), env, step_timeout)

    # --- Step 1: presence claim, all three busy -----------------------------------
    presence_calls = {}
    for role in ROLES:
        resp = cli(role, "presence", "--status", "busy", "--ttl", "10")
        presence_calls[role] = resp
        agent_ids[role] = resp.get("agent", "")
    who_resp = cli("leader", "who")
    online_by_agent = {a["agent"]: a for a in who_resp.get("online", [])}

    def who_status(entry: dict[str, Any]) -> str | None:
        # The caller's own entry carries `status`; every other (untrusted) entry
        # carries the same value under `untrusted_status` instead.
        return entry.get("status") if entry.get("self") else entry.get("untrusted_status")

    all_present_busy = all(
        presence_calls[role].get("status") == "ok"
        and presence_calls[role].get("meta", {}).get("status") == "busy"
        for role in ROLES
    ) and all(
        who_status(online_by_agent.get(agent_ids[role], {})) == "busy" for role in ROLES
    )
    steps.append(
        step_record(
            "presence_claim_all_busy",
            expected="all three roles claim presence status=busy and `who` reports each stable agent id as busy",
            actual={
                "presence_status": {r: presence_calls[r].get("status") for r in ROLES},
                "presence_meta_status": {r: presence_calls[r].get("meta", {}).get("status") for r in ROLES},
                "who_status": {agent_ids[r]: who_status(online_by_agent.get(agent_ids[r], {})) for r in ROLES},
            },
            passed=all_present_busy,
            message_ids=[],
            agent_ids=[agent_ids[r] for r in ROLES],
            timestamps=[presence_calls[r].get("meta", {}).get("status_at", "") for r in ROLES],
        )
    )
    if not all_present_busy:
        return finalize(steps, agent_ids, home_root, counter)

    # --- Step 2: presence renewal extends ttl/expiry --------------------------------
    renew_role = "maker"
    first = cli(renew_role, "presence", "--status", "busy", "--ttl", "3")
    first_expiry = first.get("expires_at", "")
    time.sleep(1.2)
    second = cli(renew_role, "presence", "--status", "busy", "--ttl", "3")
    second_expiry = second.get("expires_at", "")

    def parse_ts(value: str) -> datetime | None:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None

    first_dt = parse_ts(first_expiry)
    second_dt = parse_ts(second_expiry)
    renewal_extended = bool(first_dt and second_dt and second_dt > first_dt)
    steps.append(
        step_record(
            "presence_renewal_extends_expiry",
            expected="re-issuing busy before ttl expiry returns a strictly later expires_at",
            actual={"first_expires_at": first_expiry, "second_expires_at": second_expiry},
            passed=renewal_extended,
            message_ids=[],
            agent_ids=[agent_ids[renew_role]],
            timestamps=[first_expiry, second_expiry],
        )
    )
    if not renewal_extended:
        return finalize(steps, agent_ids, home_root, counter)

    # --- Step 3: private DM vs channel isolation -------------------------------------
    dm_body = "synthetic dm probe-maker to probe-reviewer"
    raw_bodies_used.append(dm_body)
    dm_send = cli("maker", "send", "--to", agent_ids["reviewer"], "--body", dm_body)
    counter.add(1)
    dm_id = dm_send.get("id", "")
    reviewer_inbox = cli("reviewer", "read")
    leader_inbox = cli("leader", "read")
    reviewer_has_dm = find_message(reviewer_inbox, dm_id) is not None
    leader_lacks_dm = find_message(leader_inbox, dm_id) is None
    dm_ok = dm_send.get("status") == "ok" and reviewer_has_dm and leader_lacks_dm
    steps.append(
        step_record(
            "dm_private_vs_leader_inbox",
            expected="reviewer's own inbox contains the DM id; leader's own inbox does not",
            actual={
                "dm_send_status": dm_send.get("status"),
                "reviewer_has_dm": reviewer_has_dm,
                "leader_has_dm": not leader_lacks_dm,
                "reviewer_inbox_count": reviewer_inbox.get("count"),
                "leader_inbox_count": leader_inbox.get("count"),
            },
            passed=dm_ok,
            message_ids=[dm_id],
            agent_ids=[agent_ids["maker"], agent_ids["reviewer"], agent_ids["leader"]],
            timestamps=[dm_send.get("ts", "")],
        )
    )
    if not dm_ok:
        return finalize(steps, agent_ids, home_root, counter)

    # --- Step 4: task channel post + both ack + delivery ----------------------------
    route_a = "probe-route-a"
    task_body = "synthetic route-a task announcement"
    raw_bodies_used.append(task_body)
    task_post = cli("leader", "send", "--channel", route_a, "--kind", "note", "--body", task_body)
    counter.add(1)
    task_id = task_post.get("id", "")

    maker_read_a = cli("maker", "read", "--channel", route_a)
    reviewer_read_a = cli("reviewer", "read", "--channel", route_a)
    maker_sees_task = find_message(maker_read_a, task_id) is not None
    reviewer_sees_task = find_message(reviewer_read_a, task_id) is not None

    maker_ack_task = cli("maker", "ack", "--id", task_id, "--note", "seen")
    counter.add(1)
    reviewer_ack_task = cli("reviewer", "ack", "--id", task_id, "--note", "seen")
    counter.add(1)

    task_delivery = cli("leader", "delivery", "--id", task_id, "--channel", route_a)
    acked_by = set(task_delivery.get("acknowledged_by", []))
    task_ok = (
        task_post.get("status") == "ok"
        and maker_sees_task
        and reviewer_sees_task
        and maker_ack_task.get("status") == "ok"
        and reviewer_ack_task.get("status") == "ok"
        and task_delivery.get("state") == "acknowledged"
        and agent_ids["maker"] in acked_by
        and agent_ids["reviewer"] in acked_by
    )
    steps.append(
        step_record(
            "task_channel_post_ack_delivery",
            expected="leader posts on probe-route-a; maker and reviewer read and ack it; delivery shows acknowledged by both",
            actual={
                "maker_sees_task": maker_sees_task,
                "reviewer_sees_task": reviewer_sees_task,
                "delivery_state": task_delivery.get("state"),
                "acknowledged_by": sorted(acked_by),
            },
            passed=task_ok,
            message_ids=[task_id, maker_ack_task.get("id", ""), reviewer_ack_task.get("id", "")],
            agent_ids=[agent_ids["leader"], agent_ids["maker"], agent_ids["reviewer"]],
            timestamps=[task_post.get("ts", ""), maker_ack_task.get("ts", ""), reviewer_ack_task.get("ts", "")],
        )
    )
    if not task_ok:
        return finalize(steps, agent_ids, home_root, counter)

    # --- Step 5: announced route change, both ack, both post on route-b -------------
    route_b = "probe-route-b"
    announce_body = "synthetic decision: moving the conversation from probe-route-a to probe-route-b"
    raw_bodies_used.append(announce_body)
    announce_post = cli("leader", "send", "--channel", route_a, "--kind", "decision", "--body", announce_body)
    counter.add(1)
    announce_id = announce_post.get("id", "")

    maker_read_a2 = cli("maker", "read", "--channel", route_a)
    reviewer_read_a2 = cli("reviewer", "read", "--channel", route_a)
    maker_sees_announce = find_message(maker_read_a2, announce_id) is not None
    reviewer_sees_announce = find_message(reviewer_read_a2, announce_id) is not None

    maker_ack_announce = cli("maker", "ack", "--id", announce_id, "--note", "moving")
    counter.add(1)
    reviewer_ack_announce = cli("reviewer", "ack", "--id", announce_id, "--note", "moving")
    counter.add(1)

    announce_delivery = cli("leader", "delivery", "--id", announce_id, "--channel", route_a)
    announce_acked_by = set(announce_delivery.get("acknowledged_by", []))

    maker_note_body = "synthetic maker note on probe-route-b"
    reviewer_note_body = "synthetic reviewer note on probe-route-b"
    raw_bodies_used.extend([maker_note_body, reviewer_note_body])
    maker_note = cli("maker", "send", "--channel", route_b, "--body", maker_note_body)
    counter.add(1)
    reviewer_note = cli("reviewer", "send", "--channel", route_b, "--body", reviewer_note_body)
    counter.add(1)
    maker_note_id = maker_note.get("id", "")
    reviewer_note_id = reviewer_note.get("id", "")

    leader_read_b = cli("leader", "read", "--channel", route_b, "--limit", "10")
    leader_sees_maker_note = find_message(leader_read_b, maker_note_id) is not None
    leader_sees_reviewer_note = find_message(leader_read_b, reviewer_note_id) is not None

    leader_ack_maker_note = cli("leader", "ack", "--id", maker_note_id, "--note", "seen")
    counter.add(1)
    leader_ack_reviewer_note = cli("leader", "ack", "--id", reviewer_note_id, "--note", "seen")
    counter.add(1)

    route_change_ok = (
        announce_post.get("status") == "ok"
        and maker_sees_announce
        and reviewer_sees_announce
        and announce_delivery.get("state") == "acknowledged"
        and agent_ids["maker"] in announce_acked_by
        and agent_ids["reviewer"] in announce_acked_by
        and maker_note.get("status") == "ok"
        and reviewer_note.get("status") == "ok"
        and leader_sees_maker_note
        and leader_sees_reviewer_note
        and leader_ack_maker_note.get("status") == "ok"
        and leader_ack_reviewer_note.get("status") == "ok"
    )
    steps.append(
        step_record(
            "announced_route_change_ack_and_move",
            expected=(
                "leader announces the move on probe-route-a; maker and reviewer ack it; "
                "both then post on probe-route-b; leader reads and acks both notes"
            ),
            actual={
                "maker_sees_announce": maker_sees_announce,
                "reviewer_sees_announce": reviewer_sees_announce,
                "announce_delivery_state": announce_delivery.get("state"),
                "announce_acknowledged_by": sorted(announce_acked_by),
                "leader_sees_maker_note": leader_sees_maker_note,
                "leader_sees_reviewer_note": leader_sees_reviewer_note,
            },
            passed=route_change_ok,
            message_ids=[
                announce_id,
                maker_ack_announce.get("id", ""),
                reviewer_ack_announce.get("id", ""),
                maker_note_id,
                reviewer_note_id,
                leader_ack_maker_note.get("id", ""),
                leader_ack_reviewer_note.get("id", ""),
            ],
            agent_ids=[agent_ids["leader"], agent_ids["maker"], agent_ids["reviewer"]],
            timestamps=[announce_post.get("ts", ""), maker_note.get("ts", ""), reviewer_note.get("ts", "")],
        )
    )
    if not route_change_ok:
        return finalize(steps, agent_ids, home_root, counter)

    # --- Step 6: handoff on route-b referencing the route-change + both acks --------
    # NOTE on CLI mechanics (verified against coordfs/office.py _resolve_send_target):
    # `send --reply-to <id>` pins the target to the channel that stores <id> whenever
    # only one of --to/--channel is given (the CLI's --to/--channel pair is a mutually
    # exclusive argparse group, so both can never be supplied together). Since the
    # route-change announcement lives on probe-route-a, a literal --reply-to to it
    # would silently reroute this handoff back onto probe-route-a instead of keeping
    # it on probe-route-b. We keep the handoff on probe-route-b by replying to the
    # maker's own probe-route-b note instead, and reference the announcement plus
    # both of its ack ids through --ref (a non-redirecting pointer list), with the
    # ack ids also cited by id in the handoff body text.
    handoff_body = (
        f"synthetic handoff citing acks {maker_ack_announce.get('id','')} "
        f"and {reviewer_ack_announce.get('id','')} for route-change {announce_id}"
    )
    raw_bodies_used.append(handoff_body)
    handoff_send = cli(
        "maker",
        "send",
        "--channel",
        route_b,
        "--kind",
        "handoff",
        "--reply-to",
        maker_note_id,
        "--ref",
        announce_id,
        "--ref",
        maker_ack_announce.get("id", ""),
        "--ref",
        reviewer_ack_announce.get("id", ""),
        "--body",
        handoff_body,
    )
    counter.add(1)
    handoff_id = handoff_send.get("id", "")
    handoff_stayed_on_route_b = handoff_send.get("channel") == route_b

    reviewer_read_b = cli("reviewer", "read", "--channel", route_b, "--limit", "10")
    reviewer_sees_handoff = find_message(reviewer_read_b, handoff_id) is not None
    reviewer_ack_handoff = cli("reviewer", "ack", "--id", handoff_id, "--note", "received handoff")
    counter.add(1)
    handoff_delivery = cli("leader", "delivery", "--id", handoff_id, "--channel", route_b)
    handoff_acked_by = set(handoff_delivery.get("acknowledged_by", []))

    handoff_ok = (
        handoff_send.get("status") == "ok"
        and handoff_send.get("kind") == "handoff"
        and handoff_stayed_on_route_b
        and reviewer_sees_handoff
        and reviewer_ack_handoff.get("status") == "ok"
        and handoff_delivery.get("state") == "acknowledged"
        and agent_ids["reviewer"] in handoff_acked_by
    )
    steps.append(
        step_record(
            "handoff_with_references_ack",
            expected=(
                "maker's kind=handoff on probe-route-b carries refs to the route-change id and both "
                "acks, stays on probe-route-b, and reviewer's ack is reflected in delivery"
            ),
            actual={
                "handoff_status": handoff_send.get("status"),
                "handoff_kind": handoff_send.get("kind"),
                "handoff_channel": handoff_send.get("channel"),
                "reviewer_sees_handoff": reviewer_sees_handoff,
                "handoff_delivery_state": handoff_delivery.get("state"),
                "handoff_acknowledged_by": sorted(handoff_acked_by),
            },
            passed=handoff_ok,
            message_ids=[handoff_id, reviewer_ack_handoff.get("id", "")],
            agent_ids=[agent_ids["maker"], agent_ids["reviewer"], agent_ids["leader"]],
            timestamps=[handoff_send.get("ts", ""), reviewer_ack_handoff.get("ts", "")],
        )
    )

    return finalize(steps, agent_ids, home_root, counter, raw_bodies_used)


def finalize(
    steps: list[dict[str, Any]],
    agent_ids: dict[str, str],
    home_root: str,
    counter: MessageCounter,
    raw_bodies_used: list[str] | None = None,
) -> dict[str, Any]:
    raw_bodies_used = raw_bodies_used or []
    report = {
        "synthetic": True,
        "generated_at": now_iso(),
        "home_root": home_root,
        "agent_ids": agent_ids,
        "message_count": counter.count,
        "message_budget": MAX_MESSAGES,
        "steps": steps,
        "overall_pass": bool(steps) and all(s["pass"] for s in steps),
    }

    # Step 7 (metadata-only self-check): the assembled report must carry no raw
    # message body text, only hashes, and must be labelled synthetic.
    serialized = json.dumps(report)
    leaked_bodies = [b for b in raw_bodies_used if b and b in serialized]
    metadata_only_ok = report["synthetic"] is True and bool(home_root) and not leaked_bodies
    report["steps"].append(
        step_record(
            "metadata_only_report",
            expected="report is labelled synthetic, records the isolated home path, and contains no raw message body text",
            actual={
                "synthetic": report["synthetic"],
                "home_root_present": bool(home_root),
                "leaked_body_count": len(leaked_bodies),
            },
            passed=metadata_only_ok,
            message_ids=[],
            agent_ids=list(agent_ids.values()),
            timestamps=[report["generated_at"]],
        )
    )
    report["overall_pass"] = bool(report["steps"]) and all(s["pass"] for s in report["steps"])
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="Path to write the JSON report")
    parser.add_argument(
        "--home-root",
        default=None,
        help="Isolated Office home root (default: a fresh tempfile.mkdtemp)",
    )
    parser.add_argument(
        "--step-timeout",
        type=float,
        default=30.0,
        help="Per-CLI-call timeout in seconds (default 30)",
    )
    args = parser.parse_args()

    home_root = args.home_root or tempfile.mkdtemp(prefix="office-probe-")
    Path(home_root).mkdir(parents=True, exist_ok=True)

    import os

    env = dict(os.environ)
    env["COORDFS_HOME"] = home_root  # always override any inherited value

    budget = Budget(JOURNEY_BUDGET_S)
    try:
        report = run_journey(env, args.step_timeout, home_root)
    except (CliError, RuntimeError) as exc:
        report = {
            "synthetic": True,
            "generated_at": now_iso(),
            "home_root": home_root,
            "overall_pass": False,
            "error": str(exc),
            "steps": [],
        }
    report["journey_budget_remaining_s"] = round(budget.remaining(), 3)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    return 0 if report.get("overall_pass") else 1


if __name__ == "__main__":
    sys.exit(main())
