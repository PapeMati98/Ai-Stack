"""End-to-end pipeline orchestration.

Order:
1. Load and validate config (caller responsibility).
2. Open the durable store.
3. Begin a run; record the run id.
4. Read inputs.
5. Filter records (secrets, restricted, reasoning/tool dumps).
6. Hash and dedup against Hub inventory (canonical gateway).
7. Synthesize a draft (only if non-empty).
8. Decide: new | duplicate | merge | contradiction.
9. For ``new`` / ``merge``: emit a ``record-handoff`` through the gateway
   only after health, lint, and verify-context pass. Dirty worktrees fail
   closed (no Hub write).
10. Local outage: raise durable incident, preserve outbox, optionally try
    the subscription-only fallback.
11. No-input means zero model calls.
"""

from __future__ import annotations
from caretaker import tick_budget

import hashlib
import json
import os
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .config import CaretakerConfig, TransportSpec
from .filter import FilterOutcome, filter_records
from .hub import (BeginTaskResult, KnowledgeGateway,
                  KnowledgeTransportError, VerifyContextResult,
                  WikiExcerpt, summarize_inventory)
from .ingest import IngestStats, RawRecord, read_inputs
from .model import (DECISION_CONTRADICTION, DECISION_DUPLICATE, DECISION_MERGE,
                    DECISION_NEW, SynthesisDecision, SynthesisInput,
                    SynthesisRecord, WikiExcerpt, build_prompt, decide,
                    handoff_id, proposal_id)
from .office import (CanonicalOfficeArgvTransport, OfficeConnector,
                     OfficeDedupStore, OfficeIdentity, OfficeSendRequest,
                     OfficeTransport, RateLimiter)
from .store import Store, config_hash
from .transports import Envelope, OpenClawArgvTransport, Transport, \
    TransportError, build_run_config_inline


@dataclass(frozen=True)
class PipelineResult:
    run_id: str
    dry_run: bool
    records_read: int
    records_admitted: int
    proposals_new: int
    proposals_duplicate: int
    proposals_merge: int
    proposals_contradiction: int
    local_outage: bool
    fallback_used: bool
    incidents: Tuple[str, ...]
    notes: str = ""
    model_identity: str = ""


@dataclass(frozen=True)
class HandoffAttempt:
    """Outcome of one durable canonical ``record-handoff`` attempt."""

    receipt_verified: bool
    synthetic: bool = False
    note_failed: bool = False


@dataclass
class PipelineDeps:
    primary: Transport
    fallback: Optional[Transport]
    gateway: KnowledgeGateway
    office: OfficeConnector
    worktree_root: str = ""


def _write_run_config(spec: TransportSpec, state_dir: str, cwd: str,
                      run_id: str) -> str:
    inline = build_run_config_inline(spec, state_dir, cwd)
    out_dir = Path(state_dir) / "run-configs"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{run_id}.json"
    path.write_text(json.dumps(inline, indent=2, sort_keys=True), "utf-8")
    return str(path)


def _worktree_dirty(worktree_root: str) -> bool:
    """Return whether the requested worktree cannot be proven clean.

    The canonical gateway independently checks its own vault repository. This
    guard protects the caretaker candidate before it attempts a handoff and
    must therefore inspect the actual Git status rather than merely checking
    for a ``.git`` directory.
    """
    if not worktree_root:
        return False
    root = Path(worktree_root)
    git_marker = root / ".git"
    if not root.is_dir() or not git_marker.exists():
        # Not a git checkout: we cannot prove clean; fail closed.
        return True
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain",
             "--untracked-files=all"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True
    return result.returncode != 0 or bool(result.stdout.strip())


def _office_emit(connector: OfficeConnector, *, dedup_id: str, body: str,
                 kind: str, target: Optional[str],
                 reply_to: Optional[str]):
    request = OfficeSendRequest(
        dedup_id=dedup_id, body=body, kind=kind,
        to=target if target and not target.startswith("#") else None,
        channel=target if target and target.startswith("#") else None,
        reply_to=reply_to,
    )
    return connector.publish(request)


def _synth_records_from_outcomes(records: List[RawRecord],
                                 outcomes: List[FilterOutcome]
                                 ) -> List[SynthesisRecord]:
    out: List[SynthesisRecord] = []
    for idx, outcome in enumerate(outcomes):
        tick_budget.check('pipeline')
        if not outcome.accepted:
            continue
        if idx >= len(records):
            continue
        rec = records[idx]
        out.append(SynthesisRecord(
            record_index=idx,
            identity_tool=rec.identity_tool,
            native_session_id=rec.native_session_id,
            cursor=rec.cursor,
            content_hash=outcome.content_hash or "",
            title=rec.title,
            excerpt=outcome.excerpt or "",
            speaker=rec.speaker,
            observed_at=rec.observed_at,
            event_type=rec.event_type,
        ))
    return out


def _synth_records_from_admitted_events(store: Store
                                        ) -> List[SynthesisRecord]:
    """Rebuild candidates from accepted events committed before a crash.

    Cursor advancement and the sanitized event insert are one transaction,
    but queuing the event for synthesis happens later.  Reading this durable
    projection on every run closes the crash window between those steps,
    including a process failure with no explicit transport error.
    """
    out: List[SynthesisRecord] = []
    for event in store.admitted_events():
        tick_budget.check('pipeline')
        if not event.content_hash or not event.identity_tool \
                or not event.native_session_id or not event.cursor:
            continue
        out.append(SynthesisRecord(
            record_index=len(out),
            identity_tool=event.identity_tool,
            native_session_id=event.native_session_id,
            cursor=event.cursor,
            content_hash=event.content_hash,
            title=event.title,
            excerpt=event.excerpt,
            speaker=event.speaker,
            observed_at=event.observed_at,
            event_type=event.event_type,
        ))
    return out


