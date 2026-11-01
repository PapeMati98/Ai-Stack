import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
import broker
import managed
import owner_autostart
import owner_probe
import owner_start


ORIGIN = "https://example.test"
SAVED_LOGIN = {"origin": ORIGIN, "pages": ["/login"], "posts": ["/session"]}


class Clock:
    def __init__(self):
        self.value = 0.0

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class FakeProbes:
    def __init__(self, broker_running=False, window_running=False, unknown=False, config_path="",
                 command_line=None, command_unknown=False):
        self.broker_running = broker_running
        self.window_running = window_running
        self.unknown = unknown
        self.config_path = config_path
        self.command = command_line
        self.command_unknown = command_unknown

    def processes(self):
        return [{"pid": 11, "name": "python.exe"}] if (
            self.broker_running or self.command is not None or self.command_unknown
        ) else []

    def command_line(self, pid):
        if self.command_unknown:
            return None
        if self.command is not None:
            return self.command
        return f'python "C:\\runtime files\\managed.py" broker --config "{self.config_path}"' if self.broker_running else None

    def profile_users(self, profile):
        return {
            "matches": [{"pid": 22}] if self.window_running else [],
            "unknown_chrome_pids": [33] if self.unknown else [],
        }


class Reject(Exception):
    pass


class FakeController:
    def __init__(self, reject_count=0):
        self.reject_count = reject_count
        self.registered = []
        self.released = []

    def register(self, harness, transport):
        if self.reject_count:
            self.reject_count -= 1
            raise Reject("not ready")
        self.registered.append((harness, transport))
        return "session", "capability"

    def release(self, sid, token):
        self.released.append((sid, token))
        return True


class FakePopen:
    def __init__(self, extension_ready=True, browser_error=False, breakaway_once=False, identity=False):
        self.calls = []
        self.extension_ready = extension_ready
        self.browser_error = browser_error
        self.breakaway_once = breakaway_once
        self.identity = identity
        self.extension_config_read = False
        self.next_pid = 100

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), dict(kwargs)))
        flags = kwargs["creationflags"]
        if self.breakaway_once and flags & getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000):
            self.breakaway_once = False
            raise OSError("job does not permit breakaway")
        if Path(argv[0]).name.lower().startswith("node"):
            setup = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
            episode = Path(argv[2]).parent
            if self.browser_error:
                (episode / "owner-browser-error.json").write_text('{"status":"FAILED"}', encoding="utf-8")
            elif self.identity:
                extension_id = "b" * 32
                if extension_id != setup["expected_extension_id"]:
                    (episode / "owner-browser-error.json").write_text(
                        '{"status":"FAILED","error_type":"identity"}', encoding="utf-8")
                else:
                    self.extension_config_read = True
            elif self.extension_ready:
                self.extension_config_read = True
                extension_id = "a" * 32
                (episode / "owner-extension-ready.json").write_text(
                    json.dumps({"extension_id": extension_id, "build": "owner-start-1"}), encoding="utf-8")
                (episode / "owner-browser-ready.json").write_text(
                    json.dumps({"status": "OWNER_ATTACH_REQUIRED"}), encoding="utf-8")
        process = SimpleNamespace(pid=self.next_pid, poll=lambda: None)
        self.next_pid += 1
        return process


def _write_managed_config(path, state_dir, grant_id="owner-test", origins=None):
    origins = list(origins or [ORIGIN])
    grant = {
        "grant_id": grant_id,
        "profile_id": "owner-profile",
        "allowed_origins": origins,
        "allowed_actions": sorted(broker.ACTIONS),
        "extension_id": "a" * 32,
        "persistent": True,
        "synthetic": True,
        "saved_login": SAVED_LOGIN,
        "harnesses": {"codex": {"transport": "cli"}},
    }
    raw = {
        "schema": managed.SCHEMA,
        "grant": grant,
        "broker_base": "http://127.0.0.1:8123",
        "state_dir": str(state_dir),
        "extension_token": "E" * 32,
        "controller_key": "K" * 40,
        "client_ttl_s": 60,
    }
    path.write_text(json.dumps(raw), encoding="utf-8")


