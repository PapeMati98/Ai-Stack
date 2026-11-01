"""RSI-736851: preserve verified MiniMax code after native strict-JSON rejection.

Before the fix the Graph classified every non-zero exit as ``provider-run-failed``
and discarded the review path for a real maker run that wrote valid work into
the worktree. The MiniMax-M3 adapter rejects its native handoff with exit_code 4
when the model output fails the strict-JSON schema; the original Fleet record
must stay unchanged and the bridge must hand the proven maker to the
declarative checks + one independent review.

These tests build a real synthetic Git fixture and an exact native-shaped
record. They cover:

* the shared narrow predicate on authoritative Fleet record facts
  (timeout / tool-step / context limits / wrong model / wrong provider /
  other error4 / missing or conflicting identity or runtime facts are all
  rejected)
* ``artifact_recovery.retained_candidate`` returning the proof for the
  structured-output case while preserving the proven maker-recovery path
  (exit_code 0 + handoff_parse_error)
* ``todo_fleet.blocked_cause`` reporting ``handoff-unparsed`` with a sentence
  that names the structured-output defect and forbids the
  "change executor / raise complexity" recommendation
* the ``todo_fleet.execute`` bridge turning the same record into a
  ``PARTIAL`` response with ``artifact_recovery`` (PENDING_TESTS_AND_REVIEW)
  while leaving the original Fleet record (FAILED, exit_code=4) unchanged
* fail-closed negatives: missing or mismatched identity, runtime, base,
  checkout, scope, and out-of-scope edits all block recovery
"""
import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SCRIPTS = Path(__file__).parent
sys.path.insert(0, str(SCRIPTS))

from artifact_recovery import (
    dirty_manifest,
    manifest,
    minimax_structured_output_rejection,
    retained_candidate,
)

# Never publish ClawOps Watch events from the test suite to the live Office board.
os.environ["AGENT_GRAPH_WATCH_EVENTS"] = "0"

SPEC = importlib.util.spec_from_file_location("todo_fleet_under_test", SCRIPTS / "todo_fleet.py")
todo_fleet = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = todo_fleet
SPEC.loader.exec_module(todo_fleet)


MINIMAX_AGENT = "minimax"
MINIMAX_MODEL = "MiniMax-M3"
# Matches references/model-catalog.json (lineage key, lowercase).
MINIMAX_LINEAGE = "minimax"
REJECTION_REASON = "minimax-structured-output-rejected"


def _attestation():
    return {"status": "VERIFIED", "package_fingerprint": "a" * 64,
            "payload_fingerprint": "b" * 64, "package_version": "0.9.93",
            "payload_file_count": 238, "runtime_root": str(Path.cwd().resolve())}


def _identity():
    return {"status": "verified", "expected_runtime_model": "minimax/MiniMax-M3",
            "observed_runtime_models": ["minimax/MiniMax-M3"],
            "provider_source": "provider", "provider_kind": "minimax-managed"}


def _model_facts():
    return {"agent": MINIMAX_AGENT, "model": MINIMAX_MODEL,
            "model_provider_id": MINIMAX_LINEAGE, "model_identity": _identity()}


def _git(cwd, *args, check=True, text=True):
    """Run a Git command and return its stdout text."""
    return subprocess.check_output(["git", "-C", str(cwd)] + list(args), text=text)


def _init_synthetic_repo(root, files):
    """Create a real Git repo with a single baseline commit for a fixture."""
    root.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (root / name).write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", *files.keys()], check=True)
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=Test",
         "-c", "user.email=test@local.invalid", "commit", "-qm", "Synthetic baseline"],
        check=True)
    return _git(root, "rev-parse", "HEAD").strip()


