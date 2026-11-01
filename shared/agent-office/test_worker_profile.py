"""Synthetic H02 profile and pre-claim tests; no native CLI is executed."""

from __future__ import annotations

import hashlib
import json
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from coordfs.cli_host import (HostExecutor, child_environment,
                              codex_subscription_probe)
from coordfs.errors import CoordError
from coordfs.worker_profile import HostWorkerProfile, load_worker_profiles
from test_cli_host import DISPATCH, Remote, enrollment


@pytest.fixture
def root() -> Path:
    path = Path(__file__).parent / "host-test-data" / uuid.uuid4().hex
    path.mkdir(parents=True)
    return path


def row(item, **changes):
    return {"binding_id": item.binding_id, "driver": item.driver,
            "model": "gpt-6-luna" if item.driver == "codex-cli" else "claude-sonnet-5",
            "permission": "read-only", "reasoning": "medium", "auth": "subscription",
            **changes}


def profile(item, **changes):
    return HostWorkerProfile(**row(item, **changes))


def host(root, item, remote, *, profiles=None, config=None, auth_probe=None,
         observed_model=None, runner=None):
    return HostExecutor([item], journal_home=root / "journal", remote=remote,
        idle_probe=lambda _: True, runner=runner or (lambda **_: {"status": "accepted"}),
        profiles=profiles if profiles is not None else {item.binding_id: profile(item)},
        codex_config=config or root / "missing-config.toml",
        auth_probe=auth_probe or (lambda *_: True), observed_model=observed_model)


def write_profiles(root, rows, *, raw=None):
    path = root / "profiles.json"
    path.write_text(raw if raw is not None else json.dumps({"schema_version": 1,
                          "profiles": rows}), encoding="utf-8")
    return path


@pytest.mark.parametrize("reasoning", ["low", "medium", "high"])
@pytest.mark.parametrize("permission", ["read-only", "workspace-write"])
def test_exact_codex_argv_for_every_supported_combination(root, reasoning, permission):
    item = enrollment(root)
    pinned = profile(item, reasoning=reasoning, permission=permission)
    argv = HostExecutor._argv(item, pinned)
    assert argv == [str(item.executable), "exec", "resume", "--skip-git-repo-check",
        "--strict-config", "-m", "gpt-6-luna", "-c",
        f'model_reasoning_effort="{reasoning}"', "-c",
        f'sandbox_mode="{permission}"', "-c", 'approval_policy="never"',
        item.native_thread_id, "-"]
    assert not any(flag in argv for flag in ("--fallback-model", "--ignore-user-config",
        "--mcp-config", "--settings", "-s"))
    assert not any(value.startswith("--dangerously-") for value in argv)


