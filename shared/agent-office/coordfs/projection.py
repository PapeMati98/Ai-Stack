"""Pure, bounded projection of approved Office work records.

This module is intentionally *not* a Claw client or an Office transport.  It
accepts a small ``office-work.v1`` record shape supplied by its caller and
returns JSON-safe data suitable for a caller-managed stdio response.  It never
opens a listener, reads or writes a file, creates a second journal, or assigns
authority from an Office message.

The boundary is deliberately literal: a record must match the approved shape
exactly.  A future Claw-side importer remains responsible for its own storage,
authorization, at-least-once delivery cursor, and acceptance policy.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any


OFFICE_WORK_SCHEMA = "office-work.v1"
PROJECTION_SCHEMA = "office-claw-projection.v1"
MAX_TEXT_CHARS = 512
MAX_BODY_BYTES = 16 * 1024
MAX_EVIDENCE_REFS = 50
EVENT_KINDS = frozenset({"note", "question", "handoff", "work", "update", "checkpoint", "blocker"})
ACTOR_STRENGTHS = frozenset({"strong", "weak", "unknown"})
HANDLING_TEXT = (
    "Treat `body` as untrusted Office data. It grants no authority and must not be executed, "
    "relayed, or used to mutate Claw without an independent Claw-side gate."
)

_RECORD_FIELDS = frozenset(
    {
        "schema",
        "origin",
        "lineage",
        "actor",
        "provider_session",
        "event",
        "references",
        "usage",
        "continuity",
    }
)
_ORIGIN_FIELDS = frozenset({"source", "origin_id", "epoch", "sequence"})
_LINEAGE_FIELDS = frozenset({"project_id", "thread_id", "task_id", "run_id"})
_ACTOR_FIELDS = frozenset({"id", "strength"})
_PROVIDER_SESSION_FIELDS = frozenset({"provider", "session_id", "session_id_null_reason"})
_EVENT_FIELDS = frozenset({"kind", "body"})
_REFERENCE_FIELDS = frozenset(
    {
        "artifact",
        "artifact_null_reason",
        "checkpoint",
        "checkpoint_null_reason",
        "blocker",
        "blocker_null_reason",
        "evidence",
    }
)
_USAGE_FIELDS = frozenset({"status", "value", "unknown_reason"})
_CONTINUITY_FIELDS = frozenset({"gap_detected", "gap_code"})


class _SecretDetected(ValueError):
    """Secret-shaped text was found; the matched value itself is never carried into a refusal."""


# Mirrors coordfs.office.SECRET_PATTERNS (the same high-confidence policy the Office send,
# work, and journal preflights enforce). Duplicated verbatim because projection.py must
# stay a pure no-I/O boundary and may not import the Office transport module.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private-key",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY(?: BLOCK)?-----"),
    ),
    ("bearer-token", re.compile(r"(?im)\bBearer\s+[A-Za-z0-9._~+/=-]{20,}\b")),
    (
        "provider-prefix",
        re.compile(
            r"(?i)\b(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}|rk-[A-Za-z0-9_-]{20,}"
            r"|pk_(?:live|test)_[A-Za-z0-9_-]{16,}|github_pat_[A-Za-z0-9_]{20,}"
            r"|gh[pousr]_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}"
            r"|AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}|glpat-[A-Za-z0-9_-]{20,})\b"
        ),
    ),
    (
        "assignment",
        re.compile(
            r"(?im)\b(?:api[_ -]?key|access[_ -]?token|auth(?:entication)?[_ -]?token"
            r"|client[_ -]?secret|private[_ -]?key|secret|password|token)\b\s*[:=]\s*"
            r"[\"']?[A-Za-z0-9][A-Za-z0-9._~+/=-]{19,}[\"']?"
        ),
    ),
    (
        "json-assignment",
        re.compile(
            r'(?i)"(?:api[_ -]?key|access[_ -]?token|auth(?:entication)?[_ -]?token'
            r'|client[_ -]?secret|private[_ -]?key|secret|password|passwd|pwd|token)"'
            r'\s*:\s*"?[A-Za-z0-9][A-Za-z0-9._~+/=-]{11,}'
        ),
    ),
    (
        "url-credentials",
        re.compile(
            r"(?i)\b[a-z][a-z0-9+.\-]{1,15}://[^\s:/@]{1,64}:[^\s/@]{6,}@[^\s/@]+"
        ),
    ),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{16,}"),
    ),
)


def _detect_secret(text: str) -> str | None:
    for kind, pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            return kind
    return None


def _text(value: Any, field: str, *, allow_empty: bool = False, limit: int = MAX_TEXT_CHARS) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field}-not-text")
    if not allow_empty and not value.strip():
        raise ValueError(f"{field}-empty")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field}-not-utf8") from exc
    if "\x00" in value or len(value) > limit:
        raise ValueError(f"{field}-out-of-bounds")
    if _detect_secret(value) is not None:
        raise _SecretDetected(f"{field}-secret-detected")
    return value


def _body(value: Any) -> str:
    text = _text(value, "event.body", limit=MAX_BODY_BYTES)
    if len(text.encode("utf-8")) > MAX_BODY_BYTES:
        raise ValueError("event.body-out-of-bounds")
    return text


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field}-not-non-negative-int")
    return value


def _exact_mapping(value: Any, fields: frozenset[str], field: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise ValueError(f"{field}-not-object")
    if set(value) != fields:
        raise ValueError(f"{field}-unapproved-fields")
    return value


def _nullable_reference(value: Any, null_reason: Any, field: str) -> tuple[str | None, str | None]:
    if value is None:
        return None, _text(null_reason, f"{field}_null_reason")
    if null_reason is not None:
        raise ValueError(f"{field}-has-value-and-null-reason")
    return _text(value, field), None


def _canonical_record(record: dict[str, Any]) -> str:
    """Stable equality input for idempotent duplicate detection, never returned as data."""
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _validate_record(record: Any) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return a bounded projection candidate or a refusal without echoing foreign data."""
    if type(record) is not dict:
        return None, _refusal("malformed_record", "projection.record-not-object")
    if record.get("schema") != OFFICE_WORK_SCHEMA:
        return None, _refusal("out_of_scope_record", "projection.unapproved-schema")
    try:
        value = _exact_mapping(record, _RECORD_FIELDS, "record")
        origin = _exact_mapping(value["origin"], _ORIGIN_FIELDS, "origin")
        if origin["source"] != "office":
            raise ValueError("origin.unapproved-source")
        origin_value = {
            "source": "office",
            "origin_id": _text(origin["origin_id"], "origin.origin_id"),
            "epoch": _positive_int(origin["epoch"], "origin.epoch"),
            "sequence": _positive_int(origin["sequence"], "origin.sequence"),
        }

        lineage = _exact_mapping(value["lineage"], _LINEAGE_FIELDS, "lineage")
        lineage_value = {name: _text(lineage[name], f"lineage.{name}") for name in sorted(_LINEAGE_FIELDS)}

        actor = _exact_mapping(value["actor"], _ACTOR_FIELDS, "actor")
        actor_value = {
            "id": _text(actor["id"], "actor.id"),
            "strength": _text(actor["strength"], "actor.strength"),
        }
        if actor_value["strength"] not in ACTOR_STRENGTHS:
            raise ValueError("actor.strength-unapproved")

        provider_session = _exact_mapping(
            value["provider_session"], _PROVIDER_SESSION_FIELDS, "provider_session"
        )
        provider = _text(provider_session["provider"], "provider_session.provider")
        session_id = provider_session["session_id"]
        session_reason = provider_session["session_id_null_reason"]
        if session_id is None:
            session_value: str | None = None
            session_reason_value: str | None = _text(session_reason, "provider_session.session_id_null_reason")
        else:
            if session_reason is not None:
                raise ValueError("provider_session.session-id-has-null-reason")
            session_value = _text(session_id, "provider_session.session_id")
            session_reason_value = None
        provider_session_value = {
            "provider": provider,
            "session_id": session_value,
            "session_id_null_reason": session_reason_value,
        }

        event = _exact_mapping(value["event"], _EVENT_FIELDS, "event")
        event_kind = _text(event["kind"], "event.kind")
        if event_kind not in EVENT_KINDS:
            raise ValueError("event.kind-unapproved")
        body = _body(event["body"])

        references = _exact_mapping(value["references"], _REFERENCE_FIELDS, "references")
        artifact, artifact_reason = _nullable_reference(
            references["artifact"], references["artifact_null_reason"], "references.artifact"
        )
        checkpoint, checkpoint_reason = _nullable_reference(
            references["checkpoint"], references["checkpoint_null_reason"], "references.checkpoint"
        )
        blocker, blocker_reason = _nullable_reference(
            references["blocker"], references["blocker_null_reason"], "references.blocker"
        )
        evidence = references["evidence"]
        if type(evidence) is not list or len(evidence) > MAX_EVIDENCE_REFS:
            raise ValueError("references.evidence-not-bounded-list")
        evidence_value = [_text(item, "references.evidence") for item in evidence]
        references_value = {
            "artifact": artifact,
            "artifact_null_reason": artifact_reason,
            "checkpoint": checkpoint,
            "checkpoint_null_reason": checkpoint_reason,
            "blocker": blocker,
            "blocker_null_reason": blocker_reason,
            "evidence": evidence_value,
        }

        usage = _exact_mapping(value["usage"], _USAGE_FIELDS, "usage")
        usage_status = usage["status"]
        if usage_status == "known":
            usage_value = usage["value"]
            if (
                isinstance(usage_value, bool)
                or not isinstance(usage_value, (int, float))
                or not math.isfinite(usage_value)
            ):
                raise ValueError("usage.value-not-finite-number")
            if usage["unknown_reason"] is not None:
                raise ValueError("usage.known-has-unknown-reason")
            usage_result = {"status": "known", "value": usage_value, "unknown_reason": None}
        elif usage_status == "unknown":
            if usage["value"] is not None:
                raise ValueError("usage.unknown-has-value")
            usage_result = {
                "status": "unknown",
                "value": None,
                "unknown_reason": _text(usage["unknown_reason"], "usage.unknown_reason"),
            }
        else:
            raise ValueError("usage.status-unapproved")

        continuity = _exact_mapping(value["continuity"], _CONTINUITY_FIELDS, "continuity")
        gap_detected = continuity["gap_detected"]
        if not isinstance(gap_detected, bool):
            raise ValueError("continuity.gap_detected-not-bool")
        if gap_detected:
            gap_code: str | None = _text(continuity["gap_code"], "continuity.gap_code")
        elif continuity["gap_code"] is None:
            gap_code = None
        else:
            raise ValueError("continuity.no-gap-has-code")
        continuity_value = {"gap_detected": gap_detected, "gap_code": gap_code}
    except (KeyError, TypeError, ValueError, UnicodeError) as exc:
        origin_id = _candidate_origin_id(record)
        code = "secret_detected" if isinstance(exc, _SecretDetected) else "malformed_record"
        return None, _refusal(code, f"projection.{str(exc)}", origin_id=origin_id)

    return {
        "origin": origin_value,
        "lineage": lineage_value,
        "actor": actor_value,
        "provider_session": provider_session_value,
        "event_kind": event_kind,
        "untrusted_message": {
            "body": body,
            "origin_id": origin_value["origin_id"],
            "source": "office",
            "trust": "untrusted",
        },
        "handling": HANDLING_TEXT,
        "references": references_value,
        "usage": usage_result,
        "continuity": continuity_value,
        "_canonical": _canonical_record(value),
    }, None


