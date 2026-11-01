"""Narrow, code-managed admission for a small set of exact publication repairs.

This is deliberately not a general retry policy. Each admitted case has a
specific pre-effect proof and a one-time token bound to the publisher bytes
and frozen proposal identity. Historical publication rows remain untouched;
the ordinary cycle and publication lifecycle own each successor.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import stat
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple


REPAIRED_REASON = "section_split_unanchored_keep_pending"
TARGET_PROVENANCE_RETRY_REASON = "target is not reviewed"
SUMMARY_REPAIR_SOURCE_REASON = "independent_review_not_accepted"
SUMMARY_REPAIR_REASON = "attested_draft_summary_inventory_review"
INVENTORY_HISTORY_SOURCE_REASON = "citation_outside_publication"
INVENTORY_HISTORY_RETRY_REASON = "attested_draft_inventory_history_review"
INVALID_SUMMARY_FORMAT_SOURCE_REASON = "invalid summary"
INVALID_SUMMARY_FORMAT_RETRY_REASON = "pre_candidate_invalid_summary_format"
PHASE_RESUME_REASON = "phase_resume_binding_invalid"
NO_INDEPENDENT_REVIEWER_PHASE_REASON = "no_independent_reviewer"
TERMINAL_GENERATION_SOURCE_REASON = "phase_resume_terminal_no_replay"
TERMINAL_GENERATION_RETRY_REASON = "terminal_generation_source_retry"
REPAIRED_PUBLICATION_SHA256 = "fa5d3bdfa5489d488ef2e260a7c389b7b61f8a166a8708b92b2ad81b669a5a32"
PHASE_RESUME_CURATE_SHA256 = "0296c724343a7cc11d7bf6e5ba41e9745b0b29874ef0b82bef82e5e833826ff1"
PHASE_RESUME_CODE_VERSION = "4294093f1335b1309bbbe0a9a8b4c499"
LEGACY_PREPARED_CODE_VERSION = "416088b2471fa1a108e270bd91744595"
RETRY_TABLE = "repaired_retry_tokens"


def prepared_publish_timeout_proof(config_path: str, proposal_paths: Sequence[str],
                                   error: BaseException) -> Optional[Dict[str, Any]]:
    """Return a safe pre-effect defer only for a proved canonical publish timeout.

    The phase and retained-candidate loaders revalidate the current frozen
    inputs, target preimage, review, and every pinned artifact. The canonical
    ledger must independently prove that none of the exact proposals has been
    committed. Any malformed, stale, ambiguous, or unrelated timeout fails
    closed and returns None.
    """
    if (type(error) is not subprocess.TimeoutExpired
            or getattr(error, "_caretaker_canonical_publish_command", False) is not True):
        return None
    argv = error.cmd
    if (not isinstance(argv, (list, tuple))
            or any(not isinstance(part, str) for part in argv)
            or len(argv) != 14
            or argv[1:4] != ["-B", "-X", "utf8"]
            or argv[5] != "publish"
            or argv[6] != "--manifest"
            or argv[8] != "--review"
            or argv[10] != "--lock-id"
            or argv[12] != "--agent"
            or argv[13] != "openclaw-caretaker"
            or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", argv[11])):
        return None
    paths = list(proposal_paths) if isinstance(proposal_paths, (list, tuple)) else []
    if (not 1 <= len(paths) <= 8
            or any(not _valid_path(path) for path in paths)
            or len(set(paths)) != len(paths)):
        return None
    try:
        from caretaker.config import load_config
        from caretaker.publication import freeze, safe_file
        from caretaker.pubstate import canonical_verdict, code_version
        from caretaker.registry import main_page, read_registry
        import curate_once

        cfg = load_config(config_path)
        if code_version() != PHASE_RESUME_CODE_VERSION:
            return None
        engine = Path(cfg.hub.script).with_name("curation.py").resolve()
        if (argv[0] != cfg.hub.python_executable
                or Path(argv[4]).resolve() != engine):
            return None
        state = Path(cfg.state_dir)
        phase_state = state / "curation"
        vault = Path(cfg.hub.vault)
        registry, warning = read_registry(vault)
        if registry is None or warning is not None:
            return None
        target = main_page(cfg.project_slug, registry)
        frozen = freeze(
            vault, paths, target, attestation_state_dir=state,
        )
        # The reviewed-candidate loader is the authority for the *complete*
        # current candidate, reviewer evidence, source bytes, target preimage,
        # canonical accounting, and every current candidate pin. Do not
        # reconstruct or waive a stale phase pin set to make the candidate fit.
        retained = curate_once._load_reviewed_candidate(
            cfg, paths, phase_state, frozen=frozen,
        )
        if retained is None:
            return None
        result, run_root = retained
        manifest_path = Path(result.get("manifest", "")).resolve()
        review_path = Path(result.get("review", "")).resolve()
        phase_pointer = curate_once._phase_pointer(
            phase_state, cfg.project_slug, paths,
        )
        phase_pointer = curate_once._phase_regular_file(
            phase_state, phase_pointer.name, limit=curate_once._PHASE_CACHE_LIMIT,
        )
        phase_record = json.loads(phase_pointer.read_text(encoding="utf-8"))
        phase_progress = phase_record.get("progress") if isinstance(phase_record, dict) else None
        if (not isinstance(phase_record, dict)
                or not isinstance(phase_progress, dict)
                or phase_record.get("schema_version") != 1
                or phase_record.get("project") != cfg.project_slug
                or phase_record.get("paths") != sorted(paths)
                or phase_record.get("phase") != "REVIEW_ACCEPTED"
                or phase_record.get("code_version") != PHASE_RESUME_CODE_VERSION
                or phase_record.get("cache_sha256") != curate_once._phase_digest(phase_record)
                or phase_progress.get("status") in {
                    "UNKNOWN", "UNKNOWN_REQUIRES_RECONCILIATION", "RUNNING",
                    "PUBLISHED", "ACCOUNTED", "accounted_elsewhere", "unsafe_source",
                }
                or type(phase_progress.get("maker_calls")) is not int
                or phase_progress["maker_calls"] < 1
                or type(phase_progress.get("review_calls")) is not int
                or phase_progress["review_calls"] < 1
                or not isinstance(phase_progress.get("assessment"), dict)
                or phase_progress["assessment"].get("verdict") != "PASS"
                or phase_progress["assessment"].get("manifest_sha256")
                != phase_record.get("manifest_sha256")):
            return None
        if (result.get("status") != "REVIEWED_CANDIDATE"
                or Path(argv[7]).resolve() != manifest_path
                or Path(argv[9]).resolve() != review_path
                or result.get("manifest_sha256") != phase_record.get("manifest_sha256")
                or safe_file(phase_state, phase_record.get("run_root")).resolve() != run_root.resolve()
                or safe_file(phase_state, phase_record.get("manifest")).resolve() != manifest_path
                or safe_file(phase_state, phase_record.get("candidate")).resolve() != manifest_path.parent):
            return None
        # Existing phase pins must still match, while the reviewed-candidate
        # loader above has already checked the complete post-review pin set.
        phase_pins = phase_record.get("pins")
        if not isinstance(phase_pins, dict) or not phase_pins or len(phase_pins) > 128:
            return None
        current_pins = curate_once._phase_pins(phase_state, manifest_path.parent)
        if any(current_pins.get(name) != digest for name, digest in phase_pins.items()):
            return None
        proposals = frozen.get("proposals")
        if not isinstance(proposals, list) or len(proposals) != len(paths):
            return None
        identities = {item.get("path"): item for item in proposals
                      if isinstance(item, dict)}
        if (set(identities) != set(paths)
                or any(not isinstance(identities[path].get("id"), str)
                       or not isinstance(identities[path].get("sha256"), str)
                       for path in paths)):
            return None
        canonical_row = {
            "result": json.dumps({
                "status": "RUNNING",
                "proposal_ids": [identities[path]["id"] for path in paths],
                "paths": paths,
                "hashes": [identities[path]["sha256"] for path in paths],
                "state_dir": str(state),
            }),
        }
        verdict, _evidence = canonical_verdict(vault, canonical_row)
        if verdict != "absent":
            return None
        return {
            "status": "DEFERRED_PREPARED",
            "reason": "canonical_publish_timeout_pre_effect",
            "phase": "REVIEW_ACCEPTED",
            "run_path": str(run_root),
            "candidate_path": str(manifest_path.parent),
            "manifest_sha256": phase_record["manifest_sha256"],
            "maker_calls": int(phase_progress["maker_calls"]),
            "review_calls": int(phase_progress["review_calls"]),
            "proposal_ids": [identities[path]["id"] for path in paths],
            "paths": paths,
            "hashes": [identities[path]["sha256"] for path in paths],
            "project": cfg.project_slug,
            "state_dir": str(state),
            "snapshot_sha256": frozen.get("snapshot_sha256"),
            "target": frozen.get("target"),
            "model_calls": 0,
        }
    except Exception:
        return None


def publication_sha256(path: Optional[Path] = None) -> Optional[str]:
    """Return the actual current publication.py digest, or None if unreadable."""
    target = Path(path) if path is not None else Path(__file__).with_name("publication.py")
    try:
        return hashlib.sha256(target.read_bytes()).hexdigest()
    except OSError:
        return None


def curate_sha256(path: Optional[Path] = None) -> Optional[str]:
    """Return the current exact curate_once.py digest, or None if unreadable."""
    target = Path(path) if path is not None else Path(__file__).parents[1] / "curate_once.py"
    try:
        return hashlib.sha256(target.read_bytes()).hexdigest()
    except OSError:
        return None


def _valid_path(value: Any) -> bool:
    if (not isinstance(value, str) or "\\" in value
            or not re.fullmatch(r"proposals/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+\.md", value)):
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and not any(part in {"", ".", ".."} for part in path.parts)
        and len(path.parts) >= 3
        and path.parts[0] == "proposals"
        and path.suffix == ".md"
    )


def _valid_batch_row(batch: Any, status: Any, failures: Any, attempt: Any,
                     category: Any, stored_fp: Any, raw_result: Any) -> Optional[Dict[str, Any]]:
    if status != "QUARANTINED" or category != "deterministic":
        return None
    if not isinstance(failures, int) or failures < 1 or not isinstance(attempt, int) or attempt < 1:
        return None
    if not isinstance(stored_fp, str) or not re.fullmatch(r"[0-9a-f]{64}", stored_fp):
        return None
    try:
        value = json.loads(raw_result or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    reason = value.get("reason")
    if (value.get("status") != "QUARANTINED"
            or reason not in {REPAIRED_REASON, TARGET_PROVENANCE_RETRY_REASON,
                              SUMMARY_REPAIR_SOURCE_REASON}
            or value.get("original_reason") != reason):
        return None
    project = value.get("project")
    pids, paths, hashes = (value.get(key) for key in ("proposal_ids", "paths", "hashes"))
    cycle_id = value.get("cycle_id")
    if not isinstance(project, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,100}", project):
        return None
    if not isinstance(pids, list) or not isinstance(paths, list) or not isinstance(hashes, list):
        return None
    if not 1 <= len(pids) <= 8 or len(pids) != len(paths) or len(paths) != len(hashes):
        return None
    if (any(not isinstance(pid, str) or not re.fullmatch(r"prop-[A-Za-z0-9._-]+", pid) for pid in pids)
            or any(not _valid_path(path) for path in paths)
            or any(not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                   for digest in hashes)):
        return None
    if len(set(pids)) != len(pids) or len(set(paths)) != len(paths):
        return None
    if not isinstance(cycle_id, str) or not cycle_id:
        return None
    from .pubstate import batch_key
    if batch != batch_key(project, paths) or value.get("batch") != batch:
        return None
    return {
        "batch": batch,
        "project": project,
        "proposal_ids": pids,
        "paths": paths,
        "hashes": hashes,
        "cycle_id": cycle_id,
        "reason": reason,
        "fingerprint": stored_fp,
        "result": value,
    }


def _valid_inventory_history_row(batch: Any, status: Any, failures: Any,
                                 attempt: Any, category: Any, stored_fp: Any,
                                 raw_result: Any) -> Optional[Dict[str, Any]]:
    """Validate one exact failed pre-effect inventory-repair row, at most twice."""
    if (status != "QUARANTINED" or category != "deterministic"
            or type(failures) is not int or failures not in (1, 2)
            or type(attempt) is not int or attempt != failures
            or not isinstance(stored_fp, str)
            or not re.fullmatch(r"[0-9a-f]{64}", stored_fp)):
        return None
    try:
        value = json.loads(raw_result or "{}")
    except (TypeError, ValueError):
        return None
    if (not isinstance(value, dict)
            or value.get("status") != "QUARANTINED"
            or value.get("reason") != INVENTORY_HISTORY_SOURCE_REASON
            or value.get("original_reason") != INVENTORY_HISTORY_SOURCE_REASON
            or value.get("error_code") != INVENTORY_HISTORY_SOURCE_REASON
            or value.get("review_repair_exhausted") is not True
            or value.get("format_repair_exhausted") is not False
            or value.get("candidate_path") is not None
            or type(value.get("maker_calls")) is not int or value["maker_calls"] != 2
            or type(value.get("review_calls")) is not int or value["review_calls"] != 1):
        return None
    effect_fields = (
        "publication", "checkpoint", "event_id", "manifest", "manifest_sha256",
        "ledger_path", "publication_event", "transaction", "review",
    )
    if any(value.get(key) not in (None, "", {}, []) for key in effect_fields):
        return None
    project = value.get("project")
    pids, paths, hashes = (value.get(key) for key in ("proposal_ids", "paths", "hashes"))
    cycle_id = value.get("cycle_id")
    if (not isinstance(project, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,100}", project)
            or not isinstance(pids, list) or not isinstance(paths, list)
            or not isinstance(hashes, list) or len(pids) != 1
            or len(pids) != len(paths) or len(paths) != len(hashes)
            or not isinstance(cycle_id, str) or not cycle_id
            or any(not isinstance(pid, str) or not re.fullmatch(r"prop-[A-Za-z0-9._-]+", pid)
                   for pid in pids)
            or any(not _valid_path(path) for path in paths)
            or any(not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                   for digest in hashes)
            or value.get("batch") != batch):
        return None
    from .pubstate import batch_key
    if batch != batch_key(project, paths):
        return None
    return {
        "batch": batch, "project": project, "proposal_ids": pids,
        "paths": paths, "hashes": hashes, "cycle_id": cycle_id,
        "reason": INVENTORY_HISTORY_SOURCE_REASON, "fingerprint": stored_fp,
        "result": value, "inventory_history_source": True,
    }


def _valid_pre_candidate_summary_format_row(
        batch: Any, status: Any, failures: Any, attempt: Any,
        category: Any, stored_fp: Any, raw_result: Any) -> Optional[Dict[str, Any]]:
    """Validate only the single old pre-candidate invalid-summary failure."""
    if (status != "BLOCKED" or category != "deterministic"
            or type(failures) is not int or failures != 1
            or type(attempt) is not int or attempt != 1
            or not isinstance(stored_fp, str)
            or not re.fullmatch(r"[0-9a-f]{64}", stored_fp)):
        return None
    try:
        value = json.loads(raw_result or "{}")
    except (TypeError, ValueError):
        return None
    if (not isinstance(value, dict)
            or value.get("status") != "BLOCKED"
            or value.get("reason") != INVALID_SUMMARY_FORMAT_SOURCE_REASON
            or value.get("original_reason") != INVALID_SUMMARY_FORMAT_SOURCE_REASON
            or "candidate_path" not in value or value.get("candidate_path") is not None
            or type(value.get("model_calls")) is not int or value["model_calls"] != 1
            or value.get("format_repair_exhausted") is not False):
        return None
    # A textual result is not enough to prove pre-effect state; the caller
    # also checks canonical events and journals. Refuse any claimed effect.
    effect_fields = (
        "publication", "checkpoint", "event_id", "manifest", "manifest_sha256",
        "review", "ledger_path", "publication_event", "transaction",
    )
    if any(value.get(key) not in (None, "", {}, []) for key in effect_fields):
        return None
    project = value.get("project")
    pids, paths, hashes = (value.get(key) for key in ("proposal_ids", "paths", "hashes"))
    cycle_id = value.get("cycle_id")
    if (not isinstance(project, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,100}", project)
            or not isinstance(pids, list) or not isinstance(paths, list)
            or not isinstance(hashes, list)
            or not 1 <= len(pids) <= 8
            or len(pids) != len(paths) or len(paths) != len(hashes)):
        return None
    if (any(not isinstance(pid, str) or not re.fullmatch(r"prop-[A-Za-z0-9._-]+", pid)
            for pid in pids)
            or any(not _valid_path(path) for path in paths)
            or any(not isinstance(digest, str)
                   or not re.fullmatch(r"[0-9a-f]{64}", digest) for digest in hashes)
            or len(set(pids)) != len(pids) or len(set(paths)) != len(paths)
            or not isinstance(cycle_id, str) or not cycle_id.startswith("audit-new-")):
        return None
    from .pubstate import batch_key
    if batch != batch_key(project, paths) or value.get("batch") != batch:
        return None
    return {
        "batch": batch, "project": project, "proposal_ids": pids,
        "paths": paths, "hashes": hashes, "cycle_id": cycle_id,
        "reason": INVALID_SUMMARY_FORMAT_SOURCE_REASON,
        "fingerprint": stored_fp, "result": value,
        "pre_candidate_summary_format": True,
    }


def _valid_phase_resume_row(batch: Any, status: Any, failures: Any, attempt: Any,
                            category: Any, stored_fp: Any,
                            raw_result: Any) -> Optional[Dict[str, Any]]:
    """Validate only the single failed pre-effect phase-resume attempt."""
    if (status not in {"BLOCKED", "QUARANTINED"} or category != "deterministic"
            or type(failures) is not int or type(attempt) is not int
            or failures < 1 or attempt < 1):
        return None
    if not isinstance(stored_fp, str) or not re.fullmatch(r"[0-9a-f]{64}", stored_fp):
        return None
    try:
        value = json.loads(raw_result or "{}")
    except (TypeError, ValueError):
        return None
    if (not isinstance(value, dict) or value.get("status") != status
            or value.get("reason") not in {
                PHASE_RESUME_REASON, "phase_resume_terminal_no_replay",
                NO_INDEPENDENT_REVIEWER_PHASE_REASON,
            }
            or value.get("original_reason") != value.get("reason")):
        return None
    terminal_recovery = value["reason"] == "phase_resume_terminal_no_replay"
    if (value["reason"] == NO_INDEPENDENT_REVIEWER_PHASE_REASON
            and status != "BLOCKED"):
        return None
    if not terminal_recovery and (failures != 1 or attempt != 1):
        return None
    project = value.get("project")
    pids, paths, hashes = (value.get(key) for key in ("proposal_ids", "paths", "hashes"))
    cycle_id = value.get("cycle_id")
    if not isinstance(project, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,100}", project):
        return None
    if not isinstance(pids, list) or not isinstance(paths, list) or not isinstance(hashes, list):
        return None
    if not 1 <= len(pids) <= 8 or len(pids) != len(paths) or len(paths) != len(hashes):
        return None
    if (any(not isinstance(pid, str) or not re.fullmatch(r"prop-[A-Za-z0-9._-]+", pid) for pid in pids)
            or any(not _valid_path(path) for path in paths)
            or any(not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                   for digest in hashes)):
        return None
    if len(set(pids)) != len(pids) or len(set(paths)) != len(paths):
        return None
    if not isinstance(cycle_id, str) or not cycle_id:
        return None
    from .pubstate import batch_key
    ordinary_batch = batch == batch_key(project, paths)
    repaired_batch_candidate = (
        value.get("reason") == NO_INDEPENDENT_REVIEWER_PHASE_REASON
        and status == "BLOCKED"
        and isinstance(batch, str)
        and batch.startswith("repaired-")
    )
    if (not ordinary_batch and not repaired_batch_candidate) or value.get("batch") != batch:
        return None
    return {
        "batch": batch, "project": project, "proposal_ids": pids,
        "paths": paths, "hashes": hashes, "cycle_id": cycle_id,
        "fingerprint": stored_fp, "result": value,
        "terminal_recovery": terminal_recovery,
        "reason": value.get("reason"),
        "repaired_batch_candidate": repaired_batch_candidate,
    }


def _spent_format_retry_batch_matches(conn: sqlite3.Connection, batch: str,
                                      old: Dict[str, Any], lane: str,
                                      current_version: str) -> bool:
    """Bind a repaired phase row to the exact spent pre-candidate-format retry."""
    try:
        if (old.get("reason") != NO_INDEPENDENT_REVIEWER_PHASE_REASON
                or not old.get("repaired_batch_candidate")):
            return False
        cycle_row = conn.execute(
            "SELECT lane,status,counts FROM audit_cycles WHERE id=?",
            (old.get("cycle_id"),),
        ).fetchone()
        if cycle_row is None or cycle_row[0] != lane or cycle_row[1] not in {
                "ACTIVE", "DRAINED", "SCOPED_DRAINED"}:
            return False
        counts = json.loads(cycle_row[2] or "{}")
        retry_members = counts.get("repaired_retry") if isinstance(counts, dict) else None
        if (not isinstance(retry_members, list)
                or len(retry_members) != len(old.get("proposal_ids") or [])):
            return False
        expected = set(zip(old["proposal_ids"], old["paths"], old["hashes"]))
        if not expected or len(expected) != len(old["proposal_ids"]):
            return False
        if any(not isinstance(item, dict) for item in retry_members):
            return False
        retry_identities = {
            (item.get("proposal_id"), item.get("path"), item.get("sha256"))
            for item in retry_members
        }
        if retry_identities != expected or len(retry_identities) != len(retry_members):
            return False
        if any(item.get("reason") != INVALID_SUMMARY_FORMAT_RETRY_REASON
               or item.get("project") != old.get("project")
               or item.get("publication_sha256") != REPAIRED_PUBLICATION_SHA256
               or item.get("code_version") != current_version
               or not isinstance(item.get("previous_batch"), str)
               or not item.get("previous_batch")
               or not isinstance(item.get("helper_sha256"), str)
               or not re.fullmatch(r"[0-9a-f]{64}", item["helper_sha256"])
               or item.get("token") != retry_token(
                   REPAIRED_PUBLICATION_SHA256, old["project"],
                   item["proposal_id"], item["path"], item["sha256"],
                   reason=INVALID_SUMMARY_FORMAT_RETRY_REASON,
                   helper_sha256=item["helper_sha256"],
               ) for item in retry_members):
            return False
        cycle_members = conn.execute(
            "SELECT proposal_id,path,sha256,status FROM audit_members WHERE cycle_id=?",
            (old.get("cycle_id"),),
        ).fetchall()
        if (len(cycle_members) != len(expected)
                or {(row[0], row[1], row[2]) for row in cycle_members} != expected
                or any(row[3] != "failed" for row in cycle_members)):
            return False
        members = [
            {"proposal_id": pid, "path": path, "sha256": digest}
            for pid, path, digest in sorted(expected)
        ]
        if retry_batch_key(old["project"], members, retry_members) != batch:
            return False
        try:
            token_rows = conn.execute(
                "SELECT token,cycle_id,publication_sha256,code_version,proposal_id,path,sha256,previous_batch "
                "FROM repaired_retry_tokens WHERE cycle_id=?",
                (old.get("cycle_id"),),
            ).fetchall()
        except sqlite3.Error:
            return False
        stored = {row[0]: row[1:] for row in token_rows}
        if len(stored) != len(retry_members):
            return False
        for item in retry_members:
            if stored.get(item["token"]) != (
                    old["cycle_id"], item["publication_sha256"], item["code_version"],
                    item["proposal_id"], item["path"], item["sha256"],
                    item["previous_batch"]):
                return False
        return True
    except (KeyError, TypeError, ValueError, sqlite3.Error):
        return False


def _spent_format_retry_predecessor(conn: sqlite3.Connection, batch: str,
                                    old: Dict[str, Any], lane: str,
                                    current_version: str) -> Optional[Dict[str, Any]]:
    """Return only the exact original invalid-summary row behind a spent retry."""
    if not _spent_format_retry_batch_matches(conn, batch, old, lane, current_version):
        return None
    try:
        cycle = conn.execute(
            "SELECT counts FROM audit_cycles WHERE id=?", (old.get("cycle_id"),)
        ).fetchone()
        counts = json.loads(cycle[0] or "{}") if cycle else None
        retries = counts.get("repaired_retry") if isinstance(counts, dict) else None
        previous_batches = {item.get("previous_batch") for item in retries
                            if isinstance(item, dict)}
        if len(previous_batches) != 1:
            return None
        previous_batch = next(iter(previous_batches))
        row = conn.execute(
            "SELECT batch,status,failures,attempt,category,fingerprint,result "
            "FROM publications WHERE batch=?", (previous_batch,),
        ).fetchone()
        if row is None:
            return None
        previous = _valid_pre_candidate_summary_format_row(*row)
        if (previous is None or previous.get("project") != old.get("project")
                or set(zip(previous.get("proposal_ids", []), previous.get("paths", []),
                           previous.get("hashes", [])))
                != set(zip(old.get("proposal_ids", []), old.get("paths", []),
                           old.get("hashes", [])))):
            return None
        old_cycle = conn.execute(
            "SELECT lane FROM audit_cycles WHERE id=?", (previous.get("cycle_id"),)
        ).fetchone()
        old_members = conn.execute(
            "SELECT proposal_id,path,sha256,status FROM audit_members WHERE cycle_id=?",
            (previous.get("cycle_id"),),
        ).fetchall()
        expected = set(zip(previous["proposal_ids"], previous["paths"], previous["hashes"]))
        if old_cycle != (lane,) or not _failed_exact_source_members(old_members, expected):
            return None
        return previous
    except (KeyError, TypeError, ValueError, sqlite3.Error):
        return None


def _failed_exact_source_members(cycle_members: Sequence[Tuple[Any, ...]],
                                 expected: set[Tuple[str, str, str]]) -> bool:
    """Validate only the source row's exact members within a shared cycle."""
    if not expected:
        return False
    rows = [tuple(row) for row in cycle_members]
    for proposal_id, path, digest in expected:
        matches = [row for row in rows if row[0] == proposal_id or row[1] == path]
        if (len(matches) != 1 or matches[0][:3] != (proposal_id, path, digest)
                or matches[0][3] != "failed"):
            return False
    return True


