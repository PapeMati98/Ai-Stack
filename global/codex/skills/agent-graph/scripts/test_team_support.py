from datetime import datetime, timedelta, timezone
import unittest

from team_support import (
    TeamError,
    effective_scope,
    knowledge_cache_key,
    monitor_report,
    validate_packet,
    validate_team,
)

NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def make_plan(**overrides):
    plan = {
        "version": 1,
        "goal": "Ship the thing",
        "planner": {"model": "claude-sonnet-5", "source": "parent"},
        "requirements": ["req-a"],
        "max_parallel": 2,
        "workspace": "D:/workspace",
        "tasks": [make_task()],
    }
    plan.update(overrides)
    return plan


def make_task(**overrides):
    task = {
        "id": "node-a",
        "text": "Do the work",
        "mode": "implement",
        "covers": ["req-a"],
        "done_when": ["works"],
        "scope": ["src/module_a"],
        "maker": [{"agent": "a", "model": "m", "reason": "fit", "score": 80}],
        "reviewer": [{"agent": "b", "model": "m2", "reason": "fit", "score": 70}],
        "checks": [["pytest"]],
        "depends_on": [],
    }
    task.update(overrides)
    return task


def make_packet(**overrides):
    packet = {
        "schema_version": 1,
        "project_slug": "proj-a",
        "node_id": "node-a",
        "generated_at": iso(NOW - timedelta(seconds=10)),
        "context_receipt": "receipt-123",
        "continuity_status": "OK",
        "facts": [
            {"text": "fact text", "source_ref": "wiki/page", "status": "reviewed",
             "observed_at": iso(NOW - timedelta(seconds=20))},
        ],
        "pending": [],
        "warnings": [],
        "truncated": False,
    }
    packet.update(overrides)
    return packet


class ValidateTeamBackwardCompatTests(unittest.TestCase):
    def test_plan_without_extensions_passes_unchanged(self):
        plan = make_plan()
        self.assertIs(validate_team(plan), plan)

    def test_task_without_extensions_defaults_to_maker_role(self):
        # No 'role', no 'model_class', no 'knowledge', no 'uat' -- must not raise.
        validate_team(make_plan(tasks=[make_task()]))


class ValidateTeamPlanFieldTests(unittest.TestCase):
    def test_valid_office_channel_and_parent(self):
        validate_team(make_plan(office_channel="chan-1", office_parent="parent.1"))

    def test_invalid_office_channel_rejected(self):
        with self.assertRaises(TeamError):
            validate_team(make_plan(office_channel="../etc"))

    def test_empty_office_parent_rejected(self):
        with self.assertRaises(TeamError):
            validate_team(make_plan(office_parent=""))

    def test_tasks_not_list_rejected(self):
        with self.assertRaises(TeamError):
            validate_team(make_plan(tasks="not-a-list"))


class ValidateTeamRoleTests(unittest.TestCase):
    def test_invalid_role_rejected(self):
        with self.assertRaises(TeamError):
            validate_team(make_plan(tasks=[make_task(role="architect")]))

    def test_scout_role_requires_advisory_mode(self):
        with self.assertRaises(TeamError):
            validate_team(make_plan(tasks=[make_task(role="scout", mode="implement")]))

    def test_scout_role_with_advisory_mode_passes(self):
        validate_team(make_plan(tasks=[make_task(role="scout", mode="advisory")]))

    def test_scout_role_defaults_mode_to_implement_and_fails(self):
        task = make_task(role="scout")
        task.pop("mode")
        with self.assertRaises(TeamError):
            validate_team(make_plan(tasks=[task]))

    def test_model_class_valid_values_pass(self):
        for model_class in ("mythos", "opus", "sonnet", "luna"):
            validate_team(make_plan(tasks=[make_task(model_class=model_class)]))

    def test_model_class_invalid_value_rejected(self):
        with self.assertRaises(TeamError):
            validate_team(make_plan(tasks=[make_task(model_class="astra")]))

    def test_unhashable_role_raises_team_error_not_type_error(self):
        with self.assertRaises(TeamError):
            validate_team(make_plan(tasks=[make_task(role=["maker"])]))

    def test_unhashable_model_class_raises_team_error_not_type_error(self):
        with self.assertRaises(TeamError):
            validate_team(make_plan(tasks=[make_task(model_class={"a": 1})]))


