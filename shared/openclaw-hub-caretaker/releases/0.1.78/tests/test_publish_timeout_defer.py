import datetime as dt
import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import curate_once as curator
import maintenance
from caretaker.audit import active_cycle, ensure_tables, policy_from
from caretaker import tick_budget
from caretaker.config import TransportSpec
from caretaker.publication import sha
from caretaker.pubstate import batch_key, ensure_pub_tables, read_row
from tests.test_phase_resume import Clock, DECISION, Maker, Reviewer


PROJECT = "new-slug"
PROPOSAL = "proposals/agent/one.md"
TARGET = "wiki/projects/new-slug.md"


class ScriptedSubprocess:
    def __init__(self, root, mode="resume"):
        self.root = root
        self.mode = mode
        self.publish_calls = 0
        self.lock_calls = 0
        self.last_publish_argv = None
        self.committed_verdict = None

    def run(self, argv, **kwargs):
        argv = list(argv)
        if (self.mode == "lock-timeout" and "-Action" in argv
                and argv[argv.index("-Action") + 1] == "acquire-lock"):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 1))
        if "-Action" in argv and argv[argv.index("-Action") + 1] == "acquire-lock":
            self.lock_calls += 1
            lock_id = f"synthetic-lock-{self.lock_calls}"
            lock_path = self.root / "locks" / f"{lock_id}.json"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path.write_text(json.dumps({
                "lock_id": lock_id, "agent": "openclaw-caretaker",
            }), encoding="utf-8")
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"lock_id": lock_id, "lock_path": str(lock_path)}), "",
            )
        if len(argv) == 14 and argv[5] == "publish":
            self.publish_calls += 1
            self.last_publish_argv = argv
            if self.publish_calls == 1 and self.mode != "resume":
                self._prepare_negative_case(argv)
                timeout_argv = list(argv)
                if self.mode == "wrong-command":
                    timeout_argv[5] = "status"
                raise subprocess.TimeoutExpired(
                    cmd=timeout_argv, timeout=kwargs.get("timeout", 1),
                )
            if self.publish_calls == 1:
                raise subprocess.TimeoutExpired(
                    cmd=argv, timeout=kwargs.get("timeout", 1),
                )
            return subprocess.CompletedProcess(
                argv, 0,
                json.dumps({"status": "PUBLISHED", "event_id": "evt-synthetic"}), "",
            )
        raise AssertionError(f"unexpected synthetic subprocess command: {argv!r}")

    def _prepare_negative_case(self, argv):
        if self.mode == "changed-source":
            (self.root / "vault/raw/sources/2026/10/src-one/original.md").write_bytes(
                b"Synthetic source changed after candidate review."
            )
        elif self.mode == "applying-journal":
            txdir = Path(argv[7]).parent / ".curation-transactions"
            txdir.mkdir(parents=True, exist_ok=True)
            (txdir / "tx-synthetic.json").write_text(
                json.dumps({"status": "applying"}), encoding="utf-8",
            )
        elif self.mode == "malformed-journal":
            txdir = Path(argv[7]).parent / ".curation-transactions"
            txdir.mkdir(parents=True, exist_ok=True)
            (txdir / "tx-synthetic.json").write_text("{malformed", encoding="utf-8")
        elif self.mode == "changed-target":
            (self.root / TARGET).write_text(
                "Synthetic target changed after independent review.\n", encoding="utf-8",
            )
        elif self.mode == "tampered-review":
            review_path = Path(argv[9])
            (review_path.parent / "semantic-review.json").write_text(
                "{}", encoding="utf-8",
            )
        elif self.mode == "partial-ledger":
            ledger = self.root / "vault/governance/curations/partial.json"
            ledger.parent.mkdir(parents=True, exist_ok=True)
            ledger.write_text(json.dumps({"proposals": [{
                "proposal_id": "prop-one", "path": "proposals/other/one.md",
                "sha256": "0" * 64,
            }]}), encoding="utf-8")
        elif self.mode == "committed-ledger":
            from caretaker.publication import sha
            from caretaker.pubstate import canonical_verdict

            manifest_path = Path(argv[7])
            manifest_bytes = manifest_path.read_bytes()
            manifest = json.loads(manifest_bytes)
            proposal = next(item for item in manifest["inputs"]
                            if item.get("path") == PROPOSAL)
            proposal_id = manifest["proposal_ids"][0]
            event_id = "evt-synthetic-committed"
            transaction_id = "tx-abcdef0123456789"
            digest = sha(manifest_bytes)
            ledger_path = self.root / "vault/governance/curations" / f"{event_id}.json"
            event_path = self.root / "vault/events/2026/10" / f"{event_id}.json"
            ledger_path.parent.mkdir(parents=True, exist_ok=True)
            event_path.parent.mkdir(parents=True, exist_ok=True)
            ledger_path.write_text(json.dumps({
                "proposals": [{
                    "proposal_id": proposal_id, "path": PROPOSAL,
                    "sha256": proposal["sha256"],
                }],
                "event_id": event_id, "transaction": transaction_id,
                "manifest_sha256": digest,
                "review": {"verdict": "PASS", "manifest_sha256": digest},
            }), encoding="utf-8")
            event_path.write_text(json.dumps({
                "event_id": event_id, "action": "record-curation",
                "proposal_ids": [proposal_id],
            }), encoding="utf-8")
            txdir = manifest_path.parent / ".curation-transactions"
            txdir.mkdir(parents=True, exist_ok=True)
            (txdir / f"{transaction_id}.json").write_text(json.dumps({
                "status": "committed", "event_id": event_id,
                "vault": str(self.root / "vault"),
                "manifest": str(manifest_path), "manifest_sha256": digest,
                "files": [{
                    "path": path.relative_to(self.root / "vault").as_posix(),
                    "after": sha(path.read_bytes()),
                } for path in (ledger_path, event_path)],
            }), encoding="utf-8")
            row = {"result": json.dumps({
                "proposal_ids": [proposal_id], "paths": [PROPOSAL],
                "hashes": [proposal["sha256"]],
                "state_dir": str(self.root / "state"),
            })}
            self.committed_verdict = canonical_verdict(
                self.root / "vault", row,
            )[0]
        elif self.mode == "repair-phase":
            phase_path = self.root / "state/curation" / (
                "phase-" + batch_key(PROJECT, [PROPOSAL]) + ".json"
            )
            record = json.loads(phase_path.read_text(encoding="utf-8"))
            record["phase"] = "REPAIR_PREPARED"
            record["cache_sha256"] = curator._phase_digest(record)
            phase_path.write_text(json.dumps(record), encoding="utf-8")


