"""Owner-approved Chrome Relay profile and broker start; never changes grants."""

import argparse
import hashlib
import importlib
import json
import os
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

try:
    import msvcrt
except ImportError:  # Allows the injected unit tests to run on non-Windows hosts.
    msvcrt = None
    import fcntl


SCHEMA = "chrome-relay-owner-start/1"
EXTENSION_FILES = ("manifest.json", "sw.js", "login_gate.js")
PATH_FIELDS = (
    "python", "node", "hidden_launch", "broker_runtime", "extension_dir", "private_dir",
    "managed_config", "controller_spec", "browser_exe", "profile_path", "playwright_root",
)
PREREQUISITES = (
    "python", "node", "hidden_launch", "broker_runtime", "extension_dir", "managed_config",
    "controller_spec", "browser_exe", "profile_path", "playwright_root",
)


class ConfigError(ValueError):
    pass


class RuntimeLoadError(RuntimeError):
    pass


def validate_config(value):
    required = set(PATH_FIELDS) | {
        "schema", "hidden_launch_sha256", "grant_id", "expected_origins", "profile_id",
        "saved_login", "start_url",
    }
    if not isinstance(value, dict) or set(value) != required or value.get("schema") != SCHEMA:
        raise ConfigError("config")
    result = dict(value)
    for key in PATH_FIELDS:
        path = result.get(key)
        if not isinstance(path, str) or not path or not os.path.isabs(path):
            raise ConfigError("config")
    profile_parts = [part for part in _path_text(result["profile_path"]).split("\\") if part]
    blocked_profiles = (
        ("google", "chrome", "user data"),
        ("microsoft", "edge", "user data"),
    )
    if any(tuple(profile_parts[index:index + 3]) == blocked
           for blocked in blocked_profiles
           for index in range(max(0, len(profile_parts) - 2))):
        raise ConfigError("config")
    pin = result["hidden_launch_sha256"]
    if not isinstance(pin, str) or len(pin) != 64 or any(ch not in "0123456789abcdefABCDEF" for ch in pin):
        raise ConfigError("config")
    for key in ("grant_id", "profile_id", "start_url"):
        if not isinstance(result.get(key), str) or not result[key]:
            raise ConfigError("config")
    origins = result.get("expected_origins")
    if (not isinstance(origins, list) or not origins or any(not isinstance(x, str) for x in origins)
            or len(set(origins)) != len(origins)
            or any(not _valid_origin(x) for x in origins)):
        raise ConfigError("config")
    saved = result.get("saved_login")
    if (not isinstance(saved, dict) or set(saved) != {"origin", "pages", "posts"}
            or not isinstance(saved["origin"], str)
            or not isinstance(saved["pages"], list) or not all(isinstance(x, str) for x in saved["pages"])
            or not isinstance(saved["posts"], list) or not all(isinstance(x, str) for x in saved["posts"])):
        raise ConfigError("config")
    if not result["start_url"].startswith("https://"):
        raise ConfigError("config")
    try:
        start = urlsplit(result["start_url"])
        saved_origin = urlsplit(saved["origin"])
    except ValueError:
        raise ConfigError("config") from None
    if (start.username or start.password or start.query or start.fragment
            or not _valid_origin(saved["origin"])
            or _origin_text(start) not in origins or _origin_text(saved_origin) not in origins):
        raise ConfigError("config")
    return result


def _valid_origin(value):
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username and not parsed.password
        and parsed.path in ("", "/") and not parsed.query and not parsed.fragment and "*" not in value
    )


def _origin_text(parsed):
    return parsed.scheme + "://" + parsed.netloc


def _path(value):
    return Path(value)


def _path_text(value):
    return os.path.normcase(os.path.normpath(str(value))).replace("/", "\\").casefold()


