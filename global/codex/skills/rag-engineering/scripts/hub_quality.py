"""Thin Hub pilot preparation + strict provenance/quote validation adapter.

This module is a leaf adapter around the existing RAG evaluator
(``evaluate_retrieval.py``). It does NOT implement a second metrics engine,
answer generator, or live Hub client, and it contains no fixture answers.

Pipeline:

1. ``freeze``   - sha256-freeze the pilot inputs (materials + questions).
2. ``project``  - project materials/questions plus an external independent
                  gold file into an evaluator-compatible gold corpus, with
                  provenance (hashes, revisions) preserved in the manifest.
3. ``evaluate`` - run the existing evaluator for answer/citation metrics and
                  add an actual-source quote layer: every citation quote must
                  occur verbatim (whitespace-normalized) in a real fact of the
                  cited source, with traps for stale/superseded, duplicate,
                  conflicting, and cross-project citations.
4. ``compare``  - offline A/B comparison of two raw native predictions runs.

The gold file (expected answers, required sources, adversarial kinds) is an
EXTERNAL input supplied by the parent, never authored here. Raw native
predictions are supplied by the parent; this adapter never generates answers.

Semantic limits (honest): quote verification is a verbatim-substring check
after whitespace normalization. It proves presence of the cited text in the
cited source, not semantic support, entailment, or span minimality.

Usage::

    python hub_quality.py freeze --materials <materials.json> \
        --questions <questions.json> --out freeze-manifest.json
    python hub_quality.py project --materials <materials.json> \
        --questions <questions.json> --gold <gold.json> \
        --freeze-manifest <freeze-manifest.json> --out corpus.json
    python hub_quality.py evaluate --materials <materials.json> \
        --questions <questions.json> --gold <gold.json> \
        --predictions <predictions.json> \
        --freeze-manifest <freeze-manifest.json> --out report.json
    python hub_quality.py compare --materials <materials.json> \
        --questions <questions.json> --gold <gold.json> \
        --freeze-manifest <freeze-manifest.json> \
        --baseline <baseline-predictions.json> \
        --candidate <candidate-predictions.json> --out comparison.json

Exit codes: 0 = PASS, 1 = quality FAIL (gates tripped), 2 = input error.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
from datetime import date
from pathlib import Path


HERE = Path(__file__).resolve().parent
_EVAL_SPEC = importlib.util.spec_from_file_location(
    "hub_evaluate_retrieval", HERE / "evaluate_retrieval.py"
)
assert _EVAL_SPEC and _EVAL_SPEC.loader
evaluator = importlib.util.module_from_spec(_EVAL_SPEC)
_EVAL_SPEC.loader.exec_module(evaluator)


HUB_QUALITY_VERSION = "0.1.0"
FREEZE_SCHEMA = "hub-quality-freeze-v1"
REPORT_SCHEMA = "hub-quality-report-v1"

# Minimum normalized quote length accepted by the quote layer. The evaluator
# already requires a non-empty quote; this guards against trivial one-word
# "quotes" that prove nothing about provenance.
MIN_QUOTE_CHARS = 4

# ``status`` values that never gate, but are reported for pilot triage.
_CONFLICT_STATUS_RE = re.compile(r"^contradicts(?::|\s)", re.IGNORECASE)

# Matches the fact-level supersession marker used in pilot material titles,
# e.g. ``"KSeF invoice schema (supersedes ksef-old-fact)"``.
_SUPERSEDES_RE = re.compile(r"\(\s*supersedes\s+([A-Za-z0-9._-]+)\s*\)")


class HubQualityError(Exception):
    """Input error (exit code 2)."""


def _load_json(path: str | Path, what: str) -> dict:
    p = Path(path)
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise HubQualityError(f"{what}: file not found: {p}")
    except json.JSONDecodeError as exc:
        raise HubQualityError(f"{what}: invalid JSON in {p}: {exc}")
    if not isinstance(payload, dict):
        raise HubQualityError(f"{what}: top-level JSON must be an object: {p}")
    return payload


def _load_json_with_hash(path: str | Path, what: str) -> tuple[dict, str]:
    """Read a raw JSON artifact once and bind its exact bytes to the result."""
    p = Path(path)
    try:
        raw = p.read_bytes()
    except FileNotFoundError:
        raise HubQualityError(f"{what}: file not found: {p}")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HubQualityError(f"{what}: invalid UTF-8 JSON in {p}: {exc}")
    if not isinstance(payload, dict):
        raise HubQualityError(f"{what}: top-level JSON must be an object: {p}")
    return payload, hashlib.sha256(raw).hexdigest()


def _write_json(
    path: str | Path,
    payload: dict,
    protected_paths: tuple[str | Path, ...] | list[str | Path] = (),
) -> None:
    p = Path(path)
    _guard_output_path(p, protected_paths)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        with p.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
    except FileExistsError:
        raise HubQualityError(f"output already exists; refusing overwrite: {p}")


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _normalized_path(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).resolve(strict=False)))


def _guard_output_path(path: str | Path, protected_paths: tuple[str | Path, ...] | list[str | Path]) -> None:
    """Reject aliases, links and overwrites before writing a derived artifact."""
    target = Path(os.path.abspath(os.fspath(path)))
    current = Path(target.anchor)
    for part in target.parts[1:]:
        current = current / part
        if _is_link_or_junction(current):
            raise HubQualityError(
                f"output path contains a symlink or junction; refusing write: {current}"
            )
    target_key = _normalized_path(target)
    for protected in protected_paths:
        if target_key == _normalized_path(protected):
            raise HubQualityError(
                f"output aliases protected input; refusing write: {target}"
            )
    if target.exists() or _is_link_or_junction(target):
        raise HubQualityError(f"output already exists; refusing overwrite: {target}")
    ancestor = target.parent
    while not ancestor.exists():
        if ancestor == ancestor.parent:
            raise HubQualityError(f"output parent does not exist: {target.parent}")
        ancestor = ancestor.parent
    if not ancestor.is_dir():
        raise HubQualityError(f"output parent is not a directory: {ancestor}")


def sha256_file(path: str | Path) -> str:
    """Hex sha256 of a file's raw bytes (frozen-input identity)."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_pilot_inputs(materials: dict, questions: dict) -> tuple[dict, dict]:
    """Normalize the frozen public pilot schema into the adapter's internal view."""
    expected_schema = "hub-quality-pilot-frozen-v1"
    if not isinstance(materials, dict) or materials.get("schema") != expected_schema:
        got = materials.get("schema") if isinstance(materials, dict) else None
        raise HubQualityError(
            f"materials: expected schema {expected_schema!r}, got {got!r}"
        )
    if not isinstance(questions, dict) or questions.get("schema") != expected_schema:
        got = questions.get("schema") if isinstance(questions, dict) else None
        raise HubQualityError(
            f"questions: expected schema {expected_schema!r}, got {got!r}"
        )

    raw_sources = materials.get("materials")
    raw_questions = questions.get("questions")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise HubQualityError("materials: 'materials' must be a non-empty list")
    if not isinstance(raw_questions, list) or not raw_questions:
        raise HubQualityError("questions: 'questions' must be a non-empty list")

    sources: list[dict] = []
    seen_sources: set[str] = set()
    seen_facts: set[str] = set()
    fact_id_re = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
    for position, src in enumerate(raw_sources):
        if not isinstance(src, dict):
            raise HubQualityError(f"materials[{position}]: source must be an object")
        source_id = _require_id(
            evaluator.SOURCE_ID_RE, src.get("source_id"), f"materials[{position}] source"
        )
        project_id = _require_id(
            evaluator.PROJECT_ID_RE, src.get("project"), f"source {source_id} project"
        )
        if source_id in seen_sources:
            raise HubQualityError(f"materials: duplicate source id {source_id!r}")
        seen_sources.add(source_id)
        title = src.get("title")
        if not isinstance(title, str) or not title.strip():
            raise HubQualityError(f"source {source_id!r}: title must be non-empty text")
        raw_facts = src.get("facts")
        if not isinstance(raw_facts, list) or not raw_facts:
            raise HubQualityError(f"source {source_id!r}: 'facts' must be a non-empty list")
        facts: list[dict] = []
        for fact_position, fact in enumerate(raw_facts):
            if not isinstance(fact, dict):
                raise HubQualityError(
                    f"source {source_id!r} facts[{fact_position}]: expected an object"
                )
            fact_id = fact.get("fact_id")
            if not isinstance(fact_id, str) or not fact_id_re.fullmatch(fact_id):
                raise HubQualityError(
                    f"source {source_id!r}: invalid fact_id {fact_id!r}"
                )
            if fact_id in seen_facts:
                raise HubQualityError(f"materials: duplicate fact id {fact_id!r}")
            seen_facts.add(fact_id)
            text = fact.get("text")
            observed_at = fact.get("observed_at")
            status = fact.get("status")
            if not isinstance(text, str) or not text.strip():
                raise HubQualityError(f"fact {fact_id!r}: 'text' must be non-empty text")
            if not isinstance(observed_at, str):
                raise HubQualityError(f"fact {fact_id!r}: missing 'observed_at'")
            try:
                parsed_date = date.fromisoformat(observed_at)
            except ValueError:
                raise HubQualityError(
                    f"fact {fact_id!r}: observed_at must be an ISO date (YYYY-MM-DD)"
                )
            if parsed_date.isoformat() != observed_at:
                raise HubQualityError(
                    f"fact {fact_id!r}: observed_at must use YYYY-MM-DD"
                )
            if not isinstance(status, str) or not status.strip():
                raise HubQualityError(f"fact {fact_id!r}: 'status' must be non-empty text")
            evidence = fact.get("evidence", "")
            if not isinstance(evidence, str):
                raise HubQualityError(f"fact {fact_id!r}: 'evidence' must be text")
            facts.append(
                {
                    "id": fact_id,
                    "title": "",
                    "text": text,
                    "status": status,
                    "observed_at": observed_at,
                    "evidence": evidence,
                }
            )
        sources.append(
            {"id": source_id, "project": project_id, "title": title, "facts": facts}
        )

    normalized_questions: list[dict] = []
    seen_questions: set[str] = set()
    for position, question in enumerate(raw_questions):
        if not isinstance(question, dict):
            raise HubQualityError(f"questions[{position}]: question must be an object")
        qid = question.get("q_id")
        if not isinstance(qid, str) or not qid.strip():
            raise HubQualityError(f"questions[{position}]: missing non-empty 'q_id'")
        case_id = _case_id_for_question(qid)
        if case_id in seen_questions:
            raise HubQualityError(f"questions: duplicate q_id/case id {qid!r}")
        seen_questions.add(case_id)
        question_type = question.get("type")
        text = question.get("text")
        if not isinstance(question_type, str) or not question_type.strip():
            raise HubQualityError(f"question {qid!r}: 'type' must be non-empty text")
        if not isinstance(text, str) or not text.strip():
            raise HubQualityError(f"question {qid!r}: 'text' must be non-empty text")
        normalized_questions.append(
            {
                "q_id": qid,
                "case_id": case_id,
                "q_type": question_type,
                "question": text,
            }
        )

    return (
        {"schema": expected_schema, "sources": sources},
        {"schema": expected_schema, "questions": normalized_questions},
    )