class ValidateTeamHistoricalScoutTests(unittest.TestCase):
    def test_duplicate_scouts_rejected(self):
        scout = make_task(role="scout", mode="advisory")
        plan = make_plan(tasks=[scout, dict(scout, id="node-b")])
        with self.assertRaisesRegex(TeamError, "HISTORICAL_SCOUT_DUPLICATE"):
            validate_team(plan)

    def test_contract_requires_consumer_dependency(self):
        scout = make_task(id="history", role="scout", mode="advisory")
        consumer = make_task(id="node-a", depends_on=[])
        plan = make_plan(tasks=[scout, consumer],
                         historical_scout={"topics": ["KSeF"], "consumers": ["node-a"]})
        with self.assertRaisesRegex(TeamError, "HISTORICAL_SCOUT_CONSUMER_DEPENDENCY_MISSING"):
            validate_team(plan)

    def test_valid_historical_scout_contract_passes(self):
        scout = make_task(id="history", role="scout", mode="advisory")
        consumer = make_task(id="node-a", depends_on=["history"])
        plan = make_plan(tasks=[scout, consumer],
                         historical_scout={"topics": ["KSeF", "invoice integration"],
                                           "consumers": ["node-a"], "max_records": 8})
        self.assertIs(validate_team(plan), plan)


class ValidateTeamKnowledgeTests(unittest.TestCase):
    def test_valid_knowledge_passes(self):
        task = make_task(knowledge={"project_slug": "proj-a", "query": "context", "required": True})
        validate_team(make_plan(tasks=[task], context_receipt_agent='codex', context_receipt_session='session-1'))

    def test_knowledge_requires_safe_receipt_owner(self):
        task = make_task(knowledge={"project_slug": "proj-a", "query": "context", "required": True})
        for fields in ({}, {'context_receipt_agent': 'codex'},
                       {'context_receipt_agent': '../other', 'context_receipt_session': 'session-1'},
                       {'context_receipt_agent': 'codex', 'context_receipt_session': '--spoof'}):
            with self.subTest(fields=fields), self.assertRaises(TeamError):
                validate_team(make_plan(tasks=[task], **fields))

    def test_knowledge_unknown_key_rejected(self):
        task = make_task(knowledge={"project_slug": "proj-a", "query": "context", "required": True, "extra": 1})
        with self.assertRaises(TeamError):
            validate_team(make_plan(tasks=[task]))

    def test_knowledge_unsafe_project_slug_rejected(self):
        task = make_task(knowledge={"project_slug": "../proj", "query": "context", "required": True})
        with self.assertRaises(TeamError):
            validate_team(make_plan(tasks=[task]))

    def test_knowledge_empty_query_rejected(self):
        task = make_task(knowledge={"project_slug": "proj-a", "query": "", "required": True})
        with self.assertRaises(TeamError):
            validate_team(make_plan(tasks=[task]))

    def test_knowledge_oversized_query_rejected(self):
        task = make_task(knowledge={"project_slug": "proj-a", "query": "x" * 501, "required": True})
        with self.assertRaises(TeamError):
            validate_team(make_plan(tasks=[task]))

    def test_knowledge_required_must_be_actual_bool(self):
        task = make_task(knowledge={"project_slug": "proj-a", "query": "context", "required": "true"})
        with self.assertRaises(TeamError):
            validate_team(make_plan(tasks=[task]))


def make_uat_block(**overrides):
    uat = {
        "environment": "test",
        "artifact_scope": ["src/module_a"],
        "scenarios": ["user logs in"],
        "final": False,
        "effects_blocked": True,
    }
    uat.update(overrides)
    return uat


