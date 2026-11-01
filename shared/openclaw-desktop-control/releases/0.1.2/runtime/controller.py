"""Long-lived single-instance controller loop. No shell concatenation."""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

from openclaw_control import models
from openclaw_control.botapi import BotApi, BotApiError, TOKEN_ENV_KEY
from openclaw_control.delivery import (
    recover as recover_claims,
    redacted_summary,
)
from openclaw_control.store import ControlStore, StoppedError
from openclaw_control.telegram import (
    OwnerConfig,
    intake_update,
    recover_pending,
)
from openclaw_ops.ledger import Ledger

from runtime.helpdesk_hook import HelpdeskHook
from runtime.menus import MENU_TEXT, keyboard
from runtime.notify import StatusNotifier
from runtime.registry import AdapterRegistry
from runtime.supervision import consider_resume
from openclaw_control.adapters import AdapterBusyError, AdapterUnavailableError


class Controller:
    def __init__(
        self,
        cfg: Any,
        store: ControlStore,
        env: dict,
        opener: Callable,
        registry: AdapterRegistry,
        *,
        ledger: Optional[Ledger] = None,
        clock: Any = None,
        helpdesk: Optional[HelpdeskHook] = None,
        now: Optional[Callable] = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.ledger = ledger
        self.registry = registry
        self.clock = clock
        self._now = now
        self.env = env
        self.owner = OwnerConfig(
            owner_user_ids=tuple(cfg.owner_user_ids),
            owner_chat_id=int(cfg.owner_chat_id),
        )
        self.api = None
        if env.get(TOKEN_ENV_KEY):
            try:
                self.api = BotApi(
                    env,
                    opener,
                    host=cfg.telegram_host,
                    timeout=max(15.0, float(cfg.long_poll_timeout) + 5.0),
                )
            except BotApiError:
                self.api = None
                self.last_error = "bot-api-init"
        self.notifier = StatusNotifier(
            store, self.api, int(cfg.owner_chat_id)
        )
        self.helpdesk = helpdesk or HelpdeskHook()
        self.bot_ok = False
        self.last_error = ""
        self.busy_targets: set = set()
        self.registry_factory = None

    def now(self) -> float:
        if self._now is not None:
            return float(self._now())
        if self.clock is not None:
            return float(self.clock.wall())
        import time

        return time.time()

    def validate_bot(self) -> dict:
        if self.api is None:
            self.bot_ok = False
            self.last_error = "missing-token"
            return {"ok": False, "reason": "missing-token"}
        me = self.api.get_me()
        bot_id = me.get("id")
        if int(bot_id or 0) != int(self.cfg.bot_id):
            self.bot_ok = False
            self.last_error = "bot-id-mismatch"
            return {"ok": False, "reason": "bot-id-mismatch"}
        if me.get("is_bot") is not True:
            self.bot_ok = False
            self.last_error = "not-a-bot"
            return {"ok": False, "reason": "not-a-bot"}
        if self.api.get_webhook_info().get('url'):
            self.bot_ok = False
            self.last_error = 'existing-webhook'
            return {'ok': False, 'reason': self.last_error}
        self.bot_ok = True
        return {"ok": True, "id": int(bot_id)}

    def recover(self) -> dict:
        now = self.now()
        inbox = recover_pending(self.store, self.owner, now)
        intents = self.notifier.recover(now)
        claims = recover_claims(self.store, now)
        for row in claims:
            self._reconcile_unknown(row, now)
        return {
            "inbox": len(inbox),
            "intents": len(intents),
            "claims": len(claims),
        }

    def _reconcile_unknown(self, row: dict, now: float) -> None:
        adapter = self.registry.get(row.get("provider") or "")
        native = getattr(adapter, "native_readback", None)
        if native is None:
            return
        try:
            rb = native(row)
        except Exception:
            return
        state = rb.get("state")
        if state in ("running", "completed"):
            try:
                if row['state'] == models.DELIVERY_UNKNOWN:
                    self.store.reconcile(row["dispatch_id"], True, now)
                current = self.store.require_dispatch(row["dispatch_id"])
                if current["state"] == models.DELIVERED:
                    self.store.record_start(row["dispatch_id"], now)
                    current = self.store.require_dispatch(row["dispatch_id"])
                if state == "completed" and current["state"] in (models.RUNNING, models.CANCEL_PENDING):
                    self.store.record_result(
                        row["dispatch_id"], models.COMPLETED, now
                    )
            except Exception:
                return

    def observe_dispatches(self):
        now = self.now()
        for row in self.store.list_dispatches():
            if row['state'] in (models.DELIVERY_UNKNOWN, models.DELIVERED,
                                models.RUNNING, models.CANCEL_PENDING):
                self._reconcile_unknown(row, now)
                current = self.store.require_dispatch(row['dispatch_id'])
                markup = None
                if current['state'] == models.COMPLETED:
                    action = self.store.issue_callback('result_summary', current['dispatch_id'],
                        current['provider'], current['native_thread'], current['generation'], '', now)
                    markup = {'inline_keyboard': [[{'text': 'Pokaż wynik', 'callback_data': action}]]}
                self.notifier.publish(current['dispatch_id'], current['state'], now, reply_markup=markup)

    def poll_and_intake(self) -> list:
        if not self.cfg.telegram_enabled or not self.cfg.enabled or self.api is None:
            return []
        offset = self.store.get_poll_cursor(models.POLL_CURSOR_TELEGRAM)
        initial = self.cfg.raw.get('telegram', {}).get('initial_update_offset', 0)
        if offset == 0 and isinstance(initial, int) and not isinstance(initial, bool) and initial > 0:
            self.store.set_poll_cursor(models.POLL_CURSOR_TELEGRAM, initial, self.now())
            offset = initial
        try:
            updates = self.api.get_updates(
                offset=offset or None,
                timeout=min(float(self.cfg.long_poll_timeout), 25.0),
                limit=100,
            )
        except BotApiError as exc:
            self.last_error = type(exc).__name__
            return []
        now = self.now()
        results = []
        max_id = offset
        for update in updates:
            if not isinstance(update, dict):
                continue
            uid = update.get("update_id")
            if isinstance(uid, int) and uid >= max_id:
                max_id = uid + 1
            # Stash first so a crash cannot lose the update after offset move.
            result = intake_update(self.store, update, self.owner, now)
            results.append(result)
            self._react(result, update, now)
        if max_id != offset:
            self.store.set_poll_cursor(
                models.POLL_CURSOR_TELEGRAM, max_id, now
            )
        return results

    def _react(self, result: dict, update: dict, now: float) -> None:
        kind = result.get("kind")
        if kind == 'rejected' and result.get('reason') == 'expired-callback':
            # Authentication succeeded before callback expiry was checked.
            self._send_menu(now, force=True)
            return
        if kind == 'rejected' and result.get('reason') == 'stopped':
            self.notifier.publish('stopped:' + str(update.get('update_id')), models.USER_PAUSED, now,
                                  detail='STOP aktywny. Nie przekazano dyspozycji.')
            return
        if kind in (
            "rejected",
            "duplicate",
            "bot-sender",
            "wrong-sender",
            "wrong-chat",
            "forwarded",
            "impersonation",
        ):
            # Unauthenticated / hostile: never execute, never reply.
            return
        query = update.get("callback_query") or {}
        qid = query.get("id") if isinstance(query, dict) else None
        if qid and self.api is not None and kind in (
            "target_selected",
            "stopped",
            "disabled_target",
            "show_menu",
            "result_summary",
        ):
            try:
                self.api.answer_callback(str(qid), "")
            except BotApiError:
                pass
        if kind == "show_menu":
            self._send_menu(now, force=True)
            return
        if kind in ('target_selected', 'resumed'):
            self._send_menu(now, force=True)
            return
        if kind == 'result_summary':
            row = self.store.get_dispatch(result.get('task_id') or '')
            if row is None or row['state'] != models.COMPLETED:
                return
            adapter = self.registry.get(row['provider'])
            try:
                readback = adapter.native_readback(row)
                if readback.get('state') != 'completed':
                    return
                from openclaw_ops.sanitize import redact
                answer = redact(str(readback.get('result') or 'Brak treści wyniku.'))
                text = 'Wynik tury — fragment:\n' + answer[:500]
                if len(answer) > 500:
                    text += '\nPełna odpowiedź jest w zadaniu Desktop.'
                self.notifier.publish_text(row['dispatch_id'], text, now)
            except Exception:
                pass
            return
        if kind == "stopped":
            for row in self.store.list_dispatches():
                if row['state'] in (models.DELIVERED, models.RUNNING, models.DELIVERY_UNKNOWN):
                    self.store.request_cancel(row['dispatch_id'], now)
                    self.notifier.publish(row['dispatch_id'], models.CANCEL_PENDING, now)
            self._send_menu(now, force=True)
            return
        if kind == "disabled_target":
            return
        if kind in ("prompt", "prompt-unbound", "correction", "unsupported"):
            did = result.get("dispatch_id") or result.get("receipt_id") or ""
            if did:
                state = models.QUEUED
                if kind == "unsupported":
                    state = models.BLOCKED
                if kind == "prompt-unbound":
                    detail = "wybierz cel przyciskiem"
                elif kind == "unsupported":
                    detail = "format nieobslugiwany"
                else:
                    detail = ""
                markup = None
                if kind == "prompt-unbound":
                    markup = keyboard(self.store, self.cfg, now, dispatch_id=str(did))
                self.notifier.publish(
                    str(did), state, now, detail=detail, reply_markup=markup
                )

    def _send_menu(self, now: float, *, force: bool = False) -> None:
        shown = self.store.get_kv(models.RUNTIME_MENU_SHOWN)
        if shown and not force:
            return
        if self.api is None:
            return
        markup = keyboard(self.store, self.cfg, now)
        selection = self.store.get_selection(self.cfg.owner_chat_id)
        label = next((s.label for s in self.cfg.targets
                      if selection and s.native_thread == selection['native_thread']), 'nie wybrano')
        text = MENU_TEXT + '\nCel: ' + label[:100]
        if self.store.is_stopped():
            text += '\nSTOP aktywny. Bieżąca tura może nadal pracować.'
        try:
            outcome = self.notifier.publish_text('runtime-menu', text, now, reply_markup=markup, force=force)
            if outcome.get('action') != 'ok':
                return
            self.store.set_kv(models.RUNTIME_MENU_SHOWN, "1", now)
        except BotApiError:
            self.last_error = "menu-send-failed"

    def dispatch_once(self) -> list:
        now = self.now()
        out = []
        skip: set = set()
        while True:
            claimed = self.store.claim_one("runtime", now, skip_targets=skip)
            if claimed is None:
                break
            if self.store.is_stopped(claimed.get("task_id") or ""):
                self.store.unclaim(claimed["dispatch_id"], now)
                break
            if not claimed.get("target_binding"):
                self.store.unclaim(claimed["dispatch_id"], now)
                skip.add(claimed["dispatch_id"])
                continue
            provider = claimed.get("provider") or ""
            adapter = self.registry.get(provider)
            meta = self.registry.meta(provider)
            if not meta.get("enabled"):
                self.store.unclaim(claimed["dispatch_id"], now)
                if provider == 'codex-desktop' and meta.get('verified') and meta.get('reason') in (
                        'pid-unavailable', 'pipe-unavailable', 'native-session-unavailable'):
                    skip.add(claimed.get('target_binding') or claimed['dispatch_id'])
                    self.notifier.publish(claimed['dispatch_id'], models.QUEUED, now,
                                          detail='Desktop niedostępny; czekam na połączenie')
                    continue
                self.store.mark_blocked(
                    claimed["dispatch_id"], now, "adapter-disabled"
                )
                self.notifier.publish(
                    claimed["dispatch_id"], models.BLOCKED, now,
                    detail="cel wylaczony",
                )
                continue
            thread = claimed.get("native_thread") or ""
            try:
                from openclaw_control.delivery import deliver

                row = deliver(
                    self.store,
                    adapter,
                    claimed["dispatch_id"],
                    now,
                    claim_token=claimed.get("claim_token"),
                )
            except StoppedError:
                out.append({"dispatch_id": claimed["dispatch_id"], "state": "stopped"})
                continue
            except AdapterBusyError:
                self.store.unclaim(claimed["dispatch_id"], now)
                skip.add(thread)
                skip.add(claimed.get("target_binding") or "")
                continue
            except AdapterUnavailableError:
                self.store.unclaim(claimed["dispatch_id"], now)
                skip.add(claimed.get('target_binding') or claimed['dispatch_id'])
                self.notifier.publish(
                    claimed["dispatch_id"], models.QUEUED, now,
                    detail="Desktop niedostępny; czekam na połączenie",
                )
                continue
            if row.get("state") == models.DELIVERY_UNKNOWN:
                full = self.store.require_dispatch(claimed["dispatch_id"])
                self._reconcile_unknown(full, now)
                row = self.store.require_dispatch(claimed["dispatch_id"])
            elif row.get("state") == models.DELIVERED and hasattr(
                adapter, "start_turn"
            ):
                full = self.store.require_dispatch(claimed["dispatch_id"])
                try:
                    receipt = adapter.start_turn(full, 1)
                    if receipt.get("started"):
                        row = self.store.record_start(claimed["dispatch_id"], now)
                        if receipt.get("state") == "completed":
                            row = self.store.record_result(
                                claimed["dispatch_id"], models.COMPLETED, now
                            )
                except Exception:
                    pass
            self.notifier.publish(
                claimed["dispatch_id"], row.get("state") or "", now
            )
            out.append(redacted_summary(row))
        return out

    def supervise(self) -> list:
        now = self.now()
        decisions = []
        for spec in self.cfg.targets:
            if not (spec.enabled and spec.verified and spec.native_thread):
                continue
            adapter = self.registry.get(spec.provider)
            if not hasattr(adapter, "status_of"):
                decisions.append(
                    {
                        "target_id": spec.id,
                        "decision": "observe-only",
                        "reason": "adapter-not-status-capable",
                    }
                )
                continue
            grant = self.store.get_supervision(spec.id) or {}
            decisions.append(
                consider_resume(
                    self.store,
                    adapter,
                    spec.id,
                    spec.native_thread,
                    now,
                    auto_resume=self.cfg.auto_resume,
                    grant_ref=str(grant.get("grant_ref") or ""),
                    grant_expires=float(grant.get("grant_expires") or 0),
                    max_no_progress=self.cfg.max_no_progress_attempts,
                )
            )
        return decisions

    def once(self) -> dict:
        if not self.cfg.enabled:
            return self.status_doc()
        if self.registry_factory is not None:
            self.registry.close()
            self.registry = self.registry_factory()
        recovery = self.recover()
        if self.cfg.enabled and self.cfg.telegram_enabled:
            try:
                self.validate_bot()
            except BotApiError:
                self.bot_ok = False
                self.last_error = "getMe-failed"
            if self.bot_ok:
                self._send_menu(self.now(), force=False)
                intake = self.poll_and_intake()
            else:
                intake = []
        else:
            intake = []
        if self.cfg.telegram_enabled and not self.bot_ok:
            return {'schema': models.SCHEMA_VERSION, 'kind': 'runtime-blocked',
                    'payload': {'reason': self.last_error, 'bot_ok': False}}
        self.observe_dispatches()
        dispatched = self.dispatch_once()
        supervision = self.supervise()
        helpdesk = self.helpdesk.tick(self.store, self.now())
        return {
            "schema": models.SCHEMA_VERSION,
            "kind": "runtime-once",
            "payload": {
                "recovery": recovery,
                "intake": [
                    {k: v for k, v in r.items() if k != "raw_text"}
                    for r in intake
                ],
                "dispatched": dispatched,
                "supervision": supervision,
                "helpdesk": {
                    k: helpdesk[k]
                    for k in helpdesk
                    if k in ("action", "new", "status")
                },
                "stopped": self.store.is_stopped(),
                "bot_ok": self.bot_ok,
                "last_error": self.last_error,
            },
        }

    def status_doc(self) -> dict:
        rows = [
            redacted_summary(r) for r in self.store.list_dispatches()
        ]
        return {
            "schema": models.SCHEMA_VERSION,
            "kind": "runtime-status",
            "payload": {
                "enabled": self.cfg.enabled,
                "telegram_enabled": self.cfg.telegram_enabled,
                "stopped": self.store.is_stopped(),
                "stops": self.store.list_stops(),
                "adapters": self.registry.listing(),
                "limitations": [
                    "Native cancel absent; running-turn STOP stays cancel_pending.",
                    "Receiver CAS absent; broker fencing is not receiver CAS.",
                    "Status API omits latest turn items; matching uses rollout tail.",
                    "UNKNOWN reconciles by readback only; never resend.",
                    "Claude Desktop unverified; generic registration disabled; no CLI fallback.",
                    "Idle is not semantic goal completion.",
                    "No public listener, no WSL listener, no webhook takeover.",
                ],
                "dispatches": rows,
                "menu_shown": bool(self.store.get_kv(models.RUNTIME_MENU_SHOWN)),
                "poll_offset": self.store.get_poll_cursor(
                    models.POLL_CURSOR_TELEGRAM
                ),
            },
        }

    def serve_forever(self, *, loops: Optional[int] = None) -> None:
        remaining = loops
        while remaining is None or remaining > 0:
            self.once()
            if remaining is not None:
                remaining -= 1
            if remaining == 0:
                break
            import time

            time.sleep(1.0 if self.bot_ok else 5.0)
