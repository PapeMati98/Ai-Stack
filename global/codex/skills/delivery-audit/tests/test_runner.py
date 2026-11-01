"""Injected process/sink tests. Synthetic receipts NEVER claim native execution.

All retained fixtures/logs live beneath TEAM_TEST_ROOT or this test's local artifact root.
No test launches a native model, Office, Helpdesk, service or cleanup command.
"""
from __future__ import annotations

from contextlib import contextmanager, redirect_stdout
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock
import uuid
import xml.etree.ElementTree as ET

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import audit
import audit_graph as graph
import audit_core as core

TEST_ROOT = Path(os.environ.get("TEAM_TEST_ROOT", str(Path(__file__).resolve().parent / '.test-artifacts')))
STATE_TEST_ROOT = Path(os.environ.get("AUDIT_TEST_STATE_ROOT", str(TEST_ROOT / 'state')))
CANDIDATES = [{"agent": "alpha", "model": "audit-model-alpha", "score": 85, "reason": "Metadata advisory"},
              {"agent": "beta", "model": "audit-model-beta", "score": 85, "reason": "Independent metadata review"}]
PINS = {"fleet": "present", "graph": "present", "hidden_launch": "present"}


def packet(task="task-1", workspace="a", provider="alpha"):
    return {"schema_version": 1, "task_id": task, "session_id": "owner-session", "project_slug": "test-audit",
            "harness": "codex", "task_kind": "delivery", "task_family": "implementation", "outcome": "COMPLETED",
            "workspace_id": hashlib.sha256(workspace.encode()).hexdigest(),
            "requirements": [{"id": "delivery-check", "status": "pass", "evidence_refs": ["ev-1"]}],
            "runs": [{"run_id": "delivery-" + task, "actor_id": "child-" + task, "model_id": "delivery-model",
                      "provider": provider, "tier": "W", "role": "maker", "status": "completed", "verified": True,
                      "input_tokens": None, "cached_input_tokens": None, "output_tokens": None, "tool_calls": None}],
            "metrics": {"wall_seconds": None, "repair_rounds": None, "duplicate_work": False,
                        "uat_required": False, "uat_status": "not-applicable", "evidence_complete": True},
            "delegation": {"parent_tier": "M", "eligible": True, "exception": "none", "planned_tasks": 1},
            "observations": [{"code": "schema-mismatch", "component": "fleet", "evidence_refs": ["ev-1"]}],
            "evidence_refs": ["ev-1"]}


@contextmanager
def retained(name):
    root = STATE_TEST_ROOT / ("runner-" + name + "-" + uuid.uuid4().hex)
    root.mkdir(parents=True)
    yield root  # Retain everything; no TemporaryDirectory or unlink/rmtree.


def config(root, **overrides):
    value = {"enabled": True, "state": "RUNNING", "test_only": True,
             "office_enabled": False, "rsi_enabled": False}
    value.update(overrides)
    graph.write_json(root / audit.CONFIG_FILENAME, value)
    return audit.RunnerConfig.load(root)


def call(argv, runtime=None):
    output = io.StringIO()
    with redirect_stdout(output):
        code = audit.main(argv, runtime=runtime)
    values = [json.loads(line) for line in output.getvalue().splitlines() if line.strip()]
    return code, values


def fake_git(argv, **kwargs):
    if argv[0] != "git":
        raise AssertionError("Unexpected effect in fixture")
    workspace = Path(kwargs["cwd"])
    git_dir = workspace / ".git"
    if "init" in argv:
        git_dir.mkdir(exist_ok=True)
    if "--detach" in argv:
        (git_dir / "HEAD").write_text("d" * 40, encoding="ascii")
    return subprocess.CompletedProcess(argv, 0, b"", b"")


def prepare(root, p, priors=()):
    with mock.patch.object(graph, "_candidates", return_value=CANDIDATES):
        base, workspace, run_dir, plan = graph._prepare(root, core.validate_packet(p), list(priors), fake_git)
    graph.write_json(base / "invocation.json", {"schema_version": 1, "audit_id": core.audit_id(p),
                     "status": "LAUNCH_INTENT", "attempt": 1, "verification_class": "TEST_ONLY"})
    return base, workspace, run_dir, plan


def native_marker(artifact, *, node=None, identifier=None, mask="7", vector=None):
    codes = {"systemic": "S", "task-specific": "T", "needs-evidence": "U", "transient": "X"}
    if vector is None:
        vector = "".join(codes[finding["classification"]] for finding in artifact["findings"])
    return "|".join(("AUDIT-META-V1", node or artifact["node_id"],
                     identifier or artifact["input_sha256"], mask, vector))


def seed_graph(base, workspace, run_dir, plan, p, *, downgrade=None, provider_same=False):
    """Synthetic installed-result shape for validation, never NATIVE_CLI proof."""
    nodes = {}
    for task in plan["tasks"]:
        node = task["id"]
        tests = []
        for index in range(len(task["checks"])):
            folder = run_dir / node / ("precheck" if node == "history-scout" else "1") / ("check-" + str(index))
            folder.mkdir(parents=True, exist_ok=True)
            stdout = folder / "stdout.txt"
            stdout.write_text('{"status":"PASS"}', encoding="ascii")
            test = {"exit_code": 0, "seconds": 0.1, "stdout": str(stdout)}
            graph.write_json(folder / "finished.json", test)
            tests.append(test)
        if node == "history-scout":
            graph.write_json(run_dir / node / "context/historical-input.json", {"model_required": False, "status": "EMPTY"})
            result = {"status": "ACCEPTED", "acceptance": "deterministic-precheck", "model_calls": 0,
                      "tests": tests, "history": [], "artifact": graph._snapshot(workspace, task["scope"])}
        else:
            candidate = graph._expected(workspace, node)
            if downgrade and node == "recurrence-audit":
                for finding in candidate["findings"]:
                    if finding["classification"] == "systemic":
                        finding["classification"] = downgrade
            results = {}
            for role, agent, model, provider in (("maker", "alpha", "audit-model-alpha", "alpha-provider"),
                                                 ("reviewer", "beta", "audit-model-beta", "alpha-provider" if provider_same else "beta-provider")):
                usage_path = run_dir / node / "1" / role / "fleet/usage.json"
                run_id = "run-" + node + "-" + role
                usage = {"run_id": run_id, "agent": agent, "model": model, "model_provider_id": provider,
                         "run_status": "COMPLETED", "exit_code": 0, "timed_out": False, "mode": "advisory" if role == "maker" else "review",
                         "workspace": str(workspace), "model_tier": "K", "handoff_valid": True,
                         "model_identity": {"status": "verified", "expected_model": model, "observed_models": [model]},
                         "runtime_attestation": {"status": "VERIFIED", "payload_fingerprint": graph.FLEET_FINGERPRINT_SHA,
                                                 "runtime_root": str(graph.FLEET_INSTALL)},
                         "token_usage": {"input_tokens": 14, "cached_input_tokens": 2, "output_tokens": 4},
                         "model_tool_calls": None}
                graph.write_json(usage_path, usage)
                native = {"status": "CANDIDATE" if role == "maker" else "REVIEWED", "agent": agent, "model": model,
                          "lineage": "DECLARATION-NOT-PROOF", "run_id": run_id, "usage_record": str(usage_path)}
                if role == "maker":
                    native.update(handoff={"status": "completed", "verification": [native_marker(candidate)], "summary": "Untrusted prose ignored"},
                                  handoff_evidence={"status": "available", "missing_fields": [], "truncated_fields": []})
                else:
                    native.update(verdict="ACCEPT", findings=[], checked=task["done_when"], summary="Untrusted prose ignored")
                graph.write_json(run_dir / node / "1" / role / "result.json", native)
                results[role] = native
            graph.project_native_handoff(workspace, run_dir, node)
            artifact = graph._snapshot(workspace, task["scope"])
            result = {"status": "ACCEPTED", "artifact": artifact,
                      "history": [{"role": "maker", "result": results["maker"]},
                                  {"role": "reviewer", "result": results["reviewer"], "tests": tests, "artifact": artifact}]}
        graph.write_json(run_dir / node / "result.json", result)
        nodes[node] = result
    state = {"status": "ACCEPTED_CANDIDATE", "production_deployed": False, "plan_hash": graph.graph_digest(plan), "nodes": nodes}
    graph.write_json(run_dir / "state.json", state)
    return state


