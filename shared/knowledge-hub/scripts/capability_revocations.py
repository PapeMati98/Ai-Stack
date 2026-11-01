#!/usr/bin/env python3
"""Strict parser for the versioned Capability Hub revocation catalog."""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path

from capability_core import (
    GIT_OID,
    HEX_64,
    SLUG,
    CapabilityError,
    GitSource,
    absolute_plain_path,
    parse_iso_timestamp,
    sha256_bytes,
    strict_json_loads,
)


REVOCATION_NAME = "capability-revocations.json"
ENTRY_KEYS = {
    "capability_id",
    "manifest_fingerprint",
    "payload_fingerprint",
    "reason",
    "revoked_at",
    "source_commit",
    "source_tree",
}


@dataclass(frozen=True)
class RevocationCatalog:
    path: Path
    raw_sha256: str
    updated_at: str
    entries: tuple[dict[str, str], ...]


def load_revocations(
    path: Path | str,
    *,
    raw: bytes | None = None,
) -> RevocationCatalog:
    if raw is None:
        catalog_path = absolute_plain_path(path, must_exist=True)
        if not stat.S_ISREG(catalog_path.lstat().st_mode):
            raise CapabilityError(f"Revocation catalog is not a plain file: {catalog_path}")
        raw = catalog_path.read_bytes()
    else:
        catalog_path = Path(str(path))
    try:
        value = strict_json_loads(raw.decode("ascii", errors="strict"))
    except (OSError, UnicodeDecodeError, CapabilityError) as exc:
        raise CapabilityError(f"Invalid revocation catalog: {catalog_path}") from exc
    if not isinstance(value, dict) or set(value) != {
        "entries",
        "schema_version",
        "updated_at",
    }:
        raise CapabilityError("Revocation catalog top-level keys are invalid")
    if value.get("schema_version") != 1:
        raise CapabilityError("Unsupported revocation catalog schema")
    updated_at = value.get("updated_at")
    if not isinstance(updated_at, str):
        raise CapabilityError("Revocation catalog updated_at is invalid")
    parse_iso_timestamp(updated_at, "revocation catalog updated_at")
    rows = value.get("entries")
    if not isinstance(rows, list):
        raise CapabilityError("Revocation catalog entries must be a list")
    entries: list[dict[str, str]] = []
    identities: set[tuple[str, str, str, str]] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != ENTRY_KEYS:
            raise CapabilityError("Revocation catalog entry keys are invalid")
        if not isinstance(row.get("capability_id"), str) or not SLUG.fullmatch(
            row["capability_id"]
        ):
            raise CapabilityError("Revocation capability_id is invalid")
        for key in ("source_commit", "source_tree"):
            if not isinstance(row.get(key), str) or not GIT_OID.fullmatch(row[key]):
                raise CapabilityError(f"Revocation {key} is invalid")
        for key in ("manifest_fingerprint", "payload_fingerprint"):
            if not isinstance(row.get(key), str) or not HEX_64.fullmatch(row[key]):
                raise CapabilityError(f"Revocation {key} is invalid")
        reason = row.get("reason")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
            raise CapabilityError("Revocation reason is invalid")
        revoked_at = row.get("revoked_at")
        if not isinstance(revoked_at, str):
            raise CapabilityError("Revocation revoked_at is invalid")
        parse_iso_timestamp(revoked_at, "revoked_at")
        normalized = {key: str(row[key]) for key in ENTRY_KEYS}
        identity = (
            normalized["capability_id"],
            normalized["source_tree"],
            normalized["manifest_fingerprint"],
            normalized["payload_fingerprint"],
        )
        if identity in identities:
            raise CapabilityError("Revocation catalog contains a duplicate release")
        identities.add(identity)
        entries.append(normalized)
    return RevocationCatalog(
        path=catalog_path,
        raw_sha256=sha256_bytes(raw),
        updated_at=updated_at,
        entries=tuple(entries),
    )


def matching_revocation(
    catalog: RevocationCatalog,
    capability_id: str,
    source: GitSource,
) -> dict[str, str] | None:
    expected = (
        capability_id,
        source.source_tree,
        source.manifest_fingerprint,
        source.payload_fingerprint,
    )
    for row in catalog.entries:
        identity = (
            row["capability_id"],
            row["source_tree"],
            row["manifest_fingerprint"],
            row["payload_fingerprint"],
        )
        if identity == expected:
            return row
    return None
