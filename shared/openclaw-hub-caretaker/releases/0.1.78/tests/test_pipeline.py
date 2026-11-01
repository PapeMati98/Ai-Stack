"""End-to-end pipeline tests using injectable transports."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from caretaker.hub import FakeKnowledgeGateway, InventoryItem, KnowledgeGateway
from caretaker.office import (OfficeConnector, OfficeDedupStore,
                              OfficeIdentity, OfficeSendResult,
                              RateLimiter)
from caretaker.pipeline import PipelineDeps, build_default_deps, run_pipeline
from caretaker.store import Store
from caretaker.transports import Envelope, FakeTransport, TransportError

from ._helpers import FIXTURES, write_config


def _ok_envelope(final, model="Qwen3.8-27B", provider="vllm"):
    return Envelope(ok=True, status="ok", final=final,
                    model=model, provider=provider)


def _office_connector():
    return OfficeConnector(
        identity=OfficeIdentity(harness="h", host="host",
                                session_id="sid"),
        transport=_FakeOffice(),
        dedup=OfficeDedupStore(),
        rate=RateLimiter(max_per_minute=60),
    )


class _FakeOffice:
    def __init__(self):
        self.calls = []

    def send(self, request, *, identity):
        self.calls.append((request.dedup_id, request.kind))
        return OfficeSendResult(status="sent", message_id="m1")


class PipelineFixture(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg_path = write_config(Path(self.tmp.name))
        from caretaker.config import load_config
        self.cfg = load_config(str(self.cfg_path))
        self.store = Store(str(Path(self.tmp.name) / "s.sqlite"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _deps(self, *, primary_responses, fallback_responses=None,
              primary_raises_on=None, gateway=None):
        primary = FakeTransport(
            name="primary", responses=primary_responses,
            raise_on=primary_raises_on or [],
            expected_identity=("vllm", "Qwen3.8-27B"),
        )
        fallback = None
        if self.cfg.fallback is not None:
            fallback = FakeTransport(
                name="fallback", responses=fallback_responses or [],
                expected_identity=("muse-code", "muse-spark-1.3"),
            )
        return PipelineDeps(
            primary=primary, fallback=fallback,
            gateway=gateway or FakeKnowledgeGateway(),
            office=_office_connector(),
            worktree_root="",  # bypass dirty check
        )


class HappyPathTests(PipelineFixture):

    def test_no_input_means_zero_model_calls(self):
        # Build a config with an empty input file; records_read is 0.
        empty_path = Path(self.tmp.name) / "empty.jsonl"
        empty_path.write_text("", "utf-8")
        from dataclasses import replace
        from caretaker.config import InputSpec
        self.cfg = replace(self.cfg, inputs=[
            InputSpec(path=str(empty_path), kind="normalized_events",
                      tool="openclaw"),
        ])
        deps = self._deps(primary_responses=[
            _ok_envelope('{"decision":"new","synthesis":"x",'
                         '"source_refs":["codex:s:c"]}'),
        ])
        result = run_pipeline(self.cfg, deps, store=self.store,
                              dry_run=False)
        self.assertEqual(result.records_read, 0)
        self.assertEqual(result.records_admitted, 0)
        self.assertEqual(result.notes, "no_input")
        self.assertEqual(len(deps.primary.calls), 0)
        self.assertEqual(result.proposals_new, 0)

    def test_full_happy_path_with_synthesis(self):
        # Source refs reference actual admitted records.
        deps = self._deps(primary_responses=[
            _ok_envelope(
                '{"decision":"new","synthesis":"summary",'
                '"source_refs":["codex:codex-1:run-1",'
                '"codex:codex-1:run-2"],'
                '"rationale":"r"}'
            ),
        ])
        # Pre-populate one exact duplicate via inventory so dedup is exercised.
        deps.gateway.inventory_items = [
            InventoryItem(page="x", title="existing", summary="s",
                          classification="internal", receipt_id="r1"),
        ]
        result = run_pipeline(self.cfg, deps, store=self.store,
                              dry_run=False)
        self.assertFalse(result.local_outage)
        self.assertFalse(result.fallback_used)
        self.assertGreater(result.records_admitted, 0)
        # At least one primary call was made because some records are novel.
        self.assertGreaterEqual(len(deps.primary.calls), 1)
        # The pipeline must produce at least one proposal, merge, contradiction,
        # or duplicate. It must not silently concatenate.
        self.assertGreaterEqual(result.proposals_new
                                + result.proposals_merge
                                + result.proposals_contradiction
                                + result.proposals_duplicate, 1)

    def test_exact_duplicate_no_republish(self):
        # Pre-populate an inventory item whose hash matches one record so the
        # pipeline treats that record as an exact duplicate.
        # First, compute the hash that the filter will produce.
        from caretaker.ingest import read_inputs
        from caretaker.filter import filter_records
        recs, _, _ = read_inputs(
            [(str(FIXTURES / "codex_session.jsonl"), "session_jsonl",
              "codex")],
        )
        outcomes = filter_records(
            recs, allowlist_tools=("codex",), max_body_bytes=8192,
            max_excerpt_bytes=400,
        )
        accepted = next(o for o in outcomes if o.accepted)
        deps = self._deps(primary_responses=[
            _ok_envelope('{"decision":"new","synthesis":"x",'
                         '"source_refs":["ignored"]}'),
        ])
        # Insert an inventory item whose summary matches accepted.excerpt so
        # the inventory hash matches the record hash. We cannot forge the
        # hash, but we can populate a proposal with the same content_hash.
        self.store.proposal_upsert(
            proposal_id="p1", content_hash=accepted.content_hash,
            source_refs=["x"], synthesis="existing",
            model_identity="m", decision="duplicate",
            merge_target=None, rationale="pre-existing",
        )
        result = run_pipeline(self.cfg, deps, store=self.store,
                              dry_run=False)
        # The pre-existing record is treated as duplicate; primary is still
        # invoked for any other novel records.
        self.assertGreaterEqual(result.proposals_duplicate, 1)


class CrashRetryOfflineTests(PipelineFixture):

    def test_primary_crash_uses_fallback(self):
        # Primary crashes on first call; fallback returns a valid envelope.
        # We must still admit records and produce at least one proposal.
        deps = self._deps(
            primary_responses=[],
            primary_raises_on=[0],
            fallback_responses=[
                _ok_envelope(
                    '{"decision":"new","synthesis":"summary",'
                    '"source_refs":["codex:codex-1:run-1"],'
                    '"rationale":"r"}',
                    model="muse-spark-1.3", provider="muse-code",
                ),
            ],
        )
        result = run_pipeline(self.cfg, deps, store=self.store,
                              dry_run=False)
        self.assertTrue(result.local_outage)
        self.assertTrue(result.fallback_used)
        # The model identity must be the fallback's, not the primary's.
        self.assertEqual(result.model_identity, "muse-code/muse-spark-1.3")
        # The pipeline still produced a new proposal (or merge/contradiction).
        self.assertGreaterEqual(result.proposals_new
                                + result.proposals_merge
                                + result.proposals_contradiction, 1)

    def test_primary_and_fallback_offline(self):
        # Both transports fail; pipeline must preserve the queue and raise a
        # local_outage incident.
        deps = self._deps(
            primary_responses=[],
            primary_raises_on=[0],
            fallback_responses=[],
        )
        # Force fallback to also raise by setting raise_on.
        deps.fallback._raise_on = {0}
        result = run_pipeline(self.cfg, deps, store=self.store,
                              dry_run=False)
        self.assertTrue(result.local_outage)
        self.assertTrue(result.fallback_used)
        # The pipeline must have at least one offline outbox row.
        self.assertTrue(self.store.has_open_offline_outbox())

    def test_offline_retry_picks_up_where_left_off(self):
        # First run: primary fails, fallback fails -> offline outbox.
        deps1 = self._deps(
            primary_responses=[],
            primary_raises_on=[0],
            fallback_responses=[],
        )
        deps1.fallback._raise_on = {0}
        r1 = run_pipeline(self.cfg, deps1, store=self.store,
                          dry_run=False)
        self.assertTrue(r1.local_outage)
        # Second run: primary recovers; outbox remains; pipeline proceeds.
        deps2 = self._deps(
            primary_responses=[
                _ok_envelope(
                    '{"decision":"new","synthesis":"summary",'
                    '"source_refs":["codex:codex-1:run-1"],'
                    '"rationale":"r"}'
                ),
            ],
        )
        r2 = run_pipeline(self.cfg, deps2, store=self.store,
                          dry_run=False)
        self.assertFalse(r2.local_outage)
        self.assertGreaterEqual(r2.proposals_new + r2.proposals_merge
                                + r2.proposals_contradiction, 1)


class DirtyTreeTests(PipelineFixture):

    def test_dirty_worktree_fails_closed(self):
        # Provide a non-git worktree root to force the dirty guard.
        deps = self._deps(primary_responses=[
            _ok_envelope('{"decision":"new","synthesis":"x",'
                         '"source_refs":["codex:s:c"]}'),
        ])
        deps.worktree_root = str(Path(self.tmp.name) / "no_such_dir")
        result = run_pipeline(self.cfg, deps, store=self.store,
                              dry_run=False)
        self.assertIn("dirty_worktree", result.incidents)
        self.assertEqual(result.records_admitted, 0)
        # No model call when the worktree guard fails closed.
        self.assertEqual(len(deps.primary.calls), 0)


class HubGuardTests(PipelineFixture):

    def test_hub_health_below_min_fails_closed(self):
        class BadGateway(FakeKnowledgeGateway):
            def health(self):
                from caretaker.hub import HealthResult
                return HealthResult(status="warn")
        deps = self._deps(primary_responses=[],
                           gateway=BadGateway())
        result = run_pipeline(self.cfg, deps, store=self.store,
                              dry_run=False)
        self.assertIn("hub_health", result.incidents)
        self.assertEqual(len(deps.primary.calls), 0)

    def test_hub_lint_below_min_fails_closed(self):
        class BadGateway(FakeKnowledgeGateway):
            def lint(self):
                from caretaker.hub import LintResult
                return LintResult(status="warn")
        deps = self._deps(primary_responses=[],
                           gateway=BadGateway())
        result = run_pipeline(self.cfg, deps, store=self.store,
                              dry_run=False)
        self.assertIn("hub_lint", result.incidents)


class ProvenanceTests(PipelineFixture):

    def test_provenance_preserved_in_durable_state(self):
        deps = self._deps(primary_responses=[
            _ok_envelope(
                '{"decision":"new","synthesis":"summary",'
                '"source_refs":["codex:s1:c1"],'
                '"rationale":"r"}'
            ),
        ])
        run_pipeline(self.cfg, deps, store=self.store, dry_run=False)
        # Cursors table must contain at least one row for codex with
        # native_session_id="codex-1".
        cur = self.store._conn.execute(
            "SELECT tool, native_session_id, cursor FROM cursors "
            "WHERE tool = 'codex' AND native_session_id = 'codex-1'"
        ).fetchall()
        self.assertGreater(len(cur), 0)


if __name__ == "__main__":
    unittest.main()
