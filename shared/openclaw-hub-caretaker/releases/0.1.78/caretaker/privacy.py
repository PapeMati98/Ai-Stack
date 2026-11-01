"""Fail-closed outbound gate for locally synthesized folder knowledge.

The gate does not claim to recognize every kind of sensitive information.
It blocks common structured personal/customer identifiers before a folder
handoff enters the Hub and before a subscription reviewer sees a packet.
"""

from __future__ import annotations

import re

from .filter import contains_secret


_PATTERNS = (
    re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])"),
    re.compile(r"(?i)\bPESEL\s*[:=]?\s*\d{11}\b"),
    re.compile(r"(?i)\bPL\s?(?:\d[\s-]?){26}\b"),
    re.compile(r"(?i)\b(?:PESEL|NIP|REGON|IBAN|nr\s*konta|telefon|phone|e-?mail|adres\s+zamieszkania)\s*[:=]\s*\S+"),
    re.compile(r"(?i)\b(?:ul\.|ulica|al\.|aleja)\s+[^\n,;]{3,80}\s+\d+[a-z]?\b"),
    re.compile(r"\b\d{2}-\d{3}\b"),  # Polish postal code
)

_UNLABELLED_PESEL = re.compile(r"(?<![A-Za-z0-9])\d{11}(?![A-Za-z0-9])")


def _valid_pesel(value: str) -> bool:
    digits = [int(char) for char in value]
    weights = (1, 3, 7, 9, 1, 3, 7, 9, 1, 3)
    return (10 - sum(a * b for a, b in zip(digits[:10], weights)) % 10) % 10 == digits[10]


def contains_restricted_identifier(text: str) -> bool:
    return isinstance(text, str) and (
        any(pattern.search(text) for pattern in _PATTERNS)
        or any(_valid_pesel(match.group()) for match in _UNLABELLED_PESEL.finditer(text)))


def _safe(value) -> bool:
    if isinstance(value, str):
        return not contains_restricted_identifier(value) and not contains_secret(value)
    if isinstance(value, dict):
        return all(_safe(key) and _safe(item) for key, item in value.items())
    if isinstance(value, list):
        return all(_safe(item) for item in value)
    return value is None or isinstance(value, (bool, int, float))


def outbound_text_safe(prompt: str) -> bool:
    """Recheck the exact prompt bytes before every external reviewer invoke."""
    return isinstance(prompt, str) and _safe(prompt)


def outbound_packet_safe(frozen: dict, payloads: dict[str, str],
                         manifest: dict | None = None, ledger: dict | None = None) -> bool:
    """Check all structured fields and page payloads sent to the reviewer."""
    return (_safe(frozen) and _safe(payloads)
            and (manifest is None or _safe(manifest))
            and (ledger is None or _safe(ledger)))
