"""Native SSH must never discover executables or credential files from a checkout."""
import json
from unittest.mock import patch

import pytest

from coordfs.errors import CoordError
from coordfs.office_cli_remote import parse_remote_config_bytes
from coordfs.office_remote import OfficeRemoteClient, OfficeRemoteConfig


@pytest.fixture
def native_config(tmp_path):
    return dict(host="office.example.test", user="agentoffice",
                ssh_executable=str(tmp_path / "owned" / "ssh.exe"),
                identity_file=str(tmp_path / "owned" / "key"),
                known_hosts_file=str(tmp_path / "owned" / "known_hosts"))


@pytest.mark.parametrize("field", ["ssh_executable", "identity_file", "known_hosts_file"])
def test_json_config_rejects_checkout_relative_paths(native_config, field):
    native_config[field] = "repo-controlled-file"
    with pytest.raises(CoordError, match="invalid remote configuration"):
        parse_remote_config_bytes(json.dumps(native_config).encode())


def test_json_config_requires_an_explicit_ssh_path(native_config):
    native_config.pop("ssh_executable")
    with pytest.raises(CoordError):
        parse_remote_config_bytes(json.dumps(native_config).encode())


@pytest.mark.parametrize("field", ["ssh_executable", "identity_file", "known_hosts_file"])
def test_native_transport_rejects_relative_paths_before_launch(native_config, field):
    native_config[field] = "repo-controlled-file"
    with patch("coordfs.office_remote.subprocess.Popen", side_effect=AssertionError("unsafe spawn")):
        with pytest.raises(ValueError, match="absolute"):
            OfficeRemoteClient(OfficeRemoteConfig(**native_config))


def test_native_config_validation_does_not_open_keys_or_search_path(native_config):
    with patch("pathlib.Path.open", side_effect=AssertionError("credential read")), \
         patch("shutil.which", side_effect=AssertionError("PATH lookup")):
        parsed = parse_remote_config_bytes(json.dumps(native_config).encode())
        client = OfficeRemoteClient(parsed)
    assert client._argv()[0] == native_config["ssh_executable"]


@pytest.mark.parametrize("value", [r"\\server\share\key", r"\\?\C:\keys\key",
                                   "//server/share/key", "C:/%OWNER%/key", "/home/$OWNER/key"])
def test_native_config_rejects_network_device_and_environment_shorthand(native_config, value):
    native_config["identity_file"] = value
    with pytest.raises(CoordError):
        parse_remote_config_bytes(json.dumps(native_config).encode())


@pytest.mark.parametrize("field", ["ssh_executable", "identity_file", "known_hosts_file"])
@pytest.mark.parametrize("value", [r"/\server\share\file", r"\/server/share/file",
                                   r"/\?\C:\owned\file", r"\/?/C:/owned/file"])
def test_mixed_separator_network_and_device_paths_are_refused(native_config, field, value):
    native_config[field] = value
    with pytest.raises(CoordError):
        parse_remote_config_bytes(json.dumps(native_config).encode())
    with patch("coordfs.office_remote.subprocess.Popen", side_effect=AssertionError("unsafe spawn")):
        with pytest.raises(ValueError, match="absolute"):
            OfficeRemoteClient(OfficeRemoteConfig(**native_config))
