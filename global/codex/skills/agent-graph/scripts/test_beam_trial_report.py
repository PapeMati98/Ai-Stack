"""Focused regression coverage for the Beam trial metadata report."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import beam_trial_report as report


MAKER_ID = "run-20261015T221247Z-ba23edbf1f"
REVIEW_ID = "run-20261015T221352Z-4fc85e70db"
ARTIFACT = "fe0caff1079aa6b5bb24da1c619ef8bce689150d048225d9ec7fc1389a205cd8"
CHECKS = "81b2154cba4064ad652fa89fbf1a18202b8858a61d1bdf5a9ff54f1ad09ec3f6"
INPUTS = "7a9fff4450ef59878775652eb9a26dcf62f8357896a83727092fcb24ce6ac50f"
SENTINEL = "PRIVATE_SENTINEL_DO_NOT_EMIT_9133"


def _maker_record(run_id=MAKER_ID, status="FAILED", **extra):
    record = {
        "run_id": run_id,
        "agent": report.ALLOW_AGENT,
        "model": report.ALLOW_MODEL,
        "model_provider_id": report.ALLOW_PROVIDER,
        "started_at": "2026-10-15T22:12:47.797617Z",
        "completed_at": "2026-10-15T22:13:20.027264Z",
        "run_status": status,
        "duration_seconds": 31.952,
        "model_identity": {"status": "verified"},
        "runtime_attestation": {"status": "VERIFIED"},
        "token_usage": {
            "fresh_input_tokens": 21064,
            "output_tokens": 1134,
            "cache_read_tokens": 60224,
            "cache_write_tokens": 0,
            "reasoning_tokens": 3566,
            "total_tokens": 85988,
            "cost_usd": 0,
        },
        "handoff_valid": False,
    }
    record.update(extra)
    return record


def _review_record(run_id=REVIEW_ID, agent="minimax", model="MiniMax-M3",
                   provider="minimax", **extra):
    record = {
        "run_id": run_id,
        "agent": agent,
        "model": model,
        "model_provider_id": provider,
        "started_at": "2026-10-15T22:13:53.671154Z",
        "completed_at": "2026-10-15T22:14:50.327940Z",
        "run_status": "COMPLETED",
        "model_identity": {
            "status": "verified",
            "provider_source": "provider",
            "provider_kind": "minimax-managed",
        },
        "runtime_attestation": {"status": "VERIFIED"},
    }
    record.update(extra)
    return record


def _pair(maker_id=MAKER_ID, reviewer_id=REVIEW_ID, **overrides):
    maker = {
        "role": "maker",
        "result": {
            "status": "PARTIAL",
            "agent": report.ALLOW_AGENT,
            "model": report.ALLOW_MODEL,
            "lineage": report.ALLOW_PROVIDER,
            "run_id": maker_id,
        },
    }
    reviewer_result = {
        "status": "REVIEWED",
        "agent": "minimax",
        "model": "MiniMax-M3",
        "lineage": "minimax",
        "run_id": reviewer_id,
        "verdict": "ACCEPT",
        "findings": [],
        "summary": SENTINEL,
        "notes": [SENTINEL],
    }
    reviewer = {
        "role": "reviewer",
        "result": reviewer_result,
        "artifact": ARTIFACT,
        "tests": [{"exit_code": 0, "seconds": 0.136}],
        "reason": SENTINEL,
    }
    result = {
        "status": "ACCEPTED",
        "artifact": ARTIFACT,
        "inputs_digest": INPUTS,
        "checks_digest": CHECKS,
        "history": [maker, reviewer],
        "summary": SENTINEL,
        "reasons": [SENTINEL],
    }
    result.update(overrides)
    return {"result": result}


class BeamTrialReportTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(prefix="beam-report-")
        self.root = Path(self._temp.name)
        self.runs = self.root / "runs"
        self.todo = self.root / "todo"
        self.runs.mkdir()
        self.todo.mkdir()

    def tearDown(self):
        self._temp.cleanup()

    def _write(self, path, value):
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def _collect(self, run=None, pair=None, review=None, review_id=REVIEW_ID):
        self._write(self.runs / "run-maker.json", run or _maker_record())
        if review is not False:
            self._write(self.runs / "run-review.json", review or _review_record(run_id=review_id))
        if pair is not False:
            self._write(self.todo / "pair.json", pair or _pair(reviewer_id=review_id))
        return report.collect(self.runs, report._parse_since(report.TRIAL_DEFAULT_SINCE), [self.todo])

    def test_failed_native_handoff_can_have_independent_product_acceptance(self):
        payload = self._collect()
        row = payload["rows"][0]
        self.assertEqual(row["run_status"], "FAILED")
        self.assertIs(row["native_handoff_valid"], False)
        self.assertEqual(row["quality"], "accepted")
        self.assertEqual(payload["aggregate"]["native_failures"], 1)

    def test_completed_native_run_without_review_is_unknown(self):
        payload = self._collect(run=_maker_record(status="COMPLETED"), pair={
            "result": {"status": "ACCEPTED", "artifact": ARTIFACT,
                       "checks_digest": CHECKS, "inputs_digest": INPUTS,
                       "history": [{"role": "maker", "result": {
                           "run_id": MAKER_ID, "agent": report.ALLOW_AGENT,
                           "model": report.ALLOW_MODEL, "lineage": report.ALLOW_PROVIDER,
                       }}]},
        }, review=False)
        self.assertEqual(payload["rows"][0]["run_status"], "COMPLETED")
        self.assertEqual(payload["rows"][0]["quality"], "unknown")

    def test_same_lineage_and_bad_or_empty_checks_remain_unknown(self):
        same = _pair()
        same["result"]["history"][1]["result"]["lineage"] = report.ALLOW_PROVIDER
        cases = [same]
        empty_tests = _pair()
        empty_tests["result"]["history"][1]["tests"] = []
        cases.append(empty_tests)
        boolean_exit = _pair()
        boolean_exit["result"]["history"][1]["tests"][0]["exit_code"] = False
        cases.append(boolean_exit)
        no_digests = _pair()
        no_digests["result"].pop("checks_digest")
        cases.append(no_digests)
        for pair in cases:
            with self.subTest(pair=pair):
                self.assertEqual(self._collect(pair=pair)["rows"][0]["quality"], "unknown")

    def test_stale_artifact_and_mismatched_or_forged_reviewer_are_unknown(self):
        stale = _pair()
        stale["result"]["history"][1]["artifact"] = "a" * 64
        self.assertEqual(self._collect(pair=stale)["rows"][0]["quality"], "unknown")

        mismatch = _pair()
        mismatch["result"]["history"][1]["result"]["model"] = "Forged-Model"
        self.assertEqual(self._collect(pair=mismatch)["rows"][0]["quality"], "unknown")

        forged_native = _review_record(agent="different-agent")
        self.assertEqual(self._collect(review=forged_native)["rows"][0]["quality"], "unknown")

    def test_graph_state_nodes_use_node_metadata_and_history(self):
        node = _pair()["result"]
        node["status"] = "ACCEPTED"
        self._write(self.runs / "run-maker.json", _maker_record())
        self._write(self.runs / "run-review.json", _review_record())
        self._write(self.todo / "state.json", {"nodes": {"beam": node}})
        payload = report.collect(
            self.runs, report._parse_since(report.TRIAL_DEFAULT_SINCE), [self.todo]
        )
        self.assertEqual(payload["rows"][0]["quality"], "accepted")

    def test_repair_requires_real_independent_reviewer_and_findings(self):
        pair = _pair()
        reviewer = pair["result"]["history"][1]
        reviewer["result"]["verdict"] = "REPAIR"
        reviewer["result"]["findings"] = [SENTINEL]
        self.assertEqual(self._collect(pair=pair)["rows"][0]["quality"], "repair")

        self.assertEqual(
            self._collect(pair=pair, review=_review_record(run_status="FAILED"))
            ["rows"][0]["quality"],
            "unknown",
        )

    def test_later_maker_and_later_review_are_selected(self):
        earlier = _pair()
        second_id = "run-20261015T231247Z-acde1234"
        earlier_maker = earlier["result"]["history"][0]
        later_maker = {
            "role": "maker",
            "result": {"run_id": second_id, "agent": report.ALLOW_AGENT,
                       "model": report.ALLOW_MODEL, "lineage": report.ALLOW_PROVIDER},
        }
        earlier["result"]["history"].append(later_maker)
        self.assertEqual(self._collect(pair=earlier)["rows"][0]["quality"], "unknown")
        self.assertIsNotNone(earlier_maker)

    def test_untrusted_free_text_and_raw_rejected_paths_never_appear(self):
        run = _maker_record(summary=SENTINEL, reasons=[SENTINEL])
        self._write(self.runs / "run-maker.json", run)
        output = report.collect(
            self.runs, report._parse_since(report.TRIAL_DEFAULT_SINCE),
            [self.root / SENTINEL],
        )
        self.assertNotIn(SENTINEL, json.dumps(output))
        self.assertNotIn("quality_source", output["rows"][0])
        self.assertNotIn("rejected_todo_paths", output)

    def test_invalid_numeric_values_are_null_and_huge_integers_do_not_overflow(self):
        huge = 10 ** 2000
        run = _maker_record(duration_seconds=huge)
        run["token_usage"].update({
            "fresh_input_tokens": huge,
            "output_tokens": True,
            "cache_read_tokens": -1,
            "cache_write_tokens": None,
            "reasoning_tokens": 2.5,
            "total_tokens": 8,
            "cost_usd": None,
        })
        payload = self._collect(run=run, pair=False, review=False)
        row = payload["rows"][0]
        self.assertIsNone(row["duration_seconds"])
        self.assertEqual(row["tokens"]["fresh_input_tokens"], huge)
        self.assertIsNone(row["tokens"]["output_tokens"])
        self.assertIsNone(row["tokens"]["cache_read_tokens"])
        self.assertIsNone(row["tokens"]["cache_write_tokens"])
        self.assertIsNone(row["tokens"]["reasoning_tokens"])
        self.assertEqual(row["tokens"]["total_tokens"], 8)
        self.assertIsNone(payload["aggregate"]["cost_usd_sum"])
        self.assertEqual(payload["aggregate"]["cost_known_rows"], 0)
        self.assertIsNone(report._coerce_number(10 ** 10000))
        self.assertIsNone(report._coerce_number(True))
        self.assertIsNone(report._coerce_number(-1))
        self.assertIsNone(report._coerce_number(None))

    def test_handoff_valid_requires_an_actual_boolean_value(self):
        payload = self._collect(run=_maker_record(handoff_valid="true"), pair=False, review=False)
        self.assertIsNone(payload["rows"][0]["native_handoff_valid"])

    def test_duplicate_native_run_ids_emit_one_row(self):
        record = _maker_record()
        self._write(self.runs / "run-first.json", record)
        self._write(self.runs / "run-second.json", record)
        payload = report.collect(self.runs, report._parse_since(report.TRIAL_DEFAULT_SINCE), [])
        self.assertEqual(len(payload["rows"]), 1)
        self.assertEqual(payload["rows"][0]["run_id"], MAKER_ID)

    def test_symlink_component_is_rejected_before_resolution(self):
        target = self.root / "target"
        target.mkdir()
        link = self.root / "linked"
        try:
            link.symlink_to(target, target_is_directory=True)
            self.assertIsNone(report._resolve_strict(link / "missing"))
        except (OSError, NotImplementedError):
            with mock.patch.object(report, "_is_link_or_reparse", side_effect=lambda path: path == link):
                self.assertIsNone(report._resolve_strict(link / "missing"))

    def test_malformed_and_non_object_json_are_ignored(self):
        bad = self.runs / "run-bad.json"
        bad.write_text("{", encoding="utf-8")
        non_object = self.runs / "run-list.json"
        non_object.write_text("[]", encoding="utf-8")
        payload = report.collect(self.runs, report._parse_since(report.TRIAL_DEFAULT_SINCE), [])
        self.assertEqual(payload["rows"], [])


if __name__ == "__main__":
    unittest.main()
