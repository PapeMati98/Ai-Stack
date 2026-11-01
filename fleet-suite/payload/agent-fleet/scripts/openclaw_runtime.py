"""OpenClaw native Windows adapter helper.

This module is bounded: it does **not** spawn the process, persist credentials,
or mutate the worktree. It validates the OpenClaw integration contract that the
parent commits to and produces the literal argv vector, per-run config payload,
and envelope parsing helper that the Fleet harness consumes.

The module reflects the verified source-contract snapshot
``evidence-input/openclaw-agent-exec.mjs``. That file is read-only upstream
evidence and is never imported, executed, or copied into Fleet.

Hard constraints honoured:

* ``--config`` is mutually exclusive with ``--isolated`` / ``--auth-env-only``.
  The harness always pins ``--config`` so credentials cannot be laundered
  through ambient OpenClaw state, and refuses to merge any of the three
  overloads from the request payload.
* ``--state-dir`` is mandatory and run-owned. The harness never deletes an
  existing state directory; the parent owns the run-owned directory and
  OpenClaw's automatic temporary cleanup is opt-out via ``--state-dir``.
* ``--deliver`` is never added because Fleet has its own gating and
  receipt/projection logic.
* The per-run JSON config is **always fresh**: ``fs.workspaceOnly=true``,
  mode-specific tool allow/deny, ambient MCP / plugins / skills inheritance
  disabled, subagent and network execution disabled. A reviewer run must not
  carry ``edit``, ``write``, or shell capabilities even by default.

Safe provenance aliases are defined below; the helper deliberately refuses
fallbacks to other subscription or metered endpoints and the harness never
invokes ``--fallback`` unless the parent explicitly proves them.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import urllib.error
import urllib.request
from contextlib import closing
from urllib.parse import urlparse
from pathlib import Path
from typing import Any, Iterable


class OpenClawRuntimeError(RuntimeError):
    """Raised when an OpenClaw integration invariant is violated."""


# ---------------------------------------------------------------------------
# Pinned native Windows contract (per CONTEXT.md).
# ---------------------------------------------------------------------------
PINNED_OPENCLAW_VERSION = "2026.9.4"
PINNED_OPENCLAW_INSTALL_ROOT = Path.home() / ".agents" / "runtime" / "openclaw" / PINNED_OPENCLAW_VERSION
PINNED_OPENCLAW_BIN_DIRECTORY = (
    PINNED_OPENCLAW_INSTALL_ROOT / "node_modules" / ".bin"
)
PINNED_OPENCLAW_EXECUTABLE = PINNED_OPENCLAW_BIN_DIRECTORY / "openclaw.cmd"
PINNED_OPENCLAW_AGENT_EXEC = (
    "agent.exec"  # command contract; argv is literal and not split.
)
PINNED_OPENCLAW_MAX_MESSAGE_BYTES = 4_194_304

# ---------------------------------------------------------------------------
# Pinned local backend contract (per CONTEXT.md §6).
# ---------------------------------------------------------------------------
OPENCLAW_LOCAL_RUNTIME_KIND = "openclaw-vllm"
OPENCLAW_LOCAL_BASE_URL = "http://127.0.0.1:11440/v1"
OPENCLAW_LOCAL_SERVED_MODEL = "Qwen3.8-27B"
OPENCLAW_LOCAL_PROVIDER = "vllm"
OPENCLAW_LOCAL_PROVIDER_MODEL = (
    f"{OPENCLAW_LOCAL_PROVIDER}/{OPENCLAW_LOCAL_SERVED_MODEL}"
)
# The shipped profile keeps the default 16384-token context until the parent
# proves a larger bound; explicit parent activation may request up to the
# advertised 524288-token ceiling in bounded steps.
OPENCLAW_LOCAL_DEFAULT_CONTEXT_TOKENS = 16384
# Pinned per-request output cap of the local lane (OpenClaw model ``maxTokens``).
# Reasoning tokens count against it; Fleet names the cap when an answer ends
# without a handoff at usage.output >= this value.
# HD90dab7: raised 2048 -> 8192 (finite bound kept); native stack-only failed
# run f662e9e4b0 had 12 assistant turns with the last turn a malformed tool
# call at usage.output exactly 2048 (earlier outputs 32..929).
OPENCLAW_LOCAL_MAX_OUTPUT_TOKENS = 8192
OPENCLAW_LOCAL_MIN_CONTEXT_TOKENS = 16384
OPENCLAW_LOCAL_MAX_CONTEXT_TOKENS = 524288
OPENCLAW_LOCAL_PROBE_TIMEOUT_SECONDS = 5.0
OPENCLAW_LOCAL_NOT_READY_REASON = (
    "Local Qwen3.8-27B vLLM backend NOT_READY by default: shipped profiles "
    "ship with runtime_not_ready=true so Fleet refuses to advertise the 11440 "
    "endpoint. The official BF16 weights now serve localhost:11440 on "
    "driver 610.88 / 500W and a real OpenClaw agent exec canary passed with "
    "exact vllm/Qwen3.8-27B identity and the bounded 16384-token window; "
    "the pending 500k/512k context canary is still running and has not "
    "validated a larger bound, so the configured context stays at 16384 "
    "tokens until the parent flips runtime_not_ready=false and supplies a "
    "verified context_window_tokens."
)

# ---------------------------------------------------------------------------
# Muse subscription fallback contract (per CONTEXT.md §7).
# ---------------------------------------------------------------------------
OPENCLAW_MUSE_RUNTIME_KIND = "openclaw-muse-subscription"
OPENCLAW_MUSE_SUBSCRIPTION_MODEL = "muse-spark-1.3"
OPENCLAW_MUSE_SUBSCRIPTION_PROVIDER = "muse-code"
OPENCLAW_MUSE_SUBSCRIPTION_PROVIDER_MODEL = (
    f"{OPENCLAW_MUSE_SUBSCRIPTION_PROVIDER}/{OPENCLAW_MUSE_SUBSCRIPTION_MODEL}"
)
OPENCLAW_MUSE_FORBIDDEN_MARKER = "contributor"
OPENCLAW_MUSE_SUBSCRIPTION_BLOCKER = (
    "Muse Code subscription capability is unproven. Fleet never auto-routes "
    "to muse-spark-1.3 via OpenClaw; the parent must verify the Meta Code "
    "Plan SUBSCRIPTION path is reachable and contributor-free before flipping "
    "routing. Metered Meta API is categorically not authorised."
)

# ---------------------------------------------------------------------------
# Ambient-fallback refusal contract (per CONTEXT.md §5, §7).
# ---------------------------------------------------------------------------
AMBIENT_OPENCLAW_ENV_KEYS = (
    # Config/state/root overrides can redirect OpenClaw to a user's ambient
    # profile.  These names are taken from the installed 2026.9.4 CLI source;
    # do not invent environment controls to emulate an isolation feature.
    "OPENCLAW_CONFIG_PATH",
    "OPENCLAW_STATE_DIR",
    "OPENCLAW_WORKSPACE_DIR",
    "OPENCLAW_HOME",
    "OPENCLAW_AGENT_DIR",
    "OPENCLAW_OAUTH_DIR",
    "OPENCLAW_BUNDLED_PLUGINS_DIR",
    "OPENCLAW_BUNDLED_SKILLS_DIR",
    "OPENCLAW_BUNDLED_HOOKS_DIR",
    "OPENCLAW_PLUGIN_CATALOG_PATHS",
    "OPENCLAW_MPM_CATALOG_PATHS",
    "OPENCLAW_PLUGIN_INSTALL_OVERRIDES",
    "OPENCLAW_EXTENSIONS",
    "OPENCLAW_INCLUDE_ROOTS",
    "OPENCLAW_PROFILE",
    "OPENCLAW_GATEWAY_URL",
    "OPENCLAW_GATEWAY_TOKEN",
    "OPENCLAW_GATEWAY_PASSWORD",
    "OPENCLAW_GATEWAY_SECRET",
    "OPENCLAW_AUTH_PROFILE_SECRET_KEY",
    "OPENCLAW_MCP_TOKEN",
    "OPENCLAW_MCP_CLI_CAPTURE_KEY",
    "OPENCLAW_LOAD_SHELL_ENV",
    "OPENCLAW_DEFER_SHELL_ENV_FALLBACK",
)

# OpenClaw 2026.9.4 exposes canonical lowercase tool names.  This list is
# intentionally kept in sync with the installed core descriptors rather than
# with Claude/Agent Fleet tool names. Explicit full profile avoids inheriting
# agent-exec coding defaults; the exact allowlist and denylist narrow exposure.
OPENCLAW_CORE_TOOL_NAMES = (
    "edit",
    "read",
    "ls",
    "write",
    "apply_patch",
    "exec",
    "process",
    "agents_list",
    "agents_wait",
    "ask_user",
    "openclaw",
    "computer",
    "conversations_list",
    "conversations_send",
    "conversations_turn",
    "screen",
    "secrets",
    "dashboard",
    "gateway",
    "get_goal",
    "github_identity_status",
    "github_publish",
    "heartbeat_respond",
    "view_image",
    "image_generate",
    "message",
    "mobile_ui",
    "music_generate",
    "nodes",
    "pdf",
    "session_status",
    "show_widget",
    "progress_card",
    "sessions",
    "sessions_history",
    "sessions_list",
    "sessions_search",
    "sessions_send",
    "sessions_spawn",
    "sessions_yield",
    "structured_output",
    "skill_workshop",
    "suggest_task",
    "create_goal",
    "subagents",
    "terminal",
    "portal",
    "transcripts",
    "tts",
    "update_goal",
    "dismiss_task",
    "video_generate",
    "web_fetch",
    "web_search",
)

OPENCLAW_READ_TOOLS = ("read", "ls")
OPENCLAW_IMPLEMENT_TOOLS = (*OPENCLAW_READ_TOOLS, "write", "edit", "apply_patch")

# Tools the harness always denies in both modes.  Mode-specific builders add
# the write/edit tools to this deny list for review runs.  All entries are
# canonical OpenClaw names; no Claude tool aliases are accepted.
ALWAYS_DENIED_TOOLS = tuple(
    tool for tool in OPENCLAW_CORE_TOOL_NAMES if tool not in OPENCLAW_IMPLEMENT_TOOLS
)
IMPLEMENT_DENIED_TOOLS = ALWAYS_DENIED_TOOLS
REVIEW_DENIED_TOOLS = tuple(
    tool for tool in OPENCLAW_CORE_TOOL_NAMES if tool not in OPENCLAW_READ_TOOLS
)
IMPLEMENT_ALLOWED_TOOLS = OPENCLAW_IMPLEMENT_TOOLS
REVIEW_ALLOWED_TOOLS = OPENCLAW_READ_TOOLS

# ---------------------------------------------------------------------------
# Fleet/OpenClaw sandbox MCP binding (opt-in per-run only)
# ---------------------------------------------------------------------------
# When the harness explicitly asks for a Linux sandbox assignment, an extra
# MCP server named ``fleet_sandbox`` is admitted into ``mcp.servers`` exactly
# once per run, exposing only the four pinned tool names below. The model sees
# them as ``fleet_sandbox__exec``/``fleet_sandbox__write_file``/etc. because
# OpenClaw prefixes ``<server>__<bare>`` per its native naming policy.
OPENCLAW_FLEET_SANDBOX_SERVER_NAME = "fleet_sandbox"
OPENCLAW_FLEET_SANDBOX_BARE_TOOL_NAMES: tuple[str, ...] = (
    "exec", "write_file", "read_file", "status",
)
OPENCLAW_FLEET_SANDBOX_MODEL_FACING_TOOL_NAMES: tuple[str, ...] = tuple(
    f"{OPENCLAW_FLEET_SANDBOX_SERVER_NAME}__{name}"
    for name in OPENCLAW_FLEET_SANDBOX_BARE_TOOL_NAMES
)
# The broker is owned by the same payload as this helper; pinning the script
# path through a stable on-disk location lets the materializer embed the
# SHA-256 hash and lets the broker refuse drift at startup. ``resolve()``
# is used to fix the path even when callers reach the helper from a CWD.
OPENCLAW_FLEET_SANDBOX_BROKER_RELATIVE_PATH = Path("sandbox_fleet_mcp.py")
# The pinned Python interpreter matches the project's toolchain
# (Python 3.13.x). ``pythonw.exe`` keeps the broker hidden; the harness
# always passes ``-I -B`` for an isolated, no-user-site process.
OPENCLAW_FLEET_SANDBOX_PIN_PYTHON = str(
    Path(sys.executable).with_name("pythonw.exe") if os.name == "nt" else Path(sys.executable)
)
# Environment keys the broker expects at startup. The model never sees
# these; the harness embeds them into the mcp.servers entry verbatim.
OPENCLAW_FLEET_SANDBOX_BROKER_ENV_KEYS: tuple[str, ...] = (
    "FLEET_SANDBOX_SESSION",
    "FLEET_SANDBOX_RUN_HOME",
    "FLEET_SANDBOX_CORE_SOURCE",
    "FLEET_SANDBOX_CORE_HASH",
    "FLEET_SANDBOX_IMAGE",
    "FLEET_SANDBOX_IMAGE_ID",
    "FLEET_SANDBOX_BROKER_HASH",
)
# The pinned immutable local sandbox core lives outside this payload. The
# materializer is the only authority that can decide to change this hash.
OPENCLAW_FLEET_SANDBOX_PIN_CORE_PATH = (
    Path(__file__).resolve().parents[4] / "global" / "tools" / "sandbox-terminal" / "sandbox_terminal.py"
)

OPENCLAW_FLEET_SANDBOX_PIN_CORE_HASH = (
    "790496b84847a5a35b09ef5e7b35bd1710b43cf9d908876db32460436daa190b"
)
OPENCLAW_FLEET_SANDBOX_PIN_IMAGE = "python:3.13.7-slim-bookworm"
OPENCLAW_FLEET_SANDBOX_PIN_IMAGE_ID = (
    "sha256:444ec9cb9b03c3be7c8b18d4a5e82e7cc908e2c0032c0a6e759338a48250b4de"
)

PROVIDER_MODEL_PATTERN = re.compile(
    r"^[a-z0-9][a-z0-9._-]{0,63}/[A-Za-z0-9][A-Za-z0-9._-]{0,63}(?::[A-Za-z0-9._-]{1,32})?$"
)


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------
def is_filesystem_link(path: Path) -> bool:
    metadata = path.lstat()
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction) and is_junction():
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise OpenClawRuntimeError(f"{label} must be a positive integer")
    return value


def _bounded_int(value: Any, label: str, *, minimum: int, maximum: int) -> int:
    parsed = _positive_int(value, label)
    if not minimum <= parsed <= maximum:
        raise OpenClawRuntimeError(
            f"{label} must be between {minimum} and {maximum}"
        )
    return parsed


def _loopback_url(value: Any, *, path: str, label: str) -> tuple[str, int]:
    normalized = str(value or "").rstrip("/")
    parsed = urlparse(normalized)
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
        raise OpenClawRuntimeError(
            f"{label} must be an explicit 127.0.0.1 http URL ending in {path}"
        )
    return normalized, int(parsed.port)


def safe_model_id(value: Any, *, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise OpenClawRuntimeError(f"{label} is required")
    return text


def safe_provider_model_id(value: Any, *, label: str) -> str:
    text = safe_model_id(value, label=label)
    if not PROVIDER_MODEL_PATTERN.fullmatch(text):
        raise OpenClawRuntimeError(
            f"{label} must be <provider>/<served-model> with safe characters"
        )
    return text


# ---------------------------------------------------------------------------
# Local vLLM runtime contract
# ---------------------------------------------------------------------------
def validate_local_runtime(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise OpenClawRuntimeError(
            "openclaw.local_runtime must be an object"
        )
    if raw.get("kind") != OPENCLAW_LOCAL_RUNTIME_KIND:
        raise OpenClawRuntimeError(
            f"openclaw.local_runtime.kind must be {OPENCLAW_LOCAL_RUNTIME_KIND}"
        )
    base_url, port = _loopback_url(
        raw.get("base_url"), path="/v1", label="openclaw.local_runtime.base_url"
    )
    if base_url != OPENCLAW_LOCAL_BASE_URL:
        raise OpenClawRuntimeError("OpenClaw local runtime must use pinned http://127.0.0.1:11440/v1")
    served_model = safe_model_id(
        raw.get("served_model_name"),
        label="openclaw.local_runtime.served_model_name",
    )
    if served_model != OPENCLAW_LOCAL_SERVED_MODEL:
        raise OpenClawRuntimeError(
            "openclaw.local_runtime.served_model_name must be pinned to "
            f"{OPENCLAW_LOCAL_SERVED_MODEL}"
        )
    provider = safe_model_id(
        raw.get("provider"), label="openclaw.local_runtime.provider"
    )
    if provider != OPENCLAW_LOCAL_PROVIDER:
        raise OpenClawRuntimeError(
            "openclaw.local_runtime.provider must be pinned to "
            f"{OPENCLAW_LOCAL_PROVIDER}"
        )
    state_id = str(raw.get("state_id", "")).strip()
    if not re.fullmatch(r"^[a-z0-9][a-z0-9.-]{2,63}$", state_id):
        raise OpenClawRuntimeError(
            "openclaw.local_runtime.state_id is invalid"
        )
    runtime_not_ready = raw.get("runtime_not_ready")
    if runtime_not_ready is True:
        # Fail-closed default: the shipped profile must stay on the
        # conservative 16384-token cap. No larger context is admissible while
        # the parent has not flipped the gate.
        context_window = _bounded_int(
            raw.get("context_window_tokens"),
            "openclaw.local_runtime.context_window_tokens",
            minimum=OPENCLAW_LOCAL_DEFAULT_CONTEXT_TOKENS,
            maximum=OPENCLAW_LOCAL_DEFAULT_CONTEXT_TOKENS,
        )
    elif runtime_not_ready is False:
        # Explicit parent activation. Allow bounded growth in [16384,
        # 524288]; the live probe in runtime_status / ensure_runtime still
        # verifies the bound against the running endpoint before the harness
        # admits a run.
        context_window = _bounded_int(
            raw.get("context_window_tokens"),
            "openclaw.local_runtime.context_window_tokens",
            minimum=OPENCLAW_LOCAL_MIN_CONTEXT_TOKENS,
            maximum=OPENCLAW_LOCAL_MAX_CONTEXT_TOKENS,
        )
    else:
        raise OpenClawRuntimeError(
            "openclaw.local_runtime.runtime_not_ready must be a boolean; the "
            "shipped profile uses true so Fleet refuses to advertise the "
            "11440 endpoint"
        )
    max_num_seqs = _bounded_int(
        raw.get("max_num_seqs"),
        "openclaw.local_runtime.max_num_seqs",
        minimum=1,
        maximum=1,
    )
    if bool(raw.get("externally_managed")) is not True:
        raise OpenClawRuntimeError(
            "OpenClaw local runtime must be externally_managed=true; Fleet "
            "never starts, stops, repairs, or rewrites the vLLM process."
        )
    if raw.get("provider_cost_pln") != 0:
        raise OpenClawRuntimeError(
            "Local OpenClaw lane provider_cost_pln must be exactly 0"
        )
    if raw.get("provider_cost_display") != "0 zł":
        raise OpenClawRuntimeError(
            "Local OpenClaw lane provider_cost_display must be '0 zł'"
        )
    return {
        "kind": OPENCLAW_LOCAL_RUNTIME_KIND,
        "base_url": base_url,
        "port": port,
        "provider": provider,
        "served_model_name": served_model,
        "runtime_provider_model": OPENCLAW_LOCAL_PROVIDER_MODEL,
        "state_id": state_id,
        "context_window_tokens": context_window,
        "max_num_seqs": max_num_seqs,
        "externally_managed": True,
        "provider_cost_pln": 0,
        "provider_cost_display": "0 zł",
        "runtime_not_ready": bool(runtime_not_ready),
    }


# ---------------------------------------------------------------------------
# Subscription fallback contract
# ---------------------------------------------------------------------------
def validate_muse_subscription(raw: dict[str, Any], *, require_verified: bool = True) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise OpenClawRuntimeError(
            "openclaw.subscription must be an object"
        )
    if raw.get("kind") != OPENCLAW_MUSE_RUNTIME_KIND:
        raise OpenClawRuntimeError(
            f"openclaw.subscription.kind must be {OPENCLAW_MUSE_RUNTIME_KIND}"
        )
    provider = safe_model_id(
        raw.get("provider"), label="openclaw.subscription.provider"
    )
    if provider != OPENCLAW_MUSE_SUBSCRIPTION_PROVIDER:
        raise OpenClawRuntimeError(
            "openclaw.subscription.provider must be pinned to "
            f"{OPENCLAW_MUSE_SUBSCRIPTION_PROVIDER}"
        )
    served_model = safe_model_id(
        raw.get("served_model_name"),
        label="openclaw.subscription.served_model_name",
    )
    if OPENCLAW_MUSE_FORBIDDEN_MARKER in re.sub(
        r"[^a-z0-9]+", "-", served_model.casefold()
    ):
        raise OpenClawRuntimeError(
            "Muse Contributor is categorically prohibited."
        )
    if served_model != OPENCLAW_MUSE_SUBSCRIPTION_MODEL:
        raise OpenClawRuntimeError(
            "openclaw.subscription.served_model_name must be pinned exactly "
            f"to {OPENCLAW_MUSE_SUBSCRIPTION_MODEL}; legacy 1.2 is refused."
        )
    if raw.get("transport") != "meta-subscription":
        raise OpenClawRuntimeError(
            "OpenClaw Muse fallback must use the meta-subscription transport; "
            "the metered Meta API is rejected by Owner policy."
        )
    if require_verified and raw.get("subscription_verified") is not True:
        raise OpenClawRuntimeError(
            "OpenClaw Muse fallback requires subscription_verified=true; "
            "until the parent proves the SUBSCRIPTION CLI backend is "
            "reachable, routing stays disabled."
        )
    return {
        "kind": OPENCLAW_MUSE_RUNTIME_KIND,
        "provider": provider,
        "served_model_name": served_model,
        "runtime_provider_model": OPENCLAW_MUSE_SUBSCRIPTION_PROVIDER_MODEL,
        "transport": "meta-subscription",
        "subscription_verified": raw.get("subscription_verified") is True,
    }


# ---------------------------------------------------------------------------
# Process command builder
# ---------------------------------------------------------------------------
def require_openclaw_native_executable(executable: str) -> None:
    if os.name != "nt":
        raise OpenClawRuntimeError(
            "OpenClaw native Windows transport requires Windows"
        )
    path = Path(executable)
    expected_directory = PINNED_OPENCLAW_BIN_DIRECTORY
    if (
        not path.is_absolute()
        or not path.is_file()
        or path.resolve().parent != expected_directory.resolve()
        or path.name != "openclaw.cmd"
    ):
        raise OpenClawRuntimeError(
            "OpenClaw must use its installed native Windows versioned "
            "executable at <install>/node_modules/.bin/openclaw.cmd; "
            "PATH lookup and shell bridges are refused."
        )
    if is_filesystem_link(path) or any(
        is_filesystem_link(parent) for parent in path.parents
    ):
        raise OpenClawRuntimeError(
            "OpenClaw executable path must not contain filesystem links"
        )


def build_command(
    *,
    executable: str,
    workspace: Path,
    state_dir: Path,
    run_config_path: Path,
    timeout_seconds: int,
    provider_model: str,
) -> tuple[list[str], dict[str, Any]]:
    """Construct the literal argv vector for ``openclaw agent exec``.

    The argv honours the contract from
    ``evidence-input/openclaw-agent-exec.mjs``:

    * ``agent exec`` is a literal two-token command (``agent`` and ``exec``
      are not nested subcommands — the upstream CLI treats them as a stable
      verb pair).
    * ``--config`` and the per-run JSON refuse to be combined with
      ``--isolated`` or ``--auth-env-only``; the harness always pins
      ``--config`` so credentials cannot be laundered through ambient state.
    * ``--state-dir`` is run-owned so OpenClaw never deletes it.
    * ``--deliver`` is never added; the harness owns receipts.
    * The prompt travels over stdin via ``--message-file -`` to keep the
      task out of argv and persisted process telemetry.
    """
    if not isinstance(timeout_seconds, int) or timeout_seconds <= 0:
        raise OpenClawRuntimeError("timeout_seconds must be a positive integer")
    safe_provider_model = safe_provider_model_id(
        provider_model, label="openclaw provider/model"
    )
    require_openclaw_native_executable(executable)
    if is_filesystem_link(Path(workspace)) or is_filesystem_link(Path(state_dir)):
        raise OpenClawRuntimeError(
            "OpenClaw workspace and state-dir must be plain, non-linked paths"
        )
    run_config_path = Path(run_config_path)
    if run_config_path.suffix.casefold() != ".json":
        raise OpenClawRuntimeError(
            "OpenClaw per-run config must be materialized as a .json file"
        )
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    command = [
        str(executable),
        "agent",
        "exec",
        "--message-file",
        "-",
        "--cwd",
        str(workspace.resolve()),
        "--config",
        str(run_config_path.resolve()),
        "--state-dir",
        str(state_dir.resolve()),
        "--model",
        safe_provider_model,
        "--timeout",
        str(timeout_seconds),
        "--json",
    ]
    metadata = {
        "argv_source": "evidence-input/openclaw-agent-exec.mjs",
        "max_message_bytes": PINNED_OPENCLAW_MAX_MESSAGE_BYTES,
        "ambient_fallback_disabled": True,
        "isolated_or_auth_env_only_disabled": True,
        "deliver_disabled": True,
    }
    return command, metadata


# ---------------------------------------------------------------------------
# Fleet/OpenClaw sandbox MCP materializer (opt-in per-run only)
# ---------------------------------------------------------------------------
def _path_is_link_free(path: Path) -> bool:
    return isinstance(path, Path) and all(
        not is_filesystem_link(item) for item in (path, *path.parents) if item.exists() or item.is_symlink()
    )


def _fleet_sandbox_broker_path() -> Path:
    path = Path(__file__).parent / OPENCLAW_FLEET_SANDBOX_BROKER_RELATIVE_PATH
    if not path.is_file() or not _path_is_link_free(path):
        raise OpenClawRuntimeError("sandbox broker missing or linked")
    return path.resolve()


def validate_fleet_sandbox_assignment(raw: Any) -> dict[str, Any]:
    base_keys = {"server_name", "enabled", "mode", "session", "run_home", "env"}
    derived_keys = {"broker_path", "broker_python", "broker_args"}
    if not isinstance(raw, dict) or set(raw) not in (base_keys, base_keys | derived_keys):
        raise OpenClawRuntimeError("sandbox assignment keys differ from the exact contract")
    if raw["server_name"] != OPENCLAW_FLEET_SANDBOX_SERVER_NAME or raw["enabled"] is not True or raw["mode"] != "implement":
        raise OpenClawRuntimeError("sandbox is implement-only and explicitly enabled")
    session = raw["session"]
    if not isinstance(session, str) or not re.fullmatch(r"fleet-[a-f0-9]{24}", session):
        raise OpenClawRuntimeError("invalid run-owned sandbox session")
    home = Path(raw["run_home"]) if isinstance(raw["run_home"], str) else Path()
    if not home.is_absolute() or not home.is_dir() or home.name != "fleet-sandbox-home" or not _path_is_link_free(home):
        raise OpenClawRuntimeError("sandbox home must be an existing unlinked run-owned directory")
    env = raw["env"]
    if not isinstance(env, dict) or set(env) != set(OPENCLAW_FLEET_SANDBOX_BROKER_ENV_KEYS) or not all(isinstance(x, str) and x for x in env.values()):
        raise OpenClawRuntimeError("sandbox environment differs from the exact allowlist")
    broker = _fleet_sandbox_broker_path()
    core = OPENCLAW_FLEET_SANDBOX_PIN_CORE_PATH
    if not core.is_file() or not _path_is_link_free(core) or hashlib.sha256(core.read_bytes()).hexdigest() != OPENCLAW_FLEET_SANDBOX_PIN_CORE_HASH:
        raise OpenClawRuntimeError("approved sandbox core missing, linked or changed")
    expected_env = {
        "FLEET_SANDBOX_SESSION": session,
        "FLEET_SANDBOX_RUN_HOME": str(home),
        "FLEET_SANDBOX_CORE_SOURCE": str(core),
        "FLEET_SANDBOX_CORE_HASH": OPENCLAW_FLEET_SANDBOX_PIN_CORE_HASH,
        "FLEET_SANDBOX_IMAGE": OPENCLAW_FLEET_SANDBOX_PIN_IMAGE,
        "FLEET_SANDBOX_IMAGE_ID": OPENCLAW_FLEET_SANDBOX_PIN_IMAGE_ID,
        "FLEET_SANDBOX_BROKER_HASH": hashlib.sha256(broker.read_bytes()).hexdigest(),
    }
    if env != expected_env:
        raise OpenClawRuntimeError("sandbox command/source/image/home/fingerprint pin mismatch")
    marker = home / ".fleet-sandbox-owned"
    expected_marker = {"session": session, "core_hash": OPENCLAW_FLEET_SANDBOX_PIN_CORE_HASH, "image_id": OPENCLAW_FLEET_SANDBOX_PIN_IMAGE_ID}
    try:
        if not _path_is_link_free(marker) or json.loads(marker.read_text(encoding="utf-8")) != expected_marker:
            raise ValueError("ownership mismatch")
    except (OSError, ValueError) as exc:
        raise OpenClawRuntimeError("sandbox ownership marker invalid") from exc
    result = {**raw, "env": dict(expected_env), "broker_path": str(broker), "broker_python": OPENCLAW_FLEET_SANDBOX_PIN_PYTHON, "broker_args": ["-I", "-B"]}
    if any(key in raw and raw[key] != result[key] for key in derived_keys):
        raise OpenClawRuntimeError("sandbox executable/argv/path overrides forbidden")
    return result


def build_fleet_sandbox_assignment(*, session: str, run_home: Path) -> dict[str, Any]:
    if not isinstance(session, str) or not re.fullmatch(r"fleet-[a-f0-9]{24}", session):
        raise OpenClawRuntimeError("invalid sandbox session")
    if not run_home.is_absolute() or run_home.name != "fleet-sandbox-home" or not _path_is_link_free(run_home):
        raise OpenClawRuntimeError("invalid sandbox home")
    run_home.mkdir(parents=True, exist_ok=True)
    marker = run_home / ".fleet-sandbox-owned"
    owner = {"session": session, "core_hash": OPENCLAW_FLEET_SANDBOX_PIN_CORE_HASH, "image_id": OPENCLAW_FLEET_SANDBOX_PIN_IMAGE_ID}
    if not _path_is_link_free(marker):
        raise OpenClawRuntimeError("linked sandbox ownership marker")
    if marker.exists():
        if not _path_is_link_free(marker) or json.loads(marker.read_text(encoding="utf-8")) != owner:
            raise OpenClawRuntimeError("existing sandbox home belongs to another session")
    else:
        marker.write_text(json.dumps(owner), encoding="utf-8")
    broker = _fleet_sandbox_broker_path()
    env = {
        "FLEET_SANDBOX_SESSION": session, "FLEET_SANDBOX_RUN_HOME": str(run_home),
        "FLEET_SANDBOX_CORE_SOURCE": str(OPENCLAW_FLEET_SANDBOX_PIN_CORE_PATH),
        "FLEET_SANDBOX_CORE_HASH": OPENCLAW_FLEET_SANDBOX_PIN_CORE_HASH,
        "FLEET_SANDBOX_IMAGE": OPENCLAW_FLEET_SANDBOX_PIN_IMAGE,
        "FLEET_SANDBOX_IMAGE_ID": OPENCLAW_FLEET_SANDBOX_PIN_IMAGE_ID,
        "FLEET_SANDBOX_BROKER_HASH": hashlib.sha256(broker.read_bytes()).hexdigest(),
    }
    return validate_fleet_sandbox_assignment({"server_name": OPENCLAW_FLEET_SANDBOX_SERVER_NAME, "enabled": True, "mode": "implement", "session": session, "run_home": str(run_home), "env": env})


def _build_fleet_sandbox_server_entry(assignment: dict[str, Any]) -> dict[str, Any]:
    route = validate_fleet_sandbox_assignment(assignment)
    return {"transport": "stdio", "command": route["broker_python"],
            "args": [*route["broker_args"], route["broker_path"]],
            "cwd": route["run_home"], "env": dict(route["env"]),
            "enabled": True, "connectionTimeoutMs": 10000, "requestTimeoutMs": 130000,
            "toolFilter": {"include": list(OPENCLAW_FLEET_SANDBOX_BARE_TOOL_NAMES)}}


# ---------------------------------------------------------------------------
# Per-run JSON config builder (fresh, ambient-free)
# ---------------------------------------------------------------------------
def build_run_config(
    *,
    mode: str,
    workspace: Path,
    context_window_tokens: int | None = None,
    chrome_assignment: dict[str, Any] | None = None,
    chrome_plugin_path: Path | None = None,
    fleet_sandbox_assignment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if mode not in {"advisory", "review", "implement"}:
        raise OpenClawRuntimeError(f"OpenClaw mode is invalid: {mode}")
    allowed = (
        IMPLEMENT_ALLOWED_TOOLS if mode == "implement" else REVIEW_ALLOWED_TOOLS
    )
    denied = (
        IMPLEMENT_DENIED_TOOLS if mode == "implement" else REVIEW_DENIED_TOOLS
    )
    chrome_tool_names: tuple[str, ...] = ()
    plugin_payload: dict[str, Any] = {"enabled": False}
    if chrome_assignment is not None:
        if chrome_plugin_path is None or not chrome_plugin_path.is_absolute():
            raise OpenClawRuntimeError("OpenClaw managed Chrome requires an absolute per-run plugin path")
        try:
            import fleet_chrome_integration as chrome
            route = chrome.validate_fleet_assignment(chrome_assignment, "openclaw")
            if route["mode"] != mode or "budget_state" not in route:
                raise ValueError("assignment mode or run budget mismatch")
            chrome_tool_names = tuple("fleet_chrome_" + item["name"].removeprefix("chrome_")
                                      for item in chrome.tool_specs(read_only=mode == "review"))
        except (ImportError, ValueError) as exc:
            raise OpenClawRuntimeError("OpenClaw managed Chrome assignment is invalid") from exc
        plugin_payload = {
            "enabled": True,
            "allow": ["fleet-chrome-relay"],
            "load": {"paths": [str(chrome_plugin_path.resolve())]},
            "entries": {"fleet-chrome-relay": {"enabled": True}},
        }
    sandbox_tools: tuple[str, ...] = ()
    sandbox_servers: dict[str, Any] = {}
    if fleet_sandbox_assignment is not None:
        if mode != "implement":
            raise OpenClawRuntimeError("sandbox assignment forbidden in read-only mode")
        route = validate_fleet_sandbox_assignment(fleet_sandbox_assignment)
        sandbox_tools = OPENCLAW_FLEET_SANDBOX_MODEL_FACING_TOOL_NAMES
        sandbox_servers = {OPENCLAW_FLEET_SANDBOX_SERVER_NAME: _build_fleet_sandbox_server_entry(route)}
    if context_window_tokens is None:
        context_window_tokens = OPENCLAW_LOCAL_DEFAULT_CONTEXT_TOKENS
    # Always bound the configured window to the local lane contract; callers
    # that need a larger bound must have already proven the runtime via
    # validate_local_runtime, which clamps to the same [16384, 524288] range.
    bounded_context_window = _bounded_int(
        context_window_tokens,
        "context_window_tokens",
        minimum=OPENCLAW_LOCAL_MIN_CONTEXT_TOKENS,
        maximum=OPENCLAW_LOCAL_MAX_CONTEXT_TOKENS,
    )
    return {
        # These keys and nesting match OpenClaw 2026.9.4's native config
        # schema.  The config is deliberately fresh for every run.
        "telemetry": {"enabled": False},
        "update": {"checkOnStart": False},
        "agents": {
            "defaults": {
                "workspace": str(workspace.resolve()),
                "skipBootstrap": True,
                "heartbeat": {"every": "0m"},
                "model": {
                    "primary": OPENCLAW_LOCAL_PROVIDER_MODEL,
                    "fallbacks": [],
                },
                "sandbox": {"mode": "off"},
            },
        },
        "models": {
            "providers": {
                OPENCLAW_LOCAL_PROVIDER: {
                    "baseUrl": OPENCLAW_LOCAL_BASE_URL,
                    "api": "openai-completions",
                    "apiKey": "",
                    "models": [
                        {
                            "id": OPENCLAW_LOCAL_SERVED_MODEL,
                            "name": "Official Qwen3.8-27B (runtime verification pending)",
                            "reasoning": True,
                            "input": ["text"],
                            "contextWindow": bounded_context_window,
                            "maxTokens": OPENCLAW_LOCAL_MAX_OUTPUT_TOKENS,
                            "compat": {
                                "thinkingFormat": "qwen-chat-template",
                            },
                        }
                    ],
                }
            }
        },
        "tools": {
            "profile": "full",
            "allow": [*allowed, *chrome_tool_names, *sandbox_tools],
            "deny": list(denied),
            "fs": {"workspaceOnly": True},
            "exec": {"mode": "deny"},
            "web": {
                "search": {"enabled": False},
                "fetch": {"enabled": False},
            },
            "swarm": False,
        },
        "plugins": plugin_payload,
        "mcp": {"servers": sandbox_servers},
        "skills": {
            "allowBundled": [],
            "load": {"watch": False, "extraDirs": []},
        },
    }


def materialize_run_config(
    *,
    mode: str,
    workspace: Path,
    run_dir: Path,
    context_window_tokens: int | None = None,
    chrome_assignment: dict[str, Any] | None = None,
    fleet_sandbox_enabled: bool = False,
) -> Path:
    if not isinstance(fleet_sandbox_enabled, bool):
        raise OpenClawRuntimeError("sandbox profile opt-in must be boolean")
    fleet_sandbox_assignment = None
    if fleet_sandbox_enabled and mode == "implement":
        import secrets
        fleet_sandbox_assignment = build_fleet_sandbox_assignment(
            session="fleet-" + secrets.token_hex(12), run_home=run_dir.resolve() / "fleet-sandbox-home")
    chrome_plugin_path = None
    if chrome_assignment is not None:
        chrome_plugin_path = run_dir / "fleet-chrome-relay-plugin"
        _materialize_fleet_chrome_plugin(chrome_assignment, chrome_plugin_path)
    payload = build_run_config(
        mode=mode,
        workspace=workspace,
        context_window_tokens=context_window_tokens,
        chrome_assignment=chrome_assignment,
        chrome_plugin_path=chrome_plugin_path,
        fleet_sandbox_assignment=fleet_sandbox_assignment,
    )
    validate_run_config_payload(payload, mode=mode, chrome_assignment=chrome_assignment, chrome_plugin_path=chrome_plugin_path, fleet_sandbox_assignment=fleet_sandbox_assignment)
    serialized = json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2) + "\n"
    config_path = run_dir / f"openclaw.runtime-config.{mode}.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        serialized,
        encoding="utf-8",
        newline="\n",
    )
    # agent-exec intentionally removes OPENCLAW_CONFIG_PATH after loading the
    # base config and then scopes resolution to OPENCLAW_STATE_DIR.  Mirror the
    # same owned payload at the native state filename so provider/plugin code
    # cannot fall back to a user's ambient ~/.openclaw/openclaw.json.
    state_config_path = run_dir / "openclaw-state" / "openclaw.json"
    state_config_path.parent.mkdir(parents=True, exist_ok=True)
    state_config_path.write_text(serialized, encoding="utf-8", newline="\n")
    return config_path


def validate_run_config_payload(payload: dict[str, Any], *, mode: str | None = None, chrome_assignment: dict[str, Any] | None = None, chrome_plugin_path: Path | None = None, fleet_sandbox_assignment: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate the subset of the native OpenClaw 2026.9.4 contract we own.

    Native ``openclaw config validate --json`` remains the schema authority;
    this guard catches policy regressions before a payload is materialized.
    In particular, it rejects the former helper's invented root keys and
    Claude tool names so callers cannot mistake a locally shaped payload for
    an OpenClaw config.
    """
    if mode is not None and mode not in {"advisory", "review", "implement"}:
        raise OpenClawRuntimeError("OpenClaw validator mode is invalid")
    if mode is None and chrome_assignment is not None:
        raise OpenClawRuntimeError("OpenClaw managed Chrome validation requires an explicit mode")
    if not isinstance(payload, dict) or not payload:
        raise OpenClawRuntimeError("OpenClaw runtime config must be a JSON object")
    invented_root_keys = {
        "schema_version",
        "providers",
        "review_only_blockers",
    }
    present_invented = sorted(invented_root_keys & set(payload))
    if present_invented:
        raise OpenClawRuntimeError(
            "OpenClaw runtime config contains unsupported root keys: "
            + ", ".join(present_invented)
        )
    telemetry = payload.get("telemetry")
    if not isinstance(telemetry, dict) or telemetry.get("enabled") is not False:
        raise OpenClawRuntimeError(
            "OpenClaw runtime config must set telemetry.enabled=false"
        )
    update = payload.get("update")
    if not isinstance(update, dict) or update.get("checkOnStart") is not False:
        raise OpenClawRuntimeError(
            "OpenClaw runtime config must set update.checkOnStart=false"
        )
    agents = payload.get("agents")
    defaults = agents.get("defaults") if isinstance(agents, dict) else None
    if not isinstance(defaults, dict):
        raise OpenClawRuntimeError(
            "OpenClaw runtime config.agents.defaults is missing"
        )
    workspace = defaults.get("workspace")
    if not isinstance(workspace, str) or not workspace.strip():
        raise OpenClawRuntimeError(
            "OpenClaw runtime config requires an owned workspace"
        )
    if defaults.get("skipBootstrap") is not True:
        raise OpenClawRuntimeError(
            "OpenClaw runtime config must set agents.defaults.skipBootstrap=true"
        )
    if defaults.get("heartbeat") != {"every": "0m"}:
        raise OpenClawRuntimeError(
            "OpenClaw runtime config must disable heartbeat work"
        )
    model = defaults.get("model")
    if not isinstance(model, dict):
        raise OpenClawRuntimeError(
            "OpenClaw runtime config.agents.defaults.model is missing"
        )
    if model.get("primary") != OPENCLAW_LOCAL_PROVIDER_MODEL:
        raise OpenClawRuntimeError(
            "OpenClaw runtime config model must be pinned exactly to "
            f"{OPENCLAW_LOCAL_PROVIDER_MODEL}"
        )
    if model.get("fallbacks") != []:
        raise OpenClawRuntimeError(
            "OpenClaw runtime config must not configure model fallbacks"
        )
    sandbox = defaults.get("sandbox")
    if not isinstance(sandbox, dict) or sandbox.get("mode") != "off":
        raise OpenClawRuntimeError(
            "OpenClaw runtime config must use the explicit native sandbox mode"
        )
    models = payload.get("models")
    providers = models.get("providers") if isinstance(models, dict) else None
    if not isinstance(providers, dict):
        raise OpenClawRuntimeError(
            "OpenClaw runtime config.models.providers is missing"
        )
    provider = providers.get(OPENCLAW_LOCAL_PROVIDER)
    if not isinstance(provider, dict):
        raise OpenClawRuntimeError(
            "OpenClaw runtime config is missing the pinned vllm provider"
        )
    if provider.get("baseUrl") != OPENCLAW_LOCAL_BASE_URL:
        raise OpenClawRuntimeError(
            "OpenClaw runtime config vllm.baseUrl must remain on loopback 11440"
        )
    if provider.get("api") != "openai-completions":
        raise OpenClawRuntimeError(
            "OpenClaw runtime config vllm.api must be openai-completions"
        )
    model_defs = provider.get("models")
    if not isinstance(model_defs, list) or len(model_defs) != 1:
        raise OpenClawRuntimeError(
            "OpenClaw runtime config vllm.models must contain one pinned model"
        )
    model_def = model_defs[0]
    if not isinstance(model_def, dict) or model_def.get("id") != OPENCLAW_LOCAL_SERVED_MODEL:
        raise OpenClawRuntimeError(
            "OpenClaw runtime config model id must be pinned exactly to "
            f"{OPENCLAW_LOCAL_SERVED_MODEL}"
        )
    tools = payload.get("tools")
    if not isinstance(tools, dict):
        raise OpenClawRuntimeError("OpenClaw runtime config.tools is missing")
    if tools.get("profile") != "full":
        raise OpenClawRuntimeError("OpenClaw runtime config must explicitly set profile=full with exact allow/deny lists")
    unsupported_tool_keys = {"network", "subagents"}
    present_unsupported_tools = sorted(unsupported_tool_keys & set(tools))
    if present_unsupported_tools:
        raise OpenClawRuntimeError(
            "OpenClaw runtime config.tools contains unsupported keys: "
            + ", ".join(present_unsupported_tools)
        )
    fs = tools.get("fs")
    if not isinstance(fs, dict) or fs.get("workspaceOnly") is not True:
        raise OpenClawRuntimeError(
            "OpenClaw runtime config must enforce fs.workspaceOnly=true"
        )
    if tools.get("swarm") is not False:
        raise OpenClawRuntimeError("OpenClaw runtime config must keep swarm disabled")
    exec_config = tools.get("exec")
    if not isinstance(exec_config, dict) or exec_config.get("mode") != "deny":
        raise OpenClawRuntimeError(
            "OpenClaw runtime config must keep shell exec denied"
        )
    web = tools.get("web")
    if (
        not isinstance(web, dict)
        or web.get("search") != {"enabled": False}
        or web.get("fetch") != {"enabled": False}
    ):
        raise OpenClawRuntimeError(
            "OpenClaw runtime config must disable web search and fetch"
        )
    allowed_raw = tools.get("allow")
    denied_raw = tools.get("deny")
    if not isinstance(allowed_raw, list) or not isinstance(denied_raw, list):
        raise OpenClawRuntimeError(
            "OpenClaw runtime config tool allow/deny must be lists"
        )
    if not all(isinstance(tool, str) for tool in (*allowed_raw, *denied_raw)):
        raise OpenClawRuntimeError(
            "OpenClaw runtime config tool allow/deny entries must be strings"
        )
    allowed = set(allowed_raw)
    denied = set(denied_raw)
    chrome_tool_names: set[str] = set()
    if chrome_assignment is not None:
        if chrome_plugin_path is None or not chrome_plugin_path.is_absolute():
            raise OpenClawRuntimeError("OpenClaw managed Chrome plugin path is missing")
        try:
            import fleet_chrome_integration as chrome
            route = chrome.validate_fleet_assignment(chrome_assignment, "openclaw")
            if route["mode"] != mode:
                raise ValueError("assignment mode mismatch")
            chrome_tool_names = {"fleet_chrome_" + item["name"].removeprefix("chrome_")
                                 for item in chrome.tool_specs(read_only=mode == "review")}
        except (ImportError, ValueError) as exc:
            raise OpenClawRuntimeError("OpenClaw managed Chrome assignment is invalid") from exc
    sandbox_tools: set[str] = set()
    expected_mcp = {"servers": {}}
    if fleet_sandbox_assignment is not None:
        if mode != "implement":
            raise OpenClawRuntimeError("sandbox validation requires explicit implement mode")
        route = validate_fleet_sandbox_assignment(fleet_sandbox_assignment)
        sandbox_tools = set(OPENCLAW_FLEET_SANDBOX_MODEL_FACING_TOOL_NAMES)
        expected_mcp = {"servers": {OPENCLAW_FLEET_SANDBOX_SERVER_NAME: _build_fleet_sandbox_server_entry(route)}}
    unsupported_tools = sorted(
        (allowed | denied) - set(OPENCLAW_CORE_TOOL_NAMES) - chrome_tool_names - sandbox_tools
    )
    if unsupported_tools:
        raise OpenClawRuntimeError(
            "OpenClaw runtime config contains non-native tool names: "
            + ", ".join(unsupported_tools)
        )
    if allowed & denied:
        raise OpenClawRuntimeError(
            "OpenClaw runtime config cannot simultaneously allow and "
            "deny the same native tool"
        )
    if mode is None:
        # Legacy callers validated code-only payloads without a mode argument.
        # Infer only the existing exact policies; Chrome never uses this path.
        mode = ("implement" if allowed == set(IMPLEMENT_ALLOWED_TOOLS)
                and denied == set(IMPLEMENT_DENIED_TOOLS) else "review")
    # Advisory and review deliberately share the existing read-only tool policy.
    mode_allowed = set(IMPLEMENT_ALLOWED_TOOLS if mode == "implement" else REVIEW_ALLOWED_TOOLS)
    mode_denied = set(IMPLEMENT_DENIED_TOOLS if mode == "implement" else REVIEW_DENIED_TOOLS)
    if allowed != mode_allowed | chrome_tool_names | sandbox_tools or denied != mode_denied:
        raise OpenClawRuntimeError("OpenClaw tool allow/deny lists do not match the exact mode policy")
    if set(OPENCLAW_CORE_TOOL_NAMES) - denied - allowed:
        raise OpenClawRuntimeError(
            "OpenClaw runtime config must account for every native core tool"
        )
    plugins = payload.get("plugins") or {}
    if chrome_assignment is None:
        if plugins != {"enabled": False}:
            raise OpenClawRuntimeError("OpenClaw runtime config must disable plugins with plugins.enabled=false")
    else:
        expected_plugins = {
            "enabled": True,
            "allow": ["fleet-chrome-relay"],
            "load": {"paths": [str(chrome_plugin_path.resolve())]},
            "entries": {"fleet-chrome-relay": {"enabled": True}},
        }
        if plugins != expected_plugins or not chrome_tool_names:
            raise OpenClawRuntimeError("OpenClaw runtime config plugin allowlist is not exact")
    mcp = payload.get("mcp") or {}
    if mcp != expected_mcp:
        raise OpenClawRuntimeError(
            "OpenClaw runtime config MCP map differs from its exact declared assignment"
        )
    skills = payload.get("skills") or {}
    if skills != {
        "allowBundled": [],
        "load": {"watch": False, "extraDirs": []},
    }:
        raise OpenClawRuntimeError(
            "OpenClaw runtime config must disable bundled and extra skill loading"
        )
    return payload


