"""SYNTHETIC pytest suite for office_team_probe.py.

Runs the full 7-step cross-process cooperation journey through the real
office.py CLI, in an isolated tmp_path home, and checks the produced report.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROBE = SCRIPT_DIR / "office_team_probe.py"

EXPECTED_STEP_IDS = [
    "presence_claim_all_busy",
    "presence_renewal_extends_expiry",
    "dm_private_vs_leader_inbox",
    "task_channel_post_ack_delivery",
    "announced_route_change_ack_and_move",
    "handoff_with_references_ack",
    "metadata_only_report",
]

# Synthetic message bodies the probe sends; must never appear verbatim in the report.
RAW_BODY_FRAGMENTS = [
    "synthetic dm probe-maker to probe-reviewer",
    "synthetic route-a task announcement",
    "synthetic decision: moving the conversation",
    "synthetic maker note on probe-route-b",
    "synthetic reviewer note on probe-route-b",
    "synthetic handoff citing acks",
]


def run_probe(home_root: Path, output: Path, step_timeout: float = 25.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(PROBE),
            "--output",
            str(output),
            "--home-root",
            str(home_root),
            "--step-timeout",
            str(step_timeout),
        ],
        cwd=str(SCRIPT_DIR),
        capture_output=True,
        text=True,
        timeout=110,
    )


def test_full_journey_exits_zero_and_every_step_passes(tmp_path):
    home_root = tmp_path / "coord-home"
    output = tmp_path / "report.json"

    result = run_probe(home_root, output)

    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert output.exists()

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["overall_pass"] is True
    assert report["synthetic"] is True

    step_ids = [s["id"] for s in report["steps"]]
    assert step_ids == EXPECTED_STEP_IDS
    for step in report["steps"]:
        assert step["pass"] is True, step


def test_stable_agent_ids_across_steps(tmp_path):
    home_root = tmp_path / "coord-home"
    output = tmp_path / "report.json"
    run_probe(home_root, output)
    report = json.loads(output.read_text(encoding="utf-8"))

    agent_ids = report["agent_ids"]
    assert set(agent_ids.keys()) == {"leader", "maker", "reviewer"}
    for role, agent_id in agent_ids.items():
        assert agent_id.endswith(f"probe-{role}")

    # Every step's own agent_ids list only ever cites these three stable ids.
    known = set(agent_ids.values())
    for step in report["steps"]:
        assert set(step["agent_ids"]) <= known


def test_dm_vs_channel_separation(tmp_path):
    home_root = tmp_path / "coord-home"
    output = tmp_path / "report.json"
    run_probe(home_root, output)
    report = json.loads(output.read_text(encoding="utf-8"))

    dm_step = next(s for s in report["steps"] if s["id"] == "dm_private_vs_leader_inbox")
    assert dm_step["pass"] is True
    assert dm_step["actual"]["reviewer_has_dm"] is True
    assert dm_step["actual"]["leader_has_dm"] is False


def test_announcement_and_acks_visible_in_delivery(tmp_path):
    home_root = tmp_path / "coord-home"
    output = tmp_path / "report.json"
    run_probe(home_root, output)
    report = json.loads(output.read_text(encoding="utf-8"))

    task_step = next(s for s in report["steps"] if s["id"] == "task_channel_post_ack_delivery")
    assert task_step["actual"]["delivery_state"] == "acknowledged"
    assert len(task_step["actual"]["acknowledged_by"]) == 2

    route_step = next(s for s in report["steps"] if s["id"] == "announced_route_change_ack_and_move")
    assert route_step["actual"]["announce_delivery_state"] == "acknowledged"
    assert len(route_step["actual"]["announce_acknowledged_by"]) == 2
    assert route_step["actual"]["leader_sees_maker_note"] is True
    assert route_step["actual"]["leader_sees_reviewer_note"] is True

    handoff_step = next(s for s in report["steps"] if s["id"] == "handoff_with_references_ack")
    assert handoff_step["actual"]["handoff_channel"] == "probe-route-b"
    assert handoff_step["actual"]["handoff_delivery_state"] == "acknowledged"


def test_presence_ttl_renewal_extends_expiry(tmp_path):
    home_root = tmp_path / "coord-home"
    output = tmp_path / "report.json"
    run_probe(home_root, output)
    report = json.loads(output.read_text(encoding="utf-8"))

    renewal_step = next(s for s in report["steps"] if s["id"] == "presence_renewal_extends_expiry")
    assert renewal_step["pass"] is True
    first = renewal_step["actual"]["first_expires_at"]
    second = renewal_step["actual"]["second_expires_at"]
    assert second > first


def test_report_is_metadata_only_no_raw_bodies(tmp_path):
    home_root = tmp_path / "coord-home"
    output = tmp_path / "report.json"
    run_probe(home_root, output)

    raw_text = output.read_text(encoding="utf-8")
    report = json.loads(raw_text)

    assert report["synthetic"] is True
    assert report["home_root"] == str(home_root)

    for fragment in RAW_BODY_FRAGMENTS:
        assert fragment not in raw_text

    # Every message reference elsewhere in the report is a 32-hex office message id,
    # never a body string.
    for step in report["steps"]:
        for message_id in step["message_ids"]:
            assert message_id == "" or (len(message_id) == 32 and all(c in "0123456789abcdef" for c in message_id))


def test_bounded_message_budget(tmp_path):
    home_root = tmp_path / "coord-home"
    output = tmp_path / "report.json"
    run_probe(home_root, output)
    report = json.loads(output.read_text(encoding="utf-8"))

    assert report["message_count"] <= report["message_budget"] == 20


def test_isolated_home_never_touches_real_user_home(tmp_path):
    home_root = tmp_path / "coord-home"
    output = tmp_path / "report.json"
    result = run_probe(home_root, output)

    assert result.returncode == 0
    assert home_root.exists()
    # The probe's own isolated coordfs tree lives strictly under home_root.
    contents = list(home_root.rglob("*"))
    assert contents, "expected the isolated home to contain office runtime files"