def _load_synthesis_pending(store: Store, project_slug: str
                            ) -> Tuple[List[SynthesisRecord],
                                       List[str]]:
    """Rebuild ``SynthesisRecord`` instances from durable synthesis_pending rows.

    The rows are durably persisted as outbox ``synthesis_pending`` requests
    that contain the sanitized title, excerpt, content hash, and source ref.
    When a pre-synthesis outage occurs (model crash, transport failure,
    source file rotation between runs), the next run rebuilds the same
    records from the durable store so the queued fact is delivered once
    even if the original input has disappeared.

    Returns the rebuilt records (stable order: ``recorded_at`` ascending)
    and the dedup ids of the rows that produced them, so the pipeline can
    mark them drained after a successful handoff.
    """
    out: List[SynthesisRecord] = []
    dedup_ids: List[str] = []
    seen_hashes: set[str] = set()
    for row, request in store.outbox_synthesis_pending():
        tick_budget.check('pipeline')
        if request.get("project_slug") != project_slug:
            continue
        digest = str(request.get("content_hash") or "")
        if not digest or digest in seen_hashes:
            continue
        source_ref = str(request.get("source_ref") or "")
        tool = str(request.get("identity_tool") or "")
        session = str(request.get("native_session_id") or "")
        cursor = str(request.get("cursor") or "")
        if not (tool and session and cursor):
            parts = source_ref.split(":")
            if len(parts) < 3:
                continue
            tool = parts[0]
            session = ":".join(parts[1:-1])
            cursor = parts[-1]
        speaker = str(request.get("speaker") or "")
        if speaker not in ("user", "assistant", "owner_document"):
            speaker = "owner_document" if str(request.get("event_type") or "") == "owner_document.v1" else ""
        observed = str(request.get("observed_at") or "")[:64]
        event_type = str(request.get("event_type") or "")[:64]
        out.append(SynthesisRecord(
            record_index=len(out),
            identity_tool=tool,
            native_session_id=session,
            cursor=cursor,
            content_hash=digest,
            title=str(request.get("title") or ""),
            excerpt=str(request.get("excerpt") or ""),
            speaker=speaker,
            observed_at=observed,
            event_type=event_type,
        ))
        seen_hashes.add(digest)
        dedup_ids.append(row.dedup_id)
    return out, dedup_ids


def _wiki_pages_for_inventory(inventory_items: List[InventoryItem],
                              limit: int = 8) -> List[str]:
    """Return bounded, distinct wiki pages referenced by the inventory.

    Inventory is metadata-only, but its ``page`` field points at the same
    reviewed/verified wiki files the synthesis prompt wants as evidence.
    Capping at ``limit`` keeps the prompt from chasing every inventory row.
    """
    pages: List[str] = []
    seen: set[str] = set()
    for item in inventory_items:
        tick_budget.check('pipeline')
        if not item.page:
            continue
        if item.page in seen:
            continue
        seen.add(item.page)
        pages.append(item.page)
        if len(pages) >= limit:
            break
    return pages


def _evidence_supports_decision(decision: SynthesisDecision,
                                wiki_pages: Sequence[str]) -> bool:
    """Decide whether a duplicate/merge verdict is backed by evidence.

    The pipeline accepts ``duplicate`` and ``merge`` verdicts only when the
    model named at least one wiki page that the gateway actually returned
    as a reviewed/verified excerpt.  This prevents the model from
    discarding all input by claiming a semantic duplicate without naming
    any actual evidence.
    """
    if decision.decision not in {DECISION_DUPLICATE, DECISION_MERGE}:
        return True
    if not decision.evidence_pages:
        return False
    available = set(wiki_pages)
    return any(page in available for page in decision.evidence_pages)


def _synthesis_pending_dedup_ids_for_hashes(store: Store,
                                             project_slug: str,
                                             hashes: Sequence[str]
                                             ) -> List[str]:
    """Find ``synthesis_pending`` dedup_ids whose content_hash contributed.

    Used to drain sanitized queued facts after a successful handoff so
    they are not re-synthesized on the next run.
    """
    if not hashes:
        return []
    target_hashes = {h for h in hashes if isinstance(h, str) and h}
    if not target_hashes:
        return []
    out: List[str] = []
    for row, request in store.outbox_pending_requests(
            kind="synthesis_pending"):
        tick_budget.check('pipeline')
        if request.get("project_slug") != project_slug:
            continue
        digest = request.get("content_hash")
        if isinstance(digest, str) and digest in target_hashes:
            out.append(row.dedup_id)
    return out


def _record_key(record: SynthesisRecord) -> Tuple[str, ...]:
    if record.content_hash:
        return ("hash", record.content_hash)
    return ("ref", record.identity_tool, record.native_session_id,
            record.cursor)


def _merge_records(*groups: List[SynthesisRecord]) -> List[SynthesisRecord]:
    """Merge durable and fresh records once, keeping the first source.

    Pending outbox rows have priority, followed by accepted event rows and
    finally the current input.  A content hash is the stable join key; the
    provenance tuple is the fallback for a malformed legacy row.
    """
    out: List[SynthesisRecord] = []
    seen: set[Tuple[str, ...]] = set()
    for group in groups:
        tick_budget.check('pipeline')
        for record in group:
            tick_budget.check('pipeline')
            key = _record_key(record)
            if key in seen:
                continue
            seen.add(key)
            out.append(record)
    return out


def _merge_with_pending(fresh: List[SynthesisRecord],
                        pending: List[SynthesisRecord]
                        ) -> List[SynthesisRecord]:
    """Return pending records plus any fresh records that have new hashes.

    ``pending`` records are sanitized queued facts that survive a
    pre-synthesis outage and must be delivered once.  Fresh records that
    share a content hash with a pending row are dropped to avoid double
    publication; the remaining fresh records are appended afterwards.
    """
    return _merge_records(pending, fresh)


def _queue_pending_records(store: Store, config: CaretakerConfig,
                           records: Iterable[SynthesisRecord],
                           *, reason: str) -> None:
    """Persist accepted records when a later stage cannot safely continue.

    Cursors are intentionally durable before model/Hub work, so a transport
    outage must leave enough redacted provenance in SQLite for a subsequent
    operator or retry path. Never overwrite a successfully recorded row with
    an offline status.
    """
    recorded_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for sr in records:
        tick_budget.check('pipeline')
        digest = sr.content_hash
        pid = proposal_id(content_hash=digest, decision=DECISION_NEW,
                          merge_target=None)
        existing = store.outbox_get(pid)
        if existing and existing.status in {"recorded", "deduplicated"}:
            continue
        store.outbox_put(
            dedup_id=pid, payload_hash=digest, status="offline",
            recorded_at=recorded_at,
            request={
                "kind": "synthesis_pending",
                "project_slug": config.project_slug,
                "agent": config.agent,
                "session_id": config.session_id,
                "content_hash": digest,
                "content_hashes": [digest],
                "source_ref": (f"{sr.identity_tool}:{sr.native_session_id}:"
                                f"{sr.cursor}"),
                "identity_tool": sr.identity_tool,
                "native_session_id": sr.native_session_id,
                "cursor": sr.cursor,
                "title": sr.title,
                "excerpt": sr.excerpt,
                "speaker": sr.speaker,
                "observed_at": sr.observed_at,
                "event_type": sr.event_type,
            },
            reason=reason,
        )