def _materialize_fleet_chrome_plugin(assignment: dict[str, Any], plugin_dir: Path) -> None:
    import fleet_chrome_integration as chrome
    route = chrome.validate_fleet_assignment(assignment, "openclaw")
    if "budget_state" not in route or plugin_dir.exists() or plugin_dir.is_symlink():
        raise OpenClawRuntimeError("OpenClaw per-run Chrome plugin path or budget is invalid")
    plugin_dir.mkdir(parents=False, exist_ok=False)
    names = tuple("fleet_chrome_" + item["name"].removeprefix("chrome_")
                  for item in chrome.tool_specs(read_only=route["mode"] == "review"))
    specs = []
    for item in chrome.tool_specs(read_only=route["mode"] == "review"):
        schema = json.loads(json.dumps(item["inputSchema"]))
        schema.get("properties", {}).pop("request_id", None)
        specs.append({"name": "fleet_chrome_" + item["name"].removeprefix("chrome_"),
                      "description": item["description"], "schema": schema})
    runtime = Path(__file__).resolve().with_name("fleet_chrome_integration.py")
    if not runtime.is_file() or is_filesystem_link(runtime):
        raise OpenClawRuntimeError("OpenClaw Chrome proxy helper is unavailable")
    cfg = {"python": route["python"], "helper": str(runtime), "mode": route["mode"],
           "budget_state": route["budget_state"], "budget_nonce": route["budget_nonce"],
           "budget_limit": route["budget_limit"]}
    cfg_literal = json.dumps(json.dumps(cfg, ensure_ascii=True), ensure_ascii=True)
    specs_literal = json.dumps(json.dumps(specs, ensure_ascii=True), ensure_ascii=True)
    # The pinned synchronous Jiti loader cannot execute top-level await.
    sdk_entry = (PINNED_OPENCLAW_INSTALL_ROOT / "node_modules/openclaw/dist/plugin-sdk/plugin-entry.js").as_uri()
    typebox_entry = (PINNED_OPENCLAW_INSTALL_ROOT / "node_modules/typebox/build/index.mjs").as_uri()
    source = f'''import {{ spawn }} from "node:child_process";
import {{ definePluginEntry }} from {json.dumps(sdk_entry)};
import {{ Type }} from {json.dumps(typebox_entry)};
const cfg = JSON.parse({cfg_literal});
const specs = JSON.parse({specs_literal});
const MAX = 65536;
let child = null, buffer = "", failed = false, tail = Promise.resolve(), serial = 0;
let pending = null;
function stop(error) {{ failed = true; if (child) child.kill(); if (pending) {{ clearTimeout(pending.timer); pending.reject(error); pending = null; }} }}
function start() {{
  if (failed) throw new Error("Fleet Chrome relay unavailable");
  if (child) return;
  const env = {{}};
  for (const key of ["SYSTEMROOT", "WINDIR", "TEMP", "TMP"]) if (process.env[key]) env[key] = process.env[key];
  child = spawn(cfg.python, ["-B", cfg.helper, "--managed-chrome-jsonl", "--mode", cfg.mode,
    "--budget-state", cfg.budget_state, "--budget-nonce", cfg.budget_nonce,
    "--budget-limit", String(cfg.budget_limit)], {{ shell: false, windowsHide: true, stdio: ["pipe", "pipe", "ignore"], env }});
  child.stdout.on("data", chunk => {{
    buffer += chunk.toString("utf8");
    if (buffer.length > MAX) return stop(new Error("Fleet Chrome reply exceeded bound"));
    const i = buffer.indexOf("\\n");
    if (i < 0) return;
    if (i !== buffer.length - 1 || !pending) return stop(new Error("Fleet Chrome stream desynchronized"));
    const line = buffer.slice(0, i); buffer = "";
    try {{
      const response = JSON.parse(line);
      if (!response || Object.keys(response).sort().join(",") !== "request_id,result" || response.request_id !== pending.id)
        throw new Error("mismatched relay request id");
      const resolve = pending.resolve; clearTimeout(pending.timer); pending = null; resolve(response.result);
    }} catch (error) {{ stop(error); }}
  }});
  child.on("error", error => stop(error));
  child.on("exit", code => {{ if (code !== 0 || pending) stop(new Error("Fleet Chrome relay exited")); }});
}}
function invoke(name, args, id) {{
  start();
  if (pending) return Promise.reject(new Error("Fleet Chrome proxy is already busy"));
  return new Promise((resolve, reject) => {{
    pending = {{ id, resolve, reject, timer: setTimeout(() => stop(new Error("Fleet Chrome relay timeout")), 20000) }};
    const line = JSON.stringify({{ request_id: id, name, arguments: args }}) + "\\n";
    if (Buffer.byteLength(line, "utf8") > MAX) return stop(new Error("Fleet Chrome request exceeded bound"));
    child.stdin.write(line, "utf8", error => {{ if (error) stop(error); }});
  }});
}}
function queued(name, args, id) {{ const next = tail.then(() => invoke(name, args, id)); tail = next.catch(() => {{}}); return next; }}
process.once("exit", () => {{ if (child) child.kill(); }});
export default definePluginEntry({{
  id: "fleet-chrome-relay", name: "Fleet managed Chrome relay",
  description: "Fixed semantic managed Chrome tools for this Fleet run.",
  register(api) {{
    for (const spec of specs) api.registerTool({{
      name: spec.name, description: spec.description, parameters: Type.Unsafe(spec.schema),
      async execute(callId, params) {{
        const id = typeof callId === "string" && /^[A-Za-z0-9._:-]{{1,128}}$/.test(callId)
          ? callId : "fleet-" + String(++serial);
        const result = await queued(spec.name, params, id);
        return {{ content: [{{ type: "text", text: JSON.stringify(result) }}], details: result }};
      }}
    }});
  }}
}});
'''
    manifest = {"id": "fleet-chrome-relay", "name": "Fleet managed Chrome relay",
                "description": "Per-run fixed semantic Chrome tools.", "categories": ["tools"],
                "contracts": {"tools": list(names)}, "activation": {"onStartup": True},
                "configSchema": {"type": "object", "additionalProperties": False}}
    package = {"name": "fleet-chrome-relay", "version": "0.0.1", "type": "module",
               "openclaw": {"extensions": ["./index.mjs"]}}
    (plugin_dir / "index.mjs").write_text(source, encoding="utf-8", newline="\n")
    (plugin_dir / "openclaw.plugin.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (plugin_dir / "package.json").write_text(json.dumps(package, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# State directory ownership (parent owns; harness never deletes)
# ---------------------------------------------------------------------------
def acquire_state_dir(run_dir: Path, *, label: str) -> Path:
    state_dir = run_dir / "openclaw-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    sentinel = state_dir / ".fleet-owned"
    if not sentinel.exists():
        sentinel.write_text(
            f"owner=fleet;label={label};created_unix={int(__import__('time').time())}\n",
            encoding="utf-8",
            newline="\n",
        )
    return state_dir


def assert_run_owned_state_dir(state_dir: Path) -> None:
    if is_filesystem_link(state_dir):
        raise OpenClawRuntimeError(
            f"OpenClaw state-dir is a link: {state_dir}"
        )
    sentinel = state_dir / ".fleet-owned"
    if not sentinel.is_file():
        raise OpenClawRuntimeError(
            f"OpenClaw state-dir lacks fleet ownership marker: {state_dir}"
        )
    owner_line = sentinel.read_text(encoding="utf-8", errors="replace").strip()
    if not owner_line.startswith("owner=fleet"):
        raise OpenClawRuntimeError(
            f"OpenClaw state-dir is not Fleet-owned: {state_dir}"
        )


def transcript_tool_call_count(state_dir: Path) -> int | None:
    """Count persisted tool calls in the pinned OpenClaw 2026.9.4 run DB.

    The headless JSON envelope arrives only after the run, so a parent cannot
    stop a growing tool chain by scanning stdout. OpenClaw writes each turn to
    this run-owned SQLite transcript while it works. Return None only before
    the database exists; a changed schema or unreadable database is a failed
    budget monitor, never an assumed zero.
    """
    assert_run_owned_state_dir(state_dir)
    db = state_dir / "agents" / "main" / "agent" / "openclaw-agent.sqlite"
    if not db.exists():
        return None
    if is_filesystem_link(db) or not db.resolve().is_relative_to(state_dir.resolve()):
        raise OpenClawRuntimeError("OpenClaw transcript DB escaped the run-owned state dir")
    try:
        with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)) as conn:
            version = conn.execute(
                "SELECT schema_version, app_version FROM schema_meta WHERE role='agent'"
            ).fetchone()
            if version != (19, "2026.9.4"):
                raise OpenClawRuntimeError(
                    "OpenClaw transcript schema/version changed; tool budget cannot be verified"
                )
            row = conn.execute(
                "SELECT count(*) FROM transcript_events e, "
                "json_each(e.event_json, '$.message.content') x "
                "WHERE json_extract(e.event_json, '$.message.role')='assistant' "
                "AND json_extract(x.value, '$.type')='toolCall'"
            ).fetchone()
            return int(row[0]) if row is not None else 0
    except sqlite3.Error as exc:
        raise OpenClawRuntimeError(
            "OpenClaw transcript DB cannot be read for tool-budget enforcement"
        ) from exc


