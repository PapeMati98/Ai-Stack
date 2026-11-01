"""Read-only stdio MCP server for a fixed set of gcloud metadata queries.

The process speaks MCP JSON-RPC on stdout only. Cloud commands are built from
fixed literals plus policy values and validated identifiers. Login, token, and
arbitrary CLI tools are not exposed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping


PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")
SERVER_NAME = "gcp-readonly"
SERVER_VERSION = "0.1.0"
MAX_MESSAGE_BYTES = 64 * 1024
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_OUTPUT_BYTES = 128 * 1024
MAX_LIST_ITEMS = 100

PROFILE_KEYS = (
    "account",
    "project_id",
    "gcloud_configuration",
    "cloudsdk_config",
    "python_executable",
    "gcloud_python_entrypoint",
)

DROP_ENV_EXACT = {
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CREDENTIALS",
    "GOOGLE_OAUTH_ACCESS_TOKEN",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_QUOTA_PROJECT",
    "GCLOUD_PROJECT",
}
DROP_ENV_PREFIXES = (
    "CLOUDSDK_",
    "GOOGLE_OAUTH_",
    "GOOGLE_AUTH_",
)
PROXY_ENV = {
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "FTP_PROXY",
}
PYTHON_BASENAMES = {"python", "python.exe", "python3", "python3.exe"}
BLOCKED_BASENAMES = {
    "cmd.exe",
    "powershell.exe",
    "pwsh.exe",
    "bash.exe",
    "sh.exe",
    "wscript.exe",
    "cscript.exe",
    "gcloud.cmd",
    "gcloud.ps1",
    "gcloud",
}

ACCOUNT_RE = re.compile(r"^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$")
PROJECT_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
REGION_RE = re.compile(r"^[a-z][a-z0-9-]{0,38}[0-9]$")
SERVICE_RE = re.compile(r"^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$")
API_RE = re.compile(r"^[a-z][a-z0-9.-]{1,120}$")
REVISION_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
CONDITION_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,40}$")
DISPLAY_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class PolicyError(Exception):
    def __init__(self, message: str = "policy rejected") -> None:
        super().__init__(message)
        self.public_message = message


class SafeToolError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.public_message = message


@dataclass(frozen=True)
class Profile:
    name: str
    account: str
    project_id: str
    configuration: str
    cloudsdk_config: str
    python_executable: str
    gcloud_python_entrypoint: str


@dataclass(frozen=True)
class Policy:
    profiles: Mapping[str, Profile]


def repository_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "shared" / "mcp" / "gcp-readonly").is_dir():
            return parent
    return here.parent


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate key")
        obj[key] = value
    return obj


def _load_json_object(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw, object_pairs_hook=_pairs)
    except (json.JSONDecodeError, ValueError) as exc:
        raise PolicyError("policy is not valid JSON") from exc
    if not isinstance(value, dict):
        raise PolicyError("policy must be a JSON object")
    return value


def _absolute_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise PolicyError("policy path must be absolute")
    return path


def _outside_repository(path: Path) -> None:
    root = repository_root().resolve()
    resolved = path.resolve()
    if resolved == root or _is_relative_to(resolved, root):
        raise PolicyError("cloudsdk_config must be outside the repository")


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _require_string(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or value.strip() != value or value == "":
        raise PolicyError("policy field rejected")
    return value


def load_policy(raw: str) -> Policy:
    data = _load_json_object(raw)
    if set(data) != {"profiles"}:
        raise PolicyError("unexpected policy property")
    profiles = data["profiles"]
    if not isinstance(profiles, dict) or not profiles:
        raise PolicyError("policy profiles rejected")
    loaded: dict[str, Profile] = {}
    for name, body in profiles.items():
        if not isinstance(name, str) or NAME_RE.fullmatch(name) is None:
            raise PolicyError("policy profile name rejected")
        if not isinstance(body, dict):
            raise PolicyError("policy profile rejected")
        if set(body) != set(PROFILE_KEYS):
            raise PolicyError("unexpected policy property")
        account = _require_string(body, "account")
        project_id = _require_string(body, "project_id")
        configuration = _require_string(body, "gcloud_configuration")
        cloudsdk = _require_string(body, "cloudsdk_config")
        python_executable = _require_string(body, "python_executable")
        entrypoint = _require_string(body, "gcloud_python_entrypoint")
        if ACCOUNT_RE.fullmatch(account) is None:
            raise PolicyError("policy account rejected")
        if PROJECT_RE.fullmatch(project_id) is None:
            raise PolicyError("policy project rejected")
        if NAME_RE.fullmatch(configuration) is None:
            raise PolicyError("policy configuration rejected")
        cloudsdk_path = _absolute_path(cloudsdk)
        _outside_repository(cloudsdk_path)
        python_path = _absolute_path(python_executable)
        entry_path = _absolute_path(entrypoint)
        if python_path.name.lower() not in PYTHON_BASENAMES:
            raise PolicyError("python executable rejected")
        if python_path.name.lower() in BLOCKED_BASENAMES:
            raise PolicyError("python executable rejected")
        if entry_path.name.lower() != "gcloud.py":
            raise PolicyError("gcloud entrypoint rejected")
        loaded[name] = Profile(
            name=name,
            account=account,
            project_id=project_id,
            configuration=configuration,
            cloudsdk_config=str(cloudsdk_path),
            python_executable=str(python_path),
            gcloud_python_entrypoint=str(entry_path),
        )
    return Policy(profiles=loaded)


def load_policy_file(path: str) -> Policy:
    file_path = Path(path)
    if not file_path.is_absolute():
        raise PolicyError("policy path must be absolute")
    try:
        if not file_path.is_file():
            raise PolicyError("policy file is unavailable")
        size = file_path.stat().st_size
        if size > MAX_MESSAGE_BYTES:
            raise PolicyError("policy file is unavailable")
        raw = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PolicyError("policy file is unavailable") from exc
    return load_policy(raw)


def child_environment(profile: Profile, source: Mapping[str, str] | None = None) -> dict[str, str]:
    base = os.environ if source is None else source
    env: dict[str, str] = {}
    for key, value in base.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        upper = key.upper()
        if upper in DROP_ENV_EXACT or upper in PROXY_ENV or key in PROXY_ENV:
            continue
        if upper.startswith(DROP_ENV_PREFIXES) or upper.startswith("CLOUDSDK_PROXY"):
            continue
        env[key] = value
    env["CLOUDSDK_CONFIG"] = profile.cloudsdk_config
    env["CLOUDSDK_CORE_DISABLE_PROMPTS"] = "1"
    env["CLOUDSDK_CORE_DISABLE_USAGE_REPORTING"] = "1"
    return env


def _prefix(profile: Profile) -> list[str]:
    return [
        profile.python_executable,
        profile.gcloud_python_entrypoint,
        "--configuration",
        profile.configuration,
        "--account",
        profile.account,
        "--project",
        profile.project_id,
        "--quiet",
    ]


def command_config_describe(profile: Profile) -> list[str]:
    return _prefix(profile) + [
        "config",
        "configurations",
        "describe",
        profile.configuration,
        "--format=json(name,properties.core.account,properties.core.project)",
    ]


def command_auth_list(profile: Profile) -> list[str]:
    return _prefix(profile) + [
        "auth",
        "list",
        "--filter=status:ACTIVE",
        "--format=json(account,status)",
    ]


def command_project_describe(profile: Profile) -> list[str]:
    return _prefix(profile) + [
        "projects",
        "describe",
        profile.project_id,
        "--format=json(projectId,projectNumber,name,lifecycleState)",
    ]


def command_run_services(profile: Profile, region: str) -> list[str]:
    return _prefix(profile) + [
        "run",
        "services",
        "list",
        "--platform=managed",
        "--region",
        region,
        "--format=json(metadata.name,status.conditions)",
    ]


def command_run_service(profile: Profile, region: str, service: str) -> list[str]:
    return _prefix(profile) + [
        "run",
        "services",
        "describe",
        service,
        "--platform=managed",
        "--region",
        region,
        "--format=json(metadata.name,status.latestReadyRevisionName,status.conditions)",
    ]


def command_enabled_services(profile: Profile) -> list[str]:
    return _prefix(profile) + [
        "services",
        "list",
        "--enabled",
        "--format=json(config.name,config.title)",
    ]


class SubprocessBackend:
    """Runs the trusted Python + gcloud.py argv. No shell and no token tools."""

    def run(self, argv: list[str], env: Mapping[str, str], timeout: float, max_output: int) -> str:
        self._require_runtime(argv, env)
        startupinfo = None
        creationflags = 0
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0
            creationflags = subprocess.CREATE_NO_WINDOW
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=dict(env),
                shell=False,
                startupinfo=startupinfo,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise SafeToolError("command failed") from exc
        stdout_box: dict[str, Any] = {"data": bytearray(), "overflow": False}
        stderr_box: dict[str, Any] = {"data": bytearray(), "overflow": False}
        threads = [
            threading.Thread(target=_drain, args=(proc.stdout, stdout_box, max_output, proc), daemon=True),
            threading.Thread(target=_drain, args=(proc.stderr, stderr_box, max_output, proc), daemon=True),
        ]
        for thread in threads:
            thread.start()
        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                raise SafeToolError("command timed out")
        for thread in threads:
            thread.join(timeout=5)
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass
        if timed_out:
            raise SafeToolError("command timed out")
        if stdout_box["overflow"] or stderr_box["overflow"]:
            raise SafeToolError("output exceeded limit")
        if proc.returncode != 0:
            raise SafeToolError("command failed")
        try:
            return bytes(stdout_box["data"]).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SafeToolError("command returned unreadable output") from exc

    def _require_runtime(self, argv: list[str], env: Mapping[str, str]) -> None:
        if len(argv) < 10 or any(not isinstance(part, str) or "\x00" in part for part in argv):
            raise SafeToolError("command failed")
        for flag in ("--configuration", "--account", "--project", "--quiet"):
            if flag not in argv:
                raise SafeToolError("command failed")
        python_path = Path(argv[0])
        entry_path = Path(argv[1])
        if (
            not python_path.is_file()
            or python_path.name.lower() not in PYTHON_BASENAMES
            or python_path.name.lower() in BLOCKED_BASENAMES
        ):
            raise SafeToolError("command failed")
        if entry_path.name.lower() != "gcloud.py" or not entry_path.is_file():
            raise SafeToolError("command failed")
        config = env.get("CLOUDSDK_CONFIG")
        if not isinstance(config, str):
            raise SafeToolError("command failed")
        config_path = Path(config)
        try:
            if not config_path.is_absolute() or not config_path.is_dir():
                raise SafeToolError("command failed")
            _outside_repository(config_path)
        except PolicyError as exc:
            raise SafeToolError("command failed") from exc
        if env.get("CLOUDSDK_CORE_DISABLE_PROMPTS") != "1":
            raise SafeToolError("command failed")
        if env.get("CLOUDSDK_CORE_DISABLE_USAGE_REPORTING") != "1":
            raise SafeToolError("command failed")


def _drain(pipe: Any, box: dict[str, Any], limit: int, proc: Any) -> None:
    try:
        while True:
            chunk = pipe.read(8192)
            if not chunk:
                break
            if box["overflow"]:
                continue
            if len(box["data"]) + len(chunk) > limit:
                box["overflow"] = True
                box["data"].clear()
                proc.kill()
                continue
            box["data"].extend(chunk)
    finally:
        try:
            pipe.close()
        except OSError:
            return


class Session:
    def __init__(
        self,
        policy: Policy,
        backend: Any,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_output: int = DEFAULT_MAX_OUTPUT_BYTES,
        allowed_tools: tuple[str, ...] | None = None,
    ) -> None:
        self.policy = policy
        self.backend = backend
        self.timeout = timeout
        self.max_output = max_output
        self.initialized = False
        self.allowed_tools = tuple(TOOLS) if allowed_tools is None else tuple(allowed_tools)
        if not self.allowed_tools or any(name not in TOOLS for name in self.allowed_tools):
            raise PolicyError("tool allowlist rejected")


def _run_json(session: Session, profile: Profile, argv: list[str]) -> Any:
    env = child_environment(profile)
    try:
        text = session.backend.run(argv, env, session.timeout, session.max_output)
    except SafeToolError:
        raise
    except Exception as exc:
        raise SafeToolError("tool failed") from exc
    if not isinstance(text, str):
        raise SafeToolError("command returned unreadable output")
    if len(text.encode("utf-8")) > session.max_output:
        raise SafeToolError("output exceeded limit")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise SafeToolError("command returned unreadable output") from exc


def _profile_from_arguments(policy: Policy, arguments: Mapping[str, Any]) -> Profile:
    profile_name = arguments.get("profile")
    if not isinstance(profile_name, str) or NAME_RE.fullmatch(profile_name) is None:
        raise SafeToolError("invalid profile")
    profile = policy.profiles.get(profile_name)
    if profile is None:
        raise SafeToolError("unknown profile")
    return profile


def _reject_unknown(arguments: Mapping[str, Any], allowed: set[str]) -> None:
    if set(arguments) - allowed:
        raise SafeToolError("unknown arguments")


def _region(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not 5 <= len(value) <= 40
        or "-" not in value
        or "--" in value
        or REGION_RE.fullmatch(value) is None
    ):
        raise SafeToolError("invalid region")
    return value


def _service(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 63 or SERVICE_RE.fullmatch(value) is None:
        raise SafeToolError("invalid service")
    if "--" in value:
        raise SafeToolError("invalid service")
    return value


def verify_context(session: Session, profile: Profile) -> None:
    described = _run_json(session, profile, command_config_describe(profile))
    if not isinstance(described, dict):
        raise SafeToolError("configuration does not match policy")
    if described.get("name") != profile.configuration:
        raise SafeToolError("configuration does not match policy")
    properties = described.get("properties")
    if not isinstance(properties, dict):
        raise SafeToolError("configuration does not match policy")
    core = properties.get("core")
    if not isinstance(core, dict):
        raise SafeToolError("configuration does not match policy")
    if core.get("account") != profile.account:
        raise SafeToolError("account does not match policy")
    if core.get("project") != profile.project_id:
        raise SafeToolError("project does not match policy")
    listed = _run_json(session, profile, command_auth_list(profile))
    if not isinstance(listed, list) or len(listed) != 1 or not isinstance(listed[0], dict):
        raise SafeToolError("active account is not the policy account")
    if listed[0].get("account") != profile.account or listed[0].get("status") != "ACTIVE":
        raise SafeToolError("active account is not the policy account")


def _display(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 120 or value.strip() == "":
        return None
    if DISPLAY_CONTROL.search(value) or "ya29." in value or "://" in value:
        return None
    return value


def _project_number(value: Any) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool) and 0 < value < 10**20:
        return str(value)
    if isinstance(value, str) and value.isdigit() and 1 <= len(value) <= 20:
        return value
    return None


def project_projection(payload: Any, profile: Profile) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise SafeToolError("command returned unreadable output")
    if payload.get("projectId") != profile.project_id:
        raise SafeToolError("project does not match policy")
    number = _project_number(payload.get("projectNumber"))
    state = payload.get("lifecycleState")
    allowed_states = {"ACTIVE", "DELETE_REQUESTED", "DELETE_IN_PROGRESS", "LIFECYCLE_STATE_UNSPECIFIED"}
    result: dict[str, Any] = {"project_id": profile.project_id}
    if number is not None:
        result["project_number"] = number
    name = _display(payload.get("name"))
    if name is not None:
        result["name"] = name
    result["lifecycle_state"] = state if isinstance(state, str) and state in allowed_states else "UNKNOWN"
    return result


def _conditions(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    cleaned: list[dict[str, str]] = []
    for item in value[:20]:
        if not isinstance(item, dict):
            continue
        ctype = item.get("type")
        status = item.get("status")
        if not isinstance(ctype, str) or CONDITION_RE.fullmatch(ctype) is None:
            continue
        if status not in {"True", "False", "Unknown"}:
            continue
        cleaned.append({"type": ctype, "status": status})
    return cleaned


def services_projection(payload: Any, region: str) -> dict[str, Any]:
    if not isinstance(payload, list):
        raise SafeToolError("command returned unreadable output")
    services: list[dict[str, Any]] = []
    for item in payload[:MAX_LIST_ITEMS]:
        if not isinstance(item, dict):
            continue
        meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        name = meta.get("name")
        if not isinstance(name, str) or SERVICE_RE.fullmatch(name) is None:
            continue
        status = item.get("status") if isinstance(item.get("status"), dict) else {}
        ready = any(cond["type"] == "Ready" and cond["status"] == "True" for cond in _conditions(status.get("conditions")))
        services.append({"name": name, "region": region, "ready": ready})
    return {"region": region, "services": services, "truncated": len(payload) > MAX_LIST_ITEMS}


def service_projection(payload: Any, region: str, service: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise SafeToolError("command returned unreadable output")
    meta = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    if meta.get("name") != service:
        raise SafeToolError("command returned unreadable output")
    status = payload.get("status") if isinstance(payload.get("status"), dict) else {}
    revision = status.get("latestReadyRevisionName")
    conditions = _conditions(status.get("conditions"))
    ready = any(cond["type"] == "Ready" and cond["status"] == "True" for cond in conditions)
    result: dict[str, Any] = {
        "name": service,
        "region": region,
        "ready": ready,
        "conditions": conditions,
    }
    if isinstance(revision, str) and REVISION_RE.fullmatch(revision):
        result["latest_ready_revision"] = revision
    return result


def apis_projection(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, list):
        raise SafeToolError("command returned unreadable output")
    services: list[dict[str, str]] = []
    for item in payload[:500]:
        if not isinstance(item, dict):
            continue
        config = item.get("config") if isinstance(item.get("config"), dict) else item
        if not isinstance(config, dict):
            continue
        name = config.get("name")
        title = config.get("title")
        if not isinstance(name, str) or API_RE.fullmatch(name) is None:
            continue
        shown = _display(title) if isinstance(title, str) else None
        entry: dict[str, str] = {"name": name}
        if shown is not None:
            entry["title"] = shown
        services.append(entry)
    return {"services": services, "truncated": len(payload) > 500}


def context_status(session: Session, arguments: Mapping[str, Any]) -> dict[str, Any]:
    _reject_unknown(arguments, {"profile"})
    profile = _profile_from_arguments(session.policy, arguments)
    verify_context(session, profile)
    return {
        "profile": profile.name,
        "configuration": profile.configuration,
        "account": profile.account,
        "project_id": profile.project_id,
        "account_active": True,
        "project_matches": True,
    }


def project_info(session: Session, arguments: Mapping[str, Any]) -> dict[str, Any]:
    _reject_unknown(arguments, {"profile"})
    profile = _profile_from_arguments(session.policy, arguments)
    verify_context(session, profile)
    payload = _run_json(session, profile, command_project_describe(profile))
    return project_projection(payload, profile)


def cloud_run_services(session: Session, arguments: Mapping[str, Any]) -> dict[str, Any]:
    _reject_unknown(arguments, {"profile", "region"})
    profile = _profile_from_arguments(session.policy, arguments)
    region = _region(arguments.get("region"))
    verify_context(session, profile)
    payload = _run_json(session, profile, command_run_services(profile, region))
    return services_projection(payload, region)


def cloud_run_service_status(session: Session, arguments: Mapping[str, Any]) -> dict[str, Any]:
    _reject_unknown(arguments, {"profile", "region", "service"})
    profile = _profile_from_arguments(session.policy, arguments)
    region = _region(arguments.get("region"))
    service = _service(arguments.get("service"))
    verify_context(session, profile)
    payload = _run_json(session, profile, command_run_service(profile, region, service))
    return service_projection(payload, region, service)


def enabled_services(session: Session, arguments: Mapping[str, Any]) -> dict[str, Any]:
    _reject_unknown(arguments, {"profile"})
    profile = _profile_from_arguments(session.policy, arguments)
    verify_context(session, profile)
    payload = _run_json(session, profile, command_enabled_services(profile))
    return apis_projection(payload)


TOOLS: dict[str, Callable[[Session, Mapping[str, Any]], dict[str, Any]]] = {
    "context_status": context_status,
    "project_info": project_info,
    "cloud_run_services": cloud_run_services,
    "cloud_run_service_status": cloud_run_service_status,
    "enabled_services": enabled_services,
}

TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "context_status": {
        "description": "Report whether the isolated gcloud configuration's active account and project match the named policy profile.",
        "properties": {"profile": {"type": "string", "minLength": 1, "maxLength": 63}},
        "required": ["profile"],
    },
    "project_info": {
        "description": "Return the allowlisted project's id, number, display name, and lifecycle state.",
        "properties": {"profile": {"type": "string", "minLength": 1, "maxLength": 63}},
        "required": ["profile"],
    },
    "cloud_run_services": {
        "description": "List Cloud Run service names and Ready state in one validated region.",
        "properties": {
            "profile": {"type": "string", "minLength": 1, "maxLength": 63},
            "region": {"type": "string", "minLength": 1, "maxLength": 40},
        },
        "required": ["profile", "region"],
    },
    "cloud_run_service_status": {
        "description": "Return Ready conditions and the latest ready revision name for one Cloud Run service.",
        "properties": {
            "profile": {"type": "string", "minLength": 1, "maxLength": 63},
            "region": {"type": "string", "minLength": 1, "maxLength": 40},
            "service": {"type": "string", "minLength": 1, "maxLength": 63},
        },
        "required": ["profile", "region", "service"],
    },
    "enabled_services": {
        "description": "List enabled API names and titles for the allowlisted project.",
        "properties": {"profile": {"type": "string", "minLength": 1, "maxLength": 63}},
        "required": ["profile"],
    },
}


def tool_descriptors(allowed_tools=None) -> list[dict[str, Any]]:
    items = []
    for name, schema in TOOL_SCHEMAS.items():
        if allowed_tools is not None and name not in allowed_tools:
            continue
        items.append(
            {
                "name": name,
                "description": schema["description"],
                "inputSchema": {
                    "type": "object",
                    "properties": schema["properties"],
                    "required": schema["required"],
                    "additionalProperties": False,
                },
                "annotations": {"readOnlyHint": True, "destructiveHint": False},
            }
        )
    return items


def _text_result(payload: Any, is_error: bool = False) -> dict[str, Any]:
    if isinstance(payload, str):
        text = payload
    else:
        text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def dispatch_tool(session: Session, name: Any, arguments: Any) -> dict[str, Any]:
    if not isinstance(name, str) or name not in TOOLS or name not in session.allowed_tools:
        raise SafeToolError("unknown tool")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise SafeToolError("unknown arguments")
    return TOOLS[name](session, arguments)


def handle_message(message: Any, session: Session) -> dict[str, Any] | None:
    if not isinstance(message, dict):
        return _rpc_error(None, -32600, "invalid request")
    if message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return _rpc_error(message.get("id"), -32600, "invalid request")
    method = message["method"]
    has_id = "id" in message
    request_id = message.get("id")
    if method in {"notifications/initialized", "initialized"} and not has_id:
        session.initialized = True
        return None
    if not has_id:
        return None
    if method == "initialize":
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        requested = params.get("protocolVersion")
        version = requested if isinstance(requested, str) and requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        session.initialized = True
        return _rpc_result(
            request_id,
            {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )
    if method == "ping":
        return _rpc_result(request_id, {})
    if not session.initialized:
        return _rpc_error(request_id, -32600, "server not initialized")
    if method == "tools/list":
        return _rpc_result(request_id, {"tools": tool_descriptors(session.allowed_tools)})
    if method == "tools/call":
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if set(params) - {"name", "arguments", "_meta"}:
            return _rpc_error(request_id, -32602, "unknown arguments")
        if "_meta" in params:
            meta = params["_meta"]
            try:
                valid_meta = isinstance(meta, dict) and len(
                    json.dumps(meta, separators=(",", ":")).encode("utf-8")) <= 8192
            except (TypeError, ValueError, RecursionError):
                valid_meta = False
            if not valid_meta:
                return _rpc_error(request_id, -32602, "invalid request metadata")
        try:
            payload = dispatch_tool(session, params.get("name"), params.get("arguments"))
        except SafeToolError as exc:
            return _rpc_result(request_id, _text_result(exc.public_message, is_error=True))
        return _rpc_result(request_id, _text_result(payload, is_error=False))
    return _rpc_error(request_id, -32601, "method not found")


def _rpc_result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def read_message(stdin: Any, max_bytes: int = MAX_MESSAGE_BYTES) -> dict[str, Any] | None:
    # Standard MCP stdio is one UTF-8 JSON-RPC object per line.
    body = stdin.readline(max_bytes + 2)
    if body == b"":
        return None
    if len(body) > max_bytes + 1 or not body.endswith(b"\n"):
        raise SafeToolError("framing rejected")
    try:
        message = json.loads(body.decode("utf-8"), object_pairs_hook=_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise SafeToolError("parse error") from exc
    if not isinstance(message, dict):
        raise SafeToolError("invalid request")
    return message


def write_message(stdout: Any, payload: Mapping[str, Any]) -> None:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    stdout.write(body + b"\n")
    stdout.flush()


def serve(stdin: Any, stdout: Any, session: Session) -> int:
    while True:
        try:
            message = read_message(stdin)
        except SafeToolError as exc:
            if exc.public_message == "parse error":
                write_message(stdout, _rpc_error(None, -32700, "parse error"))
                continue
            if exc.public_message == "invalid request":
                write_message(stdout, _rpc_error(None, -32600, "invalid request"))
                continue
            return 1
        if message is None:
            return 0
        try:
            response = handle_message(message, session)
        except Exception:
            response = _rpc_error(message.get("id"), -32603, "tool failed")
        if response is not None:
            write_message(stdout, response)


def run_self_test() -> int:
    import unittest

    start = str(Path(__file__).resolve().parent)
    suite = unittest.defaultTestLoader.discover(start, pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only GCP metadata MCP server")
    parser.add_argument("--policy", help="Absolute path to the trusted policy JSON")
    parser.add_argument("--self-test", action="store_true", help="Run offline unit tests")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else 2
    if args.self_test:
        if args.policy is not None:
            sys.stderr.write("policy rejected\n")
            return 2
        return run_self_test()
    if not args.policy:
        sys.stderr.write("policy path must be absolute\n")
        return 2
    try:
        policy = load_policy_file(args.policy)
    except PolicyError as exc:
        sys.stderr.write(exc.public_message + "\n")
        return 2
    session = Session(policy, SubprocessBackend())
    return serve(sys.stdin.buffer, sys.stdout.buffer, session)


if __name__ == "__main__":
    sys.exit(main())
