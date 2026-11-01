import ast
import copy
import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import idea

LOCAL_RUN = idea.subprocess.run
LOCAL_POPEN = idea.subprocess.Popen


def evidence(sandbox):
    path = sandbox["root"] / "run" / "evidence.json"
    idea.atomic_json(path, {"items": [{"evidence_id": "hd-1", "source_kind": "helpdesk"},
                                    {"evidence_id": "tm-1", "source_kind": "telemetry"},
                                    {"evidence_id": "hd-2", "source_kind": "helpdesk"}]})
    return path


def candidate(title="Lepsze wznowienie", problem="Powtarzajace sie przerwanie"):
    return {"title": title, "kind": "extend", "extend_skill": "agent-loop", "problem": problem,
            "evidence_ids": ["hd-1", "tm-1"],
            "overlap": {"nearest_skills": ["agent-loop", "agent-graph"], "why_not_existing": "Brak celowanego wznowienia"},
            "expected_gain": {"metric": "mniej bledow", "measure_how": "Porownanie dziesieciu runow"},
            "anti_bureaucracy": {"new_triggers": 0, "new_reads": 1, "new_model_calls": 0,
                                 "verdict": "Wezszy trigger: tylko przerwane zadanie"},
            "council": {"consultants": [
                {"agent": "claude", "model": "claude-fable-5-1", "lineage": "anthropic", "recommendation": "adopt", "note": "Warto"},
                {"agent": "qwen", "model": "qwen3.8-max", "lineage": "qwen", "recommendation": "revise", "note": "Zawez"}],
                "recommendation": "revise"},
            "ideator": {"agent": "codex", "model": "gpt-6.1-sol", "lineage": "openai"}}


def write_candidate(sandbox, data=None):
    path = sandbox["root"] / "candidate.json"
    idea.atomic_json(path, data if data is not None else candidate())
    return path


@pytest.mark.parametrize("age,expected", [(None, True), (13, False), (14, False), (14.001, True), (15, True)])
def test_due_14_day_boundary(sandbox, age, expected):
    state = idea.load_state()
    if age is not None:
        state["last_completed_at"] = idea.iso(sandbox["now"] - dt.timedelta(days=age))
    idea.save_state(state)
    assert idea.due()["due"] is expected


@pytest.mark.parametrize("identity", ["alive", "unknown"])
def test_live_or_unknown_active_blocks(sandbox, monkeypatch, identity):
    state = idea.load_state()
    state["active"] = {"run_id": "run-1", "pid": 12, "started_at": idea.iso(sandbox["now"])}
    idea.save_state(state)
    monkeypatch.setattr(idea, "identity", lambda _: identity)
    assert idea.due()["reason"] == "active-run"


@pytest.mark.parametrize("hours,expected", [(23.99, False), (24, True), (25, True)])
def test_failed_run_backoff_does_not_reset_completion(sandbox, hours, expected):
    state = idea.load_state()
    old = idea.iso(sandbox["now"] - dt.timedelta(days=30))
    state["last_completed_at"] = old
    state["history"] = [{"status": "FAILED", "finished_at": idea.iso(sandbox["now"] - dt.timedelta(hours=hours))}]
    idea.save_state(state)
    assert idea.due()["due"] is expected
    assert idea.load_state()["last_completed_at"] == old


def test_collect_whitelists_caps_redacts_and_aggregates(sandbox, monkeypatch):
    ticket = {"id": "ticket-1", "component": "adapter", "model": "qwen", "status": "open", "count": 4,
              "last_seen": "now", "symptom": "token=secret-value " + "x" * 300,
              "prompt": "NEVER_COPY_PROMPT", "diff": "NEVER_COPY_DIFF", "secret": "NEVER_COPY_SECRET"}
    idea.atomic_json(sandbox["helpdesk"] / "tickets" / "one.json", ticket)
    idea.atomic_json(sandbox["helpdesk"] / "tickets" / "nested.json", {"id": "two", "symptom": {"prompt": "NESTED_SECRET"}})
    telemetry = sandbox["root"] / "telemetry" / "run" / "events.jsonl"
    events = [{"utc": idea.iso(sandbox["now"]), "node": "n", "round": 1, "role": "maker",
               "agent": "codex", "model": "gpt-6.1-sol", "event": "provider_result", "status": "COMPLETED",
               "input_tokens": 10, "fresh_input_tokens": 6, "output_tokens": 4}]
    events += [{"utc": idea.iso(sandbox["now"]), "node": "n", "round": 1, "code": "E1",
                "event": event, "prompt": "TELEMETRY_PROMPT", "diff": "TELEMETRY_DIFF"}
               for event in ("repair", "autopsy", "stage_error")]
    events += [{"utc": idea.iso(sandbox["now"] - dt.timedelta(days=15)), "event": "repair", "tokens": 900},
               {"utc": idea.iso(sandbox["now"] + dt.timedelta(days=1)), "event": "repair", "tokens": 900}]
    idea.atomic_text(telemetry, "\n".join(json.dumps(e) for e in events))
    report = sandbox["root"] / "report.txt"
    idea.atomic_text(report, "password=secret-word " + "z" * 5000)
    idea.atomic_json(sandbox["home"] / "config.json", {"extra_sources": [str(report), str(report.with_name("missing.txt"))]})
    catalog = sandbox["root"] / "catalog.py"
    idea.atomic_text(catalog, "# fake")
    monkeypatch.setenv("IDEA_MAKER_CATALOG", str(catalog))
    calls = []

    def inventory(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps({"skills": [{"name": "agent-loop", "description": "d" * 2000,
                                                                          "prompt": "CATALOG_PROMPT", "path": "PRIVATE_PATH"}]}).encode())

    monkeypatch.setattr(idea.subprocess, "run", inventory)
    out = sandbox["root"] / "evidence.json"
    collected = idea.collect(out)
    text = out.read_text(encoding="utf-8")
    for marker in ("NEVER_COPY", "NESTED_SECRET", "TELEMETRY_PROMPT", "TELEMETRY_DIFF", "CATALOG_PROMPT",
                   "PRIVATE_PATH", "secret-value", "secret-word"):
        assert marker not in text
    kinds = {item["source_kind"] for item in collected["items"]}
    assert kinds == {"helpdesk", "telemetry", "catalog", "extra"}
    assert all(item["untrusted"] and item["evidence_id"] for item in collected["items"])
    assert max(len(e["symptom"]) for e in collected["items"] if e["source_kind"] == "helpdesk") <= 160
    assert len(next(e["description"] for e in collected["items"] if e["source_kind"] == "catalog")) == 1000
    group = next(e for e in collected["items"] if e["source_kind"] == "telemetry")
    assert (group["repair"], group["autopsy"], group["stage_error"]) == (1, 1, 1)
    assert (group["input_tokens"], group["fresh_input_tokens"], group["output_tokens"]) == (10, 6, 4)
    assert len(next(e["text"] for e in collected["items"] if e["source_kind"] == "extra")) <= 4000
    assert calls[0][0][-1] == "--inventory"
    assert calls[0][1]["creationflags"] == idea.flags()
    assert calls[0][1]["env"]["PYTHONIOENCODING"] == "utf-8"
    assert calls[0][1]["env"]["PYTHONUTF8"] == "1"


def test_collect_missing_sources_and_sensitive_report_are_unavailable(sandbox):
    # The path is refused before opening; no actual secret file is created/read.
    idea.atomic_json(sandbox["home"] / "config.json", {"extra_sources": [str(sandbox["root"] / ".env")]})
    result = idea.collect(sandbox["root"] / "evidence.json")
    assert result["items"] == []
    assert all(s["status"] == "unavailable" for s in result["sources"].values())
    assert result["sources"]["extra"]["invalid"] == 1


def test_collect_catalog_script_emits_non_ascii_utf8(sandbox, monkeypatch):
    catalog = sandbox["root"] / "catalog.py"
    idea.atomic_text(catalog, 'import json\nprint(json.dumps({"skills": [{"name": "polish", '
                     '"description": "za\\u017c\\u00f3\\u0142\\u0107"}]}, ensure_ascii=False))\n')
    monkeypatch.setenv("IDEA_MAKER_CATALOG", str(catalog))
    monkeypatch.setenv("PYTHONIOENCODING", "cp1250")
    monkeypatch.setenv("PYTHONUTF8", "0")
    calls = []

    def local_catalog(argv, **kwargs):
        assert argv == [idea.sys.executable, "-B", str(catalog), "--inventory"]
        assert kwargs["creationflags"] == idea.flags()
        calls.append(kwargs)
        return LOCAL_POPEN(argv, **kwargs)

    monkeypatch.setattr(idea.subprocess, "run", LOCAL_RUN)
    monkeypatch.setattr(idea.subprocess, "Popen", local_catalog)
    result = idea.collect(sandbox["root"] / "evidence.json")
    assert result["sources"]["catalog"] == {"status": "available", "count": 1, "invalid": 0}
    item = next(e for e in result["items"] if e["source_kind"] == "catalog")
    assert item["description"] == "za\u017c\u00f3\u0142\u0107"
    assert calls[0]["env"]["PYTHONIOENCODING"] == "utf-8"
    assert calls[0]["env"]["PYTHONUTF8"] == "1"
    assert idea.os.environ["PYTHONIOENCODING"] == "cp1250"


def write_journal(sandbox, name, events):
    path = sandbox["root"] / "telemetry" / "run" / (name + ".jsonl")
    idea.atomic_text(path, "\n".join(json.dumps({"utc": idea.iso(sandbox["now"]), **e}) for e in events))