# ---------------------------------------------------------------------------
# Environment hardening
# ---------------------------------------------------------------------------
def harden_openclaw_environment(environment: dict[str, str]) -> dict[str, str]:
    """Return a child environment with ambient OpenClaw controls removed.

    The installed CLI has many version-specific ``OPENCLAW_*`` controls for
    roots, credentials, gateways, plugins, and shell-env loading.  Removing
    the complete prefix is safer than maintaining a pretend denylist and does
    not add unsupported environment controls.  The adapter supplies owned
    config/state paths through its argv and the upstream agent-exec wrapper.
    """
    return {
        key: value
        for key, value in environment.items()
        if not key.upper().startswith("OPENCLAW_")
    }


def assert_no_ambient_openclaw_environment(
    environment: dict[str, str],
) -> dict[str, str]:
    blocked = sorted(
        key for key in environment if key.upper().startswith("OPENCLAW_")
    )
    if blocked:
        raise OpenClawRuntimeError(
            "OpenClaw ambient override present: " + ", ".join(blocked)
        )
    return dict(environment)


# ---------------------------------------------------------------------------
# Exact argv guard for ``--config`` / ``--isolated`` / ``--auth-env-only``
# ---------------------------------------------------------------------------
def refuse_unsafe_argv_combination(argv: Iterable[str]) -> None:
    """Refuse the contract-banned flag combinations up-front."""
    sequence = tuple(argv)
    flags = set()
    for token in sequence:
        if token.startswith("--"):
            flags.add(token.split("=", 1)[0])
    if "--config" in flags and ("--isolated" in flags or "--auth-env-only" in flags):
        raise OpenClawRuntimeError(
            "--config cannot be combined with --isolated or --auth-env-only"
        )
    if "--deliver" in flags:
        raise OpenClawRuntimeError(
            "OpenClaw --deliver is forbidden; Fleet owns the receipt and "
            "handoff gating."
        )