def test_child_environment_is_case_insensitive_and_closed():
    allowed = {key.lower(): f"value-{key}" for key in (
        "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "PATH", "USERPROFILE",
        "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
        "TEMP", "TMP", "USERNAME", "OS", "PROCESSOR_ARCHITECTURE",
        "NUMBER_OF_PROCESSORS")}
    source = {**allowed, "OPENAI_API_KEY": "secret", "ANTHROPIC_API_KEY": "secret",
              "ANTHROPIC_BASE_URL": "foreign", "CLAUDE_CODE_USE_BEDROCK": "1",
              "HTTPS_PROXY": "foreign"}
    child = child_environment(source)
    assert set(child) == {key.upper() for key in allowed}
    assert set(child).isdisjoint({"OPENAI_API_KEY", "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "HTTPS_PROXY"})


def test_loader_accepts_exact_document_and_fingerprint_is_canonical(root):
    item = enrollment(root)
    first = row(item)
    loaded = load_worker_profiles(write_profiles(root, [first]), [item])[item.binding_id]
    canonical = json.dumps(first, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False).encode("utf-8")
    assert loaded.profile_fingerprint == hashlib.sha256(canonical).hexdigest()
    reordered = dict(reversed(list(first.items())))
    assert load_worker_profiles(write_profiles(root, [reordered]), [item])[
        item.binding_id].profile_fingerprint == loaded.profile_fingerprint
    assert profile(item, reasoning="high").profile_fingerprint != loaded.profile_fingerprint


@pytest.mark.parametrize("rows,document", [
    ([], None),
    ([{"binding_id": "foreign"}], None),
    ([{"model": "gpt-6-unknown"}], None),
    ([{"driver": "claude-code-cli"}], None),
    ([{"permission": "plan"}], None),
    ([{"reasoning": "ultra"}], None),
    ([{"auth": "api-key"}], None),
    ([{"extra": "bad"}], None),
    (None, {"schema_version": 2, "profiles": []}),
    (None, {"schema_version": 1, "profiles": [], "extra": 1}),
])
def test_loader_rejects_invalid_rows_and_document(root, rows, document):
    item = enrollment(root)
    data = ([{**row(item), **change} for change in rows] if rows is not None else None)
    path = root / "profiles.json"
    path.write_text(json.dumps(document if document is not None else
                               {"schema_version": 1, "profiles": data}), encoding="utf-8")
    with pytest.raises(CoordError):
        load_worker_profiles(path, [item])


def test_loader_rejects_duplicates_oversize_missing_and_nonregular(root):
    item = enrollment(root)
    one = row(item)
    for rows in ([one, one], [one] * 21):
        with pytest.raises(CoordError):
            load_worker_profiles(write_profiles(root, rows), [item])
    with pytest.raises(CoordError):
        load_worker_profiles(write_profiles(root, [], raw=
            '{"schema_version":1,"profiles":[],"profiles":[]}'), [item])
    with pytest.raises(CoordError):
        load_worker_profiles(write_profiles(root, [], raw=" " * (16 * 1024 + 1)), [item])
    with pytest.raises(CoordError):
        load_worker_profiles(root, [item])
    with pytest.raises(CoordError):
        load_worker_profiles(root / "missing.json", [item])


def test_loader_rejects_symlink_and_reparse_profile(root, monkeypatch):
    item = enrollment(root)
    real = write_profiles(root, [row(item)])
    link = root / "profile-link.json"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError) as exc:
        unittest.TestCase().skipTest(f"symlink unavailable: {exc}")
    else:
        with pytest.raises(CoordError):
            load_worker_profiles(link, [item])
    original = Path.lstat
    def flagged(path):
        info = original(path)
        if path == real:
            from types import SimpleNamespace
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info
    with monkeypatch.context() as patch:
        patch.setattr(Path, "lstat", flagged)
        with pytest.raises(CoordError):
            load_worker_profiles(real, [item])


def test_claude_profile_refuses_with_explicit_permission_code(root):
    item = enrollment(root, driver="claude-code-cli")
    with pytest.raises(CoordError) as failure:
        load_worker_profiles(write_profiles(root, [row(item)]), [item])
    assert failure.value.code == "permission_not_enforceable"
    remote = Remote(item)
    assert host(root, item, remote, profiles={item.binding_id: profile(item)}).run_once() == [
        {"dispatch_id": DISPATCH, "status": "permission_not_enforceable"}]
    assert remote.claim_calls == []


@pytest.mark.parametrize("case,expected", [
    ("missing", "profile_missing"),
    ("wrong_binding", "profile_mismatched"),
    ("provider", "provider_config_refused"),
    ("nested_provider", "provider_config_refused"),
    ("provider_table", "provider_config_refused"),
    ("malformed", "provider_config_refused"),
    ("unreadable", "provider_config_refused"),
    ("auth", "subscription_auth_refused"),
])
def test_preflight_refuses_before_any_claim(root, case, expected):
    item = enrollment(root)
    remote = Remote(item)
    config = root / "config.toml"
    profiles = {item.binding_id: profile(item)}
    if case == "missing":
        profiles = {}
    elif case == "wrong_binding":
        profiles = {item.binding_id: profile(item, binding_id="other")}
    elif case == "provider":
        config.write_text('model_provider = "other"\n', encoding="utf-8")
    elif case == "nested_provider":
        config.write_text('[profiles.owner]\nmodel_provider = "other"\n', encoding="utf-8")
    elif case == "provider_table":
        config.write_text('[model_providers]\n', encoding="utf-8")
    elif case == "malformed":
        config.write_text('model_provider = [\n', encoding="utf-8")
    elif case == "unreadable":
        config.mkdir()
    executor = host(root, item, remote, profiles=profiles, config=config,
                    auth_probe=(lambda *_: case != "auth"))
    assert executor.run_once() == [{"dispatch_id": DISPATCH, "status": expected}]
    assert remote.claim_calls == []
    assert list((root / "journal" / "dispatches").iterdir()) == []


def test_preflight_accepts_explicit_openai_subscription_provider(root):
    item = enrollment(root)
    remote = Remote(item)
    config = root / "config.toml"
    config.write_text('model_provider = "openai"\n', encoding="utf-8")
    assert host(root, item, remote, config=config).run_once() == [
        {"dispatch_id": DISPATCH, "status": "accepted"}]
    assert len(remote.claim_calls) == 1


def test_subscription_probe_requires_exact_login_status_line(root, monkeypatch):
    import coordfs.cli_host as cli_host
    item = enrollment(root)
    calls = []
    class Result:
        returncode = 0
        stdout = "Logged in using ChatGPT\n"
        stderr = ""
    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return Result()
    monkeypatch.setattr(cli_host.subprocess, "run", fake_run)
    assert codex_subscription_probe(item, {"PATH": "synthetic"})
    assert calls[0][0] == [str(item.executable), "login", "status"]
    assert calls[0][1]["timeout"] == 20 and calls[0][1]["env"] == {"PATH": "synthetic"}
    Result.stdout = "Logged in using API key\n"
    assert not codex_subscription_probe(item, {})


def test_started_intent_contains_only_fingerprint_and_safe_argv_shape(root, monkeypatch):
    item = enrollment(root)
    remote = Remote(item)
    pinned = profile(item)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-secret")
    calls = []
    executor = host(root, item, remote, profiles={item.binding_id: pinned},
                    runner=lambda **kwargs: calls.append(kwargs) or {"status": "accepted"})
    assert executor.run_once()[0]["status"] == "accepted"
    assert len(calls) == 1 and "OPENAI_API_KEY" not in calls[0]["env"]
    assert set(calls[0]["env"]).issubset({"SYSTEMROOT", "WINDIR", "COMSPEC",
        "PATHEXT", "PATH", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA",
        "LOCALAPPDATA", "PROGRAMDATA", "TEMP", "TMP", "USERNAME", "OS",
        "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS"})
    journal, _ = executor._paths(DISPATCH, item)
    events = executor._events(journal)
    intent = next(event for event in events if event["phase"] == "started_intent")
    assert intent["profile_fingerprint"] == pinned.profile_fingerprint
    assert intent["argv_shape"] == HostExecutor._argv(item, pinned)[3:-2]
    rendered = json.dumps(intent)
    for secret in (item.native_thread_id, str(item.executable), str(item.workspace),
                   "[Office Wake]", DISPATCH):
        assert secret not in rendered
    assert "profile_unverified" in [event["phase"] for event in events]


def test_observed_model_verifies_or_quarantines_until_fingerprint_changes(root):
    item = enrollment(root)
    remote = Remote(item)
    pinned = profile(item)
    executor = host(root, item, remote, profiles={item.binding_id: pinned},
                    observed_model=lambda _: "gpt-6-sol")
    assert executor.run_once()[0]["status"] == "accepted"
    journal, _ = executor._paths(DISPATCH, item)
    assert "profile_mismatch" in [event["phase"] for event in executor._events(journal)]
    assert executor._quarantine_path(pinned).is_file()
    remote.row["dispatch_id"] = "c" * 32
    assert executor.run_once()[0]["status"] == "profile_quarantined"
    assert len(remote.claim_calls) == 1
    changed = profile(item, model="gpt-6-sol")
    replacement = host(root, item, remote, profiles={item.binding_id: changed},
                       observed_model=lambda _: "gpt-6-sol")
    assert replacement.run_once()[0]["status"] == "accepted"
    assert len(remote.claim_calls) == 2
    second_journal, _ = replacement._paths("c" * 32, item)
    assert "profile_verified" in [event["phase"] for event in replacement._events(second_journal)]


def test_quarantine_retries_existing_receipt_but_defers_new_claim(root):
    item = enrollment(root)
    remote = Remote(item)
    remote.receipt_error = TimeoutError("lost receipt")
    calls = []
    executor = host(root, item, remote, observed_model=lambda _: "gpt-6-sol",
                    runner=lambda **kwargs: calls.append(kwargs) or {"status": "accepted"})
    assert executor.run_once() == [{"dispatch_id": DISPATCH, "status": "receipt_unknown"}]
    assert executor._quarantine_path(profile(item)).is_file()
    assert len(remote.receipt_calls) == 1

    remote.receipt_error = None
    resumed = host(root, item, remote, observed_model=lambda _: "gpt-6-sol",
                   runner=lambda **_: pytest.fail("runner called twice"))
    assert resumed.run_once() == [{"dispatch_id": DISPATCH, "status": "accepted"}]
    assert len(remote.receipt_calls) == 2
    # The recorded dispatch is never claimed, started or run again; the label may be
    # either the recovery status or the quarantine deferral (both launch nothing).
    third = resumed.run_once()
    assert [row["dispatch_id"] for row in third] == [DISPATCH]
    assert third[0]["status"] in {"already_recorded", "profile_quarantined"}
    assert len(remote.receipt_calls) == 2
    assert len(remote.claim_calls) == len(remote.start_calls) == len(calls) == 1

    remote.row["dispatch_id"] = "c" * 32
    assert resumed.run_once() == [{"dispatch_id": "c" * 32,
                                   "status": "profile_quarantined"}]
    assert len(remote.claim_calls) == len(remote.start_calls) == len(calls) == 1


def test_two_executors_on_one_dispatch_launch_at_most_once(root):
    item = enrollment(root)
    remote = Remote(item)
    entered = threading.Event()
    release = threading.Event()
    calls = []
    def runner(**kwargs):
        calls.append(kwargs)
        entered.set()
        assert release.wait(5)
        return {"status": "accepted"}
    first = host(root, item, remote, runner=runner)
    second = host(root, item, remote, runner=runner)
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(first.run_once)
        assert entered.wait(5)
        assert second.run_once()[0]["status"] == "session_busy"
        release.set()
        assert future.result()[0]["status"] == "accepted"
    assert len(calls) == len(remote.claim_calls) == 1