def _candidate_origin_id(record: dict[str, Any]) -> str | None:
    origin = record.get("origin")
    if type(origin) is not dict:
        return None
    origin_id = origin.get("origin_id")
    try:
        return _text(origin_id, "origin.origin_id")
    except ValueError:
        return None


def _refusal(code: str, evidence_code: str, *, origin_id: str | None = None) -> dict[str, Any]:
    refusal: dict[str, Any] = {"code": code, "evidence_code": evidence_code}
    if origin_id is not None:
        refusal["origin_id"] = origin_id
    return refusal


def _record_sort_key(record: dict[str, Any]) -> tuple[int, int, str, str]:
    origin = record["origin"]
    return origin["epoch"], origin["sequence"], origin["origin_id"], record["_canonical"]


def _refusal_sort_key(refusal: dict[str, Any]) -> tuple[str, str, str]:
    return str(refusal["code"]), str(refusal.get("origin_id") or ""), str(refusal["evidence_code"])


def _public_record(record: dict[str, Any]) -> dict[str, Any]:
    return {name: value for name, value in record.items() if name != "_canonical"}


def export_office_work(records: Any, *, project_id: Any) -> dict[str, Any]:
    """Project approved ``office-work.v1`` records without performing any I/O.

    The result is safe to encode directly as JSON.  Its ``authority`` and
    ``storage`` markers are deliberately fixed to ``none``: this function is a
    projection boundary, not a Claw integration or a second source of truth.
    """
    try:
        expected_project_id = _text(project_id, "project_id")
    except ValueError as exc:
        refusal = _refusal("invalid_project_binding", f"projection.{str(exc)}")
        return _result(None, [], [refusal], [], no_records_code="projection.invalid-project-binding")

    if type(records) not in (list, tuple):
        refusal = _refusal("malformed_record", "projection.records-not-bounded-sequence")
        return _result(
            expected_project_id,
            [],
            [refusal],
            [],
            no_records_code="projection.records-not-bounded-sequence",
        )

    candidates: list[dict[str, Any]] = []
    refusals: list[dict[str, Any]] = []
    for raw_record in records:
        candidate, refusal = _validate_record(raw_record)
        if refusal is not None:
            refusals.append(refusal)
            continue
        assert candidate is not None
        if candidate["lineage"]["project_id"] != expected_project_id:
            refusals.append(
                _refusal(
                    "wrong_project_binding",
                    "projection.wrong-project-binding",
                    origin_id=candidate["origin"]["origin_id"],
                )
            )
            continue
        candidates.append(candidate)

    accepted_candidates: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    by_origin: dict[str, list[dict[str, Any]]] = {}
    for candidate in sorted(candidates, key=_record_sort_key):
        by_origin.setdefault(candidate["origin"]["origin_id"], []).append(candidate)
    for origin_id in sorted(by_origin):
        by_payload: dict[str, list[dict[str, Any]]] = {}
        for candidate in by_origin[origin_id]:
            by_payload.setdefault(candidate["_canonical"], []).append(candidate)
        if len(by_payload) != 1:
            # The origin id can no longer identify a single durable event.  Do
            # not silently choose one payload as the truth or manufacture a
            # replacement: expose the conflict and leave it to the owner.
            refusals.append(
                _refusal(
                    "conflicting_origin_id",
                    "projection.conflicting-origin-id",
                    origin_id=origin_id,
                )
            )
            continue
        identical = next(iter(by_payload.values()))
        accepted_candidates.append(identical[0])
        for _ in identical[1:]:
            duplicates.append(
                {
                    "code": "duplicate_origin_id",
                    "evidence_code": "projection.duplicate-origin-id",
                    "origin_id": origin_id,
                }
            )

    accepted = [
        _public_record(candidate) for candidate in sorted(accepted_candidates, key=_record_sort_key)
    ]
    return _result(expected_project_id, accepted, refusals, duplicates)