def canonical_receipt(workspace, run_dir, ledger, *, role="maker"):
    """Installed bridge shape in an injected TEST_ONLY ledger, never live Fleet."""
    role_dir = run_dir / "execution-audit" / "1" / role
    usage = graph.read_json(role_dir / "fleet/usage.json")
    result = graph.read_json(role_dir / "result.json")
    run_id = "run-20261014T131312Z-" + ("0123456789" if role == "maker" else "9876543210")
    usage_path = ledger / (run_id + ".json")
    output_dir = role_dir / "fleet" / run_id
    output_dir.mkdir(parents=True)
    usage.update(run_id=run_id, output_dir=str(output_dir), execution_workspace=str(workspace),
                 handoff=copy.deepcopy(result.get("handoff", {})))
    result.update(run_id=run_id, usage_record=str(usage_path))
    fleet_result = {**usage, "status": "COMPLETED", "usage_record": str(usage_path)}
    graph.write_json(usage_path, usage)
    graph.write_json(role_dir / "fleet-result.json", fleet_result)
    graph.write_json(role_dir / "result.json", result)
    return usage_path, usage, result, role_dir, fleet_result


class NoLiveEffects(unittest.TestCase):
    def setUp(self):
        self.live_run = mock.patch.object(subprocess, "run", side_effect=AssertionError("Live subprocess forbidden in candidate test"))
        self.live_popen = mock.patch.object(subprocess, "Popen", side_effect=AssertionError("Live model/transport forbidden in candidate test"))
        self.live_run.start()
        self.live_popen.start()
        self.addCleanup(self.live_run.stop)
        self.addCleanup(self.live_popen.stop)


class Gates(NoLiveEffects):
    def test_graph_downgrade_or_missing_recovery_capability_is_not_present(self):
        with retained("graph-capabilities") as root:
            script = root / "todo_graph.py"
            current = ('VERSION = "1.0.1"\n'
                       'def inspect_run(): pass\n'
                       'def resolve_interrupted(): pass\n'
                       'CAPABILITIES = ["inspect", "resolve-interrupted", "historical_scout", "deterministic-precheck"]\n')
            script.write_text(current, encoding="ascii")
            expected_sha = graph.sha_file(script)
            with mock.patch.multiple(graph, GRAPH_PATH=script, GRAPH_SHA256=expected_sha, create=True):
                self.assertEqual(graph.verify_runtime_pins()["graph"], "present")
                script.write_text('# downgraded Graph\n', encoding="ascii")
                self.assertNotEqual(graph.verify_runtime_pins()["graph"], "present")
                with mock.patch.object(graph, "GRAPH_SHA256", graph.sha_file(script), create=True):
                    self.assertNotEqual(graph.verify_runtime_pins()["graph"], "present")

    def test_public_commands_parse_without_effects(self):
        for command in ("validate", "enqueue", "run-one", "status", "export-confirmed"):
            with self.subTest(command=command), redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as raised:
                audit.main([command, "--help"])
            self.assertEqual(raised.exception.code, 0)

    def test_default_stop_env_and_legacy_bypass_do_not_launch(self):
        with retained("stop") as root:
            graph.write_json(root / "packet.json", packet())
            with mock.patch.dict(os.environ, {"AUDIT_STATE": "RUNNING", "AUDIT_SKIP_PIN_CHECK": "1"}), mock.patch.object(audit, "verify_runtime_pins") as pins:
                code, result = call(["enqueue", "--packet", str(root / "packet.json"), "--state-dir", str(root)])
            self.assertEqual(code, 3)
            pins.assert_not_called()
            self.assertFalse((root / "state").exists())
            with redirect_stdout(io.StringIO()), redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                audit.main(["run-one", "--state-dir", str(root), "--allow-stop"])

    def test_disabled_unreadable_and_stop_file_block_models(self):
        with retained("config") as root:
            for options in ({"enabled": False}, {"state": "STOP"}):
                config(root, **options)
                code, _ = call(["run-one", "--state-dir", str(root)])
                self.assertEqual(code, 3)
            (root / audit.CONFIG_FILENAME).write_text("{broken", encoding="ascii")
            self.assertEqual(call(["run-one", "--state-dir", str(root)])[0], 3)
            config(root)
            (root / "STOP").write_text("", encoding="ascii")
            self.assertFalse(audit.RunnerConfig.load(root).is_running())

    def test_privacy_duplicate_fields_size_and_paths_refuse_before_pins(self):
        with retained("privacy") as root:
            config(root)
            p = packet()
            p["raw_prompt"] = "private-material-must-not-print"
            graph.write_json(root / "packet.json", p)
            with mock.patch.object(audit, "verify_runtime_pins") as pins:
                code, result = call(["enqueue", "--packet", str(root / "packet.json"), "--state-dir", str(root), "--no-launch"])
            self.assertEqual(code, 3)
            self.assertNotIn("private-material", json.dumps(result))
            pins.assert_not_called()
            self.assertFalse((root / "state").exists())
            with self.assertRaises(ValueError):
                audit._json_input(b'{"schema_version":1,"schema_version":1}')
            with self.assertRaises(ValueError):
                audit._json_input(b" " * 65537)
            for path in ("relative.json", "//server/share", str(root / ".." / "unsafe")):
                with self.assertRaises(ValueError):
                    audit._absolute_path(path)

    def test_no_launch_and_dry_run_require_test_config(self):
        with retained("test-only") as root:
            config(root, test_only=False)
            graph.write_json(root / "packet.json", packet())
            self.assertEqual(call(["enqueue", "--packet", str(root / "packet.json"), "--state-dir", str(root), "--no-launch"])[0], 3)
            self.assertEqual(call(["run-one", "--state-dir", str(root), "--dry-run"])[0], 3)
            config(root)
            self.assertEqual(call(["run-one", "--state-dir", str(root), "--dry-run"])[0], 0)
            self.assertFalse((root / "state").exists())

    def test_audit_of_audit_is_skipped_without_runtime_work(self):
        with retained("skip") as root:
            for kind in ("audit", "audit-review", "subtask", "status", "metadata-only"):
                p = packet()
                p["task_kind"] = kind
                graph.write_json(root / "packet.json", p)
                with mock.patch.object(audit, "verify_runtime_pins") as pins:
                    code, output = call(["enqueue", "--packet", str(root / "packet.json"), "--state-dir", str(root)])
                self.assertEqual((code, output[-1]["status"]), (0, "SKIPPED"))
                pins.assert_not_called()
            self.assertFalse((root / "state").exists())

    def test_pins_recompute_bundle_not_marker(self):
        with retained("pins") as root:
            fleet, hidden, installed_graph = root / "fleet", root / "HiddenLaunch.exe", root / "todo_graph.py"
            (fleet / "agent-fleet/scripts").mkdir(parents=True)
            (fleet / "agent-fleet/scripts/fleet.py").write_text("# synthetic", encoding="ascii")
            rows = [{"path": "agent-fleet/scripts/fleet.py", "size": 11, "sha256": graph.sha_file(fleet / "agent-fleet/scripts/fleet.py")}]
            fingerprint = hashlib.sha256((json.dumps(rows, sort_keys=True, ensure_ascii=True, indent=2) + "\n").encode()).hexdigest()
            graph.write_json(fleet / ".agent-fleet-suite-runtime.json", {"managed_by": "agent-fleet-suite-v2-installer", "package_id": "agent-fleet-suite", "package_version": "0.9.82", "payload_file_count": 1, "payload_fingerprint": fingerprint})
            hidden.write_text("synthetic hidden host", encoding="ascii")
            installed_graph.write_text('VERSION = "1.0.1"\ndef inspect_run(): pass\ndef resolve_interrupted(): pass\nCAPABILITIES = ["inspect", "resolve-interrupted", "historical_scout", "deterministic-precheck"]\n', encoding="ascii")
            with mock.patch.multiple(graph, FLEET_INSTALL=fleet, FLEET_FINGERPRINT_SHA=fingerprint,
                                     HIDDEN_LAUNCH_PATH=hidden, HIDDEN_LAUNCH_SHA=graph.sha_file(hidden),
                                     GRAPH_PATH=installed_graph, GRAPH_SHA256=graph.sha_file(installed_graph), create=True):
                self.assertEqual(graph.verify_runtime_pins(), PINS)
                (fleet / "agent-fleet/scripts/fleet.py").write_text("# drift", encoding="ascii")
                self.assertNotEqual(graph.verify_runtime_pins()["fleet"], "present")

