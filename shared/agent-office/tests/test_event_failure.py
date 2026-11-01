from __future__ import annotations

from unittest.mock import patch

from support import CoordTestCase


class EventFailureTests(CoordTestCase):
    def test_claim_is_fail_open_and_reports_event_write_failure(self) -> None:
        with patch("coordfs.leases.append_event", side_effect=OSError("disk busy")):
            claimed = self.board.claim("data:event-failure", "agent-a", ttl_s=30)

        self.assertEqual(claimed["status"], "ok")
        self.assertEqual(claimed["holder_agent"], "agent-a")
        self.assertEqual(claimed["warnings"][0]["code"], "event_write_failed")
        current = self.board.who("data:event-failure")
        self.assertEqual(current["holder_agent"], "agent-a")
