#!/usr/bin/env python3
"""Comprehensive unit tests for browser_uat_check verifier and evidence check."""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

# Ensure scripts directory is on sys.path regardless of execution cwd
TESTS_DIR = Path(__file__).resolve().parent
SKILL_DIR = TESTS_DIR.parent
SCRIPTS_DIR = SKILL_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import browser_uat_check
from browser_uat_check import (
    BrowserUATValidationError,
    check_viewports,
    classify_step,
    get_subprocess_creation_flags,
    is_loopback_host,
    is_step_executed,
    main,
    parse_viewport_dimensions,
    probe_url,
    sha256_file,
    validate_report,
    REASON_ARTIFACT_HASH_MISMATCH,
    REASON_CONSOLE_ERRORS_PRESENT,
    REASON_HTTP_5XX_PRESENT,
    REASON_MISSING_INTERACTIVE_ACTION,
    REASON_MISSING_MOBILE_VIEWPORT,
    REASON_STATIC_SCREENSHOT_ONLY,
)


class MockServer(http.server.HTTPServer):
    """Local ephemeral HTTP server for testing probe_url, redirects, and 5xx responses."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path.startswith("/500"):
                    self.send_response(500)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    self.wfile.write(b"Internal Server Error")
                elif self.path.startswith("/redirect-external"):
                    self.send_response(302)
                    self.send_header("Location", "http://example.com/external-path")
                    self.end_headers()
                elif self.path.startswith("/redirect-credentials"):
                    srv_port = self.server.server_address[1]
                    self.send_response(302)
                    self.send_header("Location", f"http://admin:secret@127.0.0.1:{srv_port}/target")
                    self.end_headers()
                elif self.path.startswith("/redirect-loopback"):
                    srv_port = self.server.server_address[1]
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{srv_port}/ready")
                    self.end_headers()
                else:
                    body = (
                        b"<!DOCTYPE html><html><head><title>Test App</title></head>"
                        b"<body><div id='root'><h1>Ready</h1></div></body></html>"
                    )
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                pass  # Suppress stderr during test runs

        super().__init__((host, port), Handler)


class TestValidateReportBasics(unittest.TestCase):
    """Test basic report validation and required fields."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp(prefix="uat_test_")
        self.img_file = Path(self.tmp_dir) / "evidence.png"
        self.img_file.write_bytes(b"\x89PNG\r\n\x1a\nPIXEL_DATA_123")
        self.img_hash = hashlib.sha256(b"\x89PNG\r\n\x1a\nPIXEL_DATA_123").hexdigest()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _valid_report(self) -> dict:
        return {
            "route": "/login",
            "role": "customer",
            "environment": "DEV",
            "responsive_required": False,
            "viewports": [{"name": "desktop", "width": 1440, "height": 900}],
            "steps": [
                {"type": "open", "action": "navigate", "target": "/login", "status": "PASS"},
                {"type": "user_action", "action": "click", "target": "#submit-btn", "status": "PASS"},
                {"type": "visible_response", "action": "assert_visible", "target": "#welcome", "status": "PASS"},
                {"type": "terminal_state", "action": "state_check", "target": "completed", "status": "PASS"},
            ],
            "console_errors": 0,
            "http_5xx_count": 0,
            "screenshots": [
                {
                    "path": str(self.img_file),
                    "sha256": self.img_hash,
                }
            ],
        }

    def test_valid_minimal_report(self) -> None:
        report = self._valid_report()
        res = validate_report(report)
        self.assertTrue(res["valid"])
        self.assertEqual(res["reason_codes"], [])
        self.assertEqual(res["errors"], [])

    def test_valid_responsive_report(self) -> None:
        report = self._valid_report()
        report["responsive_required"] = True
        report["viewports"] = [
            {"name": "desktop", "width": 1440, "height": 900},
            {"name": "mobile", "width": 390, "height": 844},
        ]
        res = validate_report(report)
        self.assertTrue(res["valid"])
        self.assertEqual(res["reason_codes"], [])

    def test_missing_route_and_url(self) -> None:
        report = self._valid_report()
        del report["route"]
        res = validate_report(report)
        self.assertFalse(res["valid"])
        self.assertTrue(any("route" in e.lower() or "url" in e.lower() for e in res["errors"]))

    def test_missing_role(self) -> None:
        report = self._valid_report()
        del report["role"]
        res = validate_report(report)
        self.assertFalse(res["valid"])
        self.assertTrue(any("role" in e.lower() for e in res["errors"]))

    def test_invalid_environment(self) -> None:
        report = self._valid_report()
        report["environment"] = "PROD"  # PROD is not allowed for autonomous dev UAT
        res = validate_report(report)
        self.assertFalse(res["valid"])
        self.assertTrue(any("environment" in e.lower() for e in res["errors"]))

    def test_allowed_environments(self) -> None:
        for env in ("DEV", "TEST", "STAGING"):
            report = self._valid_report()
            report["environment"] = env
            res = validate_report(report)
            self.assertTrue(res["valid"], f"Environment {env} should be allowed")


