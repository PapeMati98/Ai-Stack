"""Native Antigravity sidecar transport seam.

Submits exactly one native command to an explicitly configured ``agentapi``
executable (``agentapi send-message <conversation_id> <prompt>``) for an
already-existing Desktop conversation. This module:

* never touches the network, a Desktop store, global config, or a shell;
* never discovers executables (no PATH lookup) and never accepts an
  executable from an Office message — the caller supplies one absolute path;
* invokes the injected ``runner`` at most once per attempt (no retry);
* treats a zero exit as *submission only* (SUBMITTED_UNCONFIRMED), never as
  delivery or model execution.

Policy observations (STOP, target busy/unknown, binding revocation) are
supplied by the trusted local observer through callables so this module
performs no I/O of its own. STOP or an unknown state fails closed.
Busy defers without error and never interrupts a turn.

Payloads must be short minimized retrieval/correlation instructions. The
returned receipt carries only a prompt digest, byte count, and a bounded
reason — never the prompt, conversation id, argv, or provider output.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePath, PureWindowsPath
from typing import Any, Callable, Container

from .errors import CoordError

MAX_PROMPT_CHARS = 2000
MIN_PROMPT_CHARS = 8
MAX_TTL_S = 6 * 60 * 60
MIN_TIMEOUT_S = 1
MAX_TIMEOUT_S = 300
MAX_SENDER_CHARS = 256
MAX_CHANNEL_CHARS = 128
MAX_DEDUPE_KEY_CHARS = 128
CLOCK_SKEW_TOLERANCE_S = 60

_DEDUPE_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}\Z")
_FILLER_TOKENS = frozenset({"ping", "test", "hello", "hi", "hey", "...", ".", "-", "?"})

STATUS_SUBMITTED_UNCONFIRMED = "SUBMITTED_UNCONFIRMED"
STATUS_UNKNOWN = "UNKNOWN"
STATUS_REFUSED = "REFUSED"
STATUS_DEFERRED = "DEFERRED"


@dataclass(frozen=True)
class Binding:
    """Trusted host binding for one existing conversation.

    ``conversation_id`` is the exact canonical UUID of the Desktop
    conversation. ``host`` must equal the local trusted host. The request
    must name a ``sender`` in ``allowed_senders`` and a ``channel`` in
    ``allowed_channels``.
    """

    conversation_id: str
    host: str
    allowed_senders: tuple[str, ...]
    allowed_channels: tuple[str, ...]


@dataclass(frozen=True)
class TransportRequest:
    conversation_id: str
    sender: str
    channel: str
    prompt: str
    dedupe_key: str
    issued_at: str
    expires_at: str
    timeout_s: int


@dataclass(frozen=True)
class Receipt:
    """Bounded, log-safe outcome. Never carries prompt text, argv, the
    conversation id, or provider output."""

    status: str
    reason: str
    prompt_digest: str
    prompt_bytes: int
    calls: int
    at: str


def _clean_text(value: Any, label: str, max_chars: int) -> str:
    if not isinstance(value, str):
        raise CoordError(f"{label} must be a string", code=f"invalid_{label}")
    if value != value.strip() or not value or len(value) > max_chars or "\x00" in value:
        raise CoordError(
            f"{label} must be non-empty, trimmed, and at most {max_chars} characters",
            code=f"invalid_{label}",
        )
    if any(ord(char) < 32 for char in value):
        raise CoordError(f"{label} cannot contain control characters", code=f"invalid_{label}")
    return value


def _canonical_conversation_id(value: Any) -> str:
    if not isinstance(value, str):
        raise CoordError("conversation_id must be a string", code="invalid_conversation_id")
    text = value.strip()
    try:
        parsed = uuid.UUID(text)
    except (ValueError, AttributeError, TypeError) as exc:
        raise CoordError(
            "conversation_id must be a canonical UUID", code="invalid_conversation_id"
        ) from exc
    canonical = str(parsed)
    if text != canonical:
        raise CoordError(
            "conversation_id must be the exact canonical lowercase UUID",
            code="invalid_conversation_id",
        )
    return canonical


def _parse_ts(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise CoordError(f"{label} must be a string", code=f"invalid_{label}")
    text = value.strip()
    if not text or len(text) > 64 or value != text:
        raise CoordError(f"{label} must be a trimmed ISO-8601 timestamp", code=f"invalid_{label}")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CoordError(f"{label} must be an ISO-8601 timestamp", code=f"invalid_{label}") from exc
    if parsed.tzinfo is None:
        raise CoordError(f"{label} must carry a timezone", code=f"invalid_{label}")
    return parsed.astimezone(timezone.utc)


def _utc_now_iso(now: float) -> str:
    return datetime.fromtimestamp(now, tz=timezone.utc).isoformat()


def validate_binding(raw: Any) -> Binding:
    if not isinstance(raw, dict):
        raise CoordError("binding must be a mapping", code="invalid_binding")
    conversation_id = _canonical_conversation_id(raw.get("conversation_id"))
    host = _clean_text(raw.get("host"), "host", 256)
    senders = raw.get("allowed_senders")
    channels = raw.get("allowed_channels")
    if not isinstance(senders, (list, tuple)) or not senders:
        raise CoordError("allowed_senders must be a non-empty list", code="invalid_binding")
    if not isinstance(channels, (list, tuple)) or not channels:
        raise CoordError("allowed_channels must be a non-empty list", code="invalid_binding")
    clean_senders = tuple(_clean_text(item, "sender", MAX_SENDER_CHARS) for item in senders)
    clean_channels = tuple(_clean_text(item, "channel", MAX_CHANNEL_CHARS) for item in channels)
    if len(set(clean_senders)) != len(clean_senders) or len(set(clean_channels)) != len(
        clean_channels
    ):
        raise CoordError("binding lists must not contain duplicates", code="invalid_binding")
    return Binding(
        conversation_id=conversation_id,
        host=host,
        allowed_senders=clean_senders,
        allowed_channels=clean_channels,
    )


def validate_request(raw: Any) -> TransportRequest:
    if not isinstance(raw, dict):
        raise CoordError("request must be a mapping", code="invalid_request")
    conversation_id = _canonical_conversation_id(raw.get("conversation_id"))
    sender = _clean_text(raw.get("sender"), "sender", MAX_SENDER_CHARS)
    channel = _clean_text(raw.get("channel"), "channel", MAX_CHANNEL_CHARS)
    prompt = raw.get("prompt")
    if not isinstance(prompt, str) or "\x00" in prompt:
        raise CoordError("prompt must be a string", code="invalid_prompt")
    if prompt != prompt.strip() or not prompt or len(prompt) > MAX_PROMPT_CHARS:
        raise CoordError(
            f"prompt must be non-empty, trimmed, and at most {MAX_PROMPT_CHARS} characters",
            code="invalid_prompt",
        )
    if any(ord(char) < 32 and char not in "\t\r\n" for char in prompt):
        raise CoordError("prompt cannot contain control characters", code="invalid_prompt")
    dedupe_key = raw.get("dedupe_key")
    if not isinstance(dedupe_key, str) or _DEDUPE_KEY_RE.fullmatch(dedupe_key) is None:
        raise CoordError("dedupe_key must match [A-Za-z0-9._:-]{1,128}", code="invalid_dedupe_key")
    issued = _parse_ts(raw.get("issued_at"), "issued_at")
    expires = _parse_ts(raw.get("expires_at"), "expires_at")
    if expires <= issued:
        raise CoordError("expires_at must be after issued_at", code="invalid_ttl")
    if (expires - issued).total_seconds() > MAX_TTL_S:
        raise CoordError(f"ttl must not exceed {MAX_TTL_S} seconds", code="invalid_ttl")
    timeout = raw.get("timeout_s")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int)
        or not (MIN_TIMEOUT_S <= timeout <= MAX_TIMEOUT_S)
    ):
        raise CoordError(
            f"timeout_s must be an integer in {MIN_TIMEOUT_S}..{MAX_TIMEOUT_S}",
            code="invalid_timeout",
        )
    return TransportRequest(
        conversation_id=conversation_id,
        sender=sender,
        channel=channel,
        prompt=prompt,
        dedupe_key=dedupe_key,
        issued_at=issued.isoformat(),
        expires_at=expires.isoformat(),
        timeout_s=timeout,
    )


def validate_executable(raw: Any) -> str:
    """Accept only an explicit absolute path spelling. No PATH lookup, no
    shell parsing, no existence check (offline tests inject fake paths)."""
    if not isinstance(raw, str):
        raise CoordError("executable must be a string", code="invalid_executable")
    if raw != raw.strip() or not raw or "\x00" in raw:
        raise CoordError("executable must be a non-empty trimmed path", code="invalid_executable")
    if any(ord(char) < 32 for char in raw):
        raise CoordError("executable cannot contain control characters", code="invalid_executable")
    path = PurePath(raw)
    if not path.is_absolute():
        raise CoordError("executable must be an absolute path", code="invalid_executable")
    return raw


def agentapi_command(argv: list[str]) -> list[str]:
    """Translate the configured vendor wrapper without running a shell.

    Only the literal two-line agentapi.bat form is supported. Its contents
    are trusted installation metadata, never an Office message or PATH input.
    Anything more complex remains unsupported rather than executing batch.
    """
    if not argv:
        raise CoordError("missing configured executable", code="invalid_executable")
    executable = Path(validate_executable(argv[0]))
    suffix = executable.suffix.casefold()
    if suffix not in {".bat", ".cmd", ".ps1"}:
        return list(argv)
    if executable.name.casefold() != "agentapi.bat":
        raise CoordError("unsupported executable wrapper", code="invalid_executable")
    try:
        with executable.open("rb") as handle:
            content = handle.read(4097)
        if len(content) > 4096:
            raise ValueError("oversized wrapper")
        lines = content.decode("utf-8-sig").splitlines()
    except (OSError, UnicodeError, ValueError) as exc:
        raise CoordError("configured wrapper unavailable", code="invalid_executable") from exc
    if len(lines) != 2 or lines[0].strip().casefold() != "@echo off":
        raise CoordError("unsupported executable wrapper", code="invalid_executable")
    matched = re.fullmatch(r'"([^"\r\n]+)" agentapi %\*', lines[1].strip())
    target = matched.group(1) if matched else ""
    target_path = PureWindowsPath(target)
    if (not target_path.is_absolute() or target_path.name.casefold() != "language_server.exe"
            or any(ord(char) < 32 for char in target)):
        raise CoordError("unsupported executable wrapper", code="invalid_executable")
    return [target, "agentapi", *argv[1:]]


def _is_filler(prompt: str) -> bool:
    stripped = prompt.strip()
    if len(stripped) < MIN_PROMPT_CHARS:
        return True
    if stripped.casefold() in _FILLER_TOKENS:
        return True
    if not any(char.isalnum() for char in stripped):
        return True
    if len(set(stripped)) == 1:
        return True
    return False


def _digest(prompt: str) -> tuple[str, int]:
    encoded = prompt.encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), len(encoded)


def _refuse(reason: str, prompt: str, now: float, calls: int = 0) -> Receipt:
    digest, size = _digest(prompt)
    return Receipt(
        status=STATUS_REFUSED,
        reason=reason,
        prompt_digest=digest,
        prompt_bytes=size,
        calls=calls,
        at=_utc_now_iso(now),
    )


def _defer(reason: str, prompt: str, now: float) -> Receipt:
    digest, size = _digest(prompt)
    return Receipt(
        status=STATUS_DEFERRED,
        reason=reason,
        prompt_digest=digest,
        prompt_bytes=size,
        calls=0,
        at=_utc_now_iso(now),
    )


def _unknown(reason: str, prompt: str, now: float, calls: int) -> Receipt:
    digest, size = _digest(prompt)
    return Receipt(
        status=STATUS_UNKNOWN,
        reason=reason,
        prompt_digest=digest,
        prompt_bytes=size,
        calls=calls,
        at=_utc_now_iso(now),
    )


def attempt(
    raw_request: dict[str, Any],
    raw_binding: dict[str, Any],
    *,
    executable: str,
    local_host: str,
    observe_stop: Callable[[], bool | None],
    observe_target: Callable[[], str],
    observe_revoked: Callable[[], bool | None],
    known_dedupe_keys: Container[str],
    runner: Callable[[list[str], int], Any],
    now: float,
) -> Receipt:
    """Attempt exactly one native submission. Raises CoordError on malformed
    binding/request/executable/host; otherwise returns a Receipt and invokes
    ``runner`` at most once with a fixed argv list (no shell, no retry)."""
    binding = validate_binding(raw_binding)
    request = validate_request(raw_request)
    argv0 = validate_executable(executable)
    host = _clean_text(local_host, "host", 256)
    if host != binding.host:
        raise CoordError("local host does not match the trusted binding", code="host_mismatch")

    prompt = request.prompt
    if request.conversation_id != binding.conversation_id:
        return _refuse("wrong_conversation_id", prompt, now)
    if request.sender not in binding.allowed_senders:
        return _refuse("wrong_sender", prompt, now)
    if request.channel not in binding.allowed_channels:
        return _refuse("wrong_channel", prompt, now)
    expires = datetime.fromisoformat(request.expires_at)
    issued = datetime.fromisoformat(request.issued_at)
    now_dt = datetime.fromtimestamp(now, tz=timezone.utc)
    if now_dt > expires:
        return _refuse("expired", prompt, now)
    if now_dt < issued:
        issued_epoch = issued.timestamp()
        if issued_epoch - now > CLOCK_SKEW_TOLERANCE_S:
            raise CoordError("issued_at is in the future", code="invalid_issued_at")
    if _is_filler(prompt):
        return _refuse("filler_prompt", prompt, now)
    if request.dedupe_key in known_dedupe_keys:
        return _refuse("duplicate", prompt, now)

    def policy_gate() -> Receipt | None:
        try:
            stop = observe_stop()
        except Exception:
            return _refuse("stop_unknown", prompt, now)
        if stop is None:
            return _refuse("stop_unknown", prompt, now)
        if stop:
            return _refuse("stop_active", prompt, now)
        try:
            revoked = observe_revoked()
        except Exception:
            return _refuse("revocation_unknown", prompt, now)
        if revoked is None:
            return _refuse("revocation_unknown", prompt, now)
        if revoked:
            return _refuse("binding_revoked", prompt, now)
        try:
            state = observe_target()
        except Exception:
            return _refuse("target_unknown", prompt, now)
        if state == "busy":
            return _defer("target_busy", prompt, now)
        if state != "idle":
            return _refuse("target_unknown", prompt, now)
        return None

    blocked = policy_gate()
    if blocked is not None:
        return blocked

    # Re-check STOP/binding/busy immediately before submitting; STOP or an
    # unknown state fails closed, busy defers without error.
    seam = policy_gate()
    if seam is not None:
        return seam

    argv = [argv0, "send-message", request.conversation_id, prompt]
    try:
        result = runner(argv, request.timeout_s)
    except Exception as exc:
        # subprocess.TimeoutExpired derives from SubprocessError, not
        # TimeoutError, so classify by name to stay stdlib-generic here.
        if type(exc).__name__ == "TimeoutExpired" or isinstance(exc, TimeoutError):
            return _unknown("timeout_after_submit", prompt, now, 1)
        return _unknown("runner_error", prompt, now, 1)
    try:
        code = result.returncode
    except (AttributeError, TypeError, ValueError):
        return _unknown("malformed_output", prompt, now, 1)
    if not isinstance(code, int) or isinstance(code, bool):
        return _unknown("malformed_output", prompt, now, 1)
    digest, size = _digest(prompt)
    at = _utc_now_iso(now)
    if code == 0:
        # Exit zero proves submission only — never delivery or model execution.
        return Receipt(
            status=STATUS_SUBMITTED_UNCONFIRMED,
            reason="submitted_unconfirmed",
            prompt_digest=digest,
            prompt_bytes=size,
            calls=1,
            at=at,
        )
    return Receipt(
        status=STATUS_UNKNOWN,
        reason="nonzero_exit",
        prompt_digest=digest,
        prompt_bytes=size,
        calls=1,
        at=at,
    )