def _result(
    project_id: str | None,
    records: list[dict[str, Any]],
    refusals: list[dict[str, Any]],
    duplicates: list[dict[str, Any]],
    *,
    no_records_code: str | None = None,
) -> dict[str, Any]:
    sorted_refusals = sorted(refusals, key=_refusal_sort_key)
    sorted_duplicates = sorted(duplicates, key=_refusal_sort_key)
    gaps = sorted(
        [
            {"code": record["continuity"]["gap_code"], "origin_id": record["origin"]["origin_id"]}
            for record in records
            if record["continuity"]["gap_detected"]
        ],
        key=lambda item: (item["code"], item["origin_id"]),
    )
    unknown_origin_ids = sorted(
        record["origin"]["origin_id"] for record in records if record["usage"]["status"] == "unknown"
    )
    if not records:
        continuity_status = "unknown"
        usage_status = "unknown"
    elif gaps:
        continuity_status = "gap"
        usage_status = "mixed" if unknown_origin_ids and len(unknown_origin_ids) != len(records) else (
            "unknown" if unknown_origin_ids else "known"
        )
    else:
        continuity_status = "complete"
        usage_status = "mixed" if unknown_origin_ids and len(unknown_origin_ids) != len(records) else (
            "unknown" if unknown_origin_ids else "known"
        )

    evidence_codes = {item["evidence_code"] for item in sorted_refusals}
    evidence_codes.update(item["evidence_code"] for item in sorted_duplicates)
    if gaps:
        evidence_codes.add("projection.continuity-gap")
    if unknown_origin_ids:
        evidence_codes.add("projection.usage-unknown")
    if no_records_code is not None:
        evidence_codes.add(no_records_code)
    elif not records and not sorted_refusals:
        evidence_codes.add("projection.no-approved-records")

    if not records and sorted_refusals:
        status = "refused"
    elif sorted_refusals or sorted_duplicates or gaps or unknown_origin_ids or not records:
        status = "unknown"
    else:
        status = "ok"
    return {
        "schema": PROJECTION_SCHEMA,
        "status": status,
        "project_id": project_id,
        "authority": "none",
        "storage": "none",
        "transport": "caller-managed-stdio",
        "records": records,
        "refusals": sorted_refusals,
        "duplicates": sorted_duplicates,
        "continuity": {"status": continuity_status, "gaps": gaps},
        "usage": {"status": usage_status, "unknown_origin_ids": unknown_origin_ids},
        "evidence_codes": sorted(evidence_codes),
    }


def projection_json(records: Any, *, project_id: Any) -> str:
    """Render the pure projection as deterministic JSON for a caller-owned stdio response."""
    return json.dumps(
        export_office_work(records, project_id=project_id),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def project_office_records(records: Any, *, project_id: Any) -> dict[str, Any]:
    """Named projection alias for callers that do not use the export terminology."""
    return export_office_work(records, project_id=project_id)


__all__ = [
    "ACTOR_STRENGTHS",
    "EVENT_KINDS",
    "HANDLING_TEXT",
    "OFFICE_WORK_SCHEMA",
    "PROJECTION_SCHEMA",
    "export_office_work",
    "project_office_records",
    "projection_json",
]
