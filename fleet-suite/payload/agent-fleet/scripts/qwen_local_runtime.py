from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

import adaptive

class LocalRuntimeError(RuntimeError):
    pass


STATE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{2,63}$")
DISTRO_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
MODEL_NAME_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{1,63}(/[A-Za-z0-9][A-Za-z0-9._-]{1,63})?(:[A-Za-z0-9._-]{1,32})?$"
)
CUDA_VERSION_PATTERN = re.compile(r"^[0-9]{1,2}\.[0-9]{1,2}$")
SYSTEMD_UNIT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{2,80}\.service$")
LEASE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{7,127}$")
LOCAL_HOSTS = {"127.0.0.1", "localhost"}
DEFAULT_STATE_ROOT = Path.home() / ".agents" / "local-models"
MAINTENANCE_MIN_TTL_SECONDS = 60
MAINTENANCE_MAX_TTL_SECONDS = 7200


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise LocalRuntimeError(f"{label} must be a positive integer")
    return value


def _bounded_float(value: Any, label: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LocalRuntimeError(f"{label} must be numeric")
    parsed = float(value)
    if not minimum <= parsed <= maximum:
        raise LocalRuntimeError(f"{label} must be between {minimum} and {maximum}")
    return parsed


def validate_config(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("kind") != "vllm-wsl":
        raise LocalRuntimeError("local_runtime.kind must be vllm-wsl")
    config = dict(raw)
    state_id = str(config.get("state_id", "")).strip()
    distro = str(config.get("distribution", "")).strip()
    served_model = str(config.get("served_model_name", "")).strip()
    if not STATE_ID_PATTERN.fullmatch(state_id):
        raise LocalRuntimeError("local_runtime.state_id is invalid")
    if not DISTRO_PATTERN.fullmatch(distro):
        raise LocalRuntimeError("local_runtime.distribution is invalid")
    if not MODEL_NAME_PATTERN.fullmatch(served_model):
        raise LocalRuntimeError("local_runtime.served_model_name is invalid")

    base_url = str(config.get("base_url", "")).rstrip("/")
    parsed = urllib.parse.urlparse(base_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in LOCAL_HOSTS
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
        or parsed.path != "/v1"
    ):
        raise LocalRuntimeError("local_runtime.base_url must be loopback http /v1")
    if parsed.port is None or not 1024 <= parsed.port <= 65535:
        raise LocalRuntimeError(
            "local_runtime.base_url must include an unprivileged port"
        )

    model_windows = Path(str(config.get("model_path_windows", ""))).resolve()
    model_wsl = str(config.get("model_path_wsl", "")).strip()
    vllm_wsl = str(config.get("vllm_executable_wsl", "")).strip()
    cuda_home_wsl = str(config.get("cuda_home_wsl", "")).strip()
    cuda_version = str(config.get("cuda_toolkit_version", "")).strip()
    systemd_unit = str(config.get("systemd_unit", "")).strip()
    if not model_windows.is_dir() or not (model_windows / "config.json").is_file():
        raise LocalRuntimeError(
            f"Local model directory is unavailable: {model_windows}"
        )
    if not all(value.startswith("/") for value in (model_wsl, vllm_wsl, cuda_home_wsl)):
        raise LocalRuntimeError("WSL model, vLLM, and CUDA paths must be absolute")
    if not CUDA_VERSION_PATTERN.fullmatch(cuda_version):
        raise LocalRuntimeError("local_runtime.cuda_toolkit_version is invalid")
    if not SYSTEMD_UNIT_PATTERN.fullmatch(systemd_unit):
        raise LocalRuntimeError("local_runtime.systemd_unit is invalid")

    native_context = _positive_int(
        config.get("native_context_window_tokens"),
        "local_runtime.native_context_window_tokens",
    )
    context_window = _positive_int(
        config.get("context_window_tokens"),
        "local_runtime.context_window_tokens",
    )
    yarn_factor = _bounded_float(
        config.get("yarn_factor"), "local_runtime.yarn_factor", 1.0, 8.0
    )
    if context_window > int(native_context * yarn_factor):
        raise LocalRuntimeError(
            "YaRN factor does not cover the configured context window"
        )
    max_num_seqs = _positive_int(
        config.get("max_num_seqs"), "local_runtime.max_num_seqs"
    )
    if max_num_seqs not in {1, 2}:
        raise LocalRuntimeError(
            "Local Qwen safety contract supports max_num_seqs=1 or 2"
        )
    _positive_int(
        config.get("max_num_batched_tokens"),
        "local_runtime.max_num_batched_tokens",
    )
    _positive_int(
        config.get("startup_timeout_seconds"),
        "local_runtime.startup_timeout_seconds",
    )
    _bounded_float(
        config.get("gpu_memory_utilization"),
        "local_runtime.gpu_memory_utilization",
        0.5,
        0.99,
    )
    if config.get("kv_cache_dtype") != "fp8":
        raise LocalRuntimeError("Local Qwen 500k profile requires an fp8 KV cache")
    if config.get("provider_cost_pln") != 0:
        raise LocalRuntimeError("Local Qwen provider cost must be exactly 0 PLN")
    if config.get("provider_cost_display") != "0 zł":
        raise LocalRuntimeError("Local Qwen provider cost display must be '0 zł'")

    config["base_url"] = base_url
    config["model_path_windows"] = str(model_windows)
    config["port"] = int(parsed.port)
    return config


def sandbox_base_url(raw: dict[str, Any]) -> str:
    config = validate_config(raw)
    return f"http://host.docker.internal:{config['port']}/v1"


def _run_wsl(
    config: dict[str, Any], args: Iterable[str], *, timeout: int = 20
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["wsl.exe", "-d", config["distribution"], "--", *list(args)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def endpoint_status(config: dict[str, Any], *, timeout: float = 2.0) -> dict[str, Any]:
    url = config["base_url"] + "/models"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read(2_000_000).decode("utf-8"))
    except (
        OSError,
        TimeoutError,
        urllib.error.URLError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        return {
            "status": "STOPPED",
            "endpoint": url,
            "expected_model": config["served_model_name"],
            "observed_models": [],
            "error": f"{type(exc).__name__}: {exc}",
        }
    rows = payload.get("data") if isinstance(payload, dict) else None
    observed = sorted(
        {
            str(row.get("id"))
            for row in rows or []
            if isinstance(row, dict) and isinstance(row.get("id"), str)
        }
    )
    expected = config["served_model_name"]
    return {
        "status": "READY" if expected in observed else "MODEL_MISMATCH",
        "endpoint": url,
        "expected_model": expected,
        "observed_models": observed,
        "error": None
        if expected in observed
        else "Loopback endpoint serves another model",
    }


def prerequisite_status(config: dict[str, Any]) -> dict[str, Any]:
    runtime_bin = config["vllm_executable_wsl"].rsplit("/", 1)[0]
    checks: dict[str, bool] = {
        "wsl_available": bool(shutil_which("wsl.exe")),
        "windows_model_config": (
            Path(config["model_path_windows"]) / "config.json"
        ).is_file(),
    }
    errors: list[str] = []
    if checks["wsl_available"]:
        for name, args in (
            ("wsl_vllm_executable", ["test", "-x", config["vllm_executable_wsl"]]),
            (
                "wsl_model_config",
                ["test", "-f", config["model_path_wsl"] + "/config.json"],
            ),
            (
                "wsl_nvcc_executable",
                ["test", "-x", config["cuda_home_wsl"] + "/bin/nvcc"],
            ),
            (
                "wsl_ninja_executable",
                ["test", "-x", runtime_bin + "/ninja"],
            ),
            (
                "wsl_cudart_link",
                ["test", "-e", config["cuda_home_wsl"] + "/lib64/libcudart.so"],
            ),
            (
                "wsl_cuda_driver_link",
                ["test", "-e", config["cuda_home_wsl"] + "/lib64/stubs/libcuda.so"],
            ),
            ("wsl_systemd_run", ["test", "-x", "/usr/bin/systemd-run"]),
        ):
            try:
                completed = _run_wsl(config, args, timeout=15)
                checks[name] = completed.returncode == 0
                if completed.returncode != 0:
                    errors.append(name + ": " + completed.stderr.strip()[:500])
            except (OSError, subprocess.TimeoutExpired) as exc:
                checks[name] = False
                errors.append(name + f": {type(exc).__name__}: {exc}")
        try:
            completed = _run_wsl(
                config, ["systemctl", "is-system-running", "--quiet"], timeout=15
            )
            checks["wsl_systemd_running"] = completed.returncode == 0
            if completed.returncode != 0:
                errors.append("wsl_systemd_running: systemd is not running")
        except (OSError, subprocess.TimeoutExpired) as exc:
            checks["wsl_systemd_running"] = False
            errors.append(f"wsl_systemd_running: {type(exc).__name__}: {exc}")
        nvcc = config["cuda_home_wsl"] + "/bin/nvcc"
        expected_cuda = config["cuda_toolkit_version"]
        try:
            completed = _run_wsl(config, [nvcc, "--version"], timeout=15)
            match = re.search(r"release\s+([0-9]+\.[0-9]+)", completed.stdout)
            observed_cuda = match.group(1) if match else None
            checks["wsl_nvcc_version_match"] = (
                completed.returncode == 0 and observed_cuda == expected_cuda
            )
            if not checks["wsl_nvcc_version_match"]:
                errors.append(
                    f"wsl_nvcc_version_match: expected {expected_cuda}, observed {observed_cuda}"
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            checks["wsl_nvcc_version_match"] = False
            errors.append(f"wsl_nvcc_version_match: {type(exc).__name__}: {exc}")

        major, minor = (int(part) for part in expected_cuda.split(".", 1))
        expected_header = str(major * 1000 + minor * 10)
        try:
            completed = _run_wsl(
                config,
                [
                    "grep",
                    "-Eq",
                    rf"^#define CUDA_VERSION {expected_header}$",
                    config["cuda_home_wsl"] + "/include/cuda.h",
                ],
                timeout=15,
            )
            checks["wsl_cuda_header_version_match"] = completed.returncode == 0
            if completed.returncode != 0:
                errors.append(
                    f"wsl_cuda_header_version_match: expected CUDA_VERSION {expected_header}"
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            checks["wsl_cuda_header_version_match"] = False
            errors.append(f"wsl_cuda_header_version_match: {type(exc).__name__}: {exc}")
    return {
        "ready_to_start": all(checks.values()),
        "checks": checks,
        "errors": errors,
    }


def shutil_which(name: str) -> str | None:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / name
        if candidate.is_file():
            return str(candidate)
    return None


def runtime_status(
    raw: dict[str, Any], *, state_root: Path = DEFAULT_STATE_ROOT
) -> dict[str, Any]:
    config = validate_config(raw)
    endpoint = endpoint_status(config)
    prerequisites = prerequisite_status(config)
    maintenance = maintenance_status(config, state_root=state_root)
    routing_ready = (
        not maintenance["active"]
        and (
            endpoint["status"] == "READY"
            or prerequisites["ready_to_start"]
        )
    )
    return {
        "kind": config["kind"],
        "state_id": config["state_id"],
        "status": "MAINTENANCE" if maintenance["active"] else endpoint["status"],
        "endpoint": endpoint,
        "prerequisites": prerequisites,
        "maintenance": maintenance,
        "routing_ready": routing_ready,
        "context_window_tokens": config["context_window_tokens"],
        "max_num_seqs": config["max_num_seqs"],
        "provider_cost_pln": config["provider_cost_pln"],
        "provider_cost_display": config["provider_cost_display"],
    }


def _rope_override(config: dict[str, Any]) -> str:
    payload = json.dumps(
        {
            "text_config": {
                "rope_parameters": {
                    "mrope_interleaved": True,
                    "mrope_section": [11, 11, 10],
                    "rope_type": "yarn",
                    "rope_theta": 10_000_000,
                    "partial_rotary_factor": 0.25,
                    "factor": config["yarn_factor"],
                    "original_max_position_embeddings": config[
                        "native_context_window_tokens"
                    ],
                }
            }
        },
        separators=(",", ":"),
    )
    return payload


def linux_serve_args(config: dict[str, Any]) -> list[str]:
    runtime_bin = config["vllm_executable_wsl"].rsplit("/", 1)[0]
    return [
        "env",
        f"PATH={runtime_bin}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "CUDA_DEVICE_ORDER=PCI_BUS_ID",
        f"CUDA_VISIBLE_DEVICES={config['gpu_index']}",
        "VLLM_ALLOW_LONG_MAX_MODEL_LEN=1",
        "VLLM_USE_FLASHINFER_SAMPLER=0",
        f"CUDA_HOME={config['cuda_home_wsl']}",
        config["vllm_executable_wsl"],
        "serve",
        config["model_path_wsl"],
        "--served-model-name",
        config["served_model_name"],
        "--host",
        "127.0.0.1",
        "--port",
        str(config["port"]),
        "--language-model-only",
        "--max-model-len",
        str(config["context_window_tokens"]),
        "--gpu-memory-utilization",
        str(config["gpu_memory_utilization"]),
        "--kv-cache-dtype",
        config["kv_cache_dtype"],
        "--attention-backend",
        "FLASHINFER",
        "--max-num-seqs",
        str(config["max_num_seqs"]),
        "--max-num-batched-tokens",
        str(config["max_num_batched_tokens"]),
        "--enable-chunked-prefill",
        "--reasoning-parser",
        "qwen3",
        "--enable-auto-tool-choice",
        "--tool-call-parser",
        "qwen3_coder",
        "--hf-overrides",
        _rope_override(config),
    ]


def _windows_to_wsl_path(path: Path) -> str:
    value = str(path.resolve())
    if len(value) < 4 or value[1:3] != ":\\":
        raise LocalRuntimeError(f"Cannot map Windows path into WSL: {value}")
    return f"/mnt/{value[0].lower()}/{value[3:].replace(chr(92), '/')}"


def serve_command(config: dict[str, Any], *, log_path: Path | None = None) -> list[str]:
    # wsl.exe joins the Linux argv before execution. A single bash -lc payload
    # with POSIX quoting preserves paths with spaces and JSON arguments.
    linux_command = shlex.join(linux_serve_args(config))
    if log_path is not None:
        shell_command = (
            f"exec {linux_command} >> "
            f"{shlex.quote(_windows_to_wsl_path(log_path))} 2>&1"
        )
        return [
            "wsl.exe",
            "-d",
            config["distribution"],
            "--",
            "systemd-run",
            f"--unit={config['systemd_unit']}",
            "--collect",
            "--property=Restart=on-failure",
            "--property=RestartSec=5s",
            "--no-block",
            "--quiet",
            "--",
            "/usr/bin/bash",
            "-lc",
            shell_command,
        ]
    else:
        shell_command = "exec " + linux_command
    return [
        "wsl.exe",
        "-d",
        config["distribution"],
        "--",
        "bash",
        "-lc",
        shell_command,
    ]


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    adaptive.atomic_write(path, payload)


def _maintenance_path(config: dict[str, Any], state_root: Path) -> Path:
    return state_root / config["state_id"] / "maintenance.json"


def _read_maintenance(
    config: dict[str, Any], state_root: Path
) -> dict[str, Any] | None:
    path = _maintenance_path(config, state_root)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LocalRuntimeError(
            f"Local Qwen maintenance lease is unreadable: {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise LocalRuntimeError("Local Qwen maintenance lease schema is invalid")
    if payload.get("state_id") != config["state_id"]:
        raise LocalRuntimeError("Local Qwen maintenance lease targets another runtime")
    lease_id = str(payload.get("lease_id", ""))
    if not LEASE_ID_PATTERN.fullmatch(lease_id):
        raise LocalRuntimeError("Local Qwen maintenance lease id is invalid")
    for field in ("acquired_at_unix", "expires_at_unix"):
        value = payload.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise LocalRuntimeError(
                f"Local Qwen maintenance lease {field} is invalid"
            )
    if float(payload["expires_at_unix"]) <= float(payload["acquired_at_unix"]):
        raise LocalRuntimeError("Local Qwen maintenance lease expiry is invalid")
    for field in ("owner", "reason"):
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip() or len(value) > 240:
            raise LocalRuntimeError(
                f"Local Qwen maintenance lease {field} is invalid"
            )
    return payload


def maintenance_status(
    raw: dict[str, Any],
    *,
    state_root: Path = DEFAULT_STATE_ROOT,
    now_unix: float | None = None,
) -> dict[str, Any]:
    config = validate_config(raw)
    payload = _read_maintenance(config, state_root)
    path = _maintenance_path(config, state_root)
    if payload is None:
        return {
            "status": "INACTIVE",
            "active": False,
            "lease_path": str(path),
            "lease_id": None,
        }
    now = time.time() if now_unix is None else float(now_unix)
    active = now < float(payload["expires_at_unix"])
    return {
        **payload,
        "status": "ACTIVE" if active else "EXPIRED",
        "active": active,
        "lease_path": str(path),
        "remaining_seconds": max(
            0.0, round(float(payload["expires_at_unix"]) - now, 3)
        ),
    }


def _assert_maintenance_allows(
    config: dict[str, Any],
    *,
    state_root: Path,
    maintenance_lease_id: str | None = None,
) -> dict[str, Any]:
    status = maintenance_status(config, state_root=state_root)
    if not status["active"]:
        return status
    if maintenance_lease_id and maintenance_lease_id == status.get("lease_id"):
        return status
    raise LocalRuntimeError(
        "Local Qwen is temporarily unavailable under maintenance lease "
        + str(status.get("lease_id"))
    )


@contextmanager
def _exclusive_lock(
    path: Path, timeout: int, *, timeout_message: str
) -> Iterable[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    started = time.monotonic()
    try:
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    handle.seek(0, os.SEEK_END)
                    if handle.tell() == 0:
                        handle.write(b"0")
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() - started >= timeout:
                        raise LocalRuntimeError(timeout_message)
                    time.sleep(1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


@contextmanager
def _startup_lock(path: Path, timeout: int) -> Iterable[None]:
    with _exclusive_lock(
        path,
        timeout,
        timeout_message="Timed out waiting for local runtime startup lock",
    ):
        yield


@contextmanager
def _execution_slot_lock(path: Path, slots: int, timeout: int) -> Iterable[int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    started = time.monotonic()
    acquired_slot: int | None = None
    try:
        handle.seek(0, os.SEEK_END)
        missing_bytes = slots - handle.tell()
        if missing_bytes > 0:
            handle.write(b"0" * missing_bytes)
            handle.flush()
        while acquired_slot is None:
            for slot in range(slots):
                try:
                    handle.seek(slot)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.lockf(
                            handle.fileno(),
                            fcntl.LOCK_EX | fcntl.LOCK_NB,
                            1,
                            slot,
                            os.SEEK_SET,
                        )
                    acquired_slot = slot
                    break
                except OSError:
                    continue
            if acquired_slot is not None:
                break
            if time.monotonic() - started >= timeout:
                raise LocalRuntimeError(
                    "Timed out waiting for a local Qwen execution slot"
                )
            time.sleep(0.1)
        yield acquired_slot
    finally:
        try:
            if acquired_slot is not None:
                handle.seek(acquired_slot)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.lockf(
                        handle.fileno(),
                        fcntl.LOCK_UN,
                        1,
                        acquired_slot,
                        os.SEEK_SET,
                    )
        finally:
            handle.close()


@contextmanager
def _all_execution_slots_lock(
    path: Path, slots: int, timeout: int
) -> Iterable[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    started = time.monotonic()
    acquired_slots: list[int] = []

    def unlock_all() -> None:
        for slot in reversed(acquired_slots):
            handle.seek(slot)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.lockf(
                    handle.fileno(), fcntl.LOCK_UN, 1, slot, os.SEEK_SET
                )
        acquired_slots.clear()

    try:
        handle.seek(0, os.SEEK_END)
        missing_bytes = slots - handle.tell()
        if missing_bytes > 0:
            handle.write(b"0" * missing_bytes)
            handle.flush()
        while len(acquired_slots) != slots:
            acquired_slots.clear()
            for slot in range(slots):
                try:
                    handle.seek(slot)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.lockf(
                            handle.fileno(),
                            fcntl.LOCK_EX | fcntl.LOCK_NB,
                            1,
                            slot,
                            os.SEEK_SET,
                        )
                    acquired_slots.append(slot)
                except OSError:
                    unlock_all()
                    break
            if len(acquired_slots) == slots:
                break
            if time.monotonic() - started >= timeout:
                raise LocalRuntimeError(
                    "Timed out draining local Qwen execution slots"
                )
            time.sleep(0.1)
        yield
    finally:
        try:
            unlock_all()
        finally:
            handle.close()


def capacity_lock_identity(config: dict[str, Any]) -> str:
    parsed = urllib.parse.urlparse(str(config.get("base_url", "")))
    host = str(parsed.hostname or "invalid").casefold()
    port = parsed.port or 0
    model = str(config.get("served_model_name", "")).strip().casefold()
    raw = f"{host}:{port}::{model}"
    slug = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    return slug[:96] or "local-runtime"


@contextmanager
def execution_lock(
    raw: dict[str, Any],
    *,
    timeout: int,
    capacity: int | None = None,
    state_root: Path = DEFAULT_STATE_ROOT,
) -> Iterable[dict[str, Any]]:
    config = validate_config(raw)
    _assert_maintenance_allows(config, state_root=state_root)
    effective_capacity = config["max_num_seqs"] if capacity is None else capacity
    if (
        isinstance(effective_capacity, bool)
        or not isinstance(effective_capacity, int)
        or not 1 <= effective_capacity <= config["max_num_seqs"]
    ):
        raise LocalRuntimeError(
            "Execution capacity must be between 1 and local_runtime.max_num_seqs"
        )
    lock_id = capacity_lock_identity(config)
    lock_path = state_root / lock_id / "execution.lock"
    with _execution_slot_lock(
        lock_path, effective_capacity, timeout
    ) as slot_index:
        _assert_maintenance_allows(config, state_root=state_root)
        yield {
            "state_id": config["state_id"],
            "capacity_lock_id": lock_id,
            "lock_path": str(lock_path),
            "max_num_seqs": effective_capacity,
            "runtime_max_num_seqs": config["max_num_seqs"],
            "slot_index": slot_index,
        }


def acquire_maintenance(
    raw: dict[str, Any],
    *,
    owner: str,
    reason: str,
    ttl_seconds: int,
    drain_timeout_seconds: int = 120,
    lease_id: str | None = None,
    state_root: Path = DEFAULT_STATE_ROOT,
) -> dict[str, Any]:
    config = validate_config(raw)
    if (
        isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, int)
        or not MAINTENANCE_MIN_TTL_SECONDS
        <= ttl_seconds
        <= MAINTENANCE_MAX_TTL_SECONDS
    ):
        raise LocalRuntimeError(
            "Maintenance TTL must be between "
            f"{MAINTENANCE_MIN_TTL_SECONDS} and {MAINTENANCE_MAX_TTL_SECONDS} seconds"
        )
    if (
        isinstance(drain_timeout_seconds, bool)
        or not isinstance(drain_timeout_seconds, int)
        or not 1 <= drain_timeout_seconds <= 900
    ):
        raise LocalRuntimeError(
            "Maintenance drain timeout must be between 1 and 900 seconds"
        )
    owner = owner.strip()
    reason = reason.strip()
    if not owner or len(owner) > 240:
        raise LocalRuntimeError("Maintenance owner is invalid")
    if not reason or len(reason) > 240:
        raise LocalRuntimeError("Maintenance reason is invalid")
    effective_lease_id = lease_id or f"qwen-maint-{uuid.uuid4().hex}"
    if not LEASE_ID_PATTERN.fullmatch(effective_lease_id):
        raise LocalRuntimeError("Maintenance lease id is invalid")

    state_dir = state_root / config["state_id"]
    lease_path = _maintenance_path(config, state_root)
    execution_path = state_dir / "execution.lock"
    with _startup_lock(state_dir / "startup.lock", drain_timeout_seconds):
        current = maintenance_status(config, state_root=state_root)
        if current["active"]:
            if current.get("lease_id") != effective_lease_id or current.get(
                "owner"
            ) != owner:
                raise LocalRuntimeError(
                    "Local Qwen already has an active maintenance lease: "
                    + str(current.get("lease_id"))
                )
            drained_at = current.get("drained_at_unix")
            if isinstance(drained_at, (int, float)) and not isinstance(
                drained_at, bool
            ):
                return current
            payload = _read_maintenance(config, state_root)
            if payload is None:
                raise LocalRuntimeError(
                    "Active maintenance lease disappeared during acquisition"
                )
        else:
            acquired_at = time.time()
            payload = {
                "schema_version": 1,
                "state_id": config["state_id"],
                "lease_id": effective_lease_id,
                "owner": owner,
                "reason": reason,
                "acquired_at_unix": acquired_at,
                "expires_at_unix": acquired_at + ttl_seconds,
                "ttl_seconds": ttl_seconds,
            }
            _atomic_json(lease_path, payload)
        try:
            with _all_execution_slots_lock(
                execution_path,
                config["max_num_seqs"],
                drain_timeout_seconds,
            ):
                payload["drained_at_unix"] = time.time()
                _atomic_json(lease_path, payload)
        except Exception:
            try:
                stored = _read_maintenance(config, state_root)
                if stored and stored.get("lease_id") == effective_lease_id:
                    lease_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
    return maintenance_status(config, state_root=state_root)


def release_maintenance(
    raw: dict[str, Any],
    *,
    lease_id: str,
    state_root: Path = DEFAULT_STATE_ROOT,
) -> dict[str, Any]:
    config = validate_config(raw)
    if not LEASE_ID_PATTERN.fullmatch(lease_id):
        raise LocalRuntimeError("Maintenance lease id is invalid")
    state_dir = state_root / config["state_id"]
    lease_path = _maintenance_path(config, state_root)
    with _startup_lock(state_dir / "startup.lock", 120):
        current = maintenance_status(config, state_root=state_root)
        if current.get("lease_id") != lease_id:
            raise LocalRuntimeError("Maintenance lease id does not match")
        endpoint = endpoint_status(config, timeout=3)
        if endpoint["status"] != "READY":
            raise LocalRuntimeError(
                "Refusing to release maintenance before the exact local Qwen endpoint is READY"
            )
        lease_path.unlink(missing_ok=False)
    return {
        "status": "RELEASED",
        "active": False,
        "lease_id": lease_id,
        "endpoint": endpoint,
        "lease_path": str(lease_path),
    }


def force_clear_corrupt_maintenance(
    raw: dict[str, Any],
    *,
    state_root: Path = DEFAULT_STATE_ROOT,
) -> dict[str, Any]:
    """Quarantine an unreadable lease only after the exact runtime is restored."""
    config = validate_config(raw)
    state_dir = state_root / config["state_id"]
    lease_path = _maintenance_path(config, state_root)
    with _startup_lock(state_dir / "startup.lock", 120):
        if not lease_path.is_file():
            raise LocalRuntimeError("No maintenance lease exists to quarantine")
        try:
            _read_maintenance(config, state_root)
        except LocalRuntimeError as exc:
            validation_error = str(exc)
        else:
            raise LocalRuntimeError(
                "Maintenance lease is valid; use maintenance-release or maintenance-recover"
            )
        endpoint = endpoint_status(config, timeout=3)
        if endpoint["status"] != "READY":
            raise LocalRuntimeError(
                "Refusing to quarantine a corrupt maintenance lease before the exact "
                "local Qwen endpoint is READY"
            )
        quarantine_path = lease_path.with_name(
            f"maintenance.json.corrupt.{int(time.time())}.{uuid.uuid4().hex}"
        )
        os.replace(lease_path, quarantine_path)
    return {
        "status": "CORRUPT_LEASE_QUARANTINED",
        "active": False,
        "endpoint": endpoint,
        "lease_path": str(lease_path),
        "quarantine_path": str(quarantine_path),
        "validation_error": validation_error,
    }


def _linux_process_present(config: dict[str, Any]) -> bool:
    try:
        completed = _run_wsl(
            config,
            ["pgrep", "-f", config["served_model_name"]],
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0 and bool(completed.stdout.strip())


def _systemd_unit_status(config: dict[str, Any]) -> dict[str, Any]:
    try:
        completed = _run_wsl(
            config,
            [
                "systemctl",
                "show",
                config["systemd_unit"],
                "--property=ActiveState,SubState,MainPID",
                "--no-pager",
            ],
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"running": False, "active_state": "unknown", "sub_state": "unknown"}
    values: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    active_state = values.get("ActiveState", "not-found")
    return {
        "running": completed.returncode == 0
        and active_state in {"active", "activating"},
        "active_state": active_state,
        "sub_state": values.get("SubState", "not-found"),
        "main_pid": int(values.get("MainPID", "0") or "0"),
    }


def _log_tail(path: Path, max_chars: int = 4000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-max_chars:]


def ensure_runtime(
    raw: dict[str, Any],
    *,
    state_root: Path = DEFAULT_STATE_ROOT,
    maintenance_lease_id: str | None = None,
) -> dict[str, Any]:
    config = validate_config(raw)
    state_dir = state_root / config["state_id"]
    startup_timeout = int(config["startup_timeout_seconds"])
    with _startup_lock(state_dir / "startup.lock", startup_timeout):
        _assert_maintenance_allows(
            config,
            state_root=state_root,
            maintenance_lease_id=maintenance_lease_id,
        )
        endpoint = endpoint_status(config)
        if endpoint["status"] == "READY":
            return {"status": "READY", "started": False, "endpoint": endpoint}
        if endpoint["status"] == "MODEL_MISMATCH":
            raise LocalRuntimeError(
                endpoint["error"] or "Local endpoint model mismatch"
            )
        prerequisites = prerequisite_status(config)
        if not prerequisites["ready_to_start"]:
            raise LocalRuntimeError(
                "Local Qwen prerequisites failed: " + "; ".join(prerequisites["errors"])
            )

        log_path = state_dir / "vllm.log"
        started = False
        unit = _systemd_unit_status(config)
        managed = bool(unit["running"])
        if not managed:
            if _linux_process_present(config):
                raise LocalRuntimeError(
                    "An unmanaged local Qwen process is present; refusing a duplicate launch"
                )
            state_dir.mkdir(parents=True, exist_ok=True)
            log_path.write_bytes(b"")
            _run_wsl(
                config,
                ["systemctl", "reset-failed", config["systemd_unit"]],
                timeout=10,
            )
            completed = subprocess.run(
                serve_command(config, log_path=log_path),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
            if completed.returncode != 0:
                raise LocalRuntimeError(
                    "Failed to launch local vLLM: " + completed.stderr.strip()[:1000]
                )
            started = True
            managed = True
            _atomic_json(
                state_dir / "runtime.json",
                {
                    "schema_version": 1,
                    "state_id": config["state_id"],
                    "systemd_unit": config["systemd_unit"],
                    "served_model_name": config["served_model_name"],
                    "base_url": config["base_url"],
                    "context_window_tokens": config["context_window_tokens"],
                    "max_num_seqs": config["max_num_seqs"],
                    "started_at_unix": time.time(),
                    "log_path": str(log_path),
                },
            )

        deadline = time.monotonic() + startup_timeout
        while time.monotonic() < deadline:
            endpoint = endpoint_status(config, timeout=3)
            if endpoint["status"] == "READY":
                return {
                    "status": "READY",
                    "started": started,
                    "endpoint": endpoint,
                    "log_path": str(log_path),
                }
            if endpoint["status"] == "MODEL_MISMATCH":
                raise LocalRuntimeError(
                    endpoint["error"] or "Local endpoint model mismatch"
                )
            if managed and not _systemd_unit_status(config)["running"]:
                raise LocalRuntimeError(
                    "Local vLLM exited during startup. Log tail:\n"
                    + _log_tail(log_path)
                )
            time.sleep(2)
        raise LocalRuntimeError(
            f"Local Qwen did not become ready in {startup_timeout}s. Log tail:\n"
            + _log_tail(log_path)
        )


def stop_runtime(
    raw: dict[str, Any],
    *,
    state_root: Path = DEFAULT_STATE_ROOT,
    maintenance_lease_id: str | None = None,
) -> dict[str, Any]:
    config = validate_config(raw)
    state_dir = state_root / config["state_id"]
    with _startup_lock(state_dir / "startup.lock", 120):
        maintenance = maintenance_status(config, state_root=state_root)
        if maintenance["active"]:
            _assert_maintenance_allows(
                config,
                state_root=state_root,
                maintenance_lease_id=maintenance_lease_id,
            )
        elif maintenance_lease_id:
            raise LocalRuntimeError(
                "Maintenance lease is not active; refusing a stale lease-authorized stop"
            )
        unit = _systemd_unit_status(config)
        if not unit["running"]:
            endpoint = endpoint_status(config, timeout=1)
            if endpoint["status"] == "READY":
                raise LocalRuntimeError(
                    "Local Qwen endpoint is READY but is not owned by the configured systemd unit"
                )
            return {"status": "STOPPED", "exit_code": 0}
        completed = _run_wsl(
            config,
            ["systemctl", "stop", config["systemd_unit"]],
            timeout=20,
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if endpoint_status(config, timeout=1)["status"] == "STOPPED":
                return {"status": "STOPPED", "exit_code": completed.returncode}
            time.sleep(1)
        raise LocalRuntimeError("Local Qwen endpoint did not stop within 30 seconds")


def recover_maintenance(
    raw: dict[str, Any],
    *,
    lease_id: str,
    state_root: Path = DEFAULT_STATE_ROOT,
) -> dict[str, Any]:
    ensured = ensure_runtime(
        raw,
        state_root=state_root,
        maintenance_lease_id=lease_id,
    )
    released = release_maintenance(
        raw,
        lease_id=lease_id,
        state_root=state_root,
    )
    return {
        "status": "RECOVERED",
        "lease_id": lease_id,
        "ensure": ensured,
        "release": released,
    }


def load_profile(path: Path, agent: str) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    profile = raw.get("profiles", {}).get(agent) if isinstance(raw, dict) else None
    local = profile.get("local_runtime") if isinstance(profile, dict) else None
    if not isinstance(local, dict):
        raise LocalRuntimeError(f"Profile {agent} has no local_runtime configuration")
    return local


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Manage the bounded local Qwen vLLM runtime"
    )
    parser.add_argument(
        "action",
        choices=(
            "status",
            "ensure",
            "stop",
            "maintenance-status",
            "maintenance-acquire",
            "maintenance-release",
            "maintenance-recover",
            "maintenance-force-clear-corrupt",
        ),
    )
    parser.add_argument("--profiles", required=True, type=Path)
    parser.add_argument("--agent", default="qwen-local")
    parser.add_argument("--lease-id")
    parser.add_argument("--owner")
    parser.add_argument("--reason")
    parser.add_argument("--ttl-seconds", type=int, default=5400)
    parser.add_argument("--drain-timeout-seconds", type=int, default=120)
    args = parser.parse_args(argv)
    try:
        config = load_profile(args.profiles.resolve(), args.agent)
        if args.action == "status":
            result = runtime_status(config)
        elif args.action == "ensure":
            result = ensure_runtime(
                config, maintenance_lease_id=args.lease_id
            )
        elif args.action == "stop":
            result = stop_runtime(
                config, maintenance_lease_id=args.lease_id
            )
        elif args.action == "maintenance-status":
            result = maintenance_status(config)
        elif args.action == "maintenance-acquire":
            if not args.owner or not args.reason:
                raise LocalRuntimeError(
                    "maintenance-acquire requires --owner and --reason"
                )
            result = acquire_maintenance(
                config,
                owner=args.owner,
                reason=args.reason,
                ttl_seconds=args.ttl_seconds,
                drain_timeout_seconds=args.drain_timeout_seconds,
                lease_id=args.lease_id,
            )
        elif args.action == "maintenance-release":
            if not args.lease_id:
                raise LocalRuntimeError(
                    "maintenance-release requires --lease-id"
                )
            result = release_maintenance(config, lease_id=args.lease_id)
        elif args.action == "maintenance-recover":
            if not args.lease_id:
                raise LocalRuntimeError(
                    "maintenance-recover requires --lease-id"
                )
            result = recover_maintenance(config, lease_id=args.lease_id)
        else:
            result = force_clear_corrupt_maintenance(config)
    except (OSError, ValueError, LocalRuntimeError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
