"""Product checks against actual Office RPC and observed official app shapes."""
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from coordfs import codex_relay_cycle as cycle
from coordfs.wake import Wake
from test_codex_desktop_remote import (
    CODEX_UUID, CODEX_WORKSPACE, _client, _make_codex_binding, _make_codex_dispatch,
)

PRIOR = 'abcdefab-cdef-4abc-8def-1234567890aa'
OLDER = 'abcdefab-cdef-4abc-8def-1234567890ab'
NEW = 'abcdefab-cdef-4abc-8def-1234567890bb'


def raw(payload):
    return {'content': [{'type': 'text', 'text': json.dumps(payload)}]}


class App:
    """No native tool calls: deterministic metadata/raw result fixture."""
    def __init__(self, *, state='idle', cwd=CODEX_WORKSPACE, lose_send=False):
        self.calls = []
        self.output = []
        self.state, self.cwd = state, cwd
        self.sent = False
        self.lose_send = lose_send
        self.turn_started = None

    def write_call(self, tool, arguments):
        token = uuid.uuid4().hex
        self.calls.append((token, tool, arguments))
        return token

    def read_result(self, token):
        assert self.calls[-1][0] == token
        _, tool, args = self.calls[-1]
        if tool == cycle.SEND_TOOL:
            self.sent, self.turn_started = True, time.time()
            if self.lose_send:
                raise cycle.CycleError('unknown', 'fixture_send_outcome_lost')
            return raw({'threadId': CODEX_UUID})
        if tool == cycle.WAIT_TOOL:
            assert 'afterCursor' not in args['targets'][0] or args['targets'][0]['afterCursor'].endswith(':1')
            return raw({'timedOut': False,
                        'wake': {'reason': 'turnCompleted', 'threadId': CODEX_UUID,
                                 'turnId': NEW, 'hostId': 'local'},
                        'polls': [{'schemaVersion': 1, 'cursor': CODEX_UUID + ':1',
                                   'thread': {'id': CODEX_UUID, 'hostId': 'local', 'status': {'type': 'idle'}},
                                   'latestTurn': {'id': NEW, 'status': 'completed',
                                                  'startedAt': self.turn_started, 'completedAt': time.time()}}]})
        assert tool == cycle.READ_TOOL
        assert args['threadId'] == CODEX_UUID
        now = time.time()
        turns = [{'id': PRIOR, 'status': 'completed', 'startedAt': now - 86400,
                  'completedAt': now - 86390, 'items': [{'synthetic_sensitive_text': 'discard-me'}]},
                 {'id': OLDER, 'status': 'completed', 'startedAt': now - 172800,
                  'completedAt': now - 172790, 'items': []}]
        if self.sent:
            turns.insert(0, {'id': NEW, 'status': 'completed', 'startedAt': self.turn_started,
                             'completedAt': now, 'items': []})
        return raw({'schemaVersion': 1,
                    'thread': {'id': CODEX_UUID, 'hostId': 'local', 'kind': 'codex', 'cwd': self.cwd,
                               'status': {'type': self.state, 'activeFlags': []}},
                    'page': {'order': 'newest_first', 'nextCursor': 'opaque-page-cursor', 'hasMore': True},
                    'turns': turns})

    def write_json_line(self, row):
        self.output.append(row)

    def close(self):
        pass


@pytest.fixture(autouse=True)
def server_flavor(monkeypatch):
    monkeypatch.setattr('coordfs.wake._native_workspace_flavor', lambda: 'posix')


def prepare(tmp_path):
    home = tmp_path / 'coord'
    _make_codex_binding(home, binding_id='codex-core')
    dispatch = _make_codex_dispatch(home, binding_id='codex-core')
    return home, dispatch, _client(home)


def run(client, app):
    return cycle.CodexRelayCycle(client, binding_ids=('codex-core',), host_factory=lambda: app).run()


def test_actual_gate_and_official_shapes_complete_without_ack(tmp_path):
    home, dispatch, client = prepare(tmp_path)
    app = App()
    result = run(client, app)
    assert result['status'] == 'delivered', result
    assert sum(tool == cycle.SEND_TOOL for _, tool, _ in app.calls) == 1
    stored = Wake(home=home)._load_dispatch(dispatch['dispatch_id'])
    assert stored['reconciliation_outcome'] == 'delivered'
    ack = client.request('wake-relay-ack', cycle.build_identity(),
                         {'dispatch_id': dispatch['dispatch_id'], 'driver': cycle.DRIVER})
    assert ack['acknowledged'] is False
    assert 'discard-me' not in json.dumps(stored)
    assert not stored.get('task_accepted')


@pytest.mark.parametrize('state,cwd', [('active', CODEX_WORKSPACE),
                                      ('idle', CODEX_WORKSPACE + '\\wrong-project')])
def test_busy_or_wrong_checkout_never_sends(tmp_path, state, cwd):
    home, dispatch, client = prepare(tmp_path)
    app = App(state=state, cwd=cwd)
    result = run(client, app)
    assert result['status'] in {'deferred', 'unknown', 'no_work'}, result
    assert all(tool != cycle.SEND_TOOL for _, tool, _ in app.calls)
    assert not Wake(home=home)._load_dispatch(dispatch['dispatch_id']).get('native_submission_started_at')


def test_lost_send_result_is_reconciled_without_second_send(tmp_path):
    home, dispatch, client = prepare(tmp_path)
    interrupted = App(lose_send=True)
    first = run(client, interrupted)
    assert first['status'] == 'unknown', first
    assert sum(tool == cycle.SEND_TOOL for _, tool, _ in interrupted.calls) == 1
    recovery = App()
    recovery.sent, recovery.turn_started = True, interrupted.turn_started
    second = run(client, recovery)
    assert second['status'] == 'delivered', second
    assert all(tool != cycle.SEND_TOOL for _, tool, _ in recovery.calls)
    assert Wake(home=home)._load_dispatch(dispatch['dispatch_id'])['reconciliation_outcome'] == 'delivered'


def test_real_isolated_python_entrypoint(tmp_path):
    script = Path(cycle.__file__)
    result = subprocess.run([sys.executable, '-I', '-B', '-X', 'utf8', str(script),
                             '--remote-config', str(tmp_path / 'absent.json')],
                            capture_output=True, text=True, timeout=10,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['status'] == 'no_work'
    assert not result.stderr


def test_actual_opaque_wait_cursor_is_preserved():
    cursor = CODEX_UUID + ':1'
    args = cycle.wait_arguments(thread_id=CODEX_UUID, after_cursor=cursor, timeout_ms=1000)
    assert args['targets'][0]['afterCursor'] == cursor
