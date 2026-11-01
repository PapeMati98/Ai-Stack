"""Execute current local tests and record truthful 31-case candidate evidence.

PASS is local synthetic behavior only. External qualifications remain BLOCKED.
Fixtures stay inside this task workspace; reports use an explicit absolute root.
All directories are retained, with a fresh UUID report directory on each run.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import time
import unittest
import uuid

FARM = Path(__file__).resolve().parents[1]
WORKSPACE = FARM.parents[1]
sys.path.insert(0, str(FARM))
sys.path.insert(0, str(FARM / "tests"))

# id, case, observable expected result, executed test method names, external gap
CASES = [
    (1, "empty quiet", "Empty scan: no inference or Office message, last success advances.", ["test_empty_quiet_and_failed_scan_preserve_success"], None),
    (2, "failed scan", "Failed/inconclusive scans remain visible and never advance last success.", ["test_empty_quiet_and_failed_scan_preserve_success"], None),
    (3, "restart coalescing", "One poll after missed intervals; unchanged input never reclassifies.", ["test_restart_coalesces_missed_intervals_and_changes_only", "test_complete_owner_journey_across_process_restarts"], None),
    (4, "single lease and DST", "Concurrent writer cannot claim lease; fresh constructor+tick workers, eight constructors and a short journal writer are safe; prolonged initialization lock fails within its deadline; unsupported timezone is rejected.", ["test_single_writer_lease_and_unsupported_dst", "test_concurrent_claims_have_exactly_one_fenced_writer", "test_concurrent_fresh_engine_startup_and_invoice_ticks", "test_eight_simultaneous_fresh_engine_constructors", "test_engine_startup_during_short_existing_journal_write", "test_journal_startup_lock_has_finite_deadline_and_recovers"], "Daily/local-time/DST scheduling is unsupported; only UTC intervals were exercised."),
    (5, "outage and budget pending", "Budget zero performs no call; persisted input is later interpreted even if source becomes empty; resumed pre-inference cancellation and process loss before reserve or after zero settlement recheck current caps before another call.", ["test_budget_pending_survives_restart_and_empty_source", "test_timeout_cost_held_no_automatic_retry_or_effect", "test_pre_inference_stop_rechecks_reduced_budget_after_restart", "test_missing_pre_inference_reservation_rechecks_budget", "test_settled_zero_pre_inference_reservation_rechecks_budget"], "Real source/model outages and semantic quality are unqualified."),
    (6, "emergency within global cap", "Ordinary/emergency reservations fit one cap, reserve is never added to cap.", ["test_emergency_partition_is_within_cap_and_not_double_withheld"], None),
    (7, "atomic reservations", "Concurrent per-DOTS/global reservations count once and stay within caps.", ["test_atomic_concurrent_global_and_per_dot_caps", "test_concurrent_duplicate_reservations_and_settlement_count_once"], None),
    (8, "unknown timeout held", "Unknown inference retains 10 microUSD across reopen, including after pre-inference cancellation or a saved decision interrupted before settlement; explicit settlement frees hold with no repeat call.", ["test_timeout_cost_held_no_automatic_retry_or_effect", "test_unknown_response_cost_requires_explicit_reconciliation", "test_cli_explicit_unknown_cost_reconciliation_across_restart", "test_interrupted_inference_journal_recovers_hold", "test_interrupted_unknown_decision_blocks_effect_after_reopen", "test_cli_interrupted_decision_cost_policy_survives_restart", "test_pre_inference_stop_unknown_cost_retains_reconcilable_hold"], "Paid provider cost reconciliation needs measured receipts and dedicated qualification."),
    (9, "bounded loops and cache misses", "Changed items are bounded; unknown target writes are not reissued and lookup stops at bound.", ["test_unknown_target_lookup_only_and_bounded_retry", "test_queue_payload_and_retained_journal_storage_caps"], "OCR is NOT_IMPLEMENTED; real OCR/token/cache cost/resource qualification is BLOCKED."),
    (10, "forbidden or costly fallback", "Above max-call actual is recorded and stops effects even after interrupted settlement/reopen; forbidden model/fallback/redirect is rejected.", ["test_costly_and_forbidden_model_stop_before_effect", "test_interrupted_cost_overrun_stops_effect_after_reopen", "test_cli_interrupted_decision_cost_policy_survives_restart", "test_forbidden_model_denied", "test_no_retry_fallback_redirect_and_secret_redaction"], "Only fake provider transports were exercised; Engine/CLI have no live model wiring."),
    (11, "input injection", "Untrusted memory and tool-shaped output never broaden actions or execute shell/eval.", ["test_injection_extra_tools_and_memory_poison_cannot_grant", "test_malformed_decisions_rejected"], "No real mail/PDF/OCR or adversarial LLM recall corpus was used."),
    (12, "cross org denial", "Foreign source is excluded and foreign invoice becomes owner metadata with no draft.", ["test_cross_org_source_and_invoice_denied", "test_cross_org_peer_references_rejected_in_both_creation_orders"], None),
    (13, "peer bounds", "Peer TTL/hops/schema/grants rejected when invalid; retries dedupe and unknown effects stay bounded.", ["test_peer_ttl_hops_dedup_and_confused_deputy", "test_unknown_target_lookup_only_and_bounded_retry"], "No real Office peer worker delivery/execution was activated."),
    (14, "confused deputy", "Another DOTS cannot turn metadata into tools or gain a collect capability.", ["test_peer_ttl_hops_dedup_and_confused_deputy", "test_untrusted_memory_never_broadens_intent_grants"], None),
    (15, "two collectors duplicate invoice", "Two current Store collectors finish confirmed with exactly one target invoice, including concurrent normalized-identical payloads.", ["test_duplicate_invoice_two_collectors_one_persisted_target", "test_concurrent_identical_invoice_claims_share_one_target", "test_target_concurrent_duplicate_and_restart_preserve_one_invoice"], None),
    (16, "write timeout lookup", "Target commits then times out; engine lookup confirms one persisted draft. Paused reconcile performs no lookup; explicit resume/reconcile/pause confirms the persistent receipt without a tick or new cost/write.", ["test_timeout_after_write_lookup_confirms_without_reissue", "test_cli_unknown_cost_and_lookup_reconcile_read_actual_adapters", "test_cli_paused_lookup_recovery_runbook_preserves_stop"], None),
    (17, "snapshot restore", "Restore controller/journal pair while retaining actual target; still exactly one invoice.", ["test_snapshot_restore_target_idempotency"], "Real recovery/paid ledger reconciliation and historical payload conflicts after restore are unqualified; target lookup is opaque."),
    (18, "invalid invoice", "Invalid org/date/amount/identity/currency, changed amount on the same source ID and sequential/concurrent collector conflicts produce owner metadata, no new draft; concurrent contenders construct their Engines inside fresh-journal tick workers.", ["test_invalid_invoice_is_owner_escalation_never_final_write", "test_conflicting_invoice_identity_requires_owner", "test_conflicting_invoice_two_collectors_requires_owner", "test_concurrent_invoice_conflicts_are_atomic", "test_concurrent_fresh_engine_startup_and_invoice_ticks"], "Synthetic drafts only; tax/final invoice/business approval is not implemented."),
    (19, "bug and business decision", "Needs-owner becomes neutral metadata, never coding work or content disclosure.", ["test_bug_metadata_and_owner_decision_do_not_start_coding"], None),
    (20, "STOP during inference and before effect", "Independent STOP fences inference result and committed intent before dispatch; pre-inference STOP releases unused hold, and repeated resume or restart after zero settlement obtains fresh holds without consuming inference attempts or losing known cost.", ["test_stop_during_model_and_intent_precedes_effect", "test_pause_cli_independent_of_hung_model_process", "test_pre_inference_stop_reopen_keeps_call_budget_and_attempt_limit", "test_settled_zero_pre_inference_reservation_reopens_with_fresh_hold", "test_cli_hard_exit_after_zero_settlement_recovers_with_fresh_hold"], "Remote in-flight effect fencing/STOP races require target/VM qualification."),
    (21, "version pin", "Changed grant version fences current run and blocks old decisions; pre-inference grant change and lease takeover reopen with a fresh hold and exact known cost.", ["test_grant_version_change_fences_current_run", "test_intents_require_scope_actions_version_and_bounded_payloads", "test_pre_inference_grant_change_reopen_has_fresh_hold", "test_pre_inference_lease_takeover_reopen_has_fresh_hold"], "CLI grant/config mutation is not implemented; fixture simulates an authorized config writer."),
    (22, "resource bounds", "Queue/payload/lifetime journal/memory bounds fail visibly and retain bounded state.", ["test_queue_payload_and_retained_journal_storage_caps", "test_memory_limits_and_json_safe_errors"], "OS RAM/disk, total fleet capacity, streaming sources and retention require VM qualification."),
    (23, "private data boundary", "Source/memory markers are absent from CLI stdout/stderr, status and actual Office sink.", ["test_private_content_excluded_and_semantic_fixture_not_keyword_gate", "test_complete_owner_journey_across_process_restarts", "test_office_reasons_are_closed_and_typed"], None),
    (24, "durable memory and intents", "Owner CLI journey, SQLite memory/intents and unknown holds survive real process reopen; hard exits before reserve or after zero settlement preserve attempts and complete once with a fresh hold, respecting lease expiry and STOP; nonzero settlements never become free retries; legacy missing holds accept only explicit zero-cost reconciliation without a saved reply; recovered unknown/over-limit decisions cannot dispatch effects.", ["test_complete_owner_journey_across_process_restarts", "test_effect_status_monotonic_receipt_private_and_retries_persist", "test_interrupted_inference_journal_recovers_hold", "test_cli_interrupted_decision_cost_policy_survives_restart", "test_missing_pre_inference_reservation_reopens_with_fresh_hold", "test_cli_hard_exit_before_reserve_recovers_without_phantom_hold", "test_cli_hard_exit_after_zero_settlement_recovers_with_fresh_hold", "test_stale_calling_nonzero_settlement_never_reinterprets", "test_cli_legacy_phantom_reservation_can_be_explicitly_recovered"], None),
    (25, "per DOTS memory/key isolation", "Logical memory namespaces and foreign references are denied; clean profile references differ.", ["test_memory_cross_org_refs_and_other_dot_corrections_denied", "test_hermes_profiles_keep_independent_references_and_existing_files"], "Real OS principals/key denial and secret broker/worker transport are NOT_IMPLEMENTED; no key access was attempted."),
    (26, "owner correction", "Correction revision 2 replaces old retrieval after restart; other DOTS remains unchanged.", ["test_owner_correction_invalidates_old_retrieval_and_keeps_grants", "test_complete_owner_journey_across_process_restarts"], None),
    (27, "memory poison", "Untrusted retrieved poison grants no action, model tools or cross-DOTS writes.", ["test_injection_extra_tools_and_memory_poison_cannot_grant", "test_untrusted_memory_never_broadens_intent_grants"], None),
    (28, "semantic significance without keyword", "Fixture says slipped delivery is relevant and ALARM test noise is ignored; no keyword gate exists.", ["test_private_content_excluded_and_semantic_fixture_not_keyword_gate"], "Fixture opinions test semantic Model plumbing ONLY; actual LLM alarm/noise recall is BLOCKED."),
    (29, "retrieval/API/budget failure", "Retrieval/source/model/budget failures keep visible pending or failed coverage without false green.", ["test_memory_correction_retrieval_failure_and_model_failure_visible", "test_timeout_cost_held_no_automatic_retry_or_effect", "test_budget_pending_survives_restart_and_empty_source", "test_source_malformed_truncated_and_oversize_polls_are_visible"], "Real APIs/broker are not wired or qualified."),
    (30, "hung supervisor independent STOP", "Concurrent hung synthetic supervisor and model processes do not block independent pause CLI; no effect.", ["test_pause_cli_independent_of_hung_model_process", "test_supervisor_plan_budget_and_writer"], "Installed Hermes supervisor behavior/VM concurrency remain BLOCKED; passive synthetic process is not live Hermes."),
    (31, "clean single-writer profile", "Actual profile factories return new homes/fresh memory/no credential or legacy import and one writer.", ["test_clean_profile_plan_and_pinning", "test_hermes_profiles_keep_independent_references_and_existing_files", "test_demo_clean_hermes_plan_and_validation"], "Installed Hermes compatibility, service/systemd parsing and VM deployment remain BLOCKED.")
]


class EvidenceResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.evidence = {}

    def startTest(self, test):
        self.begin = time.monotonic()
        super().startTest(test)

    def record(self, test, status):
        self.evidence[test.id()] = {"status": status, "elapsed_ms": round((time.monotonic() - self.begin) * 1000)}

    def addSuccess(self, test):
        super().addSuccess(test)
        # A failed subTest must not be overwritten by a success.
        if test.id() not in self.evidence:
            self.record(test, "PASS")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.record(test, "FAIL")

    def addError(self, test, err):
        super().addError(test, err)
        self.record(test, "ERROR")

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.record(test, "SKIP")

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self.record(test, "FAIL")


def artifacts():
    paths = [FARM / name for name in ("DESIGN.md", "dots.py", "README.md", "ACCEPTANCE.md", "tests/pilot_acceptance.py")]
    paths += list((FARM / "dots_farm").glob("*.py"))
    paths += list((FARM / "tests").glob("test_*.py"))
    paths += [path for path in (FARM / "deploy").rglob("*") if path.is_file()]
    paths += [path for path in (WORKSPACE / "global/codex/skills/create-dots").rglob("*")
              if path.is_file() and "__pycache__" not in path.parts]
    return {path.relative_to(WORKSPACE).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(set(paths))}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", required=True, help="Explicit absolute retained evidence directory (may be outside the checkout).")
    args = p.parse_args()
    output = Path(args.output)
    if not output.is_absolute():
        p.error("output must be absolute")
    output = output.resolve() / ("run-" + uuid.uuid4().hex)
    output.mkdir(parents=True, exist_ok=False)
    before = artifacts()
    log = io.StringIO()
    loader = unittest.TestLoader()
    suite = loader.discover(str(FARM / "tests"), pattern="test_*.py")
    result = unittest.TextTestRunner(stream=log, verbosity=2, resultclass=EvidenceResult).run(suite)
    matrix = []
    for number, case, expected, methods, blocked in CASES:
        actual = []
        for method in methods:
            matches = [test_id for test_id in result.evidence if test_id.endswith("." + method)]
            if len(matches) != 1:
                actual.append({"method": method, "status": "MISSING_OR_AMBIGUOUS"})
            else:
                actual.append({"test": matches[0], **result.evidence[matches[0]]})
        passed = all(entry["status"] == "PASS" for entry in actual)
        matrix.append({"id": number, "case": case, "role": "owner/controller on synthetic fixtures",
                       "expected": expected, "actual": actual,
                       "local_status": "PASS" if passed else "FAIL",
                       "external_qualification": "BLOCKED" if blocked else "not claimed or required for this local case",
                       "blocked_reason": blocked})
    roots = []
    cli_evidence = []
    for module_name in ("test_store", "test_adapters", "test_engine", "test_cli"):
        module = sys.modules.get(module_name)
        if module is None:
            continue
        for obj in vars(module).values():
            if isinstance(obj, type) and issubclass(obj, unittest.TestCase) and obj.__module__ == module_name:
                root = getattr(obj, "fixture_root", None)
                if root is not None and str(root) not in roots:
                    roots.append(str(root))
                    cli_evidence.extend(str(path) for path in Path(root).rglob("command-evidence.json"))
    after = artifacts()
    passed = result.wasSuccessful() and before == after and all(row["local_status"] == "PASS" for row in matrix)
    report = {
        "readiness": "STAGED_CANDIDATE" if passed else "FAILED_LOCAL_VERIFICATION",
        "independent_acceptance": "PENDING_PARENT_QWEN_REVIEW", "live_activation": "BLOCKED",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "executed_command": [sys.executable, "-B", str(Path(__file__).resolve()), *sys.argv[1:]],
        "test_execution": "unittest discovery in this process; CLI tests run real subprocesses with CREATE_NO_WINDOW on Windows",
        "tests_run": result.testsRun, "test_results": result.evidence,
        "fixture_roots_retained": roots, "cli_command_evidence": cli_evidence,
        "artifact_sha256": after, "artifact_unchanged_during_tests": before == after,
        "matrix": matrix,
        "anti_bureaucracy": {"assessment": "maker evidence for existing independent review",
                              "trigger": "explicit create/configure DOTS only; allow_implicit_invocation=false",
                              "ordinary_session_before": {"reads": 0, "questions": 0, "model_calls": 0},
                              "ordinary_session_after": {"reads": 0, "questions": 0, "model_calls": 0},
                              "new_registry_council_or_hooks": False,
                              "creator_actual_cli_test": "test_cli.CLITests.test_creator_helper_runs_real_create_and_preserves_existing_dot",
                              "creator_other_cwd_installed_test": "test_cli.CLITests.test_creator_absolute_paths_from_other_cwd_and_installed_skill"},
        "knowledge_closeout": "PARENT_OWNED_READ_ONLY_CONTRACTOR", "shared_skill_backup": "NOT_RUN_PARENT_REQUIRED"
    }
    (output / "test-log.txt").write_text(log.getvalue(), encoding="utf-8")
    evidence = output / "evidence.json"
    evidence.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"readiness": report["readiness"], "tests_run": result.testsRun,
                      "local_cases_pass": sum(row["local_status"] == "PASS" for row in matrix),
                      "external_cases_blocked": sum(row["external_qualification"] == "BLOCKED" for row in matrix),
                      "evidence": str(evidence), "fixtures_retained": True}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
