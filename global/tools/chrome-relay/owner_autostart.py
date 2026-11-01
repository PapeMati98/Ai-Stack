"""Plan, verify and enable the owner Chrome Relay logon task."""

import argparse
import hashlib
import json
import os
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

from owner_start import ConfigError, validate_config


TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"
ET.register_namespace("", TASK_NS)


class TaskError(RuntimeError):
    def __init__(self, detail, exit_code=1):
        super().__init__(detail)
        self.detail = detail
        self.exit_code = exit_code


def _tag(name):
    return "{" + TASK_NS + "}" + name


def _text(parent, name, value):
    child = ET.SubElement(parent, _tag(name))
    child.text = str(value)
    return child


def _xml(root):
    return ET.tostring(root, encoding="unicode")


def _validate_arguments(config, runtime_dir, task_name, user):
    config = validate_config(config)
    if not isinstance(runtime_dir, str) or not os.path.isabs(runtime_dir):
        raise ConfigError("runtime_dir")
    if not isinstance(task_name, str) or not task_name.strip() or any(ord(c) < 32 for c in task_name):
        raise ConfigError("task_name")
    if not isinstance(user, str) or not user.strip() or any(ord(c) < 32 for c in user):
        raise ConfigError("user")
    return config, runtime_dir, task_name, user


def build_xml(config, runtime_dir, task_name, user, config_path):
    """Return (task XML, HiddenLaunch XML) without writing or invoking schtasks."""
    config, runtime_dir, task_name, user = _validate_arguments(config, runtime_dir, task_name, user)
    if not isinstance(config_path, str) or not os.path.isabs(config_path):
        raise ConfigError("config")
    private_dir = Path(config["private_dir"])
    action_path = private_dir / "owner-start-action.xml"
    task_path = private_dir / "owner-start-task.xml"
    runtime = Path(runtime_dir)
    owner_script = runtime / "owner_start.py"

    launcher = ET.Element("hidden-launch", {"version": "1", "log-directory": str(private_dir / "autostart-logs")})
    ET.SubElement(launcher, "action", {
        "executable": config["python"],
        "arguments": '-B "' + str(owner_script) + '" --start --autostart --config "' + str(Path(config_path)) + '"',
        "directory": str(runtime),
    })

    task = ET.Element(_tag("Task"), {"version": "1.2"})
    registration = ET.SubElement(task, _tag("RegistrationInfo"))
    _text(registration, "Description", "Owner Chrome Relay startup")
    triggers = ET.SubElement(task, _tag("Triggers"))
    trigger = ET.SubElement(triggers, _tag("LogonTrigger"))
    _text(trigger, "Enabled", "true")
    _text(trigger, "UserId", user)
    _text(trigger, "Delay", "PT30S")
    principals = ET.SubElement(task, _tag("Principals"))
    principal = ET.SubElement(principals, _tag("Principal"), {"id": "Author"})
    _text(principal, "UserId", user)
    _text(principal, "LogonType", "InteractiveToken")
    _text(principal, "RunLevel", "LeastPrivilege")
    settings = ET.SubElement(task, _tag("Settings"))
    _text(settings, "MultipleInstancesPolicy", "IgnoreNew")
    _text(settings, "DisallowStartIfOnBatteries", "false")
    _text(settings, "StopIfGoingOnBatteries", "false")
    _text(settings, "ExecutionTimeLimit", "PT15M")
    _text(settings, "StartWhenAvailable", "false")
    _text(settings, "Enabled", "false")
    actions = ET.SubElement(task, _tag("Actions"), {"Context": "Author"})
    action = ET.SubElement(actions, _tag("Exec"))
    _text(action, "Command", config["hidden_launch"])
    _text(action, "Arguments", '"' + str(action_path) + '"')

    # Keep the config path private to the action XML; it is never emitted in status/log output.
    return _xml(task), _xml(launcher)


def _local(element):
    return element.tag.rsplit("}", 1)[-1]


def _one(parent, name):
    found = [node for node in list(parent) if node.tag == _tag(name)]
    if len(found) != 1:
        raise TaskError("task_xml_mismatch")
    return found[0]


def _value(parent, name):
    return (_one(parent, name).text or "").strip()


def _optional(parent, name, default):
    found = [node for node in list(parent) if node.tag == _tag(name)]
    if len(found) > 1:
        raise TaskError("task_xml_mismatch")
    return (found[0].text or "").strip() if found else default


def _only_children(parent, allowed):
    if any(node.tag not in {_tag(name) for name in allowed} for node in list(parent)):
        raise TaskError("task_xml_mismatch")