def make_config(tmp_path, extension_config=True):
    root = tmp_path / "owner runtime with spaces"
    root.mkdir()
    runtime = SOURCE
    extension = root / "extension"
    extension.mkdir()
    source_extension = runtime / "extension"
    for name in owner_start.EXTENSION_FILES:
        (extension / name).write_bytes((source_extension / name).read_bytes())
    private = root / "private data"
    private.mkdir()
    profile = root / "profile data"
    profile.mkdir()
    playwright = root / "playwright modules"
    playwright.mkdir()
    (root / "node.exe").write_bytes(b"fake node")
    (root / "python.exe").write_bytes(b"fake python")
    browser = root / "chrome.exe"
    browser.write_bytes(b"fake browser")
    hidden = root / "HiddenLaunch.exe"
    hidden.write_bytes(b"pinned test launcher")
    managed_config = private / "managed-config.json"
    _write_managed_config(managed_config, private / "state")
    if extension_config:
        current = managed.config_from_dict(json.loads(managed_config.read_text(encoding="utf-8")))
        (private / "extension-config.json").write_text(
            json.dumps(managed.extension_config(current)), encoding="utf-8")
    controller_spec = private / "controller-action.xml"
    controller_spec.write_text(
        '<hidden-launch version="1" isolate-desktop="true" log-directory="' + str(private / "broker logs")
        + '"><action executable="' + str(root / "python.exe") + '" arguments="-B &quot;'
        + str(runtime / "managed.py") + '&quot; broker --config &quot;' + str(managed_config)
        + '&quot;" /></hidden-launch>', encoding="utf-8")
    config = {
        "schema": owner_start.SCHEMA,
        "python": str(root / "python.exe"),
        "node": str(root / "node.exe"),
        "hidden_launch": str(hidden),
        "hidden_launch_sha256": hashlib.sha256(hidden.read_bytes()).hexdigest(),
        "broker_runtime": str(runtime),
        "extension_dir": str(extension),
        "private_dir": str(private),
        "managed_config": str(managed_config),
        "controller_spec": str(controller_spec),
        "browser_exe": str(browser),
        "profile_path": str(profile),
        "playwright_root": str(playwright),
        "grant_id": "owner-test",
        "expected_origins": [ORIGIN],
        "profile_id": "owner-profile",
        "saved_login": SAVED_LOGIN,
        "start_url": ORIGIN + "/",
    }
    return config


def run_start(config, probes=None, popen=None, clock=None, desktop=lambda: "Default", mode="manual",
              port_listening=lambda port: False):
    clock = clock or Clock()
    probes = probes or FakeProbes(config_path=config["managed_config"])
    popen = popen or FakePopen()
    controller = FakeController()
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        result = owner_start.run(
            config, mode, probes=probes, popen=popen,
            controller_factory=lambda current: controller,
            clock=clock.now, sleep=clock.sleep, desktop=desktop, port_listening=port_listening,
        )
    return result, output.getvalue(), popen, controller, clock


def test_both_running_returns_without_popen(tmp_path):
    config = make_config(tmp_path)
    probes = FakeProbes(True, True, config_path=config["managed_config"])
    result, _, popen, _, _ = run_start(config, probes=probes)
    assert (result["status"], result["exit_code"]) == ("ALREADY_RUNNING", 0)
    assert popen.calls == []


def test_neither_running_starts_both_and_logs_no_paths_or_argv(tmp_path):
    config = make_config(tmp_path)
    result, output, popen, controller, _ = run_start(config)
    assert (result["status"], result["exit_code"]) == ("STARTED", 0)
    assert result["started"] == ["window", "broker"]
    assert len(popen.calls) == 2
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    assert all(call[1]["creationflags"] & no_window for call in popen.calls)
    assert all(call[1]["shell"] is False for call in popen.calls)
    assert controller.registered == [("codex", "cli")]
    assert controller.released == [("session", "capability")]
    log_path = Path(config["private_dir"]) / "owner-start.log"
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["status"] == "STARTED"
    assert row["started"] == ["window", "broker"]
    assert row["breakaway"] == {"window": True, "broker": True}
    assert config["managed_config"] not in lines[0]
    assert "argv" not in lines[0]
    assert config["managed_config"] not in output


