"""Single short Polish status per operation.

One message per operation, edited in place, at most 600 chars. Identical
text is never resent (no unchanged spam). An uncertain send is NOT
blindly retried: the tracker reports "wait" until the owner reconciles.
"""

from __future__ import annotations

import hashlib
from typing import Any

from openclaw_control import models

STATE_PL = {
    models.QUEUED: "w kolejce",
    models.DISPATCHING: "wysylanie",
    models.DELIVERED: "przyjęto; oczekiwanie na start",
    models.RUNNING: "wykonywanie",
    models.COMPLETED: "tura zakończona",
    models.BLOCKED: "zablokowano",
    models.FAILED: "niepowodzenie",
    models.DELIVERY_UNKNOWN: "stan niepewny",
    models.USER_PAUSED: "wstrzymane",
    models.CANCEL_PENDING: "nowe wysyłki wstrzymane; tura może nadal pracować",
    models.CANCELLED: "anulowano",
}

OPERATION_PL = {
    "dispatch": "Dyspozycja",
    "delivery": "Dostarczenie",
    "cancel": "Anulowanie",
    "stop": "Wstrzymanie",
    "takeover": "Przejecie",
    "reconcile": "Uzgodnienie",
}


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def render_status_pl(
    operation: str,
    state: str,
    label: str = "",
    detail: str = "",
) -> str:
    """Render one short Polish status, hard-capped at 600 chars.

    Only our own generated wording is fitted; owner text is never
    embedded here (hashes/references only -- see delivery summary).
    """
    op = OPERATION_PL.get(operation, operation)
    st = STATE_PL.get(state, state)
    head = op
    if label:
        head += " " + label[:12]
    text = "%s: %s." % (head, st)
    if detail:
        text += " " + detail
    if len(text) > models.STATUS_MAX_CHARS:
        # Fit: keep head intact, cut the detail tail.
        keep = models.STATUS_MAX_CHARS - len(head) - 5
        if keep < 0:
            text = head[: models.STATUS_MAX_CHARS - 1] + "."
        else:
            text = "%s: %s. %s." % (head, st, detail[:keep])
            if len(text) > models.STATUS_MAX_CHARS:
                text = text[: models.STATUS_MAX_CHARS - 1] + "."
    assert len(text) <= models.STATUS_MAX_CHARS
    return text


class StatusTracker:
    """Edit-in-place decisions backed by the shared store."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def decide(self, dispatch_id: str, new_text: str) -> dict:
        """Return send/edit/skip/wait for one status text."""
        row = self.store.get_status(dispatch_id)
        fp = fingerprint(new_text)
        if row is not None and int(row.get("uncertain") or 0):
            return {
                "action": "wait",
                "reason": "uncertain-send",
                "dispatch_id": dispatch_id,
            }
        if row is not None and str(row.get("outbound_state") or "") == "intent":
            return {
                "action": "wait",
                "reason": "intent-pending",
                "dispatch_id": dispatch_id,
            }
        if row is not None and row.get("last_hash") == fp:
            return {
                "action": "skip",
                "reason": "unchanged",
                "dispatch_id": dispatch_id,
            }
        if row is not None and int(row.get("message_id") or 0):
            return {
                "action": "edit",
                "chat_id": row["chat_id"],
                "message_id": row["message_id"],
                "dispatch_id": dispatch_id,
            }
        chat_id = row["chat_id"] if row is not None else 0
        return {
            "action": "send",
            "chat_id": chat_id,
            "dispatch_id": dispatch_id,
        }

    def confirm(
        self,
        dispatch_id: str,
        chat_id: int,
        message_id: int,
        text: str,
        now: float,
    ) -> None:
        self.store.note_status(
            dispatch_id, int(chat_id), int(message_id),
            fingerprint(text), now,
        )

    def mark_uncertain(self, dispatch_id: str, now: float) -> None:
        self.store.set_status_uncertain(dispatch_id, now)

    def persist_intent(
        self, dispatch_id: str, chat_id: int, text: str, now: float
    ) -> dict:
        """Claim the outbound send BEFORE calling Telegram."""
        return self.store.begin_status_intent(
            dispatch_id, int(chat_id), fingerprint(text), now
        )

    def recover_intents(self, now: float) -> list:
        """Crash before ACK: intents become uncertain, never duplicated."""
        return self.store.recover_status_intents(now)
