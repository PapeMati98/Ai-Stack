#!/usr/bin/env python3
"""Bounded SSH client for the remote-computer-use Windows VM runner."""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


RUNNER_VERSION = "0.3.0"
DEFAULT_HOST = "ai-lab-uat-vm"
ALLOWED_HOSTS = ("ai-lab-uat-vm", "ai-lab-windows-vm")
REMOTE_ROOT = "C:/ProgramData/MateuszAI/remote-computer-use"
REMOTE_SCRIPT = f"{REMOTE_ROOT}/current/scripts/invoke-run.ps1"
REMOTE_INBOX = f"{REMOTE_ROOT}/inbox"
REMOTE_ARTIFACTS = f"{REMOTE_ROOT}/artifacts"
ORIGIN_UAT_ORIGIN = "http://127.0.0.1:13102"
ORIGIN_UAT_REMOTE_FORWARD = "127.0.0.1:13102:127.0.0.1:3102"
ORIGIN_UAT_LOCAL_PORT = 3102
WINDOWS_SSH_READINESS_EXIT_CODES = {0, 1, 3}
TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
FIXTURE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.html$")
SCREENSHOT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
GIT_COMMIT_RE = re.compile(r"^[a-f0-9]{7,40}$")
RUN_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{9}Z-[a-f0-9]{8}$")
BROWSER_READ_TOP_LEVEL_KEYS = {
    "schema_version",
    "task_id",
    "mode",
    "headless",
    "side_effects",
    "timeout_seconds",
    "target",
    "actions",
}
BROWSER_UAT_TOP_LEVEL_KEYS = BROWSER_READ_TOP_LEVEL_KEYS | {"capability"}
BROWSER_READ_ACTION_KEYS = {
    "assert_title_contains": {"type", "contains"},
    "assert_text": {"type", "selector", "contains"},
    "assert_visible": {"type", "selector"},
    "click": {"type", "selector"},
    "fill": {"type", "selector", "value"},
    "press": {"type", "selector", "key"},
    "screenshot": {"type", "name"},
}
BROWSER_UAT_ACTION_KEYS = {
    "assert_title_contains": {"type", "contains"},
    "assert_text": {"type", "selector", "contains"},
    "assert_visible": {"type", "selector"},
    "click": {"type", "selector"},
    "select_option": {"type", "selector", "value"},
    "set_fixture_file": {"type", "selector", "fixture_id", "fixture_sha256"},
}
DESKTOP_TOP_LEVEL_KEYS = {
    "schema_version",
    "task_id",
    "mode",
    "headless",
    "side_effects",
    "capability",
    "timeout_seconds",
    "target",
    "actions",
}
DESKTOP_ACTION_KEYS = {
    "screenshot": {"type", "name"},
    "wait": {"type", "milliseconds"},
    "launch": {"type", "app_id"},
    "move": {"type", "x", "y"},
    "click": {"type", "x", "y", "button", "count"},
    "text": {"type", "value"},
    "key": {"type", "key", "modifiers"},
    "close_target": {"type"},
}
SAFE_BROWSER_KEYS = {"Tab", "Escape", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"}
SAFE_DESKTOP_KEYS = {
    "TAB", "ENTER", "ESCAPE", "BACKSPACE", "DELETE", "SPACE", "HOME", "END",
    "PAGEUP", "PAGEDOWN", "ARROWUP", "ARROWDOWN", "ARROWLEFT", "ARROWRIGHT", "F5",
}
SAFE_DESKTOP_MODIFIERS = {"CTRL", "SHIFT", "ALT"}
APP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")
FILE_FIXTURE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
ARTIFACT_NAMES = {
    "result.json",
    "console.json",
    "network.json",
    "trace.zip",
    "final.png",
    "failure.png",
    "runner.stdout.log",
    "runner.stderr.log",
    "before.png",
    "desktop.json",
    "desktop-runtime.json",
    "action-ledger.json",
    "broker-result-after-timeout.json",
}


class ClientError(RuntimeError):
    def __init__(self, message: str, exit_code: int = 2) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _print_json(payload: object, *, stream=None) -> None:
    """Emit machine-readable JSON even when Windows stdout uses a legacy code page."""
    print(json.dumps(payload, ensure_ascii=True), file=stream or sys.stdout)


def _expected_git_commit(value: str) -> str:
    if not GIT_COMMIT_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(
            "expected upstream commit must be 7..40 lowercase hex characters"
        )
    return value


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


ORIGIN_UAT_HTTP_OPENER = build_opener(ProxyHandler({}), _NoRedirectHandler())


def _require_object(value: object, label: str) -> dict:
    if not isinstance(value, dict):
        raise ClientError(f"{label} must be an object")
    return value


def _require_exact_keys(value: dict, expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ClientError(f"{label} fields mismatch; missing={missing}; extra={extra}")


def _require_string(value: object, label: str, minimum: int, maximum: int) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        raise ClientError(f"{label} must have length {minimum}..{maximum}")
    return value


def _validate_common_contract(value: dict) -> None:
    task_id = value["task_id"]
    if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
        raise ClientError("task_id is invalid")
    timeout = value["timeout_seconds"]
    if type(timeout) is not int or not 10 <= timeout <= 300:
        raise ClientError("timeout_seconds must be 10..300")


def _validate_browser_contract(value: dict) -> None:
    read_only = value.get("schema_version") == 1
    origin_uat = value.get("schema_version") == 3
    if not read_only and not origin_uat:
        raise ClientError("browser contracts require schema version 1 or 3")
    _require_exact_keys(
        value,
        BROWSER_UAT_TOP_LEVEL_KEYS if origin_uat else BROWSER_READ_TOP_LEVEL_KEYS,
        "contract",
    )
    if value["mode"] != "browser" or value["headless"] is not True:
        raise ClientError("browser contracts require mode=browser and headless=true")
    if read_only and value["side_effects"] != "none":
        raise ClientError("browser schema version 1 requires side_effects=none")
    if origin_uat and (
        value["side_effects"] != "test-only"
        or value["capability"] != "origin-uat"
    ):
        raise ClientError(
            "browser schema version 3 requires capability=origin-uat and side_effects=test-only"
        )
    target = _require_object(value["target"], "target")
    _require_exact_keys(target, {"kind", "value"}, "target")
    target_value = _require_string(target["value"], "target.value", 1, 2048)
    if origin_uat and target["kind"] != "url":
        raise ClientError("browser origin UAT requires a URL target")
    if target["kind"] == "fixture":
        if not FIXTURE_RE.fullmatch(target_value):
            raise ClientError("fixture name is invalid")
    elif target["kind"] == "url":
        parsed = urlsplit(target_value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ClientError("target URL must use HTTP or HTTPS")
        if parsed.username or parsed.password:
            raise ClientError("target URL must not contain credentials")
    else:
        raise ClientError("target.kind must be fixture or url")
    _validate_actions(
        value["actions"],
        BROWSER_UAT_ACTION_KEYS if origin_uat else BROWSER_READ_ACTION_KEYS,
        "browser-origin-uat" if origin_uat else "browser",
    )
    if read_only and target["kind"] == "url" and any(
        action["type"] in {"click", "fill", "press"} for action in value["actions"]
    ):
        raise ClientError("network browser contracts with side_effects=none cannot perform input actions")


def _validate_desktop_contract(value: dict) -> None:
    _require_exact_keys(value, DESKTOP_TOP_LEVEL_KEYS, "contract")
    if value["schema_version"] != 2 or value["mode"] != "desktop":
        raise ClientError("desktop contracts require schema version 2")
    if value["headless"] is not False or value["capability"] not in {"observe", "uat"}:
        raise ClientError("desktop contracts require headless=false and a supported capability")
    if (
        value["capability"] == "observe" and value["side_effects"] != "none"
    ) or (
        value["capability"] == "uat" and value["side_effects"] != "test-only"
    ):
        raise ClientError("desktop side_effects does not match capability")
    target = _require_object(value["target"], "target")
    _require_exact_keys(target, {"kind", "app_id"}, "target")
    app_id = target["app_id"]
    if value["capability"] == "observe":
        if target != {"kind": "desktop", "app_id": "desktop"}:
            raise ClientError("observe contracts must target the desktop")
    elif target["kind"] != "app" or not isinstance(app_id, str) or not APP_ID_RE.fullmatch(app_id):
        raise ClientError("uat contracts must target an app id")
    _validate_actions(value["actions"], DESKTOP_ACTION_KEYS, "desktop")
    if value["capability"] == "observe":
        if any(action["type"] not in {"screenshot", "wait"} for action in value["actions"]):
            raise ClientError("observe contracts may only wait or take screenshots")
    else:
        launches = [action for action in value["actions"] if action["type"] == "launch"]
        if len(launches) != 1 or launches[0]["app_id"] != app_id:
            raise ClientError("uat contracts require exactly one launch action for the target app")


def _validate_actions(actions: object, keys_by_type: dict[str, set[str]], mode: str) -> None:
    if not isinstance(actions, list) or not 1 <= len(actions) <= 50:
        raise ClientError("actions must contain 1..50 items")
    for index, raw_action in enumerate(actions):
        action = _require_object(raw_action, f"actions[{index}]")
        action_type = action.get("type")
        expected = keys_by_type.get(action_type)
        if expected is None:
            raise ClientError(f"actions[{index}].type is unsupported for {mode}")
        _require_exact_keys(action, expected, f"actions[{index}]")
        if mode.startswith("browser"):
            if "selector" in expected:
                _require_string(action["selector"], f"actions[{index}].selector", 1, 512)
            if "contains" in expected:
                _require_string(action["contains"], f"actions[{index}].contains", 1, 1000)
            if action_type == "fill":
                _require_string(action["value"], f"actions[{index}].value", 0, 1000)
            if action_type == "select_option":
                _require_string(action["value"], f"actions[{index}].value", 1, 128)
            if action_type == "press" and action["key"] not in SAFE_BROWSER_KEYS:
                raise ClientError(f"actions[{index}].key is not allowed")
            if action_type == "set_fixture_file":
                fixture_id = action["fixture_id"]
                fixture_sha256 = action["fixture_sha256"]
                if (
                    not isinstance(fixture_id, str)
                    or not FILE_FIXTURE_ID_RE.fullmatch(fixture_id)
                ):
                    raise ClientError(f"actions[{index}].fixture_id is invalid")
                if (
                    not isinstance(fixture_sha256, str)
                    or not SHA256_RE.fullmatch(fixture_sha256)
                ):
                    raise ClientError(f"actions[{index}].fixture_sha256 is invalid")
        else:
            if action_type in {"move", "click"}:
                for field in ("x", "y"):
                    coordinate = action[field]
                    if type(coordinate) is not int or not -16384 <= coordinate <= 16384:
                        raise ClientError(f"actions[{index}].{field} is invalid")
            if action_type == "click" and (
                action["button"] not in {"left", "right"} or action["count"] not in {1, 2}
            ):
                raise ClientError(f"actions[{index}] click shape is invalid")
            if action_type == "wait" and (
                type(action["milliseconds"]) is not int or not 0 <= action["milliseconds"] <= 5000
            ):
                raise ClientError(f"actions[{index}].milliseconds must be 0..5000")
            if action_type == "launch" and (
                not isinstance(action["app_id"], str) or not APP_ID_RE.fullmatch(action["app_id"])
            ):
                raise ClientError(f"actions[{index}].app_id is invalid")
            if action_type == "text":
                _require_string(action["value"], f"actions[{index}].value", 0, 2000)
            if action_type == "key":
                modifiers = action["modifiers"]
                if action["key"] not in SAFE_DESKTOP_KEYS:
                    raise ClientError(f"actions[{index}].key is not allowed")
                if (
                    not isinstance(modifiers, list)
                    or len(modifiers) > 3
                    or len(set(modifiers)) != len(modifiers)
                    or any(modifier not in SAFE_DESKTOP_MODIFIERS for modifier in modifiers)
                ):
                    raise ClientError(f"actions[{index}].modifiers is invalid")
        if action_type == "screenshot":
            name = action["name"]
            if not isinstance(name, str) or not SCREENSHOT_RE.fullmatch(name):
                raise ClientError(f"actions[{index}].name is invalid")


def validate_contract(contract: object) -> dict:
    value = _require_object(contract, "contract")
    if value.get("mode") == "browser":
        _validate_browser_contract(value)
    elif value.get("mode") == "desktop":
        _validate_desktop_contract(value)
    else:
        raise ClientError("mode must be browser or desktop")
    _validate_common_contract(value)
    return value


def load_contract(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ClientError(f"contract is not valid UTF-8 JSON: {exc}") from exc
    return validate_contract(raw)


def _tool(name: str) -> str:
    executable = shutil.which(name)
    if not executable:
        raise ClientError(f"required executable is unavailable: {name}")
    return executable


def _run(argv: list[str], timeout: int, allowed_codes: set[int]) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ClientError(f"command timed out after {timeout}s", 5) from exc
    if completed.returncode not in allowed_codes:
        raise ClientError(f"command failed with exit code {completed.returncode}", 6)
    return completed


def _parse_last_json(output: str) -> dict:
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ClientError("remote command returned no JSON result", 6)


def _ssh(host: str, remote_args: list[str], timeout: int, allowed_codes: set[int]) -> subprocess.CompletedProcess[str]:
    return _run(
        [
            _tool("ssh.exe"),
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=15",
            host,
            *remote_args,
        ],
        timeout,
        allowed_codes,
    )


def _remote_powershell(host: str, arguments: list[str], timeout: int, allowed_codes: set[int]) -> dict:
    completed = _ssh(
        host,
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            REMOTE_SCRIPT,
            *arguments,
        ],
        timeout,
        allowed_codes,
    )
    return _parse_last_json(completed.stdout)


def validate_remote_artifact_dir(value: object, task_id: str, run_id: str) -> str:
    if not isinstance(value, str) or not RUN_ID_RE.fullmatch(run_id):
        raise ClientError("remote artifact identity is invalid", 6)
    normalized = value.replace("\\", "/").rstrip("/")
    expected = f"{REMOTE_ARTIFACTS}/{task_id}/{run_id}"
    if normalized.casefold() != expected.casefold():
        raise ClientError("remote artifact path escaped the expected task root", 6)
    return normalized


def _remove_remote_contract(host: str, remote_path: str) -> None:
    command = f"Remove-Item -LiteralPath '{remote_path}' -Force -ErrorAction SilentlyContinue"
    try:
        _ssh(
            host,
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
            30,
            {0},
        )
    except ClientError:
        pass


def _download_artifacts(host: str, remote_dir: str, local_dir: Path, contract: dict, status: str) -> dict:
    if local_dir.exists():
        raise ClientError(f"local artifact directory already exists: {local_dir}", 6)
    local_dir.mkdir(parents=True)
    origin_uat = contract.get("schema_version") == 3 and contract.get("capability") == "origin-uat"
    screenshot_names = set() if origin_uat else {
        f"{action['name']}.png"
        for action in contract["actions"]
        if action["type"] == "screenshot"
    }
    if origin_uat:
        candidates = sorted({
            "result.json",
            "console.json",
            "network.json",
            "runner.stdout.log",
            "runner.stderr.log",
        })
    else:
        candidates = sorted(ARTIFACT_NAMES | screenshot_names)
    fetched: list[str] = []
    for name in candidates:
        remote = f"{host}:{remote_dir}/{name}"
        completed = _run([_tool("scp.exe"), "-q", "--", remote, str(local_dir / name)], 90, {0, 1})
        if completed.returncode == 0:
            fetched.append(name)

    required = {"result.json", "runner.stdout.log", "runner.stderr.log"}
    if origin_uat:
        required |= {"console.json", "network.json"}
    elif contract["mode"] == "browser":
        required |= {"console.json", "network.json"}
        if status == "PASS":
            required |= {"trace.zip", "final.png"} | screenshot_names
    else:
        required |= {"action-ledger.json"}
        if status == "PASS":
            required |= {"desktop.json", "desktop-runtime.json", "before.png", "final.png"} | screenshot_names
    missing = sorted(required - set(fetched))
    return {
        "status": "COMPLETE" if not missing else "INCOMPLETE",
        "fetched": fetched,
        "missing": missing,
    }


def command_doctor(args: argparse.Namespace) -> int:
    result = _remote_powershell(
        args.host,
        ["-Action", "doctor", "-RequireMode", args.mode],
        45,
        WINDOWS_SSH_READINESS_EXIT_CODES,
    )
    result["host_alias"] = args.host
    _print_json(result)
    return 0 if result.get("status") == "READY" else 3


def command_validate(args: argparse.Namespace) -> int:
    contract = load_contract(args.contract.resolve())
    _print_json(
        {
            "status": "VALID",
            "runner_version": RUNNER_VERSION,
            "task_id": contract["task_id"],
            "action_count": len(contract["actions"]),
        }
    )
    return 0


def command_run(args: argparse.Namespace) -> int:
    contract_path = args.contract.resolve()
    contract = load_contract(contract_path)
    if contract.get("schema_version") == 3 and not getattr(args, "reservation_id", None):
        raise ClientError(
            "browser origin UAT contracts must be executed through run-origin-uat"
        )
    if contract["mode"] == "desktop":
        readiness = _remote_powershell(
            args.host,
            ["-Action", "doctor", "-RequireMode", "desktop"],
            45,
            WINDOWS_SSH_READINESS_EXIT_CODES,
        )
        if readiness.get("status") != "READY" or readiness.get("desktop_ready") is not True:
            raise ClientError(
                f"desktop runner is not READY: {readiness.get('desktop_status', 'UNKNOWN')}",
                3,
            )
    elif contract.get("schema_version") == 3:
        readiness = _remote_powershell(
            args.host,
            ["-Action", "doctor", "-RequireMode", "browser"],
            45,
            WINDOWS_SSH_READINESS_EXIT_CODES,
        )
        if (
            readiness.get("status") != "READY"
            or readiness.get("browser_ready") is not True
            or readiness.get("allow_network") is not True
            or readiness.get("allowed_origin_count") != 1
        ):
            raise ClientError("browser origin UAT runner is not READY", 3)
    task_id = contract["task_id"]
    remote_contract = f"{REMOTE_INBOX}/{task_id}-{uuid.uuid4().hex[:12]}.json"
    try:
        _run(
            [_tool("scp.exe"), "-q", "--", str(contract_path), f"{args.host}:{remote_contract}"],
            60,
            {0},
        )
        remote_arguments = ["-Action", "run", "-ContractPath", remote_contract]
        reservation_id = getattr(args, "reservation_id", None)
        if reservation_id:
            remote_arguments.extend(["-ReservationId", reservation_id])
        result = _remote_powershell(
            args.host,
            remote_arguments,
            int(contract["timeout_seconds"]) + 90,
            {0, 1, 4, 5, 6},
        )
    finally:
        _remove_remote_contract(args.host, remote_contract)

    status = str(result.get("status", "ERROR"))
    if status == "BUSY":
        result["host_alias"] = args.host
        _print_json(result)
        return 4

    run_id = result.get("run_id")
    if not run_id or not result.get("artifact_dir"):
        result["host_alias"] = args.host
        _print_json(result)
        return 6
    remote_dir = validate_remote_artifact_dir(result.get("artifact_dir"), task_id, run_id)
    artifact_root = (
        Path(os.environ.get("USERPROFILE", str(Path.home())))
        / ".agents"
        / "remote-computer-use-artifacts"
    ).resolve()
    local_dir = artifact_root / task_id / run_id
    evidence = _download_artifacts(args.host, remote_dir, local_dir, contract, status)
    result.update(
        {
            "host_alias": args.host,
            "local_artifact_dir": str(local_dir),
            "evidence": evidence,
        }
    )
    (local_dir / "client-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _print_json(result)
    return 0 if result.get("status") == "PASS" and evidence["status"] == "COMPLETE" else 1


def _origin_uat_lock_path() -> Path:
    return (
        Path(os.environ.get("USERPROFILE", str(Path.home())))
        / ".agents"
        / "remote-computer-use-origin-uat.lock"
    )


def _acquire_origin_uat_lock(task_id: str, reservation_id: str) -> tuple[Path, int]:
    lock_path = _origin_uat_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    except FileExistsError as exc:
        raise ClientError(f"origin UAT host lock is already held: {lock_path}", 4) from exc
    payload = json.dumps(
        {
            "schema_version": 1,
            "task_id": task_id,
            "reservation_id": reservation_id,
            "host_process_id": os.getpid(),
            "tunnel_process_id": None,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    os.write(descriptor, payload)
    os.fsync(descriptor)
    return lock_path, descriptor


def _record_origin_uat_tunnel_pid(descriptor: int, tunnel_process_id: int) -> None:
    if not isinstance(tunnel_process_id, int) or tunnel_process_id <= 0:
        raise ClientError("origin UAT tunnel process identity is invalid", 6)
    os.lseek(descriptor, 0, os.SEEK_SET)
    raw = os.read(descriptor, 8192)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ClientError("origin UAT host lock metadata is invalid", 6) from exc
    payload["tunnel_process_id"] = tunnel_process_id
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    os.lseek(descriptor, 0, os.SEEK_SET)
    os.ftruncate(descriptor, 0)
    os.write(descriptor, encoded)
    os.fsync(descriptor)


def _release_origin_uat_lock(
    lock_path: Path,
    descriptor: int,
    *,
    remove: bool,
) -> None:
    try:
        os.close(descriptor)
    finally:
        if remove:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass


def _fetch_origin_uat_json(path: str) -> dict:
    if path not in {"/api/health", "/api/system/runtime"}:
        raise ClientError("origin UAT upstream probe path is not allowed", 6)
    request = Request(
        f"http://127.0.0.1:{ORIGIN_UAT_LOCAL_PORT}{path}",
        method="GET",
        headers={"Accept": "application/json"},
    )
    try:
        with ORIGIN_UAT_HTTP_OPENER.open(request, timeout=3) as response:
            if response.status != 200:
                raise ClientError("origin UAT upstream probe returned a non-200 response", 3)
            raw = response.read(65537)
    except (OSError, URLError) as exc:
        raise ClientError(
            f"origin UAT local upstream is unavailable on 127.0.0.1:{ORIGIN_UAT_LOCAL_PORT}",
            3,
        ) from exc
    if len(raw) > 65536:
        raise ClientError("origin UAT upstream probe response is too large", 3)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ClientError("origin UAT upstream probe did not return JSON", 3) from exc
    if not isinstance(payload, dict):
        raise ClientError("origin UAT upstream probe payload is invalid", 3)
    return payload


def _assert_origin_uat_local_upstream(expected_commit: str) -> None:
    if not isinstance(expected_commit, str) or not GIT_COMMIT_RE.fullmatch(expected_commit):
        raise ClientError("expected upstream commit must be 7..40 lowercase hex characters", 2)
    health = _fetch_origin_uat_json("/api/health")
    runtime = _fetch_origin_uat_json("/api/system/runtime")
    guard = runtime.get("guard")
    if health != {
        "status": "ok",
        "version": "1.0",
        "app": "Example Platform API",
    }:
        raise ClientError("origin UAT upstream is not the expected Example API", 3)
    if (
        runtime.get("environment") != "DEV"
        or str(runtime.get("api_port")) != "8010"
        or runtime.get("database_name") != "example_dev.db"
        or runtime.get("database_path_basename") != "example_dev.db"
        or runtime.get("git_commit") != expected_commit
        or runtime.get("frontend_expected_api_host") != "127.0.0.1:8010"
        or not isinstance(guard, dict)
        or guard.get("mode") != "pass"
        or guard.get("risks") != []
    ):
        raise ClientError("origin UAT upstream fingerprint does not match the approved Example DEV lane", 3)


def _start_origin_uat_tunnel(host: str) -> subprocess.Popen[str]:
    creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    process = subprocess.Popen(
        [
            _tool("ssh.exe"),
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=15",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ForwardAgent=no",
            "-N",
            "-T",
            "-R",
            ORIGIN_UAT_REMOTE_FORWARD,
            host,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creation_flags,
    )
    time.sleep(1)
    if process.poll() is not None:
        stderr = (process.stderr.read() if process.stderr else "").strip()
        raise ClientError(
            f"origin UAT SSH tunnel failed before readiness: {stderr[:500]}",
            6,
        )
    return process


def _stop_origin_uat_tunnel(process: subprocess.Popen[str] | None) -> bool:
    if process is None or process.poll() is not None:
        return True
    try:
        process.terminate()
        process.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return False
    return process.poll() is not None


def command_run_origin_uat(args: argparse.Namespace) -> int:
    if args.host != DEFAULT_HOST:
        raise ClientError("origin UAT is allowed only on ai-lab-uat-vm")
    contract_path = args.contract.resolve()
    contract = load_contract(contract_path)
    if (
        contract.get("schema_version") != 3
        or contract.get("capability") != "origin-uat"
        or contract.get("mode") != "browser"
    ):
        raise ClientError("run-origin-uat requires a browser schema version 3 contract")
    target = urlsplit(contract["target"]["value"])
    target_origin = f"{target.scheme}://{target.netloc}"
    if target_origin != ORIGIN_UAT_ORIGIN:
        raise ClientError(f"origin UAT target must use {ORIGIN_UAT_ORIGIN}")

    _assert_origin_uat_local_upstream(args.expected_upstream_commit)
    reservation_id = f"uat-{uuid.uuid4().hex}"
    lock_path, lock_descriptor = _acquire_origin_uat_lock(contract["task_id"], reservation_id)
    tunnel: subprocess.Popen[str] | None = None
    run_result: dict | None = None
    run_exit = 6
    cleanup_status = "COMPLETE"
    reservation_held = False
    remove_host_lock = False
    try:
        reservation = _remote_powershell(
            args.host,
            [
                "-Action",
                "reserve-origin-uat",
                "-TaskId",
                contract["task_id"],
                "-ReservationId",
                reservation_id,
            ],
            45,
            {0, 4},
        )
        if reservation.get("status") != "RESERVED":
            raise ClientError("origin UAT remote lease could not be reserved", 4)
        reservation_held = True
        tunnel = _start_origin_uat_tunnel(args.host)
        _record_origin_uat_tunnel_pid(lock_descriptor, tunnel.pid)
        listener = _remote_powershell(
            args.host,
            ["-Action", "origin-listener"],
            45,
            WINDOWS_SSH_READINESS_EXIT_CODES,
        )
        if (
            listener.get("status") != "READY"
            or listener.get("addresses") != ["127.0.0.1"]
            or listener.get("listen_count") != 1
        ):
            raise ClientError("origin UAT tunnel is not bound exactly once to VM loopback", 6)

        buffer = io.StringIO()
        run_args = argparse.Namespace(
            contract=contract_path,
            host=args.host,
            reservation_id=reservation_id,
        )
        with redirect_stdout(buffer):
            run_exit = command_run(run_args)
        run_result = _parse_last_json(buffer.getvalue())
        if run_result.get("lease_status") != "HELD":
            raise ClientError("origin UAT runner did not preserve the remote reservation", 6)
    finally:
        tunnel_stopped = _stop_origin_uat_tunnel(tunnel)
        listener_closed = False
        if tunnel_stopped:
            try:
                listener_after = _remote_powershell(
                    args.host,
                    ["-Action", "origin-listener"],
                    45,
                    WINDOWS_SSH_READINESS_EXIT_CODES,
                )
                listener_closed = listener_after.get("status") == "CLOSED"
            except ClientError:
                listener_closed = False
        if not tunnel_stopped or not listener_closed:
            cleanup_status = "INCOMPLETE"

        if reservation_held and listener_closed:
            try:
                release = _remote_powershell(
                    args.host,
                    [
                        "-Action",
                        "release-origin-uat",
                        "-TaskId",
                        contract["task_id"],
                        "-ReservationId",
                        reservation_id,
                    ],
                    45,
                    {0, 4},
                )
                remove_host_lock = release.get("status") in {"NOOP", "RELEASED"}
                if not remove_host_lock:
                    cleanup_status = "INCOMPLETE"
            except ClientError:
                cleanup_status = "INCOMPLETE"
        elif not reservation_held:
            remove_host_lock = True

        _release_origin_uat_lock(
            lock_path,
            lock_descriptor,
            remove=remove_host_lock,
        )

    if run_result is None:
        raise ClientError("origin UAT returned no run result", 6)
    run_result["origin_transport"] = {
        "origin": ORIGIN_UAT_ORIGIN,
        "remote_bind": "127.0.0.1:13102",
        "local_upstream": "127.0.0.1:3102",
        "cleanup": cleanup_status,
    }
    _print_json(run_result)
    if cleanup_status != "COMPLETE":
        return 1
    return run_exit


def command_recover(args: argparse.Namespace) -> int:
    result = _remote_powershell(
        args.host,
        ["-Action", "recover", "-MaxAgeMinutes", str(args.max_age_minutes)],
        45,
        {0, 4},
    )
    result["host_alias"] = args.host
    _print_json(result)
    return 0 if result.get("status") in {"NOOP", "RECOVERED"} else 4


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST, choices=ALLOWED_HOSTS)
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor")
    doctor.add_argument("--mode", choices=["any", "browser", "desktop"], default="any")
    doctor.set_defaults(func=command_doctor)

    validate = subparsers.add_parser("validate")
    validate.add_argument("--contract", type=Path, required=True)
    validate.set_defaults(func=command_validate)

    run = subparsers.add_parser("run")
    run.add_argument("--contract", type=Path, required=True)
    run.set_defaults(func=command_run)

    run_origin_uat = subparsers.add_parser("run-origin-uat")
    run_origin_uat.add_argument("--contract", type=Path, required=True)
    run_origin_uat.add_argument(
        "--expected-upstream-commit",
        required=True,
        type=_expected_git_commit,
    )
    run_origin_uat.set_defaults(func=command_run_origin_uat)

    recover = subparsers.add_parser("recover")
    recover.add_argument("--max-age-minutes", type=int, default=20, choices=range(1, 1441))
    recover.set_defaults(func=command_recover)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.func(args))
    except ClientError as exc:
        _print_json({"status": "ERROR", "error": str(exc)})
        return exc.exit_code


if __name__ == "__main__":
    sys.exit(main())
