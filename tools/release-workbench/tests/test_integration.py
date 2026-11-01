"""Offline synthetic integration; none of these tests is native product UAT."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from release_workbench import integration as subject
from release_workbench.exporter import export_candidate


class FixtureBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="workbench-test-", dir=Path(__file__).resolve().parents[1])
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.fixture = self.base / "fixture.md"
        self.fixture.write_text("# Explicit synthetic fixture\nhttps://example.invalid/demo\n", encoding="utf-8")
        self.root = self.base / "demo"
        self.result = subject.offline_demo(self.root, self.fixture)
        self.receipt = subject.load_json(self.root / "receipt-a.json")


class OfflineJourneyTests(FixtureBase):
    def test_actual_two_export_config_scan_rights_bundle_journey(self):
        self.assertEqual(self.result["status"], "PASS")
        self.assertTrue(self.result["two_exports_equal"])
        self.assertEqual(self.receipt, subject.load_json(self.root / "receipt-b.json"))
        self.assertEqual(self.result["configuration"], "PASS")
        self.assertEqual(subject.load_json(self.root / "scan.json")["files_scanned"], 1)
        self.assertEqual(subject.load_json(self.root / "rights.json")["status"], "OPEN")
        self.assertIn("Rights status: `OPEN`", (self.root / "notices.md").read_text())
        self.assertEqual(self.result["native_uat"], "NOT_RUN")
        self.assertFalse(self.result["public_ready"])

    def test_bundle_retains_all_external_and_owner_gates(self):
        report = subject.bundle_check(self.root / "candidate-a", self.receipt)
        self.assertEqual(report["status"], "PENDING")
        self.assertEqual(report["local_status"], "PASS")
        for name in ("independent_security_review", "real_secret_scanner", "provenance_rights",
                     "clean_profile_real_cli_uat"):
            self.assertEqual(report["gates"][name]["status"], "UNKNOWN")
        for name in ("license_owner_decision", "publisher_owner_decision", "public_push_owner_approval"):
            self.assertEqual(report["gates"][name]["status"], "PENDING")

    def test_token_file_never_authorizes_publication(self):
        (self.root / "approval.txt").write_text("APPROVED BY OWNER", encoding="utf-8")
        report = subject.bundle_check(self.root / "candidate-a", self.receipt)
        self.assertFalse(report["public_ready"])
        self.assertEqual(report["gates"]["public_push_owner_approval"]["status"], "PENDING")
        (self.root / "candidate-a" / "approval.txt").write_text("APPROVED", encoding="utf-8")
        report = subject.bundle_check(self.root / "candidate-a", self.receipt)
        self.assertEqual(report["gates"]["export_integrity"]["status"], "FAIL")
        self.assertEqual(report["gates"]["local_privacy_precheck"]["status"], "BLOCKED")

    def test_changed_export_fails_and_original_second_export_recovers(self):
        (self.root / "candidate-a" / "synthetic-source.md").write_text("changed", encoding="utf-8")
        self.assertEqual(subject.bundle_check(self.root / "candidate-a", self.receipt)["status"], "FAIL")
        self.assertEqual(subject.bundle_check(self.root / "candidate-b", self.receipt)["local_status"], "PASS")

    def test_seeded_credential_is_detected_by_real_scan(self):
        content = "access_token = '" + "syntheticLeakValue9836475" + "'\n"
        (self.root / "synthetic-repo" / "synthetic-source.md").write_text(content, encoding="utf-8")
        repo = self.root / "synthetic-repo"
        subject._git(repo, "add", "--", "synthetic-source.md")
        subject._git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                     "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "Synthetic negative")
        manifest = subject.load_json(self.root / "manifest.json")
        manifest["sources"][0]["commit"] = subject._git(repo, "rev-parse", "HEAD")
        manifest["sources"][0]["files"][0]["sha256"] = hashlib.sha256((repo / "synthetic-source.md").read_bytes()).hexdigest()
        receipt = export_candidate(manifest, self.root / "negative", allowed_output_parent=self.root)
        report = subject.bundle_check(self.root / "negative", receipt)
        self.assertEqual(report["gates"]["export_integrity"]["status"], "PASS")
        self.assertEqual(report["gates"]["local_privacy_precheck"]["status"], "FAIL")
        self.assertNotIn("syntheticLeakValue9836475", json.dumps(report))

    def test_output_revision_and_bounded_duplicate_key_inputs(self):
        path = self.base / "report.json"
        subject.write_json(path, {"status": "first"})
        with self.assertRaises(ValueError):
            subject.write_json(path, {"status": "replacement"})
        self.assertEqual(subject.load_json(path)["status"], "first")
        subject.write_json(self.base / "report-v2.json", {"status": "second"})
        bad = self.base / "duplicate.json"
        bad.write_text('{"version":1,"version":2}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            subject.load_json(bad)
        with patch.object(subject, "MAX_JSON_BYTES", 8):
            with self.assertRaises(ValueError):
                subject.load_json(bad)

    def test_existing_demo_is_preserved(self):
        before = (self.root / "manifest.json").read_bytes()
        with self.assertRaises(ValueError):
            subject.offline_demo(self.root, self.fixture)
        self.assertEqual((self.root / "manifest.json").read_bytes(), before)

    def test_sensitive_input_name_refused_before_read(self):
        path = self.base / ".env.synthetic"
        with self.assertRaisesRegex(ValueError, "sensitive"):
            subject.load_json(path)


class NativePreparationTests(FixtureBase):
    """Synthetic exported failure programs test the driver, never real mechanisms."""
    def setUp(self):
        super().setUp()
        repo = self.root / "synthetic-repo"
        names = ("isolation", *subject.NATIVE_STAGES)
        for name in names:
            (repo / f"{name}.py").write_text("# SYNTHETIC failure fixture, not a stack mechanism.\nraise SystemExit(9)\n", encoding="utf-8")
        subject._git(repo, "add", "--", *(f"{n}.py" for n in names))
        subject._git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                     "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "Driver failure fixtures")
        manifest = subject.load_json(self.root / "manifest.json")
        source = manifest["sources"][0]
        source["commit"] = subject._git(repo, "rev-parse", "HEAD")
        source["files"] = [{"path": f"{n}.py", "target": f"{n}.py",
                            "sha256": hashlib.sha256((repo / f"{n}.py").read_bytes()).hexdigest()} for n in names]
        self.native_candidate = self.root / "native-code"
        self.native_receipt = export_candidate(manifest, self.native_candidate, allowed_output_parent=self.root)
        self.workspace = repo
        self.config = {"version": 1, "stack_root": "stack", "state_root": "state", "runtime_root": "runtime",
                       "enabled_clis": [{"agent": "fixture-a", "model": "fixture-model-a"},
                                        {"agent": "fixture-b", "model": "fixture-model-b"}],
                       "external_integrations": [], "network_integrations_enabled": False, "credential_references": {}}
        self.plan = {"version": 1, "tree_hash": self.native_receipt["tree_hash"],
                     "roles": {"maker": {"agent": "fixture-a", "model": "fixture-model-a", "lineage": "lineage-a"},
                               "reviewer": {"agent": "fixture-b", "model": "fixture-model-b", "lineage": "lineage-b"}},
                     "isolation": {"entrypoint": "isolation.py", "args": ["{entrypoint}", "{args_json}", "{workspace}",
                                                                                 "{config}", "{evidence}", "{stage}"]},
                     "stages": [{"id": n, "entrypoint": f"{n}.py", "args": ["--workspace", "{workspace}"],
                                 "role": "reviewer" if n == "fleet" else "maker", "expected": "synthetic-check",
                                 "evidence": f"evidence-{n}.json", "timeout_s": 10} for n in subject.NATIVE_STAGES]}

    def native(self, **changes):
        args = dict(candidate=self.native_candidate, receipt=self.native_receipt, config=self.config,
                    workspace=self.workspace, plan=self.plan)
        args.update(changes)
        return subject.native_uat(**args)

    def test_preparation_executes_no_exported_program(self):
        report = self.native()
        self.assertEqual(report["status"], "PREPARED", report)
        self.assertEqual(report["execution"], "NOT_RUN")
        self.assertFalse((self.workspace / "native-config.json").exists())
        self.assertFalse(report["public_ready"])

    def test_same_lineage_and_same_provider_reviewer_refused(self):
        self.plan["roles"]["reviewer"]["lineage"] = "LINEAGE-A"
        report = self.native(execute=True)
        self.assertEqual(report["stage"], "plan")
        self.assertIn("same-lineage", report["reason"])
        self.assertEqual(report["execution"], "NOT_RUN")

    def test_missing_isolation_does_not_fall_back_to_host(self):
        self.plan["isolation"] = {}
        report = self.native(execute=True)
        self.assertEqual(report["stage"], "isolation")
        self.assertEqual(report["status"], "BLOCKED")
        self.assertEqual(report["execution"], "NOT_RUN")

    def test_workspace_must_be_separate_committed_git_root(self):
        plain = self.root / "not-git"
        plain.mkdir()
        report = self.native(workspace=plain, execute=True)
        self.assertEqual(report["stage"], "workspace")
        self.assertEqual(report["status"], "BLOCKED")
        report = self.native(workspace=self.native_candidate)
        self.assertEqual(report["stage"], "workspace")

    def test_remote_production_and_home_arguments_refused(self):
        for arg in ("https://example.invalid", "--production", "~/.config", "../outside", "--remote-config"):
            with self.subTest(arg=arg):
                self.plan["stages"][0]["args"] = [arg]
                report = self.native(execute=True)
                self.assertEqual(report["status"], "BLOCKED")
                self.assertEqual(report["execution"], "NOT_RUN")

    def test_missing_reviewer_stage_cannot_prepare(self):
        self.plan["stages"][-1]["role"] = "maker"
        self.assertEqual(self.native()["status"], "BLOCKED")

    def test_actual_failed_process_preserves_exact_stage_and_hashes(self):
        report = self.native(execute=True)
        self.assertEqual(report["status"], "BLOCKED")
        self.assertEqual(report["stage"], "office")
        self.assertEqual(report["stages"][0]["process"]["returncode"], 9)
        record = report["stages"][0]
        for key in ("role", "expected", "actual", "environment", "hashes"):
            self.assertIn(key, record)
        self.assertEqual(report["product_acceptance"], "UNKNOWN")
        self.assertTrue((self.workspace / "native-config.json").is_file())
        # No retry/overwrite of earlier native configuration or evidence.
        second = self.native(execute=True)
        self.assertEqual(second["status"], "BLOCKED")
        self.assertEqual(second["execution"], "NOT_RUN")

    def test_timeout_is_not_retried_or_invented_as_success(self):
        original = subject._process
        invocations = []
        def timeout(argv, workspace, **kwargs):
            if argv[0] == "git":
                return original(argv, workspace, **kwargs)
            invocations.append(argv)
            raise ValueError("native process timeout; no automatic retry")
        with patch.object(subject, "_process", side_effect=timeout):
            report = self.native(execute=True)
        self.assertEqual(len(invocations), 1)
        self.assertEqual(report["stage"], "office")
        self.assertEqual(report["status"], "BLOCKED")
        self.assertIn("timeout", report["reason"])

    def test_environment_drops_ambient_provider_and_business_state(self):
        with patch.dict(os.environ, {"PROVIDER_API_KEY": "synthetic", "OFFICE_REMOTE_CONFIG": "private",
                                     "KNOWLEDGE_CONFIG": "private", "GIT_CONFIG_COUNT": "1"}):
            env = subject._environment(self.workspace)
        for key in ("PROVIDER_API_KEY", "OFFICE_REMOTE_CONFIG", "KNOWLEDGE_CONFIG", "GIT_CONFIG_COUNT"):
            self.assertNotIn(key, env)
        self.assertEqual(env["HOME"], str(self.workspace))
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], os.devnull)

    def test_subprocess_is_windowless_and_never_shell(self):
        with patch.object(subject.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            subject._process(["fixture"], self.workspace)
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertEqual(run.call_args.kwargs["creationflags"],
                         subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)

    def test_transcript_without_enforcement_never_validates(self):
        stage = self.plan["stages"][0]
        record = {"hashes": {}, "environment": {"workspace_git_head": "a" * 40}}
        with self.assertRaisesRegex(ValueError, "protocol"):
            subject._check_native_evidence({"status": "PASS", "transcript": "synthetic"}, stage,
                                           self.plan["roles"]["maker"], record, self.workspace)


class InputContractTests(unittest.TestCase):
    """No-filesystem regressions can also run in a write-restricted sandbox."""
    def test_native_arguments_reject_implicit_or_remote_state(self):
        for value in ("../outside", "https://example.invalid", "~/.config", "%APPDATA%", "--production"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                subject._arguments([value], {})
        self.assertEqual(subject._arguments(["--workspace", "{workspace}"], {"{workspace}": "/explicit/owned"}),
                         ["--workspace", "/explicit/owned"])

    def test_sensitive_path_rejected_without_file_access(self):
        with self.assertRaisesRegex(ValueError, "sensitive"):
            subject._safe_path(Path.cwd() / "auth-synthetic.json")
        with self.assertRaises(ValueError):
            subject._relative(".GiT/index")
        with self.assertRaisesRegex(ValueError, "remote"):
            subject._safe_path("//example.invalid/share/manifest.json")

    def test_environment_has_no_ambient_account_or_business_config(self):
        with patch.dict(os.environ, {"PROVIDER_API_KEY": "synthetic", "KNOWLEDGE_CONFIG": "private"}):
            result = subject._environment(Path("explicit-owned"))
        self.assertNotIn("PROVIDER_API_KEY", result)
        self.assertNotIn("KNOWLEDGE_CONFIG", result)
        self.assertEqual(result["USERPROFILE"], "explicit-owned")

    def test_blocked_auth_evidence_records_no_invented_run_id(self):
        # Protocol validation only, not an actual native execution or UAT claim.
        stage = {"id": "office", "expected": "isolated-roundtrip", "role": "maker", "evidence": "evidence.json"}
        role = {"agent": "fixture-a", "model": "fixture-model", "lineage": "fixture-lineage"}
        record = {"hashes": {}, "environment": {"workspace_git_head": "a" * 40}}
        evidence = {"version": 1, "stage": "office", "status": "BLOCKED", "role": "maker",
                    "expected": stage["expected"], "actual": {**role, "run_id": None, "result": "native-auth-unavailable"},
                    "environment": {"workspace_git_head": "a" * 40, "private_business_state": "NOT_ESTABLISHED",
                                    "production_endpoints": "NOT_ESTABLISHED", "native_auth": "UNAVAILABLE"},
                    "hashes": {}, "checks": [], "repair": {"status": "NOT_RUN", "checks": []}, "enforcement": None}
        subject._check_native_evidence(evidence, stage, role, record, Path("explicit-owned"))
        self.assertEqual(record["environment"]["native_auth"], "UNAVAILABLE")
        evidence["status"] = "PASS"
        with self.assertRaises(ValueError):
            subject._check_native_evidence(evidence, stage, role, record, Path("explicit-owned"))


if __name__ == "__main__":
    unittest.main()