def freeze_inputs(materials_path: str | Path, questions_path: str | Path) -> dict:
    """Build a freeze manifest binding the pilot inputs by content hash."""
    materials = _load_json(materials_path, "materials")
    questions = _load_json(questions_path, "questions")
    normalized_materials, normalized_questions = _normalize_pilot_inputs(
        materials, questions
    )
    return {
        "freeze_schema": FREEZE_SCHEMA,
        "hub_quality_version": HUB_QUALITY_VERSION,
        "materials_sha256": sha256_file(materials_path),
        "questions_sha256": sha256_file(questions_path),
        "materials_schema": materials.get("schema"),
        "source_count": len(normalized_materials["sources"]),
        "question_count": len(normalized_questions["questions"]),
    }


def verify_freeze(
    manifest: dict, materials_path: str | Path, questions_path: str | Path
) -> None:
    """Fail closed when the live inputs no longer match the freeze manifest."""
    if manifest.get("freeze_schema") != FREEZE_SCHEMA:
        raise HubQualityError(
            f"freeze manifest: expected freeze_schema {FREEZE_SCHEMA!r}, got "
            f"{manifest.get('freeze_schema')!r}"
        )
    for key, path in (
        ("materials_sha256", materials_path),
        ("questions_sha256", questions_path),
    ):
        expected = manifest.get(key)
        actual = sha256_file(path)
        if expected != actual:
            raise HubQualityError(
                f"freeze mismatch: {key} expected {expected}, got {actual} "
                f"(inputs changed after freeze: {path})"
            )


