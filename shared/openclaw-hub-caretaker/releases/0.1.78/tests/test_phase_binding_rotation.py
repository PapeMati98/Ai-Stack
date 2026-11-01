from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import curate_once as curator
from caretaker.publication import PublicationError, build_candidate, freeze, sha


LEGACY_CODE_VERSION = "416088b2471fa1a108e270bd91744595"
PROJECT = "new-slug"
MAIN = "wiki/projects/new-slug.md"
CONTINUATION = "wiki/projects/new-slug-cont-2.md"
PATHS = ["proposals/agent/one.md", "proposals/agent/two.md"]


class NoCallMaker:
    def __init__(self):
        self.calls = 0

    def invoke(self, **_kwargs):
        self.calls += 1
        raise AssertionError("a valid PREPARED candidate must not call the maker again")


class CountingReviewer:
    def __init__(self):
        self.calls = 0

    def invoke(self, *, prompt, **_kwargs):
        self.calls += 1
        match = re.search(r'"manifest_sha256"\s*:\s*"([a-f0-9]{64})"', prompt)
        if not match:
            raise AssertionError("review prompt did not bind the candidate manifest")
        return SimpleNamespace(
            ok=True, status="ok", provider="muse-code", model="muse-spark-1.3",
            session_id=f"synthetic-review-{self.calls}",
            final=json.dumps({
                "verdict": "PASS", "manifest_sha256": match.group(1), "findings": [],
            }),
        )


