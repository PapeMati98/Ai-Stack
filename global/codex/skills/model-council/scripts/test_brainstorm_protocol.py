import unittest

from brainstorm_protocol import BrainstormProtocolError, anonymize_bundle, require_round2_capacity


class BrainstormProtocolTests(unittest.TestCase):
    def test_bundle_is_anonymous_and_ordered(self):
        bundle = anonymize_bundle(
            [{"content": "ModelA proposes a baseline."}, {"content": "ModelB proposes an ablation."}],
            redact=["ModelA", "ModelB", "role_execution"],
        )
        self.assertIn("POMYSL 01", bundle)
        self.assertIn("POMYSL 02", bundle)
        self.assertNotIn("ModelA", bundle)
        self.assertNotIn("ModelB", bundle)
        self.assertNotIn("role_execution", bundle)

    def test_empty_round_one_fails_closed(self):
        with self.assertRaisesRegex(BrainstormProtocolError, "BRAINSTORM_EMPTY_ROUND1"):
            anonymize_bundle([])

    def test_non_string_transport_fails_closed(self):
        with self.assertRaisesRegex(BrainstormProtocolError, "BRAINSTORM_EMPTY_RESPONSE: 1"):
            anonymize_bundle([{"content": {"value": "PowerShell metadata wrapper"}}])

    def test_insufficient_round_one_blocks_round_two(self):
        with self.assertRaisesRegex(BrainstormProtocolError, "BRAINSTORM_INSUFFICIENT_ROUND1"):
            require_round2_capacity(3, 4)

    def test_round_two_capacity_accepts_exact_minimum(self):
        require_round2_capacity(4, 4)


if __name__ == "__main__":
    unittest.main()
