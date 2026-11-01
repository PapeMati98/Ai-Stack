"""Read-only proof for the one qualified terminal generation recovery."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


SOURCE_REASON = "phase_resume_terminal_no_replay"
RETRY_REASON = "terminal_generation_source_retry"


def canonical_proposal_identities(paths: Any, proposal_ids: Any,
                                  hashes: Any) -> Optional[list[tuple[str, str, str]]]:
    """Canonicalize aligned identity triples by path without detaching fields."""
    if (not isinstance(paths, list) or not paths
            or not isinstance(proposal_ids, list) or not isinstance(hashes, list)
            or len(paths) != len(proposal_ids) or len(paths) != len(hashes)):
        return None
    identities = list(zip(proposal_ids, paths, hashes))
    if (any(not isinstance(value, str) or not value for identity in identities
            for value in identity)
            or len({item[0] for item in identities}) != len(identities)
            or len({item[1] for item in identities}) != len(identities)
            or len(set(identities)) != len(identities)):
        return None
    return sorted(identities, key=lambda item: item[1])


def helper_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _parse_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _terminal_report(state_root: Path, *, batch: str, project: str,
                     proposal_ids: list[str], phase_mtime: float) -> Optional[dict]:
    runs = state_root / "runs"
    if (not runs.is_dir() or runs.is_symlink() or runs.is_junction()):
        return None
    matches = []
    try:
        for path in runs.glob("*.json"):
            if path.is_symlink() or path.is_junction() or path.stat().st_size > 2 * 1024 * 1024:
                continue
            report = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(report, dict) or report.get("running") is not False
                    or report.get("status") != "DEFERRED_BUDGET"):
                continue
            started = _parse_time(report.get("started_at"))
            completed = _parse_time(report.get("completed_at"))
            phase_time = datetime.fromtimestamp(phase_mtime, timezone.utc)
            if started is None or completed is None or not started <= phase_time <= completed:
                continue
            batches = report.get("completed_batches")
            if not isinstance(batches, list):
                continue
            for item in batches:
                if (not isinstance(item, dict) or item.get("batch") != batch
                        or item.get("project") != project):
                    continue
                reported_ids = item.get("proposal_ids")
                paths = item.get("paths")
                if (item.get("status") != "BACKOFF_DEFERRED"
                        or item.get("reason") != "process_timeout"
                        or item.get("lane") != "legacy"
                        or not isinstance(reported_ids, list)
                        or len(reported_ids) != len(proposal_ids)
                        or len(set(reported_ids)) != len(reported_ids)
                        or set(reported_ids) != set(proposal_ids)
                        or paths not in (None, [])):
                    continue
                if item.get("publication") is not None or item.get("checkpoint") is not None:
                    continue
                matches.append({"path": path, "report": report, "batch": item})
    except (OSError, UnicodeError, ValueError, TypeError):
        return None
    return matches[0] if len(matches) == 1 else None


def terminal_generation_plan(cfg, paths: list[str], frozen: dict,
                             old_result: dict) -> Optional[dict]:
    """Validate the exact old consumer report and immutable initial candidate.

    The old repair model output is deliberately not read or replayed. The only
    reusable assessment is the earlier independent REPAIR bound to the initial
    candidate manifest; the normal curator must make a new repair and obtain a
    fresh independent review.
    """
    import curate_once
    from caretaker.publication import PublicationError, safe_file, sha
    from caretaker.pubstate import code_version

    try:
        project = cfg.project_slug
        ordered_paths = sorted(paths)
        if (not ordered_paths or paths != ordered_paths or not isinstance(old_result, dict)
                or old_result.get("status") != "QUARANTINED"
                or old_result.get("reason") != SOURCE_REASON
                or old_result.get("original_reason") != SOURCE_REASON
                or old_result.get("project") != project
                or old_result.get("candidate_path") is not None
                or any(old_result.get(key) not in (None, "", {}, []) for key in (
                    "publication", "checkpoint", "event_id", "manifest",
                    "manifest_sha256", "ledger_path", "publication_event", "transaction",
                ))):
            return None
        stored_identities = canonical_proposal_identities(
            old_result.get("paths"), old_result.get("proposal_ids"),
            old_result.get("hashes"),
        )
        if (stored_identities is None
                or [item[1] for item in stored_identities] != ordered_paths):
            return None
        proposals = frozen.get("proposals")
        if not isinstance(proposals, list) or not proposals:
            return None
        identities = canonical_proposal_identities(
            [item.get("path") for item in proposals if isinstance(item, dict)],
            [item.get("id") for item in proposals if isinstance(item, dict)],
            [item.get("sha256") for item in proposals if isinstance(item, dict)],
        )
        if (identities is None or len(identities) != len(paths)
                or identities != stored_identities):
            return None
        batch = old_result.get("batch")
        cycle_id = old_result.get("cycle_id")
        if (not isinstance(batch, str) or not re.fullmatch(r"[a-f0-9]{64}", batch)
                or not isinstance(cycle_id, str) or not cycle_id.startswith("audit-new-")):
            return None

        state = Path(cfg.state_dir)
        phase_state = state / "curation"
        pointer = curate_once._phase_pointer(phase_state, project, ordered_paths)
        pointer = curate_once._phase_regular_file(
            phase_state, pointer.name, limit=curate_once._PHASE_CACHE_LIMIT,
        )
        current_pointer = pointer
        current_record = json.loads(pointer.read_text(encoding="utf-8"))
        current_progress = (current_record.get("progress")
                            if isinstance(current_record, dict) else None)
        continuation = isinstance(current_progress, dict) and (
            current_progress.get("terminal_generation_retry") is True
        )
        phase_mtime = pointer.stat().st_mtime
        if continuation:
            current_phase = current_record.get("phase")
            initial_maker_calls = current_progress.get(
                "terminal_generation_initial_maker_calls")
            initial_review_calls = current_progress.get(
                "terminal_generation_initial_review_calls")
            if (current_phase not in {"REPAIR_REQUIRED", "REPAIR_PREPARED", "PREPARED"}
                    or current_record.get("schema_version") != 1
                    or current_record.get("project") != project
                    or current_record.get("paths") != ordered_paths
                    or current_record.get("code_version") != code_version()
                    or current_record.get("cache_sha256") != curate_once._phase_digest(current_record)
                    or not curate_once._phase_bindings_equal(
                        current_record.get("binding"), curate_once._phase_binding(frozen))
                    or type(initial_maker_calls) is not int
                    or type(initial_review_calls) is not int
                    or current_progress.get("terminal_generation_source_batch")
                    != old_result.get("batch")
                    or not isinstance(current_progress.get("assessment"), dict)
                    or current_progress.get("terminal_generation_prior_manifest_sha256")
                    != current_progress.get("assessment", {}).get("manifest_sha256")
                    or current_progress.get("terminal_generation_phase_mtime") is None):
                return None
            current_manifest_sha = current_record.get("manifest_sha256")
            assessment = current_progress.get("assessment")
            review_response = current_progress.get("review_response")
            if not isinstance(assessment, dict) or current_progress.get("final") is not None:
                return None
            if current_phase == "REPAIR_REQUIRED":
                if (assessment.get("verdict") != "REPAIR"
                        or assessment.get("manifest_sha256")
                        != current_progress.get("terminal_generation_prior_manifest_sha256")
                        or review_response is not None
                        or current_progress.get("maker_response") is not None
                        or current_progress.get("maker_calls") != initial_maker_calls
                        or current_progress.get("review_calls") != initial_review_calls
                        or current_manifest_sha
                        != current_progress.get("terminal_generation_prior_manifest_sha256")):
                    return None
            elif current_phase == "REPAIR_PREPARED":
                if (assessment.get("verdict") != "REPAIR"
                        or assessment.get("manifest_sha256")
                        != current_progress.get("terminal_generation_prior_manifest_sha256")
                        or review_response is not None
                        or current_progress.get("maker_response") is not None
                        or current_progress.get("maker_calls") != initial_maker_calls + 1
                        or current_progress.get("review_calls") != initial_review_calls
                        or not isinstance(current_manifest_sha, str)
                        or not re.fullmatch(r"[a-f0-9]{64}", current_manifest_sha)
                        or current_manifest_sha
                        == current_progress.get("terminal_generation_prior_manifest_sha256")):
                    return None
            else:
                if (assessment.get("verdict") != "PASS"
                        or assessment.get("manifest_sha256") != current_manifest_sha
                        or not isinstance(review_response, dict)
                        or current_progress.get("maker_calls") != initial_maker_calls + 1
                        or type(current_progress.get("review_calls")) is not int
                        or current_progress.get("review_calls") <= initial_review_calls
                        or not isinstance(current_manifest_sha, str)
                        or not re.fullmatch(r"[a-f0-9]{64}", current_manifest_sha)):
                    return None
            archive_rel = current_progress.get("terminal_generation_archive_path")
            archive = safe_file(phase_state, archive_rel)
            if (not archive.is_dir() or archive.is_symlink() or archive.is_junction()
                    or not archive.resolve().is_relative_to(phase_state.resolve())):
                return None
            archived_pointer = curate_once._phase_regular_file(
                phase_state, (archive / "phase.json").relative_to(phase_state).as_posix(),
                limit=curate_once._PHASE_CACHE_LIMIT,
            )
            expected_pointer_sha = current_progress.get(
                "terminal_generation_old_pointer_sha256")
            if (not isinstance(expected_pointer_sha, str)
                    or hashlib.sha256(archived_pointer.read_bytes()).hexdigest()
                    != expected_pointer_sha):
                return None
            pointer = archived_pointer
            phase_mtime = current_progress.get("terminal_generation_phase_mtime")
            if type(phase_mtime) not in (int, float) or phase_mtime <= 0:
                return None
            record = json.loads(pointer.read_text(encoding="utf-8"))
        else:
            record = current_record
        progress = record.get("progress") if isinstance(record, dict) else None
        if (not isinstance(record, dict) or record.get("schema_version") != 1
                or record.get("phase") != "REPAIR_STARTED"
                or record.get("project") != project or record.get("paths") != ordered_paths
                or record.get("code_version") != code_version()
                or record.get("cache_sha256") != curate_once._phase_digest(record)
                or not curate_once._phase_bindings_equal(
                    record.get("binding"), curate_once._phase_binding(frozen))
                or record.get("candidate") is not None or record.get("manifest") is not None
                or record.get("manifest_sha256") is not None or record.get("pins") != {}
                or not isinstance(progress, dict)
                or progress.get("phase") != "REPAIR_STARTED"
                or progress.get("status") in {
                    "UNKNOWN", "UNKNOWN_REQUIRES_RECONCILIATION", "RUNNING",
                    "PUBLISHED", "ACCOUNTED", "accounted_elsewhere", "unsafe_source",
                }
                or type(progress.get("maker_calls")) is not int
                or progress["maker_calls"] < 1
                or type(progress.get("review_calls")) is not int
                or progress["review_calls"] < 1
                or progress.get("repair_attempt") != 1
                or any(progress.get(name) not in (None, "", {}, []) for name in (
                    "maker_response", "review_response", "final",
                ))):
            return None
        assessment = progress.get("assessment")
        rounds = progress.get("review_rounds")
        if (not isinstance(assessment, dict) or assessment.get("verdict") != "REPAIR"
                or not isinstance(assessment.get("findings"), list)
                or not isinstance(assessment.get("manifest_sha256"), str)
                or not isinstance(rounds, list) or not rounds
                or not isinstance(rounds[-1], dict) or rounds[-1].get("verdict") != "REPAIR"):
            return None
        from caretaker.publication import normalize_review_finding, WEIGHT_BLOCKING
        findings = [normalize_review_finding(item) for item in assessment["findings"]]
        blocking = [item for item in findings if item.get("weight") == WEIGHT_BLOCKING]
        if (not blocking or not {item.get("class") for item in blocking}.issubset(
                {"source_misattribution", "unanchored_content"})
                or any(item.get("class") not in {
                    "source_misattribution", "unanchored_content", "incomplete_coverage",
                } for item in findings)):
            return None

        run_root = safe_file(phase_state, record.get("run_root"))
        if (not run_root.is_dir() or run_root.is_symlink() or run_root.is_junction()
                or not run_root.resolve().is_relative_to(phase_state.resolve())):
            return None
        result_path = curate_once._phase_regular_file(
            phase_state, (run_root / "result.json").relative_to(phase_state).as_posix(),
        )
        result = json.loads(result_path.read_text(encoding="utf-8"))
        candidate = safe_file(phase_state, (run_root / "candidate").relative_to(phase_state).as_posix())
        if (not isinstance(result, dict) or not candidate.is_dir()
                or candidate.is_symlink() or candidate.is_junction()
                or result.get("status") != "DEFERRED_PREPARED"
                or result.get("reason") != "tick_time_budget_exhausted"
                or Path(result.get("run_path") or "").resolve() != run_root.resolve()
                or Path(result.get("candidate_path") or "").resolve() != candidate.resolve()
                or result.get("target") != frozen.get("target")
                or result.get("snapshot_sha256") != frozen.get("snapshot_sha256")
                or not isinstance(result.get("proposal_ids"), list)
                or len(result.get("proposal_ids")) != len(identities)
                or len(set(result.get("proposal_ids"))) != len(identities)
                or set(result.get("proposal_ids")) != {item[0] for item in identities}
                or any(result.get(key) not in (None, "", {}, []) for key in (
                    "publication", "checkpoint", "event_id", "manifest",
                    "manifest_sha256_effect", "ledger_path", "publication_event", "transaction",
                ))):
            return None
        result_paths = result.get("paths")
        result_hashes = result.get("hashes")
        if result_paths is not None or result_hashes is not None:
            result_identities = canonical_proposal_identities(
                result_paths, result.get("proposal_ids"), result_hashes,
            )
            if result_identities != identities:
                return None
        manifest = curate_once._phase_regular_file(candidate, "publication.json")
        manifest_sha = sha(manifest.read_bytes())
        if (continuation and (archive.parent.resolve() != run_root.resolve()
                or archive.name != "terminal-generation-archive-" + manifest_sha[:16])):
            return None
        if (result.get("manifest_sha256") != manifest_sha
                or assessment.get("manifest_sha256") != manifest_sha):
            return None
        manifest_doc = json.loads(manifest.read_text(encoding="utf-8"))
        if (not isinstance(manifest_doc, dict)
                or manifest_doc.get("vault_path") != str(Path(cfg.hub.vault))
                or not curate_once._manifest_matches_frozen(manifest_doc, frozen)
                or manifest_doc.get("ledger_path") != "ledger.json"):
            return None
        pins = curate_once._phase_pins(phase_state, candidate)
        ledger = curate_once._phase_regular_file(candidate, "ledger.json")
        if sha(ledger.read_bytes()) != manifest_doc.get("ledger_sha256"):
            return None
        outputs = manifest_doc.get("outputs")
        if not isinstance(outputs, list) or not outputs:
            return None
        for output in outputs:
            if not isinstance(output, dict):
                return None
            payload = curate_once._phase_regular_file(candidate, output.get("payload_path"))
            if sha(payload.read_bytes()) != output.get("sha256"):
                return None
            target_path = safe_file(Path(cfg.hub.vault), output.get("path"))
            target_sha = sha(target_path.read_bytes()) if target_path.is_file() else None
            if target_sha != output.get("preimage_sha256"):
                return None
        txdir = candidate / ".curation-transactions"
        if txdir.exists():
            if txdir.is_symlink() or txdir.is_junction():
                return None
            for journal_path in txdir.glob("tx-*.json"):
                if journal_path.name.endswith(".manifest.json"):
                    continue
                journal = json.loads(curate_once._phase_regular_file(
                    candidate, journal_path.relative_to(candidate).as_posix(),
                ).read_text(encoding="utf-8"))
                if not isinstance(journal, dict) or journal.get("status") != "rolled_back":
                    return None

        state_root_value = (cfg.raw or {}).get("maintenance_state_dir")
        if not isinstance(state_root_value, str) or not state_root_value:
            return None
        state_root = Path(state_root_value)
        if state_root.is_symlink() or state_root.is_junction():
            return None
        terminal = _terminal_report(
            state_root, batch=batch, project=project,
            proposal_ids=[item[0] for item in identities], phase_mtime=phase_mtime,
        )
        if terminal is None:
            return None
        if (continuation and current_progress.get("terminal_generation_report_sha256")
                != hashlib.sha256(terminal["path"].read_bytes()).hexdigest()):
            return None
        from caretaker.draft_summary import _prior_inventory_body_proof
        prior = _prior_inventory_body_proof(Path(cfg.hub.vault), state, frozen)
        if prior is None:
            return None
        if continuation:
            archived_result = archive / "result.json"
            archived_pins = archive / "candidate-pins.json"
            archived_report = archive / "consumer-report.json"
            if (not archived_result.is_file() or archived_result.is_symlink()
                    or not archived_pins.is_file() or archived_pins.is_symlink()
                    or not archived_report.is_file() or archived_report.is_symlink()
                    or archived_report.read_bytes() != terminal["path"].read_bytes()
                    or archived_result.read_bytes() != result_path.read_bytes()
                    or json.loads(archived_pins.read_text(encoding="utf-8")) != pins):
                return None
            current_candidate = safe_file(phase_state, current_record.get("candidate"))
            current_manifest = curate_once._phase_regular_file(
                current_candidate, "publication.json",
            )
            current_manifest_doc = json.loads(current_manifest.read_text(encoding="utf-8"))
            if (hashlib.sha256(current_manifest.read_bytes()).hexdigest()
                    != current_record.get("manifest_sha256")
                    or current_record.get("manifest")
                    != current_manifest.relative_to(phase_state).as_posix()
                    or not curate_once._manifest_matches_frozen(current_manifest_doc, frozen)
                    or current_record.get("pins") != curate_once._phase_pins(
                        phase_state, current_candidate)):
                return None
            current_run_root = safe_file(phase_state, current_record.get("run_root"))
            if (not current_run_root.is_dir() or current_run_root.is_symlink()
                    or current_run_root.is_junction()
                    or not current_run_root.resolve().is_relative_to(phase_state.resolve())):
                return None
            current_result_path = current_run_root / "result.json"
            if current_result_path.exists():
                current_result_path = curate_once._phase_regular_file(
                    phase_state,
                    current_result_path.relative_to(phase_state).as_posix(),
                )
                current_result = json.loads(current_result_path.read_text(encoding="utf-8"))
                from caretaker.repaired_retry import NO_INDEPENDENT_REVIEWER_PHASE_REASON
                no_effect_fields = (
                    "publication", "checkpoint", "event_id", "manifest_effect",
                    "manifest_sha256_effect", "ledger_path", "publication_event",
                    "transaction",
                )
                if (not isinstance(current_result, dict)
                        or current_result.get("status") not in {"PENDING", "DEFERRED_PREPARED"}
                        or current_result.get("project") != project
                        or Path(current_result.get("candidate_path") or "").resolve()
                        != current_candidate.resolve()
                        or Path(current_result.get("run_path") or "").resolve()
                        != current_run_root.resolve()
                        or any(current_result.get(key) not in (None, "", {}, [])
                               for key in no_effect_fields)):
                    return None
                current_ids = current_result.get("proposal_ids")
                current_paths = current_result.get("paths")
                current_hashes = current_result.get("hashes")
                if any(value is not None for value in (
                        current_ids, current_paths, current_hashes)):
                    if all(isinstance(value, list) for value in (
                            current_ids, current_paths, current_hashes)):
                        current_identities = canonical_proposal_identities(
                            current_paths, current_ids, current_hashes,
                        )
                        if current_identities != identities:
                            return None
                    else:
                        expected_fields = (
                            (current_ids, {item[0] for item in identities}),
                            (current_paths, set(ordered_paths)),
                            (current_hashes, {item[2] for item in identities}),
                        )
                        for value, expected in expected_fields:
                            if value is not None and (
                                    not isinstance(value, list) or len(value) != len(expected)
                                    or len(set(value)) != len(value) or set(value) != expected):
                                return None
                if (current_phase == "REPAIR_PREPARED"
                        and current_result.get("reason") not in {
                            "no_independent_reviewer", NO_INDEPENDENT_REVIEWER_PHASE_REASON,
                        }):
                    return None
            for output in current_manifest_doc.get("outputs", []):
                if not isinstance(output, dict):
                    return None
                target_path = safe_file(Path(cfg.hub.vault), output.get("path"))
                target_sha = sha(target_path.read_bytes()) if target_path.is_file() else None
                if target_sha != output.get("preimage_sha256"):
                    return None
        return {
            "pointer": pointer, "record": record, "progress": progress,
            "phase_state": phase_state, "run_root": run_root,
            "candidate": candidate, "manifest": manifest,
            "manifest_sha256": manifest_sha, "pins": pins,
            "assessment": assessment, "prior_inventory": prior,
            "prior_inventory_body": prior["body"],
            "old_result_path": result_path,
            "terminal_report_path": terminal["path"],
            "terminal_report_sha256": hashlib.sha256(
                terminal["path"].read_bytes()).hexdigest(),
            "phase_mtime": phase_mtime,
            "cycle_id": cycle_id, "batch": batch,
            "continuation": continuation,
            "current_pointer": current_pointer if continuation else None,
            "current_record": current_record if continuation else None,
            "current_manifest_sha256": (
                current_record.get("manifest_sha256") if continuation else None
            ),
            "frozen": frozen,
        }
    except (OSError, UnicodeError, ValueError, TypeError, KeyError,
            RuntimeError, PublicationError, ImportError):
        return None


def prepare_terminal_generation_resume(plan: dict, cfg, paths: list[str], frozen: dict,
                                       save_phase) -> Optional[dict]:
    """Archive the terminal pointer and resume from the retained initial REPAIR proof."""
    import uuid
    import curate_once
    from caretaker.publication import PublicationError, json_bytes

    try:
        record = plan["record"]
        phase_state = Path(plan["phase_state"])
        old_run = Path(plan["run_root"])
        archive = old_run / ("terminal-generation-archive-" + plan["manifest_sha256"][:16])
        evidence = [
            ("phase.json", Path(plan["pointer"]).read_bytes()),
            ("result.json", Path(plan["old_result_path"]).read_bytes()),
            ("candidate-pins.json", json_bytes(plan["pins"])),
            ("consumer-report.json", Path(plan["terminal_report_path"]).read_bytes()),
        ]
        if archive.exists():
            if archive.is_symlink() or archive.is_junction() or not archive.is_dir():
                return None
        else:
            archive.mkdir()
        for name, data in evidence:
            destination = archive / name
            if destination.exists():
                if destination.is_symlink() or destination.read_bytes() != data:
                    return None
            else:
                destination.write_bytes(data)

        run_root = phase_state / ("terminal-generation-retry-" + uuid.uuid4().hex[:16])
        run_root.mkdir()
        old_progress = plan["progress"]
        progress = dict(old_progress)
        for key in ("maker_response", "review_response", "fleet_review_path", "final"):
            progress.pop(key, None)
        progress.update(
            phase="REPAIR_REQUIRED",
            maker_calls=old_progress["maker_calls"],
            review_calls=old_progress["review_calls"],
            assessment=plan["assessment"],
            review_rounds=list(old_progress.get("review_rounds") or []),
            maker_format_repairs=old_progress.get("maker_format_repairs", 0),
            reviewer_format_repairs=dict(old_progress.get("reviewer_format_repairs") or {}),
            repair_attempt=0,
            terminal_generation_retry=True,
            terminal_generation_source_batch=plan["batch"],
            terminal_generation_report_sha256=plan["terminal_report_sha256"],
            terminal_generation_prior_manifest_sha256=plan["manifest_sha256"],
            terminal_generation_initial_maker_calls=old_progress["maker_calls"],
            terminal_generation_initial_review_calls=old_progress["review_calls"],
            terminal_generation_archive_path=archive.relative_to(phase_state).as_posix(),
            terminal_generation_old_pointer_sha256=hashlib.sha256(
                Path(plan["pointer"]).read_bytes()).hexdigest(),
            terminal_generation_phase_mtime=plan.get("phase_mtime"),
            terminal_generation_prior_repair_attempts=old_progress.get("repair_attempt", 1),
        )
        save_phase(
            cfg, paths, frozen, run_root, record.get("observed"),
            "REPAIR_REQUIRED", plan["candidate"], plan["manifest"], progress,
        )
        return {
            "phase": "REPAIR_REQUIRED", "run_root": run_root,
            "candidate": plan["candidate"], "manifest": plan["manifest"],
            "manifest_sha256": plan["manifest_sha256"],
            "archive": archive,
        }
    except (OSError, UnicodeError, ValueError, TypeError, KeyError,
            RuntimeError, PublicationError):
        return None
