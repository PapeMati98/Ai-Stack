"""Regression tests for the run.py --once exit code contract.

The run-once CLI must exit non-zero whenever the pipeline raised an
outage or hub-side incident.  The durable outbox is left intact so the
next run can resume; the exit code only signals "did the pipeline
complete cleanly?".
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from caretaker.office import (FakeOfficeTransport, OfficeConnector,
                              OfficeDedupStore, OfficeIdentity,
                              OfficeSendResult, RateLimiter)
from caretaker.pipeline import PipelineDeps
from caretaker.store import Store
from caretaker.transports import FakeTransport

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "run.py"


def _invoke_run(config_path, *, worktree_root="", deps_factory=None):
    """Invoke ``run.py main`` in-process so we can inspect exit codes."""
    if deps_factory is None:
        deps_factory = lambda: PipelineDeps(
            primary=FakeTransport(name="p", responses=[]),
            fallback=None,
            gateway=_FakeGateway(),
            office=_office(),
            worktree_root=worktree_root,
        )
    argv = ["--config", str(config_path), "--once"]
    from run import main
    with patch("run.build_default_deps", return_value=deps_factory()):
        return main(argv)


class _FakeGateway:

    def health(self):
        from caretaker.hub import HealthResult
        return HealthResult(status="pass")

    def lint(self):
        from caretaker.hub import LintResult
        return LintResult(status="pass")

    def inventory(self, *, project_slug, query, limit):
        from caretaker.hub import InventoryResult
        return InventoryResult(items=[], raw={})

    def begin_task(self, *, project_slug, agent, session_id, require_context,
                    limit):
        from caretaker.hub import BeginTaskResult
        return BeginTaskResult(receipt_path="r",
                                raw={"receipt_path": "r"})

    def verify_context(self, *, context_receipt, project_slug, agent,
                        session_id):
        from caretaker.hub import VerifyContextResult
        return VerifyContextResult(status="ok", raw={})

    def record_handoff(self, **kwargs):
        from caretaker.hub import RecordHandoffResult
        return RecordHandoffResult(status="recorded",
                                    raw={"synthetic": True}, synthetic=True)


def _office():
    return OfficeConnector(
        identity=OfficeIdentity(harness="h", host="h", session_id="sid"),
        transport=FakeOfficeTransport(responses=[
            OfficeSendResult(status="sent", message_id="m1"),
        ]),
        dedup=OfficeDedupStore(),
        rate=RateLimiter(max_per_minute=60),
    )


class RunExitCodeTests(unittest.TestCase):

    def test_config_error_returns_2(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            rc = __import__("run").main(
                ["--config",
                 str(Path(tmp.name) / "missing.json"), "--dry-run"])
            self.assertEqual(rc, 2)
        finally:
            tmp.cleanup()

    def test_dry_run_returns_zero(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            from ._helpers import write_config
            cfg_path = write_config(Path(tmp.name))
            rc = __import__("run").main(
                ["--config", str(cfg_path), "--dry-run"])
            self.assertEqual(rc, 0)
        finally:
            tmp.cleanup()

    def test_local_outage_returns_1_with_pending_queue(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            from ._helpers import write_config
            from caretaker.config import load_config
            cfg_path = write_config(Path(tmp.name))
            cfg = load_config(cfg_path)
            # ``run.main`` owns the configured state path; inspect that same
            # durable store instead of an unrelated test database.
            store = Store(str(Path(cfg.state_dir) / "caretaker.sqlite"))
            try:
                # Build a gateway that lets the run proceed but a primary
                # transport that always raises TransportError.
                primary = FakeTransport(name="p", responses=[],
                                         raise_on={0})

                def _deps():
                    return PipelineDeps(primary=primary, fallback=None,
                                        gateway=_FakeGateway(),
                                        office=_office(),
                                        worktree_root="")
                from run import main
                with patch("run.build_default_deps", return_value=_deps()):
                    rc = main(["--config", str(cfg_path),
                                "--once"])
                self.assertEqual(rc, 1)
                # The queue must remain pending (no auto-clean).
                self.assertTrue(store.has_open_offline_outbox())
            finally:
                store.close()
        finally:
            tmp.cleanup()

    def test_no_input_returns_zero(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            from ._helpers import write_config
            from dataclasses import replace
            from caretaker.config import InputSpec
            from caretaker.office import FakeOfficeTransport
            from caretaker.transports import Envelope
            cfg_path = write_config(Path(tmp.name))
            cfg = __import__("caretaker.config",
                              fromlist=["load_config"]).load_config(cfg_path)
            # Empty input file => no_input path.
            empty = Path(tmp.name) / "empty.jsonl"
            empty.write_text("", "utf-8")
            cfg = replace(cfg, inputs=[
                InputSpec(str(empty), "normalized_events", "openclaw"),
            ])
            cfg_path.write_text(json.dumps({
                **json.loads(cfg_path.read_text("utf-8")),
                "inputs": [{"path": str(empty), "kind": "normalized_events",
                              "tool": "openclaw"}],
            }), "utf-8")
            primary = FakeTransport(name="p", responses=[
                Envelope(ok=True, status="ok", final="{}", model="x",
                          provider="y"),
            ])
            from run import main
            with patch("run.build_default_deps", return_value=PipelineDeps(
                    primary=primary, fallback=None, gateway=_FakeGateway(),
                    office=_office(), worktree_root="")):
                rc = main(["--config", str(cfg_path),
                            "--once"])
            self.assertEqual(rc, 0)
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