def _make_structured_rejection_record(*, base, workspace, overrides=None):
    """Build the exact native-shaped Fleet record for the MiniMax-M3 case.

    Exit 4 is the MiniMax adapter hard schema reject for the strict-JSON
    handoff. The original record stays FAILED; the bridge never rewrites it.
    """
    record = {
        "run_id": "run-minimax-rsi736851",
        "agent": MINIMAX_AGENT,
        "model": MINIMAX_MODEL,
        "model_provider_id": MINIMAX_LINEAGE,
        "runtime_attestation": _attestation(),
        "model_identity": _identity(),
        "worktree_base_commit_sha": base,
        "worktree_base_provenance": "preserved",
        "exit_code": 4,
        "provider_terminal_reason": "failed",
        "completion_reason": REJECTION_REASON,
        "transport_outcome": {"status": "failed", "reason": REJECTION_REASON},
        "execution_workspace": str(workspace),
    }
    if overrides:
        record.update(overrides)
    return record


def _make_preserved_result(base, workspace):
    return {
        "run_id": "run-minimax-rsi736851",
        "model": MINIMAX_MODEL,
        "artifact_disposition": {
            "status": "preserved",
            "base_commit_sha": base,
            "base_provenance": "preserved",
            "worktree": str(workspace),
        },
    }


def _make_positive_fixture(temp):
    """Set up a real Git fixture with one scoped file modified in-place."""
    root = Path(temp) / "workspace"
    base = _init_synthetic_repo(root, {"file.py": "before\n"})
    before_dirty = dirty_manifest(root)
    before = manifest(root, ["file.py"])
    (root / "file.py").write_text("after\n", encoding="utf-8")
    record = _make_structured_rejection_record(base=base, workspace=root)
    result = _make_preserved_result(base, root)
    kwargs = dict(
        workspace=root, scope=["file.py"], base=base,
        expected_model=MINIMAX_MODEL, expected_agent=MINIMAX_AGENT,
        lineage=MINIMAX_LINEAGE, before=before, before_dirty=before_dirty,
    )
    return root, record, result, kwargs


class FakeFleet:
    """Minimal Fleet stub sufficient for execute() to drive the bridge path."""

    TOOL_BUDGET_ENFORCING_ADAPTERS = frozenset(
        {"codex", "claude", "qwen", "grok", "minimax", "muse", "openclaw", "mistral"})

    class FleetError(Exception):
        pass

    class RoutingV2Error(Exception):
        pass

    def __init__(self, *, result=None, symbolic_ref=1, lineage=None):
        self.symbolic_ref = symbolic_ref
        self.lineage = lineage or MINIMAX_LINEAGE
        self.result = result or {"status": None, "model": MINIMAX_MODEL,
                                 "run_id": "run-minimax-rsi736851"}
        self.execute_calls = []
        self.execute_args = []
        self.DEFAULT_PROFILES = {
            MINIMAX_AGENT: {"adapter": MINIMAX_AGENT,
                            "supported_modes": ["implement", "review", "advisory"]},
            "reviewer": {"adapter": "reviewer",
                         "supported_modes": ["review", "advisory"]},
        }
        self.adaptive = SimpleNamespace(normalize_task_profile=lambda value: value)
        self.resolve_execution_profile = self._resolve_execution_profile
        self.routing_v2 = SimpleNamespace(
            resolve_execution_profile=self._resolve_execution_profile,
            hard_gates=lambda *args, **kwargs: [],
            RoutingV2Error=self.RoutingV2Error,
        )

    def run_git(self, argv, workspace):
        if argv[0] == "symbolic-ref":
            return SimpleNamespace(returncode=self.symbolic_ref, stdout="")
        if argv[0] == "rev-parse":
            return SimpleNamespace(returncode=0,
                                   stdout=_git(workspace, "rev-parse", "HEAD"))
        return SimpleNamespace(returncode=0, stdout="")

    def _resolve_execution_profile(self, *args, **kwargs):
        return {"model_provider_id": self.lineage}

    def load_profiles(self, default):
        return self.DEFAULT_PROFILES

    def state_root(self, value):
        return Path("C:/fake-fleet-state")

    def live_readiness(self, profiles, **kwargs):
        return {agent: {"ready": True} for agent in profiles}, {}

    def current_quota_views(self, root, profiles):
        quota = {"weekly_remaining_fraction": 0.8,
                 "short_window_remaining_fraction": 0.8,
                 "current": True, "status": "fresh", "authority": "authoritative"}
        return {agent: quota for agent in profiles}, {}

    def execute_single(self, *args, **kwargs):
        self.execute_calls.append(kwargs)
        self.execute_args.append(args)
        # Simulate the maker writing the scoped file in the worktree, so
        # artifact_before/dirty_before (captured before this call) and the
        # after-manifest (captured by retained_candidate) actually differ.
        workspace = kwargs.get("workspace")
        if isinstance(workspace, Path) or isinstance(workspace, str):
            root = Path(workspace)
            target = root / "file.py"
            if target.exists():
                target.write_text("after\n", encoding="utf-8")
        return dict(self.result)

    def route_telemetry(self, route, **kwargs):
        return {"route": "fake"}

    def redact_sensitive_value(self, value):
        return value