def _normalize_prediction_case_ids(predictions: dict, questions: dict) -> dict:
    """Map public q_ids to evaluator-safe case IDs without mutating raw input."""
    aliases: dict[str, str] = {}
    for question in questions["questions"]:
        aliases[question["q_id"].casefold()] = question["case_id"]
        aliases[question["case_id"].casefold()] = question["case_id"]
    result = dict(predictions)
    rows = predictions.get("predictions")
    if not isinstance(rows, list):
        return result
    normalized_rows: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            normalized_rows.append(row)
            continue
        normalized_row = dict(row)
        case_id = row.get("case_id")
        if isinstance(case_id, str):
            normalized_row["case_id"] = aliases.get(case_id.casefold(), case_id)
        normalized_rows.append(normalized_row)
    result["predictions"] = normalized_rows
    return result


def _require_id(pattern: "re.Pattern[str]", value: object, what: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise HubQualityError(f"{what}: invalid id {value!r}")
    return value


def _case_id_for_question(qid: str) -> str:
    candidate = qid.casefold()
    if evaluator.CASE_ID_RE.fullmatch(candidate):
        return candidate
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,62}", qid):
        raise HubQualityError(f"question: unsupported q_id {qid!r}")
    candidate = "case-" + re.sub(r"_+", "-", candidate)
    return _require_id(evaluator.CASE_ID_RE, candidate, f"question {qid}")


