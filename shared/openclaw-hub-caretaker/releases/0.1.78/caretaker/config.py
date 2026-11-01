"""Configuration loading and validation.

The caretaker only accepts an explicit, versioned config. It does not infer
ambient config and never reads credentials. This module is the single source
of truth for config shape.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


SUPPORTED_SCHEMA_VERSION = 1
MAX_ID = 256
CURSOR_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
TOOL_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


class ConfigError(ValueError):
    """Raised when a caretaker config is missing or malformed."""


@dataclass(frozen=True)
class InputSpec:
    path: str
    kind: str  # "session_jsonl" | "normalized_events"
    tool: str


@dataclass(frozen=True)
class AllowlistSpec:
    tools: List[str]
    max_body_bytes: int
    max_excerpt_bytes: int


@dataclass(frozen=True)
class TransportSpec:
    kind: str  # "openclaw_argv" (only kind supported here)
    executable: str
    argv: List[str]
    run_config_inline: Optional[Dict[str, Any]] = None
    allowed_backends: List[str] = field(default_factory=list)
    allowed_models: List[str] = field(default_factory=list)
    subscription_only: bool = False
    forbidden_backends: List[str] = field(default_factory=list)
    data_classification: Optional[str] = None
    fleet_role: Optional[str] = None


@dataclass(frozen=True)
class HubSpec:
    python_executable: str
    script: str
    vault: str
    require_context: bool
    min_health: str
    min_lint: str
    context_receipt: str


@dataclass(frozen=True)
class OfficeSpec:
    python_executable: str
    script: str
    home: str
    harness: str
    host: str
    session_id: str
    rate_per_minute: int
    dedup: bool
    no_autoplay_reply: bool
    wrapper: str = ""


@dataclass(frozen=True)
class LimitsSpec:
    max_records_per_run: int
    max_prompt_bytes: int
    incident_cooldown_seconds: int


#: Legacy per-role timeout when the config has no ``publication`` section
#: (production control.json omits the new timeout fields). Do not change this
#: without an explicit compatibility decision.
DEFAULT_PUBLICATION_TIMEOUT_SECONDS = 360
#: Measured 2026-09-20 on local vllm/Qwen3.8-27B with a publication-like prompt:
#: cold start 181 s / 355 output tokens (~2 tok/s, includes model load);
#: warm 20.8 s / 400 output tokens (~19.3 tok/s). Config maxTokens is 8192, so a
#: full warm reply is ~425 s and a runtime restart adds ~160 s of cold-start
#: overhead. Maker default covers a full 8192-token warm reply plus cold start
#: with margin.
DEFAULT_PUBLICATION_MAKER_TIMEOUT_SECONDS = 900
#: Same 2026-09-20 measurement; reviewer replies are shorter than a full maker
#: draft, so 600 s covers a warm full-cap generation with cold-start margin
#: without matching the maker window.
DEFAULT_PUBLICATION_REVIEWER_TIMEOUT_SECONDS = 600
#: Alias kept for older tests: the measured maker floor when a publication
#: section is present. Not a cosmetic bump from 360 to 400.
DEFAULT_PUBLICATION_FLOOR_TIMEOUT_SECONDS = DEFAULT_PUBLICATION_MAKER_TIMEOUT_SECONDS
#: Upper guard on maker+reviewer child budgets. Raised from 900 to 1800 so
#: 900+600 from the 2026-09-20 measurement fits with margin. The canonical
#: publish step in ``curate_once.run`` must still bound the sum.
OUTER_PUBLICATION_BUDGET_SECONDS = 1800


@dataclass(frozen=True)
class ReviewerCandidateSpec:
    agent: str
    model: str


@dataclass(frozen=True)
class PublicationReviewerSpec:
    mode: str  # "muse" | "fleet"
    candidates: List[ReviewerCandidateSpec] = field(default_factory=list)
    data_classification: Optional[str] = None


@dataclass(frozen=True)
class PublicationSpec:
    maker_timeout_seconds: int = DEFAULT_PUBLICATION_MAKER_TIMEOUT_SECONDS
    reviewer_timeout_seconds: int = DEFAULT_PUBLICATION_REVIEWER_TIMEOUT_SECONDS
    reviewer: PublicationReviewerSpec = field(
        default_factory=lambda: PublicationReviewerSpec(mode="muse", candidates=[]),
    )
    append_maker: PublicationReviewerSpec | None = None


@dataclass(frozen=True)
class CaretakerConfig:
    schema_version: int
    project_slug: str
    agent: str
    session_id: str
    state_dir: str
    inputs: List[InputSpec]
    allowlist: AllowlistSpec
    primary: TransportSpec
    fallback: Optional[TransportSpec]
    hub: HubSpec
    office: OfficeSpec
    limits: LimitsSpec
    raw: Dict[str, Any]
    publication: PublicationSpec = field(default_factory=PublicationSpec)


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ConfigError(msg)


def _as_str(value: Any, where: str) -> str:
    _require(isinstance(value, str) and value.strip() and "\0" not in value,
             f"{where} must be a non-empty string without NUL")
    return value


def _as_int(value: Any, where: str, lo: int, hi: int) -> int:
    _require(isinstance(value, int) and not isinstance(value, bool),
             f"{where} must be an integer")
    _require(lo <= value <= hi, f"{where} must be in [{lo}, {hi}]")
    return value


def _as_list_str(value: Any, where: str, max_len: int = 64,
                 item_max: int = 256) -> List[str]:
    _require(isinstance(value, list) and 0 <= len(value) <= max_len,
             f"{where} must be a list (max {max_len})")
    out: List[str] = []
    for item in value:
        _require(isinstance(item, str) and item.strip() and "\0" not in item
                 and len(item) <= item_max,
                 f"{where} items must be non-empty strings <= {item_max}")
        out.append(item)
    return out


def _muse_plugin_root() -> Path:
    selected = os.environ.get("HUB_CARETAKER_MUSE_PLUGIN_ROOT")
    default = Path.home() / ".agents" / "openclaw-plugins" / "muse-code" / "0.1.0"
    if selected is None:
        return default
    path = Path(selected)
    _require(bool(selected.strip()) and path.is_absolute() and path.is_dir(),
             "HUB_CARETAKER_MUSE_PLUGIN_ROOT must name an absolute existing directory")
    _require(path.name == "0.1.0" and path.parent.name == "muse-code",
             "Muse plugin binding must retain the pinned muse-code/0.1.0 release")
    _require(not any(p.is_symlink() or (getattr(p.lstat(), "st_file_attributes", 0) & 0x400)
                     for p in (path, *path.parents)), "Muse plugin binding cannot contain filesystem links")
    return path.resolve(strict=True)


def validate_muse_inline(inline: Any) -> None:
    """Fail closed before OpenClaw can merge permissive coding defaults."""
    _require(isinstance(inline, dict) and bool(inline), "Muse fallback requires explicit run_config_inline")
    defaults = inline.get("agents", {}).get("defaults", {})
    _require(defaults.get("model") == {"primary": "muse-code/muse-spark-1.3", "fallbacks": []}, "Muse fallback model/fallbacks must be pinned")
    _require(defaults.get("skipBootstrap") is True and defaults.get("heartbeat") == {"every": "0m"}, "Muse fallback bootstrap/heartbeat must be disabled")
    _require(inline.get("telemetry") == {"enabled": False} and inline.get("update") == {"checkOnStart": False}, "Muse fallback telemetry/update must be disabled")
    tools = inline.get("tools", {})
    _require(tools.get("profile") == "full" and tools.get("allow") == [] and tools.get("deny") == ["*"], "Muse fallback must deny all tools explicitly")
    _require(tools.get("exec") == {"mode": "deny"} and tools.get("fs") == {"workspaceOnly": True} and tools.get("swarm") is False, "Muse fallback execution must be denied")
    _require(tools.get("web") == {"search": {"enabled": False}, "fetch": {"enabled": False}}, "Muse fallback web must be disabled")
    plugins = inline.get("plugins", {})
    _require(plugins.get("enabled") is True and plugins.get("allow") == ["muse-code"] and plugins.get("entries") == {"muse-code": {"enabled": True}}, "Only Muse subscription plugin is permitted")
    _require(plugins.get("load") == {"paths": [_muse_plugin_root().as_posix()]}, "Muse plugin must use the pinned reviewed release")
    _require(inline.get("models") == {"providers": {}} and inline.get("mcp") == {"servers": {}}, "Muse fallback cannot define API providers or MCP")
    _require(inline.get("skills") == {"allowBundled": [], "load": {"watch": False, "extraDirs": []}}, "Muse fallback cannot enable ambient skills")
    _require(set(inline) == {"agents", "telemetry", "update", "models", "tools", "plugins", "mcp", "skills"}, "Muse fallback has unsupported overrides")
    _require(set(inline.get("agents", {})) == {"defaults"}, "Muse fallback cannot override agent entries")
    _require(set(defaults) <= {"workspace", "skipBootstrap", "heartbeat", "model", "sandbox"}, "Muse fallback has unsupported agent defaults")


def _validate_transport(name: str, raw: Dict[str, Any]) -> TransportSpec:
    _require(isinstance(raw, dict), f"{name} must be an object")
    kind = _as_str(raw.get("kind"), f"{name}.kind")
    _require(kind == "openclaw_argv",
             f"{name}.kind must be 'openclaw_argv' (got {kind!r})")
    executable = _as_str(raw.get("executable"), f"{name}.executable")
    argv = _as_list_str(raw.get("argv"), f"{name}.argv", max_len=32,
                        item_max=512)
    _require(all(a for a in argv),
             f"{name}.argv must not contain empty entries")
    inline = raw.get("run_config_inline")
    if inline is not None:
        _require(isinstance(inline, dict),
                 f"{name}.run_config_inline must be an object")
    allowed_backends = _as_list_str(
        raw.get("allowed_backends", []) if name == "fallback" else [],
        f"{name}.allowed_backends")
    allowed_models = _as_list_str(
        raw.get("allowed_models", []) if name == "fallback" else [],
        f"{name}.allowed_models")
    forbidden_backends = _as_list_str(
        raw.get("forbidden_backends", []) if name == "fallback" else [],
        f"{name}.forbidden_backends")
    sub_only = bool(raw.get("subscription_only", False)) \
        if name == "fallback" else False
    if name == "fallback":
        validate_muse_inline(inline)
    return TransportSpec(
        kind=kind,
        executable=executable,
        argv=argv,
        run_config_inline=inline,
        allowed_backends=allowed_backends,
        allowed_models=allowed_models,
        subscription_only=sub_only,
        forbidden_backends=forbidden_backends,
    )


def _validate_input(raw: Dict[str, Any]) -> InputSpec:
    _require(isinstance(raw, dict), "inputs[*] must be an object")
    path = _as_str(raw.get("path"), "inputs[*].path")
    kind = _as_str(raw.get("kind"), "inputs[*].kind")
    _require(kind in ("session_jsonl", "normalized_events"),
             "inputs[*].kind must be session_jsonl|normalized_events")
    tool = _as_str(raw.get("tool"), "inputs[*].tool")
    _require(bool(TOOL_RE.match(tool)),
             "inputs[*].tool must match [a-z][a-z0-9_-]{0,31}")
    return InputSpec(path=path, kind=kind, tool=tool)


def _validate_allowlist(raw: Dict[str, Any]) -> AllowlistSpec:
    _require(isinstance(raw, dict), "allowlist must be an object")
    tools = _as_list_str(raw.get("tools", []), "allowlist.tools")
    for t in tools:
        _require(bool(TOOL_RE.match(t)),
                 "allowlist.tools entries must match [a-z][a-z0-9_-]{0,31}")
    max_body = _as_int(raw.get("max_body_bytes", 8192),
                       "allowlist.max_body_bytes", 256, 1_048_576)
    max_excerpt = _as_int(raw.get("max_excerpt_bytes", 400),
                          "allowlist.max_excerpt_bytes", 64, 16_384)
    return AllowlistSpec(tools=tools, max_body_bytes=max_body,
                         max_excerpt_bytes=max_excerpt)


def _validate_hub(raw: Dict[str, Any]) -> HubSpec:
    _require(isinstance(raw, dict), "hub must be an object")
    py = _as_str(raw.get("python_executable"), "hub.python_executable")
    script = _as_str(raw.get("script"), "hub.script")
    vault = _as_str(raw.get("vault"), "hub.vault")
    req_ctx = bool(raw.get("require_context", True))
    min_health = _as_str(raw.get("min_health", "pass"), "hub.min_health")
    min_lint = _as_str(raw.get("min_lint", "pass"), "hub.min_lint")
    receipt = _as_str(raw.get("context_receipt", ""), "hub.context_receipt")
    return HubSpec(python_executable=py, script=script, vault=vault,
                   require_context=req_ctx, min_health=min_health,
                   min_lint=min_lint, context_receipt=receipt)


def _validate_office(raw: Dict[str, Any]) -> OfficeSpec:
    _require(isinstance(raw, dict), "office must be an object")
    py = _as_str(raw.get("python_executable"), "office.python_executable")
    script = _as_str(raw.get("script"), "office.script")
    # ``wrapper`` is the canonical office.cmd entry point. Keep ``script``
    # for compatibility with older configs, but runtime construction must use
    # this explicit wrapper field when present.
    wrapper = _as_str(raw.get("wrapper", script), "office.wrapper")
    home = _as_str(raw.get("home"), "office.home")
    harness = _as_str(raw.get("harness"), "office.harness")
    host = _as_str(raw.get("host"), "office.host")
    session_id = _as_str(raw.get("session_id"), "office.session_id")
    rpm = _as_int(raw.get("rate_per_minute", 6),
                  "office.rate_per_minute", 1, 60)
    dedup = bool(raw.get("dedup", True))
    no_autoplay = bool(raw.get("no_autoplay_reply", True))
    return OfficeSpec(python_executable=py, script=script, wrapper=wrapper,
                      home=home,
                      harness=harness, host=host, session_id=session_id,
                      rate_per_minute=rpm, dedup=dedup,
                      no_autoplay_reply=no_autoplay)


def _validate_limits(raw: Dict[str, Any]) -> LimitsSpec:
    _require(isinstance(raw, dict), "limits must be an object")
    return LimitsSpec(
        max_records_per_run=_as_int(raw.get("max_records_per_run", 64),
                                   "limits.max_records_per_run", 1, 1024),
        max_prompt_bytes=_as_int(raw.get("max_prompt_bytes", 16384),
                                 "limits.max_prompt_bytes", 1024,
                                 1_048_576),
        incident_cooldown_seconds=_as_int(
            raw.get("incident_cooldown_seconds", 300),
            "limits.incident_cooldown_seconds", 0, 86_400),
    )


def _positive_timeout(value: Any, where: str) -> int:
    _require(isinstance(value, int) and not isinstance(value, bool),
             f"{where} must be a positive integer")
    _require(value > 0, f"{where} must be a positive integer")
    return value


def _validate_reviewer_candidates(raw: Any, field_name="reviewer") -> List[ReviewerCandidateSpec]:
    _require(isinstance(raw, list) and len(raw) > 0,
             f"publication.{field_name}.candidates must be a non-empty list")
    seen: set[tuple[str, str]] = set()
    out: List[ReviewerCandidateSpec] = []
    for idx, item in enumerate(raw):
        where = f"publication.{field_name}.candidates[{idx}]"
        _require(isinstance(item, dict), f"{where} must be an object")
        agent = item.get("agent")
        model = item.get("model")
        _require(isinstance(agent, str) and agent.strip() and "\0" not in agent,
                 f"{where}.agent must be a non-empty string")
        _require(isinstance(model, str) and model.strip() and "\0" not in model,
                 f"{where}.model must be a non-empty string")
        key = (agent, model)
        _require(key not in seen,
                 f"{where} duplicates agent/model {agent!r}/{model!r}")
        seen.add(key)
        out.append(ReviewerCandidateSpec(agent=agent, model=model))
    return out


def _publication_data_classification(raw: dict, field_name: str) -> Optional[str]:
    if "data_classification" not in raw:
        return None
    value = raw["data_classification"]
    _require(value in ("public", "internal"),
             f"publication.{field_name}.data_classification must be 'public' or 'internal'")
    return value


def _validate_publication(raw: Any) -> PublicationSpec:
    if raw is None:
        # No publication section: keep the legacy 360 s defaults so existing
        # production configs continue to behave exactly as today.
        return PublicationSpec(
            maker_timeout_seconds=DEFAULT_PUBLICATION_TIMEOUT_SECONDS,
            reviewer_timeout_seconds=DEFAULT_PUBLICATION_TIMEOUT_SECONDS,
            reviewer=PublicationReviewerSpec(mode="muse", candidates=[]),
        )
    _require(isinstance(raw, dict), "publication must be an object")
    reviewer_raw = raw.get("reviewer")
    mode = "muse"
    declared = None
    if reviewer_raw is not None:
        _require(isinstance(reviewer_raw, dict),
                 "publication.reviewer must be an object")
        declared = reviewer_raw.get("mode")
        if declared is not None:
            _require(declared in ("muse", "fleet"),
                     "publication.reviewer.mode must be 'muse' or 'fleet'")
            mode = declared
    # Publication section present but timeout fields omitted: use the
    # 2026-09-20 measured defaults (maker 900 s, reviewer 600 s). Explicit
    # mode='muse' keeps the legacy 360 s pair. Configs that omit the whole
    # publication section also keep 360 s (see the ``raw is None`` branch).
    if declared == "muse":
        maker_timeout = DEFAULT_PUBLICATION_TIMEOUT_SECONDS
        reviewer_timeout = DEFAULT_PUBLICATION_TIMEOUT_SECONDS
    else:
        maker_timeout = DEFAULT_PUBLICATION_MAKER_TIMEOUT_SECONDS
        reviewer_timeout = DEFAULT_PUBLICATION_REVIEWER_TIMEOUT_SECONDS
    if "maker_timeout_seconds" in raw:
        maker_timeout = _positive_timeout(
            raw.get("maker_timeout_seconds"),
            "publication.maker_timeout_seconds")
    if "reviewer_timeout_seconds" in raw:
        reviewer_timeout = _positive_timeout(
            raw.get("reviewer_timeout_seconds"),
            "publication.reviewer_timeout_seconds")
    # The outer canonical publish step is bounded by
    # OUTER_PUBLICATION_BUDGET_SECONDS (curate_once.run line 286); the sum of
    # the maker and reviewer budgets must fit strictly below it.
    _require(maker_timeout + reviewer_timeout < OUTER_PUBLICATION_BUDGET_SECONDS,
             ("publication.maker_timeout_seconds + "
              "publication.reviewer_timeout_seconds must be < "
              + str(OUTER_PUBLICATION_BUDGET_SECONDS)))
    if mode == "fleet":
        _require(reviewer_raw is not None,
                 "publication.reviewer must be set when mode is 'fleet'")
        candidates = _validate_reviewer_candidates(
            reviewer_raw.get("candidates"))
        reviewer = PublicationReviewerSpec(
            mode="fleet", candidates=candidates,
            data_classification=_publication_data_classification(reviewer_raw, "reviewer"))
    else:
        reviewer = PublicationReviewerSpec(
            mode="muse", candidates=[],
            data_classification=_publication_data_classification(
                reviewer_raw, "reviewer") if reviewer_raw is not None else None)
    append_raw = raw.get("append_maker")
    append_maker = None
    if append_raw is not None:
        _require(isinstance(append_raw, dict),
                 "publication.append_maker must be an object")
        _require(append_raw.get("mode") == "fleet",
                 "publication.append_maker.mode must be 'fleet'")
        append_maker = PublicationReviewerSpec(
            mode="fleet", candidates=_validate_reviewer_candidates(
                append_raw.get("candidates"), "append_maker"),
            data_classification=_publication_data_classification(
                append_raw, "append_maker"))
    return PublicationSpec(
        maker_timeout_seconds=maker_timeout,
        reviewer_timeout_seconds=reviewer_timeout,
        reviewer=reviewer,
        append_maker=append_maker,
    )


def load_config(path: str | os.PathLike[str]) -> CaretakerConfig:
    """Load and validate a caretaker config from ``path``."""
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"config file not found: {p}")
    try:
        raw = json.loads(p.read_text("utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a JSON object")
    schema = raw.get("schema_version")
    _require(schema == SUPPORTED_SCHEMA_VERSION,
             f"config.schema_version must equal {SUPPORTED_SCHEMA_VERSION}")
    project = _as_str(raw.get("project_slug"), "project_slug")
    _require(len(project) <= MAX_ID, "project_slug too long")
    agent = _as_str(raw.get("agent"), "agent")
    _require(len(agent) <= 128, "agent too long")
    session_id = _as_str(raw.get("session_id"), "session_id")
    _require(len(session_id) <= MAX_ID, "session_id too long")
    state_dir = _as_str(raw.get("state_dir"), "state_dir")

    inputs_raw = raw.get("inputs", [])
    _require(isinstance(inputs_raw, list) and inputs_raw,
             "inputs must be a non-empty list")
    inputs = [_validate_input(item) for item in inputs_raw]

    allowlist = _validate_allowlist(raw.get("allowlist", {}))
    _require(set(allowlist.tools) >= {i.tool for i in inputs},
             "allowlist.tools must include every input tool")

    primary = _validate_transport("primary", raw.get("primary", {}))
    fallback_raw = raw.get("fallback")
    fallback: Optional[TransportSpec] = None
    if fallback_raw is not None:
        fallback = _validate_transport("fallback", fallback_raw)
        _require(fallback.subscription_only,
                 "fallback.subscription_only must be true when set")
        _require(fallback.allowed_backends == ["muse-code"],
                 "fallback.allowed_backends must be ['muse-code']")
        _require(fallback.allowed_models == ["muse-spark-1.3"],
                 "fallback.allowed_models must be ['muse-spark-1.3']")
        _require("http-meta" in fallback.forbidden_backends
                 and "meta-http" in fallback.forbidden_backends,
                 "fallback.forbidden_backends must include http-meta,meta-http")
        # Confirm primary does not already advertise the muse backend.
        joined = " ".join(primary.argv)
        _require("muse-code/muse-spark-1.3" not in joined,
                 "primary must not advertise muse-code/muse-spark-1.3")

    hub = _validate_hub(raw.get("hub", {}))
    office = _validate_office(raw.get("office", {}))
    limits = _validate_limits(raw.get("limits", {}))
    publication = _validate_publication(raw.get("publication"))

    return CaretakerConfig(
        schema_version=schema,
        project_slug=project,
        agent=agent,
        session_id=session_id,
        state_dir=state_dir,
        inputs=inputs,
        allowlist=allowlist,
        primary=primary,
        fallback=fallback,
        hub=hub,
        office=office,
        limits=limits,
        raw=raw,
        publication=publication,
    )
