from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import install_kimi_desktop_plugin as kimi
import install_router_suite as suite


class KimiDesktopPluginInstallerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source_package_root = Path(__file__).resolve().parents[1]
        cls.test_temp_root = cls.source_package_root / ".test-temp"
        cls.test_temp_root.mkdir(exist_ok=True)
        cls.package_temporary = tempfile.TemporaryDirectory(dir=cls.test_temp_root)
        cls.package_root = Path(cls.package_temporary.name) / "package"
        shutil.copytree(
            cls.source_package_root,
            cls.package_root,
            ignore=shutil.ignore_patterns(".test-temp", "__pycache__"),
        )
        manifest_path = cls.package_root / "package.json"
        manifest = json.loads(manifest_path.read_text(encoding="ascii"))
        manifest["release_state"] = "released"
        suite.atomic_json(manifest_path, manifest)
        suite.seal_package(cls.package_root)
        _, cls.lock = suite.load_verified_package(cls.package_root)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.package_temporary.cleanup()

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=self.test_temp_root)
        self.root = Path(self.temporary.name)
        self.share_dir = self.root / "share"
        self.home = kimi.kimi_home(self.share_dir)
        self.bundle_root = self.root / "bundle"
        self.launcher = self.bundle_root / "bin" / "kimi-daimon.ps1"
        self.launcher.parent.mkdir(parents=True)
        self.launcher.write_text("# fixture\n", encoding="ascii")
        self.node = self.root / "node.exe"
        self.node.write_bytes(b"fixture")
        self.state_root = self.root / "state"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def fake_native_install(self, **arguments: object) -> dict[str, object]:
        source = Path(arguments["source"])
        share_dir = Path(arguments["share_dir"])
        home = kimi.kimi_home(share_dir)
        target = home / kimi.PLUGIN_TARGET_RELATIVE
        registry_path = home / kimi.REGISTRY_RELATIVE
        device_path = home / kimi.DEVICE_RELATIVE
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target)
        registry = kimi.load_registry(registry_path, allow_absent=True) or {
            "version": 1,
            "plugins": [],
        }
        registry["plugins"] = [
            item
            for item in registry["plugins"]
            if item.get("id") != kimi.PLUGIN_ID
        ]
        registry["plugins"].append(
            {
                "id": kimi.PLUGIN_ID,
                "root": str(target.resolve()),
                "source": "local-path",
                "enabled": True,
                "installedAt": "2026-07-28T00:00:00.000Z",
                "updatedAt": "2026-07-28T00:00:00.000Z",
                "originalSource": str(source.resolve()),
            }
        )
        suite.atomic_json(registry_path, registry)
        if not device_path.exists():
            device_path.write_text(
                "00000000-0000-0000-0000-000000000000", encoding="ascii"
            )
        return {
            "installed": True,
            "id": kimi.PLUGIN_ID,
            "version": "0.6.1",
            "skillCount": 4,
            "state": "ok",
            "existingProcessBehavior": "restart-required",
        }

    def write_prior_state(self) -> None:
        registry_path = self.home / kimi.REGISTRY_RELATIVE
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        suite.atomic_json(
            registry_path,
            {
                "version": 1,
                "plugins": [
                    {
                        "id": "existing",
                        "root": str((self.home / "plugins" / "existing").resolve()),
                        "source": "bundled",
                        "enabled": True,
                    }
                ],
            },
        )
        (self.home / "device_id").write_text(
            "11111111-1111-1111-1111-111111111111", encoding="ascii"
        )
        (self.home / "config.toml").write_text("fixture = true\n", encoding="ascii")
        foreign_plugin = self.home / "plugins" / "managed" / "foreign-plugin"
        foreign_plugin.mkdir(parents=True, exist_ok=True)
        (foreign_plugin / "kimi.plugin.json").write_text(
            '{"name":"foreign-plugin","version":"1.0.0"}\n', encoding="ascii"
        )

    def deploy_fixture(
        self, *, no_delete: bool = False, adopt_existing: bool = False
    ) -> dict[str, object]:
        return kimi.deploy(
            package_root=self.package_root,
            share_dir=self.share_dir,
            bundle_root=self.bundle_root,
            node=self.node,
            state_root=self.state_root,
            adopt_existing=adopt_existing,
            expected_package_fingerprint=self.lock["package_fingerprint"],
            native_runner=self.fake_native_install,
            no_delete=no_delete,
        )

    def verify_receipt(self, receipt_path: Path) -> dict[str, object]:
        return kimi.verify_receipt(
            receipt_path,
            self.state_root,
            package_root=self.package_root,
            share_dir=self.share_dir,
            bundle_root=self.bundle_root,
            node=self.node,
        )

    def rollback(
        self, receipt_path: Path, *, no_delete: bool = False
    ) -> dict[str, object]:
        return kimi.rollback(
            receipt_path,
            self.state_root,
            package_root=self.package_root,
            share_dir=self.share_dir,
            bundle_root=self.bundle_root,
            node=self.node,
            no_delete=no_delete,
        )

    def recover(self, receipt_path: Path, *, no_delete: bool = False) -> dict[str, object]:
        return kimi.recover_deploy(
            receipt_path,
            self.state_root,
            package_root=self.package_root,
            share_dir=self.share_dir,
            bundle_root=self.bundle_root,
            node=self.node,
            no_delete=no_delete,
        )

    def test_node_is_found_under_both_kimi_install_directory_names(self) -> None:
        """Kimi Desktop renamed Programs\kimi-desktop to Programs\Kimi.

        Measured 2026-09-20 on Kimi Desktop 3.2.10: node.exe sat at
        Programs\Kimi\resources\resources\runtime\node.exe while the installer
        pinned the old name and reported kimi-desktop-node-missing on a working
        installation. Both names must resolve, and the tree below them is identical.
        """
        for name in kimi.KIMI_INSTALL_DIR_NAMES:
            with self.subTest(install_dir=name):
                local = Path(tempfile.mkdtemp(dir=self.root))
                node = (
                    local / "Programs" / name / "resources" / "resources"
                    / "runtime" / "node.exe"
                )
                node.parent.mkdir(parents=True)
                node.write_bytes(b"")
                self.assertEqual(kimi.default_node(local), node)

    def test_missing_node_names_a_concrete_path(self) -> None:
        """A blocker must point somewhere, not report an empty value."""
        local = Path(tempfile.mkdtemp(dir=self.root))
        resolved = kimi.default_node(local)
        self.assertFalse(resolved.exists())
        self.assertEqual(resolved.name, "node.exe")
        self.assertIn(kimi.KIMI_INSTALL_DIR_NAMES[0], resolved.parts)

    def test_unmanaged_collision_requires_adoption(self) -> None:
        target = self.home / kimi.PLUGIN_TARGET_RELATIVE
        target.mkdir(parents=True)
        (target / "unmanaged.txt").write_text("x\n", encoding="ascii")
        plan = kimi.inspect_plan(
            package_root=self.package_root,
            share_dir=self.share_dir,
            bundle_root=self.bundle_root,
            node=self.node,
            adopt_existing=False,
            expected_package_fingerprint=self.lock["package_fingerprint"],
        )
        self.assertEqual(plan["status"], "BLOCKED")
        self.assertIn(
            "adoption-required:kimi-desktop-plugin", plan["blockers"]
        )

    def test_deploy_verify_current_and_exact_rollback(self) -> None:
        self.write_prior_state()
        prior_files = kimi.snapshot_files(self.home)
        prior_directories = kimi.snapshot_directories(self.home)
        self.assertNotIn(
            "plugins/managed/foreign-plugin/kimi.plugin.json",
            kimi.snapshot_controlled_files(self.home),
        )
        result = self.deploy_fixture()
        receipt_path = Path(result["receipt_path"])
        self.assertEqual(result["status"], "DEPLOYED")
        self.assertEqual(self.verify_receipt(receipt_path)["status"], "PASS")
        current = kimi.inspect_plan(
            package_root=self.package_root,
            share_dir=self.share_dir,
            bundle_root=self.bundle_root,
            node=self.node,
            adopt_existing=False,
            expected_package_fingerprint=self.lock["package_fingerprint"],
        )
        self.assertFalse(current["change_required"])
        self.assertTrue(current["registered"])
        rollback = self.rollback(receipt_path)
        self.assertEqual(rollback["status"], "ROLLED_BACK")
        self.assertEqual(kimi.snapshot_files(self.home), prior_files)
        self.assertEqual(kimi.snapshot_directories(self.home), prior_directories)

    def test_fresh_install_rollback_restores_exact_share_absence(self) -> None:
        self.assertFalse(self.share_dir.exists())
        result = self.deploy_fixture()
        receipt_path = Path(result["receipt_path"])
        self.assertTrue(self.share_dir.exists())
        rollback = self.rollback(receipt_path)
        self.assertEqual(rollback["status"], "ROLLED_BACK")
        self.assertFalse(self.share_dir.exists())

    def test_native_failure_auto_rolls_back_exact_prior_state(self) -> None:
        self.write_prior_state()
        prior_files = kimi.snapshot_files(self.home)
        prior_directories = kimi.snapshot_directories(self.home)

        def failing_runner(**arguments: object) -> dict[str, object]:
            self.fake_native_install(**arguments)
            raise suite.SuiteError("injected native failure")

        with self.assertRaisesRegex(suite.SuiteError, "AUTO_ROLLED_BACK"):
            kimi.deploy(
                package_root=self.package_root,
                share_dir=self.share_dir,
                bundle_root=self.bundle_root,
                node=self.node,
                state_root=self.state_root,
                adopt_existing=False,
                expected_package_fingerprint=self.lock["package_fingerprint"],
                native_runner=failing_runner,
            )
        self.assertEqual(kimi.snapshot_files(self.home), prior_files)
        self.assertEqual(kimi.snapshot_directories(self.home), prior_directories)

    def test_no_delete_moves_old_plugin_before_native_and_restores_it(self) -> None:
        self.write_prior_state()
        target = self.home / kimi.PLUGIN_TARGET_RELATIVE
        target.mkdir(parents=True)
        (target / "legacy.txt").write_text("keep this tree\n", encoding="ascii")
        old_fingerprint = kimi.plugin_fingerprint(target)
        observed: dict[str, object] = {}

        def observing_runner(**arguments: object) -> dict[str, object]:
            transactions = list((self.state_root / "transactions").iterdir())
            self.assertEqual(len(transactions), 1)
            preserved = transactions[0] / "quarantine" / "preinstall-plugin"
            self.assertTrue(preserved.is_dir())
            self.assertEqual(
                (preserved / "legacy.txt").read_text(encoding="ascii"),
                "keep this tree\n",
            )
            self.assertFalse(target.exists())
            observed["preserved"] = str(preserved)
            return self.fake_native_install(**arguments)

        result = kimi.deploy(
            package_root=self.package_root,
            share_dir=self.share_dir,
            bundle_root=self.bundle_root,
            node=self.node,
            state_root=self.state_root,
            adopt_existing=True,
            expected_package_fingerprint=self.lock["package_fingerprint"],
            native_runner=observing_runner,
            no_delete=True,
        )
        receipt_path = Path(result["receipt_path"])
        receipt = suite.load_json(receipt_path)
        self.assertIs(receipt["no_delete"], True)
        self.assertEqual(result["status"], "DEPLOYED")

        rollback = self.rollback(receipt_path)
        self.assertEqual(rollback["status"], "ROLLED_BACK")
        self.assertEqual(kimi.plugin_fingerprint(target), old_fingerprint)
        self.assertTrue((target / "legacy.txt").exists())
        self.assertTrue((Path(observed["preserved"]).parent / "active-plugin").is_dir())
        self.assertTrue((receipt_path.parent / "backups" / "plugin").is_dir())

    def test_no_delete_auto_rollback_keeps_old_plugin(self) -> None:
        self.write_prior_state()
        target = self.home / kimi.PLUGIN_TARGET_RELATIVE
        target.mkdir(parents=True)
        (target / "legacy.txt").write_text("auto rollback source\n", encoding="ascii")
        old_fingerprint = kimi.plugin_fingerprint(target)
        prior_device_id = (self.home / kimi.DEVICE_RELATIVE).read_text(encoding="ascii")

        def install_then_fail(**arguments: object) -> dict[str, object]:
            transactions = list((self.state_root / "transactions").iterdir())
            preserved = transactions[0] / "quarantine" / "preinstall-plugin"
            self.assertEqual(
                (preserved / "legacy.txt").read_text(encoding="ascii"),
                "auto rollback source\n",
            )
            self.fake_native_install(**arguments)
            raise suite.SuiteError("injected no-delete failure")

        with self.assertRaisesRegex(suite.SuiteError, "AUTO_ROLLED_BACK"):
            kimi.deploy(
                package_root=self.package_root,
                share_dir=self.share_dir,
                bundle_root=self.bundle_root,
                node=self.node,
                state_root=self.state_root,
                adopt_existing=True,
                expected_package_fingerprint=self.lock["package_fingerprint"],
                native_runner=install_then_fail,
                no_delete=True,
            )
        self.assertEqual(kimi.plugin_fingerprint(target), old_fingerprint)
        self.assertEqual(
            (self.home / kimi.DEVICE_RELATIVE).read_text(encoding="ascii"),
            prior_device_id,
        )
        transactions = list((self.state_root / "transactions").iterdir())
        receipt = suite.load_json(transactions[0] / "receipt.json")
        self.assertIs(receipt["no_delete"], True)
        self.assertEqual(receipt["status"], "AUTO_ROLLED_BACK")
        self.assertTrue((transactions[0] / "quarantine" / "active-plugin").exists())

    def test_no_delete_recover_preserves_quarantine_and_created_directories(self) -> None:
        target = self.home / kimi.PLUGIN_TARGET_RELATIVE
        result = self.deploy_fixture(no_delete=True)
        receipt_path = Path(result["receipt_path"])
        receipt = suite.load_json(receipt_path)
        receipt["status"] = "INSTALLING"
        suite.atomic_json(receipt_path, receipt)

        recovered = self.recover(receipt_path)
        self.assertEqual(recovered["status"], "RECOVERED_ROLLBACK")
        self.assertIsNone(kimi.plugin_fingerprint(target))
        self.assertTrue((receipt_path.parent / "quarantine" / "active-plugin").is_dir())
        self.assertTrue(self.share_dir.exists())
        self.assertTrue(
            (receipt_path.parent / "quarantine" / "created-device-id").is_file()
        )

    def test_no_delete_rollback_fails_closed_on_registry_drift(self) -> None:
        self.write_prior_state()
        target = self.home / kimi.PLUGIN_TARGET_RELATIVE
        target.mkdir(parents=True)
        (target / "legacy.txt").write_text("must survive\n", encoding="ascii")
        result = self.deploy_fixture(no_delete=True, adopt_existing=True)
        receipt_path = Path(result["receipt_path"])
        registry_path = self.home / kimi.REGISTRY_RELATIVE
        registry = kimi.load_registry(registry_path)
        registry["plugins"].append(
            {"id": "concurrent", "root": str((self.home / "plugins" / "concurrent").resolve())}
        )
        suite.atomic_json(registry_path, registry)

        with self.assertRaisesRegex(suite.SuiteError, "drift"):
            self.rollback(receipt_path)
        preserved = receipt_path.parent / "quarantine" / "preinstall-plugin"
        self.assertTrue((preserved / "legacy.txt").exists())
        self.assertTrue(target.exists())

    def test_no_delete_flags_are_supported_for_deploy_rollback_and_recover(self) -> None:
        parser = kimi.build_parser()
        for args in (
            ["deploy", "--no-delete", "--expected-package-fingerprint", "x"],
            ["rollback", "--no-delete", "--receipt", "r"],
            ["recover-deploy", "--no-delete", "--receipt", "r"],
        ):
            with self.subTest(args=args):
                self.assertTrue(parser.parse_args(args).no_delete)

    def test_native_powershell_child_uses_create_no_window(self) -> None:
        completed = subprocess.CompletedProcess([], 0, '{"installed":true}', "")
        with mock.patch.object(kimi.subprocess, "run", return_value=completed) as run:
            kimi.run_native_install(
                source=self.package_root / "payload" / "kimi-desktop-plugin",
                share_dir=self.share_dir,
                launcher=self.launcher,
                node=self.node,
            )
        expected = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        self.assertEqual(run.call_args.kwargs["creationflags"], expected)

    def test_registry_drift_blocks_exact_rollback(self) -> None:
        self.write_prior_state()
        result = self.deploy_fixture()
        receipt_path = Path(result["receipt_path"])
        registry_path = self.home / kimi.REGISTRY_RELATIVE
        registry = json.loads(registry_path.read_text(encoding="ascii"))
        registry["plugins"].append(
            {
                "id": "concurrent",
                "root": str((self.home / "plugins" / "concurrent").resolve()),
                "source": "local-path",
                "enabled": True,
            }
        )
        suite.atomic_json(registry_path, registry)
        with self.assertRaisesRegex(suite.SuiteError, "drift"):
            self.rollback(receipt_path)

    def test_foreign_plugin_update_keeps_verify_green_but_blocks_exact_rollback(self) -> None:
        self.write_prior_state()
        result = self.deploy_fixture()
        receipt_path = Path(result["receipt_path"])
        registry_path = self.home / kimi.REGISTRY_RELATIVE
        registry = kimi.load_registry(registry_path)
        registry["plugins"].append(
            {
                "id": "task-bootstrap",
                "root": str((self.home / "plugins" / "managed" / "task-bootstrap").resolve()),
                "source": "local-path",
                "enabled": True,
            }
        )
        suite.atomic_json(registry_path, registry)
        after = registry_path.read_bytes()

        verification = self.verify_receipt(receipt_path)
        self.assertEqual(verification["status"], "PASS", verification)
        self.assertFalse(verification["checks"]["registry_bytes"])
        self.assertFalse(verification["rollback_safe"])
        self.assertIn("task-bootstrap", verification["registry_other_ids"])
        with self.assertRaisesRegex(suite.SuiteError, "drift"):
            self.rollback(receipt_path)
        self.assertEqual(registry_path.read_bytes(), after)
        self.assertEqual(suite.load_json(receipt_path)["status"], "DEPLOYED")

    def test_own_registry_entry_drift_still_fails_verify(self) -> None:
        self.write_prior_state()
        result = self.deploy_fixture()
        receipt_path = Path(result["receipt_path"])
        registry_path = self.home / kimi.REGISTRY_RELATIVE
        registry = kimi.load_registry(registry_path)
        own = kimi.plugin_entries(registry)[0]
        own["source"] = "tampered-source"
        suite.atomic_json(registry_path, registry)

        verification = self.verify_receipt(receipt_path)
        self.assertEqual(verification["status"], "FAIL", verification)
        self.assertFalse(verification["checks"]["registry_entry"])
        with self.assertRaisesRegex(suite.SuiteError, "drift"):
            self.rollback(receipt_path)

    def test_tampered_receipt_paths_cannot_touch_external_sentinel(self) -> None:
        self.write_prior_state()
        result = self.deploy_fixture()
        receipt_path = Path(result["receipt_path"])
        receipt = suite.load_json(receipt_path)
        external = self.root / "external-sentinel"
        external.mkdir()
        sentinel = external / "keep.txt"
        sentinel.write_text("untouched\n", encoding="ascii")

        receipt["target"] = str(external.resolve())
        suite.atomic_json(receipt_path, receipt)
        with self.assertRaisesRegex(suite.SuiteError, "untrusted target path"):
            self.rollback(receipt_path)
        self.assertEqual(sentinel.read_text(encoding="ascii"), "untouched\n")

        receipt["target"] = str((self.home / kimi.PLUGIN_TARGET_RELATIVE).resolve())
        receipt["prior"]["cleanup_paths"][0]["path"] = str(external.resolve())
        suite.atomic_json(receipt_path, receipt)
        with self.assertRaisesRegex(suite.SuiteError, "untrusted cleanup path"):
            self.rollback(receipt_path)
        self.assertEqual(sentinel.read_text(encoding="ascii"), "untouched\n")

    def test_volatile_session_changes_do_not_block_plugin_transaction(self) -> None:
        self.write_prior_state()
        volatile = self.home / "sessions" / "active" / "tool-result.txt"
        volatile.parent.mkdir(parents=True)
        volatile.write_text("ephemeral\n", encoding="ascii")
        prior_controlled = kimi.snapshot_controlled_files(self.home)

        def concurrent_session_runner(**arguments: object) -> dict[str, object]:
            volatile.unlink()
            return self.fake_native_install(**arguments)

        result = kimi.deploy(
            package_root=self.package_root,
            share_dir=self.share_dir,
            bundle_root=self.bundle_root,
            node=self.node,
            state_root=self.state_root,
            adopt_existing=False,
            expected_package_fingerprint=self.lock["package_fingerprint"],
            native_runner=concurrent_session_runner,
        )
        receipt_path = Path(result["receipt_path"])
        self.assertEqual(self.verify_receipt(receipt_path)["status"], "PASS")
        self.assertEqual(
            self.rollback(receipt_path)["status"], "ROLLED_BACK"
        )
        self.assertEqual(kimi.snapshot_controlled_files(self.home), prior_controlled)


if __name__ == "__main__":
    unittest.main()
