from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).with_name("install_router_suite.py")
SPEC = importlib.util.spec_from_file_location("install_router_suite", SCRIPT)
assert SPEC and SPEC.loader
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class RouterSuiteInstallerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source_package_root = Path(__file__).resolve().parents[1]
        cls.source_lock = (cls.source_package_root / installer.PACKAGE_LOCK_NAME).read_bytes()
        cls.package_temporary = tempfile.TemporaryDirectory()
        cls.package_root = Path(cls.package_temporary.name) / "package"
        shutil.copytree(cls.source_package_root, cls.package_root)
        manifest_path = cls.package_root / "package.json"
        manifest = json.loads(manifest_path.read_text(encoding="ascii"))
        manifest["release_state"] = "released"
        installer.atomic_json(manifest_path, manifest)
        installer.seal_package(cls.package_root)
        _, cls.lock = installer.load_verified_package(cls.package_root)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.package_temporary.cleanup()

    def test_suite_setup_does_not_reseal_source_package(self) -> None:
        self.assertEqual(
            (self.source_package_root / installer.PACKAGE_LOCK_NAME).read_bytes(),
            self.source_lock,
        )

    def test_payload_cli_version_matches_release_manifest(self) -> None:
        manifest = json.loads(
            (self.source_package_root / "package.json").read_text(encoding="utf-8")
        )
        fleet_cli = (
            self.source_package_root / "payload" / "agent-fleet" / "scripts" / "fleet.py"
        )
        result = subprocess.run(
            [sys.executable, "-B", str(fleet_cli), "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"agent-fleet {manifest['package_version']}")

    def test_runtime_only_deploy_preserves_active_runtime_and_skills(self) -> None:
        temporary, home, appdata, state = self.fixture()
        self.addCleanup(temporary.cleanup)
        active=home / '.agents/runtime/agent-fleet-suite/lean-1'
        active.mkdir(parents=True)
        sentinel=active / 'active-task.txt'
        sentinel.write_text('running-0.9.2',encoding='utf-8')
        self.write_old_skill(home / '.agents/skills','agent-cli')
        tools=installer.parse_tools('runtime-only')
        self.assertEqual(tools,[])
        result=installer.deploy(package_root=self.package_root,home=home,appdata=appdata,tools=tools,state_root=state,adopt_existing=False,expected_package_fingerprint=self.lock['package_fingerprint'])
        self.assertEqual(result['entry_count'],1)
        self.assertEqual(sentinel.read_text(encoding='utf-8'),'running-0.9.2')
        self.assertEqual((home / '.agents/skills/agent-cli/SKILL.md').read_text(encoding='ascii'),'# old agent-cli\n')
        receipt=Path(result['receipt_path'])
        self.assertEqual(self.verify_receipt(receipt,state,home,appdata)['status'],'PASS')
        self.rollback(receipt,state,home,appdata)
        self.assertTrue(sentinel.is_file())

    def fixture(self) -> tuple[tempfile.TemporaryDirectory[str], Path, Path, Path]:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        home = root / "home"
        appdata = root / "appdata"
        state = root / "state"
        home.mkdir()
        appdata.mkdir()
        return temporary, home, appdata, state

    def write_old_skill(
        self, root: Path, skill: str, *, managed_legacy: bool = False
    ) -> None:
        target = root / skill
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text(f"# old {skill}\n", encoding="ascii")
        if managed_legacy:
            installer.atomic_json(
                target / installer.LEGACY_MARKER,
                {
                    "managed_by": "router-v2-transactional-deployer",
                    "transaction_id": "old-transaction",
                },
            )

    def verify_receipt(
        self, receipt: Path, state: Path, home: Path, appdata: Path
    ) -> dict:
        return installer.verify_receipt(
            receipt,
            state,
            package_root=self.package_root,
            home=home,
            appdata=appdata,
        )

    def rollback(
        self, receipt: Path, state: Path, home: Path, appdata: Path
    ) -> dict:
        return installer.rollback(
            receipt,
            state,
            package_root=self.package_root,
            home=home,
            appdata=appdata,
        )

    def recover_deploy(
        self, receipt: Path, state: Path, home: Path, appdata: Path
    ) -> dict:
        return installer.recover_deploy(
            receipt,
            state,
            package_root=self.package_root,
            home=home,
            appdata=appdata,
        )

    def test_plan_is_zero_write_and_unmanaged_collision_requires_adoption(self) -> None:
        temporary, home, appdata, state = self.fixture()
        self.addCleanup(temporary.cleanup)
        self.write_old_skill(home / ".claude" / "skills", "agent-fleet")
        plan = installer.inspect_plan(
            package_root=self.package_root,
            home=home,
            appdata=appdata,
            tools=["claude"],
            adopt_existing=False,
            expected_package_fingerprint=self.lock["package_fingerprint"],
        )
        self.assertEqual(plan["status"], "BLOCKED")
        self.assertTrue(
            any(value.startswith("adoption-required:") for value in plan["blockers"])
        )
        self.assertFalse(state.exists())

    def test_distribution_tamper_invalidates_package_lock(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        clone = Path(temporary.name) / "package"
        shutil.copytree(self.package_root, clone)
        design = clone / "DESIGN.md"
        design.write_text(
            design.read_text(encoding="utf-8") + "\nTampered.\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(installer.SuiteError, "distribution"):
            installer.load_verified_package(clone)

    def test_seal_rejects_source_hidden_in_bytecode_cache(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        clone = Path(temporary.name) / "package"
        shutil.copytree(self.package_root, clone)
        hidden = clone / "scripts" / "__pycache__" / "seal_and_verify.py"
        hidden.parent.mkdir(parents=True, exist_ok=True)
        hidden.write_text("print('unsealed')\n", encoding="ascii")
        with self.assertRaisesRegex(installer.SuiteError, "Unsealed source"):
            installer.seal_package(clone)

    def test_seal_rejects_stale_adapter_package_version(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        clone = Path(temporary.name) / "package"
        shutil.copytree(self.package_root, clone)
        adapter = clone / "adapters" / "agent-fleet" / "SKILL.md"
        current = self.lock["package_version"]
        adapter.write_text(
            adapter.read_text(encoding="utf-8").replace(
                f"--package-version {current}", "--package-version 0.0.0"
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            installer.SuiteError, "package version does not match release"
        ):
            installer.seal_package(clone)

    def test_seal_rejects_stale_adapter_runtime_fingerprint(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        clone = Path(temporary.name) / "package"
        shutil.copytree(self.package_root, clone)
        adapter = clone / "adapters" / "agent-loop" / "SKILL.md"
        adapter.write_text(
            adapter.read_text(encoding="utf-8").replace(
                self.lock["runtime"]["fingerprint"], "0" * 64
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            installer.SuiteError, "runtime fingerprint does not match payload"
        ):
            installer.seal_package(clone)

    def test_candidate_package_cannot_deploy(self) -> None:
        temporary, home, appdata, state = self.fixture()
        self.addCleanup(temporary.cleanup)
        clone = Path(temporary.name) / "package"
        shutil.copytree(self.package_root, clone)
        manifest_path = clone / "package.json"
        manifest = json.loads(manifest_path.read_text(encoding="ascii"))
        manifest["release_state"] = "candidate"
        installer.atomic_json(manifest_path, manifest)
        candidate_lock = installer.seal_package(clone)
        with self.assertRaisesRegex(installer.SuiteError, "released"):
            installer.deploy(
                package_root=clone,
                home=home,
                appdata=appdata,
                tools=["claude"],
                state_root=state,
                adopt_existing=True,
                expected_package_fingerprint=candidate_lock["package_fingerprint"],
            )
        self.assertFalse(
            (installer.default_runtime_target(home)).exists()
        )

    def test_repeat_deploy_is_current_and_next_tool_does_not_replace_runtime(
        self,
    ) -> None:
        temporary, home, appdata, state = self.fixture()
        self.addCleanup(temporary.cleanup)
        first = installer.deploy(
            package_root=self.package_root,
            home=home,
            appdata=appdata,
            tools=["antigravity"],
            state_root=state,
            adopt_existing=True,
            expected_package_fingerprint=self.lock["package_fingerprint"],
        )
        transaction_count = len(list((state / "transactions").iterdir()))
        repeated = installer.deploy(
            package_root=self.package_root,
            home=home,
            appdata=appdata,
            tools=["antigravity"],
            state_root=state,
            adopt_existing=True,
            expected_package_fingerprint=self.lock["package_fingerprint"],
        )
        self.assertEqual(repeated["status"], "CURRENT")
        self.assertEqual(repeated["writes_performed"], 0)
        self.assertEqual(len(list((state / "transactions").iterdir())), transaction_count)

        second = installer.deploy(
            package_root=self.package_root,
            home=home,
            appdata=appdata,
            tools=["claude"],
            state_root=state,
            adopt_existing=True,
            expected_package_fingerprint=self.lock["package_fingerprint"],
        )
        second_receipt = installer.load_json(Path(second["receipt_path"]))
        self.assertEqual(first["entry_count"], 5)
        self.assertEqual(second["entry_count"], 4)
        self.assertNotIn(
            "runtime",
            [entry["entry_id"] for entry in second_receipt["entries"]],
        )

    def test_deploy_verify_and_rollback_restore_exact_prior_state(self) -> None:
        temporary, home, appdata, state = self.fixture()
        self.addCleanup(temporary.cleanup)
        for skill in installer.SKILLS:
            self.write_old_skill(
                home / ".codex" / "skills", skill, managed_legacy=True
            )
            self.write_old_skill(home / ".agents" / "skills", skill)
            self.write_old_skill(home / ".claude" / "skills", skill)
        result = installer.deploy(
            package_root=self.package_root,
            home=home,
            appdata=appdata,
            tools=["shared", "claude"],
            state_root=state,
            adopt_existing=True,
            expected_package_fingerprint=self.lock["package_fingerprint"],
        )
        receipt = Path(result["receipt_path"])
        self.assertEqual(
            self.verify_receipt(receipt, state, home, appdata)["status"],
            "PASS",
        )
        self.assertTrue(
            (installer.default_runtime_target(home)).is_dir()
        )
        for skill in installer.SKILLS:
            self.assertFalse((home / ".codex" / "skills" / skill).exists())
            self.assertTrue(
                (home / ".agents" / "skills" / skill / installer.ADAPTER_MARKER).is_file()
            )
            self.assertTrue(
                (home / ".claude" / "skills" / skill / installer.ADAPTER_MARKER).is_file()
            )
        rollback = self.rollback(receipt, state, home, appdata)
        self.assertEqual(rollback["status"], "ROLLED_BACK")
        self.assertFalse(
            (installer.default_runtime_target(home)).exists()
        )
        for skill in installer.SKILLS:
            self.assertEqual(
                (home / ".codex" / "skills" / skill / "SKILL.md").read_text(
                    encoding="ascii"
                ),
                f"# old {skill}\n",
            )
            self.assertEqual(
                (home / ".agents" / "skills" / skill / "SKILL.md").read_text(
                    encoding="ascii"
                ),
                f"# old {skill}\n",
            )

    def test_injected_failure_auto_rolls_back_all_mutations(self) -> None:
        temporary, home, appdata, state = self.fixture()
        self.addCleanup(temporary.cleanup)
        for skill in installer.SKILLS:
            self.write_old_skill(
                home / ".codex" / "skills", skill, managed_legacy=True
            )
        with self.assertRaisesRegex(installer.SuiteError, "Injected"):
            installer.deploy(
                package_root=self.package_root,
                home=home,
                appdata=appdata,
                tools=["shared", "antigravity"],
                state_root=state,
                adopt_existing=True,
                expected_package_fingerprint=self.lock["package_fingerprint"],
                fail_after=5,
            )
        for skill in installer.SKILLS:
            self.assertEqual(
                (home / ".codex" / "skills" / skill / "SKILL.md").read_text(
                    encoding="ascii"
                ),
                f"# old {skill}\n",
            )
        self.assertFalse(
            (installer.default_runtime_target(home)).exists()
        )

    def test_active_drift_blocks_exact_rollback(self) -> None:
        temporary, home, appdata, state = self.fixture()
        self.addCleanup(temporary.cleanup)
        result = installer.deploy(
            package_root=self.package_root,
            home=home,
            appdata=appdata,
            tools=["claude"],
            state_root=state,
            adopt_existing=True,
            expected_package_fingerprint=self.lock["package_fingerprint"],
        )
        receipt = Path(result["receipt_path"])
        target = home / ".claude" / "skills" / "agent-fleet" / "SKILL.md"
        target.write_text(target.read_text(encoding="utf-8") + "\nlocal drift\n")
        with self.assertRaisesRegex(installer.SuiteError, "drift"):
            self.rollback(receipt, state, home, appdata)
        self.assertTrue(target.is_file())

    def test_tampered_receipt_paths_cannot_touch_external_sentinel(self) -> None:
        temporary, home, appdata, state = self.fixture()
        self.addCleanup(temporary.cleanup)
        result = installer.deploy(
            package_root=self.package_root,
            home=home,
            appdata=appdata,
            tools=["claude"],
            state_root=state,
            adopt_existing=True,
            expected_package_fingerprint=self.lock["package_fingerprint"],
        )
        receipt_path = Path(result["receipt_path"])
        receipt = installer.load_json(receipt_path)
        external = Path(temporary.name) / "external-sentinel"
        external.mkdir()
        sentinel = external / "keep.txt"
        sentinel.write_text("untouched\n", encoding="ascii")

        original_target = receipt["entries"][0]["target"]
        receipt["entries"][0]["target"] = str(external.resolve())
        installer.atomic_json(receipt_path, receipt)
        with self.assertRaisesRegex(installer.SuiteError, "untrusted target"):
            self.rollback(receipt_path, state, home, appdata)
        self.assertEqual(sentinel.read_text(encoding="ascii"), "untouched\n")

        receipt["entries"][0]["target"] = original_target
        receipt["entries"][0]["backup"] = str((external / "backup").resolve())
        installer.atomic_json(receipt_path, receipt)
        with self.assertRaisesRegex(installer.SuiteError, "untrusted recovery paths"):
            self.rollback(receipt_path, state, home, appdata)
        self.assertEqual(sentinel.read_text(encoding="ascii"), "untouched\n")

    def test_marker_semantic_drift_fails_verify_and_is_repaired(self) -> None:
        temporary, home, appdata, state = self.fixture()
        self.addCleanup(temporary.cleanup)
        result = installer.deploy(
            package_root=self.package_root,
            home=home,
            appdata=appdata,
            tools=["shared"],
            state_root=state,
            adopt_existing=True,
            expected_package_fingerprint=self.lock["package_fingerprint"],
        )
        receipt = Path(result["receipt_path"])
        target = home / ".agents" / "skills" / "agent-fleet"
        marker_path = target / installer.ADAPTER_MARKER
        marker = installer.load_json(marker_path)
        marker["tool"] = "grok"
        installer.atomic_json(marker_path, marker)

        self.assertEqual(
            self.verify_receipt(receipt, state, home, appdata)["status"], "FAIL"
        )
        plan = installer.inspect_plan(
            package_root=self.package_root,
            home=home,
            appdata=appdata,
            tools=["shared"],
            adopt_existing=False,
            expected_package_fingerprint=self.lock["package_fingerprint"],
        )
        row = next(
            entry
            for entry in plan["entries"]
            if entry["entry_id"] == "adapter:shared:agent-fleet"
        )
        self.assertFalse(row["marker_semantics_valid"])
        self.assertTrue(row["change_required"])

        repaired = installer.deploy(
            package_root=self.package_root,
            home=home,
            appdata=appdata,
            tools=["shared"],
            state_root=state,
            adopt_existing=False,
            expected_package_fingerprint=self.lock["package_fingerprint"],
        )
        self.assertEqual(repaired["entry_count"], 1)
        self.assertEqual(installer.load_json(marker_path)["tool"], "shared")
        self.assertEqual(
            self.verify_receipt(
                Path(repaired["receipt_path"]), state, home, appdata
            )["status"],
            "PASS",
        )

    def test_runtime_verifier_passes_then_detects_payload_tamper(self) -> None:
        temporary, home, appdata, state = self.fixture()
        self.addCleanup(temporary.cleanup)
        installer.deploy(
            package_root=self.package_root,
            home=home,
            appdata=appdata,
            tools=["claude"],
            state_root=state,
            adopt_existing=True,
            expected_package_fingerprint=self.lock["package_fingerprint"],
        )
        runtime = installer.default_runtime_target(home)
        verifier = runtime / "agent-fleet" / "scripts" / "verify_runtime.py"
        command = [
            sys.executable,
            str(verifier),
            "--runtime-root",
            str(runtime),
            "--expected-fingerprint",
            self.lock["runtime"]["fingerprint"],
            "--package-version",
            self.lock["runtime_release_version"],
        ]
        passed = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertEqual(passed.returncode, 0, passed.stderr)
        self.assertEqual(json.loads(passed.stdout)["status"], "PASS")

        fleet = runtime / "agent-fleet" / "scripts" / "fleet.py"
        fleet.write_text(
            fleet.read_text(encoding="utf-8") + "\n# injected drift\n",
            encoding="utf-8",
        )
        refused = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertEqual(refused.returncode, 2)
        self.assertEqual(json.loads(refused.stderr)["status"], "FAIL")

    def test_recover_deploy_rolls_back_a_nonterminal_receipt(self) -> None:
        temporary, home, appdata, state = self.fixture()
        self.addCleanup(temporary.cleanup)
        self.write_old_skill(home / ".claude" / "skills", "agent-fleet")
        result = installer.deploy(
            package_root=self.package_root,
            home=home,
            appdata=appdata,
            tools=["claude"],
            state_root=state,
            adopt_existing=True,
            expected_package_fingerprint=self.lock["package_fingerprint"],
        )
        receipt_path = Path(result["receipt_path"])
        receipt = json.loads(receipt_path.read_text(encoding="ascii"))
        receipt["status"] = "DEPLOYING"
        installer.atomic_json(receipt_path, receipt)
        recovered = self.recover_deploy(receipt_path, state, home, appdata)
        self.assertEqual(recovered["status"], "RECOVERED_ROLLBACK")
        self.assertEqual(
            (home / ".claude" / "skills" / "agent-fleet" / "SKILL.md").read_text(
                encoding="ascii"
            ),
            "# old agent-fleet\n",
        )

    def test_locked_runtime_uses_verified_in_place_fallback(self) -> None:
        self.check_locked_tree('runtime')

    def test_no_delete_refuses_locked_runtime_without_touching_existing_files(self) -> None:
        temporary, home, appdata, state = self.fixture()
        self.addCleanup(temporary.cleanup)
        runtime = installer.default_runtime_target(home)
        runtime.mkdir(parents=True)
        sentinel = runtime / "old-runtime.txt"
        sentinel.write_text("prior runtime\n", encoding="ascii")
        real_replace = installer.os.replace

        def deny_runtime_rename(source: object, target: object) -> None:
            if Path(source) == runtime and Path(target).parent.name == "backups":
                raise PermissionError(5, "Access is denied", str(source))
            real_replace(source, target)

        with mock.patch.object(installer.os, "replace", side_effect=deny_runtime_rename):
            with self.assertRaisesRegex(installer.SuiteError, "No-delete deploy refuses"):
                installer.deploy(
                    package_root=self.package_root, home=home, appdata=appdata,
                    tools=[], state_root=state, adopt_existing=True,
                    expected_package_fingerprint=self.lock["package_fingerprint"],
                    no_delete=True,
                )
        self.assertEqual(sentinel.read_text(encoding="ascii"), "prior runtime\n")

    def test_locked_adapter_uses_verified_in_place_fallback(self) -> None:
        self.check_locked_tree('adapter:claude:agent-fleet')

    def check_locked_tree(self, entry_id) -> None:
        temporary, home, appdata, state = self.fixture()
        self.addCleanup(temporary.cleanup)
        runtime = (installer.default_runtime_target(home)
                   if entry_id == 'runtime' else home / '.claude/skills/agent-fleet')
        runtime.mkdir(parents=True)
        (runtime / "old-runtime.txt").write_text("prior runtime\n", encoding="ascii")
        _, prior_fingerprint = installer.full_tree_manifest(runtime)
        real_replace = installer.os.replace

        def replace_with_locked_runtime(source: object, target: object) -> None:
            source_path = Path(source)
            target_path = Path(target)
            if source_path == runtime and target_path.parent.name == "backups":
                raise PermissionError(5, "Access is denied", str(source_path))
            real_replace(source, target)

        with mock.patch.object(
            installer.os, "replace", side_effect=replace_with_locked_runtime
        ):
            result = installer.deploy(
                package_root=self.package_root,
                home=home,
                appdata=appdata,
                tools=["claude"],
                state_root=state,
                adopt_existing=True,
                expected_package_fingerprint=self.lock["package_fingerprint"],
            )
            receipt_path = Path(result["receipt_path"])
            receipt = installer.load_json(receipt_path)
            runtime_entry = next(
                entry for entry in receipt["entries"] if entry["entry_id"] == entry_id
            )
            self.assertEqual(runtime_entry["mutation_mode"], "in-place-copy")
            self.assertTrue(runtime_entry["in_place_backup_complete"])
            self.assertTrue(runtime_entry["in_place_activation_started"])
            self.assertEqual(
                self.verify_receipt(receipt_path, state, home, appdata)["status"],
                "PASS",
            )
            self.assertFalse((runtime / "old-runtime.txt").exists())

            rollback = self.rollback(receipt_path, state, home, appdata)

        self.assertEqual(rollback["status"], "ROLLED_BACK")
        self.assertEqual(
            (runtime / "old-runtime.txt").read_text(encoding="ascii"),
            "prior runtime\n",
        )
        _, restored_fingerprint = installer.full_tree_manifest(runtime)
        self.assertEqual(restored_fingerprint, prior_fingerprint)


if __name__ == "__main__":
    unittest.main()