# ---------------------------------------------------------------------------
# Envelope parsing (read-only stdout JSON contract).
# ---------------------------------------------------------------------------
def envelope_to_result(text: str) -> dict[str, Any]:
    """Project the OpenClaw stdout envelope into a Fleet result.

    Schema reference: ``evidence-input/openclaw-agent-exec.mjs`` and
    CONTEXT.md §5. The envelope shape is::

        {
          "ok": true | false,
          "status": "ok" | "error" | "timeout",
          "final": "...",
          "payloads": [...],
          "usage"?: {...},
          "costUsd"?: number,
          "model": "<exact>",
          "provider": "<exact>",
          "sessionId": "<uuid>",
          "error"?: {"message": "...", "kind": "..."}
        }

    Numeric ``usage`` is absent means **unknown**, not zero. The harness
    preserves that semantic so the parent can bill against ``usage.status``.
    """
    raw = text.strip() if isinstance(text, str) else ""
    if not raw:
        return {
            "ok": False,
            "status": "error",
            "final": "",
            "payloads": [],
            "usage": None,
            "provider": None,
            "model": None,
            "sessionId": "",
            "error": {
                "message": "OpenClaw produced no stdout envelope",
                "kind": "empty_envelope",
            },
            "usage_unknown": True,
        }
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {
            "ok": False,
            "status": "error",
            "final": "",
            "payloads": [],
            "usage": None,
            "provider": None,
            "model": None,
            "sessionId": "",
            "error": {
                "message": f"Invalid OpenClaw envelope JSON: {exc}",
                "kind": "invalid_envelope",
            },
            "usage_unknown": True,
        }
    if not isinstance(envelope, dict):
        return {
            "ok": False,
            "status": "error",
            "final": "",
            "payloads": [],
            "usage": None,
            "provider": None,
            "model": None,
            "sessionId": "",
            "error": {
                "message": "OpenClaw envelope must be a JSON object",
                "kind": "invalid_envelope",
            },
            "usage_unknown": True,
        }
    status = str(envelope.get("status", "error")).casefold()
    if status not in {"ok", "error", "timeout"}:
        status = "error"
    final_text = envelope.get("final", "") or ""
    if not isinstance(final_text, str):
        final_text = str(final_text)
    payloads = envelope.get("payloads")
    if not isinstance(payloads, list):
        payloads = []
    provider = envelope.get("provider")
    if provider is not None and not isinstance(provider, str):
        provider = str(provider)
    model = envelope.get("model")
    if model is not None and not isinstance(model, str):
        model = str(model)
    session_id = envelope.get("sessionId", "") or ""
    if not isinstance(session_id, str):
        session_id = str(session_id)
    usage_raw = envelope.get("usage")
    usage_unknown = not isinstance(usage_raw, dict)
    usage = usage_raw if isinstance(usage_raw, dict) else None
    error_payload = envelope.get("error")
    if not isinstance(error_payload, dict):
        error_payload = None
    if status == "ok" and not final_text and not payloads:
        status = "error"
        error_payload = {
            "message": "OpenClaw reported ok without a final text or payload",
            "kind": "empty_successful_envelope",
        }
    return {
        "ok": status == "ok",
        "status": status,
        "final": final_text,
        "payloads": payloads,
        "usage": usage,
        "provider": provider,
        "model": model,
        "sessionId": session_id,
        "error": error_payload,
        "usage_unknown": usage_unknown,
        "costUsd": envelope.get("costUsd"),
    }


