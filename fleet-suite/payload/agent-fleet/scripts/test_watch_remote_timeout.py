import subprocess
import unittest
from unittest import mock

import fleet


class WatchRemoteTimeoutTests(unittest.TestCase):
    def _invoke(self, argv):
        with mock.patch.object(fleet.subprocess, "run") as run:
            run.side_effect = subprocess.TimeoutExpired(cmd=argv, timeout=1)
            telemetry, payload = fleet._invoke_watch_office([], command_info=(argv, None))
        return telemetry, payload, run

    def test_remote_token_uses_ten_seconds(self):
        argv = ["office.exe", "--remote-config", "path.json"]
        with mock.patch.object(fleet.subprocess, "run", return_value=subprocess.CompletedProcess(argv, 0, "{}", "")) as run:
            fleet._invoke_watch_office([], command_info=(argv, None))
        self.assertEqual(
            10,
            run.call_args.kwargs["timeout"],
        )

    def test_local_uses_default_timeout(self):
        argv = ["office.exe", "read"]
        with mock.patch.object(fleet.subprocess, "run", return_value=subprocess.CompletedProcess(argv, 0, "{}", "")) as run:
            fleet._invoke_watch_office([], command_info=(argv, None))
        self.assertEqual(
            fleet.WATCH_EVENT_OFFICE_TIMEOUT_SECONDS,
            run.call_args.kwargs["timeout"],
        )

    def test_lookalike_token_keeps_local_timeout(self):
        argv = ["office.exe", "--remote-config-extra"]
        with mock.patch.object(fleet.subprocess, "run", return_value=subprocess.CompletedProcess(argv, 0, "{}", "")) as run:
            fleet._invoke_watch_office([], command_info=(argv, None))
        self.assertEqual(
            fleet.WATCH_EVENT_OFFICE_TIMEOUT_SECONDS,
            run.call_args.kwargs["timeout"],
        )

    def test_timeout_yields_office_timeout_without_retry(self):
        argv = ["office.exe", "--remote-config", "path.json"]
        telemetry, payload, run = self._invoke(argv)
        self.assertEqual("office-timeout", telemetry["reason"])
        self.assertIsNone(payload)
        self.assertEqual(1, run.call_count)

    def test_message_body_is_not_route_evidence(self):
        argv = ["office.exe"]
        with mock.patch.object(fleet.subprocess, "run", return_value=subprocess.CompletedProcess(argv, 0, "{}", "")) as run:
            fleet._invoke_watch_office(["send", "--body", "--remote-config"], command_info=(argv, None))
        self.assertEqual(fleet.WATCH_EVENT_OFFICE_TIMEOUT_SECONDS, run.call_args.kwargs["timeout"])


if __name__ == "__main__":
    unittest.main()