def _write_handoff_note(state_dir: str, proposal: SynthesisDecision,
                        proposal_id_value: str, *, document_mode: bool = False) -> str:
    """Write a bounded redacted source package in the run-owned state dir."""
    from .filter import contains_secret, has_reasoning_marker, \
        looks_like_raw_tool_dump

    synthesis = proposal.synthesis.strip()
    rationale = (proposal.rationale or "").strip()
    if document_mode:
        from .privacy import contains_restricted_identifier
        if (contains_restricted_identifier(synthesis)
                or contains_restricted_identifier(rationale)):
            raise ValueError("restricted folder synthesis refused for handoff")
    if (contains_secret(synthesis) or contains_secret(rationale)
            or has_reasoning_marker(synthesis)
            or has_reasoning_marker(rationale)
            or looks_like_raw_tool_dump(synthesis)
            or looks_like_raw_tool_dump(rationale)):
        raise ValueError("unsafe model text refused for handoff")
    # Keep the source package byte-stable across retries even if the model
    # returns citations in a different order.
    refs = "\n".join(f"- {ref}" for ref in sorted(set(proposal.source_refs)))
    text = (
        "# Hub caretaker handoff\n\n"
        "This bounded note contains a model synthesis and source references; "
        "it is not a transcript.\n\n"
        f"Proposal key: {proposal_id_value}\n"
        f"Decision: {proposal.decision}\n\n"
        "## Synthesis\n\n"
        f"{synthesis}\n\n"
        "## Source references\n\n"
        f"{refs}\n"
    )
    path = Path(state_dir) / "handoff-notes" / f"{proposal_id_value}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, "utf-8")
    return str(path)


def _stable_source_refs(refs: Iterable[str]) -> List[str]:
    """Return bounded, unique source refs in a deterministic order."""
    return sorted({ref for ref in refs
                   if isinstance(ref, str) and ref.strip()
                   and "\0" not in ref})


def _receipt_metadata(result: Any) -> Optional[Dict[str, Any]]:
    """Validate the small receipt projection before acknowledging a write."""
    if result is None or getattr(result, "synthetic", False):
        return None
    if getattr(result, "status", None) not in {"recorded", "deduplicated"}:
        return None
    receipt_id = getattr(result, "receipt_id", None)
    source_id = getattr(result, "source_id", None)
    proposal_id_value = getattr(result, "proposal_id", None)
    receipt_status = getattr(result, "receipt_status", None)
    event_ids = getattr(result, "event_ids", ())
    if (receipt_status != "COMPLETE"
            or not isinstance(receipt_id, str) or not receipt_id.strip()
            or not isinstance(source_id, str) or not source_id.strip()
            or not isinstance(proposal_id_value, str)
            or not proposal_id_value.strip()
            or not isinstance(event_ids, (list, tuple))
            or not event_ids
            or any(not isinstance(item, str) or not item.strip()
                   for item in event_ids)):
        return None
    checkpoint_status = getattr(result, "checkpoint_status", None)
    if checkpoint_status is not None and not isinstance(checkpoint_status, str):
        checkpoint_status = None
    return {
        "receipt_status": receipt_status,
        "source_id": source_id,
        "proposal_id": proposal_id_value,
        "event_ids": list(event_ids),
        "checkpoint_status": checkpoint_status,
    }


def _safe_handoff_request(state_dir: str, request: Dict[str, Any]
                          ) -> Optional[Dict[str, Any]]:
    """Validate only caretaker-owned, redacted fields before retrying."""
    if request.get("kind") != "handoff":
        return None
    required_text = ("project_slug", "agent", "session_id", "proposal_id",
                     "path", "title", "state_summary", "target_page",
                     "classification")
    if any(not isinstance(request.get(key), str)
           or not request[key].strip() or "\0" in request[key]
           for key in required_text):
        return None
    if request.get("classification") not in {"internal", "public"}:
        return None
    refs = request.get("source_refs")
    hashes = request.get("content_hashes")
    if (not isinstance(refs, list) or not refs
            or any(not isinstance(ref, str) or not ref.strip()
                   or "\0" in ref for ref in refs)
            or not isinstance(hashes, list) or not hashes
            or any(not isinstance(value, str) or not value.strip()
                   or "\0" in value for value in hashes)):
        return None
    target_page = _target_page_shape(request["target_page"])
    if target_page is None:
        return None
    try:
        root = Path(state_dir).resolve()
        path = Path(request["path"]).resolve()
        path.relative_to(root)
    except (OSError, ValueError):
        return None
    if not path.is_file() or "handoff-notes" not in path.parts:
        return None
    # The retry projection intentionally excludes excerpt/raw/model text.
    return {
        "project_slug": request["project_slug"],
        "agent": request["agent"],
        "session_id": request["session_id"],
        "proposal_id": request["proposal_id"],
        "path": str(path),
        "title": request["title"][:512],
        "state_summary": request["state_summary"][:8192],
        "target_page": target_page,
        "classification": request["classification"],
        "source_refs": _stable_source_refs(refs),
        "content_hashes": sorted(set(hashes)),
    }


def _target_page_shape(value: Any) -> Optional[str]:
    """Validate a relative canonical page path without touching the vault."""
    if not isinstance(value, str):
        return None
    target = value.strip().replace("\\", "/")
    if (not target or len(target) > 512 or "\0" in target
            or target.startswith("/")
            or (len(target) >= 2 and target[1] == ":")):
        return None
    parts = target.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        return None
    return target


def _validated_merge_target(target: Any, inventory_items: List[Any],
                            wiki_excerpts: List[WikiExcerpt]
                            ) -> Optional[str]:
    """Accept only an inventory page with a returned wiki evidence excerpt."""
    normalized = _target_page_shape(target)
    if normalized is None:
        return None
    inventory_pages = {
        page for item in inventory_items
        for page in [_target_page_shape(getattr(item, "page", None))]
        if page is not None
    }
    evidence_pages = {
        page for excerpt in wiki_excerpts
        for page in [_target_page_shape(excerpt.page)]
        if page is not None
    }
    if normalized not in inventory_pages or normalized not in evidence_pages:
        return None
    return normalized


