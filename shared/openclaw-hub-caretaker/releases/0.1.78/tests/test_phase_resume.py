from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import curate_once as curator
from caretaker import tick_budget
from caretaker.publication import PublicationError, sha
from caretaker.transports import TransportError


BODY = "# New project\nDated tool canary report (src-one); deployment unverified.\nProducer-reported only."
DECISION = {
    "body": BODY,
    "summary": "Tool canary report.",
    "dispositions": {
        "prop-one": {
            "disposition": "synthesized",
            "reason": "Dated and scope-limited report integrated.",
        }
    },
}


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class Maker:
    def __init__(self, clock=None, advance=0):
        self.calls = 0
        self.clock = clock
        self.advance = advance

    def invoke(self, **kwargs):
        self.calls += 1
        if self.clock:
            self.clock.advance(self.advance)
        return SimpleNamespace(
            ok=True, status="ok", provider="vllm", model="Qwen3.8-27B",
            session_id="maker-session", final=json.dumps(DECISION),
        )


class Reviewer:
    def __init__(self, actions=(), clock=None, advance=0):
        self.actions = list(actions)
        self.calls = 0
        self.clock = clock
        self.advance = advance

    def invoke(self, *, prompt, **kwargs):
        self.calls += 1
        if self.clock:
            self.clock.advance(self.advance)
        action = self.actions.pop(0) if self.actions else "PASS"
        if action == "timeout":
            raise TransportError("synthetic reviewer timeout", reason_code="process_timeout")
        digest_match = re.search(r'manifest_sha256":"([a-f0-9]{64})"', prompt)
        if not digest_match:
            raise AssertionError("review prompt did not bind a manifest digest")
        parsed = {"verdict": "PASS", "manifest_sha256": digest_match.group(1), "findings": []}
        if isinstance(action, tuple):
            parsed["verdict"], parsed["findings"] = action
        return SimpleNamespace(
            ok=True, status="ok", provider="muse-code", model="muse-spark-1.3",
            session_id=f"review-session-{self.calls}", final=json.dumps(parsed),
        )


