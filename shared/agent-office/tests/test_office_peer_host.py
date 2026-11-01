"""SSH key configuration, not an RPC claim, selects the client's host."""

import io
import json

import pytest

from coordfs import office_ssh_gate as gate


def request(home, identity, *, peer_host="ailab", principal="ws-office", args=None):
    frame = {"v": 1, "op": "presence", "identity": identity,
             "args": {"status": "busy"} if args is None else args}
    output = io.BytesIO()
    code = gate.run(io.BytesIO(json.dumps(frame).encode() + b"\n"), output,
                    principal=principal, peer_host=peer_host, home=home, environ={})
    return code, json.loads(output.getvalue())


@pytest.mark.parametrize("peer_host", ["AILAB", "", "other/host", "../server", 7])
def test_invalid_server_peer_host_is_refused_before_home_creation(tmp_path, peer_host):
    home = tmp_path / "uncreated"
    code, result = request(home, {"harness": "codex", "host": "ailab", "session_id": "s"},
                           peer_host=peer_host)
    assert (code, result["code"]) == (1, "invalid_peer_host")
    assert not home.exists()


@pytest.mark.parametrize("identity", [
    {"harness": "codex", "host": "ailabserver", "session_id": "s"},
    {"agent_id": "codex-ailab-s"},
])
def test_forged_or_unqualified_host_is_refused_before_home(tmp_path, identity):
    home = tmp_path / "uncreated"
    code, result = request(home, identity)
    assert (code, result["code"]) == (1, "peer_host_mismatch")
    assert not home.exists()


@pytest.mark.parametrize("include_host", [True, False])
def test_peer_host_binds_real_presence_and_agent_address(tmp_path, include_host):
    identity = {"harness": "codex", "session_id": "test"}
    if include_host:
        identity["host"] = "peer-linux"
    code, result = request(tmp_path / "coord", identity, peer_host="peer-linux",
                           args={"status": "busy", "workspace": "/srv/project"})
    assert code == 0, result
    assert result["agent"] == "codex-peer-linux-test"
    assert result["meta"]["host"] == "peer-linux"
    assert result["meta"]["workspace"] == "/srv/project"


def test_hermes_principal_still_refuses_other_harness_with_valid_pin(tmp_path):
    home = tmp_path / "uncreated"
    code, result = request(home, {"harness": "codex", "host": "ailabserver", "session_id": "s"},
                           peer_host="ailabserver", principal="hermes")
    # F2: the per-principal harness allowlist refuses this before the legacy prefix check.
    assert (code, result["code"]) == (1, "harness_not_allowed")
    assert not home.exists()


def test_peer_host_cannot_be_supplied_in_operation_arguments(tmp_path):
    home = tmp_path / "coord"
    code, result = request(home, {"harness": "codex", "host": "ailab", "session_id": "s"},
                           args={"status": "busy", "peer_host": "other"})
    assert (code, result["code"]) == (1, "invalid_args")


def test_production_entrypoint_requires_server_owned_peer_host():
    with pytest.raises(SystemExit) as raised:
        gate.main(["--principal", "ws-office", "--home", "unused"])
    assert raised.value.code == 2
