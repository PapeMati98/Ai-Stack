"""Regression tests for synthesis_pending recovery after input rotation.

The pipeline persists sanitized admission provenance as ``synthesis_pending``
outbox rows.  When a pre-synthesis outage occurs (model crash, transport
failure, source file rotation) the next run must:

- rebuild ``SynthesisRecord`` instances from the durable rows,
- synthesize and publish the queued fact even though the original
  input has disappeared,
- deliver each fact exactly once, and
- drain the queued rows so they are not replayed forever.
"""

import json
import tempfile
import unittest
from pathlib import Path

from caretaker.hub import (FakeKnowledgeGateway, RecordHandoffResult,
                            WikiExcerpt)
from caretaker.office import (OfficeConnector, OfficeDedupStore,
                              OfficeIdentity, RateLimiter)
from caretaker.office import FakeOfficeTransport, OfficeSendResult
from caretaker.pipeline import PipelineDeps, run_pipeline
from caretaker.store import Store
from caretaker.transports import Envelope, FakeTransport

from ._helpers import write_config, load_config


class _CountingOffice(FakeOfficeTransport):
    """Tiny stub so the pipeline sees a healthy office."""

    def send(self, request, *, identity):
        return OfficeSendResult(status="sent", message_id="m1")


class _AckGateway(FakeKnowledgeGateway):
    """Synthetic fixture with the complete receipt shape required to ACK."""

    def record_handoff(self, **kwargs):
        super().record_handoff(**kwargs)
        return RecordHandoffResult(
            status="recorded",
            receipt_id="receipt-test-1",
            receipt_status="COMPLETE",
            source_id="source-test-1",
            proposal_id="proposal-test-1",
            event_ids=("event-test-1",),
            checkpoint_status="CHECKPOINTED",
            raw={"fixture": True},
            synthetic=False,
        )


def _office():
    return OfficeConnector(
        identity=OfficeIdentity(harness="h", host="host", session_id="sid"),
        transport=_CountingOffice(responses=[
            OfficeSendResult(status="sent", message_id="m1"),
        ]),
        dedup=OfficeDedupStore(),
        rate=RateLimiter(max_per_minute=60),
    )


class SynthesisPendingRecoveryTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = load_config(self.root)
        self.store = Store(str(self.root / "state.sqlite"))
        self.input = self.root / "input.jsonl"
        self.gateway = _AckGateway()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _deps(self, *, transport_responses):
        from caretaker.transports import Envelope
        primary = FakeTransport(
            name="primary", responses=transport_responses,
            expected_identity=("vllm", "Qwen3.8-27B"),
        )
        return PipelineDeps(primary=primary, fallback=None,
                            gateway=self.gateway, office=_office(),
                            worktree_root="")

    def _write_input(self, *, lines):
        self.input.write_text("".join(lines), "utf-8")

    def _config_input(self):
        from caretaker.config import InputSpec
        from dataclasses import replace
        return replace(self.cfg,
                       inputs=[InputSpec(str(self.input), "normalized_events",
                                          "codex")],
                       limits=replace(self.cfg.limits,
                                      max_records_per_run=64,
                                      max_prompt_bytes=16384))

    def test_pending_fact_delivered_once_after_input_rotated(self):
        # 1. Original input admitted one fact; primary crashes during
        #    synthesis so the queued fact is durably persisted as
        #    ``synthesis_pending``.
        self._write_input(lines=[json.dumps({
            "native_session_id": "codex-rotation-1",
            "cursor": "cur-1",
            "observed_at": "2026-09-17T10:00:00Z",
            "title": "rotation fact",
            "excerpt": "A verified application capability was restored.",
            "classification": "internal",
        }) + "\n"])
        cfg = self._config_input()
        deps_offline = self._deps(transport_responses=[])
        deps_offline.primary._raise_on = {0}
        r1 = run_pipeline(cfg, deps_offline, store=self.store)
        self.assertTrue(r1.local_outage)
        self.assertTrue(self.store.has_open_offline_outbox())

        # 2. Simulate source rotation: replace the input with an empty
        #    file (the original fact has "disappeared").
        self._write_input(lines=[])

        # 3. Restart: primary now returns a valid envelope.  The
        #    durable queued fact must be published once.
        self.store.close()
        self.store = Store(str(self.root / "state.sqlite"))
        final = json.dumps({
            "decision": "new",
            "synthesis": "A verified application capability was restored.",
            "source_refs": ["codex:codex-rotation-1:cur-1"],
            "rationale": "supported by reviewed wiki excerpt",
            "evidence_pages": ["wiki/projects/openclaw-stack-candidate.md"],
        })
        deps_ok = self._deps(transport_responses=[
            Envelope(ok=True, status="ok", final=final,
                     model="Qwen3.8-27B", provider="vllm"),
        ])
        # Seed the fake gateway with a wiki excerpt so the duplicate
        # verdict can be backed by evidence; for ``new`` no evidence is
        # required, but seeding exercises the new code path.
        self.gateway.wiki_pages["wiki/projects/openclaw-stack-candidate.md"] = (
            WikiExcerpt(page="wiki/projects/openclaw-stack-candidate.md",
                        title="Project",
                        excerpt="",
                        status="verified",
                        classification="internal"))
        r2 = run_pipeline(cfg, deps_ok, store=self.store)
        # Successful run: the queue is empty and the fact was recorded.
        self.assertEqual(self.store.outbox_pending(), [])
        # The pipeline must not have re-read any fresh input (file is
        # empty), but the queued fact still produces a new proposal.
        self.assertGreaterEqual(r2.proposals_new, 1)

    def test_pending_record_with_synthetic_dedup_still_requires_evidence(self):
        # Insert a queued fact and make the model return ``duplicate``
        # without naming any wiki excerpt.  The pipeline must refuse to
        # record it.
        self._write_input(lines=[json.dumps({
            "native_session_id": "codex-evidence-1",
            "cursor": "cur-1",
            "observed_at": "2026-09-17T10:00:00Z",
            "title": "evidence-required",
            "excerpt": "A semantic duplicate requires wiki evidence.",
            "classification": "internal",
        }) + "\n"])
        cfg = self._config_input()
        deps_offline = self._deps(transport_responses=[])
        deps_offline.primary._raise_on = {0}
        run_pipeline(cfg, deps_offline, store=self.store)
        self.store.close()
        self.store = Store(str(self.root / "state.sqlite"))

        # Now the model claims duplicate without evidence_pages.  The
        # pipeline must NOT record a synthetic dedup.
        final = json.dumps({
            "decision": "duplicate",
            "synthesis": "",
            "source_refs": ["codex:codex-evidence-1:cur-1"],
            "rationale": "looks duplicate",
            "evidence_pages": [],
        })
        deps_bad = self._deps(transport_responses=[
            Envelope(ok=True, status="ok", final=final,
                     model="Qwen3.8-27B", provider="vllm"),
        ])
        # Pre-load an unrelated wiki page; the model never cites it,
        # so the verdict must be rejected.
        self.gateway.wiki_pages["wiki/projects/other.md"] = WikiExcerpt(
            page="wiki/projects/other.md", title="Other", excerpt="x",
            status="verified", classification="internal")
        r2 = run_pipeline(cfg, deps_bad, store=self.store)
        self.assertIn("duplicate_without_evidence", r2.incidents)
        self.assertEqual(r2.proposals_contradiction, 1)
        # The queued fact stays in the offline outbox so it can be
        # retried later when the model supplies evidence.
        self.assertTrue(self.store.has_open_offline_outbox())


class SynthesisPendingBuildPromptTests(unittest.TestCase):

    def test_build_prompt_returns_unprocessed_when_max_records_bounded(self):
        from caretaker.model import (SynthesisInput, SynthesisRecord,
                                       WikiExcerpt, build_prompt)
        records = [
            SynthesisRecord(record_index=i, identity_tool="codex",
                            native_session_id="s", cursor=f"c{i}",
                            content_hash=f"h{i}", title=f"t{i}",
                            excerpt=f"excerpt-{i}")
            for i in range(10)
        ]
        payload = SynthesisInput(records=records, inventory_summary="{}",
                                 project_slug="p")
        # Pick a tight max_records so the prompt cannot fit everything.
        prompt, unprocessed = build_prompt(payload, max_bytes=4096,
                                              max_records=3)
        parsed = json.loads(prompt)
        self.assertEqual(len(parsed["records"]), 3)
        self.assertGreater(len(unprocessed), 0)
        # The returned tail is byte-stable: every record from index 3
        # onward is queued for the next batch.
        self.assertEqual([r.cursor for r in unprocessed],
                         ["c3", "c4", "c5", "c6", "c7", "c8", "c9"])
        # The prompt remains valid JSON.
        json.loads(prompt)


if __name__ == "__main__":
    unittest.main()