class PredicateTests(unittest.TestCase):
    """The shared narrow predicate accepts the exact MiniMax record shape."""

    def test_structured_record_facts_match_the_predicate(self):
        record = {**_model_facts(), "exit_code": 4, "provider_terminal_reason": "failed",
                  "completion_reason": REJECTION_REASON,
                  "transport_outcome": {"status": "failed", "reason": REJECTION_REASON},
                  "runtime_attestation": _attestation()}
        identity = _identity()
        self.assertTrue(minimax_structured_output_rejection(record, identity))

    def test_predicate_rejects_typed_risks(self):
        """Each typed risk fails one Fleet-record fact and is rejected."""
        good_record = {**_model_facts(), "exit_code": 4, "provider_terminal_reason": "failed",
                       "completion_reason": REJECTION_REASON,
                       "transport_outcome": {"status": "failed", "reason": REJECTION_REASON},
                       "runtime_attestation": _attestation()}
        good_identity = _identity()
        cases = {
            "timeout": {"provider_terminal_reason": "timeout"},
            "timeout flag": {"timed_out": True},
            "tool budget flag": {"tool_budget_exceeded": True},
            "context flag": {"context_guard": {"exceeded": True}},
            "context stop flag": {"context_stop_exceeded": True},
            "generic error terminal": {"provider_terminal_reason": "error"},
            "float exit": {"exit_code": 4.0},
            "wrong agent": {"agent": "muse"},
            "incomplete attestation": {"runtime_attestation": {"status": "VERIFIED"}},
            "foreign identity": {"model_identity": {**_identity(), "provider_kind": "foreign"}},

            "cancelled": {"provider_terminal_reason": "cancelled"},
            "tool-step-limit": {"provider_terminal_reason": "tool-step-limit",
                                "completion_reason": "tool-step-limit",
                                "transport_outcome": {"status": "failed",
                                                     "reason": "tool-step-limit"}},
            "context-limit": {"provider_terminal_reason": "context-exceeded",
                              "completion_reason": "context-exceeded",
                              "transport_outcome": {"status": "failed",
                                                   "reason": "context-exceeded"}},
            "wrong model": {"model": "gpt-5.6-luna"},
            "wrong provider": {"model_provider_id": "openai"},
            "other error4": {"completion_reason": "quota-exhausted",
                             "transport_outcome": {"status": "failed",
                                                  "reason": "quota-exhausted"}},
            "missing transport_outcome": {"transport_outcome": None},
            "transport success": {"transport_outcome": {"status": "complete",
                                                        "reason": "ok"}},
            "transport wrong reason": {"transport_outcome":
                                       {"status": "failed", "reason": "other-rejection"}},
            "missing completion_reason": {"completion_reason": None},
            "wrong completion_reason": {"completion_reason": "minimax-other-error"},
            "exit_code 0": {"exit_code": 0},
            "exit_code 1": {"exit_code": 1},
            "exit_code 124": {"exit_code": 124},
            "failed runtime": {"runtime_attestation": {"status": "FAILED"}},
            "missing runtime": {"runtime_attestation": None},
            "wrong terminal": {"provider_terminal_reason": "success"},
            "non-dict record": {"__non_dict__": True},
        }
        for name, fields in cases.items():
            with self.subTest(case=name):
                if name == "non-dict record":
                    self.assertFalse(minimax_structured_output_rejection(
                        "not-a-dict", good_identity))
                    return
                record = copy.deepcopy(good_record)
                record.update(fields)
                self.assertFalse(minimax_structured_output_rejection(record, good_identity),
                                 "Predicate accepted an unexpected shape: " + name)

    def test_predicate_rejects_identity_not_verified(self):
        record = {**_model_facts(), "exit_code": 4, "provider_terminal_reason": "failed",
                  "completion_reason": REJECTION_REASON,
                  "transport_outcome": {"status": "failed", "reason": REJECTION_REASON},
                  "runtime_attestation": _attestation()}
        for status in ("not-applicable", "mismatch", "unavailable", "pending", ""):
            with self.subTest(status=status):
                identity = {"status": status, "expected_model": MINIMAX_MODEL}
                self.assertFalse(minimax_structured_output_rejection(record, identity))

    def test_predicate_rejects_non_dict_identity(self):
        record = {**_model_facts(), "exit_code": 4, "provider_terminal_reason": "failed",
                  "completion_reason": REJECTION_REASON,
                  "transport_outcome": {"status": "failed", "reason": REJECTION_REASON},
                  "runtime_attestation": _attestation()}
        self.assertFalse(minimax_structured_output_rejection(record, None))
        self.assertFalse(minimax_structured_output_rejection(record, "verified"))