class ValidateTeamUatTests(unittest.TestCase):
    def test_uat_role_requires_checks_depends_on_and_block(self):
        task = make_task(role="uat", checks=[], depends_on=[], uat=None)
        with self.assertRaises(TeamError):
            validate_team(make_plan(tasks=[task]))

    def test_uat_role_missing_depends_on_rejected(self):
        task = make_task(role="uat", checks=[["pytest"]], depends_on=[], uat=make_uat_block())
        with self.assertRaises(TeamError):
            validate_team(make_plan(tasks=[task]))

    def test_uat_role_missing_uat_block_rejected(self):
        task = make_task(role="uat", checks=[["pytest"]], depends_on=["node-b"], uat=None)
        with self.assertRaises(TeamError):
            validate_team(make_plan(tasks=[task]))

    def test_uat_role_complete_passes(self):
        task = make_task(role="uat", checks=[["pytest"]], depends_on=["node-b"], uat=make_uat_block())
        validate_team(make_plan(tasks=[task]))

    def test_uat_effects_blocked_must_be_literally_true(self):
        task = make_task(role="uat", checks=[["pytest"]], depends_on=["node-b"],
                          uat=make_uat_block(effects_blocked=False))
        with self.assertRaises(TeamError):
            validate_team(make_plan(tasks=[task]))

    def test_uat_effects_blocked_truthy_non_bool_rejected(self):
        task = make_task(role="uat", checks=[["pytest"]], depends_on=["node-b"],
                          uat=make_uat_block(effects_blocked=1))
        with self.assertRaises(TeamError):
            validate_team(make_plan(tasks=[task]))

    def test_uat_environment_invalid_rejected(self):
        task = make_task(uat=make_uat_block(environment="production"))
        with self.assertRaises(TeamError):
            validate_team(make_plan(tasks=[task]))

    def test_uat_artifact_scope_absolute_path_rejected(self):
        task = make_task(uat=make_uat_block(artifact_scope=["/etc/passwd"]))
        with self.assertRaises(TeamError):
            validate_team(make_plan(tasks=[task]))

    def test_uat_artifact_scope_drive_relative_path_rejected(self):
        task = make_task(uat=make_uat_block(artifact_scope=["C:/secrets"]))
        with self.assertRaises(TeamError):
            validate_team(make_plan(tasks=[task]))

    def test_uat_artifact_scope_traversal_backslash_rejected(self):
        task = make_task(uat=make_uat_block(artifact_scope=["..\\..\\secrets"]))
        with self.assertRaises(TeamError):
            validate_team(make_plan(tasks=[task]))

    def test_uat_artifact_scope_wildcard_rejected(self):
        task = make_task(uat=make_uat_block(artifact_scope=["src/*"]))
        with self.assertRaises(TeamError):
            validate_team(make_plan(tasks=[task]))

    def test_uat_artifact_scope_empty_rejected(self):
        task = make_task(uat=make_uat_block(artifact_scope=[]))
        with self.assertRaises(TeamError):
            validate_team(make_plan(tasks=[task]))

    def test_uat_scenarios_empty_rejected(self):
        task = make_task(uat=make_uat_block(scenarios=[]))
        with self.assertRaises(TeamError):
            validate_team(make_plan(tasks=[task]))

    def test_uat_unknown_key_rejected(self):
        uat = make_uat_block()
        uat["extra"] = "x"
        task = make_task(uat=uat)
        with self.assertRaises(TeamError):
            validate_team(make_plan(tasks=[task]))

    def test_unhashable_uat_environment_raises_team_error_not_type_error(self):
        task = make_task(uat=make_uat_block(environment=["test"]))
        with self.assertRaises(TeamError):
            validate_team(make_plan(tasks=[task]))


