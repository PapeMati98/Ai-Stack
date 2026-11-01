"""Candidate repair gate: exact owner decision required before any fix work.

An owner approval authorises ONLY local isolated candidate work
(code/tests/review) for the exact bound tuple: project, issue, source
revision, scope, scope hash, candidate hash and unexpired window.
Deployment, IAM, deletion and customer-data mutation stay forbidden and need
a separate explicit scope; this module never grants them.

Audit and Graph wiring are injectable hooks (callables or None).  By default
nothing is sent anywhere and no model or remote call happens: the parent
Codex owns live integration.  All payloads carry references and hashes only.
"""

from __future__ import annotations

import hashlib
from typing import Callable, Optional

from openclaw_ops import approvals, models


def candidate_hash(project: str, stable_id: str, revision: str,
                   scope: str) -> str:
    """Deterministic binding of one repair candidate to its exact context."""
    return hashlib.sha256(
        f"candidate:{project}:{stable_id}:{revision}:{scope}".encode("utf-8")
    ).hexdigest()


def bind_repair_request(*, request_id: str, project: str, stable_id: str,
                        revision: str, scope: str, expires_at: float,
                        created_at: float = 0.0,
                        action: str = "approve") -> approvals.PendingRequest:
    """Build the exact owner-decision binding for one repair request."""
    from openclaw_ops.helpdesk import scope_hash

    return approvals.PendingRequest(
        request_id=request_id, stable_id=stable_id, revision=revision,
        scope=scope, scope_hash=scope_hash(scope), action=action,
        expires_at=expires_at, created_at=created_at, project=project,
        candidate_hash=candidate_hash(project, stable_id, revision, scope))


def authorize_repair(*, verdict: dict,
                     pending: approvals.PendingRequest, request: dict,
                     now: float,
                     audit_hook: Optional[Callable[[dict], None]] = None,
                     graph_dispatch: Optional[Callable[[dict], object]] = None
                     ) -> dict:
    """Authorise candidate-local repair work for an exact owner decision.

    Every check fails closed with a named reason; the audit hook (when set)
    receives exactly one non-empty record per call.  ``graph_dispatch``
    (when set) receives only references/hashes and runs only after all
    checks pass.  Deploy/IAM/deletion scopes are never authorised here.
    """

    def denied(reason: str) -> dict:
        record = {"authorized": False, "reason": reason,
                  "request_id": request.get("request_id"),
                  "candidate_only": True}
        if audit_hook is not None:
            audit_hook(dict(record))
        return record

    if not isinstance(verdict, dict) or not verdict.get("authorized"):
        return denied("no-owner-authorization")
    if (verdict.get("request_id") != pending.request_id
            or request.get("request_id") != pending.request_id):
        return denied("request-mismatch")
    if now > pending.expires_at:
        return denied("authorization-expired")
    if not (verdict.get("revision") == pending.revision
            == request.get("revision")):
        return denied("revision-mismatch")
    if not (verdict.get("scope") == pending.scope
            == request.get("scope")):
        return denied("scope-mismatch")
    if not (verdict.get("scope_hash") == pending.scope_hash
            == request.get("scope_hash")):
        return denied("scope-hash-mismatch")
    if verdict.get("action") != pending.action:
        return denied("action-mismatch")
    expected_candidate = candidate_hash(
        str(request.get("project", pending.project)),
        pending.stable_id, pending.revision, pending.scope)
    if pending.candidate_hash != expected_candidate:
        return denied("candidate-binding-mismatch")
    if ("candidate_hash" in verdict
            and verdict.get("candidate_hash") != expected_candidate):
        return denied("candidate-binding-mismatch")
    if request.get("project", pending.project) != pending.project:
        return denied("project-mismatch")
    if request.get("status") != "proposed":
        return denied("request-not-proposed")
    if pending.scope not in models.CANDIDATE_SCOPES:
        return denied("scope-requires-separate-explicit-authorization")

    record = {"authorized": True, "request_id": pending.request_id,
              "project": pending.project, "revision": pending.revision,
              "scope": pending.scope, "scope_hash": pending.scope_hash,
              "candidate_hash": expected_candidate, "candidate_only": True,
              "message_id": verdict.get("message_id")}
    if graph_dispatch is None:
        record["dispatched"] = False
        record["reason"] = "graph-dispatch-unconnected"
    else:
        graph_dispatch({"request_id": pending.request_id,
                        "project": pending.project,
                        "stable_id": pending.stable_id,
                        "revision": pending.revision, "scope": pending.scope,
                        "scope_hash": pending.scope_hash,
                        "candidate_hash": expected_candidate})
        record["dispatched"] = True
    if audit_hook is not None:
        audit_hook(dict(record))
    return record
