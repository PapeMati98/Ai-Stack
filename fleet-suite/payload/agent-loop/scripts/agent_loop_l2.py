from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fnmatch
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Iterator, Mapping


VERSION = "0.2.1"
SCHEMA_VERSION = 2
ACTIVE_PHASES = {"IMPLEMENTING", "VERIFYING", "REVIEWING", "REPAIRING"}
TERMINAL_PHASES = {"ACCEPTED", "ESCALATED", "STOPPED"}
VERDICT_PATTERN = re.compile(r"(?<![A-Z_])AGENT_LOOP_VERDICT=(PASS|REJECT|ESCALATE)(?![A-Z_])")
LOOP_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
SECRET_ENV_PATTERN = re.compile(
    r"(TOKEN|SECRET|PASSWORD|PASSWD|API[_-]?KEY|PRIVATE[_-]?KEY|COOKIE|CREDENTIAL)",
    re.IGNORECASE,
)


class PilotError(RuntimeError):
    pass


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode(
        "ascii"
    )


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PilotError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicate_keys)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PilotError(f"Cannot read {label} at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PilotError(f"{label} must be a JSON object")
    return value


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("xb") as handle:
        handle.write(canonical_json_bytes(value))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def append_event(path: Path, event: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(dict(event), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("ascii")
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def is_filesystem_link(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise PilotError(f"Cannot inspect path {path}: {exc}") from exc
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction) and is_junction():
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def require_plain_file(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file() or is_filesystem_link(resolved):
        raise PilotError(f"{label} must be a plain file: {resolved}")
    return resolved


def require_plain_directory(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_dir() or is_filesystem_link(resolved):
        raise PilotError(f"{label} must be a plain directory: {resolved}")
    return resolved


def require_exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise PilotError(f"{label} key mismatch; missing={missing}, extra={extra}")


def run_process(
    command: list[str],
    *,
    cwd: Path,
    timeout: int,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=str(cwd),
            env=dict(env) if env is not None else None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return subprocess.CompletedProcess(command, 124, stdout, stderr + f"\nTimeout after {timeout}s")


def run_git(args: list[str], cwd: Path, *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return run_process(["git", *args], cwd=cwd, timeout=timeout)


def git_output(args: list[str], cwd: Path, label: str) -> str:
    result = run_git(args, cwd)
    if result.returncode != 0:
        raise PilotError(f"{label} failed: {result.stderr.strip()[:1000]}")
    return result.stdout.strip()


def repository_inventory(project: Path) -> dict[str, Any]:
    result = run_git(["ls-files", "-z"], project)
    if result.returncode != 0:
        raise PilotError(f"Cannot inventory repository: {result.stderr.strip()[:1000]}")
    files: list[dict[str, Any]] = []
    total_bytes = 0
    for raw in result.stdout.split("\x00"):
        if not raw:
            continue
        relative = normalize_pattern(raw, "tracked repository path")
        path = (project / relative).resolve()
        try:
            path.relative_to(project)
        except ValueError as exc:
            raise PilotError(f"Tracked path escaped the repository: {relative}") from exc
        if not path.is_file() or is_filesystem_link(path):
            raise PilotError(f"Tracked context path is not a plain file: {relative}")
        size = path.stat().st_size
        total_bytes += size
        files.append({"path": relative, "size": size})
    return {
        "file_count": len(files),
        "total_bytes": total_bytes,
        "manifest_sha256": sha256_bytes(canonical_json_bytes(files)),
    }


def normalize_pattern(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PilotError(f"{label} must be a non-empty string")
    normalized = value.replace("\\", "/").strip()
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise PilotError(f"{label} must be relative")
    if any(part == ".." for part in normalized.split("/")):
        raise PilotError(f"{label} must not traverse parents")
    return normalized


def validate_actor(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PilotError(f"{label} must be an object")
    require_exact_keys(
        value,
        {"agent", "model", "execution_profile", "assessment_file", "timeout_seconds"},
        label,
    )
    if not isinstance(value["agent"], str) or not value["agent"].strip():
        raise PilotError(f"{label}.agent must be a non-empty string")
    for key in ("model", "execution_profile"):
        if value[key] is not None and (not isinstance(value[key], str) or not value[key].strip()):
            raise PilotError(f"{label}.{key} must be null or a non-empty string")
    timeout = value["timeout_seconds"]
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 30 <= timeout <= 1800:
        raise PilotError(f"{label}.timeout_seconds must be 30..1800")
    assessment = require_plain_file(Path(str(value["assessment_file"])), f"{label}.assessment_file")
    load_json(assessment, f"{label} assessment")
    return {
        "agent": value["agent"].strip(),
        "model": value["model"],
        "execution_profile": value["execution_profile"],
        "assessment_file": str(assessment),
        "timeout_seconds": timeout,
    }


def validate_contract(raw: Mapping[str, Any], *, runtime_root: Path) -> dict[str, Any]:
    require_exact_keys(
        raw,
        {
            "schema_version",
            "loop_id",
            "goal",
            "non_goals",
            "project",
            "base_ref",
            "scope",
            "executor",
            "implementer",
            "validator",
            "verification",
            "limits",
            "network_allowed",
            "kill_switch",
        },
        "contract",
    )
    if raw["schema_version"] != SCHEMA_VERSION:
        raise PilotError("Unsupported contract schema_version")
    loop_id = raw["loop_id"]
    if not isinstance(loop_id, str) or not LOOP_ID_PATTERN.fullmatch(loop_id):
        raise PilotError("loop_id must match ^[a-z0-9][a-z0-9-]{1,63}$")
    goal = raw["goal"]
    if not isinstance(goal, str) or not goal.strip() or len(goal) > 4000:
        raise PilotError("goal must be a non-empty string up to 4000 characters")
    non_goals = raw["non_goals"]
    if not isinstance(non_goals, list) or not all(isinstance(item, str) for item in non_goals):
        raise PilotError("non_goals must be a string list")
    if raw["base_ref"] != "HEAD":
        raise PilotError("Agent Loop L2 accepts only base_ref=HEAD")
    if raw["network_allowed"] is not False:
        raise PilotError("Agent Loop L2 requires network_allowed=false")

    project = require_plain_directory(Path(str(raw["project"])), "project")
    repo_root = Path(git_output(["rev-parse", "--show-toplevel"], project, "git root")).resolve()
    if repo_root != project:
        raise PilotError("The L2 project must be the Git repository root")
    status = git_output(["status", "--porcelain=v1", "--untracked-files=all"], project, "git status")
    if status:
        raise PilotError("The L2 project baseline must be clean")
    baseline_head = git_output(["rev-parse", "HEAD"], project, "baseline HEAD")
    inventory = repository_inventory(project)

    scope = raw["scope"]
    if not isinstance(scope, dict):
        raise PilotError("scope must be an object")
    require_exact_keys(scope, {"allowed_paths", "protected_paths"}, "scope")
    allowed = scope["allowed_paths"]
    protected = scope["protected_paths"]
    if not isinstance(allowed, list) or not allowed:
        raise PilotError("scope.allowed_paths must be a non-empty list")
    if not isinstance(protected, list):
        raise PilotError("scope.protected_paths must be a list")
    allowed_paths = [normalize_pattern(item, "allowed path") for item in allowed]
    protected_paths = [normalize_pattern(item, "protected path") for item in protected]

    implementer = validate_actor(raw["implementer"], "implementer")
    validator = validate_actor(raw["validator"], "validator")
    if implementer["agent"].casefold() == validator["agent"].casefold():
        raise PilotError("Implementer and validator must use different agents")
    if implementer["agent"].casefold() not in {"claude", "codex"}:
        raise PilotError("Implementer is not certified for Agent Loop L2-thin")
    if validator["agent"].casefold() not in {"claude", "codex"}:
        raise PilotError("Validator is not certified for Agent Loop L2-thin")

    executor = raw["executor"]
    if not isinstance(executor, dict):
        raise PilotError("executor must be an object")
    require_exact_keys(
        executor,
        {
            "runtime_profile",
            "knowledge_mode",
            "max_prompt_bytes",
            "max_model_tool_calls",
            "max_task_bytes",
            "max_repository_files",
            "max_repository_bytes",
        },
        "executor",
    )
    if executor["runtime_profile"] != "l2-thin":
        raise PilotError("Agent Loop L2 requires executor.runtime_profile=l2-thin")
    if executor["knowledge_mode"] != "none":
        raise PilotError("Agent Loop L2-thin requires executor.knowledge_mode=none")
    executor_bounds = {
        "max_prompt_bytes": (1024, 100_000),
        "max_model_tool_calls": (1, 12),
        "max_task_bytes": (256, 100_000),
        "max_repository_files": (1, 10_000),
        "max_repository_bytes": (1, 100_000_000),
    }
    for key, (minimum, maximum) in executor_bounds.items():
        value = executor[key]
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not minimum <= value <= maximum
        ):
            raise PilotError(f"executor.{key} must be {minimum}..{maximum}")
    if inventory["file_count"] > executor["max_repository_files"]:
        raise PilotError("Repository file count exceeds the executor context gate")
    if inventory["total_bytes"] > executor["max_repository_bytes"]:
        raise PilotError("Repository bytes exceed the executor context gate")

    verification = raw["verification"]
    if not isinstance(verification, dict):
        raise PilotError("verification must be an object")
    require_exact_keys(
        verification,
        {"trusted_runner", "trusted_runner_sha256", "timeout_seconds"},
        "verification",
    )
    runner = require_plain_file(Path(str(verification["trusted_runner"])), "trusted runner")
    trusted_root = (runtime_root / "trusted_verifiers").resolve()
    try:
        runner.relative_to(trusted_root)
    except ValueError as exc:
        raise PilotError("Trusted runner must be bundled below runtime/trusted_verifiers") from exc
    expected_runner_sha = verification["trusted_runner_sha256"]
    if not isinstance(expected_runner_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_runner_sha):
        raise PilotError("verification.trusted_runner_sha256 must be lowercase SHA-256")
    if sha256_file(runner) != expected_runner_sha:
        raise PilotError("Trusted runner fingerprint mismatch")
    verifier_timeout = verification["timeout_seconds"]
    if not isinstance(verifier_timeout, int) or isinstance(verifier_timeout, bool) or not 5 <= verifier_timeout <= 600:
        raise PilotError("verification.timeout_seconds must be 5..600")

    limits = raw["limits"]
    if not isinstance(limits, dict):
        raise PilotError("limits must be an object")
    require_exact_keys(
        limits,
        {
            "max_repairs",
            "max_wall_seconds",
            "max_total_tokens",
            "max_fresh_input_tokens",
            "max_cache_read_tokens",
            "max_cache_write_tokens",
            "max_output_tokens",
            "max_reasoning_tokens",
            "unknown_token_policy",
            "max_changed_files",
            "max_diff_bytes",
        } | ({"token_policy"} if "token_policy" in limits else set()),
        "limits",
    )
    token_policy = limits.get("token_policy", "hard")
    if token_policy not in ("hard", "estimate"):
        raise PilotError("limits.token_policy must be hard or estimate")
    if limits["max_repairs"] not in (0, 1):
        raise PilotError("limits.max_repairs must be 0 or 1")
    if not isinstance(limits["max_wall_seconds"], int) or not 60 <= limits["max_wall_seconds"] <= 3600:
        raise PilotError("limits.max_wall_seconds must be 60..3600")
    for key in (
        "max_total_tokens",
        "max_fresh_input_tokens",
        "max_cache_read_tokens",
        "max_cache_write_tokens",
        "max_output_tokens",
        "max_reasoning_tokens",
    ):
        maximum = limits[key]
        if maximum is not None and (
            not isinstance(maximum, int)
            or isinstance(maximum, bool)
            or maximum <= 0
        ):
            raise PilotError(f"limits.{key} must be null or a positive integer")
    if limits["unknown_token_policy"] not in ("allow", "stop"):
        raise PilotError("limits.unknown_token_policy must be allow or stop")
    if not isinstance(limits["max_changed_files"], int) or not 1 <= limits["max_changed_files"] <= 100:
        raise PilotError("limits.max_changed_files must be 1..100")
    if not isinstance(limits["max_diff_bytes"], int) or not 1 <= limits["max_diff_bytes"] <= 5_000_000:
        raise PilotError("limits.max_diff_bytes must be 1..5000000")

    kill_switch = raw["kill_switch"]
    if not isinstance(kill_switch, dict):
        raise PilotError("kill_switch must be an object")
    require_exact_keys(kill_switch, {"file", "env"}, "kill_switch")
    if kill_switch["file"] != "STOP":
        raise PilotError("The L2 kill-switch file must be STOP")
    if not isinstance(kill_switch["env"], str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", kill_switch["env"]):
        raise PilotError("kill_switch.env must be an uppercase environment name")

    return {
        "schema_version": SCHEMA_VERSION,
        "loop_id": loop_id,
        "goal": goal.strip(),
        "non_goals": non_goals,
        "project": str(project),
        "base_ref": "HEAD",
        "baseline_head": baseline_head,
        "scope": {"allowed_paths": allowed_paths, "protected_paths": protected_paths},
        "executor": {**dict(executor), "repository_inventory": inventory},
        "implementer": implementer,
        "validator": validator,
        "verification": {
            "trusted_runner": str(runner),
            "trusted_runner_sha256": expected_runner_sha,
            "timeout_seconds": verifier_timeout,
        },
        "limits": dict(limits),
        "network_allowed": False,
        "kill_switch": dict(kill_switch),
    }


def runtime_file_manifest(runtime_root: Path) -> list[dict[str, Any]]:
    root = require_plain_directory(runtime_root, "Agent Fleet runtime root")
    files: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if "__pycache__" in path.parts or path.suffix.casefold() in {".pyc", ".pyo"}:
            continue
        if not path.is_file():
            continue
        if is_filesystem_link(path):
            raise PilotError(f"Agent Fleet runtime contains a linked file: {path}")
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(root).as_posix()
        except ValueError as exc:
            raise PilotError(f"Agent Fleet runtime path escaped its root: {path}") from exc
        files.append(
            {
                "path": relative,
                "size": resolved.stat().st_size,
                "sha256": sha256_file(resolved),
            }
        )
    if not files:
        raise PilotError("Agent Fleet runtime manifest is empty")
    return files


def runtime_identity(fleet_script: Path) -> dict[str, Any]:
    script = require_plain_file(fleet_script, "Agent Fleet script")
    current = require_plain_directory(script.parent.parent.parent, "Agent Fleet runtime root")
    marker = current / ".agent-fleet-suite-runtime.json"
    if not marker.is_file():
        raise PilotError("Agent Fleet runtime marker is absent")
    marker_value = load_json(marker, "Agent Fleet runtime marker")
    manifest = runtime_file_manifest(current)
    return {
        "fleet_script": str(script),
        "fleet_script_sha256": sha256_file(script),
        "runtime_root": str(current),
        "runtime_manifest": manifest,
        "runtime_manifest_sha256": sha256_bytes(canonical_json_bytes(manifest)),
        "runtime_marker": marker_value,
        "runtime_marker_sha256": sha256_file(marker),
    }


def controller_identity() -> dict[str, str]:
    script = require_plain_file(Path(__file__), "Agent Loop controller")
    return {"script": str(script), "script_sha256": sha256_file(script)}


def freeze_actor_input(
    actor: dict[str, Any], inputs: Path, name: str, *, published_inputs: Path | None = None
) -> dict[str, Any]:
    source = Path(actor["assessment_file"])
    payload = source.read_bytes()
    target = inputs / f"{name}-assessment.json"
    target.write_bytes(payload)
    frozen = dict(actor)
    published_target = (published_inputs or inputs) / target.name
    frozen["assessment_file"] = str(published_target.resolve())
    frozen["assessment_sha256"] = sha256_bytes(payload)
    return frozen


def prepare_loop(contract_path: Path, state_root: Path, fleet_script: Path) -> dict[str, Any]:
    runtime_root = Path(__file__).resolve().parent
    raw = load_json(require_plain_file(contract_path, "contract"), "contract")
    contract = validate_contract(raw, runtime_root=runtime_root)
    identity = runtime_identity(fleet_script)
    project = Path(contract["project"])
    state_root = state_root.expanduser().resolve()
    try:
        state_root.relative_to(project)
    except ValueError:
        pass
    else:
        raise PilotError("State root must be outside the project repository")
    state_root.mkdir(parents=True, exist_ok=True)
    if is_filesystem_link(state_root):
        raise PilotError("State root must not be a link or junction")
    loop_dir = state_root / contract["loop_id"]
    if loop_dir.exists():
        raise PilotError(f"Loop directory already exists: {loop_dir}")
    staging = state_root / f".{contract['loop_id']}.prepare-{uuid.uuid4().hex}"
    try:
        staging.mkdir()
        inputs = staging / "inputs"
        inputs.mkdir()
        (staging / "evidence").mkdir()
        (staging / "fleet-runs").mkdir()

        final_inputs = loop_dir / "inputs"
        contract["implementer"] = freeze_actor_input(
            contract["implementer"], inputs, "implementer", published_inputs=final_inputs
        )
        contract["validator"] = freeze_actor_input(
            contract["validator"], inputs, "validator", published_inputs=final_inputs
        )
        frozen = {
            "schema_version": SCHEMA_VERSION,
            "agent_loop_version": VERSION,
            "created_at": utc_now(),
            "contract": contract,
            "controller_runtime": controller_identity(),
            "fleet_runtime": identity,
        }
        contract_hash = sha256_bytes(canonical_json_bytes(frozen))
        atomic_json(staging / "contract.json", frozen)
        (staging / "contract.sha256").write_text(contract_hash + "\n", encoding="ascii")
        state = {
            "schema_version": SCHEMA_VERSION,
            "loop_id": contract["loop_id"],
            "agent_loop_version": VERSION,
            "contract_hash": contract_hash,
            "phase": "AWAITING_GO",
            "created_at": frozen["created_at"],
            "approved_at": None,
            "approved_by": None,
            "started_at": None,
            "finished_at": None,
            "reason": None,
            "attempts": [],
            "tokens_total": 0,
            "fresh_input_tokens_total": 0,
            "cache_read_tokens_total": 0,
            "cache_write_tokens_total": 0,
            "output_tokens_total": 0,
            "reasoning_tokens_total": 0,
            "known_cost_usd_total": 0.0,
            "token_telemetry_complete": True,
            "cost_telemetry_complete": True,
            "token_warnings": [],
            "candidate": None,
            "manifest_path": None,
            "cleanup": None,
        }
        atomic_json(staging / "state.json", state)
        append_event(
            staging / "events.jsonl",
            {"event": "PREPARED", "at": utc_now(), "contract_hash": contract_hash},
        )
        os.replace(staging, loop_dir)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {
        "action": "prepare",
        "status": "AWAITING_GO",
        "loop_dir": str(loop_dir),
        "contract_hash": contract_hash,
        "baseline_head": contract["baseline_head"],
        "writes_performed": 7,
    }


def load_loop(loop_dir: Path) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    root = require_plain_directory(loop_dir, "loop directory")
    frozen = load_json(root / "contract.json", "frozen contract")
    expected = (root / "contract.sha256").read_text(encoding="ascii").strip()
    actual = sha256_bytes(canonical_json_bytes(frozen))
    if actual != expected:
        raise PilotError("Frozen contract drift detected")
    state = load_json(root / "state.json", "state")
    if state.get("contract_hash") != actual:
        raise PilotError("State references a different contract hash")
    contract = frozen.get("contract")
    if not isinstance(contract, dict):
        raise PilotError("Frozen contract payload is invalid")
    controller = frozen.get("controller_runtime")
    if not isinstance(controller, dict):
        raise PilotError("Frozen controller runtime identity is invalid")
    controller_script = require_plain_file(
        Path(str(controller.get("script", ""))), "Agent Loop controller"
    )
    if controller_script != Path(__file__).resolve():
        raise PilotError("Frozen contract references another Agent Loop controller")
    if sha256_file(controller_script) != controller.get("script_sha256"):
        raise PilotError("Agent Loop controller drift detected")
    for actor_name in ("implementer", "validator"):
        actor = contract.get(actor_name)
        if not isinstance(actor, dict):
            raise PilotError(f"Frozen {actor_name} is invalid")
        assessment = require_plain_file(Path(actor["assessment_file"]), f"{actor_name} assessment")
        if sha256_file(assessment) != actor.get("assessment_sha256"):
            raise PilotError(f"Frozen {actor_name} assessment drift detected")
    runner = require_plain_file(Path(contract["verification"]["trusted_runner"]), "trusted runner")
    if sha256_file(runner) != contract["verification"]["trusted_runner_sha256"]:
        raise PilotError("Trusted runner drift detected")
    fleet = frozen.get("fleet_runtime")
    if not isinstance(fleet, dict):
        raise PilotError("Fleet runtime identity is invalid")
    fleet_script = require_plain_file(Path(fleet["fleet_script"]), "Agent Fleet script")
    if sha256_file(fleet_script) != fleet["fleet_script_sha256"]:
        raise PilotError("Agent Fleet script drift detected")
    runtime_root = require_plain_directory(
        Path(str(fleet.get("runtime_root", ""))), "Agent Fleet runtime root"
    )
    if runtime_root != fleet_script.parent.parent.parent.resolve():
        raise PilotError("Agent Fleet runtime root does not contain the frozen Fleet script")
    expected_manifest = fleet.get("runtime_manifest")
    if not isinstance(expected_manifest, list):
        raise PilotError("Frozen Agent Fleet runtime manifest is invalid")
    actual_manifest = runtime_file_manifest(runtime_root)
    actual_manifest_sha = sha256_bytes(canonical_json_bytes(actual_manifest))
    if (
        actual_manifest != expected_manifest
        or actual_manifest_sha != fleet.get("runtime_manifest_sha256")
    ):
        raise PilotError("Agent Fleet runtime manifest drift detected")
    return root, frozen, state


def save_state(loop_dir: Path, state: dict[str, Any]) -> None:
    atomic_json(loop_dir / "state.json", state)


@contextlib.contextmanager
def loop_lock(loop_dir: Path) -> Iterator[None]:
    lock = loop_dir / ".lock"
    payload = canonical_json_bytes(
        {"pid": os.getpid(), "host": socket.gethostname(), "created_at": utc_now()}
    )
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise PilotError(f"Loop lock is already held: {lock}") from exc
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        yield
    finally:
        with contextlib.suppress(FileNotFoundError):
            lock.unlink()


def process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error == 5:
                return True
            if error == 87:
                return False
            raise PilotError(f"Cannot inspect loop lock process {pid}: WinError {error}")
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                raise PilotError(
                    f"Cannot read loop lock process {pid}: WinError {ctypes.get_last_error()}"
                )
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def approve_loop(loop_dir: Path, expected_hash: str, approved_by: str) -> dict[str, Any]:
    root, _, state = load_loop(loop_dir)
    if state["phase"] != "AWAITING_GO":
        raise PilotError("Only AWAITING_GO can be approved")
    if expected_hash != state["contract_hash"]:
        raise PilotError("Approval hash does not match the frozen contract")
    if not approved_by.strip():
        raise PilotError("approved_by must be non-empty")
    state["phase"] = "READY"
    state["approved_at"] = utc_now()
    state["approved_by"] = approved_by.strip()
    save_state(root, state)
    append_event(
        root / "events.jsonl",
        {"event": "APPROVED", "at": state["approved_at"], "by": state["approved_by"]},
    )
    return {"action": "approve", "status": "READY", "loop_dir": str(root), "writes_performed": 2}


def kill_switch_active(loop_dir: Path, contract: Mapping[str, Any]) -> str | None:
    if (loop_dir / str(contract["kill_switch"]["file"])).exists():
        return "kill-switch file is present"
    value = os.environ.get(str(contract["kill_switch"]["env"]), "").strip().casefold()
    if value in {"1", "true", "yes", "on", "stop"}:
        return "kill-switch environment variable is active"
    return None


def ensure_baseline_unchanged(contract: Mapping[str, Any]) -> None:
    project = Path(str(contract["project"]))
    head = git_output(["rev-parse", "HEAD"], project, "baseline HEAD")
    if head != contract["baseline_head"]:
        raise PilotError("Base repository HEAD changed after approval")
    status = git_output(["status", "--porcelain=v1", "--untracked-files=all"], project, "baseline status")
    if status:
        raise PilotError("Base repository became dirty after approval")


def ensure_executor_context_unchanged(contract: Mapping[str, Any]) -> None:
    project = Path(str(contract["project"]))
    actual = repository_inventory(project)
    expected = contract["executor"]["repository_inventory"]
    if actual != expected:
        raise PilotError("Repository context inventory changed after approval")
    if actual["file_count"] > int(contract["executor"]["max_repository_files"]):
        raise PilotError("Repository file count exceeds the frozen executor context gate")
    if actual["total_bytes"] > int(contract["executor"]["max_repository_bytes"]):
        raise PilotError("Repository bytes exceed the frozen executor context gate")


def clipped(value: Any, maximum: int = 6000) -> str:
    text = str(value).replace("\x00", "")
    return text if len(text) <= maximum else text[:maximum] + "...[truncated]"


def build_maker_task(contract: Mapping[str, Any], *, attempt: int, prior_failure: str | None) -> str:
    allowed = ", ".join(contract["scope"]["allowed_paths"])
    protected = ", ".join(contract["scope"]["protected_paths"]) or "none"
    lines = [
        "Implement the bounded candidate in the isolated worktree.",
        f"Goal: {contract['goal']}",
        "Non-goals: " + ("; ".join(contract["non_goals"]) or "none"),
        f"Allowed changed paths: {allowed}",
        f"Protected paths that must not change: {protected}",
        "Do not commit, merge, push, change remotes, read secrets, or use the network.",
        "Run only focused local checks. The parent owns final validation.",
        f"This is implementation attempt {attempt + 1} of {contract['limits']['max_repairs'] + 1}.",
    ]
    if prior_failure:
        lines.append("Prior candidate was rejected. Repair from the clean baseline using this evidence:")
        lines.append(clipped(prior_failure, 5000))
    return "\n".join(lines)


def build_review_task(
    contract: Mapping[str, Any],
    *,
    maker: Mapping[str, Any],
    candidate: Mapping[str, Any],
    verifier: Mapping[str, Any],
) -> str:
    diff_text = Path(str(candidate["diff_path"])).read_text(encoding="utf-8", errors="replace")
    return "\n".join(
        [
            "Perform an independent semantic review. You are not the implementer.",
            f"Contract goal: {contract['goal']}",
            "Non-goals: " + ("; ".join(contract["non_goals"]) or "none"),
            f"Contract hash: {candidate['contract_hash']}",
            f"Maker agent/model: {maker.get('agent')} / {maker.get('model')}",
            f"Changed paths: {', '.join(candidate['changed_paths'])}",
            f"Deterministic verifier: {json.dumps(verifier.get('result'), ensure_ascii=True, sort_keys=True)}",
            "Inspect the actual candidate worktree and the diff below. Reject scope drift, weakened validation, float rounding, test tampering, or missing requirements.",
            clipped(diff_text, 12000),
            "Return the normal Agent Fleet handoff. Include exactly one marker in summary or verification:",
            "AGENT_LOOP_VERDICT=PASS, AGENT_LOOP_VERDICT=REJECT, or AGENT_LOOP_VERDICT=ESCALATE.",
            "PASS is allowed only when every contract requirement and the deterministic evidence agree.",
        ]
    )


def parse_process_json(stdout: str, label: str) -> dict[str, Any]:
    if len(stdout.encode("utf-8", errors="replace")) > 20_000_000:
        raise PilotError(f"{label} output exceeds 20 MB")
    candidates = [stdout.strip()] + [line.strip() for line in reversed(stdout.splitlines()) if line.strip()]
    for candidate in candidates:
        try:
            value = json.loads(candidate, object_pairs_hook=_no_duplicate_keys)
        except (json.JSONDecodeError, PilotError):
            continue
        if isinstance(value, dict):
            return value
    raise PilotError(f"{label} returned no valid JSON object")


def parse_single_json_object(stdout: str, label: str) -> dict[str, Any]:
    if len(stdout.encode("utf-8", errors="replace")) > 2_000_000:
        raise PilotError(f"{label} output exceeds 2 MB")
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise PilotError(f"{label} must return exactly one non-empty JSON line")
    try:
        value = json.loads(lines[0], object_pairs_hook=_no_duplicate_keys)
    except (json.JSONDecodeError, PilotError) as exc:
        raise PilotError(f"{label} returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise PilotError(f"{label} JSON must be an object")
    return value


def fleet_invocation_environment(actor: Mapping[str, Any]) -> dict[str, str]:
    env = dict(os.environ)
    if str(actor.get("agent", "")).casefold() != "grok":
        return env
    home = Path.home().resolve()
    env["HOME"] = str(home)
    env["GROK_HOME"] = str(home / ".grok")
    env["GROK_MEMORY"] = "0"
    env["GROK_SUBAGENTS"] = "0"
    env["GROK_WORKFLOWS"] = "0"
    env["GROK_TELEMETRY_ENABLED"] = "false"
    env["GROK_TELEMETRY_TRACE_UPLOAD"] = "false"
    for vendor in ("CLAUDE", "CURSOR"):
        for surface in ("SKILLS", "RULES", "AGENTS", "MCPS", "HOOKS", "SESSIONS"):
            env[f"GROK_{vendor}_{surface}_ENABLED"] = "false"
    return env


def invoke_fleet(
    *,
    fleet_script: Path,
    actor: Mapping[str, Any],
    executor: Mapping[str, Any],
    task_path: Path,
    kind: str,
    complexity: str,
    mode: str,
    workspace: Path,
    output_root: Path,
    state_dir: Path | None = None,
) -> tuple[dict[str, Any], str, str]:
    task = require_plain_file(task_path, "Fleet task")
    task_bytes = task.stat().st_size
    if task_bytes > int(executor["max_task_bytes"]):
        raise PilotError(
            f"Fleet task exceeds the frozen context limit: "
            f"{task_bytes}>{executor['max_task_bytes']} bytes"
        )
    command = [
        sys.executable,
        str(fleet_script),
    ]
    if state_dir is not None:
        guarded_state = require_plain_directory(state_dir, "Agent Fleet state directory")
        command.extend(["--state-dir", str(guarded_state)])
    command.extend([
        "run",
        "--task-file",
        str(task_path),
        "--kind",
        kind,
        "--complexity",
        complexity,
        "--assessment-file",
        str(actor["assessment_file"]),
        "--agent",
        str(actor["agent"]),
        "--mode",
        mode,
        "--workspace",
        str(workspace),
        "--timeout",
        str(actor["timeout_seconds"]),
        "--disable-web",
        "--native-subagent-limit",
        "0",
        "--output-dir",
        str(output_root),
        "--runtime-profile",
        str(executor["runtime_profile"]),
        "--knowledge-mode",
        str(executor["knowledge_mode"]),
        "--max-prompt-bytes",
        str(executor["max_prompt_bytes"]),
        "--max-model-tool-calls",
        str(executor["max_model_tool_calls"]),
    ])
    if actor.get("model"):
        command.extend(["--model", str(actor["model"])])
    if actor.get("execution_profile"):
        command.extend(["--execution-profile", str(actor["execution_profile"])])
    if mode == "implement":
        command.append("--allow-write")
    result = run_process(
        command,
        cwd=workspace,
        timeout=int(actor["timeout_seconds"]) + 120,
        env=fleet_invocation_environment(actor),
    )
    parsed = parse_process_json(result.stdout, "Agent Fleet")
    if result.returncode != 0:
        raise PilotError(
            f"Agent Fleet process failed with {result.returncode}: {clipped(result.stderr, 2000)}"
        )
    expected_runtime = {
        "runtime_profile": executor["runtime_profile"],
        "knowledge_mode": executor["knowledge_mode"],
        "max_prompt_bytes": executor["max_prompt_bytes"],
        "max_model_tool_calls": executor["max_model_tool_calls"],
    }
    for key, expected in expected_runtime.items():
        if parsed.get(key) != expected:
            raise PilotError(
                f"Agent Fleet runtime evidence mismatch for {key}: "
                f"expected={expected!r}, actual={parsed.get(key)!r}"
            )
    prompt_bytes = parsed.get("prompt_bytes")
    if (
        not isinstance(prompt_bytes, int)
        or isinstance(prompt_bytes, bool)
        or prompt_bytes < 0
        or prompt_bytes > int(executor["max_prompt_bytes"])
    ):
        raise PilotError("Agent Fleet returned invalid prompt-size evidence")
    return parsed, result.stdout, result.stderr


def parse_status_paths(worktree: Path) -> list[str]:
    result = run_git(["status", "--porcelain=v1", "-z", "--untracked-files=all"], worktree)
    if result.returncode != 0:
        raise PilotError(f"Cannot inspect candidate status: {result.stderr.strip()[:1000]}")
    entries = result.stdout.split("\x00")
    paths: list[str] = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        if len(entry) < 4 or entry[2] != " ":
            raise PilotError("Malformed git porcelain entry")
        status = entry[:2]
        path = entry[3:].replace("\\", "/")
        paths.append(path)
        if "R" in status or "C" in status:
            if index >= len(entries) or not entries[index]:
                raise PilotError("Malformed rename/copy porcelain entry")
            paths.append(entries[index].replace("\\", "/"))
            index += 1
    return sorted(set(paths), key=str.casefold)


def path_matches(path: str, patterns: list[str]) -> bool:
    normalized = path.replace("\\", "/")
    return any(fnmatch.fnmatchcase(normalized, pattern) for pattern in patterns)


def inspect_candidate(
    result: Mapping[str, Any],
    contract: Mapping[str, Any],
    attempt_dir: Path,
    *,
    require_handoff: bool = True,
) -> dict[str, Any]:
    if require_handoff and (
        result.get("status") != "COMPLETED" or result.get("handoff_valid") is not True
    ):
        raise PilotError("Maker did not return a completed valid handoff")
    evidence = result.get("git_evidence")
    if not isinstance(evidence, dict) or not isinstance(evidence.get("worktree"), str):
        raise PilotError("Maker result has no isolated worktree evidence")
    worktree = require_plain_directory(Path(evidence["worktree"]), "candidate worktree")
    output_dir = require_plain_directory(Path(str(result.get("output_dir"))), "Fleet output directory")
    loop_root = attempt_dir.parent.parent.resolve()
    fleet_root = (loop_root / "fleet-runs").resolve()
    try:
        output_dir.relative_to(fleet_root)
    except ValueError as exc:
        raise PilotError("Fleet output directory escaped the loop state root") from exc
    if worktree != (output_dir / "worktree").resolve():
        raise PilotError("Candidate worktree is outside the Fleet run directory")
    head = git_output(["rev-parse", "HEAD"], worktree, "candidate HEAD")
    if head != contract["baseline_head"]:
        raise PilotError("Candidate worktree is not based on the frozen baseline")
    changed_paths = parse_status_paths(worktree)
    if not changed_paths:
        raise PilotError("Maker produced no candidate changes")
    if len(changed_paths) > contract["limits"]["max_changed_files"]:
        raise PilotError("Candidate changed too many files")
    for relative in changed_paths:
        if path_matches(relative, contract["scope"]["protected_paths"]):
            raise PilotError(f"Candidate changed a protected path: {relative}")
        if not path_matches(relative, contract["scope"]["allowed_paths"]):
            raise PilotError(f"Candidate changed a path outside scope: {relative}")
        candidate_path = (worktree / relative).resolve()
        try:
            candidate_path.relative_to(worktree)
        except ValueError as exc:
            raise PilotError(f"Candidate path escaped the worktree: {relative}") from exc
        if candidate_path.exists() and is_filesystem_link(candidate_path):
            raise PilotError(f"Candidate introduced a link or reparse point: {relative}")
    diff = run_git(["diff", "--binary", "--no-ext-diff", "HEAD", "--"], worktree, timeout=120)
    if diff.returncode != 0:
        raise PilotError(f"Cannot collect candidate diff: {diff.stderr.strip()[:1000]}")
    diff_bytes = diff.stdout.encode("utf-8")
    if len(diff_bytes) > contract["limits"]["max_diff_bytes"]:
        raise PilotError("Candidate diff exceeds the contract limit")
    diff_path = attempt_dir / "candidate.diff"
    diff_path.write_bytes(diff_bytes)
    untracked: list[dict[str, Any]] = []
    for relative in changed_paths:
        check = run_git(["ls-files", "--error-unmatch", "--", relative], worktree)
        path = worktree / relative
        if check.returncode != 0 and path.is_file():
            untracked.append(
                {"path": relative, "size": path.stat().st_size, "sha256": sha256_file(path)}
            )
    if len(diff_bytes) + sum(item["size"] for item in untracked) > contract["limits"]["max_diff_bytes"]:
        raise PilotError("Candidate tracked and untracked payload exceeds the contract limit")
    fingerprint_payload = {
        "diff_sha256": sha256_bytes(diff_bytes),
        "changed_paths": changed_paths,
        "untracked": untracked,
    }
    return {
        "worktree": str(worktree),
        "contract_hash": sha256_bytes(canonical_json_bytes(load_json(attempt_dir.parent.parent / "contract.json", "contract"))),
        "baseline_head": head,
        "changed_paths": changed_paths,
        "diff_path": str(diff_path),
        "diff_sha256": fingerprint_payload["diff_sha256"],
        "untracked": untracked,
        "candidate_fingerprint": sha256_bytes(canonical_json_bytes(fingerprint_payload)),
    }


def verifier_environment(worktree: Path) -> dict[str, str]:
    allowed = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env = {key: value for key, value in env.items() if not SECRET_ENV_PATTERN.search(key)}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["AGENT_LOOP_CANDIDATE"] = str(worktree)
    return env


def run_trusted_verifier(
    contract: Mapping[str, Any], candidate: Mapping[str, Any], attempt_dir: Path
) -> dict[str, Any]:
    worktree = Path(str(candidate["worktree"]))
    runner = Path(str(contract["verification"]["trusted_runner"]))
    result = run_process(
        [sys.executable, str(runner), str(worktree)],
        cwd=worktree,
        timeout=int(contract["verification"]["timeout_seconds"]),
        env=verifier_environment(worktree),
    )
    stdout_path = attempt_dir / "verifier.stdout.txt"
    stderr_path = attempt_dir / "verifier.stderr.txt"
    stdout_path.write_text(result.stdout, encoding="utf-8")
    stderr_path.write_text(result.stderr, encoding="utf-8")
    try:
        payload = parse_single_json_object(result.stdout, "trusted verifier")
    except PilotError as exc:
        payload = {"status": "FAIL", "checks": [], "error": str(exc)}
    passed = result.returncode == 0 and payload.get("status") == "PASS"
    record = {
        "status": "PASS" if passed else "FAIL",
        "exit_code": result.returncode,
        "runner_sha256": sha256_file(runner),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "stdout_sha256": sha256_file(stdout_path),
        "stderr_sha256": sha256_file(stderr_path),
        "result": payload,
    }
    atomic_json(attempt_dir / "verifier.json", record)
    return record


def extract_verdict(result: Mapping[str, Any]) -> str:
    if result.get("status") != "COMPLETED" or result.get("handoff_valid") is not True:
        return "ESCALATE"
    handoff = result.get("handoff")
    if not isinstance(handoff, dict):
        return "ESCALATE"
    matches = VERDICT_PATTERN.findall(json.dumps(handoff, ensure_ascii=True, sort_keys=True))
    unique = sorted(set(matches))
    return unique[0] if len(unique) == 1 else "ESCALATE"


def total_tokens(result: Mapping[str, Any]) -> int | None:
    usage = result.get("token_usage")
    if not isinstance(usage, dict):
        return None
    value = usage.get("total_tokens")
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


TOKEN_BUDGET_FIELDS = (
    ("total_tokens", "tokens_total", "max_total_tokens"),
    ("fresh_input_tokens", "fresh_input_tokens_total", "max_fresh_input_tokens"),
    ("cache_read_tokens", "cache_read_tokens_total", "max_cache_read_tokens"),
    ("cache_write_tokens", "cache_write_tokens_total", "max_cache_write_tokens"),
    ("output_tokens", "output_tokens_total", "max_output_tokens"),
    ("reasoning_tokens", "reasoning_tokens_total", "max_reasoning_tokens"),
)


def nonnegative_integer(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def token_usage_snapshot(result: Mapping[str, Any]) -> dict[str, Any]:
    usage = result.get("token_usage")
    if not isinstance(usage, dict):
        usage = {}
    values = {
        key: nonnegative_integer(usage.get(key))
        for key, _, _ in TOKEN_BUDGET_FIELDS
    }
    if values["fresh_input_tokens"] is None:
        input_tokens = nonnegative_integer(usage.get("input_tokens"))
        cache_read = values["cache_read_tokens"]
        if input_tokens is not None:
            if str(result.get("adapter", "")).casefold() == "codex" and cache_read is not None:
                values["fresh_input_tokens"] = max(0, input_tokens - cache_read)
            else:
                values["fresh_input_tokens"] = input_tokens
    cost = usage.get("cost_usd")
    values["cost_usd"] = (
        float(cost)
        if isinstance(cost, (int, float))
        and not isinstance(cost, bool)
        and float(cost) >= 0.0
        else None
    )
    return values


def update_budget(
    state: dict[str, Any], contract: Mapping[str, Any], result: Mapping[str, Any]
) -> list[str]:
    usage = token_usage_snapshot(result)
    limits = contract["limits"]
    token_policy = limits.get("token_policy", "hard")
    if token_policy not in ("hard", "estimate"):
        raise PilotError("limits.token_policy must be hard or estimate")
    warnings = state.setdefault("token_warnings", [])
    if not isinstance(warnings, list):
        raise PilotError("state.token_warnings must be an array")
    violations: list[str] = []
    for usage_key, state_key, limit_key in TOKEN_BUDGET_FIELDS:
        value = usage[usage_key]
        maximum = limits[limit_key]
        if value is None:
            state["token_telemetry_complete"] = False
            if maximum is not None:
                message = f"{usage_key} telemetry is unknown under a {token_policy} budget"
                if token_policy == "hard" and limits["unknown_token_policy"] == "stop":
                    violations.append(message)
                else:
                    warnings.append(message)
            continue
        state[state_key] = int(state.get(state_key, 0)) + value
        if maximum is not None and state[state_key] > maximum:
            message = f"{usage_key} budget exceeded: {state[state_key]}>{maximum}"
            if token_policy == "hard":
                violations.append(message)
            else:
                warnings.append(message)
    if usage["cost_usd"] is None:
        state["cost_telemetry_complete"] = False
    else:
        state["known_cost_usd_total"] = round(
            float(state.get("known_cost_usd_total", 0.0)) + usage["cost_usd"], 8
        )
    return violations


def evidence_manifest(loop_dir: Path, state: Mapping[str, Any], final_attempt: int) -> dict[str, Any]:
    evidence_root = loop_dir / "evidence"
    files: list[dict[str, Any]] = []
    for path in sorted(evidence_root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if path.is_file() and path.name != "manifest.json":
            files.append(
                {
                    "path": path.relative_to(evidence_root).as_posix(),
                    "size": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": utc_now(),
        "contract_hash": state["contract_hash"],
        "final_attempt": final_attempt,
        "candidate": state["candidate"],
        "files": files,
    }


def terminalize(loop_dir: Path, state: dict[str, Any], phase: str, reason: str) -> None:
    if phase not in {"CANDIDATE_READY", "ESCALATED", "STOPPED"}:
        raise PilotError(f"Invalid terminalization phase: {phase}")
    state["phase"] = phase
    state["finished_at"] = utc_now()
    state["reason"] = clipped(reason, 2000)
    save_state(loop_dir, state)
    append_event(
        loop_dir / "events.jsonl",
        {"event": phase, "at": state["finished_at"], "reason": state["reason"]},
    )


def run_loop(loop_dir: Path) -> dict[str, Any]:
    root, frozen, state = load_loop(loop_dir)
    contract = frozen["contract"]
    if state["phase"] != "READY":
        raise PilotError("Only a READY loop can run")
    with loop_lock(root):
        started = time.monotonic()
        state["started_at"] = utc_now()
        state["phase"] = "IMPLEMENTING"
        save_state(root, state)
        append_event(root / "events.jsonl", {"event": "STARTED", "at": state["started_at"]})
        prior_failure: str | None = None
        try:
            ensure_baseline_unchanged(contract)
            ensure_executor_context_unchanged(contract)
            for attempt in range(contract["limits"]["max_repairs"] + 1):
                if time.monotonic() - started > contract["limits"]["max_wall_seconds"]:
                    raise PilotError("Loop wall-time budget exceeded")
                kill_reason = kill_switch_active(root, contract)
                if kill_reason:
                    raise PilotError(kill_reason)
                state["phase"] = "IMPLEMENTING" if attempt == 0 else "REPAIRING"
                save_state(root, state)
                attempt_dir = root / "evidence" / f"attempt-{attempt + 1}"
                attempt_dir.mkdir()
                task_path = attempt_dir / "maker-task.txt"
                task_path.write_text(
                    build_maker_task(contract, attempt=attempt, prior_failure=prior_failure),
                    encoding="utf-8",
                )
                maker, maker_stdout, maker_stderr = invoke_fleet(
                    fleet_script=Path(frozen["fleet_runtime"]["fleet_script"]),
                    actor=contract["implementer"],
                    executor=contract["executor"],
                    task_path=task_path,
                    kind="implementation",
                    complexity="low",
                    mode="implement",
                    workspace=Path(contract["project"]),
                    output_root=root / "fleet-runs",
                )
                atomic_json(attempt_dir / "maker-result.json", maker)
                (attempt_dir / "maker.stdout.txt").write_text(maker_stdout, encoding="utf-8")
                (attempt_dir / "maker.stderr.txt").write_text(maker_stderr, encoding="utf-8")
                attempt_record = {
                    "attempt": attempt + 1,
                    "maker_run_id": maker.get("run_id"),
                    "maker_status": maker.get("status"),
                    "maker_handoff_valid": maker.get("handoff_valid"),
                    "maker_total_tokens": total_tokens(maker),
                    "maker_token_usage": token_usage_snapshot(maker),
                    "maker_prompt_bytes": maker.get("prompt_bytes"),
                    "maker_model_tool_calls": maker.get("model_tool_calls"),
                    "maker_tool_budget_exceeded": maker.get("tool_budget_exceeded"),
                    "outcome": "MAKER_RETURNED",
                }
                state["attempts"].append(attempt_record)
                warning_start = len(state.setdefault("token_warnings", []))
                budget_violations = update_budget(state, contract, maker)
                if maker.get("tool_budget_exceeded") is True:
                    budget_violations.append(
                        "model tool-call budget exceeded: "
                        f"{maker.get('model_tool_calls')}>{contract['executor']['max_model_tool_calls']}"
                    )
                attempt_record["budget_violations"] = sorted(set(budget_violations))
                attempt_record["budget_warnings"] = state["token_warnings"][warning_start:]
                save_state(root, state)
                state["phase"] = "VERIFYING"
                save_state(root, state)
                if budget_violations:
                    diagnostic: dict[str, Any] = {"candidate": "UNAVAILABLE", "verifier": "NOT_RUN"}
                    try:
                        candidate = inspect_candidate(
                            maker, contract, attempt_dir, require_handoff=False
                        )
                        verifier = run_trusted_verifier(contract, candidate, attempt_dir)
                        diagnostic = {
                            "candidate": "PRESENT",
                            "candidate_fingerprint": candidate["candidate_fingerprint"],
                            "verifier": verifier["status"],
                        }
                    except PilotError as exc:
                        diagnostic["reason"] = str(exc)
                    attempt_record.update(
                        {
                            "outcome": "BUDGET_STOP",
                            "diagnostic_after_stop": diagnostic,
                        }
                    )
                    save_state(root, state)
                    terminalize(
                        root,
                        state,
                        "STOPPED",
                        "Hard execution budget stopped the loop: "
                        + "; ".join(sorted(set(budget_violations))),
                    )
                    return command_status_value(root, state)
                try:
                    candidate = inspect_candidate(maker, contract, attempt_dir)
                except PilotError as exc:
                    prior_failure = f"Maker candidate was invalid: {exc}"
                    attempt_record.update(
                        {"outcome": "INVALID", "reason": str(exc)}
                    )
                    save_state(root, state)
                    if attempt < contract["limits"]["max_repairs"]:
                        continue
                    terminalize(root, state, "ESCALATED", prior_failure)
                    return command_status_value(root, state)
                verifier = run_trusted_verifier(contract, candidate, attempt_dir)
                if verifier["status"] != "PASS":
                    prior_failure = "Deterministic verifier rejected the candidate: " + clipped(
                        verifier.get("result"), 4000
                    )
                    attempt_record.update(
                        {"outcome": "VERIFIER_REJECT", "reason": prior_failure}
                    )
                    save_state(root, state)
                    if attempt < contract["limits"]["max_repairs"]:
                        continue
                    terminalize(root, state, "ESCALATED", prior_failure)
                    return command_status_value(root, state)

                state["phase"] = "REVIEWING"
                save_state(root, state)
                review_task = attempt_dir / "review-task.txt"
                review_task.write_text(
                    build_review_task(
                        contract, maker=maker, candidate=candidate, verifier=verifier
                    ),
                    encoding="utf-8",
                )
                reviewer, reviewer_stdout, reviewer_stderr = invoke_fleet(
                    fleet_script=Path(frozen["fleet_runtime"]["fleet_script"]),
                    actor=contract["validator"],
                    executor=contract["executor"],
                    task_path=review_task,
                    kind="review",
                    complexity="medium",
                    mode="review",
                    workspace=Path(candidate["worktree"]),
                    output_root=root / "fleet-runs",
                )
                atomic_json(attempt_dir / "reviewer-result.json", reviewer)
                (attempt_dir / "reviewer.stdout.txt").write_text(reviewer_stdout, encoding="utf-8")
                (attempt_dir / "reviewer.stderr.txt").write_text(reviewer_stderr, encoding="utf-8")
                warning_start = len(state.setdefault("token_warnings", []))
                reviewer_budget_violations = update_budget(state, contract, reviewer)
                if reviewer.get("tool_budget_exceeded") is True:
                    reviewer_budget_violations.append(
                        "reviewer model tool-call budget exceeded: "
                        f"{reviewer.get('model_tool_calls')}>{contract['executor']['max_model_tool_calls']}"
                    )
                attempt_record.update(
                    {
                        "reviewer_total_tokens": total_tokens(reviewer),
                        "reviewer_token_usage": token_usage_snapshot(reviewer),
                        "reviewer_prompt_bytes": reviewer.get("prompt_bytes"),
                        "reviewer_model_tool_calls": reviewer.get("model_tool_calls"),
                        "reviewer_tool_budget_exceeded": reviewer.get("tool_budget_exceeded"),
                        "reviewer_budget_violations": sorted(set(reviewer_budget_violations)),
                        "reviewer_budget_warnings": state["token_warnings"][warning_start:],
                    }
                )
                save_state(root, state)
                if reviewer_budget_violations:
                    attempt_record["outcome"] = "REVIEWER_BUDGET_STOP"
                    save_state(root, state)
                    terminalize(
                        root,
                        state,
                        "STOPPED",
                        "Hard reviewer budget stopped the loop: "
                        + "; ".join(sorted(set(reviewer_budget_violations))),
                    )
                    return command_status_value(root, state)
                if reviewer.get("run_id") == maker.get("run_id"):
                    raise PilotError("Maker and reviewer returned the same Fleet run ID")
                if str(reviewer.get("agent", "")).casefold() != str(contract["validator"]["agent"]).casefold():
                    raise PilotError("Reviewer identity differs from the frozen contract")
                verdict = extract_verdict(reviewer)
                attempt_record.update(
                    {
                        "reviewer_run_id": reviewer.get("run_id"),
                        "verifier": verifier["status"],
                        "reviewer_verdict": verdict,
                        "candidate_fingerprint": candidate["candidate_fingerprint"],
                        "outcome": "REVIEWED",
                    }
                )
                state["candidate"] = candidate
                save_state(root, state)
                if verdict == "PASS":
                    manifest = evidence_manifest(root, state, attempt + 1)
                    manifest_path = root / "evidence" / "manifest.json"
                    atomic_json(manifest_path, manifest)
                    state["manifest_path"] = str(manifest_path)
                    terminalize(root, state, "CANDIDATE_READY", "Deterministic and semantic gates passed")
                    return command_status_value(root, state)
                prior_failure = "Semantic reviewer verdict: " + verdict + ". " + clipped(
                    reviewer.get("handoff"), 4000
                )
                if verdict == "REJECT" and attempt < contract["limits"]["max_repairs"]:
                    continue
                terminalize(root, state, "ESCALATED", prior_failure)
                return command_status_value(root, state)
            raise PilotError("Loop exhausted attempts without a terminal outcome")
        except Exception as exc:
            terminalize(root, state, "STOPPED", f"{type(exc).__name__}: {exc}")
            raise
        finally:
            try:
                ensure_baseline_unchanged(contract)
                ensure_executor_context_unchanged(contract)
            except Exception as baseline_exc:
                terminalize(
                    root,
                    state,
                    "STOPPED",
                    f"Baseline integrity failure: {type(baseline_exc).__name__}: {baseline_exc}",
                )
                raise


def candidate_fingerprint(candidate: Mapping[str, Any]) -> str:
    worktree = require_plain_directory(Path(str(candidate["worktree"])), "candidate worktree")
    changed_paths = parse_status_paths(worktree)
    diff = run_git(["diff", "--binary", "--no-ext-diff", "HEAD", "--"], worktree, timeout=120)
    if diff.returncode != 0:
        raise PilotError("Cannot re-read candidate diff")
    untracked: list[dict[str, Any]] = []
    for relative in changed_paths:
        check = run_git(["ls-files", "--error-unmatch", "--", relative], worktree)
        path = worktree / relative
        if check.returncode != 0 and path.is_file():
            untracked.append({"path": relative, "size": path.stat().st_size, "sha256": sha256_file(path)})
    return sha256_bytes(
        canonical_json_bytes(
            {
                "diff_sha256": sha256_bytes(diff.stdout.encode("utf-8")),
                "changed_paths": changed_paths,
                "untracked": untracked,
            }
        )
    )


def verify_manifest(loop_dir: Path, state: Mapping[str, Any]) -> None:
    path = require_plain_file(Path(str(state["manifest_path"])), "evidence manifest")
    expected_manifest_path = (loop_dir / "evidence" / "manifest.json").resolve()
    if path != expected_manifest_path:
        raise PilotError("Evidence manifest is outside its canonical location")
    manifest = load_json(path, "evidence manifest")
    if manifest.get("contract_hash") != state["contract_hash"]:
        raise PilotError("Evidence manifest contract hash mismatch")
    rows = manifest.get("files", [])
    if not isinstance(rows, list):
        raise PilotError("Evidence manifest files must be a list")
    listed_paths: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise PilotError("Malformed evidence manifest row")
        relative = str(row.get("path", ""))
        if relative in listed_paths:
            raise PilotError(f"Duplicate evidence manifest path: {relative}")
        listed_paths.add(relative)
        candidate = (loop_dir / "evidence" / relative).resolve()
        try:
            candidate.relative_to((loop_dir / "evidence").resolve())
        except ValueError as exc:
            raise PilotError("Evidence manifest path escape") from exc
        if not candidate.is_file() or sha256_file(candidate) != row.get("sha256"):
            raise PilotError(f"Evidence file drift detected: {row.get('path')}")
    actual_paths = {
        item.relative_to(loop_dir / "evidence").as_posix()
        for item in (loop_dir / "evidence").rglob("*")
        if item.is_file() and item.resolve() != expected_manifest_path
    }
    if actual_paths != listed_paths:
        raise PilotError("Evidence manifest does not exactly cover the evidence directory")
    candidate = state.get("candidate")
    if not isinstance(candidate, dict):
        raise PilotError("State has no candidate")
    worktree = require_plain_directory(Path(str(candidate.get("worktree", ""))), "candidate worktree")
    try:
        worktree.relative_to((loop_dir / "fleet-runs").resolve())
    except ValueError as exc:
        raise PilotError("Candidate worktree escaped the loop Fleet root") from exc
    if candidate_fingerprint(candidate) != candidate.get("candidate_fingerprint"):
        raise PilotError("Candidate drift detected after review")


def accept_loop(loop_dir: Path, accepted_by: str, note: str) -> dict[str, Any]:
    root, _, state = load_loop(loop_dir)
    if state["phase"] != "CANDIDATE_READY":
        raise PilotError("Only CANDIDATE_READY can be accepted")
    if not accepted_by.strip() or not note.strip():
        raise PilotError("accepted_by and note must be non-empty")
    verify_manifest(root, state)
    state["phase"] = "ACCEPTED"
    state["finished_at"] = utc_now()
    state["reason"] = clipped(note, 2000)
    save_state(root, state)
    append_event(
        root / "events.jsonl",
        {"event": "ACCEPTED", "at": state["finished_at"], "by": accepted_by.strip(), "note": state["reason"]},
    )
    return command_status_value(root, state)


def stop_loop(loop_dir: Path, reason: str) -> dict[str, Any]:
    root, _, state = load_loop(loop_dir)
    if state["phase"] == "ACCEPTED":
        raise PilotError("An accepted loop cannot be stopped")
    terminalize(root, state, "STOPPED", reason)
    return command_status_value(root, state)


def recover_loop(loop_dir: Path, reason: str) -> dict[str, Any]:
    root = require_plain_directory(loop_dir, "loop directory")
    lock = root / ".lock"
    if lock.exists():
        lock_data = load_json(lock, "loop lock")
        lock_host = str(lock_data.get("host", "")).casefold()
        current_host = socket.gethostname().casefold()
        try:
            lock_pid = int(lock_data["pid"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PilotError("Loop lock has no valid PID; refusing automatic recovery") from exc
        if lock_host != current_host:
            raise PilotError("Loop lock belongs to another host; refusing automatic recovery")
        if process_is_alive(lock_pid):
            raise PilotError("Loop lock owner is still active; refusing recovery")
        lock.unlink()
    root, _, state = load_loop(root)
    if state["phase"] not in ACTIVE_PHASES:
        raise PilotError("Recovery is allowed only from an active interrupted phase")
    terminalize(root, state, "ESCALATED", "Recovered interrupted run: " + reason)
    return command_status_value(root, state)


def cleanup_loop(loop_dir: Path, confirm: bool) -> dict[str, Any]:
    if not confirm:
        raise PilotError("Cleanup requires --confirm")
    root, frozen, state = load_loop(loop_dir)
    if state["phase"] not in TERMINAL_PHASES:
        raise PilotError("Cleanup requires ACCEPTED, ESCALATED, or STOPPED")
    project = Path(frozen["contract"]["project"])
    removed: list[str] = []
    candidates: list[str] = []
    for attempt in state.get("attempts", []):
        if not isinstance(attempt, dict):
            continue
    for result_path in (root / "evidence").glob("attempt-*/maker-result.json"):
        result = load_json(result_path, "maker result")
        evidence = result.get("git_evidence")
        if isinstance(evidence, dict) and isinstance(evidence.get("worktree"), str):
            candidates.append(evidence["worktree"])
    for value in sorted(set(candidates), key=str.casefold):
        worktree = Path(value).resolve()
        run_root = (root / "fleet-runs").resolve()
        try:
            worktree.relative_to(run_root)
        except ValueError as exc:
            raise PilotError(f"Refusing cleanup outside Fleet run root: {worktree}") from exc
        result = run_git(["worktree", "remove", "--force", str(worktree)], project, timeout=120)
        if result.returncode != 0 and worktree.exists():
            raise PilotError(f"Cannot remove worktree {worktree}: {result.stderr.strip()[:1000]}")
        removed.append(str(worktree))
    run_git(["worktree", "prune"], project, timeout=60)
    state["cleanup"] = {"at": utc_now(), "removed_worktrees": removed}
    save_state(root, state)
    append_event(root / "events.jsonl", {"event": "CLEANED", **state["cleanup"]})
    return {"action": "cleanup", "status": state["phase"], "removed_worktrees": removed, "writes_performed": len(removed) + 2}


def command_status_value(loop_dir: Path, state: Mapping[str, Any]) -> dict[str, Any]:
    candidate = state.get("candidate") if isinstance(state.get("candidate"), dict) else None
    return {
        "action": "status",
        "status": state["phase"],
        "loop_dir": str(loop_dir),
        "contract_hash": state["contract_hash"],
        "attempt_count": len(state.get("attempts", [])),
        "tokens_total": state.get("tokens_total"),
        "fresh_input_tokens_total": state.get("fresh_input_tokens_total"),
        "cache_read_tokens_total": state.get("cache_read_tokens_total"),
        "cache_write_tokens_total": state.get("cache_write_tokens_total"),
        "output_tokens_total": state.get("output_tokens_total"),
        "reasoning_tokens_total": state.get("reasoning_tokens_total"),
        "known_cost_usd_total": state.get("known_cost_usd_total"),
        "token_telemetry_complete": state.get("token_telemetry_complete"),
        "cost_telemetry_complete": state.get("cost_telemetry_complete"),
        "token_warnings": list(state.get("token_warnings", [])),
        "candidate_worktree": candidate.get("worktree") if candidate else None,
        "candidate_fingerprint": candidate.get("candidate_fingerprint") if candidate else None,
        "manifest_path": state.get("manifest_path"),
        "reason": state.get("reason"),
        "writes_performed": 0,
    }


def status_loop(loop_dir: Path) -> dict[str, Any]:
    root, _, state = load_loop(loop_dir)
    return command_status_value(root, state)


def doctor(fleet_script: Path) -> dict[str, Any]:
    identity = runtime_identity(fleet_script)
    result = run_process(
        [sys.executable, str(fleet_script), "doctor"],
        cwd=fleet_script.parent,
        timeout=90,
    )
    parsed = parse_process_json(result.stdout, "Agent Fleet doctor")
    if result.returncode != 0:
        raise PilotError("Agent Fleet doctor failed")
    return {
        "action": "doctor",
        "status": "READY" if parsed.get("routing_ready_count", 0) >= 2 else "NOT_READY",
        "agent_loop_version": VERSION,
        "fleet_script_sha256": identity["fleet_script_sha256"],
        "routing_ready_count": parsed.get("routing_ready_count"),
        "writes_performed": 0,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Explicit, bounded Agent Loop L2-thin controller")
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="action", required=True)

    doctor_parser = sub.add_parser("doctor")
    doctor_parser.add_argument("--fleet-script", type=Path, required=True)

    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--contract", type=Path, required=True)
    prepare_parser.add_argument("--state-root", type=Path, required=True)
    prepare_parser.add_argument("--fleet-script", type=Path, required=True)

    approve_parser = sub.add_parser("approve")
    approve_parser.add_argument("--loop-dir", type=Path, required=True)
    approve_parser.add_argument("--contract-hash", required=True)
    approve_parser.add_argument("--approved-by", required=True)

    for name in ("run", "status"):
        item = sub.add_parser(name)
        item.add_argument("--loop-dir", type=Path, required=True)

    accept_parser = sub.add_parser("accept")
    accept_parser.add_argument("--loop-dir", type=Path, required=True)
    accept_parser.add_argument("--accepted-by", required=True)
    accept_parser.add_argument("--note", required=True)

    stop_parser = sub.add_parser("stop")
    stop_parser.add_argument("--loop-dir", type=Path, required=True)
    stop_parser.add_argument("--reason", required=True)

    recover_parser = sub.add_parser("recover")
    recover_parser.add_argument("--loop-dir", type=Path, required=True)
    recover_parser.add_argument("--reason", required=True)

    cleanup_parser = sub.add_parser("cleanup")
    cleanup_parser.add_argument("--loop-dir", type=Path, required=True)
    cleanup_parser.add_argument("--confirm", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.action == "doctor":
            result = doctor(args.fleet_script)
        elif args.action == "prepare":
            result = prepare_loop(args.contract, args.state_root, args.fleet_script)
        elif args.action == "approve":
            result = approve_loop(args.loop_dir, args.contract_hash, args.approved_by)
        elif args.action == "run":
            result = run_loop(args.loop_dir)
        elif args.action == "status":
            result = status_loop(args.loop_dir)
        elif args.action == "accept":
            result = accept_loop(args.loop_dir, args.accepted_by, args.note)
        elif args.action == "stop":
            result = stop_loop(args.loop_dir, args.reason)
        elif args.action == "recover":
            result = recover_loop(args.loop_dir, args.reason)
        elif args.action == "cleanup":
            result = cleanup_loop(args.loop_dir, args.confirm)
        else:  # pragma: no cover
            raise PilotError(f"Unsupported action: {args.action}")
        print(json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2))
        return 0
    except PilotError as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, ensure_ascii=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
