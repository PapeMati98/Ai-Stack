"""Status / dry-run tests."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from caretaker.config import load_config
from caretaker.status import dry_run_plan, status_report
from caretaker.store import Store

from ._helpers import FIXTURES, write_config


class DryRunTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg_path = write_config(Path(self.tmp.name))
        self.cfg = load_config(str(self.cfg_path))

    def tearDown(self):
        self.tmp.cleanup()

    def test_dry_run_plan(self):
        plan = dry_run_plan(self.cfg)
        self.assertTrue(plan["dry_run"])
        self.assertEqual(plan["project_slug"], self.cfg.project_slug)
        self.assertIn("codex", plan["allowlist"]["tools"])
        self.assertIsNotNone(plan["fallback"])
        self.assertEqual(plan["fallback"]["subscription_only"], True)
        self.assertIn("http-meta", plan["fallback"]["forbidden_backends"])
        self.assertEqual(len(plan["inputs"]), 4)


class StatusReportTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.tmp.name) / "s.sqlite"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_empty_summary(self):
        summary = status_report(self.store)
        self.assertEqual(summary["runs"], 0)
        self.assertEqual(summary["events_total"], 0)
        self.assertEqual(summary["outbox_pending"], [])

    def test_status_after_run(self):
        self.store.begin_run("r1", "cfg-hash", dry_run=False)
        self.store.finish_run("r1")
        self.store.upsert_cursor(tool="codex", native_session_id="s",
                                 cursor="c", observed_at="t",
                                 content_hash="h")
        self.store.outbox_put(dedup_id="d", payload_hash="h",
                              status="offline",
                              recorded_at="t",
                              request={"a": 1}, reason="local_outage")
        self.store.raise_incident(run_id="r1", kind="local_outage",
                                  message="boom")
        summary = status_report(self.store)
        self.assertEqual(summary["runs"], 1)
        self.assertEqual(summary["cursors"], 1)
        self.assertIn("offline", summary["outbox"])
        self.assertEqual(len(summary["outbox_pending"]), 1)


if __name__ == "__main__":
    unittest.main()