def _spent_summary_failed_entry(conn: sqlite3.Connection, old: Dict[str, Any],
                                other_row: Tuple[Any, ...], lane: str,
                                current_version: str, current_helper: str,
                                publication_digest: str) -> bool:
    """Prove one terminal-loader BLOCKED row came from an already-spent summary retry.

    This exception only lets the normal exact summary retry supersede its own
    failed loader-entry record. It does not make the failed row retryable by
    itself, and it requires the prior helper token, retry cycle, member and
    publication row to agree byte-for-byte on the one target identity.
    """
    if (old.get("reason") != SUMMARY_REPAIR_SOURCE_REASON
            or len(old.get("proposal_ids") or []) != 1
            or len(other_row) != 7):
        return False
    batch, status, failures, attempt, category, stored_fp, raw_result = other_row
    if (status != "BLOCKED" or category != "deterministic"
            or type(failures) is not int or failures != 1
            or type(attempt) is not int or attempt != 1
            or not isinstance(stored_fp, str)
            or not re.fullmatch(r"[0-9a-f]{64}", stored_fp)):
        return False
    try:
        value = json.loads(raw_result or "{}")
        if not isinstance(value, dict):
            return False
        if (value.get("status") != "BLOCKED"
                or value.get("reason") != "phase_resume_terminal_no_replay"
                or value.get("original_reason") != "phase_resume_terminal_no_replay"
                or value.get("project") != old.get("project")
                or value.get("candidate_path") is not None
                or type(value.get("model_calls")) is not int
                or value.get("model_calls") != 1):
            return False
        effect_fields = (
            "publication", "checkpoint", "event_id", "manifest",
            "manifest_sha256", "ledger_path", "publication_event",
            "transaction", "review",
        )
        if any(value.get(key) not in (None, "", {}, []) for key in effect_fields):
            return False
        project = old["project"]
        identity = (old["proposal_ids"][0], old["paths"][0], old["hashes"][0])
        if ((value.get("proposal_ids"), value.get("paths"), value.get("hashes"))
                != ([identity[0]], [identity[1]], [identity[2]])
                or not isinstance(value.get("cycle_id"), str)
                or value.get("batch") != batch):
            return False
        from .pubstate import batch_key
        if batch not in (batch_key(project, [identity[1]]),) and not batch.startswith("repaired-"):
            return False
        cycle_id = value["cycle_id"]
        cycle = conn.execute(
            "SELECT lane,status,counts FROM audit_cycles WHERE id=?", (cycle_id,),
        ).fetchone()
        if cycle is None or cycle[0] != lane or cycle[1] != "DRAINED":
            return False
        counts = json.loads(cycle[2] or "{}")
        retries = counts.get("repaired_retry") if isinstance(counts, dict) else None
        if not isinstance(retries, list) or len(retries) != 1:
            return False
        item = retries[0]
        if (not isinstance(item, dict)
                or (item.get("proposal_id"), item.get("path"), item.get("sha256")) != identity
                or item.get("project") != project
                or item.get("reason") != SUMMARY_REPAIR_REASON
                or item.get("previous_batch") != old.get("batch")
                or item.get("publication_sha256") != publication_digest
                or item.get("code_version") != current_version):
            return False
        old_helper = item.get("helper_sha256")
        if (not isinstance(old_helper, str)
                or not re.fullmatch(r"[0-9a-f]{64}", old_helper)
                or old_helper == current_helper
                or item.get("token") != retry_token(
                    publication_digest, project, *identity,
                    reason=SUMMARY_REPAIR_REASON, helper_sha256=old_helper)):
            return False
        expected_batch = retry_batch_key(project, [{
            "proposal_id": identity[0], "path": identity[1], "sha256": identity[2],
        }], retries)
        if expected_batch != batch:
            return False
        members = conn.execute(
            "SELECT proposal_id,path,sha256,project,status FROM audit_members WHERE cycle_id=?",
            (cycle_id,),
        ).fetchall()
        if members != [(identity[0], identity[1], identity[2], project, "failed")]:
            return False
        token_row = conn.execute(
            "SELECT cycle_id,publication_sha256,code_version,proposal_id,path,sha256,previous_batch "
            "FROM repaired_retry_tokens WHERE token=?", (item["token"],),
        ).fetchone()
        return token_row == (
            cycle_id, publication_digest, current_version, identity[0],
            identity[1], identity[2], old["batch"],
        )
    except (KeyError, TypeError, ValueError, sqlite3.Error):
        return False


