"""Polish status: 600-char cap, edit-in-place, no spam, uncertain wait."""

import os
import tempfile
import unittest

from openclaw_control import delivery, models
from openclaw_control.status_text import (
    StatusTracker,
    render_status_pl,
)
from openclaw_control.store import ControlStore


class StatusTextTest(unittest.TestCase):
    def test_cap_and_polish(self) -> None:
        text = render_status_pl(
            "dispatch", models.DELIVERY_UNKNOWN, label="abc123",
            detail="sprawdź " * 200,
        )
        self.assertLessEqual(len(text), models.STATUS_MAX_CHARS)
        self.assertLessEqual(len(text), 600)
        short = render_status_pl("dispatch", models.DELIVERED)
        self.assertIn("Dyspozycja", short)
        self.assertIn("przyjęto; oczekiwanie na start", short)

    def test_send_edit_skip_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "authority.db")
            store = ControlStore(path)
            try:
                row = delivery.enqueue_prompt(
                    store, "t", 1, "p", "n", "tekst", [], 1.0
                )
                did = row["dispatch_id"]
                tracker = StatusTracker(store)
                text = render_status_pl("dispatch", models.QUEUED)
                first = tracker.decide(did, text)
                self.assertEqual(first["action"], "send")
                tracker.confirm(did, 222, 9, text, 2.0)
                self.assertEqual(
                    tracker.decide(did, text)["action"], "skip"
                )
                text2 = render_status_pl("dispatch", models.DELIVERED)
                edit = tracker.decide(did, text2)
                self.assertEqual(edit["action"], "edit")
                self.assertEqual(edit["message_id"], 9)
                # Uncertain send: no blind retry until reconciled.
                tracker.mark_uncertain(did, 3.0)
                self.assertEqual(
                    tracker.decide(did, text2)["action"], "wait"
                )
                tracker.confirm(did, 222, 9, text2, 4.0)
                self.assertEqual(
                    tracker.decide(did, text2)["action"], "skip"
                )
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
