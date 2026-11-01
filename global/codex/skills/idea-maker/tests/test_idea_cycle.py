import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import idea
import idea_cycle as cycle


def cfg(sandbox, **override):
    data = {"workspace": str(sandbox["root"]), "usage_check": str(sandbox["root"] / "no-usage.py")}
    data.update(override)
    idea.atomic_json(sandbox["home"] / "config.json", data)
    return idea.config()


def active(sandbox, age_minutes=1):
    state = idea.load_state()
    state["last_started_at"] = idea.iso(sandbox["now"] - dt.timedelta(minutes=age_minutes))
    state["active"] = {"run_id": "run-1", "pid": 123, "created": 7, "started_at": state["last_started_at"]}
    idea.save_state(state)
    return state


def test_stop_and_disabled_config_block_launch(sandbox):
    cfg(sandbox)
    idea.atomic_text(sandbox["home"] / "STOP", "stop")
    assert cycle.run()["result"] == "stopped"
    assert not (sandbox["home"] / "runs").exists()


def test_enabled_false_blocks_launch(sandbox):
    cfg(sandbox, enabled=False)
    assert cycle.run()["result"] == "stopped"


@pytest.mark.parametrize("data", ["not json", "[]", '{"enabled":"yes"}', '{"max_minutes":true}',
                                  '{"ideators":[]}', '{"extra_sources":"file"}',
                                  '{"workspace":true}', '{"workspace":42}', '{"workspace":" "}',
                                  '{"ideators":[{"name":"bad","argv":["fake"]}]}'])
def test_invalid_config_fails_closed(sandbox, data):
    idea.atomic_text(sandbox["home"] / "config.json", data)
    assert cycle.run()["result"] == "config-error"
    assert not (sandbox["home"] / "runs").exists()


def test_busy_lock_exits_without_launch(sandbox):
    cfg(sandbox)
    with idea.lock("cycle.lock"):
        assert cycle.run() == {"result": "busy"}
    assert not (sandbox["home"] / "state.json").exists()


def test_not_due_does_not_check_billing_or_launch(sandbox, monkeypatch):
    cfg(sandbox)
    state = idea.load_state()
    state["last_completed_at"] = idea.iso(sandbox["now"])
    idea.save_state(state)
    before = (sandbox["home"] / "state.json").read_bytes()
    monkeypatch.setattr(cycle, "billing_mode", lambda _: pytest.fail("billing on not-due cycle"))
    assert cycle.run()["result"] == "idle"
    assert (sandbox["home"] / "state.json").read_bytes() == before


@pytest.mark.parametrize("mode", ["kredytowy", "oszczedny", "unknown"])
def test_non_normal_billing_skips_paid_ideator(sandbox, mode):
    configuration = cfg(sandbox)
    state = idea.load_state()
    state["rotation"] = 1  # Claude would otherwise be next.
    assert cycle.pick_ideator(configuration, state, mode)["agent"] == "codex"
    configuration["ideators"] = [configuration["ideators"][1]]
    assert cycle.pick_ideator(configuration, state, mode) is None


def test_normal_billing_allows_rotation_to_claude(sandbox):
    configuration = cfg(sandbox)
    state = idea.load_state()
    assert cycle.pick_ideator(configuration, state, "normalny")["agent"] == "codex"
    assert cycle.pick_ideator(configuration, state, "normalny")["agent"] == "claude"


def test_billing_reads_tryb_with_hidden_subprocess(sandbox, monkeypatch):
    usage = sandbox["root"] / "usage.py"
    idea.atomic_text(usage, "# fake")
    configuration = cfg(sandbox, usage_check=str(usage))
    calls = []

    def check(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=b'{"tryb":"kredytowy"}')

    monkeypatch.setattr(idea.subprocess, "run", check)
    assert cycle.billing_mode(configuration) == "kredytowy"
    assert calls[0][0][-1] == "--json"
    assert calls[0][1]["creationflags"] == idea.flags()
    assert calls[0][1]["env"]["PYTHONIOENCODING"] == "utf-8"
    assert calls[0][1]["env"]["PYTHONUTF8"] == "1"


@pytest.mark.parametrize("index,flag", [(0, "-C"), (1, "--add-dir")])
def test_dry_run_default_workspace_is_own_run_dir(sandbox, index, flag):
    configuration = idea.config()
    state = idea.load_state()
    report = cycle.launch(configuration["ideators"][index], configuration, state, dry_run=True)
    run_dir = sandbox["home"] / "runs" / report["run_id"]
    assert Path(report["argv"][report["argv"].index(flag) + 1]) == run_dir
    assert Path(report["cwd"]) == run_dir
    assert not run_dir.exists() and not (sandbox["home"] / "state.json").exists()


