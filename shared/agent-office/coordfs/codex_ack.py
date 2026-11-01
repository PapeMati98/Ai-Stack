"""Metadata-only ACK lookup for an owned, already fenced Codex dispatch."""
from __future__ import annotations

from pathlib import Path
from .errors import CoordError
from .office import inbox_path, parse_envelope
from .wake import CODEX_RELAY_DRIVER, Wake

MAX_SCAN_BYTES = 2 * 1024 * 1024


def _metadata(path: Path):
    # Only the current inbox and one retained generation, with a byte ceiling.
    # Bodies are parsed by the existing envelope validator but never returned.
    for candidate in (path, path.with_name(path.name + '.1')):
        if not candidate.is_file():
            continue
        with candidate.open('rb') as stream:
            size = stream.seek(0, 2)
            start = max(0, size - MAX_SCAN_BYTES)
            stream.seek(start)
            if start:
                stream.readline()
            for line in stream.read(MAX_SCAN_BYTES).splitlines(keepends=True):
                envelope = parse_envelope(line)
                if envelope:
                    yield {k: envelope.get(k) for k in ('id','from','to','ack_of','reply_to')}


def owned_codex_ack(home: Path, dispatch_id: str, relay_agent: str) -> dict:
    wake = Wake(home=home)
    row = wake._load_dispatch(dispatch_id)
    binding = wake._relay_binding_for_driver(row['binding_id'], CODEX_RELAY_DRIVER)
    if row.get('relay_agent') != relay_agent:
        raise CoordError('ACK lookup requires the dispatch owner', code='identity_mismatch')
    if row.get('status') not in {'delivery_unknown','delivered','unconfirmed','target_started','target_finished'}:
        raise CoordError('ACK lookup requires a fenced submission', code='relay_dispatch_fenced')
    target = binding['office_agent']
    origin = next((x for x in _metadata(inbox_path(home, target))
                   if x['id'] == row['trigger_ref'] and x['to'] == target), None)
    ack = None
    if origin is not None:
        ack = next((x for x in _metadata(inbox_path(home, origin['from']))
                    if x['from'] == target and x['to'] == origin['from']
                    and x['ack_of'] == row['trigger_ref'] and x['reply_to'] == row['trigger_ref']), None)
    return {'status':'ok', 'code':'wake_relay_ack', 'dispatch_id':dispatch_id,
            'target':target, 'trigger_ref':row['trigger_ref'],
            'ack_id':None if ack is None else ack['id'], 'acknowledged':ack is not None}


def owned_zcode_reply(home: Path, dispatch_id: str, relay_agent: str) -> dict:
    """Verify an actual Office reply; external CU has no native send-message id."""
    from .wake import ZCODE_RELAY_DRIVER
    wake = Wake(home=home)
    row = wake._load_dispatch(dispatch_id)
    binding = wake._relay_binding_for_driver(row['binding_id'], ZCODE_RELAY_DRIVER)
    if row.get('relay_agent') != relay_agent:
        raise CoordError('reply lookup requires the dispatch owner', code='identity_mismatch')
    if row.get('status') not in {'delivery_unknown', 'delivered', 'target_started', 'target_finished'}:
        raise CoordError('reply lookup requires a fenced submission', code='relay_dispatch_fenced')
    target = binding['office_agent']
    origin = next((x for x in _metadata(inbox_path(home, target))
                   if x['id'] == row['trigger_ref'] and x['to'] == target), None)
    reply = None
    if origin is not None:
        reply = next((x for x in _metadata(inbox_path(home, origin['from']))
                      if x['from'] == target and x['to'] == origin['from']
                      and x['reply_to'] == row['trigger_ref']), None)
    return {'status': 'ok', 'code': 'wake_relay_ack', 'dispatch_id': dispatch_id,
            'target': target, 'trigger_ref': row['trigger_ref'],
            'ack_id': None if reply is None else reply['id'], 'acknowledged': reply is not None,
            'evidence_kind': 'office_correlated_reply'}


def owned_antigravity_ack(home: Path, dispatch_id: str, relay_agent: str) -> dict:
    """Exact recipient ACK for a fenced native AG submission, metadata only.

    agentapi exit zero supplies no native message ID.  Keep the dispatch
    unknown until the actual target acknowledges its exact Office trigger.
    This establishes reception only; native started/finished remain separate.
    """
    from .wake import ANTIGRAVITY_RELAY_DRIVER
    wake = Wake(home=home)
    row = wake._load_dispatch(dispatch_id)
    binding = wake._relay_binding_for_driver(row['binding_id'], ANTIGRAVITY_RELAY_DRIVER)
    if row.get('relay_agent') != relay_agent:
        raise CoordError('ACK lookup requires the dispatch owner', code='identity_mismatch')
    if (row.get('unknown_reason') != 'native_submission_unconfirmed'
            or row.get('status') not in {'delivery_unknown', 'delivered', 'target_started', 'target_finished'}):
        raise CoordError('ACK lookup requires a fenced native submission', code='relay_dispatch_fenced')
    if (row.get('binding_version') != binding['version']
            or row.get('target_local_id') != binding['native_thread_id']
            or binding['office_agent'] != 'antigravity-ailab-' + binding['native_thread_id']):
        raise CoordError('ACK lookup requires the original exact endpoint', code='identity_mismatch')
    target = binding['office_agent']
    origin = next((x for x in _metadata(inbox_path(home, target))
                   if x['id'] == row['trigger_ref'] and x['to'] == target), None)
    ack = None
    if origin is not None:
        ack = next((x for x in _metadata(inbox_path(home, origin['from']))
                    if x['from'] == target and x['to'] == origin['from']
                    and x['ack_of'] == row['trigger_ref'] and x['reply_to'] == row['trigger_ref']), None)
    return {'status': 'ok', 'code': 'wake_relay_ack', 'dispatch_id': dispatch_id,
            'target': target, 'trigger_ref': row['trigger_ref'],
            'ack_id': None if ack is None else ack['id'], 'acknowledged': ack is not None,
            'evidence_kind': 'office_correlated_ack'}