def project_corpus(materials: dict, questions: dict, gold: dict) -> dict:
    """Project pilot inputs + external gold into an evaluator gold corpus.

    The explicit gold format is keyed by the public question IDs and carries
    independent expectations; prose gold is not interpreted or synthesized.
    """
    materials, questions = _normalize_pilot_inputs(materials, questions)
    sources = materials["sources"]
    qs = questions["questions"]
    if not isinstance(gold, dict) or gold.get("gold_schema") != "hub-quality-gold-v1":
        raise HubQualityError(
            "gold: expected object with gold_schema 'hub-quality-gold-v1'"
        )
    gold_cases = gold.get("cases")
    if not isinstance(gold_cases, dict):
        raise HubQualityError("gold: 'cases' must be an object keyed by question id")
    fixture_license = gold.get("license")
    if fixture_license not in evaluator.LICENSES:
        raise HubQualityError("gold: explicitly declare an evaluator-supported fixture license; the adapter does not assign source rights")

    manifest: list[dict] = []
    revisions: dict[str, str] = {}
    for src in sources:
        source_id = _require_id(
            evaluator.SOURCE_ID_RE, src.get("id"), "materials source"
        )
        project_id = _require_id(
            evaluator.PROJECT_ID_RE, src.get("project"), f"source {source_id}"
        )
        facts = src.get("facts")
        latest = ""
        for fact in facts:
            observed = fact.get("observed_at")
            latest = max(latest, observed)
        # Revision pins the latest observation date (matches REVISION_RE).
        revision = latest.replace("-", "")
        if not evaluator.REVISION_RE.fullmatch(revision):
            raise HubQualityError(
                f"materials: source {source_id!r} cannot form a revision "
                f"from observed_at dates"
            )
        revisions[source_id] = revision
        manifest.append(
            {
                "source_id": source_id,
                "project_id": project_id,
                "title": str(src.get("title", source_id)),
                "revision": revision,
                "valid_from": "unknown",
                "valid_until": None,
                "supersedes": [],
                "superseded_by": None,
                "aliases": [],
            }
        )

    question_ids = [q["q_id"] for q in qs]
    missing = [qid for qid in question_ids if qid not in gold_cases]
    extra = [qid for qid in gold_cases if qid not in set(question_ids)]
    if missing or extra:
        raise HubQualityError(
            f"gold/questions mismatch: missing gold for {missing}, "
            f"gold without question for {extra}"
        )

    cases: list[dict] = []
    seen_cases: set[str] = set()
    for q in qs:
        qid = q["q_id"]
        gold_case = gold_cases[qid]
        if not isinstance(gold_case, dict):
            raise HubQualityError(f"gold: case {qid!r} must be an object")
        case_id = q["case_id"]
        if case_id in seen_cases:
            raise HubQualityError(f"questions: duplicate case id {case_id!r}")
        seen_cases.add(case_id)
        expected = gold_case.get("expected")
        if not isinstance(expected, dict):
            raise HubQualityError(f"gold: case {qid!r} needs an 'expected' object")
        cases.append(
            {
                "case_id": case_id,
                "question": q["question"],
                "temporal_intent": gold_case.get("temporal_intent", "current"),
                "adversarial_kind": gold_case.get(
                    "adversarial_kind", "baseline_correctness"
                ),
                "q_id": qid,
                "q_type": q["q_type"],
                "expected": expected,
            }
        )

    corpus = {
        "schema_version": evaluator.SCHEMA_VERSION,
        "corpus_id": materials.get("pilot_id", "hub-quality-pilot"),
        "license": fixture_license,
        "manifest": manifest,
        "alias_groups": gold.get("alias_groups", []),
        "forbidden_sources": gold.get("forbidden_sources", []),
        "answer_match_policy": "per_case",
        "cases": cases,
    }
    # validate_corpus fail-closes (SystemExit 2) on any contract breach.
    # The normalized view it returns is not JSON-serializable (sets) and
    # reshapes manifest/cases, so validate for the side effect and keep
    # the raw corpus document for serialization and downstream layers.
    evaluator.validate_corpus(corpus)
    return corpus


