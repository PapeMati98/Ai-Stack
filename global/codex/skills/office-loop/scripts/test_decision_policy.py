"""Synthetic-journal tests for the office-loop shadow decision policy."""
from __future__ import annotations

import ast
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import decision_policy as dp
from dtd_core import beta_interval, beta_mean, rule_of_three

SKILL_ROOT = dp.SKILL_ROOT
BASE = datetime(2026, 10, 21, tzinfo=timezone.utc)


def utc(index: int) -> str:
    return (BASE + timedelta(seconds=index)).strftime("%Y-%m-%dT%H:%M:%SZ")


def runs_root() -> Path:
    repository = next((parent for parent in SKILL_ROOT.parents if (parent / ".git").exists()), SKILL_ROOT)
    root = Path(os.environ.get("AGENT_TEST_ROOT") or Path.home() / ".agents" / "test-runs" / "office-loop").resolve()
    if root.is_relative_to(repository) or root.is_relative_to(SKILL_ROOT):
        raise pytest.UsageError("AGENT_TEST_ROOT must be outside the repository and the skill tree")
    root.mkdir(parents=True, exist_ok=True)
    return root


def make_dir() -> Path:
    root = runs_root()
    if os.name == "nt":
        mkdir = os.mkdir
        with patch("tempfile._os.mkdir", side_effect=lambda path, mode: mkdir(path, 0o777)):
            return Path(tempfile.mkdtemp(prefix="office-loop-policy-", dir=root))
    return Path(tempfile.mkdtemp(prefix="office-loop-policy-", dir=root))


def source_snapshot() -> dict[str, bytes | None]:
    snapshot: dict[str, bytes | None] = {}
    for path in SKILL_ROOT.rglob("*"):
        relative = path.relative_to(SKILL_ROOT)
        if "__pycache__" in relative.parts or ".pytest_cache" in relative.parts:
            continue
        snapshot[str(relative)] = path.read_bytes() if path.is_file() else None
    return snapshot


@pytest.fixture(autouse=True)
def skill_tree_unchanged():
    before = source_snapshot()
    yield
    assert source_snapshot() == before


@pytest.fixture
def lab():
    root = make_dir()
    telemetry = root / "telemetry"
    telemetry.mkdir()
    catalog = root / "catalog.json"
    catalog.write_text(json.dumps({
        "models": [
            {"model_id": "gpt-6-sol", "tier": "W", "lineage": "openai",
             "profiles": [{"agent": "codex", "model": "gpt-6-sol"}]},
            {"model_id": "qwen3.8-max", "tier": "M", "lineage": "alibaba-qwen",
             "profiles": [{"agent": "qwen", "model": "qwen3.8-max"}]},
        ]
    }), encoding="utf-8")
    return {"root": root, "telemetry": telemetry, "catalog": catalog, "out": root / "out"}


def write_journal(telemetry: Path, folder: str, name: str, events: list[dict]) -> None:
    path = telemetry / folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n", encoding="utf-8")


def provider(index, node, round_, agent, model, role="maker", **extra):
    row = {
        "utc": utc(index), "event": "provider_result", "node": node, "round": round_,
        "role": role, "agent": agent, "model": model, "lineage": "test", "status": "COMPLETED",
        "fresh_input_tokens": 1, "output_tokens": 1,
    }
    row.update(extra)
    return row


def repair(index, node, round_, code):
    return {"utc": utc(index), "event": "repair", "node": node, "round": round_, "code": code}


def terminal(index, node, status, round_=None):
    row = {"utc": utc(index), "event": "node_terminal", "node": node, "status": status}
    if round_ is not None:
        row["round"] = round_
    return row


def run(capsys, *argv):
    code = dp.main(list(argv))
    captured = capsys.readouterr()
    assert captured.err == ""
    return code, json.loads(captured.out)


def analyze(capsys, lab, *extra):
    code, payload = run(
        capsys, "analyze",
        "--telemetry-root", str(lab["telemetry"]),
        "--catalog", str(lab["catalog"]),
        "--out", str(lab["out"]),
        *extra,
    )
    return code, payload


def policy_file(lab):
    return json.loads((lab["out"] / "policy.json").read_text(encoding="utf-8"))


def report_text(lab) -> str:
    return (lab["out"] / "report.md").read_text(encoding="utf-8")


def row_for(payload, code, bucket, tier):
    for row in payload["policy"]:
        state = row["state"]
        if (state["code"], state["round_bucket"], state["tier"]) == (code, bucket, tier):
            return row
    raise AssertionError((code, bucket, tier))


def continue_journal(index, node="n", code="TEST_FAILURE", maker=("codex", "gpt-6-sol"),
                     terminal_status="ACCEPTED", terminal_round=2, repair_round=1, tokens=None):
    agent, model = maker
    events = [
        provider(index, node, repair_round, agent, model, fresh_input_tokens=100),
        repair(index + 1, node, repair_round, code),
        provider(index + 2, node, repair_round + 1, agent, model,
                 fresh_input_tokens=4 if tokens is None else tokens),
        terminal(index + 3, node, terminal_status, terminal_round),
    ]
    return events


