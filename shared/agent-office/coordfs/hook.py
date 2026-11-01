"""Fail-open, warning-only adapter for native PreToolUse-style hooks."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .atomic import replace_with_backoff
from .identity import agent_identity, canonicalize_resource
from .leases import CoordBoard


class CoordAdvisoryHook:
    def __init__(
        self,
        *,
        home: Path | None = None,
        cwd: Path | None = None,
        now: Callable[[], float] | None = None,
        cache_ttl_s: float = 1.0,
    ) -> None:
        self.board = CoordBoard(home=home, cwd=cwd, now=now)
        self.cwd = cwd if cwd is not None else Path.cwd()
        self._now = now or time.time
        self.cache_ttl_s = max(0.0, cache_ttl_s)
        self.cache_dir = self.board.home / "hook-cache"

    def _cache_path(self, resource_id: str, agent_id: str) -> Path:
        digest = hashlib.sha256(f"{resource_id}\0{agent_id}".encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest}.json"

    def _read_cache(self, path: Path) -> dict[str, Any] | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            checked_at = float(payload["checked_at"])
            result = payload["result"]
        except (FileNotFoundError, OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if self._now() - checked_at > self.cache_ttl_s or not isinstance(result, dict):
            return None
        return result

    def _write_cache(self, path: Path, result: dict[str, Any]) -> None:
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex}")
            temporary.write_text(
                json.dumps(
                    {"checked_at": self._now(), "result": result},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            if not replace_with_backoff(temporary, path):
                temporary.unlink(missing_ok=True)
        except OSError:
            pass

    def check(self, resource: str, agent_id: str, *, kind: str | None = None) -> dict[str, Any]:
        try:
            resource_id = canonicalize_resource(resource, kind=kind, cwd=self.cwd)
            cache_path = self._cache_path(resource_id, agent_id)
            cached = self._read_cache(cache_path)
            if cached is not None:
                return cached
            state = self.board.who(resource, kind=kind)
            holder = state.get("holder_agent")
            if holder and holder != agent_id:
                ttl = state.get("ttl_hint_s", state.get("ttl_remaining_s", "unknown"))
                message = f"COORD_WARNING held by {holder}; ttl_hint_s={ttl}; advisory only"
                result = {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "allow",
                        "permissionDecisionReason": message,
                        "additionalContext": message,
                    }
                }
            else:
                result = {}
            self._write_cache(cache_path, result)
            return result
        except Exception:
            return {}


def _resource_from_payload(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    direct = payload.get("resource")
    if isinstance(direct, str) and direct.strip():
        kind = payload.get("kind")
        return direct, str(kind) if kind in {"path", "scope", "data"} else None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None, None
    for key in ("file_path", "path", "notebook_path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value, "path"
    return None, None


def hook_main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            raise ValueError("hook payload is not an object")
        resource, kind = _resource_from_payload(payload)
        if not resource:
            print("{}")
            return 0
        supplied_agent = payload.get("agent_id")
        if isinstance(supplied_agent, str) and supplied_agent.strip():
            agent_id = supplied_agent.strip()
        else:
            harness = str(payload.get("harness") or os.environ.get("COORDFS_HARNESS") or "unknown")
            session_id = payload.get("session_id")
            identity = agent_identity(harness, session_id=str(session_id) if session_id else None)
            agent_id = identity["agent_id"]
        result = CoordAdvisoryHook().check(resource, agent_id, kind=kind)
    except Exception:
        result = {}
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0