def telemetry_groups(sandbox):
    result = idea.collect(sandbox["root"] / "evidence.json")
    return {(e["agent"], e["model"], e["role"]): e for e in result["items"] if e["source_kind"] == "telemetry"}


def test_telemetry_provider_results_and_round_attribution(sandbox):
    write_journal(sandbox, "providers", [
        {"event": "repair", "node": "n", "round": 1, "code": "TEST_FAILURE"},
        {"event": "provider_result", "node": "n", "round": 1, "role": "maker", "agent": "codex",
         "model": "sol", "lineage": "openai", "status": "COMPLETED", "input_tokens": 20,
         "fresh_input_tokens": 12, "output_tokens": 7, "cache_read_tokens": 8, "prompt": "NO_RAW_PROMPT"},
        {"event": "provider_result", "node": "n", "round": 1, "role": "reviewer", "agent": "claude",
         "model": "fable", "lineage": "anthropic", "status": "REVIEWED", "input_tokens": 50,
         "fresh_input_tokens": 40, "output_tokens": 3, "cache_read_tokens": 10},
        {"event": "autopsy", "node": "n", "round": 1, "code": "stagnation"},
        {"event": "provider_result", "node": "n", "round": 1, "role": "autopsy", "agent": "claude",
         "model": "fable", "status": "REVIEWED", "output_tokens": 2},
        {"event": "provider_result", "node": "n", "round": 2, "role": "maker", "agent": "qwen",
         "model": "max", "status": "FAILED", "input_tokens": 5, "output_tokens": 1},
        {"event": "repair", "node": "n", "round": 2, "code": "REVIEW_FINDINGS"},
        {"event": "stage_error", "node": "n", "stage": "tests", "code": "OSError"},
        {"event": "node_terminal", "node": "n", "status": "BLOCKED"},
        {"event": "provider_result", "node": "m", "round": 1, "role": "maker", "agent": "codex",
         "model": "sol", "status": "FAILED", "input_tokens": 3, "fresh_input_tokens": 0,
         "output_tokens": 0, "cache_read_tokens": 0},
        {"event": "repair", "node": "m", "round": 1, "code": "TEST_FAILURE"},
        {"event": "stage_error", "node": "m", "round": 1, "stage": "maker", "code": "TimeoutError"},
        {"event": "node_terminal", "node": "m", "round": 1, "status": "ACCEPTED"},
        {"event": "repair", "node": "n", "round": 99, "code": "MISSING_ROUND_MAKER"},
    ])
    write_journal(sandbox, "other-file", [
        {"event": "repair", "node": "n", "round": 1, "code": "UNATTRIBUTED"},
        {"event": "stage_error", "node": "n", "stage": "maker", "code": "ValueError"},
    ])
    write_journal(sandbox, "aggregate", [
        {"event": "provider_result", "node": "n", "round": 1, "role": "maker", "agent": "codex",
         "model": "sol", "status": "COMPLETED", "input_tokens": 2, "fresh_input_tokens": 1,
         "output_tokens": 2, "cache_read_tokens": 1},
    ])
    groups = telemetry_groups(sandbox)
    maker = groups[("codex", "sol", "maker")]
    assert maker["runs"] == 3 and maker["status_counts"] == {"COMPLETED": 2, "FAILED": 1}
    assert (maker["input_tokens"], maker["fresh_input_tokens"], maker["output_tokens"],
            maker["cache_read_tokens"]) == (25, 13, 9, 9)
    assert (maker["repair"], maker["autopsy"], maker["stage_error"]) == (2, 1, 1)
    assert maker["repair_codes"] == {"TEST_FAILURE": 2}
    assert maker["autopsy_codes"] == {"stagnation": 1}
    assert maker["stage_error_codes"] == {"TimeoutError": 1}
    assert maker["node_status_counts"] == {"ACCEPTED": 1}
    reviewer = groups[("claude", "fable", "reviewer")]
    assert reviewer["runs"] == 1 and reviewer["status_counts"] == {"REVIEWED": 1}
    assert (reviewer["input_tokens"], reviewer["fresh_input_tokens"], reviewer["output_tokens"]) == (50, 40, 3)
    assert reviewer["repair"] == reviewer["autopsy"] == reviewer["stage_error"] == 0
    autopsy = groups[("claude", "fable", "autopsy")]
    assert autopsy["runs"] == 1 and autopsy["input_tokens"] is None and autopsy["output_tokens"] == 2
    qwen = groups[("qwen", "max", "maker")]
    assert qwen["repair_codes"] == {"REVIEW_FINDINGS": 1} and qwen["stage_error_codes"] == {"OSError": 1}
    assert qwen["node_status_counts"] == {"BLOCKED": 1} and qwen["fresh_input_tokens"] is None
    unknown = groups[("unknown", "unknown", "maker")]
    assert unknown["runs"] == 0 and unknown["input_tokens"] is None
    assert unknown["repair_codes"] == {"MISSING_ROUND_MAKER": 1, "UNATTRIBUTED": 1}
    assert unknown["stage_error_codes"] == {"ValueError": 1}
    assert "NO_RAW_PROMPT" not in str(groups)


def test_telemetry_window_zero_unknown_tokens_and_stage_round(sandbox):
    write_journal(sandbox, "window", [
        {"event": "provider_result", "node": "n", "round": 1, "role": "maker", "agent": "old",
         "model": "old", "status": "COMPLETED", "input_tokens": 900,
         "utc": idea.iso(sandbox["now"] - dt.timedelta(days=15))},
        {"event": "repair", "node": "n", "round": 1, "code": "IN_WINDOW"},
        {"event": "provider_result", "node": "n", "round": 2, "role": "maker", "agent": "new",
         "model": "new", "status": "COMPLETED", "input_tokens": 0, "fresh_input_tokens": None,
         "output_tokens": -1, "cache_read_tokens": True},
        {"event": "stage_start", "node": "n", "round": 3, "stage": "maker"},
        {"event": "stage_error", "node": "n", "stage": "maker", "code": "NO_MAKER"},
        {"event": "provider_result", "node": "n", "round": 4, "role": "maker", "agent": "future",
         "model": "future", "status": "COMPLETED", "input_tokens": 900,
         "utc": idea.iso(sandbox["now"] + dt.timedelta(days=1))},
    ])
    groups = telemetry_groups(sandbox)
    assert ("future", "future", "maker") not in groups
    old = groups[("old", "old", "maker")]
    assert old["runs"] == 0 and old["input_tokens"] is None and old["repair_codes"] == {"IN_WINDOW": 1}
    new = groups[("new", "new", "maker")]
    assert new["input_tokens"] == 0
    assert new["fresh_input_tokens"] is new["output_tokens"] is new["cache_read_tokens"] is None
    assert groups[("unknown", "unknown", "maker")]["stage_error_codes"] == {"NO_MAKER": 1}


def test_telemetry_code_status_and_evidence_caps(sandbox):
    events = []
    for index in range(idea.MAX_TELEMETRY_COUNTS + 1):
        events.extend([
            {"event": "provider_result", "node": "n", "round": index, "role": "maker",
             "agent": "a" * 200, "model": "model", "status": str(index), "output_tokens": 1},
            {"event": "repair", "node": "n", "round": index, "code": str(index)},
        ])
    events.append({"event": "repair", "node": "n", "round": 0, "code": "0"})
    for index in range(idea.SOURCE_LIMITS["telemetry"] + 1):
        events.append({"event": "provider_result", "node": str(index), "round": 0, "role": "maker",
                       "agent": str(index), "model": "model", "status": "COMPLETED"})
    write_journal(sandbox, "caps", events)
    result = idea.collect(sandbox["root"] / "evidence.json")
    groups = [e for e in result["items"] if e["source_kind"] == "telemetry"]
    assert len(groups) == idea.SOURCE_LIMITS["telemetry"]
    group = next(e for e in groups if e["agent"] == "a" * 100)
    assert group["runs"] == group["output_tokens"] == idea.MAX_TELEMETRY_COUNTS + 1
    assert group["repair"] == idea.MAX_TELEMETRY_COUNTS + 2
    assert len(group["status_counts"]) == len(group["repair_codes"]) == idea.MAX_TELEMETRY_COUNTS
    assert group["repair_codes"]["0"] == 2


def test_untrusted_fields_cannot_crash_or_smuggle_prompt_via_report(sandbox):
    report = sandbox["root"] / "report.txt"
    idea.atomic_text(report, '{"prompt":"DO_NOT_COPY_THIS", "diff":"PRIVATE_DIFF"}')
    idea.atomic_json(sandbox["home"] / "config.json", {"extra_sources": [str(report)]})
    path = sandbox["root"] / "telemetry" / "run" / "events.jsonl"
    idea.atomic_text(path, json.dumps({"utc": idea.iso(sandbox["now"]), "event": {"prompt": "NESTED"}}))
    result = idea.collect(sandbox["root"] / "evidence.json")
    text = json.dumps(result)
    assert all(marker not in text for marker in ("DO_NOT_COPY_THIS", "PRIVATE_DIFF", "NESTED"))
    assert "<redacted>" == idea.clean('\"token\":\"secret-value\"')


