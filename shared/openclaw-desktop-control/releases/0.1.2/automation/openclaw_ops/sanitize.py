"""Deterministic redaction and bounded admission of helpdesk findings.

Redaction runs BEFORE any inference or synthesis. Secrets, credentials,
hidden reasoning, tool transcripts and restricted records are excluded;
only terminal public/internal summaries are admitted. Each admitted excerpt
is bounded to 1600 UTF-8 bytes and each synthesis to 6000 characters.
A claim that does not fit is rejected, never silently truncated.
"""

from __future__ import annotations

import re

EXCERPT_MAX_BYTES = 1600
SYNTHESIS_MAX_CHARS = 6000

_ALLOWED_KINDS = ("public", "internal")


class TruncationRejected(ValueError):
    """A claim would cross a byte/char cap: reject, do not truncate."""


_PATTERNS = [
    # private key blocks
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    # bearer / token / password / secret assignments (value only)
    re.compile(r"(?i)\b(bearer|token|api[_-]?key|secret|password|passwd|pwd)\b\s*[:=]\s*\S+"),
    # standalone sk-/ghp-/xox- style tokens
    re.compile(r"\b(sk-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9]{8,}|xox[bap]-[A-Za-z0-9-]{8,})\b"),
    # cookie / session assignments
    re.compile(r"(?i)\b(cookie|session(id)?|auth)\b\s*[:=]\s*\S+"),
]

REDACTED = "[REDACTED]"


def redact(text: str) -> str:
    """Deterministic redaction of secret-shaped values."""
    out = text
    for pat in _PATTERNS:
        out = pat.sub(REDACTED, out)
    return out


def bound_excerpt(text: str, limit_bytes: int = EXCERPT_MAX_BYTES) -> str:
    """Return text unchanged when it fits; raise instead of truncating."""
    if len(text.encode("utf-8")) > limit_bytes:
        raise TruncationRejected(
            f"excerpt is {len(text.encode('utf-8'))} bytes, cap is"
            f" {limit_bytes}: rejected, not truncated"
        )
    return text


def bound_synthesis(text: str, limit_chars: int = SYNTHESIS_MAX_CHARS) -> str:
    if len(text) > limit_chars:
        raise TruncationRejected(
            f"synthesis is {len(text)} chars, cap is {limit_chars}"
        )
    return text


def admit_summary(text: str, kind: str) -> str:
    """Admit one terminal summary: redact, bound, allow kind only."""
    if kind not in _ALLOWED_KINDS:
        raise ValueError(f"summary kind {kind!r} not admitted (terminal only)")
    return bound_excerpt(redact(text))
