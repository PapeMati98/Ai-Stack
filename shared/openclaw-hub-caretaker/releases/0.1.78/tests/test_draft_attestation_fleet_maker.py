"""Tests for Fleet-written draft attestation in Caretaker 0.1.41."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from caretaker.publication import (
    PublicationError,
    _is_caretaker_draft,
    build_candidate,
    fm,
    freeze,
    model_lineage,
    safe_file,
    sha,
)
from caretaker.roll_forward import _model_lineage


class DraftAttestationFleetMakerTests(unittest.TestCase):
    """Test _published_draft_matches, _is_caretaker_draft, freeze, and model_lineage."""

    def _create_env(
        self,
        root: Path,
        *,
        maker="claude",
        reviewer=None,
        verdict="PASS",
        tamper_event_manifest_sha=False,
        tamper_result_manifest_sha=False,
        tamper_review_manifest_sha=False,
        tamper_page_sha=False,
        page_text_override=None,
        target="wiki/projects/test-proj.md",
        event_id="evt-20260930T061131021Z-36f894c7",
    ):
        if reviewer is None:
            reviewer = {"id": "minimax-attempt-1", "lineage": "minimax"}

        vault = root / "vault"
        state_dir = root / "state"
        vault.mkdir(parents=True, exist_ok=True)
        state_dir.mkdir(parents=True, exist_ok=True)

        page_text = page_text_override or (
            "---\n"
            'title: "Test Proj"\n'
            'status: "draft"\n'
            'type: "project"\n'
            'project_slug: "test-proj"\n'
            'last_verified: "unverified"\n'
            'verification_scope: "producer-reports"\n'
            'sources: ["src-one"]\n'
            "---\n\n"
            "## Caretaker publication 2026-09-30\n\n"
            "Draft content from producer reports.\n"
        )
        page_path = safe_file(vault, target)
        page_path.parent.mkdir(parents=True, exist_ok=True)
        page_path.write_text(page_text, encoding="utf-8")
        page_bytes = page_path.read_bytes()
        page_sha = sha(page_bytes)

        run_dir = state_dir / "curation" / "run-001"
        run_dir.mkdir(parents=True, exist_ok=True)

        manifest_output_sha = "0" * 64 if tamper_page_sha else page_sha
        manifest_data = {
            "outputs": [
                {
                    "path": target,
                    "sha256": manifest_output_sha,
                }
            ]
        }
        manifest_file = run_dir / "manifest.json"
        manifest_bytes = json.dumps(manifest_data, indent=2).encode("utf-8")
        manifest_file.write_bytes(manifest_bytes)
        manifest_sha = sha(manifest_bytes)

        res_manifest_sha = "1" * 64 if tamper_result_manifest_sha else manifest_sha
        result_data = {
            "status": "PUBLISHED",
            "manifest": str(manifest_file),
            "manifest_sha256": res_manifest_sha,
            "publication": {
                "event_id": event_id,
                "manifest_sha256": manifest_sha,
            },
        }
        (run_dir / "result.json").write_text(json.dumps(result_data), encoding="utf-8")

        evt_manifest_sha = "2" * 64 if tamper_event_manifest_sha else manifest_sha
        rev_manifest_sha = "3" * 64 if tamper_review_manifest_sha else manifest_sha
        event_data = {
            "event_id": event_id,
            "manifest_sha256": evt_manifest_sha,
            "review": {
                "verdict": verdict,
                "maker": maker,
                "manifest_sha256": rev_manifest_sha,
                "reviewer": reviewer,
            },
        }
        event_path = safe_file(vault, f"governance/curations/{event_id}.json")
        event_path.parent.mkdir(parents=True, exist_ok=True)
        event_path.write_text(json.dumps(event_data), encoding="utf-8")

        return vault, state_dir, target, page_text, page_sha

    def _setup_proposals_and_sources(self, vault: Path, target: str):
        src_dir = vault / "raw/sources/2026/09/src-one"
        src_dir.mkdir(parents=True, exist_ok=True)
        orig_data = b"Original source report."
        (src_dir / "original.md").write_bytes(orig_data)
        meta = {
            "source_id": "src-one",
            "classification": "internal",
            "local_only": False,
            "original_file": "original.md",
            "sha256": sha(orig_data),
            "byte_length": len(orig_data),
        }
        (src_dir / "source.json").write_text(json.dumps(meta), encoding="utf-8")

        prop_file = vault / "proposals/agent/prop-001.md"
        prop_file.parent.mkdir(parents=True, exist_ok=True)
        prop_text = (
            "---\n"
            "id: prop-001\n"
            "classification: internal\n"
            "source_ids:\n"
            "  - src-one\n"
            "target_pages:\n"
            f"  - {target}\n"
            "---\n\n"
            "Proposal body citing src-one.\n"
        )
        prop_file.write_text(prop_text, encoding="utf-8")

    def test_maker_openclaw_caretaker_regression(self):
        with tempfile.TemporaryDirectory() as td:
            vault, state_dir, target, page_text, page_sha = self._create_env(
                Path(td),
                maker="openclaw-caretaker",
                reviewer={"id": "minimax-attempt-1", "lineage": "minimax"},
            )
            self.assertTrue(
                _is_caretaker_draft(
                    page_text,
                    target,
                    vault=vault,
                    state_dir=state_dir,
                    page_sha256=page_sha,
                    expected_project="test-proj",
                )
            )

    def test_maker_claude_reviewer_minimax_live_case(self):
        with tempfile.TemporaryDirectory() as td:
            vault, state_dir, target, page_text, page_sha = self._create_env(
                Path(td),
                maker="claude",
                reviewer={"id": "minimax-attempt-1", "lineage": "minimax"},
            )
            self.assertTrue(
                _is_caretaker_draft(
                    page_text,
                    target,
                    vault=vault,
                    state_dir=state_dir,
                    page_sha256=page_sha,
                    expected_project="test-proj",
                )
            )

    def test_maker_claude_reviewer_claude_same_lineage_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            vault, state_dir, target, page_text, page_sha = self._create_env(
                Path(td),
                maker="claude",
                reviewer={"id": "claude-attempt-1", "lineage": "claude"},
            )
            self.assertFalse(
                _is_caretaker_draft(
                    page_text,
                    target,
                    vault=vault,
                    state_dir=state_dir,
                    page_sha256=page_sha,
                    expected_project="test-proj",
                )
            )

    def test_maker_claude_reviewer_id_openclaw_caretaker_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            vault, state_dir, target, page_text, page_sha = self._create_env(
                Path(td),
                maker="claude",
                reviewer={"id": "openclaw-caretaker", "lineage": "minimax"},
            )
            self.assertFalse(
                _is_caretaker_draft(
                    page_text,
                    target,
                    vault=vault,
                    state_dir=state_dir,
                    page_sha256=page_sha,
                    expected_project="test-proj",
                )
            )

    def test_empty_or_non_string_maker_rejected(self):
        for bad_maker in ("", None, 123, [], {}, True):
            with self.subTest(maker=bad_maker):
                with tempfile.TemporaryDirectory() as td:
                    vault, state_dir, target, page_text, page_sha = self._create_env(
                        Path(td),
                        maker=bad_maker,
                        reviewer={"id": "minimax-attempt-1", "lineage": "minimax"},
                    )
                    self.assertFalse(
                        _is_caretaker_draft(
                            page_text,
                            target,
                            vault=vault,
                            state_dir=state_dir,
                            page_sha256=page_sha,
                            expected_project="test-proj",
                        )
                    )

    def test_verdict_not_pass_rejected(self):
        for bad_verdict in ("FAIL", "REPAIR", "", None):
            with self.subTest(verdict=bad_verdict):
                with tempfile.TemporaryDirectory() as td:
                    vault, state_dir, target, page_text, page_sha = self._create_env(
                        Path(td),
                        maker="claude",
                        reviewer={"id": "minimax-attempt-1", "lineage": "minimax"},
                        verdict=bad_verdict,
                    )
                    self.assertFalse(
                        _is_caretaker_draft(
                            page_text,
                            target,
                            vault=vault,
                            state_dir=state_dir,
                            page_sha256=page_sha,
                            expected_project="test-proj",
                        )
                    )

    def test_tampered_manifest_hashes_rejected(self):
        cases = [
            ("tamper_event_manifest_sha", {"tamper_event_manifest_sha": True}),
            ("tamper_result_manifest_sha", {"tamper_result_manifest_sha": True}),
            ("tamper_review_manifest_sha", {"tamper_review_manifest_sha": True}),
        ]
        for name, kwargs in cases:
            with self.subTest(tamper=name):
                with tempfile.TemporaryDirectory() as td:
                    vault, state_dir, target, page_text, page_sha = self._create_env(
                        Path(td),
                        maker="claude",
                        reviewer={"id": "minimax-attempt-1", "lineage": "minimax"},
                        **kwargs,
                    )
                    self.assertFalse(
                        _is_caretaker_draft(
                            page_text,
                            target,
                            vault=vault,
                            state_dir=state_dir,
                            page_sha256=page_sha,
                            expected_project="test-proj",
                        )
                    )

    def test_page_bytes_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            vault, state_dir, target, page_text, page_sha = self._create_env(
                Path(td),
                maker="claude",
                reviewer={"id": "minimax-attempt-1", "lineage": "minimax"},
                tamper_page_sha=True,
            )
            self.assertFalse(
                _is_caretaker_draft(
                    page_text,
                    target,
                    vault=vault,
                    state_dir=state_dir,
                    page_sha256=page_sha,
                    expected_project="test-proj",
                )
            )

        with tempfile.TemporaryDirectory() as td:
            vault, state_dir, target, page_text, _ = self._create_env(
                Path(td),
                maker="claude",
                reviewer={"id": "minimax-attempt-1", "lineage": "minimax"},
            )
            self.assertFalse(
                _is_caretaker_draft(
                    page_text,
                    target,
                    vault=vault,
                    state_dir=state_dir,
                    page_sha256="0" * 64,
                    expected_project="test-proj",
                )
            )

    def test_end_to_end_freeze_on_fleet_written_draft(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            vault, state_dir, target, _, page_sha = self._create_env(
                root,
                maker="claude",
                reviewer={"id": "minimax-attempt-1", "lineage": "minimax"},
            )
            self._setup_proposals_and_sources(vault, target)

            # Without attestation_state_dir, freeze raises "target is not reviewed"
            with self.assertRaises(PublicationError) as err:
                freeze(vault, ["proposals/agent/prop-001.md"], target)
            self.assertEqual(str(err.exception), "target is not reviewed")

            # With attestation_state_dir for Fleet-written draft, freeze succeeds
            frozen = freeze(
                vault,
                ["proposals/agent/prop-001.md"],
                target,
                attestation_state_dir=state_dir,
            )
            self.assertIsInstance(frozen, dict)
            self.assertEqual(frozen["target"], target)
            self.assertEqual(frozen["preimage_sha256"], page_sha)

    def test_model_lineage_unit_cases(self):
        cases = [
            (("claude", None), "claude"),
            (("opencode", "claude-sonnet-5"), "claude"),
            (("x", "MiniMax-M3"), "minimax"),
            (("Qwen", None), "qwen"),
            (("claude", "opus-4"), "claude"),
            (("claude", "haiku-3.5"), "claude"),
            (("anthropic", "claude-3-7-sonnet"), "claude"),
            (("x", "grok-2"), "grok"),
            (("google", "gemini-1.5-pro"), "gemini"),
            (("deepseek", "deepseek-chat"), "deepseek"),
            (("openai", "gpt-4o"), "gpt"),
            (("muse", "muse-spark"), "muse"),
            (("openclaw-caretaker", None), "openclaw-caretaker"),
            (("sonnet", None), "claude"),
            (("minimax-attempt-1", None), "minimax"),
            (("claude-attempt-1", None), "claude"),
        ]
        for (provider, model), expected in cases:
            with self.subTest(provider=provider, model=model):
                self.assertEqual(model_lineage(provider, model), expected)
                self.assertEqual(_model_lineage(provider, model), expected)

    def test_reviewer_lineage_fallback_to_id(self):
        with tempfile.TemporaryDirectory() as td:
            vault, state_dir, target, page_text, page_sha = self._create_env(
                Path(td),
                maker="claude",
                reviewer={"id": "minimax-attempt-1"},
            )
            self.assertTrue(
                _is_caretaker_draft(
                    page_text,
                    target,
                    vault=vault,
                    state_dir=state_dir,
                    page_sha256=page_sha,
                    expected_project="test-proj",
                )
            )

    def test_reviewer_lineage_fallback_to_id_same_lineage_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            vault, state_dir, target, page_text, page_sha = self._create_env(
                Path(td),
                maker="claude",
                reviewer={"id": "claude-attempt-1"},
            )
            self.assertFalse(
                _is_caretaker_draft(
                    page_text,
                    target,
                    vault=vault,
                    state_dir=state_dir,
                    page_sha256=page_sha,
                    expected_project="test-proj",
                )
            )

    def test_attested_fleet_draft_grows_into_reviewed_content(self):
        """An attested draft with durable source content becomes reviewed.

        The status describes the content (independently reviewable project
        facts). It never claims live verification, and the attested draft
        chain itself stays the only accepted starting point.
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            vault, state_dir, target, _page_text, _page_sha = self._create_env(
                root,
                maker="claude",
                reviewer={"id": "minimax-attempt-1", "lineage": "minimax"},
            )
            self._setup_proposals_and_sources(vault, target)
            frozen = freeze(
                vault,
                ["proposals/agent/prop-001.md"],
                target,
                attestation_state_dir=state_dir,
            )
            decision = {
                "body": "The reported tool canary passed and the deployment "
                        "stays unverified (src-one).",
                "summary": "One dated report.",
                "dispositions": {
                    "prop-001": {
                        "disposition": "synthesized",
                        "reason": "Dated and scope-limited report integrated.",
                        "knowledge_class": "durable-fact",
                    }
                },
            }
            candidate = root / "candidate"
            build_candidate(frozen, decision, candidate, "2026-10-01")
            page = (candidate / "payload" / "wiki" / "projects"
                    / "test-proj.md").read_text(encoding="utf-8")
            self.assertEqual(fm(page, "status"), "reviewed")
            self.assertEqual(fm(page, "last_verified"), "unverified")
            self.assertEqual(fm(page, "verification_scope"), "producer-reports")
            self.assertIn("Draft content from producer reports.", page)


if __name__ == "__main__":
    unittest.main()