def test_launch_default_cwd_and_prompt_use_absolute_tools(sandbox, monkeypatch):
    configuration = idea.config()
    calls = []

    def popen(argv, **kwargs):
        prompt = kwargs["stdin"].read().decode("utf-8")
        calls.append((argv, kwargs, prompt))
        return SimpleNamespace(pid=4321)

    monkeypatch.setattr(idea.subprocess, "Popen", popen)
    monkeypatch.setattr(idea, "probe", lambda _: ("alive", 77))
    report = cycle.launch(configuration["ideators"][0], configuration, idea.load_state(), dry_run=False)
    run_dir = sandbox["home"] / "runs" / report["run_id"]
    argv, kwargs, prompt = calls[0]
    assert Path(kwargs["cwd"]) == run_dir
    assert Path(argv[argv.index("-C") + 1]) == run_dir
    assert str((idea.SKILL_ROOT / "scripts" / "idea.py").resolve()) in prompt
    assert str((idea.SKILL_ROOT.parent / "agent-graph" / "scripts" / "todo_fleet.py").resolve()) in prompt
    assert str(run_dir) in prompt
    assert kwargs["env"]["PYTHONIOENCODING"] == "utf-8" and kwargs["env"]["PYTHONUTF8"] == "1"


def test_explicit_workspace_override_is_preserved(sandbox):
    configuration = cfg(sandbox)
    report = cycle.launch(configuration["ideators"][0], configuration, idea.load_state(), dry_run=True)
    assert Path(report["cwd"]) == sandbox["root"]
    assert Path(report["argv"][report["argv"].index("-C") + 1]) == sandbox["root"]


def test_launch_once_hidden_stdin_files_and_journal(sandbox, monkeypatch, source_snapshot):
    before = source_snapshot()
    cfg(sandbox)
    calls = []

    def popen(argv, **kwargs):
        # Pre-launch state must already contain a recoverable journal.
        assert idea.load_state()["active"]["pid"] is None
        assert "collect --out" in kwargs["stdin"].read().decode()
        calls.append((argv, kwargs))
        return SimpleNamespace(pid=4321)

    monkeypatch.setattr(idea.subprocess, "Popen", popen)
    monkeypatch.setattr(idea, "probe", lambda _: ("alive", 77))
    report = cycle.run()
    assert report["result"] == "launched" and len(calls) == 1
    record = idea.load_state()["active"]
    assert record["pid"] == 4321 and record["created"] == 77
    assert idea.load_state()["last_completed_at"] is None
    assert idea.load_state()["last_started_at"] == record["started_at"]
    assert calls[0][1]["creationflags"] == idea.flags(group=True)
    if idea.os.name == "nt":
        assert calls[0][1]["creationflags"] & idea.CREATE_NO_WINDOW
        assert calls[0][1]["creationflags"] & idea.CREATE_NEW_PROCESS_GROUP
    directory = sandbox["home"] / "runs" / record["run_id"]
    assert json.loads((directory / "launch.json").read_text())["pid"] == 4321
    assert (directory / "stdout.txt").exists() and (directory / "stderr.txt").exists()
    assert calls[0][1]["env"]["IDEA_MAKER_RUN_ID"] == record["run_id"]
    assert cycle.run()["result"] == "idle" and len(calls) == 1
    assert len((sandbox["home"] / "cycles.jsonl").read_text().splitlines()) == 2
    assert source_snapshot() == before


def test_completion_marks_clock_and_no_next_launch(sandbox):
    cfg(sandbox)
    active(sandbox)
    idea.atomic_json(sandbox["home"] / "runs" / "run-1" / "summary.json", {"status": "completed", "candidate_ids": []})
    assert cycle.run()["result"] == "idle"
    state = idea.load_state()
    assert state["active"] is None
    assert state["last_completed_at"] == idea.iso(sandbox["now"])
    assert state["history"][-1]["status"] == "COMPLETED"
    assert not idea.due()["due"]


@pytest.mark.parametrize("summary", [None, "corrupt", '{"status":"failed"}'])
def test_dead_run_without_valid_summary_fails_and_backs_off(sandbox, summary):
    cfg(sandbox)
    old = idea.iso(sandbox["now"] - dt.timedelta(days=30))
    state = active(sandbox)
    state["last_completed_at"] = old
    idea.save_state(state)
    if summary is not None:
        idea.atomic_text(sandbox["home"] / "runs" / "run-1" / "summary.json", summary)
    report = cycle.run()
    assert report["due"]["reason"] == "failure-backoff"
    assert idea.load_state()["last_completed_at"] == old
    sandbox["now"] += dt.timedelta(hours=24)
    assert idea.due()["due"]


def test_launch_failure_is_retained_and_retried_after_24h(sandbox, monkeypatch):
    cfg(sandbox)

    def fail(*args, **kwargs):
        raise OSError("fake launch failure")

    monkeypatch.setattr(idea.subprocess, "Popen", fail)
    assert cycle.run()["result"] == "launch-failed"
    state = idea.load_state()
    assert state["active"] is None and state["history"][-1]["reason"] == "launch-failed"
    assert cycle.run()["due"]["reason"] == "failure-backoff"


