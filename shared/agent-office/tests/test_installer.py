from __future__ import annotations

import ast
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

import install_agent_office as installer


REPO_ROOT = Path(__file__).resolve().parents[3]
BACKUP_MODULE = REPO_ROOT / "global" / "codex" / "backup" / "shared_skills_backup.py"
OWNERSHIP_RECORDS = REPO_ROOT / "global" / "codex" / "skills-ownership.json"

_REAL_PROTECT = installer.protect_private_bytes
_REAL_UNPROTECT = installer.unprotect_private_bytes


def _protect_or_skip(data: bytes) -> bytes:
    """Install transactions encrypt config images with Windows DPAPI.

    The ``os.name != 'nt'`` gate in the installer is a platform requirement,
    not an integration failure: report it as a skip with proof instead of an
    error, and re-raise every other DeployError unchanged.
    """
    try:
        return _REAL_PROTECT(data)
    except installer.DeployError as exc:
        if "Windows DPAPI" in str(exc):
            raise unittest.SkipTest(
                "install transactions require Windows DPAPI (os.name != 'nt')"
            ) from exc
        raise


def _unprotect_or_skip(data: bytes) -> bytes:
    try:
        return _REAL_UNPROTECT(data)
    except installer.DeployError as exc:
        if "Windows DPAPI" in str(exc):
            raise unittest.SkipTest(
                "install transactions require Windows DPAPI (os.name != 'nt')"
            ) from exc
        raise

# The pre-fix mirrored entrypoint: it inserted the runtime root on sys.path and
# imported coordfs while bytecode writing was still enabled.
UNPATCHED_ENTRYPOINT = b"".join(
    (
        b"#!/usr/bin/env python3\n",
        b'"""Mirrored office CLI entry. Safe under `python -I` without an install."""\n',
        b"\n",
        b"from __future__ import annotations\n",
        b"\n",
        b"import sys\n",
        b"from pathlib import Path\n",
        b"\n",
        b"sys.path.insert(0, str(Path(__file__).resolve().parent))\n",
        b"\n",
        b"from coordfs.office_cli import main\n",
        b"\n",
        b'if __name__ == "__main__":\n',
        b"    raise SystemExit(main())\n",
    )
)


class AgentOfficeInstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="agent-office-installer-")
        self.root = Path(self.temp.name)
        self.home = self.root / "Synthetic User Home With Spaces"
        self.appdata = self.home / "AppData" / "Roaming"
        self.state = self.home / ".agents" / "agent-office-deploy"
        self.python = self.home / "Python Runtime With Spaces" / "python.exe"
        self.python.parent.mkdir(parents=True)
        self.python.write_bytes(b"synthetic-python-fixture")
        self.home.mkdir(exist_ok=True)
        self.pin_patch = mock.patch.object(installer, "pin_python", return_value=self.python)
        self.pin_patch.start()
        self.dpapi_patch = mock.patch.object(installer, "protect_private_bytes", side_effect=_protect_or_skip)
        self.dpapi_patch.start()
        self.dpapi_unpatch = mock.patch.object(installer, "unprotect_private_bytes", side_effect=_unprotect_or_skip)
        self.dpapi_unpatch.start()

    def tearDown(self) -> None:
        self.dpapi_unpatch.stop()
        self.dpapi_patch.stop()
        self.pin_patch.stop()
        self.temp.cleanup()

    def install(self) -> dict[str, object]:
        return installer.install(self.home, self.appdata, self.state, self.python)

    def receipt(self) -> dict[str, object]:
        receipts = list((self.state / "transactions").glob("*/receipt.json"))
        self.assertEqual(len(receipts), 1)
        return json.loads(receipts[0].read_text(encoding="utf-8"))

    def test_dry_run_is_write_free(self) -> None:
        before = sorted(path.relative_to(self.home).as_posix() for path in self.home.rglob("*"))
        result = installer.dry_run(self.home, self.appdata, self.state, self.python)
        after = sorted(path.relative_to(self.home).as_posix() for path in self.home.rglob("*"))
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["writes_performed"], 0)
        self.assertEqual(after, before)
        self.assertFalse(self.state.exists())

    def test_install_verify_rollback_recover_and_durable_home_is_untouched(self) -> None:
        durable = self.home / ".agents" / "coord" / "office" / "inbox" / "alice.jsonl"
        durable.parent.mkdir(parents=True)
        durable.write_bytes(b'{"synthetic":"message"}\n')
        original = durable.read_bytes()

        deployed = self.install()
        self.assertEqual(deployed["status"], "DEPLOYED")
        transaction_id = str(deployed["transaction_id"])
        current = installer.verify_install(self.home, self.appdata, self.state, self.python)
        self.assertEqual(current["status"], "CURRENT")
        self.assertEqual(durable.read_bytes(), original)

        rolled = installer.rollback(self.home, self.appdata, self.state, transaction_id)
        self.assertEqual(rolled["status"], "ROLLED_BACK")
        repeated = installer.rollback(self.home, self.appdata, self.state, transaction_id)
        self.assertEqual(repeated["status"], "ROLLED_BACK")
        self.assertEqual(durable.read_bytes(), original)

        recovered = installer.recover(
            self.home, self.appdata, self.state, transaction_id, self.python
        )
        self.assertEqual(recovered["status"], "DEPLOYED")
        self.assertEqual(
            installer.verify_install(self.home, self.appdata, self.state, self.python)["status"],
            "CURRENT",
        )
        self.assertEqual(durable.read_bytes(), original)

    def test_repeated_install_returns_current_without_new_transaction(self) -> None:
        first = self.install()
        second = self.install()
        self.assertEqual(second["status"], "CURRENT")
        self.assertEqual(second["transaction_id"], first["transaction_id"])
        transactions = [path for path in (self.state / "transactions").iterdir() if path.is_dir()]
        self.assertEqual(len(transactions), 1)

    def test_preimage_race_preserves_external_edit_and_compensates_own_writes(self) -> None:
        claude = self.home / ".claude.json"
        claude.write_bytes(b'{"race":"before"}\n')
        original_activate = installer._activate_target
        fired = False

        def race(row, transaction_root, from_field, to_field, home):
            nonlocal fired
            if row["id"] == "claude-mcp" and not fired:
                fired = True
                claude.write_bytes(b'{"race":"external-edit"}\n')
            return original_activate(row, transaction_root, from_field, to_field, home)

        with mock.patch.object(installer, "_activate_target", side_effect=race):
            with self.assertRaises(installer.TransitionFailure):
                self.install()
        self.assertEqual(claude.read_bytes(), b'{"race":"external-edit"}\n')
        self.assertFalse((self.home / installer.RUNTIME_RELATIVE).exists())
        for _, _, relative, _ in installer.SKILL_TARGETS:
            self.assertFalse((self.home / relative).exists())
        self.assertEqual(self.receipt()["status"], "FAILED")

    def test_write_failure_after_activation_is_compensated(self) -> None:
        original_activate = installer._activate_target
        fired = False

        def fail_after_write(row, transaction_root, from_field, to_field, home):
            nonlocal fired
            result = original_activate(row, transaction_root, from_field, to_field, home)
            if row["id"] == "skill-shared-agents" and not fired:
                fired = True
                raise OSError("synthetic write failure")
            return result

        with mock.patch.object(installer, "_activate_target", side_effect=fail_after_write):
            with self.assertRaises(installer.TransitionFailure):
                self.install()
        self.assertFalse((self.home / installer.RUNTIME_RELATIVE).exists())
        for _, _, relative, _ in installer.SKILL_TARGETS:
            self.assertFalse((self.home / relative).exists())
        self.assertEqual(self.receipt()["status"], "AUTO_ROLLED_BACK")

    def test_prepare_receipt_write_failure_removes_private_staging(self) -> None:
        original_write = installer.atomic_write_json

        def fail_prepare_receipt(path, value, *, private=False):
            if path.name == "receipt.json" and value.get("status") == "PREPARED":
                raise OSError("synthetic prepare receipt failure")
            return original_write(path, value, private=private)

        with mock.patch.object(installer, "atomic_write_json", side_effect=fail_prepare_receipt):
            with self.assertRaises(OSError):
                self.install()
        transactions = self.state / "transactions"
        self.assertTrue(transactions.is_dir())
        self.assertEqual(list(transactions.iterdir()), [])
        self.assertFalse((self.home / installer.RUNTIME_RELATIVE).exists())

    def test_deployed_receipt_write_failure_restores_exact_prestate(self) -> None:
        original_write = installer.atomic_write_json
        fired = False

        def fail_deployed_receipt(path, value, *, private=False):
            nonlocal fired
            if path.name == "receipt.json" and value.get("status") == "DEPLOYED" and not fired:
                fired = True
                raise OSError("synthetic deployed receipt failure")
            return original_write(path, value, private=private)

        with mock.patch.object(installer, "atomic_write_json", side_effect=fail_deployed_receipt):
            with self.assertRaises(installer.TransitionFailure):
                self.install()
        self.assertTrue(fired)
        self.assertFalse((self.home / installer.RUNTIME_RELATIVE).exists())
        for _, _, relative, _ in installer.SKILL_TARGETS:
            self.assertFalse((self.home / relative).exists())
        self.assertEqual(self.receipt()["status"], "AUTO_ROLLED_BACK")

    def test_changed_live_config_blocks_rollback_before_any_write(self) -> None:
        deployed = self.install()
        runtime = self.home / installer.RUNTIME_RELATIVE
        runtime_before = installer.snapshot(runtime)
        claude = self.home / ".claude.json"
        payload = json.loads(claude.read_text(encoding="utf-8"))
        payload["unrelated_edit_after_install"] = True
        claude.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaises(installer.DeployError):
            installer.rollback(
                self.home, self.appdata, self.state, str(deployed["transaction_id"])
            )
        self.assertEqual(installer.snapshot(runtime), runtime_before)
        self.assertEqual(self.receipt()["status"], "DEPLOYED")

    def test_upgrade_preserves_newer_native_config_and_rolls_back(self) -> None:
        deployed = self.install()
        old_transaction_id = str(deployed["transaction_id"])
        runtime = self.home / installer.RUNTIME_RELATIVE / "office.py"
        old_runtime = runtime.read_bytes()
        claude = self.home / ".claude.json"
        claude_payload = json.loads(claude.read_text(encoding="utf-8"))
        claude_payload["newer_native_setting"] = {"keep": True}
        claude.write_text(json.dumps(claude_payload), encoding="utf-8")

        package = installer.verify_package()
        upgraded_runtime = dict(package.runtime_files)
        upgraded_runtime["office.py"] += b"\n# synthetic upgrade\n"
        upgraded_skill = dict(package.skill_files)
        upgraded_skill["SKILL.md"] += b"\n<!-- synthetic upgrade -->\n"
        upgraded_package = replace(
            package,
            fingerprint="1" * 64,
            runtime_fingerprint="2" * 64,
            skill_fingerprint="3" * 64,
            runtime_files=upgraded_runtime,
            skill_files=upgraded_skill,
        )
        with mock.patch.object(installer, "verify_package", return_value=upgraded_package):
            upgraded = installer.upgrade(
                self.home, self.appdata, self.state, old_transaction_id, self.python
            )
            self.assertEqual(upgraded["status"], "DEPLOYED")
            new_transaction_id = str(upgraded["transaction_id"])
            self.assertNotEqual(new_transaction_id, old_transaction_id)
            self.assertNotEqual(runtime.read_bytes(), old_runtime)
            self.assertEqual(
                json.loads(claude.read_text(encoding="utf-8"))["newer_native_setting"],
                {"keep": True},
            )
            self.assertEqual(
                installer.verify_install(
                    self.home, self.appdata, self.state, self.python
                )["status"],
                "CURRENT",
            )

        rolled_back = installer.rollback(
            self.home, self.appdata, self.state, new_transaction_id
        )
        self.assertEqual(rolled_back["status"], "ROLLED_BACK")
        self.assertEqual(runtime.read_bytes(), old_runtime)
        self.assertEqual(
            json.loads(claude.read_text(encoding="utf-8"))["newer_native_setting"],
            {"keep": True},
        )
        old_receipt = json.loads(
            (self.state / "transactions" / old_transaction_id / "receipt.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(old_receipt["status"], "DEPLOYED")

    def test_upgrade_blocks_runtime_drift_before_creating_transaction(self) -> None:
        deployed = self.install()
        old_transaction_id = str(deployed["transaction_id"])
        runtime = self.home / installer.RUNTIME_RELATIVE / "office.py"
        runtime.write_bytes(runtime.read_bytes() + b"\n# external drift\n")
        with self.assertRaises(installer.DeployError):
            installer.upgrade(
                self.home, self.appdata, self.state, old_transaction_id, self.python
            )
        transactions = [
            path for path in (self.state / "transactions").iterdir() if path.is_dir()
        ]
        self.assertEqual([path.name for path in transactions], [old_transaction_id])
        self.assertEqual(
            json.loads(
                (self.state / "transactions" / old_transaction_id / "receipt.json").read_text(
                    encoding="utf-8"
                )
            )["status"],
            "DEPLOYED",
        )

    def test_no_delete_upgrade_preview_and_round_trip_preserve_tree_bytes(self) -> None:
        deployed = self.install()
        old_id = str(deployed["transaction_id"])
        runtime = self.home / installer.RUNTIME_RELATIVE
        cache = runtime / "coordfs" / "__pycache__" / "synthetic.pyc"
        cache.parent.mkdir()
        cache.write_bytes(b"original bytecode cache")
        original_file = (runtime / "office.py").read_bytes()
        settings = self.home / ".claude" / "settings.json"
        original_settings = settings.read_bytes()
        package = installer.verify_package()
        upgraded_runtime = dict(package.runtime_files)
        upgraded_runtime["office.py"] += b"\n# preserved upgrade\n"
        upgraded_package = replace(
            package,
            fingerprint="1" * 64,
            runtime_fingerprint="2" * 64,
            runtime_files=upgraded_runtime,
        )
        original_patch = installer.patch_claude_settings

        def synthetic_settings_patch(data):
            value = json.loads(original_patch(data))
            value["synthetic_upgrade_marker"] = True
            return json.dumps(value, sort_keys=True).encode("utf-8")

        with mock.patch.object(installer, "verify_package", return_value=upgraded_package), mock.patch.object(
            installer, "patch_claude_settings", side_effect=synthetic_settings_patch
        ):
            before = sorted(path.relative_to(self.home).as_posix() for path in self.home.rglob("*"))
            preview = installer.upgrade(
                self.home, self.appdata, self.state, old_id, self.python,
                no_delete=True, preview=True,
            )
            self.assertEqual(preview["status"], "READY")
            self.assertEqual(preview["writes_performed"], 0)
            self.assertEqual(sorted(path.relative_to(self.home).as_posix() for path in self.home.rglob("*")), before)
            upgraded = installer.upgrade(
                self.home, self.appdata, self.state, old_id, self.python, no_delete=True
            )
            new_id = str(upgraded["transaction_id"])
            self.assertEqual(upgraded["preserve_mode"], "no-delete")
            self.assertFalse(cache.exists())
            self.assertEqual(installer.verify_install(self.home, self.appdata, self.state, self.python)["status"], "CURRENT")

            transaction_root = self.state / "transactions" / new_id
            originals = [
                row for row in upgraded["preserved_operations"]
                if row["target_id"] == "runtime" and row["from"] == "before"
            ]
            self.assertEqual(len(originals), 1)
            archived = transaction_root / originals[0]["archive"]
            self.assertEqual((archived / "office.py").read_bytes(), original_file)
            self.assertEqual((archived / "coordfs" / "__pycache__" / "synthetic.pyc").read_bytes(), b"original bytecode cache")
            settings_archive = next(
                transaction_root / row["archive"] for row in upgraded["preserved_operations"]
                if row["target_id"] == "claude-settings"
            )
            self.assertEqual(settings_archive.read_bytes(), original_settings)

            rolled = installer.rollback(self.home, self.appdata, self.state, new_id)
            self.assertEqual(rolled["status"], "ROLLED_BACK")
            self.assertEqual((runtime / "office.py").read_bytes(), original_file)
            self.assertEqual(cache.read_bytes(), b"original bytecode cache")
            self.assertEqual(settings.read_bytes(), original_settings)
            self.assertTrue(archived.is_dir())
            self.assertTrue(any(
                row["target_id"] == "runtime" and row["from"] == "after"
                for row in rolled["preserved_operations"]
            ))
            recovered = installer.recover(self.home, self.appdata, self.state, new_id, self.python)
            self.assertEqual(recovered["status"], "DEPLOYED")
            self.assertEqual((runtime / "office.py").read_bytes(), upgraded_runtime["office.py"])

    def test_no_delete_upgrade_preserves_staging_if_activation_never_starts(self) -> None:
        deployed = self.install()
        package = installer.verify_package()
        upgraded_package = replace(package, fingerprint="c" * 64)
        transactions = self.state / "transactions"
        before = set(transactions.iterdir())
        with mock.patch.object(installer, "verify_package", return_value=upgraded_package), mock.patch.object(
            installer, "update_receipt", side_effect=OSError("synthetic receipt failure")
        ):
            with self.assertRaisesRegex(installer.DeployError, "private transaction was preserved"):
                installer.upgrade(
                    self.home, self.appdata, self.state,
                    str(deployed["transaction_id"]), self.python, no_delete=True,
                )
        added = set(transactions.iterdir()) - before
        self.assertEqual(len(added), 1)
        self.assertTrue((next(iter(added)) / "receipt.json").is_file())
        self.assertEqual(deployed["transaction_id"], installer.verify_install(
            self.home, self.appdata, self.state, self.python
        )["transaction_id"])

    def test_no_delete_upgrade_failure_compensates_without_erasing_archives(self) -> None:
        deployed = self.install()
        runtime = self.home / installer.RUNTIME_RELATIVE
        original = (runtime / "office.py").read_bytes()
        cache = runtime / "coordfs" / "__pycache__" / "synthetic.pyc"
        cache.parent.mkdir()
        cache.write_bytes(b"cache before failed upgrade")
        package = installer.verify_package()
        upgraded_runtime = dict(package.runtime_files)
        upgraded_runtime["office.py"] += b"\n# fail after runtime\n"
        upgraded_package = replace(package, fingerprint="4" * 64, runtime_fingerprint="5" * 64, runtime_files=upgraded_runtime)
        original_activate = installer.activate_preserved_target
        fired = False

        def fail_after_runtime(receipt, transaction_root, row, from_field, to_field, home):
            nonlocal fired
            original_activate(receipt, transaction_root, row, from_field, to_field, home)
            if row["id"] == "runtime" and from_field == "before" and not fired:
                fired = True
                raise OSError("synthetic failure after runtime activation")

        with mock.patch.object(installer, "verify_package", return_value=upgraded_package), mock.patch.object(
            installer, "activate_preserved_target", side_effect=fail_after_runtime
        ):
            with self.assertRaises(installer.TransitionFailure):
                installer.upgrade(self.home, self.appdata, self.state, str(deployed["transaction_id"]), self.python, no_delete=True)
        self.assertTrue(fired)
        self.assertEqual((runtime / "office.py").read_bytes(), original)
        self.assertEqual(cache.read_bytes(), b"cache before failed upgrade")
        receipts = [json.loads(path.read_text(encoding="utf-8")) for path in (self.state / "transactions").glob("*/receipt.json")]
        failed = next(row for row in receipts if row.get("preserve_mode") == "no-delete")
        self.assertEqual(failed["status"], "AUTO_ROLLED_BACK")
        transaction_root = self.state / "transactions" / failed["transaction_id"]
        archives = [transaction_root / row["archive"] for row in failed["preserved_operations"] if row["target_id"] == "runtime"]
        self.assertEqual(len(archives), 2)
        self.assertTrue(all(path.is_dir() for path in archives))
        self.assertEqual((archives[0] / "coordfs" / "__pycache__" / "synthetic.pyc").read_bytes(), b"cache before failed upgrade")

    def test_no_delete_recover_normalizes_interrupted_tree_move(self) -> None:
        deployed = self.install()
        package = installer.verify_package()
        upgraded_runtime = dict(package.runtime_files)
        upgraded_runtime["office.py"] += b"\n# interrupted upgrade\n"
        upgraded_package = replace(package, fingerprint="6" * 64, runtime_fingerprint="7" * 64, runtime_files=upgraded_runtime)
        original_move = installer.move_without_replace
        runtime = self.home / installer.RUNTIME_RELATIVE
        fired = False

        def interrupt_after_archive(source, destination):
            nonlocal fired
            if "activation" in source.parts and destination == runtime and not fired:
                fired = True
                raise KeyboardInterrupt("synthetic interrupted activation")
            return original_move(source, destination)

        with mock.patch.object(installer, "verify_package", return_value=upgraded_package):
            with mock.patch.object(installer, "move_without_replace", side_effect=interrupt_after_archive):
                with self.assertRaises(KeyboardInterrupt):
                    installer.upgrade(self.home, self.appdata, self.state, str(deployed["transaction_id"]), self.python, no_delete=True)
            self.assertTrue(fired)
            self.assertFalse(runtime.exists())
            receipts = [json.loads(path.read_text(encoding="utf-8")) for path in (self.state / "transactions").glob("*/receipt.json")]
            interrupted = next(row for row in receipts if row.get("preserve_mode") == "no-delete")
            recovered = installer.recover(self.home, self.appdata, self.state, interrupted["transaction_id"], self.python)
            self.assertEqual(recovered["status"], "DEPLOYED")
            self.assertEqual((runtime / "office.py").read_bytes(), upgraded_runtime["office.py"])
            self.assertTrue(any(row["state"] == "COMPENSATED" for row in recovered["preserved_operations"]))

    def test_no_delete_failed_rollback_restores_deployment_and_can_retry(self) -> None:
        deployed = self.install()
        package = installer.verify_package()
        upgraded_runtime = dict(package.runtime_files)
        upgraded_runtime["office.py"] += b"\n# rollback retry\n"
        upgraded_package = replace(package, fingerprint="9" * 64, runtime_fingerprint="a" * 64, runtime_files=upgraded_runtime)
        with mock.patch.object(installer, "verify_package", return_value=upgraded_package):
            upgraded = installer.upgrade(self.home, self.appdata, self.state, str(deployed["transaction_id"]), self.python, no_delete=True)
            original_activate = installer.activate_preserved_target
            fired = False

            def fail_once(receipt, transaction_root, row, from_field, to_field, home):
                nonlocal fired
                original_activate(receipt, transaction_root, row, from_field, to_field, home)
                if row["id"] == "runtime" and from_field == "after" and not fired:
                    fired = True
                    raise OSError("synthetic rollback failure")

            with mock.patch.object(installer, "activate_preserved_target", side_effect=fail_once):
                with self.assertRaises(installer.TransitionFailure):
                    installer.rollback(self.home, self.appdata, self.state, str(upgraded["transaction_id"]))
            self.assertTrue(fired)
            self.assertEqual((self.home / installer.RUNTIME_RELATIVE / "office.py").read_bytes(), upgraded_runtime["office.py"])
            self.assertEqual(installer.verify_install(self.home, self.appdata, self.state, self.python)["status"], "CURRENT")
            rolled = installer.rollback(self.home, self.appdata, self.state, str(upgraded["transaction_id"]))
            self.assertEqual(rolled["status"], "ROLLED_BACK")

    def test_no_delete_receipt_rejects_archive_path_tampering(self) -> None:
        deployed = self.install()
        package = installer.verify_package()
        upgraded_package = replace(package, fingerprint="8" * 64)
        with mock.patch.object(installer, "verify_package", return_value=upgraded_package):
            upgraded = installer.upgrade(self.home, self.appdata, self.state, str(deployed["transaction_id"]), self.python, no_delete=True)
        receipt_path = self.state / "transactions" / str(upgraded["transaction_id"]) / "receipt.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["preserved_operations"][0]["archive"] = "../../outside"
        installer.atomic_write_json(receipt_path, receipt, private=True)
        live = (self.home / installer.RUNTIME_RELATIVE / "office.py").read_bytes()
        with self.assertRaises(installer.DeployError):
            installer.rollback(self.home, self.appdata, self.state, str(upgraded["transaction_id"]))
        self.assertEqual((self.home / installer.RUNTIME_RELATIVE / "office.py").read_bytes(), live)

    def test_no_delete_receipt_rejects_changed_archive_bytes(self) -> None:
        deployed = self.install()
        package = installer.verify_package()
        upgraded_package = replace(package, fingerprint="b" * 64)
        with mock.patch.object(installer, "verify_package", return_value=upgraded_package):
            upgraded = installer.upgrade(self.home, self.appdata, self.state, str(deployed["transaction_id"]), self.python, no_delete=True)
        transaction_root = self.state / "transactions" / str(upgraded["transaction_id"])
        runtime_archive = next(
            transaction_root / row["archive"] for row in upgraded["preserved_operations"]
            if row["target_id"] == "runtime"
        )
        old_file = runtime_archive / "office.py"
        old_file.write_bytes(old_file.read_bytes() + b"\n# synthetic archive drift\n")
        live = (self.home / installer.RUNTIME_RELATIVE / "office.py").read_bytes()
        with self.assertRaises(installer.DeployError):
            installer.rollback(self.home, self.appdata, self.state, str(upgraded["transaction_id"]))
        self.assertEqual((self.home / installer.RUNTIME_RELATIVE / "office.py").read_bytes(), live)

    def test_unknown_skill_ownership_is_refused(self) -> None:
        foreign = self.home / ".agents" / "skills" / "agent-office"
        foreign.mkdir(parents=True)
        (foreign / "SKILL.md").write_text("foreign", encoding="utf-8")
        with self.assertRaises(installer.DeployError):
            self.install()
        self.assertEqual((foreign / "SKILL.md").read_text(encoding="utf-8"), "foreign")
        self.assertFalse((self.home / installer.RUNTIME_RELATIVE).exists())
        transactions = self.state / "transactions"
        self.assertFalse(transactions.exists())

    def test_unknown_runtime_marker_is_rejected_before_python_execution(self) -> None:
        runtime = self.home / installer.RUNTIME_RELATIVE
        runtime.mkdir(parents=True)
        (runtime / installer.RUNTIME_MARKER).write_text(
            json.dumps(
                {
                    "transaction_id": "0" * 32,
                    "python_executable": str(self.python),
                    "managed_by": "foreign-installer",
                }
            ),
            encoding="utf-8",
        )
        with mock.patch.object(
            installer,
            "pin_python",
            side_effect=AssertionError("foreign executable must not be probed"),
        ):
            with self.assertRaises(installer.DeployError):
                self.install()

    def test_unrelated_json_toml_bytes_round_trip_exactly(self) -> None:
        claude = self.home / ".claude.json"
        settings = self.home / ".claude" / "settings.json"
        codex = self.home / ".codex" / "config.toml"
        claude.parent.mkdir(parents=True, exist_ok=True)
        settings.parent.mkdir(parents=True, exist_ok=True)
        codex.parent.mkdir(parents=True, exist_ok=True)
        claude_before = b'{\r\n  "spacing"  : [ 1,  2 ],\r\n  "unicode": "synthetic-zolc"\r\n}\r\n'
        settings_before = b'{\n  "permissions": {"allow": ["Read"]},\n  "env": {"OTHER": "synthetic", "MCP_TOOL_TIMEOUT": "999999"}\n}\n'
        codex_before = (
            b"# exact unrelated comment\r\n"
            b'model = "synthetic-model"\r\n\r\n'
            b"[features]\r\n"
            b"example = true\r\n"
        )
        claude.write_bytes(claude_before)
        settings.write_bytes(settings_before)
        codex.write_bytes(codex_before)

        deployed = self.install()
        self.assertIn(b'"spacing"  : [ 1,  2 ]', claude.read_bytes())
        self.assertIn(b'"OTHER": "synthetic"', settings.read_bytes())
        self.assertTrue(codex.read_bytes().startswith(codex_before))
        self.assertIn(installer.TOML_BEGIN.encode("ascii"), codex.read_bytes())

        installer.rollback(
            self.home, self.appdata, self.state, str(deployed["transaction_id"])
        )
        self.assertEqual(claude.read_bytes(), claude_before)
        self.assertEqual(settings.read_bytes(), settings_before)
        self.assertEqual(codex.read_bytes(), codex_before)

    def test_wrapper_quotes_absolute_paths_with_spaces_and_forwards_arguments(self) -> None:
        deployed = self.install()
        wrapper = self.home / installer.RUNTIME_RELATIVE / "office.cmd"
        text = wrapper.read_text(encoding="utf-8")
        runtime_script = self.home / installer.RUNTIME_RELATIVE / "office.py"
        coord_home = self.home / installer.COORD_RELATIVE
        self.assertIn(
            f'"{self.python}" -I "{runtime_script}" --runtime-home "{coord_home}" %*', text
        )
        self.assertIn("exit /b %ERRORLEVEL%", text)
        self.assertTrue(Path(str(deployed["python_executable"])).is_absolute())

    def test_timeout_target_precedes_activation_and_values_are_exact(self) -> None:
        original_activate = installer._activate_target
        order: list[str] = []

        def observe(row, transaction_root, from_field, to_field, home):
            if row["id"] == "claude-mcp":
                settings_row = next(item for item in self.receipt()["targets"] if item["id"] == "claude-settings")
                self.assertEqual(installer.snapshot(Path(settings_row["path"])), settings_row["after"])
            if row["id"] == "codex-mcp":
                image = installer._read_file_image(transaction_root, row, "after")
                installer.validate_codex_mcp_bytes(
                    image,
                    installer.codex_server(
                        self.python,
                        self.home / installer.RUNTIME_RELATIVE,
                        self.home / installer.COORD_RELATIVE,
                    ),
                )
            order.append(str(row["id"]))
            return original_activate(row, transaction_root, from_field, to_field, home)

        with mock.patch.object(installer, "_activate_target", side_effect=observe):
            self.install()
        self.assertLess(order.index("claude-settings"), order.index("claude-mcp"))
        settings = json.loads((self.home / ".claude" / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["env"]["MCP_TOOL_TIMEOUT"], "100000000")
        claude = json.loads((self.home / ".claude.json").read_text(encoding="utf-8"))
        self.assertNotIn("env", claude["mcpServers"][installer.PACKAGE_ID])
        self.assertEqual(
            claude["mcpServers"][installer.PACKAGE_ID]["args"][2:4],
            ["--home", str(self.home / installer.COORD_RELATIVE)],
        )
        codex_text = (self.home / ".codex" / "config.toml").read_text(encoding="utf-8")
        self.assertIn("startup_timeout_sec = 30", codex_text)
        self.assertIn("tool_timeout_sec = 300", codex_text)
        self.assertIn("required = false", codex_text)

    def test_invalid_or_conflicting_claude_timeout_is_visible_and_write_free(self) -> None:
        cases = (
            {"env": {"MCP_TOOL_TIMEOUT": "299999"}},
            {"env": {"MCP_TOOL_TIMEOUT": "not-numeric"}},
            {"env": {"mcp_tool_timeout": "100000000"}},
        )
        for index, payload in enumerate(cases):
            with self.subTest(index=index), tempfile.TemporaryDirectory(prefix="office-timeout-") as temp:
                home = Path(temp) / "home"
                settings = home / ".claude" / "settings.json"
                settings.parent.mkdir(parents=True)
                before = json.dumps(payload, indent=2).encode("utf-8")
                settings.write_bytes(before)
                state = home / ".agents" / "agent-office-deploy"
                with self.assertRaises(installer.DeployError):
                    installer.dry_run(home, home / "AppData" / "Roaming", state, self.python)
                self.assertEqual(settings.read_bytes(), before)
                self.assertFalse(state.exists())

    def test_path_traversal_root_escape_and_reparse_fail_closed(self) -> None:
        with self.assertRaises(installer.DeployError):
            installer.safe_source_path(installer.SOURCE_ROOT, "../outside.py")
        outside = self.root / "outside-state"
        with self.assertRaises(installer.DeployError):
            installer.dry_run(self.home, self.appdata, outside, self.python)
        with mock.patch.object(installer, "is_reparse", return_value=True):
            with self.assertRaises(installer.DeployError):
                installer.assert_plain_chain(self.home / "child", "synthetic target")

    def test_markers_bind_tool_transaction_and_derived_source_hashes(self) -> None:
        self.assertEqual(
            installer.SKILL_TARGETS,
            (
                (
                    "skill-shared-agents",
                    "shared-agents",
                    Path(".agents/skills/agent-office"),
                    "shared",
                ),
                (
                    "skill-claude-code",
                    "claude-code",
                    Path(".claude/skills/agent-office"),
                    "claude",
                ),
                (
                    "skill-antigravity",
                    "antigravity",
                    Path(".gemini/config/skills/agent-office"),
                    "antigravity",
                ),
                (
                    "skill-grok-cli",
                    "grok-cli",
                    Path(".grok/skills/agent-office"),
                    "grok",
                ),
                (
                    "skill-qwen-code",
                    "qwen-code",
                    Path(".qwen/skills/agent-office"),
                    "qwen",
                ),
            ),
        )
        unknown_codex_skill = self.home / ".codex" / "skills" / "agent-office" / "SKILL.md"
        unknown_codex_skill.parent.mkdir(parents=True)
        unknown_codex_skill.write_bytes(b"unknown-live-root\n")
        package = installer.verify_package()
        deployed = self.install()
        transaction_id = str(deployed["transaction_id"])
        self.assertEqual(unknown_codex_skill.read_bytes(), b"unknown-live-root\n")
        runtime = self.home / installer.RUNTIME_RELATIVE
        runtime_marker = json.loads((runtime / installer.RUNTIME_MARKER).read_text(encoding="utf-8"))
        self.assertEqual(runtime_marker["package_fingerprint"], package.fingerprint)
        self.assertEqual(runtime_marker["source_fingerprint"], package.runtime_fingerprint)
        self.assertEqual(runtime_marker["transaction_id"], transaction_id)
        for identifier, _, relative, tool in installer.SKILL_TARGETS:
            target = self.home / relative
            marker = json.loads((target / installer.SKILL_MARKER).read_text(encoding="utf-8"))
            self.assertEqual(marker["tool"], tool, identifier)
            self.assertEqual(marker["source_fingerprint"], package.skill_fingerprint)
            self.assertEqual(marker["transaction_id"], transaction_id)
            self.assertEqual(
                (target / "SKILL.md").read_bytes(),
                (installer.SKILL_SOURCE_ROOT / "SKILL.md").read_bytes(),
            )
        target = self.home / installer.SKILL_TARGETS[0][2] / "SKILL.md"
        target.write_bytes(target.read_bytes() + b"\n# tampered\n")
        self.assertEqual(
            installer.verify_install(self.home, self.appdata, self.state, self.python)["status"],
            "DRIFT",
        )

    def test_manifest_hash_mismatch_is_rejected(self) -> None:
        package_root = self.root / "bad-package"
        (package_root / "skill" / "agent-office").mkdir(parents=True)
        (package_root / "runtime.py").write_bytes(b"runtime")
        (package_root / "skill" / "agent-office" / "SKILL.md").write_bytes(b"skill")
        manifest = {
            "schema_version": 1,
            "package_id": installer.PACKAGE_ID,
            "package_version": installer.PACKAGE_VERSION,
            "runtime_version": "synthetic",
            "runtime_fingerprint": "0" * 64,
            "skill_fingerprint": "0" * 64,
            "runtime": [
                {"source": "runtime.py", "target": "runtime.py", "bytes": 7, "sha256": "0" * 64}
            ],
            "skill": [
                {
                    "source": "skill/agent-office/SKILL.md",
                    "target": "SKILL.md",
                    "bytes": 5,
                    "sha256": hashlib.sha256(b"skill").hexdigest(),
                }
            ],
        }
        manifest_path = package_root / "package.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with (
            mock.patch.object(installer, "SOURCE_ROOT", package_root),
            mock.patch.object(installer, "PACKAGE_PATH", manifest_path),
            mock.patch.object(installer, "RUNTIME_SOURCE_MAP", (("runtime.py", "runtime.py"),)),
            mock.patch.object(
                installer,
                "SKILL_SOURCE_MAP",
                (("skill/agent-office/SKILL.md", "SKILL.md"),),
            ),
        ):
            with self.assertRaises(installer.DeployError):
                installer.verify_package()

    def test_stop_gate_hook_is_staged_and_bound_by_manifest(self) -> None:
        """The STOP gate hook is a runtime file: staged like any other, never activated here."""
        hook_source = installer.SOURCE_ROOT / "hooks" / "office_wake_stop_gate.py"
        hook_row = next(
            row for row in installer.verify_package().manifest["runtime"]
            if row["source"] == "hooks/office_wake_stop_gate.py"
        )
        self.assertEqual(hook_row["target"], "hooks/office_wake_stop_gate.py")
        self.assertEqual(hook_row["sha256"], hashlib.sha256(hook_source.read_bytes()).hexdigest())
        user_settings = self.home / ".claude" / "settings.json"
        user_settings.parent.mkdir(parents=True)
        existing_hooks = {
            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "synthetic-existing-hook"}]}]
        }
        user_settings.write_text(json.dumps({"hooks": existing_hooks, "env": {"KEEP": "synthetic"}}), encoding="utf-8")
        self.install()
        staged = self.home / installer.RUNTIME_RELATIVE / "hooks" / "office_wake_stop_gate.py"
        self.assertEqual(staged.read_bytes(), hook_source.read_bytes())
        self.assertEqual(
            installer.verify_install(self.home, self.appdata, self.state, self.python)["status"],
            "CURRENT",
        )
        settings_example = installer.SOURCE_ROOT / "hooks" / "office-wake-stop-gate.settings.example.json"
        settings_after = json.loads(user_settings.read_text(encoding="utf-8"))
        self.assertEqual(settings_after["hooks"], existing_hooks)
        self.assertEqual(settings_after["env"]["KEEP"], "synthetic")
        self.assertEqual(settings_after["env"]["MCP_TOOL_TIMEOUT"], str(installer.CLAUDE_TIMEOUT_DEFAULT_MS))
        self.assertFalse((self.appdata / "Claude" / "settings.json").exists())
        self.assertTrue(settings_example.is_file())

    def test_tampered_hook_runtime_blocks_rollback_until_owned_fixture_is_restored(self) -> None:
        deployed = self.install()
        transaction_id = str(deployed["transaction_id"])
        hook = self.home / installer.RUNTIME_RELATIVE / "hooks" / "office_wake_stop_gate.py"
        original = hook.read_bytes()
        hook.write_bytes(original + b"\n# external drift\n")
        self.assertEqual(
            installer.verify_install(self.home, self.appdata, self.state, self.python)["status"],
            "DRIFT",
        )
        drifted = hook.read_bytes()
        with self.assertRaises(installer.DeployError):
            installer.rollback(self.home, self.appdata, self.state, transaction_id)
        self.assertEqual(hook.read_bytes(), drifted)
        # Restore only the synthetic file this test changed; real drift is never discarded.
        hook.write_bytes(original)
        rolled = installer.rollback(self.home, self.appdata, self.state, transaction_id)
        self.assertEqual(rolled["status"], "ROLLED_BACK")
        self.assertFalse((self.home / installer.RUNTIME_RELATIVE).exists())
        recovered = installer.recover(
            self.home, self.appdata, self.state, transaction_id, self.python
        )
        self.assertEqual(recovered["status"], "DEPLOYED")
        self.assertEqual(hook.read_bytes(), original)
        self.assertEqual(
            installer.verify_install(self.home, self.appdata, self.state, self.python)["status"],
            "CURRENT",
        )

    def test_hook_manifest_hash_mismatch_is_rejected(self) -> None:
        """A malformed/forged hash for the hook row must fail closed, like any other row."""
        package_root = self.root / "bad-hook-package"
        (package_root / "skill" / "agent-office").mkdir(parents=True)
        (package_root / "hooks").mkdir(parents=True)
        (package_root / "runtime.py").write_bytes(b"runtime")
        (package_root / "hooks" / "office_wake_stop_gate.py").write_bytes(b"hook-source")
        (package_root / "skill" / "agent-office" / "SKILL.md").write_bytes(b"skill")
        manifest = {
            "schema_version": 1,
            "package_id": installer.PACKAGE_ID,
            "package_version": installer.PACKAGE_VERSION,
            "runtime_version": "synthetic",
            "runtime_fingerprint": "0" * 64,
            "skill_fingerprint": "0" * 64,
            "runtime": [
                {"source": "runtime.py", "target": "runtime.py", "bytes": 7, "sha256": hashlib.sha256(b"runtime").hexdigest()},
                {
                    "source": "hooks/office_wake_stop_gate.py",
                    "target": "hooks/office_wake_stop_gate.py",
                    "bytes": 11,
                    "sha256": "0" * 64,
                },
            ],
            "skill": [
                {
                    "source": "skill/agent-office/SKILL.md",
                    "target": "SKILL.md",
                    "bytes": 5,
                    "sha256": hashlib.sha256(b"skill").hexdigest(),
                }
            ],
        }
        manifest_path = package_root / "package.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with (
            mock.patch.object(installer, "SOURCE_ROOT", package_root),
            mock.patch.object(installer, "PACKAGE_PATH", manifest_path),
            mock.patch.object(
                installer,
                "RUNTIME_SOURCE_MAP",
                (
                    ("runtime.py", "runtime.py"),
                    ("hooks/office_wake_stop_gate.py", "hooks/office_wake_stop_gate.py"),
                ),
            ),
            mock.patch.object(
                installer,
                "SKILL_SOURCE_MAP",
                (("skill/agent-office/SKILL.md", "SKILL.md"),),
            ),
        ):
            with self.assertRaises(installer.DeployError):
                installer.verify_package()

    def test_receipt_contains_hashes_not_config_payloads_and_state_is_private(self) -> None:
        claude = self.home / ".claude.json"
        sentinel = "synthetic-private-preimage-value"
        claude.write_text(json.dumps({"unrelated": sentinel}), encoding="utf-8")
        deployed = self.install()
        receipt_path = self.state / "transactions" / str(deployed["transaction_id"]) / "receipt.json"
        receipt_text = receipt_path.read_text(encoding="utf-8")
        self.assertNotIn(sentinel, receipt_text)
        self.assertTrue(receipt_path.is_relative_to(self.home / ".agents" / "agent-office-deploy"))
        self.assertFalse(receipt_path.is_relative_to(self.home / installer.RUNTIME_RELATIVE))
        receipt = json.loads(receipt_text)
        self.assertTrue(all("sha256" in row["after"] or row["after"]["state"] == "tree" for row in receipt["targets"]))
        transaction_root = receipt_path.parent
        encrypted_images = list(transaction_root.rglob(f"*{installer.PRIVATE_FILE_SUFFIX}"))
        self.assertTrue(encrypted_images)
        self.assertTrue(all(sentinel.encode("utf-8") not in path.read_bytes() for path in encrypted_images))
        claude_row = next(row for row in receipt["targets"] if row["id"] == "claude-mcp")
        self.assertIn(
            sentinel.encode("utf-8"),
            installer._read_file_image(transaction_root, claude_row, "before"),
        )

    def test_backup_iterator_excludes_runtime_receipts_and_test_outputs(self) -> None:
        spec = importlib.util.spec_from_file_location("office_backup_test_module", BACKUP_MODULE)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader if spec else None)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        source = self.root / "backup-source"
        source.mkdir()
        (source / "office.py").write_text("source", encoding="utf-8")
        generated = (
            source / ".agents" / "coord" / "office" / "inbox",
            source / "agent-office-deploy" / "transactions" / "synthetic",
            source / "test-results",
            source / "__pycache__",
        )
        for directory in generated:
            directory.mkdir(parents=True)
            (directory / "payload.json").write_text("generated", encoding="utf-8")
        files = [relative for _, relative in module.iter_files(source)]
        self.assertEqual(files, [Path("office.py")])