class TestValidateReportReasonCodes(unittest.TestCase):
    """Test the 6 specific required fail-closed reason codes."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp(prefix="uat_reasons_")
        self.img_file = Path(self.tmp_dir) / "screen.png"
        self.img_file.write_bytes(b"\x89PNG\r\n\x1a\nVALID_BYTES")
        self.img_hash = hashlib.sha256(b"\x89PNG\r\n\x1a\nVALID_BYTES").hexdigest()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _base_report(self) -> dict:
        return {
            "route": "/checkout",
            "role": "shopper",
            "environment": "TEST",
            "responsive_required": False,
            "steps": [
                {"type": "open", "action": "navigate", "status": "PASS"},
                {"type": "user_action", "action": "click", "status": "PASS"},
                {"type": "visible_response", "action": "assert_visible", "status": "PASS"},
                {"type": "terminal_state", "action": "state_check", "status": "PASS"},
            ],
            "console_errors": 0,
            "http_5xx_count": 0,
            "screenshots": [
                {
                    "path": str(self.img_file),
                    "sha256": self.img_hash,
                }
            ],
        }

    def test_static_screenshot_only_flag(self) -> None:
        report = self._base_report()
        report["static_screenshot_only"] = True
        res = validate_report(report)
        self.assertFalse(res["valid"])
        self.assertIn(REASON_STATIC_SCREENSHOT_ONLY, res["reason_codes"])

    def test_static_screenshot_only_kind(self) -> None:
        report = self._base_report()
        report["kind"] = "static_screenshot"
        res = validate_report(report)
        self.assertFalse(res["valid"])
        self.assertIn(REASON_STATIC_SCREENSHOT_ONLY, res["reason_codes"])

    def test_static_screenshot_only_steps_list(self) -> None:
        report = self._base_report()
        # Only open and screenshot without any interactive steps
        report["steps"] = [
            {"type": "open", "action": "navigate", "status": "PASS"},
            {"type": "screenshot", "action": "capture", "status": "PASS"},
        ]
        res = validate_report(report)
        self.assertFalse(res["valid"])
        self.assertIn(REASON_STATIC_SCREENSHOT_ONLY, res["reason_codes"])

    def test_missing_interactive_action(self) -> None:
        report = self._base_report()
        # Progression with open, visible response, and terminal state but missing user_action
        report["steps"] = [
            {"type": "open", "action": "navigate", "status": "PASS"},
            {"type": "visible_response", "action": "assert_visible", "status": "PASS"},
            {"type": "terminal_state", "action": "state_check", "status": "PASS"},
        ]
        res = validate_report(report)
        self.assertFalse(res["valid"])
        self.assertIn(REASON_MISSING_INTERACTIVE_ACTION, res["reason_codes"])

    def test_pending_user_action_fails(self) -> None:
        report = self._base_report()
        report["steps"] = [
            {"type": "open", "action": "navigate", "status": "PASS"},
            {"type": "user_action", "action": "click", "status": "PENDING"},
            {"type": "visible_response", "action": "assert_visible", "status": "PENDING"},
            {"type": "terminal_state", "action": "state_check", "status": "PENDING"},
        ]
        res = validate_report(report)
        self.assertFalse(res["valid"])
        self.assertIn(REASON_MISSING_INTERACTIVE_ACTION, res["reason_codes"])

    def test_console_errors_present_int(self) -> None:
        report = self._base_report()
        report["console_errors"] = 3
        res = validate_report(report)
        self.assertFalse(res["valid"])
        self.assertIn(REASON_CONSOLE_ERRORS_PRESENT, res["reason_codes"])

    def test_console_errors_present_list(self) -> None:
        report = self._base_report()
        report["console_errors"] = ["Uncaught TypeError: undefined is not a function"]
        res = validate_report(report)
        self.assertFalse(res["valid"])
        self.assertIn(REASON_CONSOLE_ERRORS_PRESENT, res["reason_codes"])

    def test_console_errors_omitted_fails_closed(self) -> None:
        report = self._base_report()
        del report["console_errors"]
        res = validate_report(report)
        self.assertFalse(res["valid"])
        self.assertIn(REASON_CONSOLE_ERRORS_PRESENT, res["reason_codes"])

    def test_console_errors_invalid_types_fail_closed(self) -> None:
        for invalid_val in (True, False, 5.0, "5", "0", -1, {"err": 1}):
            report = self._base_report()
            report["console_errors"] = invalid_val
            res = validate_report(report)
            self.assertFalse(res["valid"], f"Expected invalid for {invalid_val!r}")
            self.assertIn(REASON_CONSOLE_ERRORS_PRESENT, res["reason_codes"])

    def test_console_errors_conflicting_alternate_fails_closed(self) -> None:
        report = self._base_report()
        report["console_errors"] = 0
        report["console_error_count"] = 2
        res = validate_report(report)
        self.assertFalse(res["valid"])
        self.assertIn(REASON_CONSOLE_ERRORS_PRESENT, res["reason_codes"])

    def test_console_error_count_valid_zero(self) -> None:
        report = self._base_report()
        del report["console_errors"]
        report["console_error_count"] = 0
        res = validate_report(report)
        self.assertTrue(res["valid"])

    def test_http_5xx_present_int(self) -> None:
        report = self._base_report()
        report["http_5xx_count"] = 2
        res = validate_report(report)
        self.assertFalse(res["valid"])
        self.assertIn(REASON_HTTP_5XX_PRESENT, res["reason_codes"])

    def test_http_5xx_omitted_fails_closed(self) -> None:
        report = self._base_report()
        del report["http_5xx_count"]
        res = validate_report(report)
        self.assertFalse(res["valid"])
        self.assertIn(REASON_HTTP_5XX_PRESENT, res["reason_codes"])

    def test_http_5xx_invalid_types_fail_closed(self) -> None:
        for invalid_val in (True, False, 5.0, "5", "0", -1, [500]):
            report = self._base_report()
            report["http_5xx_count"] = invalid_val
            res = validate_report(report)
            self.assertFalse(res["valid"], f"Expected invalid for {invalid_val!r}")
            self.assertIn(REASON_HTTP_5XX_PRESENT, res["reason_codes"])

    def test_http_5xx_conflicting_alternate_fails_closed(self) -> None:
        report = self._base_report()
        report["http_5xx_count"] = 0
        report["http_5xx_errors"] = 1
        res = validate_report(report)
        self.assertFalse(res["valid"])
        self.assertIn(REASON_HTTP_5XX_PRESENT, res["reason_codes"])

    def test_http_5xx_errors_valid_zero_or_empty_list(self) -> None:
        report = self._base_report()
        del report["http_5xx_count"]
        report["http_5xx_errors"] = []
        res = validate_report(report)
        self.assertTrue(res["valid"])

    def test_missing_mobile_viewport(self) -> None:
        report = self._base_report()
        report["responsive_required"] = True
        report["viewports"] = [{"name": "desktop", "width": 1440, "height": 900}]
        res = validate_report(report)
        self.assertFalse(res["valid"])
        self.assertIn(REASON_MISSING_MOBILE_VIEWPORT, res["reason_codes"])

    def test_artifact_hash_mismatch_corrupted(self) -> None:
        report = self._base_report()
        report["screenshots"] = [
            {
                "path": str(self.img_file),
                "sha256": "0000000000000000000000000000000000000000000000000000000000000000",
            }
        ]
        res = validate_report(report, base_dir=self.tmp_dir)
        self.assertFalse(res["valid"])
        self.assertIn(REASON_ARTIFACT_HASH_MISMATCH, res["reason_codes"])

    def test_artifact_hash_mismatch_missing_file(self) -> None:
        report = self._base_report()
        report["screenshots"] = [
            {
                "path": "non_existent_screen.png",
                "sha256": "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
            }
        ]
        res = validate_report(report, base_dir=self.tmp_dir)
        self.assertFalse(res["valid"])
        self.assertIn(REASON_ARTIFACT_HASH_MISMATCH, res["reason_codes"])

    def test_artifact_hash_mismatch_empty_screenshots(self) -> None:
        report = self._base_report()
        report["screenshots"] = []
        res = validate_report(report)
        self.assertFalse(res["valid"])
        self.assertIn(REASON_ARTIFACT_HASH_MISMATCH, res["reason_codes"])

    def test_artifact_hash_mismatch_omitted_screenshots(self) -> None:
        report = self._base_report()
        del report["screenshots"]
        res = validate_report(report)
        self.assertFalse(res["valid"])
        self.assertIn(REASON_ARTIFACT_HASH_MISMATCH, res["reason_codes"])

    def test_valid_screenshot_hash_match(self) -> None:
        report = self._base_report()
        res = validate_report(report, base_dir=self.tmp_dir)
        self.assertTrue(res["valid"])
        self.assertEqual(res["reason_codes"], [])

    def test_raise_on_error(self) -> None:
        report = self._base_report()
        report["console_errors"] = 1
        with self.assertRaises(BrowserUATValidationError) as ctx:
            validate_report(report, raise_on_error=True)
        self.assertIn(REASON_CONSOLE_ERRORS_PRESENT, ctx.exception.reason_codes)


class TestStepSequenceValidation(unittest.TestCase):
    """Test interactive step progression classifications and ordering."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp(prefix="uat_step_")
        self.img_file = Path(self.tmp_dir) / "screen.png"
        self.img_file.write_bytes(b"DATA")
        self.img_hash = hashlib.sha256(b"DATA").hexdigest()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _base_report(self) -> dict:
        return {
            "route": "/app",
            "role": "admin",
            "environment": "DEV",
            "steps": [],
            "console_errors": 0,
            "http_5xx_count": 0,
            "screenshots": [{"path": str(self.img_file), "sha256": self.img_hash}],
        }

    def test_string_steps_valid(self) -> None:
        report = self._base_report()
        report["steps"] = ["open", "user_action", "visible_response", "terminal_state"]
        res = validate_report(report)
        self.assertTrue(res["valid"])

    def test_action_names_valid(self) -> None:
        report = self._base_report()
        report["steps"] = [
            {"action": "navigate", "target": "/app", "status": "PASS"},
            {"action": "click", "target": "#save", "status": "PASS"},
            {"action": "assert_visible", "target": ".alert-success", "status": "PASS"},
            {"action": "complete", "target": "success", "status": "PASS"},
        ]
        res = validate_report(report)
        self.assertTrue(res["valid"])

    def test_out_of_order_steps(self) -> None:
        report = self._base_report()
        report["steps"] = [
            {"type": "user_action", "status": "PASS"},
            {"type": "open", "status": "PASS"},
            {"type": "visible_response", "status": "PASS"},
            {"type": "terminal_state", "status": "PASS"},
        ]
        res = validate_report(report)
        self.assertFalse(res["valid"])
        self.assertTrue(any("progression" in e.lower() for e in res["errors"]))


