"""Native wrapper/process behavior on synthetic inputs; no real Desktop API."""
import subprocess
import sys
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from coordfs.antigravity_relay import native_runner
from coordfs.antigravity_transport import agentapi_command, attempt
from coordfs.errors import CoordError

NATIVE_ID = 'abcdefab-cdef-4abc-8def-1234567890ab'


def wrapper(tmp_path, line=None):
    path = tmp_path / 'agentapi.bat'
    path.write_text('@echo off\n' + (line or '"C:\\synthetic\\language_server.exe" agentapi %*') + '\n', encoding='utf8')
    return path


def test_vendor_wrapper_maps_to_exact_native_command(tmp_path, monkeypatch):
    path = wrapper(tmp_path)
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(subprocess, 'run', run)
    result = native_runner([str(path), 'send-message', NATIVE_ID, 'Read the Office synthetic task.'], 2)
    assert result.returncode == 0 and len(calls) == 1
    argv, settings = calls[0]
    assert argv == [r'C:\synthetic\language_server.exe', 'agentapi', 'send-message', NATIVE_ID, 'Read the Office synthetic task.']
    assert settings['shell'] is False and settings['timeout'] == 2
    assert settings['stdin'] == settings['stdout'] == settings['stderr'] == subprocess.DEVNULL
    assert settings['creationflags'] == getattr(subprocess, 'CREATE_NO_WINDOW', 0)


@pytest.mark.parametrize('line', [
    '"C:\\synthetic\\language_server.exe" agentapi %* & echo unsafe',
    '"C:\\synthetic\\other.exe" agentapi %*',
    'language_server.exe agentapi %*',
    '"C:\\synthetic\\language_server.exe" agentapi %*\necho extra',
])
def test_unsupported_batch_never_executes(tmp_path, line, monkeypatch):
    path = wrapper(tmp_path, line)
    calls = []
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: calls.append(a))
    with pytest.raises(CoordError):
        native_runner([str(path), 'send-message', NATIVE_ID, 'Synthetic task'], 2)
    assert calls == []


def test_real_synthetic_child_has_no_console_and_provider_output_is_discarded():
    code = ('import ctypes,os,sys; '
            'sys.exit(0 if os.name != "nt" or ctypes.windll.kernel32.GetConsoleWindow() == 0 else 8)')
    assert native_runner([sys.executable, '-c', code], 3).returncode == 0
    assert native_runner([sys.executable, '-c', 'raise SystemExit(9)'], 3).returncode == 9


def test_real_synthetic_timeout_is_bounded():
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        native_runner([sys.executable, '-c', 'import time; time.sleep(10)'], 1)
    assert time.monotonic() - started < 3


def request(now):
    stamp = lambda value: datetime.fromtimestamp(value, timezone.utc).isoformat()
    return {'conversation_id':NATIVE_ID, 'sender':'synthetic-sender', 'channel':'synthetic-channel',
            'prompt':'Read only the synthetic task in Office.', 'dedupe_key':'synthetic-once',
            'issued_at':stamp(now-1), 'expires_at':stamp(now+60), 'timeout_s':2}


def binding():
    return {'conversation_id':NATIVE_ID, 'host':'synthetic-host',
            'allowed_senders':['synthetic-sender'], 'allowed_channels':['synthetic-channel']}