class ValidatePacketSchemaTests(unittest.TestCase):
    def test_valid_packet_passes(self):
        result = validate_packet(make_packet(), "proj-a", "node-a", NOW)
        self.assertFalse(result["requires_parent_review"])

    def test_not_a_dict_rejected(self):
        with self.assertRaises(TeamError):
            validate_packet("not-a-dict", "proj-a", "node-a", NOW)

    def test_oversized_packet_rejected(self):
        packet = make_packet(context_receipt="x" * 5000)
        with self.assertRaises(TeamError):
            validate_packet(packet, "proj-a", "node-a", NOW)

    def test_wrong_schema_version_rejected(self):
        with self.assertRaises(TeamError):
            validate_packet(make_packet(schema_version=2), "proj-a", "node-a", NOW)

    def test_mixed_project_slug_rejected(self):
        packet = make_packet(project_slug="proj-b")
        with self.assertRaises(TeamError):
            validate_packet(packet, "proj-a", "node-a", NOW)

    def test_mismatched_node_id_rejected(self):
        packet = make_packet(node_id="node-b")
        with self.assertRaises(TeamError):
            validate_packet(packet, "proj-a", "node-a", NOW)

    def test_stale_generated_at_rejected(self):
        packet = make_packet(generated_at=iso(NOW - timedelta(seconds=301)))
        with self.assertRaises(TeamError):
            validate_packet(packet, "proj-a", "node-a", NOW)

    def test_future_generated_at_within_tolerance_passes(self):
        packet = make_packet(generated_at=iso(NOW + timedelta(seconds=10)))
        validate_packet(packet, "proj-a", "node-a", NOW)

    def test_future_generated_at_beyond_tolerance_rejected(self):
        packet = make_packet(generated_at=iso(NOW + timedelta(seconds=31)))
        with self.assertRaises(TeamError):
            validate_packet(packet, "proj-a", "node-a", NOW)

    def test_naive_generated_at_rejected(self):
        packet = make_packet(generated_at=(NOW - timedelta(seconds=10)).replace(tzinfo=None).isoformat())
        with self.assertRaises(TeamError):
            validate_packet(packet, "proj-a", "node-a", NOW)

    def test_empty_context_receipt_rejected(self):
        with self.assertRaises(TeamError):
            validate_packet(make_packet(context_receipt=""), "proj-a", "node-a", NOW)

    def test_missing_continuity_status_type_rejected(self):
        with self.assertRaises(TeamError):
            validate_packet(make_packet(continuity_status=None), "proj-a", "node-a", NOW)

    def test_fact_bad_status_rejected(self):
        packet = make_packet(facts=[{"text": "t", "source_ref": "r", "status": "trust-me",
                                      "observed_at": iso(NOW)}])
        with self.assertRaises(TeamError):
            validate_packet(packet, "proj-a", "node-a", NOW)

    def test_fact_missing_source_ref_rejected(self):
        packet = make_packet(facts=[{"text": "t", "source_ref": "", "status": "reviewed",
                                      "observed_at": iso(NOW)}])
        with self.assertRaises(TeamError):
            validate_packet(packet, "proj-a", "node-a", NOW)

    def test_unhashable_fact_status_raises_team_error_not_type_error(self):
        packet = make_packet(facts=[{"text": "t", "source_ref": "r", "status": ["reviewed"],
                                      "observed_at": iso(NOW)}])
        with self.assertRaises(TeamError):
            validate_packet(packet, "proj-a", "node-a", NOW)

    def test_pending_live_verification_required_string_true_rejected(self):
        packet = make_packet(pending=[{"summary": "s", "source_ref": "r",
                                        "handoff_integrity": "COMPLETE",
                                        "live_verification_required": "true"}])
        with self.assertRaises(TeamError):
            validate_packet(packet, "proj-a", "node-a", NOW)

    def test_warnings_not_string_list_rejected(self):
        with self.assertRaises(TeamError):
            validate_packet(make_packet(warnings=[1, 2]), "proj-a", "node-a", NOW)

    def test_truncated_not_bool_rejected(self):
        with self.assertRaises(TeamError):
            validate_packet(make_packet(truncated="yes"), "proj-a", "node-a", NOW)


