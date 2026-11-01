"""Real client/gate/MCP/store integration; synthetic hosts, no SSH connection."""

import io
import json

import pytest

from coordfs.checkout_claims import local_host
from coordfs.office_remote import OfficeRemoteClient, OfficeRemoteConfig
from coordfs.office_ssh_gate import run
from test_office_remote_roundtrip import _GateProcess, _RequestPipe


class PinnedRequestPipe(_RequestPipe):
    def close(self):
        if self.closed:
            return
        self.closed = True
        output = io.BytesIO()
        self.process.returncode = run(
            io.BytesIO(self.data), output, principal=self.process.principal,
            peer_host=self.process.peer_host, home=self.process.home, environ={},
        )
        self.process.stdout.supply(output.getvalue())


class PinnedGateProcess(_GateProcess):
    def __init__(self, home, peer_host, principal):
        super().__init__(home)
        self.peer_host = peer_host
        self.principal = principal
        self.stdin = PinnedRequestPipe(self)


def client(home, host, *, harness="codex", principal="ws-office"):
    config = OfficeRemoteConfig(host="office.invalid", user="agentoffice",
                                identity_file="unused-key", known_hosts_file="unused-hosts")
    remote = OfficeRemoteClient(config, popen_factory=lambda *_args, **_kwargs:
                                PinnedGateProcess(home, host, principal))
    identity = {"harness": harness, "host": host, "session_id": "test-session"}
    def call(name, **arguments):
        return remote.request("tool-call", identity, {"name": name, "arguments": arguments})
    return call, identity


@pytest.mark.parametrize("foreign_checkout", ["/srv/hermes/stack", "D:/stack/repo"])
def test_project_claims_and_delivery_through_pinned_remote_tools(tmp_path, foreign_checkout):
    home = tmp_path / "coord"
    checkout = tmp_path / "local-repo"
    checkout.mkdir()
    native, native_id = client(home, local_host())
    foreign, foreign_id = client(home, "foreign-peer", harness="hermes", principal="hermes")
    first = native("office_project_register", project_id="shared-stack", checkout=str(checkout))
    assert first["status"] == "ok", first
    second = foreign("office_project_register", project_id="shared-stack", checkout=foreign_checkout)
    assert second["status"] == "ok", second
    assert len(second["project"]["checkout_claims"]) == 2
    assert second["project"]["checkout"] == first["project"]["checkout"]
    stream = dict(project_id="shared-stack", task_id="task", thread_id="thread", run_id="run")
    sent = native("office_project_send", **stream, recipient=foreign_id, body="synthetic shared work")
    assert sent["status"] == "ok", sent
    received = foreign("office_project_read", **stream)
    assert received["status"] == "ok", received
    assert received["messages"][0]["untrusted_message"]["body"] == "synthetic shared work"
    ack = foreign("office_project_acknowledge", project_id="shared-stack", delivery_id=sent["delivery_id"])
    assert ack["status"] == "ok", ack
    reply = foreign("office_project_send", **stream, recipient=native_id, body="synthetic foreign reply")
    assert reply["status"] == "ok", reply
    back = native("office_project_read", **stream)
    assert any(item["untrusted_message"].get("application", {}).get("delivery_id") == reply["delivery_id"]
               and item["untrusted_message"]["body"] == "synthetic foreign reply" for item in back["messages"])
    acknowledged = native("office_project_acknowledge", project_id="shared-stack", delivery_id=reply["delivery_id"])
    assert acknowledged["status"] == "ok", acknowledged
    foreign("office_project_read", **stream)
    for call, message in ((native, sent), (foreign, reply)):
        delivered = call("office_project_delivery", project_id="shared-stack", delivery_id=message["delivery_id"])
        assert delivered["delivery"]["state"] == "ACKNOWLEDGED", delivered
    # An explicit sender override is never authority to claim another peer.
    forged = foreign("office_project_send", **stream, recipient=native_id, body="forged sender",
                     harness="hermes", host=native_id["host"], session_id=foreign_id["session_id"])
    assert forged["status"] == "error", forged


def test_work_binding_filters_and_collaborator_updates_use_connection_host(tmp_path):
    home = tmp_path / "coord"
    maker, _ = client(home, "peer-one", harness="hermes", principal="hermes")
    collaborator, _ = client(home, "peer-two")
    checkout = "/srv/shared-spelling"
    registered = maker("office_work_register", project="shared-stack", task_id="task",
                       checkout=checkout, scopes=["src/task.py"], base_sha="a" * 40,
                       commit_intent="candidate only", journal_refs=["journal://test"])
    assert registered["status"] == "ok", registered
    assert registered["task"]["checkout_claim"]["host"] == "peer-one"
    listed = maker("office_work_list", project="shared-stack", checkout=checkout)
    assert [item["task_id"] for item in listed["tasks"]] == ["task"]
    other = collaborator("office_work_list", project="shared-stack", checkout=checkout)
    assert other["tasks"] == []
    update = collaborator("office_work_update", project="shared-stack", task_id="task", status="ready")
    assert update["status"] == "ok", update
    assert update["task"]["checkout_claim"]["host"] == "peer-one"
    rebind = collaborator("office_work_update", project="shared-stack", task_id="task", checkout=checkout)
    assert rebind["status"] == "error", rebind
    # No public keyword can replace the transport's host binding.
    override = collaborator("office_work_register", project="shared-stack", task_id="forged",
                            checkout=checkout, scopes=["src/task.py"], checkout_host="peer-one")
    assert override["status"] == "error", override
