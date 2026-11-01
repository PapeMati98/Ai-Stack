"""Telegram update intake: owner-only, literal prompts, explicit callbacks.

Rules (payload mode):
- Only a private chat with the paired owner chat id, from an allowlisted
  owner sender id, is admitted. Groups, channels, bots, via_bot,
  sender_chat impersonation, and any forwarded content are rejected.
- Message text is data. Prefixes /, !, @ stay literal; even the word
  STOP in text is a literal prompt. STOP happens ONLY via an explicit
  callback/control action, never via text.
- Message edits are NEW correction dispatches (never history rewrites).
- Duplicate update_ids are durable no-ops; out-of-order delivery is
  accepted (reorder-safe: dedupe only, no sequence enforcement).
- Voice, oversize text, and attachments yield an explicit unsupported
  receipt (persisted) -- never a truncation, summary, or transcription
  presented as the raw prompt.
- Raw text is stored ONLY in the protected local DB. Intake results
  carry hashes/reasons/ids, never message text.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from openclaw_control import models
from openclaw_control.store import StoppedError


class CrashInjected(Exception):
    """Test-only hook: simulate a crash at a named intake boundary."""

    def __init__(self, where: str) -> None:
        self.where = where
        super().__init__(where)

# Content keys that are NOT literal text prompts.
ATTACHMENT_KEYS = (
    "photo",
    "document",
    "voice",
    "video_note",
    "video",
    "audio",
    "animation",
    "sticker",
    "location",
    "venue",
    "contact",
    "poll",
    "dice",
    "story",
    "media_group_id",
)

# Any of these marks a message as forwarded (never inherits owner rights).
FORWARD_KEYS = (
    "forward_date",
    "forward_from",
    "forward_origin",
    "forward_sender_name",
    "forward_from_chat",
    "forward_signature",
)


@dataclass(frozen=True)
class OwnerConfig:
    owner_user_ids: tuple
    owner_chat_id: int


def _rejected(reason: str, update_id: Any = None, **extra: Any) -> dict:
    out: dict[str, Any] = {"kind": "rejected", "reason": reason}
    if update_id is not None:
        out["update_id"] = update_id
    out.update(extra)
    return out


def _sender_of(msg: dict) -> tuple[Any, bool]:
    frm = msg.get("from") or {}
    if not isinstance(frm, dict):
        return None, False
    return frm.get("id"), bool(frm.get("is_bot"))


def intake_update(
    store: Any,
    update: dict,
    cfg: OwnerConfig,
    now: float,
    *,
    crash_after: Optional[str] = None,
) -> dict:
    """Intake one Telegram update dict. Never raises on hostile input.

    Receipt is stashed durably BEFORE enqueue. A crash after stash leaves
    the payload in cc_inbox for recover_pending. Enqueue + mark-processed
    share one IMMEDIATE transaction so a committed prompt cannot be lost.
    """
    if not isinstance(update, dict):
        return _rejected("bad-update")
    update_id = update.get("update_id")
    if not isinstance(update_id, int) or isinstance(update_id, bool):
        return _rejected("missing-update-id")
    # Reject foreign content before persisting any payload to the protected inbox.
    query = update.get('callback_query')
    sender = query if isinstance(query, dict) else (update.get('edited_message') or update.get('message'))
    if not isinstance(sender, dict):
        return _rejected('no-message', update_id)
    sender_id, sender_is_bot = _sender_of(sender)
    if sender_is_bot:
        return _rejected('bot-sender', update_id)
    if type(sender_id) is not int or sender_id not in cfg.owner_user_ids:
        return _rejected('wrong-sender', update_id)
    message = query.get('message') if isinstance(query, dict) else sender
    if not isinstance(message, dict) or not isinstance(message.get('chat'), dict):
        return _rejected('bad-chat', update_id)
    chat = message['chat']
    if type(chat.get('id')) is not int or chat['id'] != cfg.owner_chat_id or chat.get('type') != 'private':
        return _rejected('wrong-chat', update_id)
    if message.get('via_bot') or message.get('sender_chat'):
        return _rejected('impersonation', update_id)
    if any(key in message for key in FORWARD_KEYS):
        return _rejected('forwarded', update_id)
    payload = json.dumps(update, ensure_ascii=False)
    state = store.stash_inbox(update_id, payload, now)
    if crash_after == "after-stash":
        raise CrashInjected("after-stash")
    if state == "processed":
        return {"kind": "duplicate", "update_id": update_id}
    return _process_stashed(store, update, cfg, now, crash_after=crash_after)


def recover_pending(store: Any, cfg: OwnerConfig, now: float) -> list:
    """Replay unprocessed inbox rows after a crash. Idempotent."""
    out = []
    for item in store.pending_inbox():
        try:
            update = json.loads(item["payload_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            store._begin()
            try:
                store.mark_inbox_processed(
                    int(item["update_id"]), "bad-inbox", now
                )
                store._commit()
            except BaseException:
                store._rollback()
                raise
            out.append(_rejected("bad-inbox", item["update_id"]))
            continue
        out.append(_process_stashed(store, update, cfg, now))
    return out


def _process_stashed(
    store: Any,
    update: dict,
    cfg: OwnerConfig,
    now: float,
    *,
    crash_after: Optional[str] = None,
) -> dict:
    update_id = update.get("update_id")
    store._begin()
    try:
        existing = store.get_inbox(int(update_id))
        if existing is not None and int(existing.get("processed") or 0):
            store._commit()
            return {"kind": "duplicate", "update_id": update_id}
        result = _intake_body(store, update, cfg, now, int(update_id))
        store.mark_inbox_processed(
            int(update_id), str(result.get("kind") or ""), now
        )
        if crash_after == "after-enqueue":
            raise CrashInjected("after-enqueue")
        store._commit()
        return result
    except CrashInjected:
        store._rollback()
        raise
    except BaseException:
        store._rollback()
        raise


def _intake_body(
    store: Any, update: dict, cfg: OwnerConfig, now: float, update_id: int
) -> dict:
    if "callback_query" in update:
        query = update.get("callback_query")
        if not isinstance(query, dict):
            return _rejected("bad-callback", update_id)
        return intake_callback(store, query, cfg, now, update_id)

    is_edit = isinstance(update.get("edited_message"), dict)
    msg = update.get("edited_message") if is_edit else update.get("message")
    if not isinstance(msg, dict):
        return _rejected("no-message", update_id)

    sender_id, sender_is_bot = _sender_of(msg)
    if sender_is_bot:
        return _rejected(
            "bot-sender", update_id, sender_id=sender_id
        )
    if sender_id not in cfg.owner_user_ids:
        return _rejected(
            "wrong-sender", update_id, sender_id=sender_id
        )
    chat = msg.get("chat") or {}
    if not isinstance(chat, dict):
        return _rejected("bad-chat", update_id)
    chat_id = chat.get("id")
    if chat.get("type") != "private" or chat_id != cfg.owner_chat_id:
        return _rejected("wrong-chat", update_id, chat_id=chat_id)
    if msg.get("via_bot") or msg.get("sender_chat"):
        return _rejected("impersonation", update_id)
    for key in FORWARD_KEYS:
        if key in msg:
            return _rejected("forwarded", update_id)

    message_id = msg.get("message_id") or 0
    for key in ATTACHMENT_KEYS:
        if key in msg:
            receipt_id = store.add_receipt(
                int(chat_id), int(message_id), "unsupported:%s" % key, now
            )
            return {
                "kind": "unsupported",
                "reason": "attachment",
                "detail": key,
                "receipt_id": receipt_id,
                "update_id": update_id,
            }
    text = msg.get("text")
    entities = msg.get("entities") or []
    if not isinstance(entities, list):
        entities = []
    if not isinstance(text, str) or text == "":
        return _rejected("empty-text", update_id)
    # Exact text: never trimmed, normalised, or command-parsed here.
    if len(text) > models.TELEGRAM_TEXT_LIMIT:
        receipt_id = store.add_receipt(
            int(chat_id), int(message_id), "unsupported:too-large", now
        )
        return {
            "kind": "unsupported",
            "reason": "too-large",
            "chars": len(text),
            "receipt_id": receipt_id,
            "update_id": update_id,
        }

    selection = store.get_selection(int(chat_id))
    bound = {
        "task_id": selection["task_id"] if selection else "",
        "generation": int(selection["generation"]) if selection else 0,
        "provider": selection["provider"] if selection else "",
        "native_thread": selection["native_thread"] if selection else "",
        "target_binding": selection["target_binding"] if selection else "",
    }
    if is_edit:
        base = store.find_dispatch_by_message(
            int(chat_id), int(message_id)
        )
        # Correction MUST bind to the ORIGINAL message target, not a
        # newly selected target. If the original is unknown, stay unbound
        # rather than routing to the current selection.
        if base is not None:
            bound = {
                "task_id": base.get("task_id") or "",
                "generation": int(base.get("generation") or 0),
                "provider": base.get("provider") or "",
                "native_thread": base.get("native_thread") or "",
                "target_binding": base.get("target_binding") or "",
            }
            correction_of = base["dispatch_id"]
        else:
            bound = {
                "task_id": "",
                "generation": 0,
                "provider": "",
                "native_thread": "",
                "target_binding": "",
            }
            correction_of = ""
        try:
            row = store.enqueue_dispatch(
                task_id=bound["task_id"],
                generation=bound["generation"],
                provider=bound["provider"],
                native_thread=bound["native_thread"],
                raw_text=text,
                entities=entities,
                now=now,
                target_binding=bound["target_binding"],
                correction_of=correction_of,
                source_update_id=update_id,
            )
        except StoppedError:
            return _rejected("stopped", update_id)
        return {
            "kind": "correction",
            "dispatch_id": row["dispatch_id"],
            "correction_of": correction_of,
            "text_sha256": row["text_sha256"],
            "bound": bool(bound["target_binding"]),
            "update_id": update_id,
        }
    try:
        row = store.enqueue_dispatch(
            task_id=bound["task_id"],
            generation=bound["generation"],
            provider=bound["provider"],
            native_thread=bound["native_thread"],
            raw_text=text,
            entities=entities,
            now=now,
            target_binding=bound["target_binding"],
            source_update_id=update_id,
        )
    except StoppedError:
        return _rejected("stopped", update_id)
    store.note_message(
        int(chat_id), int(message_id), row["dispatch_id"], now
    )
    return {
        "kind": "prompt" if bound["target_binding"] else "prompt-unbound",
        "dispatch_id": row["dispatch_id"],
        "text_sha256": row["text_sha256"],
        "update_id": update_id,
    }


def intake_callback(
    store: Any,
    query: dict,
    cfg: OwnerConfig,
    now: float,
    update_id: Any = None,
) -> dict:
    """Intake one callback_query dict. Consumes actions transactionally."""
    sender_id, sender_is_bot = _sender_of(query)
    if sender_is_bot:
        return _rejected("bot-sender", update_id, sender_id=sender_id)
    if sender_id not in cfg.owner_user_ids:
        return _rejected("wrong-sender", update_id, sender_id=sender_id)
    msg = query.get("message") or {}
    chat = msg.get("chat") or {} if isinstance(msg, dict) else {}
    chat_id = chat.get("id")
    if chat.get("type") != "private" or chat_id != cfg.owner_chat_id:
        return _rejected("wrong-chat", update_id, chat_id=chat_id)
    data = query.get("data")
    if not isinstance(data, str) or not data:
        return _rejected("empty-callback", update_id)
    peeked = store.peek_callback(data)
    if peeked is None:
        return _rejected("forged-callback", update_id)
    kind = peeked.get("kind") or ""
    if kind == "target_select":
        from openclaw_control.store import (
            ExpiredCallbackError,
            ReplayedCallbackError,
            UnknownCallbackError,
        )

        try:
            scope = peeked.get('scope') or ''
            if scope.startswith('dispatch:'):
                bound_result = store.consume_and_bind(data, scope.removeprefix('dispatch:'), now)
                entry = bound_result['callback']
            else:
                entry = store.consume_callback(data, now)
        except (UnknownCallbackError,) as exc:
            return _rejected("forged-callback", update_id,
                             detail=str(exc))
        except (ReplayedCallbackError,) as exc:
            return _rejected("replayed-callback", update_id,
                             detail=str(exc))
        except (ExpiredCallbackError,) as exc:
            return _rejected("expired-callback", update_id,
                             detail=str(exc))
        store.set_selection(
            int(chat_id),
            entry["task_id"],
            int(entry["generation"]),
            entry["provider"],
            entry["native_thread"],
            data,
            now,
        )
        return {
            "kind": "target_selected",
            "task_id": entry["task_id"],
            "generation": entry["generation"],
            "target_binding": entry["target_binding"],
            "update_id": update_id,
        }
    if kind in ("stop", 'resume'):
        from openclaw_control.store import (
            CallbackError,
            ExpiredCallbackError,
            ReplayedCallbackError,
            UnknownCallbackError,
        )

        try:
            entry = store.consume_callback(data, now)
        except (UnknownCallbackError,) as exc:
            return _rejected("forged-callback", update_id,
                             detail=str(exc))
        except (ReplayedCallbackError,) as exc:
            return _rejected("replayed-callback", update_id,
                             detail=str(exc))
        except (ExpiredCallbackError,) as exc:
            return _rejected("expired-callback", update_id,
                             detail=str(exc))
        except (CallbackError,) as exc:
            return _rejected("callback-rejected", update_id,
                             detail=str(exc))
        scope = entry.get("scope") or models.STOP_SCOPE_ALL
        if kind == 'resume':
            store.clear_stop(scope)
            return {'kind': 'resumed', 'scope': scope, 'update_id': update_id}
        store.set_stop(scope, now, "owner-callback")
        return {"kind": "stopped", "scope": scope, "update_id": update_id}
    if kind in ("disabled_target", "show_menu", "result_summary"):
        from openclaw_control.store import (
            ExpiredCallbackError,
            ReplayedCallbackError,
            UnknownCallbackError,
        )

        try:
            entry = store.consume_callback(data, now)
        except (UnknownCallbackError,) as exc:
            return _rejected("forged-callback", update_id, detail=str(exc))
        except (ReplayedCallbackError,) as exc:
            return _rejected("replayed-callback", update_id, detail=str(exc))
        except (ExpiredCallbackError,) as exc:
            return _rejected("expired-callback", update_id, detail=str(exc))
        return {
            "kind": kind,
            "task_id": entry.get("task_id") or "",
            "update_id": update_id,
        }
    return _rejected("unknown-callback-kind", update_id)
