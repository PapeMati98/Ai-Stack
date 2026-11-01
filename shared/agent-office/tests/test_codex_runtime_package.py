"""Codex wake must be usable from installed payloads, without a checkout."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import install_agent_office as installer
import office_server_package as server_package

REQUIRED = (
    'coordfs/codex_ack.py',
    'coordfs/codex_desktop_tool_host.py',
    'host/codex_app_transport.js',
)


def test_codex_delivery_modules_are_declared_runtime_sources():
    declared = dict(installer.RUNTIME_SOURCE_MAP)
    assert all(declared.get(rel) == rel for rel in REQUIRED)


def assert_usable_runtime(root):
    package = installer.verify_package()
    for rel in REQUIRED:
        assert (root / rel).read_bytes() == package.runtime_files[rel]
    code = (
        'import json,sys;sys.path.insert(0,sys.argv[1]);'
        'import coordfs.codex_ack as ack;'
        'import coordfs.codex_desktop_tool_host as host;'
        'import coordfs.office_ssh_gate;import coordfs.wake;'
        'print(json.dumps({"ack":ack.__file__,"host":host.__file__}))'
    )
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    process = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(root)],
                             capture_output=True, text=True, timeout=30,
                             creationflags=flags)
    assert process.returncode == 0, process.stderr
    paths = json.loads(process.stdout)
    assert Path(paths['ack']).resolve() == (root / REQUIRED[0]).resolve()
    assert Path(paths['host']).resolve() == (root / REQUIRED[1]).resolve()


@pytest.mark.skipif(os.name != 'nt', reason='real install requires Windows DPAPI')
def test_installed_runtime_contains_and_imports_codex_delivery(tmp_path):
    home = tmp_path / 'synthetic-user'
    home.mkdir()
    appdata = home / 'AppData' / 'Roaming'
    state = home / '.agents' / 'agent-office-deploy'
    result = installer.install(home, appdata, state, Path(sys.executable))
    assert result['status'] == 'DEPLOYED'
    assert installer.verify_install(home, appdata, state, Path(sys.executable))['status'] == 'CURRENT'
    assert_usable_runtime(home / installer.RUNTIME_RELATIVE)


def test_rendered_server_runtime_contains_and_imports_codex_delivery(tmp_path):
    out = tmp_path / 'inert-server-package'
    result = server_package.render(out, server_package.Params())
    assert result['code'] == 'package_rendered'
    package = installer.verify_package()
    assert_usable_runtime(out / 'runtime' / package.runtime_fingerprint[:12])