class RetainedCandidateTests(unittest.TestCase):
    """artifact_recovery.retained_candidate accepts the structured-output case."""

    def test_retained_candidate_returns_proof_for_structured_rejection(self):
        with tempfile.TemporaryDirectory() as temp:
            root, record, result, kwargs = _make_positive_fixture(temp)
            proof = retained_candidate(record, result, **kwargs)
            self.assertIsNotNone(proof)
            self.assertEqual(proof["acceptance_status"], "PENDING_TESTS_AND_REVIEW")
            self.assertEqual(proof["artifact_status"], "preserved-verified")
            self.assertEqual(proof["transport_status"], "handoff-unparsed")
            self.assertEqual(proof["knowledge_closeout"], "PARENT_PENDING")
            self.assertEqual(proof["model"], MINIMAX_MODEL)
            self.assertEqual(proof["lineage"], MINIMAX_LINEAGE)
            self.assertEqual(proof["base_commit"], kwargs["base"])
            self.assertIn("file.py", proof["changed"])
            self.assertIn("file.py", proof["files"])

    def test_retained_candidate_blocks_typed_risks_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root, record, result, kwargs = _make_positive_fixture(temp)
            cases = {
                "timeout": {"provider_terminal_reason": "timeout"},
                "tool-step-limit": {"provider_terminal_reason": "tool-step-limit",
                                    "completion_reason": "tool-step-limit",
                                    "transport_outcome":
                                        {"status": "failed", "reason": "tool-step-limit"}},
                "context-limit": {"provider_terminal_reason": "context-exceeded",
                                  "completion_reason": "context-exceeded",
                                  "transport_outcome":
                                      {"status": "failed", "reason": "context-exceeded"}},
                "wrong-model": {"model": "gpt-5.6-luna"},
                "wrong-provider": {"model_provider_id": "openai"},
                "other-error4": {"completion_reason": "quota-exhausted",
                                 "transport_outcome":
                                     {"status": "failed", "reason": "quota-exhausted"}},
                "wrong-exit-code": {"exit_code": 1},
                "wrong-terminal": {"provider_terminal_reason": "success"},
                "missing-runtime": {"runtime_attestation": None},
                "wrong-base": {"worktree_base_commit_sha": "0" * 40},
                "wrong-base-provenance": {"worktree_base_provenance": "unknown"},
                "missing-identity": {"model_identity": None},
                "mismatch-identity": {"model_identity":
                                      {"status": "mismatch", "expected_model": MINIMAX_MODEL}},
                "not-applicable-identity": {"model_identity":
                                            {"status": "not-applicable",
                                             "expected_model": MINIMAX_MODEL}},
            }
            for name, fields in cases.items():
                with self.subTest(case=name):
                    bad = copy.deepcopy(record)
                    bad.update(fields)
                    self.assertIsNone(retained_candidate(bad, result, **kwargs),
                                     "retention accepted an unexpected shape: " + name)

    def test_retained_candidate_blocks_out_of_scope_change(self):
        with tempfile.TemporaryDirectory() as temp:
            root, record, result, kwargs = _make_positive_fixture(temp)
            (root / "outside.py").write_text("unscoped\n", encoding="utf-8")
            self.assertIsNone(retained_candidate(record, result, **kwargs))

    def test_retained_candidate_blocks_quarantined_artifact(self):
        with tempfile.TemporaryDirectory() as temp:
            root, record, result, kwargs = _make_positive_fixture(temp)
            bad = copy.deepcopy(result)
            bad["artifact_disposition"] = "quarantined"
            self.assertIsNone(retained_candidate(record, bad, **kwargs))
            bad = copy.deepcopy(result)
            bad["artifact_disposition"]["status"] = "quarantined"
            self.assertIsNone(retained_candidate(record, bad, **kwargs))

    def test_existing_generic_exit0_retention_still_passes(self):
        """Proven maker-recovery path: exit_code 0 + handoff_parse_error is preserved."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = _init_synthetic_repo(root, {"file.py": "before\n"})
            before_dirty = dirty_manifest(root)
            before = manifest(root, ["file.py"])
            (root / "file.py").write_text("after\n", encoding="utf-8")
            record = _make_structured_rejection_record(base=base, workspace=root,
                overrides={"exit_code": 0, "provider_terminal_reason": "success",
                           "completion_reason": None, "transport_outcome": None,
                           "handoff_parse_error": "invalid-json"})
            result = _make_preserved_result(base, root)
            kwargs = dict(
                workspace=root, scope=["file.py"], base=base,
                expected_model=MINIMAX_MODEL, expected_agent=MINIMAX_AGENT,
                lineage=MINIMAX_LINEAGE, before=before, before_dirty=before_dirty,
            )
            proof = retained_candidate(record, result, **kwargs)
            self.assertIsNotNone(proof)
            self.assertEqual(proof["acceptance_status"], "PENDING_TESTS_AND_REVIEW")
            self.assertEqual(proof["artifact_status"], "preserved-verified")
            self.assertEqual(proof["transport_status"], "handoff-unparsed")
            self.assertEqual(proof["model"], MINIMAX_MODEL)


class BlockedCauseTests(unittest.TestCase):
    """todo_fleet.blocked_cause reports handoff-unparsed without executor pivot."""

    def cause(self, record, **overrides):
        identity = record.get("model_identity") or {}
        kwargs = {"attested": True, "identity": identity, "identity_ok": True,
                  "role": "maker", "handoff_missing": False}
        kwargs.update(overrides)
        return todo_fleet.blocked_cause({"status": None}, record, **kwargs)

    def test_structured_rejection_returns_handoff_unparsed(self):
        record = _make_structured_rejection_record(
            base="0" * 40, workspace=Path("/unused"))
        cause, reason = self.cause(record)
        self.assertEqual(cause, "handoff-unparsed")
        self.assertIn(REJECTION_REASON, reason)
        self.assertIn("exit_code=4", reason)
        # Must NOT recommend account changes or increasing complexity.
        self.assertNotIn("change executor", reason.casefold())
        self.assertNotIn("raise complexity", reason.casefold())

    def test_structured_rejection_reviewer_role_is_not_retention(self):
        """A reviewer that hits this case is not retention; maker is the only path."""
        record = _make_structured_rejection_record(
            base="0" * 40, workspace=Path("/unused"))
        cause, reason = self.cause(record, role="reviewer", handoff_missing=False)
        # Cause is still handoff-unparsed; execute() only consults
        # retained_candidate for the maker role, so the reviewer stays
        # blocked without a valid verdict and without a recorded recovery.
        self.assertEqual(cause, "handoff-unparsed")


class ExecuteIntegrationTests(unittest.TestCase):
    """todo_fleet.execute returns PARTIAL with artifact_recovery exactly once."""

    def setUp(self):
        # Tier routing exercises an installed catalog/usage file; the MiniMax
        # bridge path under test does not need it. Match test_todo_fleet.py.
        self._tier_env = os.environ.get("AGENT_GRAPH_TIER_ROUTING")
        os.environ["AGENT_GRAPH_TIER_ROUTING"] = "0"
        self.addCleanup(self._restore_tier_env)

    def _restore_tier_env(self):
        if self._tier_env is None:
            os.environ.pop("AGENT_GRAPH_TIER_ROUTING", None)
        else:
            os.environ["AGENT_GRAPH_TIER_ROUTING"] = self._tier_env

    def _request(self, workspace):
        return {
            "goal": "RSI-736851 MiniMax structured-output retention",
            "role": "maker",
            "workspace": str(workspace),
            "task": {
                "id": "rsi736851",
                "text": "fix script",
                "mode": "implement",
                "scope": ["file.py"],
                "inputs": [],
                "done_when": ["verified-script-passes"],
                "maker": [{"agent": MINIMAX_AGENT, "model": MINIMAX_MODEL,
                           "reason": "fit", "score": 80}],
                "reviewer": [{"agent": "reviewer", "model": "r-model",
                              "reason": "fit", "score": 80}],
                "complexity": "low",
                "review_complexity": "high",
            },
        }

    def test_execute_returns_partial_with_recovery_unchanged_fleet_record(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            base = _init_synthetic_repo(workspace, {"file.py": "before\n"})
            usage_path = root / "usage.json"
            record = _make_structured_rejection_record(base=base, workspace=workspace)
            usage_path.write_text(json.dumps(record), encoding="utf-8")

            result_payload = {"status": None, "model": MINIMAX_MODEL,
                              "run_id": record["run_id"],
                              "usage_record": str(usage_path),
                              "artifact_disposition": {
                                  "status": "preserved", "base_commit_sha": base,
                                  "base_provenance": "preserved",
                                  "worktree": str(workspace)}}

            fleet = FakeFleet(result=result_payload, symbolic_ref=1)
            outdir = root / "out"

            with patch.object(Path, "home", return_value=root):
                response = todo_fleet.execute(self._request(workspace), outdir, fleet=fleet)

            self.assertEqual(response["status"], "PARTIAL")
            self.assertEqual(response["cause"], "handoff-unparsed")
            self.assertEqual(response["transport_status"], "handoff-unparsed")
            self.assertEqual(response["artifact_status"], "preserved-verified")
            self.assertEqual(response["acceptance_status"], "PENDING_TESTS_AND_REVIEW")
            self.assertEqual(response["knowledge_closeout"], "PARENT_PENDING")
            self.assertIn("artifact_recovery", response)
            recovery = response["artifact_recovery"]
            self.assertEqual(recovery["model"], MINIMAX_MODEL)
            self.assertEqual(recovery["lineage"], MINIMAX_LINEAGE)
            self.assertEqual(recovery["acceptance_status"], "PENDING_TESTS_AND_REVIEW")
            self.assertIn("file.py", recovery["changed"])

            # Original Fleet record stays unchanged: still FAILED exit 4.
            persisted = json.loads(usage_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["exit_code"], 4)
            self.assertEqual(persisted["provider_terminal_reason"], "failed")
            self.assertEqual(persisted["completion_reason"], REJECTION_REASON)
            self.assertEqual(persisted["transport_outcome"]["status"], "failed")
            self.assertEqual(persisted["transport_outcome"]["reason"], REJECTION_REASON)
            self.assertEqual(persisted["runtime_attestation"]["status"], "VERIFIED")
            self.assertEqual(persisted["model_identity"]["status"], "verified")

            # Exactly one model call was placed (no rerun).
            self.assertEqual(len(fleet.execute_calls), 1)

    def test_execute_blocks_when_runtime_attestation_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            base = _init_synthetic_repo(workspace, {"file.py": "before\n"})
            usage_path = root / "usage.json"
            record = _make_structured_rejection_record(base=base, workspace=workspace)
            record["runtime_attestation"] = {"status": "FAILED"}
            usage_path.write_text(json.dumps(record), encoding="utf-8")

            result_payload = {"status": None, "model": MINIMAX_MODEL,
                              "run_id": record["run_id"],
                              "usage_record": str(usage_path)}
            fleet = FakeFleet(result=result_payload)
            outdir = root / "out"

            with patch.object(Path, "home", return_value=root):
                response = todo_fleet.execute(self._request(workspace), outdir, fleet=fleet)

            self.assertEqual(response["status"], "BLOCKED")
            self.assertEqual(response["cause"], "runtime-attestation-failed")
            self.assertNotIn("artifact_recovery", response)

    def test_execute_blocks_when_identity_mismatch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            base = _init_synthetic_repo(workspace, {"file.py": "before\n"})
            usage_path = root / "usage.json"
            record = _make_structured_rejection_record(base=base, workspace=workspace)
            record["model_identity"] = {"status": "mismatch",
                                         "expected_model": MINIMAX_MODEL}
            usage_path.write_text(json.dumps(record), encoding="utf-8")

            result_payload = {"status": None, "model": MINIMAX_MODEL,
                              "run_id": record["run_id"],
                              "usage_record": str(usage_path)}
            fleet = FakeFleet(result=result_payload)
            outdir = root / "out"

            with patch.object(Path, "home", return_value=root):
                response = todo_fleet.execute(self._request(workspace), outdir, fleet=fleet)

            # Identity mismatch with exit_code 4 is reported as provider-run-failed
            # (per the existing order: a process that died cannot also be a clean
            # identity verdict). Either way, retention is blocked and the original
            # Fleet record stays unchanged.
            self.assertEqual(response["status"], "BLOCKED")
            self.assertNotEqual(response["cause"], "handoff-unparsed")
            self.assertNotIn("artifact_recovery", response)
            persisted = json.loads(usage_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["exit_code"], 4)
            self.assertEqual(persisted["model_identity"]["status"], "mismatch")

    def test_execute_blocks_when_other_error4(self):
        """Exit_code 4 with a non-structured-output reason stays blocked."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            base = _init_synthetic_repo(workspace, {"file.py": "before\n"})
            usage_path = root / "usage.json"
            record = _make_structured_rejection_record(base=base, workspace=workspace)
            record["completion_reason"] = "quota-exhausted"
            record["transport_outcome"] = {"status": "failed", "reason": "quota-exhausted"}
            usage_path.write_text(json.dumps(record), encoding="utf-8")

            result_payload = {"status": None, "model": MINIMAX_MODEL,
                              "run_id": record["run_id"],
                              "usage_record": str(usage_path)}
            fleet = FakeFleet(result=result_payload)
            outdir = root / "out"

            with patch.object(Path, "home", return_value=root):
                response = todo_fleet.execute(self._request(workspace), outdir, fleet=fleet)

            self.assertEqual(response["status"], "BLOCKED")
            self.assertEqual(response["cause"], "provider-run-failed")
            self.assertNotIn("artifact_recovery", response)


if __name__ == "__main__":
    unittest.main()