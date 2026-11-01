from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "remote_computer.py"
SPEC = importlib.util.spec_from_file_location("remote_computer", MODULE_PATH)
assert SPEC and SPEC.loader
REMOTE_COMPUTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REMOTE_COMPUTER)


def valid_contract() -> dict:
    return {
        "schema_version": 1,
        "task_id": "test-pilot",
        "mode": "browser",
        "headless": True,
        "side_effects": "none",
        "timeout_seconds": 60,
        "target": {"kind": "fixture", "value": "pilot.html"},
        "actions": [
            {"type": "assert_visible", "selector": "#increment"},
            {"type": "click", "selector": "#increment"},
            {"type": "screenshot", "name": "after-click"},
        ],
    }


def valid_desktop_contract() -> dict:
    return {
        "schema_version": 2,
        "task_id": "desktop-pilot",
        "mode": "desktop",
        "headless": False,
        "side_effects": "test-only",
        "capability": "uat",
        "timeout_seconds": 60,
        "target": {"kind": "app", "app_id": "notepad"},
        "actions": [
            {"type": "launch", "app_id": "notepad"},
            {"type": "text", "value": "canary"},
            {"type": "screenshot", "name": "typed"},
            {"type": "close_target"},
        ],
    }


def valid_browser_uat_contract() -> dict:
    return {
        "schema_version": 3,
        "task_id": "example-project-uat",
        "mode": "browser",
        "headless": True,
        "side_effects": "test-only",
        "capability": "origin-uat",
        "timeout_seconds": 120,
        "target": {"kind": "url", "value": "http://127.0.0.1:13102/login"},
        "actions": [
            {"type": "click", "selector": "#login-dev-superadmin"},
            {
                "type": "set_fixture_file",
                "selector": "input[type=file]",
                "fixture_id": "example-synthetic-2026-07",
                "fixture_sha256": "a" * 64,
            },
            {"type": "assert_text", "selector": "[role=status]", "contains": "Zaimportowano"},
        ],
    }


