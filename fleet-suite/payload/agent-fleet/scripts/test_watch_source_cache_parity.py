"""Regression tests for Fleet/Office source fingerprint cache parity.

The managed Office installer fingerprints runtime source while ignoring
ordinary ``__pycache__`` trees and ``.pyc``/``.pyo`` files.  Fleet computes the
same fingerprint locally to decide whether an installed runtime still matches
its pinned marker, so both sides have to exclude exactly the same paths.

These tests pin the required behaviour:

* bytecode never changes the fingerprint (and is never executed: the callback
  launches the runtime with ``-B -I``);
* real source drift -- changed, added, or missing source -- still changes the
  fingerprint and is refused against a pinned marker;
* a linked file or directory is refused even when it hides under an ignored
  cache name;
* the remote route attestation is unchanged by the cache exclusion.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import fleet  # noqa: E402


OFFICE_SOURCE = "print('office runtime')\n"
REMOTE_CONFIG = {
    "host": "office.example.invalid",
    "user": "office",
    "identity_file": "C:/keys/id_ed25519",
    "known_hosts_file": "C:/keys/known_hosts",
    "ssh_executable": "C:/Windows/System32/OpenSSH/ssh.exe",
}


def _symlink_supported(
    link_target: Path, link_path: Path, *, target_is_directory: bool = False
) -> bool:
    """Return True when a symlink could actually be created on this host."""
    try:
        if link_path.exists() or link_path.is_symlink():
            return False
        link_path.symlink_to(link_target, target_is_directory=target_is_directory)
    except (OSError, NotImplementedError):
        return False
    finally:
        try:
            if link_path.is_symlink():
                link_path.unlink()
        except OSError:
            pass
    return True


class WatchSourceCacheParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="fleet-watch-cache-parity-"))
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        (self.runtime / "office.py").write_text(OFFICE_SOURCE, encoding="utf-8")
        (self.runtime / "office.cmd").write_text(
            "synthetic launcher\n", encoding="utf-8"
        )
        (self.runtime / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.baseline = fleet._watch_office_source_fingerprint(self.runtime)
        self.assertIsNotNone(self.baseline)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def marker(self, **extra: object) -> dict[str, object]:
        marker: dict[str, object] = {
            "managed_by": "agent-office-installer",
            "package_id": "agent-office",
            "schema_version": 1,
            "package_version": "1.0.0",
            "tool": "runtime",
            "package_fingerprint": "a" * 64,
            "source_fingerprint": self.baseline,
            "python_executable": sys.executable,
        }
        marker.update(extra)
        return marker

    def write_marker(self, marker: dict[str, object]) -> None:
        (self.runtime / ".agent-office-runtime.json").write_text(
            json.dumps(marker), encoding="utf-8"
        )

    def write_remote_config(self, config: dict[str, object]) -> str:
        raw = json.dumps(config).encode("utf-8")
        (self.runtime / fleet.OFFICE_REMOTE_CONFIG_FILE).write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()

    # ------------------------------------------------------------------
    # cache exclusion
    # ------------------------------------------------------------------
    def test_bytecode_cache_does_not_change_the_fingerprint(self) -> None:
        cache = self.runtime / "__pycache__"
        cache.mkdir()
        (cache / "office.cpython-313.pyc").write_bytes(b"\x00\x01cache")
        (self.runtime / "office.pyc").write_bytes(b"\x00\x02cache")
        (self.runtime / "office.pyo").write_bytes(b"\x00\x03cache")
        (self.runtime / "MiXeD.PyC").write_bytes(b"\x00\x04cache")
        (self.runtime / "MiXeD.PyO").write_bytes(b"\x00\x05cache")

        self.assertEqual(
            fleet._watch_office_source_fingerprint(self.runtime), self.baseline
        )

    def test_nested_cache_tree_does_not_change_the_fingerprint(self) -> None:
        nested = self.runtime / "pkg" / "__pycache__"
        nested.mkdir(parents=True)
        (nested / "pkg.cpython-313.pyc").write_bytes(b"\x00\x06cache")
        self.assertEqual(
            fleet._watch_office_source_fingerprint(self.runtime), self.baseline
        )

    def test_mixed_case_cache_directory_keeps_non_bytecode_source_hashed(
        self,
    ) -> None:
        # Only a lowercase __pycache__ hides a whole tree; a renamed directory
        # must not let real source disappear from the fingerprint.
        renamed = self.runtime / "__PyCache__"
        renamed.mkdir()
        (renamed / "office.cpython-313.pyc").write_bytes(b"\x00\x07cache")
        (renamed / "hidden.py").write_text("VALUE = 2\n", encoding="utf-8")

        current = fleet._watch_office_source_fingerprint(self.runtime)
        self.assertIsNotNone(current)
        self.assertNotEqual(current, self.baseline)

    # ------------------------------------------------------------------
    # real drift is still refused
    # ------------------------------------------------------------------
    def test_changed_source_changes_the_fingerprint(self) -> None:
        (self.runtime / "office.py").write_text(
            "print('office runtime changed')\n", encoding="utf-8"
        )
        current = fleet._watch_office_source_fingerprint(self.runtime)
        self.assertIsNotNone(current)
        self.assertNotEqual(current, self.baseline)

    def test_added_source_changes_the_fingerprint(self) -> None:
        (self.runtime / "owner_reply_port.py").write_text(
            "def reply():\n    return None\n", encoding="utf-8"
        )
        current = fleet._watch_office_source_fingerprint(self.runtime)
        self.assertIsNotNone(current)
        self.assertNotEqual(current, self.baseline)

    def test_removed_source_changes_the_fingerprint(self) -> None:
        (self.runtime / "helper.py").unlink()
        current = fleet._watch_office_source_fingerprint(self.runtime)
        self.assertIsNotNone(current)
        self.assertNotEqual(current, self.baseline)

    def test_pinned_marker_still_matches_after_bytecode_only_change(self) -> None:
        self.write_marker(self.marker())
        cache = self.runtime / "__pycache__"
        cache.mkdir()
        (cache / "office.cpython-313.pyc").write_bytes(b"\x00\x08cache")

        marker = fleet._office_installed_marker(self.runtime)
        self.assertIsNotNone(marker)
        self.assertEqual(
            fleet._watch_office_source_fingerprint(self.runtime),
            marker["source_fingerprint"],
        )

    def test_pinned_marker_is_refused_after_real_source_drift(self) -> None:
        self.write_marker(self.marker())
        (self.runtime / "helper.py").write_text("VALUE = 99\n", encoding="utf-8")

        marker = fleet._office_installed_marker(self.runtime)
        self.assertIsNotNone(marker)
        self.assertNotEqual(
            fleet._watch_office_source_fingerprint(self.runtime),
            marker["source_fingerprint"],
        )

    def test_pinned_marker_is_refused_after_added_source(self) -> None:
        self.write_marker(self.marker())
        (self.runtime / "owner_reply_port.py").write_text(
            "def reply():\n    return None\n", encoding="utf-8"
        )

        marker = fleet._office_installed_marker(self.runtime)
        self.assertIsNotNone(marker)
        self.assertNotEqual(
            fleet._watch_office_source_fingerprint(self.runtime),
            marker["source_fingerprint"],
        )

    # ------------------------------------------------------------------
    # linked entries are refused, cache aliases included
    # ------------------------------------------------------------------
    def test_symlinked_file_under_cache_directory_is_refused(self) -> None:
        cache = self.runtime / "__pycache__"
        cache.mkdir()
        target = self.root / "outside.py"
        target.write_text("VALUE = 3\n", encoding="utf-8")
        link = cache / "office.cpython-313.pyc"
        if not _symlink_supported(target, link):
            self.skipTest("symlink creation is not permitted on this host")
        link.symlink_to(target)

        self.assertIsNone(fleet._watch_office_source_fingerprint(self.runtime))

    def test_symlinked_cache_file_is_refused(self) -> None:
        target = self.root / "outside.py"
        target.write_text("VALUE = 4\n", encoding="utf-8")
        link = self.runtime / "alias.pyc"
        if not _symlink_supported(target, link):
            self.skipTest("symlink creation is not permitted on this host")
        link.symlink_to(target)

        self.assertIsNone(fleet._watch_office_source_fingerprint(self.runtime))

    def test_symlinked_cache_directory_is_refused(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "payload.py").write_text("VALUE = 5\n", encoding="utf-8")
        link = self.runtime / "__pycache__"
        if not _symlink_supported(outside, link, target_is_directory=True):
            self.skipTest("symlink creation is not permitted on this host")
        link.symlink_to(outside, target_is_directory=True)

        self.assertIsNone(fleet._watch_office_source_fingerprint(self.runtime))

    def test_symlinked_source_file_is_refused(self) -> None:
        target = self.root / "outside.py"
        target.write_text("VALUE = 6\n", encoding="utf-8")
        link = self.runtime / "linked.py"
        if not _symlink_supported(target, link):
            self.skipTest("symlink creation is not permitted on this host")
        link.symlink_to(target)

        self.assertIsNone(fleet._watch_office_source_fingerprint(self.runtime))

    # ------------------------------------------------------------------
    # remote route attestation is unchanged
    # ------------------------------------------------------------------
    def test_unpinned_remote_config_is_refused(self) -> None:
        self.write_remote_config(REMOTE_CONFIG)
        self.write_marker(self.marker())
        self.assertIsNone(fleet._watch_office_source_fingerprint(self.runtime))

    def test_remote_config_with_matching_pin_is_excluded_from_the_hash(self) -> None:
        pin = self.write_remote_config(REMOTE_CONFIG)
        self.write_marker(self.marker(remote_config_sha256=pin))

        current = fleet._watch_office_source_fingerprint(self.runtime)
        self.assertIsNotNone(current)
        self.assertEqual(current, self.baseline)

        marker = fleet._office_installed_marker(self.runtime)
        self.assertIsNotNone(marker)
        route, refusal = fleet._office_resolve_installed_route(self.runtime, marker)
        self.assertIsNone(refusal)
        self.assertEqual(
            route, ["--remote-config", str(self.runtime / fleet.OFFICE_REMOTE_CONFIG_FILE)]
        )

    def test_remote_config_drift_is_refused(self) -> None:
        pin = self.write_remote_config(REMOTE_CONFIG)
        self.write_marker(self.marker(remote_config_sha256=pin))
        (self.runtime / fleet.OFFICE_REMOTE_CONFIG_FILE).write_text(
            json.dumps({**REMOTE_CONFIG, "host": "other.example.invalid"}),
            encoding="utf-8",
        )
        self.assertIsNone(fleet._watch_office_source_fingerprint(self.runtime))

    def test_remote_config_with_invalid_pin_is_refused(self) -> None:
        self.write_remote_config(REMOTE_CONFIG)
        self.write_marker(self.marker(remote_config_sha256="not-a-digest"))
        self.assertIsNone(fleet._watch_office_source_fingerprint(self.runtime))

    def test_cache_exclusion_does_not_mask_remote_route_refusal(self) -> None:
        # A runtime that only gained bytecode but has an untrusted remote route
        # is still refused: the cache exclusion never weakens the route check.
        self.write_remote_config(REMOTE_CONFIG)
        self.write_marker(self.marker())
        cache = self.runtime / "__pycache__"
        cache.mkdir()
        (cache / "office.cpython-313.pyc").write_bytes(b"\x00\x09cache")
        self.assertIsNone(fleet._watch_office_source_fingerprint(self.runtime))


if __name__ == "__main__":
    unittest.main()
