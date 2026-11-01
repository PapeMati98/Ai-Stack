"""Exercise the real CLI entrypoint, including its local-store boundary."""

import json
import io
import stat
from types import SimpleNamespace
from pathlib import Path

import pytest

from coordfs import office_cli
from coordfs import office_cli_remote
from coordfs.errors import CoordError


def forbid_local(*args, **kwargs):
    pytest.fail("Remote invocation reached the local Office constructor")


def test_real_entrypoint_routes_remote_before_local_store(monkeypatch, capfd):
    calls = []
    monkeypatch.setattr(office_cli, "build_office", forbid_local)
    monkeypatch.setattr(
        office_cli_remote, "dispatch_remote",
        lambda args: calls.append(args) or {"status": "ok", "code": "remote_read"},
    )
    assert office_cli.main([
        "--remote-config", "trusted.json", "--harness", "codex",
        "--session-id", "native-session", "read", "--limit", "3",
    ]) == 0
    assert len(calls) == 1
    assert str(calls[0].remote_config) == "trusted.json"
    assert calls[0].session_id == "native-session"
    assert calls[0].limit == 3
    output = capfd.readouterr()
    assert json.loads(output.out) == {"status": "ok", "code": "remote_read"}
    assert output.err == ""


def test_remote_refusal_is_printed_without_local_fallback(monkeypatch, capfd):
    monkeypatch.setattr(office_cli, "build_office", forbid_local)

    def refusal(args):
        raise CoordError("synthetic remote refusal", code="invalid_remote_config")

    monkeypatch.setattr(office_cli_remote, "dispatch_remote", refusal)
    assert office_cli.main(["--remote-config", "trusted.json", "read"]) == 1
    assert json.loads(capfd.readouterr().out)["code"] == "invalid_remote_config"


@pytest.mark.parametrize("reason", ["ssh_unavailable", "timeout", "invalid_response", "relay_error"])
def test_remote_transport_degraded_returns_failure_without_local_fallback(monkeypatch, capfd, reason):
    monkeypatch.setattr(office_cli, "build_office", forbid_local)
    monkeypatch.setattr(
        office_cli_remote, "dispatch_remote",
        lambda args: {"status": "degraded", "reason": reason},
    )
    assert office_cli.main(["--remote-config", "trusted.json", "read"]) == 1
    assert json.loads(capfd.readouterr().out) == {"status": "degraded", "reason": reason}


def test_local_and_remote_domain_degraded_remain_fail_open(monkeypatch, capfd):
    monkeypatch.setattr(office_cli, "dispatch", lambda args: {"status": "degraded", "reason": "office_unavailable"})
    assert office_cli.main(["read"]) == 0
    assert json.loads(capfd.readouterr().out)["reason"] == "office_unavailable"

    monkeypatch.setattr(office_cli, "build_office", forbid_local)
    monkeypatch.setattr(
        office_cli_remote, "dispatch_remote",
        lambda args: {"status": "degraded", "reason": "office_unavailable"},
    )
    assert office_cli.main(["--remote-config", "trusted.json", "read"]) == 0
    assert json.loads(capfd.readouterr().out)["reason"] == "office_unavailable"


def test_owner_only_refusal_precedes_config_and_stdin(monkeypatch, capfd):
    monkeypatch.setattr(office_cli, "build_office", forbid_local)
    monkeypatch.setattr(office_cli_remote, "load_remote_config", forbid_local)
    assert office_cli.main([
        "--remote-config", "missing.json", "wake-start",
    ]) == 1
    assert json.loads(capfd.readouterr().out)["code"] == "remote_local_only"


def test_remote_home_conflict_is_refused_before_config(monkeypatch, capfd):
    monkeypatch.setattr(office_cli, "build_office", forbid_local)
    monkeypatch.setattr(office_cli_remote, "load_remote_config", forbid_local)
    assert office_cli.main([
        "--remote-config", "missing.json", "--home", "unused", "read",
    ]) == 1
    assert json.loads(capfd.readouterr().out)["code"] == "remote_home_conflict"


def test_relay_verification_stays_local_before_any_file_read(monkeypatch, capfd):
    monkeypatch.setattr(office_cli, "build_office", forbid_local)
    monkeypatch.setattr(office_cli, "load_allow", forbid_local)
    monkeypatch.setattr(office_cli_remote, "load_remote_config", forbid_local)
    assert office_cli.main([
        "--remote-config", "missing.json", "wake-relay-verify",
        "--allow-file", "must-not-be-read.json", "--dispatch-json", "{}",
    ]) == 1
    assert json.loads(capfd.readouterr().out)["code"] == "remote_local_only"


def test_remote_mcp_owns_stdout(monkeypatch, capfd):
    monkeypatch.setattr(office_cli, "build_office", forbid_local)
    monkeypatch.setattr(office_cli_remote, "dispatch_remote", lambda args: None)
    assert office_cli.main(["--remote-config", "trusted.json", "mcp"]) == 0
    assert capfd.readouterr().out == ""


def test_existing_local_entrypoint_does_not_route_remote(monkeypatch, capfd):
    monkeypatch.setattr(office_cli_remote, "dispatch_remote", forbid_local)
    monkeypatch.setattr(office_cli, "dispatch", lambda args: {"status": "ok", "code": "local"})
    assert office_cli.main(["read"]) == 0
    assert json.loads(capfd.readouterr().out)["code"] == "local"


def test_remote_cli_module_is_in_installed_runtime():
    import install_agent_office as installer

    assert (
        "coordfs/office_cli_remote.py", "coordfs/office_cli_remote.py",
    ) in installer.RUNTIME_SOURCE_MAP


def test_remote_config_read_is_bounded_before_allocating(monkeypatch):
    requested = []

    class ConfigStream(io.BytesIO):
        def read(self, size=-1):
            requested.append(size)
            assert size == office_cli_remote.MAX_REMOTE_CONFIG_BYTES + 1
            return super().read(size)

    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: ConfigStream(b"x" * 100_000))
    monkeypatch.setattr(office_cli_remote, "_config_stat", lambda *args, **kwargs: SimpleNamespace(st_mode=stat.S_IFREG))
    monkeypatch.setattr(Path, "read_bytes", forbid_local)
    with pytest.raises(CoordError) as raised:
        office_cli_remote.load_remote_config(Path("oversized.json"))
    assert raised.value.code == "invalid_remote_config"
    assert requested == [office_cli_remote.MAX_REMOTE_CONFIG_BYTES + 1]


@pytest.mark.parametrize("mode", [stat.S_IFIFO, stat.S_IFCHR, stat.S_IFDIR, stat.S_IFSOCK])
def test_remote_config_non_regular_path_is_refused_before_open(monkeypatch, mode):
    monkeypatch.setattr(office_cli_remote, "_config_stat", lambda *args, **kwargs: SimpleNamespace(st_mode=mode))
    monkeypatch.setattr(Path, "open", forbid_local)
    with pytest.raises(CoordError) as raised:
        office_cli_remote.load_remote_config(Path("not-a-regular-file"))
    assert raised.value.code == "invalid_remote_config"


@pytest.mark.parametrize("path", [r"\\.\pipe\synthetic", r"\\?\GLOBALROOT\Device\synthetic"])
def test_remote_config_device_namespace_is_refused_before_metadata_or_open(monkeypatch, path):
    monkeypatch.setattr(office_cli_remote, "_config_stat", forbid_local)
    monkeypatch.setattr(Path, "open", forbid_local)
    with pytest.raises(CoordError) as raised:
        office_cli_remote.load_remote_config(path)
    assert raised.value.code == "invalid_remote_config"