class ValidatePacketReviewFlagTests(unittest.TestCase):
    def test_stale_context_receipt_requires_parent_review(self):
        result = validate_packet(make_packet(warnings=['context_receipt_stale:vault_changed']),
                                 'proj-a', 'node-a', NOW)
        self.assertTrue(result['requires_parent_review'])

    def test_source_date_precision_preserved_and_review_required(self):
        for observed in (None, "2026-09-25"):
            with self.subTest(observed=observed):
                packet = make_packet(facts=[{"text": "t", "source_ref": "r",
                    "status": "verified", "observed_at": observed}])
                result = validate_packet(packet, "proj-a", "node-a", NOW)
                self.assertEqual(result["facts"][0]["observed_at"], observed)
                self.assertTrue(result["requires_parent_review"])
                self.assertIn("FACT_DATE_PRECISION_LIMITED", result["warnings"])

    def test_bad_source_dates_remain_invalid(self):
        for observed in ("2026-02-30", "unknown", "2026-09-25T12:00:00", 42, []):
            with self.subTest(observed=observed):
                packet = make_packet(facts=[{"text": "t", "source_ref": "r",
                    "status": "verified", "observed_at": observed}])
                with self.assertRaises(TeamError):
                    validate_packet(packet, "proj-a", "node-a", NOW)

    def test_unknown_source_age_never_refreshes_an_old_packet(self):
        packet = make_packet(generated_at=iso(NOW - timedelta(seconds=301)),
            facts=[{"text": "t", "source_ref": "r", "status": "reviewed", "observed_at": None}])
        with self.assertRaises(TeamError):
            validate_packet(packet, "proj-a", "node-a", NOW)

    def test_boolean_schema_version_rejected(self):
        with self.assertRaises(TeamError):
            validate_packet(make_packet(schema_version=True), "proj-a", "node-a", NOW)

    def test_truncation_forces_review_and_warning(self):
        result = validate_packet(make_packet(truncated=True), "proj-a", "node-a", NOW)
        self.assertTrue(result["requires_parent_review"])
        self.assertIn("TRUNCATED", result["warnings"])

    def test_pending_items_force_review(self):
        packet = make_packet(pending=[{"summary": "s", "source_ref": "r",
                                        "handoff_integrity": "COMPLETE",
                                        "live_verification_required": True}])
        result = validate_packet(packet, "proj-a", "node-a", NOW)
        self.assertTrue(result["requires_parent_review"])

    def test_producer_attested_fact_forces_review(self):
        packet = make_packet(facts=[{"text": "t", "source_ref": "r", "status": "producer-attested",
                                      "observed_at": iso(NOW)}])
        result = validate_packet(packet, "proj-a", "node-a", NOW)
        self.assertTrue(result["requires_parent_review"])

    def test_missing_continuity_forces_review(self):
        packet = make_packet(continuity_status="MISSING")
        result = validate_packet(packet, "proj-a", "node-a", NOW)
        self.assertTrue(result["requires_parent_review"])

    def test_clean_verified_packet_does_not_force_review(self):
        packet = make_packet(facts=[{"text": "t", "source_ref": "r", "status": "verified",
                                      "observed_at": iso(NOW)}])
        result = validate_packet(packet, "proj-a", "node-a", NOW)
        self.assertFalse(result["requires_parent_review"])

    def test_result_never_claims_consumption(self):
        result = validate_packet(make_packet(), "proj-a", "node-a", NOW)
        self.assertNotIn("consumed", result)


class KnowledgeCacheKeyTests(unittest.TestCase):
    def test_stable_for_same_inputs(self):
        key1 = knowledge_cache_key("proj-a", "node-a", "context", ["src"], "receipt-1")
        key2 = knowledge_cache_key("proj-a", "node-a", "context", ["src"], "receipt-1")
        self.assertEqual(key1, key2)

    def test_scope_order_does_not_change_key(self):
        key1 = knowledge_cache_key("proj-a", "node-a", "context", ["src", "docs"], "receipt-1")
        key2 = knowledge_cache_key("proj-a", "node-a", "context", ["docs", "src"], "receipt-1")
        self.assertEqual(key1, key2)

    def test_different_receipt_changes_key(self):
        key1 = knowledge_cache_key("proj-a", "node-a", "context", ["src"], "receipt-1")
        key2 = knowledge_cache_key("proj-a", "node-a", "context", ["src"], "receipt-2")
        self.assertNotEqual(key1, key2)

    def test_unsafe_project_slug_rejected(self):
        with self.assertRaises(TeamError):
            knowledge_cache_key("../proj", "node-a", "context", ["src"], "receipt-1")


def make_state(nodes=None, status="BLOCKED"):
    return {"status": status, "nodes": nodes or {}}