def test_ticket_backlog_does_not_displace_catalog(sandbox, monkeypatch):
    for index in range(idea.SOURCE_LIMITS["helpdesk"] + 1):
        idea.atomic_json(sandbox["helpdesk"] / "tickets" / f"{index}.json", {"id": str(index)})
    catalog = sandbox["root"] / "catalog.py"
    idea.atomic_text(catalog, "# fake")
    monkeypatch.setenv("IDEA_MAKER_CATALOG", str(catalog))
    monkeypatch.setattr(idea.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout=b'{"skills":[{"name":"existing","description":"shared"}]}'))
    result = idea.collect(sandbox["root"] / "evidence.json")
    assert sum(e["source_kind"] == "helpdesk" for e in result["items"]) == idea.SOURCE_LIMITS["helpdesk"]
    assert any(e["source_kind"] == "catalog" for e in result["items"])


def test_add_persists_required_metadata_and_ignores_extra_fields(sandbox):
    data = candidate()
    data["prompt"] = "not stored"
    row = idea.add(write_candidate(sandbox, data), evidence(sandbox), "run-1")
    assert row["id"].startswith("IDEA-20261014-")
    assert len(row["fingerprint"]) == 16
    assert row["status"] == "PROPOSED" and row["run_id"] == "run-1"
    assert idea.parse_iso(row["expires_at"]) - idea.parse_iso(row["created_at"]) == dt.timedelta(days=60)
    assert "prompt" not in row


@pytest.mark.parametrize("mutate", [
    lambda c: c.update(evidence_ids=["hd-1"]),
    lambda c: c.update(evidence_ids=["hd-1", "hd-2"]),
    lambda c: c.update(evidence_ids=["hd-1", "nonexistent"]),
    lambda c: c.update(evidence_ids=["hd-1", "hd-1"]),
    lambda c: c["council"].update(consultants=c["council"]["consultants"][:1]),
    lambda c: c["council"].update(consultants=c["council"]["consultants"] * 2),
    lambda c: c["council"]["consultants"][1].update(lineage="ANTHROPIC"),
    lambda c: c["council"]["consultants"][0].update(lineage=" OpenAI "),
    lambda c: c["council"]["consultants"][0].update(recommendation="ok"),
    lambda c: c.pop("extend_skill"),
    lambda c: c["anti_bureaucracy"].update(new_model_calls=True),
    lambda c: c["anti_bureaucracy"].update(new_reads=-1),
    lambda c: c["expected_gain"].update(measure_how=""),
])
def test_add_invalid_candidate_refused_without_store_write(sandbox, mutate):
    data = candidate()
    mutate(data)
    with pytest.raises(idea.IdeaError):
        idea.add(write_candidate(sandbox, data), evidence(sandbox), "run-1")
    assert not (sandbox["home"] / "candidates").exists()


def test_max_three_per_run_and_other_run_can_continue(sandbox):
    for index in range(3):
        idea.add(write_candidate(sandbox, candidate(title=f"Pomysl {index}")), evidence(sandbox), "run-1")
    with pytest.raises(idea.IdeaError, match="max 3"):
        idea.add(write_candidate(sandbox, candidate(title="Czwarty")), evidence(sandbox), "run-1")
    assert idea.add(write_candidate(sandbox, candidate(title="Czwarty")), evidence(sandbox), "run-2")["status"] == "PROPOSED"


def test_same_day_revisions_cannot_bypass_per_run_cap(sandbox):
    for _ in range(3):
        row = idea.add(write_candidate(sandbox), evidence(sandbox), "run-1")
        idea.decide(row["id"], "accepted", "Decyzja", "Mateusz")
    with pytest.raises(idea.IdeaError, match="max 3"):
        idea.add(write_candidate(sandbox), evidence(sandbox), "run-1")
    assert len(list((sandbox["home"] / "runs" / "run-1" / "candidate-revisions").glob("*.json"))) == 2


def test_duplicates_and_rejected_fingerprint_require_new_evidence(sandbox):
    original = candidate()
    row = idea.add(write_candidate(sandbox, original), evidence(sandbox), "run-1")
    duplicate = copy.deepcopy(original)
    duplicate["title"] = "  LEPSZE   WZNOWIENIE "
    with pytest.raises(idea.IdeaError, match="duplicate"):
        idea.add(write_candidate(sandbox, duplicate), evidence(sandbox), "run-2")
    idea.decide(row["id"], "rejected", "Za malo korzysci", "Mateusz")
    with pytest.raises(idea.IdeaError, match="new evidence"):
        idea.add(write_candidate(sandbox, original), evidence(sandbox), "run-2")
    original["evidence_ids"].append("hd-2")
    new = idea.add(write_candidate(sandbox, original), evidence(sandbox), "run-2")
    assert new["status"] == "PROPOSED"
    assert new["id"] == row["id"]  # canonical ID; old record retained in archive/history
    archive = list((sandbox["home"] / "runs" / "run-1" / "candidate-revisions").glob("*.json"))
    assert len(archive) == 1 and idea.read_json(archive[0])["status"] == "REJECTED"
    assert any(event["status"] == "REJECTED" for event in new["history"])


def test_related_list_expiry_changes_status_without_deletion(sandbox):
    row = idea.add(write_candidate(sandbox), evidence(sandbox), "run-1")
    assert idea.list_candidates("agent-loop")[0]["id"] == row["id"]
    assert idea.list_candidates("agent-graph")[0]["id"] == row["id"]
    assert idea.list_candidates("wznowienie")[0]["id"] == row["id"]
    assert idea.list_candidates("unrelated") == []
    sandbox["now"] += dt.timedelta(days=60)
    assert idea.list_candidates()[0]["status"] == "EXPIRED"
    stored = idea.candidates()[0]
    assert stored["status"] == "EXPIRED" and stored["history"][-1]["by"] == "expiry"
    assert idea.list_candidates(status="EXPIRED")[0]["id"] == row["id"]
    assert (sandbox["home"] / "candidates" / (row["id"] + ".json")).exists()


def test_decide_appends_manual_history(sandbox):
    row = idea.add(write_candidate(sandbox), evidence(sandbox), "run-1")
    for status in ("revise", "accepted", "rejected"):
        result = idea.decide(row["id"], status, "Uzasadnienie", "Mateusz")
    assert [h["status"] for h in result["history"]] == ["PROPOSED", "REVISE", "ACCEPTED", "REJECTED"]
    assert all(h["by"] == "Mateusz" and h["note"] == "Uzasadnienie" for h in result["history"][1:])


def test_announce_dry_run_and_cli_no_office(sandbox, capsys):
    row = idea.add(write_candidate(sandbox), evidence(sandbox), "run-1")
    assert idea.main(["announce", "--id", row["id"], "--dry-run"]) == 0
    body = capsys.readouterr().out
    assert row["id"] in body and row["title"] in body and "Mateusza" in body


