"""Bounded injected helpdesk tick. Shares DB and notification policy.

Parent wires collectors later. This slice never talks to live helpdesks.
"""

from __future__ import annotations

from typing import Any, Callable, Optional


class HelpdeskHook:
    def __init__(
        self,
        callback: Optional[Callable] = None,
        notifier: Any = None,
    ) -> None:
        self.callback = callback
        self.notifier = notifier
        self.last_report: Optional[dict] = None

    def tick(self, store: Any, now: float) -> dict:
        if self.callback is None:
            return {"action": "unwired", "new": 0}
        report = self.callback(store, now)
        if not isinstance(report, dict):
            return {"action": "invalid-report", "new": 0}
        self.last_report = report
        # Notifications use the same StatusTracker policy: hashes/refs only.
        if self.notifier is not None and report.get("notify_dispatch_id"):
            self.notifier.publish(
                str(report["notify_dispatch_id"]),
                str(report.get("state") or "queued"),
                now,
                operation="dispatch",
                detail=str(report.get("detail") or "")[:80],
            )
        return report