@pytest.mark.parametrize(
    ("broker_running", "window_running", "expected"),
    [(True, False, ["window"]), (False, True, ["broker"])],
)
def test_partial_state_starts_only_missing_component(tmp_path, broker_running, window_running, expected):
    config = make_config(tmp_path)
    probes = FakeProbes(broker_running, window_running, config_path=config["managed_config"])
    result, _, popen, _, _ = run_start(config, probes=probes)
    assert result["status"] == "STARTED"
    assert result["started"] == expected
    assert len(popen.calls) == 1


@pytest.mark.parametrize("gate", [
    "desktop", "launcher_pin", "extension_drift", "grant_drift", "controller_spec",
    "profile_unknown", "extension_config_missing", "extension_config_drift",
])
def test_gates_fail_closed_without_popen(tmp_path, gate):
    config = make_config(tmp_path, extension_config=gate != "extension_config_missing")
    desktop = lambda: "Default"
    probes = FakeProbes(config_path=config["managed_config"])
    expected_detail = gate
    if gate == "desktop":
        desktop = lambda: "AgentStackBackground"
    elif gate == "launcher_pin":
        config["hidden_launch_sha256"] = "0" * 64
    elif gate == "extension_drift":
        (Path(config["extension_dir"]) / "sw.js").write_bytes(b"drift")
    elif gate == "grant_drift":
        _write_managed_config(Path(config["managed_config"]), Path(config["private_dir"]) / "state", grant_id="other-grant")
    elif gate == "controller_spec":
        Path(config["controller_spec"]).write_text(
            '<hidden-launch version="1" isolate-desktop="true" log-directory="' + str(Path(config["private_dir"]) / "broker logs")
            + '"><action executable="' + config["python"]
            + '" arguments="-B &quot;' + str(Path(config["broker_runtime"]) / "managed.py")
            + '&quot; broker --config &quot;C:\\wrong\\managed-config.json&quot;" /></hidden-launch>',
            encoding="utf-8")
    elif gate == "profile_unknown":
        probes.unknown = True
    elif gate == "extension_config_drift":
        expected_detail = "extension_config"
        (Path(config["private_dir"]) / "extension-config.json").write_text("{}", encoding="utf-8")
    elif gate == "extension_config_missing":
        expected_detail = "extension_config"
    result, _, popen, _, _ = run_start(config, probes=probes, desktop=desktop)
    assert (result["status"], result["detail"], result["exit_code"]) == ("BLOCKED", expected_detail, 2)
    assert popen.calls == []


def test_extension_config_gate_preserves_json_value_types(tmp_path):
    config = make_config(tmp_path)
    extension_path = Path(config["private_dir"]) / "extension-config.json"
    extension_config = json.loads(extension_path.read_text(encoding="utf-8"))
    extension_config["persistent"] = 1
    extension_path.write_text(json.dumps(extension_config), encoding="utf-8")
    result, _, popen, _, _ = run_start(config)
    assert (result["status"], result["detail"]) == ("BLOCKED", "extension_config")
    assert popen.calls == []


def test_lock_contention_returns_already_starting(tmp_path):
    config = make_config(tmp_path)
    held = owner_start._acquire_lock(config["private_dir"])
    try:
        result, _, popen, _, _ = run_start(config)
    finally:
        owner_start._release_lock(held)
    assert (result["status"], result["exit_code"]) == ("ALREADY_STARTING", 0)
    assert popen.calls == []


def test_owner_probe_fails_closed_when_chrome_liveness_is_unknown(monkeypatch):
    monkeypatch.setattr(owner_probe, "_require_windows", lambda: None)
    monkeypatch.setattr(owner_probe, "processes", lambda: [{"pid": 77, "name": "chrome.exe"}])
    monkeypatch.setattr(owner_probe, "_live", lambda pid: None)
    assert owner_probe.profile_users(r"C:\\owner-profile") == {
        "matches": [], "unknown_chrome_pids": [77],
    }


def test_owner_probe_normalizes_profile_path_before_matching(monkeypatch):
    monkeypatch.setattr(owner_probe, "_require_windows", lambda: None)
    monkeypatch.setattr(owner_probe, "processes", lambda: [{"pid": 77, "name": "chrome.exe"}])
    monkeypatch.setattr(owner_probe, "_live", lambda pid: True)
    monkeypatch.setattr(owner_probe, "command_line", lambda pid: 'chrome.exe --user-data-dir="C:/Owner Data/Profile"')
    assert owner_probe.profile_users(r"c:\owner data\profile") == {
        "matches": [{"pid": 77}], "unknown_chrome_pids": [],
    }


