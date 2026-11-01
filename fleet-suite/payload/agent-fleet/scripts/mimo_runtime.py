"""MiMo Code native adapter helper (bounded, side-effect free).

This module never spawns the MiMo CLI or mutates the worktree. It reads only
the designated native auth record for validated in-memory injection and prepares
isolated native run state. It validates the MiMo integration contract that the
parent commits to and produces the literal argv vector, the hardened child
environment, the post-run identity check, and the usage/credit accounting that
the Fleet harness consumes.

The contract is MIMO-CONTRACT.md (2026-09-22). Everything the contract marks
UNVERIFIED stays fail-closed here:

* Only the native Token Plan identity is admissible: provider ``xiaomi`` (or one
  of the three documented ``xiaomi-token-plan-*`` region ids) with the exact
  native model ``mimo-v2.6-pro``. ``mimo-auto``, Flash and every other model are
  refused before a command is built.
* Only the three official Token Plan hosts are admissible endpoints. A custom
  endpoint or any PAYG marker is refused; there is never a PAYG fallback.
* Telemetry, sharing, autoupdate, ambient skills/plugins/MCP and shell/network/
  recursive task tools stay disabled through environment flags and an explicit
  permission payload. No skip-permissions flag is ever emitted.
* Credentials stay in the designated native store. Fleet validates the single
  ``xiaomi`` Token Plan API record (type ``api``, ``tp-`` key prefix, exact
  Token Plan base_url) and injects only a rebuilt copy of that record into the
  child through MIMOCODE_AUTH_CONTENT. The raw key is never logged and never
  copied into run evidence; the shared login home is never reused as the run
  home and the per-run home preseeds the native cache version.
* Usage accounting is observation-based. Empty, error, or timed-out output is
  UNKNOWN, never zero. Credits are not tokens and not USD.
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import stat
from pathlib import Path
from typing import Any, Iterable


class MiMoRuntimeError(RuntimeError):
    """Raised when a MiMo integration invariant is violated."""


class MiMoAuthError(MiMoRuntimeError):
    """Raised when the designated Token Plan login is missing or invalid.

    ``reason`` is the machine-readable readiness reason so the caller can report
    the exact blocker without exposing any credential material.
    """

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


# ---------------------------------------------------------------------------
# Pinned native contract (MIMO-CONTRACT.md).
# ---------------------------------------------------------------------------
MIMO_ADAPTER_ID = "mimo"
MIMO_NATIVE_VERSION = "0.1.15"
MIMO_NATIVE_EXECUTABLE_NAME = "mimo.exe"
MIMO_NATIVE_MODEL_ID = "mimo-v2.6-pro"
MIMO_PROVIDER_ID = "xiaomi"
MIMO_RUNTIME_PROVIDER_MODEL = f"{MIMO_PROVIDER_ID}/{MIMO_NATIVE_MODEL_ID}"

# Documented Token Plan provider ids from the upstream error.ts snapshot.
MIMO_TOKEN_PLAN_PROVIDER_IDS = (
    "xiaomi-token-plan-cn",
    "xiaomi-token-plan-ams",
    "xiaomi-token-plan-sgp",
)
MIMO_REGION_BY_PROVIDER = {
    "xiaomi-token-plan-cn": "cn",
    "xiaomi-token-plan-ams": "ams",
    "xiaomi-token-plan-sgp": "sgp",
}
# Official Token Plan hosts. Anything else is a custom endpoint and is refused.
MIMO_TOKEN_PLAN_ENDPOINTS = {
    "cn": "https://token-plan-cn.xiaomimimo.com/v1",
    "sgp": "https://token-plan-sgp.xiaomimimo.com/v1",
    "ams": "https://token-plan-ams.xiaomimimo.com/v1",
}
MIMO_ACCEPTED_PROVIDERS = frozenset({MIMO_PROVIDER_ID, *MIMO_TOKEN_PLAN_PROVIDER_IDS})

# Verified native auth contract (plugin/mimo.ts + src/auth/index.ts, tag 0.1.15):
# Auth.all() reads MIMOCODE_AUTH_CONTENT (or Global.Path.data/auth.json) and the
# MiMoAuthPlugin loader takes baseURL from auth.metadata.base_url under the
# "xiaomi" provider. Only that record is validated and injected.
MIMO_AUTH_RECORD_TYPE = "api"
MIMO_AUTH_KEY_PREFIX = "tp-"
MIMO_DESIGNATED_AUTH_ENDPOINT = MIMO_TOKEN_PLAN_ENDPOINTS["ams"]
# Upstream Global bootstrap deletes the cache recursively when cache/version is
# not 21, so every per-run home preseeds it instead of touching the user cache.
MIMO_NATIVE_CACHE_VERSION = 21
MIMO_RUN_HOME_NAME = "mimo-home"
# The native login window the owner has to complete (five minutes).
MIMO_NATIVE_AUTH_WINDOW_SECONDS = 300
MIMO_MAX_AUTH_STORE_BYTES = 1_048_576
MIMO_AUTH_UNAVAILABLE_REASONS = (
    "mimo-native-login-store-missing",
    "mimo-native-login-store-unreadable",
    "mimo-native-token-plan-auth-invalid",
    "mimo-native-token-plan-endpoint-mismatch",
)

# Models the owner explicitly excluded. The allowlist is the single source of
# truth; this set exists so a refusal can name what was rejected.
MIMO_REFUSED_MODEL_MARKERS = (
    "mimo-auto",
    "auto",
    "flash",
    "mimo-v2-6-flash",
    "mini",
    "lite",
)
MIMO_PAYG_MARKERS = (
    "payg",
    "pay-as-you-go",
    "pay_as_you_go",
    "payasyougo",
    "on-demand",
    "metered",
)
MIMO_CREDIT_MARKERS = ("credit", "credits")

# Verified environment (MIMO-CONTRACT.md). MIMOCODE_ENABLE_ANALYSIS defaults to
# true upstream, so it is pinned off on every child run.
MIMO_HARDENED_ENV = {
    "MIMOCODE_DISABLE_CLAUDE_IMPORT": "true",
    "MIMOCODE_ENABLE_ANALYSIS": "false",
    "MIMOCODE_AUTO_SHARE": "false",
    "MIMOCODE_DISABLE_SHARE": "true",
    "MIMOCODE_DISABLE_AUTOUPDATE": "true",
    "MIMOCODE_DISABLE_PRUNE": "true",
    "MIMOCODE_DISABLE_DEFAULT_PLUGINS": "true",
    "MIMOCODE_DISABLE_LSP_DOWNLOAD": "true",
    "MIMOCODE_DISABLE_EMBEDDED_WEB_UI": "true",
    "MIMOCODE_DISABLE_PROJECT_CONFIG": "true",
    "MIMOCODE_MIMO_ONLY": "true",
    "MIMOCODE_DISABLE_PROVIDER_ENV": "true",
    "MIMOCODE_DISABLE_CLAUDE_CODE_MCP": "true",
    "MIMOCODE_DISABLE_EXTERNAL_SKILLS": "true",
    "MIMOCODE_DISABLE_BUILTIN_SKILLS": "true",
    "MIMOCODE_DISABLE_OFFICIAL_SKILLS": "true",
    # MiMo docs omit OpenCode's --pure flag; the contract says set the env var.
    "MIMOCODE_PURE": "true",
    # Parent live verification 2026-09-22T20:15Z additions.
    "MIMOCODE_AUTO_APPROVE_DELETE": "false",
    "MIMOCODE_DANGEROUSLY_SKIP_PERMISSIONS": "false",
    "MIMOCODE_DISABLE_LOG_ROTATION": "true",
    "OTEL_SDK_DISABLED": "true",
}

# Ambient overrides that must never reach the child. MIMOCODE_AUTH_CONTENT is a
# secret inline auth override, MIMOCODE_CONFIG_CONTENT is an arbitrary config
# override, and the rest can redirect the binary, the endpoint, the provider or
# the permission set away from the bounded Fleet contract.
MIMO_STRIP_ENV = (
    "MIMOCODE_AUTH_CONTENT",
    "MIMOCODE_CONFIG_CONTENT",
    "MIMOCODE_PERMISSION",
    "MIMOCODE_BIN_PATH",
    "MIMOCODE_HOME",
    "MIMOCODE_CONFIG_DIR",
    "MIMOCODE_BASE_URL",
    "MIMOCODE_ENDPOINT",
    "MIMOCODE_MODEL",
    "MIMOCODE_PROVIDER",
    "MIMOCODE_API_KEY",
    "MIMOCODE_AUTH_TOKEN",
    "MIMOCODE_TOKEN",
    "MIMOCODE_OPENAI_API_KEY",
    "MIMOCODE_OPENAI_BASE_URL",
    "MIMOCODE_ANTHROPIC_API_KEY",
    "MIMOCODE_ANTHROPIC_AUTH_TOKEN",
    "MIMOCODE_ANTHROPIC_BASE_URL",
    # Another inherited auth/endpoint override documented in the contract.
    "MIMO_PLATFORM_URL",
    "MIMOCODE_PLATFORM_URL",
)
# Keys Fleet itself sets on every child run. They are stripped from the ambient
# environment first and then written back with Fleet-pinned values, so their
# presence is proof of the binding, never a leak.
MIMO_MANAGED_ENV = (
    "MIMOCODE_HOME",
    "MIMOCODE_AUTH_CONTENT",
    "MIMOCODE_CONFIG_CONTENT",
    "MIMOCODE_PERMISSION",
)
# Values that must never be written to run evidence.
MIMO_SECRET_ENV_KEYS = (
    "MIMOCODE_AUTH_CONTENT",
    "MIMOCODE_API_KEY",
    "MIMOCODE_AUTH_TOKEN",
    "MIMOCODE_TOKEN",
    "MIMOCODE_OPENAI_API_KEY",
    "MIMOCODE_ANTHROPIC_API_KEY",
    "MIMOCODE_ANTHROPIC_AUTH_TOKEN",
)

MIMO_ALLOWED_AGENTS = ("build", "plan")
MIMO_DEFAULT_AGENT = "build"

# Flags that bypass permissions, resume ambient session state, or redirect the
# run outside the bounded workspace. The parent's native help DOES expose
# --pure/--dir/--variant, but Fleet still refuses them: --dir can point the run
# outside the workspace, --variant is an unverified selector, and the contract
# pins MIMOCODE_PURE=true instead of --pure.
MIMO_UNSAFE_FLAGS = frozenset(
    {
        "--pure",
        "--variant",
        "--dir",
        "--dangerously-skip-permissions",
        "--skip-permissions",
        "--yolo",
        "--trust",
        "--trust-workspace",
        "--auto-approve",
        "--approve-all",
        "--disable-approval",
        "--continue",
        "--session",
        "--resume",
        "--share",
        "--serve",
        "--swarm",
        "--bg",
        "--remote-control",
        "--fallback-model",
        "--no-permission-check",
    }
)

# Tools the bounded worker never receives. Shell, network and recursive
# task/actor tools stay denied unless the parent proves a need. MiMo task
# subagents run through the actor tool, so actor is denied too.
MIMO_ALWAYS_DENIED_TOOLS = (
    "bash",
    "shell",
    "exec",
    "task",
    "agent",
    "actor",
    "subagent",
    "webfetch",
    "websearch",
    "web_fetch",
    "web_search",
    "computer",
    "mcp",
)
MIMO_REVIEW_ALLOWED_TOOLS = ("read", "glob", "grep", "list")
MIMO_IMPLEMENT_ALLOWED_TOOLS = ("read", "glob", "grep", "list", "edit", "write")

MIMO_COST_SEMANTICS = "mimo_token_plan_credits_not_usd_or_tokens"
MIMO_MAX_PARSE_CHARS = 4_000_000
MIMO_MAX_JSON_OBJECTS = 512

_PROVIDER_MODEL_PATTERN = re.compile(
    r"^(?P<provider>[a-z0-9][a-z0-9._-]{0,63})/(?P<model>[A-Za-z0-9][A-Za-z0-9._-]{0,63})$"
)
_LOGGER_PATTERN = re.compile(
    r"^INFO\s+\d{4}-\d{2}-\d{2}T\S+\s+[^\r\n]*\bservice=llm\s+"
    r"[^\r\n]*\bproviderID=(?P<provider>\"[^\"]+\"|\S+)"
    r"[^\r\n]*\bmodelID=(?P<model>\"[^\"]+\"|\S+)"
)
_STRUCTURED_IDENTITY_KEYS = (
    ("providerID", "modelID"),
    ("providerId", "modelId"),
    ("provider_id", "model_id"),
    ("provider", "model"),
)
_USAGE_KEY_MAP = {
    "inputtokens": "input_tokens",
    "inputtokenscount": "input_tokens",
    "prompttokens": "input_tokens",
    "outputtokens": "output_tokens",
    "completiontokens": "output_tokens",
    "cachereadtokens": "cache_read_tokens",
    "cachewritetokens": "cache_write_tokens",
    "reasoningtokens": "reasoning_tokens",
    "totaltokens": "total_tokens",
    "credits": "credits",
    "credit": "credits",
}


def _is_link(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return True
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction):
        try:
            if is_junction():
                return True
        except OSError:
            return True
    return bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value).casefold()).strip("-")


def _clean_token(value: Any) -> str:
    return str(value).strip().strip('"').rstrip(",;")


# ---------------------------------------------------------------------------
# Identity: provider/model pinning, region mapping, PAYG refusal.
# ---------------------------------------------------------------------------
def normalize_provider_model(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise MiMoRuntimeError("MiMo provider/model is required")
    return text


def require_token_plan_model(value: Any, *, label: str = "MiMo model") -> str:
    """Pin the exact Token Plan Pro identity and refuse everything else.

    Accepts ``mimo-v2.6-pro`` and ``<accepted-provider>/mimo-v2.6-pro``. Any
    other model (``mimo-auto``, Flash, another provider's model) or any PAYG
    marker is refused before a command can be built.
    """
    text = normalize_provider_model(value)
    provider: str | None = None
    model = text
    if "/" in text:
        match = _PROVIDER_MODEL_PATTERN.fullmatch(text)
        if match is None:
            raise MiMoRuntimeError(
                f"{label} must be <provider>/<model> with safe characters: {text!r}"
            )
        provider = match.group("provider")
        model = match.group("model")
    lowered = model.casefold()
    for marker in MIMO_PAYG_MARKERS:
        if marker in lowered or marker in (provider or "").casefold():
            raise MiMoRuntimeError(
                f"{label} refused: PAYG or metered billing is never authorised"
            )
    if provider is not None and provider not in MIMO_ACCEPTED_PROVIDERS:
        raise MiMoRuntimeError(
            f"{label} refused: provider {provider!r} is not a documented "
            "MiMo Token Plan provider id"
        )
    if model != MIMO_NATIVE_MODEL_ID:
        refused = next(
            (marker for marker in MIMO_REFUSED_MODEL_MARKERS if marker in lowered),
            None,
        )
        detail = f" ({refused} is explicitly excluded)" if refused else ""
        raise MiMoRuntimeError(
            f"{label} refused: only native {MIMO_NATIVE_MODEL_ID} is "
            f"authorised, got {model!r}{detail}"
        )
    return f"{provider or MIMO_PROVIDER_ID}/{MIMO_NATIVE_MODEL_ID}"


def require_token_plan_endpoint(
    value: Any, *, label: str = "MiMo endpoint"
) -> str | None:
    """Accept only the official Token Plan hosts; refuse custom endpoints."""
    if value is None:
        return None
    text = str(value).strip().rstrip("/")
    if not text:
        return None
    lowered = text.casefold()
    for marker in MIMO_PAYG_MARKERS:
        if marker in lowered:
            raise MiMoRuntimeError(
                f"{label} refused: PAYG or metered endpoint is never authorised"
            )
    if text not in {item for item in MIMO_TOKEN_PLAN_ENDPOINTS.values()}:
        raise MiMoRuntimeError(
            f"{label} refused: custom endpoints are not authorised; only the "
            "official Token Plan hosts are admissible"
        )
    return text


def region_for_provider(provider: str) -> str | None:
    return MIMO_REGION_BY_PROVIDER.get(str(provider).strip())


def region_for_endpoint(endpoint: str | None) -> str | None:
    if endpoint is None:
        return None
    text = str(endpoint).strip().rstrip("/").casefold()
    for region, host in MIMO_TOKEN_PLAN_ENDPOINTS.items():
        if text == host.rstrip("/").casefold():
            return region
    return None


def bind_token_plan_region(
    provider_model: str, endpoint: str | None
) -> tuple[str | None, str]:
    """Bind the declared provider/model and endpoint to one Token Plan region.

    The MiMo run argv has no endpoint flag, so the region is bound through the
    authenticated provider selection, not through a CLI switch. This function
    proves the two declarations agree and reports how weak that binding is:
    ``provider-id`` when the provider id itself names the region,
    ``declared-endpoint-only`` when only the built-in ``xiaomi`` catalog id is
    available, and ``unbound`` when neither side pins a region.
    """
    provider = str(provider_model).split("/", 1)[0].strip()
    provider_region = region_for_provider(provider)
    endpoint_region = region_for_endpoint(endpoint)
    if provider_region is not None and endpoint_region is not None:
        if provider_region != endpoint_region:
            raise MiMoRuntimeError(
                "MiMo region binding refused: provider "
                f"{provider!r} maps to region {provider_region!r} but the "
                f"declared endpoint maps to {endpoint_region!r}"
            )
        return provider_region, "provider-id"
    if provider_region is not None:
        return provider_region, "provider-id"
    if endpoint_region is not None:
        return endpoint_region, "declared-endpoint-only"
    return None, "unbound"


def require_mimo_agent(value: Any) -> str:
    agent = str(value or MIMO_DEFAULT_AGENT).strip()
    if agent not in MIMO_ALLOWED_AGENTS:
        raise MiMoRuntimeError(
            f"MiMo agent must be one of {', '.join(MIMO_ALLOWED_AGENTS)}"
        )
    return agent


# ---------------------------------------------------------------------------
# Profile contract (validated by fleet.load_profiles).
# ---------------------------------------------------------------------------
def validate_credential_store(raw: Any) -> dict[str, Any]:
    """Validate the designated native login store declaration.

    The store is read only to validate and rebuild the single ``xiaomi`` Token
    Plan API record (plugin/mimo.ts + auth/index.ts, tag 0.1.15). Fleet never
    logs the key, never copies the store, and never reuses that home as the run
    home. The declared path is the one the parent designated for the login.
    """
    if not isinstance(raw, dict):
        raise MiMoRuntimeError("mimo.credential_store must be an object")
    path = str(raw.get("path", "")).strip()
    if not path:
        raise MiMoRuntimeError("mimo.credential_store.path is required")
    if set(raw) - {"path", "note"}:
        raise MiMoRuntimeError(
            "mimo.credential_store supports only path and note: "
            + ", ".join(sorted(set(raw) - {"path", "note"}))
        )
    return {
        "path": path,
        **({"note": raw["note"]} if "note" in raw else {}),
    }


def validate_mimo_profile(raw: Any) -> dict[str, Any]:
    """Validate and normalise the ``mimo`` block of a MiMo profile."""
    if not isinstance(raw, dict):
        raise MiMoRuntimeError("mimo profile block must be an object")
    allowed = {
        "token_plan_only",
        "credential_store",
        "native_agent",
        "provider_model",
        "endpoint",
        "note",
        "region",
        "endpoint_binding",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise MiMoRuntimeError(
            "mimo profile block has unsupported keys: " + ", ".join(unknown)
        )
    if raw.get("token_plan_only") is not True:
        raise MiMoRuntimeError(
            "mimo.token_plan_only must be true: the owner forbids PAYG fallback"
        )
    provider_model = require_token_plan_model(
        raw.get("provider_model", MIMO_RUNTIME_PROVIDER_MODEL),
        label="mimo.provider_model",
    )
    endpoint = require_token_plan_endpoint(
        raw.get("endpoint"), label="mimo.endpoint"
    )
    region, binding = bind_token_plan_region(provider_model, endpoint)
    for key, expected in (("region", region), ("endpoint_binding", binding)):
        if key in raw and raw[key] != expected:
            raise MiMoRuntimeError(f"mimo.{key} does not match the native endpoint binding")
    return {
        "token_plan_only": True,
        "credential_store": validate_credential_store(raw.get("credential_store")),
        "native_agent": require_mimo_agent(raw.get("native_agent")),
        "provider_model": provider_model,
        "endpoint": endpoint,
        "region": region,
        "endpoint_binding": binding,
    }


def mimo_profile_block(profile: dict[str, Any]) -> dict[str, Any]:
    """Return the validated ``mimo`` contract block of a profile."""
    block = profile.get("mimo")
    if not isinstance(block, dict):
        raise MiMoRuntimeError("MiMo profile is missing its mimo contract block")
    return block


def mimo_provider_model(profile: dict[str, Any]) -> str:
    block = mimo_profile_block(profile)
    return require_token_plan_model(
        block.get("provider_model", MIMO_RUNTIME_PROVIDER_MODEL),
        label="MiMo provider model",
    )


def credential_store_path(profile: dict[str, Any]) -> Path:
    block = profile.get("mimo")
    if not isinstance(block, dict):
        raise MiMoRuntimeError("MiMo profile is missing its mimo contract block")
    store = block.get("credential_store")
    if not isinstance(store, dict):
        raise MiMoRuntimeError("MiMo profile is missing its credential store")
    return Path(
        os.path.expanduser(os.path.expandvars(str(store["path"])))
    )


def _auth_endpoint(profile: dict[str, Any]) -> str:
    """The pinned Token Plan endpoint the auth record has to match."""
    block = profile.get("mimo")
    endpoint = None
    if isinstance(block, dict):
        endpoint = require_token_plan_endpoint(
            block.get("endpoint"), label="mimo.endpoint"
        )
    return endpoint or MIMO_DESIGNATED_AUTH_ENDPOINT


def read_designated_auth_store(path: Path) -> Any:
    """Read the designated native login store without ever logging it."""
    try:
        if not path.is_file():
            raise MiMoAuthError(
                "MiMo native login store is missing",
                "mimo-native-login-store-missing",
            )
        if _is_link(path) or any(_is_link(parent) for parent in path.parents):
            raise MiMoAuthError(
                "MiMo auth store path must not contain filesystem links",
                "mimo-native-login-store-unreadable",
            )
        size = path.stat().st_size
        if size <= 0 or size > MIMO_MAX_AUTH_STORE_BYTES:
            raise MiMoAuthError(
                "MiMo native login store has an unexpected size",
                "mimo-native-login-store-unreadable",
            )
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MiMoAuthError(
            f"MiMo native login store is unreadable ({type(exc).__name__})",
            "mimo-native-login-store-unreadable",
        ) from exc
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError, RecursionError):
        raise MiMoAuthError(
            "MiMo native login store is not valid JSON",
            "mimo-native-token-plan-auth-invalid",
        ) from None


def validate_auth_record(raw: Any, *, expected_endpoint: str) -> dict[str, Any]:
    """Validate the designated ``xiaomi`` Token Plan API record.

    Only the documented record shape is accepted: ``type`` ``api``, an API key
    with the ``tp-`` Token Plan prefix and ``metadata.base_url`` equal to the
    pinned Token Plan host. The returned record is rebuilt from the validated
    fields alone, so unknown or foreign auth data is never injected into the
    child. The key is returned for injection only and must never be logged.
    """
    if not isinstance(raw, dict):
        raise MiMoAuthError(
            "MiMo auth store is not a JSON object",
            "mimo-native-token-plan-auth-invalid",
        )
    record = raw.get(MIMO_PROVIDER_ID)
    if not isinstance(record, dict):
        raise MiMoAuthError(
            "MiMo auth store has no xiaomi Token Plan record",
            "mimo-native-token-plan-auth-invalid",
        )
    if str(record.get("type", "")).strip() != MIMO_AUTH_RECORD_TYPE:
        raise MiMoAuthError(
            "MiMo auth record type must be api",
            "mimo-native-token-plan-auth-invalid",
        )
    key = record.get("key")
    if not isinstance(key, str) or not key.strip():
        raise MiMoAuthError(
            "MiMo auth record has no usable API key",
            "mimo-native-token-plan-auth-invalid",
        )
    key = key.strip()
    if not key.startswith(MIMO_AUTH_KEY_PREFIX):
        raise MiMoAuthError(
            "MiMo auth key is not a Token Plan key",
            "mimo-native-token-plan-auth-invalid",
        )
    if any(marker in key.casefold() for marker in MIMO_PAYG_MARKERS):
        raise MiMoAuthError(
            "MiMo auth key carries a PAYG marker",
            "mimo-native-token-plan-auth-invalid",
        )
    metadata = record.get("metadata")
    if not isinstance(metadata, dict):
        raise MiMoAuthError(
            "MiMo auth record has no metadata",
            "mimo-native-token-plan-auth-invalid",
        )
    try:
        endpoint = require_token_plan_endpoint(
            metadata.get("base_url"), label="MiMo auth base_url"
        )
    except MiMoRuntimeError:
        raise MiMoAuthError(
            "MiMo auth base_url is not an official Token Plan host",
            "mimo-native-token-plan-endpoint-mismatch",
        ) from None
    if endpoint != expected_endpoint:
        raise MiMoAuthError(
            "MiMo auth base_url does not match the pinned Token Plan endpoint",
            "mimo-native-token-plan-endpoint-mismatch",
        )
    rebuilt: dict[str, Any] = {
        "type": MIMO_AUTH_RECORD_TYPE,
        "key": key,
        "metadata": {"base_url": endpoint},
    }
    uid = metadata.get("uid")
    if isinstance(uid, str) and uid.strip():
        rebuilt["metadata"]["uid"] = uid.strip()
    return rebuilt


def _auth_unavailable_status(path: str, reason: str) -> dict[str, Any]:
    return {
        "required": True,
        "ready": False,
        "reason": reason,
        "path": path,
        "contents_inspected": False,
        "auth_schema_known": True,
        "auth_record_validated": False,
        "credential_source": "designated-native-home",
        "injection_mode": "validated-inline-auth-content",
        "native_auth_window_seconds": MIMO_NATIVE_AUTH_WINDOW_SECONDS,
    }


def load_native_auth(profile: dict[str, Any]) -> dict[str, Any]:
    """Validate the designated Token Plan login and build the injection payload.

    Returns ``{"status": <redacted evidence>, "auth_content": <inline JSON>}``.
    The shared login home is never copied: only the validated ``xiaomi`` record
    is rebuilt for MIMOCODE_AUTH_CONTENT, and the status dict carries no key.
    """
    path = credential_store_path(profile).expanduser()
    try:
        raw = read_designated_auth_store(path)
        record = validate_auth_record(raw, expected_endpoint=_auth_endpoint(profile))
    except MiMoAuthError as exc:
        raise MiMoAuthError(str(exc), str(exc.reason)) from exc
    except MiMoRuntimeError as exc:
        raise MiMoAuthError(str(exc), "mimo-native-token-plan-auth-invalid") from exc
    ignored = sorted(
        str(key)
        for key in (raw if isinstance(raw, dict) else {})
        if str(key) != MIMO_PROVIDER_ID
    )
    auth_content = json.dumps(
        {MIMO_PROVIDER_ID: record},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    status = {
        "required": True,
        "ready": True,
        "reason": "mimo-native-token-plan-auth-valid",
        "path": str(path),
        "contents_inspected": True,
        "auth_schema_known": True,
        "auth_record_validated": True,
        "credential_source": "designated-native-home",
        "injection_mode": "validated-inline-auth-content",
        "native_auth_window_seconds": MIMO_NATIVE_AUTH_WINDOW_SECONDS,
        "auth_evidence": {
            "provider": MIMO_PROVIDER_ID,
            "type": MIMO_AUTH_RECORD_TYPE,
            "key_prefix": MIMO_AUTH_KEY_PREFIX,
            "uid_present": "uid" in record["metadata"],
            "base_url": record["metadata"]["base_url"],
            "ignored_store_providers": ignored,
        },
    }
    return {"status": status, "auth_content": auth_content}


def credential_status(profile: dict[str, Any]) -> dict[str, Any]:
    """Validated check of the designated native Token Plan login.

    Returns ``ready=False`` with an exact reason when the store is missing,
    unreadable, or does not carry a valid Token Plan API record, so Fleet
    refuses before any paid inference instead of failing mid-run. The status is
    redacted evidence only: it never carries the API key.
    """
    try:
        path = credential_store_path(profile).expanduser()
    except MiMoRuntimeError:
        return _auth_unavailable_status("", "mimo-native-login-store-missing")
    try:
        return load_native_auth(profile)["status"]
    except MiMoAuthError as exc:
        return _auth_unavailable_status(str(path), str(exc.reason))
    except MiMoRuntimeError:
        return _auth_unavailable_status(
            str(path), "mimo-native-token-plan-auth-invalid"
        )


def require_native_login(profile: dict[str, Any]) -> dict[str, Any]:
    """Validate the designated Token Plan login or refuse the paid run."""
    try:
        return load_native_auth(profile)
    except MiMoRuntimeError as exc:
        raise MiMoRuntimeError(
            "MiMo native Token Plan login is not ready: "
            + str(exc)
            + " Complete the native Token Plan login in MiMo Code (the "
            + str(MIMO_NATIVE_AUTH_WINDOW_SECONDS)
            + "s native auth window applies) and retry; Fleet refuses to start "
            "a paid run without a validated xiaomi Token Plan record"
        ) from exc


# ---------------------------------------------------------------------------
# Native executable guard
# ---------------------------------------------------------------------------
def require_mimo_native_executable(executable: str) -> None:
    """Require the installed, version-pinned native Windows executable."""
    if os.name != "nt":
        raise MiMoRuntimeError(
            "MiMo native transport requires Windows"
        )
    path = Path(executable)
    if not path.is_absolute() or not path.is_file():
        raise MiMoRuntimeError(
            "MiMo must use its installed native Windows executable; PATH "
            "lookup and shell bridges are refused"
        )
    if path.name != MIMO_NATIVE_EXECUTABLE_NAME:
        raise MiMoRuntimeError(
            f"MiMo executable must be {MIMO_NATIVE_EXECUTABLE_NAME}, got {path.name}"
        )
    if path.parent.name != MIMO_NATIVE_VERSION:
        raise MiMoRuntimeError(
            f"MiMo executable must live in the versioned {MIMO_NATIVE_VERSION} "
            f"install directory, got {path.parent.name}"
        )
    if _is_link(path) or any(_is_link(parent) for parent in path.parents):
        raise MiMoRuntimeError(
            "MiMo executable path must not contain filesystem links"
        )
    if any(path.parent.glob("mimo.exe.old_*")):
        raise MiMoRuntimeError("MiMo upgrade leftovers would be deleted by native startup; launch refused")
    with path.open("rb") as binary:
        observed_hash = hashlib.file_digest(binary, "sha256").hexdigest()
    if observed_hash != "ea706738d7f00ace7e63ea4ba7e55c738730b960c330fd212c17654131e99a4c":
        raise MiMoRuntimeError("MiMo executable does not match the pinned official 0.1.15 package")


# ---------------------------------------------------------------------------
# Permission payload and child environment
# ---------------------------------------------------------------------------
def build_permission_payload(mode: str, *, chrome_tool_names: tuple[str, ...] = ()) -> dict[str, Any]:
    """Mode-scoped permission payload; shell/network/task tools stay denied."""
    if mode not in {"advisory", "review", "implement"}:
        raise MiMoRuntimeError(f"MiMo mode is invalid: {mode}")
    if mode == "implement":
        allowed = MIMO_IMPLEMENT_ALLOWED_TOOLS
    else:
        allowed = MIMO_REVIEW_ALLOWED_TOOLS
    payload: dict[str, Any] = {"*": "deny"}
    for tool in allowed:
        payload[tool] = "allow"
    for tool in MIMO_ALWAYS_DENIED_TOOLS:
        payload[tool] = "deny"
    # MiMo 0.1.15 MCP.tools uses sanitize(server) + "_" + sanitize(tool),
    # unlike Claude's mcp__server__tool conversation namespace.
    allowed_names = {"fleet-chrome-relay_" + name for name in ("chrome_attachment", "chrome_open", "chrome_read", "chrome_click", "chrome_type", "chrome_wait", "chrome_close", "chrome_saved_login")}
    if mode == "review":
        allowed_names -= {"fleet-chrome-relay_" + name for name in ("chrome_click", "chrome_type", "chrome_saved_login")}
    elif mode != "implement":
        allowed_names = set()
    if any(type(name) is not str or name not in allowed_names for name in chrome_tool_names):
        raise MiMoRuntimeError("MiMo managed Chrome permission name is invalid")
    for name in chrome_tool_names:
        payload[name] = "allow"
    return payload


def permission_json(mode: str, *, chrome_tool_names: tuple[str, ...] = ()) -> str:
    return json.dumps(
        build_permission_payload(mode, chrome_tool_names=chrome_tool_names),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def prepare_mimo_run_home(run_dir: Path) -> Path:
    """Create the per-run MiMo home and preseed the native cache version.

    The shared native login home is never reused as the run home and never
    copied: the child gets a fresh MIMOCODE_HOME whose ``cache/version`` is
    preseeded to 21, so the upstream Global bootstrap cannot recursively delete
    the user cache. Everything is written inside the run directory.
    """
    home = Path(run_dir) / MIMO_RUN_HOME_NAME
    cache = home / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "version").write_text(
        str(MIMO_NATIVE_CACHE_VERSION), encoding="utf-8"
    )
    return home


MIMO_AGENT_STEPS = {"advisory": 8, "review": 16, "implement": 24}


def mimo_config_payload(
    mode: str,
    *,
    provider_model: str,
    endpoint: str,
    chrome_assignment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Pinned config; web tools are opt-in and exactly assigned."""
    if mode not in MIMO_AGENT_STEPS:
        raise MiMoRuntimeError(f"MiMo mode is invalid: {mode}")
    chrome_tool_names: tuple[str, ...] = ()
    mcp: dict[str, Any] = {}
    if chrome_assignment is not None:
        if mode not in {"review", "implement"}:
            raise MiMoRuntimeError("MiMo managed Chrome requires review or implement mode")
        try:
            import fleet_chrome_integration as chrome
            chrome.validate_fleet_assignment(chrome_assignment, "mimo")
            if chrome_assignment["mode"] != mode:
                raise ValueError("mode mismatch")
            chrome_tool_names = tuple(chrome.SERVER + "_" + name for name in chrome.mcp_tool_names(chrome_assignment))
            mcp["fleet-chrome-relay"] = chrome.mimo_mcp_config_entry(chrome_assignment)
        except (ImportError, ValueError) as exc:
            raise MiMoRuntimeError("MiMo managed Chrome assignment is invalid") from exc
    permission = build_permission_payload(mode, chrome_tool_names=chrome_tool_names)
    return {
        "enabled_providers": [MIMO_PROVIDER_ID],
        "model": require_token_plan_model(
            provider_model, label="MiMo config model"
        ),
        "small_model": require_token_plan_model(
            provider_model, label="MiMo config small model"
        ),
        "provider": {
            MIMO_PROVIDER_ID: {
                "options": {
                    "baseURL": require_token_plan_endpoint(
                        endpoint, label="MiMo config baseURL"
                    )
                    or MIMO_DESIGNATED_AUTH_ENDPOINT
                }
            }
        },
        "plugin": [],
        "mcp": mcp,
        "snapshot": False,
        "share": "disabled",
        "instructions": [],
        # MiMo 0.1.15 accepts OpenCode's native agent.steps control. Every
        # invocation gets a fresh isolated home, so this bounds repeated
        # context replay inside one paid Token Plan task.
        "agent": {"build": {"steps": MIMO_AGENT_STEPS[mode]}},
        "permission": permission,
    }


def mimo_config_content(mode: str, *, provider_model: str, endpoint: str, chrome_assignment: dict[str, Any] | None = None) -> str:
    return json.dumps(
        mimo_config_payload(mode, provider_model=provider_model, endpoint=endpoint, chrome_assignment=chrome_assignment),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def harden_mimo_environment(
    env: dict[str, str],
    *,
    mode: str,
    run_home: str | None = None,
    auth_content: str | None = None,
    provider_model: str | None = None,
    endpoint: str | None = None,
    chrome_assignment: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Return a child environment with ambient MiMo overrides removed.

    Every run gets an isolated MIMOCODE_HOME (never the shared login home), the
    pinned native config, the telemetry/sharing/plugin disables and the
    mode-scoped permission payload. ``auth_content`` is the rebuilt Token Plan
    record produced by :func:`load_native_auth`; it lives in the child
    environment only and never reaches run evidence.
    """
    if mode not in {"advisory", "review", "implement"}:
        raise MiMoRuntimeError(f"MiMo mode is invalid: {mode}")
    strip = {item.upper() for item in MIMO_STRIP_ENV}
    hardened = {
        key: value for key, value in env.items() if key.upper() not in strip
    }
    hardened.update(MIMO_HARDENED_ENV)
    chrome_tool_names: tuple[str, ...] = ()
    if chrome_assignment is not None:
        try:
            import fleet_chrome_integration as chrome
            chrome.validate_fleet_assignment(chrome_assignment, "mimo")
            if mode not in {"implement", "review"} or chrome_assignment["mode"] != mode:
                raise ValueError("mode mismatch")
            chrome_tool_names = tuple(chrome.SERVER + "_" + name for name in chrome.mcp_tool_names(chrome_assignment))
        except (ImportError, ValueError) as exc:
            raise MiMoRuntimeError("MiMo managed Chrome assignment is invalid") from exc
    hardened["MIMOCODE_PERMISSION"] = permission_json(mode, chrome_tool_names=chrome_tool_names)
    hardened["MIMOCODE_CONFIG_CONTENT"] = mimo_config_content(
        mode,
        provider_model=provider_model or MIMO_RUNTIME_PROVIDER_MODEL,
        endpoint=endpoint or MIMO_DESIGNATED_AUTH_ENDPOINT,
        chrome_assignment=chrome_assignment,
    )
    if run_home is not None:
        hardened["MIMOCODE_HOME"] = str(run_home)
    if auth_content is not None:
        hardened["MIMOCODE_AUTH_CONTENT"] = str(auth_content)
    return hardened


def injected_output_secret(env: dict[str, str]) -> str:
    """Return the validated in-memory key for exact output redaction only."""
    try:
        record = json.loads(env["MIMOCODE_AUTH_CONTENT"])[MIMO_PROVIDER_ID]
        key = record["key"]
        if record["type"] != "api" or not isinstance(key, str) or not key.startswith("tp-"):
            raise ValueError("invalid native record")
        return key
    except (KeyError, TypeError, ValueError) as exc:
        raise MiMoRuntimeError("Missing validated MiMo key for output redaction") from exc


def redact_mimo_environment(env: dict[str, str]) -> dict[str, str]:
    """Return a log-safe copy of a MiMo environment.

    Used before any environment snapshot reaches run evidence.
    """
    secret_keys = {key.upper() for key in MIMO_SECRET_ENV_KEYS}
    return {
        key: ("<redacted>" if key.upper() in secret_keys else value)
        for key, value in env.items()
    }


def _config_binding(env: dict[str, str]) -> dict[str, Any] | None:
    """Redacted view of the pinned native config, when it is parseable."""
    raw = env.get("MIMOCODE_CONFIG_CONTENT")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(parsed, dict):
        return None
    providers = parsed.get("provider")
    options = providers.get(MIMO_PROVIDER_ID) if isinstance(providers, dict) else None
    base_url = None
    if isinstance(options, dict) and isinstance(options.get("options"), dict):
        value = options["options"].get("baseURL")
        base_url = value if isinstance(value, str) else None
    enabled = parsed.get("enabled_providers")
    return {
        "enabled_providers": enabled if isinstance(enabled, list) else None,
        "model": parsed.get("model"),
        "small_model": parsed.get("small_model"),
        "base_url": base_url,
    }


def mimo_environment_attestation(
    env: dict[str, str], *, mode: str, chrome_assignment: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Redacted, bounded evidence that a MiMo child environment is hardened.

    Only MIMOCODE_*/OTEL keys are reported and secret-valued keys are replaced,
    so the record proves the hardening without carrying any credential. An
    injected auth record without an isolated MIMOCODE_HOME is UNVERIFIED: the
    credential must never land in a shared home.
    """
    redacted = redact_mimo_environment(env)
    reported = {
        key: value
        for key, value in redacted.items()
        if key.upper().startswith("MIMOCODE_") or key.upper().startswith("OTEL_")
    }
    strip = {item.upper() for item in MIMO_STRIP_ENV}
    managed = {item.upper() for item in MIMO_MANAGED_ENV}
    leaked = sorted(
        key
        for key in env
        if key.upper() in strip and key.upper() not in managed
    )
    auth_present = "MIMOCODE_AUTH_CONTENT" in env
    home_isolated = "MIMOCODE_HOME" in env
    verified = not leaked and (not auth_present or home_isolated)
    chrome_tool_names: tuple[str, ...] = ()
    if chrome_assignment is not None:
        try:
            import fleet_chrome_integration as chrome
            chrome.validate_fleet_assignment(chrome_assignment, "mimo")
            if mode not in {"implement", "review"} or chrome_assignment["mode"] != mode:
                raise ValueError("mode mismatch")
            chrome_tool_names = tuple(chrome.SERVER + "_" + name for name in chrome.mcp_tool_names(chrome_assignment))
        except (ImportError, ValueError):
            verified = False
    return {
        "status": "VERIFIED" if verified else "UNVERIFIED",
        "adapter": MIMO_ADAPTER_ID,
        "mode": mode,
        "permission_payload": build_permission_payload(mode, chrome_tool_names=chrome_tool_names),
        "managed_chrome": chrome_assignment is not None and bool(chrome_tool_names),
        "stripped_overrides_present": leaked,
        "auth_injection": (
            "validated-inline-auth-content" if auth_present else "absent"
        ),
        "run_home_isolated": home_isolated,
        "config_binding": _config_binding(env),
        "environment": reported,
    }


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------
def build_command(
    executable: str,
    *,
    provider_model: str,
    mode: str,
    agent: str,
    title: str,
) -> list[str]:
    """Construct the literal argv vector for ``mimo run``.

    Only documented MiMo flags are emitted. OpenCode-only flags
    (``--pure``/``--variant``/``--dir``) are refused: the MiMo docs omit them and
    the contract pins cwd plus ``MIMOCODE_PURE=true`` instead. The task travels
    over stdin, never through argv, so a large bounded prompt cannot hit the
    Windows command-line limit or land in process listings.
    """
    if mode not in {"advisory", "review", "implement"}:
        raise MiMoRuntimeError(f"MiMo mode is invalid: {mode}")
    command = [
        executable,
        "run",
        # Global flags (same CLI family as the verified OpenCode run argv).
        "--print-logs",
        "--log-level",
        "INFO",
        "--model",
        require_token_plan_model(provider_model, label="MiMo --model"),
        "--agent",
        require_mimo_agent(agent),
        "--format",
        "json",
        "--title",
        str(title).strip() or "Agent Fleet bounded MiMo run",
    ]
    refuse_unsafe_mimo_command(command)
    return command


def refuse_unsafe_mimo_command(command: Iterable[str]) -> None:
    sequence = list(command)
    for token in sequence:
        if token in MIMO_UNSAFE_FLAGS:
            raise MiMoRuntimeError(
                f"MiMo command contains a categorically unsafe flag: {token}"
            )
    if "--permission" in sequence:
        raise MiMoRuntimeError(
            "MiMo permissions are set through MIMOCODE_PERMISSION only"
        )


# ---------------------------------------------------------------------------
# Output parsing (structured events only; never model self-report prose)
# ---------------------------------------------------------------------------
_STRUCTURED_CANDIDATE_PATTERN = re.compile(r'[{\["]')


def mimo_events(text: str) -> list[dict[str, Any]]:
    """Parse MiMo ``--format json`` output into structured events.

    Accepts a JSON array, a single JSON object, or newline-delimited JSON.
    Non-JSON lines are ignored so anchored INFO logger lines survive alongside
    structured events.
    """
    raw = text[:MIMO_MAX_PARSE_CHARS] if isinstance(text, str) else ""
    if not raw.strip():
        return []
    events: list[dict[str, Any]] = []
    stripped = raw.strip()
    if stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
        except (json.JSONDecodeError, RecursionError, ValueError):
            parsed = None
        if isinstance(parsed, list):
            events.extend(row for row in parsed if isinstance(row, dict))
            return events[:MIMO_MAX_JSON_OBJECTS]
    decoder = json.JSONDecoder()
    index = 0
    length = len(raw)
    while index < length and len(events) < MIMO_MAX_JSON_OBJECTS:
        match = _STRUCTURED_CANDIDATE_PATTERN.search(raw, index)
        if match is None:
            break
        index = match.start()
        try:
            value, end = decoder.raw_decode(raw, index)
        except (json.JSONDecodeError, RecursionError, ValueError):
            index += 1
            continue
        if isinstance(value, dict):
            events.append(value)
        index = end if end > index else index + 1
    return events


def _event_type(event: dict[str, Any]) -> str:
    return str(
        event.get("payload_type")
        or event.get("type")
        or event.get("event")
        or ""
    ).casefold()


def _is_tool_event(event: dict[str, Any]) -> bool:
    event_type = _event_type(event)
    return any(
        marker in event_type
        for marker in ("tool", "bash", "shell", "exec", "patch", "file_edit")
    )


def _event_text(event: dict[str, Any]) -> str | None:
    part = event.get("part")
    if isinstance(part, dict):
        for key in ("text", "content", "output"):
            value = part.get(key)
            if isinstance(value, str) and value.strip():
                return value
    for key in ("text", "content", "output", "result", "message"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def mimo_terminal_text(text: str) -> str:
    """Join assistant text fragments the native runtime actually reported.

    Tool results, tool calls and progress chatter are never treated as the
    model's answer, so a handoff cannot be recovered from tool output.
    """
    fragments: list[str] = []
    for event in mimo_events(text):
        if _is_tool_event(event):
            continue
        event_type = _event_type(event)
        part = event.get("part")
        role = str(
            (part.get("role") if isinstance(part, dict) else None)
            or event.get("role")
            or ""
        ).casefold()
        if role and role != "assistant" and event_type not in {
            "text",
            "assistant",
            "message",
            "result",
        }:
            continue
        value = _event_text(event)
        if value:
            # Some native answers echo the JSON schema's harmless type marker.
            # Remove only this exact annotation from a complete handoff shape;
            # preserve every other field for the normal strict validator.
            try:
                obj = json.loads(value)
                required = {"status", "summary", "files_changed", "verification", "risks", "next_steps"}
                if isinstance(obj, dict) and obj.get("type") == "object" and required <= obj.keys():
                    obj.pop("type")
                    value = json.dumps(obj, ensure_ascii=False)
            except (ValueError, TypeError):
                pass
            fragments.append(value)
    return "\n".join(fragments)


MIMO_SHUTDOWN_ABORT_LINE = re.compile(
    r"service=session\.processor\b.*\berror=Aborted process$"
)


def mimo_shutdown_abort_only(stdout: Any, stderr: Any) -> bool:
    """True when every abort on stderr is the post-answer shutdown abort.

    MiMo 0.1.15 starts an internal ``checkpoint-writer`` subagent after the
    model's turn; disposing the instance while it still streams logs
    ``ERROR service=session.processor ... error=Aborted process`` although
    the process exits 0 and the answer is already on stdout. Only a stream
    whose native terminal status is ``completed`` qualifies; an abort before
    any completed answer, a native error, or any other abort wording keeps
    the transport marker. Measured 2026-10-18 on 33 stack runs
    (HD-20261018-1abe4d): 11 carried that exact line, none another abort.
    """
    if not isinstance(stdout, str) or not isinstance(stderr, str):
        return False
    terminal_reason, terminal_error = mimo_terminal_status(stdout)
    if terminal_reason != "completed" or terminal_error is not None:
        return False
    abort_lines = [
        line.strip() for line in stderr.splitlines() if "abort" in line.casefold()
    ]
    if not abort_lines:
        return False
    return all(MIMO_SHUTDOWN_ABORT_LINE.search(line) for line in abort_lines)


def mimo_terminal_status(text: str) -> tuple[str | None, str | None]:
    """Return ``(terminal_reason, error)`` observed in native output.

    Unknown stays unknown: an empty or unrecognised stream returns
    ``(None, None)`` so the caller rejects the run instead of inventing a
    successful completion.
    """
    for event in mimo_events(text):
        error = event.get("error")
        if isinstance(error, dict):
            data = error.get("data")
            nested_message = data.get("message") if isinstance(data, dict) else None
            message = (
                nested_message.strip()
                if isinstance(nested_message, str) and nested_message.strip()
                else (error.get("message") or error.get("name") or "provider error")
            )
            return "failed", str(message)
        if isinstance(error, str) and error.strip():
            return "failed", error.strip()
        status = event.get("status")
        if isinstance(status, str) and status.strip():
            normalized = status.strip().casefold()
            if normalized in {"error", "failed", "failure", "cancelled", "canceled"}:
                return "failed", f"MiMo reported status {status.strip()}"
            if normalized in {
                "completed",
                "complete",
                "success",
                "succeeded",
                "ok",
                "done",
            }:
                return "completed", None
        part = event.get("part")
        if isinstance(part, dict):
            reason = part.get("reason")
            if isinstance(reason, str) and reason.strip():
                normalized = reason.strip().casefold()
                if normalized in {"error", "failed", "cancelled", "canceled", "abort"}:
                    return "failed", f"MiMo stopped with reason {reason.strip()}"
                if normalized in {"stop", "end_turn", "completed", "success"}:
                    return "completed", None
    return None, None


# ---------------------------------------------------------------------------
# Native Token Plan refusal (HTTP 429 "quota exhausted")
# ---------------------------------------------------------------------------
MIMO_QUOTA_EXHAUSTED_REASON = "mimo-token-plan-quota-exhausted-http429"
# The only accepted shape is the native ``service=llm`` error line of the MiMo
# logger (``--print-logs``), never model prose, tool output or a quoted sample.
# The JSON after ``error=`` carries the whole request body (the prompt), so
# only ``name``, ``statusCode`` and ``responseBody`` of that object are read;
# the " stream error" suffix after the object is ignored by raw_decode.
MIMO_LLM_ERROR_LINE = re.compile(
    r"^ERROR\s+(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\s+\+\d+ms\s+service=llm\s+"
    r"providerID=(?P<provider>[A-Za-z0-9_.-]+)\s.*?\berror=(?P<json>\{.*)$"
)
MIMO_MAX_LLM_ERROR_LINES = 256
MIMO_MAX_LLM_ERROR_LINE_CHARS = 1_048_576


def _mimo_llm_error_line(line: str) -> dict[str, Any] | None:
    """Classify one native llm error line.

    Returns ``{"at": <timestamp>, "quota": <bool>}`` for an anchored
    ``AI_APICallError`` line; ``quota`` is True only for HTTP 429 whose
    response body is the Token Plan refusal
    ``{"error": {"code": "429", "message": "quota exhausted", "type": "limitation"}}``.
    Any other line, including a malformed anchored one, returns ``None``.
    """
    if not isinstance(line, str) or len(line) > MIMO_MAX_LLM_ERROR_LINE_CHARS:
        return None
    match = MIMO_LLM_ERROR_LINE.match(line.rstrip("\r"))
    if not match or match.group("provider") not in MIMO_ACCEPTED_PROVIDERS:
        return None
    try:
        payload, _end = json.JSONDecoder().raw_decode(match.group("json"))
    except (ValueError, RecursionError):
        return None
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict) or error.get("name") != "AI_APICallError":
        return None
    row = {"at": match.group("ts"), "quota": False}
    if error.get("statusCode") not in (429, "429") or not isinstance(error.get("responseBody"), str):
        return row
    try:
        body = json.loads(error["responseBody"])
    except (ValueError, RecursionError):
        return row
    refusal = body.get("error") if isinstance(body, dict) else None
    row["quota"] = (
        isinstance(refusal, dict)
        and str(refusal.get("code")) == "429"
        and refusal.get("message") == "quota exhausted"
        and refusal.get("type") == "limitation"
    )
    return row


def mimo_quota_refusal(stderr: Any) -> dict[str, Any] | None:
    """Summarise an unbroken Token Plan refusal on the native stderr.

    MiMo 0.1.15 marks the Token Plan reply HTTP 429 ``quota exhausted`` as
    retryable and re-sends the same request with a growing pause (2 s, 4 s,
    8 s, 15 s, 30 s, 60 s, 2 min, 4 min, 5 min ...) without ever writing a
    stdout event, until the parent timeout kills it (exit 124) or the process
    gives up on its own (exit 1 or 4294967295). Measured 2026-10-21
    (HD-20261014-2cfc5e) on 14 stack runs 0.9.60-0.9.102: 8-48 such lines per
    run, stdout 0 bytes, no other native llm error; healthy runs carry none.

    Returns ``{"count", "first_at", "last_at"}`` only when at least one
    anchored ``service=llm`` ``AI_APICallError`` line is present and every one
    of them is that refusal. A different status, a different body or an
    anchored line that does not parse returns ``None``, so the generic
    diagnosis stays and nothing blocks the pool on a guess. At most
    ``MIMO_MAX_LLM_ERROR_LINES`` (256) anchored lines are parsed; when a
    further one follows, the summary would not have seen every line, so the
    result is ``None`` (unknown) as well. Observed runs carry 8-48 lines.
    """
    if not isinstance(stderr, str) or "AI_APICallError" not in stderr:
        return None
    rows: list[dict[str, Any]] = []
    for line in stderr.splitlines():
        if "service=llm" not in line or "AI_APICallError" not in line:
            continue
        if not line.startswith("ERROR"):
            continue
        if len(rows) >= MIMO_MAX_LLM_ERROR_LINES:
            # The parse bound is reached and yet another anchored llm error
            # line follows: "every one of them is the refusal" can no longer
            # be asserted, so stay unknown instead of blocking the pool.
            return None
        row = _mimo_llm_error_line(line)
        if row is None or not row["quota"]:
            return None
        rows.append(row)
    if not rows:
        return None
    return {"count": len(rows), "first_at": rows[0]["at"], "last_at": rows[-1]["at"]}


def mimo_quota_rejection(stdout: Any, stderr: Any, exit_code: Any) -> dict[str, Any] | None:
    """Availability refusal for the native Token Plan quota, never a guess.

    All of these must hold: the process did not exit 0 (a parent-timeout kill
    is 124, a native give-up is 1 or 4294967295), the stdout stream has no
    completed native terminal (an answered run is never a refusal, even when
    an early request was throttled), and ``mimo_quota_refusal`` found only the
    ``quota exhausted`` refusal among the native llm errors. The result has
    the shape of a ``quota_availability.rejection`` entry: no reset time is
    known, so the pool follows the costly no-probe backoff and the next
    verified native run re-opens it.
    """
    if type(exit_code) is not int or exit_code == 0:
        return None
    if not isinstance(stdout, str) or not isinstance(stderr, str):
        return None
    terminal_reason, _terminal_error = mimo_terminal_status(stdout)
    if terminal_reason == "completed":
        return None
    if mimo_quota_refusal(stderr) is None:
        return None
    return {
        "status": "exhausted",
        "scope": "subscription",
        "source": "native-provider-error",
        "reason": MIMO_QUOTA_EXHAUSTED_REASON,
        "reset_at": None,
    }


def mimo_quota_diagnosis(refusal: Any, exit_code: Any, *, timed_out: bool) -> str:
    """One bounded diagnosis sentence; no provider or prompt text is echoed."""
    summary = refusal if isinstance(refusal, dict) else {}
    count = summary.get("count")
    count_text = f"all {count}" if type(count) is int and count > 0 else "every"
    window = ""
    first_at, last_at = summary.get("first_at"), summary.get("last_at")
    if isinstance(first_at, str) and isinstance(last_at, str):
        window = f" between {first_at} and {last_at}"
    ending = (
        "Agent Fleet stopped the native process at the parent timeout"
        if timed_out
        else f"the native process ended with exit code {exit_code}"
    )
    return (
        "PROVIDER QUOTA REFUSAL (not a handoff problem): the MiMo Token Plan "
        f"endpoint answered HTTP 429 quota exhausted (type limitation) to {count_text} "
        f"native requests{window}; MiMo 0.1.15 retried with a growing pause and never "
        f"wrote a stdout event, so the model was not served; {ending}. The mimo pool is "
        f"recorded as exhausted ({MIMO_QUOTA_EXHAUSTED_REASON}); route the task to another "
        "agent instead of re-running MiMo until the pool re-opens."
    )


# ---------------------------------------------------------------------------
# Identity verification (native observations only)
# ---------------------------------------------------------------------------
def mimo_model_identity(
    text: str,
    *,
    expected_provider: str = MIMO_PROVIDER_ID,
    expected_model: str = MIMO_NATIVE_MODEL_ID,
) -> dict[str, Any]:
    """Verify the exact Token Plan Pro identity from native observations.

    Evidence is anchored INFO logger events and structured session/assistant
    metadata. Fleet calls this with native stderr, never stdout/model prose.
    Both provider and model must match the expected native identity exactly.
    """
    expected_provider = str(expected_provider or MIMO_PROVIDER_ID).strip()
    expected_model = str(expected_model or MIMO_NATIVE_MODEL_ID).strip()
    raw = text[:MIMO_MAX_PARSE_CHARS] if isinstance(text, str) else ""
    observations: list[tuple[str, str, str]] = []

    for line in raw.splitlines():
        match = _LOGGER_PATTERN.search(line)
        if match is not None:
            observations.append(
                (
                    "mimo.info-log",
                    _clean_token(match.group("provider")),
                    _clean_token(match.group("model")),
                )
            )

    for event in mimo_events(raw):
        if _is_tool_event(event):
            continue
        event_type = _event_type(event)
        role = str(event.get("role") or "").casefold()
        part = event.get("part")
        part_role = str(part.get("role") if isinstance(part, dict) else "" or "").casefold()
        if (
            event_type
            not in {"session", "assistant", "message", "text", "step_start", "step_finish"}
            and role != "assistant"
            and part_role != "assistant"
        ):
            continue
        containers: list[dict[str, Any]] = [event]
        if isinstance(part, dict):
            containers.append(part)
        for container in containers:
            for provider_key, model_key in _STRUCTURED_IDENTITY_KEYS:
                provider_value = container.get(provider_key)
                model_value = container.get(model_key)
                if isinstance(provider_value, str) and isinstance(model_value, str):
                    observations.append(
                        (
                            "mimo.structured-event",
                            _clean_token(provider_value),
                            _clean_token(model_value),
                        )
                    )

    observed_providers = sorted({provider for _, provider, _ in observations})
    observed_models = sorted({model for _, _, model in observations})
    evidence_sources = sorted({source for source, _, _ in observations})
    base = {
        "expected_provider": expected_provider,
        "expected_model": expected_model,
        "observed_providers": observed_providers,
        "observed_models": observed_models,
        "evidence_sources": evidence_sources,
    }
    if not observations:
        return {
            **base,
            "status": "unavailable",
            "error": "MiMo output contains no provider/model observation; "
            "exact Token Plan identity cannot be verified",
        }
    foreign_providers = sorted(
        provider for provider in observed_providers if provider != expected_provider
    )
    foreign_models = sorted(
        model for model in observed_models if model != expected_model
    )
    if foreign_providers or foreign_models:
        return {
            **base,
            "status": "mismatch",
            "error": "MiMo observed a provider/model outside the requested "
            "Token Plan Pro identity"
            + (f" (providers: {', '.join(foreign_providers)})" if foreign_providers else "")
            + (f" (models: {', '.join(foreign_models)})" if foreign_models else ""),
        }
    regions = sorted(
        {
            region
            for provider in observed_providers
            for region in [region_for_provider(provider)]
            if region
        }
    )
    return {
        **base,
        "status": "verified",
        "token_plan_regions": regions,
        "error": None,
    }


# ---------------------------------------------------------------------------
# Usage and credit accounting
# ---------------------------------------------------------------------------
def _coerce_count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0 or value != value:  # NaN guard
        return None
    return int(value)


def _collect_usage(container: Any, found: dict[str, Any]) -> None:
    if not isinstance(container, dict):
        return
    for key, value in container.items():
        normalized = _slug(str(key))
        target = _USAGE_KEY_MAP.get(normalized)
        if target is None:
            continue
        if target == "credits":
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                found.setdefault("credits", float(value))
            continue
        count = _coerce_count(value)
        if count is not None:
            found.setdefault(target, count)


def extract_mimo_token_usage(text: str) -> dict[str, Any]:
    """Account MiMo usage from native observations only.

    Credits are not tokens and not USD, so ``cost_usd`` is always ``None`` and
    credits are reported separately when the runtime actually reports them.
    Empty, error, or timed-out output is UNKNOWN with ``None`` counters, never
    zero.
    """
    unknown = {
        "status": "unknown",
        "source": "mimo-provider-output",
        "confidence": 0.0,
        "input_tokens": None,
        "fresh_input_tokens": None,
        "output_tokens": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "reasoning_tokens": None,
        "total_tokens": None,
        "cost_usd": None,
        "credits": None,
        "adapter": MIMO_ADAPTER_ID,
        "cost_semantics": MIMO_COST_SEMANTICS,
    }
    events = mimo_events(text)
    if not events:
        return unknown
    # Native MiMo emits one step-finish record per model call. Costs in these
    # records are API estimates, not the amount charged to a Token Plan.
    steps = {}
    for index, event in enumerate(events):
        part = event.get("part")
        if event.get("type") != "step_finish" or not isinstance(part, dict):
            continue
        tokens = part.get("tokens")
        if part.get("type") != "step-finish" or not isinstance(tokens, dict):
            continue
        step = {target: _coerce_count(tokens.get(source)) for source, target in (
            ("input", "input_tokens"), ("output", "output_tokens"),
            ("reasoning", "reasoning_tokens"), ("total", "total_tokens"))}
        cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
        step.update(cache_read_tokens=_coerce_count(cache.get("read")),
                    cache_write_tokens=_coerce_count(cache.get("write")))
        if any(value is not None for value in step.values()):
            steps[str(part.get("id") or part.get("messageID") or index)] = step
    if steps:
        result = dict(unknown, status="known", source="mimo-step-finish", confidence=1.0)
        for key in next(iter(steps.values())):
            values = [step[key] for step in steps.values()]
            result[key] = sum(values) if all(value is not None for value in values) else None
        result["fresh_input_tokens"] = result["input_tokens"]
        result["step_count"] = len(steps)
        result["partial"] = any(event.get("type") == "error" for event in events)
        return result
    found: dict[str, Any] = {}
    for event in events:
        for key in ("usage", "tokens", "cost", "credits"):
            value = event.get(key)
            if isinstance(value, dict):
                _collect_usage(value, found)
            elif key == "credits" and isinstance(value, (int, float)):
                found.setdefault("credits", float(value))
        part = event.get("part")
        if isinstance(part, dict):
            for key in ("usage", "tokens", "cost"):
                value = part.get(key)
                if isinstance(value, dict):
                    _collect_usage(value, found)
        _collect_usage(event, found)
    token_keys = (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "total_tokens",
    )
    if not any(key in found for key in token_keys) and "credits" not in found:
        return unknown
    result = dict(unknown)
    result["status"] = "known"
    result["source"] = "mimo-provider-json"
    result["confidence"] = 0.9 if "total_tokens" in found else 0.6
    for key in token_keys:
        if key in found:
            result[key] = found[key]
    total = result["total_tokens"]
    if total is None:
        inputs = result["input_tokens"]
        outputs = result["output_tokens"]
        if isinstance(inputs, int) and isinstance(outputs, int):
            result["total_tokens"] = inputs + outputs
            result["confidence"] = 0.6
    result["fresh_input_tokens"] = result["input_tokens"]
    if "credits" in found:
        result["credits"] = round(float(found["credits"]), 8)
    return result


def mimo_session_id(text: str) -> str | None:
    """Session id from structured native output, when the runtime reports one."""
    for event in reversed(mimo_events(text)):
        for key in ("sessionID", "sessionId", "session_id"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None