def _prerequisites(config):
    missing = []
    file_keys = {"python", "node", "hidden_launch", "managed_config", "controller_spec", "browser_exe"}
    directory_keys = {"broker_runtime", "extension_dir", "profile_path", "playwright_root"}
    for key in PREREQUISITES:
        path = _path(config[key])
        if (not path.exists() or (key in file_keys and not path.is_file())
                or (key in directory_keys and not path.is_dir())):
            missing.append(key)
    if not (_path(config["broker_runtime"]) / "managed.py").is_file():
        missing.append("broker_runtime")
    if not (_path(config["broker_runtime"]) / "uat.py").is_file():
        missing.append("broker_runtime")
    if not (_path(__file__).resolve().parent / "owner_chrome_agent.cjs").is_file():
        missing.append("owner_chrome_agent.cjs")
    return sorted(set(missing))


def _wait_prerequisites(config, clock, sleep):
    deadline = clock() + 180.0
    while True:
        missing = _prerequisites(config)
        if not missing:
            return True
        if clock() >= deadline:
            return False
        sleep(min(1.0, max(0.0, deadline - clock())))


def _read_grant(config):
    runtime = str(_path(config["broker_runtime"]))
    managed = _load_managed(runtime)
    raw = json.loads(_path(config["managed_config"]).read_text(encoding="utf-8"))
    current = managed.config_from_dict(raw)
    return managed, current


def _load_managed(runtime):
    if runtime in sys.path:
        sys.path.remove(runtime)
    sys.path.insert(0, runtime)
    try:
        managed = importlib.import_module("managed")
    except Exception as error:
        raise RuntimeLoadError("runtime") from error
    expected = _path_text(_path(runtime) / "managed.py")
    actual = _path_text(getattr(managed, "__file__", ""))
    if actual != expected:
        raise RuntimeLoadError("runtime")
    return managed


def _load_uat(runtime):
    if runtime in sys.path:
        sys.path.remove(runtime)
    sys.path.insert(0, runtime)
    try:
        uat = importlib.import_module("uat")
    except Exception as error:
        raise RuntimeLoadError("runtime") from error
    expected = _path_text(_path(runtime) / "uat.py")
    actual = _path_text(getattr(uat, "__file__", ""))
    if actual != expected:
        raise RuntimeLoadError("runtime")
    return uat


def _runtime_missing(config):
    runtime = _path(config["broker_runtime"])
    return not runtime.is_dir() or not (runtime / "managed.py").is_file() or not (runtime / "uat.py").is_file()