class NativeGraph(NoLiveEffects):
    def test_accepted_history_survives_catalog_interpreter_and_installation_path_drift(self):
        with retained("frozen-history") as root:
            p = packet()
            base, workspace, run_dir, plan = prepare(root, p)
            seed_graph(base, workspace, run_dir, plan, p)
            report, proof = graph.collect_accepted(base, p, "TEST_ONLY")
            graph.write_json(base / "acceptance.json", proof, immutable=True)
            with mock.patch.object(graph, "_excluded_candidates", side_effect=AssertionError("Historical read cannot consult today's catalog")), \
                    mock.patch.object(sys, "executable", str(root / "upgraded-python/python.exe")), \
                    mock.patch.object(graph, "__file__", str(root / "synced-skill/scripts/audit_graph.py")), \
                    mock.patch.object(graph, "GRAPH_PATH", root / "synced-graph/scripts/todo_graph.py"), \
                    mock.patch.object(graph, "FLEET_INSTALL", root / "upgraded-fleet-0.80"), \
                    mock.patch.object(graph, "FLEET_FINGERPRINT_SHA", "0" * 64):
                self.assertEqual(graph._prepare(root, p, [], fake_git), (base, workspace, run_dir, plan))
                self.assertEqual(graph.collect_accepted(base, p, "TEST_ONLY"), (report, proof))
                with self.assertRaises(ValueError):
                    graph.load_accepted(root, {"status": "COMPLETED", "audit_id": core.audit_id(p), "packet": p, "report": report})

    def test_live_acceptance_cannot_reuse_frozen_historical_runtime_pin(self):
        with retained("current-runtime-only") as root:
            p = packet()
            base, workspace, run_dir, plan = prepare(root, p)
            seed_graph(base, workspace, run_dir, plan, p)
            with mock.patch.object(graph, "FLEET_INSTALL", root / "upgraded-fleet-0.80"), \
                    mock.patch.object(graph, "FLEET_FINGERPRINT_SHA", "0" * 64), self.assertRaises(ValueError):
                graph.collect_accepted(base, p, "TEST_ONLY")

    def test_frozen_plan_source_and_receipt_tampering_are_rejected(self):
        with retained("frozen-tampering") as root:
            p = packet()
            base, workspace, run_dir, plan = prepare(root, p)
            state = seed_graph(base, workspace, run_dir, plan, p)
            _, proof = graph.collect_accepted(base, p, "TEST_ONLY")
            graph.write_json(base / "acceptance.json", proof, immutable=True)
            changed = copy.deepcopy(plan)
            for task in changed["tasks"]:
                for role in ("maker", "reviewer"):
                    task["excluded_candidates"][role].append({"agent": "zcode", "model": "new-glm"})
            graph.write_json(base / "todo.json", changed)
            graph.write_json(run_dir / "state.json", {**state, "plan_hash": graph.graph_digest(changed)})
            with self.assertRaises(ValueError):
                graph.collect_accepted(base, p, "TEST_ONLY")
            graph.write_json(base / "todo.json", plan)
            graph.write_json(run_dir / "state.json", state)
            graph.write_json(base / "acceptance.json", {**proof, "controller_sha256": "0" * 64})
            with self.assertRaises(ValueError):
                graph.collect_accepted(base, p, "TEST_ONLY")
            graph.write_json(base / "acceptance.json", proof)
            usage_path = run_dir / "execution-audit/1/maker/fleet/usage.json"
            graph.write_json(usage_path, {**graph.read_json(usage_path), "model_identity": {"status": "unverified"}})
            with self.assertRaises(ValueError):
                graph.collect_accepted(base, p, "TEST_ONLY")

    def test_invocation_intent_without_state_is_explicit_nonretryable_unknown(self):
        with retained("launch-unknown") as root:
            p = packet()
            base, workspace, run_dir, plan = prepare(root, p)
            graph.write_json(base / "invocation.json", {"schema_version": 1, "audit_id": core.audit_id(p),
                             "verification_class": "TEST_ONLY", "status": "LAUNCH_INTENT"})
            process = mock.Mock(side_effect=AssertionError("Unknown launch must not dispatch"))
            with mock.patch.object(graph, "verify_runtime_pins", return_value=PINS), self.assertRaises(ValueError) as raised:
                graph.run_native(p, [], root, process_runner=process, launch_allowed=lambda: True, recover_interrupted=True)
            self.assertEqual(str(raised.exception), "AUDIT_GRAPH_LAUNCH_OUTCOME_UNKNOWN")
            self.assertIs(getattr(raised.exception, "retryable", None), False)
            self.assertEqual(getattr(raised.exception, "recovery", None), "inspect-owned-attempt")
            process.assert_not_called()

    def test_canonical_receipt_projects_real_bridge_shape_without_local_usage_copy(self):
        with retained("canonical-receipt") as root:
            p = packet()
            base, workspace, run_dir, plan = prepare(root, p)
            seed_graph(base, workspace, run_dir, plan, p)
            ledger = root / "injected-fleet-ledger"
            for role in ("maker", "reviewer"):
                usage_path, usage, result, role_dir, _ = canonical_receipt(workspace, run_dir, ledger, role=role)
                with self.subTest(role=role), mock.patch.object(graph, "FLEET_RUNS_ROOT", ledger, create=True):
                    run, proof = graph._run_identity(result, "execution-audit", role, run_dir, workspace, plan["tasks"][1][role])
                    self.assertEqual(run["run_id"], usage["run_id"])
                    self.assertEqual(proof["usage_sha256"], graph.sha_file(usage_path))
                    self.assertEqual(proof["bridge_sha256"], graph.sha_file(role_dir / "fleet-result.json"))
                    self.assertEqual(proof["usage_ref"], "fleet-run-" + usage["run_id"])
                    self.assertNotIn(str(ledger), json.dumps(proof))
                    self.assertEqual(list(Path(usage["output_dir"]).glob("*.json")), [])
                    if role == "maker":
                        graph.project_native_handoff(workspace, run_dir, "execution-audit")
                        self.assertEqual(graph.read_json(workspace / "execution-audit.json"), graph._expected(workspace, "execution-audit"))

    def test_canonical_receipt_refuses_foreign_paths_and_mismatched_owned_bridge(self):
        with retained("canonical-binding") as root:
            p = packet()
            base, workspace, run_dir, plan = prepare(root, p)
            seed_graph(base, workspace, run_dir, plan, p)
            ledger = root / "injected-fleet-ledger"
            usage_path, usage, result, role_dir, fleet_result = canonical_receipt(workspace, run_dir, ledger)
            with mock.patch.object(graph, "FLEET_RUNS_ROOT", ledger, create=True):
                for changes in ({"usage_record": str(root / usage_path.name)},
                                {"usage_record": str(ledger / "other.json")},
                                {"run_id": "unbound-native-id"}):
                    with self.subTest(result=changes), self.assertRaises(ValueError):
                        graph._run_identity({**result, **changes}, "execution-audit", "maker", run_dir, workspace, plan["tasks"][1]["maker"])
                for changes in ({"output_dir": str(run_dir / "recurrence-audit/1/maker/fleet" / usage["run_id"])},
                                {"output_dir": str(role_dir / "fleet/other-run")},
                                {"execution_workspace": str(root / "foreign-workspace")},
                                {"model_provider_id": "unknown"},
                                {"handoff": {"status": "completed", "verification": ["unbound-marker"]}}):
                    graph.write_json(usage_path, {**usage, **changes})
                    with self.subTest(usage=changes), self.assertRaises(ValueError):
                        graph._run_identity(result, "execution-audit", "maker", run_dir, workspace, plan["tasks"][1]["maker"])
                    graph.write_json(usage_path, usage)
                for changes in ({"model_provider_id": "wrong-provider"}, {"run_id": "another-run"},
                                {"status": "PARTIAL"}, {"usage_record": str(root / usage_path.name)}):
                    graph.write_json(role_dir / "fleet-result.json", {**fleet_result, **changes})
                    with self.subTest(bridge=changes), self.assertRaises(ValueError):
                        graph._run_identity(result, "execution-audit", "maker", run_dir, workspace, plan["tasks"][1]["maker"])
                    graph.write_json(role_dir / "fleet-result.json", fleet_result)
                graph.write_json(role_dir / "result.json", {**result, "run_id": "replaced-result"})
                with self.assertRaises(ValueError):
                    graph._run_identity(result, "execution-audit", "maker", run_dir, workspace, plan["tasks"][1]["maker"])

    def test_local_synthetic_receipt_cannot_be_promoted_to_native(self):
        with retained("canonical-only-native") as root:
            p = packet()
            base, workspace, run_dir, plan = prepare(root, p)
            seed_graph(base, workspace, run_dir, plan, p)
            graph.write_json(base / "invocation.json", {"verification_class": "NATIVE_CLI"})
            result = graph.read_json(run_dir / "execution-audit/1/maker/result.json")
            with self.assertRaises(ValueError):
                graph._run_identity(result, "execution-audit", "maker", run_dir, workspace, plan["tasks"][1]["maker"])

    def test_compact_marker_projects_only_explicit_same_or_downgraded_classes(self):
        with retained("marker-decisions") as root:
            p = packet()
            base, workspace, run_dir, plan = prepare(root, p, [packet("task-2", "b", "beta"), packet("task-3", "b", "gamma")])
            expected = graph._expected(workspace, "recurrence-audit")
            self.assertEqual(expected["findings"][0]["classification"], "systemic")
            for code, classification in (("S", "systemic"), ("T", "task-specific"), ("U", "needs-evidence")):
                with self.subTest(code=code):
                    projected = graph._project_marker(native_marker(expected, vector=code), expected)
                    self.assertEqual(projected["findings"][0]["classification"], classification)
                    self.assertEqual(projected["findings"][0]["evidence_refs"], expected["findings"][0]["evidence_refs"])
            self.assertEqual(expected["findings"][0]["classification"], "systemic")
            execution = graph._expected(workspace, "execution-audit")
            with self.assertRaises(ValueError):
                graph._project_marker(native_marker(execution, vector="S"), execution)

    def test_compact_marker_zero_findings_requires_explicit_empty_final_field(self):
        with retained("marker-empty") as root:
            p = packet()
            p["observations"] = []
            base, workspace, run_dir, plan = prepare(root, p)
            expected = graph._expected(workspace, "execution-audit")
            marker = "AUDIT-META-V1|execution-audit|" + core.audit_id(p) + "|7|"
            self.assertEqual(graph._project_marker(marker, expected), expected)
            for wrong in (marker[:-1], marker + "U", marker.replace("|7|", "|6|")):
                with self.subTest(marker=wrong), self.assertRaises(ValueError):
                    graph._project_marker(wrong, expected)

    def test_compact_marker_rejects_hash_node_count_flags_prose_and_private_data(self):
        with retained("marker-invalid") as root:
            base, workspace, run_dir, plan = prepare(root, packet())
            expected = graph._expected(workspace, "execution-audit")
            valid = native_marker(expected)
            malformed = [native_marker(expected, identifier="0" * 64),
                         native_marker(expected, node="recurrence-audit"),
                         native_marker(expected, mask="6"), native_marker(expected, mask="07"),
                         native_marker(expected, mask="0x7"), native_marker(expected, vector=""),
                         native_marker(expected, vector="TT"), native_marker(expected, vector="Z"),
                         native_marker(expected, vector="t"), valid + "|extra", valid + "\n",
                         " " + valid, json.dumps(expected), "Bearer secret-material",
                         valid + "\u0105", "A" * 4097]
            for value in malformed:
                with self.subTest(marker=value[:40]), self.assertRaises(ValueError) as rejected:
                    graph._project_marker(value, expected)
                self.assertNotIn("secret-material", str(rejected.exception))

    def test_compact_marker_enforces_256_checked_flags_bound(self):
        with retained("marker-bounds") as root:
            base, workspace, run_dir, plan = prepare(root, packet())
            expected = graph._expected(workspace, "execution-audit")
            expected["criteria"] = ["criterion-" + str(index) for index in range(256)]
            marker = native_marker(expected, mask="f" * 64)
            self.assertEqual(graph._project_marker(marker, expected), expected)
            self.assertLessEqual(len(marker.encode("ascii")), 4096)
            with self.assertRaises(ValueError):
                graph._project_marker(native_marker(expected, vector="T" * 257), expected)
            expected["criteria"].append("criterion-256")
            with self.assertRaises(ValueError):
                graph._project_marker(marker, expected)

    def test_native_handoff_rejects_duplicate_marker_or_any_other_evidence_item(self):
        with retained("marker-one-item") as root:
            p = packet()
            base, workspace, run_dir, plan = prepare(root, p)
            seed_graph(base, workspace, run_dir, plan, p)
            path = run_dir / "execution-audit/1/maker/result.json"
            original = graph.read_json(path)
            marker = original["handoff"]["verification"][0]
            for evidence in ([marker, marker], [marker, "Bearer private-data"], [marker, "PASS"], [], "marker"):
                altered = copy.deepcopy(original)
                altered["handoff"]["verification"] = evidence
                graph.write_json(path, altered)
                with self.subTest(evidence=evidence), self.assertRaises(ValueError):
                    graph.project_native_handoff(workspace, run_dir, "execution-audit")

    def test_protocol_and_prompt_require_one_marker_and_prefer_worker_makers(self):
        with retained("marker-protocol") as root:
            base, workspace, run_dir, plan = prepare(root, packet())
            protocol = graph.read_json(workspace / "protocol.json")
            self.assertEqual(protocol["verification_items"], 1)
            self.assertEqual(protocol["checked_masks"], {"execution-audit": "7", "recurrence-audit": "7"})
            self.assertEqual(protocol["classification_codes"], {"S": "systemic", "T": "task-specific", "U": "needs-evidence", "X": "transient"})
            for task in plan["tasks"][1:]:
                self.assertIn("No nested JSON", task["text"])
                self.assertIn("You MUST decide", task["text"])
            candidates = graph._candidates()  # Installed profile metadata only; no readiness/model calls.
            self.assertEqual({c["score"] for c in candidates if c["tier"] == "W"}, {100})
            self.assertEqual({c["score"] for c in candidates if c["tier"] == "K"}, {85})

    def test_zcode_catalog_fallback_is_excluded_and_autopsy_remains_enabled(self):
        with retained("excluded") as root:
            candidates = CANDIDATES + [{"agent": "manager", "model": "manager-model", "score": 60,
                                       "reason": "Existing-node autopsy", "tier": "M"}]
            plan = graph._plan(root, candidates)
            for task in plan["tasks"]:
                for role in ("maker", "reviewer"):
                    excluded = {(c["agent"], c["model"]) for c in task["excluded_candidates"][role]}
                    self.assertTrue({("zcode", "GLM-5.3"), ("opencode", "glm-5.3"),
                                     ("opencode", "glm-5.3-flash")} <= excluded)
            self.assertNotIn("manager", [c["agent"] for c in plan["tasks"][1]["maker"]])
            self.assertIn("manager", [c["agent"] for c in plan["tasks"][1]["reviewer"]])
            with mock.patch.dict(os.environ, {"AGENT_GRAPH_AUTOPSY": "0"}):
                self.assertNotEqual(graph.native_env().get("AGENT_GRAPH_AUTOPSY"), "0")
            self.assertEqual(graph.native_env()["AGENT_GRAPH_TIER_ROUTING"], "0")

    def test_interrupted_resume_uses_public_hash_bound_inspect_then_resolve(self):
        with retained("inspect-resolve") as root:
            base, workspace, run_dir, plan = prepare(root, packet())
            graph.write_json(run_dir / "state.json", {"nodes": {"execution-audit": {"status": "RUNNING"}}, "status": "BLOCKED"})
            calls = []
            def effect(argv, **kwargs):
                calls.append(argv)
                if argv[3] == "inspect":
                    return subprocess.CompletedProcess(argv, 0, json.dumps({"plan_hash": graph.graph_digest(plan)}).encode(), b"")
                if argv[3] == "resolve-interrupted":
                    self.assertIn("--inspection", argv)
                    graph.write_json(run_dir / "state.json", {"nodes": {"execution-audit": {"status": "BLOCKED"}}, "status": "BLOCKED"})
                    return subprocess.CompletedProcess(argv, 0, b"{}", b"")
                raise AssertionError("Unexpected recovery operation")
            state = graph._recover_interrupted(base, workspace, run_dir, plan, lambda: True, effect)
            self.assertEqual([argv[3] for argv in calls], ["inspect", "resolve-interrupted"])
            self.assertEqual(state["nodes"]["execution-audit"]["status"], "BLOCKED")

    def test_owned_git_and_fixed_plan_calls_only_owned_safe_commands(self):
        with retained("owned") as root:
            calls = []
            def run(argv, **kwargs):
                calls.append((argv, kwargs))
                return fake_git(argv, **kwargs)
            with mock.patch.object(graph, "_candidates", return_value=CANDIDATES):
                base, workspace, run_dir, plan = graph._prepare(root, packet(), [], run)
            self.assertEqual([t["id"] for t in plan["tasks"]], list(graph.NODES))
            self.assertEqual(plan["tasks"][2]["depends_on"], list(graph.NODES[:2]))
            self.assertTrue(all(t["mode"] == "advisory" and t["checks"] for t in plan["tasks"]))
            self.assertTrue(any("--detach" in argv for argv, _ in calls))
            self.assertTrue(all("core.hooksPath=" + str(workspace / "disabled-hooks") in argv for argv, _ in calls))
            self.assertTrue(all(kwargs["env"]["GIT_CONFIG_GLOBAL"] == os.devnull for _, kwargs in calls))
            self.assertTrue(all(kwargs["creationflags"] == (subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0) for _, kwargs in calls))
            self.assertEqual(graph.read_json(workspace / "history-catalog.json"), [])

    def test_actual_handoff_projection_precedes_independent_review(self):
        with retained("native-projection") as root:
            p = packet()
            base, workspace, run_dir, plan = prepare(root, p)
            seed_graph(base, workspace, run_dir, plan, p)
            report, proof = graph.collect_accepted(base, p, "TEST_ONLY")
            self.assertEqual(proof["verification_class"], "TEST_ONLY")
            self.assertEqual(len(report["model_run_ids"]), 4)
            self.assertNotIn(p["runs"][0]["run_id"], report["model_run_ids"])
            self.assertEqual(report["runs"][0]["provider"], "alpha-provider")
            self.assertEqual(report["runs"][1]["provider"], "beta-provider")
            self.assertNotIn("Untrusted prose", json.dumps((report, proof)))
            with self.assertRaises(ValueError):
                graph.collect_accepted(base, p, "NATIVE_CLI")

    def test_original_task_providers_cannot_qualify_same_provider_audit_review(self):
        with retained("provider") as root:
            p = packet(provider="original-provider-A")
            base, workspace, run_dir, plan = prepare(root, p)
            seed_graph(base, workspace, run_dir, plan, p, provider_same=True)
            with self.assertRaises(ValueError):
                graph.collect_accepted(base, p, "TEST_ONLY")

    def test_receipt_tampering_and_unbound_reference_fail_closed(self):
        with retained("identity") as root:
            p = packet()
            base, workspace, run_dir, plan = prepare(root, p)
            seed_graph(base, workspace, run_dir, plan, p)
            usage_path = run_dir / "execution-audit/1/maker/fleet/usage.json"
            original = graph.read_json(usage_path)
            for changes in ({"model_provider_id": "unknown"}, {"model": "wrong-model"},
                            {"model_identity": {"status": "unverified", "expected_model": "audit-model-alpha"}},
                            {"runtime_attestation": {"status": "VERIFIED", "payload_fingerprint": "0" * 64, "runtime_root": str(graph.FLEET_INSTALL)}}):
                altered = {**original, **changes}
                graph.write_json(usage_path, altered)
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    graph.collect_accepted(base, p, "TEST_ONLY")
                graph.write_json(usage_path, original)
            with self.assertRaises(ValueError):
                graph._under(root / "foreign-usage.json", run_dir)

    def test_missing_checks_verdict_criteria_and_changed_artifact_refuse(self):
        for change in ("checks", "verdict", "criteria", "artifact"):
            with retained("missing-" + change) as root:
                p = packet()
                base, workspace, run_dir, plan = prepare(root, p)
                state = seed_graph(base, workspace, run_dir, plan, p)
                node = state["nodes"]["execution-audit"]
                if change == "artifact":
                    value = graph.read_json(workspace / "execution-audit.json")
                    value["raw_prompt"] = "must-not-route"
                    graph.write_json(workspace / "execution-audit.json", value)
                else:
                    reviewer = node["history"][-1]
                    if change == "checks":
                        reviewer["tests"] = []
                    elif change == "verdict":
                        reviewer["result"]["verdict"] = "REPAIR"
                    else:
                        reviewer["result"]["checked"] = []
                    graph.write_json(run_dir / "execution-audit/result.json", node)
                    graph.write_json(run_dir / "state.json", state)
                with self.subTest(change=change), self.assertRaises(ValueError):
                    graph.collect_accepted(base, p, "TEST_ONLY")

    def test_model_can_downgrade_correlation_without_rsi_confirmation(self):
        with retained("downgrade") as root:
            priors = [packet("task-2", "b", "beta"), packet("task-3", "b", "gamma")]
            p = packet()
            self.assertEqual(core.recurrence_findings(p, priors)[0]["classification"], "systemic")
            base, workspace, run_dir, plan = prepare(root, p, priors)
            seed_graph(base, workspace, run_dir, plan, p, downgrade="needs-evidence")
            report, proof = graph.collect_accepted(base, p, "TEST_ONLY")
            self.assertEqual(report["recurrence_findings"][0]["classification"], "needs-evidence")
            bad = graph._expected(workspace, "execution-audit")
            bad["findings"][0]["classification"] = "systemic"
            with self.assertRaises(ValueError):
                graph._validate_artifact(bad, graph._expected(workspace, "execution-audit"))

    def test_native_run_once_and_accepted_retry_reuses_graph(self):
        with retained("once") as root:
            p = packet()
            calls = []
            def executor(argv, **kwargs):
                calls.append(argv)
                if argv[0] == "git":
                    return fake_git(argv, **kwargs)
                self.assertEqual(argv[2:4], [str(graph.GRAPH_PATH), "run"])
                base = root / "native" / core.audit_id(p)
                plan = graph.read_json(base / "todo.json")
                seed_graph(base, base / "metadata", base / "graph-run", plan, p)
                return subprocess.CompletedProcess(argv, 0, b"untrusted output is not forwarded", b"")
            with mock.patch.object(graph, "verify_runtime_pins", return_value=PINS), mock.patch.object(graph, "_candidates", return_value=CANDIDATES):
                report, proof = graph.run_native(p, [], root, process_runner=executor, launch_allowed=lambda: True)
                repeated = graph.run_native(p, [], root, process_runner=executor, launch_allowed=lambda: True)
            self.assertEqual((report, proof), repeated)
            self.assertEqual(sum(argv[0] != "git" for argv in calls), 1)
            self.assertEqual(proof["verification_class"], "TEST_ONLY")

    def test_stop_or_interrupted_never_blindly_replays(self):
        with retained("interrupted") as root:
            with mock.patch.object(graph, "verify_runtime_pins") as pins:
                with self.assertRaises(ValueError):
                    graph.run_native(packet(), [], root, process_runner=fake_git, launch_allowed=lambda: False)
                pins.assert_not_called()
            base, workspace, run_dir, plan = prepare(root, packet())
            graph.write_json(run_dir / "state.json", {"nodes": {"execution-audit": {"status": "RUNNING"}}, "status": "BLOCKED"})
            process = mock.Mock(side_effect=AssertionError("Interrupted node must not replay"))
            with mock.patch.object(graph, "verify_runtime_pins", return_value=PINS), self.assertRaises(ValueError):
                graph.run_native(packet(), [], root, process_runner=process, launch_allowed=lambda: True)
            process.assert_not_called()

    def test_mid_run_stop_targets_only_own_process_handle(self):
        with retained("watcher") as root:
            process = mock.Mock(pid=4242, returncode=-1)
            process.poll.side_effect = [None, None, -1]
            owned = mock.Mock(creation_ticks=123456)
            with mock.patch.object(subprocess, "Popen", return_value=process), mock.patch.object(graph, "_WindowsOwnedJob", return_value=owned):
                with self.assertRaises(ValueError):
                    graph._invoke_graph(["owned-graph"], root, root, root / "attempt-test.json", lambda: False)
            process.wait.assert_called_once_with(timeout=30)
            if os.name == "nt":
                owned.stop.assert_called_once_with()
                owned.close.assert_called_once_with()
            else:
                process.terminate.assert_called_once()
            self.assertEqual(graph.read_json(root / "attempt-test.json")["status"], "STOPPED")

