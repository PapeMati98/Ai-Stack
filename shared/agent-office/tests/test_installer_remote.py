"""Remote enrollment must be explicit, pinned, and survive an ordinary upgrade."""
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import install_agent_office as installer


REMOTE = {"host": "office.example.test", "user": "agentoffice",
          "identity_file": "C:/owned/key", "known_hosts_file": "C:/owned/known_hosts",
          "ssh_executable": "C:/Windows/System32/OpenSSH/ssh.exe"}


def package():
    return SimpleNamespace(fingerprint="a" * 64, runtime_fingerprint="b" * 64,
                           skill_fingerprint="c" * 64, runtime_files={}, skill_files={})


def test_remote_registrations_and_wrapper_have_one_remote_store():
    runtime = Path("C:/owned/runtime")
    for builder in (installer.claude_server, installer.codex_server):
        args = builder(Path("C:/python.exe"), runtime, Path("C:/old/store"),
                       remote_config=REMOTE)["args"]
        assert "--remote-config" in args
        assert args[args.index("--remote-config") + 1] == str(runtime / installer.REMOTE_CONFIG_FILE)
        assert "--home" not in args
    wrapper = installer.wrapper_bytes(Path("C:/python.exe"), runtime, Path("C:/old/store"),
                                      remote_config=REMOTE).decode()
    assert "--remote-config" in wrapper
    assert "--runtime-home" not in wrapper
    assert "old/store" not in wrapper.replace("\\", "/")


def test_remote_payload_pins_connection_and_does_not_copy_keys():
    payload = installer.runtime_payload(package(), "d" * 32, Path("C:/python.exe"),
                                        Path("C:/runtime"), Path("C:/coord"), remote_config=REMOTE)
    expected = installer.json_bytes(installer.normalize_remote_config(REMOTE))
    assert payload[installer.REMOTE_CONFIG_FILE] == expected
    marker = json.loads(payload[installer.RUNTIME_MARKER])
    assert marker["remote_config_sha256"] == installer.sha256_bytes(expected)
    assert set(payload) == {installer.REMOTE_CONFIG_FILE, installer.RUNTIME_MARKER, "office.cmd"}


@pytest.mark.parametrize("changes", [{"token": "not-a-config-field"}, {"port": True},
                                    {"host": "-oProxyCommand=x"}, {"request_timeout_s": float("nan")}])
def test_remote_configuration_is_closed(changes):
    with pytest.raises(installer.DeployError):
        installer.normalize_remote_config({**REMOTE, **changes})


def test_receipt_null_is_not_implicit_local_fallback():
    with pytest.raises(installer.DeployError):
        installer.receipt_remote_config({"remote_config": None})


def test_local_defaults_remain_byte_compatible():
    server = installer.claude_server(Path("C:/python.exe"), Path("C:/runtime"), Path("C:/coord"))
    assert "--home" in server["args"]
    assert "--remote-config" not in server["args"]
    assert "remote_config_sha256" not in installer.runtime_marker(package(), "d" * 32, Path("C:/python.exe"))


