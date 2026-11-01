"""Deterministic tests for the hub_quality adapter.

Stdlib-only, no subprocess, no network. All fixtures are synthetic and
inline: nothing here is pilot evidence and no test-case answers are
hardcoded in the adapter itself. The adapter under test lives in
``hub_quality.py``; answer/citation metrics always come from the existing
``evaluate_retrieval.py``.

Run with::

    python test_hub_quality.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from contextlib import contextmanager
import unittest
from pathlib import Path
from unittest.mock import patch


HERE = Path(__file__).resolve().parent
PUBLIC_FIXTURE_ROOT = HERE.parent / "fixtures" / "hub-quality-pilot"
SPEC = importlib.util.spec_from_file_location("hub_quality", HERE / "hub_quality.py")
assert SPEC and SPEC.loader
hub = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hub)


def make_materials() -> dict:
    """Synthetic pilot-shaped materials with stale/duplicate/conflict traps."""
    return {
        "schema": "hub-quality-pilot-frozen-v1",
        "materials": [
            {
                "source_id": "atlas-handbook",
                "project": "atlas",
                "title": "Atlas handbook (supersedes atlas-fact-old)",
                "facts": [
                    {
                        "fact_id": "atlas-fact-new",
                        "text": "All engineers complete role-based training quarterly.",
                        "status": "current",
                        "observed_at": "2026-02-01",
                        "evidence": "synthetic",
                    },
                    {
                        "fact_id": "atlas-fact-old",
                        "text": "All engineers complete yearly training.",
                        "status": "superseded",
                        "observed_at": "2024-01-01",
                        "evidence": "synthetic",
                    },
                ],
            },
            {
                "source_id": "atlas-notes",
                "project": "atlas",
                "title": "Atlas notes",
                "facts": [
                    {
                        "fact_id": "atlas-note-1",
                        "text": "The handbook draft was copied twice during migration.",
                        "status": "duplicate",
                        "observed_at": "2026-02-01",
                        "evidence": "synthetic",
                    },
                ],
            },
            {
                "source_id": "boreal-guide",
                "project": "boreal",
                "title": "Boreal guide",
                "facts": [
                    {
                        "fact_id": "boreal-fact-1",
                        "text": "Boreal teams follow the shared on-call rotation.",
                        "status": "contradicts:boreal-fact-2",
                        "observed_at": "2026-03-01",
                        "evidence": "synthetic",
                    },
                    {
                        "fact_id": "boreal-fact-2",
                        "text": "Boreal teams never join the shared on-call rotation.",
                        "status": "contradicts:boreal-fact-1",
                        "observed_at": "2026-03-01",
                        "evidence": "synthetic",
                    },
                ],
            },
        ],
    }


def make_questions() -> dict:
    return {
        "schema": "hub-quality-pilot-frozen-v1",
        "questions": [
            {"q_id": "Q01", "type": "current_value", "text": "How often is training?"},
            {"q_id": "Q02", "type": "outdated_detection", "text": "Identify the outdated rule and current rule."},
            {"q_id": "Q03", "type": "conflict", "text": "Who is on call?"},
            {"q_id": "Q04", "type": "duplicate", "text": "Which facts are duplicates?"},
        ]
    }


def make_gold() -> dict:
    return {
        "gold_schema": "hub-quality-gold-v1",
        "license": "CC0-1.0",
        "cases": {
            "Q01": {
                "temporal_intent": "current",
                "adversarial_kind": "baseline_correctness",
                "expected": {
                    "answer": "role-based training quarterly",
                    "answer_match_policy": "contains",
                    "abstain_expected": False,
                    "required_sources": ["atlas-handbook"],
                    "allowed_revisions": ["20260201"],
                    "forbidden_sources": [],
                },
            },
            "Q02": {
                "temporal_intent": "current",
                "adversarial_kind": "superseded_source",
                "expected": {
                    "answer": "role-based training quarterly",
                    "answer_match_policy": "contains",
                    "abstain_expected": False,
                    "required_sources": ["atlas-handbook"],
                    "allowed_revisions": ["20260201"],
                    "forbidden_sources": [],
                },
            },
            "Q03": {
                "temporal_intent": "current",
                "adversarial_kind": "conflicting_evidence",
                "expected": {
                    "answer": "shared on-call rotation",
                    "answer_match_policy": "contains",
                    "abstain_expected": False,
                    "required_sources": ["boreal-guide"],
                    "allowed_revisions": ["20260301"],
                    "forbidden_sources": [],
                },
            },
            "Q04": {
                "temporal_intent": "any",
                "adversarial_kind": "duplicate_advisory",
                "expected": {
                    "answer": "copied twice",
                    "answer_match_policy": "contains",
                    "abstain_expected": False,
                    "required_sources": ["atlas-notes"],
                    "allowed_revisions": ["20260201"],
                    "forbidden_sources": [],
                },
            },
        },
    }


def make_synthetic_abstention_gold(questions: dict) -> dict:
    """Build schema-only projection gold without inventing answer values."""
    expected = {
        "answer": None,
        "answer_match_policy": "abstain",
        "abstain_expected": True,
        "required_sources": [],
        "allowed_revisions": None,
        "forbidden_sources": [],
        "must_disambiguate_alias_group": None,
    }
    return {
        "gold_schema": "hub-quality-gold-v1",
        "license": "CC0-1.0",
        "cases": {
            question["q_id"]: {"expected": dict(expected)}
            for question in questions["questions"]
        },
    }


def make_predictions(kind: str = "good") -> dict:
    """Synthetic native predictions. ``kind`` selects the trap to trigger."""
    q1_quote = "All engineers complete role-based training quarterly."
    q2_stale_quote = "All engineers complete yearly training."
    q3_quote = "Boreal teams follow the shared on-call rotation."
    dup_quote = "The handbook draft was copied twice during migration."
    if kind == "good":
        q2_quote, q2_rev = q1_quote, "20260201"
        extra_citations_q3 = []
    elif kind == "stale":
        q2_quote, q2_rev = q2_stale_quote, "20260201"
        extra_citations_q3 = []
    elif kind == "stale-current":
        q1_quote, q2_quote, q2_rev = q2_stale_quote, q1_quote, "20260201"
        extra_citations_q3 = []
    elif kind == "invented":
        q2_quote, q2_rev = "Training happens on the moon.", "20260201"
        extra_citations_q3 = []
    elif kind == "duplicate":
        q2_quote, q2_rev = q1_quote, "20260201"
        extra_citations_q3 = [
            {
                "source_id": "atlas-notes",
                "revision": "20260201",
                "quote": dup_quote,
                "page": 1,
                "confidence": 0.5,
                "timestamp_zone": "UTC",
                "retrieved_at": "2026-04-01T00:00:00Z",
            }
        ]
    elif kind == "wrong-answer":
        q2_quote, q2_rev = q1_quote, "20260201"
        extra_citations_q3 = []
    else:
        raise AssertionError(kind)
    q3_answer = "shared on-call rotation"
    q2_answer = "role-based training quarterly"
    if kind == "wrong-answer":
        q2_answer = "something entirely different"
    return {
        # Contract field required by evaluate_retrieval (mirrors its own
        # fixtures); omitting it must fail closed, never default.
        "schema_version": hub.evaluator.SCHEMA_VERSION,
        "model_id": "synth-run-1",
        "model_kind": "baseline",
        "predictions": [
            {
                "case_id": "q01",
                "answer": q2_answer,
                "abstained": False,
                "citations": [
                    {
                        "source_id": "atlas-handbook",
                        "revision": "20260201",
                        "quote": q1_quote,
                        "page": 1,
                        "confidence": 0.9,
                        "timestamp_zone": "UTC",
                        "retrieved_at": "2026-04-01T00:00:00Z",
                    }
                ],
            },
            {
                "case_id": "q02",
                "answer": q2_answer,
                "abstained": False,
                "citations": [
                    {
                        "source_id": "atlas-handbook",
                        "revision": q2_rev,
                        "quote": q2_quote,
                        "page": 1,
                        "confidence": 0.8,
                        "timestamp_zone": "UTC",
                        "retrieved_at": "2026-04-01T00:00:00Z",
                    }
                ],
            },
            {
                "case_id": "q03",
                "answer": q3_answer,
                "abstained": False,
                "citations": [
                    {
                        "source_id": "boreal-guide",
                        "revision": "20260301",
                        "quote": q3_quote,
                        "page": 1,
                        "confidence": 0.7,
                        "timestamp_zone": "UTC",
                        "retrieved_at": "2026-04-01T00:00:00Z",
                    },
                    *extra_citations_q3,
                ],
            },
            {
                "case_id": "q04",
                "answer": "The handbook draft was copied twice during migration.",
                "abstained": False,
                "citations": [
                    {
                        "source_id": "atlas-notes",
                        "revision": "20260201",
                        "quote": dup_quote,
                        "page": 1,
                        "confidence": 0.6,
                        "timestamp_zone": "UTC",
                        "retrieved_at": "2026-04-01T00:00:00Z",
                    }
                ],
            },
        ],
    }


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


@contextmanager
def task_temp_dir():
    """Retain task-owned synthetic artifacts outside the skill source tree."""
    artifact_parent = Path(os.environ.get("TEAM_TEST_ROOT", tempfile.gettempdir())).resolve()
    if artifact_parent.is_relative_to(HERE.parent.parent):
        raise RuntimeError("Test artifacts must stay outside the skill source tree")
    artifact_parent.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix="hub-quality-test-", dir=artifact_parent))
    yield str(path)


class FreezeTests(unittest.TestCase):
    def test_freeze_is_deterministic_and_verifies(self) -> None:
        with task_temp_dir() as tmp:
            root = Path(tmp)
            mat, que = root / "m.json", root / "q.json"
            write_json(mat, make_materials())
            write_json(que, make_questions())
            first = hub.freeze_inputs(mat, que)
            second = hub.freeze_inputs(mat, que)
            self.assertEqual(first, second)
            hub.verify_freeze(first, mat, que)

    def test_freeze_detects_post_freeze_tamper(self) -> None:
        with task_temp_dir() as tmp:
            root = Path(tmp)
            mat, que = root / "m.json", root / "q.json"
            write_json(mat, make_materials())
            write_json(que, make_questions())
            manifest = hub.freeze_inputs(mat, que)
            tampered = make_materials()
            tampered["materials"].append(
                {
                    "source_id": "extra-source",
                    "project": "atlas",
                    "title": "Extra",
                    "facts": [
                        {
                            "fact_id": "x-1",
                            "text": "Extra fact.",
                            "status": "current",
                            "observed_at": "2026-05-01",
                            "evidence": "synthetic",
                        }
                    ],
                }
            )
            write_json(mat, tampered)
            with self.assertRaises(hub.HubQualityError):
                hub.verify_freeze(manifest, mat, que)


class ProjectTests(unittest.TestCase):
    def test_fixture_license_must_be_explicit(self) -> None:
        gold = make_gold()
        gold.pop("license")
        with self.assertRaisesRegex(hub.HubQualityError, "license"):
            hub.project_corpus(make_materials(), make_questions(), gold)

    def test_supplied_public_inputs_project_with_synthetic_schema_gold(self) -> None:
        materials_path = PUBLIC_FIXTURE_ROOT / "materials.json"
        questions_path = PUBLIC_FIXTURE_ROOT / "questions.json"
        materials = json.loads(materials_path.read_bytes())
        questions = json.loads(questions_path.read_bytes())
        normalized_materials, normalized_questions = hub._normalize_pilot_inputs(
            materials, questions
        )
        corpus = hub.project_corpus(
            materials, questions, make_synthetic_abstention_gold(questions)
        )
        self.assertEqual(len(normalized_materials["sources"]), 7)
        self.assertEqual(len(normalized_questions["questions"]), 8)
        self.assertEqual(len(corpus["manifest"]), 7)
        self.assertEqual(len(corpus["cases"]), 8)
        self.assertEqual(
            [case["case_id"] for case in corpus["cases"]],
            [question["case_id"] for question in normalized_questions["questions"]],
        )

    def test_public_fixture_quote_context_and_abstention_gates(self) -> None:
        materials = json.loads(
            (PUBLIC_FIXTURE_ROOT / "materials.json").read_bytes()
        )
        questions = json.loads(
            (PUBLIC_FIXTURE_ROOT / "questions.json").read_bytes()
        )
        gold = make_synthetic_abstention_gold(questions)
        corpus = hub.project_corpus(materials, questions, gold)
        fact_index = hub.build_fact_index(materials, questions)
        manifest = {source["source_id"]: source for source in corpus["manifest"]}

        def case_for(predicate, project_id=None):
            return next(
                case for case in corpus["cases"]
                if predicate(case)
                and (project_id is None or project_id in case["question"])
            )

        def source_fact(predicate):
            return next(
                (source, fact)
                for source in materials["materials"]
                for fact in source["facts"]
                if predicate(source, fact)
            )

        def prediction(case, source, fact, quote=None):
            return {
                "case_id": case["case_id"],
                "answer": None,
                "abstained": True,
                "citations": [{
                    "source_id": source["source_id"],
                    "revision": manifest[source["source_id"]]["revision"],
                    "quote": fact["text"] if quote is None else quote,
                }],
            }

        stale_source, stale_fact = source_fact(
            lambda _source, fact: fact["fact_id"] in fact_index["superseded"]
        )
        historical_case = case_for(hub._case_expects_historical_evidence)
        current_case = case_for(
            lambda case: bool(hub._context_tokens(case.get("q_type")) & {"current"}),
            stale_source["project"],
        )

        duplicate_source, duplicate_fact = source_fact(
            lambda _source, fact: fact["status"].casefold() == "duplicate"
        )
        duplicate_case = case_for(
            hub._case_expects_duplicate_evidence, duplicate_source["project"]
        )

        conflict_source, conflict_fact = source_fact(
            lambda _source, fact: bool(hub._CONFLICT_STATUS_RE.search(fact["status"]))
        )
        conflict_case = case_for(
            lambda case: "contradiction" in hub._context_tokens(case.get("q_type")),
            conflict_source["project"],
        )

        predictions = {"predictions": [
            prediction(historical_case, stale_source, stale_fact),
            prediction(current_case, stale_source, stale_fact),
            prediction(duplicate_case, duplicate_source, duplicate_fact),
            prediction(conflict_case, conflict_source, conflict_fact),
        ]}
        report = hub.quote_layer(predictions, corpus, fact_index)
        by_case = {case["case_id"]: case for case in report["by_case"]}
        historical = by_case[historical_case["case_id"]]
        current = by_case[current_case["case_id"]]
        duplicate = by_case[duplicate_case["case_id"]]
        conflict = by_case[conflict_case["case_id"]]
        self.assertTrue(historical["citations"][0]["verified"])
        self.assertTrue(historical["citations"][0]["stale"])
        self.assertNotIn("stale_citation", historical["case_gates"])
        self.assertIn("stale_citation", current["case_gates"])
        self.assertTrue(duplicate["citations"][0]["verified"])
        self.assertTrue(duplicate["citations"][0]["duplicate"])
        self.assertNotIn("duplicate_citation", duplicate["case_gates"])
        self.assertTrue(conflict["conflict_context"])
        self.assertEqual(conflict["case_gates"], [])
        self.assertEqual(report["totals"]["citations_verified"], 4)

        case_index = next(
            i for i, case in enumerate(corpus["cases"])
            if case["case_id"] == current_case["case_id"]
        )
        invalid_abstentions = (
            ("source", lambda citation: citation.update(source_id="unknown-source"),
             "quote_unverified"),
            ("revision", lambda citation: citation.update(revision="unknown-revision"),
             "quote_revision_mismatch"),
            ("quote", lambda citation: citation.update(quote="fabricated abstention source"),
             "quote_unverified"),
        )
        for label, mutate, expected_gate in invalid_abstentions:
            with self.subTest(invalid=label):
                fabricated = prediction(current_case, stale_source, stale_fact)
                mutate(fabricated["citations"][0])
                abstention_report = hub.quote_layer(
                    {"predictions": [fabricated]}, corpus, fact_index
                )
                rejected = abstention_report["by_case"][case_index]
                self.assertTrue(fabricated["abstained"])
                self.assertIn(expected_gate, rejected["case_gates"])

    def test_project_rejects_bad_source_id(self) -> None:
        materials = make_materials()
        materials["materials"][0]["source_id"] = "Bad_ID"
        with self.assertRaises(hub.HubQualityError):
            hub.project_corpus(materials, make_questions(), make_gold())

    def test_project_rejects_gold_question_mismatch(self) -> None:
        gold = make_gold()
        del gold["cases"]["Q03"]
        with self.assertRaises(hub.HubQualityError):
            hub.project_corpus(make_materials(), make_questions(), gold)

    def test_project_rejects_unknown_gold_ids_and_unsupported_prose(self) -> None:
        gold = make_gold()
        gold["cases"]["Q99"] = gold["cases"]["Q01"]
        with self.assertRaises(hub.HubQualityError):
            hub.project_corpus(make_materials(), make_questions(), gold)
        with self.assertRaises(hub.HubQualityError):
            hub.project_corpus(
                make_materials(), make_questions(),
                {"gold_schema": "hub-quality-gold-v1", "cases": "answer key in prose"},
            )

    def test_project_rejects_duplicate_ids_and_wrong_schema(self) -> None:
        materials = make_materials()
        materials["materials"].append(materials["materials"][0])
        with self.assertRaises(hub.HubQualityError):
            hub.project_corpus(materials, make_questions(), make_gold())
        materials = make_materials()
        materials["materials"][1]["facts"][0]["fact_id"] = "atlas-fact-new"
        with self.assertRaises(hub.HubQualityError):
            hub.project_corpus(materials, make_questions(), make_gold())
        questions = make_questions()
        questions["questions"].append(dict(questions["questions"][0]))
        with self.assertRaises(hub.HubQualityError):
            hub.project_corpus(make_materials(), questions, make_gold())
        questions = make_questions()
        questions["schema"] = "unsupported-v2"
        with self.assertRaises(hub.HubQualityError):
            hub.project_corpus(make_materials(), questions, make_gold())

    def test_project_output_passes_evaluator_validation(self) -> None:
        corpus = hub.project_corpus(make_materials(), make_questions(), make_gold())
        # Fail-closed contract: validate_corpus returns a normalized view
        # dict on success and raises SystemExit(2) on any breach.
        view = hub.evaluator.validate_corpus(corpus)
        self.assertEqual(view["corpus_id"], corpus["corpus_id"])
        self.assertEqual(
            {c["case_id"] for c in view["cases"]}, {"q01", "q02", "q03", "q04"}
        )
        revisions = {m["source_id"]: m["revision"] for m in corpus["manifest"]}
        self.assertEqual(
            revisions,
            {
                "atlas-handbook": "20260201",
                "atlas-notes": "20260201",
                "boreal-guide": "20260301",
            },
        )


def run_eval(kind: str) -> tuple[dict, int]:
    with task_temp_dir() as tmp:
        root = Path(tmp)
        mat, que = root / "m.json", root / "q.json"
        write_json(mat, make_materials())
        write_json(que, make_questions())
        manifest = hub.freeze_inputs(mat, que)
        return hub.run_evaluation(
            make_materials(), make_questions(), make_gold(),
            make_predictions(kind), manifest, mat, que,
        )


def run_eval_with_predictions(predictions: dict) -> tuple[dict, int]:
    with task_temp_dir() as tmp:
        root = Path(tmp)
        mat, que = root / "m.json", root / "q.json"
        write_json(mat, make_materials())
        write_json(que, make_questions())
        manifest = hub.freeze_inputs(mat, que)
        return hub.run_evaluation(
            make_materials(), make_questions(), make_gold(), predictions,
            manifest, mat, que,
        )


class TemporaryDirectoryPortabilityTests(unittest.TestCase):
    def test_run_evaluation_works_with_shallow_installed_module_path(self) -> None:
        with task_temp_dir() as tmp:
            root = Path(tmp)
            materials_path, questions_path = root / "m.json", root / "q.json"
            write_json(materials_path, make_materials())
            write_json(questions_path, make_questions())
            materials_before = materials_path.read_bytes()
            questions_before = questions_path.read_bytes()
            manifest = hub.freeze_inputs(materials_path, questions_path)

            # A canonical install may be only two components below its drive/root.
            # Keep HERE synthetic so the old source-ancestor lookup fails without
            # attempting to create anything outside this task's temp directory.
            shallow_install = Path(Path.cwd().anchor) / "installed" / "scripts"
            with patch.object(hub, "HERE", shallow_install):
                report, rc = hub.run_evaluation(
                    make_materials(), make_questions(), make_gold(),
                    make_predictions("good"), manifest,
                    materials_path, questions_path,
                )

        self.assertEqual(rc, 0)
        self.assertEqual(report["overall_status"], "PASS")
        self.assertEqual(materials_path.read_bytes(), materials_before)
        self.assertEqual(questions_path.read_bytes(), questions_before)


class QuoteLayerTests(unittest.TestCase):
    def test_good_run_passes(self) -> None:
        report, rc = run_eval("good")
        self.assertEqual(rc, 0)
        self.assertEqual(report["overall_status"], "PASS")
        self.assertEqual(report["hub_gates"], [])
        self.assertEqual(
            report["quote_layer"]["totals"]["citations_verified"], 4
        )

    def test_historical_quote_is_valid_when_identifying_outdated_evidence(self) -> None:
        report, rc = run_eval("stale")
        self.assertEqual(rc, 0)
        self.assertEqual(report["overall_status"], "PASS")
        self.assertNotIn("stale_citation:q02", report["hub_gates"])
        q2 = next(c for c in report["quote_layer"]["by_case"] if c["case_id"] == "q02")
        self.assertTrue(q2["citations"][0]["verified"])
        self.assertTrue(q2["citations"][0]["stale"])

    def test_stale_quote_still_gates_current_value_use(self) -> None:
        report, rc = run_eval("stale-current")
        self.assertEqual(rc, 1)
        self.assertIn("stale_citation:q01", report["hub_gates"])

    def test_invented_quote_gates(self) -> None:
        report, rc = run_eval("invented")
        self.assertEqual(rc, 1)
        self.assertIn("quote_unverified:q02", report["hub_gates"])

    def test_duplicate_quote_gates_but_conflict_is_advisory(self) -> None:
        report, rc = run_eval("duplicate")
        self.assertEqual(rc, 1)
        self.assertIn("duplicate_citation:q03", report["hub_gates"])
        self.assertNotIn("duplicate_citation:q04", report["hub_gates"])
        q4 = next(c for c in report["quote_layer"]["by_case"] if c["case_id"] == "q04")
        self.assertTrue(q4["citations"][0]["verified"])
        self.assertTrue(q4["citations"][0]["duplicate"])
        # Conflict context is recorded but never a gate by itself.
        q3 = next(
            c for c in report["quote_layer"]["by_case"] if c["case_id"] == "q03"
        )
        self.assertTrue(q3["conflict_context"])
        self.assertNotIn("conflict_context:q03", report["hub_gates"])

    def test_conflict_context_alone_does_not_gate(self) -> None:
        report, rc = run_eval("good")
        q3 = next(
            c for c in report["quote_layer"]["by_case"] if c["case_id"] == "q03"
        )
        self.assertTrue(q3["conflict_context"])
        self.assertEqual(rc, 0)

    def test_wrong_answer_fails_via_evaluator_not_quote_layer(self) -> None:
        report, rc = run_eval("wrong-answer")
        self.assertEqual(rc, 1)
        self.assertIn("evaluator_not_pass", report["hub_gates"])

    def test_revision_mismatch_gates(self) -> None:
        preds = make_predictions("good")
        preds["predictions"][0]["citations"][0]["revision"] = "19990101"
        with task_temp_dir() as tmp:
            root = Path(tmp)
            mat, que = root / "m.json", root / "q.json"
            write_json(mat, make_materials())
            write_json(que, make_questions())
            manifest = hub.freeze_inputs(mat, que)
            report, rc = hub.run_evaluation(
                make_materials(), make_questions(), make_gold(), preds,
                manifest, mat, que,
            )
        self.assertEqual(rc, 1)
        self.assertIn("quote_revision_mismatch:q01", report["hub_gates"])

    def test_abstention_does_not_bypass_quote_validation(self) -> None:
        predictions = make_predictions("good")
        predictions["predictions"][0].update({"answer": None, "abstained": True})
        predictions["predictions"][0]["citations"][0]["quote"] = "fabricated evidence"
        report, rc = run_eval_with_predictions(predictions)
        self.assertEqual(rc, 1)
        self.assertIn("quote_unverified:q01", report["hub_gates"])
        q1 = next(c for c in report["quote_layer"]["by_case"] if c["case_id"] == "q01")
        self.assertFalse(q1["citations"][0]["verified"])

    def test_unknown_citation_source_is_rejected(self) -> None:
        predictions = make_predictions("good")
        predictions["predictions"][0]["citations"][0]["source_id"] = "unknown-source"
        report, rc = run_eval_with_predictions(predictions)
        self.assertEqual(rc, 1)
        self.assertIn("quote_unverified:q01", report["hub_gates"])
        q1 = next(c for c in report["quote_layer"]["by_case"] if c["case_id"] == "q01")
        self.assertFalse(q1["citations"][0]["verified"])
        self.assertFalse(q1["citations"][0]["revision_match"])

    def test_unknown_usage_stays_unknown(self) -> None:
        report, _ = run_eval("good")
        self.assertEqual(
            report["evaluator_report"]["latency_observed"], "UNKNOWN"
        )
        for forbidden in ("latency_ms", "token_count", "cost_usd", "tokens", "cost"):
            self.assertNotIn(forbidden, report)

    def test_provenance_hashes_preserved(self) -> None:
        with task_temp_dir() as tmp:
            root = Path(tmp)
            mat, que = root / "m.json", root / "q.json"
            write_json(mat, make_materials())
            write_json(que, make_questions())
            manifest = hub.freeze_inputs(mat, que)
            report, _ = hub.run_evaluation(
                make_materials(), make_questions(), make_gold(),
                make_predictions("good"), manifest, mat, que,
            )
            self.assertEqual(
                report["provenance"]["materials_sha256"],
                manifest["materials_sha256"],
            )
            self.assertEqual(
                report["provenance"]["questions_sha256"],
                manifest["questions_sha256"],
            )

    def test_native_provenance_preserves_explicit_zero_and_unknown(self) -> None:
        predictions = make_predictions("good")
        predictions.update({
            "run_id": "native-run-123",
            "timing": {"elapsed_ms": 0},
            "usage": {"input_tokens": 0, "output_tokens": 0},
        })
        report, _ = run_eval_with_predictions(predictions)
        provenance = report["provenance"]
        self.assertEqual(provenance["native_run_id"], "native-run-123")
        self.assertEqual(provenance["timing"], {"elapsed_ms": 0})
        self.assertEqual(provenance["usage"]["input_tokens"], 0)
        self.assertEqual(provenance["model_id"], "synth-run-1")
        self.assertEqual(provenance["raw_predictions_sha256"], "UNKNOWN")


class CrossProjectTests(unittest.TestCase):
    def test_cross_project_citation_is_advisory_only(self) -> None:
        preds = make_predictions("good")
        preds["predictions"][0]["citations"].append(
            {
                "source_id": "boreal-guide",
                "revision": "20260301",
                "quote": "Boreal teams follow the shared on-call rotation.",
                "page": 1,
                "confidence": 0.4,
                "timestamp_zone": "UTC",
                "retrieved_at": "2026-04-01T00:00:00Z",
            }
        )
        with task_temp_dir() as tmp:
            root = Path(tmp)
            mat, que = root / "m.json", root / "q.json"
            write_json(mat, make_materials())
            write_json(que, make_questions())
            manifest = hub.freeze_inputs(mat, que)
            report, rc = hub.run_evaluation(
                make_materials(), make_questions(), make_gold(), preds,
                manifest, mat, que,
            )
        q1 = next(
            c for c in report["quote_layer"]["by_case"] if c["case_id"] == "q01"
        )
        self.assertTrue(q1["cross_project_cited"])
        self.assertEqual(q1["cited_projects"], ["atlas", "boreal"])
        # Evaluator-side project isolation may still fail the run, but the
        # hub quote layer itself adds no cross-project gate.
        self.assertFalse(
            [g for g in report["hub_gates"] if g.startswith("cross_project")]
        )
        self.assertEqual(
            report["quote_layer"]["totals"]["cases_cross_project_cited"], 1
        )

    def test_explicit_forbidden_source_still_fails_evaluator_gate(self) -> None:
        predictions = make_predictions("good")
        predictions["predictions"][0]["citations"].append(
            {
                "source_id": "boreal-guide",
                "revision": "20260301",
                "quote": "Boreal teams follow the shared on-call rotation.",
                "page": 1,
                "confidence": 0.4,
                "timestamp_zone": "UTC",
                "retrieved_at": "2026-04-01T00:00:00Z",
            }
        )
        gold = make_gold()
        gold["cases"]["Q01"]["expected"]["forbidden_sources"] = ["boreal-guide"]
        with task_temp_dir() as tmp:
            root = Path(tmp)
            mat, que = root / "m.json", root / "q.json"
            write_json(mat, make_materials())
            write_json(que, make_questions())
            manifest = hub.freeze_inputs(mat, que)
            report, rc = hub.run_evaluation(
                make_materials(), make_questions(), gold, predictions,
                manifest, mat, que,
            )
        self.assertEqual(rc, 1)
        self.assertIn("evaluator_not_pass", report["hub_gates"])
        q1 = next(c for c in report["quote_layer"]["by_case"] if c["case_id"] == "q01")
        self.assertTrue(q1["cross_project_cited"])
        self.assertTrue(all(c["verified"] for c in q1["citations"]))


class CliTests(unittest.TestCase):
    def _write_inputs(self, root: Path) -> dict[str, Path]:
        paths = {
            "materials": root / "materials.json",
            "questions": root / "questions.json",
            "gold": root / "gold.json",
            "freeze": root / "freeze.json",
            "good": root / "good.json",
            "stale": root / "stale.json",
        }
        write_json(paths["materials"], make_materials())
        write_json(paths["questions"], make_questions())
        write_json(paths["gold"], make_gold())
        write_json(paths["good"], make_predictions("good"))
        write_json(paths["stale"], make_predictions("stale-current"))
        return paths

    def test_cli_end_to_end(self) -> None:
        with task_temp_dir() as tmp:
            root = Path(tmp)
            p = self._write_inputs(root)
            corpus_out, report_out, stale_report_out, cmp_out = (
                root / "corpus.json",
                root / "report.json",
                root / "stale-report.json",
                root / "cmp.json",
            )
            self.assertEqual(
                hub.main(["freeze", "--materials", str(p["materials"]),
                          "--questions", str(p["questions"]),
                          "--out", str(p["freeze"])]),
                0,
            )
            self.assertEqual(
                hub.main(["project", "--materials", str(p["materials"]),
                          "--questions", str(p["questions"]),
                          "--gold", str(p["gold"]),
                          "--freeze-manifest", str(p["freeze"]),
                          "--out", str(corpus_out)]),
                0,
            )
            self.assertEqual(
                hub.main(["evaluate", "--materials", str(p["materials"]),
                          "--questions", str(p["questions"]),
                          "--gold", str(p["gold"]),
                          "--predictions", str(p["good"]),
                          "--freeze-manifest", str(p["freeze"]),
                          "--out", str(report_out)]),
                0,
            )
            report = json.loads(report_out.read_text(encoding="utf-8"))
            self.assertEqual(report["overall_status"], "PASS")
            self.assertEqual(
                report["provenance"]["raw_predictions_sha256"],
                hub.sha256_file(p["good"]),
            )
            self.assertEqual(report["provenance"]["native_run_id"], "UNKNOWN")
            self.assertEqual(
                hub.main(["evaluate", "--materials", str(p["materials"]),
                          "--questions", str(p["questions"]),
                          "--gold", str(p["gold"]),
                          "--predictions", str(p["stale"]),
                          "--freeze-manifest", str(p["freeze"]),
                          "--out", str(stale_report_out)]),
                1,
            )
            self.assertEqual(
                hub.main(["compare", "--materials", str(p["materials"]),
                          "--questions", str(p["questions"]),
                          "--gold", str(p["gold"]),
                          "--freeze-manifest", str(p["freeze"]),
                          "--baseline", str(p["good"]),
                          "--candidate", str(p["stale"]),
                          "--out", str(cmp_out)]),
                0,
            )
            comparison = json.loads(cmp_out.read_text(encoding="utf-8"))
            self.assertEqual(comparison["baseline_overall"], "PASS")
            self.assertEqual(comparison["candidate_overall"], "FAIL")
            self.assertEqual(
                comparison["baseline_native_provenance"]["raw_predictions_sha256"],
                hub.sha256_file(p["good"]),
            )
            self.assertEqual(
                comparison["candidate_native_provenance"]["raw_predictions_sha256"],
                hub.sha256_file(p["stale"]),
            )

    def test_cli_freeze_tamper_returns_2(self) -> None:
        with task_temp_dir() as tmp:
            root = Path(tmp)
            p = self._write_inputs(root)
            self.assertEqual(
                hub.main(["freeze", "--materials", str(p["materials"]),
                          "--questions", str(p["questions"]),
                          "--out", str(p["freeze"])]),
                0,
            )
            tampered = make_materials()
            tampered["materials"] = tampered["materials"][:1]
            write_json(p["materials"], tampered)
            self.assertEqual(
                hub.main(["project", "--materials", str(p["materials"]),
                          "--questions", str(p["questions"]),
                          "--gold", str(p["gold"]),
                          "--freeze-manifest", str(p["freeze"]),
                          "--out", str(root / "corpus.json")]),
                2,
            )

    def test_cli_freezes_and_projects_supplied_public_fixture_without_changing_bytes(self) -> None:
        materials = PUBLIC_FIXTURE_ROOT / "materials.json"
        questions = PUBLIC_FIXTURE_ROOT / "questions.json"
        material_bytes = materials.read_bytes()
        question_bytes = questions.read_bytes()
        normalized_materials, normalized_questions = hub._normalize_pilot_inputs(
            json.loads(material_bytes), json.loads(question_bytes)
        )
        self.assertEqual(len(normalized_materials["sources"]), 7)
        self.assertEqual(len(normalized_questions["questions"]), 8)
        self.assertEqual(normalized_questions["questions"][0]["case_id"], "case-q1")
        normalized_prediction = hub._normalize_prediction_case_ids(
            {"predictions": [{"case_id": "q1"}]}, normalized_questions
        )
        self.assertEqual(
            normalized_prediction["predictions"][0]["case_id"], "case-q1"
        )
        with task_temp_dir() as tmp:
            root = Path(tmp)
            out = root / "public-freeze.json"
            gold_path = root / "synthetic-gold.json"
            corpus_path = root / "public-corpus.json"
            gold_path.write_text(
                json.dumps(make_synthetic_abstention_gold({
                    "questions": [
                        {"q_id": question["q_id"]}
                        for question in normalized_questions["questions"]
                    ]
                })),
                encoding="utf-8",
            )
            self.assertEqual(
                hub.main([
                    "freeze", "--materials", str(materials), "--questions",
                    str(questions), "--out", str(out),
                ]),
                0,
            )
            manifest = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(
                hub.main([
                    "project", "--materials", str(materials), "--questions",
                    str(questions), "--gold", str(gold_path),
                    "--freeze-manifest", str(out), "--out", str(corpus_path),
                ]),
                0,
            )
            corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["source_count"], 7)
        self.assertEqual(manifest["question_count"], 8)
        self.assertEqual(len(corpus["manifest"]), 7)
        self.assertEqual(len(corpus["cases"]), 8)
        self.assertEqual(
            [case["case_id"] for case in corpus["cases"]],
            [question["case_id"] for question in normalized_questions["questions"]],
        )
        self.assertTrue(all(case["expected"]["answer"] is None for case in corpus["cases"]))
        self.assertEqual(manifest["materials_sha256"], hub.sha256_file(materials))
        self.assertEqual(manifest["questions_sha256"], hub.sha256_file(questions))
        self.assertEqual(materials.read_bytes(), material_bytes)
        self.assertEqual(questions.read_bytes(), question_bytes)

    def test_cli_refuses_input_alias_and_existing_output(self) -> None:
        with task_temp_dir() as tmp:
            root = Path(tmp)
            p = self._write_inputs(root)
            original = p["materials"].read_bytes()
            self.assertEqual(
                hub.main([
                    "freeze", "--materials", str(p["materials"]), "--questions",
                    str(p["questions"]), "--out", str(p["materials"]),
                ]),
                2,
            )
            self.assertEqual(p["materials"].read_bytes(), original)

            existing = root / "existing.json"
            existing.write_text("keep this", encoding="utf-8")
            self.assertEqual(
                hub.main([
                    "freeze", "--materials", str(p["materials"]), "--questions",
                    str(p["questions"]), "--out", str(existing),
                ]),
                2,
            )
            self.assertEqual(existing.read_text(encoding="utf-8"), "keep this")

    def test_cli_rejects_symlink_output_escape(self) -> None:
        with task_temp_dir() as tmp:
            root = Path(tmp)
            p = self._write_inputs(root)
            target = root / "target"
            target.mkdir()
            link = root / "linked-output"
            try:
                link.symlink_to(target, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("directory symlink creation is unavailable")
            out = link / "freeze.json"
            self.assertEqual(
                hub.main([
                    "freeze", "--materials", str(p["materials"]), "--questions",
                    str(p["questions"]), "--out", str(out),
                ]),
                2,
            )
            self.assertFalse((target / "freeze.json").exists())


if __name__ == "__main__":
    sys.exit(unittest.main())