def build_fact_index(materials: dict, questions: dict) -> dict:
    """Index facts per source with status and supersession markers.

    Returns ``{"sources": {source_id: {"project_id", "revision", "facts":
    {fact_id: {...}}}}, "superseded": {fact_id, ...}}``.
    """
    normalized, _ = _normalize_pilot_inputs(materials, questions)
    index: dict[str, dict] = {"sources": {}, "superseded": set()}
    supersession_labels: list[tuple[str, str]] = []
    fact_ids_by_project: dict[str, set[str]] = {}
    for src in normalized["sources"]:
        entry: dict = {
            "project_id": src.get("project"),
            "title": src.get("title", ""),
            "facts": {},
        }
        project_id = str(src.get("project", ""))
        supersession_labels.append((project_id, str(src.get("title", ""))))
        for fact in src.get("facts", []):
            fid = fact.get("id")
            if isinstance(fid, str) and fid:
                fact_ids_by_project.setdefault(project_id, set()).add(fid)
                entry["facts"][fid] = {
                    "text": str(fact.get("text", "")),
                    "status": str(fact.get("status", "current")),
                    "observed_at": str(fact.get("observed_at", "")),
                    "evidence": str(fact.get("evidence", "")),
                }
                supersession_labels.append((project_id, str(fact.get("title", ""))))
        index["sources"][src.get("id")] = entry
    superseded: set[str] = set()
    for project_id, label in supersession_labels:
        match = _SUPERSEDES_RE.search(label)
        if not match:
            continue
        token = match.group(1)
        project_fact_ids = fact_ids_by_project.get(project_id, set())
        candidates = [fid for fid in project_fact_ids if fid == token]
        if not candidates:
            candidates = [fid for fid in project_fact_ids if fid.endswith("-" + token)]
        if len(candidates) == 1:
            superseded.add(candidates[0])
    index["superseded"] = superseded
    return index


def normalize_quote(text: object) -> str:
    """Collapse all whitespace runs to single spaces for substring checks."""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def check_quote(
    quote: object, source_id: str, fact_index: dict
) -> dict:
    """Check one citation quote against the actual facts of its source."""
    entry = fact_index["sources"].get(source_id)
    result: dict = {
        "verified": False,
        "matched_fact_ids": [],
        "stale": False,
        "duplicate": False,
        "conflict_context": False,
        "reason": "",
    }
    if entry is None:
        result["reason"] = f"unknown source {source_id!r}"
        return result
    norm = normalize_quote(quote)
    if len(norm) < MIN_QUOTE_CHARS:
        result["reason"] = (
            f"quote too short ({len(norm)} chars, minimum {MIN_QUOTE_CHARS})"
        )
        return result
    matched: list[str] = []
    for fid, fact in entry["facts"].items():
        if norm in normalize_quote(fact["text"]):
            matched.append(fid)
    if not matched:
        result["reason"] = "quote not found verbatim in any fact of cited source"
        return result
    result["verified"] = True
    result["matched_fact_ids"] = sorted(matched)
    superseded = fact_index.get("superseded", set())
    if any(fid in superseded for fid in matched):
        result["stale"] = True
    if any(entry["facts"][fid]["status"] == "duplicate" for fid in matched):
        result["duplicate"] = True
    if any(
        _CONFLICT_STATUS_RE.search(str(fact.get("status", "")))
        for fact in entry["facts"].values()
    ):
        result["conflict_context"] = True
    return result


def _context_tokens(value: object) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def _case_expects_historical_evidence(case: dict) -> bool:
    q_type = _context_tokens(case.get("q_type"))
    temporal_intent = str(case.get("temporal_intent", "current")).casefold()
    if temporal_intent in {"historical", "any"}:
        return True
    if q_type & {"current", "current_value", "latest", "latest_value"}:
        return False
    context = q_type | _context_tokens(case.get("adversarial_kind"))
    context |= _context_tokens(case.get("question"))
    return bool(
        context
        & {
            "historical", "history", "stale", "outdated", "superseded",
            "obsolete", "nieaktual", "nieaktualna", "nieaktualne",
        }
    )


def _case_expects_duplicate_evidence(case: dict) -> bool:
    context = _context_tokens(case.get("q_type"))
    context |= _context_tokens(case.get("adversarial_kind"))
    context |= _context_tokens(case.get("question"))
    return bool(context & {"duplicate", "duplicates", "duplikat", "duplikaty"})