def test_catalog_allowlist_excludes_synthetic_agents(lab, capsys):
    null_agent = provider(4, "n", 1, "ignored", "synthetic")
    null_agent["agent"] = None
    write_journal(lab["telemetry"], "run", "mixed.jsonl", [
        provider(1, "n", 1, "maker", "synthetic"),
        provider(2, "n", 1, "other", "synthetic"),
        provider(3, "n", 1, "None", "synthetic"),
        null_agent,
        provider(5, "n", 1, "qwen", "qwen3.8-max"),
        provider(6, "n", 1, "codex", "gpt-6-sol", role="reviewer"),
        provider(7, "n", 1, "codex", "gpt-6-sol", prompt="SUPER_SECRET_PROMPT", diff="PRIVATE_DIFF"),
        repair(8, "n", 1, "IGNORED_CODE"),
        repair(9, "n", 1, "TEST_FAILURE"),
        provider(10, "n", 2, "codex", "gpt-6-sol", fresh_input_tokens=4),
        provider(11, "n", 3, "codex", "gpt-6-sol", fresh_input_tokens=6, role="reviewer"),
        terminal(12, "n", "ACCEPTED_WITH_NOTES", 2),
        {"utc": utc(13), "event": "autopsy", "node": "n", "round": 1, "code": "stagnation",
         "prompt": "SUPER_SECRET_PROMPT"},
    ])
    write_journal(lab["telemetry"], "other", "synthetic.jsonl", [
        provider(20, "n", 1, "maker", "synthetic"),
        repair(21, "n", 1, "TEST_FAILURE"),
        provider(22, "n", 2, "other", "synthetic"),
        terminal(23, "n", "ACCEPTED"),
    ])
    write_journal(lab["telemetry"], "nested", "deep/hidden.jsonl", [
        *continue_journal(30, code="HIDDEN_CODE"),
    ])
    code, payload = analyze(capsys, lab)
    assert code == 0
    assert payload["status"] == "SHADOW"
    assert payload["release_authority"] is False
    assert payload["decision_points"] == 1
    assert payload["actions"] == {"continue": 1, "swap": 0}
    assert payload["excluded_records"] == 6
    assert payload["excluded_journals"] == 2
    assert payload["journals"] == 2
    assert "HIDDEN_CODE" not in payload["repair_codes"]
    row = row_for(payload, "TEST_FAILURE", "1", "W")
    assert row["covered"] is False
    assert row["recommendation"] == "uncovered"
    assert row["p_success"]["continue"]["successes"] == 1
    assert row["p_success"]["continue"]["mean"] == pytest.approx(beta_mean(1, 1))
    low, high = beta_interval(1, 1)
    assert row["p_success"]["continue"]["low"] == pytest.approx(low)
    assert row["p_success"]["continue"]["high"] == pytest.approx(high)
    assert payload["fresh_input_tokens"]["sum"] == 10
    stored = policy_file(lab)
    report = report_text(lab)
    blob = json.dumps(stored) + report
    assert "SUPER_SECRET_PROMPT" not in blob
    assert "PRIVATE_DIFF" not in blob
    assert stored == payload


def test_swap_round_buckets_truncation_and_shadow(lab, capsys):
    events = []
    cursor = 1
    for offset in range(5):
        events.extend([
            provider(cursor, f"c{offset}", 1, "codex", "gpt-6-sol"),
            repair(cursor + 1, f"c{offset}", 1, "REVIEW_FINDINGS"),
            provider(cursor + 2, f"c{offset}", 2, "codex", "gpt-6-sol"),
            terminal(cursor + 3, f"c{offset}", "ACCEPTED", 2),
        ])
        cursor += 10
    for offset in range(5):
        events.extend([
            provider(cursor, f"s{offset}", 1, "codex", "gpt-6-sol"),
            repair(cursor + 1, f"s{offset}", 1, "REVIEW_FINDINGS"),
            provider(cursor + 2, f"s{offset}", 2, "qwen", "qwen3.8-max"),
            terminal(cursor + 3, f"s{offset}", "BLOCKED", 2),
        ])
        cursor += 10
    for offset in range(5):
        events.extend([
            provider(cursor, f"u{offset}", 1, "codex", "gpt-6-sol"),
            repair(cursor + 1, f"u{offset}", 1, "TEST_FAILURE"),
            provider(cursor + 2, f"u{offset}", 2, "codex", "gpt-6-sol"),
            terminal(cursor + 3, f"u{offset}", "ACCEPTED", 2),
        ])
        cursor += 10
    for offset in range(4):
        events.extend([
            provider(cursor, f"v{offset}", 1, "codex", "gpt-6-sol"),
            repair(cursor + 1, f"v{offset}", 1, "TEST_FAILURE"),
            provider(cursor + 2, f"v{offset}", 2, "qwen", "qwen3.8-max"),
            terminal(cursor + 3, f"v{offset}", "BLOCKED", 2),
        ])
        cursor += 10
    events.extend([
        provider(cursor, "late", 4, "codex", "gpt-6-sol"),
        repair(cursor + 1, "late", 4, "X" * 50),
        provider(cursor + 2, "late", 5, "codex", "gpt-6-sol"),
        terminal(cursor + 3, "late", "ACCEPTED", 5),
    ])
    write_journal(lab["telemetry"], "run", "decisions.jsonl", events)
    # A same node id in another journal must not borrow the maker above.
    write_journal(lab["telemetry"], "isolated", "partial.jsonl", [
        repair(900, "late", 4, "SHOULD_NOT_JOIN"),
        provider(901, "late", 5, "codex", "gpt-6-sol"),
    ])
    code, payload = analyze(capsys, lab, "--min-support", "5", "--failure-cost", "10")
    assert code == 0
    covered = row_for(payload, "REVIEW_FINDINGS", "1", "W")
    assert covered["covered"] is True
    assert covered["recommendation"] == "continue"
    assert covered["support"] == {"continue": 5, "swap": 5}
    assert covered["expected_cost"] == pytest.approx((6 / 7) * 1 + (1 / 7) * 10)
    short = row_for(payload, "TEST_FAILURE", "1", "W")
    assert short["covered"] is False
    assert short["recommendation"] == "uncovered"
    assert short["support"] == {"continue": 5, "swap": 4}
    uncovered = row_for(payload, "X" * 40, "3+", "W")
    assert uncovered["covered"] is False
    assert uncovered["recommendation"] == "uncovered"
    assert len(uncovered["state"]["code"]) == 40
    assert "SHOULD_NOT_JOIN" not in payload["repair_codes"]
    assert payload["coverage"]["covered_points"] == 10
    assert payload["coverage"]["decision_points"] == 20
    assert payload["coverage"]["percent"] == pytest.approx(50.0)
    assert payload["actions"] == {"continue": 11, "swap": 9}
    assert payload["repair_codes"]["REVIEW_FINDINGS"] == 10
    assert payload["repair_codes"]["TEST_FAILURE"] == 9
    assert payload["shadow"]["covered_points"] == 10
    assert payload["shadow"]["agree"] == 5
    assert payload["shadow"]["agree_success"] == 5
    assert payload["shadow"]["disagree"] == 5
    assert payload["shadow"]["disagree_not_success"] == 5
    assert payload["shadow"]["causal_claim"] is False
    report = report_text(lab)
    assert "Saturacja" in report and "p_new_upper" in report
    assert "przedzial" in report and "Pokrycie" in report
    assert "Porownanie cieniowe" in report
    assert dp.CAVEAT in report
    assert "obserwacyjne" in report and "nielosowe" in report and "przyczynowosci" in report
    assert "REVIEW_FINDINGS" in report and "swap" in report