def identity_check(
    *,
    expected_provider: str,
    expected_model: str,
    envelope_result: dict[str, Any],
) -> dict[str, Any]:
    """Verify the envelope matches the requested CLI identity.

    CONTEXT.md §5 says: validate the *exact* runtime provider+model, never
    infer from assistant text. A missing or mismatched envelope is
    ``"mismatch"``; a structurally valid envelope with an unknown
    ``usage`` field is ``"verified_unknown_usage"`` so callers can
    distinguish "ok but no usage reported" from "ok and billed".
    """
    expected_provider = safe_model_id(
        expected_provider, label="expected provider"
    )
    expected_model = safe_model_id(expected_model, label="expected model")
    observed_provider = envelope_result.get("provider")
    observed_model = envelope_result.get("model")
    if (
        not isinstance(observed_provider, str)
        or not observed_provider.strip()
        or not isinstance(observed_model, str)
        or not observed_model.strip()
    ):
        return {
            "status": "mismatch",
            "expected_provider": expected_provider,
            "expected_model": expected_model,
            "observed_provider": observed_provider,
            "observed_model": observed_model,
            "error": (
                "OpenClaw envelope is missing provider or model; "
                "exact identity cannot be verified"
            ),
        }
    providers_match = observed_provider.casefold() == expected_provider.casefold()
    models_match = observed_model.strip() == expected_model.strip()
    if not (providers_match and models_match):
        return {
            "status": "mismatch",
            "expected_provider": expected_provider,
            "expected_model": expected_model,
            "observed_provider": observed_provider,
            "observed_model": observed_model,
            "error": (
                "OpenClaw envelope provider/model does not match the requested "
                "identity; refuse to claim the runtime reported the contract"
            ),
        }
    if envelope_result.get("status") != "ok":
        return {
            "status": "mismatch",
            "expected_provider": expected_provider,
            "expected_model": expected_model,
            "observed_provider": observed_provider,
            "observed_model": observed_model,
            "error": "OpenClaw reported a non-ok status with the expected "
            "provider/model; treating as mismatch",
        }
    return {
        "status": (
            "verified_unknown_usage"
            if envelope_result.get("usage_unknown")
            else "verified"
        ),
        "expected_provider": expected_provider,
        "expected_model": expected_model,
        "observed_provider": observed_provider,
        "observed_model": observed_model,
        "usage_status": (
            "unknown"
            if envelope_result.get("usage_unknown")
            else "reported"
        ),
        "error": None,
    }