def quote_layer(predictions: dict, corpus: dict, fact_index: dict) -> dict:
    """Per-case quote verification; returns ``{"by_case": [...], "totals": ...}``."""
    revisions = {m["source_id"]: m["revision"] for m in corpus["manifest"]}
    pred_by_case = {p.get("case_id"): p for p in predictions.get("predictions", [])}
    by_case: list[dict] = []
    totals = {
        "citations_checked": 0,
        "citations_verified": 0,
        "cases_quote_unverified": 0,
        "cases_revision_mismatch": 0,
        "cases_stale_citation": 0,
        "cases_duplicate_citation": 0,
        "cases_stale_citation_gated": 0,
        "cases_duplicate_citation_gated": 0,
        "cases_conflict_context": 0,
        "cases_cross_project_cited": 0,
    }
    for case in corpus["cases"]:
        case_id = case["case_id"]
        pred = pred_by_case.get(case_id)
        entry: dict = {
            "case_id": case_id,
            "cited_projects": [],
            "cross_project_cited": False,
            "conflict_context": False,
            "citations": [],
            "case_gates": [],
        }
        if pred is None:
            by_case.append(entry)
            continue
        stale_expected = _case_expects_historical_evidence(case)
        duplicate_expected = _case_expects_duplicate_evidence(case)
        projects: set[str] = set()
        for i, citation in enumerate(pred.get("citations", [])):
            source_id = citation.get("source_id")
            check = check_quote(citation.get("quote"), source_id, fact_index)
            totals["citations_checked"] += 1
            if check["verified"]:
                totals["citations_verified"] += 1
            pinned = revisions.get(source_id)
            revision_ok = pinned is not None and citation.get("revision") == pinned
            manifest_entry = next(
                (m for m in corpus["manifest"] if m["source_id"] == source_id), None
            )
            if manifest_entry is not None:
                projects.add(manifest_entry["project_id"])
            entry["citations"].append(
                {
                    "index": i,
                    "source_id": source_id,
                    "verified": check["verified"],
                    "matched_fact_ids": check["matched_fact_ids"],
                    "revision_match": revision_ok,
                    "stale": check["stale"],
                    "duplicate": check["duplicate"],
                    "historical_evidence_expected": stale_expected,
                    "duplicate_evidence_expected": duplicate_expected,
                    "conflict_context": check["conflict_context"],
                    "reason": check["reason"],
                }
            )
            if check["conflict_context"]:
                entry["conflict_context"] = True
        entry["cited_projects"] = sorted(projects)
        entry["cross_project_cited"] = len(projects) > 1
        gates: list[str] = []
        if any(not c["verified"] for c in entry["citations"]):
            gates.append("quote_unverified")
        if any(not c["revision_match"] for c in entry["citations"]):
            gates.append("quote_revision_mismatch")
        if any(c["stale"] and not c["historical_evidence_expected"] for c in entry["citations"]):
            gates.append("stale_citation")
        if any(c["duplicate"] and not c["duplicate_evidence_expected"] for c in entry["citations"]):
            gates.append("duplicate_citation")
        entry["case_gates"] = gates
        # Conflict context and cross-project citation are advisory only.
        if gates:
            totals["cases_quote_unverified"] += 1
        if "quote_revision_mismatch" in gates:
            totals["cases_revision_mismatch"] += 1
        if any(c["stale"] for c in entry["citations"]):
            totals["cases_stale_citation"] += 1
        if "stale_citation" in gates:
            totals["cases_stale_citation_gated"] += 1
        if any(c["duplicate"] for c in entry["citations"]):
            totals["cases_duplicate_citation"] += 1
        if "duplicate_citation" in gates:
            totals["cases_duplicate_citation_gated"] += 1
        if entry["conflict_context"]:
            totals["cases_conflict_context"] += 1
        if entry["cross_project_cited"]:
            totals["cases_cross_project_cited"] += 1
        by_case.append(entry)
    return {"by_case": by_case, "totals": totals}