class QueueAndFeed(NoLiveEffects):
    def test_enqueue_replay_is_one_durable_job_and_no_launch_receipt_is_test_only(self):
        with retained("queue") as root:
            config(root)
            graph.write_json(root / "packet.json", packet())
            argv = ["enqueue", "--packet", str(root / "packet.json"), "--state-dir", str(root), "--no-launch"]
            with mock.patch.object(audit, "verify_runtime_pins", return_value=PINS):
                first, second = call(argv), call(argv)
            self.assertEqual(first[1][-1]["status"], "QUEUED")
            self.assertEqual(second[1][-1]["status"], "DUPLICATE")
            self.assertFalse(first[1][-1]["native_completed"])
            self.assertEqual(len(list((root / "state/records").glob("*.json"))), 1)

    def test_injected_audit_cannot_be_completed_or_exported_confirmed(self):
        with retained("test-ledger") as root:
            config(root)
            store = core.AuditStore(root / "state")
            store.enqueue(packet())
            runtime = audit.Runtime(native_executor=lambda p, history, state_dir, **kwargs: (graph.run(p, history), {"verification_class": "TEST_ONLY"}))
            with mock.patch.object(audit, "verify_runtime_pins", return_value=PINS):
                code, output = call(["run-one", "--state-dir", str(root)], runtime)
            self.assertEqual((code, output[-1]["status"]), (0, "TEST_ONLY"))
            self.assertEqual(store.records()[0]["status"], "BLOCKED")
            self.assertEqual(audit.confirmed_feed(root)["records"], [])

    def test_failure_expires_only_own_claim_and_recovers_on_next_attempt(self):
        with retained("retry") as root:
            config(root)
            store = core.AuditStore(root / "state")
            store.enqueue(packet())
            failure = audit.Runtime(native_executor=mock.Mock(side_effect=ValueError("private exception must not print")))
            with mock.patch.object(audit, "verify_runtime_pins", return_value=PINS):
                code, output = call(["run-one", "--state-dir", str(root)], failure)
            self.assertEqual(code, 3)
            self.assertNotIn("private exception", json.dumps(output))
            deferred = audit._read_records(root, finished_only=False)[0]
            deadline = deferred["claim"]["expires_at"]
            self.assertGreater(deadline, core.time.time())
            self.assertIsNone(store.claim("before-backoff-worker"))
            foreign = {**deferred, "claim_token": "f" * 64}
            self.assertIsNone(audit._release_retryable(store, foreign))
            self.assertEqual(audit._read_records(root, finished_only=False)[0], deferred)
            with mock.patch.object(core.time, "time", return_value=deadline + 1):
                job = store.claim("new-worker")
            self.assertIsNotNone(job)
            self.assertEqual(job["status"], "RUNNING")
            self.assertNotEqual(job["claim_token"], deferred["claim"]["claim_token"])

    def test_background_drain_processes_enqueue_during_active_audit(self):
        with retained("drain") as root:
            config(root)
            store = core.AuditStore(root / "state")
            store.enqueue(packet())
            processed = []
            def executor(p, prior, state, **kwargs):
                processed.append(p["task_id"])
                if len(processed) == 1:
                    store.enqueue(packet("task-2"))
                return graph.run(p, prior), {"verification_class": "TEST_ONLY"}
            with mock.patch.object(audit, "verify_runtime_pins", return_value=PINS):
                code, _ = call(["run-one", "--state-dir", str(root), "--drain"], audit.Runtime(native_executor=executor))
            self.assertEqual(code, 0)
            self.assertEqual(processed, ["task-1", "task-2"])
            self.assertFalse(any(r["status"] == "QUEUED" for r in audit._read_records(root, finished_only=False)))

    def test_stop_during_drain_preserves_second_queued_job(self):
        with retained("drain-stop") as root:
            config(root)
            store = core.AuditStore(root / "state")
            store.enqueue(packet())
            store.enqueue(packet("task-2"))
            processed = []
            def executor(p, prior, state, **kwargs):
                processed.append(p["task_id"])
                config(root, state="STOP")
                return graph.run(p, prior), {"verification_class": "TEST_ONLY"}
            with mock.patch.object(audit, "verify_runtime_pins", return_value=PINS):
                code, _ = call(["run-one", "--state-dir", str(root), "--drain"], audit.Runtime(native_executor=executor))
            self.assertEqual((code, len(processed)), (3, 1))
            self.assertEqual(sum(r["status"] == "QUEUED" for r in audit._read_records(root, finished_only=False)), 1)

    def test_export_ignores_core_only_unknown_and_test_reports_and_writes_no_state(self):
        with retained("feed") as root:
            config(root)
            store = core.AuditStore(root / "state")
            for index in range(3):
                p = packet("task-" + str(index), "a" if index == 0 else "b", "alpha" if index == 0 else "beta")
                queued = store.enqueue(p)
                job = store.claim("fixture-owner")
                report = graph.run(p)  # Original delivery providers prove no native audit.
                store.finish(queued["audit_id"], job["claim_token"], "COMPLETED", report)
            before = {p.relative_to(root / "state").as_posix(): graph.sha_file(p) for p in (root / "state").rglob("*") if p.is_file()}
            code, _ = call(["export-confirmed", "--state-dir", str(root), "--output", str(root / "feed.json")])
            self.assertEqual(code, 0)
            self.assertEqual(graph.read_json(root / "feed.json")["records"], [])
            call(["status", "--state-dir", str(root)])
            after = {p.relative_to(root / "state").as_posix(): graph.sha_file(p) for p in (root / "state").rglob("*") if p.is_file()}
            self.assertEqual(before, after)
            self.assertNotIn("state_dir", graph.read_json(root / "feed.json"))

    @unittest.skipUnless(os.name == "nt", "Windows WMI launch")
    def test_background_action_xml_and_wmi_detach_are_actual_code_path(self):
        with retained("wmi") as root:
            config(root, test_only=False)
            argv = audit.build_launch_argv(root, "a" * 64)
            xml = ET.parse(argv[1]).getroot()
            self.assertEqual(xml.attrib["isolate-desktop"], "true")
            self.assertIn("--drain", xml.find("action").attrib["arguments"])
            calls = []
            def effect(argv, **kwargs):
                calls.append((argv, kwargs))
                return subprocess.CompletedProcess(argv, 0, b'{"ReturnValue":0,"ProcessId":8123}', b"")
            with mock.patch.object(graph, "sha_file", return_value=graph.HIDDEN_LAUNCH_SHA):
                result = audit.detached_launch(argv, runtime=audit.Runtime(process_runner=effect))
            self.assertEqual(result["mechanism"], "WMI-HiddenLaunch-private-desktop")
            self.assertIn("Invoke-CimMethod", calls[0][0][-1])
            self.assertEqual(calls[0][1]["creationflags"], subprocess.CREATE_NO_WINDOW)
            self.assertNotIn("Win32_Service", calls[0][0][-1])


