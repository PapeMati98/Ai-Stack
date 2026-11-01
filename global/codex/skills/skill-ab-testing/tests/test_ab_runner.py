"""Offline product tests. Test-owned state is retained, never cleaned up."""
import ast
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

import pytest

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "scripts" / "ab_runner.py"
REPO = HERE.parents[4]


def locate_test_controller():
    candidates = []
    override = os.environ.get("TESTY_AB_TEST_CONTROLLER")
    if override:
        candidates.append(Path(override))
    candidates.extend((HERE.parents[1] / "skill-evolution" / "scripts" / "focused_trials.py",
                       Path.home() / ".agents" / "skills" / "skill-evolution" / "scripts" / "focused_trials.py"))
    return next((path.resolve() for path in candidates if path.is_file()), None)


TEST_CONTROLLER = locate_test_controller()
module_spec = importlib.util.spec_from_file_location("testy_ab_runner", SCRIPT)
ab = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(ab)


def save(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def anonymous_packet():
    return {
        "schema_version": 1,
        "trial_id": "offline-trial",
        "target_skill": "synthetic-skill",
        "condition_sha256": "a" * 64,
        "blind": True,
        "cases": [{
            "case_id": "case-0",
            "case_type": "persistence",
            "prompt": "Do the operation",
            "expected_behavior": "Preserve the requested behavior",
            "variant_a_answer": "Answer A",
            "variant_b_answer": "Answer B",
            "artifact_sha256": {"variant_a": "b" * 64, "variant_b": "c" * 64},
        }],
        "outcome_trial": {},
    }


@pytest.fixture
def lab(monkeypatch):
    # Retain all test state outside the repository; pytest tmp_path deletes it.
    root = Path(os.environ.get("AGENT_TEST_ROOT") or
                Path.home() / ".agents" / "test-runs" / "skill-ab-testing").resolve()
    if root.is_relative_to(REPO):
        raise ValueError("AGENT_TEST_ROOT must be outside the repository")
    root.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        # Python 3.13's mkdir(0700) discards inherited Windows ACLs, including
        # the workspace sandbox SID. Inherit the external root's normal ACL.
        mkdir = os.mkdir
        with patch("os.mkdir", side_effect=lambda path, mode: mkdir(path, 0o777)):
            directory = Path(tempfile.mkdtemp(prefix=".ab-test-", dir=root))
    else:
        directory = Path(tempfile.mkdtemp(prefix=".ab-test-", dir=root))
    monkeypatch.setenv("TESTY_AB_HOME", str(directory / "state"))
    if TEST_CONTROLLER is not None:
        monkeypatch.setenv("TESTY_AB_FOCUSED_TRIALS", str(TEST_CONTROLLER))
    else:
        monkeypatch.delenv("TESTY_AB_FOCUSED_TRIALS", raising=False)
    monkeypatch.setenv("TESTY_AB_TODO_FLEET", str(directory / "missing-bridge.py"))
    monkeypatch.delenv("AGENT_MODEL_CATALOG", raising=False)
    for variant in ("baseline", "better", "worse"):
        (directory / (variant + ".txt")).write_text("Rule " + variant, encoding="utf-8")
    value = {
        "trial_id": "offline-trial", "target_skill": "synthetic-skill",
        "baseline": {"variant_id": "baseline", "skill_text_file": "baseline.txt"},
        "alternatives": [{"variant_id": "better", "skill_text_file": "better.txt",
                           "changed_components": ["step-3"]}],
        "hypothesis": {"cue": "Repeated handoff failures", "lesson": "Clarify the recovery step."},
        "cases": [{"case_id": f"case-{i}", "case_type": sorted(ab.CASE_TYPES)[i % 4],
                   "kind": "targeted" if i < 5 else "preservation", "prompt": f"Do operation {i}",
                   "expected_behavior": f"Preserve observable behavior {i}"} for i in range(8)],
        "subject": {"agent": "synthetic-subject", "model": "cheap", "tier": "W", "provider_lineage": "qwen"},
        "confirm_model": {"agent": "synthetic-confirm", "model": "strong", "tier": "M", "provider_lineage": "openai"},
        "evaluator": {"agent": "synthetic-evaluator", "model": "judge", "tier": "K", "provider_lineage": "xai"},
        "proposer": {"actor_id": "synthetic-author", "provider_lineage": "anthropic"},
    }
    # Every subprocess must be the real local controller or an explicitly installed
    # test-owned fake bridge. This prevents accidental Fleet/model calls.
    actual_hidden = ab.hidden
    calls = []

    def local_only(argv, allowed=(0,)):
        permitted = {str(Path(os.environ["TESTY_AB_TODO_FLEET"]).resolve())}
        if TEST_CONTROLLER is not None:
            permitted.add(str(TEST_CONTROLLER))
        assert argv[0] == sys.executable and argv[1] == "-B"
        assert argv[2] in permitted
        assert Path(argv[2]).is_file()
        calls.append(argv)
        return actual_hidden(argv, allowed)

    monkeypatch.setattr(ab, "hidden", local_only)
    actual_focused = ab.focused

    def focused_if_available(*args, **kwargs):
        if TEST_CONTROLLER is None and not os.environ.get("TESTY_AB_FOCUSED_TRIALS"):
            pytest.skip("focused_trials controller unavailable (env, sibling and deployed paths checked)")
        return actual_focused(*args, **kwargs)

    monkeypatch.setattr(ab, "focused", focused_if_available)
    return directory, value, calls


def command(capsys, *argv, code=0):
    result = ab.main(list(argv))
    captured = capsys.readouterr()
    assert captured.err == ""
    output = json.loads(captured.out)
    assert result == code, output
    return output


def make_plan(lab, capsys, value=None, name="run"):
    directory, default, _ = lab
    source = save(directory / "spec.json", value or default)
    output = command(capsys, "plan", "--spec", str(source), "--out", name)
    return Path(os.environ["TESTY_AB_HOME"]) / name, output


def fake_files(lab, value=None, *, telemetry=True, critical=False):
    directory, default, _ = lab
    value = value or default
    vids = [value["baseline"]["variant_id"], *[a["variant_id"] for a in value["alternatives"]]]
    answers_by_variant = {variant: f"response-{index + 1}" for index, variant in enumerate(vids)}
    answers, confirmation = {}, {}
    for c in value["cases"]:
        answers[c["case_id"]] = {
            v: {"answer": answers_by_variant[v], "tokens": {"baseline": 20, "better": 10, "worse": 15}.get(v, 12) if telemetry else None,
                "seconds": 1.5 if telemetry else None, "tool_calls": None,
                "secret": "untrusted-extra", "command": "UNTRUSTED_DO_NOT_EXECUTE"} for v in vids}
        confirmation[c["case_id"]] = {v: {"answer": v + " preserved", "passed": True, "notes": "offline independent fixture"} for v in vids}
    answer_path = save(directory / "fake-answers.json", {"answers": answers, "confirmation": confirmation})
    scores = {answers_by_variant[v]: {"score": {"baseline": 5, "better": 8, "worse": 2}.get(v, 8),
                                      "critical_failure": critical and v == "better"} for v in vids}
    eval_path = save(directory / "fake-evaluation.json", {"answer_scores": scores, "command": "UNTRUSTED_DO_NOT_EXECUTE"})
    return "fake:" + str(answer_path), "fake:" + str(eval_path)


def finish(lab, capsys, value=None, **fake_options):
    run_dir, planned = make_plan(lab, capsys, value)
    executor, evaluator = fake_files(lab, value, **fake_options)
    command(capsys, "run", "--run-dir", str(run_dir), "--executor", executor)
    verdict = command(capsys, "judge", "--run-dir", str(run_dir), "--evaluator", evaluator)
    if verdict["status"] == "NEEDS_REPEATS":
        command(capsys, "run", "--run-dir", str(run_dir), "--executor", executor)
        verdict = command(capsys, "judge", "--run-dir", str(run_dir), "--evaluator", evaluator)
    assert verdict["status"] == "JUDGED"
    return run_dir, verdict, executor, evaluator


def source_snapshot():
    return {str(path.relative_to(HERE.parent)): path.read_bytes() if path.is_file() else None
            for path in HERE.parent.rglob("*") if "__pycache__" not in path.parts}


def test_command_path_leaves_skill_source_tree_unchanged(lab, capsys):
    before = source_snapshot()
    assert not lab[0].is_relative_to(REPO)
    change = save(lab[0] / "change.json", {"target_skill": "synthetic-skill",
                  "change_kind": "prune", "alternatives": ["better"]})
    assert command(capsys, "gate", "--change", str(change))["decision"] == "RUN"
    run_dir, _, executor, _ = finish(lab, capsys)
    result = command(capsys, "report", "--run-dir", str(run_dir), "--confirm", executor)
    assert result["winner"] == "better" and result["confirmation"]["status"] == "PASS"
    assert source_snapshot() == before


@pytest.mark.parametrize("kind,alternatives,evidence,expected", [
    ("text", ["one", "two"], ["a", "b"], "SKIP"),
    ("path", ["one", "two"], ["a", "b"], "SKIP"),
    ("behavior", [], ["a", "b"], "SKIP"),
    ("behavior", ["one"], ["a"], "SKIP"),
    ("behavior", ["one", "two"], [], "RUN"),
    ("trigger", ["one"], ["a", "b"], "RUN"),
    ("flow", ["one", "two"], [], "RUN"),
    ("routing", ["one"], ["a", "b"], "RUN"),
    ("prune", ["without-rule"], [], "RUN"),
    ("prune", [], ["a", "b"], "SKIP"),
    ("behavior", ["one", "one"], ["a", "a"], "SKIP"),
])
def test_gate_matrix(lab, capsys, kind, alternatives, evidence, expected):
    path = save(lab[0] / "change.json", {"target_skill": "ordinary-skill", "change_kind": kind,
                                       "alternatives": alternatives, "evidence_refs": evidence})
    output = command(capsys, "gate", "--change", str(path))
    assert output["decision"] == expected
    assert "\n" not in output["reason"]
    assert lab[2] == []


@pytest.mark.parametrize("target", [*sorted(ab.META), "SKILL-AB-TESTING"])
def test_self_meta_cannot_gate_or_plan(lab, capsys, target):
    directory, value, _ = lab
    path = save(directory / "change.json", {"target_skill": target, "change_kind": "prune", "alternatives": ["pruned"]})
    assert command(capsys, "gate", "--change", str(path))["decision"] == "SKIP"
    value["target_skill"] = target
    output = command(capsys, "plan", "--spec", str(save(directory / "spec.json", value)), "--out", "run", code=2)
    assert output["code"] == "META_SKILL"


@pytest.mark.parametrize("change", ["seven", "nine", "six-two", "four-four"])
def test_needs_exactly_five_plus_three_cases(lab, capsys, change):
    directory, value, _ = lab
    if change == "seven":
        value["cases"].pop()
    elif change == "nine":
        value["cases"].append({**value["cases"][0], "case_id": "extra"})
    elif change == "six-two":
        value["cases"][5]["kind"] = "targeted"
    else:
        value["cases"][4]["kind"] = "preservation"
    output = command(capsys, "plan", "--spec", str(save(directory / "spec.json", value)), "--out", "run", code=2)
    assert output["code"] == "NEEDS_CASES"


@pytest.mark.parametrize("tier", ["M", "D", "DENY"])
def test_expensive_and_denied_subject_refused(lab, capsys, tier):
    directory, value, _ = lab
    value["subject"]["tier"] = tier
    result = command(capsys, "plan", "--spec", str(save(directory / "spec.json", value)), "--out", "run", code=2)
    assert result["code"] in {"EXPENSIVE_SUBJECT", "MODEL_POLICY"}


@pytest.mark.parametrize("provider", ["anthropic", "qwen", "alibaba-qwen"])
def test_evaluator_independence(lab, capsys, provider):
    directory, value, _ = lab
    value["evaluator"]["provider_lineage"] = provider
    output = command(capsys, "plan", "--spec", str(save(directory / "spec.json", value)), "--out", "run", code=2)
    assert output["code"] == "SELF_REVIEW"


@pytest.mark.parametrize("maximum", [31, 0, True, 15])
def test_budget_refusal(lab, capsys, maximum):
    directory, value, _ = lab
    value["max_runs"] = maximum
    output = command(capsys, "plan", "--spec", str(save(directory / "spec.json", value)), "--out", "run", code=2)
    assert output["code"] == "BUDGET"


def test_catalog_tiers_lineages_and_condition_hash(lab, capsys):
    _, value, _ = lab
    value["subject"] = {"agent": "codex", "model": "gpt-6-luna"}
    value["confirm_model"] = {"agent": "codex", "model": "gpt-6-astra"}
    value["evaluator"] = {"agent": "grok", "model": "grok-4.7", "provider_lineage": "xai"}
    run_dir, output = make_plan(lab, capsys)
    assert output["spec"]["subject"]["tier"] == "W"
    case = output["spec"]["cases"][0]
    expected = {k: case[k] for k in ("prompt", "expected_behavior", "subject")}
    assert case["condition_sha256"] == hashlib.sha256(ab.canonical(expected)).hexdigest()
    assert ab.read_json(run_dir / "plan.json") == output


def test_known_m_cannot_be_declared_worker(lab, capsys):
    directory, value, _ = lab
    value["subject"] = {"agent": "qwen", "model": "qwen3.8-max", "tier": "W", "provider_lineage": "qwen"}
    result = command(capsys, "plan", "--spec", str(save(directory / "spec.json", value)), "--out", "run", code=2)
    assert result["code"] == "MODEL_POLICY"


def test_prune_can_be_empty_and_text_is_frozen(lab, capsys):
    directory, value, _ = lab
    (directory / "better.txt").write_text("", encoding="utf-8")
    run_dir, output = make_plan(lab, capsys)
    assert output["spec"]["variants"][1]["skill_text"] == ""
    (directory / "baseline.txt").write_text("changed after plan", encoding="utf-8")
    assert ab.load_plan(run_dir)["spec"]["variants"][0]["skill_text"] == "Rule baseline"


def test_state_boundary_and_plan_overwrite_refused(lab, capsys):
    directory, value, _ = lab
    run_dir, _ = make_plan(lab, capsys)
    outside = command(capsys, "plan", "--spec", str(directory / "spec.json"), "--out", str(directory / "outside"), code=2)
    assert outside["code"] == "STATE_OUTSIDE_HOME"
    value["cases"][0]["prompt"] = "Different condition"
    save(directory / "spec.json", value)
    assert command(capsys, "plan", "--spec", str(directory / "spec.json"), "--out", str(run_dir), code=2)["code"] == "PLAN_EXISTS"


def test_run_resumes_partial_answers_and_ignores_untrusted_fields(lab, capsys):
    run_dir, _ = make_plan(lab, capsys)
    executor, _ = fake_files(lab, telemetry=False)
    planned = ab.load_plan(run_dir)
    state = ab.load_answers(run_dir, planned)
    # Emulate a process interruption after five completed calls and one spent
    # attempt, then resume the frozen schedule without redoing completed work.
    for case in planned["spec"]["cases"][:2]:
        for variant in planned["spec"]["variants"]:
            ab.observe(run_dir, planned, state, executor, ab.selector(executor), case, variant, 1)
    case = planned["spec"]["cases"][2]
    ab.observe(run_dir, planned, state, executor, ab.selector(executor), case, planned["spec"]["variants"][0], 1)
    state["records"].append({"phase": "trial", "round": 1, "case_id": "case-2",
                             "variant_id": "better", "status": "started",
                             "condition_sha256": case["condition_sha256"]})
    state["runs_used"] += 1
    ab.write_json(run_dir / "answers.json", state)
    first = ab.read_json(run_dir / "answers.json")
    assert first["runs_used"] == 6
    output = command(capsys, "run", "--run-dir", str(run_dir), "--executor", executor)
    assert output["runs_used"] == 17  # five complete, one failed, eleven resumed
    records = ab.read_json(run_dir / "answers.json")["records"]
    assert sum(r["status"] == "completed" for r in records) == 16
    assert all(r.get("tokens") is None for r in records)
    assert all("secret" not in r and "command" not in r for r in records)
    before = (run_dir / "answers.json").read_bytes()
    assert command(capsys, "run", "--run-dir", str(run_dir), "--executor", executor)["runs_used"] == 17
    assert (run_dir / "answers.json").read_bytes() == before


def test_real_controller_accepts_better_and_rejects_worse(lab, capsys):
    _, value, calls = lab
    value["alternatives"].append({"variant_id": "worse", "skill_text_file": "worse.txt"})
    run_dir, verdict, executor, _ = finish(lab, capsys)
    assert verdict["alternatives"]["better"]["decision"] == "SHADOW_ACCEPTED"
    assert verdict["alternatives"]["worse"]["decision"] == "REJECTED"
    assert any("create-packet" in c for c in calls)
    assert any("resolve" in c for c in calls)
    assert all(c[2] == str(TEST_CONTROLLER) for c in calls)
    for result in verdict["alternatives"].values():
        resolved = ab.read_json(result["result_file"])
        assert resolved["release_authority"] is False
        packet = ab.read_json(Path(result["result_file"]).parent / "input" / "packet.json")
        assert packet["blind"] is True
        assert "proposer" not in json.dumps(packet)
    output = command(capsys, "report", "--run-dir", str(run_dir), "--confirm", executor)
    assert output["winner"] == "better"
    assert output["runs_used"] == 30
    assert output["confirmation"]["status"] == "PASS"
    assert len(output["confirmation"]["cases"]) == 6
    assert output["release_authority"] is False
    assert output["variants"]["better"]["mean_tokens"] == 10
    assert output["variants"]["better"]["mean_seconds"] == 1.5
    assert output["edits"][0]["variant"] == "better"
    assert output["edits"][0]["components"] == ["step-3"]
    assert output["edits"][0]["verdict"] == "helped"
    assert output["lesson"]["component"] == "step-3"
    assert output["lesson"]["confidence"] == "medium"
    assert "confidence: medium" in (run_dir / "report.md").read_text(encoding="utf-8")
    assert "origin -> sync -> backup" in (run_dir / "report.md").read_text(encoding="utf-8")
    again = command(capsys, "report", "--run-dir", str(run_dir), "--confirm", executor)
    assert again["runs_used"] == 30


def test_edit_verdict_precedence_and_single_lesson():
    spec = {
        "variants": [
            {"variant_id": "baseline"},
            {"variant_id": "helped", "changed_components": ["trigger"]},
            {"variant_id": "missing"},
            {"variant_id": "multi", "changed_components": ["trigger", "step-2"]},
            {"variant_id": "hurt", "changed_components": ["stop"]},
            {"variant_id": "neutral", "changed_components": ["format"]},
        ],
        "cases": [{"case_id": "case-1"}, {"case_id": "case-2"}],
        "hypothesis": {"cue": "Repeated misses", "lesson": "Keep the recovery cue."},
    }
    alternatives = {
        "helped": {"decision": "SHADOW_ACCEPTED", "score_delta": 2, "veto": False, "regressions": []},
        "missing": {"decision": "REJECTED", "score_delta": 0, "veto": False, "regressions": []},
        "multi": {"decision": "REJECTED", "score_delta": 0, "veto": False, "regressions": []},
        "hurt": {"decision": "REJECTED", "score_delta": -1, "veto": True, "regressions": ["case-2"]},
        "neutral": {"decision": "REJECTED", "score_delta": 0, "veto": False, "regressions": []},
    }
    records = [{"phase": "trial", "status": "completed", "case_id": case["case_id"],
                "variant_id": variant, "round": round_no}
               for case in spec["cases"] for variant in [v["variant_id"] for v in spec["variants"]]
               for round_no in (1,)]
    judgments = {"status": "JUDGED", "alternatives": alternatives, "unavailable_cases": []}
    edits, lesson = ab.edit_registry(spec, judgments, {"records": records}, "helped")
    assert {edit["variant"]: edit["verdict"] for edit in edits} == {
        "helped": "helped", "missing": "confounded", "multi": "confounded",
        "hurt": "hurt", "neutral": "no_effect"}
    assert lesson == {"cue": "Repeated misses", "lesson": "Keep the recovery cue.",
                      "component": "trigger", "evidence": ["edit-helped"], "confidence": "medium"}

    judgments["status"] = "NEEDS_EVIDENCE"
    edits, lesson = ab.edit_registry(spec, judgments, {"records": records}, "helped")
    assert all(edit["verdict"] == "confounded" for edit in edits)
    assert lesson == "NO_ENTRY"

    judgments.update(status="JUDGED", unavailable_cases=[{"case_id": case["case_id"]}
                                                          for case in spec["cases"]])
    edits, lesson = ab.edit_registry(spec, judgments, {"records": records}, "helped")
    assert all(edit["verdict"] == "confounded" for edit in edits)
    assert lesson == "NO_ENTRY"


@pytest.mark.parametrize("leak", ["top-key", "case-key", "identity", "path", "filename", "artifact-key"])
def test_leak_guard_blocks_evaluator_before_any_call(lab, capsys, monkeypatch, leak):
    directory, _, _ = lab
    run_dir, _ = make_plan(lab, capsys)
    executor, _ = fake_files(lab)
    command(capsys, "run", "--run-dir", str(run_dir), "--executor", executor)
    planned = ab.load_plan(run_dir)
    variant_path = planned["spec"]["variants"][1]["skill_text_file"]
    evaluator_calls = []

    def emit_leaking_packet(argv, outputs, allowed=(0,)):
        packet_tmp, sealed_tmp = (Path(item[0]) for item in outputs)
        packet = anonymous_packet()
        if leak == "top-key":
            packet["variant_mapping"] = {"variant_a": "baseline"}
        elif leak == "case-key":
            packet["cases"][0]["winner"] = "better"
        elif leak == "identity":
            packet["trial_id"] = "better"
        elif leak == "path":
            packet["cases"][0]["variant_a_answer"] = variant_path
        elif leak == "filename":
            packet["cases"][0]["variant_b_answer"] = Path(variant_path).name
        else:
            packet["cases"][0]["artifact_sha256"]["baseline"] = "d" * 64
        ab.write_json(packet_tmp, packet)
        ab.write_json(sealed_tmp, {"private": True})
        for temporary, destination in outputs:
            os.replace(temporary, destination)

    monkeypatch.setattr(ab, "focused", emit_leaking_packet)
    monkeypatch.setattr(ab, "fleet_call", lambda *args, **kwargs: evaluator_calls.append(args))
    result = command(capsys, "judge", "--run-dir", str(run_dir), "--evaluator", "fleet", code=2)
    assert result["code"] == "LEAK_GUARD"
    assert evaluator_calls == []
    assert not (run_dir / "evaluator-calls.json").exists()


def test_leak_guard_allows_variant_words_generic_paths_and_verdict_words():
    packet = anonymous_packet()
    packet["cases"][0]["variant_a_answer"] = "The baseline says scripts/run.py helped; the result was rejected."
    variants = [
        {"variant_id": "baseline", "skill_text_file": "C:/skills/baseline.txt"},
        {"variant_id": "better", "skill_text_file": "C:/skills/better.txt"},
    ]
    assert ab.guard_packet(packet, variants) is None


def test_leak_guard_checks_variant_identity_only_as_metadata():
    packet = anonymous_packet()
    packet["cases"][0]["expected_behavior"] = "The better answer should pass."
    variants = [
        {"variant_id": "baseline", "skill_text_file": "C:/skills/baseline.txt"},
        {"variant_id": "better", "skill_text_file": "C:/skills/better.txt"},
    ]
    assert ab.guard_packet(packet, variants) is None
    packet["cases"][0]["case_id"] = "better"
    with pytest.raises(ab.ABError) as error:
        ab.guard_packet(packet, variants)
    assert error.value.code == "LEAK_GUARD"


def test_repeats_only_discordant_cases_and_budget_trim(lab, capsys):
    run_dir, planned = make_plan(lab, capsys)
    assert planned["budget"]["pass_1"] == 16
    assert planned["budget"]["pass_2_cap"] == 8
    executor, evaluator = fake_files(lab)
    command(capsys, "run", "--run-dir", str(run_dir), "--executor", executor)
    initial = command(capsys, "judge", "--run-dir", str(run_dir), "--evaluator", evaluator)
    assert initial["status"] == "NEEDS_REPEATS"
    assert initial["budget"]["pass_2_requested"] == 32
    assert initial["budget"]["pass_2_scheduled"] == 8
    assert initial["budget"]["trimmed"] is True
    assert command(capsys, "report", "--run-dir", str(run_dir), code=2)["code"] == "NEEDS_REPEATS"
    command(capsys, "run", "--run-dir", str(run_dir), "--executor", executor)
    final = command(capsys, "judge", "--run-dir", str(run_dir), "--evaluator", evaluator)
    assert final["status"] == "JUDGED"
    records = ab.read_json(run_dir / "answers.json")["records"]
    assert len(records) == 24
    assert {r["case_id"] for r in records if r["round"] > 1} == {"case-0", "case-1"}
    command(capsys, "report", "--run-dir", str(run_dir), "--confirm", executor)
    assert ab.read_json(run_dir / "answers.json")["runs_used"] == 30


def test_concordant_scores_need_no_repeats_and_unknown_costs_stay_null(lab, capsys):
    run_dir, _ = make_plan(lab, capsys)
    executor, evaluator = fake_files(lab, telemetry=False)
    save(Path(evaluator[5:]), {"answer_scores": {"response-1": {"score": 5}, "response-2": {"score": 5}}})
    command(capsys, "run", "--run-dir", str(run_dir), "--executor", executor)
    verdict = command(capsys, "judge", "--run-dir", str(run_dir), "--evaluator", evaluator)
    assert verdict["status"] == "JUDGED"
    assert verdict["budget"]["pass_2_scheduled"] == 0
    output = command(capsys, "report", "--run-dir", str(run_dir))
    assert output["winner"] is None and output["runs_used"] == 16
    assert all(v["mean_tokens"] is None and v["mean_seconds"] is None for v in output["variants"].values())


def test_critical_failure_veto_even_with_high_scores(lab, capsys):
    run_dir, verdict, _, _ = finish(lab, capsys, critical=True)
    assert verdict["alternatives"]["better"]["decision"] == "VETOED"
    assert verdict["alternatives"]["better"]["veto"] is True
    assert command(capsys, "report", "--run-dir", str(run_dir))["winner"] is None


def test_any_case_regression_blocks_mean_gain(lab, capsys):
    run_dir, _ = make_plan(lab, capsys)
    executor, evaluator = fake_files(lab)
    answers = ab.read_json(executor[5:])
    answers["answers"]["case-7"]["better"]["answer"] = "regression answer"
    save(Path(executor[5:]), answers)
    evaluation = ab.read_json(evaluator[5:])
    evaluation["answer_scores"]["regression answer"] = {"score": 4}
    save(Path(evaluator[5:]), evaluation)
    command(capsys, "run", "--run-dir", str(run_dir), "--executor", executor)
    command(capsys, "judge", "--run-dir", str(run_dir), "--evaluator", evaluator)
    command(capsys, "run", "--run-dir", str(run_dir), "--executor", executor)
    verdict = command(capsys, "judge", "--run-dir", str(run_dir), "--evaluator", evaluator)
    better = verdict["alternatives"]["better"]
    assert better["controller_decision"] == "SHADOW_ACCEPTED"
    assert better["decision"] == "REJECTED" and better["regressions"] == ["case-7"]


def test_wrong_evaluator_identity_and_outcome_downgrade_refused(lab, capsys):
    run_dir, _ = make_plan(lab, capsys)
    executor, evaluator = fake_files(lab)
    command(capsys, "run", "--run-dir", str(run_dir), "--executor", executor)
    evaluation = ab.read_json(evaluator[5:])
    evaluation["evaluator"] = {"actor_id": "synthetic-author", "provider_lineage": "anthropic"}
    save(Path(evaluator[5:]), evaluation)
    assert command(capsys, "judge", "--run-dir", str(run_dir), "--evaluator", evaluator, code=2)["code"] == "SELF_REVIEW"
    evaluation.pop("evaluator")
    evaluation["answer_scores"]["response-2"]["outcome"] = {"action": {"value": False}}
    save(Path(evaluator[5:]), evaluation)
    assert command(capsys, "judge", "--run-dir", str(run_dir), "--evaluator", evaluator, code=2)["code"] == "OUTCOME_UNSUPPORTED"


def test_low_budget_and_missing_confirmation_evidence(lab, capsys):
    _, value, _ = lab
    value["max_runs"] = 16
    run_dir, _, executor, _ = finish(lab, capsys)
    output = command(capsys, "report", "--run-dir", str(run_dir), "--confirm", executor)
    assert output["runs_used"] == 16
    assert output["confirmation"]["status"] == "FAIL"
    assert "budget" in output["confirmation"]["notes"]


def test_confirmation_failure_resume_and_stale_answer_detection(lab, capsys):
    run_dir, _, executor, evaluator = finish(lab, capsys)
    answers = ab.read_json(executor[5:])
    answers["confirmation"]["case-5"]["better"].pop("passed")
    save(Path(executor[5:]), answers)
    report = command(capsys, "report", "--run-dir", str(run_dir), "--confirm", executor)
    assert report["confirmation"]["status"] == "FAIL"
    assert command(capsys, "report", "--run-dir", str(run_dir))["runs_used"] == 30
    saved = ab.read_json(run_dir / "answers.json")
    saved["records"][0]["answer"] = "New answer invalidates judgment"
    save(run_dir / "answers.json", saved)
    assert command(capsys, "report", "--run-dir", str(run_dir), code=2)["code"] == "STALE_JUDGMENT"


def test_winner_tie_prefers_lower_tokens(lab, capsys):
    directory, value, _ = lab
    value["alternatives"].append({"variant_id": "worse", "skill_text_file": "worse.txt"})
    run_dir, _ = make_plan(lab, capsys)
    executor, evaluator = fake_files(lab)
    data = ab.read_json(evaluator[5:])
    data["answer_scores"]["response-3"]["score"] = 8
    save(Path(evaluator[5:]), data)
    command(capsys, "run", "--run-dir", str(run_dir), "--executor", executor)
    command(capsys, "judge", "--run-dir", str(run_dir), "--evaluator", evaluator)
    report = command(capsys, "report", "--run-dir", str(run_dir))
    assert report["alternatives"]["better"]["score_delta"] == report["alternatives"]["worse"]["score_delta"]
    assert report["winner"] == "better"


def install_fake_bridge(lab, monkeypatch, *, failing=(), fail_once=()):
    path = lab[0] / "bridge.py"
    path.write_text('''import argparse, json
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument("--request")
p.add_argument("--output")
a = p.parse_args()
req = json.loads(Path(a.request).read_text(encoding="utf-8"))
task = req["task"]
def require(condition, message):
    if not condition:
        raise SystemExit(message)
require(req.get("role") == "maker", "role must be maker")
require(task.get("mode") == "advisory", "mode must be advisory")
workspace = Path(req["workspace"]).resolve()
for field in ("scope", "inputs"):
    names = task.get(field)
    require(isinstance(names, list) and bool(names), field + " must be a nonempty list")
    for name in names:
        require(isinstance(name, str) and bool(name.strip()), field + " must name files")
        relative = Path(name)
        require(not relative.is_absolute() and ".." not in relative.parts, "input must be relative")
        path = (workspace / relative).resolve()
        require(path.is_relative_to(workspace) and path.is_file(), "input must exist inside workspace")
require("reviewer" in task and isinstance(task["reviewer"], list), "reviewer must be present as a list")
maker = task.get("maker")
require(isinstance(maker, list) and len(maker) == 1, "maker must pin one candidate")
profile = maker[0]
require(isinstance(profile, dict) and all(isinstance(profile.get(k), str) and profile[k].strip()
        for k in ("agent", "model", "reason")) and type(profile.get("score")) in (int, float),
        "maker candidate must pin agent and model")
packet = workspace / "packet.json"
if packet.exists():
    data = json.loads(packet.read_text(encoding="utf-8"))
    cases = []
    for c in data["cases"]:
        cases.append({"case_id": c["case_id"], **{v: {"score": 8 if "response-2" in c[v+"_answer"] else 5, "critical_failure": False} for v in ("variant_a", "variant_b")}})
    summary = json.dumps({"trial_id": data["trial_id"], "case_evaluations": cases})
    provider = "xai"
else:
    observation = (workspace / "observation.md").read_text(encoding="utf-8")
    require(observation == task["text"], "observation must match the supplied task")
    summary = "response-2" if "Rule better" in observation else "response-1"
    provider = "alibaba-qwen"
    if profile["agent"] == "synthetic-subject":
        case_id = "case-" + observation.split("User message:\\nDo operation ", 1)[1].splitlines()[0]
        pair = [case_id, "better" if "Rule better" in observation else "baseline"]
        failing = FAILING_PAIRS
        fail_once = FAIL_ONCE_PAIRS
        marker = workspace.parents[1] / ("failed-once-" + "-".join(pair) + ".json")
        if pair in failing or (pair in fail_once and not marker.exists()):
            marker.write_text("{}", encoding="utf-8")
            raise SystemExit("MALFORMED JSON: handoff candidate does not parse")
    if profile["agent"] == "synthetic-confirm":
        summary = json.dumps({"answer": summary, "passed": True, "notes": "synthetic confirmation"})
        provider = "openai"
Path(a.output).write_text(json.dumps({"status": "CANDIDATE", "agent": profile["agent"], "model": profile["model"], "lineage": provider, "tokens": None, "model_tool_calls": None, "handoff": {"status": "completed", "summary": summary, "files_changed": [], "verification": [], "risks": [], "next_steps": []}}), encoding="utf-8")
''', encoding="utf-8")
    source = path.read_text(encoding="utf-8")
    path.write_text(source.replace("FAILING_PAIRS", repr([list(p) for p in failing]))
                    .replace("FAIL_ONCE_PAIRS", repr([list(p) for p in fail_once])), encoding="utf-8")
    monkeypatch.setenv("TESTY_AB_TODO_FLEET", str(path))
    return path


def assert_complete_handoff_instruction(instruction, summary_example, verification_item):
    assert "Return a complete handoff object" in instruction
    assert "all six fields are required" in instruction
    for field in ("files_changed", "risks", "next_steps"):
        assert field + " []" in instruction
    assert "exactly one concrete item" in instruction
    assert "verification " + json.dumps([verification_item]) in instruction
    example = json.loads(instruction.rsplit("Minimal complete handoff example: ", 1)[1])
    assert len(example["verification"]) == 1 and example["verification"][0].strip()
    assert example == {"status": "completed", "summary": summary_example,
                       "files_changed": [], "verification": [verification_item], "risks": [], "next_steps": []}
    assert '"' not in example["summary"]


def test_fleet_advisory_transport_with_local_fake_and_packet_only(lab, capsys, monkeypatch):
    bridge = install_fake_bridge(lab, monkeypatch)
    run_dir, _ = make_plan(lab, capsys)
    command(capsys, "run", "--run-dir", str(run_dir), "--executor", "fleet")
    command(capsys, "judge", "--run-dir", str(run_dir), "--evaluator", "fleet")
    command(capsys, "run", "--run-dir", str(run_dir), "--executor", "fleet")
    command(capsys, "judge", "--run-dir", str(run_dir), "--evaluator", "fleet")
    report = command(capsys, "report", "--run-dir", str(run_dir), "--confirm", "fleet")
    assert report["winner"] == "better"
    assert report["confirmation"]["status"] == "PASS"
    assert report["runs_used"] == 30
    assert report["evaluator_calls"] == 2
    assert report["variants"]["better"]["mean_tokens"] is None
    assert any(c[2] == str(bridge) for c in lab[2])
    for request in run_dir.glob("judgments/*/observer/request.json"):
        data = ab.read_json(request)
        assert data["role"] == "maker" and data["task"]["mode"] == "advisory"
        assert data["task"]["inputs"] == ["packet.json"] == data["task"]["scope"]
        assert data["task"]["reviewer"] == []
        assert "ONLY the evaluation JSON in handoff.summary" in data["task"]["text"]
        assert "serialized as a JSON string with escaped inner double quotes" in data["task"]["text"]
        assert_complete_handoff_instruction(data["task"]["text"], "Evaluation JSON as an escaped string",
                                            "Ocena oparta wylacznie na anonimowym pakiecie")
        inputs = list(Path(data["workspace"]).iterdir())
        assert [p.name for p in inputs] == ["packet.json"]
        assert "baseline.txt" not in json.dumps(data) and "better.txt" not in json.dumps(data)
        assert "sealed.json" not in json.dumps(data)
    for path in run_dir.glob("calls/*/request.json"):
        data = ab.read_json(path)
        assert data["task"]["mode"] == "advisory"
        assert data["task"]["reviewer"] == []
        assert data["task"]["scope"] == ["observation.md"] == data["task"]["inputs"]
        workspace = Path(data["workspace"])
        assert [p.name for p in workspace.iterdir()] == ["observation.md"]


def test_subject_observation_contains_skill_prompt_and_exact_reply_instruction(lab, capsys, monkeypatch):
    install_fake_bridge(lab, monkeypatch)
    run_dir, _ = make_plan(lab, capsys)
    command(capsys, "run", "--run-dir", str(run_dir), "--executor", "fleet")
    planned = ab.load_plan(run_dir)["spec"]
    records = ab.read_json(run_dir / "answers.json")["records"]
    for number, record in enumerate(records, 1):
        request = ab.read_json(run_dir / "calls" / str(number) / "request.json")
        observation = (Path(request["workspace"]) / "observation.md").read_text(encoding="utf-8")
        variant = next(v for v in planned["variants"] if v["variant_id"] == record["variant_id"])
        case = next(c for c in planned["cases"] if c["case_id"] == record["case_id"])
        assert variant["skill_text"] in observation
        assert case["prompt"] in observation
        assert "Act as the assistant governed by the skill text" in observation
        assert "answer the user message" in observation
        assert "ONLY the exact user-facing reply in handoff.summary" in observation
        assert "Inside handoff.summary never use the double-quote character" in observation
        assert "use single quotes or no quotes" in observation
        assert "Keep the reply otherwise verbatim" in observation
        assert "summary equal to that reply" in observation
        assert_complete_handoff_instruction(observation, "Exact user-facing reply",
                                            "Odpowiedz oparta wylacznie na tekscie skilla i wiadomosci uzytkownika")
        assert "no meta commentary" in observation and "no file reads needed" in observation
        assert observation == request["task"]["text"]
        assert record["answer"] == ("response-1" if record["variant_id"] == "baseline" else "response-2")


@pytest.mark.parametrize("field,value", [
    ("role", "reviewer"), ("mode", "implement"),
    ("scope", []), ("scope", "observation.md"), ("scope", [""]), ("scope", [1]),
    ("scope", ["missing.md"]), ("scope", ["../baseline.txt"]),
    ("inputs", []), ("inputs", "observation.md"), ("inputs", [""]), ("inputs", [1]),
    ("inputs", ["missing.md"]), ("inputs", ["../baseline.txt"]),
    ("reviewer", None), ("reviewer", {}), ("maker", []), ("maker", [{}, {}]),
    ("maker", [{"agent": "synthetic-subject", "score": 100, "reason": "unpinned"}]),
    ("inputs", "absolute"),
])
def test_fake_bridge_rejects_invalid_advisory_request(lab, monkeypatch, field, value):
    bridge = install_fake_bridge(lab, monkeypatch)
    workspace = lab[0] / "bridge-workspace"
    workspace.mkdir()
    (workspace / "observation.md").write_text("Rule better", encoding="utf-8")
    request = {"goal": "Synthetic advisory", "role": "maker", "workspace": str(workspace),
               "task": {"text": "Rule better", "mode": "advisory", "scope": ["observation.md"],
                        "inputs": ["observation.md"], "reviewer": [],
                        "maker": [{"agent": "synthetic-subject", "model": "cheap", "score": 100,
                                   "reason": "Pinned synthetic observer"}]}}
    if field == "role":
        request[field] = value
    elif field == "reviewer" and value is None:
        request["task"].pop(field)
    else:
        request["task"][field] = [str(workspace / "observation.md")] if value == "absolute" else value
    path = save(lab[0] / "invalid-request.json", request)
    result = ab.hidden([sys.executable, "-B", str(bridge), "--request", str(path),
                        "--output", str(lab[0] / "invalid-output.json")], allowed=(0, 1))
    assert result.returncode == 1 and result.stderr.strip()
    assert not (lab[0] / "invalid-output.json").exists()


def test_only_discordant_preservation_case_is_repeated(lab, capsys):
    run_dir, _ = make_plan(lab, capsys)
    executor, evaluator = fake_files(lab)
    answers = ab.read_json(executor[5:])
    for i in range(7):
        answers["answers"][f"case-{i}"]["better"]["answer"] = "same-score answer"
    save(Path(executor[5:]), answers)
    scores = ab.read_json(evaluator[5:])
    scores["answer_scores"]["same-score answer"] = {"score": 5}
    save(Path(evaluator[5:]), scores)
    command(capsys, "run", "--run-dir", str(run_dir), "--executor", executor)
    verdict = command(capsys, "judge", "--run-dir", str(run_dir), "--evaluator", evaluator)
    assert verdict["budget"]["pass_2_scheduled"] == 4
    assert verdict["budget"]["trimmed"] is False
    assert ab.load_plan(run_dir)["repeat_case_ids"] == ["case-7"]
    command(capsys, "run", "--run-dir", str(run_dir), "--executor", executor)
    command(capsys, "judge", "--run-dir", str(run_dir), "--evaluator", evaluator)
    assert command(capsys, "report", "--run-dir", str(run_dir), "--confirm", executor)["runs_used"] == 26


def test_failed_evaluator_call_is_retained_in_usage(lab, capsys, monkeypatch):
    install_fake_bridge(lab, monkeypatch)
    run_dir, _ = make_plan(lab, capsys)
    executor, _ = fake_files(lab)
    command(capsys, "run", "--run-dir", str(run_dir), "--executor", executor)
    actual_call = ab.fleet_call
    attempts = []

    def first_attempt_fails(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise ab.ABError("SUBPROCESS_FAILED", "synthetic failed observation")
        return actual_call(*args, **kwargs)

    monkeypatch.setattr(ab, "fleet_call", first_attempt_fails)
    command(capsys, "judge", "--run-dir", str(run_dir), "--evaluator", "fleet", code=2)
    command(capsys, "judge", "--run-dir", str(run_dir), "--evaluator", "fleet")
    command(capsys, "run", "--run-dir", str(run_dir), "--executor", executor)
    command(capsys, "judge", "--run-dir", str(run_dir), "--evaluator", "fleet")
    report = command(capsys, "report", "--run-dir", str(run_dir))
    assert report["evaluator_calls"] == 3 and report["evaluator_calls_failed"] == 1
    calls = ab.read_json(run_dir / "evaluator-calls.json")
    assert calls[0]["tokens"] is None and calls[0]["seconds"] is None
    # Reusing a sealed packet's existing evaluation never spends another call.
    command(capsys, "judge", "--run-dir", str(run_dir), "--evaluator", "fleet")
    assert command(capsys, "report", "--run-dir", str(run_dir))["evaluator_calls"] == 3


@pytest.mark.parametrize("missing", [("case-0",), ("case-0", "case-1"), ("case-5",)])
def test_permanent_transport_failures_are_excluded_and_reportable(lab, capsys, monkeypatch, missing):
    install_fake_bridge(lab, monkeypatch, failing=[(cid, "baseline") for cid in missing])
    run_dir, _ = make_plan(lab, capsys)
    result = command(capsys, "run", "--run-dir", str(run_dir), "--executor", "fleet")
    state = ab.read_json(run_dir / "answers.json")
    assert result["runs_used"] == 16 + len(missing)
    assert {c["case_id"] for c in result["unavailable_cases"]} == set(missing)
    assert state["unavailable_cases"] == result["unavailable_cases"]
    for cid in missing:
        failures = [r for r in state["records"] if r["case_id"] == cid and r["variant_id"] == "baseline"]
        assert len(failures) == 2 and all(r["status"] == "failed" for r in failures)
        assert all(r["error_code"] == "SUBPROCESS_FAILED" for r in failures)
    assert ab.completed(state, "trial", 1, "case-7", "better")
    # The retry comes after all first attempts, protecting later cases' budget.
    assert all(r["case_id"] in missing for r in state["records"][16:])
    frozen = (run_dir / "answers.json").read_bytes()
    assert command(capsys, "run", "--run-dir", str(run_dir), "--executor", "fleet") == result
    assert (run_dir / "answers.json").read_bytes() == frozen
    verdict = command(capsys, "judge", "--run-dir", str(run_dir), "--evaluator", "fleet")
    if len(missing) == 1 and missing[0] == "case-0":
        assert verdict["status"] == "NEEDS_REPEATS"
        assert "case-0" not in ab.load_plan(run_dir)["repeat_case_ids"]
        command(capsys, "run", "--run-dir", str(run_dir), "--executor", "fleet")
        verdict = command(capsys, "judge", "--run-dir", str(run_dir), "--evaluator", "fleet")
        assert verdict["status"] == "JUDGED"
        assert verdict["alternatives"]["better"]["decision"] == "SHADOW_ACCEPTED"
        for trial_path in run_dir.glob("judgments/*/spec.json"):
            trial = ab.read_json(trial_path)
            assert all(c["case_id"] != "case-0" for c in trial["cases"])
            assert any(c["case_id"] == "case-7" for c in trial["cases"])
        expected_winner = "better"
    else:
        assert verdict["status"] == "NEEDS_EVIDENCE"
        assert verdict["alternatives"]["better"]["decision"] == "NEEDS_EVIDENCE"
        assert not list(run_dir.glob("judgments/*/observer/request.json"))
        expected_winner = None
    output = command(capsys, "report", "--run-dir", str(run_dir), "--confirm", "fleet")
    assert output["winner"] == expected_winner
    assert output["status"] == verdict["status"]
    assert output["unavailable_cases"] == verdict["unavailable_cases"]
    assert output["release_authority"] is False and output["runs_used"] <= 30
    assert output["variants"]["better"]["mean_tokens"] is None
    markdown = (run_dir / "report.md").read_text(encoding="utf-8")
    assert all(cid in markdown for cid in missing)
    assert f"Winner: {expected_winner or 'none'}" in markdown


def test_transient_transport_failure_is_retried_once_and_recovers(lab, capsys, monkeypatch):
    install_fake_bridge(lab, monkeypatch, fail_once=[("case-4", "baseline")])
    run_dir, _ = make_plan(lab, capsys)
    result = command(capsys, "run", "--run-dir", str(run_dir), "--executor", "fleet")
    assert result["runs_used"] == 17 and result["unavailable_cases"] == []
    records = ab.read_json(run_dir / "answers.json")["records"]
    attempts = [r for r in records if r["case_id"] == "case-4" and r["variant_id"] == "baseline"]
    assert [r["status"] for r in attempts] == ["failed", "completed"]
    assert records[-1] == attempts[-1]
    assert command(capsys, "run", "--run-dir", str(run_dir), "--executor", "fleet")["runs_used"] == 17


@pytest.mark.parametrize("maximum", [16, 17, 30])
def test_repeated_failure_cannot_spend_beyond_budget(lab, capsys, monkeypatch, maximum):
    value = lab[1]
    value["max_runs"] = maximum
    install_fake_bridge(lab, monkeypatch, failing=[(f"case-{i}", "baseline") for i in range(8)])
    run_dir, _ = make_plan(lab, capsys)
    output = command(capsys, "run", "--run-dir", str(run_dir), "--executor", "fleet")
    assert output["runs_used"] == min(maximum, 24)
    state = ab.read_json(run_dir / "answers.json")
    assert len(state["records"]) == state["runs_used"] <= maximum
    for i in range(8):
        assert ab.completed(state, "trial", 1, f"case-{i}", "better")
        assert sum(r["case_id"] == f"case-{i}" and r["variant_id"] == "baseline"
                   for r in state["records"]) <= 2
    before = (run_dir / "answers.json").read_bytes()
    assert command(capsys, "run", "--run-dir", str(run_dir), "--executor", "fleet") == output
    assert (run_dir / "answers.json").read_bytes() == before
    verdict = command(capsys, "judge", "--run-dir", str(run_dir), "--evaluator", "fleet")
    assert verdict["status"] == "NEEDS_EVIDENCE"
    report = command(capsys, "report", "--run-dir", str(run_dir), "--confirm", "fleet")
    assert report["winner"] is None and report["runs_used"] == output["runs_used"]


def test_judge_requires_all_variants_of_every_available_case(lab, capsys):
    run_dir, _ = make_plan(lab, capsys)
    _, evaluator = fake_files(lab)
    result = command(capsys, "judge", "--run-dir", str(run_dir), "--evaluator", evaluator, code=2)
    assert result["code"] == "NEEDS_RUN"


@pytest.mark.parametrize("model", ["gpt-6-sol", "openai/gpt-6-sol", "Muse Spark Contributor", "training-enabled-model"])
def test_forbidden_models_never_become_subject(lab, capsys, model):
    directory, value, _ = lab
    value["subject"]["model"] = model
    error = command(capsys, "plan", "--spec", str(save(directory / "spec.json", value)), "--out", "run", code=2)
    assert error["code"] == "MODEL_POLICY"


def test_actual_cli_end_to_end_offline(lab):
    if TEST_CONTROLLER is None:
        pytest.skip("focused_trials controller unavailable (env, sibling and deployed paths checked)")
    before = source_snapshot()
    directory, value, _ = lab
    value["alternatives"].append({"variant_id": "worse", "skill_text_file": "worse.txt"})
    save(directory / "spec.json", value)
    executor, evaluator = fake_files(lab)
    run_dir = Path(os.environ["TESTY_AB_HOME"]) / "cli"
    commands = [("plan", "--spec", str(directory / "spec.json"), "--out", str(run_dir)),
                ("run", "--run-dir", str(run_dir), "--executor", executor),
                ("judge", "--run-dir", str(run_dir), "--evaluator", evaluator),
                ("report", "--run-dir", str(run_dir), "--confirm", executor)]
    for args in commands:
        process = subprocess.run([sys.executable, "-B", str(SCRIPT), *args], capture_output=True,
                                 text=True, encoding="utf-8", shell=False, timeout=30,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
                                 env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                                      "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
        assert process.returncode == 0, process.stdout + process.stderr
        assert process.stderr == ""
        result = json.loads(process.stdout)
    assert result["winner"] == "better" and result["runs_used"] == 30
    assert result["release_authority"] is False
    assert source_snapshot() == before


def test_hidden_child_non_ascii_json_round_trips(lab, monkeypatch):
    child = lab[0] / "utf8-child.py"
    child.write_text('import json, os, sys\n'
                     'print(json.dumps({"text": "za\\u017c\\u00f3\\u0142\\u0107", '
                     '"ioencoding": os.environ["PYTHONIOENCODING"], '
                     '"utf8": os.environ["PYTHONUTF8"]}, ensure_ascii=False))\n'
                     'print("za\\u017c\\u00f3\\u0142\\u0107", file=sys.stderr)\n', encoding="utf-8")
    monkeypatch.setenv("TESTY_AB_TODO_FLEET", str(child))
    monkeypatch.setenv("PYTHONIOENCODING", "cp1250")
    monkeypatch.setenv("PYTHONUTF8", "0")
    process = ab.hidden([sys.executable, "-B", str(child)])
    assert json.loads(process.stdout) == {"text": "za\u017c\u00f3\u0142\u0107",
                                        "ioencoding": "utf-8", "utf8": "1"}
    assert process.stderr.strip() == "za\u017c\u00f3\u0142\u0107"


def test_all_subprocesses_hidden_without_shell(monkeypatch):
    observed = []

    def fake_run(argv, **kwargs):
        observed.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ab.hidden([sys.executable, "-B", "offline.py"])
    assert observed[0][1]["shell"] is False
    expected = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    assert observed[0][1]["creationflags"] == expected
    assert observed[0][1]["env"]["PYTHONDONTWRITEBYTECODE"] == "1"
    assert observed[0][1]["env"]["PYTHONIOENCODING"] == "utf-8"
    assert observed[0][1]["env"]["PYTHONUTF8"] == "1"


def test_missing_controller_and_cli_errors_are_json(lab, capsys, monkeypatch):
    run_dir, _ = make_plan(lab, capsys)
    executor, evaluator = fake_files(lab)
    command(capsys, "run", "--run-dir", str(run_dir), "--executor", executor)
    monkeypatch.setenv("TESTY_AB_FOCUSED_TRIALS", str(lab[0] / "missing.py"))
    assert command(capsys, "judge", "--run-dir", str(run_dir), "--evaluator", evaluator, code=2)["code"] == "MISSING_DEPENDENCY"
    assert command(capsys, "run", code=2)["code"] == "ARGUMENTS"


def test_no_deletion_calls_in_skill_source():
    for path in (SCRIPT, Path(__file__).resolve()):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        forbidden = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                     and isinstance(node.func, ast.Attribute) and node.func.attr in {"remove", "unlink", "rmtree"}]
        assert forbidden == [], str(path)
    source = SCRIPT.read_text(encoding="utf-8")
    assert "shell=True" not in source
    assert "os.system(" not in source
    assert len((HERE.parent / "SKILL.md").read_text(encoding="utf-8").splitlines()) <= 120
