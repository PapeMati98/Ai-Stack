import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from caretaker.model import SynthesisDecision
from caretaker.pipeline import _invoke_with_fallback, _write_handoff_note
from caretaker.privacy import (contains_restricted_identifier,
                               outbound_packet_safe, outbound_text_safe)
from caretaker.transports import TransportError


class PrivacyBoundaryTests(unittest.TestCase):
    def test_structured_personal_identifiers_are_blocked(self):
        for text in (
            "Kontakt: osoba@example.org",
            "PESEL: 12345678901",
            "telefon: +48 123 456 789",
            "ul. Przykładowa 12",
            "00-123 Warszawa",
        ):
            with self.subTest(text=text):
                self.assertTrue(contains_restricted_identifier(text))

    def test_project_facts_are_not_overblocked(self):
        self.assertFalse(contains_restricted_identifier(
            "Projekt ma backend Python, bazę PostgreSQL i dwie ścieżki deployu."))

    def test_folder_handoff_refuses_restricted_synthesis_before_hub_write(self):
        decision = SynthesisDecision(
            decision="new", synthesis="Kontakt: osoba@example.org",
            source_refs=["files:synthetic:1"])
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temporary:
            with self.assertRaisesRegex(ValueError, "restricted folder synthesis"):
                _write_handoff_note(temporary, decision, "safe-test-key",
                                    document_mode=True)
            self.assertFalse((Path(temporary) / "handoff-notes").exists())

    def test_reviewer_packet_checks_source_and_candidate(self):
        frozen = {"before": "", "sources": {"src": {"content": "General project architecture"}}}
        self.assertTrue(outbound_packet_safe(frozen, {"page": "Public design notes"}))
        self.assertFalse(outbound_packet_safe(frozen, {"page": "Kontakt: osoba@example.org"}))
        frozen["sources"]["src"]["content"] = "PESEL: 12345678901"
        self.assertFalse(outbound_packet_safe(frozen, {"page": "Public design notes"}))

    def test_reviewer_packet_checks_metadata_and_ledger(self):
        frozen = {"before": "", "sources": {"src": {"content": "Project architecture"}}}
        payloads = {"wiki/projects/test.md": "Project architecture"}
        self.assertFalse(outbound_packet_safe(
            frozen, payloads, {"summary": "Call osoba@example.org"}, {}))
        self.assertFalse(outbound_packet_safe(
            frozen, payloads, {}, {"dispositions": [{"note": "telefon: +48 123 456 789"}]}))
        self.assertFalse(outbound_packet_safe(
            frozen, payloads, {"secret": "password=synthetic-test-value"}, {}))

    def test_format_repair_prompt_is_rechecked_for_new_sensitive_text(self):
        self.assertTrue(outbound_text_safe("Review the redacted proposal."))
        self.assertFalse(outbound_text_safe(
            "Previous reviewer answer: osoba@example.org"))
        self.assertFalse(outbound_text_safe(
            "Previous reviewer answer: password=synthetic-test-value"))

    def test_owner_document_primary_outage_never_invokes_subscription_fallback(self):
        class BrokenPrimary:
            def invoke(self, **_kwargs):
                raise TransportError("local down", reason_code="process_timeout")

        class ForbiddenFallback:
            def invoke(self, **_kwargs):
                raise AssertionError("raw owner document sent to subscription")

        config = SimpleNamespace(state_dir="local", primary=object(), fallback=object())
        deps = SimpleNamespace(primary=BrokenPrimary(), fallback=ForbiddenFallback())
        with patch("caretaker.pipeline._write_run_config", return_value="unused"):
            envelope, used_fallback, reason = _invoke_with_fallback(
                deps, config, "synthetic owner document excerpt",
                state_dir="local", run_id="canary", local_only=True)
        self.assertIsNone(envelope)
        self.assertFalse(used_fallback)
        self.assertIn("primary_failed", reason)


if __name__ == "__main__":
    unittest.main()