def retry_token(publication_digest: str, project: str, proposal_id: str,
                path: str, sha256: str, *, reason: str = REPAIRED_REASON,
                helper_sha256: Optional[str] = None) -> str:
    """Stable one-time token for the exact repaired code and member identity."""
    payload = ([reason, helper_sha256, publication_digest, project, proposal_id, path, sha256]
               if helper_sha256 is not None else
               [reason, publication_digest, project, proposal_id, path, sha256])
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _pre_candidate_format_helper_sha256(paths: Optional[Sequence[Path]]) -> Optional[str]:
    """Bind the retry to its gate, repair hook, and called curator helpers."""
    if not isinstance(paths, (list, tuple)) or len(paths) != 4:
        return None
    files = {}
    total = 0
    try:
        for raw_path in paths:
            path = Path(raw_path)
            if path.name not in {
                    "maintenance.py", "repaired_retry.py", "curate_once.py",
                    "draft_summary.py"}:
                return None
            if path.name in files or path.is_symlink() or path.is_junction():
                return None
            content = path.read_bytes()
            total += len(content)
            if total > 2 * 1024 * 1024:
                return None
            files[path.name] = content
    except (OSError, TypeError, ValueError):
        return None
    if set(files) != {
            "maintenance.py", "repaired_retry.py", "curate_once.py",
            "draft_summary.py"}:
        return None
    digest = hashlib.sha256(b"pre-candidate-invalid-summary-format-v1\0")
    for name in sorted(files):
        encoded_name = name.encode("ascii")
        content = files[name]
        digest.update(len(encoded_name).to_bytes(4, "big"))
        digest.update(encoded_name)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def phase_resume_retry_token(curate_digest: str, publication_digest: str,
                             project: str, proposal_id: str,
                             path: str, sha256: str) -> str:
    """One-time token bound to both exact fixed code files and failed identity."""
    payload = [PHASE_RESUME_REASON, curate_digest, publication_digest,
               project, proposal_id, path, sha256]
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def retry_batch_key(project: str, members: Sequence[Dict[str, Any]],
                    retry_members: Sequence[Dict[str, Any]]) -> str:
    """Use a separate stable row so the previous publication evidence survives."""
    identities = sorted(
        (member.get("proposal_id"), member.get("path"), member.get("sha256"))
        for member in members
    )
    tokens = sorted(str(item.get("token") or "") for item in retry_members)
    value = json.dumps(["repaired-retry-v1", project, identities, tokens],
                       ensure_ascii=True, separators=(",", ":"))
    return "repaired-" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def ensure_retry_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS repaired_retry_tokens("
        "token TEXT PRIMARY KEY, cycle_id TEXT NOT NULL, publication_sha256 TEXT NOT NULL, "
        "code_version TEXT NOT NULL, proposal_id TEXT NOT NULL, path TEXT NOT NULL, "
        "sha256 TEXT NOT NULL, previous_batch TEXT NOT NULL)"
    )


