from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


# Resolve package root dynamically so the parent can copy this test module
# to an unchanged baseline (e.g. 0.9.131) for independent RED replay.
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PACKAGE_ROOT / "scripts"
INSTALLER_PATH = SCRIPTS_DIR / "install_router_suite.py"
VERIFIER_PATH = PACKAGE_ROOT / "payload" / "agent-fleet" / "scripts" / "verify_runtime.py"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if not spec or not spec.loader:
        raise ImportError(f"Cannot load {name} from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


installer = _load_module("install_router_suite", INSTALLER_PATH)
verifier = _load_module("verify_runtime", VERIFIER_PATH)

PROHIBITED_SUFFIXES = [
    ".bat",
    ".cmd",
    ".com",
    ".cjs",
    ".exe",
    ".js",
    ".mjs",
    ".ps1",
    ".py",
    ".sh",
]


class RuntimePytestCacheTests(unittest.TestCase):
    """Regression suite for benign pytest cache handling during Fleet runtime attestation.

    Operating constraints:
    - Tests must run outside the installed runtime: never place fixtures or run
      tests inside ~/.agents/runtime.
    - Use TEAM_TEST_ROOT, pointing it at a task-owned disposable fixture root
      outside the installed runtime, when a task needs a fixed test root.
    - All tests execute strictly inside disposable temporary directories created via tempfile.
    - Package paths are dynamically resolved from __file__.parents[1] for honest RED replay.
    """

    def make_test_tree(self) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "runtime"
        root.mkdir(parents=True, exist_ok=True)
        # Create minimal standard payload files
        (root / "module.py").write_text("def run():\n    return 42\n", encoding="utf-8")
        pkg = root / "pkg"
        pkg.mkdir(parents=True, exist_ok=True)
        (pkg / "submodule.py").write_text("VALUE = 100\n", encoding="utf-8")
        (pkg / "data.json").write_text('{"name": "test"}\n', encoding="ascii")
        return temporary, root

    def write_runtime_marker(
        self,
        root: Path,
        *,
        fingerprint: str,
        file_count: int,
        version: str = "0.9.132",
        managed_by: str = "agent-fleet-suite-v2-installer",
    ) -> Path:
        marker_path = root / verifier.RUNTIME_MARKER
        payload = {
            "schema_version": 1,
            "managed_by": managed_by,
            "package_id": "agent-fleet-suite",
            "package_version": version,
            "distribution_package_version": version,
            "package_fingerprint": "a" * 64,
            "payload_fingerprint": fingerprint,
            "payload_file_count": file_count,
            "transaction_id": "0123456789abcdef0123456789abcdef",
            "installed_at": "2026-10-23T18:00:00Z",
        }
        marker_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="ascii")
        return marker_path

    def test_benign_root_and_nested_pytest_cache_preserves_fingerprint(self) -> None:
        _, root = self.make_test_tree()
        base_rows_v, base_fp_v = verifier.runtime_manifest(root)
        # marker_names keeps the installer manifest aligned with the verifier,
        # which never includes the runtime marker file.
        base_rows_i, base_fp_i = installer.tree_manifest(
            root, marker_names={installer.RUNTIME_MARKER}
        )
        self.assertEqual(base_fp_v, base_fp_i)
        self.assertEqual(len(base_rows_v), 3)

        self.write_runtime_marker(
            root,
            fingerprint=base_fp_v,
            file_count=len(base_rows_v),
            version="0.9.132",
        )
        verify_result = verifier.verify(root, base_fp_v, "0.9.132")
        self.assertEqual(verify_result["status"], "PASS")

        # Add root benign pytest cache
        root_cache = root / ".pytest_cache" / "v" / "cache"
        root_cache.mkdir(parents=True, exist_ok=True)
        (root_cache / "nodeids").write_text('["test_a", "test_b"]\n', encoding="utf-8")
        (root / ".pytest_cache" / "CACHEDIR.TAG").write_text(
            "Signature: 8a477f31474f11042347000e9a4c5174\n", encoding="ascii"
        )
        (root / ".pytest_cache" / "README.md").write_text("# Pytest cache\n", encoding="utf-8")

        # Add nested benign pytest cache
        nested_cache = root / "pkg" / ".pytest_cache" / "v" / "cache"
        nested_cache.mkdir(parents=True, exist_ok=True)
        (nested_cache / "lastfailed").write_text('{"test_failed": true}\n', encoding="utf-8")
        (nested_cache / "stepwise").write_text("[]\n", encoding="ascii")

        # Add deep nested benign pytest cache
        deep_cache = root / "pkg" / "sub" / ".pytest_cache" / "v" / "cache"
        deep_cache.mkdir(parents=True, exist_ok=True)
        (deep_cache / "nodeids").write_text('["test_sub"]\n', encoding="utf-8")

        after_rows_v, after_fp_v = verifier.runtime_manifest(root)
        after_rows_i, after_fp_i = installer.tree_manifest(
            root, marker_names={installer.RUNTIME_MARKER}
        )

        self.assertEqual(after_fp_v, base_fp_v)
        self.assertEqual(after_fp_i, base_fp_i)
        self.assertEqual(after_rows_v, base_rows_v)
        self.assertEqual(after_rows_i, base_rows_i)

        verify_result_after = verifier.verify(root, base_fp_v, "0.9.132")
        self.assertEqual(verify_result_after["status"], "PASS")

    def test_cache_mixed_with_legacy_ignored_dirs_preserves_fingerprint(self) -> None:
        _, root = self.make_test_tree()
        base_rows_v, base_fp_v = verifier.runtime_manifest(root)
        # Capture the installer baseline before any cache or legacy ignored dir
        # is created, so the comparison below is against the same starting tree.
        base_rows_i, base_fp_i = installer.tree_manifest(
            root, marker_names={installer.RUNTIME_MARKER}
        )

        # Legacy __pycache__ and .ruff_cache
        pycache = root / "__pycache__"
        pycache.mkdir(parents=True, exist_ok=True)
        (pycache / "module.cpython-312.pyc").write_bytes(b"\x00\x00\x00\x00bytecode")

        ruff_cache = root / ".ruff_cache" / "content"
        ruff_cache.mkdir(parents=True, exist_ok=True)
        (ruff_cache / "cache.bin").write_bytes(b"ruff_cache_data")

        # .pytest_cache alongside legacy ignored dirs
        pytest_cache = root / ".pytest_cache" / "v" / "cache"
        pytest_cache.mkdir(parents=True, exist_ok=True)
        (pytest_cache / "nodeids").write_text('["test"]\n', encoding="utf-8")

        # Nested: .pytest_cache inside __pycache__ (benign)
        nested_in_pycache = pycache / ".pytest_cache"
        nested_in_pycache.mkdir(parents=True, exist_ok=True)
        (nested_in_pycache / "nodeids").write_text('["test_in_pycache"]\n', encoding="utf-8")

        # Nested: __pycache__ inside .pytest_cache (benign bytecode)
        pycache_in_pytest = root / ".pytest_cache" / "__pycache__"
        pycache_in_pytest.mkdir(parents=True, exist_ok=True)
        (pycache_in_pytest / "cached.pyc").write_bytes(b"\x00bytecode")

        mixed_rows_v, mixed_fp_v = verifier.runtime_manifest(root)
        mixed_rows_i, mixed_fp_i = installer.tree_manifest(
            root, marker_names={installer.RUNTIME_MARKER}
        )

        self.assertEqual(mixed_fp_v, base_fp_v)
        self.assertEqual(mixed_fp_i, base_fp_i)
        self.assertEqual(mixed_rows_v, base_rows_v)
        self.assertEqual(mixed_rows_i, base_rows_i)

    def test_prohibited_executable_suffixes_rejected_by_verifier(self) -> None:
        for suffix in PROHIBITED_SUFFIXES:
            for use_upper in (False, True):
                ext = suffix.upper() if use_upper else suffix
                with self.subTest(suffix=suffix, ext=ext, target="verifier"):
                    _, root = self.make_test_tree()
                    cache_dir = root / ".pytest_cache"
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    malicious = cache_dir / f"payload{ext}"
                    malicious.write_bytes(b"echo payload")

                    with self.assertRaisesRegex(
                        verifier.VerificationError,
                        "Executable source",
                    ):
                        verifier.runtime_manifest(root)

    def test_prohibited_executable_suffixes_rejected_by_installer(self) -> None:
        for suffix in PROHIBITED_SUFFIXES:
            for use_upper in (False, True):
                ext = suffix.upper() if use_upper else suffix
                with self.subTest(suffix=suffix, ext=ext, target="installer"):
                    _, root = self.make_test_tree()
                    cache_dir = root / ".pytest_cache"
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    malicious = cache_dir / f"payload{ext}"
                    malicious.write_bytes(b"echo payload")

                    with self.assertRaisesRegex(
                        installer.SuiteError,
                        "Executable source",
                    ):
                        installer.tree_files(root)

    def test_prohibited_executable_suffixes_rejected_in_nested_cache(self) -> None:
        for suffix in (".py", ".PY", ".sh", ".SH", ".exe", ".EXE", ".bat", ".BAT"):
            with self.subTest(suffix=suffix, location="nested_cache"):
                _, root = self.make_test_tree()
                nested_cache = root / "pkg" / "sub" / ".pytest_cache" / "v"
                nested_cache.mkdir(parents=True, exist_ok=True)
                malicious = nested_cache / f"script{suffix}"
                malicious.write_bytes(b"bad_code")

                with self.assertRaisesRegex(
                    verifier.VerificationError,
                    "Executable source",
                ):
                    verifier.runtime_manifest(root)

                with self.assertRaisesRegex(
                    installer.SuiteError,
                    "Executable source",
                ):
                    installer.tree_files(root)

    def test_prohibited_executable_in_pytest_cache_nested_in_pycache_rejected(self) -> None:
        # Avoid nested-cache bypass: __pycache__/.pytest_cache/malicious.py
        for suffix in (".py", ".PY", ".sh", ".SH", ".exe", ".EXE"):
            with self.subTest(suffix=suffix):
                _, root = self.make_test_tree()
                nested = root / "__pycache__" / ".pytest_cache"
                nested.mkdir(parents=True, exist_ok=True)
                malicious = nested / f"exploit{suffix}"
                malicious.write_bytes(b"exploit")

                with self.assertRaisesRegex(
                    verifier.VerificationError,
                    "Executable source",
                ):
                    verifier.runtime_manifest(root)

                with self.assertRaisesRegex(
                    installer.SuiteError,
                    "Executable source",
                ):
                    installer.tree_files(root)

    def test_prohibited_executable_in_pycache_nested_in_pytest_cache_rejected(self) -> None:
        # .pytest_cache/__pycache__/malicious.py
        for suffix in (".py", ".PY", ".bat", ".BAT"):
            with self.subTest(suffix=suffix):
                _, root = self.make_test_tree()
                nested = root / ".pytest_cache" / "__pycache__"
                nested.mkdir(parents=True, exist_ok=True)
                malicious = nested / f"exploit{suffix}"
                malicious.write_bytes(b"exploit")

                with self.assertRaisesRegex(
                    verifier.VerificationError,
                    "Executable source",
                ):
                    verifier.runtime_manifest(root)

                with self.assertRaisesRegex(
                    installer.SuiteError,
                    "Executable source",
                ):
                    installer.tree_files(root)

    def test_paths_outside_cache_including_prefix_lookalike_change_fingerprint(self) -> None:
        lookalikes = [
            ".pytest_cache_tamper",
            ".pytest_cache.backup",
            "pytest_cache",
            ".pytest_cache-fake",
            "prefix_.pytest_cache",
        ]
        for name in lookalikes:
            with self.subTest(lookalike_name=name):
                _, root = self.make_test_tree()
                _, base_fp_v = verifier.runtime_manifest(root)
                _, base_fp_i = installer.tree_manifest(
                    root, marker_names={installer.RUNTIME_MARKER}
                )

                tamper_dir = root / name
                tamper_dir.mkdir(parents=True, exist_ok=True)
                (tamper_dir / "data.txt").write_text("content\n", encoding="ascii")

                _, after_fp_v = verifier.runtime_manifest(root)
                _, after_fp_i = installer.tree_manifest(
                    root, marker_names={installer.RUNTIME_MARKER}
                )

                self.assertNotEqual(after_fp_v, base_fp_v)
                self.assertNotEqual(after_fp_i, base_fp_i)
                self.assertEqual(after_fp_v, after_fp_i)

    def test_normal_payload_and_marker_tamper_refusal(self) -> None:
        _, root = self.make_test_tree()
        _, expected_fp = verifier.runtime_manifest(root)
        self.write_runtime_marker(root, fingerprint=expected_fp, file_count=3)
        self.assertEqual(verifier.verify(root, expected_fp, "0.9.132")["status"], "PASS")

        # 1. Normal payload tamper
        (root / "module.py").write_text("def run():\n    return 'tampered'\n", encoding="utf-8")
        with self.assertRaisesRegex(
            verifier.VerificationError,
            "Runtime marker or payload fingerprint mismatch",
        ):
            verifier.verify(root, expected_fp, "0.9.132")

        # Restore payload
        (root / "module.py").write_text("def run():\n    return 42\n", encoding="utf-8")
        self.assertEqual(verifier.verify(root, expected_fp, "0.9.132")["status"], "PASS")

        # 2. Marker payload_fingerprint tamper
        marker_path = root / verifier.RUNTIME_MARKER
        marker_data = json.loads(marker_path.read_text(encoding="ascii"))
        marker_data["payload_fingerprint"] = "0" * 64
        marker_path.write_text(json.dumps(marker_data), encoding="ascii")
        with self.assertRaisesRegex(
            verifier.VerificationError,
            "Runtime marker or payload fingerprint mismatch",
        ):
            verifier.verify(root, expected_fp, "0.9.132")

        # 3. Marker payload_file_count tamper
        marker_data["payload_fingerprint"] = expected_fp
        marker_data["payload_file_count"] = 999
        marker_path.write_text(json.dumps(marker_data), encoding="ascii")
        with self.assertRaisesRegex(
            verifier.VerificationError,
            "Runtime marker or payload fingerprint mismatch",
        ):
            verifier.verify(root, expected_fp, "0.9.132")

        # 4. Marker package_version tamper
        marker_data["payload_file_count"] = 3
        marker_data["package_version"] = "0.0.1"
        marker_path.write_text(json.dumps(marker_data), encoding="ascii")
        with self.assertRaisesRegex(
            verifier.VerificationError,
            "Runtime marker or payload fingerprint mismatch",
        ):
            verifier.verify(root, expected_fp, "0.9.132")

        # 5. Marker managed_by tamper
        marker_data["package_version"] = "0.9.132"
        marker_data["managed_by"] = "unauthorized-installer"
        marker_path.write_text(json.dumps(marker_data), encoding="ascii")
        with self.assertRaisesRegex(
            verifier.VerificationError,
            "Runtime marker or payload fingerprint mismatch",
        ):
            verifier.verify(root, expected_fp, "0.9.132")

        # 6. Marker corrupted JSON
        marker_path.write_text("invalid json", encoding="ascii")
        with self.assertRaisesRegex(
            verifier.VerificationError,
            "Cannot read runtime marker",
        ):
            verifier.verify(root, expected_fp, "0.9.132")

        # 7. Marker not an object
        marker_path.write_text("[1, 2, 3]", encoding="ascii")
        with self.assertRaisesRegex(
            verifier.VerificationError,
            "Runtime marker is not an object",
        ):
            verifier.verify(root, expected_fp, "0.9.132")

        # 8. Marker missing
        marker_path.unlink()
        with self.assertRaisesRegex(
            verifier.VerificationError,
            "Cannot read runtime marker",
        ):
            verifier.verify(root, expected_fp, "0.9.132")

    def test_mocked_cache_root_link_refusal(self) -> None:
        _, root = self.make_test_tree()
        cache_dir = root / ".pytest_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "nodeids").write_text("[]\n", encoding="ascii")

        # Verifier: mock is_filesystem_link returning True at cache root
        real_v_link = verifier.is_filesystem_link

        def mock_v_link(p: Path) -> bool:
            if p == cache_dir:
                return True
            return real_v_link(p)

        with mock.patch.object(verifier, "is_filesystem_link", side_effect=mock_v_link):
            with self.assertRaisesRegex(
                verifier.VerificationError,
                "Linked runtime path is refused",
            ):
                verifier.runtime_manifest(root)

        # Installer: mock is_filesystem_link returning True at cache root
        real_i_link = installer.is_filesystem_link

        def mock_i_link(p: Path) -> bool:
            if p == cache_dir:
                return True
            return real_i_link(p)

        with mock.patch.object(installer, "is_filesystem_link", side_effect=mock_i_link):
            with self.assertRaisesRegex(
                installer.SuiteError,
                "Linked or reparse payload path is refused",
            ):
                installer.tree_files(root)

    def test_mocked_cache_nested_file_link_refusal(self) -> None:
        _, root = self.make_test_tree()
        cache_dir = root / ".pytest_cache" / "v" / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        target_file = cache_dir / "nodeids"
        target_file.write_text("[]\n", encoding="ascii")

        # Verifier: mock link specifically at the nested file
        real_v_link = verifier.is_filesystem_link

        def mock_v_file_link(p: Path) -> bool:
            if p == target_file:
                return True
            return real_v_link(p)

        with mock.patch.object(verifier, "is_filesystem_link", side_effect=mock_v_file_link):
            with self.assertRaisesRegex(
                verifier.VerificationError,
                "Linked runtime path is refused",
            ):
                verifier.runtime_manifest(root)

        # Installer: mock link specifically at the nested file
        real_i_link = installer.is_filesystem_link

        def mock_i_file_link(p: Path) -> bool:
            if p == target_file:
                return True
            return real_i_link(p)

        with mock.patch.object(installer, "is_filesystem_link", side_effect=mock_i_file_link):
            with self.assertRaisesRegex(
                installer.SuiteError,
                "Linked or reparse payload path is refused",
            ):
                installer.tree_files(root)

    def test_mocked_nested_cache_directory_link_refusal(self) -> None:
        _, root = self.make_test_tree()
        nested_cache_dir = root / "pkg" / ".pytest_cache"
        nested_cache_dir.mkdir(parents=True, exist_ok=True)
        (nested_cache_dir / "stepwise").write_text("[]\n", encoding="ascii")

        real_v_link = verifier.is_filesystem_link

        def mock_v_nested_dir(p: Path) -> bool:
            if p == nested_cache_dir:
                return True
            return real_v_link(p)

        with mock.patch.object(verifier, "is_filesystem_link", side_effect=mock_v_nested_dir):
            with self.assertRaisesRegex(
                verifier.VerificationError,
                "Linked runtime path is refused",
            ):
                verifier.runtime_manifest(root)

        real_i_link = installer.is_filesystem_link

        def mock_i_nested_dir(p: Path) -> bool:
            if p == nested_cache_dir:
                return True
            return real_i_link(p)

        with mock.patch.object(installer, "is_filesystem_link", side_effect=mock_i_nested_dir):
            with self.assertRaisesRegex(
                installer.SuiteError,
                "Linked or reparse payload path is refused",
            ):
                installer.tree_files(root)

    def test_mocked_windows_symlink_refusal(self) -> None:
        _, root = self.make_test_tree()
        cache_dir = root / ".pytest_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "nodeids").write_text("[]\n", encoding="ascii")

        real_is_symlink = Path.is_symlink

        def mock_symlink(self_path: Path) -> bool:
            if ".pytest_cache" in self_path.parts:
                return True
            return real_is_symlink(self_path)

        with mock.patch.object(Path, "is_symlink", mock_symlink):
            with self.assertRaisesRegex(
                verifier.VerificationError,
                "Linked runtime path is refused",
            ):
                verifier.runtime_manifest(root)

            with self.assertRaisesRegex(
                installer.SuiteError,
                "Linked or reparse payload path is refused",
            ):
                installer.tree_files(root)

    def test_real_junction_in_cache_refusal_if_supported(self) -> None:
        temporary, root = self.make_test_tree()
        external_target = Path(temporary.name) / "junction_target"
        external_target.mkdir(parents=True, exist_ok=True)
        (external_target / "nested.txt").write_text("external\n", encoding="ascii")

        junction_path = root / ".pytest_cache"

        created = False
        try:
            import _winapi

            _winapi.CreateJunction(str(external_target), str(junction_path))
            created = True
        except Exception:
            try:
                res = subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(junction_path), str(external_target)],
                    capture_output=True,
                    check=False,
                )
                created = res.returncode == 0 and junction_path.exists()
            except Exception:
                created = False

        if not created:
            self.skipTest("Real NTFS directory junction creation is not supported on this platform")

        with self.assertRaisesRegex(
            verifier.VerificationError,
            "Linked runtime path is refused",
        ):
            verifier.runtime_manifest(root)

        with self.assertRaisesRegex(
            installer.SuiteError,
            "Linked or reparse payload path is refused",
        ):
            installer.tree_files(root)

    def test_optional_real_symlink_refusal_if_supported(self) -> None:
        temporary, root = self.make_test_tree()
        target_file = Path(temporary.name) / "target.txt"
        target_file.write_text("target\n", encoding="ascii")

        cache_dir = root / ".pytest_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        symlink_path = cache_dir / "symlink_file.txt"

        try:
            symlink_path.symlink_to(target_file)
        except (OSError, NotImplementedError):
            # As allowed by requirements, lack of symlink creation privilege skips optional test
            return

        with self.assertRaisesRegex(
            verifier.VerificationError,
            "Linked runtime path is refused",
        ):
            verifier.runtime_manifest(root)

        with self.assertRaisesRegex(
            installer.SuiteError,
            "Linked or reparse payload path is refused",
        ):
            installer.tree_files(root)

    def test_compare_installer_and_runtime_manifests_identical(self) -> None:
        _, root = self.make_test_tree()

        # Add rich diverse benign cache structure
        cache_root = root / ".pytest_cache" / "v" / "cache"
        cache_root.mkdir(parents=True, exist_ok=True)
        (cache_root / "nodeids").write_text('["test_one", "test_two"]\n', encoding="utf-8")
        (root / ".pytest_cache" / "CACHEDIR.TAG").write_text("Header\n", encoding="ascii")

        nested_cache = root / "pkg" / ".pytest_cache" / "v"
        nested_cache.mkdir(parents=True, exist_ok=True)
        (nested_cache / "stepwise").write_text("[]\n", encoding="ascii")

        pycache = root / "__pycache__"
        pycache.mkdir(parents=True, exist_ok=True)
        (pycache / "module.cpython-312.pyc").write_bytes(b"\x00\x01bytecode")

        ruff_cache = root / ".ruff_cache" / "0.1"
        ruff_cache.mkdir(parents=True, exist_ok=True)
        (ruff_cache / "cache.bin").write_bytes(b"ruff")

        installer_rows, installer_fp = installer.tree_manifest(
            root, marker_names={installer.RUNTIME_MARKER}
        )
        runtime_rows, runtime_fp = verifier.runtime_manifest(root)

        self.assertEqual(installer_fp, runtime_fp)
        self.assertEqual(installer_rows, runtime_rows)
        # Ensure only the 3 canonical payload files are in the manifest
        expected_paths = sorted(["module.py", "pkg/data.json", "pkg/submodule.py"], key=str.casefold)
        manifest_paths = sorted([r["path"] for r in runtime_rows], key=str.casefold)
        self.assertEqual(manifest_paths, expected_paths)


if __name__ == "__main__":
    unittest.main()
