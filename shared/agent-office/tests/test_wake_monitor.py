"""W1: a no-model Wake monitor, the global tick heartbeat and a multi-binding relay watch."""

from __future__ import annotations

import io
import json
import unittest
from pathlib import Path
from unittest import mock
from contextlib import redirect_stdout

from support import PACKAGE_ROOT  # noqa: F401  (puts the package on sys.path)

from coordfs.errors import CoordError
from coordfs.leases import format_ts
from coordfs.office import Office
from coordfs.office_cli import main as office_main
from coordfs.wake import Wake
import test_wake_relay as _relay
from test_wake_relay import SENDER, TARGET

OTHER = "claude-desktop-other"
OTHER_LOCAL = "local_223e4567-e89b-12d3-a456-426614174000"


class WakeMonitorTests(unittest.TestCase):
    setUp = _relay.WakeRelayTests.setUp
    wake = _relay.WakeRelayTests.wake
    office = _relay.WakeRelayTests.office
    enroll = _relay.WakeRelayTests.enroll

    def enroll_other(self, wake: Wake) -> None:
        wake.enroll(
            binding_id="other-binding", office_agent=OTHER, driver="claude-desktop-relay",
            native_thread_id=OTHER_LOCAL, workspace=str(self.workspace), enrolled_by="owner",
            allowed_wakers=[SENDER], allowed_triggers=["message"], wake_kinds=["question", "handoff"],
            budget_per_day=6, expires_at=format_ts(self.clock[0] + 30 * 86400), owner_confirm=True,
        )
        wake.verify("other-binding", owner_confirm=True)
        Office(agent_id=OTHER, home=self.home)._remember_canonical_agent()

    def kinds(self, result: dict) -> list[str]:
        return [finding["kind"] for finding in result["findings"]]

    def test_tick_heartbeat_missing_then_fresh_then_stale(self) -> None:
        wake = self.wake()
        self.assertIn("tick_missing", self.kinds(wake.monitor()))
        wake.tick()
        self.assertNotIn("tick_missing", self.kinds(wake.monitor()))
        self.assertNotIn("tick_stale", self.kinds(wake.monitor()))
        wake.tick(binding_id=None)
        self.clock[0] += 6 * 60
        self.assertIn("tick_stale", self.kinds(wake.monitor()))

    def test_a_scoped_tick_is_not_a_heartbeat(self) -> None:
        wake = self.wake()
        self.enroll(wake, triggers=["message"])
        wake.tick(binding_id="desktop-binding")
        self.assertIn("tick_missing", self.kinds(wake.monitor()))

    def test_stop_is_reported(self) -> None:
        wake = self.wake()
        wake.tick()
        wake.stop()
        self.assertIn("stop_active", self.kinds(wake.monitor()))

    def test_a_lapsed_watcher_pulse_is_reported(self) -> None:
        wake = self.wake()
        self.enroll(wake, triggers=["message"])
        wake.tick()
        self.office(SENDER).send(TARGET, "pytanie", kind="question")
        wake.relay_watch(binding_id="desktop-binding", sleep=lambda _s: None, monotonic=lambda: self.clock[0])
        wake.tick()
        self.assertNotIn("relay_watch_lapsed", self.kinds(wake.monitor()))
        self.clock[0] += 600 + 301
        wake.tick()
        self.assertIn("relay_watch_lapsed", self.kinds(wake.monitor()))

    def test_ledger_losses_are_reported_once_and_history_is_not_replayed(self) -> None:
        wake = self.wake()
        self.enroll(wake, triggers=["message"])
        self.office(SENDER).send(TARGET, "stare", kind="question")
        wake.tick()
        wake.stop()
        wake.relay_pending()  # an old loss: STOP cancels the pending dispatch
        wake.start()
        first = wake.monitor()  # first pass starts at the end of the ledger
        self.assertFalse([k for k in self.kinds(first) if k.startswith("wake_")])
        self.clock[0] += 60
        self.office(SENDER).send(TARGET, "nowe", kind="question")
        wake.tick()
        wake.stop()
        wake.relay_pending()  # a new loss after the first pass
        wake.start()
        second = wake.monitor()
        losses = [k for k in self.kinds(second) if k.startswith("wake_")]
        self.assertEqual(losses, ["wake_cancelled"], second)
        third = wake.monitor()
        self.assertFalse([k for k in self.kinds(third) if k.startswith("wake_")])

    def test_a_damaged_cursor_restarts_at_the_end_without_crashing(self) -> None:
        wake = self.wake()
        wake.tick()
        for bad in (-1, "abc", True, 10**12):
            wake.monitor_state_path.write_text(json.dumps({"ledger_offset": bad}), encoding="utf-8")
            result = wake.monitor()
            self.assertIn("monitor_cursor_reset", self.kinds(result), bad)
        wake.monitor_state_path.write_text("{broken", encoding="utf-8")
        self.assertIn("monitor_cursor_reset", self.kinds(wake.monitor()))
        self.assertNotIn("monitor_cursor_reset", self.kinds(wake.monitor()))

    def test_a_large_backlog_is_read_in_chunks_without_losing_events(self) -> None:
        wake = self.wake()
        wake.monitor()  # cursor at the end (the ledger does not exist yet)
        rows = [{"decision": "cancelled", "reason": "target_busy_limit", "binding_id": "b",
                 "dispatch_id": f"{i:032x}", "attempt_id": f"a{i}", "pad": "x" * 200} for i in range(40)]
        wake.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with wake.ledger_path.open("ab") as handle:
            for row in rows:
                handle.write((json.dumps(row) + "\n").encode("utf-8"))
        seen: list[str] = []
        with mock.patch("coordfs.wake.MONITOR_LEDGER_CHUNK_BYTES", 2048):
            for _ in range(20):
                result = wake.monitor()
                seen.extend(f["key"] for f in result["findings"] if f["kind"] == "wake_cancelled")
                if "monitor_backlog" not in self.kinds(result):
                    break
        self.assertEqual(seen, [f"wake_cancelled:a{i}" for i in range(40)])

    def test_an_expected_watcher_that_never_started_is_reported(self) -> None:
        wake = self.wake()
        self.enroll(wake, triggers=["message"])
        wake.tick()
        with self.assertRaises(CoordError):
            wake.relay_watch_expect(binding_id="desktop-binding")  # owner confirmation required
        wake.relay_watch_expect(binding_id="desktop-binding", owner_confirm=True)
        self.assertIn("relay_watch_missing", self.kinds(wake.monitor()))
        wake.relay_watch(binding_id="desktop-binding", timeout_s=1, sleep=lambda _s: None,
                         monotonic=lambda: self.clock[0])
        self.assertNotIn("relay_watch_missing", self.kinds(wake.monitor()))
        (wake.relay_watch_dir / "desktop-binding.json").write_text("{}", encoding="utf-8")  # pulse lost
        self.assertIn("relay_watch_missing", self.kinds(wake.monitor()))
        wake.revoke("desktop-binding")
        self.assertNotIn("relay_watch_missing", self.kinds(wake.monitor()))
        off = wake.relay_watch_expect(binding_id="desktop-binding", enabled=False, owner_confirm=True)
        self.assertEqual(off["binding_ids"], [])

    def test_expectations_of_several_bindings_survive_and_a_damaged_marker_is_never_overwritten(self) -> None:
        wake = self.wake()
        self.enroll(wake, triggers=["message"])
        self.enroll_other(wake)
        wake.relay_watch_expect(binding_id="desktop-binding", owner_confirm=True)
        both = wake.relay_watch_expect(binding_id="other-binding", owner_confirm=True)
        self.assertEqual(both["binding_ids"], ["desktop-binding", "other-binding"])
        one = wake.relay_watch_expect(binding_id="desktop-binding", enabled=False, owner_confirm=True)
        self.assertEqual(one["binding_ids"], ["other-binding"])
        wake.relay_watch_expected_path.write_text("{damaged", encoding="utf-8")
        for enabled in (True, False):
            with self.assertRaises(CoordError) as caught:
                wake.relay_watch_expect(binding_id="desktop-binding", enabled=enabled, owner_confirm=True)
            self.assertEqual(caught.exception.code, "wake_expect_invalid")
            self.assertEqual(wake.relay_watch_expected_path.read_text(encoding="utf-8"), "{damaged")
        self.assertIn("relay_watch_expected_unreadable", self.kinds(wake.monitor()))
        recovered = wake.relay_watch_expect(binding_id="desktop-binding", owner_confirm=True, recover=True)
        self.assertEqual(recovered["binding_ids"], ["desktop-binding"])
        self.assertEqual(Path(recovered["moved_aside"]).read_text(encoding="utf-8"), "{damaged")
        # A second recovery in the same second (clock frozen) keeps both damaged copies.
        wake.relay_watch_expected_path.write_text("{damaged-2", encoding="utf-8")
        again = wake.relay_watch_expect(binding_id="desktop-binding", owner_confirm=True, recover=True)
        self.assertNotEqual(again["moved_aside"], recovered["moved_aside"])
        self.assertEqual(Path(recovered["moved_aside"]).read_text(encoding="utf-8"), "{damaged")
        self.assertEqual(Path(again["moved_aside"]).read_text(encoding="utf-8"), "{damaged-2")

    def test_rename_no_clobber_refuses_an_existing_target(self) -> None:
        from coordfs.wake import _rename_no_clobber
        src = self.root / "a.txt"; dst = self.root / "b.txt"
        src.write_text("new", encoding="utf-8"); dst.write_text("kept", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            _rename_no_clobber(src, dst)
        self.assertEqual(dst.read_text(encoding="utf-8"), "kept")
        self.assertEqual(src.read_text(encoding="utf-8"), "new")

    def test_cli_runs_without_a_console(self) -> None:
        with mock.patch("sys.stdout", None):
            code = office_main(["--home", str(self.home), "--agent", "office-host-wake-tick", "wake-tick"])
        self.assertEqual(code, 0)
        self.assertTrue(self.wake().tick_heartbeat_path.is_file())

    def test_one_watch_serves_several_bindings(self) -> None:
        wake = self.wake()
        self.enroll(wake, triggers=["message"])
        self.enroll_other(wake)
        self.office(SENDER).send(OTHER, "do innej", kind="question")
        found = wake.relay_watch(binding_id=["desktop-binding", "other-binding"],
                                 sleep=lambda _s: None, monotonic=lambda: self.clock[0])
        self.assertEqual(found["result"], "pending")
        self.assertEqual(found["binding_ids"], ["desktop-binding", "other-binding"])
        self.assertEqual([d["binding_id"] for d in found["dispatches"]], ["other-binding"])
        self.assertEqual(wake._watched_bindings(), frozenset({"desktop-binding", "other-binding"}))

    def test_cli_post_deduplicates_one_incident(self) -> None:
        def run() -> dict:
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = office_main(["--home", str(self.home), "--agent", "office-host-wake-monitor",
                                    "wake-monitor", "--post"])
            self.assertEqual(code, 0)
            return json.loads(stdout.getvalue())

        first = run()
        self.assertIn("tick_missing", self.kinds(first))
        self.assertTrue(all(not row["duplicate"] for row in first["posted"]))
        second = run()
        self.assertTrue(all(row["duplicate"] for row in second["posted"]))
        posts = Office(agent_id="claude-host-reader", home=self.home).read(channel="ops")["messages"]
        self.assertEqual(len(posts), len(first["posted"]))
        self.assertEqual(posts[0]["untrusted_message"]["kind"], "alert")


if __name__ == "__main__":
    unittest.main()