class InstalledEntrypointBytecodeRegressionTests(unittest.TestCase):
    """`python -I` also implies `-E`, so only the installed entrypoint can refuse bytecode."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="agent-office-bytecode-")
        self.root = Path(self.temp.name)
        self.home = self.root / "Synthetic User Home With Spaces"
        self.appdata = self.home / "AppData" / "Roaming"
        self.state = self.home / ".agents" / "agent-office-deploy"
        self.runtime = self.home / installer.RUNTIME_RELATIVE
        self.coord_home = self.home / installer.COORD_RELATIVE
        self.entrypoint = self.runtime / "office.py"
        self.python = installer.absolute_lexical(Path(sys.executable))
        self.home.mkdir(parents=True)
        self.claude = self.home / ".claude.json"
        self.claude_preimage = b'{\n  "unrelated": "synthetic-preimage"\n}\n'
        self.claude.write_bytes(self.claude_preimage)
        self.pin_patch = mock.patch.object(installer, "pin_python", return_value=self.python)
        self.pin_patch.start()
        self.dpapi_patch = mock.patch.object(installer, "protect_private_bytes", side_effect=_protect_or_skip)
        self.dpapi_patch.start()
        self.dpapi_unpatch = mock.patch.object(installer, "unprotect_private_bytes", side_effect=_unprotect_or_skip)
        self.dpapi_unpatch.start()
        deployed = installer.install(self.home, self.appdata, self.state, self.python)
        self.assertEqual(deployed["status"], "DEPLOYED")
        self.transaction_id = str(deployed["transaction_id"])

    def tearDown(self) -> None:
        self.dpapi_unpatch.stop()
        self.dpapi_patch.stop()
        self.pin_patch.stop()
        self.temp.cleanup()

    def verification_status(self) -> str:
        report = installer.verify_install(self.home, self.appdata, self.state, self.python)
        return str(report["status"])

    def isolated_env(self) -> dict[str, str]:
        env = {
            name: value
            for name, value in os.environ.items()
            if not name.upper().startswith(("PYTH", "COORDFS_", "OFFICE_"))
        }
        self.assertFalse([name for name in env if "BYTECODE" in name.upper()])
        self.assertFalse([name for name in env if "PYCACHEPREFIX" in name.upper()])
        return env

    def runtime_files(self) -> dict[str, tuple[int, str]]:
        files = {}
        for path, relative in installer.iter_plain_files(self.runtime):
            data = path.read_bytes()
            files[relative] = (len(data), installer.sha256_bytes(data))
        return files

    def cache_artifacts(self) -> list[str]:
        artifacts = []
        for path in self.runtime.rglob("*"):
            relative = path.relative_to(self.runtime)
            if "__pycache__" in relative.parts or path.suffix == ".pyc":
                artifacts.append(relative.as_posix())
        return sorted(artifacts)

    def entrypoint_argv(self, *arguments: str) -> list[str]:
        return [
            str(self.python),
            "-I",
            str(self.entrypoint),
            "--home",
            str(self.coord_home),
            "--harness",
            "synthetic",
            "--session-id",
            "installer-bytecode-regression",
            *arguments,
        ]

    def launch(self, argv: list[str]) -> subprocess.CompletedProcess:
        self.assertEqual(argv[1], "-I")
        self.assertNotIn("-B", argv)
        self.assertNotIn("-E", argv)
        return subprocess.run(
            argv,
            cwd=str(self.home),
            env=self.isolated_env(),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=120,
            check=False,
        )

    def write_rpc(self, process: subprocess.Popen, payload: dict) -> None:
        self.assertIsNotNone(process.stdin)
        process.stdin.write(json.dumps(payload).encode("utf-8") + b"\n")
        process.stdin.flush()

    def read_rpc(self, process: subprocess.Popen) -> dict:
        self.assertIsNotNone(process.stdout)
        line = process.stdout.readline()
        self.assertTrue(line, "installed MCP server closed stdout before responding")
        return json.loads(line.decode("utf-8"))

    def test_installed_entrypoint_help_and_cli_leave_runtime_bytes_exact(self) -> None:
        files_before = self.runtime_files()
        snapshot_before = installer.snapshot(self.runtime)
        self.assertEqual(self.cache_artifacts(), [])
        self.assertEqual(self.verification_status(), "CURRENT")

        help_run = self.launch(self.entrypoint_argv("--help"))
        self.assertEqual(help_run.returncode, 0, help_run.stderr)
        self.assertIn("Agent Office", help_run.stdout)
        version_run = self.launch(self.entrypoint_argv("--version"))
        self.assertEqual(version_run.returncode, 0, version_run.stderr)
        self.assertIn("agent-office", version_run.stdout)
        channels_run = self.launch(self.entrypoint_argv("channels"))
        self.assertEqual(channels_run.returncode, 0, channels_run.stderr)
        self.assertEqual(json.loads(channels_run.stdout)["code"], "channels")

        self.assertEqual(self.cache_artifacts(), [])
        self.assertEqual(self.runtime_files(), files_before)
        self.assertEqual(installer.snapshot(self.runtime), snapshot_before)
        self.assertEqual(self.verification_status(), "CURRENT")

    def test_installed_lease_cli_reports_version_and_leaves_runtime_bytes_exact(self) -> None:
        """`coord` is the deterministic lease CLI; running it must not drift the tree."""
        files_before = self.runtime_files()
        snapshot_before = installer.snapshot(self.runtime)
        for target in (
            "coord.cmd",
            "coord.py",
            "coordfs/cli.py",
            "coordfs/doctor.py",
            "coordfs/__main__.py",
        ):
            self.assertIn(target, files_before)

        entrypoint = self.runtime / "coord.py"
        version_run = self.launch([str(self.python), "-I", str(entrypoint), "--version"])
        self.assertEqual(version_run.returncode, 0, version_run.stderr)
        self.assertIn("coordfs", version_run.stdout)

        self.assertEqual(self.cache_artifacts(), [])
        self.assertEqual(self.runtime_files(), files_before)
        self.assertEqual(installer.snapshot(self.runtime), snapshot_before)
        self.assertEqual(self.verification_status(), "CURRENT")

    def test_installed_mcp_registration_initializes_and_exits_cleanly_on_eof(self) -> None:
        files_before = self.runtime_files()
        snapshot_before = installer.snapshot(self.runtime)
        registration = json.loads(self.claude.read_text(encoding="utf-8"))["mcpServers"][
            installer.PACKAGE_ID
        ]
        self.assertEqual(
            registration, installer.claude_server(self.python, self.runtime, self.coord_home)
        )
        self.assertEqual(registration["args"][:2], ["-I", str(self.entrypoint)])
        self.assertNotIn("-B", registration["args"])

        process = subprocess.Popen(
            [registration["command"], *registration["args"]],
            cwd=str(self.home),
            env=self.isolated_env(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            self.write_rpc(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "installer-bytecode-regression", "version": "0"},
                    },
                },
            )
            initialized = self.read_rpc(process)
            self.assertEqual(initialized["result"]["serverInfo"]["name"], "agent-office")
            self.write_rpc(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            tools = self.read_rpc(process)
            self.assertIn(
                "office_send", {tool["name"] for tool in tools["result"]["tools"]}
            )
            self.write_rpc(
                process,
                {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            )
        finally:
            process.stdin.close()
            stdout, stderr = process.communicate(timeout=120)
        self.assertEqual(process.returncode, 0, stderr.decode("utf-8", "replace") + stdout.decode("utf-8", "replace"))

        self.assertEqual(self.cache_artifacts(), [])
        self.assertEqual(self.runtime_files(), files_before)
        self.assertEqual(installer.snapshot(self.runtime), snapshot_before)
        self.assertEqual(self.verification_status(), "CURRENT")

    def test_unpatched_entrypoint_reproduces_the_cache_without_drifting(self) -> None:
        """The unpatched entrypoint still writes bytecode; bytecode is no longer drift.

        The cache really appears - that is what this regression pins - but it is
        interpreter output, not package payload. Counting it made using Office
        block upgrading Office, so verification ignores the cache instead of
        deleting it. The patched entrypoint in the shipped package still refuses
        to write bytecode at all, which is what keeps the cache empty in practice.
        """
        files_before = self.runtime_files()
        snapshot_before = installer.snapshot(self.runtime)
        patched = self.entrypoint.read_bytes()
        self.assertEqual(patched, (installer.SOURCE_ROOT / "office.py").read_bytes())
        try:
            self.entrypoint.write_bytes(UNPATCHED_ENTRYPOINT)
            # Swapping the entrypoint is itself a managed-file edit, so it moves the
            # fingerprint on its own. Baseline against the swapped tree: what this
            # regression pins is that RUNNING it - and only the bytecode that appears -
            # leaves the fingerprint alone.
            snapshot_swapped = installer.snapshot(self.runtime)
            self.assertNotEqual(snapshot_swapped, snapshot_before)
            completed = self.launch(self.entrypoint_argv("channels"))
            self.assertEqual(completed.returncode, 0, completed.stderr)
            artifacts = self.cache_artifacts()
            self.assertTrue(
                artifacts,
                "the unpatched -I entrypoint must still create coordfs/__pycache__ or this test is vacuous",
            )
            self.assertTrue(all("__pycache__/" in item or item.endswith("__pycache__") for item in artifacts))
            self.assertEqual(installer.snapshot(self.runtime), snapshot_swapped)
            self.entrypoint.write_bytes(patched)
            self.assertEqual(self.runtime_files(), files_before)
            self.assertEqual(
                self.verification_status(),
                "CURRENT",
                "the interpreter cache must not be reported as deployment drift",
            )
        finally:
            self.entrypoint.write_bytes(patched)
            # Moved out of the tree rather than deleted: this suite adds no
            # removal step, not even for the cache it produced itself.
            staging = self.root / "moved-cache-artifacts"
            staging.mkdir(parents=True, exist_ok=True)
            for index, relative in enumerate(reversed(self.cache_artifacts())):
                artifact = self.runtime / relative
                os.replace(artifact, staging / f"{index}-{artifact.name}")
        self.assertEqual(self.cache_artifacts(), [])
        self.assertEqual(self.runtime_files(), files_before)
        self.assertEqual(self.verification_status(), "CURRENT")

    def test_packaged_generated_launcher_and_runtime_tree_reject_extra_files(self) -> None:
        package = installer.verify_package()
        payload = installer.runtime_payload(
            package, self.transaction_id, self.python, self.runtime, self.coord_home
        )
        launcher = self.runtime / "office.cmd"
        self.assertEqual(launcher.read_bytes(), payload["office.cmd"])
        text = launcher.read_text(encoding="utf-8")
        self.assertIn(
            f'"{self.python}" -I "{self.entrypoint}" --runtime-home "{self.coord_home}" %*', text
        )
        self.assertNotIn("-B", text)
        payload_names = set(payload)
        self.assertEqual(
            {relative for _, relative in installer.iter_plain_files(self.runtime)}, payload_names
        )

        # Interpreter bytecode is not package payload: ignored, never counted, never deleted.
        cache = self.runtime / "coordfs" / "__pycache__" / "office.cpython-399.pyc"
        cache.parent.mkdir(parents=True)
        cache.write_bytes(b"synthetic-cache-artifact")
        self.assertEqual(self.verification_status(), "CURRENT")
        self.assertEqual(
            {relative for _, relative in installer.iter_plain_files(self.runtime)}, payload_names
        )
        os.replace(cache, self.root / "synthetic-cache-artifact.moved")

        # A foreign non-cache file inside the managed tree is still drift.
        extra = self.runtime / "coordfs" / "synthetic-extra.py"
        extra.write_bytes(b"synthetic-package-file")
        self.assertEqual(self.verification_status(), "DRIFT")
        os.replace(extra, self.root / "synthetic-extra.moved")
        self.assertEqual(self.verification_status(), "CURRENT")

    def test_generated_mcp_args_stay_unchanged_and_conflicting_owned_entries_are_refused(self) -> None:
        claude_desired = installer.claude_server(self.python, self.runtime, self.coord_home)
        codex_desired = installer.codex_server(self.python, self.runtime, self.coord_home)
        self.assertEqual(claude_desired["args"][0], "-I")
        self.assertEqual(codex_desired["args"][0], "-I")
        for injected in ("-B", "-E"):
            conflicting_args = [claude_desired["args"][0], injected, *claude_desired["args"][1:]]
            conflicting = json.dumps(
                {"mcpServers": {installer.PACKAGE_ID: {**claude_desired, "args": conflicting_args}}},
                indent=2,
            ).encode("utf-8")
            with self.assertRaises(installer.DeployError):
                installer.patch_claude_mcp(conflicting, claude_desired)
            codex_conflict = "\n".join(
                (
                    f"[mcp_servers.{installer.PACKAGE_ID}]",
                    f"command = {installer.toml_string(codex_desired['command'])}",
                    "args = [" + ", ".join(installer.toml_string(item) for item in conflicting_args) + "]",
                    f"startup_timeout_sec = {codex_desired['startup_timeout_sec']}",
                    f"tool_timeout_sec = {codex_desired['tool_timeout_sec']}",
                    "required = false",
                    "",
                )
            ).encode("utf-8")
            with self.assertRaises(installer.DeployError):
                installer.patch_codex_mcp(codex_conflict, codex_desired)

    def test_entrypoint_refuses_bytecode_before_importing_local_modules(self) -> None:
        source = self.entrypoint.read_text(encoding="utf-8")
        self.assertEqual(source.encode("utf-8"), (installer.SOURCE_ROOT / "office.py").read_bytes())
        guard_lines: list[int] = []
        local_import_lines: list[int] = []
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Attribute) and target.attr == "dont_write_bytecode"
                for target in node.targets
            ):
                self.assertIsInstance(node.value, ast.Constant)
                self.assertIs(node.value.value, True)
                guard_lines.append(node.lineno)
            if isinstance(node, ast.Import):
                local_import_lines.extend(
                    node.lineno
                    for alias in node.names
                    if alias.name.split(".")[0] == "coordfs"
                )
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "coordfs":
                local_import_lines.append(node.lineno)
        self.assertEqual(len(guard_lines), 1)
        self.assertTrue(local_import_lines)
        self.assertLess(guard_lines[0], min(local_import_lines))

    def test_old_transaction_rolls_back_to_preimage_then_new_patch_rolls_back_and_recovers(self) -> None:
        durable = self.coord_home / "office" / "inbox" / "synthetic-peer.jsonl"
        durable.parent.mkdir(parents=True, exist_ok=True)
        durable.write_bytes(b'{"synthetic":"durable"}\n')
        self.assertEqual(self.verification_status(), "CURRENT")

        rolled = installer.rollback(self.home, self.appdata, self.state, self.transaction_id)
        self.assertEqual(rolled["status"], "ROLLED_BACK")
        self.assertFalse(self.runtime.exists())
        self.assertEqual(self.claude.read_bytes(), self.claude_preimage)
        self.assertEqual(durable.read_bytes(), b'{"synthetic":"durable"}\n')

        second = installer.install(self.home, self.appdata, self.state, self.python)
        self.assertEqual(second["status"], "DEPLOYED")
        second_id = str(second["transaction_id"])
        self.assertNotEqual(second_id, self.transaction_id)
        self.assertEqual(self.verification_status(), "CURRENT")

        completed = self.launch(self.entrypoint_argv("channels"))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.cache_artifacts(), [])
        self.assertEqual(self.verification_status(), "CURRENT")

        rolled_again = installer.rollback(self.home, self.appdata, self.state, second_id)
        self.assertEqual(rolled_again["status"], "ROLLED_BACK")
        self.assertFalse(self.runtime.exists())
        recovered = installer.recover(self.home, self.appdata, self.state, second_id, self.python)
        self.assertEqual(recovered["status"], "DEPLOYED")
        self.assertEqual(self.verification_status(), "CURRENT")
        self.assertEqual(durable.read_bytes(), b'{"synthetic":"durable"}\n')
        for transaction_id in (self.transaction_id, second_id):
            receipt = json.loads(
                (self.state / "transactions" / transaction_id / "receipt.json").read_text(encoding="utf-8")
            )
            self.assertEqual(receipt["package_version"], installer.PACKAGE_VERSION)
            self.assertEqual(receipt["package_id"], installer.PACKAGE_ID)

    def test_patch_release_metadata_and_owned_manifest_agree(self) -> None:
        manifest = json.loads(installer.PACKAGE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(manifest["package_id"], installer.PACKAGE_ID)
        self.assertEqual(manifest["package_version"], installer.PACKAGE_VERSION)
        self.assertEqual(manifest["runtime_version"], "0.2.0")
        self.assertEqual(
            [row["source"] for row in manifest["runtime"]],
            [source for source, _ in installer.RUNTIME_SOURCE_MAP],
        )
        runtime_marker = json.loads(
            (self.runtime / installer.RUNTIME_MARKER).read_text(encoding="utf-8")
        )
        self.assertEqual(runtime_marker["package_version"], installer.PACKAGE_VERSION)
        skill_marker = json.loads(
            (
                self.home / installer.SKILL_TARGETS[0][2] / installer.SKILL_MARKER
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(skill_marker["capability_version"], installer.PACKAGE_VERSION)
        contract = json.loads(OWNERSHIP_RECORDS.read_text(encoding="utf-8"))["manager_contracts"][
            installer.MANAGED_BY
        ]
        self.assertEqual(contract["package_id"], installer.PACKAGE_ID)
        self.assertEqual(contract["marker_version_field"], "capability_version")
        self.assertEqual(contract["marker_version"], installer.PACKAGE_VERSION)
        self.assertEqual(contract["receipt_version_field"], "package_version")
        self.assertEqual(contract["receipt_version"], installer.PACKAGE_VERSION)


class OwnedConfigVerificationRegressionTests(unittest.TestCase):
    """Standalone verify tolerates unrelated foreign shared-config fields.

    The three owned config targets (``.claude.json``, ``.claude/settings.json``
    and ``.codex/config.toml``) are shared with native tools that append their
    own metadata. A native Claude CLI run adds a top-level ``skillUsage`` key to
    ``.claude.json`` while leaving the Agent Office MCP registration untouched.
    Standalone ``verify`` must report that whole-file byte non-equality yet stay
    CURRENT while the owned semantics validate exactly. That tolerance is granted
    only by the explicit ``strict=False`` the read-only surfaces pass:
    ``verify_install`` is strict by default, so install and recover keep a
    byte-exact compare-and-set at every check and a repeated install refuses the
    same drift without writing anything, exactly like rollback and recover.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="agent-office-owned-config-")
        self.root = Path(self.temp.name)
        self.home = self.root / "Synthetic User Home With Spaces"
        self.appdata = self.home / "AppData" / "Roaming"
        self.state = self.home / ".agents" / "agent-office-deploy"
        self.python = self.home / "Python Runtime With Spaces" / "python.exe"
        self.python.parent.mkdir(parents=True)
        self.python.write_bytes(b"synthetic-python-fixture")
        self.home.mkdir(exist_ok=True)
        self.runtime = self.home / installer.RUNTIME_RELATIVE
        self.coord_home = self.home / installer.COORD_RELATIVE
        self.claude = self.home / ".claude.json"
        self.settings = self.home / ".claude" / "settings.json"
        self.codex = self.home / ".codex" / "config.toml"
        self.pin_patch = mock.patch.object(installer, "pin_python", return_value=self.python)
        self.pin_patch.start()
        self.dpapi_patch = mock.patch.object(installer, "protect_private_bytes", side_effect=_protect_or_skip)
        self.dpapi_patch.start()
        self.dpapi_unpatch = mock.patch.object(installer, "unprotect_private_bytes", side_effect=_unprotect_or_skip)
        self.dpapi_unpatch.start()

    def tearDown(self) -> None:
        self.dpapi_unpatch.stop()
        self.dpapi_patch.stop()
        self.pin_patch.stop()
        self.temp.cleanup()

    def install(self) -> dict[str, object]:
        return installer.install(self.home, self.appdata, self.state, self.python)

    def verify(self) -> dict[str, object]:
        """The read-only standalone surface, the only tolerant ``verify_install``."""
        return installer.verify_install(
            self.home, self.appdata, self.state, self.python, strict=False
        )

    def receipt(self) -> dict[str, object]:
        receipts = list((self.state / "transactions").glob("*/receipt.json"))
        self.assertEqual(len(receipts), 1)
        return json.loads(receipts[0].read_text(encoding="utf-8"))

    def transaction_ids(self) -> list[str]:
        transactions = self.state / "transactions"
        if not transactions.is_dir():
            return []
        return sorted(path.name for path in transactions.iterdir() if path.is_dir())

    def config_paths(self) -> dict[str, Path]:
        return {
            "claude-mcp": self.claude,
            "claude-settings": self.settings,
            "codex-mcp": self.codex,
        }

    def config_bytes(self) -> dict[str, bytes]:
        return {key: path.read_bytes() for key, path in self.config_paths().items()}

    def add_foreign_field(self, identifier: str) -> None:
        apply_drift = {
            "claude-mcp": self.add_foreign_claude_mcp_field,
            "claude-settings": self.add_foreign_claude_settings_field,
            "codex-mcp": self.add_foreign_codex_field,
        }[identifier]
        apply_drift()

    def report_for(self, result: dict[str, object], identifier: str) -> dict[str, object]:
        rows = {str(row["id"]): row for row in result["targets"]}  # type: ignore[index]
        self.assertIn(identifier, rows)
        return rows[identifier]

    def owned_claude_server(self) -> dict[str, object]:
        return installer.claude_server(self.python, self.runtime, self.coord_home)

    def add_foreign_claude_mcp_field(self) -> None:
        payload = json.loads(self.claude.read_text(encoding="utf-8"))
        payload["skillUsage"] = {installer.PACKAGE_ID: {"synthetic": "foreign-metadata"}}
        self.claude.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def add_foreign_claude_settings_field(self) -> None:
        payload = json.loads(self.settings.read_text(encoding="utf-8"))
        payload["theme"] = "synthetic-dark"
        self.settings.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def add_foreign_codex_field(self) -> None:
        data = self.codex.read_bytes()
        self.codex.write_bytes(data + b'\n[foreignSection]\nlabel = "synthetic-foreign"\n')

    def assert_foreign_current(self, result: dict[str, object], identifier: str) -> None:
        self.assertEqual(result["status"], "CURRENT", result.get("diagnostics"))
        self.assertEqual(result["unrelated_config_drift"], [identifier])
        report = self.report_for(result, identifier)
        self.assertIs(report["passed"], True)
        self.assertIs(report["whole_file_equal"], False)
        self.assertIs(report["unrelated_field_drift"], True)
        self.assertIs(report["owned_semantics_current"], True)
        self.assertNotEqual(report["current"]["sha256"], report["expected"]["sha256"])

    def test_foreign_claude_skill_usage_field_stays_current(self) -> None:
        self.install()
        self.add_foreign_claude_mcp_field()
        result = self.verify()
        self.assert_foreign_current(result, "claude-mcp")
        payload = json.loads(self.claude.read_text(encoding="utf-8"))
        self.assertEqual(payload["mcpServers"][installer.PACKAGE_ID], self.owned_claude_server())
        self.assertIn("skillUsage", payload)
        for identifier in ("claude-settings", "codex-mcp"):
            report = self.report_for(result, identifier)
            self.assertIs(report["passed"], True)
            self.assertNotIn("whole_file_equal", report)
            self.assertNotIn("unrelated_field_drift", report)

    def test_foreign_claude_settings_field_stays_current(self) -> None:
        self.install()
        self.add_foreign_claude_settings_field()
        result = self.verify()
        self.assert_foreign_current(result, "claude-settings")
        payload = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(payload["env"]["MCP_TOOL_TIMEOUT"], "100000000")
        self.assertEqual(payload["theme"], "synthetic-dark")

    def test_foreign_codex_field_stays_current(self) -> None:
        self.install()
        self.add_foreign_codex_field()
        result = self.verify()
        self.assert_foreign_current(result, "codex-mcp")
        data = self.codex.read_bytes()
        self.assertIn(b"[mcp_servers.agent-office]", data)
        self.assertIn(b"tool_timeout_sec = 300", data)
        self.assertIn(b"[foreignSection]", data)

    def test_foreign_metadata_in_all_three_targets_reports_non_equality(self) -> None:
        self.install()
        self.add_foreign_claude_mcp_field()
        self.add_foreign_claude_settings_field()
        self.add_foreign_codex_field()
        result = self.verify()
        self.assertEqual(result["status"], "CURRENT", result.get("diagnostics"))
        self.assertEqual(
            sorted(result["unrelated_config_drift"]),
            sorted(["claude-mcp", "claude-settings", "codex-mcp"]),
        )
        for identifier in ("claude-mcp", "claude-settings", "codex-mcp"):
            report = self.report_for(result, identifier)
            self.assertIs(report["passed"], True)
            self.assertIs(report["whole_file_equal"], False)
            self.assertIs(report["unrelated_field_drift"], True)
            self.assertNotEqual(report["current"]["sha256"], report["expected"]["sha256"])
        for identifier, _, _, _ in installer.SKILL_TARGETS:
            report = self.report_for(result, identifier)
            self.assertIs(report["passed"], True)
            self.assertNotIn("whole_file_equal", report)
        runtime_report = self.report_for(result, "runtime")
        self.assertIs(runtime_report["passed"], True)
        self.assertNotIn("whole_file_equal", runtime_report)

    def restore_config(self, installed: dict[str, bytes]) -> None:
        paths = self.config_paths()
        for identifier, data in installed.items():
            paths[identifier].write_bytes(data)

    def test_owned_semantic_breach_still_fails_verify(self) -> None:
        self.install()
        installed = {
            "claude-mcp": self.claude.read_bytes(),
            "claude-settings": self.settings.read_bytes(),
            "codex-mcp": self.codex.read_bytes(),
        }
        self.assertEqual(self.verify()["status"], "CURRENT")

        def mutate_json(path: Path, transform) -> None:
            payload = json.loads(path.read_text(encoding="utf-8"))
            transform(payload)
            path.write_bytes(json.dumps(payload, indent=2).encode("utf-8"))

        cases = {
            "claude-mcp-command-tampered": lambda: mutate_json(
                self.claude,
                lambda p: p["mcpServers"].__setitem__(
                    installer.PACKAGE_ID,
                    {**p["mcpServers"][installer.PACKAGE_ID], "command": "synthetic-tampered"},
                ),
            ),
            "claude-mcp-entry-removed": lambda: mutate_json(
                self.claude, lambda p: p["mcpServers"].pop(installer.PACKAGE_ID)
            ),
            "claude-mcp-servers-missing": lambda: self.claude.write_bytes(b'{"foreign": 1}\n'),
            "claude-settings-timeout-lowered": lambda: mutate_json(
                self.settings, lambda p: p["env"].__setitem__("MCP_TOOL_TIMEOUT", "1")
            ),
            "claude-settings-timeout-removed": lambda: mutate_json(
                self.settings, lambda p: p["env"].pop("MCP_TOOL_TIMEOUT")
            ),
            "codex-mcp-timeout-tampered": lambda: self.codex.write_bytes(
                installed["codex-mcp"].replace(b"tool_timeout_sec = 300", b"tool_timeout_sec = 7")
            ),
            "codex-mcp-entry-removed": lambda: self.codex.write_bytes(
                b'model = "synthetic-foreign"\n'
            ),
        }
        for label, apply_mutation in cases.items():
            with self.subTest(label=label):
                self.restore_config(installed)
                apply_mutation()
                self.assertEqual(self.verify()["status"], "DRIFT")

    def test_deleted_owned_config_fails_verify(self) -> None:
        self.install()
        for path in (self.claude, self.settings, self.codex):
            with self.subTest(path=path.name):
                original = path.read_bytes()
                path.unlink()
                self.assertEqual(self.verify()["status"], "DRIFT")
                path.write_bytes(original)
        self.assertEqual(self.verify()["status"], "CURRENT")

    def test_duplicate_and_malformed_json_fails_verify(self) -> None:
        self.install()
        entry = json.dumps(self.owned_claude_server())
        duplicate = (
            '{"mcpServers": {"agent-office": ' + entry + '}, '
            '"mcpServers": {"synthetic-foreign": {}}}'
        ).encode("utf-8")
        cases = {
            "duplicate-mcpServers-key": duplicate,
            "truncated-json": b'{"mcpServers": {"agent-office": ',
            "trailing-content": b'{"mcpServers": {"agent-office": {}}} {"extra": 1}',
        }
        for label, data in cases.items():
            with self.subTest(label=label):
                self.claude.write_bytes(data)
                self.assertEqual(self.verify()["status"], "DRIFT")

    def test_malformed_codex_toml_fails_verify(self) -> None:
        self.install()
        self.codex.write_bytes(b"this is not valid toml =\n[[unclosed\n")
        self.assertEqual(self.verify()["status"], "DRIFT")

    def test_stray_runtime_bytecode_is_ignored_under_config_tolerance(self) -> None:
        """Config tolerance stays scoped to the owned configs and never widens to payload.

        A stray bytecode file is ignored because it is interpreter output, not
        because tolerance is loose: the runtime tree is still compared exactly.
        """
        self.install()
        self.add_foreign_claude_mcp_field()
        self.assertEqual(self.verify()["status"], "CURRENT")
        stray = self.runtime / "coordfs" / "__pycache__" / "office.cpython-999.pyc"
        stray.parent.mkdir(parents=True)
        stray.write_bytes(b"synthetic-cache-artifact")
        result = self.verify()
        self.assertEqual(result["status"], "CURRENT")
        self.assertIs(result["passed"], True)
        self.assertEqual(result["unrelated_config_drift"], ["claude-mcp"])
        os.replace(stray, self.root / "synthetic-cache-artifact.moved")
        self.assertEqual(self.verify()["status"], "CURRENT")

    def test_rollback_refuses_foreign_drift_and_preserves_bytes(self) -> None:
        deployed = self.install()
        transaction_id = str(deployed["transaction_id"])
        self.add_foreign_claude_mcp_field()
        self.assertEqual(self.verify()["status"], "CURRENT")
        foreign_bytes = self.claude.read_bytes()
        runtime_before = installer.snapshot(self.runtime)
        with self.assertRaises(installer.DeployError):
            installer.rollback(self.home, self.appdata, self.state, transaction_id)
        self.assertEqual(self.claude.read_bytes(), foreign_bytes)
        self.assertEqual(installer.snapshot(self.runtime), runtime_before)
        self.assertTrue(self.runtime.exists())
        self.assertEqual(self.receipt()["status"], "DEPLOYED")

    def test_recover_refuses_foreign_drift_and_preserves_bytes(self) -> None:
        deployed = self.install()
        transaction_id = str(deployed["transaction_id"])
        self.add_foreign_claude_mcp_field()
        foreign_bytes = self.claude.read_bytes()
        with self.assertRaises(installer.DeployError):
            installer.recover(
                self.home, self.appdata, self.state, transaction_id, self.python
            )
        self.assertEqual(self.claude.read_bytes(), foreign_bytes)
        self.assertTrue(self.runtime.exists())
        self.assertEqual(self.verify()["status"], "CURRENT")

    def test_clean_install_rollback_and_recover_are_byte_exact(self) -> None:
        preimage = b'{\n  "seed": "preimage"\n}\n'
        self.claude.write_bytes(preimage)
        deployed = self.install()
        transaction_id = str(deployed["transaction_id"])
        current = self.verify()
        self.assertEqual(current["status"], "CURRENT")
        self.assertEqual(current["unrelated_config_drift"], [])
        rolled = installer.rollback(self.home, self.appdata, self.state, transaction_id)
        self.assertEqual(rolled["status"], "ROLLED_BACK")
        self.assertFalse(self.runtime.exists())
        self.assertEqual(self.claude.read_bytes(), preimage)
        self.assertEqual(self.verify()["status"], "NOT_INSTALLED")
        recovered = installer.recover(
            self.home, self.appdata, self.state, transaction_id, self.python
        )
        self.assertEqual(recovered["status"], "DEPLOYED")
        self.assertEqual(self.verify()["status"], "CURRENT")
        self.assertEqual(self.verify()["unrelated_config_drift"], [])

    def test_repeat_install_without_drift_is_idempotent(self) -> None:
        first = self.install()
        transaction_id = str(first["transaction_id"])
        installed = self.config_bytes()
        runtime_before = installer.snapshot(self.runtime)
        second = self.install()
        self.assertEqual(second["status"], "CURRENT")
        self.assertEqual(second["transaction_id"], transaction_id)
        self.assertEqual(second["unrelated_config_drift"], [])
        self.assertEqual(self.transaction_ids(), [transaction_id])
        self.assertEqual(self.config_bytes(), installed)
        self.assertEqual(installer.snapshot(self.runtime), runtime_before)
        self.assertEqual(self.receipt()["status"], "DEPLOYED")
        self.assertEqual(self.verify()["status"], "CURRENT")

    def test_repeat_install_refuses_each_foreign_owned_config_field(self) -> None:
        deployed = self.install()
        transaction_id = str(deployed["transaction_id"])
        for identifier in ("claude-mcp", "claude-settings", "codex-mcp"):
            with self.subTest(target=identifier):
                installed = self.config_bytes()
                runtime_before = installer.snapshot(self.runtime)
                self.add_foreign_field(identifier)
                foreign = self.config_bytes()
                self.assertNotEqual(foreign[identifier], installed[identifier])
                with self.assertRaises(installer.DeployError):
                    self.install()
                live = self.config_bytes()
                self.assertEqual(live[identifier], foreign[identifier], identifier)
                for other, data in installed.items():
                    if other != identifier:
                        self.assertEqual(live[other], data, other)
                self.assertEqual(installer.snapshot(self.runtime), runtime_before)
                self.assertEqual(self.transaction_ids(), [transaction_id])
                self.assertEqual(self.receipt()["status"], "DEPLOYED")
                self.assert_foreign_current(self.verify(), identifier)
                self.restore_config(installed)
                self.assertEqual(self.install()["status"], "CURRENT")

    def test_repeat_install_refuses_foreign_metadata_in_all_three_owned_configs(self) -> None:
        deployed = self.install()
        transaction_id = str(deployed["transaction_id"])
        installed = self.config_bytes()
        runtime_before = installer.snapshot(self.runtime)
        for identifier in ("claude-mcp", "claude-settings", "codex-mcp"):
            self.add_foreign_field(identifier)
        foreign = self.config_bytes()
        for identifier in installed:
            self.assertNotEqual(foreign[identifier], installed[identifier], identifier)
        for attempt in (1, 2):
            with self.subTest(attempt=attempt):
                with self.assertRaises(installer.DeployError):
                    self.install()
                self.assertEqual(self.config_bytes(), foreign)
                self.assertEqual(installer.snapshot(self.runtime), runtime_before)
                self.assertEqual(self.transaction_ids(), [transaction_id])
                self.assertEqual(self.receipt()["status"], "DEPLOYED")
        result = self.verify()
        self.assertEqual(result["status"], "CURRENT", result.get("diagnostics"))
        self.assertEqual(
            sorted(result["unrelated_config_drift"]),
            sorted(["claude-mcp", "claude-settings", "codex-mcp"]),
        )
        for identifier in ("claude-mcp", "claude-settings", "codex-mcp"):
            report = self.report_for(result, identifier)
            self.assertIs(report["passed"], True)
            self.assertIs(report["whole_file_equal"], False)
            self.assertIs(report["unrelated_field_drift"], True)
        self.restore_config(installed)
        self.assertEqual(self.verify()["unrelated_config_drift"], [])
        self.assertEqual(self.install()["status"], "CURRENT")

    def test_repeated_install_rollback_and_recover_stay_safe_under_foreign_drift(self) -> None:
        preimage = b'{\n  "seed": "preimage"\n}\n'
        self.claude.write_bytes(preimage)
        deployed = self.install()
        transaction_id = str(deployed["transaction_id"])
        postimage = self.claude.read_bytes()
        self.assertEqual(self.install()["status"], "CURRENT")
        rolled = installer.rollback(self.home, self.appdata, self.state, transaction_id)
        self.assertEqual(rolled["status"], "ROLLED_BACK")
        self.assertEqual(self.claude.read_bytes(), preimage)
        recovered = installer.recover(
            self.home, self.appdata, self.state, transaction_id, self.python
        )
        self.assertEqual(recovered["status"], "DEPLOYED")
        self.assertEqual(self.claude.read_bytes(), postimage)
        self.assertEqual(self.install()["status"], "CURRENT")
        self.assertEqual(self.transaction_ids(), [transaction_id])
        runtime_before = installer.snapshot(self.runtime)
        self.add_foreign_field("claude-mcp")
        foreign = self.claude.read_bytes()
        actions = {
            "install": lambda: self.install(),
            "rollback": lambda: installer.rollback(
                self.home, self.appdata, self.state, transaction_id
            ),
            "recover": lambda: installer.recover(
                self.home, self.appdata, self.state, transaction_id, self.python
            ),
        }
        for action, call in actions.items():
            with self.subTest(action=action):
                with self.assertRaises(installer.DeployError):
                    call()
                self.assertEqual(self.claude.read_bytes(), foreign)
                self.assertEqual(installer.snapshot(self.runtime), runtime_before)
                self.assertEqual(self.transaction_ids(), [transaction_id])
                self.assertEqual(self.receipt()["status"], "DEPLOYED")
                self.assert_foreign_current(self.verify(), "claude-mcp")
        self.claude.write_bytes(postimage)
        self.assertEqual(self.verify()["unrelated_config_drift"], [])
        rolled = installer.rollback(self.home, self.appdata, self.state, transaction_id)
        self.assertEqual(rolled["status"], "ROLLED_BACK")
        self.assertEqual(self.claude.read_bytes(), preimage)
        recovered = installer.recover(
            self.home, self.appdata, self.state, transaction_id, self.python
        )
        self.assertEqual(recovered["status"], "DEPLOYED")
        self.assertEqual(self.claude.read_bytes(), postimage)
        self.assertEqual(self.install()["status"], "CURRENT")
        self.assertEqual(self.transaction_ids(), [transaction_id])

    def test_install_compensates_when_foreign_bytes_landed_after_activation(self) -> None:
        self.claude.write_bytes(b'{\n  "seed": "preimage"\n}\n')
        original_validate = installer.validate_config_semantics
        fired = False

        def validate_then_write_foreign(receipt: dict[str, object], home: Path) -> None:
            nonlocal fired
            result = original_validate(receipt, home)
            if not fired:
                fired = True
                payload = json.loads(self.claude.read_text(encoding="utf-8"))
                payload["skillUsage"] = {installer.PACKAGE_ID: {"synthetic": "raced-write"}}
                self.claude.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            return result

        with mock.patch.object(
            installer, "validate_config_semantics", side_effect=validate_then_write_foreign
        ):
            with self.assertRaises(installer.TransitionFailure) as caught:
                self.install()
        self.assertTrue(fired)
        self.assertIn("claude-mcp", caught.exception.drift_ids)
        payload = json.loads(self.claude.read_text(encoding="utf-8"))
        self.assertEqual(payload["skillUsage"], {installer.PACKAGE_ID: {"synthetic": "raced-write"}})
        self.assertFalse(self.runtime.exists())
        for identifier, _, relative, _ in installer.SKILL_TARGETS:
            self.assertFalse((self.home / relative).exists(), identifier)
        self.assertEqual(self.receipt()["status"], "FAILED")
        self.assertEqual(self.verify()["status"], "NOT_INSTALLED")

    def test_installer_source_binds_tolerance_to_read_only_surfaces(self) -> None:
        tree = ast.parse(Path(installer.__file__).read_text(encoding="utf-8"))
        mutating = {"install", "recover"}
        read_only = {"dry_run", "main"}
        strict_default: object = None
        flagged_calls: dict[str, list[bool]] = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if node.name == "verify_install":
                keyword_names = [argument.arg for argument in node.args.kwonlyargs]
                self.assertIn("strict", keyword_names)
                default = node.args.kw_defaults[keyword_names.index("strict")]
                self.assertIsInstance(default, ast.Constant)
                strict_default = default.value
            if node.name in mutating | read_only:
                calls: list[bool] = []
                for inner in ast.walk(node):
                    if not isinstance(inner, ast.Call):
                        continue
                    if getattr(inner.func, "id", "") != "verify_install":
                        continue
                    keyword = next(
                        (item for item in inner.keywords if item.arg == "strict"), None
                    )
                    self.assertIsNotNone(
                        keyword, f"{node.name} calls verify_install without an explicit strict="
                    )
                    self.assertIsInstance(keyword.value, ast.Constant)  # type: ignore[union-attr]
                    calls.append(bool(keyword.value.value))  # type: ignore[union-attr]
                flagged_calls[node.name] = calls
        self.assertIs(strict_default, True)
        for name in mutating:
            self.assertTrue(flagged_calls.get(name), f"{name} performs no strict verification")
            self.assertEqual(set(flagged_calls[name]), {True}, name)
        for name in read_only:
            self.assertIn(False, flagged_calls.get(name, []), name)