def test_upgrade_inherits_remote_route_unless_explicitly_replaced(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    p = package()
    definitions = installer.expected_target_defs(home)
    old = {"remote_config": REMOTE, "targets": [
        {"id": row["id"], "after": installer.snapshot(Path(row["path"]))} for row in definitions]}
    specs = installer.build_upgrade_specs(p, home, Path("C:/python.exe"), "d" * 32, old)
    runtime = next(row["desired"] for row in specs if row["id"] == "runtime")
    assert installer.REMOTE_CONFIG_FILE in runtime
    for identifier in ("claude-mcp", "codex-mcp"):
        assert b"--remote-config" in next(row["desired"] for row in specs if row["id"] == identifier)
    changed = {**REMOTE, "port": 2222}
    specs = installer.build_upgrade_specs(p, home, Path("C:/python.exe"), "e" * 32, old, remote_config=changed)
    runtime = next(row["desired"] for row in specs if row["id"] == "runtime")
    assert runtime[installer.REMOTE_CONFIG_FILE] == installer.json_bytes(installer.normalize_remote_config(changed))


@pytest.fixture
def enrollment(tmp_path, monkeypatch):
    home = tmp_path / "Synthetic Remote Client"
    home.mkdir()
    python = Path(sys.executable).resolve()
    monkeypatch.setattr(installer, "pin_python", lambda candidate=None: python)
    state = home / installer.DEFAULT_STATE_RELATIVE
    return home, state, python


def test_remote_install_verify_upgrade_rollback_recover(enrollment):
    home, state, python = enrollment
    durable = home / installer.COORD_RELATIVE / "owner-evidence.json"
    durable.parent.mkdir(parents=True)
    durable.write_bytes(b"original local store is never touched")
    before = {p.relative_to(home).as_posix(): p.read_bytes() for p in home.rglob("*") if p.is_file()}
    preview = installer.dry_run(home, python_executable=python, remote_config=REMOTE)
    assert preview["deployment_mode"] == "remote"
    assert preview["writes_performed"] == 0
    assert before == {p.relative_to(home).as_posix(): p.read_bytes() for p in home.rglob("*") if p.is_file()}
    first = installer.install(home, python_executable=python, remote_config=REMOTE)
    assert first["status"] == "DEPLOYED"
    assert installer.verify_install(home, python_executable=python)["deployment_mode"] == "remote"
    connection = home / installer.RUNTIME_RELATIVE / installer.REMOTE_CONFIG_FILE
    pinned_bytes = connection.read_bytes()
    second = installer.upgrade(home, None, state, first["transaction_id"], python, no_delete=True)
    assert connection.read_bytes() == pinned_bytes
    assert second["remote_config"] == installer.normalize_remote_config(REMOTE)
    assert installer.verify_install(home, python_executable=python)["status"] == "CURRENT"
    assert installer.rollback(home, None, state, second["transaction_id"])["status"] == "ROLLED_BACK"
    assert connection.read_bytes() == pinned_bytes
    assert installer.verify_install(home, python_executable=python)["status"] == "CURRENT"
    assert installer.recover(home, None, state, second["transaction_id"], python)["status"] == "DEPLOYED"
    assert installer.verify_install(home, python_executable=python)["deployment_mode"] == "remote"
    assert durable.read_bytes() == before[durable.relative_to(home).as_posix()]


def test_local_to_remote_requires_explicit_transaction_and_rolls_back(enrollment):
    home, state, python = enrollment
    local = installer.install(home, python_executable=python)
    with pytest.raises(installer.DeployError, match="explicit upgrade"):
        installer.install(home, python_executable=python, remote_config=REMOTE)
    before = {p.relative_to(home).as_posix(): p.read_bytes() for p in home.rglob("*") if p.is_file()}
    preview = installer.upgrade(home, None, state, local["transaction_id"], python,
                                no_delete=True, preview=True, remote_config=REMOTE)
    assert preview["deployment_mode"] == "remote"
    assert before == {p.relative_to(home).as_posix(): p.read_bytes() for p in home.rglob("*") if p.is_file()}
    remote = installer.upgrade(home, None, state, local["transaction_id"], python,
                               no_delete=True, remote_config=REMOTE)
    assert installer.verify_install(home, python_executable=python)["deployment_mode"] == "remote"
    assert installer.rollback(home, None, state, remote["transaction_id"])["status"] == "ROLLED_BACK"
    assert installer.verify_install(home, python_executable=python)["deployment_mode"] == "local"


def test_connection_drift_is_refused_without_fallback(enrollment):
    home, state, python = enrollment
    receipt = installer.install(home, python_executable=python, remote_config=REMOTE)
    connection = home / installer.RUNTIME_RELATIVE / installer.REMOTE_CONFIG_FILE
    connection.write_bytes(installer.json_bytes({**REMOTE, "port": 2222}))
    assert installer.verify_install(home, python_executable=python)["status"] == "DRIFT"
    with pytest.raises(installer.DeployError):
        installer.upgrade(home, None, state, receipt["transaction_id"], python, no_delete=True)


def test_remote_cli_flag_is_not_an_authority_for_verify(capsys):
    assert installer.main(["--remote-config", str(Path("C:/not-read.json")), "verify"]) == 1
    assert "only valid" in capsys.readouterr().out


def test_receipt_verified_patch_preserves_other_servers_and_refuses_unknown_owner():
    runtime = Path("C:/runtime")
    python = Path("C:/python.exe")
    store = Path("C:/coord")
    old = installer.claude_server(python, runtime, store)
    new = installer.claude_server(python, runtime, store, remote_config=REMOTE)
    raw = b'{"foreign": [1, 2], "mcpServers": {"other": {"command":"other"}}}\r\n'
    local = installer.patch_claude_mcp(raw, old)
    remote = installer.patch_claude_mcp(local, new, owned_previous=old)
    parsed = json.loads(remote)
    assert parsed["foreign"] == [1, 2]
    assert parsed["mcpServers"]["other"] == {"command": "other"}
    with pytest.raises(installer.DeployError):
        installer.patch_claude_mcp(local, new, owned_previous={"command": "unverified"})
    old = installer.codex_server(python, runtime, store)
    new = installer.codex_server(python, runtime, store, remote_config=REMOTE)
    foreign = b'model = "configured-model"\r\n[mcp_servers.other]\r\ncommand = "other"\r\n'
    local = installer.patch_codex_mcp(foreign, old) + b'\r\n[foreign_section]\r\nkeep = true\r\n'
    remote = installer.patch_codex_mcp(local, new, owned_previous=old)
    assert remote.startswith(foreign)
    assert remote.endswith(b'\r\n[foreign_section]\r\nkeep = true\r\n')
    with pytest.raises(installer.DeployError):
        installer.patch_codex_mcp(local, new, owned_previous={"command": "unverified"})


def test_codex_patch_never_replaces_foreign_content_inside_managed_markers():
    python, runtime, store = Path("C:/python.exe"), Path("C:/runtime"), Path("C:/coord")
    old = installer.codex_server(python, runtime, store)
    new = installer.codex_server(python, runtime, store, remote_config=REMOTE)
    local = installer.patch_codex_mcp(b"", old)
    local = local.replace(installer.TOML_END.encode(), b'[unowned]\nkeep = true\n' + installer.TOML_END.encode())
    with pytest.raises(installer.DeployError, match="unowned"):
        installer.patch_codex_mcp(local, new, owned_previous=old)


def test_native_installed_client_connection_failure_never_creates_local_office(enrollment):
    home, _state, python = enrollment
    isolated = {**REMOTE, "host": "127.0.0.1", "port": 1,
                "connect_timeout_s": 1, "request_timeout_s": 2}
    installer.install(home, python_executable=python, remote_config=isolated)
    runtime = home / installer.RUNTIME_RELATIVE
    environment = {key: os.environ[key] for key in ("SystemRoot", "WINDIR", "PATH", "PATHEXT") if key in os.environ}
    environment.update(USERPROFILE=str(home), HOME=str(home), APPDATA=str(home / "AppData" / "Roaming"))
    result = subprocess.run(
        [str(python), "-I", str(runtime / "office.py"),
         "--runtime-home", str(home / installer.COORD_RELATIVE),
         "--remote-config", str(runtime / installer.REMOTE_CONFIG_FILE),
         "--harness", "codex", "--session-id", "synthetic-native-enrollment",
         "presence", "--status", "idle"],
        capture_output=True, text=True, timeout=15, env=environment,
    )
    reply = json.loads(result.stdout)
    assert reply["status"] == "degraded", reply
    assert isinstance(reply.get("reason"), str)
    # Remote transport failure must not look like delivery to a shell caller.
    # The agent may still continue its independent work after this signal.
    assert result.returncode == 1
    assert not (home / installer.COORD_RELATIVE).exists()