def _submit_handoff(config: CaretakerConfig, deps: PipelineDeps,
                    store: Store, *, run_id: str, state_dir: str,
                    decision: SynthesisDecision,
                    selected_records: List[SynthesisRecord],
                    cited_hashes: List[str], group_hash: str,
                    proposal_key: str, target_page: str,
                    context_receipt: Optional[str]) -> HandoffAttempt:
    """Reserve, submit, validate, and reconcile a canonical handoff.

    ``new`` and evidence-backed ``merge`` use exactly this path.  The note
    and outbox reservation are local durable state; only a complete canonical
    receipt publishes hashes and drains older synthesis-pending rows.
    """
    stable_refs = _stable_source_refs(decision.source_refs)
    title = f"Hub caretaker proposal {proposal_key[:12]}"
    try:
        note_path = _write_handoff_note(
            state_dir, decision, proposal_key,
            document_mode=any(record.speaker == "owner_document"
                              for record in selected_records),
        )
    except tick_budget.TickBudgetExceeded:
        raise
    except Exception:
        # No remote write was attempted.  Keep the selected sources available
        # for a later model/canonical retry.
        _queue_pending_records(store, config, selected_records,
                               reason="handoff_note_failed")
        return HandoffAttempt(receipt_verified=False, note_failed=True)

    handoff_id_value = handoff_id(proposal_id=proposal_key,
                                  source_refs=stable_refs)
    handoff_request = {
        "kind": "handoff",
        "project_slug": config.project_slug,
        "agent": config.agent,
        "session_id": config.session_id,
        "proposal_id": proposal_key,
        "source_refs": stable_refs,
        "content_hashes": sorted(set(cited_hashes)),
        "path": note_path,
        "title": title,
        "state_summary": decision.synthesis,
        "target_page": target_page,
        "classification": "internal",
        "receipt": None,
    }
    reserved_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    # The reservation is committed before crossing the Hub boundary.  A
    # crash after remote success can therefore replay the same request.
    store.outbox_put(
        dedup_id=handoff_id_value, payload_hash=group_hash,
        status="pending", receipt_id=None, reason="handoff_reserved",
        recorded_at=reserved_at, request=handoff_request,
    )

    result = None
    try:
        result = deps.gateway.record_handoff(
            project_slug=config.project_slug,
            path=note_path,
            title=title,
            agent=config.agent,
            state_summary=decision.synthesis,
            source_refs=stable_refs,
            target_page=target_page,
            classification="internal",
            context_receipt=context_receipt,
            session_id=config.session_id,
        )
    except tick_budget.TickBudgetExceeded:
        raise
    except Exception:
        # Keep only a caretaker-owned reason; gateway error text can contain
        # credentials or arbitrary wrapper output.
        result = None

    receipt_metadata = _receipt_metadata(result)
    synthetic_result = bool(result is not None and result.synthetic)
    if synthetic_result:
        outbox_status = "offline"
        result_reason = "synthetic_gateway_result"
    elif receipt_metadata is not None:
        outbox_status = result.status
        result_reason = None
    else:
        outbox_status = "offline"
        result_reason = "handoff_failed:gateway_offline"

    if receipt_metadata is not None:
        receipt_id = str(result.receipt_id)
        completed_request = dict(handoff_request)
        completed_request["receipt"] = receipt_metadata
        store.outbox_put(
            dedup_id=handoff_id_value, payload_hash=group_hash,
            status=outbox_status, receipt_id=receipt_id, reason=None,
            recorded_at=reserved_at, request=completed_request,
        )
        store.reconcile_outbox(
            content_hashes=cited_hashes, proposal_id=proposal_key,
            status=outbox_status, receipt_id=receipt_id,
            receipt=receipt_metadata,
        )
        for digest in cited_hashes:
            tick_budget.check('pipeline')
            store.publish_hash(digest, str(result.proposal_id))
        drained = store.outbox_drain(
            _synthesis_pending_dedup_ids_for_hashes(
                store, config.project_slug, cited_hashes,
            ),
        )
        if drained:
            store.checkpoint_set("synthesis_drained", run_id,
                                 {"drained": drained})
        return HandoffAttempt(receipt_verified=True)

    # The reserved row survives incomplete, offline, and synthetic results.
    store.outbox_put(
        dedup_id=handoff_id_value, payload_hash=group_hash,
        status="offline", receipt_id=None, reason=result_reason,
        recorded_at=reserved_at, request=handoff_request,
    )
    return HandoffAttempt(receipt_verified=False,
                          synthetic=synthetic_result)


def _retry_pending_handoffs(config: CaretakerConfig, deps: PipelineDeps,
                            store: Store, *, context_receipt: str,
                            dry_run: bool = False) -> int:
    """Retry reserved handoffs with their original stable request shape.

    The Hub call is made only for a row that was durably reserved in a prior
    run.  A verified receipt updates that row and reconciles the older
    per-record admission rows, which may have a different local dedup id.
    """
    if dry_run:
        return 0
    drained = 0
    for row, raw_request in store.outbox_pending_requests(kind="handoff"):
        tick_budget.check('pipeline')
        request = _safe_handoff_request(config.state_dir, raw_request)
        if request is None:
            continue
        try:
            result = deps.gateway.record_handoff(
                project_slug=request["project_slug"],
                path=request["path"],
                title=request["title"],
                agent=request["agent"],
                state_summary=request["state_summary"],
                source_refs=request["source_refs"],
                target_page=request["target_page"],
                classification=request["classification"],
                context_receipt=context_receipt or None,
                session_id=request["session_id"],
            )
        except tick_budget.TickBudgetExceeded:
            raise
        except Exception:
            # The row remains pending/offline for a later bounded retry.
            continue
        metadata = _receipt_metadata(result)
        if metadata is None:
            continue
        receipt_id = str(result.receipt_id)
        updated = dict(raw_request)
        updated.update(request)
        updated["receipt"] = metadata
        # This update is intentionally after the Hub call.  If the process
        # crashes here, the durable pending row is replayed with the same
        # note/path/request and the canonical gateway can deduplicate it.
        store.outbox_put(
            dedup_id=row.dedup_id,
            payload_hash=row.payload_hash,
            status=result.status,
            receipt_id=receipt_id,
            reason=None,
            recorded_at=row.recorded_at,
            request=updated,
        )
        store.reconcile_outbox(
            content_hashes=request["content_hashes"],
            proposal_id=request["proposal_id"],
            status=result.status,
            receipt_id=receipt_id,
            receipt=metadata,
        )
        for digest in request["content_hashes"]:
            tick_budget.check('pipeline')
            store.publish_hash(digest, str(result.proposal_id))
        drained += 1
    return drained


