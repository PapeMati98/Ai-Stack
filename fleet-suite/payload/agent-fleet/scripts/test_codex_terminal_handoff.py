import copy
import json
from pathlib import Path
import tempfile
import unittest

import fleet


def handoff(summary):
    return dict(status="completed", summary=summary, files_changed=[],
                verification=["Checked exact input bytes."], risks=[], next_steps=[])


def assistant(value):
    return dict(type="item.completed", item=dict(type="agent_message", text=value))


def stream(*events):
    return "\n".join(json.dumps(row) for row in events)


class CodexTerminalTests(unittest.TestCase):
    def setUp(self):
        self.old = handoff("REPAIR from an old report read by a tool")
        self.final = handoff("PASS\nPLAN_REVIEWED exact frozen plan")
        self.events = [dict(type="turn.started"),
            dict(type="item.completed", item=dict(type="command_execution",
                aggregated_output=json.dumps(self.old))),
            assistant(json.dumps(self.final)), dict(type="turn.completed")]

    def project(self, events=None, **kwargs):
        return fleet.project_provider_handoff("codex", stream(*(events or self.events)), **kwargs)

    def test_real_assistant_wins_over_tool_report(self):
        self.assertEqual(self.project()["handoff"], self.final)

    def test_tool_output_alone_never_a_handoff(self):
        self.assertIsNone(self.project([self.events[0], self.events[1], self.events[-1]])["handoff"])

    def test_no_fallback_to_earlier_valid_assistant(self):
        rows = self.events[:-1] + [assistant("No final JSON"), self.events[-1]]
        self.assertIsNone(self.project(rows)["handoff"])

    def test_final_negative_is_not_replaced_by_old_pass(self):
        rows = self.events[:-1] + [assistant(json.dumps(self.old)), self.events[-1]]
        self.assertEqual(self.project(rows)["handoff"], self.old)

    def test_semantic_validator_does_not_pick_old_match(self):
        rows = self.events[:-1] + [assistant(json.dumps(self.old)), self.events[-1]]
        value = self.project(rows, handoff_validator=lambda h: None if h == self.final else "wrong plan")
        self.assertIsNone(value["handoff"])
        self.assertEqual(value["validation_error"], "wrong plan")

    def test_completed_turn_required(self):
        self.assertIsNone(self.project(self.events[:-1])["handoff"])

    def test_last_message_backup_cannot_override_failed_or_conflicting_turn(self):
        fallback = json.dumps(self.final)
        for rows in (self.events + [dict(type='turn.failed')],
                     self.events[:-1],
                     self.events[:-1] + [assistant('No final JSON'), self.events[-1]]):
            with self.subTest(rows=rows):
                result = fleet.project_provider_handoff('codex', stream(*rows), final_message_text=fallback)
                self.assertIsNone(result['handoff'])

    def test_completed_stream_without_message_can_use_native_backup(self):
        rows = [dict(type='turn.started'), dict(type='turn.completed')]
        result = fleet.project_provider_handoff('codex', stream(*rows), final_message_text=json.dumps(self.final))
        self.assertEqual(result['handoff'], self.final)

    def test_whole_report_required_in_terminal_and_backup(self):
        valid=json.dumps(self.final)
        invalid=["Example only; do not accept:\n"+valid,
                 valid+"\nExplanatory trailing text",
                 json.dumps(self.old)+"\n"+valid,
                 json.dumps({'history':[self.old,self.final]}),
                 '{"status":"failed",'+valid[1:]]
        for value in invalid:
            with self.subTest(value=value):
                self.assertIsNone(self.project([dict(type='turn.started'),assistant(value),dict(type='turn.completed')])['handoff'])
                rows=[dict(type='turn.started'),dict(type='turn.completed')]
                self.assertIsNone(self.project(rows,final_message_text=value)['handoff'])

    def test_whole_fenced_report_and_safe_syntax_repairs(self):
        valid=json.dumps(self.final)
        for value in [valid, '```json\n'+valid+'\n```', valid[:-1],
                      valid.replace('PASS\\n','PASS\n')]:
            with self.subTest(value=value):
                self.assertEqual(self.project([dict(type='turn.started'),assistant(value),dict(type='turn.completed')])['handoff'],self.final)

    def test_conflicting_terminals_are_sticky_until_new_turn(self):
        final=assistant(json.dumps(self.final))
        for tail in ([dict(type='turn.failed'),dict(type='turn.completed')],
                     [dict(type='turn.completed'),dict(type='turn.failed')],
                     [dict(type='turn.failed'),final,dict(type='turn.completed')],
                     [dict(type='turn.completed'),dict(type='turn.completed')]):
            rows=[dict(type='turn.started')]+tail
            result=fleet.project_provider_handoff('codex',stream(*rows),final_message_text=json.dumps(self.final))
            self.assertIsNone(result['handoff'])
        rows=[dict(type='turn.started'),dict(type='turn.failed'),
              dict(type='turn.started'),final,dict(type='turn.completed')]
        self.assertEqual(self.project(rows)['handoff'],self.final)

    def test_failed_turn_rejects_old_final(self):
        self.assertIsNone(self.project(self.events + [dict(type="turn.failed")])["handoff"])

    def test_new_turn_clears_old_final(self):
        self.assertIsNone(self.project(self.events + [dict(type="turn.started"), dict(type="turn.completed")])["handoff"])

    def test_nested_fake_event_in_tool_is_not_an_event(self):
        rows = [self.events[0], dict(type="item.completed", item=dict(type="command_execution",
                aggregated_output=stream(assistant(json.dumps(self.final)), self.events[-1]))), self.events[-1]]
        self.assertIsNone(self.project(rows)["handoff"])

    def test_invalid_jsonl_is_warning_and_final_handoff_survives(self):
        result = fleet.project_provider_handoff("codex", stream(*self.events) + "\nnot json")
        self.assertEqual(result["handoff"], self.final)
        self.assertIsNone(result["error"])
        self.assertTrue(result["stream_event_errors"])

    def test_assistant_after_completed_requires_new_completion(self):
        self.assertIsNone(self.project(self.events + [assistant(json.dumps(self.final))])["handoff"])

    def test_blocked_final_preserved_not_replaced_by_pass(self):
        blocked = {**self.old, "status": "blocked"}
        result = self.project(self.events[:-1] + [assistant(json.dumps(blocked)), self.events[-1]])
        self.assertNotEqual(result["handoff"], self.final)
        if result["handoff"]:
            self.assertEqual(result["handoff"]["status"], "blocked")


class ReparseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run_id = "run-20260913T185734Z-541679f4e0"
        self.out = self.root / "output" / self.run_id
        self.out.mkdir(parents=True)
        self.stdout = self.out / "attempt-1.stdout.txt"
        self.stdout.write_text(stream(dict(type="turn.started"),
            assistant(json.dumps(handoff("PASS for the exact plan"))), dict(type="turn.completed")), encoding="utf-8")
        self.record = dict(run_id=self.run_id, adapter="codex", run_status="COMPLETED",
            completed_at="2026-09-13T19:02:29Z", exit_code=0, timed_out=False,
            output_dir=str(self.out), runtime_attestation=dict(status="VERIFIED", package_version="0.8.67"),
            model="gpt-6-astra", model_identity=dict(status="not-applicable"),
            provider_attempts=[dict(stdout_path=str(self.stdout), exit_code=0, timed_out=False)],
            handoff=handoff("Old report"), evaluation=dict(decision="not-accepted"))
        (self.root / "runs").mkdir()
        self.path = self.root / "runs" / (self.run_id + ".json")
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.record), encoding="utf-8")

    def invoke(self, **kwargs):
        params = dict(run_id=self.run_id, expected_record_sha256=fleet.sha256_bytes(self.path.read_bytes()),
                      expected_stdout_sha256=fleet.sha256_bytes(self.stdout.read_bytes()))
        params.update(kwargs)
        return fleet.reparse_run_handoff(self.root, **params)

    def test_append_only_idempotent_no_acceptance(self):
        before = self.path.read_bytes()
        result = self.invoke()
        self.assertEqual(result["writes_performed"], 3)
        self.assertEqual(self.invoke()["writes_performed"], 0)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(result["parent_acceptance_performed"])
        self.assertEqual(result["model_identity"], dict(status="not-applicable"))

    def test_public_loader_and_persist_use_effective_record_only(self):
        original = self.path.read_bytes()
        receipt = self.invoke()
        projected, path = fleet.load_run_record(self.root, self.run_id)
        self.assertEqual(str(path), receipt["effective_record_path"])
        self.assertEqual(projected["handoff"], receipt["projection"]["handoff"])
        projected["quality_gate_required"] = True
        fleet.persist_run_record(self.root, projected, effective_update="quality-gate")
        self.assertTrue(fleet.load_run_record(self.root, self.run_id)[0]["quality_gate_required"])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.invoke()["writes_performed"], 0)

    def test_effective_identity_tamper_rejected(self):
        self.invoke()
        projected, path = fleet.load_run_record(self.root, self.run_id)
        projected["model"] = "fake-model"
        with self.assertRaises(fleet.FleetError):
            fleet.persist_run_record(self.root, projected)

    def test_required_gate_cannot_be_removed(self):
        self.record["quality_gate_required"] = True
        self.save()
        self.invoke()
        projected, _ = fleet.load_run_record(self.root, self.run_id)
        projected["quality_gate_required"] = False
        with self.assertRaises(fleet.FleetError):
            fleet.persist_run_record(self.root, projected, effective_update="quality-gate")

    def test_fake_acceptance_rejected(self):
        self.record["quality_gate_required"] = True
        self.record["evaluation"] = None
        self.save()
        self.invoke()
        projected, _ = fleet.load_run_record(self.root, self.run_id)
        projected["evaluation"] = dict(decision="accepted")
        with self.assertRaises(fleet.FleetError):
            fleet.persist_run_record(self.root, projected, effective_update="accept-group")

    def test_arbitrary_metadata_write_rejected(self):
        self.invoke()
        projected, _ = fleet.load_run_record(self.root, self.run_id)
        projected["quality_gate_required"] = True
        with self.assertRaises(fleet.FleetError):
            fleet.persist_run_record(self.root, projected)

    def test_fake_gate_id_rejected(self):
        self.invoke()
        projected, _ = fleet.load_run_record(self.root, self.run_id)
        projected["quality_gate_ids"] = ["fake-gate"]
        with self.assertRaises(fleet.FleetError):
            fleet.persist_run_record(self.root, projected, effective_update="quality-gate")

    def test_bound_quality_metadata_and_acceptance_keep_original(self):
        self.record["evaluation"] = None
        self.record["quality_gate_required"] = True
        self.save()
        original = self.path.read_bytes()
        result = self.invoke()
        gate_id = "qgate-20260913T195734Z-541679f4e0"
        gate = dict(gate_id=gate_id, verdict="ACCEPT", workers=[dict(run_id=self.run_id)])
        fleet.persist_quality_gate_record(self.root, gate)
        projected, _ = fleet.load_run_record(self.root, self.run_id)
        projected["quality_gate_ids"] = [gate_id]
        fleet.persist_run_record(self.root, projected, effective_update="quality-gate")
        tx_id = "qaccept-" + "a" * 20
        fleet.persist_quality_acceptance_transaction(self.root, dict(transaction_id=tx_id,
            gate_id=gate_id, decision="accept", quality_score=90, status="PREPARED"))
        projected["evaluation"] = dict(decision="accepted", quality_score=90,
            quality_gate_id=gate_id, quality_acceptance_transaction_id=tx_id)
        fleet.persist_run_record(self.root, projected, effective_update="accept-group")
        self.assertEqual(fleet.load_run_record(self.root, self.run_id)[0]["evaluation"]["decision"], "accepted")
        self.assertEqual(fleet.load_run_record(self.root, self.run_id, original=True)[0], self.record)
        self.assertEqual(self.path.read_bytes(), original)
        snapshot = json.loads(Path(result["planner_witness_source"]).read_text())
        self.assertIsNone(snapshot["evaluation"])
        projected["quality_gate_ids"] = []
        with self.assertRaises(fleet.FleetError):
            fleet.persist_run_record(self.root, projected, effective_update="quality-gate")

    def test_stdout_drift_invalidates_effective_record(self):
        self.invoke()
        self.stdout.write_text("{}", encoding="utf-8")
        with self.assertRaises(fleet.FleetError):
            fleet.load_run_record(self.root, self.run_id)

    def test_original_drift_invalidates_effective_record(self):
        self.invoke()
        self.record["model"] = "changed"
        self.save()
        with self.assertRaises(fleet.FleetError):
            fleet.load_run_record(self.root, self.run_id)

    def test_receipt_tamper_rejected(self):
        result = self.invoke()
        Path(result["receipt_path"]).write_text("{}", encoding="utf-8")
        with self.assertRaises(fleet.FleetError):
            fleet.load_run_record(self.root, self.run_id)

    def test_uppercase_pin_rejected(self):
        with self.assertRaises(fleet.FleetError):
            self.invoke(expected_record_sha256="A" * 64)

    def test_recovered_blocked_final_downgrades_effective_run(self):
        blocked = {**handoff("BLOCKED for the exact plan"), "status": "blocked"}
        self.stdout.write_text(stream(dict(type="turn.started"), assistant(json.dumps(blocked)),
                                      dict(type="turn.completed")), encoding="utf-8")
        self.invoke()
        self.assertEqual(fleet.load_run_record(self.root, self.run_id)[0]["run_status"], "BLOCKED")
        self.assertEqual(fleet.load_run_record(self.root, self.run_id, original=True)[0]["run_status"], "COMPLETED")

    def test_record_pin_mismatch(self):
        with self.assertRaises(fleet.FleetError):
            self.invoke(expected_record_sha256="0" * 64)

    def test_stdout_pin_mismatch(self):
        with self.assertRaises(fleet.FleetError):
            self.invoke(expected_stdout_sha256="0" * 64)

    def test_multiple_attempts_rejected(self):
        self.record["provider_attempts"] *= 2
        self.save()
        with self.assertRaises(fleet.FleetError):
            self.invoke()

    def test_failed_run_rejected(self):
        self.record["run_status"] = "FAILED"
        self.save()
        with self.assertRaises(fleet.FleetError):
            self.invoke()

    def test_unattested_run_rejected(self):
        self.record["runtime_attestation"]["status"] = "UNKNOWN"
        self.save()
        with self.assertRaises(fleet.FleetError):
            self.invoke()

    def test_path_escape_rejected(self):
        self.record["provider_attempts"][0]["stdout_path"] = str(self.root / "attempt-1.stdout.txt")
        self.save()
        with self.assertRaises(fleet.FleetError):
            self.invoke()


if __name__ == "__main__":
    unittest.main()