class Delivery(NoLiveEffects):
    def fixture(self, root):
        p = packet()
        report = graph.run(p)
        report["model_run_ids"] = ["native-maker", "native-reviewer"]
        finding = core.recurrence_findings(p, [packet("task-2", "b", "beta"), packet("task-3", "b", "gamma")])[0]
        report["recurrence_findings"] = [finding]
        identifier = core.audit_id(p)
        record = {"audit_id": identifier, "packet": p, "report": report, "status": "COMPLETED", "native_accepted": True}
        sink = {"sink": "rsi", "dedupe_key": "a" * 64, "audit_id": identifier, "task_id": p["task_id"], "pattern_id": finding["pattern_id"], "status": "PENDING"}
        record["outbox"] = [sink]
        delivery = {"schema_version": 1, "dedupe_key": sink["dedupe_key"], "audit_id": identifier,
                    "sink": "rsi", "status": "PENDING", "attempts": 1, "receipt_ref": None}
        return record, sink, delivery, finding

    def test_office_uses_safe_stdin_native_dedupe_and_technical_identity(self):
        with retained("office") as root:
            record, sink, _, _ = self.fixture(root)
            sink["sink"] = "office"
            calls = []
            def effect(argv, **kwargs):
                calls.append((argv, kwargs))
                return subprocess.CompletedProcess(argv, 0, b'{"message":{"id":"office-opaque-receipt"}}', b"")
            result = audit.publish_office(sink, record=record, state_dir=root, runtime=audit.Runtime(process_runner=effect))
            self.assertEqual(result["status"], "SENT")
            argv, kwargs = calls[0]
            self.assertIn("--body-stdin", argv)
            self.assertEqual(argv[argv.index("--harness") + 1], "agent-graph")
            self.assertEqual(argv[argv.index("--session-id") + 1], "job-" + record["audit_id"][:48])
            self.assertIn(sink["dedupe_key"], argv)
            body = json.loads(kwargs["input"])
            self.assertEqual(set(body), {"schema_version", "audit_id", "task_id", "classification",
                                         "execution_findings", "recurrence_findings", "model_run_ids",
                                         "release_authority"})
            self.assertEqual(body["audit_id"], record["audit_id"])
            self.assertEqual(body["task_id"], record["packet"]["task_id"])
            self.assertEqual(body["classification"], "accepted-audit")
            self.assertEqual(body["execution_findings"], record["report"]["execution_findings"])
            self.assertEqual(body["recurrence_findings"], record["report"]["recurrence_findings"])
            self.assertEqual(body["model_run_ids"], record["report"]["model_run_ids"])
            self.assertFalse(body["release_authority"])
            self.assertNotIn("owner-session", json.dumps(body))
            self.assertNotIn("summary", body)

    def test_rsi_known_closed_or_any_existing_ticket_is_never_refiled(self):
        with retained("rsi-closed") as root:
            record, sink, delivery, finding = self.fixture(root)
            for status in ("resolved", "wontfix", "open"):
                effect = mock.Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps({"signature": audit._rsi_signature(finding), "status": status, "evidence": []}).encode(), b""))
                result = audit.file_rsi_ticket(sink, record=record, delivery=delivery, state_dir=root, runtime=audit.Runtime(process_runner=effect))
                self.assertEqual(result["status"], "SUPPRESSED_EXISTING")
                self.assertEqual(effect.call_count, 1)
                self.assertEqual(effect.call_args.args[0][3], "show")

    def test_rsi_new_once_then_unknown_reconcile_never_replays_file(self):
        with retained("rsi-unknown") as root:
            record, sink, delivery, finding = self.fixture(root)
            calls = []
            def effect(argv, **kwargs):
                calls.append(argv)
                command = argv[3]
                if command == "show":
                    return subprocess.CompletedProcess(argv, 1, b"", b"absent")
                if command == "list":
                    return subprocess.CompletedProcess(argv, 0, b"[]", b"")
                if command == "file":
                    raise OSError("Injected outcome unknown")
                raise AssertionError("Unexpected helpdesk operation")
            runtime = audit.Runtime(process_runner=effect)
            with self.assertRaises(OSError):
                audit.file_rsi_ticket(sink, record=record, delivery=delivery, state_dir=root, runtime=runtime)
            self.assertEqual(delivery["status"], "CALLING")
            result = audit.file_rsi_ticket(sink, record=record, delivery=delivery, state_dir=root, runtime=runtime)
            self.assertEqual(result["status"], "UNKNOWN")
            self.assertEqual(sum(argv[3] == "file" for argv in calls), 1)
            self.assertIn("filing-" + sink["dedupe_key"], next(argv for argv in calls if argv[3] == "file"))

    def test_rsi_uncertain_filing_receipt_is_reconciled_by_evidence_marker(self):
        with retained("rsi-reconciled") as root:
            record, sink, delivery, finding = self.fixture(root)
            delivery["status"] = "UNKNOWN"
            effect = mock.Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps({"signature": audit._rsi_signature(finding), "status": "resolved", "evidence": ["filing-" + sink["dedupe_key"]]}).encode(), b""))
            result = audit.file_rsi_ticket(sink, record=record, delivery=delivery, state_dir=root, runtime=audit.Runtime(process_runner=effect))
            self.assertEqual(result["status"], "SENT")
            self.assertEqual(effect.call_count, 1)

    def test_office_outage_retains_retry_then_sent_is_never_ack(self):
        with retained("outbox") as root:
            cfg = config(root, test_only=False, office_enabled=True)
            record, sink, _, _ = self.fixture(root)
            sink.update(sink="office", pattern_id=None)
            effect = mock.Mock(side_effect=[OSError("transport down"), {"status": "SENT", "receipt_ref": "office-receipt"}])
            with mock.patch.object(audit, "_accepted_records", return_value=[record]):
                first = audit.dispatch_outbox(root, cfg, audit.Runtime(office_sink=effect))
                second = audit.dispatch_outbox(root, cfg, audit.Runtime(office_sink=effect))
                third = audit.dispatch_outbox(root, cfg, audit.Runtime(office_sink=effect))
            self.assertEqual(first[0]["status"], "PENDING")
            self.assertEqual(second[0]["status"], "SENT")
            self.assertEqual(third, [])
            self.assertEqual(effect.call_count, 2)
            self.assertEqual(graph.read_json(root / "delivery" / (sink["dedupe_key"] + ".json"))["status"], "SENT")


if __name__ == "__main__":
    unittest.main()
