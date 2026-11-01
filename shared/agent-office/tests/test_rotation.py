from __future__ import annotations

from coordfs.events import read_events
from coordfs.store import events_path

from support import CoordTestCase


class RotationTests(CoordTestCase):
    def test_rotation_keeps_triggering_event(self) -> None:
        self.board.events_max_bytes = 180
        self.board.events_retain = 2
        for index in range(12):
            result = self.board.claim(f"data:rot-{index}", "agent-a", ttl_s=30)
            self.assertEqual(result["status"], "ok")
        current = events_path(self.home)
        rotated = current.with_name("events.jsonl.1")
        self.assertTrue(current.is_file())
        self.assertTrue(rotated.is_file())
        live_events = read_events(self.home)
        types = [item["type"] for item in live_events]
        self.assertIn("claimed", types)
        self.assertEqual(live_events[-1]["resource_id"], "data:rot-11")
        live_text = current.read_text(encoding="utf-8")
        self.assertIn("data:rot-11", live_text)
        rotated_text = rotated.read_text(encoding="utf-8")
        self.assertTrue("data:rot-" in rotated_text)
        self.assertNotIn("data:rot-11", rotated_text)
        self.assertGreater(current.stat().st_size, 0)