def test_timeout_kills_only_verified_tree_and_records_failure(sandbox, monkeypatch):
    cfg(sandbox, max_minutes=90)
    active(sandbox, 91)
    identities = iter(["alive", "ended"])
    monkeypatch.setattr(idea, "identity", lambda _: next(identities))
    killed = []
    monkeypatch.setattr(cycle, "terminate_tree", lambda pid: killed.append(pid) or True)
    assert cycle.run()["due"]["reason"] == "failure-backoff"
    assert killed == [123]
    assert idea.load_state()["history"][-1]["reason"] == "timeout"


def test_unknown_identity_never_killed_and_blocks_launch(sandbox, monkeypatch):
    cfg(sandbox)
    active(sandbox, 91)
    monkeypatch.setattr(idea, "identity", lambda _: "unknown")
    monkeypatch.setattr(cycle, "terminate_tree", lambda _: pytest.fail("unverified pid was killed"))
    assert cycle.run()["due"]["reason"] == "active-run"


def test_successful_taskkill_without_proof_of_exit_keeps_slot(sandbox, monkeypatch):
    cfg(sandbox)
    active(sandbox, 91)
    monkeypatch.setattr(idea, "identity", lambda _: "alive")
    monkeypatch.setattr(cycle, "terminate_tree", lambda _: True)
    report = cycle.run()
    assert report["due"]["reason"] == "active-run"
    assert report["events"][0]["event"] == "stop-failed"
    assert idea.load_state()["active"]["pid"] == 123


def test_bad_argv_template_blocks_before_launch(sandbox):
    configuration = cfg(sandbox)
    configuration["ideators"][0]["argv"] = ["fake", "{0}"]
    idea.atomic_json(sandbox["home"] / "config.json", configuration)
    assert cycle.run()["result"] == "config-error"


def test_reused_pid_reconciles_without_killing_other_process(sandbox, monkeypatch):
    cfg(sandbox)
    active(sandbox, 91)
    monkeypatch.setattr(idea, "probe", lambda _: ("alive", 99))
    monkeypatch.setattr(cycle, "terminate_tree", lambda _: pytest.fail("reused pid killed"))
    assert cycle.run()["due"]["reason"] == "failure-backoff"


def test_dry_run_and_status_are_read_only(sandbox, capsys):
    cfg(sandbox)
    files = {p: p.read_bytes() for p in sandbox["home"].rglob("*") if p.is_file()}
    assert cycle.main(["dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["result"] == "dry-run"
    assert cycle.main(["status"]) == 0
    assert json.loads(capsys.readouterr().out)["due"]["due"]
    assert {p: p.read_bytes() for p in sandbox["home"].rglob("*") if p.is_file()} == files


def test_corrupt_state_is_not_overwritten(sandbox):
    cfg(sandbox)
    path = sandbox["home"] / "state.json"
    idea.atomic_text(path, "not JSON")
    assert cycle.run()["result"] == "state-error"
    assert path.read_text() == "not JSON"


def test_stop_arriving_before_popen_wins(sandbox, monkeypatch):
    cfg(sandbox)
    original = cycle.ideator_prompt

    def stop(*args, **kwargs):
        idea.atomic_text(sandbox["home"] / "STOP", "stop")
        return original(*args, **kwargs)

    monkeypatch.setattr(cycle, "ideator_prompt", stop)
    assert cycle.run()["result"] == "stopped"
    assert idea.load_state()["active"] is None


def test_prompt_uses_bounded_fixed_flow(sandbox):
    configuration = cfg(sandbox)
    text = cycle.ideator_prompt(configuration["ideators"][0], "run-1", sandbox["root"] / "run", configuration)
    for marker in ("collect", "0-3", "extend", "todo_fleet.py", "advisory", "anti-bureaucracy",
                   "--evidence", "--run-id", "announce", "summary.json", "STOP", "nie deployuj", "nie kasuj"):
        assert marker in text


def test_schedule_plan_precedes_all_scheduler_mutations():
    text = (idea.SKILL_ROOT / "scripts" / "install_schedule.ps1").read_text(encoding="utf-8")
    guard = text.index("if (-not $Apply)")
    end = text.index("foreach ($required", guard)
    assert "return" in text[guard:end]
    assert text.index("Get-ScheduledTask") > end
    assert "New-ScheduledTaskTrigger -Daily" in text and '$At = "09:15"' in text
    assert "-Disable -MultipleInstances IgnoreNew -StartWhenAvailable" in text
    assert "-Minutes 20" in text and "$ProtectScript" in text
    assert text.index("if (-not $wrapped)") < text.index("Enable-ScheduledTask")
