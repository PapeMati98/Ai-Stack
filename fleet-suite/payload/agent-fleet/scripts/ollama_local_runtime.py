from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable


class LocalRuntimeError(RuntimeError):
    pass


STATE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{2,63}$")
MODEL_NAME_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{1,63}(/[A-Za-z0-9][A-Za-z0-9._-]{1,63})?(:[A-Za-z0-9._-]{1,32})?$"
)
SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
DEFAULT_STATE_ROOT = Path.home() / ".agents" / "local-models"
PINNED_QWEN_MODEL = "huihui_ai/Qwen3.8-abliterated"
PINNED_QWEN_MANIFEST_DIGEST = (
    "bdc5e13cd1645bc409e95a7d9e864dc3ce3e5fe5beeb24a43c193e8041edd3e0"
)
PINNED_OLLAMA_EXECUTABLE = (
    r"%LOCALAPPDATA%\MateuszAI\ollama\0.33.2\ollama.exe"
)
PINNED_OLLAMA_VERSION = "0.33.2"
PINNED_OLLAMA_EXECUTABLE_SHA256 = (
    "c79df1e0c1bfa10ed813c7030ac4c3ba38bb0e350bd7322d9bb58320343235c6"
)
PINNED_OLLAMA_BASE_URL = "http://127.0.0.1:11439/v1"
PINNED_OLLAMA_TAGS_URL = "http://127.0.0.1:11439/api/tags"


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise LocalRuntimeError(f"{label} must be a positive integer")
    return value


def _loopback_url(value: Any, *, path: str, label: str) -> tuple[str, int]:
    normalized = str(value or "").rstrip("/")
    parsed = urllib.parse.urlparse(normalized)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.path != path
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
        or parsed.port is None
        or not 1024 <= parsed.port <= 65535
    ):
        raise LocalRuntimeError(
            f"{label} must be an explicit 127.0.0.1 http URL ending in {path}"
        )
    return normalized, int(parsed.port)


def _expanded_windows_path(value: Any, *, label: str) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise LocalRuntimeError(f"{label} is required")

    def replace_environment(match: re.Match[str]) -> str:
        name = match.group(1)
        resolved = os.environ.get(name)
        return resolved if resolved else match.group(0)

    expanded = re.sub(r"%([^%]+)%", replace_environment, raw)
    if re.search(r"%[^%]+%", expanded):
        raise LocalRuntimeError(f"{label} contains an unresolved environment variable")
    return Path(os.path.expanduser(expanded)).resolve()


def _normalize_manifest_digest(value: Any) -> str | None:
    raw = str(value or "").strip().casefold()
    if raw.startswith("sha256:"):
        raw = raw.removeprefix("sha256:")
    return raw if SHA256_PATTERN.fullmatch(raw) else None