def verify_task_xml(xml_text, config, action_path, user, sid=None, enabled=False):
    """Fail closed unless the registered task matches the requested task.

    Task Scheduler exports omit default values (trigger Enabled, RunLevel LeastPrivilege,
    StartWhenAvailable false, Settings Enabled true) and store the principal as a SID.
    """
    try:
        root = ET.fromstring(xml_text)
        if root.tag != _tag("Task"):
            raise TaskError("task_xml_mismatch")
        triggers = _one(root, "Triggers")
        trigger = _one(triggers, "LogonTrigger")
        if len(list(triggers)) != 1:
            raise TaskError("task_xml_mismatch")
        _only_children(trigger, ("Enabled", "UserId", "Delay"))
        if (_optional(trigger, "Enabled", "true").casefold() != "true"
                or _value(trigger, "UserId").casefold() not in {user.casefold(), (sid or user).casefold()}
                or _value(trigger, "Delay") != "PT30S"):
            raise TaskError("task_xml_mismatch")
        principals = _one(root, "Principals")
        principal = _one(principals, "Principal")
        if len(list(principals)) != 1:
            raise TaskError("task_xml_mismatch")
        _only_children(principal, ("UserId", "LogonType", "RunLevel"))
        principal_user = _value(principal, "UserId").casefold()
        if (principal_user not in {user.casefold(), (sid or user).casefold()}
                or _value(principal, "LogonType") != "InteractiveToken"
                or _optional(principal, "RunLevel", "LeastPrivilege") != "LeastPrivilege"):
            raise TaskError("task_xml_mismatch")
        settings = _one(root, "Settings")
        expected_settings = {
            "MultipleInstancesPolicy": ("IgnoreNew", "IgnoreNew"),
            "DisallowStartIfOnBatteries": ("false", None),
            "StopIfGoingOnBatteries": ("false", None),
            "ExecutionTimeLimit": ("PT15M", None),
            "StartWhenAvailable": ("false", "false"),
            "Enabled": ("true" if enabled else "false", "true"),
        }
        for name, (expected, default) in expected_settings.items():
            actual = _optional(settings, name, default)
            if actual is None or actual.casefold() != expected.casefold():
                raise TaskError("task_xml_mismatch")
        for name in ("Hidden", "WakeToRun", "RunOnlyIfIdle", "RunOnlyIfNetworkAvailable"):
            if _optional(settings, name, "false").casefold() != "false":
                raise TaskError("task_xml_mismatch")
        if any(node.tag == _tag("DeleteExpiredTaskAfter") for node in list(settings)):
            raise TaskError("task_xml_mismatch")
        actions = _one(root, "Actions")
        action_nodes = [node for node in list(actions) if _local(node) == "Exec"]
        if len(action_nodes) != 1 or len(list(actions)) != 1:
            raise TaskError("task_xml_mismatch")
        action = action_nodes[0]
        _only_children(action, ("Command", "Arguments"))
        if (_value(action, "Command") != config["hidden_launch"]
                or _value(action, "Arguments") != '"' + str(action_path) + '"'):
            raise TaskError("task_xml_mismatch")
    except (ET.ParseError, AttributeError, TypeError, ValueError) as error:
        if isinstance(error, TaskError):
            raise
        raise TaskError("task_xml_mismatch") from None
    return True


def _current_sid(runner=None):
    try:
        result = _run_schtasks(["whoami.exe", "/user", "/fo", "csv", "/nh"], runner=runner)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if _returncode(result) != 0:
        return None
    fields = [part.strip().strip('"') for part in (getattr(result, "stdout", "") or "").strip().split(",")]
    return fields[-1] if fields and fields[-1].startswith("S-1-") else None


def _run_schtasks(argv, *, runner=None):
    if runner is None:
        runner = subprocess.run
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return runner(argv, capture_output=True, text=True, timeout=30, creationflags=flags, shell=False)


def _query_argv(task_name):
    return ["schtasks.exe", "/Query", "/TN", task_name, "/XML"]


def _returncode(result):
    return getattr(result, "returncode", 1)


def _check_launcher_pin(config):
    try:
        launcher_hash = hashlib.sha256(Path(config["hidden_launch"]).read_bytes()).hexdigest()
    except OSError:
        raise TaskError("launcher_pin", 2) from None
    if launcher_hash.casefold() != config["hidden_launch_sha256"].casefold():
        raise TaskError("launcher_pin", 2)