@pytest.mark.parametrize("path", [
    r"C:\Users\owner\AppData\Local\Google\Chrome\User Data\Default",
    r"C:\Users\owner\AppData\Local\Microsoft\Edge\User Data\Default",
])
def test_validate_config_rejects_private_browser_profiles(tmp_path, path):
    config = make_config(tmp_path)
    config["profile_path"] = path
    with pytest.raises(owner_start.ConfigError):
        owner_start.validate_config(config)


@pytest.mark.parametrize(
    ("kwargs", "detail"),
    [({"extension_ready": False}, "extension_ready"), ({"browser_error": True}, "browser_launch")],
)
def test_browser_readiness_failures_are_reported(tmp_path, kwargs, detail):
    config = make_config(tmp_path)
    popen = FakePopen(**kwargs)
    clock = Clock()
    result, _, _, _, _ = run_start(config, popen=popen, clock=clock)
    assert (result["status"], result["detail"], result["exit_code"]) == ("FAILED", detail, 1)
    if detail == "extension_ready":
        assert clock.value >= 60


def test_setup_contains_expected_extension_identity(tmp_path):
    config = make_config(tmp_path)
    result, _, _, _, _ = run_start(config)
    setup = json.loads((Path(config["private_dir"]) / "owner-episodes" / result["episode"] / "setup.json").read_text(encoding="utf-8"))
    assert setup["expected_extension_id"] == "a" * 32


def test_identity_mismatch_fails_browser_launch_without_reading_extension_config(tmp_path):
    config = make_config(tmp_path)
    popen = FakePopen(identity=True)
    result, _, _, _, _ = run_start(config, popen=popen)
    episode = Path(config["private_dir"]) / "owner-episodes" / result["episode"]
    error = json.loads((episode / "owner-browser-error.json").read_text(encoding="utf-8"))
    assert (result["status"], result["detail"]) == ("FAILED", "browser_launch")
    assert error["error_type"] == "identity"
    assert popen.extension_config_read is False


@pytest.mark.parametrize("command", [
    'python.exe "C:\\runtime files\\managed.py" mcp --config "{config}"',
    'python.exe "C:\\runtime files\\managed.py" broker --config "C:\\other\\managed.json"',
])
def test_detect_ignores_mcp_and_broker_with_other_config(tmp_path, command):
    config = make_config(tmp_path)
    probes = FakeProbes(command_line=command.format(config=config["managed_config"]))
    assert owner_start._detect(config, probes) == (False, False, None)


def test_detect_matches_quoted_managed_path_with_spaces(tmp_path):
    config = make_config(tmp_path)
    command = 'python.exe "C:\\owner runtime with spaces\\managed.py" broker --config "' + config["managed_config"] + '"'
    probes = FakeProbes(command_line=command)
    assert owner_start._detect(config, probes) == (True, False, None)


def test_detect_skips_unreadable_python_command_line(tmp_path):
    config = make_config(tmp_path)
    probes = FakeProbes(command_unknown=True)
    assert owner_start._detect(config, probes) == (False, False, None)


def test_unidentified_listener_on_broker_port_blocks_without_controller_contact(tmp_path):
    config = make_config(tmp_path)
    probes = FakeProbes(False, True, config_path=config["managed_config"], command_unknown=True)
    result, _, popen, controller, _ = run_start(config, probes=probes, port_listening=lambda port: True)
    assert (result["status"], result["detail"], result["exit_code"]) == ("BLOCKED", "broker_unknown", 2)
    assert popen.calls == []
    assert controller.registered == []


def test_detect_matching_broker_wins_over_unreadable_python(tmp_path):
    config = make_config(tmp_path)
    rows = [
        {"pid": 11, "name": "python.exe"},
        {"pid": 12, "name": "python313.exe"},
    ]

    class MixedProbes:
        def processes(self):
            return rows

        def command_line(self, pid):
            if pid == 11:
                return ('python.exe "C:\\owner runtime\\managed.py" broker --config "'
                        + config["managed_config"] + '"')
            return None

        def profile_users(self, profile):
            return {"matches": [], "unknown_chrome_pids": []}

    assert owner_start._detect(config, MixedProbes()) == (True, False, None)


