#!/usr/bin/env python3
"""Offline review-synthesis helper for marketing-context.

Reads a JSON list of already collected redacted review records and writes a
coverage-aware synthesis summary. No scraping, no URL fetching, no provider or
model classification, no population inference, no env/secret reads.

Identity is (source_url, id). Text is never used to collapse reviews.

Every emitted label (provenance, identity, themes, stance) is redacted when it
looks sensitive; identity and deduplication always use the original internal
values so sanitization never merges unrelated records.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"


class ReviewSynthesisError(ValueError):
    """Input or refusal error that must not echo raw sensitive text."""


_SECRET_FILENAMES = frozenset(
    {
        ".env",
        ".env.local",
        ".env.production",
        ".env.development",
        ".netrc",
        "auth.json",
        "cookies.json",
        "credentials",
        "credentials.json",
        "netrc",
        "secrets",
        "secrets.json",
        "id_rsa",
        "id_rsa.pub",
        "id_ed25519",
        "id_ed25519.pub",
        "private.key",
        "private_key",
        "private_key.json",
        "client_secret.json",
        "service_account.json",
    }
)

_SENSITIVE_BASENAME_RE = re.compile(
    r"(?:^|[._-])(?:secrets?|credentials?|tokens?|passwords?|cookies?|"
    r"private[-_]?key|api[-_]?key|sessions?|auth[-_]?tokens?)(?:$|[._-])",
    re.IGNORECASE,
)

_SENSITIVE_KEY_NAMES = (
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "x-auth-token",
    "credential",
    "credentials",
    "password",
    "passwd",
    "pwd",
    "secret",
    "secrets",
    "token",
    "tokens",
    "api_key",
    "apikey",
    "api-key",
    "private_key",
    "privatekey",
    "private-key",
    "session_id",
    "sessionid",
    "session-id",
    "access_token",
    "refresh_token",
    "client_secret",
    "clientsecret",
    "client-secret",
    "auth",
    "bearer",
)


def _normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


_NORMALIZED_SENSITIVE_KEYS = frozenset(
    _normalize_key(name) for name in _SENSITIVE_KEY_NAMES
)

_DQUOTE = chr(34)
_SQUOTE = "'"
SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)(?:^|[^A-Za-z0-9])"
    r"((?:api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|auth[_-]?token|"
    r"client[_-]?secret|private[_-]?key|session[_-]?id|"
    r"authorization|proxy-authorization|cookie|set-cookie|"
    r"x-api-key|x-auth-token|credential|credentials|"
    r"password|passwd|pwd|secret|secrets|token|tokens|auth|bearer))"
    r"(\s*[\"']?\s*[:=]\s*)"
    r"(?:"
    + _DQUOTE
    + r"[^"
    + _DQUOTE
    + r"]*"
    + _DQUOTE
    + r"|"
    + _SQUOTE
    + r"[^"
    + _SQUOTE
    + r"]*"
    + _SQUOTE
    + r"|[^\s,;\"'\]}>]+)"
)
BEARER_TOKEN_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-+/=]{8,}")
JWT_RE = re.compile(
    r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"
)
AWS_KEY_RE = re.compile(r"\bAKIA[0-9A-Z]{16}\b")


def looks_sensitive(value: Any) -> bool:
    """True when a string looks like unredacted credential material."""
    if not isinstance(value, str) or not value:
        return False
    if SENSITIVE_ASSIGNMENT_RE.search(value):
        return True
    if BEARER_TOKEN_RE.search(value):
        return True
    if JWT_RE.search(value):
        return True
    if AWS_KEY_RE.search(value):
        return True
    return False


def safe_emit_label(value: str) -> str:
    """Redact sensitive-looking metadata while keeping distinct values distinct."""
    if not looks_sensitive(value):
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return f"<redacted:{digest}>"


def redact_url(raw: Any) -> str:
    """Credential-free, query-free, fragment-free provenance label.

    Keeps scheme://host/path so distinct paths stay distinct after sanitization.
    Falls back to a stable hash label when the remaining path still looks sensitive.
    """
    if not isinstance(raw, str) or not raw.strip():
        return "<missing-url>"
    try:
        parsed = urlparse(raw.strip())
    except ValueError:
        return "<unparseable-url>"
    scheme = (parsed.scheme or "").lower()
    host = parsed.hostname or ""
    if scheme and host:
        path = parsed.path or ""
        candidate = f"{scheme}://{host}{path}"
        if looks_sensitive(candidate) or looks_sensitive(path):
            digest = hashlib.sha256(raw.strip().encode("utf-8")).hexdigest()[:8]
            return f"<redacted-url:{digest}>"
        return candidate
    return "<unparseable-url>"


def _query_has_credential_params(query: str) -> bool:
    for part in query.split("&"):
        if not part:
            continue
        raw_key = part.split("=", 1)[0].strip()
        if _normalize_key(raw_key) in _NORMALIZED_SENSITIVE_KEYS:
            return True
    return False


def validate_source_url(raw: Any) -> tuple[bool, str]:
    """Validate http(s) provenance-only URL.

    Syntactic validity is provenance labeling only; it never verifies content.
    Rejects non-http(s) schemes, embedded credentials, and credential-like
    query parameters.
    """
    if not isinstance(raw, str) or not raw.strip():
        return False, "missing or empty source_url"
    text = raw.strip()
    try:
        parsed = urlparse(text)
    except ValueError:
        return False, "source_url is not parseable"
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return False, "source_url scheme must be http or https"
    if not parsed.hostname:
        return False, "source_url missing host"
    if parsed.username is not None or parsed.password is not None:
        return False, "source_url must not contain credentials"
    if "@" in (parsed.netloc or ""):
        return False, "source_url must not contain credentials"
    if parsed.query and (
        _query_has_credential_params(parsed.query) or looks_sensitive(parsed.query)
    ):
        return False, "source_url must not contain credential-like query parameters"
    return True, "provenance-only; syntax does not verify content"


def _optional_str(value: Any, field: str) -> tuple[str | None, list[str]]:
    if value is None:
        return None, []
    if not isinstance(value, str):
        return None, [f"{field} must be a string when present"]
    return value, []


def normalize_record(record: Any, row_index: int) -> tuple[dict[str, Any] | None, list[str]]:
    """Validate one record. Errors never include raw text or credential URLs."""
    if not isinstance(record, dict):
        return None, [f"row {row_index}: record must be a JSON object"]

    errors: list[str] = []

    raw_id = record.get("id")
    if not isinstance(raw_id, str) or not raw_id.strip():
        errors.append(f"row {row_index}: missing or invalid id")
        rec_id = None
    else:
        rec_id = raw_id.strip()

    url_ok, url_reason = validate_source_url(record.get("source_url"))
    if not url_ok:
        errors.append(f"row {row_index}: {url_reason}")

    text = record.get("text")
    if not isinstance(text, str):
        errors.append(f"row {row_index}: missing or invalid text")
        text = None

    themes = record.get("themes")
    if not isinstance(themes, list):
        errors.append(f"row {row_index}: themes must be a list of strings")
        themes = None
    else:
        cleaned_themes: list[str] = []
        for entry in themes:
            if not isinstance(entry, str):
                errors.append(f"row {row_index}: themes entries must be strings")
                cleaned_themes = []
                break
            cleaned_themes.append(entry)
        themes = cleaned_themes

    published_at, pub_errors = _optional_str(record.get("published_at"), "published_at")
    for message in pub_errors:
        errors.append(f"row {row_index}: {message}")

    stance, stance_errors = _optional_str(record.get("stance"), "stance")
    for message in stance_errors:
        errors.append(f"row {row_index}: {message}")

    if errors:
        return None, errors

    source_url = record.get("source_url")
    assert isinstance(source_url, str) and isinstance(rec_id, str)
    assert isinstance(text, str) and isinstance(themes, list)
    return {
        "row_index": row_index,
        "id": rec_id,
        "source_url": source_url.strip(),
        "text": text,
        "themes": themes,
        "published_at": published_at,
        "stance": stance,
    }, []


def content_key(rec: dict[str, Any]) -> str:
    """Material content fingerprint for conflict vs exact-duplicate detection."""
    payload = {
        "text": rec["text"],
        "themes": sorted(set(rec["themes"])),
        "stance": rec["stance"],
        "published_at": rec["published_at"],
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def summarize_reviews(records: Any) -> dict[str, Any]:
    """Synthesize already collected redacted review records.

    Never collapses equal text across identities, never silently selects a
    conflicting identity, and always reports denominator accounting.

    Identity and deduplication use original (source_url, id). Emitted labels are
    redacted independently so sanitization never merges unrelated records.
    """
    if not isinstance(records, list):
        raise ReviewSynthesisError("input must be a JSON list of review records")

    invalid_records: list[dict[str, Any]] = []
    normalized: list[dict[str, Any]] = []
    redacted_label_count = 0

    for index, raw in enumerate(records):
        record = raw if isinstance(raw, dict) else None
        normalized_rec, errors = normalize_record(raw, index)
        if normalized_rec is None:
            raw_id = record.get("id") if record is not None else None
            invalid_records.append(
                {
                    "row_index": index,
                    "id": safe_emit_label(raw_id)
                    if isinstance(raw_id, str)
                    else None,
                    "source_url_redacted": redact_url(
                        record.get("source_url") if record is not None else None
                    ),
                    "reasons": errors,
                }
            )
            if isinstance(raw_id, str) and looks_sensitive(raw_id):
                redacted_label_count += 1
        else:
            normalized.append(normalized_rec)

    by_identity: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for rec in normalized:
        by_identity.setdefault((rec["source_url"], rec["id"]), []).append(rec)

    unique_reviews: list[dict[str, Any]] = []
    duplicate_rows = 0
    conflict_rows = 0
    conflicts: list[dict[str, Any]] = []

    for source_url, rec_id in sorted(by_identity):
        items = by_identity[(source_url, rec_id)]
        variants: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            variants.setdefault(content_key(item), []).append(item)

        if len(variants) == 1:
            representative = items[0]
            unique_reviews.append(representative)
            duplicate_rows += len(items) - 1
            continue

        conflict_rows += len(items)
        emit_id = safe_emit_label(rec_id)
        emit_url = redact_url(source_url)
        if emit_id != rec_id:
            redacted_label_count += 1
        if looks_sensitive(source_url):
            redacted_label_count += 1
        conflicts.append(
            {
                "id": emit_id,
                "source_url": emit_url,
                "row_indices": sorted(item["row_index"] for item in items),
                "variant_count": len(variants),
                "variant_row_counts": sorted(len(v) for v in variants.values()),
                "reason": (
                    "same identity with differing content; unresolved and not "
                    "silently selected"
                ),
            }
        )

    unique_reviews.sort(key=lambda rec: rec["row_index"])

    source_distribution: dict[str, int] = {}
    for rec in unique_reviews:
        emit_key = redact_url(rec["source_url"])
        if looks_sensitive(rec["source_url"]):
            redacted_label_count += 1
        source_distribution[emit_key] = source_distribution.get(emit_key, 0) + 1

    themes_out: dict[str, dict[str, Any]] = {}
    for rec in unique_reviews:
        emit_review_id = safe_emit_label(rec["id"])
        if emit_review_id != rec["id"]:
            redacted_label_count += 1
        for theme in sorted(set(rec["themes"])):
            emit_theme = safe_emit_label(theme)
            if emit_theme != theme:
                redacted_label_count += 1
            entry = themes_out.setdefault(emit_theme, {"count": 0, "review_ids": []})
            entry["count"] += 1
            if emit_review_id not in entry["review_ids"]:
                entry["review_ids"].append(emit_review_id)

    stance_breakdown: dict[str, int] = {}
    unknown_dates = 0
    for rec in unique_reviews:
        if rec["stance"] is None:
            label = "unspecified"
        else:
            label = safe_emit_label(rec["stance"])
            if label != rec["stance"]:
                redacted_label_count += 1
        stance_breakdown[label] = stance_breakdown.get(label, 0) + 1
        if rec["published_at"] is None:
            unknown_dates += 1

    total_rows = len(records)
    valid_rows = len(normalized)
    invalid_rows = len(invalid_records)
    unique_count = len(unique_reviews)

    limitations = [
        (
            "sample_only=true: counts and proportions describe only this "
            "collected redacted sample and must not be sold as population facts"
        ),
        (
            "source_url values are provenance labels only; a syntactically "
            "valid URL does not verify review content"
        ),
        (
            "no scraping, fetching, model classification, or population "
            "inference is performed by this helper"
        ),
    ]
    if redacted_label_count:
        limitations.append(
            f"{redacted_label_count} emitted label(s) were redacted as sensitive "
            "metadata; original values are not exposed and identity/dedup used "
            "the original internal values"
        )
    if unknown_dates:
        limitations.append(
            f"{unknown_dates} unique review(s) have unknown dates "
            "(missing published_at); they are retained, not dropped, and cannot "
            "support temporal claims"
        )
    distinct_stances = sorted(
        label for label in stance_breakdown if label != "unspecified"
    )
    if len(distinct_stances) > 1:
        limitations.append(
            "opposing or mixed stances present ("
            + ", ".join(distinct_stances)
            + "); all are retained in stance_breakdown and must not be netted "
            "into a single sentiment score"
        )
    if conflicts:
        limitations.append(
            f"{len(conflicts)} identity conflict(s) remain unresolved and are "
            "excluded from unique_reviews"
        )

    return {
        "schema_version": 1,
        "sample_only": True,
        "coverage": {
            "total_rows": total_rows,
            "valid_rows": valid_rows,
            "invalid_rows": invalid_rows,
            "duplicate_rows": duplicate_rows,
            "unique_reviews": unique_count,
            "conflict_rows": conflict_rows,
            "conflict_identities": len(conflicts),
        },
        "source_distribution": dict(sorted(source_distribution.items())),
        "themes": {
            key: {
                "count": themes_out[key]["count"],
                "review_ids": themes_out[key]["review_ids"],
            }
            for key in sorted(themes_out)
        },
        "stance_breakdown": {
            key: stance_breakdown[key] for key in sorted(stance_breakdown)
        },
        "conflicts": conflicts,
        "invalid_records": invalid_records,
        "limitations": limitations,
    }


def _looks_like_secret_file(path: Path) -> bool:
    """Reject env/secret filenames, aliases, and reparse paths before reading."""
    basenames: set[str] = set()
    component_names: set[str] = set()
    if path.name:
        basenames.add(path.name.lower())
    for part in path.parts:
        if part:
            component_names.add(part.lower())
    try:
        resolved = path.resolve(strict=False)
        if resolved.name:
            basenames.add(resolved.name.lower())
        for part in resolved.parts:
            if part:
                component_names.add(part.lower())
    except (OSError, ValueError):
        pass
    for name in component_names:
        if name in _SECRET_FILENAMES:
            return True
        if name.startswith(".env"):
            return True
    for name in basenames:
        if name in _SECRET_FILENAMES:
            return True
        if name.startswith(".env"):
            return True
        if _SENSITIVE_BASENAME_RE.search(name):
            return True
    return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Summarize already collected redacted review records offline "
            "(no fetching, no classification, no population inference)."
        )
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Path to a JSON list of already collected redacted review records",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="New JSON output path; refused when the file already exists",
    )
    return parser


def _create_new_file_exclusive(path: Path, text: str) -> None:
    """Atomically create a new output file; refuse if the path already exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(str(path), flags, 0o600)
    except FileExistsError as exc:
        raise ReviewSynthesisError(
            "refusing to overwrite existing output file"
        ) from exc
    except OSError as exc:
        raise ReviewSynthesisError("output file could not be created") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def run_cli(input_path: str, output_path: str | None) -> dict[str, Any]:
    in_path = Path(input_path)
    if _looks_like_secret_file(in_path):
        raise ReviewSynthesisError("refusing to read env or secret filename")
    if not in_path.is_file():
        raise ReviewSynthesisError("input file not found")
    try:
        payload = json.loads(in_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ReviewSynthesisError(
            f"input is not valid JSON (line {exc.lineno})"
        ) from exc
    except OSError as exc:
        raise ReviewSynthesisError("input file could not be read") from exc

    result = summarize_reviews(payload)

    if output_path is not None:
        out_path = Path(output_path)
        try:
            same = out_path.resolve() == in_path.resolve()
        except OSError:
            same = False
        if same:
            raise ReviewSynthesisError("refusing to overwrite the input source file")
        if _looks_like_secret_file(out_path):
            raise ReviewSynthesisError("refusing to write env or secret filename")
        if out_path.exists():
            raise ReviewSynthesisError("refusing to overwrite existing output file")
        _create_new_file_exclusive(
            out_path, json.dumps(result, indent=2, ensure_ascii=False) + "\n"
        )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = run_cli(args.input, args.output)
    except ReviewSynthesisError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.output is None:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
