"""Parent integration: actual remote framing, fresh native metadata, no ACK."""
import time
import uuid

import pytest

from coordfs.wake import CODEX_RELAY_DRIVER, Wake
from test_codex_desktop_remote import (
    CODEX_UUID, _client, _make_codex_binding, _make_codex_dispatch,
)

OWNER = 'codex-ailab-00000000-0000-0000-0000-000000000001'


@pytest.fixture(autouse=True)
def linux_gate(monkeypatch):
    monkeypatch.setattr('coordfs.wake._native_workspace_flavor', lambda: 'posix')


def request(client, op, **args):
    return client.request(op, {'agent_id': OWNER}, {'driver': CODEX_RELAY_DRIVER, **args})


def test_completed_new_turn_releases_binding_even_without_ack(tmp_path):
    home = tmp_path / 'coord'
    _make_codex_binding(home)
    dispatch = _make_codex_dispatch(home)
    client = _client(home)
    did = dispatch['dispatch_id']
    snap = request(client, 'wake-relay-pending', binding_id='codex-bind-001',
                   include_owned=True, relay_agent=OWNER)
    assert snap['status'] == 'ok', snap
    assert snap['binding']['native_thread_id'] == CODEX_UUID
    assert request(client, 'wake-relay-claim', dispatch_id=did, relay_agent=OWNER)['status'] == 'ok'
    prior = str(uuid.uuid4())
    begun = request(client, 'wake-relay-native-begin', dispatch_id=did, relay_agent=OWNER,
                    prior_turn_id=prior, baseline_observed_at=time.time())
    assert begun['status'] == 'ok', begun
    turn = str(uuid.uuid4())
    stamp = time.time()
    assert request(client, 'wake-relay-receipt', dispatch_id=did,
                   status='unconfirmed', turn_id=turn)['status'] == 'ok'
    proof = {'schema': 'codex-native-turn/v1', 'source': 'mcp__codex_app__read_thread',
             'thread_id': CODEX_UUID, 'turn_id': turn, 'started_at': stamp,
             'completed_at': time.time(), 'status': 'completed', 'observed_at': time.time()}
    observed = request(client, 'wake-relay-observe', dispatch_id=did,
                       stage='target_finished', native_proof=proof)
    assert observed['status'] == 'ok', observed
    wake = Wake(home=home)
    settled = wake._load_dispatch(did)
    assert settled['reconciliation_outcome'] == 'delivered'
    ack = request(client, 'wake-relay-ack', dispatch_id=did)
    assert ack['acknowledged'] is False
    # Transport delivery is not task acceptance and does not refund its attempt.
    assert not settled.get('task_accepted')
    assert wake._daily_attempts(wake._load_binding('codex-bind-001'), wake._ledger_entries()) == 1
    again = request(client, 'wake-relay-observe', dispatch_id=did,
                    stage='target_finished', native_proof=proof)
    assert again['status'] == 'ok', again
    rows = [r for r in wake._ledger_entries() if r.get('dispatch_id') == did
            and r.get('decision') == 'reconciled']
    assert len(rows) == 1
    second = _make_codex_dispatch(home)
    claimed = request(client, 'wake-relay-claim', dispatch_id=second['dispatch_id'], relay_agent=OWNER)
    assert claimed['status'] == 'ok', claimed


def test_same_turn_or_another_thread_keeps_unknown_fenced(tmp_path):
    home = tmp_path / 'coord'
    _make_codex_binding(home)
    dispatch = _make_codex_dispatch(home)
    client = _client(home)
    did = dispatch['dispatch_id']
    claim = request(client, 'wake-relay-claim', dispatch_id=did, relay_agent=OWNER)
    assert claim['status'] == 'ok', claim
    prior = str(uuid.uuid4())
    begin = request(client, 'wake-relay-native-begin', dispatch_id=did, relay_agent=OWNER,
                    prior_turn_id=prior, baseline_observed_at=time.time())
    assert begin['status'] == 'ok', begin
    receipt = request(client, 'wake-relay-receipt', dispatch_id=did, status='unconfirmed', turn_id=prior)
    assert receipt['status'] == 'ok', receipt
    stamp = time.time()
    proof = {'schema': 'codex-native-turn/v1', 'source': 'mcp__codex_app__read_thread',
             'thread_id': CODEX_UUID, 'turn_id': prior, 'started_at': stamp,
             'completed_at': stamp, 'status': 'completed', 'observed_at': stamp}
    for bad in (proof, {**proof, 'thread_id': str(uuid.uuid4())}):
        out = request(client, 'wake-relay-observe', dispatch_id=did,
                      stage='target_finished', native_proof=bad)
        assert out['status'] != 'ok', out
    assert Wake(home=home)._load_dispatch(did).get('reconciliation_outcome') != 'delivered'
