"""Load parent-supplied protected JSON. Token never lives in this file."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


class ConfigError(ValueError):
    pass


def _require_int(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError("%s must be an exact int" % name)
    return value


def _require_int_tuple(name: str, value: Any) -> tuple:
    if not isinstance(value, list) or not value:
        # Empty is allowed for disabled defaults.
        if value == []:
            return tuple()
        raise ConfigError("%s must be a list of ints" % name)
    out = []
    for item in value:
        out.append(_require_int(name, item))
    return tuple(out)


@dataclass
class TargetSpec:
    id: str
    label: str
    provider: str
    native_thread: str = ""
    task_id: str = ""
    generation: int = 0
    enabled: bool = False
    verified: bool = False
    rollout_path: str = ""


@dataclass
class CodexAdapterSpec:
    enabled: bool = False
    node_path: str = ""
    plugin_path: str = ""
    plugin_sha256: str = ""
    pipe_path: str = ""
    caller_thread: str = ""
    sessions_root: str = ""
    pid: int = 0
    server_image: str = ""
    threads: dict = field(default_factory=dict)


@dataclass
class RuntimeConfig:
    enabled: bool
    authority_db: str
    owner_user_ids: tuple
    owner_chat_id: int
    bot_id: int
    telegram_enabled: bool
    long_poll_timeout: float
    telegram_host: str
    targets: list
    codex: CodexAdapterSpec
    claude_enabled: bool
    auto_resume: bool
    max_no_progress_attempts: int
    helpdesk_enabled: bool
    unknown_alarm_after: float
    raw: dict


def load_config(path: str) -> RuntimeConfig:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ConfigError("config must be a JSON object")
    owner = data.get("owner") or {}
    telegram = data.get("telegram") or {}
    adapters = data.get("adapters") or {}
    codex_raw = adapters.get("codex-desktop") or {}
    claude_raw = adapters.get("claude-desktop") or {}
    supervision = data.get("supervision") or {}
    helpdesk = data.get("helpdesk") or {}
    if telegram.get('host', 'api.telegram.org') != 'api.telegram.org':
        raise ConfigError('Only the official Telegram Bot API host is allowed')
    targets = []
    for item in data.get("targets") or []:
        if not isinstance(item, dict):
            raise ConfigError("target must be an object")
        targets.append(
            TargetSpec(
                id=str(item.get("id") or ""),
                label=str(item.get("label") or item.get("id") or ""),
                provider=str(item.get("provider") or ""),
                native_thread=str(item.get("native_thread") or ""),
                task_id=str(item.get("task_id") or ""),
                generation=int(item.get("generation") or 0),
                enabled=bool(item.get("enabled")),
                verified=bool(item.get("verified")),
                rollout_path=str(item.get("rollout_path") or ""),
            )
        )
    threads = codex_raw.get("threads") or {}
    if not isinstance(threads, dict):
        raise ConfigError("codex-desktop.threads must be an object")
    pid = codex_raw.get("pid") or 0
    if pid in (None, ""):
        pid = 0
    if pid != 0:
        pid = _require_int("adapters.codex-desktop.pid", pid)
    return RuntimeConfig(
        enabled=bool(data.get("enabled")),
        authority_db=str(data.get("authority_db") or ""),
        owner_user_ids=_require_int_tuple(
            "owner.user_ids", owner.get("user_ids") or []
        ),
        owner_chat_id=_require_int(
            "owner.chat_id", owner.get("chat_id") if owner.get("chat_id") is not None else 0
        ),
        bot_id=_require_int(
            "owner.bot_id", owner.get("bot_id") if owner.get("bot_id") is not None else 0
        ),
        telegram_enabled=bool(telegram.get("enabled")),
        long_poll_timeout=float(telegram.get("long_poll_timeout") or 25),
        telegram_host=str(telegram.get("host") or "api.telegram.org"),
        targets=targets,
        codex=CodexAdapterSpec(
            enabled=bool(codex_raw.get("enabled")),
            node_path=str(codex_raw.get("node_path") or ""),
            plugin_path=str(codex_raw.get("plugin_path") or ""),
            plugin_sha256=str(codex_raw.get("plugin_sha256") or ""),
            pipe_path=str(codex_raw.get("pipe_path") or ""),
            caller_thread=str(codex_raw.get("caller_thread") or ""),
            sessions_root=str(codex_raw.get("sessions_root") or ""),
            pid=int(pid),
            server_image=str(codex_raw.get('server_image') or ''),
            threads=dict(threads),
        ),
        claude_enabled=bool(claude_raw.get("enabled")) and bool(
            claude_raw.get("verified")
        ),
        auto_resume=bool(supervision.get("auto_resume")),
        max_no_progress_attempts=int(
            supervision.get("max_no_progress_attempts") or 2
        ),
        helpdesk_enabled=bool(helpdesk.get("enabled")),
        unknown_alarm_after=float(data.get("unknown_alarm_after") or 120),
        raw=data,
    )
