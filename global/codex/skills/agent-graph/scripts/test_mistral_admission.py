"""Native Mistral check admission: an incompatible check never reaches a model.

RSI HD-807892. The installed Fleet runtime supplies the real native policy
(build_policy) and Fleet's own check conversion (convert_check_argv); the
Fleet transport is fake, so no provider login, Docker, model call or check
execution happens in this file. Synthetic fixtures only.
"""
import io
import json
import os
import sys
import types
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parent
# The bridge under test is the copy the harness put first on sys.path: the
# baseline copy for the RED replay, the candidate copy for the check run.
if str(SCRIPTS) not in sys.path:
    sys.path.append(str(SCRIPTS))
import todo_fleet

try:
    from test_todo_fleet import FakeFleet
except Exception:  # the sibling fake transport ships next to this module
    FakeFleet = None


def real_mistral_runtime():
    """The installed Fleet native Mistral runtime, or None to skip."""
    try:
        with redirect_stderr(io.StringIO()):
            scripts = todo_fleet.resolve_fleet_root() / "agent-fleet" / "scripts"
    except BaseException:
        return None
    if not (scripts / "mistral_runtime.py").is_file():
        return None
    if str(scripts) not in sys.path:
        sys.path.append(str(scripts))
    try:
        import mistral_runtime
    except Exception:
        return None
    return mistral_runtime


REAL_MISTRAL = real_mistral_runtime()

CATALOG_MODELS = [
    {"model_id": "mistral-large-4", "display": "Mistral Large 4", "lineage": "mistral",
     "tier": "K", "lifecycle": "active", "roles": ["execute", "review"],
     "profiles": [{"agent": "mistral", "model": "mistral-large-4"}]},
    {"model_id": "synthetic-large-4", "display": "Synthetic Large 4", "lineage": "synthetic",
     "tier": "K", "lifecycle": "active", "roles": ["execute", "review"],
     "profiles": [{"agent": "antigravity", "model": "synthetic-large-4"}]},
]
MISTRAL_MODEL = "mistral-large-4"
OTHER_MODEL = "synthetic-large-4"