class PublishTimeoutDeferTests(unittest.TestCase):
    def setUp(self):
        private_temp = Path(os.environ.get("TEAM_TEST_ROOT") or tempfile.gettempdir())
        private_temp.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="publish-timeout-defer-", dir=private_temp))
        self.vault = self.root / "vault"
        self.state = self.root / "state"
        self.vault.mkdir()
        self.state.mkdir()
        self._write_vault()
        self.cfg = SimpleNamespace(
            state_dir=self.state,
            project_slug=PROJECT,
            hub=SimpleNamespace(
                vault=self.vault,
                script=self.root / "knowledge.py",
                python_executable="synthetic-python",
            ),
            publication=SimpleNamespace(
                maker_timeout_seconds=500,
                reviewer_timeout_seconds=500,
                reviewer=SimpleNamespace(mode="muse"),
            ),
            primary=SimpleNamespace(kind="synthetic"),
            fallback=SimpleNamespace(kind="synthetic"),
            raw={},
        )
        self.paths = [PROPOSAL]
        self.maker = Maker()
        self.reviewer = Reviewer()
        proposal_bytes = (self.vault / PROPOSAL).read_bytes()
        self.member = {
            "proposal_id": "prop-one", "path": PROPOSAL,
            "sha256": sha(proposal_bytes), "project": PROJECT,
            "producer": "codex", "episode": "", "source_ids": ["src-one"],
        }
        self.policy = {
            "enabled": True, "include_existing_hub_backlog": True,
            "projects": [PROJECT], "held_projects": [],
            "min_ready_groups": 1, "max_wait_hours": 24, "max_batch": 8,
        }
        self.summary = {
            "trigger": "threshold", "eligible": [self.member],
            "raw_eligible": 1, "group_count": 1, "ready_group_count": 1,
            "oldest_age_hours": 48, "deferred": 0, "unsupported": 0,
            "policy": policy_from(self.policy),
        }
        self.maint_cfg = {
            "hub": {"vault": str(self.vault)}, "project_slug": PROJECT,
            "state_dir": str(self.state),
        }
        self.database = sqlite3.connect(":memory:")
        ensure_tables(self.database)
        ensure_pub_tables(self.database)
        self.now = dt.datetime(2026, 10, 22, 12, 0, tzinfo=dt.timezone.utc)

    def _write_vault(self):
        registry = self.vault / "wiki/registry.md"
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text(
            "```json\n" + json.dumps({
                "schema_version": 1,
                "projects": {PROJECT: {"main_page": TARGET}},
                "aliases": {},
            }) + "\n```\n",
            encoding="utf-8",
        )
        page = self.vault / TARGET
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(
            "---\ntitle: Synthetic project\ntype: project\nstatus: reviewed\n"
            f"project_slug: {PROJECT}\nlast_verified: 2026-10-21\n"
            "verification_scope: synthetic\nsources: [src-one]\n---\n\nExisting reviewed context.\n",
            encoding="utf-8",
        )
        source_dir = self.vault / "raw/sources/2026/10/src-one"
        source_dir.mkdir(parents=True)
        source = b"Dated synthetic report: bounded local test, no external state."
        (source_dir / "original.md").write_bytes(source)
        (source_dir / "source.json").write_text(json.dumps({
            "source_id": "src-one", "classification": "internal",
            "local_only": False, "original_file": "original.md",
            "sha256": sha(source), "byte_length": len(source),
        }), encoding="utf-8")
        proposal = self.vault / PROPOSAL
        proposal.parent.mkdir(parents=True)
        proposal.write_text(
            "---\nid: prop-one\nclassification: internal\nsource_ids:\n"
            "  - src-one\ntarget_pages:\n  - wiki/projects/new-slug.md\n---\n"
            "Synthetic proposal.\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.database.close()

    def _tick(self, runner):
        def run_with_synthetic_config(_config, paths, publish, maintenance_lock_held=False):
            return curator._run_with_config(
                self.cfg, paths, publish, self.maker, self.reviewer,
            )

        gateway = SimpleNamespace(
            health=lambda: SimpleNamespace(status="pass"),
            lint=lambda: SimpleNamespace(status="pass"),
        )
        with mock.patch.object(maintenance, "_audit_summary", return_value=self.summary), \
             mock.patch("caretaker.config.load_config", return_value=self.cfg), \
             mock.patch.object(curator, "run", side_effect=run_with_synthetic_config), \
             mock.patch.object(curator, "CanonicalKnowledgeGateway", return_value=gateway), \
             mock.patch.object(curator, "_write_run_config", return_value=str(self.root / "run.json")), \
             mock.patch.object(curator, "checkpoint_or_pending", return_value={"status": "CHECKPOINTED"}), \
             mock.patch.object(curator.subprocess, "run", side_effect=runner.run):
            return maintenance.publication_tick(
                "synthetic-config", self.maint_cfg,
                {"publication": self.policy}, self.database,
                now=self.now + dt.timedelta(seconds=runner.publish_calls),
            )

    def _phase_path(self):
        return self.state / "curation" / (
            "phase-" + batch_key(PROJECT, self.paths) + ".json"
        )

    def test_normal_two_tick_timeout_resumes_without_repeating_models(self):
        runner = ScriptedSubprocess(self.root)
        first = self._tick(runner)
        key = batch_key(PROJECT, self.paths)
        cycle = active_cycle(self.database)
        row = read_row(self.database, key)
        self.assertEqual(first["status"], "DEFERRED_PREPARED", first)
        self.assertEqual(first["phase"], "REVIEW_ACCEPTED")
        self.assertEqual(first["model_calls"], 0)
        self.assertTrue(first["pre_effect"])
        self.assertEqual(row["status"], "PREPARED_DEFERRED")
        self.assertEqual(row["attempt"], 0)
        self.assertEqual(row["failures"], 0)
        self.assertEqual(cycle["status"], "ACTIVE")
        self.assertEqual(cycle["members"][0]["status"], "pending")
        self.assertEqual(self.maker.calls, 1)
        self.assertEqual(self.reviewer.calls, 1)

        second = self._tick(runner)
        self.assertEqual(second["status"], "PUBLISHED", second)
        self.assertEqual(self.maker.calls, 1)
        self.assertEqual(self.reviewer.calls, 1)
        self.assertEqual(runner.publish_calls, 2)
        self.assertEqual(read_row(self.database, key)["status"], "PUBLISHED")
        self.assertEqual(read_row(self.database, key)["attempt"], 1)
        self.assertIsNone(active_cycle(self.database))
        self.assertEqual(json.loads(self._phase_path().read_text(encoding="utf-8"))["phase"],
                         "REVIEW_ACCEPTED")

    def test_timeout_from_noncanonical_or_mismatched_argv_is_not_deferred(self):
        for index, mode in enumerate(("lock-timeout", "wrong-command")):
            with self.subTest(mode=mode):
                if index:
                    self.database.close()
                    self.setUp()
                runner = ScriptedSubprocess(self.root, mode=mode)
                result = self._tick(runner)
                self.assertEqual(result["status"], "BLOCKED", result)
                self.assertNotEqual(result.get("phase"), "REVIEW_ACCEPTED")
                self.assertEqual(read_row(
                    self.database, batch_key(PROJECT, self.paths),
                )["status"], "BLOCKED")

    def test_changed_source_partial_canonical_ledger_or_transaction_blocks_defer(self):
        for index, mode in enumerate(("changed-source", "partial-ledger", "applying-journal")):
            with self.subTest(mode=mode):
                if index:
                    self.database.close()
                    self.setUp()
                runner = ScriptedSubprocess(self.root, mode=mode)
                result = self._tick(runner)
                self.assertEqual(result["status"], "BLOCKED", result)
                self.assertNotEqual(result.get("pre_effect"), True)
                self.assertEqual(read_row(
                    self.database, batch_key(PROJECT, self.paths),
                )["status"], "BLOCKED")

    def test_changed_target_or_tampered_reviewed_candidate_blocks_defer(self):
        for index, mode in enumerate(("changed-target", "tampered-review")):
            with self.subTest(mode=mode):
                if index:
                    self.database.close()
                    self.setUp()
                runner = ScriptedSubprocess(self.root, mode=mode)
                result = self._tick(runner)
                self.assertEqual(result["status"], "BLOCKED", result)
                self.assertNotEqual(result.get("pre_effect"), True)
                self.assertEqual(read_row(
                    self.database, batch_key(PROJECT, self.paths),
                )["status"], "BLOCKED")

    def test_malformed_journal_and_complete_commit_proof_block_defer(self):
        for index, mode in enumerate(("malformed-journal", "committed-ledger")):
            with self.subTest(mode=mode):
                if index:
                    self.database.close()
                    self.setUp()
                runner = ScriptedSubprocess(self.root, mode=mode)
                result = self._tick(runner)
                self.assertEqual(result["status"], "BLOCKED", result)
                self.assertNotEqual(result.get("pre_effect"), True)
                if mode == "committed-ledger":
                    self.assertEqual(runner.committed_verdict, "committed")
                self.assertEqual(read_row(
                    self.database, batch_key(PROJECT, self.paths),
                )["status"], "BLOCKED")

    def test_repair_phase_is_never_deferred_as_a_reviewed_candidate(self):
        runner = ScriptedSubprocess(self.root, mode="repair-phase")
        result = self._tick(runner)
        self.assertEqual(result["status"], "BLOCKED", result)
        self.assertNotEqual(result.get("phase"), "REVIEW_ACCEPTED")
        self.assertEqual(self.maker.calls, 1)
        self.assertEqual(self.reviewer.calls, 1)

    def test_budget_clipped_repair_timeout_defers_and_resumes_through_normal_tick(self):
        clock = Clock()
        self.cfg.primary = TransportSpec(
            kind="openclaw_argv", executable="synthetic-openclaw",
            argv=["agent", "exec"],
        )
        self.cfg.fallback = TransportSpec(
            kind="openclaw_argv", executable="synthetic-openclaw",
            argv=["agent", "exec"],
        )
        maker_invocations = []
        observed_timeouts = []
        openclaw_transport = curator.OpenClawArgvTransport
        synthetic_reviewer = Reviewer([
            ("REPAIR", [{
                "class": "unsupported_claim", "weight": "blocking",
                "text": "synthetic unsupported claim",
            }]),
            "PASS",
        ])

        def run_maker(*, executable, argv, cwd, prompt, timeout):
            observed_timeouts.append(timeout)
            maker_invocations.append(timeout)
            if len(maker_invocations) == 2:
                raise subprocess.TimeoutExpired(cmd=executable, timeout=timeout)
            return 0, json.dumps({
                "ok": True, "status": "ok", "final": json.dumps(DECISION),
                "provider": "vllm", "model": "Qwen3.8-27B",
                "sessionId": f"synthetic-maker-{len(maker_invocations)}",
            }), ""

        synthetic_maker = openclaw_transport(
            run_callable=run_maker,
            expected_identity=("vllm", "Qwen3.8-27B"),
        )
        original_wire = curator._wire_fleet_publication

        def wire_synthetic_pair(**kwargs):
            kwargs["maker"] = synthetic_maker
            kwargs["reviewer"] = synthetic_reviewer
            return original_wire(**kwargs)

        def run_tick(*, with_budget):
            config_writes = 0

            def write_run_config(*_args, **_kwargs):
                nonlocal config_writes
                config_writes += 1
                # Spend the remaining tick budget after the repair phase's
                # timeout was chosen but before the transport enforces it.
                if with_budget and config_writes == 3:
                    clock.advance(210)
                return str(self.root / f"synthetic-run-{config_writes}.json")

            def publish_synthetic(_cfg, _paths, _run_root, result):
                result.update(
                    status="PUBLISHED",
                    publication={"status": "PUBLISHED"},
                    checkpoint={"status": "CHECKPOINTED"},
                )

            patches = [
                mock.patch.object(maintenance, "_audit_summary", return_value=self.summary),
                mock.patch("caretaker.config.load_config", return_value=self.cfg),
                mock.patch.object(curator, "load_config", return_value=self.cfg),
                mock.patch.object(curator, "_wire_fleet_publication", side_effect=wire_synthetic_pair),
                mock.patch.object(curator, "CanonicalKnowledgeGateway", return_value=SimpleNamespace(
                    health=lambda: SimpleNamespace(status="pass"),
                    lint=lambda: SimpleNamespace(status="pass"),
                )),
                mock.patch.object(curator, "_write_run_config", side_effect=write_run_config),
                mock.patch.object(curator, "_publish_retained", side_effect=publish_synthetic),
            ]
            for patcher in patches:
                patcher.start()
            try:
                if with_budget:
                    budget = tick_budget.TickBudget(seconds=600, clock=clock)
                    with budget.activate():
                        return maintenance.publication_tick(
                            "synthetic-config", self.maint_cfg,
                            {"publication": self.policy}, self.database,
                            now=self.now,
                        )
                return maintenance.publication_tick(
                    "synthetic-config", self.maint_cfg,
                    {"publication": self.policy}, self.database,
                    now=self.now + dt.timedelta(minutes=10),
                )
            finally:
                for patcher in reversed(patches):
                    patcher.stop()

        first = run_tick(with_budget=True)
        key = batch_key(PROJECT, self.paths)
        row = read_row(self.database, key)
        self.assertEqual(first["status"], "DEFERRED_PREPARED", first)
        self.assertEqual(first["phase"], "REPAIR_REQUIRED")
        self.assertEqual(first["pre_effect"], True)
        phase = json.loads(self._phase_path().read_text(encoding="utf-8"))
        self.assertEqual(row["status"], "PREPARED_DEFERRED")
        self.assertEqual(row["attempt"], 0)
        self.assertEqual(row["failures"], 0)
        self.assertEqual(active_cycle(self.database)["members"][0]["status"], "pending")
        self.assertEqual(phase["phase"], "REPAIR_REQUIRED")
        self.assertEqual(phase["progress"]["assessment"]["verdict"], "REPAIR")
        self.assertEqual(len(maker_invocations), 2)
        self.assertEqual(synthetic_reviewer.calls, 1)
        self.assertEqual(len(observed_timeouts), 2)
        self.assertLess(observed_timeouts[-1], 395)

        second = run_tick(with_budget=False)
        self.assertEqual(second["status"], "PUBLISHED", second)
        self.assertEqual(len(maker_invocations), 3)
        self.assertEqual(synthetic_reviewer.calls, 2)
        self.assertEqual(read_row(self.database, key)["status"], "PUBLISHED")
        self.assertIsNone(active_cycle(self.database))


if __name__ == "__main__":
    unittest.main()
