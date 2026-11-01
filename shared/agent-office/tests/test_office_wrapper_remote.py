"""Exercise the generated Windows wrapper, not just a hand-built argv list."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import install_agent_office as installer


@pytest.fixture
def wrapper(tmp_path):
    launcher = tmp_path / "wrapper with spaces.cmd"
    home = tmp_path / "local office"
    launcher.write_bytes(installer.wrapper_bytes(Path(sys.executable), Path(__file__).parents[1], home))
    return launcher, home


def invoke(launcher, *args):
    # CMD strips the first/last quotes when /c sees multiple quoted paths.
    # Give /s a dedicated outer pair so the batch path keeps its own quotes.
    command = subprocess.list2cmdline([str(launcher), *args])
    result = subprocess.run('cmd.exe /d /s /c "' + command + '"',
                            capture_output=True, text=True, encoding="utf-8", timeout=15)
    assert result.stdout, (result.returncode, result.stderr)
    return result.returncode, json.loads(result.stdout)


@pytest.mark.skipif(os.name != "nt", reason="actual Windows cmd wrapper")
def test_generated_wrapper_remote_never_injects_local_home(wrapper, tmp_path):
    launcher, local = wrapper
    code, result = invoke(launcher, "--remote-config", str(tmp_path / "missing.json"), "read")
    assert (code, result["code"]) == (1, "invalid_remote_config"), result
    assert not local.exists()


@pytest.mark.skipif(os.name != "nt", reason="actual Windows cmd wrapper")
def test_generated_wrapper_preserves_bound_local_default(wrapper):
    launcher, local = wrapper
    code, result = invoke(launcher, "--harness", "codex", "--session-id", "wrapper-test",
                          "presence", "--status", "idle")
    assert code == 0, result
    assert result["status"] == "ok"
    assert local.is_dir()


@pytest.mark.skipif(os.name != "nt", reason="actual Windows cmd wrapper")
def test_explicit_local_home_still_conflicts_with_remote(wrapper, tmp_path):
    launcher, local = wrapper
    override = tmp_path / "caller-override"
    code, result = invoke(launcher, "--home", str(override), "--remote-config",
                          str(tmp_path / "missing.json"), "read")
    assert (code, result["code"]) == (1, "remote_home_conflict"), result
    assert not local.exists()
    assert not override.exists()
