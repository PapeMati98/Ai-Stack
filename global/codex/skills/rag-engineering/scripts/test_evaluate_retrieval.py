"""Deterministic tests for the rag-engineering evaluator.

The tests are stdlib-only and never spawn a subprocess. They cover:

- gold corpus schema validation (happy path + adversarial malformed cases)
- predictions schema validation (critical format errors)
- per-case verdicts (answer correctness, abstention, citation precision/recall,
  temporal revision, project isolation, alias resolution, false merge)
- aggregate gates and the rule that critical failures cannot be averaged
  away
- CLI entry points: validate-corpus, evaluate, compare

Tests run with:

    python -m unittest test_evaluate_retrieval
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path


DEFAULT_ARTIFACT_ROOT = Path(__file__).resolve().parent / '.test-artifacts' / 'rag'
_ARTIFACT_ROOT: Path | None = None


def set_artifact_root(path: Path | str) -> None:
    global _ARTIFACT_ROOT
    _ARTIFACT_ROOT = Path(path).resolve()
    _ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)


def get_artifact_root() -> Path:
    global _ARTIFACT_ROOT
    if _ARTIFACT_ROOT is None:
        env_root = os.environ.get("RAG_ARTIFACT_ROOT")
        if env_root:
            _ARTIFACT_ROOT = Path(env_root).resolve()
        else:
            _ARTIFACT_ROOT = DEFAULT_ARTIFACT_ROOT.resolve()
    _ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    return _ARTIFACT_ROOT


HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "evaluate_retrieval.py"
SPEC = importlib.util.spec_from_file_location("evaluate_retrieval", SCRIPT)
assert SPEC and SPEC.loader
evaluator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evaluator)

FIXTURES = HERE.parent / "fixtures"
GOLD_PATH = FIXTURES / "gold-corpus.json"
BASELINE_PATH = FIXTURES / "baseline-predictions.json"
CANDIDATE_PATH = FIXTURES / "candidate-predictions.json"


def write_json(path: Path | str, payload: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def load_json(path: Path | str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def make_minimal_corpus(case_overrides: list[dict] | None = None) -> dict:
    """Build a minimal, valid gold corpus for ad-hoc tests."""

    cases = case_overrides if case_overrides is not None else [
        {
            "case_id": "case-1",
            "question": "What does the handbook say?",
            "temporal_intent": "current",
            "adversarial_kind": "baseline_correctness",
            "expected": {
                "answer": "role-based training",
                "answer_match_policy": "contains",
                "abstain_expected": False,
                "required_sources": ["handbook-current"],
                "allowed_revisions": ["v2"],
                "forbidden_sources": [],
            },
        }
    ]
    return {
        "schema_version": "1.0.0",
        "corpus_id": "synth-test",
        "license": "public-domain",
        "manifest": [
            {
                "source_id": "handbook-current",
                "project_id": "atlas",
                "title": "Current handbook",
                "revision": "v2",
                "valid_from": "2026-01-01T00:00:00Z",
                "valid_until": None,
                "supersedes": ["handbook-superseded"],
                "superseded_by": None,
                "aliases": [],
            },
            {
                "source_id": "handbook-superseded",
                "project_id": "atlas",
                "title": "Superseded handbook",
                "revision": "v1",
                "valid_from": "2024-01-01T00:00:00Z",
                "valid_until": "2025-12-31T23:59:59Z",
                "supersedes": [],
                "superseded_by": "handbook-current",
                "aliases": [],
            },
            {
                "source_id": "phoenix-a",
                "project_id": "atlas",
                "title": "Phoenix A",
                "revision": "v1",
                "valid_from": "2026-01-01T00:00:00Z",
                "valid_until": None,
                "supersedes": [],
                "superseded_by": None,
                "aliases": [],
            },
            {
                "source_id": "phoenix-b",
                "project_id": "hyperion",
                "title": "Phoenix B",
                "revision": "v1",
                "valid_from": "2026-01-01T00:00:00Z",
                "valid_until": None,
                "supersedes": [],
                "superseded_by": None,
                "aliases": [],
            },
            {
                "source_id": "forbidden-doc",
                "project_id": "forbidden-project",
                "title": "Forbidden",
                "revision": "v1",
                "valid_from": "2026-01-01T00:00:00Z",
                "valid_until": None,
                "supersedes": [],
                "superseded_by": None,
                "aliases": [],
            },
        ],
        "alias_groups": [
            {
                "alias_group_id": "phoenix-name-collision",
                "canonical_source_id": "phoenix-a",
                "other_source_ids": ["phoenix-b"],
                "rationale": "Two distinct entities share the name Phoenix.",
            }
        ],
        "forbidden_sources": [
            {
                "source_id": "forbidden-doc",
                "reason": "outside authorised scope",
                "never_cite": True,
            }
        ],
        "cases": cases,
    }


def make_prediction(case_id: str, answer: str | None = None, abstained: bool = False,
                    citations: list[dict] | None = None, rationale: str = "") -> dict:
    return {
        "case_id": case_id,
        "answer": answer,
        "abstained": abstained,
        "abstain_reason": "abstain reason" if abstained else None,
        "citations": citations or [],
        "rationale": rationale,
    }


def wrap_predictions(model_id: str, predictions: list[dict]) -> dict:
    return {
        "schema_version": "1.0.0",
        "model_id": model_id,
        "model_kind": "candidate",
        "predictions": predictions,
    }


class CorpusValidationTests(unittest.TestCase):
    def test_minimal_corpus_validates(self):
        corpus = make_minimal_corpus()
        view = evaluator.validate_corpus(corpus)
        self.assertEqual(view["corpus_id"], "synth-test")
        self.assertEqual(set(view["manifest"].keys()), {
            "handbook-current", "handbook-superseded", "phoenix-a",
            "phoenix-b", "forbidden-doc",
        })

    def test_missing_schema_version_rejected(self):
        corpus = make_minimal_corpus()
        del corpus["schema_version"]
        with self.assertRaises(SystemExit):
            evaluator.validate_corpus(corpus)

    def test_wrong_schema_version_rejected(self):
        corpus = make_minimal_corpus()
        corpus["schema_version"] = "9.9.9"
        with self.assertRaises(SystemExit):
            evaluator.validate_corpus(corpus)

    def test_unknown_license_rejected(self):
        corpus = make_minimal_corpus()
        corpus["license"] = "totally-made-up"
        with self.assertRaises(SystemExit):
            evaluator.validate_corpus(corpus)

    def test_duplicate_source_id_rejected(self):
        corpus = make_minimal_corpus()
        corpus["manifest"].append(dict(corpus["manifest"][0]))
        with self.assertRaises(SystemExit):
            evaluator.validate_corpus(corpus)

    def test_duplicate_case_id_rejected(self):
        cases = [
            make_minimal_corpus()[0] if False else None,  # placeholder
        ]
        # Build two cases with the same case_id directly.
        corpus = make_minimal_corpus([
            {
                "case_id": "dup",
                "question": "x",
                "temporal_intent": "any",
                "adversarial_kind": "baseline_correctness",
                "expected": {
                    "answer": "a",
                    "answer_match_policy": "case_insensitive",
                    "abstain_expected": False,
                    "required_sources": [],
                    "allowed_revisions": None,
                    "forbidden_sources": [],
                },
            },
            {
                "case_id": "dup",
                "question": "y",
                "temporal_intent": "any",
                "adversarial_kind": "baseline_correctness",
                "expected": {
                    "answer": "b",
                    "answer_match_policy": "case_insensitive",
                    "abstain_expected": False,
                    "required_sources": [],
                    "allowed_revisions": None,
                    "forbidden_sources": [],
                },
            },
        ])
        with self.assertRaises(SystemExit):
            evaluator.validate_corpus(corpus)

    def test_supersedes_self_rejected(self):
        corpus = make_minimal_corpus()
        corpus["manifest"][0]["supersedes"] = ["handbook-current"]
        with self.assertRaises(SystemExit):
            evaluator.validate_corpus(corpus)

    def test_supersedes_unknown_source_rejected(self):
        corpus = make_minimal_corpus()
        corpus["manifest"][0]["supersedes"] = ["ghost-source"]
        with self.assertRaises(SystemExit):
            evaluator.validate_corpus(corpus)

    def test_forbidden_source_not_in_manifest_rejected(self):
        corpus = make_minimal_corpus()
        corpus["forbidden_sources"].append({
            "source_id": "ghost",
            "reason": "x",
            "never_cite": True,
        })
        with self.assertRaises(SystemExit):
            evaluator.validate_corpus(corpus)

    def test_forbidden_source_missing_never_cite_rejected(self):
        corpus = make_minimal_corpus()
        corpus["forbidden_sources"].append({
            "source_id": "forbidden-doc",
            "reason": "x",
            "never_cite": False,
        })
        with self.assertRaises(SystemExit):
            evaluator.validate_corpus(corpus)

    def test_alias_group_canonical_in_other_rejected(self):
        corpus = make_minimal_corpus()
        corpus["alias_groups"][0]["other_source_ids"] = [
            "phoenix-b", "phoenix-a"  # canonical erroneously listed
        ]
        with self.assertRaises(SystemExit):
            evaluator.validate_corpus(corpus)

    def test_unzoned_timestamp_rejected(self):
        corpus = make_minimal_corpus()
        corpus["manifest"][0]["valid_from"] = "2026-01-01T00:00:00"
        with self.assertRaises(SystemExit):
            evaluator.validate_corpus(corpus)

    def test_adversarial_kind_enum_enforced(self):
        corpus = make_minimal_corpus([
            {
                "case_id": "case-x",
                "question": "?",
                "temporal_intent": "any",
                "adversarial_kind": "not-a-real-kind",
                "expected": {
                    "answer": "x",
                    "answer_match_policy": "case_insensitive",
                    "abstain_expected": False,
                    "required_sources": [],
                    "allowed_revisions": None,
                    "forbidden_sources": [],
                },
            }
        ])
        with self.assertRaises(SystemExit):
            evaluator.validate_corpus(corpus)

    def test_abstain_expected_with_non_abstain_policy_rejected(self):
        corpus = make_minimal_corpus([
            {
                "case_id": "case-abstain",
                "question": "?",
                "temporal_intent": "any",
                "adversarial_kind": "unknown_answer",
                "expected": {
                    "answer": None,
                    "answer_match_policy": "contains",
                    "abstain_expected": True,
                    "required_sources": [],
                    "allowed_revisions": None,
                    "forbidden_sources": [],
                },
            }
        ])
        with self.assertRaises(SystemExit):
            evaluator.validate_corpus(corpus)

    def test_abstain_expected_with_non_null_answer_rejected(self):
        corpus = make_minimal_corpus([
            {
                "case_id": "case-abstain",
                "question": "?",
                "temporal_intent": "any",
                "adversarial_kind": "unknown_answer",
                "expected": {
                    "answer": "cannot answer",
                    "answer_match_policy": "abstain",
                    "abstain_expected": True,
                    "required_sources": [],
                    "allowed_revisions": None,
                    "forbidden_sources": [],
                },
            }
        ])
        with self.assertRaises(SystemExit):
            evaluator.validate_corpus(corpus)

    def test_abstain_false_with_null_answer_rejected(self):
        corpus = make_minimal_corpus([
            {
                "case_id": "case-no-answer",
                "question": "What is the answer?",
                "temporal_intent": "any",
                "adversarial_kind": "baseline_correctness",
                "expected": {
                    "answer": None,
                    "answer_match_policy": "contains",
                    "abstain_expected": False,
                    "required_sources": ["handbook-current"],
                    "allowed_revisions": None,
                    "forbidden_sources": [],
                },
            }
        ])
        with self.assertRaises(SystemExit):
            evaluator.validate_corpus(corpus)

    def test_abstain_false_with_empty_answer_rejected(self):
        corpus = make_minimal_corpus([
            {
                "case_id": "case-empty-answer",
                "question": "What is the answer?",
                "temporal_intent": "any",
                "adversarial_kind": "baseline_correctness",
                "expected": {
                    "answer": "   ",
                    "answer_match_policy": "contains",
                    "abstain_expected": False,
                    "required_sources": ["handbook-current"],
                    "allowed_revisions": None,
                    "forbidden_sources": [],
                },
            }
        ])
        with self.assertRaises(SystemExit):
            evaluator.validate_corpus(corpus)


class PredictionsValidationTests(unittest.TestCase):
    def test_minimal_predictions_validates(self):
        payload = wrap_predictions("m1", [
                make_prediction("case-1", answer="x", abstained=False,
                                citations=[{
                                    "source_id": "handbook-current",
                                    "chunk_id": "c",
                                    "page": 1,
                                    "revision": "v2",
                                    "quote": "q",
                                    "confidence": 0.5,
                                    "timestamp_zone": "UTC",
                                    "retrieved_at": "2026-09-15T10:00:00Z",
                                }])
            ])
        view = evaluator.validate_predictions(payload)
        self.assertEqual(view["model_id"], "m1")
        self.assertIn("case-1", view["predictions"])

    def test_wrong_schema_version_rejected(self):
        payload = wrap_predictions("m1", [])
        payload["schema_version"] = "9.9.9"
        with self.assertRaises(SystemExit):
            evaluator.validate_predictions(payload)

    def test_duplicate_case_id_rejected(self):
        payload = wrap_predictions("m1", [
            make_prediction("case-1"),
            make_prediction("case-1"),
        ])
        with self.assertRaises(SystemExit):
            evaluator.validate_predictions(payload)

    def test_abstain_with_answer_rejected(self):
        payload = wrap_predictions("m1", [
            make_prediction("case-1", answer="x", abstained=True)
        ])
        with self.assertRaises(SystemExit):
            evaluator.validate_predictions(payload)

    def test_unknown_timestamp_zone_rejected(self):
        payload = wrap_predictions("m1", [
            make_prediction("case-1", answer="x", abstained=False,
                            citations=[{
                                "source_id": "handbook-current",
                                "chunk_id": "c",
                                "page": 1,
                                "revision": "v2",
                                "quote": "q",
                                "confidence": 0.5,
                                "timestamp_zone": "Mars/Olympus",
                                "retrieved_at": "2026-09-15T10:00:00Z",
                            }])
        ])
        with self.assertRaises(SystemExit):
            evaluator.validate_predictions(payload)

    def test_unzoned_retrieved_at_rejected(self):
        payload = wrap_predictions("m1", [
            make_prediction("case-1", answer="x", abstained=False,
                            citations=[{
                                "source_id": "handbook-current",
                                "chunk_id": "c",
                                "page": 1,
                                "revision": "v2",
                                "quote": "q",
                                "confidence": 0.5,
                                "timestamp_zone": "UTC",
                                "retrieved_at": "2026-09-15T10:00:00",
                            }])
        ])
        with self.assertRaises(SystemExit):
            evaluator.validate_predictions(payload)

    def test_confidence_out_of_range_rejected(self):
        payload = wrap_predictions("m1", [
            make_prediction("case-1", answer="x", abstained=False,
                            citations=[{
                                "source_id": "handbook-current",
                                "chunk_id": "c",
                                "page": 1,
                                "revision": "v2",
                                "quote": "q",
                                "confidence": 1.5,
                                "timestamp_zone": "UTC",
                                "retrieved_at": "2026-09-15T10:00:00Z",
                            }])
        ])
        with self.assertRaises(SystemExit):
            evaluator.validate_predictions(payload)


class EvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = get_artifact_root() / "evaluation" / self._testMethodName
        self.root.mkdir(parents=True, exist_ok=True)
        self.corpus_path = self.root / "corpus.json"
        self.predictions_path = self.root / "predictions.json"
        write_json(self.corpus_path, make_minimal_corpus())

    def tearDown(self) -> None:
        # Intentionally retain synthetic fixtures per rule: no cleanup/rmtree/unlink.
        pass

    def _write_predictions(self, payload: dict) -> None:
        write_json(self.predictions_path, payload)

    def test_correct_answer_with_citation_passes(self):
        self._write_predictions(wrap_predictions("m1", [
            make_prediction("case-1", answer="role-based training", abstained=False,
                            citations=[{
                                "source_id": "handbook-current",
                                "chunk_id": "c",
                                "page": 1,
                                "revision": "v2",
                                "quote": "role-based training",
                                "confidence": 0.9,
                                "timestamp_zone": "UTC",
                                "retrieved_at": "2026-09-15T10:00:00Z",
                            }])
        ]))
        report = evaluator.evaluate_bundle(self.corpus_path, self.predictions_path)
        self.assertEqual(report["overall_status"], "PASS")
        self.assertEqual(report["totals"]["critical_failures"], 0)
        case = report["by_case"][0]
        self.assertEqual(case["answer_correctness"], "PASS")
        self.assertEqual(case["abstention_correctness"], "PASS")
        self.assertEqual(case["citation_precision"], 1.0)
        self.assertEqual(case["citation_recall"], 1.0)
        self.assertEqual(case["temporal_revision_correctness"], "PASS")
        self.assertEqual(case["project_isolation"], "PASS")

    def test_unknown_answer_requires_abstention(self):
        self._write_predictions(wrap_predictions("m1", [
            make_prediction("case-1", answer="leaked", abstained=False,
                            citations=[{
                                "source_id": "handbook-current",
                                "chunk_id": "c",
                                "page": 1,
                                "revision": "v2",
                                "quote": "leaked",
                                "confidence": 0.7,
                                "timestamp_zone": "UTC",
                                "retrieved_at": "2026-09-15T10:00:00Z",
                            }])
        ]))
        # Override corpus to demand abstention.
        corpus = make_minimal_corpus([
            {
                "case_id": "case-1",
                "question": "leak me",
                "temporal_intent": "any",
                "adversarial_kind": "unknown_answer",
                "expected": {
                    "answer": None,
                    "answer_match_policy": "abstain",
                    "abstain_expected": True,
                    "required_sources": [],
                    "allowed_revisions": None,
                    "forbidden_sources": [],
                },
            }
        ])
        write_json(self.corpus_path, corpus)
        report = evaluator.evaluate_bundle(self.corpus_path, self.predictions_path)
        self.assertEqual(report["overall_status"], "FAIL")
        self.assertIn("critical_failures_present", report["failure_gates"])
        case = report["by_case"][0]
        self.assertEqual(case["answer_correctness"], "FAIL")
        self.assertEqual(case["abstention_correctness"], "FAIL")
        self.assertIn("unsupported_critical_answer", case["critical_reasons"])

    def test_forbidden_project_citation_is_critical(self):
        self._write_predictions(wrap_predictions("m1", [
            make_prediction("case-1", answer="role-based training", abstained=False,
                            citations=[{
                                "source_id": "forbidden-doc",
                                "chunk_id": "c",
                                "page": 1,
                                "revision": "v1",
                                "quote": "role-based training",
                                "confidence": 0.8,
                                "timestamp_zone": "UTC",
                                "retrieved_at": "2026-09-15T10:00:00Z",
                            }])
        ]))
        report = evaluator.evaluate_bundle(self.corpus_path, self.predictions_path)
        self.assertEqual(report["overall_status"], "FAIL")
        self.assertIn("forbidden_project_disclosure", report["failure_gates"])
        case = report["by_case"][0]
        self.assertIn("forbidden_project_disclosure", case["critical_reasons"])

    def test_temporal_revision_violation_is_critical(self):
        self._write_predictions(wrap_predictions("m1", [
            make_prediction("case-1", answer="role-based training", abstained=False,
                            citations=[{
                                "source_id": "handbook-superseded",
                                "chunk_id": "c",
                                "page": 1,
                                "revision": "v1",
                                "quote": "role-based training",
                                "confidence": 0.8,
                                "timestamp_zone": "UTC",
                                "retrieved_at": "2026-09-15T10:00:00Z",
                            }])
        ]))
        report = evaluator.evaluate_bundle(self.corpus_path, self.predictions_path)
        case = report["by_case"][0]
        self.assertEqual(case["temporal_revision_correctness"], "FAIL")
        self.assertIn("invalid_citation", case["critical_reasons"])
        self.assertEqual(report["overall_status"], "FAIL")

    def test_alias_collision_no_false_merge(self):
        # Prediction cites canonical only and explicitly notes the
        # distinct other entity.
        self._write_predictions(wrap_predictions("m1", [
            make_prediction(
                "alias-case",
                answer=("Phoenix is a product in Atlas; the Hyperion personnel record "
                        "is a distinct entity and must NOT be merged."),
                abstained=False,
                citations=[{
                    "source_id": "phoenix-a",
                    "chunk_id": "c",
                    "page": 1,
                    "revision": "v1",
                    "quote": "Phoenix product spec",
                    "confidence": 0.85,
                    "timestamp_zone": "UTC",
                    "retrieved_at": "2026-09-15T10:00:00Z",
                }],
                rationale="Two distinct entities share the name Phoenix.",
            )
        ]))
        corpus = make_minimal_corpus([
            {
                "case_id": "alias-case",
                "question": "Find me Phoenix?",
                "temporal_intent": "any",
                "adversarial_kind": "alias_collision",
                "expected": {
                    "answer": "Phoenix is a product in Atlas.",
                    "answer_match_policy": "contains",
                    "abstain_expected": False,
                    "required_sources": ["phoenix-a"],
                    "allowed_revisions": None,
                    "forbidden_sources": [],
                    "must_disambiguate_alias_group": "phoenix-name-collision",
                },
            }
        ])
        write_json(self.corpus_path, corpus)
        report = evaluator.evaluate_bundle(self.corpus_path, self.predictions_path)
        case = report["by_case"][0]
        self.assertEqual(case["alias_resolution"], "PASS")
        self.assertEqual(case["no_false_merge"], "PASS")
        self.assertNotIn("false_merge", case["critical_reasons"])

    def test_alias_collision_false_merge_is_critical(self):
        self._write_predictions(wrap_predictions("m1", [
            make_prediction(
                "alias-case",
                answer="Phoenix is the same entity across both projects.",
                abstained=False,
                citations=[
                    {
                        "source_id": "phoenix-a",
                        "chunk_id": "c",
                        "page": 1,
                        "revision": "v1",
                        "quote": "Phoenix product",
                        "confidence": 0.8,
                        "timestamp_zone": "UTC",
                        "retrieved_at": "2026-09-15T10:00:00Z",
                    },
                    {
                        "source_id": "phoenix-b",
                        "chunk_id": "c",
                        "page": 1,
                        "revision": "v1",
                        "quote": "Phoenix personnel",
                        "confidence": 0.8,
                        "timestamp_zone": "UTC",
                        "retrieved_at": "2026-09-15T10:00:00Z",
                    },
                ],
                rationale="Both records refer to the same entity.",
            )
        ]))
        corpus = make_minimal_corpus([
            {
                "case_id": "alias-case",
                "question": "Find me Phoenix?",
                "temporal_intent": "any",
                "adversarial_kind": "alias_collision",
                "expected": {
                    "answer": "Phoenix is a product in Atlas.",
                    "answer_match_policy": "contains",
                    "abstain_expected": False,
                    "required_sources": ["phoenix-a"],
                    "allowed_revisions": None,
                    "forbidden_sources": ["phoenix-b"],
                    "must_disambiguate_alias_group": "phoenix-name-collision",
                },
            }
        ])
        write_json(self.corpus_path, corpus)
        report = evaluator.evaluate_bundle(self.corpus_path, self.predictions_path)
        case = report["by_case"][0]
        self.assertEqual(case["no_false_merge"], "FAIL")
        self.assertIn("false_merge", case["critical_reasons"])
        self.assertEqual(report["overall_status"], "FAIL")

    def test_alias_collision_citing_both_with_disambiguation_passes(self):
        self._write_predictions(wrap_predictions("m1", [
            make_prediction(
                "alias-case",
                answer="Phoenix product in Atlas and Phoenix personnel in Hyperion are not the same entity.",
                abstained=False,
                citations=[
                    {
                        "source_id": "phoenix-a",
                        "chunk_id": "c",
                        "page": 1,
                        "revision": "v1",
                        "quote": "Phoenix product spec",
                        "confidence": 0.85,
                        "timestamp_zone": "UTC",
                        "retrieved_at": "2026-09-15T10:00:00Z",
                    },
                    {
                        "source_id": "phoenix-b",
                        "chunk_id": "c",
                        "page": 1,
                        "revision": "v1",
                        "quote": "Phoenix personnel record",
                        "confidence": 0.85,
                        "timestamp_zone": "UTC",
                        "retrieved_at": "2026-09-15T10:00:00Z",
                    },
                ],
                rationale="The two records are distinct entities and must not be merged.",
            )
        ]))
        corpus = make_minimal_corpus([
            {
                "case_id": "alias-case",
                "question": "Find me Phoenix?",
                "temporal_intent": "any",
                "adversarial_kind": "alias_collision",
                "expected": {
                    "answer": "not the same entity",
                    "answer_match_policy": "contains",
                    "abstain_expected": False,
                    "required_sources": ["phoenix-a"],
                    "allowed_revisions": None,
                    "forbidden_sources": [],
                    "must_disambiguate_alias_group": "phoenix-name-collision",
                },
            }
        ])
        write_json(self.corpus_path, corpus)
        report = evaluator.evaluate_bundle(self.corpus_path, self.predictions_path)
        case = report["by_case"][0]
        self.assertEqual(case["no_false_merge"], "PASS")
        self.assertEqual(case["alias_resolution"], "PASS")
        self.assertNotIn("false_merge", case["critical_reasons"])

    def test_alias_collision_alternative_phrasings_fail_false_merge(self):
        phrasings = [
            ("Phoenix in Atlas and Phoenix in Hyperion are one and the same.", "one and the same"),
            ("The two Phoenix records are merged into one record.", "merged into one record"),
            ("The two records were merged as a single entity.", "merged as a single entity"),
        ]
        for answer_text, desc in phrasings:
            with self.subTest(phrase=desc):
                self._write_predictions(wrap_predictions("m1", [
                    make_prediction(
                        "alias-case",
                        answer=answer_text,
                        abstained=False,
                        citations=[{
                            "source_id": "phoenix-a",
                            "chunk_id": "c",
                            "page": 1,
                            "revision": "v1",
                            "quote": "Phoenix product spec",
                            "confidence": 0.8,
                            "timestamp_zone": "UTC",
                            "retrieved_at": "2026-09-15T10:00:00Z",
                        }],
                        rationale="Merged record.",
                    )
                ]))
                corpus = make_minimal_corpus([
                    {
                        "case_id": "alias-case",
                        "question": "Find me Phoenix?",
                        "temporal_intent": "any",
                        "adversarial_kind": "alias_collision",
                        "expected": {
                            "answer": "Phoenix product",
                            "answer_match_policy": "contains",
                            "abstain_expected": False,
                            "required_sources": ["phoenix-a"],
                            "allowed_revisions": None,
                            "forbidden_sources": [],
                            "must_disambiguate_alias_group": "phoenix-name-collision",
                        },
                    }
                ])
                write_json(self.corpus_path, corpus)
                report = evaluator.evaluate_bundle(self.corpus_path, self.predictions_path)
                case = report["by_case"][0]
                self.assertEqual(case["no_false_merge"], "FAIL")
                self.assertIn("false_merge", case["critical_reasons"])

    def test_missing_prediction_is_critical(self):
        self._write_predictions(wrap_predictions("m1", []))
        report = evaluator.evaluate_bundle(self.corpus_path, self.predictions_path)
        case = report["by_case"][0]
        self.assertFalse(case["present_in_predictions"])
        self.assertIn("missing_prediction", case["critical_reasons"])
        self.assertEqual(report["overall_status"], "FAIL")
        self.assertIn("critical_failures_present", report["failure_gates"])

    def test_citation_to_unknown_source_is_critical(self):
        self._write_predictions(wrap_predictions("m1", [
            make_prediction("case-1", answer="role-based training", abstained=False,
                            citations=[{
                                "source_id": "ghost-source",
                                "chunk_id": "c",
                                "page": 1,
                                "revision": "v1",
                                "quote": "role-based training",
                                "confidence": 0.8,
                                "timestamp_zone": "UTC",
                                "retrieved_at": "2026-09-15T10:00:00Z",
                            }])
        ]))
        report = evaluator.evaluate_bundle(self.corpus_path, self.predictions_path)
        case = report["by_case"][0]
        self.assertEqual(case["citation_format_valid"], "FAIL")
        self.assertIn("invalid_citation", case["critical_reasons"])
        self.assertEqual(report["overall_status"], "FAIL")

    def test_citation_revision_mismatch_is_critical(self):
        self._write_predictions(wrap_predictions("m1", [
            make_prediction("case-1", answer="role-based training", abstained=False,
                            citations=[{
                                "source_id": "handbook-current",
                                "chunk_id": "c",
                                "page": 1,
                                "revision": "v9",
                                "quote": "role-based training",
                                "confidence": 0.8,
                                "timestamp_zone": "UTC",
                                "retrieved_at": "2026-09-15T10:00:00Z",
                            }])
        ]))
        report = evaluator.evaluate_bundle(self.corpus_path, self.predictions_path)
        case = report["by_case"][0]
        self.assertEqual(case["citation_format_valid"], "FAIL")
        self.assertIn("invalid_citation", case["critical_reasons"])

    def test_averages_cannot_mask_critical_failures(self):
        # Build a corpus with 4 trivial correct cases plus 1 critical failure.
        cases = []
        for i in range(4):
            cases.append({
                "case_id": f"ok-{i}",
                "question": "x",
                "temporal_intent": "current",
                "adversarial_kind": "baseline_correctness",
                "expected": {
                    "answer": "role-based training",
                    "answer_match_policy": "contains",
                    "abstain_expected": False,
                    "required_sources": ["handbook-current"],
                    "allowed_revisions": ["v2"],
                    "forbidden_sources": [],
                },
            })
        cases.append({
            "case_id": "critical-1",
            "question": "forbidden",
            "temporal_intent": "any",
            "adversarial_kind": "forbidden_project",
            "expected": {
                "answer": None,
                "answer_match_policy": "abstain",
                "abstain_expected": True,
                "required_sources": [],
                "allowed_revisions": None,
                "forbidden_sources": ["forbidden-doc"],
            },
        })
        corpus = make_minimal_corpus(cases)
        write_json(self.corpus_path, corpus)

        predictions = []
        for i in range(4):
            predictions.append(make_prediction(
                f"ok-{i}", answer="role-based training", abstained=False,
                citations=[{
                    "source_id": "handbook-current",
                    "chunk_id": "c",
                    "page": 1,
                    "revision": "v2",
                    "quote": "role-based training",
                    "confidence": 0.9,
                    "timestamp_zone": "UTC",
                    "retrieved_at": "2026-09-15T10:00:00Z",
                }]
            ))
        # Critical: cite the forbidden source AND answer.
        predictions.append(make_prediction(
            "critical-1", answer="leaked", abstained=False,
            citations=[{
                "source_id": "forbidden-doc",
                "chunk_id": "c",
                "page": 1,
                "revision": "v1",
                "quote": "leaked",
                "confidence": 0.9,
                "timestamp_zone": "UTC",
                "retrieved_at": "2026-09-15T10:00:00Z",
            }]
        ))
        self._write_predictions(wrap_predictions("m1", predictions))
        report = evaluator.evaluate_bundle(self.corpus_path, self.predictions_path)
        # Even with a 4/5 = 0.8 average, status must FAIL.
        self.assertEqual(report["overall_status"], "FAIL")
        self.assertIn("critical_failures_present", report["failure_gates"])
        self.assertIn("critical-1", report["totals"]["critical_failure_cases"])

    def test_evaluate_case_missing_gold_answer_is_unsupported_critical_answer(self):
        corpus_view = evaluator.validate_corpus(make_minimal_corpus())
        case = {
            "case_id": "case-1",
            "question": "question?",
            "temporal_intent": "current",
            "adversarial_kind": "baseline_correctness",
            "expected": {
                "answer": None,
                "answer_match_policy": "contains",
                "abstain_expected": False,
                "required_sources": ["handbook-current"],
                "allowed_revisions": ["v2"],
                "forbidden_sources": [],
            },
        }
        pred = make_prediction("case-1", answer="some answer", abstained=False,
                               citations=[{
                                   "source_id": "handbook-current",
                                   "chunk_id": "c",
                                   "page": 1,
                                   "revision": "v2",
                                   "quote": "some answer",
                                   "confidence": 0.9,
                                   "timestamp_zone": "UTC",
                                   "retrieved_at": "2026-09-15T10:00:00Z",
                               }])
        result = evaluator.evaluate_case(case, pred, corpus_view)
        self.assertEqual(result["answer_correctness"], "FAIL")
        self.assertIn("unsupported_critical_answer", result["critical_reasons"])
        self.assertTrue(result["critical_failure"])

    def test_duplicate_advisory_explicit_disclosure_passes(self):
        case = {
            "case_id": "dup-case",
            "question": "Advisory?",
            "temporal_intent": "current",
            "adversarial_kind": "duplicate_advisory",
            "expected": {
                "answer": "Duplicate advisory.",
                "answer_match_policy": "contains",
                "abstain_expected": False,
                "required_sources": ["handbook-current"],
                "allowed_revisions": None,
                "forbidden_sources": [],
            },
        }
        pred = make_prediction(
            "dup-case",
            answer="Duplicate advisory.",
            abstained=False,
            citations=[{
                "source_id": "handbook-current",
                "chunk_id": "c",
                "page": 1,
                "revision": "v2",
                "quote": "Duplicate advisory.",
                "confidence": 0.9,
                "timestamp_zone": "UTC",
                "retrieved_at": "2026-09-15T10:00:00Z",
            }],
            rationale="Candidate explicitly discloses the duplicate advisory and cites them both.",
        )
        corpus_view = evaluator.validate_corpus(make_minimal_corpus())
        result = evaluator.evaluate_case(case, pred, corpus_view)
        self.assertEqual(result["errors"], [])

    def test_duplicate_advisory_negated_disclosure_fails_check(self):
        case = {
            "case_id": "dup-case",
            "question": "Advisory?",
            "temporal_intent": "current",
            "adversarial_kind": "duplicate_advisory",
            "expected": {
                "answer": "Duplicate advisory.",
                "answer_match_policy": "contains",
                "abstain_expected": False,
                "required_sources": ["handbook-current"],
                "allowed_revisions": None,
                "forbidden_sources": [],
            },
        }
        phrasings = [
            "Baseline cited one channel without disclosing duplication.",
            "no duplicate was flagged in this review",
            "Regular advisory without duplicate markers.",
        ]
        corpus_view = evaluator.validate_corpus(make_minimal_corpus())
        for rationale in phrasings:
            with self.subTest(rationale=rationale):
                pred = make_prediction(
                    "dup-case",
                    answer="Advisory text.",
                    abstained=False,
                    citations=[{
                        "source_id": "handbook-current",
                        "chunk_id": "c",
                        "page": 1,
                        "revision": "v2",
                        "quote": "Advisory text.",
                        "confidence": 0.9,
                        "timestamp_zone": "UTC",
                        "retrieved_at": "2026-09-15T10:00:00Z",
                    }],
                    rationale=rationale,
                )
                result = evaluator.evaluate_case(case, pred, corpus_view)
                self.assertIn(
                    "duplicate_advisory case requires rationale to disclose the duplication",
                    result["errors"],
                )


class GoldCorpusFixtureTests(unittest.TestCase):
    """Run the evaluator end-to-end against the bundled gold corpus."""

    def test_baseline_report_fails_critical_gates(self):
        report = evaluator.evaluate_bundle(GOLD_PATH, BASELINE_PATH)
        self.assertEqual(report["model_kind"], "baseline")
        self.assertEqual(report["overall_status"], "FAIL")
        # Baseline exposes multiple critical failures (Falcon, secret, false merge).
        self.assertGreater(report["totals"]["critical_failures"], 0)
        for gate in (
            "critical_failures_present",
            "forbidden_project_disclosure",
        ):
            self.assertIn(gate, report["failure_gates"])

    def test_candidate_report_passes_critical_gates(self):
        report = evaluator.evaluate_bundle(GOLD_PATH, CANDIDATE_PATH)
        self.assertEqual(report["overall_status"], "PASS")
        self.assertEqual(report["totals"]["critical_failures"], 0)
        self.assertEqual(report["failure_gates"], [])
        # Required sources for required cases must all be cited.
        for case in report["by_case"]:
            self.assertEqual(case["project_isolation"], "PASS")

    def test_comparison_decision_promote_candidate(self):
        out_dir = get_artifact_root() / "gold_fixtures" / "comparison_decision" / "reports"
        out_dir.mkdir(parents=True, exist_ok=True)
        argv = [
            "evaluate_retrieval",
            "compare",
            "--corpus", str(GOLD_PATH),
            "--baseline", str(BASELINE_PATH),
            "--candidate", str(CANDIDATE_PATH),
            "--output-dir", str(out_dir),
        ]
        old_argv = sys.argv
        sys.argv = argv
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                rc = evaluator.main(argv[1:])
        finally:
            sys.argv = old_argv
            self.assertEqual(rc, 0)
            for name in ("baseline.report.json", "candidate.report.json", "comparison.json"):
                self.assertTrue((out_dir / name).exists(), f"missing {name}")
            comparison = json.loads((out_dir / "comparison.json").read_text(encoding="utf-8"))
            self.assertEqual(comparison["baseline"]["overall_status"], "FAIL")
            self.assertEqual(comparison["candidate"]["overall_status"], "PASS")
            self.assertEqual(comparison["delta"]["candidate_regressed_critical"], [])
            self.assertIn("promote", comparison["decision"])
            # Improvement claim must NOT be made.
            self.assertIn("NOT_MADE", comparison["improvement_claim"])

    def test_latency_token_cost_are_unknown(self):
        report = evaluator.evaluate_bundle(GOLD_PATH, CANDIDATE_PATH)
        self.assertEqual(report["latency_observed"], "UNKNOWN")
        self.assertEqual(report["token_observed"], "UNKNOWN")
        self.assertEqual(report["cost_observed"], "UNKNOWN")
        self.assertFalse(report["freshness_inferred"])


class MalformedFixtureTests(unittest.TestCase):
    """The malformed/ directory ships with deliberately invalid inputs."""

    def test_missing_schema_version(self):
        with self.assertRaises(SystemExit):
            evaluator.validate_corpus(
                load_json(FIXTURES / "malformed" / "missing-schema-version.json")
            )

    def test_wrong_schema_version(self):
        with self.assertRaises(SystemExit):
            evaluator.validate_corpus(
                load_json(FIXTURES / "malformed" / "wrong-schema-version.json")
            )

    def test_duplicate_case_id(self):
        with self.assertRaises(SystemExit):
            evaluator.validate_corpus(
                load_json(FIXTURES / "malformed" / "duplicate-case-id.json")
            )

    def test_invalid_json(self):
        with self.assertRaises(SystemExit):
            evaluator.read_json(FIXTURES / "malformed" / "not-json.txt")

    def test_citation_malformed_source_id(self):
        payload = load_json(FIXTURES / "malformed" / "citation-bad-source-id.json")
        with self.assertRaises(SystemExit):
            evaluator.validate_predictions(payload)

    def test_citation_to_unknown_source_id(self):
        # Alias for backward compatibility with older test harnesses
        self.test_citation_malformed_source_id()

    def test_bad_timestamp_zone(self):
        payload = load_json(FIXTURES / "malformed" / "bad-timestamp-zone.json")
        with self.assertRaises(SystemExit):
            evaluator.validate_predictions(payload)


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = get_artifact_root() / "cli" / self._testMethodName
        self.root.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        # Intentionally retain synthetic fixtures per rule: no cleanup/rmtree/unlink.
        pass

    def test_validate_corpus_subcommand(self):
        out_path = self.root / "corpus.json"
        write_json(out_path, make_minimal_corpus())
        argv = ["evaluate_retrieval", "validate-corpus", "--corpus", str(out_path)]
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = evaluator.main(argv[1:])
        self.assertEqual(rc, 0)

    def test_evaluate_subcommand_writes_report(self):
        corpus_path = self.root / "corpus.json"
        predictions_path = self.root / "predictions.json"
        out_path = self.root / "report.json"
        write_json(corpus_path, make_minimal_corpus())
        write_json(predictions_path, wrap_predictions("m1", [
            make_prediction("case-1", answer="role-based training", abstained=False,
                            citations=[{
                                "source_id": "handbook-current",
                                "chunk_id": "c",
                                "page": 1,
                                "revision": "v2",
                                "quote": "role-based training",
                                "confidence": 0.9,
                                "timestamp_zone": "UTC",
                                "retrieved_at": "2026-09-15T10:00:00Z",
                            }])
        ]))
        argv = [
            "evaluate_retrieval",
            "evaluate",
            "--corpus", str(corpus_path),
            "--predictions", str(predictions_path),
            "--output", str(out_path),
        ]
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = evaluator.main(argv[1:])
        self.assertEqual(rc, 0)
        self.assertTrue(out_path.exists())
        report = json.loads(out_path.read_text(encoding="utf-8"))
        self.assertEqual(report["model_id"], "m1")
        self.assertEqual(report["overall_status"], "PASS")

    def test_evaluate_subcommand_exits_nonzero_on_critical_failure(self):
        corpus_path = self.root / "corpus.json"
        predictions_path = self.root / "predictions.json"
        out_path = self.root / "report.json"
        write_json(corpus_path, make_minimal_corpus())
        write_json(predictions_path, wrap_predictions("m1", [
            make_prediction("case-1", answer="leaked", abstained=False,
                            citations=[{
                                "source_id": "forbidden-doc",
                                "chunk_id": "c",
                                "page": 1,
                                "revision": "v1",
                                "quote": "leaked",
                                "confidence": 0.9,
                                "timestamp_zone": "UTC",
                                "retrieved_at": "2026-09-15T10:00:00Z",
                            }])
        ]))
        argv = [
            "evaluate_retrieval",
            "evaluate",
            "--corpus", str(corpus_path),
            "--predictions", str(predictions_path),
            "--output", str(out_path),
        ]
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as exit_ctx:
                evaluator.main(argv[1:])
        self.assertEqual(exit_ctx.exception.code, 1)
        # Report JSON must still exist for downstream inspection.
        self.assertTrue(out_path.exists())
        report = json.loads(out_path.read_text(encoding="utf-8"))
        self.assertEqual(report["overall_status"], "FAIL")

    def test_selftest_subcommand(self):
        out_dir = self.root / "selftest_out"
        out_dir.mkdir(parents=True, exist_ok=True)
        argv = [
            "evaluate_retrieval",
            "selftest",
            "--output-dir", str(out_dir),
        ]
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = evaluator.main(argv[1:])
        self.assertEqual(rc, 0)
        for name in ("baseline.report.json", "candidate.report.json", "comparison.json", "selftest.json"):
            self.assertTrue((out_dir / name).exists(), f"missing {name}")
        selftest = json.loads((out_dir / "selftest.json").read_text(encoding="utf-8"))
        self.assertEqual(selftest["status"], "PASS")
        self.assertEqual(selftest["baseline_status"], "FAIL")
        self.assertEqual(selftest["candidate_status"], "PASS")

    def test_artifact_root_compatibility_retains_fixtures(self):
        root = get_artifact_root()
        self.assertTrue(root.exists())
        marker = root / "artifact_root_retained.json"
        write_json(marker, {"retained": True, "artifact_root": str(root)})
        self.assertTrue(marker.exists())


def parse_args_and_run() -> None:
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    argv = list(sys.argv)
    artifact_root: Path | None = None
    filtered_argv: list[str] = [argv[0]]
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--artifact-root":
            if i + 1 < len(argv):
                artifact_root = Path(argv[i + 1])
                i += 2
                continue
        elif arg.startswith("--artifact-root="):
            artifact_root = Path(arg.split("=", 1)[1])
            i += 1
            continue
        filtered_argv.append(arg)
        i += 1

    if artifact_root is not None:
        set_artifact_root(artifact_root)
    else:
        get_artifact_root()

    unittest.main(argv=filtered_argv)


if __name__ == "__main__":
    # Allow running directly: ``python test_evaluate_retrieval.py --artifact-root <dir>``.
    parse_args_and_run()