@pytest.mark.parametrize('mode,expected', [
    ('idle','SUBMITTED_UNCONFIRMED'), ('nonzero','UNKNOWN'), ('timeout','UNKNOWN'),
    ('busy','DEFERRED'), ('unknown','REFUSED'), ('stop','REFUSED'),
    ('revoked','REFUSED'), ('duplicate','REFUSED'), ('wrong_id','REFUSED'), ('expired','REFUSED'),
])
def test_one_attempt_guards_and_unknown_never_claims_delivery(mode, expected):
    now = time.time()
    raw = request(now)
    if mode == 'wrong_id':
        raw['conversation_id'] = 'abcdefab-cdef-4abc-8def-1234567890ac'
    if mode == 'expired':
        raw = request(now-100)
    calls = []
    def run(argv, timeout):
        calls.append(argv)
        if mode == 'timeout':
            raise subprocess.TimeoutExpired('redacted', timeout)
        return SimpleNamespace(returncode=1 if mode == 'nonzero' else 0)
    out = attempt(raw, binding(), executable=sys.executable, local_host='synthetic-host',
                  observe_stop=lambda: mode == 'stop', observe_revoked=lambda: mode == 'revoked',
                  observe_target=lambda: mode if mode in {'busy','unknown'} else 'idle',
                  known_dedupe_keys={'synthetic-once'} if mode == 'duplicate' else (), runner=run, now=now)
    assert out.status == expected
    assert len(calls) == (1 if mode in {'idle','nonzero','timeout'} else 0)
    assert out.calls == len(calls)
    assert 'Read only' not in repr(out) and NATIVE_ID not in repr(out)


def test_busy_between_checks_blocks_real_seam():
    now = time.time()
    states = iter(['idle','busy'])
    calls = []
    out = attempt(request(now), binding(), executable=sys.executable, local_host='synthetic-host',
                  observe_stop=lambda: False, observe_revoked=lambda: False,
                  observe_target=lambda: next(states), known_dedupe_keys=(),
                  runner=lambda *args: calls.append(args), now=now)
    assert out.status == 'DEFERRED' and calls == []


def test_real_office_dispatch_is_fenced_before_native_call_and_never_resent(tmp_path, monkeypatch):
    from coordfs.antigravity_relay import NativeObservation, record_hook_state, submit_dispatch
    from coordfs.office import Office
    from coordfs.wake import ANTIGRAVITY_RELAY_DRIVER, Wake
    from coordfs.leases import format_ts
    monkeypatch.setattr('coordfs.wake._native_workspace_flavor', lambda: 'posix')
    home = tmp_path / 'coord'
    wake = Wake(home=home)
    sender = 'codex-ailab-00000000-0000-0000-0000-000000000003'
    receiver = 'antigravity-ailab-' + NATIVE_ID
    wake.enroll(binding_id='synthetic-ag', office_agent=receiver, driver=ANTIGRAVITY_RELAY_DRIVER,
                native_thread_id=NATIVE_ID, workspace=r'C:\synthetic\antigravity',
                enrolled_by='synthetic-owner', allowed_wakers=[sender], allowed_triggers=['message'],
                wake_kinds=['question'], expires_at=format_ts(time.time()+3600), owner_confirm=True)
    wake.verify('synthetic-ag', owner_confirm=True)
    sent = Office(agent_id=sender, home=home).send(to=receiver, body='Synthetic bounded task',
                                                kind='question', new_recipient=True)
    assert sent['status'] == 'ok'
    wake.tick(binding_id='synthetic-ag')
    pending = wake.relay_pending(binding_id='synthetic-ag', driver=ANTIGRAVITY_RELAY_DRIVER)
    assert pending['count'] == 1, pending
    did = pending['dispatches'][0]['dispatch_id']
    state_path = tmp_path / 'synthetic-native-state.json'
    record_hook_state(wake, state_path, event='Stop', payload={'conversationId':NATIVE_ID, 'fullyIdle':True})
    observation = NativeObservation(state_path, NATIVE_ID, now=wake.now)
    calls = []
    def run(argv, timeout):
        assert wake._load_dispatch(did)['status'] == 'delivery_unknown'
        calls.append(argv)
        return SimpleNamespace(returncode=0)
    kwargs = dict(relay_agent=sender, executable=sys.executable, local_host='synthetic-host',
                  channel='synthetic-channel', observation=observation, runner=run)
    result = submit_dispatch(wake, did, **kwargs)
    assert result['status'] == 'SUBMITTED_UNCONFIRMED', result
    assert len(calls) == 1
    assert submit_dispatch(wake, did, **kwargs)['reason'] == 'dispatch_fenced'
    assert len(calls) == 1
