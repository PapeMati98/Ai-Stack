"""Project-scoped transport built on the advisory :mod:`coordfs.office` API.

The legacy Office inbox and channel APIs deliberately remain unchanged.  This
module composes them into explicitly registered project streams: the registry
binds a canonical project id to its checkout bindings (one immutable legacy
checkout, or additive host-qualified claims after an explicit offline owner
migration), and every direct inbox, channel, cursor and named-pipe wake
namespace is derived from that binding plus ``task_id`` / ``thread_id`` /
``run_id``.  Therefore a process never picks a project route from its current
working directory.

Project payloads are still ordinary untrusted Office bodies.  This module never
executes a body, treats authority claims as audit rejections, and keeps
application acknowledgement separate from durable filesystem delivery.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import time
import unicodedata
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import checkout_claims
from .atomic import exclusive_create, exclusive_mutex, replace_with_backoff
from .errors import CoordError
from .identity import WINDOWS_RESERVED_NAMES, agent_identity
from .leases import CoordBoard, format_ts
from .office import HANDLING_TEXT, Office, channel_source, cursor_path, detect_secret
from .office_pipe import namespace as pipe_namespace

PROJECT_SCHEMA_LEGACY = 1
PROJECT_SCHEMA_VERSION = 2
PROJECT_ENVELOPE_SCHEMA = "project-office/v1"
PROJECT_ACTIVITY_SCHEMA = "office-activity/v1"
PROJECT_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}\Z")
MESSAGE_ID_RE = re.compile(r"^[0-9a-f]{32}\Z")
ACTIVITY_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}\Z")
ACTIVITY_MAX_SUMMARY = 280
ACTIVITY_MAX_EVENTS = 2048
# Project reads add bounded routing, ACK, and audit fields to each Office frame.
# Reserve room for those wrappers before the underlying Office cursor commits.
PROJECT_READ_RESPONSE_FIXED_RESERVE_BYTES = 8192
PROJECT_READ_RESPONSE_PER_MESSAGE_RESERVE_BYTES = 4096
ACTIVITY_KINDS = frozenset(
    {
        "thread_started",
        "task_started",
        "message_delta",
        "reasoning_delta",
        "plan_updated",
        "tool_started",
        "tool_completed",
        "command_started",
        "command_completed",
        "command_output",
        "file_changed",
        "subagent_started",
        "subagent_completed",
        "approval_requested",
        "quota_updated",
        "usage_updated",
        "validation_result",
        "turn_completed",
        "error",
        "opaque",
    }
)
ACTIVITY_STATUSES = frozenset(
    {"queued", "running", "waiting", "blocked", "succeeded", "failed", "cancelled"}
)
ACTIVITY_PROVIDERS = frozenset(
    {
        "antigravity",
        "claude",
        "codex",
        "gemini",
        "grok",
        "hermes",
        "kimi",
        "kimi-code",
        "kimi-desktop",
        "minimax",
        "muse",
        "opencode",
        "qwen",
        "qwen-code",
        "qwen-local",
        "qwen-local-5070ti",
        "zcode",
    }
)
ACTIVITY_USAGE_KEYS = frozenset(
    {"input_tokens", "output_tokens", "total_tokens", "context_window", "remaining", "duration_ms", "cost_micros"}
)
ACTIVITY_UNSAFE_PATTERNS = (
    re.compile(r"(?:ignore|disregard|forget)\s+(?:all\s+)?(?:previous|prior|above)\s+(?:instructions|prompts|rules)", re.I),
    re.compile(r"(?:you are|act as|behave as)\s+(?:an?\s+)?(?:ai|assistant|model|agent|bot|dan)\b", re.I),
    re.compile(r"(?:system prompt|system message|developer instruction|developer mode)", re.I),
    re.compile(r"^(?:system|assistant|user|human|ai):\s*", re.I | re.M),
    re.compile(r"<\|(?:im_start|im_end|system|user|assistant)\|>", re.I),
    re.compile(r"\b(?:jailbreak|do anything now)\b", re.I),
    re.compile(r"\b(?:sudo|chmod|chown|rm\s+-rf|curl\s+.*\|\s*(?:ba)?sh|wget\s+.*\|\s*(?:ba)?sh)\b", re.I),
    re.compile(r"\b(?:powershell(?:\.exe)?|cmd(?:\.exe)?|wscript(?:\.exe)?|cscript(?:\.exe)?)\s+", re.I),
    re.compile(r"<script\b[^>]*>", re.I),
    re.compile(r"\b(?:child_process|subprocess)\b", re.I),
)

# These are the local runtimes presently connected by the stack, plus the
# owner.  The local transport does not reject another harness, but the SSH gate
# does: office_ssh_gate.PRINCIPAL_HARNESSES allowlists harnesses per principal
# (F2) and a test keeps that list covering these runtimes ("mateusz" is the
# owner identity, not a remote CLI harness, and is intentionally not allowlisted).
CONNECTED_HARNESSES = (
    "codex",
    "claude",
    "kimi-code",
    "kimi-desktop",
    "antigravity",
    "grok",
    "hermes",
    "mateusz",
)

AUTHORITY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("deploy", re.compile(r"\bdeploy(?:ment)?\b", re.IGNORECASE)),
    (
        "secret-change",
        re.compile(
            r"\b(?:secret|credential|token)\b.{0,40}\b(?:change|rotate|set)\b|\b(?:change|rotate|set)\b.{0,40}\b(?:secret|credential|token)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "real-mail",
        re.compile(
            r"\b(?:real|production)\b.{0,30}\b(?:mail|email)\b|\b(?:mail|email)\b.{0,30}\b(?:real|production)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "remote-hermes",
        re.compile(r"\bremote\s+hermes\b|\bhermes\b.{0,30}\bremote\b", re.IGNORECASE),
    ),
    (
        "scheduler",
        re.compile(r"\b(?:scheduler|scheduled task|task scheduler)\b", re.IGNORECASE),
    ),
    (
        "payment",
        re.compile(
            r"\b(?:payment|payout|transfer)\b.{0,40}\b(?:authori[sz]e|approve|send|execute)\b|\b(?:authori[sz]e|approve|send|execute)\b.{0,40}\b(?:payment|payout|transfer)\b",
            re.IGNORECASE,
        ),
    ),
)


class ProjectReject(CoordError):
    """A deterministic project-registry or project-envelope validation refusal."""


def _copy_json(value: Any) -> Any:
    """Return a JSON-only defensive copy without retaining mutable registry state."""
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _token(raw: Any, label: str) -> str:
    if not isinstance(raw, str):
        raise ProjectReject(f"{label} must be a string", code=f"invalid_{label}")
    text = unicodedata.normalize("NFC", raw.strip()).lower()
    if "\x00" in text or "/" in text or "\\" in text or text in {".", ".."}:
        raise ProjectReject(
            f"{label} cannot contain traversal or path separators",
            code=f"invalid_{label}",
        )
    if not PROJECT_TOKEN_RE.match(text):
        raise ProjectReject(
            f"{label} must be 1..128 lowercase safe characters", code=f"invalid_{label}"
        )
    return text


def canonicalize_project_id(raw: Any) -> str:
    """Return the closed, canonical project id; aliases are resolved only by the registry."""
    return _token(raw, "project_id")


def canonicalize_correlation(raw: Any, label: str) -> str:
    """Canonicalize one task/thread/run/channel component without using ``Path.cwd()``."""
    if label not in {"task_id", "thread_id", "run_id", "channel"}:
        raise ValueError(f"unsupported correlation label: {label}")
    return _token(raw, label)


def _is_reparse(path: Path) -> bool:
    """True when an existing path is a symlink or a Windows reparse point.

    Mirrors ``install_agent_office.is_reparse``: ``lstat`` never follows the
    final component, and the reparse flag covers junctions, which ``is_symlink``
    alone does not report on Windows.
    """
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ProjectReject(
            f"checkout path metadata cannot be inspected: {exc}",
            code="invalid_checkout",
        ) from exc
    attributes = getattr(info, "st_file_attributes", 0)
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(attributes & flag)


def _reject_reserved_device_component(parts: Sequence[str]) -> None:
    """Reject Windows reserved device names in any checkout component.

    Same denylist as an agent id (``con``, ``nul``, ``com1``, ...), where the
    segment before the first dot decides, matching how Windows treats
    ``NUL.txt``.  A checkout reaches the filesystem raw (unlike hashed project
    stream names), so ``\\\\.\\COM1`` or ``C:\\foo\\NUL`` must never bind.
    """
    for part in parts:
        if not part or part.endswith(":") and len(part) == 2 and part[0].isalpha():
            continue
        # Windows also interprets a reserved device followed by an ADS suffix
        # as the same device (for example ``NUL:stream``).
        stem = part.strip().lower().replace(":", ".", 1).split(".", 1)[0]
        if stem in WINDOWS_RESERVED_NAMES:
            raise ProjectReject(
                f"checkout component {part!r} is a Windows reserved device name",
                code="invalid_checkout",
            )


def _reject_reparse_checkout(path: Path, label: str) -> None:
    """Reject an existing checkout binding that is itself a reparse point."""
    if os.path.lexists(os.fspath(path)) and _is_reparse(path):
        raise ProjectReject(
            f"{label} is a symlink or reparse point",
            code="invalid_checkout",
        )


def canonicalize_checkout(raw: Any) -> str:
    """Canonicalize an *absolute* checkout binding and reject traversal before resolving.

    Relative paths are intentionally rejected instead of being made relative to
    the caller's CWD.  A checkout need not exist yet, so ``strict=False`` keeps
    registration deterministic for a planned worktree.  Windows reserved device
    names (also with an extension) and symlink/reparse-point bindings are
    refused because the checkout is a raw filesystem path.
    """
    if not isinstance(raw, (str, Path)):
        raise ProjectReject("checkout must be a path string", code="invalid_checkout")
    text = unicodedata.normalize("NFC", str(raw).strip())
    if not text or "\x00" in text:
        raise ProjectReject(
            "checkout must be a non-empty path without NUL", code="invalid_checkout"
        )
    slash_parts = text.replace("\\", "/").split("/")
    if ".." in slash_parts:
        raise ProjectReject(
            "checkout cannot contain parent traversal", code="invalid_checkout"
        )
    _reject_reserved_device_component(slash_parts)
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise ProjectReject(
            "checkout must be absolute; current working directory is not a binding",
            code="invalid_checkout",
        )
    _reject_reparse_checkout(Path(os.path.abspath(str(path))), "checkout")
    try:
        resolved = path.resolve(strict=False)
    except OSError as exc:
        raise ProjectReject(
            f"checkout cannot be resolved: {exc}", code="invalid_checkout"
        ) from exc
    canonical = os.path.normcase(os.path.abspath(str(resolved)))
    if not Path(canonical).is_absolute():
        raise ProjectReject(
            "checkout did not resolve to an absolute path", code="invalid_checkout"
        )
    _reject_reserved_device_component(canonical.replace("\\", "/").split("/"))
    _reject_reparse_checkout(Path(canonical), "checkout")
    return canonical


def _correlation(task_id: Any, thread_id: Any, run_id: Any) -> dict[str, str]:
    return {
        "task_id": canonicalize_correlation(task_id, "task_id"),
        "thread_id": canonicalize_correlation(thread_id, "thread_id"),
        "run_id": canonicalize_correlation(run_id, "run_id"),
    }


_CHECKOUT_DRIVE_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")


def _canonical_host(raw: Any) -> str:
    """Require an already-canonical host identity component; never normalize a claim."""
    try:
        return checkout_claims.canonical_host(raw)
    except CoordError as exc:
        raise ProjectReject(str(exc), code=exc.code) from exc


def _checkout_claim(
    raw: Any, *, host: str, path_style: str | None = None
) -> dict[str, str]:
    """Pure lexical claim construction; no resolve, lstat, or existence check."""
    try:
        return checkout_claims.checkout_claim(raw, host=host, path_style=path_style)
    except CoordError as exc:
        raise ProjectReject(str(exc), code=exc.code) from exc


def _checkout_path_style(text: str) -> str | None:
    if _CHECKOUT_DRIVE_ABSOLUTE.match(text) or text.startswith("\\\\"):
        return "windows"
    if text.startswith("/"):
        return "posix"
    return None


def _native_path_style() -> str:
    return "windows" if os.name == "nt" else "posix"


def _validate_stored_checkout(checkout: Any, *, allow_native: bool = True) -> None:
    """Validate a persisted legacy checkout: native-strength on this OS, lexical abroad.

    A foreign spelling is checked with the pure lexical claim rules and is never
    resolved, lstat'd, or probed for existence; the throwaway host below is a
    validation vessel only and is never stored, so no host is invented for a
    legacy binding whose owner host is not yet known.  ``allow_native=False``
    (an offline migration attesting a foreign source host) forces the lexical
    branch even for a spelling in this OS's path style: locality comes from the
    explicit host, never from the spelling, so no foreign path is inspected.
    """
    if not isinstance(checkout, str) or not checkout:
        raise ProjectReject(
            "project registry checkout binding is invalid", code="registry_corrupt"
        )
    style = _checkout_path_style(checkout)
    if style is None:
        raise ProjectReject(
            "project registry checkout binding is invalid", code="registry_corrupt"
        )
    if allow_native and style == _native_path_style():
        if canonicalize_checkout(checkout) != checkout:
            raise ProjectReject(
                "project registry checkout binding is invalid", code="registry_corrupt"
            )
        return
    try:
        claim = checkout_claims.checkout_claim(
            checkout, host="legacy-foreign", path_style=style
        )
    except CoordError as exc:
        raise ProjectReject(
            "project registry checkout binding is invalid", code="registry_corrupt"
        ) from exc
    if claim["path"] != checkout:
        raise ProjectReject(
            "project registry checkout binding is invalid", code="registry_corrupt"
        )


def _digest(value: str, length: int = 24) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def _stream_id(project_id: str, correlation: Mapping[str, str]) -> str:
    encoded = json.dumps(
        {"project_id": project_id, **dict(correlation)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return "stream-" + _digest(encoded)


def _identity_from_mapping(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ProjectReject(
            "runtime identity must be a mapping", code="invalid_identity"
        )
    fields = ("agent_id", "harness", "host", "session", "strength", "source")
    identity: dict[str, str] = {}
    for field in fields:
        item = value.get(field)
        if not isinstance(item, str) or not item:
            raise ProjectReject(
                f"runtime identity field {field} is invalid", code="invalid_identity"
            )
        identity[field] = item
    expected = f"{identity['harness']}-{identity['host']}-{identity['session']}"
    if identity["agent_id"] != expected:
        raise ProjectReject(
            "runtime identity does not match harness-host-session",
            code="invalid_identity",
        )
    if identity["strength"] not in {"strong", "weak"}:
        raise ProjectReject(
            "runtime identity strength is invalid", code="invalid_identity"
        )
    return identity


def _activity_token(value: Any, label: str) -> str:
    if not isinstance(value, str) or not ACTIVITY_TOKEN_RE.fullmatch(value):
        raise ProjectReject(f"{label} must be a bounded activity identifier", code="invalid_activity")
    return value


def _activity_summary(value: Any) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > ACTIVITY_MAX_SUMMARY
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ProjectReject("activity summary is invalid or too large", code="invalid_activity")
    secret = detect_secret(value)
    if secret is not None:
        raise ProjectReject(
            f"activity summary contains a high-confidence secret ({secret})",
            code="sensitive_activity",
        )
    if any(pattern.search(value) for pattern in ACTIVITY_UNSAFE_PATTERNS):
        raise ProjectReject(
            "activity summary resembles a prompt or executable instruction",
            code="unsafe_activity",
        )
    return value


def _activity_usage(value: Any) -> dict[str, int | float]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ProjectReject("activity usage must be an object", code="invalid_activity")
    unknown = sorted(set(value) - ACTIVITY_USAGE_KEYS)
    if unknown:
        raise ProjectReject(
            "activity usage has unsupported fields: " + ", ".join(str(item) for item in unknown),
            code="invalid_activity",
        )
    result: dict[str, int | float] = {}
    for key, item in value.items():
        if key == "remaining":
            if isinstance(item, bool) or not isinstance(item, (int, float)) or item < 0:
                raise ProjectReject(
                    f"activity usage.{key} must be a non-negative number", code="invalid_activity"
                )
            if isinstance(item, float) and not math.isfinite(item):
                raise ProjectReject(
                    f"activity usage.{key} must be finite", code="invalid_activity"
                )
        elif type(item) is not int or item < 0:
            raise ProjectReject(
                f"activity usage.{key} must be a non-negative integer", code="invalid_activity"
            )
        result[key] = item
    return result


def _activity_payload(
    *,
    project_id: str,
    stream: ProjectStream,
    identity: Mapping[str, str],
    source_event_id: Any,
    provider_id: Any,
    kind: Any,
    status: Any,
    summary: Any,
    raw_method: Any,
    model_id: Any,
    role_id: Any,
    requires_attention: Any,
    usage: Any,
) -> dict[str, Any]:
    if not isinstance(requires_attention, bool):
        raise ProjectReject("activity requires_attention must be boolean", code="invalid_activity")
    provider = _activity_token(provider_id, "provider_id").lower()
    if provider not in ACTIVITY_PROVIDERS:
        raise ProjectReject(
            f"activity provider {provider!r} is not allowlisted", code="invalid_activity"
        )
    activity_kind = _activity_token(kind, "kind")
    if activity_kind not in ACTIVITY_KINDS:
        raise ProjectReject(
            f"activity kind {activity_kind!r} is not allowlisted", code="invalid_activity"
        )
    activity_status = None if status is None else _activity_token(status, "status")
    if activity_status is not None and activity_status not in ACTIVITY_STATUSES:
        raise ProjectReject(
            f"activity status {activity_status!r} is not allowlisted", code="invalid_activity"
        )
    source = _activity_token(source_event_id, "source_event_id")
    method = _activity_token(raw_method, "raw_method")
    model = None if model_id is None else _activity_token(model_id, "model_id")
    role = None if role_id is None else _activity_token(role_id, "role_id")
    normalized_identity = _identity_from_mapping(identity)
    correlation = stream.correlation()
    return {
        "schema": PROJECT_ACTIVITY_SCHEMA,
        "source_event_id": source,
        "project_id": canonicalize_project_id(project_id),
        "stream_id": stream.stream_id,
        **correlation,
        "provider_id": provider,
        "kind": activity_kind,
        "status": activity_status,
        "summary": _activity_summary(summary),
        "raw_method": method,
        "model_id": model,
        "role_id": role,
        "requires_attention": requires_attention,
        "usage": _activity_usage(usage),
        "identity": normalized_identity,
    }


def _activity_fingerprint(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _validate_activity_record(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProjectReject("project activity record is invalid", code="registry_corrupt")
    expected = {
        "schema",
        "source_event_id",
        "project_id",
        "stream_id",
        "task_id",
        "thread_id",
        "run_id",
        "provider_id",
        "kind",
        "status",
        "summary",
        "raw_method",
        "model_id",
        "role_id",
        "requires_attention",
        "usage",
        "identity",
        "event",
        "fingerprint",
    }
    if set(value) != expected or value["schema"] != PROJECT_ACTIVITY_SCHEMA:
        raise ProjectReject("project activity record shape is invalid", code="registry_corrupt")
    base = dict(value)
    fingerprint = base.pop("fingerprint")
    event = base.pop("event")
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise ProjectReject("project activity fingerprint is invalid", code="registry_corrupt")
    _activity_token(value["source_event_id"], "source_event_id")
    _activity_token(value["project_id"], "project_id")
    _activity_token(value["stream_id"], "stream_id")
    for field in ("task_id", "thread_id", "run_id"):
        _activity_token(value[field], field)
    if value["provider_id"] not in ACTIVITY_PROVIDERS:
        raise ProjectReject("project activity provider is invalid", code="registry_corrupt")
    if value["kind"] not in ACTIVITY_KINDS:
        raise ProjectReject("project activity kind is invalid", code="registry_corrupt")
    if value["status"] is not None and value["status"] not in ACTIVITY_STATUSES:
        raise ProjectReject("project activity status is invalid", code="registry_corrupt")
    _activity_summary(value["summary"])
    _activity_token(value["raw_method"], "raw_method")
    for field in ("model_id", "role_id"):
        if value[field] is not None:
            _activity_token(value[field], field)
    if not isinstance(value["requires_attention"], bool):
        raise ProjectReject("project activity attention flag is invalid", code="registry_corrupt")
    _activity_usage(value["usage"])
    _identity_from_mapping(value["identity"])
    if not isinstance(event, dict) or set(event) != {"sequence", "at", "type", "project_id"}:
        raise ProjectReject("project activity event metadata is invalid", code="registry_corrupt")
    if type(event["sequence"]) is not int or event["sequence"] < 1:
        raise ProjectReject("project activity event sequence is invalid", code="registry_corrupt")
    if not isinstance(event["at"], str) or not event["at"]:
        raise ProjectReject("project activity event timestamp is invalid", code="registry_corrupt")
    if event["project_id"] != value["project_id"]:
        raise ProjectReject("project activity event project is invalid", code="registry_corrupt")
    base["event"] = event
    base["fingerprint"] = fingerprint
    if _activity_fingerprint({key: value[key] for key in expected if key not in {"event", "fingerprint"}}) != fingerprint:
        raise ProjectReject("project activity fingerprint does not match payload", code="registry_corrupt")
    return value


def _scoped_name(prefix: str, material: Mapping[str, str]) -> str:
    """One opaque Office path component: a single digest of the whole binding.

    Every derived name becomes a Windows path component
    (``office/inbox/<address>.jsonl``, ``office/channels/<name>.jsonl`` and, deepest of
    all, ``office/cursors/<address>/channel-<name>``), and the Office cursor writer
    stages a ``.tmp-<32 hex>`` sibling next to it.  Digesting each binding field
    separately produced 80-character components whose channel cursor exceeded
    MAX_PATH, so the first channel read degraded with ``FileNotFoundError`` while every
    shorter derived path still worked.  One 96-bit digest over the whole binding keeps
    the same determinism and the same isolation inside that budget.
    """
    encoded = json.dumps(dict(material), sort_keys=True, separators=(",", ":"))
    return prefix + _digest(encoded)


def _scoped_inbox_address(
    project_id: str, stream_id: str, identity: Mapping[str, str]
) -> str:
    """Opaque Office address derived from project, stream, and full runtime identity."""
    return _scoped_name(
        "pa-",
        {
            "agent_id": identity["agent_id"],
            "project_id": project_id,
            "stream_id": stream_id,
        },
    )


def _scoped_presence_address(project_id: str, identity: Mapping[str, str]) -> str:
    return _scoped_name(
        "pp-", {"agent_id": identity["agent_id"], "project_id": project_id}
    )


def _scoped_channel_name(project_id: str, stream_id: str, channel: str) -> str:
    return _scoped_name(
        "pc-",
        {"channel": channel, "project_id": project_id, "stream_id": stream_id},
    )


def _received_key(delivery_id: str, identity: Mapping[str, str]) -> str:
    """Keep one receiver's ACK state distinct from another channel reader's state."""
    return f"{_digest(identity['agent_id'])}-{delivery_id}"


def is_registered_project_inbox(home: Path, address: str) -> bool:
    """Check whether `address` matches an opaque project-stream inbox in the project registry."""
    if (
        not isinstance(address, str)
        or not address.startswith("pa-")
        or len(address) != 3 + 24
        or not all(c in "0123456789abcdef" for c in address[3:])
    ):
        return False
    # This is a shared-store routing query, not a check of local checkout access.
    registry = ProjectRegistry(home=home, allow_native_checkout=False)
    if not registry.path.is_file():
        return False

    def query(state: dict[str, Any]) -> bool:
        for project in state.get("projects", {}).values():
            if not isinstance(project, dict):
                continue
            project_id = project.get("project_id")
            if not isinstance(project_id, str):
                continue
            streams = project.get("streams", {})
            runtimes = project.get("runtimes", {})
            if not isinstance(streams, dict) or not isinstance(runtimes, dict):
                continue
            for stream_id, stream in streams.items():
                if not isinstance(stream, dict):
                    raise ProjectReject("project stream binding is corrupt", code="registry_corrupt")
                correlation = _correlation(stream.get("task_id"), stream.get("thread_id"), stream.get("run_id"))
                if stream.get("stream_id") != stream_id or _stream_id(project_id, correlation) != stream_id:
                    raise ProjectReject("project stream binding is corrupt", code="registry_corrupt")
                for runtime in runtimes.values():
                    identity = _identity_from_mapping(runtime.get("identity") if isinstance(runtime, dict) else None)
                    if _scoped_inbox_address(project_id, stream_id, identity) == address:
                        return True
            # An explicit project-send may deliberately contact a session which has
            # not joined yet. Its prepared delivery is durable routing evidence;
            # an arbitrary prefix, body, inbox file or unrecorded identity is not.
            for delivery in project.get("deliveries", {}).values():
                if not isinstance(delivery, dict) or delivery.get("recipient_address") != address:
                    continue
                binding = delivery.get("stream")
                if not isinstance(binding, dict) or set(binding) != {"stream_id", "task_id", "thread_id", "run_id"}:
                    continue
                registered = streams.get(binding.get("stream_id"))
                if not isinstance(registered, dict) or any(registered.get(k) != v for k, v in binding.items()):
                    continue
                identity = _identity_from_mapping(delivery.get("recipient_identity"))
                if _scoped_inbox_address(project_id, binding["stream_id"], identity) == address:
                    return True
        return False

    return bool(registry._read(query))


def authority_claims(body: Any) -> list[str]:
    """Classify authority-shaped text without executing, interpreting, or forwarding it."""
    if not isinstance(body, str):
        return ["invalid-body"]
    return [name for name, pattern in AUTHORITY_PATTERNS if pattern.search(body)]


@dataclass(frozen=True)
class ProjectStream:
    """One deterministic project stream, isolated by task, thread, and run."""

    project_id: str
    checkout: str
    task_id: str
    thread_id: str
    run_id: str
    stream_id: str

    def correlation(self) -> dict[str, str]:
        return {
            "task_id": self.task_id,
            "thread_id": self.thread_id,
            "run_id": self.run_id,
        }

    def payload(self) -> dict[str, str]:
        return {"stream_id": self.stream_id, **self.correlation()}


class ProjectRegistry:
    """Atomic local registry for canonical project/check-out/stream bindings.

    The registry stores only routing metadata, state-machine fields, and audit
    categories.  It intentionally never stores an untrusted message body.

    Schema 1 records bind one immutable native ``checkout``.  Schema 2 records
    add an additive ``checkout_claims`` list of closed ``{host, path_style,
    path}`` triples (several task worktrees on one host are valid) while the
    original ``checkout`` string is retained as immutable provenance.  Writes
    use schema 2 exactly when host claims exist, so schema-1 binaries fail
    closed instead of silently dropping them; reads accept both schemas but
    refuse a schema that contradicts the stored claim records.
    """

    def __init__(
        self,
        *,
        home: Path,
        now: Callable[[], float] | None = None,
        local_host: str | None = None,
        allow_native_checkout: bool = True,
    ) -> None:
        self.home = Path(home).expanduser()
        self._now = now or time.time
        self.allow_native_checkout = allow_native_checkout
        self.local_host = (
            _canonical_host(local_host)
            if local_host is not None
            else checkout_claims.local_host()
        )

    @property
    def root(self) -> Path:
        return self.home / "office" / "projects"

    @property
    def path(self) -> Path:
        return self.root / "registry.json"

    @property
    def mutex_path(self) -> Path:
        return self.root / "registry.lock"

    @staticmethod
    def _empty_state() -> dict[str, Any]:
        # A registry without host claims stays schema 1 so legacy binaries keep working.
        return {"schema": PROJECT_SCHEMA_LEGACY, "next_event_seq": 0, "projects": {}}

    def _read_locked(self, *, legacy_host: str | None = None) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._empty_state()
        except (OSError, ValueError) as exc:
            raise ProjectReject(
                f"project registry cannot be read: {exc}", code="registry_corrupt"
            ) from exc
        self._validate_state(payload, legacy_host=legacy_host)
        return payload

    def _write_locked(self, state: dict[str, Any]) -> None:
        state["schema"] = (
            PROJECT_SCHEMA_VERSION
            if any("checkout_claims" in record for record in state["projects"].values())
            else PROJECT_SCHEMA_LEGACY
        )
        self._validate_state(state)
        temporary = self.path.with_name(f"{self.path.name}.tmp-{uuid.uuid4().hex}")
        encoded = json.dumps(
            state, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        temporary.write_text(encoded, encoding="utf-8")
        if not replace_with_backoff(temporary, self.path):
            temporary.unlink(missing_ok=True)
            raise CoordError("project registry replace remained busy", code="busy")

    @staticmethod
    def _event(
        state: dict[str, Any], event_type: str, project_id: str
    ) -> dict[str, Any]:
        sequence = state["next_event_seq"] + 1
        state["next_event_seq"] = sequence
        return {
            "sequence": sequence,
            "at": format_ts(time.time()),
            "type": event_type,
            "project_id": project_id,
        }

    def _event_now(
        self, state: dict[str, Any], event_type: str, project_id: str
    ) -> dict[str, Any]:
        event = self._event(state, event_type, project_id)
        event["at"] = format_ts(float(self._now()))
        return event

    def _mutate(self, operation: Callable[[dict[str, Any]], tuple[Any, bool]]) -> Any:
        self.root.mkdir(parents=True, exist_ok=True)
        with exclusive_mutex(self.mutex_path):
            state = self._read_locked()
            result, changed = operation(state)
            if changed:
                self._write_locked(state)
            return _copy_json(result)

    def _read(self, operation: Callable[[dict[str, Any]], Any]) -> Any:
        self.root.mkdir(parents=True, exist_ok=True)
        with exclusive_mutex(self.mutex_path):
            return _copy_json(operation(self._read_locked()))

    def _validate_state(
        self, state: Any, *, legacy_host: str | None = None
    ) -> None:
        # Native-strength legacy validation requires a trusted local connection
        # and an owner host matching this registry's local host (or unattested).
        # An explicit foreign ``legacy_host`` - set by the offline owner
        # migration alone - keeps every legacy checkout purely lexical, so a
        # spelling in this OS's path style is never resolved or lstat'd either.
        allow_native = self.allow_native_checkout and (
            legacy_host is None or legacy_host == self.local_host
        )
        if not isinstance(state, dict) or state.get("schema") not in (
            PROJECT_SCHEMA_LEGACY,
            PROJECT_SCHEMA_VERSION,
        ):
            raise ProjectReject(
                "project registry schema is invalid", code="registry_corrupt"
            )
        if (
            not isinstance(state.get("next_event_seq"), int)
            or state["next_event_seq"] < 0
        ):
            raise ProjectReject(
                "project registry event sequence is invalid", code="registry_corrupt"
            )
        projects = state.get("projects")
        if not isinstance(projects, dict):
            raise ProjectReject(
                "project registry projects are invalid", code="registry_corrupt"
            )
        checkouts: dict[str, str] = {}
        claims: dict[tuple[str, str, str], str] = {}
        aliases: dict[str, str] = {}
        has_host_claims = False
        for project_id, record in projects.items():
            if (
                not isinstance(project_id, str)
                or canonicalize_project_id(project_id) != project_id
            ):
                raise ProjectReject(
                    "project registry has a noncanonical project id",
                    code="registry_corrupt",
                )
            if not isinstance(record, dict) or record.get("project_id") != project_id:
                raise ProjectReject(
                    "project registry project record is invalid",
                    code="registry_corrupt",
                )
            if "checkout_claims" not in record:
                # Legacy classification is the ABSENT key alone: an explicit
                # ``checkout_claims: null`` is malformed host-aware data, not a
                # legacy record.  Treating null as legacy would let a mixed
                # schema-2 registry accept it while ``migrate_legacy`` skips the
                # same record (the key exists) and ``register`` later iterates
                # None; classifying by key presence keeps all three fail-closed.
                checkout = record.get("checkout")
                _validate_stored_checkout(checkout, allow_native=allow_native)
                previous = checkouts.setdefault(checkout, project_id)
                if previous != project_id:
                    raise ProjectReject(
                        "project registry binds one checkout to multiple projects",
                        code="registry_corrupt",
                    )
            else:
                listed_claims = record["checkout_claims"]
                # Fail closed on the schema boundary: a schema-1 file must never
                # carry host claims a legacy binary would silently drop, and the
                # writer only emits schema 2 when at least one claim exists.
                if state["schema"] == PROJECT_SCHEMA_LEGACY:
                    raise ProjectReject(
                        "project registry schema 1 cannot carry host checkout claims",
                        code="registry_corrupt",
                    )
                if not isinstance(listed_claims, list) or not listed_claims:
                    raise ProjectReject(
                        "project registry checkout claims are invalid",
                        code="registry_corrupt",
                    )
                has_host_claims = True
                seen_keys: set[tuple[str, str, str]] = set()
                normalized_claims: list[dict[str, str]] = []
                for item in listed_claims:
                    try:
                        claim = checkout_claims.validate_claim(item)
                    except CoordError as exc:
                        raise ProjectReject(
                            "project registry checkout claim is invalid",
                            code="registry_corrupt",
                        ) from exc
                    key = checkout_claims.claim_key(claim)
                    if key in seen_keys:
                        raise ProjectReject(
                            "project registry duplicates a checkout claim",
                            code="registry_corrupt",
                        )
                    seen_keys.add(key)
                    normalized_claims.append(claim)
                    owner = claims.setdefault(key, project_id)
                    if owner != project_id:
                        raise ProjectReject(
                            "project registry binds one checkout claim to multiple projects",
                            code="registry_corrupt",
                        )
                if normalized_claims != listed_claims:
                    raise ProjectReject(
                        "project registry checkout claims are not canonical",
                        code="registry_corrupt",
                    )
                # The legacy field is immutable provenance: the path of the first claim.
                if record.get("checkout") != normalized_claims[0]["path"]:
                    raise ProjectReject(
                        "project registry checkout provenance is invalid",
                        code="registry_corrupt",
                    )
            listed_aliases = record.get("aliases")
            if not isinstance(listed_aliases, list) or listed_aliases != sorted(
                set(listed_aliases)
            ):
                raise ProjectReject(
                    "project registry aliases are invalid", code="registry_corrupt"
                )
            for alias in listed_aliases:
                if (
                    not isinstance(alias, str)
                    or canonicalize_project_id(alias) != alias
                    or alias == project_id
                ):
                    raise ProjectReject(
                        "project registry alias is invalid", code="registry_corrupt"
                    )
                existing = aliases.setdefault(alias, project_id)
                if existing != project_id or alias in projects and alias != project_id:
                    raise ProjectReject(
                        "project registry aliases are ambiguous",
                        code="registry_corrupt",
                    )
            for key in ("streams", "runtimes", "deliveries", "received"):
                if not isinstance(record.get(key), dict):
                    raise ProjectReject(
                        f"project registry {key} are invalid", code="registry_corrupt"
                    )
            if not isinstance(record.get("audits"), list):
                raise ProjectReject(
                    "project registry audits are invalid", code="registry_corrupt"
                )
            activity = record.get("activity", [])
            if not isinstance(activity, list) or len(activity) > ACTIVITY_MAX_EVENTS:
                raise ProjectReject(
                    "project registry activity is invalid", code="registry_corrupt"
                )
            seen_activity: set[str] = set()
            for item in activity:
                checked_activity = _validate_activity_record(item)
                source_event_id = checked_activity["source_event_id"]
                if source_event_id in seen_activity:
                    raise ProjectReject(
                        "project registry activity ids must be unique", code="registry_corrupt"
                    )
                seen_activity.add(source_event_id)
            for event_key in ("created_event", "updated_event"):
                event = record.get(event_key)
                if not isinstance(event, dict) or not isinstance(
                    event.get("sequence"), int
                ):
                    raise ProjectReject(
                        "project registry event metadata is invalid",
                        code="registry_corrupt",
                    )
        if state["schema"] == PROJECT_SCHEMA_VERSION and not has_host_claims:
            raise ProjectReject(
                "project registry schema 2 requires host checkout claims",
                code="registry_corrupt",
            )
        for (host, _style, path), owner in claims.items():
            if host != self.local_host:
                continue
            legacy_owner = checkouts.get(path)
            if legacy_owner is not None and legacy_owner != owner:
                raise ProjectReject(
                    "project registry duplicates a local checkout across record kinds",
                    code="registry_corrupt",
                )

    @staticmethod
    def _resolve_id(state: dict[str, Any], raw: Any) -> str:
        candidate = canonicalize_project_id(raw)
        if candidate in state["projects"]:
            return candidate
        found = [
            project_id
            for project_id, record in state["projects"].items()
            if candidate in record["aliases"]
        ]
        if len(found) != 1:
            raise ProjectReject(
                "project id or alias is unknown or ambiguous", code="unknown_project"
            )
        return found[0]

    @staticmethod
    def _record(state: dict[str, Any], project_id: str) -> dict[str, Any]:
        record = state["projects"].get(project_id)
        if not isinstance(record, dict):
            raise ProjectReject("project record is missing", code="unknown_project")
        return record

    def register(
        self,
        project_id: Any,
        checkout: Any,
        *,
        aliases: Sequence[Any] = (),
        checkout_host: Any = None,
    ) -> dict[str, Any]:
        """Register one project or verify its immutable checkout binding.

        Without ``checkout_host`` (an INTERNAL keyword, never an RPC field) the
        legacy contract is unchanged: one immutable native checkout per project,
        never rebindable to another project.  With an explicit host the checkout
        becomes a closed lexical ``{host, path_style, path}`` claim; native
        filesystem validation applies only for a trusted local connection whose
        host is this registry's local host. An existing host-aware record accepts
        additional claims
        (multiple task worktrees on one host are valid), exact claim triples
        are deduplicated, and one claim triple can never belong to two projects.
        Adding host claims to a populated legacy registry is refused until an
        explicit offline :meth:`migrate_legacy` pins the owner host.
        """
        canonical = canonicalize_project_id(project_id)
        normalized_aliases = sorted(
            {canonicalize_project_id(alias) for alias in aliases}
        )
        if canonical in normalized_aliases:
            raise ProjectReject(
                "project id cannot also be its alias", code="ambiguous_alias"
            )
        claim: dict[str, str] | None = None
        bound_checkout: str | None = None
        if checkout_host is None:
            bound_checkout = canonicalize_checkout(checkout)
        else:
            host = _canonical_host(checkout_host)
            if self.allow_native_checkout and host == self.local_host:
                bound_checkout = canonicalize_checkout(checkout)
                claim = _checkout_claim(
                    bound_checkout, host=host, path_style=_native_path_style()
                )
            else:
                claim = _checkout_claim(checkout, host=host)

        def operation(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            for other_id, other in state["projects"].items():
                claimed = {other_id, *other["aliases"]}
                if other_id != canonical and any(
                    alias in claimed for alias in normalized_aliases
                ):
                    raise ProjectReject(
                        "project alias is ambiguous", code="ambiguous_alias"
                    )
                if other_id != canonical and canonical in other["aliases"]:
                    raise ProjectReject(
                        "project id collides with an existing alias",
                        code="ambiguous_alias",
                    )
            if claim is None:
                for other_id, other in state["projects"].items():
                    if other_id != canonical and other["checkout"] == bound_checkout:
                        raise ProjectReject(
                            "checkout is already bound to another project",
                            code="checkout_bound",
                        )
                existing = state["projects"].get(canonical)
                if existing is not None:
                    if existing["checkout"] != bound_checkout:
                        raise ProjectReject(
                            "project id is already bound to another checkout",
                            code="project_checkout_mismatch",
                        )
                    merged_aliases = sorted(
                        set(existing["aliases"]) | set(normalized_aliases)
                    )
                    if merged_aliases == existing["aliases"]:
                        return existing, False
                    event = self._event_now(state, "project_aliases_updated", canonical)
                    existing["aliases"] = merged_aliases
                    existing["updated_event"] = event
                    return existing, True
                event = self._event_now(state, "project_registered", canonical)
                record = {
                    "project_id": canonical,
                    "checkout": bound_checkout,
                    "aliases": normalized_aliases,
                    "streams": {},
                    "runtimes": {},
                    "deliveries": {},
                    "received": {},
                    "audits": [],
                    "activity": [],
                    "created_event": event,
                    "updated_event": event,
                }
                state["projects"][canonical] = record
                return record, True
            key = checkout_claims.claim_key(claim)
            if state["schema"] == PROJECT_SCHEMA_LEGACY and state["projects"]:
                raise ProjectReject(
                    "registry holds legacy single-checkout records; explicit offline "
                    "migrate_legacy(source_host) is required before adding host claims",
                    code="legacy_owner_required",
                )
            for other_id, other in state["projects"].items():
                if other_id == canonical:
                    continue
                if key in {
                    checkout_claims.claim_key(item)
                    for item in other.get("checkout_claims", [])
                }:
                    raise ProjectReject(
                        "checkout claim is already bound to another project",
                        code="checkout_bound",
                    )
                if (
                    "checkout_claims" not in other
                    and claim["host"] == self.local_host
                    and other["checkout"] == claim["path"]
                ):
                    raise ProjectReject(
                        "checkout is already bound to another project",
                        code="checkout_bound",
                    )
            existing = state["projects"].get(canonical)
            if existing is not None:
                if "checkout_claims" not in existing:
                    raise ProjectReject(
                        "project record predates host claims; explicit offline "
                        "migrate_legacy(source_host) is required first",
                        code="legacy_owner_required",
                    )
                known = {
                    checkout_claims.claim_key(item)
                    for item in existing["checkout_claims"]
                }
                merged_aliases = sorted(
                    set(existing["aliases"]) | set(normalized_aliases)
                )
                if key in known:
                    if merged_aliases == existing["aliases"]:
                        return existing, False
                    event = self._event_now(state, "project_aliases_updated", canonical)
                    existing["aliases"] = merged_aliases
                    existing["updated_event"] = event
                    return existing, True
                event = self._event_now(
                    state, "project_checkout_claim_added", canonical
                )
                existing["checkout_claims"].append(claim)
                existing["aliases"] = merged_aliases
                existing["updated_event"] = event
                return existing, True
            event = self._event_now(state, "project_registered", canonical)
            record = {
                "project_id": canonical,
                "checkout": claim["path"],
                "checkout_claims": [claim],
                "aliases": normalized_aliases,
                "streams": {},
                "runtimes": {},
                "deliveries": {},
                "received": {},
                "audits": [],
                "activity": [],
                "created_event": event,
                "updated_event": event,
            }
            state["projects"][canonical] = record
            return record, True

        return self._mutate(operation)

    def migrate_legacy(self, source_host: Any) -> dict[str, Any]:
        """Offline owner migration: pin every legacy checkout to one explicit host.

        This is a direct owner operation, not an RPC.  ``source_host`` is
        required, canonical, and never inferred from the OS or a path spelling.
        Native-strength checkout validation applies only when ``source_host`` is
        this registry's local host; a foreign source is a lexical attestation,
        not proof of ownership.  The original registry bytes are snapshotted to
        a unique no-overwrite backup before the first registry write, checkout
        strings are not rewritten, and a repeated migration is a no-op.
        """
        host = _canonical_host(source_host)
        self.root.mkdir(parents=True, exist_ok=True)
        with exclusive_mutex(self.mutex_path):
            # The attested owner host decides validation strength before any
            # legacy checkout is looked at: a foreign source stays lexical even
            # when a checkout is spelled in this OS's native path style.
            state = self._read_locked(legacy_host=host)
            legacy_ids = [
                project_id
                for project_id, record in state["projects"].items()
                if "checkout_claims" not in record
            ]
            if not legacy_ids:
                return {
                    "status": "ok",
                    "code": "already_migrated",
                    "source_host": host,
                    "migrated": 0,
                    "projects": [],
                    "backup_path": None,
                    "backup_sha256": None,
                }
            original = self.path.read_bytes()
            backup = self.path.with_name(
                f"{self.path.name}.legacy-backup-{uuid.uuid4().hex}"
            )
            exclusive_create(backup, original)
            native = self.allow_native_checkout and host == self.local_host
            for project_id in legacy_ids:
                record = state["projects"][project_id]
                checkout = record["checkout"]
                if native:
                    canonicalize_checkout(checkout)
                    claim = _checkout_claim(
                        checkout, host=host, path_style=_native_path_style()
                    )
                else:
                    style = _checkout_path_style(checkout)
                    if style is None:
                        raise ProjectReject(
                            "legacy checkout cannot be attested lexically",
                            code="invalid_checkout",
                        )
                    claim = _checkout_claim(checkout, host=host, path_style=style)
                record["checkout_claims"] = [claim]
                record["updated_event"] = self._event_now(
                    state, "legacy_checkout_migrated", project_id
                )
            self._write_locked(state)
        return {
            "status": "ok",
            "code": "migrated",
            "source_host": host,
            "migrated": len(legacy_ids),
            "projects": sorted(legacy_ids),
            "backup_path": str(backup),
            "backup_sha256": hashlib.sha256(original).hexdigest(),
        }

    def resolve(self, project_or_alias: Any) -> dict[str, Any]:
        """Read one canonical project record without allowing CWD-derived aliases."""
        return self._read(
            lambda state: self._record(state, self._resolve_id(state, project_or_alias))
        )

    def ensure_stream(
        self, project_or_alias: Any, correlation: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Register and return one task/thread/run stream with a stable deterministic id."""
        values = _correlation(
            correlation.get("task_id"),
            correlation.get("thread_id"),
            correlation.get("run_id"),
        )

        def operation(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            project_id = self._resolve_id(state, project_or_alias)
            record = self._record(state, project_id)
            stream_id = _stream_id(project_id, values)
            existing = record["streams"].get(stream_id)
            if existing is not None:
                expected = {"stream_id": stream_id, **values}
                if any(existing.get(key) != value for key, value in expected.items()):
                    raise ProjectReject(
                        "stream id collision has incompatible correlation",
                        code="registry_corrupt",
                    )
                return {"project": record, "stream": existing}, False
            event = self._event_now(state, "stream_registered", project_id)
            stream = {"stream_id": stream_id, **values, "created_event": event}
            record["streams"][stream_id] = stream
            record["updated_event"] = event
            return {"project": record, "stream": stream}, True

        return self._mutate(operation)

    def record_activity(
        self,
        project_or_alias: Any,
        *,
        stream: ProjectStream,
        identity: Mapping[str, str],
        source_event_id: Any,
        provider_id: Any,
        kind: Any,
        status: Any = None,
        summary: Any = None,
        raw_method: Any,
        model_id: Any = None,
        role_id: Any = None,
        requires_attention: Any = False,
        usage: Any = None,
    ) -> dict[str, Any]:
        """Record one bounded provider activity event for a registered stream.

        The registry is the only writer and derives the event sequence/time.
        ``source_event_id`` is the idempotence key supplied by the provider
        adapter; a reused key with another payload is a visible conflict.
        """
        if not isinstance(stream, ProjectStream):
            raise ProjectReject("activity stream is invalid", code="invalid_activity")
        payload = _activity_payload(
            project_id=stream.project_id,
            stream=stream,
            identity=identity,
            source_event_id=source_event_id,
            provider_id=provider_id,
            kind=kind,
            status=status,
            summary=summary,
            raw_method=raw_method,
            model_id=model_id,
            role_id=role_id,
            requires_attention=requires_attention,
            usage=usage,
        )
        fingerprint = _activity_fingerprint(payload)

        def operation(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            project_id = self._resolve_id(state, project_or_alias)
            record = self._record(state, project_id)
            if project_id != stream.project_id or stream.stream_id not in record["streams"]:
                raise ProjectReject(
                    "activity stream is not registered for this project",
                    code="stream_project_mismatch",
                )
            registered = record["streams"][stream.stream_id]
            if any(registered.get(key) != stream.payload()[key] for key in stream.payload()):
                raise ProjectReject("activity stream correlation is invalid", code="invalid_activity")
            activity = record.setdefault("activity", [])
            for existing in activity:
                if existing.get("source_event_id") != payload["source_event_id"]:
                    continue
                if existing.get("fingerprint") == fingerprint:
                    return {"activity": existing, "duplicate": True}, False
                raise ProjectReject(
                    "source_event_id is already bound to another activity payload",
                    code="activity_conflict",
                )
            if len(activity) >= ACTIVITY_MAX_EVENTS:
                raise ProjectReject("project activity capacity reached", code="activity_capacity")
            event = self._event_now(state, "activity_recorded", project_id)
            entry = {**payload, "event": event, "fingerprint": fingerprint}
            _validate_activity_record(entry)
            activity.append(entry)
            record["updated_event"] = event
            return {"activity": entry, "duplicate": False}, True

        return self._mutate(operation)

    def activities(
        self,
        project_or_alias: Any,
        *,
        stream: ProjectStream | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Read newest bounded activity events, optionally for one stream."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ProjectReject("activity limit must be between 1 and 500", code="invalid_limit")

        def operation(state: dict[str, Any]) -> list[dict[str, Any]]:
            project_id = self._resolve_id(state, project_or_alias)
            record = self._record(state, project_id)
            activity = record.get("activity", [])
            if stream is not None:
                if not isinstance(stream, ProjectStream) or stream.project_id != project_id:
                    raise ProjectReject("activity stream is not bound to this project", code="stream_project_mismatch")
                registered = record["streams"].get(stream.stream_id)
                if registered is None or any(registered.get(key) != stream.payload()[key] for key in stream.payload()):
                    raise ProjectReject("activity stream correlation is invalid", code="invalid_activity")
                activity = [item for item in activity if item.get("stream_id") == stream.stream_id]
            return activity[-limit:]

        return self._read(operation)

    def bind_runtime(
        self, project_or_alias: Any, identity: Mapping[str, str]
    ) -> dict[str, Any]:
        """Persist the exact harness-host-session identity and its strength for this project."""
        normalized = _identity_from_mapping(identity)

        def operation(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            project_id = self._resolve_id(state, project_or_alias)
            record = self._record(state, project_id)
            existing = record["runtimes"].get(normalized["agent_id"])
            if (
                existing is not None
                and existing.get("identity", {}).get("strength")
                != normalized["strength"]
            ):
                raise ProjectReject(
                    "runtime identity strength changed for the same session",
                    code="identity_strength_mismatch",
                )
            event = self._event_now(state, "runtime_bound", project_id)
            runtime = {
                "identity": normalized,
                "strength": normalized["strength"],
                "last_event": event,
            }
            record["runtimes"][normalized["agent_id"]] = runtime
            record["updated_event"] = event
            return runtime, True

        return self._mutate(operation)

    def record_presence(
        self, project_or_alias: Any, identity: Mapping[str, str], ttl_s: int
    ) -> dict[str, Any]:
        if isinstance(ttl_s, bool) or not isinstance(ttl_s, int) or ttl_s < 1:
            raise ProjectReject(
                "presence ttl must be a positive integer", code="invalid_ttl"
            )
        normalized = _identity_from_mapping(identity)

        def operation(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            project_id = self._resolve_id(state, project_or_alias)
            record = self._record(state, project_id)
            runtime = record["runtimes"].get(normalized["agent_id"])
            if runtime is None:
                raise ProjectReject(
                    "runtime must be bound before presence", code="unknown_runtime"
                )
            event = self._event_now(state, "presence_claimed", project_id)
            runtime["presence"] = {
                "ttl_s": ttl_s,
                "expires_epoch": float(self._now()) + ttl_s,
                "event": event,
            }
            runtime["last_event"] = event
            record["updated_event"] = event
            return runtime["presence"], True

        return self._mutate(operation)

    def presence_record(
        self, project_or_alias: Any, identity: Mapping[str, str]
    ) -> dict[str, Any] | None:
        normalized = _identity_from_mapping(identity)

        def operation(state: dict[str, Any]) -> Any:
            project_id = self._resolve_id(state, project_or_alias)
            runtime = self._record(state, project_id)["runtimes"].get(
                normalized["agent_id"]
            )
            if not isinstance(runtime, dict):
                return None
            return runtime.get("presence")

        return self._read(operation)

    def is_registered_inbox_address(self, address: str) -> bool:
        """Check whether `address` matches an opaque project-stream inbox in this registry."""
        return is_registered_project_inbox(self.home, address)

    def prepare_delivery(
        self,
        project_or_alias: Any,
        *,
        delivery_id: str,
        stream: ProjectStream,
        recipient_identity: Mapping[str, str],
        recipient_address: str,
        ack_ttl_s: int,
    ) -> dict[str, Any]:
        if not MESSAGE_ID_RE.match(delivery_id):
            raise ProjectReject("delivery id is invalid", code="invalid_delivery")
        if (
            isinstance(ack_ttl_s, bool)
            or not isinstance(ack_ttl_s, int)
            or ack_ttl_s < 1
        ):
            raise ProjectReject(
                "ack ttl must be a positive integer", code="invalid_ack_ttl"
            )
        recipient = _identity_from_mapping(recipient_identity)

        def operation(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            project_id = self._resolve_id(state, project_or_alias)
            record = self._record(state, project_id)
            if delivery_id in record["deliveries"]:
                raise ProjectReject(
                    "delivery id already exists", code="duplicate_delivery"
                )
            if stream.stream_id not in record.get("streams", {}):
                raise ProjectReject(
                    "stream is not registered for this project",
                    code="stream_project_mismatch",
                )
            registered = record["streams"][stream.stream_id]
            if stream.project_id != project_id or any(registered.get(k) != v for k, v in stream.payload().items()):
                raise ProjectReject("stream binding is inconsistent", code="stream_project_mismatch")
            # Both direct inboxes and project channels prepare a delivery here.
            # Their distinct derived addresses are produced by ProjectTransport;
            # direct inbox admission is checked by Office before envelope write.
            event = self._event_now(state, "delivery_prepared", project_id)
            delivery = {
                "delivery_id": delivery_id,
                "stream": stream.payload(),
                "recipient_identity": recipient,
                "recipient_address": recipient_address,
                "recipient_kind": "inbox",
                "ack_ttl_s": ack_ttl_s,
                "ack_deadline_epoch": float(self._now()) + ack_ttl_s,
                "state": "PENDING_ACK",
                "attempts": 1,
                "prepared_event": event,
            }
            record["deliveries"][delivery_id] = delivery
            record["updated_event"] = event
            return delivery, True

        return self._mutate(operation)

    def finish_delivery(
        self,
        project_or_alias: Any,
        delivery_id: str,
        transport: Mapping[str, Any],
        *,
        recipient_kind: str = "inbox",
    ) -> dict[str, Any]:
        """Record exactly one delivery attempt; this method never schedules a retry."""

        def operation(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            project_id = self._resolve_id(state, project_or_alias)
            record = self._record(state, project_id)
            delivery = record["deliveries"].get(delivery_id)
            if not isinstance(delivery, dict):
                raise ProjectReject("delivery is unknown", code="unknown_delivery")
            if recipient_kind not in {"inbox", "channel"}:
                raise ProjectReject(
                    "delivery recipient kind is invalid", code="invalid_delivery"
                )
            if "transport" in delivery:
                return delivery, False
            success = (
                transport.get("status") == "ok" and transport.get("code") == "sent"
            )
            event = self._event_now(
                state,
                "delivery_committed" if success else "delivery_failed",
                project_id,
            )
            delivery["state"] = "PENDING_ACK" if success else "DELIVERY_FAILED"
            delivery["transport"] = {
                "status": transport.get("status"),
                "code": transport.get("code"),
                "transport_id": transport.get("id")
                if isinstance(transport.get("id"), str)
                else None,
            }
            delivery["recipient_kind"] = recipient_kind
            delivery["delivery_event"] = event
            record["updated_event"] = event
            return delivery, True

        return self._mutate(operation)

    def record_inbound(
        self,
        project_or_alias: Any,
        *,
        delivery_id: str,
        stream: ProjectStream,
        sender_address: str,
        recipient_identity: Mapping[str, str],
        audit_rejected: bool,
    ) -> dict[str, Any]:
        if not MESSAGE_ID_RE.match(delivery_id):
            raise ProjectReject(
                "inbound delivery id is invalid", code="invalid_delivery"
            )

        def operation(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            project_id = self._resolve_id(state, project_or_alias)
            record = self._record(state, project_id)
            recipient = _identity_from_mapping(recipient_identity)
            key = _received_key(delivery_id, recipient)
            existing = record["received"].get(key)
            if existing is not None:
                if (
                    existing.get("sender_address") != sender_address
                    or existing.get("stream") != stream.payload()
                    or existing.get("recipient_agent_id") != recipient["agent_id"]
                ):
                    raise ProjectReject(
                        "duplicate delivery conflicts with recorded routing",
                        code="delivery_conflict",
                    )
                return {"received": existing, "duplicate": True}, False
            event = self._event_now(state, "delivery_received", project_id)
            received = {
                "delivery_id": delivery_id,
                "stream": stream.payload(),
                "sender_address": sender_address,
                "recipient_agent_id": recipient["agent_id"],
                "audit_rejected": audit_rejected,
                "ack_state": "PENDING",
                "received_event": event,
            }
            record["received"][key] = received
            record["updated_event"] = event
            return {"received": received, "duplicate": False}, True

        return self._mutate(operation)

    def reserve_ack(
        self,
        project_or_alias: Any,
        delivery_id: str,
        recipient_identity: Mapping[str, str],
    ) -> dict[str, Any]:
        """Reserve one ACK attempt.  A failed attempt remains failed; no retry is implicit."""

        def operation(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            project_id = self._resolve_id(state, project_or_alias)
            record = self._record(state, project_id)
            recipient = _identity_from_mapping(recipient_identity)
            received = record["received"].get(_received_key(delivery_id, recipient))
            if not isinstance(received, dict):
                raise ProjectReject(
                    "received delivery is unknown", code="unknown_delivery"
                )
            if received.get("audit_rejected"):
                return {"code": "audit_rejected", "received": received}, False
            state_value = received.get("ack_state")
            if state_value != "PENDING":
                return {"code": "duplicate_ack", "received": received}, False
            event = self._event_now(state, "ack_reserved", project_id)
            received["ack_state"] = "RESERVED"
            received["ack_event"] = event
            record["updated_event"] = event
            return {"code": "ack_reserved", "received": received}, True

        return self._mutate(operation)

    def finish_ack(
        self,
        project_or_alias: Any,
        delivery_id: str,
        result: Mapping[str, Any],
        *,
        recipient_identity: Mapping[str, str],
    ) -> dict[str, Any]:
        """Close one recipient's reserved ACK attempt without retrying it automatically.

        ``received`` is keyed by ``_received_key(delivery_id, recipient)`` because two
        readers of the same channel delivery each own one ACK; finalizing by delivery id
        alone would never find the entry ``record_inbound`` and ``reserve_ack`` wrote.
        """

        def operation(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            project_id = self._resolve_id(state, project_or_alias)
            record = self._record(state, project_id)
            recipient = _identity_from_mapping(recipient_identity)
            received = record["received"].get(_received_key(delivery_id, recipient))
            if (
                not isinstance(received, dict)
                or received.get("ack_state") != "RESERVED"
            ):
                raise ProjectReject("ack is not reserved", code="ack_not_reserved")
            success = result.get("status") == "ok" and result.get("code") == "sent"
            event = self._event_now(
                state, "ack_sent" if success else "ack_failed", project_id
            )
            received["ack_state"] = "SENT" if success else "FAILED"
            received["ack_id"] = (
                result.get("id")
                if success and isinstance(result.get("id"), str)
                else None
            )
            received["ack_result"] = {
                "status": result.get("status"),
                "code": result.get("code"),
            }
            received["ack_event"] = event
            record["updated_event"] = event
            return received, True

        return self._mutate(operation)

    def record_ack(
        self, project_or_alias: Any, delivery_id: str, sender_address: str
    ) -> dict[str, Any]:
        """Apply one observed application ACK to the sender-side delivery ledger."""

        def operation(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            project_id = self._resolve_id(state, project_or_alias)
            record = self._record(state, project_id)
            delivery = record["deliveries"].get(delivery_id)
            if not isinstance(delivery, dict):
                return {"code": "unknown_ack", "delivery": None}, False
            if (
                delivery.get("recipient_kind", "inbox") == "inbox"
                and delivery.get("recipient_address") != sender_address
            ):
                return {"code": "ack_sender_mismatch", "delivery": delivery}, False
            if delivery.get("state") == "CANCELLED":
                return {"code": "cancelled", "delivery": delivery}, False
            if delivery.get("state") == "ACKNOWLEDGED":
                return {"code": "duplicate_ack", "delivery": delivery}, False
            if delivery.get("state") == "DELIVERY_FAILED":
                return {"code": "failed_delivery", "delivery": delivery}, False
            event = self._event_now(state, "ack_observed", project_id)
            delivery["state"] = "ACKNOWLEDGED"
            delivery["ack_event"] = event
            delivery["ack_sender_address"] = sender_address
            record["updated_event"] = event
            return {"code": "acknowledged", "delivery": delivery}, True

        return self._mutate(operation)

    def cancel_delivery(
        self, project_or_alias: Any, delivery_id: str
    ) -> dict[str, Any]:
        """Cancel application follow-up only; durable transport delivery is never retracted or retried."""

        def operation(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            project_id = self._resolve_id(state, project_or_alias)
            record = self._record(state, project_id)
            delivery = record["deliveries"].get(delivery_id)
            if not isinstance(delivery, dict):
                raise ProjectReject("delivery is unknown", code="unknown_delivery")
            if delivery.get("state") == "CANCELLED":
                return delivery, False
            if delivery.get("state") == "ACKNOWLEDGED":
                return delivery, False
            event = self._event_now(state, "delivery_cancelled", project_id)
            delivery["state"] = "CANCELLED"
            delivery["cancel_event"] = event
            record["updated_event"] = event
            return delivery, True

        return self._mutate(operation)

    def delivery_state(self, project_or_alias: Any, delivery_id: str) -> dict[str, Any]:
        """Return ACK state; a missed deadline becomes ``UNKNOWN`` and never triggers a retry."""

        def operation(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            project_id = self._resolve_id(state, project_or_alias)
            record = self._record(state, project_id)
            delivery = record["deliveries"].get(delivery_id)
            if not isinstance(delivery, dict):
                raise ProjectReject("delivery is unknown", code="unknown_delivery")
            if delivery.get("state") == "PENDING_ACK" and float(self._now()) >= float(
                delivery["ack_deadline_epoch"]
            ):
                event = self._event_now(state, "ack_unknown", project_id)
                delivery["state"] = "UNKNOWN"
                delivery["unknown_event"] = event
                record["updated_event"] = event
                return delivery, True
            return delivery, False

        return self._mutate(operation)

    def record_audit(
        self,
        project_or_alias: Any,
        *,
        direction: str,
        delivery_id: str | None,
        reasons: Sequence[str],
    ) -> dict[str, Any]:
        if direction not in {"outbound", "inbound", "invalid-envelope"}:
            raise ProjectReject("audit direction is invalid", code="invalid_audit")
        safe_reasons = sorted(
            {str(reason) for reason in reasons if isinstance(reason, str) and reason}
        )
        if not safe_reasons:
            raise ProjectReject("audit reasons are empty", code="invalid_audit")

        def operation(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            project_id = self._resolve_id(state, project_or_alias)
            record = self._record(state, project_id)
            marker = (direction, delivery_id, tuple(safe_reasons))
            for existing in record["audits"]:
                if (
                    existing.get("direction"),
                    existing.get("delivery_id"),
                    tuple(existing.get("reasons", [])),
                ) == marker:
                    return existing, False
            event = self._event_now(state, "authority_claim_rejected", project_id)
            audit = {
                "status": "rejected",
                "direction": direction,
                "delivery_id": delivery_id,
                "reasons": safe_reasons,
                "event": event,
                "handling": "Authority claims are untrusted data and were not executed or escalated.",
            }
            record["audits"].append(audit)
            record["updated_event"] = event
            return audit, True

        return self._mutate(operation)

    def audits(self, project_or_alias: Any) -> list[dict[str, Any]]:
        return self._read(
            lambda state: list(
                self._record(state, self._resolve_id(state, project_or_alias))["audits"]
            )
        )


class ProjectTransport:
    """Use a registered project stream without changing legacy Office behavior.

    ``harness``, ``host`` and ``session_id`` are required to keep the original
    ``<harness>-<host>-<session>`` identity explicit.  The Office address is an
    opaque deterministic derivative so identical local names in two projects
    cannot share inboxes, cursors, recipients, or wake signals.
    """

    def __init__(
        self,
        *,
        home: Path,
        project_id: str,
        harness: str,
        host: str,
        session_id: str,
        now: Callable[[], float] | None = None,
        ack_ttl_s: int = 60,
        office_kwargs: Mapping[str, Any] | None = None,
        allow_native_checkout: bool = True,
    ) -> None:
        if (
            isinstance(ack_ttl_s, bool)
            or not isinstance(ack_ttl_s, int)
            or ack_ttl_s < 1
        ):
            raise ProjectReject(
                "ack ttl must be a positive integer", code="invalid_ack_ttl"
            )
        self.registry = ProjectRegistry(home=home, now=now,
                                        allow_native_checkout=allow_native_checkout)
        record = self.registry.resolve(project_id)
        self.project_id = record["project_id"]
        self.checkout = record["checkout"]
        self.identity = agent_identity(harness, host=host, session_id=session_id)
        self.identity = _identity_from_mapping(self.identity)
        self._now = now or time.time
        self.ack_ttl_s = ack_ttl_s
        self.office_kwargs = dict(office_kwargs or {})
        self.registry.bind_runtime(self.project_id, self.identity)

    def _result(self, status: str, code: str, **fields: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": status,
            "code": code,
            "project_id": self.project_id,
            "checkout": self.checkout,
            "identity": _copy_json(self.identity),
        }
        payload.update(fields)
        return payload

    def _guard(self, op: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        try:
            return fn()
        except ProjectReject as exc:
            return self._result(
                "error", exc.code, op=op, message=str(exc), **exc.payload
            )
        except (
            OSError,
            CoordError,
            ValueError,
            TypeError,
            RuntimeError,
            LookupError,
            AttributeError,
        ) as exc:
            return self._result(
                "degraded",
                "project_transport_unavailable",
                op=op,
                error=f"{type(exc).__name__}: {exc}",
                message="project transport is unavailable; continue without automatic retry",
            )

    def stream(self, *, task_id: str, thread_id: str, run_id: str) -> ProjectStream:
        """Create or resolve one explicit task/thread/run stream in this registered project."""
        values = _correlation(task_id, thread_id, run_id)
        result = self.registry.ensure_stream(self.project_id, values)
        stream = result["stream"]
        return ProjectStream(
            project_id=self.project_id,
            checkout=self.checkout,
            task_id=stream["task_id"],
            thread_id=stream["thread_id"],
            run_id=stream["run_id"],
            stream_id=stream["stream_id"],
        )

    def record_activity(
        self,
        activity: Mapping[str, Any],
        *,
        stream: ProjectStream,
    ) -> dict[str, Any]:
        """Persist typed provider activity without accepting a raw message body."""

        def operation() -> dict[str, Any]:
            if not isinstance(activity, Mapping):
                raise ProjectReject("activity must be a mapping", code="invalid_activity")
            allowed = {
                "source_event_id",
                "provider_id",
                "kind",
                "status",
                "summary",
                "raw_method",
                "model_id",
                "role_id",
                "requires_attention",
                "usage",
            }
            unknown = sorted(set(activity) - allowed)
            if unknown:
                raise ProjectReject(
                    "activity has unsupported fields: " + ", ".join(str(item) for item in unknown),
                    code="invalid_activity",
                )
            binding = self._require_stream(stream)
            result = self.registry.record_activity(
                self.project_id,
                stream=binding,
                identity=self.identity,
                source_event_id=activity.get("source_event_id"),
                provider_id=activity.get("provider_id"),
                kind=activity.get("kind"),
                status=activity.get("status"),
                summary=activity.get("summary"),
                raw_method=activity.get("raw_method"),
                model_id=activity.get("model_id"),
                role_id=activity.get("role_id"),
                requires_attention=activity.get("requires_attention", False),
                usage=activity.get("usage"),
            )
            entry = result["activity"]
            return self._result(
                "ok",
                "deduplicated" if result["duplicate"] else "recorded",
                duplicate=result["duplicate"],
                source_event_id=entry["source_event_id"],
                event_sequence=entry["event"]["sequence"],
                activity=entry,
            )

        return self._guard("record_activity", operation)

    def read_activity(
        self,
        *,
        stream: ProjectStream,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Read typed activity for one task/thread/run stream."""

        def operation() -> dict[str, Any]:
            binding = self._require_stream(stream)
            activity = self.registry.activities(self.project_id, stream=binding, limit=limit)
            return self._result("ok", "activity", count=len(activity), activity=activity)

        return self._guard("read_activity", operation)

    def _require_stream(self, stream: ProjectStream) -> ProjectStream:
        # Canonical project id plus deterministic correlation own stream routing;
        # the checkout field is retained on the stream as provenance only.
        if not isinstance(stream, ProjectStream) or stream.project_id != self.project_id:
            raise ProjectReject(
                "stream is not bound to this registered project",
                code="stream_project_mismatch",
            )
        expected = _stream_id(self.project_id, stream.correlation())
        if stream.stream_id != expected:
            raise ProjectReject(
                "stream id is not deterministic for its correlation",
                code="invalid_stream",
            )
        return stream

    def _office(self, stream: ProjectStream) -> Office:
        stream = self._require_stream(stream)
        return Office(
            agent_id=_scoped_inbox_address(
                self.project_id, stream.stream_id, self.identity
            ),
            home=self.registry.home,
            now=self._now,
            **self.office_kwargs,
        )

    def _presence_office(self) -> Office:
        return Office(
            agent_id=_scoped_presence_address(self.project_id, self.identity),
            home=self.registry.home,
            now=self._now,
            **self.office_kwargs,
        )

    def _prepare_cursor_directory(self, agent_id: str, source: str) -> None:
        """Create this reader's cursor directory before delegating to the Office API.

        Best effort on purpose.  Office still owns the cursor write and its own
        fail-open diagnostics, so a home that cannot create this directory must degrade
        inside Office - where the reason is recorded - and must not skip a read that
        Office could still have served without touching the cursor.
        """
        try:
            cursor_path(self.registry.home, agent_id, source).parent.mkdir(
                parents=True, exist_ok=True
            )
        except OSError:
            pass

    def inbox_address(
        self, stream: ProjectStream, identity: Mapping[str, str] | None = None
    ) -> str:
        """Return the deterministic underlying Office inbox address for diagnostics/tests."""
        binding = self._require_stream(stream)
        return _scoped_inbox_address(
            self.project_id,
            binding.stream_id,
            _identity_from_mapping(identity or self.identity),
        )

    def channel_name(self, stream: ProjectStream, channel: str) -> str:
        """Return the deterministic underlying Office channel name for a project stream."""
        binding = self._require_stream(stream)
        return _scoped_channel_name(
            self.project_id,
            binding.stream_id,
            canonicalize_correlation(channel, "channel"),
        )

    def wake_namespace(
        self, stream: ProjectStream, *, channel: str | None = None
    ) -> str:
        """Return the exact local pipe namespace used by this project stream, without opening it."""
        binding = self._require_stream(stream)
        if channel is None:
            return pipe_namespace(
                self.registry.home, "inbox", self.inbox_address(binding)
            )
        return pipe_namespace(
            self.registry.home, "channel", self.channel_name(binding, channel)
        )

    def _recipient_identity(
        self, recipient: Mapping[str, str] | ProjectTransport
    ) -> dict[str, str]:
        if isinstance(recipient, ProjectTransport):
            if recipient.project_id != self.project_id:
                raise ProjectReject(
                    "recipient belongs to another project",
                    code="cross_project_recipient",
                )
            return recipient.identity
        return _identity_from_mapping(recipient)

    def _payload(
        self,
        *,
        message_type: str,
        stream: ProjectStream,
        delivery_id: str | None,
        body: str | None,
        sender_address: str,
    ) -> str:
        payload: dict[str, Any] = {
            "schema": PROJECT_ENVELOPE_SCHEMA,
            "type": message_type,
            "project_id": self.project_id,
            "stream": stream.payload(),
            "sender_identity": self.identity,
            "sender_address": sender_address,
        }
        if delivery_id is not None:
            payload["delivery_id"] = delivery_id
        if body is not None:
            if not isinstance(body, str):
                raise ProjectReject("body must be a string", code="invalid_body")
            payload["body"] = body
        return json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

    def _preflight_body(self, body: Any) -> str:
        """Refuse a secret-shaped body before anything is written or delivered.

        The body reaches the Office inbox wrapped in JSON, and JSON escaping alone is
        enough to hide every high-confidence shape: `"` becomes `\\"`, a newline becomes
        `\\n`, so `Bearer` with its token on the next line and a quoted password stop
        looking like assignments. The scan therefore runs on the text the caller passed,
        not on the envelope, and uses the same detector as the plain send path
        (`coordfs.office.detect_secret`) instead of a second copy of the policy.
        """
        if not isinstance(body, str):
            raise ProjectReject("body must be a string", code="invalid_body")
        secret = detect_secret(body)
        if secret is not None:
            raise ProjectReject(
                f"body matches a high-confidence secret pattern ({secret}); refused before write",
                code="secret_detected",
            )
        return body

    def send(
        self,
        recipient: Mapping[str, str] | ProjectTransport,
        body: str,
        *,
        stream: ProjectStream,
        kind: str = "note",
        ack_ttl_s: int | None = None,
    ) -> dict[str, Any]:
        """Send one direct application message; transport delivery never means application ACK."""

        def operation() -> dict[str, Any]:
            binding = self._require_stream(stream)
            remote = self._recipient_identity(recipient)
            ttl = self.ack_ttl_s if ack_ttl_s is None else ack_ttl_s
            if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl < 1:
                raise ProjectReject(
                    "ack ttl must be a positive integer", code="invalid_ack_ttl"
                )
            self._preflight_body(body)
            delivery_id = uuid.uuid4().hex
            sender_address = self.inbox_address(binding)
            recipient_address = self.inbox_address(binding, remote)
            self.registry.prepare_delivery(
                self.project_id,
                delivery_id=delivery_id,
                stream=binding,
                recipient_identity=remote,
                recipient_address=recipient_address,
                ack_ttl_s=ttl,
            )
            claims = authority_claims(body)
            audit = (
                self.registry.record_audit(
                    self.project_id,
                    direction="outbound",
                    delivery_id=delivery_id,
                    reasons=claims,
                )
                if claims
                else None
            )
            transport = self._office(binding).send(
                recipient_address,
                self._payload(
                    message_type="message",
                    stream=binding,
                    delivery_id=delivery_id,
                    body=body,
                    sender_address=sender_address,
                ),
                kind=kind,
            )
            delivery = self.registry.finish_delivery(
                self.project_id, delivery_id, transport
            )
            status = transport.get("status", "error")
            code = (
                "sent"
                if status == "ok" and transport.get("code") == "sent"
                else transport.get("code", "delivery_failed")
            )
            return self._result(
                status,
                code,
                stream=binding.payload(),
                delivery_id=delivery_id,
                recipient_address=recipient_address,
                transport=transport,
                application_state=delivery["state"],
                retry="manual-only",
                audit=audit,
            )

        return self._guard("send", operation)

    def send_channel(
        self,
        channel: str,
        body: str,
        *,
        stream: ProjectStream,
        kind: str = "note",
        ack_ttl_s: int | None = None,
    ) -> dict[str, Any]:
        """Publish to a project-scoped channel; readers still ACK explicitly if required."""

        def operation() -> dict[str, Any]:
            binding = self._require_stream(stream)
            ttl = self.ack_ttl_s if ack_ttl_s is None else ack_ttl_s
            if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl < 1:
                raise ProjectReject(
                    "ack ttl must be a positive integer", code="invalid_ack_ttl"
                )
            self._preflight_body(body)
            local_channel = self.channel_name(binding, channel)
            delivery_id = uuid.uuid4().hex
            sender_address = self.inbox_address(binding)
            # Channel ACKs return to the sender's direct project stream.  A synthetic
            # recipient identity is not needed for routing, but the immutable ledger
            # still records the deterministic channel address.
            self.registry.prepare_delivery(
                self.project_id,
                delivery_id=delivery_id,
                stream=binding,
                recipient_identity=self.identity,
                recipient_address=local_channel,
                ack_ttl_s=ttl,
            )
            claims = authority_claims(body)
            audit = (
                self.registry.record_audit(
                    self.project_id,
                    direction="outbound",
                    delivery_id=delivery_id,
                    reasons=claims,
                )
                if claims
                else None
            )
            transport = self._office(binding).send(
                None,
                self._payload(
                    message_type="message",
                    stream=binding,
                    delivery_id=delivery_id,
                    body=body,
                    sender_address=sender_address,
                ),
                kind=kind,
                channel=local_channel,
            )
            delivery = self.registry.finish_delivery(
                self.project_id, delivery_id, transport, recipient_kind="channel"
            )
            status = transport.get("status", "error")
            code = (
                "sent"
                if status == "ok" and transport.get("code") == "sent"
                else transport.get("code", "delivery_failed")
            )
            return self._result(
                status,
                code,
                stream=binding.payload(),
                channel=canonicalize_correlation(channel, "channel"),
                channel_address=local_channel,
                delivery_id=delivery_id,
                transport=transport,
                application_state=delivery["state"],
                retry="manual-only",
                audit=audit,
            )

        return self._guard("send_channel", operation)

    def _decode_message(
        self, frame: Mapping[str, Any], stream: ProjectStream, expected_to: str
    ) -> dict[str, Any]:
        envelope = frame.get("untrusted_message")
        if not isinstance(envelope, Mapping):
            return {
                "frame": _copy_json(frame),
                "accepted": False,
                "reasons": ["invalid-office-frame"],
            }
        raw_body = envelope.get("body")
        try:
            payload = json.loads(raw_body) if isinstance(raw_body, str) else None
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            return {
                "frame": _copy_json(frame),
                "accepted": False,
                "reasons": ["invalid-project-envelope"],
            }
        reasons: list[str] = []
        if payload.get("schema") != PROJECT_ENVELOPE_SCHEMA:
            reasons.append("invalid-project-schema")
        if payload.get("project_id") != self.project_id:
            reasons.append("project-mismatch")
        if payload.get("stream") != stream.payload():
            reasons.append("stream-mismatch")
        if payload.get("sender_address") != envelope.get("from"):
            reasons.append("sender-route-mismatch")
        if envelope.get("to") != expected_to:
            reasons.append("recipient-route-mismatch")
        try:
            claimed_identity = _identity_from_mapping(payload.get("sender_identity"))
        except ProjectReject:
            claimed_identity = None
            reasons.append("invalid-sender-identity")
        if claimed_identity is not None and payload.get(
            "sender_address"
        ) != _scoped_inbox_address(self.project_id, stream.stream_id, claimed_identity):
            reasons.append("sender-identity-route-mismatch")
        message_type = payload.get("type")
        if message_type not in {"message", "ack"}:
            reasons.append("invalid-message-type")
        delivery_id = payload.get("delivery_id")
        if not isinstance(delivery_id, str) or not MESSAGE_ID_RE.match(delivery_id):
            reasons.append("invalid-delivery-id")
        if message_type == "message" and not isinstance(payload.get("body"), str):
            reasons.append("invalid-body")
        if reasons:
            return {"frame": _copy_json(frame), "accepted": False, "reasons": reasons}
        return {
            "frame": _copy_json(frame),
            "accepted": True,
            "payload": payload,
            "envelope": dict(envelope),
        }

    def _handle_read(
        self, *, stream: ProjectStream, transport: Mapping[str, Any], expected_to: str
    ) -> dict[str, Any]:
        if transport.get("status") != "ok":
            return self._result(
                str(transport.get("status", "error")),
                str(transport.get("code", "read_failed")),
                stream=stream.payload(),
                transport=_copy_json(transport),
            )
        messages: list[dict[str, Any]] = []
        for frame in transport.get("messages", []):
            decoded = self._decode_message(frame, stream, expected_to)
            if not decoded["accepted"]:
                audit = self.registry.record_audit(
                    self.project_id,
                    direction="invalid-envelope",
                    delivery_id=None,
                    reasons=decoded["reasons"],
                )
                wrapped = _copy_json(decoded["frame"])
                wrapped["project_transport"] = {
                    "status": "rejected",
                    "reasons": decoded["reasons"],
                }
                wrapped["audit"] = audit
                messages.append(wrapped)
                continue
            payload = decoded["payload"]
            envelope = decoded["envelope"]
            delivery_id = payload["delivery_id"]
            app_type = payload["type"]
            audit: dict[str, Any] | None = None
            application: dict[str, Any]
            if app_type == "message":
                claims = authority_claims(payload["body"])
                audit_rejected = bool(claims)
                received = self.registry.record_inbound(
                    self.project_id,
                    delivery_id=delivery_id,
                    stream=stream,
                    sender_address=str(envelope["from"]),
                    recipient_identity=self.identity,
                    audit_rejected=audit_rejected,
                )
                if claims:
                    audit = self.registry.record_audit(
                        self.project_id,
                        direction="inbound",
                        delivery_id=delivery_id,
                        reasons=claims,
                    )
                application = {
                    "type": "message",
                    "delivery_id": delivery_id,
                    "duplicate_delivery": received["duplicate"],
                    "ack_state": received["received"]["ack_state"],
                }
            else:
                observed = self.registry.record_ack(
                    self.project_id, delivery_id, str(envelope["from"])
                )
                application = {
                    "type": "ack",
                    "delivery_id": delivery_id,
                    "ack_result": observed["code"],
                }
            # The only exposed application body remains inside the same untrusted
            # Office envelope; no field here is an instruction or authority grant.
            visible = dict(envelope)
            if app_type == "message":
                visible["body"] = payload["body"]
            visible["project"] = {
                "project_id": self.project_id,
                "stream": stream.payload(),
            }
            visible["application"] = application
            visible["trust"] = "untrusted"
            result: dict[str, Any] = {
                "untrusted_message": visible,
                "handling": HANDLING_TEXT,
            }
            if audit is not None:
                result["audit"] = audit
            messages.append(result)
        payload_result = self._result(
            "ok",
            "read",
            stream=stream.payload(),
            count=len(messages),
            remaining=transport.get("remaining"),
            skipped_malformed=transport.get("skipped_malformed"),
            gap_detected=transport.get("gap_detected"),
            handling=HANDLING_TEXT,
            messages=messages,
        )
        if "channel" in transport:
            payload_result["channel"] = transport["channel"]
        if "gap_message" in transport:
            payload_result["gap_message"] = transport["gap_message"]
        return payload_result

    def _read_response_reserves(self, stream: ProjectStream) -> tuple[int, int]:
        """Conservatively reserve bytes added by the project wrapper before cursor commit."""
        empty_result = self._result(
            "ok", "read", stream=stream.payload(), count=0, remaining=0,
            skipped_malformed=0, gap_detected=False, handling=HANDLING_TEXT, messages=[],
        )
        fixed_bytes = len(json.dumps(
            empty_result, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")) + 1
        return (
            max(PROJECT_READ_RESPONSE_FIXED_RESERVE_BYTES, fixed_bytes + 4096),
            PROJECT_READ_RESPONSE_PER_MESSAGE_RESERVE_BYTES,
        )

    def read(
        self, *, stream: ProjectStream, limit: int | None = None, peek: bool = False,
        max_response_bytes: int | None = None,
    ) -> dict[str, Any]:
        """Read one direct project stream as untrusted frames and observe any application ACKs."""

        def operation() -> dict[str, Any]:
            binding = self._require_stream(stream)
            reserve_kwargs: dict[str, int] = {}
            if max_response_bytes is not None:
                fixed_reserve, per_message_reserve = self._read_response_reserves(binding)
                reserve_kwargs = {
                    "response_reserve_bytes": fixed_reserve,
                    "response_per_message_reserve_bytes": per_message_reserve,
                }
            return self._handle_read(
                stream=binding,
                transport=self._office(binding).read(
                    limit=limit, peek=peek, max_response_bytes=max_response_bytes,
                    **reserve_kwargs,
                ),
                expected_to=self.inbox_address(binding),
            )

        return self._guard("read", operation)

    def read_channel(
        self,
        channel: str,
        *,
        stream: ProjectStream,
        limit: int | None = None,
        peek: bool = False,
        max_response_bytes: int | None = None,
    ) -> dict[str, Any]:
        """Read one project-scoped channel with an independent underlying Office cursor."""

        def operation() -> dict[str, Any]:
            binding = self._require_stream(stream)
            local_channel = self.channel_name(binding, channel)
            office = self._office(binding)
            self._prepare_cursor_directory(
                office.agent_id, channel_source(local_channel)
            )
            reserve_kwargs: dict[str, int] = {}
            if max_response_bytes is not None:
                fixed_reserve, per_message_reserve = self._read_response_reserves(binding)
                reserve_kwargs = {
                    "response_reserve_bytes": fixed_reserve,
                    "response_per_message_reserve_bytes": per_message_reserve,
                }
            return self._handle_read(
                stream=binding,
                transport=office.read(
                    limit=limit, peek=peek, channel=local_channel,
                    max_response_bytes=max_response_bytes,
                    **reserve_kwargs,
                ),
                expected_to="#" + local_channel,
            )

        return self._guard("read_channel", operation)

    def wait(self, *, stream: ProjectStream, timeout_s: float = 30.0) -> dict[str, Any]:
        """Wait only on this deterministic project inbox; a wake is still merely a durable-log hint."""

        def operation() -> dict[str, Any]:
            binding = self._require_stream(stream)
            transport = self._office(binding).wait(timeout_s=timeout_s)
            return self._result(
                str(transport.get("status", "error")),
                str(transport.get("code", "wait_failed")),
                stream=binding.payload(),
                transport=transport,
            )

        return self._guard("wait", operation)

    def wait_channel(
        self, channel: str, *, stream: ProjectStream, timeout_s: float = 30.0
    ) -> dict[str, Any]:
        """Wait only on the channel namespace derived from this project stream."""

        def operation() -> dict[str, Any]:
            binding = self._require_stream(stream)
            local_channel = self.channel_name(binding, channel)
            transport = self._office(binding).wait(
                timeout_s=timeout_s, channel=local_channel
            )
            return self._result(
                str(transport.get("status", "error")),
                str(transport.get("code", "wait_failed")),
                stream=binding.payload(),
                channel=canonicalize_correlation(channel, "channel"),
                transport=transport,
            )

        return self._guard("wait_channel", operation)

    def acknowledge(self, delivery_id: str) -> dict[str, Any]:
        """Send at most one application ACK for a read delivery; failed ACKs stay manual-only."""

        def operation() -> dict[str, Any]:
            if not isinstance(delivery_id, str) or not MESSAGE_ID_RE.match(delivery_id):
                raise ProjectReject("delivery id is invalid", code="invalid_delivery")
            reservation = self.registry.reserve_ack(
                self.project_id, delivery_id, self.identity
            )
            if reservation["code"] != "ack_reserved":
                return self._result(
                    "ok",
                    reservation["code"],
                    delivery_id=delivery_id,
                    retry="manual-only",
                )
            received = reservation["received"]
            data = received["stream"]
            stream = self.stream(
                task_id=data["task_id"],
                thread_id=data["thread_id"],
                run_id=data["run_id"],
            )
            sender_address = received["sender_address"]
            transport = self._office(stream).send(
                sender_address,
                self._payload(
                    message_type="ack",
                    stream=stream,
                    delivery_id=delivery_id,
                    body=None,
                    sender_address=self.inbox_address(stream),
                ),
                kind="note",
                reply_to=delivery_id,
            )
            ack = self.registry.finish_ack(
                self.project_id,
                delivery_id,
                transport,
                recipient_identity=self.identity,
            )
            status = transport.get("status", "error")
            code = (
                "acknowledged"
                if status == "ok" and transport.get("code") == "sent"
                else "ack_failed"
            )
            return self._result(
                status,
                code,
                delivery_id=delivery_id,
                ack_id=ack.get("ack_id"),
                ack_state=ack["ack_state"],
                transport=transport,
                retry="manual-only",
            )

        return self._guard("acknowledge", operation)

    def delivery_state(self, delivery_id: str) -> dict[str, Any]:
        """Observe sender-side application state; the caller chooses whether to make a new delivery."""

        def operation() -> dict[str, Any]:
            if not isinstance(delivery_id, str) or not MESSAGE_ID_RE.match(delivery_id):
                raise ProjectReject("delivery id is invalid", code="invalid_delivery")
            delivery = self.registry.delivery_state(self.project_id, delivery_id)
            return self._result(
                "ok",
                "delivery_state",
                delivery_id=delivery_id,
                application_state=delivery["state"],
                attempts=delivery["attempts"],
                retry="manual-only",
                delivery=delivery,
            )

        return self._guard("delivery_state", operation)

    def cancel_delivery(self, delivery_id: str) -> dict[str, Any]:
        """Cancel application follow-up without withdrawing the already durable Office message."""

        def operation() -> dict[str, Any]:
            if not isinstance(delivery_id, str) or not MESSAGE_ID_RE.match(delivery_id):
                raise ProjectReject("delivery id is invalid", code="invalid_delivery")
            delivery = self.registry.cancel_delivery(self.project_id, delivery_id)
            return self._result(
                "ok",
                "cancelled"
                if delivery.get("state") == "CANCELLED"
                else "not_cancelled",
                delivery_id=delivery_id,
                application_state=delivery["state"],
                retry="manual-only",
            )

        return self._guard("cancel_delivery", operation)

    def presence(self, *, ttl_s: int = 60, status: str | None = None) -> dict[str, Any]:
        """Announce one project-scoped presence lease while retaining original identity strength."""

        def operation() -> dict[str, Any]:
            transport = self._presence_office().presence(ttl_s=ttl_s, status=status)
            if transport.get("status") == "ok":
                record = self.registry.record_presence(
                    self.project_id, self.identity, ttl_s
                )
            else:
                record = None
            return self._result(
                str(transport.get("status", "error")),
                str(transport.get("code", "presence_failed")),
                transport=transport,
                identity_strength=self.identity["strength"],
                presence=record,
            )

        return self._guard("presence", operation)

    def presence_state(
        self, identity: Mapping[str, str] | ProjectTransport
    ) -> dict[str, Any]:
        """Return explicit ``FRESH``, ``EXPIRED`` or ``UNKNOWN`` presence for a project runtime."""

        def operation() -> dict[str, Any]:
            remote = self._recipient_identity(identity)
            known = self.registry.resolve(self.project_id)["runtimes"].get(
                remote["agent_id"]
            )
            if not isinstance(known, dict):
                return self._result(
                    "ok", "presence", runtime_identity=remote, presence_state="UNKNOWN"
                )
            address = _scoped_presence_address(self.project_id, remote)
            board = CoordBoard(home=self.registry.home, now=self._now)
            live = board.who("agent:" + address, kind="agent")
            if live.get("holder_agent") == address:
                return self._result(
                    "ok", "presence", runtime_identity=remote, presence_state="FRESH"
                )
            presence = known.get("presence")
            if isinstance(presence, dict) and float(
                presence.get("expires_epoch", 0)
            ) <= float(self._now()):
                return self._result(
                    "ok", "presence", runtime_identity=remote, presence_state="EXPIRED"
                )
            return self._result(
                "ok", "presence", runtime_identity=remote, presence_state="UNKNOWN"
            )

        return self._guard("presence_state", operation)

    def audit_rejections(self) -> dict[str, Any]:
        """List metadata-only authority-claim rejections; raw untrusted bodies are intentionally absent."""
        return self._guard(
            "audit_rejections",
            lambda: self._result(
                "ok", "audit_rejections", audits=self.registry.audits(self.project_id)
            ),
        )


__all__ = [
    "AUTHORITY_PATTERNS",
    "ACTIVITY_KINDS",
    "ACTIVITY_PROVIDERS",
    "ACTIVITY_STATUSES",
    "CONNECTED_HARNESSES",
    "PROJECT_ENVELOPE_SCHEMA",
    "PROJECT_ACTIVITY_SCHEMA",
    "ProjectRegistry",
    "ProjectReject",
    "ProjectStream",
    "ProjectTransport",
    "authority_claims",
    "canonicalize_checkout",
    "canonicalize_correlation",
    "canonicalize_project_id",
]
