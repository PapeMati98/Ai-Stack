"""Store tests."""

import tempfile
import unittest
from pathlib import Path

from caretaker.store import Store


class StoreTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.tmp.name) / "s.sqlite"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_cursors_dedup(self):
        self.store.upsert_cursor(tool="codex", native_session_id="s1",
                                 cursor="c1", observed_at="t",
                                 content_hash="h")
        self.store.upsert_cursor(tool="codex", native_session_id="s1",
                                 cursor="c1", observed_at="t",
                                 content_hash="h")
        row = self.store._conn.execute(
            "SELECT COUNT(*) AS c FROM cursors").fetchone()
        self.assertEqual(row["c"], 1)

    def test_event_seen(self):
        eid = self.store.insert_event(
            run_id="r", identity_tool="codex", source="native",
            native_session_id="s", cursor="c", observed_at="t",
            title="t", excerpt="e", content_hash="h",
            filter_reason="accepted",
        )
        self.assertGreater(eid, 0)
        self.assertTrue(self.store.event_seen("h"))

    def test_outbox_publish_then_pending(self):
        self.store.outbox_put(dedup_id="d1", payload_hash="h1",
                              status="pending",
                              recorded_at="t", request={"a": 1})
        self.store.outbox_put(dedup_id="d2", payload_hash="h2",
                              status="offline",
                              recorded_at="t", request={"a": 2},
                              reason="local_outage")
        pending = self.store.outbox_pending()
        self.assertEqual(len(pending), 2)

    def test_incident_cooldown(self):
        self.store.raise_incident(run_id="r", kind="local_outage",
                                  message="boom")
        recent = self.store.recent_incident("local_outage",
                                            within_seconds=120)
        self.assertIsNotNone(recent)
        # A five-day window is still recent; use a zero-second window to
        # exercise the stale branch deterministically rather than relying on
        # an accidental timezone offset.
        old = self.store.recent_incident("local_outage", within_seconds=0)
        self.assertIsNone(old)

    def test_checkpoint_roundtrip(self):
        self.store.checkpoint_set("merge_proposal", "r",
                                  {"merge_target": "x"})
        self.assertEqual(self.store.checkpoint_get("merge_proposal"),
                         {"merge_target": "x"})


if __name__ == "__main__":
    unittest.main()
