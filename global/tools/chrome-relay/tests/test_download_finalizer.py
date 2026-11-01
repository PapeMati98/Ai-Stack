"""Unit and synthetic Blob/ZIP tests for download_finalizer (HD-20261014-b53dfc).

Stdlib only; retains all test fixtures (no-delete policy).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME))

from download_finalizer import (  # noqa: E402
    build_synthetic_zip_bytes,
    finalize_download,
    run_chromium_blob_zip_canary,
    run_synthetic_blob_zip_reproducer,
    verify_zip_integrity,
    wait_for_download_or_finalize,
)

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
FIXTURE_ROOT = Path(os.environ.get("TEAM_TEST_ROOT", tempfile.gettempdir())) / "chrome-relay-download-tests"


def _new_case_dir(prefix: str) -> Path:
    FIXTURE_ROOT.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f"{prefix}-", dir=str(FIXTURE_ROOT)))


class DownloadFinalizerTests(unittest.TestCase):
    def test_synthetic_blob_zip_reproducer_recovers_guid_tmp_no_delete(self):
        work_dir = _new_case_dir("repro")
        report = run_synthetic_blob_zip_reproducer(work_dir)
        self.assertTrue(report["ok"], report)
        self.assertTrue(report["guid_tmp_preserved"])
        self.assertTrue(report["final_exists"])
        self.assertTrue(report["zip_crc_ok"])
        self.assertEqual(report["outcome"]["status"], "ok")
        self.assertEqual(report["outcome"]["data"]["source_kind"], "guid_tmp")
        self.assertEqual(report["outcome"]["data"]["wait_event_status"], "timeout_recovered")
        self.assertEqual(report["outcome"]["data"]["sha256"], report["expected_sha256"])

    def test_named_download_already_present_is_verified_and_copied_no_delete(self):
        case_dir = _new_case_dir("named")
        dl_dir = case_dir / "dl"
        out_dir = case_dir / "out"
        dl_dir.mkdir()
        out_dir.mkdir()
        payload = build_synthetic_zip_bytes("named-case")
        named = dl_dir / "report.xlsx"
        named.write_bytes(payload)

        res = finalize_download(dl_dir, "report.xlsx", timeout_s=1.0, poll_interval_s=0.01, output_dir=out_dir)
        self.assertEqual(res["status"], "ok", res)
        self.assertEqual(res["data"]["source_kind"], "named")
        self.assertEqual(res["data"]["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertTrue(named.is_file())
        self.assertTrue((out_dir / "report.xlsx").is_file())
        self.assertTrue(verify_zip_integrity(out_dir / "report.xlsx"))

    def test_corrupt_tmp_zip_is_rejected(self):
        case_dir = _new_case_dir("corrupt")
        guid_tmp = case_dir / f"{uuid.uuid4()}.tmp"
        guid_tmp.write_bytes(b"PK\x03\x04corrupt-truncated-zip-bytes")

        res = finalize_download(case_dir, "export.zip", timeout_s=0.2, poll_interval_s=0.02)
        self.assertEqual(res["status"], "denied", res)
        self.assertEqual(res["data"]["reason"], "corrupt_zip_payload")
        self.assertFalse((case_dir / "export.zip").exists())
        self.assertTrue(guid_tmp.is_file())

    def test_active_crdownload_blocks_premature_tmp_pickup(self):
        case_dir = _new_case_dir("crdownload")
        guid_tmp = case_dir / f"{uuid.uuid4()}.tmp"
        guid_tmp.write_bytes(build_synthetic_zip_bytes("while-crdownload"))
        cr = case_dir / "unfinished.xlsx.crdownload"
        cr.write_bytes(b"partial")

        res = finalize_download(case_dir, "export.xlsx", timeout_s=0.15, poll_interval_s=0.02)
        self.assertEqual(res["status"], "denied", res)
        self.assertEqual(res["data"]["reason"], "timeout")

    def test_ambiguous_distinct_guid_tmp_files_fail_closed(self):
        case_dir = _new_case_dir("ambiguous")
        (case_dir / f"{uuid.uuid4()}.tmp").write_bytes(build_synthetic_zip_bytes("one"))
        (case_dir / f"{uuid.uuid4()}.tmp").write_bytes(build_synthetic_zip_bytes("two"))

        res = finalize_download(case_dir, "export.xlsx", timeout_s=0.2, poll_interval_s=0.02)
        self.assertEqual(res["status"], "denied", res)
        self.assertEqual(res["data"]["reason"], "ambiguous_tmp_candidates")

    def test_target_exists_with_different_bytes_is_never_overwritten(self):
        case_dir = _new_case_dir("no-overwrite")
        (case_dir / f"{uuid.uuid4()}.tmp").write_bytes(build_synthetic_zip_bytes("new-export"))
        target = case_dir / "export.xlsx"
        original_bytes = build_synthetic_zip_bytes("existing-precious-file")
        target.write_bytes(original_bytes)
        # Ensure target is older than started_after_ns so named branch skips it and guid_tmp branch hits conflict
        started_ns = time.time_ns() + 10_000_000_000
        tmp2 = case_dir / f"{uuid.uuid4()}.tmp"
        tmp2.write_bytes(build_synthetic_zip_bytes("new-export"))
        os.utime(tmp2, ns=(started_ns + 1000, started_ns + 1000))

        res = finalize_download(
            case_dir,
            "export.xlsx",
            started_after_ns=started_ns,
            timeout_s=0.2,
            poll_interval_s=0.02,
        )
        self.assertEqual(res["status"], "denied", res)
        self.assertEqual(res["data"]["reason"], "target_exists_mismatch")
        self.assertEqual(target.read_bytes(), original_bytes)

    def test_path_traversal_and_invalid_filenames_are_denied(self):
        case_dir = _new_case_dir("traversal")
        for bad_name in ("../evil.xlsx", "sub/evil.xlsx", "sub\\evil.xlsx", ".hidden.zip", "file.tmp", "file.crdownload"):
            res = finalize_download(case_dir, bad_name, timeout_s=0.1)
            self.assertEqual(res["status"], "denied", bad_name)
            self.assertEqual(res["data"]["reason"], "invalid_filename")

    def test_cli_finalize_download_subcommand(self):
        case_dir = _new_case_dir("cli")
        payload = build_synthetic_zip_bytes("cli-subcommand")
        guid_tmp = case_dir / f"{uuid.uuid4()}.tmp"
        guid_tmp.write_bytes(payload)

        proc = subprocess.run(
            [
                sys.executable,
                "-B",
                str(RUNTIME / "cli.py"),
                "finalize-download",
                "--download-dir",
                str(case_dir),
                "--expected-name",
                "cli-export.zip",
                "--timeout",
                "1.5",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(RUNTIME),
            creationflags=NO_WINDOW,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        body = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(body["status"], "ok", body)
        self.assertEqual(body["data"]["source_kind"], "guid_tmp")
        self.assertEqual(body["data"]["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertTrue((case_dir / "cli-export.zip").is_file())
        self.assertTrue(guid_tmp.is_file())

    def test_real_chromium_blob_zip_export_and_guid_tmp_finalization(self):
        work_dir = _new_case_dir("chromium-blob")
        report = run_chromium_blob_zip_canary(work_dir)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["browser_meta"]["suggestedFilename"], "browser-export.xlsx")
        self.assertTrue(report["guid_tmp_preserved"])
        self.assertTrue(report["zip_crc_ok"])
        self.assertEqual(report["outcome"]["status"], "ok")
        self.assertEqual(report["outcome"]["data"]["source_kind"], "guid_tmp")
        self.assertEqual(report["outcome"]["data"]["sha256"], report["expected_sha256"])


if __name__ == "__main__":
    unittest.main()
