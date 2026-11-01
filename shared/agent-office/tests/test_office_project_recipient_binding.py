"""Synthetic regression coverage for project-scoped Office recipient admission.

Verifies that bogus project-shaped addresses (pa-, pp-, pc- plus 24 hex digits)
refuse admission in both preflight (_check_direct_recipient) and actual send (_send),
that fake pre-existing inbox files or new_recipient=True cannot legitimize them,
that genuine registered project sender/receiver roundtrip and ACK succeed without presence,
that wrong project/stream/runtime bindings refuse, and that corrupt registries degrade visibly.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from office_support import OfficeTestCase

from coordfs.errors import CoordError
from coordfs.office import Office, OfficeReject, inbox_path
from coordfs.projects import (
    ProjectRegistry,
    ProjectReject,
    ProjectTransport,
    _scoped_inbox_address,
)


class BogusProjectShapeAdmissionTests(OfficeTestCase):
    """Unregistered shaped recipients refuse before envelope write in both paths."""

    BOGUS_PA = "pa-0123456789abcdef01234567"
    BOGUS_PP = "pp-0123456789abcdef01234567"
    BOGUS_PC = "pc-0123456789abcdef01234567"
    BOGUS_SHAPES = (BOGUS_PA, BOGUS_PP, BOGUS_PC)

    def test_bogus_shapes_refused_in_preflight_and_send_without_registry(self) -> None:
        for address in self.BOGUS_SHAPES:
            with self.subTest(address=address):
                with self.assertRaises(OfficeReject) as refusal:
                    self.alice._check_direct_recipient(address)
                self.assertEqual(refusal.exception.code, "unknown_recipient")

                result = self.alice.send(address, "junk payload")
                self.assertEqual(result["status"], "error", result)
                self.assertEqual(result["code"], "unknown_recipient", result)
                self.assertFalse(
                    inbox_path(self.home, address).exists(),
                    f"a refused send to {address} must leave no void inbox",
                )

    def test_fake_preexisting_inbox_file_cannot_legitimize_shape(self) -> None:
        for address in self.BOGUS_SHAPES:
            with self.subTest(address=address):
                fake_inbox = inbox_path(self.home, address)
                fake_inbox.parent.mkdir(parents=True, exist_ok=True)
                fake_inbox.write_text("bogus existing content\n", encoding="utf-8")
                initial_size = fake_inbox.stat().st_size

                with self.assertRaises(OfficeReject) as refusal:
                    self.alice._check_direct_recipient(address)
                self.assertEqual(refusal.exception.code, "unknown_recipient")

                result = self.alice.send(address, "more junk")
                self.assertEqual(result["status"], "error", result)
                self.assertEqual(result["code"], "unknown_recipient", result)
                self.assertEqual(
                    fake_inbox.stat().st_size,
                    initial_size,
                    "existing inbox file must not be appended to for refused recipient",
                )

    def test_new_recipient_flag_does_not_legitimize_project_shapes(self) -> None:
        for address in self.BOGUS_SHAPES:
            with self.subTest(address=address):
                result = self.alice.send(address, "attempted contact", new_recipient=True)
                self.assertEqual(result["status"], "error", result)
                self.assertEqual(result["code"], "unknown_recipient", result)
                self.assertFalse(inbox_path(self.home, address).exists())

    def test_presence_lease_on_pp_cannot_turn_it_into_inbox(self) -> None:
        """pp is presence, not an inbox; active presence on pp- must not vouch for an inbox send."""
        pp_office = self.office(self.BOGUS_PP)
        self.assertEqual(pp_office.presence(ttl_s=300)["status"], "ok")

        with self.assertRaises(OfficeReject) as refusal:
            self.alice._check_direct_recipient(self.BOGUS_PP)
        self.assertEqual(refusal.exception.code, "unknown_recipient")

        result = self.alice.send(self.BOGUS_PP, "send to presence address")
        self.assertEqual(result["status"], "error", result)
        self.assertEqual(result["code"], "unknown_recipient", result)
        self.assertFalse(inbox_path(self.home, self.BOGUS_PP).exists())

    def test_cli_refuses_bogus_project_shapes(self) -> None:
        for address in self.BOGUS_SHAPES:
            with self.subTest(address=address):
                code, payload = self.run_cli(
                    ["--agent", "codex-testhost-alice", "send", "--to", address, "--body", "cli junk"]
                )
                self.assertEqual(code, 1, payload)
                self.assertEqual(payload["status"], "error")
                self.assertEqual(payload["code"], "unknown_recipient")
                self.assertFalse(inbox_path(self.home, address).exists())


class GenuineProjectBindingDeliveryTests(OfficeTestCase):
    """Actual registered project delivery/ACK and offline contracts remain working."""

    def setUp(self) -> None:
        super().setUp()
        self.checkout_dir = Path(self.tmp.name) / "checkout-alpha"
        self.checkout_dir.mkdir(parents=True, exist_ok=True)
        self.registry = ProjectRegistry(home=self.home)
        self.registry.register("alpha", checkout=self.checkout_dir)

    def test_registered_genuine_project_sender_receiver_roundtrip_and_ack_succeed(self) -> None:
        sender = ProjectTransport(
            home=self.home,
            project_id="alpha",
            harness="codex",
            host="ailab",
            session_id="sender-1",
            now=lambda: self.clock[0],
            ack_ttl_s=10,
        )
        receiver = ProjectTransport(
            home=self.home,
            project_id="alpha",
            harness="claude",
            host="ailab",
            session_id="receiver-1",
            now=lambda: self.clock[0],
            ack_ttl_s=10,
        )
        stream = sender.stream(task_id="task-1", thread_id="thread-1", run_id="run-1")

        # Verify neither has live presence on the presence board
        self.assertEqual(self.alice.who()["count"], 0)

        # Preflight resolves receiver's pa- address
        receiver_inbox = receiver.inbox_address(stream)
        self.assertEqual(self.alice._check_direct_recipient(receiver_inbox), receiver_inbox)

        # Sender sends to receiver
        sent = sender.send(receiver, "hello from genuine sender", stream=stream)
        self.assertEqual(sent["status"], "ok", sent)
        self.assertEqual(sent["code"], "sent", sent)
        delivery_id = sent["delivery_id"]

        # Receiver reads the message
        received = receiver.read(stream=stream)
        self.assertEqual(received["status"], "ok", received)
        self.assertEqual(received["count"], 1)
        self.assertEqual(
            received["messages"][0]["untrusted_message"]["body"],
            "hello from genuine sender",
        )

        # Receiver acknowledges
        ack = receiver.acknowledge(delivery_id)
        self.assertEqual(ack["status"], "ok", ack)
        self.assertEqual(ack["code"], "acknowledged", ack)

        # Sender reads the ACK
        sender_inbound = sender.read(stream=stream)
        self.assertEqual(sender_inbound["count"], 1)
        self.assertEqual(
            sender_inbound["messages"][0]["untrusted_message"]["application"]["type"],
            "ack",
        )
        self.assertEqual(
            sender_inbound["messages"][0]["untrusted_message"]["application"]["ack_result"],
            "acknowledged",
        )

        # Delivery state is completed
        state = sender.delivery_state(delivery_id)
        self.assertEqual(state["application_state"], "ACKNOWLEDGED")

    def test_offline_project_binding_delivered_when_presence_board_unreadable(self) -> None:
        receiver = ProjectTransport(
            home=self.home,
            project_id="alpha",
            harness="claude",
            host="ailab",
            session_id="offline-recv",
            now=lambda: self.clock[0],
        )
        stream = receiver.stream(task_id="task-off", thread_id="thread-off", run_id="run-off")
        address = receiver.inbox_address(stream)

        with mock.patch.object(self.alice, "_board_or_raise", side_effect=OSError("presence board gone")):
            self.assertEqual(self.alice._check_direct_recipient(address), address)
            sent = self.alice.send(address, "direct delivery to offline project stream")
        self.assertEqual(sent["status"], "ok", sent)
        self.assertEqual(sent["to"], address)
        self.assertIn(
            "direct delivery to offline project stream",
            inbox_path(self.home, address).read_text(encoding="utf-8"),
        )

    def test_explicit_offline_delivery_requires_a_complete_recorded_stream(self) -> None:
        sender = ProjectTransport(home=self.home, project_id="alpha", harness="codex",
                                  host="ailab", session_id="sender-before-recipient")
        stream = sender.stream(task_id="offline-task", thread_id="offline-thread", run_id="offline-run")
        recipient = {"harness":"claude", "host":"ailab", "session":"not-joined",
                     "agent_id":"claude-ailab-not-joined", "strength":"strong",
                     "source":"explicit-session-id"}
        address = _scoped_inbox_address("alpha", stream.stream_id, recipient)
        self.assertEqual(self.alice.send(address, "raw unregistered route")["code"], "unknown_recipient")
        self.assertFalse(inbox_path(self.home, address).exists())
        sent = sender.send(recipient, "deliberate offline first contact", stream=stream)
        self.assertEqual(sent["status"], "ok", sent)
        self.assertEqual(self.alice._check_direct_recipient(address), address)
        # A partial ledger entry must not turn historical contact into a wildcard.
        state = json.loads(self.registry.path.read_text(encoding="utf-8"))
        del state["projects"]["alpha"]["deliveries"][sent["delivery_id"]]["stream"]["task_id"]
        self.registry.path.write_text(json.dumps(state), encoding="utf-8")
        original = inbox_path(self.home, address).read_bytes()
        refused = self.alice.send(address, "must not append against incomplete binding")
        self.assertEqual(refused["code"], "unknown_recipient", refused)
        self.assertEqual(inbox_path(self.home, address).read_bytes(), original)


class ProjectShapeTamperingAndCorruptionTests(OfficeTestCase):
    """Wrong project/stream/runtime bindings and corrupt registries refuse or degrade."""

    def setUp(self) -> None:
        super().setUp()
        self.checkout_dir = Path(self.tmp.name) / "checkout-alpha"
        self.checkout_dir.mkdir(parents=True, exist_ok=True)
        self.registry = ProjectRegistry(home=self.home)
        self.registry.register("alpha", checkout=self.checkout_dir)
        self.stream_rec = self.registry.ensure_stream(
            "alpha", {"task_id": "t1", "thread_id": "th1", "run_id": "r1"}
        )
        self.identity = {
            "harness": "claude",
            "host": "ailab",
            "session": "s1",
            "agent_id": "claude-ailab-s1",
            "strength": "strong",
            "source": "explicit-session-id",
        }
        self.registry.bind_runtime("alpha", self.identity)
        self.stream_id = self.stream_rec["stream"]["stream_id"]
        self.valid_pa = _scoped_inbox_address("alpha", self.stream_id, self.identity)

    def test_wrong_project_stream_or_runtime_binding_refuses(self) -> None:
        wrong_project = _scoped_inbox_address("beta", self.stream_id, self.identity)
        wrong_stream = _scoped_inbox_address("alpha", "wrong-stream-id", self.identity)
        wrong_runtime = _scoped_inbox_address(
            "alpha",
            self.stream_id,
            {
                "harness": "claude",
                "host": "ailab",
                "session": "wrong-session",
                "agent_id": "claude-ailab-wrong-session",
                "strength": "strong",
                "source": "explicit-session-id",
            },
        )

        for wrong_address in (wrong_project, wrong_stream, wrong_runtime):
            with self.subTest(wrong_address=wrong_address):
                with self.assertRaises(OfficeReject) as refusal:
                    self.alice._check_direct_recipient(wrong_address)
                self.assertEqual(refusal.exception.code, "unknown_recipient")

                result = self.alice.send(wrong_address, "tampered payload")
                self.assertEqual(result["status"], "error", result)
                self.assertEqual(result["code"], "unknown_recipient", result)
                self.assertFalse(inbox_path(self.home, wrong_address).exists())

    def test_unrecorded_offline_identity_does_not_vouch_for_a_raw_inbox(self) -> None:
        from coordfs.projects import ProjectStream
        stream_obj = ProjectStream(
            project_id="alpha",
            checkout=str(self.checkout_dir),
            task_id="t1",
            thread_id="th1",
            run_id="r1",
            stream_id=self.stream_id,
        )
        unregistered_remote = {
            "harness": "grok",
            "host": "ailab",
            "session": "unregistered",
            "agent_id": "grok-ailab-unregistered",
            "strength": "strong",
            "source": "explicit-session-id",
        }
        bogus_address = _scoped_inbox_address("alpha", self.stream_id, unregistered_remote)

        with self.assertRaises(OfficeReject) as refusal:
            self.alice._check_direct_recipient(bogus_address)
        self.assertEqual(refusal.exception.code, "unknown_recipient")
        self.assertEqual(self.alice.send(bogus_address, "unrecorded contact")["code"], "unknown_recipient")
        self.assertFalse(inbox_path(self.home, bogus_address).exists())
        wrong_stream = ProjectStream(
            project_id="alpha", checkout=str(self.checkout_dir), task_id="other",
            thread_id="th1", run_id="r1", stream_id="stream-" + "0" * 24,
        )
        with self.assertRaises(ProjectReject) as refusal:
            self.registry.prepare_delivery(
                "alpha", delivery_id="0123456789abcdef0123456789abcdef",
                stream=wrong_stream, recipient_identity=unregistered_remote,
                recipient_address=bogus_address, ack_ttl_s=10,
            )
        self.assertEqual(refusal.exception.code, "stream_project_mismatch")

    def test_corrupt_registry_visibly_refuses_or_degrades(self) -> None:
        # Corrupt the registry on disk
        self.registry.path.write_text("{ corrupt json content", encoding="utf-8")

        # Direct preflight must refuse / raise
        with self.assertRaises((ProjectReject, CoordError, OfficeReject)):
            self.alice._check_direct_recipient(self.valid_pa)

        # Send must visibly degrade or error, never succeed
        result = self.alice.send(self.valid_pa, "payload against corrupt registry")
        self.assertIn(result["status"], ("degraded", "error"), result)
        self.assertNotEqual(result["status"], "ok")
        self.assertFalse(
            inbox_path(self.home, self.valid_pa).exists(),
            "corrupt registry must never create an orphan inbox",
        )
