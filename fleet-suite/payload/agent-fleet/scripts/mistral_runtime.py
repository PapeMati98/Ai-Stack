"""Native Vibe Pro candidate. No API client, credential migration or PAYG lane.

The managed CLI admits only packaged tools bound by a trusted dispatch policy.
It runs in a fresh directory whose .vibe is VIBE_HOME: Vibe 2.26.1 then disables
project discovery (HarnessFilesManager.cwd_is_user_config_home). HOME is intact.
Native config is checked before inference and exported config is checked again.
Configured identity is NOT provider-reported identity: Fleet must refuse final
acceptance until an independently qualified served-model witness exists.
"""
from __future__ import annotations

import json
import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import weakref
import datetime as dt
import math
import stat
import time
from typing import Any, Callable


class MistralRuntimeError(RuntimeError):
    pass


MISTRAL_ADAPTER_ID = "mistral"
MISTRAL_NATIVE_VERSION = "2.26.1"
MISTRAL_NATIVE_MODEL_ID = "mistral-large-4"
MISTRAL_PROVIDER_ID = "mistral"
MISTRAL_ENDPOINT = "https://api.mistral.ai/v1"
MISTRAL_BROWSER_ENDPOINT = "https://console.mistral.ai/api"
_NATIVE_SERVICE_URLS = {
    "console_base_url": "https://console.mistral.ai",
    "vibe_base_url": "https://chat.mistral.ai",
    "vibe_code_sessions_base_url": "https://chat.mistral.ai",
}
MISTRAL_NATIVE_EXECUTABLE = os.path.expanduser("~/.agents/runtime/mistral-vibe/2.26.1/Scripts/vibe.exe")
MISTRAL_NATIVE_PYTHON = os.path.expanduser("~/.agents/runtime/mistral-vibe/2.26.1/Scripts/python.exe")
MISTRAL_MAX_PARSE_BYTES = 2_000_000
MISTRAL_MAX_HISTORY_ENTRIES = 4096
MISTRAL_MAX_TURNS = 64
# Vibe counts cumulative prompt + completion tokens across the entire session.
# This is a bounded session budget, not a per-response generation limit.
# Kept as the floor: short reviews/implementations never go below the historical
# value, but bounded runs (48-96 tool calls) need a per-step derived ceiling.
MISTRAL_MIN_SESSION_TOKENS = 262144
MISTRAL_SESSION_TOKENS_PER_STEP = 16384
MISTRAL_MAX_TOKENS = MISTRAL_MAX_TURNS * MISTRAL_SESSION_TOKENS_PER_STEP
MISTRAL_STALL_SECONDS_DEFAULT = 1200
MISTRAL_STALL_SECONDS_ENV = "FLEET_MISTRAL_STALL_SECONDS"
# Vibe's own ``--time-limit`` ends the run with outcome ``deadline`` (exit 3) and
# writes the export (identity, config, usage) after a wind-down of at most
# STOP_GRACE_SECONDS=10 s; its clock starts after the native bootstrap. With the
# limit equal to Fleet's wall timeout the parent kill always won (0.9.78
# canaries, HD-20261014-0c526e: exit 124, no export, identity unavailable) while
# a hung model stream burned the whole timeout. Give the native run its deadline
# this margin before the parent timeout so a stalled turn ends as a classified
# native stop; short runs without room keep the parent timeout as the limit.
MISTRAL_NATIVE_DEADLINE_MARGIN_SECONDS = 30
MISTRAL_NATIVE_DEADLINE_MIN_TIMEOUT = 2 * MISTRAL_NATIVE_DEADLINE_MARGIN_SECONDS
MISTRAL_PROGRESS_FILES = (
    "mistral-tool-telemetry.jsonl",
    "mistral-workspace/.vibe/logs/session/*/messages.jsonl",
    "mistral-workspace/.vibe/logs/vibe.log",
)


def native_time_limit(timeout: int) -> int:
    """Native Vibe ``--time-limit`` for one Fleet wall timeout (both seconds)."""
    if type(timeout) is not int or timeout < 1:
        raise MistralRuntimeError("Native Vibe run limit is invalid")
    if timeout < MISTRAL_NATIVE_DEADLINE_MIN_TIMEOUT:
        return timeout
    return timeout - MISTRAL_NATIVE_DEADLINE_MARGIN_SECONDS


def native_stall_seconds(timeout: int) -> int | None:
    raw = os.environ.get(MISTRAL_STALL_SECONDS_ENV)
    if raw is not None and raw.strip().isascii() and raw.strip().isdigit():
        val = int(raw.strip())
        if val == 0:
            return None
        limit = val
    else:
        limit = MISTRAL_STALL_SECONDS_DEFAULT
    if timeout <= limit:
        return None
    return limit


def _is_safe_regular_file(path: Path) -> tuple[bool, float]:
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode):
            return False, 0.0
        if stat.S_ISLNK(st.st_mode):
            return False, 0.0
        if getattr(st, "st_reparse_tag", 0) != 0:
            return False, 0.0
        file_attrs = getattr(st, "st_file_attributes", 0)
        reparse_attr = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if file_attrs & reparse_attr:
            return False, 0.0
        if getattr(st, "st_nlink", 1) > 1:
            return False, 0.0
        return True, float(st.st_mtime)
    except OSError:
        return False, 0.0


def native_progress_probe(
    run_dir: str | Path,
    *,
    started_at: float | None = None,
) -> Callable[[], float | None]:
    base = Path(run_dir)
    initial_time = time.time() if started_at is None else float(started_at)

    def probe() -> float | None:
        try:
            newest_mtime: float | None = None
            for telem_cand in (
                base / "mistral-tool-telemetry.jsonl",
                base / "mistral-output" / "telemetry.jsonl",
                base / "telemetry.jsonl",
            ):
                ok, mtime = _is_safe_regular_file(telem_cand)
                if ok:
                    newest_mtime = mtime if newest_mtime is None else max(newest_mtime, mtime)

            for vibe_cand in (
                base / "mistral-workspace" / ".vibe" / "logs" / "vibe.log",
                base / "vibe.log",
            ):
                ok, mtime = _is_safe_regular_file(vibe_cand)
                if ok:
                    newest_mtime = mtime if newest_mtime is None else max(newest_mtime, mtime)

            for sroot_cand in (
                base / "mistral-workspace" / ".vibe" / "logs" / "session",
                base / "sessions",
                base / "session",
            ):
                if sroot_cand.is_dir():
                    try:
                        for entry in os.scandir(sroot_cand):
                            try:
                                if entry.is_dir(follow_symlinks=False):
                                    ok, mtime = _is_safe_regular_file(Path(entry.path) / "messages.jsonl")
                                    if ok:
                                        newest_mtime = mtime if newest_mtime is None else max(newest_mtime, mtime)
                            except OSError:
                                continue
                    except OSError:
                        pass

            effective = initial_time if newest_mtime is None else newest_mtime
            return max(0.0, time.time() - effective)
        except Exception:
            return None

    return probe


_ALLOWED_TIME_CHARS = set("0123456789-:.T+Z")
_NUMERIC_INT_FIELDS = (
    "steps",
    "session_prompt_tokens",
    "session_completion_tokens",
    "last_turn_prompt_tokens",
    "last_turn_completion_tokens",
    "tool_calls_failed",
    "tool_calls_succeeded",
    "context_tokens",
)


def _clean_time_str(v: Any) -> str | None:
    if isinstance(v, str) and 0 < len(v) <= 40:
        if all(c in _ALLOWED_TIME_CHARS for c in v):
            return v
    return None