class MonitorReportCoverageTests(unittest.TestCase):
    def test_uncovered_requirement_is_incomplete(self):
        plan = make_plan(requirements=["req-a", "req-b"], tasks=[make_task(covers=["req-a"])])
        state = make_state({"node-a": {"status": "ACCEPTED"}})
        result = monitor_report(plan, state, now=NOW)
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertIn("req-b", result["uncovered_requirements"])
        self.assertIn("UNCOVERED_REQUIREMENTS", result["issues"])

    def test_partial_task_coverage_is_incomplete(self):
        plan = make_plan(requirements=["req-a"], tasks=[
            make_task(id="node-a", covers=["req-a"]),
            make_task(id="node-b", covers=["req-a"], depends_on=[]),
        ])
        state = make_state({"node-a": {"status": "ACCEPTED"}, "node-b": {"status": "RUNNING"}})
        result = monitor_report(plan, state, now=NOW)
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertFalse(result["requirements"]["req-a"]["satisfied"])
        self.assertIn("node-b", result["running_tasks"])

    def test_all_accepted_is_candidate_complete(self):
        plan = make_plan(requirements=["req-a"], tasks=[make_task(covers=["req-a"])])
        state = make_state({"node-a": {"status": "ACCEPTED"}}, status="ACCEPTED_CANDIDATE")
        result = monitor_report(plan, state, now=NOW)
        self.assertEqual(result["status"], "CANDIDATE_COMPLETE")
        self.assertEqual(result["issues"], [])

    def test_status_is_never_an_acceptance_or_production_word(self):
        plan = make_plan(requirements=["req-a"], tasks=[make_task(covers=["req-a"])])
        state = make_state({"node-a": {"status": "ACCEPTED"}}, status="ACCEPTED_CANDIDATE")
        result = monitor_report(plan, state, now=NOW)
        self.assertIn(result["status"], {"INCOMPLETE", "CANDIDATE_COMPLETE"})


class MonitorReportIntegrityTests(unittest.TestCase):
    def test_duplicate_requirement_flagged_and_never_candidate_complete(self):
        plan = make_plan(requirements=["req-a", "req-a"], tasks=[make_task(covers=["req-a"])])
        state = make_state({"node-a": {"status": "ACCEPTED"}})
        result = monitor_report(plan, state, now=NOW)
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertIn("DUPLICATE_REQUIREMENTS", result["issues"])

    def test_unknown_requirement_in_covers_flagged(self):
        plan = make_plan(requirements=["req-a"], tasks=[make_task(covers=["req-a", "req-ghost"])])
        state = make_state({"node-a": {"status": "ACCEPTED"}})
        result = monitor_report(plan, state, now=NOW)
        self.assertIn("req-ghost", result["unknown_requirements"])
        self.assertIn("UNKNOWN_REQUIREMENTS", result["issues"])

    def test_unhashable_node_status_raises_team_error_not_type_error(self):
        plan = make_plan(requirements=["req-a"], tasks=[make_task(covers=["req-a"])])
        state = make_state({"node-a": {"status": ["ACCEPTED"]}})
        result = monitor_report(plan, state, now=NOW)
        self.assertIn("node-a", result["unknown_status_tasks"])

    def test_unhashable_task_id_raises_team_error_not_type_error(self):
        plan = make_plan(requirements=["req-a"], tasks=[make_task(id=["node-a"], covers=["req-a"])])
        state = make_state({})
        with self.assertRaises(TeamError):
            monitor_report(plan, state, now=NOW)

    def test_unhashable_covers_entry_raises_team_error_not_type_error(self):
        plan = make_plan(requirements=["req-a"], tasks=[make_task(covers=[["req-a"]])])
        state = make_state({"node-a": {"status": "ACCEPTED"}})
        with self.assertRaises(TeamError):
            monitor_report(plan, state, now=NOW)

    def test_unknown_state_node_flagged(self):
        plan = make_plan(requirements=["req-a"], tasks=[make_task(covers=["req-a"])])
        state = make_state({"node-a": {"status": "ACCEPTED"}, "ghost-node": {"status": "ACCEPTED"}})
        result = monitor_report(plan, state, now=NOW)
        self.assertIn("ghost-node", result["unknown_state_nodes"])
        self.assertEqual(result["status"], "INCOMPLETE")

    def test_contradictory_terminal_status_flagged(self):
        plan = make_plan(requirements=["req-a"], tasks=[make_task(covers=["req-a"])])
        state = make_state({"node-a": {"status": "RUNNING"}}, status="ACCEPTED_CANDIDATE")
        result = monitor_report(plan, state, now=NOW)
        self.assertIn("CONTRADICTORY_TERMINAL_STATUS", result["issues"])
        self.assertEqual(result["status"], "INCOMPLETE")

    def test_never_authorizes_acceptance_even_when_complete(self):
        plan = make_plan(requirements=["req-a"], tasks=[make_task(covers=["req-a"])])
        state = make_state({"node-a": {"status": "ACCEPTED"}}, status="ACCEPTED_CANDIDATE")
        result = monitor_report(plan, state, now=NOW)
        for forbidden in ("ACCEPT", "PRODUCTION_READY", "DEPLOY"):
            self.assertNotEqual(result["status"], forbidden)
            self.assertNotIn(forbidden, result.get("issues", []))