class ContractTests(unittest.TestCase):
    def test_user_perception_uat_is_normative_without_runner_expansion(self) -> None:
        skill_root = MODULE_PATH.parents[1]
        skill = (skill_root / "SKILL.md").read_text(encoding="utf-8")
        reference = (skill_root / "references" / "user-perception-uat.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("references/user-perception-uat.md", skill)
        self.assertIn("mandatory PASS/FAIL scenarios", skill)
        for phrase in (
            "Surface reachability",
            "Perceived action feedback",
            "Visible failure and recovery",
            "injected condition",
            "Screenshots alone cannot prove timing or backend truth",
            "never target production or weaken the VM policy",
        ):
            self.assertIn(phrase, reference)

    def test_json_output_is_safe_for_windows_cp1250_console(self) -> None:
        raw_output = io.BytesIO()
        output = io.TextIOWrapper(raw_output, encoding="cp1250", errors="strict")

        REMOTE_COMPUTER._print_json(
            {"status": "FAIL", "error": "locator hidden \u2192 no upload"},
            stream=output,
        )
        output.flush()

        serialized = raw_output.getvalue().decode("cp1250")
        self.assertEqual(
            json.loads(serialized),
            {"status": "FAIL", "error": "locator hidden \u2192 no upload"},
        )

    def test_origin_uat_allows_bounded_select_option_without_enabling_text_input(self) -> None:
        contract = valid_browser_uat_contract()
        contract["actions"] = [
            {
                "type": "select_option",
                "selector": 'select[aria-label="Okres rozliczeń"]',
                "value": "2026-08-01_2026-08-27",
            }
        ]

        validated = REMOTE_COMPUTER.validate_contract(contract)
        self.assertEqual(validated["actions"][0]["type"], "select_option")

        read_only = valid_contract()
        read_only["actions"] = contract["actions"]
        with self.assertRaisesRegex(REMOTE_COMPUTER.ClientError, "unsupported"):
            REMOTE_COMPUTER.validate_contract(read_only)

        contract["actions"][0]["value"] = "x" * 129
        with self.assertRaisesRegex(REMOTE_COMPUTER.ClientError, "value"):
            REMOTE_COMPUTER.validate_contract(contract)

    def test_valid_contract(self) -> None:
        result = REMOTE_COMPUTER.validate_contract(valid_contract())
        self.assertEqual(result["task_id"], "test-pilot")

    def test_rejects_extra_top_level_field(self) -> None:
        contract = valid_contract()
        contract["command"] = "whoami"
        with self.assertRaises(REMOTE_COMPUTER.ClientError):
            REMOTE_COMPUTER.validate_contract(contract)

    def test_rejects_missing_action_field(self) -> None:
        contract = valid_contract()
        contract["actions"] = [{"type": "click"}]
        with self.assertRaises(REMOTE_COMPUTER.ClientError):
            REMOTE_COMPUTER.validate_contract(contract)

    def test_rejects_unsafe_key(self) -> None:
        contract = valid_contract()
        contract["actions"] = [{"type": "press", "selector": "body", "key": "Control+L"}]
        with self.assertRaises(REMOTE_COMPUTER.ClientError):
            REMOTE_COMPUTER.validate_contract(contract)

    def test_rejects_url_credentials(self) -> None:
        contract = valid_contract()
        contract["target"] = {"kind": "url", "value": "https://user:secret@example.test/"}
        with self.assertRaises(REMOTE_COMPUTER.ClientError):
            REMOTE_COMPUTER.validate_contract(contract)

    def test_network_browser_contract_rejects_input(self) -> None:
        contract = valid_contract()
        contract["target"] = {"kind": "url", "value": "https://staging.example.test/"}
        contract["actions"] = [{"type": "click", "selector": "#submit"}]
        with self.assertRaises(REMOTE_COMPUTER.ClientError):
            REMOTE_COMPUTER.validate_contract(contract)

    def test_accepts_exact_origin_browser_uat_contract_without_paths(self) -> None:
        result = REMOTE_COMPUTER.validate_contract(valid_browser_uat_contract())
        self.assertEqual(result["capability"], "origin-uat")
        self.assertEqual(
            result["actions"][1]["fixture_id"],
            "example-synthetic-2026-07",
        )

    def test_browser_uat_rejects_contract_controlled_paths_and_traversal_ids(self) -> None:
        contract = valid_browser_uat_contract()
        contract["actions"][1]["path"] = r"C:\Users\Example\statement.txt"
        with self.assertRaises(REMOTE_COMPUTER.ClientError):
            REMOTE_COMPUTER.validate_contract(contract)

        contract = valid_browser_uat_contract()
        contract["actions"][1]["fixture_id"] = "../statement"
        with self.assertRaises(REMOTE_COMPUTER.ClientError):
            REMOTE_COMPUTER.validate_contract(contract)

    def test_load_contract_requires_utf8_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "contract.json"
            path.write_text(json.dumps(valid_contract()), encoding="utf-8")
            self.assertEqual(REMOTE_COMPUTER.load_contract(path)["mode"], "browser")

    def test_accepts_desktop_uat_contract(self) -> None:
        result = REMOTE_COMPUTER.validate_contract(valid_desktop_contract())
        self.assertEqual(result["capability"], "uat")

    def test_observe_rejects_input_actions(self) -> None:
        contract = valid_desktop_contract()
        contract.update(
            capability="observe",
            side_effects="none",
            target={"kind": "desktop", "app_id": "desktop"},
            actions=[{"type": "text", "value": "no"}],
        )
        with self.assertRaises(REMOTE_COMPUTER.ClientError):
            REMOTE_COMPUTER.validate_contract(contract)

    def test_desktop_rejects_unsafe_key(self) -> None:
        contract = valid_desktop_contract()
        contract["actions"] = [
            {"type": "launch", "app_id": "notepad"},
            {"type": "key", "key": "F12", "modifiers": []},
        ]
        with self.assertRaises(REMOTE_COMPUTER.ClientError):
            REMOTE_COMPUTER.validate_contract(contract)


class ArtifactPathTests(unittest.TestCase):
    def test_accepts_exact_remote_artifact_path(self) -> None:
        run_id = "20260825T052234123Z-abc12345"
        path = (
            "C:\\ProgramData\\MateuszAI\\remote-computer-use"
            f"\\artifacts\\test-pilot\\{run_id}"
        )
        result = REMOTE_COMPUTER.validate_remote_artifact_dir(path, "test-pilot", run_id)
        self.assertTrue(result.endswith(run_id))

    def test_rejects_remote_path_escape(self) -> None:
        run_id = "20260825T052234123Z-abc12345"
        with self.assertRaises(REMOTE_COMPUTER.ClientError):
            REMOTE_COMPUTER.validate_remote_artifact_dir(
                f"C:/Users/Example/AppData/Local/Temp/{run_id}", "test-pilot", run_id
            )

    def test_origin_uat_download_never_requests_privacy_sensitive_browser_artifacts(self) -> None:
        requested: list[str] = []

        def capture_run(arguments, *_args, **_kwargs):
            requested.append(" ".join(str(value) for value in arguments))
            return mock.Mock(returncode=1)

        with tempfile.TemporaryDirectory() as directory:
            local_dir = Path(directory) / "evidence"
            with mock.patch.object(REMOTE_COMPUTER, "_run", side_effect=capture_run):
                REMOTE_COMPUTER._download_artifacts(
                    "ai-lab-uat-vm",
                    "C:/ProgramData/MateuszAI/remote-computer-use/artifacts/test/run",
                    local_dir,
                    valid_browser_uat_contract(),
                    "FAIL",
                )

        joined = "\n".join(requested)
        self.assertNotIn("trace.zip", joined)
        self.assertNotIn("final.png", joined)
        self.assertNotIn("failure.png", joined)
        self.assertNotIn(".png", joined)


class RunStatusTests(unittest.TestCase):
    def test_origin_uat_upstream_requires_exact_example_dev_fingerprint(self) -> None:
        payloads = {
            "/api/health": {
                "status": "ok",
                "version": "1.0",
                "app": "Example Platform API",
            },
            "/api/system/runtime": {
                "environment": "DEV",
                "api_port": "8010",
                "database_name": "example_dev.db",
                "database_path_basename": "example_dev.db",
                "git_commit": "fcd5b01",
                "frontend_expected_api_host": "127.0.0.1:8010",
                "guard": {"mode": "pass", "risks": []},
            },
        }

        def fetch(path: str) -> dict:
            return payloads[path]

        with mock.patch.object(
            REMOTE_COMPUTER,
            "_fetch_origin_uat_json",
            side_effect=fetch,
        ):
            REMOTE_COMPUTER._assert_origin_uat_local_upstream("fcd5b01")
            with self.assertRaises(REMOTE_COMPUTER.ClientError):
                REMOTE_COMPUTER._assert_origin_uat_local_upstream("deadbee")

    def test_origin_uat_lock_records_exact_tunnel_pid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "origin.lock"
            with mock.patch.object(
                REMOTE_COMPUTER,
                "_origin_uat_lock_path",
                return_value=lock_path,
            ):
                path, descriptor = REMOTE_COMPUTER._acquire_origin_uat_lock(
                    "example-project-uat",
                    "uat-1234567890abcdef",
                )
                try:
                    REMOTE_COMPUTER._record_origin_uat_tunnel_pid(descriptor, 4242)
                    metadata = json.loads(path.read_text(encoding="utf-8"))
                    self.assertEqual(metadata["tunnel_process_id"], 4242)
                    self.assertEqual(metadata["task_id"], "example-project-uat")
                finally:
                    REMOTE_COMPUTER._release_origin_uat_lock(
                        path,
                        descriptor,
                        remove=True,
                    )

    def test_direct_run_rejects_origin_uat_before_remote_access(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            contract_path = Path(directory) / "contract.json"
            contract_path.write_text(
                json.dumps(valid_browser_uat_contract()), encoding="utf-8"
            )
            arguments = argparse.Namespace(contract=contract_path, host="ai-lab-uat-vm")
            with mock.patch.object(REMOTE_COMPUTER, "_remote_powershell") as remote:
                with self.assertRaises(REMOTE_COMPUTER.ClientError) as raised:
                    REMOTE_COMPUTER.command_run(arguments)
            self.assertIn("run-origin-uat", str(raised.exception))
            remote.assert_not_called()

    def test_doctor_accepts_windows_ssh_mapped_not_ready_exit(self) -> None:
        arguments = argparse.Namespace(host="ai-lab-windows-vm", mode="desktop")
        output = io.StringIO()
        with mock.patch.object(
            REMOTE_COMPUTER,
            "_remote_powershell",
            return_value={"status": "NOT_READY", "desktop_status": "DISABLED"},
        ) as remote, redirect_stdout(output):
            exit_code = REMOTE_COMPUTER.command_doctor(arguments)

        self.assertEqual(exit_code, 3)
        self.assertEqual(json.loads(output.getvalue())["desktop_status"], "DISABLED")
        self.assertIn(1, remote.call_args.args[3])

    def test_desktop_run_fails_before_upload_when_vm_is_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            contract_path = Path(directory) / "contract.json"
            contract_path.write_text(json.dumps(valid_desktop_contract()), encoding="utf-8")
            arguments = argparse.Namespace(contract=contract_path, host="ai-lab-windows-vm")
            with (
                mock.patch.object(
                    REMOTE_COMPUTER,
                    "_remote_powershell",
                    return_value={"status": "NOT_READY", "desktop_status": "DISABLED"},
                ),
                mock.patch.object(REMOTE_COMPUTER, "_run") as upload,
            ):
                with self.assertRaises(REMOTE_COMPUTER.ClientError) as raised:
                    REMOTE_COMPUTER.command_run(arguments)
            self.assertEqual(raised.exception.exit_code, 3)
            upload.assert_not_called()

    def test_busy_is_reported_without_artifact_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            contract_path = Path(directory) / "contract.json"
            contract_path.write_text(json.dumps(valid_contract()), encoding="utf-8")
            arguments = argparse.Namespace(contract=contract_path, host="ai-lab-windows-vm")
            output = io.StringIO()
            with (
                mock.patch.object(REMOTE_COMPUTER, "_run"),
                mock.patch.object(
                    REMOTE_COMPUTER,
                    "_remote_powershell",
                    return_value={"action": "run", "status": "BUSY", "lease": {"status": "HELD"}},
                ),
                mock.patch.object(REMOTE_COMPUTER, "_remove_remote_contract"),
                mock.patch.object(REMOTE_COMPUTER, "validate_remote_artifact_dir") as artifact_check,
                redirect_stdout(output),
            ):
                exit_code = REMOTE_COMPUTER.command_run(arguments)
            self.assertEqual(exit_code, 4)
            self.assertEqual(json.loads(output.getvalue())["status"], "BUSY")
            artifact_check.assert_not_called()

    def test_origin_uat_reserves_before_tunnel_and_always_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            contract_path = Path(directory) / "contract.json"
            contract_path.write_text(
                json.dumps(valid_browser_uat_contract()), encoding="utf-8"
            )
            arguments = argparse.Namespace(
                contract=contract_path,
                host="ai-lab-uat-vm",
                expected_upstream_commit="fcd5b01",
            )
            events: list[str] = []
            remote_responses = iter(
                [
                    {"status": "RESERVED"},
                    {"status": "READY", "addresses": ["127.0.0.1"], "listen_count": 1},
                    {"status": "CLOSED", "addresses": [], "listen_count": 0},
                    {"status": "RELEASED"},
                ]
            )

            def remote_side_effect(*_args, **_kwargs):
                response = next(remote_responses)
                events.append(response["status"])
                return response

            def run_side_effect(run_arguments):
                self.assertTrue(run_arguments.reservation_id.startswith("uat-"))
                events.append("RUN")
                print(json.dumps({"status": "PASS", "run_id": "test-run", "lease_status": "HELD"}))
                return 0

            output = io.StringIO()
            with (
                mock.patch.object(REMOTE_COMPUTER, "_assert_origin_uat_local_upstream"),
                mock.patch.object(
                    REMOTE_COMPUTER,
                    "_acquire_origin_uat_lock",
                    return_value=(Path(directory) / "lock", 7),
                ),
                mock.patch.object(REMOTE_COMPUTER, "_release_origin_uat_lock") as release_lock,
                mock.patch.object(
                    REMOTE_COMPUTER,
                    "_remote_powershell",
                    side_effect=remote_side_effect,
                ),
                mock.patch.object(
                    REMOTE_COMPUTER,
                    "_start_origin_uat_tunnel",
                    side_effect=lambda _host: events.append("TUNNEL_START") or mock.Mock(pid=4242),
                ),
                mock.patch.object(REMOTE_COMPUTER, "_record_origin_uat_tunnel_pid"),
                mock.patch.object(
                    REMOTE_COMPUTER,
                    "_stop_origin_uat_tunnel",
                    side_effect=lambda _process: events.append("TUNNEL_STOP") or True,
                ),
                mock.patch.object(REMOTE_COMPUTER, "command_run", side_effect=run_side_effect),
                redirect_stdout(output),
            ):
                exit_code = REMOTE_COMPUTER.command_run_origin_uat(arguments)

            self.assertEqual(exit_code, 0)
            self.assertEqual(
                events,
                ["RESERVED", "TUNNEL_START", "READY", "RUN", "TUNNEL_STOP", "CLOSED", "RELEASED"],
            )
            result = json.loads(output.getvalue())
            self.assertEqual(result["origin_transport"]["cleanup"], "COMPLETE")
            release_lock.assert_called_once_with(
                Path(directory) / "lock",
                7,
                remove=True,
            )

    def test_origin_uat_preserves_host_lock_and_remote_lease_when_listener_survives(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            contract_path = Path(directory) / "contract.json"
            contract_path.write_text(
                json.dumps(valid_browser_uat_contract()), encoding="utf-8"
            )
            arguments = argparse.Namespace(
                contract=contract_path,
                host="ai-lab-uat-vm",
                expected_upstream_commit="fcd5b01",
            )
            remote_responses = iter(
                [
                    {"status": "RESERVED"},
                    {"status": "READY", "addresses": ["127.0.0.1"], "listen_count": 1},
                    {"status": "READY", "addresses": ["127.0.0.1"], "listen_count": 1},
                    {"status": "RELEASED"},
                ]
            )

            output = io.StringIO()
            with (
                mock.patch.object(REMOTE_COMPUTER, "_assert_origin_uat_local_upstream"),
                mock.patch.object(
                    REMOTE_COMPUTER,
                    "_acquire_origin_uat_lock",
                    return_value=(Path(directory) / "lock", 7),
                ),
                mock.patch.object(REMOTE_COMPUTER, "_release_origin_uat_lock") as release_lock,
                mock.patch.object(
                    REMOTE_COMPUTER,
                    "_remote_powershell",
                    side_effect=lambda *_args, **_kwargs: next(remote_responses),
                ) as remote,
                mock.patch.object(
                    REMOTE_COMPUTER,
                    "_start_origin_uat_tunnel",
                    return_value=mock.Mock(pid=4242),
                ),
                mock.patch.object(REMOTE_COMPUTER, "_record_origin_uat_tunnel_pid"),
                mock.patch.object(REMOTE_COMPUTER, "_stop_origin_uat_tunnel", return_value=True),
                mock.patch.object(
                    REMOTE_COMPUTER,
                    "command_run",
                    side_effect=lambda _args: print(
                        json.dumps({"status": "PASS", "run_id": "test-run", "lease_status": "HELD"})
                    ) or 0,
                ),
                redirect_stdout(output),
            ):
                exit_code = REMOTE_COMPUTER.command_run_origin_uat(arguments)

            self.assertEqual(exit_code, 1)
            release_lock.assert_called_once_with(
                Path(directory) / "lock",
                7,
                remove=False,
            )
            release_calls = [
                call for call in remote.call_args_list
                if "release-origin-uat" in call.args[1]
            ]
            self.assertEqual(release_calls, [])
            result = json.loads(output.getvalue())
            self.assertEqual(result["origin_transport"]["cleanup"], "INCOMPLETE")

    def test_origin_uat_rejects_wrong_host_and_origin_before_transport(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            contract_path = Path(directory) / "contract.json"
            contract_path.write_text(
                json.dumps(valid_browser_uat_contract()), encoding="utf-8"
            )
            with mock.patch.object(REMOTE_COMPUTER, "_assert_origin_uat_local_upstream") as upstream:
                with self.assertRaises(REMOTE_COMPUTER.ClientError):
                    REMOTE_COMPUTER.command_run_origin_uat(
                        argparse.Namespace(contract=contract_path, host="ai-lab-windows-vm")
                    )
                upstream.assert_not_called()

            contract = valid_browser_uat_contract()
            contract["target"]["value"] = "http://127.0.0.1:13103/login"
            contract_path.write_text(json.dumps(contract), encoding="utf-8")
            with mock.patch.object(REMOTE_COMPUTER, "_assert_origin_uat_local_upstream") as upstream:
                with self.assertRaises(REMOTE_COMPUTER.ClientError):
                    REMOTE_COMPUTER.command_run_origin_uat(
                        argparse.Namespace(contract=contract_path, host="ai-lab-uat-vm")
                    )
                upstream.assert_not_called()


if __name__ == "__main__":
    unittest.main()