def run_pipeline(config: CaretakerConfig, deps: PipelineDeps, *,
                 store: Store, dry_run: bool = False,
                 max_prompt_bytes: Optional[int] = None) -> PipelineResult:
    run_id = uuid.uuid4().hex
    state_dir = config.state_dir
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    store.begin_run(run_id, config_hash(config.raw), dry_run=dry_run)

    incidents: List[str] = []
    fallback_used = False
    local_outage = False
    model_identity = ""

    # 1. Pre-flight: Hub health / lint / context. The dirty-tree guard is
    #    a hard precondition. Failures short-circuit to durable incidents.
    health = deps.gateway.health()
    lint = deps.gateway.lint()
    if health.status != config.hub.min_health:
        msg = (f"hub health {health.status} < min {config.hub.min_health}")
        store.raise_incident(run_id=run_id, kind="hub_health",
                             message=msg, cooldown_seconds=300)
        incidents.append("hub_health")
        store.finish_run(run_id)
        return PipelineResult(run_id=run_id, dry_run=dry_run, records_read=0,
                              records_admitted=0, proposals_new=0,
                              proposals_duplicate=0, proposals_merge=0,
                              proposals_contradiction=0, local_outage=False,
                              fallback_used=False, incidents=tuple(incidents))
    if lint.status != config.hub.min_lint:
        msg = (f"hub lint {lint.status} < min {config.hub.min_lint}")
        store.raise_incident(run_id=run_id, kind="hub_lint", message=msg,
                             cooldown_seconds=300)
        incidents.append("hub_lint")
        store.finish_run(run_id)
        return PipelineResult(run_id=run_id, dry_run=dry_run, records_read=0,
                              records_admitted=0, proposals_new=0,
                              proposals_duplicate=0, proposals_merge=0,
                              proposals_contradiction=0, local_outage=False,
                              fallback_used=False, incidents=tuple(incidents))
    if _worktree_dirty(deps.worktree_root):
        msg = f"worktree dirty or unverified: {deps.worktree_root}"
        store.raise_incident(run_id=run_id, kind="dirty_worktree",
                             message=msg, cooldown_seconds=300)
        incidents.append("dirty_worktree")
        store.finish_run(run_id)
        return PipelineResult(run_id=run_id, dry_run=dry_run, records_read=0,
                              records_admitted=0, proposals_new=0,
                              proposals_duplicate=0, proposals_merge=0,
                              proposals_contradiction=0, local_outage=False,
                              fallback_used=False, incidents=tuple(incidents))

    # 2. Read inputs.
    triples: List[Tuple[str, str, str]] = [
        (i.path, i.kind, i.tool) for i in config.inputs
    ]
    records, unsupported, stats = read_inputs(triples)
    for marker in unsupported:
        tick_budget.check('pipeline')
        store.raise_incident(run_id=run_id, kind="unsupported_input",
                             message=marker)

    # 3. Filter.
    outcomes = filter_records(
        records,
        allowlist_tools=tuple(config.allowlist.tools),
        max_body_bytes=config.allowlist.max_body_bytes,
        max_excerpt_bytes=config.allowlist.max_excerpt_bytes,
    )
    admitted_indices = [o for o in outcomes if o.accepted]
    records_admitted = len(admitted_indices)

    # Persist the sanitized event first, then advance the cursor.  The
    # event row carries the redacted payload provenance so the row stays
    # durable even if a crash aborts before the cursor upsert completes;
    # ``cursor_seen`` checks the join so a later run still recognises the
    # event.  Keeping both writes in one transaction closes the
    # multi-process check/insert race for new (tool, session, cursor)
    # triples.
    for outcome, rec in zip(outcomes, records):
        tick_budget.check('pipeline')
        if not outcome.accepted:
            continue
        with store.transaction():
            if store.cursor_seen(tool=rec.identity_tool,
                                 native_session_id=rec.native_session_id,
                                 cursor=rec.cursor):
                continue
            store.insert_event(
                run_id=run_id,
                identity_tool=rec.identity_tool,
                source=rec.source,
                native_session_id=rec.native_session_id,
                cursor=rec.cursor,
                observed_at=rec.observed_at,
                title=rec.title,
                excerpt=outcome.excerpt or "",
                content_hash=outcome.content_hash or "",
                filter_reason=outcome.reason,
                speaker=rec.speaker,
                event_type=rec.event_type,
            )
            store.upsert_cursor(
                tool=rec.identity_tool,
                native_session_id=rec.native_session_id,
                cursor=rec.cursor,
                observed_at=rec.observed_at,
                content_hash=outcome.content_hash,
            )

    # Build the redacted handoff candidates before any canonical read. If the
    # gateway transport fails, these records must be retained in the durable
    # outbox because their cursors were already committed above.
    synth_records = _synth_records_from_outcomes(records, outcomes)
    pending_handoffs_exist = bool(
        store.outbox_pending_requests(kind="handoff")
    )

    # Load sanitized queued facts from the durable store.  After a pre-
    # synthesis interruption, the original input may have rotated or
    # disappeared; the rebuilt records still carry their title, excerpt,
    # and source ref so synthesis can proceed without re-reading.  This
    # happens before the first early-exit so a queued fact is delivered
    # even when the fresh input file is now empty.
    pending_records, _pending_dedup_ids = _load_synthesis_pending(
        store, config.project_slug,
    )
    pending_hashes = {r.content_hash for r in pending_records
                      if r.content_hash}
    if pending_records:
        store.checkpoint_set("synthesis_pending_loaded", run_id, {
            "count": len(pending_records),
            "hashes": sorted(pending_hashes)[:32],
        })

    # Rebuild accepted events as a second durable recovery source.  This is
    # required even when no transport error was raised: a process can crash
    # after the event/cursor transaction commits and before
    # ``_queue_pending_records`` runs.  Pending outbox rows take priority,
    # then the event projection, then fresh input; hashes prevent replay
    # duplication across those sources.
    recovered_events = _synth_records_from_admitted_events(store)
    if recovered_events:
        store.checkpoint_set("admitted_events_loaded", run_id, {
            "count": len(recovered_events),
            "hashes": sorted({r.content_hash for r in recovered_events
                               if r.content_hash})[:32],
        })
    synth_records = _merge_records(
        pending_records, recovered_events, synth_records,
    )

    # No-input still means zero model calls.  A prior reserved handoff, a
    # queued synthesis_pending record, or an admitted-event projection is
    # the reason an empty run proceeds to canonical read/context so it can
    # retry durable work without inventing synthesis input.
    if (records_admitted == 0 and not pending_handoffs_exist
            and not pending_records and not recovered_events):
        store.finish_run(run_id)
        return PipelineResult(
            run_id=run_id, dry_run=dry_run, records_read=len(records),
            records_admitted=0, proposals_new=0, proposals_duplicate=0,
            proposals_merge=0, proposals_contradiction=0, local_outage=False,
            fallback_used=False, incidents=tuple(incidents),
            notes="no_input",
        )

    # 5. Compare against Hub inventory (canonical gateway).
    try:
        begin = deps.gateway.begin_task(
            project_slug=config.project_slug, agent=config.agent,
            session_id=config.session_id,
            require_context=config.hub.require_context,
            limit=config.limits.max_records_per_run,
        )
        inventory = deps.gateway.inventory(
            project_slug=config.project_slug, query=None,
            limit=config.limits.max_records_per_run,
        )
    except tick_budget.TickBudgetExceeded:
        raise
    except Exception as exc:
        # Do not persist gateway stderr or arbitrary exception text: it may
        # contain paths, prompts, or credentials from a failed wrapper.
        _queue_pending_records(store, config, synth_records,
                               reason="hub_preflight_failed")
        store.raise_incident(
            run_id=run_id, kind="hub_offline",
            message=f"canonical_preflight_failed:{type(exc).__name__}",
            cooldown_seconds=config.limits.incident_cooldown_seconds,
        )
        incidents.append("hub_offline")
        store.finish_run(run_id)
        return PipelineResult(
            run_id=run_id, dry_run=dry_run, records_read=len(records),
            records_admitted=records_admitted, proposals_new=0,
            proposals_duplicate=0, proposals_merge=0,
            proposals_contradiction=0, local_outage=False,
            fallback_used=False, incidents=tuple(incidents),
            notes="hub_preflight_failed",
        )

    if config.hub.require_context:
        try:
            verified = deps.gateway.verify_context(
                context_receipt=begin.receipt_path,
                project_slug=config.project_slug,
                agent=config.agent, session_id=config.session_id,
            )
        except tick_budget.TickBudgetExceeded:
            raise
        except Exception as exc:
            _queue_pending_records(store, config, synth_records,
                                   reason="context_verification_failed")
            store.raise_incident(
                run_id=run_id, kind="context_stale",
                message=f"context_verification_failed:{type(exc).__name__}",
                cooldown_seconds=config.limits.incident_cooldown_seconds,
            )
            incidents.append("context_stale")
            store.finish_run(run_id)
            return PipelineResult(
                run_id=run_id, dry_run=dry_run, records_read=len(records),
                records_admitted=records_admitted, proposals_new=0,
                proposals_duplicate=0, proposals_merge=0,
                proposals_contradiction=0, local_outage=False,
                fallback_used=False, incidents=tuple(incidents),
                notes="context_verification_failed",
            )
        if verified.status != "ok":
            _queue_pending_records(store, config, synth_records,
                                   reason="context_not_verified")
            store.raise_incident(run_id=run_id, kind="context_stale",
                                 message=verified.status)
            incidents.append("context_stale")
            store.finish_run(run_id)
            return PipelineResult(
                run_id=run_id, dry_run=dry_run, records_read=len(records),
                records_admitted=records_admitted, proposals_new=0,
                proposals_duplicate=0, proposals_merge=0,
                proposals_contradiction=0, local_outage=False,
                fallback_used=False, incidents=tuple(incidents),
            )

    # Retry a previously reserved handoff before considering new model work.
    # This path is also used by an empty input run and never invokes a model.
    pending_drained = _retry_pending_handoffs(
        config, deps, store, context_receipt=begin.receipt_path,
        dry_run=dry_run,
    )

    # 6. Identify exact duplicates via inventory, existing proposals, and the
    #    durable outbox. A previously recorded handoff for the same content
    #    hash means the record was already published; treat it as a duplicate
    #    so the retry path does not republish.
    # The canonical inventory is metadata-only unless it explicitly carries
    # a source content hash.  Recomputing a hash from title/page/summary would
    # compare a different function and falsely claim exact deduplication.
    inventory_hashes = {item.content_hash for item in inventory.items
                        if isinstance(item.content_hash, str)
                        and item.content_hash.strip()}
    pending_hashes = {digest for _, request in store.outbox_pending_requests(kind="handoff")
                      for digest in request.get("content_hashes", [])}
    if pending_hashes:
        incidents.append("hub_offline")
    exact_dupes: List[SynthesisRecord] = []
    novel: List[SynthesisRecord] = []
    for sr in synth_records:
        tick_budget.check('pipeline')
        existing = store.proposal_for_hash(sr.content_hash)
        previously_published = store.is_published(sr.content_hash)
        if sr.content_hash in pending_hashes:
            # Reserved remote write is retried above; never rewrite its note.
            continue
        # A local duplicate is safe only because it already passed the
        # page-level evidence check.  ``merge`` and ``contradiction`` remain
        # unresolved until a canonical source/proposal receipt exists; a
        # process restart must therefore retry their admitted events.
        resolved_local_decision = (existing is not None
                                   and existing["decision"] ==
                                   DECISION_DUPLICATE)
        if (sr.content_hash in inventory_hashes
                or resolved_local_decision
                or previously_published):
            exact_dupes.append(sr)
        else:
            novel.append(sr)

    proposals_new = 0
    proposals_duplicate = len(exact_dupes)
    store.outbox_drain(_synthesis_pending_dedup_ids_for_hashes(
        store, config.project_slug, [r.content_hash for r in exact_dupes]))
    proposals_merge = 0
    proposals_contradiction = 0

    # 7. Synthesize for novel records only.
    if novel:
        # Pull reviewed/verified wiki excerpts so the model can ground a
        # semantic duplicate/merge verdict in real canonical evidence.  The
        # inventory's metadata-only ``page`` is the same wiki file the
        # gateway reads, so reusing it stays consistent with the canonical
        # contents.  Page-level failures fall back to an empty excerpt
        # set; the pipeline still proceeds with synthesis, contradiction
        # detection, and explicit "no evidence" handling.
        wiki_pages = _wiki_pages_for_inventory(inventory.items)
        wiki_excerpts: Tuple[WikiExcerpt, ...] = ()
        if wiki_pages:
            try:
                # Reserve at most 25% of the prompt budget for wiki
                # excerpts so a large vault cannot push records out of
                # the prompt.
                excerpt_budget = max(512,
                    (max_prompt_bytes or config.limits.max_prompt_bytes)
                    // 4)
                wiki_excerpts = deps.gateway.read_wiki_excerpts(
                    pages=wiki_pages, max_bytes=excerpt_budget,
                    project_slug=config.project_slug,
                )
            except tick_budget.TickBudgetExceeded:
                raise
            except Exception:
                wiki_excerpts = ()

        payload = SynthesisInput(
            records=novel,
            inventory_summary=summarize_inventory(inventory.items),
            project_slug=config.project_slug,
            wiki_excerpts=wiki_excerpts,
        )
        prompt_bytes = max_prompt_bytes or config.limits.max_prompt_bytes
        prompt, unprocessed = build_prompt(
            payload, max_bytes=prompt_bytes,
            max_records=config.limits.max_records_per_run,
        )
        # ``build_prompt`` returns a contiguous tail.  Only the prefix that
        # actually made it into the JSON prompt may influence the model
        # decision, citations, or proposal hash; the tail is a separate
        # durable batch.
        selected_count = len(novel) - len(unprocessed)
        selected_records = novel[:max(0, selected_count)]
        # Track the synthesis prompt as a checkpoint so crash recovery can
        # observe what was attempted before a fault.
        store.checkpoint_set("synthesis_prompt", run_id, {
            "novel_count": len(novel),
            "selected_count": len(selected_records),
            "exact_dupes": len(exact_dupes),
            "prompt_bytes": len(prompt.encode("utf-8")),
            "unprocessed_count": len(unprocessed),
            "wiki_excerpt_count": len(wiki_excerpts),
        })
        if dry_run:
            store.finish_run(run_id)
            return PipelineResult(
                run_id=run_id, dry_run=True, records_read=len(records),
                records_admitted=records_admitted, proposals_new=0,
                proposals_duplicate=len(exact_dupes), proposals_merge=0,
                proposals_contradiction=0, local_outage=False,
                fallback_used=False, incidents=tuple(incidents),
                notes="dry_run",
            )
        if not selected_records:
            # A deliberately tiny prompt budget can fit valid JSON but no
            # whole source record.  Queue the complete tail and avoid a
            # model call with no evidence.
            _queue_pending_records(store, config, unprocessed or novel,
                                   reason="prompt_no_records_fit")
            store.finish_run(run_id)
            return PipelineResult(
                run_id=run_id, dry_run=False, records_read=len(records),
                records_admitted=records_admitted, proposals_new=0,
                proposals_duplicate=proposals_duplicate, proposals_merge=0,
                proposals_contradiction=0, local_outage=False,
                fallback_used=False, incidents=tuple(incidents),
                notes="prompt_no_records_fit",
            )
        # Persist the tail before model work.  It is independent of the
        # selected batch and must never be included in its proposal.
        if unprocessed:
            _queue_pending_records(store, config, unprocessed,
                                   reason="prompt_unprocessed")
            store.checkpoint_set("synthesis_unprocessed", run_id, {
                "count": len(unprocessed),
            })
        envelope, used_fallback, used_kind = _invoke_with_fallback(
            deps, config, prompt,
            state_dir=state_dir, run_id=run_id,
            local_only=any(record.speaker == "owner_document"
                           for record in selected_records))
        fallback_used = fallback_used or used_fallback
        if used_fallback:
            local_outage = True
            incidents.append("local_outage")
            store.raise_incident(run_id=run_id, kind="local_outage",
                                 message="primary_failed_subscription_fallback_attempted",
                                 cooldown_seconds=config.limits.incident_cooldown_seconds)
        if envelope is None:
            # Local outage; durable incident and preserved outbox.
            local_outage = True
            incidents.append(f"local_outage:{used_kind}")
            _queue_pending_records(store, config, selected_records,
                                   reason="local_outage")
            store.raise_incident(run_id=run_id, kind="local_outage",
                                 message=used_kind or "primary_failed",
                                 cooldown_seconds=config.limits.
                                 incident_cooldown_seconds)
            store.finish_run(run_id)
            return PipelineResult(
                run_id=run_id, dry_run=dry_run, records_read=len(records),
                records_admitted=records_admitted, proposals_new=0,
                proposals_duplicate=len(exact_dupes), proposals_merge=0,
                proposals_contradiction=0, local_outage=True,
                fallback_used=used_fallback, incidents=tuple(incidents),
            )

        store.checkpoint_set("model_response_metadata", run_id, {
            "native_session_id": envelope.session_id,
            "final_characters": len(envelope.final),
            "json_fenced": envelope.final.strip().startswith("```"),
            "provider": envelope.provider, "model": envelope.model,
        })
        decision_payload = SynthesisInput(
            records=selected_records,
            inventory_summary=payload.inventory_summary,
            project_slug=payload.project_slug,
            existing_target=payload.existing_target,
            wiki_excerpts=payload.wiki_excerpts,
        )
        decision = decide(envelope.final, records=selected_records,
                          payload=decision_payload,
                          model_identity=f"{envelope.provider}/"
                          f"{envelope.model}")
        model_identity = decision.model_identity or model_identity
        cited_hashes = _cited_hashes(decision.source_refs, selected_records)

        # Semantic duplicate / merge verdicts must be backed by an actual
        # reviewed/verified wiki excerpt.  Without evidence the pipeline
        # refuses to discard the input and routes the records back through
        # synthesis_pending rather than recording a synthetic dedup.
        wiki_page_paths = [w.page for w in wiki_excerpts]
        if not _evidence_supports_decision(decision, wiki_page_paths):
            incidents.append("duplicate_without_evidence")
            store.raise_incident(
                run_id=run_id, kind="duplicate_without_evidence",
                message=(f"decision={decision.decision} but no "
                          "wiki excerpt matched"),
            )
            _queue_pending_records(store, config, selected_records,
                                   reason="duplicate_without_evidence")
            store.finish_run(run_id)
            return PipelineResult(
                run_id=run_id, dry_run=dry_run, records_read=len(records),
                records_admitted=records_admitted,
                proposals_new=0,
                proposals_duplicate=proposals_duplicate,
                proposals_merge=0, proposals_contradiction=1,
                local_outage=local_outage,
                fallback_used=fallback_used,
                incidents=tuple(incidents),
                notes="duplicate_without_evidence",
                model_identity=model_identity,
            )

        # A merge target is model-controlled input.  Require the target to be
        # the same safe relative page returned by inventory and wiki evidence
        # before allowing any canonical handoff.
        validated_merge_target: Optional[str] = None
        if decision.decision == DECISION_MERGE:
            validated_merge_target = _validated_merge_target(
                decision.merge_target, inventory.items, list(wiki_excerpts),
            )
            if validated_merge_target is None:
                kind = ("merge_target_missing" if not decision.merge_target
                        else "merge_target_invalid")
                store.raise_incident(
                    run_id=run_id, kind=kind,
                    message="model merge target was not a returned canonical page",
                )
                incidents.append(kind)
                _queue_pending_records(store, config, selected_records,
                                       reason=kind)
                store.finish_run(run_id)
                return PipelineResult(
                    run_id=run_id, dry_run=dry_run,
                    records_read=len(records),
                    records_admitted=records_admitted, proposals_new=0,
                    proposals_duplicate=proposals_duplicate, proposals_merge=0,
                    proposals_contradiction=1, local_outage=local_outage,
                    fallback_used=fallback_used, incidents=tuple(incidents),
                    notes=kind, model_identity=model_identity,
                )

        # Persist a proposal for the selected batch only.  The unprocessed
        # tail was queued above and must not change its decision or group id.
        target_for_hash = validated_merge_target or ""
        group_hash = _group_hash(selected_records)
        pid = proposal_id(content_hash=group_hash,
                          decision=decision.decision,
                          merge_target=target_for_hash or None)
        store.proposal_upsert(
            proposal_id=pid, content_hash=group_hash,
            source_refs=decision.source_refs, synthesis=decision.synthesis,
            model_identity=decision.model_identity,
            decision=decision.decision, merge_target=target_for_hash or None,
            rationale=decision.rationale, source_hashes=cited_hashes,
        )

        if decision.decision != DECISION_CONTRADICTION:
            _queue_pending_records(store, config,
                                   [r for r in selected_records
                                    if r.content_hash not in cited_hashes],
                                   reason="uncited_source_tail")

        if decision.decision == DECISION_CONTRADICTION:
            proposals_contradiction = 1
            failure_kind = "model_contradiction" if decision.synthesis else "invalid_decision"
            store.raise_incident(run_id=run_id, kind=failure_kind,
                                 message=decision.rationale or "")
            incidents.append(failure_kind)
            # A contradiction is a durable unresolved outcome.  Keep every
            # admitted source queued until a later run can obtain a safe
            # model verdict and a canonical source/proposal receipt.
            _queue_pending_records(store, config, selected_records,
                                   reason=failure_kind)
        elif decision.decision == DECISION_DUPLICATE:
            # Only explicitly cited sources are resolved. The proposal/hash
            # relation above is durable; an older rejected decision must not
            # shadow it on replay. This is local semantic disposition, never
            # a fabricated canonical publication receipt.
            proposals_duplicate += len(cited_hashes)
            store.outbox_drain(_synthesis_pending_dedup_ids_for_hashes(
                store, config.project_slug, cited_hashes))
        elif decision.decision == DECISION_MERGE:
            proposals_merge = 1
            # Evidence-backed merges use the same durable canonical write as
            # new facts.  The target was validated above against inventory
            # and the returned reviewed/verified excerpt.
            store.checkpoint_set("merge_proposal", run_id, {
                "merge_target": validated_merge_target,
                "source_refs": decision.source_refs,
            })
            attempt = _submit_handoff(
                config, deps, store, run_id=run_id, state_dir=state_dir,
                decision=decision, selected_records=selected_records,
                cited_hashes=cited_hashes, group_hash=group_hash,
                proposal_key=pid, target_page=validated_merge_target or "",
                context_receipt=begin.receipt_path,
            )
            if attempt.note_failed:
                incidents.append("hub_offline")
            elif attempt.synthetic:
                # A synthetic result never proves that the canonical merge
                # was applied; the reserved handoff remains retryable.
                incidents.append("hub_unverified")
            elif not attempt.receipt_verified:
                incidents.append("hub_offline")
        elif decision.decision == DECISION_NEW:
            proposals_new = 1
            # Synthesize is required to reference at least one content_hash
            # we computed; cross-check to forbid pure concatenation.
            if not cited_hashes:
                proposals_new = 0
                proposals_contradiction = 1
                store.raise_incident(run_id=run_id,
                                     kind="unresolved_citations",
                                     message="no cited hashes")
                incidents.append("unresolved_citations")
                _queue_pending_records(store, config, selected_records,
                                       reason="unresolved_citations")
            else:
                target_page = (decision.merge_target
                               or f"wiki/projects/{config.project_slug}.md")
                attempt = _submit_handoff(
                    config, deps, store, run_id=run_id, state_dir=state_dir,
                    decision=decision, selected_records=selected_records,
                    cited_hashes=cited_hashes, group_hash=group_hash,
                    proposal_key=pid, target_page=target_page,
                    context_receipt=begin.receipt_path,
                )
                if attempt.note_failed:
                    proposals_new = 0
                    incidents.append("hub_offline")
                elif attempt.synthetic:
                    incidents.append("hub_unverified")
                elif not attempt.receipt_verified:
                    proposals_new = 0
                    incidents.append("hub_offline")
        else:
            proposals_contradiction = 1
            store.raise_incident(run_id=run_id, kind="invalid_decision",
                                 message=str(decision.decision))
            incidents.append("invalid_decision")
            _queue_pending_records(store, config, selected_records,
                                   reason="invalid_decision")

    store.finish_run(run_id)
    return PipelineResult(
        run_id=run_id, dry_run=dry_run, records_read=len(records),
        records_admitted=records_admitted, proposals_new=proposals_new,
        proposals_duplicate=proposals_duplicate,
        proposals_merge=proposals_merge,
        proposals_contradiction=proposals_contradiction,
        local_outage=local_outage, fallback_used=fallback_used,
        incidents=tuple(incidents),
        model_identity=model_identity,
    )


def _invoke_with_fallback(deps: PipelineDeps, config: CaretakerConfig,
                          prompt: str, *, state_dir: str, run_id: str,
                          local_only: bool = False
                          ) -> Tuple[Optional[Envelope], bool, str]:
    """Try the pinned local primary; on outage, optionally fall back.

    The fallback is only invoked when:
    - ``config.fallback`` is set,
    - the primary raised a transport error (not a model contradiction),
    - and the local outage cooldown has elapsed.
    """
    cwd = config.state_dir
    tick_budget.check('primary')
    primary_path = _write_run_config(config.primary, state_dir, cwd, run_id)
    try:
        env = deps.primary.invoke(
            spec=config.primary, cwd=cwd, prompt=prompt,
            run_config_path=primary_path, state_dir=state_dir, timeout=120,
        )
        return env, False, "primary_ok"
    except TransportError as exc:
        primary_failure = str(exc)
    if local_only or config.fallback is None or deps.fallback is None:
        return None, False, f"primary_failed:{primary_failure}"
    tick_budget.check('fallback')
    fallback_path = _write_run_config(config.fallback, state_dir, cwd, run_id)
    try:
        env = deps.fallback.invoke(
            spec=config.fallback, cwd=cwd, prompt=prompt,
            run_config_path=fallback_path, state_dir=state_dir, timeout=120,
        )
        return env, True, f"fallback_ok:{primary_failure}"
    except TransportError as exc:
        return None, True, f"fallback_failed:{exc}"


def _group_hash(records: Iterable[SynthesisRecord]) -> str:
    h = hashlib.sha256()
    # A proposal groups source records; source order must not change its
    # durable id or the retry request after an input file is reordered.
    hashes = {r.content_hash for r in records if r.content_hash}
    for digest in sorted(hashes):
        tick_budget.check('pipeline')
        h.update(digest.encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


def _cited_hashes(refs: List[str], records: List[SynthesisRecord]
                  ) -> List[str]:
    known = {r.content_hash for r in records}
    ref_hashes = {f"{r.identity_tool}:{r.native_session_id}:{r.cursor}":
                  r.content_hash for r in records}
    cited = []
    for ref in refs:
        tick_budget.check('pipeline')
        if ref in known:
            cited.append(ref)
            continue
        if ref in ref_hashes:
            cited.append(ref_hashes[ref])
    # Dedup preserving order.
    seen = set()
    out = []
    for h in cited:
        tick_budget.check('pipeline')
        if h in seen:
            continue
        seen.add(h)
        out.append(h)
    return out


def build_default_deps(config: CaretakerConfig, gateway: KnowledgeGateway,
                       *, worktree_root: str = "",
                       primary_override: Optional[Transport] = None,
                       fallback_override: Optional[Transport] = None,
                       office_override: Optional[OfficeConnector] = None
                       ) -> PipelineDeps:
    primary = primary_override or OpenClawArgvTransport(
        expected_identity=("vllm", "Qwen3.8-27B"),
    )
    fallback: Optional[Transport] = None
    if config.fallback is not None:
        fallback = fallback_override or OpenClawArgvTransport(
            expected_identity=("muse-code", "muse-spark-1.3"),
        )
    office = office_override or _default_office(config)
    return PipelineDeps(primary=primary, fallback=fallback, gateway=gateway,
                        office=office, worktree_root=worktree_root)


def _default_office(config: CaretakerConfig) -> OfficeConnector:
    identity = OfficeIdentity(
        harness=config.office.harness,
        host=config.office.host,
        session_id=config.office.session_id,
    )
    return OfficeConnector(
        identity=identity,
        transport=CanonicalOfficeArgvTransport(
            executable=config.office.wrapper or config.office.script,
            home=config.office.home,
            harness=config.office.harness,
            session_id=config.office.session_id,
            python_executable=config.office.python_executable,
            script=config.office.script,
        ),
        dedup=OfficeDedupStore(
            path=str(Path(config.state_dir) / "office-dedup.json")
            if config.office.dedup else None,
        ),
        rate=RateLimiter(
            max_per_minute=config.office.rate_per_minute,
            path=str(Path(config.state_dir) / "office-rate.json"),
        ),
        no_autoplay_reply=config.office.no_autoplay_reply,
    )