def run_evaluation(
    materials: dict,
    questions: dict,
    gold: dict,
    predictions: dict,
    freeze_manifest: dict,
    materials_path: str | Path,
    questions_path: str | Path,
    raw_predictions_sha256: str | None = None,
) -> tuple[dict, int]:
    """Project, verify freeze, evaluate via existing evaluator, add quotes."""
    verify_freeze(freeze_manifest, materials_path, questions_path)
    _, normalized_questions = _normalize_pilot_inputs(materials, questions)
    normalized_predictions = _normalize_prediction_case_ids(
        predictions, normalized_questions
    )
    corpus = project_corpus(materials, questions, gold)
    # validate_predictions fail-closes (SystemExit 2) on any contract
    # breach; the normalized view is for the evaluator, so keep the raw
    # predictions document for the quote layer and serialization.
    evaluator.validate_predictions(normalized_predictions)
    fact_index = build_fact_index(materials, questions)
    quotes = quote_layer(normalized_predictions, corpus, fact_index)
    with tempfile.TemporaryDirectory(prefix="hub-quality-runtime-") as tmp:
        tmp_path = Path(tmp)
        corpus_path = tmp_path / "corpus.json"
        pred_path = tmp_path / "predictions.json"
        corpus_path.write_text(
            json.dumps(corpus, ensure_ascii=False), encoding="utf-8"
        )
        pred_path.write_text(
            json.dumps(normalized_predictions, ensure_ascii=False), encoding="utf-8"
        )
        eval_report = evaluator.evaluate_bundle(corpus_path, pred_path)
    hub_gates: list[str] = []
    if eval_report.get("overall_status") != "PASS":
        hub_gates.append("evaluator_not_pass")
    for case_entry in quotes["by_case"]:
        for gate in case_entry["case_gates"]:
            hub_gates.append(f"{gate}:{case_entry['case_id']}")
    status = "PASS" if not hub_gates else "FAIL"
    report = {
        "report_schema": REPORT_SCHEMA,
        "hub_quality_version": HUB_QUALITY_VERSION,
        "evaluator_version": evaluator.VERSION,
        "evaluator_schema_version": evaluator.SCHEMA_VERSION,
        "provenance": {
            "materials_sha256": freeze_manifest.get("materials_sha256"),
            "questions_sha256": freeze_manifest.get("questions_sha256"),
            "corpus_id": corpus.get("corpus_id"),
            "raw_predictions_sha256": (
                raw_predictions_sha256 if raw_predictions_sha256 is not None else "UNKNOWN"
            ),
            "native_run_id": predictions.get(
                "native_run_id", predictions.get("run_id", "UNKNOWN")
            ) or "UNKNOWN",
            "model_id": predictions.get("model_id", "UNKNOWN") or "UNKNOWN",
            "model_kind": predictions.get("model_kind", "UNKNOWN") or "UNKNOWN",
            "timing": predictions.get("timing") if predictions.get("timing") is not None else "UNKNOWN",
            "usage": predictions.get("usage") if predictions.get("usage") is not None else "UNKNOWN",
        },
        "evaluator_report": eval_report,
        "quote_layer": quotes,
        "hub_gates": sorted(set(hub_gates)),
        "overall_status": status,
        "limits": [
            "quote verification is verbatim-substring presence after "
            "whitespace normalization; it does not prove semantic support, "
            "entailment, or span minimality",
            "conflict context and cross-project citation are advisory only "
            "and never gate PASS/FAIL",
            "latency/token/cost remain UNKNOWN unless native prediction "
            "provenance supplies values; explicit zero is preserved",
        ],
    }
    return report, 0 if status == "PASS" else 1


def _cmd_freeze(args: argparse.Namespace) -> int:
    manifest = freeze_inputs(args.materials, args.questions)
    _write_json(args.out, manifest, protected_paths=(args.materials, args.questions))
    print(f"froze {manifest['source_count']} sources, {manifest['question_count']} "
          f"questions -> {args.out}")
    return 0


def _cmd_project(args: argparse.Namespace) -> int:
    materials = _load_json(args.materials, "materials")
    questions = _load_json(args.questions, "questions")
    gold = _load_json(args.gold, "gold")
    manifest = _load_json(args.freeze_manifest, "freeze manifest")
    verify_freeze(manifest, args.materials, args.questions)
    corpus = project_corpus(materials, questions, gold)
    _write_json(
        args.out, corpus,
        protected_paths=(args.materials, args.questions, args.gold, args.freeze_manifest),
    )
    print(f"projected {len(corpus['cases'])} cases, "
          f"{len(corpus['manifest'])} sources -> {args.out}")
    return 0