def validate_config(raw: dict[str, Any], *, require_executable: bool = True) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("kind") != "ollama-windows-external":
        raise LocalRuntimeError(
            "local_runtime.kind must be ollama-windows-external"
        )
    config = dict(raw)
    state_id = str(config.get("state_id", "")).strip()
    served_model = str(config.get("served_model_name", "")).strip()
    if not STATE_ID_PATTERN.fullmatch(state_id):
        raise LocalRuntimeError("local_runtime.state_id is invalid")
    if not MODEL_NAME_PATTERN.fullmatch(served_model):
        raise LocalRuntimeError("local_runtime.served_model_name is invalid")
    if served_model != PINNED_QWEN_MODEL:
        raise LocalRuntimeError("local_runtime.served_model_name must use the pinned Qwen model")

    base_url, port = _loopback_url(
        config.get("base_url"), path="/v1", label="local_runtime.base_url"
    )
    native_tags_url, tags_port = _loopback_url(
        config.get("native_tags_url"),
        path="/api/tags",
        label="local_runtime.native_tags_url",
    )
    if tags_port != port:
        raise LocalRuntimeError("Ollama OpenAI and native endpoints must share a port")

    executable = _expanded_windows_path(
        config.get("ollama_executable_windows"),
        label="local_runtime.ollama_executable_windows",
    )
    if require_executable and not executable.is_file():
        raise LocalRuntimeError(f"Ollama executable is unavailable: {executable}")
    for key in ("ollama_executable_sha256", "model_manifest_digest"):
        value = str(config.get(key, "")).strip().casefold()
        if not SHA256_PATTERN.fullmatch(value):
            raise LocalRuntimeError(f"local_runtime.{key} must be SHA-256")
        config[key] = value
    expected_version = str(config.get("ollama_version", "")).strip()
    if not VERSION_PATTERN.fullmatch(expected_version):
        raise LocalRuntimeError("local_runtime.ollama_version is invalid")

    context_window = _positive_int(
        config.get("context_window_tokens"),
        "local_runtime.context_window_tokens",
    )
    if context_window != 16_384:
        raise LocalRuntimeError("RTX 5070 Ti canary context must be exactly 16384")
    max_num_seqs = _positive_int(
        config.get("max_num_seqs"), "local_runtime.max_num_seqs"
    )
    if max_num_seqs != 1:
        raise LocalRuntimeError("RTX 5070 Ti canary permits exactly one sequence")
    max_wall_time = _positive_int(
        config.get("max_wall_time_seconds"),
        "local_runtime.max_wall_time_seconds",
    )
    if max_wall_time != 600:
        raise LocalRuntimeError("RTX 5070 Ti canary wall deadline must be 600 seconds")
    if config.get("externally_managed") is not True:
        raise LocalRuntimeError("Ollama canary must be externally managed")
    if config.get("provider_cost_pln") != 0:
        raise LocalRuntimeError("Local Qwen provider cost must be exactly 0 PLN")
    if config.get("provider_cost_display") != "0 zł":
        raise LocalRuntimeError("Local Qwen provider cost display must be '0 zł'")

    config.update(
        {
            "base_url": base_url,
            "native_tags_url": native_tags_url,
            "ollama_executable_windows": str(executable),
            "port": port,
        }
    )
    return config


def validate_pinned_qwen_ollama_contract(
    raw: dict[str, Any], *, require_executable: bool = True
) -> dict[str, Any]:
    config = validate_config(raw, require_executable=require_executable)
    expected_executable = _expanded_windows_path(
        PINNED_OLLAMA_EXECUTABLE,
        label="pinned Ollama executable",
    )
    expected = {
        "base_url": PINNED_OLLAMA_BASE_URL,
        "native_tags_url": PINNED_OLLAMA_TAGS_URL,
        "served_model_name": PINNED_QWEN_MODEL,
        "model_manifest_digest": PINNED_QWEN_MANIFEST_DIGEST,
        "ollama_executable_windows": str(expected_executable),
        "ollama_executable_sha256": PINNED_OLLAMA_EXECUTABLE_SHA256,
        "ollama_version": PINNED_OLLAMA_VERSION,
        "context_window_tokens": 16_384,
        "max_num_seqs": 1,
        "max_wall_time_seconds": 600,
        "externally_managed": True,
        "provider_cost_pln": 0,
        "provider_cost_display": "0 zł",
    }
    mismatches = [
        key
        for key, expected_value in expected.items()
        if config.get(key) != expected_value
    ]
    if mismatches:
        raise LocalRuntimeError(
            "Pinned Qwen Ollama runtime contract mismatch: "
            + ",".join(sorted(mismatches))
        )
    return config


def sandbox_base_url(raw: dict[str, Any]) -> str:
    config = validate_config(raw)
    return f"http://host.docker.internal:{config['port']}/v1"


