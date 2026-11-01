from __future__ import annotations

from unittest.mock import patch

from support import CoordTestCase

from coordfs.hook import CoordAdvisoryHook


class AdvisoryHookTests(CoordTestCase):
    def test_other_holder_produces_allow_warning_with_identity_and_ttl(self) -> None:
        self.board.claim("data:hook-resource", "grok-host-session-a", ttl_s=40)
        hook = CoordAdvisoryHook(home=self.home, cwd=self.cwd, now=lambda: self.clock[0])
        result = hook.check(
            "data:hook-resource",
            "claude-host-session-b",
            kind="data",
        )
        output = result["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "allow")
        self.assertIn("grok-host-session-a", output["additionalContext"])
        self.assertIn("40", output["additionalContext"])

    def test_board_failure_is_fail_open(self) -> None:
        hook = CoordAdvisoryHook(home=self.home, cwd=self.cwd, now=lambda: self.clock[0])
        with patch.object(hook.board, "who", side_effect=OSError("board unavailable")):
            result = hook.check("data:fail-open", "claude-host-session", kind="data")
        self.assertEqual(result, {})

    def test_per_resource_cache_avoids_duplicate_board_reads(self) -> None:
        self.board.claim("data:cached", "grok-host-session-a", ttl_s=40)
        hook = CoordAdvisoryHook(home=self.home, cwd=self.cwd, now=lambda: self.clock[0])
        with patch.object(hook.board, "who", wraps=hook.board.who) as who:
            first = hook.check("data:cached", "claude-host-session-b", kind="data")
            second = hook.check("data:cached", "claude-host-session-b", kind="data")
        self.assertEqual(first, second)
        self.assertEqual(who.call_count, 1)