class PhaseBindingRotationTests(unittest.TestCase):
    def setUp(self):
        private_temp = Path(os.environ.get("TEAM_TEST_ROOT") or tempfile.gettempdir())
        private_temp.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="phase-binding-rotation-", dir=private_temp))
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.runtime_state = self.root / "runtime"
        self.state = self.runtime_state / "projects" / PROJECT / "state"
        self.state.mkdir(parents=True)
        self._write_synthetic_vault()
        self.cfg = SimpleNamespace(
            state_dir=self.state,
            project_slug=PROJECT,
            hub=SimpleNamespace(
                vault=self.vault, script=self.root / "knowledge.py", python_executable="python",
            ),
            publication=SimpleNamespace(
                maker_timeout_seconds=500, reviewer_timeout_seconds=500,
                reviewer=SimpleNamespace(mode="muse"),
            ),
            primary=SimpleNamespace(kind="synthetic"),
            fallback=SimpleNamespace(kind="synthetic"),
            raw={},
        )
        self.paths = list(PATHS)
        self.run_root = self.state / "curation" / "synthetic-run"
        self.run_root.mkdir(parents=True)

    def _page(self, *, continues=None, body):
        fields = [
            "title: synthetic project", "type: project", "status: reviewed",
            f"project_slug: {PROJECT}", "last_verified: 2026-10-21",
            "verification_scope: producer-reports", "sources: [src-one]",
        ]
        if continues:
            fields.append(f"continues: {continues}")
        return "---\n" + "\n".join(fields) + "\n---\n\n" + body + "\n"

    def _write_synthetic_vault(self):
        registry = self.vault / "wiki/registry.md"
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text(
            "```json\n" + json.dumps({
                "schema_version": 1,
                "projects": {PROJECT: {"main_page": MAIN}},
                "aliases": {},
            }) + "\n```\n",
            encoding="utf-8",
        )
        projects = self.vault / "wiki/projects"
        projects.mkdir(parents=True, exist_ok=True)
        main_text = self._page(body="Existing reviewed context. " * 540)
        continuation_text = self._page(
            continues=MAIN, body="Existing continuation context. " * 24,
        )
        self.assertLess(len(main_text.encode("utf-8")), 16000)
        self.assertLess(len(continuation_text.encode("utf-8")), 16000)
        (self.vault / MAIN).write_text(main_text, encoding="utf-8")
        (self.vault / CONTINUATION).write_text(continuation_text, encoding="utf-8")

        source_dir = self.vault / "raw/sources/2026/10/src-one"
        source_dir.mkdir(parents=True)
        original = b"Synthetic report: bounded local test fixture, no external state."
        (source_dir / "original.md").write_bytes(original)
        (source_dir / "source.json").write_text(json.dumps({
            "source_id": "src-one", "classification": "internal",
            "local_only": False, "original_file": "original.md",
            "sha256": sha(original), "byte_length": len(original),
        }), encoding="utf-8")
        for name, pid in zip(PATHS, ("prop-one", "prop-two")):
            proposal = self.vault / name
            proposal.parent.mkdir(parents=True, exist_ok=True)
            proposal.write_text(
                "---\n"
                f"id: {pid}\nclassification: internal\n"
                "source_ids:\n  - src-one\n"
                f"target_pages:\n  - {MAIN}\n"
                "---\nSynthetic proposal.\n",
                encoding="utf-8",
            )

    def _decision(self):
        return {
            "body": "Synthetic bounded report (src-one). " * 300,
            "summary": "Synthetic bounded report.",
            "dispositions": {
                "prop-one": {"disposition": "synthesized", "reason": "Synthetic cited evidence."},
                "prop-two": {"disposition": "synthesized", "reason": "Synthetic cited evidence."},
            },
        }

    def _phase_pointer(self):
        return curator._phase_pointer(
            self.state / "curation", PROJECT, self.paths,
        )

    def _prepared_record(self, *, legacy=True):
        frozen = freeze(self.vault, self.paths, MAIN, attestation_state_dir=self.state)
        self.assertEqual([item["id"] for item in frozen["proposals"]], ["prop-one", "prop-two"])
        manifest = build_candidate(
            frozen, self._decision(), self.run_root / "candidate", "2026-10-22T12:00:00Z",
        )
        self.assertEqual(frozen["target"], CONTINUATION)
        progress = {
            "phase": "PREPARED", "maker_calls": 1, "review_calls": 0,
            "maker_format_repairs": 0, "reviewer_format_repairs": {},
            "maker_provider": "vllm", "maker_model": "Qwen3.8-27B",
            "maker_route": "local_fallback", "review_rounds": [],
            "observed": "2026-10-22T12:00:00Z", "assessment": None,
        }
        curator._save_phase_progress(
            self.cfg, self.paths, frozen, self.run_root, "2026-10-22T12:00:00Z",
            "PREPARED", manifest.parent, manifest, progress,
        )
        pointer = self._phase_pointer()
        record = json.loads(pointer.read_text(encoding="utf-8"))
        if legacy:
            # Emulate the actual 0.1.53 binding: proposal order followed the
            # initial freeze order, and code_version was the deployed 416 pin.
            record["binding"]["proposals"] = [
                {key: item.get(key) for key in ("id", "path", "sha256", "source_ids")}
                for item in frozen["proposals"]
            ]
            record["code_version"] = LEGACY_CODE_VERSION
            record["cache_sha256"] = curator._phase_digest(record)
            curator._durable_json(pointer, record)
        return frozen, manifest, pointer, record

    def _fresh_freeze(self, paths):
        return freeze(self.vault, paths, MAIN, attestation_state_dir=self.state)

    def _load(self, paths, frozen):
        return curator._load_phase_progress(
            self.cfg, paths, self.state / "curation", frozen,
        )

    def test_real_prepare_order_change_rotation_and_old_pin_resume_to_new_review(self):
        frozen, manifest, pointer, record = self._prepared_record()
        self.assertEqual(record["phase"], "PREPARED")
        self.assertEqual(record["progress"]["maker_calls"], 1)
        self.assertEqual(record["progress"]["review_calls"], 0)

        fresh_paths = list(reversed(self.paths))
        fresh = self._fresh_freeze(fresh_paths)
        self.assertEqual([item["id"] for item in fresh["proposals"]], ["prop-two", "prop-one"])
        self.assertEqual(fresh["target"], MAIN)
        self.assertEqual(record["binding"]["target"], CONTINUATION)
        self.assertNotEqual(record["binding"]["preimage_sha256"], fresh["preimage_sha256"])
        loaded = self._load(fresh_paths, fresh)
        self.assertEqual(loaded["phase"], "PREPARED")
        self.assertEqual(fresh["target"], CONTINUATION)

        # The scheduled path repeats the normal fresh freeze and candidate load.
        # The old phase is admitted only through its narrow PREPARED compatibility
        # check; its candidate must still receive a new independent review.
        maker, reviewer = NoCallMaker(), CountingReviewer()
        publish_impl = lambda _cfg, _manifest, _review, _run_root, result: result.update(
            status="PUBLISHED", publication={"status": "PUBLISHED"},
            checkpoint={"status": "CHECKPOINTED"},
        )
        with mock.patch.object(
            curator, "CanonicalKnowledgeGateway",
            return_value=SimpleNamespace(
                health=lambda: SimpleNamespace(status="pass"),
                lint=lambda: SimpleNamespace(status="pass"),
            ),
        ), mock.patch.object(curator, "_write_run_config", return_value="synthetic-config"), \
             mock.patch.object(curator, "_publish_under_lock", side_effect=publish_impl):
            result = curator._run_with_config(
                self.cfg, fresh_paths, True, maker, reviewer,
            )

        self.assertEqual(result["status"], "PUBLISHED")
        self.assertEqual(maker.calls, 0)
        self.assertEqual(reviewer.calls, 1)
        self.assertTrue(manifest.is_file())

    def test_legacy_review_accept_unknown_and_started_phases_do_not_replay(self):
        _frozen, _manifest, pointer, record = self._prepared_record()
        for phase, progress in (
            ("REVIEW_ACCEPTED", {"maker_calls": 1, "review_calls": 1,
                                  "assessment": {"verdict": "PASS"},
                                  "review_response": {"provider": "muse-code"}}),
            ("PREPARED", {"maker_calls": 1, "review_calls": 0, "status": "UNKNOWN"}),
            ("REVIEW_STARTED", {"maker_calls": 1, "review_calls": 1}),
        ):
            altered = dict(record)
            altered["phase"] = phase
            altered_progress = dict(record["progress"])
            altered_progress.update(progress)
            altered_progress["phase"] = phase
            altered["progress"] = altered_progress
            altered["cache_sha256"] = curator._phase_digest(altered)
            curator._durable_json(pointer, altered)
            fresh = self._fresh_freeze(list(reversed(self.paths)))
            with self.subTest(phase=phase), self.assertRaises(PublicationError):
                self._load(list(reversed(self.paths)), fresh)
        self.assertEqual(record["code_version"], LEGACY_CODE_VERSION)

    def test_changed_source_refuses_prepared_resume(self):
        _frozen, _manifest, _pointer, _record = self._prepared_record()
        source = self.vault / "raw/sources/2026/10/src-one/original.md"
        source.write_text("changed synthetic source", encoding="utf-8")
        with self.assertRaises(PublicationError):
            self._fresh_freeze(list(reversed(self.paths)))

    def test_changed_target_preimage_refuses_rotated_resume(self):
        self._prepared_record()
        target = self.vault / CONTINUATION
        target.write_text(target.read_text(encoding="utf-8") + "Changed live target.\n", encoding="utf-8")
        fresh = self._fresh_freeze(list(reversed(self.paths)))
        with self.assertRaises(PublicationError):
            self._load(list(reversed(self.paths)), fresh)

    def test_changed_candidate_pin_refuses_rotated_resume(self):
        _frozen, manifest, _pointer, _record = self._prepared_record()
        payload = manifest.parent / json.loads(manifest.read_text(encoding="utf-8"))["outputs"][0]["payload_path"]
        payload.write_text(payload.read_text(encoding="utf-8") + "tamper\n", encoding="utf-8")
        fresh = self._fresh_freeze(list(reversed(self.paths)))
        with self.assertRaisesRegex(PublicationError, "phase_resume_artifact_changed"):
            self._load(list(reversed(self.paths)), fresh)

    def test_foreign_continuation_chain_refuses_rotated_resume(self):
        self._prepared_record()
        target = self.vault / CONTINUATION
        contents = target.read_text(encoding="utf-8")
        target.write_text(contents.replace(f"continues: {MAIN}", "continues: wiki/projects/foreign.md"),
                           encoding="utf-8")
        fresh = self._fresh_freeze(list(reversed(self.paths)))
        with self.assertRaisesRegex(PublicationError, "phase_resume_binding_invalid"):
            self._load(list(reversed(self.paths)), fresh)

    def test_reparse_candidate_path_refuses_resume(self):
        _frozen, _manifest, _pointer, record = self._prepared_record()
        fresh = self._fresh_freeze(list(reversed(self.paths)))
        candidate = (self.state / "curation" / record["candidate"]).resolve()
        original_is_symlink = Path.is_symlink

        def synthetic_reparse(path):
            return Path(path).resolve() == candidate or original_is_symlink(path)

        # Windows may not permit creating a symlink for an unprivileged test
        # process. Report the candidate directory as a reparse point at the
        # filesystem boundary so the same production refusal path is exercised.
        with mock.patch.object(Path, "is_symlink", autospec=True, side_effect=synthetic_reparse):
            with self.assertRaisesRegex(PublicationError, "reparse path refused"):
                self._load(list(reversed(self.paths)), fresh)

    def test_maintenance_priority_uses_the_same_narrow_legacy_compatibility(self):
        _frozen, manifest, _pointer, record = self._prepared_record()
        import maintenance
        from caretaker.pubstate import batch_key

        ordered = sorted(self.paths)
        proposal_by_path = {item["path"]: item for item in record["binding"]["proposals"]}
        result = {
            "project": PROJECT, "phase": "PREPARED", "paths": ordered,
            "proposal_ids": [proposal_by_path[path]["id"] for path in ordered],
            "hashes": [proposal_by_path[path]["sha256"] for path in ordered],
            "maker_calls": 1, "review_calls": 0,
            "manifest_sha256": sha(manifest.read_bytes()),
        }
        self.assertEqual(
            maintenance._prepared_priority_phase_cache_manifest(
                result, batch_key(PROJECT, self.paths), self.runtime_state,
            ),
            sha(manifest.read_bytes()),
        )


if __name__ == "__main__":
    unittest.main()