def _read_json_url(url: str, *, timeout: float) -> Any:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as response:
            return json.loads(response.read(2_000_000).decode("utf-8"))
    except (
        OSError,
        TimeoutError,
        urllib.error.URLError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise LocalRuntimeError(f"Ollama endpoint failed at {url}: {exc}") from exc


def _endpoint_identity(config: dict[str, Any]) -> dict[str, Any]:
    tags_payload = _read_json_url(config["native_tags_url"], timeout=3.0)
    models_payload = _read_json_url(config["base_url"] + "/models", timeout=3.0)
    tags = [
        {
            "name": str(row.get("name")),
            "manifest_digest": _normalize_manifest_digest(row.get("digest")),
        }
        for row in (
            tags_payload.get("models", [])
            if isinstance(tags_payload, dict)
            else []
        )
        if isinstance(row, dict) and row.get("name")
    ]
    tag_names = sorted({row["name"] for row in tags})
    model_ids = sorted(
        {
            str(row.get("id"))
            for row in (models_payload.get("data", []) if isinstance(models_payload, dict) else [])
            if isinstance(row, dict) and row.get("id")
        }
    )
    expected = config["served_model_name"]
    expected_names = {expected, expected + ":latest"}
    matching_tags = [row for row in tags if row["name"] in expected_names]
    expected_digest = config["model_manifest_digest"]
    matching_model_ids = sorted(set(model_ids) & expected_names)
    digest_matches = bool(matching_tags) and all(
        row["manifest_digest"] == expected_digest for row in matching_tags
    )
    return {
        "expected_model": expected,
        "expected_manifest_digest": expected_digest,
        "native_models": tag_names,
        "native_model_digests": matching_tags,
        "openai_models": model_ids,
        "matching_openai_models": matching_model_ids,
        "matches": digest_matches and bool(matching_model_ids),
    }


def _listener_identity(config: dict[str, Any]) -> dict[str, Any]:
    port = int(config["port"])
    script = (
        f"$rows=@(Get-NetTCPConnection -State Listen -LocalPort {port} "
        "-ErrorAction Stop);"
        "$out=@();foreach($row in $rows){$p=Get-Process -Id $row.OwningProcess "
        "-ErrorAction Stop;$out+=[pscustomobject]@{LocalAddress=$row.LocalAddress;"
        "LocalPort=$row.LocalPort;OwningProcess=$row.OwningProcess;Path=$p.Path}};"
        "$out|ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LocalRuntimeError(f"Ollama listener proof failed: {exc}") from exc
    if completed.returncode != 0 or not completed.stdout.strip():
        raise LocalRuntimeError(
            "Ollama listener proof failed: " + completed.stderr.strip()[:500]
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise LocalRuntimeError("Ollama listener proof returned invalid JSON") from exc
    rows = payload if isinstance(payload, list) else [payload]
    expected_path = os.path.normcase(str(Path(config["ollama_executable_windows"])))
    normalized = [
        {
            "local_address": str(row.get("LocalAddress", "")),
            "local_port": int(row.get("LocalPort", 0) or 0),
            "pid": int(row.get("OwningProcess", 0) or 0),
            "path": str(row.get("Path", "")),
        }
        for row in rows
        if isinstance(row, dict)
    ]
    matches = bool(normalized) and all(
        row["local_address"] == "127.0.0.1"
        and row["local_port"] == port
        and os.path.normcase(str(Path(row["path"]))) == expected_path
        for row in normalized
    )
    return {"listeners": normalized, "matches": matches}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _executable_identity(config: dict[str, Any]) -> dict[str, Any]:
    executable = Path(config["ollama_executable_windows"])
    observed_hash = _sha256(executable)
    expected_hash = config["ollama_executable_sha256"]
    if observed_hash != expected_hash:
        return {
            "path": str(executable),
            "sha256": observed_hash,
            "expected_sha256": expected_hash,
            "version": None,
            "expected_version": config["ollama_version"],
            "matches": False,
            "error": "Ollama executable SHA-256 mismatch; version probe not executed",
        }
    try:
        completed = subprocess.run(
            [str(executable), "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LocalRuntimeError(f"Ollama version probe failed: {exc}") from exc
    output = (completed.stdout + "\n" + completed.stderr).strip()
    version_match = re.search(r"(?:version\s+)?([0-9]+\.[0-9]+\.[0-9]+)", output)
    observed_version = version_match.group(1) if version_match else None
    observed_hash_after_probe = _sha256(executable)
    return {
        "path": str(executable),
        "sha256": observed_hash_after_probe,
        "sha256_before_probe": observed_hash,
        "expected_sha256": expected_hash,
        "version": observed_version,
        "expected_version": config["ollama_version"],
        "matches": completed.returncode == 0
        and observed_hash_after_probe == expected_hash
        and observed_version == config["ollama_version"],
    }


def runtime_status(raw: dict[str, Any]) -> dict[str, Any]:
    config = validate_pinned_qwen_ollama_contract(raw)
    errors: list[str] = []
    try:
        endpoint = _endpoint_identity(config)
    except LocalRuntimeError as exc:
        endpoint = {"matches": False, "error": str(exc)}
        errors.append(str(exc))
    try:
        listener = _listener_identity(config)
    except LocalRuntimeError as exc:
        listener = {"matches": False, "error": str(exc)}
        errors.append(str(exc))
    try:
        executable = _executable_identity(config)
    except LocalRuntimeError as exc:
        executable = {"matches": False, "error": str(exc)}
        errors.append(str(exc))
    ready = all(
        bool(row.get("matches")) for row in (endpoint, listener, executable)
    )
    if not endpoint.get("matches"):
        errors.append("exact-model-identity-mismatch")
    if not listener.get("matches"):
        errors.append("loopback-listener-ownership-mismatch")
    if not executable.get("matches"):
        errors.append("ollama-executable-identity-mismatch")
    return {
        "status": "READY" if ready else "UNAVAILABLE",
        "routing_ready": ready,
        "runtime_kind": config["kind"],
        "externally_managed": True,
        "state_id": config["state_id"],
        "endpoint": endpoint,
        "listener": listener,
        "executable": executable,
        "model_manifest": {
            "expected_digest": config["model_manifest_digest"],
            "verification": "native Ollama /api/tags manifest digest; tag name alone is insufficient",
        },
        "context_window_tokens": config["context_window_tokens"],
        "max_num_seqs": config["max_num_seqs"],
        "max_wall_time_seconds": config["max_wall_time_seconds"],
        "provider_cost_pln": config["provider_cost_pln"],
        "provider_cost_display": config["provider_cost_display"],
        "excluded_costs": "electricity and hardware",
        "errors": sorted(set(errors)),
    }


def ensure_runtime(raw: dict[str, Any]) -> dict[str, Any]:
    status = runtime_status(raw)
    if not status["routing_ready"]:
        raise LocalRuntimeError(
            "Externally managed Ollama canary is not READY; Fleet will not start, "
            "stop, or repair it"
        )
    return {**status, "started_by_fleet": False}


@contextmanager
def _execution_slot_lock(path: Path, timeout: int) -> Iterable[int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    started = time.monotonic()
    acquired = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() < 1:
            handle.write(b"0")
            handle.flush()
        while not acquired:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.lockf(
                        handle.fileno(),
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                        1,
                        0,
                        os.SEEK_SET,
                    )
                acquired = True
            except OSError:
                if time.monotonic() - started >= timeout:
                    raise LocalRuntimeError(
                        "Timed out waiting for the RTX 5070 Ti local Qwen slot"
                    )
                time.sleep(0.1)
        yield 0
    finally:
        try:
            if acquired:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.lockf(handle.fileno(), fcntl.LOCK_UN, 1, 0, os.SEEK_SET)
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
    effective_capacity = config["max_num_seqs"] if capacity is None else capacity
    if effective_capacity != 1:
        raise LocalRuntimeError("RTX 5070 Ti canary execution capacity must be 1")
    lock_id = capacity_lock_identity(config)
    lock_path = state_root / lock_id / "execution.lock"
    with _execution_slot_lock(lock_path, timeout) as slot_index:
        yield {
            "state_id": config["state_id"],
            "capacity_lock_id": lock_id,
            "lock_path": str(lock_path),
            "max_num_seqs": 1,
            "runtime_max_num_seqs": 1,
            "slot_index": slot_index,
        }