def read_native_session_stats(run_dir: str | Path) -> dict[str, Any] | None:
    base = Path(run_dir)
    session_root = None
    for sroot_cand in (
        base / "mistral-workspace" / ".vibe" / "logs" / "session",
        base / "sessions",
        base / "session",
    ):
        if sroot_cand.is_dir():
            session_root = sroot_cand
            break
    if session_root is None:
        return None
    # Vibe keeps helper directories (`active`, `.last_session`) beside the session directory, so only a
    # directory that holds a regular meta.json counts as a session; the most recently written one is current.
    candidates: list[tuple[float, str, Path]] = []
    try:
        for entry in os.scandir(session_root):
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                meta_cand = Path(entry.path) / "meta.json"
                ok, mtime = _is_safe_regular_file(meta_cand)
                if ok:
                    candidates.append((mtime, entry.name, meta_cand))
            except OSError:
                pass
    except OSError:
        return None

    if not candidates:
        return None

    meta_file = max(candidates)[2]
    try:
        if meta_file.stat().st_size > 1024 * 1024:
            return None
        content = meta_file.read_bytes()
        data = json.loads(content.decode("utf-8"))
    except Exception:
        return None

    if not isinstance(data, dict):
        return None

    stats = data.get("stats")
    if not isinstance(stats, dict):
        return None

    copied: dict[str, Any] = {}
    for field in _NUMERIC_INT_FIELDS:
        val = stats.get(field)
        if isinstance(val, (int, float)) and not isinstance(val, bool) and math.isfinite(val):
            copied[field] = int(val)

    ltd = stats.get("last_turn_duration")
    if isinstance(ltd, (int, float)) and not isinstance(ltd, bool) and math.isfinite(ltd):
        copied["last_turn_seconds"] = int(round(ltd))

    tps = stats.get("tokens_per_second")
    if isinstance(tps, (int, float)) and not isinstance(tps, bool) and math.isfinite(tps):
        copied["tokens_per_second"] = round(float(tps), 1)

    numeric_count = sum(
        1 for f in (*_NUMERIC_INT_FIELDS, "last_turn_seconds", "tokens_per_second")
        if f in copied
    )
    if numeric_count == 0:
        return None

    for tkey in ("start_time", "end_time"):
        tval = _clean_time_str(data.get(tkey))
        if tval is None:
            tval = _clean_time_str(stats.get(tkey))
        if tval is not None:
            copied[tkey] = tval

    newest_mtime: float | None = None
    for telem_cand in (
        base / "mistral-tool-telemetry.jsonl",
        base / "mistral-output" / "telemetry.jsonl",
        base / "telemetry.jsonl",
    ):
        ok, mtime = _is_safe_regular_file(telem_cand)
        if ok:
            newest_mtime = mtime if newest_mtime is None else max(newest_mtime, mtime)

    for vibe_cand in (
        base / "mistral-workspace" / ".vibe" / "logs" / "vibe.log",
        base / "vibe.log",
    ):
        ok, mtime = _is_safe_regular_file(vibe_cand)
        if ok:
            newest_mtime = mtime if newest_mtime is None else max(newest_mtime, mtime)

    try:
        ok, mtime = _is_safe_regular_file(meta_file.parent / "messages.jsonl")
        if ok:
            newest_mtime = mtime if newest_mtime is None else max(newest_mtime, mtime)
    except Exception:
        pass

    if newest_mtime is not None:
        try:
            copied["last_progress_at"] = (
                dt.datetime.fromtimestamp(newest_mtime, tz=dt.timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
        except Exception:
            pass

    return copied


def mistral_native_session_summary(
    stats: dict[str, Any] | None,
    *,
    idle_seconds: float | None = None,
) -> str | None:
    if not isinstance(stats, dict):
        return None

    clauses: list[str] = []

    steps = stats.get("steps")
    if steps is not None:
        succ = stats.get("tool_calls_succeeded")
        if succ is None:
            succ = stats.get("tool_results")
        fail = stats.get("tool_calls_failed")
        if fail is None:
            fail = stats.get("refused")
        if succ is not None and fail is not None:
            tool_part = f" ({succ} tool results, {fail} refused)"
        elif succ is not None:
            tool_part = f" ({succ} tool results)"
        elif fail is not None:
            tool_part = f" ({fail} refused)"
        else:
            tool_part = ""
        clauses.append(f"the native Vibe session completed {steps} model steps{tool_part}")

    last_sec = stats.get("last_turn_seconds")
    if last_sec is None:
        last_sec = stats.get("last_step_duration_seconds")
    if last_sec is None:
        last_sec = stats.get("last_turn_duration")
    if isinstance(last_sec, (int, float)) and not isinstance(last_sec, bool):
        last_sec = int(round(last_sec))

    last_tokens = stats.get("last_turn_completion_tokens")
    if last_tokens is None:
        last_tokens = stats.get("session_completion_tokens")
    if last_tokens is None:
        last_tokens = stats.get("completion_tokens")
    if isinstance(last_tokens, (int, float)) and not isinstance(last_tokens, bool):
        last_tokens = int(last_tokens)

    if last_sec is not None and last_tokens is not None:
        clauses.append(f"the last completed step took {last_sec} s for {last_tokens} completion tokens")
    elif last_sec is not None:
        clauses.append(f"the last completed step took {last_sec} s")
    elif last_tokens is not None:
        clauses.append(f"the last completed step took {last_tokens} completion tokens")

    if idle_seconds is not None and idle_seconds >= 0:
        clauses.append(
            f"no native progress for the final {round(idle_seconds)} s before the process ended, "
            f"so a model turn was still generating (long reasoning without a tool call), not a transport hang"
        )

    if not clauses:
        return None
    return "; ".join(clauses)


def session_token_budget(max_tool_calls: int | None) -> int:
    """Map a run's tool-call bound to Vibe's cumulative session token ceiling.

    Vibe's `--max-tokens` caps the SUM of every session_prompt_tokens and
    session_completion_tokens across the entire run. With native compaction
    disabled and the full context resent each step, the historical 262144 is
    exhausted after ~13-27 tool calls. Bound the budget to the actual step
    count: ``max_tool_calls + 1`` model steps (the +1 covers a non-tooled
    completion), clipped to the native turn limit, then mapped at
    ``MISTRAL_SESSION_TOKENS_PER_STEP`` per step with the historical floor.
    """
    if max_tool_calls is None:
        return MISTRAL_MIN_SESSION_TOKENS
    if type(max_tool_calls) is not int or max_tool_calls < 1:
        raise MistralRuntimeError("Native Vibe session token bound needs an explicit positive integer")
    steps = min(MISTRAL_MAX_TURNS, max_tool_calls + 1)
    return max(MISTRAL_MIN_SESSION_TOKENS, min(MISTRAL_MAX_TOKENS, steps * MISTRAL_SESSION_TOKENS_PER_STEP))


_DISABLES = (
    "enable_telemetry", "enable_update_checks", "enable_auto_update",
    "enable_connectors", "enable_notifications", "experimental_enable_registry_skills",
)
_ENV_ALLOWLIST = {
    "systemroot", "windir", "comspec", "path", "pathext", "temp", "tmp",
    "userprofile", "home", "homedrive", "homepath", "localappdata", "appdata",
    "programdata", "programfiles", "programfiles(x86)", "allusersprofile",
    "processor_architecture", "number_of_processors",
}


def tool_module() -> Any:
    """Load only this release's sibling module, including under native Python -I."""
    name = "fleet_mistral_tools"
    path = Path(__file__).resolve().with_name("mistral_tools.py")
    existing = sys.modules.get(name)
    if existing is not None:
        if Path(existing.__file__).resolve() != path:
            raise MistralRuntimeError("Native tool module origin drift")
        return existing
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise MistralRuntimeError("Packaged native tools unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def bind_tool_policy(task: str, *, workspace: Path, run_dir: Path, mode: str,
                     dispatch: dict[str, Any] | None = None,
                     max_tool_calls: int | None = None,
                      chrome_assignment: dict[str, Any] | None = None,
                      preview: bool = False,
                      source_workspace: Path | str | None = None,
                      verified_read_inputs: Any = None) -> dict[str, Any] | None:
    """Bind caller data, never assistant output. An explicit UAT dispatch is supported.

    The TODO bridge embeds a bounded JSON payload in task text. If both that
    payload and a programmatic dispatch exist they must agree exactly.
    """
    candidates = []
    if not isinstance(task, str) or len(task.encode("utf-8")) > MISTRAL_MAX_PARSE_BYTES:
        raise MistralRuntimeError("Native task payload exceeds its bound")
    decoder = json.JSONDecoder()
    start = 0
    for _ in range(256):
        start = task.find("{", start)
        if start < 0:
            break
        try:
            value, end = decoder.raw_decode(task[start:])
        except (ValueError, RecursionError):
            start += 1
            continue
        if isinstance(value, dict) and "scope" in value and "checks" in value:
            candidates.append({key: value.get(key) for key in ("scope", "inputs", "checks")})
        elif (isinstance(value, dict) and mode in {"advisory", "review"}
              and dispatch is not None and "checks" not in value
              and isinstance(value.get("scope"), list) and isinstance(value.get("inputs"), list)
              and isinstance(value.get("task"), str) and isinstance(value.get("done_when"), list)):
            # The real reviewer envelope omits checks; its trusted dispatch must
            # still match these paths and contain an empty check list below.
            candidates.append({"scope": value["scope"], "inputs": value["inputs"], "checks": []})
        start += end
    if len(candidates) > 1:
        raise MistralRuntimeError("Ambiguous native dispatch payload")
    def normalized(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) != {"scope", "inputs", "checks"}:
            raise MistralRuntimeError("Native dispatch requires scope, inputs and checks")
        checks = value["checks"]
        if not isinstance(checks, list):
            raise MistralRuntimeError("Native checks must be an argv list")
        rows = []
        for check in checks:
            if isinstance(check, dict):
                if check.get("status", "included") != "included" or check.get("omitted_args", 0) != 0:
                    raise MistralRuntimeError("Incomplete native declared check refused")
                check = check.get("argv")
            rows.append(check)
        return {"scope": value["scope"], "inputs": value["inputs"], "checks": rows}
    payload = normalized(candidates[0]) if candidates else None
    bound = normalized(dispatch) if dispatch is not None else payload
    if payload is not None and bound != payload:
        raise MistralRuntimeError("Native prompt and trusted dispatch disagree")
    if bound is None:
        if mode == "implement":
            raise MistralRuntimeError("Native implementation requires trusted tool dispatch")
        return None  # Preserve prompt-only advisory callers.
    try:
        return tool_module().build_policy(workspace=workspace, run_dir=run_dir, mode=mode,
                                         preview=preview, source_workspace=source_workspace,
                                         max_tool_calls=max_tool_calls, chrome_assignment=chrome_assignment,
                                         verified_read_inputs=verified_read_inputs, **bound)
    except (ValueError, TypeError, OSError) as exc:
        # The legacy TODO bridge makes a structural admission call before Fleet's
        # required-input preflight. Only an exactly matched programmatic dispatch
        # may get a non-activatable declaration for a soft-name input; authority
        # still comes exclusively from the later verified controller state.
        tools = tool_module()
        if (not preview and verified_read_inputs is None and dispatch is not None
                and payload is not None and bound == payload
                and isinstance(exc, tools.ToolPolicyError) and exc.code == "protected"
                and isinstance(bound["inputs"], list)):
            try:
                has_soft_input = False
                for item in bound["inputs"]:
                    try:
                        tools.normalize_relative(item)
                    except tools.ToolPolicyError:
                        tools.normalize_relative(item, _soft_secret=True)
                        has_soft_input = True
                if has_soft_input:
                    return tools.build_policy(
                        workspace=workspace, run_dir=run_dir, mode=mode,
                        preview=True, source_workspace=source_workspace,
                        max_tool_calls=max_tool_calls, chrome_assignment=chrome_assignment,
                        verified_read_inputs=None, **bound)
            except (ValueError, TypeError, OSError):
                pass
        raise MistralRuntimeError(f"Native tool dispatch policy refused: {exc}") from exc


def preview_tool_policy(task: str, *, workspace: Path, run_dir: Path, mode: str,
                        dispatch: dict[str, Any] | None = None,
                        source_workspace: Path | str | None = None,
                        max_tool_calls: int | None = None,
                        chrome_assignment: dict[str, Any] | None = None) -> dict[str, Any] | None:
    return bind_tool_policy(
        task, workspace=workspace, run_dir=run_dir, mode=mode,
        dispatch=dispatch, max_tool_calls=max_tool_calls,
        chrome_assignment=chrome_assignment, preview=True,
        source_workspace=source_workspace,
    )


def require_model(model: Any) -> str:
    if model != MISTRAL_NATIVE_MODEL_ID:
        raise MistralRuntimeError("Vibe requires the exact mistral-large-4 model; aliases/fallbacks refused")
    return MISTRAL_NATIVE_MODEL_ID


def require_native_executable(executable: str) -> str:
    if Path(executable).resolve() != Path(MISTRAL_NATIVE_EXECUTABLE).resolve():
        raise MistralRuntimeError("Vibe executable must be the pinned official 2.26.1 installation")
    return str(Path(executable).resolve())


def validate_mistral_profile(profile: Any) -> dict[str, Any]:
    if not isinstance(profile, dict):
        raise MistralRuntimeError("Vibe profile contract is missing")
    block = profile.get("mistral")
    expected = {
        "pro_only": True, "payg_fallback": False, "native_version": MISTRAL_NATIVE_VERSION,
        "model": MISTRAL_NATIVE_MODEL_ID, "endpoint": MISTRAL_ENDPOINT,
        "credential_backend": "native-windows-keyring", "tools": "fleet-scoped-native",
        "identity_evidence": "official-mistralai-sdk-response",
    }
    if (not isinstance(block, dict) or block != expected
            or any(type(block[key]) is not type(value) for key, value in expected.items())):
        raise MistralRuntimeError("Vibe contract must pin native Pro, Large 4, scoped tools and no PAYG fallback")
    if (profile.get("adapter") != MISTRAL_ADAPTER_ID
            or profile.get("cli_provider_id") != MISTRAL_PROVIDER_ID
            or profile.get("quota_pool_id") != "mistral-vibe-code-pro"):
        raise MistralRuntimeError("Vibe profile/provider/quota lane is not pinned")
    if "credential" in profile or "local_runtime" in profile:
        raise MistralRuntimeError("Vibe cannot use Fleet credentials or a replacement runtime")
    qualification = profile.get("qualification", {})
    qualified = isinstance(qualification, dict) and qualification.get("status") == "qualified"
    if qualified:
        import re
        for gate in ("native_uat", "security", "fleet_uat", "tier_routing"):
            evidence = qualification.get(gate)
            if (not isinstance(evidence, dict) or evidence.get("status") != "PASS"
                    or not isinstance(evidence.get("ref"), str) or not evidence["ref"].strip()
                    or not isinstance(evidence.get("sha256"), str)
                    or not re.fullmatch(r"[a-f0-9]{64}", evidence["sha256"])):
                raise MistralRuntimeError("Vibe qualification requires parent acceptance evidence")
    if type(profile.get("routing_enabled")) is not bool:
        raise MistralRuntimeError("Vibe routing state must be explicit")
    if profile["routing_enabled"] and (not qualified or profile.get("routing_blocker")):
        raise MistralRuntimeError("Vibe activation requires qualified parent evidence")
    if not profile["routing_enabled"] and not profile.get("routing_blocker"):
        raise MistralRuntimeError("Vibe disabled candidate requires a blocker")
    if profile.get("supported_modes") != ["advisory", "review", "implement"]:
        raise MistralRuntimeError("Vibe requires explicit read and maker modes")
    if profile.get("candidates") != [MISTRAL_NATIVE_EXECUTABLE]:
        raise MistralRuntimeError("Vibe candidates must contain only the pinned executable")
    if profile.get("version_args") != ["--version"] or profile.get("deep_probe_args") != ["--version"]:
        raise MistralRuntimeError("Vibe readiness probes must be version-only")
    models = profile.get("models")
    if not isinstance(models, dict) or set(models) != {"low", "medium", "high"}:
        raise MistralRuntimeError("Vibe model map is incomplete")
    for model in models.values():
        require_model(model)
    executions = profile.get("execution_profiles")
    if not isinstance(executions, dict) or not executions:
        raise MistralRuntimeError("Vibe execution profiles are missing")
    for row in executions.values():
        if not isinstance(row, dict):
            raise MistralRuntimeError("Vibe execution profile is invalid")
        require_model(row.get("model"))
        require_model(row.get("runtime_model_id"))
        maker = row.get("supported_modes") == ["implement"]
        expected_tools = (["filesystem-read", "filesystem-write", "write", "sandboxed-check"] if maker else
                          ["independent-review", "filesystem-read"])
        if (row.get("model_provider_id") != MISTRAL_PROVIDER_ID or row.get("model_tier") != "K"
                or row.get("supported_modes") != (["implement"] if maker else ["advisory", "review"])
                or row.get("tools") != expected_tools
                or (not qualified and (row.get("lifecycle") != "LIMITED" or row.get("evidence_confidence") != 0.0))
                or (qualified and row.get("lifecycle") not in {"LIMITED", "ACTIVE"})):
            raise MistralRuntimeError("Vibe execution profile capability/qualification drift")
    if not any(row.get("supported_modes") == ["implement"] for row in executions.values()):
        raise MistralRuntimeError("Vibe maker execution profile missing")
    return dict(block)


def harden_mistral_environment(env: dict[str, str], *, run_home: Path | None = None) -> dict[str, str]:
    """Reject native overrides by name; never include their values in errors."""
    for key, value in env.items():
        lower = key.casefold()
        if lower.startswith("vibe_"):
            if (lower == "vibe_cli" and value == "python") or (
                    lower == "vibe_active_model" and value == MISTRAL_NATIVE_MODEL_ID):
                continue
            raise MistralRuntimeError("Ambient VIBE override refused")
        if lower.startswith("mistral_") and lower != "mistral_api_key":
            raise MistralRuntimeError("Ambient Mistral route override refused")
    result = {key: value for key, value in env.items() if key.casefold() in _ENV_ALLOWLIST}
    result.update(VIBE_CLI="python", VIBE_ACTIVE_MODEL=MISTRAL_NATIVE_MODEL_ID,
                  OTEL_SDK_DISABLED="true", DO_NOT_TRACK="1", PYTHONIOENCODING="utf-8")
    if run_home is not None:
        result["VIBE_HOME"] = str(Path(run_home).resolve())
    return result


# Runs ONLY in the native installation's isolated Python, never Fleet's host.
# stdout is a boolean marker. Exceptions and backend details never leave it.
_CREDENTIAL_CHECK = """
import sys
try:
    import keyring
    from keyring.backends.Windows import WinVaultKeyring
    backend = keyring.get_keyring()
    if type(backend) is not WinVaultKeyring:
        sys.exit(2)
    present = bool(backend.get_password('ai.mistral.vibe', 'MISTRAL_API_KEY'))
    print('present' if present else 'missing')
except Exception:
    sys.exit(2)
"""


def credential_status(profile: dict[str, Any]) -> dict[str, Any]:
    """Presence-only; NOT proof of Pro entitlement, training opt-out or quota."""
    validate_mistral_profile(profile)
    status = {"required": True, "ready": False, "reason": "mistral-native-keyring-unavailable",
              "auth_evidence": "native-keyring-presence-only", "entitlement": "UNVERIFIED"}
    if os.name != "nt" or not Path(MISTRAL_NATIVE_PYTHON).is_file():
        return status
    try:
        child = subprocess.run(
            [MISTRAL_NATIVE_PYTHON, "-I", "-c", _CREDENTIAL_CHECK],
            env=harden_mistral_environment(dict(os.environ)),
            capture_output=True, text=True, timeout=10, check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError, MistralRuntimeError):
        return status
    if child.returncode == 0 and child.stdout.strip() == "present":
        return dict(status, ready=True, reason="mistral-native-keyring-present")
    if child.returncode == 0 and child.stdout.strip() == "missing":
        return dict(status, reason="mistral-native-keyring-missing")
    return status


def require_native_login(profile: dict[str, Any]) -> dict[str, Any]:
    status = credential_status(profile)
    if not status["ready"]:
        raise MistralRuntimeError("Vibe native Windows credential unavailable; no environment/API fallback")
    return status


def managed_config(tool_policy: dict[str, Any] | None = None) -> dict[str, Any]:
    config = {
        "active_model": MISTRAL_NATIVE_MODEL_ID, "allowed_models": [MISTRAL_NATIVE_MODEL_ID],
        "models": [{"name": MISTRAL_NATIVE_MODEL_ID, "alias": MISTRAL_NATIVE_MODEL_ID,
                    "provider": MISTRAL_PROVIDER_ID, "auto_compact_threshold": 0}],
        "providers": [{"name": MISTRAL_PROVIDER_ID, "api_base": MISTRAL_ENDPOINT,
                       "api_key_env_var": "MISTRAL_API_KEY", "backend": "mistral",
                       "browser_auth_base_url": "https://console.mistral.ai",
                       "browser_auth_api_base_url": MISTRAL_BROWSER_ENDPOINT}],
        **{key: False for key in _DISABLES}, "auto_compact_threshold": 0,
        **_NATIVE_SERVICE_URLS,
        "experiments": {"enable": False}, "session_logging": {"generate_titles": False},
        "utility_models": {"title": "active", "smart_approve": "active"},
        "mcp_servers": [], "disabled_skills": ["*"], "enabled_skills": [],
        "disabled_tools": ["*"], "enabled_tools": [], "skill_paths": [], "tool_paths": [],
    }
    if tool_policy is not None:
        try:
            is_preview = bool(isinstance(tool_policy, dict) and tool_policy.get("preview") is True)
            tool_policy = tool_module().validate_policy(tool_policy, preview=is_preview)
        except (ValueError, OSError) as exc:
            raise MistralRuntimeError(f"Native config tool policy refused: {exc}") from exc
        config.update(disabled_tools=[], enabled_tools=tool_policy["tools"],
                      tool_paths=[str(Path(__file__).resolve().with_name("mistral_tools.py"))])
    return config


def validate_native_config(config: Any, *, tool_policy: dict[str, Any] | None = None) -> None:
    """Vibe merges inert schema defaults. Only the exact allowlisted model can run.

    Reject every other selectable alias, utility/vision/compaction override and
    any active external route. The default Medium entry may remain in the full
    native config, but is excluded by the exact allowlist and cannot be selected.
    """
    if not isinstance(config, dict):
        raise MistralRuntimeError("Native effective Vibe config is missing")
    if config.get("active_model") != MISTRAL_NATIVE_MODEL_ID or config.get("allowed_models") != [MISTRAL_NATIVE_MODEL_ID]:
        raise MistralRuntimeError("Native Vibe model selection drift")
    models = config.get("models")
    if isinstance(models, list):
        rows = models
    elif isinstance(models, dict):
        rows = list(models.values())
        if any(not isinstance(row, dict) or row.get("alias") != alias for alias, row in models.items()):
            raise MistralRuntimeError("Native Vibe model aliases are inconsistent")
    else:
        raise MistralRuntimeError("Native Vibe model metadata is missing")
    if any(not isinstance(row, dict) for row in rows):
        raise MistralRuntimeError("Native Vibe model metadata is invalid")
    eligible = [row for row in rows if row.get("name") == MISTRAL_NATIVE_MODEL_ID]
    if len(eligible) != 1 or eligible[0].get("alias") != MISTRAL_NATIVE_MODEL_ID or eligible[0].get("provider") != MISTRAL_PROVIDER_ID:
        raise MistralRuntimeError("Native Vibe Large 4 identity missing or mismatched")
    if eligible[0].get("auto_compact_threshold") != 0:
        raise MistralRuntimeError("Native Vibe model compaction must be disabled")
    providers = config.get("providers")
    if not isinstance(providers, list) or any(not isinstance(row, dict) for row in providers):
        raise MistralRuntimeError("Native Vibe provider metadata is missing")
    selected = [row for row in providers if row.get("name") == MISTRAL_PROVIDER_ID]
    if len(selected) != 1:
        raise MistralRuntimeError("Native Vibe provider metadata is ambiguous")
    provider = selected[0]
    if (provider.get("api_base") != MISTRAL_ENDPOINT or provider.get("backend") != "mistral"
            or provider.get("api_key_env_var") != "MISTRAL_API_KEY"
            or provider.get("browser_auth_base_url") != "https://console.mistral.ai"
            or provider.get("browser_auth_api_base_url") != MISTRAL_BROWSER_ENDPOINT
            or provider.get("browser_auth_allow_origin_rewrite", False) is not False
            or provider.get("extra_headers", {})):
        raise MistralRuntimeError("Native Vibe endpoint/auth route drift")
    if any(config.get(key) is not False for key in _DISABLES):
        raise MistralRuntimeError("Native Vibe telemetry/update/connector disable missing")
    if any(config.get(key) != value for key, value in _NATIVE_SERVICE_URLS.items()):
        raise MistralRuntimeError("Native Vibe service endpoint drift")
    expected_config = managed_config(tool_policy)
    for key, expected in (("auto_compact_threshold", 0), ("mcp_servers", []),
                          ("disabled_skills", ["*"]), ("enabled_skills", []),
                          ("enabled_tools", expected_config["enabled_tools"]),
                          ("skill_paths", []), ("tool_paths", expected_config["tool_paths"])):
        if config.get(key) != expected:
            raise MistralRuntimeError("Native Vibe tool/skill/compaction boundary drift")
    # Native plan/CLI layers append their own denials and may repeat '*'.
    # Accept only these known additional denials, never an enabling pattern.
    disabled = config.get("disabled_tools")
    if (not isinstance(disabled, list) or (tool_policy is None and "*" not in disabled)
            or any(type(item) is not str or item not in {
                *( ["*"] if tool_policy is None else [] ), "ask_user_question", "exit_plan_mode", "cron"
            } for item in disabled)):
        raise MistralRuntimeError("Native Vibe tool disable boundary drift")
    experiments = config.get("experiments")
    logging = config.get("session_logging")
    utility = config.get("utility_models")
    if not isinstance(experiments, dict) or experiments.get("enable") is not False:
        raise MistralRuntimeError("Native Vibe experiments must be disabled")
    if not isinstance(logging, dict) or logging.get("generate_titles") is not False:
        raise MistralRuntimeError("Native Vibe title generation must be disabled")
    if not isinstance(utility, dict) or utility.get("title") != "active" or utility.get("smart_approve") != "active":
        raise MistralRuntimeError("Native Vibe utility models must use the active model")
    if config.get("compaction_model") is not None or config.get("vision_model") is not None:
        raise MistralRuntimeError("Native Vibe secondary model override refused")


def prepare_mistral_run(run_dir: Path, *, tool_policy: dict[str, Any] | None = None) -> Path:
    """Fresh, exclusive state; never reuse a home, create .env or delete files."""
    if isinstance(tool_policy, dict) and tool_policy.get("preview") is True:
        raise MistralRuntimeError("Native preview policy cannot be prepared for inference")
    workdir = Path(run_dir).resolve() / "mistral-workspace"
    workdir.mkdir(parents=True, exist_ok=False)
    home = workdir / ".vibe"
    home.mkdir()
    config = managed_config(tool_policy)
    if tool_policy is not None:
        if Path(tool_policy["run_dir"]).resolve() != Path(run_dir).resolve():
            raise MistralRuntimeError("Native policy/run directory mismatch")
        tool_module().write_policy(tool_policy)
    # All strings are fixed contract constants, not user-supplied TOML.
    lines = [f'{key} = false' for key in _DISABLES]
    lines += [f'{key} = {json.dumps(value)}' for key, value in _NATIVE_SERVICE_URLS.items()]
    lines += [f'active_model = "{MISTRAL_NATIVE_MODEL_ID}"',
              f'allowed_models = ["{MISTRAL_NATIVE_MODEL_ID}"]',
              'auto_compact_threshold = 0', 'mcp_servers = []',
              'disabled_skills = ["*"]', 'enabled_skills = []',
              f'disabled_tools = {json.dumps(config["disabled_tools"])}',
              f'enabled_tools = {json.dumps(config["enabled_tools"])}',
              'skill_paths = []', f'tool_paths = {json.dumps(config["tool_paths"])}']
    for group in ("experiments", "session_logging", "utility_models"):
        lines.append(f'[{group}]')
        for key, value in config[group].items():
            lines.append(f'{key} = ' + ('false' if value is False else json.dumps(value)))
    for group in ("models", "providers"):
        lines.append(f'[[{group}]]')
        for key, value in config[group][0].items():
            lines.append(f'{key} = {json.dumps(value)}')
    with (home / "config.toml").open("x", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")
    return home


# Same native process: pre-inference effective-config check, then official CLI.
# The user config-home equality disables all project layers despite --trust.
_RUN_BOOTSTRAP = """
import asyncio, importlib.util, os, sys
sys.stdout.reconfigure(encoding='utf-8', errors='strict')
sys.stderr.reconfigure(encoding='utf-8', errors='strict')
helper, workdir, policy_path, expected_policy_sha = sys.argv[1:5]
sys.argv = ['vibe', *sys.argv[5:]]
os.chdir(workdir)
spec = importlib.util.spec_from_file_location('fleet_mistral_boundary', helper)
boundary = importlib.util.module_from_spec(spec)
spec.loader.exec_module(boundary)
try:
    from pathlib import Path
    if (Path(os.environ['VIBE_HOME']) / '.env').exists():
        raise boundary.MistralRuntimeError('Plaintext Vibe credential fallback refused')
    policy = None
    if policy_path:
        import hashlib, hmac
        with Path(policy_path).open('rb') as source:
            policy_bytes = source.read(262145)
        if (len(expected_policy_sha) != 64 or
                len(policy_bytes) > 262144 or
                not hmac.compare_digest(hashlib.sha256(policy_bytes).hexdigest(), expected_policy_sha)):
            raise boundary.MistralRuntimeError('Native tool policy digest mismatch')
        tools = boundary.tool_module()
        if Path(policy_path) != Path(workdir).parent / tools.POLICY_FILENAME:
            raise boundary.MistralRuntimeError('Native tool policy location drift')
        policy = tools.activate(policy_path, expected_sha256=expected_policy_sha)
        boundary.install_tool_discovery_guard(tools)
    elif expected_policy_sha:
        raise boundary.MistralRuntimeError('Native tool policy digest without policy')
    from vibe import __version__
    if os.name != 'nt' or sys.maxsize <= 2**32 or __version__ != boundary.MISTRAL_NATIVE_VERSION:
        raise boundary.MistralRuntimeError('Native Vibe version drift')
    from vibe.core.config.harness_files import init_harness_files_manager, get_harness_files_manager
    from vibe.core.config.default_orchestrator import build_default_orchestrator
    init_harness_files_manager('user', 'project')
    manager = get_harness_files_manager()
    if not manager.cwd_is_user_config_home or manager.project_source_enabled:
        raise boundary.MistralRuntimeError('Native Vibe project discovery refused')
    async def verify():
        runtime = await build_default_orchestrator()
        boundary.validate_native_config(runtime.config.model_dump(mode='json'), tool_policy=policy)
        return boundary.native_tool_schemas(runtime.config, policy)
    schemas = asyncio.run(verify())
    boundary.install_model_observer(Path(workdir).parent / 'mistral-model-witness.json', tool_schemas=schemas)
except Exception as error:
    import json, traceback
    diagnostic = {'type': type(error).__name__, 'frames': [{'file': Path(frame.filename).name, 'line': frame.lineno, 'function': frame.name} for frame in traceback.extract_tb(error.__traceback__)[-6:]]}
    if isinstance(error, boundary.MistralRuntimeError):
        diagnostic['boundary_code'] = str(error)
    with (Path(workdir).parent / 'mistral-boundary-diagnostic.json').open('x', encoding='utf-8') as output:
        json.dump(diagnostic, output)
    print('Native Vibe configuration boundary refused', file=sys.stderr)
    sys.exit(1)
from vibe.cli.entrypoint import main
main()
"""


def build_command(executable: str, *, model: str, mode: str, run_dir: Path,
                  timeout: int, max_turns: int = MISTRAL_MAX_TURNS,
                  max_tokens: int | None = None,
                  tool_policy: dict[str, Any] | None = None) -> list[str]:
    require_native_executable(executable)
    require_model(model)
    if mode not in {"advisory", "review", "implement"} or (mode == "implement" and tool_policy is None):
        raise MistralRuntimeError("Native Vibe implementation requires a bound tool policy")
    try:
        bound_max_tool_calls = (tool_policy["limits"]["max_tool_calls"]
                                if tool_policy is not None else None)
    except (KeyError, TypeError) as exc:
        raise MistralRuntimeError("Native Vibe tool policy limits are invalid") from exc
    if max_tokens is None:
        max_tokens = session_token_budget(bound_max_tool_calls)
    for value, cap in ((timeout, 3600), (max_turns, MISTRAL_MAX_TURNS), (max_tokens, MISTRAL_MAX_TOKENS)):
        if type(value) is not int or not 1 <= value <= cap:
            raise MistralRuntimeError("Native Vibe run limit is invalid")
    run_dir = Path(run_dir).resolve()
    policy_path = ""
    policy_sha = ""
    if tool_policy is not None:
        if tool_policy.get("preview") is True:
            raise MistralRuntimeError("Native preview policy cannot be launched")
        managed_config(tool_policy)
        if tool_policy["mode"] != mode or Path(tool_policy["run_dir"]).resolve() != run_dir:
            raise MistralRuntimeError("Native command/policy mode or run mismatch")
        path = run_dir / tool_module().POLICY_FILENAME
        try:
            with path.open("rb") as source:
                policy_bytes = source.read(tool_module().MAX_POLICY_BYTES + 1)
            if len(policy_bytes) > tool_module().MAX_POLICY_BYTES:
                raise MistralRuntimeError("Native persisted policy exceeds its bound")
            policy_sha = hashlib.sha256(policy_bytes).hexdigest()
            persisted = tool_module().load_policy(path, expected_sha256=policy_sha)
            if persisted != tool_module().validate_policy(tool_policy):
                raise MistralRuntimeError("Native persisted policy differs from bound policy")
        except (OSError, ValueError) as exc:
            raise MistralRuntimeError(f"Native persisted policy refused: {exc}") from exc
        policy_path = str(path)
    agent_args = ["--agent", "accept-edits"] if tool_policy is not None else ["--agent", "plan", "--disabled-tools", "*"]
    return [MISTRAL_NATIVE_PYTHON, "-I", "-c", _RUN_BOOTSTRAP, str(Path(__file__).resolve()),
            str(run_dir / "mistral-workspace"),
            policy_path, policy_sha,
            "--legacy-harness", "--prompt-file", str(run_dir / "mistral-prompt.txt"),
            "--output", "json", "--output-dir", str(run_dir / "mistral-output"),
            *agent_args, "--max-turns", str(max_turns),
            "--max-tokens", str(max_tokens), "--time-limit", str(native_time_limit(timeout)),
            "--workdir", str(run_dir / "mistral-workspace"), "--trust"]


def install_tool_discovery_guard(module: Any) -> None:
    """Pin the native loader to one module; forbid ambient custom tool imports."""
    from vibe.core.tools.manager import ToolManager, _compute_module_name
    path = Path(__file__).resolve().with_name("mistral_tools.py")
    if not module._NATIVE or Path(module.__file__).resolve() != path:
        raise MistralRuntimeError("Pinned native tool APIs unavailable")
    for cls in module.NATIVE_TOOL_CLASSES:
        # Descriptions also come from this exact module, never sibling prompt files.
        cls.get_full_description = classmethod(lambda tool: tool.description)
    sys.modules[_compute_module_name(path)] = module
    original = ToolManager._load_tools_from_file
    def load(file_path):
        if Path(file_path).resolve() != path:
            raise MistralRuntimeError("Unadmitted native tool module refused")
        classes = original(file_path)
        if set(classes or ()) != set(module.NATIVE_TOOL_CLASSES):
            raise MistralRuntimeError("Native custom-tool discovery drift")
        return classes
    ToolManager._load_tools_from_file = staticmethod(load)
    ToolManager._compute_search_paths = lambda self, config: [path]
    ToolManager._iter_tool_descriptions = staticmethod(lambda paths: iter(()))


def native_tool_schemas(config: Any, policy: dict[str, Any] | None) -> list[dict[str, Any]]:
    if policy is None:
        return []
    from vibe.core.tools.manager import ToolManager
    from vibe.core.types import AvailableTool
    manager = ToolManager(lambda: config, defer_mcp=True)
    if set(manager.available_tools) != set(policy["tools"]):
        raise MistralRuntimeError("Native tool availability does not match policy")
    return [AvailableTool(function=spec).model_dump(mode="json") for spec in manager.available_tool_specs()]


def _bounded_json(text: str) -> Any:
    if not isinstance(text, str) or len(text) > MISTRAL_MAX_PARSE_BYTES:
        return None
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        return None


def read_native_export(run_dir: Path) -> dict[str, Any] | None:
    """Only designated native export, never stdout's model-authored metadata."""
    path = Path(run_dir).resolve() / "mistral-output" / "export.json"
    try:
        if path.is_symlink() or path.resolve() != path or path.stat().st_size > MISTRAL_MAX_PARSE_BYTES:
            return None
        with path.open("rb") as stream:
            raw = stream.read(MISTRAL_MAX_PARSE_BYTES + 1)
        if len(raw) > MISTRAL_MAX_PARSE_BYTES:
            return None
        value = _bounded_json(raw.decode("utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def observe_served_model(model: Any, host: Any, destination: Path) -> None:
    """Trusted SDK metadata only. Never inspect or store response content."""
    if model != MISTRAL_NATIVE_MODEL_ID or host != "api.mistral.ai":
        raise MistralRuntimeError("Native provider served unexpected model/origin")
    value = {"source": "official-mistralai-sdk-response", "provider": MISTRAL_PROVIDER_ID,
             "served_model": MISTRAL_NATIVE_MODEL_ID, "host": "api.mistral.ai"}
    if destination.exists():
        if read_model_witness(destination.parent) != value:
            raise MistralRuntimeError("Conflicting native provider witness")
        return
    with destination.open("x", encoding="utf-8") as stream:
        json.dump(value, stream)


def install_model_observer(destination: Path, *, tool_schemas: list[dict[str, Any]] | None = None) -> None:
    from mistralai.client.utils.eventstreaming import EventStreamAsync
    from vibe.core.llm.backend.mistral import MistralBackend
    if not all(callable(method) for method in (
            MistralBackend.complete, MistralBackend.complete_streaming, EventStreamAsync.__anext__)):
        raise MistralRuntimeError("Native request/SDK observation targets unavailable")
    # The pinned legacy CLI lane uses streaming. A non-streaming response loses
    # SDK model/origin metadata inside the backend, so refuse it before inference.
    async def refused_complete(self, *args, **kwargs):
        raise MistralRuntimeError("Unobserved non-streaming native inference refused")
    MistralBackend.complete = refused_complete
    original_stream = MistralBackend.complete_streaming
    expected = {row["function"]["name"]: row for row in (tool_schemas or [])}
    admitted_names = set(tool_module().MAKER_TOOLS) | set(tool_module().CHROME_TOOLS)
    if len(expected) != len(tool_schemas or []) or not set(expected).issubset(admitted_names):
        raise MistralRuntimeError("Native schema admission drift")
    def admitted_tools(value):
        if value in (None, []):
            return not expected
        if not isinstance(value, list) or len(value) != len(expected):
            return False
        try:
            rows = [item.model_dump(mode="json") for item in value]
            observed = {row["function"]["name"]: row for row in rows}
            return len(observed) == len(rows) and observed == expected
        except (AttributeError, KeyError, TypeError, ValueError):
            return False
    async def checked_stream(self, *args, **kwargs):
        model = kwargs.get("model")
        if (args or getattr(model, "name", None) != MISTRAL_NATIVE_MODEL_ID
                or self._server_url not in {"https://api.mistral.ai", "https://api.mistral.ai/"}
                or not admitted_tools(kwargs.get("tools"))):
            raise MistralRuntimeError("Native inference route/model/tools boundary refused")
        async for chunk in original_stream(self, *args, **kwargs):
            yield chunk
    MistralBackend.complete_streaming = checked_stream
    original_next = EventStreamAsync.__anext__
    verified_streams = weakref.WeakKeyDictionary()
    async def observed_next(self):
        event = await original_next(self)
        model = getattr(getattr(event, "data", None), "model", None)
        host = self.response.request.url.host
        if host != "api.mistral.ai" or (self not in verified_streams and not isinstance(model, str)):
            raise MistralRuntimeError("Unobserved native SDK stream origin/model refused")
        if isinstance(model, str):
            observe_served_model(model, host, destination)
            verified_streams[self] = True
        return event
    EventStreamAsync.__anext__ = observed_next


def read_model_witness(run_dir: Path) -> dict[str, Any] | None:
    path = Path(run_dir).resolve() / "mistral-model-witness.json"
    try:
        if path.is_symlink() or path.resolve() != path or path.stat().st_size > 2048:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def mistral_model_identity(export: Any, *, provider_witness: Any = None,
                           tool_policy: dict[str, Any] | None = None) -> dict[str, Any]:
    result = {"status": "unavailable", "configured_status": "unavailable",
              "evidence_type": "configured_native", "expected_model": MISTRAL_NATIVE_MODEL_ID,
              "expected_provider": MISTRAL_PROVIDER_ID, "observed_models": [], "observed_providers": [],
              "provider_reported_model": None, "error": "Native Vibe export/config metadata missing"}
    if (not isinstance(export, dict) or type(export.get("schema_version")) is not int
            or export.get("schema_version") != 1 or export.get("vibe_version") != MISTRAL_NATIVE_VERSION):
        return result
    try:
        validate_native_config(export.get("config"), tool_policy=tool_policy)
    except (MistralRuntimeError, AttributeError, TypeError):
        return dict(result, status="mismatch", configured_status="mismatch",
                    error="Native Vibe effective model/provider/config mismatch")
    expected_witness = {"source": "official-mistralai-sdk-response", "provider": MISTRAL_PROVIDER_ID,
                        "served_model": MISTRAL_NATIVE_MODEL_ID, "host": "api.mistral.ai"}
    if provider_witness is not None:
        if provider_witness != expected_witness:
            return dict(result, status="mismatch", configured_status="verified",
                        error="Native provider witness mismatch")
        return dict(result, status="verified", configured_status="verified",
                    evidence_type="official-mistralai-sdk-response",
                    observed_models=[MISTRAL_NATIVE_MODEL_ID], observed_providers=[MISTRAL_PROVIDER_ID],
                    provider_reported_model=MISTRAL_NATIVE_MODEL_ID, error=None)
    return dict(result, status="unverified", configured_status="verified",
                observed_models=[MISTRAL_NATIVE_MODEL_ID], observed_providers=[MISTRAL_PROVIDER_ID],
                error="Configured Large 4 verified; provider-reported served model unavailable")


def mistral_token_budget_exhausted(export: Any, exit_code: Any) -> bool:
    """Classify only the pinned native export, never model text or bare exit 3."""
    return (
        type(exit_code) is int and exit_code == 3
        and isinstance(export, dict)
        and type(export.get("schema_version")) is int and export["schema_version"] == 1
        and export.get("vibe_version") == MISTRAL_NATIVE_VERSION
        and type(export.get("exit_code")) is int and export["exit_code"] == exit_code
        and export.get("outcome") == "token_limit"
        and "error" in export and export["error"] is None
    )


# Vibe 2.26.1 ``RunOutcome`` taxonomy (vibe/app_server/run_export.py): every
# headless run ends with exactly one outcome and its fixed process exit code.
# ``finished`` (0) is not a stop. Anything else is a controlled native stop whose
# export carries the cause; a run that ends without an export ended outside
# Vibe's own exit path (killed, crashed, or never reached ``HeadlessRun.exit``).
MISTRAL_NATIVE_STOP_EXIT_CODES = {
    "usage_error": 1, "config_error": 1,
    "infrastructure_failure": 2,
    "turn_limit": 3, "token_limit": 3, "price_limit": 3, "deadline": 3,
    "terminated": 3, "length": 3, "refusal": 3,
    "aborted": 4,
}
# Vibe sets ``error`` only on its failure paths (``HeadlessRun.fail``, a turn
# error, an abort message): exit 1/2 always carry one, the limit stops never
# do, ``length``/``refusal``/``aborted`` may or may not. A combination Vibe
# does not produce is not classified (the 0.9.78 token_limit pin stays exact).
MISTRAL_NATIVE_STOP_ERROR_RULE = {
    "usage_error": "required", "config_error": "required", "infrastructure_failure": "required",
    "turn_limit": "absent", "token_limit": "absent", "price_limit": "absent", "deadline": "absent",
    "terminated": "absent", "length": "optional", "refusal": "optional", "aborted": "optional",
}
MISTRAL_NATIVE_STOP_MEANING = {
    "usage_error": "the run could not start as asked (caller mistake, deterministic)",
    "config_error": "the native Vibe configuration or credential could not be loaded",
    "infrastructure_failure": "the model API failed after its retries or the runtime failed under the agent",
    "turn_limit": "the agent stopped at its turn limit",
    "token_limit": "the agent stopped at its cumulative token budget",
    "price_limit": "the agent stopped at its price limit",
    "deadline": "the agent stopped at its native time limit",
    "terminated": "the native process was told to stop",
    "length": "the model answer was cut at its output length",
    "refusal": "the model refused to continue",
    "aborted": "the agent-socket server aborted the run",
}
# Fixed categories only: the native error message is never copied into Fleet
# evidence (same policy as safe_native_stderr).
MISTRAL_NATIVE_ERROR_MARKERS = (
    "timed out", "timeout", "connection", "connect", "reset by peer", "ssl",
    "name resolution", "dns", "rate limit", "429", "500", "502", "503", "504",
    "401", "403", "unauthorized", "not authenticated", "retries", "retry",
    "stream", "empty response", "quota", "credits", "insufficient",
)
# vibe/app_server/models.py TurnStop: the only stop_reason values Vibe writes.
MISTRAL_NATIVE_STOP_REASONS = frozenset({"interrupted", "limit", "length"})
_MISTRAL_ERROR_CODE_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-")


def _pinned_native_export(export: Any, exit_code: Any) -> bool:
    return (
        type(exit_code) is int
        and isinstance(export, dict)
        and type(export.get("schema_version")) is int and export["schema_version"] == 1
        and export.get("vibe_version") == MISTRAL_NATIVE_VERSION
        and type(export.get("exit_code")) is int and export["exit_code"] == exit_code
        # The key itself is part of the contract (RunResult always serialises it);
        # a missing key is not "no error".
        and "error" in export
    )


def mistral_native_stop(export: Any, exit_code: Any) -> dict[str, Any] | None:
    """Classify a non-zero controlled native stop from the pinned export only.

    Returns None for exit 0, a missing/unpinned/inconsistent export, an outcome
    outside the pinned taxonomy, or an outcome whose fixed exit code differs
    from the observed one. The returned record holds the outcome, its meaning,
    an optional short native error code and fixed error categories; never the
    native message text.
    """
    if not _pinned_native_export(export, exit_code) or exit_code == 0:
        return None
    outcome = export.get("outcome")
    if not isinstance(outcome, str) or MISTRAL_NATIVE_STOP_EXIT_CODES.get(outcome) != exit_code:
        return None
    error = export.get("error")
    if error is not None and not isinstance(error, dict):
        return None
    rule = MISTRAL_NATIVE_STOP_ERROR_RULE[outcome]
    if (rule == "required" and error is None) or (rule == "absent" and error is not None):
        return None
    code = error.get("code") if isinstance(error, dict) else None
    if code is not None and (not isinstance(code, str) or not 1 <= len(code) <= 64
                             or not set(code) <= _MISTRAL_ERROR_CODE_CHARS):
        return None
    message = error.get("message") if isinstance(error, dict) else None
    lowered = message.casefold() if isinstance(message, str) else ""
    categories = [marker for marker in MISTRAL_NATIVE_ERROR_MARKERS if marker in lowered]
    stop_reason = export.get("stop_reason")
    return {
        "outcome": outcome,
        "exit_code": exit_code,
        "meaning": MISTRAL_NATIVE_STOP_MEANING[outcome],
        "error_code": code,
        "error_present": isinstance(error, dict),
        "error_categories": categories,
        "stop_reason": (stop_reason if isinstance(stop_reason, str)
                        and stop_reason in MISTRAL_NATIVE_STOP_REASONS else None),
        "reason": "mistral-" + outcome.replace("_", "-"),
    }


def mistral_native_stop_diagnosis(stop: dict[str, Any]) -> str:
    """Parent-visible cause of a controlled native stop (no raw provider text)."""
    label = str(stop["outcome"]).replace("_", " ").upper()
    parts = [f"MISTRAL NATIVE {label}: the native Vibe run ended with outcome "
             f"'{stop['outcome']}' (exit {stop['exit_code']}): {stop['meaning']}."]
    if stop.get("error_code"):
        parts.append(f"Native error code '{stop['error_code']}'.")
    if stop.get("error_categories"):
        parts.append("Native error categories: " + ", ".join(stop["error_categories"]) + ".")
    elif stop.get("error_present"):
        parts.append("Native error message withheld (no known category).")
    if stop.get("stop_reason"):
        parts.append(f"Native stop reason '{stop['stop_reason']}'.")
    parts.append("The worker never delivered a handoff to judge; handoff and model identity "
                 "validation still determine whether the run is failed or partial.")
    return " ".join(parts)


def mistral_export_absence_detail(
    export: Any,
    exit_code: Any,
    *,
    session: dict[str, Any] | None = None,
    idle_seconds: float | None = None,
) -> str | None:
    """Why a non-zero Mistral exit has no classified native cause, for the generic diagnosis."""
    if type(exit_code) is not int or exit_code == 0:
        return None
    summary = mistral_native_session_summary(session, idle_seconds=idle_seconds) if session is not None else None
    suffix = f"; {summary}" if summary else ""
    if export is None:
        return ("the native Vibe export (mistral-output/export.json) is missing or unreadable, so no "
                "native cause is available; the process may have ended outside Vibe's own exit path "
                "(killed, crashed or hung before its exit)" + suffix)
    if not _pinned_native_export(export, exit_code):
        return "the native Vibe export does not match the pinned run contract (version, schema or exit code)" + suffix
    return "the native Vibe export outcome is outside the pinned taxonomy" + suffix


def mistral_terminal_status(export: Any) -> tuple[str | None, str | None]:
    if not isinstance(export, dict):
        return None, "Native Vibe export missing"
    outcome = export.get("outcome")
    if export.get("error") is not None:
        return "failed", "Native Vibe reported an error (details withheld)"
    if outcome == "finished" and type(export.get("exit_code")) is int and export["exit_code"] == 0:
        if export.get("stop_reason") is not None:
            return "failed", "Native Vibe stopped without finishing"
        return "completed", None
    return "failed", "Native Vibe outcome is incomplete or inconsistent"


def mistral_terminal_text(text: str) -> str:
    value = _bounded_json(text)
    history = value.get("history") if isinstance(value, dict) else value
    if not isinstance(history, list) or len(history) > MISTRAL_MAX_HISTORY_ENTRIES:
        return ""
    for entry in reversed(history):
        if not isinstance(entry, dict) or entry.get("type") != "message" or entry.get("role") != "assistant":
            continue
        if entry.get("generationStatus", entry.get("generation_status")) != "completed":
            return ""
        content = entry.get("content")
        if not isinstance(content, list):
            return ""
        return "\n".join(block["text"] for block in content if isinstance(block, dict)
                         and block.get("type") == "text" and isinstance(block.get("text"), str))
    return ""


def safe_native_stderr(text: str) -> str:
    """Persist fixed diagnostic categories only, never arbitrary native stderr."""
    raw = text.casefold() if isinstance(text, str) else ""
    categories = []
    for marker in ("quota exhausted", "credits exhausted", "insufficient credits",
                   "rate limit", "http 429", "not authenticated", "unauthorized",
                   "configuration boundary refused"):
        if marker in raw:
            categories.append(marker)
    return "Vibe native diagnostic: " + ", ".join(categories) if categories else (
        "Vibe native diagnostic withheld" if raw.strip() else "")


def native_session_id(export: Any) -> str | None:
    import uuid
    if not isinstance(export, dict) or not isinstance(export.get("session_id"), str):
        return None
    try:
        return str(uuid.UUID(export["session_id"]))
    except ValueError:
        return None


def mistral_quota_rejection(export: Any) -> dict[str, Any] | None:
    """Only explicit native error codes; no balance guesses or generic 429."""
    if (not isinstance(export, dict) or type(export.get("schema_version")) is not int
            or export.get("schema_version") != 1
            or export.get("vibe_version") != MISTRAL_NATIVE_VERSION
            or not isinstance(export.get("outcome"), str)
            or export.get("outcome") not in {"infrastructure_failure", "usage_error"}
            or type(export.get("exit_code")) is not int or export["exit_code"] == 0):
        return None
    error = export.get("error")
    if (not isinstance(error, dict) or not isinstance(error.get("code"), str)
            or error.get("code") not in {"quota_exhausted", "credits_exhausted"}):
        return None
    return {"status": "exhausted", "scope": "native", "source": "native-provider-error",
            "reason": "vibe-native-allowance-exhausted", "reset_at": None,
            "clear_requires": "native-cli-check"}


def extract_mistral_token_usage(export: Any) -> dict[str, Any]:
    result = {"status": "unknown", "source": "mistral-native-export", "confidence": 0.0,
              "adapter": MISTRAL_ADAPTER_ID, "input_tokens": None, "fresh_input_tokens": None,
              "output_tokens": None, "cache_read_tokens": None, "cache_write_tokens": None,
              "reasoning_tokens": None, "total_tokens": None, "cost_usd": None,
              "cost_semantics": "vibe-code-pro-allowance-not-api-estimate"}
    if (not isinstance(export, dict) or type(export.get("schema_version")) is not int
            or export.get("vibe_version") != MISTRAL_NATIVE_VERSION or export.get("schema_version") != 1):
        return result
    usage = export.get("usage")
    if not isinstance(usage, dict):
        return result
    counts = [usage.get(key) for key in ("input_tokens", "output_tokens", "cached_input_tokens", "total_tokens")]
    if any(type(value) is not int or not 0 <= value <= 10**12 for value in counts):
        return result
    inputs, outputs, cached, total = counts
    if cached > inputs or total != inputs + outputs:
        return result
    return dict(result, status="known", confidence=1.0, input_tokens=inputs,
                fresh_input_tokens=inputs - cached, output_tokens=outputs,
                cache_read_tokens=cached, total_tokens=total,
                partial=export.get("outcome") != "finished")


def environment_attestation(env: dict[str, str], *, tool_policy: dict[str, Any] | None = None) -> dict[str, Any]:
    """No dump of environment/config/credentials into public Fleet evidence."""
    return {"adapter": MISTRAL_ADAPTER_ID, "status": "PREPARED",
            "native_version": MISTRAL_NATIVE_VERSION, "model": MISTRAL_NATIVE_MODEL_ID,
            "identity_evidence": "configured_native", "tools": tool_policy["tools"] if tool_policy else "none",
            "keyring": "native-presence-only", "pro_entitlement": "UNVERIFIED",
            "quota": "unknown", "payg_fallback": False,
            "environment_key_inherited": any(key.casefold() == "mistral_api_key" for key in env),
            "run_home_isolated": "VIBE_HOME" in env,
            "pre_inference_config_check": "same-native-process"}