@unittest.skipIf(REAL_MISTRAL is None, "installed Fleet Mistral runtime unavailable")
@unittest.skipIf(FakeFleet is None, "sibling fake Fleet transport unavailable")
class MistralAdmissionTests(unittest.TestCase):
    def _restore_tier_env(self):
        if self._tier_env is None:
            os.environ.pop("AGENT_GRAPH_TIER_ROUTING", None)
        else:
            os.environ["AGENT_GRAPH_TIER_ROUTING"] = self._tier_env

    def setUp(self):
        # Tier routing stays on: the tier fallback is one of the covered paths.
        self._tier_env = os.environ.get("AGENT_GRAPH_TIER_ROUTING")
        os.environ["AGENT_GRAPH_TIER_ROUTING"] = "1"
        self.addCleanup(self._restore_tier_env)
        self.temp_context = TemporaryDirectory()
        self.addCleanup(self.temp_context.cleanup)
        self.root = Path(self.temp_context.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.home_patch = patch.object(Path, "home", return_value=self.root)
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.catalog_path = self.root / "catalog.json"
        self.write_catalog(["mistral", "antigravity"])
        self.env_patch = patch.dict(os.environ, {
            "AGENT_MODEL_CATALOG": str(self.catalog_path),
            "AGENT_MODEL_TIERS_PICKS": str(self.root / "picks.json"),
            "AGENT_MODEL_TIERS_INDEX": str(self.root / "usage-index.json"),
            "AGENT_FLEET_RUNS_DIR": str(self.root / "runs"),
        })
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def write_catalog(self, agents):
        models = [model for model in CATALOG_MODELS
                  if model["profiles"][0]["agent"] in agents]
        self.catalog_path.write_text(json.dumps(
            {"schema_version": 1, "policy": {"plan_weights": {}}, "models": models}),
            encoding="utf-8")

    def candidate(self, agent, model, score=90):
        return {"agent": agent, "model": model, "reason": "synthetic fit", "score": score}

    def task(self, *, mode="implement", checks=None, candidates=None, scope=None):
        default = [self.candidate("mistral", MISTRAL_MODEL)]
        return {
            "id": "slice-1", "text": "one bounded slice", "mode": mode,
            "scope": list(scope) if scope is not None else ["src"],
            "inputs": [],
            "checks": [] if checks is None else list(checks),
            "done_when": ["evidence"],
            "maker": candidates if candidates is not None else default,
            "reviewer": candidates if candidates is not None else default,
            "complexity": "low", "review_complexity": "low",
        }

    def request(self, role="maker", task=None):
        return {"goal": "synthetic bridge check", "role": role,
                "workspace": str(self.workspace), "task": task or self.task(), "maker": None}

    def fleet(self, *agents):
        fleet = FakeFleet()
        fleet.mistral_runtime = REAL_MISTRAL
        fleet.DEFAULT_PROFILES = {
            agent: {"adapter": "mistral" if agent == "mistral" else "claude",
                    "supported_modes": ["implement", "review", "advisory"]}
            for agent in agents
        }
        return fleet

    def execute(self, request, fleet, outdir):
        outdir.mkdir(parents=True, exist_ok=True)
        with patch("artifact_recovery.dirty_manifest", return_value={}):
            return todo_fleet.execute(request, outdir, fleet=fleet)

    def refused_reasons(self, outdir):
        skipped = outdir / "preflight-skipped.json"
        if not skipped.is_file():
            return []
        return [row["reason"] for row in json.loads(skipped.read_text(encoding="utf-8"))]

    def test_declared_mistral_candidate_with_a_node_check_is_refused_before_dispatch(self):
        for argv in (["node", "tests/app.test.js"],
                     ["npx", "tsc", "--noEmit"],
                     ["npm", "run", "check", "&&", "python", "-B", "verify.py"]):
            with self.subTest(argv=argv):
                fleet = self.fleet("mistral", "antigravity")
                task = self.task(candidates=[self.candidate("mistral", MISTRAL_MODEL),
                                             self.candidate("antigravity", OTHER_MODEL)],
                                 checks=[argv])
                outdir = self.root / ("explicit-" + argv[0].replace("/", "-"))
                self.execute(self.request(task=task), fleet, outdir)
                self.assertEqual([call["agent_id"] for call in fleet.execute_calls],
                                 ["antigravity"])
                self.assertNotIn("mistral_tool_dispatch", fleet.execute_calls[0])
                reasons = self.refused_reasons(outdir)
                self.assertTrue(reasons, "the refused candidate must stay visible")
                self.assertIn("Mistral native admission refused", reasons[0])

    def test_tier_fallback_picks_another_profile_after_an_unsupported_mistral(self):
        fleet = self.fleet("mistral", "antigravity")
        task = self.task(candidates=[self.candidate("mistral", MISTRAL_MODEL)],
                         checks=[["node", "tests/app.test.js"]])
        outdir = self.root / "tier"
        self.execute(self.request(task=task), fleet, outdir)
        self.assertEqual([call["agent_id"] for call in fleet.execute_calls], ["antigravity"])
        self.assertNotIn("mistral_tool_dispatch", fleet.execute_calls[0])
        self.assertIn("Mistral native admission refused", self.refused_reasons(outdir)[0])

    def test_every_mistral_candidate_refused_blocks_without_a_model_call(self):
        self.write_catalog(["mistral"])
        fleet = self.fleet("mistral")
        task = self.task(candidates=[self.candidate("mistral", MISTRAL_MODEL)],
                         checks=[["node", "tests/app.test.js"]])
        outdir = self.root / "blocked"
        result = self.execute(self.request(task=task), fleet, outdir)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(fleet.execute_calls, [])
        reasons = [row["reason"] for row in result["preflight"]]
        self.assertTrue(any("Mistral native admission refused" in reason for reason in reasons))
        for reason in reasons:
            self.assertLessEqual(len(reason), 400)

    def test_read_only_review_receives_no_check_execution(self):
        fleet = self.fleet("mistral")
        task = self.task(checks=[["node", "tests/app.test.js"]])
        outdir = self.root / "review"
        self.execute(self.request(role="reviewer", task=task), fleet, outdir)
        self.assertEqual([call["agent_id"] for call in fleet.execute_calls], ["mistral"])
        dispatch = fleet.execute_calls[0]["mistral_tool_dispatch"]
        self.assertEqual(dispatch["checks"], [])
        self.assertEqual(dispatch["scope"], ["src"])
        self.assertEqual(dispatch["inputs"], [])

    def test_supported_python_check_keeps_the_exact_trusted_dispatch(self):
        for argv in ([sys.executable, "-B", "-c", "assert True"],
                     ["python", "-B", "-m", "pytest", "tests"]):
            with self.subTest(argv=argv):
                fleet = self.fleet("mistral")
                task = self.task(checks=[argv])
                outdir = self.root / ("python-" + str(len(argv)))
                self.execute(self.request(task=task), fleet, outdir)
                self.assertEqual([call["agent_id"] for call in fleet.execute_calls], ["mistral"])
                dispatch = fleet.execute_calls[0]["mistral_tool_dispatch"]
                self.assertEqual(dispatch["scope"], ["src"])
                self.assertEqual(dispatch["inputs"], [])
                self.assertEqual(dispatch["checks"], task["checks"])
                self.assertEqual(fleet.execute_calls[0]["max_model_tool_calls"], 48)

    def test_writer_bound_refuses_the_mistral_candidate(self):
        self.write_catalog(["mistral"])
        (self.workspace / "big.txt").write_bytes(b"synthetic bound probe" * 30000)
        fleet = self.fleet("mistral")
        task = self.task(scope=["big.txt"], checks=[[sys.executable, "-B", "verify.py"]])
        outdir = self.root / "writer-bound"
        result = self.execute(self.request(task=task), fleet, outdir)
        self.assertEqual(fleet.execute_calls, [])
        self.assertEqual(result["status"], "BLOCKED")
        reasons = [row["reason"] for row in result["preflight"]]
        self.assertTrue(any("writer" in reason.lower() for reason in reasons), reasons)

    def test_check_path_outside_the_workspace_is_refused(self):
        outside = self.root / "outside.py"
        outside.write_text("synthetic = 1\n", encoding="utf-8")
        self.write_catalog(["mistral"])
        fleet = self.fleet("mistral")
        task = self.task(checks=[[sys.executable, "-B", str(outside)]])
        outdir = self.root / "outside-path"
        result = self.execute(self.request(task=task), fleet, outdir)
        self.assertEqual(fleet.execute_calls, [])
        self.assertEqual(result["status"], "BLOCKED")
        reasons = [row["reason"] for row in result["preflight"]]
        self.assertTrue(any("workspace" in reason for reason in reasons), reasons)

    def python_assertion(self):
        return [sys.executable, "-B", "-c", "assert True"]

    def test_missing_native_runtime_refuses_even_if_a_global_module_would_admit(self):
        self.write_catalog(["mistral"])
        fake = types.ModuleType("mistral_runtime")
        fake.MistralRuntimeError = type("MistralRuntimeError", (Exception,), {})
        fake.bind_tool_policy = lambda *args, **kwargs: {"checks": []}
        fake.tool_module = lambda: types.SimpleNamespace(
            convert_check_argv=lambda policy, argv: list(argv))
        fleet = self.fleet("mistral")
        fleet.mistral_runtime = None
        task = self.task(checks=[["node", "tests/app.test.js"]])
        outdir = self.root / "no-runtime"
        with patch.dict(sys.modules, {"mistral_runtime": fake}):
            result = self.execute(self.request(task=task), fleet, outdir)
        self.assertEqual(fleet.execute_calls, [])
        self.assertEqual(result["status"], "BLOCKED")
        reasons = [row["reason"] for row in result["preflight"]]
        self.assertTrue(any(
            "Mistral native admission refused: native runtime unavailable" in reason
            for reason in reasons), reasons)

    def test_missing_native_conversion_refuses_before_bind(self):
        self.write_catalog(["mistral"])
        binds = []

        class Runtime:
            class MistralRuntimeError(Exception):
                pass

            def bind_tool_policy(self, *args, **kwargs):
                binds.append(args)
                return {"checks": []}

            def tool_module(self):
                return types.SimpleNamespace()

        fleet = self.fleet("mistral")
        fleet.mistral_runtime = Runtime()
        task = self.task(checks=[self.python_assertion()])
        outdir = self.root / "no-convert"
        result = self.execute(self.request(task=task), fleet, outdir)
        self.assertEqual(binds, [])
        self.assertEqual(fleet.execute_calls, [])
        self.assertEqual(result["status"], "BLOCKED")
        reasons = [row["reason"] for row in result["preflight"]]
        self.assertTrue(any(
            "native check conversion unavailable" in reason for reason in reasons), reasons)

    def test_redactor_failure_returns_a_content_free_constant(self):
        secret = "api_key=secret-value"
        fleet = self.fleet("mistral")
        samples = (
            None,
            lambda value: (_ for _ in ()).throw(RuntimeError(secret)),
            lambda value: {"leak": secret},
        )
        for redactor in samples:
            with self.subTest(redactor=redactor):
                fleet.redact_sensitive_value = redactor
                reason = todo_fleet._mistral_admission_reason(
                    fleet, "Mistral native admission refused: " + secret,
                    cause=RuntimeError(secret))
                self.assertEqual(reason, "Mistral native admission refused")
                self.assertNotIn(secret, reason)
                self.assertLessEqual(len(reason), 400)
        fleet.redact_sensitive_value = lambda value: value + secret
        bounded = todo_fleet._mistral_admission_reason(
            fleet, "Mistral native admission refused", cause=ValueError("x" * 500))
        self.assertEqual(len(bounded), 400)
        self.assertNotIn(secret, bounded[:len("Mistral native admission refused")])
        fleet.redact_sensitive_value = FakeFleet.redact_sensitive_value.__get__(fleet, FakeFleet)
        redacted = todo_fleet._mistral_admission_reason(
            fleet, "Mistral native admission refused", cause=ValueError(secret))
        self.assertIn("api_key=[REDACTED]", redacted)
        self.assertNotIn(secret, redacted)
        self.assertLessEqual(len(redacted), 400)

        self.write_catalog(["mistral"])
        fleet = self.fleet("mistral")
        fleet.redact_sensitive_value = lambda value: (_ for _ in ()).throw(RuntimeError(secret))
        task = self.task(checks=[["node", "tests/app.test.js"]])
        outdir = self.root / "redactor-failed"
        result = self.execute(self.request(task=task), fleet, outdir)
        self.assertEqual(fleet.execute_calls, [])
        self.assertEqual(result["status"], "BLOCKED")
        reasons = [row["reason"] for row in result["preflight"]]
        self.assertTrue(reasons)
        for reason in reasons:
            self.assertEqual(reason, "Mistral native admission refused")
            self.assertNotIn(secret, reason)

    def test_input_only_script_is_admitted_and_unlisted_script_is_refused(self):
        (self.workspace / "verify.py").write_text("assert True\n", encoding="utf-8")
        admitted = [sys.executable, "-B", "verify.py"]
        fleet = self.fleet("mistral")
        task = self.task(checks=[admitted])
        task["inputs"] = ["verify.py"]
        outdir = self.root / "input-only"
        self.execute(self.request(task=task), fleet, outdir)
        self.assertEqual([call["agent_id"] for call in fleet.execute_calls], ["mistral"])
        dispatch = fleet.execute_calls[0]["mistral_tool_dispatch"]
        self.assertEqual(dispatch["scope"], ["src"])
        self.assertEqual(dispatch["inputs"], ["verify.py"])
        self.assertEqual(dispatch["checks"], [admitted])
        self.assertEqual(fleet.execute_calls[0]["max_model_tool_calls"], 48)

        self.write_catalog(["mistral"])
        refused = self.fleet("mistral")
        bare = self.task(checks=[admitted])
        blocked = self.root / "unlisted-script"
        result = self.execute(self.request(task=bare), refused, blocked)
        self.assertEqual(refused.execute_calls, [])
        self.assertEqual(result["status"], "BLOCKED")
        reasons = [row["reason"] for row in result["preflight"]]
        self.assertTrue(any("Mistral native admission refused" in reason for reason in reasons), reasons)

    def test_parent_traversal_in_a_check_argument_is_refused(self):
        self.write_catalog(["mistral"])
        fleet = self.fleet("mistral")
        task = self.task(checks=[[sys.executable, "-B", "-c", "assert True", "../outside.py"]])
        outdir = self.root / "traversal"
        result = self.execute(self.request(task=task), fleet, outdir)
        self.assertEqual(fleet.execute_calls, [])
        self.assertEqual(result["status"], "BLOCKED")
        reasons = [row["reason"] for row in result["preflight"]]
        self.assertTrue(any("Mistral native admission refused" in reason for reason in reasons), reasons)
        self.assertTrue(any("travers" in reason.lower() for reason in reasons), reasons)

    def test_mixed_python_and_node_checks_refuse_mistral_before_dispatch(self):
        fleet = self.fleet("mistral", "antigravity")
        task = self.task(candidates=[self.candidate("mistral", MISTRAL_MODEL),
                                     self.candidate("antigravity", OTHER_MODEL)],
                         checks=[self.python_assertion(), ["node", "tests/app.test.js"]])
        outdir = self.root / "mixed"
        self.execute(self.request(task=task), fleet, outdir)
        self.assertEqual([call["agent_id"] for call in fleet.execute_calls], ["antigravity"])
        self.assertNotIn("mistral_tool_dispatch", fleet.execute_calls[0])
        reasons = self.refused_reasons(outdir)
        self.assertTrue(any("Mistral native admission refused" in reason for reason in reasons), reasons)
        self.assertTrue(any("declared check" in reason for reason in reasons), reasons)


if __name__ == "__main__":
    unittest.main()
