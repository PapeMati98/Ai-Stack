from __future__ import annotations

from support import CoordTestCase


class WarningPayloadTests(CoordTestCase):
    def test_losing_claimant_gets_warning_with_holder_and_ttl_hint(self) -> None:
        first = self.board.claim("data:warn", "agent-a", ttl_s=40)
        self.assertEqual(first["status"], "ok")
        self.advance(10)
        second = self.board.claim("data:warn", "agent-b", ttl_s=40)
        self.assertEqual(second["status"], "warning")
        self.assertEqual(second["code"], "held")
        self.assertEqual(second["holder_agent"], "agent-a")
        self.assertEqual(second["ttl_remaining_s"], 30)
        self.assertEqual(second["ttl_hint_s"], 30)
        self.assertIn("agent-a", second["message"])
        self.assertIn("30", second["message"])