# ---------------------------------------------------------------------------
# Legacy alias routing refusal
# ---------------------------------------------------------------------------
def legacy_qwen_alias_routing_status(profile: dict[str, Any]) -> dict[str, Any]:
    """Report whether the legacy ``qwen-local`` alias lane is still disabled.

    Per CONTEXT.md §6, Fleet preserves the historical 11439 Ollama lane but
    no longer advertises it for routing; the new ``openclaw-local-qwen`` lane
    stays disabled until the parent proves the 11440 vLLM backend is
    reachable. Both layers must be reflected here so audits do not have
    to re-parse profiles.json.
    """
    legacy_ids = ("qwen-local", "qwen-local-5070ti")
    state: dict[str, Any] = {
        "legacy_aliases_present": [],
        "legacy_aliases_routing_enabled": [],
        "legacy_aliases_blockers": {},
        "openclaw_local_qwen_present": False,
        "openclaw_local_qwen_routing_enabled": False,
        "openclaw_local_qwen_blocker": None,
    }
    for agent_id in legacy_ids:
        entry = profile.get(agent_id)
        if not isinstance(entry, dict):
            continue
        state["legacy_aliases_present"].append(agent_id)
        if entry.get("routing_enabled") is True:
            state["legacy_aliases_routing_enabled"].append(agent_id)
        else:
            state["legacy_aliases_blockers"][agent_id] = entry.get(
                "routing_blocker",
                "legacy-local-qwen-alias-disabled-pending-openclaw-11440-canary",
            )
    openclaw_local = profile.get("openclaw-local-qwen")
    if isinstance(openclaw_local, dict):
        state["openclaw_local_qwen_present"] = True
        state["openclaw_local_qwen_routing_enabled"] = bool(
            openclaw_local.get("routing_enabled")
        )
        state["openclaw_local_qwen_blocker"] = openclaw_local.get(
            "routing_blocker"
        )
    legacy_off = not state["legacy_aliases_routing_enabled"]
    openclaw_off = (
        state["openclaw_local_qwen_present"]
        and not state["openclaw_local_qwen_routing_enabled"]
    )
    state["routing_disabled_until_parent_canary"] = bool(legacy_off and openclaw_off)
    return state