class PhaseResumeTests(unittest.TestCase):
    def setUp(self):
        test_temp = Path(os.environ.get("TEAM_TEST_ROOT") or tempfile.gettempdir()) / "caretaker-tests"
        test_temp.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="phase-resume-", dir=test_temp))
        self.vault = self.root / "vault"
        self.state = self.root / "state"
        self.vault.mkdir()
        self.state.mkdir()
        self._write_source_and_proposal()
        publication = SimpleNamespace(
            maker_timeout_seconds=500,
            reviewer_timeout_seconds=500,
            reviewer=SimpleNamespace(mode="muse"),
        )
        self.cfg = SimpleNamespace(
            state_dir=self.state,
            project_slug="new-slug",
            hub=SimpleNamespace(
                vault=self.vault,
                script=self.root / "knowledge.py",
                python_executable="python",
            ),
            publication=publication,
            primary=SimpleNamespace(kind="synthetic"),
            fallback=SimpleNamespace(kind="synthetic"),
            raw={},
        )
        self.paths = ["proposals/agent/one.md"]
        self.maker = Maker()
        self.reviewer = Reviewer()

    def _write_source_and_proposal(self):
        source_dir = self.vault / "raw/sources/2026/09/src-one"
        source_dir.mkdir(parents=True)
        data = b"Dated report: local tool canary passed; deployment remains unverified."
        (source_dir / "original.md").write_bytes(data)
        (source_dir / "source.json").write_text(json.dumps({
            "source_id": "src-one", "classification": "internal",
            "local_only": False, "original_file": "original.md",
            "sha256": sha(data), "byte_length": len(data),
        }), encoding="utf-8")
        proposal = self.vault / "proposals/agent/one.md"
        proposal.parent.mkdir(parents=True)
        proposal.write_text(
            "---\nid: prop-one\nclassification: internal\nsource_ids:\n  - src-one\n"
            "target_pages:\n  - wiki/projects/new-slug.md\n---\nReport.\n",
            encoding="utf-8",
        )

    def _gateway(self):
        return SimpleNamespace(
            health=lambda: SimpleNamespace(status="pass"),
            lint=lambda: SimpleNamespace(status="pass"),
        )

    def _run(self, *, publish=True, budget=None):
        publish_impl = lambda cfg, manifest, review, run_root, result: result.update(
            status="PUBLISHED",
            publication={"status": "PUBLISHED"},
            checkpoint={"status": "CHECKPOINTED"},
        )
        patches = [
            mock.patch.object(curator, "CanonicalKnowledgeGateway", return_value=self._gateway()),
            mock.patch.object(curator, "_write_run_config", return_value=str(self.root / "run-config.json")),
            mock.patch.object(curator, "_publish_under_lock", side_effect=publish_impl),
        ]
        for patcher in patches:
            patcher.start()
        try:
            if budget is None:
                return curator._run_with_config(
                    self.cfg, self.paths, publish, self.maker, self.reviewer,
                )
            with budget.activate():
                return curator._run_with_config(
                    self.cfg, self.paths, publish, self.maker, self.reviewer,
                )
        finally:
            for patcher in reversed(patches):
                patcher.stop()

    def _budget(self, clock, seconds):
        return tick_budget.TickBudget(seconds=seconds, clock=clock)

    def _phase_path(self):
        from caretaker.pubstate import batch_key
        return self.state / "curation" / (
            "phase-" + batch_key(self.cfg.project_slug, self.paths) + ".json"
        )

    def test_prepared_candidate_resumes_at_review_and_publishes_without_second_maker(self):
        clock = Clock()
        self.maker = Maker(clock, advance=350)
        budget = self._budget(clock, 500)
        first = self._run(budget=budget)
        self.assertEqual(first["status"], "DEFERRED_PREPARED")
        self.assertEqual(first["phase"], "PREPARED")
        self.assertEqual(self.maker.calls, 1)
        self.assertEqual(self.reviewer.calls, 0)

        second = self._run()
        self.assertEqual(second["status"], "PUBLISHED")
        self.assertEqual(second["model_calls"], 1)
        self.assertEqual(self.maker.calls, 1)
        self.assertEqual(self.reviewer.calls, 1)

    def test_repair_assessment_and_attempt_resume_before_only_missing_second_review(self):
        clock = Clock()
        self.maker = Maker(clock, advance=150)
        self.reviewer = Reviewer([
            ("REPAIR", [{"class": "unsupported_claim", "weight": "blocking",
                          "text": "synthetic unsupported claim"}]),
            "timeout",
            "PASS",
        ], clock=clock, advance=400)
        first = self._run(budget=self._budget(clock, 700))
        self.assertEqual(first["status"], "DEFERRED_PREPARED")
        self.assertEqual(first["phase"], "REPAIR_REQUIRED")
        self.assertEqual(self.maker.calls, 1)
        self.assertEqual(self.reviewer.calls, 1)

        second = self._run()
        self.assertEqual(second["status"], "DEFERRED_PREPARED")
        self.assertEqual(second["phase"], "REPAIR_PREPARED")
        self.assertEqual(self.maker.calls, 2)
        self.assertEqual(self.reviewer.calls, 2)
        stored = json.loads(self._phase_path().read_text(encoding="utf-8"))
        self.assertEqual(stored["phase"], "REPAIR_PREPARED")
        self.assertEqual(stored["progress"]["repair_attempt"], 1)
        self.assertEqual(stored["progress"]["assessment"]["verdict"], "REPAIR")
        self.assertEqual(stored["progress"]["maker_format_repairs"], 0)

        third = self._run()
        self.assertEqual(third["status"], "PUBLISHED")
        self.assertEqual(third["model_calls"], 1)
        self.assertEqual(self.maker.calls, 2)
        self.assertEqual(self.reviewer.calls, 3)

    def test_successful_review_result_is_durable_before_following_phase(self):
        with mock.patch.object(
            curator, "_review_round_record",
            side_effect=tick_budget.TickBudgetExceeded("synthetic phase boundary"),
        ):
            first = self._run()
        self.assertEqual(first["status"], "DEFERRED_PREPARED")
        self.assertEqual(first["phase"], "REVIEW_RESULT")
        self.assertEqual(self.maker.calls, 1)
        self.assertEqual(self.reviewer.calls, 1)

        second = self._run()
        self.assertEqual(second["status"], "PUBLISHED")
        self.assertEqual(self.maker.calls, 1)
        self.assertEqual(self.reviewer.calls, 1)

    def test_targeted_repair_response_resumes_without_another_maker_call(self):
        self.reviewer = Reviewer([
            ("REPAIR", [{"class": "unsupported_claim", "weight": "blocking",
                          "text": "synthetic unsupported claim"}]),
            "PASS",
        ])
        build_candidate = curator.build_candidate

        def stop_after_repair_response(frozen, decision, candidate, observed):
            if Path(candidate).name == "candidate-review-repair":
                raise tick_budget.TickBudgetExceeded("synthetic phase boundary")
            return build_candidate(frozen, decision, candidate, observed)

        with mock.patch.object(curator, "build_candidate", side_effect=stop_after_repair_response):
            first = self._run()
        self.assertEqual(first["status"], "DEFERRED_PREPARED")
        self.assertEqual(first["phase"], "REPAIR_RESULT")
        self.assertEqual(self.maker.calls, 2)
        self.assertEqual(self.reviewer.calls, 1)

        second = self._run()
        self.assertEqual(second["status"], "PUBLISHED")
        self.assertEqual(self.maker.calls, 2)
        self.assertEqual(self.reviewer.calls, 2)

    def test_changed_source_and_target_refuse_prepared_resume(self):
        clock = Clock()
        self.maker = Maker(clock, advance=350)
        self._run(budget=self._budget(clock, 500))
        source = self.vault / "raw/sources/2026/09/src-one/original.md"
        source.write_text("changed synthetic source", encoding="utf-8")
        with mock.patch.object(curator, "CanonicalKnowledgeGateway", return_value=self._gateway()), \
             mock.patch.object(curator, "_write_run_config", return_value="synthetic"):
            with self.assertRaises(PublicationError):
                curator._run_with_config(self.cfg, self.paths, True, self.maker, self.reviewer)
        self.assertEqual(self.maker.calls, 1)

    def test_changed_target_refuses_prepared_resume(self):
        clock = Clock()
        self.maker = Maker(clock, advance=350)
        self._run(budget=self._budget(clock, 500))
        target = self.vault / "wiki/projects/new-slug.md"
        target.parent.mkdir(parents=True)
        target.write_text(
            "---\nstatus: reviewed\nproject_slug: new-slug\n---\nPrior synthetic page.\n",
            encoding="utf-8",
        )
        with mock.patch.object(curator, "CanonicalKnowledgeGateway", return_value=self._gateway()), \
             mock.patch.object(curator, "_write_run_config", return_value="synthetic"):
            with self.assertRaisesRegex(PublicationError, "phase_resume_binding_invalid"):
                curator._run_with_config(self.cfg, self.paths, True, self.maker, self.reviewer)
        self.assertEqual(self.maker.calls, 1)

    def test_cache_tamper_and_unknown_phase_refuse_replay(self):
        clock = Clock()
        self.maker = Maker(clock, advance=350)
        self._run(budget=self._budget(clock, 500))
        pointer = self._phase_path()
        stored = json.loads(pointer.read_text(encoding="utf-8"))
        stored["progress"]["maker_calls"] = 99
        pointer.write_text(json.dumps(stored), encoding="utf-8")
        frozen = curator.freeze(self.vault, self.paths, "wiki/projects/new-slug.md",
                                attestation_state_dir=self.state)
        with self.assertRaisesRegex(PublicationError, "phase_resume_binding_invalid"):
            curator._load_phase_progress(self.cfg, self.paths, pointer.parent, frozen)
        stored = json.loads(pointer.read_text(encoding="utf-8"))
        stored["phase"] = "RUNNING"
        stored["cache_sha256"] = curator._phase_digest(stored)
        pointer.write_text(json.dumps(stored), encoding="utf-8")
        with self.assertRaisesRegex(PublicationError, "phase_resume_binding_invalid"):
            curator._load_phase_progress(self.cfg, self.paths, pointer.parent, frozen)
        self.assertEqual(self.maker.calls, 1)

    def test_normal_publication_ticks_keep_prepared_cycle_pending_and_resume(self):
        import datetime as dt
        import maintenance
        from caretaker.audit import ensure_tables, policy_from
        from caretaker.pubstate import ensure_pub_tables, read_row, batch_key

        registry = self.vault / "wiki/registry.md"
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text(
            "```json\n"
            + json.dumps({
                "schema_version": 1,
                "projects": {"new-slug": {"main_page": "wiki/projects/new-slug.md"}},
                "aliases": {},
            })
            + "\n```\n",
            encoding="utf-8",
        )
        proposal_path = self.vault / self.paths[0]
        proposal_text = proposal_path.read_bytes()
        member = {
            "proposal_id": "prop-one", "path": self.paths[0],
            "sha256": sha(proposal_text), "project": "new-slug",
            "producer": "codex", "episode": "", "source_ids": ["src-one"],
        }
        policy = {
            "enabled": True, "include_existing_hub_backlog": True,
            "projects": ["new-slug"], "held_projects": [],
            "min_ready_groups": 1, "max_wait_hours": 24, "max_batch": 8,
        }
        summary = {
            "trigger": "threshold", "eligible": [member],
            "raw_eligible": 1, "group_count": 1, "ready_group_count": 1,
            "oldest_age_hours": 48, "deferred": 0, "unsupported": 0,
            "policy": policy_from(policy),
        }
        database = sqlite3.connect(":memory:")
        self.addCleanup(database.close)
        ensure_tables(database)
        ensure_pub_tables(database)
        self.reviewer = Reviewer(["timeout", "PASS"])
        now = dt.datetime(2026, 10, 22, 12, 0, tzinfo=dt.timezone.utc)
        maint_cfg = {
            "hub": {"vault": str(self.vault)}, "project_slug": "new-slug",
            "state_dir": str(self.state),
        }

        def invoke_curate(_config_path, _paths):
            return self._run(publish=True)

        with mock.patch.object(maintenance, "_audit_summary", return_value=summary), \
             mock.patch.object(maintenance, "_invoke_curate", side_effect=invoke_curate):
            first = maintenance.publication_tick(
                "synthetic-config", maint_cfg, {"publication": policy}, database, now=now,
            )
            self.assertEqual(first["status"], "DEFERRED_PREPARED")
            self.assertTrue(first["pre_effect"])
            cycle = __import__("caretaker.audit", fromlist=["active_cycle"]).active_cycle(database)
            self.assertEqual(cycle["status"], "ACTIVE")
            self.assertEqual(cycle["members"][0]["status"], "pending")
            cycle_id = cycle["id"]
            key = batch_key("new-slug", self.paths)
            row = read_row(database, key)
            self.assertEqual(row["status"], "PREPARED_DEFERRED")
            self.assertEqual(row["attempt"], 0)

            second = maintenance.publication_tick(
                "synthetic-config", maint_cfg, {"publication": policy}, database,
                now=now + dt.timedelta(seconds=1),
            )

        self.assertEqual(second["status"], "PUBLISHED")
        self.assertEqual(self.maker.calls, 1)
        self.assertEqual(self.reviewer.calls, 2)
        self.assertEqual(second["model_calls"], 1)
        self.assertEqual(read_row(database, key)["status"], "PUBLISHED")
        self.assertEqual(database.execute(
            "SELECT COUNT(*) FROM publications WHERE status='PREPARED_DEFERRED'"
        ).fetchone()[0], 0)
        self.assertEqual(database.execute(
            "SELECT status FROM audit_cycles WHERE id=?", (cycle_id,)
        ).fetchone()[0], "DRAINED")
        cycle = __import__("caretaker.audit", fromlist=["active_cycle"]).active_cycle(database)
        self.assertIsNone(cycle)
        status = database.execute(
            "SELECT status FROM audit_members WHERE proposal_id='prop-one'"
        ).fetchone()[0]
        self.assertEqual(status, "published")


if __name__ == "__main__":
    unittest.main()