def test_chain_cost_and_beta_prior_transitions(lab, capsys, monkeypatch):
    write_journal(lab["telemetry"], "chain", "one.jsonl", [
        provider(1, "n", 1, "codex", "gpt-6-sol"),
        repair(2, "n", 1, "TEST_FAILURE"),
        provider(3, "n", 2, "codex", "gpt-6-sol"),
        repair(4, "n", 2, "TEST_FAILURE"),
        provider(5, "n", 3, "codex", "gpt-6-sol"),
        terminal(6, "n", "ACCEPTED", 4),
    ])
    code, payload = analyze(capsys, lab, "--min-support", "1")
    assert code == 0
    # Round 2 is one observed success (2 extra rounds) plus the unobserved failure prior.
    # Round 1 is one next-state plus unobserved success (step cost 0) and failure.
    assert row_for(payload, "TEST_FAILURE", "2", "W")["expected_cost"] == pytest.approx(14 / 3)
    assert row_for(payload, "TEST_FAILURE", "1", "W")["expected_cost"] == pytest.approx(16 / 3)
    assert row_for(payload, "TEST_FAILURE", "1", "W")["recommendation"] == "uncovered"

    seen = {}
    real = dp.value_iteration

    def wrapped(transitions, terminal_costs, max_iter=1000, tol=1e-9):
        seen["transitions"] = transitions
        return real(transitions, terminal_costs, max_iter=max_iter, tol=tol)

    monkeypatch.setattr(dp, "value_iteration", wrapped)
    lab2_root = make_dir()
    tele = lab2_root / "telemetry"
    tele.mkdir()
    out = lab2_root / "out"
    catalog = lab["catalog"]
    events = []
    cursor = 1
    for index in range(3):
        events.extend(continue_journal(cursor, node=f"ok{index}", terminal_status="ACCEPTED", terminal_round=2))
        cursor += 10
    events.extend(continue_journal(cursor, node="bad", terminal_status="BLOCKED", terminal_round=2))
    cursor += 10
    events.extend([
        provider(cursor, "swap", 1, "codex", "gpt-6-sol"),
        repair(cursor + 1, "swap", 1, "TEST_FAILURE"),
        provider(cursor + 2, "swap", 2, "qwen", "qwen3.8-max"),
        terminal(cursor + 3, "swap", "ACCEPTED", 2),
    ])
    write_journal(tele, "mix", "events.jsonl", events)
    code, payload = run(
        capsys, "analyze", "--telemetry-root", str(tele), "--catalog", str(catalog),
        "--out", str(out), "--min-support", "1", "--failure-cost", "10",
    )
    assert code == 0
    triples = seen["transitions"][(("TEST_FAILURE", "1", "W"), "continue")]
    assert sum(prob for prob, _, _ in triples) == pytest.approx(1.0)
    success = sum(prob for prob, _, cost in triples if cost < 5)
    failure = sum(prob for prob, _, cost in triples if cost >= 5)
    assert success == pytest.approx(4 / 6)
    assert failure == pytest.approx(2 / 6)
    row = row_for(payload, "TEST_FAILURE", "1", "W")
    assert row["p_success"]["continue"]["mean"] == pytest.approx(beta_mean(3, 4))
    low, high = beta_interval(3, 4)
    assert (row["p_success"]["continue"]["low"], row["p_success"]["continue"]["high"]) == (
        pytest.approx(low), pytest.approx(high),
    )
    assert low <= row["p_success"]["continue"]["mean"] <= high


def test_saturation_follows_time_not_path_order(lab, capsys):
    # Path order is a-late, m-mid, z-early. Time order is FIRST, FIRST, SECOND.
    write_journal(lab["telemetry"], "a-late", "events.jsonl", continue_journal(30, node="b", code="SECOND"))
    write_journal(lab["telemetry"], "z-early", "one.jsonl", continue_journal(1, node="a", code="FIRST"))
    write_journal(lab["telemetry"], "m-mid", "two.jsonl", continue_journal(10, node="c", code="FIRST"))
    code, payload = analyze(capsys, lab, "--min-support", "1")
    assert code == 0
    assert payload["saturation"]["unique"] == 2
    assert payload["saturation"]["first_seen"] == [0, 2]
    assert payload["saturation"]["since_last_new"] == 0
    assert payload["saturation"]["p_new_upper"] is None
    assert payload["saturation"]["p_new_upper"] == rule_of_three(0)


def test_no_data_paths(lab, capsys):
    missing = lab["root"] / "missing-telemetry"
    code, payload = run(
        capsys, "analyze", "--telemetry-root", str(missing), "--catalog", str(lab["catalog"]),
        "--out", str(lab["out"]),
    )
    assert code == 0
    assert payload["status"] == "NO_DATA"
    assert payload["release_authority"] is False
    assert payload["gap"] == dp.GAP_NO_DATA
    assert payload["decision_points"] == 0
    assert payload["saturation"]["unique"] == 0
    assert payload["saturation"]["p_new_upper"] is None
    assert payload["coverage"]["percent"] == 0
    report = report_text(lab)
    assert "Luka" in report and dp.GAP_NO_DATA in report
    assert "Tabela polityki" in report and "przedzial" in report
    assert dp.CAVEAT in report and "Porownanie cieniowe" in report

    empty_out = lab["root"] / "empty-out"
    code, payload = run(
        capsys, "analyze", "--telemetry-root", str(lab["telemetry"]), "--catalog", str(lab["catalog"]),
        "--out", str(empty_out),
    )
    assert code == 0 and payload["status"] == "NO_DATA"
    assert (empty_out / "policy.json").is_file()
    assert (empty_out / "report.md").is_file()


