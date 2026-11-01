"""One Polish status per operation. Intent persisted BEFORE outbound send."""

from __future__ import annotations

from typing import Any, Optional

from openclaw_control.status_text import StatusTracker, render_status_pl


class StatusNotifier:
    def __init__(self, store: Any, api: Any, chat_id: int) -> None:
        self.store = store
        self.api = api
        self.chat_id = int(chat_id)
        self.tracker = StatusTracker(store)
        self.sends = 0

    def recover(self, now: float) -> list:
        return self.tracker.recover_intents(now)

    def publish(
        self,
        dispatch_id: str,
        state: str,
        now: float,
        *,
        operation: str = "dispatch",
        detail: str = "",
        reply_markup: Optional[dict] = None,
    ) -> dict:
        text = render_status_pl(
            operation, state, label=dispatch_id[-6:], detail=detail
        )
        return self.publish_text(dispatch_id, text, now, reply_markup=reply_markup)

    def publish_text(self, dispatch_id, text, now, *, reply_markup=None, force=False):
        if len(text) > 600:
            raise ValueError('Status too long')
        decision = self.tracker.decide(dispatch_id, text)
        if force and decision['action'] == 'skip':
            existing = self.store.get_status(dispatch_id) or {}
            if existing.get('message_id') and not existing.get('uncertain'):
                decision = {'action': 'edit', 'message_id': existing['message_id'], 'chat_id': existing['chat_id']}
        if decision["action"] in ("skip", "wait"):
            return decision
        chat_id = int(decision.get("chat_id") or self.chat_id)
        if self.api is None:
            return {"action": "wait", "reason": "no-api"}
        self.tracker.persist_intent(dispatch_id, chat_id, text, now)
        try:
            if decision["action"] == "edit":
                result = self.api.edit_text(
                    chat_id,
                    int(decision["message_id"]),
                    text,
                    reply_markup=reply_markup,
                )
            else:
                result = self.api.send_text(
                    chat_id, text, reply_markup=reply_markup
                )
                self.sends += 1
            message_id = int(result.get("message_id") or decision.get("message_id") or 0)
            self.tracker.confirm(dispatch_id, chat_id, message_id, text, now)
            return {"action": "ok", "message_id": message_id}
        except Exception:
            self.tracker.mark_uncertain(dispatch_id, now)
            return {"action": "uncertain"}
