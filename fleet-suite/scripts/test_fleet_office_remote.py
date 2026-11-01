"""Actual installed remote Office payload must reach MCP and Watch unchanged."""
import hashlib
import json
from pathlib import Path
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'payload/agent-fleet/scripts'))
import fleet

SOURCE = Path(__file__).resolve().parents[5] / 'shared/agent-office'
sys.path.insert(0, str(SOURCE))
import install_agent_office as installer


@pytest.fixture
def runtime(tmp_path):
    root = tmp_path / 'runtime'
    config = dict(host='office.example.test', user='agentoffice', ssh_executable=str(tmp_path/'ssh.exe'),
                  identity_file=str(tmp_path/'not-read-key'), known_hosts_file=str(tmp_path/'not-read-hosts'))
    package = installer.verify_package()
    files = installer.runtime_payload(package, '0'*32, Path(sys.executable), root,
                                      tmp_path/'unused-local-office', remote_config=config)
    for name, data in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return root


def mcp(root):
    return fleet.office_mcp_server_config(harness='claude', session_id='synthetic',
                                         channels=['synthetic-team'], parent='codex-parent', office=root/'office.cmd')


def watch(root):
    with patch.object(fleet,'office_command_path',return_value=root/'office.cmd'), \
         patch.object(fleet,'office_integration_enabled',return_value=True):
        return fleet._watch_office_runtime_argv()


def marker(root, **changes):
    path = root / '.agent-office-runtime.json'
    data = json.loads(path.read_bytes()) | changes
    path.write_text(json.dumps(data), encoding='utf-8')


def test_actual_remote_runtime_inherits_identical_route_and_identity(runtime):
    server = mcp(runtime)
    args = server['args']
    assert args[args.index('--remote-config')+1] == str(runtime/'office-remote.json')
    assert args.index('--remote-config') < args.index('mcp')
    assert '--home' not in args
    assert args[args.index('--session-id')+1] == 'synthetic'
    assert args[args.index('--channel')+1] == 'synthetic-team'
    assert args[args.index('--peer')+1] == 'codex-parent'
    argv, reason = watch(runtime)
    assert reason is None and '--remote-config' in argv
    assert argv[argv.index('--remote-config')+1] == str(runtime/'office-remote.json')


@pytest.mark.parametrize('pin', [None, '', 'xyz', 123])
def test_invalid_remote_pin_cannot_select_local_store(runtime, pin):
    marker(runtime, remote_config_sha256=pin)
    assert mcp(runtime) is None
    assert watch(runtime)[0] is None


@pytest.mark.parametrize('payload', [b'{', b'[]', b'{"x":NaN}', b'{"host":"x","host":"y"}',
                                      b'{}', b'x'*(16*1024+1)])
def test_even_pinned_malformed_or_oversized_json_refused(runtime, payload):
    (runtime/'office-remote.json').write_bytes(payload)
    marker(runtime, remote_config_sha256=hashlib.sha256(payload).hexdigest())
    assert mcp(runtime) is None and watch(runtime)[0] is None


def test_tampering_missing_and_present_unpinned_config_refused(runtime):
    (runtime/'office-remote.json').write_bytes(b'{}')
    assert mcp(runtime) is None and watch(runtime)[0] is None
    (runtime/'office-remote.json').rename(runtime/'retained-remote.json')
    assert mcp(runtime) is None and watch(runtime)[0] is None
    data = json.loads((runtime/'.agent-office-runtime.json').read_bytes())
    data.pop('remote_config_sha256')
    (runtime/'.agent-office-runtime.json').write_text(json.dumps(data),encoding='utf-8')
    (runtime/'office-remote.json').mkdir()
    assert mcp(runtime) is None and watch(runtime)[0] is None


def test_malformed_marker_does_not_become_a_local_marker(runtime):
    (runtime/'.agent-office-runtime.json').write_bytes(b'{')
    assert mcp(runtime) is None and watch(runtime)[0] is None


def test_unrelated_extra_runtime_file_remains_unverified(runtime):
    (runtime/'arbitrary.py').write_bytes(b'pass')
    assert watch(runtime) == (None, 'office-runtime-unverified')


def test_config_link_refused_without_reading_link_target(runtime):
    path = runtime/'office-remote.json'
    path.rename(runtime/'retained-config.json')
    try:
        path.symlink_to(runtime/'retained-config.json')
    except OSError:
        pytest.skip('Host lacks filesystem symlink capability')
    assert mcp(runtime) is None and watch(runtime)[0] is None


def test_markerless_legacy_mcp_remains_local(tmp_path):
    (tmp_path/'office.py').write_bytes(b'pass')
    assert '--remote-config' not in mcp(tmp_path)['args']