def test_announce_transport_failure_is_single_hidden_call(sandbox, monkeypatch):
    row = idea.add(write_candidate(sandbox), evidence(sandbox), "run-1")
    cli = sandbox["root"] / "office.py"
    idea.atomic_text(cli, "# fake")
    idea.atomic_json(sandbox["home"] / "config.json", {"office_cli": str(cli)})
    calls = []

    def fail(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(idea.subprocess, "run", fail)
    assert idea.announce(row["id"])["result"] == "announce-failed"
    assert len(calls) == 1
    assert calls[0][0][-1] == "--body-stdin"
    assert "skill-candidates" in calls[0][0] and "note" in calls[0][0]
    assert row["id"] in calls[0][1]["input"].decode()
    assert calls[0][1]["creationflags"] == idea.flags()
    assert calls[0][1]["env"]["PYTHONIOENCODING"] == "utf-8"
    assert calls[0][1]["env"]["PYTHONUTF8"] == "1"


def test_command_lifecycle_leaves_source_tree_unchanged(sandbox, monkeypatch, capsys, source_snapshot):
    before = source_snapshot()
    idea.atomic_json(sandbox["helpdesk"] / "tickets" / "one.json", {"id": "ticket-1", "symptom": "Wznowienie"})
    catalog = sandbox["root"] / "catalog.py"
    idea.atomic_text(catalog, "# fake")
    monkeypatch.setenv("IDEA_MAKER_CATALOG", str(catalog))
    monkeypatch.setattr(idea.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout=b'{"skills":[{"name":"agent-loop","description":"shared"}]}'))
    evidence_path = sandbox["root"] / "run-1" / "evidence.json"
    assert idea.main(["collect", "--out", str(evidence_path)]) == 0
    collected = json.loads(capsys.readouterr().out)
    data = candidate()
    data["evidence_ids"] = [next(item["evidence_id"] for item in collected["items"]
                                 if item["source_kind"] == kind) for kind in ("helpdesk", "catalog")]
    candidate_path = write_candidate(sandbox, data)
    assert idea.main(["add", "--file", str(candidate_path), "--evidence", str(evidence_path),
                      "--run-id", "run-1"]) == 0
    cid = json.loads(capsys.readouterr().out)["id"]
    assert idea.main(["list", "--related", "agent-loop", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["id"] == cid
    assert idea.main(["decide", "--id", cid, "--status", "revise", "--note", "Zawez", "--by", "Mateusz"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "REVISE"
    assert idea.main(["announce", "--id", cid, "--dry-run"]) == 0
    assert cid in capsys.readouterr().out
    assert source_snapshot() == before


def test_no_deletion_calls_in_source():
    root = Path(__file__).resolve().parents[1]
    forbidden = {"remove", "unlink", "rmtree", "rmdir"}
    for path in (root / "scripts").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assert not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in forbidden
                       for n in ast.walk(tree)), path.name
    schedule = (root / "scripts" / "install_schedule.ps1").read_text(encoding="utf-8").lower()
    assert "remove-item" not in schedule and "unregister-scheduledtask" not in schedule


def test_collect_execution_audit_success_and_idempotence(sandbox, monkeypatch):
    fake_cli = sandbox["root"] / "fake_audit.py"
    idea.atomic_text(fake_cli, "# fake audit CLI")
    state_dir = sandbox["root"] / "audit-state"
    monkeypatch.setenv("IDEA_MAKER_AUDIT_CLI", str(fake_cli))
    monkeypatch.setenv("IDEA_MAKER_AUDIT_STATE", str(state_dir))

    valid_rec_id = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    valid_aud_id = "1123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    valid_input_sha = "2123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    calls = []

    def fake_export(argv, **kwargs):
        calls.append((argv, kwargs))
        output_file = Path(argv[argv.index("--output") + 1])
        payload = {
            "schema_version": 1,
            "producer": "delivery-audit",
            "records": [
                {
                    "schema_version": 1,
                    "producer": "delivery-audit",
                    "record_id": valid_rec_id,
                    "audit_id": valid_aud_id,
                    "project_slug": "project-alpha",
                    "task_id": "task-01",
                    "task_family": "feature",
                    "harness": "cli",
                    "observed_at": "2026-10-14T10:00:00Z",
                    "input_sha256": valid_input_sha,
                    "classification": "systemic",
                    "code": "LOOP_ISSUE",
                    "component": "agent-loop",
                    "evidence_refs": ["ref-a", "ref-b"],
                    "support": {"tasks": 3, "workspaces": 2, "actor_provider_pairs": 4},
                    "verification": {
                        "status": "CONFIRMED",
                        "recorded_run_ids": ["run-1", "run-2"],
                        "maker_provider": "codex",
                        "reviewer_provider": "claude",
                    },
                    "release_authority": False,
                },
                {
                    "schema_version": 1,
                    "producer": "delivery-audit",
                    "record_id": valid_rec_id,  # duplicate record_id to test idempotence
                    "code": "DUPLICATE_SHOULD_BE_IGNORED",
                },
            ],
        }
        idea.atomic_json(output_file, payload)
        return SimpleNamespace(returncode=0, stdout=b"")

    monkeypatch.setattr(idea.subprocess, "run", fake_export)
    out = sandbox["root"] / "evidence.json"
    collected = idea.collect(out)

    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[0] == idea.sys.executable
    assert argv[1] == "-B"
    assert argv[2] == str(fake_cli)
    assert argv[3] == "export-confirmed"
    assert argv[4] == "--state-dir"
    assert argv[5] == str(state_dir.resolve())
    assert argv[6] == "--output"
    assert kwargs["creationflags"] == idea.flags()
    assert kwargs["timeout"] == 120
    assert kwargs["env"]["PYTHONIOENCODING"] == "utf-8"
    assert kwargs["env"]["PYTHONUTF8"] == "1"

    source_info = collected["sources"]["execution-audit"]
    assert source_info == {"status": "available", "count": 1, "invalid": 0}

    audit_items = [e for e in collected["items"] if e["source_kind"] == "execution-audit"]
    assert len(audit_items) == 1
    rec = audit_items[0]
    assert rec["schema_version"] == 1
    assert rec["producer"] == "delivery-audit"
    assert rec["record_id"] == valid_rec_id
    assert rec["code"] == "LOOP_ISSUE"
    assert rec["component"] == "agent-loop"
    assert rec["release_authority"] is False
    assert rec["evidence_refs"] == ["ref-a", "ref-b"]
    assert rec["support"] == {"tasks": 3, "workspaces": 2, "actor_provider_pairs": 4}
    assert rec["verification"]["status"] == "CONFIRMED"


def test_collect_execution_audit_empty_feed_valid(sandbox, monkeypatch):
    fake_cli = sandbox["root"] / "fake_audit.py"
    idea.atomic_text(fake_cli, "# fake audit CLI")
    monkeypatch.setenv("IDEA_MAKER_AUDIT_CLI", str(fake_cli))

    def fake_empty(argv, **kwargs):
        output_file = Path(argv[argv.index("--output") + 1])
        idea.atomic_json(output_file, {"schema_version": 1, "producer": "delivery-audit", "records": []})
        return SimpleNamespace(returncode=0, stdout=b"")

    monkeypatch.setattr(idea.subprocess, "run", fake_empty)
    out = sandbox["root"] / "evidence.json"
    collected = idea.collect(out)
    assert collected["sources"]["execution-audit"] == {"status": "available", "count": 0, "invalid": 0}
    assert not any(e["source_kind"] == "execution-audit" for e in collected["items"])


def test_collect_execution_audit_missing_cli_error_and_timeout(sandbox, monkeypatch):
    # A: Missing CLI
    monkeypatch.setenv("IDEA_MAKER_AUDIT_CLI", str(sandbox["root"] / "nonexistent-audit.py"))
    collected = idea.collect(sandbox["root"] / "evidence.json")
    assert collected["sources"]["execution-audit"] == {"status": "unavailable", "count": 0, "invalid": 0}

    # B: CLI error returncode
    fake_cli = sandbox["root"] / "fake_audit.py"
    idea.atomic_text(fake_cli, "# fake")
    monkeypatch.setenv("IDEA_MAKER_AUDIT_CLI", str(fake_cli))
    monkeypatch.setattr(idea.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=1, stdout=b""))
    collected = idea.collect(sandbox["root"] / "evidence.json")
    assert collected["sources"]["execution-audit"]["status"] == "unavailable"

    # C: Timeout
    def timeout_call(*a, **kw):
        raise idea.subprocess.TimeoutExpired(a[0], 120)

    monkeypatch.setattr(idea.subprocess, "run", timeout_call)
    collected = idea.collect(sandbox["root"] / "evidence.json")
    assert collected["sources"]["execution-audit"]["status"] == "unavailable"


def test_collect_execution_audit_malicious_record_capped_and_redacted(sandbox, monkeypatch):
    fake_cli = sandbox["root"] / "fake_audit.py"
    idea.atomic_text(fake_cli, "# fake")
    monkeypatch.setenv("IDEA_MAKER_AUDIT_CLI", str(fake_cli))

    evil_rec_id = "e123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    evil_aud_id = "e223456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    evil_input_sha = "e323456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

    def fake_malicious(argv, **kwargs):
        output_file = Path(argv[argv.index("--output") + 1])
        payload = {
            "schema_version": 1,
            "producer": "delivery-audit",
            "records": [
                {
                    "schema_version": 1,
                    "producer": "delivery-audit",
                    "record_id": evil_rec_id,
                    "audit_id": evil_aud_id,
                    "project_slug": "project-evil",
                    "task_id": "task-evil",
                    "task_family": "feature",
                    "harness": "cli",
                    "observed_at": "2026-10-14T10:00:00Z",
                    "input_sha256": evil_input_sha,
                    "classification": "systemic",
                    "code": "C" * 500,
                    "component": "password=supersecret " + "K" * 300,
                    "evidence_refs": [f"ref-{i}" for i in range(50)],
                    "support": {"tasks": 1, "workspaces": 1, "actor_provider_pairs": 1},
                    "verification": {
                        "status": "CONFIRMED",
                        "recorded_run_ids": ["run-1"],
                        "maker_provider": "codex",
                        "reviewer_provider": "claude",
                    },
                    "release_authority": False,
                    "prompt": "FORBIDDEN_PROMPT_DO_NOT_COPY",
                    "diff": "FORBIDDEN_DIFF_DO_NOT_COPY",
                    "secret": "FORBIDDEN_SECRET_DO_NOT_COPY",
                }
            ],
        }
        idea.atomic_json(output_file, payload)
        return SimpleNamespace(returncode=0, stdout=b"")

    monkeypatch.setattr(idea.subprocess, "run", fake_malicious)
    out = sandbox["root"] / "evidence.json"
    collected = idea.collect(out)
    text = out.read_text(encoding="utf-8")

    assert "FORBIDDEN_PROMPT" not in text
    assert "FORBIDDEN_DIFF" not in text
    assert "FORBIDDEN_SECRET" not in text
    assert "supersecret" not in text

    rec = next(e for e in collected["items"] if e["source_kind"] == "execution-audit")
    assert len(rec["code"]) == 100
    assert len(rec["evidence_refs"]) == 20
    assert "prompt" not in rec and "diff" not in rec and "secret" not in rec


def test_collect_ab_lesson_and_edits(sandbox, monkeypatch):
    ab_home = sandbox["root"] / "testy-ab"
    monkeypatch.setenv("TESTY_AB_HOME", str(ab_home))

    # Report 1: valid lesson and helped edit
    rep1 = ab_home / "run1" / "report.json"
    idea.atomic_json(rep1, {
        "trial_id": "trial-01",
        "target_skill": "skill-alpha",
        "reported_at": "2026-10-14T10:00:00Z",
        "lesson": {
            "cue": "when test fails",
            "lesson": "narrow condition",
            "component": "step-1",
            "evidence": ["e1", "e2"],
            "confidence": "high",
        },
        "edits": [
            {
                "variant": "v1",
                "verdict": "helped",
                "components": ["step-1"],
                "observed": {"score_delta": 0.4, "regressions": 0},
            },
            {
                "variant": "v2",
                "verdict": "no_effect",
                "components": ["step-2"],
            },
        ],
    })

    # Report 2: NO_ENTRY lesson (should be ignored) and hurt edit
    rep2 = ab_home / "run2" / "report.json"
    idea.atomic_json(rep2, {
        "trial_id": "trial-02",
        "target_skill": "skill-beta",
        "reported_at": "2026-10-12T10:00:00Z",
        "lesson": "NO_ENTRY",
        "edits": [
            {
                "variant": "v3",
                "verdict": "hurt",
                "components": ["trigger"],
                "observed": {"score_delta": -0.3, "regressions": 2},
            },
            {
                "variant": "v4",
                "verdict": "confounded",
                "components": ["step-a", "step-b"],
            },
        ],
    })

    # Report 3: Older than 60 days (should be skipped)
    rep3 = ab_home / "run_old" / "report.json"
    idea.atomic_json(rep3, {
        "trial_id": "trial-old",
        "target_skill": "skill-old",
        "reported_at": "2026-08-01T00:00:00Z",
        "lesson": {
            "cue": "ancient cue",
            "lesson": "ancient lesson",
            "component": "ancient-comp",
        },
        "edits": [
            {
                "variant": "v_old",
                "verdict": "helped",
                "components": ["comp"],
            },
        ],
    })

    out = sandbox["root"] / "evidence.json"
    collected = idea.collect(out)
    assert collected["sources"]["ab-lesson"]["status"] == "available"

    ab_items = [e for e in collected["items"] if e["source_kind"] == "ab-lesson"]
    # 3 items expected: 1 lesson (rep1), 1 helped edit (rep1), 1 hurt edit (rep2)
    assert len(ab_items) == 3

    lessons = [e for e in ab_items if e.get("kind") == "lesson"]
    assert len(lessons) == 1
    assert lessons[0]["trial_id"] == "trial-01"
    assert lessons[0]["lesson"] == "narrow condition"
    assert lessons[0]["confidence"] == "high"

    edits = [e for e in ab_items if e.get("kind") == "edit"]
    assert len(edits) == 2
    helped = next(e for e in edits if e["verdict"] == "helped")
    assert helped["trial_id"] == "trial-01"
    assert helped["variant"] == "v1"
    assert helped["observed"] == {"score_delta": 0.4, "regressions": 0}

    hurt = next(e for e in edits if e["verdict"] == "hurt")
    assert hurt["trial_id"] == "trial-02"
    assert hurt["variant"] == "v3"
    assert hurt["observed"] == {"score_delta": -0.3, "regressions": 2}


def test_collect_ab_lesson_missing_dir_unavailable(sandbox, monkeypatch):
    monkeypatch.setenv("TESTY_AB_HOME", str(sandbox["root"] / "nonexistent-testy-ab"))
    out = sandbox["root"] / "evidence.json"
    collected = idea.collect(out)
    assert collected["sources"]["ab-lesson"] == {"status": "unavailable", "count": 0, "invalid": 0}


def test_collect_execution_audit_invalid_envelope_rejected(sandbox, monkeypatch):
    fake_cli = sandbox["root"] / "fake_audit.py"
    idea.atomic_text(fake_cli, "# fake")
    monkeypatch.setenv("IDEA_MAKER_AUDIT_CLI", str(fake_cli))

    def fake_invalid_envelope(argv, **kwargs):
        output_file = Path(argv[argv.index("--output") + 1])
        payload = {
            "schema_version": 999,
            "producer": "unknown",
            "records": [
                {
                    "record_id": "rec-001",
                    "code": "CODE",
                    "component": "comp",
                    "verification": {"status": "REFUSED"},
                }
            ],
        }
        idea.atomic_json(output_file, payload)
        return SimpleNamespace(returncode=0, stdout=b"")

    monkeypatch.setattr(idea.subprocess, "run", fake_invalid_envelope)
    out = sandbox["root"] / "evidence.json"
    collected = idea.collect(out)
    assert collected["sources"]["execution-audit"] == {"status": "unavailable", "count": 0, "invalid": 1}
    assert not any(e["source_kind"] == "execution-audit" for e in collected["items"])


def test_collect_execution_audit_unconfirmed_and_incomplete_records_rejected(sandbox, monkeypatch):
    fake_cli = sandbox["root"] / "fake_audit.py"
    idea.atomic_text(fake_cli, "# fake")
    monkeypatch.setenv("IDEA_MAKER_AUDIT_CLI", str(fake_cli))

    def fake_unconfirmed(argv, **kwargs):
        output_file = Path(argv[argv.index("--output") + 1])
        payload = {
            "schema_version": 1,
            "producer": "delivery-audit",
            "records": [
                {"schema_version": 1, "producer": "delivery-audit", "record_id": "r-refused", "code": "C1", "component": "cmp1", "verification": {"status": "REFUSED"}},
                {"schema_version": 1, "producer": "delivery-audit", "record_id": "r-unknown", "code": "C2", "component": "cmp2", "verification": {"status": "UNKNOWN"}},
                {"schema_version": 1, "producer": "delivery-audit", "record_id": "r-no-ver", "code": "C3", "component": "cmp3"},
                {"schema_version": 1, "producer": "delivery-audit", "record_id": "r-no-code", "component": "cmp4", "verification": {"status": "CONFIRMED"}},
                {"schema_version": 1, "producer": "delivery-audit", "record_id": "r-no-comp", "code": "C5", "verification": {"status": "CONFIRMED"}},
                {"schema_version": 1, "producer": "delivery-audit", "code": "C6", "component": "cmp6", "verification": {"status": "CONFIRMED"}},
            ],
        }
        idea.atomic_json(output_file, payload)
        return SimpleNamespace(returncode=0, stdout=b"")

    monkeypatch.setattr(idea.subprocess, "run", fake_unconfirmed)
    out = sandbox["root"] / "evidence.json"
    collected = idea.collect(out)
    assert collected["sources"]["execution-audit"] == {"status": "unavailable", "count": 0, "invalid": 6}
    assert not any(e["source_kind"] == "execution-audit" for e in collected["items"])


def test_collect_execution_audit_mixed_confirmed_and_refused(sandbox, monkeypatch):
    fake_cli = sandbox["root"] / "fake_audit.py"
    idea.atomic_text(fake_cli, "# fake")
    monkeypatch.setenv("IDEA_MAKER_AUDIT_CLI", str(fake_cli))

    valid_rec_id = "4" * 64
    valid_aud_id = "5" * 64
    valid_input_sha = "6" * 64

    def fake_mixed(argv, **kwargs):
        output_file = Path(argv[argv.index("--output") + 1])
        payload = {
            "schema_version": 1,
            "producer": "delivery-audit",
            "records": [
                {
                    "schema_version": 1,
                    "producer": "delivery-audit",
                    "record_id": valid_rec_id,
                    "audit_id": valid_aud_id,
                    "project_slug": "project-slug",
                    "task_id": "task-01",
                    "task_family": "feature",
                    "harness": "cli",
                    "observed_at": "2026-10-14T10:00:00Z",
                    "input_sha256": valid_input_sha,
                    "classification": "systemic",
                    "code": "FINDING",
                    "component": "comp",
                    "evidence_refs": ["ref-1"],
                    "support": {"tasks": 1, "workspaces": 1, "actor_provider_pairs": 1},
                    "verification": {
                        "status": "CONFIRMED",
                        "recorded_run_ids": ["run-001"],
                        "maker_provider": "codex",
                        "reviewer_provider": "claude",
                    },
                    "release_authority": False,
                },
                {
                    "schema_version": 1,
                    "producer": "delivery-audit",
                    "record_id": "7" * 64,
                    "code": "REFUSED_CODE",
                    "component": "comp",
                    "verification": {"status": "REFUSED"},
                },
            ],
        }
        idea.atomic_json(output_file, payload)
        return SimpleNamespace(returncode=0, stdout=b"")

    monkeypatch.setattr(idea.subprocess, "run", fake_mixed)
    out = sandbox["root"] / "evidence.json"
    collected = idea.collect(out)
    assert collected["sources"]["execution-audit"] == {"status": "available", "count": 1, "invalid": 1}
    audit_items = [e for e in collected["items"] if e["source_kind"] == "execution-audit"]
    assert len(audit_items) == 1
    assert audit_items[0]["record_id"] == valid_rec_id


def test_collect_ab_lesson_200_old_reports_do_not_displace_current_report(sandbox, monkeypatch):
    ab_home = sandbox["root"] / "testy-ab"
    monkeypatch.setenv("TESTY_AB_HOME", str(ab_home))

    # 205 old reports older than 60 days, named so they sort lexicographically before current report
    old_time = idea.iso(sandbox["now"] - dt.timedelta(days=75))
    for i in range(205):
        rep = ab_home / f"aaa_old_{i:03d}" / "report.json"
        idea.atomic_json(rep, {
            "trial_id": f"old-{i}",
            "target_skill": "skill-old",
            "reported_at": old_time,
            "lesson": {"cue": "old", "lesson": "old", "component": "old"},
            "edits": [{"variant": "v_old", "verdict": "helped", "components": ["c"]}],
        })

    # 1 current report (within 60 days)
    current_time = idea.iso(sandbox["now"] - dt.timedelta(days=5))
    current_rep = ab_home / "zzz_current" / "report.json"
    idea.atomic_json(current_rep, {
        "trial_id": "current-01",
        "target_skill": "skill-current",
        "reported_at": current_time,
        "lesson": {
            "cue": "fresh cue",
            "lesson": "fresh lesson",
            "component": "fresh-comp",
            "confidence": "high",
        },
        "edits": [
            {
                "variant": "v_fresh",
                "verdict": "helped",
                "components": ["fresh-comp"],
                "observed": {"score_delta": 0.8, "regressions": 0},
            }
        ],
    })

    out = sandbox["root"] / "evidence.json"
    collected = idea.collect(out)
    assert collected["sources"]["ab-lesson"]["status"] == "available"
    ab_items = [e for e in collected["items"] if e["source_kind"] == "ab-lesson"]
    assert len(ab_items) == 2
    assert any(e.get("trial_id") == "current-01" and e.get("kind") == "lesson" for e in ab_items)
    assert any(e.get("trial_id") == "current-01" and e.get("kind") == "edit" for e in ab_items)


def test_collect_ab_lesson_preserves_regressions_case_list(sandbox, monkeypatch):
    ab_home = sandbox["root"] / "testy-ab"
    monkeypatch.setenv("TESTY_AB_HOME", str(ab_home))

    rep = ab_home / "run_reg" / "report.json"
    idea.atomic_json(rep, {
        "trial_id": "trial-reg",
        "target_skill": "skill-reg",
        "reported_at": idea.iso(sandbox["now"] - dt.timedelta(days=2)),
        "lesson": "NO_ENTRY",
        "edits": [
            {
                "variant": "v_hurt",
                "verdict": "hurt",
                "components": ["step-reg"],
                "observed": {
                    "score_delta": -0.6,
                    "regressions": ["case-fail-1", "case-fail-2"],
                },
            }
        ],
    })

    out = sandbox["root"] / "evidence.json"
    collected = idea.collect(out)
    assert collected["sources"]["ab-lesson"]["status"] == "available"
    hurt_edit = next(e for e in collected["items"] if e["source_kind"] == "ab-lesson" and e.get("kind") == "edit")
    assert hurt_edit["observed"]["regressions"] == ["case-fail-1", "case-fail-2"]


def test_collect_execution_audit_strict_contract_validation_rejects_incomplete(sandbox, monkeypatch):
    fake_cli = sandbox["root"] / "fake_audit.py"
    idea.atomic_text(fake_cli, "# fake")
    monkeypatch.setenv("IDEA_MAKER_AUDIT_CLI", str(fake_cli))

    base_valid = {
        "schema_version": 1,
        "producer": "delivery-audit",
        "record_id": "a" * 64,
        "audit_id": "b" * 64,
        "project_slug": "project-slug",
        "task_id": "task-01",
        "task_family": "feature",
        "harness": "cli",
        "observed_at": "2026-10-14T10:00:00Z",
        "input_sha256": "c" * 64,
        "classification": "systemic",
        "code": "FINDING",
        "component": "comp",
        "evidence_refs": ["ref-1"],
        "support": {"tasks": 1, "workspaces": 1, "actor_provider_pairs": 1},
        "verification": {
            "status": "CONFIRMED",
            "recorded_run_ids": ["run-001"],
            "maker_provider": "codex",
            "reviewer_provider": "claude",
        },
        "release_authority": False,
    }

    # 1. Confirm acceptance of the valid base fixture first
    def fake_base_ok(argv, **kwargs):
        output_file = Path(argv[argv.index("--output") + 1])
        idea.atomic_json(output_file, {
            "schema_version": 1,
            "producer": "delivery-audit",
            "records": [base_valid],
        })
        return SimpleNamespace(returncode=0, stdout=b"")

    monkeypatch.setattr(idea.subprocess, "run", fake_base_ok)
    out_ok = sandbox["root"] / "evidence_ok.json"
    collected_ok = idea.collect(out_ok)
    assert collected_ok["sources"]["execution-audit"] == {"status": "available", "count": 1, "invalid": 0}
    assert any(e["source_kind"] == "execution-audit" and e["record_id"] == "a" * 64 for e in collected_ok["items"])

    # 2. Individual violations for release_authority, evidence_refs, hashes, safe-ids, support, etc.
    bad_records = [
        # schema_version violations (must be strictly int 1)
        {k: v for k, v in base_valid.items() if k != "schema_version"},
        dict(base_valid, schema_version=2),
        dict(base_valid, schema_version=0),
        dict(base_valid, schema_version="1"),
        dict(base_valid, schema_version=True),
        dict(base_valid, schema_version=None),

        # producer violations (must be "delivery-audit")
        {k: v for k, v in base_valid.items() if k != "producer"},
        dict(base_valid, producer="other-producer"),
        dict(base_valid, producer=""),
        dict(base_valid, producer=None),
        dict(base_valid, producer=123),

        # release_authority violations (must be strictly False)
        dict(base_valid, release_authority=True),
        {k: v for k, v in base_valid.items() if k != "release_authority"},  # missing
        dict(base_valid, release_authority=None),
        dict(base_valid, release_authority=1),
        dict(base_valid, release_authority="False"),
        dict(base_valid, release_authority="true"),

        # evidence_refs violations (must be present list of safe-ids)
        {k: v for k, v in base_valid.items() if k != "evidence_refs"},  # missing
        dict(base_valid, evidence_refs=None),
        dict(base_valid, evidence_refs="not-a-list"),
        dict(base_valid, evidence_refs=["ref-1", "bad ref with spaces"]),
        dict(base_valid, evidence_refs=["ref-1", None]),
        dict(base_valid, evidence_refs=["ref-1", 123]),

        # hash violations: record_id, audit_id, input_sha256 (must be raw 64 hex chars)
        {k: v for k, v in base_valid.items() if k != "record_id"},
        dict(base_valid, record_id=None),
        dict(base_valid, record_id="rec-001"),  # short
        dict(base_valid, record_id="g" * 64),   # non-hex
        dict(base_valid, record_id=12345),
        {k: v for k, v in base_valid.items() if k != "audit_id"},
        dict(base_valid, audit_id=None),
        dict(base_valid, audit_id=""),
        dict(base_valid, audit_id="aud-001"),  # short
        {k: v for k, v in base_valid.items() if k != "input_sha256"},
        dict(base_valid, input_sha256=None),
        dict(base_valid, input_sha256=""),
        dict(base_valid, input_sha256="1234567890abcdef"),  # short

        # safe-id violations: project_slug, task_id, verification safe-ids
        {k: v for k, v in base_valid.items() if k != "project_slug"},
        dict(base_valid, project_slug=None),
        dict(base_valid, project_slug=""),
        dict(base_valid, project_slug="bad slug with space"),
        dict(base_valid, project_slug="-dash-start"),
        {k: v for k, v in base_valid.items() if k != "task_id"},
        dict(base_valid, task_id=None),
        dict(base_valid, task_id=""),
        dict(base_valid, task_id="bad@task"),
        dict(base_valid, verification=dict(base_valid["verification"], maker_provider="bad maker!")),
        dict(base_valid, verification=dict(base_valid["verification"], reviewer_provider="")),
        dict(base_valid, verification=dict(base_valid["verification"], recorded_run_ids=[])),
        dict(base_valid, verification=dict(base_valid["verification"], recorded_run_ids=["run-1", "bad run id"])),

        # support violations
        {k: v for k, v in base_valid.items() if k != "support"},
        dict(base_valid, support=None),
        dict(base_valid, support={"tasks": -1, "workspaces": 1, "actor_provider_pairs": 1}),
        dict(base_valid, support={"tasks": 1, "workspaces": 1}),
        dict(base_valid, support={"tasks": True, "workspaces": 1, "actor_provider_pairs": 1}),

        # classification, timestamp, and required field violations
        dict(base_valid, classification="isolated"),
        dict(base_valid, observed_at="2099-01-01T00:00:00Z"),
        dict(base_valid, observed_at="not-a-date"),
        dict(base_valid, observed_at="0001-01-01T00:00:00+01:00"),
        dict(base_valid, observed_at="9999-12-31T23:59:59-01:00"),
        dict(base_valid, task_family=""),
        dict(base_valid, harness=""),
        dict(base_valid, code=""),
        dict(base_valid, component=""),
    ]

    def fake_incomplete(argv, **kwargs):
        output_file = Path(argv[argv.index("--output") + 1])
        idea.atomic_json(output_file, {
            "schema_version": 1,
            "producer": "delivery-audit",
            "records": bad_records,
        })
        return SimpleNamespace(returncode=0, stdout=b"")

    monkeypatch.setattr(idea.subprocess, "run", fake_incomplete)
    out = sandbox["root"] / "evidence.json"
    collected = idea.collect(out)
    assert collected["sources"]["execution-audit"] == {"status": "unavailable", "count": 0, "invalid": len(bad_records)}
    assert not any(e["source_kind"] == "execution-audit" for e in collected["items"])


def test_collect_ab_lesson_future_reports_ignored(sandbox, monkeypatch):
    ab_home = sandbox["root"] / "testy-ab"
    monkeypatch.setenv("TESTY_AB_HOME", str(ab_home))

    rep_fut = ab_home / "fut_run" / "report.json"
    idea.atomic_json(rep_fut, {
        "trial_id": "trial-future",
        "target_skill": "skill-fut",
        "reported_at": idea.iso(sandbox["now"] + dt.timedelta(days=10)),
        "lesson": {
            "cue": "future cue",
            "lesson": "future lesson",
            "component": "future-comp",
        },
        "edits": [{"variant": "v_fut", "verdict": "helped", "components": ["c"]}],
    })

    rep_cur = ab_home / "cur_run" / "report.json"
    idea.atomic_json(rep_cur, {
        "trial_id": "trial-cur",
        "target_skill": "skill-cur",
        "reported_at": idea.iso(sandbox["now"] - dt.timedelta(days=2)),
        "lesson": {
            "cue": "current cue",
            "lesson": "current lesson",
            "component": "current-comp",
            "confidence": "high",
        },
        "edits": [{"variant": "v_cur", "verdict": "helped", "components": ["c"], "observed": {"score_delta": 0.5}}],
    })

    out = sandbox["root"] / "evidence.json"
    collected = idea.collect(out)
    assert collected["sources"]["ab-lesson"]["status"] == "available"
    ab_items = [e for e in collected["items"] if e["source_kind"] == "ab-lesson"]
    assert len(ab_items) == 2
    assert all(e["trial_id"] == "trial-cur" for e in ab_items)


def test_collect_ab_lesson_bounded_buffer_keeps_newest_200(sandbox, monkeypatch):
    ab_home = sandbox["root"] / "testy-ab"
    monkeypatch.setenv("TESTY_AB_HOME", str(ab_home))
    # Raise source limit exclusively in this test to independently verify the 200 buffer cap
    monkeypatch.setitem(idea.SOURCE_LIMITS, "ab-lesson", 300)

    # 240 reports created in shuffled/non-monotonic order
    # Index 0 is newest (1 hour ago), index 239 is oldest (240 hours ago)
    # The heap bounded buffer of 200 elements must keep exactly indices 0..199 and discard 200..239
    shuffled_order = list(range(240))
    shuffled_order = shuffled_order[::2] + shuffled_order[1::2]
    shuffled_order.reverse()

    for idx, i in enumerate(shuffled_order):
        rep = ab_home / f"dir_{idx:03d}" / "report.json"
        rep_time = idea.iso(sandbox["now"] - dt.timedelta(hours=i + 1))
        idea.atomic_json(rep, {
            "trial_id": f"trial-{i:03d}",
            "target_skill": f"skill-{i:03d}",
            "reported_at": rep_time,
            "lesson": {
                "cue": f"cue-{i}",
                "lesson": f"lesson-{i}",
                "component": f"comp-{i}",
            },
        })

    out = sandbox["root"] / "evidence.json"
    collected = idea.collect(out)
    assert collected["sources"]["ab-lesson"]["status"] == "available"
    ab_lessons = [e for e in collected["items"] if e["source_kind"] == "ab-lesson" and e.get("kind") == "lesson"]
    assert len(ab_lessons) == 200
    assert ab_lessons[0]["trial_id"] == "trial-000"
    assert ab_lessons[199]["trial_id"] == "trial-199"
    trial_ids = {e["trial_id"] for e in ab_lessons}
    assert "trial-200" not in trial_ids
    assert "trial-239" not in trial_ids


def test_collect_ab_lesson_lazy_enumeration(sandbox, monkeypatch):
    ab_home = sandbox["root"] / "testy-ab"
    monkeypatch.setenv("TESTY_AB_HOME", str(ab_home))

    for i in range(5):
        rep = ab_home / f"run_{i}" / "report.json"
        idea.atomic_json(rep, {
            "trial_id": f"trial-{i}",
            "target_skill": f"skill-{i}",
            "reported_at": idea.iso(sandbox["now"] - dt.timedelta(hours=i + 1)),
            "lesson": {
                "cue": f"cue-{i}",
                "lesson": f"lesson-{i}",
                "component": f"comp-{i}",
            },
        })

    events: list[str] = []
    real_glob = Path.glob

    def tracking_glob(self, pattern):
        for item in real_glob(self, pattern):
            events.append("glob")
            yield item

    real_read = idea.read_json

    def tracking_read(path):
        events.append("read")
        return real_read(path)

    monkeypatch.setattr(Path, "glob", tracking_glob)
    monkeypatch.setattr(idea, "read_json", tracking_read)

    out = sandbox["root"] / "evidence.json"
    idea.collect(out)

    first_read_idx = events.index("read")
    first_glob_idx = events.index("glob")
    assert first_read_idx == first_glob_idx + 1
    assert events[:first_read_idx].count("glob") == 1


def test_parse_iso_range_overflow():
    assert idea.parse_iso("0001-01-01T00:00:00+01:00") is None
    assert idea.parse_iso("9999-12-31T23:59:59-01:00") is None
    assert idea.parse_iso("not-a-date") is None
    assert idea.parse_iso(12345) is None
    parsed = idea.parse_iso("2026-10-21T02:00:00Z")
    assert parsed == dt.datetime(2026, 10, 21, 2, 0, 0, tzinfo=dt.timezone.utc)


def test_collect_execution_audit_overflow_timestamp_rejected_or_unavailable(sandbox, monkeypatch):
    fake_cli = sandbox["root"] / "fake_audit.py"
    idea.atomic_text(fake_cli, "# fake")
    monkeypatch.setenv("IDEA_MAKER_AUDIT_CLI", str(fake_cli))

    valid_rec_id = "1" * 64
    valid_aud_id = "2" * 64
    valid_input_sha = "3" * 64

    base_record = {
        "schema_version": 1,
        "producer": "delivery-audit",
        "record_id": valid_rec_id,
        "audit_id": valid_aud_id,
        "project_slug": "project-slug",
        "task_id": "task-01",
        "task_family": "feature",
        "harness": "cli",
        "observed_at": "2026-10-14T10:00:00Z",
        "input_sha256": valid_input_sha,
        "classification": "systemic",
        "code": "FINDING",
        "component": "comp",
        "evidence_refs": ["ref-1"],
        "support": {"tasks": 1, "workspaces": 1, "actor_provider_pairs": 1},
        "verification": {
            "status": "CONFIRMED",
            "recorded_run_ids": ["run-001"],
            "maker_provider": "codex",
            "reviewer_provider": "claude",
        },
        "release_authority": False,
    }

    # Case A: mixed records: one valid, one overflow timestamp
    overflow_rec = dict(base_record, record_id="8" * 64, observed_at="0001-01-01T00:00:00+01:00")

    def fake_mixed(argv, **kwargs):
        output_file = Path(argv[argv.index("--output") + 1])
        payload = {
            "schema_version": 1,
            "producer": "delivery-audit",
            "records": [base_record, overflow_rec],
        }
        idea.atomic_json(output_file, payload)
        return SimpleNamespace(returncode=0, stdout=b"")

    monkeypatch.setattr(idea.subprocess, "run", fake_mixed)
    out = sandbox["root"] / "evidence_mixed.json"
    collected = idea.collect(out)
    assert collected["sources"]["execution-audit"] == {"status": "available", "count": 1, "invalid": 1}
    audit_items = [e for e in collected["items"] if e["source_kind"] == "execution-audit"]
    assert len(audit_items) == 1
    assert audit_items[0]["record_id"] == valid_rec_id

    # Case B: only overflow record -> source is unavailable, not crashing
    def fake_only_overflow(argv, **kwargs):
        output_file = Path(argv[argv.index("--output") + 1])
        payload = {
            "schema_version": 1,
            "producer": "delivery-audit",
            "records": [overflow_rec],
        }
        idea.atomic_json(output_file, payload)
        return SimpleNamespace(returncode=0, stdout=b"")

    monkeypatch.setattr(idea.subprocess, "run", fake_only_overflow)
    out2 = sandbox["root"] / "evidence_only_overflow.json"
    collected2 = idea.collect(out2)
    assert collected2["sources"]["execution-audit"] == {"status": "unavailable", "count": 0, "invalid": 1}
    assert not any(e["source_kind"] == "execution-audit" for e in collected2["items"])


def test_collect_ab_lesson_overflow_timestamp_rejected_or_unavailable(sandbox, monkeypatch):
    ab_home = sandbox["root"] / "testy-ab"
    monkeypatch.setenv("TESTY_AB_HOME", str(ab_home))

    # Report with overflow timestamp (0001-01-01T00:00:00+01:00)
    rep_overflow = ab_home / "run_overflow" / "report.json"
    idea.atomic_json(rep_overflow, {
        "trial_id": "trial-overflow",
        "target_skill": "skill-overflow",
        "reported_at": "0001-01-01T00:00:00+01:00",
        "lesson": {
            "cue": "overflow cue",
            "lesson": "overflow lesson",
            "component": "comp-overflow",
        },
        "edits": [{"variant": "v_overflow", "verdict": "helped", "components": ["c"]}],
    })

    # Valid report within 60 days
    rep_valid = ab_home / "run_valid" / "report.json"
    idea.atomic_json(rep_valid, {
        "trial_id": "trial-valid",
        "target_skill": "skill-valid",
        "reported_at": idea.iso(sandbox["now"] - dt.timedelta(days=3)),
        "lesson": {
            "cue": "valid cue",
            "lesson": "valid lesson",
            "component": "comp-valid",
            "confidence": "high",
        },
        "edits": [{"variant": "v_valid", "verdict": "helped", "components": ["c"], "observed": {"score_delta": 0.5}}],
    })

    # Case A: mixed reports: valid is accepted, overflow is rejected as invalid
    out = sandbox["root"] / "evidence_ab_mixed.json"
    collected = idea.collect(out)
    assert collected["sources"]["ab-lesson"] == {"status": "available", "count": 2, "invalid": 1}
    ab_items = [e for e in collected["items"] if e["source_kind"] == "ab-lesson"]
    assert len(ab_items) == 2
    assert all(e["trial_id"] == "trial-valid" for e in ab_items)

    # Case B: only overflow report in directory -> source is unavailable, not crashing
    ab_home_overflow_only = sandbox["root"] / "testy-ab-overflow-only"
    monkeypatch.setenv("TESTY_AB_HOME", str(ab_home_overflow_only))
    rep_only = ab_home_overflow_only / "run1" / "report.json"
    idea.atomic_json(rep_only, {
        "trial_id": "trial-overflow",
        "target_skill": "skill-overflow",
        "reported_at": "0001-01-01T00:00:00+01:00",
        "lesson": {
            "cue": "cue",
            "lesson": "lesson",
            "component": "comp",
        },
    })
    out2 = sandbox["root"] / "evidence_ab_only.json"
    collected2 = idea.collect(out2)
    assert collected2["sources"]["ab-lesson"] == {"status": "unavailable", "count": 0, "invalid": 1}
    assert not any(e["source_kind"] == "ab-lesson" for e in collected2["items"])


def test_collect_execution_audit_record_schema_version_and_producer_rejection(sandbox, monkeypatch):
    fake_cli = sandbox["root"] / "fake_audit.py"
    idea.atomic_text(fake_cli, "# fake")
    monkeypatch.setenv("IDEA_MAKER_AUDIT_CLI", str(fake_cli))

    valid_rec_id = "a" * 64
    base_valid = {
        "schema_version": 1,
        "producer": "delivery-audit",
        "record_id": valid_rec_id,
        "audit_id": "b" * 64,
        "project_slug": "project-slug",
        "task_id": "task-01",
        "task_family": "feature",
        "harness": "cli",
        "observed_at": "2026-10-14T10:00:00Z",
        "input_sha256": "c" * 64,
        "classification": "systemic",
        "code": "FINDING",
        "component": "comp",
        "evidence_refs": ["ref-1"],
        "support": {"tasks": 1, "workspaces": 1, "actor_provider_pairs": 1},
        "verification": {
            "status": "CONFIRMED",
            "recorded_run_ids": ["run-001"],
            "maker_provider": "codex",
            "reviewer_provider": "claude",
        },
        "release_authority": False,
    }

    bad_schema_records = [
        {k: v for k, v in base_valid.items() if k != "schema_version"},
        dict(base_valid, schema_version=2),
        dict(base_valid, schema_version=0),
        dict(base_valid, schema_version="1"),
        dict(base_valid, schema_version=True),
        {k: v for k, v in base_valid.items() if k != "producer"},
        dict(base_valid, producer="foreign-producer"),
        dict(base_valid, producer=""),
        dict(base_valid, producer=123),
    ]

    def fake_records(argv, **kwargs):
        output_file = Path(argv[argv.index("--output") + 1])
        payload = {
            "schema_version": 1,
            "producer": "delivery-audit",
            "records": [base_valid] + bad_schema_records,
        }
        idea.atomic_json(output_file, payload)
        return SimpleNamespace(returncode=0, stdout=b"")

    monkeypatch.setattr(idea.subprocess, "run", fake_records)
    out = sandbox["root"] / "evidence_schema_check.json"
    collected = idea.collect(out)
    assert collected["sources"]["execution-audit"] == {"status": "available", "count": 1, "invalid": len(bad_schema_records)}
    audit_items = [e for e in collected["items"] if e["source_kind"] == "execution-audit"]
    assert len(audit_items) == 1
    assert audit_items[0]["record_id"] == valid_rec_id
    assert audit_items[0]["schema_version"] == 1
    assert audit_items[0]["producer"] == "delivery-audit"


def test_collect_execution_audit_deep_recursion_continues_and_unavailable(sandbox, monkeypatch):
    fake_cli = sandbox["root"] / "fake_audit.py"
    idea.atomic_text(fake_cli, "# fake")
    monkeypatch.setenv("IDEA_MAKER_AUDIT_CLI", str(fake_cli))

    idea.atomic_json(sandbox["helpdesk"] / "tickets" / "ticket1.json", {
        "id": "t-001", "component": "comp", "model": "codex", "status": "open", "symptom": "help me",
    })

    trigger = '{"__audit_recursion_trigger__": true}'
    real_loads = json.loads

    def mock_loads(s, *args, **kwargs):
        if trigger in s:
            raise RecursionError("maximum recursion depth exceeded while parsing audit JSON")
        return real_loads(s, *args, **kwargs)

    monkeypatch.setattr(idea.json, "loads", mock_loads)

    def fake_deep_audit(argv, **kwargs):
        output_file = Path(argv[argv.index("--output") + 1])
        idea.atomic_text(output_file, trigger)
        return SimpleNamespace(returncode=0, stdout=b"")

    monkeypatch.setattr(idea.subprocess, "run", fake_deep_audit)

    sample_file = sandbox["root"] / "sample_audit_recursion.json"
    idea.atomic_text(sample_file, trigger)
    with pytest.raises(idea.IdeaError) as exc_info:
        idea.read_json(sample_file)
    assert isinstance(exc_info.value.__cause__, RecursionError)

    out = sandbox["root"] / "evidence_audit_recursion.json"
    collected = idea.collect(out)

    assert collected["sources"]["execution-audit"] == {"status": "unavailable", "count": 0, "invalid": 1}
    assert not any(e["source_kind"] == "execution-audit" for e in collected["items"])
    assert any(e["source_kind"] == "helpdesk" and e.get("id") == "t-001" for e in collected["items"])
    assert out.is_file()


def test_collect_ab_lesson_deep_recursion_skipped_and_continues(sandbox, monkeypatch):
    ab_home = sandbox["root"] / "testy-ab"
    monkeypatch.setenv("TESTY_AB_HOME", str(ab_home))

    trigger = '{"__ab_recursion_trigger__": true}'
    bad_rep = ab_home / "bad_deep" / "report.json"
    idea.atomic_text(bad_rep, trigger)

    real_loads = json.loads

    def mock_loads(s, *args, **kwargs):
        if trigger in s:
            raise RecursionError("maximum recursion depth exceeded while parsing A/B report")
        return real_loads(s, *args, **kwargs)

    monkeypatch.setattr(idea.json, "loads", mock_loads)

    with pytest.raises(idea.IdeaError) as exc_info:
        idea.read_json(bad_rep)
    assert isinstance(exc_info.value.__cause__, RecursionError)

    good_rep = ab_home / "good_rep" / "report.json"
    idea.atomic_json(good_rep, {
        "trial_id": "trial-good",
        "target_skill": "skill-good",
        "reported_at": idea.iso(sandbox["now"] - dt.timedelta(days=1)),
        "lesson": {
            "cue": "good cue",
            "lesson": "good lesson",
            "component": "comp",
            "confidence": "high",
        },
        "edits": [{"variant": "v_good", "verdict": "helped", "components": ["comp"]}],
    })

    out = sandbox["root"] / "evidence_ab_recursion.json"
    collected = idea.collect(out)

    assert collected["sources"]["ab-lesson"]["status"] == "available"
    assert collected["sources"]["ab-lesson"]["invalid"] >= 1
    ab_items = [e for e in collected["items"] if e["source_kind"] == "ab-lesson"]
    assert len(ab_items) == 2
    assert all(e["trial_id"] == "trial-good" for e in ab_items)
    assert out.is_file()


def test_collect_ab_lesson_score_delta_rejects_non_finite_and_bool(sandbox, monkeypatch):
    ab_home = sandbox["root"] / "testy-ab"
    monkeypatch.setenv("TESTY_AB_HOME", str(ab_home))

    cases = [
        ("rep_inf", '{"score_delta": 1e309}'),
        ("rep_neginf", '{"score_delta": -1e309}'),
        ("rep_true", '{"score_delta": true}'),
        ("rep_false", '{"score_delta": false}'),
        ("rep_str", '{"score_delta": "0.5"}'),
        ("rep_valid_float", '{"score_delta": 0.75}'),
        ("rep_valid_int", '{"score_delta": -2}'),
        ("rep_valid_zero", '{"score_delta": 0}'),
    ]

    for name, obs_snippet in cases:
        rep = ab_home / name / "report.json"
        content = (
            '{\n'
            f'  "trial_id": "{name}",\n'
            '  "target_skill": "skill-target",\n'
            f'  "reported_at": "{idea.iso(sandbox["now"] - dt.timedelta(days=2))}",\n'
            '  "lesson": "NO_ENTRY",\n'
            '  "edits": [{\n'
            '    "variant": "v1",\n'
            '    "verdict": "helped",\n'
            '    "components": ["step-1"],\n'
            f'    "observed": {obs_snippet}\n'
            '  }]\n'
            '}\n'
        )
        idea.atomic_text(rep, content)

    out = sandbox["root"] / "evidence_score_delta.json"
    collected = idea.collect(out)

    assert collected["sources"]["ab-lesson"]["status"] == "available"
    edits = {e["trial_id"]: e for e in collected["items"] if e["source_kind"] == "ab-lesson" and e.get("kind") == "edit"}

    for bad_name in ("rep_inf", "rep_neginf", "rep_true", "rep_false", "rep_str"):
        assert "score_delta" not in edits[bad_name]["observed"], f"unexpected score_delta in {bad_name}"

    assert edits["rep_valid_float"]["observed"]["score_delta"] == 0.75
    assert edits["rep_valid_int"]["observed"]["score_delta"] == -2
    assert edits["rep_valid_zero"]["observed"]["score_delta"] == 0

    raw_text = out.read_text(encoding="utf-8")
    assert "Infinity" not in raw_text
    assert "NaN" not in raw_text
    parsed = json.loads(raw_text)
    assert isinstance(parsed, dict)
