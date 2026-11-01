"""Synthetic current-core reproduction: fenced AG must settle on its real ACK."""
from datetime import datetime, timezone
from pathlib import Path
import sys
import time

from coordfs.office import Office
from coordfs.wake import ANTIGRAVITY_RELAY_DRIVER, Wake
from coordfs.leases import format_ts
from coordfs.errors import CoordError
import pytest

NATIVE_ID = 'abcdefab-cdef-4abc-8def-1234567890ab'
SENDER = 'codex-ailab-00000000-0000-0000-0000-000000000003'
TARGET = 'antigravity-ailab-' + NATIVE_ID
RELAY = 'antigravity-ailab-00000000-0000-0000-0000-000000000004'


def prepared(tmp_path, monkeypatch):
    monkeypatch.setattr('coordfs.wake._native_workspace_flavor', lambda: 'posix')
    home = tmp_path / 'synthetic-coord'
    wake = Wake(home=home)
    wake.enroll(binding_id='synthetic-ag', office_agent=TARGET,
                driver=ANTIGRAVITY_RELAY_DRIVER, native_thread_id=NATIVE_ID,
                workspace=r'C:\synthetic\antigravity', enrolled_by='synthetic-owner',
                allowed_wakers=[SENDER], allowed_triggers=['message'], wake_kinds=['question'],
                expires_at=format_ts(time.time()+3600), owner_confirm=True)
    wake.verify('synthetic-ag', owner_confirm=True)
    sender = Office(agent_id=SENDER, home=home)
    sent = sender.send(to=TARGET, body='Read this synthetic Office task only.',
                       kind='question', new_recipient=True)
    assert sent['status'] == 'ok', sent
    wake.tick(binding_id='synthetic-ag')
    pending = wake.relay_pending(binding_id='synthetic-ag', driver=ANTIGRAVITY_RELAY_DRIVER)
    assert pending['count'] == 1, pending
    did = pending['dispatches'][0]['dispatch_id']
    wake.relay_claim(did, relay_agent=RELAY, driver=ANTIGRAVITY_RELAY_DRIVER)
    wake.relay_native_begin(did, relay_agent=RELAY, driver=ANTIGRAVITY_RELAY_DRIVER)
    assert wake._load_dispatch(did)['status'] == 'delivery_unknown'
    return wake, home, sent, did


def test_real_ag_ack_can_settle_fenced_native_submission(tmp_path, monkeypatch):
    wake, home, sent, did = prepared(tmp_path, monkeypatch)
    target = Office(agent_id=TARGET, home=home)
    read = target.read()
    assert read['count'] == 1
    ack = target.ack(sent['id'], note='Synthetic task read; no business effect.')
    assert ack['status'] == 'ok', ack
    receipt = wake.relay_receipt(did, status='delivered', message_id=ack['id'],
                               relay_agent=RELAY, driver=ANTIGRAVITY_RELAY_DRIVER)
    assert receipt['status'] == 'ok', receipt
    wake.relay_observe(did, stage='target_started', relay_agent=RELAY,
                       driver=ANTIGRAVITY_RELAY_DRIVER)
    final = wake._load_dispatch(did)
    assert final['receipt_message_id'] == ack['id']
    assert final['reconciliation_outcome'] == 'delivered'
    assert wake.relay_pending(binding_id='synthetic-ag', driver=ANTIGRAVITY_RELAY_DRIVER)['count'] == 0


@pytest.mark.parametrize('mode', ['missing', 'wrong_id', 'reply_only', 'other_message', 'foreign_sender'])
def test_exit_zero_or_unrelated_reply_never_settles(mode, tmp_path, monkeypatch):
    wake, home, sent, did = prepared(tmp_path, monkeypatch)
    target = Office(agent_id=TARGET, home=home)
    claimed_ack_id = '0' * 32
    if mode == 'wrong_id':
        target.read()
        target.ack(sent['id'])
    elif mode == 'reply_only':
        reply = target.send(to=SENDER, reply_to=sent['id'], body='Synthetic reply without ACK', new_recipient=True)
        assert reply['status'] == 'ok', reply
    elif mode == 'other_message':
        second = Office(agent_id=SENDER, home=home).send(to=TARGET, body='Another synthetic task', new_recipient=True)
        assert second['status'] == 'ok', second
        target.read()
        claimed_ack_id = target.ack(second['id'])['id']
    elif mode == 'foreign_sender':
        foreign = Office(agent_id='antigravity-ailab-00000000-0000-0000-0000-000000000005', home=home)
        reply = foreign.send(to=SENDER, reply_to=sent['id'], body='Foreign synthetic reply', new_recipient=True)
        assert reply['status'] == 'ok', reply
    with pytest.raises(CoordError) as error:
        wake.relay_receipt(did, status='delivered', message_id=claimed_ack_id,
                           relay_agent=RELAY, driver=ANTIGRAVITY_RELAY_DRIVER)
    assert error.value.code == 'relay_ack_missing'
    assert wake._load_dispatch(did)['status'] == 'delivery_unknown'


def test_dispatch_owner_and_endpoint_version_are_bound(tmp_path, monkeypatch):
    wake, home, sent, did = prepared(tmp_path, monkeypatch)
    target = Office(agent_id=TARGET, home=home)
    target.read()
    ack = target.ack(sent['id'])
    with pytest.raises(CoordError) as error:
        wake.relay_receipt(did, status='delivered', message_id=ack['id'],
                           relay_agent=SENDER, driver=ANTIGRAVITY_RELAY_DRIVER)
    assert error.value.code == 'identity_mismatch'
    binding = wake._load_binding('synthetic-ag')
    binding['version'] += 1
    wake._atomic_replace_json(wake._binding_path('synthetic-ag'), binding)
    with pytest.raises(CoordError) as error:
        wake.relay_receipt(did, status='delivered', message_id=ack['id'],
                           relay_agent=RELAY, driver=ANTIGRAVITY_RELAY_DRIVER)
    assert error.value.code == 'identity_mismatch'


def test_native_receipt_and_observation_retry_is_idempotent(tmp_path, monkeypatch):
    wake, home, sent, did = prepared(tmp_path, monkeypatch)
    target = Office(agent_id=TARGET, home=home)
    target.read()
    ack = target.ack(sent['id'])
    kw = dict(status='delivered', message_id=ack['id'], relay_agent=RELAY,
              driver=ANTIGRAVITY_RELAY_DRIVER)
    wake.relay_receipt(did, **kw)
    assert wake.relay_receipt(did, **kw)['idempotent'] is True
    for _ in range(2):
        wake.relay_observe(did, stage='target_started', relay_agent=RELAY,
                           driver=ANTIGRAVITY_RELAY_DRIVER)
    rows = [x for x in wake._ledger_entries() if x.get('dispatch_id') == did]
    assert sum(x.get('decision') == 'reconciled' for x in rows) == 1
    assert sum(x.get('decision') == 'delivered' for x in rows) == 1


def test_unsubmitted_claim_cannot_be_reported_delivered(tmp_path, monkeypatch):
    wake, home, sent, did = prepared(tmp_path, monkeypatch)
    row = wake._load_dispatch(did)
    row['status'] = 'claimed'
    row.pop('unknown_reason')
    row.pop('unknown_at')
    wake._atomic_replace_json(wake._dispatch_path(did), row)
    with pytest.raises(CoordError) as error:
        wake.relay_receipt(did, status='delivered', message_id='0' * 32,
                           relay_agent=RELAY, driver=ANTIGRAVITY_RELAY_DRIVER)
    assert error.value.code == 'relay_native_fence_missing'
