"""Canonical content policy for the trusted scanner.

The SENSITIVE_PATTERNS definition below is moved verbatim (literal regex,
flags and behavior) from the pinned Fleet source
SHA256 1d411bb4fb6407acd9cd561e4a9e0086bdec5809e63feb4aca864a89821a27e0
into this small module, so the trusted scanner never imports the full Fleet
and never accepts task-supplied patterns. The scanner implementation is the
admitted sibling source_prefilter.py, loaded only from this file's fixed
absolute sibling path, independent of cwd, the task workspace, PYTHONPATH
and injected sys.modules.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import re
from typing import Any

SENSITIVE_PATTERNS = (
    (
        # Match the full PEM block (RSA/EC/DSA/OPENSSH or no prefix), from the
        # BEGIN marker through the END marker inclusive. When the END marker is
        # missing (truncated log), match to the end of the text so the body is
        # never left exposed.
        "private-key",
        re.compile(
            r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
            r".*?"
            r"(?:-----END (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----|\Z)",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "aws-access-key-id",
        re.compile(
            r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|ABIA|ACCA)"
            r"[A-Z0-9]{16}\b"
        ),
    ),
    (
        "aws-secret-access-key",
        re.compile(
            r"\b[\w\-]*aws[\w\-]*secret[\w\-]*key\s*[:=]\s*"
            # The secret is exactly 40 base64-like characters and may end with
            # ``+``, ``/`` or ``=`` (real AWS secret access keys use that
            # alphabet). A trailing ``\b`` would silently fail to match when the
            # last character is non-word and the surrounding delimiter (closing
            # quote, newline, comma, end of string) is also non-word, so we
            # anchor with an explicit lookahead instead.
            r"[\"']?[A-Za-z0-9/+=]{40}(?=[^\w]|\Z)",
            re.IGNORECASE,
        ),
    ),
    (
        "bearer-token",
        re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE),
    ),
    (
        "vendor-token",
        re.compile(
            r"\b(?:sk-(?:ant-|proj-|or-)?|xai-|ghp_|github_pat_)[A-Za-z0-9_-]{16,}",
            re.IGNORECASE,
        ),
    ),
    (
        "assigned-secret",
        re.compile(
            r"\b(?:api[_ -]?key|access[_ -]?token|secret[_ -]?key)\s*[:=]\s*[\"']?[^\s\"']{16,}",
            re.IGNORECASE,
        ),
    ),
)

_PREFILTER_MODULE: Any = None


def _prefilter_module() -> Any:
    """Load the pinned source_prefilter.py sibling; sys.modules is never consulted."""
    global _PREFILTER_MODULE
    if _PREFILTER_MODULE is None:
        path = Path(__file__).resolve().with_name("source_prefilter.py")
        spec = importlib.util.spec_from_file_location("fleet_content_policy_prefilter", path)
        if spec is None or spec.loader is None:
            raise RuntimeError("Pinned source_prefilter sibling is unavailable")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _PREFILTER_MODULE = module
    return _PREFILTER_MODULE


def scan_sensitive_source(content: bytes, path: str) -> list[str]:
    """Unchanged canonical scan: pinned source_prefilter plus the fixed patterns."""
    return _prefilter_module().scan(content, path, SENSITIVE_PATTERNS)