def apply(config, runtime_dir, task_name, user, config_path, *, runner=None):
    config, runtime_dir, task_name, user = _validate_arguments(config, runtime_dir, task_name, user)
    if not os.path.isabs(config_path):
        raise TaskError("config")
    _check_launcher_pin(config)
    task_xml, launch_xml = build_xml(config, runtime_dir, task_name, user, config_path)
    private_dir = Path(config["private_dir"])
    action_path = private_dir / "owner-start-action.xml"
    task_path = private_dir / "owner-start-task.xml"
    try:
        existing = _run_schtasks(_query_argv(task_name), runner=runner)
    except (OSError, subprocess.TimeoutExpired):
        raise TaskError("task_query") from None
    if _returncode(existing) == 0:
        raise TaskError("task_exists", 2)
    runtime = Path(runtime_dir)
    if any(not (runtime / name).is_file()
           for name in ("owner_start.py", "owner_probe.py", "owner_chrome_agent.cjs")):
        raise TaskError("runtime")
    private_dir.mkdir(parents=True, exist_ok=True)
    action_path.write_text(launch_xml, encoding="utf-8")
    task_path.write_bytes(task_xml.encode("utf-16"))
    create = ["schtasks.exe", "/Create", "/XML", str(task_path), "/TN", task_name]
    _check_launcher_pin(config)
    try:
        created = _run_schtasks(create, runner=runner)
    except (OSError, subprocess.TimeoutExpired):
        raise TaskError("task_create") from None
    if _returncode(created) != 0:
        raise TaskError("task_create", 2)
    try:
        queried = _run_schtasks(_query_argv(task_name), runner=runner)
    except (OSError, subprocess.TimeoutExpired):
        raise TaskError("task_query") from None
    if _returncode(queried) != 0:
        raise TaskError("task_query")
    sid = _current_sid(runner)
    verify_task_xml(getattr(queried, "stdout", ""), config, action_path, user, sid=sid)
    try:
        enabled = _run_schtasks(["schtasks.exe", "/Change", "/TN", task_name, "/ENABLE"], runner=runner)
    except (OSError, subprocess.TimeoutExpired):
        raise TaskError("task_enable") from None
    if _returncode(enabled) != 0:
        raise TaskError("task_enable")
    return {"status": "ENABLED", "task_name": task_name}


def task_status(task_name, config, action_path, user, *, runner=None):
    try:
        result = _run_schtasks(_query_argv(task_name), runner=runner)
    except (OSError, subprocess.TimeoutExpired):
        raise TaskError("task_query") from None
    if _returncode(result) != 0:
        return {"status": "ABSENT", "task_name": task_name}
    try:
        root = ET.fromstring(getattr(result, "stdout", ""))
        enabled = _optional(_one(root, "Settings"), "Enabled", "true").casefold() == "true"
        verify_task_xml(getattr(result, "stdout", ""), config, action_path, user,
                        sid=_current_sid(runner), enabled=enabled)
    except (ET.ParseError, TaskError, AttributeError, TypeError, ValueError):
        return {"status": "MISMATCH", "task_name": task_name}
    return {"status": "ENABLED" if enabled else "DISABLED", "task_name": task_name}


def _load_inputs(args):
    if not os.path.isabs(args.config):
        raise ConfigError("config")
    config = validate_config(json.loads(Path(args.config).read_text(encoding="utf-8")))
    config, runtime_dir, task_name, user = _validate_arguments(
        config, args.runtime_dir, args.task_name, args.user)
    return config, runtime_dir, task_name, user, args.config


def main(argv=None):
    parser = argparse.ArgumentParser(description="Plan or manage the owner Chrome Relay logon task")
    parser.add_argument("command", choices=("plan", "apply", "status"))
    parser.add_argument("--config", required=True)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--task-name", required=True)
    parser.add_argument("--user", required=True)
    args = parser.parse_args(argv)
    try:
        config, runtime_dir, task_name, user, config_path = _load_inputs(args)
        if args.command == "plan":
            task_xml, launch_xml = build_xml(config, runtime_dir, task_name, user, config_path)
            print(json.dumps({"task_xml": task_xml, "hidden_launch_xml": launch_xml}, ensure_ascii=False))
            return 0
        if args.command == "apply":
            print(json.dumps(apply(config, runtime_dir, task_name, user, config_path), ensure_ascii=False))
            return 0
        result = task_status(task_name, config, Path(config["private_dir"]) / "owner-start-action.xml",
                             user, runner=None)
        print(json.dumps(result, ensure_ascii=False))
        return 2 if result["status"] == "MISMATCH" else 0
    except TaskError as error:
        print(json.dumps({"status": "FAILED", "detail": error.detail}, separators=(",", ":")))
        return error.exit_code
    except (OSError, ValueError, ConfigError):
        print(json.dumps({"status": "FAILED", "detail": "config"}, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