def test_status_unknown_returns_nonzero_from_main(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    config_path = Path(config["private_dir"]) / "owner-start.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    original_import = owner_start.importlib.import_module

    def import_module(name):
        if name == "owner_probe":
            return FakeProbes(unknown=True)
        return original_import(name)

    monkeypatch.setattr(owner_start.importlib, "import_module", import_module)
    assert owner_start.main(["--status", "--config", str(config_path)]) == 1
    row = json.loads(capsys.readouterr().out)
    assert row == {"state": "unknown", "detail": "profile_unknown"}


def test_controller_factory_imports_runtime_only_when_used(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    calls = []
    monkeypatch.setattr(owner_start, "_load_managed", lambda runtime: (
        calls.append(runtime) or SimpleNamespace(Controller=lambda current: "controller")
    ))
    factory = owner_start._load_controller(config)
    assert calls == []
    assert factory(object()) == "controller"
    assert calls == [config["broker_runtime"]]


def test_main_reports_missing_runtime_to_jsonl(tmp_path, capsys):
    config = make_config(tmp_path)
    config["broker_runtime"] = str(tmp_path / "missing runtime")
    config_path = Path(config["private_dir"]) / "owner-start.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    assert owner_start.main(["--start", "--config", str(config_path)]) == 1
    output = json.loads(capsys.readouterr().out)
    log = json.loads((Path(config["private_dir"]) / "owner-start.log").read_text(encoding="utf-8").splitlines()[-1])
    assert (output["status"], output["detail"]) == ("FAILED", "runtime")
    assert (log["status"], log["detail"]) == ("FAILED", "runtime")


def test_autostart_waits_for_config_file_to_appear(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    config_path = Path(config["private_dir"]) / "delayed owner-start.json"
    clock = Clock()
    slept = []
    controller = FakeController()
    popen = FakePopen()
    original_import = owner_start.importlib.import_module

    def import_module(name):
        if name == "owner_probe":
            return FakeProbes(config_path=config["managed_config"])
        return original_import(name)

    def appear(seconds):
        slept.append(seconds)
        clock.sleep(seconds)
        config_path.write_text(json.dumps(config), encoding="utf-8")

    monkeypatch.setattr(owner_start.importlib, "import_module", import_module)
    monkeypatch.setattr(owner_start, "_load_uat", lambda runtime: SimpleNamespace(current_desktop=lambda: "Default"))
    monkeypatch.setattr(owner_start, "_load_controller", lambda current: lambda value: controller)
    monkeypatch.setattr(owner_start.subprocess, "Popen", popen)
    result = owner_start.main(["--start", "--autostart", "--config", str(config_path)],
                              clock=clock.now, sleep=appear)
    assert result == 0
    assert slept
    assert clock.value >= 1
    assert json.loads(capsys.readouterr().out)["status"] == "STARTED"


def test_autostart_waits_for_prerequisite_then_starts(tmp_path):
    config = make_config(tmp_path)
    missing_node = Path(config["node"] + ".pending")
    config["node"] = str(missing_node)
    clock = Clock()
    slept = []

    def appear(seconds):
        slept.append(seconds)
        clock.sleep(seconds)
        missing_node.write_bytes(b"node appeared")

    popen = FakePopen()
    controller = FakeController()
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        result = owner_start.run(
            config, "autostart", probes=FakeProbes(config_path=config["managed_config"]),
            popen=popen, controller_factory=lambda current: controller,
            clock=clock.now, sleep=appear, desktop=lambda: "Default",
        )
    assert slept
    assert result["status"] == "STARTED"


def test_autostart_prerequisite_timeout_is_failed_without_popen(tmp_path):
    config = make_config(tmp_path)
    config["node"] = config["node"] + ".missing"
    clock = Clock()
    result, _, popen, _, _ = run_start(config, clock=clock, mode="autostart")
    assert (result["status"], result["detail"], result["exit_code"]) == ("FAILED", "prerequisites", 1)
    assert clock.value >= 180
    assert popen.calls == []


def test_breakaway_error_retries_once_without_breakaway(tmp_path):
    config = make_config(tmp_path)
    popen = FakePopen(breakaway_once=True)
    result, _, _, _, _ = run_start(config, popen=popen)
    assert result["status"] == "STARTED"
    flags = [call[1]["creationflags"] for call in popen.calls[:2]]
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
    assert flags[0] == (no_window | breakaway)
    assert flags[1] == no_window
    row = json.loads((Path(config["private_dir"]) / "owner-start.log").read_text(encoding="utf-8").splitlines()[-1])
    assert row["breakaway"] == {"window": False, "broker": True}


def _xml_values(parent, name):
    return [node.text for node in parent.iter() if node.tag.rsplit("}", 1)[-1] == name]


def test_autostart_plan_xml_is_valid_and_quotes_paths_with_spaces(tmp_path):
    config = make_config(tmp_path)
    config_path = Path(config["private_dir"]) / "owner config.json"
    config_path.write_text("{}", encoding="utf-8")
    runtime = str(Path(config["private_dir"]) / "runtime files")
    task_xml, launch_xml = owner_autostart.build_xml(config, runtime, "Owner Chrome Relay", "DOMAIN\\user", str(config_path))
    task = ET.fromstring(task_xml)
    launch = ET.fromstring(launch_xml)
    assert _xml_values(task, "LogonTrigger")
    assert "PT30S" in _xml_values(task, "Delay")
    assert "InteractiveToken" in _xml_values(task, "LogonType")
    assert "LeastPrivilege" in _xml_values(task, "RunLevel")
    assert "IgnoreNew" in _xml_values(task, "MultipleInstancesPolicy")
    assert "false" in [value.casefold() for value in _xml_values(task, "Enabled")]
    assert _xml_values(task, "Command") == [config["hidden_launch"]]
    assert _xml_values(task, "Arguments") == ['"' + str(Path(config["private_dir"]) / "owner-start-action.xml") + '"']
    assert "isolate-desktop" not in launch.attrib
    action = next(node for node in launch.iter() if node.tag == "action")
    assert '"' + str(config_path) + '"' in action.attrib["arguments"]
    assert '"' + str(Path(runtime) / "owner_start.py") + '"' in action.attrib["arguments"]


def test_autostart_apply_creates_disabled_verifies_then_enables(tmp_path):
    config = make_config(tmp_path)
    config_path = str(Path(config["private_dir"]) / "owner config.json")
    task_xml, _ = owner_autostart.build_xml(config, str(SOURCE), "Owner Chrome Relay", "DOMAIN\\user", config_path)
    events = []
    query_count = 0

    def runner(argv, **kwargs):
        nonlocal query_count
        if argv[0] == "whoami.exe":
            return SimpleNamespace(returncode=0, stdout='"domain\\user","S-1-5-21-1-2-3-1001"\n', stderr="")
        events.append(argv[1])
        assert kwargs["shell"] is False
        assert kwargs["timeout"] == 30
        assert kwargs["creationflags"] & getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        if argv[1] == "/Query":
            query_count += 1
            if query_count == 1:
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            return SimpleNamespace(returncode=0, stdout=task_xml, stderr="")
        if argv[1] == "/Create":
            written = Path(argv[3]).read_bytes().decode("utf-16")
            assert "<Enabled>false</Enabled>" in written
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[1] == "/Change":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(argv)

    result = owner_autostart.apply(config, str(SOURCE), "Owner Chrome Relay", "DOMAIN\\user", config_path, runner=runner)
    assert result["status"] == "ENABLED"
    assert events == ["/Query", "/Create", "/Query", "/Change"]
    assert events[-1] == "/Change"


def test_autostart_apply_refuses_existing_task(tmp_path):
    config = make_config(tmp_path)
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="<Task />", stderr="")

    with pytest.raises(owner_autostart.TaskError) as error:
        owner_autostart.apply(config, str(SOURCE), "Owner Chrome Relay", "DOMAIN\\user", str(Path(config["private_dir"]) / "owner.json"), runner=runner)
    assert error.value.exit_code == 2
    assert error.value.detail == "task_exists"
    assert len(calls) == 1
    assert calls[0][1] == "/Query"


def test_autostart_apply_bad_launcher_pin_never_creates_task(tmp_path):
    config = make_config(tmp_path)
    Path(config["hidden_launch"]).write_bytes(b"changed launcher")
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    with pytest.raises(owner_autostart.TaskError) as error:
        owner_autostart.apply(config, str(SOURCE), "Owner Chrome Relay", "DOMAIN\\user",
                              str(Path(config["private_dir"]) / "owner.json"), runner=runner)
    assert (error.value.detail, error.value.exit_code) == ("launcher_pin", 2)
    assert not any(argv[1] == "/Create" for argv in calls)


def test_autostart_apply_rechecks_launcher_pin_before_create(tmp_path):
    config = make_config(tmp_path)
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "/Query":
            Path(config["hidden_launch"]).write_bytes(b"changed during preflight")
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        raise AssertionError(argv)

    with pytest.raises(owner_autostart.TaskError) as error:
        owner_autostart.apply(config, str(SOURCE), "Owner Chrome Relay", "DOMAIN\\user",
                              str(Path(config["private_dir"]) / "owner.json"), runner=runner)
    assert (error.value.detail, error.value.exit_code) == ("launcher_pin", 2)
    assert [argv[1] for argv in calls] == ["/Query"]


def test_autostart_apply_mismatch_never_enables_task(tmp_path):
    config = make_config(tmp_path)
    config_path = str(Path(config["private_dir"]) / "owner.json")
    task_xml, _ = owner_autostart.build_xml(config, str(SOURCE), "Owner Chrome Relay", "DOMAIN\\user", config_path)
    mismatched = task_xml.replace("LeastPrivilege", "HighestAvailable")
    query_count = 0
    events = []

    def runner(argv, **kwargs):
        nonlocal query_count
        if argv[0] == "whoami.exe":
            return SimpleNamespace(returncode=0, stdout='"domain\\user","S-1-5-21-1-2-3-1001"\n', stderr="")
        events.append(argv[1])
        if argv[1] == "/Query":
            query_count += 1
            if query_count == 1:
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            return SimpleNamespace(returncode=0, stdout=mismatched, stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with pytest.raises(owner_autostart.TaskError) as error:
        owner_autostart.apply(config, str(SOURCE), "Owner Chrome Relay", "DOMAIN\\user", config_path, runner=runner)
    assert error.value.detail == "task_xml_mismatch"
    assert events == ["/Query", "/Create", "/Query"]


def test_autostart_verify_accepts_real_export_shape(tmp_path):
    config = make_config(tmp_path)
    action_path = Path(config["private_dir"]) / "owner-start-action.xml"
    ns = "http://schemas.microsoft.com/windows/2004/02/mit/task"
    exported = (
        '<?xml version="1.0" encoding="UTF-16"?>\n<Task version="1.2" xmlns="' + ns + '">'
        "<RegistrationInfo><URI>\\Owner Chrome Relay</URI></RegistrationInfo>"
        "<Triggers><LogonTrigger><Delay>PT30S</Delay><UserId>DOMAIN\\user</UserId></LogonTrigger></Triggers>"
        '<Principals><Principal id="Author"><UserId>S-1-5-21-1-2-3-1001</UserId>'
        "<LogonType>InteractiveToken</LogonType></Principal></Principals>"
        "<Settings><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>"
        "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><ExecutionTimeLimit>PT15M</ExecutionTimeLimit>"
        "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><Enabled>false</Enabled>"
        "<IdleSettings><StopOnIdleEnd>true</StopOnIdleEnd></IdleSettings></Settings>"
        '<Actions Context="Author"><Exec><Command>' + config["hidden_launch"] + "</Command>"
        "<Arguments>\"" + str(action_path) + "\"</Arguments></Exec></Actions></Task>"
    )
    assert owner_autostart.verify_task_xml(exported, config, action_path, "DOMAIN\\user", sid="S-1-5-21-1-2-3-1001")
    with pytest.raises(owner_autostart.TaskError):
        owner_autostart.verify_task_xml(exported, config, action_path, "DOMAIN\\user", sid="S-1-5-21-9-9-9-1001")
    with pytest.raises(owner_autostart.TaskError):
        owner_autostart.verify_task_xml(exported.replace("<Enabled>false</Enabled>", ""), config, action_path,
                                        "DOMAIN\\user", sid="S-1-5-21-1-2-3-1001")
    assert owner_autostart.verify_task_xml(exported.replace("<Enabled>false</Enabled>", ""), config, action_path,
                                           "DOMAIN\\user", sid="S-1-5-21-1-2-3-1001", enabled=True)
    assert owner_autostart.verify_task_xml(
        exported.replace("<UserId>DOMAIN\\user</UserId>", "<UserId>S-1-5-21-1-2-3-1001</UserId>", 1),
        config, action_path, "DOMAIN\\user", sid="S-1-5-21-1-2-3-1001")
    with pytest.raises(owner_autostart.TaskError):
        owner_autostart.verify_task_xml(exported.replace("<Delay>PT30S</Delay>", "<Delay>PT30S</Delay><Repetition />"),
                                        config, action_path, "DOMAIN\\user", sid="S-1-5-21-1-2-3-1001")


@pytest.mark.parametrize(("setting", "value"), [
    ("Hidden", "true"), ("WakeToRun", "true"), ("RunOnlyIfIdle", "true"),
    ("RunOnlyIfNetworkAvailable", "true"), ("DeleteExpiredTaskAfter", "PT1H"),
])
def test_autostart_verify_rejects_unsafe_settings(tmp_path, setting, value):
    config = make_config(tmp_path)
    action_path = Path(config["private_dir"]) / "owner-start-action.xml"
    ns = "http://schemas.microsoft.com/windows/2004/02/mit/task"
    exported = (
        '<Task version="1.2" xmlns="' + ns + '"><Triggers><LogonTrigger><Delay>PT30S</Delay>'
        "<UserId>DOMAIN\\user</UserId></LogonTrigger></Triggers>"
        "<Principals><Principal><UserId>S-1-5-21-1-2-3-1001</UserId><LogonType>InteractiveToken</LogonType>"
        "</Principal></Principals><Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>"
        "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>"
        "<ExecutionTimeLimit>PT15M</ExecutionTimeLimit><StartWhenAvailable>false</StartWhenAvailable>"
        "<Enabled>false</Enabled></Settings><Actions><Exec><Command>" + config["hidden_launch"] + "</Command>"
        "<Arguments>\"" + str(action_path) + "\"</Arguments></Exec></Actions></Task>"
    )
    altered = exported.replace("</Settings>", f"<{setting}>{value}</{setting}></Settings>")
    with pytest.raises(owner_autostart.TaskError):
        owner_autostart.verify_task_xml(altered, config, action_path, "DOMAIN\\user",
                                        sid="S-1-5-21-1-2-3-1001")


def test_task_status_reports_xml_mismatch(tmp_path):
    config = make_config(tmp_path)
    config_path = Path(config["private_dir"]) / "owner config.json"
    action_path = Path(config["private_dir"]) / "owner-start-action.xml"
    task_xml, _ = owner_autostart.build_xml(config, str(SOURCE), "Owner Chrome Relay", "DOMAIN\\user", str(config_path))
    mismatched = task_xml.replace("</Settings>", "<WakeToRun>true</WakeToRun></Settings>")

    def runner(argv, **kwargs):
        if argv[0] == "whoami.exe":
            return SimpleNamespace(returncode=0, stdout='"domain\\user","S-1-5-21-1-2-3-1001"\n', stderr="")
        return SimpleNamespace(returncode=0, stdout=mismatched, stderr="")

    assert owner_autostart.task_status("Owner Chrome Relay", config, action_path, "DOMAIN\\user", runner=runner) == {
        "status": "MISMATCH", "task_name": "Owner Chrome Relay",
    }


def test_task_status_mismatch_exits_two_from_main(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    config_path = Path(config["private_dir"]) / "owner-start.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    action_path = Path(config["private_dir"]) / "owner-start-action.xml"
    task_xml, _ = owner_autostart.build_xml(config, str(SOURCE), "Owner Chrome Relay", "DOMAIN\\user", str(config_path))
    mismatched = task_xml.replace("</Settings>", "<WakeToRun>true</WakeToRun></Settings>")

    def runner(argv, **kwargs):
        if argv[0] == "whoami.exe":
            return SimpleNamespace(returncode=0, stdout='"domain\\user","S-1-5-21-1-2-3-1001"\n', stderr="")
        return SimpleNamespace(returncode=0, stdout=mismatched, stderr="")

    monkeypatch.setattr(owner_autostart.subprocess, "run", runner)
    exit_code = owner_autostart.main([
        "status", "--config", str(config_path), "--runtime-dir", str(SOURCE),
        "--task-name", "Owner Chrome Relay", "--user", "DOMAIN\\user",
    ])
    assert exit_code == 2
    assert json.loads(capsys.readouterr().out)["status"] == "MISMATCH"