def _same_json_object(left, right):
    try:
        return json.dumps(left, sort_keys=True, separators=(",", ":"), allow_nan=False) == json.dumps(
            right, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        return False


def _controller_spec_matches(config):
    try:
        root = ET.parse(config["controller_spec"]).getroot()
    except (OSError, ET.ParseError, ValueError):
        return False
    if (root.tag != "hidden-launch" or root.attrib.get("version") != "1"
            or root.attrib.get("isolate-desktop") != "true"
            or not os.path.isabs(root.attrib.get("log-directory", ""))):
        return False
    actions = [element for element in root.iter() if element.tag == "action"]
    if len(actions) != 1:
        return False
    action = actions[0]
    if _path_text(action.attrib.get("executable", "")) != _path_text(config["python"]):
        return False
    arguments = action.attrib.get("arguments", "")
    tokens = []
    token = []
    quoted = False
    for char in arguments:
        if char == '"':
            quoted = not quoted
        elif char.isspace() and not quoted:
            if token:
                tokens.append("".join(token))
                token = []
        else:
            token.append(char)
    if quoted:
        return False
    if token:
        tokens.append("".join(token))
    expected_managed = _path_text(_path(config["broker_runtime"]) / "managed.py")
    expected_config = _path_text(config["managed_config"])
    normalized = [_path_text(value) if (":\\" in value or value.startswith(("/", "\\\\"))) else value.casefold()
                  for value in tokens]
    expected_tail = [expected_managed, "broker", "--config", expected_config]
    try:
        script_index = normalized.index(expected_managed)
    except ValueError:
        return False
    prefix = normalized[:script_index]
    return prefix in ([], ["-b"]) and normalized[script_index:] == expected_tail


def _gates(config, desktop):
    try:
        if desktop() != "Default":
            return "desktop", None, None
    except RuntimeLoadError:
        return "runtime", None, None
    except Exception:
        return "desktop", None, None
    try:
        launcher_hash = hashlib.sha256(_path(config["hidden_launch"]).read_bytes()).hexdigest()
    except OSError:
        return "launcher_pin", None, None
    if launcher_hash.casefold() != config["hidden_launch_sha256"].casefold():
        return "launcher_pin", None, None
    try:
        source_ext = _path(config["broker_runtime"]) / "extension"
        target_ext = _path(config["extension_dir"])
        if any((source_ext / name).read_bytes() != (target_ext / name).read_bytes() for name in EXTENSION_FILES):
            return "extension_drift", None, None
    except OSError:
        return "extension_drift", None, None
    try:
        managed, current = _read_grant(config)
    except RuntimeLoadError:
        return "runtime", None, None
    except Exception:
        return "grant_drift", None, None
    grant = current.grant
    if (grant.grant_id != config["grant_id"] or grant.profile_id != config["profile_id"]
            or sorted(grant.allowed_origins) != sorted(config["expected_origins"])):
        return "grant_drift", None, None
    try:
        extension_config = json.loads(
            (_path(config["private_dir"]) / "extension-config.json").read_text(encoding="utf-8")
        )
        if not _same_json_object(extension_config, managed.extension_config(current)):
            return "extension_config", None, None
    except (OSError, ValueError, TypeError, AttributeError):
        return "extension_config", None, None
    if not _controller_spec_matches(config):
        return "controller_spec", None, None
    return None, managed, current


def _windows_command_line_tokens(command):
    """Split a Windows command line using the CommandLineToArgvW quote rules."""
    if not isinstance(command, str):
        return None
    tokens = []
    index = 0
    length = len(command)
    while index < length:
        while index < length and command[index] in " \t":
            index += 1
        if index >= length:
            break
        token = []
        quoted = False
        while index < length:
            if command[index] in " \t" and not quoted:
                break
            if command[index] == "\\":
                start = index
                while index < length and command[index] == "\\":
                    index += 1
                slashes = index - start
                if index < length and command[index] == '"':
                    token.extend("\\" * (slashes // 2))
                    if slashes % 2:
                        token.append('"')
                    else:
                        quoted = not quoted
                    index += 1
                else:
                    token.extend("\\" * slashes)
                continue
            if command[index] == '"':
                if quoted and index + 1 < length and command[index + 1] == '"':
                    token.append('"')
                    index += 2
                else:
                    quoted = not quoted
                    index += 1
                continue
            token.append(command[index])
            index += 1
        tokens.append("".join(token))
        while index < length and command[index] in " \t":
            index += 1
    return tokens


def _detect(config, probes):
    broker = False
    expected_config = _path_text(config["managed_config"])
    for row in probes.processes():
        name = str(row.get("name", "")).casefold()
        if not (name.startswith("python") and name.endswith(".exe")):
            continue
        try:
            command = probes.command_line(row["pid"])
        except Exception:
            command = None
        if not isinstance(command, str) or not command.strip():
            # Elevated or foreign processes (e.g. HighestAvailable logon tasks) are unreadable for the
            # owner token; owner_start never starts an elevated broker. The listening-port check in
            # run() fails closed for a broker that cannot be identified here.
            continue
        tokens = _windows_command_line_tokens(command)
        if not tokens:
            continue
        for index, token in enumerate(tokens[:-3]):
            script = _path_text(token)
            if (script.endswith("\\managed.py") and tokens[index + 1] == "broker"
                    and tokens[index + 2] == "--config"
                    and _path_text(tokens[index + 3]) == expected_config):
                broker = True
                break
    usage = probes.profile_users(config["profile_path"])
    if usage.get("unknown_chrome_pids"):
        return None, None, "profile_unknown"
    return broker, bool(usage.get("matches")), None


def _port_listening(port, timeout=1.0):
    """Return True when something accepts TCP connections on 127.0.0.1:<port>."""
    import socket
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _acquire_lock(private_dir):
    lock_path = _path(private_dir) / "owner-start.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    stream = open(lock_path, "a+b")
    stream.seek(0, os.SEEK_END)
    if stream.tell() == 0:
        stream.write(b"\0")
        stream.flush()
    stream.seek(0)
    try:
        if msvcrt is not None:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        return None
    return stream


def _release_lock(stream):
    if stream is None:
        return
    try:
        stream.seek(0)
        if msvcrt is not None:
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        stream.close()


def _new_episode(private_dir):
    episodes = _path(private_dir) / "owner-episodes"
    episodes.mkdir(parents=True, exist_ok=True)
    episode = episodes / ("start-" + uuid.uuid4().hex[:12])
    episode.mkdir()
    return episode


def _setup(config, episode, expected_extension_id):
    value = {
        "browser_exe": config["browser_exe"],
        "profile_path": config["profile_path"],
        "playwright_root": config["playwright_root"],
        "extension_path": config["extension_dir"],
        "extension_config_path": str(_path(config["private_dir"]) / "extension-config.json"),
        "required_debugger_consent": True,
        "expected_origins": sorted(config["expected_origins"]),
        "profile_id": config["profile_id"],
        "expected_extension_id": expected_extension_id,
        "saved_login": config["saved_login"],
        "start_url": config["start_url"],
    }
    setup_path = episode / "setup.json"
    setup_path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    return setup_path


def _popen_with_fallback(popen, argv, **kwargs):
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
    base = dict(kwargs)
    base.update(shell=False, creationflags=no_window | breakaway)
    try:
        return popen(argv, **base), True
    except OSError:
        base["creationflags"] = no_window
        return popen(argv, **base), False


def _wait_json(path, timeout, clock, sleep, episode, process=None):
    deadline = clock() + timeout
    error_path = episode / "owner-browser-error.json"
    while True:
        if error_path.exists():
            return None, "browser_launch"
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8")), None
            except (OSError, ValueError):
                pass
        if process is not None:
            try:
                if process.poll() is not None:
                    return None, "browser_launch"
            except Exception:
                pass
        now = clock()
        if now >= deadline:
            return None, None
        sleep(min(0.1, deadline - now))


def _is_reject(error):
    return type(error).__name__ == "Reject"


def _probe_controller(controller_factory, current, clock, sleep, timeout=30.0):
    try:
        controller = controller_factory(current)
    except RuntimeLoadError:
        return "runtime"
    except Exception:
        return "register"
    deadline = clock() + timeout
    while True:
        try:
            sid, token = controller.register("codex", "cli")
        except Exception as error:
            if not _is_reject(error) or clock() >= deadline:
                return "register"
            sleep(min(0.2, max(0.0, deadline - clock())))
            continue
        try:
            return None if controller.release(sid, token) else "register"
        except Exception:
            return "register"


def _result(status, detail, exit_code, episode=None, started=None, pids=None, breakaway=None):
    return {
        "status": status, "detail": detail, "exit_code": exit_code,
        "episode": episode, "started": list(started or []), "pids": dict(pids or {}),
        "breakaway": dict(breakaway or {"window": False, "broker": False}),
    }


def _emit(result, mode, private_dir):
    row = {
        "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "mode": mode, "status": result["status"], "detail": result["detail"],
        "episode": result["episode"], "started": result["started"], "pids": result["pids"],
        "breakaway": result.get("breakaway", {"window": False, "broker": False}),
    }
    line = json.dumps(row, separators=(",", ":"), ensure_ascii=True)
    try:
        log_path = _path(private_dir) / "owner-start.log"
        with open(log_path, "a", encoding="utf-8", newline="\n") as log:
            log.write(line + "\n")
    except OSError:
        pass
    print(line)
    return result


def run(config, mode, *, probes, popen, controller_factory, clock, sleep, desktop, port_listening=None):
    """Run one locked start pass using injected process, clock and desktop dependencies."""
    try:
        config = validate_config(config)
    except ConfigError:
        result = _result("FAILED", "config", 1)
        print(json.dumps({"status": result["status"], "detail": result["detail"]}, separators=(",", ":")))
        return result
    if mode not in {"manual", "autostart"}:
        result = _result("FAILED", "config", 1)
        return _emit(result, "manual", config["private_dir"])
    try:
        lock = _acquire_lock(config["private_dir"])
    except OSError:
        return _emit(_result("FAILED", "lock", 1), mode, config["private_dir"])
    if lock is None:
        return _emit(_result("ALREADY_STARTING", "already_starting", 0), mode, config["private_dir"])
    try:
        if mode == "autostart":
            ready = _wait_prerequisites(config, clock, sleep)
        else:
            ready = not _prerequisites(config)
        if not ready:
            result = _result("FAILED", "runtime" if _runtime_missing(config) else "prerequisites", 1)
            return _emit(result, mode, config["private_dir"])
        detail, managed, current = _gates(config, desktop)
        if detail:
            if detail == "runtime":
                return _emit(_result("FAILED", detail, 1), mode, config["private_dir"])
            return _emit(_result("BLOCKED", detail, 2), mode, config["private_dir"])
        try:
            broker, window, state_error = _detect(config, probes)
        except Exception:
            return _emit(_result("FAILED", "process_probe", 1), mode, config["private_dir"])
        if state_error:
            return _emit(_result("BLOCKED", state_error, 2), mode, config["private_dir"])
        if not broker and (port_listening or _port_listening)(current.port):
            # Something unidentified owns the broker port. Never send the controller key to it and
            # never start a second broker beside it (SO_REUSEADDR does not prevent that on Windows).
            return _emit(_result("BLOCKED", "broker_unknown", 2), mode, config["private_dir"])
        if broker and window:
            return _emit(_result("ALREADY_RUNNING", "already_running", 0), mode, config["private_dir"])

        episode = None
        browser_process = None
        broker_process = None
        started = []
        pids = {}
        breakaway = {"window": False, "broker": False}
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        if not window:
            try:
                episode_path = _new_episode(config["private_dir"])
                episode = episode_path.name
                setup_path = _setup(config, episode_path, current.grant.extension_id)
                browser_log = open(_path(config["private_dir"]) / "browser-process.log", "ab")
                try:
                    browser_process, breakaway["window"] = _popen_with_fallback(
                        popen,
                        [config["node"], str(_path(__file__).resolve().parent / "owner_chrome_agent.cjs"), str(setup_path)],
                        cwd=str(_path(__file__).resolve().parent), env=env, stdin=subprocess.DEVNULL,
                        stdout=browser_log, stderr=browser_log,
                    )
                finally:
                    browser_log.close()
                started.append("window")
                pids["window"] = int(browser_process.pid)
            except (OSError, ValueError):
                result = _result("FAILED", "window_launch", 1, episode, started, pids, breakaway)
                return _emit(result, mode, config["private_dir"])
        if not broker:
            try:
                broker_process, breakaway["broker"] = _popen_with_fallback(
                    popen, [config["hidden_launch"], config["controller_spec"]],
                    cwd=config["broker_runtime"], env=env, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                started.append("broker")
                pids["broker"] = int(broker_process.pid)
            except (OSError, ValueError):
                result = _result("FAILED", "broker_launch", 1, episode, started, pids, breakaway)
                return _emit(result, mode, config["private_dir"])

        if not window:
            ready, error = _wait_json(episode_path / "owner-extension-ready.json", 60.0,
                                      clock, sleep, episode_path, browser_process)
            if error:
                return _emit(_result("FAILED", error, 1, episode, started, pids, breakaway), mode, config["private_dir"])
            if ready is None:
                return _emit(_result("FAILED", "extension_ready", 1, episode, started, pids, breakaway), mode, config["private_dir"])
            if ready.get("extension_id") != current.grant.extension_id:
                return _emit(_result("FAILED", "extension_id", 1, episode, started, pids, breakaway), mode, config["private_dir"])
        if not broker:
            error = _probe_controller(controller_factory, current, clock, sleep, 30.0)
            if error:
                return _emit(_result("FAILED", error, 1, episode, started, pids, breakaway), mode, config["private_dir"])
        if not window:
            ready, error = _wait_json(episode_path / "owner-browser-ready.json", 60.0,
                                      clock, sleep, episode_path, browser_process)
            if error:
                return _emit(_result("FAILED", error, 1, episode, started, pids, breakaway), mode, config["private_dir"])
            if ready is None:
                return _emit(_result("FAILED", "browser_ready", 1, episode, started, pids, breakaway), mode, config["private_dir"])
        return _emit(_result("STARTED", "started", 0, episode, started, pids, breakaway), mode, config["private_dir"])
    finally:
        _release_lock(lock)


def status(config, probes):
    """Read and report the known broker/window process state without starting anything."""
    config = validate_config(config)
    broker, window, error = _detect(config, probes)
    if error:
        return {"state": "unknown", "detail": error}
    if broker and window:
        state = "both"
    elif broker:
        state = "broker"
    elif window:
        state = "window"
    else:
        state = "none"
    return {"state": state}


def _load_controller(config):
    runtime = str(_path(config["broker_runtime"]))
    managed = None

    def controller_factory(current):
        nonlocal managed
        if managed is None:
            managed = _load_managed(runtime)
        return managed.Controller(current)

    return controller_factory


def _load_config_file(config_path, *, wait, clock, sleep):
    if not os.path.isabs(config_path):
        raise ConfigError("config")
    deadline = clock() + 180.0
    while True:
        try:
            content = _path(config_path).read_text(encoding="utf-8")
        except OSError:
            if not wait or clock() >= deadline:
                raise ConfigError("config") from None
            sleep(min(1.0, max(0.0, deadline - clock())))
            continue
        try:
            raw = json.loads(content)
        except (ValueError, UnicodeError):
            if not wait or clock() >= deadline:
                raise ConfigError("config") from None
            sleep(min(1.0, max(0.0, deadline - clock())))
            continue
        return validate_config(raw)


def main(argv=None, *, clock=None, sleep=None):
    parser = argparse.ArgumentParser(description="Start or inspect the owner Chrome Relay profile")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--start", action="store_true")
    mode.add_argument("--status", action="store_true")
    parser.add_argument("--config", required=True)
    parser.add_argument("--autostart", action="store_true")
    args = parser.parse_args(argv)
    clock = time.monotonic if clock is None else clock
    sleep = time.sleep if sleep is None else sleep
    try:
        config = _load_config_file(args.config, wait=args.autostart, clock=clock, sleep=sleep)
    except (OSError, ValueError, ConfigError):
        print(json.dumps({"status": "FAILED", "detail": "config"}, separators=(",", ":")))
        return 1
    if args.status:
        try:
            state = status(config, importlib.import_module("owner_probe"))
            print(json.dumps(state, separators=(",", ":")))
            return 1 if state.get("state") == "unknown" else 0
        except Exception:
            print(json.dumps({"state": "none", "detail": "process_probe"}, separators=(",", ":")))
            return 1
    result = run(config, "autostart" if args.autostart else "manual",
                 probes=importlib.import_module("owner_probe"), popen=subprocess.Popen,
                 controller_factory=_load_controller(config), clock=clock,
                 sleep=sleep, desktop=lambda: _load_uat(config["broker_runtime"]).current_desktop())
    return result["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