def _cmd_evaluate(args: argparse.Namespace) -> int:
    materials = _load_json(args.materials, "materials")
    questions = _load_json(args.questions, "questions")
    gold = _load_json(args.gold, "gold")
    predictions, predictions_hash = _load_json_with_hash(
        args.predictions, "predictions"
    )
    manifest = _load_json(args.freeze_manifest, "freeze manifest")
    report, rc = run_evaluation(
        materials, questions, gold, predictions, manifest,
        args.materials, args.questions, predictions_hash,
    )
    _write_json(
        args.out, report,
        protected_paths=(args.materials, args.questions, args.gold,
                         args.predictions, args.freeze_manifest),
    )
    print(f"hub quality {report['overall_status']} "
          f"(gates: {len(report['hub_gates'])}) -> {args.out}")
    return rc


def _cmd_compare(args: argparse.Namespace) -> int:
    materials = _load_json(args.materials, "materials")
    questions = _load_json(args.questions, "questions")
    gold = _load_json(args.gold, "gold")
    baseline, baseline_hash = _load_json_with_hash(
        args.baseline, "baseline predictions"
    )
    candidate, candidate_hash = _load_json_with_hash(
        args.candidate, "candidate predictions"
    )
    manifest = _load_json(args.freeze_manifest, "freeze manifest")
    base_report, _ = run_evaluation(
        materials, questions, gold, baseline, manifest,
        args.materials, args.questions, baseline_hash,
    )
    cand_report, _ = run_evaluation(
        materials, questions, gold, candidate, manifest,
        args.materials, args.questions, candidate_hash,
    )
    with tempfile.TemporaryDirectory(prefix="hub-quality-compare-runtime-") as tmp:
        tmp_path = Path(tmp)
        base_path = tmp_path / "baseline.json"
        cand_path = tmp_path / "candidate.json"
        base_path.write_text(
            json.dumps(base_report["evaluator_report"], ensure_ascii=False),
            encoding="utf-8",
        )
        cand_path.write_text(
            json.dumps(cand_report["evaluator_report"], ensure_ascii=False),
            encoding="utf-8",
        )
        comparison = evaluator.compare(base_path, cand_path, tmp_path)
    payload = {
        "report_schema": REPORT_SCHEMA,
        "hub_quality_version": HUB_QUALITY_VERSION,
        "provenance": base_report["provenance"],
        "baseline_native_provenance": base_report["provenance"],
        "candidate_native_provenance": cand_report["provenance"],
        "baseline_overall": base_report["overall_status"],
        "candidate_overall": cand_report["overall_status"],
        "baseline_hub_gates": base_report["hub_gates"],
        "candidate_hub_gates": cand_report["hub_gates"],
        "evaluator_comparison": comparison,
    }
    _write_json(
        args.out, payload,
        protected_paths=(args.materials, args.questions, args.gold,
                         args.freeze_manifest, args.baseline, args.candidate),
    )
    print(f"compare: baseline {payload['baseline_overall']}, "
          f"candidate {payload['candidate_overall']} -> {args.out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hub_quality",
        description="Hub pilot preparation + strict provenance/quote validation "
                    "adapter over evaluate_retrieval.py",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_freeze = sub.add_parser("freeze", help="sha256-freeze pilot inputs")
    p_freeze.add_argument("--materials", required=True)
    p_freeze.add_argument("--questions", required=True)
    p_freeze.add_argument("--out", required=True)
    p_freeze.set_defaults(func=_cmd_freeze)

    p_project = sub.add_parser("project", help="project inputs+gold to a corpus")
    p_project.add_argument("--materials", required=True)
    p_project.add_argument("--questions", required=True)
    p_project.add_argument("--gold", required=True)
    p_project.add_argument("--freeze-manifest", required=True)
    p_project.add_argument("--out", required=True)
    p_project.set_defaults(func=_cmd_project)

    p_eval = sub.add_parser("evaluate", help="evaluate predictions with quote layer")
    p_eval.add_argument("--materials", required=True)
    p_eval.add_argument("--questions", required=True)
    p_eval.add_argument("--gold", required=True)
    p_eval.add_argument("--predictions", required=True)
    p_eval.add_argument("--freeze-manifest", required=True)
    p_eval.add_argument("--out", required=True)
    p_eval.set_defaults(func=_cmd_evaluate)

    p_cmp = sub.add_parser("compare", help="offline compare of two predictions runs")
    p_cmp.add_argument("--materials", required=True)
    p_cmp.add_argument("--questions", required=True)
    p_cmp.add_argument("--gold", required=True)
    p_cmp.add_argument("--freeze-manifest", required=True)
    p_cmp.add_argument("--baseline", required=True)
    p_cmp.add_argument("--candidate", required=True)
    p_cmp.add_argument("--out", required=True)
    p_cmp.set_defaults(func=_cmd_compare)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except HubQualityError as exc:
        print(f"hub_quality: input error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"hub_quality: file error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
