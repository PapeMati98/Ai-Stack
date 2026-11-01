#!/usr/bin/env python3
"""Deterministic, stdlib-only RAG retrieval evaluator.

This evaluator consumes a gold corpus and one or more prediction bundles
(baseline, candidate) and produces per-case and aggregate metrics with
explicit failure gates. It never executes the retrieval system under
test, never infers freshness or semantic truth, and never certifies the
content of a citation beyond its structural validity and binding to a
declared corpus source.

Hard contracts:

- Answer correctness is tracked SEPARATELY from citation precision/recall.
- Critical failures (forbidden-project citation, false merge, unsupported
  critical answer, structurally invalid citation) cannot be averaged away.
- Latency / token / cost remain UNKNOWN unless explicitly provided; the
  evaluator never invents them.
- Timestamps are accepted only when zoned or marked ``unknown``; no
  freshness is inferred.
- The evaluator is read-only with respect to the Hub and any retrieval
  backend. It never writes to a shared store, never grants authority,
  and never performs dedup or canonical writes.

No third-party packages are used.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# Defensive: never write .pyc next to imports of this module.
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
try:
    sys.dont_write_bytecode  # type: ignore[attr-defined]
    sys.dont_write_bytecode = True  # type: ignore[attr-defined]
except Exception:  # pragma: no cover - extremely defensive
    pass


VERSION = "0.1.0"
SCHEMA_VERSION = "1.0.0"
SKILL_ROOT = Path(__file__).resolve().parents[1]

# ---------- Enum sets (kept inline so the file stays self-contained) ----------

SOURCE_ID_RE = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
CASE_ID_RE = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
ALIAS_GROUP_RE = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
MODEL_ID_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9._-]{1,63}$")
PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
REVISION_RE = re.compile(r"^[A-Za-z0-9._+-]{1,64}$")
ISO_TS_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(Z|[+-]\d{2}:\d{2})$"
)
UNMARKED_TS_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?$"
)
ALLOWED_ZONES = {"UTC", "unknown"}

TEMPORAL_INTENTS = {"current", "historical", "any"}
ANSWER_MATCH_POLICIES = {"exact", "case_insensitive", "contains", "abstain"}
ADVERSARIAL_KINDS = {
    "current_vs_historical",
    "superseded_source",
    "alias_collision",
    "unknown_answer",
    "conflicting_evidence",
    "forbidden_project",
    "duplicate_advisory",
    "citation_revision",
    "abstention_positive",
    "baseline_correctness",
}
MODEL_KINDS = {"baseline", "candidate"}
PASS_FAIL = {"PASS", "FAIL"}
PASS_FAIL_NA = {"PASS", "FAIL", "NOT_APPLICABLE"}
LICENSES = {
    "public-domain",
    "CC-BY-4.0",
    "CC0-1.0",
    "MIT",
    "Apache-2.0",
    "BSD-2-Clause",
    "BSD-3-Clause",
}


# ---------- Helpers ----------

def utc_now() -> str:
    """Return an ISO-8601 UTC timestamp with millisecond precision and ``Z``."""

    now = datetime.now(timezone.utc)
    now = now.replace(microsecond=(now.microsecond // 1000) * 1000)
    return now.isoformat().replace("+00:00", "Z")


def fail(message: str) -> None:
    """Raise a clean ``SystemExit`` carrying a single-line failure message."""

    sys.stderr.write(f"evaluate_retrieval: error: {message}\n")
    raise SystemExit(2)


def read_json(path: Path | str) -> Any:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        fail(f"file not found: {p}")
    except OSError as exc:
        fail(f"cannot read {p}: {exc}")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        fail(f"invalid JSON in {p}: {exc.msg} (line {exc.lineno} col {exc.colno})")
    return None


def write_json(path: Path | str, payload: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def must_match(value: Any, allowed: Iterable[Any], field: str) -> None:
    if value not in allowed:
        allowed_repr = ", ".join(sorted(str(x) for x in allowed))
        fail(f"{field}={value!r} not in {{{allowed_repr}}}")


def must_regex(value: str, regex: re.Pattern, field: str) -> None:
    if not regex.match(value):
        fail(f"{field}={value!r} does not match {regex.pattern}")


def is_zoned_or_unknown_timestamp(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    if ISO_TS_RE.match(value):
        return True
    if value == "unknown":
        return True
    return False


def normalize_for_match(value: str, policy: str) -> str:
    if policy == "exact":
        return value
    if policy in ("case_insensitive", "contains"):
        return value.lower()
    return value


MERGE_PATTERNS = (
    re.compile(r"\b(?:is|are|was|were|as|being|treat(?:ed|s|ing)?(?:\s+as)?)\s+(?:the\s+)?same\s+entity\b", re.IGNORECASE),
    re.compile(r"\bthe\s+same\s+entity\b", re.IGNORECASE),
    re.compile(r"\bsame\s+entity\b", re.IGNORECASE),
    re.compile(r"\bone\s+and\s+the\s+same\b", re.IGNORECASE),
    re.compile(r"\bmerged\s+(?:into\s+)?(?:one|a\s+single)\b", re.IGNORECASE),
    re.compile(r"\bmerged\s+into\s+one\s+record\b", re.IGNORECASE),
    re.compile(r"\bmerged\s+(?:as|into)\s+(?:a\s+)?single\s+entity\b", re.IGNORECASE),
    re.compile(r"\b(?:single|identical)\s+entity\b", re.IGNORECASE),
    re.compile(r"\b(?:is|are)\s+the\s+same\s+(?:record|item|person|product)\b", re.IGNORECASE),
)

NEGATION_PREFIX = re.compile(
    r"\b(?:not|never|no|neither|cannot|can't|must\s+not|should\s+not|do\s+not|does\s+not|did\s+not|are\s+not|is\s+not|distinct|separate|different)\b(?:\s+\w+){0,3}\s*$",
    re.IGNORECASE,
)


def check_false_merge(text: str) -> bool:
    """Return True if text asserts an unnegated merge between distinct entities."""
    for pattern in MERGE_PATTERNS:
        for match in pattern.finditer(text):
            start = match.start()
            prefix = text[:start]
            last_boundary = max(
                prefix.rfind("."),
                prefix.rfind(";"),
                prefix.rfind("!"),
                prefix.rfind("?"),
                prefix.rfind("\n"),
            )
            if last_boundary != -1:
                prefix = prefix[last_boundary + 1:]
            if not NEGATION_PREFIX.search(prefix):
                return True
    return False


DUPLICATE_PATTERNS = (
    re.compile(r"\b(?:duplicate|duplicated|duplicates|duplication|redundant|redundancy)\b", re.IGNORECASE),
)

DUPLICATE_NEGATION_PREFIX = re.compile(
    r"\b(?:not|never|no|neither|cannot|can't|must\s+not|should\s+not|do\s+not|does\s+not|did\s+not|without|fail(?:ed|s|ing)?(?:\s+to)?|lacks?|lacking|omits?|omitted|omitting)\b(?:\s+\w+){0,4}\s*$",
    re.IGNORECASE,
)


def check_duplicate_disclosure(text: str) -> bool:
    """Return True if text asserts an unnegated disclosure of duplication."""
    for pattern in DUPLICATE_PATTERNS:
        for match in pattern.finditer(text):
            start = match.start()
            prefix = text[:start]
            last_boundary = max(
                prefix.rfind("."),
                prefix.rfind(";"),
                prefix.rfind("!"),
                prefix.rfind("?"),
                prefix.rfind("\n"),
            )
            if last_boundary != -1:
                prefix = prefix[last_boundary + 1:]
            if not DUPLICATE_NEGATION_PREFIX.search(prefix):
                return True
    return False


# ---------- Corpus validation ----------

def validate_corpus(corpus: Any) -> dict:
    """Validate a gold corpus document and return a normalized view."""

    if not isinstance(corpus, dict):
        fail("corpus must be a JSON object")

    schema_version = corpus.get("schema_version")
    must_match(schema_version, {SCHEMA_VERSION}, "corpus.schema_version")

    corpus_id = corpus.get("corpus_id")
    if not isinstance(corpus_id, str) or not corpus_id:
        fail("corpus.corpus_id must be a non-empty string")

    license_ = corpus.get("license")
    if license_ not in LICENSES:
        fail(f"corpus.license must be one of {sorted(LICENSES)}")

    manifest = corpus.get("manifest")
    if not isinstance(manifest, list) or not manifest:
        fail("corpus.manifest must be a non-empty list")

    seen_sources: dict[str, dict] = {}
    for idx, source in enumerate(manifest):
        if not isinstance(source, dict):
            fail(f"corpus.manifest[{idx}] must be an object")
        source_id = source.get("source_id")
        if not isinstance(source_id, str):
            fail(f"corpus.manifest[{idx}].source_id missing")
        must_regex(source_id, SOURCE_ID_RE, f"corpus.manifest[{idx}].source_id")
        if source_id in seen_sources:
            fail(f"duplicate source_id in corpus.manifest: {source_id!r}")
        seen_sources[source_id] = source

        project_id = source.get("project_id")
        if not isinstance(project_id, str):
            fail(f"corpus.manifest[{idx}].project_id missing")
        must_regex(project_id, PROJECT_ID_RE, f"corpus.manifest[{idx}].project_id")

        revision = source.get("revision")
        if not isinstance(revision, str):
            fail(f"corpus.manifest[{idx}].revision missing")
        must_regex(revision, REVISION_RE, f"corpus.manifest[{idx}].revision")

        valid_from = source.get("valid_from")
        if valid_from is not None:
            if not is_zoned_or_unknown_timestamp(valid_from):
                fail(
                    f"corpus.manifest[{idx}].valid_from must be ISO zoned or 'unknown'"
                )

        valid_until = source.get("valid_until")
        if valid_until is not None:
            if not is_zoned_or_unknown_timestamp(valid_until):
                fail(
                    f"corpus.manifest[{idx}].valid_until must be ISO zoned or 'unknown'"
                )

        supersedes = source.get("supersedes") or []
        if not isinstance(supersedes, list):
            fail(f"corpus.manifest[{idx}].supersedes must be a list")
        for ref in supersedes:
            if ref not in seen_sources and ref != source_id:
                # forward refs allowed; we re-validate after the loop
                pass

        aliases = source.get("aliases") or []
        if not isinstance(aliases, list):
            fail(f"corpus.manifest[{idx}].aliases must be a list")
        for alias in aliases:
            if not isinstance(alias, str) or not alias:
                fail(
                    f"corpus.manifest[{idx}].aliases entries must be non-empty strings"
                )

    # Second pass — supersedes references must exist.
    for idx, source in enumerate(manifest):
        for ref in source.get("supersedes") or []:
            if ref == source["source_id"]:
                fail(
                    f"corpus.manifest[{idx}].supersedes references itself"
                )
            if ref not in seen_sources:
                fail(
                    f"corpus.manifest[{idx}].supersedes references unknown source {ref!r}"
                )

    alias_groups = corpus.get("alias_groups") or []
    if not isinstance(alias_groups, list):
        fail("corpus.alias_groups must be a list")
    for idx, group in enumerate(alias_groups):
        if not isinstance(group, dict):
            fail(f"corpus.alias_groups[{idx}] must be an object")
        gid = group.get("alias_group_id")
        if not isinstance(gid, str):
            fail(f"corpus.alias_groups[{idx}].alias_group_id missing")
        must_regex(gid, ALIAS_GROUP_RE, f"corpus.alias_groups[{idx}].alias_group_id")
        canonical = group.get("canonical_source_id")
        if canonical not in seen_sources:
            fail(
                f"corpus.alias_groups[{idx}].canonical_source_id references unknown source"
            )
        others = group.get("other_source_ids") or []
        if not isinstance(others, list) or not others:
            fail(
                f"corpus.alias_groups[{idx}].other_source_ids must be a non-empty list"
            )
        for ref in others:
            if ref not in seen_sources:
                fail(
                    f"corpus.alias_groups[{idx}].other_source_ids references unknown source {ref!r}"
                )
            if ref == canonical:
                fail(
                    f"corpus.alias_groups[{idx}] must not list canonical source as 'other'"
                )

    forbidden_sources = corpus.get("forbidden_sources") or []
    if not isinstance(forbidden_sources, list):
        fail("corpus.forbidden_sources must be a list")
    for idx, entry in enumerate(forbidden_sources):
        if not isinstance(entry, dict):
            fail(f"corpus.forbidden_sources[{idx}] must be an object")
        sid = entry.get("source_id")
        if sid not in seen_sources:
            fail(
                f"corpus.forbidden_sources[{idx}].source_id references unknown source {sid!r}"
            )
        if entry.get("never_cite") is not True:
            fail(
                f"corpus.forbidden_sources[{idx}].never_cite must be true"
            )

    cases = corpus.get("cases")
    if not isinstance(cases, list) or not cases:
        fail("corpus.cases must be a non-empty list")
    seen_cases: set[str] = set()
    for idx, case in enumerate(cases):
        if not isinstance(case, dict):
            fail(f"corpus.cases[{idx}] must be an object")
        cid = case.get("case_id")
        if not isinstance(cid, str):
            fail(f"corpus.cases[{idx}].case_id missing")
        must_regex(cid, CASE_ID_RE, f"corpus.cases[{idx}].case_id")
        if cid in seen_cases:
            fail(f"duplicate case_id: {cid!r}")
        seen_cases.add(cid)

        if "question" not in case or not isinstance(case["question"], str):
            fail(f"corpus.cases[{idx}].question missing or not a string")

        ti = case.get("temporal_intent")
        must_match(ti, TEMPORAL_INTENTS, f"corpus.cases[{idx}].temporal_intent")

        ak = case.get("adversarial_kind")
        must_match(ak, ADVERSARIAL_KINDS, f"corpus.cases[{idx}].adversarial_kind")

        expected = case.get("expected")
        if not isinstance(expected, dict):
            fail(f"corpus.cases[{idx}].expected must be an object")

        policy = expected.get("answer_match_policy", "case_insensitive")
        must_match(
            policy,
            ANSWER_MATCH_POLICIES,
            f"corpus.cases[{idx}].expected.answer_match_policy",
        )

        if policy == "abstain":
            if expected.get("abstain_expected") is not True:
                fail(
                    f"corpus.cases[{idx}] answer_match_policy=abstain requires "
                    f"expected.abstain_expected=true"
                )
            if expected.get("answer") is not None:
                fail(
                    f"corpus.cases[{idx}] answer_match_policy=abstain requires "
                    f"expected.answer=null"
                )

        abst = expected.get("abstain_expected")
        if not isinstance(abst, bool):
            fail(
                f"corpus.cases[{idx}].expected.abstain_expected must be boolean"
            )

        if abst:
            if policy != "abstain":
                fail(
                    f"corpus.cases[{idx}] expected.abstain_expected=true requires "
                    f"answer_match_policy='abstain'"
                )
            if expected.get("answer") is not None:
                fail(
                    f"corpus.cases[{idx}] expected.abstain_expected=true requires "
                    f"expected.answer=null"
                )
        else:
            if policy == "abstain":
                fail(
                    f"corpus.cases[{idx}] expected.abstain_expected=false requires "
                    f"answer_match_policy!='abstain'"
                )
            ans = expected.get("answer")
            if not isinstance(ans, str) or not ans.strip():
                fail(
                    f"corpus.cases[{idx}] expected.abstain_expected=false requires "
                    f"expected.answer to be a non-empty string"
                )

        required = expected.get("required_sources") or []
        if not isinstance(required, list):
            fail(
                f"corpus.cases[{idx}].expected.required_sources must be a list"
            )
        for ref in required:
            if ref not in seen_sources:
                fail(
                    f"corpus.cases[{idx}].expected.required_sources references unknown source {ref!r}"
                )

        allowed_revs = expected.get("allowed_revisions")
        if allowed_revs is not None:
            if not isinstance(allowed_revs, list) or not allowed_revs:
                fail(
                    f"corpus.cases[{idx}].expected.allowed_revisions must be a non-empty list or null"
                )
            for r in allowed_revs:
                if not isinstance(r, str):
                    fail(
                        f"corpus.cases[{idx}].expected.allowed_revisions entries must be strings"
                    )

        forb = expected.get("forbidden_sources") or []
        if not isinstance(forb, list):
            fail(
                f"corpus.cases[{idx}].expected.forbidden_sources must be a list"
            )
        for ref in forb:
            if ref not in seen_sources:
                fail(
                    f"corpus.cases[{idx}].expected.forbidden_sources references unknown source {ref!r}"
                )

        alias_group = expected.get("must_disambiguate_alias_group")
        if alias_group is not None:
            if not isinstance(alias_group, str):
                fail(
                    f"corpus.cases[{idx}].expected.must_disambiguate_alias_group must be a string"
                )
            valid_groups = {g["alias_group_id"] for g in alias_groups}
            if alias_group not in valid_groups:
                fail(
                    f"corpus.cases[{idx}].expected.must_disambiguate_alias_group references unknown group"
                )

    return {
        "corpus_id": corpus_id,
        "license": license_,
        "manifest": seen_sources,
        "alias_groups": alias_groups,
        "forbidden_sources": {e["source_id"] for e in forbidden_sources},
        "cases": cases,
    }


# ---------- Predictions validation ----------

def validate_predictions(payload: Any) -> dict:
    if not isinstance(payload, dict):
        fail("predictions must be a JSON object")

    schema_version = payload.get("schema_version")
    must_match(schema_version, {SCHEMA_VERSION}, "predictions.schema_version")

    model_id = payload.get("model_id")
    if not isinstance(model_id, str) or not model_id:
        fail("predictions.model_id must be a non-empty string")
    must_regex(model_id, MODEL_ID_RE, "predictions.model_id")

    model_kind = payload.get("model_kind", "candidate")
    must_match(model_kind, MODEL_KINDS, "predictions.model_kind")

    predictions = payload.get("predictions")
    if not isinstance(predictions, list):
        fail("predictions.predictions must be a list")

    by_case: dict[str, dict] = {}
    for idx, pred in enumerate(predictions):
        if not isinstance(pred, dict):
            fail(f"predictions.predictions[{idx}] must be an object")
        cid = pred.get("case_id")
        if not isinstance(cid, str):
            fail(f"predictions.predictions[{idx}].case_id missing")
        must_regex(cid, CASE_ID_RE, f"predictions.predictions[{idx}].case_id")
        if cid in by_case:
            fail(f"predictions: duplicate case_id {cid!r}")
        by_case[cid] = pred

        if "answer" not in pred:
            fail(
                f"predictions.predictions[{idx}].answer must be present (use null for abstention)"
            )
        if pred["answer"] is not None and not isinstance(pred["answer"], str):
            fail(
                f"predictions.predictions[{idx}].answer must be string or null"
            )

        abst = pred.get("abstained")
        if not isinstance(abst, bool):
            fail(
                f"predictions.predictions[{idx}].abstained must be boolean"
            )

        if abst and pred.get("answer") not in (None, ""):
            fail(
                f"predictions.predictions[{idx}]: abstained=true requires answer=null or empty string"
            )

        citations = pred.get("citations") or []
        if not isinstance(citations, list):
            fail(
                f"predictions.predictions[{idx}].citations must be a list"
            )
        for jdx, cit in enumerate(citations):
            if not isinstance(cit, dict):
                fail(
                    f"predictions.predictions[{idx}].citations[{jdx}] must be an object"
                )
            for field in ("source_id", "revision", "quote"):
                if not isinstance(cit.get(field), str) or not cit[field]:
                    fail(
                        f"predictions.predictions[{idx}].citations[{jdx}].{field} missing"
                    )
            must_regex(
                cit["source_id"],
                SOURCE_ID_RE,
                f"predictions.predictions[{idx}].citations[{jdx}].source_id",
            )
            must_regex(
                cit["revision"],
                REVISION_RE,
                f"predictions.predictions[{idx}].citations[{jdx}].revision",
            )
            page = cit.get("page")
            if page is not None and not isinstance(page, int):
                fail(
                    f"predictions.predictions[{idx}].citations[{jdx}].page must be int or null"
                )
            confidence = cit.get("confidence")
            if confidence is not None:
                if not isinstance(confidence, (int, float)):
                    fail(
                        f"predictions.predictions[{idx}].citations[{jdx}].confidence must be number or null"
                    )
                if not (0.0 <= float(confidence) <= 1.0):
                    fail(
                        f"predictions.predictions[{idx}].citations[{jdx}].confidence out of [0,1]"
                    )
            zone = cit.get("timestamp_zone", "unknown")
            if zone not in ALLOWED_ZONES:
                fail(
                    f"predictions.predictions[{idx}].citations[{jdx}].timestamp_zone must be one of {sorted(ALLOWED_ZONES)}"
                )
            retrieved = cit.get("retrieved_at")
            if retrieved is not None and retrieved != "unknown":
                if not ISO_TS_RE.match(retrieved):
                    fail(
                        f"predictions.predictions[{idx}].citations[{jdx}].retrieved_at must be ISO zoned or 'unknown'"
                    )

    return {
        "model_id": model_id,
        "model_kind": model_kind,
        "predictions": by_case,
    }


# ---------- Evaluation ----------

def evaluate_case(case: dict, prediction: dict, corpus_view: dict) -> dict:
    expected = case["expected"]
    case_id = case["case_id"]
    errors: list[str] = []
    critical_reasons: list[str] = []

    policy = expected.get("answer_match_policy", "case_insensitive")
    gold_answer = expected.get("answer")
    pred_answer = prediction.get("answer")
    pred_abstained = prediction.get("abstained", False)

    # --- answer correctness
    if expected.get("abstain_expected") or policy == "abstain":
        answer_correctness = "PASS" if pred_abstained else "FAIL"
        if answer_correctness == "FAIL":
            errors.append(
                "expected abstention but prediction provided an answer"
            )
            critical_reasons.append("unsupported_critical_answer")
    else:
        if pred_abstained:
            answer_correctness = "FAIL"
            errors.append(
                "unexpected abstention on a question requiring an answer"
            )
            critical_reasons.append("unsupported_critical_answer")
        elif not isinstance(gold_answer, str) or not gold_answer:
            answer_correctness = "FAIL"
            errors.append("gold answer missing or empty; cannot score correctness")
            critical_reasons.append("unsupported_critical_answer")
        elif not isinstance(pred_answer, str):
            answer_correctness = "FAIL"
            errors.append("prediction answer is not a string")
            critical_reasons.append("unsupported_critical_answer")
        else:
            gold_norm = normalize_for_match(gold_answer, policy)
            pred_norm = normalize_for_match(pred_answer, policy)
            if policy in ("exact", "case_insensitive"):
                ok = gold_norm == pred_norm
            elif policy == "contains":
                ok = gold_norm in pred_norm
            else:  # pragma: no cover — guarded upstream
                ok = False
            answer_correctness = "PASS" if ok else "FAIL"
            if not ok:
                critical_reasons.append("unsupported_critical_answer")

    # --- abstention correctness
    if expected.get("abstain_expected"):
        abstention_correctness = "PASS" if pred_abstained else "FAIL"
        if abstention_correctness == "FAIL":
            errors.append("did not abstain on unknown question")
    else:
        abstention_correctness = (
            "PASS" if not pred_abstained else "FAIL"
        )

    # --- citation precision/recall
    required = set(expected.get("required_sources") or [])
    citations = prediction.get("citations") or []
    cited_sources: list[str] = []
    citation_format_valid = "PASS"
    for idx, citation in enumerate(citations):
        # Validate the citation binds to a known corpus source.
        sid = citation["source_id"]
        if sid not in corpus_view["manifest"]:
            errors.append(
                f"citation[{idx}].source_id {sid!r} not declared in corpus.manifest"
            )
            citation_format_valid = "FAIL"
            critical_reasons.append("invalid_citation")
        else:
            cited_sources.append(sid)
        # Confirm revision matches at least one of the matching sources' revisions.
        if sid in corpus_view["manifest"]:
            declared_rev = corpus_view["manifest"][sid]["revision"]
            if citation["revision"] != declared_rev:
                errors.append(
                    f"citation[{idx}].revision {citation['revision']!r} does not match corpus declared revision {declared_rev!r}"
                )
                citation_format_valid = "FAIL"
                critical_reasons.append("invalid_citation")

    cited_set = set(cited_sources)
    if required:
        true_positives = cited_set & required
        precision = (
            len(true_positives) / len(cited_set) if cited_set else 0.0
        )
        recall = len(true_positives) / len(required)
    else:
        precision = 1.0 if not cited_set else 0.0
        recall = 1.0 if not cited_set else 0.0

    # --- temporal revision correctness
    allowed_revs = expected.get("allowed_revisions")
    if allowed_revs:
        temporal_revision_correctness = "PASS"
        for idx, citation in enumerate(citations):
            sid = citation["source_id"]
            if sid not in corpus_view["manifest"]:
                continue
            declared_rev = corpus_view["manifest"][sid]["revision"]
            if declared_rev not in allowed_revs:
                errors.append(
                    f"citation[{idx}] uses revision {declared_rev!r} not in allowed_revisions"
                )
                temporal_revision_correctness = "FAIL"
                critical_reasons.append("invalid_citation")
    else:
        temporal_revision_correctness = "NOT_APPLICABLE"

    # --- project isolation (forbidden sources + corpus-level forbidden)
    case_forbidden = set(expected.get("forbidden_sources") or [])
    corpus_forbidden = corpus_view["forbidden_sources"]
    all_forbidden = case_forbidden | corpus_forbidden
    project_isolation = "PASS"
    for idx, citation in enumerate(citations):
        sid = citation["source_id"]
        if sid in all_forbidden:
            errors.append(
                f"citation[{idx}] cites forbidden source {sid!r}"
            )
            project_isolation = "FAIL"
            critical_reasons.append("forbidden_project_disclosure")

    # --- alias disambiguation (no false merge)
    must_disambiguate = expected.get("must_disambiguate_alias_group")
    if must_disambiguate:
        group = next(
            g for g in corpus_view["alias_groups"]
            if g["alias_group_id"] == must_disambiguate
        )
        rationale = (prediction.get("rationale") or "")
        answer_text = (
            pred_answer if isinstance(pred_answer, str) else ""
        )
        combined_text = f"{answer_text} {rationale}".strip()
        merge_declared = check_false_merge(combined_text)

        if merge_declared:
            alias_resolution = "FAIL"
            no_false_merge = "FAIL"
            errors.append(
                f"prediction claims alias group {must_disambiguate!r} is a single entity"
            )
            critical_reasons.append("false_merge")
        else:
            alias_resolution = "PASS"
            no_false_merge = "PASS"
    else:
        alias_resolution = "NOT_APPLICABLE"
        no_false_merge = "NOT_APPLICABLE"

    # --- duplicate advisory (duplicate evidence deduped)
    # We require that the prediction does not claim a duplicate proposal
    # is unique without flagging the redundancy; we look for an explicit
    # unnegated duplicate marker when the case demands it. Explicit
    # non-disclosure (e.g. 'without disclosing duplication' or 'no duplicate
    # was flagged') is scored as failure to disclose.
    if case.get("adversarial_kind") == "duplicate_advisory":
        rationale = prediction.get("rationale") or ""
        answer_text = pred_answer if isinstance(pred_answer, str) else ""
        combined_text = f"{answer_text} {rationale}".strip()
        if not check_duplicate_disclosure(combined_text):
            errors.append(
                "duplicate_advisory case requires rationale to disclose the duplication"
            )
            # Duplicate advisory disclosure is tracked but is not a
            # critical failure on its own; it can be aggregated later.
        # no critical_reasons mutation here

    critical_failure = bool(critical_reasons)

    return {
        "case_id": case_id,
        "present_in_predictions": True,
        "answer_correctness": answer_correctness,
        "abstention_correctness": abstention_correctness,
        "citation_precision": round(precision, 4),
        "citation_recall": round(recall, 4),
        "temporal_revision_correctness": temporal_revision_correctness,
        "project_isolation": project_isolation,
        "alias_resolution": alias_resolution,
        "no_false_merge": no_false_merge,
        "citation_format_valid": citation_format_valid,
        "critical_failure": critical_failure,
        "critical_reasons": critical_reasons,
        "errors": errors,
    }


def aggregate(by_case: list[dict], total_cases: int) -> dict:
    """Aggregate per-case verdicts into counts and rates."""

    def count_pass(verdicts: list[str], target: str) -> tuple[int, int]:
        scored = [v for v in verdicts if v != "NOT_APPLICABLE"]
        passed = sum(1 for v in scored if v == target)
        return passed, len(scored)

    answer_pass, answer_total = count_pass(
        [c["answer_correctness"] for c in by_case], "PASS"
    )
    abst_pass, abst_total = count_pass(
        [c["abstention_correctness"] for c in by_case], "PASS"
    )
    temp_pass, temp_total = count_pass(
        [c["temporal_revision_correctness"] for c in by_case], "PASS"
    )
    iso_pass, iso_total = count_pass(
        [c["project_isolation"] for c in by_case], "PASS"
    )
    alias_pass, alias_total = count_pass(
        [c["alias_resolution"] for c in by_case], "PASS"
    )
    merge_pass, merge_total = count_pass(
        [c["no_false_merge"] for c in by_case], "PASS"
    )
    fmt_pass, fmt_total = count_pass(
        [c["citation_format_valid"] for c in by_case], "PASS"
    )

    precisions = [c["citation_precision"] for c in by_case if c["citation_precision"] is not None]
    recalls = [c["citation_recall"] for c in by_case if c["citation_recall"] is not None]
    avg_precision = round(sum(precisions) / len(precisions), 4) if precisions else 0.0
    avg_recall = round(sum(recalls) / len(recalls), 4) if recalls else 0.0

    critical_cases = [c["case_id"] for c in by_case if c["critical_failure"]]
    critical_failures = len(critical_cases)

    return {
        "cases": total_cases,
        "predicted_cases": sum(1 for c in by_case if c.get("present_in_predictions")),
        "answer_correctness_rate": (
            round(answer_pass / answer_total, 4) if answer_total else 0.0
        ),
        "answer_correctness_scored": answer_total,
        "abstention_correctness_rate": (
            round(abst_pass / abst_total, 4) if abst_total else 0.0
        ),
        "abstention_correctness_scored": abst_total,
        "citation_precision": avg_precision,
        "citation_recall": avg_recall,
        "temporal_revision_correctness_rate": (
            round(temp_pass / temp_total, 4) if temp_total else 1.0
        ),
        "temporal_revision_correctness_scored": temp_total,
        "project_isolation_pass_rate": (
            round(iso_pass / iso_total, 4) if iso_total else 1.0
        ),
        "project_isolation_scored": iso_total,
        "alias_resolution_pass_rate": (
            round(alias_pass / alias_total, 4) if alias_total else 1.0
        ),
        "alias_resolution_scored": alias_total,
        "no_false_merge_pass_rate": (
            round(merge_pass / merge_total, 4) if merge_total else 1.0
        ),
        "no_false_merge_scored": merge_total,
        "citation_format_valid_rate": (
            round(fmt_pass / fmt_total, 4) if fmt_total else 1.0
        ),
        "citation_format_valid_scored": fmt_total,
        "critical_failures": critical_failures,
        "critical_failure_cases": critical_cases,
    }


def evaluate_bundle(corpus_path: Path, predictions_path: Path) -> dict:
    corpus = read_json(corpus_path)
    predictions = read_json(predictions_path)
    corpus_view = validate_corpus(corpus)
    pred_view = validate_predictions(predictions)

    by_case: list[dict] = []
    gold_by_id = {c["case_id"]: c for c in corpus_view["cases"]}
    for case in corpus_view["cases"]:
        cid = case["case_id"]
        if cid in pred_view["predictions"]:
            by_case.append(
                evaluate_case(case, pred_view["predictions"][cid], corpus_view)
            )
        else:
            by_case.append(
                {
                    "case_id": cid,
                    "present_in_predictions": False,
                    "answer_correctness": "FAIL",
                    "abstention_correctness": "NOT_APPLICABLE",
                    "citation_precision": 0.0,
                    "citation_recall": 0.0,
                    "temporal_revision_correctness": "NOT_APPLICABLE",
                    "project_isolation": "PASS",
                    "alias_resolution": "NOT_APPLICABLE",
                    "no_false_merge": "NOT_APPLICABLE",
                    "citation_format_valid": "NOT_APPLICABLE",
                    "critical_failure": True,
                    "critical_reasons": ["missing_prediction"],
                    "errors": [
                        f"no prediction provided for case_id {cid!r}"
                    ],
                }
            )

    totals = aggregate(by_case, total_cases=len(corpus_view["cases"]))

    failure_gates: list[str] = []
    if totals["critical_failures"] > 0:
        failure_gates.append("critical_failures_present")
    if totals["citation_format_valid_rate"] < 1.0:
        failure_gates.append("citation_format_invalid")
    # Project isolation MUST be 100% for cases that declared any forbidden source.
    isolation_cases = [
        c for c in by_case
        if c["project_isolation"] != "NOT_APPLICABLE"
    ]
    if any(c["project_isolation"] == "FAIL" for c in isolation_cases):
        failure_gates.append("forbidden_project_disclosure")
    # False merges MUST not occur for cases that demanded disambiguation.
    merge_cases = [
        c for c in by_case if c["no_false_merge"] != "NOT_APPLICABLE"
    ]
    if any(c["no_false_merge"] == "FAIL" for c in merge_cases):
        failure_gates.append("false_merge_detected")
    # Answer correctness on critical (non-abstain) cases must be PASS for ALL.
    critical_answers = [
        c for c in by_case
        if c["answer_correctness"] == "FAIL"
        and "unsupported_critical_answer" in c["critical_reasons"]
    ]
    if critical_answers:
        failure_gates.append("unsupported_critical_answer")

    overall_status = "PASS" if not failure_gates else "FAIL"

    return {
        "schema_version": SCHEMA_VERSION,
        "report_kind": "single-model",
        "evaluator_version": VERSION,
        "corpus_id": corpus_view["corpus_id"],
        "model_id": pred_view["model_id"],
        "model_kind": pred_view["model_kind"],
        "generated_at": utc_now(),
        "totals": totals,
        "overall_status": overall_status,
        "failure_gates": failure_gates,
        "latency_observed": "UNKNOWN",
        "token_observed": "UNKNOWN",
        "cost_observed": "UNKNOWN",
        "freshness_inferred": False,
        "by_case": by_case,
    }


def compare(
    baseline_path: Path, candidate_path: Path, report_dir: Path
) -> dict:
    baseline = read_json(baseline_path)
    candidate = read_json(candidate_path)
    if not isinstance(baseline, dict) or "totals" not in baseline:
        fail(f"{baseline_path} is not a baseline report JSON")
    if not isinstance(candidate, dict) or "totals" not in candidate:
        fail(f"{candidate_path} is not a candidate report JSON")

    delta: dict[str, Any] = {}
    diff_metrics = [
        "answer_correctness_rate",
        "abstention_correctness_rate",
        "citation_precision",
        "citation_recall",
        "temporal_revision_correctness_rate",
        "project_isolation_pass_rate",
        "alias_resolution_pass_rate",
        "no_false_merge_pass_rate",
        "citation_format_valid_rate",
    ]
    for metric in diff_metrics:
        b = baseline["totals"].get(metric, 0.0)
        c = candidate["totals"].get(metric, 0.0)
        delta[metric] = round(c - b, 4)

    b_crit = set(baseline["totals"].get("critical_failure_cases", []))
    c_crit = set(candidate["totals"].get("critical_failure_cases", []))
    delta["critical_failures_baseline"] = baseline["totals"].get("critical_failures", 0)
    delta["critical_failures_candidate"] = candidate["totals"].get("critical_failures", 0)
    delta["candidate_fixed_critical"] = sorted(b_crit - c_crit)
    delta["candidate_regressed_critical"] = sorted(c_crit - b_crit)

    if (
        baseline["overall_status"] == "PASS"
        and candidate["overall_status"] == "PASS"
        and not delta["candidate_regressed_critical"]
    ):
        decision = "promote (no critical regressions, candidate keeps all gates green)"
    elif (
        candidate["overall_status"] == "PASS"
        and not delta["candidate_regressed_critical"]
        and any(v > 0 for v in (delta[m] for m in diff_metrics))
    ):
        decision = (
            "promote (candidate cleared all critical gates; averages improved "
            "but evidence is reported without further claim)"
        )
    elif candidate["overall_status"] == "FAIL":
        decision = "do not promote (candidate fails critical gates)"
    elif delta["candidate_regressed_critical"]:
        decision = (
            "do not promote (candidate regressed on critical cases previously passing)"
        )
    else:
        decision = (
            "insufficient evidence (averages alone cannot justify promotion; "
            "no improvement is claimed)"
        )

    comparison = {
        "schema_version": SCHEMA_VERSION,
        "report_kind": "baseline-vs-candidate",
        "evaluator_version": VERSION,
        "generated_at": utc_now(),
        "baseline": {
            "model_id": baseline["model_id"],
            "model_kind": "baseline",
            "overall_status": baseline["overall_status"],
            "failure_gates": baseline["failure_gates"],
            "totals": baseline["totals"],
            "report_path": str(report_dir / "baseline.report.json"),
        },
        "candidate": {
            "model_id": candidate["model_id"],
            "model_kind": "candidate",
            "overall_status": candidate["overall_status"],
            "failure_gates": candidate["failure_gates"],
            "totals": candidate["totals"],
            "report_path": str(report_dir / "candidate.report.json"),
        },
        "delta": delta,
        "decision": decision,
        "improvement_claim": (
            "NOT_MADE — averages alone do not justify a claim of improvement; "
            "the decision reflects critical-gate evidence only."
        ),
        "notes": (
            "Latency, token usage and cost remain UNKNOWN in this evaluator. "
            "No freshness, semantic truth, or authority has been inferred."
        ),
    }
    return comparison


# ---------- CLI ----------

def cmd_validate_corpus(args: argparse.Namespace) -> None:
    corpus = read_json(Path(args.corpus))
    validate_corpus(corpus)
    sys.stdout.write(
        f"corpus {args.corpus_id or '(unnamed)'} validates against schema {SCHEMA_VERSION}\n"
    )


def cmd_evaluate(args: argparse.Namespace) -> None:
    report = evaluate_bundle(Path(args.corpus), Path(args.predictions))
    out = Path(args.output)
    write_json(out, report)
    sys.stdout.write(
        f"wrote {out} (status={report['overall_status']}, "
        f"critical_failures={report['totals']['critical_failures']})\n"
    )
    if report["overall_status"] == "FAIL":
        # Non-zero exit so that pipelines catch critical regressions, but
        # the JSON report is still produced.
        raise SystemExit(1)


def cmd_compare(args: argparse.Namespace) -> None:
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    baseline_report = evaluate_bundle(Path(args.corpus), Path(args.baseline))
    candidate_report = evaluate_bundle(Path(args.corpus), Path(args.candidate))
    write_json(out_dir / "baseline.report.json", baseline_report)
    write_json(out_dir / "candidate.report.json", candidate_report)
    comparison = compare(
        Path(out_dir / "baseline.report.json"),
        Path(out_dir / "candidate.report.json"),
        out_dir,
    )
    write_json(out_dir / "comparison.json", comparison)
    sys.stdout.write(
        f"wrote {out_dir}/baseline.report.json, candidate.report.json, comparison.json\n"
    )
    sys.stdout.write(f"decision: {comparison['decision']}\n")


def cmd_selftest(args: argparse.Namespace) -> None:
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    fixtures_dir = SKILL_ROOT / "fixtures"
    corpus_path = Path(args.corpus) if args.corpus else (fixtures_dir / "gold-corpus.json")
    baseline_path = Path(args.baseline) if args.baseline else (fixtures_dir / "baseline-predictions.json")
    candidate_path = Path(args.candidate) if args.candidate else (fixtures_dir / "candidate-predictions.json")

    if not corpus_path.exists():
        fail(f"gold corpus not found: {corpus_path}")
    if not baseline_path.exists():
        fail(f"baseline predictions not found: {baseline_path}")
    if not candidate_path.exists():
        fail(f"candidate predictions not found: {candidate_path}")

    corpus = read_json(corpus_path)
    validate_corpus(corpus)

    baseline_report = evaluate_bundle(corpus_path, baseline_path)
    candidate_report = evaluate_bundle(corpus_path, candidate_path)

    baseline_out = out_dir / "baseline.report.json"
    candidate_out = out_dir / "candidate.report.json"
    comparison_out = out_dir / "comparison.json"

    write_json(baseline_out, baseline_report)
    write_json(candidate_out, candidate_report)

    comparison = compare(baseline_out, candidate_out, out_dir)
    write_json(comparison_out, comparison)

    if baseline_report["overall_status"] != "FAIL":
        fail("selftest: baseline expected to fail critical gates")
    if candidate_report["overall_status"] != "PASS":
        fail("selftest: candidate expected to pass critical gates")
    if "promote" not in comparison["decision"]:
        fail("selftest: comparison decision expected to promote candidate")

    selftest_summary = {
        "schema_version": SCHEMA_VERSION,
        "evaluator_version": VERSION,
        "status": "PASS",
        "corpus_id": corpus.get("corpus_id"),
        "baseline_model_id": baseline_report.get("model_id"),
        "baseline_status": baseline_report.get("overall_status"),
        "candidate_model_id": candidate_report.get("model_id"),
        "candidate_status": candidate_report.get("overall_status"),
        "decision": comparison.get("decision"),
        "generated_at": utc_now(),
    }
    write_json(out_dir / "selftest.json", selftest_summary)

    sys.stdout.write(
        f"selftest passed: wrote reports to {out_dir}\n"
        f"baseline={baseline_report['overall_status']}, candidate={candidate_report['overall_status']}, decision={comparison['decision']}\n"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="evaluate_retrieval",
        description=(
            "Deterministic stdlib-only RAG retrieval evaluator. "
            "Evaluates baseline and/or candidate predictions against a "
            "gold corpus and produces reports with explicit failure gates."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {VERSION}"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser(
        "validate-corpus",
        help="Validate a gold corpus against the schema and exit.",
    )
    p_validate.add_argument("--corpus", required=True)
    p_validate.add_argument("--corpus-id", default=None)
    p_validate.set_defaults(func=cmd_validate_corpus)

    p_eval = sub.add_parser(
        "evaluate",
        help="Evaluate predictions against the gold corpus and write a report.",
    )
    p_eval.add_argument("--corpus", required=True)
    p_eval.add_argument("--predictions", required=True)
    p_eval.add_argument("--output", required=True)
    p_eval.set_defaults(func=cmd_evaluate)

    p_compare = sub.add_parser(
        "compare",
        help=(
            "Run deterministic baseline and candidate evaluations and emit a "
            "comparison report with explicit gates."
        ),
    )
    p_compare.add_argument("--corpus", required=True)
    p_compare.add_argument("--baseline", required=True)
    p_compare.add_argument("--candidate", required=True)
    p_compare.add_argument("--output-dir", required=True)
    p_compare.set_defaults(func=cmd_compare)

    p_selftest = sub.add_parser(
        "selftest",
        help="Run self-test on bundled gold corpus and prediction fixtures.",
    )
    p_selftest.add_argument(
        "--output-dir",
        required=True,
        help="Directory to write self-test evaluation and comparison reports.",
    )
    p_selftest.add_argument(
        "--corpus",
        default=None,
        help="Optional corpus override (defaults to bundled gold-corpus.json).",
    )
    p_selftest.add_argument(
        "--baseline",
        default=None,
        help="Optional baseline override (defaults to bundled baseline-predictions.json).",
    )
    p_selftest.add_argument(
        "--candidate",
        default=None,
        help="Optional candidate override (defaults to bundled candidate-predictions.json).",
    )
    p_selftest.set_defaults(func=cmd_selftest)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())