# ---------------------------------------------------------------------------
# CLI executable probe (read-only diagnostic, never invoked against real CLI)
# ---------------------------------------------------------------------------
def probe_executable_metadata(executable: Path) -> dict[str, Any]:
    """Light, read-only metadata fetch. No process is spawned."""
    if not executable.is_file():
        raise OpenClawRuntimeError(f"OpenClaw executable is missing: {executable}")
    digest = hashlib.sha256()
    with executable.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return {
        "path": str(executable.resolve()),
        "size_bytes": executable.stat().st_size,
        "sha256": digest.hexdigest(),
        "exists": True,
    }


def write_executable_probe_skip(
    run_dir: Path,
    *,
    label: str,
    reason: str,
) -> Path:
    """Materialise a placeholder probe artefact without launching the CLI."""
    probe_path = run_dir / f"openclaw.probe.{label}.SKIPPED.txt"
    probe_path.parent.mkdir(parents=True, exist_ok=True)
    probe_path.write_text(
        "status=SKIPPED\n"
        f"reason={reason}\n"
        f"pinned_version={PINNED_OPENCLAW_VERSION}\n"
        "note=OpenClaw was not invoked; this artefact is reserved for the "
        "parent's local probe that runs only after the vLLM backend canary.\n",
        encoding="utf-8",
        newline="\n",
    )
    return probe_path


