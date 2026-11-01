import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import incidents


class IncidentEventPathTests(unittest.TestCase):
    def event(self, **overrides):
        event = {
            "event_id": "evt-normal-01",
            "source": "test:source/path",
            "event_type": "incident",
            "occurred_at": "2026-09-13T10:00:00Z",
        }
        event.update(overrides)
        return event

    def test_event_id_rejects_path_shaped_values(self):
        for event_id in ("C:/x", "C:\\x", "a/b", "a\\b", "../x", "..\\x", "C:x"):
            with self.subTest(event_id=event_id), self.assertRaises(ValueError):
                incidents._normalize_event(self.event(event_id=event_id))

    def test_other_identifiers_can_keep_colon_and_slash(self):
        normalized = incidents._normalize_event(self.event(source="test:source/path"))
        self.assertEqual(normalized["source"], "test:source/path")

    def test_normal_event_id_stays_flat_and_recording_is_idempotent(self):
        with tempfile.TemporaryDirectory() as state_root:
            first = incidents.record_event(state_root, self.event())
            second = incidents.record_event(state_root, self.event())
            events_dir = Path(state_root) / "incidents" / "events"

            self.assertTrue(first["stored"])
            self.assertFalse(first["duplicate"])
            self.assertFalse(second["stored"])
            self.assertTrue(second["duplicate"])
            self.assertEqual(
                sorted(path.relative_to(events_dir).as_posix() for path in events_dir.rglob("*")),
                ["evt-normal-01.json"],
            )


if __name__ == "__main__":
    unittest.main()