class MonitorReportObservationsTests(unittest.TestCase):
    def test_stalled_candidate_detected(self):
        plan = make_plan(requirements=["req-a"], tasks=[make_task(covers=["req-a"])])
        state = make_state({"node-a": {"status": "RUNNING"}})
        observations = {"node-a": {"elapsed_seconds": 100, "timeout_seconds": 60, "delivery_state": "sent"}}
        result = monitor_report(plan, state, now=NOW, observations=observations)
        self.assertEqual(len(result["stalled_candidates"]), 1)
        self.assertIn("STALLED_CANDIDATES", result["issues"])

    def test_missing_timing_is_not_treated_as_stall(self):
        plan = make_plan(requirements=["req-a"], tasks=[make_task(covers=["req-a"])])
        state = make_state({"node-a": {"status": "RUNNING"}})
        observations = {"node-a": {"delivery_state": "sent"}}
        result = monitor_report(plan, state, now=NOW, observations=observations)
        self.assertEqual(result["stalled_candidates"], [])
        self.assertNotIn("STALLED_CANDIDATES", result["issues"])

    def test_non_positive_timeout_rejected(self):
        plan = make_plan(requirements=["req-a"], tasks=[make_task(covers=["req-a"])])
        state = make_state({"node-a": {"status": "RUNNING"}})
        observations = {"node-a": {"elapsed_seconds": 10, "timeout_seconds": 0}}
        with self.assertRaises(TeamError):
            monitor_report(plan, state, now=NOW, observations=observations)

    def test_no_automatic_retry_state_untouched(self):
        plan = make_plan(requirements=["req-a"], tasks=[make_task(covers=["req-a"])])
        state = make_state({"node-a": {"status": "BLOCKED"}})
        result = monitor_report(plan, state, now=NOW)
        self.assertIn("node-a", result["blocked_tasks"])
        self.assertEqual(state["nodes"]["node-a"]["status"], "BLOCKED")


class MonitorReportFingerprintTests(unittest.TestCase):
    def test_fingerprint_stable_across_different_now(self):
        plan = make_plan(requirements=["req-a", "req-b"], tasks=[make_task(covers=["req-a"])])
        state = make_state({"node-a": {"status": "ACCEPTED"}})
        result1 = monitor_report(plan, state, now=NOW)
        result2 = monitor_report(plan, state, now=NOW + timedelta(hours=5))
        self.assertEqual(result1["alert_fingerprint"], result2["alert_fingerprint"])

    def test_fingerprint_changes_with_new_issue(self):
        plan = make_plan(requirements=["req-a", "req-b"], tasks=[make_task(covers=["req-a"])])
        state1 = make_state({"node-a": {"status": "ACCEPTED"}})
        state2 = make_state({"node-a": {"status": "ACCEPTED"}, "ghost": {"status": "ACCEPTED"}})
        result1 = monitor_report(plan, state1, now=NOW)
        result2 = monitor_report(plan, state2, now=NOW)
        self.assertNotEqual(result1["alert_fingerprint"], result2["alert_fingerprint"])


class EffectiveScopeTests(unittest.TestCase):
    def test_no_uat_returns_scope_only(self):
        task = make_task(scope=["src/a", "src/b"])
        self.assertEqual(effective_scope(task), ["src/a", "src/b"])

    def test_uat_artifact_scope_merged_and_deduplicated_in_order(self):
        task = make_task(scope=["src/a", "src/b"],
                          uat=make_uat_block(artifact_scope=["src/b", "docs/c"]))
        self.assertEqual(effective_scope(task), ["src/a", "src/b", "docs/c"])

    def test_missing_scope_rejected(self):
        task = make_task()
        del task["scope"]
        with self.assertRaises(TeamError):
            effective_scope(task)


if __name__ == "__main__":
    unittest.main()
