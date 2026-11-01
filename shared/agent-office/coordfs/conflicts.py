"""Warning-only advice for existing coordfs lease observations.

This module consumes a result already returned by ``CoordBoard.claim`` or a
similar read.  It does not acquire/release a lease, retry, touch a checkout, or
make an allow/deny decision.  Its purpose is to make a holder, TTL hint, and
lineage relationship visible so same-task fan-out can share a scoped plan.
"""

from __future__ import annotations

from typing import Any, Mapping


LINEAGE_KEYS = ("task_id", "node_id", "graph_id", "run_id")


def _lineage(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, str] = {}
    for key in LINEAGE_KEYS:
        item = value.get(key)
        if isinstance(item, str):
            text = item.strip()
            if text and len(text) <= 256 and "\x00" not in text and not any(ord(char) < 32 for char in text):
                result[key] = text
    return result


def _holder_lineage(lease_result: Mapping[str, Any]) -> dict[str, str]:
    lease = lease_result.get("lease")
    if not isinstance(lease, Mapping):
        return {}
    return _lineage(lease.get("meta"))


def _relation(holder: Mapping[str, str], requester: Mapping[str, str]) -> str:
    holder_task = holder.get("task_id")
    requester_task = requester.get("task_id")
    if not holder_task or not requester_task:
        return "unknown"
    if holder_task != requester_task:
        return "unrelated_task"
    # Task id is the primary fan-out binding.  Distinct candidate runs are
    # normal in one Fleet fan-out, so `run_id` is deliberately informational.
    # An explicitly different graph is the one signal that proves a different
    # task lineage.  Missing optional fields never cause false serialization.
    if holder.get("graph_id") and requester.get("graph_id") and holder["graph_id"] != requester["graph_id"]:
        return "unrelated_lineage"
    return "same_task"


def _base(*, code: str, visibility: str, holder_agent: str | None, ttl_hint_s: float | int | None) -> dict[str, Any]:
    return {
        "status": "warning",
        "code": code,
        "visibility": visibility,
        "holder_agent": holder_agent,
        "ttl_hint_s": ttl_hint_s,
        "advisory_only": True,
        "authority": "none",
        "automatic_retry": False,
        "requires_serialization": False,
    }


class ConflictAdvisor:
    """Translate lease observations into coordination advice without enforcing them.

    ``advise`` is intentionally pure with respect to coordfs: callers supply the
    already-observed result.  A returned recommendation never authorizes a VCS,
    checkout, permission, or production action, and no branch retries a board
    operation automatically.
    """

    def advise(
        self,
        lease_result: Mapping[str, Any] | None,
        *,
        requester_agent: str | None = None,
        requester_lineage: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Expose held/stale/unknown/unavailable state while preserving warning-only semantics."""
        requester = _lineage(requester_lineage)
        if not isinstance(lease_result, Mapping):
            result = _base(code="lease_state_unknown", visibility="unknown", holder_agent=None, ttl_hint_s=None)
            result.update(
                {
                    "lineage_relation": "unknown",
                    "holder_lineage": {},
                    "requester_lineage": requester,
                    "fanout_compatible": False,
                    "recommendation": "continue_without_board_advice",
                }
            )
            return result

        source_status = lease_result.get("status")
        source_code = lease_result.get("code")
        holder = lease_result.get("holder_agent")
        holder_agent = holder if isinstance(holder, str) and holder else None
        holder_lineage = _holder_lineage(lease_result)
        relation = _relation(holder_lineage, requester)

        if source_status == "warning" and source_code == "held":
            ttl = lease_result.get("ttl_hint_s", lease_result.get("ttl_remaining_s"))
            ttl_hint = ttl if isinstance(ttl, (int, float)) and not isinstance(ttl, bool) and ttl >= 0 else None
            result = _base(code="lease_held", visibility="held", holder_agent=holder_agent, ttl_hint_s=ttl_hint)
            result.update(
                {
                    "ttl_remaining_s": ttl_hint,
                    "lineage_relation": relation,
                    "holder_lineage": holder_lineage,
                    "requester_lineage": requester,
                    "fanout_compatible": relation == "same_task",
                    "recommendation": (
                        "share_scoped_integration_plan"
                        if relation == "same_task"
                        else "coordinate_scoped_integration_plan"
                    ),
                    "source_status": source_status,
                    "source_code": source_code,
                }
            )
            return result

        if source_code == "stale_resource" or source_status == "conflict":
            result = _base(code="stale_resource", visibility="stale", holder_agent=holder_agent, ttl_hint_s=None)
            result.update(
                {
                    "lineage_relation": relation,
                    "holder_lineage": holder_lineage,
                    "requester_lineage": requester,
                    "fanout_compatible": False,
                    "recommendation": "inspect_stale_state",
                    "source_status": source_status,
                    "source_code": source_code,
                }
            )
            return result

        if source_status == "degraded" or source_code in {"board_unavailable", "office_unavailable"}:
            result = _base(code="board_unavailable", visibility="unavailable", holder_agent=holder_agent, ttl_hint_s=None)
            result.update(
                {
                    "lineage_relation": relation,
                    "holder_lineage": holder_lineage,
                    "requester_lineage": requester,
                    "fanout_compatible": False,
                    "recommendation": "continue_without_board_advice",
                    "source_status": source_status,
                    "source_code": source_code,
                }
            )
            return result

        if source_status == "ok" and source_code == "free":
            result = _base(code="lease_free", visibility="free", holder_agent=None, ttl_hint_s=None)
            result.update(
                {
                    "lineage_relation": "unknown",
                    "holder_lineage": {},
                    "requester_lineage": requester,
                    "fanout_compatible": False,
                    "recommendation": "no_holder_observed",
                    "source_status": source_status,
                    "source_code": source_code,
                }
            )
            return result

        result = _base(code="lease_state_unknown", visibility="unknown", holder_agent=holder_agent, ttl_hint_s=None)
        result.update(
            {
                "lineage_relation": relation,
                "holder_lineage": holder_lineage,
                "requester_lineage": requester,
                "fanout_compatible": False,
                "recommendation": "continue_without_board_advice",
                "source_status": source_status,
                "source_code": source_code,
            }
        )
        return result

    def propose_scoped_integration(self, work_board: Any, **kwargs: Any) -> dict[str, Any]:
        """Delegate only record creation to ``WorkBoard``; no advice becomes permission."""
        return work_board.record_integration_plan(**kwargs)


def advise_lease_result(
    lease_result: Mapping[str, Any] | None,
    *,
    requester_agent: str | None = None,
    requester_lineage: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Convenience entry point for integration code that does not need an advisor instance."""
    return ConflictAdvisor().advise(
        lease_result,
        requester_agent=requester_agent,
        requester_lineage=requester_lineage,
    )


__all__ = ["ConflictAdvisor", "LINEAGE_KEYS", "advise_lease_result"]
