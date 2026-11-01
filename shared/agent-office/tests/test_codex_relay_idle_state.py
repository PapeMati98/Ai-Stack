"""Observed unloaded Codex metadata and isolated Office delivery regressions."""
import json
import time
from pathlib import Path

import pytest

from coordfs import codex_relay_cycle as cycle
from coordfs.wake import Wake
from test_codex_desktop_remote import CODEX_UUID
from test_codex_relay_cycle_product import App, prepare, raw, run


@pytest.fixture(autouse=True)
def server_flavor(monkeypatch):
    monkeypatch.setattr('coordfs.wake._native_workspace_flavor', lambda: 'posix')


def test_actual_completed_unloaded_thread_is_idle():
    payload = json.loads((Path(__file__).parent / 'fixtures' /
                          'codex_read_not_loaded_metadata.json').read_text(encoding='utf8'))
    assert payload['thread']['status'] == {'type': 'notLoaded'}
    assert payload['turns'] and all(t['status'] == 'completed' for t in payload['turns'])
    metadata = cycle.parse_read_result(raw(payload), expected_thread_id=payload['thread']['id'], now=time.time())
    assert metadata.state == 'idle'
    assert not metadata.active_flags
    assert not hasattr(metadata.turns[0], 'items')


class ObservedApp(App):
    def __init__(self, *, state='notLoaded', active=False, flags=(), empty=False):
        super().__init__(state=state)
        self.active, self.flags, self.empty = active, flags, empty

    def read_result(self, token):
        result = super().read_result(token)
        if self.calls[-1][1] != cycle.READ_TOOL:
            return result
        payload = json.loads(result['content'][0]['text'])
        payload['thread']['status'] = {'type': self.state}
        if self.flags:
            payload['thread']['status']['activeFlags'] = list(self.flags)
        if self.empty:
            payload['turns'] = []
        elif self.active:
            payload['turns'][0].update(status='inProgress', completedAt=None)
        return raw(payload)


def test_unloaded_terminal_turn_delivers_once_and_settles_without_ack(tmp_path):
    home, dispatch, client = prepare(tmp_path)
    app = ObservedApp()
    result = run(client, app)
    assert result['status'] == 'delivered', result
    assert sum(tool == cycle.SEND_TOOL for _, tool, _ in app.calls) == 1
    stored = Wake(home=home)._load_dispatch(dispatch['dispatch_id'])
    assert stored['reconciliation_outcome'] == 'delivered'
    assert not stored.get('task_accepted')
    assert client.request('wake-relay-ack', cycle.build_identity(),
                          {'dispatch_id': dispatch['dispatch_id'], 'driver': cycle.DRIVER})['acknowledged'] is False


@pytest.mark.parametrize('state,active,flags,empty', [
    ('notLoaded', True, (), False),
    ('idle', True, (), False),
    ('notLoaded', False, ('waitingForApproval',), False),
    ('idle', False, ('waitingForUserInput',), False),
    ('notLoaded', False, (), True),
    ('busy', False, (), False),
    ('active', False, (), False),
    ('systemError', False, (), False),
    ('unknown', False, (), False),
])
def test_uncertain_or_active_thread_never_claims_or_sends(tmp_path, state, active, flags, empty):
    home, dispatch, client = prepare(tmp_path)
    app = ObservedApp(state=state, active=active, flags=flags, empty=empty)
    result = run(client, app)
    assert result['status'] in {'deferred', 'unknown', 'no_work'}, result
    assert all(tool != cycle.SEND_TOOL for _, tool, _ in app.calls)
    stored = Wake(home=home)._load_dispatch(dispatch['dispatch_id'])
    assert not stored.get('native_submission_started_at')
    assert not stored.get('relay_agent')


@pytest.mark.parametrize('terminal', ['completed', 'failed', 'interrupted'])
def test_unloaded_terminal_states_are_inactive(terminal):
    app = ObservedApp()
    token = app.write_call(cycle.READ_TOOL, cycle.read_arguments(CODEX_UUID))
    payload = json.loads(app.read_result(token)['content'][0]['text'])
    payload['turns'][0]['status'] = terminal
    assert cycle.parse_read_result(raw(payload), expected_thread_id=CODEX_UUID, now=time.time()).state == 'idle'