class TestProbeUrl(unittest.TestCase):
    """Test probe_url loopback safety, HTTP DOM probe, and skeleton generation."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = MockServer()
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def test_external_domain_rejected_fail_closed(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            probe_url("https://example.com/login")
        self.assertIn("External network calls prohibited", str(ctx.exception))

    def test_all_interfaces_0000_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            probe_url("http://0.0.0.0:8080/test")
        self.assertIn("External network calls prohibited", str(ctx.exception))

    def test_userinfo_prohibited_fail_closed(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            probe_url("http://admin:secret@127.0.0.1:8080/secure")
        self.assertIn("URLs with embedded credentials (userinfo) are prohibited", str(ctx.exception))

    def test_redirect_to_external_rejected(self) -> None:
        url = f"http://127.0.0.1:{self.port}/redirect-external"
        with self.assertRaises(ValueError) as ctx:
            probe_url(url)
        self.assertIn("External network calls prohibited", str(ctx.exception))

    def test_redirect_with_credentials_rejected(self) -> None:
        url = f"http://127.0.0.1:{self.port}/redirect-credentials"
        with self.assertRaises(ValueError) as ctx:
            probe_url(url)
        self.assertIn("Redirect URL contains embedded credentials", str(ctx.exception))

    def test_redirect_to_loopback_allowed(self) -> None:
        url = f"http://127.0.0.1:{self.port}/redirect-loopback"
        skeleton = probe_url(url)
        self.assertEqual(skeleton["probe"]["status_code"], 200)
        self.assertTrue(skeleton["probe"]["dom_ready"])

    def test_unreachable_loopback_returns_skeleton(self) -> None:
        # Unreachable port returns skeleton with exit 0 behavior
        skeleton = probe_url("http://127.0.0.1:59998/offline-test", role="tester", environment="DEV")
        self.assertEqual(skeleton["route"], "/offline-test")
        self.assertEqual(skeleton["environment"], "DEV")
        self.assertIsNone(skeleton["probe"]["status_code"])
        self.assertFalse(skeleton["probe"]["dom_ready"])
        self.assertIsNotNone(skeleton["probe"]["error"])
        self.assertEqual(len(skeleton["steps"]), 4)

    def test_probe_500_server_sets_5xx_count(self) -> None:
        url = f"http://127.0.0.1:{self.port}/500"
        skeleton = probe_url(url)
        self.assertEqual(skeleton["probe"]["status_code"], 500)
        self.assertEqual(skeleton["http_5xx_count"], 1)

        val_res = validate_report(skeleton)
        self.assertFalse(val_res["valid"])
        self.assertIn(REASON_HTTP_5XX_PRESENT, val_res["reason_codes"])

    def test_live_local_http_probe_and_skeleton_rejection(self) -> None:
        url = f"http://127.0.0.1:{self.port}/dashboard?user=1"
        skeleton = probe_url(url=url, role="manager", environment="DEV", responsive_required=True)
        self.assertEqual(skeleton["route"], "/dashboard?user=1")
        self.assertEqual(skeleton["role"], "manager")
        self.assertEqual(skeleton["environment"], "DEV")
        self.assertTrue(skeleton["responsive_required"])
        self.assertEqual(skeleton["probe"]["status_code"], 200)
        self.assertTrue(skeleton["probe"]["dom_ready"])
        self.assertEqual(len(skeleton["viewports"]), 2)  # desktop + mobile

        # Verify that unexecuted raw skeleton fails validation as required
        val_res = validate_report(skeleton)
        self.assertFalse(val_res["valid"], "Raw skeleton should fail validation before steps are executed")
        self.assertIn(REASON_MISSING_INTERACTIVE_ACTION, val_res["reason_codes"])
        self.assertIn(REASON_ARTIFACT_HASH_MISMATCH, val_res["reason_codes"])

        # When steps are completed and real screenshot evidence is provided, validation passes
        with tempfile.TemporaryDirectory() as td:
            shot_file = Path(td) / "shot.png"
            shot_file.write_bytes(b"\x89PNG_PIXELS")
            shot_hash = hashlib.sha256(b"\x89PNG_PIXELS").hexdigest()

            completed_report = dict(skeleton)
            completed_report["steps"] = [
                {"type": "open", "action": "navigate", "status": "PASS"},
                {"type": "user_action", "action": "click", "status": "PASS"},
                {"type": "visible_response", "action": "assert_visible", "status": "PASS"},
                {"type": "terminal_state", "action": "state_check", "status": "PASS"},
            ]
            completed_report["screenshots"] = [{"path": str(shot_file), "sha256": shot_hash}]
            completed_res = validate_report(completed_report)
            self.assertTrue(completed_res["valid"], f"Completed report failed: {completed_res['errors']}")

    def test_probe_output_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out_file = Path(td) / "skeleton.json"
            skeleton = probe_url(
                f"http://127.0.0.1:{self.port}/api",
                output_path=out_file,
            )
            self.assertTrue(out_file.exists())
            loaded = json.loads(out_file.read_text(encoding="utf-8"))
            self.assertEqual(loaded["route"], "/api")
            self.assertEqual(skeleton["route"], "/api")


class TestCLIAndSubprocess(unittest.TestCase):
    """Test CLI subcommand handling and creation flags."""

    def test_subprocess_creation_flags(self) -> None:
        flags = get_subprocess_creation_flags()
        if sys.platform == "win32":
            self.assertEqual(flags, 0x08000000)
        else:
            self.assertEqual(flags, 0)

    def test_cli_validate_report_exit_codes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            img_file = Path(td) / "cli_screen.png"
            img_file.write_bytes(b"CLI_SCREEN_BYTES")
            img_hash = hashlib.sha256(b"CLI_SCREEN_BYTES").hexdigest()

            valid_file = Path(td) / "valid.json"
            valid_file.write_text(
                json.dumps(
                    {
                        "route": "/home",
                        "role": "user",
                        "environment": "DEV",
                        "steps": [
                            {"type": "open", "status": "PASS"},
                            {"type": "user_action", "status": "PASS"},
                            {"type": "visible_response", "status": "PASS"},
                            {"type": "terminal_state", "status": "PASS"},
                        ],
                        "console_errors": 0,
                        "http_5xx_count": 0,
                        "screenshots": [{"path": str(img_file), "sha256": img_hash}],
                    }
                ),
                encoding="utf-8",
            )

            # Valid report exits 0
            code = main(["validate-report", str(valid_file)])
            self.assertEqual(code, 0)

            invalid_file = Path(td) / "invalid.json"
            invalid_file.write_text(
                json.dumps(
                    {
                        "route": "/home",
                        "role": "user",
                        "environment": "DEV",
                        "console_errors": 5,
                    }
                ),
                encoding="utf-8",
            )

            # Invalid report exits 1
            code_inv = main(["validate-report", str(invalid_file), "--json"])
            self.assertEqual(code_inv, 1)

    def test_cli_probe_url(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out_path = Path(td) / "probe_out.json"
            code = main(
                [
                    "probe-url",
                    "http://127.0.0.1:59997/test",
                    "--role",
                    "admin",
                    "--environment",
                    "TEST",
                    "-o",
                    str(out_path),
                ]
            )
            self.assertEqual(code, 0)
            self.assertTrue(out_path.exists())


if __name__ == "__main__":
    unittest.main()
