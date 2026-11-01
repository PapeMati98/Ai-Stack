"""Scheduler-owned rebase of retained pre-effect prepared knowledge.

A blind phase replay correctly refuses a retained PREPARED/STARTED checkpoint
whose bound publication target changed under it (another normal publication
moved the preimage). When the current freeze proves that only target,
preimage, and snapshot moved while every source and proposal input hash and
every proposal identity is byte-identical to the retained binding, the
retained conclusive maker output (``decision.json``) is re-rendered against
the current freeze with the existing ``build_candidate`` validation, saved as
a fresh PREPARED checkpoint, and then revalidated by the ORIGINAL full phase
loader. The resumed candidate always needs a fresh independent review; no
maker call, review verdict, or ACCEPT is ever reused.

This module is invoked only from the scheduler hooks in ``maintenance.py``.
The direct curator loader keeps rejecting the changed binding, and the
publisher fingerprint pins stay untouched.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, Optional

PHASE_RESUME_REASON = "phase_resume_binding_invalid"
# old60 retained-interruption code pin; current61 is read from repaired_retry.
OLD60_CODE_VERSION = "6d74ee300e9032e35c27ced6fd2f274e"
REBASE_PHASES = frozenset({
    "PREPARED", "REVIEW_STARTED", "REVIEW2_STARTED", "REPAIR_STARTED",
})
AMBIGUOUS_STATUS = frozenset({
    "UNKNOWN", "UNKNOWN_REQUIRES_RECONCILIATION", "RUNNING", "PUBLISHED",
    "ACCOUNTED", "accounted_elsewhere", "unsafe_source",
})
MAX_REBASE_CANDIDATES = 8


def load_with_prepared_rebase(original_loader: Callable, cfg, paths, state, frozen):
    """Load phase progress, rebasing retained knowledge on a target-only change.

    The ORIGINAL loader is always tried first and stays authoritative. Only an
    exact ``phase_resume_binding_invalid`` rejection is reconsidered, and only
    against a qualified known pre-effect record. The original loader must be
    passed explicitly so the maintenance monkeypatch wrapper never recurses.
    """
    from caretaker.publication import PublicationError

    try:
        return original_loader(cfg, paths, state, frozen)
    except PublicationError as exc:
        if str(exc) != PHASE_RESUME_REASON:
            raise
        original_error = exc
    from caretaker import tick_budget

    try:
        plan = _rebase_plan(cfg, paths, state, frozen)
    except tick_budget.TickBudgetExceeded:
        raise
    except Exception:
        # Any unreadable, malformed, escaping, or ambiguous retained artifact
        # fails closed with the original blind-replay rejection.
        plan = None
    if plan is None:
        raise original_error
    import curate_once as curator
    from caretaker import publication

    state = Path(state)
    pointer_path = curator._phase_pointer(state, cfg.project_slug, paths)
    record = plan["record"]
    run_root = plan["run_root"]
    new_candidate = None
    suffixes = ("",) + tuple("-%d" % n for n in range(2, MAX_REBASE_CANDIDATES + 1))
    for suffix in suffixes:
        candidate = run_root / ("candidate-rebase" + suffix)
        if not candidate.exists():
            new_candidate = candidate
            break
    if new_candidate is None:
        raise original_error
    observed = dt.datetime.now(dt.timezone.utc).isoformat()
    saved = False
    try:
        # Preserve the original binding evidence durably before the pointer is
        # overwritten: a later resume rewrites run_root/result.json and later
        # checkpoints drop prior_interrupted_calls, so the pre-rebase record
        # and its deferral proof live in a chained archive in the run root.
        archive = {
            "schema_version": 1,
            "kind": "prepared_rebase_archive",
            "archived_at": observed,
            "original_record": record,
            "original_result": plan.get("result_doc"),
        }
        archive_path = run_root / "prepared-rebase-archive.json"
        if (archive_path.is_file()
                and not archive_path.is_symlink() and not archive_path.is_junction()):
            try:
                prior_archive = json.loads(archive_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError, UnicodeError):
                prior_archive = None
            if isinstance(prior_archive, dict):
                archive["prior_archive"] = prior_archive
        curator._durable_json(archive_path, archive)
        # The retained decision is untrusted old maker output. The current
        # build re-runs every validation, limit, citation, privacy, and
        # source guard against the CURRENT freeze in a NEW separate candidate
        # directory under the SAME owned run root; old evidence is preserved.
        manifest = publication.build_candidate(
            frozen, plan["decision"], new_candidate, observed,
        )
        curator._save_phase_progress(
            cfg, paths, frozen, run_root, observed, "PREPARED",
            new_candidate, manifest, plan["progress"],
        )
        saved = True
        # The original full loader revalidates the complete current frozen
        # closure, target preimages, pins, accounting, and journals.
        return original_loader(cfg, paths, state, frozen)
    except tick_budget.TickBudgetExceeded:
        # A durably saved rebased PREPARED stays valid for the next tick.
        raise
    except Exception:
        if saved:
            try:
                # Roll back to the original phase record on any validation
                # failure after the checkpoint write (for example a target
                # preimage race); never publish or reuse an ACCEPT.
                curator._durable_json(pointer_path, record)
            except Exception:
                pass
        raise original_error


def _rebase_plan(cfg, paths, state, frozen) -> Optional[Dict[str, Any]]:
    """Prove the retained record is a safe target-only rebase candidate."""
    import curate_once as curator
    from caretaker.audit import accounted_proposal_ids
    from caretaker.publication import safe_file, sha
    from caretaker.repaired_retry import PHASE_RESUME_CODE_VERSION

    state = Path(state)
    pointer_path = curator._phase_pointer(state, cfg.project_slug, paths)
    if not pointer_path.exists():
        return None
    pointer = curator._phase_regular_file(
        state, pointer_path.name, limit=curator._PHASE_CACHE_LIMIT,
    )
    record = json.loads(pointer.read_text(encoding="utf-8"))
    if (not isinstance(record, dict)
            or record.get("schema_version") != 1
            or record.get("phase") not in REBASE_PHASES
            or record.get("code_version") not in {OLD60_CODE_VERSION, PHASE_RESUME_CODE_VERSION}
            or record.get("project") != cfg.project_slug
            or record.get("paths") != sorted(paths)
            or record.get("cache_sha256") != curator._phase_digest(record)):
        return None
    binding = record.get("binding")
    if not isinstance(binding, dict):
        return None
    if curator._normalized_phase_binding(binding) is None:
        return None
    current = curator._phase_binding(frozen)
    # Source and proposal input hashes and proposal identities must be exactly
    # the old binding; only target, preimage, and snapshot may have moved.
    if (binding.get("project") != current.get("project")
            or binding.get("inputs") != current.get("inputs")
            or binding.get("proposals") != current.get("proposals")):
        return None
    # The exception exists only for a genuine target-side move. When target,
    # preimage, and snapshot are all unchanged the original loader owns the
    # record; the helper must not launder any other retained defect.
    if (binding.get("target"), binding.get("preimage_sha256"),
            binding.get("snapshot_sha256")) == (
            current.get("target"), current.get("preimage_sha256"),
            current.get("snapshot_sha256")):
        return None
    progress = record.get("progress")
    if (not isinstance(progress, dict)
            or progress.get("status") in AMBIGUOUS_STATUS):
        return None
    run_root = safe_file(state, record.get("run_root"))
    if not run_root.is_dir() or run_root.is_symlink() or run_root.is_junction():
        return None
    if record["phase"] == "PREPARED":
        candidate_name = record.get("candidate")
        manifest_name = record.get("manifest")
        manifest_sha = record.get("manifest_sha256")
        pins = record.get("pins")
        # The retained PREPARED record must satisfy the same review-free
        # conditions the original loader enforces. The helper never zeroes or
        # rewrites a dirty field to make the record fit; it refuses instead.
        if (not isinstance(candidate_name, str) or not isinstance(manifest_name, str)
                or not isinstance(manifest_sha, str)
                or re.fullmatch(r"[a-f0-9]{64}", manifest_sha) is None
                or not isinstance(pins, dict) or not pins or len(pins) > 128
                or progress.get("phase") != "PREPARED"
                or type(progress.get("maker_calls")) is not int
                or progress.get("maker_calls") != 1
                or type(progress.get("review_calls")) is not int
                or progress.get("review_calls") != 0
                or progress.get("maker_format_repairs", 0) != 0
                or progress.get("reviewer_format_repairs", {}) != {}
                or progress.get("review_rounds", []) != []
                or progress.get("assessment") is not None
                or progress.get("review_response")
                or progress.get("fleet_review_path")):
            return None
        old_candidate = safe_file(state, candidate_name)
        if (not old_candidate.is_dir()
                or old_candidate.is_symlink() or old_candidate.is_junction()):
            return None
        old_manifest = curator._phase_regular_file(state, manifest_name)
        if (old_manifest.name != "publication.json"
                or old_manifest.parent != old_candidate
                or sha(old_manifest.read_bytes()) != manifest_sha
                or curator._phase_pins(state, old_candidate) != pins):
            return None
        # The PREPARED branch needs the same persisted pre-effect proof the
        # STARTED recovery requires: an exact DEFERRED_PREPARED /
        # tick_time_budget_exhausted result bound to this run, candidate,
        # manifest, snapshot, target, and proposal set, with no effect proof.
        # A native timeout, an UNKNOWN outcome, or any effect refuses.
        result_doc = _deferred_prepared_result(
            run_root, old_candidate, manifest_sha, binding,
        )
        if result_doc is None:
            return None
    else:
        # STARTED records carry no candidate pin set; the existing recovery
        # validation must prove the persisted exact DEFERRED_PREPARED /
        # tick_time_budget_exhausted result, old manifest/snapshot/proposal
        # set/target/run/candidate, and all payload and ledger hashes. A
        # native-timeout, rejected, unknown, or effected record yields None.
        recovered = curator._recover_deferred_prepared(record, cfg, paths, state)
        if recovered is None:
            return None
        old_candidate = safe_file(state, recovered["candidate"])
        old_manifest = curator._phase_regular_file(state, recovered["manifest"])
        try:
            result_doc = json.loads(curator._phase_regular_file(
                run_root, "result.json").read_text(encoding="utf-8"))
        except Exception:
            result_doc = None
    doc = json.loads(old_manifest.read_text(encoding="utf-8"))
    if (not isinstance(doc, dict)
            or not curator._manifest_matches_frozen(doc, binding)):
        return None
    outputs = doc.get("outputs")
    ledger_rel = doc.get("ledger_path")
    if not isinstance(outputs, list) or not outputs or not isinstance(ledger_rel, str):
        return None
    for item in outputs:
        if not isinstance(item, dict):
            return None
        payload = curator._phase_regular_file(old_candidate, item.get("payload_path"))
        if sha(payload.read_bytes()) != item.get("sha256"):
            return None
    ledger = curator._phase_regular_file(old_candidate, ledger_rel)
    if sha(ledger.read_bytes()) != doc.get("ledger_sha256"):
        return None
    vault = Path(cfg.hub.vault)
    if set(doc.get("proposal_ids") or []) & accounted_proposal_ids(vault):
        return None
    txdir = old_candidate / ".curation-transactions"
    if txdir.exists():
        for journal_path in txdir.glob("tx-*.json"):
            if journal_path.name.endswith(".manifest.json"):
                continue
            journal = json.loads(safe_file(
                old_candidate, journal_path.relative_to(old_candidate).as_posix(),
            ).read_text(encoding="utf-8"))
            if not isinstance(journal, dict) or journal.get("status") != "rolled_back":
                return None
    decision_file = curator._phase_regular_file(old_candidate, "decision.json")
    decision = json.loads(decision_file.read_text(encoding="utf-8"))
    if not isinstance(decision, dict) or set(decision) != {"body", "summary", "dispositions"}:
        return None
    # New review cycle over the original maker bytes; prior call totals stay
    # explicit and the old independent review is never reused. A chained
    # rebase keeps the original interrupted totals instead of resetting them.
    prior = progress.get("prior_interrupted_calls")
    if (not isinstance(prior, dict)
            or type(prior.get("maker")) is not int
            or type(prior.get("reviewer")) is not int):
        prior = {
            "maker": progress.get("maker_calls"),
            "reviewer": progress.get("review_calls"),
        }
    new_progress = {
        key: progress[key]
        for key in ("maker_provider", "maker_model", "maker_route", "observed")
        if key in progress
    }
    new_progress.update(
        phase="PREPARED", maker_calls=1, review_calls=0,
        maker_format_repairs=0, reviewer_format_repairs={},
        review_rounds=[], prior_interrupted_calls=prior,
    )
    return {
        "record": record, "run_root": run_root, "result_doc": result_doc,
        "decision": decision, "progress": new_progress,
    }


def _deferred_prepared_result(run_root, candidate, manifest_sha, binding):
    """Persisted exact pre-effect deferral bound to this run and binding."""
    import curate_once as curator

    try:
        result_file = curator._phase_regular_file(run_root, "result.json")
        result = json.loads(result_file.read_text(encoding="utf-8"))
    except Exception:
        return None
    if (not isinstance(result, dict)
            or result.get("status") != "DEFERRED_PREPARED"
            or result.get("reason") != "tick_time_budget_exhausted"
            or result.get("phase") != "PREPARED"
            or result.get("run_path") != str(run_root)
            or result.get("candidate_path") != str(candidate)
            or result.get("manifest_sha256") != manifest_sha
            or result.get("snapshot_sha256") != binding.get("snapshot_sha256")
            or result.get("target") != binding.get("target")
            or sorted(result.get("proposal_ids") or [])
                != sorted(item["id"] for item in binding["proposals"])
            or result.get("publication") is not None
            or result.get("checkpoint") is not None):
        return None
    return result
