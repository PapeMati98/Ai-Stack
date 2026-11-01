"""Office tests."""

import unittest

from caretaker.office import (FakeOfficeTransport, OfficeConnector,
                             OfficeDedupStore, OfficeIdentity,
                             OfficeSendRequest, RateLimiter)


class OfficeConnectorTests(unittest.TestCase):

    def setUp(self):
        self.identity = OfficeIdentity(harness="h", host="host",
                                       session_id="sid")
        self.transport = FakeOfficeTransport(responses=[
            self._ok(), self._ok(), self._ok(),
        ])
        self.connector = OfficeConnector(
            identity=self.identity,
            transport=self.transport,
            dedup=OfficeDedupStore(),
            rate=RateLimiter(max_per_minute=2),
        )

    @staticmethod
    def _ok():
        from caretaker.office import OfficeSendResult
        return OfficeSendResult(status="sent", message_id="m1")

    def test_dedup(self):
        req = OfficeSendRequest(dedup_id="d1", body="hello", kind="note",
                                to="target")
        r1 = self.connector.publish(req)
        r2 = self.connector.publish(req)
        self.assertEqual(r1.status, "sent")
        self.assertEqual(r2.status, "deduplicated")

    def test_id_conflict(self):
        req1 = OfficeSendRequest(dedup_id="d1", body="hello", kind="note",
                                 to="target")
        req2 = OfficeSendRequest(dedup_id="d1", body="hello!",
                                 kind="note", to="target")
        self.connector.publish(req1)
        r = self.connector.publish(req2)
        self.assertEqual(r.status, "rejected")
        self.assertEqual(r.reason, "id_conflict")

    def test_rate_bound(self):
        req = lambda i: OfficeSendRequest(dedup_id=f"d{i}", body="x",
                                          kind="note", to="t")
        # Rate = 2/minute. First two succeed, third is offline/rate-limited.
        r1 = self.connector.publish(req(1))
        r2 = self.connector.publish(req(2))
        r3 = self.connector.publish(req(3))
        self.assertEqual(r1.status, "sent")
        self.assertEqual(r2.status, "sent")
        self.assertEqual(r3.status, "offline")
        self.assertEqual(r3.reason, "rate_limited")

    def test_no_autoplay_reply_loop(self):
        a = OfficeSendRequest(dedup_id="d1", body="hi", kind="note",
                             to="t")
        b = OfficeSendRequest(dedup_id="d2", body="reply", kind="note",
                             to="t", reply_to="d1")
        c = OfficeSendRequest(dedup_id="d2", body="reply", kind="note",
                             to="t", reply_to="d1")
        self.connector.publish(a)
        r1 = self.connector.publish(b)
        # Second send with same dedup_id+reply_to must be rejected as loop.
        r2 = self.connector.publish(c)
        self.assertEqual(r1.status, "sent")
        self.assertEqual(r2.status, "rejected")
        self.assertEqual(r2.reason, "autoplay_loop")

    def test_invalid_request(self):
        req = OfficeSendRequest(dedup_id="d1", body="x", kind="weird",
                                to="t")
        r = self.connector.publish(req)
        self.assertEqual(r.status, "rejected")

    def test_requires_target_or_channel(self):
        req = OfficeSendRequest(dedup_id="d1", body="x", kind="note")
        r = self.connector.publish(req)
        self.assertEqual(r.status, "rejected")
        self.assertEqual(r.reason, "invalid_request")


if __name__ == "__main__":
    unittest.main()
