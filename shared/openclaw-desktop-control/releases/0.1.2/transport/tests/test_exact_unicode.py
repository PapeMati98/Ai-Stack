"""Exact-Unicode fidelity: durable queue + receiver readback, no rewrite."""

import hashlib
import os
import tempfile
import unittest

from openclaw_control import delivery
from openclaw_control.adapters import (
    FakeControlAdapter,
    NonLiteralAdapter,
)
from openclaw_control.store import ControlStore, target_binding_id

CASES = [
    "literowka  z  podwojnymi spacjami",
    "Zażółć gęślą jaźń: polskie znaki ąęćźżńół",
    "emoji 🙂🚀 i `backtick` oraz \"cudzysłów\" 'apostrof'",
    "wiele\nlinii\n\nz pustą w środku",
    "CRLF\r\nzachowane\r\njawnie",
    "NFC: café \u00e9 kontra NFD: café \u0065\u0301",
    "   wiodące i końcowe spacje   ",
    "\t tabulacja \t",
    "mieszane: zażółć\n\t 🙂 `x` \"y\"\r\n",
]

PREFIX_CASES = [
    "/clear w treści",
    "!echo nie jest komendą",
    "@plik nie jest załącznikiem",
    "/ ! @ wszystko literalne",
]


def _mkstore(tmp: str) -> ControlStore:
    return ControlStore(os.path.join(tmp, "authority.db"))


class ExactUnicodeTest(unittest.TestCase):
    def test_roundtrip_exact_all_cases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _mkstore(tmp)
            try:
                adapter = FakeControlAdapter()
                now = 1000.0
                for i, text in enumerate(CASES + PREFIX_CASES):
                    row = delivery.enqueue_prompt(
                        store,
                        task_id="task-%d" % i,
                        generation=1,
                        provider="p",
                        native_thread="n",
                        raw_text=text,
                        entities=[],
                        now=now,
                    )
                    # SHA over exact UTF-8 bytes, computed independently.
                    expect = hashlib.sha256(
                        text.encode("utf-8")
                    ).hexdigest()
                    self.assertEqual(row["text_sha256"], expect)
                    claimed = store.claim_one("w", now)
                    self.assertIsNotNone(claimed)
                    out = delivery.deliver(
                        store, adapter, claimed["dispatch_id"], now
                    )
                    self.assertEqual(out["state"], "delivered")
                    back = store.require_dispatch(row["dispatch_id"])
                    self.assertEqual(back["raw_text"], text)
                    self.assertEqual(
                        adapter.readback(row["dispatch_id"]), text
                    )
                    store.record_start(row['dispatch_id'], now)
                    store.record_result(row['dispatch_id'], 'completed', now)
                    now += 1
            finally:
                store.close()

    def test_nfc_nfd_are_distinct(self) -> None:
        nfc = "\u00e9"
        nfd = "\u0065\u0301"
        self.assertNotEqual(nfc, nfd)
        with tempfile.TemporaryDirectory() as tmp:
            store = _mkstore(tmp)
            try:
                r1 = delivery.enqueue_prompt(
                    store, "t", 1, "p", "n", nfc, [], 1.0
                )
                r2 = delivery.enqueue_prompt(
                    store, "t", 1, "p", "n", nfd, [], 1.0
                )
                self.assertNotEqual(
                    r1["text_sha256"], r2["text_sha256"]
                )
                self.assertEqual(
                    store.require_dispatch(r1["dispatch_id"])["raw_text"],
                    nfc,
                )
                self.assertEqual(
                    store.require_dispatch(r2["dispatch_id"])["raw_text"],
                    nfd,
                )
            finally:
                store.close()

    def test_restart_preserves_exact_bytes(self) -> None:
        text = CASES[1] + "\r\n" + CASES[2]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "authority.db")
            store = ControlStore(path)
            row = delivery.enqueue_prompt(
                store, "t", 1, "p", "n", text, [{"t": "e"}], 5.0
            )
            did = row["dispatch_id"]
            store.close()
            # Simulate process restart: brand-new connection, same file.
            store2 = ControlStore(path)
            try:
                back = store2.require_dispatch(did)
                self.assertEqual(back["raw_text"], text)
                self.assertEqual(
                    back["text_sha256"],
                    hashlib.sha256(text.encode("utf-8")).hexdigest(),
                )
            finally:
                store2.close()

    def test_no_model_rewrite_markers(self) -> None:
        # The store must never normalise, strip, or escape owner text.
        text = " Ž ć\t\n/COMMAND  "
        with tempfile.TemporaryDirectory() as tmp:
            store = _mkstore(tmp)
            try:
                row = delivery.enqueue_prompt(
                    store, "t", 1, "p", "n", text, [], 1.0
                )
                self.assertEqual(
                    store.require_dispatch(
                        row["dispatch_id"])["raw_text"], text
                )
            finally:
                store.close()

    def test_prefix_refusal_when_adapter_not_literal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _mkstore(tmp)
            try:
                row = delivery.enqueue_prompt(
                    store, "t", 1, "p", "n", "/clear wszystko",
                    [], 1.0,
                )
                claimed = store.claim_one("w", 1.0)
                self.assertIsNotNone(claimed)
                out = delivery.deliver(
                    store, NonLiteralAdapter(),
                    claimed["dispatch_id"], 1.0,
                )
                self.assertEqual(out["state"], "blocked")
                # Original text untouched by the refusal.
                self.assertEqual(
                    store.require_dispatch(
                        row["dispatch_id"])["raw_text"],
                    "/clear wszystko",
                )
            finally:
                store.close()

    def test_binding_id_stable(self) -> None:
        a = target_binding_id("desktop-code", "thread-7")
        b = target_binding_id("desktop-code", "thread-7")
        c = target_binding_id("desktop-code", "thread-8")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)


if __name__ == "__main__":
    unittest.main()