class BytecodeCacheDriftScopeTests(unittest.TestCase):
    """Using Office must not block upgrading it; real payload drift still must.

    Measured 2026-09-20 in production: after one day of normal use
    ``install_agent_office.py upgrade`` refused with "Upgrade target drift blocks
    the transaction: runtime" because Python had written nine ``.pyc`` files into
    ``coordfs/__pycache__`` - 30 live files against a 21-file receipt, and the
    bytecode was the only difference. The Fleet package installer already filters
    those names in its own ``tree_manifest``. Bytecode is now ignored rather than
    deleted here, and every file the package owns still blocks the transaction
    when it is changed or removed.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="agent-office-cache-scope-")
        self.root = Path(self.temp.name)
        self.home = self.root / "Synthetic User Home With Spaces"
        self.appdata = self.home / "AppData" / "Roaming"
        self.state = self.home / ".agents" / "agent-office-deploy"
        self.python = self.home / "Python Runtime With Spaces" / "python.exe"
        self.python.parent.mkdir(parents=True)
        self.python.write_bytes(b"synthetic-python-fixture")
        self.home.mkdir(exist_ok=True)
        self.runtime = self.home / installer.RUNTIME_RELATIVE
        self.pin_patch = mock.patch.object(installer, "pin_python", return_value=self.python)
        self.pin_patch.start()
        self.dpapi_patch = mock.patch.object(installer, "protect_private_bytes", side_effect=_protect_or_skip)
        self.dpapi_patch.start()
        self.dpapi_unpatch = mock.patch.object(installer, "unprotect_private_bytes", side_effect=_unprotect_or_skip)
        self.dpapi_unpatch.start()

    def tearDown(self) -> None:
        self.dpapi_unpatch.stop()
        self.dpapi_patch.stop()
        self.pin_patch.stop()
        self.temp.cleanup()

    def install(self) -> dict[str, object]:
        return installer.install(self.home, self.appdata, self.state, self.python)

    def verify(self) -> dict[str, object]:
        return installer.verify_install(self.home, self.appdata, self.state, self.python, strict=False)

    def transaction_ids(self) -> list[str]:
        transactions = self.state / "transactions"
        if not transactions.is_dir():
            return []
        return sorted(path.name for path in transactions.iterdir() if path.is_dir())

    def write_cache(self) -> list[Path]:
        cache = self.runtime / "coordfs" / "__pycache__"
        cache.mkdir(parents=True)
        written: list[Path] = []
        for name in ("office.cpython-313.pyc", "office.cpython-313.pyo"):
            target = cache / name
            target.write_bytes(b"synthetic-bytecode-cache")
            written.append(target)
        return written

    def test_interpreter_cache_is_not_drift_and_does_not_block_upgrade(self) -> None:
        deployed = self.install()
        self.assertEqual(deployed["status"], "DEPLOYED")
        snapshot_before = installer.snapshot(self.runtime)
        self.assertTrue(self.write_cache())
        self.assertEqual(installer.snapshot(self.runtime), snapshot_before)
        self.assertEqual(self.verify()["status"], "CURRENT")

        upgraded = installer.upgrade(
            self.home, self.appdata, self.state, str(deployed["transaction_id"]), self.python
        )
        self.assertEqual(upgraded["status"], "DEPLOYED")
        self.assertEqual(self.verify()["status"], "CURRENT")

    def test_changed_managed_file_still_blocks_upgrade(self) -> None:
        deployed = self.install()
        self.write_cache()
        snapshot_before = installer.snapshot(self.runtime)
        managed = self.runtime / "coordfs" / "hashing.py"
        managed.write_bytes(managed.read_bytes() + b"\n# synthetic external drift\n")
        self.assertNotEqual(installer.snapshot(self.runtime), snapshot_before)
        self.assertEqual(self.verify()["status"], "DRIFT")
        with self.assertRaises(installer.DeployError) as caught:
            installer.upgrade(
                self.home, self.appdata, self.state, str(deployed["transaction_id"]), self.python
            )
        self.assertIn("drift", str(caught.exception).lower())
        self.assertEqual(self.transaction_ids(), [str(deployed["transaction_id"])])

    def test_removed_managed_file_still_blocks_upgrade(self) -> None:
        deployed = self.install()
        self.write_cache()
        snapshot_before = installer.snapshot(self.runtime)
        managed = self.runtime / "coordfs" / "hashing.py"
        # Moved out of the tree rather than deleted: this test must not add a cleanup step.
        os.replace(managed, self.root / "removed-managed-file.py")
        self.assertNotEqual(installer.snapshot(self.runtime), snapshot_before)
        self.assertEqual(self.verify()["status"], "DRIFT")
        with self.assertRaises(installer.DeployError):
            installer.upgrade(
                self.home, self.appdata, self.state, str(deployed["transaction_id"]), self.python
            )
        self.assertEqual(self.transaction_ids(), [str(deployed["transaction_id"])])


class SkillPinReportTests(unittest.TestCase):
    """A changed SKILL.md invalidates the ownership pin; the installer reports, never repins.

    ``skills-ownership.json`` pins the derived skill fingerprint in
    ``marker_fingerprint`` and ``receipt_fingerprint``. Every edit of
    ``skill/agent-office/SKILL.md`` changes that value and
    ``test_dedicated_manager_contract_matches_derived_skill_source`` then fails.
    On 2026-09-20 that blocked a task three times in a row: the pin lives in
    another layer, outside the executor's write scope, so it could neither be
    fixed nor even recognised. ``skill-pin`` names the stale fields and the exact
    value to write, and never writes the file.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="agent-office-skill-pin-")
        self.addCleanup(self.temp.cleanup)
        self.records = Path(self.temp.name) / "skills-ownership.json"
        self.derived = installer.verify_package().skill_fingerprint

    def write_records(self, pin: str) -> None:
        self.records.write_text(
            json.dumps(
                {
                    "manager_contracts": {
                        installer.MANAGED_BY: {
                            "marker_fingerprint": pin,
                            "receipt_fingerprint": pin,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

    def test_stale_pin_is_reported_with_the_value_to_write(self) -> None:
        self.write_records("0" * 64)
        before = self.records.read_bytes()
        report = installer.skill_pin_report(self.records)
        self.assertEqual(report["status"], "STALE_PIN")
        self.assertIs(report["passed"], False)
        self.assertEqual(report["derived_skill_fingerprint"], self.derived)
        self.assertEqual(report["value_to_pin"], self.derived)
        self.assertEqual(report["stale_pin_fields"], ["marker_fingerprint", "receipt_fingerprint"])
        self.assertEqual(
            report["remediation"]["fields"],
            {"marker_fingerprint": self.derived, "receipt_fingerprint": self.derived},
        )
        self.assertIn(self.derived, json.dumps(report["remediation"], sort_keys=True))
        self.assertIs(report["ownership_written"], False)
        self.assertEqual(self.records.read_bytes(), before)

    def test_current_pin_reports_current(self) -> None:
        self.write_records(self.derived)
        report = installer.skill_pin_report(self.records)
        self.assertEqual(report["status"], "CURRENT")
        self.assertIs(report["passed"], True)
        self.assertEqual(report["stale_pin_fields"], [])
        self.assertEqual(report["pinned"]["marker_fingerprint"], self.derived)

    def test_missing_records_are_reported_without_raising(self) -> None:
        self.assertFalse(self.records.exists())
        report = installer.skill_pin_report(self.records)
        self.assertEqual(report["status"], "UNKNOWN")
        self.assertIs(report["passed"], False)
        self.assertEqual(report["ownership_state"], "unreadable")
        self.assertIs(report["ownership_written"], False)
        self.assertFalse(self.records.exists())

    def test_unreadable_records_are_reported_without_raising(self) -> None:
        self.records.write_text("{ this is not json", encoding="utf-8")
        report = installer.skill_pin_report(self.records)
        self.assertEqual(report["status"], "UNKNOWN")
        self.assertIs(report["passed"], False)

    def test_records_without_the_manager_contract_are_reported_without_raising(self) -> None:
        self.records.write_text(json.dumps({"manager_contracts": {}}), encoding="utf-8")
        report = installer.skill_pin_report(self.records)
        self.assertEqual(report["status"], "UNKNOWN")
        self.assertEqual(report["ownership_state"], "missing-manager-contract")

    def test_default_path_is_the_repo_ownership_file_and_is_never_written(self) -> None:
        self.assertEqual(
            os.path.normcase(str(installer.ownership_records_path())),
            os.path.normcase(str(OWNERSHIP_RECORDS)),
        )
        if not OWNERSHIP_RECORDS.is_file():
            self.skipTest("shared-skills ownership records are absent in this checkout")
        before = OWNERSHIP_RECORDS.read_bytes()
        report = installer.skill_pin_report()
        self.assertIs(report["ownership_written"], False)
        self.assertEqual(OWNERSHIP_RECORDS.read_bytes(), before)

    def test_skill_pin_command_reports_and_writes_nothing(self) -> None:
        self.write_records("0" * 64)
        before = self.records.read_bytes()
        with (
            mock.patch.object(installer, "ownership_records_path", return_value=self.records),
            mock.patch.object(sys, "stdout", io.StringIO()) as stdout,
        ):
            code = installer.main(["skill-pin"])
        self.assertEqual(code, 1)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["action"], "skill-pin")
        self.assertEqual(payload["status"], "STALE_PIN")
        self.assertEqual(payload["value_to_pin"], self.derived)
        self.assertEqual(self.records.read_bytes(), before)

    def test_skill_pin_never_resolves_a_deployment_home(self) -> None:
        with (
            mock.patch.object(
                installer, "cli_paths", side_effect=AssertionError("skill-pin resolved a deployment home")
            ),
            mock.patch.object(
                installer, "skill_pin_report", return_value={"action": "skill-pin", "status": "CURRENT"}
            ),
            mock.patch.object(sys, "stdout", io.StringIO()),
        ):
            self.assertEqual(installer.main(["skill-pin"]), 0)


class SealPackageTests(unittest.TestCase):
    """`seal` is the source-side build step that refreshes package.json digests.

    Before it existed, any edit under coordfs/ left the manifest stale and the
    installer suite failed with ~40 `Package source hash/size mismatch` errors,
    which read as "the change broke the office" rather than "the manifest is old".
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "package"
        (self.root / "skill" / "agent-office").mkdir(parents=True)
        (self.root / "runtime.py").write_bytes(b"runtime-v1")
        (self.root / "skill" / "agent-office" / "SKILL.md").write_bytes(b"skill-v1")
        self.manifest_path = self.root / "package.json"
        self.manifest_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "package_id": installer.PACKAGE_ID,
                    "package_version": installer.PACKAGE_VERSION,
                    "runtime_version": "synthetic",
                    "runtime_fingerprint": "0" * 64,
                    "skill_fingerprint": "0" * 64,
                    "runtime": [
                        {"source": "runtime.py", "target": "runtime.py", "bytes": 0, "sha256": "0" * 64}
                    ],
                    "skill": [
                        {
                            "source": "skill/agent-office/SKILL.md",
                            "target": "SKILL.md",
                            "bytes": 0,
                            "sha256": "0" * 64,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def patched(self) -> Any:
        return mock.patch.multiple(
            installer,
            SOURCE_ROOT=self.root,
            PACKAGE_PATH=self.manifest_path,
            RUNTIME_SOURCE_MAP=(("runtime.py", "runtime.py"),),
            SKILL_SOURCE_MAP=(("skill/agent-office/SKILL.md", "SKILL.md"),),
        )

    def test_seal_refreshes_stale_digests_and_verify_then_passes(self) -> None:
        with self.patched():
            with self.assertRaises(installer.DeployError):
                installer.verify_package()
            result = installer.seal_package()
            self.assertEqual(result["status"], "READY")
            self.assertEqual(
                result["resealed_sources"], ["runtime.py", "skill/agent-office/SKILL.md"]
            )
            installer.verify_package()

    def test_seal_is_idempotent_and_byte_stable(self) -> None:
        with self.patched():
            installer.seal_package()
            first = self.manifest_path.read_bytes()
            second_result = installer.seal_package()
        self.assertEqual(second_result["status"], "CURRENT")
        self.assertEqual(second_result["resealed_sources"], [])
        self.assertEqual(self.manifest_path.read_bytes(), first)

    def test_seal_cannot_add_a_file_that_is_not_on_the_installer_allowlist(self) -> None:
        """The packaged file list is pinned in code, so sealing can never smuggle a file in."""
        (self.root / "smuggled.py").write_bytes(b"not-packaged")
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        manifest["runtime"].append(
            {
                "source": "smuggled.py",
                "target": "smuggled.py",
                "bytes": len(b"not-packaged"),
                "sha256": hashlib.sha256(b"not-packaged").hexdigest(),
            }
        )
        self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.patched():
            installer.seal_package()
            sealed = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual([row["source"] for row in sealed["runtime"]], ["runtime.py"])

    def test_seal_fails_closed_when_a_declared_source_is_missing(self) -> None:
        with self.patched():
            os.replace(self.root / "runtime.py", self.root / "runtime.py.moved")
            with self.assertRaises((installer.DeployError, OSError)):
                installer.seal_package()

    def test_seal_reports_the_skill_pin_comparison(self) -> None:
        """Sealing a changed SKILL.md also invalidates the ownership pin: say so."""
        with self.patched():
            result = installer.seal_package()
        pin = result["skill_pin"]
        self.assertEqual(pin["action"], "skill-pin")
        self.assertEqual(pin["derived_skill_fingerprint"], result["skill_fingerprint"])
        self.assertIs(pin["ownership_written"], False)
        self.assertIn(pin["status"], {"CURRENT", "STALE_PIN", "UNKNOWN"})

    def test_seal_never_resolves_a_deployment_home(self) -> None:
        """Sealing is source-side only; touching a real home would make it a deploy."""
        with (
            mock.patch.object(
                installer, "cli_paths", side_effect=AssertionError("seal resolved a deployment home")
            ),
            mock.patch.object(installer, "seal_package", return_value={"action": "seal", "status": "CURRENT"}),
            mock.patch.object(sys, "stdout", io.StringIO()),
        ):
            self.assertEqual(installer.main(["seal"]), 0)


if __name__ == "__main__":
    unittest.main()
