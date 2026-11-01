"""Bounded repair for summaries on provenance-attested caretaker drafts."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Optional


def helper_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def summary_retry_lane_matches(lane: Any, cycle_id: Any) -> bool:
    """Bind summary-retry markers to their actual legacy or new audit lane."""
    if not isinstance(cycle_id, str):
        return False
    if lane == "new":
        return re.fullmatch(r"audit-new-[A-Za-z0-9-]{1,80}", cycle_id) is not None
    if lane == "legacy":
        return re.fullmatch(r"audit-(?!new-)[A-Za-z0-9-]{1,80}", cycle_id) is not None
    return False


def corrected_draft_decision(frozen: dict, decision: dict, *,
                              require_change: bool = False) -> Optional[dict]:
    """Use the validated synthesis summary only for a genuinely owned draft."""
    from caretaker.filter import contains_secret
    from caretaker.privacy import outbound_text_safe
    from caretaker.publication import (
        PublicationError, _is_caretaker_draft, _validate_publication_decision, fm,
    )

    if not isinstance(frozen, dict) or not isinstance(decision, dict):
        return None
    prior = frozen.get("before")
    target = frozen.get("target")
    vault = frozen.get("vault_path")
    state_dir = frozen.get("attestation_state_dir")
    preimage = frozen.get("preimage_sha256")
    project = frozen.get("project_slug")
    if (not isinstance(prior, str) or not prior
            or not isinstance(target, str) or not isinstance(vault, str)
            or not isinstance(state_dir, str) or not isinstance(preimage, str)
            or fm(prior, "status") != "draft"):
        return None
    if not _is_caretaker_draft(
            prior, target, vault=Path(vault), state_dir=Path(state_dir),
            page_sha256=preimage, expected_project=project):
        return None
    try:
        _body, summary, _dispositions, _proposal_ids = _validate_publication_decision(
            frozen, decision,
        )
    except (PublicationError, OSError, ValueError, TypeError, KeyError):
        return None
    if (not outbound_text_safe(summary) or contains_secret(summary)
            or (require_change and summary.strip() == (fm(prior, "summary") or "").strip())):
        return None
    updated = dict(decision)
    updated["summary"] = summary
    return updated


def patch_draft_summary_candidate(frozen: dict, decision: dict,
                                  candidate: Path, manifest_path: Path) -> bool:
    """Replace only the output summary line for an attested prior draft."""
    import json
    from caretaker.publication import fm, json_bytes, safe_file, sha

    updated = corrected_draft_decision(frozen, decision, require_change=True)
    if updated is None:
        return False
    try:
        manifest_path = Path(manifest_path)
        candidate = Path(candidate)
        if (manifest_path.name != "publication.json"
                or manifest_path.parent.resolve() != candidate.resolve()):
            return False
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        outputs = manifest.get("outputs") if isinstance(manifest, dict) else None
        target = frozen.get("target")
        matches = [item for item in outputs or []
                   if isinstance(item, dict) and item.get("path") == target]
        if len(matches) != 1:
            return False
        output = matches[0]
        payload = safe_file(candidate, output.get("payload_path"))
        text = payload.read_text(encoding="utf-8")
        prior = frozen.get("before")
        old_summary = fm(prior, "summary")
        if (fm(text, "summary") != old_summary
                or not isinstance(old_summary, str)):
            return False
        header_end = text.find("\n---\n", 4)
        if header_end < 0:
            return False
        header = text[:header_end]
        line = re.compile(r"(?m)^summary:[ \t]*.*$")
        if len(line.findall(header)) != 1:
            return False
        replacement = "summary: " + json.dumps(updated["summary"], ensure_ascii=False)
        new_header = line.sub(lambda _match: replacement, header, count=1)
        new_text = new_header + text[header_end:]
        payload.write_text(new_text, encoding="utf-8", newline="\n")
        output["sha256"] = sha(payload.read_bytes())
        manifest["summary"] = updated["summary"]
        manifest_path.write_bytes(json_bytes(manifest))
        return True
    except (OSError, UnicodeError, ValueError, TypeError, KeyError):
        return False


def prepared_summary_repair_plan(cfg, paths: list[str], frozen: dict,
                                 loaded: dict) -> Optional[dict]:
    """Prepare only a validated, unreviewed prior draft candidate for re-render."""
    import json
    import curate_once
    from caretaker.publication import fm, safe_file

    try:
        phase = loaded.get("phase") if isinstance(loaded, dict) else None
        if phase not in {"PREPARED", "REPAIR_PREPARED"}:
            return None
        phase_state = Path(cfg.state_dir) / "curation"
        pointer = curate_once._phase_pointer(phase_state, cfg.project_slug, paths)
        pointer = curate_once._phase_regular_file(
            phase_state, pointer.name, limit=curate_once._PHASE_CACHE_LIMIT,
        )
        record = json.loads(pointer.read_text(encoding="utf-8"))
        progress = record.get("progress") if isinstance(record, dict) else None
        if (not isinstance(record, dict) or record.get("phase") != phase
                or not isinstance(progress, dict)
                or record.get("cache_sha256") != curate_once._phase_digest(record)):
            return None
        candidate = safe_file(phase_state, record.get("candidate"))
        manifest = curate_once._phase_regular_file(phase_state, record.get("manifest"))
        decision_path = curate_once._phase_regular_file(candidate, "decision.json")
        decision = json.loads(decision_path.read_text(encoding="utf-8"))
        updated = corrected_draft_decision(frozen, decision, require_change=True)
        if updated is None:
            return None
        manifest_doc = json.loads(manifest.read_text(encoding="utf-8"))
        outputs = manifest_doc.get("outputs") if isinstance(manifest_doc, dict) else None
        target = frozen.get("target")
        output = next((item for item in outputs or []
                       if isinstance(item, dict) and item.get("path") == target), None)
        if output is None:
            return None
        payload = safe_file(candidate, output.get("payload_path"))
        if fm(payload.read_text(encoding="utf-8"), "summary") != fm(frozen["before"], "summary"):
            return None
        return {
            "pointer": pointer, "record": record, "progress": progress,
            "phase_state": phase_state,
            "run_root": safe_file(phase_state, record.get("run_root")),
            "candidate": candidate, "manifest": manifest,
            "decision": updated,
            "old_result_path": safe_file(
                phase_state, (safe_file(phase_state, record.get("run_root"))
                              / "result.json").relative_to(phase_state).as_posix(),
            ),
            "old_manifest_sha256": record.get("manifest_sha256"),
        }
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, RuntimeError):
        return None


def prepared_inventory_history_plan(cfg, paths: list[str], frozen: dict,
                                    loaded: dict) -> Optional[dict]:
    """Prove a fresh prepared candidate can safely present old bootstrap notes as history."""
    import curate_once
    from caretaker.publication import (
        PublicationError, _validate_publication_decision, safe_file, sha,
    )

    try:
        project = cfg.project_slug
        ordered_paths = sorted(paths)
        if (not ordered_paths or paths != ordered_paths
                or not isinstance(loaded, dict)
                or loaded.get("phase") != "PREPARED"):
            return None
        phase_state = Path(cfg.state_dir) / "curation"
        pointer = curate_once._phase_pointer(phase_state, project, ordered_paths)
        pointer = curate_once._phase_regular_file(
            phase_state, pointer.name, limit=curate_once._PHASE_CACHE_LIMIT,
        )
        record = loaded
        progress = record.get("progress")
        if (not isinstance(progress, dict)
                or progress.get("deterministic_inventory_history_relocation") is True
                or progress.get("status") in {
                    "UNKNOWN", "UNKNOWN_REQUIRES_RECONCILIATION", "RUNNING",
                    "PUBLISHED", "ACCOUNTED", "accounted_elsewhere", "unsafe_source",
                }
                or record.get("schema_version") != 1
                or record.get("project") != project
                or record.get("paths") != ordered_paths
                or record.get("phase") != "PREPARED"
                or progress.get("assessment") is not None
                or progress.get("review_response") is not None
                or progress.get("review_rounds") not in (None, [])
                or not curate_once._phase_bindings_equal(
                    record.get("binding"), curate_once._phase_binding(frozen))):
            return None
        run_root = safe_file(phase_state, record.get("run_root"))
        if (not run_root.is_dir() or run_root.is_symlink() or run_root.is_junction()
                or not run_root.resolve().is_relative_to(phase_state.resolve())):
            return None
        candidate = safe_file(phase_state, record.get("candidate"))
        manifest = curate_once._phase_regular_file(phase_state, record.get("manifest"))
        if (not candidate.is_dir() or candidate.is_symlink() or candidate.is_junction()
                or manifest.name != "publication.json" or manifest.parent != candidate
                or sha(manifest.read_bytes()) != record.get("manifest_sha256")):
            return None
        pins = record.get("pins")
        if (not isinstance(pins, dict) or not pins
                or curate_once._phase_pins(phase_state, candidate) != pins):
            return None
        manifest_doc = json.loads(manifest.read_text(encoding="utf-8"))
        if (not isinstance(manifest_doc, dict)
                or manifest_doc.get("vault_path") != str(Path(cfg.hub.vault))
                or not curate_once._manifest_matches_frozen(manifest_doc, frozen)):
            return None
        ledger_path = curate_once._phase_regular_file(candidate, manifest_doc.get("ledger_path"))
        if sha(ledger_path.read_bytes()) != manifest_doc.get("ledger_sha256"):
            return None
        decision_path = curate_once._phase_regular_file(candidate, "decision.json")
        decision = json.loads(decision_path.read_text(encoding="utf-8"))
        _validate_publication_decision(frozen, decision)
        outputs = manifest_doc.get("outputs")
        if not isinstance(outputs, list) or not outputs:
            return None
        vault = Path(cfg.hub.vault)
        for output in outputs:
            if not isinstance(output, dict):
                return None
            payload = curate_once._phase_regular_file(candidate, output.get("payload_path"))
            if sha(payload.read_bytes()) != output.get("sha256"):
                return None
            current = safe_file(vault, output.get("path"))
            actual = sha(current.read_bytes()) if current.is_file() else None
            if actual != output.get("preimage_sha256"):
                return None
        transaction_dir = candidate / ".curation-transactions"
        if transaction_dir.exists():
            if transaction_dir.is_symlink() or transaction_dir.is_junction():
                return None
            for journal_path in transaction_dir.glob("tx-*.json"):
                if journal_path.name.endswith(".manifest.json"):
                    continue
                journal = json.loads(curate_once._phase_regular_file(
                    candidate, journal_path.relative_to(candidate).as_posix(),
                ).read_text(encoding="utf-8"))
                if not isinstance(journal, dict) or journal.get("status") != "rolled_back":
                    return None
        result_path = run_root / "result.json"
        if result_path.exists():
            if result_path.is_symlink() or result_path.is_junction():
                return None
            result = json.loads(curate_once._phase_regular_file(
                phase_state, result_path.relative_to(phase_state).as_posix(),
            ).read_text(encoding="utf-8"))
            if (not isinstance(result, dict)
                    or result.get("status") not in {"PENDING", "DEFERRED_PREPARED"}
                    or result.get("candidate_path") not in (None, str(candidate))
                    or result.get("run_path") not in (None, str(run_root))
                    or result.get("manifest_sha256") not in (None, sha(manifest.read_bytes()))
                    or result.get("snapshot_sha256") not in (None, frozen.get("snapshot_sha256"))
                    or result.get("target") not in (None, frozen.get("target"))
                    or set(result.get("proposal_ids") or [])
                       != {item.get("id") for item in frozen.get("proposals", [])}
                    or (result.get("status") == "DEFERRED_PREPARED"
                        and result.get("reason") != "tick_time_budget_exhausted")
                    or any(result.get(key) not in (None, "", {}, []) for key in (
                        "publication", "checkpoint", "event_id", "manifest",
                        "manifest_sha256_effect", "ledger_path", "transaction",
                    ))):
                return None
        prior = _prior_inventory_body_proof(vault, Path(cfg.state_dir), frozen)
        if prior is None:
            return None
        return {
            "pointer": pointer, "record": record, "progress": progress,
            "phase_state": phase_state, "run_root": run_root,
            "candidate": candidate, "manifest": manifest,
            "decision": decision, "prior_inventory_body": prior["body"],
            "prior_inventory": prior,
            "old_result_path": result_path if result_path.is_file() else None,
            "old_manifest_sha256": record.get("manifest_sha256"),
            "pins": pins,
        }
    except (OSError, UnicodeError, ValueError, TypeError, KeyError,
            RuntimeError, PublicationError):
        return None


def terminal_summary_retry_plan(vault: Path, state_dir: Path, frozen: dict,
                                old_result: dict, *, project: str,
                                paths: list[str]) -> Optional[dict]:
    """Validate one exact no-effect REPAIR_REJECTED summary-only continuation."""
    from caretaker.audit import accounted_proposal_ids
    from caretaker.publication import (
        PublicationError, _validate_publication_decision, canonical_project_slug,
        fm, safe_file, sha, normalize_review_finding, WEIGHT_BLOCKING,
    )
    from caretaker.pubstate import canonical_verdict, code_version
    from caretaker.registry import main_page, read_registry
    import curate_once

    if (not isinstance(old_result, dict)
            or old_result.get("status") != "QUARANTINED"
            or old_result.get("reason") != "independent_review_not_accepted"
            or old_result.get("original_reason") != "independent_review_not_accepted"
            or old_result.get("project") != project
            or old_result.get("publication") or old_result.get("checkpoint")
            or not isinstance(paths, list) or not paths or paths != sorted(paths)):
        return None
    pids = old_result.get("proposal_ids")
    old_paths = old_result.get("paths")
    hashes = old_result.get("hashes")
    if (not isinstance(pids, list) or not isinstance(old_paths, list)
            or not isinstance(hashes, list) or len(pids) != len(paths)
            or old_paths != paths or len(hashes) != len(paths)):
        return None
    try:
        from caretaker.pubstate import batch_key
        if old_result.get("batch") != batch_key(project, paths):
            return None
        registry, warning = read_registry(vault)
        if registry is None or warning is not None:
            return None
        target = main_page(project, registry)
        if frozen.get("target") != target:
            return None
        proposals = frozen.get("proposals")
        actual = {(item.get("id"), item.get("path"), item.get("sha256"))
                  for item in proposals if isinstance(item, dict)}
        expected = set(zip(pids, paths, hashes))
        if not expected or actual != expected or len(proposals) != len(expected):
            return None

        phase_state = Path(state_dir) / "curation"
        pointer = curate_once._phase_pointer(phase_state, project, paths)
        pointer = curate_once._phase_regular_file(
            phase_state, pointer.name, limit=curate_once._PHASE_CACHE_LIMIT,
        )
        record = json.loads(pointer.read_text(encoding="utf-8"))
        if (not isinstance(record, dict)
                or record.get("phase") != "REPAIR_REJECTED"
                or record.get("schema_version") != 1
                or record.get("project") != project
                or record.get("paths") != paths
                or record.get("code_version") != code_version()
                or record.get("cache_sha256") != curate_once._phase_digest(record)
                or not curate_once._phase_bindings_equal(
                    record.get("binding"), curate_once._phase_binding(frozen))):
            return None
        progress = record.get("progress")
        assessment = progress.get("assessment") if isinstance(progress, dict) else None
        review_rounds = progress.get("review_rounds") if isinstance(progress, dict) else None
        if (not isinstance(progress, dict)
                or progress.get("phase") != "REPAIR_REJECTED"
                or progress.get("manifest_sha256") != record.get("manifest_sha256")
                or type(progress.get("maker_calls")) is not int
                or progress["maker_calls"] < 2
                or type(progress.get("review_calls")) is not int
                or progress["review_calls"] < 2
                or progress.get("repair_attempt") not in (None, 1)
                or not isinstance(review_rounds, list) or len(review_rounds) != 2
                or any(not isinstance(item, dict) or item.get("verdict") != "REPAIR"
                       for item in review_rounds)
                or not isinstance(assessment, dict)
                or assessment.get("verdict") != "REPAIR"):
            return None
        terminal_digest = record.get("manifest_sha256")
        if (not isinstance(terminal_digest, str)
                or not re.fullmatch(r"[a-f0-9]{64}", terminal_digest)
                or (assessment.get("manifest_sha256") not in (None, terminal_digest))):
            return None
        findings = assessment.get("findings")
        if not isinstance(findings, list) or not findings:
            return None
        blocking = [normalize_review_finding(item) for item in findings
                    if normalize_review_finding(item).get("weight") == WEIGHT_BLOCKING]
        if (not blocking
                or any(item.get("class") != "inventory_as_project_knowledge"
                       or not re.search(r"\bsummary\b", item.get("text", ""), re.I)
                       for item in blocking)):
            return None

        run_root = safe_file(phase_state, record.get("run_root"))
        if (not run_root.is_dir() or run_root.is_symlink() or run_root.is_junction()
                or run_root.resolve().is_relative_to(phase_state.resolve()) is False):
            return None
        result_path = curate_once._phase_regular_file(
            phase_state, (run_root / "result.json").relative_to(phase_state).as_posix(),
        )
        run_result = json.loads(result_path.read_text(encoding="utf-8"))
        candidate = safe_file(phase_state, record.get("candidate"))
        manifest = curate_once._phase_regular_file(phase_state, record.get("manifest"))
        if (not candidate.is_dir() or candidate.is_symlink() or candidate.is_junction()
                or manifest.name != "publication.json" or manifest.parent != candidate
                or sha(manifest.read_bytes()) != terminal_digest
                or not isinstance(run_result, dict)
                or run_result.get("status") != "PENDING"
                or run_result.get("error_code") != "independent_review_not_accepted"
                or run_result.get("manifest_sha256") != terminal_digest
                or Path(run_result.get("candidate_path") or "").resolve() != candidate.resolve()
                or Path(run_result.get("run_path") or "").resolve() != run_root.resolve()
                or set(run_result.get("proposal_ids") or []) != set(pids)
                or run_result.get("publication") or run_result.get("checkpoint")):
            return None
        if (old_result.get("manifest_sha256") != terminal_digest
                or Path(old_result.get("candidate_path") or "").resolve() != candidate.resolve()):
            return None

        pins = record.get("pins")
        if (not isinstance(pins, dict) or not pins or len(pins) > 128
                or curate_once._phase_pins(phase_state, candidate) != pins):
            return None
        manifest_doc = json.loads(manifest.read_text(encoding="utf-8"))
        if (not isinstance(manifest_doc, dict)
                or manifest_doc.get("vault_path") != str(vault)
                or not curate_once._manifest_matches_frozen(manifest_doc, frozen)
                or manifest_doc.get("ledger_path") != "ledger.json"):
            return None
        ledger_path = curate_once._phase_regular_file(candidate, "ledger.json")
        if sha(ledger_path.read_bytes()) != manifest_doc.get("ledger_sha256"):
            return None
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        if (not isinstance(ledger, dict)
                or ledger.get("snapshot_sha256") != frozen.get("snapshot_sha256")):
            return None
        ledger_proposals = {
            (item.get("proposal_id"), item.get("path"), item.get("sha256"))
            for item in ledger.get("proposals", []) if isinstance(item, dict)
        }
        if ledger_proposals != expected:
            return None
        if set(pids) & accounted_proposal_ids(vault):
            return None
        transaction_dir = candidate / ".curation-transactions"
        if transaction_dir.exists():
            if transaction_dir.is_symlink() or transaction_dir.is_junction():
                return None
            for journal_path in transaction_dir.glob("tx-*.json"):
                if journal_path.name.endswith(".manifest.json"):
                    continue
                journal_path = curate_once._phase_regular_file(
                    candidate, journal_path.relative_to(candidate).as_posix(),
                )
                journal = json.loads(journal_path.read_text(encoding="utf-8"))
                if not isinstance(journal, dict) or journal.get("status") != "rolled_back":
                    return None
        for output in manifest_doc.get("outputs") or []:
            if not isinstance(output, dict):
                return None
            current = safe_file(vault, output.get("path"))
            actual_sha = sha(current.read_bytes()) if current.is_file() else None
            if actual_sha != output.get("preimage_sha256"):
                return None

        decision_path = curate_once._phase_regular_file(candidate, "decision.json")
        decision = json.loads(decision_path.read_text(encoding="utf-8"))
        if (not isinstance(decision, dict)
                or manifest_doc.get("summary") != decision.get("summary")):
            return None
        updated = corrected_draft_decision(frozen, decision, require_change=True)
        if updated is None:
            return None
        prior_summary = fm(frozen.get("before", ""), "summary")
        output_doc = next((item for item in manifest_doc.get("outputs", [])
                           if isinstance(item, dict) and item.get("path") == target), None)
        if output_doc is None:
            return None
        payload = curate_once._phase_regular_file(candidate, output_doc.get("payload_path"))
        prior_page = payload.read_text(encoding="utf-8")
        if fm(prior_page, "summary") != prior_summary:
            return None
        verdict, _evidence = canonical_verdict(vault, {
            "batch": old_result.get("batch"), "status": "QUARANTINED",
            "category": "deterministic", "fingerprint": old_result.get("fingerprint"),
            "result": json.dumps(old_result, ensure_ascii=False),
        })
        if verdict != "absent":
            return None
        return {
            "pointer": pointer, "record": record, "progress": progress,
            "phase_state": phase_state, "run_root": run_root,
            "candidate": candidate, "manifest": manifest,
            "decision": updated, "old_result_path": result_path,
            "old_manifest_sha256": terminal_digest,
        }
    except (OSError, UnicodeError, ValueError, TypeError, KeyError,
            RuntimeError, PublicationError):
        return None


def _prior_inventory_body_proof(vault: Path, state_dir: Path,
                                frozen: dict) -> Optional[dict]:
    """Find the unique attested prior page with a closed published provenance chain."""
    import curate_once
    from caretaker.publication import (
        PublicationError, _bootstrap_source_closure, _is_caretaker_draft, existing_page_body,
        json_bytes, safe_file, sha,
    )

    target = frozen.get("target")
    page_before = frozen.get("before")
    preimage = frozen.get("preimage_sha256")
    if (not isinstance(target, str) or not isinstance(page_before, str)
            or not isinstance(preimage, str)
            or not _is_caretaker_draft(
                page_before, target, vault=vault, state_dir=state_dir,
                page_sha256=preimage, expected_project=frozen.get("project_slug"))):
        return None
    matches = []
    curation = Path(state_dir) / "curation"
    try:
        for run_root in curation.iterdir():
            if (not run_root.is_dir() or run_root.is_symlink()
                    or run_root.is_junction()):
                continue
            result_path = run_root / "result.json"
            if not result_path.is_file() or result_path.is_symlink() or result_path.is_junction():
                continue
            result = json.loads(result_path.read_text(encoding="utf-8"))
            if not isinstance(result, dict):
                continue
            publication = result.get("publication") if isinstance(result, dict) else None
            event_id = publication.get("event_id") if isinstance(publication, dict) else None
            if (result.get("status") != "PUBLISHED"
                    or not isinstance(event_id, str)
                    or not re.fullmatch(r"evt-[A-Za-z0-9_-]+", event_id)):
                continue
            manifest_path = Path(result.get("manifest") or "")
            if (not manifest_path.is_file() or manifest_path.is_symlink()
                    or manifest_path.is_junction()
                    or not manifest_path.resolve().is_relative_to(run_root.resolve())):
                continue
            manifest_bytes = manifest_path.read_bytes()
            manifest_digest = sha(manifest_bytes)
            if (manifest_digest != result.get("manifest_sha256")
                    or manifest_digest != publication.get("manifest_sha256")):
                continue
            manifest = json.loads(manifest_bytes)
            if not isinstance(manifest, dict):
                continue
            outputs = manifest.get("outputs") if isinstance(manifest, dict) else None
            target_outputs = [item for item in outputs or []
                              if isinstance(item, dict) and item.get("path") == target]
            if (len(target_outputs) != 1
                    or target_outputs[0].get("sha256") != preimage):
                continue
            candidate = manifest_path.parent
            payload_path = curate_once._phase_regular_file(
                candidate, target_outputs[0].get("payload_path"),
            )
            if sha(payload_path.read_bytes()) != preimage:
                continue
            ledger_path = curate_once._phase_regular_file(candidate, manifest.get("ledger_path"))
            if (sha(ledger_path.read_bytes()) != manifest.get("ledger_sha256")
                    or manifest.get("vault_path") != str(vault)):
                continue
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            decision_path = curate_once._phase_regular_file(candidate, "decision.json")
            packet_path = curate_once._phase_regular_file(candidate, "source-packet.json")
            decision = json.loads(decision_path.read_text(encoding="utf-8"))
            packet = json.loads(packet_path.read_text(encoding="utf-8"))
            if (not isinstance(ledger, dict) or not isinstance(decision, dict)
                    or not isinstance(packet, dict)):
                continue
            proposals = packet.get("proposals") if isinstance(packet, dict) else None
            sources = packet.get("sources") if isinstance(packet, dict) else None
            dispositions = decision.get("dispositions") if isinstance(decision, dict) else None
            manifest_inputs = manifest.get("inputs") if isinstance(manifest, dict) else None
            packet_inputs = packet.get("inputs") if isinstance(packet, dict) else None
            if (packet.get("project_slug") != frozen.get("project_slug")
                    or packet.get("target") != target
                    or not isinstance(proposals, list) or not 1 <= len(proposals) <= 8
                    or not isinstance(sources, dict) or not sources
                    or not isinstance(dispositions, dict)
                    or set(decision) != {"body", "summary", "dispositions"}
                    or not isinstance(decision.get("body"), str)
                    or not isinstance(decision.get("summary"), str)
                    or not isinstance(ledger, dict)
                    or not isinstance(ledger.get("captured_at"), str)
                    or not isinstance(manifest_inputs, list)
                    or packet_inputs != manifest_inputs
                    or packet.get("snapshot_sha256") != manifest.get("snapshot_sha256")
                    or sha(json_bytes(manifest_inputs)) != manifest.get("snapshot_sha256")
                    or packet.get("preimage_sha256") != target_outputs[0].get("preimage_sha256")):
                continue
            input_hashes = {}
            input_paths_ok = True
            for item in manifest_inputs:
                if (not isinstance(item, dict)
                        or not isinstance(item.get("path"), str)
                        or not isinstance(item.get("sha256"), str)
                        or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
                        or type(item.get("bytes")) is not int or item["bytes"] < 0
                        or item["path"] in input_hashes):
                    input_paths_ok = False
                    break
                safe_file(vault, item["path"])
                input_hashes[item["path"]] = item["sha256"]
            if not input_paths_ok:
                continue
            proposal_by_id = {}
            proposal_input_ids = set()
            source_ref_ids = set()
            proposal_records_ok = True
            for proposal in proposals:
                if not isinstance(proposal, dict):
                    proposal_records_ok = False
                    break
                pid, path, digest = (proposal.get("id"), proposal.get("path"),
                                     proposal.get("sha256"))
                source_ids = proposal.get("source_ids")
                if (not isinstance(pid, str) or not pid
                        or pid in proposal_by_id
                        or not isinstance(path, str) or path in proposal_input_ids
                        or not isinstance(digest, str)
                        or not re.fullmatch(r"[0-9a-f]{64}", digest)
                        or input_hashes.get(path) != digest
                        or not isinstance(source_ids, list) or not source_ids
                        or any(not isinstance(sid, str) or not sid for sid in source_ids)
                        or len(set(source_ids)) != len(source_ids)):
                    proposal_records_ok = False
                    break
                proposal_by_id[pid] = (path, digest, source_ids)
                proposal_input_ids.add(path)
                source_ref_ids.update(source_ids)
            if (not proposal_records_ok or set(dispositions) != set(proposal_by_id)
                    or sorted(proposal_by_id) != sorted(manifest.get("proposal_ids") or [])
                    or set(result.get("proposal_ids") or []) != set(proposal_by_id)
                    or len(result.get("proposal_ids") or []) != len(proposal_by_id)):
                continue
            source_by_id = {}
            source_records_ok = True
            for sid, source in sources.items():
                if (not isinstance(sid, str) or not sid
                        or not isinstance(source, dict)
                        or source.get("source_id") != sid
                        or not isinstance(source.get("path"), str)
                        or not isinstance(source.get("sha256"), str)
                        or not re.fullmatch(r"[0-9a-f]{64}", source["sha256"])
                        or input_hashes.get(source["path"]) != source["sha256"]):
                    source_records_ok = False
                    break
                safe_file(vault, source["path"])
                source_by_id[sid] = (source["path"], source["sha256"])
            if (not source_records_ok or source_ref_ids != set(source_by_id)
                    or set(manifest.get("source_ids") or []) != set(source_by_id)
                    or len(manifest.get("source_ids") or []) != len(source_by_id)):
                continue
            ledger_proposals = ledger.get("proposals")
            ledger_sources = ledger.get("sources")
            if (not isinstance(ledger_proposals, list)
                    or len(ledger_proposals) != len(proposal_by_id)
                    or not isinstance(ledger_sources, list)
                    or len(ledger_sources) != len(source_by_id)):
                continue
            ledger_proposal_by_id = {}
            ledger_sources_by_id = {}
            for item in ledger_proposals:
                if not isinstance(item, dict):
                    ledger_proposal_by_id = {}
                    break
                pid = item.get("proposal_id")
                if pid in ledger_proposal_by_id:
                    ledger_proposal_by_id = {}
                    break
                ledger_proposal_by_id[pid] = (
                    item.get("path"), item.get("sha256"), item.get("source_ids"),
                    item.get("disposition"),
                )
            for item in ledger_sources:
                if not isinstance(item, dict):
                    ledger_sources_by_id = {}
                    break
                sid = item.get("source_id")
                if sid in ledger_sources_by_id:
                    ledger_sources_by_id = {}
                    break
                ledger_sources_by_id[sid] = (item.get("path"), item.get("sha256"))
            expected_ledger_proposals = {
                pid: (path, digest, source_ids,
                      dispositions[pid].get("disposition")
                      if isinstance(dispositions[pid], dict) else None)
                for pid, (path, digest, source_ids) in proposal_by_id.items()
            }
            if (ledger_proposal_by_id != expected_ledger_proposals
                    or ledger_sources_by_id != source_by_id):
                continue
            event_path = safe_file(vault, f"governance/curations/{event_id}.json")
            event = json.loads(event_path.read_text(encoding="utf-8"))
            if not isinstance(event, dict):
                continue
            review = event.get("review") if isinstance(event, dict) else None
            reviewer = review.get("reviewer") if isinstance(review, dict) else None
            if (event.get("event_id") != event_id
                    or event.get("manifest_sha256") != manifest_digest
                    or not isinstance(review, dict)
                    or review.get("verdict") != "PASS"
                    or review.get("manifest_sha256") != manifest_digest
                    or not isinstance(reviewer, dict)
                    or not isinstance(reviewer.get("id"), str)
                    or reviewer["id"] in {"", "openclaw-caretaker"}):
                continue
            matches.append({"body": existing_page_body(page_before), "run_root": run_root,
                            "manifest_sha256": manifest_digest,
                            "proposal_ids": sorted(proposal_by_id),
                            "source_ids": sorted(source_by_id),
                            "canonical_bootstrap_metadata": all(
                                _bootstrap_source_closure({"sources": sources}, proposal)
                                for proposal in proposals
                            )})
    except (OSError, UnicodeError, ValueError, TypeError, KeyError,
            RuntimeError, PublicationError):
        return None
    return matches[0] if len(matches) == 1 else None


def inventory_history_repair_plan(cfg, paths: list[str], frozen: dict,
                                  failed_result: Optional[dict] = None) -> Optional[dict]:
    """Prove the exact pre-effect inventory-only REPAIR_FAILED continuation."""
    import curate_once
    from caretaker.publication import (
        PublicationError, _validate_publication_decision, knowledge_basis,
        safe_file, sha,
    )
    from caretaker.pubstate import code_version
    from caretaker.registry import read_registry, main_page

    try:
        project = cfg.project_slug
        ordered_paths = sorted(paths)
        if not ordered_paths or paths != ordered_paths:
            return None
        vault = Path(cfg.hub.vault)
        state = Path(cfg.state_dir)
        phase_state = state / "curation"
        pointer = curate_once._phase_pointer(phase_state, project, ordered_paths)
        pointer = curate_once._phase_regular_file(
            phase_state, pointer.name, limit=curate_once._PHASE_CACHE_LIMIT,
        )
        record = json.loads(pointer.read_text(encoding="utf-8"))
        progress = record.get("progress") if isinstance(record, dict) else None
        if (not isinstance(record, dict) or record.get("phase") != "REPAIR_FAILED"
                or record.get("schema_version") != 1 or record.get("project") != project
                or record.get("paths") != ordered_paths
                or record.get("code_version") != code_version()
                or record.get("cache_sha256") != curate_once._phase_digest(record)
                or not curate_once._phase_bindings_equal(
                    record.get("binding"), curate_once._phase_binding(frozen))
                or not isinstance(progress, dict)
                or progress.get("phase") != "REPAIR_FAILED"
                or type(progress.get("maker_calls")) is not int
                or progress.get("maker_calls") != 2
                or type(progress.get("review_calls")) is not int
                or progress.get("review_calls") != 1
                or progress.get("status") in {
                    "UNKNOWN", "UNKNOWN_REQUIRES_RECONCILIATION", "RUNNING",
                    "PUBLISHED", "ACCOUNTED", "accounted_elsewhere", "unsafe_source",
                }
                or progress.get("repair_attempt") not in (None, 1)
                or not isinstance(progress.get("maker_provider"), str)
                or not isinstance(progress.get("maker_model"), str)
                or not isinstance(progress.get("maker_route"), str)):
            return None
        assessment = progress.get("assessment")
        rounds = progress.get("review_rounds")
        if (not isinstance(assessment, dict) or assessment.get("verdict") != "REPAIR"
                or not isinstance(rounds, list) or len(rounds) != 1
                or not isinstance(rounds[0], dict) or rounds[0].get("verdict") != "REPAIR"):
            return None
        from caretaker.publication import WEIGHT_BLOCKING, normalize_review_finding
        findings = assessment.get("findings")
        if not isinstance(findings, list) or not findings:
            return None
        normalized = [normalize_review_finding(item) for item in findings]
        blocking = [item for item in normalized if item.get("weight") == WEIGHT_BLOCKING]
        inventory_history_finding = (
            len(blocking) == 1
            and blocking[0].get("class") == "inventory_as_project_knowledge"
            and all(item.get("class") in {
                "inventory_as_project_knowledge", "incomplete_coverage",
            } for item in normalized)
        )
        source_misattribution_finding = (
            len(blocking) == 1
            and blocking[0].get("class") == "source_misattribution"
            and all(item.get("class") == "source_misattribution" for item in normalized)
        )
        if not (inventory_history_finding or source_misattribution_finding):
            return None

        run_root = safe_file(phase_state, record.get("run_root"))
        if (not run_root.is_dir() or run_root.is_symlink() or run_root.is_junction()
                or not run_root.resolve().is_relative_to(phase_state.resolve())):
            return None
        result_path = curate_once._phase_regular_file(
            phase_state, (run_root / "result.json").relative_to(phase_state).as_posix(),
        )
        run_result = json.loads(result_path.read_text(encoding="utf-8"))
        if (not isinstance(run_result, dict)
                or run_result.get("status") != "PENDING"
                or run_result.get("reason") != "citation_outside_publication"
                or run_result.get("error_code") != "citation_outside_publication"
                or run_result.get("review_repair_exhausted") is not True
                or run_result.get("format_repair_exhausted") is not False
                or run_result.get("candidate_path") is not None
                or Path(run_result.get("run_path") or "").resolve() != run_root.resolve()
                or run_result.get("maker_calls") != progress.get("maker_calls")
                or run_result.get("review_calls") != progress.get("review_calls")
                or run_result.get("target") != frozen.get("target")
                or run_result.get("snapshot_sha256") != frozen.get("snapshot_sha256")
                or sorted(run_result.get("proposal_ids") or [])
                   != sorted(item.get("id") for item in frozen.get("proposals", []))
                or any(run_result.get(name) not in (None, "", {}, []) for name in (
                    "publication", "checkpoint", "event_id", "manifest",
                    "manifest_sha256", "ledger_path", "publication_event", "transaction",
                ))):
            return None
        if failed_result is not None:
            if (failed_result.get("status") != "QUARANTINED"
                    or failed_result.get("reason") != "citation_outside_publication"
                    or failed_result.get("original_reason") != "citation_outside_publication"
                    or failed_result.get("project") != project
                    or failed_result.get("paths") != ordered_paths
                    or failed_result.get("cycle_id") is None
                    or failed_result.get("candidate_path") is not None
                    or failed_result.get("publication") or failed_result.get("checkpoint")):
                return None
            if (failed_result.get("proposal_ids") != run_result.get("proposal_ids")
                    or failed_result.get("hashes") != [item.get("sha256") for item in frozen.get("proposals", [])]
                    or failed_result.get("run_path") != run_result.get("run_path")):
                return None

        candidate = run_root / "candidate"
        if not candidate.is_dir() or candidate.is_symlink() or candidate.is_junction():
            return None
        manifest = curate_once._phase_regular_file(candidate, "publication.json")
        manifest_digest = sha(manifest.read_bytes())
        if assessment.get("manifest_sha256") != manifest_digest:
            return None
        manifest_doc = json.loads(manifest.read_text(encoding="utf-8"))
        if (not isinstance(manifest_doc, dict)
                or manifest_doc.get("vault_path") != str(vault)
                or not curate_once._manifest_matches_frozen(manifest_doc, frozen)
                or manifest_doc.get("ledger_path") != "ledger.json"):
            return None
        pins = curate_once._phase_pins(phase_state, candidate)
        ledger_path = curate_once._phase_regular_file(candidate, "ledger.json")
        if sha(ledger_path.read_bytes()) != manifest_doc.get("ledger_sha256"):
            return None
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        decision_path = curate_once._phase_regular_file(candidate, "decision.json")
        decision = json.loads(decision_path.read_text(encoding="utf-8"))
        _body, _summary, dispositions, _pids = _validate_publication_decision(frozen, decision)
        basis = knowledge_basis(frozen, decision["body"], dispositions)
        if (basis.get("status") != "durable"
                or not isinstance(ledger, dict)
                or ledger.get("knowledge_basis", {}).get("status") != "durable"):
            return None
        for output in manifest_doc.get("outputs") or []:
            if not isinstance(output, dict):
                return None
            payload = curate_once._phase_regular_file(candidate, output.get("payload_path"))
            if sha(payload.read_bytes()) != output.get("sha256"):
                return None
            current = safe_file(vault, output.get("path"))
            current_sha = sha(current.read_bytes()) if current.is_file() else None
            if current_sha != output.get("preimage_sha256"):
                return None
        transaction_dir = candidate / ".curation-transactions"
        if transaction_dir.exists():
            if transaction_dir.is_symlink() or transaction_dir.is_junction():
                return None
            for journal_path in transaction_dir.glob("tx-*.json"):
                if journal_path.name.endswith(".manifest.json"):
                    continue
                journal = json.loads(curate_once._phase_regular_file(
                    candidate, journal_path.relative_to(candidate).as_posix(),
                ).read_text(encoding="utf-8"))
                if not isinstance(journal, dict) or journal.get("status") != "rolled_back":
                    return None
        prior = _prior_inventory_body_proof(vault, state, frozen)
        if prior is None:
            return None
        updated = corrected_draft_decision(frozen, decision, require_change=True)
        if updated is None:
            return None
        if failed_result is not None and failed_result.get("status") != "QUARANTINED":
            return None
        return {
            "pointer": pointer, "record": record, "progress": progress,
            "phase_state": phase_state, "run_root": run_root,
            "candidate": candidate, "manifest": manifest,
            "decision": updated, "prior_inventory_body": prior["body"],
            "prior_inventory": prior, "old_result_path": result_path,
            "old_manifest_sha256": manifest_digest, "pins": pins,
            "source_misattribution_repair": source_misattribution_finding,
        }
    except (OSError, UnicodeError, ValueError, TypeError, KeyError,
            RuntimeError, PublicationError):
        return None


def _relocate_inventory_history_candidate(frozen: dict, candidate: Path,
                                          manifest: Path, prior_body: str,
                                          observed: str) -> bool:
    """Move the exact attested before-body behind a historical label in one candidate."""
    from caretaker.publication import existing_page_body, json_bytes, safe_file, sha

    try:
        candidate = Path(candidate)
        manifest = Path(manifest)
        if (not candidate.is_dir() or candidate.is_symlink() or candidate.is_junction()
                or manifest.is_symlink() or manifest.is_junction()
                or manifest.parent.resolve() != candidate.resolve()
                or not isinstance(prior_body, str) or not prior_body):
            return False
        manifest_doc = json.loads(manifest.read_text(encoding="utf-8"))
        outputs = manifest_doc.get("outputs") if isinstance(manifest_doc, dict) else None
        target = frozen.get("target")
        matching = [item for item in outputs or []
                    if isinstance(item, dict) and item.get("path") == target]
        if len(matching) != 1:
            return False
        payload = safe_file(candidate, matching[0].get("payload_path"))
        raw_payload = payload.read_bytes()
        if sha(raw_payload) != matching[0].get("sha256"):
            return False
        page = raw_payload.decode("utf-8")
        split = page.find("\n---\n", 4)
        if split < 0:
            return False
        header = page[:split + 5]
        rendered_body = existing_page_body(page)
        if rendered_body.startswith("\n" + prior_body):
            # The canonical page wrapper contributes one blank-line newline
            # before a preserved body. Exclude only that renderer byte.
            rendered_body = rendered_body[1:]
        if not rendered_body.startswith(prior_body):
            return False
        tail = rendered_body[len(prior_body):]
        separator = "\n" if prior_body.endswith("\n") else "\n\n"
        if not tail.startswith(separator):
            return False
        new_section = tail[len(separator):]
        expected_heading = "## Caretaker publication " + str(observed or "")
        if not new_section.startswith(expected_heading):
            return False
        history_label = (
            "## Historical evidence\n\n"
            "The following section is retained verbatim from the previously published "
            "and attested project page. It is historical context and is not asserted "
            "as current project knowledge.\n\n"
        )
        payload.write_text(
            header + new_section.rstrip("\n") + "\n\n" + history_label + prior_body,
            encoding="utf-8", newline="\n",
        )
        matching[0]["sha256"] = sha(payload.read_bytes())
        manifest.write_bytes(json_bytes(manifest_doc))
        return True
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, RuntimeError):
        return False


def archive_and_relocate_inventory_history(plan: dict, frozen: dict, cfg,
                                            paths: list[str], build_candidate,
                                            save_phase) -> Optional[dict]:
    """Archive a proved pre-effect phase and prepare a fresh-review candidate with history relocated."""
    import uuid
    import curate_once
    from caretaker.publication import PublicationError
    from caretaker.publication import json_bytes, sha

    try:
        record = plan["record"]
        phase_state = Path(plan["phase_state"])
        run_root = Path(plan["run_root"])
        archive = run_root / ("inventory-history-archive-" + plan["old_manifest_sha256"][:16])
        files = [
            ("phase.json", Path(plan["pointer"]).read_bytes()),
            ("candidate-pins.json", json_bytes(plan["pins"])),
        ]
        old_result_path = plan.get("old_result_path")
        if old_result_path is not None and Path(old_result_path).is_file():
            files.append(("result.json", Path(old_result_path).read_bytes()))
        for name in ("review.json", "semantic-review.json"):
            review_path = Path(plan["candidate"]) / name
            if review_path.is_file() and not review_path.is_symlink() and not review_path.is_junction():
                files.append((name, review_path.read_bytes()))
        if archive.exists():
            if archive.is_symlink() or archive.is_junction() or not archive.is_dir():
                return None
        else:
            archive.mkdir()
        for name, data in files:
            archived = archive / name
            if archived.exists():
                if archived.is_symlink() or archived.read_bytes() != data:
                    return None
            else:
                archived.write_bytes(data)

        build_frozen = dict(frozen)
        previous_target = frozen.get("target")
        candidate = run_root / ("candidate-inventory-history-" + uuid.uuid4().hex[:12])
        if candidate.exists():
            return None
        manifest = Path(build_candidate(
            build_frozen, plan["decision"], candidate, record.get("observed"),
        ))
        if (build_frozen.get("target") != previous_target
                or not manifest.is_file() or manifest.parent != candidate):
            return None
        if not _relocate_inventory_history_candidate(
                build_frozen, candidate, manifest,
                plan["prior_inventory_body"], record.get("observed")):
            return None
        manifest_digest = sha(manifest.read_bytes())

        old_progress = plan["progress"]
        progress = {
            key: old_progress[key]
            for key in ("maker_provider", "maker_model", "maker_route", "observed")
            if key in old_progress
        }
        progress.update(
            phase="PREPARED", maker_calls=old_progress["maker_calls"],
            review_calls=old_progress["review_calls"], maker_format_repairs=0,
            reviewer_format_repairs={}, review_rounds=[], assessment=None,
            prior_interrupted_calls={
                "maker": old_progress["maker_calls"],
                "reviewer": old_progress["review_calls"],
            },
            deterministic_inventory_history_relocation=True,
        )
        save_phase(
            cfg, paths, build_frozen, run_root, record.get("observed"),
            "PREPARED", candidate, manifest, progress,
        )
        return {"candidate": candidate, "manifest": manifest,
                "manifest_sha256": manifest_digest, "phase": "PREPARED"}
    except (OSError, UnicodeError, ValueError, TypeError, KeyError,
            RuntimeError, PublicationError):
        return None


def archive_and_rerender(plan: dict, frozen: dict, cfg, paths: list[str],
                         build_candidate, save_phase, *,
                         terminal_retry: bool = False) -> Optional[dict]:
    """Archive prior evidence and persist a fresh unreviewed candidate phase."""
    import uuid
    from caretaker.publication import sha
    import curate_once

    try:
        record = plan["record"]
        run_root = Path(plan["run_root"])
        phase_state = Path(plan["phase_state"])
        old_candidate = Path(plan["candidate"])
        old_manifest_sha = plan["old_manifest_sha256"]
        archive = run_root / ("summary-repair-archive-" + old_manifest_sha[:16])
        if archive.exists():
            if archive.is_symlink() or archive.is_junction() or not archive.is_dir():
                return None
        else:
            archive.mkdir()

        files = [("phase.json", Path(plan["pointer"]).read_bytes())]
        result_path = plan.get("old_result_path") or (run_root / "result.json")
        if Path(result_path).is_file():
            files.append(("result.json", Path(result_path).read_bytes()))
        for name in ("review.json", "semantic-review.json"):
            review_path = old_candidate / name
            if review_path.is_file() and not review_path.is_symlink() and not review_path.is_junction():
                files.append((name, review_path.read_bytes()))
        for name, data in files:
            archived = archive / name
            if archived.exists():
                if archived.is_symlink() or archived.read_bytes() != data:
                    return None
            else:
                archived.write_bytes(data)

        output_name = "candidate-summary-repair-" + uuid.uuid4().hex[:12]
        candidate = run_root / output_name
        if candidate.exists():
            return None
        decision = plan["decision"]
        manifest = Path(build_candidate(frozen, decision, candidate,
                                        record.get("observed")))
        if not manifest.is_file() or manifest.parent != candidate:
            return None
        manifest_sha = sha(manifest.read_bytes())
        saved_phase = ("REPAIR_PREPARED" if terminal_retry
                       else record.get("phase"))
        if terminal_retry and saved_phase != "REPAIR_PREPARED":
            return None
        progress = dict(plan["progress"])
        progress["phase"] = saved_phase
        progress["manifest_sha256"] = manifest_sha
        progress["reviewer_format_repairs"] = {}
        progress.pop("reviewer_retry", None)
        progress.pop("review_response", None)
        progress.pop("fleet_review_path", None)
        if saved_phase == "PREPARED":
            progress["assessment"] = None
            progress.pop("repair_attempt", None)
        save_phase(
            cfg, paths, frozen, run_root, record.get("observed"), saved_phase,
            candidate, manifest, progress,
        )
        return {"candidate": candidate, "manifest": manifest,
                "manifest_sha256": manifest_sha, "phase": saved_phase}
    except (OSError, UnicodeError, ValueError, TypeError, KeyError,
            RuntimeError):
        return None
