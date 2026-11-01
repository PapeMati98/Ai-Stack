"""Synthetic AG ACK through the real CLI mapping and SSH protocol gate."""
import sys
from pathlib import Path

import pytest
from coordfs.office import Office
from coordfs.office_cli import build_parser, dispatch
from coordfs import office_cli_remote
from coordfs.errors import CoordError
from coordfs.wake import ANTIGRAVITY_RELAY_DRIVER
from test_antigravity_ack_settlement import prepared, RELAY, TARGET
from test_codex_desktop_remote import _client


def args(did, remote=False):
    argv = ['--agent', RELAY]
    if remote:
        argv += ['--remote-config', 'synthetic-config.json']
    argv += ['wake-relay-ack', '--dispatch-id', did,
             '--driver', ANTIGRAVITY_RELAY_DRIVER]
    return build_parser().parse_args(argv)


def acknowledged(home, sent):
    target = Office(agent_id=TARGET, home=home)
    target.read()
    return target.ack(sent['id'])['id']


def test_local_cli_returns_only_correlated_ack_metadata(tmp_path, monkeypatch):
    wake, home, sent, did = prepared(tmp_path, monkeypatch)
    monkeypatch.setattr('coordfs.office_cli.build_office',
                        lambda _: Office(agent_id=RELAY, home=home))
    before = dispatch(args(did))
    assert before['code'] == 'wake_relay_ack'
    assert before['acknowledged'] is False
    ack_id = acknowledged(home, sent)
    result = dispatch(args(did))
    assert result['acknowledged'] is True
    assert result['ack_id'] == ack_id
    assert result['evidence_kind'] == 'office_correlated_ack'
    assert 'body' not in result and 'note' not in result
    assert wake._load_dispatch(did)['status'] == 'delivery_unknown'


def test_remote_cli_uses_real_gate_without_local_store_fallback(tmp_path, monkeypatch):
    wake, home, sent, did = prepared(tmp_path, monkeypatch)
    client = _client(home)
    monkeypatch.setattr(office_cli_remote, 'load_remote_config', lambda _: client.config)
    monkeypatch.setattr(office_cli_remote, 'OfficeRemoteClient', lambda _: client)
    def forbidden(*_a, **_kw):
        raise AssertionError('remote ACK must never construct a local Office store')
    monkeypatch.setattr('coordfs.office_cli.build_office', forbidden)
    assert dispatch(args(did, remote=True))['acknowledged'] is False
    ack_id = acknowledged(home, sent)
    result = dispatch(args(did, remote=True))
    assert result['acknowledged'] is True and result['ack_id'] == ack_id
    client.request('wake-relay-receipt', {'agent_id': RELAY},
                   {'dispatch_id': did, 'driver': ANTIGRAVITY_RELAY_DRIVER,
                    'status': 'delivered', 'message_id': ack_id})
    client.request('wake-relay-observe', {'agent_id': RELAY},
                   {'dispatch_id': did, 'driver': ANTIGRAVITY_RELAY_DRIVER,
                    'stage': 'target_started'})
    assert wake._load_dispatch(did)['reconciliation_outcome'] == 'delivered'


def test_remote_gate_rejects_foreign_dispatch_owner(tmp_path, monkeypatch):
    _, home, sent, did = prepared(tmp_path, monkeypatch)
    acknowledged(home, sent)
    result = _client(home).request('wake-relay-ack', {'agent_id': TARGET},
                                   {'dispatch_id': did, 'driver': ANTIGRAVITY_RELAY_DRIVER})
    assert result['status'] == 'error' and result['code'] == 'identity_mismatch'


def test_remote_gate_rejects_driver_substitution(tmp_path, monkeypatch):
    _, home, sent, did = prepared(tmp_path, monkeypatch)
    acknowledged(home, sent)
    result = _client(home).request('wake-relay-ack', {'agent_id': RELAY},
                                   {'dispatch_id': did, 'driver': 'codex-desktop-relay'})
    assert result['status'] == 'error' and result['code'] == 'relay_driver_mismatch'


def test_cli_requires_known_ack_driver():
    with pytest.raises(SystemExit):
        build_parser().parse_args(['wake-relay-ack', '--dispatch-id', 'x',
                                   '--driver', 'claude-desktop-relay'])


def test_remote_cli_transport_failure_does_not_fall_back(tmp_path, monkeypatch):
    _, home, _, did = prepared(tmp_path, monkeypatch)
    client = _client(home)
    monkeypatch.setattr(office_cli_remote, 'load_remote_config', lambda _: client.config)
    class Broken:
        def request(self, *_a, **_kw):
            raise CoordError('synthetic transport failure', code='ssh_unavailable')
    monkeypatch.setattr(office_cli_remote, 'OfficeRemoteClient', lambda _: Broken())
    with pytest.raises(CoordError, match='synthetic transport failure'):
        dispatch(args(did, remote=True))