def _retry_table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (RETRY_TABLE,)
    ).fetchone()
    return row is not None


def _used_tokens(conn: sqlite3.Connection) -> Optional[set[str]]:
    if not _retry_table_exists(conn):
        return set()
    try:
        return {str(row[0]) for row in conn.execute(
            f"SELECT token FROM {RETRY_TABLE}"
        ).fetchall()}
    except sqlite3.Error:
        return None


def _terminal_owned_absence_result(vault: Path, old: dict, plan: dict) -> Optional[dict]:
    """Bind missing ownership only to a revalidated closed terminal generation.

    The historical row is never repaired. The returned copy is evidence for a
    second normal canonical_verdict call; uncertain canonical evidence still fails.
    """
    try:
        from .config import load_config
        from .terminal_generation import terminal_generation_plan, canonical_proposal_identities
        value = old['result']
        if (old.get('reason') != TERMINAL_GENERATION_SOURCE_REASON
                or not old.get('terminal_recovery') or not isinstance(plan, dict)
                or value.get('state_dir') not in (None, '')
                or value.get('run_path') not in (None, '')):
            return None
        phase = Path(plan['phase_state'])
        if not phase.is_absolute() or phase.name != 'curation':
            return None
        for part in (phase, *phase.parents):
            info = part.lstat()
            if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or getattr(info, 'st_file_attributes', 0) & 0x400):
                return None
        state = phase.parent
        config = state.parent / 'config.json'
        if config.is_symlink() or config.is_junction() or not config.is_file():
            return None
        cfg = load_config(config)
        frozen = plan['frozen']
        expected = canonical_proposal_identities(old['paths'], old['proposal_ids'], old['hashes'])
        proposals = frozen.get('proposals') or []
        actual = canonical_proposal_identities(
            [item.get('path') for item in proposals], [item.get('id') for item in proposals],
            [item.get('sha256') for item in proposals])
        if (not expected or actual != expected or len(proposals) != len(expected)
                or cfg.project_slug != old['project']
                or Path(cfg.state_dir).resolve() != state.resolve()
                or Path(cfg.hub.vault).resolve() != Path(vault).resolve()
                or Path(frozen['vault_path']).resolve() != Path(vault).resolve()
                or Path(frozen['attestation_state_dir']).resolve() != state.resolve()
                or state.resolve() != (Path(cfg.raw['maintenance_state_dir']) /
                                      'projects' / old['project'] / 'state').resolve()):
            return None
        fresh = terminal_generation_plan(cfg, sorted(old['paths']), frozen, value)
        keys = ('phase_state', 'pointer', 'record', 'progress', 'run_root', 'pins',
                'manifest_sha256', 'terminal_report_sha256', 'batch', 'cycle_id')
        if (fresh is None or fresh.get('batch') != old['batch']
                or fresh.get('cycle_id') != old['cycle_id']
                or any(fresh.get(key) != plan.get(key) for key in keys)):
            return None
        return {**value, 'state_dir': str(state.resolve())}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError):
        return None