# Expose ``shutil.which`` for callers that want pure-Path checks.
def which(name: str) -> str | None:
    return shutil.which(name)


# ---------------------------------------------------------------------------
# Read-only live probe (OpenAI-compatible /v1/models) for parent activation.
# ---------------------------------------------------------------------------
class _NoProbeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _probe_models_payload(base_url: str, *, timeout: float) -> dict[str, Any]:
    """Fetch ``GET {base_url}/models`` from the pinned loopback endpoint.

    Fleet deliberately disables proxy inheritance so the probe never reaches
    anything other than the loopback listener. Network failures, malformed
    JSON, and timeouts surface as ``OpenClawRuntimeError`` so callers can
    distinguish a probe outage from a validated ready state.
    """
    if base_url != OPENCLAW_LOCAL_BASE_URL:
        raise OpenClawRuntimeError("OpenClaw probe requires the pinned localhost endpoint")
    probe_url = base_url + "/models"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoProbeRedirect())
    try:
        with opener.open(probe_url, timeout=timeout) as response:
            raw_bytes = response.read(2_000_001)
            if len(raw_bytes) > 2_000_000:
                raise OpenClawRuntimeError("OpenClaw local probe response exceeded size limit")
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        raise OpenClawRuntimeError(
            f"OpenClaw local probe failed to reach {probe_url}: {exc}"
        ) from exc
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OpenClawRuntimeError(
            f"OpenClaw local probe returned non-UTF-8 bytes from {probe_url}"
        ) from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise OpenClawRuntimeError(
            f"OpenClaw local probe returned malformed JSON from {probe_url}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise OpenClawRuntimeError(
            f"OpenClaw local probe payload must be a JSON object: {probe_url}"
        )
    return payload


def _models_identity_probe(
    payload: dict[str, Any],
    *,
    expected_model: str,
    declared_context_window: int,
) -> dict[str, Any]:
    """Verify the live models list matches the pinned local backend contract.

    The probe is deliberately narrow: only the exact served model id and the
    declared context window are inspected. Anything else is ignored so the
    probe stays a read-only health check, not a tool/feature canary.
    """
    data = payload.get("data")
    if not isinstance(data, list):
        raise OpenClawRuntimeError(
            "OpenClaw local probe missing OpenAI-compatible 'data' array"
        )
    matching = [
        row
        for row in data
        if isinstance(row, dict)
        and str(row.get("id", "")).strip() == expected_model
    ]
    if not matching:
        raise OpenClawRuntimeError(
            "OpenClaw local probe did not return the pinned served model "
            f"{expected_model!r}; refusing to claim readiness"
        )
    row = matching[0]
    max_model_len = row.get("max_model_len")
    if not isinstance(max_model_len, int) or isinstance(max_model_len, bool):
        raise OpenClawRuntimeError(
            f"OpenClaw local probe missing integer max_model_len for "
            f"{expected_model!r}; refusing to claim readiness"
        )
    if max_model_len < declared_context_window:
        raise OpenClawRuntimeError(
            "OpenClaw local probe reports max_model_len="
            f"{max_model_len} < declared context_window_tokens="
            f"{declared_context_window}; refusing to claim readiness"
        )
    return {
        "expected_model": expected_model,
        "observed_model_id": expected_model,
        "max_model_len": max_model_len,
        "declared_context_window_tokens": declared_context_window,
        "context_bound_satisfied": True,
    }


# Generic Fleet local-runtime interface. This rollout remains fail closed.
validate_config = validate_local_runtime

def runtime_status(raw):
    cfg = validate_local_runtime(raw)
    if cfg["runtime_not_ready"]:
        return {
            **cfg,
            "status": "NOT_READY",
            "routing_ready": False,
            "ready": False,
            "reason": OPENCLAW_LOCAL_NOT_READY_REASON,
            "probe": None,
        }
    try:
        payload = _probe_models_payload(
            cfg["base_url"],
            timeout=OPENCLAW_LOCAL_PROBE_TIMEOUT_SECONDS,
        )
    except OpenClawRuntimeError as exc:
        return {
            **cfg,
            "status": "NOT_READY",
            "routing_ready": False,
            "ready": False,
            "reason": (
                "runtime_not_ready: explicit parent activation could not "
                "complete the read-only /v1/models probe; verify the local "
                "vLLM backend, the 11440 listener, and the exact "
                "vllm/Qwen3.8-27B identity"
            ),
            "probe_error": str(exc),
            "probe": None,
        }
    try:
        identity = _models_identity_probe(
            payload,
            expected_model=cfg["served_model_name"],
            declared_context_window=cfg["context_window_tokens"],
        )
    except OpenClawRuntimeError as exc:
        return {
            **cfg,
            "status": "NOT_READY",
            "routing_ready": False,
            "ready": False,
            "reason": (
                "runtime_not_ready: live /v1/models probe failed exact "
                "identity / context checks; refuse to advertise the lane"
            ),
            "probe_error": str(exc),
            "probe": None,
        }
    return {
        **cfg,
        "status": "READY",
        "routing_ready": True,
        "ready": True,
        "reason": (
            "explicit parent activation: read-only /v1/models probe matched "
            f"exact model {cfg['served_model_name']} and "
            f"max_model_len>={cfg['context_window_tokens']}"
        ),
        "probe": identity,
    }

def ensure_runtime(raw):
    cfg = validate_local_runtime(raw)
    if cfg["runtime_not_ready"]:
        raise OpenClawRuntimeError(
            "runtime_not_ready: externally managed Qwen11440 is shipped in "
            "fail-closed mode; explicit parent activation must set "
            "runtime_not_ready=false alongside a verified context_window_tokens"
        )
    payload = _probe_models_payload(
        cfg["base_url"],
        timeout=OPENCLAW_LOCAL_PROBE_TIMEOUT_SECONDS,
    )
    identity = _models_identity_probe(
        payload,
        expected_model=cfg["served_model_name"],
        declared_context_window=cfg["context_window_tokens"],
    )
    return {
        **cfg,
        "started_by_fleet": False,
        "probe": identity,
    }

def capacity_lock_identity(raw):
    return validate_local_runtime(raw)["state_id"]