def test_limits_skip_bad_lines_and_keep_later_events(lab, capsys):
    write_journal(lab["telemetry"], "a-kept", "events.jsonl", continue_journal(1, code="ONLY_A"))
    write_journal(lab["telemetry"], "b-dropped", "events.jsonl", continue_journal(10, code="ONLY_B"))
    code, payload = analyze(capsys, lab, "--max-files", "1")
    assert code == 0
    assert payload["files_matched"] == 2
    assert payload["files_read"] == 1
    assert "ONLY_A" in payload["repair_codes"]
    assert "ONLY_B" not in payload["repair_codes"]

    limited = lab["root"] / "limited"
    tele = limited / "telemetry"
    write_journal(tele, "run", "short.jsonl", [
        provider(1, "n", 1, "codex", "gpt-6-sol"),
        repair(2, "n", 1, "TEST_FAILURE"),
        provider(3, "n", 2, "codex", "gpt-6-sol"),
    ])
    out = limited / "out"
    code, payload = run(
        capsys, "analyze", "--telemetry-root", str(tele), "--catalog", str(lab["catalog"]),
        "--out", str(out), "--max-lines", "2",
    )
    assert code == 0
    assert payload["status"] == "NO_DATA"
    assert payload["truncated_files"] == 1
    assert payload["decision_points"] == 0

    noisy = lab["root"] / "noisy"
    folder = noisy / "telemetry" / "run"
    folder.mkdir(parents=True)
    events = [
        provider(1, "n", 1, "codex", "gpt-6-sol"),
        "not-json",
        "z" * 800,
        repair(2, "n", 1, "TEST_FAILURE"),
        provider(3, "n", 2, "codex", "gpt-6-sol", fresh_input_tokens=None),
        terminal(4, "n", "ACCEPTED", 2),
    ]
    (folder / "events.jsonl").write_text(
        "\n".join(event if isinstance(event, str) else json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )
    out = noisy / "out"
    code, payload = run(
        capsys, "analyze", "--telemetry-root", str(noisy / "telemetry"), "--catalog", str(lab["catalog"]),
        "--out", str(out), "--max-line-bytes", "500",
    )
    assert code == 0
    assert payload["status"] == "SHADOW"
    assert payload["invalid_lines"] >= 2
    assert payload["decision_points"] == 1
    assert payload["fresh_input_tokens"]["missing"] == 1
    assert payload["fresh_input_tokens"]["sum"] is None


def test_out_inside_skill_and_relative_out_are_refused(lab, capsys):
    forbidden = dp.SKILL_ROOT / "scripts" / "policy-out-forbidden"
    code, payload = run(
        capsys, "analyze", "--telemetry-root", str(lab["telemetry"]), "--out", str(forbidden),
    )
    assert code == 1
    assert payload == {
        "status": "ERROR",
        "code": "OUT_IN_SKILL",
        "message": "--out must not lie inside the skill tree",
        "release_authority": False,
    }
    assert not forbidden.exists()

    code, payload = run(capsys, "analyze", "--out", "relative-policy-out")
    assert code == 1
    assert payload["status"] == "ERROR"
    assert payload["code"] == "OUT_NOT_ABSOLUTE"
    assert payload["release_authority"] is False


def test_catalog_resolution_and_invalid_catalog(lab, capsys, monkeypatch):
    monkeypatch.setenv("AGENT_MODEL_CATALOG", str(lab["catalog"]))
    write_journal(lab["telemetry"], "run", "events.jsonl", continue_journal(1))
    code, payload = run(
        capsys, "analyze", "--telemetry-root", str(lab["telemetry"]), "--out", str(lab["out"]),
    )
    assert code == 0
    assert row_for(payload, "TEST_FAILURE", "1", "W")["state"]["tier"] == "W"

    monkeypatch.delenv("AGENT_MODEL_CATALOG", raising=False)
    out = lab["root"] / "sibling-out"
    write_journal(lab["telemetry"], "run", "sibling.jsonl", [
        provider(50, "s", 1, "codex", "gpt-6-sol"),
        repair(51, "s", 1, "FROM_SIBLING"),
        provider(52, "s", 2, "codex", "gpt-6-sol"),
        terminal(53, "s", "ACCEPTED", 2),
    ])
    code, payload = run(
        capsys, "analyze", "--telemetry-root", str(lab["telemetry"]), "--out", str(out),
    )
    assert code == 0
    sibling = json.loads(dp.SIBLING_CATALOG.read_text(encoding="utf-8"))
    expected = next(
        model["tier"] for model in sibling["models"]
        if any(profile.get("agent") == "codex" and profile.get("model") == "gpt-6-sol"
               for profile in model.get("profiles", []))
    )
    assert row_for(payload, "FROM_SIBLING", "1", expected)["state"]["tier"] == expected

    missing = lab["root"] / "missing-catalog.json"
    code, payload = run(
        capsys, "analyze", "--telemetry-root", str(lab["telemetry"]), "--catalog", str(missing),
        "--out", str(lab["root"] / "missing-cat-out"),
    )
    assert code == 1 and payload["code"] == "CATALOG_MISSING"

    invalid = lab["root"] / "invalid-catalog.json"
    invalid.write_text("{", encoding="utf-8")
    code, payload = run(
        capsys, "analyze", "--telemetry-root", str(lab["telemetry"]), "--catalog", str(invalid),
        "--out", str(lab["root"] / "invalid-cat-out"),
    )
    assert code == 1 and payload["code"] == "CATALOG_INVALID"

    code, payload = analyze(capsys, lab, "--exclude-agent", "codex", "--min-support", "1")
    assert code == 0
    assert payload["status"] == "NO_DATA"
    assert payload["excluded_records"] > 0
    assert payload["decision_points"] == 0


def test_tie_prefers_continue_and_swap_can_win(lab, capsys):
    events = []
    cursor = 1
    events.extend(continue_journal(cursor, node="same-a", terminal_status="ACCEPTED", terminal_round=2))
    cursor += 10
    events.extend([
        provider(cursor, "same-b", 1, "codex", "gpt-6-sol"),
        repair(cursor + 1, "same-b", 1, "TEST_FAILURE"),
        provider(cursor + 2, "same-b", 2, "qwen", "qwen3.8-max"),
        terminal(cursor + 3, "same-b", "ACCEPTED", 2),
    ])
    write_journal(lab["telemetry"], "tie", "events.jsonl", events)
    code, payload = analyze(capsys, lab, "--min-support", "1")
    assert row_for(payload, "TEST_FAILURE", "1", "W")["recommendation"] == "continue"

    swap_root = lab["root"] / "swap-wins"
    tele = swap_root / "telemetry"
    events = []
    cursor = 1
    events.extend(continue_journal(cursor, node="fail", terminal_status="BLOCKED", terminal_round=2))
    cursor += 10
    events.extend([
        provider(cursor, "good-swap", 1, "codex", "gpt-6-sol"),
        repair(cursor + 1, "good-swap", 1, "TEST_FAILURE"),
        provider(cursor + 2, "good-swap", 2, "qwen", "qwen3.8-max"),
        terminal(cursor + 3, "good-swap", "ACCEPTED", 2),
    ])
    write_journal(tele, "run", "events.jsonl", events)
    code, payload = run(
        capsys, "analyze", "--telemetry-root", str(tele), "--catalog", str(lab["catalog"]),
        "--out", str(swap_root / "out"), "--min-support", "1", "--failure-cost", "10",
    )
    assert code == 0
    row = row_for(payload, "TEST_FAILURE", "1", "W")
    assert row["recommendation"] == "swap"
    assert row["expected_cost"] == pytest.approx((2 / 3) * 1 + (1 / 3) * 10)
    assert row["covered"] is True


def test_module_stays_local_and_does_not_delete(capsys):
    tree = ast.parse((SCRIPT_DIR / "decision_policy.py").read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
        elif isinstance(node, ast.Attribute) and node.attr in {"remove", "unlink", "rmtree", "removedirs"}:
            pytest.fail(node.attr)
    assert modules.isdisjoint({"subprocess", "socket", "urllib", "http", "requests", "ctypes"})
    code, payload = run(capsys, "analyze")
    assert code == 1 and payload["code"] == "BAD_ARGUMENT" and payload["release_authority"] is False
    assert dp.DEFAULT_MAX_FILES == 20000
    assert dp.DEFAULT_MAX_LINES == 10000
    assert dp.DEFAULT_MAX_LINE_BYTES == 65536
    assert dp.DEFAULT_MIN_SUPPORT == 5
    assert dp.DEFAULT_FAILURE_COST == 10.0
    assert dp.DEFAULT_TELEMETRY_ROOT == Path.home() / ".agents" / "telemetry" / "todo"


def _action_cost(triples) -> float:
    return sum(prob * cost for prob, _destination, cost in triples)


def test_unobserved_outcomes_keep_beta_prior(lab, capsys, monkeypatch):
    events = []
    cursor = 1
    for index in range(100):
        events.extend(continue_journal(
            cursor, node=f"c{index}", terminal_status="ACCEPTED", terminal_round=3,
        ))
        cursor += 10
    for index in range(5):
        events.extend([
            provider(cursor, f"s{index}", 1, "codex", "gpt-6-sol"),
            repair(cursor + 1, f"s{index}", 1, "TEST_FAILURE"),
            provider(cursor + 2, f"s{index}", 2, "qwen", "qwen3.8-max"),
            terminal(cursor + 3, f"s{index}", "ACCEPTED", 2),
        ])
        cursor += 10
    write_journal(lab["telemetry"], "prior", "events.jsonl", events)
    seen = {}
    real = dp.value_iteration

    def wrapped(transitions, terminal_costs, max_iter=1000, tol=1e-9):
        seen["transitions"] = transitions
        return real(transitions, terminal_costs, max_iter=max_iter, tol=tol)

    monkeypatch.setattr(dp, "value_iteration", wrapped)
    code, payload = analyze(capsys, lab, "--min-support", "5", "--failure-cost", "10")
    assert code == 0
    state = ("TEST_FAILURE", "1", "W")
    continue_rows = seen["transitions"][(state, "continue")]
    swap_rows = seen["transitions"][(state, "swap")]
    assert len(continue_rows) == 2
    assert len(swap_rows) == 2
    continue_cost = _action_cost(continue_rows)
    swap_cost = _action_cost(swap_rows)
    assert continue_cost == pytest.approx((101 / 102) * 2 + (1 / 102) * 10)
    assert swap_cost == pytest.approx((6 / 7) * 1 + (1 / 7) * 10)
    assert continue_cost == pytest.approx(2.0784313725490197)
    assert swap_cost == pytest.approx(2.2857142857142856)
    assert sum(prob for prob, dest, _cost in continue_rows if dest == dp.TERM_FAILURE) == pytest.approx(1 / 102)
    row = row_for(payload, "TEST_FAILURE", "1", "W")
    assert row["recommendation"] == "continue"
    assert row["covered"] is True
    assert row["expected_cost"] == pytest.approx(continue_cost)
    assert row["support"] == {"continue": 100, "swap": 5}
    assert row["p_success"]["continue"]["mean"] == pytest.approx(beta_mean(100, 100))
    assert row["p_success"]["continue"]["mean"] < 1


def test_missing_terminal_is_not_a_failure(lab, capsys):
    clipped = lab["root"] / "clipped"
    tele = clipped / "telemetry"
    write_journal(tele, "run", "events.jsonl", continue_journal(1))
    code, payload = run(
        capsys, "analyze", "--telemetry-root", str(tele), "--catalog", str(lab["catalog"]),
        "--out", str(clipped / "out"), "--max-lines", "3", "--min-support", "1", "--failure-cost", "10",
    )
    assert code == 0
    assert payload["status"] == "NO_DATA"
    assert payload["truncated_files"] == 1
    assert payload["decision_points"] == 1
    assert payload["resolved_points"] == 0
    assert payload["unresolved_points"] == 1
    assert payload["actions"] == {"continue": 0, "swap": 0}
    assert payload["policy"] == []
    assert payload["gap"] == dp.GAP_UNRESOLVED
    assert payload["release_authority"] is False
    assert "bez przypisania porazki" in report_text({"out": clipped / "out"})
    assert dp.GAP_UNRESOLVED in report_text({"out": clipped / "out"})

    events = []
    cursor = 1
    for index in range(6):
        events.extend(continue_journal(cursor, node=f"c{index}", terminal_status="ACCEPTED", terminal_round=2))
        cursor += 10
    for index in range(5):
        events.extend([
            provider(cursor, f"s{index}", 1, "codex", "gpt-6-sol"),
            repair(cursor + 1, f"s{index}", 1, "TEST_FAILURE"),
            provider(cursor + 2, f"s{index}", 2, "qwen", "qwen3.8-max"),
            terminal(cursor + 3, f"s{index}", "ACCEPTED", 2),
        ])
        cursor += 10
    for index in range(3):
        events.extend([
            provider(cursor, f"o{index}", 1, "codex", "gpt-6-sol"),
            repair(cursor + 1, f"o{index}", 1, "TEST_FAILURE"),
            provider(cursor + 2, f"o{index}", 2, "codex", "gpt-6-sol"),
        ])
        cursor += 10
    poison = lab["root"] / "poison"
    write_journal(poison / "telemetry", "run", "events.jsonl", events)
    code, payload = run(
        capsys, "analyze", "--telemetry-root", str(poison / "telemetry"), "--catalog", str(lab["catalog"]),
        "--out", str(poison / "out"), "--min-support", "5", "--failure-cost", "10",
    )
    assert code == 0
    assert payload["status"] == "SHADOW"
    assert payload["decision_points"] == 14
    assert payload["resolved_points"] == 11
    assert payload["unresolved_points"] == 3
    assert payload["actions"] == {"continue": 6, "swap": 5}
    row = row_for(payload, "TEST_FAILURE", "1", "W")
    assert row["recommendation"] == "continue"
    assert row["support"] == {"continue": 6, "swap": 5}
    assert row["p_success"]["continue"]["successes"] == 6
    assert row["p_success"]["continue"]["trials"] == 6
    assert row["expected_cost"] == pytest.approx((7 / 8) * 1 + (1 / 8) * 10)
    assert payload["shadow"]["agree"] == 6
    assert payload["shadow"]["agree_success"] == 6
    assert payload["shadow"]["agree_not_success"] == 0
    assert payload["shadow"]["disagree"] == 5
    assert payload["shadow"]["disagree_success"] == 5
    assert payload["coverage"]["covered_points"] == 14
    assert payload["coverage"]["percent"] == pytest.approx(100.0)
    report = report_text({"out": poison / "out"})
    assert "bez przypisania porazki" in report
    assert "3" in report


def test_unresolved_states_count_toward_inventory_and_saturation(lab, capsys):
    """A new state without node_terminal is observed, not a policy outcome.

    Two decision points and two states used to collapse to states=1 and
    saturation.unique=1 whenever the second episode was still open.
    """
    write_journal(lab["telemetry"], "resolved", "one.jsonl", continue_journal(
        1, node="a", code="REVIEW_FINDINGS",
    ))
    write_journal(lab["telemetry"], "open", "two.jsonl", [
        provider(20, "b", 1, "codex", "gpt-6-sol"),
        repair(21, "b", 1, "TEST_FAILURE"),
        provider(22, "b", 2, "qwen", "qwen3.8-max"),
    ])
    code, payload = analyze(capsys, lab, "--min-support", "1", "--failure-cost", "10")
    assert code == 0
    assert payload["status"] == "SHADOW"
    assert payload["release_authority"] is False
    assert payload["decision_points"] == 2
    assert payload["resolved_points"] == 1
    assert payload["unresolved_points"] == 1
    assert payload["states"] == 2
    assert payload["saturation"]["unique"] == 2
    assert payload["saturation"]["first_seen"] == [0, 1]
    assert payload["saturation"]["since_last_new"] == 0
    assert payload["saturation"]["p_new_upper"] is None
    assert payload["actions"] == {"continue": 1, "swap": 0}
    assert [row["state"]["code"] for row in payload["policy"]] == ["REVIEW_FINDINGS"]
    row = row_for(payload, "REVIEW_FINDINGS", "1", "W")
    assert row["support"] == {"continue": 1, "swap": 0}
    assert row["covered"] is False
    assert row["recommendation"] == "uncovered"
    assert row["p_success"]["continue"]["trials"] == 1
    assert row["p_success"]["swap"]["trials"] == 0
    report = report_text(lab)
    assert "takze bez node_terminal" in report
    assert "nie wchodzi do estymacji polityki" in report
    assert dp.CAVEAT in report

    # Repeating a known state and then seeing a new open state must end the
    # no-new-state run. Dropping the open point would leave unique=1 and a
    # positive rule-of-three bound.
    root = make_dir()
    tele = root / "telemetry"
    tele.mkdir()
    write_journal(tele, "a", "early.jsonl", continue_journal(1, node="a", code="REVIEW_FINDINGS"))
    write_journal(tele, "b", "mid.jsonl", continue_journal(10, node="c", code="REVIEW_FINDINGS"))
    write_journal(tele, "c", "late.jsonl", [
        provider(30, "b", 1, "codex", "gpt-6-sol"),
        repair(31, "b", 1, "TEST_FAILURE"),
        provider(32, "b", 2, "codex", "gpt-6-sol"),
    ])
    code, payload = run(
        capsys, "analyze",
        "--telemetry-root", str(tele),
        "--catalog", str(lab["catalog"]),
        "--out", str(root / "out"),
        "--min-support", "5",
        "--failure-cost", "10",
    )
    assert code == 0
    assert payload["decision_points"] == 3
    assert payload["resolved_points"] == 2
    assert payload["unresolved_points"] == 1
    assert payload["states"] == 2
    assert payload["saturation"]["unique"] == 2
    assert payload["saturation"]["first_seen"] == [0, 2]
    assert payload["saturation"]["since_last_new"] == 0
    assert payload["saturation"]["p_new_upper"] is None
    assert payload["saturation"]["p_new_upper"] != rule_of_three(1)
    assert payload["actions"] == {"continue": 2, "swap": 0}
    assert [row["state"]["code"] for row in payload["policy"]] == ["REVIEW_FINDINGS"]
    assert "TEST_FAILURE" not in payload["repair_codes"]


def test_max_line_bytes_counts_utf8_bytes(lab, capsys):
    prefix = '{"utc":"2026-10-21T00:00:00Z","event":"provider_result","node":"'
    suffix = '","round":0,"agent":"maker","model":"m"}\n'
    big = None
    for pad in range(0, 8):
        for euros in range(0, 40):
            candidate = prefix + ("a" * pad) + ("\u20ac" * euros) + suffix
            if len(candidate.encode("utf-8")) == 168 and len(candidate) <= 128:
                big = candidate
                break
        if big is not None:
            break
    assert big is not None
    parsed = json.loads(big)
    assert parsed["event"] == "provider_result"
    assert parsed["agent"] == "maker"
    small = '{"utc":"2026-10-21T00:00:02Z","event":"repair","node":"\u20ac","round":1,"code":"OK"}\n'
    assert len(small.encode("utf-8")) <= 128
    assert len(small) < len(small.encode("utf-8"))
    folder = lab["root"] / "bytes" / "telemetry" / "run"
    folder.mkdir(parents=True)
    target = folder / "events.jsonl"
    target.write_bytes(big.encode("utf-8") + small.encode("utf-8"))
    kept, invalid, truncated = dp._read_journal(target, 10, 128)
    assert invalid == 1
    assert truncated is False
    assert len(kept) == 1
    assert "OK" in kept[0]
    assert "\u20ac" in kept[0]
    code, payload = run(
        capsys, "analyze", "--telemetry-root", str(folder.parent), "--catalog", str(lab["catalog"]),
        "--out", str(lab["root"] / "bytes-out"), "--max-line-bytes", "128",
    )
    assert code == 0
    assert payload["invalid_lines"] == 1
    assert payload["excluded_records"] == 0
    assert payload["decision_points"] == 0
    assert payload["status"] == "NO_DATA"

    write_journal(lab["telemetry"], "multi", "events.jsonl", continue_journal(1, node="n\u20ac"))
    code, payload = analyze(capsys, lab, "--max-line-bytes", "512")
    assert code == 0
    assert payload["invalid_lines"] == 0
    assert payload["decision_points"] == 1
    assert payload["status"] == "SHADOW"


@pytest.mark.parametrize("raw", ["nan", "NaN", "inf", "+inf", "-inf", "Infinity"])
def test_nonfinite_failure_cost_is_bad_argument(lab, capsys, raw):
    code, payload = run(
        capsys, "analyze",
        "--telemetry-root", str(lab["telemetry"]),
        "--catalog", str(lab["catalog"]),
        "--out", str(lab["out"]),
        f"--failure-cost={raw}",
    )
    assert code == 1
    assert payload["status"] == "ERROR"
    assert payload["code"] == "BAD_ARGUMENT"
    assert payload["release_authority"] is False
    assert "finite" in payload["message"]


def test_excluded_provider_round_does_not_change_cost(lab, capsys):
    events = []
    cursor = 1
    for index in range(6):
        chunk = continue_journal(cursor, node=f"c{index}", terminal_status="ACCEPTED", terminal_round=2)
        if index == 0:
            chunk.append(provider(cursor + 4, f"c{index}", 1000000, "maker", "synthetic"))
        events.extend(chunk)
        cursor += 10
    for index in range(5):
        events.extend([
            provider(cursor, f"s{index}", 1, "codex", "gpt-6-sol"),
            repair(cursor + 1, f"s{index}", 1, "TEST_FAILURE"),
            provider(cursor + 2, f"s{index}", 2, "qwen", "qwen3.8-max"),
            terminal(cursor + 3, f"s{index}", "ACCEPTED", 2),
        ])
        cursor += 10
    write_journal(lab["telemetry"], "run", "events.jsonl", events)
    code, payload = analyze(capsys, lab, "--min-support", "5", "--failure-cost", "10")
    assert code == 0
    assert payload["status"] == "SHADOW"
    assert payload["excluded_records"] == 1
    assert payload["release_authority"] is False
    row = row_for(payload, "TEST_FAILURE", "1", "W")
    assert row["covered"] is True
    assert row["recommendation"] == "continue"
    assert row["support"] == {"continue": 6, "swap": 5}
    assert row["expected_cost"] == pytest.approx((7 / 8) * 1 + (1 / 8) * 10)
    assert payload["actions"] == {"continue": 6, "swap": 5}


def test_malformed_records_are_skipped_and_counted(lab, capsys):
    folder = lab["root"] / "malformed" / "telemetry" / "run"
    folder.mkdir(parents=True)
    huge_object = (
        '{"event":"repair","node":"n","code":"TEST_FAILURE","round":'
        + ("9" * 5000)
        + ',"utc":"2026-10-21T00:00:00Z"}'
    )
    huge_number = "9" * 5000
    bad_utc = {
        "utc": "0001-01-01T00:00:00+01:00",
        "event": "provider_result",
        "node": "n",
        "round": 1000000,
        "role": "maker",
        "agent": "codex",
        "model": "gpt-6-sol",
        "fresh_input_tokens": 50,
        "status": "COMPLETED",
    }
    encoded = [huge_number, huge_object, json.dumps(bad_utc)]
    assert all(len(line.encode("utf-8")) <= dp.DEFAULT_MAX_LINE_BYTES for line in encoded)
    swap_events = [
        provider(20, "s", 1, "codex", "gpt-6-sol"),
        repair(21, "s", 1, "TEST_FAILURE"),
        provider(22, "s", 2, "qwen", "qwen3.8-max"),
        terminal(23, "s", "ACCEPTED", 2),
    ]
    lines = encoded + [json.dumps(event) for event in continue_journal(1, node="n")]
    lines.extend(json.dumps(event) for event in swap_events)
    (folder / "events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    code, payload = run(
        capsys, "analyze",
        "--telemetry-root", str(folder.parent),
        "--catalog", str(lab["catalog"]),
        "--out", str(lab["root"] / "malformed-out"),
        "--min-support", "1",
        "--failure-cost", "10",
    )
    assert code == 0
    assert payload["status"] == "SHADOW"
    assert payload["invalid_lines"] == 3
    assert payload["excluded_records"] == 0
    assert payload["decision_points"] == 2
    assert payload["release_authority"] is False
    row = row_for(payload, "TEST_FAILURE", "1", "W")
    assert row["covered"] is True
    assert row["support"] == {"continue": 1, "swap": 1}
    assert row["recommendation"] == "continue"
    assert row["expected_cost"] == pytest.approx((2 / 3) * 1 + (1 / 3) * 10)
    assert "0001-01-01" not in report_text({"out": lab["root"] / "malformed-out"})
    assert dp.parse_utc("0001-01-01T00:00:00+01:00") is None
    assert dp.parse_utc("2026-10-21T00:00:00Z") == datetime(2026, 10, 21, tzinfo=timezone.utc)


@pytest.mark.parametrize("exc", [
    OverflowError("date value out of range"),
    ValueError("integer string conversion"),
    RecursionError("maximum recursion depth exceeded NESTED_SECRET_SHOULD_NOT_LEAK"),
])
def test_escaped_parser_errors_return_json(lab, capsys, monkeypatch, exc):
    def boom(*_args, **_kwargs):
        raise exc

    monkeypatch.setattr(dp, "build_result", boom)
    code, payload = analyze(capsys, lab)
    assert code == 1
    assert payload == {
        "status": "ERROR",
        "code": "INVALID_TELEMETRY",
        "message": "telemetry value could not be parsed",
        "release_authority": False,
    }


def _policy_snapshot(payload):
    return {
        "policy": payload["policy"],
        "shadow": payload["shadow"],
        "actions": payload["actions"],
        "fresh_input_tokens": payload["fresh_input_tokens"],
        "repair_codes": payload["repair_codes"],
        "decision_points": payload["decision_points"],
        "resolved_points": payload["resolved_points"],
        "unresolved_points": payload["unresolved_points"],
        "coverage": payload["coverage"],
        "saturation": payload["saturation"],
        "states": payload["states"],
    }


def test_synthetic_round_repair_and_autopsy_do_not_change_policy(lab, capsys):
    """Same 6/5 set before and after a synthetic maker round at 1000000.

    One continue node has a real later allowed round. Terminals omit round.
    The added provider, repair and autopsy must not move cost or the shadow.
    """
    events = []
    cursor = 1
    for index in range(6):
        chunk = continue_journal(
            cursor, node=f"c{index}", terminal_status="ACCEPTED", terminal_round=None,
        )
        if index == 1:
            chunk.insert(-1, provider(
                cursor + 4, f"c{index}", 3, "codex", "gpt-6-sol", fresh_input_tokens=7,
            ))
        events.extend(chunk)
        cursor += 10
    for index in range(5):
        events.extend([
            provider(cursor, f"s{index}", 1, "codex", "gpt-6-sol"),
            repair(cursor + 1, f"s{index}", 1, "TEST_FAILURE"),
            provider(cursor + 2, f"s{index}", 2, "qwen", "qwen3.8-max"),
            terminal(cursor + 3, f"s{index}", "ACCEPTED"),
        ])
        cursor += 10
    write_journal(lab["telemetry"], "run", "events.jsonl", events)
    code, before = analyze(capsys, lab, "--min-support", "5", "--failure-cost", "10")
    assert code == 0
    assert before["status"] == "SHADOW"
    assert before["release_authority"] is False
    assert before["excluded_records"] == 0
    assert before["excluded_journals"] == 0
    assert before["resolved_points"] == 11
    assert before["unresolved_points"] == 0
    row = row_for(before, "TEST_FAILURE", "1", "W")
    assert row["covered"] is True
    assert row["recommendation"] == "continue"
    assert row["support"] == {"continue": 6, "swap": 5}
    # Five continue successes cost 1; the real round-3 maker costs 2. Swap stays at 1.
    assert row["expected_cost"] == pytest.approx((7 / 8) * (7 / 6) + (1 / 8) * 10)
    assert before["fresh_input_tokens"]["sum"] == 36
    assert before["shadow"]["agree"] == 6
    assert before["shadow"]["agree_success"] == 6
    assert before["shadow"]["disagree"] == 5
    assert before["shadow"]["disagree_success"] == 5
    assert "round" not in json.loads(
        (lab["telemetry"] / "run" / "events.jsonl").read_text(encoding="utf-8").splitlines()[3]
    )

    poisoned = list(events)
    poisoned.extend([
        provider(9000, "c0", 1000000, "maker", "synthetic", fresh_input_tokens=999),
        repair(9001, "c0", 1000000, "REVIEW_FINDINGS"),
        {"utc": utc(9002), "event": "autopsy", "node": "c0", "round": 1000000, "code": "stagnation"},
    ])
    write_journal(lab["telemetry"], "run", "events.jsonl", poisoned)
    after_out = lab["root"] / "out-after-synthetic-round"
    code, after = run(
        capsys, "analyze",
        "--telemetry-root", str(lab["telemetry"]),
        "--catalog", str(lab["catalog"]),
        "--out", str(after_out),
        "--min-support", "5",
        "--failure-cost", "10",
    )
    assert code == 0
    assert after["status"] == "SHADOW"
    assert after["release_authority"] is False
    assert _policy_snapshot(after) == _policy_snapshot(before)
    assert after["excluded_records"] == before["excluded_records"] + 1
    assert after["excluded_journals"] == before["excluded_journals"] + 1
    assert after["invalid_lines"] == before["invalid_lines"]
    assert row_for(after, "TEST_FAILURE", "1", "W")["recommendation"] == "continue"
    assert "REVIEW_FINDINGS" not in after["repair_codes"]
    report = report_text({"out": after_out})
    assert "stagnation" not in report
    assert dp.CAVEAT in report


def test_deeply_nested_json_line_is_skipped_and_journal_continues(lab, capsys):
    nested = "[" * 30000 + "]" * 30000
    assert len((nested + "\n").encode("utf-8")) <= dp.DEFAULT_MAX_LINE_BYTES
    with pytest.raises(RecursionError):
        json.loads(nested)
    folder = lab["root"] / "nested" / "telemetry" / "run"
    folder.mkdir(parents=True)
    later = continue_journal(1, node="n")
    later.extend([
        provider(20, "s", 1, "codex", "gpt-6-sol"),
        repair(21, "s", 1, "TEST_FAILURE"),
        provider(22, "s", 2, "qwen", "qwen3.8-max"),
        terminal(23, "s", "ACCEPTED", 2),
    ])
    body = [nested.encode("utf-8")]
    body.extend(json.dumps(event).encode("utf-8") for event in later)
    (folder / "events.jsonl").write_bytes(b"\n".join(body) + b"\n")
    code, payload = run(
        capsys, "analyze",
        "--telemetry-root", str(folder.parent),
        "--catalog", str(lab["catalog"]),
        "--out", str(lab["root"] / "nested-out"),
        "--min-support", "1",
        "--failure-cost", "10",
    )
    assert code == 0
    assert payload["status"] == "SHADOW"
    assert payload["release_authority"] is False
    assert payload["invalid_lines"] == 1
    assert payload["excluded_records"] == 0
    assert payload["decision_points"] == 2
    row = row_for(payload, "TEST_FAILURE", "1", "W")
    assert row["covered"] is True
    assert row["support"] == {"continue": 1, "swap": 1}
    assert row["recommendation"] == "continue"
    assert row["expected_cost"] == pytest.approx((2 / 3) * 1 + (1 / 3) * 10)


def test_write_error_returns_json(lab, capsys, monkeypatch):
    def boom(_path, _text):
        raise PermissionError("denied")

    monkeypatch.setattr(dp, "atomic_text", boom)
    code, payload = analyze(capsys, lab)
    assert code == 1
    assert payload == {
        "status": "ERROR",
        "code": "IO_ERROR",
        "message": "failed to write the policy report",
        "release_authority": False,
    }