def repaired_retry_candidates(
    conn: sqlite3.Connection,
    summary: Dict[str, Any],
    lane: str,
    project_allowed: Callable[[str], bool],
    vault: Path,
    *,
    source_preflight: Callable[[Dict[str, Any]], bool],
    publication_path: Optional[Path] = None,
    curate_path: Optional[Path] = None,
    phase_cache_preflight: Optional[Callable[[str, Sequence[Dict[str, Any]]], bool]] = None,
    summary_repair_preflight: Optional[Callable[[Dict[str, Any]], bool]] = None,
    summary_repair_path: Optional[Path] = None,
    inventory_history_preflight: Optional[Callable[[Dict[str, Any]], bool]] = None,
    inventory_history_path: Optional[Path] = None,
    pre_candidate_summary_format_preflight: Optional[Callable[[Dict[str, Any]], bool]] = None,
    pre_candidate_summary_format_paths: Optional[Sequence[Path]] = None,
    stale_pending_preflight: Optional[Callable[[Dict[str, Any], Tuple[str, str, str], str], bool]] = None,
    terminal_generation_preflight: Optional[Callable[[Dict[str, Any], Sequence[Dict[str, Any]]], Any]] = None,
    terminal_generation_path: Optional[Path] = None,
    terminal_generation_pending_preflight: Optional[Callable[[Dict[str, Any], Tuple[str, str, str], str], bool]] = None,
) -> List[Dict[str, Any]]:
    """Return exact independently eligible identities for the one supported fix.

    The function is read-only. Tokens are consumed atomically only when the
    caller opens the ordinary successor cycle.
    """
    digest = publication_sha256(publication_path)
    if digest != REPAIRED_PUBLICATION_SHA256:
        return []
    try:
        from .pubstate import canonical_verdict, code_version
        current_version = code_version()
    except Exception:
        return []
    if not isinstance(current_version, str) or not re.fullmatch(r"[0-9a-f]{32}", current_version):
        return []
    phase_digest = curate_sha256(curate_path) if phase_cache_preflight is not None else None
    phase_retry_enabled = (
        phase_cache_preflight is not None
        and current_version == PHASE_RESUME_CODE_VERSION
        and phase_digest == PHASE_RESUME_CURATE_SHA256
    )
    summary_helper_digest = None
    if summary_repair_preflight is not None and summary_repair_path is not None:
        try:
            helper_bytes = Path(summary_repair_path).read_bytes()
            if len(helper_bytes) <= 256 * 1024:
                summary_helper_digest = hashlib.sha256(helper_bytes).hexdigest()
        except OSError:
            pass
    inventory_helper_digest = None
    if inventory_history_preflight is not None and inventory_history_path is not None:
        try:
            helper_bytes = Path(inventory_history_path).read_bytes()
            if len(helper_bytes) <= 512 * 1024:
                inventory_helper_digest = hashlib.sha256(helper_bytes).hexdigest()
        except OSError:
            pass
    format_helper_digest = (
        _pre_candidate_format_helper_sha256(pre_candidate_summary_format_paths)
        if pre_candidate_summary_format_preflight is not None else None
    )
    terminal_generation_helper_digest = None
    if terminal_generation_preflight is not None and terminal_generation_path is not None:
        try:
            helper_bytes = Path(terminal_generation_path).read_bytes()
            if len(helper_bytes) <= 512 * 1024:
                terminal_generation_helper_digest = hashlib.sha256(helper_bytes).hexdigest()
        except OSError:
            pass
    used = _used_tokens(conn)
    if used is None:
        return []

    eligible_by_identity: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
    for meta in summary.get("eligible") or []:
        if not isinstance(meta, dict):
            continue
        identity = (meta.get("proposal_id"), meta.get("path"), meta.get("sha256"))
        if all(isinstance(value, str) and value for value in identity):
            eligible_by_identity.setdefault(identity, []).append(meta)

    try:
        publication_rows = conn.execute(
            "SELECT batch,status,failures,attempt,category,fingerprint,result FROM publications "
            "ORDER BY batch"
        ).fetchall()
    except sqlite3.Error:
        return []

    # An exact already-spent format retry may have produced a retained phase
    # row under its repaired batch key. Do not re-issue the old format token
    # when that proof is what authorizes phase continuation.
    spent_format_predecessors = set()
    for phase_row in publication_rows:
        phase_batch, phase_status, phase_failures, phase_attempt, phase_category, phase_fp, phase_raw = phase_row
        phase_old = _valid_phase_resume_row(
            phase_batch, phase_status, phase_failures, phase_attempt,
            phase_category, phase_fp, phase_raw,
        )
        if (phase_old is None or not phase_old.get("repaired_batch_candidate")
                or not _spent_format_retry_batch_matches(
                    conn, phase_batch, phase_old, lane, current_version,
                )):
            continue
        predecessor = _spent_format_retry_predecessor(
            conn, phase_batch, phase_old, lane, current_version,
        )
        if predecessor is not None:
            spent_format_predecessors.add(predecessor["batch"])

    admitted: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for row in publication_rows:
        batch, status, failures, attempt, category, stored_fp, raw_result = row
        phase_resume = False
        terminal_generation_retry = False
        terminal_generation_plan = None
        pre_candidate_format_retry = False
        if status == "BLOCKED" and format_helper_digest is not None:
            old = _valid_pre_candidate_summary_format_row(
                batch, status, failures, attempt, category, stored_fp, raw_result,
            )
            pre_candidate_format_retry = old is not None
            if old is not None and batch in spent_format_predecessors:
                continue
        elif status == "QUARANTINED":
            old = None
            if inventory_helper_digest is not None:
                old = _valid_inventory_history_row(
                    batch, status, failures, attempt, category, stored_fp, raw_result,
                )
            if old is None:
                old = _valid_batch_row(batch, status, failures, attempt, category, stored_fp, raw_result)
        else:
            old = None
        if old is None and phase_retry_enabled:
            phase_old = _valid_phase_resume_row(
                batch, status, failures, attempt, category, stored_fp, raw_result,
            )
            if phase_old is not None:
                if (phase_old.get("terminal_recovery")
                        and phase_old.get("reason") == TERMINAL_GENERATION_SOURCE_REASON):
                    if (lane != "legacy" or terminal_generation_helper_digest is None
                            or terminal_generation_preflight is None):
                        continue
                    exact_metas = [
                        (eligible_by_identity.get(identity) or [None])[0]
                        for identity in sorted(set(zip(
                            phase_old.get("proposal_ids") or [],
                            phase_old.get("paths") or [],
                            phase_old.get("hashes") or [],
                        )))
                    ]
                    if (len(exact_metas) != len(phase_old.get("proposal_ids") or [])
                            or any(not isinstance(meta, dict) for meta in exact_metas)):
                        continue
                    try:
                        terminal_generation_plan = terminal_generation_preflight(
                            phase_old, exact_metas,
                        )
                    except Exception:
                        terminal_generation_plan = None
                    if not isinstance(terminal_generation_plan, dict):
                        continue
                    phase_old["terminal_generation_retry"] = True
                    terminal_generation_retry = True
                if (phase_old.get("repaired_batch_candidate")
                        and not _spent_format_retry_batch_matches(
                            conn, batch, phase_old, lane, current_version,
                        )):
                    continue
                if phase_old.get("repaired_batch_candidate"):
                    predecessor = _spent_format_retry_predecessor(
                        conn, batch, phase_old, lane, current_version,
                    )
                    if (predecessor is None or canonical_verdict(vault, {
                            "batch": predecessor["batch"], "status": "BLOCKED",
                            "category": "deterministic",
                            "fingerprint": predecessor["fingerprint"],
                            "result": json.dumps(predecessor["result"], ensure_ascii=False),
                        })[0] != "absent"):
                        continue
                    if pre_candidate_summary_format_preflight is None:
                        continue
                    try:
                        if pre_candidate_summary_format_preflight(predecessor) is not True:
                            continue
                    except Exception:
                        continue
                    phase_old["format_retry_predecessor_batch"] = predecessor["batch"]
                old = phase_old
                phase_resume = True
        if old is None:
            continue
        if not project_allowed(old["project"]):
            continue
        # Old rows do not record a trustworthy publisher version. Keep that
        # unknown historical value untouched; only verify this exact repair.
        try:
            verdict_row = {
                "batch": batch, "status": status, "category": category,
                "fingerprint": stored_fp, "result": raw_result,
            }
            verdict, _evidence = canonical_verdict(vault, verdict_row)
            if (terminal_generation_retry and verdict == 'uncertain'
                    and _evidence.get('reason') == 'missing_owned_state_root'):
                owned = _terminal_owned_absence_result(vault, old, terminal_generation_plan)
                if owned is not None:
                    verdict, _evidence = canonical_verdict(
                        vault, {**verdict_row, 'result': json.dumps(owned, ensure_ascii=False)},
                    )
        except Exception:
            continue
        if verdict != "absent":
            continue

        summary_retry = old.get("reason") == SUMMARY_REPAIR_SOURCE_REASON
        inventory_history_retry = old.get("inventory_history_source") is True
        if pre_candidate_format_retry:
            if pre_candidate_summary_format_preflight is None:
                continue
            try:
                if pre_candidate_summary_format_preflight(old) is not True:
                    continue
            except Exception:
                continue
        if summary_retry:
            if (summary_helper_digest is None or summary_repair_preflight is None):
                continue
            policy = summary.get("policy") or {}
            max_batch = policy.get("max_batch", 8) if isinstance(policy, dict) else 8
            if (type(max_batch) is not int or max_batch < 1
                    or len(old["paths"]) > max_batch):
                continue
            try:
                if summary_repair_preflight(old) is not True:
                    continue
            except Exception:
                continue
        if inventory_history_retry:
            if (inventory_helper_digest is None or inventory_history_preflight is None):
                continue
            try:
                if inventory_history_preflight(old) is not True:
                    continue
            except Exception:
                continue

        cycle = conn.execute(
            "SELECT lane,status FROM audit_cycles WHERE id=?", (old["cycle_id"],)
        ).fetchone()
        if (cycle is None or (cycle[0] != lane
                and not (terminal_generation_retry and lane == "legacy"
                         and cycle == ("new", "DRAINED")))
                or (terminal_generation_retry and cycle[1] != "DRAINED")):
            continue
        previous_members = conn.execute(
            "SELECT proposal_id,path,sha256,status FROM audit_members WHERE cycle_id=?",
            (old["cycle_id"],),
        ).fetchall()
        expected_identity = set(zip(old["proposal_ids"], old["paths"], old["hashes"]))
        matched_members = [
            item for item in previous_members if (item[0], item[1], item[2]) in expected_identity
        ]
        matched_identity = {(item[0], item[1], item[2]) for item in matched_members}
        if (matched_identity != expected_identity
                or len(matched_members) != len(expected_identity)
                or any(item[3] not in (
                    {"failed"} if (phase_resume and not old.get("terminal_recovery"))
                    or pre_candidate_format_retry else {"failed", "quarantined"}
                )
                       for item in matched_members)):
            continue

        selected = []
        approved_stale_pending: Dict[Tuple[str, str, str], str] = {}
        for identity in sorted(expected_identity):
            matches = eligible_by_identity.get(identity) or []
            if len(matches) != 1:
                continue
            meta = matches[0]
            if meta.get("project") != old["project"] or not project_allowed(meta.get("project")):
                continue
            retry_reason = (
                TERMINAL_GENERATION_RETRY_REASON if terminal_generation_retry else
                SUMMARY_REPAIR_REASON if summary_retry else
                INVENTORY_HISTORY_RETRY_REASON if inventory_history_retry else
                INVALID_SUMMARY_FORMAT_RETRY_REASON if pre_candidate_format_retry else
                old.get("reason", REPAIRED_REASON)
            )
            token_helper_digest = (
                terminal_generation_helper_digest if terminal_generation_retry else
                summary_helper_digest if summary_retry else
                inventory_helper_digest if inventory_history_retry else
                format_helper_digest if pre_candidate_format_retry else None
            )
            token = (
                retry_token(
                    digest, old["project"], *identity, reason=retry_reason,
                    helper_sha256=token_helper_digest,
                ) if terminal_generation_retry else
                phase_resume_retry_token(phase_digest, digest, old["project"], *identity)
                if phase_resume else retry_token(
                    digest, old["project"], *identity, reason=retry_reason,
                    **({"helper_sha256": token_helper_digest}
                   if summary_retry or inventory_history_retry or pre_candidate_format_retry else {}),
                )
            )
            if token in used:
                continue
            try:
                source_safe = source_preflight(meta)
            except Exception:
                source_safe = False
            if not source_safe:
                continue
            selected.append((identity, meta, token))

            pending_rows = conn.execute(
                "SELECT m.cycle_id,c.lane,c.status,m.project,m.sha256,m.status "
                "FROM audit_members m JOIN audit_cycles c ON c.id=m.cycle_id "
                "WHERE c.lane=? AND m.proposal_id=? AND m.path=? AND m.status='pending'",
                (lane, identity[0], identity[1]),
            ).fetchall()
            if pending_rows:
                if terminal_generation_retry and terminal_generation_pending_preflight is not None:
                    try:
                        pending_ok = terminal_generation_pending_preflight(
                            old, identity, pending_rows[0][0] if len(pending_rows) == 1 else "",
                        ) is True
                    except Exception:
                        pending_ok = False
                    if (pending_ok and len(pending_rows) == 1
                            and pending_rows[0][1] == "legacy"
                            and pending_rows[0][2] == "ACTIVE"
                            and pending_rows[0][3] == old["project"]
                            and pending_rows[0][4] == identity[2]):
                        approved_stale_pending[identity] = pending_rows[0][0]
                        continue
                if len(pending_rows) != 1 or stale_pending_preflight is None:
                    selected.pop()
                    continue
                pending_cycle, pending_lane, pending_status, pending_project, pending_sha, _member_status = pending_rows[0]
                if (pending_lane != lane or pending_status != "ACTIVE"
                        or pending_project != old["project"] or pending_sha != identity[2]):
                    selected.pop()
                    continue
                try:
                    stale_ok = stale_pending_preflight(old, identity, pending_cycle)
                except Exception:
                    stale_ok = False
                if stale_ok is not True:
                    selected.pop()
                    continue
                approved_stale_pending[identity] = pending_cycle

        if phase_resume:
            # A prepared manifest binds the complete sorted proposal set and
            # current target preimage. Do not retry a convenient subset.
            if len(selected) != len(expected_identity):
                continue
            selected_metas = [item[1] for item in selected]
            if any(meta.get("project") != old["project"] for meta in selected_metas):
                continue
            if terminal_generation_retry:
                cache_ok = isinstance(terminal_generation_plan, dict)
            else:
                try:
                    phase_metas = [
                        {**meta, "_phase_resume_reason": old["result"].get("reason")}
                        for meta in selected_metas
                    ]
                    cache_ok = phase_cache_preflight(old["project"], phase_metas)
                except Exception:
                    cache_ok = False
            if not cache_ok:
                continue
        elif ((summary_retry or inventory_history_retry or pre_candidate_format_retry)
              and len(selected) != len(expected_identity)):
            # Both summary paths are all-or-nothing: one requires a retained
            # REPAIR manifest, the other exact hashes from a pre-candidate failure.
            continue

        # A batch may contain independently eligible members; preserve only
        # their exact, source-safe subset in the ordinary review cycle.
        for identity, meta, token in selected:
            if identity in admitted:
                continue
            history = conn.execute(
                "SELECT m.cycle_id,m.sha256,m.status FROM audit_members m "
                "JOIN audit_cycles c ON c.id=m.cycle_id "
                "WHERE c.lane=? AND m.proposal_id=? AND m.path=?",
                (lane, identity[0], identity[1]),
            ).fetchall()
            stale_cycle = approved_stale_pending.get(identity)
            if any(
                row[1] != identity[2]
                or (row[2] not in {"failed", "quarantined"}
                    and not (row[0] == stale_cycle and row[2] == "pending"))
                for row in history
            ):
                continue
            identity_rows = []
            for other_row in publication_rows:
                other_batch, _other_status, _other_failures, _other_attempt, _other_category, _other_fp, other_raw = other_row
                if (other_batch == old["batch"]
                        or other_batch == old.get("format_retry_predecessor_batch")):
                    continue
                try:
                    other = json.loads(other_raw or "{}")
                except (TypeError, ValueError):
                    other = {}
                if not isinstance(other, dict):
                    continue
                other_ids = other.get("proposal_ids")
                other_paths = other.get("paths")
                other_hashes = other.get("hashes")
                if (not isinstance(other_ids, list) or not isinstance(other_paths, list)
                        or not isinstance(other_hashes, list)
                        or len(other_ids) != len(other_paths)
                        or len(other_ids) != len(other_hashes)):
                    continue
                if any((pid, path) == identity[:2]
                       for pid, path in zip(other_ids, other_paths)):
                    identity_rows.append(other_row)
            unsafe_publication = bool(identity_rows)
            if (unsafe_publication and summary_retry and len(identity_rows) == 1
                    and summary_helper_digest is not None
                    and _spent_summary_failed_entry(
                        conn, old, identity_rows[0], lane, current_version,
                        summary_helper_digest, digest,
                    )):
                unsafe_publication = False
            if unsafe_publication:
                continue
            # Never let this exception override a pending duplicate identity.
            pending = conn.execute(
                "SELECT m.cycle_id FROM audit_members m JOIN audit_cycles c ON c.id=m.cycle_id "
                "WHERE c.lane=? AND m.proposal_id=? AND m.path=? AND m.status='pending'",
                (lane, identity[0], identity[1]),
            ).fetchall()
            if (pending and (len(pending) != 1
                             or approved_stale_pending.get(identity) != pending[0][0])):
                continue
            if terminal_generation_retry and terminal_generation_helper_digest is None:
                continue
            admitted[identity] = {
                "proposal_id": identity[0], "path": identity[1], "sha256": identity[2],
                "project": meta["project"], "token": token,
                "publication_sha256": digest, "code_version": current_version,
                "previous_fingerprint": old["fingerprint"],
                "previous_batch": old["batch"],
                "reason": (TERMINAL_GENERATION_RETRY_REASON if terminal_generation_retry else
                           PHASE_RESUME_REASON if phase_resume else retry_reason),
                **({"curate_sha256": phase_digest} if phase_resume and not terminal_generation_retry else {}),
                **({"helper_sha256": terminal_generation_helper_digest}
                   if terminal_generation_retry else {}),
                **({"helper_sha256": token_helper_digest}
                   if (summary_retry or inventory_history_retry or pre_candidate_format_retry
                       or terminal_generation_retry) else {}),
            }
    return [admitted[key] for key in sorted(admitted)]
