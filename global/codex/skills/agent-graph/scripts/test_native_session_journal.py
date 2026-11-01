"""Focused regression tests for individual native-session journals (RSI a6ca0d).

Covers maker+reviewer bridge integration, outcomes, preflight/identity checks,
gateway resolution, idempotent reuse, conflict detection, timeouts, and failure
isolation preserving product verdicts.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import gettempdir, mkdtemp
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True

# Never publish ClawOps Watch events or TODO telemetry from test suite
os.environ["AGENT_GRAPH_WATCH_EVENTS"] = "0"
_test_telemetry = patch.dict(os.environ, {"AGENT_TODO_TELEMETRY": "off", "AGENT_GRAPH_TIER_ROUTING": "0"})


def setUpModule():
    _test_telemetry.start()


def tearDownModule():
    _test_telemetry.stop()


SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import todo_graph
import native_session_journal

SPEC = importlib.util.spec_from_file_location("todo_fleet_under_test", SCRIPTS / "todo_fleet.py")
todo_fleet = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = todo_fleet
SPEC.loader.exec_module(todo_fleet)


FIXTURE_BASE = Path(os.environ.get("TEAM_TEST_ROOT") or gettempdir()) / "native-session-journal-fixtures"
FIXTURE_BASE.mkdir(parents=True, exist_ok=True)


class MockNativeFleet:
    SYNTHETIC_ONLY_PROFILE_ID = "opencode-beam"
    TOOL_BUDGET_ENFORCING_ADAPTERS = frozenset(
        {"codex", "claude", "qwen", "grok", "minimax", "muse", "openclaw", "mistral"}
    )

    class FleetError(Exception):
        pass

    class RoutingV2Error(Exception):
        pass

    def __init__(
        self,
        *,
        symbolic_ref=1,
        quota=None,
        result=None,
        execution_lineage="provider/maker",
        enable_session_journal=True,
    ):
        self.symbolic_ref = symbolic_ref
        self.quota = quota or {
            "weekly_remaining_fraction": 0.8,
            "short_window_remaining_fraction": 0.8,
            "current": True,
            "status": "fresh",
            "authority": "authoritative",
        }
        self.result = result or {
            "status": "COMPLETED",
            "model": "m-model",
            "run_id": "run-test-1",
        }
        self.execution_lineage = execution_lineage
        self.enable_session_journal = enable_session_journal
        self.execute_calls = []
        self.execute_args = []
        self.chosen = []
        self.quota_profiles = []
        self.DEFAULT_PROFILES = {
            "maker": {"adapter": "claude", "supported_modes": ["implement", "review", "advisory"]},
            "reviewer": {"adapter": "qwen", "supported_modes": ["review", "advisory"]},
            "opencode-beam": {"adapter": "synthetic", "supported_modes": ["implement", "review", "advisory"]},
        }
        self.adaptive = SimpleNamespace(normalize_task_profile=lambda value: value)
        self.routing_v2 = SimpleNamespace(
            resolve_execution_profile=self.resolve_execution_profile,
            hard_gates=lambda *args, **kwargs: [],
            RoutingV2Error=self.RoutingV2Error,
        )

    def run_git(self, argv, workspace):
        return SimpleNamespace(
            returncode=self.symbolic_ref if argv[0] == "symbolic-ref" else 0,
            stdout="abc\n",
        )

    def load_profiles(self, default):
        return self.DEFAULT_PROFILES

    def state_root(self, value):
        return Path(getattr(self, "_state_root_path", FIXTURE_BASE / "fake-fleet-state"))

    def live_readiness(self, profiles, **kwargs):
        self.chosen.extend(profiles)
        return {agent: {"ready": True} for agent in profiles}, {}

    def current_quota_views(self, root, profiles):
        self.quota_profiles.extend(profiles)
        return {agent: self.quota for agent in profiles}, {}

    def qualify_beam_workspace(self, profile_id, profile, workspace, *, now=None):
        return Path(workspace)

    def resolve_execution_profile(self, agent_id, profile, **kwargs):
        return {"model_provider_id": self.execution_lineage + "/" + agent_id}

    def execute_single(self, *args, **kwargs):
        self.execute_calls.append(kwargs)
        self.execute_args.append(args)
        return dict(self.result)

    def route_telemetry(self, route, **kwargs):
        return {"route": "fake"}

    def redact_sensitive_value(self, value):
        return value


class FakeFleet(MockNativeFleet):
    pass


class NativeSessionJournalTests(unittest.TestCase):
    def _restore_env(self):
        if self._old_tier_routing is None:
            os.environ.pop("AGENT_GRAPH_TIER_ROUTING", None)
        else:
            os.environ["AGENT_GRAPH_TIER_ROUTING"] = self._old_tier_routing
        if self._old_telemetry is None:
            os.environ.pop("AGENT_TODO_TELEMETRY", None)
        else:
            os.environ["AGENT_TODO_TELEMETRY"] = self._old_telemetry
        if self._old_userprofile is None:
            os.environ.pop("USERPROFILE", None)
        else:
            os.environ["USERPROFILE"] = self._old_userprofile
        for key in ("AGENT_MODEL_TIERS_PICKS", "AGENT_FLEET_RUNS_DIR", "AGENT_MODEL_TIERS_INDEX"):
            os.environ.pop(key, None)

    def setUp(self):
        self._old_tier_routing = os.environ.get("AGENT_GRAPH_TIER_ROUTING")
        self._old_telemetry = os.environ.get("AGENT_TODO_TELEMETRY")
        self._old_userprofile = os.environ.get("USERPROFILE")
        os.environ["AGENT_GRAPH_TIER_ROUTING"] = "0"
        os.environ["AGENT_TODO_TELEMETRY"] = "off"
        os.environ["AGENT_GRAPH_WATCH_EVENTS"] = "0"

        # Retained fixture root strictly below worktree/_do-usuniecia
        self.test_dir = Path(mkdtemp(prefix="journal-test-", dir=str(FIXTURE_BASE)))
        os.environ["USERPROFILE"] = str(self.test_dir)
        os.environ["AGENT_MODEL_TIERS_PICKS"] = str(self.test_dir / "picks.json")
        os.environ["AGENT_FLEET_RUNS_DIR"] = str(self.test_dir / "runs")
        os.environ["AGENT_MODEL_TIERS_INDEX"] = str(self.test_dir / "usage-index.json")
        self.addCleanup(self._restore_env)

        self.workspace = self.test_dir / "workspace"
        self.workspace.mkdir()
        self.outdir = self.test_dir / "out"
        self.outdir.mkdir()

        # Knowledge hub gateway mock structure
        self.hub_skill_dir = self.test_dir / "knowledge-skill"
        (self.hub_skill_dir / "scripts").mkdir(parents=True)
        self.gateway_path = self.hub_skill_dir / "scripts" / "knowledge.py"
        self.gateway_path.write_text("# gateway script stub\n", encoding="utf-8")

        self.config_path = self.test_dir / "knowledge-config.json"
        self.config_path.write_text(
            json.dumps({"canonical_skill_path": str(self.hub_skill_dir)}),
            encoding="utf-8",
        )

        self.home_patch = patch.object(Path, "home", return_value=self.test_dir)
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)

    def tearDown(self):
        # Retain fixtures under _do-usuniecia without deletion
        pass

    def make_candidate(self, agent, model="m-model", score=80):
        return {"agent": agent, "model": model, "reason": "test fit", "score": score}

    def parent_context(self, project="test-proj"):
        session="parent-session"
        path=self.test_dir/"bound-parent-context.json"
        path.write_text(json.dumps({"schema_version":2,"action":"begin-task","status":"COMPLETE",
            "coherent_read":True,"agent":"codex","project_slug":project,
            "session_sha256":hashlib.sha256(session.encode()).hexdigest(),
            "vault_binding":{"version":1,"kind":"local-execution-evidence","vault_path_sha256":"a"*64}}))
        return {"context_receipt":str(path),"context_receipt_agent":"codex","context_receipt_session":session}

    def make_task(self, *, mode="advisory", project_slug="test-proj", candidates=None):
        return {
            "id": "slice-1",
            "text": "perform test task",
            "mode": mode,
            "project_slug": project_slug,
            "scope": ["src"],
            "done_when": ["criteria met"],
            "maker": candidates or [self.make_candidate("maker")],
            "reviewer": candidates or [self.make_candidate("reviewer")],
            "complexity": "low",
            "review_complexity": "high",
        }

    def make_request(self, role="maker", task=None, project_slug=None, maker=None):
        req = {
            "goal": "test native session journal",
            "role": role,
            "workspace": str(self.workspace),
            "task": task or self.make_task(project_slug=project_slug or "test-proj"),
            "maker": maker,
        }
        if project_slug:
            req["project_slug"] = project_slug
        return req

    def write_usage_record(
        self,
        *,
        run_id="run-test-1",
        agent="maker",
        model="m-model",
        attested=True,
        identity_status="verified",
        provider_id="provider/maker",
        exit_code=0,
    ):
        path = self.test_dir / f"usage-{run_id}.json"
        data = {
            "run_id": run_id,
            "agent": agent,
            "model": model,
            "exit_code": exit_code,
            "completion_time": 1700000000.0,
            "completed_at": 1700000000.0,
            "runtime_attestation": {"status": "VERIFIED" if attested else "UNVERIFIED"},
            "model_identity": {"status": identity_status, "expected_model": model},
            "model_provider_id": provider_id,
        }
        todo_graph.write(path, data)
        return path

    def create_mock_gateway_runner(
        self,
        *,
        receipt_status="COMPLETE",
        outcome_override=None,
        session_sha256=None,
        checkpoint_key_override=None,
        exit_code=0,
        malformed=False,
    ):
        calls = []

        def runner(argv, timeout=240):
            calls.append(argv)
            if exit_code != 0:
                return SimpleNamespace(returncode=exit_code, stdout="", stderr="gateway error")
            if malformed:
                return SimpleNamespace(returncode=0, stdout="not json {", stderr="")

            # Parse argv to reflect expected values
            parsed = {}
            for i in range(len(argv)):
                if argv[i].startswith("--") and i + 1 < len(argv):
                    parsed[argv[i][2:]] = argv[i + 1]

            agent = parsed.get("agent", "maker")
            project = parsed.get("project-slug", "test-proj")
            sess_id = parsed.get("session-id", "run-test-1")
            cid = parsed.get("checkpoint-id") or parsed.get("checkpoint", "checkpoint-default")
            ckey = checkpoint_key_override or native_session_journal.compute_checkpoint_key(agent, project, sess_id, cid)
            sess_sha = session_sha256 if session_sha256 is not None else hashlib.sha256(sess_id.encode("utf-8")).hexdigest()

            resp = {
                "action": "record-session",
                "receipt_status": receipt_status,
                "outcome": outcome_override or parsed.get("outcome", "COMPLETED"),
                "session_sha256": sess_sha,
                "checkpoint_key": ckey,
                "project_slug": project,
                "agent": agent,
                "source_id": "src-1",
                "proposal_id": "prop-1",
                "source_ids": ["src-1"],
                "proposal_ids": ["prop-1"],
                "lifecycle_binding": "unverified",
            }
            return SimpleNamespace(returncode=0, stdout=json.dumps(resp), stderr="")

        runner.calls = calls
        return runner

    # 1. Maker bridge integration
    def test_maker_bridge_integration(self):
        usage = self.write_usage_record(run_id="run-test-1", agent="maker", model="m-model")
        fleet = MockNativeFleet(
            result={"status": "COMPLETED", "model": "m-model", "run_id": "run-test-1", "usage_record": str(usage)}
        )
        runner = self.create_mock_gateway_runner()

        req = self.make_request(role="maker", project_slug="test-proj")
        res = todo_fleet.execute(
            req,
            self.outdir / "maker-test",
            fleet=fleet,
            journal_runner=runner,
            gateway_config_path=self.config_path,
        )

        self.assertEqual(res["status"], "CANDIDATE")
        self.assertIn("session_journal", res)
        journal = res["session_journal"]
        self.assertEqual(journal["status"], "COMPLETE")
        self.assertEqual(journal["receipt_status"], "COMPLETE")
        self.assertEqual(journal["agent"], "maker")
        self.assertEqual(journal["outcome"], "COMPLETED")
        self.assertTrue(journal["hub_written"])
        self.assertIn("maker", journal["checkpoint_id"])
        expected_key = native_session_journal.compute_checkpoint_key("maker", "test-proj", "run-test-1", journal["checkpoint_id"])
        self.assertEqual(journal["checkpoint_key"], expected_key)
        self.assertEqual(len(runner.calls), 1)

        argv = runner.calls[0]
        self.assertIn("--checkpoint-id", argv)
        self.assertIn("--path", argv)
        self.assertIn("--classification", argv)
        self.assertEqual(argv[argv.index("--classification") + 1], "internal")
        self.assertTrue(argv[argv.index("--path") + 1].endswith(".md"))

    # 2. Reviewer bridge integration
    def test_reviewer_bridge_integration(self):
        usage = self.write_usage_record(
            run_id="run-rev-1", agent="reviewer", model="m-model", provider_id="provider/reviewer"
        )
        review_handoff = {
            "quality_gate": {
                "verdict": "ACCEPT",
                "findings": [],
                "checked": ["criteria met"],
                "summary": "verified all criteria",
            }
        }
        fleet = MockNativeFleet(
            result={
                "status": "COMPLETED",
                "model": "m-model",
                "run_id": "run-rev-1",
                "usage_record": str(usage),
                "handoff": review_handoff,
            },
            execution_lineage="provider/reviewer",
        )
        runner = self.create_mock_gateway_runner()

        req = self.make_request(
            role="reviewer",
            project_slug="test-proj",
            maker={"lineage": "provider/maker"},
        )
        res = todo_fleet.execute(
            req,
            self.outdir / "reviewer-test",
            fleet=fleet,
            journal_runner=runner,
            gateway_config_path=self.config_path,
        )

        self.assertEqual(res["status"], "REVIEWED")
        self.assertIn("session_journal", res)
        journal = res["session_journal"]
        self.assertEqual(journal["status"], "COMPLETE")
        self.assertEqual(journal["agent"], "reviewer")
        self.assertEqual(journal["outcome"], "COMPLETED")
        self.assertIn("reviewer", journal["checkpoint_id"])
        expected_key = native_session_journal.compute_checkpoint_key("reviewer", "test-proj", "run-rev-1", journal["checkpoint_id"])
        self.assertEqual(journal["checkpoint_key"], expected_key)
        self.assertEqual(len(runner.calls), 1)

    # 3. Completed outcome and explicit separate product acceptance note
    def test_completed_outcome_and_note(self):
        usage = self.write_usage_record(run_id="run-comp-1", agent="maker", model="m-model")
        fleet = MockNativeFleet(
            result={"status": "COMPLETED", "model": "m-model", "run_id": "run-comp-1", "usage_record": str(usage)}
        )
        runner = self.create_mock_gateway_runner()

        req = self.make_request(role="maker", project_slug="test-proj")
        outdir = self.outdir / "completed-test"
        res = todo_fleet.execute(req, outdir, fleet=fleet, journal_runner=runner, gateway_config_path=self.config_path)

        journal = res["session_journal"]
        self.assertEqual(journal["outcome"], "COMPLETED")

        # Verify note on disk
        note_file = outdir / "session-journal" / "note.md"
        self.assertTrue(note_file.is_file())
        note_data = json.loads(note_file.read_text(encoding="utf-8"))
        self.assertEqual(note_data["outcome"], "COMPLETED")
        self.assertEqual(note_data["product_acceptance"], "separate")
        self.assertIn("product acceptance remains separate", note_data["note"])

    # 4. Partial outcome maps to INTERRUPTED
    def test_partial_outcome_maps_to_interrupted(self):
        usage = self.write_usage_record(run_id="run-part-1", agent="maker", model="m-model")
        fleet = MockNativeFleet(
            result={"status": "PARTIAL", "model": "m-model", "run_id": "run-part-1", "usage_record": str(usage)}
        )
        runner = self.create_mock_gateway_runner()

        req = self.make_request(role="maker", project_slug="test-proj")
        res = todo_fleet.execute(req, self.outdir / "partial-test", fleet=fleet, journal_runner=runner, gateway_config_path=self.config_path)

        self.assertEqual(res["status"], "PARTIAL")
        journal = res["session_journal"]
        self.assertEqual(journal["outcome"], "INTERRUPTED")
        self.assertEqual(runner.calls[0][runner.calls[0].index("--outcome") + 1], "INTERRUPTED")

    # 5. Failed outcome maps to BLOCKED
    def test_failed_outcome_maps_to_blocked(self):
        usage = self.write_usage_record(run_id="run-fail-1", agent="maker", model="m-model", exit_code=1)
        fleet = MockNativeFleet(
            result={"status": "FAILED", "model": "m-model", "run_id": "run-fail-1", "usage_record": str(usage)}
        )
        runner = self.create_mock_gateway_runner()

        req = self.make_request(role="maker", project_slug="test-proj")
        res = todo_fleet.execute(req, self.outdir / "failed-test", fleet=fleet, journal_runner=runner, gateway_config_path=self.config_path)

        self.assertEqual(res["status"], "BLOCKED")
        journal = res["session_journal"]
        self.assertEqual(journal["outcome"], "BLOCKED")
        self.assertEqual(runner.calls[0][runner.calls[0].index("--outcome") + 1], "BLOCKED")

    # 6. No dispatch -> NOT_APPLICABLE and no Hub write
    def test_no_dispatch_no_hub_write(self):
        fleet = MockNativeFleet(
            quota={
                "availability_blocked": True,
                "status": "blocked",
                "current": True,
                "weekly_remaining_fraction": 0.0,
                "short_window_remaining_fraction": 0.0,
            }
        )
        runner = self.create_mock_gateway_runner()

        task = self.make_task()
        req = self.make_request(task=task)
        res = todo_fleet.execute(
            req,
            self.outdir / "no-dispatch",
            fleet=fleet,
            journal_runner=runner,
            gateway_config_path=self.config_path,
        )

        self.assertEqual(res["status"], "BLOCKED")
        journal = res.get("session_journal", {})
        self.assertEqual(journal.get("status"), "NOT_APPLICABLE")
        self.assertEqual(journal.get("receipt_status"), "NOT_APPLICABLE")
        self.assertEqual(journal.get("reason"), "no-native-dispatch")
        self.assertFalse(journal.get("hub_written", False))
        self.assertEqual(len(runner.calls), 0)
        self.assertEqual(len(fleet.execute_calls), 0)

    # 7. Wrong identity or unverified attestation -> UNVERIFIED without Hub write
    def test_wrong_identity_or_unverified_attestation(self):
        # Case A: unverified attestation
        usage_unattested = self.write_usage_record(run_id="run-unattested", attested=False)
        fleet_unattested = MockNativeFleet(
            result={"status": "COMPLETED", "model": "m-model", "run_id": "run-unattested", "usage_record": str(usage_unattested)}
        )
        runner_a = self.create_mock_gateway_runner()
        res_a = todo_fleet.execute(
            self.make_request(project_slug="p"),
            self.outdir / "unattested",
            fleet=fleet_unattested,
            journal_runner=runner_a,
            gateway_config_path=self.config_path,
        )
        self.assertEqual(res_a["status"], "BLOCKED")
        self.assertEqual(res_a["cause"], "runtime-attestation-failed")
        self.assertEqual(res_a["session_journal"]["status"], "UNVERIFIED")
        self.assertFalse(res_a["session_journal"]["hub_written"])
        self.assertEqual(len(runner_a.calls), 0)

        # Case B: identity mismatch
        usage_mismatch = self.write_usage_record(run_id="run-mismatch", identity_status="mismatch")
        fleet_mismatch = MockNativeFleet(
            result={"status": "COMPLETED", "model": "m-model", "run_id": "run-mismatch", "usage_record": str(usage_mismatch)}
        )
        runner_b = self.create_mock_gateway_runner()
        res_b = todo_fleet.execute(
            self.make_request(project_slug="p"),
            self.outdir / "mismatch",
            fleet=fleet_mismatch,
            journal_runner=runner_b,
            gateway_config_path=self.config_path,
        )
        self.assertEqual(res_b["status"], "BLOCKED")
        self.assertEqual(res_b["cause"], "model-identity-unverified")
        self.assertEqual(res_b["session_journal"]["status"], "UNVERIFIED")
        self.assertFalse(res_b["session_journal"]["hub_written"])
        self.assertEqual(len(runner_b.calls), 0)

        # Case C: started_at alone does not prove terminal episode
        usage_started = self.test_dir / "usage-started.json"
        todo_graph.write(usage_started, {
            "run_id": "run-started",
            "agent": "maker",
            "model": "m-model",
            "started_at": 1700000000.0,
            "runtime_attestation": {"status": "VERIFIED"},
            "model_identity": {"status": "verified", "expected_model": "m-model"},
            "model_provider_id": "provider/maker",
        })
        fleet_started = MockNativeFleet(
            result={"status": "COMPLETED", "model": "m-model", "run_id": "run-started", "usage_record": str(usage_started)}
        )
        runner_c = self.create_mock_gateway_runner()
        res_c = todo_fleet.execute(
            self.make_request(project_slug="p"),
            self.outdir / "started-only",
            fleet=fleet_started,
            journal_runner=runner_c,
            gateway_config_path=self.config_path,
        )
        self.assertEqual(res_c["session_journal"]["status"], "UNVERIFIED")
        self.assertEqual(res_c["session_journal"]["reason"], "insufficient-execution-evidence")
        self.assertFalse(res_c["session_journal"]["hub_written"])
        self.assertEqual(len(runner_c.calls), 0)

    # 8. Missing knowledge config -> BLOCKED
    def test_missing_config_blocked(self):
        usage = self.write_usage_record(run_id="run-noconfig", agent="maker")
        fleet = MockNativeFleet(
            result={"status": "COMPLETED", "model": "m-model", "run_id": "run-noconfig", "usage_record": str(usage)}
        )
        req = self.make_request(role="maker", project_slug="test-proj")
        res = todo_fleet.execute(
            req,
            self.outdir / "noconfig",
            fleet=fleet,
            journal_runner=self.create_mock_gateway_runner(),
            gateway_config_path=self.test_dir / "nonexistent-config.json",
        )
        self.assertEqual(res["status"], "CANDIDATE")  # Product verdict preserved!
        journal = res["session_journal"]
        self.assertEqual(journal["status"], "BLOCKED")
        self.assertEqual(journal["cause"], "gateway-resolution-error")
        self.assertFalse(journal["hub_written"])

    # 9. Missing project_slug -> BLOCKED without inventing generic project
    def test_missing_project_slug_blocked(self):
        usage = self.write_usage_record(run_id="run-noproj", agent="maker")
        fleet = MockNativeFleet(
            result={"status": "COMPLETED", "model": "m-model", "run_id": "run-noproj", "usage_record": str(usage)}
        )
        runner = self.create_mock_gateway_runner()

        task = self.make_task(project_slug=None)
        req = self.make_request(task=task, project_slug=None)
        res = todo_fleet.execute(req, self.outdir / "noproj", fleet=fleet, journal_runner=runner, gateway_config_path=self.config_path)

        self.assertEqual(res["status"], "CANDIDATE")  # Product verdict preserved!
        journal = res["session_journal"]
        self.assertEqual(journal["status"], "BLOCKED")
        self.assertEqual(journal["cause"], "project-slug-error")
        self.assertIn("missing-project-slug", journal["reason"])
        self.assertFalse(journal["hub_written"])
        self.assertEqual(len(runner.calls), 0)

    # 10. Project slug conflict -> BLOCKED
    def test_project_slug_conflict_blocked(self):
        usage = self.write_usage_record(run_id="run-conflict", agent="maker")
        fleet = MockNativeFleet(
            result={"status": "COMPLETED", "model": "m-model", "run_id": "run-conflict", "usage_record": str(usage)}
        )
        runner = self.create_mock_gateway_runner()

        task = self.make_task(project_slug="project-alpha")
        req = self.make_request(task=task, project_slug="project-beta")
        res = todo_fleet.execute(req, self.outdir / "conflict-proj", fleet=fleet, journal_runner=runner, gateway_config_path=self.config_path)

        self.assertEqual(res["status"], "CANDIDATE")  # Product verdict preserved!
        journal = res["session_journal"]
        self.assertEqual(journal["status"], "BLOCKED")
        self.assertEqual(journal["cause"], "project-slug-error")
        self.assertIn("project-slug-conflict", journal["reason"])
        self.assertFalse(journal["hub_written"])
        self.assertEqual(len(runner.calls), 0)

    # 11. Canonical gateway resolution from config
    def test_canonical_gateway_resolution(self):
        gateway_file = self.test_dir / "canonical" / "scripts" / "knowledge.py"
        gateway_file.parent.mkdir(parents=True)
        gateway_file.write_text("# canonical gateway\n", encoding="utf-8")
        custom_cfg = self.test_dir / "custom-knowledge-config.json"
        custom_cfg.write_text(
            json.dumps({"canonical_skill_path": str(self.test_dir / "canonical")}),
            encoding="utf-8",
        )

        usage = self.write_usage_record(run_id="run-canon-1", agent="maker")
        fleet = MockNativeFleet(
            result={"status": "COMPLETED", "model": "m-model", "run_id": "run-canon-1", "usage_record": str(usage)}
        )
        runner = self.create_mock_gateway_runner()

        req = self.make_request(project_slug="canon-proj")
        todo_fleet.execute(req, self.outdir / "canon", fleet=fleet, journal_runner=runner, gateway_config_path=custom_cfg)

        called_gateway = runner.calls[0][2]
        self.assertEqual(Path(called_gateway).resolve(), gateway_file.resolve())

    # 12. Parent context receipt is NOT passed to record-session
    def test_parent_receipt_not_passed(self):
        parent = self.parent_context("parent-proj")
        parent_receipt = Path(parent["context_receipt"])

        usage = self.write_usage_record(run_id="run-rcpt-1", agent="maker")
        fleet = MockNativeFleet(
            result={"status": "COMPLETED", "model": "m-model", "run_id": "run-rcpt-1", "usage_record": str(usage)}
        )
        runner = self.create_mock_gateway_runner()

        task = self.make_task(project_slug=None)
        req = self.make_request(task=task)
        req.update(parent)

        todo_fleet.execute(req, self.outdir / "no-parent-rcpt", fleet=fleet, journal_runner=runner, gateway_config_path=self.config_path)

        executed_argv = runner.calls[0]
        self.assertNotIn("--context-receipt", executed_argv)
        self.assertNotIn(str(parent_receipt), executed_argv)
        self.assertEqual(executed_argv[executed_argv.index("--project-slug")+1],"parent-proj")
        self.assertEqual(fleet.execute_calls[0]["knowledge_mode"],"none")

    # 13. Own correlation IDs and harness-mediated labels
    def test_own_ids_and_harness_mediated(self):
        usage = self.write_usage_record(agent="maker", run_id="my-run-99")
        fleet = MockNativeFleet(
            result={"status": "COMPLETED", "model": "m-model", "run_id": "my-run-99", "usage_record": str(usage)}
        )
        runner = self.create_mock_gateway_runner()

        req = self.make_request(role="maker", project_slug="proj-ids")
        outdir = self.outdir / "ids-test"
        res = todo_fleet.execute(req, outdir, fleet=fleet, journal_runner=runner, gateway_config_path=self.config_path)

        executed_argv = runner.calls[0]
        self.assertIn("--session-id", executed_argv)
        self.assertEqual(executed_argv[executed_argv.index("--session-id") + 1], "my-run-99")
        self.assertIn("--agent", executed_argv)
        self.assertEqual(executed_argv[executed_argv.index("--agent") + 1], "maker")

        journal = res["session_journal"]
        self.assertEqual(journal["session_id_source"], "fleet-run")
        self.assertEqual(journal["producer_authentication"], "none")
        self.assertEqual(journal["transport"], "harness-mediated")
        self.assertEqual(journal["lifecycle_binding"], "unverified")

        # Verify outbox file
        outbox = json.loads((outdir / "session-journal" / "outbox.json").read_text(encoding="utf-8"))
        self.assertEqual(outbox["session_id"], "my-run-99")
        self.assertEqual(outbox["session_id_source"], "fleet-run")
        self.assertEqual(outbox["transport"], "harness-mediated")

    # 14. Role-distinct checkpoints
    def test_role_distinct_checkpoints(self):
        task = self.make_task()
        run_id = "run-shared-123"

        maker_cid = native_session_journal.make_checkpoint_id(task, "maker", run_id)
        reviewer_cid = native_session_journal.make_checkpoint_id(task, "reviewer", run_id)

        self.assertNotEqual(maker_cid, reviewer_cid)
        self.assertIn("maker", maker_cid)
        self.assertIn("reviewer", reviewer_cid)
        self.assertIn(task["id"], maker_cid)
        self.assertIn(run_id, maker_cid)

        maker_key = native_session_journal.compute_checkpoint_key("maker", "test-proj", run_id, maker_cid)
        reviewer_key = native_session_journal.compute_checkpoint_key("reviewer", "test-proj", run_id, reviewer_cid)
        self.assertNotEqual(maker_key, reviewer_key)

    # 15. Malformed, nonzero, and blocked gateway receipts preserve product verdict
    def test_malformed_and_blocked_receipts(self):
        cases = [
            ("nonzero-exit", {"exit_code": 2}, "NONZERO_EXIT"),
            ("malformed-json", {"malformed": True}, "MALFORMED_RESPONSE"),
            ("incomplete", {"receipt_status": "INCOMPLETE"}, "INCOMPLETE"),
            ("checkpoint-blocked", {"receipt_status": "CHECKPOINT_BLOCKED"}, "CHECKPOINT_BLOCKED"),
            ("mismatched-outcome", {"outcome_override": "WRONG_OUTCOME"}, "CONFIRMATION_MISMATCH"),
            ("mismatched-sha256", {"session_sha256": "bad-sha"}, "CONFIRMATION_MISMATCH"),
            ("mismatched-key", {"checkpoint_key_override": "bad-key"}, "CONFIRMATION_MISMATCH"),
        ]

        for name, runner_kwargs, expected_receipt_status in cases:
            with self.subTest(name=name):
                usage = self.write_usage_record(run_id=f"run-{name}")
                fleet = MockNativeFleet(
                    result={"status": "COMPLETED", "model": "m-model", "run_id": f"run-{name}", "usage_record": str(usage)}
                )
                runner = self.create_mock_gateway_runner(**runner_kwargs)
                req = self.make_request(project_slug="test-proj")
                res = todo_fleet.execute(
                    req,
                    self.outdir / f"fail-{name}",
                    fleet=fleet,
                    journal_runner=runner,
                    gateway_config_path=self.config_path,
                )

                # Product verdict is NEVER mutated
                self.assertEqual(res["status"], "CANDIDATE")
                journal = res["session_journal"]
                self.assertEqual(journal["status"], "BLOCKED")
                self.assertEqual(journal["receipt_status"], expected_receipt_status)
                self.assertFalse(journal["hub_written"])

    # 16. Complete and idempotent reuse
    def test_complete_idempotent_reuse(self):
        usage = self.write_usage_record(agent="maker", run_id="run-reuse-1")
        fleet = MockNativeFleet(
            result={"status": "COMPLETED", "model": "m-model", "run_id": "run-reuse-1", "usage_record": str(usage)}
        )
        runner = self.create_mock_gateway_runner()

        outdir = self.outdir / "reuse-test"
        req = self.make_request(project_slug="test-proj")

        # First run calls gateway
        res1 = todo_fleet.execute(req, outdir, fleet=fleet, journal_runner=runner, gateway_config_path=self.config_path)
        self.assertEqual(res1["session_journal"]["status"], "COMPLETE")
        self.assertEqual(len(runner.calls), 1)

        # Second run reuses receipt without re-running gateway
        res2 = todo_fleet.execute(req, outdir, fleet=fleet, journal_runner=runner, gateway_config_path=self.config_path)
        self.assertEqual(res2["session_journal"]["status"], "COMPLETE")
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(res1["session_journal"]["payload_sha256"], res2["session_journal"]["payload_sha256"])

    # 17. Changed bytes triggers conflict
    def test_changed_bytes_conflict(self):
        usage = self.write_usage_record(agent="maker", run_id="run-conflict-bytes")
        fleet = MockNativeFleet(
            result={"status": "COMPLETED", "model": "m-model", "run_id": "run-conflict-bytes", "usage_record": str(usage)}
        )
        runner = self.create_mock_gateway_runner()

        outdir = self.outdir / "conflict-bytes"
        req1 = self.make_request(project_slug="test-proj")

        # First run
        res1 = todo_fleet.execute(req1, outdir, fleet=fleet, journal_runner=runner, gateway_config_path=self.config_path)
        self.assertEqual(res1["session_journal"]["status"], "COMPLETE")

        # Mutate outbox payload on disk to simulate payload collision / change
        outbox_file = outdir / "session-journal" / "outbox.json"
        outbox_file.write_bytes(b'{"action":"record-session","altered":true}')

        # Second run on same outdir with original request
        res2 = todo_fleet.execute(req1, outdir, fleet=fleet, journal_runner=runner, gateway_config_path=self.config_path)
        self.assertEqual(res2["status"], "CANDIDATE")  # Product verdict preserved!
        journal = res2["session_journal"]
        self.assertEqual(journal["status"], "BLOCKED")
        self.assertEqual(journal["receipt_status"], "CONFLICT")
        self.assertEqual(journal["cause"], "payload-conflict")

    # 18. Timeout and saved pending attempt cannot silently retry -> UNKNOWN
    def test_timeout_unknown_no_replay(self):
        usage = self.write_usage_record(agent="maker", run_id="run-timeout-1")
        fleet = MockNativeFleet(
            result={"status": "COMPLETED", "model": "m-model", "run_id": "run-timeout-1", "usage_record": str(usage)}
        )

        def timeout_runner(argv, timeout=240):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

        outdir = self.outdir / "timeout-test"
        req = self.make_request(project_slug="test-proj")

        # First run encounters timeout
        res1 = todo_fleet.execute(req, outdir, fleet=fleet, journal_runner=timeout_runner, gateway_config_path=self.config_path)
        self.assertEqual(res1["status"], "CANDIDATE")  # Product verdict preserved
        self.assertEqual(res1["session_journal"]["status"], "UNKNOWN")
        self.assertEqual(res1["session_journal"]["cause"], "gateway-timeout")

        # Pending marker was retained
        pending_file = outdir / "session-journal" / "pending.json"
        self.assertTrue(pending_file.is_file())

        # Second run: saved pending attempt cannot silently retry; UNKNOWN without calling runner
        called = []
        def normal_runner(argv, timeout=240):
            called.append(argv)
            return SimpleNamespace(returncode=0, stdout="{}", stderr="")

        res2 = todo_fleet.execute(req, outdir, fleet=fleet, journal_runner=normal_runner, gateway_config_path=self.config_path)
        self.assertEqual(res2["status"], "CANDIDATE")
        self.assertEqual(res2["session_journal"]["status"], "UNKNOWN")
        self.assertEqual(res2["session_journal"]["cause"], "pending-attempt-lost")
        self.assertEqual(len(called), 0)  # No retry!

    # 19. Publication failure preserves product verdict
    def test_publication_failure_preserves_product_verdict(self):
        usage = self.write_usage_record(run_id="run-exc-1", agent="maker")
        fleet = MockNativeFleet(
            result={"status": "COMPLETED", "model": "m-model", "run_id": "run-exc-1", "usage_record": str(usage)}
        )

        def crashing_runner(argv, timeout=240):
            raise OSError("Synthetic gateway crash")

        req = self.make_request(project_slug="test-proj")
        res = todo_fleet.execute(req, self.outdir / "crash", fleet=fleet, journal_runner=crashing_runner, gateway_config_path=self.config_path)

        self.assertEqual(res["status"], "CANDIDATE")
        self.assertEqual(res["session_journal"]["status"], "BLOCKED")
        self.assertEqual(res["session_journal"]["cause"], "gateway-invocation-error")

    # 20. Fake/synthetic adapter no real write
    def test_fake_adapter_no_real_write(self):
        usage = self.write_usage_record(run_id="run-fake-1", agent="maker")
        fleet = FakeFleet(
            result={"status": "COMPLETED", "model": "m-model", "run_id": "run-fake-1", "usage_record": str(usage)},
            enable_session_journal=False,
        )

        req = self.make_request(project_slug="test-proj")
        res = todo_fleet.execute(req, self.outdir / "fake-adapter", fleet=fleet, gateway_config_path=self.config_path)

        self.assertEqual(res["status"], "CANDIDATE")
        journal = res["session_journal"]
        self.assertEqual(journal["status"], "NOT_APPLICABLE")
        self.assertFalse(journal["hub_written"])

    # 21. Plan-level project_slug and FleetAdapter.invoke integration
    def test_fleet_adapter_forwards_project_slug(self):
        plan = {
            "goal": "test project forwarding",
            "workspace": str(self.workspace),
            "project_slug": "plan-slug-xyz",
            "constraints": [],
        }
        task = self.make_task(project_slug=None)
        adapter = todo_graph.FleetAdapter()

        called_requests = []

        def mock_command(argv, cwd, folder, timeout, env=None):
            req_file = folder / "request.json"
            if req_file.is_file():
                called_requests.append(json.loads(req_file.read_text(encoding="utf-8")))
            # Write a stub result.json
            todo_graph.write(folder / "result.json", {"status": "CANDIDATE", "session_journal": {"status": "COMPLETE"}})
            return {"exit_code": 0}

        with patch.object(todo_graph, "command", side_effect=mock_command):
            adapter.invoke(plan, task, "maker", self.workspace, self.outdir / "adapter-proj", {})

        self.assertEqual(len(called_requests), 1)
        self.assertEqual(called_requests[0].get("project_slug"), "plan-slug-xyz")

    def test_journal_io_failure_preserves_both_role_results(self):
        for role in ("maker", "reviewer"):
            with self.subTest(role=role):
                usage = self.write_usage_record(agent=role, run_id="run-io-" + role)
                fleet = MockNativeFleet(result={"status":"COMPLETED","model":"m-model",
                    "run_id":"run-io-" + role,"usage_record":str(usage),
                    "handoff":{"verdict":"ACCEPT","findings":[],"checked":["criteria met"],"summary":"Synthetic"}})
                with patch.object(native_session_journal,"record_native_session",side_effect=OSError("Synthetic I/O")):
                    result = todo_fleet.execute(self.make_request(role=role),self.outdir/role,
                        fleet=fleet,journal_runner=self.create_mock_gateway_runner(),gateway_config_path=self.config_path)
                self.assertEqual(result["status"],"CANDIDATE" if role=="maker" else "REVIEWED")
                self.assertEqual(result["session_journal"]["status"],"UNKNOWN")

    def test_typeerror_during_gateway_call_is_never_replayed(self):
        calls = []
        def runner(argv, **kwargs):
            calls.append(argv)
            raise TypeError("Synthetic failure after dispatch")
        usage = self.write_usage_record()
        fleet = MockNativeFleet(result={"status":"COMPLETED","model":"m-model",
            "run_id":"run-test-1","usage_record":str(usage)})
        args = dict(fleet=fleet,journal_runner=runner,gateway_config_path=self.config_path)
        request = self.make_request()
        first = todo_fleet.execute(request,self.outdir,**args)
        second = todo_fleet.execute(request,self.outdir,**args)
        self.assertEqual(first["status"],"CANDIDATE")
        self.assertEqual(second["session_journal"]["status"],"UNKNOWN")
        self.assertEqual(len(calls),1)

    def test_corrupt_confirmation_never_republishes(self):
        usage = self.write_usage_record()
        fleet = MockNativeFleet(result={"status":"COMPLETED","model":"m-model",
            "run_id":"run-test-1","usage_record":str(usage)})
        runner = self.create_mock_gateway_runner()
        args = dict(fleet=fleet,journal_runner=runner,gateway_config_path=self.config_path)
        request = self.make_request()
        first = todo_fleet.execute(request,self.outdir,**args)
        self.assertEqual(first["session_journal"]["status"],"COMPLETE")
        (self.outdir/"session-journal/receipt.json").write_text('{"status":"COMPLETE"}')
        second = todo_fleet.execute(request,self.outdir,**args)
        self.assertEqual(second["session_journal"]["status"],"UNKNOWN")
        self.assertEqual(len(runner.calls),1)

    def test_both_source_and_proposal_ids_are_required(self):
        for field in ("source_id","proposal_id"):
            base = self.create_mock_gateway_runner()
            def runner(argv, **kwargs):
                response = base(argv,**kwargs)
                value = json.loads(response.stdout)
                value[field] = None
                response.stdout = json.dumps(value)
                return response
            usage = self.write_usage_record()
            fleet = MockNativeFleet(result={"status":"COMPLETED","model":"m-model",
                "run_id":"run-test-1","usage_record":str(usage)})
            result = todo_fleet.execute(self.make_request(),self.outdir/field,fleet=fleet,
                journal_runner=runner,gateway_config_path=self.config_path)
            self.assertEqual(result["session_journal"]["receipt_status"],"CONFIRMATION_MISMATCH")

    def test_request_json_cannot_choose_gateway(self):
        usage = self.write_usage_record()
        fleet = MockNativeFleet(result={"status":"COMPLETED","model":"m-model",
            "run_id":"run-test-1","usage_record":str(usage)})
        request = self.make_request()
        request["_gateway_config_path"] = str(self.config_path)
        with patch.object(todo_fleet,"load_fleet",return_value=fleet), patch.object(subprocess,"run") as process:
            result = todo_fleet.execute(request,self.outdir)
        self.assertEqual(result["session_journal"]["cause"],"gateway-resolution-error")
        process.assert_not_called()

    def test_missing_or_mismatched_producer_fields_never_publish(self):
        for field in ("run_id","agent","model"):
            usage = self.write_usage_record()
            record = json.loads(usage.read_text())
            record[field] = "wrong"
            usage.write_text(json.dumps(record))
            fleet = MockNativeFleet(result={"status":"COMPLETED","model":"m-model",
                "run_id":"run-test-1","usage_record":str(usage)})
            runner = self.create_mock_gateway_runner()
            result = todo_fleet.execute(self.make_request(),self.outdir/field,fleet=fleet,
                journal_runner=runner,gateway_config_path=self.config_path)
            self.assertEqual(result["session_journal"]["status"],"UNVERIFIED")
            self.assertEqual(runner.calls,[])

    def test_parent_agent_must_be_identified_even_when_both_sides_match(self):
        for agent in (None,"","unknown"):
            parent=self.parent_context()
            path=Path(parent["context_receipt"])
            data=json.loads(path.read_text())
            data["agent"]=agent
            path.write_text(json.dumps(data))
            parent["context_receipt_agent"]=agent
            self.assertIsNone(native_session_journal.parent_project_slug(parent))

    def test_conflicting_task_project_fields_do_not_publish(self):
        usage=self.write_usage_record()
        fleet=MockNativeFleet(result={"status":"COMPLETED","model":"m-model",
            "run_id":"run-test-1","usage_record":str(usage)})
        task=self.make_task(project_slug="direct-project")
        task["knowledge"]={"project_slug":"other-project"}
        runner=self.create_mock_gateway_runner()
        result=todo_fleet.execute(self.make_request(task=task),self.outdir,fleet=fleet,
            journal_runner=runner,gateway_config_path=self.config_path)
        self.assertEqual(result["status"],"CANDIDATE")
        self.assertEqual(result["session_journal"]["cause"],"project-slug-error")
        self.assertIn("project-slug-conflict",result["session_journal"]["reason"])
        self.assertEqual(runner.calls,[])

    def test_parent_receipt_fallback_is_forwarded_as_routing_only(self):
        plan={"goal":"Bound parent routing",**self.parent_context()}
        task=self.make_task(project_slug=None)
        folder=self.outdir/"fallback"
        def command(argv,cwd,output,timeout,**kwargs):
            todo_graph.write(output/"result.json",{"status":"CANDIDATE"})
            return {"exit_code":0}
        with patch.object(todo_graph,"command",side_effect=command):
            todo_graph.FleetAdapter().invoke(plan,task,"maker",self.workspace,folder,None)
        request=json.loads((folder/"request.json").read_text())
        self.assertEqual(request["project_slug"],"test-proj")
        self.assertNotIn("context_receipt",request)
        self.assertNotIn("context_receipt_agent",request)

    def test_both_native_roles_keep_snapshot_policy_none(self):
        for role in ("maker","reviewer"):
            usage=self.write_usage_record(agent=role)
            fleet=MockNativeFleet(result={"status":"COMPLETED","model":"m-model",
                "run_id":"run-test-1","usage_record":str(usage),
                "handoff":{"verdict":"ACCEPT","findings":[],"checked":["criteria met"],"summary":"Synthetic"}})
            result=todo_fleet.execute(self.make_request(role=role),self.outdir/role,fleet=fleet,
                journal_runner=self.create_mock_gateway_runner(),gateway_config_path=self.config_path)
            self.assertEqual(result["session_journal"]["status"],"COMPLETE")
            self.assertEqual(fleet.execute_calls[0]["knowledge_mode"],"none")


if __name__ == "__main__":
    unittest.main()
